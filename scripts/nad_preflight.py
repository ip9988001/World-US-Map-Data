#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""51 州 NAD 覆盖排期预检：每州打一次 `returnCountOnly`，产出"哪些州有 NAD 覆盖"的基线表。

为什么每州只打一次（而不是逐县）：
  · 州级 51 次请求 ≈ 1 分钟，成本可忽略；
  · 逐县要 3000+ 次，之前实测会触发上游限流/超时，得到失真数字；
  · 真正的县级兜底由抓取器在"州级=0"时自动触发（bots/fetch_state_raw.py 双层预检）。

输出：
  · 终端 Markdown 表 + `--json-out` 机器可读
  · `--step-summary` 写进 GitHub Actions 运行页
退出码：永远 0（排期是导航仪，不该因为个别州查询失败把 CI 弄红）；失败州标 `error` 并计入表。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROSTER = ROOT / "rules" / "US" / "state_roster.yaml"
NAD = ("https://services.arcgis.com/xOi1kZaI0eWDREZv/ArcGIS/rest/services/"
       "Address_Points_from_National_Address_Database_view/FeatureServer/0/query")
UA = {"User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/124.0.0.0 Safari/537.36")}


def post(params: dict, timeout: int = 90) -> dict:
    body = urllib.parse.urlencode(params, doseq=True).encode()
    req = urllib.request.Request(NAD, data=body,
                                headers={**UA, "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def probe_state(bbox: list, tries: int = 3) -> tuple[str, int | None, str]:
    """返回 (status, count, note)。status ∈ has_coverage / zero_coverage / error。"""
    geom = ",".join(str(v) for v in bbox)
    delay = 2.0
    last = ""
    for i in range(1, tries + 1):
        try:
            p = post({"where": "1=1", "geometry": geom, "geometryType": "esriGeometryEnvelope",
                      "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
                      "returnCountOnly": "true", "f": "json"})
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {str(exc)[:120]}"
            p = {}
        if p and not p.get("error") and isinstance(p.get("count"), int):
            n = p["count"]
            return ("has_coverage" if n > 0 else "zero_coverage"), n, f"try{i}"
        last = str(p.get("error") or last)[:160]
        if i < tries:
            time.sleep(delay)
            delay = min(delay * 2, 20)
    return "error", None, last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="all", help="all/tiny/small/medium/large")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--step-summary", action="store_true")
    args = ap.parse_args()

    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        sys.exit("需要 pyyaml")
    states = (yaml.safe_load(ROSTER.read_text(encoding="utf-8")) or {}).get("states") or {}
    order = ("tiny", "small", "medium", "large")
    picked = [c for c, v in states.items()
              if args.tier == "all" or (v.get("size_tier") or "").lower() == args.tier]
    picked.sort(key=lambda c: (order.index((states[c].get("size_tier") or "large").lower())
                               if (states[c].get("size_tier") or "").lower() in order else 9, c))

    rows = []
    t0 = time.time()
    for code in picked:
        st, n, note = probe_state(states[code]["bbox"])
        rows.append({"state": code, "tier": states[code].get("size_tier"), "status": st,
                     "count": n, "note": note})
        mark = {"has_coverage": "✅", "zero_coverage": "○", "error": "⚠️"}[st]
        print(f"  {mark} {code:<3} {str(states[code].get('size_tier')):<7} "
              f"{(f'{n:,}' if isinstance(n, int) else '—'):>12}  {note}", flush=True)

    has = [r for r in rows if r["status"] == "has_coverage"]
    zero = [r for r in rows if r["status"] == "zero_coverage"]
    err = [r for r in rows if r["status"] == "error"]
    total = sum(r["count"] or 0 for r in rows)

    lines = [f"# NAD 覆盖预检（{len(rows)} 州，{time.time() - t0:.0f}s）", "",
             f"- 有覆盖：**{len(has)}** 州，声明行数合计 **{total:,}**",
             f"- 零覆盖（走 no_upstream_coverage）：**{len(zero)}** 州 —— "
             f"{', '.join(r['state'] for r in zero) or '无'}",
             f"- 查询失败（重试后仍失败，需复查）：**{len(err)}** 州 —— "
             f"{', '.join(r['state'] for r in err) or '无'}", "",
             "| 州 | 档 | 状态 | 官方 count |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['state']} | {r['tier']} | "
                     f"{ {'has_coverage': '有覆盖', 'zero_coverage': '零覆盖', 'error': '失败'}[r['status']] } | "
                     f"{(format(r['count'], ',') if isinstance(r['count'], int) else '—')} |")
    md = "\n".join(lines) + "\n"
    print("\n" + md)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "tier": args.tier, "rows": rows, "total_declared_rows": total},
            ensure_ascii=False, indent=1), encoding="utf-8")
    if args.step_summary and os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
