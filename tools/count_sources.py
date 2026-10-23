#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动作1：把 Overture / OpenAddresses 的行数真正数出来（含州内行数）

关键约束：GitHub Release asset 用 browser_download_url + curl 下载，
不计入 API rate limit（GITHUB_TOKEN 每仓库 1000/h，之前几千次 download 打爆 → 全州 404）。
asset 列表只调 1 次 `gh api repos/{repo}/releases/tags/{tag}`。
顺带把该 tag 的 MANIFEST.json 一起抓下来放进 counts/manifests/，merge 阶段就不用再碰 release。

输出：counts/<TAG>.json  +  counts/manifests/<TAG>.json
"""
from __future__ import annotations
import io, json, os, re, subprocess, sys, tempfile, time, zipfile

REPO = os.environ.get("GITHUB_REPOSITORY") or "ip9988001/World-US-Map-Data"


def sh(a, timeout=3600):
    r = subprocess.run(a, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def assets_of(tag):
    """1 次 API 拿到全部 asset（name + browser_download_url）"""
    rc, out, err = sh(["gh", "api", "repos/%s/releases/tags/%s" % (REPO, tag)])
    if rc != 0:
        print("  [api-fail]", rc, (err or out or "")[:200].replace("\n", " "))
        return None
    try:
        d = json.loads(out)
    except Exception as e:
        print("  [api-parse-fail]", repr(e)[:120], (out or "")[:120])
        return None
    a = d.get("assets") or []
    print("  assets=%d" % len(a))
    return a


def fetch(url, path, tries=4):
    """走 CDN，不消耗 API 配额"""
    for i in range(tries):
        rc, o, e = sh(["curl", "-fsSL", "--retry", "2", "--retry-delay", "2",
                       "--max-time", "1800", "-o", path, url], timeout=1900)
        if rc == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
            return True
        time.sleep(2 * (i + 1))
    print("  [dl-fail]", os.path.basename(path), (e or "")[:100].replace("\n", " "))
    return False


def count_overture(tag, st, assets, tmp):
    ov = [a for a in assets if a["name"].upper().startswith("OVERTURE-")]
    if not ov:
        return None
    import pyarrow.parquet as pq
    import pyarrow.compute as pc
    ov.sort(key=lambda a: a["name"])
    totals = 0
    inset = 0
    got = 0
    for a in ov:
        name = a["name"]
        p = os.path.join(tmp, name)
        url = a.get("browser_download_url") or ("https://github.com/%s/releases/download/%s/%s" % (REPO, tag, name))
        if not fetch(url, p):
            continue
        got += 1
        try:
            pf = pq.ParquetFile(p)
            totals += pf.metadata.num_rows
            for i in range(pf.metadata.num_row_groups):
                col = pf.read_row_group(i, columns=["address_levels"]).column(0)
                flat = pc.list_flatten(col)
                if len(flat) == 0:
                    continue
                vals = pc.struct_field(flat, "value")
                inset += int(pc.sum(pc.cast(pc.equal(vals, st), "int64")).as_py() or 0)
        except Exception as ex:
            inset = -1
            print("  州内统计失败:", name, repr(ex)[:80])
        finally:
            if os.path.exists(p):
                os.remove(p)
    return {"file": "%s (%d 个文件)" % (ov[0]["name"] if len(ov) == 1 else "OVERTURE-US-%s-*" % st, len(ov)),
            "files": len(ov), "files_downloaded": got,
            "rows": totals, "in_state_rows": inset}


def count_oa(tag, assets, tmp):
    oa = [a for a in assets if a["name"].startswith("OA-")]
    total = 0
    files = {}
    for a in oa:
        name = a["name"]
        p = os.path.join(tmp, name)
        url = a.get("browser_download_url") or ("https://github.com/%s/releases/download/%s/%s" % (REPO, tag, name))
        n = 0
        if fetch(url, p):
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
        files[name] = n
        total += max(n, 0)
    return {"files": len(oa), "rows": total, "detail": files}


if __name__ == "__main__":
    tag = sys.argv[1]
    st = re.sub(r"^raw-US-", "", tag).split("-v1")[0].split("-")[0]
    tmp = tempfile.mkdtemp()
    res = {"tag": tag, "state": st}
    assets = assets_of(tag)
    if assets is None:
        res["error"] = "asset list failed"
    else:
        # 顺手把 MANIFEST 抓下来给 merge 用（不耗 API）
        man = [a for a in assets if a["name"] == "MANIFEST.json"]
        if man:
            mp = os.path.join("counts", "manifests", tag + ".json")
            os.makedirs(os.path.dirname(mp), exist_ok=True)
            url = man[0].get("browser_download_url") or ("https://github.com/%s/releases/download/%s/MANIFEST.json" % (REPO, tag))
            if not fetch(url, mp):
                print("  [manifest-fail]", tag)
        try:
            res["overture"] = count_overture(tag, st, assets, tmp)
        except Exception as e:
            res["overture"] = {"error": repr(e)[:200]}
        try:
            res["openaddresses"] = count_oa(tag, assets, tmp)
        except Exception as e:
            res["openaddresses"] = {"error": repr(e)[:200]}
    os.makedirs("counts", exist_ok=True)
    json.dump(res, open("counts/%s.json" % tag, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({k: (v if not isinstance(v, dict) else {kk: vv for kk, vv in v.items() if kk != "detail"}) for k, v in res.items()}, ensure_ascii=False))
