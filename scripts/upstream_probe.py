#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上游健康探针 —— **必须在 GitHub Actions runner 上跑，不能在本机跑**。

为什么不能在本机（2026-10-22 实证，这条比代码本身重要）：
  · 本机是香港 VPS（45.136.15.188 / AS139659），数据源全在美国。
  · TIGER：本机 curl 与 urllib 对 www2.census.gov 一律 **403**，而同一时刻 runner 侧
    正成功下载 32 个 TIGER 资产 → 403 是本机 IP 被 WAF 拦，不是上游挂了。
  · NAD：本机冷启动 count 49.8s、热态 10.0s；runner 侧同一查询在秒级 → 本机测出的
    "上游慢"里大部分是跨太平洋延迟。
  · Overture：本机没装 overturemaps CLI，且本机装的那版 `-f parquet` 直接报参数错。

结论：**探针与干活的机器必须是同一台**，否则结论指向的是探针所在网络，不是上游。
所以探针放在 runner 侧，结论写 step summary；异常时本脚本 exit 1 让 job 变红，
本机 gh_watch.py 已有的 failure 通道会自动告警 —— 不需要新的推送管道。

用法（Actions 里）：
  python scripts/upstream_probe.py --step-summary
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
NAD = ("https://services.arcgis.com/xOi1kZaI0eWDREZv/ArcGIS/rest/services/"
       "Address_Points_from_National_Address_Database_view/FeatureServer/0/query")
TIGER = "https://www2.census.gov/geo/tiger/TIGER2024/COUNTY/tl_2024_us_county.zip"
OA_HOME = "https://results.openaddresses.io/"

NAD_SLOW_SEC = 20.0     # runner 侧正常 1~5s；>20s 即上游退化（本机 49.8s 是延迟假象）


def probe_nad() -> dict:
    bbox = "-75.79,38.45,-75.05,39.84"          # DE：门牌量小、覆盖稳定
    body = urllib.parse.urlencode({
        "geometry": bbox, "geometryType": "esriGeometryEnvelope", "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects", "returnCountOnly": "true", "f": "json",
    }, quote_via=urllib.parse.quote, safe=",").encode()   # 逗号不能编码成 %2C，否则 400
    t0 = time.time()
    try:
        req = urllib.request.Request(NAD, data=body, headers={
            **UA, "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=90) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))
        sec = time.time() - t0
        cnt = doc.get("count")
        if cnt is None:
            return {"ok": False, "sec": sec, "note": f"无 count：{str(doc)[:120]}"}
        ok = sec <= NAD_SLOW_SEC
        return {"ok": ok, "sec": sec, "count": cnt,
                "note": f"{cnt:,} 行 / {sec:.1f}s" + ("" if ok else
                        f"（>{NAD_SLOW_SEC:.0f}s 上游退化，此时派大州会白跑）")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "sec": time.time() - t0,
                "note": f"{type(e).__name__}: {str(e)[:110]}（400/超时=上游拒服务）"}


def probe_tiger() -> dict:
    t0 = time.time()
    try:
        req = urllib.request.Request(TIGER, headers={**UA, "Range": "bytes=0-1023"})
        with urllib.request.urlopen(req, timeout=40) as r:
            code, n = r.status, len(r.read())
        return {"ok": code in (200, 206) and n > 0, "sec": time.time() - t0,
                "note": f"HTTP {code} / {n}B"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "sec": time.time() - t0,
                "note": f"{type(e).__name__}: {str(e)[:110]}"}


def probe_oa() -> dict:
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(OA_HOME, headers=UA),
                                    timeout=40) as r:
            html = r.read().decode("utf-8", "replace")
        import re
        ids = set(re.findall(r"/runs/(\d+)/", html))
        return {"ok": bool(ids), "sec": time.time() - t0,
                "note": f"解析到 {len(ids)} 个 run id" if ids else "首页解析不出 run id"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "sec": time.time() - t0,
                "note": f"{type(e).__name__}: {str(e)[:110]}"}


def probe_overture() -> dict:
    cli = subprocess.run(["bash", "-lc", "command -v overturemaps || true"],
                         capture_output=True, text=True).stdout.strip()
    if not cli:
        return {"ok": True, "sec": 0, "skip": True, "note": "runner 上未装 CLI，跳过"}
    out = "/tmp/_ovt_probe.geoparquet"
    t0 = time.time()
    try:
        r = subprocess.run([cli, "download", "--bbox=-75.56,39.83,-75.55,39.84",
                            "-f", "geoparquet", "--type=address", "-o", out],
                           capture_output=True, text=True, timeout=240)
        sz = os.path.getsize(out) if os.path.exists(out) else 0
        ok = r.returncode == 0 and sz > 0
        return {"ok": ok, "sec": time.time() - t0,
                "note": f"{sz}B" if ok else
                        f"rc={r.returncode} {((r.stderr or '')[-160:]).strip()}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "sec": time.time() - t0, "note": f"{type(e).__name__}: {e}"}
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def main() -> int:
    res = {"nad": probe_nad(), "tiger": probe_tiger(),
           "oa": probe_oa(), "overture": probe_overture()}
    bad = [k for k, v in res.items() if isinstance(v, dict)
           and not v.get("ok") and not v.get("skip")]

    rows = ["| 探针 | 结论 | 详情 |", "|---|---|---|"]
    for k, v in res.items():
        mark = "✅" if v.get("ok") else ("⏭ 跳过" if v.get("skip") else "🔴 异常")
        rows.append(f"| {k.upper()} | {mark} | {v.get('note', '')} |")
    md = ("## 上游健康探针（runner 侧）\n\n" + "\n".join(rows) +
          f"\n\n异常项：**{', '.join(x.upper() for x in bad) if bad else '无'}**\n"
          "\n> 本探针只在 runner 上跑才有意义：本机（香港 VPS）测出的 TIGER 403 / "
          "NAD 49.8s 都是本机网络假象。\n")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(md)
    print(md)
    json.dump(res, open("upstream_probe.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    if bad:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from sentinel_inbox import emit
            for k in bad:
                emit("red", f"upstream:{k}", f"上游 {k.upper()} 异常",
                     str(res.get(k, {}).get("note") or ""))
        except Exception as e:  # noqa: BLE001
            print(f"[inbox] 投信失败：{type(e).__name__}: {e}")
        print(f"::notice::上游异常：{', '.join(bad)}（已投信，本 run 不算失败）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
