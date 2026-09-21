# -*- coding: utf-8 -*-
"""迭代补漏（追加轮）执行器：终池实体盘点 → 两波定向补检索 → 并池与机制读数。

When: Step2 首轮排序完成后、下载前（与引文扩展轮同窗，票 05 §2／§9）；只跑全窗，近两年窗不重跑。
Do: uv run python .claude/skills/med-lit-review/scripts/gap_fill.py --delivery workspace/YYYY-MM-DD-<slug> \
        [--search-cli "node <repo>/.claude/skills/jadense-scholar-search/dist/bin/scholar-search.mjs"] \
        [--limit 20] [--provider pubmed] [--no-enrich] [--no-lexicon] [--no-pool]
Why: 运行中临时发起的补充检索（myelofibrosis #13–16 型）此前无判停与账目；本机制把它常设为
     发现层默认动作——盘点、追加轮、判停全自动，不新增 HITL 交互点。

读（交付内，只读）：`run/b1/sort-report.json`（排序终池＝盘点基准，`rows[].key` 为归一 DOI）、
`run/b1/step1-pools.json`（终池文本与主轮口径 `providers`／`limit_per_query`；同为并池目标）、
`run/b1/gap-entities.json`（agent 产盘点清单：四类实体＋逐行「为何预期该有」依据＋`indication`；
可选 `new_terms`＝波间回授的词表范畴新词／新检出词，每条 `{term, category, for?, edge?[], no_edge?, family?, canonical?, english?, note?}`）。

写（交付内，`run/b1/` 忽略位）：`gap-fill.json`（逐实体闭合表＋逐波读数＋机制块）、`raw/gap-fill-w<波>.json`
（波级原始响应）；追加轮池以 `pool_kind=追加轮`／`origin=gapfill_w<波>`／`window=全窗补·波<波>` 追加进
`step1-pools.json`（先撤同 `pool_kind` 旧池再追加，重跑幂等）。入池后由调用方**重跑 Step2**：
`uv run python .claude/skills/med-lit-review/scripts/step2_dedupe_sort_bucket.py --delivery <交付>`
——段键含 `step1-pools.json` 的 sha256，故并池后自动重算、快照按 DOI 重叠复用，**不新增缓存条款**。

机制口径（票 05 §3–§6）：盘点基准＝排序终池（非下载队列）；实体在终池 0 题录＝缺口，关键试验类仅有
次级文献（无 pivotal 主报告）＝缺口，其余判「在池」；两波封顶（波 1＝定向实体检索、波 2＝结果回授），
**主路径单次跑完两波**——回授词由 agent 读**终池题录／摘要**（盘上已有，无需二次运行）后随清单一次带进
`new_terms`，脚本同次调用即重拼未闭合实体的检索式并执行；每波 ≤4 query＝单次检索 CLI 运行上限
（`-q` 至多四次），合计 ≤8——两个上限在本脚本内写死，不做配置。
判停（票面口径）＝某波**无新增实体／词**即停：三面读数分记（新增题录数／新增实体数／新增词数），
判停取实体／词两面（未用的回授新词计在词面）；两波封顶仍存未闭合＝达上限。
跨次台账（可重入）：已发检索式与波号持久化在读数件（`query_ledger`）；二次调用不重发已发检索式
（结果已在池、闭合判定照样看得到）、波号续编，`waves`／`query_count` 按累计口径记、本轮数另记
`run_waves`／`run_query_count`——否则两次运行各发一遍波 1，会突破「合计 ≈≤8」。
实体闭合律：每实体终态＝在池｜未闭合（已定向搜过仍缺，如实记原因，句式接 §8 缺口块）；未搜实体分因：
确因上限＝`未搜（缺口实体超 8 query 上限）`，其余＝`未搜（未列入已跑的波次计划）`，计划内检索失败＝
`检索未完成（波N 未跑通）`。
词表回授：`new_terms` 里的词表范畴词（药物名／研发代号／疾病名／别名）先判词表三态，未命中才当场
`term_expand.py --append --source "追加轮"` 落候选队列；试验缩写不进候选队列，由脚本按题录形态检出后
记入机制读数 `detected_acronyms[]` 与盘点表（不作检索式）。

判据为机制代理、不是临床判断：pivotal 主报告＝题名命中该实体且类型非次级（类型缺失时要求题名含试验
设计词）；次级类型＝review／meta-analysis／editorial／comment／letter／erratum／paratext／guideline。

退出码：0 正常（零新增与部分失败如实进账，不算失败）；2 用法、输入文件或形态错误。
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import unicodedata
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from step3_queue import norm_doi, read_json  # noqa: E402 - 同族脚本跨件复用（DOI 归一与容错读 JSON 同口径）

REPO_ROOT = Path(__file__).resolve().parents[4]
SKILL_DIR = Path(__file__).resolve().parents[1]
TERM_EXPAND = SKILL_DIR / "scripts" / "term_expand.py"
DEFAULT_SEARCH_CLI = ("node "
                      + (REPO_ROOT / ".claude/skills/jadense-scholar-search/dist/bin/scholar-search.mjs").as_posix())

B1_DIR = "run/b1"
SORT_NAME = f"{B1_DIR}/sort-report.json"
POOLS_NAME = f"{B1_DIR}/step1-pools.json"
ENTITIES_NAME = f"{B1_DIR}/gap-entities.json"
REPORT_NAME = f"{B1_DIR}/gap-fill.json"
RAW_DIR = "raw"

POOL_KIND = "追加轮"
POOL_WINDOW = "全窗补"
POOL_FAMILY = {1: "迭代补漏·波1", 2: "迭代补漏·波2"}
POOL_ORIGIN = {1: "gapfill_w1", 2: "gapfill_w2"}
POOL_PREFIX = {1: "gapfill-w1", 2: "gapfill-w2"}

WAVE_QUERY_CAP = 4      # 每波 query 上限＝单次检索 CLI 运行上限（票 05 §4）
MAX_WAVES = 2           # 波数封顶（票 05 §5）
DEFAULT_LIMIT = 30
LIMIT_CEILING = 30   # 检索 CLI 的 --limit 契约（1..30）
DEFAULT_PROVIDERS = ("pubmed", "openalex")
DEFAULT_TIMEOUT = 900.0
LEXICON_SOURCE = "追加轮"   # 词表生长循环触发点记法（票 05 §6）

KINDS = ("trial", "drug", "disease", "endpoint")
KIND_LABEL = {"trial": "关键试验", "drug": "药物", "disease": "疾病", "endpoint": "终点"}
LEXICON_CATEGORIES = ("drug", "code", "disease", "alias")
LEXICON_CATEGORY_LABEL = {"drug": "药物名", "code": "研发代号", "disease": "疾病名", "alias": "别名"}
SECONDARY_TYPE_TOKENS = ("review", "meta-analysis", "meta analysis", "editorial", "comment", "letter",
                         "erratum", "paratext", "guideline")
DESIGN_TOKENS = ("randomis", "randomiz", "phase", "trial", "placebo", "double-blind", "open-label")
ACRONYM_RE = re.compile(r"[A-Z][A-Z0-9]{2,}(?:-[A-Z0-9]+)?")
DASH_RE = re.compile(r"[\u2010\u2011\u2012\u2013\u2014\u2212]")
ACRONYM_MIN_CHARS = 4   # 去连字符后的最短长度：滤掉 SR／MA／AE 这类设计噪声
ACRONYM_CAP = 50        # 读数封顶（超出另记 truncated 位，不静默截断）
# 机制检测用的通用缩写噪声（试验设计／统计／常见共病）：只影响「新检出缩写」列表，不进词表缓冲
ACRONYM_STOP = {
    "ABSTRACT", "BACKGROUND", "CONCLUSION", "CONCLUSIONS", "METHODS", "OBJECTIVE", "OBJECTIVES",
    "RESULTS", "INTRODUCTION", "DISCUSSION", "REVIEW", "RCT", "ITT", "CI", "HR", "OR", "RR", "NNT",
    "AE", "AES", "SAE", "TEAE", "QOL", "BMI", "SBP", "DBP", "ABPM", "CKD", "HF", "MI", "LVEF",
    "NYHA", "MMP", "JAK", "JAK2", "SGLT2", "ACEI", "ARB", "ARNI", "GDMT", "TNF", "IL", "COVID",
    "FDA", "EMA", "NICE", "WHO", "NIH", "US", "EU", "USA", "PMID", "DOI", "XML", "HTML", "PDF",
}
PUNCT_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")

STATE_IN_POOL = "在池"
STATE_UNCLOSED = "未闭合"
REASON_ZERO = "终池 0 题录（已定向搜过仍缺）"
REASON_SECONDARY = "仅次级文献（无 pivotal 主报告）"
REASON_TITLE_MISS = "主报告未由题名确认（命中题录非全为次级类型）"
REASON_UNSEARCHED = "未搜（缺口实体超 8 query 上限）"
REASON_NOT_PLANNED = "未搜（未列入已跑的波次计划）"
REASON_WAVES_SPENT = "未搜（波数已用满）"
REASON_UNKNOWN = "未判（本轮未得结论）"


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(path.suffix + ".staging")
    staging.write_text(text, encoding="utf-8")
    staging.replace(path)


# ---------------------------------------------------------------- 文本命中


def norm_text(text) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).casefold()


def phrase_tokens(text) -> list[str]:
    return [token for token in PUNCT_RE.split(norm_text(text)) if token]


def flat_text(text) -> str:
    return PUNCT_RE.sub("", norm_text(text))


def contains_phrase(text: str, phrases: list[str]) -> str | None:
    """实体命中判据：拉丁形按词元序列整段匹配（避免子串误中），中文按去标点子串匹配。"""
    if not text:
        return None
    hay_tokens, hay_flat = phrase_tokens(text), flat_text(text)
    for phrase in phrases:
        if CJK_RE.search(phrase):
            needle = flat_text(phrase)
            if needle and needle in hay_flat:
                return phrase
            continue
        needle = phrase_tokens(phrase)
        width = len(needle)
        if width and any(hay_tokens[i:i + width] == needle
                         for i in range(len(hay_tokens) - width + 1)):
            return phrase
    return None


def is_secondary(record_type: str) -> bool:
    return any(token in record_type for token in SECONDARY_TYPE_TOKENS)


def is_primary(rec: dict) -> bool:
    """pivotal 主报告代理判据：类型非次级（无类型时要求题名含试验设计词）。题名命中由调用方判。"""
    types = [text for text in (str(t).strip().casefold() for t in rec["types"]) if text]
    if any(is_secondary(text) for text in types):
        return False
    if types:
        return True
    title = norm_text(" ".join(rec["titles"]))
    return any(token in title for token in DESIGN_TOKENS)


def is_secondary_record(rec: dict) -> bool:
    types = [str(t).strip().casefold() for t in rec["types"] if str(t).strip()]
    return bool(types) and all(is_secondary(text) for text in types)


def record_key(rec: dict) -> str:
    """本轮读数用的题录身份键（DOI 一级、标题＋年二级）；正式去重归 Step2 R1。"""
    doi = norm_doi(rec.get("doi"))
    if doi:
        return doi
    title = flat_text(rec.get("title"))
    year = rec.get("year")
    return f"{title}|{year if year is not None else ''}" if title else ""


# ---------------------------------------------------------------- 盘点清单


def load_checklist(path: Path) -> tuple[list[dict], list[str], list[dict], list[str]]:
    """读盘点清单并校验形态（只校验不代判）：四类实体＋依据非空，`indication` 必填，`new_terms` 走词表范畴。"""
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("entities"), list):
        raise ValueError(f"盘点清单形态不符（需 {{\"entities\": […]}}）：{path}")
    notes: list[str] = []
    indication = [str(x).strip() for x in (data.get("indication") or []) if str(x).strip()]
    if not indication:
        raise ValueError(f"盘点清单缺 indication（追加轮检索式＝实体＋药物＋适应症）：{path}")

    entities, seen = [], set()
    for index, item in enumerate(data["entities"], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"盘点清单第 {index} 条非对象：{path}")
        name = str(item.get("name") or "").strip()
        kind = str(item.get("kind") or "").strip()
        basis = str(item.get("basis") or "").strip()
        problems = []
        if not name:
            problems.append("name 空")
        if kind not in KINDS:
            problems.append(f"kind 应为 {'／'.join(KINDS)}")
        if not basis:
            problems.append("basis 空（逐行须记「为何预期该有」）")
        if name.casefold() in seen:
            problems.append("name 重复")
        if problems:
            raise ValueError(f"盘点清单第 {index} 条不合格（{'；'.join(problems)}）：{path}")
        seen.add(name.casefold())
        entities.append({
            "index": index,
            "name": name,
            "kind": kind,
            "basis": basis,
            "aliases": [str(a).strip() for a in (item.get("aliases") or []) if str(a).strip()],
            "drug": [str(a).strip() for a in (item.get("drug") or []) if str(a).strip()],
        })
    if not entities:
        notes.append("迭代补漏：盘点清单 0 条实体（无缺口可判，机制空跑）")

    # 试验缩写不进词表候选队列（ADR-0016／票 05 §6）：清单里的关键试验形与之同名即拒绝
    trial_shapes = set()
    for entity in entities:
        if entity["kind"] == "trial":
            trial_shapes.update({entity["name"].casefold(), *(a.casefold() for a in entity["aliases"])})

    new_terms: list[dict] = []
    for index, item in enumerate(data.get("new_terms") or [], start=1):
        term = str((item or {}).get("term") or "").strip() if isinstance(item, dict) else ""
        category = str((item or {}).get("category") or "").strip() if isinstance(item, dict) else ""
        if not term or category not in LEXICON_CATEGORIES:
            raise ValueError(f"new_terms 第 {index} 条不合格（需 term ＋ category ∈ "
                             f"{'／'.join(LEXICON_CATEGORIES)}）：{path}")
        if term.casefold() in trial_shapes:
            raise ValueError(f"试验缩写不进候选队列（票 05 §6）：{term} 属关键试验形，"
                             f"记盘点表与机制读数，不落词表缓冲")
        edges = [str(edge).strip() for edge in (item.get("edge") or []) if str(edge).strip()]
        for edge in edges:
            if edge.count(":") != 2:
                raise ValueError(f"new_terms 第 {index} 条的 edge 应为 type:target:方向（target 不含冒号）：{edge}")
        new_terms.append({
            "term": term, "category": category, "for": str(item.get("for") or "").strip(),
            "edges": edges, "no_edge": bool(item.get("no_edge")),
            "family": str(item.get("family") or "").strip(),
            "canonical": str(item.get("canonical") or "").strip(),
            "english": str(item.get("english") or "").strip(),
            "note": str(item.get("note") or "").strip(),
        })
    return entities, indication, new_terms, notes


# ---------------------------------------------------------------- 终池与闭合


def load_terminal(ctx: dict) -> tuple[list[dict], list[str], dict, dict]:
    """排序终池＝盘点基准：`rows[].key` 定集合，题录／摘要／类型按 DOI 从池记录补齐（缺文本仍计条目）。"""
    sort_data = read_json(ctx["sort_path"])
    if not isinstance(sort_data, dict) or not isinstance(sort_data.get("rows"), list):
        raise ValueError(f"排序报告形态不符（需 {{\"rows\": […]}}）：{ctx['sort_path']}")
    pools_data = read_json(ctx["pools_path"])
    if not isinstance(pools_data, dict) or not isinstance(pools_data.get("pools"), list):
        raise ValueError(f"发现清单形态不符（需 {{\"pools\": […]}}）：{ctx['pools_path']}")

    by_doi: dict[str, dict] = {}
    malformed = 0
    for pool in pools_data["pools"]:
        if not isinstance(pool, dict):
            malformed += 1
            continue
        for rec in pool.get("results") or []:
            if not isinstance(rec, dict):
                malformed += 1
                continue
            doi = norm_doi(rec.get("doi"))
            if not doi:
                continue
            entry = by_doi.setdefault(doi, {"titles": [], "abstracts": [], "types": []})
            if rec.get("title"):
                entry["titles"].append(str(rec["title"]))
            if rec.get("abstract"):
                entry["abstracts"].append(str(rec["abstract"]))
            if rec.get("type"):
                entry["types"].append(str(rec["type"]))

    terminal, no_doi, textless = [], 0, 0
    for row in sort_data["rows"]:
        if not isinstance(row, dict):
            continue
        raw_key = row.get("key") if isinstance(row.get("key"), str) else row.get("doi")
        doi = norm_doi(raw_key) if isinstance(raw_key, str) else ""
        if not doi:
            no_doi += 1
            continue
        base = by_doi.get(doi)
        titles = ([str(row["title"])] if row.get("title") else []) + (base["titles"] if base else [])
        terminal.append({"doi": doi, "titles": titles,
                         "abstracts": base["abstracts"] if base else [],
                         "types": base["types"] if base else []})
        if not titles and not (base and base["abstracts"]):
            textless += 1

    notes = []
    if malformed:
        notes.append(f"迭代补漏：发现清单 {malformed} 条非对象条目（已跳过，形态问题不冒栈）")
    if no_doi:
        notes.append(f"迭代补漏：排序终池 {no_doi} 行无可用 DOI，盘点跳过（不计基准）")
    if textless:
        notes.append(f"迭代补漏：终池 {textless} 条无题录／摘要文本，按可得文本判命中")
    info = {"sort_report": ctx["sort_path"].as_posix(), "terminal_records": len(terminal),
            "pool_records": len(by_doi), "no_doi_rows": no_doi, "textless_records": textless}
    return terminal, notes, info, pools_data


def evaluate(entities: list[dict], states: list[dict], terminal: list[dict], wave: int) -> None:
    """逐实体重判闭合：基准＝排序终池 ∪ 本轮已入池的追加轮题录（并池重跑后终池的等价读数）。"""
    for entity, state in zip(entities, states):
        phrases = [entity["name"], *entity["aliases"]]
        matched = title_hit = primary = secondary_hits = 0
        for rec in terminal:
            title_text = " ".join(rec["titles"])
            full_text = title_text + " " + " ".join(rec["abstracts"])
            if not contains_phrase(full_text, phrases):
                continue
            matched += 1
            if is_secondary_record(rec):
                secondary_hits += 1
            in_title = bool(contains_phrase(title_text, phrases))
            if in_title:
                title_hit += 1
                if is_primary(rec):
                    primary += 1
        state.update({"matched": matched, "title_hit": title_hit, "primary": primary,
                      "secondary_hits": secondary_hits})
        gap, reason = False, None
        if matched == 0:
            gap, reason = True, REASON_ZERO
        elif entity["kind"] == "trial" and primary == 0:
            gap = True
            reason = REASON_SECONDARY if secondary_hits == matched else REASON_TITLE_MISS
        if not gap:
            if state["state"] != STATE_IN_POOL:
                state["closed_at"] = "基线" if wave == 0 else f"波{wave}"
            state["state"], state["reason"] = STATE_IN_POOL, None
        else:
            state["state"], state["reason"] = STATE_UNCLOSED, reason


def init_states(entities: list[dict]) -> list[dict]:
    return [{"index": index, "name": e["name"], "kind": e["kind"], "basis": e["basis"],
             "state": STATE_UNCLOSED, "reason": None, "closed_at": None,
             "matched": 0, "title_hit": 0, "primary": 0, "secondary_hits": 0,
             "queries": [], "waves": []} for index, e in enumerate(entities)]


def build_query(entity: dict, indication: list[str], extra: tuple[str, ...] = ()) -> str:
    """追加轮检索式：实体名＋别名／代号＋药物＋适应症（先例空格组合形态，票 04 关闭后为常态）。"""
    parts, seen = [], set()
    for raw in [entity["name"], *entity["aliases"], *entity["drug"], *extra, *indication]:
        text = str(raw).strip()
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            parts.append(text)
    return " ".join(parts)


# ---------------------------------------------------------------- 波规划


def plan_wave(wave: int, entities: list[dict], states: list[dict], indication: list[str],
              new_terms: list[dict], issued: set[str]) -> list[dict]:
    """波级检索计划：波 1＝缺口实体定向检索；波 2＝未闭合实体（带 agent 回授的新词）＋新检出词。
    机械刮出的缩写不作检索式（只是读数，喂盘点表）——先例见 myelo 包 #13–16：补检索由实体名成式。"""
    plan: list[dict] = []
    unclosed = [s for s in states if s["state"] == STATE_UNCLOSED]

    if wave == 1:
        for state in unclosed:
            if len(plan) >= WAVE_QUERY_CAP:
                break
            entity = entities[state["index"]]
            query = build_query(entity, indication)
            if query in issued:
                continue  # 同式已发过：不重发（结果已在池里）
            plan.append({"query": query, "source": "缺口实体", "entity": entity["name"], "refine": []})
        return plan

    for state in unclosed:
        if len(plan) >= WAVE_QUERY_CAP:
            break
        entity = entities[state["index"]]
        refine = tuple(term["term"] for term in new_terms if term["for"].casefold() == entity["name"].casefold())
        query = build_query(entity, indication, refine)
        if query in state["queries"]:
            continue  # 无新词可回授：同式不重跑（同一检索式不打两次）
        if query in issued:
            continue
        plan.append({"query": query,
                     "source": "未闭合实体（回授新词）" if refine else "未闭合实体",
                     "entity": entity["name"], "refine": list(refine)})
    known = {e["name"].casefold() for e in entities}
    for term in new_terms:
        if len(plan) >= WAVE_QUERY_CAP:
            break
        if term["for"] and term["for"].casefold() in known:
            continue  # 已随所属实体的回授式进入本轮，不重复出一条
        query = " ".join([term["term"], *indication])
        if query in issued:
            continue
        plan.append({"query": query, "source": "新检出词", "entity": None, "term": term["term"],
                     "refine": []})
    return plan


def detect_acronyms(records: list[dict], known: set[str], seen: set[str]) -> tuple[list[dict], bool]:
    """机制检测波内题录缩写（读错只作读数与盘点表，不进候选队列、不直接成检索式）：

    连字符先归一（含 U+2010 等排版连字符），去连字符后 <4 字符或命中通用噪声表即丢；读数封顶
    ACRONYM_CAP 条（超出另记截断位）。agent 读这些读数与题录后决定下一波／下个交付的盘点表。"""
    found, truncated = [], False
    for rec in records:
        title = str(rec.get("title") or "")
        for token in ACRONYM_RE.findall(DASH_RE.sub("-", title)):
            key = token.casefold()
            if key in known or key in seen or token.upper() in ACRONYM_STOP:
                continue
            if len(token.replace("-", "")) < ACRONYM_MIN_CHARS:
                continue
            seen.add(key)
            if len(found) >= ACRONYM_CAP:
                truncated = True
                continue
            found.append({"acronym": token, "title": title})
    return found, truncated


# ---------------------------------------------------------------- 检索与并池


def run_search(plan: list[dict], ctx: dict, wave: int) -> tuple[list[dict], dict]:
    """单波一次检索 CLI 运行（`-q` ≤4）：原始响应落 raw/，读数三分（命中／入池／无标识丢弃）。"""
    row = {"wave": wave, "wave_label": f"全窗补·波{wave}", "queries": plan,
           "providers": list(ctx["providers"]), "limit": ctx["limit"], "enrich": ctx["enrich"],
           "status": "ok", "failure": None, "requests": 0, "hits": 0, "pooled": 0,
           "identity_dropped": 0, "statuses": [], "diagnostics": [], "collected_at": [],
           "cache_hits": 0, "truncated": 0, "raw": None}

    parts = [p for p in shlex.split(ctx["search_cli"].replace("\\", "/")) if p]
    if not parts:
        row.update({"status": "failed", "failure": "检索 CLI 命令为空"})
        return [], row
    cmd = list(parts)
    for item in plan:
        cmd += ["-q", item["query"]]
    for provider in ctx["providers"]:
        cmd += ["-p", provider]
    cmd += ["--limit", str(ctx["limit"]), "--format", "json"]
    if ctx["enrich"]:
        cmd.append("--enrich")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=ctx["timeout"])
    except (OSError, subprocess.TimeoutExpired) as err:
        row.update({"status": "failed",
                    "failure": f"检索 CLI 未跑通：{type(err).__name__}: {err}"})
        return [], row
    if proc.returncode != 0:
        row.update({"status": "failed",
                    "failure": f"检索 CLI 退出码 {proc.returncode}：{(proc.stderr or '').strip()[:200]}"})
        return [], row
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as err:
        row.update({"status": "failed", "failure": f"检索 CLI 输出非 JSON：{err}"})
        return [], row

    write_atomic(ctx["raw_dir"] / f"gap-fill-w{wave}.json", dumps(payload))
    row["raw"] = f"{B1_DIR}/{RAW_DIR}/gap-fill-w{wave}.json"
    plan_data = payload.get("queryPlan") if isinstance(payload.get("queryPlan"), dict) else {}
    for status in plan_data.get("queryStatuses") or []:
        if not isinstance(status, dict):
            continue
        entry = {key: status.get(key) for key in
                 ("query", "provider", "status", "hitCount", "totalAvailable", "capHit",
                  "capPolicy", "attempts", "retried", "cacheHit", "collectedAt")}
        row["statuses"].append(entry)
        row["requests"] += int(entry.get("attempts") or 0)
        if entry.get("cacheHit"):
            row["cache_hits"] += 1
        if entry.get("capHit"):
            row["truncated"] += 1
        if entry.get("collectedAt"):
            row["collected_at"].append(str(entry["collectedAt"]))
        if entry.get("status") not in ("success", "cached", None):
            row["diagnostics"].append(f"{entry.get('provider')}／{entry.get('query')}：{entry.get('status')}")
    for diag in payload.get("diagnostics") or []:
        if isinstance(diag, dict):
            row["diagnostics"].append(f"{diag.get('severity')}：{diag.get('message')}")
    row["collected_at"] = sorted(set(row["collected_at"]))

    raw_records = [rec for rec in (payload.get("results") or []) if isinstance(rec, dict)]
    records = [project(rec) for rec in raw_records]
    pooled = [rec for rec in records if rec.get("doi")]
    row["hits"] = len(records)
    row["pooled"] = len(pooled)
    row["identity_dropped"] = len(records) - len(pooled)
    if not records:
        row["status"] = "empty"
    return pooled, row


def project(rec: dict) -> dict:
    """投影为 Step2 `load_pools` 消费形态（与交付内装配脚本同口径）：authors 取字符串、类型与年取自 sourceMetadata。"""
    meta = rec.get("sourceMetadata") if isinstance(rec.get("sourceMetadata"), dict) else {}
    authors = [a.get("displayName") if isinstance(a, dict) else a for a in (rec.get("authors") or []) if a]
    return {
        "doi": rec.get("doi"),
        "title": rec.get("title"),
        "authors": [a for a in authors if a],
        "venue": rec.get("venue"),
        "year": meta.get("publicationYear"),
        "publishedDate": rec.get("publishedDate"),
        "type": meta.get("type") or rec.get("type"),
        "abstract": rec.get("abstract"),
        "citationCount": rec.get("citationCount"),
        "url": rec.get("url"),
        "externalSource": rec.get("externalSource"),
        "externalId": rec.get("externalId"),
        "retrievalProvider": rec.get("retrievalProvider"),
    }


def is_gap_pool(pool: object) -> bool:
    return isinstance(pool, dict) and pool.get("pool_kind") == POOL_KIND


def pool_for(wave: int, plan: list[dict], records: list[dict], ctx: dict) -> dict:
    return {
        "pool_id": POOL_PREFIX[wave],
        "pool_kind": POOL_KIND,
        "family": POOL_FAMILY[wave],
        "window": f"{POOL_WINDOW}·波{wave}",
        "origin": POOL_ORIGIN[wave],
        "query": "；".join(item["query"] for item in plan),
        "providers": list(ctx["providers"]),
        "results": records,
    }


def pool_record_keys(pools: list) -> set[str]:
    """池内题录身份键集合：非对象条目跳过（畸形清单不该冒栈，退出码契约见文件头）。"""
    keys = set()
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        for rec in pool.get("results") or []:
            if not isinstance(rec, dict):
                continue
            key = record_key(rec)
            if key:
                keys.add(key)
    return keys


def wave_of_pool(pool: dict) -> int | None:
    """追加轮池的波号（池 id 形如 gapfill-w<N>）；不可辨记 None。"""
    match = re.search(r"-w(\d+)$", str(pool.get("pool_id") or ""))
    return int(match.group(1)) if match else None


def load_prior(ctx: dict, pools_data: dict) -> tuple[dict, list[dict], list[dict], list[str]]:
    """跨次运行台账（可重入）：返回（prior 概要、先前波行、先前入池题录、notes）。

    主路径是**单次跑完两波**——回授词读终池题录／摘要（盘上已有）后随清单一次带入 `new_terms`，
    同次调用即完成「盘点→波 1→回授→波 2」。本台账只兜二义安全：二次调用**不重发已发检索式**
    （结果已在池、闭合判定照样看得到）、波号续编、query 合计按累计口径记——否则两次运行各发一遍
    波 1，会突破「每波 ≤4、合计 ≈≤8」的预算口径。
    """
    notes: list[str] = []
    records, pool_waves = [], {}
    for pool in pools_data["pools"]:
        if not isinstance(pool, dict) or not is_gap_pool(pool):
            continue
        rows = [rec for rec in (pool.get("results") or []) if isinstance(rec, dict)]
        records.extend(rows)
        entry = pool_waves.setdefault(wave_of_pool(pool), {"records": 0, "queries": []})
        entry["records"] += len(rows)
        entry["queries"].extend(text for text in str(pool.get("query") or "").split("；") if text)

    readings = read_json(ctx["report_path"])
    queries, prior_waves, source = [], [], "无先前运行"
    if isinstance(readings, dict) and isinstance(readings.get("waves"), list):
        source = "读数件"
        prior_waves = [row for row in readings["waves"] if isinstance(row, dict)]
        ledger = readings.get("query_ledger") if isinstance(readings.get("query_ledger"), dict) else {}
        queries = [row for row in (ledger.get("queries") or []) if isinstance(row, dict)]

    ordered = sorted(pool_waves.items(), key=lambda item: (item[0] is None, item[0]))
    if pool_waves:
        pooled_ledger_waves = {wave for wave in pool_waves if isinstance(wave, int)}
        known = {row.get("wave") for row in queries if isinstance(row.get("wave"), int)}
        if pooled_ledger_waves - known:
            notes.append(f"迭代补漏：追加轮池波 {sorted(pooled_ledger_waves - known)} 不在台账里"
                         f"（读数件缺失或已清）：该池题录仍计入合计与闭合判定")
        if not queries:
            queries = [{"wave": wave, "query": text, "entity": None, "source": "池重建"}
                       for wave, data in ordered for text in data["queries"]]
        if not prior_waves:
            source = "追加轮池重建"
            prior_waves = [{"wave": wave, "wave_label": f"{POOL_WINDOW}·波{wave}", "reconstructed": True,
                            "queries": [{"query": text, "source": "池重建", "entity": None}
                                        for text in data["queries"]],
                            "providers": [], "records": data["records"]} for wave, data in ordered]

    issued = {str(row.get("query")) for row in queries if row.get("query")}
    max_wave = max([row["wave"] for row in queries if isinstance(row.get("wave"), int)] or [0])
    prior = {"source": source, "readings": ctx["report_path"].as_posix() if source == "读数件" else None,
             "records": len(records), "waves": max_wave, "query_count": len(issued),
             "queries": [{"wave": row.get("wave"), "query": str(row.get("query")),
                          "entity": row.get("entity"), "source": row.get("source")}
                         for row in queries if row.get("query")],
             "issued": issued, "start_wave": min(max_wave + 1, MAX_WAVES + 1)}
    return prior, prior_waves, records, notes


def append_pools(path: Path, pools_data: dict, pools: list[dict]) -> None:
    """幂等并池：先撤同 `pool_kind` 的旧池，再按波序追加；顶层其余键与缩进照旧，写走原子替换。"""
    text = path.read_text(encoding="utf-8")
    match = re.search(r"\n( +)\"", text)
    kept = [pool for pool in pools_data["pools"] if not is_gap_pool(pool)]
    merged = dict(pools_data)
    merged["pools"] = kept + pools
    write_atomic(path, json.dumps(merged, ensure_ascii=False,
                                  indent=len(match.group(1)) if match else 1) + "\n")


# ---------------------------------------------------------------- 词表回授


def lexicon_argv(ctx: dict) -> list[str]:
    argv = [sys.executable, str(TERM_EXPAND)]
    if ctx["lexicon_buffer"]:
        argv += ["--buffer", ctx["lexicon_buffer"]]
    return argv


def append_argv(term: dict, ctx: dict) -> tuple[list[str], str]:
    """落队命令行：初判边随条目写（`--edge`）；判不出边按 §6 AFK 提案口径显式 `--no-edge`，
    免把无声明条目送进交付门禁（无边且未声明 no_edge 即机械生长判红）。"""
    argv = [*lexicon_argv(ctx), term["term"], "--append", "--source", LEXICON_SOURCE]
    if term["edges"]:
        for edge in term["edges"]:
            argv += ["--edge", edge]
        edge_source = "盘点清单初判边"
    else:
        argv.append("--no-edge")
        edge_source = "脚本补 no_edge（AFK 提案口径）" if not term["no_edge"] else "盘点清单显式无边"
    for flag, key in (("--family", "family"), ("--canonical", "canonical"), ("--english", "english")):
        if term[key]:
            argv += [flag, term[key]]
    return argv, edge_source


def term_status(term: str, ctx: dict) -> tuple[str | None, str]:
    """词表三态（expanded／hit_no_expansion／miss）：只有 miss 才当场落队（命中者不需落队）。"""
    try:
        proc = subprocess.run([*lexicon_argv(ctx), term, "--format", "json"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired) as err:
        return None, f"{type(err).__name__}: {err}"
    if proc.returncode != 0:
        return None, ((proc.stderr or proc.stdout or "").strip()[:200] or f"退出码 {proc.returncode}")
    try:
        status = json.loads(proc.stdout).get("status")
        return (str(status) or None) if status else None, ""
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None, "term_expand 输出非 JSON"


def apply_lexicon(new_terms: list[dict], ctx: dict) -> list[dict]:
    """词表范畴新词当场落候选队列（未命中才 `--append --source "追加轮"`）；失败如实记读数、不冒泡。"""
    rows: list[dict] = []
    for term in new_terms:
        row = {"term": term["term"], "category": term["category"],
               "category_label": LEXICON_CATEGORY_LABEL[term["category"]], "for": term["for"] or None,
               "edge": term["edges"] or None, "edge_source": None, "lexicon_status": None,
               "command": None, "status": "ok", "note": None}
        if not ctx["lexicon"]:
            row.update({"status": "skipped", "note": "--no-lexicon：未落候选队列（探针）"})
            rows.append(row)
            continue
        status, error = term_status(term["term"], ctx)
        row["lexicon_status"] = status
        if status is None:
            row.update({"status": "failed", "note": f"词表判定未跑通：{error}"})
            rows.append(row)
            continue
        if status != "miss":
            row.update({"status": "skipped", "note": f"词表状态 {status}：命中者不需落队"})
            rows.append(row)
            continue
        argv, edge_source = append_argv(term, ctx)
        row["edge_source"] = edge_source
        row["command"] = "uv run python scripts/term_expand.py " + " ".join(
            f'"{part}"' if " " in part else part for part in argv[2:])
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=120)
        except (OSError, subprocess.TimeoutExpired) as err:
            row.update({"status": "failed", "note": f"{type(err).__name__}: {err}"})
            rows.append(row)
            continue
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if proc.returncode != 0 or "候选已入队" not in output:
            row.update({"status": "failed", "note": output[-200:] or f"退出码 {proc.returncode}"})
        else:
            row["note"] = "候选已入队（回读确认以候选队列为准）"
        rows.append(row)
    return rows


# ---------------------------------------------------------------- 主流程


def ctx_for(args: argparse.Namespace) -> dict:
    delivery = Path(args.delivery)
    b1 = delivery / B1_DIR
    return {
        "delivery": delivery,
        "b1": b1,
        "raw_dir": b1 / RAW_DIR,
        "sort_path": Path(args.sort_report) if args.sort_report else b1 / Path(SORT_NAME).name,
        "pools_path": Path(args.pools) if args.pools else b1 / Path(POOLS_NAME).name,
        "entities_path": Path(args.entities) if args.entities else b1 / Path(ENTITIES_NAME).name,
        "report_path": Path(args.report) if args.report else b1 / Path(REPORT_NAME).name,
        "search_cli": args.search_cli,
        "providers": tuple(args.provider) if args.provider else None,
        "limit": args.limit,
        "enrich": args.enrich,
        "timeout": args.timeout,
        "lexicon": args.lexicon,
        "lexicon_buffer": args.lexicon_buffer,
        "pool": args.pool,
    }


def print_plan_rows(report: dict) -> None:
    """检索策略「检索式逐条」追加轮行：窗列「全窗补」，多波标「全窗补·波N」；先前波行一并打印。"""
    multi = report["mechanism"]["waves"] > 1
    rows = ([(wave, "先前波") for wave in report["prior_waves"]]
            + [(wave, "本轮") for wave in report["waves"]])
    for wave, origin in rows:
        window = f"{POOL_WINDOW}·波{wave['wave']}" if multi else POOL_WINDOW
        per_query = {}
        for status in wave.get("statuses") or []:
            per_query[status.get("query")] = per_query.get(status.get("query"), 0) + int(status.get("hitCount") or 0)
        for item in wave.get("queries") or []:
            hits = per_query.get(item.get("query"))
            print(f"迭代补漏·检索式逐条：窗 {window}｜来源 {'+'.join(wave.get('providers') or []) or '随主轮'}｜"
                  f"检索式「{item.get('query')}」｜limit {wave.get('limit') or '随主轮'}｜"
                  f"执行日期 {report['generated_at']}｜"
                  f"数据采集 {'／'.join(wave.get('collected_at') or []) or '见读数件'}｜"
                  f"命中 {hits if hits is not None else '∅'}｜来源类型 {item.get('source')}｜{origin}")


def print_entities(report: dict) -> None:
    for entity in report["entities"]:
        print(f"迭代补漏·实体：{entity['name']}（{KIND_LABEL[entity['kind']]}）｜依据 {entity['basis']}｜"
              f"终态 {entity['state']}｜终池命中 {entity['matched']}（题名命中 {entity['title_hit']}／"
              f"主报告 {entity['primary']}）｜闭合于 {entity['closed_at'] or '—'}"
              + (f"｜原因 {entity['reason']}" if entity["reason"] else ""))


def print_gaps(report: dict) -> None:
    """未闭合实体逐条留痕；末行是可原样抄进 §8 局限声明的缺口句（不另加日志前缀）。"""
    for item in report["mechanism"]["unclosed_entities"]:
        query = item["queries"][-1] if item["queries"] else "（未定向搜过）"
        print(f"迭代补漏·未闭合：{item['name']}（{KIND_LABEL[item['kind']]}）｜query「{query}」｜"
              f"波 {'／'.join(str(w) for w in item['waves']) or '—'}｜仍缺：{item['reason']}")
    for item in report["mechanism"]["unclosed_entities"]:
        print(f"- 迭代补漏缺口：{item['name']}（{KIND_LABEL[item['kind']]}）"
              f"按「{item['queries'][-1] if item['queries'] else '（未定向搜过）'}」全窗定向检索"
              f"（波 {'／'.join(str(w) for w in item['waves']) or '—'}）后，{item['reason']}；"
              f"相关结论待补检索或待新发表，本次如实记局限。")


def print_summary(report: dict) -> None:
    mechanism = report["mechanism"]
    kinds = {}
    for entity in report["entities"]:
        kinds[KIND_LABEL[entity["kind"]]] = kinds.get(KIND_LABEL[entity["kind"]], 0) + 1
    wave_rows = "／".join(f"波{wave['wave']} query {len(wave['queries'])}"
                         for wave in report["waves"]) or "波数 0"
    prior = report["prior_run"]
    if prior["waves"]:
        print(f"迭代补漏·先前运行：{prior['source']}"
              f"（波 {prior['waves']}／query {prior['query_count']}／入池题录 {prior['records']}）"
              f"｜本轮自波 {prior['start_wave']} 起，已发检索式不重发"
              + (f"（读数件 {prior['readings']}）" if prior["readings"] else ""))
    unclosed = mechanism["unclosed_entities"]
    unclosed_names = "：" + "、".join(item["name"] for item in unclosed) if unclosed else ""
    print(f"迭代补漏：ran={mechanism['ran']}｜实体清单 {mechanism['entity_checklist_count']}"
          f"（{'／'.join(f'{k} {v}' for k, v in kinds.items())}）"
          f"｜疑似缺口 {mechanism['gap_entity_count']}"
          f"｜波数 {mechanism['waves']}／{MAX_WAVES}（本轮 {mechanism['run_waves']}：{wave_rows}）"
          f"｜query 合计 {mechanism['query_count']}／{WAVE_QUERY_CAP * MAX_WAVES}"
          f"（本轮 {mechanism['run_query_count']}）"
          f"｜新增题录 去重前 {mechanism['new_before_dedupe']}／去重后 {mechanism['new_after_dedupe']}"
          f"／入池新增 {report['new_records']['new_to_pools']}"
          f"｜本轮新增实体 {mechanism['new_entity_count']}／新增词 {mechanism['new_word_count']}"
          f"｜判停 {mechanism['closure']}（{mechanism['stop_reason']}）"
          f"｜未闭合 {len(mechanism['unclosed_entities'])}{unclosed_names}")
    for wave in report["waves"]:
        print(f"迭代补漏·波{wave['wave']}：query {len(wave['queries'])}（≤{WAVE_QUERY_CAP}）｜"
              f"来源 {'+'.join(wave['providers'])}｜limit {wave['limit']}"
              f"｜请求 {wave['requests']}（缓存命中 {wave['cache_hits']}）"
              f"｜命中 {wave['hits']}／入池 {wave['pooled']}／无标识丢弃 {wave['identity_dropped']}"
              f"｜新增题录（对检索池与在前的波）{wave['new_keys']}"
              f"｜新增实体 {wave['new_entity_count']}／新增词 {wave['new_word_count']}"
              f"（未用回授词 {wave['pending_word_count']}）"
              f"｜状态 {wave['status']}" + (f"｜失败 {wave['failure']}" if wave["failure"] else ""))
    if report["lexicon_appends"]:
        for row in report["lexicon_appends"]:
            print(f"迭代补漏·词表回授：{row['term']}（{row['category_label']}）"
                  f"｜词表状态 {row['lexicon_status'] or '未判'}｜{row['status']}"
                  + (f"｜边 {row.get('edge_source')}" if row["status"] == "ok" else "")
                  + (f"（{row['note']}）" if row["note"] else ""))
    if report["detected_acronyms"]:
        print(f"迭代补漏·新检出缩写（不进词表缓冲、不作检索式，记盘点表与机制读数"
              f"{'；已截断' if report['detected_acronyms_truncated'] else ''}）："
              f"{'、'.join(item['acronym'] for item in report['detected_acronyms'])}")
    if report["pools"]:
        carried = "、".join(pool["pool_id"] for pool in report["prior_pools"])
        print(f"迭代补漏（并池）：写回{POOL_KIND}池 {len(report['pools']) + len(report['prior_pools'])} 个进 "
              f"step1-pools.json（本轮 {'、'.join(pool['pool_id'] for pool in report['pools'])}"
              f"{'；续存 ' + carried if carried else ''}）")
    else:
        print("迭代补漏（并池）：本轮无追加轮池可并（零入池或未跑通）")
    print(f"迭代补漏·下一步：重跑 Step2 —— uv run python .claude/skills/med-lit-review/scripts/"
          f"step2_dedupe_sort_bucket.py --delivery {report['delivery']}"
          f"（段键含 step1-pools.json sha256，自动重算；快照按 DOI 重叠复用）")


def cmd_fill(args: argparse.Namespace) -> int:
    ctx = ctx_for(args)
    for path, label in ((ctx["sort_path"], "排序报告"), (ctx["pools_path"], "发现清单"),
                        (ctx["entities_path"], "盘点清单")):
        if not path.is_file():
            print(f"迭代补漏错误：{label}缺失（{path}）", file=sys.stderr)
            return 2
    try:
        entities, indication, new_terms, checklist_notes = load_checklist(ctx["entities_path"])
        terminal, terminal_notes, terminal_info, pools_data = load_terminal(ctx)
        prior, prior_waves, prior_records, prior_notes = load_prior(ctx, pools_data)
    except ValueError as err:
        print(f"迭代补漏错误：{err}", file=sys.stderr)
        return 2

    notes = checklist_notes + terminal_notes + prior_notes
    main_providers = [str(p) for p in (pools_data.get("providers") or []) if str(p).strip()]
    main_limit = pools_data.get("limit_per_query")
    ctx["providers"] = ctx["providers"] or tuple(main_providers) or DEFAULT_PROVIDERS
    ctx["limit"] = ctx["limit"] or (main_limit if isinstance(main_limit, int) and main_limit > 0 else DEFAULT_LIMIT)
    if ctx["limit"] > LIMIT_CEILING:
        notes.append(f"迭代补漏：limit {ctx['limit']} 超检索 CLI 契约上限 {LIMIT_CEILING}，按 {LIMIT_CEILING} 夹取")
        ctx["limit"] = LIMIT_CEILING
    if prior["waves"]:
        notes.append(f"迭代补漏：续跑（台账源 {prior['source']}）——先前 {prior['waves']} 波／"
                     f"{prior['query_count']} query／{prior['records']} 题录；本轮自波 {prior['start_wave']} 起，"
                     f"已发检索式不重发")

    # 闭合判定基准＝排序终池 ∪ 已入池追加轮题录（先前波与本轮波同视；同 DOI 只计一次）
    terminal_dois = {rec["doi"] for rec in terminal}
    for rec in prior_records:
        doi = norm_doi(rec.get("doi"))
        if doi and doi not in terminal_dois:
            terminal_dois.add(doi)
            terminal.append({"doi": doi, "titles": [rec["title"]] if rec.get("title") else [],
                             "abstracts": [rec["abstract"]] if rec.get("abstract") else [],
                             "types": [rec["type"]] if rec.get("type") else []})

    history: dict[str, dict] = {}
    for row in prior["queries"]:
        if row.get("entity"):
            entry = history.setdefault(row["entity"], {"queries": [], "waves": []})
            entry["queries"].append(row["query"])
            if isinstance(row.get("wave"), int) and row["wave"] not in entry["waves"]:
                entry["waves"].append(row["wave"])

    states = init_states(entities)
    for state in states:
        if state["name"] in history:
            state["queries"] = list(history[state["name"]]["queries"])
            state["waves"] = sorted(history[state["name"]]["waves"])
    searched = set(history)
    evaluate(entities, states, terminal, 0)
    baseline_gaps = [s for s in states if s["state"] == STATE_UNCLOSED]

    base_keys = pool_record_keys([pool for pool in pools_data["pools"] if not is_gap_pool(pool)])
    all_records = list(prior_records)
    waves, pools = [], []
    acronyms, seen_acronyms, acronyms_truncated = [], set(), False
    checked_names = {e["name"].casefold() for e in entities}
    stop_reason, used_words = None, set()

    for wave in range(prior["start_wave"], MAX_WAVES + 1):
        issued = prior["issued"] | {item["query"] for row in waves for item in row["queries"]}
        plan = plan_wave(wave, entities, states, indication, new_terms, issued)
        if not plan:
            stop_reason = stop_reason or ("闭合" if all(s["state"] == STATE_IN_POOL for s in states)
                                          else f"波{wave}无可用检索式（判停）")
            break
        records, row = run_search(plan, ctx, wave)
        for item in plan:
            if item["entity"]:
                state = next(s for s in states if s["name"] == item["entity"])
                state["queries"].append(item["query"])
                if wave not in state["waves"]:
                    state["waves"].append(wave)
                searched.add(item["entity"])
        wave_keys = {key for key in (record_key(rec) for rec in records) if key}
        prior_keys = {key for key in (record_key(rec) for rec in all_records) if key}
        row["new_keys"] = len(wave_keys - base_keys - prior_keys)
        row["detected_acronyms"], row["detected_acronyms_truncated"] = detect_acronyms(
            records, checked_names, seen_acronyms)
        acronyms_truncated = acronyms_truncated or row["detected_acronyms_truncated"]
        acronyms.extend(row["detected_acronyms"])
        seen_acronyms.update(item["acronym"].casefold() for item in row["detected_acronyms"])
        row["new_words"] = sorted({word for item in plan
                                   for word in ([item["term"]] if item.get("term") else [])
                                   + list(item.get("refine") or [])})
        row["new_entity_count"] = len(row["detected_acronyms"])
        row["new_word_count"] = len(row["new_words"])
        used_words.update(row["new_words"])
        row["pending_word_count"] = len({term["term"] for term in new_terms} - used_words)
        all_records.extend(records)
        waves.append(row)

        if records and ctx["pool"] and row["status"] != "failed":
            pools.append(pool_for(wave, plan, records, ctx))

        for rec in records:
            doi = norm_doi(rec.get("doi"))
            if doi and doi not in terminal_dois:
                terminal_dois.add(doi)
                terminal.append({"doi": doi, "titles": [rec["title"]] if rec.get("title") else [],
                                 "abstracts": [rec["abstract"]] if rec.get("abstract") else [],
                                 "types": [rec["type"]] if rec.get("type") else []})
        evaluate(entities, states, terminal, wave)

        if row["status"] == "failed":
            stop_reason = f"波{wave}检索失败（中止）"
            for item in plan:
                if item["entity"]:
                    state = next(s for s in states if s["name"] == item["entity"])
                    if state["state"] == STATE_UNCLOSED:
                        state["reason"] = f"检索未完成（波{wave} 未跑通）"
            break
        if all(s["state"] == STATE_IN_POOL for s in states):
            stop_reason = "闭合"
            break
        if wave < MAX_WAVES and row["new_entity_count"] == 0 and row["new_word_count"] == 0 \
                and row["pending_word_count"] == 0:
            stop_reason = f"波{wave}无新增实体／词（判停）"
            break

    if stop_reason is None:
        if all(s["state"] == STATE_IN_POOL for s in states):
            stop_reason = "闭合"
        elif waves:
            stop_reason = "达上限（波数封顶仍存未闭合）"
        else:
            stop_reason = "达上限（波数已用满，未再发检索式）"

    # 未搜实体的原因分档：query 预算用尽＝超 8 query 上限；仅波数用满＝波数已用满；其余＝未列入已跑的波次计划
    spent = prior["query_count"] + sum(len(row["queries"]) for row in waves)
    if stop_reason.startswith("达上限") and spent >= WAVE_QUERY_CAP * MAX_WAVES:
        cap_reason = REASON_UNSEARCHED
    elif stop_reason.startswith("达上限"):
        cap_reason = REASON_WAVES_SPENT
    else:
        cap_reason = REASON_NOT_PLANNED
    for state in states:
        if state["state"] == STATE_UNCLOSED and state["name"] not in searched:
            state["reason"] = cap_reason
    lexicon_rows = apply_lexicon(new_terms, ctx)
    carried = [pool for pool in pools_data["pools"] if isinstance(pool, dict) and is_gap_pool(pool)]
    if pools:
        # 先撤同 pool_kind 再追加：先前波池必须一并写回，否则续跑新波会把先前波题录从池里顶掉
        append_pools(ctx["pools_path"], pools_data,
                     sorted(carried + pools, key=lambda pool: wave_of_pool(pool) or 0))
        if carried:
            notes.append(f"迭代补漏：并池时先前波池 {'、'.join(str(pool.get('pool_id')) for pool in carried)} "
                         f"一并续存（随本轮池写回，未被顶替）")

    unique_keys = {key for key in (record_key(rec) for rec in all_records) if key}
    prior_identity = sum(int(row.get("identity_dropped") or 0) for row in prior_waves
                         if not row.get("reconstructed"))
    if prior_waves and any(row.get("reconstructed") for row in prior_waves):
        notes.append("迭代补漏：先前波由池重建，其无标识丢弃数不可复原"
                     "（new_records.identity_dropped 只含可读波）")
    run_waves = len(waves)
    run_query_count = sum(len(wave["queries"]) for wave in waves)
    ledger = [dict(row) for row in prior["queries"]]
    ledger += [{"wave": wave["wave"], "query": item["query"], "entity": item.get("entity"),
                "source": item["source"]} for wave in waves for item in wave["queries"]]
    mechanism = {
        "ran": True,
        "entity_checklist_count": len(entities),
        "gap_entity_count": len(baseline_gaps),
        "waves": prior["waves"] + run_waves,
        "query_count": prior["query_count"] + run_query_count,
        "new_before_dedupe": len(all_records),
        "new_after_dedupe": len(unique_keys),
        "new_entity_count": sum(wave["new_entity_count"] for wave in waves),
        "new_word_count": sum(wave["new_word_count"] for wave in waves),
        "run_waves": run_waves,
        "run_query_count": run_query_count,
        "closure": "闭合" if not any(s["state"] == STATE_UNCLOSED for s in states) else "未闭合",
        "stop_reason": stop_reason,
        "unclosed_entities": [
            {"name": s["name"], "kind": s["kind"],
             "reason": s["reason"] or REASON_UNKNOWN,
             "queries": s["queries"], "waves": s["waves"], "matched": s["matched"],
             "title_hit": s["title_hit"], "primary": s["primary"],
             "secondary_hits": s["secondary_hits"]}
            for s in states if s["state"] == STATE_UNCLOSED],
    }
    report = {
        "generated_at": date.today().isoformat(),
        "delivery": ctx["delivery"].as_posix(),
        "checklist": {"path": ctx["entities_path"].as_posix(), "indication": indication, "entities": len(entities),
                      "kinds": {label: sum(1 for e in entities if KIND_LABEL[e["kind"]] == label)
                                for label in KIND_LABEL.values()},
                      "new_terms": len(new_terms)},
        "terminal_pool": terminal_info,
        "round_params": {"providers": list(ctx["providers"]), "limit": ctx["limit"], "enrich": ctx["enrich"],
                         "wave_query_cap": WAVE_QUERY_CAP, "max_waves": MAX_WAVES,
                         "window": POOL_WINDOW, "search_cli": ctx["search_cli"]},
        "prior_run": {"source": prior["source"], "readings": prior["readings"],
                      "waves": prior["waves"], "query_count": prior["query_count"],
                      "records": prior["records"], "start_wave": prior["start_wave"]},
        "prior_waves": prior_waves,
        "query_ledger": {"count": len(ledger), "queries": ledger},
        "baseline": {"gap_entity_count": len(baseline_gaps),
                     "gaps": [{"name": s["name"], "kind": s["kind"], "reason": s["reason"]}
                              for s in baseline_gaps]},
        "entities": [dict(state, kind_label=KIND_LABEL[state["kind"]],
                          aliases=entities[state["index"]]["aliases"]) for state in states],
        "waves": waves,
        "detected_acronyms": acronyms,
        "detected_acronyms_truncated": acronyms_truncated,
        "lexicon_appends": lexicon_rows,
        "new_records": {"before_dedupe": mechanism["new_before_dedupe"],
                        "after_dedupe": mechanism["new_after_dedupe"],
                        "already_in_pools": len(unique_keys & base_keys),
                        "new_to_pools": len(unique_keys - base_keys),
                        "identity_dropped": prior_identity + sum(wave["identity_dropped"]
                                                               for wave in waves)},
        "pools": [{"pool_id": pool["pool_id"], "origin": pool["origin"], "family": pool["family"],
                   "window": pool["window"], "records": len(pool["results"])} for pool in pools],
        "prior_pools": [{"pool_id": pool.get("pool_id"), "origin": pool.get("origin"),
                         "records": len(pool.get("results") or [])}
                        for pool in pools_data["pools"] if isinstance(pool, dict) and is_gap_pool(pool)],
        "mechanism": mechanism,
        "raw_dir": f"{B1_DIR}/{RAW_DIR}",
        "report_path": ctx["report_path"].as_posix(),
        "notes": notes,
    }
    write_atomic(ctx["report_path"], dumps(report))

    for note in notes:
        print(note)
    print_plan_rows(report)
    print_entities(report)
    print_gaps(report)
    print_summary(report)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="迭代补漏执行器：终池实体盘点、两波定向补检索与并池（票 05）")
    parser.add_argument("--delivery", required=True, help="交付目录（workspace/YYYY-MM-DD-<slug>）")
    parser.add_argument("--search-cli", default=DEFAULT_SEARCH_CLI,
                        help="检索 CLI 命令（默认 jadense 的 node dist/bin/scholar-search.mjs；探针可换桩，"
                             "Windows 路径用正斜杠）")
    parser.add_argument("--limit", type=int, default=None,
                        help=f"每 query 取数上限（默认随主轮：step1-pools.json 的 limit_per_query，缺省 {DEFAULT_LIMIT}）")
    parser.add_argument("--provider", action="append",
                        help="来源（可重复；默认随主轮：step1-pools.json 的 providers）")
    parser.add_argument("--no-enrich", dest="enrich", action="store_false", help="不带 --enrich（默认带，随主轮口径）")
    parser.add_argument("--no-lexicon", dest="lexicon", action="store_false",
                        help="不落词表候选队列（探针用；默认当场 --append）")
    parser.add_argument("--lexicon-buffer", default=None,
                        help="候选队列落点（默认 skill 内 references/lexicon/term-lexicon.candidates.yaml；"
                             "探针／沙箱可指向副本）")
    parser.add_argument("--no-pool", dest="pool", action="store_false", help="只出读数不并池（探针用）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"单波检索超时秒（默认 {DEFAULT_TIMEOUT:g}）")
    parser.add_argument("--sort-report", help=f"排序报告（默认 <交付>/{SORT_NAME}）")
    parser.add_argument("--pools", help=f"发现清单（默认 <交付>/{POOLS_NAME}）")
    parser.add_argument("--entities", help=f"盘点清单（默认 <交付>/{ENTITIES_NAME}）")
    parser.add_argument("--report", help=f"读数落点（默认 <交付>/{REPORT_NAME}）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return cmd_fill(args)
    except (OSError, KeyError, ValueError) as exc:
        print(f"迭代补漏错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
