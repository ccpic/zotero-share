# Zotero 医学文献阅读 Agent

从文献发现到入库研读的全链路 agent 工作区。五个 skill 由 `med-lit-review` 串成主链：
`jadense-scholar-search`（检索）→ `scansci-sort`（分类分桶）→ `scansci-pdf`（全文下载）→ `use-zotero`（入库归置与研读）。

本文件只讲**安装与初始化**——让一份干净的 clone 在你的机器上跑起来。
技能地图、路线预设与交付物形态见 `AGENTS.md`；术语见 `CONTEXT.md`；决策见 `docs/adr/`。
（`CLAUDE.md` 是一行 include：`@AGENTS.md`。）

这份 README 只覆盖工具链安装。`workspace/` 是每次研读在本机生成的交付目录，clone 里可以没有现成知识包。灰色渠道默认开启，合规风险由使用者承担（见 §6）。

---

## 1 前置条件

| 项 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Windows 11 | 本文件只覆盖 Windows，命令全部是 PowerShell |
| Agent 宿主 | 能加载仓库内 `.claude/skills/` 与根 `AGENTS.md` 的任意 agent 工具 | 不绑定某一家宿主 |
| Zotero | 10（本机实测 10.0.2） | 必须能启用本地 API，见 §2.4 |
| uv | 必需 | 仓库内一切 Python 都经 `uv run`，不假设有全局 Python |
| Node.js | ≥ 20 | Step1 检索 CLI 的构建与运行 |
| git | 必需 | clone 与 `.worktrees/` |
| curl.exe | 必需 | 本地 API 连通自检。PowerShell 里写 `curl.exe`，不要写 `curl` |

一次装齐：

```powershell
winget install --id astral-sh.uv -e
winget install --id OpenJS.NodeJS.LTS -e
```

装完**重开一个 PowerShell**，确认：

```powershell
uv --version      # 本机实测 0.9.24
node --version    # 需 >= 20，本机实测 v24.11.1
```

> `uv` 与 `uv tool` 安装的命令都落在 `%USERPROFILE%\.local\bin`，该目录必须在 PATH 上。
> `uv` 安装时通常会自动加，加完要重开终端才生效——`scansci-pdf`、`zotero-mcp` 都从这里解析。
>
> 注：`jq` / `grep` / `sha256sum` 出现在仓库的做法文档里，那是 **agent 侧**工具；普通 PowerShell 里没有它们，不影响安装与使用。

---

## 2 安装

### 2.1 构建检索 CLI

`dist/` 与 `node_modules/` 都不入库，干净 clone 里两者都不存在，必须重建，否则 Step1 无法检索。

```powershell
cd .claude\skills\jadense-scholar-search
npm ci
npm run build
cd ..\..\..
```

自检：

```powershell
node .claude/skills/jadense-scholar-search/dist/bin/scholar-search.mjs --offline --query "transformer interpretability"
```

### 2.2 安装 scansci-pdf（全文下载）

```powershell
uv tool install --python 3.13 "scansci-pdf[fast,vpnsci]"
scansci-pdf check
```

仓库根 `.mcp.json` 以它作为 MCP server 命令（`scansci-pdf run`），所以它必须在 PATH 上。

**选装：浏览器后端**（只在走发布商 / 机构路线时需要；`scansci-pdf check` 里以 `[optional]` 列出）：

```powershell
uv tool install --python 3.13 --force "scansci-pdf[fast,vpnsci,patchright,cloakbrowser]" --with camoufox
patchright install chromium
cloakbrowser install
camoufox fetch
```

三个坑：装 `--force` 前先停掉正在运行该工具的常驻进程，否则 `--force` 复制 entrypoint 时会失败并回滚 receipt，日后 `uv tool upgrade` 会静默卸掉这些依赖；装包不等于装好，每个后端要能真启动一次浏览器；装了新后端必须重启常驻 MCP server，否则新后端不可见。

### 2.3 安装 zotero-mcp（只读检索）

```powershell
uv tool install zotero-mcp
```

提供三个只读工具：`zotero_search_items` / `zotero_item_metadata` / `zotero_item_fulltext`，无写入能力。
仓库根 `.mcp.json` 已声明它（`ZOTERO_LOCAL=true`）。**读取不需要任何 key。**

### 2.4 启动 Zotero 并开启本地 API

1. 安装并启动 Zotero 10 桌面端。
2. 编辑 → 首选项 → 高级 → 勾选 **Allow other applications on this computer to communicate with Zotero**。
3. **完全退出并重启 Zotero。**

第 3 步不能省：不重启会读到 `403 Local API is not enabled`，或 `users/0` 误报空数组。

自检：

```powershell
curl.exe -s http://127.0.0.1:23119/connector/ping
# 期望：输出含 Zotero is running（完整响应形如 <html><body>Zotero is running</body></html>）
```

本地库固定 `users/0`、端口 23119。不要直读 `zotero.sqlite`——Zotero 运行时会占用它（`SQLite database is locked`）。

### 2.5 建 `.env` 与首次写入授权

**只读不需要 key；写库需要。** 写库指建条目、挂附件、归置收藏集。

在仓库根建 `.env`（已被 `.gitignore` 忽略，不会进版本）：

```
ZOTERO_LOCAL_API_KEY=<你的 key>
```

首次获取 key：跑一次授权，Zotero 会弹窗，点 **Always Allow**：

```powershell
uv run --isolated --no-project --with pyzotero python -c "from pyzotero import zotero; z=zotero.Zotero('0','user',local=True); print(z.authorize_local('med-lit-review'))"
```

弹窗出现前不要走开。调用超时通常是没人点弹窗，**不是**网络故障。授权端点有速率限制，不要写成重试循环。点 `Allow`（单次）只够一次写入，下一次写会报 `401 LocalAPIKeyRequiredError`；要点 `Always Allow` 才能长期复用。

> **`.env` 只是存放处，脚本不会自动读它。**
> 人手动跑脚本前要显式设置进程环境变量：
> ```powershell
> $env:ZOTERO_LOCAL_API_KEY = "<你的 key>"
> ```
> 或给脚本传 `--api-key`。agent 侧会自己读 `.env` 取值。

### 2.6 选装：翻译（pdf2zh server + XPI）

启动翻译服务：

```powershell
uv run --no-project --with-requirements zotero-pdf2zh/server/requirements.txt python zotero-pdf2zh/server/server.py
```

默认 `127.0.0.1:8890`，改端口加 `--port`。首次启动会从 `config/venv.json.example` 生成 `config/venv.json`，并自建引擎 venv（Python 3.12）——这一步要联网拉依赖。翻译引擎默认是免费的 SiliconFlow（`siliconflowfree = true`），**不需要付费 key**。离线环境加 `--check_update=no` 可跳过启动时的更新检查。

Zotero 端装插件：「工具 → 插件」（Tools → Add-ons）→ 齿轮图标 → 「从文件安装插件」（Install Add-on From File）→ 选 `zotero-pdf2zh/zotero-pdf-2-zh.xpi`。

自检：

```powershell
curl.exe -s http://127.0.0.1:8890/health
# 期望：{"message":"PDF2zh Server is running","outputDir":"...","status":"ok","version":"4.1.7"}
```

该功能与主链相互独立，不装不影响检索、下载与入库。

---

## 3 MCP 注册

仓库根 `.mcp.json` 已声明两个 server，随 clone 一起进来：

```json
{
  "mcpServers": {
    "scansci-pdf": {
      "command": "scansci-pdf",
      "args": ["run"]
    },
    "zotero-mcp": {
      "command": "zotero-mcp",
      "args": [],
      "env": {
        "ZOTERO_LOCAL": "true"
      }
    }
  }
}
```

- **改完 `.mcp.json` 必须重启 agent 会话才生效。**
- 如果你的宿主不读项目级 `.mcp.json`，把上面的 `mcpServers` 片段加到宿主的全局 MCP 配置里（如 `~/.claude.json` 的 `mcpServers`、`~/.omp/agent/mcp.json`）。
- 未安装的 server 会报告启动失败，不影响其它 server。两个 server 都依赖同名命令在 PATH 上。

---

## 4 安装后自检

### 4.1 逐组件

| 组件 | 命令 | 期望 |
|---|---|---|
| 检索 CLI | `node .claude/skills/jadense-scholar-search/dist/bin/scholar-search.mjs --offline --query "transformer interpretability"` | 输出结果，报错则 §2.1 没做完 |
| scansci-pdf | `scansci-pdf check` | 核心依赖全 OK |
| zotero-mcp | `zotero-mcp --help` | 打印 usage |
| Zotero 本地 API | `curl.exe -s http://127.0.0.1:23119/connector/ping` | 输出含 `Zotero is running` |
| Zotero 读库 | `uv run --isolated --no-project --with pyzotero python -c "from pyzotero import zotero; print(len(zotero.Zotero('0','user',local=True).collections()))"` | 打印一个数字（空库为 `0`） |
| 翻译 server（选装） | `curl.exe -s http://127.0.0.1:8890/health` | JSON 含 `"status":"ok"` |

### 4.2 零网络脚本自检

证明 `uv` + Python + skill 脚本这条链路可用，且校验器真的会报错：

```powershell
uv run python .claude/skills/med-lit-review/scripts/review_knowledge.py .claude/skills/med-lit-review/evals/review-cases/clean.md --cur-year 2026
echo $LASTEXITCODE    # 期望 0：无 error

uv run python .claude/skills/med-lit-review/scripts/review_knowledge.py .claude/skills/med-lit-review/evals/review-cases/dirty.md --cur-year 2026
echo $LASTEXITCODE    # 期望 1：有 error（不是 2）
```

### 4.3 完整端到端验收（不是安装自检）

`run_gate.py` 需要**一份已经跑完的八节知识包与 `run/` 证据**作为输入，并默认联网核对锚点现行性与本地库。
它是交付验收工具，不是安装烟测：

```powershell
uv run python .claude/skills/med-lit-review/evals/acceptance/run_gate.py --run <验收跑目录> --out <结果目录> [--no-live]
```

选题与钉子见 `.claude/skills/med-lit-review/evals/acceptance/topic.md`。

---

## 5 可选配置与凭证

**检索**：默认 PubMed + OpenAlex + Crossref，不需要任何 key。
只有 Google Scholar 需要 `SERPAPI_API_KEY`，而它默认关闭。可选 `SEMANTIC_SCHOLAR_API_KEY`、`NCBI_API_KEY`、`NCBI_TOOL`。

**全文**：付费墙走机构登录——`scansci_pdf_login` / `scansci_pdf_channel_status` / `scansci_pdf_schools`，或 API 路线 `scansci_pdf_elsevier_setup` / `scansci_pdf_springer_setup`。
打开交互式登录、导入 cookie、改访问配置前要先征得同意；登录后必须验证通道通过再下载。

**scansci-pdf 的全局配置**在 `~/.scansci-pdf/`（不在本仓库）。用 `scansci_pdf_config` 读写，用 `scansci_pdf_diagnostics` 排障。

---

## 6 已知政策

**灰色源（Sci-Hub 等）与 Tor 路线默认开启，无人值守同样开启。**（ADR-0006）

下载默认走竞速策略 `fastest`，灰色通道与免费、仓储、出版商接口、机构授权路线并列参与竞速；逐篇实际来源会如实报告（含灰色与 Tor 路线）。机构登录与访问设置的改动仍须每次确认。灰色渠道的版权与合规风险由使用者承担。政策全文见 `docs/adr/0006-grey-default-on-full-scope.md`。

---

## 7 排障

| 现象 | 处理 |
|---|---|
| `403 Local API is not enabled` | 没勾选 §2.4 第 2 步，或勾了没完全重启 Zotero |
| `users/0` 返回空数组 | 同上；或库确实是空的 |
| `401 LocalAPIKeyRequiredError` | key 是单次授权，重走 §2.5 拿 `Always Allow` 的 key |
| Step1 报模块/文件不存在 | §2.1 没做：`dist/` 与 `node_modules/` 不在版本里 |
| MCP server 启动失败 | 命令不在 PATH 上，或改了 `.mcp.json` 没重启会话 |
| `SQLite database is locked` | 不要直读 `zotero.sqlite`，走本地 API |

更多见 `.claude/skills/use-zotero/references/troubleshooting.md` 与 `.claude/skills/scansci-pdf/SKILL.md`。

---

## 8 本机已验证版本

版本不作钉定（安装命令均不指定版本号），此表仅供出事时对照：

| 组件 | 版本 |
|---|---|
| Windows | 11（10.0.26200） |
| uv | 0.9.24 |
| Node.js / npm | v24.11.1 / 11.6.2 |
| uv 托管 Python | 3.14.2 |
| git | 2.52.0.windows.1 |
| Zotero | 10.0.2（build 20260909185038） |
| scansci-pdf | 1.16.0 |
| zotero-mcp | 0.3.1 |
