# 检索策略模板

每任务一份，记检索方案的可复现要素。填表说明见注释行（`<!-- 注：`），填完可删注释。

<!-- 注：研究问题写一句系统化问题；关键词与检索边界从其推导，不确定项写未知，不推断。 -->

- 研究问题：<一句话系统化问题，与知识包 §2 同一句>
- 问题自检：可检索 ✓｜可回答 ✓（不通过记追问与定稿；口径见 references/protocols/hitl-protocol.md）
- 中英关键词表：中文主题词 | 英文通用名 | 商品名 | 别名（来源：用户 / 由研究问题推导；词表版本必记，如 lexicon vX.Y；无表记「无表（不扩展、不猜关系）」）
- 概念组与检索式：每个关键概念一组，组内同义词／别名 OR、组间 AND，按来源语法拼检索式
- 来源与日期窗：来源（PubMed、OpenAlex、Crossref 等）| 全窗起止 | 近两年窗起止
- 发表类型筛选：随机对照、指南、系统综述等（放宽过程记筛选日志）
- 检索式逐条：来源 | 检索式原文 | 执行日期 | 数据采集 | 命中数
- 数据采集＝该格数据的自身采集日期：命中缓存记缓存写入日（原采集日），本次取数记运行日；读数取检索 CLI 输出的 `queryPlan.queryStatuses[].collectedAt` 与 `cacheHit`（窗口 168 小时、命中不顺延，见 workspace-guide §14.2）。回填命中的摘要／venue／被引自成一天，日期另取 `queryPlan.enrichCacheHits[].collectedAt`（逐条命中的原采集日），与本轮检索日期分记。执行日期始终记本次运行日——缓存读数不得写成当日抓取。
- 术语扩展：查询词 | 词表版本 | 方向（同义／正字归一／上级→下级／下级→上级）| 带出词 | 排除易混（`uv run python scripts/term_expand.py "<词>" --log` 的行原样入日志）

## 源集合声明与覆盖账

本节记本次交付的源集合（在组源／未纳入源与原因）与覆盖账指针；机制节读数（引文扩展／迭代补漏／高被引缺席核对）与本节的源声明都由覆盖账收口。

<!-- 注：源声明件 `run/b1/source-declaration.json` 为机器真源（`{"sources":["pubmed","openalex"],"not_included":[{"source":"google_scholar","reason":"默认关闭（需密钥）"}],"gs_enabled":false,"note":"…"}`）；本节表格与它同源，不另立口径。未纳入源＝零覆盖面（Google Scholar／试验注册库／预印本面／中文商业库），有则逐条记原因、无则记 `—`；GS 位写 `gs_enabled` 真值，未声明记 ∅（**不得默认写成「未启用」**）。`pool_kind` 三值＝检索／引文扩展／追加轮；**缺省口径＝检索池**（Step1 池不写该字段时按检索计；扩展／追加池由 `citation_expand.py`／`gap_fill.py` 写入）——A-01 只数 `pool_kind=检索` 的池，扩展／追加池分列读数、不断言（口径冻结在 ADR-0023）。 -->

| 源 | 状态（在组／未纳入） | 本轮用途 | 理由 |
|---|---|---|---|
| PubMed | | 发现层检索（Step1 双源） | |
| OpenAlex | | 发现层检索（Step1 双源）＋引文扩展轮／高被引缺席核对直连取数 | |
| Crossref | | | |
| Google Scholar | | | |
| 试验注册库／预印本面 | | | |
| 中文商业库（CNKI／万方／维普） | | | |

- 未纳入源与原因（逐条）：`<源>｜<原因>`（如 `试验注册库｜本轮问题为药物疗效与指南推荐，注册库记录非纳入源`）；无未纳入源记 `—`。
- 池级读数（按 `pool_kind` 分列）：检索池 N 个（候选 M 条）｜引文扩展池 N 个（候选 M 条）｜追加轮池 N 个（候选 M 条）——候选数＝各池 `results` 条数；读数件 `run/b1/step1-pools.json`（池级唯一家，账不复制）。A-01 取检索池个数；B-05 取 `dedupe-report.final_records[].origin=discovery` 分量。
- 归属口径（ADR-0024 配套票 03）：`origin`＝**最先入库池**（B-05 与 `s_cn` 按此读，**不得改义**）；贡献＝**全部来源池**（`dedupe-report.final_records[].pool_ids`）。两问分两处回答：「按机制分列的队列条目数」按覆盖账的 `contributed_*` 读数（`contributed_queue_kinds` 的键值＝按机制分列的队列条目数）；「某条队列条目的全部贡献池」按 `contribution_pointer` 逐条查最终名单件的 `pool_ids`（同一 DOI 多条记录取并集）——**不得**用 `origin` 代读（口径与读法见 `references/protocols/step2-dedupe-and-snapshot.md` §6）。
- 覆盖账产出：`uv run python .claude/skills/med-lit-review/scripts/coverage_ledger.py --delivery <交付>` → `run/b1/coverage-ledger.json`（`run/` 忽略位）；九块机制读数与指针的唯一家（引文扩展／迭代补漏／高被引缺席核对／经典段／源失败／命中上限／日期过滤／源声明／队列），本节与筛选日志、知识包 §3／§8 只写指针与缺口句，不复制读数。引文扩展／迭代补漏两块另带贡献归因读数（`contributed_final_count`／`contributed_queue_count`／`contributed_queue_kinds`／`contributed_queue_origins`／`contribution_pointer`）。
- 账→人类面：机制执行与新增量摘要行写知识包 §3；缺口三段式（`机制｜读数｜缺口与补法`）写知识包 §8「覆盖缺口」块；账产出命令末尾打印的机制行与缺口行与账同源，照抄即可（数字以账为准）。

## 引文扩展（单轮 1-hop）

每任务一节，记引文扩展轮的输入指针与读数；时点＝Step2 首轮排序完成后、规模确认之前（扩展先于规模确认），读数取 `run/b1/citation-expansion.json`。执行 `uv run python scripts/citation_expand.py --delivery workspace/YYYY-MM-DD-<slug>`（直连 OpenAlex＋Europe PMC；零改 jadense、不经 MCP、每轮实查不设缓存位）。

<!-- 注：种子＝`run/b1/sort-report.json` 的 `order` 1–20（头部位次；最终名单不足 20 取全池并记实际条数）∪ 指南/SR 判定集 `run/b1/citation-seeds.json`（agent 产，每条写理由 `head-rank N`｜`guideline-sr` 与依据字段；脚本只校验形态不代判，判定集零命中记一行、继续不阻断）。方向＝后向（种子引用的）＋前向（引用种子的）双方向全跑、单轮不迭代。源＝后向 OpenAlex 主＋EPMC 证；前向 OpenAlex＋EPMC；S2 前向仅当两源对同一种子双空读时补一次（字段表不含 `references.authors.name`）。上限＝每种子每方向 前向 ≤100／后向 ≤50，超限只截取并记截断；整轮 ≤300 请求含重试，逼近即停未跑格并记预算耗尽。过滤裁决只作用于引文扩展池，且**不省请求、不省配额**；被滤条目**既不得写成「缺席」（源侧没有）、也不得写成「截断」（源侧可及但未取）**——它是已取回但按口径不入池，另计 `filtered` 并逐条留在读数件 `filtered[]`，复议凭此回看。 -->

- 种子清单：`run/b1/citation-seeds.json` 判定集 N 条｜头部位次 N 条（`order` 1–20）｜拼装去重后种子 N 条
- 方向与源：后向（OpenAlex 主＋EPMC 证）＋前向（OpenAlex＋EPMC，＋S2 补通道）
- 上限与时限：前向 ≤100／后向 ≤50 per seed per direction（＝每方向入池上界，合并方向桶时二次截取、主通道优先；通道取数上限同值，`openalex-bwd` 另受 ≤50 id／请求夹取）｜预算 ≤300 请求｜EPMC 每种子每方向 1 请求｜运行日 <YYYY-MM-DD>
- 摄入过滤（`filter_policy`，ADR-0024）：通过＝被引 ≥100 或 发表年份 ≥ 参考年−3（含当年）；被引读数缺失一律不入池（fail-closed，全通道含 EPMC）、年份缺失按「新」走豁免档；只作用于引文扩展池（检索池与追加轮池零改动）；上限与截断口径不变（上限＝取回条数、不补取）。口径逐交付记：版本 `cite-ingest-filter-v1`（照读数件 `filter_policy.version`）｜阈值 `被引 ≥100`｜豁免窗口 `参考年−3 起（含当年）`｜范围 `引文扩展`｜开关态 `启用／未过滤`（`--no-filter` 关闭时记「未过滤」，**不得记 0**）——读数取 `run/b1/citation-expansion.json` 的 `filter_policy` 与被滤清单 `filtered[]`（字段 doi｜title｜cited｜year｜direction｜channel）
- 预算读数：请求 N／300｜判停（种子集跑完／预算耗尽／全通道不可用中止）｜通道命中 N／入池 N（**通道级 `pooled`＝过滤前可入库数**；实际入池以种子级读数与并池 `pools[].records` 为准）／无标识丢弃 N｜摄入过滤 被滤 N 条（关时记「未过滤」）｜截断 N 通道｜失败 N 通道（逐条带原因）
- 新增题录数：去重前 N｜去重后 N｜入池新增 N（只统计入池件；无标识丢弃 N 条单列，同数见筛选日志机制行）
- 贡献读数（ADR-0024 配套票 03，口径见 `references/protocols/step2-dedupe-and-snapshot.md` §6）：`贡献 最终名单 A 条／队列 B 条（按贡献池类 <类集合> N／…；按最先入库池 <origin 标记> N／…）`——`contributed_final_count`／`contributed_queue_count`／`contributed_queue_kinds`／`contributed_queue_origins`，读数从 `dedupe-report.json` 的 `pool_ids` 与 `step1-pools.json` 的 `pool_kind` 派生（账侧唯一家，本节只抄账产出末尾的机制行）
- 并池与重跑：扩展池以 `pool_kind=引文扩展` 追加进 `run/b1/step1-pools.json`，随后重跑 Step2（重去重／重排／分桶）；`origin`＝**最先入库池**、贡献＝`dedupe-report.final_records[].pool_ids`（**全部来源池**），两读数分列落覆盖账（`contributed_*`，见 §6）——只见其一必错
- 词表生长循环：本轮检出的词表范畴词（药物名／研发代号／疾病名／别名）当场 `uv run python scripts/term_expand.py "<词>" --append --source "引文扩展轮"`；试验缩写与指南名不落缓冲，记筛选日志实体清单
- 引文图读法（读法归票 04，取数不重复请求）：读本节产物 `run/b1/citation-expansion.json` 与其 `raw/citation-*.json`，算「被池内引用计数」（＝被池内有引用表的种子引用的次数；非种子池记录无引用表，故为**下界**）、文献耦合（种子间共引参考文献条数）、共被引（参考文献被同批种子共引的次数）；读数件 `run/b1/citation-graph.json`（`uv run python scripts/citation_graph.py --delivery <交付>`，请求 0、不写池）。用途＝经典识别／影响标注／缺席核对证据；**不作排序输入**（权重口径冻结在 `sort-weights-v2`，增减维随 05／07）。

<!-- 注：覆盖账落点＝本节＋筛选日志机制行；扩展池**不计入** A-01／B-05 断言（口径冻结在 ADR-0023：A-01 只数检索池、B-05 只断言检索池分量；扩展／追加分量分列读数见覆盖账与筛选日志）。 -->

## 迭代补漏（两波追加轮）

每任务一节，记实体盘点清单与追加轮读数；时点＝Step2 首轮排序完成后、下载前（**与引文扩展轮同窗**——两者各记其账、并池重排一次）；盘点基准＝**排序最终名单**（非下载队列）。执行 `uv run python scripts/gap_fill.py --delivery workspace/YYYY-MM-DD-<slug>`（检索 CLI＝jadense 的 `dist/bin/scholar-search.mjs`；只跑全窗，近两年窗不重跑；来源与 limit 随主轮口径，取 `step1-pools.json` 的 `providers`／`limit_per_query`）。

<!-- 注：盘点清单落 `run/b1/gap-entities.json`（agent 产；脚本只校验形态不代判）：四类实体＝关键试验缩写／药物（含研发代号与商品名）／疾病名／终点词；来源＝研究问题关键词表＋领域先验＋池内指南与 SR 提及；逐行记「为何预期该有」的依据；顶层 `indication` 必填（追加轮检索式＝实体＋药物＋适应症）。基线判定＝实体在最终名单 0 题录→缺口；关键试验类仅有次级文献（无 pivotal 主报告）→缺口；其余判「在池」。 -->

| 实体 | 类 | 为何预期该有（依据） | 基线判定 | 终态 |
|---|---|---|---|---|
| | 关键试验／药物／疾病／终点 | | 在池／缺口（0 题录）／缺口（仅次级文献） | 在池／未闭合（原因） |

- 波数与上限：波 1＝定向实体检索（缺口实体：缩写／代号＋药物＋适应症组合，沿用先例空格组合形态）｜波 2＝结果回授（读**最终名单**题录／摘要——最终名单已在盘上，**主路径单次跑完两波**：回授新词与新检出词随清单一次带进 `new_terms`，脚本同次调用即重拼未闭合实体的检索式）｜**每波 ≤4 query**（＝单次检索 CLI 运行上限）｜**≤2 波**（合计 ≈≤8 query）；两个上限在脚本内写死、不做配置
- 判停（票面口径）：某波**无新增实体／词**即停（三面读数分记：新增题录数／新增实体数／新增词数；判停取实体／词两面，未用的回授新词计在词面）；两波封顶仍存未闭合＝达上限
- 跨次台账（可重入）：已发检索式与波号持久化在读数件 `query_ledger`；二次调用不重发已发检索式、波号续编，`waves`／`query_count` 记合计、`run_waves`／`run_query_count` 记本轮
- 检索式逐条（追加轮行）：窗列记「全窗补」，多波标「全窗补·波N」；其余列同 Step1 表；数据采集取 `run/b1/gap-fill.json` 各波 `collected_at`（缓存命中的原采集日，不顺延）
- 读数：`run/b1/gap-fill.json`（逐实体闭合表＋逐波读数＋`query_ledger`＋机制块 `ran｜entity_checklist_count｜gap_entity_count｜waves｜query_count｜new_before_dedupe｜new_after_dedupe｜new_entity_count｜new_word_count｜run_waves｜run_query_count｜closure｜stop_reason｜unclosed_entities[]`）｜原始响应 `run/b1/raw/gap-fill-w<波>.json`
- 并池与重跑：追加轮记录以 `pool_kind=追加轮`／`origin=gapfill_w<波>`／`window=全窗补·波<波>` 追加进 `run/b1/step1-pools.json`，随后重跑 Step2（`uv run python scripts/step2_dedupe_sort_bucket.py --delivery <交付>`）；段键含池文件 sha256，故并池后自动重算、快照按 DOI 重叠复用、**不新增缓存条款**；并池时**先前波池随本轮池一并写回**（续跑不顶替，读数件 `prior_pools` 与「一并续存」note 在案）；去重计数按终态口径一次记录（读数件 `new_records`）
- 贡献读数（ADR-0024 配套票 03）：`origin=gapfill_w<波>` 只说明**最先入库**的是哪一波追加轮池；贡献取该条目来源池的并集——`贡献 最终名单 A 条／队列 B 条（按贡献池类 <类集合> N／…；按最先入库池 <origin 标记> N／…）`（类集合＝贡献池类的组合键，跨机制共贡献如 `引文扩展＋追加轮` 照实成键），读数见覆盖账迭代补漏块的 `contributed_*`（读法见 `references/protocols/step2-dedupe-and-snapshot.md` §6）
- 实体闭合律：每实体终态＝**在池**｜**未闭合**（已定向搜过仍缺，逐条记原因；未搜者按因分记三档——「未列入已跑的波次计划」／「波数已用满」／「超 8 query 上限」（query 预算真用满才记末档），计划内检索失败记「检索未完成（波N 未跑通）」）；未闭合项句式接 §8 缺口块——`- 迭代补漏缺口：<实体>（<类>）按「<检索式>」全窗定向检索（波 N）后，<原因>；相关结论待补检索或待新发表，本次如实记局限。`
- 词表生长循环：波内检出的词表范畴词（药物名／研发代号／疾病名／别名）**未命中即当场** `uv run python scripts/term_expand.py "<词>" --append --source "追加轮"` 落候选队列（判不准按 AFK 提案口径显式 `--no-edge`）；**试验缩写不落缓冲**，记盘点表与机制读数 `detected_acronyms[]`

<!-- 注：覆盖账落点＝本节＋筛选日志「机制记账（迭代补漏）」节；机制块字段供 06 票消费（本节只报读数）。 -->

## 高被引缺席核对（被引降序 top-N 对账，只核对不入池）

每任务一节，记核对输入、深度与缺席清单指针；时点＝Step2 首轮排序完成后、规模确认之前（与引文扩展轮、迭代补漏同窗）。执行 `uv run python scripts/high_cited_check.py --delivery workspace/YYYY-MM-DD-<slug>`（直连 OpenAlex `sort=cited_by_count:desc`；零改 jadense、不经 MCP）；判定阶段（规模确认拍板后回写处置）加 `--classify-only`，不发请求。

<!-- 注：核对输入落 `run/b1/high-cited-queries.json`（agent 产；脚本只校验形态不代判）：1–3 组主题关键概念查询（`queries[]{query,concept}`）＋可选经典清单（`checklist[]{id,kind,match[],doi?}`，词面或 DOI 命中即检出）＋可选日期窗（`window{from,to}`；缺省＝全窗口径、无窗外判定）＋可选 `indication`（补入提案用）。基础取数＝每查询 1 请求（top-100，`per-page=100`）；清单未闭合按页加深（每页 1 请求），深度无固定上限、整轮受 `--budget`（默认 30）夹取；多查询轮次交替加深。判停：清单闭合／检索集耗尽／被引带下沿／清单无命中／预算耗尽／取数失败。分类列：相关／超题由 agent 判（`run/b1/high-cited-judgments.json` 的 `judgments[]{doi,verdict,reason,kind?}`）、窗外由脚本按日期窗机械判、未判＝未给判定；处置读同件 `disposition{verdict,note}`（补入／不补）。只核对不入池：补入走盘点表＋`gap_fill.py`，本通道不写池、不自动合并。 -->

| 查询原文 | 概念 | 深度（所取条数） | 集合规模 | 判停 | 缺席（相关／超题／窗外／未判） | 经典段（检出／不可达） | 读数指针 |
|---|---|---|---|---|---|---|---|
| | | | | | | | `run/b1/high-cited-check.json` |

- 机制读数（账目字段，供覆盖账消费）：`ran｜queries[]{query,depth}｜absent_total｜absent_relevant｜absent_off_topic｜absent_out_of_window｜user_disposition`；经典段 `checked｜found｜unrecovered[]{id,reason}`；未选取读数不落在本件——由选取步计入 `run/b1/selection.json`（`unselected_total`／`unselected_by_rank_band`／`unselected_by_origin`，§8 第 10 类机制句读它）；`classic_segment_cut`（ADR-0023 冻结名）＝清单项在池且不在研读选取集，同样由选取步算、写进同一件（本通道不再自算；无高被引读数件时记 `null`＋`note`，不编造）。
- 同一性（清单项命中与位次归属）：带 `doi` 的清单项**只认 doi 相等**；无 `doi` 的按 `gap_fill.contains_phrase` 的词元序列整段口径匹配题名（不因子串误中）。**唯一命中才认定**——通道侧多义（≥2 候选）不作检出、不进带下沿，逐条记「词面多义（不认定）」；最终名单位次只在 DOI 直查或词面**唯一**回查时归属，多义记 `in_terminal: null`＋`in_terminal_source: ambiguous_title`（在池与否记 ∅，不写 false）。
- 不可达经典逐条记因（机制＋证据：查询原文与所取深度；不承诺全量召回）：`- 高被引缺席核对未达：<清单项>｜<查询>（集合 N，取 M 条）内词面与 DOI 均无命中。`
- 未选取读数（指针 `run/b1/selection.json`）：`unselected_total = 终池 {X} − 研读选取 {Y}`；`unselected_by_rank_band` 按 S 位次固定五档＋`无位次`分列、`unselected_by_origin` 按来源池分列（键取原始 `origin` 值）——三读者唯一家＝该件，逐条不再复制；补法：需要时另开一轮或以人工单篇归置。实例（历史交付，按当时口径读）：LDL-C 交付 WOSCOPS order 320／LIPID order 403 均不在研读选取集内——该批读数的旧形态按「最终名单 − 队列」计（资产 §9 的 251 系队列文件行数、含表头注释行），本模板不再沿用该口径。
- 补入接口：读数件 `pool_entry_proposals.rows` 为盘点表 `entities[]` 行形态（`name／kind／basis／aliases／drug`）；拍板补入后并入 `run/b1/gap-entities.json` 再跑 `uv run python scripts/gap_fill.py --delivery <交付>`；本通道不写池。

<!-- 注：覆盖账落点＝本节＋筛选日志「机制记账（高被引缺席核对）」；机制块字段供 06 票消费（本节只报读数）。 -->

## 中文手动补充（B1）检索动作

用户在商业库执行检索，agent 不代搜、不碰机构账号；导出文件原样保留并记路径。汇入点为发现之后、分桶之前（同设计同权）。

<!-- 注：命中数为商业库检索页自报值，检索词与日期窗须完整可重跑；解析失败条目原样退回并报行号，不猜补。 -->

| 库名 | 检索词 | 日期窗 | 执行日期 | 命中 | 导出 | 解析 | 汇入（去重后） |
|---|---|---|---|---|---|---|---|
| CNKI | | | | | | | |
| 万方 | | | | | | | |
| 维普 | | | | | | | |

- 中英分别计数：英文源 N 条 ｜ 中文源 N 条 ｜ 合并去重后 N 条（报数只数正式题录，不数附件与笔记；0 命中≠缺失，走同义词→放宽类型→查窗后仍零则如实记）。
- 解析失败原样退回：文件 | 行号 | 原文本 | 原因。

## 监管直取（B2）检索动作

独立于发现与下载，直入归置；只取规范性断言，不做疗效解读。

| 来源 | 文件 | 版本/文号 | 取用日期 | 原文留档路径 |
|---|---|---|---|---|
| FDA／EMA／NMPA | | | | |

- 未获官方原文的条目：记待核对，不进证据表可追溯列。
- 凭证类（密钥、登录票据、代理凭证、机构信息）一律不记。

## 外供输入（P3 已有文献写总结）

- P3 默认不跑发现／分桶／下载：检索策略节保留标题，写外供说明——`外供输入：本地 PDF×{N}／库内条目键×{M}；上游日志引用（文件名＋日期＋词表版本，无则记未知）`；本节为外供说明、非可复现检索式。
- 外供缺研究问题陈述：ST-02（warning）照报并进局限，不豁免。
- 经 Other 增回发现链后按本模板全量记。
