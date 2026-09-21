# reading — 检索与附件正文（基本流程）

- 分类：`collections()`；条目：`items()` / `top()` / `collection_items()`；
  全文检索：`items(q=...)`。
- 子附件：`children(parentKey)`；附件正文走 MCP 的 `zotero_item_fulltext`
  或 pyzotero `fulltext_item()`。
- 刚启用本地 API 就读到空数组，先重启 Zotero 再判空库。
- 技术栈见 SKILL.md 选型：只读检索优先 `zotero-mcp` 的三个工具，
  批量/程序化读走 pyzotero。

## 读原文与报数约定

- 本地附件优先（default recommendation）。
  When: 用户要读某条目原文/PDF 时。
  Do: 先 `children(parentKey)` 查已有 PDF 附件，有则直接给本地路径，不走下载链；仅无附件时才按 placement 全文链下载。
  Why: 已有附件仍重下浪费且可能超时失败。
- 报数口径（default recommendation）。
  When: 回答“库里有几篇某主题文献”时。
  Do: 只数正式条目类型，排除 attachment/annotation/note，并注明口径。
  Why: `qmode=everything` 原始命中数含附件与标注，会虚高。
- 同义词扩展（default recommendation）。
  When: 用中文主题词检索时。
  Do: 同步扩英文通用名、商品名、中文别名分别检索，按 DOI 去重后报数。
  Why: 单中文词 0 命中不等于缺失，易漏检。
- 被引数比较（default recommendation）。
  When: 比较几篇文献引用高低时。
  Do: 一次 `GET /works?filter=doi:a|b|c`（≤50/页）+ `select` 最小字段批量取 `cited_by_count`，返回 DOI 小写比对，并注明查询日期与来源；不用库内元数据或记忆值作答。
  Why: Zotero 条目本身不存被引数，记忆值易过期；一次批量实查可比且免逐篇循环。
- 参考文献取数 Crossref 优先（default recommendation）。
  When: 已知论文 DOI，需列其参考文献时。
  Do: 先读 Crossref `/works/{doi}` 的 `reference[].DOI`，再用 OpenAlex 批量 resolve 标题/作者/被引数；仅无 DOI 或 API 无覆盖时才回退 PDF 坐标解析。
  Why: 出版社 deposition 结构化不断裂；双栏等版式 PDF 文本提取易串行。
- 参考文献对照表脚本（default recommendation）。
  When: 要出参考文献对照表（标题/作者/被引数/是否在库）时。
  Do: 跑 `scripts/refs_report.py --doi <DOI>`，默认按 `cited_by_count` 降序；`--sort orig` 保持 Crossref 原序，`--top N` 取前 N 篇。
  Why: 取数→比对→排序闭环可复用；在库判定只认 DOI 字段（大小写无关），附件文本命中不算收录。
- OpenAlex 降级回退（default recommendation）。
  When: OpenAlex 标题/作者缺失，或来源/年份明显降级（如机构仓储代实际期刊、作者名混入 ORCID）时。
  Do: 按 DOI 回查 Crossref deposition 补标题/作者/期刊/年份，来源优先取 locations 中 `type=journal` 者，作者名 strip ORCID。
  Why: 部分老记录 `authorships` 为空，`primary_location` 可能指向仓储；Crossref 是权威来源。
- 外部标识符入库前先核验存在性：标题关键词 + `qmode=everything` 检索库内，
  比对命中条目的 DOI 字段（大小写差异忽略）；片段式标识符查询 0 命中不等于
  缺失，不得直接建条目。
