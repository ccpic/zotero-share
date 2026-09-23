# placement — 导入后归置、去重与落点报告

通用可执行落点唯一真源。词汇见根 `CONTEXT.md`；决策见 ADR-0002（批量确认）、ADR-0007（挂载）；医学实例化见 `med-lit-review` 内 `references/protocols/placement-guide.md`。

## 放置闭环（strong rule）

When: 任何批量建条目或补录正式条目完成后，条目 collections 为空。
Do: 按顺序收尾——建缺失分类 → 设 `collections` → 转挂附件 → 删旧快照 → 逐条回查并报告每篇落点。
Why: 不收尾则新条目静默堆积在未分类条目，用户感知为“导入丢了”；附件不迁直接删旧会丢文件。

1. 先读全库 `collections()` 按维度规则算出候选落点（2~4 个，首个为推荐位），一律经互动工具（AskUserQuestion / `ask`，`recommended` 指向推荐位）请用户确认一次后才写库；单命中也不静默写，不擅自新建。
2. 无合适收藏集时候选含新建位置（名 + 父级）与留未分类，用户确认才执行；选新建则建后放，选留未分类则不写 `collections`。
3. 快照类旧条目（webpage + 附件齐、无 DOI/作者/卷期）不算正式收录：先建正式条目，再把附件 `parentItem` 转挂过去，最后删旧条目。附件转挂前确认无子标注丢失风险。
4. 批量走 `scripts/place_imports.py`（先 `--dry-run` 看计划再写库）。
5. 收尾逐条回查 `collections`/`parentItem` 并报告每篇落点，不以写入返回为准。
6. 全文链（default recommendation）：归置完成后默认继续，无需另行确认——**任务已下载且验明身份的本地 PDF 直接挂载（`scripts/attach_pdfs.py`，先 `--dry-run`），不重抓**；全文链只补漏：取件前先跑**入库预检**（`scripts/check_duplicates.py`，口径见 `references/local-api.md`，决策见 ADR-0020）——`DUP` 已在库且完整：不再取件；`DUP-GAP` 已在库但缺正文附件：按预检返回的条目 key 取件并挂到既有条目（不新建）；`NEW` 照常。按 DOI 经 scansci-pdf 取缺失 PDF（单篇 `scansci_pdf_download`，≥3 篇 `scansci_pdf_batch_download`；无来源偏好时 `oa_first`），落盘验明身份后挂载。`strategy` 只定优先级，不保证实际来源，以返回 `source` 为准并如实报告（如 `oa_first` 实际返回 Sci-Hub）。每段可被用户显式跳过（只建条目不归置 / 只归置不挂载）；跳过不记为失败，报告中注明断点。
7. 落点报告收尾读摘要：摘要本身在写库时已随条目落位（写入口径与状态词见 `references/local-api.md`「摘要入库」，决策见 ADR-0021）；报告按「摘要」列逐条记状态、表尾出计数行——列取值与做法在编排层，见 `med-lit-review` 内 `references/templates/placement-report-template.md` 与 `references/protocols/placement-guide.md`，本文件不重复列定义。

## 维度规则（default recommendation）
When: 为一篇选目标收藏集时。
Do:
- 优先已存在架构；语义命中只定推荐位、不直接写，不限于单一维度（如作者/药品/疾病/试验名）。
- 分期/类型维度（如 III 期临床）可落主题目录、可落分期目录、可落二者嵌套，按已存在者取。
- 一篇可多归类并存（如主题 + 父级），不只取最具体的一个。
- 指南/共识进指南目录，不跟主题走；多主题荟萃/比较研究上卷到机制父级，不猜单一主题。
- 最终落点以用户一次确认为准；用户自定位置照办，仅位置不存在或又引入新歧义时才追问一次。
Why: 单维度硬套会把指南、荟萃、亚组塞错类；多归类利用 Zotero 原生能力，检索与浏览各取所需。

## 校验

- `collections()` 复核目标 key 与父级；新建后断言 `successful` 非空。
- 回查逐条打印 `key | title | collections | parent`，旧快照条目确认已消失。
- 读快照只省读：`--dry-run` 的收藏集视图与 placements 逐条当前归属存包内 `.cache/`（分钟级 TTL、写库即失效），紧接的写库轮命中即省重复读，命中要过写前库版本探针；确认门、写路径与逐条回查不因命中改变（`CACHE` 行记 source＝snapshot／snapshot+fetch／fetch 与命中读数或 miss 原因）。
- 回查按集合比对（strong rule）：`place_imports.py` 回查 `collections` 用集合相等判一致，不用列表 `==`——Zotero 返回顺序与写入顺序无关，顺序不同即误报 FAIL（本 session 实测 25 条误报，集合实际全一致）。
- 同名合集复用：`place_imports.py` 写库前按（集名，父集）查既有收藏集，命中直接复用 key，不再建（重跑脚本不再造出同名空集）。
- 改本文件任一规则时同步更新 `evals/evals.json` 并校验 JSON 可解析。
