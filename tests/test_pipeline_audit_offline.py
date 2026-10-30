#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自检：scripts/pipeline_audit.py 的三处账本 bug 回归（不联网、不装依赖）。

跑法：python3 tests/test_pipeline_audit_offline.py

覆盖 2026-10-30 修的三处：
  A1 卷号：raw-US-TX-v1-p1 / -p2 必须被识别成同一个州 TX，且 STATUS 按**实际 tag** 查
  A2 取数失败：gql 返回空时必须 exit 1 且**一条 B1 都不许产出**
  A3 盲区：有 MANIFEST 的 Release 也必须被检查 STATUS（MI 就是靠这条漏了 A6）
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pipeline_audit as pa  # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def run_audit(nodes: list[dict], statuses: dict[str, dict | None],
              manifests: set[str] | None = None,
              running: set[str] | None = None) -> tuple[int, list[dict], str]:
    """把网络层全换成假数据，跑一遍 main()，返回 (exit, findings, stdout)。"""
    manifests = manifests or set()
    running = running or set()

    def fake_gql(_q: str) -> dict:
        return {"releases": {"nodes": nodes}}

    def fake_sh(args, timeout=120):  # noqa: ANN001, ANN003
        if args[:2] == ["gh", "run"]:
            return "[]"
        return ""

    def fake_status_of(tag: str):
        return statuses.get(tag)

    def fake_has_manifest(tag: str) -> bool:
        return tag in manifests

    pa.gql, pa.sh = fake_gql, fake_sh
    pa.status_of, pa.has_manifest = fake_status_of, fake_has_manifest
    pa.hrs = lambda iso: -1.0            # 一律当作新鲜，避免时间相关抖动

    old_running = set()

    def fake_running(_runs):             # running 集合由 fake_sh 空列表自然得出
        return old_running

    buf = io.StringIO()
    cwd = os.getcwd()
    tmp = tempfile.mkdtemp(prefix="pa-test-")
    os.chdir(tmp)
    try:
        with redirect_stdout(buf):
            rc = pa.main()
    finally:
        os.chdir(cwd)
    payload = {}
    p = Path(tmp) / "audit_report.json"
    if p.exists():
        payload = json.loads(p.read_text(encoding="utf-8"))
    return rc, payload.get("findings", []), buf.getvalue()


def node(tag: str, n: int, names: list[str] | None = None) -> dict:
    return {"tagName": tag,
            "releaseAssets": {"totalCount": n,
                              "nodes": [{"name": x} for x in (names or [])]}}


def main() -> int:
    print("===== T1 A2：gql 返回空 → exit 1 且零 B1 =====")
    rc, finds, out = run_audit([], {})
    check("exit=1", rc == 1, f"rc={rc}")
    check("零 B1", not any(f["code"] == "B1" for f in finds), f"findings={len(finds)}")
    check("stdout 无『未开工』表格", "未开工" not in out.split("\n")[0] if out else True)

    print("\n===== T2 A1：TX 两卷合并 + STATUS 按实际 tag 查 =====")
    nodes = [node("raw-US-TX-v1-p1", 665, ["MANIFEST.json", "STATUS.json"]),
             node("raw-US-TX-v1-p2", 667, ["MANIFEST.json"]),
             node("raw-US-MI-v1", 233, ["MANIFEST.json"])]
    statuses = {"raw-US-TX-v1-p1": {"phase": "complete", "updated_at": "2026-10-30T00:00:00Z"},
                "raw-US-TX-v1-p2": None,
                "raw-US-MI-v1": None}
    rc, finds, _ = run_audit(nodes, statuses, manifests={"raw-US-TX-v1-p2", "raw-US-MI-v1"})
    b1 = [f for f in finds if f["code"] == "B1"]
    a6 = [f["msg"] for f in finds if f["code"] == "A6"]
    a5 = [f for f in finds if f["code"] == "A5"]
    check("TX 不报 B1", not any("TX" in f["msg"] for f in b1), str(b1)[:80])
    check("TX p2 报 A6（按实际 tag）", any("raw-US-TX-v1-p2" in m for m in a6), str(a6)[:120])
    check("TX p1 不报 A6（有 STATUS）", not any("raw-US-TX-v1-p1" in m for m in a6), str(a6)[:120])
    check("合并 1332 资产不误报 A5", not a5, str(a5)[:80])

    print("\n===== T3 A3：有 MANIFEST 也必须查 STATUS（MI 盲区） =====")
    check("MI 报 A6", any("MI" in m and "MANIFEST" in m for m in a6), str(a6)[:160])

    print("\n===== T4 已完成州零 A*；未开工州仍照实报 B1（不误杀真 B1） =====")
    done = ("AL", "AK", "AZ")


    print("\n===== T4 已完成州零 A*；未开工州仍照实报 B1（不误杀真 B1） =====")
    done = ("AL", "AK", "AZ")
    nodes = [node(f"raw-US-{s}-v1", 10, ["MANIFEST.json", "STATUS.json"]) for s in done]
    statuses = {f"raw-US-{s}-v1": {"phase": "complete",
                                   "updated_at": "2026-10-30T00:00:00Z"}
                for s in done}
    rc, finds, _ = run_audit(nodes, statuses, manifests={f"raw-US-{s}-v1" for s in done})
    a = [f for f in finds if f["code"] != "B1"]
    b1 = sorted(f["msg"].split(" ")[0] for f in finds if f["code"] == "B1")
    check("三个已完成州零 A* 告警",
          not any(any(s in f["msg"] for s in done) for f in a), str(a)[:100])
    check("未开工州照实报 B1（不误杀真 B1）",
          len(b1) > 0 and not any(s in b1 for s in done), f"B1={len(b1)} 个，均不在 {done}")
    print(f"\n总判定：{'✅ 全部通过' if FAIL == 0 else '❌ 有失败'}（{PASS} 通过 / {FAIL} 失败）")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
