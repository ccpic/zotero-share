# attachments — linkMode 映射、键顺序坑与上传约定

有文件附件上传分支必读。

## linkMode 映射

- 无 linkMode 有文件 → `imported_file`。
- `linkMode=1` 有文件 → `imported_url`，附带原 url/accessDate。
- `linkMode=3` 无文件 → `linked_url` 书签，经 `create_items` 带 `parentid` 建，
  不要走上传通道。

## 键顺序坑（已验证）

附件创建对 JSON 键顺序敏感：`linkMode` 必须排在 `filename/contentType`
之前，否则 400 `Link mode must be set before setting attachment path`
（httpx 手探 A/B 顺序确认）。构造 dict 时先放 `itemType/linkMode` 再放文件名。
pyzotero 的 `upload_attachments` 原样透传 dict 顺序，不会替你重排。

## filename 与 basedir

`filename` 传 basedir 相对路径（如 `files/11/xxx.pdf`），`basedir` 指到
files 的父目录：Zupload 靠它拼真实路径，发包时自动压成 basename。
传绝对路径必错。
`basedir` 本身用绝对路径（或先 `cd` 到固定工作目录再用相对路径）：它是按进程 cwd 解析的，cwd 不对则全员 `file_ok=False`（本 session 实测相对路径全员 unreadable，改绝对路径后 21/21 可读）。

## 同名附件不重挂（strong rule）

When: 挂载前 children 里已存在同名（`filename` 或 `title` 命中）子附件时。
Do: 跳过该条上传（`attach_pdfs.py` 打印 `SKIP dup` 并计入 `SUMMARY skipped_dup`），只在报告记"既有条目已有附件 → 跳过"。既有条目默认补挂的前提是"缺正文附件"（`DUP-GAP`）；`dup` 计数只是展示——不跳过就会造出同名双附件。
Why: 本 session 实测 9 个父条目被挂出同名双附件（脚本只展示 `dup=1` 仍继续上传），事后逐个删重复子附件收尾。

## 其他约定

- `contentType` 取 RDF 的 `link:type`，缺省按 `application/pdf`。

## 挂前验身份（strong rule）

When: 外部下载的 PDF 落盘后、调 `upload_attachments` 之前。
Do: 抽正文核对与目标 DOI 为同一篇（标题分词多数命中/作者姓/卷期页三项至少两项命中；先判无文本层：总字符 < 200 标 unverified 转人工，再判页数 ≤3 为错文）。失败则 `cache_clear` 后换 strategy 重下，不把错文挂进库；缺 PyMuPDF 时校验无法运行，直接失败不挂库。Sci-Hub 等来源返回正确率非 100%，更要验。
Why: 本 session 实测 OpenAIRE 对同一 DOI 返回了另一篇同刊文章（3 页错文），且首次 `cache_clear` 后 `scihub_first` 仍命中 `local_cache` 残留索引吐出同一错文；删本地错文件改 `scihub_only` 才拿到 10 页正文。不验则错文永久入库。

- stored file 的入库时间由上传决定，API 不接受回填，不要试图还原 dateSubmitted。

## 缓存口径（挂载脚本两处，只省读、不省核验）

`scripts/attach_pdfs.py` 两处缓存不改挂载写路径与回查口径（ADR-0007）；规则沿 `workspace-guide.md` §14.2 进程内行与 §14.1 身份核验跑跳行，不另立口径。

- **children 列表**：按父条目键单次运行内 memo——同一相位内同父只取一次（两条目共一父时不重复取）；上传（含写失败）后该键失效、回查相必重取；进程内、跨跑永 miss。留痕：`SUMMARY children_reads=…`（每个已挂父条目计 2：计划＋回查）。
- **身份核验结论**：键＝方法版本＋归一 DOI＋字节 sha256＋父条目键＋父条目版本＋期望值（题名／作者／卷期），全等才复用「通过」（不重开 PDF）；字节、父条目、DOI、期望值或方法任一变即重跑核验。条目自身缺项的 pin 按同侧值进键（同侧同值即键等）：缺项不单独产出「通过」——核验判据要求三项（题名／作者／卷期页）至少两项命中，故缓存中的「通过」必有 ≥2 组 pin 命中，同字节同 pin 重跑即同判决；§14.1「期望值缺项即重跑」用在两侧对等的比对上（取件链 `--expect` 比对）。`False`／`None`（拦截态）永不入缓存、永不作为通过复用；缺 PyMuPDF 仍 fail-closed，缓存命中也不挂库。缓存位＝skill 包内 `.cache/`（`--cache-dir` 须为被忽略的非交付路径；`--no-cache` 显式绕过、不读不写）；键即正确性、无 TTL。留痕：逐条 `identity=… src=cache|verify|unreadable checked=<原核验时刻>`——命中沿用原核验时刻，不写本次重跑日。
