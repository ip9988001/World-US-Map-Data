#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TIGER warn-partial 归因：列出 warn 的具体县，逐个核对上游 addrfeat 是否存在。

判据只有一条：上游 URL 返回 200（有文件）→ **我们漏抓**；404/410（没有）→ **上游无**。
本脚本只做 HEAD 探测，不下载、不重抓。

⚠️ 必须在能访问 www2.census.gov 的网络里跑（GitHub Actions runner 可以；
某些机房 IP 会被整站 403，此时本脚本输出会全是 ERR，不能据此下结论）。

用法：
  GH_TOKEN=xxx python3 scripts/tiger_warn_probe.py --states AK GA ID \
      --out docs/TIGER-WARN-2026-10-30.md
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
ROSTER = Path("rules/US/state_roster.yaml")


def names_map():
    """fips→name，按 roster 的 `- key/fips/name` 三元组顺序解析。"""
    out = {}
    try:
        lines = ROSTER.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001
        return out
    i = 0
    while i < len(lines):
        if re.match(r"^\s+- key:", lines[i]):
            fips = name = None
            for j in range(i, min(i + 4, len(lines))):
                mf = re.match(r"^\s+fips:\s*'?(\d{5})'?\s*$", lines[j])
                mn = re.match(r"^\s+name:\s*(.+?)\s*$", lines[j])
                if mf:
                    fips = mf.group(1)
                if mn:
                    name = mn.group(1).strip().strip("'\"")
            if fips:
                out[fips] = name or ""
            i += 3
        else:
            i += 1
    return out


def get_missed(tag, tmp):
    r = subprocess.run(["gh", "release", "download", tag, "-R", REPO, "-p", "MANIFEST.json",
                        "-D", str(tmp), "--clobber"], capture_output=True, text=True)
    f = Path(tmp) / "MANIFEST.json"
    if not f.exists():
        raise SystemExit(f"取不到 {tag} 的 MANIFEST.json：{r.stderr[:200]}")
    d = json.loads(f.read_text(encoding="utf-8"))
    tiger = [s for s in d.get("sources") or [] if s.get("source") == "census_tiger"]
    if not tiger:
        return [], 0, 0
    t = tiger[0]
    missed = [m for m in (t.get("missed") or []) if m.get("url")]
    counties = t.get("counties") or []
    n_c = len(counties) if isinstance(counties, list) else int(counties or 0)
    return missed, n_c, len(missed)


def head(url):
    try:
        req = urllib.request.Request(url, method="HEAD", headers=UA)
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.headers.get("Content-Length") or "?"
    except urllib.error.HTTPError as e:  # noqa: F821
        return e.code, "-"
    except Exception as e:  # noqa: BLE001
        return f"ERR {str(e)[:40]}", "-"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", nargs="+", default=["AK", "GA", "ID"])
    ap.add_argument("--out", default="docs/TIGER-WARN-2026-10-30.md")
    args = ap.parse_args()
    nm = names_map()

    rows, todo = [], []
    with tempfile.TemporaryDirectory() as td:
        for st in args.states:
            tag = f"raw-US-{st}-v1"
            missed, n_c, n_m = get_missed(tag, Path(td) / st)
            for m in missed:
                fips = ""
                mm = re.search(r"tl_2024_(\d{5})_addrfeat", m["file"])
                if mm:
                    fips = mm.group(1)
                elif re.search(r"tl_2024_[0-9]{2}_", m["file"]):
                    fips = ""
                rec = {"state": st, "file": m["file"], "fips": fips,
                       "county": nm.get(fips, ""), "url": m["url"],
                       "reason": m.get("reason", "")}
                rows.append(rec)
                todo.append(rec["url"])

    with ThreadPoolExecutor(max_workers=8) as ex:
        res = dict(zip(todo, ex.map(head, todo)))
    for r in rows:
        code, size = res.get(r["url"], ("?", "-"))
        r["http"] = code
        r["size"] = size
        if code == 200:
            r["verdict"] = "漏抓（上游有文件）"
        elif code in (404, 410):
            r["verdict"] = "上游无（真缺）"
        elif code == 403:
            r["verdict"] = "本机被封（403），需在 runner 重跑"
        else:
            r["verdict"] = f"未知（{code}）"

    by_state = {}
    for r in rows:
        by_state.setdefault(r["state"], []).append(r)

    L = [f"# TIGER warn-partial 归因（2026-10-30）", "",
         f"- 生成时间（UTC）：{datetime.now(timezone.utc):%Y-%m-%d %H:%M}",
         "- 口径：MANIFEST `sources[census_tiger].missed[]` 即 warn 的具体县；"
         "逐条 HEAD 上游 `tl_2024_<fips>_addrfeat.zip`。",
         "- 判定：**HTTP 200 = 我们有漏抓**；**404/410 = 上游真无文件，不算缺陷**。",
         ""]
    for st, rs in sorted(by_state.items()):
        miss = sum(1 for r in rs if r["verdict"].startswith("漏抓"))
        nou = sum(1 for r in rs if r["verdict"].startswith("上游无"))
        unk = len(rs) - miss - nou
        L.append(f"## {st}")
        L.append("")
        L.append(f"- warn 县数：**{len(rs)}** → 漏抓 **{miss}** / 上游无 **{nou}** / 未判定 **{unk}**")
        L.append("")
        L.append("| FIPS | 县 | 上游 HTTP | 字节 | 结论 | 采集器记录 |")
        L.append("|---|---|---|---|---|---|")
        for r in sorted(rs, key=lambda x: x["fips"]):
            L.append(f"| {r['fips'] or '—'} | {r['county'] or ('（州级 place 文件，非县级）' if not r['fips'] else '?')} | {r['http']} | {r['size']} | "
                     f"{r['verdict']} | {(r['reason'] or '')[:40]} |")
        L.append("")
    L.append("## 说明")
    L.append("")
    L.append("采集器记录的失败原因是「上游返回非 zip（WAF 拒页 / 截断）」——"
             "这既可能是上游真无文件，也可能是采集时被 WAF 拦或连接截断。"
             "本表用 HEAD 探测的是**现在**上游有没有；200 但采集器当初失败 = 当时是瞬时故障 → **补抓即可**。")
    L.append("")
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    Path(args.out).write_text("\n".join(L), encoding="utf-8")
    json.dump(rows, open("/tmp/tiger_warn_rows.json", "w"), ensure_ascii=False, indent=1)
    for st, rs in sorted(by_state.items()):
        miss = sum(1 for r in rs if r["verdict"].startswith("漏抓"))
        nou = sum(1 for r in rs if r["verdict"].startswith("上游无"))
        print(f"{st}: warn={len(rs)} missed_us={miss} upstream_absent={nou} other={len(rs)-miss-nou}")
    print(f"写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
