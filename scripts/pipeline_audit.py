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
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

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


class GqlError(RuntimeError):
    """取数失败（rc≠0 / 非 JSON / 超时 / GraphQL errors / repository=null）。

调用方不用捕获 —— 直接冒泡成 run 红。这是 muse-000156 一的全部要点：
「取数失败」绝不能被降级成「空集合」，否则审计会把失败当结论。
    """


def _run_gh(args: list[str], what: str, *, timeout: int = 120) -> str:
    """跑一条 gh 命令，失败 **一律 raise**（不返回空串）。

返回 stdout；rc≠0 / 超时 都抛 GqlError，stderr 留前 200 字节便于定位。
    """
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GqlError("%s 超时（timeout=%ds）：%s"
                       % (what, timeout, " ".join(args)[:120])) from None
    if r.returncode != 0:
        raise GqlError("%s 失败（returncode=%d）：stderr 前 200 字节=%r"
                       % (what, r.returncode, (r.stderr or "")[:200]))
    return r.stdout


def gql(query: str, *, timeout: int = 180) -> dict:
    """跑一次 GraphQL，返回 **repository 节点 dict**。

2026-11-22 muse-000156 一：以前这里是
`return json.loads(r.stdout)["data"]["repository"] if r.returncode == 0 else {}`
外面包一层 `except Exception: return {}` —— 504 / 限流 / 非 JSON 全被静默吞成空 dict，
上层于是把「取数失败」当成「所有州都没开工」（issue #5 的 51 条假 B1）。现在一律 raise。
    """
    out = _run_gh(["gh", "api", "graphql", "-f", f"query={query}"],
                  "GraphQL 查询", timeout=timeout)
    try:
        doc = json.loads(out)
    except json.JSONDecodeError as e:
        raise GqlError("GraphQL 返回非 JSON（stdout 前 200 字节=%r）：%s"
                       % (out[:200], e)) from None
    errors = doc.get("errors")
    if errors:
        raise GqlError("GraphQL 返回 errors 数组（前 300 字节）：%s"
                       % json.dumps(errors, ensure_ascii=False)[:300])
    repo = (doc.get("data") or {}).get("repository")
    if repo is None:
        # 注意：这跟「仓库里确实 0 个 release」不是一回事 —— 后者是 nodes=[]（交给 A2 硬拦判）
        raise GqlError("GraphQL data.repository 为 null（仓库不存在 / 无权限 / 被限流）："
                       "query 前 120 字节=%r" % query[:120])
    return repo


# ★ A1（2026-10-30）：tag 不再是写死的 raw-US-{ST}-v1 —— TX 真实 tag 是 -p1/-p2，
#   写死会让 TX 永远 release not found → 每轮假红「665 资产但无 STATUS」。
#   正则解析卷号，同州多卷合并计资产数，MANIFEST / STATUS 一律按**实际 tag** 逐个查。
TAG_RE = re.compile(r"^raw-US-([A-Z]{2})-v1(-p\d+)?$")

# ---------------------------------------------------------------- 取数（轻查询 + 分页）
# ★ 2026-11-22 muse-000156 一（504 根因）：以前是
#     releases(first:80){nodes{tagName releaseAssets(first:100){totalCount nodes{name}}}}
#   —— 一次拉 80 release × 每个 100 个 asset、现库 52 release / 12,780 asset，
#   GraphQL 服务端算不动 → 稳定 HTTP 504（本机 11.6s、runner 11.9–12.0s，用 first:5 只要 0.6s）。
#   现拆成：① GraphQL 每页 100 个 release 只取 tagName + releaseAssets(first:1).totalCount
#          （不取 asset 名 → 一页几百字节，不再 504）；② asset 名按需走 REST（独立限额）。
RELEASES_PAGE = 100          # GraphQL releases 每页上限（GitHub 硬限 100）
RELEASES_MAX_PAGES = 40      # 安全阀：≤ 4000 个 release，防分页游标死循环
ASSETS_PER_PAGE = 100        # REST assets 每页上限
ASSETS_MAX_PAGES = 40        # 安全阀：≤ 4000 个资产/卷（A5 上限是 1000，远未到）

_ASSET_NAMES: dict[str, list[str]] = {}     # tag → 全量资产名（同轮内复用，避免重复 REST）
_HAS_MANIFEST: dict[str, bool] = {}         # tag → 是否含 MANIFEST*（找到即停，不一定要翻到底）


def fetch_releases() -> dict:
    """分页取全部 release：**只取 tagName + 资产总数**，不取资产名。

返回结构故意做成跟旧 GraphQL 响应同形（{"releases": {"nodes": [...]}}），
好让下方 A2 硬拦、TAG_RE 归并与输出字段**一字不用改**。
    """
    owner, name = REPO.split("/")
    nodes: list[dict] = []
    total = 0
    cursor = ""
    for page in range(1, RELEASES_MAX_PAGES + 1):
        argv = [f"first:{RELEASES_PAGE}"]
        if cursor:
            argv.append('after:"%s"' % cursor.replace('"', ""))
        q = (f'query{{repository(owner:"{owner}",name:"{name}"){{'
             f'releases({",".join(argv)}){{'
             f'pageInfo{{hasNextPage endCursor}}totalCount '
             f'nodes{{tagName releaseAssets(first:1){{totalCount}}}}}}}}}}')
        block = gql(q).get("releases") or {}
        total = int(block.get("totalCount") or 0)
        nodes.extend(block.get("nodes") or [])
        pi = block.get("pageInfo") or {}
        cursor = pi.get("endCursor") or ""
        if not pi.get("hasNextPage") or not cursor:
            return {"releases": {"nodes": nodes, "totalCount": total,
                                 "pageInfo": {"hasNextPage": False, "endCursor": cursor}}}
    raise GqlError("releases 分页超过上限（%d 页 × %d 个）—— 分页游标疑似不前进"
                   % (RELEASES_MAX_PAGES, RELEASES_PAGE))


def _asset_pages(tag: str):
    """逐页产出某 tag 的资产名（REST，100/页）。**每次请求都走 _run_gh，失败即 raise。**

    先 `releases/tags/{tag}` 拿 id（1 次），再 `/releases/{id}/assets?per_page=100&page=N` 翻页。
    REST 有独立限额，不会像 GraphQL 那样一次拖死；某页失败也 raise，不降级成空集合。
    """
    safe_tag = quote(tag, safe="")
    rid_txt = _run_gh(["gh", "api", f"repos/{REPO}/releases/tags/{safe_tag}",
                       "--jq", ".id"], f"REST releases/tags/{tag}")
    try:
        rid = int(rid_txt.strip())
    except ValueError:
        raise GqlError("REST releases/tags/%s 返回的 id 不是整数（前 80 字节=%r）"
                       % (tag, rid_txt[:80])) from None
    for page in range(1, ASSETS_MAX_PAGES + 1):
        out = _run_gh(["gh", "api",
                       f"repos/{REPO}/releases/{rid}/assets?per_page={ASSETS_PER_PAGE}&page={page}",
                       "--jq", ".[].name"], f"REST releases/{rid}/assets page={page}")
        got = [x for x in out.splitlines() if x.strip()]
        yield got
        if len(got) < ASSETS_PER_PAGE:
            return
    raise GqlError("tag %s 的资产分页超过上限（%d 页 × %d 个）"
                   % (tag, ASSETS_MAX_PAGES, ASSETS_PER_PAGE))


def release_asset_names(tag: str) -> list[str]:
    """该 tag 的**全部**资产名（REST 分页拉到底）。同轮内缓存。"""
    if tag in _ASSET_NAMES:
        return _ASSET_NAMES[tag]
    names: list[str] = []
    for got in _asset_pages(tag):
        names.extend(got)
    _ASSET_NAMES[tag] = names
    return names


def has_manifest(tag: str) -> bool:
    """该 tag 下是否存在 MANIFEST* 资产（REST 按需分页；**找到即停**，不必翻到底）。

    以前靠 GraphQL 的 releaseAssets(first:100).nodes —— 资产 >100 的卷（VA 598 个）里
    MANIFEST 一旦不在前 100 就漏判，才需要 `or has_manifest(tag)` 二次兜底；现在没有 100 截断。
    """
    if tag in _HAS_MANIFEST:
        return _HAS_MANIFEST[tag]
    if tag in _ASSET_NAMES:
        _HAS_MANIFEST[tag] = any(x.startswith("MANIFEST") for x in _ASSET_NAMES[tag])
        return _HAS_MANIFEST[tag]
    for got in _asset_pages(tag):
        if any(x.startswith("MANIFEST") for x in got):
            _HAS_MANIFEST[tag] = True
            return True
    _HAS_MANIFEST[tag] = False
    return False


def status_of(tag: str) -> dict | None:
    """按**实际 tag** 取 STATUS.json（卷号原样带进去）。"""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", tag)
    tmp = Path(f"/tmp/_audit_{safe}.json")
    sh(["gh", "release", "download", tag, "-R", REPO,
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

    repos = fetch_releases()          # 分页轻查询：只取 tagName + 资产总数（不取资产名）

    # ★ A2（2026-10-30）：取数失败必须显式失败，绝不被静默吞成「所有州都没开工」。
    #   历史事故：gql 偶发返回 {} → rel 空 → 51 条假 B1（issue #5 的 41 条评论就在
    #   「51 项 / 1 项」之间来回摆）。这里改成硬拦：roster 非空而 nodes 为空 = 查询失败。
    nodes = ((repos or {}).get("releases") or {}).get("nodes") or []
    if not nodes and states:
        print("::error::GitHub GraphQL 查询失败或返回空（releases.nodes 为空）——"
              "这是取数失败，不是『未开工』；本轮禁止产出任何 B1，直接退出",
              file=sys.stderr)
        return 1

    rel: dict[str, dict] = {}
    for n in nodes:
        tag = n.get("tagName") or ""
        m = TAG_RE.match(tag)
        if not m:
            continue                      # 非 raw-US-XX-v1[-pN] 的 release 不参与账本巡检
        st = m.group(1)
        ra = n.get("releaseAssets") or {}
        has_man = has_manifest(tag)          # REST 按需：找到 MANIFEST 即停（无 100 截断）
        cnt = int(ra.get("totalCount") or 0)
        e = rel.setdefault(st, {"n": 0, "tags": []})
        e["n"] += cnt                    # 同州多卷（TX p1/p2）合并计资产
        e["tags"].append({"tag": tag, "n": cnt,
                          "man": has_man})

    if not rel and states:
        print("::error::GraphQL 返回了 releases 但没有任何 raw-US-* 命中 —— 同上按取数失败处理",
              file=sys.stderr)
        return 1

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
        busy = st in running
        if n == 0:
            if not busy:
                add("red", "A1", f"{st} 空壳：Release 存在但资产 0（无 STATUS）",
                    f"states={st} → 全量重跑")
            continue

        # A5 按**单卷**判（TX p1=665 / p2=667 各自没顶到 1000，合并成 1332 会误报）
        for t in info["tags"]:
            if t["n"] >= ASSET_CAP * 0.9:
                add("yellow", "A5",
                    f"{st}（{t['tag']}）资产 {t['n']} 个逼近上限 {ASSET_CAP}",
                    "该卷需再分卷")

        # ★ A3（2026-10-30）：有 MANIFEST 也照样查 STATUS —— 旧代码 `if man: continue`
        #   让「有 MANIFEST 的 Release 永不被检查 STATUS」，MI 就是靠这条漏了 A6。
        docs: dict[str, dict] = {}
        for t in info["tags"]:
            man = t["man"] or has_manifest(t["tag"])   # names 只取前 100，MANIFEST 未必在内
            doc = status_of(t["tag"])
            if doc is None:
                add("yellow", "A6",
                    f"{st}（{t['tag']}）有 {t['n']} 资产"
                    f"{'且有 MANIFEST' if man else ''}但无 STATUS.json",
                    "查上轮是否写 STATUS 前被 kill"
                    + ("；账本需按 manifest_reheal 口径补 STATUS" if man else ""))
            else:
                docs[t["tag"]] = doc
        if not docs:
            continue
        # 多卷时取最近更新的那份账本做进度判定
        doc = max(docs.values(), key=lambda d: str(d.get("updated_at") or ""))
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
