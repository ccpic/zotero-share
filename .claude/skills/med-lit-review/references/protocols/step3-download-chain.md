# Step3 下载链（队列层与调用侧纪律）

本文件是 Step3 下载链执行做法的唯一家：队列层脚本怎么调、跨交付取件链（形态 B 复用）怎么跑、调用侧纪律七条、产出物位置。

**口径句（ADR-0032）**：`agent/download-queue.txt`＝研读选取集＝入库集＝研读对象——成员一次选定、三处同用；机器账＝`run/b1/selection.json`（产出表见 `step2-dedupe-and-snapshot.md` §1）。本文件其余各处的「队列」一律读作研读选取集。

规则真源不在这里——错误类二分、TTL 与显式绕过见 `workspace-guide.md` §14.1／§14.2，台账十列与口径列语义见 §7，复用边界与指针字段见 §14.3／§14.5，交互点二见 `hitl-protocol.md`；本文件只写做法与指针。

## 1 队列层脚本

`uv run python .claude/skills/med-lit-review/scripts/step3_queue.py <动词> --delivery workspace/YYYY-MM-DD-<slug> …`

| 动词 | 输入 | 输出 | 何时用 |
|---|---|---|---|
| `plan` | 研读选取集件 `agent/download-queue.txt`＋失败记忆（`--carry` 可在先交付的记忆） | `run/step3-queue-pruned.txt`＋剪队列动作进 `run/step3-queue-log.jsonl`，并打印批量命令 | 每轮下载前（含重跑）；`--force`＝用户显式重抓，绕过否定缓存 |
| `collect` | 上游收据 `run/batches/**/*.json`（`results`／`entries`／行列表三种形态都认） | `run/fulltext/<DOI-slug>.pdf`＋`run/download-results.json`＋失败记忆写入／清账 | 每轮批量下载后 |
| `retry` | 上轮收据（默认 `run/download-results.json`，无则扫 `run/batches`）或 `--dois` 显式条目 | `run/step3-queue-retry.txt`＋续跑或恢复命令 | 续跑与错文错源恢复；无全量重提入口 |
| `probe-guard` | `--kind`（默认 `channel_status`） | `run/step3-session-probes.json`＋日志 | 下载前探渠道状态；同会话（同日）第二次打印 SKIP，登录后 `--reset` 显式放行一次 |

- 口径默认读交付 `agent/manifest.json` 的 `caliber.grey_gate_version`，缺省为现行 `post-0006-fastest`；`--strategy` 默认 `fastest`。
- 失败记忆键＝归一 DOI＋错误类＋口径版本，同键下按 strategy 收敛（显式 strategy 天然 miss，与上游同目录索引的策略匹配口径一致）；到期基准＝该 DOI 收据落盘时刻（上游收据无逐条时间戳，取该 DOI 各收据 mtime 的最早读数）。
- 跨交付携带（`--carry <在先交付>/run/step3-failure-memory.json`）属缓存携带、非形态 B 取件链：无字节件、不进 `agent/manifest.json` 指针段；只读、悬空即忽略、不补写、不反向登记；窗口仍自原始失败时刻起算。
- `--now`（`YYYY-MM-DD` 或 ISO 8601）是探针时钟钩子，正常执行不传。
- 指令纪律：脚本只读写本交付目录；不直写上游磁盘缓存、不改上游键（上游机制见路线图票 04 资产）。

## 2 调用侧纪律（七条）

1. **搬运只认返回的文件定位字段**：成功件一律按批量／单篇返回的 `file` 字段定位并搬进 `run/fulltext/`（`collect` 已实现）；`--output` 只当暂存（如 `run/batches/out/` 或按块各自 `run/batches/out-*/`），不得假设其中已有件——上游命中不搬运是既有行为。
2. **重试只走剪裁队列或上轮收据**：续跑用 `retry`（源＝上轮收据／显式 DOI）或 `plan` 后的剪后队列（行序＝S 序，`retry` 的默认排序依据即此位次，非覆盖位次）；禁把 `agent/download-queue.txt` 原样重提（零收益重试的主因）。
3. **错文错源恢复优先换显式 strategy**：`retry --dois <条目> --strategy <显式值>` 逐条走单篇（上游 batch 无 strategy 位，批量表达不了显式策略；显式策略天然 miss，免清缓存）；确需清理才动上游三件套（`cache_clear`＋删错文件＋清索引条目），并在运行日志记明原因。
4. **渠道状态每会话最多一次**：先用 `probe-guard --kind <kind>` 判 PROBE／SKIP 再探；来源健康交由上游自适应排序与冷却，repo 侧不建健康缓存；登录后必须验通道再下载，用 `--reset` 显式放行并留痕。
5. **批量保持默认车道、机构车道串行**：批量命令不加 `--no-lanes`、不加 `--scihub`（默认路径即四车道调度）；灰导向 strategy（`scihub_only`／`grey_only`／`scihub_first`）逐条竞速，与默认车道不混用；机构车道串行（上游 `institutional_workers=1`）不改。
6. **不静默改设置**：不因下载失败自动改访问配置、不自动装浏览器后端；配置缺口按渠道状态与 `agent_hint` 记断点，去向进局限声明（灰色与 Tor 属默认策略，不在此列，按 ADR-0006 默认启用）。
7. **下载前先跑入库预检**：每轮批量下载前对研读选取集 DOI 跑 use-zotero 的入库预检（`check_duplicates.py`；预检条数＝队列条目数＝研读选取集条目数；行格式与口径见其 `references/local-api.md`，决策见 ADR-0020／ADR-0032）——`DUP` 剔出本轮下载（台账未采用原因列记「在库重复（入库预检）」）；`DUP-GAP` 保留下载，预检 key 供 Step4 挂载指向既有条目；预检 fail-closed 时中止本轮。

## 3 产出物位置（队列件＝研读选取集，是交付合同件，其余都在 `run/` 忽略缓存内）

| 件 | 位置 | 说明 |
|---|---|---|
| 队列＝研读选取集（交付合同件） | `agent/download-queue.txt` | Step2 选取步（`step2_select.py apply`）产出，一行一条 DOI（首行为口径句）；＝入库集＝研读对象（三集合一，ADR-0032）；剪队列不改写本件 |
| 剪后队列 | `run/step3-queue-pruned.txt` | 本轮实际下载输入（含头部口径／strategy／剪裁计数） |
| 重试队列 | `run/step3-queue-retry.txt` | 只含上轮未采用或显式指定条目 |
| 收据 | `run/download-results.json` | 逐篇实际来源、采用判定、失败类、文件 basename 指针；`identity_check` 与 `run/fulltext-identity.json` 同源（按 DOI 回填该核验步的 verdict，未核验留空）；门禁 C-01～C-03／C-07 读本件 |
| 失败记忆 | `run/step3-failure-memory.json` | 否定缓存；只记全通道穷尽型确定性失败，件到手即清账，剪队列不写回本件 |
| 运行日志 | `run/step3-queue-log.jsonl` | 剪队列／写入记忆／清账／显式绕过／重试／探针动作逐条留痕 |
| 全文目录 | `run/fulltext/<DOI-slug>.pdf` | 命名沿 `workspace-guide.md` §4 |
| 上游收据 | `run/batches/**` | 批量暂存与逐块收据（`batch_results*.json` 等） |

## 4 台账与计数的衔接

- **队列条目数守恒**：下载台账一行一条、条目数＝队列条目数＝研读选取集条目数（三集合一，ADR-0032；四桶计数之和与之一致）；剪队列不改写 `agent/download-queue.txt`。剪掉的条目在台账「未采用原因」列逐条记 `失败记忆剪队列（<错误类>；原始失败 <YYYY-MM-DD>）`（逐条读数取 `run/step3-queue-log.jsonl` 的 `pruned` 行）。
- **未采用行照常按本次实际读数填写**，不因剪队列或复用变形（列语义见 §7，不复述）。
- **交互点二触发不变**：剪队列条目属「上一轮已确证没拿到」，不改变触发条件本身；含灰色在内全通道仍没拿到才问（`hitl-protocol.md`），剪队列动作只进运行日志与台账。

## 5 跨交付复用取件链（形态 B）

`uv run python .claude/skills/med-lit-review/scripts/reuse_pickup.py pick --delivery workspace/YYYY-MM-DD-<slug> [--queue <队列>（默认 agent/download-queue.txt＝研读选取集）] [--doi <DOI>]… [--sources <在先交付目录>]… [--expect <期望值 JSON>] [--force] [--dry-run] [--now <时钟钩子>]`

每轮下载前跑（同 topic 重跑与跨 topic 一视同仁）：研读选取集条目（＝队列件行集）先行取件，命中即不进下载链，miss 照常进（队列与本件都不改写）。

| 环节 | 做法 |
|---|---|
| 定位 | 扫消费方同级目录（`ls workspace/` 即全局视图，不建索引、不建中心目录）＋显式 `--sources`；单个在先交付内先认其清单的 `reuse_pointers`（新形态 `agent/manifest.json`、历史包在交付根）（带 `sha256`），再认其下载台账（`agent/bucketing-and-sources.md`）按 DOI 取行的已采用件（文件列非空、未采用原因留空、身份读数判决为「通过」；禁复用位——隔离件 `run/quarantine/`、监管留档 `run/b2/`、抽取文本 `run/text/`——一律不入链） |
| 源哈希 | 源件 `sha256` 全等校验：指针在位时须等于指针的 `sha256`（不等＝miss，不改指针）；指针不在位时以本次读数为钉子写进指针 |
| 物化 | 复制进消费方 `run/fulltext/<DOI-slug>.pdf`；同名件字节相同＝不重写（noop 命中），字节不同＝miss（不覆盖消费方已有件） |
| 副本复验 | 物化后重算副本 `sha256`；不符即清残副本＝miss |
| 身份核验 | `--expect`（归一键→题名／作者／卷期 pins＋方法，消费方自己声明）与在先交付身份收据（`run/fulltext-identity.json`）逐项全等 → 复用原「通过」并沿用原 `checked_at`；收据条目按归一 DOI 全等认领（同名 basename 不作凭据），字节 sha256 项由取件链自身两处校验承担、收据另记 `sha256` 时须全等；`--expect` 缺省、任一缺项或不等 → 记本地重跑（该件照常物化，由本轮身份核验步重跑）；在先判决为拦截态（`False`／`mismatch` 一类）不入链（miss `identity_not_pass`），未判态（`None`／缺读数）照常物化并转本地重跑——两者都不判通过 |
| 登记 | 命中写 `agent/manifest.json`：`reuse_pointers[DOI]` 十字段（`workspace-guide.md` §14.5）＋`regeneration` 的 `fulltext_reuse` 条目（命令＝本次 `pick` 调用，含显式 `--sources`；哈希＝同 `sha256`）；写清单按原件换行与缩进最小差异追加（不整体重排）；miss 零写（队列不改写、旧交付不补写、指针不自动改） |
| 收据 | `run/reuse-receipt.json`：逐条模式（`pickup`／`replay`）、`hit`／`miss` 与原因码、`source_sha256` 与 `copy_sha256` 两处独立读数、身份决策（`reuse`／`rerun`／`carried`）与台账六格；属运行记录（非再生命令件）——交付收尾按该交付既有收据口径登记其时点哈希，链不为其造再生命令 |
| 再跑 | 擦除 `run/` 后用 `fulltext_reuse` 条目的命令重放（指针在位＝replay；删件／改字节／删交付＝miss，如实落收据）；`--force`＝显式重抓，绕过指针重新定位但绝不改写既有指针 |

- 原因码：`no_candidate`｜`artifact_missing`｜`source_missing`｜`pointer_invalid`｜`identity_not_pass`｜`blocked_artifact`｜`hash_mismatch_source`｜`hash_mismatch_copy`｜`materialize_failed`｜`target_conflict`｜`pointer_conflict`——全部落常规下载链路（含上游近窗缓存），miss 不降正确性。
- 台账复用行六格取收据的 `ledger`（`DOI｜实际来源｜采用｜grey_hit｜身份核验｜口径版本｜文件`）：实际来源／grey_hit／身份核验为原读数逐字沿用，口径版本＝本次交付口径，采用＝`是（复用自 <旧交付目录名>）`（跨口径附注原口径），文件＝消费方运行缓存内 basename；桶／预计来源／未采用原因按本次交付填（行内编码见 `workspace-guide.md` §14.4）。
- 纪律：旧交付全程只读（不写、不反向登记）；只读写消费方交付内文件；指针、收据与台账格只写 basename 与相对路径；抽取文本凭复用后的 PDF 再生，不从在先交付取（边界见 `workspace-guide.md` §14.3）。

## 6 长批次编排与中断恢复（作业时限・断点检查点・收据重建）

本节做法自「大批量分片下载被作业时限中断、收据缺失」的实跑归纳；上位机制见 `workspace-guide.md` §14（缓存位与失效）与 §7（台账列语义），本文件只写做法。

1. **长批次必须以无作业期限的方式启动**：单片期望时长 ≥10 分钟的批处理（大批量分片下载）要显式无期限（会话作业工具给 `timeout=0`／不设 deadline；默认时限常在 ~1 小时静默中断批次）。中断的批**不会写 `batch_results.json`**——`collect` 无收据可认，已落盘的全文本轮不可采纳。编排形态：分片（按队列切分，单片条目数与其期望时长匹配）＋每片一个**监督环**（检测该片进程死亡后无期限重启；并发 3–6 片，不叠加更多）。
2. **重跑即断点续跑（跳过已完成）**：上游 `batch_id = md5(sorted(规范化标识))[:12]`，断点检查点在 `~/.scansci-pdf/batch_progress/<batch_id>.jsonl`，逐条追加且行 schema 与收据一致（`identifier／doi／success／source／file`）；重跑同一切片输入自动跳过检查点内成功条目（日志的 `skipped_completed`），自然完成时写 `batch_results.json` 并删除该检查点。故「重跑」只重试未完成项，不重复全量。
3. **中断批的收据重建（投影，不新造读数）**：批次已中断且无 `batch_results.json` 时，从检查点按片投影 `run/batches/out-*/batch_results.reconstructed.json`（同 schema，逐行带 `_reconstructed` 标记与 `batch_ids` 出处），在台账／收据里如实标注「检查点投影」；`collect` 照常读（`run/batches/**/*.json`）。投影不给条目编造来源——来源只在第 4 条可证时才填。
4. **快车道取回件的来源补读**（示例口径）：快车道（OA／出版商直取）取回件不进逐条检查点，其来源依次从：① `.doi_index.json`（工具自写 `DOI→{file,source}`）；② 文件名来源后缀（`_LibGen`／`_OpenAIRE`／`_CORE`／`_CrossrefPage`／`_OpenAlexOA`／`_Sci-Hub` 等）；③ 正文 DOI／题名前缀（∩ 本交付池）归因；三者皆无记 `unrecorded`（如实记「批中断，来源未回执」，不冒充来源）。收据扩展行同样带 `_reconstructed`／`_source_basis` 标记。
5. **脚本**：`uv run --isolated --no-project --with pymupdf python .claude/skills/med-lit-review/scripts/step3_recover_receipts.py --delivery workspace/YYYY-MM-DD-<slug> [--progress-dir ~/.scansci-pdf/batch_progress] [--write]`——按片日志取 `batch_id`、投影检查点、补读快车道来源，输出 `batch_results.reconstructed.json`；`--write` 才落盘。
