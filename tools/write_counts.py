#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把数出来的行数写回各 Release 的 MANIFEST（row_counts + audit）"""
import glob, json, os, subprocess, tempfile
REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"
def sh(a):
    r = subprocess.run(a, capture_output=True, text=True, timeout=1800)
    return r.returncode, r.stdout, r.stderr
ok = fail = 0
for f in sorted(glob.glob("counts/*.json")):
    c = json.load(open(f, encoding="utf-8"))
    tag = c["tag"]
    tmp = tempfile.mkdtemp()
    sh(["gh", "release", "download", tag, "--repo", REPO, "--pattern", "MANIFEST.json", "--dir", tmp, "--clobber"])
    p = os.path.join(tmp, "MANIFEST.json")
    if not os.path.exists(p):
        print("跳过(无MANIFEST)", tag); fail += 1; continue
    man = json.load(open(p, encoding="utf-8"))
    rc = man.setdefault("row_counts", {})
    ov = c.get("overture") or {}
    oa = c.get("openaddresses") or {}
    if ov.get("file") and ov.get("rows"):
        rc["data/raw/US-%s/overture/%s" % (c["state"], ov["file"])] = ov["rows"]
    for n, v in (oa.get("detail") or {}).items():
        if isinstance(v, int) and v > 0:
            rc["data/raw/US-%s/openaddresses/%s" % (c["state"], n)] = v
    a = man.setdefault("audit", {}).setdefault("by_source", {})
    if ov:
        a.setdefault("overture", {})
        a["overture"]["rows"] = ov.get("rows")
        a["overture"]["in_state_rows"] = ov.get("in_state_rows")
    if oa:
        a.setdefault("openaddresses", {})
        a["openaddresses"]["files"] = oa.get("files")
        a["openaddresses"]["rows"] = oa.get("rows")
    man["audit"]["rows_counted_at"] = __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())
    man["audit"]["note"] = "Overture/OA 行数已实测（Overture 含州内行数：按 address_levels 字段判定）"
    json.dump(man, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    r1, o1, e1 = sh(["gh", "release", "upload", tag, p, "--repo", REPO, "--clobber"])
    if r1 == 0:
        ok += 1
        print("[ok]", tag, "ov_rows=", ov.get("rows"), "ov_in_state=", ov.get("in_state_rows"), "oa_rows=", oa.get("rows"))
    else:
        fail += 1; print("[fail]", tag, (e1 or "")[:100])
print("完成 ok=%d fail=%d" % (ok, fail))
