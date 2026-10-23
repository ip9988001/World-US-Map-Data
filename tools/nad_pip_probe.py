#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NAD 逐县「真实覆盖」探针（PIP 版）—— 治 returnCountOnly 混入邻州点的假阳性。

背景（2026-10-23 实测）：NH Rockingham 县 bbox 查询 110,783 行，样本全是 MA Essex 的点；
NH 全州 bbox 返回 354,804 行，State 分布 MA/ME/VT —— 零条 NH。
即：NAD 的几何查询会把 bbox 内的邻州点一起返回，returnCountOnly 完全不可信。

做法：
  1. 从 Release 取该州 TIGER 县界 `TIGER-US-<ST>-county-<fips>.geojson`（Esri rings）
  2. 每县算 bbox，向 NAD 取样本点（returnGeometry=false，只要经纬度）
  3. 用县界 rings 做点内判定，统计「落在本县内的比例」
  4. 比例=0 → 该县在 NAD 几何查询下拿不到本州点（上游缺陷）；>0 → 上游确实有数据

输出：counts_pip/<TAG>.json
"""
from __future__ import annotations
import json, os, subprocess, sys, tempfile, time, urllib.parse, urllib.request

REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"
NAD = ("https://services.arcgis.com/xOi1kZaI0eWDREZv/ArcGIS/rest/services/"
       "Address_Points_from_National_Address_Database_view/FeatureServer/0/query")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
SAMPLE = int(os.environ.get("PIP_SAMPLE") or 1500)


def sh(a, timeout=600):
    r = subprocess.run(a, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def nad_post(params, tries=3):
    body = urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe=",").encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(NAD, data=body,
                                         headers={**UA, "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            time.sleep(2 * (i + 1))
    return {}


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


def sample_county(bbox: str, rings: list, count: int | None):
    """取样本点，返回 (sampled, in_poly)。count 已知时抽样点分段取，避开顺序偏差。"""
    offsets = [0]
    if count and count > SAMPLE * 2:
        offsets.append(count // 2)
    sampled = in_poly = 0
    for off in offsets:
        d = nad_post({"geometry": bbox, "geometryType": "esriGeometryEnvelope", "inSR": "4326",
                      "spatialRel": "esriSpatialRelIntersects",
                      "outFields": "Longitude,Latitude", "returnGeometry": "false",
                      "resultOffset": str(off), "resultRecordCount": str(SAMPLE // len(offsets)),
                      "f": "json"})
        for f in (d.get("features") or []):
            a = f.get("attributes") or {}
            try:
                lon = float(a.get("Longitude")); lat = float(a.get("Latitude"))
            except Exception:
                continue
            sampled += 1
            if pip(lon, lat, rings):
                in_poly += 1
    return sampled, in_poly


def county_bounds(rings: list) -> str:
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    return f"{min(xs):.6f},{min(ys):.6f},{max(xs):.6f},{max(ys):.6f}"


def main():
    tag = sys.argv[1]
    st = tag.replace("raw-US-", "").split("-v1")[0].split("-")[0]
    rc, out, err = sh(["gh", "api", "repos/%s/releases/tags/%s" % (REPO, tag)])
    assets = json.loads(out).get("assets", []) if rc == 0 else []
    geo = sorted([a["name"] for a in assets if a["name"].startswith(f"TIGER-US-{st}-county-")])
    print(f"  {tag}: 县界文件 {len(geo)} 个，样本量 {SAMPLE}")
    tmp = tempfile.mkdtemp()
    res = []
    for name in geo:
        fips = name.split("-")[-1].split(".")[0]
        p = os.path.join(tmp, name)
        url = f"https://github.com/{REPO}/releases/download/{tag}/{name}"
        if sh(["curl", "-fsSL", "--retry", "3", "-o", p, url], timeout=600)[0] != 0:
            res.append({"fips": fips, "error": "county geojson download failed"})
            continue
        try:
            doc = json.load(open(p, encoding="utf-8"))
            rings = ((doc["features"][0].get("geometry") or {}).get("rings")) or []
        except Exception as e:
            res.append({"fips": fips, "error": "parse:" + repr(e)[:60]}); os.remove(p); continue
        os.remove(p)
        if not rings:
            res.append({"fips": fips, "error": "no rings"}); continue
        bbox = county_bounds(rings)
        c = nad_post({"geometry": bbox, "geometryType": "esriGeometryEnvelope", "inSR": "4326",
                      "spatialRel": "esriSpatialRelIntersects", "returnCountOnly": "true", "f": "json"})
        cnt = c.get("count")
        sampled, in_poly = sample_county(bbox, rings, cnt if isinstance(cnt, int) else None)
        res.append({"fips": fips, "bbox_rows": cnt, "sampled": sampled, "in_poly": in_poly,
                    "ratio": round(in_poly / sampled, 3) if sampled else None})
        print(f"    {fips} bbox={cnt} sampled={sampled} in_poly={in_poly}")
        time.sleep(0.2)
    summ = {
        "counties_total": len(res),
        "with_data": len([r for r in res if (r.get("in_poly") or 0) > 0]),
        "empty": len([r for r in res if r.get("ratio") == 0]),
        "errors": len([r for r in res if r.get("error")]),
    }
    os.makedirs("counts_pip", exist_ok=True)
    json.dump({"tag": tag, "state": st, "summary": summ, "counties": res},
              open(f"counts_pip/{tag}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("  汇总:", json.dumps(summ, ensure_ascii=False))


if __name__ == "__main__":
    main()
