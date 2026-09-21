# -*- coding: utf-8 -*-
"""覆盖账产出器：把发现层九块机制读数与指针收进单文件机器账（字段契约＝ADR-0023）。

When: 格级件已投影（`scripts/step1_diagnostics.py`）且各机制读数件与终池都落定之后——引文扩展轮／迭代补漏／
      高被引缺席核对同窗跑完、并池后重跑过 Step2。
Do: uv run python .claude/skills/med-lit-review/scripts/coverage_ledger.py \
        --delivery workspace/YYYY-MM-DD-<slug> [--report F]

读（交付内，只读）：`run/b1/step1-diagnostics.json`（格级读数唯一家，**冻结名形态**见 ADR-0023；未投影的
驼峰件会被识别并落 note 指明补投影）、`run/b1/step1-pools.json`（池级读数唯一家，`pools[]` 的 `pool_kind`）、
`run/b1/dedupe-report.json`（终池构成唯一家）、`run/b1/citation-expansion.json` 与 `citation-seeds.json`／
`gap-fill.json` 与 `gap-entities.json`／`high-cited-check.json`（机制读数件与指针目标）、
`source-declaration.json`（agent 产源集合声明，可缺）、`run/b1/selection.json`（研读选取集机器账，可缺）
与 `agent/download-queue.txt`（研读选取集逐行 DOI＝下载队列＝入库集，ADR-0032；贡献读数按它逐条归因）。

写（交付内，`run/b1/` 忽略位）：`coverage-ledger.json`——九个机制块各带 ADR-0023 冻结字段名＋`pointer`
（ADR 正文要求「每机制一块＋指向各自产物的指针」）＋**可选 `note`**（唯一允许的增量键：未跑／未取数原因、
口径注记）；末尾打印可直接入筛选日志的机制行与可直接入知识包 §8 的三段式缺口行（`机制｜读数｜缺口与补法`）。

账不复制既有读数（同一读数不得两处真源，ADR-0023）：格级读数留 `step1-diagnostics.json`、池级留
`step1-pools.json`、终池构成留 `dedupe-report.json`——账只收**机制级 rollup** 与指针；`pointer` 只列在场件
（不在场的读数件由 `null`＋`note` 交代，账不指向空处）；运行级 rollup（`failed_cell_count`／
`truncated_cell_count`／`date_unknown_candidate_count` 等）随机制块计算，不另立字段。块内读数为产出时从
读数件派生的汇总（逐通道行、逐实体表、逐格明细都不进账，只留指针）；读数件缺＝该块各读数记 ∅（`null`／空表）
＋ `note` 写原因，不编造。

口径纪律：
- `cap_hit: boolean|null`，`null` 不得读作 `false`：`truncated_cell_count` 按 `=== true` 计数，判定不可得者
  单列 `cap_hit_unknown_count`——**未取数不得写成未截断**（`total_available` 同）。
- 逐格求和的读数（日期两计数）任一格缺该读数即记 ∅（半个和会低估）。
- `pool_kind` 三值＝`检索`／`引文扩展`／`追加轮`；**缺省口径＝`检索`**（Step1 池不写该字段时按检索池计，
  引文扩展／追加轮池由各自脚本写入）。A-01 只数 `pool_kind=检索` 的池；池级读数唯一家在 `step1-pools.json`，
  账不复制。
- 研读选取块（`selection`，ADR-0032 决定 6／7）读数取自 `run/b1/selection.json`：`selected_total`／
  `must_take_count`／`rank_fill_count`／`unselected_total`／`unselected_by_rank_band`／`unselected_by_origin`／
  `classic_segment_cut`——逐条选取明细（`entries[]`）留在该件，账不复制。未选取＝终池 − 研读选取集，
  按 S 位次分档与来源池（`origin`）两类披露，**未逐条验证纳入与全文**；原「截线外条目属队列截断」的辩护句
  退场（新口径下这些条目本就没被选取，不作漏检断言）。
- 经典段被截标记（`classic_segment_cut`）改由**选取步**计算并落在 `selection.json`（＝清单项在池且归一 DOI
  不在研读选取集，集合成员判据，不用位次截线）；同一性判据（DOI 直查／词元序列唯一命中／多义不认定）唯一
  实现在 `high_cited_check.py`，账不另立第二套判据；`[]`＝实测零被截、`null`＝未取数（判定不可得项数写在该件
  `note`，账不另立键）。
- 轮空记 ∅（`null`／空表），实测零记 `0`；未取数不得写成 0，也不得写成「未选取 0 条」。
- 引文扩展块摄入过滤读数（ADR-0024）：`filtered`＝被滤条数（实测零记 `0`）；`--no-filter` 关闭记
  「未过滤」（**不得记 0**）；读数件无 `filter_policy` 记 ∅＋`note`。被滤条目**不是缺口**——不进
  `gap_reasons`，也不得读成缺席或截断；清单逐条在 `citation-expansion.json` 的 `filtered[]`，
  指针落 `filtered_pointer`（只列在场件）。
- 机制贡献归因读数（ADR-0024 配套票 03）：引文扩展／迭代补漏两块按**贡献**（条目被本机制任一池吸收过）而非
  **最先入库**（`origin`）计入内容与队列条目数——`contributed_final_count`（终池内容数）／
  `contributed_queue_count`（队列条目数）／`contributed_queue_kinds`（按贡献池类集合分列，共贡献成键）／
  `contributed_queue_origins`（按 `origin` 分列，非本机制标记者即 origin 单项口径下的少算面）／
  `contribution_pointer`（逐条归因的真源指针）。`origin` 含义不动（最先入库池，合并顶替不改口径）；
  读数从既有真源派生（终池 `pool_ids` × 池表 `pool_kind` × 队列〔＝研读选取集，ADR-0032〕），逐条归因仍只在
  终池件里、账不复制。轮空／未取数记 ∅（不得记 `0`）。

人类面（ADR-0023 四处指针化，不填表复制）：检索策略「源集合声明与覆盖账」节、筛选日志「机制记账（发现层
格读数）」节、知识包 §3 摘要行与 §8 缺口块；本脚本末尾打印的机制行与 §8 缺口行与账同源，照抄即可。

退出码：0 正常（机制未跑与零读数如实进账，不算失败）；2 用法或发现层读数件缺失／形态不符。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from step3_queue import read_json, read_queue  # noqa: E402 - 同族脚本跨件复用（容错读 JSON／队列读法同口径）
from citation_expand import STOP_BUDGET, dumps, write_atomic  # noqa: E402 - 预算判停名／原子写／JSON 落盘同口径
from citation_expand import POOL_KIND as EXPANSION_POOL_KIND  # noqa: E402 - 池类型词汇同源（引文扩展池）
from gap_fill import KIND_LABEL  # noqa: E402 - 实体四类的展示名同口径，不复制第二份
from gap_fill import POOL_KIND as GAPFILL_POOL_KIND  # noqa: E402 - 池类型词汇同源（追加轮池）

B1_DIR = "run/b1"
DIAGNOSTICS_NAME = f"{B1_DIR}/step1-diagnostics.json"
POOLS_NAME = f"{B1_DIR}/step1-pools.json"
DEDUPE_NAME = f"{B1_DIR}/dedupe-report.json"
EXPANSION_NAME = f"{B1_DIR}/citation-expansion.json"
SEEDS_NAME = f"{B1_DIR}/citation-seeds.json"
GAP_ENTITIES_NAME = f"{B1_DIR}/gap-entities.json"
GAP_FILL_NAME = f"{B1_DIR}/gap-fill.json"
HIGH_CITED_NAME = f"{B1_DIR}/high-cited-check.json"
SOURCE_DECLARATION_NAME = f"{B1_DIR}/source-declaration.json"
SELECTION_NAME = f"{B1_DIR}/selection.json"
SORT_NAME = f"{B1_DIR}/sort-report.json"
QUEUE_NAME = "agent/download-queue.txt"
REPORT_NAME = f"{B1_DIR}/coverage-ledger.json"

STATUS_SUCCESS = "success"
DEFAULT_POOL_KIND = "检索"
POOL_KIND_ORDER = (DEFAULT_POOL_KIND, EXPANSION_POOL_KIND, GAPFILL_POOL_KIND)  # 展示序（检索池先入库）
KIND_RANK = {kind: rank for rank, kind in enumerate(POOL_KIND_ORDER)}
UNKNOWN_POOL_KIND = "未知池"  # 池 id 不在池表时的贡献池类：不猜、也不并入检索池
# 源型（沿 jadense-scholar-search/src/types.ts 的 `sourceType` 标注）：精确集源超限可抬 limit／切片；
# 召回形源总数只记、不作漏检量。未知源不猜——不计入 by_source_type，落 note。
EXACT_SET_SOURCES = ("pubmed", "arxiv")
RECALL_FORM_SOURCES = ("openalex", "crossref", "semantic_scholar", "google_scholar")
CAP_POLICY_SOURCE_TYPE = {"raised_limit": "exact_set", "year_sliced": "exact_set", "recorded_only": "recall_form"}
# 检索 CLI 的驼峰格键（`queryPlan.queryStatuses`）：出现即说明格级件未投影（不是兼容分支，只作诊断提示）
CLI_CELL_KEYS = ("hitCount", "totalAvailable", "capHit", "capPolicy", "dateFilteredCount", "dateUnknownCount",
                 "dateRejectedCount", "recoveredByRetry", "cacheHit", "collectedAt")


# ---------------------------------------------------------------- 读数件取用


def delivery_relative(delivery: Path, path: Path) -> str:
    try:
        return path.relative_to(delivery).as_posix()
    except ValueError:
        return path.as_posix()


def int_or_none(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def int_or_zero(value) -> int:
    """计数位求和用：非整数（畸形态）按 0 计——读数件由同族脚本写，畸形即当噪声不计。"""
    return int_or_none(value) or 0


def row_list(value) -> list[dict]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def cells_of(diagnostics) -> list[dict]:
    """格级读数＝逐 池×源 一行（`provider` 在场）；enrich 等非格诊断不是检索格，另记 note。"""
    return [row for row in row_list(diagnostics) if str(row.get("provider") or "").strip()]


def stage_note(diagnostics, cells: list[dict]) -> str | None:
    others = [row for row in row_list(diagnostics) if not str(row.get("provider") or "").strip()]
    parts = []
    if others:
        stages = sorted({str(row["diagnostic"].get("stage") or "未知") if isinstance(row.get("diagnostic"), dict)
                         else "未知" for row in others})
        parts.append(f"格级件另有 {len(others)} 条非检索格诊断（阶段：{'／'.join(stages)}）——不进格级读数，"
                     f"缺口以其所在阶段为准")
    if any(key in row for row in cells for key in CLI_CELL_KEYS):
        parts.append("格级件仍是检索 CLI 的驼峰形态——先跑 `uv run python scripts/step1_diagnostics.py "
                     "--delivery <交付>` 投影成 ADR-0023 冻结名，再重产账")
    return "；".join(parts) if parts else None


def sum_field(cells: list[dict], key: str) -> int | None:
    """逐格求和；任一格缺该读数即记 ∅——半个和会把「未取数」读成「实测零」。"""
    if not cells or any(int_or_none(row.get(key)) is None for row in cells):
        return None
    return sum(int(row[key]) for row in cells)


def error_of(row: dict) -> str | None:
    value = row.get("error") if row.get("error") is not None else row.get("failure")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("message", "class"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key].strip()
    return None


def provider_of(record: dict) -> str:
    return str(record.get("retrievalProvider") or record.get("externalSource") or "")


def pool_candidates(pools_data: dict, pool_id: str) -> list[dict]:
    for pool in row_list(pools_data.get("pools")):
        if pool.get("pool_id") == pool_id:
            return row_list(pool.get("results")) or row_list(pool.get("items"))
    return []


# ---------------------------------------------------------------- 机制贡献归因（ADR-0024 配套票 03）


def pool_kind_index(pools_data: dict) -> dict[str, str]:
    """池 id → `pool_kind`；缺省口径＝检索池（与池分量／A-01 同一缺省：Step1 池不写该字段即按检索计）。"""
    index: dict[str, str] = {}
    for pool in row_list(pools_data.get("pools")):
        index[str(pool.get("pool_id") or "pool")] = str(pool.get("pool_kind") or "").strip() or DEFAULT_POOL_KIND
    return index


def kind_order(kind: str) -> tuple[int, str]:
    """池类的定序键：三值固定序，池表以外的类排在其后（同一集合的标签跨交付可比）。"""
    return (KIND_RANK.get(kind, len(KIND_RANK)), kind)


def kind_label(kinds) -> str:
    """贡献池类集合的标签（定序去重后以「＋」连接）；尺寸 >1 即多机制共贡献。"""
    return "＋".join(sorted(set(kinds), key=kind_order)) or "∅"


def contributing_kinds(rec: dict, pool_kinds: dict[str, str]) -> list[str]:
    """一条终池记录的贡献池类集合（真源＝`pool_ids`）：池 id 不在池表里记 `未知池`，不猜、不并入检索池。"""
    return sorted({pool_kinds.get(str(pool_id), UNKNOWN_POOL_KIND)
                   for pool_id in rec.get("pool_ids") or []}, key=kind_order)


def no_contribution() -> dict:
    """贡献读数轮空／未取数时的 ∅ 形态（不得记 0——实测零与未取数是两回事）。"""
    return {"contributed_final_count": None, "contributed_queue_count": None,
            "contributed_queue_kinds": None, "contributed_queue_origins": None, "contribution_pointer": []}


def contribution_readings(kind: str, delivery: Path, pools_data: dict, dedupe) -> tuple[dict, list[str]]:
    """本机制（`pool_kind=kind`）的贡献归因读数与注记：内容数／队列条目数／按贡献池类集合分列／按最先入库池分列。

    口径＝「条目被本机制任一池吸收过」（同一 DOI 只计一次；与检索池共贡献者同计）：
    内容数＝终池**记录**里 `pool_ids` 含本机制池者（与 `final_count`＝终池条目数同口径）；
    队列条目数＝队列**唯一 DOI** 行里、该 DOI 在终池全部记录的贡献池**并集**含本机制池者
    （未裁定的「DOI 同题名异」对逐条保留，并集不丢任一来源；该口径在 ADR-0024 参考态上复现
    会话的「仅检索池 114／扩展池有 136」）。`origin` 仍是**最先入库池**（合并顶替不改口径），
    取该 DOI 最先入库的那条记录——`contributed_queue_origins` 里非本机制标记者即 origin 单项口径的少算面。
    真源＝终池 `final_records[].pool_ids` × `step1-pools.json` 的 `pool_kind` × 队列（读法同
    `step3_queue.read_queue`）；账只留派生读数与指针，「某条队列条目的全部贡献池」按
    `contribution_pointer` 逐条查 `pool_ids`。**不在终池记录上新增 `contributing_pools[]`／
    `contributing_kinds[]`**：`pool_ids` 已是逐条真源（`load_pools` 播种、`absorb()` 取并集），
    再加字段＝同一事实写两处（ADR-0023「同一读数不得两处真源」）且需在播种／合并／顶替三处同步。
    读不到即记 ∅＋`note`（不得记 0）：终池件缺 `final_records`／记录无 `pool_ids` 键（旧形态件或
    段缓存回放的旧段）／队列件缺。
    """
    fields, notes = no_contribution(), []
    raw_records = dedupe.get("final_records") if isinstance(dedupe, dict) else None
    if not isinstance(raw_records, list):   # 键不在场或形态不符＝未取数；键在场且为空表才是实测零
        return fields, [f"贡献读数未取数（{DEDUPE_NAME} 缺 `final_records` 或形态不符）——不得记 0"]
    records = row_list(raw_records)
    bare = sum(1 for row in records if "pool_ids" not in row)
    if records and bare == len(records):
        return fields, [f"贡献读数未取数（终池记录无 `pool_ids` 键——旧形态件或段缓存回放的旧段）——不得记 0"]
    queue_path = delivery / QUEUE_NAME
    if not queue_path.is_file():
        return fields, [f"贡献读数未取数（{QUEUE_NAME} 缺）——不得记 0"]
    pool_kinds = pool_kind_index(pools_data)
    final_kinds = [contributing_kinds(row, pool_kinds) for row in records]
    # 一个 DOI 在终池可有多条记录（未裁定的「DOI 同题名异」对逐条保留、不合并）：队列一行一个 DOI，
    # 故贡献池取该 DOI 全部记录的并集（不丢任一来源），`origin` 取最先入库的那条记录。
    at_doi: dict[str, dict] = {}
    for index, row in enumerate(records):
        doi = str(row.get("doi") or "")
        if not doi:
            continue
        slot = at_doi.setdefault(doi, {"kinds": set(), "origin": str(row.get("origin") or "").strip(),
                                       "records": 0})
        slot["kinds"] |= set(final_kinds[index])
        slot["records"] += 1
    mine: list[tuple[list[str], str]] = []
    unmatched = multi = nonpool = 0
    for key in read_queue(queue_path):
        slot = at_doi.get(key)
        if slot is None:      # 键非 DOI（中文三段键）或该 DOI 无终池记录：归因不成立，单列不猜
            unmatched += 1
            continue
        if slot["records"] > 1:
            multi += 1
        kinds = sorted(slot["kinds"], key=kind_order)
        if not kinds:      # 终池无任何来源池（如 B1 中文补充记录 `pool_ids: []`）：不属于任何机制
            nonpool += 1
            continue
        if kind in kinds:
            mine.append((kinds, slot["origin"]))
    kind_counts = Counter(kind_label(kinds) for kinds, _origin in mine)
    origin_counts = Counter(origin or "未知" for _kinds, origin in mine)
    fields.update({
        "contributed_final_count": sum(1 for kinds in final_kinds if kind in kinds),
        "contributed_queue_count": len(mine),
        "contributed_queue_kinds": {label: count for label, count
                                    in sorted(kind_counts.items(), key=lambda kv: (-kv[1], kv[0]))},
        "contributed_queue_origins": {name: count for name, count
                                      in sorted(origin_counts.items(), key=lambda kv: (-kv[1], kv[0]))},
        "contribution_pointer": [name for name in (DEDUPE_NAME, POOLS_NAME, QUEUE_NAME)
                                 if (delivery / name).is_file()],
    })
    if unmatched:
        notes.append(f"贡献读数：队列 {unmatched} 条未在终池记录里匹配到 DOI（键非 DOI 或该 DOI 无终池记录）——未计入，不得读成「无贡献」")
    if nonpool:
        notes.append(f"贡献读数：队列 {nonpool} 条在终池无任何 `pool_ids`（非池来源，如 B1 中文补充）"
                     f"——未计入任何机制，勿按「队列 − 各机制贡献＝仅检索池」的差式读成检索池独占")
    if bare:      # 形态混合：带键者照算，无键者按「无来源池」计但必须留痕（不静默当零）
        notes.append(f"贡献读数：终池 {bare} 条记录无 `pool_ids` 键（形态混合）——按「无来源池」计，"
                     f"该部分贡献不可归因")
    if multi:
        notes.append(f"贡献读数：队列 {multi} 条 DOI 在终池里有多条记录（未裁定的「DOI 同题名异」对）"
                     "——贡献池按该 DOI 全部记录的并集计，`origin` 取最先入库的那条")
    if any(UNKNOWN_POOL_KIND in kinds for kinds in final_kinds):
        notes.append(f"贡献读数：有池 id 不在 {POOLS_NAME}（池类记 `{UNKNOWN_POOL_KIND}`，不并入检索池）")
    notes.append("贡献口径＝条目被本机制任一池吸收过（同一 DOI 只计一次；与检索池共贡献者同计），"
                 "按池类集合分列的键即共贡献组合；`origin` 仍记最先入库池（合并顶替不改口径）")
    return fields, notes


def contribution_text(block: dict) -> str:
    """贡献读数的人读片段（机制行与 §8 读数同源，格式只此一份）。"""
    if block.get("contributed_queue_count") is None:
        return "贡献 未取数"
    kinds = block.get("contributed_queue_kinds") or {}
    origins = block.get("contributed_queue_origins") or {}
    kinds_text = "／".join(f"{label} {count}" for label, count in kinds.items()) or "∅"
    origins_text = "／".join(f"{name} {count}" for name, count in origins.items()) or "∅"
    return (f"贡献 终池 {show(block.get('contributed_final_count'))} 条／队列 "
            f"{block['contributed_queue_count']} 条（按贡献池类 {kinds_text}；按最先入库池 {origins_text}）")


CONTRIBUTION_NOTE = ("贡献读数为机制归因口径（条目被本机制任一池吸收过），与 `origin`（最先入库池）并置："
                     "两者都不进缺口的判据；「按最先入库池」里非本机制标记者即 origin 单项口径下的少算面。")


# ---------------------------------------------------------------- 九块

def build_citation_expansion(delivery: Path, data, pools_data: dict, dedupe) -> dict:
    """引文扩展：种子／方向／逐通道请求与命中（按通道汇总）／新增去重前后／判停／缺口原因／摄入过滤读数／贡献归因读数。

    摄入过滤（ADR-0024）：`filtered`＝被滤条数（实测零记 0；`--no-filter` 关闭记「未过滤」不记 0；
    读数件缺该口径记 ∅）；`filtered_pointer`＝被滤清单所在在场件的指针（子集语义：冻结名须在场、
    pointer 只列在场件；清单逐条在 `citation-expansion.json` 的 `filtered[]`）。被滤条目**不是**缺口：
    不进 `gap_reasons`（不进「缺席」，也不进「截断」）。

    摘要重建（票 02）：读数件 `abstract_rebuild` 只在块 `note` 内指路——账键集＝ADR-0023 冻结名，
    不为它新增字段，故门禁的冻结名元组与 §8 分句数字都不动（全量读数在读数件内）。

    贡献归因（ADR-0024 配套票 03）：`contributed_final_count` 与贡献池类集合两读数＋`contributed_queue_origins`
    ＋`contribution_pointer` 等冻结名——该轮未跑或读数取不到记 ∅，绝不记 0。
    """
    pointer = [EXPANSION_NAME, SEEDS_NAME]
    if not isinstance(data, dict):
        return {"ran": False, "seed_head": None, "seed_judged": None, "directions": [], "channels": [],
                "new_before_dedupe": None, "new_after_dedupe": None, "stop_reason": None, "gap_reasons": [],
                "filtered": None, "filtered_pointer": [], **no_contribution(), "pointer": pointer,
                "note": f"读数件缺或形态不符（{EXPANSION_NAME}）——该轮未跑，各读数记 ∅"}
    rows = row_list(data.get("channels"))
    source = data.get("seed_source") if isinstance(data.get("seed_source"), dict) else {}
    budget = data.get("budget") if isinstance(data.get("budget"), dict) else {}
    records = data.get("new_records") if isinstance(data.get("new_records"), dict) else {}
    policy = data.get("filter_policy") if isinstance(data.get("filter_policy"), dict) else None
    enabled = policy.get("enabled") if policy is not None else None
    listed = data.get("filtered")
    filtered = row_list(listed)
    rebuild = data.get("abstract_rebuild") if isinstance(data.get("abstract_rebuild"), dict) else None
    notes: list[str] = []
    if enabled is None:
        filtered_reading: int | str | None = None
        filtered_pointer = []
        notes.append(f"摄入过滤读数未取数（{EXPANSION_NAME} 无 `filter_policy`）——不得写成未过滤或 0")
    elif enabled is False:
        filtered_reading = "未过滤"
        filtered_pointer = []
        notes.append("摄入过滤未过滤（`--no-filter`）：被滤读数记「未过滤」，不记 0")
    elif not isinstance(listed, list):   # 键不在场＝未取数，不得写成 0（实测零记 0）
        filtered_reading = None
        filtered_pointer = []
        notes.append("摄入过滤清单未取数（读数件有 `filter_policy` 但无 `filtered[]`）——不得写成 0")
    else:
        filtered_reading = len(filtered)
        filtered_pointer = [EXPANSION_NAME] if (delivery / EXPANSION_NAME).is_file() else []
        notes.append("filtered 计数口径＝被滤**取回行**（逐条 `filtered[]` 一条一行；同一 DOI 跨通道／"
                     "跨种子重号各计一次，故与「唯一 DOI」口径及「扩展独占」子集口径不等值——"
                     "ADR-0024 后果段的「扩展独占 1322 条中 194 条被滤」是后者）")
    if rebuild is not None:
        # 摘要重建（票 02）读数按需引用：只指路、不抄数字（ADR-0023「同一读数不得两处真源」），
        # 也不增冻结名（账键集＝ADR-0023 冻结名），故门禁冻结元组与 §8 分句数字都不动
        notes.append(f"扩展记录摘要重建读数见 {EXPANSION_NAME} 的 abstract_rebuild"
                     f"（可重建／通道原样／无摘要三分）——扩展记录的 `s_rel`／`comp` 输入由此可用")
    contribution, contribution_notes = contribution_readings(EXPANSION_POOL_KIND, delivery, pools_data,
                                                                  dedupe)
    notes.extend(contribution_notes)
    channels: list[dict] = []
    seen: dict[str, dict] = {}
    for row in rows:
        name = str(row.get("channel") or "").strip()
        if not name:
            continue
        slot = seen.get(name)
        if slot is None:
            slot = {"channel": name, "requests": 0, "hits": 0}
            seen[name] = slot
            channels.append(slot)
        slot["requests"] += int_or_zero(row.get("requests"))
        slot["hits"] += int_or_zero(row.get("hits"))
    directions = [label for code, label in (("bwd", "后向"), ("fwd", "前向"))
                  if any(row.get("direction") == code for row in rows)]
    gaps: list[str] = []
    failed = [row for row in rows if row.get("status") == "failed"]
    if failed:
        classes = sorted({str((row.get("failure") or {}).get("class") or "未知") for row in failed})
        gaps.append(f"失败 {len(failed)} 通道（{'／'.join(classes)}）")
    truncated = [row for row in rows if row.get("truncated") is True]
    if truncated:
        gaps.append(f"截断 {len(truncated)} 通道（每种子每方向上限内截取，可及数逐格见读数件）")
    dropped = sum(int_or_zero(row.get("dropped_no_doi")) for row in rows)
    if dropped:
        gaps.append(f"无标识丢弃 {dropped} 条（无 DOI 题录按 Step2 一级规则禁入持久化，只记读数）")
    if records.get("new_to_pools") == 0:
        gaps.append("零新增（入池新增 0 条）")
    if not int_or_none(source.get("judgment_valid")):
        gaps.append("判定集 0 条（无指南/SR 种子，只跑头部位次）")
    if budget.get("aborted") is True:
        gaps.append("该轮中止（全通道不可用），扩展池不并池")
    elif budget.get("stop_reason") == STOP_BUDGET:
        gaps.append(f"预算耗尽（请求 {budget.get('used')}／{budget.get('limit')}），未跑格带原因记入读数件")
    return {"ran": True, "seed_head": int_or_none(source.get("head_taken")),
            "seed_judged": int_or_none(source.get("judgment_valid")),
            "directions": directions, "channels": channels,
            "new_before_dedupe": int_or_none(records.get("before_dedupe")),
            "new_after_dedupe": int_or_none(records.get("after_dedupe")),
            "stop_reason": budget.get("stop_reason") if isinstance(budget.get("stop_reason"), str) else None,
            "filtered": filtered_reading, "filtered_pointer": filtered_pointer,
            "gap_reasons": gaps, **contribution, "pointer": pointer,
            **({"note": "；".join(notes)} if notes else {})}


def build_gap_fill(delivery: Path, data, pools_data: dict, dedupe) -> dict:
    """迭代补漏：盘点实体数／缺口实体数／波与 query 数／新增／闭合与未闭合实体；贡献归因口径同引文扩展块。"""
    pointer = [GAP_FILL_NAME, GAP_ENTITIES_NAME]
    mechanism = data.get("mechanism") if isinstance(data, dict) else None
    if not isinstance(mechanism, dict):
        return {"ran": False, "entity_checklist_count": None, "gap_entity_count": None, "waves": None,
                "query_count": None, "new_before_dedupe": None, "new_after_dedupe": None, "closure": None,
                "unclosed_entities": [], **no_contribution(), "pointer": pointer,
                "note": f"读数件缺或形态不符（{GAP_FILL_NAME}）——该轮未跑，各读数记 ∅"}
    unclosed = [{"name": str(row.get("name") or ""),
                 "kind": KIND_LABEL.get(str(row.get("kind") or ""), str(row.get("kind") or "")),
                 "reason": str(row.get("reason") or ""),
                 "queries": [str(item) for item in row.get("queries") or []],
                 "waves": [item for item in row.get("waves") or [] if isinstance(item, int)]}
                for row in row_list(mechanism.get("unclosed_entities"))]
    ran = mechanism.get("ran") is True
    notes = [] if ran else ["读数件记 ran=false（该轮未执行）——贡献读数记 ∅"]
    contribution = no_contribution()
    if ran:      # 轮空记 ∅（ADR-0023）：未跑不是实测零
        contribution, contribution_notes = contribution_readings(GAPFILL_POOL_KIND, delivery, pools_data,
                                                                  dedupe)
        notes.extend(contribution_notes)
    return {"ran": ran, "entity_checklist_count": int_or_none(mechanism.get("entity_checklist_count")),
            "gap_entity_count": int_or_none(mechanism.get("gap_entity_count")),
            "waves": int_or_none(mechanism.get("waves")),
            "query_count": int_or_none(mechanism.get("query_count")),
            "new_before_dedupe": int_or_none(mechanism.get("new_before_dedupe")),
            "new_after_dedupe": int_or_none(mechanism.get("new_after_dedupe")),
            "closure": mechanism.get("closure") if isinstance(mechanism.get("closure"), str) else None,
            "unclosed_entities": unclosed, **contribution, "pointer": pointer,
            **({"note": "；".join(notes)} if notes else {})}


def build_high_cited(data) -> dict:
    """高被引缺席核对：查询与深度／缺席分列／用户处置（只核对不入池）。"""
    pointer = [HIGH_CITED_NAME]
    mechanism = data.get("mechanism") if isinstance(data, dict) else None
    if not isinstance(mechanism, dict):
        return {"ran": False, "queries": [], "absent_total": None, "absent_relevant": None,
                "absent_off_topic": None, "absent_out_of_window": None, "user_disposition": None,
                "pointer": pointer,
                "note": f"读数件缺或形态不符（{HIGH_CITED_NAME}）——该轮未跑，各读数记 ∅"}
    queries = [{"query": str(row.get("query") or ""), "depth": int_or_none(row.get("depth"))}
               for row in row_list(mechanism.get("queries"))]
    ran = mechanism.get("ran") is True
    return {"ran": ran, "queries": queries,
            "absent_total": int_or_none(mechanism.get("absent_total")),
            "absent_relevant": int_or_none(mechanism.get("absent_relevant")),
            "absent_off_topic": int_or_none(mechanism.get("absent_off_topic")),
            "absent_out_of_window": int_or_none(mechanism.get("absent_out_of_window")),
            "user_disposition": mechanism.get("user_disposition")
            if isinstance(mechanism.get("user_disposition"), str) else None,
            "pointer": pointer, **({} if ran else {"note": "读数件记 ran=false（该轮未执行）"})}


def build_classic_segment(data) -> dict:
    """经典段：核出与未达（未达逐条带原因）；被截标记在队列块。"""
    pointer = [HIGH_CITED_NAME]
    block = data.get("classic_segment") if isinstance(data, dict) else None
    if not isinstance(block, dict):
        return {"checked": None, "found": None, "unrecovered": [], "pointer": pointer,
                "note": f"读数件缺或形态不符（{HIGH_CITED_NAME}）——经典段未取数，核出与未达都记 ∅"}
    unrecovered = [{"id": str(row.get("id") or ""), "reason": str(row.get("reason") or "")}
                   for row in row_list(block.get("unrecovered"))]
    note = None if int_or_none(block.get("checked")) else "本次未给经典清单（经典段轮空）"
    return {"checked": int_or_none(block.get("checked")), "found": int_or_none(block.get("found")),
            "unrecovered": unrecovered, "pointer": pointer, **({"note": note} if note else {})}


def build_source_failure(cells: list[dict], pools_data: dict) -> dict:
    """源失败：失败格与重试取回格逐格成行，净影响如实记（未取到即不猜条数）。"""
    failed = [row for row in cells if str(row.get("status") or "") != STATUS_SUCCESS]
    recovered = [row for row in cells if row.get("recovered_by_retry") is True]
    rows: list[dict] = []
    for row in failed + recovered:
        recovered_flag = row.get("recovered_by_retry") is True
        pool_id = str(row.get("pool_id") or "")
        provider = str(row.get("provider") or "")
        if recovered_flag:
            net_effect = "重试取回（无净损失）"
        else:
            others = [rec for rec in pool_candidates(pools_data, pool_id)
                      if not provider or provider_of(rec) != provider]
            net_effect = (f"未知（该格未取到；同池其余源 {len(others)} 条候选兜底）" if others
                          else "整池缺（该格为池内唯一源，该池候选 0 条）")
        rows.append({"pool": pool_id, "provider": provider, "error": error_of(row),
                     "attempts": int_or_none(row.get("attempts")), "recovered": recovered_flag,
                     "net_effect": net_effect})
    return {"failed_cell_count": len(failed), "recovered_cell_count": len(recovered), "cells": rows,
            "pointer": [DIAGNOSTICS_NAME]}


def source_type_of(row: dict) -> str | None:
    policy = row.get("cap_policy")
    if policy in CAP_POLICY_SOURCE_TYPE:
        return CAP_POLICY_SOURCE_TYPE[policy]
    provider = str(row.get("provider") or "").strip()
    if provider in EXACT_SET_SOURCES:
        return "exact_set"
    if provider in RECALL_FORM_SOURCES:
        return "recall_form"
    return None


def build_cap_truncation(cells: list[dict]) -> dict:
    """命中上限：`=== true` 计数＋按源型分列＋判定不可得单列（null 不得读作 false）。"""
    truncated = [row for row in cells if row.get("cap_hit") is True]
    unknown = [row for row in cells if row.get("cap_hit") not in (True, False)]
    by_type = {"exact_set": 0, "recall_form": 0}
    unmapped = [str(row.get("provider") or "未知") for row in truncated
                if source_type_of(row) is None]
    for row in truncated:
        kind = source_type_of(row)
        if kind is not None:
            by_type[kind] += 1
    notes = []
    if unmapped:
        notes.append(f"命中上限：截断格源型不可判 {len(unmapped)} 格（{'／'.join(sorted(set(unmapped)))}）"
                     f"未计入 by_source_type——源型表只认已知源，不猜")
    if unknown:
        notes.append(f"cap_hit 判定不可得 {len(unknown)} 格（未取数，不得读作未截断）")
    return {"truncated_cell_count": len(truncated), "by_source_type": by_type,
            "cap_hit_unknown_count": len(unknown), "pointer": [DIAGNOSTICS_NAME],
            **({"note": "；".join(notes)} if notes else {})}


def build_date_filter(cells: list[dict]) -> dict:
    """日期过滤：无日期候选（带标记保留）与越窗候选（丢弃并计数）两读数。"""
    unknown = sum_field(cells, "date_unknown_count")
    window = sum_field(cells, "date_rejected_count")
    note = None
    if unknown is None or window is None:
        note = ("逐格读数缺 `date_unknown_count`／`date_rejected_count`（格级件未投影或该读数未取）"
                "——两计数记 ∅，未取数不得写成 0")
    return {"date_unknown_candidate_count": unknown, "out_of_window_candidate_count": window,
            "pointer": [DIAGNOSTICS_NAME], **({"note": note} if note else {})}


def build_source_declaration(declaration, expansion, pools_data: dict, dedupe) -> dict:
    """源声明：在组源／未纳入源与原因／GS 启用位／无稳定身份剔除数。"""
    pointer = [SOURCE_DECLARATION_NAME, POOLS_NAME, DEDUPE_NAME]
    declared = [str(item).strip() for item in (declaration.get("sources") or [])
                if str(item).strip()] if isinstance(declaration, dict) else []
    mechanism_sources: list[str] = []
    for row in row_list(expansion.get("channels")) if isinstance(expansion, dict) else []:
        name = str(row.get("source") or "").strip()
        if name and name not in mechanism_sources:
            mechanism_sources.append(name)
    pools_sources = [str(item).strip() for item in (pools_data.get("providers") or []) if str(item).strip()]
    notes: list[str] = []
    if declared:
        sources = declared
    else:
        sources = sorted(set(pools_sources) | set(mechanism_sources))
        notes.append("源声明件缺（run/b1/source-declaration.json）：在组源取发现清单 `providers`"
                     f"（{'／'.join(pools_sources) or '∅'}）与引文扩展轮通道源（{'／'.join(mechanism_sources) or '∅'}）；"
                     f"`gs_enabled` 与未纳入源记 ∅，不得写成「未启用」")
    not_included = [{"source": str(row.get("source") or "").strip(), "reason": str(row.get("reason") or "").strip()}
                    for row in row_list(declaration.get("not_included"))
                    if str(row.get("source") or "").strip()] if isinstance(declaration, dict) else []
    gs_enabled = declaration.get("gs_enabled") if isinstance(declaration, dict) else None
    if not isinstance(gs_enabled, bool):
        gs_enabled = None
    identity = dedupe.get("identity_rejected") if isinstance(dedupe, dict) else None
    return {"sources": sources, "not_included": not_included, "gs_enabled": gs_enabled,
            "identity_rejected_count": len(identity) if isinstance(identity, list) else None,
            "pointer": pointer, **({"note": "；".join(notes)} if notes else {})}


def build_selection(selection) -> dict:
    """研读选取（ADR-0032）：读数取自 `run/b1/selection.json`——选取总数／必取／补足／未选取与两类分列／
    经典段被截；逐条选取明细留该件 `entries[]`，账不复制。被截判据＝清单项在池且不在研读选取集（集合成员
    判据，不用位次截线）；同一性判定（DOI 直查／词元序列唯一命中／多义不认定）仍在 `high_cited_check.py`。"""
    pointer = [SELECTION_NAME, SORT_NAME, DEDUPE_NAME]
    if not isinstance(selection, dict):
        return {"selected_total": None, "must_take_count": None, "rank_fill_count": None,
                "unselected_total": None, "unselected_by_rank_band": None, "unselected_by_origin": None,
                "classic_segment_cut": None, "pointer": pointer,
                "note": f"读数件缺或形态不符（{SELECTION_NAME}）——研读选取与未选取各读数记 ∅，不得写成 0"}
    classic_cut = selection.get("classic_segment_cut")
    classic_cut = list(classic_cut) if isinstance(classic_cut, list) else None
    bands = selection.get("unselected_by_rank_band")
    origins = selection.get("unselected_by_origin")
    notes: list[str] = ["口径＝研读选取集（ADR-0032：下载队列＝研读选取集＝入库集）；未选取＝终池 − 研读选取集，"
                        "按 S 位次分档与来源池分列披露，不作漏检断言；逐条选取明细与判定不可得项数见 "
                        f"{SELECTION_NAME} 的 `entries[]`／`note`，账不复制"]
    if int_or_none(selection.get("unselected_total")) is None:
        notes.append("未选取未取数（" + ("读数件缺 `unselected_total`" if "unselected_total" not in selection
                                        else "`unselected_total` 记 null")
                     + "）——不得写成 0")
    if classic_cut is None:
        notes.append(f"经典段被截标记未取数（{SELECTION_NAME} 的 `classic_segment_cut` 记 null——"
                     "无高被引读数件或判定不可得）——不得写成未被截")
    return {"selected_total": int_or_none(selection.get("selected_total")),
            "must_take_count": int_or_none(selection.get("must_take_count")),
            "rank_fill_count": int_or_none(selection.get("rank_fill_count")),
            "unselected_total": int_or_none(selection.get("unselected_total")),
            "unselected_by_rank_band": dict(bands) if isinstance(bands, dict) else None,
            "unselected_by_origin": dict(origins) if isinstance(origins, dict) else None,
            "classic_segment_cut": classic_cut, "pointer": pointer, "note": "；".join(notes)}


# ---------------------------------------------------------------- 账组装


def build_ledger(delivery: Path) -> dict:
    diagnostics = read_json(delivery / DIAGNOSTICS_NAME)
    pools_data = read_json(delivery / POOLS_NAME)
    if not isinstance(diagnostics, list):
        raise ValueError(f"格级读数件缺或形态不符（需列表）：{DIAGNOSTICS_NAME}")
    if not isinstance(pools_data, dict) or not isinstance(pools_data.get("pools"), list):
        raise ValueError(f"池级读数件缺或形态不符（需 {{\"pools\": […]}}）：{POOLS_NAME}")
    dedupe = read_json(delivery / DEDUPE_NAME)
    high_cited = read_json(delivery / HIGH_CITED_NAME)
    expansion = read_json(delivery / EXPANSION_NAME)
    gap_report = read_json(delivery / GAP_FILL_NAME)
    declaration = read_json(delivery / SOURCE_DECLARATION_NAME)
    selection = read_json(delivery / SELECTION_NAME)
    cells = cells_of(diagnostics)

    notes = []
    stage = stage_note(diagnostics, cells)
    if stage:
        notes.append(stage)
    ledger = {
        "generated_at": date.today().isoformat(),
        "delivery": delivery.as_posix(),
        "contract": "ADR-0023",
        "notes": notes,
        "mechanisms": {
            "citation_expansion": build_citation_expansion(delivery, expansion, pools_data, dedupe),
            "iterative_gap_fill": build_gap_fill(delivery, gap_report, pools_data, dedupe),
            "high_cited_absence": build_high_cited(high_cited),
            "classic_segment": build_classic_segment(high_cited),
            "source_failure": build_source_failure(cells, pools_data),
            "cap_truncation": build_cap_truncation(cells),
            "date_filter": build_date_filter(cells),
            "source_declaration": build_source_declaration(declaration, expansion, pools_data, dedupe),
            "selection": build_selection(selection),
        },
        "report_path": delivery_relative(delivery, delivery / REPORT_NAME),
    }
    # 指针只列在场件：不在场的读数件由 ∅＋note 交代，账不指向空处。
    for block in ledger["mechanisms"].values():
        block["pointer"] = [name for name in block["pointer"] if (delivery / name).is_file()]
    return ledger


# ---------------------------------------------------------------- 机制行与 §8 缺口行


def show(value) -> str:
    return "—" if value is None else str(value)


def block_note(block: dict) -> str:
    return block.get("note") or ""


def bounded(rows: list[str], limit: int, pointer: str) -> str:
    """人类面逐条明细封顶：超出部分指向读数件（机读面照旧全量在账）。"""
    if len(rows) <= limit:
        return "；".join(rows)
    return "；".join(rows[:limit]) + f"；余 {len(rows) - limit} 条见 {pointer}"


def unselected_text(block: dict) -> str:
    """研读选取块的读数片段（机制行与 §8 读数同源，格式只此一份）：未选取＝终池 − 研读选取集。

    终池条目数在账内派生（研读选取 ＋ 未选取；账不另存终池键，「同一读数不得两处真源」）；未取数记
    「未取数」，不写 0。"""
    total = block.get("unselected_total")
    if not isinstance(total, int):
        return "未取数"
    selected = block.get("selected_total")
    pool = total + selected if isinstance(selected, int) else None
    return f"{total} 条（终池 {show(pool)} − 研读选取 {show(selected)}）"


def breakdown_text(value) -> str:
    """分档／分来源读数渲染：逐键列值；未取数记 ∅（不得写成全零），空表（实测零）记 0。"""
    if not isinstance(value, dict):
        return "∅"
    return "／".join(f"{key} {count}" for key, count in value.items()) or "0"


def mechanism_lines(ledger: dict) -> list[str]:
    """可直接入筛选日志「机制记账」节的机制行（读数与账同源；未跑记「未跑」不记 0）。"""
    mech = ledger["mechanisms"]
    expansion, gap_fill = mech["citation_expansion"], mech["iterative_gap_fill"]
    absent, classic = mech["high_cited_absence"], mech["classic_segment"]
    failure, cap = mech["source_failure"], mech["cap_truncation"]
    dates, declaration, selection = mech["date_filter"], mech["source_declaration"], mech["selection"]
    lines: list[str] = []
    if expansion["ran"]:
        requests = sum(c["requests"] for c in expansion["channels"])
        hits = sum(c["hits"] for c in expansion["channels"])
        filtered_text = (f"被滤 {show(expansion['filtered'])} 条"
                         if isinstance(expansion["filtered"], int) else show(expansion["filtered"]))
        lines.append(f"覆盖账：引文扩展轮 种子 {show(expansion['seed_head'])}（头部）／"
                     f"{show(expansion['seed_judged'])}（判定集）｜通道 {len(expansion['channels'])} 个"
                     f"（请求 {requests}／命中 {hits}）｜新增 去重前 {show(expansion['new_before_dedupe'])}／"
                     f"去重后 {show(expansion['new_after_dedupe'])}｜摄入过滤 {filtered_text}"
                     f"｜判停 {show(expansion['stop_reason'])}｜{contribution_text(expansion)}")
    else:
        lines.append(f"覆盖账：引文扩展轮 未跑（{block_note(expansion)}）")
    if gap_fill["ran"]:
        lines.append(f"覆盖账：迭代补漏 实体清单 {show(gap_fill['entity_checklist_count'])}／疑似缺口 "
                     f"{show(gap_fill['gap_entity_count'])}｜波 {show(gap_fill['waves'])}／query "
                     f"{show(gap_fill['query_count'])}｜新增 去重前 {show(gap_fill['new_before_dedupe'])}／"
                     f"去重后 {show(gap_fill['new_after_dedupe'])}｜{show(gap_fill['closure'])}｜未闭合 "
                     f"{len(gap_fill['unclosed_entities'])}｜{contribution_text(gap_fill)}")
    else:
        lines.append(f"覆盖账：迭代补漏 未跑（{block_note(gap_fill)}）")
    if absent["ran"]:
        lines.append(f"覆盖账：高被引缺席核对 查询 {len(absent['queries'])} 组｜缺席 "
                     f"{show(absent['absent_total'])}（相关 {show(absent['absent_relevant'])}／超题 "
                     f"{show(absent['absent_off_topic'])}／窗外 {show(absent['absent_out_of_window'])}）｜"
                     f"处置 {show(absent['user_disposition'])}")
    else:
        lines.append(f"覆盖账：高被引缺席核对 未跑（{block_note(absent)}）")
    if classic["checked"] is None:
        lines.append(f"覆盖账：经典段 未取数（{block_note(classic)}）")
    else:
        lines.append(f"覆盖账：经典段 清单 {classic['checked']} 条｜核出 {show(classic['found'])}／未达 "
                     f"{len(classic['unrecovered'])}")
    lines.append(f"覆盖账：格读数 失败 {failure['failed_cell_count']} 格／重试取回 "
                 f"{failure['recovered_cell_count']} 格｜命中截断 {cap['truncated_cell_count']} 格（精确集源 "
                 f"{cap['by_source_type']['exact_set']}／召回形源 {cap['by_source_type']['recall_form']}）"
                 f"｜cap_hit 判定不可得 {cap['cap_hit_unknown_count']} 格｜无日期候选 "
                 f"{show(dates['date_unknown_candidate_count'])}／越窗候选 "
                 f"{show(dates['out_of_window_candidate_count'])}")
    lines.append(f"覆盖账：源声明 在组 {'／'.join(declaration['sources']) or '∅'}｜未纳入 "
                 f"{len(declaration['not_included'])}｜Google Scholar {show(declaration['gs_enabled'])}"
                 f"｜无稳定身份剔除 {show(declaration['identity_rejected_count'])} 条")
    if not isinstance(selection["unselected_total"], int) and not isinstance(selection["selected_total"], int):
        lines.append(f"覆盖账：研读选取 未取数（{block_note(selection)}）")
    else:
        lines.append(f"覆盖账：研读选取 {show(selection['selected_total'])} 条（必取 "
                     f"{show(selection['must_take_count'])}／补足 {show(selection['rank_fill_count'])}）｜"
                     f"未选取 {unselected_text(selection)}（按 S 位次分档 "
                     f"{breakdown_text(selection['unselected_by_rank_band'])}／按来源池 "
                     f"{breakdown_text(selection['unselected_by_origin'])}）")
    return lines


def gap_lines(ledger: dict) -> list[tuple[str, str, str]]:
    """§8「覆盖缺口」块的三段式（机制｜读数｜缺口与补法）——读数为账上同一批数字，块 note 原文附后；
    第 10 句「未选取」句式逐字固定（ADR-0032），其 ∅ 与口径注记留在账内 `note`。"""
    mech = ledger["mechanisms"]
    expansion, gap_fill = mech["citation_expansion"], mech["iterative_gap_fill"]
    absent, classic = mech["high_cited_absence"], mech["classic_segment"]
    failure, cap = mech["source_failure"], mech["cap_truncation"]
    dates, declaration, selection = mech["date_filter"], mech["source_declaration"], mech["selection"]
    lines: list[tuple[str, str, str]] = []

    if failure["failed_cell_count"] == 0 and failure["recovered_cell_count"] == 0:
        lines.append(("源失败补偿", "失败 0 格／重试取回 0 格", "无缺口。"))
    else:
        detail = bounded([f"{row['pool']}／{row['provider']}（{show(row['error'])}，{show(row['attempts'])} 次，"
                          f"{'已恢复' if row['recovered'] else '未恢复'}，净影响 {row['net_effect']}）"
                          for row in failure["cells"]], 5, DIAGNOSTICS_NAME)
        lines.append(("源失败补偿",
                      f"失败 {failure['failed_cell_count']} 格／重试取回 {failure['recovered_cell_count']} 格",
                      f"逐格：{detail}。补法：同源重跑该格（只对瞬时错误类，不整池重跑）；仍败的缺口由本条如实记。"))

    if cap["truncated_cell_count"] == 0 and cap["cap_hit_unknown_count"] == 0:
        lines.append(("命中上限披露", "命中截断 0 格／cap_hit 判定不可得 0 格", "无缺口。"))
    else:
        fix = ("精确集源已抬 limit 或按年切片跑全、召回形源总数只记不作漏检量；补法：对判定不可得格重取或按"
               "缓存收据复算后再判。") if cap["truncated_cell_count"] else \
              "cap_hit 判定不可得者**未取数**，不得读作未截断；补法：重取或按收据复算该格后再判。"
        lines.append(("命中上限披露",
                      f"命中截断 {cap['truncated_cell_count']} 格（精确集源 {cap['by_source_type']['exact_set']}／"
                      f"召回形源 {cap['by_source_type']['recall_form']}）｜cap_hit 判定不可得 "
                      f"{cap['cap_hit_unknown_count']} 格", fix))

    unknown = dates["date_unknown_candidate_count"]
    window = dates["out_of_window_candidate_count"]
    lines.append(("无日期候选", f"{unknown} 条（Σ 逐格 date_unknown_count）" if unknown is not None else "未取数",
                  "无缺口。" if unknown == 0 else
                  ("未取数不得写成 0；补法：跑 `scripts/step1_diagnostics.py` 投影后重产账。" if unknown is None
                   else "带窗池不丢弃、以 `dateUnknown` 标记入池交筛选裁定，带窗池的窗口断言在这 "
                        f"{unknown} 条上不成立；补法：无——属覆盖不确定的如实披露。")))
    lines.append(("越窗候选", f"{window} 条（Σ 逐格 date_rejected_count）" if window is not None else "未取数",
                  "无缺口。" if window == 0 else
                  ("未取数不得写成 0；补法：跑 `scripts/step1_diagnostics.py` 投影后重产账。" if window is None
                   else "越窗是正确过滤（合法日期落窗外），区别于无日期候选；补法：无——按窗剔除并如实计数。")))

    if expansion["ran"]:
        filtered_text = (f"被滤 {show(expansion['filtered'])} 条"
                         if isinstance(expansion["filtered"], int) else show(expansion["filtered"]))
        reading = (f"种子 {show(expansion['seed_head'])}（头部）／{show(expansion['seed_judged'])}（判定集）"
                   f"｜方向 {'＋'.join(expansion['directions']) or '∅'}｜通道请求 "
                   f"{sum(c['requests'] for c in expansion['channels'])}／命中 "
                   f"{sum(c['hits'] for c in expansion['channels'])}｜新增 去重前 "
                   f"{show(expansion['new_before_dedupe'])}／去重后 {show(expansion['new_after_dedupe'])}"
                   f"｜摄入过滤 {filtered_text}"
                   f"｜判停 {show(expansion['stop_reason'])}｜{contribution_text(expansion)}")
        filter_note = ("" if not isinstance(expansion["filtered"], int) or not expansion["filtered"] else
                       f"被滤 {expansion['filtered']} 条按摄入过滤口径另计（既非源侧缺席、也非上限截断；"
                       f"计数口径＝取回行，重号各计一次，唯一 DOI／扩展独占子集数见 {EXPANSION_NAME}），"
                       f"逐条见 {EXPANSION_NAME} 的 `filtered[]`；")
        fix = (filter_note + CONTRIBUTION_NOTE + "；".join(expansion["gap_reasons"])
               + "。补法：截断／失败通道按读数件逐格原因复核，零新增如实记 0；"
               "差距交由迭代补漏与高被引缺席核对承接。") if expansion["gap_reasons"] \
            else (filter_note + CONTRIBUTION_NOTE + "无缺口。")
        lines.append(("引文扩展轮", reading, fix))
    else:
        lines.append(("引文扩展轮", "未跑", f"{block_note(expansion)}——未取数不得写成零新增。"))

    if gap_fill["ran"]:
        reading = (f"实体清单 {show(gap_fill['entity_checklist_count'])}／疑似缺口 {show(gap_fill['gap_entity_count'])}"
                   f"｜波 {show(gap_fill['waves'])}／query {show(gap_fill['query_count'])}｜新增 去重前 "
                   f"{show(gap_fill['new_before_dedupe'])}／去重后 {show(gap_fill['new_after_dedupe'])}"
                   f"｜{show(gap_fill['closure'])}｜未闭合 {len(gap_fill['unclosed_entities'])}"
                   f"｜{contribution_text(gap_fill)}")
        if gap_fill["unclosed_entities"]:
            detail = bounded([f"{row['name']}（{row['kind']}）按「{'／'.join(row['queries']) or '∅'}」全窗定向检索"
                              f"（波 {'／'.join(str(w) for w in row['waves']) or '∅'}）后，{row['reason']}"
                              for row in gap_fill["unclosed_entities"]], 5, GAP_FILL_NAME)
            fix = (CONTRIBUTION_NOTE + f"未闭合逐条：{detail}；相关结论待补检索或待新发表。"
                   "补法：下轮盘点重判或用户指认新证据面。")
        else:
            fix = CONTRIBUTION_NOTE + "无缺口（实体全闭合）。"
        lines.append(("迭代补漏", reading, fix))
    else:
        lines.append(("迭代补漏", "未跑", f"{block_note(gap_fill)}——未取数不得写成已闭合。"))

    if absent["ran"]:
        unjudged = None
        if all(absent[key] is not None for key in ("absent_total", "absent_relevant", "absent_off_topic",
                                                   "absent_out_of_window")):
            unjudged = absent["absent_total"] - (absent["absent_relevant"] + absent["absent_off_topic"]
                                                 + absent["absent_out_of_window"])
        lines.append(("高被引缺席核对",
                      f"查询 {len(absent['queries'])} 组｜缺席 {show(absent['absent_total'])}（相关 "
                      f"{show(absent['absent_relevant'])}／超题 {show(absent['absent_off_topic'])}／窗外 "
                      f"{show(absent['absent_out_of_window'])}／未判 {show(unjudged)}）｜处置 "
                      f"{show(absent['user_disposition'])}",
                      "通道只核对不入池，补入与否由用户拍板（补入走盘点表→追加轮）；未判项待 agent 逐条判读。"))
    else:
        lines.append(("高被引缺席核对", "未跑", f"{block_note(absent)}——未取数不得写成零缺席。"))

    if classic["checked"] is None:
        lines.append(("经典段", "未取数", f"{block_note(classic)}——核出与未达都不得写成 0。"))
    elif classic["checked"] == 0:
        lines.append(("经典段", "清单 0 条", "本次未给经典清单（经典段轮空记 —）；补法：需要经典覆盖时补清单后重跑核对通道。"))
    elif classic["unrecovered"]:
        detail = bounded([f"{row['id']}｜{row['reason']}" for row in classic["unrecovered"]], 5, HIGH_CITED_NAME)
        lines.append(("经典段", f"清单 {classic['checked']} 条｜核出 {show(classic['found'])}／未达 "
                                f"{len(classic['unrecovered'])}",
                      f"未达逐条：{detail}。补法：改用 DOI 或更具体词面重查，或按被引降序通道加深后复核；"
                      f"机制不承诺全量召回。"))
    else:
        lines.append(("经典段", f"清单 {classic['checked']} 条｜核出 {show(classic['found'])}／未达 0", "无缺口。"))

    gs = declaration["gs_enabled"]
    gs_text = "未启用（gs_enabled=false）" if gs is False else ("已启用" if gs is True else "未声明（∅）")
    not_included = "；".join(f"{row['source']}（{row['reason']}）" for row in declaration["not_included"])
    if not not_included:
        not_included = "未声明（∅）" if gs is None else "—"
    lines.append(("未启用源",
                  f"在组 {'／'.join(declaration['sources']) or '∅'}｜未纳入 {not_included}｜Google Scholar "
                  f"{gs_text}｜无稳定身份剔除 {show(declaration['identity_rejected_count'])} 条（不入持久化）",
                  f"{block_note(declaration)}；补法：需覆盖该面时由用户增源后重跑发现层。" if declaration.get("note")
                  else "零覆盖面（GS／注册库／预印本面／中文商业库）以声明代补源，未启用即在此披露；"
                       "补法：需覆盖该面时由用户增源后重跑发现层。"))

    if not isinstance(selection["unselected_total"], int):
        lines.append(("未选取", "未取数", f"{block_note(selection)}——未取数不得写成 0。"))
    else:
        lines.append(("未选取",
                      f"未选取 {unselected_text(selection)}",
                      f"按 S 位次分档 {breakdown_text(selection['unselected_by_rank_band'])}、按来源池 "
                      f"{breakdown_text(selection['unselected_by_origin'])}；未逐条验证纳入与全文；"
                      f"补法：需要时另开一轮或以人工单篇归置。"))
    return lines


def format_lines(lines: list[tuple[str, str, str]]) -> list[str]:
    return [f"- {name}｜{reading}｜{fix}" for name, reading, fix in lines]


def print_summary(ledger: dict) -> None:
    for line in mechanism_lines(ledger):
        print(line)
    for note in ledger["notes"]:
        print(f"覆盖账·注：{note}")
    print(f"覆盖账：九块机制读数与指针见 {ledger['report_path']}（忽略位）；§8 覆盖缺口块照下方三段式照抄：")
    for line in format_lines(gap_lines(ledger)):
        print(line)


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="覆盖账产出器：发现层九块机制读数与指针（ADR-0023）")
    parser.add_argument("--delivery", required=True, help="交付目录（workspace/YYYY-MM-DD-<slug>）")
    parser.add_argument("--report", help=f"账落点（默认 <交付>/{REPORT_NAME}）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    delivery = Path(args.delivery)
    report = Path(args.report) if args.report else delivery / REPORT_NAME
    try:
        ledger = build_ledger(delivery)
    except (OSError, ValueError) as exc:
        print(f"覆盖账错误：{exc}", file=sys.stderr)
        return 2
    ledger["report_path"] = delivery_relative(delivery, report)
    write_atomic(report, dumps(ledger))
    print_summary(ledger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
