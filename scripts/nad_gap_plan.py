#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""算出「上游有数据、但我们 Release 里没有县片」的县 fips —— 只出事实，不下判定。

与 nad_county_probe.py 的区别：
  · 探针回答「上游这个县有没有数据」
  · 本脚本回答「上游有、我们还缺哪个」= 上游有数据的县 − 我们已有的县片

已有县片从 Release 资产名提取（NAD-US-<ST>-<5位fips>-<name>.ndjson.gz），
上游有无直接复用 nad_county_probe 的 county_bboxes() / nad_count()，不复制代码。

用法：
  python3 scripts/nad_gap_plan.py --state GA --out gap/GA.json
  python3 scripts/nad_gap_plan.py --state GA --sample 20        # 抽样自检
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from nad_county_probe import REPO, county_bboxes, nad_count  # noqa: E402


def release_tags() -> list[str]:
    r = subprocess.run(["gh", "release", "list", "-R", REPO, "--limit", "200",
                        "--json", "tagName", "-q", ".[].tagName"],
                       capture_output=True, text=True)
    return [t for t in (r.stdout or "").split() if t.startswith("raw-US-")]


def have_fips(state: str) -> list[str]:
    """该州 Release 里已经存在的 NAD 县片 fips（兼容 TX 这类拆 p1/p2 的州）。"""
    tags = [t for t in release_tags() if t.startswith(f"raw-US-{state}")]
    have: set[str] = set()
    for tag in tags:
        r = subprocess.run(["gh", "release", "view", tag, "-R", REPO,
                            "--json", "assets", "-q", ".assets[].name"],
                           capture_output=True, text=True)
        for n in (r.stdout or "").split():
            m = re.match(rf"NAD-US-{state}-(\d{{5}})-", n)
            if m:
                have.add(m.group(1))
    return sorted(have)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--sample", type=int, default=0, help="只抽 N 个县（自检用）")
    ap.add_argument("--all", action="store_true", default=True)
    a = ap.parse_args()
    st = a.state.upper()

    have = set(have_fips(st))
    with tempfile.TemporaryDirectory() as td:
        boxes = county_bboxes(st, Path(td), a.sample, bool(a.all))
    need, hit, failed, already = [], [], [], []
    for item in boxes:
        fips = str(item[0]).zfill(5)
        bb = item[2]
        if fips in have:
            already.append(fips)
            continue
        c = nad_count(bb)
        if c is None:
            failed.append(fips)
        elif c > 0:
            hit.append({"fips": fips, "upstream_count": c})
            need.append(fips)
    out = {
        "state": st,
        "counties_total": len(boxes),
        "have_fips": sorted(have),
        "have_n": len(have),
        "upstream_hit_fips": [h["fips"] for h in hit],
        "upstream_hit_detail": hit,
        "need_fips": need,
        "need_n": len(need),
        "query_failed": failed,
        "note": "只出事实：need = 上游有数据且我们无片的县。是否补采由大脑决定。",
    }
    txt = json.dumps(out, ensure_ascii=False, indent=1)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(txt, encoding="utf-8")
    print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
