# -*- coding: utf-8 -*-
"""高被引缺席核对器：被引降序 top-N 与终池对账，产出缺席清单（只核对不入池）。

When: Step2 首轮排序完成后、规模确认之前（与引文扩展轮、迭代补漏同窗；ADR-0019「扩展先于规模确认」）。
     核对读数随规模确认出示，补入与否由用户拍板；通道只核对，不并池、不自动合并。
Do: uv run python .claude/skills/med-lit-review/scripts/high_cited_check.py --delivery workspace/YYYY-MM-DD-<slug> \
        [--per-page 100] [--budget 30] [--page-interval 0.0] [--queries-file F] [--judgments-file F] \
        [--classify-only] [--openalex-base URL]

读（交付内，只读）：`run/b1/dedupe-report.json`（终池 `final_records`＝对账基准）、`run/b1/sort-report.json`
（`rows[].key`＋`rows[].order`，给经典段项标 S 序位次，供选取步判未选取）、
`run/b1/high-cited-queries.json`（agent 产核对输入：1–3 组主题关键概念查询＋可选经典清单／日期窗／适应症）、
`run/b1/high-cited-judgments.json`（agent 产相关性判定与用户处置，可缺）。

写（交付内，`run/b1/` 忽略位）：`high-cited-check.json`（机制读数＋缺席清单＋经典段逐项判定）、
`raw/high-cited-<查询 slug>.json`（逐页原始响应）。**不触碰 `step1-pools.json`、不并池、不自动合并**：
补入动作由用户拍板后走迭代补漏的追加轮（把 `pool_entry_proposals` 行并入 `run/b1/gap-entities.json`
后跑 `gap_fill.py`），本脚本只给接口与记录形态。

取数与深度（票 08 §2）：OpenAlex `search=<查询原文>&sort=cited_by_count:desc`，`per-page` 起（默认 100）；
基础取数＝每查询 1 请求（top-100，1–3 查询），清单未闭合则按页加深（每页 1 请求）——深度无固定上限，
整轮受 `--budget`（默认 30，含重试）夹取。多查询按轮次交替加深（每轮各查询各取一页），先取满基础面再加深。
逐查询判停：`清单闭合`（该查询内清单项全检出）／`检索集耗尽`（页不满或无游标）／`被引带下沿`（页边界被引
< 已检出清单项的最低被引＝经典段带下沿；机制代理，命中项被引越高加深越浅）／`清单无命中`（无命中取满 2 屏
即停）／`无清单（首屏即停）`／`预算耗尽`／`取数失败`。

缺席清单（对账）：终池＝`final_records`，键＝DOI 一级、`标题|年` 二级（同 Step2 去重口径）；不在终池的
高被引条目进 `absent[]`，逐条带 `appearances[]{query,rank,depth}` 与 `cited_by_count`。分类列：`相关`／
`超题`＝**agent 判**（`high-cited-judgments.json` 的 `judgments[]{doi,verdict,reason,kind?}`）、`窗外`＝
脚本按 `window` 机械判（越窗优先于 agent 判定）、未给判定的记 `未判`——脚本不代判、不猜。处置记录
`user_disposition`（`补入`／`不补`；未给记 `未答`）取 `high-cited-judgments.json` 的 `disposition{verdict,note}`。

同一性（清单与经典段）：带 `doi` 的清单项**只认 doi 相等**；无 `doi` 的项按 `gap_fill.contains_phrase` 的
词元序列整段口径匹配题名（不因子串误中）。**唯一命中才认定**：通道侧多义（≥2 条候选）不作检出、不进带下沿，
逐条记「词面多义（不认定）」；终池位次只在 DOI 直查或词面**唯一**回查时归属，多义记 `in_terminal: null` 与
`in_terminal_source: ambiguous_title`（在池与否 ∅，不写 false 冒充未在池）。

两阶段：默认阶段发请求（取数，写读数件）；`--classify-only` 为判定阶段——不发请求，读既有读数件与判定件
重算分类列、补入提案与机制读数（规模确认拍板后回写处置用）。取数读数（请求／深度／判停／原始响应指针）
两阶段同一份，不重取。

账目（ADR-0023 冻结字段名，供覆盖账消费；块标签 `mechanism`／`classic_segment`／`checklist` 为读数件容器）：
`mechanism` ＝ `ran｜queries[]{query,depth}｜absent_total｜absent_relevant｜absent_off_topic｜
absent_out_of_window｜user_disposition`；`classic_segment` ＝ `checked｜found｜unrecovered[]{id,reason}`；
经典段逐项落 `checklist.items[]{id,doi,in_terminal,in_terminal_source,order}`——**被截判据已移交选取步**
（ADR-0032：`run/b1/selection.json` 的 `classic_segment_cut`＝清单项 `in_terminal==true` 且归一 DOI 不在
研读选取集，**集合成员判据**，不用位次截线；判定不可得项数记该件 `note`），本件不再落队列截线读数。

退出码：0 正常（零缺席与部分失败如实进账，不算失败）；2 用法、输入文件或形态错误。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.parse
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from step3_queue import is_doi_key, norm_doi, read_json  # noqa: E402 - 同族脚本跨件复用（DOI 归一／容错读 JSON 同口径）
from gap_fill import DESIGN_TOKENS, contains_phrase, phrase_tokens  # noqa: E402 - 词面认定与设计词表复用（拉丁形词元序列整段匹配、中文去标点子串、试验设计词同表；不复制第二份）
from citation_expand import (  # noqa: E402 - 取数层复用：预算／节流／重试与 OpenAlex 题录、键、slug 同口径，不复制第二份
    Budget,
    BudgetExhausted,
    HttpClient,
    Throttle,
    dumps,
    openalex_record,
    record_key,
    slug_for,
    write_atomic,
)

B1_DIR = "run/b1"
DEDUPE_NAME = f"{B1_DIR}/dedupe-report.json"
SORT_NAME = f"{B1_DIR}/sort-report.json"
QUERIES_NAME = f"{B1_DIR}/high-cited-queries.json"
JUDGMENTS_NAME = f"{B1_DIR}/high-cited-judgments.json"
REPORT_NAME = f"{B1_DIR}/high-cited-check.json"
RAW_DIR = "raw"

DEFAULT_OPENALEX_BASE = "https://api.openalex.org"
PER_PAGE = 100           # 基础深度＝top-100（1 请求/查询）
PER_PAGE_CEILING = 200   # OpenAlex `per-page` 契约上限
REQ_BUDGET = 30          # 整轮请求预算含重试（深度无固定上限，预算即夹取）
MAX_QUERIES = 3          # 票 08 §2：1–3 组主题关键概念查询
NO_MATCH_PAGES = 2       # 无清单命中时取满的屏数（无闭合信号时的停法）
SELECT_CHECK = "id,doi,display_name,publication_year,publication_date,type,cited_by_count,primary_location"

VERDICT_RELEVANT = "相关"
VERDICT_OFF_TOPIC = "超题"
VERDICT_OUT_OF_WINDOW = "窗外"
VERDICT_UNJUDGED = "未判"
AGENT_VERDICTS = (VERDICT_RELEVANT, VERDICT_OFF_TOPIC)
DISPOSITION_MERGE = "补入"
DISPOSITION_SKIP = "不补"
DISPOSITION_UNANSWERED = "未答"
DISPOSITION_VALUES = (DISPOSITION_MERGE, DISPOSITION_SKIP)

STOP_DONE = "查询集跑完"
STOP_CLOSED = "清单闭合"
STOP_EXHAUSTED = "检索集耗尽"
STOP_BAND = "被引带下沿"
STOP_NO_MATCH = "清单无命中"
STOP_NO_CHECKLIST = "无清单（首屏即停）"
STOP_BUDGET = "预算耗尽"
STOP_FAILED = "取数失败"

KINDS = ("trial", "drug", "disease", "endpoint")
STAGE_FETCH = "取数"
STAGE_CLASSIFY = "判定"
# 取数阶段按「判定件缺」记的行，判定阶段须重算——否则同一读数件会同时留着新旧两说
STALE_JUDGMENT_NOTES = ("高被引缺席核对：判定件缺（", "高被引缺席核对：判定件形态不符")


# ---------------------------------------------------------------- 词面与键


def design_token_title(title: object) -> bool:
    """题名是否含试验设计词（词元前缀判据；词表直接取自 `gap_fill.DESIGN_TOKENS`，不另立一份）。"""
    return any(token.startswith(prefix) for token in phrase_tokens(str(title or ""))
               for prefix in DESIGN_TOKENS)


def query_slug(text: str) -> str:
    """查询原文的 slug（沿种子 slug 规则：可读形式＋哈希保唯一）；纯中文等无 ASCII 时只留哈希。"""
    slug = slug_for(text)
    if slug == "seed":
        slug = "q-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return slug


# ---------------------------------------------------------------- 输入


def load_queries(path: Path) -> tuple[list[dict], list[dict], dict, list[str], list[str]]:
    """核对输入形态校验（只校验不代判）：1–3 组查询；清单项需 id＋（match 词面或 doi）；窗与适应症可缺。"""
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("queries"), list):
        raise ValueError(f"核对输入形态不符（需 {{\"queries\": […]}}）：{path}")
    notes: list[str] = []
    queries: list[dict] = []
    for index, item in enumerate(data["queries"], start=1):
        text = str((item or {}).get("query") or "").strip() if isinstance(item, dict) else ""
        if not text:
            raise ValueError(f"核对输入第 {index} 组查询 query 空：{path}")
        queries.append({"query": text, "concept": str((item or {}).get("concept") or "").strip() or None})
    if not queries:
        raise ValueError(f"核对输入 0 组查询（核对通道需 1–{MAX_QUERIES} 组主题关键概念查询）：{path}")
    if len(queries) > MAX_QUERIES:
        notes.append(f"高被引缺席核对：核对输入 {len(queries)} 组查询，超上限 {MAX_QUERIES}，只取前 "
                     f"{MAX_QUERIES} 组")
        queries = queries[:MAX_QUERIES]

    checklist: list[dict] = []
    for index, item in enumerate(data.get("checklist") or [], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"核对输入清单第 {index} 条非对象：{path}")
        item_id = str(item.get("id") or "").strip()
        phrases = [str(phrase).strip() for phrase in (item.get("match") or []) if str(phrase).strip()]
        doi = norm_doi(item.get("doi")) if isinstance(item.get("doi"), str) else ""
        kind = str(item.get("kind") or KINDS[0]).strip()
        problems = []
        if not item_id:
            problems.append("id 空")
        if not phrases and not doi:
            problems.append("match 词面与 doi 皆缺（清单项须至少一种命中途径）")
        if kind not in KINDS:
            problems.append(f"kind 应为 {'／'.join(KINDS)}")
        if problems:
            raise ValueError(f"核对输入清单第 {index} 条不合格（{'；'.join(problems)}）：{path}")
        checklist.append({"id": item_id, "kind": kind, "match": phrases, "doi": doi})
    if not checklist:
        notes.append("高被引缺席核对：未给清单（经典段轮空记 —，只出缺席清单）")

    window = data.get("window") if isinstance(data.get("window"), dict) else {}
    bounds = {edge: (window.get(edge) if isinstance(window.get(edge), int) else None) for edge in ("from", "to")}
    if bounds["from"] is None and bounds["to"] is None:
        notes.append("高被引缺席核对：未给日期窗（全窗口径，无窗外判定）")
    indication = [str(word).strip() for word in (data.get("indication") or []) if str(word).strip()]
    return queries, checklist, bounds, indication, notes


def load_judgments(path: Path) -> tuple[dict, dict, list[dict], list[str]]:
    """判定件形态校验：verdict ∈ 相关／超题、reason 非空、disposition ∈ 补入／不补；缺件＝未判＋未答。"""
    notes: list[str] = []
    unanswered = {"verdict": DISPOSITION_UNANSWERED, "note": None}
    if not path.is_file():
        notes.append(f"高被引缺席核对：判定件缺（{path.name}），分类列记未判、处置记未答")
        return {}, unanswered, [], notes
    data = read_json(path)
    if not isinstance(data, dict):
        notes.append(f"高被引缺席核对：判定件形态不符（需对象），分类列记未判、处置记未答：{path}")
        return {}, unanswered, [], notes
    judgments: dict[str, dict] = {}
    rejected: list[dict] = []
    for index, item in enumerate(data.get("judgments") or [], start=1):
        if not isinstance(item, dict):
            rejected.append({"index": index, "doi": None, "problems": ["条目非对象"]})
            continue
        doi = norm_doi(item.get("doi")) if isinstance(item.get("doi"), str) else ""
        verdict = str(item.get("verdict") or "").strip()
        reason = str(item.get("reason") or "").strip()
        kind = str(item.get("kind") or "").strip()
        problems = []
        if not doi:
            problems.append("doi 缺或不可归一")
        if verdict not in AGENT_VERDICTS:
            problems.append(f"verdict 应为 {'／'.join(AGENT_VERDICTS)}（窗外由脚本按日期窗机械判）")
        if not reason:
            problems.append("reason 空")
        if kind and kind not in KINDS:
            problems.append(f"kind 应为 {'／'.join(KINDS)}")
        if problems:
            rejected.append({"index": index, "doi": item.get("doi"), "problems": problems})
            continue
        judgments[doi] = {"verdict": verdict, "reason": reason, "kind": kind or None, "index": index}
    disposition = data.get("disposition") if isinstance(data.get("disposition"), dict) else {}
    verdict = str(disposition.get("verdict") or "").strip()
    if not verdict:
        disposition_row = unanswered
    elif verdict not in DISPOSITION_VALUES:
        notes.append(f"高被引缺席核对：处置 verdict 应为 {'／'.join(DISPOSITION_VALUES)}，记未答"
                     f"（原值：{verdict}）")
        disposition_row = unanswered
    else:
        disposition_row = {"verdict": verdict, "note": str(disposition.get("note") or "").strip() or None}
    if rejected:
        notes.append(f"高被引缺席核对：判定件 {len(rejected)} 条不合形态被拒（doi／verdict／reason／kind）")
    return judgments, disposition_row, rejected, notes


# ---------------------------------------------------------------- 终池与位次


def load_terminal(dedupe_path: Path, sort_path: Path) -> dict:
    """终池：`final_records` 键集（DOI 一级、`标题|年` 二级）＋排序 `order`。
    `order` 只收排序报告里的 DOI 键：非 DOI 键是 step2 的 `T:<归一标题24>` 形态（标题截断且无年），
    与终池二级键不同形，硬凑只会造出永不命中的键——非 DOI 行逐条计数进读数，缺位次处记 ∅ 不记 false。"""
    dedupe = read_json(dedupe_path)
    if not isinstance(dedupe, dict) or not isinstance(dedupe.get("final_records"), list):
        raise ValueError(f"终池形态不符（需 {{\"final_records\": […]}}）：{dedupe_path}")
    sort_data = read_json(sort_path)
    if not isinstance(sort_data, dict) or not isinstance(sort_data.get("rows"), list):
        raise ValueError(f"排序报告形态不符（需 {{\"rows\": […]}}）：{sort_path}")
    keys: set[str] = set()
    titles: list[dict] = []          # 词面回查用：键随行留档，命中后可直接查 order（LIPID 型：通道未达但在池）
    for record in dedupe["final_records"]:
        if not isinstance(record, dict):
            continue
        key = record_key(record)
        if key:
            keys.add(key)
        if record.get("title"):
            titles.append({"key": key, "title": str(record["title"])})
    order: dict[str, int] = {}
    non_doi_rows = 0
    for row in sort_data["rows"]:
        if not isinstance(row, dict) or not isinstance(row.get("order"), int):
            continue
        key = norm_doi(row.get("key")) if isinstance(row.get("key"), str) else ""
        if key and is_doi_key(key):
            order.setdefault(key, row["order"])
        else:
            non_doi_rows += 1
    return {"source": dedupe_path.as_posix(), "sort_report": sort_path.as_posix(), "keys": keys,
            "titles": titles, "order": order, "count": len(keys), "sort_rows": len(sort_data["rows"]),
            "order_rows": len(order), "order_rows_non_doi": non_doi_rows}


# ---------------------------------------------------------------- 取数


def works_url(base: str, query: str, per_page: int, cursor: str) -> str:
    return (f"{base}/works?search={urllib.parse.quote(query)}&sort=cited_by_count:desc"
            f"&per-page={per_page}&select={SELECT_CHECK}&cursor={urllib.parse.quote(cursor)}")


def new_state(query: dict, ctx: dict) -> dict:
    return {"query": query["query"], "concept": query["concept"], "slug": query_slug(query["query"]),
            "cursor": "*", "next_cursor": None, "entries": [], "requests": 0, "pages": 0, "depth": 0,
            "records": [], "matched_ids": set(), "unique_ids": [], "boundary": None, "available": None,
            "short_page": False, "failure": None, "stop_reason": None}


def page_entries(state: dict, client: HttpClient, ctx: dict) -> list[dict]:
    """取一页：预算耗尽／取数失败只落该查询的失败态，不冒泡。"""
    url = works_url(ctx["openalex_base"], state["query"], ctx["per_page"], state["cursor"])
    if ctx["page_interval"] > 0 and client.budget.used:   # 匿名配额节律（08 资产 §3 实测 429）：默认 0
        time.sleep(ctx["page_interval"])
    try:
        entry = client.get_json("openalex", url)
    except BudgetExhausted:
        state["stop_reason"] = STOP_BUDGET
        return []
    state["entries"].append(entry)
    state["requests"] += int(entry.get("attempts") or 0)
    if entry.get("error"):
        state["failure"] = {"class": entry["error"]["class"], "message": entry["error"]["message"],
                            "attempts": entry.get("attempts"), "retried": bool(entry.get("retried"))}
        return []
    payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    if isinstance(meta.get("count"), int):
        state["available"] = meta["count"]
    state["next_cursor"] = meta.get("next_cursor")
    results = payload.get("results")
    return [item for item in (results if isinstance(results, list) else []) if isinstance(item, dict)]


def page_row(record: dict, rank: int) -> dict:
    """核对行：题录要点＋名次（名次＝该查询内被引降序的累计位次，即「深度」读数）。"""
    return {"key": record_key(record), "doi": record.get("doi"), "title": record.get("title"),
            "year": record.get("year"), "type": record.get("type"),
            "cited_by_count": record.get("citationCount"), "external_id": record.get("externalId"),
            "rank": rank}


def checklist_match(row: dict, checklist: list[dict]) -> list[str]:
    """清单项命中：带 doi 的项**只认 doi 相等**（词面不作第二判据）；无 doi 的项按 `gap_fill.contains_phrase`
    的词元序列整段口径匹配题名（避免子串误中，如缩写 LIPID 撞他文题名里的 lipid）。"""
    hits = []
    for item in checklist:
        if item["doi"]:
            if norm_doi(row.get("doi")) == item["doi"]:
                hits.append(item["id"])
        elif item["match"] and contains_phrase(str(row.get("title") or ""), item["match"]):
            hits.append(item["id"])
    return hits


def item_hit_rows(states: list[dict], item_id: str) -> list[dict]:
    """某清单项的通道命中行（跨查询去重按题录键，同键多查询只算一条）。"""
    seen: dict[str, dict] = {}
    for state in states:
        for row in state["records"]:
            if item_id in row["matched"]:
                seen.setdefault(row["key"] or f"{row['rank']}@{state['query']}", row)
    return list(seen.values())


def unique_hit(states: list[dict], item_id: str) -> dict | None:
    """同一性认定：恰好一条命中才认检出（零条＝未达，多义＝不认定，一律不当检出）。"""
    hits = item_hit_rows(states, item_id)
    return hits[0] if len(hits) == 1 else None


def band_floor(states: list[dict], checklist: list[dict]) -> int | None:
    """经典段带下沿＝**唯一认定检出**项的最低被引（每轮重算）；无＝None（无带信号）。
    多义命中不作检出、不进带下沿——否则他文被引会左右逐查询判停。"""
    counts = [hit["cited_by_count"] for item in checklist
              for hit in [unique_hit(states, item["id"])]
              if hit is not None and isinstance(hit["cited_by_count"], int)]
    return min(counts) if counts else None


def query_unique_ids(state: dict, checklist: list[dict]) -> set[str]:
    """该查询内「唯一认定检出」的清单项：多义命中不算——闭合与否只看可认定项。"""
    ids = set()
    for item in checklist:
        if item["id"] not in state["matched_ids"]:
            continue
        hits = [row for row in state["records"] if item["id"] in row["matched"] and row["key"]]
        keys = {row["key"] for row in hits}
        if len(keys) == 1:
            ids.add(item["id"])
    return ids


def decide_stop(state: dict, ctx: dict) -> str | None:
    """逐查询判停（票 08 §2 深度自适应）：闭合／耗尽／带下沿／无命中取满／无清单即首屏。
    闭合与「有命中」都只认**唯一认定**的清单项（多义命中无闭合信号，继续加深）。"""
    if not ctx["checklist"]:
        return STOP_NO_CHECKLIST
    state["unique_ids"] = sorted(query_unique_ids(state, ctx["checklist"]))
    if len(state["unique_ids"]) >= len(ctx["checklist"]):
        return STOP_CLOSED
    if state["short_page"] or not state["next_cursor"]:
        return STOP_EXHAUSTED
    if not state["unique_ids"] and state["pages"] >= NO_MATCH_PAGES:
        return STOP_NO_MATCH
    floor = ctx["band_floor"]
    if state["unique_ids"] and floor is not None and isinstance(state["boundary"], int) \
            and state["boundary"] < floor:
        return STOP_BAND
    return None


def consume_page(state: dict, items: list[dict], ctx: dict) -> None:
    """收下一屏：行入账（带名次）＋页内清单命中＋边界被引（页末条被引＝加深判据）。"""
    for offset, item in enumerate(items, start=1):
        row = page_row(openalex_record(item), state["depth"] + offset)
        row["matched"] = checklist_match(row, ctx["checklist"])
        state["records"].append(row)
        state["matched_ids"].update(row["matched"])
    state["depth"] += len(items)
    state["pages"] += 1
    state["short_page"] = len(items) < ctx["per_page"]
    state["boundary"] = state["records"][-1]["cited_by_count"] if state["records"] else None
    state["cursor"] = state["next_cursor"] or ""


def run_queries(ctx: dict, client: HttpClient) -> tuple[list[dict], str]:
    """轮次交替加深：每轮各查询各取一页，先取满基础面（1–3 请求）再按清单闭合加深。"""
    states = [new_state(query, ctx) for query in ctx["queries"]]
    stop_reason = STOP_DONE
    while not all(state["stop_reason"] is not None for state in states):
        ctx["band_floor"] = band_floor(states, ctx["checklist"])
        for state in states:
            if state["stop_reason"] is not None:
                continue
            if client.budget.used >= client.budget.limit:
                stop_reason = STOP_BUDGET
                state["stop_reason"] = STOP_BUDGET
                continue
            items = page_entries(state, client, ctx)
            if state["stop_reason"] is None and state["failure"] is not None:
                state["stop_reason"] = STOP_FAILED
            if state["stop_reason"] is not None:
                if state["stop_reason"] == STOP_BUDGET:
                    stop_reason = STOP_BUDGET
                continue
            consume_page(state, items, ctx)
            state["stop_reason"] = decide_stop(state, ctx)
        if stop_reason == STOP_BUDGET:
            for state in states:
                state["stop_reason"] = state["stop_reason"] or STOP_BUDGET
    return states, stop_reason


def write_raw(ctx: dict, state: dict) -> None:
    if not state["entries"]:
        return
    write_atomic(ctx["raw_dir"] / f"high-cited-{state['slug']}.json",
                 dumps({"channel": "high-cited", "query": state["query"], "slug": state["slug"],
                        "requests": state["entries"]}))


# ---------------------------------------------------------------- 对账、分类与产出


def merge_records(states: list[dict], terminal: dict) -> tuple[list[dict], dict]:
    """跨查询合并（同键取首见为展示行，名次逐条留在 appearances）并分池内外。"""
    merged: dict[str, dict] = {}
    for state in states:
        for row in state["records"]:
            key = row["key"]
            if not key:
                continue
            entry = merged.get(key)
            if entry is None:
                entry = merged[key] = {field: row[field] for field in
                                       ("key", "doi", "title", "year", "type", "cited_by_count",
                                        "external_id")}
                entry["appearances"], entry["matched"] = [], []
            entry["appearances"].append({"query": state["query"], "rank": row["rank"],
                                         "depth": state["depth"]})
            entry["matched"] = sorted(set(entry["matched"]) | set(row["matched"]))
            if entry["cited_by_count"] is None:
                entry["cited_by_count"] = row["cited_by_count"]
    rows = sorted(merged.values(), key=lambda row: (
        -(row["cited_by_count"] if isinstance(row["cited_by_count"], int) else -1),
        -(row["year"] if isinstance(row["year"], int) else -1)))
    absent = [row for row in rows if row["key"] not in terminal["keys"]]
    readings = {"unique_records": len(rows), "absent": len(absent), "present": len(rows) - len(absent),
                "no_identity": sum(1 for state in states for row in state["records"] if not row["key"])}
    return absent, readings


def window_bounds_text(window: dict) -> str:
    low = window.get("from") if isinstance(window.get("from"), int) else "—"
    high = window.get("to") if isinstance(window.get("to"), int) else "—"
    return f"[{low}, {high}]"


def out_of_window(row: dict, window: dict) -> str | None:
    """窗外：年越出日期窗（年缺＝判不了，归分类列前不改判）。"""
    year = row.get("year")
    if not isinstance(year, int) or not window:
        return None
    low, high = window.get("from"), window.get("to")
    if (isinstance(low, int) and year < low) or (isinstance(high, int) and year > high):
        return f"窗外：年 {year} ∉ {window_bounds_text(window)}"
    return None


VERDICT_ORDER = {VERDICT_RELEVANT: 0, VERDICT_OUT_OF_WINDOW: 1, VERDICT_UNJUDGED: 2, VERDICT_OFF_TOPIC: 3}


def classify(absent: list[dict], judgments: dict, window: dict) -> dict:
    """分类列：窗外（机械，优先）→ agent 判定（相关／超题）→ 未判；agent 判定原样留档。"""
    stats = {"absent_relevant": 0, "absent_off_topic": 0, "absent_out_of_window": 0, "absent_unjudged": 0}
    counter = {VERDICT_RELEVANT: "absent_relevant", VERDICT_OFF_TOPIC: "absent_off_topic",
               VERDICT_OUT_OF_WINDOW: "absent_out_of_window", VERDICT_UNJUDGED: "absent_unjudged"}
    attributed: set[str] = set()
    for row in absent:
        doi = norm_doi(row.get("doi"))
        judgement = judgments.get(doi) if doi else None
        if judgement:
            attributed.add(doi)
        row["window_out"] = out_of_window(row, window)
        row["agent_verdict"] = judgement["verdict"] if judgement else None
        row["agent_reason"] = judgement["reason"] if judgement else None
        if row["window_out"]:
            row["verdict"], row["verdict_reason"] = VERDICT_OUT_OF_WINDOW, row["window_out"]
        elif judgement:
            row["verdict"], row["verdict_reason"] = judgement["verdict"], judgement["reason"]
        else:
            row["verdict"] = VERDICT_UNJUDGED
            row["verdict_reason"] = "未给相关性判定（判定件未列该条目）"
        stats[counter[row["verdict"]]] += 1
    absent.sort(key=lambda row: (VERDICT_ORDER[row["verdict"]],
                                 -(row["cited_by_count"] if isinstance(row["cited_by_count"], int) else -1)))
    return {"stats": stats,
            "unmatched_judgments": [{"doi": doi, "verdict": entry["verdict"], "reason": entry["reason"],
                                     "index": entry["index"]}
                                    for doi, entry in judgments.items() if doi not in attributed]}


def resolve_kind(row: dict, judgement: dict | None, checklist: list[dict]) -> tuple[str | None, str]:
    """补入提案的实体类：agent 判定给的 kind → 清单项 kind（DOI 或词面同项）→ 题名设计词代理。"""
    if judgement and judgement.get("kind"):
        return judgement["kind"], "judgment"
    for item in checklist:   # 与 checklist_match 同口径：带 doi 只认 doi，无 doi 才走词元序列词面
        if item["doi"]:
            if norm_doi(row.get("doi")) == item["doi"]:
                return item["kind"], "checklist"
        elif item["match"] and contains_phrase(str(row.get("title") or ""), item["match"]):
            return item["kind"], "checklist"
    if design_token_title(row.get("title")):
        return "trial", "title_proxy"
    return None, "undefined"


def build_proposals(absent: list[dict], judgments: dict, checklist: list[dict], indication: list[str]) -> dict:
    """补入提案（接口行，非入池动作）：形态＝迭代补漏盘点表 `entities[]` 行，由 agent 按拍板并入。"""
    rows, skipped, seen_names = [], [], set()
    for row in absent:
        if row["verdict"] != VERDICT_RELEVANT:
            continue
        judgment = judgments.get(norm_doi(row.get("doi")))
        name = str(row.get("title") or "").strip()   # 名先行：name 空／重名会让 gap_fill 整表拒收，先剔
        if not name:
            skipped.append({"key": row["key"], "doi": row.get("doi"), "title": row.get("title"),
                            "reason": "题名缺（盘点表 `name` 必填，无法成行）——拍板补入时由 agent 补名"})
            continue
        if name.casefold() in seen_names:
            skipped.append({"key": row["key"], "doi": row.get("doi"), "title": row.get("title"),
                            "reason": "题名重复（盘点表 `name` 须唯一，同名会让盘点表整表拒收）"})
            continue
        kind, source = resolve_kind(row, judgment, checklist)
        if kind is None:
            skipped.append({"key": row["key"], "doi": row.get("doi"), "title": row.get("title"),
                            "reason": "类不可定（非试验形且判定件未给 kind），拍板补入时由 agent 指定"})
            continue
        seen_names.add(name.casefold())
        appearance = row["appearances"][0] if row["appearances"] else {}
        rows.append({
            "name": name, "kind": kind, "aliases": [], "drug": [],
            "basis": f"高被引缺席核对：查询「{appearance.get('query')}」名次 {appearance.get('rank')}"
                     f"（深度 {appearance.get('depth')}），被引 {row.get('cited_by_count')}"
                     f"（OpenAlex {row.get('external_id') or '∅'}）；agent 判定相关："
                     f"{row.get('agent_reason') or '未附理由'}",
            "doi": row.get("doi"), "year": row.get("year"),
            "cited_by_count": row.get("cited_by_count"), "kind_source": source,
        })
    digest = hashlib.sha1(json.dumps({"rows": rows, "skipped": skipped}, ensure_ascii=False,
                                     sort_keys=True).encode("utf-8")).hexdigest()
    return {"indication": list(indication), "rows": rows, "skipped": skipped, "digest": digest,
            "interface": "并入 run/b1/gap-entities.json 的 entities[] 后跑 gap_fill.py（本脚本不写盘点表、不并池）",
            "name_note": "name 取核对通道题名原文，拍板补入时按盘点表习惯改写（关键试验用缩写＋aliases）"}


def checklist_key(row: dict | None, item: dict) -> str:
    """清单项在终池里的键：通道行 DOI → 清单项 DOI → 通道行 `标题|年` 二级键。"""
    for candidate in ((row or {}).get("doi"), item["doi"]):
        key = norm_doi(candidate) if isinstance(candidate, str) else ""
        if key:
            return key
    if row and row.get("title"):
        return record_key({"doi": None, "title": row["title"], "year": row.get("year")})
    return ""


def build_checklist_block(ctx: dict, states: list[dict], terminal: dict) -> dict:
    """经典段：清单逐项检出（通道可见性，唯一认定）＋在池判定与 S 序位次；未达者逐条记因（机制＋证据）。

    同一性优先：带 doi 的项只认 doi；无 doi 的项按词元序列口径在通道与终池题名里认定，**唯一命中才算**，
    多义（≥2 条候选）既不作检出也不归属位次——宁可记「不认定」也不把位次算到他文头上。
    逐项只落 `in_terminal｜in_terminal_source｜doi｜order`：**被截判据在选取步**（ADR-0032：`in_terminal==true`
    且归一 DOI 不在研读选取集＝未选取，集合成员判据）；位次不可得与词面回查认定的项写进块 `notes`。"""
    items, unrecovered = [], []
    for item in ctx["checklist"]:
        hit_rows = item_hit_rows(states, item["id"])
        ambiguous = len(hit_rows) > 1
        row = None
        state = None
        if len(hit_rows) == 1:
            row = hit_rows[0]
            state = next((state for state in states if row in state["records"]), None)
        key = norm_doi(item["doi"]) if item["doi"] else (checklist_key(row, item) if row else "")
        in_terminal = bool(key and key in terminal["keys"])
        in_terminal_source = "doi" if in_terminal else None
        matched_title = None
        if not in_terminal and not item["doi"] and item["match"]:
            # 无 doi 才走词面回查终池题名（通道未达但在池者，如 LIPID）：唯一命中才归属键，order 才可算
            hits = [entry for entry in terminal["titles"] if contains_phrase(entry["title"], item["match"])]
            if len(hits) == 1:
                in_terminal, key, matched_title = True, hits[0]["key"], hits[0]["title"]
                in_terminal_source = "title"
            elif len(hits) > 1:
                in_terminal, in_terminal_source = None, "ambiguous_title"
        order = terminal["order"].get(key) if key else None
        entry = {"id": item["id"], "kind": item["kind"], "match": list(item["match"]),
                 "found": row is not None, "ambiguous": ambiguous, "ambiguous_hits": len(hit_rows),
                 "matched_title": matched_title, "in_terminal_source": in_terminal_source,
                 "query": state["query"] if state else None, "rank": row["rank"] if row else None,
                 "depth": state["depth"] if state else None,
                 "doi": (row or {}).get("doi") or item["doi"] or None,
                 "cited_by_count": (row or {}).get("cited_by_count"), "key": key or None,
                 "in_terminal": in_terminal, "order": order}
        items.append(entry)
        if entry["found"]:
            continue
        searched = "；".join(f"「{state['query']}」（集合 "
                             f"{state['available'] if state['available'] is not None else '∅'}，"
                             f"取 {state['depth']} 条）" for state in states)
        if ambiguous:
            unrecovered.append({"id": item["id"],
                                "reason": f"被引降序通道词面多义：{searched} 内该清单项词面命中 {len(hit_rows)} 条"
                                          f"候选（无法认定同一性），不作检出——请改用 DOI 或更具体的词面"
                                          f"（如试验全称＋标志性终点），本次如实记未认定"})
        else:
            unrecovered.append({"id": item["id"],
                                "reason": f"被引降序通道未达：{searched} 内词面与 DOI 均无命中——通道只记可见性，"
                                          f"词面不匹配或落在所取深度之外即成未达，机制不承诺全量召回"})
    found = sum(1 for entry in items if entry["found"])
    title_looked = [entry["id"] for entry in items if entry.get("in_terminal_source") == "title"]
    ambiguous_title = [entry["id"] for entry in items if entry.get("in_terminal_source") == "ambiguous_title"]
    # 不可得＝无 DOI（无法判研读选取集成员）或在池判定 ∅（词面多义）：选取步按此计数落 note
    undecidable = [entry["id"] for entry in items if entry["in_terminal"] is None or not entry["doi"]]
    notes: list[str] = []
    if terminal.get("order_rows_non_doi"):
        notes.append(f"排序报告 {terminal['order_rows_non_doi']} 行非 DOI 键（step2 的标题形键）不进 order 表"
                     f"——清单项若只以标题键匹配终池，S 序位次记 ∅（未取数不得写成位次在选取集内）")
    if undecidable:
        notes.append(f"经典段 {len(undecidable)} 项无 DOI 或在池判定 ∅——研读选取集成员判据不可得"
                     f"（选取步按此计数落 note，不得写成未被截）：{'、'.join(str(entry_id) for entry_id in undecidable)}")
    if title_looked:
        notes.append(f"经典段 {len(title_looked)} 项经**词面回查**认定在池（未经 DOI 校验，matched_title 留证）："
                     f"{'、'.join(title_looked)}")
    if ambiguous_title:
        notes.append(f"经典段 {len(ambiguous_title)} 项终池词面回查多义，在池与否记 ∅（不归属位次）："
                     f"{'、'.join(ambiguous_title)}")
    return {"state": "已跑" if items else "轮空", "checked": len(items), "found": found, "items": items,
            "unrecovered": unrecovered,
            "missing_note": None if items else "未给清单（经典段轮空，记 —）",
            **({"notes": notes} if notes else {})}


def build_mechanism(states: list[dict], stop_reason: str, stats: dict, disposition: dict,
                    absent_total: int) -> dict:
    """ADR-0023 冻结名：ran｜queries[]{query,depth}｜absent_total｜absent_*｜user_disposition。"""
    return {"ran": True,
            "queries": [{"query": state["query"], "depth": state["depth"]} for state in states],
            "absent_total": absent_total,
            "absent_relevant": stats["absent_relevant"],
            "absent_off_topic": stats["absent_off_topic"],
            "absent_out_of_window": stats["absent_out_of_window"],
            "absent_unjudged": stats["absent_unjudged"],
            "user_disposition": disposition["verdict"],
            "user_disposition_note": disposition["note"],
            "stop_reason": stop_reason}


def query_reading(state: dict) -> dict:
    return {"query": state["query"], "concept": state["concept"], "requests": state["requests"],
            "pages": state["pages"], "depth": state["depth"], "available": state["available"],
            "stop_reason": state["stop_reason"], "boundary_cited": state["boundary"],
            "matched_ids": sorted(state["matched_ids"]), "unique_ids": state["unique_ids"],
            "failure": state["failure"],
            "raw": f"{B1_DIR}/{RAW_DIR}/high-cited-{state['slug']}.json" if state["entries"] else None}


def intent_block(ctx: dict, judgments: dict, rejected: list[dict], unmatched: list[dict]) -> dict:
    return {"queries_path": ctx["queries_path"].as_posix(), "queries": ctx["queries"],
            "checklist_count": len(ctx["checklist"]), "window": ctx["window"],
            "indication": ctx["indication"], "judgments_path": ctx["judgments_path"].as_posix(),
            "judgments_present": ctx["judgments_path"].is_file(), "judgments_valid": len(judgments),
            "judgments_rejected": rejected, "judgments_unmatched": unmatched}


def build_report(ctx: dict, terminal: dict, states: list[dict], stop_reason: str, budget: dict,
                 judgments: dict, disposition: dict, rejected: list[dict], notes: list[str]) -> dict:
    absent, readings = merge_records(states, terminal)
    classified = classify(absent, judgments, ctx["window"])
    checklist_block = build_checklist_block(ctx, states, terminal)
    if readings["no_identity"]:
        notes.append(f"高被引缺席核对：通道 {readings['no_identity']} 条无身份（无 DOI 亦无标题），不进对账")
    if classified["unmatched_judgments"]:
        notes.append(f"高被引缺席核对：判定件 {len(classified['unmatched_judgments'])} 条未对上缺席清单"
                     f"（条目已在池或键不符），原样留档")
    return {
        "generated_at": date.today().isoformat(),
        "delivery": ctx["delivery"].as_posix(),
        "stage": STAGE_FETCH,
        "openalex": {"base": ctx["openalex_base"], "sort": "cited_by_count:desc",
                     "per_page": ctx["per_page"], "select": SELECT_CHECK},
        "inputs": intent_block(ctx, judgments, rejected, classified["unmatched_judgments"]),
        "budget": budget,
        "queries": [query_reading(state) for state in states],
        "terminal": {"source": terminal["source"], "sort_report": terminal["sort_report"],
                     "final_count": terminal["count"], "sort_rows": terminal["sort_rows"],
                     "order_rows": terminal["order_rows"],
                     "order_rows_non_doi": terminal["order_rows_non_doi"]},
        "absent": absent,
        "absent_readings": readings,
        "checklist": checklist_block,
        "pool_entry_proposals": build_proposals(absent, judgments, ctx["checklist"], ctx["indication"]),
        "mechanism": build_mechanism(states, stop_reason, classified["stats"], disposition, readings["absent"]),
        "classic_segment": {"state": checklist_block["state"], "checked": checklist_block["checked"],
                            "found": checklist_block["found"],
                            "unrecovered": checklist_block["unrecovered"]},
        "not_merged": {"pools_written": False, "gap_entities_written": False,
                       "note": "只核对不入池：本脚本只写 run/b1/high-cited-check.json 与 raw/high-cited-*.json，"
                               "不触碰 step1-pools.json，也不写 gap-entities.json"},
        "raw_dir": f"{B1_DIR}/{RAW_DIR}",
        "report_path": ctx["report_path"].as_posix(),
        "notes": notes,
    }


def update_classification(report: dict, ctx: dict, judgments: dict, disposition: dict,
                          rejected: list[dict], notes: list[str]) -> dict:
    """判定阶段：不发请求，按既有缺席清单重算分类列、提案与机制读数（取数读数原样保留）。"""
    absent = [row for row in (report.get("absent") or []) if isinstance(row, dict)]
    window = ctx["window"] or (report.get("inputs", {}).get("window") or {})
    classified = classify(absent, judgments, window)
    if classified["unmatched_judgments"]:
        notes.append(f"高被引缺席核对：判定件 {len(classified['unmatched_judgments'])} 条未对上缺席清单"
                     f"（条目已在池或键不符），原样留档")
    checklist_block = report.get("checklist") if isinstance(report.get("checklist"), dict) else {}
    # 旧形态件（ADR-0032 前）带着队列截线读数与逐项 `cut`／`in_queue`：重写本件时剥掉，不留废口径
    for row in checklist_block.get("items") or []:
        if isinstance(row, dict):
            row.pop("cut", None)
            row.pop("in_queue", None)
    report.pop("truncation", None)
    mechanism = report.get("mechanism") if isinstance(report.get("mechanism"), dict) else {}
    mechanism.update({"absent_total": len(absent), "user_disposition": disposition["verdict"],
                      "user_disposition_note": disposition["note"], **classified["stats"]})
    report["absent"] = absent
    report["inputs"] = intent_block(ctx, judgments, rejected, classified["unmatched_judgments"])
    report["stage"] = STAGE_CLASSIFY
    report["pool_entry_proposals"] = build_proposals(absent, judgments, ctx["checklist"], ctx["indication"])
    report["mechanism"] = mechanism
    report["classic_segment"] = {"state": checklist_block.get("state", "轮空"),
                                 "checked": checklist_block.get("checked", 0),
                                 "found": checklist_block.get("found", 0),
                                 "unrecovered": checklist_block.get("unrecovered", [])}
    kept = [note for note in (report.get("notes") or [])
            if not note.startswith(STALE_JUDGMENT_NOTES)]
    report["notes"] = kept + notes + ["判定阶段：不发请求，按既有读数件重算分类列"]
    return report


# ---------------------------------------------------------------- 打印


def print_absent(report: dict, limit: int = 5) -> None:
    """缺席清单：相关逐条全列（可动作面），其余按桶给计数＋前若干条（不静默截断）。"""
    for verdict in (VERDICT_RELEVANT, VERDICT_OUT_OF_WINDOW, VERDICT_UNJUDGED, VERDICT_OFF_TOPIC):
        rows = [row for row in report["absent"] if row["verdict"] == verdict]
        for index, row in enumerate(rows):
            if verdict != VERDICT_RELEVANT and index >= limit:
                print(f"高被引缺席核对·{verdict}：（共 {len(rows)} 条，余 {len(rows) - limit} 条见读数件）")
                break
            where = "；".join(f"{appearance['query']}（名次 {appearance['rank']}）"
                              for appearance in row["appearances"][:2])
            print(f"高被引缺席核对·{verdict}：{row.get('title')}"
                  f"（{row.get('year') if row.get('year') is not None else '∅'}；被引 "
                  f"{row.get('cited_by_count') if row.get('cited_by_count') is not None else '∅'}）"
                  f"｜{where}｜{row.get('verdict_reason')}")


def print_checklist(report: dict) -> None:
    block = report["checklist"]
    if not block["checked"]:
        print(f"高被引缺席核对·经典段：轮空（{block['missing_note']}）")
        return
    for entry in block["items"]:
        state = "检出" if entry["found"] else ("词面多义（不认定）" if entry.get("ambiguous") else "未达")
        rank = f"名次 {entry['rank']}／深度 {entry['depth']}" if entry["found"] else "名次 —／深度 —"
        pool = {True: "在池", False: "不在池", None: "在池与否 ∅（词面多义）"}[entry["in_terminal"]]
        order = f"order {entry['order']}" if isinstance(entry["order"], int) else "order ∅"
        print(f"高被引缺席核对·经典段：{entry['id']}｜{state}（{rank}）｜{pool}（{order}）")
    for row in block["unrecovered"]:
        print(f"高被引缺席核对·不可达经典：{row['id']}｜{row['reason']}")
    for note in block.get("notes") or []:
        print(f"高被引缺席核对·经典段注：{note}")


def print_summary(report: dict) -> None:
    mechanism = report["mechanism"]
    depths = "／".join(f"{row['query']}={row['depth']}" for row in report["queries"])
    requests = sum(row["requests"] for row in report["queries"])
    print(f"高被引缺席核对（{report['stage']}阶段）：查询 {len(report['queries'])} 组（深度 {depths}）｜"
          f"请求 {requests}／{report['budget']['limit']}｜唯一题录 {report['absent_readings']['unique_records']}"
          f"｜缺席 {mechanism['absent_total']}（相关 {mechanism['absent_relevant']}／超题 "
          f"{mechanism['absent_off_topic']}／窗外 {mechanism['absent_out_of_window']}／未判 "
          f"{mechanism['absent_unjudged']}）｜清单 {report['classic_segment']['state']} "
          f"{report['classic_segment']['checked']} 条（检出 "
          f"{report['classic_segment']['found']}／不可达 {len(report['classic_segment']['unrecovered'])}）｜"
          f"经典段是否被截由**选取步**算（`run/b1/selection.json` 的 `classic_segment_cut`＝在池且不在"
          f"研读选取集；本件只给逐项 `in_terminal｜doi｜order`）｜"
          f"处置 {mechanism['user_disposition']}｜判停 {mechanism['stop_reason']}")
    print(f"高被引缺席核对（补入接口）：提案 {len(report['pool_entry_proposals']['rows'])} 行（相关缺席项；"
          f"形态＝盘点表 entities 行）｜未成提案 {len(report['pool_entry_proposals']['skipped'])} 行｜"
          f"并入 run/b1/gap-entities.json 后跑 gap_fill.py，本通道不并池")
    for note in report["notes"]:
        print(note)
    print(f"高被引缺席核对（读数件）：{report['report_path']}｜原始响应 {report['raw_dir']}｜"
          f"只核对不入池（pools_written={report['not_merged']['pools_written']}）")


def print_prompt(report: dict) -> None:
    """规模确认提示句（逐字同 `references/protocols/hitl-protocol.md` 规模确认节「高被引缺席核对」块的
    提示句式，填充位已代；随规模确认同一问出示，不另起一问）。"""
    mechanism = report["mechanism"]
    relevant = [row for row in report["absent"] if row["verdict"] == VERDICT_RELEVANT]
    listed = "；".join(f"{row.get('title')}（{row.get('year') if row.get('year') is not None else '∅'}／被引 "
                       f"{row.get('cited_by_count') if row.get('cited_by_count') is not None else '∅'}）"
                       for row in relevant[:5])
    more = f"；余 {len(relevant) - 5} 条见读数件" if len(relevant) > 5 else ""
    print(f"高被引缺席核对（只核对不入池）：被引降序核对出「相关」缺席 {len(relevant)} 条"
          f"（{listed or '∅'}{more}）；另有超题 {mechanism['absent_off_topic']} 条、窗外 "
          f"{mechanism['absent_out_of_window']} 条、未判 {mechanism['absent_unjudged']} 条。"
          f"补入请在 Other 写「补入：<条目…>」；不写即不补。")


# ---------------------------------------------------------------- 主流程


def ctx_for(args: argparse.Namespace) -> dict:
    delivery = Path(args.delivery)
    b1 = delivery / B1_DIR
    return {
        "delivery": delivery, "b1": b1, "raw_dir": b1 / RAW_DIR,
        "dedupe_path": Path(args.dedupe_report) if args.dedupe_report else b1 / Path(DEDUPE_NAME).name,
        "sort_path": Path(args.sort_report) if args.sort_report else b1 / Path(SORT_NAME).name,
        "queries_path": Path(args.queries_file) if args.queries_file else b1 / Path(QUERIES_NAME).name,
        "judgments_path": Path(args.judgments_file) if args.judgments_file else b1 / Path(JUDGMENTS_NAME).name,
        "report_path": Path(args.report) if args.report else b1 / Path(REPORT_NAME).name,
        "openalex_base": args.openalex_base.rstrip("/"), "per_page": args.per_page,
        "page_interval": args.page_interval,
        "queries": [], "checklist": [], "window": {}, "indication": [], "band_floor": None,
    }


def load_report_inputs(report: dict, ctx: dict) -> None:
    """判定阶段：从读数件回填查询／清单／窗／适应症（形态已在校验期固定，缺项记空）。"""
    inputs = report.get("inputs") if isinstance(report.get("inputs"), dict) else {}
    ctx["queries"] = [row for row in (inputs.get("queries") or []) if isinstance(row, dict)]
    ctx["checklist"] = [{"id": row.get("id"), "kind": row.get("kind") or KINDS[0],
                          "match": [str(phrase) for phrase in (row.get("match") or [])],
                          "doi": norm_doi(row.get("doi")) if isinstance(row.get("doi"), str) else ""}
                        for row in ((report.get("checklist") or {}).get("items") or [])
                        if isinstance(row, dict)]
    ctx["window"] = inputs.get("window") or {}
    ctx["indication"] = inputs.get("indication") or []


def cmd_check(args: argparse.Namespace) -> int:
    ctx = ctx_for(args)
    for path, label in ((ctx["dedupe_path"], "终池"), (ctx["sort_path"], "排序报告")):
        if not path.is_file():
            print(f"高被引缺席核对错误：{label}缺失（{path}）——先跑 Step2 首轮排序", file=sys.stderr)
            return 2
    terminal = load_terminal(ctx["dedupe_path"], ctx["sort_path"])
    judgments, disposition, rejected, notes = load_judgments(ctx["judgments_path"])

    if args.classify_only:
        if not ctx["report_path"].is_file():
            print(f"高被引缺席核对错误：判定阶段需既有读数件（{ctx['report_path']}）", file=sys.stderr)
            return 2
        report = read_json(ctx["report_path"])
        if not isinstance(report, dict) or not isinstance(report.get("absent"), list):
            print(f"高被引缺席核对错误：读数件形态不符（{ctx['report_path']}）", file=sys.stderr)
            return 2
        load_report_inputs(report, ctx)
        report = update_classification(report, ctx, judgments, disposition, rejected, notes)
        write_atomic(ctx["report_path"], dumps(report))
    else:
        if not ctx["queries_path"].is_file():
            print(f"高被引缺席核对错误：核对输入缺失（{ctx['queries_path']}）——需 agent 产 1–{MAX_QUERIES} "
                  f"组主题关键概念查询", file=sys.stderr)
            return 2
        queries, checklist, window, indication, query_notes = load_queries(ctx["queries_path"])
        ctx.update({"queries": queries, "checklist": checklist, "window": window, "indication": indication})
        notes = query_notes + notes
        client = HttpClient(Budget(args.budget), Throttle())
        states, stop_reason = run_queries(ctx, client)
        for state in states:
            write_raw(ctx, state)
        budget = {"limit": args.budget, "used": client.budget.used,
                  "remaining": args.budget - client.budget.used, "stop_reason": stop_reason,
                  "aborted": stop_reason == STOP_BUDGET}
        report = build_report(ctx, terminal, states, stop_reason, budget, judgments, disposition,
                              rejected, notes)
        write_atomic(ctx["report_path"], dumps(report))
    print_checklist(report)
    print_absent(report)
    print_summary(report)
    print_prompt(report)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="高被引缺席核对器：被引降序 top-N 与终池对账（只核对不入池）")
    parser.add_argument("--delivery", required=True, help="交付目录（workspace/YYYY-MM-DD-<slug>）")
    parser.add_argument("--per-page", type=int, default=PER_PAGE,
                        help=f"每页条数（默认 {PER_PAGE}；上限 {PER_PAGE_CEILING}）")
    parser.add_argument("--budget", type=int, default=REQ_BUDGET, help=f"整轮请求预算含重试（默认 {REQ_BUDGET}）")
    parser.add_argument("--page-interval", type=float, default=0.0,
                        help="页间额外间隔秒（默认 0；匿名配额下按 08 资产 §3 实测节律可设 2.0）")
    parser.add_argument("--openalex-base", default=DEFAULT_OPENALEX_BASE, help="OpenAlex 基址（探针／代理用）")
    parser.add_argument("--queries-file", help=f"核对输入（默认 <交付>/{QUERIES_NAME}）")
    parser.add_argument("--judgments-file", help=f"相关性判定与处置（默认 <交付>/{JUDGMENTS_NAME}）")
    parser.add_argument("--dedupe-report", help=f"终池读数件（默认 <交付>/{DEDUPE_NAME}）")
    parser.add_argument("--sort-report", help=f"排序报告（默认 <交付>/{SORT_NAME}）")
    parser.add_argument("--report", help=f"读数落点（默认 <交付>/{REPORT_NAME}）")
    parser.add_argument("--classify-only", action="store_true",
                        help="判定阶段：不发请求，按既有读数件重算分类列与处置记录")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.per_page <= PER_PAGE_CEILING:
        print(f"高被引缺席核对错误：--per-page 应在 1..{PER_PAGE_CEILING}", file=sys.stderr)
        return 2
    if args.budget < 1:
        print("高被引缺席核对错误：--budget 应 ≥ 1", file=sys.stderr)
        return 2
    try:
        return cmd_check(args)
    except (OSError, KeyError, ValueError) as exc:
        print(f"高被引缺席核对错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
