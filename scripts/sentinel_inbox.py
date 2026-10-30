#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""哨兵投信模块（在 runner 上被各巡检脚本调用）—— 把异常写进 GitHub Issue 信箱。

用户口径（2026-10-22 定案）：
  · 哨兵**不向用户推任何消息**，只把问题投进信箱（Issue，label=sentinel-alarm）
  · 本机收信 py 轮询 Issue → 按轻重缓急进队列 → 唤醒 pi
  · pi 处理完才由「哨兵企微机器人」推结果给用户

去重规则：`key` 唯一标识一个问题（如 `SC:missing_counties`）。
  已有 open issue 带同一 key → 追加评论（更新时间线），不重复开；
  否则新建。这样同一个未修好的问题不会每 15 分钟刷一条新 Issue。

等级 → 前缀 + 排序：
  red 🔴 错误（数据缺失/停摆/失败）
  yellow 🟡 警告（缺县/停滞/未封印）
  blue 🔵 提示（未开工/配额）
  green 🟢 发布（新 release 出现，需要 pi 检查质量）
"""
from __future__ import annotations

import hashlib

import json
import os
import subprocess

REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"
LABEL = "sentinel-alarm"
ICON = {"red": "🔴", "yellow": "🟡", "blue": "🔵", "green": "🟢"}
ORDER = {"red": 0, "yellow": 1, "blue": 2, "green": 3}


def _sh(args: list[str], timeout: int = 90) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def ensure_label() -> None:
    _sh(["gh", "label", "create", LABEL, "--repo", REPO, "--color", "d73a4a",
         "--description", "哨兵投信：等待 pi 处理", "--force"])


def open_issues() -> list[dict]:
    raw = _sh(["gh", "issue", "list", "--repo", REPO, "--label", LABEL,
               "--state", "open", "--limit", "100",
               "--json", "number,title,body,createdAt"])
    try:
        return json.loads(raw) or []
    except Exception:  # noqa: BLE001
        return []


def emit(level: str, key: str, title: str, body: str, *, quiet: bool = False) -> str:
    """投递一条。返回 'created' / 'commented' / 'skip'。"""
    if level not in ICON:
        level = "yellow"
    icon = ICON[level]
    head = f"{icon} [{key}]"
    body_full = (f"**等级**：{level}（{ORDER[level]}）\n"
                 f"**key**：`{key}`\n\n{body}\n\n"
                 f"> 由哨兵自动投递，pi 处理完请 comment 并 close。\n\n{tag}")

    # ★ 去重（2026-10-30）：同一条告警内容重复投递会让 issue 变评论垃圾场
    #   （#5 曾被刷上百条一模一样的「复检仍异常」）。做法：正文带内容指纹，
    #   已有 issue 的最后一条评论若指纹相同则跳过，内容变了才追加。
    sig = hashlib.sha256(f"{key}|{body}".encode("utf-8")).hexdigest()[:12]
    tag = f"<!-- sig:{sig} -->"
    cbody = f"{icon} 复检仍异常（{body[:600]}）\n\n{tag}"

    existing = None
    for it in open_issues():
        if f"[{key}]" in (it.get("title") or ""):
            existing = it
            break
    if existing:
        last = _sh(["gh", "issue", "view", str(existing["number"]), "--repo", REPO,
                    "--json", "comments", "-q", ".comments[-1].body"])
        if sig in (last or ""):
            print(f"[inbox] {key} 内容与上条评论完全相同 → 跳过（去重）")
            return "skip"
        _sh(["gh", "issue", "comment", str(existing["number"]), "--repo", REPO,
             "--body", cbody])
        return "commented"

    ensure_label()
    out = _sh(["gh", "issue", "create", "--repo", REPO, "--label", LABEL,
               "--title", f"{head} {title}"[:200], "--body", body_full])
    if not quiet:
        print(f"[inbox] {level} → {key} {('<'+out.strip()+'>') if out else '(fail)'}")
    return "created" if out else "skip"


def emit_release(state: str, tag: str, summary: str) -> str:
    """新 release 出现 → 🟢，唤醒 pi 去做质量检查。"""
    return emit("green", f"{state}:new_release",
                f"{state} 新发布 {tag}，请检查发布物质量",
                f"流水线刚封印 `{tag}`。\n\n{summary}\n\n"
                f"请检查：四源是否齐全、县片是否齐、账实是否一致。")


def close_for_key(key: str, note: str) -> int:
    """pi 处理完后调用：把该 key 的 open issue 全部 close。"""
    n = 0
    for it in open_issues():
        if f"[{key}]" in (it.get("title") or ""):
            _sh(["gh", "issue", "comment", str(it["number"]), "--repo", REPO,
                 "--body", note or "已处理"])
            _sh(["gh", "issue", "close", str(it["number"]), "--repo", REPO])
            n += 1
    return n
