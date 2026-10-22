#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NAD 逐县覆盖探针 —— 把「上游真无数据」和「我们查询漏了」分开。

背景（见 后期问题集锦/01-严重-数据正确性.md P1）：
  GA 有 159 个县，Release 里只有 2 个 NAD 县片，而 STATUS 判 `phase=complete`、
  `counties_failed=[]`。账本上一点痕迹都不留，下游会以为"GA 的 NAD 就这么多"。

做法：
  1. 从 Release 里取现成的 `TIGER-US-<ST>-county-<fips>.geojson`（已经下载好的县界）
  2. 每个县算 bbox（外扩 0.002 度防边界点漏）
  3. 对该 bbox 打 NAD `returnCountOnly`
  4. 统计：有数据县 / 无数据县 / 查询失败县

判定：
  · 绝大多数县 count>0 → 我们漏抓了，要修县 polygon 查询
  · 绝大多数县 count=0 → 上游真无覆盖，与 NV/NH/HI 同性质，应写 nad_no_coverage

用法：
  python3 scripts/nad_county_probe.py --states GA,FL,SC,MS,LA,SD --sample 30
  python3 scripts/nad_county_probe.py --states GA --all
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
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


def sh(args: list[str], timeout: int = 180) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def nad_count(bbox: list[float], tries: int = 3) -> int | None:
    body = urllib.parse.urlencode({
        "geometry": ",".join(f"{v:.6f}" for v in bbox),
        "geometryType": "esriGeometryEnvelope", "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "returnCountOnly": "true", "f": "json",
    }, quote_via=urllib.parse.quote, safe=",").encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(NAD, data=body, headers={
                **UA, "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=90) as r:
                doc = json.loads(r.read().decode("utf-8", "replace"))
            c = doc.get("count")
            if isinstance(c, int):
                return c
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2 * (i + 1))
    return None


def county_bboxes(state: str, tmp: Path, sample: int, run_all: bool) -> list[tuple[str, str, list[float]]]:
    tag = f"raw-US-{state}-v1"
    # ★ 先列名再逐个下：一次性 --pattern 下 159 个县界在 runner 上会超时（实测 0 县），
    #   而且我们本来就只需要抽样那几个。
    names = sh(["gh", "release", "view", tag, "--repo", REPO,
                "--json", "assets", "-q", ".assets[].name"]).split()
    files = sorted(n for n in names if n.startswith(f"TIGER-US-{state}-county-"))
    if not run_all and len(files) > sample:
        step = max(1, len(files) // sample)
        files = files[::step][:sample]
    for n in files:
        sh(["gh", "release", "download", tag, "-R", REPO, "-p", n,
            "-O", str(tmp / n), "--clobber"], timeout=120)
    out = []
    for f in sorted(tmp.glob(f"TIGER-US-{state}-county-*.geojson")):
        fips = f.stem.rsplit("-", 1)[-1]
        try:
            doc = json.loads(f.read_text(encoding="utf-8", errors="replace"))
            feats = doc.get("features") or []
            if not feats:
                continue
            xs, ys = [], []
            for ring in (feats[0].get("geometry") or {}).get("coordinates") or []:
                pts = ring if isinstance(ring[0][0], (list, tuple)) else [ring]
                for poly in pts:
                    for pt in poly:
                        xs.append(float(pt[0]))
                        ys.append(float(pt[1]))
            if not xs:
                continue
            out.append((fips, f.stem, [min(xs) - 0.002, min(ys) - 0.002,
                                      max(xs) + 0.002, max(ys) + 0.002]))
        except Exception:  # noqa: BLE001
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", default="GA")
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    rows = []
    for st in [s.strip().upper() for s in args.states.split(",") if s.strip()]:
        tmp = Path(tempfile.mkdtemp(prefix=f"cn-{st}-"))
        boxes = county_bboxes(st, tmp, args.sample, args.all)
        have = zero = fail = 0
        detail = []
        for fips, name, bb in boxes:
            c = nad_count(bb)
            if c is None:
                fail += 1
                detail.append(f"{fips}:ERR")
            elif c > 0:
                have += 1
                detail.append(f"{fips}:{c}")
            else:
                zero += 1
                detail.append(f"{fips}:0")
        total = len(boxes)
        rows.append((st, total, have, zero, fail))
        print(f"\n=== {st} 抽查 {total} 县：有数据 {have} / 无数据 {zero} / 查询失败 {fail} ===")
        print("  " + " ".join(detail[:40]))

    md = ["| 州 | 抽查县数 | 有数据 | 无数据 | 查询失败 | 判定 |", "|---|---|---|---|---|---|"]
    for st, t, h, z, f in rows:
        verdict = ("🔴 我们漏抓（需修县查询）" if h >= max(1, (t - f) * 0.3)
                   else "⬜ 上游真无覆盖" if f < t / 2 else "❓ 查询失败太多，结论不可信")
        md.append(f"| {st} | {t} | {h} | {z} | {f} | {verdict} |")
    body = "## NAD 逐县覆盖探针\n\n" + "\n".join(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(body + "\n")
    print("\n" + body)

    if any(h >= max(1, (t - f) * 0.3) for _, t, h, _, f in rows):
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from sentinel_inbox import emit
            emit("red", "nad:county_coverage",
                 "NAD 县覆盖异常：抽查显示上游有数据但 Release 缺片",
                 "\n".join(md))
        except Exception as e:  # noqa: BLE001
            print(f"[inbox] 投信失败：{e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
