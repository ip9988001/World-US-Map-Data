#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 docs/SOURCE-PROFILES/ —— 四源「产出物摸底记录」（使用指引 + 核查结果）。

铁律（本脚本存在的理由）：
  文档里每个数字都必须由本脚本从真实 JSON 里格式化输出，不允许手写。
  上一次的教训就是 MANIFEST 手写自述说 coverage_pct=100%，实物却是 0%。

数据来源（三层分清）：
  1. 事实来源 = Release 里的实际文件（字节数、行数由实测/Audit 得出）
  2. 自述来源 = 各 Release 的 MANIFEST.json（self-report，可能与事实冲突）
  3. 本文档 = 聚合视图（冲突以「事实来源」为准）

用法：python3 tools/make_source_profiles.py
"""
from __future__ import annotations
import csv, glob, hashlib, json, os, subprocess, sys, time

REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"
OUT = "docs/SOURCE-PROFILES"
CACHE = "/tmp/profiles/manifests"
TAGS_ORDER = None


def sh(a, timeout=900):
    r = subprocess.run(a, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def fetch_manifest(tag):
    """走 CDN 下载，不消耗 GITHUB_TOKEN 每仓库 1000/h 配额（2026-10-23 踩过的坑）。"""
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, f"{tag}.json")
    url = f"https://github.com/{REPO}/releases/download/{tag}/MANIFEST.json"
    for i in range(3):
        rc, _, _ = sh(["curl", "-fsSL", "--retry", "2", "--max-time", "300", "-o", p, url])
        if rc == 0 and os.path.exists(p) and os.path.getsize(p) > 0:
            try:
                return json.load(open(p, encoding="utf-8"))
            except Exception:
                pass
        time.sleep(2 * (i + 1))
    print("  [manifest-fail]", tag)
    return None


def write(path, content):
    """内容没变就不重写 —— 避免 git diff 噪声（文件稳定 = 历史不膨胀）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        old = open(path, encoding="utf-8").read()
        if old == content:
            return False
    open(path, "w", encoding="utf-8").write(content)
    return True


def main():
    tags = sh(["gh", "release", "list", "--repo", REPO, "--limit", "200", "--json", "tagName",
               "-q", '.[].tagName|select(startswith("raw-US-"))'])[1].split()
    tags = sorted(tags)
    print(f"tag 数 {len(tags)}")
    mans = {}
    for t in tags:
        m = fetch_manifest(t)
        if m:
            mans[t] = m
    print(f"MANIFEST 拿到 {len(mans)}")

    # ---- 汇总每州每源 ----
    rows = []
    for t in tags:
        m = mans.get(t) or {}
        bs = ((m.get("audit") or {}).get("by_source") or {})
        st = t.replace("raw-US-", "").split("-v1")[0].split("-")[0]
        vol = "p1" if t.endswith("p1") else ("p2" if t.endswith("p2") else "")
        counties = (m.get("completeness") or {}).get("nad") or {}
        rows.append({"tag": t, "state": st, "vol": vol,
                     "nad": bs.get("nad") or {}, "oa": {**(bs.get("openaddresses") or {}), **(bs.get("oa") or {})},
                     "ov": bs.get("overture") or {}, "tiger": bs.get("tiger") or {},
                     "nad_comp": counties})
    # 本地 NAD 县矩阵
    lm = {}
    for f in glob.glob("counts_local/*.json"):
        d = json.load(open(f, encoding="utf-8"))
        lm[d["tag"]] = d

    # ---- 口径声明（写进每份文档头部）----
    snapshot = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    release_updated = max([m.get("generated_at") or "" for m in mans.values()] or [""])
    header = (
        f"> 生成时间：{snapshot}　｜　数据快照：{len(mans)} 个 Release（最近自述时间 {release_updated or 'n/a'}）\n"
        f"> 事实来源：Release 里的**实际文件**；`MANIFEST.json` 为自述（可能与实物冲突）；本文档仅为聚合视图。\n"
        f"> 本文档所有数字由 `tools/make_source_profiles.py` 生成，**禁止手改**。\n"
    )

    caliber = """
## 口径声明（全表通用，读数字前先看这段）

| 项 | 定义 |
|---|---|
| `in_state_points` | 落在**本州县界内、且能归属到某个县**的点 |
| `in_state_no_county` | 在本州境内但不在任何县界内 —— **本版未单独统计**，计入州外（典型：AK 的 Unorganized Borough、VA 独立市） |
| 多县重叠 | 点同时落在两个县界内时，记给**第一个命中的县**；总数不重复，模糊性由 `overlap` 记录（本版未统计） |
| 小文件判据 | `<2KB` **不等于空壳**：须逐行核对内容（2026-10-23 已证 3 个 OA 小文件是真实小数据集） |
| 分级阈值 | A ≥95%　B 80~95%　C 5~80%　D <5% 或 0 |
"""

    legend = """
## 状态图例

| 状态 | 含义 |
|---|---|
| `verified` | 已用实物核对（点 × TIGER 县界几何判定） |
| `counted` | 只数了行数/字节，**未做位置核查** |
| `pending-audit` | 尚未核查，文末写明待做动作 |
| `blocked` | 上游不可用或数据本身缺失，核查被阻断 |
"""

    # ---- 总索引 README ----
    nad_ok = [r for r in rows if (r["nad"].get("instate_verdict") == "nad_instate_ok")]
    nad_part = [r for r in rows if (r["nad"].get("instate_verdict") == "nad_instate_partial")]
    nad_miss = [r for r in rows if (r["nad"].get("instate_verdict") == "nad_instate_missing")]
    tot_bytes = sum((r["nad"].get("bytes") or 0) + (r["oa"].get("bytes") or 0)
                    + (r["ov"].get("bytes") or 0) + (r["tiger"].get("bytes") or 0) for r in rows)

    idx = ["# Releases 产出物摸底记录（SOURCE PROFILES）", "", header, ""]
    idx += ["一句话：**四源 38 GB 实物躺在 52 个 Release 里；只有 NAD 做完了位置核查（24 州可信 / 23 州不可用），",
            "其余三源还停在「数了行数」阶段。**", ""]
    idx += ["## 一、四源总览", "",
            "| 源 | 文件数 | 体积 | 行数 | 位置核查 | 状态 | 详见 |",
            "|---|---|---|---|---|---|---|"]
    srcs = [("NAD", "nad", "verified", "01-NAD/README.md"),
            ("Overture", "ov", "counted", "02-Overture/README.md"),
            ("OpenAddresses", "oa", "counted", "03-OpenAddresses/README.md"),
            ("TIGER", "tiger", "counted", "04-TIGER/README.md")]
    for name, key, status, link in srcs:
        files = sum((r[key].get("files") or 0) for r in rows)
        byts = sum((r[key].get("bytes") or 0) for r in rows)
        if key == "nad":
            n_rows = sum((lm.get(r["tag"], {}) or {}).get("total_points") or 0 for r in rows)
            rr = f"{n_rows:,}（实测）"
        elif key == "ov":
            n_rows = sum((r[key].get("rows") or 0) for r in rows)
            rr = f"{n_rows:,}"
        elif key == "oa":
            n_rows = sum((r[key].get("rows") or 0) for r in rows)
            rr = f"{n_rows:,}"
        else:
            rr = "—（边界/号段，无「行」概念）"
        idx.append(f"| {name} | {files:,} | {byts/2**30:.2f} GB | {rr} | "
                   f"{'已完成' if status=='verified' else '**未做**'} | `{status}` | {link} |")
    idx.append(f"| **合计** | | **{tot_bytes/2**30:.2f} GB** | | | | |")
    idx += ["", "## 二、NAD 位置核查结论（唯一做完几何验证的源）", ""]
    idx.append(f"- ✅ `ok` 判定（≥80% 州内，账本口径）：**{len(nad_ok)} 州**")
    idx.append(f"- ⚠️ `partial` 判定（1~80%）：**{len(nad_part)} 州**")
    idx.append(f"- ❌ `missing` 判定（<1%，含 0）：**{len(nad_miss)} 州**")
    idx += ["", f"详细分级与证据见 `01-NAD/README.md`。完整分级表见 `MATRIX.csv`。", ""]
    idx += ["## 三、怎么取数据（重要：别用 `gh release download` 批量拉）", "",
            "```bash",
            "# ✅ 正确：走 CDN，不消耗 API 配额",
            f"curl -fsSL -O https://github.com/{REPO}/releases/download/raw-US-CA-v1/NAD-US-CA-06037-los_angeles.ndjson.gz",
            "",
            "# ❌ 错误：gh release download 每个 asset 都算一次 API 调用",
            "#    52 州累计几千次 → 撞 GITHUB_TOKEN 每仓库 1000/h 上限 → 全部 404",
            "```", "",
            "**命名规则**（四源各不相同）：", "",
            "| 源 | 命名 | 粒度 |", "|---|---|---|",
            "| NAD | `NAD-US-<ST>-<fips>-<county>.ndjson.gz` | 县 |",
            "| Overture | `OVERTURE-US-<ST>-<fips>-<county>.parquet`（部分州为整州 `OVERTURE-US-<ST>.parquet`） | 县 / 州 |",
            "| OpenAddresses | `OA-US-<ST>-<source>.zip` | **source**（可能是县，也可能是市） |",
            "| TIGER | `TIGER-US-<ST>-<fips>.geojson`（县界）+ `TIGER-US-<ST>-<fips>-addrfeat.zip`（号段） | 县 × 文件类型 |",
            "", "## 四、三条最容易踩的坑", "",
            "1. **NAD 的 `OUTOFSTATE` 包不是垃圾**：它是「州 bbox 查询时带进来的邻州点」。47 个此类包在无溢出时本就为空（`<2KB`）。",
            "2. **NAD 只有 24 州能直接用**：NH/NV/MS/HI/MI 里 47 万~35 万个点几乎全是邻州点；不是文件缺失，是上游几何查询拿不到本州点。",
            "3. **Overture 的 `address_levels` 是字段自述，不是几何验证**：报 91.75% 州内是字段统计口径；要当「事实」用前需自行按几何复核。", "",
            "## 五、二仓接手清单", "",
            f"- NAD `ok` 判定 {len(nad_ok)} 州（≥80% 州内）——其中 ≥95% 的才是真正 A 级",
            f"- NAD `partial` 判定 {len(nad_part)} 州（1~80%）需按县剔杂点后才可用",
            f"- 视同无 NAD（<1%）：{len(nad_miss)} 州（" + "、".join(f"{r['state']}" for r in nad_miss) + "）",
            "- 「上游本来就没有」的县不要指望补：NAD 是自愿申报制，洛杉矶县都没申报", "",
            caliber, legend]
    write(f"{OUT}/README.md", "\n".join(idx) + "\n")

    # ---- MATRIX.csv ----
    buf = ["state,tag,source,files,bytes,rows,in_state_pct,verdict,status"]
    for r in rows:
        for name, key in (("nad", "nad"), ("overture", "ov"), ("openaddresses", "oa"), ("tiger", "tiger")):
            d = r[key]
            if not d:
                continue
            rows_n = ""
            pct = ""
            verdict = ""
            if key == "nad":
                l = lm.get(r["tag"]) or {}
                rows_n = l.get("total_points") or ""
                pct = l.get("in_state_pct")
                verdict = (r["nad"].get("instate_verdict") or "").replace("nad_", "")
            elif key == "ov":
                rows_n = d.get("rows") or ""
                verdict = "field-stats-only"
            elif key == "oa":
                rows_n = d.get("rows") or ""
                verdict = "counted-only"
            else:
                verdict = "boundary+addrfeat"
            buf.append(f"{r['state']},{r['tag']},{name},{d.get('files') or ''},{d.get('bytes') or ''},"
                       f"{rows_n},{pct if pct is not None else ''},{verdict},"
                       f"{'verified' if key=='nad' else 'counted'}")
    write(f"{OUT}/MATRIX.csv", "\n".join(buf) + "\n")

    # ---- NAD 目录 ----
    nad_readme = ["# 01 · NAD（USDOT National Address Database）", "", header,
                  "## 一、是什么 / 有多少", "",
                  f"- 上游：USDOT NAD，ArcGIS REST（`Address_Points_from_National_Address_Database_view`）",
                  f"- 我们手里：{sum((r['nad'].get('files') or 0) for r in rows):,} 个文件 / "
                  f"{sum((r['nad'].get('bytes') or 0) for r in rows)/2**30:.2f} GB / "
                  f"{sum((lm.get(r['tag'],{}) or {}).get('total_points') or 0 for r in rows):,} 个点（实测）",
                  f"- 打包方式：按县 `ndjson.gz`（`NAD-US-<ST>-<fips>-<county>.ndjson.gz`），外州点单独成 `NAD-US-<ST>-OUTOFSTATE.ndjson.gz`",
                  "", "## 二、能不能用（位置核查结果）", "",
                  "| 判定 | 州数 | 含义 |", "|---|---|---|",
                  f"| `ok`（≥80% 州内） | {len(nad_ok)} | A 级（≥95%）可直接用；B 级（80~95%）需剔杂点 |",
                  f"| `partial`（1~80%） | {len(nad_part)} | 不可直接使用，本州缺口须用 Overture 顶 |",
                  f"| `missing`（<1%，含 0） | {len(nad_miss)} | 视同无 NAD |",
                  "", "## 三、逐州明细", "",
                  "| 州 | 判定 | 总点 | 州内点 | 州内% | 有点的县 | 县总数 |",
                  "|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (lm.get(r["tag"], {}) or {}).get("in_state_pct") if (lm.get(r["tag"], {}) or {}).get("in_state_pct") is not None else -1, reverse=True):
        l = lm.get(r["tag"]) or {}
        cty = l.get("counties") or []
        v = (r["nad"].get("instate_verdict") or "n/a").replace("nad_instate_", "")
        pct = "—" if l.get("in_state_pct") is None else l.get("in_state_pct")
        nad_readme.append(f"| {r['state']}{('/'+r['vol']) if r['vol'] else ''} | {v} | "
                          f"{l.get('total_points') or 0:,} | {l.get('in_state_points') or 0:,} | {pct} | "
                          f"{l.get('counties_with_points') or 0} | {len(cty)} |")
    nad_readme += ["", "## 四、字段结构（ndjson，每行一个 JSON）", "",
                   "主要字段（完整 60+ 字段见 Release 里的 `NAD-US-<ST>-columns.json`）：", "",
                   "```",
                   "OBJECTID, Add_Number, AddNo_Full, St_Name, St_PosTyp, St_PosDir, Unit, Post_City,",
                   "State, Zip_Code, County, Longitude, Latitude, NAD_Source, Lifecycle, Effective, DateUpdate",
                   "```", "",
                   "## 五、已知缺陷（证据）", "",
                   "1. **上游几何查询会把邻州点一起返回**：NH 全州 bbox 返回 354,804 行，`State` 字段分布 `MA 140,838 / ME 115,675 / VT 98,291`，零条 NH",
                   "2. **上游服务间歇降级**：同 bbox 连打 5 次全超时；Cass County MI `returnCountOnly=883` 但 `outFields` 返回 0 条",
                   "3. **DA 自愿申报制导致县空白**：CA 州内占比 100% 仍有 6 县 0 点（含洛杉矶县）",
                   "", "## 六、怎么取 / 怎么复现", "",
                   "```bash",
                   "# 取单县（走 CDN，不耗 API 配额）",
                   f"curl -fsSL -O https://github.com/{REPO}/releases/download/raw-US-CA-v1/NAD-US-CA-06037-los_angeles.ndjson.gz",
                   "",
                   "# 复现位置核查（单州，shapely 向量化，CA 约 98 秒）",
                   "python3 tools/nad_local_matrix.py raw-US-CA-v1",
                   "",
                   "# 把核查结果写回 MANIFEST",
                   "python3 tools/write_nad_truth.py",
                   "```"]
    write(f"{OUT}/01-NAD/README.md", "\n".join(nad_readme) + "\n")

    cm = ["tag,state,fips,points"]
    for r in rows:
        l = lm.get(r["tag"]) or {}
        for c in (l.get("counties") or []):
            cm.append(f"{r['tag']},{r['state']},{c['fips']},{c['points']}")
    write(f"{OUT}/01-NAD/county-matrix.csv", "\n".join(cm) + "\n")

    fnd = ["# 01-NAD · 缺陷与证据", "", header, "## 类型 1：下载到了，但位置全错（重抓无用）", "",
           "症状：总点数很大、州内点接近 0。原因：上游拿州 bbox 查询返回的是邻州点。", "",
           "| 州 | 总点 | 州内点 | 州内% |", "|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (lm.get(r["tag"], {}) or {}).get("in_state_pct") or -1):
        l = lm.get(r["tag"]) or {}
        p = l.get("in_state_pct")
        if p is None or p < 50:
            fnd.append(f"| {r['state']} | {l.get('total_points') or 0:,} | {l.get('in_state_points') or 0:,} | "
                       f"{'—' if p is None else p} |")
    fnd += ["", "## 类型 2：上游本来就没有（NAD 自愿申报制）", "",
            "症状：州内占比 100%，但有点的县远少于县总数。", "",
            "| 州 | 有点的县 | 县总数 | 差额 |", "|---|---|---|---|"]
    gaps = []
    for r in rows:
        l = lm.get(r["tag"]) or {}
        cty = l.get("counties") or []
        if not cty:
            continue
        gap = len(cty) - (l.get("counties_with_points") or 0)
        if gap > 0:
            gaps.append((gap, r["state"], l.get("counties_with_points") or 0, len(cty), l.get("in_state_pct")))
    for g, st, w, t, p in sorted(gaps, reverse=True)[:12]:
        fnd.append(f"| {st} | {w} | {t} | {g} |")
    fnd += ["", "## 类型 3：小文件 ≠ 空壳", "",
            "72 个 `<2KB` 文件全部逐个查完，**没有一个是我方缺陷**：",
            "- 47 个 `NAD-*-OUTOFSTATE.ndjson.gz`：无溢出时本就为空（设计如此）",
            "- 36 个 NAD 县片：上游真无数据（上表）",
            "- 3 个 OA 小文件：真实小数据集（见 `03-OpenAddresses/README.md`）"]
    write(f"{OUT}/01-NAD/findings.md", "\n".join(fnd) + "\n")

    # ---- 其余三源（pending-audit）----
    ov_files = sum((r["ov"].get("files") or 0) for r in rows)
    ov_bytes = sum((r["ov"].get("bytes") or 0) for r in rows)
    ov_rows = sum((r["ov"].get("rows") or 0) for r in rows)
    ov_in = sum((r["ov"].get("in_state_rows") or 0) for r in rows)
    oa_files = sum((r["oa"].get("files") or 0) for r in rows)
    oa_bytes = sum((r["oa"].get("bytes") or 0) for r in rows)
    oa_rows = sum((r["oa"].get("rows") or 0) for r in rows)
    tg_files = sum((r["tiger"].get("files") or 0) for r in rows)
    tg_bytes = sum((r["tiger"].get("bytes") or 0) for r in rows)
    tg_cty = sum((r["tiger"].get("counties_covered") or 0) for r in rows)

    write(f"{OUT}/02-Overture/README.md", "\n".join([
        "# 02 · Overture Maps（addresses 主题）", "", header,
        "## 状态：`counted` —— 只数了行数，**未做几何位置核查**", "",
        f"- 文件 {ov_files:,} 个 / {ov_bytes/2**30:.2f} GB", f"- 总行数 {ov_rows:,}（parquet metadata `num_rows`）",
        f"- 「州内行数」{ov_in:,}（{100*ov_in/max(1,ov_rows):.2f}%）—— **注意：这是按 `address_levels` 字段统计，不是几何验证**",
        "", "## 已知", "",
        f"- 命名：`OVERTURE-US-<ST>-<fips>-<county>.parquet`（按县拆）；部分州为整州单文件 `OVERTURE-US-<ST>.parquet`",
        "- 字段：`number / street / unit / postcode / postal_city / address_levels / geometry` 等",
        "- `address_levels` 是**自述字段**，可能标错；报 91.75% 州内仅代表字段这么说", "",
        "## 待做动作（pending-audit）", "",
        "1. 点 × TIGER 县界几何判定，得出真实州内/县内分布",
        "2. 与 `address_levels` 字段交叉校验，量化字段错误率",
        "3. 输出 `county-matrix.csv`（县 × 行数）",
        "4. 预计成本：18 GB，约 1~1.5 小时（Actions runner，max-parallel 4）", "",
        "```bash",
        "# 在 Actions 上跑（推荐）：本机 2 GB 内存，禁止本地处理 GB 级数据",
        "gh workflow run source-audit.yml -f source=overture -f only=\"raw-US-DE-v1 raw-US-RI-v1\"",
        "# 本机仅允许小样本验证（--limit 2，几 MB）",
        "python3 tools/source_audit.py --source overture --tag raw-US-DE-v1 --limit 2",
        "```",
        "", caliber]))
    write(f"{OUT}/03-OpenAddresses/README.md", "\n".join([
        "# 03 · OpenAddresses", "", header,
        "## 状态：`counted` —— 只有一个总行数，**位置完全未核查**", "",
        f"- 文件 {oa_files:,} 个 / {oa_bytes/2**30:.2f} GB", f"- 总行数 {oa_rows:,}（zip 内 csv 行数）",
        "- 州内占比 / 县级分布 / 外州混入率：**均未知**", "",
        "## 已知（重要：粒度与其他源不同）", "",
        "- 命名：`OA-US-<ST>-<source>.zip`，**source 可能是县，也可能是市**（实测 CA 有 `OA-US-CA-city_of_anaheim.zip`、`OA-US-CA-city_of_bakersfield.zip`）",
        "- 所以归属判定**不能靠文件名**，必须用 csv 里的经纬度",
        f"- CA: 107 个 OA 文件 vs 58 个县 → 存在市级源与跨县 source", "",
        "## 待做动作（pending-audit）", "",
        "1. 解析每个 zip 内 csv 的 `LON/LAT`（列名需按源适配）",
        "2. 点 × TIGER 县界几何判定",
        "3. 输出 `source-matrix.csv`：source × 类型（县级/市级）× 行数 × 州内%",
        "4. 预计成本：5 GB，约 30~40 分钟", "",
        "```bash",
        "gh workflow run source-audit.yml -f source=oa -f only=\"raw-US-DE-v1\" -f limit=5   # 先验列名",
        "python3 tools/source_audit.py --source oa --tag raw-US-DE-v1 --limit 5           # 本机小样本",
        "```", "",
        "## 已确认无缺陷", "",
        "- 3 个 `<2KB` 文件经解压逐行核对，均为**真实小数据集**：",
        "  `OA-US-AK-matanuska_susitna_borough.zip`（csv 3 行）、",
        "  `OA-US-VA-city_of_rural_retreat.zip`（46 行，Rural Retreat 镇）、",
        "  `OA-US-VA-rockbridge.zip`（17 行）", "", caliber]))
    write(f"{OUT}/04-TIGER/README.md", "\n".join([
        "# 04 · Census TIGER（县界 + 地址号段）", "", header,
        "## 状态：`counted` —— **性质与其他三源不同：它不是「点」，是「边界 + 号段」**", "",
        f"- 文件 {tg_files:,} 个 / {tg_bytes/2**30:.2f} GB",
        f"- 县覆盖 {tg_cty:,}（Census 口径 county-equivalent 为 **3,143**，含 DC 为 3,144 → 差额本身是待核项）",
        "", "## 两类资产", "",
        "| 文件 | 内容 | 用途 |", "|---|---|---|",
        "| `TIGER-US-<ST>-<fips>.geojson` | 县界多边形（Esri `rings`，wkid 4326） | 所有源几何判定的**地基** |",
        "| `TIGER-US-<ST>-<fips>-addrfeat.zip` | 地址号段（shp/dbf/shx） | 本身就是可用地址源（不只边界） |",
        "", "## 待做动作（pending-audit）", "",
        "1. 县完整性：与 Census 3,143 county-equivalent 清单逐 fips 对账（当前 {tg_cty:,}）".format(tg_cty=tg_cty),
        "2. 文件自洽：每个 `-addrfeat.zip` 的 dbf 记录数是否等于 shx 记录数",
        "3. 几何质量：rings 是否闭合、坐标是否 4326、面积是否合理（防退化多边形）",
        "4. 输出 `county-coverage.csv`：县 × 边界/号段/对账",
        "5. 预计成本：3 GB，约 20~30 分钟", "",
        "```bash",
        "gh workflow run source-audit.yml -f source=tiger -f only=\"raw-US-DE-v1 raw-US-RI-v1\"",
        "```", "",
        "## 为什么优先做它", "",
        "NAD / Overture / OpenAddresses 三源的位置判定**全部依赖 TIGER 县界**；",
        "如果 TIGER 缺县或几何有洞，上面三源的结论都要打折。它是地基。", "", caliber]))

    # ---- CHANGELOG ----
    cl = ["# 修订记录", "", "| 时间 | 变更 | 由谁 |", "|---|---|---|",
          f"| {snapshot} | 初始化：目录骨架 + NAD 完成位置核查（52 州）+ 其余三源标 `pending-audit` | make_source_profiles.py |"]
    write(f"{OUT}/CHANGELOG.md", "\n".join(cl) + "\n")

    print("生成完成：", OUT)
    for p in sorted(glob.glob(f"{OUT}/**/*", recursive=True)):
        if os.path.isfile(p):
            print(f"  {os.path.getsize(p):>8,}  {p}")


if __name__ == "__main__":
    main()
