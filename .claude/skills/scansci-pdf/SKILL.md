---
name: scansci-pdf
description: 下载学术论文全文 PDF。DOI/arXiv 单篇与批量下载、开放获取定位、机构登录绕过付费墙、引文导出、Zotero 推送。当用户提到下载论文、全文 PDF、DOI 下载、批量下文献、付费墙、机构登录时使用。纯文献发现走 jadense-scholar-search，文献库管理走 use-zotero。
---

# scansci-pdf

学术论文全文获取入口。MCP 服务器 `scansci-pdf`（本 repo `.mcp.json`，`scansci-pdf run`）+ CLI `scansci-pdf` 兜底。

## 前置

`scansci-pdf check` 核心依赖全 OK；缺失则 `uv tool install --python 3.13 "scansci-pdf[fast,vpnsci]"`。MCP 工具参数以 `tools/list` 为准。

浏览器后端（发布商/机构路线的必需件，`check` 里以 `[optional]` 列出）分三步，只装包不算装好：

1. 装包：`uv tool install --python 3.13 --force "scansci-pdf[fast,vpnsci,patchright,cloakbrowser]"`；camoufox 未声明为 extra，用 `--with camoufox`；本地 editable 依赖用 `--with-editable <路径>` 保留。
2. 装浏览器二进制（各后端独立，缺则导入成功也启动不了）：`patchright install chromium`；`cloakbrowser install`（或复用已有用户缓存）；`camoufox fetch`。
3. 装了新后端后必须重启常驻 MCP server（装前先停，见下条）：运行中的实例加载旧环境并持有 shim，不重启新后端不可见。

- 装前先停掉持有该工具 entrypoint 的常驻进程：否则 `--force` 在复制 entrypoint 时失败并**回滚 receipt**（包已进环境、元数据退回旧规格），日后 `uv tool upgrade` 会静默卸掉这些依赖；无法停进程时，装后核对 receipt 是否含新 extras。
- 大二进制下载勿据「无输出」判挂起：进度条在非 TTY 下被缓冲（零输出、缓存目录不涨）；用 PTY 跑或看进程 I/O 再判断。

验收：不以 `import` 成功当通过——每个后端真启动一次浏览器并载入可达页面，再叠加 `scansci-pdf check` / `browser-status`，最后看下载日志是否从 `no browser backend available` 变为真实 `create_tab`。

## 分工

- 发现选文：`jadense-scholar-search`（provider-neutral 检索，可审计）。
- 全文下载：本 skill。
- 入库管理：`use-zotero`（`scansci_pdf_zotero_push` 只推单篇已下载缓存）。

## 路由

- 单篇：`scansci_pdf_download(identifier, strategy?, bibtex?, download_si?, markdown?)`，strategy 取值 `fastest|grey_only|scihub_only|scihub_first|oa_first|legal_only`。返回非 success 时看 `agent_hint`：paywall → 登录后重试；Elsevier → 先 `scansci_pdf_elsevier_setup`。
- 批量：`scansci_pdf_batch_download(identifiers?|file?, output_dir?, batch_id?, resume=true, lanes?)`，file 支持 txt/csv/xlsx/BibTeX/APA；`.bib` 自动走导入路径；≥3 篇默认四车道调度。只解析不下载用 `scansci_pdf_parse_list(file_path)`。
- 检索与队列：`scansci_pdf_search(query, limit≤50, year_from/year_to, sort, author/author_id, out_file?)`（out_file 直接产出待下载队列）；系统性发现 `scansci_pdf_find(action=plan|estimate|smoke|calibrate, query, domain, depth, sample_size)`；引文扩展 `scansci_pdf_expand_citations(query, rounds≤5, citation_source, limit≤100)`；队列准备 `scansci_pdf_prepare_queue(action=verify|resolve_oa|build|full, candidates_json|query,limit,depth)`。
- 机构：`scansci_pdf_login(kind=publisher|webvpn|carsi|ezproxy|custom|cookie_import, identifier|publisher|custom_url|cookie_file)`；状态 `scansci_pdf_channel_status(kind=webvpn|carsi|ezproxy|browser|browser_doctor|webvpn_test, doi?)`；学校 `scansci_pdf_schools(action=search|set, query|school)`；Elsevier/Springer API 走 `scansci_pdf_elsevier_setup` / `scansci_pdf_springer_setup`。
- 引文：`scansci_pdf_citation(identifier, format=bibtex|ris|endnote|metadata)`。
- 诊断配置：`scansci_pdf_diagnostics(check=health|network|sources|setup|auto_setup, detailed?)`；`scansci_pdf_config(key?, value?)`；`scansci_pdf_tor(action=install|start|stop, use_bridges?)`；`scansci_pdf_cache_clear(identifier?)`。

## 边界

- 无来源偏好时默认走竞速策略（`fastest`，含灰色源与 Tor 路线）；免费、仓储、出版商接口、机构授权路线为并列通道；逐篇实际来源如实报告（含灰色与 Tor 路线）。
- 打开交互式登录、导入 cookie、改访问配置前先征得用户同意。
- API Key、cookie、代理凭证、机构信息视为机密，不复述。
- 灰色源与反爬路线（含 Tor）默认启用（本机全局配置 `scihub_enabled=true`），无人值守同样启用（ADR-0006：风险由用户承担，不设每次确认）；仅在用户有权且规则允许时使用。
- 浏览器路线第一步就失败时，先测「浏览器能否打开该地址」，不要换个 curl 能通的地址就下结论：同一地址 curl 可 200，浏览器直连与经代理仍可能超时。发布商路线的首个 tab 常建在一个硬编码的轻量预热页上，该页不可达会让整条路线在第一步返回不可用；定位到该 URL 后再决定改预热页还是换网络路径。

## 上游规范

改反爬/嗅探/镜像逻辑前读 [PLAYBOOK](https://github.com/Rimagination/scansci-pdf/blob/main/docs/PLAYBOOK.md)（镜像健康唯一存储、失败签名表、六条铁律）；完整工作流见 [skill/SKILL.md](https://github.com/Rimagination/scansci-pdf/blob/main/skill/SKILL.md)。

完成标准：单篇以文件落盘且 `success=true` 为准；批量以返回计数为准；登录后必须 `channel_status` 验证通过再下载。
