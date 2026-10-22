#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""哨兵看门狗（在 GitHub Actions runner 上跑）—— 盯「哨兵自己不转了」。

问题：pipeline-audit / run-watch / content-audit / preflight-upstream 都在 schedule 上跑，
但它们**如果自己停摆，没有任何东西会说话** —— schedule 被 GitHub 悄悄禁用、
cron 表达式写错、脚本 import 崩了，表现都是「安静」，跟「一切正常」长得一模一样。
这个看门狗就是专门把「安静」和「正常」区分开。

判定（每个 workflow 一条）：查它最近一次 run。
  · 从未运行                       → 🔴
  · 最近一次距今超过该 workflow 的容忍窗口 → 🔴
  · 最近一次是 failure 且距今已超一轮     → 🟡（连续失败）
  · 全部在窗口内                   → 🟢

输出：有问题 → 开/更新 Issue（label=sentinel-alarm），正文列出每个 workflow 的最后运行
时间与结论。**不推送任何企微消息** —— 按用户口径，哨兵只负责把问题投进信箱（Issue），
由本机收信 py 唤醒 pi，pi 处理完再由二号 bot 推结果。

天然上限（必须知道）：GitHub Actions 整体挂掉时，本脚本也不会跑，谁也通知不了。
所以本机收信 py 必须兼任「外部观察者」：它检查最近一次 sentinel run 的时间戳，
超时就直接推你 —— 那一步是纯告警，不经过本脚本。
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone

REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"
LABEL = "sentinel-alarm"

# (workflow 文件, 容忍分钟数, 原定频率)
CHECKS = [
    ("run-watch.yml", 60, "每 15 分钟"),
    ("pipeline-audit.yml", 120, "每 30 分钟"),
    ("preflight-upstream.yml", 180, "每小时"),
    ("content-audit.yml", 300, "每 2 小时"),
]


def sh(args: list[str], timeout: int = 120) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def mins(iso: str) -> float:
    try:
        t = datetime.strptime(iso.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
        return (datetime.now(timezone.utc) - t).total_seconds() / 60
    except Exception:  # noqa: BLE001
        return 10 ** 9


def ensure_label() -> None:
    sh(["gh", "label", "create", LABEL, "--repo", REPO, "--color", "d73a4a",
        "--description", "哨兵/流水线异常，等待 pi 处理", "--force"])


def last_run(wf: str) -> dict | None:
    raw = sh(["gh", "run", "list", "--workflow", wf, "--repo", REPO, "--limit", "1",
              "--json", "databaseId,status,conclusion,updatedAt,htmlUrl"])
    try:
        rows = json.loads(raw)
        return rows[0] if rows else None
    except Exception:  # noqa: BLE001
        return None


def wf_age_days(wf: str) -> float:
    """workflow 自身建立多少天 —— 新建的给宽限期，否则第一次 schedule 还没到点就被判停摆。"""
    raw = sh(["gh", "api", f"repos/{REPO}/actions/workflows/{wf}"])
    try:
        created = json.loads(raw).get("created_at") or ""
        return mins(created) / 1440.0
    except Exception:  # noqa: BLE001
        return 99.0


def main() -> int:
    rows, bad = [], []
    for wf, limit, freq in CHECKS:
        age_days = wf_age_days(wf)
        if 0 <= age_days < 1:
            rows.append((wf, freq, f"🆕 建立 {age_days * 24:.1f}h", "🆕"))
            continue                            # 24h 宽限期
        r = last_run(wf)
        if not r:
            rows.append((wf, freq, "从未运行", "🔴"))
            bad.append(f"{wf} 从未运行（原定{freq}）")
            continue
        age = mins(str(r.get("updatedAt") or ""))
        con = r.get("conclusion") or r.get("status") or "?"
        flag = "🟢"
        if age > limit:
            flag = "🔴"
            bad.append(f"{wf} 已 {age / 60:.1f}h 没跑（原定{freq}，容忍 {limit} 分钟）")
        elif con == "failure":
            flag = "🟡"
            bad.append(f"{wf} 最近一次 failure（{age / 60:.1f}h 前）{r.get('htmlUrl', '')}")
        rows.append((wf, freq, f"{con} / {age / 60:.1f}h 前", flag))

    table = ["| workflow | 原定 | 最近一次 | 判定 |", "|---|---|---|---|"]
    table += [f"| `{w}` | {f} | {s} | {g} |" for w, f, s, g in rows]
    body = ("## 哨兵看门狗\n\n" + "\n".join(table) +
            ("\n\n**异常：**\n" + "\n".join(f"- {b}" for b in bad) if bad else "\n\n全部在窗口内 ✅"))

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(body + "\n")
    print(body)

    if bad:
        ensure_label()
        title = "🔴 哨兵停摆：部分巡检 workflow 未按时运行"
        exists = sh(["gh", "issue", "list", "--repo", REPO, "--label", LABEL,
                     "--state", "open", "--search", "哨兵停摆在", "--json", "number,title"])
        num = None
        try:
            for it in json.loads(exists) or []:
                if "哨兵停摆" in (it.get("title") or ""):
                    num = it.get("number")
                    break
        except Exception:  # noqa: BLE001
            num = None
        if num:
            sh(["gh", "issue", "comment", str(num), "--repo", REPO, "--body", body])
        else:
            sh(["gh", "issue", "create", "--repo", REPO, "--label", LABEL,
                "--title", title, "--body", body])
        print(f"::error::{len(bad)} 项哨兵停摆")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
