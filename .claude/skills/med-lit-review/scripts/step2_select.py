# -*- coding: utf-8 -*-
"""Step2 选取步：研读选取集（＝下载队列＝入库集）的预演／定稿与四桶报告、下载台账骨架（ADR-0032）。

    uv run python .claude/skills/med-lit-review/scripts/step2_select.py plan  --delivery <dir> --n 30 [--n2 60] [--must-take <路径>]
    uv run python .claude/skills/med-lit-review/scripts/step2_select.py apply --delivery <dir> --n 60 [--must-take <路径>]
    uv run python .claude/skills/med-lit-review/scripts/step2_select.py pool-ledger --delivery <dir>

口径（ADR-0032 决定 1／3／5，本件是它的唯一执行处）：`agent/download-queue.txt`（逐行 DOI）＝研读选取集
＝入库集＝研读对象；`run/b1/selection.json` 是它的机器账。选取集＝必取集 ∪ 按 S 序（order 升序）前 k 条
非必取行，`k = max(0, N − m)`；`m ≥ N` 时 k=0（必取全收，N=m）。行集只取有 DOI 键的行（跳过 `T:` 键与
重复键，与队列构建同口径）。必取层判定件＝`run/b1/must-take.json`（agent 出件、人工可增删，格式见
下），不计入 N 上限；无摘要条目按题名可判者照选，进选取集后走 R5a 三级取数。

读（交付内，全部只读）：`run/b1/sort-report.json`（`rows[]`：`key`／`order`／`score`／`oa_status`／
`title`／`cited_by_count`＝S 序行集与位次真源）、`run/b1/dedupe-report.json`（`final_count`、
`final_records[].origin`＝原始终池与来源池）、`run/b1/ingest-abstracts.json`（`coverage`／`missing`＝
四桶报告读数行）、`run/b1/openalex-enrich.json`（快照无覆盖／传输失败读数）、`run/b1/cited-counts.json`
（快照查询日期：enrich 件不载日期，两件同为 Step2 产物、口径同源）、`run/b1/high-cited-check.json`
（可缺；经典段清单项判 `classic_segment_cut`）、`agent/manifest.json`（口径版本）、
`agent/screening-log.md`（获取率读数：优先「实际」（Step3 采信后回填），无则「预期」；缺记 ∅ 不编造）。

写（交付内）：
  plan          零写：只按给定 N 打读数行（选取集数／必取 m／补足 k／补足段最深位次／必取段位次区间／
                预计可得全文 ≈ N×获取率／必取清单逐条题名与依据）。
  apply         定稿四件——`agent/download-queue.txt`（首行口径句＋逐行 DOI，按 S 序 order 升序）、
                `run/b1/selection.json`（schema 冻结：执行契约 §4／ADR-0032 决定 6）、
                `run/b1/bucketing-report.md`（四桶报告，行集＝选取集；守恒行原文供门禁 B-09 读）、
                **首写**（已在位不覆盖）`agent/bucketing-and-sources.md`（下载台账骨架，§7 十列）。
  pool-ledger   P1 路线（ADR-0032 决定 10）：同两件报告但行集＝终池全量有 DOI 键行，不产队列件、
                不产 `selection.json`；P1 的进版四桶报告即 `agent/bucketing-and-sources.md`。

`must-take.json`：`{"note": "可选注记", "entries": [{"doi": "10.xxxx/yyy", "basis": "现行指南（KDIGO 2024 CKD）"}]}`
——DOI 一律过 `norm_doi`、`basis` 必填非空、同 DOI 重复即报错；**必取层须在终池内圈定**（ADR-0032 决定 3
Q1 裁定）：池外 DOI 直接拒产出（退出码 2、错误逐条列出），不静默收下也不静默丢弃——池外文献走
B1／B2 手工补充路径或重跑检索；文件缺席＝空必取集（stderr 提示一行，账内记 `must_take_input: "absent"`）。
桶判据／四桶文案／台账骨架全部从 `step2_dedupe_sort_bucket` 导入，不落第二份。

退出码：0 正常；2 用法、输入缺失或形态不符。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# 同族脚本跨件复用：桶判据／四桶文案／台账骨架与落点名出自 step2 件，DOI 归一与容错读 JSON 出自 step3 件
from step2_dedupe_sort_bucket import (  # noqa: E402
    BUCKET_ORDER, LEDGER_NAME, NO_COVERAGE_REASON, QUEUE_NAME, REPORT_NAME,
    build_report, bucket_of, ledger_skeleton, write_if_absent, write_if_changed,
)
from step3_queue import norm_doi, read_json  # noqa: E402

B1_DIR = "run/b1"
SORT_NAME = f"{B1_DIR}/sort-report.json"
DEDUPE_REPORT_NAME = f"{B1_DIR}/dedupe-report.json"
INGEST_ABSTRACTS_NAME = f"{B1_DIR}/ingest-abstracts.json"
ENRICH_NAME = f"{B1_DIR}/openalex-enrich.json"
CITED_NAME = f"{B1_DIR}/cited-counts.json"
HIGH_CITED_NAME = f"{B1_DIR}/high-cited-check.json"
MUST_TAKE_NAME = f"{B1_DIR}/must-take.json"
SELECTION_NAME = f"{B1_DIR}/selection.json"
SCREENING_LOG_NAME = "agent/screening-log.md"
MANIFEST_NAME = "agent/manifest.json"

QUEUE_HEADER = "# 研读选取集（＝下载队列＝入库集）：一行一条 DOI；剪队列不改写本件（Step3 读入）"
LAYER_MUST = "must_take"
LAYER_FILL = "rank_fill"
# 位次分档固定五档＋无位次（ADR-0032 决定 7 的披露维；键名进 selection.json，冻结）
RANK_BANDS = ("1-100", "101-300", "301-600", "601-1000", "1001+", "无位次")
BAND_EDGES = (("1-100", 1, 100), ("101-300", 101, 300), ("301-600", 301, 600),
              ("601-1000", 601, 1000), ("1001+", 1001, None))
RATE_LABELS = ("实际", "预期")  # 获取率读法：实际（上轮回填）优先，无则预期
DEFAULT_ORIGIN = "discovery"


# ---------------------------------------------------------------- 载入件


def load_stage(args, with_must_take: bool = True) -> dict:
    """三个动词共用的装载：交付路径、排序行集、原始终池、覆盖率读数与必取判定件。

    `with_must_take=False`（`pool-ledger` 用）：P1 路线无选取语义，不读必取判定件、也不打缺席提示
    ——否则会误导执行方去找 `must-take.json`。
    """
    delivery = Path(args.delivery)
    if not delivery.is_dir():
        raise ValueError(f"交付目录不存在：{delivery}")
    sort_path = delivery / SORT_NAME
    if not sort_path.is_file():
        raise ValueError(f"排序报告不存在（先跑 step2_dedupe_sort_bucket.py）：{sort_path}")
    dedupe_path = delivery / DEDUPE_REPORT_NAME
    if not dedupe_path.is_file():
        raise ValueError(f"去重报告不存在（选取口径的来源池与终池读数在此）：{dedupe_path}")
    abstracts_path = delivery / INGEST_ABSTRACTS_NAME
    if not abstracts_path.is_file():
        raise ValueError(f"入库摘要表不存在（四桶报告的覆盖率读数在此）：{abstracts_path}")

    sort_report = read_json(sort_path)
    if not isinstance(sort_report, dict) or not isinstance(sort_report.get("rows"), list):
        raise ValueError(f"排序报告形态不符（需 {{\"rows\": […]}}）：{sort_path}")
    dedupe = read_json(dedupe_path)
    if not isinstance(dedupe, dict) or not isinstance(dedupe.get("final_records"), list) \
            or not isinstance(dedupe.get("final_count"), int):
        raise ValueError(f"去重报告形态不符（需 {{\"final_records\": […], \"final_count\": N}}）：{dedupe_path}")
    abstracts = read_json(abstracts_path)
    if not isinstance(abstracts, dict) or not isinstance(abstracts.get("coverage"), dict) \
            or not isinstance(abstracts.get("missing"), list):
        raise ValueError(f"入库摘要表形态不符（需 {{\"coverage\": {{…}}, \"missing\": […]}}）：{abstracts_path}")

    pool = pool_rows_of(sort_report)
    must_take_path = Path(args.must_take) if getattr(args, "must_take", None) else delivery / MUST_TAKE_NAME
    return {
        "delivery": delivery,
        "pool": pool,
        "pool_total": len(pool),
        "origins": origin_map_of(dedupe),
        "weight_version": sort_report.get("weight_version") or "∅",
        "final_count": dedupe.get("final_count"),
        "identity_rejected": len(dedupe.get("identity_rejected") or []),
        "coverage": abstracts["coverage"],
        "missing_count": len(abstracts["missing"]),
        "must_take": (load_must_take(must_take_path) if with_must_take
                      else {"state": "not_read", "entries": [], "note": None}),
        "must_take_input": ((MUST_TAKE_NAME if must_take_path.is_file() else "absent")
                            if with_must_take else None),
    }


def pool_rows_of(sort_report: dict) -> list[dict]:
    """终池有 DOI 键行（一 DOI 一行）：跳过 `T:` 键（无 DOI 的标题形键）与重复键，与队列构建同口径。"""
    pool: list[dict] = []
    seen: set[str] = set()
    for row in sort_report["rows"]:
        key = str((row or {}).get("key") or "").strip()
        if not key or key.startswith("T:") or key in seen:
            continue
        seen.add(key)
        pool.append(row)
    return pool


def origin_map_of(dedupe: dict) -> dict[str, str]:
    """DOI → 原始来源池 `origin`（`final_records[].origin`，缺省 `discovery`）；同 DOI 多记录取首现（＝保留者）。"""
    origins: dict[str, str] = {}
    for record in dedupe["final_records"]:
        doi = norm_doi((record or {}).get("doi"))
        if doi:
            origins.setdefault(doi, (record.get("origin") or DEFAULT_ORIGIN))
    return origins


def load_must_take(path: Path) -> dict:
    """读必取判定件（agent 产，可缺）：`{"note": …, "entries": [{"doi": …, "basis": …}]}`。

    DOI 一律过 `norm_doi`；`basis` 必填非空（不占人工位、不代填）；同 DOI 重复即报错（退出码 2）。
    文件缺席＝空必取集（stderr 提示一行；账内记 `must_take_input: "absent"`）。
    """
    if not path.is_file():
        print(f"提示：必取判定件缺席（{path}），按空必取集记（m=0）", file=sys.stderr)
        return {"state": "absent", "entries": [], "note": None}
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError(f"必取判定件形态不符（需 {{\"entries\": […]}}）：{path}")
    entries: list[dict] = []
    seen: set[str] = set()
    for item in data["entries"]:
        if not isinstance(item, dict):
            raise ValueError(f"必取条目形态不符（需 {{\"doi\", \"basis\"}}）：{json.dumps(item, ensure_ascii=False)}")
        doi, basis = norm_doi(item.get("doi")), str(item.get("basis") or "").strip()
        if not doi:
            raise ValueError(f"必取条目缺 DOI：{json.dumps(item, ensure_ascii=False)}")
        if not basis:
            raise ValueError(f"必取条目缺依据（basis 必填非空）：{json.dumps(item, ensure_ascii=False)}")
        if doi in seen:
            raise ValueError(f"必取判定件内同 DOI 重复：{doi}")
        seen.add(doi)
        entries.append({"doi": doi, "basis": basis})
    return {"state": "present", "entries": entries,
            "note": (data.get("note") or "").strip() or None}


# ---------------------------------------------------------------- 选取


def select_of(pool: list[dict], must_take: dict, n: int) -> dict:
    """选取集＝必取集 ∪ 按 S 序（order 升序）前 k 条非必取行，`k = max(0, N − m)`。

    必取不计入 N 上限：`m ≥ N` 时 k=0，选取集＝必取集（N 的实义＝m）。**必取层在终池内圈定**
    （ADR-0032 决定 3）：池外 DOI 不静默收下也不静默丢弃，直接拒产出（退出码 2）——池外文献走
    B1／B2 手工补充路径或重跑检索。entries 逐条带 `row`（终池行），供四桶报告与台账骨架按同一批
    读数落行。
    """
    if n < 1:
        raise ValueError(f"N 须为正整数（研读篇数目标）：{n}")
    must = list(must_take["entries"])
    must_dois = {entry["doi"] for entry in must}
    row_of = {row["key"]: row for row in pool}
    outside = sorted(doi for doi in must_dois if doi not in row_of)
    if outside:
        raise ValueError("必取层须在终池内圈定（池外 DOI 不入选取集）：" + "、".join(outside)
                         + "；池外文献走 B1／B2 手工补充路径或重跑检索")
    k = max(0, n - len(must))
    fill = [row for row in pool if row["key"] not in must_dois][:k]

    entries: list[dict] = []
    for item in must:
        row = row_of[item["doi"]]
        entries.append({"doi": item["doi"], "layer": LAYER_MUST, "order": row.get("order"),
                        "basis": item["basis"], "row": row})
    for row in fill:
        entries.append({"doi": row["key"], "layer": LAYER_FILL, "order": row.get("order"),
                        "basis": f"按 S 序补足（第 {row.get('order')} 位）", "row": row})
    # 行序＝S 序 order 升序；缺位次（异版本／手工改过的排序报告）置末，彼此按 DOI 稳定排序
    entries.sort(key=lambda e: (0, e["order"], e["doi"]) if isinstance(e["order"], int) else (1, 0, e["doi"]))

    selected = {entry["doi"] for entry in entries}
    orders = [entry["order"] for entry in entries if isinstance(entry["order"], int)]
    fills = [entry["order"] for entry in entries
             if entry["layer"] == LAYER_FILL and isinstance(entry["order"], int)]
    return {
        "n": n, "must_take_count": len(must), "rank_fill_count": len(fill),
        "fill_shortfall": max(0, k - len(fill)),
        "entries": entries, "selected_total": len(entries),
        "fill_boundary_order": max(fills) if fills else None,
        "selected_order_min": min(orders) if orders else None,
        "selected_order_max": max(orders) if orders else None,
        "pool_total": len(pool),
        "unselected": [row for row in pool if row["key"] not in selected],
    }


def rank_band_of(order) -> str:
    if not isinstance(order, int):
        return "无位次"
    for label, low, high in BAND_EDGES:
        if order >= low and (high is None or order <= high):
            return label
    return "无位次"


def classic_cut_of(delivery: Path, selected: set[str] | None) -> tuple[list[str] | None, int, str | None]:
    """经典段被截（ADR-0032 决定 7 的选取口径）：**集合成员判据**——清单项 `in_terminal == true`
    且其 DOI ∉ 选取集（不得用「order > 截线」——选取集已不是前缀）。

    同一性判定仍归 `high_cited_check.py`（本件只读它的逐项读数）；`in_terminal` 为 `null`（词面多义）
    或项无 DOI 时归属不可判，计不可得项数、不进被截清单。缺件记 ∅（不写成未被截）。
    `selected` 传 `None`＝只读清单与不可得项（`plan` 预演不做成员判定）。
    """
    path = delivery / HIGH_CITED_NAME
    if not path.is_file():
        return None, 0, f"classic_segment_cut 记 ∅（缺 {HIGH_CITED_NAME}，未取数不得写成未被截）"
    data = read_json(path)
    block = data.get("checklist") if isinstance(data, dict) else None
    items = block.get("items") if isinstance(block, dict) else None
    if not isinstance(items, list):
        return None, 0, f"classic_segment_cut 记 ∅（{HIGH_CITED_NAME} 缺 checklist.items）"
    cut: list[str] = []
    unknown = 0
    for item in items:
        if not isinstance(item, dict):
            unknown += 1
            continue
        in_terminal, key = item.get("in_terminal"), norm_doi(item.get("doi"))
        if in_terminal is None or (in_terminal is True and not key):
            unknown += 1
        elif in_terminal is True and selected is not None and key not in selected and key not in cut:
            cut.append(key)
    note = (None if not unknown else
            f"经典段 {unknown} 项判定不可得（`in_terminal` 为 null 或项无 DOI），不计入被截清单")
    return cut, unknown, note


# ---------------------------------------------------------------- 读数件


def snapshot_view_of(delivery: Path) -> dict:
    """重建快照读数视图（供四桶报告／台账骨架的读数行）：查询日期取 `cited-counts.json` 的
    `snapshot.query_dates`（`openalex-enrich.json` 不载日期，两件同为 Step2 产物、口径同源）；
    逐条读数按 `openalex-enrich.json` 的 `records`／`no_coverage`／`transport_errors` 三类复原，
    缺件照实记 ∅（不冒认有覆盖）。
    """
    enrich, cited = read_json(delivery / ENRICH_NAME), read_json(delivery / CITED_NAME)
    enrich = enrich if isinstance(enrich, dict) else {}
    block = cited.get("snapshot") if isinstance(cited, dict) else None
    dates = [d for d in ((block or {}).get("query_dates") or []) if d]
    records = enrich.get("records") if isinstance(enrich.get("records"), dict) else {}
    no_coverage = [norm_doi(doi) for doi in (enrich.get("no_coverage") or []) if norm_doi(doi)]
    transport = enrich.get("transport_errors") if isinstance(enrich.get("transport_errors"), list) else []
    date = dates[0] if dates else None
    readings: dict[str, dict] = {}
    for doi, record in records.items():
        readings[norm_doi(doi)] = {"record": record, "status": "covered", "reason": None, "date": date}
    for doi in no_coverage:
        readings.setdefault(doi, {"record": None, "status": "no_coverage",
                                  "reason": NO_COVERAGE_REASON, "date": date})
    for item in transport:
        for doi in (item or {}).get("dois") or []:
            readings[norm_doi(doi)] = {"record": None, "status": "transport_error", "reason": None, "date": None}
    return {"readings": readings, "dates": dates, "stats": enrich.get("cache") or {},
            "no_coverage": [{"doi": doi, "reason": NO_COVERAGE_REASON} for doi in no_coverage],
            "transport_errors": transport}


def _rate_after(line: str, label: str) -> float | None:
    """标签后到下一个分隔符之间的获取率读数：有 `=0.65` 取等号右侧（`=65%` 折半），
    否则取首个百分比，再退到首个 `0.x` 小数；读不出即 ∅（不猜）。"""
    for match in re.finditer(label, line):
        clause = re.split(r"[｜|／/）()\n]", line[match.end():], maxsplit=1)[0]
        eq = re.search(r"[=＝]\s*(\d+(?:\.\d+)?)\s*(%?)", clause)
        if eq:
            value = float(eq.group(1)) / (100 if eq.group(2) == "%" else 1)
            return round(value, 4) if 0 < value <= 1 else None
        pct = re.search(r"(\d+(?:\.\d+)?)\s*%", clause)
        if pct:
            return round(float(pct.group(1)) / 100, 4)
        dec = re.search(r"(?<![\d.])(0\.\d+)", clause)
        if dec:
            return round(float(dec.group(1)), 4)
    return None


def acquisition_rate_of(delivery: Path) -> tuple[float | None, str | None]:
    """筛选日志的获取率读数（上轮回填值）：优先「实际」（Step3 采信后回填），无则「预期」；
    多行时取最末行（最靠后一轮的记账）；读不出记 ∅，不编造。
    """
    path = delivery / SCREENING_LOG_NAME
    if not path.is_file():
        return None, None
    found: tuple[float, str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if "获取率" not in line:
            continue
        for label in RATE_LABELS:
            value = _rate_after(line, label)
            if value is not None:
                found = (value, label)
                break
    return found if found else (None, None)


def caliber_of(delivery: Path) -> str:
    """交付口径版本：读 `agent/manifest.json` 的 `caliber.grey_gate_version`，缺省现行口径（与 step3_queue 同口径）。"""
    data = read_json(delivery / MANIFEST_NAME)
    if isinstance(data, dict):
        value = str((data.get("caliber") or {}).get("grey_gate_version") or "").strip()
        if value:
            return value
    return "post-0006-fastest"


# ---------------------------------------------------------------- 产出


def buckets_of(rows: list[dict]) -> dict[str, list[dict]]:
    buckets = {name: [] for name in BUCKET_ORDER}
    for row in rows:
        buckets[bucket_of(row.get("oa_status"))].append(row)
    return buckets


def queue_text(entries: list[dict]) -> str:
    return QUEUE_HEADER + "\n" + "".join(f"{entry['doi']}\n" for entry in entries)


def selection_doc(sel: dict, stage: dict, classic: list[str] | None, classic_note: str | None) -> dict:
    """`run/b1/selection.json`（schema 冻结，ADR-0032 决定 6）：逐条 DOI ｜层｜依据｜S 位次 ＋ 未选取披露。

    ∅ 口径：未取数记 `null`，实测零记 `0`／`[]`，不得互顶；`fill_boundary_order` 在 k=0 时记 `null`；
    `unselected_by_rank_band` 五档＋`无位次` 恒在场；`unselected_by_origin` 键取原始 `origin` 值（缺省
    `discovery`），计数为实测零的来源池照记 0。
    """
    notes = ["选取集＝必取集 ∪ 按 S 序（order 升序）前 k 条非必取行，k=max(0, N − m)；m≥N 时 k=0"
             "（必取全收，N=m）；行集只取有 DOI 键的行（跳过 `T:` 键与重复键，与队列构建同口径）",
             "未选取＝终池 − 选取集；∅（未取数）与 0／[]（实测零）不互顶"]
    band = {label: 0 for label in RANK_BANDS}
    # 键序固定（来源池排序）：产物逐字节可复算，重跑不因集合迭代序而改字
    by_origin = {origin: 0 for origin in sorted({stage["origins"].get(row["key"]) or DEFAULT_ORIGIN
                                                 for row in stage["pool"]})}
    for row in sel["unselected"]:
        band[rank_band_of(row.get("order"))] += 1
        origin = stage["origins"].get(row["key"]) or DEFAULT_ORIGIN
        by_origin[origin] = by_origin.get(origin, 0) + 1
    if stage["must_take"]["state"] == "absent":
        notes.append("必取判定件缺席，按空必取集记（`must_take_input: \"absent\"`）")
    if classic_note:
        notes.append(classic_note)
    elif classic:
        notes.append(f"经典段 {len(classic)} 条在池但未选取（`classic_segment_cut`）")
    if sel["fill_shortfall"]:
        notes.append(f"非必取行不足：补足位 k={sel['n'] - sel['must_take_count']} 条，可补行仅 "
                     f"{sel['rank_fill_count']} 条，选取集＝{sel['selected_total']} 条（如实记，不缩必取）")
    # 守恒式自检（必取层已在池内圈定 ⇒ 未选取＝终池 − 选取集，两表之和恒等于 unselected_total）：
    # 不等即拒出账——不把自相矛盾的账落盘
    unselected_total = sel["pool_total"] - sel["selected_total"]
    if sum(band.values()) != unselected_total or sum(by_origin.values()) != unselected_total:
        raise ValueError(f"未选取分档／来源池之和与 pool_total − selected_total（{unselected_total}）不符："
                         f"分档 {sum(band.values())}／来源池 {sum(by_origin.values())}（守恒式自检不过，不出账）")
    return {
        "note": "；".join(notes) + "。",
        "target_n": sel["n"],
        "selected_total": sel["selected_total"],
        "must_take_count": sel["must_take_count"],
        "rank_fill_count": sel["rank_fill_count"],
        "must_take_input": stage["must_take_input"],
        "fill_boundary_order": sel["fill_boundary_order"],
        "selected_order_min": sel["selected_order_min"],
        "selected_order_max": sel["selected_order_max"],
        "pool_total": sel["pool_total"],
        "unselected_total": unselected_total,
        "unselected_by_rank_band": band,
        "unselected_by_origin": by_origin,
        "classic_segment_cut": classic,
        "entries": [{"doi": entry["doi"], "layer": entry["layer"], "order": entry["order"],
                     "basis": entry["basis"]} for entry in sel["entries"]],
    }


def dump_selection(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


# ---------------------------------------------------------------- 动词


def cmd_plan(args) -> int:
    """预演：按给定 N 打读数行；零写（口径句＝ADR-0032 决定 3／5）。"""
    stage = load_stage(args)
    rate, rate_label = acquisition_rate_of(stage["delivery"])
    rate_text = (f"{rate}（取筛选日志「{rate_label}」值）" if rate is not None else "∅（筛选日志无获取率读数）")
    _, _, classic_note = classic_cut_of(stage["delivery"], None)
    print("研读选取集预演（零写；选取集＝必取集 ∪ 按 S 序前 k 条非必取行，k=max(0, N − m)）")
    print(f"终池 {stage['pool_total']} 条（有 DOI 键）｜排序口径 `{stage['weight_version']}`｜"
          f"必取输入 {stage['must_take_input']}（m 逐 N 给出）｜获取率 {rate_text}")
    if classic_note:
        print(f"经典段：{classic_note}")
    for n in ns_of(args):
        sel = select_of(stage["pool"], stage["must_take"], n)
        must_orders = [entry["order"] for entry in sel["entries"]
                       if entry["layer"] == LAYER_MUST and isinstance(entry["order"], int)]
        must_span = f"{min(must_orders)}–{max(must_orders)}" if must_orders else "∅"
        selected_span = f"{sel['selected_order_min']}–{sel['selected_order_max']}"
        if sel["selected_order_min"] is None:
            selected_span = "∅"
        boundary = sel["fill_boundary_order"] if sel["fill_boundary_order"] is not None else "∅"
        estimate = f"≈ {n}×{rate}＝{round(n * rate, 1)} 条" if rate is not None else f"≈ {n}×∅"
        print(f"N={n}｜选取集 {sel['selected_total']} 条（必取 {sel['must_take_count']}／补足 "
              f"{sel['rank_fill_count']}）｜补足段止于 S 序第 {boundary} 位｜必取段位次区间 {must_span}｜"
              f"选取集位次区间 {selected_span}｜预计可得全文 {estimate}")
    print("必取清单（逐条题名与依据）：")
    must_entries = stage["must_take"]["entries"]
    if not must_entries:
        print("- （空必取集：判定件缺席或无条目）")
    row_of = {row["key"]: row for row in stage["pool"]}
    for entry in must_entries:
        row = row_of[entry["doi"]]  # 必取层已在 select_of 内核对在池（池外即拒产出）
        print(f"- {entry['doi']}｜S 序 {row['order']}｜{row.get('title') or '（题名不可得）'}｜"
              f"依据：{entry['basis']}")
    return 0


def cmd_apply(args) -> int:
    """定稿：写队列＋selection.json＋四桶报告（行集＝选取集）＋台账骨架（首写）。"""
    stage = load_stage(args)
    sel = select_of(stage["pool"], stage["must_take"], args.n)
    rows = [entry["row"] for entry in sel["entries"]]
    classic, unknown, classic_note = classic_cut_of(stage["delivery"], {entry["doi"] for entry in sel["entries"]})
    doc = selection_doc(sel, stage, classic, classic_note)
    snapshot_view = snapshot_view_of(stage["delivery"])
    written = []
    if write_if_changed(stage["delivery"] / QUEUE_NAME, queue_text(sel["entries"])):
        written.append(QUEUE_NAME)
    if write_if_changed(stage["delivery"] / SELECTION_NAME, dump_selection(doc)):
        written.append(SELECTION_NAME)
    report = build_report(rows, buckets_of(rows), sel["pool_total"], stage["final_count"],
                          stage["identity_rejected"], snapshot_view, stage["weight_version"],
                          stage["coverage"], stage["missing_count"],
                          sel["must_take_count"], sel["rank_fill_count"])
    if write_if_changed(stage["delivery"] / REPORT_NAME, report):
        written.append(REPORT_NAME)
    ledger = ledger_skeleton(rows, caliber_of(stage["delivery"]), stage["final_count"],
                             stage["identity_rejected"], snapshot_view, stage["weight_version"],
                             sel["must_take_count"], sel["rank_fill_count"])
    seeded = [LEDGER_NAME] if write_if_absent(stage["delivery"] / LEDGER_NAME, ledger) else []
    classic_text = len(classic) if classic is not None else "∅"
    cut_note = f"（判定不可得 {unknown} 项）" if unknown else ""
    print(f"研读选取集定稿（N={sel['n']}）：选取集 {doc['selected_total']} 条"
          f"（必取 {doc['must_take_count']}／补足 {doc['rank_fill_count']}）｜终池 {doc['pool_total']} 条｜"
          f"未选取 {doc['unselected_total']} 条（按位次分档 {json.dumps(doc['unselected_by_rank_band'], ensure_ascii=False)}）")
    print(f"选取集位次区间 "
          f"{doc['selected_order_min'] if doc['selected_order_min'] is not None else '∅'}–"
          f"{doc['selected_order_max'] if doc['selected_order_max'] is not None else '∅'}｜补足段止于 S 序第 "
          f"{doc['fill_boundary_order'] if doc['fill_boundary_order'] is not None else '∅'} 位｜"
          f"classic_segment_cut {classic_text} 条{cut_note}")
    print(f"改动 {len(written)} 件：{'、'.join(written) or '无'}｜版内首写 {'、'.join(seeded) or '无'}")
    return 0


def cmd_pool_ledger(args) -> int:
    """P1 路线（ADR-0032 决定 10）：行集＝终池全量有 DOI 键行，产四桶报告与台账骨架，不产队列与 selection.json。

    骨架落盘用**覆盖**（`write_if_changed`）而非 `apply` 的首写：P1 无 Step3，本件即进版四桶报告终件、
    没有回填内容要保护，池或排序重算后必须随新读数更新（首写会留住上一轮的全池行集）。
    """
    stage = load_stage(args, with_must_take=False)  # P1 无选取语义：不读必取判定件、不打缺席提示
    rows = stage["pool"]
    snapshot_view = snapshot_view_of(stage["delivery"])
    report = build_report(rows, buckets_of(rows), len(rows), stage["final_count"],
                          stage["identity_rejected"], snapshot_view, stage["weight_version"],
                          stage["coverage"], stage["missing_count"])
    written = [REPORT_NAME] if write_if_changed(stage["delivery"] / REPORT_NAME, report) else []
    ledger = ledger_skeleton(rows, caliber_of(stage["delivery"]), stage["final_count"],
                             stage["identity_rejected"], snapshot_view, stage["weight_version"])
    if write_if_changed(stage["delivery"] / LEDGER_NAME, ledger):
        written.append(LEDGER_NAME)
    print(f"P1 全池四桶报告：行集＝终池全量有 DOI 键 {len(rows)} 条（不产队列件、不产 selection.json）")
    print(f"改动 {len(written)} 件：{'、'.join(written) or '无'}")
    return 0


def ns_of(args) -> list[int]:
    ns = [args.n] + ([args.n2] if getattr(args, "n2", None) else [])
    return list(dict.fromkeys(ns))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Step2 选取步：研读选取集（＝下载队列＝入库集）的预演／定稿与四桶报告、下载台账骨架（ADR-0032）")
    ap.add_argument("command", choices=("plan", "apply", "pool-ledger"),
                    help="plan＝按 N 打读数行（零写）；apply＝定稿选取集四件；"
                         "pool-ledger＝P1 路线（全池行集的四桶报告与台账骨架，不产队列）")
    ap.add_argument("--delivery", required=True, help="交付目录（workspace/YYYY-MM-DD-<slug>）")
    ap.add_argument("--n", type=int, help="研读篇数 N（apply 必填；plan 必填，可另给 --n2 比档）")
    ap.add_argument("--n2", type=int, help="plan 的第二档 N（如推荐档 30＋扩展档 60 并示）")
    ap.add_argument("--must-take", help=f"必取判定件（默认 <交付>/{MUST_TAKE_NAME}；缺省＝空必取集）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "pool-ledger":
        if args.n is not None or args.n2 is not None:
            print("Step2 选取步错误：pool-ledger 不按 N 选取，--n／--n2 不适用", file=sys.stderr)
            return 2
    else:
        if args.n is None:
            print(f"Step2 选取步错误：{args.command} 需要 --n（研读篇数 N）", file=sys.stderr)
            return 2
        if args.command != "plan" and args.n2 is not None:
            print("Step2 选取步错误：--n2 只用于 plan（比档预演）", file=sys.stderr)
            return 2
    try:
        if args.command == "plan":
            return cmd_plan(args)
        if args.command == "apply":
            return cmd_apply(args)
        return cmd_pool_ledger(args)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Step2 选取步错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
