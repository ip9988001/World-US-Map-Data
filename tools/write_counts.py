#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把数出来的行数写回各 Release 的 MANIFEST（row_counts + audit）

MANIFEST 来自 count job 的 artifact（counts/manifests/<tag>.json），merge 阶段不再下载，
只剩每州 1 次 upload，避免把 GITHUB_TOKEN 的 1000/h 打爆。
"""
import glob, json, os, subprocess, time

REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"


def sh(a, timeout=1800):
    r = subprocess.run(a, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


ok = fail = 0
fails = []
for f in sorted(glob.glob("counts/*.json")):
    c = json.load(open(f, encoding="utf-8"))
    tag = c["tag"]
    src = os.path.join("counts", "manifests", tag + ".json")
    if not os.path.exists(src):
        print("[fail] 无 MANIFEST", tag, c.get("error", ""))
        fail += 1
        fails.append(tag)
        continue
    man = json.load(open(src, encoding="utf-8"))
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
        a["overture"]["files"] = ov.get("files")
    if oa:
        a.setdefault("openaddresses", {})
        a["openaddresses"]["files"] = oa.get("files")
        a["openaddresses"]["rows"] = oa.get("rows")
    man["audit"]["rows_counted_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    man["audit"]["note"] = "Overture/OA 行数已实测（Overture 含州内行数：按 address_levels 字段判定）"
    out = os.path.join("out", tag, "MANIFEST.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(man, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    r1, o1, e1 = 1, "", ""
    for attempt in range(5):
        r1, o1, e1 = sh(["gh", "release", "upload", tag, out, "--repo", REPO, "--clobber"])
        if r1 == 0:
            break
        time.sleep(5 * (attempt + 1))
    if r1 == 0:
        ok += 1
        print("[ok]", tag, "ov_rows=", ov.get("rows"), "ov_in_state=", ov.get("in_state_rows"), "oa_rows=", oa.get("rows"))
    else:
        fail += 1
        fails.append(tag)
        print("[fail]", tag, (e1 or o1 or "")[:300].replace("\n", " "))
print("完成 ok=%d fail=%d" % (ok, fail))
if fails:
    print("失败:", " ".join(fails))
