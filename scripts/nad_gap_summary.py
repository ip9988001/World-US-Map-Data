#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""汇总 nad_gap_plan.py 的每州 JSON → docs/NAD-COVERAGE-FULL-*.md

判据来自各州 JSON 本身（真实县界多边形），本脚本只做合并与统计，不重算、不猜数。
核心回答：NAD 空县里，哪些是「上游真无数据」（polygon 查询 0 条），
哪些是「我们有漏抓」（上游有数据、我们无片）。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone


def load(d):
    rows = []
    for f in sorted(glob.glob(os.path.join(d, "*.json"))):
        try:
            rows.append(json.load(open(f, encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="gap")
    ap.add_argument("--out", default="docs/NAD-COVERAGE-FULL-2026-10-30.md")
    ap.add_argument("--run-url", default="")
    ap.add_argument("--expect-states", type=int, default=51)
    a = ap.parse_args()

    rows = [r for r in load(a.dir) if r.get("state")]
    rows.sort(key=lambda r: r["state"])
    tot_c = sum(r.get("counties_total", 0) for r in rows)
    tot_h = sum(r.get("have_n", 0) for r in rows)
    tot_need = sum(r.get("need_n", 0) for r in rows)
    tot_fp = sum(r.get("bbox_false_positive_n", 0) for r in rows)
    tot_empty = sum(r.get("upstream_empty_n", 0) for r in rows)
    tot_fail = sum(r.get("query_failed_n", 0) for r in rows)
    need_states = [r for r in rows if r.get("need_n")]
    partial = [r for r in rows if r.get("status") != "ok"]

    L = []
    L.append("# NAD 全量县覆盖 · 多边形差集（2026-10-30）")
    L.append("")
    L.append(f"- 生成时间（UTC）：{datetime.now(timezone.utc):%Y-%m-%d %H:%M}")
    if a.run_url:
        L.append(f"- 产生本次数据的 workflow run：{a.run_url}")
    L.append("- 判据：**县真实多边形**（TIGER 县界 geojson 的 `rings` + `esriGeometryPolygon`），"
             "**不用 bbox**。bbox 会把邻县数据算进本县，系统性地假阳性。")
    L.append("- `need` = 上游（NAD Address Points 视图）在本县多边形内有数据，但我们 Release 里没有该县 NAD 片。")
    L.append("- `bbox_false_positive` = 多边形 0 条、bbox 有数据 → 该县「上游空」的结论是被 bbox 污染的假象，"
             "这些县实际属**上游真无数据**。")
    L.append("")
    L.append("## 一、总账")
    L.append("")
    L.append(f"- 覆盖州数：**{len(rows)}**（期望 {a.expect_states}；缺 "
             f"{max(0, a.expect_states - len(rows))} 州即未完成）")
    L.append(f"- 县总数：**{tot_c:,}**")
    L.append(f"- 我们已有 NAD 县片：**{tot_h:,}**（{100.0 * tot_h / max(1, tot_c):.1f}%）")
    L.append(f"- **漏抓 need：{tot_need:,} 县**（上游有数据、我们没片）")
    L.append(f"- 上游真无数据：**{tot_empty:,} 县**（多边形查询 0 条，不属我们的缺陷）")
    L.append(f"- bbox 假阳性：**{tot_fp:,} 县**（若用 bbox 会被误判成「上游有数据」，即方法性假红）")
    L.append(f"- 查询失败：**{tot_fail:,} 县**（API 超时/异常，需重跑该州）")
    L.append("")
    L.append("### 结论（上游真无数据 vs 我们漏抓）")
    L.append("")
    if tot_need == 0:
        L.append("**本轮全部州 need = 0。** 所有「NAD 空县」都是上游该县多边形内 0 条地址点，"
                 "属上游 NAD 覆盖不到，**不是我们漏抓**。")
    else:
        L.append(f"漏抓 {tot_need:,} 县分布在 **{len(need_states)} 个州**（见第三节逐县清单）；"
                 f"其余空县（{tot_empty:,}）是上游真无数据。")
    if tot_fp:
        L.append(f"\n另有 {tot_fp:,} 县若按 bbox 判会显示「上游有数据」——那 235 条之类的数字全落在邻县境内，"
                 "**bbox 判据不可用**（见 d549489 的 Clayton County GA 实证：bbox 235 条 / 多边形 0 条）。")
    L.append("")
    L.append("## 二、逐州四列")
    L.append("")
    L.append("| 州 | 县总数 | 已有 NAD 县片 | 漏抓 need | bbox 假阳性 | 上游真无数据 | 查询失败 | 状态 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        st = r["state"]
        mark = " ⚠️" if r.get("need_n") else ""
        L.append(f"| {st}{mark} | {r.get('counties_total', 0)} | {r.get('have_n', 0)} | "
                 f"**{r.get('need_n', 0)}** | {r.get('bbox_false_positive_n', 0)} | "
                 f"{r.get('upstream_empty_n', 0)} | {r.get('query_failed_n', 0)} | {r.get('status')} |")
    L.append("")
    missing = a.expect_states - len(rows)
    if missing > 0:
        L.append(f"> ⚠️ 缺 {missing} 州结果（artifact 未回来或 job 失败），本表不完整。")
        L.append("")
    L.append("## 三、need 逐县清单（上游有数据、我们没片）")
    L.append("")
    if not need_states:
        L.append("**无。** 全部州 need = 0。")
    else:
        for r in need_states:
            L.append(f"### {r['state']}（need {r['need_n']} / 共 {r.get('counties_total', 0)} 县）")
            L.append("")
            L.append("| FIPS | 县 | 上游多边形内地址点 |")
            L.append("|---|---|---|")
            for c in r.get("need", []):
                L.append(f"| {c.get('fips')} | {c.get('county') or '?'} | {c.get('upstream_count', 0):,} |")
            L.append("")
    L.append("## 四、查询失败（需重跑）")
    L.append("")
    if not partial:
        L.append("无失败；全部州 status = ok。")
    else:
        for r in partial:
            fs = ", ".join(f"{c.get('fips')} {c.get('county') or ''}".strip()
                           for c in r.get("query_failed", []))
            L.append(f"- **{r['state']}**：{r.get('query_failed_n', 0)} 县失败 —— {fs}")
    L.append("")
    L.append("## 五、复现")
    L.append("")
    L.append("```bash")
    L.append("python3 scripts/nad_gap_plan.py --state GA --out gap/GA.json --workers 16")
    L.append("python3 scripts/nad_gap_summary.py --dir gap --out docs/NAD-COVERAGE-FULL-2026-10-30.md")
    L.append("```")
    L.append("")
    L.append("或 `gh workflow run nad-gap-matrix.yml`（matrix 全州并行，每州 60 分钟超时，"
             "`reuse_existing=1` 可跳过已提交结果续跑）。")
    L.append("")

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    open(a.out, "w", encoding="utf-8").write("\n".join(L))
    print(f"写入 {a.out}：{len(rows)} 州，need={tot_need}，empty={tot_empty}，fp={tot_fp}，fail={tot_fail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
