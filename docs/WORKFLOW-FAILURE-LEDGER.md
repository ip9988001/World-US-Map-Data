# WORKFLOW 故障清单 · 世界地图一仓（World-US-Map-Data）

> 立规：2026-10-31。用户原话：**每个仓建一份 workflow 故障清单，写之前必查、跑后必补。**
> 本文是该仓 workflow 的**唯一故障台账**（唯一真值）。别处写的「踩坑记录」一律以本文为准并回链到编号。
>
> **本表条目以代码 / 文档可复核证据为准，记忆与传闻不入表**（复核不了的标「待复核」，不进结论）。
>
> 三条铁律：
> 1. **写之前必查**：派单前先查本文 + 本次涉及的类别，派单里写 `故障清单已查：<编号列表 | 无>`，没有这行视为无效派单。
> 2. **跑后必补**：新踩的坑**当天**补进本文（含错误原文关键行、证据路径、修法、验证方式、关联提交），**不许事后凭记忆补**。
> 3. **补完提交**：commit message 写 `ledger: +N <类别>`（例如 `ledger: +1 采集器逻辑`）。
>
> 类别固定 10 个：`并发队列` `artifact与下载` `git提交` `账本与口径` `checkpoint与断点` `切分与体积` `采集器逻辑` `上游不可达` `缓存与签名` `测试与CI`。
> 一仓是**数据源头仓**：Release `raw-US-<ST>-v1` 就是对外契约；`MANIFEST.json` / `STATUS.json` 的语义（尤其 `phase`）下游两仓直接吃，改之前先看本清单 W1-001/W1-004。

## 0. 本仓 workflow 骨架速查（写之前先看这一段）

- **主链**：`fetch-states-matrix.yml`（`plan` → `fetch` 矩阵 → `summary`，多州批量）、`fetch-state-raw.yml`（单州全量）。产物 = 本仓 Release `raw-US-<ST>-v1`（可 `-pN` 分卷，例如 `TX-p2`）+ `MANIFEST.json` + `STATUS.json`。
- **NAD 专项（判县/补县，最容易踩 bbox 坑）**：`nad-county-backfill.yml`、`nad-county-probe.yml`、`nad-gap-matrix.yml`、`nad-local-matrix.yml`、`nad-pip-probe.yml`。
- **预检**：`preflight-nad.yml`、`preflight-upstream.yml`（上游 count/HTTP 预探，只读）。
- **TIGER / Overture**：`tiger-backfill.yml`（补抓 + 封印 + 零变化自检）、`tiger-warn-probe.yml`（warn 归因）、`overture-probe.yml`、`probe-overture.yml`。
- **审计与运维**：`audit-count.yml`、`audit-fix.yml`、`content-audit.yml`、`pipeline-audit.yml`、`release-quality-audit.yml`、`source-audit.yml`、`ops.yml`、`run-watch.yml`（事件驱动 + 每小时兜底）、`sentinel-watchdog.yml`。
- **并发/产物纪律**：矩阵 job 数上限决定了同 tag 的并发上传压力；`--clobber` 会覆盖同名资产，**封印（写 MANIFEST/STATUS）必须在该州全部资产上传完之后**。

## 1. 台账

| 编号 | 发现日期 | 类别 | 症状（错误原文关键行） | 根因 | 影响（数据/账本/时间/假红） | 修法 | 验证方式 | 关联提交 | 复发次数 |
|---|---|---|---|---|---|---|---|---|---|
| W1-001 | 2026-10-30 | 账本与口径 | TIGER 一侧 **77 条 warn**（AK 31 / GA 1 / ID 45），逐条 HEAD 上游后：只有 **2 个是真缺**（GA 13309 Wheeler、ID 16045），其余 75 条是 `MANIFEST.sources[census_tiger].missed[]` 没清 | `missed[]` 是**采集期**记录，补抓成功后没人清 → 账本陈旧 | 账本：77 条「缺县」里有 75 条是假的（**账本陈旧 ≠ 实物缺**）；时间：每次都要重新逐条核 | 补抓 + 封印后用 `tiger_backfill.py --selfcheck` 重算 `missed[]`；判缺一律「HEAD 上游 200 = 我们漏抓 / 404·410 = 上游真无」 | `python3 scripts/tiger_backfill.py --selfcheck-all` 输出 `真缺 2`；`docs/TIGER-BACKFILL-2026-10-30.md` 的零变化表（49/52 tag 逐字节相同） | 424785d；`docs/TIGER-WARN-2026-10-30.md`；`docs/TIGER-BACKFILL-2026-10-30.md` | 1 |
| W1-002 | 2026-10-2x | 采集器逻辑 | 「点落在哪个县」用 **bbox** 判 → Clayton County GA：`bbox count 235` / `多边形 count 0`；GA 全州 `bbox_false_positive_n: 32` | 县是不规则形状，bbox 面积远大于真实县界 → 邻县数据算成本县 | 账本：GA 覆盖结论反转（曾报「34 县有数据 / 缺 32 县」，全部作废）；数据：若照此补采＝白采 | 判县**必须**多边形（`esriGeometryPolygon` + TIGER rings）；bbox 只能粗筛，不得用于结论；`nad_gap_plan.py`/`nad_county_probe.py` 已重写 | 复算 GA：`need_n` 与多边形版一致；`bbox_false_positive_n` 只作为「bbox 不可用」的注脚 | d549489（Clayton 实证）；`docs/NAD-COVERAGE-2026-10-28.md`、`docs/NAD-COVERAGE-FULL-2026-10-30.md` | 2 |
| W1-003 | 2026-10-30 | 采集器逻辑 | TIGER warn 的 `采集器记录` 一栏大面积是「**上游返回非 zip（WAF 拒页 / 截断），已重试**」（AK 31 县几乎全部如此） | 上游 WAF 返回 HTML/截断内容，采集器照存成 `.zip`（或直接当失败），没校验 zip 魔数 | 数据：整州 addrfeat 拿不到；账本：warn 数量虚高（看着像「上游无」） | 落盘前校验 zip 魔数（`PK\x03\x04`）与长度；非 zip 一律重试 + 记 `anomaly`，不许当「上游无」 | 重抓 AK：addrfeat 数 = 上游 200 的县数；`tiger-warn-probe` 里「非 zip」条目归零 | 424785d（AK 重抓 77 个 addrfeat） | 1 |
| W1-004 | 2026-10-30 | artifact与下载 | 分卷 tag 与单卷 tag 混在一起：`TX-p2`（4,958,127 行）只在下游按 `-pN` 枚举时才会被读到；旧下游只认 `raw-US-TX-v1` → **整卷静默缺** | 分卷是「同前缀多 tag」，没有「卷清单」字段 | 数据：整卷缺失且下游看着成功（二仓已踩，见 W2-006）；账本：行数对不上 | MANIFEST 里显式写 `volumes[]`（卷名 + 行数）；下游按 `volumes` 枚举而不是猜 tag 前缀 | `MANIFEST.volumes` 存在且行数之和 = `STATUS` 总行数；下游按 `volumes` 读能取到全部卷 | 见 `docs/RELEASE_HEALTH-2026-10-23.md`（`TX-p2` 行）；未修（MANIFEST 尚无 `volumes[]`） | 0 |
| W1-005 | 2026-10-2x | 采集器逻辑 | 判县探针把结果分成 `⬜ 上游真无覆盖` 与 `🔴 我们漏抓` 两档，**两档都不可信**（同一 bbox 错在 `nad_county_probe.py` 与 `scripts/nad_gap_plan.py` 各犯一次） | 复制粘贴式实现：两处各写一份判县 | 账本：结论双错（既误报「上游无」，也误报「我们漏」）；时间：两处各修一遍 | 判县只留**一个**实现（公共函数），探针只消费它的输出；禁止再各写一份 | 两处调用同一个函数（`grep -c` 判县实现只有 1 处）；探针输出与多边形版一致 | `docs/NAD-COVERAGE-2026-10-28.md` 铁律 2；09d8472（三仓同源修法） | 2 |
| W1-006 | 2026-10-31 | 并发队列 | 轮询式 `run-watch` 每 N 分钟扫一遍 + 哨兵重复投信 / watchdog 误判超时 → 同一个 run 被反复通知、误报 | 盯 run 靠轮询、没有内容指纹去重；容忍窗口与真实节奏不匹配 | 时间：噪音淹没真告警（等于没监控） | `run-watch` 改事件驱动（`workflow_run: completed`）+ 每小时兜底；投信/看门狗加内容指纹去重；容忍窗口与新节奏同步 | 同一 run 只投一次信；人为制造「长跑」不再误报 | 0513380 | 0 |
| W1-007 | 2026-10-31 | 测试与CI | 本仓的对外契约（tag 名、资产名、`MANIFEST`/`STATUS` 字段语义）变更后，**下游两仓不会自动告警**，只能等它们跑挂才发现（例如 `phase` 语义、`-pN` 分卷、addrfeat 命名） | 三仓之间没有契约测试，一仓改动不跑下游 | 时间：契约漂移要靠下游返工才发现（最贵的一类） | 契约写进 `docs/交付契约.md` 并加最小校验脚本（字段齐全 + 卷清单 + phase 枚举）；下游在自己的清单里回链本条款 | 故意删一个契约字段：一仓自检报错；下游 preflight 报「上游契约不符」 | 未修 | 0 |

## 2. 用法（照抄即可）

```bash
# ① 派单前：查清单 + 本次涉及的类别
#    故障清单已查：W1-001,W1-002   （或「无」）

# ② 判县类改动的最小自检（bbox 是本仓最贵的错）
python3 scripts/tiger_backfill.py --selfcheck-all --report-out /tmp/tiger-selfcheck.md
python3 - <<'PY'
# 断言：任何「判县」结论都带 polygon 证据，不带 bbox 结论
import pathlib
bad = [p for p in pathlib.Path('.').rglob('*.py')
       if 'bbox' in p.read_text(encoding='utf-8', errors='ignore')
       and 'polygon' not in p.read_text(encoding='utf-8', errors='ignore').lower()]
print("只有 bbox 没有 polygon 的文件：", bad or "无")
PY

# ③ 新坑当天补一行，然后
git commit -m "ledger: +1 采集器逻辑"
```

## 3. 证据路径索引

- `docs/TIGER-WARN-2026-10-30.md`（77 条 warn 逐县 HEAD 上游的归因表：AK 31 / GA 1 / ID 45）
- `docs/TIGER-BACKFILL-2026-10-30.md`（补抓终稿 + 零变化对比：49/52 tag 逐字节相同）
- `docs/NAD-COVERAGE-2026-10-28.md`（Clayton County GA 对照实验：bbox 235 / 多边形 0；bbox 判据作废声明）
- `docs/NAD-COVERAGE-FULL-2026-10-30.md`（`bbox_false_positive_n` 与「380 县 bbox 假阳性」结论）
- `docs/RELEASE_HEALTH-2026-10-23.md`（`TX-p2` 分卷行数）
- `docs/交付契约.md`（对外契约原文；W1-007 要求把它变成可执行校验）
- `scripts/tiger_backfill.py`、`scripts/nad_gap_plan.py`、`nad_county_probe.py`（判县/归因实现，W1-002/W1-005 的修复点）
