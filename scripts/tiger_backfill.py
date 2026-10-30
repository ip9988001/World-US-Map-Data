#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TIGER addrfeat 补抓 + 三州重新封印（只动 census_tiger，不碰 NAD/OA/OVERTURE）。

背景（2026-10-30）：AK/GA/ID 的 `sources[census_tiger].status=warn-partial`，
`missed[]` 记录「上游返回非 zip（WAF 拒页/截断）」。逐条 HEAD 上游后确认
77 个文件上游 HTTP 200、字节正常 → 属**采集时的瞬时 WAF**，不是上游缺文件。
本脚本把那 77 个文件补进 Release，并把账本改回 ok。

下载加固（根因修复，写进脚本而不是手工处理）：
  · 校验 Content-Length 存在且 > 0
  · 校验实收字节数 == Content-Length（截断即失败）
  · 校验 zip 魔数 `PK\\x03\\x04` + `zipfile.is_zipfile`
  · 解压读 .dbf 表头（字段数 / 记录数）—— 读不出表头也算失败
  · 任一步失败：退避重试 3 次（2s / 8s / 30s），仍失败才记 failed（不改账本状态）

只动三州：
  · 新增资产名必须过第七把锁正则：TIGER-US-<ST>-<fips>-addrfeat.zip
  · MANIFEST：assets[] 按 Release 实物（bytes/sha256 来自 REST digest）；
    sources[census_tiger].missed=[]、status=ok、upstream_rejected=0
  · STATUS.json：updated_at 刷新，phase 保持 complete

用法：
  GH_TOKEN=... python3 scripts/tiger_backfill.py --state AK --apply
  GH_TOKEN=... python3 scripts/tiger_backfill.py --state AK --selfcheck
  GH_TOKEN=... python3 scripts/tiger_backfill.py --selfcheck-all --report-out docs/X.md
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROSTER = ROOT / "rules" / "US" / "state_roster.yaml"
REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"
BOOK = {"MANIFEST.json", "STATUS.json", "RELEASE_NOTES.md"}
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
BACKOFF = (2, 8, 30)          # 退避重试间隔（秒）
STATES = ("AK", "GA", "ID")

ADDRFEAT_RE = re.compile(r"^TIGER-US-([A-Z]{2})-(\d{5})-addrfeat\.zip$")   # 第七把锁
PLACES_RE = re.compile(r"^TIGER-US-([A-Z]{2})-places\.zip$")
COUNTY_RE = re.compile(r"^TIGER-US-([A-Z]{2})-county-(\d{5})\.geojson$")
# 州界/普查区等非县级边界（契约允许：TIGER-US-<ST>-<layer>.geojson）
BOUNDARY_RE = re.compile(r"^TIGER-US-([A-Z]{2})-(state|tract|zcta|place|places|bg|roads)"
                         r"\.(geojson|zip)$")


def log(m: str) -> None:
    print(m, flush=True)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gh(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)


def release(tag: str) -> dict:
    r = gh(["api", f"repos/{REPO}/releases/tags/{tag}"])
    if r.returncode != 0:
        raise SystemExit(f"取 release {tag} 失败：{r.stderr[:200]}")
    return json.loads(r.stdout)


def roster_cfg(st: str) -> dict:
    return yaml.safe_load(ROSTER.read_text(encoding="utf-8"))["states"][st]


def dbf_header(blob: bytes) -> dict:
    """从 zip 内 .dbf 读表头：记录数 / 头长 / 字段名。读不出即抛异常。"""
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = zf.namelist()
        dbf = next((n for n in names if n.lower().endswith(".dbf")), None)
        shp = next((n for n in names if n.lower().endswith(".shp")), None)
        if not dbf:
            raise ValueError(f"zip 内无 .dbf（成员 {names[:4]}）")
        b = zf.read(dbf)
        if len(b) < 32:
            raise ValueError(".dbf 头长度不足 32B")
        nrec = int.from_bytes(b[4:8], "little")
        hlen = int.from_bytes(b[8:10], "little")
        fields = [b[32 + i * 32:32 + i * 32 + 11].split(b"\x00")[0].decode("ascii", "replace")
                  for i in range(max(0, (hlen - 33) // 32))]
        return {"members": len(names), "dbf": dbf, "shp": shp,
                "records": nrec, "fields_n": len(fields), "fields": fields[:8]}


def fetch_zip(url: str, timeout: float = 300) -> tuple[bytes | None, str, dict]:
    """加固下载：Content-Length>0 + 未截断 + PK 魔数 + zip 可解 + dbf 表头。

    失败按 2/8/30 秒退避重试（共 3 次重试）。返回 (blob|None, last_error, meta)。
    """
    err = "未尝试"
    for attempt in range(len(BACKOFF) + 1):
        if attempt:
            wait = BACKOFF[attempt - 1]
            log(f"    重试 {attempt}/3（等 {wait}s，上次失败：{err}）")
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                cl = r.headers.get("Content-Length")
                blob = r.read()
            if not cl or int(cl) <= 0:
                err = f"Content-Length 缺失/为 0（{cl}）"
                continue
            if len(blob) != int(cl):
                err = f"截断：收到 {len(blob)}B != Content-Length {cl}B"
                continue
            if blob[:4] != b"PK\x03\x04":
                err = f"非 zip 魔数（头 4B={blob[:4]!r}，共 {len(blob)}B，疑似 WAF 拒页）"
                continue
            if not zipfile.is_zipfile(io.BytesIO(blob)):
                err = "zipfile.is_zipfile=False"
                continue
            meta = dbf_header(blob)
            meta.update({"bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest(),
                         "url": url, "attempts": attempt + 1})
            return blob, "", meta
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {str(e)[:80]}"
    return None, err, {}


def upload(tag: str, path: Path) -> bool:
    r = gh(["release", "upload", tag, str(path), "--repo", REPO, "--clobber"])
    if r.returncode != 0:
        log(f"    ✗ 上传 {path.name} 失败：{(r.stderr or r.stdout).strip()[:200]}")
        return False
    log(f"    ✓ 上传 {path.name}（{path.stat().st_size}B）")
    return True


def download_asset(tag: str, name: str, dest: Path) -> Path | None:
    r = gh(["release", "download", tag, "-R", REPO, "-p", name, "-D", str(dest), "--clobber"])
    p = dest / name
    return p if p.exists() and r.returncode == 0 else None


def tiger_base() -> str:
    d = yaml.safe_load(ROSTER.read_text(encoding="utf-8"))
    return d["endpoints"]["tiger_base"].rstrip("/")


def plan(st: str) -> tuple[list[tuple[str, str]], dict]:
    """返回 (缺失资产名, 上游URL), 以及该州现状摘要。"""
    cfg = roster_cfg(st)
    base = tiger_base()
    sf = str(cfg["state_fips"]).zfill(2)
    rel = release(f"raw-US-{st}-v1")
    have = {a["name"] for a in rel.get("assets") or []}
    meta = {a["name"]: a for a in rel.get("assets") or []}

    want: dict[str, str] = {}
    for c in cfg.get("counties") or []:
        fips = str(c["fips"]).zfill(5)
        want[f"TIGER-US-{st}-{fips}-addrfeat.zip"] = f"{base}/ADDRFEAT/tl_2024_{fips}_addrfeat.zip"
    want[f"TIGER-US-{st}-places.zip"] = f"{base}/PLACE/tl_2024_{sf}_place.zip"

    miss = [(n, u) for n, u in want.items() if n not in have]
    return miss, {"tag": f"raw-US-{st}-v1", "assets_n": len(have), "want_n": len(want),
                  "have": have, "release": rel, "counties_n": len(cfg.get("counties") or []),
                  "state_fips": sf, "meta": meta}


def patch_ledger(st: str, tag: str, tmp: Path, new_meta: dict[str, dict],
                 verified: list[str]) -> dict:
    """按 Release 实物改账本（只改 census_tiger + assets 事实字段）。"""
    rel = release(tag)
    live = {a["name"]: a for a in rel.get("assets") or [] if a["name"] not in BOOK}

    mp = download_asset(tag, "MANIFEST.json", tmp)
    sp = download_asset(tag, "STATUS.json", tmp)
    if not mp:
        raise SystemExit(f"{tag} 取不到 MANIFEST.json")
    man = json.loads(mp.read_text(encoding="utf-8"))
    sta = json.loads(sp.read_text(encoding="utf-8")) if sp else {}

    # ---- assets[]：覆盖 Release 实物，bytes/sha256 一律按实物写 ----
    old = {Path(a["path"]).name: a for a in man.get("assets") or []}
    # 该州账本里资产 path 的目录惯例（GA 用 data/raw/US-GA/tiger/…，AK/ID 用纯文件名）
    dirs = {str(Path(a["path"]).parent): 0 for a in man.get("assets") or []}
    prefix_dir = ""
    if dirs:
        common = max(dirs, key=lambda d: sum(1 for a in man.get("assets") or []
                                             if str(Path(a["path"]).parent) == d))
        if common not in (".", ""):
            prefix_dir = common + "/"

    def ent(name: str) -> dict:
        a = live[name]
        digest = (a.get("digest") or "").split(":")[-1] or None
        if not digest and name in new_meta:               # 刚上传的走本地实测 sha256
            digest = new_meta[name]["sha256"]
        src = name.split("-")[0]
        keep = old.get(name) or {}
        return {
            "path": keep.get("path") or (prefix_dir + name),
            "bytes": int(a["size"]),
            "sha256": digest,
            "sha256_head": (digest[:16] if digest else None),
            "source": keep.get("source") or {"NAD": "nad", "TIGER": "census_tiger",
                                             "OA": "openaddresses", "OVERTURE": "overture"}
                                          .get(src, src.lower()),
        }

    man["assets"] = [ent(n) for n in sorted(live)]
    n_assets = len(man["assets"])

    # ---- sources[census_tiger]：missed 清空、status ok ----
    tiger = next((s for s in man.get("sources") or [] if s.get("source") == "census_tiger"), None)
    if tiger is None:
        raise SystemExit(f"{tag} MANIFEST 里没有 census_tiger 段")
    before = {"status": tiger.get("status"), "missed_n": len(tiger.get("missed") or [])}
    tiger["status"] = "ok"
    tiger["missed"] = []
    tiger["upstream_rejected"] = 0
    tiger["backfilled_at"] = now()
    tiger["backfilled_files"] = sorted(set(tiger.get("backfilled_files") or []) | set(verified))
    tiger["backfill_note"] = ("2026-10-30 补抓：原 missed 记录的「上游返回非 zip（WAF 拒页/截断）」"
                              "经 HEAD 核对上游均 200，已在 runner 上带 PK 魔数+Content-Length 校验重抓并上架；"
                              "下载加固与退避重试见 scripts/tiger_backfill.py")
    # detail 里补上本轮新上架文件的元信息（记录数/字段数 = 实物解包所得）
    det = tiger.setdefault("detail", [])
    for n, m in new_meta.items():
        det.append({"file": n, "bytes": m["bytes"], "sha256": m["sha256"],
                    "dbf_records": m.get("records"), "dbf_fields_n": m.get("fields_n"),
                    "backfilled": True, "url": m.get("url")})

    # ---- 其他事实字段（有才改）----
    if isinstance(man.get("audit"), dict):
        man["audit"]["assets_total"] = len(live) + len(BOOK)
        man["audit"]["bytes_total"] = sum(int(a["size"]) for a in live.values())
        man["audit"]["audited_at"] = now()
        man["audit"]["tiger_backfill"] = now()
    if isinstance(man.get("quality"), dict):
        by = {}
        nb = {}
        for n, a in live.items():
            k = re.sub(r"-.*$", "", n).split("-")[0]
            by[k] = by.get(k, 0) + 1
            nb[k] = nb.get(k, 0) + int(a["size"])
        man["quality"]["by_source_files"] = {k.lower(): v for k, v in by.items()}
        man["quality"]["by_source_bytes"] = {k.lower(): v for k, v in nb.items()}
    man["generated_at"] = man.get("generated_at") or now()
    man["ledger_heal"] = {"at": now(), "by": "scripts/tiger_backfill.py",
                          "reason": "TIGER addrfeat 补抓后按 Release 实物重写 assets[]，"
                                    "census_tiger.status ok / missed 清空；未改任何数据字节"}
    man["integrity"] = integrity_of(man)
    mp.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- STATUS.json：updated_at 刷新，phase 保持 complete ----
    if sta:
        st_before = {"phase": sta.get("phase"), "updated_at": sta.get("updated_at"),
                     "assets_n": len(sta.get("assets") or {})}
        sa = sta.setdefault("assets", {})
        for n, a in live.items():
            if n not in sa:
                sa[n] = {"bytes": int(a["size"]),
                         "sha256": (a.get("digest") or "").split(":")[-1] or None,
                         "source": "census_tiger" if n.startswith("TIGER") else None}
        sta["updated_at"] = now()
        if sta.get("phase") != "complete":
            sta["phase"] = "complete"
            sta["phase_note"] = "TIGER 补抓完成，恢复 complete"
        sta.setdefault("tiger_backfill", {})["at"] = now()
        sta["tiger_backfill"]["files_added"] = sorted(new_meta)
        sp.write_text(json.dumps(sta, ensure_ascii=False, indent=1), encoding="utf-8")

    for p in (mp, sp):
        if p.exists():
            upload(tag, p)
    return {"before": before, "assets_manifest_n": n_assets, "assets_live_data_n": len(live),
            "status_before": sta.get("phase") if sta else None}


def integrity_of(manifest: dict) -> dict:
    body = json.dumps({k: v for k, v in manifest.items() if k != "integrity"},
                      ensure_ascii=False, sort_keys=True).encode()
    h = hashlib.sha256(body).hexdigest()
    return {"sha256": h, "algo": "sha256(manifest-minus-integrity,sort_keys)",
            "sha256_head": h[:16], "bytes": len(body)}


def check_names(st: str, names: list[str]) -> list[str]:
    bad = []
    for n in names:
        if n.startswith("TIGER"):
            if not (ADDRFEAT_RE.match(n) or PLACES_RE.match(n) or COUNTY_RE.match(n)
                    or BOUNDARY_RE.match(n)):
                bad.append(n)
    return bad


def do_backfill(st: str, apply: bool) -> dict:
    miss, info = plan(st)
    tag = info["tag"]
    log(f"[{st}] release 资产 {info['assets_n']}；应有 TIGER {info['want_n']}；"
        f"缺失 {len(miss)} → {[n for n, _ in miss]}")
    out = {"state": st, "tag": tag, "missing": [n for n, _ in miss],
           "assets_before": info["assets_n"], "counties_n": info["counties_n"],
           "ok": [], "failed": [], "meta": {}}
    if not miss:
        log(f"[{st}] 无缺失资产（只修账本）")
    for name, url in miss:
        if not (ADDRFEAT_RE.match(name) or PLACES_RE.match(name)):
            out["failed"].append({"name": name, "error": "资产名不过第七把锁正则"})
            continue
        log(f"  下载 {name} ← {url}")
        blob, err, meta = fetch_zip(url)
        if blob is None:
            log(f"    ✗ 失败：{err}")
            out["failed"].append({"name": name, "error": err})
            continue
        log(f"    ✓ {meta['bytes']}B sha256={meta['sha256'][:16]} "
            f"dbf_records={meta['records']} fields={meta['fields_n']}")
        if not apply:
            out["ok"].append(name)
            out["meta"][name] = meta
            continue
        tmp = Path(tempfile.mkdtemp(prefix="tiger-"))
        p = tmp / name
        p.write_bytes(blob)
        if upload(tag, p):
            out["ok"].append(name)
            out["meta"][name] = meta
        else:
            out["failed"].append({"name": name, "error": "上传失败"})
    if apply:
        # 补抓后再核一遍：所有 county addrfeat + places 是否齐
        miss2, info2 = plan(st)
        out["assets_after_upload"] = info2["assets_n"]
        out["still_missing"] = [n for n, _ in miss2]
        if out["still_missing"]:
            log(f"[{st}] ⚠️ 上架后仍缺 {out['still_missing']} → 不改账本状态")
        else:
            out["ledger"] = patch_ledger(st, tag, Path(tempfile.mkdtemp(prefix="ledger-")),
                                         out["meta"], out["ok"])
    log(f"[{st}] 结果：成功 {len(out['ok'])} / 失败 {len(out['failed'])}")
    return out



def all_tags() -> list[str]:
    r = gh(["api", f"repos/{REPO}/releases", "--paginate", "-q", ".[].tag_name"])
    if r.returncode != 0:
        raise SystemExit(f"列 release 失败：{r.stderr[:200]}")
    return sorted(t for t in r.stdout.split() if t.startswith("raw-US-"))


def snapshot() -> dict:
    """全仓快照：每个 tag 的资产数 + MANIFEST/STATUS 的 sha256（REST digest，不下载）。

    用来证明「只动了 AK/GA/ID」，其他州零变化。
    """
    snap = {}
    for tag in all_tags():
        rel = release(tag)
        assets = rel.get("assets") or []
        book = {a["name"]: (a.get("digest") or "").split(":")[-1] for a in assets
                if a["name"] in BOOK}
        snap[tag] = {"assets_n": len(assets),
                     "data_n": len([a for a in assets if a["name"] not in BOOK]),
                     "manifest_sha": book.get("MANIFEST.json"),
                     "status_sha": book.get("STATUS.json")}
    return snap


def diff_snapshot(before: dict, after: dict) -> dict:
    changed, added, removed = {}, [], []
    for tag in sorted(set(before) | set(after)):
        if tag not in before:
            added.append(tag); continue
        if tag not in after:
            removed.append(tag); continue
        b, a2 = before[tag], after[tag]
        if b != a2:
            changed[tag] = {k: [b.get(k), a2.get(k)] for k in set(b) | set(a2)
                            if b.get(k) != a2.get(k)}
    return {"changed": changed, "added": added, "removed": removed,
            "unchanged_n": len([t for t in before if t in after and t not in changed])}

def selfcheck(st: str) -> dict:
    """对单州做验收级自检（全部从 Release 重新读，不看本地缓存）。"""
    tag = f"raw-US-{st}-v1"
    rel = release(tag)
    names = [a["name"] for a in rel.get("assets") or []]
    data = [n for n in names if n not in BOOK]
    tmp = Path(tempfile.mkdtemp(prefix="sc-"))
    mp = download_asset(tag, "MANIFEST.json", tmp)
    sp = download_asset(tag, "STATUS.json", tmp)
    man = json.loads(mp.read_text(encoding="utf-8")) if mp else {}
    sta = json.loads(sp.read_text(encoding="utf-8")) if sp else {}
    tiger = next((s for s in man.get("sources") or [] if s.get("source") == "census_tiger"), {})
    res = {
        "state": st,
        "tiger_status": tiger.get("status"),
        "missed_n": len(tiger.get("missed") or []),
        "assets_release_data_n": len(data),
        "assets_manifest_n": len(man.get("assets") or []),
        "assets_status_n": len(sta.get("assets") or {}),
        "bad_names": check_names(st, names),
        "integrity_ok": man.get("integrity", {}).get("sha256") == integrity_of(man).get("sha256"),
        "phase": sta.get("phase"),
        "addrfeat_n": sum(1 for n in data if ADDRFEAT_RE.match(n)),
        "places_n": sum(1 for n in data if PLACES_RE.match(n)),
        "county_geojson_n": sum(1 for n in data if COUNTY_RE.match(n)),
        "zip_verified": [],
        "zip_failed": [],
    }
    # 每个**新上架**的 TIGER zip 在 Release 里可下载 + 本地解压能读出表头
    targets = [n for n in (tiger.get("backfilled_files") or []) if n.startswith("TIGER")]
    if not targets:
        targets = [n for n in data if n.startswith("TIGER") and n.endswith(".zip")][:3]
    for n in targets:
        p = download_asset(tag, n, tmp)
        try:
            blob = p.read_bytes()
            h = dbf_header(blob)
            res["zip_verified"].append({"name": n, "bytes": len(blob),
                                        "records": h["records"], "fields_n": h["fields_n"]})
        except Exception as e:  # noqa: BLE001
            res["zip_failed"].append({"name": n, "error": f"{type(e).__name__}: {str(e)[:80]}"})
    res["zip_verified_n"] = len(res["zip_verified"])
    res["zip_failed_n"] = len(res["zip_failed"])
    res["pass"] = (res["tiger_status"] == "ok" and res["missed_n"] == 0
                   and not res["bad_names"] and res["integrity_ok"]
                   and res["assets_manifest_n"] == res["assets_release_data_n"]
                   and res["phase"] == "complete" and res["zip_failed_n"] == 0)
    log(f"[{st}] 自检 {'PASS' if res['pass'] else 'FAIL'}：status={res['tiger_status']} "
        f"missed={res['missed_n']} manifest={res['assets_manifest_n']}/release={res['assets_release_data_n']} "
        f"bad_names={len(res['bad_names'])} integrity={res['integrity_ok']} "
        f"zip_ok={res['zip_verified_n']} zip_fail={res['zip_failed_n']} phase={res['phase']}")
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--selfcheck-all", action="store_true")
    ap.add_argument("--report-out", default="")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--snapshot", default="", help="写全仓快照（资产数 + MANIFEST sha）")
    ap.add_argument("--snapshot-diff", default="", help="与 before 快照对比，写 diff JSON")
    a = ap.parse_args()
    if a.snapshot:
        snap = snapshot()
        Path(a.snapshot).parent.mkdir(parents=True, exist_ok=True)
        Path(a.snapshot).write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"快照 {len(snap)} 个 tag → {a.snapshot}")
        return 0
    if a.snapshot_diff:
        before = json.loads(Path(a.snapshot_diff).read_text(encoding="utf-8"))
        d = diff_snapshot(before, snapshot())
        Path(a.snapshot_diff.replace(".json", "-diff.json")).write_text(
            json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"对比：变化 {len(d['changed'])} 个 tag（{sorted(d['changed'])}）；"
            f"零变化 {d['unchanged_n']} 个")
        return 0 if set(d["changed"]) <= {f"raw-US-{s}-v1" for s in STATES} else 1

    if not (a.state or a.selfcheck_all):
        raise SystemExit("需要 --state XX / --selfcheck-all / --snapshot / --snapshot-diff")

    results = []
    if a.state:
        st = a.state.upper()
        if a.selfcheck:
            results.append({"selfcheck": selfcheck(st)})
        else:
            results.append({"backfill": do_backfill(st, a.apply)})
    if a.selfcheck_all:
        results = [{"selfcheck": selfcheck(s)} for s in STATES]

    if a.json_out:
        Path(a.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json_out).write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    if a.report_out:
        scs = [r["selfcheck"] for r in results if "selfcheck" in r]
        bf = [r["backfill"] for r in results if "backfill" in r]
        L = ["# TIGER addrfeat 补抓 · 三州重新封印（2026-10-30）", "",
             f"- 生成时间（UTC）：{now()}",
             "- 判据：`sources[census_tiger].status == ok` 且 `missed == []`；"
             "资产名全过第七把锁；`MANIFEST.assets` 数量 == Release 数据资产数（−3 账本文件）；"
             "每个 TIGER zip 可下载且解压能读出 .dbf 表头。", ""]
        if bf:
            L += ["## 一、补抓", "", "| 州 | 上架前资产数 | 缺失 | 成功 | 失败 | 上架后资产数 | 仍缺 |",
                  "|---|---|---|---|---|---|---|"]
            for b in bf:
                L.append(f"| {b['state']} | {b['assets_before']} | {len(b['missing'])} | "
                         f"{len(b['ok'])} | {len(b['failed'])} | "
                         f"{b.get('assets_after_upload', '-')} | {len(b.get('still_missing') or [])} |")
            L.append("")
            fails = [(b['state'], f) for b in bf for f in b['failed']]
            if fails:
                L += ["失败项：", ""]
                for st, f in fails:
                    L.append(f"- {st} {f['name']}：{f['error']}")
                L.append("")
        if scs:
            L += ["## 二、自检（全部从 Release 重新读）", "",
                  "| 州 | tiger.status | missed | MANIFEST 资产 | Release 数据资产 | 命名违规 | integrity | zip 可解 | phase | 结论 |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
            for s in scs:
                L.append(f"| {s['state']} | {s['tiger_status']} | {s['missed_n']} | "
                         f"{s['assets_manifest_n']} | {s['assets_release_data_n']} | "
                         f"{len(s['bad_names'])} | {s['integrity_ok']} | "
                         f"{s['zip_verified_n']}/{s['zip_verified_n'] + s['zip_failed_n']} | "
                         f"{s['phase']} | {'PASS' if s['pass'] else 'FAIL'} |")
            L.append("")
            for s in scs:
                L.append(f"- {s['state']}：TIGER addrfeat {s['addrfeat_n']} / places {s['places_n']}"
                         f" / county geojson {s['county_geojson_n']}")
            L.append("")
        diff_p = Path(a.report_out).parent / "tiger-backfill-snapshot-diff.json"
        if diff_p.exists():
            d = json.loads(diff_p.read_text(encoding="utf-8"))
            L += ["## 三、其他州零变化证明（全仓 52 个 tag 前后快照）", "",
                  "| tag | 字段 | 前 | 后 |", "|---|---|---|---|"]
            for tag, ch in sorted(d["changed"].items()):
                for k, (b, af) in ch.items():
                    L.append(f"| {tag} | {k} | {b} | {af} |")
            L += ["", f"- 发生变化：{sorted(d['changed'])}",
                  f"- 零变化：**{d['unchanged_n']}** 个 tag"
                  + ("（仅上述三州变动，其他州 MANIFEST/STATUS/资产数逐字节相同）"
                     if set(d["changed"]) <= {f"raw-US-{x}-v1" for x in STATES} else "（⚠️ 有非目标州变动）"),
                  ""]
        Path(a.report_out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.report_out).write_text("\n".join(L), encoding="utf-8")
        log(f"写入 {a.report_out}")

    if a.selfcheck_all:
        return 0 if all(r["selfcheck"]["pass"] for r in results) else 1
    if a.selfcheck:
        return 0 if all(r["selfcheck"]["pass"] for r in results) else 1
    return 0 if all(not r["backfill"]["failed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
