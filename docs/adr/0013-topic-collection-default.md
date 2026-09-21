# ADR-0013 本次合集：默认创建＋双挂＋AFK 全自动（修订 ADR-0002 与交互点三回退）

- 日期：2026-09-17；状态：已接受；来源：grill-with-docs 会话（四轮 17 问 live exchange＋用户确认）。
- 上下文：med-lit-review 此前无独立 topic 库、无双写：Zotero 侧永远单库 `users/0`，topic 只活在文件侧 `workspace/YYYY-MM-DD-<slug>/`。用户要求 HITL 加一步：是否创建本次 topic 的 collection，条目默认写既有收藏集但多挂到本次集，默认创建。
- 决定：
  - 术语**本次合集**（CONTEXT.md 新词）：单库内本次交付的收藏集挂载，多归类并存的一挂；非独立库、非双写。Avoid：topic 库／topic collection／独立 topic 库／双写。
  - 载体：新建 collection；父级固定 `med-lit-review`；集名＝交付目录名原文 ASCII slug（如 `med-lit-review/2026-09-17-pcsk9-ascvd`）；同名复用幂等；只增不删，与 workspace 同形。
  - 挂载＝双挂：新条目＝语义落点＋本次合集；既有条目原收藏集不动，追加本次合集＋缺附件补挂；webpage 快照转正式后同样双挂。`place_imports.py` 的 `placements={itemKey:[..., topicKey]}` 天然支持。
  - 双挂篇数来源（ADR-0032 对齐）：本次合集双挂与写库同一范围——写库范围＝研读选取集 − 仅题录成员（`agent/download-queue.txt`＝研读选取集＝入库集，三集合一；仅题录成员不建条目、不入库，只在可及性清单与 §8 逐条交代）。
  - HITL：并入交互点三，不新增断点；同一问加本次合集行＋每行双挂确认。有人值守默认创建＝推荐位指向“建＋双挂”，仍需一次确认（默认≠免确认）。
  - AFK 全自动（修订 ADR-0002 与交互点三“未确认不写库”fail-closed）：无人值守下集＋新条目双挂＋既有追挂＋补附件＋父缺席同自动建，全自动执行，只记日志，缺口进局限声明。P2／P3（写库设计跳过）不建集，走未写库块。
  - “禁擅自新建”例外：经交互点三确认、或 AFK 特批自动建，即非擅自。
  - 门禁：新增 D-10 本次合集回查一致（硬门禁，与 D-03 同形态的文档级断言；live 下由 D-07 验库内一致）；H-04 聚合扩为 D-03／D-07／D-08／D-09／D-10；自检增第 7 变异（本次合集挂载缺口）。旧口径包（无本次合集列）D-10 按当时口径读记通过，不判现行失败。
- 备选（未采纳）：打 tag 代替建集（无层级，回查与 D-07 口径另写）；只挂本次合集不写语义目录（破坏指南分流）；AFK 不建（与用户拍板冲突）；独立新断点（多一次往返）；P2／P3 建空集（空集语义另定）。
- 后果：hitl-protocol／placement-guide／placement-report-template／workspace-guide §7／run_gate（D-07、D-10、G-04、H-04、自检）／demo_topic_fixture 收据／SKILL 入库默认与 Step4 回退同步改写；CONTEXT.md 增本次合集一词。
