#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一仓账本回补（只修账，不动一个字节数据）

背景（2026-10-20 核查）：28 个 Release 里 8 个州 MANIFEST 漏记实物
  · AK：实物 23 个 NAD 县片，shards = []
  · ID：实物 43 个 addrfeat，assets 里一个都没有
  · 其余 WY 19 / SD 8 / RI 5 / DE 4 / NH 1 / HI 1
另有一处占位符 bug：integrity.sha256 的值是一句中文说明，从来没被真值替换。

做法：
  shards  ← 现有 shards ∪ 实物 NAD-<国家>-<州>-<fips>-<县>.ndjson.gz
  assets  ← 现有 assets ∪ 实物清单
  integrity ← 真 sha256（与 bots/fetch_state_raw.py 同口径）

用法：
  python3 scripts/manifest_reheal.py --all              # 预演（只打印，不上传）
  python3 scripts/manifest_reheal.py --all --apply      # 真回补并 clobber 上传
  python3 scripts/manifest_reheal.py --state AK --apply
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

RE_COUNTY = re.compile(r"^NAD-US-([A-Z]{2})-(\d{5})-(.+)\.ndjson\.gz$")
RE_OVERFLOW = re.compile(r"^NAD-US-([A-Z]{2})-(OUTOFSTATE|OVERFLOW)\.ndjson\.gz$")
BOOKKEEPING = {"MANIFEST.json", "STATUS.json", "RELEASE_NOTES.md"}


def log(m: str) -> None:
    print(m, flush=True)


def gh(*args: str, timeout: int = 120) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[:300])
    return r.stdout


def release_tags(prefix: str = "raw-US-") -> list[str]:
    out = gh("release", "list", "--limit", "200", "--json", "tagName", "-q", ".[].tagName")
    return sorted(t for t in out.split() if t.startswith(prefix))


def physical_assets(tag: str) -> list[tuple[str, int | None]]:
    out = gh("release", "view", tag, "--json", "assets",
             "-q", '.assets[]|"\\(.name)\\t\\(.size)"')
    res = []
    for line in out.splitlines():
        p = line.strip().split("\t")
        if p and p[0]:
            res.append((p[0], int(p[1]) if len(p) > 1 and p[1].isdigit() else None))
    return res


def manifest_integrity(m: dict) -> dict:
    """与 bots/fetch_state_raw.py::_manifest_integrity 完全同口径。"""
    body = {k: v for k, v in m.items() if k != "integrity"}
    blob = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    h = hashlib.sha256(blob).hexdigest()
    return {"sha256": h, "algo": "sha256(manifest-minus-integrity,sort_keys)",
            "sha256_head": h[:16], "bytes": len(blob)}


def heal_one(tag: str, workdir: Path, apply: bool) -> dict:
    st = tag[7:9]
    d = workdir / st
    d.mkdir(parents=True, exist_ok=True)
    rep = {"state": st, "shards_added": 0, "assets_added": 0, "integrity_fixed": False,
           "skipped": "", "uploaded": False}
    try:
        gh("release", "download", tag, "-p", "MANIFEST.json", "-p", "STATUS.json",
           "-D", str(d), "--clobber", timeout=180)
    except RuntimeError:
        pass
    mp, sp = d / "MANIFEST.json", d / "STATUS.json"
    if not mp.exists():
        rep["skipped"] = "无 MANIFEST（半成品/空壳，等续跑）"
        return rep

    m = json.loads(mp.read_text(encoding="utf-8"))
    actual = physical_assets(tag)
    rows_map: dict[str, int] = {}
    if sp.exists():
        try:
            for name, rec in (json.loads(sp.read_text(encoding="utf-8")).get("assets") or {}).items():
                if isinstance(rec, dict) and rec.get("rows") is not None:
                    rows_map[name] = int(rec["rows"])
        except Exception:  # noqa: BLE001
            pass

    # ---- shards 补 ----
    # ★ 先做 file 规范化：老账本里 file 是相对路径（data/raw/US-AL/nad/NAD-...），
    #   实物清单是纯文件名 → 直接比会一条也匹配不上，从而重复添加。统一成 basename。
    shards = [s for s in (m.get("shards") or []) if isinstance(s, dict)]
    for s in shards:
        if s.get("file"):
            s["file"] = str(s["file"]).rsplit("/", 1)[-1]
    have = {str(s.get("file")) for s in shards if s.get("file")}
    added = 0
    for name, size in actual:
        if name in have:
            continue
        mc, mo = RE_COUNTY.match(name), RE_OVERFLOW.match(name)
        if mc:
            shards.append({"kind": "county", "file": name, "fips": mc.group(2),
                           "county": mc.group(3), "rows": rows_map.get(name), "bytes": size})
            have.add(name)
            added += 1
        elif mo:
            shards.append({"kind": "overflow", "file": name,
                           "rows": rows_map.get(name), "bytes": size})
            have.add(name)
            added += 1
    if added:
        shards.sort(key=lambda s: (0 if s.get("kind") == "county" else 1, str(s.get("file"))))
    if shards:
        m["shards"] = shards          # 即使没新增也要回写（file 规范化生效）
    rep["shards_added"] = added

    # ---- assets 补 ----
    assets = [a for a in (m.get("assets") or []) if isinstance(a, dict)]
    known = {str(a.get("path", "")).rsplit("/", 1)[-1] for a in assets}
    for name, size in actual:
        if name in known or name in BOOKKEEPING:
            continue
        assets.append({"path": name, "bytes": size, "sha256": None,
                       "sha256_head": None, "source": "release"})
        rep["assets_added"] += 1
    if rep["assets_added"]:
        m["assets"] = sorted(assets, key=lambda a: str(a.get("path")))

    # ---- integrity 真值 ----
    old = m.get("integrity") or {}
    if not re.fullmatch(r"[0-9a-f]{64}", str(old.get("sha256") or "")):
        rep["integrity_fixed"] = True
    m["integrity"] = {}
    m["integrity"] = manifest_integrity(m)
    m.setdefault("heal", {})["at"] = __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ",
                                                               __import__("time").gmtime())
    m["heal"]["reason"] = "按 Release 实物补全账本 + integrity 修真值（数据未改动）"

    if not (rep["shards_added"] or rep["assets_added"] or rep["integrity_fixed"]):
        rep["skipped"] = "账实已一致"
        return rep

    mp.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    if apply:
        gh("release", "upload", tag, str(mp), "--clobber", timeout=180)
        rep["uploaded"] = True
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="一仓 MANIFEST 账本回补")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--state", default="")
    ap.add_argument("--apply", action="store_true", help="真上传（默认只预演）")
    args = ap.parse_args(argv)

    tags = release_tags()
    if args.state:
        tags = [t for t in tags if t[7:9] == args.state.upper()]
    if not tags:
        log("没有匹配的 release")
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="reheal-"))
    log(f"模式：{'实补并上传' if args.apply else '预演（不上传）'}｜目标 {len(tags)} 个 release\n")
    log(f"{'ST':4}{'补shards':>9}{'补assets':>9}{'修integrity':>12}  备注")
    total_s = total_a = total_i = uploaded = 0
    for t in tags:
        try:
            rep = heal_one(t, workdir, args.apply)
        except Exception as exc:  # noqa: BLE001
            log(f"{t[7:9]:4}  ❌ {type(exc).__name__}: {str(exc)[:100]}")
            continue
        total_s += rep["shards_added"]
        total_a += rep["assets_added"]
        total_i += int(rep["integrity_fixed"])
        uploaded += int(rep["uploaded"])
        note = rep["skipped"] or ("已上传" if rep["uploaded"] else "待上传")
        log(f"{rep['state']:4}{rep['shards_added']:>9}{rep['assets_added']:>9}"
            f"{'是' if rep['integrity_fixed'] else '-':>12}  {note}")
    log(f"\n合计：补 shards {total_s} 条、补 assets {total_a} 条、修 integrity {total_i} 个、"
        f"上传 {uploaded} 个 release")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
