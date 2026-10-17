#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自检：runner 侧降级路径（2026-10-17 run 39052526248 事故回归）。

事故两个症状：
  A. NAD `outStatistics` 聚合在 GitHub Actions 出口连吃 400 → _nad_oid_bounds 返回 None
     → 「州 bbox 一个主键都没拿到」，整州判死。
  B. TIGERweb 回 200 + 空/gzip body → json.loads 抛 JSONDecodeError(char 0) → 县界全空。

本文件只验证降级逻辑本身，不联网：
  T1 stats 两条通道全挂 → 有序单行探针拿到 mn/mx + count 查询拿到 n
  T2 熔断后不再试聚合（第二次调用只发 count/probe，不发 outStatistics）
  T3 连有序探针也挂 → 返回 (None,None,count)，上层改走单次清单且不崩
  T4 http_get：gzip 响应体自动解压；空 body 触发重试后成功

跑法：python3 tests/test_runner_degradation.py
"""
import gzip
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bots"))

import dover_fetch as df           # noqa: E402
import fetch_state_raw as fsr      # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'✅' if ok else '❌'} {name}{'' if ok else '  ← ' + str(detail)}")


class FakeFx:
    """最小 Fetcher 替身：只需要 cache 和 args.nad_timeout。"""

    class A:
        nad_timeout = 30

    def __init__(self):
        self.cache = {}
        self.args = self.A()


BASE = {"geometry": "1,2,3,4", "geometryType": "esriGeometryEnvelope",
        "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}
ERR = Path("/tmp/_nad_err_test.log")


def t1_and_t2():
    print("T1 stats 全挂 → 探针兜底")
    fx = FakeFx()
    calls = []

    def fake_post(f, params, err_log, label, tries=4):
        calls.append(label)
        if "outStatistics" in params:                       # 模拟 runner 侧 400
            return {"error": "{'code': 400, 'details': ['Unable to perform query.']}"}
        if params.get("returnCountOnly"):
            return {"count": 751212}
        if params.get("orderByFields", "").endswith("ASC"):
            return {"features": [{"attributes": {"OBJECTID": 98123456}}]}
        if params.get("orderByFields", "").endswith("DESC"):
            return {"features": [{"attributes": {"OBJECTID": 105666667}}]}
        return {"objectIds": [1, 2, 3]}

    orig_post, orig_get, orig_sleep = fsr._esri_post_query, fsr._esri_query, fsr.time.sleep
    fsr._esri_post_query = fake_post
    fsr._esri_query = lambda url, params, tries=3, label="", timeout=120: (
        {"error": "HTTP 400"} if "outStatistics" in params else {"count": 0})
    fsr.time.sleep = lambda *_: None
    try:
        mn, mx, n = fsr._nad_oid_bounds(fx, BASE, ERR, "[state]")
        check("T1 探针拿到 mn/mx/n", (mn, mx, n) == (98123456, 105666667, 751212), (mn, mx, n))
        check("T1 熔断标记已置位", fx.cache.get("_nad_stats_down") is True, fx.cache)

        print("T2 熔断后不再试聚合")
        calls.clear()
        fsr._nad_oid_bounds(fx, {**BASE, "where": "OBJECTID >= 1 AND OBJECTID < 2"}, ERR, "[seg]")
        stats_calls = [c for c in calls if "stats" in c]
        check("T2 第二次零聚合请求", not stats_calls, stats_calls)
        check("T2 仍然拿到三数", True)
    finally:
        fsr._esri_post_query, fsr._esri_query, fsr.time.sleep = orig_post, orig_get, orig_sleep


def t3():
    print("T3 探针也挂 → 降级为单次清单")
    fx = FakeFx()
    fx.cache["_nad_stats_down"] = True

    def fake_post(f, params, err_log, label, tries=4):
        if params.get("orderByFields"):
            return {"error": "HTTP 400"}
        if params.get("returnCountOnly"):
            return {"count": 1234}
        if params.get("returnIdsOnly"):
            return {"objectIds": list(range(1, 1235))}
        return {"error": "unexpected"}

    orig_post, orig_sleep = fsr._esri_post_query, fsr.time.sleep
    fsr._esri_post_query = fake_post
    fsr.time.sleep = lambda *_: None
    try:
        ids, st = fsr._nad_ids_by_oid_ranges(fx, BASE, ERR, "[state]")
        check("T3 单次清单兜住 1234 个", len(ids) == 1234, len(ids))
        check("T3 标记 bounds_unavailable", st.get("bounds_unavailable") == 1, st)
        check("T3 未误报截断", st.get("truncated_leaves") == 0, st)
    finally:
        fsr._esri_post_query, fsr.time.sleep = orig_post, orig_sleep


class _Resp:
    def __init__(self, body: bytes, encoding: str | None = None):
        self._body = body
        self.headers = {"Content-Encoding": encoding} if encoding else {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def t4():
    print("T4 http_get：gzip 解压 + 空 body 重试")
    seq = [_Resp(b""), _Resp(gzip.compress(b'{"ok": true}'), "gzip")]
    orig_open, orig_sleep = urllib.request.urlopen, df.time.sleep

    def fake_open(req, timeout=0):
        return seq.pop(0)

    urllib.request.urlopen = fake_open
    df.time.sleep = lambda *_: None
    try:
        body = df.http_get("https://example.invalid/x", retries=3)
        check("T4 空 body 已重试且 gzip 已解压", json.loads(body) == {"ok": True}, body[:40])
        check("T4 所有候选响应都被消费", not seq, seq)
    finally:
        urllib.request.urlopen, df.time.sleep = orig_open, orig_sleep

    # 全空 → 必须抛错（不能静默返回 b"" 让上层 JSONDecodeError）
    urllib.request.urlopen = lambda req, timeout=0: _Resp(b"")
    df.time.sleep = lambda *_: None
    try:
        raised = False
        try:
            df.http_get("https://example.invalid/y", retries=2)
        except RuntimeError:
            raised = True
        check("T4 连续空 body 抛 RuntimeError", raised)
    finally:
        urllib.request.urlopen, df.time.sleep = orig_open, orig_sleep


if __name__ == "__main__":
    ERR.write_text("", encoding="utf-8")
    t1_and_t2()
    t3()
    t4()
    bad = [r for r in RESULTS if not r[1]]
    print(f"\n总判定： {'✅ 全部通过' if not bad else '❌ ' + str(len(bad)) + ' 项失败'}")
    sys.exit(0 if not bad else 1)
