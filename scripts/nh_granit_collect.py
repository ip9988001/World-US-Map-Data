#!/usr/bin/env python3
"""NH GRANIT Parcel Mosaic parcel-point collector (runner edition, sharded)."""
import gzip, json, os, sys, time
import requests, urllib3
urllib3.disable_warnings()

BASE = "https://nhgeodata.unh.edu/hosting/rest/services/Hosted/CAD_ParcelMosaic/FeatureServer/0/query"
PAGE = int(os.environ.get("PAGE", "2000"))
OUT = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else "out"
os.makedirs(OUT, exist_ok=True)
TAGS = {"geom_precision": "parcel_centroid", "addr_source": "granit_parcel_mosaic",
        "is_interpolated": False, "source_state": "NH"}

def main():
    s = requests.Session(); s.headers["User-Agent"] = "pi-nh-collect/1.0"
    total = s.get(BASE, params={"where": "1=1", "returnCountOnly": "true", "f": "json"},
                  timeout=120, verify=False).json()["count"]
    print("declared", total, flush=True)
    written = 0
    for i, off in enumerate(range(0, total, PAGE)):
        params = {"where": "1=1", "outFields": "*", "f": "geojson",
                  "resultOffset": off, "resultRecordCount": PAGE, "orderByFields": "objectid"}
        for attempt in range(5):
            try:
                d = s.get(BASE, params=params, timeout=180, verify=False).json()
                if "error" not in d:
                    break
            except Exception as e:
                print("retry", off, e, flush=True)
            time.sleep(2 * (attempt + 1))
        feats = d.get("features", [])
        with gzip.open(f"{OUT}/granit-part-{i:05d}.ndjson.gz", "wt") as fh:
            for f in feats:
                p = dict(f["properties"]); p.update(TAGS)
                g = f.get("geometry") or {}
                p["_lon"], p["_lat"] = (g.get("coordinates") or [None, None])[:2]
                fh.write(json.dumps(p, ensure_ascii=False, default=str) + "\n")
        written += len(feats)
        if i % 50 == 0:
            print("page", i, "rows", written, flush=True)
        if len(feats) < PAGE:
            break
    print("DONE rows", written, flush=True)

if __name__ == "__main__":
    main()
