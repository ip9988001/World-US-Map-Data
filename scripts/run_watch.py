#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行态巡检（在 GitHub Actions runner 上跑，不在本机）。

铁律：**全部流程在仓库里完成**。本脚本从本机 sentinel/gh_watch.py 迁来，
去掉本机路径/本机队列/本机 hook，只依赖 runner 的 gh CLI + GITHUB_TOKEN。

补 pipeline_audit 的缺口：那边管「停下来的」，这边管「正在动但动坏了」。
  R1 卡死    in_progress job 的 Release 账本 >2h 不动（且 job 已跑 >1h）
  R2 临限    in_progress job 已跑 >5h（timeout 是 6h / 本仓设 350min）
  R3 失败无接管  failure/cancelled run 且涉及州既未封印也无新 run 接手

判活原则（2026-10-21 教训）：**只看账本进度，不看 run.updatedAt** ——
GitHub 对长 step 不刷新 updatedAt，AZ 那轮因此误报 3 次。
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"
ROOT = Path(__file__).resolve().parents[1]
ROSTER = ROOT / "rules" / "US" / "state_roster.yaml"

STUCK_LEDGER_H = 2.0        # 账本超 2h 不动
MIN_JOB_H = 1.0             # job 至少跑了 1h 才可能判卡（避免刚起步误杀）
NEAR_LIMIT_H = 5.0          # 逼近 350 分钟上限预警（350min ≈ 5.83h）


def sh(args: list[str], timeout: int = 120) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def hrs(iso: str) -> float:
    try:
        t = datetime.strptime(iso.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        return -1.0


def ledger_age(state: str) -> float:
    """Release STATUS.json 的 updated_at 距今小时数；取不到返回 -1。"""
    tmp = Path(f"/tmp/_rw_{state}.json")
    sh(["gh", "release", "download", f"raw-US-{state}-v1", "-R", REPO,
        "--pattern", "STATUS.json", "-O", str(tmp), "--clobber"])
    try:
        d = json.loads(tmp.read_text(encoding="utf-8"))
        return hrs(str(d.get("updated_at") or ""))
    except Exception:  # noqa: BLE001
        return -1.0


def sealed(state: str) -> bool:
    raw = sh(["gh", "release", "view", f"raw-US-{state}-v1", "--repo", REPO,
              "--json", "assets", "-q", ".assets[].name"])
    return any(x.startswith("MANIFEST") for x in raw.split())


def main() -> int:
    import yaml
    total_states = len(yaml.safe_load(ROSTER.read_text(encoding="utf-8"))["states"])
    runs = json.loads(sh(["gh", "run", "list", "--limit", "30", "--repo", REPO,
                          "--json", "databaseId,status,conclusion,createdAt"])) or []
    find: list[str] = []

    for r in runs:
        st, con = r.get("status"), r.get("conclusion")
        rid = r.get("databaseId")
        if st not in ("queued", "in_progress") and con not in ("failure", "cancelled"):
            continue
        try:
            js = json.loads(sh(["gh", "run", "view", str(rid), "--repo", REPO,
                                "--json", "jobs"])).get("jobs") or []
        except Exception:  # noqa: BLE001
            js = []
        for j in js:
            nm = (j.get("name") or "")
            if not nm.startswith("抓取 "):
                continue
            s = nm[3:].strip()
            jst = j.get("status")
            started = hrs(str(j.get("startedAt") or j.get("started_at") or ""))
            if jst in ("queued", "in_progress") and started >= 0:
                if started > NEAR_LIMIT_H:
                    find.append(f"R2 {s} job 已跑 {started:.1f}h（350 分钟上限在即）")
                elif started > MIN_JOB_H:
                    age = ledger_age(s)
                    if age < 0:
                        find.append(f"R1 {s} job 已跑 {started:.1f}h 但无 STATUS 账本可查")
                    elif age > STUCK_LEDGER_H:
                        find.append(f"R1 {s} job 已跑 {started:.1f}h，账本 {age:.1f}h 没动"
                                    f"（疑似卡死）")
            elif jst == "completed" and j.get("conclusion") in ("failure", "cancelled"):
                if hrs(str(r.get("createdAt") or "")) < 24 and not sealed(s):
                    others = [x for x in js if (x.get("name") or "") == nm
                              and x.get("status") in ("queued", "in_progress")]
                    if not others:
                        find.append(f"R3 {s} job {j.get('conclusion')}（run {rid}）"
                                    f"且未封印、无新 job 接手")

    md = ["## 运行态巡检（runner 侧）", ""]
    md += [f"- {x}" for x in find] or ["✅ 无异常"]
    md.append("")
    md.append(f"> 判活只看账本进度，不看 run.updatedAt（长 step 不刷新，AZ 轮误报 3 次的根因）。"
              f"州总数 {total_states}")
    text = "\n".join(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)
    if find:
        # ★ 投信：运行态异常全部归为 red（卡死/临限/失败无接管都需立即处理）
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from sentinel_inbox import emit
            for i, msg in enumerate(find):
                emit("red", f"run:{msg[:28].replace(' ', '')}", msg,
                     "看 run 日志定位；必要时 cancel 后用 ops.yml 重新派跑")
        except Exception as e:  # noqa: BLE001
            print(f"[inbox] 投信失败：{type(e).__name__}: {e}")
        print(f"::error::{len(find)} 项运行态异常")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
