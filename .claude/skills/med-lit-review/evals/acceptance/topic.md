# 固定选题与验收钉子（acceptance topic）

本文件是端到端验收跑（质量门禁）的唯一固定输入：任何一次验收跑（含隔离盲测）都用这一份选题与这些钉子，跑完由 `run_gate.py` 跑断言并**产出** `assertion-table.md`（逐条读数与判定；非本目录的静态输入件）。选题只作路由与断言用；案例值不断言任何效应数值（沿 02／03 案例纪律）。

## 选题

- 研究问题：PCSK9 抑制剂（PCSK9 inhibitor）用于 ASCVD（动脉粥样硬化性心血管疾病，Atherosclerotic Cardiovascular Disease，ASCVD）。
- 范围要素（输入材料，供拟稿参考）：人群＝ASCVD 成人（含他汀不耐受者）；干预＝PCSK9 抑制剂（evolocumab／alirocumab 类单抗语义，inclisiran 另述）；对照＝他汀（statin）± 依折麦布（ezetimibe）；结局＝MACE（主要不良心血管事件，major adverse cardiovascular event，MACE）组分与 LDL-C（低密度脂蛋白胆固醇，low-density lipoprotein cholesterol，LDL-C）达标。
- 中英关键词表：PCSK9 inhibitor／PCSK9 抑制剂、evolocumab／依洛尤单抗、alirocumab／阿利西尤单抗、ezetimibe／依折麦布、ASCVD／动脉粥样硬化性心血管疾病、guideline／指南。

## 双窗与来源

- 全窗（不限日期）与近两年窗（2024-09-16 至 2026-09-16，含端点）各跑一遍；未知日期保持未知（不推断补全）。
- 来源：PubMed（美国国立医学图书馆生物医学文献库）＋ OpenAlex（开放学术图谱）双源；Crossref（数字出版元数据注册机构）逐条核身份；中文补充走 B1（用户在三库执行、执行方解析汇入），监管走 B2（官方直取）。
- 检索式（逐条、含窗口与命中数）：以验收跑的检索策略文件为准；六族检索式冻结如下（扩窗与放宽只在零命中时走，且记日志）。

| 族 | 检索式原文 |
|---|---|
| 指南 | PCSK9 inhibitor ASCVD guideline |
| 关键试验 | PCSK9 inhibitor cardiovascular outcomes randomized controlled trial |
| 系统综合 | PCSK9 inhibitor systematic review meta-analysis cardiovascular outcomes |
| 观察性 | PCSK9 inhibitor real-world cohort observational study outcomes |
| 剂量探索 | PCSK9 inhibitor dose-ranging phase 2 LDL cholesterol |
| 病例对照 | PCSK9 inhibitor case-control study |

## 五类路由覆盖（一题全齐）

| 路由 | 本题命中 | 进哪步 |
|---|---|---|
| 指南现行版 | BMJ 2022 快速推荐、2023 中国血脂管理指南（英文版／中文版）、2023 AHA 慢性冠心病指南（仅摘要） | Step1 → Step5（现行版时效豁免） |
| 关键试验（III 期 pivotal） | FOURIER、ODYSSEY OUTCOMES、VESALIUS-CV（近两年窗） | Step1 → Step5（RoB 2） |
| 系统综合（SR/MA） | BMJ 2022 网络荟萃、BMC 2015 荟萃（降档）、Cureus 2023 系统综述（降档） | Step1 → Step5（AMSTAR-2） |
| 中文条目 | 2026 版卒中二级预防专家共识、中国血脂管理指南中文版、中文观察性与仅摘要条目（CNKI／万方／维普按词表手动补） | Step1 后 Step2 前汇入（同设计同权，中英分别计数） |
| 监管文件 | FDA 说明书（REPATHA，SPL 版本 32）＋ EMA 产品特征概要（修订号 28）；NMPA 未见官方原文转待核对 | B2 直入 Step4（限规范性断言） |

## 固定钉子（跑前冻结，跑后写进可复现包）

| 钉子 | 值 |
|---|---|
| 参考年份 | `--cur-year 2026`（传给 `scripts/review_knowledge.py`，保证 future-year 规则可复现） |
| 词表版本 | 与 `references/lexicon/term-lexicon.yaml` 的 `version` 一致（读取值随生长循环晋升推进，**不钉具体号**；每任务在筛选日志与检索策略里引用） |
| 排序权重版本 | `sort-weights-v2`（相关 0.4／时效 0.1／影响 0.2／完整 0.2／中国证据 0.1，初值冻结、任务内不调参） |
| 引文制式 | GB/T 7714-2015 单制式；著者前 3 位加「等」；未核对标 `[待核对]` 且不进可追溯列 |
| 下载默认策略 | 竞速（`fastest`）含灰色源（Sci-Hub 等）与 Tor 路线，默认启用；无人值守同样启用（AFK 照走灰色） |
| 校验退出语义 | `scripts/review_knowledge.py`：0 无 error／1 有 error／2 用法或文件错误；warning 不阻断 |
| 锚点清单 | `anchors.json`（version `anchors-v2`；每条锚点的包含、获取与锁定口径见该文件 `rule` 段） |
| 锚点锁定 | 窗口内可复用：pins 全量全等且锁龄不超过 168 小时时复用原 valid 结论（门禁输出记「复用（锁龄 Xh）＋原锁定时刻」，锁龄自原锁定时刻算、不因复用顺延）；超窗或 pins 任一项不等即联网重核，漂移／缺失／未取到永不复用为通过；结果写 `anchor-lock.json` |

## 断言与容差口径

- 锚点＝包含断言：固定锚点标识必须在证据表与引文表命中（`P-02`），开放类锚点必须在下载收据中成功且来源如实（允许灰色，`C-06`）。
- 计数、排序、被引、命中数＝容差或顺序断言（下界／范围／相对顺序），不断言精确值（`A-07`、`B-05`、`B-08`、`P-08`）。
- 规模断言的**分量口径**（ADR-0023）：`A-01` 只数检索池（`pool_kind=检索`，缺省视为检索），引文扩展／追加轮池分列读数、不断言；`B-05` 只断言检索池分量（`dedupe-report.final_records[].origin=discovery` ∈ [200, 550]），扩展／追加分量分列读数、不断言——基线宽度保持告警意义，机制扩展不再顶穿上界。
- 来源漂移（provider 更新、被引增长、库版本翻页、文档改版）写漂移日志并如实记局限，不判假失败；锚点整体失效（注册记录消失、版本停更）才是硬失败。
- 每条断言的断言类别（包含／精确／容差／顺序／一致性）、断言对象、期望与容差由 `run_gate.py` 的断言表发布，结果与判定写 `assertion-table.md`。

- 仅题录须可证无摘要（`E-19`）为硬门禁（ADR-0023）：凡标 `仅题录` 者不得在可及性清单 `run/step5-accessibility.json` 的 `abstract_source`、落点报告「摘要」列或库内 `abstractNote` 上取到摘要；核心交叉核对离线可算，`--no-live` 只跳过库内加强项、不降级为未执行。

## 覆盖账与抽查 fixture（门禁面，ADR-0023）

- **覆盖账在场与字段齐**（硬门禁 `A-10`）：交付内 `run/b1/coverage-ledger.json` 必须在场，其 `mechanisms` 下九块齐（`citation_expansion`／`iterative_gap_fill`／`high_cited_absence`／`classic_segment`／`source_failure`／`cap_truncation`／`date_filter`／`source_declaration`／`selection`），逐块冻结名与 `pointer` 在场（**子集语义**：冻结名须在场、`note` 可选；字段集＝ADR-0023 ＋ ADR-0024／配套票 03 增补——`filtered`／`filtered_pointer` 与五个 `contributed_*`／`contribution_pointer`；**第九块＝ADR-0032 的「入库集守恒」**：块名由 `queue` 改 `selection`，「队列截断／`queue_cut_count`」口径整体退场，冻结名＝`selected_total`／`must_take_count`／`rank_fill_count`／`unselected_total`／`unselected_by_rank_band`／`unselected_by_origin`／`classic_segment_cut`，读数派生自 `run/b1/selection.json`、账不复制逐条，`pointer` 取 `selection.json`／`sort-report.json`／`dedupe-report.json` 中在场者）；逐行块（`channels`／`queries`／`unrecovered`／`cells`／`not_included`）再核 ADR 逐项列出的内层冻结名。产出命令与字段集见 `SKILL.md` Step2 行与 ADR-0023／ADR-0024／ADR-0032。
- **账上缺口⇒§8 分句且数字一致**（硬门禁 `A-11`）：账上缺口必须在知识包第八节「覆盖缺口」块有对应三段式分句（`机制｜读数｜缺口与补法`），十类机制逐机制一行、读数数字与账一致——类目＝源失败补偿／命中上限披露／无日期候选／越窗候选／引文扩展轮／迭代补漏／高被引缺席核对／经典段／未启用源／**未选取**（第 10 类，ADR-0032：`未选取 {N} 条（终池 {X} − 研读选取 {Y}）`，位次分档与来源池分列）。`scripts/coverage_ledger.py` 末尾按账打印该块，照抄即合规（数字以账为准）。
- **抽查轨 1 的 fixture 场景集**（软项 `A-12` 已知必中核出／`A-13` 经典·高被引核出）：分两个文件面，路径与字段为门禁契约——
  - **场景定义**：`evals/coverage/<场景 id>.json`，每场景一文件（`id` 须与文件名一致）。字段：`topic`（题材，人读）｜`kind`（`known-hit`｜`classic`｜`high-cited`，决定进 `A-12` 还是 `A-13`）｜`expect_shape`（`核出`｜`取回`）｜`applies_to[]`（适用交付的 token 表）｜`known_hits[]{id,doi?,pmid?,openalex?,label}`（已知必中清单，逐条断言核出）｜`baseline`（基线读数，人读不断言）。
  - **核出读数**：`run/b1/coverage-check.json`（`run/` 忽略位，核出器产出）。字段：`checked_at`｜`scenarios[]{id,expect_shape,found[]{id,where,evidence},missing[]{id,reason},pointer?,note?}`。
  - **启用与适用**：抽查轨 1 只在**显式**传 `--coverage-fixtures` 时启用（不传＝不适用，固定选题验收跑即此路，机制 fixture 不改写其判定）；启用后逐场景判适用——`applies_to` 任一 token 命中**交付目录名**即适用，只认交付目录名、不扫包内文本（固定选题包的研究问题节含多题材词，按文本匹配会把机制 fixture 拖进固定选题跑）；适用场景未记录核出读数、或已知必中条目未进 `found`，即软项缺口（须进局限声明）。场景文件与核出器归发现层执行票 08 落地；token 取交付 slug 的辨识词（如 `ldl-c`／`myelofibrosis`），不得取会把固定选题包（PCSK9／ASCVD）圈进来的词。
- fixture 场景集**不进**固定选题验收跑的选题集（该跑只跑 PCSK9／ASCVD）；场景对某交付不适用时记「不适用」，不阻断。

## 交付必跑三件套（交付期口径）

交付期必跑三件，读数进 `agent/gate-summary.md` 的 verdict 头判定行（§11）：

| # | 执行体 | 对象 | 退出码 |
|---|---|---|---|
| ① | `uv run python scripts/review_knowledge.py <包.md> --cur-year YYYY` | `agent/knowledge-package.md`（脚本零改动，调用侧指新落点） | 0／1／2 |
| ② | `uv run python scripts/check_growth_loop.py <交付目录>` | `agent/screening-log.md` 的「术语词表引用与候选」节 | 0／1／2 |
| ③ | `uv run python scripts/check_delivery.py --delivery <交付目录>` | 交付形态契约（W 组，零网络零锚点） | 0／1／2 |

## 人读主件与交付形态检查面（W 组）

执行体＝`scripts/check_delivery.py`（零网络零锚点；退出码同族同形：0 过／1 硬门禁未过／2 用法或输入缺失）：硬门禁判「内容／契约」类形态、软项只记读数并告警不阻断——逐条门与档位映射的**唯一真源是 `references/protocols/review-rules.md` §6**，形态口径见 `references/protocols/workspace-guide.md` §9–§11，本文件不复述。

- 自证：`uv run python scripts/check_delivery.py --selftest`——每条硬断言一孪生场景（先造骨架合规的最小交付要求硬断言全过，再逐条变异要求该条未通过）。
- 扩面按票走：骨架类三条硬断言（02）与卡面解析健康 W-12／漂移抽跑（03）已在执行体；人读主件七条（W-01..W-05／W-07／W-08）与版内件机密面（W-13，在 `run_gate.py` 的 `secret_scan`，HTML 先归一化）随 04 号票挂进同一执行体；精读序三条（W-06：存在性／出处／域封闭，读 §5.1 真源）与精读序形态软项 W-20 随 05 号票挂进同一执行体（P1 与无卡域记「不适用」；不查排序正确性，理由见 `review-rules.md` §6）；入库集守恒 W-22 随 ADR-0032 挂进同一执行体（写库设计跳过的路线记「不适用」，判据沿 D-10 同式三级读法）。

## 及格线与独立核对

硬门禁全过（可追溯 100%、零阻断错误、占位清零或已披露、落点回查一致、机密零泄漏、实际来源如实、覆盖账在场与字段齐、账上缺口⇒§8 分句且数字一致）＋软项（可复现、覆盖率、分级准确、时效性、已知必中核出、经典／高被引核出）缺口全部写入局限声明即过；任一硬门禁不过或任一软项缺口未披露即不过。

- 独立核对 fail-closed：运行时刻的锚点现行性核对与本地库只读回查属于独立核对。默认（联网）模式下这两项必须执行；取不到（限流、超时、本地库不可达）即判不过，报告单列「未执行项（独立核对缺位）」，不做静默通过。锚点现行性在窗口内可复用原 valid 结论（`pins` 全等＋锁龄 ≤168 小时，不算缺位；超窗或 pins 不等即重核）。
- `--no-live` 是显式放弃独立核对的选择：结果目录里窗口内的原 valid 锁按复用承接（记「复用（锁龄 Xh）＋原锁定时刻」，不算缺位）；没有可用锁时这两项记未执行、不影响判定。归档里必须写明本轮未做独立核对，供复核者判断证据强度。
- 版本/文档改版这类现行性漂移只记漂移日志（数据在跑时刻仍成立），不判假失败；锚点注册记录整体缺失才算硬失败。

## 复跑

```bash
uv run python .claude/skills/med-lit-review/evals/acceptance/run_gate.py --run <验收跑目录> --out <结果目录>
uv run python .claude/skills/med-lit-review/scripts/check_delivery.py --delivery <交付目录>            # 交付形态（三件套之三）
```

- 验收跑目录按**新落点**读件（真源与账目件在 `agent/` 直下、`run/` 留交付根）：件不在 `agent/` 的包按「重排未接线」报出（退出码 2），不静默按旧位次读。
- `--no-live` 跳过联网核对（锚点锁定与在库回查记未执行，不判失败；结果目录里窗口内的原 valid 锁仍按复用承接）。
- `--baseline <上一次的 assertion-results.json>` 打开漂移对比（数值不同记漂移行，判定仍按容差）。
- `--selftest` 用变异副本验证门禁有牙（孪生对照口径：每场景先造只对齐该断言前提的孪生副本、要求目标断言在孪生上通过，再变异、要求其未通过；覆盖锚点缺失、容差内漂移、软缺口未披露、附件缺口未披露、本次合集缺口、生长循环未落缓冲、旧顺序节序、摘要声明与库内不一致、仅题录摘要可得（E-19）、覆盖账缺字段（A-10）、缺口未披露（A-11）、fixture 核出失败（A-12／A-13）、卡目录形态（0 卡／目录缺，E-04）、锁窗口内复用、锁龄超窗重核、pins 指纹不等重核、漂移／缺失／未取到不复用为通过）。基线行只如实报读数——历史包在新门禁下未过属预期（ADR-0008），故自检不依赖基线包口径新旧；`--run` 须指向**新落点**的验收跑目录（孪生夹具按 `agent/` 读写；旧落点目录会在「基线可构造性」前置检查上如实记红）。
- `--zotero-fixture <JSON>` 是**自检专用口**（只有 `--selftest` 内部会传）：把摘要断言（`D-11`）的本地库只读回查换成确定性夹具（`{"abstract_notes": {条目键: 摘要}}`），让孪生对照离线可跑；正常跑不传该参数，一律走真实本地库。
- `--coverage-fixtures [目录]` **显式启用**覆盖 fixture 场景目录（ADR-0023 抽查轨 1；不带值＝skill 内 `evals/coverage`）；**不传该参数即该轨不适用**（固定选题验收跑不受机制 fixture 影响）；`--selftest` 全程改指自检的合成场景目录，使自检读数不依赖真实场景件的内容。
- 锚点清单按 `run_gate.py` 同目录的 `anchors.json` 读：要按改过的钉子跑（自检／探针），须调用**该副本目录内**的 `run_gate.py`——`--skill` 只切 `references` 与 skill 内脚本，不换锚点清单。
