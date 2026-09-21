# ADR-0017 skill 文档分层：常驻集判据与 references 按职能分桶

- 日期：2026-09-17；状态：已接受；来源：grill-with-docs 会话（三轮 15 问 live exchange ＋ 收束三问）——用户拍板分桶方案、词表落点、常驻集边界、波及范围、迁移与验证口径。
- 上下文：`med-lit-review` 的 `SKILL.md` 达 24,862 B（触发即无条件载入），`references/` 16 件平铺 117,373 B（按需载入）。实测常驻面构成：校验 24.7%｜编排路由表 15.1%｜HITL 11.8%｜研读完整性 R1–R4 11.5%｜可追溯契约 10.9%｜结论写法 9.9%｜默认策略 7.7%。其中三章主体是**跨文档复述**——结论写法复述 `conclusion-template.md` 与 `review-rules.md`；校验章复述 `review-rules.md` 的脚本参数与退出码、`term-lexicon.md` §1/§3/§7；术语词表两节复述 `term-lexicon.md`——与 ADR-0009「跨文档复述处只留一句话指针」相冲，属长期未清的存量，非本次新立判据的反面。平铺另有可发现性缺陷：5 件在 `SKILL.md` 无指针，其中 `evidence-table-template.md` 与 `scoping-note-template.md` 全仓零入站指针；后者是 P1 快线的主要交付件模板，P1 跑起来只能自编格式。指针形态上，裸兄弟文件名属**静默失效**类（`skill://` 协议无 fallback search，指针写错即硬 `File not found`），`docs/adr/0011` 第 7 行已有一条同类死指针（`references/question-frameworks.md`，文件已随 ADR-0012 删除）。
- 决定：
  - **常驻集判据**：无条件在场＝行为禁令（研读完整性 R1–R4）、横切决策默认（默认策略——七条默认分属七个不同的家，拆出去要读五件才知道默认怎么走，正是 ADR-0009 要避免的回跳）、交付契约、路由主干、HITL 门位表、输入输出契约、references 索引。按需件＝操作手册、模板与机器数据。
  - **分区只锁真源**：跨文档复述处只留一句话指针；**同一指令文件内的点用重复仍按设计接受**（ADR-0009 末次修订立场不变，本次不触碰）。
  - **按职能分桶**：`references/{templates,protocols,lexicon}/`——`templates/`＝产出物模板（写作与落盘前读，8 件）｜`protocols/`＝口径与协议（决策点读，5 件）｜`lexicon/`＝术语词表三件套（机器真源／人读镜像／隔离缓冲，3 件）。**机器数据与文档同桶分层，不另立顶层 `data/`**：三件套互相指涉密集，拆开违背「两者不一致以 YAML 为准」的配对惯例。
  - **指针形态**：一律 skill 根相对 `references/<桶>/<件>`，**禁用裸兄弟文件名**。**basename 一律不改**——`run_gate.py` 的 F-07（第 1600 行）与 D-05（第 1072 行）靠 basename 子串匹配，改 basename 即误红。
  - **索引形态**：`SKILL.md` 按桶分组列全 16 件。`templates/` 桶无高频可筛——8 件各是某一步骤的产出契约，故「桶＋高频件」在该桶退化为全列，不省反失。
  - **桶级不变式挂门禁**：`references/` 根下无零散件、三桶俱在、`SKILL.md` 点到的桶名真实存在 → `run_gate.py` 新增 `K-01`（硬门禁）。该条只守「件必须落进桶」，**不要求每件都有入站指针**（与「桶＋索引」形态一致；后者会与索引设计相冲）。
  - **提取内容的落点：零新增件**。薄化的主体是**删复述**而非搬家，故不新建件：门映射与及格线并入 `review-rules.md` §6（唯一真源，原住 `SKILL.md`）；去重键三层、判定与优先级公式补入 `screening-log-template.md` 既有两节的注释（原已承载大半，本次补齐权重初值与合并取字段两条）；落盘命名归 `workspace-guide.md`、引文制式归 `reference-list-template.md`、术语消费与生长循环归 `term-lexicon.md`（均已有家，只留指针）。
  - **迁移安全**：逐件核过 6 处门禁内容耦合与 3 处脚本路径耦合，判定本次操作对它们是**路径耦合而非内容耦合**（`A-08/A-09` 只读 `version:` 行、`D-05` 只查两个中文短语、`E-13` 只查五个规则 id、卡片解析器是格式耦合），故只改路径常量。三个脚本的模块常量与 CLI 默认、`run_gate` 六处 `atext()` ＋两处子进程路径 ＋`cmd_hint` 记录串、`demo_topic_fixture.py` 的数据串同步改写。
  - **冻结区不动**：`.scratch/**`、`workspace/**`、`tmp/**`、`.worktrees/**` 内的指针一律不改（历史交付冻结）。跨仓只改**因本次移动而失效的调用方**：根 `AGENTS.md` 第 65 行与 `use-zotero/references/placement.md` 第 3 行；其余为名指（`term-lexicon`、`placement-guide` 等无路径），仍准确，不动。
- 备选（未采纳）：**保持平铺＋补索引件**（SKILL.md 仍要背更长指针清单，与薄主技能反向）｜**按 Step 分目录**（`hitl-protocol`／`workspace-guide`／`review-rules` 天然横切，无单一步骤归属，桶失去判别力）｜**词表三件套留 `references/` 根**（根仍混杂非文档件，与 references 作为文档目录的定位不符）｜**词表移出到 skill 根 `lexicon/`**（把 `.md` 人读镜像与 `.yaml` 真源配对惯例拆到两处）｜**R1–R4 与默认策略一并外移**（R1–R4 是语义纪律，事后脚本门抓的是六条形态错误、抓不到静默降质；默认策略是横切默认，外移即兑现回跳代价）｜**新建 `verification.md` 收纳校验章**（references 16→17，反打「件太多」的原始痛点；且 `review-rules.md` 早已承载脚本调用与退出码）｜**顺带统一全仓 5 个 skill 的 references 约定**（其余 skill 的 references 仅 2–8 件，嵌套收益近零而成本纯增）｜**关闭 `check_growth_loop.py` 的「缓冲不可达返回空集」**（**已实测证伪**：空集使需落缓冲的行判 `RED` 而非绿，该路径 fail-closed；`review_knowledge.py` 的无表回退是 `review-rules.md` 明文的设计，text 模式首行打「词表：内置对」可观测——两处均不动，此处立此存照以免后人「修」一个不存在的洞）｜**加「每件必有入站指针」门**（与「桶＋索引」形态相冲，等价于回退到平铺列全）｜**只改本 skill 而把约定写进 `AGENTS.md` 供后续沿用**（单实例外推，样本 = 1）。
- 后果：`SKILL.md` 24,862 B → 17,876 B（−28%），常驻面砍掉跨文档复述三章（校验 6.1→1.2 KB、结论写法 2.5→0.4 KB、可追溯契约 2.7→1.8 KB），保留 R1–R4 与默认策略两个受保护的常驻块；`references/` 仍 16 件但归三桶，5 件缺指针补齐、2 件真孤儿消解（P1 不再自编 scoping-note 格式）；新增 `K-01` 桶级硬门禁。**未达会话中「−44%」的估算**：该估算假设 `研读完整性硬规则`（2.9 KB）与 `默认策略`（1.9 KB）也外移、且 `编排路由表`（3.8 KB）压缩到 3.2 KB，而前者经拍板保留、后者为保证 SOP 可读未压——三者合计 8.6 KB 是常驻面的实际地板，现实下界约 17.5 KB。已知未修：`evals/acceptance/anchors.json` 钉 `lexicon v1.2` 而真源已 v3.0（冻结验收锚点，改动的风险大于收益，本次记而未修）。
- 修订（2026-09-17，同会话收口）：新增第 6 节「门映射与及格线」到 `references/protocols/review-rules.md`（原 `SKILL.md` 校验章），该文件原 `## 6`／`## 7` 顺延为 `## 7`／`## 8`，无入站节号指针受影响。
- 修订（2026-09-17，缓存优化 handoff 02 号票）：`references/protocols/` 增第 6 件 `step3-download-chain.md`（Step3 队列层与调用侧纪律），references 计 17 件（templates 8／protocols 6／lexicon 3）。分桶判据、指针形态与「不新建顶层桶」三条不变；`K-01` 桶级门禁复跑通过（三桶齐、根下无零散件、索引可达）。本修订只记件数与新件落位，不改本次分桶决策。
- 修订（2026-09-17，缓存优化 handoff 05 号票）：`references/protocols/` 增第 7 件 `step2-dedupe-and-snapshot.md`（Step2 去重／排序／分桶与两处缓存），references 计 18 件（templates 8／protocols 7／lexicon 3）。分桶判据、指针形态与「不新建顶层桶」三条不变；`K-01` 桶级门禁复跑通过（三桶齐、根下无零散件、索引可达）。本修订只记件数与新件落位，不改本次分桶决策。

## 附：references 路径变更表（16 条）

| 旧路径 | 新路径 |
|---|---|
| `references/conclusion-template.md` | `references/templates/conclusion-template.md` |
| `references/evidence-table-template.md` | `references/templates/evidence-table-template.md` |
| `references/placement-report-template.md` | `references/templates/placement-report-template.md` |
| `references/reading-card-template.md` | `references/templates/reading-card-template.md` |
| `references/reference-list-template.md` | `references/templates/reference-list-template.md` |
| `references/scoping-note-template.md` | `references/templates/scoping-note-template.md` |
| `references/screening-log-template.md` | `references/templates/screening-log-template.md` |
| `references/search-strategy-template.md` | `references/templates/search-strategy-template.md` |
| `references/grading-and-synthesis-notes.md` | `references/protocols/grading-and-synthesis-notes.md` |
| `references/hitl-protocol.md` | `references/protocols/hitl-protocol.md` |
| `references/placement-guide.md` | `references/protocols/placement-guide.md` |
| `references/review-rules.md` | `references/protocols/review-rules.md` |
| `references/workspace-guide.md` | `references/protocols/workspace-guide.md` |
| `references/term-lexicon.yaml` | `references/lexicon/term-lexicon.yaml` |
| `references/term-lexicon.md` | `references/lexicon/term-lexicon.md` |
| `references/term-lexicon.candidates.yaml` | `references/lexicon/term-lexicon.candidates.yaml` |

- 修订（2026-09-18，三件套分工，ADR-0022）：词表件三分改写——协议部分出 lexicon 桶进 protocols 桶（`references/lexicon/term-lexicon.md` → `references/protocols/term-lexicon-protocol.md`），人读镜像与变更记退场，`references/lexicon/` 只留两件数据（真源＋候选队列）。K-01 三桶不变式不变；本节所定「分区只锁真源、跨文档复述处只留一句话指针」按新形态执行（`review-rules.md` 的 T-01 复述改指针）。

同步改写面：`scripts/review_knowledge.py`（常量＋CLI 默认＋文档串 6 处）｜`scripts/term_expand.py`（常量 2 ＋`--md` 默认＋文档串 7 处）｜`scripts/check_growth_loop.py`（常量＋注释 2 处）｜`scripts/demo_topic_fixture.py`（数据串 1 处）｜`evals/acceptance/run_gate.py`（`atext()` 6 ＋子进程路径 2 ＋`cmd_hint` 1 ＋`@spec` 标签 4 ＋注释 1）｜`evals/evals.json`（结构化路径 8 ＋散文 3）｜`evals/review-cases/*.md`（夹具引注 7）｜`evals/acceptance/topic.md`（1）｜`SKILL.md`（索引与指针对齐）｜`references/` 内部互指 21 处｜根 `AGENTS.md`（1）｜`use-zotero/references/placement.md`（1）。
