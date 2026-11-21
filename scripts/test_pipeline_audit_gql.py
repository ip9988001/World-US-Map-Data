#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline_audit.gql() 禁吞异常 + 分页/REST 取数 的离线回归（muse-000156 一）。

跑法：python3 scripts/test_pipeline_audit_gql.py
依赖：只用标准库（unittest + unittest.mock 打 subprocess.run），不联网、不需要 gh CLI。

历史事故（本次要钉死的）：旧 gql() 是
    return json.loads(r.stdout)["data"]["repository"] if r.returncode == 0 else {}
外加 `except Exception: return {}` —— 504 / 限流 / 非 JSON / GraphQL errors 全被静默吞成
空 dict，上层 A2 于是把「取数失败」判成「所有州都没开工」，灌出 51 条假 B1（issue #5）。
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pipeline_audit as pa  # noqa: E402

OK_REPO = {"releases": {"nodes": [{"tagName": "raw-US-AL-v1",
                                   "releaseAssets": {"totalCount": 7}}],
                        "totalCount": 1,
                        "pageInfo": {"hasNextPage": False, "endCursor": "c1"}}}


def cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def gql_stdout(repo: dict | None, errors: list | None = None) -> str:
    doc: dict = {"data": {"repository": repo}}
    if errors:
        doc["errors"] = errors
    return json.dumps(doc)


class GqlFailureModes(unittest.TestCase):
    """五种失败都必须 raise，一个都不许 return {}。"""

    def test_returncode_nonzero_raises(self) -> None:
        with patch("subprocess.run", return_value=cp(1, "", "HTTP 504: Gateway Timeout")):
            with self.assertRaises(pa.GqlError) as cm:
                pa.gql("query{x}")
        msg = str(cm.exception)
        self.assertIn("returncode=1", msg)
        self.assertIn("504", msg)

    def test_invalid_json_raises(self) -> None:
        with patch("subprocess.run", return_value=cp(0, "<html>504 Gateway Timeout</html>")):
            with self.assertRaises(pa.GqlError) as cm:
                pa.gql("query{x}")
        self.assertIn("非 JSON", str(cm.exception))
        self.assertIn("504 Gateway", str(cm.exception))     # stdout 前 200 字节要带出来

    def test_timeout_raises(self) -> None:
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=180)):
            with self.assertRaises(pa.GqlError) as cm:
                pa.gql('query{repository(owner:"o",name:"n"){releases(first:1){nodes{tagName}}}}')
        msg = str(cm.exception)
        self.assertIn("timeout=180s", msg)
        self.assertIn("releases(first:1)", msg)             # query 前 120 字节要带出来

    def test_graphql_errors_raises(self) -> None:
        with patch("subprocess.run",
                   return_value=cp(0, gql_stdout(OK_REPO, errors=[{"message": "rate limited"}]))):
            with self.assertRaises(pa.GqlError) as cm:
                pa.gql("query{x}")
        self.assertIn("errors", str(cm.exception))
        self.assertIn("rate limited", str(cm.exception))

    def test_repository_null_raises(self) -> None:
        with patch("subprocess.run", return_value=cp(0, gql_stdout(None))):
            with self.assertRaises(pa.GqlError) as cm:
                pa.gql("query{x}")
        self.assertIn("repository 为 null", str(cm.exception))

    def test_success_returns_repository_node(self) -> None:
        """契约不变：返回的是 repository 节点 dict，调用方不用改。"""
        with patch("subprocess.run", return_value=cp(0, gql_stdout(OK_REPO))):
            self.assertEqual(pa.gql("query{x}"), OK_REPO)


class PaginationAndRest(unittest.TestCase):
    """轻查询分页 + REST 资产名：失败同样 raise，不许静默降级成空集合。"""

    def test_releases_paging_follows_cursor(self) -> None:
        pages = [
            cp(0, json.dumps({"data": {"repository": {"releases": {
                "nodes": [{"tagName": "raw-US-AL-v1", "releaseAssets": {"totalCount": 3}}],
                "totalCount": 2,
                "pageInfo": {"hasNextPage": True, "endCursor": "CUR1"}}}}})),
            cp(0, json.dumps({"data": {"repository": {"releases": {
                "nodes": [{"tagName": "raw-US-AK-v1", "releaseAssets": {"totalCount": 5}}],
                "totalCount": 2,
                "pageInfo": {"hasNextPage": False, "endCursor": "CUR2"}}}}})),
        ]
        with patch("subprocess.run", side_effect=pages) as m:
            got = pa.fetch_releases()
        self.assertEqual([n["tagName"] for n in got["releases"]["nodes"]],
                         ["raw-US-AL-v1", "raw-US-AK-v1"])
        self.assertEqual(got["releases"]["totalCount"], 2)
        self.assertFalse(got["releases"]["pageInfo"]["hasNextPage"])
        self.assertEqual(m.call_count, 2)                       # 真的翻了两页
        self.assertIn('after:"CUR1"', m.call_args_list[1][0][0][-1])   # 第二页带游标

    def test_asset_names_use_rest_and_page(self) -> None:
        pa._ASSET_NAMES.clear()
        first = [f"asset-{i:03d}" for i in range(100)]
        rest_calls = [
            cp(0, "4242\n"),                                     # releases/tags/<tag> → id
            cp(0, "\n".join(first) + "\n"),                      # assets 第 1 页（满 100）
            cp(0, "MANIFEST.json\nSTATUS.json\n"),               # assets 第 2 页（不满 100 → 完）
        ]
        with patch("subprocess.run", side_effect=rest_calls) as m:
            names = pa.release_asset_names("raw-US-AL-v1")
        self.assertEqual(len(names), 102)
        self.assertIn("MANIFEST.json", names)
        self.assertEqual(m.call_count, 3)
        self.assertIn("/releases/4242/assets?per_page=100&page=2", m.call_args_list[2][0][0][2])
        # 缓存生效：第二次 0 次请求
        with patch("subprocess.run", side_effect=AssertionError("不该再请求")) as m2:
            self.assertEqual(pa.release_asset_names("raw-US-AL-v1"), names)
            self.assertEqual(m2.call_count, 0)

    def test_asset_rest_failure_raises_not_empty(self) -> None:
        """REST 挂了必须 raise —— 否则 MANIFEST 会被误判成「不存在」→ 假 A6/A4。"""
        pa._ASSET_NAMES.clear()
        with patch("subprocess.run", return_value=cp(1, "", "HTTP 403: API rate limit exceeded")):
            with self.assertRaises(pa.GqlError) as cm:
                pa.release_asset_names("raw-US-AL-v1")
        self.assertIn("releases/tags/raw-US-AL-v1", str(cm.exception))
        self.assertIn("returncode=1", str(cm.exception))

    def test_asset_id_not_int_raises(self) -> None:
        pa._ASSET_NAMES.clear()
        with patch("subprocess.run", return_value=cp(0, "null\n")):
            with self.assertRaises(pa.GqlError) as cm:
                pa.release_asset_names("raw-US-AL-v1")
        self.assertIn("不是整数", str(cm.exception))

    def test_has_manifest_uses_rest_names(self) -> None:
        pa._ASSET_NAMES["raw-US-AL-v1"] = ["MANIFEST.json", "STATUS.json"]
        pa._ASSET_NAMES["raw-US-AK-v1"] = ["STATUS.json"]
        self.assertTrue(pa.has_manifest("raw-US-AL-v1"))
        self.assertFalse(pa.has_manifest("raw-US-AK-v1"))


class A2StillBlocks(unittest.TestCase):
    """A2 硬拦原样保留：nodes 为空且 roster 非空 → exit 1 且一条 B1 都不许产出。"""

    def test_empty_nodes_exits_1_without_b1(self) -> None:
        import contextlib
        import os
        import tempfile
        buf = io.StringIO()
        cwd, tmp = os.getcwd(), tempfile.mkdtemp(prefix="pa-gql-")
        try:
            os.chdir(tmp)
            with patch.object(pa, "fetch_releases", return_value={"releases": {"nodes": []}}), \
                 contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = pa.main()
            report = Path(tmp) / "audit_report.json"
            wrote = report.exists()
        finally:
            os.chdir(cwd)
        self.assertEqual(rc, 1)
        self.assertFalse(wrote, "A2 硬拦后不应产出 audit_report.json")
        self.assertNotIn("（未开工）", buf.getvalue())      # 一条 B1 行都不许有
        self.assertIn("::error::", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
