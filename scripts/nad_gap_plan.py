#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""算「上游有数据、但我们 Release 没片」的县 —— **用县真实多边形，不用 bbox**。

2026-10-28 血案：本脚本首版用县 bbox 查上游，导致严重假阳性。
实证 Clayton County GA：bbox 查询 count=235，真实多边形查询 count=0
（235 条落在相邻的 DeKalb/Fulton 境内）。bbox 面积远大于不规则县界，
用 bbox 判「上游有数据」= 系统性地把邻县数据算成本县。

因此本版：
  · 县界取 TIGER 资产的 rings（Esri JSON，不是 GeoJSON coordinates）
  · 查询用 esriGeometryPolygon（真实县界）
  · 同时输出 bbox 对照值，用于证明两者差异（只做诊断，不参与判定）
  · 仍只出事实，不下「合格/不合格」

用法：
  python3 scripts/nad_gap_plan.py --state GA --out gap/GA.json
  python3 scripts/nad_gap_plan.py --state GA --sample 10
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"
NAD = ("https://services.arcgis.com/xOi1kZaI0eWDREZv/ArcGIS/rest/services/"
       "Address_Points_from_National_Address_Database_view/FeatureServer/0/query")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}


def sh(args, timeout=600):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _post(params, tries=3):
    body = urllib.parse.urlencode(params).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(NAD, data=body, headers={
                **UA, "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            c = d.get("count")
            if isinstance(c, int):
                return c
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2 * (i + 1))
    return None


def poly_count(rings):
    return _post({"geometry": json.dumps({"rings": rings, "spatialReference": {"wkid": 4326}}),
                  "geometryType": "esriGeometryPolygon", "inSR": "4326",
                  "spatialRel": "esriSpatialRelIntersects",
                  "returnCountOnly": "true", "f": "json"})


def bbox_count(rings, pad=0.002):
    pts = [p for r in rings for p in r]
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    bb = f"{min(xs)-pad:.6f},{min(ys)-pad:.6f},{max(xs)+pad:.6f},{max(ys)+pad:.6f}"
    return _post({"geometry": bb, "geometryType": "esriGeometryEnvelope", "inSR": "4326",
                  "spatialRel": "esriSpatialRelIntersects",
                  "returnCountOnly": "true", "f": "json"})


def release_tags():
    return [t for t in (sh(["gh", "release", "list", "-R", REPO, "--limit", "200",
                            "--json", "tagName", "-q", ".[].tagName"]) or "").split()
            if t.startswith("raw-US-")]


def have_fips(state):
    have = set()
    for tag in [t for t in release_tags() if t.startswith(f"raw-US-{state}")]:
        names = (sh(["gh", "release", "view", tag, "-R", REPO,
                     "--json", "assets", "-q", ".assets[].name"]) or "").split()
        for n in names:
            m = re.match(rf"NAD-US-{state}-(\d{{5}})-", n)
            if m:
                have.add(m.group(1))
    return sorted(have)


def county_rings(state, tmp):
    """TIGER 县界 rings（Esri JSON）。TX 这类拆 p1/p2 的州逐个 tag 试。"""
    for tag in [t for t in release_tags() if t.startswith(f"raw-US-{state}")]:
        sh(["gh", "release", "download", tag, "-R", REPO,
            "-p", f"TIGER-US-{state}-county-*.geojson", "-D", str(tmp)])
    out = {}
    for f in sorted(tmp.glob(f"TIGER-US-{state}-county-*.geojson")):
        fips = f.name.rsplit("-", 1)[-1].split(".")[0]
        try:
            g = json.load(open(f, encoding="utf-8"))["features"][0]["geometry"]
            rings = g.get("rings") or []
            if rings:
                out[fips.zfill(5)] = rings
        except Exception:  # noqa: BLE001
            pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--sample", type=int, default=0)
    a = ap.parse_args()
    st = a.state.upper()
    have = set(have_fips(st))
    with tempfile.TemporaryDirectory() as td:
        rings_map = county_rings(st, Path(td))
    fips_list = sorted(rings_map)
    if a.sample:
        fips_list = fips_list[: a.sample]
    need, hit, failed, false_pos = [], [], [], []
    for fips in fips_list:
        if fips in have:
            continue
        rings = rings_map[fips]
        pc = poly_count(rings)
        if pc is None:
            failed.append(fips); continue
        if pc > 0:
            hit.append({"fips": fips, "upstream_count": pc}); need.append(fips)
        else:
            bc = bbox_count(rings)
            if bc and bc > 0:
                false_pos.append({"fips": fips, "bbox_count": bc})
    out = {
        "state": st,
        "method": "polygon（TIGER rings）—— bbox 会系统性假阳性，勿用",
        "counties_total": len(rings_map),
        "have_fips": sorted(have), "have_n": len(have),
        "upstream_hit_detail": hit, "need_fips": need, "need_n": len(need),
        "query_failed": failed,
        "bbox_false_positive": false_pos,
        "bbox_false_positive_n": len(false_pos),
        "note": "只出事实：need = 上游有数据（真实县界）且我们无片的县。",
    }
    txt = json.dumps(out, ensure_ascii=False, indent=1)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(txt, encoding="utf-8")
    print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
