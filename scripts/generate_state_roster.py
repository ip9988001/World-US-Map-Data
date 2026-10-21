#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成/刷新 51 州配置档 `rules/US/state_roster.yaml`（50 州 + DC）。

全部字段从官方源自动取，不手填：
  · bbox       ← TIGERweb 州界图层 `returnExtentOnly=true`（一次轻量请求，不拉几何）
  · counties   ← TIGERweb 县界图层（GEOID 5 位 FIPS + BASENAME），DC 无县则用自身兜底
  · est_rows   ← NAD `returnCountOnly`（州 bbox，含邻州四角，仅用于**排序预估**）
  · 排序       ← 按 est_rows 从小到大（小州先跑，先暴露边缘 case）

★ 合并策略：**已存在且手工调过的条目（bbox/places/oa_priority/note）一律保留**，
  只补缺失字段。避免把 DE/RI 这两个已验证州的精调参数冲掉。

用法：
  python3 scripts/generate_state_roster.py                # 联网刷新 + 合并，写回 roster
  python3 scripts/generate_state_roster.py --dry-run      # 只打印不写
  python3 scripts/generate_state_roster.py --no-count     # 跳过 NAD 计数（秒出，不排序）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROSTER = ROOT / "rules" / "US" / "state_roster.yaml"

TIGERWEB = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer"
LAYER_STATE, LAYER_COUNTY = 0, 1
NAD = ("https://services.arcgis.com/xOi1kZaI0eWDREZv/ArcGIS/rest/services/"
       "Address_Points_from_National_Address_Database_view/FeatureServer/0/query")
UA = "Mozilla/5.0 (compatible; world-us-map-data/1.0; +https://github.com/ip9988001/World-US-Map-Data)"

# 规模分档（**仅用于决定跑批顺序**，不是数据字段）：
#   依据人口/门牌量级常识分四档，同档内按字母序。精确计数不走 NAD 全表聚合
#   （实测服务端每次几十秒~数分钟，并发还会被降级，性价比极低）。
TIER = {
    "tiny": ["VT", "WY", "AK", "ND", "SD", "DE", "RI", "MT", "ME", "NH", "HI", "DC"],
    "small": ["NM", "WV", "MS", "AR", "IA", "KS", "NE", "ID", "UT", "NV", "CT", "OR",
              "OK", "KY", "LA", "AL", "SC", "MN", "CO", "MD"],
    "medium": ["WI", "MO", "IN", "TN", "VA", "MA", "AZ", "WA", "NJ", "NC", "MI", "GA"],
    "large": ["CA", "TX", "FL", "NY", "PA", "IL", "OH"],
}
TIER_OF = {abbr: t for t, arr in TIER.items() for abbr in arr}
TIER_ORDER = {"tiny": 0, "small": 1, "medium": 2, "large": 3}

# 50 州 + DC（排除属地 PR/VI/GU/AS/MP）
STATE_CODES = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT",
    "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL",
    "18": "IN", "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD",
    "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT", "31": "NE",
    "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
    "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA", "54": "WV",
    "55": "WI", "56": "WY",
}


def log(msg: str) -> None:
    print(f"[roster] {msg}", file=sys.stderr, flush=True)


def http_json(base: str, params: dict, timeout: int = 120) -> dict:
    url = base + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def http_post_json(url: str, params: dict, timeout: int = 180) -> dict:
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def state_bbox(code: str) -> list | None:
    """州界外接矩形：returnExtentOnly=true，一次轻量请求。"""
    for where in (f"STATE='{code}'", f"GEOID='{code}'"):
        try:
            d = http_json(f"{TIGERWEB}/{LAYER_STATE}/query",
                          {"where": where, "returnExtentOnly": "true",
                           "outSR": "4326", "f": "json"})
        except Exception:  # noqa: BLE001
            continue
        ext = d.get("extent") or {}
        if ext.get("xmin") is not None:
            return [round(float(ext[k]), 3) for k in ("xmin", "ymin", "xmax", "ymax")]
    return None


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")
    return s or "county"


def state_counties(code: str) -> list:
    try:
        d = http_json(f"{TIGERWEB}/{LAYER_COUNTY}/query",
                      {"where": f"STATE='{code}'", "outFields": "GEOID,BASENAME,NAME",
                       "returnGeometry": "false", "f": "json"})
    except Exception:  # noqa: BLE001
        return []
    out, seen = [], set()
    for f in d.get("features") or []:
        at = f.get("attributes") or {}
        fips = str(at.get("GEOID") or "")
        name = at.get("BASENAME") or at.get("NAME") or fips
        if len(fips) != 5 or fips in seen:
            continue
        seen.add(fips)
        out.append({"key": slug(name), "fips": fips, "name": str(name)})
    return sorted(out, key=lambda c: c["fips"])


def nad_count(bbox: list, timeout: int = 45) -> int | None:
    bb = ",".join(str(v) for v in bbox)
    try:
        d = http_post_json(NAD, {"where": "1=1", "geometry": bb,
                                 "geometryType": "esriGeometryEnvelope", "inSR": "4326",
                                 "spatialRel": "esriSpatialRelIntersects",
                                 "returnCountOnly": "true", "f": "json"}, timeout=timeout)
        c = d.get("count")
        return int(c) if isinstance(c, int) else None
    except Exception:  # noqa: BLE001
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成/刷新 51 州配置档（数据全部来自官方源）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-count", action="store_true",
                    help="（已废弃）旧版用于跳过 NAD 计数；现在排序改用静态分档，无需联网计数")
    ap.add_argument("--roster", default=str(ROSTER))
    args = ap.parse_args(argv)

    try:
        import yaml
    except ImportError:
        log("需要 pyyaml：pip install pyyaml")
        return 1

    path = Path(args.roster)
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    old_states = doc.get("states") or {}
    endpoints = doc.get("endpoints") or {
        "tiger_base": "https://www2.census.gov/geo/tiger/TIGER2024",
        "tigerweb": "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb",
        "openaddresses_home": "https://results.openaddresses.io/",
        "openaddresses_run": "https://data.openaddresses.io/runs/{run}/us/{st}/{src}.zip",
    }

    names = {}
    try:
        d = http_json(f"{TIGERWEB}/{LAYER_STATE}/query",
                      {"where": "1=1", "outFields": "STATE,NAME,GEOID",
                       "returnGeometry": "false", "f": "json"})
        for f in d.get("features") or []:
            at = f.get("attributes") or {}
            code = str(at.get("STATE") or "")
            if code in STATE_CODES:
                names[code] = str(at.get("NAME") or STATE_CODES[code]).replace(" ", " ")
    except Exception as exc:  # noqa: BLE001
        log(f"⚠️ 州名列表取失败（用内置映射兜底）：{exc}")

    # 先把 bbox / counties 取齐（串行、轻量），NAD 计数单独并行一轮（每个 45s 上限）
    base_entries: dict = {}
    for idx, (code, abbr) in enumerate(sorted(STATE_CODES.items()), 1):
        old = old_states.get(abbr) or {}
        bbox = old.get("bbox") or state_bbox(code)
        counties = old.get("counties") or state_counties(code)
        if abbr == "DC" and not counties:          # DC 无县 → 自身兜底成一个"县"
            counties = [{"key": "district_of_columbia", "fips": "11001",
                         "name": "District of Columbia"}]
        if not bbox or not counties:
            log(f"⚠️ {abbr} 缺 bbox({bool(bbox)}) / counties({len(counties)})，跳过")
            continue
        est = None
        entry = {
            "name": old.get("name") or names.get(code) or abbr,
            "state_fips": code,
            "bbox": bbox,
            "counties": counties,
        }
        if old.get("zcta_range"):
            entry["zcta_range"] = old["zcta_range"]
        entry["oa_priority"] = old.get("oa_priority") or []
        entry["places"] = old.get("places") or []
        if est is not None:
            entry["est_nad_rows"] = est
        entry["note"] = old.get("note") or ("自动生成：bbox/县清单来自 TIGERweb，未人工复核" if abbr not in ("DE", "RI") else "")
        base_entries[abbr] = entry
        log(f"  [{idx:>2}/51] {abbr} bbox={bbox} counties={len(counties)}")

    states: list = []
    for abbr, entry in base_entries.items():
        entry["size_tier"] = TIER_OF.get(abbr, "small")
        states.append((abbr, entry))

    states.sort(key=lambda x: (TIER_ORDER.get(x[1]["size_tier"], 9), x[0]))

    out = {"version": 1, "country": "US", "endpoints": endpoints,
           "states": {abbr: e for abbr, e in states}}
    text = ("# 美国州级下载配置档 · state_roster.yaml\n"
            "# ============================================================================\n"
            "# 由 `scripts/generate_state_roster.py` 自动生成 / 刷新（数据全部来自官方源）：\n"
            "#   bbox     ← TIGERweb 州界 returnExtentOnly（外接矩形，四源统一过滤口径）\n"
            "#   counties ← TIGERweb 县界（GEOID 5 位 FIPS + BASENAME）\n"
            "#   size_tier ← 静态分档 tiny/small/medium/large（仅用于决定跑批顺序）\n"
            "# 已有条目里的手工精调字段（bbox/places/oa_priority/note）会被保留，不会被冲掉。\n"
            "# ============================================================================\n\n"
            + yaml.safe_dump(out, allow_unicode=True, sort_keys=False, width=200))
    if args.dry_run:
        print(text[:3000])
        log(f"dry-run：共 {len(states)} 州，未写文件")
        return 0
    path.write_text(text, encoding="utf-8")
    order = [a for a, _ in states]
    by_tier = {}
    for a, e in states:
        by_tier.setdefault(e["size_tier"], []).append(a)
    log(f"✅ 写入 {path.relative_to(ROOT)}：{len(states)} 州")
    for t in ("tiny", "small", "medium", "large"):
        log(f"   {t:<7}（{len(by_tier.get(t, [])):>2}）: {by_tier.get(t, [])}")
    log(f"   跑批顺序（前 8）：{order[:8]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
