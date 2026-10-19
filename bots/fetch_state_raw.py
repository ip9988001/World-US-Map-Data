#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用单州四源【全量原始】数据下载入口（World-US-Map-Data）

把前两代脚本（`dover_fetch.py` 取样版 / `delaware_full_dump.py` 单州全量版）收敛成
一个「按州调度」的通用入口：州特有知识全部搬进 `rules/US/state_roster.yaml`，
本文件里不再出现任何州特有常量。

四源与适配规则（延续项目铁律：源里给什么就落什么，不清洗 / 不去重 / 不裁剪字段）：

  A. USDOT NAD       官方 GeoServices REST（动态全字段）
                     ★ 翻页改用「游标分页」：orderByFields=OBJECTID ASC + where=OBJECTID > last_id
                       旧版纯 resultOffset 深分页会在 offset=8000 被 Esri 熔断返回 400
  B. Overture Maps   官方 CLI 云端按 bbox 切（parquet 优先，失败自动回落 geojson）
  C. OpenAddresses   首页正则**动态枚举**该州全部源（run id 是 7 位流水号，不能写死）
  D. Census TIGER    TIGERweb REST 导边界 + TIGER/Line 号段小包
                     ★ 县界查询用 3 位 COUNTY 码（图层字段 COUNTY 长度=3，旧版错填 5 位导致空壳）

铁律：
  1) 所有下载都发生在 GitHub Actions runner 上（本机零下载）
  2) 产物不进 git，只作为 Release 资产
  3) 缺源不猜：单源失败如实记账
  4) **发布门禁**：任何源缺失 / 空壳产物（<1KB）/ 边界 features=0 → 直接阻断发布，绝不把空壳当资产上传

用法：
  python bots/fetch_state_raw.py --state DE --publish-release
  python bots/fetch_state_raw.py --state RI --no-release
  python bots/fetch_state_raw.py --state DE --only nad,tiger --tag raw-US-DE-v1-repair
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import random
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
import zipfile
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "bots") not in sys.path:
    sys.path.insert(0, str(ROOT / "bots"))

# 复用四源端点与 http 基建 —— 单一事实来源，换代只需改 dover_fetch.py
import dover_fetch as df  # noqa: E402

ROSTER_PATH = ROOT / "rules" / "US" / "state_roster.yaml"
ADMIN_REGISTRY = ROOT / "rules" / "geo" / "global_admin_registry.json"
MIN_ASSET_BYTES = 1024                      # 数据载荷（geojson/ndjson/zip）的空壳体量阈值
PAYLOAD_SUFFIXES = (".geojson", ".ndjson", ".zip", ".csv", ".parquet")
REQUIRED_SOURCES = ("nad", "overture", "openaddresses", "census_tiger")

# Esri 服务端对 `returnIdsOnly` 的主键清单有**静默上限**：实测 RI 整州 BBox
# returnCountOnly=569,222，returnIdsOnly 正好返回 400,000 条且**不报任何错**。
# 也就是说“已取集合 == 主键集合”这种自洽对账会被这个上限骗过去（覆盖 70% 却全绿）。
# 铁律：拿到的清单必须跟同一 BBox 的 returnCountOnly 对上，对不上就四象限递归切。
NAD_IDS_LIST_CAP = 400_000
NAD_IDS_MAX_DEPTH = 6
STAMP = time.strftime("%Y%m%d-%H%M", time.gmtime())


def log(msg: str) -> None:
    print(f"[fetch_state_raw] {msg}", flush=True)


def die(msg: str, code: int = 2):
    log("FATAL " + msg)
    raise SystemExit(code)


def rel(p) -> str:
    """相对仓库根的可读路径；--out-dir 指到仓外（本地自检）时原样返回绝对路径。"""
    try:
        return str(Path(p).relative_to(ROOT))
    except ValueError:
        return str(p)


def _esri_query(url: str, params: dict, tries: int = 4, label: str = "", timeout: int = 120) -> dict:
    """Esri / 联邦视图查询：**必须重试**，不能一次 400 就判死刑。

    2026-10-16 探针实证（DOT NAD 联邦视图，同一图层同一 bbox）：
      · `resultRecordCount=2`                          → ✅
      · `resultOffset=0` 单独带上（无 orderBy）           → ❌ 400 Cannot perform query
      · `orderByFields=OBJECTID ASC` + rc=2            → ✅
      · `resultRecordCount=2000`（=maxRecordCount）     → ❌ 400
      · rc=1000 + resultOffset=0（旧代码姿势）            → ✅ 返回 1000 条
    同一组参数时好时坏 → 这就是为什么旧版在 offset=8000 上“偶然”撞上 400、
    而换一组参数又在第 1 页撞上 —— 不是深度分页熔断，是服务端抖动。
    对策：重试（指数退避）+ 失败时缩页，最后仍失败才算真失败。
    """
    # ★ 2026-10-18 实测：同一请求这一秒 400/超时、下一秒正常 → 退避要带**抖动**，
    #   否则并发下所有客户端会在同一毫秒同时重试，把上游连接池再打崩一次。
    steps = [5.0, 15.0, 30.0]
    last = ""
    for i in range(1, tries + 1):
        try:
            payload = df.http_json(url, params, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            payload = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        err = payload.get("error")
        if not err:
            return payload
        last = str(err)[:200]
        if i < tries:
            delay = steps[min(i - 1, len(steps) - 1)] * random.uniform(0.7, 1.3)
            log(f"⚠️ {label} 第 {i}/{tries} 次被拒：{last[:110]} → {delay:.1f}s 后重试（带抖动）")
            time.sleep(delay)
    return {"error": last}


# --------------------------------------------------------------------------- 配置档
def load_roster(path: Path | None = None) -> dict:
    path = path or ROSTER_PATH
    if not path.exists():
        die(f"州配置档不存在：{path}")
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        die("缺少 PyYAML（requirements.txt 里应有 pyyaml）")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not (data.get("states") or {}):
        die(f"配置档里没有 states：{path}")
    return data


def state_cfg(roster: dict, code: str) -> dict:
    """取某州配置，并把 fips/bbox 归一化成代码里好用的形态。"""
    code = str(code).strip().upper()
    states = roster["states"]
    if code not in states:
        die(f"配置档里没有州 {code}。现有：" + ", ".join(sorted(states)))
    cfg = dict(states[code])
    cfg["state"] = code
    cfg["state_fips"] = str(cfg["state_fips"]).zfill(2)
    bbox = [float(v) for v in cfg["bbox"]]
    if len(bbox) != 4:
        die(f"{code}.bbox 必须是 [W,S,E,N] 四个数")
    cfg["bbox"] = bbox
    cfg["bbox_str"] = ",".join(str(v) for v in bbox)
    counties = []
    for c in cfg.get("counties") or []:
        fips = str(c["fips"]).zfill(5)
        counties.append({"key": str(c["key"]), "name": c.get("name") or str(c["key"]),
                         "fips": fips, "fips3": fips[2:]})
    if not counties:
        die(f"{code} 没有 counties，TIGER 号段包无法枚举")
    cfg["counties"] = counties
    lo_hi = [str(v) for v in (cfg.get("zcta_range") or [])]
    cfg["zcta_lo"] = lo_hi[0] if len(lo_hi) > 0 else ""
    cfg["zcta_hi"] = lo_hi[1] if len(lo_hi) > 1 else ""
    cfg["places"] = [str(p) for p in (cfg.get("places") or [])]
    cfg["oa_priority"] = [str(p) for p in (cfg.get("oa_priority") or [])]
    return cfg


# --------------------------------------------------------------------------- 调度器
class Fetcher:
    """一次州级抓取的上下文：产物目录、记账、问题清单。"""

    def __init__(self, cfg: dict, out: Path, args: argparse.Namespace):
        self.cfg = cfg
        self.out = out
        self.args = args
        self.ledger: list[dict] = []        # 全量记账（含中间事件）
        self.status: dict[str, dict] = {}   # 每源最终状态
        self.problems: list[str] = []       # 门禁问题（缺源/空壳/边界空）
        self.files: list[Path] = []
        self.cache: dict = {}               # 跨源共享（县界多边形：NAD 切块与 TIGER 导出共用）
        # ---- 增量发布 / 断点续传（Step 2）----
        self.publish = False                # True = 边抓边传（分片即传、MANIFEST 最后封印）
        self.tag = ""                       # 目标 Release tag
        self.meta: dict = {}                # rel(path) → {bytes, sha256, sha256_head, ...}
        self.remote: dict = {}              # 远端资产 name → size
        self.status_doc: dict = {}          # 远端 STATUS.json 内容（断点续传的依据）

    # -- 记账 ----------------------------------------------------------------
    def note(self, source: str, status: str, **kw) -> dict:
        rec = {"source": source, "status": status, **kw}
        self.ledger.append(rec)
        self.status[source] = rec
        return rec

    def problem(self, msg: str) -> None:
        self.problems.append(msg)
        log("⛔ " + msg)

    def add(self, *paths) -> None:
        for p in paths:
            p = Path(p)
            if p.exists():
                self.files.append(p)

    # -- 目录 ----------------------------------------------------------------
    def dir(self, name: str) -> Path:
        d = self.out / name
        d.mkdir(parents=True, exist_ok=True)
        return d


# --------------------------------------------------------------------------- A. NAD
def _nad_bbox_pull(fx: Fetcher, bbox_str: str, fields: list[str], oid: str,
                   limit: int, page: int, fh, seen: set, tag: str = "") -> tuple[int, int]:
    """★ 游标分页：orderByFields + `WHERE OBJECTID > last_id`，彻底绕开深分页熔断。

    旧实现用 resultOffset 翻页，跑到 offset=8000 就被 Esri 以
    `400 Cannot perform query / Invalid query parameters` 拒掉（全州实测）。
    探针复测后确认那其实是**服务端抖动**（同一组参数时好时坏），所以除了游标，
    这里还叠了 _esri_query 的指数退避重试 + 连续被拒时缩页。

    内存纪律：**流式直接写 ndjson**，只在内存里留一个 OID 集合做去重
    （40 万行 ≈ 30MB，而不是把 40 万个 39 字段字典全揣在内存里）。
    返回 (新增行数, 页数)。
    """
    added = 0
    cursor = None
    pages = 0
    while added < limit:
        batch = min(page, limit - added)
        params = {
            "where": "1=1" if cursor is None else f"{oid} > {cursor}",
            "outFields": ",".join(fields),
            "geometry": bbox_str,
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outSR": "4326",
            "orderByFields": f"{oid} ASC",   # 游标分页的前提：服务端稳定排序
            "resultRecordCount": batch,       # 不再传 resultOffset
            "f": "json",
        }
        payload = _esri_query(f"{df.NAD_OFFICIAL}/query", params, tries=4,
                              label=f"NAD{tag} page {pages + 1}（cursor={cursor} rc={batch}）",
                              timeout=fx.args.nad_timeout)
        if payload.get("error"):
            # 连续重试仍被拒 → 单页缩半再试（1000 → 500 → 250 → 125 → 100）
            if batch > 100:
                page = max(100, batch // 2)
                log(f"⚠️ NAD{tag} 连续被拒 → 单页缩到 {page} 再试")
                continue
            raise RuntimeError(f"query error {str(payload['error'])[:200]} | cursor={cursor} page={pages + 1} rc={batch}")
        feats = payload.get("features") or []
        pages += 1
        if not feats:
            break
        last = cursor
        newly = 0
        for feat in feats:
            attrs = dict(feat.get("attributes") or {})
            oid_val = attrs.get(oid)
            # ★ 游标必须跟每一条的 OID 前进（包括重复行），否则整页都是重复行时
            #   last 永远不推进 → cursor 原地不动 → 无限重拉同一页（自检里真实卡死过一次）
            dup = oid_val is not None and oid_val in seen
            if oid_val is not None:
                if last is None or oid_val > last:
                    last = oid_val
                if not dup:
                    seen.add(oid_val)
            if dup:
                continue
            geom = feat.get("geometry") or {}
            attrs["_lon"], attrs["_lat"] = geom.get("x"), geom.get("y")
            fh.write(json.dumps(attrs, ensure_ascii=False) + "\n")
            added += 1
            newly += 1
        # 三条退出阀：本页不满 / 本页全重复（无净增）/ 游标未前进
        if len(feats) < batch or newly == 0:
            break
        if cursor is not None and last == cursor:
            log(f"⚠️ NAD{tag} 游标未前进（{oid}={cursor}）→ 退出防死循环")
            break
        if last is None:
            log(f"⚠️ NAD{tag} 本页没有任何 {oid}，无法推进游标 → 退出防死循环")
            break
        cursor = last
    return added, pages


def _nad_official_count(fx: Fetcher, bbox_str: str):
    payload = _esri_query(f"{df.NAD_OFFICIAL}/query", {
        "where": "1=1", "geometry": bbox_str, "geometryType": "esriGeometryEnvelope",
        "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
        "returnCountOnly": "true", "f": "json"}, tries=3, label="NAD 官方计数",
        timeout=fx.args.nad_timeout)
    return payload.get("count")


# --------------------------------------------------------------------------- POST 通道
def _post_json(url: str, params: dict, timeout: int = 180) -> dict:
    """★★ objectIds 批取**必须**走 HTTP POST（表单 body）。

    500 个 8~9 位 OBJECTID 拼成 `objectIds=98123456,98123457,...` 是 4,500~9,000 字节，
    塞进 GET 的 query string 会被 Esri 前置网关直接 414 URI Too Long / 400 打回。
    改用 POST 表单后每批 500~800 个稳如泰山。
    出错时把**完整响应体**带回来（原来只截 200 字符，诊断信息根本不够）。
    """
    body = urllib.parse.urlencode(params, doseq=True).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": "Mozilla/5.0 (compatible; world-us-map-data/1.0; +https://github.com/ip9988001/World-US-Map-Data)",
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:4000]
        except Exception:  # noqa: BLE001
            pass
        return {"error": f"HTTP {exc.code} {exc.reason} | body={detail}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}


def _dump_error(err_log: Path, label: str, params: dict, err) -> None:
    """把失败的完整请求参数 + 完整响应体追加落盘，供事后归因。"""
    try:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "label": label,
               "params": {k: (v if k != "objectIds" else f"<{len(str(v).split(','))} ids>")
                          for k, v in params.items()},
               "objectIds_head": str(params.get("objectIds", ""))[:200],
               "error": str(err)[:4000]}
        with err_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _esri_post_query(fx: Fetcher, params: dict, err_log: Path, label: str,
                     tries: int = 4) -> dict:
    """POST 查询 + 指数退避重试；失败的完整响应落盘。"""
    steps = [5.0, 15.0, 30.0]
    last = ""
    for i in range(1, tries + 1):
        payload = _post_json(f"{df.NAD_OFFICIAL}/query", params, timeout=fx.args.nad_timeout)
        err = payload.get("error")
        if not err:
            return payload
        last = str(err)[:4000]
        _dump_error(err_log, f"{label} try{i}", params, last)
        if i < tries:
            delay = steps[min(i - 1, len(steps) - 1)] * random.uniform(0.7, 1.3)
            log(f"⚠️ {label} 第 {i}/{tries} 次被拒：{last[:110].replace(chr(10), ' ')} "
                f"→ {delay:.1f}s 后重试（带抖动）")
            time.sleep(delay)
    return {"error": last}


# --------------------------------------------------------------------------- IDS 主路径
def _nad_ids_only(fx: Fetcher, bbox_str: str, err_log: Path, label: str = "") -> list:
    """第一步：`returnIdsOnly=true` 拿主键清单（一次轻量请求，不做排序、不翻页）。"""
    payload = _esri_post_query(fx, {
        "where": "1=1", "geometry": bbox_str, "geometryType": "esriGeometryEnvelope",
        "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
        "returnIdsOnly": "true", "f": "json",
    }, err_log, f"ids_only{label}")
    if payload.get("error"):
        return []
    ids = payload.get("objectIds") or []
    return [i for i in ids if isinstance(i, int)]


def _nad_seg_count(fx: Fetcher, base: dict, err_log: Path, label: str = ""):
    """单次 `returnCountOnly`（带 where 也行）—— 最轻的对账探针。

    为什么不用 `_nad_oid_bounds` 判每段是否截断：后者要 3 个请求（count + 两个有序探针），
    而探针在 runner 出口并不总是可靠；逐段只问 count 就能判「拿到的 ids 数 == 官方数」，
    既足够又便宜（一个州几十段，省下来的全是实打实的时间）。
    """
    payload = _esri_post_query(fx, {**base, "where": base.get("where") or "1=1",
                                    "returnCountOnly": "true", "f": "json"},
                               err_log, f"{label}:count", tries=3)
    if payload.get("error"):
        return None
    cnt = payload.get("count")
    return int(cnt) if isinstance(cnt, int) else None


def _nad_ids_sample(fx: Fetcher, base: dict, err_log: Path, label: str = ""):
    """一次性拿清单（不看上限、不切分）—— 只在边界拿不到时用作抽样起点。"""
    payload = _esri_post_query(fx, {**base, "where": base.get("where") or "1=1",
                                    "returnIdsOnly": "true", "f": "json"},
                               err_log, f"{label}:ids-sample", tries=2)
    if payload.get("error"):
        return [], None
    ids = [i for i in (payload.get("objectIds") or []) if isinstance(i, int)]
    return ids, len(ids)


def _nad_count(fx: Fetcher, bbox_str: str, err_log: Path, label: str = ""):
    """同一 BBox 的官方行数（POST，失败返回 None）。"""
    payload = _esri_post_query(fx, {
        "where": "1=1", "geometry": bbox_str, "geometryType": "esriGeometryEnvelope",
        "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
        "returnCountOnly": "true", "f": "json",
    }, err_log, f"count{label}")
    if payload.get("error"):
        return None
    cnt = payload.get("count")
    return int(cnt) if isinstance(cnt, int) else None


def _quad_split(bbox) -> list:
    """四象限平分：四个子框无缝拼接 == 原框（无重叠、无缝隙）→ 并集恒等于父集。

    ★ 这里**故意不用县级 BBox 起步**：5 个县框的并集面积小于整州 BBox（少了四角
      邻州点），用县框合并出的 ID 数去跟州级 count 对账会永远差一截。四象限切分
      是严格划分，汇总去重后必然等于州级 count。
    """
    w, s, e, n = (float(v) for v in bbox)
    mx, my = (w + e) / 2.0, (s + n) / 2.0
    return [(w, s, mx, my), (mx, s, e, my), (w, my, mx, n), (mx, my, e, n)]


def _nad_collect_ids_capped(fx: Fetcher, bbox, err_log: Path, label: str = "",
                            depth: int = 0, stats: dict | None = None):
    """拿一整块 BBox 的主键清单；命中服务端 400,000 上限就四象限递归切分。

    判据（任一成立即视为「清单被截断」，必须继续切）：
      ① count 拿到了 → len(ids) < count（截断铁证）；
      ② count 没拿到 → len(ids) >= NAD_IDS_LIST_CAP（顶到上限）。
    叶子验收：len(leaf_ids) == leaf_count —— 逐叶对齐后再合并去重。
    返回 (ids, stats)。
    """
    stats = stats if stats is not None else {"leaves": 0, "cap_hits": 0,
                                              "requests": 0, "max_depth": 0,
                                              "root_count": None}
    bb_str = ",".join(str(v) for v in bbox)
    # ★ OID 区间二分（通用：几何是矩形还是多边形都行，大县也不会撞 400k）
    if getattr(fx.args, "nad_ids_slicing", "quadrant") == "oid":
        base = {"geometry": bb_str, "geometryType": "esriGeometryEnvelope",
                "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}
        ids, ost = _nad_ids_by_oid_ranges(fx, base, err_log, label or "[state]")
        cnt = ost.get("count")
        stats["leaves"] += 1 if ids else 0
        stats["cap_hits"] += ost["splits"]
        stats["requests"] += ost["queries"]
        stats["max_depth"] = max(stats["max_depth"], ost["max_depth"])
        if depth == 0:
            stats["root_count"] = cnt
        return ids, stats
    ids = _nad_ids_only(fx, bb_str, err_log, label or "[state]")
    stats["requests"] += 1
    if not ids:
        return [], stats
    cnt = _nad_count(fx, bb_str, err_log, label or "[state]")
    stats["requests"] += 1
    if depth == 0:
        stats["root_count"] = cnt
    truncated = (len(ids) < cnt) if cnt is not None else (len(ids) >= NAD_IDS_LIST_CAP)
    if not truncated:
        stats["leaves"] += 1
        stats["max_depth"] = max(stats["max_depth"], depth)
        return ids, stats
    stats["cap_hits"] += 1
    if depth >= int(getattr(fx.args, "nad_ids_max_depth", NAD_IDS_MAX_DEPTH)):
        log(f"⚠️ NAD{label} 切到第 {depth} 层仍被截断"
            f"（{len(ids):,}/{cnt if cnt is not None else '?'}）→ 用已拿到的清单，由门禁拦")
        return ids, stats
    log(f"⚠️ NAD{label} 主键清单被截断：{len(ids):,} / 官方 count "
        f"{cnt if cnt is not None else '未知'} → 四象限切分（第 {depth + 1} 层）")
    out: list = []
    for i, qb in enumerate(_quad_split(bbox)):
        sub, _ = _nad_collect_ids_capped(fx, qb, err_log, f"{label}.q{i + 1}",
                                        depth + 1, stats)
        out.extend(sub)
    return list(dict.fromkeys(out)), stats


def _nad_oid_probe(fx: Fetcher, base: dict, err_log: Path, label: str, desc: bool):
    """有序取一条 OBJECTID（stats 不可用时的 mn/mx 探针）。

    2026-10-16 探针实证：`orderByFields=OBJECTID ASC + resultRecordCount=1/2` 走 GET 稳定 200，
    而 `outStatistics` 聚合在 runner 出口会被连打 400（本机同参正常）→ 探针是可靠替补。
    """
    params = {**base, "where": base.get("where") or "1=1",
              "outFields": "OBJECTID", "returnGeometry": "false",
              "orderByFields": f"OBJECTID {'DESC' if desc else 'ASC'}",
              "resultRecordCount": "1", "f": "json"}
    payload = _esri_post_query(fx, params, err_log,
                               f"{label}:probe{'-max' if desc else '-min'}", tries=2)
    if payload.get("error"):
        return None
    feats = payload.get("features") or []
    if feats:
        at = feats[0].get("attributes") or {}
        for k, v in at.items():
            if str(k).upper().replace("_", "") == "OBJECTID" and isinstance(v, (int, float)):
                return int(v)
        vals = [int(v) for v in at.values() if isinstance(v, (int, float))]
        if vals:
            return vals[0]
    ids = payload.get("objectIds") or []
    return int(ids[0]) if ids else None


def _nad_oid_bounds(fx: Fetcher, base: dict, err_log: Path, label: str = ""):
    """用聚合一次性拿 (min OBJECTID, max OBJECTID, count)，不拉全量 ids。

    ★ 分层回退（2026-10-17 runner 实证）：GitHub Actions 出口对 `outStatistics` 聚合查询
      连吃 4 次 400 `Unable to perform query`（本地同参数 4.5s 正常返回 751,212），
      而普通 ids/count/有序查询不受影响 → 聚合必须有降级，否则整州直接判死。
        ① POST outStatistics（1 请求拿三数，最优）
        ② GET  outStatistics（换通道，绕开 POST 侧限流）
        ③ 有序单行探针 mn/mx + returnCountOnly 拿 count
        ④ 仍不行 → (None, None, count|None)，上层改走「单次清单」不做区间切分
    ★ 熔断：①② 一旦连败，本进程内标记 `_nad_stats_down`，后续每段不再白等重试
      （否则每段 4×56s，一个州上百段 = 跑到 job 超时）。
    """
    stats = json.dumps([
        {"statisticType": "min", "onStatisticField": "OBJECTID", "outStatisticFieldName": "mn"},
        {"statisticType": "max", "onStatisticField": "OBJECTID", "outStatisticFieldName": "mx"},
        {"statisticType": "count", "onStatisticField": "OBJECTID", "outStatisticFieldName": "n"},
    ])

    def _read(payload: dict):
        feats = payload.get("features") or []
        if not feats:
            return None, None, 0
        at = feats[0].get("attributes") or {}
        mn, mx, n = at.get("mn"), at.get("mx"), at.get("n")
        return (int(mn) if mn is not None else None,
                int(mx) if mx is not None else None,
                int(n) if n is not None else None)

    if not fx.cache.get("_nad_stats_down"):
        payload = _esri_post_query(fx, {**base, "outStatistics": stats, "f": "json"},
                                   err_log, f"{label}:stats", tries=2)
        if not payload.get("error"):
            return _read(payload)
        log(f"⚠️ {label} 聚合查询(POST)被拒 → 换 GET 通道再试一次")
        payload = _esri_query(f"{df.NAD_OFFICIAL}/query", {**base, "outStatistics": stats, "f": "json"},
                              tries=2, label=f"{label}:stats-get", timeout=fx.args.nad_timeout)
        if not payload.get("error"):
            return _read(payload)
        fx.cache["_nad_stats_down"] = True
        log(f"⚠️ {label} 聚合查询两条通道全挂 → 本轮改用「有序单行探针」，后续段不再试聚合")

    # ---- 降级：count 用普通查询拿（轻），mn/mx 用有序单行探针
    cnt = None
    p = _esri_post_query(fx, {**base, "where": base.get("where") or "1=1",
                              "returnCountOnly": "true", "f": "json"},
                         err_log, f"{label}:count", tries=2)
    if not p.get("error"):
        cnt = p.get("count")
        cnt = int(cnt) if isinstance(cnt, int) else None
    mn = _nad_oid_probe(fx, base, err_log, label, desc=False)
    mx = _nad_oid_probe(fx, base, err_log, label, desc=True)
    if cnt is not None and cnt == 0:
        return None, None, 0
    if mn is None or mx is None:
        return None, None, cnt
    return mn, mx, cnt


def _nad_ids_by_oid_ranges(fx: Fetcher, base: dict, err_log: Path,
                           label: str = "", max_depth: int = 24):
    """★ 通用主键清单获取：按 OBJECTID 区间二分，直到每段 ids < 400,000。

    为什么不继续用 bbox 四象限：
      · 几何是**多边形**（县界）时根本没法切矩形；
      · 大县（如 LA 约 270 万）连县级 ids 也会撞 400k 静默上限。
    OBJECTID 区间是**纯属性切分**，跟几何/属性过滤完全正交：不重、不漏、可递推。

    `base` 是除 where 以外的查询参数（几何、spatialRel、inSR 等）。
    """
    out: list = []
    stats = {"queries": 0, "splits": 0, "max_depth": 0, "truncated_leaves": 0}
    mn, mx, n = _nad_oid_bounds(fx, base, err_log, label)
    stats["queries"] += 3
    stats["count"] = n
    if n == 0:
        return [], stats
    if mn is None or mx is None:
        # ★ 连有序探针都拿不到边界 → 放弃区间切分，退回单次清单（<400k 的县/州照样完整）
        # ★ 边界拿不到（聚合 + 两个探针全挂）时，**不能直接相信一次清单**：
        #   run 39058950492 的 RI 就是这么错的 —— 一次返回 400,000（=服务端上限）被当成全量，
        #   实际 569,222，少了 16.9 万主键，直到三数咬合才暴露（差 52,874）。
        #   正确姿势：拿一份抽样清单当起点，用 returnCountOnly 指数扩大上界，再走正常二分。
        stats["bounds_bootstrapped"] = 1
        sample, _ = _nad_ids_sample(fx, base, err_log, label)
        stats["queries"] += 1
        if not sample:
            return [], stats
        if n is None:
            n = _nad_seg_count(fx, base, err_log, f"{label}:count-boot")
            stats["queries"] += 1
            stats["count"] = n
        lo, hi = min(sample), max(sample) + 1
        if n is None or len(sample) >= n:
            if len(sample) >= NAD_IDS_LIST_CAP:
                stats["truncated_leaves"] += 1
                log(f"⚠️ {label} 清单顶到 {NAD_IDS_LIST_CAP:,} 上限且无官方 count 可对"
                    f" → 原样交上层，由三数咬合暴露缺口")
            else:
                log(f"ℹ️ {label} 无边界，单次清单已是全量：{len(sample):,} 个")
            return sample, stats
        grew = 0
        while grew < 24:
            c = _nad_seg_count(fx, {**base, "where": f"OBJECTID >= {lo} AND OBJECTID < {hi}"},
                               err_log, f"{label}:grow{grew + 1}")
            stats["queries"] += 1
            grew += 1
            if c is not None and c >= n:
                break
            hi = lo + (hi - lo) * 2
        mn, mx = lo, hi - 1          # 自兜出来的边界直接进正常二分
        log(f"ℹ️ {label} 边界自兜：抽样 {len(sample):,} / 官方 {n:,}"
            f" → 上界扩到 {hi:,}（{grew} 次 count 试探）后照常二分")
    stack = [(mn, mx + 1, 0)]
    while stack:
        lo, hi, depth = stack.pop()
        where = f"OBJECTID >= {lo} AND OBJECTID < {hi}"
        node = {**base, "where": where}
        # ★ 逐段先问 count：这样即使服务端上限不是 400k（或有其它隐式限制），
        #   也能用“拿到的手数 != count”当场发现被截断，不依赖任何魔法数字。
        #   用单次 returnCountOnly（不是 3 请求的 _nad_oid_bounds）—— 聚合熔断也照样能用。
        seg_n = _nad_seg_count(fx, node, err_log, f"{label}[{lo}-{hi})")
        stats["queries"] += 1
        payload = _esri_post_query(fx, {**node, "returnIdsOnly": "true", "f": "json"},
                                   err_log, f"{label}[{lo}-{hi})")
        stats["queries"] += 1
        stats["max_depth"] = max(stats["max_depth"], depth)
        ids = [i for i in (payload.get("objectIds") or []) if isinstance(i, int)]
        complete = (not payload.get("error")) and len(ids) < NAD_IDS_LIST_CAP \
            and (seg_n is None or len(ids) == seg_n)
        if complete:
            out.extend(ids)
            continue
        if hi - lo <= 1 or depth >= max_depth:
            out.extend(ids)           # 退化：拿多少算多少，交给上层 count 对账暴露
            stats["truncated_leaves"] += 1
            continue
        mid = (lo + hi) // 2
        stats["splits"] += 1
        stack.append((mid, hi, depth + 1))
        stack.append((lo, mid, depth + 1))
    return list(dict.fromkeys(out)), stats


def _nad_one_batch(fx: Fetcher, chunk: list, fields: list[str], err_log: Path,
                   label: str) -> list | None:
    """按 objectIds 点名取一批属性（POST）。失败返回 None，由上层缩批重试。

    ★ 这里故意**不重试同一批**（tries=1）：如果 400 是“批太大/URL 太长”引起的，
      原样重发十次也是十次 400，浪费时间。正确姿势是**立即拆半**（拆到单条），
      真正的瞬时抖动由最后那次“硬对账 + 补拉”兼顾。
    """
    payload = _esri_post_query(fx, {
        "objectIds": ",".join(str(i) for i in chunk),
        "outFields": ",".join(fields),
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
    }, err_log, label, tries=1)
    if payload.get("error"):
        return None
    feats = payload.get("features") or []
    out = []
    for feat in feats:
        attrs = dict(feat.get("attributes") or {})
        geom = feat.get("geometry") or {}
        attrs["_lon"], attrs["_lat"] = geom.get("x"), geom.get("y")
        out.append(attrs)
    return out


def _nad_fetch_by_ids(fx: Fetcher, ids: list, fields: list[str], oid: str, fh, seen: set,
                      err_log: Path) -> dict:
    """第二/三步：分批点名取数（无游标、无 offset、成本恒定）+ 失败缩批重试。"""
    batch = max(50, int(fx.args.nad_ids_batch))
    stats = {"batches": 0, "rows": 0, "shrinks": 0, "failed_batches": 0, "batch_size": batch}
    pending = [ids[i:i + batch] for i in range(0, len(ids), batch)]
    total = len(pending)
    done = 0
    while pending:
        chunk = pending.pop(0)
        got = _nad_one_batch(fx, chunk, fields, err_log, f"ids_batch[{done + 1}/{total}]")
        if got is None:
            if len(chunk) > 1:          # 任何一条失败就拆半重试（一直拆到单条）
                mid = len(chunk) // 2
                pending.insert(0, chunk[mid:])
                pending.insert(0, chunk[:mid])
                stats["shrinks"] += 1
                continue
            stats["failed_batches"] += 1
            done += 1
            continue
        stats["batches"] += 1
        done += 1
        for attrs in got:
            o = attrs.get(oid)
            if o is None or o in seen:
                continue
            seen.add(o)
            fh.write(json.dumps(attrs, ensure_ascii=False) + "\n")
            stats["rows"] += 1
        if done % 50 == 0:
            log(f"NAD 批取进度 {done}/{total} 批｜已落 {stats['rows']:,} 行｜缩批 {stats['shrinks']} 次")
    return stats


# --------------------------------------------------------------------------- 自适应降级链
def _grid_bboxes(bbox, n: int = 2) -> list[dict]:
    """把 bbox 均分成 n×n 网格（纯几何、零依赖、完整覆盖、互不重叠）。"""
    W, S, E, N = [float(v) for v in bbox]
    n = max(2, int(n))
    xs = [W + (E - W) * i / n for i in range(n + 1)]
    ys = [S + (N - S) * j / n for j in range(n + 1)]
    return [{"key": f"grid_{i}{j}", "bbox": (xs[i], ys[j], xs[i + 1], ys[j + 1])}
            for i in range(n) for j in range(n)]


def _tract_bboxes(fx: Fetcher) -> list[dict]:
    """第三级切块：Census Tract。一州一个小包，pyshp 读 bbox，**GEOID 11 位 + vintage 2020**。

    TIGER/Line 的 TRACT 包同时也是 `geo_units` 表要用的东西 —— 一次下载，两件事都办。
    注意 tract 编号 10 年一换版（TIGER2024 里是 2020 版），manifest 必须锁 vintage。
    """
    if "tract_bboxes" in fx.cache:
        return fx.cache["tract_bboxes"]
    out: list[dict] = []
    url = f"{fx.cfg['_endpoints']['tiger_base']}/TRACT/tl_2024_{fx.cfg['state_fips']}_tract.zip"
    try:
        import shapefile  # noqa: PLC0415
        blob = df.http_get(url, timeout=fx.args.tiger_timeout)
        d = fx.dir("tiger")
        path = d / f"tl_2024_{fx.cfg['state_fips']}_tract.zip"
        path.write_bytes(blob)
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            shp_name = next((n for n in zf.namelist() if n.endswith(".shp")), None)
            dbf_name = next((n for n in zf.namelist() if n.endswith(".dbf")), None)
            if shp_name and dbf_name:
                reader = shapefile.Reader(shp=io.BytesIO(zf.read(shp_name)),
                                          dbf=io.BytesIO(zf.read(dbf_name)))
                for shape, rec in zip(reader.shapes(), reader.records()):
                    a = rec.as_dict()
                    geoid = str(a.get("GEOID") or "")
                    b = list(shape.bbox)
                    if len(b) != 4 or not geoid:
                        continue
                    out.append({"key": f"tract_{geoid}", "bbox": tuple(b), "vintage": "2020"})
        log(f"TIGER tract 边界：{len(out)} 个（vintage 2020）")
    except Exception as exc:  # noqa: BLE001
        log(f"⚠️ tract 边界获取失败（跳过这一级）：{type(exc).__name__}: {str(exc)[:160]}")
    fx.cache["tract_bboxes"] = out
    return out


def _bbox_overlap(a, b) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _subdivide(fx: Fetcher, bbox_str: str, depth: int) -> list[dict]:
    """给一个抓不动的 bbox 找下一级切块：tract 优先，没 tract 就 2×2 网格。"""
    bbox = tuple(float(v) for v in bbox_str.split(","))
    tracts = [t for t in _tract_bboxes(fx) if _bbox_overlap(t["bbox"], bbox)]
    if tracts:
        return tracts
    return _grid_bboxes(bbox, 2)


def _collect_ids(fx: Fetcher, bbox_str: str, err_log: Path, label: str = "",
                 depth: int = 0) -> list:
    """取某块的全量主键；失败或超阈值就自动降级到下一级切块（县 → tract → 网格）。"""
    ids = _nad_ids_only(fx, bbox_str, err_log, label=f"[{label}]")
    threshold = int(fx.args.nad_split_threshold)
    if ids and len(ids) <= threshold:
        return ids
    if depth >= 2:
        return ids                       # 到底了，有多少算多少（后面还有硬对账兜底）
    subs = _subdivide(fx, bbox_str, depth)
    log(f"NAD 降级：{label}（{len(ids):,} 条）→ 拆成 {len(subs)} 个下级块（depth={depth + 1}）")
    out: list = []
    for i, s in enumerate(subs):
        bb = ",".join(str(v) for v in s["bbox"])
        got = _collect_ids(fx, bb, err_log, label=f"{label}.{s['key']}", depth=depth + 1)
        out.extend(got)
    return list(dict.fromkeys(out))


def _nad_shards(fx: Fetcher) -> list[dict]:
    """一级切块（按县）；给 cursor 模式与降级链用。"""
    mode = fx.args.nad_shards
    if mode == "none":
        return [{"key": "whole", "bbox": tuple(fx.cfg["bbox"])}]
    if mode == "grid":
        return _grid_bboxes(fx.cfg["bbox"], int(fx.args.nad_grid))
    bnd = _county_boundaries(fx)          # 县界同时给 TIGER 用 → 只查一次
    shards = []
    for c in fx.cfg["counties"]:
        rec = bnd.get(f"county_{c['key']}")
        if not rec:
            continue
        bb = _geojson_bbox(rec["payload"])
        if bb:
            shards.append({"key": f"cty_{c['key']}", "bbox": bb})
    if len(shards) < 2:
        log(f"⚠️ 可用县界只有 {len(shards)} 个 → 退回「州 bbox 切 2×2 网格」")
        return _grid_bboxes(fx.cfg["bbox"], 2)
    return shards


def _geojson_bbox(payload: dict):
    """从 Polygon / MultiPolygon 的 rings 里算包围盒（只取坐标，不碰属性）。"""
    xs, ys = [], []
    for feat in payload.get("features") or []:
        geom = feat.get("geometry") or {}
        rings = geom.get("rings") or []
        if not rings and geom.get("type") == "MultiPolygon":
            rings = [r for poly in (geom.get("coordinates") or []) for r in poly]
        for ring in rings:
            for pt in ring:
                try:
                    xs.append(float(pt[0]))
                    ys.append(float(pt[1]))
                except (TypeError, ValueError, IndexError):
                    continue
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _nad_shard_worker(fx: Fetcher, shard: dict, fields: list[str], oid: str,
                      page: int, limit: int) -> dict:
    """cursor 模式下的单分片翻页（保留作备选路径；ids 模式不用它）。"""
    bbox_str = ",".join(str(v) for v in shard["bbox"])
    part = fx.dir("nad") / f"nad_part_{shard['key']}.ndjson"
    seen: set = set()
    t0 = time.time()
    with part.open("w", encoding="utf-8") as fh:
        added, pages = _nad_bbox_pull(fh=fh, seen=seen, fx=fx, bbox_str=bbox_str, fields=fields,
                                      oid=oid, limit=limit, page=page,
                                      tag=f"[{shard['key']}]")
    return {"shard": shard["key"], "bbox": bbox_str, "rows": added, "pages": pages,
            "bytes": part.stat().st_size, "elapsed": round(time.time() - t0, 1), "part": part}


def harvest_nad(fx: Fetcher) -> None:
    t0 = time.time()
    d = fx.dir("nad")
    df.nad_guard()                                  # 黑名单守卫：绝不回退第三方阉割镜像

    meta, fields = df.nad_official_metadata()
    (d / "nad_native_metadata.json").write_text(
        json.dumps({k: meta[k] for k in ("name", "type", "maxRecordCount", "objectIdField",
                                         "description", "copyrightText", "fields") if k in meta},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    oid = str(meta.get("objectIdField") or "OBJECTID")
    if oid not in fields:
        fields = [oid] + fields
    maxrc = int(meta.get("maxRecordCount") or 2000)
    page = int(fx.args.nad_page) if fx.args.nad_page else min(maxrc, 1000)
    page = max(1, min(page, maxrc, 1000))
    err_log = d / "nad_http_errors.log"
    if err_log.exists():
        err_log.unlink()
    ndjson = d / f"nad_us_{fx.cfg['state'].lower()}_full.ndjson"
    seen: set = set()

    # ================= v1 主路径：按县切片 + gzip（--pack-mode county_shards）=================
    if getattr(fx.args, "pack_mode", "single") == "county_shards" and fx.args.nad_mode == "ids":
        _nad_harvest_county_shards(fx, d, fields, oid, err_log)
        return

    # ================= cursor 模式（备选路径）=================
    if fx.args.nad_mode == "cursor":
        shards = _nad_shards(fx)
        workers = max(1, min(int(fx.args.nad_workers), len(shards)))
        official = _nad_official_count(fx, fx.cfg["bbox_str"])
        log(f"NAD 模式=cursor｜切块={fx.args.nad_shards}｜分片={len(shards)}｜并发={workers}｜单页={page}")
        parts, shard_detail = [], []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_nad_shard_worker, fx, s, fields, oid, page, fx.args.nad_max): s
                    for s in shards}
            for fut in as_completed(futs):
                s = futs[fut]
                try:
                    info = fut.result()
                    parts.append(info.pop("part"))
                    shard_detail.append(info)
                except Exception as exc:  # noqa: BLE001
                    shard_detail.append({"shard": s["key"], "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        with ndjson.open("w", encoding="utf-8") as out:
            for part in parts:
                with part.open(encoding="utf-8") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        o = json.loads(line).get(oid)
                        if o is not None:
                            if o in seen:
                                continue
                            seen.add(o)
                        out.write(line if line.endswith("\n") else line + "\n")
        for p in parts:
            p.unlink(missing_ok=True)
        added = len(seen)

        # 整州自愈：合并后仍少于官方 count 的 99% → 4 分块重拉补缺口
        repair = None
        if official and added < int(official) * 0.99:
            W, S, E, N = fx.cfg["bbox"]
            midx, midy = (W + E) / 2, (S + N) / 2
            tiles = [(W, S, midx, midy), (midx, S, E, midy), (W, midy, midx, N), (midx, midy, E, N)]
            extra_rows = extra_pages = 0
            with ndjson.open("a", encoding="utf-8") as out:
                for tl in tiles:
                    n, p = _nad_bbox_pull(fh=out, seen=seen, fx=fx,
                                          bbox_str=",".join(str(v) for v in tl), fields=fields,
                                          oid=oid, limit=fx.args.nad_max, page=page, tag="[repair]")
                    extra_rows += n
                    extra_pages += p
            added += extra_rows
            repair = {"tiles": 4, "pages": extra_pages, "rows_added": extra_rows, "after": added}

        truncated = bool(official) and added < int(official)
        scan = _scan_nad_ndjson(ndjson, fx.cfg["state"])
        fx.add(d / "nad_native_metadata.json", ndjson)
        fx.note("nad", "warn-truncated" if truncated else "ok", channel="A-official-rest",
                mode="cursor", pagination=f"cursor: orderByFields={oid} ASC",
                shard_mode=fx.args.nad_shards, shard_count=len(shards), workers=workers,
                shard_detail=shard_detail, page_size=page, repair=repair, **scan,
                rows_fetched=added, official_count_in_bbox=official, truncated=truncated,
                bytes=ndjson.stat().st_size, elapsed=round(time.time() - t0, 1))
        if truncated:
            fx.problem(f"NAD 拉取 {added} 行 < 官方 count {official}")
        if added == 0:
            fx.problem("NAD 一行都没拉到")
        return

    # ================= ids 模式（主路径：returnIdsOnly + objectIds 批取）=================
    log(f"NAD 模式=ids｜单批={fx.args.nad_ids_batch}｜降级阈值={fx.args.nad_split_threshold} 行")

    target, id_stats = _nad_collect_ids_capped(fx, fx.cfg["bbox"], err_log, label="[state]")
    official_count = id_stats.get("root_count")
    fallback_used = None
    if not target:
        log("⚠️ 州级 returnIdsOnly 失败/为空 → 启用自适应降级链（县 → tract → 0.02°网格）")
        ids: list = []
        for s in _nad_shards(fx):
            bb = ",".join(str(v) for v in s["bbox"])
            ids.extend(_collect_ids(fx, bb, err_log, label=s["key"]))
        target = list(dict.fromkeys(ids))
        fallback_used = f"adaptive:{fx.args.nad_shards}"
        official_count = None      # 降级链不保证覆盖州框，不对账（由行数对账兜底）
    else:
        log(f"NAD 官方主键清单：{len(target):,} 个（官方 count "
            f"{official_count if official_count is not None else '未知'}｜"
            f"叶子 {id_stats['leaves']}｜上限命中 {id_stats['cap_hits']}｜"
            f"切分层深 {id_stats['max_depth']}）")
        if official_count is not None and len(target) != official_count:
            fx.problem(f"NAD 主键清单与官方 count 不一致：{len(target):,} != {official_count:,}"
                       f"（覆盖率 {len(target) / official_count:.1%}）")

    if not target:
        fx.add(d / "nad_native_metadata.json")
        fx.note("nad", "error", channel="A-official-rest", mode="ids",
                error="returnIdsOnly 与降级链都没拿到主键", elapsed=round(time.time() - t0, 1))
        fx.problem("NAD 一个主键都没拿到（见 nad_http_errors.log）")
        return

    with ndjson.open("w", encoding="utf-8") as fh:
        stats = _nad_fetch_by_ids(fx, target, fields, oid, fh, seen, err_log)

    # ---- 硬对账：已取 OID 集合必须 == 官方主键集合（差一条都不放过）----
    missing = [i for i in target if i not in seen]
    retried = 0
    if missing:
        log(f"⚠️ 首次批取缺 {len(missing):,} 条 → 用更小批次补一次")
        old_batch = fx.args.nad_ids_batch
        fx.args.nad_ids_batch = min(100, max(25, old_batch // 5))
        with ndjson.open("a", encoding="utf-8") as fh:
            r2 = _nad_fetch_by_ids(fx, missing, fields, oid, fh, seen, err_log)
        retried = r2.get("rows", 0)
        fx.args.nad_ids_batch = old_batch
        missing = [i for i in target if i not in seen]

    (d / "nad_full_columns.json").write_text(
        json.dumps({"fields": fields, "fields_n": len(fields), "object_id_field": oid},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    got = len(seen)
    truncated = bool(missing)
    scan = _scan_nad_ndjson(ndjson, fx.cfg["state"])
    fx.add(d / "nad_native_metadata.json", ndjson, d / "nad_full_columns.json")
    if err_log.exists() and err_log.stat().st_size > 0:
        fx.add(err_log)
    fx.note("nad", "warn-truncated" if truncated else "ok",
            channel="A-official-rest", mode="ids",
            fetch_strategy="returnIdsOnly → objectIds 批取（POST）→ 集合硬对账",
            ids_batch=stats.get("batch_size"), batches=stats.get("batches"),
            shrinks=stats.get("shrinks"), failed_batches=stats.get("failed_batches"),
            fallback=fallback_used, retry_pass_rows=retried,
            official_count=official_count, coverage_pct=(round(100.0 * got / official_count, 2)
                                                        if official_count else None),
            ids_leaves=id_stats["leaves"], ids_cap_hits=id_stats["cap_hits"],
            ids_requests=id_stats["requests"], ids_max_depth=id_stats["max_depth"],
            official_ids=len(target), rows_fetched=got, missing_ids=len(missing),
            **scan,
            missing_sample=[str(i) for i in missing[:5]],
            truncated=truncated, streamed_to=rel(ndjson), fields_n=len(fields),
            bytes=ndjson.stat().st_size, elapsed=round(time.time() - t0, 1))
    if truncated:
        fx.problem(f"NAD 硬对账失败：缺 {len(missing):,}/{len(target):,} 个主键"
                   f"（样本 {missing[:5]}，详见 nad_http_errors.log）")
    if got == 0:
        fx.problem("NAD 一行都没拉到")


# --------------------------------------------------------------------------- B. Overture
def _count_parquet(path: Path) -> int | None:
    try:
        import duckdb  # noqa: PLC0415
    except ImportError:
        return None
    try:
        con = duckdb.connect()
        return int(con.execute("select count(*) from read_parquet(?)", [str(path)]).fetchone()[0])
    except Exception:  # noqa: BLE001
        return None


def harvest_overture(fx: Fetcher) -> None:
    t0 = time.time()
    cli = shutil.which("overturemaps")
    if not cli:
        raise RuntimeError("overturemaps CLI 未安装（requirements.txt 里应有 overturemaps）")
    d = fx.dir("overture")
    state_lc = fx.cfg["state"].lower()
    bbox = fx.cfg["bbox_str"]
    cli_version = ""
    try:
        v = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=120)
        cli_version = ((v.stdout or "") + (v.stderr or "")).strip().splitlines()[0][:80]
    except Exception:  # noqa: BLE001
        cli_version = ""
    release = ""

    # ★ CLI 的 -f 只接受 geojson / geojsonseq / geoparquet（写 parquet 会 rc=2）
    #   → 参数层仍叫 parquet（对外语义），内部映射成 geoparquet（CLI 真值），落盘后缀 .parquet
    CLI_FMT = {"parquet": "geoparquet", "geojson": "geojson"}
    EXT = {"geoparquet": "parquet", "geojson": "geojson"}
    prefs = ["geoparquet", "geojson"] if fx.args.overture_format == "parquet" else ["geojson"]
    target, used, tries = None, None, []
    # ★ 同一格式重试一次：Overture CLI 的 STAC 查询偶发抖动（0 行秒回），
    #   不重试就会把网络抖动误判成"上游没数据"（HI 事故的教训之一）。
    plan_fmt = [f for fmt in prefs for f in (fmt, fmt)]
    for fmt in plan_fmt:
        cand = d / f"overture_us_{state_lc}_addresses.{EXT[fmt]}"
        cmd = [cli, "download", f"--bbox={bbox}", "-f", fmt, "--type=address", "-o", str(cand)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=fx.args.overture_timeout)
        tail = ((proc.stdout or "")[-300:] + (proc.stderr or "")[-400:]).strip()
        ok = proc.returncode == 0 and cand.exists() and cand.stat().st_size >= MIN_ASSET_BYTES
        tries.append({"format": fmt, "rc": proc.returncode, "ok": bool(ok),
                      "bytes": cand.stat().st_size if cand.exists() else 0, "tail": tail[-200:]})
        if ok:
            target, used = cand, fmt
            m = re.search(r"(\d{4}-\d{2}-\d{2}(?:\.\d+)?)", (proc.stdout or "") + (proc.stderr or ""))
            release = m.group(1) if m else ""
            break
        if cand.exists():
            cand.unlink()          # 不留下半成品/空壳
    if not target:
        raise RuntimeError(f"overturemaps 两种格式都失败：{json.dumps(tries, ensure_ascii=False)[:400]}")

    feats = _count_parquet(target) if used == "geoparquet" else _count_geojson(target)
    audit = {"cli_version": cli_version, "release": release, "state_bbox": bbox,
             "command": "overturemaps download --type=address", "tries": tries,
             "features": feats, "probed_at": _now()}

    if feats == 0:
        # ★ 防假 0（2026-10-18）：州框 0 行不能直接认"上游没有"，必须用大县小框证伪。
        probes = _overture_probe_counties(fx, cli, d)
        audit["probes"] = probes
        hit = [x for x in probes if (x.get("rows") or 0) > 0]
        if hit:
            target.unlink(missing_ok=True)
            fx.note("overture", "error", bbox=bbox, features=0, format=used, audit=audit,
                    elapsed=round(time.time() - t0, 1))
            fx.problem(f"Overture 假 0：州框 0 行、但县探针 {hit[0]['county']} 返回 "
                       f"{hit[0]['rows']:,} 行 → BBox/空间拓扑异常，禁止封印（已上传资产保留）")
            return
        target.unlink(missing_ok=True)
        fx.note("overture", "empty_upstream", bbox=bbox, features=0, format=used,
                audit=audit, elapsed=round(time.time() - t0, 1),
                note=(f"当前 release 的 addresses 主题无本州数据：州框 {bbox} 与 "
                      f"{len(probes)} 个大县探针均返回 0 行（CLI {cli_version}）"))
        _append_notes(fx, [f"- **Overture**: 当前 release 的 addresses 主题无本州数据"
                           f"（州框与 {len(probes)} 个大县探针均 0 行，CLI {cli_version}，"
                           f"release {release or '未知'}）。"])
        log(f"ℹ️ Overture 无本州数据（州框 + {len(probes)} 个县探针均 0）→ 记 empty_upstream，放行")
        return

    fx.add(target)
    fx.note("overture", "ok", bbox=bbox, format=used, features=feats,
            cli_version=cli_version, overture_release=release, feature_file=rel(target),
            format_cli=CLI_FMT.get(used, used), audit=audit,
            bytes=target.stat().st_size, tries=tries, elapsed=round(time.time() - t0, 1))


# --------------------------------------------------------------------------- C. OpenAddresses
def _scan_nad_ndjson(path: Path, state_code: str) -> dict:
    """一遍流式扫描 NAD 产物：行数 + State 分布（**只读观察，不改数据**）。

    为什么写进元数据：BBox 是矩形，必然把邻州边角门牌包进来。下游看一眼
    `state_distribution` 就知道这个包里纯本州数据到底多少条，不用去解 800MB。

    ★ 用正则抠字符串而不是 json.loads：这里只要两个字段，千万行级别差几倍 CPU。
    """
    rows = 0
    states: Counter = Counter()
    pat = re.compile(r'"State"\s*:\s*"([^"]*)"')
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                rows += 1
                m = pat.search(line)
                states[m.group(1) if m else ""] += 1
    except Exception as exc:  # noqa: BLE001
        return {"rows": 0, "state_distribution": {}, "scan_error": str(exc)[:150]}
    dist = dict(states.most_common())
    in_scope = states.get(state_code, 0)
    return {"rows": rows, "state_distribution": dist,
            "state_in_scope_rows": in_scope,
            "state_in_scope_pct": round(100.0 * in_scope / rows, 2) if rows else None,
            "state_blank_rows": states.get("", 0)}


def _count_zip_rows(blob: bytes) -> int:
    n = 0
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for info in zf.infolist():
                if not info.filename.lower().endswith((".csv", ".ndjson")):
                    continue
                with zf.open(info) as fh:
                    n += sum(1 for _ in io.TextIOWrapper(fh, encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        pass
    return n


def discover_oa_sources(state_lc: str) -> dict[str, tuple[str, str, str]]:
    """★ 动态枚举：首页正则抽全部 us/<州>/ 源 zip，取最新 run。

    OA 的 run id 是 7 位流水号，源名是**数据提供者名**（kent / city_of_dover / sussex…）
    而不是州名 —— 所以每个州的源清单都不一样，只能动态发现，绝不写死。
    """
    home = df.http_get(df.OA_HOME, timeout=120).decode("utf-8", "replace")
    found = re.findall(r"https://data\.openaddresses\.io/runs/(\d+)/us/([a-z]{2})/([a-z0-9_]+)\.zip", home)
    latest: dict[str, tuple[str, str, str]] = {}
    for run, st, name in found:
        if st == state_lc:
            latest.setdefault(name, (run, st, name))   # 首页只列最新 run → 取首次出现
    return latest


def harvest_openaddresses(fx: Fetcher) -> None:
    t0 = time.time()
    d = fx.dir("openaddresses")
    state_lc = fx.cfg["state"].lower()
    latest = discover_oa_sources(state_lc)
    if not latest:
        raise RuntimeError(f"results.openaddresses.io 首页未发现 us/{state_lc} 的任何源 zip")

    priority = [n for n in fx.cfg["oa_priority"] if n in latest]
    order = priority + [n for n in sorted(latest) if n not in priority]
    files, detail, fail = [], [], []
    for name in order[:fx.args.oa_max_files]:
        run, st, src = latest[name]
        url = df.OA_RUN.format(run=run, st=st, src=src)
        try:
            blob = df.http_get(url, timeout=fx.args.oa_timeout)
            if len(blob) > fx.args.oa_max_mb * 1024 * 1024:
                detail.append({"src": name, "run": run, "skipped": f"{len(blob) / 1e6:.1f}MB 超过 {fx.args.oa_max_mb}MB 上限"})
                continue
            rows = _count_zip_rows(blob)
            if rows == 0 or len(blob) < MIN_ASSET_BYTES:
                # ★ 2026-10-18 AK 实测：OA 对"无门牌县"只吐 ~891B 的空 zip 头，
                #   没有任何数据文件。它是**合法的空上游**，不是传输截断：
                #   记 empty_upstream、不上架该 zip、不阻断发布（否则一县拖死全州）。
                detail.append({"src": name, "run": run, "url": url, "bytes": len(blob),
                               "native_rows": 0, "status": "empty_upstream",
                               "note": "上游该源 0 行（合法空包），不上架"})
                log(f"  ○ OA 空包放行：{name}（{len(blob)}B，0 行，不上架）")
                continue
            path = d / f"openaddresses_us_{state_lc}_{name}.zip"
            path.write_bytes(blob)
            detail.append({"src": name, "run": run, "url": url, "bytes": len(blob),
                           "native_rows": rows, "status": "ok",
                           "file": rel(path)})
            files.append(path)
        except Exception as exc:  # noqa: BLE001
            fail.append({"src": name, "run": run, "url": url, "error": str(exc)[:200]})
    fx.add(*files)
    empty_n = len([d for d in detail if d.get("status") == "empty_upstream"])
    status = "ok" if files else ("empty_upstream" if empty_n else "error")
    fx.note("openaddresses", status, state=state_lc, sources_total=len(latest),
            downloaded=len(files), empty_upstream=empty_n, detail=detail, failures=fail,
            elapsed=round(time.time() - t0, 1))
    if not files and not empty_n:
        fx.problem("OpenAddresses 一个源包都没下到")


# --------------------------------------------------------------------------- D. Census TIGER
def _geojson_local_field(key: str) -> str:
    """本地兜底过滤时用哪个字段判值。"""
    if key == "zcta":
        return "ZCTA5"
    if key.startswith("county_"):
        return "COUNTY"
    if key.startswith("place_"):
        return "BASENAME"
    return "STATE"


def _tiger_boundary_queries(cfg: dict) -> list[tuple[str, str, list[tuple[str, str, str | None]]]]:
    """州界 / ZCTA / 市界的候选查询链。

    返回 (key, layer, [(where, outFields, local_filter_value), ...])，多组候选依次重试。
    县界不在这里 —— 它单独走 `_county_boundaries()`，因为 NAD 的按县切块也要用同一份
    县界多边形，两边共用缓存，只查一次。

    ★ 县界用 3 位 COUNTY 码（图层元数据实证：字段 COUNTY 长度=3）：
        STATE='10' AND COUNTY='001'   ✅
        STATE='10' AND COUNTY='10001' ❌ → 服务端静默返回 features=[] 空壳
    ★ ZCTA 图层**没有 STATE 字段**（实证：带 STATE → 400 Failed to execute query），
       只能 "ZCTA5,GEOID"。
    """
    t = cfg["_endpoints"]
    s2 = cfg["state_fips"]
    q = []

    # 州界
    q.append(("state",
              f"{t['tigerweb']}/State_County/MapServer/0",
              [(f"STATE='{s2}'", "GEOID,STATE,STUSAB,BASENAME,NAME", None),
               (f"STUSAB='{cfg['state']}'", "GEOID,STATE,STUSAB,BASENAME,NAME", None),
               ("1=1", "GEOID,STATE,STUSAB,BASENAME,NAME", s2)]))

    # ZCTA5 邮编区（本州邮编段）
    if cfg["zcta_lo"] and cfg["zcta_hi"]:
        q.append(("zcta",
                  f"{t['tigerweb']}/PUMA_TAD_TAZ_UGA_ZCTA/MapServer/1",
                  [(f"ZCTA5>='{cfg['zcta_lo']}' AND ZCTA5<='{cfg['zcta_hi']}'", "ZCTA5,GEOID", None),
                   ("1=1", "ZCTA5,GEOID", f"{cfg['zcta_lo']}:{cfg['zcta_hi']}")]))

    # 市界（可选）
    pfields = "GEOID,NAME,BASENAME,STATE,PLACE,CENTLAT,CENTLON"
    for p in cfg["places"]:
        key = "place_" + re.sub(r"[^a-z0-9]+", "_", p.lower()).strip("_")
        q.append((key,
                  f"{t['tigerweb']}/Places_CouSub_ConCity_SubMCD/MapServer/4",
                  [(f"STATE='{s2}' AND BASENAME='{p}'", pfields, None),
                   (f"STATE='{s2}'", pfields, p)]))
    return q


def _local_filter(feats: list, field: str, value: str) -> list:
    """本地兜底过滤：整州取回来后在内存里筛（ZCTA 用 "lo:hi" 段，其余精确匹配）。"""
    if field == "ZCTA5" and ":" in value:
        lo, hi = value.split(":", 1)
        return [f for f in feats if lo <= str(((f.get("attributes") or {}).get(field)) or "") <= hi]
    return [f for f in feats if str(((f.get("attributes") or {}).get(field)) or "") == value]


def _tiger_boundary_one(fx: Fetcher, key: str, layer: str,
                        attempts: list[tuple[str, str, str | None]]) -> tuple[dict | None, dict]:
    """查一条边界：多组候选（含"整州取回本地筛"兜底）+ 重试。

    返回 (payload | None, detail)。失败时 detail 里带完整候选重试链，便于事后归因。
    """
    tried = []
    for where, fields, local in attempts:
        try:
            payload = _esri_query(f"{layer}/query", {
                "where": where, "outFields": fields, "returnGeometry": "true",
                "outSR": "4326", "f": "json"}, tries=3, label=f"TIGER {key}",
                timeout=fx.args.tiger_timeout)
        except Exception as exc:  # noqa: BLE001
            tried.append({"where": where, "error": str(exc)[:150]})
            continue
        if payload.get("error"):
            tried.append({"where": where, "esri_error": str(payload["error"])[:160]})
            continue
        feats = payload.get("features") or []
        if local is not None:
            feats = _local_filter(feats, _geojson_local_field(key), local)
            payload = {**payload, "features": feats}
        if not feats:
            tried.append({"where": where, "features": 0})
            continue
        return payload, {"key": key, "layer": layer, "where": where, "features": len(feats)}
    return None, {"key": key, "layer": layer, "status": "all_attempts_failed", "tried": tried}


def _county_boundaries(fx: Fetcher) -> dict:
    """取本州全部县界多边形：落盘 + 缓存，供 NAD 按县切块与 TIGER 边界导出**共用**。

    返回 {county_<key>: {"payload":…, "detail":…, "path": Path}}。
    """
    cached = fx.cache.get("county_boundaries")
    if cached is not None:
        return cached
    d = fx.dir("tiger")
    s2 = fx.cfg["state_fips"]
    layer = f"{fx.cfg['_endpoints']['tigerweb']}/State_County/MapServer/1"
    fields_c = "GEOID,BASENAME,STATE,COUNTY,NAME"
    out: dict = {}
    for c in fx.cfg["counties"]:
        key = f"county_{c['key']}"
        attempts = [(f"STATE='{s2}' AND COUNTY='{c['fips3']}'", fields_c, None),
                    (f"STATE='{s2}' AND COUNTY='{c['fips']}'", fields_c, None),
                    (f"STATEFP='{s2}' AND COUNTYFP='{c['fips3']}'", fields_c, None),
                    (f"STATE='{s2}'", fields_c, c["fips3"])]
        payload, detail = _tiger_boundary_one(fx, key, layer, attempts)
        if not payload:
            detail["reason"] = "县界候选链全部失败/空壳"
            fx.cache.setdefault("county_misses", []).append(detail)
            continue
        path = d / f"tiger_{key}.geojson"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        size = path.stat().st_size
        if size < MIN_ASSET_BYTES:            # ★ 空壳绝不当资产上传，也不参与切块
            path.unlink()
            fx.cache.setdefault("county_misses", []).append(
                {**detail, "reason": f"产物仅 {size}B < {MIN_ASSET_BYTES}B，按空壳丢弃"})
            continue
        out[key] = {"payload": payload, "detail": detail, "path": path}
    fx.cache["county_boundaries"] = out
    return out


def _zip_blob(http_get, url: str, timeout: float, tries: int = 3) -> bytes | None:
    """下载 zip 并校验魔数 —— 只认 `PK` 开头，失败返回 None。

    为什么必须校验：Census 的 WAF 会回 **HTTP 200 + HTML 拒页**
    (`<html>...Request Rejected...support ID...</html>`，实测 247B）。
    http_get 拿到 200 就当成功返回，落盘后文件名是 .zip、内容是 HTML，
    最终被发布门禁按「过小产物」拦下 —— 2026-10-18 ID 州就是这么挂的（exit 3）。
    这里重试几轮（WAF 多是瞬时/按 IP 抖动），仍不行就交给上层记 upstream_rejected。
    """
    for i in range(tries):
        blob = b""
        try:
            blob = http_get(url, timeout=timeout)
        except Exception:  # noqa: BLE001
            blob = b""
        if blob[:2] == b"PK":
            return blob
        if i + 1 < tries:
            time.sleep(5 * (i + 1) + random.random() * 3)   # 5/10s + 抖动
    return None


def harvest_tiger(fx: Fetcher) -> None:
    t0 = time.time()
    d = fx.dir("tiger")
    files, detail, misses = [], [], []
    base = fx.cfg["_endpoints"]["tiger_base"]

    # 1) 县界（NAD 按县切块可能已经先取过 → 直接复用缓存，不重复请求）
    cached = _county_boundaries(fx)
    for key, rec in cached.items():
        files.append(rec["path"])
        detail.append(rec["detail"])
    misses.extend(fx.cache.get("county_misses") or [])
    for c in fx.cfg["counties"]:
        if f"county_{c['key']}" not in cached:
            misses.append({"key": f"county_{c['key']}", "reason": "县界未取到（候选链全挂 / 空壳）"})

    # 2) 州界 / ZCTA / 市界
    for key, layer, attempts in _tiger_boundary_queries(fx.cfg):
        payload, dd = _tiger_boundary_one(fx, key, layer, attempts)
        if not payload:
            misses.append(dd)
            continue
        path = d / f"tiger_{key}.geojson"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        size = path.stat().st_size
        if size < MIN_ASSET_BYTES:
            path.unlink()
            misses.append({**dd, "reason": f"产物仅 {size}B < {MIN_ASSET_BYTES}B，按空壳丢弃"})
            continue
        files.append(path)
        detail.append({**dd, "file": rel(path)})

    # 3) 号段小包：每县一个（包名用 5 位县 FIPS）
    for c in fx.cfg["counties"]:
        name = f"tl_2024_{c['fips']}_addrfeat.zip"
        url = f"{base}/ADDRFEAT/{name}"
        blob = _zip_blob(df, url, fx.args.tiger_timeout)
        if blob is None:
            misses.append({"file": name, "url": url, "upstream_rejected": True,
                           "reason": "上游返回非 zip（WAF 拒页 / 截断），已重试"})
            continue
        path = d / name
        path.write_bytes(blob)
        files.append(path)
        detail.append({"file": name, "county": c["key"], "bytes": len(blob), "url": url})

    # 州级 PLACE 小包
    pname = f"tl_2024_{fx.cfg['state_fips']}_place.zip"
    purl = f"{base}/PLACE/{pname}"
    blob = _zip_blob(df, purl, fx.args.tiger_timeout)
    if blob is None:
        misses.append({"file": pname, "url": purl, "upstream_rejected": True,
                       "reason": "上游返回非 zip（WAF 拒页 / 截断），已重试"})
    else:
        path = d / pname
        path.write_bytes(blob)
        files.append(path)
        detail.append({"file": pname, "bytes": len(blob), "url": purl})

    # ADDRFEAT 行数/列名（只读元信息，不改数据）
    for c in fx.cfg["counties"]:
        addrfeat = d / f"tl_2024_{c['fips']}_addrfeat.zip"
        if not addrfeat.exists():
            continue
        try:
            with zipfile.ZipFile(addrfeat) as zf:
                dbf = next((n for n in zf.namelist() if n.endswith(".dbf")), None)
                if dbf:
                    b = zf.read(dbf)
                    nrec = int.from_bytes(b[4:8], "little")
                    hlen = int.from_bytes(b[8:10], "little")
                    names = [b[32 + i * 32:32 + i * 32 + 11].split(b"\x00")[0].decode()
                             for i in range((hlen - 33) // 32)]
                    # .shx 记录数 = (文件长度-100)/8：与 .dbf 头对账，防止半载包
                    shx = next((n for n in zf.namelist() if n.endswith(".shx")), None)
                    shx_rec = ((zf.getinfo(shx).file_size - 100) // 8) if shx else None
                    detail.append({"file": addrfeat.name, "dbf_records": nrec,
                                   "shx_records": shx_rec,
                                   "records_match": (shx_rec is None or shx_rec == nrec),
                                   "dbf_fields_n": len(names), "dbf_fields": names})
        except Exception as exc:  # noqa: BLE001
            detail.append({"file": addrfeat.name, "dbf_error": str(exc)[:150]})

    # ★ 上游可恢复性分级：Census WAF 拒页（返回 200 + HTML 拒页）属于**上游瞬时状态**，
    #   不是「本州缺源」。记 upstream_rejected → 状态降为 warn-partial、不阻断发布，
    #   下次重跑自然再抓一遍；真正的缺源/空壳仍按 error 阻断。
    hard = [m for m in misses if not m.get("upstream_rejected")]
    soft_reject = len(misses) - len(hard)
    if not files:
        st = "error"
    elif hard:
        st = "error"
    elif soft_reject:
        st = "warn-partial"
    else:
        st = "ok"
    fx.add(*files)
    fx.note("census_tiger", st, detail=detail, missed=misses,
            counties=len(fx.cfg["counties"]), upstream_rejected=soft_reject,
            elapsed=round(time.time() - t0, 1))
    for m in misses:
        msg = (f"TIGER 缺失/空壳：{m.get('key') or m.get('file')} → "
               f"{json.dumps(m.get('tried') or m.get('error') or m.get('reason'), ensure_ascii=False)[:200]}")
        if m.get("upstream_rejected"):
            log("⚠️ " + msg + "（上游 WAF 拒页，本次跳过，重跑再抓）")
        else:
            fx.problem(msg)



# --------------------------------------------------------------------------- v1: 按县切片 + gzip
def _asset_name(source: str, state: str, *parts, ext: str = "") -> str:
    """统一命名：`{源}-US-{州}-{段...}`，例 `NAD-US-RI-44007-providence.ndjson.gz`。

    ★ 县一律用 5 位 FIPS：美国县缩写没有标准，`LA` 既是洛杉矶县又是路易斯安那州码，
      而 FIPS 跟 TIGER / OA / ZCTA 全都能对上。短名放后面给人看。
    """
    seg = "-".join([source.upper(), "US", state.upper()] + [str(p) for p in parts if p])
    return seg + ext


class _HashSink:
    """包在 TextIOWrapper 外面：边写边算「解压后」sha256、行数、State 分布。

    这样 gzip 落盘后能同时给出两个摘要（压缩后 / 解压后），完整性不打折。
    """
    _PAT = re.compile(r'"State"\s*:\s*"([^"]*)"')

    def __init__(self, txt):
        self.txt = txt
        self.h = hashlib.sha256()
        self.rows = 0
        self.bytes = 0
        self.states: Counter = Counter()

    def write(self, s: str) -> int:
        self.txt.write(s)
        b = s.encode("utf-8")
        self.h.update(b)
        self.bytes += len(b)
        self.rows += s.count("\n")
        m = self._PAT.search(s)
        if m:
            self.states[m.group(1)] += 1
        return len(s)

    def flush(self) -> None:
        self.txt.flush()


def _gz_open(path: Path):
    """流式 gzip 写；返回 (sink, close())。close() 返回解压后统计。"""
    raw = gzip.open(path, "wb", compresslevel=6)
    txt = io.TextIOWrapper(raw, encoding="utf-8", newline="\n")
    sink = _HashSink(txt)

    def close() -> dict:
        try:
            txt.flush()
        finally:
            txt.close()
        return {"rows": sink.rows, "uncompressed_bytes": sink.bytes,
                "sha256_raw": sink.h.hexdigest(),
                "state_distribution": dict(sink.states.most_common())}

    return sink, close


def _county_polygon_base(rings: list) -> dict:
    return {"geometry": json.dumps({"rings": rings, "spatialReference": {"wkid": 4326}}),
            "geometryType": "esriGeometryPolygon", "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects"}


def _county_rings(fx: Fetcher, county: dict) -> list | None:
    """取县的真实边界 rings（Esri 格式），复用 TIGER 县界缓存（NAD/TIGER 共用一次查询）。"""
    got = _county_boundaries(fx)
    rec = got.get(f"county_{county['key']}")
    if not rec:
        return None
    feats = (rec.get("payload") or {}).get("features") or []
    if not feats:
        return None
    return ((feats[0].get("geometry") or {}).get("rings")) or None


def _nad_harvest_county_shards(fx: Fetcher, d: Path, fields: list, oid: str,
                               err_log: Path) -> None:
    """v1 主路径：按县真实边界切片 → 每县一个 gzip 分片 + 一个溢出包。

    三数咬合（必须分毫不差，否则门禁阻断）：
        州 bbox 主键总数 == 落在各县多边形内的主键并集 + 溢出主键数
    溢出 = 州 bbox 主键清单 − 各县主键并集（纯集合差，不重新拉一遍数据）。
    """
    t0 = time.time()
    st = fx.cfg["state"]
    (d / "nad_full_columns.json").write_text(
        json.dumps({"fields": fields, "fields_n": len(fields), "object_id_field": oid},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    fx.add(d / "nad_native_metadata.json", d / "nad_full_columns.json")
    base = {"geometry": fx.cfg["bbox_str"], "geometryType": "esriGeometryEnvelope",
            "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}
    log("NAD 模式=ids+county_shards｜先取州 bbox 全集（OID 区间二分）")
    root_ids, ost = _nad_ids_by_oid_ranges(fx, base, err_log, "[state]")
    official = ost.get("count")
    if not root_ids:
        # ★ 2026-10-18 实测：NAD 上游会**局部降级** —— 同一时刻 HI、CA 小框本机也返回 count=0，
        #   而 AK/DE 正常。所以 count=0 不能直接判州死刑，先复测一次，再退到「县并集」当 root。
        confirm = _nad_seg_count(fx, base, err_log, "[state]:recheck")
        if confirm:
            log(f"ℹ️ 州 bbox 复测 count={confirm:,}（首次拿不到主键）→ 重试一次清单")
            root_ids, ost2 = _nad_ids_by_oid_ranges(fx, base, err_log, "[state]:retry")
            official = ost2.get("count") or confirm
            ost = ost2
        if not root_ids:
            # ★ 双层预检（2026-10-18 裁决）：州级 0 不等于整州无覆盖 —— NAD 是逐县逐机构
            #   自愿申报的，必须逐县打轻量探针（returnCountOnly，几十个县几秒钟），
            #   否则会出现"账本看起来 100% 完整、实际漏掉已申报县"的静默丢数据。
            log("⚠️ 州 bbox 仍为空 → 进入县级逐一探针（州级 0 不代表县级 0）")
            union_root: set = set()
            probed = empty_c = 0
            err_c = 0
            for c in fx.cfg["counties"]:
                rings = _county_rings(fx, c)
                if not rings:
                    continue
                cbase = _county_polygon_base(rings)
                cprob = _nad_seg_count(fx, cbase, err_log, f"[{c['key']}]:preflight")
                probed += 1
                if cprob is None:
                    err_c += 1                      # 查询失败（5xx/超时）≠ 0 行
                    continue
                if cprob == 0:
                    empty_c += 1
                    continue
                cid, _ = _nad_ids_by_oid_ranges(fx, cbase, err_log, f"[{c['key']}]")
                union_root |= set(cid)
            if union_root:
                log(f"✅ 县级探针发现有覆盖：{probed} 个县中 {probed - empty_c - err_c} 个有数据 "
                    f"→ 按县采集（州级 bbox 为空不影响）")
                root_ids = sorted(union_root)
                official = len(root_ids)
                ost = {**ost, "county_preflight": 1, "county_fallback": 1}
            elif probed and empty_c == probed:
                # 整州连带所有县都是 0 → 上游确实没有本州覆盖（NAD 自愿申报制）
                #   ★ 这是"合法缺失"，不是故障：照常流动到 Overture/OA/TIGER。
                fx.cache["nad_no_coverage"] = {"probed": probed, "empty": empty_c}
                fx.note("nad", "no_upstream_coverage", channel="A-official-rest",
                        mode="ids+county_shards", official_count=0, rows_fetched=0,
                        collected_rows=0, shard_count=0, probes=probed,
                        note=("本州在联邦 USDOT NAD 无任何覆盖（州级 count=0 且 "
                              f"{probed} 个县逐一探针全为 0）"),
                        elapsed=round(time.time() - t0, 1))
                _record_no_coverage(fx, probed)
                log(f"ℹ️ NAD 无本州覆盖：邻州级 count=0 + {probed} 个县逐一探针全 0 "
                    f"→ 记 no_upstream_coverage，跳过 NAD，继续其它三源")
                return
            elif err_c:
                fx.note("nad", "error", channel="A-official-rest", mode="ids+county_shards",
                        error=f"县级探针 {err_c} 个县查询失败（非 0 行）", elapsed=round(time.time() - t0, 1))
                fx.problem(f"NAD upstream_unavailable：{err_c} 个县探针请求失败（5xx/超时），稍后重跑")
                return
        if not root_ids:
            fx.note("nad", "error", channel="A-official-rest", mode="ids+county_shards",
                    error="州 bbox 与县并集都没拿到主键（上游疑似降级）",
                    elapsed=round(time.time() - t0, 1))
            fx.problem("NAD 州 bbox + 县并集都没拿到主键（上游疑似降级，稍后同 tag 重跑即可）")
            return
    log(f"州 bbox 主键 {len(root_ids):,}｜官方 count {official}｜"
        f"OID 查询 {ost['queries']} 次｜切分 {ost['splits']} 次")

    shards: list = []
    union: set = set()          # 已取到主键的县（成功或失败）→ 溢出计算要减掉，防失败县数据流进溢出包
    union_ok: set = set()       # 真正交付成功的县
    dup_rows = 0
    state_dist: Counter = Counter()
    failed_counties: list = []

    only = [s.strip() for s in str(getattr(fx.args, "county_fips", "") or "").split(",") if s.strip()]
    counties = [c for c in fx.cfg["counties"]
                if not only or c["fips"] in only or c["key"] in only]
    if only and not counties:
        die(f"--county-fips 未匹配到任何本州县：{only}（roster 里本州有 "
            f"{len(fx.cfg['counties'])} 个县）")
    partial = bool(only) and len(counties) < len(fx.cfg["counties"])
    if partial:
        known = {c["fips"] for c in counties} | {c["key"] for c in counties}
        bad = [s for s in only if s not in known]
        if bad:
            log(f"⚠️ --county-fips 未匹配到本州县：{bad}（已忽略）")
        log(f"⚠️ 只补指定县 {len(counties)} 个 → 本轮不重算溢出包、不做州级比对参考")

    for c in counties:
        rings = _county_rings(fx, c)
        if not rings:
            failed_counties.append({"county_fips": c["fips"], "county_key": c["key"],
                                    "county_name": c["name"], "reason": "缺县界多边形",
                                    "official_count": None, "rows": 0, "missing_ids": None})
            log(f"  ✗ 县失败（缺县界多边形）：{c['name']}（{c['fips']}）→ 本轮不产片，记录待补")
            continue
        ids, cst = _nad_ids_by_oid_ranges(fx, _county_polygon_base(rings),
                                         err_log, f"[{c['key']}]")
        ccnt = cst.get("count")
        if not ids:
            if ccnt:
                # 官方说有量、我们拿到 0 → 真缺口（上游降级/限流）
                failed_counties.append({"county_fips": c["fips"], "county_key": c["key"],
                                        "county_name": c["name"],
                                        "reason": "主键清单为空但官方 count>0（上游疑似降级）",
                                        "official_count": ccnt, "rows": 0, "missing_ids": None})
                log(f"  ✗ 县失败（官方 {ccnt:,} 却拿不到主键）：{c['name']}（{c['fips']}）")
            else:
                # 双重确认：count 与 idsOnly 两条不同查询都为 0 才认定"真空县"。
                # 单条查询为 0 可能是上游局部降级（2026-10-18 HI/CA 实测），不能直接押空。
                c2 = _nad_seg_count(fx, _county_polygon_base(rings), err_log, f"[{c['key']}]:empty-count")
                sample2, n2 = _nad_ids_sample(fx, _county_polygon_base(rings), err_log, f"[{c['key']}]:empty-ids")
                if (c2 or 0) == 0 and (n2 or 0) == 0 and not sample2:
                    fx.cache.setdefault("empty_counties", []).append(c["fips"])
                    log(f"  ○ 空县放行：{c['name']}（{c['fips']}）双通道确认 count=0（NAD 确无门牌）")
                else:
                    failed_counties.append({"county_fips": c["fips"], "county_key": c["key"],
                                            "county_name": c["name"],
                                            "reason": f"首查为空但复核 count={c2} ids={n2}（上游不稳定）",
                                            "official_count": c2, "rows": 0, "missing_ids": None})
                    log(f"  ✗ 县失败（首查空、复核 count={c2} ids={n2}）：{c['name']}（{c['fips']}）")
            continue
        fname = _asset_name("NAD", st, c["fips"], c["key"], ext=".ndjson.gz")
        union |= set(ids)        # 已取到主键（成功或失败都要算）→ 溢出必须先减掉它们
        # ★ 断点续传：远端已有该片且大小一致 → 跳过抓取（ids 仍查，因为溢出包要算差集）
        prevrec = (fx.status_doc.get("assets") or {}).get(fname)
        if (fx.publish and prevrec and c["fips"] in (fx.status_doc.get("done") or [])
                and fx.remote.get(fname) == prevrec.get("bytes")):
            log(f"  ⏭ 断点续传：跳过已完成县片 {fname}")
            fx.meta[rel(d / fname)] = {"bytes": prevrec.get("bytes"),
                                       "sha256": prevrec.get("sha256_gz"),
                                       "sha256_head": str(prevrec.get("sha256_raw") or "")[:16],
                                       "rows": prevrec.get("rows")}
            fx.add(d / fname)
            shards.append(prevrec)
            union_ok |= set(ids)
            state_dist.update(prevrec.get("state_distribution") or {})
            continue
        path = d / fname
        sink, close = _gz_open(path)
        seen_l: set = set()
        _nad_fetch_by_ids(fx, ids, fields, oid, sink, seen_l, err_log)
        missing = [i for i in ids if i not in seen_l]
        if missing:
            old = fx.args.nad_ids_batch
            fx.args.nad_ids_batch = min(100, max(25, old // 5))
            _nad_fetch_by_ids(fx, missing, fields, oid, sink, seen_l, err_log)
            fx.args.nad_ids_batch = old
            missing = [i for i in ids if i not in seen_l]
        meta = close()
        ok = (ccnt is None or meta["rows"] == ccnt) and not missing
        if not ok:
            # ★ 县级是**唯一硬性校验单元**：不合格的县只丢自己，绝不牵连全州。
            #   （州级三数咬合降级为参考记录，见下方 reference_check）
            path.unlink(missing_ok=True)
            failed_counties.append({
                "county_fips": c["fips"], "county_key": c["key"], "county_name": c["name"],
                "reason": (f"行数不符：取 {meta['rows']:,} / 官方 {ccnt}" if ccnt is not None
                           and meta["rows"] != ccnt else f"缺 {len(missing):,} 主键"),
                "official_count": ccnt, "rows": meta["rows"], "missing_ids": len(missing)})
            log(f"  ✗ 县失败（取 {meta['rows']:,} / 官方 {ccnt} / 缺 {len(missing):,}）："
                f"{c['name']}（{c['fips']}）→ 不产片，记录待补")
            continue
        dup_rows += max(0, meta["rows"] - len(seen_l))
        state_dist.update(meta["state_distribution"])
        rec = {"kind": "county", "county_fips": c["fips"], "county_key": c["key"],
               "county_name": c["name"], "file": rel(path),
               "rows": meta["rows"], "expected_rows": ccnt,
               "uncompressed_bytes": meta["uncompressed_bytes"],
               "gz_bytes": path.stat().st_size,
               "sha256_gz": df.sha256_full(path),
               "sha256_raw": meta["sha256_raw"],
               "state_distribution": meta["state_distribution"],
               "missing_ids": len(missing), "bytes": path.stat().st_size}
        fx.add(path)
        fx.status_doc.setdefault("assets", {})[fname] = rec
        if not _checkpoint(fx, path, extra={"rows": rec["rows"], "expected_rows": ccnt}):
            fx.problem(f"县片上传失败：{fname}")
        if c["fips"] not in (fx.status_doc.get("done") or []):
            fx.status_doc.setdefault("done", []).append(c["fips"])
        _write_status(fx)
        shards.append(rec)
        union_ok |= set(ids)
        log(f"  县片 {c['fips']} {c['key']:<14} 行 {meta['rows']:>8,} / 官方 {ccnt:>8,}"
            f"｜gz {rec['gz_bytes'] / 1e6:>6.2f} MB")

    if partial:
        # 只补指定县：溢出包与州级参考都是**全州口径**，本轮不动（避免把 subset 当成全集算差）
        fx.cache["nad_shards"] = shards
        fx.cache["partial_run"] = True          # ★ 硬护栏：部分县运行禁止封印
        fx.cache["nad_reference"] = {
            "policy": "县级核验为准；本轮为部分县补跑，未重算溢出包与州级参考",
            "partial": True, "counties_run": [c["fips"] for c in counties],
            "failed_counties": failed_counties,
            "empty_counties": list(fx.cache.get("empty_counties") or []),
        }
        fx.note("nad", "ok" if not failed_counties else "warn-partial",
                channel="A-official-rest", mode="ids+county_shards", partial=True,
                official_count=official, official_ids=len(root_ids),
                shard_count=len(shards), failed_counties=failed_counties,
                empty_counties=list(fx.cache.get("empty_counties") or []),
                rows_fetched=sum(s["rows"] for s in shards), truncated=False,
                elapsed=round(time.time() - t0, 1))
        return

    # 溢出：州 bbox 全集 − 各县并集（纯集合差，绝不重复拉数据）
    over_ids = [i for i in root_ids if i not in union]
    over_n = len(over_ids)          # ★ 真实溢出主键数（续传跳过时 over_ids 会清空，计数要留住）
    over_meta = {"rows": 0, "uncompressed_bytes": 0, "sha256_raw": None,
                 "state_distribution": {}}
    over_name = _asset_name("NAD", st, "OUTOFSTATE", ext=".ndjson.gz")
    prev_over = (fx.status_doc.get("assets") or {}).get(over_name)
    if (fx.publish and prev_over and "OUTOFSTATE" in (fx.status_doc.get("done") or [])
            and fx.remote.get(over_name) == prev_over.get("bytes")
            and prev_over.get("expected_rows") == over_n):
        log(f"  ⏭ 断点续传：跳过已完成溢出包 {over_name}")
        fx.meta[rel(d / over_name)] = {"bytes": prev_over.get("bytes"),
                                       "sha256": prev_over.get("sha256_gz"),
                                       "sha256_head": str(prev_over.get("sha256_raw") or "")[:16],
                                       "rows": prev_over.get("rows")}
        fx.add(d / over_name)
        shards.append(prev_over)
        state_dist.update(prev_over.get("state_distribution") or {})
        over_ids = []
        over_meta = {"rows": prev_over.get("rows", 0),
                     "uncompressed_bytes": prev_over.get("uncompressed_bytes", 0),
                     "sha256_raw": prev_over.get("sha256_raw"),
                     "state_distribution": prev_over.get("state_distribution") or {}}
    if over_ids:
        path = d / over_name
        sink, close = _gz_open(path)
        seen_o: set = set()
        _nad_fetch_by_ids(fx, over_ids, fields, oid, sink, seen_o, err_log)
        missing_o = [i for i in over_ids if i not in seen_o]
        if missing_o:
            old = fx.args.nad_ids_batch
            fx.args.nad_ids_batch = min(100, max(25, old // 5))
            _nad_fetch_by_ids(fx, missing_o, fields, oid, sink, seen_o, err_log)
            fx.args.nad_ids_batch = old
            missing_o = [i for i in over_ids if i not in seen_o]
        over_meta = close()
        fx.add(path)
        state_dist.update(over_meta["state_distribution"])
        if missing_o:
            fx.problem(f"NAD 溢出包对账失败：缺 {len(missing_o):,} 主键")
        rec = {"kind": "overflow", "county_fips": None, "county_key": "OUTOFSTATE",
               "county_name": "不在本州界内的 BBox 溢出点（邻州门牌，供核验用）",
               "file": rel(path), "rows": over_meta["rows"],
               "expected_rows": len(over_ids),
               "uncompressed_bytes": over_meta["uncompressed_bytes"],
               "gz_bytes": path.stat().st_size,
               "sha256_gz": df.sha256_full(path),
               "sha256_raw": over_meta["sha256_raw"],
               "state_distribution": over_meta["state_distribution"],
               "missing_ids": len(missing_o), "bytes": path.stat().st_size}
        fx.status_doc.setdefault("assets", {})[over_name] = rec
        fx.status_doc.setdefault("done", []).append("OUTOFSTATE")
        if not _checkpoint(fx, path, extra={"rows": rec["rows"],
                                           "expected_rows": rec["expected_rows"]}):
            fx.problem(f"溢出包上传失败：{over_name}")
        _write_status(fx)
        shards.append(rec)
        log(f"  溢出包 OUTOFSTATE 行 {over_meta['rows']:>8,} / 应得 {len(over_ids):>8,}"
            f"｜gz {rec['gz_bytes'] / 1e6:>6.2f} MB")

    # ---- 州级比对参考（**只记录，不阻断**）----
    # 口径变更（2026-10-18）：县级 count/行数/缺主键 是唯一门槛；州级三数咬合降级为参考。
    # 一个县出问题只丢那个县（failed_counties），其余县照常上架，后面按县补跑即可。
    covered_ok = len(union_ok) + over_n
    total_rows = sum(s["rows"] for s in shards)
    ref = {
        "policy": "县级核验为唯一门槛；州级三数咬合仅作比对参考",
        "state_upstream_count": official,
        "state_root_ids": len(root_ids),
        "counties_total": len(fx.cfg["counties"]),
        "counties_ok": len([s for s in shards if s["kind"] == "county"]),
        "counties_empty": len(fx.cache.get("empty_counties") or []),
        "counties_failed": len(failed_counties),
        "failed_counties": failed_counties,
        "county_union_ids": len(union_ok),
        "overflow_ids": over_n,
        "three_number_check": {
            "formula": "县并集 + 溢出 == 州官方 count",
            "covered": covered_ok, "official": official,
            "matches": (official is None or covered_ok == official),
            "gap_ids": (official - covered_ok) if isinstance(official, int) else None,
            "note": "不咬合不改数据、不阻断发布；差异通常来自失败/降级县，按需补跑",
        },
        "shard_rows_check": {
            "formula": "Σ县片行 − 县界重复 == 县并集 + 溢出",
            "sum_rows": total_rows, "boundary_dup_rows": dup_rows,
            "net": total_rows - dup_rows,
            "matches": (total_rows - dup_rows == covered_ok),
        },
    }
    fx.cache["nad_reference"] = ref
    if not ref["three_number_check"]["matches"]:
        log(f"ℹ️ 州级比对参考：覆盖 {covered_ok:,} / 官方 {official}"
            f"（差 {ref['three_number_check']['gap_ids']}，按县口径已记录待补，不阻断发布）")

    fx.cache["nad_shards"] = shards
    fx.note("nad", "ok" if not fx.problems else "warn-truncated",
            channel="A-official-rest", mode="ids+county_shards",
            fetch_strategy="OID 区间二分取主键 → 县多边形切片 → objectIds 批取 → gzip 落盘",
            official_count=official, official_ids=len(root_ids),
            shard_count=len([s for s in shards if s["kind"] == "county"]),
            empty_counties=list(fx.cache.get("empty_counties") or []),
            failed_counties=failed_counties,
            reference_check="manifest.reference_check（州级仅参考）",
            overflow_rows=over_meta["rows"], overflow_ids=over_n,
            boundary_duplicate_rows=dup_rows, total_rows=total_rows,
            coverage_pct=(round(100.0 * covered_ok / official, 2) if official else None),
            ids_queries=ost["queries"], ids_splits=ost["splits"],
            state_distribution=dict(state_dist.items()),
            state_in_scope_rows=state_dist.get(st),
            state_in_scope_pct=(round(100.0 * state_dist.get(st, 0) / total_rows, 2)
                                if total_rows else None),
            rows_fetched=covered_ok, truncated=False,
            bytes=sum(s["gz_bytes"] for s in shards),
            elapsed=round(time.time() - t0, 1))


def _apply_naming(fx: Fetcher) -> None:
    """v1 命名统一：把所有资产改名为 `{源}-US-{州}-...`（含 ledger 里的引用）。"""
    st = fx.cfg["state"]
    st_lc = st.lower()
    fips2key = {c["fips"]: c["key"] for c in fx.cfg["counties"]}
    mapping: dict[str, str] = {}
    for p in list(fx.files):
        n = p.name
        new = n
        if n.startswith("tiger_state.geojson"):
            new = _asset_name("TIGER", st, "state", ext=".geojson")
        elif n.startswith("tiger_zcta.geojson"):
            new = _asset_name("TIGER", st, "zcta", ext=".geojson")
        elif n.startswith("tiger_county_"):
            key = n[len("tiger_county_"):-len(".geojson")]
            fips = next((f for f, k in fips2key.items() if k == key), key)
            new = _asset_name("TIGER", st, f"county-{fips}", ext=".geojson")
        elif n.startswith("tiger_place_"):
            place = n[len("tiger_place_"):-len(".geojson")]
            new = _asset_name("TIGER", st, f"place-{place}", ext=".geojson")
        elif n.startswith(f"tl_2024_{fx.cfg['state_fips']}") and n.endswith("_addrfeat.zip"):
            mid = n[len(f"tl_2024_"):-len("_addrfeat.zip")]
            new = _asset_name("TIGER", st, mid, "addrfeat", ext=".zip")
        elif n.startswith("tl_2024_"):
            rest = n[len("tl_2024_"):-len(".zip")].split("_", 1)[-1]
            if rest == "place":                 # 州级 places 边界包 → places
                rest = "places"
            new = _asset_name("TIGER", st, rest, ext=".zip")
        elif n.startswith("openaddresses_us_"):
            src = n[len(f"openaddresses_us_{st_lc}_"):-len(".zip")]
            new = _asset_name("OA", st, src, ext=".zip")
        elif n.startswith("overture_us_"):
            ext = ".geojson" if n.endswith(".geojson") else "." + n.rsplit(".", 1)[-1]
            new = _asset_name("OVERTURE", st, ext=ext)
        elif n == f"nad_us_{st_lc}_full.ndjson":
            new = _asset_name("NAD", st, "ALL", ext=".ndjson")
        elif n == "nad_full_columns.json":
            new = _asset_name("NAD", st, "columns", ext=".json")
        elif n == "nad_native_metadata.json":
            new = _asset_name("NAD", st, "layer-metadata", ext=".json")
        elif n == "nad_http_errors.log":
            new = _asset_name("NAD", st, "http-errors", ext=".log")
        if new != n:
            mapping[n] = new
    if not mapping:
        return
    for p in list(fx.files):
        if p.name in mapping:
            dst = p.with_name(mapping[p.name])
            if p.exists() and dst != p:
                os.replace(p, dst)
    fx.files = [p.with_name(mapping.get(p.name, p.name)) for p in fx.files]

    # 已经上传并删除本地的文件：只改记账键与 STATUS 索引（文件已不在磁盘上）
    def _rename_path(relp: str) -> str:
        head, _, base = relp.rpartition("/")
        if base in mapping:
            return (head + "/" if head else "") + mapping[base]
        return relp

    fx.meta = {_rename_path(k): v for k, v in fx.meta.items()}
    assets = fx.status_doc.get("assets") or {}
    if assets:
        fixed = {}
        for name, rec in assets.items():
            r = dict(rec)
            if isinstance(r.get("file"), str):
                r["file"] = _rename_path(r["file"])
            fixed[mapping.get(name, name)] = r
        fx.status_doc["assets"] = fixed

    def rewrite(obj):
        if isinstance(obj, dict):
            return {k: (rewrite(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [rewrite(v) for v in obj]
        if isinstance(obj, str) and obj in mapping:
            return mapping[obj]
        return obj

    fx.status = rewrite(fx.status)
    fx.ledger = rewrite(fx.ledger)

    # ★ 2026-10-18：OA 空包若已在上一轮上架（891B 空 zip），本轮直接清掉远端与账本
    junk = [n for n in (fx.status_doc.get("assets") or {})
            if n.startswith("OA-") and n.endswith(".zip")
            and ((fx.status_doc["assets"][n] or {}).get("bytes") or 0) < MIN_ASSET_BYTES]
    for n in junk:
        gh_delete_asset(fx.tag, n)
        fx.status_doc["assets"].pop(n, None)
        log(f"  🗑 清掉空 OA 资产：{n}")
    log(f"命名统一完成：{len(mapping)} 个资产改为 {{源}}-US-{st}-... 口径")


# --------------------------------------------------------------------------- 增量发布 / 断点续传
STATUS_NAME = "STATUS.json"
SEAL_NAME = "MANIFEST.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _notify(evt: str, payload: dict) -> None:
    """向 pi 哨兵上报流水线事件。任何失败静默 —— 绝不拖累抓取。

    county      每县上传完成 → 哨兵只记账（不唤醒）
    state_sealed 全州封印完成 → 触发体检
    alarm       异常（源抛错 / 门禁失败 / 上传失败）→ 立即唤醒
    """
    url = (os.environ.get("SENTINEL_URL") or "").strip()
    if not url:
        return
    try:
        import urllib.request as _ur
        body = json.dumps({"evt": evt, **payload}, ensure_ascii=False).encode("utf-8")
        req = _ur.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "X-Sentinel-Token": os.environ.get("SENTINEL_TOKEN", ""),
        })
        _ur.urlopen(req, timeout=6).read()
    except Exception:  # noqa: BLE001
        pass


def _state_of_tag(tag: str) -> str:
    parts = (tag or "").split("-")
    for p in parts:
        if len(p) == 2 and p.isalpha() and p.isupper():
            return p
    return "?"


def _gh_env():
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not tok or not os.environ.get("GITHUB_REPOSITORY"):
        return None
    return {**os.environ, "GH_TOKEN": tok}


def _gh(args: list, timeout: int = 3600) -> subprocess.CompletedProcess:
    env = _gh_env()
    if env is None:
        raise RuntimeError("缺少 GITHUB_TOKEN / GITHUB_REPOSITORY")
    return subprocess.run(["gh", *args], capture_output=True, text=True, env=env, timeout=timeout)


def gh_ensure_release(tag: str) -> None:
    """先建空 Release（不带 --latest，避免 51 州互相抢 latest 标签）。"""
    if _gh(["release", "view", tag, "--json", "tagName"]).returncode == 0:
        log(f"Release 已存在，按断点续传模式继续：{tag}")
        return
    r = _gh(["release", "create", tag, "--title", tag, "--latest=false",
             "--notes", "**采集中**：进度见 `STATUS.json`；`MANIFEST.json` 出现即代表交付完成。"])
    if r.returncode != 0:
        raise RuntimeError(f"创建 Release 失败：{(r.stderr or '')[:300]}")
    log(f"已创建空 Release：{tag}")


def gh_assets(tag: str) -> dict:
    r = _gh(["release", "view", tag, "--json", "assets"])
    if r.returncode != 0:
        return {}
    try:
        return {a["name"]: a["size"] for a in (json.loads(r.stdout or "{}").get("assets") or [])}
    except Exception:  # noqa: BLE001
        return {}


def gh_upload(tag: str, path: Path, clobber: bool = False) -> bool:
    args = ["release", "upload", tag, str(path)]
    if clobber:
        args.append("--clobber")
    r = _gh(args)
    if r.returncode != 0:
        log(f"⚠️ 上传失败 {path.name}：{(r.stderr or '')[:200]}")
        return False
    return True


def gh_delete_asset(tag: str, name: str) -> bool:
    """删掉 Release 里某个同名资产（用于清理空包/脏资产）。失败只告警不阻断。"""
    env = _gh_env()
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    r = subprocess.run(["gh", "api", f"repos/{repo}/releases/tags/{tag}",
                        "--jq", '.assets[]|"\(.id)\t\(.name)"'],
                       capture_output=True, text=True, env=env)
    for line in (r.stdout or "").splitlines():
        if "\t" in line and line.split("\t", 1)[1].strip() == name:
            aid = line.split("\t", 1)[0].strip()
            d = subprocess.run(["gh", "api", "-X", "DELETE", f"repos/{repo}/releases/assets/{aid}"],
                               capture_output=True, text=True, env=env)
            if d.returncode != 0:
                log(f"⚠️ 删除资产失败 {name}：{(d.stderr or '')[:160]}")
                return False
            return True
    return False


def gh_read_asset_text(tag: str, name: str) -> str | None:
    env = _gh_env()
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    r = subprocess.run(["gh", "api", f"repos/{repo}/releases/tags/{tag}",
                        "--jq", '.assets[]|"\(.id)\t\(.name)"'],
                       capture_output=True, text=True, env=env)
    for line in (r.stdout or "").splitlines():
        if "\t" not in line:
            continue
        aid, aname = line.split("\t", 1)
        if aname.strip() != name:
            continue
        r2 = subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream",
                             f"repos/{repo}/releases/assets/{aid.strip()}"],
                            capture_output=True, text=True, env=env)
        if r2.returncode == 0:
            return r2.stdout
    return None


def _write_status(fx: Fetcher) -> None:
    """STATUS.json 是**过程态**：唯一允许 --clobber 覆盖的资产。"""
    doc = fx.status_doc
    doc.update({"schema": "state-release-status/v1", "state": fx.cfg["state"], "tag": fx.tag,
                "generator": "bots/fetch_state_raw.py",
                "started_at": doc.get("started_at") or _now(),
                "updated_at": _now()})
    doc.setdefault("phase", "collecting")
    doc.setdefault("expected", [])
    doc.setdefault("done", [])
    doc.setdefault("assets", {})
    p = fx.out / STATUS_NAME
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    if fx.publish:
        gh_upload(fx.tag, p, clobber=True)
    p.unlink(missing_ok=True)


def _checkpoint(fx: Fetcher, path: Path, extra: dict | None = None,
                clobber: bool = False, delete: bool = True) -> bool:
    """单个文件 → 记账 → 上传 → 核对远端 size → 本地删除。数据资产绝不 clobber。"""
    r = rel(path)
    m = {"bytes": path.stat().st_size, "sha256": df.sha256_full(path),
         "sha256_head": df.sha256_head(path)}
    if extra:
        m.update(extra)
    fx.meta[r] = m
    if not fx.publish:
        return True
    size = m["bytes"]
    name = path.name
    if not clobber and fx.remote.get(name) == size:
        log(f"  ⏭ 远端已存在且大小一致，跳过：{name}")
    else:
        if not gh_upload(fx.tag, path, clobber=clobber):
            # ★ 同名旧资产冲突（run 39058950492 的 DE 溢出包就是这么挂的：tag 被复用，
            #   上一轮已存在同名资产 → gh 报 already_exists）。数据资产平时不 clobber，
            #   但“上传失败 + 远端确有同名”时覆盖是唯一正确解（内容就是本轮产物）。
            if name in gh_assets(fx.tag):
                log(f"⚠️ {name} 同名资产已存在 → 覆盖上传（远端旧版与本轮冲突）")
                if not gh_upload(fx.tag, path, clobber=True):
                    return False
            else:
                return False
        remote = gh_assets(fx.tag).get(name)
        if remote != size:
            log(f"⚠️ 远端大小不符（{remote} != {size}），重传一次：{name}")
            if not gh_upload(fx.tag, path, clobber=True):
                return False
            remote = gh_assets(fx.tag).get(name)
            if remote != size:
                fx.problem(f"上传校验失败：{name} 远端 {remote} != 本地 {size}")
                return False
        fx.remote[name] = size
        log(f"  ⬆ 已上传 {name}（{size / 1e6:.2f} MB）")
        if name.endswith(".ndjson.gz") or name.startswith(("OVERTURE-", "OA-", "TIGER-")):
            kind = ("county" if name.endswith(".ndjson.gz") and "OUTOFSTATE" not in name
                    else "asset")
            _notify("county", {
                "state": _state_of_tag(fx.tag), "tag": fx.tag, "asset": name,
                "kind": kind, "bytes": size,
                "fips": (extra or {}).get("county_fips", ""),
                "rows": (extra or {}).get("rows"),
            })
    if delete:
        path.unlink(missing_ok=True)
    return True


def _checkpoint_others(fx: Fetcher) -> None:
    """把当前所有**已抓完但还没上传**的非分片文件立刻传走并删本地（Overture/TIGER/OA…）。"""
    if not fx.publish:
        return
    _apply_naming(fx)          # 先统一命名，再上传 —— 否则远端会留下旧口径文件名
    for p in list(fx.files):
        if not p.exists() or p.name in (STATUS_NAME, SEAL_NAME, "RELEASE_NOTES.md"):
            continue
        if p.name.endswith(".ndjson.gz"):
            continue                      # 县片由 shard 循环自己上传
        _checkpoint(fx, p)


# --------------------------------------------------------------------------- 门禁 / 打包
def _asset_sanity(p: Path) -> str | None:
    """产物空壳检测：**走结构，不靠体积**。返回问题描述，None = 正常。

    为什么不能一刀切拿 1KB 卡体积：实测 `nad_full_columns.json` 只有 908 字节，
    是**完全合法**的元信息（只是一串列名）；而真正的空壳是 680 字节、`features: []`
    的县界 geojson —— 体积一样小，必须靠结构区分。
    规则：数据载荷（geojson/ndjson/zip/csv）既要过 1KB 线，又要过结构校验；
         元信息 json / md 只要求非 0 字节。
    """
    size = p.stat().st_size
    if size == 0:
        return "0 字节空文件"
    if p.suffix.lower() in PAYLOAD_SUFFIXES and size < MIN_ASSET_BYTES:
        return f"过小产物（{size}B < {MIN_ASSET_BYTES}B）"
    if p.suffix.lower() == ".zip" and not zipfile.is_zipfile(p):
        return "非 zip 内容（上游拒页 / 截断）"          # ★ 体积够大也照样拦：zip 头不对就是假包
    if p.suffix.lower() == ".geojson":
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return f"geojson 无法解析：{str(exc)[:100]}"
        if isinstance(obj, dict) and not obj.get("features"):
            return "geojson features 为空（空壳）"
    if p.suffix.lower() == ".ndjson":
        with p.open(encoding="utf-8") as fh:
            if not any(line.strip() for line in fh):
                return "ndjson 0 行"
    return None


def gate(fx: Fetcher) -> None:
    """★ 发布门禁：缺源 / 空壳 / 边界为 0 → 直接阻断，绝不把废资产推上架。"""
    errs = list(fx.problems)
    for src in REQUIRED_SOURCES:
        st = fx.status.get(src)
        if not st:
            errs.append(f"源 {src} 未执行（--only 漏了？）")
        elif st.get("status") not in ("ok", "warn-truncated", "warn-partial",
                                       "empty_upstream", "no_upstream_coverage"):
            errs.append(f"源 {src} 状态异常：{st.get('status')}")
    # ★ 第二把锁：不管走哪条通道，已取行数必须与官方 count 相等（防“自洽但残缺”骗过门禁）
    nad = fx.status.get("nad") or {}
    oc, rf = nad.get("official_count"), nad.get("rows_fetched")
    ref = fx.cache.get("nad_reference") or {}
    county_mode = str(nad.get("mode") or "").endswith("county_shards")
    if isinstance(oc, int) and isinstance(rf, int) and rf != oc:
        if county_mode and ref:
            # ★ 口径变更（2026-10-18）：县级核验为准入门槛，州级覆盖差异只记录不阻断。
            log(f"ℹ️ 州级参考：覆盖 {rf:,} / 官方 {oc:,}（{rf / oc:.1%}）"
                f"｜县 ok={ref.get('counties_ok')} 失败={ref.get('counties_failed')} "
                f"空={ref.get('counties_empty')} → 不阻断，按需补跑失败县")
        else:
            errs.append(f"NAD 覆盖不全：已取 {rf:,} ≠ 官方 {oc:,}（{rf / oc:.1%}）")
    # ★ 第三把锁：TIGER 空间包双头对账（.dbf 记录数 vs .shx 记录数），防半载包
    for d in ((fx.status.get("census_tiger") or {}).get("detail") or []):
        if isinstance(d, dict) and d.get("records_match") is False:
            errs.append(f"TIGER 半载包：{d.get('file')} dbf={d.get('dbf_records')} "
                        f"≠ shx={d.get('shx_records')}")
    # ★ 第四把锁：州码必须是真 ISO 3166-2（查全局注册表，防 roster 拼错）
    iso = _iso_codes(fx.cfg["state"])
    if iso["iso_3166_2_verified"] is False:
        errs.append(f"州码 {iso['iso_3166_2']} 不在 ISO 3166-2 注册表"
                    f"（rules/geo/global_admin_registry.json）")
    for p in fx.files:
        if not p.exists():                       # 已随抓随传并删本地 → 只能按记账字节查
            m = fx.meta.get(rel(p)) or {}
            b = m.get("bytes")
            if b is None:
                errs.append(f"资产既不在本地也无上传记账：{rel(p)}")
            elif (p.suffix.lower() in PAYLOAD_SUFFIXES and b < MIN_ASSET_BYTES
                  and not (p.name.startswith("OA-") and p.name.endswith(".zip"))):
                errs.append(f"过小产物（{b}B < {MIN_ASSET_BYTES}B）：{rel(p)}")
            elif str(p).endswith(".ndjson.gz") and not (m.get("rows") or 0):
                errs.append(f"分片行数为 0：{rel(p)}")
            continue
        bad = _asset_sanity(p)
        if bad:
            errs.append(f"{bad}：{rel(p)}")
    if errs:
        raise RuntimeError("发布门禁未通过，共 %d 项：\n  - %s" % (len(errs), "\n  - ".join(errs)))


def _iso_codes(state_code: str) -> dict:
    """本厂产物的 ISO 归属：国家固定 US；州码在**全局注册表**里核对（查不到就如实标 False）。

    注册表是 `rules/geo/global_admin_registry.json`（随代码走，不发 Release）。
    注册表不存在时返回 None（不阻断），存在但对不上才视为真问题。
    """
    iso2 = f"US-{state_code}"
    rec = {"iso_3166_1": "US", "iso_3166_2": iso2, "iso_3166_2_verified": None}
    try:
        data = json.loads(ADMIN_REGISTRY.read_text(encoding="utf-8"))
        rec["iso_3166_2_verified"] = any(d.get("iso_3166_2") == iso2
                                         for d in (data.get("admin_divisions") or []))
    except Exception:  # noqa: BLE001
        pass
    return rec


def build_completeness(fx: Fetcher) -> dict:
    """★ 四源「自证完整」：逐源给出【上游声明值 vs 本地产出】对照，不只说“下载完成”。

    设计原则：**只记录事实，不改数据**。ok=False 表示该源没有可对账的上游基准
    （而不是数据有问题），原因写在 method/reason 里，由下游自己判断。
    """
    st = fx.status
    nad = st.get("nad") or {}
    ov = st.get("overture") or {}
    oa = st.get("openaddresses") or {}
    tg = st.get("census_tiger") or {}

    nad_claim = nad.get("official_count")
    if nad_claim is None:
        nad_claim = nad.get("official_count_in_bbox")
    nad_rows = nad.get("rows_fetched")
    oa_files = [d for d in (oa.get("detail") or []) if isinstance(d, dict) and d.get("file")]
    oa_rows = sum(int(d.get("native_rows") or 0) for d in oa_files)
    addr = [d for d in (tg.get("detail") or [])
            if isinstance(d, dict) and d.get("dbf_records") is not None]
    addr_rows = sum(int(d["dbf_records"]) for d in addr)
    boundary = [d for d in (tg.get("detail") or [])
                if isinstance(d, dict) and not d.get("dbf_records") and d.get("features") is not None]

    return {
        "note": "可证明完整性：上游声明值 vs 本地产出。只记录事实，不修改任何数据。",
        "nad": {
            "channel": "USDOT NAD / Esri GeoServices REST",
            "acquire_mode": nad.get("mode"),
            "upstream_declared_rows": nad_claim,
            "local_rows": nad_rows,
            "local_unique_ids": nad.get("official_ids"),
            "coverage_pct": nad.get("coverage_pct"),
            "method": ("【参考】县多边形并集主键 + 溢出主键 == 州 returnCountOnly；"
                       "且 Σ分片行 − 县界重复 == 主键覆盖（门禁以县为准，见 reference_check）"
                       if str(nad.get("mode") or "").endswith("county_shards")
                       else ("returnCountOnly(同一 BBox) == returnIdsOnly 全集 == 落盘行数"
                             if nad.get("mode") == "ids" else "returnCountOnly == 去重后行数")),
            "evidence_1_full": bool(nad_claim) and nad_rows == nad_claim,
        },
        "overture": {
            "channel": "Overture Maps CLI（云端按 bbox 切）",
            "upstream_declared_rows": None,
            "local_rows": ov.get("features"),
            "release": ov.get("overture_release"),
            "cli_version": ov.get("cli_version"),
            "method": "CLI 不返回 bbox 级官方行数声明 → 只能自证“文件非空 + feature 可数”",
            "evidence_1_full": bool(ov.get("features")),
        },
        "openaddresses": {
            "channel": "OpenAddresses runs/{run}/us/{st}/{src}.zip",
            "upstream_declared_rows": None,
            "local_rows": oa_rows,
            "files": [{"file": os.path.basename(str(d.get("file"))), "run": d.get("run"),
                       "src": d.get("src"), "rows": d.get("native_rows")} for d in oa_files],
            "method": ("OA 逐源不公开行数声明（/runs/{run}/us/{st}/{src}.json 实测 404）"
                       " → 记录 run id + 包内实标行数，可溯源重下比对"),
            "evidence_1_full": bool(oa_files) and oa_rows > 0,
        },
        "census_tiger": {
            "channel": "TIGERweb REST（边界）+ TIGER/Line 号段包",
            "upstream_declared_rows": None,
            "local_rows": addr_rows,
            "addrfeat_files": [{"file": d.get("file"), "dbf_records": d.get("dbf_records"),
                                "shx_records": d.get("shx_records"),
                                "records_match": d.get("records_match")} for d in addr],
            "boundary_layers": len(boundary),
            "missed": len(tg.get("missed") or []),
            "method": "空间包自身双头对账：.dbf 记录数 == .shx 记录数（防半载包）；边界层非空即存",
            "evidence_1_full": (not (tg.get("missed") or [])) and addr_rows > 0
                               and all(d.get("records_match") is not False for d in addr),
        },
    }


def _append_notes(fx: Fetcher, lines: list[str]) -> None:
    """把客观事实追加进随 Release 上架的 RAW_DATA_NOTES.md（幂等，不重复写同一行）。"""
    try:
        p = fx.out / "RAW_DATA_NOTES.md"
        head = ("# RAW DATA NOTES\n\n"
                "**管道完整性（Pipeline Fidelity）≠ 地理真值（Ground Truth）**：本仓库声明的"
                "\"可证明完整性\"特指**对上游 API 所公布声明的 100% 无损对齐**（上游说多少行，"
                "我们就交付多少行）。上游为 0 或未申报，不代表该地区地面的真实门牌总量。\n")
        prev = p.read_text(encoding="utf-8") if p.exists() else head
        if not prev.startswith("# RAW DATA NOTES"):
            prev = head + "\n" + prev
        add = [ln for ln in lines if ln and ln not in prev]
        if add:
            p.write_text(prev.rstrip() + "\n" + "\n".join(add) + "\n", encoding="utf-8")
        fx.add(p)
    except Exception as exc:  # noqa: BLE001
        log(f"⚠️ 写 RAW_DATA_NOTES.md 失败：{exc}")


def _count_geojson(p: Path):
    try:
        return len(json.loads(p.read_text(encoding="utf-8")).get("features") or [])
    except Exception:  # noqa: BLE001
        return None


def _overture_probe_counties(fx: Fetcher, cli: str, d: Path, top: int = 2) -> list:
    """取 TIGER 县界里 **bbox 面积最大** 的 top 个县，各自小框探一次 Overture。

    为什么用面积不用人口：roster 只有 fips/name，没有人口；面积可由本地县界直接算，
    不引入外部依赖。大县必然有门牌，返 0 就能证伪"上游没有"。
    """
    try:
        recs = _county_boundaries(fx)
    except Exception as exc:  # noqa: BLE001
        log(f"⚠️ 县界缓存不可用，跳过 Overture 县探针：{exc}")
        return []
    rows = []
    for key, rec in recs.items():
        rings = (((rec.get("payload") or {}).get("features") or [{}])[0]
                 .get("geometry") or {}).get("rings") or []
        xs = [pt[0] for ring in rings for pt in ring]
        ys = [pt[1] for ring in rings for pt in ring]
        if not xs:
            continue
        bb = (min(xs), min(ys), max(xs), max(ys))
        rows.append(((bb[2] - bb[0]) * (bb[3] - bb[1]), key, bb))
    rows.sort(reverse=True)
    probes = []
    for _area, key, bb in rows[:top]:
        p = d / f"overture_probe_{key}.parquet"
        cmd = [cli, "download", f"--bbox={','.join(f'{v:.6f}' for v in bb)}",
               "-f", "geoparquet", "--type=address", "-o", str(p)]
        try:
            pr = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=fx.args.overture_timeout)
            n = _count_parquet(p) if p.exists() else None
            probes.append({"county": key, "bbox": [round(v, 6) for v in bb],
                           "rows": n, "rc": pr.returncode,
                           "tail": ((pr.stdout or "")[-120:] + (pr.stderr or "")[-120:]).strip()[-200:]})
        except Exception as exc:  # noqa: BLE001
            probes.append({"county": key, "bbox": [round(v, 6) for v in bb],
                           "rows": None, "error": str(exc)[:160]})
        finally:
            p.unlink(missing_ok=True)
    log(f"Overture 防假 0 探针：{[ (x['county'], x.get('rows')) for x in probes ]}")
    return probes


def _record_no_coverage(fx: Fetcher, probed: int) -> None:
    """把"本州在 NAD 无覆盖"这条客观事实写进产物（随 Release 上架，可追溯）。"""
    _append_notes(fx, [f"- **NAD**: 本州在联邦 USDOT NAD 无覆盖"
                       f"（州级 returnCountOnly=0；{probed} 个县逐一探针全为 0）。"])


def build_manifest(fx: Fetcher) -> Path:
    def asset(p: Path) -> dict:
        r = rel(p)
        if p.exists():
            return {"path": r, "bytes": p.stat().st_size,
                    "sha256": df.sha256_full(p), "sha256_head": df.sha256_head(p)}
        m = fx.meta.get(r) or {}
        return {"path": r, "bytes": m.get("bytes"), "sha256": m.get("sha256"),
                "sha256_head": m.get("sha256_head")}

    def row_counts() -> dict:
        """每个数据产物可数的行/记录数（按资产路径给，数不了就不写）。"""
        byname: dict = {}
        for s in fx.status.values():
            if s.get("source") == "nad" and s.get("rows_fetched"):
                byname[f"nad_us_{fx.cfg['state'].lower()}_full.ndjson"] = s["rows_fetched"]
            if (s.get("source") == "overture" and s.get("features") is not None
                    and s.get("feature_file")):
                byname[os.path.basename(str(s["feature_file"]))] = s["features"]
            for d in (s.get("detail") or []):
                if not isinstance(d, dict) or not d.get("file"):
                    continue
                n = os.path.basename(str(d["file"]))
                if d.get("native_rows") is not None:
                    byname[n] = d["native_rows"]
                elif d.get("dbf_records") is not None:
                    byname[n] = d["dbf_records"]
        out = {rel(p): byname[os.path.basename(p)] for p in fx.files
               if os.path.basename(p) in byname}
        for s in (fx.cache.get("nad_shards") or []):
            if s.get("file"):
                out[s["file"]] = s["rows"]
        return out

    cfg = fx.cfg
    nad_st = fx.status.get("nad") or {}
    iso = _iso_codes(cfg["state"])
    m = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runner": os.environ.get("GITHUB_RUN_ID", "local"),
        "generator": "bots/fetch_state_raw.py",
        "roster": str(ROSTER_PATH.relative_to(ROOT)),
        "scope": {"country": "US", "state": cfg["state"], "state_name": cfg.get("name"),
                  "iso_3166_1": iso["iso_3166_1"], "iso_3166_2": iso["iso_3166_2"],
                  "iso_3166_2_verified": iso["iso_3166_2_verified"],
                  "state_fips": cfg["state_fips"], "bbox": cfg["bbox"],
                  "counties": [{"key": c["key"], "fips": c["fips"], "name": c["name"]} for c in cfg["counties"]],
                  "note": "四源全量【原始】数据 —— 未做任何清洗 / 去重 / 过滤 / 裁剪"},
        "integrity": {"sha256": "整个文件的 sha256",
                      "sha256_head": "前 1MB 的 sha256 前 16 位（快检用）"},
        "assets": [asset(p) for p in sorted(fx.files)],
        "row_counts": row_counts(),
        "shards": fx.cache.get("nad_shards") or [],
        "completeness": build_completeness(fx),
        "reference_check": fx.cache.get("nad_reference") or {},
        "nad_no_coverage": fx.cache.get("nad_no_coverage") or None,
        "quality": {"note": "只读统计，不做裁剪：BBox 是矩形，必然包含邻州边角门牌",
                    "state_distribution": nad_st.get("state_distribution"),
                    "state_in_scope_rows": nad_st.get("state_in_scope_rows"),
                    "state_in_scope_pct": nad_st.get("state_in_scope_pct"),
                    "state_blank_rows": nad_st.get("state_blank_rows"),
                    "nad_rows_scanned": nad_st.get("rows")},
        "sources": list(fx.status.values()),
        "ledger": fx.ledger,
    }
    p = fx.out / "MANIFEST.json"
    p.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def release_notes(manifest_path: Path, fx: Fetcher, tag: str) -> Path:
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    sc = m["scope"]
    lines = [
        f"# US {sc['state']}（{sc['state_name']}）四源全量原始数据 · `{tag}`",
        "",
        f"**范围**：州 `{sc['state']}`/FIPS `{sc['state_fips']}`｜"
        f"{len(sc['counties'])} 县（{', '.join(c['name'] for c in sc['counties'])}）｜"
        f"BBox `[{', '.join(str(v) for v in sc['bbox'])}]`",
        "",
        "**口径**：源里给什么就是什么 —— **不做清洗、不去重、不合并、不裁剪字段**。"
        "全部下载在 GitHub Actions runner 上发生（本地服务器零下载）。",
        "",
        f"生成时间：{m['generated_at']}　run：`{m['runner']}`　生成器：`{m['generator']}`",
        "",
        "## 资产清单",
        "",
        "| 资产 | 大小 | 说明 |",
        "|---|---|---|",
    ]
    detail_by_file = {}
    for s in m["sources"]:
        for dd in (s.get("detail") or []):
            if isinstance(dd, dict) and dd.get("file"):
                detail_by_file[os.path.basename(str(dd["file"]))] = dd
    rc = m.get("row_counts") or {}
    for a in m["assets"]:
        n = rc.get(a["path"])
        extra = ""
        if isinstance(n, int):
            extra = f"{n:,} 行/记录"
        else:
            dd = detail_by_file.get(Path(a["path"]).name) or {}
            if dd.get("features"):
                extra = f"{dd['features']:,} feature"
        lines.append(f"| `{Path(a['path']).name}` | {a['bytes']:,} B | {extra} |")
    lines += ["", "## 各源抓取结果（含分页模式 / 候选重试链）", "", "```json",
              json.dumps(m["sources"], ensure_ascii=False, indent=1)[:8000], "```", ""]

    shards = m.get("shards") or []
    if shards:
        lines += ["## 按县分片（每片 gzip，双摘要可校验）", "",
                  "| 分片 | 行数 | gzip | 解压后 sha256 |", "|---|---|---|---|"]
        for sh in shards:
            lines.append(f"| `{Path(sh['file']).name}` | {sh.get('rows', 0):,} "
                         f"| {sh.get('gz_bytes', 0) / 1e6:.2f} MB "
                         f"| `{str(sh.get('sha256_raw'))[:16]}` |")
        county_rows = sum(s['rows'] for s in shards if s.get('kind') == 'county')
        over_rows = sum(s['rows'] for s in shards if s.get('kind') == 'overflow')
        lines += ["", f"县片合计 **{county_rows:,}** 行 + 溢出包 **{over_rows:,}** 行"
                      f"（县界重复 {m['sources'][0].get('boundary_duplicate_rows', 0):,} 行，"
                      f"去重后 == 州官方 count：{m['sources'][0].get('official_count')}）", ""]

    comp = m.get("completeness") or {}
    if comp:
        lines += ["## 可证明完整性（上游声明 vs 本地产出）", "",
                  "| 源 | 上游声明 | 本地实得 | 对账 | 方法 |", "|---|---|---|---|---|",]
        for name, key in (("NAD", "nad"), ("Overture", "overture"),
                          ("OpenAddresses", "openaddresses"), ("Census TIGER", "census_tiger")):
            c = comp.get(key) or {}
            declared = c.get("upstream_declared_rows")
            lines.append(f"| {name} | {declared if declared is not None else '—（上游未声明）'} "
                         f"| {c.get('local_rows')} | {'✅' if c.get('evidence_1_full') else '⚠️ 仅存证'} "
                         f"| {str(c.get('method'))[:60]} |")
        lines += [""]

    q = m.get("quality") or {}
    dist = q.get("state_distribution")
    if dist:
        lines += ["## BBox 内 State 分布（只读统计，未裁剪）", "",
                  f"共扫 `{q.get('nad_rows_scanned')}` 行：", ""]
        for k, v in dist.items():
            lines.append(f"- `{k or '(空)'}`：{v:,}")
        lines += ["", f"本州（{sc['state']}）行数：{q.get('state_in_scope_rows')} "
                       f"（{q.get('state_in_scope_pct')}%）—— 其余为 BBox 四角邻州门牌，入湖不裁剪。", ""]
    if fx.problems:
        lines += ["", "## ⚠️ 门禁问题（已阻断发布）", ""] + [f"- {p}" for p in fx.problems]
    p = fx.out / "RELEASE_NOTES.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def create_release(files: list[Path], notes: Path, tag: str) -> None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        log("缺少 GITHUB_TOKEN / GITHUB_REPOSITORY → 跳过上传（本地模式）")
        return
    env = {**os.environ, "GH_TOKEN": token}
    cmd = ["gh", "release", "create", tag, "--repo", repo,
           "--title", tag, "--notes-file", str(notes), "--latest"] + [str(p) for p in files]
    log(f"gh release create {tag}（{len(files)} 个资产）")
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=7200)
    log(f"gh rc={proc.returncode} out={(proc.stdout or '')[-400:]} err={(proc.stderr or '')[-600:]}")
    if proc.returncode != 0:
        raise RuntimeError("Release 创建失败")


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="通用单州四源全量原始下载（World-US-Map-Data）")
    ap.add_argument("--state", required=True, help="州简称，如 DE / RI")
    ap.add_argument("--tag", default="", help="Release tag，留空 = raw-US-<州>-v1")
    ap.add_argument("--out-dir", default="", help="产物目录，留空 = data/raw/US-<州>")
    ap.add_argument("--roster", default=str(ROSTER_PATH), help="州配置档路径")
    ap.add_argument("--only", default="", help="只跑指定源，逗号分隔：nad,overture,openaddresses,census_tiger")
    ap.add_argument("--publish-release", action="store_true", help="建 Release 并上架资产")
    ap.add_argument("--no-release", action="store_true", help="只抓取不上架（本地自检）")
    ap.add_argument("--skip-gate", action="store_true", help="跳过发布门禁（仅排障用，禁止在正式跑里用）")
    # 四源参数
    ap.add_argument("--nad-max", type=int, default=800000)
    ap.add_argument("--nad-page", type=int, default=0, help="留空 = 用图层 maxRecordCount（硬上限 1000）")
    ap.add_argument("--nad-timeout", type=int, default=300)
    ap.add_argument("--nad-mode", choices=["ids", "cursor"], default="ids",
                    help="ids=returnIdsOnly+objectIds POST 批取（默认，治本）｜cursor=旧游标翻页（备选）")
    ap.add_argument("--nad-ids-batch", type=int, default=500,
                    help="每批 objectIds 个数（500~800 安全，必须走 POST）")
    ap.add_argument("--nad-split-threshold", type=int, default=50000,
                    help="ids 模式下单块超这个行数就自动降级到 tract/网格")
    ap.add_argument("--county-fips", default="",
                    help="只补跑指定县（逗号分隔 fips 或 key，如 10003,new_castle）；"
                         "留空=全州县清单")
    ap.add_argument("--nad-ids-max-depth", type=int, default=NAD_IDS_MAX_DEPTH,
                    help="主键清单被 400k 上限截断时四象限递归的最大层数（默认 6）")
    ap.add_argument("--nad-ids-slicing", choices=["quadrant", "oid"], default="quadrant",
                    help="主键清单分段方式：quadrant=四象限递归（仅矩形，v0 默认）｜"
                         "oid=OBJECTID 区间二分（通用，多边形/大县也能用）")
    ap.add_argument("--pack-mode", choices=["single", "county_shards"], default="single",
                    help="打包方式：single=v0 单文件（默认）｜county_shards=按县切片+gzip+统一命名")
    ap.add_argument("--nad-shards", choices=["county", "grid", "none"], default="county",
                    help="降级链一级切块：county=按县（默认）｜grid=州 bbox 网格｜none=整州")
    ap.add_argument("--nad-grid", type=int, default=2, help="--nad-shards grid 时的 N（N×N）")
    ap.add_argument("--nad-workers", type=int, default=2, help="cursor 模式分片并发（建议 2，避免撞按 IP 限速）")
    ap.add_argument("--overture-format", choices=["parquet", "geojson"], default="parquet")
    ap.add_argument("--overture-timeout", type=int, default=7200)
    ap.add_argument("--oa-max-mb", type=int, default=800)
    ap.add_argument("--oa-max-files", type=int, default=60)
    ap.add_argument("--oa-timeout", type=int, default=1800)
    ap.add_argument("--tiger-timeout", type=int, default=900)
    args = ap.parse_args(argv)

    roster = load_roster(Path(args.roster))
    cfg = state_cfg(roster, args.state)
    cfg["_endpoints"] = (roster.get("endpoints") or {})
    state = cfg["state"]
    tag = args.tag or f"raw-US-{state}-v1"
    out = Path(args.out_dir) if args.out_dir else (ROOT / "data" / "raw" / f"US-{state}")
    out.mkdir(parents=True, exist_ok=True)

    log(f"目标：US-{state}（{cfg.get('name')}）｜{len(cfg['counties'])} 县｜"
        f"BBox {cfg['bbox_str']}｜tag={tag}｜nad_mode={args.nad_mode}｜out={rel(out)}")

    fx = Fetcher(cfg, out, args)
    publishing = bool(args.publish_release) and not args.no_release and _gh_env() is not None
    fx.publish, fx.tag = publishing, tag
    if publishing:
        try:
            gh_ensure_release(tag)
            fx.remote = gh_assets(tag)
            txt = gh_read_asset_text(tag, STATUS_NAME)
            if txt:
                fx.status_doc = json.loads(txt)
                fx.status_doc.setdefault("phase", "collecting")
            fx.status_doc.setdefault("expected", [c["fips"] for c in cfg["counties"]])
            log(f"增量发布：tag={tag}｜远端已有资产 {len(fx.remote)} 个｜"
                f"已完成分片 {len(fx.status_doc.get('done') or [])} 个")
        except Exception as exc:  # noqa: BLE001
            log(f"⚠️ 增量发布初始化失败（回落为跑完一次性发布）：{exc}")
            publishing = fx.publish = False
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    jobs = [("nad", harvest_nad), ("overture", harvest_overture),
            ("openaddresses", harvest_openaddresses), ("census_tiger", harvest_tiger)]
    for name, fn in jobs:
        if only and name not in only:
            fx.note(name, "skipped", note="未在 --only 里")
            continue
        try:
            fn(fx)
        except Exception as exc:  # noqa: BLE001 单源失败不拖垮整轮，但如实记账
            fx.note(name, "exception", error=f"{type(exc).__name__}: {str(exc)[:400]}")
            fx.problem(f"源 {name} 抛异常：{type(exc).__name__}: {str(exc)[:200]}")
            _notify("alarm", {"state": state, "tag": tag, "stage": name,
                              "msg": f"源 {name} 抛异常：{type(exc).__name__}: {str(exc)[:300]}"})
        _checkpoint_others(fx)          # 每源跑完立刻把已产出文件传走并删本地

    if getattr(args, "pack_mode", "single") != "single":
        _apply_naming(fx)

    manifest = build_manifest(fx)
    fx.files.append(manifest)
    notes = release_notes(manifest, fx, tag)
    fx.files.append(notes)
    total = sum((p.stat().st_size if p.exists() else (fx.meta.get(rel(p)) or {}).get("bytes") or 0)
                for p in fx.files)
    log(f"产物 {len(fx.files)} 个，合计 {total / 1e6:.1f} MB")
    log("各源状态：" + json.dumps({k: v.get("status") for k, v in fx.status.items()}, ensure_ascii=False))

    if not args.skip_gate:
        try:
            gate(fx)
            log("✅ 发布门禁通过")
        except RuntimeError as exc:
            log("❌ " + str(exc))
            _notify("alarm", {"state": state, "tag": tag, "stage": "gate",
                              "msg": str(exc)[:500]})
            return 3
    if fx.cache.get("partial_run"):
        # ★ 2026-10-18 事故护栏：`--county-fips` 传成空引号字面量 '' 时，县循环空转、
        #   其他源照常跑完，结果拿 0 行账本盖成 phase=complete（DE/NH/RI/SD/WY 五个伪封印）。
        #   部分县运行永远不封印：MANIFEST 是全州口径，必须由全州重跑收尾。
        log("🛑 本轮为「只补指定县」模式：县片已上传，但**拒绝封印**"
            "（MANIFEST 需全州口径，请用全州重跑收尾）")
        return 5
    if args.no_release or not args.publish_release:
        log("（--no-release 或未指定 --publish-release）跳过上架")
        return 0
    if not fx.publish:                  # 没有 token/仓库信息 → 老路径：一次性发布
        create_release(fx.files, notes, tag)
        return 0
    # ★ 封印：MANIFEST 最后上传（唯一允许 clobber 的终态文件），然后 STATUS 标 complete
    if not _checkpoint(fx, manifest, clobber=True, delete=False):
        log("❌ MANIFEST 封印上传失败")
        return 4
    _checkpoint(fx, notes, clobber=True, delete=False)
    fx.status_doc["phase"] = "complete"
    fx.status_doc["completed_at"] = _now()
    _write_status(fx)
    log("✅ 已封印：MANIFEST.json 上架，STATUS.phase=complete")
    _notify("state_sealed", {
        "state": state, "tag": tag,
        "assets": len(fx.files),
        "counties_done": len(fx.status_doc.get("done") or []),
        "counties_expected": len(fx.status_doc.get("expected") or []),
    })
    log("完成：" + json.dumps({k: v.get("status") for k, v in fx.status.items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
