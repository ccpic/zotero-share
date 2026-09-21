# import-runbook — RDF 回灌执行顺序、批量与校验

执行回灌分支必读。本仓库已验证实例：`tmp/restore_library.py`
（`--dry-run` 普查 / `--auth` 授权 / `--run` 执行，
配 `restore_state.json` 做 old→new 幂等映射，重跑自动跳过已建条目）。

## 先普查再写

`--dry-run` 只读统计：父条目（Article/Document）、附件（child/top）、
分类数与根分类、`unresolved_members / multi_parent / missing_child /
child_in_col / unordered_cols`、子附件 `(linkMode, 有无文件)` 分布、
附件总字节。任一项异常先停手，不要开写。

## 写入顺序

分类按层推进 → 父条目（建时直接带 `collections`）→ 独立附件上传 →
有文件子附件（逐个带 `parentid` 上传）→ 纯链接书签（`create_items`
带 `parentid`）→ `dc:relation` best-effort 回填 → 父级对账 → 计数校验。

分类必须按“就绪集循环”建：每轮只建父已落定（或本就是根）的分类，
落盘后再算下一轮。禁止按固定条数切片——同批父子一起发时，
子的 `parentCollection` 只能填空串（未知 key 占位），成功响应不会报错，
子分类静默落到根目录（曾一次错位 33 个）。

## 批量与幂等

- `create_items` 按 40 条分批，`create_collections` 按就绪集分轮、
  轮内再按 40 条分批；每次从 `successful` 按下标取新 key 并断言
  `failed` 为空；附件逐个上传。
- 每次落盘 state（old→new 映射），中断后续跑自动跳过已建条目。
- relations 写失败只记数跳过，不阻塞主流程。

## 收尾校验

- 父级对账：`scripts/audit_collections.py --map mapping.json`
  逐条比期望父与实际父，要求 `BAD=0` 才算对齐；
  有 `MISPLACED` 用 `update_collection` 挂回正确父后重跑对账。
  mapping 由回灌 state 导出：`{collections: {oldId: newKey},
  parents: {oldId: oldParentId|null}, names: {oldId: name}}`。
- 计数：断言 `len(collections())`、`count_items()==父条目+附件总数`，
  抽查父条目 `children()` 非空、关键词检索命中。
