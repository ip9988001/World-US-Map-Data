#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自检：批次计划（scripts/matrix_plan.py）+ 汇总表渲染口径。

只验证"计划"这一层的事实正确性，不联网：
  M1 tiny 档 == 12 个州，且是预期那 12 个
  M2 all == roster 全部州数，且 tiny 排在最前（小州先跑）
  M3 --states 手工覆盖：保序去重
  M4 未收录的州必须非零退出（绝不静默少跑）
  M5 --limit 截断
  M6 汇总判定口径：已封印 / 未封印 / 无数据 三种 verdict 与三数咬合

跑法：python3 tests/test_matrix_plan.py
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "scripts" / "matrix_plan.py"
SUM = ROOT / "scripts" / "matrix_summary.py"
RESULTS = []

TINY = ["AK", "DC", "DE", "HI", "ME", "MT", "ND", "NH", "RI", "SD", "VT", "WY"]


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'✅' if ok else '❌'} {name}{'' if ok else '  ← ' + str(detail)}")


def run(args):
    return subprocess.run([sys.executable, str(PLAN), *args],
                          capture_output=True, text=True, cwd=ROOT)


def plan_of(args):
    r = run(args)
    if r.returncode != 0:
        return None, r
    last = [ln for ln in r.stdout.strip().splitlines() if ln.startswith("[")]
    return json.loads(last[-1]), r


def main():
    print("M1 tiny 档 == 预期 12 州")
    tiny, r = plan_of(["--tier", "tiny"])
    check("M1 tiny 12 州且名单一致", tiny == TINY, tiny)

    print("M2 all == roster 全量，tiny 排在前面")
    allv, r2 = plan_of(["--tier", "all"])
    check("M2 all 覆盖全部州", allv is not None and len(allv) > len(TINY), allv and len(allv))
    check("M2 tiny 是 all 的前缀", allv and allv[:len(TINY)] == TINY, allv and allv[:len(TINY)])
    check("M2 无重复州", allv and len(set(allv)) == len(allv))

    print("M3 手工覆盖保序去重")
    ov, _ = plan_of(["--states", " WY,AK,WY,DE "])
    check("M3 覆盖保序去重", ov == ["WY", "AK", "DE"], ov)

    print("M4 未收录州必须报错")
    r = run(["--states", "AK,ZZ"])
    check("M4 未知州非零退出", r.returncode != 0, r.returncode)
    check("M4 报错信息点名", "ZZ" in (r.stdout + r.stderr), r.stderr[:120])

    print("M5 --limit 截断")
    lim, _ = plan_of(["--tier", "tiny", "--limit", "3"])
    check("M5 limit=3", lim == TINY[:3], lim)

    print("M6 汇总 verdict 口径（直接import，不联网）")
    sys.path.insert(0, str(SUM.parent))
    import matrix_summary as ms

    ok = {"release": True, "phase": "complete", "gate": True, "bite": True, "note": ""}
    half = {"release": True, "phase": "fetching", "gate": True, "bite": True, "note": ""}
    none = {"release": False, "phase": None, "gate": None, "bite": None, "note": ""}
    gap = {"release": True, "phase": "complete", "gate": True, "bite": False, "note": ""}
    check("M6 已封印", ms.verdict(ok).startswith("✅"), ms.verdict(ok))
    check("M6 未封印", ms.verdict(half).startswith("⚠️"), ms.verdict(half))
    check("M6 无数据", ms.verdict(none).startswith("⛔"), ms.verdict(none))
    check("M6 三数不咬合判缺口", ms.verdict(gap).startswith("⚠️"), ms.verdict(gap))

    bad = [x for x in RESULTS if not x[1]]
    print(f"\n总判定： {'✅ 全部通过' if not bad else '❌ ' + str(len(bad)) + ' 项失败'}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
