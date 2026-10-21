#!/usr/bin/env python3
"""Dover, DE 四源取样机器人（跑在 GitHub Actions 上，绝不在本地服务器下载）

锚定参数（四源统一口径）：
  州  DE / FIPS 10       县  Kent County / FIPS 10001
  市  Dover / Place FIPS 1021200        邮编 19901（扩展 19904）
  BBox [-75.60, 39.10, -75.45, 39.22]  = [西经, 南纬, 东经, 北纬]

四个数据源（全部走"切片/过滤/小包"，绝不下全美总包）：
  1. Overture Maps (addresses)  —— `overturemaps download --bbox=...` 云端按框切
  2. USDOT NAD —— **只用交通部官方正牌三通道**（机构 ID xOi1kZaI0eWDREZv）：
       通道 A 官方 GeoServices REST（FeatureServer/0，动态解析全部字段 + envelope 分页）
       通道 B 官方 OGC WFS 双活备份（dservices.arcgis.com，GetCapabilities → GetFeature）
       通道 C data.transportation.gov 的 Release 24 样本附件（校验第 24 版列结构）
     🚫 严禁使用第三方阉割镜像（Do88DoK2xjTUCXd1 / USA_NAD_Addresses：仅 9 个 OSM 简化字段、2023 停更）
  3. OpenAddresses.io           —— us/de 州级小包（自动发现最新 run id）
  4. Census TIGERweb (REST)     —— ZCTA5 / Incorporated Places / Counties / States
                                   + TIGER/Line 小包（tl_2024_10_place.zip 等）

产物（全部落在仓库里，由 workflow 提交回 main）：
  data/dover/raw/*              各源原始产物（>4MB 自动转 .gz）
  data/dover/*.csv              各源 19901 抽样
  data/dover/nad_dover_full.csv NAD 全字段原始落库
  data/dover/align_src_*.csv    各源主干道全量命中（MAIN/STATE）
  data/dover/align_19901_main_state.csv  三源并排对齐样本
  data/dover/manifest.json      状态/耗时/条数/字节/字段清单/首 3 条记录
  docs/dover_report.md          人类可读报告（含正牌字段清单与验收样本）
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

# ---------------------------------------------------------------- 锚定参数
STATE = os.environ.get("DOVER_STATE", "DE").upper()
STATE_FIPS = os.environ.get("DOVER_STATE_FIPS", "10")
COUNTY = os.environ.get("DOVER_COUNTY", "Kent County")
COUNTY_FIPS = os.environ.get("DOVER_COUNTY_FIPS", "001")
CITY = os.environ.get("DOVER_CITY", "Dover")
CITY_PLACE_FIPS = os.environ.get("DOVER_PLACE_FIPS", "1021200")
ZIP_MAIN = os.environ.get("DOVER_ZIP", "19901")
ZIP_ALT = os.environ.get("DOVER_ZIP_ALT", "19904")
BBOX = tuple(float(x) for x in os.environ.get("DOVER_BBOX", "-75.60,39.10,-75.45,39.22").split(","))
W, S, E, N = BBOX
ZIPS = [ZIP_MAIN, ZIP_ALT]

SAMPLE_N = int(os.environ.get("DOVER_SAMPLE_N", "500"))
NAD_PAGE = int(os.environ.get("DOVER_NAD_PAGE", "1000"))
NAD_MAX = int(os.environ.get("DOVER_NAD_MAX", "40000"))
BACKOFF = (2.0, 5.0, 10.0)          # 指数退避：官方联邦视图偶发抽风/维护

# ★ runner 出口实证（2026-10-17）：Census/Esri 前置网关对脚本类 UA 更容易拦出"200 + 空 body"，
#   换成常规桌面 UA；同时显式声明 gzip（Tiger/Esri 会 gzip 编码，不解压 → JSONDecodeError char 0）。
UA = {"User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/124.0.0.0 Safari/537.36"),
      "Accept": "*/*",
      "Accept-Encoding": "gzip, deflate",
      "Accept-Language": "en-US,en;q=0.9"}

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "dover"
RAW = OUT / "raw"
DOCS = ROOT / "docs"

# TIGERweb 端点（已探明：全部可匿名查询）
TIGER = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb"
TIGER_ZCTA = f"{TIGER}/PUMA_TAD_TAZ_UGA_ZCTA/MapServer/1"
TIGER_PLACES = f"{TIGER}/Places_CouSub_ConCity_SubMCD/MapServer/4"
TIGER_COUNTY = f"{TIGER}/State_County/MapServer/1"
TIGER_STATE = f"{TIGER}/State_County/MapServer/0"

# OpenAddresses
OA_HOME = "https://results.openaddresses.io/"
OA_RUN = "https://data.openaddresses.io/runs/{run}/us/{st}/{src}.zip"
OA_TARGETS = ["city_of_dover", "kent"]
OA_MAX_ZIP_MB = 300

# NAD —— 只用官方正牌通道
NAD_ITEM = "ddcaa5e1a9e24c27bff3c3ce16ea2944"
NAD_OFFICIAL = ("https://services.arcgis.com/xOi1kZaI0eWDREZv/ArcGIS/rest/services/"
                "Address_Points_from_National_Address_Database_view/FeatureServer/0")
NAD_WFS = ("https://dservices.arcgis.com/xOi1kZaI0eWDREZv/arcgis/services/"
           "National_Address_Database_Public_WFS/WFSServer")
NAD_SOCRATA_META = "https://data.transportation.gov/api/views/fc2s-wawr"
NAD_SOCRATA_FILE = "https://data.transportation.gov/api/views/fc2s-wawr/files/{asset}?download=true"
# 🚫 黑名单：命中即视为配置错误（第三方阉割镜像）
NAD_BANNED = ("do88dok2xjtucxd1", "usa_nad_addresses")
# 验收要点：这些原版字段必须就位
NAD_KEY_ALIASES = {
    "Add_Number": ("add_number", "addno_full", "housenumber"),
    "StreetName": ("streetname", "stnam_full", "st_nam_full", "st_name"),
    "County": ("county",),
    "Plus_4": ("plus_4", "plus4"),
    "Addr_Type": ("addr_type",),
}

# 内部统一字段 ↔ 各源真实字段名（NAD 官方用的是 St_Name / StNam_Full 这套）
NAD_FIELD_HINTS = {
    "number": ("add_number", "addno_full", "addr_housenumber", "housenumber", "number"),
    "street": ("stnam_full", "streetname", "st_name", "addr_street", "street", "street_name"),
    "city": ("post_city", "inc_muni", "addr_city", "city", "municipality"),
    "state": ("state", "addr_state", "stusps"),
    "postcode": ("zip_code", "addr_postcode", "zipcode", "postcode", "zip"),
    "county": ("county",),
    "unit": ("unit", "addr_unit", "unit_number"),
    "plus4": ("plus_4", "plus4"),
    "addr_type": ("addr_type",),
    "source": ("source", "addauth", "source_dataset", "src"),
}

RESULTS: list[dict] = []


def log(msg: str) -> None:
    print(f"[bot] {msg}", flush=True)


def http_get(url: str, *, timeout: int = 60, retries: int = 3, headers: dict | None = None,
             backoff: tuple[float, ...] = BACKOFF) -> bytes:
    """带 UA / 指数退避重试的 GET（2s, 5s, 10s）。4xx 不重试。"""
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip" or body[:2] == b"\x1f\x8b":
                try:
                    body = gzip.decompress(body)
                except Exception:  # noqa: BLE001  非 gzip 流（如截断）→ 原样返回交给上层判错
                    pass
            if not body.strip():
                # ★ 2026-10-17 runner 实证：TIGERweb 偶发 200 + 空 body，直接 json.loads 会
                #   抛 JSONDecodeError(char 0)，看起来像参数错，实际是上游抖动 → 必须当失败重试。
                raise RuntimeError("空响应体（HTTP 200 但 body 为空）")
            return body
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read()[:200].decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            last = RuntimeError(f"HTTP {exc.code} {body}")
            if exc.code in (400, 401, 404):     # 403 值得重试（联邦视图偶发 403）
                break
        except Exception as exc:  # noqa: BLE001
            last = exc
        if attempt < retries:
            time.sleep(backoff[min(attempt - 1, len(backoff) - 1)])
    raise RuntimeError(f"{url} → {last}")


def http_json(url: str, params: dict | None = None, **kw) -> dict:
    query = f"{url}?{urllib.parse.urlencode(params, safe='*,')}" if params else url
    return json.loads(http_get(query, **kw).decode("utf-8", "replace"))


def sha256_head(path: Path, limit: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        h.update(fh.read(limit))
    return h.hexdigest()[:16]


def sha256_full(path: Path, chunk: int = 8 << 20) -> str:
    """整文件 sha256（分块读，内存恒定）。大文件也给全摘要，便于下游字节级校验。"""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for blk in iter(lambda: fh.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def norm_zip(value) -> str:
    return re.sub(r"\D", "", str(value or ""))[:5]


def in_bbox(lon: float, lat: float) -> bool:
    return W <= lon <= E and S <= lat <= N


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def shrink(path: Path, threshold_mb: float = 4.0) -> str:
    """大产物转 gz 再入库 —— 每天跑一轮，不能让 git 历史被几十 MB 明文吹爆。"""
    if path.exists() and path.stat().st_size > threshold_mb * 1024 * 1024:
        gz = path.with_suffix(path.suffix + ".gz")
        with path.open("rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        path.unlink()
        return str(gz.relative_to(ROOT))
    return str(path.relative_to(ROOT))


MAIN_STATE_RE = re.compile(r"\b(MAIN|STATE)\b")
ALIGN_FIELDS = ["source", "number", "street", "city", "state", "postcode", "lon", "lat"]


def write_align_source(source: str, rows: list[dict]) -> int:
    """在**全量**结果上（不是抽样上）筛出 MAIN/STATE 主干道，供三源并排对齐。"""
    hit = [r for r in rows
           if norm_zip(r.get("postcode")) in ZIPS
           and MAIN_STATE_RE.search(str(r.get("street", "")).upper())]
    hit.sort(key=lambda r: (str(r.get("street", "")), str(r.get("number", ""))))
    write_csv(OUT / f"align_src_{source}.csv", hit[:SAMPLE_N], ALIGN_FIELDS[1:])
    return len(hit)


def record(source: str, status: str, **kw) -> dict:
    item = {"source": source, "status": status, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **kw}
    RESULTS.append(item)
    log(f"{source}: {status} {json.dumps({k: v for k, v in kw.items() if k != 'samples'}, ensure_ascii=False)[:400]}")
    return item


# ---------------------------------------------------------------- 1. Overture
def fetch_overture() -> dict:
    t0 = time.time()
    target = RAW / "overture_dover.geojson"
    target.parent.mkdir(parents=True, exist_ok=True)
    cli = shutil.which("overturemaps")
    if not cli:
        return record("overture", "skipped", note="未安装 overturemaps CLI")
    cmd = [cli, "download", f"--bbox={W},{S},{E},{N}", "-f", "geojson", "--type=address", "-o", str(target)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    tail = (proc.stdout or "")[-400:] + (proc.stderr or "")[-400:]
    if proc.returncode != 0 or not target.exists():
        return record("overture", "error", cmd=" ".join(cmd), rc=proc.returncode, tail=tail,
                      elapsed=round(time.time() - t0, 1))

    data = json.loads(target.read_text(encoding="utf-8"))
    rows: list[dict] = []
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        coords = ((feat.get("geometry") or {}).get("coordinates") or [None, None])
        rows.append({
            "number": props.get("number", ""), "street": props.get("street", ""),
            "unit": props.get("unit", ""), "city": props.get("city", ""),
            "state": props.get("region", ""), "postcode": props.get("postcode", ""),
            "lon": coords[0], "lat": coords[1],
        })
    rows_19901 = [r for r in rows if norm_zip(r["postcode"]) in ZIPS]
    write_csv(OUT / "overture_19901.csv", rows_19901[:SAMPLE_N],
              ["number", "street", "unit", "city", "state", "postcode", "lon", "lat"])
    main_state = write_align_source("overture", rows)
    return record("overture", "ok", bbox=list(BBOX), features_bbox=len(rows), features_19901=len(rows_19901),
                  main_state_hits=main_state, sample=SAMPLE_N, bytes=target.stat().st_size,
                  sha256=sha256_head(target), file=shrink(target), elapsed=round(time.time() - t0, 1))


# ---------------------------------------------------------------- 2. NAD 官方正牌三通道
def nad_guard() -> None:
    """黑名单守卫：宁可报错，也不许再退回第三方阉割镜像。"""
    for url in (NAD_OFFICIAL, NAD_WFS, NAD_SOCRATA_META):
        low = url.lower()
        for bad in NAD_BANNED:
            if bad in low:
                raise RuntimeError(f"配置错误：NAD 端点命中黑名单 {bad} → {url}")


def nad_official_metadata() -> tuple[dict, list[str]]:
    """通道 A 第一步：动态解析图层元数据里的**全部**真实字段名（约 39 个）。"""
    meta = http_json(NAD_OFFICIAL, {"f": "json"}, timeout=90)
    if meta.get("error"):
        raise RuntimeError(f"元数据 error：{str(meta['error'])[:160]}")
    drop = {"shape", "shape_area", "shape_length", "shape__area", "shape__length", "stgeometry"}
    fields = [str(f.get("name")) for f in meta.get("fields") or []
              if str(f.get("type", "")).lower() != "esrifieldtypegeometry"
              and str(f.get("name", "")).lower() not in drop]
    if not fields:
        raise RuntimeError("元数据 fields 为空")
    return meta, fields


def nad_official_pull(fields: list[str], limit: int = NAD_MAX) -> list[dict]:
    """通道 A：where=1=1 + 多佛 envelope 分页（resultRecordCount=1000，Esri JSON）。"""
    out: list[dict] = []
    offset = 0
    while len(out) < limit:
        batch = min(NAD_PAGE, limit - len(out))
        params = {
            "where": "1=1",
            "outFields": ",".join(fields),
            "geometry": ",".join(str(v) for v in BBOX),
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outSR": "4326",
            "resultOffset": offset,
            "resultRecordCount": batch,
            "f": "json",
        }
        payload = http_json(f"{NAD_OFFICIAL}/query", params, timeout=120)
        if payload.get("error"):
            raise RuntimeError(f"query error {str(payload['error'])[:160]} | offset={offset}")
        feats = payload.get("features") or []
        if not feats:
            break
        for feat in feats:
            geom = feat.get("geometry") or {}
            row = dict(feat.get("attributes") or {})
            row["_lon"], row["_lat"] = geom.get("x"), geom.get("y")
            out.append(row)
        offset += len(feats)
        if len(feats) < batch:
            break
    return out


def nad_wfs_capabilities() -> tuple[str, list[str]]:
    """通道 B 第一步：GetCapabilities → 拿到准确 typeNames。"""
    raw = http_get(f"{NAD_WFS}?service=WFS&request=GetCapabilities", timeout=120)
    text = raw.decode("utf-8", "replace")
    (RAW / "nad_wfs_capabilities.xml").write_bytes(raw)
    names = [a or b for a, b in re.findall(r"<wfs:Name>([^<]+)</wfs:Name>|<(?:FeatureType[^>]*)?Name>([^<]+)</Name>", text)]
    names = [n for n in names if n and n.strip().lower() not in ("wfs", "national_address_database_public_wfs")]
    return text, names


def nad_wfs_pull(type_name: str, limit: int = 6000) -> list[dict]:
    """通道 B：标准 GetFeature + bbox（OGC 轴序 lat,lon）+ startIndex 翻页。"""
    out: list[dict] = []
    start = 0
    prev_sig = None
    while len(out) < limit:
        url = (f"{NAD_WFS}?service=WFS&version=2.0.0&request=GetFeature"
               f"&typeNames={urllib.parse.quote(type_name)}"
               f"&bbox={S},{W},{N},{E},urn:ogc:def:crs:EPSG::4326"
               f"&count=1000&startIndex={start}&outputFormat=application/json")
        blob = http_get(url, timeout=180)
        try:
            payload = json.loads(blob.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return out                                   # 服务不认 json 输出格式 → 放弃该通道
        feats = payload.get("features") or []
        if not feats:
            break
        sig = json.dumps(feats[0].get("properties") or {}, sort_keys=True)[:200]
        if sig == prev_sig:                              # 不支持 startIndex → 别死循环
            break
        prev_sig = sig
        for feat in feats:
            row = dict(feat.get("properties") or {})
            coords = ((feat.get("geometry") or {}).get("coordinates") or [None, None])
            row["_lon"], row["_lat"] = coords[0], coords[1]
            out.append(row)
        start += len(feats)
        if len(feats) < 1000:
            break
    return out


def nad_socrata_files() -> tuple[list[dict], list[dict]]:
    """通道 C：data.transportation.gov 元数据 → Release 24 样本附件（校验列结构）。"""
    meta = json.loads(http_get(NAD_SOCRATA_META, timeout=90,
                               headers={"Accept": "application/json"}).decode("utf-8", "replace"))
    atts = (meta.get("metadata") or {}).get("attachments") or []
    saved: list[dict] = []
    for att in atts:
        name = str(att.get("filename") or att.get("name") or "")
        up = name.upper()
        if not ("SAMPLE" in up or "PARSING" in up or "TECHNICAL" in up):
            continue
        asset = att.get("assetId") or att.get("id") or att.get("fileId")
        if not asset:
            continue
        try:
            blob = http_get(NAD_SOCRATA_FILE.format(asset=asset) + f"&filename={urllib.parse.quote(name)}",
                            timeout=180)
            target = RAW / name
            target.write_bytes(blob)
            info = {"file": name, "bytes": len(blob), "asset_id": asset,
                    "path": str(target.relative_to(ROOT))}
            if up.endswith(".TXT") and "SAMPLE" in up:
                head = blob.decode("utf-8", "replace").splitlines()[:2]
                info["header_fields"] = [c.strip() for c in re.split(r"\t|\|", head[0]) if c.strip()][:60]
            saved.append(info)
        except Exception as exc:  # noqa: BLE001
            saved.append({"file": name, "asset_id": asset, "error": str(exc)[:160]})
    return saved, atts


def nad_field_check(fields: list[str]) -> dict:
    low = {f.lower(): f for f in fields}
    found, missing = {}, []
    for canonical, aliases in NAD_KEY_ALIASES.items():
        hit = next((low[a] for a in aliases if a in low), "")
        if hit:
            found[canonical] = hit
        else:
            missing.append(canonical)
    return {"present": found, "missing": missing}


def fetch_nad() -> dict:
    t0 = time.time()
    nad_guard()
    notes: list[str] = []
    ledger: list[dict] = []
    rows: list[dict] = []
    fields: list[str] = []
    channel = ""

    # ---- 通道 A：官方 GeoServices REST ----
    try:
        meta, fields = nad_official_metadata()
        (RAW / "nad_official_metadata.json").write_text(
            json.dumps({k: meta[k] for k in ("name", "type", "maxRecordCount", "description",
                                             "copyrightText", "fields") if k in meta},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        ledger.append({"channel": "A-official-rest", "url": NAD_OFFICIAL, "ok": True,
                       "fields_n": len(fields), "maxRecordCount": meta.get("maxRecordCount")})
        rows = nad_official_pull(fields)
        channel = "A-official-rest"
    except Exception as exc:  # noqa: BLE001
        ledger.append({"channel": "A-official-rest", "url": NAD_OFFICIAL, "ok": False,
                       "error": str(exc)[:220]})
        notes.append(f"通道 A 失败 →{str(exc)[:140]}")

    # ---- 通道 B：官方 OGC WFS（仅当 A 拿不到数据）----
    if not rows:
        try:
            _caps, type_names = nad_wfs_capabilities()
            pick = next((n for n in type_names if "address" in n.lower()), type_names[0] if type_names else "")
            ledger.append({"channel": "B-official-wfs", "url": NAD_WFS, "typeNames": type_names[:6], "picked": pick})
            if pick:
                rows = nad_wfs_pull(pick)
                channel = "B-official-wfs"
                fields = [k for k in (rows[0].keys() if rows else []) if not k.startswith("_")]
        except Exception as exc:  # noqa: BLE001
            ledger.append({"channel": "B-official-wfs", "url": NAD_WFS, "ok": False, "error": str(exc)[:220]})

    # ---- 通道 C：Release 24 样本附件（无论 A/B 成败都拉，用于列结构对照）----
    socrata_files: list[dict] = []
    socrata_all: list[dict] = []
    try:
        socrata_files, socrata_all = nad_socrata_files()
    except Exception as exc:  # noqa: BLE001
        notes.append(f"通道 C 元数据失败：{str(exc)[:160]}")

    if not rows:
        return record("nad_arcgis", "blocked", channel_tried=ledger, socrata_files=socrata_files,
                      socrata_attachments=[a.get("filename") or a.get("name") for a in socrata_all][:12],
                      notes=notes or ["官方正牌通道均未取到数据"],
                      note="官方正牌 REST + WFS 均未取到数据（禁止回退第三方镜像）",
                      elapsed=round(time.time() - t0, 1))

    # ---- 落库：全字段 + 19901 子集 ----
    clean_fields = [f for f in fields if not f.startswith("_")]
    full_rows = [{**r, "lon": r.get("_lon"), "lat": r.get("_lat")} for r in rows]
    write_csv(OUT / "nad_dover_full.csv", full_rows[:SAMPLE_N], clean_fields + ["lon", "lat"])

    fmap = {k: next((f for f in clean_fields if f.lower() in aliases), "")
            for k, aliases in NAD_FIELD_HINTS.items()}
    mapped = [{
        "number": r.get(fmap.get("number", ""), ""), "street": r.get(fmap.get("street", ""), ""),
        "unit": r.get(fmap.get("unit", ""), ""), "city": r.get(fmap.get("city", ""), ""),
        "state": r.get(fmap.get("state", ""), ""), "postcode": r.get(fmap.get("postcode", ""), ""),
        "lon": r.get("_lon"), "lat": r.get("_lat"),
    } for r in rows]
    zip_ok = [r for r in mapped if norm_zip(r["postcode"]) == ZIP_MAIN]
    write_csv(OUT / "nad_dover_19901.csv", (zip_ok or mapped)[:SAMPLE_N],
              ["number", "street", "unit", "city", "state", "postcode", "lon", "lat"])

    zip_field = fmap.get("postcode", "")
    first3 = []
    if zip_field:
        for r in rows:
            if norm_zip(r.get(zip_field)) == ZIP_MAIN:
                first3.append({k: r.get(k) for k in clean_fields})
            if len(first3) >= 3:
                break
    raw_geojson = RAW / "nad_dover_official.geojson"
    raw_geojson.write_text(json.dumps(
        {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {k: v for k, v in r.items() if k in clean_fields},
             "geometry": {"type": "Point", "coordinates": [r.get("_lon"), r.get("_lat")]}} for r in rows[:20000]]},
        ensure_ascii=False), encoding="utf-8")
    main_state = write_align_source("nad_arcgis", mapped)
    chk = nad_field_check(clean_fields)
    return record("nad_arcgis", "ok", channel=channel, layer=NAD_OFFICIAL, fields_n=len(clean_fields),
                  fields=clean_fields, field_check=chk, rows=len(rows), rows_zip_main=len(zip_ok),
                  first3_zip_main=first3, field_map=fmap, main_state_hits=main_state,
                  sample=SAMPLE_N, channels=ledger, socrata_files=socrata_files, notes=notes,
                  file=shrink(raw_geojson), elapsed=round(time.time() - t0, 1))


# ---------------------------------------------------------------- 3. OpenAddresses
def fetch_openaddresses() -> dict:
    t0 = time.time()
    home = http_get(OA_HOME, timeout=90).decode("utf-8", "replace")
    found = re.findall(r"https://data\.openaddresses\.io/runs/(\d+)/us/([a-z]{2})/([a-z0-9_]+)\.zip", home)
    targets: list[tuple[str, str]] = []
    for src in OA_TARGETS:
        for run, st, name in found:
            if st == STATE.lower() and name == src:
                targets.append((src, OA_RUN.format(run=run, st=st, src=name)))
                break
    if not targets:
        return record("openaddresses", "error", note=f"results 首页里没找到 us/{STATE.lower()} 的 {OA_TARGETS}",
                      discovered=len(found), elapsed=round(time.time() - t0, 1))

    all_rows: list[dict] = []
    detail = []
    for src, url in targets:
        try:
            blob = http_get(url, timeout=600)
            if len(blob) > OA_MAX_ZIP_MB * 1024 * 1024:
                detail.append({"src": src, "url": url, "skipped": f"{len(blob)/1e6:.1f}MB 超上限"})
                continue
            (RAW / f"openaddresses_{src}.zip").write_bytes(blob)
            rows, files = parse_oa_zip(blob)
            all_rows.extend(rows)
            detail.append({"src": src, "url": url, "bytes": len(blob), "csv_members": files,
                           "rows": len(rows), "rows_19901": sum(1 for r in rows if norm_zip(r["postcode"]) in ZIPS)})
        except Exception as exc:  # noqa: BLE001
            detail.append({"src": src, "url": url, "error": str(exc)[:200]})

    hit = [r for r in all_rows if norm_zip(r["postcode"]) in ZIPS]
    if not hit:
        hit = [r for r in all_rows if r.get("lon") and in_bbox(float(r["lon"]), float(r["lat"]))]
    write_csv(OUT / "openaddresses_19901.csv", hit[:SAMPLE_N],
              ["number", "street", "unit", "city", "state", "postcode", "lon", "lat"])
    main_state = write_align_source("openaddresses", all_rows)
    status = "ok" if all_rows else "error"
    return record("openaddresses", status, targets=len(targets), rows=len(all_rows),
                  rows_19901=len(hit), main_state_hits=main_state, sample=SAMPLE_N, detail=detail,
                  elapsed=round(time.time() - t0, 1))


def parse_oa_zip(blob: bytes) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    members: list[str] = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for info in zf.infolist():
            if info.file_size > OA_MAX_ZIP_MB * 1024 * 1024:
                continue
            name = info.filename.lower()
            if not (name.endswith(".csv") or name.endswith(".ndjson") or name.endswith(".geojson")):
                continue
            members.append(info.filename)
            with zf.open(info) as fh:
                text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
                if name.endswith(".csv"):
                    for rec in csv.DictReader(text):
                        low = {str(k).strip().lower(): v for k, v in rec.items()}
                        rows.append({
                            "number": low.get("number", "") or low.get("housenumber", ""),
                            "street": low.get("street", ""), "unit": low.get("unit", ""),
                            "city": low.get("city", ""), "state": low.get("region", "") or low.get("state", ""),
                            "postcode": low.get("postcode", ""), "lon": low.get("lon", ""), "lat": low.get("lat", ""),
                        })
    return rows, members


# ---------------------------------------------------------------- 4. Census TIGER
TIGER_QUERIES = [
    ("zcta", TIGER_ZCTA, f"ZCTA5='{ZIP_MAIN}' OR ZCTA5='{ZIP_ALT}'", "ZCTA5,GEOID"),
    ("place_dover", TIGER_PLACES, f"STATE='{STATE_FIPS}' AND BASENAME='{CITY}'",
     "GEOID,NAME,BASENAME,STATE,PLACE,CENTLAT,CENTLON"),
    ("county_kent", TIGER_COUNTY, f"STATE='{STATE_FIPS}' AND COUNTY='{COUNTY_FIPS}'",
     "GEOID,BASENAME,STATE,COUNTY,NAME"),
    ("state_de", TIGER_STATE, f"STATE='{STATE_FIPS}'", "GEOID,STUSPS,NAME,BASENAME"),
]
TIGER_FILES = [
    ("tl_2024_10_place.zip", "https://www2.census.gov/geo/tiger/TIGER2024/PLACE/tl_2024_10_place.zip"),
    ("tl_2024_10001_addrfeat.zip", "https://www2.census.gov/geo/tiger/TIGER2024/ADDRFEAT/tl_2024_10001_addrfeat.zip"),
]


def fetch_tiger() -> dict:
    t0 = time.time()
    detail = []
    for key, layer, where, fields in TIGER_QUERIES:
        try:
            payload = http_json(f"{layer}/query", {
                "where": where, "outFields": fields, "returnGeometry": "true",
                "outSR": "4326", "f": "json"}, timeout=90)
            feats = payload.get("features") or []
            target = RAW / f"tiger_{key}.json"
            target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            detail.append({"key": key, "layer": layer, "where": where, "count": len(feats),
                           "file": str(target.relative_to(ROOT))})
        except Exception as exc:  # noqa: BLE001
            detail.append({"key": key, "layer": layer, "where": where, "error": str(exc)[:180]})

    shp = []
    for name, url in TIGER_FILES:
        try:
            blob = http_get(url, timeout=300)
            (RAW / name).write_bytes(blob)
            info = {"file": name, "bytes": len(blob), "url": url}
            try:
                import shapefile                          # noqa: PLC0415
                with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                    dbf_name = next((n for n in zf.namelist() if n.endswith(".dbf")), None)
                    if dbf_name:
                        reader = shapefile.Reader(dbf=io.BytesIO(zf.read(dbf_name)))
                        info["dbf_records"] = len(reader)
                        info["dbf_fields"] = [f[0] for f in reader.fields if f[0] != "DeletionFlag"][:12]
            except Exception as exc:  # noqa: BLE001
                info["dbf_note"] = str(exc)[:120]
            shp.append(info)
        except Exception as exc:  # noqa: BLE001
            shp.append({"file": name, "url": url, "error": str(exc)[:180]})

    ok = sum(1 for d in detail if "count" in d)
    return record("census_tiger", "ok" if ok else "error", rest_queries=detail, tiger_line_files=shp,
                  elapsed=round(time.time() - t0, 1))


# ---------------------------------------------------------------- 对齐样本
def read_sample(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def build_alignment() -> dict:
    picked: list[dict] = []
    stats = {}
    for source in ("overture", "nad_arcgis", "openaddresses"):
        rows = read_sample(OUT / f"align_src_{source}.csv")
        stats[source] = len(rows)
        for row in rows[:SAMPLE_N]:
            picked.append({"source": source, **{k: row.get(k, "") for k in ALIGN_FIELDS[1:]}})
    write_csv(OUT / "align_19901_main_state.csv", picked, ALIGN_FIELDS)
    return {"rows": len(picked), "per_source": stats, "file": "data/dover/align_19901_main_state.csv"}


# ---------------------------------------------------------------- 报告
def write_report(align: dict) -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Dover, DE 四源取样报告（自动生成）",
        "",
        f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())} ｜ 运行位置：**GitHub Actions**（本机服务器零下载）",
        f"> 锚定参数：州 `{STATE}`/FIPS `{STATE_FIPS}`｜县 `{COUNTY}`/FIPS `{STATE_FIPS}{COUNTY_FIPS}`｜"
        f"市 `{CITY}`/Place FIPS `{CITY_PLACE_FIPS}`｜邮编 `{'`, `'.join(ZIPS)}`｜"
        f"BBox `[{W}, {S}, {E}, {N}]`",
        "",
        "## 0. 四源状态",
        "",
        "| 源 | 状态 | 关键数字 | 备注 |",
        "|---|---|---|---|",
    ]
    for item in RESULTS:
        key_bits = []
        for k in ("features_bbox", "features_19901", "rows", "rows_zip_main", "main_state_hits",
                  "fields_n", "channel", "targets"):
            if k in item:
                key_bits.append(f"{k}={item[k]}")
        if "rest_queries" in item:
            key_bits.append("REST=" + ",".join(f"{d['key']}:{d.get('count', d.get('error', '?'))}"
                                               for d in item["rest_queries"]))
        note = item.get("note") or ""
        lines.append(f"| `{item['source']}` | {item['status']} | {' / '.join(map(str, key_bits)) or '-'} | {str(note)[:160]} |")

    nad = next((i for i in RESULTS if i["source"] == "nad_arcgis"), None)
    if nad and nad.get("fields"):
        chk = nad.get("field_check") or {}
        lines += [
            "",
            "## 1. NAD 正牌图层字段清单（交通部官方 xOi1kZaI0eWDREZv）",
            "",
            f"- 通道：`{nad.get('channel')}`　端点：`{nad.get('layer')}`",
            f"- 动态解析到的字段总数：**{nad.get('fields_n')}**",
            "- 关键字段验收："
            + "、".join(f"`{k}`→`{v}`" for k, v in (chk.get("present") or {}).items())
            + (f"　❌ 缺失：{', '.join(chk.get('missing') or [])}" if chk.get("missing") else "　✅ 全部就位"),
            "",
            "```",
            ", ".join(nad["fields"]),
            "```",
            "",
            f"### Zip_Code='{ZIP_MAIN}' 首 3 条完整记录（键值对）",
            "",
        ]
        for i, rec in enumerate(nad.get("first3_zip_main") or [], 1):
            lines += [f"**#{i}**", "", "```json", json.dumps(rec, ensure_ascii=False, indent=1), "```", ""]
        if nad.get("socrata_files"):
            lines += ["### 通道 C：Release 24 官方样本附件", ""]
            for f in nad["socrata_files"]:
                lines.append(f"- `{f.get('file')}` — {f.get('bytes', '?')} B"
                             + (f"｜表头 {len(f.get('header_fields') or [])} 列" if f.get("header_fields") else ""))

    lines += ["", f"## 2. 三源并排对齐样本（邮编 {'/'.join(ZIPS)} + 街道含 MAIN 或 STATE）", "",
              f"合计 **{align['rows']}** 行 → `{align['file']}`",
              f"各源命中：{json.dumps(align['per_source'], ensure_ascii=False)}", ""]

    lines += ["## 3. 产物清单", ""]
    for path in sorted(OUT.rglob("*")):
        if path.is_file():
            lines.append(f"- `{path.relative_to(ROOT)}` — {path.stat().st_size:,} B")
    lines += [
        "",
        "## 4. 口径与已知边界",
        "",
        "- NAD 只走交通部官方正牌三通道（REST → WFS → Socrata 样本）；"
        "**第三方阉割镜像 `Do88DoK2xjTUCXd1 / USA_NAD_Addresses` 已在代码层拉黑**"
        "（它只有 9 个 OSM 简化字段、2023 停更，缺 Plus_4 / County / Post_Comm / LandmkName 与街道 8 级拆分）。",
        "- 官方视图偶发抽风，脚本带 2s/5s/10s 指数退避重试。",
        "- `outFields` 由元数据动态生成（非手写、非 `*`），保证 100% 取到原版字段。",
        "- OpenAddresses 的 run id 每次会变，机器人每次现查首页取最新 zip。",
        "- 三源字段命名各异，本报告只做**并排对齐**，不做合并 —— 合成规则待架构师定稿。",
        "",
    ]
    (DOCS / "dover_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    log(f"目标：{CITY}, {STATE}｜邮编 {ZIPS}｜BBox {BBOX}")
    for fn in (fetch_overture, fetch_nad, fetch_openaddresses, fetch_tiger):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 单源失败绝不拖垮整轮
            record(fn.__name__.replace("fetch_", ""), "exception", error=f"{type(exc).__name__}: {str(exc)[:300]}")
    align = build_alignment()
    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "anchors": {"state": STATE, "state_fips": STATE_FIPS, "county": COUNTY,
                    "county_fips": f"{STATE_FIPS}{COUNTY_FIPS}", "city": CITY,
                    "place_fips": CITY_PLACE_FIPS, "zips": ZIPS, "bbox": list(BBOX)},
        "runner": os.environ.get("GITHUB_RUN_ID", "local"),
        "sources": RESULTS, "alignment": align,
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    write_report(align)
    log("完成：" + json.dumps({i["source"]: i["status"] for i in RESULTS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
