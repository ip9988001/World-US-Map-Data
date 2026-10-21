#!/usr/bin/env python3
"""TASK-001：Dover DE 19901 四源样本对照 + 全字段非空填充率

依据 https://docs.google.com/document/d/1DV2ph7smxUOJQg8lODnKpEP89oW9zSdNkIweVWGHXpA
（首行 TASK-001）执行：

  1. 四源各抽 20 条（共 80 条），保留原始列与原始值：
       · NAD  60 列 —— ZIP=19901，必须包含 5921 West Denneys Road 与 S State St / Loockerman St
       · Overture   —— ZIP=19901，全部原始属性
       · OpenAddresses us/de —— ZIP=19901，11 个原始列
       · Census TIGER —— 15 条 tl_2024_10001_addrfeat 道路门牌区间 + 5 条边界（ZCTA5 19901/19904、
                         Dover Place、Kent County、DE State）
  2. 四源全字段「非空填充率」统计表
  3. 产出直接可发布的交付文本（relay_post）

数据来源：**全部读仓库里已有的原始产物**，外加 NAD 官方 REST 现拉（不引入任何新数据源）。
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bots"))
import dover_fetch as df  # noqa: E402  复用锚定参数与官方通道

OUTDIR = ROOT / "data" / "dover" / "task001"
RAWD = ROOT / "data" / "dover" / "raw"
DOCS = ROOT / "docs"
ZIP = df.ZIP_MAIN
N_PER_SOURCE = 20


def blank(v) -> bool:
    return str(v if v is not None else "").strip().lower() in ("", "none", "null", "nan")


def log(m: str) -> None:
    print(f"[task001] {m}", flush=True)


def fillrate(rows: list[dict], fields: list[str], scope: str) -> list[dict]:
    out = []
    for f in fields:
        nn = sum(1 for r in rows if not blank(r.get(f)))
        out.append({"source": scope, "field": f, "non_null": nn, "total": len(rows),
                    "fill_pct": round(100.0 * nn / len(rows), 2) if rows else 0.0})
    return out


# ---------------------------------------------------------------- 四源读取
def overture_rows() -> list[dict]:
    gz, plain = RAWD / "overture_dover.geojson.gz", RAWD / "overture_dover.geojson"
    raw = gzip.decompress(gz.read_bytes()) if gz.exists() else plain.read_bytes()
    feats = (json.loads(raw.decode("utf-8")) or {}).get("features") or []
    rows = []
    for ft in feats:
        props = dict(ft.get("properties") or {})
        coords = ((ft.get("geometry") or {}).get("coordinates") or [None, None])
        props["lon"], props["lat"] = coords[0], coords[1]
        rows.append(props)
    return rows


def nad_pull() -> tuple[list[str], list[dict]]:
    _meta, fields = df.nad_official_metadata()
    rows = df.nad_official_pull(fields)
    for r in rows:                                  # 把几何并入 Longitude/Latitude 兜底
        if blank(r.get("Longitude")):
            r["Longitude"] = r.get("_lon")
        if blank(r.get("Latitude")):
            r["Latitude"] = r.get("_lat")
    return fields, rows


def openaddresses_rows() -> list[dict]:
    rows: list[dict] = []
    for name in ("openaddresses_city_of_dover.zip", "openaddresses_kent.zip"):
        p = RAWD / name
        if not p.exists():
            continue
        with zipfile.ZipFile(io.BytesIO(p.read_bytes())) as zf:
            for info in zf.infolist():
                if not info.filename.lower().endswith(".csv"):
                    continue
                with zf.open(info) as fh:
                    rows += [dict(r) for r in csv.DictReader(
                        io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline=""))]
    return rows


def tiger_addrfeat() -> tuple[list[str], list[dict]]:
    import shapefile                                # noqa: PLC0415
    p = RAWD / "tl_2024_10001_addrfeat.zip"
    with zipfile.ZipFile(io.BytesIO(p.read_bytes())) as zf:
        dbf = next(n for n in zf.namelist() if n.endswith(".dbf"))
        reader = shapefile.Reader(dbf=io.BytesIO(zf.read(dbf)))
        flds = [f[0] for f in reader.fields if f[0] != "DeletionFlag"]
        rows = [dict(zip(flds, rec)) for rec in reader.records()]
    return flds, rows


TIGER_BOUNDARY_TARGETS = [
    ("ZCTA5 19901", f"{df.TIGER_ZCTA}", f"ZCTA5='{ZIP}'"),
    ("ZCTA5 19904", f"{df.TIGER_ZCTA}", f"ZCTA5='{df.ZIP_ALT}'"),
    ("Dover Place", f"{df.TIGER_PLACES}", "STATE='10' AND BASENAME='Dover'"),
    ("Kent County", f"{df.TIGER_COUNTY}", "STATE='10' AND COUNTY='001'"),
    # DE 州界：不同 vintage 图层字段名不一，逐个候选试（TASK-001 明确给了两种判据）
    ("DE State", f"{df.TIGER_STATE}", "STUSPS='DE'"),
    ("DE State", f"{df.TIGER_STATE}", "STATEFP='10'"),
    ("DE State", f"{df.TIGER_STATE}", "STATE='10'"),
    ("DE State", f"{df.TIGER_STATE}", "STATE='10' AND NAME='Delaware'"),
]


def tiger_boundaries() -> tuple[list[dict], list[dict]]:
    got, ledger = [], []
    won: set[str] = set()
    for label, layer, where in TIGER_BOUNDARY_TARGETS:
        if label in won:
            continue
        try:
            payload = df.http_json(f"{layer}/query", {
                "where": where, "outFields": "*", "returnGeometry": "true",
                "outSR": "4326", "f": "json"}, timeout=90)
            feats = payload.get("features") or []
            ledger.append({"label": label, "where": where, "count": len(feats),
                           "fields": [f.get("name") for f in payload.get("fields") or []]})
            if feats:
                won.add(label)
                got.append({"source_label": label, "where": where, "layer": layer,
                            "count": len(feats), "attributes": feats[0].get("attributes"),
                            "geometry_type": (feats[0].get("geometry") or {}).get("rings") and "polygon" or "other",
                            "geometry_ring_pts": len(((feats[0].get("geometry") or {}).get("rings") or [[]])[0])})
                (OUTDIR / f"tiger_boundary_{label.split()[0].lower()}_{(where.split('=')[1] if '=' in where else 'x').strip(chr(39))}.json").write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            ledger.append({"label": label, "where": where, "error": str(exc)[:160]})
    return got, ledger


# ---------------------------------------------------------------- 抽样
STREET_RE = re.compile(r"DENNEYS|\bSTATE\b|LOOCKERMAN", re.I)


def pick_want(rows: list[dict], zip_field: str, street_field: str, n: int) -> list[dict]:
    """ZIP 命中者优先；架构师点名的 DENNEYS / State St / Loockerman St 置于最前。"""
    hit = [r for r in rows if str(r.get(zip_field, "")).strip()[:5] == ZIP]
    if not hit:
        hit = rows
    want = [r for r in hit if STREET_RE.search(str(r.get(street_field, "")))]
    rest = [r for r in hit if r not in want]
    out, seen = [], set()
    for r in want + rest:
        key = json.dumps(r, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= n:
            break
    return out


# ---------------------------------------------------------------- 主流程
def main() -> int:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    DOCS.mkdir(parents=True, exist_ok=True)
    log("读四源…")
    ov_rows = overture_rows()
    nad_fields, nad_rows = nad_pull()
    oa_rows = openaddresses_rows()
    af_fields, af_rows = tiger_addrfeat()
    tb_got, tb_ledger = tiger_boundaries()

    ov_fields = sorted({k for r in ov_rows[:5000] for k in r})
    oa_fields = list(oa_rows[0].keys()) if oa_rows else []

    nad_pick = pick_want(nad_rows, "Zip_Code", "StNam_Full", N_PER_SOURCE)
    ov_pick = pick_want(ov_rows, "postcode", "street", N_PER_SOURCE)
    oa_pick = pick_want(oa_rows, "postcode", "street", N_PER_SOURCE)
    af_pick = pick_want(af_rows, "ZIP", "FULLNAME", 15)
    if len(af_pick) < 15:
        af_pick = af_rows[:15]

    # 统一信封
    def wrap(source: str, rec: dict, note: str = "") -> dict:
        return {"source": source, "note": note, "record": rec}

    records: list[dict] = []
    records += [wrap("USDOT NAD (official REST, 60 cols)", r,
                     "官方正牌通道 A；字段名按元数据动态解析") for r in nad_pick]
    records += [wrap("Overture Maps Addresses", r, "bbox 云端切片 + postcode 过滤") for r in ov_pick]
    records += [wrap("OpenAddresses us/de", r, "kent + city_of_dover 成品小包") for r in oa_pick]
    records += [wrap("Census TIGER addrfeat (road range)", r,
                     "tl_2024_10001_addrfeat：道路门牌区间表（非点地址）") for r in af_pick]
    for tb in tb_got:
        records.append(wrap(f"Census TIGER boundary ({tb['source_label']})",
                            {"where": tb["where"], "count": tb["count"], **{k: v for k, v in (tb["attributes"] or {}).items()}},
                            "行政区/邮编边界多边形（几何已单独落 JSON）"))

    (OUTDIR / "task001_records.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in records) + "\n", encoding="utf-8")

    union: list[str] = []
    for r in records:
        for k in r["record"]:
            if k not in union:
                union.append(k)
    write_fields = ["source", "note"] + union
    with (OUTDIR / "task001_records.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=write_fields, extrasaction="ignore")
        w.writeheader()
        for r in records:
            w.writerow({"source": r["source"], "note": r["note"], **r["record"]})

    rates: list[dict] = []
    rates += fillrate(nad_rows, nad_fields, "USDOT NAD (official, 60 cols)")
    rates += fillrate(ov_rows, ov_fields, "Overture Maps (bbox 全量)")
    rates += fillrate(oa_rows, oa_fields, "OpenAddresses us/de (全量)")
    rates += fillrate([{**r, "FULLNAME": r.get("FULLNAME", "")} for r in af_rows], af_fields,
                      "Census TIGER addrfeat (Kent 全量)")
    with (OUTDIR / "fillrate_4sources.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["source", "field", "non_null", "total", "fill_pct"])
        w.writeheader()
        w.writerows(rates)

    # ---------- 交付文本（可直接发到中继站 / 邮件）----------
    by_src: dict[str, list[dict]] = {}
    for r in rates:
        by_src.setdefault(r["source"], []).append(r)

    L: list[str] = []
    L.append("TASK-001 交付：Dover DE 19901 四源样本对照 + 全字段非空填充率")
    L.append("作战室（免登录直接打开）: https://gempi.gtop.dev")
    L.append("仓库: https://github.com/ip9988001/world-map-data-test")
    L.append("产物: data/dover/task001/task001_records.jsonl | task001_records.csv | "
             "fillrate_4sources.csv | docs/task001_report.md")
    L.append("")
    L.append("【一、四源规模与已抽样】")
    L.append(f"· USDOT NAD（官方正牌通道 A，字段 {len(nad_fields)} 列）: 全量 {len(nad_rows)} 行 / 已抽 {len(nad_pick)}")
    L.append(f"· Overture Maps（字段 {len(ov_fields)} 列）: 全量 {len(ov_rows)} 行 / 已抽 {len(ov_pick)}")
    L.append(f"· OpenAddresses us/de（字段 {len(oa_fields)} 列）: 全量 {len(oa_rows)} 行 / 已抽 {len(oa_pick)}")
    L.append(f"· Census TIGER addrfeat（字段 {len(af_fields)} 列）: 全量 {len(af_rows)} 行 / 已抽 {len(af_pick)}"
             f" + 边界 {len(tb_got)} 条")
    L.append("")
    L.append("【二、全字段非空填充率（关键字段；全量见 fillrate_4sources.csv）】")
    key_map = {
        "USDOT NAD (official, 60 cols)": ["Add_Number", "StNam_Full", "St_Name", "County", "Inc_Muni",
                                          "Post_City", "Zip_Code", "Plus_4", "Addr_Type", "Longitude",
                                          "Latitude", "UUID", "AddAuth", "Placement", "NAD_Source"],
        "Overture Maps (bbox 全量)": ["number", "street", "unit", "postcode", "city", "region",
                                      "country", "lon", "lat", "id", "address_levels", "sources"],
        "OpenAddresses us/de (全量)": list(oa_fields[:11]),
        "Census TIGER addrfeat (Kent 全量)": ["FULLNAME", "FROMHN", "TOHN", "ZIP", "PLUS4", "SIDE",
                                              "FROMADD", "TOADD", "TLID", "MTFCC"],
    }
    for src, rl in by_src.items():
        L.append(f"--- {src} ---")
        want = key_map.get(src, [])
        shown = [r for r in rl if r["field"] in want] or sorted(rl, key=lambda x: -x["fill_pct"])[:12]
        for r in shown:
            L.append(f"  {r['field']:<18} {r['fill_pct']:>6.2f}%   ({r['non_null']}/{r['total']})")
        avg = round(sum(r["fill_pct"] for r in rl) / len(rl), 2) if rl else 0
        L.append(f"  [该源 {len(rl)} 个字段整体平均非空率 {avg}%]")
        L.append("")
    L.append("【三、80 条样本（每条一行；完整键值对见 task001_records.jsonl）】")
    for i, r in enumerate(records, 1):
        rec = r["record"]
        if "Add_Number" in rec:
            line = (f"{i:>3}. [NAD] #{rec.get('Add_Number')} {rec.get('StNam_Full')} | "
                    f"{rec.get('Inc_Muni')}/{rec.get('County')}/{rec.get('State')} {rec.get('Zip_Code')}"
                    f"{'-' + str(rec.get('Plus_4')) if rec.get('Plus_4') else ''} | "
                    f"Unit={rec.get('Unit')} Type={rec.get('Addr_Type')} "
                    f"({rec.get('Longitude')},{rec.get('Latitude')})")
        elif "FULLNAME" in rec:
            line = (f"{i:>3}. [TIGER-range] {rec.get('FROMHN')}-{rec.get('TOHN')} {rec.get('FULLNAME')} "
                    f"| ZIP={rec.get('ZIP')} PLUS4={rec.get('PLUS4')} SIDE={rec.get('SIDE')} "
                    f"TLID={rec.get('TLID')}")
        elif "where" in rec:
            line = (f"{i:>3}. [TIGER-boundary] {r['source'].split('(')[-1].rstrip(')')} "
                    f"count={rec.get('count')} where={rec.get('where')} "
                    f"GEOID={rec.get('GEOID') or rec.get('STATE') or ''}")
        else:
            num = rec.get("number") or rec.get("NUMBER") or ""
            st = rec.get("street") or rec.get("STREET") or ""
            city = rec.get("city") or rec.get("CITY") or ""
            zc = rec.get("postcode") or rec.get("POSTCODE") or ""
            lon = rec.get("lon") or rec.get("LON") or ""
            lat = rec.get("lat") or rec.get("LAT") or ""
            tag = "OVERTURE" if "Overture" in r["source"] else "OA"
            line = f"{i:>3}. [{tag}] {num} {st} | {city} {zc} | ({lon},{lat})"
        L.append(line)
    L.append("")
    L.append("【四、方法与边界（如实披露）】")
    L.append("· NAD 走交通部官方正牌通道 A（xOi1kZaI0eWDREZv），字段 60 列由元数据动态解析；"
             "第三方阉割镜像 Do88DoK2xjTUCXd1 已在代码层拉黑。")
    L.append("· 抽取偏好：ZIP=19901 命中者优先，且 DENNEYS / State St / Loockerman St 置前（TASK-001 点名）。")
    L.append(f"· TIGER 边界命中：{', '.join(t['source_label'] for t in tb_got)}"
             f"（未命中候选 {[l for l in tb_ledger if 'count' in l and l['count'] == 0]}）")
    L.append("· TIGER addrfeat 是**道路门牌区间表**（FROMHN-TOHN/TLID），与前三源的「点地址」不同性质，不可直接合并。")
    L.append("· Overture 的 address_levels / sources 是嵌套结构，在宽表里按 JSON 字符串落库。")
    (OUTDIR / "relay_post.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    # ---------- 人类报告 ----------
    R = ["# TASK-001 报告 · Dover DE 19901 四源样本对照", "",
         f"> 生成 {__import__('time').strftime('%Y-%m-%d %H:%M:%SZ', __import__('time').gmtime())}｜"
         "运行位置 **GitHub Actions**｜依据 TASK-001（Google Doc 首行）", "",
         "## 1. 抽样结果", "",
         "| 源 | 字段数 | 全量行数 | 本次抽样 |", "|---|---:|---:|---:|",
         f"| USDOT NAD（官方正牌） | {len(nad_fields)} | {len(nad_rows)} | {len(nad_pick)} |",
         f"| Overture Maps | {len(ov_fields)} | {len(ov_rows)} | {len(ov_pick)} |",
         f"| OpenAddresses us/de | {len(oa_fields)} | {len(oa_rows)} | {len(oa_pick)} |",
         f"| Census TIGER addrfeat | {len(af_fields)} | {len(af_rows)} | {len(af_pick)} |",
         f"| Census TIGER boundary | - | - | {len(tb_got)} |", "",
         "## 2. 全字段非空填充率", ""]
    for src, rl in by_src.items():
        R += [f"### {src}（{len(rl)} 字段）", "", "| 字段 | 非空率 | 非空/总数 |", "|---|---:|---:|"]
        for r in sorted(rl, key=lambda x: -x["fill_pct"]):
            R.append(f"| `{r['field']}` | {r['fill_pct']}% | {r['non_null']}/{r['total']} |")
        R.append("")
    R += ["## 3. 产物", "",
          "- `data/dover/task001/task001_records.jsonl` — 80 条完整原始记录（JSONL，保留全部原始列）",
          "- `data/dover/task001/task001_records.csv` — 同上的宽表版",
          "- `data/dover/task001/fillrate_4sources.csv` — 全字段填充率（长表）",
          "- `data/dover/task001/relay_post.md` — 发往中继站的交付正文", ""]
    (DOCS / "task001_report.md").write_text("\n".join(R) + "\n", encoding="utf-8")

    log(f"完成：记录 {len(records)} 条｜填充率行 {len(rates)} 条｜边界命中 {[t['source_label'] for t in tb_got]}")
    log("── 交付正文预览 ──")
    for line in L[:14]:
        log(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
