#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动作1：把 Overture / OpenAddresses 的行数真正数出来（含州内行数）

Overture：parquet 元数据取总行数；只读 address_levels 列，统计州内行数
OA      ：zip 里的 csv 数行
输出：counts/<TAG>.json
"""
from __future__ import annotations
import csv, io, json, os, re, subprocess, sys, tempfile, zipfile

REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"


def sh(a, timeout=3600):
    r = subprocess.run(a, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def count_overture(tag, st, tmp):
    name = "OVERTURE-US-%s.parquet" % st
    rc, o, e = sh(["gh", "release", "download", tag, "--repo", REPO, "--pattern", name, "--dir", tmp, "--clobber"])
    p = os.path.join(tmp, name)
    if not os.path.exists(p):
        return None
    import pyarrow.parquet as pq
    import pyarrow.compute as pc
    pf = pq.ParquetFile(p)
    total = pf.metadata.num_rows
    inset = 0
    try:
        for i in range(pf.metadata.num_row_groups):
            col = pf.read_row_group(i, columns=["address_levels"]).column(0)
            flat = pc.list_flatten(col)
            if len(flat) == 0:
                continue
            vals = pc.struct_field(flat, "value")
            m = pc.equal(vals, st)
            inset += int(pc.sum(pc.cast(m, "int64")).as_py() or 0)
    except Exception as ex:
        inset = -1
        print("  州内统计失败:", repr(ex)[:120])
    os.remove(p)
    return {"file": name, "rows": total, "in_state_rows": inset}


def count_oa(tag, tmp):
    rc, o, e = sh(["gh", "release", "list", "--repo", REPO, "--limit", "200", "--json", "tagName"])
    rc, out, _ = sh(["gh", "api", "repos/%s/releases/tags/%s" % (REPO, tag)])
    d = json.loads(out or "{}")
    oa = [a for a in d.get("assets", []) if a["name"].startswith("OA-")]
    total = 0
    files = {}
    for a in oa:
        rc, o2, e2 = sh(["gh", "release", "download", tag, "--repo", REPO,
                          "--pattern", a["name"], "--dir", tmp, "--clobber"])
        p = os.path.join(tmp, a["name"])
        n = 0
        if os.path.exists(p):
            try:
                with zipfile.ZipFile(p) as z:
                    for zi in z.infolist():
                        if zi.filename.lower().endswith((".csv", ".geojson", ".ndjson")):
                            with z.open(zi) as f:
                                raw = f.read()
                            n += max(0, raw.count(b"\n") - 1)
            except Exception:
                n = -1
            os.remove(p)
        files[a["name"]] = n
        total += max(n, 0)
    return {"files": len(oa), "rows": total, "detail": files}


if __name__ == "__main__":
    tag = sys.argv[1]
    st = re.sub(r"^raw-US-", "", tag).split("-v1")[0].split("-")[0]
    tmp = tempfile.mkdtemp()
    res = {"tag": tag, "state": st}
    try:
        res["overture"] = count_overture(tag, st, tmp)
    except Exception as e:
        res["overture"] = {"error": repr(e)[:200]}
    try:
        res["openaddresses"] = count_oa(tag, tmp)
    except Exception as e:
        res["openaddresses"] = {"error": repr(e)[:200]}
    os.makedirs("counts", exist_ok=True)
    json.dump(res, open("counts/%s.json" % tag, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({k: (v if not isinstance(v, dict) else {kk: vv for kk, vv in v.items() if kk != "detail"}) for k, v in res.items()}, ensure_ascii=False))
