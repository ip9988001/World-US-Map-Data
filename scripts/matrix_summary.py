#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批次跑完后的汇总报表：逐州读 Release 里的 MANIFEST/STATUS，输出一张可复核的表。

为什么不让 matrix 各 job 互相通信：matrix 的 job 之间天然隔离、顺序不定、失败隔离；
唯一可靠的共享载体就是**每州的 Release**。所以汇总 = 回头读一遍各自的家底，
这样报表反映的是"真正上架了什么"，而不是"job 自己说成功了"。

判定口径：
  ✅ 已封印   STATUS.phase=complete 且 MANIFEST 可解析 且 三数咬合成立
  ⚠️ 未封印   有 MANIFEST 但 phase != complete（中途失败/未跑完）
  ⛔ 无数据   Release 不存在或没有 MANIFEST（job 挂了/还没跑）

用法：
  python scripts/matrix_summary.py --states-json '["AK","DE"]'
  python scripts/matrix_summary.py --states-json "$STATES" --step-summary
  GH_TOKEN=xxx python scripts/matrix_summary.py --tag-template verify-US-DE-stream-v1 --states-json '["DE"]'
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""


def _api(path: str, raw: bool = False, timeout: int = 60):
    url = path if path.startswith("http") else f"{API}{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/octet-stream" if raw else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "matrix-summary/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return json.loads(data) if not raw else data
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def release_by_tag(tag: str):
    if not REPO:
        sys.exit("缺 GITHUB_REPOSITORY 环境变量（形如 owner/repo）")
    return _api(f"/repos/{REPO}/releases/tags/{urllib.parse.quote(tag)}")


def asset_bytes(asset: dict):
    data = _api(asset["url"], raw=True)
    return json.loads(data.decode("utf-8", "replace")) if data else None


def summarize(state: str, tag: str) -> dict:
    rel = release_by_tag(tag)
    row = {"state": state, "tag": tag, "release": bool(rel), "assets": 0, "bytes": 0,
           "nad_rows": None, "nad_official": None, "coverage": None, "shards": None,
           "gz_bytes": 0, "gate": None, "phase": None, "bite": None, "note": ""}
    if not rel:
        row["note"] = "Release 不存在（job 未跑/未上架）"
        return row
    assets = rel.get("assets") or []
    row["assets"] = len(assets)
    row["bytes"] = sum(int(a.get("size") or 0) for a in assets)
    row["gz_bytes"] = sum(int(a.get("size") or 0) for a in assets
                          if a["name"].endswith((".gz", ".parquet", ".zip")))

    by_name = {a["name"]: a for a in assets}
    man = asset_bytes(by_name["MANIFEST.json"]) if "MANIFEST.json" in by_name else None
    status = asset_bytes(by_name["STATUS.json"]) if "STATUS.json" in by_name else None
    if status:
        row["phase"] = status.get("phase")

    if not man:
        row["note"] = "无 MANIFEST.json（未封印）"
        return row

    nad = (man.get("completeness") or {}).get("nad") or {}
    row["nad_rows"] = nad.get("local_rows")
    row["nad_official"] = nad.get("upstream_declared_rows")
    row["coverage"] = nad.get("coverage_pct")

    shards = man.get("shards") or []
    if shards:
        row["shards"] = len(shards)
        union = sum(int(s.get("rows") or 0) for s in shards if s.get("kind") == "county")
        over = sum(int(s.get("rows") or 0) for s in shards if s.get("kind") == "overflow")
        if row["nad_rows"] is not None:
            row["bite"] = (union + over == int(row["nad_rows"]))

    row["gate"] = bool(nad.get("evidence_1_full")) and row["bite"] is not False
    if row["phase"] != "complete":
        row["note"] = f"phase={row['phase']}（未封印）"
    elif row["bite"] is False:
        row["note"] = "三数不咬合"
    elif not row["gate"]:
        row["note"] = "NAD 覆盖未满"
    return row


def verdict(r: dict) -> str:
    if not r["release"]:
        return "⛔ 无数据"
    if r["phase"] != "complete":
        return "⚠️ 未封印"
    if r["bite"] is False or not r["gate"]:
        return "⚠️ 有缺口"
    return "✅ 已封印"


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n} B"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--states-json", required=True, help='JSON 数组，如 \'["AK","DE"]\'')
    ap.add_argument("--tag-template", default="raw-US-{state}-v1")
    ap.add_argument("--out", default="", help="Markdown 落盘路径（可选）")
    ap.add_argument("--step-summary", action="store_true", help="追加到 $GITHUB_STEP_SUMMARY")
    args = ap.parse_args()

    try:
        states = json.loads(args.states_json)
    except json.JSONDecodeError:
        sys.exit(f"--states-json 不是合法 JSON：{args.states_json[:80]}")
    if not isinstance(states, list) or not states:
        sys.exit("--states-json 必须是非空数组")

    rows = []
    for st in states:
        tag = args.tag_template.format(state=st)
        try:
            rows.append(summarize(st, tag))
        except urllib.error.URLError as exc:          # 网络抖动不拖垮整表
            rows.append({"state": st, "tag": tag, "release": False, "assets": 0, "bytes": 0,
                         "nad_rows": None, "nad_official": None, "coverage": None,
                         "shards": None, "gz_bytes": 0, "gate": None, "phase": None,
                         "bite": None, "note": f"取数失败：{type(exc).__name__}"})

    ok = sum(1 for r in rows if verdict(r).startswith("✅"))
    lines = [f"# 批次汇总（{ok}/{len(rows)} 州已封印）", "",
             "| 州 | 判定 | NAD 行数 | 官方 count | 覆盖 | 分片 | 产物 | 三数咬合 | tag |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        bite = "—" if r["bite"] is None else ("✅" if r["bite"] else "❌")
        lines.append("| {st} | {v} | {rows} | {off} | {cov} | {sh} | {sz} | {bite} | `{tag}` |".format(
            st=r["state"], v=verdict(r),
            rows=f"{r['nad_rows']:,}" if isinstance(r["nad_rows"], int) else "—",
            off=f"{r['nad_official']:,}" if isinstance(r["nad_official"], int) else "—",
            cov=f"{r['coverage']:.1f}%" if isinstance(r["coverage"], (int, float)) else "—",
            sh=r["shards"] if r["shards"] is not None else "—",
            sz=f"{fmt_bytes(r['gz_bytes'])} / {r['assets']} 件",
            bite=bite, tag=r["tag"]))
    problems = [r for r in rows if not verdict(r).startswith("✅")]
    if problems:
        lines += ["", "## 需要复核", ""]
        lines += [f"- **{r['state']}**：{r['note'] or verdict(r)}（`{r['tag']}`）" for r in problems]

    md = "\n".join(lines) + "\n"
    print(md)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(md)
    if args.step_summary:
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if path:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(md)
    return 0          # 有缺口也要绿灯：判定写在表里，别让汇总 job 自己变红掩盖问题


if __name__ == "__main__":
    sys.exit(main())
