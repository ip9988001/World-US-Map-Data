#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一仓内容体检（在 GitHub Actions runner 上跑）—— 不信 run 的 success，只对账实物

为什么需要它：
  2026-10-20 查出 OA 源被静默截断（MN 上游 66 个源只发了 60 个），
  而那次 run 是 **success**、gate 通过、哨兵一条告警都没收到。
  埋点永远会漏，所以再加一层「事后对账」。

检查四项（逐州）：
  ① OA：Release 里 OA-*.zip 数 vs OpenAddresses 上游该州源数
  ② NAD：实物县片数 vs MANIFEST.shards 的 county 数 vs reference_check.counties_ok
  ③ 四源：NAD / OVERTURE / TIGER 资产是否都在
  ④ 账实：MANIFEST.assets 数 vs Release 实物数（允许差 3 = 三本账本文件）

铁律：全部流程在仓库里完成，不涉及本机服务器。本脚本从本机 sentinel/audit.py 迁来，
去掉本机 outbox 路径，结论写 step summary；发现缺件则 exit 1 → run 变红。

用法（Actions 里）：
  python3 scripts/content_audit.py            # 全部 raw-US-*-v1
  python3 scripts/content_audit.py --state MN # 单州
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import subprocess
import os
import tempfile
import urllib.request
from pathlib import Path

REPO = "ip9988001/World-US-Map-Data"


def gh(*args: str, timeout: int = 180) -> str:
    r = subprocess.run(["gh", *args, "-R", REPO], capture_output=True, text=True, timeout=timeout)
    return r.stdout


def release_tags() -> list[str]:
    out = gh("release", "list", "--limit", "200", "--json", "tagName", "-q", ".[].tagName")
    return sorted(t for t in out.split() if re.match(r"^raw-US-[A-Z]{2}-v1$", t))


def physical(tag: str) -> list[str]:
    out = gh("release", "view", tag, "--json", "assets", "-q", ".assets[].name")
    return [n.strip() for n in out.splitlines() if n.strip()]


def oa_upstream() -> dict[str, int]:
    """OpenAddresses 首页 → 每个州的源数（上游声明）。"""
    try:
        req = urllib.request.Request("https://results.openaddresses.io/",
                                     headers={"User-Agent": "Mozilla/5.0"})
        home = urllib.request.urlopen(req, timeout=45).read().decode("utf-8", "replace")
        found = re.findall(
            r"https://data\.openaddresses\.io/runs/(\d+)/us/([a-z]{2})/([a-z0-9_]+)\.zip", home)
        # ★ 必须按 (州, 源名) 去重：上游页面同一源可能重复出现（OR 的 washington 就出现 2 次），
        #   按州裸计数会把重复条目算成「上游多 1 个源」，导致误报「OA 缺 1 个源」。
        return dict(collections.Counter(st for _r, st, _n in set(found)))
    except Exception as exc:  # noqa: BLE001
        print(f"[audit] ⚠️ 上游 OA 源数获取失败（跳过 OA 检查）：{exc}")
        return {}


def audit_one(tag: str, tmp: Path, oa_up: dict[str, int]) -> list[str]:
    st = tag[7:9]
    d = tmp / st
    d.mkdir(parents=True, exist_ok=True)
    subprocess.run(["gh", "release", "download", tag, "-R", REPO, "-p", "MANIFEST.json",
                    "-D", str(d), "--clobber"], capture_output=True, timeout=180)
    mp = d / "MANIFEST.json"
    if not mp.exists():
        return [f"{st} 未封印（无 MANIFEST，半成品/空壳）"]
    try:
        m = json.loads(mp.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [f"{st} MANIFEST 无法解析：{exc}"]

    names = physical(tag)
    probs: list[str] = []
    nad_names = [n for n in names if n.startswith("NAD-") and n.endswith(".ndjson.gz")]
    nad_n = len([n for n in nad_names if "OUTOFSTATE" not in n and "OVERFLOW" not in n])
    src = {s.get("source"): s for s in (m.get("sources") or []) if isinstance(s, dict)}
    ref = m.get("reference_check") or {}
    ok, empty = ref.get("counties_ok"), ref.get("counties_empty")

    # ① OA 全量（空包是合法上游，不计缺失）
    oa_n = sum(1 for n in names if n.startswith("OA-"))
    oa_empty = int((src.get("openaddresses") or {}).get("empty_sources")
                   or (src.get("openaddresses") or {}).get("empty_upstream") or 0)
    up = oa_up.get(st.lower(), 0)
    if up and oa_n + oa_empty < up:
        probs.append(f"{st} OA 缺 {up - oa_n - oa_empty} 个源"
                     f"（上游 {up} / 已发 {oa_n} / 空包 {oa_empty}）")

    # ② NAD 三数一致（HI 这类 counties_ok=0 且 empty>0 是合法状态）
    sh = [s for s in (m.get("shards") or []) if s.get("kind") == "county"]
    if isinstance(ok, int) and len(sh) != ok:
        probs.append(f"{st} shards {len(sh)} != counties_ok {ok}")
    if nad_n and len(sh) and nad_n != len(sh):
        probs.append(f"{st} 实物县片 {nad_n} != 账 shards {len(sh)}")
    if not nad_n and not sh and not (ok == 0 and (empty or 0) > 0) \
            and (src.get("nad") or {}).get("status") not in ("no_upstream_coverage",
                                                            "empty_upstream"):
        probs.append(f"{st} 没有任何 NAD 县片（且非「全县空」合法状态）")

    # ③ 四源齐
    if not any(n.startswith("OVERTURE-") for n in names):
        probs.append(f"{st} 缺 Overture 资产")
    if not any(n.startswith("TIGER-") for n in names):
        probs.append(f"{st} 缺 TIGER 资产")

    # ④ 账实一致（允许差 3：MANIFEST / STATUS / RELEASE_NOTES）
    acc = len(m.get("assets") or [])
    if acc and abs(acc - (len(names) - 3)) > 3:
        probs.append(f"{st} 账 assets {acc} vs 实物 {len(names) - 3}")
    return probs


def main() -> int:
    ap = argparse.ArgumentParser(description="一仓内容体检（哨兵用）")
    ap.add_argument("--state", default="")
    args = ap.parse_args()

    oa_up = oa_upstream()
    tags = [t for t in release_tags() if not args.state or t[7:9] == args.state.upper()]
    if not tags:
        print("[audit] 没有匹配的 release")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="audit-"))
    probs: list[str] = []
    for t in tags:
        try:
            probs += audit_one(t, tmp, oa_up)
        except Exception as exc:  # noqa: BLE001
            probs.append(f"{t[7:9]} 体检异常：{type(exc).__name__}: {str(exc)[:80]}")

    if not probs:
        msg = f"🔵 一仓内容体检通过：{len(tags)} 个 release 四源齐、账实一致、OA 源无缺"
    else:
        color = "🔴" if any("缺 " in p for p in probs) else "🟡"
        msg = (f"{color} 一仓内容体检：{len(tags)} 个 release 中发现 {len(probs)} 处问题\n"
               + "\n".join(f"· {p}" for p in probs[:25]))
    print(msg)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"## 内容体检（runner 侧）\n\n```\n{msg}\n```\n")
    if probs:
        # ★ 投信（2026-10-22 定案）：异常交给 Issue，不推企微、不 red 化 run
        try:
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).resolve().parent))
            from sentinel_inbox import emit
            emit("red", "content:audit", f"内容体检 {len(probs)} 处问题",
                 "\n".join(f"- {x}" for x in probs[:25]))
        except Exception as e:  # noqa: BLE001
            print(f"[inbox] 投信失败：{type(e).__name__}: {e}")
        print(f"::notice::内容体检发现 {len(probs)} 处问题（已投信，本 run 不算失败）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
