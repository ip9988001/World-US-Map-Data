# 03 · OpenAddresses

> 生成时间：2026-10-23T20:45:05Z　｜　数据快照：52 个 Release（最近自述时间 2026-10-23T10:11:39Z）
> 事实来源：Release 里的**实际文件**；`MANIFEST.json` 为自述（可能与实物冲突）；本文档仅为聚合视图。
> 本文档所有数字由 `tools/make_source_profiles.py` 生成，**禁止手改**。

## 状态：`counted` —— 只有一个总行数，**位置完全未核查**

- 文件 1,862 个 / 4.98 GB
- 总行数 212,206,580（zip 内 csv 行数）
- 州内占比 / 县级分布 / 外州混入率：**均未知**

## 已知（重要：粒度与其他源不同）

- 命名：`OA-US-<ST>-<source>.zip`，**source 可能是县，也可能是市**（实测 CA 有 `OA-US-CA-city_of_anaheim.zip`、`OA-US-CA-city_of_bakersfield.zip`）
- 所以归属判定**不能靠文件名**，必须用 csv 里的经纬度
- CA: 107 个 OA 文件 vs 58 个县 → 存在市级源与跨县 source

## 待做动作（pending-audit）

1. 解析每个 zip 内 csv 的 `LON/LAT`（列名需按源适配）
2. 点 × TIGER 县界几何判定
3. 输出 `source-matrix.csv`：source × 类型（县级/市级）× 行数 × 州内%
4. 预计成本：5 GB，约 30~40 分钟

## 已确认无缺陷

- 3 个 `<2KB` 文件经解压逐行核对，均为**真实小数据集**：
  `OA-US-AK-matanuska_susitna_borough.zip`（csv 3 行）、
  `OA-US-VA-city_of_rural_retreat.zip`（46 行，Rural Retreat 镇）、
  `OA-US-VA-rockbridge.zip`（17 行）


## 口径声明（全表通用，读数字前先看这段）

| 项 | 定义 |
|---|---|
| `in_state_points` | 落在**本州县界内、且能归属到某个县**的点 |
| `in_state_no_county` | 在本州境内但不在任何县界内 —— **本版未单独统计**，计入州外（典型：AK 的 Unorganized Borough、VA 独立市） |
| 多县重叠 | 点同时落在两个县界内时，记给**第一个命中的县**；总数不重复，模糊性由 `overlap` 记录（本版未统计） |
| 小文件判据 | `<2KB` **不等于空壳**：须逐行核对内容（2026-10-23 已证 3 个 OA 小文件是真实小数据集） |
| 分级阈值 | A ≥95%　B 80~95%　C 5~80%　D <5% 或 0 |
