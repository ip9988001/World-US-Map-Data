#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NAD 本地实物 × 县界 覆盖矩阵（shapely 向量化版）—— 不问上游 API，只核对手里握住了什么。

背景（2026-10-23）：USDOT NAD 的 ArcGIS 视图间歇降级（同 bbox 连打 5 次全超时、
returnCountOnly=883 但 outFields 返回 0 条），「问上游」已不可信。
改为核对实物：Release 里已下载的 NAD 数据 × TIGER 县界，算出每县真实点数。

性能：纯 Python 逐点射线法在 CA（240 万点 × 58 县）上要跑几十分钟，
改用 shapely 2.x 的向量化 contains_xy（C 实现），单州 10~20 秒。

输出：counts_local/<TAG>.json
"""
from __future__ import annotations
import gzip, json, os, re, subprocess, sys, tempfile

import numpy as np
from shapely import contains_xy, prepare
from shapely.geometry import Polygon

REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"
RE_LON = re.compile(r'"Longitude"\s*:\s*(-?\d+\.?\d*(?:[eE][-+]?\d+)?)')
RE_LAT = re.compile(r'"Latitude"\s*:\s*(-?\d+\.?\d*(?:[eE][-+]?\d+)?)')
CHUNK = 32 * 1024 * 1024


def sh(a, timeout=1800):
    r = subprocess.run(a, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def build_geom(rings: list):
    """Esri rings → shapely Polygon/MultiPolygon（首环保外框，其余按包含关系当洞）。"""
    if not rings:
        return None
    try:
        outer = rings[0]
        holes = [r for r in rings[1:] if len(r) >= 3]
        if len(outer) < 3:
            return None
        g = Polygon(outer, holes)
        if not g.is_valid:
            g = g.buffer(0)
        if g.is_empty:
            return None
        prepare(g)
        return g
    except Exception:
        return None


def main():
    tag = sys.argv[1]
    st = tag.replace("raw-US-", "").split("-v1")[0].split("-")[0]
    rc, out, err = sh(["gh", "api", "repos/%s/releases/tags/%s" % (REPO, tag)])
    if rc != 0:
        print("[api-fail]", (err or out)[:200]); sys.exit(1)
    names = [a["name"] for a in json.loads(out).get("assets", [])]
    county_files = sorted(n for n in names if n.startswith(f"TIGER-US-{st}-county-"))
    nad_files = sorted(n for n in names if n.startswith("NAD-") and n.endswith(".ndjson.gz"))
    print(f"  {tag}: 县界 {len(county_files)} / NAD 包 {len(nad_files)}")
    tmp = tempfile.mkdtemp()

    def fetch(name):
        p = os.path.join(tmp, name)
        url = f"https://github.com/{REPO}/releases/download/{tag}/{name}"
        if sh(["curl", "-fsSL", "--retry", "3", "-o", p, url])[0] != 0:
            print("  [dl-fail]", name); return None
        return p

    counties = []
    for name in county_files:
        p = fetch(name)
        if not p:
            continue
        try:
            doc = json.load(open(p, encoding="utf-8"))
            rings = ((doc["features"][0].get("geometry") or {}).get("rings")) or []
        except Exception:
            os.remove(p); continue
        os.remove(p)
        g = build_geom(rings)
        if g is None:
            continue
        fips = name.split("-")[-1].split(".")[0]
        x0, y0, x1, y1 = g.bounds
        counties.append({"fips": fips, "geom": g, "bounds": (x0, y0, x1, y1), "points": 0})
    print(f"  可用县界 {len(counties)}")

    total = 0
    for name in nad_files:
        p = fetch(name)
        if not p:
            continue
        n_this = 0
        with gzip.open(p, "rb") as f:
            rest = b""
            while True:
                blk = f.read(CHUNK)
                if not blk:
                    break
                buf = rest + blk
                cut = buf.rfind(b"\n")
                if cut < 0:
                    rest = buf; continue
                rest = buf[cut + 1:]
                txt = buf[:cut].decode("utf-8", "replace")
                lons = np.fromiter((float(x) for x in RE_LON.findall(txt)), dtype=np.float64)
                lats = np.fromiter((float(x) for x in RE_LAT.findall(txt)), dtype=np.float64)
                n = min(len(lons), len(lats))
                if n == 0:
                    continue
                lons, lats = lons[:n], lats[:n]
                total += n; n_this += n
                assigned = np.zeros(n, dtype=bool)
                for c in counties:
                    x0, y0, x1, y1 = c["bounds"]
                    m = (~assigned) & (lons >= x0) & (lons <= x1) & (lats >= y0) & (lats <= y1)
                    idx = np.nonzero(m)[0]
                    if idx.size == 0:
                        continue
                    hit = contains_xy(c["geom"], lons[idx], lats[idx])
                    if not hit.any():
                        continue
                    sel = idx[hit]
                    c["points"] += int(sel.size)
                    assigned[sel] = True
        print(f"    {name}: {n_this:,} 行")
        os.remove(p)

    in_state = sum(c["points"] for c in counties)
    res = {"tag": tag, "state": st, "total_points": total,
           "in_state_points": in_state, "out_state_points": total - in_state,
           "in_state_pct": round(100.0 * in_state / total, 2) if total else None,
           "counties": [{"fips": c["fips"], "points": c["points"]} for c in counties],
           "counties_with_points": len([c for c in counties if c["points"] > 0])}
    os.makedirs("counts_local", exist_ok=True)
    json.dump(res, open(f"counts_local/{tag}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"  汇总: 总 {total:,} / 州内 {in_state:,} ({res['in_state_pct']}%) / "
          f"有数据的县 {res['counties_with_points']}/{len(counties)}")


if __name__ == "__main__":
    main()
