# 04 · Census TIGER（县界 + 地址号段）

> 生成时间：2026-10-23T21:00:41Z　｜　数据快照：52 个 Release（最近自述时间 2026-10-23T10:11:39Z）
> 事实来源：Release 里的**实际文件**；`MANIFEST.json` 为自述（可能与实物冲突）；本文档仅为聚合视图。
> 本文档所有数字由 `tools/make_source_profiles.py` 生成，**禁止手改**。

## 状态：`counted` —— **性质与其他三源不同：它不是「点」，是「边界 + 号段」**

- 文件 6,390 个 / 3.06 GB
- 县覆盖 3,142（Census 口径 county-equivalent 为 **3,143**，含 DC 为 3,144 → 差额本身是待核项）

## 两类资产

| 文件 | 内容 | 用途 |
|---|---|---|
| `TIGER-US-<ST>-<fips>.geojson` | 县界多边形（Esri `rings`，wkid 4326） | 所有源几何判定的**地基** |
| `TIGER-US-<ST>-<fips>-addrfeat.zip` | 地址号段（shp/dbf/shx） | 本身就是可用地址源（不只边界） |

## 待做动作（pending-audit）

1. 县完整性：与 Census 3,143 county-equivalent 清单逐 fips 对账（当前 3,142）
2. 文件自洽：每个 `-addrfeat.zip` 的 dbf 记录数是否等于 shx 记录数
3. 几何质量：rings 是否闭合、坐标是否 4326、面积是否合理（防退化多边形）
4. 输出 `county-coverage.csv`：县 × 边界/号段/对账
5. 预计成本：3 GB，约 20~30 分钟

```bash
gh workflow run source-audit.yml -f source=tiger -f only="raw-US-DE-v1 raw-US-RI-v1"
```

## 为什么优先做它

NAD / Overture / OpenAddresses 三源的位置判定**全部依赖 TIGER 县界**；
如果 TIGER 缺县或几何有洞，上面三源的结论都要打折。它是地基。


## 口径声明（全表通用，读数字前先看这段）

| 项 | 定义 |
|---|---|
| `in_state_points` | 落在**本州县界内、且能归属到某个县**的点 |
| `in_state_no_county` | 在本州境内但不在任何县界内 —— **本版未单独统计**，计入州外（典型：AK 的 Unorganized Borough、VA 独立市） |
| 多县重叠 | 点同时落在两个县界内时，记给**第一个命中的县**；总数不重复，模糊性由 `overlap` 记录（本版未统计） |
| 小文件判据 | `<2KB` **不等于空壳**：须逐行核对内容（2026-10-23 已证 3 个 OA 小文件是真实小数据集） |
| 分级阈值 | A ≥95%　B 80~95%　C 5~80%　D <5% 或 0 |
