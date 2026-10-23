#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 NAD 本地覆盖矩阵写回各 Release 的 MANIFEST —— 只写事实，不粉饰。

输入：counts_local/<TAG>.json（来自 nad_local_matrix.py）
输出：更新 MANIFEST 的 audit.nad / completeness.nad 自述后回传 Release

判定口径：
  in_state_points == 0            → nad_instate_missing（本州点一个都没有，上游或抓取缺口）
  0 < in_state_pct < 80           → nad_instate_partial（大量点落在州界外）
  其余                            → nad_instate_ok
同时把原来那个会误导人的 coverage_pct 保留原值但加 superseded_note，说明它把溢出包当成了本地产出。
"""
from __future__ import annotations
import glob, json, os, subprocess, time

REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"


def sh(a, timeout=1800):
    r = subprocess.run(a, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def fetch(url, path, tries=3):
    for i in range(tries):
        rc, o, e = sh(["curl", "-fsSL", "--retry", "2", "--max-time", "600", "-o", path, url])
        if rc == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
            return True
        time.sleep(2 * (i + 1))
    print("  [dl-fail]", path, (e or "")[:100].replace("\n", " "))
    return False


ok = fail = 0
fails = []
rows = []
for f in sorted(glob.glob("counts_local/*.json")):
    c = json.load(open(f, encoding="utf-8"))
    tag, st = c["tag"], c["state"]
    p = f"/tmp/mf/{tag}.json"
    os.makedirs("/tmp/mf", exist_ok=True)
    if not fetch(f"https://github.com/{REPO}/releases/download/{tag}/MANIFEST.json", p):
        fail += 1; fails.append(tag); continue
    try:
        man = json.load(open(p, encoding="utf-8"))
    except Exception as e:
        print("[fail] MANIFEST 解析失败", tag, repr(e)[:80]); fail += 1; fails.append(tag); continue
    tot, ins = c["total_points"], c["in_state_points"]
    pct = c.get("in_state_pct") or 0.0
    verdict = ("nad_instate_missing" if ins == 0
               else ("nad_instate_partial" if pct < 80 else "nad_instate_ok"))
    audit = man.setdefault("audit", {})
    a = audit.setdefault("by_source", {}).setdefault("nad", {})
    a["local_points"] = tot
    a["in_state_points"] = ins
    a["in_state_pct"] = c.get("in_state_pct")
    a["counties_with_points"] = c["counties_with_points"]
    a["county_total"] = len(c.get("counties") or [])
    a["instate_verdict"] = verdict
    a["instate_note"] = ("按 TIGER 县界逐点判定（一仓本地实物，非上游声明）："
                         "总点数 / 落在本州县界内的点数")
    comp = (man.get("completeness") or {}).get("nad") or {}
    if comp.get("coverage_pct") is not None:
        comp["coverage_pct_superseded_note"] = (
            "该 coverage_pct 由「县片行数 + 溢出包行数」比对上游 state count 得出；"
            f"实测溢出包内落在本州县界内的点仅 {ins:,} / {tot:,}"
            f"（{c.get('in_state_pct')}%），故此值不可作为本州覆盖率解读")
    audit["nad_instate_checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out = f"/tmp/mf/out-{tag}.json"
    json.dump(man, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    r1, o1, e1 = 1, "", ""
    for attempt in range(5):
        r1, o1, e1 = sh(["gh", "release", "upload", tag, out, "--repo", REPO, "--clobber"])
        if r1 == 0:
            break
        time.sleep(5 * (attempt + 1))
    if r1 == 0:
        ok += 1
        rows.append((st, tot, ins, c.get("in_state_pct"), c["counties_with_points"], verdict))
        print(f"[ok] {tag} 总 {tot:,} 州内 {ins:,} ({c.get('in_state_pct')}%) "
              f"有点县 {c['counties_with_points']} → {verdict}")
    else:
        fail += 1; fails.append(tag)
        print("[fail]", tag, (e1 or o1 or "")[:200].replace("\n", " "))
print("完成 ok=%d fail=%d" % (ok, fail))
if fails:
    print("失败:", " ".join(fails))
print("\n| 州 | NAD 总点 | 州内点 | 州内% | 有点的县 | 判定 |")
print("|---|---|---|---|---|---|")
for r in sorted(rows):
    print(f"| {r[0]} | {r[1]:,} | {r[2]:,} | {r[3]} | {r[4]} | {r[5]} |")
