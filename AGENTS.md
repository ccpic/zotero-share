# Zotero 医学文献阅读 Agent

医学文献阅读 Agent：发现→分类→下载→入库→研读全链路，交付可追溯知识包。词汇以 `CONTEXT.md` 通用词表为准，决策以 `docs/adr/` 为准，流程以 `.claude/skills/` 为准。



## 核心架构


| 路径                | 职责                               |
| ----------------- | -------------------------------- |
| `.claude/skills/` | 5 个 skill，地图见下节；各 SKILL.md 为执行真源 |
| `zotero-pdf2zh/`  | 翻译插件 XPI + 翻译 server，见下节         |


注：`tmp/`、`.worktrees/`、venv、`translated/`、`__pycache__` 为一次性产物，不读不写，不作为规范依据。

## Skills 地图

主链：`jadense-scholar-search` → `scansci-sort` → `scansci-pdf` → `use-zotero`。
`med-lit-review` 为编排者，按其 SKILL.md 编排路由表顺序调用前四者并综合输出。


| Skill                    | 职责                                                                                      | 输入→输出                                      |
| ------------------------ | --------------------------------------------------------------------------------------- | ------------------------------------------ |
| `jadense-scholar-search` | provider-neutral 学术检索，可审计                                                               | PICO + 关键词→候选清单 + query plan + diagnostics |
| `scansci-sort`           | 大清单分类分桶，只探不下                                                                            | 候选文件→四桶报告 + 下载队列                           |
| `scansci-pdf`            | 全文 PDF 获取（MCP `scansci-pdf` + CLI 兜底）                                                   | 队列→PDF 落盘 + 实际来源报告                         |
| `use-zotero`             | 入库归置与研读，RDF 解析回灌、attachment 挂载（做法见 `.claude/skills/use-zotero/references/placement.md`） | PDF + 元数据→落点报告 + 提取卡                       |
| `med-lit-review`         | 编排者：检索/下载/入库/研读全包，不写学术综述正文                                                              | PICO→八节知识包 + 七列表                           |


跨 skill 边界以各 SKILL.md 为准，本文件只定地图，不复述分支与参数。

## zotero-pdf2zh

`zotero-pdf2zh/zotero-pdf-2-zh.xpi` 为 Zotero 端翻译入口，与 server 配套使用。
启动：`uv run --no-project --with-requirements zotero-pdf2zh/server/requirements.txt python zotero-pdf2zh/server/server.py`（默认 `127.0.0.1:8890`，venv 自动沿用/新建；改端口加 `--port`；离线加 `--check_update=no`）。
不可写成裸 `uv run python zotero-pdf2zh/server/server.py`：服务器外层依赖（`server/requirements.txt` 的 flask/toml/pypdf/PyMuPDF/packaging）不进根环境，裸跑会 `ModuleNotFoundError: flask`。

## Node 运行时

Step1 检索 CLI（`.claude/skills/jadense-scholar-search/`）是 Node 包，需 Node ≥ 20 + npm。
其 `dist/` 与 `node_modules/` 被该 skill 的 `.gitignore` 忽略，**干净 checkout 里不存在**：须先在 skill 目录内 `npm ci && npm run build`，再跑 `node dist/bin/scholar-search.mjs …`。

## Python 运行时

仓库内一切 Python 一律经 `uv run`，禁裸 `python`/`pip`（本机无全局 Python）。
- 标准库脚本：`uv run python <脚本>`（当前 skills 仅此形态，无根 `pyproject`，不建根 `.venv`）。
- 需第三方库（`pyzotero`/`lxml`，验身份另需 `pymupdf`）：用 `uv run --isolated --no-project --with …`（见 `use-zotero/references/local-api.md`）。
- 翻译引擎（`pdf2zh`/`pdf2zh_next`）独立：`server.py` 自管 `zotero-pdf2zh/server/zotero-pdf2zh*-venv/`（声明在 `config/venv.json`，外层依赖在 `server/requirements.txt`）；已有托管环境不动、不删、不指向根。

## Zotero 库读写分工

只读检索走 `zotero-mcp`（search / item_metadata / item_fulltext，无写入能力）；读写全套走 `pyzotero(local=True)` + `lxml`；分支细节见 `use-zotero` SKILL.md。

## 学术写作风格

不编造、未知标占位（`[待补充出处]` / `[待查证]` / `未见原文`）、中文为主且药物/疾病/结局指标保留英文原名。
硬规则见 `med-lit-review` SKILL.md 研读完整性 R1–R4；引文单制式 GB/T 7714；术语扩展见其 `term-lexicon`。

## 领域文档与术语

输出命名沿用 `CONTEXT.md` 通用词（放置关系 + 证据类型/设计类型主名），不自创同义词；与 ADR 冲突时显式声明，见 `docs/agents/domain.md`。
三分工：通用词（是什么）→根 `CONTEXT.md`；做法（怎么做）→执行方 skill references（通用落点 `use-zotero`，医学实例化 `med-lit-review`）；决策（为何这样）→`docs/adr/`。分工总览见本文件（Skills 地图 + 核心架构）；`docs/agents/domain.md` 只留消费指南；决策见 ADR-0009。
医学实例关系边（同义/上下位/易混/正字变体的具体词对）只进 `med-lit-review` 内 `term-lexicon`，任务内正表冻结、候选走候选队列（晋升即出账）。
灰色渠道默认启用，口径见 ADR-0006，本文件不复述政策。

## workspace 持久化（薄指针）

每次交付落仓库根 `workspace/YYYY-MM-DD-<slug>/` 一个目录（日期加主题即身份，重跑新目录不覆盖）；交付根固定为 `INDEX.md`（机读纯索引）＋`<slug>.html`（人读主件）＋`agent/`（真源与账目）＋`run/`（忽略缓存）——人读走 HTML，机器读 `agent/`，被忽略件只留 `run/` 并凭再生清单兜底。细节见 `.claude/skills/med-lit-review/references/protocols/workspace-guide.md`（人读主件渲染契约见同目录 `reader-html.md`），决策见 `docs/adr/0025-delivery-output-layering.md` 与 `docs/adr/0010-workspace-persistence-contract.md`。

## 协作约定

### Issue tracker

Issues live as local markdown files under `.scratch/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary (needs-triage / needs-info / ready-for-agent / ready-for-human / wontfix). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout (one CONTEXT.md + docs/adr/ at root). See `docs/agents/domain.md`.
