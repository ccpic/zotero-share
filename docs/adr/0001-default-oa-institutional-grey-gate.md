# ADR-0001 默认 OA/机构路线与灰色门禁

- 日期：2026-09-16；状态：已取代（superseded by ADR-0006，2026-09-17）；来源：04 号票 live exchange（承 02 §7、03 §3/§6.4）。
- 上下文：scansci-pdf 边界要求灰色源与反爬路线须用户明确选择；默认链路与 e2e 默认路径是否进灰色待锁死。
- 决定：默认链路与 e2e 默认路径不进灰色源；`grey_only/scihub_only/scihub_first`、Tor 等仅在用户明确选择、且用户有权、规则允许时启用。
- 后果：paywall 走机构登录（HITL-2）或记待重试；灰色桶走 HITL-1（`recommended` 不启用），AFK 回退为跳过并记局限声明。
