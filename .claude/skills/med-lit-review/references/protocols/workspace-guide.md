# workspace 规范（workspace-guide）

本文件是 workspace 持久化细节的唯一家：命名与版本、粒度、边界、索引、再生清单、门禁摘要、派生件登记、跨交付复用与清理口径都写在这里；根 `AGENTS.md` 只留薄指针，合约见 `docs/adr/0010-workspace-persistence-contract.md`，交付产出分层见 `docs/adr/0025-delivery-output-layering.md`。作用域：仓库根 `workspace/` 的持久化，加 §14.2 时效与 §15 清理所列缓存位的归属与人工动作口径（含仓外：翻译输出、引擎缓存、上游仓外缓存）。历史包（`.scratch/med-lit-review-skill/`）与上游发布件不受本文件影响（历史交付冻结，只加文件头口径注记）。

**交付形态＝交付根固定四项**（§6／§8 `R6`）：`INDEX.md`（机读纯索引，§9）／`<slug>.html`（人读主件，由版内 markdown 真源确定性渲染；渲染契约见 `reader-html.md`）／`agent/`（真源与账目件，直下扁平、`cards/` 为唯一子目录）／`run/`（忽略缓存，位次与忽略语义不变）。人读走 HTML，机器读 `agent/`。

## 1 根与目录名

- 根：仓库根 `workspace/`（冻结）。每个 topic 的每次交付各占一个目录，平行 topic 并存不碰撞。
- 目录名 `YYYY-MM-DD-<slug>`：日期为交付日期（该次交付当日，四位年两位月两位日）；`slug` 为小写英文 kebab（`[a-z0-9]+(-[a-z0-9]+)*`，药物-疾病/人群缩写，如 `pcsk9-ascvd`）；日期与主题两要素必含；禁中文、下划线、空格。
- 可读性：交付日期加主题即身份，`ls workspace/` 即全局视图（不建跨 topic 全局索引）。

## 2 版本形态：topic 级新目录

- 每次重跑建新 topic 目录，日期取该次交付日期；旧交付一律不改（重跑只增不改，历史交付永远可寻址）。
- 版本不进 `run/`：`run/` 是忽略缓存，其中不放版本件，也不承载版本身份。
- 重跑后缀只进目录名；`agent/manifest.json` 的 `topic.slug` 记主题 slug、`topic.dir` 记完整目录名。

## 3 同名与同问题的消歧

| 情形 | 做法 |
|---|---|
| 同日同 slug 重跑（同一研究问题） | 自第二次交付起追增量后缀 `-rN`：当日第二次交付 `-r2`、第三次 `-r3`，递增（不存在 `-r1`） |
| 跨日重跑（同一研究问题） | 只换日期，不追后缀 |
| 不同研究问题撞主题 | 加长 slug（加人群/干预限定词，如 `pcsk9-ascvd` → `pcsk9-ascvd-statin-intolerant`），禁用后缀 |
| 任何情形 | 禁语义版本后缀（`-v1`/`-v2` 一类）：重跑不是 API 兼容承诺，后缀不是版本链 |

## 4 run 缓存内命名

- 一律英文 ASCII、小写（词形 kebab；DOI-slug 以 `_` 连接 DOI 与 slug，如 `10.0000_demo-101.pdf`）。
- 禁中文、大写与空格（历史包既有中文名不追改）。

## 5 商业库导出原文

- B1 导出原文（`b1-<库>-export-<YYYYMMDD>.txt`）放 `agent/` 直下，不进 `run/`；属进版小文本。

## 6 边界：什么进版、什么忽略

| 类 | 内容 |
|---|---|
| 进版（交付根） | `INDEX.md`（机读纯索引，§9）；`<slug>.html`（人读主件，由版内 markdown 真源确定性渲染，`<slug>`＝`manifest.topic.slug`；渲染契约见 `reader-html.md`） |
| 进版（`agent/` 直下，内部扁平） | `agent/scoping-note.md`（P1 主要结论件；P0／P2／P3 结论在八节包 §6）；`agent/knowledge-package.md`（§5 证据表 canonical）；`agent/cards/<DOI-slug>.md`（单卡 canonical；`cards/` 是 `agent/` 唯一子目录）；`agent/search-strategy.md`；`agent/screening-log.md`；`agent/bucketing-and-sources.md`（下载台账）；`agent/placement-plan.md`（落点计划）；`agent/reference-list.md`；`agent/b1-and-regulatory-supplement.md`；`agent/gate-summary.md`；`agent/manifest.json`；`agent/doi-list.txt`（身份列表＝终池全量 DOI）；`agent/download-queue.txt`（队列＝研读选取集＝Step3 输入＝入库集，与台账行数对齐）；`agent/version-record.json`（监管版本记录，KB 级 JSON，扁平化）；商业库导出原文 `agent/b1-<库>-export-<YYYYMMDD>.txt` |
| 忽略（`run/` 缓存，凭 `agent/manifest.json` 再生） | 抽取文本；下载正文 PDF；原始池（含发现层格级件 `run/b1/step1-diagnostics.json`，冻结名形态、由 `scripts/step1_diagnostics.py` 投影）与发现层机制读数／覆盖账（`citation-expansion.json`／`gap-fill.json`／`high-cited-check.json`／`citation-graph.json`／`source-declaration.json`／`coverage-ledger.json`）；去重与分桶中间报告（含入库摘要表 `run/b1/ingest-abstracts.json`）；研读可及性清单 `run/step5-accessibility.json`（研读集逐条记 DOI｜可及性三值｜摘要来源｜取数时刻，ADR-0023）；批处理中间件；中间队列；下载、身份核验与写库收据；库快照；复核文本；门禁全量；运行脚本副本 |
| 唯一例外 | `run/b2/ema-*-spc.pdf`：监管原文字节留档进版，哈希记 `agent/version-record.json` 的 `archive_sha256` |

- 进版表按**全部路线与条件件**枚举（单次交付只在场其中一份子集；路线剪裁见本节 P1 行与 §11）：`R6` 的 `agent/` 直下 ≤14 条目按**实际在场件**计。

- `agent/` 内一切件**默认进版**——忽略规则只兜 PDF 与 `run/`（`/workspace/**/*.pdf`、`/workspace/*/run/*`），没有针对 `agent/` 的规则；**故 `agent/` 内不得落 PDF**（进版件无一为 PDF，PDF 只进 `run/`）。「该不该进版」由本表枚举，不靠忽略规则兜底。
- `agent/doi-list.txt` 与 `agent/download-queue.txt` 是两件、两条契约（ADR-0032）：前者＝身份列表（终池去重后**全量 DOI**，含未选取者；P1 兼作候选清单）；后者＝研读选取集（＝下载队列＝入库集＝研读对象，Step2 选取步产出、一行一条 DOI、Step3 的输入，台账行数＝本件条目数）。剪队列不改写身份列表。

- P1 路线剪裁（进版 9 件）：根 2 件 `INDEX.md`＋`<slug>.html`；`agent/` 直下 7 件 `agent/scoping-note.md`、`agent/search-strategy.md`、`agent/screening-log.md`、`agent/bucketing-and-sources.md`（版内四桶报告，行集＝全池）、`agent/doi-list.txt`（候选清单，终池全量 DOI）、`agent/gate-summary.md`、`agent/manifest.json`；**无 `agent/download-queue.txt`（P1 不产队列件）**、无 `agent/cards/` 单卡、`agent/knowledge-package.md` 八节包、`agent/placement-plan.md` 落点计划；`agent/reference-list.md` 无证据表则缺席（`ledger_pointers` 记「无」）。P1 产出＝候选清单＋四桶报告，产出命令 `uv run python .claude/skills/med-lit-review/scripts/step2_select.py pool-ledger --delivery workspace/<目录名>`；manifest 剪裁见 §10，verdict 与三门见 §11。

- 忽略语义（`.gitignore`，作用域只限仓库根 `/workspace/`，顺序即语义，不可调）：先全忽略 workspace 下 PDF → 再忽略各 topic `run/` 的内容（忽略内容而非目录本身，否则其下否定全部失效）→ 两级否定先捞回 `run/b2/`、再捞回 `run/b2/ema-*-spc.pdf`。误 `git add -A` 也带不进 PDF 与缓存。
- 已跟踪的历史包与上游发布件不受新规则影响（已跟踪文件不被驱逐）。
- 被忽略件一律凭 `agent/manifest.json` 再生（§10）；擦除演练口径：删掉 `run/` 内容后，只按清单命令逐条重建，再逐条比对哈希；人读 HTML 按输入哈希分流——输入件字节全等时以 `--check` 复算比对，任一漂移记「跳过」不判失败；清理动作与对账口径见 §15。

## 7 台账口径与 join 键（下载台账、落点计划、监管版本记录）

三张版内表各是一类被忽略收据的版内投影，按身份切片取行，查询目标只指版内件。角色：`agent/bucketing-and-sources.md` 下载台账、`agent/placement-plan.md` 落点计划、`agent/version-record.json` 监管版本记录。

- 下载台账（`agent/bucketing-and-sources.md`）：行集＝研读选取集（＝下载输入，逐条见 `run/b1/selection.json` 的 `entries[]`），一行一条、一行一指针（条目数 = 队列条目数 = 研读选取集条目数，四桶计数之和与之一致）；P1 无选取，本件即版内四桶报告、行集＝全池。十列固定：`DOI｜桶｜预计来源｜实际来源｜采用｜未采用原因｜grey_hit｜身份核验｜文件｜口径版本`。文件列只写 basename（DOI-slug），禁绝对路径与转义形态；`身份核验` 列只记短串 `verdict｜checked_at｜方法`（如 `match｜2026-09-17｜前3页DOI命中`），完整证据（`head_first_lines`）留在被忽略的身份核验收据，`agent/manifest.json` 的 `receipts` 条目记其 sha256——台账行内不复制证据字节；口径版本列每行必填（见本节末条）；复用命中行的取值编码见 §14.4。
- 落点计划（`agent/placement-plan.md`）：列序 `条目键｜DOI（纸张身份／join 键）｜标题｜候选落点｜确认结果｜收藏集｜父条目｜回查｜本次合集｜附件`。DOI 列是按身份检索的主键；本次合集列逐条记挂载：`已挂：med-lit-review/<交付目录名>` 或 `未挂：<去向>`；附件列逐条记状态：`已挂：<basename>` 或 `无 PDF：<补挂环结果｜待接机构后重试>`；表头另有一行本次合集行（集名｜建／复用／AFK 自动建｜双挂／追挂一致 M／N，D-10 读数位；P2／P3 记「不建集」）；表尾两行计数是被忽略写库收据（`run/placement-write-report.json`）的版内投影，键一对一——`计划数↔counts.plan`、`写入数↔counts.written`、`回查一致数↔counts.rechecked_consistent`、`既有跳过↔counts.preexisting_not_rewritten`，附件已挂／无文件／既有补挂由附件列逐条数出（以 children 回查为准）。
- 监管版本记录（`agent/version-record.json`）：逐条冻结 `source/document/identifier/version_pin/effective_time/set_id（有则）/archive/archive_sha256（仅字节留档条）/scope`；修订号取官方页面读数（PDF 正文与元数据无修订串），读数来源写进 `reading_sources`；无监管收录的 topic 不建该件，索引里显式记「无监管版本记录」。
- join 键与三处字节位置：一个身份一条链——下载台账 DOI → 落点计划条目键／DOI → Zotero children（附件回查）。**两表行集关系（ADR-0032）**：台账行集＝研读选取集，落点计划行集＝写库集合＝研读选取集 − 仅题录成员（＝有卡集）——两集合现只差「仅题录」一档，落点计划条目集 ≡ `agent/cards/*.md` 的 DOI 集（硬门禁 W-22）。三处位置（仓外抓取缓存、知识库本地附件、仓内台账）各归各的真源，同一份字节不存两份；同一份文件是否同一份的唯一机器证据是身份核验记录（短串在台账行、完整证据在被忽略收据）。无文件条目走落点表附件列加知识包 §8 局限声明，不单独立台账。
- 身份链切片（索引寻址命令表必备任务）：一条身份的各跳都在版内——`grep` 台账行 → `grep` 落点行 → 条目键的 children 回查（只读 API；版内投影即落点行附件列）；监管身份再多一跳 `jq` 版本记录行，字节留档条以 `sha256sum run/b2/ema-*-spc.pdf` 对齐 `archive_sha256`（该字节是进版例外，可查）。被忽略收据只经 `agent/manifest.json` 的哈希指针出现，不作查询目标。
- 口径版本与分界：`pre-0006-oa_first`（默认启用之前：灰色命中不采用）｜`post-0006-fastest`（默认启用之后：灰色竞速采用 + 如实报告）。分界以该次交付的 `agent/manifest.json` 的 `caliber.grey_gate_version` 为准，台账每行的口径版本列与之一致；历史包报告原文冻结，只在文件头加口径注记。
- 口径注记（历史包专用形态）：下载台账（`bucketing-and-sources.md`）文件头一行 `> 口径版本：<值>（历史归档口径：一句说明）`；门禁读包时先读该注记（无注记按该包清单的 `caliber.grey_gate_version`——新形态在 `agent/manifest.json`、历史包在交付根 `manifest.json`，再退现行口径），`pre-0006-oa_first` 的「灰色不采用」按当时口径解读，不判现行失败。

## 8 粒度规则（R1–R8）

- R1 索引单件**单层**：`INDEX.md` 为机读纯索引——文件表（一行一件）＋寻址命令表（任务→可粘贴命令）＋**唯一一行**状态计数＋「待查」纪律（形态见 §9）。人读层（先读我／深读路径）改由人读 HTML 承担，不再进索引。
- R2 单源：同一证据表只存一处——`agent/knowledge-package.md` §5 内联为 canonical，运行期草稿表（如 `run/b1/evidence-table.md`）不落盘、永不进版；卡片单卡 canonical。人读 HTML 是**派生渲染**、非第二处真源（与 R5 同族，§6 擦除演练）。`workspace/` 之外的历史包（`.scratch/med-lit-review-skill/`）内 pre-cutover 草稿属冻结归档：本规范不回溯删除、也不判其违规（唯一机器副本的说法只对规范生效后新建的 topic 成立）。
- R3 卡片单形态：`agent/cards/<DOI-slug>.md` 一件一篇、唯一家（文件名即身份、即寻址）；无派生合卡，机器读面唯一家即该目录。
- R4 台账行寻址：逐条台账一行即一指针，按身份列（DOI）`grep` 取行（台账件在 `agent/`）。
- R5 再生件指针化：抽取文本、原始池、下载正文、批处理中间件、门禁全量不进版，只留 `agent/manifest.json` 的再生命令 + 哈希。
- R6 版内封顶（枚举制）：顶层**固定四项**——`INDEX.md`、`<slug>.html`、`agent/`、`run/`，出现第五项即结构错误；`agent/` 直下 ≤14 条目（P0 基准 12＝11 件＋`cards/`）；`agent/cards/` 逐卡不计入件数（篇数由研读规模决定）；体积 ≤2.5MB；`step1-pools` 一类原始池不计入。P1 按 9 件清单折算（§6）。
- R7 门禁摘要常驻：`agent/gate-summary.md` 头部人读 verdict，全量只经 manifest 指针（§11）。
- R8 头尾惯例：厚件顶部小结（≤40 行）、尾部机读体——角色是供源件定位，人读入口归人读 HTML；派生件的来源与再生命令按 §10 的 `derived` 段登记；被忽略件无需人读头。

## 9 索引单层形态（机读纯索引）与切片查询式

`INDEX.md` 是机读纯索引，也是唯一入口：只列本交付的文件与寻址命令，**无内容、无观点**。可机器判的骨架＝**表＋命令**结构——除状态计数行外**不得出现自由段落**（自由段落＝落不进标题／表格／代码块／块引用／列表项的散文行；骨架判定见 `check_delivery.py` 的 W-09，样例形态见 04 票资产 §5.1）。

- 文件表（一行一件）：路径写**交付内相对**（根件名或 `agent/…`），描述中性（不写结论、不摘内容），不写除状态计数外的任何读数。
- 寻址命令表（任务 → 切片命令）：命令一律**仓库根相对**（`workspace/<目录名>/agent/…`；`<目录名>` 为占位，不写死日期）；包内相对只允许出现在「人读入口」一行的 Markdown 链接。任务至少覆盖：人读主件、结论、按 DOI 取证据表行、按 DOI 取单卡、来源与灰色、落点与附件、身份链（一条身份走通下载行→落点行→附件回查，§7）、口径版本、引文表按行取条、监管版本记录（有则）、门禁全量、再生；每行给一条可粘贴命令。
- **唯一一行状态计数**：候选 → 去重后 → 研读选取 → 证据表行 → 写库 → 附件。本件允许的唯一读数；需要别的读数就写命令，让命令输出承载。
- 「待查」段：定向只读本件；查不到再按 `agent/manifest.json` 的清单指针走，**不整读主题包**；查询只查版内件，`run/` 被忽略件一律经 `agent/manifest.json` 取命令与哈希。

明禁：抄结论、写 verdict（过／未过）、写建议或优先级判断、写因果解释；除那一行状态计数外的任何读数（含行数、件数、体积、日期）；把 `run/` 被忽略路径当查询目标；把人读导航（先读我／深读路径）写进索引——人在人读 HTML 里导航，索引只做机器入口。

纪律：200KB 以上 JSON 必 `jq`，400 行以上台账必 `grep`；**厚件禁整读**——任何取数走切片，全读禁令是本规范的一部分。

P1 变体同形（文件表 10 行：根 2 件＋`agent/` 直下 7 件＋`run/`，无 `agent/download-queue.txt`、无 `agent/cards/`、无八节包、无落点计划），状态计数行同形（P1 计数止于四桶：候选总数（终池）＝四桶和＝`agent/doi-list.txt` 条目数，§11）。

## 10 再生清单（manifest.json）字段

清单落 `agent/manifest.json`（§6 进版表）；段与字段如下。

- `topic{slug, delivered, dir}`：主题 slug、交付日期、仓库根相对目录名；演示件另加 `demo` 与 `demo_note`。
- `caliber{grey_gate_version}`：口径版本（`pre-0006-oa_first` / `post-0006-fastest`）。
- `scripts_home`：权威脚本家 `.claude/skills/med-lit-review/scripts/`（唯一权威家，skill 内脚本随 skill 交付）；topic 内 `run/scripts/` 一律是忽略副本，再生命令只引权威家、不引副本；历史包内脚本为冻结存档、非权威（tracer 侧废弃指针见 `.scratch/med-lit-review-skill/tracer-guidelines/scripts/README.md`；blind 侧 `run/scripts/` 属忽略缓存副本）。
- `regeneration[{class, step, command, artifacts[{path, sha256}]}]`：逐类再生命令加逐件哈希；**被忽略件**必须逐条在此登记——本段只收 `run/` 下的件，`artifacts.path` 保持 `run/…`、不带 `agent/` 前缀（`run/` 位次不变）；进版件不进本段、走 `versioned_artifacts[]`。`command` 以仓库根为工作目录执行，`artifacts.path` 为 topic 相对路径。
- 分桶类（Step2）一次登记一件条目：`step` 记 `Step2`、`command` 记重跑 `.claude/skills/med-lit-review/scripts/step2_dedupe_sort_bucket.py --delivery workspace/<目录名>`，`artifacts` 逐件记 `run/b1/` 下的分桶产物哈希（`dedupe-report.json`／`dedupe-decisions.json`／`sort-report.json`／`cited-counts.json`／`openalex-enrich.json`／**入库摘要表 `ingest-abstracts.json`**）；本步不产队列、不产四桶报告；入库摘要表不另起类、不新增字段。
- 选取类（Step2 选取步，ADR-0032）一次登记一件条目：`step` 记 `Step2`、`command` 记定稿 `uv run python .claude/skills/med-lit-review/scripts/step2_select.py apply --delivery workspace/<目录名> --n <N>`，`artifacts` 逐件记 `run/b1/` 下选取产物哈希（`selection.json`／`bucketing-report.md` 四桶报告（行集＝研读选取集））；**队列件 `agent/download-queue.txt` 与台账骨架 `agent/bucketing-and-sources.md` 属进版件**（走 `versioned_artifacts[]`、不登记本段）；P1 无选取步，其候选清单与四桶报告由 `pool-ledger` 动词产出、同为进版件。
- 研读类（Step5）一次登记一件：`step` 记 `Step5`，`command` 记产生该件的管线命令，`artifacts` 逐件记 `run/` 下研读产物哈希（**可及性清单 `run/step5-accessibility.json`** 为必登件）；可及性清单不另起类、不新增字段。
- 发现层类（Step1 ＋同窗机制与覆盖账）一次登记一件：`step` 记 `Step1`（格级件与发现层机制读数件）或 `Step2`（覆盖账）；`command` 逐件记产生该件的权威命令——格级件记 `uv run python .claude/skills/med-lit-review/scripts/step1_diagnostics.py --delivery workspace/<目录名>`（逐池检索 CLI 读数按 ADR-0023 冻结名投影），引文扩展轮／迭代补漏／高被引缺席核对的读数件各记其执行器命令（`citation_expand.py`／`gap_fill.py`／`high_cited_check.py`，引文图另记 `citation_graph.py`），覆盖账记 `uv run python .claude/skills/med-lit-review/scripts/coverage_ledger.py --delivery workspace/<目录名>`；`artifacts` 逐件记 `run/b1/` 下哈希（`step1-diagnostics.json`／`citation-expansion.json`／`gap-fill.json`／`high-cited-check.json`／`citation-graph.json`／`coverage-ledger.json`，在场者逐条登记）。

- `versioned_artifacts[{path, sha256}]`：进版件逐件登记（单卡逐卡登记 `agent/cards/<DOI-slug>.md`）。`path` 为 topic 相对路径——交付根件（`INDEX.md`、`<slug>.html`）不带前缀，其余带 `agent/` 前缀。
- `receipts[{path, sha256}]`：被忽略收据逐件登记，`path` 保持 `run/…`（与 `regeneration` 同口径，不带 `agent/` 前缀）。
- `ledger_pointers{download_ledger, placement_plan, gate_summary, reference_list[, regulatory_version_record]}`：版内台账位置，值带 `agent/` 前缀（如 `agent/bucketing-and-sources.md`）。
- P1 剪裁：`regeneration` 缺席类（cards 单卡／八节包／落点类）不记条目；`ledger_pointers` 缺席指针记「无」（P1 通常为 `placement_plan` 与 `regulatory_version_record`，`reference_list` 缺席时同样记「无」）。
- `derived{reader_html{path, sha256, inputs[{path, sha256}], command, machine_reads}}`：派生渲染件（人读主件，由版内 markdown 真源确定性渲染）的来源、输入件清单、再生命令与哈希；`machine_reads: false`——机器读面在 `agent/`，渲染件不是第二处真源。字段口径、登记时机与 `--check` 复算见 `reader-html.md` §4–§5（登记由渲染器在产页同批写入，幂等、语义不变即零写）。
- `exemptions[{path, sha256, version_record, note}]`：唯一例外（监管原文）；自带字节、不进 `regeneration`，靠版本记录的 `archive_sha256` 证明。
- 复用指针段：容器键 `reuse_pointers`（按归一 DOI 主键），字段与登记口径见 §14.5。
- 命令口径：真实 topic 记产生该件的管线命令（来源在网，哈希是完整性钉子，不复现字节）；演示 topic 记 `scripts/demo_topic_fixture.py` 的确定性再生命令——演示无真实管线输出，且门禁执行器一类真实工具的输出含时间字段（`locked_at`），不可能逐字节复原，故演示件用确定性重建，命令可实跑、逐字节可比。

## 11 门禁摘要 verdict 头形态（gate-summary.md）

- 头段 `## Verdict（先读我）` 固定三行：判定（过/未过 + 一句依据）、未过条目（列表或「无」）、缺口去向（指向版内件）。
- **交付门禁必跑项（三件套）**：本件生成时必须跑过——① 包文本校验 `uv run python scripts/review_knowledge.py agent/knowledge-package.md --cur-year <YYYY>`（对象是八节包；P1 无八节包时不适用）；② 生长循环落缓冲检查 `uv run python scripts/check_growth_loop.py <交付目录>`（读 `agent/screening-log.md`；ADR-0016 的原判据）；③ 交付形态契约 `uv run python scripts/check_delivery.py --delivery <交付目录>`。任一缺口即**阻断交付**（verdict 判未过并列该条，同 ADR-0016 的阻断语义；三件套的扩面与结构类停软项的边界见 ADR-0025 与 ADR-0010 窄修订条）；P0–P3 全覆盖（P1 无 T-01 信号只是少一路输入，不豁免）。判定行须逐条引用该次读数（含 ③），摘要的「交付门禁读数」表随之增一行。
- **抽跑一条**：`uv run python scripts/check_delivery.py --card-probe [<真实包>]`——用门禁自己的解析器（`scripts/package_parse.py`：验收门 E-04／E-05 与交付形态检查 W-12 两处共用一份实现，禁第二份）跑最新**同形态**真实包，只判解析器健康（解析条数 > 0 且 ＝ `agent/cards/*.md` 文件数；0 卡即红）。无可抽样本记「样本缺位」，不报错、不阻断；失败＝当次交付门禁红，在未过条目与缺口去向各记一行（钻取指该探针输出）。探针输出一行 `RESULT: …` 读数（含样本路径与判据；「样本缺位」亦是一行读数），照录进摘要的「交付门禁读数」表。
- 钻取段：全量门禁输出只经 `agent/manifest.json` 的 full gate 再生条目定位（`jq '.regeneration[] | select(.class=="full_gate")' agent/manifest.json`）；摘要不复述全量，也不把被忽略路径当查询目标。「全量尾」指本摘要尾部的这段钻取指针，不是把全量复制进版内。
- 秒级：verdict 在文件最前，不读全量即可判过否；失败才钻取。
- P1 形态（三门单点落字处，他节不复述）：verdict 头段仍固定三行、只判计数——判定＝三数一致即过＋一句依据；未过条目列三数；缺口去向指版内四桶报告（`agent/bucketing-and-sources.md`，行集＝全池）与筛选日志。三数＝候选总数（终池）＝四桶和＝`agent/doi-list.txt` 条目数（P1 不产队列件，ADR-0032）。P1 三条独立门：五要素齐（问题／候选计数／四桶构成／可研判性／升格建议）／计数三方一致（同上三数相等）／计数皆有出处指针（每个数必指台账出处：文件名＋grep 键）。执行面为人工＋grep 切片；三件套适用性：① 不适用（P1 无八节包），② ③ 照跑；验收门（`run_gate.py`）仍只跑 P0。

## 13 演示件重建口径

- 演示包（`2026-09-17-exclusion-gate-drill` 全形状骨架、`2026-09-17-pcsk9-ascvd` 当日交付）**当前不在 `workspace/` 内**：`git log --diff-filter=D -- workspace/` 可见删除记录，现存各目录不含演示件。§10 的 `topic` 字段（`demo`／`demo_note`）与命令口径提到演示件时按本节读。
- 演示件的忽略件由 `scripts/demo_topic_fixture.py` 再生（即 manifest `regeneration[].command`），它是演示件忽略件的**唯一确定性再生器**：真实 topic 的来源在网、字节不复原，演示 topic 没有真实管线输出，故可逐字节重建（未登记的 topic 或类名报错退出）。各演示件自标 demo，不作真实证据。
- **演示件不产人读主件**：不做演示 HTML、不扩 fixture 件类；渲染与确定性口径的演练面由**复算开关**承担——`scripts/reader_html.py --check`（配真实包），擦除演练按输入哈希分流（§6／§15）。
- 若日后重建演示包：按当时形态用同一渲染器产出（含人读主件与 `agent/` 落点），重建范围仍只限已登记的忽略类，不扩 `demo_topic_fixture.py` 的件类。

## 14 跨交付复用与失效规则（形态 B：旧交付被忽略件指针）

复用＝消费方交付从在先交付的被忽略件取件（同 topic 重跑与跨 topic 一视同仁）：消费方 `agent/manifest.json` 记复用指针段（§14.5），按归一 DOI 主键（小写、去前缀）定位指针、字节以 `sha256` 校验；取件链＝定位指针 → 读旧交付件 → 源件 `sha256` 全等校验 → 物化复制进消费方运行全文目录（`run/fulltext/`）→ 副本 `sha256` 复验 → 身份核验。不建中心目录、不建跨 topic 索引，复用边不集中登记；旧交付严格只读，不补写、不反向登记。本节同时是失效与正确性规则（§14.1）与 TTL 总表（§14.2）的家，覆盖全部缓存位、不限于复用。脚本做法、取件收据与台账六格落点见 `step3-download-chain.md` §5。合约见 `docs/adr/0010-workspace-persistence-contract.md` 修订条。

### 14.1 失效与正确性规则

| 规则 | 口径 |
|---|---|
| 哈希每次必验 | 命中校验与物化复验两处 `sha256` 全等才放行；mtime 与体积不作校验证据（只可作诊断字段，不得作放行或跳过依据） |
| 失败统一语义 | 取件链任一环失败（指针缺、件缺、源哈希不符、副本复验不符）＝ miss，落常规下载链路（含上游近窗缓存）；不补写旧交付、不自动改指针 |
| 进程内 memo 例外 | 同 run 内已验物件允许进程内 memo（同一副本不重复哈希）；随物化写操作失效、生命周期＝进程；跨跑一律重验 |
| 身份核验跑跳 | 归一 DOI、字节 `sha256`、期望值（题名／作者／卷期 pins）、方法四项全等 → 复用原「通过」结论（跳），不重开 PDF；任一不等（含期望值缺项、方法版本变化）→ 本地重跑核验；原结论 `False`／`None`（拦截态）永不缓存为通过，重跑核验或按原语义转人工 |
| 读数沿用 | 实际来源、grey_hit、身份核验短串（含原 `checked_at`）与被引查询日期（快照 `query_date`）一律沿用原始读数，加复用标记、不写重跑日；不虚报为本次抓取（ADR-0006） |
| 缓存命中读数 | 命中格（`cache_hit: true`）读数分两路：可复算读数（实返、日期过滤三计数）由缓存载荷确定性复算、不存第二份；不可复算读数（`total_available`）随载荷收据沿用——缺收据即按 miss 重取、不得补造；不可得记 `null`，`cap_hit` 不可判定记 `null`、不得读作 `false`；`collected_at`＝原始采集时刻 |
| 跨口径双留痕 | 允许跨口径复用；口径列＝本次交付口径（＝`manifest.caliber.grey_gate_version`、列语义不变），复用标记附注原口径（行内编码见 §14.4） |
| 显式绕过 | 用户显式要求重抓或换显式 strategy → 无条件绕过负面缓存与既有指针、全量重提；动作进日志 |
| 失败记忆错误类二分 | 可记忆＝全通道穷尽型确定性失败（无来源类）；不可记忆＝传输类（超时、限流、5xx、连接与域名解析失败）、凭证权限类（未授权、被挡、登录态缺失）、配置类——永不写负面缓存；剪队列动作进运行日志（错误类＋原始失败日期） |
| 锚点锁窗口内复用（验收门禁） | pins 全量全等且锁龄不超过 168 小时 → 复用原 `valid` 结论，门禁输出记「复用（锁龄 Xh）＋原 `locked_at`」；漂移、缺失、未取到（unverified）永不复用为通过；超窗或 pins 任一项不等 → 联网重核；传输失败仍 fail-closed |
| 监管版本记录不复用 | 每趟直取官方、版本钉读数进版本记录（`agent/version-record.json`）；不因 DOI 复用 |

### 14.2 有效期（TTL）总表

到期基准＝数据自身采集时刻；复用不顺延——命中与复用一律不延长窗口（防复用链把旧数据续成永久）。

| 缓存位 | 窗口 | 越界行为 | 留痕 |
|---|---|---|---|
| 检索响应、回填、被引快照、补录元数据 | 168 小时 | 超窗或键变 → miss 重取 | 数据自身采集日期（缓存写入时间；被引快照 `query_date`；补录日期）进检索策略／筛选日志 |
| 锚点锁（验收门禁） | 168 小时（锁龄自原 `locked_at`） | 超窗或 pins 不等 → 重核 | 门禁输出记「复用（锁龄 Xh）＋原 `locked_at`」；drifted／missing／unverified 永不记通过 |
| 失败记忆（否定缓存） | 14 天（自原始失败时刻） | 超期允许重试；显式重抓无条件绕过 | 剪队列动作进运行日志（错误类＋原始失败日期） |
| 监管版本记录 | 不复用 | 每趟直取官方 | 版本钉读数进 `agent/version-record.json` |
| 内容寻址缓存（确定性段、词表解析） | 无 TTL（键即正确性） | 键项任一变即 miss | 不落版 |
| 进程内／会话内缓存 | 进程生命周期；归置库读快照＝分钟级窗口（同 session；窗口内按 plan 哈希＋库版本探针命中，库版本未变时窗口内命中与重读等价） | 写操作即失效；窗口或库版本任一变即 miss，跨交付不复用 | 不落版 |
| 上游仓外缓存 | 上游自管（窗口与本地档位对齐） | 只作近窗补充，不替代复用指针与下载台账 | 不上 repo 台账，以返回收据为准 |

> 注：检索响应缓存位载荷形态升级（旧记录缺收据）同「超窗或键变」按 miss 处理——重取后以当前形态改写、不写双形态读路径。

### 14.3 边界清单（六位进出）

| 位置 | 复用 | 条件与口径 |
|---|---|---|
| `run/` 下载正文 PDF | 开放 | 形态 B 指针链；悬空或哈希不符＝miss 重下载 |
| Zotero children 正文附件 | 条件开放 | 作研读与取件来源（本地附件优先、不走下载链）；身份按全键 verdict 复用或重跑核验，`False`／`None` 永不缓存为通过；在库（收录）判定仍只认 DOI 字段；复用只读，挂载写路径按 ADR-0007 不变 |
| 身份核验收据（`run/fulltext-identity.json` 一类收据位） | 条件开放 | 全键等复用「通过」（见 §14.1）；收据不复制字节——短串进台账、完整证据留被忽略收据、`agent/manifest.json` 记哈希 |
| `run/` 抽取文本（`run/text/`） | 禁 | 一律凭本轮 PDF 再生（再生命令已登记 `agent/manifest.json`）；避抽取文本与抽取工具版本耦合 |
| 监管原文 PDF 与版本记录（`run/b2/ema-*-spc.pdf`、`agent/version-record.json`） | 禁 | 每趟直取官方、版本钉读数进版本记录；字节不进复用键 |
| 翻译件与引擎缓存（`zotero-pdf2zh` 的 `translated/`、babeldoc 缓存） | 引擎自管 | 不进 repo 复用体系、不进交付；译文仅辅助读，禁作原文／证据／身份输入 |

- 隔离件（身份不符件，`run/quarantine/`）永不进复用链与挂载。
- 通用条件（全位适用）：口径留痕见 §14.1；旧交付与库内附件只读；miss 安全回退（不损正确性）；台账、指针与报告只写 basename 与相对路径（绝对路径零命中，含转义形态），机密四样（密钥、登录票据、代理凭证、机构信息）不复述、不因复用进记录。

### 14.4 下载台账复用命中行编码（十列不变）

| 列 | 值 |
|---|---|
| 实际来源 | 原始下载读数（原样，如 Sci-Hub／OpenAIRE） |
| grey_hit | 原始读数（是／否） |
| 身份核验 | 原短串 `verdict｜checked_at｜方法`（`checked_at` 为原核验时间） |
| 口径版本 | 本次交付口径（＝`manifest.caliber.grey_gate_version`；列语义不变、每行必填） |
| 采用 | `是（复用自 <旧交付目录名>）`；原口径与本次不同时附注 `；原口径 <值>` |
| 文件 | 消费方 `run/fulltext/` 内 basename |
| 其余列 | 按本次交付填写（桶／预计来源按本次队列；未采用原因照常） |

miss（悬空或哈希不符）与未复用行照常按本次实际来源与口径填写，不因清理或复用变形（ADR-0006）。

### 14.5 复用指针段字段（`agent/manifest.json`）

复用指针段容器键＝`reuse_pointers`（对象，按归一 DOI 主键——小写、去前缀）：十字段 `source_delivery`（来源交付目录名）｜`path`（旧交付内相对路径）｜`sha256`｜`source`（原始来源）｜`grey_hit`｜`caliber`（原口径版本）｜`retrieved_at`（原始采集日期）｜`identity`（身份核验短串）｜`verified_at`（物化校验时刻）｜`reused_into`（消费方相对路径）；物化件同时进 `regeneration`（类 `fulltext_reuse`，命令＝定位→源哈希→物化→复验链，哈希＝同 `sha256`）。按 DOI 切片：`jq '.reuse_pointers["<归一 DOI>"]' agent/manifest.json`。

## 15 清理与配额口径

四处缓存位——仓内旧交付 `run/`、翻译输出 `zotero-pdf2zh/server/translated/`、翻译引擎缓存（babeldoc）、上游仓外缓存——一律不设主动清理机制、不设配额；清理只在用户显式要求或交付间空档由人工执行，不自动触发、不新增脚本、不进交付门禁（合约见 ADR-0010 修订条）。`run/` 是四处中唯一进 `agent/manifest.json` 再生链的位；其余三处删除零对账动作（翻译输出为一次性产物、引擎与上游仓外缓存归其自管）。

- 只删被忽略字节（人工、路径限定）：标准动作＝`git clean -Xdf`（先 `-n` 预览；限定 `workspace/` 或更窄，禁全仓裸跑）；进版例外（`run/b2/ema-*-spc.pdf`）与一切跟踪件不在清理面（否定规则与 clean 语义天然幸免）。人读 HTML 与 `agent/` 内全部件都是进版件，同样不在清理面；`run/` 留在交付根顶层，故 `git clean -Xdf` 的清理面不随本次形态调整变化。全清合法（＝擦除演练语义）：删掉 `run/` 全部内容后按 `agent/manifest.json` 清单逐条再生；回收按复用价值逆序——`run/quarantine`／`run/batches`／`run/text`／`run/gate` 先行、`run/fulltext` 最后。
- 脚本自管缓存（各 skill 包内 `.cache/`，如 `jadense-scholar-search` 的检索响应与回填合并结果、`use-zotero` 的 Crossref 补录元数据、附件身份核验结论与归置库读快照、`med-lit-review` 的词表解析结果）沿用上面这条口径、不改四位决议：不设主动清理、不设配额，清理＝人工、路径限定到缓存目录本身——`git clean -Xdf -n <skill 包>/.cache` 先预览、确认只列缓存格后再去掉 `-n`，不得把整包列入清理面（包内 `dist/` 与 `node_modules/` 同属被忽略字节、但不是缓存）。该位供本仓各次运行共享，只存脚本级读数——不是交付件、不进 `manifest.json` 与台账、不集中登记复用边（§14.2 窗口与命中规则照用）。
- 清单不因清理改写：`regeneration` 是再生合约（命令＋哈希钉子）、不是在场清单——不删条目、不改 `regeneration[].artifacts[].sha256`、不置空；缺席不判 `agent/manifest.json` 失效。
- 悬空即 miss：复用取件 `sha256` 校验兼作清理对账——悬空／不符＝miss、走常规链路重下载；不补写旧交付、不改指针、不反向登记；删除只降复用命中率、不损正确性。
- 在场才对账：在场被忽略件的哈希与 `manifest.regeneration[].artifacts[].sha256`（§10）比对，不符＝漂移警示；缺席件跳过、不报错。

## 16 历史包读法（形态分界）

本节给**旧形态包**的判别特征与读法；新旧形态的分界只写在这里，不在包里逐件标注（新形态＝本文件其余各节所述）。

- **判别特征**（按件实判，不按目录名与日期判）：① **无主件**——交付根无 `<slug>.html`，人读入口至多是一份双层 `INDEX.md`；② **真源不在 `agent/`**——`knowledge-package.md`／`cards/`／`manifest.json` 等落 topic 顶层而非 `agent/` 直下；③ **含合卡类件**——`extraction-cards.md`／`extraction-cards.merged.md` 在场（`R3` 改单形态之前的双形态）。
- **读法：完全冻结、不逐包加注记**——包内容一律不动（含合卡两件与 `doi-list.txt` ≡ `download-queue.txt` 一类旧重复对），不补写、不重排、不迁移，也不为标记形态往包里加注记；包内 `manifest.json` 的哈希记录与版本身份按原样保留。三特征任一成立即按本节读，不混用新形态规则（§8 的 `R1`／`R3`／`R6` 一类只对新形态包成立）。
- **边界**：① §7 的口径注记（下载台账文件头一行口径版本）是**口径版本**惯例、与形态分界无关——旧形态包按原口径读，本节既不新增、也不撤销该注记；② 旧形态包在新门禁（§11）下的未过读数按 ADR-0008 口径留档，**不作交付阻断**；③ 包内容只在用户显式要求时改动，不在交付流程内自动触发。
