# 02 · Overture Maps（addresses 主题）

> 生成时间：2026-10-23T21:00:41Z　｜　数据快照：52 个 Release（最近自述时间 2026-10-23T10:11:39Z）
> 事实来源：Release 里的**实际文件**；`MANIFEST.json` 为自述（可能与实物冲突）；本文档仅为聚合视图。
> 本文档所有数字由 `tools/make_source_profiles.py` 生成，**禁止手改**。

## 状态：`counted` —— 只数了行数，**未做几何位置核查**

- 文件 1,961 个 / 18.12 GB
- 总行数 186,709,054（parquet metadata `num_rows`）
- 「州内行数」171,310,606（91.75%）—— **注意：这是按 `address_levels` 字段统计，不是几何验证**

## 已知

- 命名：`OVERTURE-US-<ST>-<fips>-<county>.parquet`（按县拆）；部分州为整州单文件 `OVERTURE-US-<ST>.parquet`
- 字段：`number / street / unit / postcode / postal_city / address_levels / geometry` 等
- `address_levels` 是**自述字段**，可能标错；报 91.75% 州内仅代表字段这么说

## 待做动作（pending-audit）

1. 点 × TIGER 县界几何判定，得出真实州内/县内分布
2. 与 `address_levels` 字段交叉校验，量化字段错误率
3. 输出 `county-matrix.csv`（县 × 行数）
4. 预计成本：18 GB，约 1~1.5 小时（Actions runner，max-parallel 4）

```bash
# 在 Actions 上跑（推荐）：本机 2 GB 内存，禁止本地处理 GB 级数据
gh workflow run source-audit.yml -f source=overture -f only="raw-US-DE-v1 raw-US-RI-v1"
# 本机仅允许小样本验证（--limit 2，几 MB）
python3 tools/source_audit.py --source overture --tag raw-US-DE-v1 --limit 2
```


## 口径声明（全表通用，读数字前先看这段）

| 项 | 定义 |
|---|---|
| `in_state_points` | 落在**本州县界内、且能归属到某个县**的点 |
| `in_state_no_county` | 在本州境内但不在任何县界内 —— **本版未单独统计**，计入州外（典型：AK 的 Unorganized Borough、VA 独立市） |
| 多县重叠 | 点同时落在两个县界内时，记给**第一个命中的县**；总数不重复，模糊性由 `overlap` 记录（本版未统计） |
| 小文件判据 | `<2KB` **不等于空壳**：须逐行核对内容（2026-10-23 已证 3 个 OA 小文件是真实小数据集） |
| 分级阈值 | A ≥95%　B 80~95%　C 5~80%　D <5% 或 0 |
