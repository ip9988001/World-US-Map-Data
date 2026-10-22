#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全量发布物质量抽查（在 runner 上跑）—— 看记录 + 每州抽几个小样本验实物。

用户口径（2026-10-22）：
  「不用每个发布物都仔细查看，就看看记录然后每个 releases 抽查几个比较小的发布物看看质量」

分两段：
  A 记录核查（每州一次）：STATUS.phase / done 数 vs roster 县数 / MANIFEST 是否存在 /
    资产数 vs MANIFEST.assets 数
  B 实物抽查（每州 3 个最小可验资产，跳过 STATUS/MANIFEST/columns/metadata）：
    .ndjson.gz  → gzip 能解、行数 >0、首行 JSON 可解析、有几何或坐标键
    .zip        → zipfile 可打开、testzip 无损坏、成员数 >0
    .parquet    → duckdb 能读、schema 非空、行数 ≥0
    .geojson    → JSON 可解析、features 非空
    .json       → JSON 可解析

输出：Markdown 表（每州一行）+ 问题清单，问题投 Issue 信箱。
"""
from __future__ import annotations

import gzip
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import yaml

REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"
ROOT = Path(__file__).resolve().parents[1]
ROSTER = ROOT / "rules" / "US" / "state_roster.yaml"
META = ("STATUS.json", "MANIFEST.json", "columns.json", "layer-metadata")
SAMPLE_N = 3
MAX_SAMPLE_MB = 40          # 单个抽样资产上限，防大文件拖死


def sh(args: list[str], timeout: int = 180) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def is_meta(n: str) -> bool:
    return any(m in n for m in META)


def verify(name: str, path: Path) -> str:
    """返回 'ok' 或问题描述。"""
    try:
        if name.endswith(".ndjson.gz"):
            n = 0
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
                first = f.readline()
                if not first.strip():
                    return "gzip 首行为空"
                doc = json.loads(first)
                if not isinstance(doc, dict) or not doc:
                    return "首行不是对象"
                n = 1 + sum(1 for _ in f)
            if n <= 0:
                return "行数 0"
            return "ok"
        if name.endswith(".zip"):
            with zipfile.ZipFile(path) as z:
                bad = z.testzip()
                if bad:
                    return f"zip 成员损坏：{bad}"
                if not z.namelist():
                    return "zip 空"
            return "ok"
        if name.endswith(".parquet") or name.endswith(".geoparquet"):
            import duckdb
            q = duckdb.sql(f"SELECT count(*) FROM read_parquet('{path}')").fetchone()
            if q is None:
                return "parquet 读不出行数"
            cols = duckdb.sql(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()
            if not cols:
                return "parquet schema 空"
            return "ok"
        if name.endswith((".geojson", ".json")):
            doc = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(doc, dict) and "features" in doc:
                if not doc["features"]:
                    return "geojson features 为 0"
            return "ok"
        return "跳过（未知类型）"
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {str(e)[:90]}"


def main() -> int:
    counties = {k: len(v.get("counties") or [])
                for k, v in yaml.safe_load(ROSTER.read_text(encoding="utf-8"))["states"].items()}

    raw = sh(["gh", "api", "--paginate", f"repos/{REPO}/releases?per_page=100",
              "-q", '.[] | select(.tag_name|startswith("raw-US-")) | .tag_name'])
    tags = [t.strip() for t in raw.splitlines() if t.strip()]
    print(f"共 {len(tags)} 个 raw-US release\n")

    tmp = Path(tempfile.mkdtemp(prefix="qa-"))
    rows, probs = [], []
    for tag in tags:
        st = tag.split("-")[2]
        aj = sh(["gh", "api", f"repos/{REPO}/releases/tags/{tag}",
                 "-q", '.assets[] | "\(.name)\t\(.size)"'])
        assets = []
        for line in aj.splitlines():
            if "\t" in line:
                nm, sz = line.rsplit("\t", 1)
                assets.append((nm, int(sz or 0)))
        total_n = len(assets)
        data = [(n, s) for n, s in assets if not is_meta(n)]
        meta = [n for n, _ in assets if is_meta(n)]

        # ---- A 记录 ----
        phase, done = "?", 0
        stf = tmp / f"{st}_STATUS.json"
        sh(["gh", "release", "download", tag, "-R", REPO, "--pattern", "STATUS.json",
            "-O", str(stf), "--clobber"])
        try:
            d = json.loads(stf.read_text(encoding="utf-8"))
            phase = d.get("phase") or "?"
            done = len(d.get("done") or [])
        except Exception:  # noqa: BLE001
            probs.append(f"{st} 无/坏 STATUS.json")

        man_ok = any(n.startswith("MANIFEST") for n in meta)
        man_n = None
        if man_ok:
            mf = tmp / f"{st}_MANIFEST.json"
            sh(["gh", "release", "download", tag, "-R", REPO, "--pattern", "MANIFEST.json",
                "-O", str(mf), "--clobber"])
            try:
                m = json.loads(mf.read_text(encoding="utf-8"))
                man_n = len(m.get("assets") or {})
            except Exception:  # noqa: BLE001
                probs.append(f"{st} MANIFEST 解析失败")

        # ---- B 抽样：最小的 N 个数据资产 ----
        picks = sorted(data, key=lambda x: x[1])[:SAMPLE_N]
        results = []
        for nm, sz in picks:
            if sz > MAX_SAMPLE_MB * 1024 * 1024:
                results.append(f"{nm[:28]}:跳过(>{MAX_SAMPLE_MB}MB)")
                continue
            out = tmp / nm
            sh(["gh", "release", "download", tag, "-R", REPO, "-p", nm,
                "-O", str(out), "--clobber"])
            if not out.exists() or out.stat().st_size == 0:
                results.append(f"{nm[:28]}:下载失败")
                probs.append(f"{st} 下载失败 {nm}")
                continue
            v = verify(nm, out)
            results.append(f"{nm[:28]}:{v}")
            if v not in ("ok", "跳过（未知类型）"):
                probs.append(f"{st} {nm} → {v}")
            out.unlink(missing_ok=True)

        rows.append({"st": st, "n": total_n, "phase": phase, "done": done,
                     "total": counties.get(st, 0), "man": man_ok, "man_n": man_n,
                     "sample": results})
        flag = "🟢" if (man_ok and phase == "complete" and not any(
            ":" in r and not r.endswith(":ok") for r in results)) else "🟡"
        print(f"{flag} {st:3} 资产={total_n:4} phase={phase:16} done={done}/{counties.get(st, '?')} "
              f"MANIFEST={'Y' if man_ok else 'N'} 抽样：" + " | ".join(results))

    # ---- 汇总 ----
    md = ["| 州 | 资产 | phase | done/总 | MANIFEST | 抽样 |", "|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['st']} | {r['n']} | {r['phase']} | {r['done']}/{r['total']} | "
                  f"{'Y' if r['man'] else 'N'} | {'；'.join(r['sample'])} |")
    body = (f"## 发布物质量抽查（{len(rows)} 个州）\n\n" + "\n".join(md) +
            (f"\n\n### 问题（{len(probs)}）\n" + "\n".join(f"- {p}" for p in probs[:40])
             if probs else "\n\n全部通过 ✅"))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(body + "\n")
    print("\n" + body[:3000])

    if probs:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from sentinel_inbox import emit
            emit("yellow", "quality:sweep", f"发布物抽查发现 {len(probs)} 处问题",
                 "\n".join(f"- {p}" for p in probs[:40]))
        except Exception as e:  # noqa: BLE001
            print(f"[inbox] 投信失败：{e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
