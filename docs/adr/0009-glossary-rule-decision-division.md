# ADR-0009 术语/领域文档三分工：通用词进 CONTEXT、做法随 skill、决策进 ADR

- 日期：2026-09-17；状态：已接受；来源：grill-with-docs 会话（三轮 13 问 live exchange）——用户拍板纯词汇表 + 三分法 + 单点真源 + 单语境 + 移动式迁移。
- 上下文：根 CONTEXT.md 混装词汇与流程（确认优先、补漏环、挂载动作），与 `use-zotero/references/placement.md`（闭环/维度规则/全文链）、`med-lit-review/references/placement-guide.md`（复述八条 + 医学表 + 附件 §4）互相复述；改一处要同步两处。
- 决定：三分法单点真源——通用词（是什么）只在根 `CONTEXT.md`（放置关系 + 证据类型/设计类型主名 + Avoid，不收关系边）；做法（怎么做）随执行 skill（通用落点唯一真源为 `use-zotero` placement.md，医学实例化只在 `placement-guide.md` 医学落点表 + Step3 台账载体）；决策（为何这样）只在 `docs/adr/`（ADR-0002 批量确认、ADR-0006 灰色默认、ADR-0007 挂载）；`term-lexicon` 只收医学实例关系边（具体词对），不收通用词；单语境，不建 CONTEXT-MAP；复述处只留一句话指针；SOP 入口只住 AGENTS.md（Skills 地图行内路径），`docs/agents/domain.md` 回到消费指南（分工表只留词汇与决策三行）。
- 备选（未采纳）：词汇 + 落点规则留 CONTEXT（规范不纯）；受控复述（少跳转但要人维护同步）；多语境 CONTEXT-MAP（ceremony 重）；合并 placement-guide 进 use-zotero（医学 skill 多带通用包袱）。
- 后果：CONTEXT.md 批量确认/入库挂载/补漏环三条改为定义 + 指针；placement-guide 瘦身，闭环/边界/附件通用做法改为指针；`med-lit-review` SKILL.md、evals、report-template 指 placement-guide 的既有行继续有效（内容收敛未改名）；AGENTS.md 术语分流改为三行（通用词/做法/决策），SOP 入口住 AGENTS.md。
- 修订（2026-09-17）：分工总览入口由 domain.md 分工表改为 AGENTS.md——placement.md 系 SOP（做法），指针住执行地图；domain.md 分工表删 placement.md / placement-guide.md 两行，只留通用词与决策。
- 修订（2026-09-17）：CONTEXT 由“只收放置词汇”扩为通用词表（+证据类型/设计类型主名，不收关系边；关系边仍归 term-lexicon，升降规则仍归 grading notes）；placement-guide §2 干净重排为 8 行（旧 3 一分为试验 3 + 监管 5，新增观察性行 4；附旧编号别名），冻结后只增不改。
- 修订（2026-09-17）：单点真源射程＝跨文档复述（CONTEXT／skill／ADR 之间）——同一指令文件内的点用重复（同一规则在入口节、两处触发行与记录节各写一次）按设计选择接受：执行会话在点用处就地自足、不回跳，不判为「复述」；落字面（按 spec 资产逐字落字的文本）在实现阶段不回改，优化须另立票据。来源：grill-with-docs 会话（`.scratch/med-lit-review-entry` 02 号票评审 follow-up；判定四写／AFK 双写／同形句／P0–P3 五面分述 四类发现接受归档）。
- 修订（2026-09-17，references 分桶，ADR-0017）：路径变更为 `med-lit-review/references/protocols/placement-guide.md`。另澄清射程——本条上一条修订的「就地自足、不回跳」只解释**同一指令文件内**的点用重复；**跨文档**复述仍按本条主决定「复述处只留一句话指针」处理，`SKILL.md` 对 references 的复述属后者，已按 ADR-0017 清理（属对本条的执行，非立场反转）。
