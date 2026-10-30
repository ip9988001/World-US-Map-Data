#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 Release **实物清单** 合成 MANIFEST.json / STATUS.json（只修账，不动一个字节数据）。

用途：某个 Release 的账本残缺（例：raw-US-MI-v1 的 MANIFEST 是早期版本，只有
audit/scope/generated_at/generated_by/row_counts 五个键，assets=[]，且从来没有
STATUS.json），需要一个能对齐正常账本口径的完整版。

数据来源全部是**可核验的公开事实**，不猜、不下载、不改数据：
  · 资产清单 / 字节数 / sha256  ← GitHub REST release assets（含 digest 字段）
  · 县清单 / bbox / state_fips  ← rules/US/state_roster.yaml
  · 四源分组                    ← 资产名前缀 NAD-/TIGER-/OA-/OVERTURE-
  · 时间戳                      ← 各资产的 createdAt（首/末）

拿不到的量一律写 null + note，**绝不编数字**（例：TIGER zip / OA zip / parquet
的行数需要解包才能知道，本脚本不解包，因此 row_counts 留空并注明）。

用法：
  python3 scripts/manifest_synth.py --tag raw-US-MI-v1 --dry-run
  python3 scripts/manifest_synth.py --tag raw-US-MI-v1 --apply     # clobber 上传账本
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROSTER = ROOT / "rules" / "US" / "state_roster.yaml"
REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"
BOOKKEEPING = {"MANIFEST.json", "STATUS.json", "RELEASE_NOTES.md"}

SOURCE_OF = [
    ("nad", ("NAD-",)),
    ("overture", ("OVERTURE-",)),
    ("openaddresses", ("OA-",)),
    ("census_tiger", ("TIGER-",)),
]


def log(m: str) -> None:
    print(m, flush=True)


def gh_json(args: list[str]) -> dict:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise SystemExit(f"gh 失败：{' '.join(args)}\n{r.stderr[:400]}")
    return json.loads(r.stdout)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def source_of(name: str) -> str | None:
    for src, prefixes in SOURCE_OF:
        if name.startswith(prefixes):
            return src
    return None


def split_tag(tag: str) -> tuple[str, int, int]:
    """raw-US-MI-v1 → ('MI', 1, 1)；raw-US-TX-v1-p2 → ('TX', 2, 6)。"""
    parts = tag.split("-")
    st = parts[2]
    vol = 1
    total = 1
    for p in parts[4:]:
        if p.startswith("p") and p[1:].isdigit():
            vol = int(p[1:])
    return st, vol, total


def build(tag: str) -> tuple[dict, dict]:
    rel = gh_json(["api", f"repos/{REPO}/releases/tags/{tag}"])
    st, vol, _ = split_tag(tag)
    roster_all = yaml.safe_load(ROSTER.read_text(encoding="utf-8"))["states"]
    cfg = roster_all.get(st)
    if cfg is None:
        raise SystemExit(f"roster 里没有州 {st}")
    counties = cfg.get("counties") or []
    state_fips = str(cfg.get("state_fips") or "").zfill(2)

    assets_raw = [a for a in rel.get("assets") or [] if a["name"] not in BOOKKEEPING]
    assets_raw.sort(key=lambda a: a["name"])

    by_src: dict[str, list[dict]] = {s: [] for s, _ in SOURCE_OF}
    unknown: list[str] = []
    for a in assets_raw:
        src = source_of(a["name"])
        if not src:
            unknown.append(a["name"])
            continue
        digest = (a.get("digest") or "").split(":")[-1]
        by_src[src].append({
            "name": a["name"],
            "bytes": int(a["size"]),
            "sha256": digest or None,
            "sha256_head": (digest[:16] if digest else None),
            "created_at": a.get("created_at"),
        })

    # 县片按 FIPS 归集（NAD 才有；TIGER 是 addrfeat/县界，OA 是 zip 源，OVERTURE 是 parquet）
    def fips_of(name: str) -> str | None:
        for seg in name.split("-"):
            if len(seg) == 5 and seg.isdigit():
                return seg
        return None

    nad_shards = by_src["nad"]
    nad_county_files = [a for a in nad_shards if fips_of(a["name"]) and a["name"].endswith(".ndjson.gz")]
    tiger_county = [a for a in by_src["census_tiger"] if "-county-" in a["name"]]
    county_fips_have = sorted({fips_of(a["name"]) for a in nad_county_files if fips_of(a["name"])})

    times = sorted(a["created_at"] for a in assets_raw if a.get("created_at"))
    started = times[0] if times else None
    finished = times[-1] if times else None

    sources = []
    for src, _ in SOURCE_OF:
        files = by_src[src]
        ent = {
            "source": src,
            "status": "ok" if files else "empty",
            "channel": {"nad": "USDOT NAD / Esri GeoServices REST",
                        "overture": "Overture Maps (S3/CLI)",
                        "openaddresses": "OpenAddresses (S3 列举)",
                        "census_tiger": "US Census TIGER REST"}[src],
            "file_count": len(files),
            "bytes": sum(a["bytes"] for a in files),
            "truncated": False,
            "fetched_at": finished,
            # 这些量必须解包才能得到，本脚本不解包 → null（不编数字）
            "total_rows": None,
            "official_count": None,
            "coverage_pct": None,
            "rows_note": "未解包统计：本脚本只是账本合成，不解压 zip/parquet/ndjson",
        }
        if src == "nad":
            ent["shard_count"] = len(nad_county_files)
            ent["empty_counties"] = []
            ent["failed_counties"] = []
            ent["reference_check"] = "manifest.reference_check（州级仅参考）"
        sources.append(ent)

    ledger = [{k: v for k, v in s.items()} for s in sources]

    manifest = {
        "generated_at": now(),
        "runner": "local:manifest_synth",
        "generator": "scripts/manifest_synth.py",
        "roster": "rules/US/state_roster.yaml",
        "scope": {
            "country": "US",
            "state": st,
            "state_fips": state_fips,
            "bbox": cfg.get("bbox"),
            "counties": counties,
            "volume": {"index": vol, "total": None},
        },
        "integrity": {},
        "assets": [{
            "path": a["name"],
            "bytes": a["bytes"],
            "sha256": a["sha256"],
            "sha256_head": a["sha256_head"],
            "source": src,
        } for src, _ in SOURCE_OF for a in by_src[src]],
        "row_counts": {},
        "row_counts_note": "未解包统计（数据未改动）；NAD 县片数以 shards 为准",
        "shards": [{
            "kind": "county",
            "county_fips": fips_of(a["name"]),
            "file": a["name"],
            "gz_bytes": a["bytes"],
            "sha256_gz": a["sha256"],
            "rows": None,
            "rows_note": "未解包统计",
        } for a in nad_county_files],
        "completeness": {
            "note": "只记录可核验事实：本账本由 release 实物清单合成，未解包数据文件。",
            "nad": {
                "acquire_mode": "ids+county_shards",
                "county_shards": len(nad_county_files),
                "counties_with_shards": county_fips_have,
                "empty_counties": [],
                "failed_counties": [],
                "local_rows": None,
                "upstream_declared_rows": None,
                "coverage_pct": None,
                "method": "未解包统计；NAD 覆盖缺口见 docs/NAD-COVERAGE-FULL-<日期>.md",
            },
        },
        "reference_check": {
            "policy": "县级核验为唯一门槛；州级三数咬合仅作比对参考",
            "counties_total": len(counties),
            "counties_ok": len(county_fips_have),
            "counties_empty": max(0, len(counties) - len(county_fips_have)),
            "counties_failed": 0,
            "failed_counties": [],
            "county_union_ids": None,
            "overflow_ids": None,
            "three_number_check": {
                "formula": "县并集 + 溢出 == 州官方 count",
                "covered": None, "official": None, "matches": None, "gap_ids": None,
                "note": "本账本未解包数据，三数咬合不参与判定；县片数为实物统计",
            },
            "method": "release 实物核查（按资产名/字节统计，不解析 zip/parquet 内容）",
        },
        "nad_no_coverage": (None if nad_county_files else {
            "county_shards": 0,
            "counties_total": len(counties),
            "note": "本 Release 没有任何 NAD 县片（ndjson.gz=0）；"
                    "这属于「未产出」而非「上游无数据」，缺口台账见 "
                    "docs/NAD-COVERAGE-FULL-<日期>.md",
        }),
        "quality": {
            "note": "只读统计，不做裁剪",
            "by_source_files": {s: len(by_src[s]) for s, _ in SOURCE_OF},
            "by_source_bytes": {s: sum(a["bytes"] for a in by_src[s]) for s, _ in SOURCE_OF},
            "unknown_prefix_files": unknown,
        },
        "sources": sources,
        "ledger": ledger,
        "heal": {
            "at": now(),
            "reason": "按 Release 实物补全账本（数据未改动；只在 release 上覆盖 MANIFEST/STATUS）",
            "by": "scripts/manifest_synth.py",
        },
        "audit": {
            "audited_at": now(),
            "method": "release 实物核查：按资产文件名/字节/digest 统计，不解析内容",
            "assets_total": len(assets_raw),
            "bytes_total": sum(int(a["size"]) for a in assets_raw),
            "by_source": {s: {"files": len(by_src[s]),
                              "bytes": sum(a["bytes"] for a in by_src[s])} for s, _ in SOURCE_OF},
        },
    }
    manifest["integrity"] = integrity_of(manifest)

    status = {
        "schema": "state-release-status/v1",
        "state": st,
        "tag": tag,
        "generator": "scripts/manifest_synth.py",
        "started_at": started,
        "updated_at": finished,
        "phase": "complete",
        "completed_at": finished,
        "expected": [c["fips"] for c in counties],
        "done": county_fips_have,
        "assets": {a["name"]: {"bytes": a["bytes"], "sha256": a["sha256"],
                               "source": src}
                   for src, _ in SOURCE_OF for a in by_src[src]},
        "known_gap": (None if nad_county_files else {
            "nad_county_shards": 0,
            "nad_counties_expected_unknown": True,
            "note": "NAD 县片为 0；本状态按四源实物贴 complete，"
                    "NAD 缺口单独记账（docs/NAD-COVERAGE-FULL-<日期>.md）",
        }),
        "note": "账本由 scripts/manifest_synth.py 按 release 实物合成；未改任何数据文件",
    }
    return manifest, status


def integrity_of(manifest: dict) -> dict:
    body = json.dumps({k: v for k, v in manifest.items() if k != "integrity"},
                      ensure_ascii=False, sort_keys=True).encode()
    h = hashlib.sha256(body).hexdigest()
    return {"sha256": h, "algo": "sha256(manifest-minus-integrity,sort_keys)",
            "sha256_head": h[:16], "bytes": len(body)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, help="例 raw-US-MI-v1")
    ap.add_argument("--apply", action="store_true", help="clobber 上传 MANIFEST/STATUS")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    manifest, status = build(args.tag)
    tmp = Path(tempfile.mkdtemp(prefix="ledger-synth-"))
    mpath, spath = tmp / "MANIFEST.json", tmp / "STATUS.json"
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    spath.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")

    log(f"[{args.tag}] 资产 {manifest['audit']['assets_total']} 个 / "
        f"{manifest['audit']['bytes_total']} B；四源文件数 "
        f"{manifest['quality']['by_source_files']}")
    log(f"[{args.tag}] NAD 县片 {len(manifest['shards'])} 个；"
        f"counties_ok={manifest['reference_check']['counties_ok']}/"
        f"{manifest['reference_check']['counties_total']}")
    log(f"[{args.tag}] MANIFEST {mpath.stat().st_size} B / STATUS {spath.stat().st_size} B → {tmp}")

    if not args.apply:
        log("（dry-run，未上传）")
        return 0

    for p in (mpath, spath):
        r = subprocess.run(["gh", "release", "upload", args.tag, str(p),
                            "--repo", REPO, "--clobber"],
                           capture_output=True, text=True, timeout=300)
        log(f"上传 {p.name}: rc={r.returncode} {(r.stdout or r.stderr).strip()[:160]}")
        if r.returncode != 0:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
