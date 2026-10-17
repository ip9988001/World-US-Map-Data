#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Matrix 计划的唯一事实来源：从 state_roster.yaml 生成"本批次要跑哪些州"。

为什么单独一个脚本（而不是在 workflow 里手写 51 个州）：
  · 州清单只在 roster 里维护一份，避免 YAML 与 roster 漂移；
  · 顺序由 size_tier 决定（tiny → small → medium → large），小的先跑、快速暴露问题；
  · GitHub Actions 的 `strategy.matrix` 吃 JSON 数组，这里直接产出，不做二次解析。

用法：
  python scripts/matrix_plan.py --tier tiny                       # 打印 JSON 数组
  python scripts/matrix_plan.py --tier all --limit 3              # 只取前 3 个（调试）
  python scripts/matrix_plan.py --states AK,WY,DE                 # 手工覆盖
  python scripts/matrix_plan.py --tier tiny --github-output $GITHUB_OUTPUT
      → 往 $GITHUB_OUTPUT 写 states=<json> / count=<n> / tag_list=<逗号分隔>

退出码：0 正常；2 参数/roster 有问题（workflow 会直接红，不会静默跑 0 个州）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROSTER = ROOT / "rules" / "US" / "state_roster.yaml"

# 跑批顺序：档位内按州代码字母序；档位之间按这个顺序展开
TIER_ORDER = ("tiny", "small", "medium", "large")


def load_roster(path: Path) -> dict:
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        sys.exit("需要 pyyaml：pip install pyyaml")
    if not path.exists():
        sys.exit(f"roster 不存在：{path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    states = doc.get("states") or {}
    if not isinstance(states, dict) or not states:
        sys.exit(f"roster 里没有 states：{path}")
    return states


def build_plan(states: dict, tier: str, override: str = "", limit: int = 0) -> list[str]:
    """返回本批次州代码列表（已按档位排序、去重、校验）。"""
    known = {code.upper(): v for code, v in states.items()}

    if override.strip():
        picked = [s.strip().upper() for s in override.replace(";", ",").split(",") if s.strip()]
        unknown = [s for s in picked if s not in known]
        if unknown:
            sys.exit(f"--states 里有 roster 未收录的州：{unknown}")
        plan = list(dict.fromkeys(picked))
    else:
        if tier == "all":
            # 按档位顺序展开，档位内字母序
            plan = []
            for t in TIER_ORDER:
                plan.extend(sorted(c for c, v in known.items() if (v.get("size_tier") or "").lower() == t))
            rest = sorted(c for c in known if c not in set(plan))   # 未标档位的兜底排在最后
            plan.extend(rest)
        else:
            if tier not in TIER_ORDER:
                sys.exit(f"--tier 只能是 {'/'.join(TIER_ORDER)}/all")
            plan = sorted(c for c, v in known.items() if (v.get("size_tier") or "").lower() == tier)

    if not plan:
        sys.exit(f"本批次为空：tier={tier} override={override!r}（roster 共 {len(known)} 州）")
    if limit and limit > 0:
        plan = plan[:limit]
    return plan


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="tiny", help="tiny/small/medium/large/all")
    ap.add_argument("--states", default="", help="手工指定（逗号分隔），留空=按 tier")
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 个（0=不限）")
    ap.add_argument("--tag-template", default="raw-US-{state}-v1", help="每州 Release tag 模板")
    ap.add_argument("--github-output", default="", help="$GITHUB_OUTPUT 路径")
    ap.add_argument("--json-out", default="", help="把计划写成 JSON 文件（可选）")
    args = ap.parse_args()

    states = load_roster(ROSTER)
    plan = build_plan(states, args.tier, args.states, args.limit)
    tiers = {c: (states[c].get("size_tier") or "?") for c in plan}
    tags = [args.tag_template.format(state=c) for c in plan]

    print(f"批次计划：{len(plan)} 个州  tier={args.tier}")
    for c in plan:
        print(f"  {c}  {tiers[c]:<7} {states[c].get('name','')}  ← tag {args.tag_template.format(state=c)}")
    print(json.dumps(plan))

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"tier": args.tier, "states": plan, "tiers": tiers, "tags": tags},
            ensure_ascii=False, indent=1), encoding="utf-8")
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(f"states={json.dumps(plan)}\n")
            fh.write(f"count={len(plan)}\n")
            fh.write(f"tag_list={','.join(tags)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
