# Releases 产出物摸底记录（SOURCE PROFILES）

> 生成时间：2026-10-23T20:45:05Z　｜　数据快照：52 个 Release（最近自述时间 2026-10-23T10:11:39Z）
> 事实来源：Release 里的**实际文件**；`MANIFEST.json` 为自述（可能与实物冲突）；本文档仅为聚合视图。
> 本文档所有数字由 `tools/make_source_profiles.py` 生成，**禁止手改**。


一句话：**四源 38 GB 实物躺在 52 个 Release 里；只有 NAD 做完了位置核查（24 州可信 / 23 州不可用），
其余三源还停在「数了行数」阶段。**

## 一、四源总览

| 源 | 文件数 | 体积 | 行数 | 位置核查 | 状态 | 详见 |
|---|---|---|---|---|---|---|
| NAD | 2,293 | 12.18 GB | 108,025,465（实测） | 已完成 | `verified` | 01-NAD/README.md |
| Overture | 1,961 | 18.12 GB | 186,709,054 | **未做** | `counted` | 02-Overture/README.md |
| OpenAddresses | 1,862 | 4.98 GB | 212,206,580 | **未做** | `counted` | 03-OpenAddresses/README.md |
| TIGER | 6,390 | 3.06 GB | —（边界/号段，无「行」概念） | **未做** | `counted` | 04-TIGER/README.md |
| **合计** | | **38.34 GB** | | | | |

## 二、NAD 位置核查结论（唯一做完几何验证的源）

- ✅ `ok` 判定（≥80% 州内，账本口径）：**32 州**
- ⚠️ `partial` 判定（1~80%）：**15 州**
- ❌ `missing` 判定（<1%，含 0）：**5 州**

详细分级与证据见 `01-NAD/README.md`。完整分级表见 `MATRIX.csv`。

## 三、怎么取数据（重要：别用 `gh release download` 批量拉）

```bash
# ✅ 正确：走 CDN，不消耗 API 配额
curl -fsSL -O https://github.com/ip9988001/World-US-Map-Data/releases/download/raw-US-CA-v1/NAD-US-CA-06037-los_angeles.ndjson.gz

# ❌ 错误：gh release download 每个 asset 都算一次 API 调用
#    52 州累计几千次 → 撞 GITHUB_TOKEN 每仓库 1000/h 上限 → 全部 404
```

**命名规则**（四源各不相同）：

| 源 | 命名 | 粒度 |
|---|---|---|
| NAD | `NAD-US-<ST>-<fips>-<county>.ndjson.gz` | 县 |
| Overture | `OVERTURE-US-<ST>-<fips>-<county>.parquet`（部分州为整州 `OVERTURE-US-<ST>.parquet`） | 县 / 州 |
| OpenAddresses | `OA-US-<ST>-<source>.zip` | **source**（可能是县，也可能是市） |
| TIGER | `TIGER-US-<ST>-<fips>.geojson`（县界）+ `TIGER-US-<ST>-<fips>-addrfeat.zip`（号段） | 县 × 文件类型 |

## 四、三条最容易踩的坑

1. **NAD 的 `OUTOFSTATE` 包不是垃圾**：它是「州 bbox 查询时带进来的邻州点」。47 个此类包在无溢出时本就为空（`<2KB`）。
2. **NAD 只有 24 州能直接用**：NH/NV/MS/HI/MI 里 47 万~35 万个点几乎全是邻州点；不是文件缺失，是上游几何查询拿不到本州点。
3. **Overture 的 `address_levels` 是字段自述，不是几何验证**：报 91.75% 州内是字段统计口径；要当「事实」用前需自行按几何复核。

## 五、二仓接手清单

- NAD `ok` 判定 32 州（≥80% 州内）——其中 ≥95% 的才是真正 A 级
- NAD `partial` 判定 15 州（1~80%）需按县剔杂点后才可用
- 视同无 NAD（<1%）：5 州（HI、MI、MS、NH、NV）
- 「上游本来就没有」的县不要指望补：NAD 是自愿申报制，洛杉矶县都没申报


## 口径声明（全表通用，读数字前先看这段）

| 项 | 定义 |
|---|---|
| `in_state_points` | 落在**本州县界内、且能归属到某个县**的点 |
| `in_state_no_county` | 在本州境内但不在任何县界内 —— **本版未单独统计**，计入州外（典型：AK 的 Unorganized Borough、VA 独立市） |
| 多县重叠 | 点同时落在两个县界内时，记给**第一个命中的县**；总数不重复，模糊性由 `overlap` 记录（本版未统计） |
| 小文件判据 | `<2KB` **不等于空壳**：须逐行核对内容（2026-10-23 已证 3 个 OA 小文件是真实小数据集） |
| 分级阈值 | A ≥95%　B 80~95%　C 5~80%　D <5% 或 0 |


## 状态图例

| 状态 | 含义 |
|---|---|
| `verified` | 已用实物核对（点 × TIGER 县界几何判定） |
| `counted` | 只数了行数/字节，**未做位置核查** |
| `pending-audit` | 尚未核查，文末写明待做动作 |
| `blocked` | 上游不可用或数据本身缺失，核查被阻断 |

