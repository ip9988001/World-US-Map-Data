#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一仓 · 历史账本补算（只补 reference_check，不动数据、不重跑）

用途：早期封印的州（如 AK）MANIFEST 里没有 `reference_check`，
      下游（二仓 preflight/scope）只能靠“实物重建”兜着，判不出县覆盖闭合。
      本脚本从现有 `shards[]` 反算一份**诚实的**参考段，并标注是反算出来的。

口径（不造假）：
  counties_ok      = rows>0 的 county 分片数
  counties_empty   = counties_total − counties_ok − counties_failed（未列出的县按“无数据”算）
  counties_failed  = 反算不出来 → 记 0，并在 derived_from_shards 里写明“原始失败名单缺失”
  county_union_ids / overflow_ids 若账本已有则保留，没有就不编（留 null）

用法：
  python3 scripts/ledger_reheal.py --manifest /tmp/ak/MANIFEST.json --dry-run
  python3 scripts/ledger_reheal.py --manifest /tmp/ak/MANIFEST.json --upload raw-US-AK-v1
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def log(msg: str) -> None:
    print(f"[ledger] {msg}", flush=True)


def derive(m: dict) -> dict:
    rc = dict(m.get("reference_check") or {})
    if rc.get("counties_total") and rc.get("derived_from_shards") is not True:
        log("账本已有完整 reference_check，跳过（要强制覆盖请先手工清掉该字段）")
        return {}
    shards = [s for s in (m.get("shards") or []) if isinstance(s, dict)]
    county = [s for s in shards if s.get("kind") == "county"]
    ok = [s for s in county if (s.get("rows") or 0) > 0]
    total = ((m.get("scope") or {}).get("counties") or [])
    total_n = len(total) or rc.get("counties_total")
    if not total_n:
        raise SystemExit("无法确定县总数：scope.counties 与 reference_check.counties_total 都缺")
    empty_n = max(0, int(total_n) - len(ok))
    over = [s for s in shards if s.get("kind") == "overflow"]
    new = {
        "policy": "县级核验为准；本段为**事后从 shards 反算**（历史账缺字段）",
        "derived_from_shards": True,
        "derived_at_note": ("counties_ok/empty 由 shards 行数反算；原始失败名单与主键差集已不可得，"
                            "counties_failed 记 0 表示“账本未记录失败县”，不代表当时没有失败"),
        "counties_total": int(total_n),
        "counties_ok": len(ok),
        "counties_empty": empty_n,
        "counties_failed": 0,
        "county_union_ids": rc.get("county_union_ids"),
        "overflow_ids": rc.get("overflow_ids"),
        "state_upstream_count": ((m.get("completeness") or {}).get("nad") or {}).get(
            "upstream_declared_rows"),
        "three_number_check": {
            "applicable": False,
            "note": "历史账缺主键差集，无法复原；请以各 county 分片自身行数与 shards 为准",
        },
        "overflow_shards": len(over),
        "overflow_rows": sum((s.get("rows") or 0) for s in over),
    }
    return new


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="历史 MANIFEST 的 reference_check 补算")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--upload", default="", help="补算后上传到该 tag（--clobber）")
    ap.add_argument("--repo", default="ip9988001/World-US-Map-Data")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    p = Path(args.manifest)
    m = json.loads(p.read_text(encoding="utf-8"))
    new = derive(m)
    if not new:
        return 0
    log(f"反算结果：县 {new['counties_ok']} 有数据 / {new['counties_empty']} 无数据 / "
        f"共 {new['counties_total']}｜溢出片 {new['overflow_shards']} 个 {new['overflow_rows']:,} 行")
    m["reference_check"] = new
    # 账本自身的完整性摘要一起更新（否则改完等于没改）
    body = {k: v for k, v in m.items() if k != "integrity"}
    import hashlib
    blob = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    m["integrity"] = {"sha256": hashlib.sha256(blob).hexdigest(),
                      "algo": "sha256(manifest-minus-integrity,sort_keys)",
                      "sha256_head": hashlib.sha256(blob).hexdigest()[:16], "bytes": len(blob)}
    if args.dry_run:
        log("--dry-run：不写盘、不上传")
        return 0
    p.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"已写回 {p}")
    if args.upload:
        r = subprocess.run(["gh", "release", "upload", args.upload, "-R", args.repo,
                            str(p), "--clobber"], capture_output=True, text=True)
        if r.returncode != 0:
            log(f"上传失败：{(r.stderr or r.stdout)[:300]}")
            return 2
        log(f"已上传并覆盖 {args.upload}/MANIFEST.json（封印文件，属于允许覆盖的终态件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
