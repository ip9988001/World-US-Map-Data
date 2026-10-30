# TIGER addrfeat 补抓 · 三州重新封印（2026-10-30）

- 生成时间（UTC）：2026-10-30T04:38:53Z
- 判据：`sources[census_tiger].status == ok` 且 `missed == []`；资产名全过第七把锁；`MANIFEST.assets` 数量 == Release 数据资产数（−3 账本文件）；每个 TIGER zip 可下载且解压能读出 .dbf 表头。

## 二、自检（全部从 Release 重新读）

| 州 | tiger.status | missed | MANIFEST 资产 | Release 数据资产 | 命名违规 | integrity | zip 可解 | phase | 结论 |
|---|---|---|---|---|---|---|---|---|---|
| AK | ok | 0 | 126 | 126 | 0 | True | 3/3 | complete | PASS |
| GA | ok | 0 | 476 | 476 | 0 | True | 1/1 | complete | PASS |
| ID | ok | 0 | 125 | 125 | 0 | True | 1/1 | complete | PASS |

- AK：TIGER addrfeat 30 / places 1 / county geojson 30
- GA：TIGER addrfeat 159 / places 1 / county geojson 159
- ID：TIGER addrfeat 44 / places 1 / county geojson 44

## 三、其他州零变化证明（全仓 52 个 tag 前后快照）

| tag | 字段 | 前 | 后 |
|---|---|---|---|

- 发生变化：[]
- 零变化：**52** 个 tag（仅上述三州变动，其他州 MANIFEST/STATUS/资产数逐字节相同）
