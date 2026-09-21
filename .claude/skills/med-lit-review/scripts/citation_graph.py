# -*- coding: utf-8 -*-
"""引文图读法：按票 02 的取数算「被池内引用计数」与文献耦合／共被引（不重复请求）。

When: 引文扩展轮跑完（`run/b1/citation-expansion.json` 与 `raw/citation-*.json` 已在盘上）之后、规模确认
     之前，与高被引缺席核对同批两用（ADR-0019 的发现层机制窗）。
Do: uv run python .claude/skills/med-lit-review/scripts/citation_graph.py --delivery workspace/YYYY-MM-DD-<slug> \
        [--top 20] [--expansion F] [--pools F] [--dedupe F] [--report F]

读（交付内，只读）：`run/b1/citation-expansion.json`（种子清单）、`run/b1/raw/citation-*.json`（票 02 的逐通道
原始响应：种子记录的 `referenced_works` 与后向题录是**唯一**可用的引用表面）、`run/b1/step1-pools.json`
（池记录索引：OpenAlex work id → 题录，供被引条款目取题名；`pool_kind` 分池面）、`run/b1/dedupe-report.json`
（终池 `final_records[].origin`，判定记录是否已进终池）。**只读不写池、不发请求**（请求数记 0）。

写（交付内，`run/b1/` 忽略位）：`citation-graph.json`（读数件；含 `usage` 用途标注）。

口径（与票 02 不重复取数）：
- **被池内引用计数**＝该记录被池内已知引用表引用的次数；已知引用表＝票 02 实取的种子记录（头部 20 条＋
  判定集）的 `referenced_works`——非种子池记录无引用表（票 02 的 `select` 不含 `references`），故读数为
  **下界**，不重复请求补齐；键＝OpenAlex work id（种子引用表给 id，池记录与后向题录给 id→题录）。
- **在池面**：有终池位次（`dedupe-report.final_records` 的 DOI）记「终池」；否则把该记录所属 `pool_kind`
  **全部列出**（`检索池`／`扩展池`／其余池按 `pool_kind` 直标如「追加轮池」），同记录多池并存者不丢面
  （一行 `pool_kinds` 与 `pool_face` 同源，不按「非空且非扩展 ⇒ 检索池」兜底）。扩展池已并池但未重跑
  Step2 时终池不含扩展记录——读数件 `notes` 记明。
- **文献耦合**＝两篇种子共引参考文献的条数（只在有引用表的种子之间可算）。
- **共被引**＝两篇参考文献被同一种子同时引用的次数（共引种子数）。
- **用途标注**（`usage`）：经典识别（在池记录中位处被池内高频被引／与里程碑共被引者）｜影响标注（逐条
  被池内引用计数＋OpenAlex 被引）｜缺席核对证据（高被引缺席核对清单条目可用同批引用表佐证）。
  **本轮不作排序输入**：权重口径冻结在 `sort-weights-v2`，增减维随票 05／07；本读数只作标注与证据。

退出码：0 正常（零读数如实进账）；2 用法、输入文件或形态错误。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from step3_queue import norm_doi, read_json  # noqa: E402 - 同族脚本跨件复用（DOI 归一与容错读 JSON 同口径）
from citation_expand import dumps, last_segment, write_atomic  # noqa: E402 - 取数层同口径（题录字段与原子写）
from gap_fill import POOL_KIND as GAPFILL_POOL_KIND  # noqa: E402 - 池类型词汇同源（追加轮）

B1_DIR = "run/b1"
EXPANSION_NAME = f"{B1_DIR}/citation-expansion.json"
POOLS_NAME = f"{B1_DIR}/step1-pools.json"
DEDUPE_NAME = f"{B1_DIR}/dedupe-report.json"
REPORT_NAME = f"{B1_DIR}/citation-graph.json"
RAW_DIR = f"{B1_DIR}/raw"
EXTENSION_KIND = "引文扩展"
RETRIEVAL_KIND = "检索"
POOL_FACE_LABEL = {EXTENSION_KIND: "扩展池", RETRIEVAL_KIND: "检索池"}
POOL_KIND_ORDER = (RETRIEVAL_KIND, EXTENSION_KIND, GAPFILL_POOL_KIND)   # 展示次序：检索 → 扩展 → 追加轮 → 其他
SEED_RAW = "citation-openalex-seed-{slug}.json"
BWD_RAW = "citation-openalex-bwd-{slug}.json"
TOP_PAIRS = 20


def raw_payload(path: Path, keys: tuple[str, ...]) -> dict | None:
    """raw 原始响应取首请求载荷：缺件／形态不符即 None（读数记 ∅，不猜）。"""
    data = read_json(path)
    requests = data.get("requests") if isinstance(data, dict) else None
    if not isinstance(requests, list) or not requests or not isinstance(requests[0], dict):
        return None
    payload = requests[0].get("payload")
    for key in keys:
        payload = payload.get(key) if isinstance(payload, dict) else None
    return payload if isinstance(payload, dict) else None


def year_of(record: dict) -> int | None:
    """年份：`year` 一级，缺则取 `publishedDate` 前四位（检索池记录只有后者，扩展池与后向题录有前者）。"""
    year = record.get("year")
    if isinstance(year, int):
        return year
    published = str(record.get("publishedDate") or "")
    return int(published[:4]) if published[:4].isdigit() else None


def merge_work(works: dict, work_id: str, row: dict) -> None:
    """同 work id 合并题录：只补缺项、不覆盖已有值（池记录与后向题录各有缺面，互相补）。"""
    existing = works.get(work_id)
    if existing is None:
        works[work_id] = dict(row)
        return
    for key, value in row.items():
        if existing.get(key) in (None, "") and value not in (None, ""):
            existing[key] = value


def work_row(item: dict) -> dict:
    return {"doi": norm_doi(item.get("doi")), "title": item.get("display_name") or item.get("title"),
            "year": year_of({"year": item.get("publication_year"), "publishedDate": item.get("publication_date")}),
            "cited_by_count": item.get("cited_by_count")}


def pool_index(pools_path: Path) -> tuple[dict, dict, dict, list[str]]:
    """池记录索引（只索引不复制读数）：work id → 题录、DOI → 池面、池面计数与池 id。
    入参＝**发现清单文件路径**（`--pools` 可换落点），不再由交付目录拼。"""
    pools_data = read_json(pools_path)
    if not isinstance(pools_data, dict) or not isinstance(pools_data.get("pools"), list):
        return {}, {}, {"present": False}, [f"引文图读法：发现清单缺失或形态不符（{pools_path}），"
                                            f"扩展池规模与池面判定记 ∅"]
    works: dict[str, dict] = {}
    kinds: dict[str, set] = {}
    counts: dict[str, int] = {}
    pool_ids: list[str] = []
    for pool in pools_data["pools"]:
        if not isinstance(pool, dict):
            continue
        kind = str(pool.get("pool_kind") or RETRIEVAL_KIND)
        counts[kind] = counts.get(kind, 0) + 1
        if kind == EXTENSION_KIND:
            pool_ids.append(pool.get("pool_id"))
        for record in (pool.get("results") or []):
            if not isinstance(record, dict):
                continue
            doi = norm_doi(record.get("doi"))
            if doi:
                kinds.setdefault(doi, set()).add(kind)   # 同记录可属多池：全收，不按先到者顶替
            work_id = record.get("externalId") if str(record.get("externalId") or "").startswith("W") else None
            if work_id:
                merge_work(works, work_id, {"doi": doi, "title": record.get("title"),
                                            "year": year_of(record),
                                            "cited_by_count": record.get("citationCount")})
    return works, kinds, {"present": True, "by_kind": counts, "pool_ids": pool_ids}, []


def seed_references(delivery: Path, seeds: list[dict], works: dict) -> tuple[dict, list[str]]:
    """种子引用表：slug → {doi, work_id, refs[]}；题录映射只补索引里缺的（缺件逐条记因）。"""
    refs_by_slug: dict[str, dict] = {}
    notes: list[str] = []
    for seed in seeds:
        slug = seed.get("slug")
        if not slug:
            continue
        payload = raw_payload(delivery / RAW_DIR / SEED_RAW.format(slug=slug), ())
        if payload is None:
            notes.append(f"引文图读法：种子 {seed.get('doi')} 的原始响应缺失或形态不符"
                         f"（{SEED_RAW.format(slug=slug)}），该种子无引用表")
            continue
        work_id = last_segment(payload.get("id")) if payload.get("id") else seed.get("openalex_id")
        # `referenced_works` 给的是完整 URL（https://openalex.org/W…），与池记录 `externalId` 同归一成短 id
        refs = [last_segment(item) for item in (payload.get("referenced_works") or [])
                if isinstance(item, str) and last_segment(item)]
        refs_by_slug[slug] = {"slug": slug, "doi": norm_doi(seed.get("doi")), "work_id": work_id, "refs": refs}
        if work_id:
            merge_work(works, work_id, {"doi": norm_doi(seed.get("doi")), "title": None, "year": None,
                                        "cited_by_count": payload.get("cited_by_count")})
        bwd = raw_payload(delivery / RAW_DIR / BWD_RAW.format(slug=slug), ("results",))
        for item in (bwd or []):
            item_id = last_segment(item.get("id"))
            if item_id:
                merge_work(works, item_id, work_row(item))
    return refs_by_slug, notes


def terminal_origins(dedupe_path: Path) -> tuple[dict, dict, list[str]]:
    """终池：DOI → origin，及 origin 分布；缺件记 ∅（不推断）。入参＝**终池读数件路径**（`--dedupe` 可换落点）。"""
    dedupe = read_json(dedupe_path)
    if not isinstance(dedupe, dict) or not isinstance(dedupe.get("final_records"), list):
        return {}, {"source": dedupe_path.as_posix(), "present": False, "by_origin": {},
                    "final_count": None}, [f"引文图读法：终池读数件缺失或形态不符（{dedupe_path}），"
                                            f"在池判定记 ∅"]
    origins, counts = {}, {}
    for record in dedupe["final_records"]:
        if not isinstance(record, dict):
            continue
        doi = norm_doi(record.get("doi"))
        if not doi:
            continue
        origin = str(record.get("origin") or "discovery")
        origins[doi] = origin
        counts[origin] = counts.get(origin, 0) + 1
    return origins, {"source": dedupe_path.as_posix(), "present": True,
                     "by_origin": counts, "final_count": len(origins)}, []


def kind_order(kinds: set) -> list[str]:
    """池类型展示次序：`POOL_KIND_ORDER` 优先，其余按字典序（同记录多池全列，不顶替）。"""
    known = [kind for kind in POOL_KIND_ORDER if kind in kinds]
    return known + sorted(str(kind) for kind in kinds - set(POOL_KIND_ORDER))


def pool_face_label(origin: str | None, kinds: list[str]) -> str | None:
    """在池面标签：有 origin＝终池；否则全部 `pool_kind` 逐个映射后以 `／` 连接（多池并存全列）。"""
    if origin:
        return "终池"
    if not kinds:
        return None
    return "／".join(POOL_FACE_LABEL.get(kind, f"{kind}池") for kind in kinds)


def in_pool_citations(refs_by_slug: dict, works: dict, origins: dict, kinds: dict) -> tuple[list[dict], dict]:
    """被池内引用计数：逐条款目被多少条已知引用表（种子）引用；在池面按终池与池面索引分别标。"""
    cited: dict[str, list] = {}
    for seed in refs_by_slug.values():
        for work_id in seed["refs"]:
            cited.setdefault(work_id, []).append(seed["doi"])
    rows = []
    for work_id, citing_seeds in cited.items():
        work = works.get(work_id) or {}
        doi = norm_doi(work.get("doi"))
        kind_list = kind_order(kinds.get(doi) or set()) if doi else []
        origin = origins.get(doi) if doi else None
        rows.append({"work_id": work_id, "doi": doi or None, "title": work.get("title"),
                     "year": work.get("year"), "openalex_cited_by_count": work.get("cited_by_count"),
                     "in_pool_citations": len(citing_seeds), "cited_by_seeds": citing_seeds,
                     "pool_face": pool_face_label(origin, kind_list),
                     "origin": origin, "pool_kinds": kind_list})
    rows.sort(key=lambda item: (-item["in_pool_citations"],
                                -(item["openalex_cited_by_count"]
                                  if isinstance(item["openalex_cited_by_count"], int) else -1)))
    readings = {"cited_records": len(rows), "in_terminal": sum(1 for row in rows if row["origin"]),
                "in_extension_pool": sum(1 for row in rows if EXTENSION_KIND in row["pool_kinds"]),
                "in_retrieval_pool": sum(1 for row in rows if RETRIEVAL_KIND in row["pool_kinds"]),
                "in_gapfill_pool": sum(1 for row in rows if GAPFILL_POOL_KIND in row["pool_kinds"]),
                "max_in_pool_citations": rows[0]["in_pool_citations"] if rows else 0,
                "mapped_no_doi": sum(1 for row in rows if not row["doi"])}
    return rows, readings


def coupling(refs_by_slug: dict, top: int) -> tuple[list[dict], dict]:
    """文献耦合：两篇种子共引的参考文献条数（只在有引用表的种子之间可算）。"""
    seeds = sorted(refs_by_slug.values(), key=lambda seed: seed["slug"])
    pairs = []
    for index, left in enumerate(seeds):
        left_refs = set(left["refs"])
        if not left_refs:
            continue
        for right in seeds[index + 1:]:
            shared = left_refs & set(right["refs"])
            if shared:
                pairs.append({"a": left["doi"], "b": right["doi"], "shared": len(shared),
                              "shared_works": sorted(shared)[:10]})
    pairs.sort(key=lambda pair: -pair["shared"])
    with_refs = [seed for seed in seeds if seed["refs"]]
    readings = {"seed_pairs_total": len(pairs), "kept": min(len(pairs), top),
                "seeds_with_refs": len(with_refs), "seeds_without_refs": len(seeds) - len(with_refs)}
    return pairs[:top], readings


def co_citation(refs_by_slug: dict, works: dict, top: int) -> tuple[list[dict], dict]:
    """共被引：两篇参考文献被同一批种子同时引用的次数（＝共引种子数）。"""
    citing: dict[str, set] = {}
    for seed in refs_by_slug.values():
        for work_id in seed["refs"]:
            citing.setdefault(work_id, set()).add(seed["doi"])
    work_ids = sorted(work_id for work_id, seeds in citing.items() if len(seeds) >= 2)
    pairs = []
    for index, left in enumerate(work_ids):
        for right in work_ids[index + 1:]:
            shared = citing[left] & citing[right]
            if not shared:
                continue
            pairs.append({"a": (works.get(left) or {}).get("doi"), "b": (works.get(right) or {}).get("doi"),
                          "a_title": (works.get(left) or {}).get("title"),
                          "b_title": (works.get(right) or {}).get("title"),
                          "co_citations": len(shared), "seeds": sorted(shared)})
    pairs.sort(key=lambda pair: -pair["co_citations"])
    readings = {"co_cited_works": len(work_ids), "pair_total": len(pairs), "kept": min(len(pairs), top)}
    return pairs[:top], readings


def build_report(args: argparse.Namespace) -> tuple[dict, list[str]]:
    delivery = Path(args.delivery)
    expansion_path = Path(args.expansion) if args.expansion else delivery / EXPANSION_NAME
    pools_path = Path(args.pools) if args.pools else delivery / POOLS_NAME
    dedupe_path = Path(args.dedupe) if args.dedupe else delivery / DEDUPE_NAME
    expansion = read_json(expansion_path)
    if not isinstance(expansion, dict) or not isinstance(expansion.get("seeds"), list):
        raise ValueError(f"引文扩展读数件形态不符（需 {{\"seeds\": […]}}）：{expansion_path}")
    notes: list[str] = []
    seeds = [seed for seed in expansion["seeds"] if isinstance(seed, dict)]
    if not seeds:
        notes.append("引文图读法：引文扩展轮 0 种子（引用表为空，读数记 0）")
    works, kinds, pools, pool_notes = pool_index(pools_path)
    notes.extend(pool_notes)
    refs_by_slug, seed_notes = seed_references(delivery, seeds, works)
    notes.extend(seed_notes)
    origins, terminal, terminal_notes = terminal_origins(dedupe_path)
    notes.extend(terminal_notes)
    if pools.get("present") and pools["by_kind"].get(EXTENSION_KIND) and terminal.get("by_origin") \
            and not any(str(origin).startswith("citation_") for origin in terminal["by_origin"]):
        notes.append(f"引文图读法：扩展池已并池（{pools['by_kind'][EXTENSION_KIND]} 池）但终池未重跑 Step2"
                     f"（origin 无 citation_*），在池判定另按扩展池记录集核（pool_kind={EXTENSION_KIND}）")
    citation_rows, citation_readings = in_pool_citations(refs_by_slug, works, origins, kinds)
    coupling_pairs, coupling_readings = coupling(refs_by_slug, args.top)
    co_pairs, co_readings = co_citation(refs_by_slug, works, args.top)
    report = {
        "generated_at": date.today().isoformat(),
        "delivery": delivery.as_posix(),
        "inputs": {"expansion": expansion_path.as_posix(), "expansion_seeds": len(seeds),
                   "seed_raw_present": len(refs_by_slug),
                   "pools": pools_path.as_posix(),
                   "pool_kinds": pools.get("by_kind", {}), "extension_pool_ids": pools.get("pool_ids", []),
                   "dedupe": terminal["source"], "terminal": terminal},
        "base": {"seeds_with_refs": len(refs_by_slug),
                 "refs_total": sum(len(seed["refs"]) for seed in refs_by_slug.values()),
                 "works_mapped": len(works),
                 "note": "引用表只来自票 02 实取的种子记录（头部 20＋判定集）；非种子池记录无引用表"
                         "（票 02 的 select 不含 references），不重复请求 → 被池内引用计数为下界"},
        "in_pool_citations": citation_rows[:args.top],
        "in_pool_citations_readings": citation_readings,
        "coupling": {"pairs": coupling_pairs, "readings": coupling_readings},
        "co_citation": {"pairs": co_pairs, "readings": co_readings},
        "usage": {
            "经典识别": "被池内引用计数高、或与里程碑共被引的池内记录＝经典候选"
                        "（读数件 `in_pool_citations`／`co_citation`）",
            "影响标注": "逐条给出被池内引用计数与 OpenAlex 被引（`openalex_cited_by_count`），作交付内影响标注",
            "缺席核对证据": "高被引缺席核对清单条目可用同批引用表佐证（同批两用，不重复请求）",
            "排序输入": "本轮不作排序输入：权重口径冻结在 sort-weights-v2，增减维随票 05／07 定案",
        },
        "not_refetched": {"requests": 0,
                          "note": "读数只来自票 02 产物（citation-expansion.json 与 raw/citation-*.json），"
                                  "本脚本不发请求、不写池"},
        "report_path": (Path(args.report) if args.report else delivery / REPORT_NAME).as_posix(),
        "notes": notes,
    }
    if len(citation_rows) > args.top:
        notes.append(f"引文图读法：被池内引用计数 {len(citation_rows)} 条，读数件留前 {args.top} 条"
                     f"（读数 `in_pool_citations_readings` 记全量计数）")
    return report, notes


def print_report(report: dict) -> None:
    base = report["base"]
    citation = report["in_pool_citations_readings"]
    print(f"引文图读法（不重复取数）：已知引用表 种子 {base['seeds_with_refs']} 条／参考文献 {base['refs_total']} 条"
          f"｜被池内引用计数 {citation['cited_records']} 条（终池 {citation['in_terminal']}／扩展池 "
          f"{citation['in_extension_pool']}／检索池 {citation['in_retrieval_pool']}；最高 "
          f"{citation['max_in_pool_citations']}）")
    for row in report["in_pool_citations"][:5]:
        print(f"引文图读法·被池内高频被引：{row.get('title') or row['work_id']}"
              f"（{row.get('year') if row.get('year') is not None else '∅'}；OpenAlex 被引 "
              f"{row.get('openalex_cited_by_count') if row.get('openalex_cited_by_count') is not None else '∅'}）"
              f"｜被池内引用 {row['in_pool_citations']} 次（池面 {row['pool_face'] or '∅'}）")
    coupling_readings = report["coupling"]["readings"]
    print(f"引文图读法·文献耦合：种子对 {coupling_readings['seed_pairs_total']} 对"
          f"（有引用表种子 {coupling_readings['seeds_with_refs']}／无引用表 "
          f"{coupling_readings['seeds_without_refs']}，读数件留 {coupling_readings['kept']} 对）")
    for pair in report["coupling"]["pairs"][:3]:
        print(f"引文图读法·耦合对：共引 {pair['shared']} 条｜{pair['a']} × {pair['b']}")
    co_readings = report["co_citation"]["readings"]
    print(f"引文图读法·共被引：共被引条款目 {co_readings['co_cited_works']} 条｜对 "
          f"{co_readings['pair_total']}（读数件留 {co_readings['kept']} 对）")
    for pair in report["co_citation"]["pairs"][:3]:
        print(f"引文图读法·共被引对：{pair['co_citations']} 次｜{pair['a_title'] or pair['a'] or '∅'}"
              f" × {pair['b_title'] or pair['b'] or '∅'}")
    print("引文图读法·用途：经典识别／影响标注／缺席核对证据；本轮不作排序输入（权重口径冻结）")
    for note in report["notes"]:
        print(note)
    print(f"引文图读法（读数件）：{report['report_path']}｜请求 {report['not_refetched']['requests']}"
          f"（不重复取数）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="引文图读法：被池内引用计数与文献耦合／共被引（沿票 02 取数）")
    parser.add_argument("--delivery", required=True, help="交付目录（workspace/YYYY-MM-DD-<slug>）")
    parser.add_argument("--top", type=int, default=TOP_PAIRS, help=f"读数件每类保留条数（默认 {TOP_PAIRS}）")
    parser.add_argument("--expansion", help=f"引文扩展读数件（默认 <交付>/{EXPANSION_NAME}）")
    parser.add_argument("--pools", help=f"发现清单（默认 <交付>/{POOLS_NAME}）")
    parser.add_argument("--dedupe", help=f"终池读数件（默认 <交付>/{DEDUPE_NAME}）")
    parser.add_argument("--report", help=f"读数落点（默认 <交付>/{REPORT_NAME}）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.top < 1:
        print("引文图读法错误：--top 应 ≥ 1", file=sys.stderr)
        return 2
    try:
        report, notes = build_report(args)
    except (OSError, KeyError, ValueError) as exc:
        print(f"引文图读法错误：{exc}", file=sys.stderr)
        return 2
    report["notes"] = notes
    write_atomic(Path(report["report_path"]), dumps(report))
    print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
