#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按实物核查结果修正自述文件（MANIFEST / RELEASE_NOTES / STATUS）

原则：
  · 能核实的写实数（文件数、字节、县覆盖、空分片）
  · 核实不了的明确写"未统计"及原因（zip/parquet 不解析）
  · 不改原始数据文件，只改自述
  · MI 没有 MANIFEST → 按实物生成一份，并标注 generated_by=audit_fix
"""
from __future__ import annotations
import json, os, re, subprocess, sys, tempfile, collections

REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"
DRY = "--dry" in sys.argv
ONLY = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None


def sh(args, timeout=300):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def releases():
    rc, out, _ = sh(["gh", "release", "list", "--repo", REPO, "--limit", "200",
                     "--json", "tagName"])
    tags = [x["tagName"] for x in json.loads(out or "[]")]
    return [t for t in tags if t.startswith("raw-US-")]


def assets(tag):
    rc, out, _ = sh(["gh", "api", "repos/%s/releases/tags/%s" % (REPO, tag)])
    d = json.loads(out or "{}")
    return [(a["name"], a["size"]) for a in d.get("assets", [])]


def pref(n):
    u = n.upper()
    for p in ("NAD-", "TIGER-", "OA-", "OVERTURE-"):
        if u.startswith(p):
            return p.rstrip("-").lower()
    return "other"


def analyze(alist):
    by = collections.defaultdict(lambda: {"files": 0, "bytes": 0, "counties": set(), "shards": []})
    oos = 0
    for n, s in alist:
        p = pref(n)
        if p == "other":
            continue
        if "COLUMNS" in n.upper() or "METADATA" in n.upper():
            continue
        if "OUTOFSTATE" in n.upper():
            oos += s
            by[p]["files"] += 1
            by[p]["bytes"] += s
            by[p]["shards"].append(n)
            continue
        by[p]["files"] += 1
        by[p]["bytes"] += s
        m = re.search(r"-(\d{5})-", n)
        if m:
            by[p]["counties"].add(m.group(1))
        if s < 2048:
            by[p]["shards"].append(n)
    out = {}
    for k, v in by.items():
        out[k] = {"files": v["files"], "bytes": v["bytes"],
                  "counties_covered": len(v["counties"]),
                  "small_shards": v["shards"][:20]}
    out["_outofstate_bytes"] = oos
    return out


def main():
    tags = releases()
    if ONLY:
        tags = [t for t in tags if ONLY in t]
    ok = fail = 0
    for tag in tags:
        st = tag.replace("raw-US-", "").replace("-v1-p1", ":p1").replace("-v1-p2", ":p2").replace("-v1", "")
        alist = assets(tag)
        act = analyze(alist)
        tmp = tempfile.mkdtemp()
        # 下载现有自述
        man_p = os.path.join(tmp, "MANIFEST.json")
        sh(["gh", "release", "download", tag, "--repo", REPO, "--pattern", "MANIFEST.json",
            "--dir", tmp, "--clobber"])
        man = {}
        if os.path.exists(man_p):
            try:
                man = json.load(open(man_p, encoding="utf-8"))
            except Exception:
                man = {}
        declared = man.get("scope", {}).get("counties") or []
        nct = len(declared)
        findings = []
        for s in ("nad", "tiger", "openaddresses", "overture"):
            pass
        nad = act.get("nad", {"counties_covered": 0, "files": 0})
        tiger = act.get("tiger", {"counties_covered": 0, "files": 0})
        if nct and nad["counties_covered"] == 0:
            findings.append("NAD 实际 0 个县有数据（声明 %d 县）" % nct)
        elif nct and nad["counties_covered"] < nct:
            findings.append("NAD 实际仅覆盖 %d/%d 县" % (nad["counties_covered"], nct))
        if nct and tiger["counties_covered"] < nct:
            findings.append("TIGER 实际覆盖 %d/%d 县" % (tiger["counties_covered"], nct))
        small = sum(len(act.get(k, {}).get("small_shards", [])) for k in ("nad", "tiger"))
        if small:
            findings.append("%d 个空分片（<2KB）" % small)
        if act["_outofstate_bytes"] > 50 * 1048576:
            findings.append("外州溢出 %.0f MB" % (act["_outofstate_bytes"] / 1048576))
        if act.get("overture", {}).get("bytes", 0) == 0:
            findings.append("Overture 无数据文件")
        if not findings:
            findings.append("实物核查无异常")
        man.setdefault("audit", {})
        man["audit"] = {
            "audited_at": __import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "method": "release 实物核查：按资产文件名/字节统计，不解析 zip/parquet 内容",
            "assets_total": len(alist),
            "bytes_total": sum(s for _, s in alist),
            "by_source": {k: v for k, v in act.items() if not k.startswith("_")},
            "outofstate_bytes": act["_outofstate_bytes"],
            "findings": findings,
            "note": "OA / Overture 行数未统计（zip / parquet 未解析），只统计文件数与字节",
        }
        if not man.get("scope"):
            man["scope"] = {"state": st.split(":")[0], "note": "scope 由 audit_fix 依据实物补建"}
        if not man.get("generated_at"):
            man["generated_at"] = man["audit"]["audited_at"]
            man["generated_by"] = "audit_fix（原缺 MANIFEST，按实物补建）"
        json.dump(man, open(man_p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

        # RELEASE_NOTES 追加修正段
        notes_p = os.path.join(tmp, "RELEASE_NOTES.md")
        sh(["gh", "release", "download", tag, "--repo", REPO, "--pattern", "RELEASE_NOTES.md",
            "--dir", tmp, "--clobber"])
        head = "# %s 实物核查修正（2026-10-23）\n\n" % st
        body = "\n".join("- " + f for f in findings) + "\n\n按 Release 内真实文件统计，未解析 zip/parquet 内容。\n\n---\n\n"
        old = ""
        if os.path.exists(notes_p):
            old = open(notes_p, encoding="utf-8").read()
            old = old.split("---\n\n", 1)[1] if old.startswith(head.split("\n")[0]) and "---\n\n" in old else old
        open(notes_p, "w", encoding="utf-8").write(head + body + old)

        if DRY:
            print("[dry]", tag, findings)
            ok += 1
            continue
        rc1, o1, e1 = sh(["gh", "release", "upload", tag, man_p, "--repo", REPO, "--clobber"])
        rc2, o2, e2 = sh(["gh", "release", "upload", tag, notes_p, "--repo", REPO, "--clobber"])
        if rc1 == 0 and rc2 == 0:
            ok += 1
            print("[ok]", tag, findings)
        else:
            fail += 1
            print("[fail]", tag, (e1 or e2 or "")[:120])
    print("完成 ok=%d fail=%d" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
