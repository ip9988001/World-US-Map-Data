#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NAD 本地实物 × 县界 覆盖矩阵 —— 不问上游 API，只核对我们手里到底握住了什么。

背景（2026-10-23）：USDOT NAD 的 ArcGIS 视图在间歇降级 ——
  · 同一个 bbox 连打 5 次全部超时
  · returnCountOnly 返回 883，同一 bbox 的 outFields 查询返回 0 条 features
  · NH 全州 bbox 返回 354,804 行，State 分布 MA/ME/VT，零 NH
所以「问上游」已经不可信，改为核对实物：拿 Release 里已下载的 NAD 数据，
用 TIGER 县界做点内判定，算出每个县真正有多少点落在境内。

输出：counts_local/<TAG>.json
  {tag, state, counties:[{fips, points}], total_points, in_state_points,
   out_state_points, out_of_state_bbox_points}
"""
from __future__ import annotations
import gzip, json, os, re, subprocess, sys, tempfile

REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"
LON = re.compile(r'"Longitude"\s*:\s*(-?\d+\.?\d*)')
LAT = re.compile(r'"Latitude"\s*:\s*(-?\d+\.?\d*)')


def sh(a, timeout=1800, capture=True):
    r = subprocess.run(a, capture_output=capture, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def pip(lon: float, lat: float, rings: list) -> bool:
    inside = False
    for ring in rings:
        n = len(ring)
        if n < 3:
            continue
        j = n - 1
        for i in range(n):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
                inside = not inside
            j = i
    return inside


def main():
    tag = sys.argv[1]
    st = tag.replace("raw-US-", "").split("-v1")[0].split("-")[0]
    rc, out, err = sh(["gh", "api", "repos/%s/releases/tags/%s" % (REPO, tag)])
    if rc != 0:
        print("[api-fail]", (err or out)[:200]); sys.exit(1)
    assets = json.loads(out).get("assets", [])
    names = [a["name"] for a in assets]
    county_files = sorted(n for n in names if n.startswith(f"TIGER-US-{st}-county-"))
    nad_files = sorted(n for n in names if n.startswith("NAD-") and n.endswith(".ndjson.gz"))
    print(f"  {tag}: 县界 {len(county_files)} / NAD 包 {len(nad_files)}")
    tmp = tempfile.mkdtemp()

    def fetch(name):
        p = os.path.join(tmp, name)
        url = f"https://github.com/{REPO}/releases/download/{tag}/{name}"
        if sh(["curl", "-fsSL", "--retry", "3", "-o", p, url])[0] != 0:
            return None
        return p

    # 1) 县界 → bbox + rings
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
        if not rings:
            continue
        xs = [q[0] for r in rings for q in r]
        ys = [q[1] for r in rings for q in r]
        fips = name.split("-")[-1].split(".")[0]
        counties.append({"fips": fips, "rings": rings,
                         "bbox": (min(xs), min(ys), max(xs), max(ys)), "points": 0})
    print(f"  可用县界 {len(counties)}")

    # 2) NAD 实物逐行判点
    total = in_state = out_state = 0
    for name in nad_files:
        p = fetch(name)
        if not p:
            print("  [dl-fail]", name); continue
        n_this = 0
        with gzip.open(p, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                m1, m2 = LON.search(line), LAT.search(line)
                if not m1 or not m2:
                    continue
                lon = float(m1.group(1)); lat = float(m2.group(1))
                total += 1; n_this += 1
                hit = False
                for c in counties:
                    x0, y0, x1, y1 = c["bbox"]
                    if x0 <= lon <= x1 and y0 <= lat <= y1 and pip(lon, lat, c["rings"]):
                        c["points"] += 1
                        hit = True
                        break
                if hit:
                    in_state += 1
                else:
                    out_state += 1
        print(f"    {name}: {n_this:,} 行")
        os.remove(p)

    res = {"tag": tag, "state": st,
           "total_points": total, "in_state_points": in_state,
           "out_state_points": out_state,
           "in_state_pct": round(100.0 * in_state / total, 2) if total else None,
           "counties": [{"fips": c["fips"], "points": c["points"]} for c in counties],
           "counties_with_points": len([c for c in counties if c["points"] > 0])}
    os.makedirs("counts_local", exist_ok=True)
    json.dump(res, open(f"counts_local/{tag}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"  汇总: 总 {total:,} / 州内 {in_state:,} ({res['in_state_pct']}%) / "
          f"州外 {out_state:,} / 有数据的县 {res['counties_with_points']}/{len(counties)}")


if __name__ == "__main__":
    main()
