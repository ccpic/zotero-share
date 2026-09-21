# troubleshooting — 报错速查

先对表，对不上再深挖，不要猜。

- `connector/ping` 不通：桌面端没启动。
- 403 Local API is not enabled：没勾选或没重启（见 local-api）。
- `users/0` 空数组：刚启用未重启，或真空库。
- 写报 401 `LocalAPIKeyRequiredError`：单次 key 耗尽，重走授权并点 Always Allow。
- `authorize_local()` ReadTimeout：超时内没人点弹窗，不是网络故障；按 local-api
  告知用户后单次重调，不写重试循环。
- 写前误判缺失：片段式标识符查询 0 命中≠缺失，先标题关键词 + `qmode=everything`
  跨类型核验（见 reading），快照类条目不算正式收录。
- 400 `Link mode must be set before setting attachment path`：
  见 attachments，重排键顺序。
- `CallDoesNotExistError: /items/new`：本地无模板端点，手组 dict。
- `InvalidItemFieldsError`：先 `item_type_fields` 对字段表。
- `SQLite database is locked`：改走本地 API，不要直读库文件。
- 根目录同名分类≠重复：先对源层级再定性。同名节点分属不同父时是独立
  分类（如三套体系下各一套基础研究/临床/综述），错位时用
  `update_collection` 挂回正确父，不要删除；删前先跑父级对账确认。
