#!/usr/bin/env python3
import os as _os; _os.environ.setdefault("FSR_FAST", "1")
# -*- coding: utf-8 -*-
"""离线自检：不联网，用假 http 层把 fetch_state_raw.py 的四源全流程跑一遍。

跑法：python3 tests/test_fetch_state_raw_offline.py     （无需第三方依赖，纯标准库）

六个场景：
  S1 cursor 四源齐全      → 门禁通过；游标分页 orderByFields=OBJECTID ASC、全程无 resultOffset
  S2 县界全空             → 门禁阻断（退出 3），且**不落空壳文件**
  S3 cursor 游标漏行      → 4 分块自愈补齐 → 门禁通过
  S4 cursor 按县切块      → 3 片并行、跨片按 OID 去重、县界只查一次（缓存共用）
  S5 ids 主路径           → returnIdsOnly + objectIds(POST) 批取；某批首次被拒 → 缩批后自恢复；
                            硬对账 official_ids == rows_fetched == 目标集合，全程不碰游标
  S6 ids 降级链           → 州级 returnIdsOnly 失败 → 自动降级到「县」块，仍能拿满并对账通过
  S7 ids 400k 静默截断    → 州级 ids 被服务端截到上限（count 更大）→ 四象限递归切分 →
                            叶子逐个对齐、汇总 == 州级 count、门禁放行（模拟真实 RI 事故）
  S8 四源自证 + 元数据   → MANIFEST 的 completeness（四源对账）/ quality（State 分布）/
                            row_counts / 全文 sha256 全部到位（P0+P2）
  S9 OID 区间二分        → 主键清单撞 400k 上限时按 OBJECTID 区间二分补齐（v1 通用切法）
  S11 增量发布 + 断点续传 → STATUS.json 过程态 / 分片即传即删 / MANIFEST 最后封印 /
                            重跑零批取（全命中断点）
  S12 县级核验为准入门槛 → 一个县坏只丢那个县；其余县照常上架；州级三数只做参考
  S10 按县切片 + gzip    → 每个县一个 gz 分片 + 溢出包；州总量 == Σ县片 + 溢出（三数咬合）；
                            命名统一为 NAD/TIGER/OA/OVERTURE-US-{州}-...
"""
import io
import json
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bots"))
import fetch_state_raw as fs  # noqa: E402
import dover_fetch as df  # noqa: E402

FULL_BBOX = "-75.8,38.44,-75.04,39.85"
STATE_BBOX = (-75.8, 38.44, -75.04, 39.85)
PAD = "x" * 2000

FAKE_CLI_SRC = '''#!/usr/bin/env python3
import sys
a = sys.argv[1:]
if not a or a[0] != "download":
    sys.exit(1)
out = a[a.index("-o") + 1]
fmt = a[a.index("-f") + 1]
if fmt == "geojson":
    feats = ",".join(
        '{"type":"Feature","properties":{"number":"%d","street":"MAIN ST"},'
        '"geometry":{"type":"Point","coordinates":[-75.5,39.1]}}' % i for i in range(3))
    open(out, "w").write('{"type":"FeatureCollection","features":[' + feats + ']}')
else:
    open(out, "wb").write(b"PAR1" + b"\\x00" * 2048)
'''
FAKE_CLI = Path(tempfile.mkdtemp(prefix="fakebin-")) / "overturemaps"
FAKE_CLI.write_text(FAKE_CLI_SRC, encoding="utf-8")
FAKE_CLI.chmod(0o755)

COUNTY_RINGS = {
    "001": [[[0.0, 0.0], [0.1, 0.0], [0.1, 0.1], [0.0, 0.1], [0.0, 0.0]]],
    "003": [[[0.1, 0.0], [0.2, 0.0], [0.2, 0.1], [0.1, 0.1], [0.1, 0.0]]],
    "005": [[[0.2, 0.0], [0.3, 0.0], [0.3, 0.1], [0.2, 0.1], [0.2, 0.0]]],
}
SHARD_POOLS = [((0.0, 0.0, 0.1, 0.1), [1, 2]),
               ((0.1, 0.0, 0.2, 0.1), [3, 4]),
               ((0.2, 0.0, 0.3, 0.1), [5])]

CALLS = {"cursor": [], "tiger": [], "post": []}
FAKE = {"official": 5, "main_pool": [], "shard_pool": [], "county_ok": True,
        "ids_target": [], "ids_by_bbox": {}, "ids_cap": 0, "poly_pools": {},
        "ids_fail_state": False, "fail_min_batch": 0}


def kk(bb):
    """BBox → 可哈希 key（6 位小数）。生产端用 str(float) 拼串，回读后取整一致。"""
    return tuple(round(float(v), 6) for v in bb) if bb else None


def _poly_key(params):
    try:
        rings = (json.loads(params.get("geometry") or "{}") or {}).get("rings") or []
    except Exception:
        return None
    if not rings or not rings[0]:
        return None
    pt = rings[0][0]
    for k, rr in COUNTY_RINGS.items():
        if abs(rr[0][0][0] - pt[0]) < 1e-9 and abs(rr[0][0][1] - pt[1]) < 1e-9:
            return k
    return None


def _pool_of(params):
    """多边形 → 按县池；矩形 → 按 bbox 池。"""
    if str(params.get("geometryType")) == "esriGeometryPolygon":
        return list(FAKE["poly_pools"].get(_poly_key(params)) or [])
    return _ids_for(_bbox_of(params))


def _ids_for(bb):
    """某 BBox 的**真实**主键全集（未截断）；未登记的 BBox 回退 ids_target。"""
    if bb is None:
        return []
    hit = FAKE["ids_by_bbox"].get(kk(bb))
    return list(hit) if hit is not None else list(FAKE["ids_target"])


def make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("us/de/x.csv",
                    "number,street,unit,city,region,postcode,lon,lat\n"
                    + "".join(f"{i} Main St,,Dover,DE,19901,-75.5,39.1\n" for i in range(30)))
        zf.writestr("us/de/x.shp", b"\x00" * 3000)
        # 真 .dbf/.shx 头：给 TIGER 双头对账（.dbf 记录数 == .shx 记录数）用
        nrec, hlen = 30, 33 + 32
        dbf = bytearray(32 + 32 + 1)
        dbf[0] = 3
        dbf[4:8] = nrec.to_bytes(4, "little")
        dbf[8:10] = hlen.to_bytes(2, "little")
        dbf[10:12] = (1 + 12).to_bytes(2, "little")
        dbf[32:43] = b"TLID\x00\x00\x00\x00\x00\x00\x00"
        dbf[64] = 0x0D
        zf.writestr("us/de/x.dbf", bytes(dbf))
        zf.writestr("us/de/x.shx", b"\x00" * (100 + nrec * 8))
    return buf.getvalue()


ZIP = make_zip()


def nad_feats(oids):
    return [{"attributes": {"OBJECTID": o, "Add_Number": str(o), "StNam_Full": "MAIN ST",
                            "State": "DE", "Pad": "y" * 200},
             "geometry": {"x": -75.5, "y": 39.1}} for o in oids]


def _bbox_of(params):
    try:
        vals = tuple(float(x) for x in str(params.get("geometry", "")).split(","))
        return vals if len(vals) == 4 else None
    except ValueError:
        return None


def _pool_for(bb):
    if bb is None:
        return []
    if all(abs(a - b) < 1e-9 for a, b in zip(bb, STATE_BBOX)):
        return FAKE["main_pool"]
    for ref, pool in SHARD_POOLS:
        if all(abs(a - b) < 1e-9 for a, b in zip(bb, ref)):
            return pool
    return FAKE["shard_pool"]


# ---------------- cursor 模式（GET / df.http_json）----------------
def fake_http_json(url, params=None, **kw):
    params = params or {}
    if "returnCountOnly" in params:
        return {"count": FAKE["official"]}
    if url.startswith(df.NAD_OFFICIAL):
        bb = _bbox_of(params)
        pool = _pool_for(bb)
        CALLS["cursor"].append({"where": params.get("where", ""), "bbox": bb,
                                "orderByFields": params.get("orderByFields"),
                                "offset": params.get("resultOffset"),
                                "rc": params.get("resultRecordCount")})
        cur = int(params["where"].split(">")[1].strip()) if ">" in params.get("where", "") else None
        avail = [o for o in pool if cur is None or o > cur]
        return {"features": nad_feats(avail[: int(params.get("resultRecordCount") or 2)])}

    where = params.get("where", "")
    CALLS["tiger"].append({"url": url, "where": where})
    pad = {"PAD": PAD}
    if "/State_County/MapServer/0" in url:
        return {"features": [{"attributes": {"STATE": "10", "NAME": "Delaware", **pad},
                              "geometry": {"rings": [[[0, 0], [0.1, 0], [0.1, 0.1]]]}}]}
    if "/State_County/MapServer/1" in url:
        if not FAKE["county_ok"]:
            return {"features": []}
        hits = [c for c in COUNTY_RINGS if f"COUNTY='{c}'" in where]
        if not hits and "COUNTY" not in where:
            hits = list(COUNTY_RINGS)
        return {"features": [{"attributes": {"COUNTY": c, "GEOID": "1000" + c[-1], **pad},
                              "geometry": {"rings": COUNTY_RINGS[c]}} for c in hits]}
    if "PUMA_TAD_TAZ_UGA_ZCTA" in url:
        return {"features": [{"attributes": {"ZCTA5": "19901", **pad}, "geometry": {"rings": []}}]}
    return {"features": [{"attributes": {"BASENAME": "Dover", **pad}, "geometry": {"rings": []}}]}


# ---------------- ids 模式（POST / fs._post_json）----------------
def _oid_range(where: str):
    m = re.match(r"OBJECTID >= (\d+) AND OBJECTID < (\d+)", str(where or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def fake_post(url, params, timeout=180):
    params = params or {}
    if params.get("outStatistics"):
        bb = _bbox_of(params)
        pool = _pool_of(params)
        rng = _oid_range(params.get("where"))
        if rng:
            pool = [o for o in pool if rng[0] <= o < rng[1]]
        CALLS["post"].append({"kind": "stats", "bbox": bb})
        return {"features": [{"attributes": {"mn": min(pool) if pool else None,
                                            "mx": max(pool) if pool else None,
                                            "n": len(pool)}}]}
    if str(params.get("returnCountOnly", "")).lower() == "true":
        bb = _bbox_of(params)
        CALLS["post"].append({"kind": "count", "bbox": bb})
        return {"count": len(_pool_of(params))}
    if str(params.get("returnIdsOnly", "")).lower() == "true":
        bb = _bbox_of(params)
        is_state = bb is not None and all(abs(a - b) < 1e-9 for a, b in zip(bb, STATE_BBOX))
        CALLS["post"].append({"kind": "ids_only", "bbox": bb, "state": is_state})
        if is_state and FAKE["ids_fail_state"]:
            return {"error": "HTTP 400 fake Cannot perform query"}
        pool = _pool_of(params)
        rng = _oid_range(params.get("where"))
        if rng:
            pool = [o for o in pool if rng[0] <= o < rng[1]]
        # 模拟 Esri 服务端静默上限：不报错，只少给
        if FAKE["ids_cap"] and len(pool) > FAKE["ids_cap"]:
            pool = pool[:FAKE["ids_cap"]]
        return {"objectIds": list(pool)}
    if "objectIds" in params:
        ids = [int(x) for x in str(params["objectIds"]).split(",") if str(x).strip()]
        CALLS["post"].append({"kind": "batch", "n": len(ids)})
        # 模拟“批太大 → 400”：只要批大于阈值就拒（永远不会因重试而变好，只能拆半）
        if FAKE["fail_min_batch"] and len(ids) >= FAKE["fail_min_batch"]:
            return {"error": "HTTP 400 fake batch too big"}
        avail = [i for i in ids if i in set(FAKE["ids_target"])]
        return {"features": nad_feats(avail)}
    return {"error": "unexpected POST params"}


def fake_http_get(url, **kw):
    if url.rstrip("/") == df.OA_HOME.rstrip("/"):
        return (b'<a href="https://data.openaddresses.io/runs/1195302/us/de/new_castle.zip">n</a>'
                b'<a href="https://data.openaddresses.io/runs/1195369/us/de/kent.zip">k</a>'
                b'<a href="https://data.openaddresses.io/runs/9/us/ri/statewide.zip">ri</a>')
    return ZIP


def setup(official=5, main_pool=None, shard_pool=None, county_ok=True,
          ids_target=None, ids_fail_state=False, fail_min_batch=0,
          ids_by_bbox=None, ids_cap=0, poly_pools=None):
    FAKE.update({"official": official, "county_ok": county_ok,
                 "main_pool": list(main_pool if main_pool is not None else range(1, official + 1)),
                 "shard_pool": list(shard_pool or []),
                 "ids_target": list(ids_target or []),
                 "ids_by_bbox": dict(ids_by_bbox or {}),
                 "ids_cap": ids_cap,
                 "poly_pools": dict(poly_pools or {}),
                 "ids_fail_state": ids_fail_state,
                 "fail_min_batch": fail_min_batch})
    for k in CALLS:
        CALLS[k].clear()
    df.nad_official_metadata = lambda: (
        {"maxRecordCount": 2, "objectIdField": "OBJECTID", "name": "nad", "type": "Feature Layer",
         "fields": [{"name": "OBJECTID"}, {"name": "Add_Number"}, {"name": "StNam_Full"}]},
        ["OBJECTID", "Add_Number", "StNam_Full"])
    df.http_json = fake_http_json
    df.http_get = fake_http_get
    fs._post_json = fake_post
    # ★ OA 改流式下载后不能真发网络：夹具直接写 ZIP 到目标文件
    def _fake_stream(url, dest, timeout=0, tries=1):
        Path(dest).write_bytes(ZIP)
        return len(ZIP), "0" * 64
    fs._stream_zip = _fake_stream
    fs.shutil.which = lambda name: str(FAKE_CLI)


def scenario(name, out, mode="cursor", shards="none", extra=None, publish=False, **kw):
    setup(**kw)
    argv = ["--state", "DE", "--out-dir", str(out)] + (
        ["--publish-release"] if publish else ["--no-release"]) + [
            "--overture-format", "parquet", "--nad-mode", mode, "--nad-shards", shards]
    argv += (extra or [])
    rc = fs.main(argv)
    files = sorted(p for p in out.rglob("*") if p.is_file())
    print(f"\n===== {name} → exit {rc} =====")
    for p in files:
        print(f"   {p.stat().st_size:>9,}  {p.relative_to(out)}")
    return rc, files


STORE: dict = {}          # 假的远端 Release：name -> bytes


def install_fake_release():
    STORE.clear()
    fs._gh_env = lambda: {"GH_TOKEN": "fake", "GITHUB_REPOSITORY": "fake/repo"}
    fs.gh_ensure_release = lambda tag: STORE.setdefault("_tag", tag)
    fs.gh_assets = lambda tag: {k: len(v) for k, v in STORE.items() if not k.startswith("_")}
    fs.gh_upload = lambda tag, path, clobber=False: (
        False if (path.name in STORE and not clobber) else
        (STORE.__setitem__(path.name, path.read_bytes()) or True))
    fs.gh_read_asset_text = lambda tag, name: (
        STORE[name].decode("utf-8") if isinstance(STORE.get(name), (bytes, bytearray)) else STORE.get(name))


def nad_of(out):
    m = json.loads((out / "MANIFEST.json").read_text(encoding="utf-8"))
    return next(s for s in m["sources"] if s["source"] == "nad")


def gate_content_assert():
    """S13（2026-10-19 · AK 假封印事故回归）：存在性≠内容。

    旧 gate 只查「源跑了没 / 文件够不够大」，AK 跨经线导致 27 个县全失败、
    counties_ok=0，却照样盖 STATUS=complete。本用例锁死：0 个真县片必须阻断。
    """
    import types

    def mk(counties_ok, failed, empty, total=27):
        return types.SimpleNamespace(
            problems=[],
            status={"nad": {"status": "warn-partial", "mode": "ids+county_shards",
                            "official_count": 100, "rows_fetched": 90},
                    "overture": {"status": "ok"},
                    "openaddresses": {"status": "ok"},
                    "census_tiger": {"status": "ok"}},
            cache={"nad_reference": {"counties_ok": counties_ok, "counties_failed": failed,
                                     "counties_empty": empty, "counties_total": total},
                   "nad_shards": [{"kind": "county"}] * counties_ok},
            files=[], meta={},
            cfg={"state": "AK", "counties": [{"fips": "02020"}] * total},
        )

    try:
        fs.gate(mk(0, 27, 0))
        blocked = "未阻断（回归！）"
    except RuntimeError as exc:
        blocked = str(exc)
    try:
        fs.gate(mk(3, 0, 24))
        ok_msg = "通过"
    except RuntimeError as exc:
        ok_msg = f"误伤：{exc}"
    good = ("县级分片为 0" in blocked) and ok_msg == "通过"
    print("\n===== S13 gate 内容断言（AK 假封印回归） =====")
    print(f"  counties_ok=0  → {blocked[:90]}")
    print(f"  counties_ok=3  → {ok_msg}")
    print("S13 判定：", "✅ 通过" if good else "❌ 失败")
    return good


def oa_gate_assert():
    """S15（2026-10-20 · OA 静默截断回归）：上游 70 个源只落了 60 个 → 必须阻断。

    事故：`order[:oa_max_files]`（默认 60）把 MN 的 66 个源默默砍成 60，
    run 仍报 success、gate 不拦、二仓拿到残缺数据也不会知道。
    """
    import types

    def mk(up, fet, emp, fai, allow=False):
        return types.SimpleNamespace(
            problems=[],
            status={"nad": {"status": "ok", "mode": "ids", "official_count": 10,
                            "rows_fetched": 10},
                    "overture": {"status": "ok"},
                    "openaddresses": {"status": "ok", "upstream_sources": up,
                                      "fetched_sources": fet, "empty_sources": emp,
                                      "failed_sources": fai},
                    "census_tiger": {"status": "ok"}},
            cache={"nad_reference": {}}, files=[], meta={},
            cfg={"state": "DE", "counties": [{"fips": "10001"}]},
            args=types.SimpleNamespace(pack_mode="single", allow_partial=allow),
        )

    def errs_of(fx):
        try:
            fs.gate(fx)
            return ""
        except RuntimeError as exc:
            return str(exc)

    a = errs_of(mk(70, 60, 0, 0))         # 不闭合
    b = errs_of(mk(70, 60, 5, 5))         # 闭合但有失败
    c = errs_of(mk(70, 60, 5, 5, True))   # 显式 --allow-partial
    good = ("OA 源数不闭合" in a) and ("OA 有 5 个源失败" in b) and c == ""
    print("\n===== S15 OA 全量声明（静默截断回归） =====")
    print(f"  70→60 不闭合 → {'已阻断' if a else '未阻断（回归）'}")
    print(f"  有 5 个失败   → {'已阻断' if b else '未阻断（回归）'}")
    print(f"  --allow-partial → {'通过' if not c else c[:80]}")
    print("S15 判定：", "✅ 通过" if good else "❌ 失败")
    return good


def main():
    base = Path(tempfile.mkdtemp(prefix="fsr-test-"))
    results = {}
    results["S13"] = gate_content_assert()
    results["S15"] = oa_gate_assert()

    # ---------- S1 cursor 四源齐全 ----------
    rc1, f1 = scenario("S1 cursor 四源齐全", base / "s1", mode="cursor",
                       official=5, main_pool=range(1, 6))
    nd1 = base / "s1" / "nad" / "nad_us_de_full.ndjson"
    n1 = len(nd1.read_text().splitlines()) if nd1.exists() else 0
    n_cnty = len([p for p in f1 if p.name.startswith("tiger_county")])
    n_oa = len([p for p in f1 if p.name.startswith("openaddresses_us_de_")])
    n_ov = len([p for p in f1 if p.name.lower().startswith("overture")])
    results["S1"] = (rc1 == 0 and n1 == 5 and n_cnty == 3 and n_oa == 2 and n_ov == 3
                     and all(c["orderByFields"] == "OBJECTID ASC" for c in CALLS["cursor"])
                     and all(c["offset"] is None for c in CALLS["cursor"])
                     and CALLS["cursor"][1]["where"] == "OBJECTID > 2")
    print(f"NAD 行数={n1}｜县界={n_cnty}｜OA={n_oa}｜Overture={n_ov}")
    print("S1 判定：", "✅ 通过" if results["S1"] else "❌ 失败")

    # ---------- S2 县界全空 ----------
    rc2, f2 = scenario("S2 县界全空", base / "s2", mode="cursor",
                       official=5, main_pool=range(1, 6), county_ok=False)
    empty = [p.name for p in f2 if p.name.startswith("tiger_county")]
    results["S2"] = (rc2 == 3 and not empty)
    print(f"S2 判定：{'✅ 通过' if results['S2'] else '❌ 失败'}｜退出={rc2}（期望3）｜残留空壳={empty}")

    # ---------- S3 cursor 游标漏行 → 4 分块自愈 ----------
    rc3, _ = scenario("S3 cursor 游标漏行→自愈", base / "s3", mode="cursor",
                      official=10, main_pool=range(1, 6), shard_pool=range(6, 11))
    nd3 = base / "s3" / "nad" / "nad_us_de_full.ndjson"
    n3 = len(nd3.read_text().splitlines()) if nd3.exists() else 0
    nad3 = nad_of(base / "s3")
    results["S3"] = (rc3 == 0 and n3 == 10 and nad3.get("truncated") is False
                     and (nad3.get("repair") or {}).get("rows_added") == 5)
    print(f"NAD 行数={n3}（期望10）｜repair={nad3.get('repair')}")
    print("S3 判定：", "✅ 通过" if results["S3"] else "❌ 失败")

    # ---------- S4 cursor 按县切块并发 ----------
    rc4, f4 = scenario("S4 cursor 按县切块", base / "s4", mode="cursor", shards="county",
                       official=5, main_pool=[])
    nd4 = base / "s4" / "nad" / "nad_us_de_full.ndjson"
    n4 = len(nd4.read_text().splitlines()) if nd4.exists() else 0
    nad4 = nad_of(base / "s4")
    cnt_layer = len([c for c in CALLS["tiger"] if "/State_County/MapServer/1" in c["url"]])
    results["S4"] = (rc4 == 0 and n4 == 5 and nad4.get("shard_count") == 3
                     and cnt_layer == 3 and not list((base / "s4" / "nad").glob("nad_part_*")))
    print(f"NAD 行数={n4}｜分片={nad4.get('shard_count')}｜县界请求={cnt_layer}（期望3）")
    print("S4 判定：", "✅ 通过" if results["S4"] else "❌ 失败")

    # ---------- S5 ids 主路径（含缩批自恢复）----------
    rc5, f5 = scenario("S5 ids 主路径 + 缩批自恢复", base / "s5", mode="ids",
                       official=10, main_pool=[], ids_target=range(1, 11),
                       fail_min_batch=3, extra=["--nad-ids-batch", "10"])
    nd5 = base / "s5" / "nad" / "nad_us_de_full.ndjson"
    n5 = len(nd5.read_text().splitlines()) if nd5.exists() else 0
    nad5 = nad_of(base / "s5")
    batches = [c for c in CALLS["post"] if c["kind"] == "batch"]
    results["S5"] = (rc5 == 0 and n5 == 10
                     and nad5.get("mode") == "ids"
                     and nad5.get("official_ids") == 10
                     and nad5.get("rows_fetched") == 10
                     and nad5.get("missing_ids") == 0
                     and nad5.get("shrinks", 0) >= 1
                     and not CALLS["cursor"])
    print(f"NAD 行数={n5}（期望10）｜mode={nad5.get('mode')}｜官方IDS={nad5.get('official_ids')}｜"
          f"实取={nad5.get('rows_fetched')}｜缺={nad5.get('missing_ids')}｜缩批={nad5.get('shrinks')}｜"
          f"批次数={len(batches)}｜游标调用={len(CALLS['cursor'])}（期望0）")
    print("S5 判定：", "✅ 通过" if results["S5"] else "❌ 失败")

    # ---------- S6 ids 降级链（州级失败 → 县块）----------
    rc6, _ = scenario("S6 ids 降级链", base / "s6", mode="ids", shards="county",
                      official=10, main_pool=[], ids_target=range(1, 11),
                      ids_fail_state=True, extra=["--nad-ids-batch", "5"])
    nd6 = base / "s6" / "nad" / "nad_us_de_full.ndjson"
    n6 = len(nd6.read_text().splitlines()) if nd6.exists() else 0
    nad6 = nad_of(base / "s6")
    kinds = [c["kind"] for c in CALLS["post"]]
    results["S6"] = (rc6 == 0 and n6 == 10 and nad6.get("missing_ids") == 0
                     and str(nad6.get("fallback") or "").startswith("adaptive")
                     and kinds.count("ids_only") >= 2)
    print(f"NAD 行数={n6}（期望10）｜fallback={nad6.get('fallback')}｜"
          f"ids_only 调用={kinds.count('ids_only')}（州级失败后逐县重试）")
    print("S6 判定：", "✅ 通过" if results["S6"] else "❌ 失败")

    # ---------- S7 ids 400k 静默截断 → 四象限递归切分 ----------
    # 真事故复刻：RI 整州 count=569,222 但 returnIdsOnly 只给 400,000（不报错）。
    # 这里用更小的数：整州 12 条、上限 6 条 → 4 个象限各 3 条（低于上限）。
    w, s0, e, n = STATE_BBOX
    mx, my = (w + e) / 2.0, (s0 + n) / 2.0
    quads = [(w, s0, mx, my), (mx, s0, e, my), (w, my, mx, n), (mx, my, e, n)]
    ids_map = {kk(STATE_BBOX): list(range(1, 13))}
    for i, q in enumerate(quads):
        ids_map[kk(q)] = [i * 3 + 1, i * 3 + 2, i * 3 + 3]
    rc7, _ = scenario("S7 ids 上限截断→四象限递归", base / "s7", mode="ids",
                      official=12, main_pool=[], ids_target=range(1, 13),
                      ids_by_bbox=ids_map, ids_cap=6, extra=["--nad-ids-batch", "10"])
    nd7 = base / "s7" / "nad" / "nad_us_de_full.ndjson"
    n7 = len(nd7.read_text().splitlines()) if nd7.exists() else 0
    nad7 = nad_of(base / "s7")
    kinds7 = [c["kind"] for c in CALLS["post"]]
    results["S7"] = (rc7 == 0 and n7 == 12
                     and nad7.get("rows_fetched") == 12
                     and nad7.get("official_count") == 12
                     and nad7.get("coverage_pct") == 100.0
                     and nad7.get("missing_ids") == 0
                     and nad7.get("ids_cap_hits", 0) >= 1
                     and nad7.get("ids_leaves") == 4
                     and kinds7.count("ids_only") == 5)   # 1 州级 + 4 象限
    print(f"NAD 行数={n7}（期望12）｜official_count={nad7.get('official_count')}｜"
          f"覆盖率={nad7.get('coverage_pct')}%｜叶子={nad7.get('ids_leaves')}（期望4）｜"
          f"上限命中={nad7.get('ids_cap_hits')}｜ids_only 调用={kinds7.count('ids_only')}（期望5）")
    print("S7 判定：", "✅ 通过" if results["S7"] else "❌ 失败")

    # ---------- S8 四源自证完整 + 元数据（P0/P2）----------
    rc8, _ = scenario("S8 四源自证 + 元数据", base / "s8", mode="ids", official=4,
                      main_pool=[], ids_target=range(1, 5))
    man8 = json.loads((base / "s8" / "MANIFEST.json").read_text(encoding="utf-8"))
    comp8 = man8.get("completeness") or {}
    q8 = man8.get("quality") or {}
    dist8 = q8.get("state_distribution") or {}
    res = {
        "退出0": rc8 == 0,
        "四源齐": set(comp8) == {"note", "nad", "overture", "openaddresses", "census_tiger"},
        "nad对账": comp8.get("nad", {}).get("evidence_1_full") is True,
        "tiger双头对账": comp8.get("census_tiger", {}).get("evidence_1_full") is True,
        "oa行数": comp8.get("openaddresses", {}).get("local_rows", 0) > 0,
        "state分布": dist8.get("DE") == 4 and sum(dist8.values()) == 4,
        "sha256全文64位": all(len(a.get("sha256", "")) == 64 for a in man8["assets"]),
        "sha256_head16位": all(len(a.get("sha256_head", "")) == 16 for a in man8["assets"]),
        "row_counts": bool(man8.get("row_counts")),
    }
    results["S8"] = all(res.values())
    print(f"  completeness.nad={comp8.get('nad', {}).get('upstream_declared_rows')}→{comp8.get('nad', {}).get('local_rows')}｜"
          f"state_distribution={dist8}｜资产摘要长度={len(man8['assets'][0].get('sha256', ''))}/"
          f"{len(man8['assets'][0].get('sha256_head', ''))}｜row_counts键={len(man8.get('row_counts') or {})}")
    bad8 = [k for k, v in res.items() if not v]
    print("S8 判定：", "✅ 通过" if results["S8"] else f"❌ 失败 {bad8}")

    # ---------- S9 OID 区间二分（通用版破 400k 上限）----------
    rc9, _ = scenario("S9 OID 区间二分", base / "s9", mode="ids",
                      official=12, main_pool=[], ids_target=range(1, 13),
                      ids_cap=6, extra=["--nad-ids-slicing", "oid", "--nad-ids-batch", "10"])
    nd9 = base / "s9" / "nad" / "nad_us_de_full.ndjson"
    n9 = len(nd9.read_text().splitlines()) if nd9.exists() else 0
    nad9 = nad_of(base / "s9")
    kinds9 = [c["kind"] for c in CALLS["post"]]
    results["S9"] = (rc9 == 0 and n9 == 12
                     and nad9.get("official_count") == 12
                     and nad9.get("coverage_pct") == 100.0
                     and nad9.get("missing_ids") == 0
                     and kinds9.count("stats") >= 1        # 先聚合拿 min/max/count
                     and kinds9.count("ids_only") >= 2)    # 区间被拆开多次取
    print(f"NAD 行数={n9}（期望12）｜coverage={nad9.get('coverage_pct')}%｜"
          f"stats调用={kinds9.count('stats')}｜ids_only调用={kinds9.count('ids_only')}｜"
          f"上限命中={nad9.get('ids_cap_hits')}")
    print("S9 判定：", "✅ 通过" if results["S9"] else "❌ 失败")

    # ---------- S10 按县切片 + gzip（v1 主路径）----------
    # 5 个点落在 3 个县多边形内（2/2/1），另 2 个在州 bbox 内但不属任何县 → 溢出包
    rc10, _ = scenario("S10 county_shards 切片+gzip", base / "s10", mode="ids",
                       official=7, main_pool=[], ids_target=range(1, 8),
                       poly_pools={"001": [1, 2], "003": [3, 4], "005": [5]},
                       extra=["--pack-mode", "county_shards",
                              "--nad-ids-slicing", "oid", "--nad-ids-batch", "10"])
    import gzip as _gz
    d10 = base / "s10" / "nad"
    got10 = {p.name: len(_gz.open(p, "rt", encoding="utf-8").read().strip().splitlines())
             for p in sorted(d10.glob("*.ndjson.gz"))}
    man10 = json.loads((base / "s10" / "MANIFEST.json").read_text(encoding="utf-8"))
    sh10 = man10.get("shards") or []
    nad10 = nad_of(base / "s10")
    res10 = {
        "退出0": rc10 == 0,
        "三县片存在": all(f"NAD-US-DE-1000{i}-{k}.ndjson.gz" in got10
                         for i, k in ((1, "kent"), (3, "new_castle"), (5, "sussex"))),
        "溢出包存在": "NAD-US-DE-OUTOFSTATE.ndjson.gz" in got10,
        "分片行数正确": sorted(got10.values()) == [1, 2, 2, 2],
        "MANIFEST分片清单": len(sh10) == 4,
        "双摘要齐全": all(len(s.get("sha256_gz", "")) == 64 and len(s.get("sha256_raw", "")) == 64
                          for s in sh10),
        "三数咬合": nad10.get("coverage_pct") == 100.0 and nad10.get("official_count") == 7,
        "places命名": "TIGER-US-DE-places.zip" in [p.name for p in (base / "s10").rglob("*")],
        "分片方法文案": "县多边形并集" in (man10["completeness"]["nad"].get("method") or ""),
        "命名统一": all(p.name.startswith(("NAD-US-DE-", "TIGER-US-DE-", "OA-US-DE-",
                                          "OVERTURE-US-DE"))
                        or p.name in ("MANIFEST.json", "RELEASE_NOTES.md")
                        for p in (base / "s10").rglob("*") if p.is_file()),
    }
    results["S10"] = all(res10.values())
    print(f"  分片: {got10}")
    print(f"  coverage={nad10.get('coverage_pct')}%｜溢出={nad10.get('overflow_rows')}｜"
          f"县界重复={nad10.get('boundary_duplicate_rows')}｜shards={len(sh10)}")
    bad10 = [k for k, v in res10.items() if not v]
    print("S10 判定：", "✅ 通过" if results["S10"] else f"❌ 失败 {bad10}")

    # ---------- S12 县级核验为准入门槛（一个县坏 → 只丢那个县）----------
    # 设计：州主键 = [1,2,3,5,6,7]；001=2、003=2、005=1；003 的 4 号主键在批取层取不到
    #   → 003 判失败（取 1 / 官方 2）→ 不产片、也不让它混进溢出包；
    #   州级三数参考允许不咬合（差 1 = 失败县那条已解析却未交付的主键），但不阻断发布
    rc13, _ = scenario("S12 单县失败只丢该县", base / "s12", mode="ids", official=6,
                       main_pool=[], ids_target=[1, 2, 3, 5, 6, 7],
                       poly_pools={"001": [1, 2], "003": [3, 4], "005": [5]},
                       extra=["--pack-mode", "county_shards",
                              "--nad-ids-slicing", "oid", "--nad-ids-batch", "10",
                              # ★ 2026-10-21：带缺发布现在必须显式放行（第九把锁）
                              "--allow-partial"])
    import gzip as _gz12
    d12 = base / "s12" / "nad"
    got12 = {p.name: len(_gz12.open(p, "rt", encoding="utf-8").read().strip().splitlines())
             for p in sorted(d12.glob("*.ndjson.gz"))}
    man13 = json.loads((base / "s12" / "MANIFEST.json").read_text(encoding="utf-8"))
    ref13 = man13.get("reference_check") or {}
    tn13 = ref13.get("three_number_check") or {}
    nad13 = nad_of(base / "s12")
    res13 = {
        "退出0(不因单县阻断)": rc13 == 0,
        "好县照常产片": all(f"NAD-US-DE-1000{i}-{k}.ndjson.gz" in got12
                           for i, k in ((1, "kent"), (5, "sussex"))),
        "坏县不产片": "NAD-US-DE-10003-new_castle.ndjson.gz" not in got12,
        "坏县记录在案": [f.get("county_fips") for f in ref13.get("failed_counties") or []] == ["10003"],
        "坏县主键未漏进溢出": got12.get("NAD-US-DE-OUTOFSTATE.ndjson.gz") == 2,
        "州级仅参考不阻断": tn13.get("matches") is False and tn13.get("gap_ids") == 1,
        "县计数正确": (ref13.get("counties_ok"), ref13.get("counties_failed")) == (2, 1),
        "nad状态warn": nad13.get("status") in ("ok", "warn-truncated", "warn-partial"),
    }
    results["S12"] = all(res13.values())
    print(f"  分片: {got12}")
    print(f"  参考: ok={ref13.get('counties_ok')} failed={ref13.get('counties_failed')} "
          f"gap={tn13.get('gap_ids')} matches={tn13.get('matches')}")
    bad13 = [k for k, v in res13.items() if not v]
    print("S12 判定：", "✅ 通过" if results["S12"] else f"❌ 失败 {bad13}")

    # ---------- S16 部分县失败 → 没显式 --allow-partial 必须阻断封印 ----------
    # 2026-10-21 用户硬口径「差额必须 0」：S12 那种「一个县坏照常发布」现在必须显式放行。
    rc16, _ = scenario("S16 失败县未放行→门禁阻断", base / "s16", mode="ids", official=6,
                       main_pool=[], ids_target=[1, 2, 3, 5, 6, 7],
                       poly_pools={"001": [1, 2], "003": [3, 4], "005": [5]},
                       extra=["--pack-mode", "county_shards",
                              "--nad-ids-slicing", "oid", "--nad-ids-batch", "10"])
    results["S16"] = (rc16 == 3)
    print(f"S16：退出={rc16}（期望 3 = 门禁阻断，不许封印）")
    print("S16 判定：", "✅ 通过" if results["S16"] else "❌ 失败")

    # ---------- S17 账本字段口径（2026-10-21 修的一批账本 bug）----------
    import types as _t
    st17 = {
        "NAD-US-DE-10003-new_castle.ndjson.gz": {
            "kind": "county", "county_fips": "10003", "county_key": "new_castle",
            "county_name": "New Castle",
            "file": "data/raw/US-DE/nad/NAD-US-DE-10003-new_castle.ndjson.gz",
            "rows": 12, "expected_rows": 12, "gz_bytes": 345, "bytes": 345,
            "sha256_gz": "ab" * 32, "sha256_raw": "cd" * 32},
        "STATUS.json": {"bytes": 10},
        "MANIFEST.json": {"bytes": 20},
    }
    fx17 = _t.SimpleNamespace(status_doc={"assets": st17}, files=[], meta={},
                              tag="raw-US-DE-v1", cache={}, cfg={"state": "DE"})
    sh17 = fs._shards_from_status(fx17)
    res17 = {
        "state_of_tag不返回US": (fs._state_of_tag("raw-US-DE-v1-p2") == "DE"
                                 and fs._state_of_tag("raw-US-TX-v1") == "TX"),
        "自愈片带fips": [s.get("county_fips") for s in sh17] == ["10003"],
        "自愈片带key/name": bool(sh17) and sh17[0].get("county_key") == "new_castle"
                            and bool(sh17[0].get("county_name")),
        "自愈片保留全路径": bool(sh17) and "/" in str(sh17[0].get("file"))
                            and str(sh17[0].get("file")).endswith(".ndjson.gz"),
        "自愈片保留双摘要": bool(sh17) and sh17[0].get("sha256_gz") == "ab" * 32
                            and sh17[0].get("sha256_raw") == "cd" * 32,
        "元信息不进assets": all(a["path"] not in ("STATUS.json", "MANIFEST.json",
                                                "RELEASE_NOTES.md")
                                for a in fs._all_assets(fx17)),
    }
    results["S17"] = all(res17.values())
    print(f"  S17: {res17}")
    print("S17 判定：", "✅ 通过" if results["S17"]
          else f"❌ 失败 {[k for k, v in res17.items() if not v]}")

    # ---------- S11 增量发布 + 断点续传（Step 2）----------
    install_fake_release()
    rc11, _ = scenario("S11 增量发布(首跑)", base / "s11", mode="ids", official=7,
                       main_pool=[], ids_target=range(1, 8),
                       poly_pools={"001": [1, 2], "003": [3, 4], "005": [5]},
                       publish=True,
                       extra=["--pack-mode", "county_shards", "--nad-ids-slicing", "oid",
                              "--nad-ids-batch", "10"])
    st11 = json.loads(STORE.get("STATUS.json", b"{}").decode("utf-8"))
    local_gz = list((base / "s11" / "nad").glob("*.ndjson.gz"))
    first_batches = [c for c in CALLS["post"] if c["kind"] == "batch"]
    r1 = {
        "首跑成功": rc11 == 0,
        "STATUS采集完成": st11.get("phase") == "complete",
        "状态机字段": all(k in st11 for k in ("expected", "done", "assets")),
        "三县已标记": len([d for d in st11.get("done") or []]) >= 3,
        "分片已上传": all(n in STORE for n in ("NAD-US-DE-10001-kent.ndjson.gz",
                                              "NAD-US-DE-10003-new_castle.ndjson.gz",
                                              "NAD-US-DE-10005-sussex.ndjson.gz",
                                              "NAD-US-DE-OUTOFSTATE.ndjson.gz")),
        "本地已清理": not local_gz,
        "MANIFEST已封印": "MANIFEST.json" in STORE,
        "首跑有批取": len(first_batches) > 0,
    }
    # 第二跑：全部命中断点，不应再发 objectIds 批取
    rc12, _ = scenario("S11 断点续传(重跑)", base / "s11b", mode="ids", official=7,
                       main_pool=[], ids_target=range(1, 8),
                       poly_pools={"001": [1, 2], "003": [3, 4], "005": [5]},
                       publish=True,
                       extra=["--pack-mode", "county_shards", "--nad-ids-slicing", "oid",
                              "--nad-ids-batch", "10"])
    second_batches = [c for c in CALLS["post"] if c["kind"] == "batch"]
    st12 = json.loads(STORE.get("STATUS.json", b"{}").decode("utf-8"))
    man12 = json.loads((base / "s11b" / "MANIFEST.json").read_text(encoding="utf-8"))
    r2 = {
        "重跑成功": rc12 == 0,
        "零批取(全跳过)": len(second_batches) == 0,
        "分片仍在": len(man12.get("shards") or []) == 4,
        "行数未变": sum(x.get("rows", 0) for x in man12.get("shards") or []) == 7,
        "STATUS仍完整": st12.get("phase") == "complete",
        "资产未被重传覆盖": len([k for k in STORE if k.startswith("NAD-US-") and k.endswith(".ndjson.gz")]) == 4,
        "其它源也是新命名": all(
            not k.startswith(("tiger_", "tl_2024_", "openaddresses_", "overture_"))
            for k in STORE if k.endswith((".zip", ".geojson", ".parquet", ".json", ".log"))),
    }
    results["S11"] = all(r1.values()) and all(r2.values())
    print(f"  首跑: {r1}")
    print(f"  重跑: {r2}")
    bad11 = [k for k, v in {**r1, **r2}.items() if not v]
    print("S11 判定：", "✅ 通过" if results["S11"] else f"❌ 失败 {bad11}")

    # ---------- S14 分卷（--volume 1/2）：只做本卷县、不产溢出包 ----------
    rc14, f14 = scenario("S14 分卷 1/2", base / "s14", mode="ids", official=7,
                         main_pool=[], ids_target=range(1, 8),
                         poly_pools={"001": [1, 2], "003": [3, 4], "005": [5]},
                         extra=["--volume", "1/2", "--pack-mode", "county_shards",
                                "--nad-ids-slicing", "oid", "--nad-ids-batch", "10"])
    nad14 = [p for p in f14 if p.name.startswith("NAD-") and p.name.endswith(".ndjson.gz")]
    man14 = json.loads((base / "s14" / "MANIFEST.json").read_text(encoding="utf-8"))
    vol = man14.get("volume") or {}
    ov14 = [p.name for p in f14 if "OUTOFSTATE" in p.name]
    results["S14"] = (rc14 == 0 and len(nad14) == 2 and vol.get("index") == 1
                      and vol.get("total") == 2 and vol.get("state_total_counties") == 3
                      and vol.get("this_volume_counties") == 2 and not ov14)
    print(f"S14：退出={rc14} 县片={len(nad14)}（期望2） volume={vol} 溢出包={ov14}")
    print("S14 判定：", "✅ 通过" if results["S14"] else "❌ 失败")

    shutil.rmtree(base, ignore_errors=True)
    print("\n总判定：", f"✅ {len(results)} 个场景全部符合预期" if all(results.values()) else f"❌ {results}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
