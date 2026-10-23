# World-US-Map-Data（一仓 · 采集）

美国 51 州 + DC 的**原始地址数据采集仓库**。四源全量下载、分州发布到 GitHub Release，**不做任何加工**。

> 三仓分工：**一仓 World-US-Map-Data = 采集**（tag 前缀 `raw-US-`）；**二仓 world-map-data-test = 加工**（tag 前缀 `gold-US`）。
> 一仓的边界是「下载 + 自述 + 可核查」。裁剪、去重、统一 schema 都是二仓的活。

## 铁律

1. **所有下载默认在 GitHub Actions runner 上执行**，本机只写代码、触发、看结果。任何 GB 级总包都不落到本地。
   - **唯一例外**：核查类任务（读一遍 → 算个数字 → 删）允许在本机跑，但必须同时满足：
     并行度 = 1、单进程内存 ≤ 400 MB（`resource.setrlimit` 硬限）、磁盘余量 ≥ 5 GB、走 CDN 限速 30 MB/s、即读即删。
   - 本机只有 1,966 MB 内存，`pi` 主进程常驻 247 MB —— 超限的进程必须自己死，不能拖动整机。
   - 紧急停机：`touch /root/pi-work/STOP`，所有带护栏的脚本下一轮检查即退出。
2. **数据不进 git**，只作为 Release 资产上架。仓库里只放代码、文档、账本。
3. **自述必须与实物一致**。任何数字若无法由脚本从实际文件复现，就不许写进 MANIFEST。

## 四个源

| 源 | 内容 | 命名 | 粒度 |
|---|---|---|---|
| USDOT NAD | 联邦地址点 | `NAD-US-<ST>-<fips>-<county>.ndjson.gz` | 县 |
| Overture Maps | addresses 主题 | `OVERTURE-US-<ST>-<fips>-<county>.parquet` | 县 / 州 |
| OpenAddresses | 社区地址集 | `OA-US-<ST>-<source>.zip` | source（县或市） |
| Census TIGER | 县界 + 地址号段 | `TIGER-US-<ST>-<fips>.geojson` / `-addrfeat.zip` | 县 × 类型 |

## 52 个 Release

每州一个 tag（`raw-US-<ST>-v1`），TX 因资产数超单 Release 上限拆成 `p1`/`p2`。合计 **38.34 GB / 12,509 文件**。

## 📖 先用这个：产出物摸底记录

**`docs/SOURCE-PROFILES/`** —— 四源的真实数据摸底、可用性判定、取数方式、已知坑。
下游要用一仓的数据，**先读这里**，不要自己翻 52 个 MANIFEST：

- `README.md` — 四源总览 + 怎么取数据 + 三条大坑 + 二仓接手清单
- `MATRIX.csv` — 机读总表（州 × 源）
- `01-NAD/` — 唯一做完几何位置核查的源（含 `county-matrix.csv` 3,142 县明细）
- `02-Overture/` `03-OpenAddresses/` `04-TIGER/` — 标注 `pending-audit`，文末写明待做动作

文档全部由 `tools/make_source_profiles.py` 生成，**禁止手改**。

## 常用命令

```bash
# 取单个文件（走 CDN，不消耗 API 配额；批量绝对不要用 gh release download）
curl -fsSL -O https://github.com/ip9988001/World-US-Map-Data/releases/download/raw-US-CA-v1/NAD-US-CA-06037-los_angeles.ndjson.gz

# 单州四源全量抓取（runner 侧，断点续传）
gh workflow run fetch-state-raw.yml -f state=DE

# 批量编排
gh workflow run fetch-states-matrix.yml -f tier=all -f max_parallel=2

# NAD 位置核查（单州）+ 写回 MANIFEST
python3 tools/nad_local_matrix.py raw-US-CA-v1
python3 tools/write_nad_truth.py

# 重新生成摸底记录
python3 tools/make_source_profiles.py
```

## 工具索引（`tools/`）

| 脚本 | 用途 |
|---|---|
| `nad_local_matrix.py` | NAD 实物 × TIGER 县界逐点判定（shapely 向量化） |
| `write_nad_truth.py` | 把核查结果写回 MANIFEST（只写事实） |
| `make_source_profiles.py` | 生成 `docs/SOURCE-PROFILES/` |
| `count_sources.py` | 数 Overture/OA 真实行数（走 CDN，不耗配额） |
| `write_counts.py` | 把行数写回 MANIFEST 的 `row_counts` |
| `audit_fix_manifests.py` | 按 Release 内真实文件重新核查并修正自述 |
| `nad_pip_probe.py` | NAD 逐县 PIP 探针（上游 count 会混邻州点，故改用样本点判定） |
