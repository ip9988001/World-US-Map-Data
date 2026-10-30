# TIGER addrfeat 补抓 · 三州重新封印（2026-10-30）

- 产生本次结果的 workflow run：https://github.com/ip9988001/World-US-Map-Data/actions/runs/41001647530（补抓）→ https://github.com/ip9988001/World-US-Map-Data/actions/runs/41002017458（自检+封印，全绿）
- 加固判据（写进 `scripts/tiger_backfill.py`，不是手工处理）：`Content-Length` 存在且 >0 → 实收字节数 == Content-Length（截断即失败）→ zip 魔数 `PK\x03\x04` → `zipfile.is_zipfile` → 解出 `.dbf` 表头（记录数/字段名）；任一步失败按 **2s/8s/30s** 退避重试 3 次，仍失败才记 failed。
- 采集器同源加固：`bots/fetch_state_raw.py::_zip_blob` 升级为 PK+zipfile+dbf 三重校验并改 2/8/30s 退避；`bots/dover_fetch.py::http_get` 增加 Content-Length 截断检测（对所有下载生效）。

## 〇、先纠一个口径（重要）

上一轮 HEAD 核对列出 77 个「warn 县」，本轮逐个对账后只有 **2 个文件真的不在 Release 里**：
GA 13309、ID 16045。其余 **75 个早已上架**（字节与上游一致），warn 的真实成因是**账本 `missed[]` 陈旧**：后续重跑把文件抓上来了，但 MANIFEST 的 missed 没清、status 没改回 ok。
AK 尤其典型：30 个 addrfeat + 1 个 places 全在，missed 却记 31 条。

| 州 | Release 里真缺 | 账本 missed 陈旧 | 本轮新上架 |
|---|---|---|---|
| AK | 0 | 31 | 0 |
| GA | 1 | 0 | 1 |
| ID | 1 | 44 | 1 |

## 一、补抓（matrix 按州分片，runner 上跑）

| 州 | 缺失 | 成功 | 失败 | Release 资产 前→后 | 新上架文件 |
|---|---|---|---|---|---|
| AK | 0 | 0 | 0 | 129→129 | — |
| GA | 1 | 1 | 0 | 478→479 | TIGER-US-GA-13309-addrfeat.zip |
| ID | 1 | 1 | 0 | 127→128 | TIGER-US-ID-16045-addrfeat.zip |

新上架文件的实测元数据（下载即校验，能解出 .dbf 表头）：

| 文件 | 字节 | sha256(前16) | .dbf 记录数 | 字段数 | 上游 |
|---|---|---|---|---|---|
| TIGER-US-GA-13309-addrfeat.zip | 127,716 | `6144f5bee66cc0f4` | 1,275 | 25 | https://www2.census.gov/geo/tiger/TIGER2024/ADDRFEAT/tl_2024_13309_addrfeat.zip |
| TIGER-US-ID-16045-addrfeat.zip | 262,515 | `2d5cf31ba2d5950e` | 2,581 | 25 | https://www2.census.gov/geo/tiger/TIGER2024/ADDRFEAT/tl_2024_16045_addrfeat.zip |

## 二、自检（三州全部从 Release 重新读，不看本地缓存）

| 州 | tiger.status | missed | MANIFEST.assets | Release 数据资产 | 命名违规 | integrity 复算 | zip 可解 | STATUS.phase | 结论 |
|---|---|---|---|---|---|---|---|---|---|
| AK | ok | 0 | 126 | 126 | 0 | True | 3/3 | complete | PASS |
| GA | ok | 0 | 476 | 476 | 0 | True | 1/1 | complete | PASS |
| ID | ok | 0 | 125 | 125 | 0 | True | 1/1 | complete | PASS |

TIGER 明细：
- AK：addrfeat 30 / places 1 / county geojson 30
- GA：addrfeat 159 / places 1 / county geojson 159
- ID：addrfeat 44 / places 1 / county geojson 44

## 三、其他 48 州零变化证明（全仓 tag 前后快照，比 MANIFEST/STATUS 的 sha256 与资产数）

| tag | 字段 | 前 | 后 |
|---|---|---|---|
| raw-US-AK-v1 | status_sha | `8ef92487fac427da` | `648649167fbcf9c5` |
| raw-US-AK-v1 | manifest_sha | `dd85db2b1d5a0e1a` | `fa25d5074e70e017` |
| raw-US-GA-v1 | status_sha | `fcf5b3a6885dd5c7` | `529bf9ffb7e38494` |
| raw-US-GA-v1 | manifest_sha | `a2272ca2cde31431` | `858a33fb3289c87c` |
| raw-US-ID-v1 | status_sha | `5b1eff62f8c8a7cc` | `ad588b3b306f597f` |
| raw-US-ID-v1 | manifest_sha | `0f9c7381e1aa400f` | `2aeac99abc55452e` |

- 有变化的 tag：**3** 个 = ['raw-US-AK-v1', 'raw-US-GA-v1', 'raw-US-ID-v1']
- **零变化 tag：49 个**（其余全部州 MANIFEST/STATUS 的 sha256 与资产数逐字节相同）
- 快照口径：`gh api releases/tags/<tag>` 的资产数 + MANIFEST/STATUS 的 REST digest（全仓 52 个 raw-US-* tag，前后各取一次）

## 四、复现

```bash
gh workflow run tiger-backfill.yml -f apply=1          # 矩阵三州：补抓+封印+自检+零变化对比
python3 scripts/tiger_backfill.py --state GA --selfcheck
python3 scripts/tiger_backfill.py --selfcheck-all --report-out docs/TIGER-BACKFILL-2026-10-30.md
```
