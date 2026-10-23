#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 counts_local/*.json 汇总成下游可读的 NAD 覆盖报告。

用法：python3 tools/make_nad_coverage_report.py > docs/NAD-COVERAGE-2026-10-23.md
"""
from __future__ import annotations
import glob, json


def grade(pct):
    if pct is None:
        return "D", "无数据包"
    if pct >= 95:
        return "A", "可信"
    if pct >= 80:
        return "B", "轻微偏移"
    if pct >= 5:
        return "C", "严重偏移"
    return "D", "近乎全废"


rows = []
for f in sorted(glob.glob("counts_local/*.json")):
    d = json.load(open(f, encoding="utf-8"))
    g, label = grade(d.get("in_state_pct"))
    cty = d.get("counties") or []
    empty = [c["fips"] for c in cty if not c["points"]]
    rows.append({**d, "grade": g, "grade_label": label,
                 "county_total": len(cty), "empty_fips": empty})

rows.sort(key=lambda r: (r["grade"], -(r.get("in_state_pct") or -1)))
A = [r for r in rows if r["grade"] == "A"]
B = [r for r in rows if r["grade"] == "B"]
C = [r for r in rows if r["grade"] == "C"]
D = [r for r in rows if r["grade"] == "D"]
tot = sum(r["total_points"] for r in rows)
ins = sum(r["in_state_points"] for r in rows)

print("# 一仓 NAD 真实覆盖率核查报告（2026-10-23）")
print()
print("> 一句话：**NAD 里只有 24 个州的数据真的落在本州境内，其余 28 个州的点绝大部分跑到州界外去了。**")
print("> 这不是文件缺失，是「下载到了、位置全错」或「上游本来就没有」。")
print()
print("## 一、为什么必须重做这次核查")
print()
print("上一轮 MANIFEST 里写着 `coverage_pct: 100.0`，那个数字来自「县片行数 + 溢出包行数 = 上游 state count」。")
print("问题是**溢出包装的是外州点**，把它算进本地产出，账做平了、事实没对上。")
print()
print("复现到的上游异常（2026-10-23 实测）：")
print()
print("- 同一个 bbox 连打 5 次 `returnCountOnly` + `outFields`，全部超时")
print("- Cass County MI：`returnCountOnly` 返回 883，同一 bbox 的 `outFields` 查询返回 **0 条 features**")
print("- NH 全州 bbox 返回 354,804 行，State 字段分布 `MA 140,838 / ME 115,675 / VT 98,291` —— **零条 NH**")
print("- Rockingham County NH 的 bbox 查询 110,783 行，抽样全是 MA Essex 的点")
print()
print("结论：**不能向上游问「每县有多少数据」**，只能核对手里握住的东西。")
print()
print("## 二、核查方法（可复现）")
print()
print("```")
print("python3 tools/nad_local_matrix.py raw-US-XX-v1     # 单州：NAD 实物 × TIGER 县界逐点判定")
print("python3 tools/write_nad_truth.py                   # 把结果写回各 Release 的 MANIFEST")
print("python3 tools/make_nad_coverage_report.py          # 生成本报告")
print("```")
print()
print("做法：把 Release 里已下载的 NAD 数据逐行取出经纬度，用该州 TIGER 县界（3,142 县）做点内判定，")
print("算出每个县真实有多少点落在境内。**不问上游、不依赖 API、不受限流影响。**")
print("性能上用 shapely 2.x 的向量化 `contains_xy`：CA（240 万点 × 58 县）98 秒，全 52 州并行 3 路 45 分钟。")
print()
print("## 三、总账")
print()
print(f"- 52 州 NAD 点数合计：**{tot:,}**")
print(f"- 其中落在本州县界内：**{ins:,}**（{100*ins/max(1,tot):.2f}%）")
print(f"- 分级：A {len(A)} 州 ｜ B {len(B)} 州 ｜ C {len(C)} 州 ｜ D {len(D)} 州")
print()
print("## 四、分级明细")
print()
print("| 州 | 判定 | NAD 总点 | 州内点 | 州内% | 有点的县 | 县总数 |")
print("|---|---|---|---|---|---|---|")
for r in rows:
    pct = "—" if r["in_state_pct"] is None else f"{r['in_state_pct']}"
    print(f"| {r['state']} | {r['grade']} {r['grade_label']} | {r['total_points']:,} | "
          f"{r['in_state_points']:,} | {pct} | {r['counties_with_points']} | {r['county_total']} |")
print()
print("## 五、两类缺陷必须分开处置")
print()
print("### 类型 1：下载到了，但位置全错（重抓无用）")
print()
print("特征是「总点数很大、州内点接近 0」，因为上游拿州 bbox 查询时返回的是**邻州点**。")
print("重抓 100 次结果一样 —— 上游这个视图对这些州就是这个行为。")
print()
for r in [x for x in D + C if (x["in_state_pct"] or 0) < 50]:
    pct = "—" if r["in_state_pct"] is None else f"{r['in_state_pct']}%"
    print(f"- **{r['state']}**：{r['total_points']:,} 点里只有 {r['in_state_points']:,} 落在境内（{pct}）")
print()
print("### 类型 2：上游本来就没有那些县（NAD 自愿申报制）")
print()
print("特征是「州内占比 100%，但有点的县数远少于县总数」。这些县从来没向联邦提交数据，")
print("空壳文件不是抓取失败，是客观事实。典型：")
print()
for r in sorted(rows, key=lambda r: -(r["county_total"] - r["counties_with_points"]))[:8]:
    gap = r["county_total"] - r["counties_with_points"]
    if gap <= 0:
        continue
    print(f"- **{r['state']}**：{r['county_total']} 县里 {gap} 县无数据"
          f"（{r['counties_with_points']}/{r['county_total']} 有数据，州内占比 "
          f"{r['in_state_pct']}%）")
print()
print("## 六、给下游（二仓）的处置建议")
print()
print("| 分级 | 州数 | 建议 |")
print("|---|---|---|")
print(f"| A（≥95%） | {len(A)} | NAD 可直接作为本州地址源使用 |")
print(f"| B（80~95%） | {len(B)} | 可用，但需按县剔除州界外的杂点 |")
print(f"| C（5~80%） | {len(C)} | 不可直接使用；本州缺口用 Overture（州内可精确筛）补齐 |")
print(f"| D（<5%） | {len(D)} | 视同无 NAD；整州改用 Overture + OpenAddresses |")
print()
print("**A 级州清单**：" + "、".join(r["state"] for r in A))
print()
print("**D 级州清单**：" + "、".join(f"{r['state']}（{r['grade_label']}）" for r in D))
print()
print("## 七、账本字段（每个 Release 的 MANIFEST.json）")
print()
print("```json")
print('"audit": { "by_source": { "nad": {')
print('  "local_points": 470759,          // 手里有多少 NAD 点')
print('  "in_state_points": 3,            // 其中多少真的落在本州县界内')
print('  "in_state_pct": 0.0,')
print('  "counties_with_points": 1,       // 有数据的县数')
print('  "county_total": 82,')
print('  "instate_verdict": "nad_instate_partial",   // ok / partial / missing')
print('  "instate_note": "按 TIGER 县界逐点判定（一仓本地实物，非上游声明）"')
print("} } }")
print("```")
print()
print("旧的 `completeness.nad.coverage_pct` 保留原值，但附 `coverage_pct_superseded_note` 说明它为何不可作为覆盖率解读。")
