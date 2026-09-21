# ADR-0005 e2e隔离盲测：隔离worktree剪裁与三查

- 日期：2026-09-16；状态：已接受；来源：14 号票 grill-with-docs 四轮（承 05 号票计划、13 号票 placement 内化）。
- 上下文：在主仓跑 e2e 会偷看 CONTEXT.md 增补结论与 `.scratch/` 答案，自包含/防作弊/可移植三者皆失；初版 skill 落字后须有与记忆隔离的跑法。
- 决定：基线为初版 med-lit-review skill 落字 + 全提交干净时的 commit；落点 `.worktrees/e2e-blind`；剪裁删 docs/（全）、tmp/、.scratch 残留、CLAUDE.md，留 AGENTS.md（执行体必读：先读根地图与分工再进 SKILL.md）、CONTEXT.md（通用词汇表）、.mcp.json、zotero-pdf2zh/、.claude/skills/*（`.scratch/`/`.workbuddy/`/`.env` 自然缺席）；隔离测试库 + `--dry-run`（主库零写入）；文件/内容/环境三查（内容只查答案串，不查 skill 正文领域词；AGENTS.md 自身 `.scratch/` 字样为例外白名单）；根说明漂移只判 harness 差异。
- 后果：tmp/ 删即脏属有意，不提交；docs/agents/ 亦删，runner 不读 map；CONTEXT.md 在场为通用词汇（非答案），执行体可读；05 资产 §9 链 14 号票，§1–§8 口径不动。
- 修订（2026-09-17）：剪裁由“删 AGENTS.md/CONTEXT.md”改为留二者——AGENTS.md 新增 skills 地图与分工后成为执行入口，盲测须验证其指路效果；placement-guide 瘦身后依赖根 CONTEXT.md 词汇指针，删后指针悬空。
