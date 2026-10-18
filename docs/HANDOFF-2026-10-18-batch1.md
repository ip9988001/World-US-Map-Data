# 交接封存 · 2026-10-18（Batch1 Tiny12 完成 / Small20 在跑）

## 0. 一句话状态
- **Tiny 12 州全部封印**（12/12），Small 20 于 16:26 起跑（run 39111474584，2 路并发）。
- 仓库 Release：**14 个 / 957 个资产 / 约 1.62 GB**（已清理 3.6 GB 旧格式与临时验证 tag）。
- 产物 = **四源原始数据 + 账本**，未清洗/未去重/未裁剪。

## 1. 命名契约（★ 不再变更，下游按此引用）
```
Release tag : raw-US-<ST>-v1            （ST = 两位州码，如 DE/AK）
NAD 县片    : NAD-US-<ST>-<fips>-<county_key>.ndjson.gz
NAD 溢出包  : NAD-US-<ST>-OUTOFSTATE.ndjson.gz
NAD 元信息  : NAD-US-<ST>-columns.json / NAD-US-<ST>-layer-metadata.json
Overture    : OVERTURE-US-<ST>.parquet          （geoparquet）
OpenAddress : OA-US-<ST>-<source>.zip
TIGER 边界  : TIGER-US-<ST>-state.geojson / -zcta.geojson / -county-<fips>.geojson / -place-<name>.geojson
TIGER 号段  : TIGER-US-<ST>-<fips>-addrfeat.zip / TIGER-US-<ST>-places.zip
账本        : MANIFEST.json（最后封印）/ STATUS.json（过程态）/ RELEASE_NOTES.md / RAW_DATA_NOTES.md
```
> ⚠️ tag 内容会随重跑原地更新（断点续传/修复重跑），**文件名与 tag 名不变**；
> 下游若需版本锁定，请锚定 `MANIFEST.json` 内的 `assets[].sha256`。

## 2. 已封印 12 州（数据行数 = 官方 count，100% 咬合）
| 州 | NАД 行数 | 分片 | 资产 | 体积 |
|---|---|---|---|---|
| DE | 751,212 | 4 | 25 | 226 MB |
| ME | 772,912 | 16 | 57 | 261 MB |
| MT | 660,136 | 57 | 179 | 213 MB |
| DC | 633,331 | 2 | 13 | 126 MB |
| RI | 569,222 | 6 | 31 | 154 MB |
| ND | 471,543 | 54 | 186 | 152 MB |
| VT | 366,638 | 15 | 53 | 131 MB |
| NH | 354,804 | 1 | 35 | 123 MB |
| AK | 325,379 | 23 | 100 | 57 MB |
| WY | 233,703 | 19 | 93 | 85 MB |
| SD | 124,237 | 8 | 158 | 72 MB |
| HI | 0（合法缺失） | 0 | 20 | 18 MB |

合计 **5,263,117 行**。

## 3. 关键口径（下游必须知道）
1. **县级核验是唯一门槛**：`官方 county count == 落盘行数 == 缺主键 0`；不合格的县**只丢该县**，记入 `MANIFEST.reference_check.failed_counties`，不影响其它县与封印。
2. **州级三数咬合只做参考**（`reference_check.three_number_check`），不阻断发布。
3. **OUTOFSTATE 是矩形框的邻州溢出**（DE 实测本州占比 74.85%，25.15% 是邻州门牌）→ 下游必须按 `State` 字段过滤，或直接忽略该包。
4. **空县**：`count=0` 且 idsOnly 也为 0 才认定（记入 `reference_check.empty_counties`）。
5. **合法缺失枚举**：`nad.status = no_upstream_coverage`（HI 5 县探针全 0）、`overture.status = empty_upstream`（HI 州框 + 2 大县探针全 0）。
6. **可证明完整性 = 相对上游声明**，不等于地理真值（CA 仅 16/58 县申报，共 1,577,369 行，不代表 CA 只有这些门牌）。

## 4. 代码与自检
- 主程序：`bots/fetch_state_raw.py`（~2600 行）；通用抓取层：`bots/dover_fetch.py`
- 编排：`.github/workflows/fetch-states-matrix.yml`（plan → fetch matrix → summary）
- 单州：`.github/workflows/fetch-state-raw.yml`；预检：`preflight-nad.yml`；诊断：`probe-overture.yml`
- 脚本：`scripts/matrix_plan.py`（州清单）、`scripts/matrix_summary.py`（批次汇总表）、`scripts/nad_preflight.py`（51 州 count 预检）、`scripts/generate_state_roster.py`
- 配置：`rules/US/state_roster.yaml`（51 州 bbox/县清单/size_tier）
- 离线自检（全部绿）：
  - `tests/test_fetch_state_raw_offline.py` 12 场景（含 S10 切片、S11 断点续传、S12 单县失败只丢该县）
  - `tests/test_runner_degradation.py` 20 项（stats 400 回退、RI 400k 截断、TIGER 空 body/gzip、Overture 防假 0、notes 幂等）
  - `tests/test_matrix_plan.py` 15 项（matrix 计划 + 汇总判定口径）

## 5. 已知上游特性（别当 bug 查）
- NAD 联邦视图**间歇性降级**：同一请求这一秒 400/超时、下一秒正常；并发 1 最稳，2 边缘，4 会大面积失败。已加 5/15/30s + 0.7~1.3 抖动退避。
- TIGERweb 偶发 `200 + 空 body` 或 gzip 未解压 → 已自动重试。
- HI：NAD 与 Overture 均无数据（多轮探针证实）；CA：16/58 县有 NAD 覆盖。

## 6. Small 20 进度（明日查看）
- run `39111474584`（16:26 起）：AL、AR 先跑，其余 18 州排队；`max_parallel=2`。
- 进度速查：`gh run list --workflow=fetch-states-matrix --limit 1`
- 批次汇总：`GH_TOKEN=... GITHUB_REPOSITORY=ip9988001/World-US-Map-Data python3 scripts/matrix_summary.py --states-json '[...]'`
- 补齐手段：同 tag 重跑即断点续传；单县补跑用 `--county-fips <fips,...>`（该模式**永不封印**）
