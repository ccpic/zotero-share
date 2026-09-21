---
name: use-zotero
description: Zotero 文献库导入导出读取、RDF 解析回灌、附件上传、分类重建与本地 API 排错。当用户提到 Zotero、文献库、RDF 导入导出、附件上传、分类整理、文献检索、zotero.sqlite 或 pyzotero 时使用，即使没点名本 skill 也要先读它。
---

# use-zotero

Repo 内操作 Zotero 的统一入口。先按分支读对应 reference，再动手。

## 技术栈选型

只读检索走 `zotero-mcp`（search / item_metadata / item_fulltext 三个工具，
经实测无写入能力）；读写全套走 `pyzotero`（`local=True`）+ `lxml`。
手动兜底是桌面端文件菜单导入导出；用户要求 MCP/CLI 时不许切 computer-use。

`zotero-mcp` 由仓库根 `.mcp.json` 声明（`ZOTERO_LOCAL=true`，命令须在 PATH 上）；安装与环境前提见仓库根 `README.md`。

## 分支

- 连通/授权/写前校验：读 `references/local-api.md`。
- 检索与附件正文：读 `references/reading.md`。
- 导出：读 `references/exporting.md`。
- 导入后归置：读 `references/placement.md`（放置闭环 + 维度规则；批量执行走 `scripts/place_imports.py`，先 `--dry-run` 再写库；PDF 获取与挂载默认走 scansci-pdf 链，见该文件“全文链”一节）。
- 摘要入库（新建／既有补齐）：读 `references/local-api.md` 的「摘要入库」一节；补录 `scripts/seed_from_doi.py` 与既有条目补齐 `scripts/fill_abstracts.py` 共用 `scripts/abstracts.py`，按「入库摘要表 → 元数据源」取数。
 - 报错先对 `references/troubleshooting.md`，对不上再深挖。

完成标准：导入以收尾计数校验为准；读取以命中目标条目为准；
报障以复现→修复→回归为准；
摘要写入以写后回查为准，缺口非阻断。

## 校验

- 行为用例见 `evals/evals.json`，触发边界见 `evals/trigger-evals.json`；
  改写入、去重、授权任一规则时同步更新用例并校验 JSON 可解析。
