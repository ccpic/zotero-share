# -*- coding: utf-8 -*-
"""已知必中抽查核出器：fixture 场景的发现层核出／取回，与 SR／指南纳入清单对交付池的对账（ADR-0023 抽查双轨）。

When: 机制变更时与验收时跑轨 1（ADR-0023 抽查轨 1：fixture **不进**固定选题验收跑的选题集）；轨 2 在该交付
      取到 SR／指南全文时跑，未取到记「不适用」、不阻断。
Do: 轨 1（发现层探针＋交付池对账，真网）：
        uv run python .claude/skills/med-lit-review/scripts/coverage_check.py \
            --delivery workspace/YYYY-MM-DD-<slug> [--coverage-dir DIR] [--scenario ID] [--per-page N] \
            [--page-interval F] [--budget N] [--report F] [--openalex-base URL]
    轨 2（SR／指南纳入清单对账，只读交付池、不联网）：
        uv run python .claude/skills/med-lit-review/scripts/coverage_check.py --delivery <交付> --list <清单.json>
读（只读）：场景定义 `evals/coverage/<场景 id>.json`（字段契约见 `evals/acceptance/topic.md`；核出器另读
      `baseline.probes[]` 的探针声明与基线名次）｜交付内 `run/b1/dedupe-report.json`（终池 `final_records`）或
      `run/merged-pool.json`（历史交付形态）｜`run/b1/sort-report.json`（`rows[].key`／`order`）｜
      `agent/download-queue.txt`（研读选取集逐行 DOI＝下载队列＝入库集，ADR-0032；判在池条目的集内外）。
写（交付内 `run/` 忽略位）：`run/b1/coverage-check.json`（门禁读件：`checked_at`｜
      `scenarios[]{id,expect_shape,found[]{id,where,evidence},missing[]{id,reason},pointer?,note?}`，与
      `topic.md` 冻结名一致）｜`run/b1/raw/coverage-<场景 id>.json`（逐探针取数读数的提取面）。

判读（两形两轨，语义不同——不扩类、不替换锚点体系）：
- `expect_shape=核出`（经典段／高被引）：条目须被**核对通道检出**（探针在声明查询内命中其身份）**且不在终池**
  ——即列入缺席清单；在池者记「规模确认提示」并带研读选取集位次（选取集外＝未选取，ADR-0032）。**核出不是
  入池断言**（ADR-0023 接口注意：任何实现把「核出」当「入池」即违该 ADR）。
- `expect_shape=取回`（已知必中回归）：机制集（实体闭合查询／引文扩展）须**取回**该条目（探针命中其
  DOI／OpenAlex／PMID 身份）。
- `found[]`＝「该条目按期望形态被核出／取回」，不是「进池」；`missing[]` 逐条带原因（通道未达／取数失败／
  无探针覆盖）。
漂移容忍（ADR-0023 接口注意）：provider 排序与收录漂移**只记 `drift[]` 行与披露，不判假失败**——探针命中即
`found`，名次或池内位次与基线不符记漂移行；取数失败逐条记「取数失败」，不写成未核出。探针集合规模（可及总数）
随收录增长而变，不作断言。
同一性：冻结场景的命中认定只认身份键（DOI 一级、OpenAlex／PMID 二级），不做词面猜测；交付池对账无 DOI 时走
词元序列整段口径（`gap_fill.contains_phrase`）词面回查，**唯一命中才认定**，多义记 ∅（不归属在池与否）；`核出` 在池件缺失时整条记「核出未达」——「不在终池」半边没有基准即不可核对，
不按未确认写成 `found`（同「未取数不得写成 0」）。
退出码：0 正常（核出缺口如实进读数并须进局限声明，不算脚本失败）；2 用法、输入件或形态错误。
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.parse
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gap_fill import contains_phrase  # noqa: E402 - 词元序列整段口径复用（唯一命中才认定，不复制第二份）
from step3_queue import norm_doi, read_json, read_queue  # noqa: E402 - DOI 归一／容错读 JSON／队列读法同口径
from citation_expand import (  # noqa: E402 - 取数层复用：预算／节流／重试与响应形态同口径，不复制第二份
    Budget,
    BudgetExhausted,
    HttpClient,
    Throttle,
    dumps,
    last_segment,
    record_key,
    write_atomic,
)

B1_DIR = "run/b1"
REPORT_REL = f"{B1_DIR}/coverage-check.json"
DEDUPE_REL = f"{B1_DIR}/dedupe-report.json"
SORT_REL = f"{B1_DIR}/sort-report.json"
LEGACY_POOL_REL = "run/merged-pool.json"
QUEUE_REL = "agent/download-queue.txt"
RAW_DIR = f"{B1_DIR}/raw"
DEFAULT_COVERAGE_DIR = Path(__file__).resolve().parent.parent / "evals" / "coverage"
SCENARIO_FIELDS = ("topic", "kind", "expect_shape", "applies_to", "known_hits", "baseline")
KINDS = ("known-hit", "classic", "high-cited")   # known-hit → A-12；classic／high-cited → A-13（门禁同口径）
EXPECT_SHAPES = ("核出", "取回")
SHAPE_RETRIEVE = "取回"
WHERE_ABSENT = "缺席清单"
WHERE_SCALE = "规模确认提示"
WHERE_RETRIEVED = "发现层检索"
PROBE_PROVIDER = "openalex"  # 探针取数层只实现 OpenAlex：声明别的源会被静默按 OpenAlex 取数，故校验拒绝
DEFAULT_PER_PAGE = 100    # 与 08 资产 §9.3 的取数口径一致（per-page 100）
PAGE_CEILING = 200        # OpenAlex `per-page` 契约上限
DEFAULT_BUDGET = 30       # 整轮请求预算含重试（同高被引核对器：深度无固定上限，预算即夹取）
DEFAULT_OPENALEX_BASE = "https://api.openalex.org"
SELECT_WORK = "id,ids,doi,display_name,publication_year,cited_by_count"


# ---------------------------------------------------------------- 场景定义


def scenario_slug(text: str) -> str:
    """场景／查询原文的文件名 slug（非身份键，只求可读与唯一）。"""
    slug = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "-", str(text or "").lower()).strip("-")
    return slug[:60] or "scene"


def load_scenarios(root: Path) -> tuple[list[dict], list[str]]:
    """读 `*.json` 场景定义并校验形态（字段契约同 `run_gate.coverage_scenarios`，两处同报不互斥）。

    目录不在场＝无场景集（该交付不适用，不阻断）；不合契约的定位信息逐条给（供对齐契约）。
    """
    if not root.is_dir():
        return [], []
    scenarios, problems = [], []
    for path in sorted(root.glob("*.json")):
        data = read_json(path)
        if data is None:
            problems.append(f"{path.name} 不可解析")
            continue
        if not isinstance(data, dict):
            problems.append(f"{path.name} 不是对象")
            continue
        if data.get("id") != path.stem:
            problems.append(f"{path.name} 的 id 与文件名不符（{data.get('id')}）")
            continue
        miss = [field for field in SCENARIO_FIELDS if field not in data]
        if miss:
            problems.append(f"{path.name} 缺字段 {'、'.join(miss)}")
            continue
        applies = data["applies_to"]
        if (not isinstance(applies, list) or not applies
                or any(not str(token).strip() for token in applies)):
            problems.append(f"{path.name} 的 applies_to 形态不符（需非空 token 表）")
            continue
        hits = data["known_hits"]
        if (not isinstance(hits, list) or not hits
                or any(not isinstance(hit, dict) or not hit.get("id") for hit in hits)):
            problems.append(f"{path.name} 的 known_hits 形态不符（需非空且逐条带 id）")
            continue
        if data["kind"] not in KINDS:
            problems.append(f"{path.name} 的 kind 不在契约三值（{'／'.join(KINDS)}）：{data['kind']}"
                            f"（kind 决定进 A-12 还是 A-13；写错会让核出器判绿而门禁判「定义不合契约」）")
            continue
        bad = probe_problems(path.name, data)
        if bad:
            problems.extend(bad)
            continue
        scenarios.append(data)
    return scenarios, problems


def probe_problems(name: str, data: dict) -> list[str]:
    """探针声明与期望形态的形态校验：核出器靠 `baseline.probes[]` 取数，缺它则该场景不可判。"""
    problems: list[str] = []
    if data.get("expect_shape") not in EXPECT_SHAPES:
        problems.append(f"{name} 的 expect_shape 不在契约两值（{'／'.join(EXPECT_SHAPES)}）："
                        f"{data.get('expect_shape')}")
    baseline = data.get("baseline")
    probes = baseline.get("probes") if isinstance(baseline, dict) else None
    if not isinstance(probes, list) or not probes:
        problems.append(f"{name} 的 baseline.probes 形态不符（需非空探针表：query＋covers）")
        return problems
    covered: set[str] = set()
    for index, probe in enumerate(probes, start=1):
        if not isinstance(probe, dict) or not str(probe.get("query") or "").strip():
            problems.append(f"{name} 探针第 {index} 条缺 query")
            continue
        provider = str(probe.get("provider") or PROBE_PROVIDER).strip().lower()
        if provider != PROBE_PROVIDER:
            problems.append(f"{name} 探针第 {index} 条的 provider 不在支持值（{PROBE_PROVIDER}）：{provider}"
                            f"（取数层只实现 OpenAlex，声明的别的源会被静默按 OpenAlex 取数）")
            continue
        covers = probe.get("covers")
        if (not isinstance(covers, list) or not covers
                or any(not isinstance(row, dict) or not row.get("id") for row in covers)):
            problems.append(f"{name} 探针第 {index} 条的 covers 形态不符（需非空且逐条带 id）")
            continue
        covered.update(str(row["id"]) for row in covers)
    want = {str(hit["id"]) for hit in data["known_hits"]}
    if want - covered:
        problems.append(f"{name} 已知必中条目未被任何探针覆盖：{'、'.join(sorted(want - covered))}"
                        f"（漏声明会让该条目恒判未达）")
    return problems


def scenario_applies(scenario: dict, delivery_name: str) -> bool:
    """场景是否适用本交付：`applies_to` 任一 token 命中**交付目录名**（ADR-0023：token 写明确交付目录名，
    泛化主题词会误伤固定选题包；副本与基线同名时同样命中，改名副本用 `--scenario` 显式指定）。"""
    hay = delivery_name.lower()
    return any(str(token).strip().lower() in hay for token in scenario["applies_to"])


# ---------------------------------------------------------------- 交付池与研读选取集


def load_pool(delivery: Path) -> dict:
    """交付池（对账基准）：终池 `final_records` 优先、历史交付 `merged-pool.json` 兜底；两件皆无＝池 ∅。

    返回身份集（DOI 一级）与题名表（词面回查用）；`count` 为池条目数，缺件记 null 并带原因（不写成 0）。
    """
    tried = []
    for rel in (DEDUPE_REL, LEGACY_POOL_REL):
        path = delivery / rel
        if not path.is_file():
            tried.append(rel)
            continue
        data = read_json(path)
        if data is None:
            return pool_none(f"{rel} 不可解析（形态或编码错）")
        records = data.get("final_records") if isinstance(data, dict) else data
        if not isinstance(records, list):
            return pool_none(f"{rel} 形态不符（需 `final_records` 列表或记录列表）", path)
        dois, titles = set(), []
        for record in records:
            if not isinstance(record, dict):
                continue
            doi = norm_doi(record.get("doi"))
            if doi:
                dois.add(doi)
            if str(record.get("title") or "").strip():
                titles.append({"title": str(record["title"]), "year": record.get("year")})
        return {"path": path.as_posix(), "count": len(records), "dois": dois, "titles": titles,
                "reason": None}
    return pool_none(f"交付内无池件（{'、'.join(tried)} 皆缺）：在池与否记 ∅")


def pool_none(reason: str, path: Path | None = None) -> dict:
    return {"path": path.as_posix() if path else None, "count": None, "dois": set(), "titles": [],
            "reason": reason}


def load_orders(delivery: Path) -> tuple[dict, dict, set[str] | None]:
    """排序位次与研读选取集：`sort-report.json` 的 `rows[].key`→`order`；研读选取集＝`agent/download-queue.txt`
    逐行 DOI（ADR-0032：＝下载队列＝入库集；读法复用 `step3_queue.read_queue`，与 Step3 同口径）。

    集内外＝**集合成员判据**（该 DOI 是否在选取集里），不按位次截线判——必取层条目可在 S 序截线之外；
    缺件记 null（集内外记 ∅，未取数不得写成「集外」）。返回（位次表、选取集读数、选取集 DOI 集）。"""
    orders: dict[str, int] = {}
    sort_path = delivery / SORT_REL
    data = read_json(sort_path) if sort_path.is_file() else None
    rows = data.get("rows") if isinstance(data, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("order"), int) and row.get("key"):
            orders.setdefault(str(row["key"]), row["order"])
    queue_path = delivery / QUEUE_REL
    dois = read_queue(queue_path) if queue_path.is_file() else None
    reading = {"path": queue_path.as_posix() if dois is not None else None,
               "size": len(dois) if dois is not None else None,
               "reason": None if dois is not None else "研读选取集未定稿（缺 agent/download-queue.txt）"}
    return orders, reading, (set(dois) if dois is not None else None)


# ---------------------------------------------------------------- 取数


def works_url(base: str, query: str, per_page: int, sort: str, cursor: str) -> str:
    return (f"{base}/works?search={urllib.parse.quote(query)}&sort={urllib.parse.quote(sort)}"
            f"&per-page={per_page}&select={SELECT_WORK}&cursor={urllib.parse.quote(cursor)}")


def probe_row(item: dict, rank: int) -> dict:
    ids = item.get("ids") if isinstance(item.get("ids"), dict) else {}
    return {"rank": rank, "doi": norm_doi(item.get("doi")), "openalex": last_segment(item.get("id")),
            "pmid": last_segment(ids.get("pmid")), "year": item.get("publication_year"),
            "cited_by": item.get("cited_by_count"), "title": item.get("display_name") or item.get("title")}


def fetch_probe(probe: dict, client: HttpClient, ctx: dict) -> dict:
    """按探针声明逐页取数：深度＝`depth`（缺省＝per_page），预算耗尽／取数失败只落该探针失败态，不冒泡。"""
    query = str(probe["query"])
    per_page = int(probe.get("per_page") or ctx["per_page"])
    per_page = max(1, min(per_page, PAGE_CEILING))
    depth = int(probe.get("depth") or per_page)
    sort = str(probe.get("sort") or "cited_by_count:desc")
    rows: list[dict] = []
    cursor, requests, failure, available = "*", 0, None, None
    for _ in range(max(1, -(-depth // per_page))):
        if ctx["page_interval"] > 0 and requests:
            time.sleep(ctx["page_interval"])
        try:
            entry = client.get_json("openalex", works_url(ctx["openalex_base"], query, per_page, sort, cursor))
        except BudgetExhausted:
            failure = {"class": "budget_exhausted", "message": "请求预算耗尽"}
            break
        requests += int(entry.get("attempts") or 0)
        if entry.get("error"):
            failure = {"class": entry["error"]["class"], "message": entry["error"]["message"]}
            break
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        if isinstance(meta.get("count"), int):
            available = meta["count"]
        results = [item for item in (payload.get("results") or []) if isinstance(item, dict)]
        for item in results:
            rows.append(probe_row(item, len(rows) + 1))
        cursor = meta.get("next_cursor")
        if not cursor or len(results) < per_page or len(rows) >= depth:
            break
    return {"provider": str(probe.get("provider") or PROBE_PROVIDER).strip().lower(),
            "query": query, "sort": sort, "per_page": per_page, "depth": depth,
            "available": available, "requests": requests, "collected": len(rows),
            "failure": failure, "rows": rows}


def identities(obj: object) -> set[str]:
    """身份键集：DOI 一级、OpenAlex／PMID 二级（只认身份，不做词面猜测）。"""
    if not isinstance(obj, dict):
        return set()
    out: set[str] = set()
    doi = norm_doi(obj.get("doi")) if isinstance(obj.get("doi"), str) else ""
    if doi:
        out.add("doi:" + doi)
    for key, prefix in (("openalex", "openalex:"), ("pmid", "pmid:")):
        value = obj.get(key)
        if value is not None and str(value).strip():
            out.add(prefix + str(value).strip().lower())
    return out


def declared_coverage(scenario: dict) -> dict[str, list[int]]:
    """条目 → 声明覆盖它的探针下标：只在**声明覆盖该条目**的探针内判可达——否则他探针的低名次会
    冒充该条目的可达读数并伪造漂移行（名次读数须与基线声明同源）。"""
    coverage: dict[str, list[int]] = {}
    for index, probe in enumerate(scenario["baseline"]["probes"]):
        for cover in probe["covers"]:
            coverage.setdefault(str(cover["id"]), []).append(index)
    return coverage


def probe_match(readings: list[dict], hit: dict, allowed: list[int]) -> dict | None:
    """该条目的最优探针命中（声明覆盖它的探针内名次最小者）：身份键相交即认定；无身份键不发问。"""
    want = identities(hit)
    if not want:
        return None
    best = None
    for index in allowed:
        reading = readings[index]
        for row in reading["rows"]:
            if identities(row) & want and (best is None or row["rank"] < best["row"]["rank"]):
                best = {"reading": reading, "row": row}
    if best is None:
        return None
    reading, row = best["reading"], best["row"]
    return {"query": reading["query"], "available": reading["available"], "depth": reading["depth"],
            "rank": row["rank"], "cited_by": row["cited_by"], "doi": row["doi"], "openalex": row["openalex"]}


def pool_verdict(item: dict, pool: dict) -> dict:
    """在池与否：DOI 一级；无 DOI 且有词面时按词元序列整段口径回查题名，**唯一命中才认定**。

    池不可得（无池件）＝在池与否记 ∅——**未取数不得写成缺席**（与 `cap_hit: null` 不读作 false 同口径）。
    """
    if pool["count"] is None:
        return {"in_pool": None, "key": None, "via": None}
    doi = norm_doi(item.get("doi")) if isinstance(item.get("doi"), str) else ""
    if doi:
        if doi in pool["dois"]:
            return {"in_pool": True, "key": doi, "via": "DOI"}
        return {"in_pool": False, "key": None, "via": "DOI 不在池"}
    words = [str(word).strip() for word in (item.get("match") or []) if str(word).strip()]
    if not words:
        return {"in_pool": None, "key": None, "via": None}
    hits = [row for row in pool["titles"] if contains_phrase(row["title"], words)]
    if len(hits) == 1:
        key = record_key({"doi": None, "title": hits[0]["title"], "year": hits[0]["year"]})
        return {"in_pool": True, "key": key, "via": f"词面唯一（{str(hits[0]['title'])[:60]}）"}
    if len(hits) > 1:
        return {"in_pool": None, "key": None, "via": f"词面多义（{len(hits)} 条候选，不认定）"}
    return {"in_pool": False, "key": None, "via": "词面不在池"}


def order_reading(verdict: dict, orders: dict, selected: set[str] | None) -> str:
    """在池条目的位次读数：order／研读选取集集内外的三段式（位次不可得或选取集未定稿记 ∅）。

    三值 token＝`研读选取集内`／`研读选取集外`／`∅`：∅ 不得写成「集外」——未取数不是未选取。"""
    order = orders.get(verdict["key"]) if verdict.get("key") else None
    if not isinstance(order, int):
        return "order ∅（位次不可得）"
    if selected is None:
        return f"order {order}（研读选取集未定稿，集内外 ∅）"
    return f"order {order}（研读选取集{'内' if verdict['key'] in selected else '外'}）"


# ---------------------------------------------------------------- 轨 1：场景核出


def judge_hit(shape: str, match: dict | None, verdict: dict, orders: dict, selected: set[str] | None,
              pool: dict) -> tuple[bool, str, str, str | None]:
    """逐条判定：返回 (ok, where, evidence, missing_reason)。

    `核出`＝通道检出（探针命中身份）且不在终池（缺席清单）；在池者记规模确认提示并带研读选取集位次。
    `取回`＝机制集取回（探针命中身份）。取数失败与通道未达分列，不混写（ADR-0023 漂移容忍）。
    """
    if match is None:
        return False, None, None, "核对通道未达：声明探针内身份无命中（机制不承诺全量召回）"
    detected = f"探针「{match['query']}」（集合 {match['available']}，取 {match['depth']} 条）内名次 {match['rank']}"
    cited = f"／被引 {match['cited_by']}" if isinstance(match["cited_by"], int) else ""
    if shape == SHAPE_RETRIEVE:
        pool_reading = "池 ∅（未取数）" if pool["count"] is None else (
            f"交付池 {pool['count']} 条含该身份" if verdict["in_pool"] is True
            else f"交付池 {pool['count']} 条内无该身份（＝基线漏检，回归点在此）")
        return True, WHERE_RETRIEVED, f"机制集取回：{detected}{cited}（DOI {match['doi']}）；{pool_reading}", None
    if pool["count"] is None:
        # 核出的「不在终池」半边没有基准即不可核对：不得按未确认写成 found（同「未取数不得写成 0」）
        return False, None, None, (f"核出未达：池不可得（{pool['reason']}）——「不在终池／入缺席清单」半边无法核对"
                                  f"（通道已检出：{detected}{cited}）")
    if verdict["in_pool"] is False:
        where, tail = WHERE_ABSENT, f"终池 {pool['count']} 条内无该身份（不在池＝入缺席清单）"
    elif verdict["in_pool"] is True:
        where, tail = WHERE_SCALE, f"在池（{order_reading(verdict, orders, selected)}）"
    else:
        where, tail = WHERE_SCALE, "在池与否 ∅（无 DOI 且词面多义，不猜）"
    return True, where, f"核对通道检出：{detected}{cited}；{tail}", None


def drift_rows(scenario: dict, matches: dict, verdicts: dict, orders: dict, queue: dict) -> list[dict]:
    """漂移行：基线名次与本次不符、或基线声明可达而本次未达（provider 排序／收录漂移只记行，不判失败）。"""
    rows: list[dict] = []
    baseline_pool = scenario.get("baseline", {}).get("pool") if isinstance(scenario.get("baseline"), dict) else None
    raw_in_pool = (baseline_pool or {}).get("in_pool", [])
    want_in_pool = [str(row.get("id") if isinstance(row, dict) else row) for row in raw_in_pool]
    for probe in scenario["baseline"]["probes"]:
        for cover in probe["covers"]:
            hit_id = str(cover["id"])
            match = matches.get(hit_id)
            base_rank = cover.get("rank")
            if match is None:
                rows.append({"id": hit_id, "kind": "名次", "baseline": f"名次 {base_rank}",
                             "observed": "未达", "note": f"基线查询「{probe['query']}」可达，本次未命中"
                                                          f"（排序／收录漂移，只记行不判假失败）"})
            elif isinstance(base_rank, int) and base_rank != match["rank"]:
                rows.append({"id": hit_id, "kind": "名次", "baseline": f"名次 {base_rank}",
                             "observed": f"名次 {match['rank']}",
                             "note": f"「{probe['query']}」名次漂移（集合规模随收录变化，不断言）"})
    if want_in_pool:
        for hit_id in want_in_pool:
            if verdicts.get(hit_id, {}).get("in_pool") is not True:
                rows.append({"id": hit_id, "kind": "在池", "baseline": "在池",
                             "observed": f"在池与否 {verdicts.get(hit_id, {}).get('in_pool')}",
                             "note": "交付池与基线不符（重跑或池变更），只记行不判假失败"})
    return rows


def build_scenario_row(scenario: dict, pool: dict, orders: dict, queue: dict, selected: set[str] | None,
                       ctx: dict) -> dict:
    """一个场景一行：跑探针 → 逐条判定 → 组装门禁读件行与漂移行。"""
    readings = [fetch_probe(probe, ctx["client"], ctx) for probe in scenario["baseline"]["probes"]]
    coverage = declared_coverage(scenario)
    matches = {str(hit["id"]): probe_match(readings, hit, coverage[str(hit["id"])])
               for hit in scenario["known_hits"]}
    verdicts = {str(hit["id"]): pool_verdict(hit, pool) for hit in scenario["known_hits"]}
    found, missing = [], []
    for hit in scenario["known_hits"]:
        hit_id = str(hit["id"])
        ok, where, evidence, reason = judge_hit(scenario["expect_shape"], matches[hit_id],
                                               verdicts[hit_id], orders, selected, pool)
        if ok:
            found.append({"id": hit_id, "where": where, "evidence": evidence})
        else:
            failures = [reading["failure"] for reading in readings if reading["failure"]]
            if failures:   # 取数失败与通道未达分列，不混写（ADR-0023 漂移容忍）
                reason += (f"；探针取数失败 {len(failures)}／{len(readings)} 组"
                           f"（{failures[0]['class']}：{failures[0]['message']}）")
            missing.append({"id": hit_id, "reason": reason})
    raw_rel = f"{RAW_DIR}/coverage-{scenario_slug(scenario['id'])}.json"
    write_raw(ctx["delivery"] / raw_rel, scenario, readings, pool, queue)
    shape_note = {"核出": "核出＝通道检出并列入缺席清单／规模确认提示，非入池断言",
                  "取回": "取回＝机制集取回该条目"}[scenario["expect_shape"]]
    return {
        "id": scenario["id"],
        "expect_shape": scenario["expect_shape"],
        "found": found,
        "missing": missing,
        "pointer": raw_rel,
        "note": f"{shape_note}｜基线交付 {scenario.get('baseline_delivery')}｜探针 {len(readings)} 组"
                f"｜池 {pool['path'] or '∅'}（{pool['count'] if pool['count'] is not None else '∅'} 条）",
        "drift": drift_rows(scenario, matches, verdicts, orders, queue),
        "probes": [{key: reading[key] for key in
                    ("provider", "query", "sort", "per_page", "depth", "available", "requests",
                     "collected", "failure")}
                   for reading in readings],
        "pool": {"path": pool["path"], "count": pool["count"], "reason": pool["reason"]},
    }


def write_raw(path: Path, scenario: dict, readings: list[dict], pool: dict, queue: dict) -> None:
    """原始读数面：逐探针取数读数与命中行（可复核取数节律／集合规模／名次），不含整包响应。"""
    payload = {
        "checked_at": date.today().isoformat(),
        "scenario": scenario["id"],
        "expect_shape": scenario["expect_shape"],
        "baseline_delivery": scenario.get("baseline_delivery"),
        "pool": {"path": pool["path"], "count": pool["count"], "reason": pool["reason"]},
        "queue": queue,
        "probes": readings,
    }
    write_atomic(path, dumps(payload))


def cmd_fixtures(args: argparse.Namespace) -> int:
    delivery = Path(args.delivery)
    if not delivery.is_dir():
        print(f"已知必中抽查错误：交付目录不在场（{delivery}）", file=sys.stderr)
        return 2
    scenarios, problems = load_scenarios(ctx_coverage_dir(args))
    if problems:
        for problem in problems:
            print(f"已知必中抽查错误：场景定义不合契约——{problem}", file=sys.stderr)
        return 2
    picked = [scenario for scenario in scenarios
              if scenario["id"] == args.scenario or (args.scenario is None
                                                     and scenario_applies(scenario, delivery.name))]
    if not picked:
        if args.scenario:   # 显式点名＝用法面：id 写错退 2（「不适用」只对未点名且不适用成立）
            present = "、".join(scenario["id"] for scenario in scenarios) or "无"
            print(f"已知必中抽查错误：无 id 为 {args.scenario} 的场景（id 须与 "
                  f"{ctx_coverage_dir(args).as_posix()} 下文件名一致；在场：{present}）", file=sys.stderr)
            return 2
        print(f"已知必中抽查：无场景适用交付 {delivery.name}（applies_to 需写明交付目录名）"
              f"（不阻断；记「不适用」）")
        return 0
    pool = load_pool(delivery)
    orders, queue, selected = load_orders(delivery)
    ctx = {"delivery": delivery, "client": HttpClient(Budget(args.budget), Throttle()),
           "per_page": args.per_page, "page_interval": args.page_interval,
           "openalex_base": args.openalex_base.rstrip("/")}
    rows = [build_scenario_row(scenario, pool, orders, queue, selected, ctx) for scenario in picked]
    report = {"checked_at": date.today().isoformat(), "delivery": delivery.as_posix(),
              "coverage_dir": str(ctx_coverage_dir(args)), "scenarios": rows,
              "budget": {"limit": args.budget, "used": ctx["client"].budget.used}}
    report_path = Path(args.report) if args.report else delivery / REPORT_REL
    write_atomic(report_path, dumps(report))
    for row in rows:
        print_scenario_row(row)
    print(f"已知必中抽查：场景 {len(rows)} 个｜请求 {report['budget']['used']}／{args.budget}"
          f"｜读数 {report_path.as_posix()}（核出缺口须进局限声明）")
    return 0


def print_scenario_row(row: dict) -> None:
    print(f"已知必中抽查·{row['id']}（形态 {row['expect_shape']}）：核出／取回 {len(row['found'])}"
          f"／未达 {len(row['missing'])}")
    for entry in row["found"]:
        print(f"  ✓ {entry['id']}｜{entry['where']}｜{entry['evidence']}")
    for entry in row["missing"]:
        print(f"  ✗ {entry['id']}｜{entry['reason']}")
    for probe in row["probes"]:
        failure = probe["failure"] or {}
        state = f"失败（{failure.get('class')}）" if probe["failure"] else f"集合 {probe['available']}"
        print(f"  探针（{probe['provider']}）「{probe['query']}」｜{probe['sort']}｜{state}｜取 {probe['collected']}"
              f"／深度 {probe['depth']}｜请求 {probe['requests']}")
    for drift in row["drift"]:
        print(f"  漂移·{drift['kind']}：{drift['id']}｜基线 {drift['baseline']} → 本次 {drift['observed']}"
              f"｜{drift['note']}")


# ---------------------------------------------------------------- 轨 2：纳入清单对账


def load_list(path: Path) -> tuple[dict, list[str]]:
    """纳入清单读取：`{"source": …, "items":[{id, doi?, match?, year?}]}`；逐条需 id＋（doi 或 match）。"""
    data = read_json(path)
    if not isinstance(data, dict):
        return {}, [f"清单不可解析或不是对象：{path}"]
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return {}, [f"清单 items 形态不符（需非空列表）：{path}"]
    problems = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict) or not str(item.get("id") or "").strip():
            problems.append(f"清单第 {index} 条缺 id")
            continue
        doi = norm_doi(item.get("doi")) if isinstance(item.get("doi"), str) else ""
        words = [str(word).strip() for word in (item.get("match") or []) if str(word).strip()]
        if not doi and not words:
            problems.append(f"清单第 {index} 条（{item['id']}）doi 与 match 皆缺（须至少一种命中途径）")
    return {"source": str(data.get("source") or path.name), "items": items}, problems


def cmd_list(args: argparse.Namespace) -> int:
    delivery = Path(args.delivery)
    if not delivery.is_dir():
        print(f"纳入清单对账错误：交付目录不在场（{delivery}）", file=sys.stderr)
        return 2
    listing, problems = load_list(Path(args.list))
    if problems:
        for problem in problems:
            print(f"纳入清单对账错误：{problem}", file=sys.stderr)
        return 2
    pool = load_pool(delivery)
    if pool["count"] is None:
        print(f"纳入清单对账错误：交付池不可得（{pool['reason']}）——对账无基准", file=sys.stderr)
        return 2
    orders, queue, selected = load_orders(delivery)
    rows, absent, unknown = [], [], []
    for item in listing["items"]:
        hit_id = str(item["id"])
        verdict = pool_verdict(item, pool)
        order = orders.get(verdict["key"]) if verdict.get("key") else None
        if verdict["in_pool"] is True:
            take, basis = "在池", f"{verdict['via']}｜{order_reading(verdict, orders, selected)}"
        elif verdict["in_pool"] is False:
            take, basis = "缺席", f"{verdict['via']}（终池 {pool['count']} 条内无该身份／词面）"
            absent.append(hit_id)
        else:
            take, basis = "∅", "在池与否不可认定（无 DOI 且未给词面或词面多义）"
            unknown.append(hit_id)
        rows.append({"id": hit_id, "take": take, "basis": basis,
                     "order": order if isinstance(order, int) else None})
    print(f"纳入清单对账（{listing['source']}）｜交付 {delivery.name}｜池 {pool['path']}（{pool['count']} 条）"
          f"｜条目 {len(rows)}｜在池 {len(rows) - len(absent) - len(unknown)}｜缺席 {len(absent)}"
          f"｜∅ {len(unknown)}")
    print("| 清单条目 | 能否经本交付机制取回 | 依据（在池 origin／命中途径／研读选取集位次） | 缺口记因 |")
    print("|---|---|---|---|")
    for row in rows:
        print(f"| {row['id']} | {row['take']} | {row['basis']} | |")
    if absent:
        print(f"缺席逐条（缺口记因由 agent 判并接 §8 句式）：{'、'.join(absent)}")
    if unknown:
        print(f"在池与否 ∅ 逐条：{'、'.join(unknown)}（补 DOI 或更具体词面后重跑）")
    return 0


# ---------------------------------------------------------------- 命令行


def ctx_coverage_dir(args: argparse.Namespace) -> Path:
    return Path(args.coverage_dir) if args.coverage_dir else DEFAULT_COVERAGE_DIR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="已知必中抽查核出器：fixture 场景核出／取回＋纳入清单对账")
    parser.add_argument("--delivery", required=True, help="交付目录（workspace/YYYY-MM-DD-<slug>）")
    parser.add_argument("--coverage-dir", help=f"场景定义目录（默认 skill 内 {DEFAULT_COVERAGE_DIR.name}／"
                                              f"evals/coverage）")
    parser.add_argument("--scenario", help="只跑该 id 的场景（副本改名时用它显式指定，越过 applies_to）")
    parser.add_argument("--list", help="SR／指南纳入清单（轨 2 对账；给此项即只做对账、不联网）")
    parser.add_argument("--per-page", type=int, default=DEFAULT_PER_PAGE,
                        help=f"探针缺省每页条数（默认 {DEFAULT_PER_PAGE}，上限 {PAGE_CEILING}）")
    parser.add_argument("--page-interval", type=float, default=0.0,
                         help="逐页间隔秒（默认 0；匿名配额下按 08 资产 §3 的节律给 1–2 防 429）")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                        help=f"整轮请求预算含重试（默认 {DEFAULT_BUDGET}）")
    parser.add_argument("--report", help=f"读数落点（默认 <交付>/{REPORT_REL}）")
    parser.add_argument("--openalex-base", default=DEFAULT_OPENALEX_BASE, help="OpenAlex 基址（探针／代理用）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.per_page <= PAGE_CEILING:
        print(f"已知必中抽查错误：--per-page 应在 1..{PAGE_CEILING}", file=sys.stderr)
        return 2
    if args.budget < 1:
        print("已知必中抽查错误：--budget 应 ≥ 1", file=sys.stderr)
        return 2
    try:
        return cmd_list(args) if args.list else cmd_fixtures(args)
    except (OSError, KeyError, ValueError) as exc:
        print(f"已知必中抽查错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
