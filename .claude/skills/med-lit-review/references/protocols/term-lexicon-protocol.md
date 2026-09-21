# 术语词表协议（关系四分、消费与生长循环）

本文件是词表的规则与协议唯一原文。数据两件住 `references/lexicon/`：`term-lexicon.yaml`（机器唯一真源，手写 YAML 子集，`scripts/term_expand.py` 自带读取器）与 `term-lexicon.candidates.yaml`（候选队列）。词表版本随 skill 冻结，每任务在筛选日志与检索策略模板里引用版本号；通用词表见仓库 `CONTEXT.md`，定位与三件分工见 `docs/adr/`（ADR-0009 三分工、ADR-0016 生长循环、ADR-0022 三件套分工与命中判据）。

## 1 四类关系（关系有向，仅 report 为真的关系进 T-01 报告）

| 类 | 记法 | 语义 | 发现侧 | 事后校验（T-01） | 排序 s_rel |
|---|---|---|---|---|---|
| 同义 synonym | `=` | 同一概念可互换 | 扩展 | 混用报 warning | 计分 |
| 上下位 hierarchy | `⊃`／`⊂` | 包含，有方向 | 双向扩展并记方向 | 不报 | 计分（注明方向） |
| 易混 confusable | `≠` | 形近但不等 | 永不自动扩展 | 同篇并现报 info | 不计分 |
| 正字变体 variant | `≈` | 连字符／大小写／错字 | 静默归一 | 不报 | 计分（归一后按主名算） |

## 2 落点、字段与写法

- `references/lexicon/term-lexicon.yaml`（机器唯一真源）：顶层 `version`／`frozen_at`／`relations`／`synonym_sets`／`terms`；每条术语记 `term`（本形）、`canonical`（主名）、`chinese_aliases`／`english`／`abbreviations`、`family`（家族，HITL 预算按此分组）、`report`（该条是否进 T-01）、`rank`（是否进 s_rel 计分）、`source`（来源或版本）、`relations[]`（`type`／`target`／`direction`）。
- 关系方向（`direction`）以本条术语为主语写：`=` 同义；`⊃` 本条是目标的上级词；`⊂` 本条是目标的下级词；`≠` 易混；`≈` 正交字变体。扩展时方向记成「上级→下级」或「下级→上级」，不写成裸符号。
- `references/lexicon/term-lexicon.candidates.yaml`（候选队列）：字段 `term`／`family`／`edges[]`（`type`／`target`／`direction`）／`source`／`version_candidate`；**文件即队列**——在队＝待晋升，晋升即出账（条目移出，历史归真源 `source` 字段与 git）。
- 命名惯例：疾病族取中文主名（如 骨髓纤维化），药物族取英文 INN（如 ruxolitinib）；未确证的译名留空不猜补。**商品名与开发代号记别名，不建同义边、不进同义集**——中文商品名进 `chinese_aliases`、拉丁形商品名／开发代号进 `abbreviations`（例：ruxolitinib 的 Jakafi／Jakavi／Opzelura；沙库巴曲缬沙坦 的 诺欣妥／Entresto／LCZ696）；同义集只收通用名与英文名。
- 写法约束：真源是手写 YAML 子集（块映射／块序列／内联 `[a, b]` 列表／标量／注释），不用锚点、多文档与嵌套内联映射。

## 3 消费节点（在哪里、如何用）

- **发现（Step1）**：用 `uv run python scripts/term_expand.py "<词>"` 取扩展词；同义与上下位双向扩展并记方向，易混永不自动扩展。日志记 `词表 lexicon vX ｜ 查询词 ｜ 扩展方向 ｜ 带出词`。**检出即录（含初判边）**：`--log` 报「未命中」（词表未收录）即当场**对表挑战**（§6.1 第 1 步：同义／上下位／易混／正字，还是新家族？）出初判边，并 `--append --edge "type:target:方向"`（可重复；同义／变体形另给 `--canonical <主名>`；零确认；新家族加 `--family`）落队列——**只登记词、不记边就是机械生长，交付门禁会判红**；判不准就只记词、把边留到 §6.3 那一问。「命中无边」不需落队；结论无边时 `--no-edge` 显式声明。
- **分桶与中文补充（Step2／B1）**：只做归一化，禁术语归一。去重键 `norm()` = NFKC → 全小写 → 去全部标点 → 空白归一；同义成员在键上**不合并**（`心肌梗死` 与 `心梗` 是两个键），英文题录另存并列不改中文键。**检出即录**：题录词未命中词表、或中文键疑似态伴随术语差异时当场按上条落队（含初判边）。
- **事后校验（Step5 T-01）**：`review_knowledge.py` 读本词表——同义家族里出现两个及以上不同形（英文与缩写不算）且该家族 `report: true` 时报 warning；上下位不报；易混对同篇并现报 info；正字变体静默归一不报。**检出即录**：warning／info 涉及的表形写进 gate 输出作**候选建议**，由 agent 判断后 `--append --source "T-01 <行号>"`；脚本自身不写队列（混用告警不等于新词）。词表缺失或不可解析时按无表处理（T-01 不报），宁漏勿噪。
- 说明：词表不参与 Step2 排序取分——相关性 `s_rel` 读候选清单顶层 `pico_groups`（见 `references/protocols/step2-dedupe-and-snapshot.md`）；词表的扩展词经 Step1 检索式进入词组，无文件级消费。

## 4 查询与日志

```bash
uv run python scripts/term_expand.py "<查询词>"                    # 人读扩展清单（本形/别名/缩写皆可查）
uv run python scripts/term_expand.py "<查询词>" --format json       # 机器契约：term/canonical/version/status/expanded[]/notes[]/log
uv run python scripts/term_expand.py "<查询词>" --log               # 只输出一行筛选日志记法
uv run python scripts/term_expand.py --dump [家族]                  # 按家族打印词表表（不带家族＝全部；人读取用）
uv run python scripts/term_expand.py "<候选词>" --append \
  --edge "synonym:钙通道阻滞剂:=" --canonical "<主名>" --english "<英文形>" \
  --source "Step1/日志行 12"                                # 带初判边与英文形落队列（幂等；绝不改真源）
uv run python scripts/term_expand.py "<新家族词>" --append --family "<家族名>"   # 新家族建族用
uv run python scripts/term_expand.py "<词>" --append --no-edge --source "…"      # 显式声明无边（家族红线据此豁免）
uv run python scripts/term_expand.py --promote "<候选词>" --dry-run   # 晋升计划表（不写盘；先出表再问用户那一问）
uv run python scripts/term_expand.py --promote "<候选词>"             # 确认后一次成型：真源＋版本＋队列出账
uv run python scripts/term_expand.py --promote-all-candidates --dry-run   # 本任务全部候选的计划表
uv run python scripts/term_expand.py "<查询词>" --fallback          # 强制走无表姿态（演练：不扩展、不猜关系）
uv run python scripts/check_growth_loop.py --selftest                # 生长环机制自检（沙箱：边入口／晋升／扩展回归）
uv run python scripts/term_expand.py "<查询词>" --no-cache          # 绕过词表解析缓存（不读不写）
```

- 三态（机器契约 `status`）：`expanded`（有扩展）／`hit_no_expansion`（**命中无边**：已收录、无可扩展边，不扩展）／`miss`（**未命中**：未收录，不猜扩）。`--log` 一行是三态的中文记法：「扩展 N 个 ｜ 带出 …」／「命中无边（不扩展）」／「未命中（不扩展）」。
- `expanded[]` 每项四字段：`term`／`relation`／`direction`／`source`（恒为 `lexicon`——内置副本已删，关系数据只有真源一份）。
- `direction` 以**查询词为主语**判方向：`同义`、`正字归一`、`上级→下级`（查询词包含该项）、`下级→上级`（该项包含查询词）；非相邻项附「（经 中间词）」。
- 扩展口径：先按主名折叠（同义与正字变体并入主名），再沿主名级上下位图双向走整个连通分量；同主名的其余表形只出一个代表（变体形优先）。跨家族不自动串（无关系边即不扩）。
- 易混词**不出现**在 `expanded[]`，只在 `notes[]` 与 `--log` 的 `排除易混` 里列出。
- 词表未命中或 `--fallback`（无表姿态）：不扩展、不猜任何关系，只给 `notes` 说明；词表外的词一律不猜扩。
- **生成侧 nudge**：`--append` 除回执外会列出本族现有术语（真源∪队列，带边／无边标态）并提示「用 `--edge` 记初判边、确无关系用 `--no-edge`」——提示出现在刚记完词的那一刻，不依赖 agent 回头读协议件。
- 词表解析缓存：解析结果按「词表文件 `sha256` ＋ 解析器版本」跨进程复用（skill 包内 `.cache/lexicon/`，内容寻址、无 TTL——键项任一变即 miss）；`--no-cache` 显式绕过（不读不写）、`--cache-dir` 改位，`review_knowledge.py` 与门禁共用同一格。表在场且未 `--fallback` 的每次查询在 stderr 打一行 `CACHE lexicon source=cache|parse|off file=<basename> parser=<版本>`（`off`＝绕过态；`parse`＝未命中后重新解析并写格）；`--fallback` 与默认表缺失（无表姿态）时不打这行（无表读取，也不落格）。候选队列是写路径，永不进缓存键也不被缓存；缓存损坏或键对值坏＝通报后 miss 重解析，不放行坏值。

## 5 事后校验（T-01）口径

| 情形 | 动作 |
|---|---|
| 同义家族出现两个及以上不同形（`report: true`） | 报 warning，消息给全部出现形与次数，建议统一到主名 |
| 同义家族只出现一个形，或只出现英文／缩写 | 不报 |
| 上下位（上级词与下级词并现、或只有一方） | 不报 |
| 易混对同篇并现 | 报 info，提示人工确认；两词互不为子串才判（`心梗` 落在 `心肌梗死` 内不算并现） |
| 正字变体 | 静默归一，不报 |

词表缺失或不可解析时按无表处理（T-01 不报），宁漏勿噪；不给任何内置关系。

## 6 生长循环（运行中新增与更新）

七处触发只检出候选、不合并；候选一律先进候选队列，任务内正表冻结。冻结只锁 `references/lexicon/term-lexicon.yaml` 真源，`term-lexicon.candidates.yaml` 队列仍可写、必须写；`--log` 报**未命中**即当场 `--append`（含 P1 快线），命中无边不需落队；筛查日志文字行不算已检出，以队列条目为准；`--append` 后回读队列确认落点，不只信 stdout 路径回显。

- 确定性检查：`uv run python scripts/check_growth_loop.py <交付目录> [--json]`——按本节契约比对 `agent/screening-log.md` 的「术语词表引用与候选」表（查询词／候选词）与真源、队列：**声明为未命中／候选，但既不在真源也不在队列，或结构不可解析（文件／节／行缺失）即红**（退出码 1，不能验证即不判绿）；**本交付声明且已落库的术语，无边又未声明 `no_edge` → 机械生长，逐条判红**（单成员家族同样管；逐条口径，ADR-0022 修订）。每任务收尾必跑；端到端门禁 A-08 调用本检查（`evals/acceptance/run_gate.py`）；改过 `term_expand.py` 后另跑机制自检 `check_growth_loop.py --selftest`。

| 节点 | 谁检出 | 信号 | 记什么 |
|---|---|---|---|
| Step1 关键词表 | 执行 agent | 词表未命中（新中文主题词／商品名／别名） | 未命中词 + 上下文 + `term_expand.py --log` 的未命中说明 |
| B1 中文补充 | 解析步骤 | 题录词未命中词表；归一碰撞（标题键同但术语形不同） | 题录词 + 题录键 + 撞键条目 |
| Step2 去重 | 去重步骤 | 中文键疑似态（标题等作者等、年份一方 ∅）伴随术语差异 | 疑似对 + 三段键 + 术语差异 |
| 引文扩展轮 | 执行脚本 | 引文邻域题录检出词表范畴词（药物名／研发代号／疾病名／别名） | 新词 + `run/b1/citation-expansion.json` 读数 + `--append --source "引文扩展轮"`（票 02 落字；试验缩写与指南名不落缓冲） |
| 追加轮（迭代补漏） | 执行脚本＋agent 读终池／波内题录 | 波内题录检出词表范畴词（药物名／研发代号／疾病名／别名） | 新词 + `run/b1/gap-fill.json` 的 `lexicon_appends` 行（未命中才 `--append --source "追加轮"`；判不准按 AFK 提案口径 `--no-edge`）；**试验缩写不落缓冲**，记盘点表与机制读数 `detected_acronyms[]` |
| Step5 T-01 | 确定性脚本 | warning（同义混用）／info（易混提示）即信号 | 脚本行号 + 规则 + 消息 |
| 用户纠正 | 任意时刻 | 用户原话最高优先 | 用户原话原文（`source` 栏记原话） |

### 6.1 五步轻量循环（每任务，grill-with-docs 式）

与 domain-modeling 的唯一差异：不碰仓库 `CONTEXT.md`，只写本 skill 内词表。

| 步 | 输入 | 做什么 | 终止条件 |
|---|---|---|---|
| 1 对表挑战 | 候选 + 上下文 | 对照真源四分逐一过（同义？上下位？易混？正字？还是新家族？），出初判边当场 `--append --edge` 落队，或标「新家族」 | 初判已落队（含边与方向），或已标新家族 |
| 2 精确化提问 | 初判边 | 用 ask 向用户确认主名／英文／缩写／关系方向（问法模板见 §6.3，每家族每任务最多一问，多候选同家族合并一问） | 用户拍板，或明确 defer（defer → 留候选，下次再问） |
| 3 场景探针 | 已拍板边 | 编边界场景反问（「查 X 要不要带出 Y？」「Z 算下位吗？」），用 `term_expand.py` 的 `--log` 跑一遍看是否自洽 | 方向经得起探针，或改边重回第 2 步（最多一轮，沿 HITL 追问只一次） |
| 4 交叉核对 | 待写边 | 核三处：归一化碰撞（`norm()` 下是否撞已有键）、与真源既有家族／易混对是否冲突、标准术语（MeSH／指南用名）是否一致 | 无矛盾，或矛盾已解（以标准术语为准并记来源） |
| 5 一次成型落盘 | 已核边 | 晋升一次写：真源 + 版本 + 队列出账，不攒批（patch 静默、minor 先问） | 版本已 bump、队列已出账、任务 log 已引版本 |

### 6.2 写盘、版本与晋升（patch／minor）

- 任务内正表冻结：候选走 `term_expand.py --append` 落 `references/lexicon/term-lexicon.candidates.yaml`，任务结束一次晋升；晋升过的候选才进真源，任务中途绝不改真源。
- 候选字段：`term`／`family`／`canonical`（非本形时；同义与变体形折向主名）／`english`（英文形；确证才填、不猜补）／`no_edge`（声明无边；AFK 下可作提案，晋升计划表带表确认）／`edges[]`（`type`／`target`／`direction`；＝真源镜像边与 `--edge` 初判边的并集）／`source`（任务与日志行）／`version_candidate`（patch／minor）／`no_edge: true`（显式无边声明）。
- 晋升分两种：**新增**（真源无此词 → 写新术语块）与**并入**（真源已有此词 → 新边并进原块，原字段与 `source` 保留、晋升来源追加）；`--dry-run` 计划表逐条标 `[新增]`／`[并入]`；队列出账＝被晋升条目＋**无增量**的陈旧条目。
- 对称边两侧各记一条（如 `氨氯地平 ⊂ 钙通道阻滞剂` 与 `钙通道阻滞剂 ⊃ 氨氯地平`）；扩展只需一侧，表观一致要两侧。
- `--append` 幂等（ADR-0016）：以 **term 为键**合并——`edges` 取并集、`family` 以新值覆盖、`source` 追加来源轨迹。多点内联下同一词重复检出是常态，非幂等会写出重复块。
- 划分：**patch** = 正字变体（连字符／大小写／错字）与纯正字层级的别名写法增补——静默进（版本 `vX.Y+1`）；**minor** = 新家族、新关系边、关系改类、任何改变检索面的同义词／上下位／易混增补与别名增补——必须过第 2 步确认（版本 `vX+1.0`）。判不准一律按 minor 走确认（宁确认勿静默）。
- 晋升动作（脚本化，ADR-0016／ADR-0022）：`--promote <词>`（可重复）或 `--promote-all-candidates`——**先 `--dry-run` 出计划表，带表问用户那一问**（每家族一问，先算表后问，不额外增加确认轮次），确认后一次成型写：真源术语块／同义集、`version` 与 `frozen_at`（minor → `vX+1.0`，patch → `vX.Y+1`）、队列出账（被晋升条目移出）；**条目已在真源者跳过并出账**（空跑会是无变更的静默版本 bump，故不允许）。晋升后对**本任务交付件**重跑 T-01 回归；异常则不入晋升。
- 每任务引用版本：筛选日志与检索策略模板各记一行版本号；版本随 skill 冻结，任务中途不换。

### 6.3 问法与预算（每家族每任务最多一问）

问法模板（中文，选项给推荐位）：

1. 同义／别名：「`<候选词>` 与 `<主名>` 是同一概念吗？① 是同义，主名用 `<主名>`（推荐）② 是同义，主名用 `<候选词>` ③ 是上下位（谁包含谁？）④ 是易混，永不自动扩展」
2. 上下位／易混：「查 `<甲>` 要不要自动带出 `<乙>`？① 要（甲 ⊃ 乙，双向扩展）② 不要，属易混（永不扩展，出现只给 info）（推荐）③ 要，但方向相反（乙 ⊃ 甲）」

- 预算：每家族每任务最多一问；同一家族多个候选合并成一问；第 3 步探针要改边时沿用这一问，不新开一轮。该问与 HITL 入口两问、规模确认与两处断点预算独立，不为省问而跳过。
- AFK（人不在）：只记候选、不晋升（下次有人任务再跑循环晋升，断点记筛选日志）；判不出边的词可记 `--no-edge` 作**提案**——收尾计划表带表逐条确认时由人拍板（no_edge 行标「待你确认」）。
- 静默路径：只正字变体（含大小写／连字符／错字的别名写法）属 patch，静默进真源；其余一律 minor 走确认（每家族每任务一问），与既有边冲突（同形已挂别的关系）时也算 minor。
