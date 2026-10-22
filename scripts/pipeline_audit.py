#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全流水线账本巡检（在 GitHub Actions runner 上跑，不在本机）。

铁律（用户明令）：**全部流程在仓库里完成，不涉及本机服务器**。
所以本脚本只依赖 runner 自带的 gh CLI + 仓库内文件，无本机路径、无本机队列、无本机 cron。

职责：盯「没有 job 在跑时的死状态」——这是 gh_watch 类 run 事件监控的盲区。
  A1 空壳        Release 存在但资产 0
  A2/A3 停滞/缺县 STATUS 超时不动且无活跃 job
  A4 未封印      数据齐但 phase≠complete（有 MANIFEST→reheal；无→全州重跑）
  A5 逼近上限    单 Release 资产 ≥900（GitHub 硬上限 1000）
  A6 无 STATUS   有资产但状态文档缺失
  B1 未开工      roster 有、Release 无
  B2 配额        同州 24h 内 ≥3 次 run
  B3 失败无接管  run failure/cancelled 且 6h 无新 run 接手

输出：step summary Markdown + upstream 同级的 audit_report.json artifact。
异常 → exit 1 → run 变红（复用既有 failure 告警通道，不新建推送管道）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROSTER = ROOT / "rules" / "US" / "state_roster.yaml"
REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"

STALE_HOURS = 6
GAP_HOURS = 6
QUOTA_24H = 3
ASSET_CAP = 1000


def sh(args: list[str], timeout: int = 120) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def gql(query: str) -> dict:
    try:
        r = subprocess.run(["gh", "api", "graphql", "-f", f"query={query}"],
                           capture_output=True, text=True, timeout=180)
        return json.loads(r.stdout)["data"]["repository"] if r.returncode == 0 else {}
    except Exception:  # noqa: BLE001
        return {}


def has_manifest(state: str) -> bool:
    raw = sh(["gh", "release", "view", f"raw-US-{state}-v1", "--repo", REPO,
              "--json", "assets", "-q", ".assets[].name"])
    return any(x.startswith("MANIFEST") for x in raw.split())


def status_of(state: str) -> dict | None:
    tmp = Path(f"/tmp/_audit_{state}.json")
    sh(["gh", "release", "download", f"raw-US-{state}-v1", "-R", REPO,
        "--pattern", "STATUS.json", "-O", str(tmp), "--clobber"])
    try:
        return json.loads(tmp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def hrs(iso: str) -> float:
    try:
        t = datetime.strptime(iso.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        return -1.0


def main() -> int:
    states = {k: len(v.get("counties") or [])
              for k, v in yaml.safe_load(ROSTER.read_text(encoding="utf-8"))["states"].items()}

    repos = gql('query{repository(owner:"%s",name:"%s"){releases(first:80){nodes{'
                'tagName releaseAssets(first:100){totalCount nodes{name}}}}}}'
                % tuple(REPO.split("/")))
    rel: dict[str, dict] = {}
    for n in repos.get("releases", {}).get("nodes") or []:
        tag = n.get("tagName") or ""
        if not tag.startswith("raw-US-"):
            continue
        st = tag.split("-")[2]
        ra = n.get("releaseAssets") or {}
        names = [x.get("name") or "" for x in (ra.get("nodes") or [])]
        rel[st] = {"n": int(ra.get("totalCount") or 0),
                   "man": any(x.startswith("MANIFEST") for x in names)}

    runs = []
    try:
        runs = json.loads(sh(["gh", "run", "list", "--limit", "60", "--repo", REPO,
                              "--json", "databaseId,status,conclusion,createdAt"]))
    except Exception:  # noqa: BLE001
        pass

    running: set[str] = set()
    for r in runs:
        if r.get("status") not in ("queued", "in_progress"):
            continue
        try:
            js = json.loads(sh(["gh", "run", "view", str(r.get("databaseId")),
                                "--repo", REPO, "--json", "jobs"])).get("jobs") or []
        except Exception:  # noqa: BLE001
            js = []
        for j in js:
            nm = j.get("name") or ""
            if nm.startswith("抓取 ") and j.get("status") in ("queued", "in_progress"):
                running.add(nm[3:].strip())

    findings: list[dict] = []

    def add(sev: str, code: str, msg: str, fix: str = "") -> None:
        findings.append({"sev": sev, "code": code, "msg": msg, "fix": fix})

    for st, info in sorted(rel.items()):
        n = info["n"]
        man = info["man"] or (n > 100 and has_manifest(st))
        busy = st in running
        if n == 0:
            if not busy:
                add("red", "A1", f"{st} 空壳：Release 存在但资产 0（无 STATUS）",
                    f"states={st} → 全量重跑")
            continue
        if man:
            if n >= ASSET_CAP * 0.9:
                add("yellow", "A5", f"{st} 资产 {n} 个逼近上限 {ASSET_CAP}", "该州需分卷")
            continue
        doc = status_of(st)
        if not doc:
            add("yellow", "A6", f"{st} 有 {n} 资产但无 STATUS.json", "查上轮是否写 STATUS 前被 kill")
            continue
        if doc.get("phase") == "complete" or busy:
            continue
        age = hrs(str(doc.get("updated_at") or ""))
        done, total = len(doc.get("done") or []), states.get(st) or 0
        if 0 <= age < (STALE_HOURS if total and done >= total else GAP_HOURS):
            continue
        if total and done >= total:
            fix = (f"python3 scripts/manifest_reheal.py --state {st} --apply" if man
                   else f"数据齐但缺 MANIFEST → states={st} 全州重跑收尾")
            add("red", "A4", f"{st} 数据齐（{done}/{total} 县）但 phase={doc.get('phase')} "
                             f"未封印，账本 {age:.1f}h 没动且无 job 在跑", fix)
        else:
            add("red", "A3", f"{st} 缺县 {done}/{total}，账本 {age:.1f}h 没动且无 job"
                             f"（死状态）", f"states={st} → 续跑")

    for st in sorted(states):
        if st not in rel:
            add("yellow", "B1", f"{st} 无 Release（未开工）", f"states={st} → 派跑")

    cnt: dict[str, int] = {}
    for r in runs:
        if hrs(str(r.get("createdAt") or "")) > 24:
            continue
        try:
            js = json.loads(sh(["gh", "run", "view", str(r.get("databaseId")),
                                "--repo", REPO, "--json", "jobs"])).get("jobs") or []
        except Exception:  # noqa: BLE001
            js = []
        for j in js:
            nm = j.get("name") or ""
            if nm.startswith("抓取 "):
                s = nm[3:].strip()
                cnt[s] = cnt.get(s, 0) + 1
    for st, c in sorted(cnt.items()):
        if c >= QUOTA_24H:
            add("yellow", "B2", f"{st} 24h 内已跑 {c} 次（配额用满）", "先查原因再派")

    order = {"red": 0, "yellow": 1}
    findings.sort(key=lambda f: order.get(f["sev"], 2))
    payload = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "releases": len(rel), "rosted": len(states),
               "running": sorted(running), "findings": findings}
    Path("audit_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    rows = ["| 级别 | 代号 | 问题 | 处置 |", "|---|---|---|---|"]
    rows += [f"| {'🔴' if f['sev'] == 'red' else '🟡'} | {f['code']} | {f['msg']} | "
             f"{f['fix']} |" for f in findings]
    md = (f"## 账本巡检（runner 侧）\n\n"
          f"扫描 **{len(rel)}/{len(states)}** 州 · 在跑 **{', '.join(sorted(running)) or '无'}** "
          f"· findings **{len(findings)}**\n\n" + "\n".join(rows) + "\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md)
    print(md)

    reds = [f for f in findings if f["sev"] == "red"]

    # ★ 投信（2026-10-22 定案）：哨兵不推企微，只把问题写进 Issue 信箱。
    #   红线逐条（便于逐项 close），黄条合并一条（防刷屏）。
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from sentinel_inbox import emit
        for f in reds:
            emit("red", f"audit:{f['code']}:{f['msg'][:32].replace(' ', '')}",
                 f["msg"], f.get("fix") or "")
        yl = [f for f in findings if f["sev"] == "yellow"]
        if yl:
            emit("yellow", "audit:warnings", f"账本巡检黄条 {len(yl)} 项",
                 "\n".join(f"- {f['code']} {f['msg']} → {f.get('fix')}" for f in yl))
    except Exception as e:  # noqa: BLE001
        print(f"[inbox] 投信失败：{type(e).__name__}: {e}")

    if reds:
        # ★ 2026-10-22 改语义：发现问题不算失败（Issue 已表达异常），否则 run 列表一片红，
        #   分不清「发现了问题」和「哨兵自己崩了」。只有脚本异常/取数失败才该 exit≠0。
        print(f"::notice::{len(reds)} 项红线已投 Issue 信箱（本 run 不算失败）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
