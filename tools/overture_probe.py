#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动作0：验证 Overture parquet 里到底有什么（字段/行数/样例）"""
import json, os, subprocess, sys, tempfile
REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"

def sh(a):
    r = subprocess.run(a, capture_output=True, text=True, timeout=1800)
    return r.returncode, r.stdout, r.stderr

def probe(tag, name):
    tmp = tempfile.mkdtemp()
    rc, o, e = sh(["gh", "release", "download", tag, "--repo", REPO, "--pattern", name, "--dir", tmp, "--clobber"])
    p = os.path.join(tmp, name)
    if not os.path.exists(p):
        print("跳过", tag, name, (e or o)[:100]); return
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(p)
    print("=" * 70)
    print("文件:", name, "大小: %.1f MB" % (os.path.getsize(p) / 1048576))
    print("行数:", pf.metadata.num_rows, "列数:", pf.metadata.num_columns, "row_groups:", pf.metadata.num_row_groups)
    print("字段类型:")
    for f in pf.schema_arrow:
        print("   %-32s %s" % (f.name, f.type))
    t = pf.read_row_group(0).slice(0, 3).to_pylist()
    for r in t:
        print("样例:", json.dumps(r, ensure_ascii=False, default=str)[:600])

if __name__ == "__main__":
    for tag, name in [("raw-US-VT-v1", "OVERTURE-US-VT.parquet"),
                      ("raw-US-DE-v1", "OVERTURE-US-DE.parquet"),
                      ("raw-US-RI-v1", "OVERTURE-US-RI.parquet")]:
        try:
            probe(tag, name)
        except Exception as e:
            print("ERR", tag, repr(e)[:200])
