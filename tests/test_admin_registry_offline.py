#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全球行政编码注册表自检（离线，纯标准库）。

跑法：python3 tests/test_admin_registry_offline.py

校验点：
  1. 文件存在、schema 正确、大小在合理区间（防止误提交空壳/巨型文件）
  2. 国家 249 条以上，代码/名称/alpha-3 长度合法，无重复
  3. 美国：电话前缀 = 1、US-DC 与 US-RI 存在、类型为 State、州数 ≥ 51
  4. 一级行政区：无重复 iso_3166_2、非 ISO 兜底行不得带 ISO 字段
  5. 与 state_roster.yaml 交叉校验：roster 里每个州都必须能查到 US-XX
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REG = ROOT / "rules" / "geo" / "global_admin_registry.json"
ROSTER = ROOT / "rules" / "US" / "state_roster.yaml"


def roster_states() -> list[str]:
    """从 state_roster.yaml 里抠出州号（不引第三方 yaml，按缩进取 `  XX:`）。"""
    out, in_states = [], False
    for line in ROSTER.read_text(encoding="utf-8").splitlines():
        if re.match(r"^states:\s*$", line):
            in_states = True
            continue
        if in_states:
            if re.match(r"^\S", line):        # 回到顶层键 → states 段结束
                break
            m = re.match(r"^  ([A-Z]{2}):\s*$", line)
            if m:
                out.append(m.group(1))
    return out


def main() -> int:
    res: dict[str, bool] = {}
    if not REG.exists():
        print(f"❌ 缺注册表：{REG}")
        return 1
    raw = REG.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    size_kb = len(raw) / 1024

    res["schema"] = data.get("schema") == "global_admin_registry/v1"
    res["体积合理(200KB~5MB)"] = 200 <= size_kb <= 5 * 1024

    countries = data.get("countries") or []
    cc = [c.get("country_code") for c in countries]
    res["国家数≥249"] = len(countries) >= 249
    res["国家码合法且不重复"] = (all(isinstance(x, str) and len(x) == 2 for x in cc)
                                 and len(set(cc)) == len(cc))
    res["alpha3 合法"] = all(isinstance(c.get("alpha_3"), str) and len(c["alpha_3"]) == 3
                              for c in countries)

    us = next((c for c in countries if c["country_code"] == "US"), None)
    res["US 存在且名称/区号正确"] = bool(us) and us["country_name"] == "United States" \
        and us.get("phone_prefix") == "1"

    divs = data.get("admin_divisions") or []
    iso_rows = [d for d in divs if d.get("in_iso3166_2")]
    fb_rows = [d for d in divs if not d.get("in_iso3166_2")]
    codes = [d.get("iso_3166_2") for d in iso_rows]
    res["ISO 区划≥5000"] = len(iso_rows) >= 5000
    res["ISO 码不重复"] = len(set(codes)) == len(codes) and all(codes)
    res["兜底行不带 ISO 字段"] = all(d.get("iso_3166_2") is None
                                     and d.get("source") == "geonames" for d in fb_rows)

    usdiv = {d["iso_3166_2"]: d for d in iso_rows if d.get("country_code") == "US"}
    res["US 州 ≥51 且含 DC/RI"] = "US-DC" in usdiv and "US-RI" in usdiv and len(usdiv) >= 51
    res["US-RI 名称/类型正确"] = (usdiv.get("US-RI", {}).get("admin_division_name") == "Rhode Island"
                                   and usdiv.get("US-RI", {}).get("admin_division_type") == "State")

    states = roster_states()
    missing = [s for s in states if f"US-{s}" not in usdiv]
    res["roster 州全部可解析"] = bool(states) and not missing

    print(f"注册表：{REG.relative_to(ROOT)}  {size_kb:.0f} KB")
    print(f"  国家 {len(countries)}｜ISO 区划 {len(iso_rows)}｜兜底 {len(fb_rows)}｜"
          f"覆盖国家 {data['counts']['countries_with_iso3166_2']}")
    print(f"  US 区划 {len(usdiv)}｜roster 州 {states}｜缺失 {missing}")
    print()
    for k, v in res.items():
        print(f"  {'✅' if v else '❌'}  {k}")
    ok = all(res.values())
    print("\n总判定：", "✅ 全部通过" if ok else "❌ 有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
