import sys, types, json
sys.path.insert(0, "bots")
import fetch_state_raw as fs

man = json.load(open(".local/mn/MANIFEST.json"))
st  = json.load(open(".local/mn/STATUS.json"))
nad_src = next(s for s in man["sources"] if s["source"] == "nad")

fx = types.SimpleNamespace(
    problems=[],
    status={"nad": {**nad_src, "status": "ok"},
            "overture": {"status": "ok"},
            "openaddresses": {"status": "ok", "upstream_sources": 66,
                              "fetched_sources": 66, "empty_sources": 0, "failed_sources": 0},
            "census_tiger": {"status": "ok"}},
    cache={"prev_manifest": man, "nad_reference": man["reference_check"]},  # repair: 无 nad_shards
    status_doc=st,
    files=[], meta={},
    cfg={"state": "MN", "counties": man["scope"]["counties"]},
    args=types.SimpleNamespace(pack_mode="county_shards", repair_oa=True, allow_partial=False),
)
print("cache.nad_shards =", fx.cache.get("nad_shards"))
print("status_doc county 片 =", sum(1 for v in st["assets"].values() if v.get("kind") == "county"))
try:
    fs.gate(fx)
    print("RESULT: 门禁通过 → 修复有效")
except RuntimeError as e:
    print("RESULT: 阻断 →", e)
