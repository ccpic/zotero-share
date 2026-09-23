# local-api — 连通、授权与字段校验

导入/写入分支必读；只读分支只用连通自检一节。

## 连通自检

1. `curl http://127.0.0.1:23119/connector/ping` 期望 `Zotero is running`；
   不通表示桌面端没启动。
2. `Zotero('0','user',local=True).collections()` 能读到分类，
   空库返回 `[]` 属正常。
3. 刚在首选项 高级里勾选 Allow other applications on this computer to
   communicate with Zotero，必须完全重启一次 Zotero 才生效；
   否则读到 403 Local API is not enabled，或 `users/0` 误报空数组。
4. 本地库固定 `users/0`，端口 23119。运行时走本地 API，
   直读 `zotero.sqlite` 会撞 `SQLite database is locked`。

## 写入授权

本地写必须先 `authorize_local(app_name)`，Zotero 弹窗，让用户点
**Always Allow**，返回 `{"key": ..., "remember": true}` 后存好，
后续 `Zotero(library_id='0', library_type='user', local=True, local_api_key=key)`。

Allow（单次）只够一次写，下一次写报 401 `LocalAPIKeyRequiredError`
就要重新授权。授权端点有 rate limit，用单次调用拿 key，不要写重试循环。
- 永久 key 落盘复用：`remember: true` 的 key 长期有效，存到本机不进版的位置
  （如仓库 `.env` 的 `ZOTERO_LOCAL_API_KEY` 并进 `.gitignore`），后续从该位置
  读 key 初始化，不再弹窗。

- `authorize_local()` 调起后 Zotero 弹窗等人点：调用前先告知用户去点并给足
  超时（如 120s）。ReadTimeout 通常=超时内没人点，不是网络故障；不要写重试
  循环（授权端点有 rate limit），等人确认后单次重调。
- 写操作必须显式传 key：建条目／建集（POST）无 key 可能成功，但改 `collections`（PATCH）必报 `API key required`——ad-hoc 调 pyzotero 写操作一律显式传 `local_api_key`（`--api-key` 或 `ZOTERO_LOCAL_API_KEY`），不依赖"之前能写"的印象。

- 读 key 先去空白：`.env` 取值须 `strip` 引号与 `\r\n` 空白（Windows CRLF 尾随 `\r` 会使 key 非法，报 `Illegal header value`）。

## 写入前去重

- 建正式条目前先做**入库预检**（决策见 ADR-0020）：以归一 DOI 为权威键、单探针
  先行——命中即判重复、零网络元数据（有正文附件→`DUP` 重复取消；缺正文附件→
  `DUP-GAP` 转挂载链）；未命中才取 Crossref 并走慢路径复核（后缀探针＋标题探针，
  命中即停；附件与网页快照命中仍算，供迁移与报告）。
- 性能契约：探针 ≤3s；含正文附件核的完整判定 ≤5s。**预检异常一律 fail-closed**
  （中止报错，不得当未命中继续建库）。
- 批量预检走 `scripts/check_duplicates.py`（DOI 清单 ⇒ `DUP`／`DUP-GAP`／`NEW`
  行＋`OK` 汇总；重复不是失败，exit 0）；≥10 篇自动改走进程内全库索引（库版本探针
  验鲜）。下载前预检的排跑位置见 `references/placement.md` 全文链。
- 快照类条目（webpage + 附件齐、但无 DOI/作者/卷期）不算正式收录；建正式条目
  后把附件挂过去再删旧条目，不要自动删附件。
- 批量建条目后用标题检索 + DOI 比对逐条回查，以回查为准，不以写入返回为准。
- 批量补录走 `scripts/seed_from_doi.py`（先 `--dry-run` 核验去重结论；预检先行，命中零 Crossref 调用）。
- 补录脚本缓存只省读：Crossref 元数据按归一 DOI（小写、去前缀）缓存在包内 `.cache/`
  （TTL 168 小时、`--no-cache` 显式绕过），命中时输出 `META` 行记**原始补录日期**，
  报告不得写成当日抓取；去重结论仅进程内 memo（写库后重查、跨运行不复用）。
  写库确认门与写路径不受影响。

## 摘要入库

口径单源（ADR-0021）；命令契约见脚本 docstring，不在此复述。

- **落点单字段**：摘要只写条目 `abstractNote`，不建子笔记、不只留交付物；正文原样入库，不翻译、不截断、不写占位文本。
- **取数次序**：**入库摘要表**（Step2 产物，口径见 `med-lit-review` 内 `references/protocols/step2-dedupe-and-snapshot.md`）→ Crossref（`message.abstract`，JATS）→ PubMed（EFetch）→ OpenAlex（按词位重建）；逐级降级，某级无正文或取数失败不阻断下一级。新建补录 `scripts/seed_from_doi.py` 与既有条目补齐 `scripts/fill_abstracts.py` 共用 `scripts/abstracts.py`，取数与清洗只此一份。
- **写前先看文种**：按条目的类型字段（`itemType`）判适用性——`journalArticle`／`preprint`／`conferencePaper` 可摘要；指南、监管文件、书章等记 `na`（不适用，不算缺口）。类型缺失**不判不适用**，按可摘要侧处理（取不到记 `empty`）。
- **状态词与来源名**：状态 `filled`（取到正文）／`empty`（表未命中且各级无正文或取数失败）／`na`（文种不适用）／`no-identifier`（无 DOI 且无 PMID）；来源 `table`／`crossref`／`pubmed`／`openalex`／`none`。
- **清洗口径**：JATS/HTML 标签转换行（段落与分节因此成行）、解 HTML 实体、压空白、剔开头的 `Abstract`／`摘要` 标签行；结构化摘要的分节标题原样留在正文、不转 markdown。
- **仅空补齐、非空永不覆盖**：新建条目一律尝试填；既有条目只在 `abstractNote` 为空时才补，非空条目原样不动、零写入——幂等可重跑（补过的条目再跑即跳过，仍空的条目再跑会重试取数）。
- **缺口非阻断**：`empty`／`na`／`no-identifier` 一律不写占位文本，字段留空、状态如实留痕，条目照建、报告照走；缺口只留痕，不拦住写库与交付。
- **写后回查为唯一读数**：每条写完只读回查库内条目（`ok`／`empty`／`na`；读不到即 `?`），不以写入返回为准。
- 命令契约（`--abstracts-file`／`--no-fetch`／`--dry-run`、留痕行、退出码与 fail-closed 面）见 `scripts/seed_from_doi.py` 与 `scripts/fill_abstracts.py` 的 docstring。

## 请求细节

- Server-ID 与 428 由 pyzotero `_write` 自动处理，业务代码不要手造请求头；
  手探 API 时 write token 取 5–32 字符。
- `Last-Modified-Version` 是**库级**版本（多对象响应取 `library.clientVersion`，
  任一对象写入都使其前进），故可作「库有没有变过」的轻量探针：`items?limit=1`
  一次读头即得（pyzotero：`last_modified_version()`）。拿它校验读数复用（缓存/快照
  命中条件）时，探针必须取在**读数之前**——读数之后再取，会把「读数与探针之间」的
  他会话写入吞成命中（数据已旧而版本号仍等）；取不到版本（无该头或传输失败）一律
  按 miss 处理，不得当 0 用。
- `item_template()` 本地不可用（无 `/items/new` 端点），直接手组 dict
  经 `create_items` / `upload_attachments` 发送。
- 写前用 `check_items()` 校验键名，合法业务字段先问
  `item_type_fields(itemType)`。附件业务字段只有
  `title/accessDate/url`，另加 `linkMode/filename/contentType` 等上传字段。
- 本机无全局 Python 时用
  `uv run --isolated --no-project --with pyzotero --with lxml python`。
