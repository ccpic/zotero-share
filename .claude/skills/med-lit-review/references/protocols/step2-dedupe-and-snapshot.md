# Step2 分类分桶（去重／排序／分桶与两处缓存）

本文件是 Step2 执行做法的唯一家：脚本怎么调、输入输出落点、两处缓存（被引／OA 快照复用、确定性段短路）
的键与命中行为、疑似裁定表怎么填。规则真源不在这里——窗口与命中规则见 `references/protocols/workspace-guide.md`
§14.2、复用与留痕口径见 §14.1／§14.4、清理口径见 §15，去重记账户数与排序维度见
`references/templates/screening-log-template.md`，交互点见 `references/protocols/hitl-protocol.md`；
本文件只写做法与指针。

## 1 脚本与调用

本步两件脚本：`step2_dedupe_sort_bucket.py`（三轮去重／五维排序与两处缓存，本文件主件）与 `step2_select.py`（四桶分桶与研读选取定稿，规模确认前后各调一次；交互面见 `references/protocols/hitl-protocol.md` 规模确认节）。

`uv run python .claude/skills/med-lit-review/scripts/step2_dedupe_sort_bucket.py --delivery workspace/YYYY-MM-DD-<slug> [--cur-year YYYY] [--verdicts <路径>] [--cache-dir <目录>] [--refresh | --no-cache]`

`uv run python .claude/skills/med-lit-review/scripts/step2_select.py plan  --delivery workspace/YYYY-MM-DD-<slug> --n 30 [--n2 60] [--must-take <路径>]`（零写读数）
`uv run python .claude/skills/med-lit-review/scripts/step2_select.py apply --delivery workspace/YYYY-MM-DD-<slug> --n 60 [--must-take <路径>]`（定稿三件）

| 输入（交付内，只读） | 说明 |
|---|---|
| `run/b1/step1-pools.json` | 发现清单（Step1 原始池）：`{"pools": [{"pool_id", "family", "window", "results": […]}]}`；顶层 `frozen_at`（池采集时刻）**必需**——它是入库摘要表 `collected_at` 的唯一真源，缺即拒绝产出（exit 2），不编造时刻、不用重跑日顶替；顶层可选 `current_docs`（现行版豁免 DOI）与 `pico_groups`（相关性命中的 PICO 词组，逐组命中即计分——`s_rel` 即「题名＋正文」对这些词组的覆盖度）——缺项保持未知（`s_rel` 记 ∅），不编造。**扩展池记录的正文由机制侧给出**：引文扩展轮的 **OpenAlex 题录**从同批响应的 `abstract_inverted_index` 按 position 还原（零额外请求），EPMC／S2 的正文由通道字段原样保留——扩展记录因此不再恒为空串，相关维与完整度维对它们不再被结构性压低；OpenAlex 侧不可还原者记 `null`（∅，**不写空串冒充**，成因见记录 `abstractReason` 的 `{class, message}`），EPMC／S2 侧通道未给正文时仍是原位空串——两类对本脚本同义（`rec.get("abstract") or ""` 归一），故「空正文」的判定与未知维语义都不因此改变。`results` 记录中 `authors` 须为字符串列表：检索 CLI 原样返回的 `[{"displayName": …}]` 形态须在装配时投影为字符串（脚本 `norm_author` 只收 str，不做类型归一；直接喂原样结果会在 R1 去重处 TypeError；池级可选 `origin`（来源标记，缺省 `discovery`）——引文扩展轮追加的池带 `citation_bwd`／`citation_fwd`，脚本只读池值、不另判，标记原样进 `dedupe-report.final_records[].origin` 崩溃） |
| `run/b1/step1-diagnostics.json` | 发现层格级读数（逐 池×源 一行）：**字段名由 ADR-0023 冻结为 snake_case**（`pool_id｜query｜provider｜status｜hit_count｜total_available｜cap_hit｜cap_policy｜date_filtered_count｜date_unknown_count｜date_rejected_count｜attempts｜retried｜recovered_by_retry｜cache_hit｜collected_at`；`total_available: number\|null`、`cap_hit: boolean\|null`，null 不得读作 false）。由 `uv run python .claude/skills/med-lit-review/scripts/step1_diagnostics.py --delivery <交付>` 从逐池原始响应投影产出——检索 CLI 的 `queryPlan.queryStatuses[]` 是 camelCase（`hitCount／totalAvailable／capHit／capPolicy／dateFilteredCount／dateUnknownCount／dateRejectedCount／recoveredByRetry／cacheHit／collectedAt`），**不得原样复制**：不投影即让下游（覆盖账的命中上限／日期过滤／源失败三块与 §8 分句）恒读不到数。CLI 缺的冻结键记 `null`（未取数，不得写成 0／false）；CLI 行其余键原样透传（失败格 `error` 供覆盖账源失败块）。只收检索池（`pool_kind` 缺省按 `检索`）；机制取数件不进本件（其截断／失败读数在各自读数件）。 |
| `run/b1/b1-records.json` | 中文手动补充（B1）：`{"records": […"publication_year"...]}`；B1 是条件分支，文件可缺——缺席按空表记（R2 记 0／0／0，筛选日志按轮空行记账） |
| `run/b1/step2-verdicts.json` | 疑似裁定表（人工／AFK 位，可缺）；格式见 §3 |
| `run/b1/must-take.json` | 必取集判定件（人工／agent 写，可缺）：`{"note", "entries": [{"doi", "basis"}]}`；读入时 DOI 一律过 `norm_doi`、`basis` 必填非空、同 DOI 重复即退出码 2；**文件缺席＝空必取集**（stderr 提示一行，账内记 `must_take_input: "absent"`）。由选取步读 |

| 输出（交付内） | 说明 |
|---|---|
| `run/b1/dedupe-report.json` | 三轮（R1 发现内部／R2 中文内部／R3 两表合并）输入／保留／合并／疑似计数（另给 `suspected_ambiguous_count`＝其中待人工裁定者）、逐条合并来源轨迹、无稳定身份剔除、终池 |
| `run/b1/dedupe-decisions.json` | 裁定结果（`decisions`／`unadjudicated`／`decisions_unused`）；人工裁定带裁定表的人工位字段，规则判定带 `decided_by: "rule"` |
| `run/b1/cited-counts.json` | 被引快照：`snapshot.query_date`＝各读数日期中最陈者（全同即该日）、`query_dates` 日期集、`batch_range` 本轮实查／复用分布、`counts` 逐 DOI 被引（无覆盖与传输失败记 ∅） |
| `run/b1/sort-report.json` | 五维排序（权重版本冻结、未知维 ∅、无证据等级字段） |
| `run/b1/openalex-enrich.json` | 被引与 OA 定位逐条读数（无覆盖清单、传输失败清单） |
| `run/b1/bucketing-report.md` | 四桶报告＋逐条路由预测（**由选取步产出**，行集＝研读选取集；忽略位中间件，只含内容稳定读数）。保留行 `- 桶计数之和：{total}；队列条目数：{len(queue)}（一致）。` 供 B-09 读，原样不动 |
| `run/b1/ingest-abstracts.json` | 入库摘要表（ADR-0021；忽略位中间件）：`entries`（归一 DOI → `{abstract, source, collected_at}`）／`missing`（无正文的归一 DOI 清单）／`coverage`（`denominator`／`with_abstract`／`unknown_type`／`inputs`）；口径见 §5，入库侧按此形态判读。再生命令＝重跑本脚本 |
| `run/b1/selection.json` | 研读选取机器账（**由选取步产出**）：`target_n`／`selected_total`／`must_take_count`／`rank_fill_count`／`must_take_input`／`fill_boundary_order`／`selected_order_min`／`selected_order_max`／`pool_total`／`unselected_total`／`unselected_by_rank_band`／`unselected_by_origin`／`classic_segment_cut`／`entries[]`；∅ 口径＝未取数记 `null`、实测零记 `0`／`[]`（不得互顶），`fill_boundary_order` 在 k=0 时记 `null` |
| `agent/download-queue.txt` | **研读选取集＝下载队列＝入库集**（一行一条 DOI，按 S 序；**由选取步 `apply` 产出**，Step3 的输入，剪队列不改写本件）；本脚本不再产本件 |
| `agent/doi-list.txt` | 版内件**首写**（已在位即不覆盖）：身份列表（终池全量 DOI，一行一条、含未入选者；与研读选取集件契约分离） |
| `agent/bucketing-and-sources.md` | 下载台账骨架（§7 十列，行集＝研读选取集，口径列取 `agent/manifest.json` 的 `caliber.grey_gate_version`）：**由选取步首写**（已在位即不覆盖——绝不回写 Step3 已填的台账）。P1 无选取步，本件由 `step2_select.py pool-ledger --delivery <交付>` 以全池行集产出（版内四桶报告终件，写入口径＝覆盖；P0 下由选取步 apply 以选取集行集首写）。 |

- 排序年份口径 `--cur-year` 进段键：跨年重跑即重算（时效维按年份位移）。
- 排序并列以 DOI 次序稳定（得分相同即按 key 排）——研读选取集行序（＝S 序）只依赖输入内容，不依赖池顺序；桶按选取集行分列。
- 选取步在**排序终池定稿后、规模确认前**跑 `plan`（零写读数，供规模确认题面），拍板后跑 `apply` 定稿三件；口径＝`必取集 ∪ 按 S 序（order 升序）前 k 条非必取行`，`k = max(0, N − m)`（m ≥ N 时 k=0、篇数记 m），行集只取有 DOI 键的行（跳过 `T:` 键与重复键，与队列构建同口径）；共用函数（`bucket_of`／`expected_source`／`BUCKET_ORDER`／`BUCKET_EXPECT`／`read_json`／`norm_doi`）自本模块与 `step3_queue` 导入，不复制第二份。
- `--cache-dir` 不得落在交付目录内（缓存不是交付件，脚本拒绝）；默认落 skill 包内被忽略位。
- 引文扩展轮（票 02）在本步**首轮排序完成后、规模确认之前**调用：`uv run python .claude/skills/med-lit-review/scripts/citation_expand.py --delivery workspace/YYYY-MM-DD-<slug>`（直连 OpenAlex＋Europe PMC，零改 jadense、不经 MCP、每轮实查），把扩展记录以 `pool_kind=引文扩展` 的池追加进 `run/b1/step1-pools.json` 后**重跑本脚本**（重去重／重排）与选取步（分桶与选取随终池重算）。段键含 `step1-pools.json` 的 sha256 与本脚本自身 sha256，故并池后自动重算、**不新增缓存条款**（`workspace-guide.md` §14.2 无追加）。写法见 `references/templates/search-strategy-template.md`「引文扩展」节与 `screening-log-template.md` 机制记账节。
- 迭代补漏（票 03）在**同窗**调用：`uv run python .claude/skills/med-lit-review/scripts/gap_fill.py --delivery workspace/YYYY-MM-DD-<slug>`（盘点排序终池四类实体→至多两波定向补检索；只跑全窗；来源与 limit 随主轮口径，取池文件 `providers`／`limit_per_query`；**每波 ≤4 query＝单次检索 CLI 运行上限、波数 ≤2 在脚本内写死**），把补漏记录以 `pool_kind=追加轮` 的池（`origin=gapfill_w<波>`、`window=全窗补·波<波>`）追加进 `run/b1/step1-pools.json` 后**重跑本脚本**与选取步。段键随输入变化自动重算（完整键项见 §2：池文件与中文补充、裁定表、本脚本 sha256、权重版本与权重、`--cur-year`、快照逐条读数指纹与日期集），故并池后自动重算、**不新增缓存条款**；逐实体闭合表与机制读数见 `run/b1/gap-fill.json`，写法见 `search-strategy-template.md`「迭代补漏（两波追加轮）」节与 `screening-log-template.md` 机制记账（迭代补漏）节。

## 2 两处缓存

两处缓存落在 skill 包内被忽略位（默认 `.claude/skills/med-lit-review/.cache/step2/`，`--cache-dir` 可改；
清理口径见 `workspace-guide.md` §15）。窗口与命中规则见 §14.2，本表只写做法与键。

| 缓存 | 键 | 命中与落空行为 |
|---|---|---|
| O-03 被引／OA 快照（`cited/<键>.json`） | 一次实查的 DOI **排序集合** sha256 ＋ `select` 字段表（`snapshot_key`；不按块序号、不按块大小寻址） | ①当前块与某格键全等 → 整块命中；②否则按 DOI 逐条复用其它格中**重叠**的 DOI（跨块划分照样命中），只把剩下的送去实查；③实查成功才写格，**传输类失败（超时／5xx／连接）永不入缓存**，也不给它配查询日期（没读到的数据不记日期）；确定性阴性（无覆盖）入格并记原因。窗口自格内**实查时刻** `queried_at` 起算（秒级、精确 168 小时），命中不顺延 |
| O-04 确定性段（`segment/<键>.json`） | 输入哈希（`step1-pools.json` ＋ `b1-records.json` ＋ 裁定表 ＋ 脚本自身 sha256）＋ 权重版本与权重＋`--cur-year` ＋ 快照逐条读数指纹（`record`／`status`／`date`）与日期集（`segment_key`） | 键全等 → **整段短路**：去重、排序、入库摘要表一概不重算，按格内存的产物文本原地重发（内容未变不重写）；分桶与选取不在本段（由选取步按终池现算，随重排重跑）；任一键项变化即 miss 重算；内容寻址、无 TTL |

- 两处缓存都带**验算**：段格在用之前交叉核对守恒式（每轮 `input ＝ kept ＋ merged`、`merged_count ＝ len(removed)`、
  `len(final_records) ＝ R3 kept_count`，入库摘要表的 `denominator ＝` 终池标 DOI 记录数、
  `with_abstract ＝ len(entries)`、`entries＋missing ＝` 终池唯一 DOI 数且与 `dedupe-report.json` 的
  `coverage` 全等）并逐条核对裁定依据（§3），验算不过即当 miss 重算并把原因打到 stderr——
  手工改过的或异版本的格子（含「只改计数不改明细」这类自洽伪装）不可能把坏数据送进交付。
- 段产物的读数全部落在键内（报告里的日期、无覆盖与传输失败条数都按读数状态数出）；本轮缓存行为统计只进
  `cited-counts.json` 的 `cache` 块与运行日志，不进段产物——命中时不会拿上一轮的实查计数冒充本轮。
- 显式重跑：`--refresh` 无条件越缓存重查（快照）与重算（段），新读数照常写回缓存；`--no-cache` 全程不读不写（探针与排障用，正常执行不传）。
- 命中不虚报：快照读数（查询日期、被引、OA 状态、无覆盖原因）一律是整个读数的原样沿用，运行日志与
  `cited-counts.json` 的 `cache` 块如实记命中与实查计数，供筛选日志落「数据采集」日期（§7）。

## 3 疑似裁定表（人工位）

```json
{
  "decided_by": "executor（AFK 人工确认位）",
  "decided_at": "YYYY-MM-DD",
  "decisions": [
    {"pair": ["<a 归一 DOI>", "<b 归一 DOI>", "<a 题名归一前 20 字>"], "verdict": "合并", "reason": "…"}
  ]
}
```

- 写法：先不提裁定表跑一次，把 `run/b1/dedupe-decisions.json` 的 `unadjudicated[].pair` **逐字**抄进
  `decisions[].pair`，补 `verdict`（只认 `合并`／`判异篇`）与 `reason`（不得空挂）。
- 裁定三分：**人工裁定**（裁定表，`decided_by` 取表内值）／**规则已判**（题名键同而第一作者或年份不同 →
  规则判异篇，`decided_by: "rule"` 给规则原文，不占人工位、不必人工补）／**未裁定**（照实进 `unadjudicated`、
  疑似行 `decision` 留空）。脚本与段缓存都不代填人工裁定、不猜测；人工若给规则已判的对另下裁定，以人工为准。
- 裁定表里对不上任何疑似对的条目会在 `decisions_unused` 报出并终止（不静默吞掉人的裁定）。
- 裁定表改动即段键变化 → 重算：裁定结果不会从旧段落里被当成既成事实带过来。
- 去重键三层与疑似判定规则（DOI 一级键、标题归一二级键、中文三段键）见 `references/templates/screening-log-template.md`；
  筛选日志「疑似（待人工裁定）」列按 `suspected_ambiguous_count`（待人工裁定者）记，规则已判的对不占该列。

## 4 与门禁、台账的衔接

- `bucketing-report.md` 与台账骨架的四桶计数一致、且等于研读选取集条目数（行集＝研读选取集，桶按选取集行分列：OA 状态
  gold／diamond／hybrid／bronze → 开放直下；green → 仓储副本；closed 或 ∅ → 需机构；其余 → 灰色候选）；
  逐条路由是**预测**，实际来源与台账十列由 Step3 回填（`step3-download-chain.md`）。
- 被引实查范围＝去重前的候选 DOI 集（唯一 DOI 数≈终池，读数是终池的超集）；快照的查询日期作版本日期戳
  进可复现包（`SKILL.md` 可追溯契约）。
- 文档口径：`cited-counts.json` 的 `no_coverage` 为逐条 `{doi, reason}`（比历史包的纯 DOI 列表多带原因，
  便于把「为什么记 ∅」写进筛选日志）；历史包与演示件读数冻结不动。

## 5 入库摘要表口径（`run/b1/ingest-abstracts.json`）

Step2 的规范产物（ADR-0021）：入库侧取数的第一级，从终池机器产出、不再依赖手搓池文件。形态是硬契约——
入库侧 `use-zotero/scripts/abstracts.py` 的 `load_table` 按此判读，偏离即抛（表读错要炸，不静默降级）。

- 三块字段：`entries`（归一 DOI → `{abstract, source, collected_at}`）／`missing`（无正文的归一 DOI 清单）／
  `coverage`（`denominator`／`with_abstract`／`unknown_type`／`inputs`）。**键集＝终池标 DOI 记录按归一 DOI
  去重后的全 DOI 集**，表与清单互补、合起来正是它。
- **同 DOI 取最完整者**：正文取范围＝**终池记录的正文 ＋ 本次读入的全部同 DOI 池记录正文**（去重时被并入者、
  仍并列的同 DOI 记录都在内；同篇多来源取最长者），最长者胜出、并列取先出现者（位序稳定）；`source`＝胜出记录的
  池来源轨迹（`externalSource`，缺则 `retrievalProvider`，皆缺记 `unknown`；合并过的记录多条以「、」连接）。
  正文取池内原样文本（首尾空白去掉），清洗口径不在此复制——归入库侧同一份实现（ADR-0021）。终池外的 DOI
  （去重时整条被弃的无标识记录）不进表。
- **分母＝终池标 DOI 的「记录」数**：入库侧对每条这类记录都按可摘要文种取数，两侧因此同一口径；
  `with_abstract`＝有正文的 DOI 数（＝`entries` 条数）；`missing` 与 `entries` 按 DOI 键，故同 DOI 多条终池
  记录（未裁定的「DOI 同题名异」疑似对）时分母可大于二者之和。
- **`unknown_type`＝池未给 `type` 的记录数**：单列的数据质量读数，绝不计出分母之外（`type` 缺失者照计分母，
  入库侧同判可摘要）。终池记录的 `type` 经去重时的字段补齐而来，与原始池的无类型条数不必相等。
- **不造映射**：池 provider 的类型串（OpenAlex `article`／`review`／`editorial`／`erratum`）与入库侧文种
  （`journalArticle`／`preprint`／`conferencePaper`…）不是一套词汇，本表只报「有无」、不做转换——不要把它
  「修」成映射表。
- `collected_at` 取池顶层 `frozen_at`（池采集时刻）；`coverage.inputs` 记本次读到的上游件相对路径 → `sha256`
  （缺件记 `absent`）。机器读：`dedupe-report.json` 的 `coverage` 字段与表内同值；人读一行进 `bucketing-report.md`
  （覆盖率 N/M ＋ 无摘要计数 ＋ 类型未知计数）。
- 消费面：入库侧取数梯级的**第一级**（次序、清洗与文种适用性判定见 ADR-0021 与 `use-zotero/scripts/abstracts.py`，
  本文件不复述）；本表不直接写库、不参与分桶路由。
- 登记：忽略类产物，`agent/manifest.json` 的 `regeneration` 随分桶类产物一次登记（命令＝重跑本脚本，逐件记哈希），
  见 `workspace-guide.md` §6／§10。

## 6 归属口径：`origin`＝最先入库池｜贡献＝全部来源池

两个读数不是一回事，**不得互相顶替**（ADR-0024 配套票 03 修的正是这处混用）：

- `origin`＝**最先入库的池**：记录入场时按所在池标记（检索池缺省 `discovery`；引文扩展池 `citation_bwd`／
  `citation_fwd`；追加轮池 `gapfill_w<波>`；B1 中文补充 `b1_chinese`），合并顶替（字段更全者上位）时
  照抄原 `origin`（脚本内注释「origin 记最先入库的池（检索池在扩展池之前），顶替不改口径」）。本口径
  **向后兼容、不改义**：`scripts/citation_graph.py`、筛选日志的引文图读法行、中国证据维 `s_cn`
  （`origin == b1_chinese`）、门禁 B-05 的检索池分量（只数 `origin=discovery`）都按此读——
  **不得改写成「全部来源池」**。
- 贡献＝**全部来源池**：`dedupe-report.final_records[].pool_ids`＝该记录被哪些池吸收过（去重合并时取并集）。
  一条记录的 `origin` 只有一个，来源可以有多个——扩展轮捞回而检索池也命中的条目 `origin` 记 `discovery`、
  `pool_ids` 里却含引文扩展池；**只看 `origin` 会把这类条目读成检索池独占、低估机制贡献**。
- 同一 DOI 在终池可有**多条记录**（未裁定的「DOI 同题名异」疑似对逐条保留、不自动合并），研读选取集一行仍
  只有一个条目（按键去重）：读该选取集条目的「全部贡献池」取该 DOI **全部记录的 `pool_ids` 并集**，
  `origin` 取最先入库的那条记录。
- 覆盖账的贡献读数（引文扩展／迭代补漏两块各一份；从上述真源派生，账不复制逐条归因）：
  `contributed_final_count`（终池**记录**口径的内容数）／`contributed_queue_count`（口径＝研读选取集条目数；冻结字段名沿用）／`contributed_queue_kinds`（按贡献池类集合分列：`引文扩展`／`检索＋引文扩展`…，共贡献显式
  成键、各项相加不重不漏）／`contributed_queue_origins`（同一批条目按 `origin` 分列——**非本机制标记者
  即 origin 单项口径下的少算面**）／`contribution_pointer`（在场真源指针）。口径：池类缺省＝检索池
  （与 A-01 同一缺省）；同一 DOI 只计一次；轮空或未取数记 ∅（**不得记 0**）；终池件缺 `pool_ids`
  （旧形态件或段缓存回放的旧段）同样记 ∅＋`note`。**不另设终池字段**（如 `contributing_pools[]`）：
  `pool_ids` 已是逐条真源，再记一份即同一读数两处真源（ADR-0023），故只在账收派生汇总与指针。
- 读法：**「某条研读选取集条目的全部贡献池」逐条查**——按 `contribution_pointer` 打开终池件取该 DOI 的
  `pool_ids`，再经 `step1-pools.json` 的 `pool_kind` 归类；**「按机制分列的研读选取集条目数」查账**——取两块
  `contributed_queue_kinds` 的键值并集（各机制的贡献条目数＝其各行相加；研读选取集条目数 − 各机制贡献的并集
  ＝仅检索池条目数——**但终池无任何 `pool_ids` 的条目（非池来源，如 B1 中文补充）不属于任何机制**，
  账在 `note` 里单列，勿按该差式读成「仅检索池」）。**不要**用 `origin` 单项口径回答这两问——它答的是
  「最先入库」。
