---
name: med-lit-review
description: 医学文献知识综述：以入口确认的一句话研究问题驱动的系统完备知识包，编排检索/下载/入库 skill，医学证据类型优先。仅当用户明确要医学文献/药物试验/循证交付物，或给定具体医学主题并要综合证据，且属人用药/疾病/生理/临床证据/指南共识/监管文件范畴时使用；单事实/概念问答直接回答不触发，其他行业论文不触发；不写学术综述正文、不要研究空白章节。
---

# med-lit-review

本 skill 面向工业行业研究者交付系统完备的医学知识包：以研究问题（原始提示词经入口「研究问题确认」冻为一句系统化问题，见「HITL 协议」与 `references/protocols/hitl-protocol.md`）为输入，产出固定八节正文加七列证据表（见「输入输出」），中文正文保留英文关键术语（药物、疾病、结局指标原名）。

非目标：不写学术综述正文；不设**学科空白与未来方向**章节；证据空白与待补证据写进 §6.2（本次交付口径），交付局限仍写 §8。

## 输入输出

入：用户原始提示词（入口经「研究问题确认」冻为一句研究问题，见 HITL 协议）+ 中英关键词表（含同义词、商品名、别名；缺省由研究问题推导）+ 日期窗（全窗与近两年窗各跑一遍）。

出：八节知识包（执行摘要、研究问题、检索策略、主题式证据综合、结论、证据表、方法附录、局限声明）+ 七列证据表（文献、设计与等级、人群、干预与暴露、结局与效应量、偏倚备注、与结论的关联）+ 检索式与筛选日志（纳入排除记录）+ 落点报告（入库位置记录）+ 局限声明。§6 结论固定四块（6.1 背景／6.2 核心结论／6.3 讨论／6.4 支持结论），结论节整体排在证据表之前。

## 编排路由表

顺序执行，前一步输出即后一步输入。每行格式为输入→调用既有 skill→输出物→失败回退。

| 步骤 | 输入 | 调用既有技能 | 输出物 | 失败回退 |
|---|---|---|---|---|
| Step1 发现 | 研究问题 + 中英关键词（或推导稿）+ 双窗 + 来源计划 | jadense-scholar-search（学术检索；调用纪律：默认带 `--enrich` 回填缺失摘要，仍缺者按该 skill 富集步骤兜底）；`term_expand.py --log`（词表未命中词当场判四类——同义／上下位／易混／正字——出初判边并 `--append --edge` 落候选队列；工具回声同族成员提醒记边；判不准留候选，收尾计划表带表问一次）；**格级件装配**（`uv run python scripts/step1_diagnostics.py --delivery workspace/YYYY-MM-DD-<slug>`；把逐池原始响应 `queryPlan.queryStatuses[]`（camelCase）投影成 `run/b1/step1-diagnostics.json` 的 **ADR-0023 冻结名**（snake_case，16 键恒在场、缺者记 ∅）——逐池原样复制会让覆盖账三块与 §8 分句恒读不到数；字段映射与口径见 `references/protocols/step2-dedupe-and-snapshot.md` 的格级件行） | 候选清单 + 查询计划 + 来源诊断 + 检索式初稿 + 筛选日志初版（含候选队列条目） | 单一来源失败照收其余并记告警；零命中依次放宽发表类型筛选、扩同义词、核查日期窗，仍零如实记日志不虚构 |
| Step2 分类分桶 | 候选落盘文件 + 中文手动条目 + 证据类型预标签 + 必取判定件 | scansci-sort（分类分桶）；`scripts/step2_dedupe_sort_bucket.py`（三轮去重／五维排序（含中国证据维）；分桶与选取见下述选取步；被引快照复用与确定性段短路见 `references/protocols/step2-dedupe-and-snapshot.md`）；归一碰撞检出即 `--append`（含初判边）落候选队列；**排序首轮后、规模确认前**跑一轮引文扩展并池重跑本步（`uv run python scripts/citation_expand.py --delivery workspace/YYYY-MM-DD-<slug>`；直连 OpenAlex＋Europe PMC、零改 jadense、不经 MCP）；**同窗再跑迭代补漏**（`uv run python scripts/gap_fill.py --delivery workspace/YYYY-MM-DD-<slug>`；盘点基准＝排序终池，清单四类实体——关键试验缩写／药物（含研发代号与商品名）／疾病名／终点词——逐行记「为何预期该有」依据；缺口实体至多两波定向补检索（**每波 ≤4 query、≤2 波**由脚本写死）后并池重跑本步；未闭合实体逐条记原因、句式接 §8）；**同窗再跑高被引缺席核对**（`uv run python scripts/high_cited_check.py --delivery workspace/YYYY-MM-DD-<slug>`；被引降序 top-N 与终池对账、**只核对不入池**，缺席清单随规模确认出示）；**规模确认前跑选取步**（`uv run python scripts/step2_select.py plan --delivery <交付> --n <N> [--n2 <2N>] [--must-take <路径>]` 零写读数供题面；拍板后 `step2_select.py apply --delivery <交付> --n <N>` 定稿三件——`agent/download-queue.txt`（＝研读选取集＝入库集＝研读对象）、`run/b1/selection.json`（机器账）、`run/b1/bucketing-report.md`（四桶报告，行集＝选取集），另首写台账骨架 `agent/bucketing-and-sources.md`；口径＝必取集 ∪ 按 S 序前 k 条非必取行、`k = max(0, N − m)`，见 `references/protocols/step2-dedupe-and-snapshot.md`）；**机制读数齐、研读选取定稿后产覆盖账**（`uv run python scripts/coverage_ledger.py --delivery workspace/YYYY-MM-DD-<slug>`；写 `run/b1/coverage-ledger.json`（`run/` 忽略位），九块机制读数与指针沿 ADR-0023；人类面四处落点见下「覆盖账与缺口披露」节）| 四桶报告（开放直下、仓储副本、需机构、灰色候选，行集＝研读选取集）+ 研读选取集（＝下载队列＝入库集）+ 路由 | 渠道未命中标需机构或回旁路补漏；拦截只记诊断；灰色候选桶默认进 Step3 竞速（不再询问） |
| Step3 下载 | 规模确认后研读选取集（＝下载队列＝入库集，`agent/download-queue.txt`） | scansci-pdf（全文下载）；`scripts/step3_queue.py`（剪队列／搬运／重试）与 `scripts/reuse_pickup.py`（跨交付同 DOI 取件链），做法见 `references/protocols/step3-download-chain.md` | PDF 落盘 + 逐篇实际来源报告 + 成功计数 | 按返回提示分流；逐篇实际来源如实报告（含灰色与 Tor 路线）；含灰色在内仍拿不到的走交互点二（勾选后才动，不静默改设置）；失败记局限 |
| Step4 入库归置 | PDF + 元数据 + 监管文件 + 全库收藏集快照 + 入库摘要表 | use-zotero 归置分支（摘要链路：入库摘要表 `run/b1/ingest-abstracts.json` → 补录／补齐脚本 → 落点报告摘要列与摘要计数行；表形态见 `references/protocols/step2-dedupe-and-snapshot.md` §5） | 落点报告（含本次合集列＋附件列＋摘要列与摘要计数行） + 逐条回查（收藏集／父条目／附件／本次合集） | 摘要缺口非阻断（ADR-0021）：未填／不适用照记状态、不阻断写库，取数与清洗口径见 `use-zotero/scripts/abstracts.py`；按 `references/protocols/placement-guide.md` 执行（含本次合集双挂与附件挂载口径）；写库回查后默认继续挂载与补漏环；写库前走交互点三（计划表一次确认＋本次合集，P2／P3 不建集）；有人值守用户缺席留未分类记断点，AFK 全自动建挂（ADR-0013） |
| Step5 研读综合输出 | 正式条目 + 本地 PDF + 证据分级 + 交付口径（无全文条目先走三级取数并落可及性清单，见 R5a） | use-zotero 研读分支 | 八节知识包 + 七列表（每行有对应提取卡） + §5.1 优先精读序（先读层 3–5 篇带名次、其后层按主题分组；口径见证据表模板 §5.1） + 可及性清单（`run/step5-accessibility.json`） + 近两年说明 + 局限 | 未建卡文献禁入综合；可及性按 R5／R5a（仅摘要可进表与结论层，须标注＋偏倚未评＋§8 逐行交代；仅题录不进表、不建卡、按 DOI 进 §8）；未知保持未知；分级争议降档注理由。§5 与 §5.1 先走共享草稿脚本再人工并入包内真源（D5 草稿原则）：`step5_reference_list.py`（引文表草稿，吃 crossref 件）→ 人工核对并入 `agent/reference-list.md` → `step5_table.py`（七列表草稿，「文献」列取引文表同号条目）→ 人工核对并入 §5 → `step5_priority.py`（§5.1 名单与组序草稿，理由与维度人工填）→ 人工并入 §5 下子段；三脚本只写 `run/` 草稿件、不碰包内真源；形态由硬门禁 W-24 守 |
| B1 中文库手动补充 | 医学词表 + 商业库导出文件 | 用户在商业库执行检索，执行方解析后交 scansci-sort 汇入；题录词未命中或归一碰撞检出即 `--append`（含初判边）落候选队列 | 汇入条目 + 检索动作日志 | 失败原样退回；Step1 之后 Step2 之前汇入，同设计同权 |
| B2 监管文件直取 | 适应症与用法问题 | 官网直取，直交 use-zotero 归置分支 | 监管文件 + 版本文号记录 | 无官网文件记局限；限定规范性断言，不做疗效解读 |

分级口径见 `references/protocols/grading-and-synthesis-notes.md`，落点口径见 `references/protocols/placement-guide.md`，交互口径见 `references/protocols/hitl-protocol.md`，研读口径见 `references/templates/reading-card-template.md`，检索与引文口径见 `references/templates/search-strategy-template.md` 与 `references/templates/reference-list-template.md`。

注：术语生长循环是**横切规则，不占 Step 号**（ADR-0016）——它不产出下游步骤的输入，故不列本表；其录制责任内联在上述 Step1／Step2／B1 三格（Step2 格的录制责任含引文扩展轮、迭代补漏与高被引缺席核对），闭合在交付门禁（见「校验」章）。

注：人读主件渲染同样是**横切一步，不占 Step 号**——位次＝Step5 之后、交付门禁之前：`uv run python scripts/reader_html.py --delivery workspace/YYYY-MM-DD-<slug>`（`--check` 复算比对）。输入分档、确定性口径与失败分档（含程序级失败阻断交付）见 `references/protocols/reader-html.md`。

路线预设与入口选择（P0 完整／P1 查新摸底／P2 免写库／P3 已有文献写总结；预设打底＋Other 增删，依赖闭合检查，非法组合拦在入口）见 `references/protocols/hitl-protocol.md` 入口路线选择节；本表 Steps 顺序与失败回退不变。

## 默认策略

- 路线默认：默认走完整路线（Step1→Step5 全走，八节正文加七列证据表全交付，不跳步）；入口单选一经确认即锁定打底勾选，后续增删只做依赖闭合检查；非法组合拦在入口。路线矩阵与快线形态见 `references/protocols/hitl-protocol.md` 入口节。
- 研究问题默认：入口确认一句话研究问题（关键词与检索边界只读附注，可改写）；agent 侧记一行问题自检（可检索／可回答），不通过才追问一次；确认稿与自检行进检索策略，§2 落「研究问题：<一句话>」（ADR-0012）。
- 检索默认：PubMed（美国国立医学图书馆生物医学文献库）+ OpenAlex（开放学术图谱）双源，Crossref（数字出版元数据注册机构）核对身份；Google Scholar（谷歌学术，默认关闭，需密钥才开）、中文库、监管文件为条件分支。
- 下载默认：竞速策略（`fastest`）优先拿到原文，含灰色源（Sci-Hub 等）与 Tor 路线，默认启用；免费、仓储、出版商接口、机构授权路线为并列通道；逐篇实际来源如实报告。
- 入库默认：一次确认写库；批量先 --dry-run（试运行）看计划再写库；落点按 `references/protocols/placement-guide.md`；本次合集默认创建（推荐位，仍需一次确认；AFK 全自动；P2／P3 不建集；决策见 ADR-0013）；写库回查后默认继续附件挂载（本地已验 PDF 直挂＋补漏环；既有条目无正文附件默认补挂）。
- 研读默认：本地附件优先；单包即全部（不拆包），推荐规模二十至四十篇、正文体量自适应（不设固定字数）；下载前做规模确认（一次确认；AFK 按推荐档走），拍板篇数即研读选取集——下载队列、入库集与研读对象三处同一集合（ADR-0032）；**未选取留池备查，不下载、不入库**。研读目标名额按可及性三档合并计：无全文但摘要可得的条目照占名额（ADR-0023），仅题录同样计入——它也是本轮盘过的条目（仅题录不建条目、不入库）。
- 灰色默认（ADR-0006）：灰色源与 Tor/反爬路线默认启用，无人值守同样启用；版权与规则风险由用户确认承担（2026-09-17）；逐篇实际来源如实报告；机构登录与访问设置改动仍须确认。

## HITL 协议

HITL（人机回路，Human-in-the-Loop）入口两问、规模确认加两处断点：入口启动即问（先路线问、后研究问题问）、规模确认与两处断点到点再问、分开问，各最多一次 ask（互动提问工具）；题目、选项与 `recommended`（推荐选项标记）逐字取 `references/protocols/hitl-protocol.md`，agent 不代答。灰色桶默认启用、不再打断（原交互点一已废）。

| 编号 | 位置（必问门） | 推荐位 | 人不在回退 |
|---|---|---|---|
| 入口·路线 | 启动即问（有人值守每次必问；prompt 已写路线仍需一次确认） | 完整路线 | 不问，直接走完整路线（P0） |
| 入口·研究问题 | 紧随路线问（P0–P3 都问；prompt 已写研究问题仍需一次确认） | agent 拟稿 | 按拟稿继续，断点记筛选日志，缺口进局限声明 |
| 规模确认 | 选取步读数（`step2_select.py plan`）出来后、Step3 下载前，下载在组（P0／P2）每次必问（随附高被引缺席核对出示，同一次问；拍板篇数即研读选取集＝下载队列＝入库集） | 推荐档 | 按推荐档直走（`plan` 读数直接 `apply` 定稿） |
| 交互点二 | Step3 下载中，含灰色在内全通道仍没拿到才问（能拿到就不问） | 先不登录 | 没拿到的记待接机构后重试 |
| 交互点三 | Step4 写库前，每次都问（单篇不例外） | 表内每行首候选＋建本次合集双挂 | 有人值守留未分类；AFK 全自动建挂 |
| 生长循环·每家族一问 | 晋升 `--dry-run` 计划表出来时（每家族每任务最多一次；与上表各门预算独立） | 计划表逐条边（补边／改向／无边） | 只记候选，不晋升；下次有人任务再问 |

- 未确认不写库（有人值守；AFK 按交互点三回退全自动，ADR-0013）；未勾选的登录通道与访问设置一律不动，不静默改设置（灰色与 Tor 属默认策略，不算设置改动）。
- 停下即记断点：断点进运行日志（筛选日志或落点报告），缺口原文进局限声明（第八节），只写本次交付局限。
- 快线与跳步：设计跳过（预设未含）与执行缺口（在组未拿到）分开记；未写库＝写库设计跳过（非执行零写入）；P2／P3 包仍八节正文加七列证据表，缺席节保留标题＋一句跳步声明；P1 产 scoping-note（查新范围说明），不产八节包；记法与第八节句式见协议文件记录节与 `references/templates/` 两模板。
- 默认继续、显式跳过：写库回查后默认继续附件挂载与补漏环（本地已验 PDF 直挂、缺者补漏环再抓、既有条目无正文附件默认补挂；不另行确认）；任一段可跳过（只建条目不归置 / 只归置不挂载），记断点不记失败，跳过不弹二次确认。
- 自定位置照办；仅位置不存在或新歧义追问一次，再定不下来留未分类记断点；任何补问最多一次。
- 机密四样（密钥、登录票据、代理凭证、机构信息）不复述、不写进报告、记录与资产；报告只写渠道类名（免费 / 仓储 / 机构授权路线）。

## 研读完整性硬规则

本节为每次输出知识包内容前必读的行为禁令（behavior rules），不是产出物结构契约：本节管生成时真实；可追溯与可复现契约管产出物结构，校验节管事后机器检查。三者互补不重复。

- R1 不编造（No fabrication）：绝不生成不可查阅的条目、引文、数字——作者／标题／期刊／年／页码／DOI／样本量／效应量／p值／指南版本号／说明书版任一项不编。记不清就明说不确定，不填“大概对”的值；未读到原文就说未读到。
- R2 分清知道与不知道（Known vs unknown）：训练知识可用，但具体文献信息（标题／页码／出版年／效应量）须核实。不确定用占位——引文位置`[待补充出处]`，数字／细节`[待查证]`，拿不到全文的部分标`未见原文`；不编看似合理的引用。
- R3 文献红线（Literature red lines）：不由标题推内容；无原文不拆解——不凭摘要写论证过程／核心发现，不把摘要概括当全文；区分作者观点（author claim）与作者转引（re-cited），数据／事实标原始来源，转引不算原创；纯会议摘要（conference abstract）禁支撑结论，只作信号或排除并进局限。
- R4 生成后自查（Post-generation self-check，每次输出后跑一遍）：(1)文献真实存在（DOI／PMID（PubMed唯一标识，PubMed Unique Identifier）／文号可查）？(2)引文／断言是否都标了来源（含占位）且点到证据表行？(3)E级（证据等级，evidence level）与措辞强度是否匹配，有无把推测写成确定断言？(4)年份／人名／数字／版本号是否准确（现行指南版、说明书版、效应量原样）？任一项不过即改，不硬撑。
- R5 全文可及性（ADR-0023，取代 ADR-0018）：每篇文献按可及性取一值——`有全文`／`仅摘要`／`仅题录`（`有全文` 不区分载体，预印本与未编辑接受稿同值；`未见原文` 是内容占位词，不是状态值）。三档待遇固定，本表即唯一出处，模板与协议只引用不复述：
  | 可及性 | 建卡 | 进可追溯行 | 进结论层 | 标注 |
  |---|---|---|---|---|
  | 有全文 | 是 | 是 | 是 | 无额外限制；页码标 `PDF p.` |
  | 仅摘要 | 是（只填标识＋研究要素按摘要，不拆解） | 是 | 是（§6.2 数字与 §6.4 条目均计，不另设形式要求） | 状态词写进证据表「设计与等级」列；偏倚列写「未评（未见全文）」；§8 逐行交代 |
  | 仅题录 | 否 | 否 | 否 | 不进表、不进结论层；§8 按 DOI 交代 |
  | 会议摘要（类型侧另管） | 是 | 否 | 否 | 类型约束优先；只作信号或排除并进 §8 |
  仅摘要按设计给先验 E 级，不因不可及降档；降档理由仍限过时／异质／发表偏倚。建卡守恒：**建卡 ⇔ 至少读到摘要**（故仅题录不建卡）。
- R5a 仅摘要指派（义务，不是许可；ADR-0023）：研读集内**凡无全文的条目**，先走三级取数——库内条目 `abstractNote` → 入库摘要表 `run/b1/ingest-abstracts.json` → 联网（次序与默认联网、`--no-fetch` 语义沿用 ADR-0021）；**取到即判 `仅摘要`**、建卡、进可追溯行与结论层；三级取数**皆无**才判 `仅题录`。不适用摘要的文种（指南／监管文件／书章，ADR-0021 的 `na`）无全文时归 `仅题录`。全队列逐条读数落**可及性清单**（`run/step5-accessibility.json`，属 `run/` 忽略类）：DOI｜可及性三值｜摘要来源（`table`／`crossref`／`pubmed`／`openalex`／`库内`／`none`）｜取数时刻，按忽略类登记进 `agent/manifest.json` 再生条目，做法见 `references/protocols/workspace-guide.md`。误标的机器判据是验收门禁 `E-19`（硬门禁）：标仅题录者不得在可及性清单、落点报告摘要列或库内 `abstractNote` 上取到摘要。
- 每篇一卡：每篇**读到内容**的文献一张提取卡（extraction card，见 `references/templates/reading-card-template.md`），卡是主题综合的唯一输入，未建卡文献禁入综合与证据表可追溯列。卡内必填全文状态（full-text status）、可引用摘录（quotable excerpts：页码＋原创 original／转引 re-cited＋原始来源）、作者自认局限（author-admitted limitations）、一主关系（primary relation）、未见/未知（unseen/unknowns）；只有“有全文”才填人群／干预／结局细节，“仅摘要”只填标识＋研究要素（按摘要）＋未知标注；仅题录不建卡（建卡守恒见 R5），只进可及性清单、§8 与引文表；未核实文字禁入综合。卡规则细节以本节为准，模板只给执行填法。
- 先读层（§5.1「先读哪几篇」；生成口径与逐条细则的唯一原文见 `references/templates/evidence-table-template.md` §5.1）：Step5 内联，排在 §6 与证据表定稿之后；真源＝八节包 §5 下子段。域＝有卡 ∩ 有表行（仅题录、会议摘要、无表行的背景卡不入序）；先读层 3–5 篇带 `1..N` 名次、其余按 `§4` 主题分组不标名次；每篇一句理由（正文 ≤40 汉字，出处指针另计），必须点真实存在的文献并落在具名维度（`结论关联`／`权衡价值`／`主题补位`），禁纯评价词。依据＝固定优先级序列（§6 行指针计数 → 卡内主关系 → 主题去重 → 定级 E 级 → 被引数仅作平局），**禁**训练知识印象、期刊名望与影响因子、作者声望、Altmetric 类不可溯源项；版本内数据自足。仅摘要不降权（R5）。无卡路线不产该层、页面不留空壳。
- 主题综合只组卡（综合细节见 `references/templates/reading-card-template.md` §4）：同主题卡先列文献再写主题段，段内每句实质断言点到文献。分歧只写主题分歧（topic tension：A说X、B说Y，分歧点在人群／剂量／随访）；缺口只写本次交付局限（查了什么、缺什么、为何缺）；禁写学科空白与未来方向章节。

## 结论写法

§6 按四块固定顺序写（6.1 背景 → 6.2 核心结论 → 6.3 讨论 → 6.4 支持结论），结论节整体排在证据表之前。四块的逐块口径、空白边界、来源留痕与正反例见 `references/templates/conclusion-template.md`；机器检查 M-01／M-02／M-03、§1 执行摘要字数与类型约束（叙述综述／病例报告／监管文件）见 `references/protocols/review-rules.md`。

## 覆盖账与缺口披露（§3 摘要行、§8 缺口块）

发现层各机制跑完（引文扩展并池重跑 Step2 → 迭代补漏并池重跑 Step2 → 高被引缺席核对）后产覆盖账：`uv run python .claude/skills/med-lit-review/scripts/coverage_ledger.py --delivery workspace/YYYY-MM-DD-<slug>` → `run/b1/coverage-ledger.json`（`run/` 忽略位，ADR-0023）。账收九块机制读数与指针（引文扩展／迭代补漏／高被引缺席核对／经典段／源失败／命中上限／日期过滤／源声明／研读选取），**不复制**格级／池级／终池读数（唯一家分别在 `run/b1/step1-diagnostics.json`／`step1-pools.json`／`dedupe-report.json`）。每块键集＝ADR-0023 冻结名（引文扩展块另含 ADR-0024 的 `filtered`／`filtered_pointer`；引文扩展与迭代补漏两块另含配套票 03 的 `contributed_final_count`／`contributed_queue_count`（口径＝研读选取集条目数；冻结字段名沿用）／`contributed_queue_kinds`／`contributed_queue_origins`／`contribution_pointer`；`ran` 只在冻结它的三块在场）＋`pointer`（ADR 正文要求「每机制一块＋指向各自产物的指针」）＋**可选 `note`**（唯一的可选增量键：未跑／未取数原因、口径注记）——门禁按子集语义核（冻结名须在场）。账与格级件属 `run/` 忽略类，按 `references/protocols/workspace-guide.md` §10「发现层类」登记进 `agent/manifest.json` 的 `regeneration`。

- **口径纪律**：轮空记 `—`、实测零记 `0`；**未取数不得写成 0，也不得写成「未截断」**——`cap_hit: boolean|null`（`total_available` 同），`null` 不读作 `false`，断言语境按 `=== true` 计数。`pool_kind` 三值＝检索／引文扩展／追加轮，**缺省口径＝检索池**（Step1 池不写该字段时按检索计）；A-01 只数检索池，扩展／追加池分列读数、不断言。机制贡献按 `dedupe-report.final_records[].pool_ids`（**全部来源池**）读、**不得**用 `origin`（最先入库池）代读；贡献读数见覆盖账引文扩展／迭代补漏块的 `contributed_*`（口径见 `references/protocols/step2-dedupe-and-snapshot.md` §6）。
- **读者面文案（写作规则，ADR-0028）**：读者可见的一切文字，只写临床读者不查本仓就能读懂的中文——**内部词表一律不进读者面**（`CONTEXT.md` 词条名、发现与交付流程用语、件名、脚本名、口径码、内部编号）；正文按 `references/protocols/reader-html.md` §3 的读者面词表写：证据表行记「文献 N」（契约记号 `行 N` 只进 markdown 真源与 §5.1 表）、可及性三值记「有全文／仅摘要／仅题录」、卡记「文献卡」、先读名次记「先读 N」、缺失读数记「未取到」（机器面记 `∅`）；内部件路径／脚本名／可粘贴命令与身份码只进页尾「校验信息」区（正文里换成指向该区的链接）；`## 小节` 与无名段按卡面形态渲染。机器记号（锚点、占位符、未渲染 markdown）的判据见 §3，硬门禁 `W-21` 按此判定。
- **摄入过滤口径**（ADR-0024；引文扩展块 `filtered`／`filtered_pointer` 两冻结名）：引文扩展记录入池前按被引下限裁决（通过＝被引 ≥100 或 发表年份 ≥ 参考年−3 含当年；被引读数缺失一律不入池，年份缺失按「新」豁免），只作用于引文扩展池；上限与截断口径不变（上限＝取回条数、不补取）。**被滤条目既不得写成「缺席」（源侧没有）、也不得写成「截断」（源侧可及但未取）**——它是已取回但按口径不入池，另计 `filtered` 并逐条留痕在 `run/b1/citation-expansion.json` 的 `filtered[]`；关闭过滤时读数记「未过滤」（**不得记 0**）。
- **扩展记录摘要重建口径**（ADR-0024 配套票 02；读数在 `run/b1/citation-expansion.json` 的 `abstract_rebuild`）：引文扩展的 **OpenAlex 题录**从**同批响应**的 `abstract_inverted_index` 按 position 还原正文（`SELECT_WORK` 多取一字段，**零额外请求**）；**EPMC 的 `abstractText` 与 S2 的 `abstract` 由通道原样保留、不走重建**（通道未给正文时仍是原位空串，也无逐条成因键）。OpenAlex 侧**不可还原者记 ∅**（`null`——**不写空串冒充**），成因按码落在记录 `abstractReason`（`{class, message}`，读数件 `missing_reasons` 按 `class` 计数）；两类无正文在读数件里一并计入「无摘要」，与「可重建」「通道原样」分列。未知维语义不变（`comp`／`s_rel` 对空正文的判定照旧；本机制不引入任何阈值）。账的引文扩展块只在 `note` 内指向此读数（不抄数字、不增冻结名，§8 分句数字不动）。
- **§3 检索策略摘要行**（机制执行与新增量，一行）：`机制执行与新增量：引文扩展轮 <已跑（种子 N｜新增 去重前 x／去重后 y｜摄入过滤 被滤 k 条／未过滤）｜未跑（原因）>｜迭代补漏 <已跑（波 N｜新增 去重前 x／去重后 y｜未闭合 M）｜未跑（原因）>｜高被引缺席核对 <已跑（缺席 N：相关 a／超题 b／窗外 c｜处置 补入／不补）｜未跑（原因）>｜逐格读数与指针见 run/b1/coverage-ledger.json`——两机制的「入池新增」不在账内（账只收 ADR-0023 冻结的 `new_before_dedupe`／`new_after_dedupe`），需要时另读 `run/b1/citation-expansion.json`／`run/b1/gap-fill.json` 的 `new_records.new_to_pools`。源集合声明与未纳入源见检索策略模板同名节（机器真源 `run/b1/source-declaration.json`）。
- **§8 覆盖缺口块**（八节包第八节局限声明内，逐机制一行、三段式 `机制｜读数｜缺口与补法`）：读数数字与覆盖账一致（账产出命令末尾按账打印该块，照抄即可，数字以账为准）；只写本次交付局限，不写学科空白；轮空记 `—`、实测零记 `0`。十类机制实例句：
  1. 源失败补偿：`- 源失败补偿｜失败 N 格／重试取回 N 格｜逐格：<池>／<源>（<错误类>，N 次，已恢复／未恢复，净影响 <N 条｜未知>）。补法：同源重跑该格（只对瞬时错误类，不整池重跑）。`
  2. 命中上限披露：`- 命中上限披露｜命中截断 N 格（精确集源 N／召回形源 N）｜cap_hit 判定不可得 N 格｜判定不可得＝未取数，不得读作未截断；精确集源已抬 limit 或按年切片跑全，召回形源总数只记、不作漏检量。`
  3. 无日期候选：`- 无日期候选｜N 条（Σ 逐格 date_unknown_count）｜带窗池不丢弃、以 dateUnknown 标记入池交筛选裁定，带窗池的窗口断言在这 N 条上不成立；补法：无——属覆盖不确定的如实披露。`
  4. 越窗候选：`- 越窗候选｜N 条（Σ 逐格 date_rejected_count）｜越窗是正确过滤（合法日期落窗外），区别于无日期候选；补法：无——按窗剔除并如实计数。`
  5. 引文扩展轮：`- 引文扩展轮｜种子 N（头部 a／判定集 b）｜方向 后向＋前向｜通道请求 N／命中 N｜新增 去重前 N／去重后 N｜摄入过滤 <被滤 k 条／未过滤>｜判停 <种子集跑完／预算耗尽／中止>｜贡献 最终名单 A 条／研读选取集 B 条（按贡献池类 <类集合> N／…；按最先入库池 <origin 标记> N／…）｜失败 N 通道／截断 N 通道／无标识丢弃 N 条／零新增；被滤 k 条按摄入过滤口径另计（既非源侧缺席、也非上限截断，逐条见 run/b1/citation-expansion.json 的 filtered[]）；贡献读数为机制归因口径（条目被本机制任一池吸收过，与 `origin`＝最先入库池并置，两者都不是缺口），读法见 `references/protocols/step2-dedupe-and-snapshot.md` §6；补法：截断与失败通道逐格复核，零新增如实记 0。`
  6. 迭代补漏：`- 迭代补漏｜实体清单 N／疑似缺口 M｜波 N／query N｜新增 去重前 N／去重后 N｜闭合／未闭合｜贡献 最终名单 A 条／研读选取集 B 条（按贡献池类 <类集合> N／…；按最先入库池 <origin 标记> N／…）｜未闭合逐条：<实体>（<类>）按「<检索式>」全窗定向检索（波 N）后，<原因>；相关结论待补检索或待新发表；贡献读数与 `origin`（最先入库池）口径并置，读法见 references/protocols/step2-dedupe-and-snapshot.md §6。`
  7. 高被引缺席核对：`- 高被引缺席核对｜查询 N 组｜缺席 N（相关 a／超题 b／窗外 c／未判 d）｜处置 <补入／不补／未答>｜通道只核对不入池，补入与否由用户拍板（补入走盘点表→追加轮）。`
  8. 经典段：`- 经典段｜清单 N 条｜核出 N／未达 N｜未达逐条：<清单项>｜<查询>（集合 N，取 M 条）内词面与 DOI 均无命中；补法：改用 DOI 或更具体词面重查，机制不承诺全量召回。`
  9. 未启用源：`- 未启用源｜在组 <源…>｜未纳入 <源>（<原因>）｜Google Scholar <未启用（gs_enabled=false）／已启用／未声明>｜无稳定身份剔除 N 条（不入持久化）｜零覆盖面以声明代补源，未启用即在此披露。`
  10. 未选取：`- 未选取｜未选取 N 条（终池 X − 研读选取 Y）｜按 S 位次分档 {…}、按来源池 {…}；未逐条验证纳入与全文。补法：需要时另开一轮或以人工单篇归置。`
- **已知必中抽查（ADR-0023 抽查双轨；软项 A-12／A-13，**不进**固定选题验收跑的选题集）**：
  - **轨 1 固定 fixture 场景集**：场景件在 `evals/coverage/<场景 id>.json`（字段契约见 `evals/acceptance/topic.md`：`topic`／`kind`／`expect_shape`／`applies_to`／`known_hits`／`baseline`；`applies_to` 取交付 slug 辨识词，不得圈进固定选题包）。核出器：`uv run python .claude/skills/med-lit-review/scripts/coverage_check.py --delivery workspace/YYYY-MM-DD-<slug>` → `run/b1/coverage-check.json`（门禁读件，`run/` 忽略位）＋ `run/b1/raw/coverage-<场景 id>.json`（逐探针取数读数）。**机制变更时与验收时跑**；场景适用该交付而未记录读数、或已知必中条目未核出，即软项缺口、须进 §8；不适用记「不适用」，不阻断；池件（`run/b1/dedupe-report.json`／历史 `run/merged-pool.json`）不可得时`核出` 整条记「核出未达」并须进 §8——`核出` 要「通道检出＋不在终池」两半，缺池半边不可核对，**不按未确认写成 `found`**。两形语义不同：`核出`＝核对通道检出并列入缺席清单／规模确认提示（**不是入池断言**，在池与研读选取集位次只是读数），`取回`＝机制集取回该条目。**漂移容忍**（ADR-0023 接口注意）：provider 排序与收录漂移只记 `drift[]` 行并披露，不判假失败；取数失败逐条记「取数失败」，不写成未核出。
  - **轨 2 每交付 SR／指南纳入清单对账**：取到**带纳入清单的** SR／指南全文时按筛选日志「纳入清单对账」节抽一次对账（条目｜能否经本交付机制取回｜缺口记因），机械部分跑 `coverage_check.py --delivery <交付> --list <纳入清单.json>`（只读交付池、不联网）；未取到或该规范性文件无纳入清单（纯推荐型指南共识）记「不适用」，不阻断。对账身份柄（SR 主引用／研究主报告）须声明，两类柄分列读数。
- 门禁侧（在场与字段齐、缺口⇒§8 分句且数字一致）见 `evals/acceptance/topic.md` 与 `evals/acceptance/run_gate.py` 断言表——本账是那些断言的对象，账本身不判门禁。

## 可追溯与可复现契约

可追溯链（硬门槛）：数字对象标识符 DOI（Digital Object Identifier）与来源外部身份（Step1 原样保留）→去重键（Step2，大小写无关；无标识学术搜索记录禁入持久化）→实际来源报告（Step3，以返回为准如实报告）→在库判定只认 DOI 字段（Step4，附件文本命中不算收录）→证据表行链接与正文断言点到行（Step5）。每条实质断言可点到证据表行，否则记局限。

- 可复现包：检索方案 + 检索式（含来源、日期窗、发表类型筛选）+ 来源诊断 + 筛选日志（含每轮纳入排除计数与未知日期注明）+ 分桶队列文件 + 落点报告 + 版本日期戳（指南版本号、说明书版、文号、被引查询日期）。中文手动补充与监管直取动作同样记日志。
- 落盘与命名：每次交付落仓库根 `workspace/YYYY-MM-DD-<slug>/`（重跑建新目录不覆盖：同日同 slug 追 `-r2` 起后缀，不同问题加长 slug）；交付根四项（`INDEX.md`／人读主件 `<slug>.html`／`agent/`／`run/`）与逐件进版落点见 `references/protocols/workspace-guide.md` §5–§6；合约见 `docs/adr/0010-workspace-persistence-contract.md`，交付产出分层见 `docs/adr/0025-delivery-output-layering.md`。
- 去重与优先级：去重三轮先分后合、键三层、判定与优先级公式见 `references/templates/screening-log-template.md`；引文统一 GB/T 7714-2015 单制式与逐条核对见 `references/templates/reference-list-template.md`。
- 术语词表：真源 `references/lexicon/term-lexicon.yaml`（候选队列 `references/lexicon/term-lexicon.candidates.yaml`；规则与协议唯一原文 `references/protocols/term-lexicon-protocol.md`），版本随 skill 冻结、每任务在筛选日志与检索策略里引用版本号；生长循环闭合在交付门禁（见「校验」）。

## 校验

包写完后必跑事后确定性检查与交付门禁（三件套，不可豁免，ADR-0016；verdict 头记三行读数，见 `references/protocols/workspace-guide.md` §11）：

- 事后确定性检查：`uv run python scripts/review_knowledge.py agent/knowledge-package.md --cur-year YYYY`——15 条规则子集加 error／warning／info 三档，五字段报告；旗标全集（`--format`／`--rules`／`--lexicon`／缓存位）、退出码与门映射及格线见 `references/protocols/review-rules.md`。
- 生长循环落缓冲检查：`uv run python scripts/check_growth_loop.py <交付目录>` 读 `agent/screening-log.md`（读法：交付目录下的 `agent/` 直下件，非根层同名件），比对筛选日志「术语词表引用与候选」表的查询词／候选词与真源 `references/lexicon/term-lexicon.yaml`、候选队列 `references/lexicon/term-lexicon.candidates.yaml`；**声明为未命中／候选、但既不在真源也不在队列、已落术语无边且未声明 `no_edge`（机械生长，逐条口径），或结构不可解析即红**（退出码 1），缺口即阻断交付；判据与规则见 `references/protocols/term-lexicon-protocol.md`。
- 交付形态契约：`uv run python scripts/check_delivery.py --delivery <交付目录>`——零网络零锚点，退出码同族同形（0 过／1 硬门禁未过／2 用法或输入缺失）；硬门禁判人读主件（在场与命名／自足／锚点完整／DOI 可定位／卡全覆盖／精读序三条／图契约／渲染对账／读者面文案零机器记号）与索引骨架／摘要骨架／落点一致／**入库集守恒（落点计划条目集 ≡ 有卡集；W-22）**／**证据表文献列可拆档且有题名（W-24；「文献」列须为 GB/T 7714 单条，自拼「作者，年，期刊」类简式会让题名在页面上整段消失）**／卡面解析健康（0 卡与目录缺显式报错），软项（结构／封顶／禁词面／精读序形态）只记读数并告警不阻断；逐条门与档位映射见 `references/protocols/review-rules.md` §6，形态口径见 `references/protocols/workspace-guide.md` §9–§11 与 `references/protocols/reader-html.md` §7。
- 形态检查自证：`uv run python scripts/check_delivery.py --selftest`——每条硬断言一孪生场景（改过本脚本后必跑）。
- 漂移抽跑：`uv run python scripts/check_delivery.py --card-probe [<真实包>]`——用门禁自己的解析器跑最新**同形态**真实包，只判解析器健康（条数＝`agent/cards/*.md` 文件数，0 卡即红）；无可抽样本记「样本缺位」不报错不阻断；每次交付期跑一次，读数行照录进门禁摘要的「交付门禁读数」表。
- 生长环机制自检：`uv run python scripts/check_growth_loop.py --selftest`（沙箱：边入口／晋升出账／扩展／无边与主名全链；改过 `term_expand.py` 后必跑）。
- 端到端验收跑：`evals/acceptance/topic.md`（固定选题与冻结钉子）＋ `evals/acceptance/anchors.json`（锚点清单）＋ `evals/acceptance/run_gate.py`（断言执行器）；命令、断言口径与硬门禁六项见 `evals/acceptance/topic.md`。
- 摘要声明↔库内一致（D-11，档位＝硬门禁）：落点报告「摘要」列标「已填」的条目——含来源记 `库内` 者（条目原有摘要、本轮未取数；来源词表见 `references/templates/placement-report-template.md`）——库内 `abstractNote` 必须非空（复用本地库只读回查，`库内` 与其它来源同一读数核验）；live 取不到即判不过（fail-closed），`--no-live` 记未执行；无「摘要」列者按 D-10 同式三级读法判豁免——先读落点计划／落点报告的口径注记（写库设计跳过且确不含计划表与摘要列者按写库设计跳过记，自报不实——该块仍带计划表行——即判不过；现行包须记 `摘要口径：present`，缺此注记者按历史包读），再看该包清单的口径（新形态 `agent/manifest.json`、历史包在交付根），最后才按有无「摘要」列推断：写照却漏列「摘要」列者不按历史包豁免（ADR-0008）；带「摘要」列的表全部计入（多表逐条汇总核，同一键两表声明不一致即判不过），摘要计数行条条须与全表逐条解析一致。缺口非阻断（ADR-0021）：未填／不适用不进失败，该记的照记。
- 仅题录须可证无摘要（E-19，档位＝硬门禁，ADR-0023）：凡标 `仅题录` 的条目——含可及性清单 `run/step5-accessibility.json` 与证据表「设计与等级」列两处形态——都不得在**可及性清单的 `abstract_source`、落点报告「摘要」列或库内 `abstractNote`** 上取到摘要；任一取到即判不过并逐条列出。**核心交叉核对离线可算**（清单与摘要在包内，`--no-live` 只跳过库内加强项、不降级为未执行）；库内回查取不到照旧判不过。可及性清单在场时另核它覆盖证据表全部行号（逐条记研读集条目）。
- 行为用例见 `evals/evals.json`（以可追溯抽查为硬门槛）；校验脚本自身的脏样例与清洁样例回归见 `evals/review-cases/`。

## references 索引

- `references/templates/` —— 产出物模板（写作与落盘前读）：`conclusion-template.md`（§6 四块）· `evidence-table-template.md`（七列证据表）· `reading-card-template.md`（Step5 提取卡与主题综合）· `screening-log-template.md`（Step2 日志与去重口径）· `search-strategy-template.md`（Step1 检索策略与外供输入）· `reference-list-template.md`（引文制式与逐条核对）· `placement-report-template.md`（Step4 落点报告）· `scoping-note-template.md`（P1 查新范围说明）
- `references/protocols/` —— 口径与协议（决策点读）：`hitl-protocol.md`（入口两问、规模确认与两处断点的唯一原文）· `review-rules.md`（校验规则、门映射与及格线）· `placement-guide.md`（医学落点表与附件挂载口径）· `workspace-guide.md`（落盘、进版、索引与再生）· `reader-html.md`（人读主件渲染契约：CLI、输入分档、确定性、登记、调用位次与失败分档）· `step2-dedupe-and-snapshot.md`（Step2 去重／排序／分桶与两处缓存）· `step3-download-chain.md`（Step3 队列层、跨交付取件链与调用侧纪律）· `grading-and-synthesis-notes.md`（分级升降与主题综合口径）· `term-lexicon-protocol.md`（词表四分、消费与生长循环）
- `references/lexicon/` —— 术语词表数据件：`term-lexicon.yaml`（机器唯一真源）· `term-lexicon.candidates.yaml`（候选队列，文件即队列：在队＝待晋升，晋升即出账）
