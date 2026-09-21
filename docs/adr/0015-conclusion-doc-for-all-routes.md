# ADR-0015 所有路线皆有主要结论文档：P1 新建查新范围说明

- 日期：2026-09-17；状态：已接受；来源：grill-with-docs 会话（三轮 10 问 live exchange，用户逐项拍板）。
- 上下文：P1 查新/摸底快线现行口径为「不产包」（hitl-protocol.md 交付形态行＋P1 封面备注），止于分桶后 workspace 无主要结论文档，与 P0 八节包不对称。用户决议所有路线皆有一件人读结论文件，模板可按路线不同。
- 决定：P1 新建 `scoping-note.md`（中文称谓查新范围说明），五要素＝问题／候选计数／四桶构成／可研判性按元数据类型计数／升格建议，只写计数类数字（禁效应量数字与行号，每个数必指台账出处）；P2／P3 沿用八节包即满足一件主义；INDEX 仍唯一入口（指针不抄正文），P1 gate-summary verdict 只判计数三方一致；同落 `workspace/YYYY-MM-DD-<slug>/`，P1 进版清单按路线剪裁（无 cards／包／落点，封顶约 15 件）；`review_knowledge.py` 不动，P1 另立三条独立门，`run_gate` 验收仍只跑 P0；新词只进 `conclusion-template.md` §0 skill 内 glossary，不进 CONTEXT。
- 备选（未采纳）：全路线复用八节包（P1 硬套只造空声明）；INDEX 即结论（同一文字存两处）；P1 无机器门禁（计数一致可确定性断言，有牙才立）；旧 ADR 原地改（沿 0008/0010 先例，新决策立新 ADR，联动修订另开实施票）。
- 后果：须另开实施票修订 hitl-protocol 交付形态「P1 不产包」行、P1 封面备注去向（§8→scoping-note）、screening-log 跳步记法散点、workspace-guide 进版清单与封顶折算、新建 scoping-note 模板与 P1 三条门断言；P1 升格（Other 增回）后按终态 steps 形态改套对应模板。
