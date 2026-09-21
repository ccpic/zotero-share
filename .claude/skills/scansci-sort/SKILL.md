---
name: scansci-sort
description: 大批量文献清单先分类再下载。归一化去重、开放获取/仓储/机构分桶、输出下载队列。当用户给出大清单（xlsx/csv/bib/DOI 列表）要求分类、嗅探、分桶、再分批下载时使用。分类本身不下载全文。
---

# scansci-sort

大清单卫生与路线规划，用 `scansci-pdf` MCP 工具做轻量探测，不下载全文。

## 流程

1. `scansci_pdf_parse_list(file_path)` 归一化、去重、校验标识符。
2. `scansci_pdf_prepare_queue(action=full, candidates_json=...)` 校验 + OA 定位；`scansci_pdf_search(out_file=...)` 可直接产出带渠道预测的队列。
3. 分桶：OA 直下 / 仓储副本 / 需机构 / 灰色候选（灰色源默认启用，直接进下载竞速、不再询问）。
4. 每桶输出报告 + 队列文件 + 下一步路由（`scansci_pdf_batch_download(file=队列)`）。

## 边界

- 区分 `hit` / `miss` / `turnstile` / `blocked`，HTTP 200 不等于拿到全文。
- 分类不触发整单下载；下载只走用户确认后的队列。
- 其余边界与引文/入库分工见 `scansci-pdf` skill。
