#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三源产出物核查（TIGER / OpenAddresses / Overture）—— 单文件，含护栏。

设计原则（2026-10-23 定）：
  1. **默认在 GitHub Actions runner 上跑**，本机只做几 MB 级的小样本验证。
  2. 本机跑的时候必须有护栏：内存上限 / 磁盘余量 / STOP 开关 / 单文件顺序处理。
  3. 一次只处理一个 tag，产物是几百 KB 的 JSON，读一个删一个。

用法：
  python3 tools/source_audit.py --source tiger   --tag raw-US-CA-v1
  python3 tools/source_audit.py --source oa      --tag raw-US-CA-v1
  python3 tools/source_audit.py --source overture --tag raw-US-CA-v1
  python3 tools/source_audit.py --source oa --tag raw-US-VA-v1 --limit 5   # 小样本验证
  python3 tools/source_audit.py --check-env                                # 只检查护栏

输出：counts_audit/<source>/<TAG>.json
"""
from __future__ import annotations
import argparse, csv, glob, gzip, io, json, os, re, struct, subprocess, sys, tempfile, time, zipfile

REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"
STOP_FILE = os.environ.get("STOP_FILE") or "/root/pi-work/STOP"
MIN_DISK_GB = float(os.environ.get("MIN_DISK_GB") or 5)
MAX_VM_MB = int(os.environ.get("MAX_VM_MB") or 400)
RATE = os.environ.get("CURL_RATE") or "30M"


# ---------------------------------------------------------------- 护栏
class GuardError(SystemExit):
    pass


def check_env(purpose: str = "") -> None:
    """三道闸：STOP 开关 / 磁盘余量 / 内存上限。任一不过直接退出（不拖累宿主机）。"""
    if os.path.exists(STOP_FILE):
        raise GuardError(f"[guard] STOP 开关存在（{STOP_FILE}），拒绝执行 {purpose}")
    st = os.statvfs("/")
    free_gb = st.f_bavail * st.f_frsize / 2**30
    if free_gb < MIN_DISK_GB:
        raise GuardError(f"[guard] 磁盘余量 {free_gb:.1f} GB < {MIN_DISK_GB} GB，拒绝执行 {purpose}")
    try:
        import resource
        cap = MAX_VM_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    except Exception as e:  # 非 Linux 或无权限时降级为警告
        print(f"[guard] 内存上限设置失败（继续跑）：{e}")
    print(f"[guard] OK {purpose}｜磁盘余量 {free_gb:.1f} GB｜内存上限 {MAX_VM_MB} MB")


def fetch(url: str, path: str, timeout: int = 1800) -> bool:
    """走 CDN 下载（不消耗 GITHUB_TOKEN 配额）+ 限速 + 重试。"""
    for i in range(3):
        if os.path.exists(STOP_FILE):
            return False
        rc = subprocess.run(["curl", "-fsSL", "--retry", "2", "--retry-delay", "2",
                             "--limit-rate", RATE, "--max-time", str(timeout),
                             "-o", path, url], capture_output=True).returncode
        if rc == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
            return True
        time.sleep(2 * (i + 1))
    print("  [dl-fail]", os.path.basename(path))
    return False


def release_assets(tag: str) -> list[str]:
    r = subprocess.run(["gh", "api", f"repos/{REPO}/releases/tags/{tag}"],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        print("[api-fail]", (r.stderr or r.stdout)[:200])
        return []
    return [a["name"] for a in json.loads(r.stdout).get("assets", [])]


def asset_url(tag: str, name: str) -> str:
    return f"https://github.com/{REPO}/releases/download/{tag}/{name}"


# ---------------------------------------------------------------- 县界（四源共用）
def load_counties(tag: str, st: str, tmp: str):
    """下载该州 TIGER 县界 geojson → shapely 几何（Esri rings，wkid 4326）。"""
    from shapely import prepare
    from shapely.geometry import Polygon
    names = [n for n in release_assets(tag) if n.startswith(f"TIGER-US-{st}-county-")]
    out = []
    for n in names:
        p = os.path.join(tmp, n)
        if not fetch(asset_url(tag, n), p):
            continue
        try:
            doc = json.load(open(p, encoding="utf-8"))
            rings = ((doc["features"][0].get("geometry") or {}).get("rings")) or []
        except Exception as e:
            print("  [geojson-bad]", n, repr(e)[:60]); os.remove(p); continue
        os.remove(p)
        if len(rings) < 1 or len(rings[0]) < 3:
            continue
        try:
            g = Polygon(rings[0], [r for r in rings[1:] if len(r) >= 3])
            if not g.is_valid:
                g = g.buffer(0)
            if g.is_empty:
                continue
            prepare(g)
        except Exception:
            continue
        fips = n.split("-")[-1].split(".")[0]
        x0, y0, x1, y1 = g.bounds
        out.append({"fips": fips, "geom": g, "bounds": (x0, y0, x1, y1), "points": 0, "rows": 0})
    return out


def assign(counties, lons, lats) -> int:
    """把点分给县（第一个命中），返回命中数。"""
    import numpy as np
    from shapely import contains_xy
    n = len(lons)
    assigned = np.zeros(n, dtype=bool)
    for c in counties:
        x0, y0, x1, y1 = c["bounds"]
        m = (~assigned) & (lons >= x0) & (lons <= x1) & (lats >= y0) & (lats <= y1)
        idx = np.nonzero(m)[0]
        if idx.size == 0:
            continue
        hit = contains_xy(c["geom"], lons[idx], lats[idx])
        if not hit.any():
            continue
        sel = idx[hit]
        c["points"] += int(sel.size)
        assigned[sel] = True
    return int(assigned.sum())


# ---------------------------------------------------------------- TIGER
def audit_tiger(tag: str, st: str, tmp: str, limit: int = 0) -> dict:
    """TIGER 性质不同：无「点」，验的是「县齐不齐 + 号段包 dbf/shx 是否自洽 + 几何是否可用」。"""
    names = release_assets(tag)
    geos = sorted(n for n in names if n.startswith(f"TIGER-US-{st}-county-"))
    feats = sorted(n for n in names if n.startswith(f"TIGER-US-{st}-") and "-addrfeat" in n)
    if limit:
        geos, feats = geos[:limit], feats[:limit]
    rows = {"geojson": [], "addrfeat": []}
    for n in geos:
        p = os.path.join(tmp, n)
        rec = {"file": n}
        if not fetch(asset_url(tag, n), p):
            rows["geojson"].append({**rec, "error": "download"}); continue
        try:
            doc = json.load(open(p, encoding="utf-8"))
            f0 = (doc.get("features") or [{}])[0]
            rings = ((f0.get("geometry") or {}).get("rings")) or []
            sr = (doc.get("spatialReference") or {}).get("wkid")
            flat = [pt for r in rings for pt in r]
            xs = [pt[0] for pt in flat]; ys = [pt[1] for pt in flat]
            ring_ok = all(len(r) >= 4 and r[0] == r[-1] for r in rings) if rings else False
            rec.update({"fips": n.split("-")[-1].split(".")[0], "rings": len(rings),
                        "wkid": sr, "ring_closed": ring_ok,
                        "coords_in_range": bool(xs and -180 <= min(xs) and max(xs) <= -60
                                                and 15 <= min(ys) and max(ys) <= 72),
                        "bytes": os.path.getsize(p)})
        except Exception as e:
            rec["error"] = repr(e)[:80]
        os.remove(p)
        rows["geojson"].append(rec)
    for n in feats:
        p = os.path.join(tmp, n)
        rec = {"file": n}
        if not fetch(asset_url(tag, n), p):
            rows["addrfeat"].append({**rec, "error": "download"}); continue
        try:
            with zipfile.ZipFile(p) as z:
                infos = {zi.filename.lower(): zi for zi in z.infolist()}
                dbf = next((k for k in infos if k.endswith(".dbf")), None)
                shx = next((k for k in infos if k.endswith(".shx")), None)
                shp = next((k for k in infos if k.endswith(".shp")), None)
                nd = ns = None
                if dbf:
                    with z.open(infos[shp or dbf]) as h:  # noqa: F841  (占位，保持顺序)
                        pass
                    with z.open(infos[dbf]) as h:
                        head = h.read(8)
                    if len(head) >= 8:
                        nd = struct.unpack("<I", head[4:8])[0]
                if shx:
                    ns = (infos[shx].file_size - 100) // 8
                rec.update({"fips": n.split("-")[-1].split(".")[0],
                            "dbf_records": nd, "shx_records": ns,
                            "records_match": (None if nd is None or ns is None else nd == ns),
                            "has_shp": bool(shp), "bytes": os.path.getsize(p)})
        except Exception as e:
            rec["error"] = repr(e)[:80]
        os.remove(p)
        rows["addrfeat"].append(rec)
    bad = [r for r in rows["addrfeat"] if r.get("records_match") is False or r.get("error")]
    return {"tag": tag, "state": st, "source": "tiger",
            "county_geojson": len(rows["geojson"]), "addrfeat": len(rows["addrfeat"]),
            "geojson_bad": [r for r in rows["geojson"] if r.get("error") or not r.get("ring_closed")
                            or not r.get("coords_in_range")],
            "addrfeat_bad": bad,
            "detail": rows}


# ---------------------------------------------------------------- OpenAddresses
LONKEYS = ("LON", "LONGITUDE", "LON_1", "X", "LONG", "LNG")
LATKEYS = ("LAT", "LATITUDE", "LAT_1", "Y")


def pick_col(header, keys):
    up = {h.strip().upper(): i for i, h in enumerate(header)}
    for k in keys:
        if k in up:
            return up[k]
    return None


def audit_oa(tag: str, st: str, tmp: str, limit: int = 0) -> dict:
    import numpy as np
    names = sorted(n for n in release_assets(tag) if n.startswith("OA-"))
    if limit:
        names = names[:limit]
    counties = load_counties(tag, st, tmp)
    rows, failures = [], []
    total = in_state = 0
    for n in names:
        p = os.path.join(tmp, n)
        rec = {"file": n}
        if not fetch(asset_url(tag, n), p):
            failures.append({**rec, "error": "download"}); continue
        try:
            with zipfile.ZipFile(p) as z:
                csvs = [zi for zi in z.infolist() if zi.filename.lower().endswith(".csv")]
                if not csvs:
                    failures.append({**rec, "error": "no csv（可能是 shapefile 源）"}); continue
                rec["csv_files"] = len(csvs)
                n_rows = n_hit = 0
                for zi in csvs:
                    with z.open(zi) as h:
                        txt = io.TextIOWrapper(h, encoding="utf-8", errors="replace")
                        rd = csv.reader(txt)
                        try:
                            header = next(rd)
                        except StopIteration:
                            continue
                        li, ai = pick_col(header, LONKEYS), pick_col(header, LATKEYS)
                        if li is None or ai is None:
                            failures.append({**rec, "error": f"列名未识别: {header[:8]}"}); continue
                        bufs = []
                        for row in rd:
                            n_rows += 1
                            try:
                                bufs.append((float(row[li]), float(row[ai])))
                            except Exception:
                                pass
                            if len(bufs) >= 200000:
                                lon = np.array([b[0] for b in bufs]); lat = np.array([b[1] for b in bufs])
                                n_hit += assign(counties, lon, lat); bufs = []
                        if bufs:
                            lon = np.array([b[0] for b in bufs]); lat = np.array([b[1] for b in bufs])
                            n_hit += assign(counties, lon, lat)
                rec.update({"rows": n_rows, "in_state_points": n_hit})
                total += n_rows; in_state += n_hit
        except Exception as e:
            failures.append({**rec, "error": repr(e)[:80]})
        os.remove(p)
        rows.append(rec)
        print(f"    {n}: {rec.get('rows', 0):,} 行 / 州内 {rec.get('in_state_points', 0):,}")
    return {"tag": tag, "state": st, "source": "oa",
            "files": len(names), "total_rows": total, "in_state_points": in_state,
            "in_state_pct": round(100.0 * in_state / total, 2) if total else None,
            "counties_with_points": len([c for c in counties if c["points"] > 0]),
            "county_total": len(counties),
            "counties": [{"fips": c["fips"], "points": c["points"]} for c in counties],
            "failures": failures, "detail": rows}


# ---------------------------------------------------------------- Overture
def audit_overture(tag: str, st: str, tmp: str, limit: int = 0) -> dict:
    import numpy as np
    import pyarrow.parquet as pq
    from shapely import from_wkb, get_x, get_y
    names = sorted(n for n in release_assets(tag) if n.upper().startswith("OVERTURE-"))
    if limit:
        names = names[:limit]
    counties = load_counties(tag, st, tmp)
    files = []
    total = in_state = 0
    for n in names:
        p = os.path.join(tmp, n)
        rec = {"file": n}
        if not fetch(asset_url(tag, n), p):
            files.append({**rec, "error": "download"}); continue
        try:
            pf = pq.ParquetFile(p)
            rec["rows"] = pf.metadata.num_rows
            rec["row_groups"] = pf.metadata.num_row_groups
            cols = set(pf.schema_arrow.names)
            rec["has_geometry"] = "geometry" in cols
            n_hit = 0
            if rec["has_geometry"]:
                for i in range(pf.metadata.num_row_groups):
                    g = pf.read_row_group(i, columns=["geometry"]).column(0).to_pylist()
                    pts = from_wkb([b for b in g if b])
                    lon = get_x(pts); lat = get_y(pts)
                    n_hit += assign(counties, np.asarray(lon), np.asarray(lat))
            rec["in_state_points"] = n_hit
            total += rec["rows"] or 0; in_state += n_hit
        except Exception as e:
            rec["error"] = repr(e)[:100]
        os.remove(p)
        files.append(rec)
        print(f"    {n}: {rec.get('rows', 0):,} 行 / 州内 {rec.get('in_state_points', 0):,}")
    return {"tag": tag, "state": st, "source": "overture",
            "files": len(names), "total_rows": total, "in_state_points": in_state,
            "in_state_pct": round(100.0 * in_state / total, 2) if total else None,
            "counties_with_points": len([c for c in counties if c["points"] > 0]),
            "county_total": len(counties),
            "counties": [{"fips": c["fips"], "points": c["points"]} for c in counties],
            "detail": files}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["tiger", "oa", "overture"])
    ap.add_argument("--tag")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个资产（小样本验证）")
    ap.add_argument("--check-env", action="store_true")
    a = ap.parse_args()
    if a.check_env:
        check_env("env-check"); return
    if not a.source or not a.tag:
        ap.error("需要 --source 与 --tag")
    check_env(f"{a.source} {a.tag}")
    st = a.tag.replace("raw-US-", "").split("-v1")[0].split("-")[0]
    tmp = tempfile.mkdtemp(prefix="saudit-")
    fn = {"tiger": audit_tiger, "oa": audit_oa, "overture": audit_overture}[a.source]
    res = fn(a.tag, st, tmp, a.limit)
    os.makedirs(f"counts_audit/{a.source}", exist_ok=True)
    out = f"counts_audit/{a.source}/{a.tag}.json"
    json.dump(res, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.rmdir(tmp) if not os.listdir(tmp) else None
    brief = {k: v for k, v in res.items() if k not in ("detail", "counties")}
    print(json.dumps(brief, ensure_ascii=False)[:600])


if __name__ == "__main__":
    main()
