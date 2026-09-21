# -*- coding: utf-8 -*-
"""Step2 分类分桶：三轮去重（先分后合）＋ 五维排序 ＋ 四桶分桶备料；两处缓存入此件。

    uv run python .claude/skills/med-lit-review/scripts/step2_dedupe_sort_bucket.py \
        --delivery workspace/YYYY-MM-DD-<slug> [--cur-year 2026] \
        [--cache-dir <目录>] [--refresh | --no-cache]

输入（交付内，全部只读）：`run/b1/step1-pools.json`（发现清单；顶层 `frozen_at`＝池采集时刻，作入库
摘要表的 `collected_at`，缺即拒绝产出——不编造时刻、不用重跑日顶替；顶层可选 `current_docs`＝现行版
豁免 DOI 列表、`pico_groups`＝相关性命中词组，缺项保持未知不编造；池级可选 `origin`＝来源标记，
缺省 `discovery`——引文扩展池带 `citation_bwd`／`citation_fwd`，原样进
`dedupe-report.final_records[].origin`，且合并顶替时以最先出现的池（检索池在前）为准）、`run/b1/b1-records.json`
（中文手动补充 B1，条件分支、可缺：缺席按空表记，R2 记 0／0／0）、
`run/b1/step2-verdicts.json`（疑似裁定表，人工／AFK 填，可缺）。
输出（交付内）：`run/b1/` 下 `dedupe-report.json`、`dedupe-decisions.json`、`cited-counts.json`、
`sort-report.json`、`openalex-enrich.json`、`ingest-abstracts.json`（入库摘要表：`entries`／`missing`／
`coverage`；ADR-0021）；另**首写**（已在位即不覆盖）
`agent/doi-list.txt`（身份列表＝终池全量 DOI；见 `references/protocols/workspace-guide.md` §6）。
四桶报告与下载台账骨架、队列（＝研读选取集＝入库集，ADR-0032）**不在此件产出**：行集由选取步
`step2_select.py` 定稿（`plan`／`apply`；P1 路线 `pool-ledger` 以全池行集产四桶报告），本件只备其输入
（排序行集＝`sort-report.json`）。本件仍留 `build_report`／`ledger_skeleton`／`bucket_of`／
`expected_source` 与其桶常量，供选取步导入——同一批文案与桶判据不落第二份。

两处缓存（规则真源 `references/protocols/workspace-guide.md` §14.2；做法与键定义见
`references/protocols/step2-dedupe-and-snapshot.md`）：

  O-03 被引／OA 定位快照：键＝一次实查的 DOI 排序集合 sha256 ＋ select 字段表；窗口 168 小时
      （基准＝条目实查时刻 `queried_at`，命中不顺延）；确定性阴性（无覆盖）同样入缓存并记原因；
      寻址一律走集合键、不走块序号——同一批 DOI 换划分顺序照样命中，集合外的重叠 DOI 逐条复用；
      传输类失败（超时／5xx／连接）永不入缓存、不配查询日期（没读到的数据不记日期）。
      实查范围为去重前的候选 DOI 集（唯一 DOI 数≈终池，读数是终池的超集；`cited-counts.json`
      的 `counts` 按该范围给，逐条日期为原始读数）。
  O-04 去重排序确定性段：键＝输入哈希（池＋记录＋裁定表＋脚本）＋权重版本＋年份
      ＋快照逐条读数（record／status／date）与日期；同键整段短路（去重、排序、分桶备料全部
      不重算，同一批产物原地重发）；任一键项变化即 miss 重算；内容寻址、无 TTL。

裁定三分：人工裁定表（`decided_by` 取裁定表）／规则已判（标题键同而作者或年份不同 →
`decided_by: rule` 给规则原文，不占人工位）／未裁定（照实进 `unadjudicated`、`decision` 留空）。
脚本与段缓存都不代填人工裁定、不猜测；缓存产物在用之前逐条验算（守恒式交叉核对＋裁定依据）。

显式重跑：`--refresh` 越缓存重查重算（新读数照常写回缓存），`--no-cache` 全程不读不写缓存（探针与排障用）。

退出码：0 正常；2 用法、输入文件或裁定表错误。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from step3_queue import norm_doi, read_json  # noqa: E402 - 同族脚本跨件复用（DOI 归一与容错读 JSON 同口径）

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

B1_DIR = "run/b1"
POOLS_NAME = f"{B1_DIR}/step1-pools.json"
RECORDS_NAME = f"{B1_DIR}/b1-records.json"
VERDICTS_NAME = f"{B1_DIR}/step2-verdicts.json"
DEDUPE_REPORT_NAME = f"{B1_DIR}/dedupe-report.json"
DECISIONS_NAME = f"{B1_DIR}/dedupe-decisions.json"
CITED_NAME = f"{B1_DIR}/cited-counts.json"
SORT_NAME = f"{B1_DIR}/sort-report.json"
ENRICH_NAME = f"{B1_DIR}/openalex-enrich.json"
INGEST_ABSTRACTS_NAME = f"{B1_DIR}/ingest-abstracts.json"
# 三件落点名留本模块供选取步（step2_select）使用：本件不再落盘，只作常量真源（避免两处写字面量）
REPORT_NAME = f"{B1_DIR}/bucketing-report.md"
QUEUE_NAME = "agent/download-queue.txt"
LEDGER_NAME = "agent/bucketing-and-sources.md"
DOI_LIST_NAME = "agent/doi-list.txt"
MANIFEST_NAME = "agent/manifest.json"
DEFAULT_CACHE_DIR = SKILL_ROOT / ".cache" / "step2"

SEGMENT_SCHEMA = "step2-segment-v1"
SNAPSHOT_SCHEMA = "step2-cited-v1"
SNAPSHOT_TTL_HOURS = 168
CHUNK_SIZE = 40
CUR_YEAR_DEFAULT = date.today().year
WEIGHT_VERSION = "sort-weights-v2"
WEIGHTS: dict[str, float] = OrderedDict([("rel", 0.4), ("rec", 0.1), ("imp", 0.2), ("comp", 0.2), ("cn", 0.1)])
ENRICH_SELECT = "doi,cited_by_count,open_access,best_oa_location,type,language"
CN_LANG = "zh"  # 中国证据维的语言标记（判据取快照逐条读数 `language`，非题录自述）
B1_ORIGIN = "b1_chinese"  # B1 中文源汇入记录的 origin（R3 顶替时可能记为检索池，故判据并看来源轨迹）
B1_SOURCE = "openalex-zh"  # 同上：load_chinese 写入的来源标记（有 DOI）
B1_SOURCE_NO_DOI = "openalex-zh-no-doi"  # 同上（无 DOI，中文三段键题录）
B1_SOURCES = frozenset((B1_SOURCE, B1_SOURCE_NO_DOI))  # B1 判据用的来源标记全集（成员判据，不依赖顺序）
SNAPSHOT_SOURCE = "OpenAlex cited_by_count（works API 批量实查，按 DOI 过滤）"
SNAPSHOT_NOTE = "无覆盖条目记 ∅ 不计分母，不因缺被引覆盖降分；查询日期为快照原始读数，复用不顺延"
NO_COVERAGE_REASON = "OpenAlex works 批量查询未返回该 DOI（无覆盖，记 ∅）"
UA = {"User-Agent": "med-lit-review-step2/1.0"}
VERDICT_VALUES = ("合并", "判异篇")
OA_EXPECT = {
    "gold": "开放直取（出版商开放获取）",
    "diamond": "开放直取（出版商开放获取）",
    "hybrid": "开放直取（出版商开放获取）",
    "green": "仓储副本（机构／学科仓储）",
    "bronze": "出版商站点免费阅读（无开放许可）",
    "closed": "需机构授权路线",
}

BUCKET_OA_DIRECT = "开放直下（OA direct）"
BUCKET_REPOSITORY = "仓储副本（repository copy）"
BUCKET_INSTITUTIONAL = "需机构（institutional）"
BUCKET_GREY = "灰色候选（grey candidate）"
BUCKET_ORDER = (BUCKET_OA_DIRECT, BUCKET_REPOSITORY, BUCKET_INSTITUTIONAL, BUCKET_GREY)
BUCKET_EXPECT = {
    BUCKET_OA_DIRECT: "出版商开放获取或站点免费直取",
    BUCKET_REPOSITORY: "机构／学科仓储副本",
    BUCKET_INSTITUTIONAL: "需机构授权路线",
    BUCKET_GREY: "Step3 竞速（灰色源默认启用，ADR-0006）",
}

# 段产物逻辑名 → 交付内落点（缓存按逻辑名存文本；四桶报告与队列不属本段，由选取步定稿）
SEGMENT_OUTPUTS = {
    "dedupe_report": DEDUPE_REPORT_NAME,
    "dedupe_decisions": DECISIONS_NAME,
    "sort_report": SORT_NAME,
    "ingest_abstracts": INGEST_ABSTRACTS_NAME,
}


# ---------------------------------------------------------------- 读写与归一


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def canonical_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(path.suffix + ".staging")
    staging.write_text(text, encoding="utf-8")
    os.replace(staging, path)


def write_if_changed(path: Path, text: str) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    write_atomic(path, text)
    return True


def write_if_absent(path: Path, text: str) -> bool:
    """版内首写件（台账骨架、身份列表）：已在位一律不覆盖（不回写 Step3 已填的台账）。"""
    if path.is_file():
        return False
    write_atomic(path, text)
    return True


def norm_title(text: str | None) -> str:
    s = unicodedata.normalize("NFKC", text or "").lower()
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[^\w一-鿿]+", "", s)


def norm_author(text: str | None) -> str:
    return re.sub(r"[^\w一-鿿]+", "", unicodedata.normalize("NFKC", text or "").lower())


def year_of(rec: dict):
    return rec.get("publication_year") or rec.get("publicationYear") or (
        int(rec["publishedDate"][:4]) if (rec.get("publishedDate") or "")[:4].isdigit() else None
    )


def three_keys(rec: dict):
    """去重键三层：DOI 一级键、标题归一二级键、中文题录三段键。"""
    doi = rec["doi"]
    tkey = norm_title(rec["title"])
    nauth = norm_author((rec["authors"] or [None])[0])
    yr = rec["year"]
    three = f"{tkey}|{nauth}|{yr if yr is not None else '∅'}|∅" if tkey and nauth and yr is not None else None
    return doi, tkey, three


CJK_RE = re.compile(r"[一-鿿]")


def primary_key(rec: dict) -> str | None:
    """一级／二级／中文三段键：DOI 优先；无 DOI 的中文题录走三段键。

    无标识学术搜索记录（无 DOI 且非中文题录）禁入持久化，返回 None 进 identity_rejected。
    """
    doi, _tkey, three = three_keys(rec)
    if doi:
        return "D:" + doi
    if three and CJK_RE.search(rec.get("title") or ""):
        return "T3:" + three
    return None


def completeness(rec: dict) -> int:
    return sum(
        bool(x)
        for x in (rec.get("doi"), rec.get("title"), rec.get("authors"), rec.get("venue"),
                  rec.get("year"), rec.get("abstract"), rec.get("type"))
    )


def pair_key(a_doi: str | None, b_doi: str | None, a_title: str | None) -> list[str]:
    """疑似对的裁定键：两个归一 DOI ＋ 一方题名归一前 20 字（DOI 同标题异时用它区分）。"""
    return [norm_doi(a_doi), norm_doi(b_doi), norm_title(a_title)[:20]]


def pair_tuple(pair) -> tuple[str, str, str]:
    p = list(pair or []) + ["", "", ""]
    return tuple(pair_key(p[0], p[1], p[2]))


def chunks_of(items: list[str], size: int = CHUNK_SIZE) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def parse_date(text: str | None) -> date | None:
    text = (text or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text)
    if re.match(r"\d{4}-\d{2}-\d{2}T", text):
        return date.fromisoformat(text[:10])
    return None


# ---------------------------------------------------------------- 载入候选


def load_pools(path: Path) -> tuple[list[dict], dict]:
    """读发现清单：返回（候选记录，顶层参数）。顶层参数＝current_docs／pico_groups（缺项不改语义）＋frozen_at（池采集时刻）。"""
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("pools"), list):
        raise ValueError(f"发现清单形态不符（需 {{\"pools\": […]}}）：{path}")
    cands: list[dict] = []
    for pool in data["pools"]:
        for rec in pool.get("results") or pool.get("items") or []:
            cands.append(
                {
                    "doi": norm_doi(rec.get("doi")),
                    "title": rec.get("title"),
                    "authors": [a for a in (rec.get("authors") or []) if a],
                    "venue": rec.get("venue"),
                    "year": year_of(rec),
                    "publishedDate": rec.get("publishedDate"),
                    "type": rec.get("type"),
                    "abstract": rec.get("abstract") or "",
                    "citationCount": rec.get("citationCount"),
                    "sources": sorted({rec.get("externalSource") or rec.get("retrievalProvider") or "unknown"}),
                    "externalIds": sorted({f"{rec.get('externalSource')}:{rec.get('externalId')}"}),
                    "origin": pool.get("origin") or "discovery",
                    "pool_ids": [pool.get("pool_id") or "pool"],
                    "family": pool.get("family"),
                    "window": pool.get("window"),
                    "route_label": None,
                }
            )
    # 原样返回全部候选：去重与来源轨迹归并一律留给 R1 记账，这里不预先合并
    params = {
        "current_docs": {norm_doi(d) for d in (data.get("current_docs") or []) if norm_doi(d)},
        "pico_groups": [list(g) for g in (data.get("pico_groups") or []) if g],
        "frozen_at": (data.get("frozen_at") or "").strip(),
    }
    return cands, params


def load_chinese(path: Path) -> list[dict]:
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise ValueError(f"中文补充形态不符（需 {{\"records\": […]}}）：{path}")
    out = []
    for rec in data["records"]:
        out.append(
            {
                "doi": norm_doi(rec.get("doi")),
                "title": rec.get("title"),
                "authors": [a for a in (rec.get("authors") or []) if a],
                "venue": rec.get("venue"),
                "year": rec.get("publication_year"),
                "publishedDate": rec.get("publication_date"),
                "type": rec.get("type"),
                "abstract": rec.get("abstract") or "",
                "citationCount": rec.get("cited_by_count"),
                "sources": [B1_SOURCE if rec.get("doi") else B1_SOURCE_NO_DOI],
                "externalIds": [f"openalex:{rec.get('openalex_id')}"],
                "origin": B1_ORIGIN,
                "pool_ids": [],
                "family": "中文补充",
                "window": "全窗",
                "route_label": rec.get("route_label"),
                "crossref": rec.get("crossref_registered"),
                "is_oa": rec.get("is_oa"),
                "oa_status": rec.get("oa_status"),
                "volume": rec.get("volume"),
                "issue": rec.get("issue"),
                "first_page": rec.get("first_page"),
                "last_page": rec.get("last_page"),
                "issn_l": rec.get("issn_l"),
            }
        )
    return out


def load_verdicts(path: Path) -> dict:
    """读疑似裁定表（人工位，可缺）。返回 {decisions: {pair_key: …}, decided_by, decided_at, sha256}。"""
    if not path.is_file():
        return {"decisions": {}, "decided_by": None, "decided_at": None, "sha256": "absent", "path": path}
    blob = path.read_bytes()
    data = json.loads(blob.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        raise ValueError(f"裁定表形态不符（需 {{\"decisions\": […]}}）：{path}")
    table: dict[tuple, dict] = {}
    for item in data["decisions"]:
        pair, verdict = item.get("pair"), (item.get("verdict") or "").strip()
        reason = (item.get("reason") or "").strip()
        if not isinstance(pair, list) or len(pair) != 3:
            raise ValueError(f"裁定表条目缺 pair（[a_doi, b_doi, a_title_key]）：{json.dumps(item, ensure_ascii=False)}")
        if verdict not in VERDICT_VALUES:
            raise ValueError(f"裁定值只认 {'／'.join(VERDICT_VALUES)}：{json.dumps(item, ensure_ascii=False)}")
        if not reason:
            raise ValueError(f"裁定条目缺理由（人工位不得空挂）：{json.dumps(item, ensure_ascii=False)}")
        key = pair_tuple(pair)
        if key in table and table[key]["verdict"] != verdict:
            raise ValueError(f"裁定表内同键两种裁定：{list(key)}")
        table[key] = {"verdict": verdict, "reason": reason}
    return {
        "decisions": table,
        "decided_by": data.get("decided_by"),
        "decided_at": data.get("decided_at"),
        "sha256": sha256_bytes(blob),
        "path": path,
    }


# ---------------------------------------------------------------- 三轮去重


def absorb(keeper: dict, dropped: dict) -> None:
    """把被合并条目的来源轨迹并入保留条目，字段缺项按最全者补。"""
    keeper["sources"] = sorted(set(keeper["sources"]) | set(dropped["sources"]))
    keeper["externalIds"] = sorted(set(keeper["externalIds"]) | set(dropped["externalIds"]))
    keeper["pool_ids"] = sorted(set(keeper.get("pool_ids", [])) | set(dropped.get("pool_ids", [])))
    for field in ("abstract", "venue", "year", "type", "title"):
        if not keeper.get(field) and dropped.get(field):
            keeper[field] = dropped[field]
    for field in ("first_page", "last_page", "volume", "issue", "issn_l"):
        if not keeper.get(field) and dropped.get(field):
            keeper[field] = dropped[field]


def dedupe_round(records: list[dict], round_name: str, verdicts: dict) -> dict:
    """一轮去重：先按键归并（DOI 一级 → 中文三段键），再按标题键检疑似。

    判定口径：DOI 相同即同篇（合并）；标题键同而作者或年份不同判异篇（各自保留）；
    一方无 DOI 的标题撞键与「标题同而 DOI 异（作者年份同）」判疑似，进人工确认，不自动合并。
    裁定只认裁定表：未裁定的疑似对原样保留并记 suspected（不代填、不猜测）。
    无稳定身份（无 DOI 且无标题＋作者＋年）可筛不可入持久化。
    """
    table = verdicts.get("decisions") or {}
    kept: list[dict] = []
    removed: list[dict] = []
    suspected: list[dict] = []
    identity_rejected: list[dict] = []
    index: dict[str, dict] = {}
    title_index: dict[str, list[dict]] = {}

    for rec in records:
        pk = primary_key(rec)
        if pk is None:
            identity_rejected.append(
                {
                    "title": (rec.get("title") or "")[:90] or "（无题名）",
                    "sources": rec["sources"],
                    "reason": "无标识学术搜索记录（无 DOI 且非中文三段键题录），可筛不可入持久化",
                }
            )
            continue
        tkey = norm_title(rec["title"])
        if pk in index:
            keeper = index[pk]
            why = "DOI 同" if pk.startswith("D:") else "中文三段键同"
            # DOI 同而标题异：判疑似进人工确认；裁定为「合并」的并入保留条目，裁定「判异篇」的各自保留
            if pk.startswith("D:") and norm_title(keeper["title"]) != tkey:
                pair = pair_key(keeper["doi"], rec["doi"], keeper["title"])
                verdict = table.get(tuple(pair))
                suspected.append(
                    {
                        "a_title": keeper["title"], "a_doi": keeper["doi"], "a_year": keeper["year"],
                        "b_title": rec["title"], "b_doi": rec["doi"], "b_year": rec["year"],
                        "pair": pair, "reason": "DOI 同标题异",
                    }
                )
                if verdict is None or verdict["verdict"] != "合并":
                    kept.append(rec)  # 未裁定或判异篇：原样保留，不丢记录
                    continue
            if completeness(rec) > completeness(keeper):  # 保留字段最全者，位置不变
                pos = kept.index(keeper)
                absorb(rec, keeper)
                rec["origin"] = keeper["origin"]  # origin 记最先入库的池（检索池在扩展池之前），顶替不改口径
                kept[pos] = rec
                index[pk] = rec
                keeper, dropped = rec, keeper
            else:
                absorb(keeper, rec)
                dropped = rec
            removed.append(
                {
                    "kept_title": (keeper.get("title") or "")[:110],
                    "kept_sources": keeper["sources"],
                    "dropped_title": (dropped.get("title") or "")[:110],
                    "dropped_sources": dropped["sources"],
                    "doi": keeper["doi"] or dropped["doi"] or "无",
                    "reason": why,
                }
            )
            continue
        kept.append(rec)
        index[pk] = rec
        if tkey:
            title_index.setdefault(tkey, []).append(rec)

    # 疑似：同一标题键下跨不同一级键的碰撞
    for tkey, group in title_index.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a["doi"] and b["doi"] and a["doi"] == b["doi"]:
                    continue  # 已按 DOI 合并
                same_person = norm_author((a["authors"] or [None])[0]) == norm_author((b["authors"] or [None])[0])
                same_year = a["year"] == b["year"] and a["year"] is not None
                if bool(a["doi"]) != bool(b["doi"]):
                    reason = "一方无 DOI 的标题撞键"
                elif same_person and same_year:
                    reason = "标题归一撞键而 DOI 异（第一作者与年相同）"
                else:
                    # 标题键同而作者或年份不同：规则直接判异篇（登记进疑似表留痕，裁定由规则给出、
                    # 不占人工位；人工若另给裁定表条目以人工为准）
                    reason = "标题归一撞键而第一作者或年份不同（规则判异篇）"
                entry = {
                    "a_title": a["title"], "a_doi": a["doi"] or "无", "a_year": a["year"],
                    "b_title": b["title"], "b_doi": b["doi"] or "无", "b_year": b["year"],
                    "pair": pair_key(a["doi"], b["doi"], a["title"]), "reason": reason,
                }
                if reason.endswith("（规则判异篇）"):
                    entry["rule_verdict"] = "判异篇"
                    entry["rule_note"] = "规则：标题归一撞键而第一作者或年份不同，判异篇（不自动合并）"
                suspected.append(entry)

    # 输入数＝进入键归并的记录数（无稳定身份者在归并前剔除，单独记 prefiltered_identity_rejected）
    # 由此保证计数守恒：input ＝ kept ＋ merged；剔除数另列，不混入守恒式。
    eligible = len(records) - len(identity_rejected)
    return {
        "round": round_name,
        "input_count": eligible,
        "prefiltered_identity_rejected": len(identity_rejected),
        "kept_count": len(kept),
        "merged_count": len(removed),
        "suspected_count": len(suspected),
        "suspected_ambiguous_count": sum(1 for s in suspected if not s.get("rule_verdict")),
        "kept": kept,
        "removed": removed,
        "suspected": suspected,
        "identity_rejected": identity_rejected,
    }


def render_decisions(rounds: list[dict], verdicts: dict) -> dict:
    """按裁定表落裁定结果；规则已判异篇的对由规则给出（`decided_by: rule`）；
    既无人工裁定又非规则判定的疑似对进 `unadjudicated`（人工位不代填、不猜测）。"""
    table = verdicts.get("decisions") or {}
    decisions, unadjudicated, seen, used = [], [], set(), set()
    for rnd in rounds:
        for s in rnd["suspected"]:
            key = tuple(s["pair"])
            verdict = table.get(key)
            if verdict is not None:  # 人工裁定优先
                used.add(key)
                s["decision"] = f"{verdict['verdict']}（{verdict['reason']}）"
                decision = {"verdict": verdict["verdict"], "reason": verdict["reason"],
                            "decided_by": verdicts.get("decided_by"), "decided_at": verdicts.get("decided_at")}
            elif s.get("rule_verdict"):  # 规则已判：不占人工位、不记人工字段
                s["decision"] = f"{s['rule_verdict']}（{s['rule_note']}）"
                decision = {"verdict": s["rule_verdict"], "reason": s["rule_note"],
                            "decided_by": "rule", "decided_at": None}
            else:  # 人工位空缺：照实列未裁定
                s["decision"] = ""
                if key not in seen:
                    seen.add(key)
                    unadjudicated.append(
                        {
                            "pair": list(key),
                            "a_title": (s["a_title"] or "")[:70],
                            "b_title": (s["b_title"] or "")[:70],
                            "reason": s["reason"],
                        }
                    )
                continue
            decisions.append(
                {
                    "round": rnd["round"],
                    "pair": list(key),
                    "a_title": s["a_title"], "a_doi": s["a_doi"], "a_year": s["a_year"],
                    "b_title": s["b_title"], "b_doi": s["b_doi"], "b_year": s["b_year"],
                    "suspected_reason": s["reason"], "verdict": decision["verdict"], "reason": decision["reason"],
                    "decided_by": decision["decided_by"], "decided_at": decision["decided_at"],
                }
            )
    decisions_unused = [f"{v['verdict']}｜{k[0]}｜{k[1]}" for k, v in table.items() if k not in used]
    return {
        "decisions": decisions,
        "decided_at": verdicts.get("decided_at"),
        "decided_by": verdicts.get("decided_by"),
        "unadjudicated": unadjudicated,
        "decisions_unused": decisions_unused,
    }


# ---------------------------------------------------------------- 排序五维


def s_rel(rec: dict, pico_groups: list[list[str]]) -> float | None:
    """相关性：题名／摘要对 PICO 词组的命中覆盖度（确定性、可复算）。

    词组由发现清单顶层 `pico_groups` 给；缺项即无词组可判——记 ∅，不编造。
    """
    if not pico_groups:
        return None
    text = ((rec.get("title") or "") + " " + (rec.get("abstract") or "")).lower()
    hit = sum(1 for group in pico_groups if any(str(k).lower() in text for k in group if k))
    return round(hit / len(pico_groups), 3)


def s_rec(rec: dict, cur_year: int, current_docs: set[str]) -> float | None:
    """时效：现行版（指南／说明书）豁免记满分；其余按发表年线性衰减（半衰期 8 年）。"""
    if rec["doi"] and rec["doi"] in current_docs:
        return 1.0
    yr = rec.get("year")
    if not yr:
        return None  # 未知日期保持未知，记 ∅
    return round(max(0.0, 1.0 - (cur_year - yr) / 24.0), 3)


def s_imp(cited) -> float | None:
    """影响：被引对数归一（1000 为 1.0 的刻度）。"""
    if cited is None:
        return None
    return round(min(1.0, math.log10(cited + 1) / math.log10(1001)), 3)


def s_comp(rec: dict) -> float:
    fields = (rec.get("doi"), rec.get("title"), rec.get("authors"), rec.get("venue"),
              rec.get("year"), rec.get("abstract"), rec.get("type"))
    return round(sum(bool(f) for f in fields) / len(fields), 3)


def s_cn(rec: dict) -> float:
    """中国证据：`language=zh` 或 B1 中文源汇入记录记 1.0，其余 0.0。

    出处标记（语言／来源），既不是证据等级也不是人群字段：英文发表的中国人群研究不在本维
    （需机构国别／人群字段，登记为待扩展边界）。恒有值，故从不进 `unknown_dims`——「未知维不计
    分母」不变式约束的是 rel／rec／imp 三类缺覆盖维，本维按冻结口径「其余 0.0」判。
    """
    if (rec.get("language") or "").strip().lower() == CN_LANG:
        return 1.0
    if rec.get("origin") == B1_ORIGIN or any(s in B1_SOURCES for s in rec.get("sources") or []):
        return 1.0
    return 0.0


def build_rows(final: list[dict], params: dict, cur_year: int) -> list[dict]:
    rows = []
    for c in final:
        dims = {
            "rel": s_rel(c, params["pico_groups"]),
            "rec": s_rec(c, cur_year, params["current_docs"]),
            "imp": s_imp(c.get("cited_by_count")),
            "comp": s_comp(c),
            "cn": s_cn(c),
        }
        known = {k: v for k, v in dims.items() if v is not None}
        unknown = [k for k, v in dims.items() if v is None]
        score = round(sum(WEIGHTS[k] * v for k, v in known.items()) / sum(WEIGHTS[k] for k in known), 4) if known else 0.0
        rows.append(
            {
                "key": c["doi"] or ("T:" + norm_title(c["title"])[:24]),
                "title": (c.get("title") or "")[:110],
                "language": c.get("language"),
                "dims": dims,
                "unknown_dims": unknown,
                "weight_version": WEIGHT_VERSION,
                "score": score,
                "cited_by_count": c.get("cited_by_count"),
                "sources": c["sources"],
                "oa_status": (c.get("oa") or {}).get("oa_status"),
            }
        )
    rows.sort(key=lambda r: (-r["score"], r["key"] or ""))
    for i, r in enumerate(rows, 1):
        r["order"] = i
    return rows


def bucket_of(oa_status: str | None) -> str:
    oa = (oa_status or "").lower()
    if oa in ("gold", "diamond", "hybrid", "bronze"):
        # bronze＝出版商站点免费阅读（无开放许可），同属免费直取通道
        return BUCKET_OA_DIRECT
    if oa == "green":
        return BUCKET_REPOSITORY
    if oa in ("closed", ""):
        # 无 OA 读数（∅）不冒认开放，按需机构路线记
        return BUCKET_INSTITUTIONAL
    return BUCKET_GREY


def expected_source(oa_status: str | None) -> str:
    return OA_EXPECT.get((oa_status or "").lower(), "需机构授权路线")


def build_report(rows: list[dict], buckets: dict[str, list[dict]], rows_total: int, final_count: int,
                 identity_rejected: int, snapshot: dict, weight_version: str,
                 coverage: dict, missing_count: int,
                 must_take_count: int | None = None, rank_fill_count: int | None = None) -> str:
    """四桶报告（忽略位中间件）：一行一条（＝行集条目），内容只含稳定读数。

    行集两口径（ADR-0032）：选取步 `apply` 传研读选取集行并给 `must_take_count`／`rank_fill_count`
    两读数；P1 路线 `pool-ledger` 传终池全量有 DOI 键行（两参省略＝全池口径，不产队列件）。
    守恒行原文固定供 B-09 读：`- 桶计数之和：{total}；队列条目数：{len(queue)}（一致）。`
    本轮缓存行为统计只进 `cited-counts.json` 的 `cache` 块与运行日志，不进本件。
    """
    queue = [r["key"] for r in rows]
    if must_take_count is None:
        scope = (f"- 行集：终池全量有 DOI 键 {len(queue)} 条（P1 路线，不产队列件；终池 {rows_total} 条）")
    else:
        scope = (f"- 行集：研读选取集（＝下载队列＝入库集，Step3 输入）{len(queue)} 条"
                 f"（必取 {must_take_count} 条 ＋ 按 S 序补足 {rank_fill_count} 条；"
                 f"终池有 DOI 键 {rows_total} 条）")
    readings = snapshot["readings"]
    no_coverage = sum(1 for r in readings.values() if r.get("status") == "no_coverage")
    transport = sum(1 for r in readings.values() if r.get("status") == "transport_error")
    lines = [
        "# 四桶报告与来源路由（Step2 选取步输出）",
        "",
        scope,
        f"- 去重终池：{final_count} 条；无稳定身份剔除 {identity_rejected} 条（可筛不可入持久化）",
        f"- 被引／OA 快照：查询日期 {'／'.join(snapshot['dates']) or '无（本轮未读到数据）'}"
        f"（来源 OpenAlex；无覆盖 {no_coverage} 条、传输失败未读 {transport} 条；读数日期为快照原始读数）",
        f"- 入库摘要表（`{INGEST_ABSTRACTS_NAME}`）：覆盖率 {coverage['with_abstract']}/"
        f"{coverage['denominator']}（无摘要 {missing_count} 条；类型未知 {coverage['unknown_type']} 条）",
        f"- 排序：权重版本 `{weight_version}`（真源 `run/b1/sort-report.json`）；未知维记 ∅ 不计分母；"
        f"证据等级与设计不进排序",
        "",
        "## 四桶计数（与队列条目数一致）",
        "",
        "| 桶 | 计数 | 预计来源 |",
        "|---|---|---|",
    ]
    for name in BUCKET_ORDER:
        lines.append(f"| {name} | {len(buckets[name])} | {BUCKET_EXPECT[name]} |")
    total = sum(len(buckets[name]) for name in BUCKET_ORDER)
    lines += [
        "",
        f"- 桶计数之和：{total}；队列条目数：{len(queue)}（一致）。",
        "",
        "## 逐条路由（行集条目 = 下载输入与入库集；实际来源与台账列由 Step3 回填）",
        "",
        "| DOI | 桶 | OA 状态 | 预计来源 | S | 被引 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        cited = r["cited_by_count"] if r["cited_by_count"] is not None else "∅"
        lines.append(
            f"| {r['key']} | {bucket_of(r.get('oa_status'))} | {r.get('oa_status') or '未知'} | "
            f"{expected_source(r.get('oa_status'))} | {r['score']} | {cited} |"
        )
    lines += [
        "",
        "## 边界说明",
        "- 本表只记路由预测（按 OpenAlex OA 状态定位）；实际来源以 Step3 下载返回为准，漂移如实记入下载台账。",
        "- 灰色候选桶按 ADR-0006 默认进 Step3 竞速（不再询问）；未静默改访问设置、未静默升级策略。",
        "- 需机构桶与全通道未命中条目按交互点二处理（勾选后才动，不静默改设置）；缺口进局限声明。",
        "- 被引查询日期为快照原始读数（复用不顺延、不写重跑日）；无覆盖条目记 ∅ 不计分母。",
        "- P1 路线（`pool-ledger`）行集＝终池全量，无队列件：上文「队列条目数」即全池行数；"
        "P0 路线行集＝研读选取集（＝下载队列＝入库集）。",
        "",
    ]
    return "\n".join(lines)


def ingest_abstracts(final: list[dict], pool_records: list[dict], collected_at: str,
                     inputs: dict[str, str]) -> tuple[str, dict, int]:
    """入库摘要表（ADR-0021）与覆盖率：一处算出两件产物（表文本、报告字段），不给同一分母两套口径。

    表形态（入库侧 `use-zotero/scripts/abstracts.py` 的 `load_table` 按此判读，偏离即抛）：`entries`＝
    归一 DOI → `{abstract, source, collected_at}`／`missing`＝无正文的归一 DOI 清单／`coverage`（见下）。
    键集＝终池标 DOI 记录按归一 DOI 去重后的全 DOI 集（表与清单互补、合起来正是它）；终池外的 DOI
    （去重时整条被弃的无标识记录）不进表。

    口径三条——
    ① 同 DOI 取最完整者，正文取范围＝**终池记录的正文 ＋ 本次读入的全部同 DOI 池记录正文**（去重时被并入
       记录的正文、仍并列的同 DOI 记录正文都在内；同篇多来源取最长者）：**最长**正文胜出，并列取先出现者
       （位序稳定）；`source`＝胜出记录的池来源轨迹（`externalSource`，缺则 `retrievalProvider`，皆缺记
       `unknown`；合并过的记录多条以「、」连接）。正文取池内原样文本（首尾空白去掉），清洗口径不在此复制
       ——归入库侧同一份实现。
    ② 分母＝终池标 DOI 的**记录**数：入库侧对每条这类记录都判「可摘要」文种，两侧因此同一口径；`type`
       缺失者照计分母（入库侧同判可摘要），另单列 `unknown_type` 作数据质量读数、绝不计出分母之外。
       终池记录的 `type` 经去重字段补齐而来，与原始池的无类型条数不必相等。
    ③ 不造映射：池 provider 的类型串（OpenAlex 的 `article`／`review`／`editorial`／`erratum`）与入库侧文种
       （`journalArticle`／`preprint`／`conferencePaper`…）不是一套词汇，此处只报「有无」，不做转换——
       谁都不许把这张表「修」成映射表。
    """
    best: dict[str, dict] = {}
    dois: set[str] = set()
    denominator = 0
    unknown_type = 0

    def offer(doi: str, abstract, sources: list[str]) -> None:
        text = (abstract or "").strip()
        if text and (doi not in best or len(text) > len(best[doi]["abstract"])):
            best[doi] = {"abstract": text, "source": "、".join(sources) or "unknown",
                         "collected_at": collected_at}

    for rec in pool_records:  # 同 DOI 多来源（含去重时被并入者）：先按池记录取最长，终池记录随后同台比较
        if rec["doi"]:
            offer(rec["doi"], rec.get("abstract"), rec["sources"])
    for rec in final:
        doi = rec["doi"]
        if not doi:
            continue
        denominator += 1
        dois.add(doi)
        if not (rec.get("type") or "").strip():
            unknown_type += 1
        offer(doi, rec.get("abstract"), rec["sources"])  # 终池记录可能带 absorb 补齐的正文
    entries = {doi: best[doi] for doi in sorted(dois) if doi in best}
    missing = sorted(doi for doi in dois if doi not in best)
    coverage = {"denominator": denominator, "with_abstract": len(entries), "unknown_type": unknown_type,
                "inputs": dict(inputs)}
    return dumps({"entries": entries, "missing": missing, "coverage": coverage}), coverage, len(missing)


def ledger_skeleton(rows: list[dict], caliber: str, final_count: int, identity_rejected: int,
                    snapshot: dict, weight_version: str,
                    must_take_count: int | None = None, rank_fill_count: int | None = None) -> str:
    """版内下载台账骨架：四桶计数 ＋ §7 十列逐条路由行（一行一条）；实际来源等列留空由 Step3 回填。

    行集两口径（ADR-0032）：`apply` 传研读选取集行（＝入库集，给两选取读数）并以首写落盘（不回写
    Step3 已填的台账）；P1 路线 `pool-ledger` 传终池全量有 DOI 键行（两参省略＝全池口径，覆盖落盘）。
    """
    queue = [r["key"] for r in rows]
    readings = snapshot["readings"]
    no_coverage = sum(1 for r in readings.values() if r.get("status") == "no_coverage")
    transport = sum(1 for r in readings.values() if r.get("status") == "transport_error")
    if must_take_count is None:
        title = "# 四桶报告＋下载台账路由（全池行集；Step2 选取步 `pool-ledger` 产出骨架）"
        scope = (f"桶为**预测**（下载验证前），按终池全量有 DOI 键行分列（{len(queue)} 条）＝下表行数；"
                 f"P1 路线不产队列件。")
    else:
        title = "# 四桶报告＋下载台账路由（研读选取集；Step2 选取步 `apply` 产出骨架）"
        scope = (f"桶为**预测**（下载验证前），按研读选取集条目分列（{len(queue)} 条＝必取 {must_take_count} 条"
                 f"＋按 S 序补足 {rank_fill_count} 条）＝下表行数；本表行集＝下载队列＝入库集。")
    lines = [
        title,
        "",
        f"> 口径版本：{caliber}",
        "",
        f"方法：三轮去重（终池 {final_count} 条；无稳定身份剔除 {identity_rejected} 条）＋五维排序"
        f"（`{weight_version}`；未知维 ∅ 不计分母）＋OA 定位分桶。",
        scope,
        f"被引／OA 快照：查询日期 {'／'.join(snapshot['dates']) or '无（本轮未读到数据）'}（来源 OpenAlex；"
        f"无覆盖 {no_coverage} 条、传输失败未读 {transport} 条）。",
        "",
        "| 桶 | 计数 | 含义 |",
        "|---|---|---|",
    ]
    counts = {name: 0 for name in BUCKET_ORDER}
    for r in rows:
        counts[bucket_of(r.get("oa_status"))] += 1
    for name in BUCKET_ORDER:
        lines.append(f"| {name} | {counts[name]} | {BUCKET_EXPECT[name]} |")
    lines += [
        "",
        f"- 计数一致：四桶 {'＋'.join(str(counts[n]) for n in BUCKET_ORDER)} ＝ {len(queue)} ＝ 队列条目数。",
        "",
        "## 逐条路由行（下载台账十列；实际来源／采用／未采用原因／grey_hit／身份核验／文件列由 Step3 回填）",
        "",
        "| DOI | 桶 | 预计来源 | 实际来源 | 采用 | 未采用原因 | grey_hit | 身份核验 | 文件 | 口径版本 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['key']} | {bucket_of(r.get('oa_status'))} | {expected_source(r.get('oa_status'))} | | | | | | | {caliber} |"
        )
    lines += [
        "",
        "## 边界说明",
        "- 桶与预计来源是 Step2 路由预测；实际来源以 Step3 下载返回为准（逐篇如实，含灰色路线）。",
        "- 灰色候选桶按 ADR-0006 默认进 Step3 竞速（不再询问）；未静默改访问设置、未静默升级策略。",
        "- 身份核验列只记短串（完整证据留在被忽略身份收据，`agent/manifest.json` 记其哈希）。",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- O-03 被引／OA 快照


def snapshot_key(dois: list[str]) -> str:
    """快照键＝一次实查的 DOI 排序集合 ＋ select 字段表（不按块序号寻址）。"""
    payload = canonical_json({"schema": SNAPSHOT_SCHEMA, "select": ENRICH_SELECT, "dois": sorted(set(dois))})
    return sha256_text(payload)


def http_json(url: str, timeout: int = 60, attempts: int = 3):
    last = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code == 404:
                return None, last
        except Exception as exc:  # 传输类失败：不缓存、不冒认无覆盖
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(2 * (i + 1))
    return None, last


def fetch_chunk(dois: list[str]) -> tuple[dict | None, list[str], str | None]:
    """一次批量实查：返回（逐条读数，无覆盖 DOI，传输错误）。"""
    url = (
        "https://api.openalex.org/works?filter=doi:"
        + "|".join(urllib.parse.quote(d) for d in dois)
        + f"&per-page={max(len(dois), 1)}&select={ENRICH_SELECT}"
    )
    data, err = http_json(url)
    if data is None:
        return None, [], err or "未知传输错误"
    records, got = {}, set()
    for w in data.get("results", []):
        d = norm_doi(w.get("doi"))
        got.add(d)
        oa = w.get("open_access") or {}
        best = w.get("best_oa_location") or {}
        src = best.get("source") or {}
        records[d] = {
            "cited_by_count": w.get("cited_by_count"),
            "is_oa": oa.get("is_oa"),
            "oa_status": oa.get("oa_status"),
            "oa_url": oa.get("oa_url"),
            "best_pdf_url": best.get("pdf_url"),
            "best_landing": best.get("landing_page_url"),
            "best_source": src.get("display_name"),
            "best_source_type": src.get("type"),
            "best_is_repository": bool(src.get("type") == "repository"),
            "language": w.get("language"),
            "type": w.get("type"),
        }
    return records, [d for d in dois if d not in got], None


class SnapshotCache:
    """O-03 快照缓存：一格一文件；窗口 168 小时自条目实查时刻（`queried_at`，缺则回退 `query_date` 零时）起算，命中不顺延。"""

    def __init__(self, root: Path, enabled: bool, refresh: bool):
        self.dir = root / "cited"
        self.read_enabled = enabled and not refresh
        self.write_enabled = enabled

    def entries(self, today: date) -> dict[str, dict]:
        out: dict[str, dict] = {}
        if not self.read_enabled or not self.dir.is_dir():
            return out
        now = datetime.now()
        for path in sorted(self.dir.glob("*.json")):
            data = read_json(path)
            if not isinstance(data, dict) or data.get("schema") != SNAPSHOT_SCHEMA:
                continue  # 损坏或异版本：当 miss
            queried = parse_date(data.get("query_date"))
            if queried is None or not isinstance(data.get("records"), dict):
                continue
            if now - self.moment_of(data, queried) > timedelta(hours=SNAPSHOT_TTL_HOURS):
                continue  # 超窗即 miss（到期基准＝快照自身实查时刻，复用不顺延）
            dois = list(data["records"]) + [nc.get("doi", "") for nc in data.get("no_coverage", [])]
            if snapshot_key(dois) != data.get("key"):
                continue  # 键与内容不符：当 miss
            out[data["key"]] = data
        return out

    @staticmethod
    def moment_of(data: dict, fallback: date) -> datetime:
        """条目实查时刻：`queried_at` 秒级优先，缺失回退到 `query_date` 当日起点。"""
        text = (data.get("queried_at") or "").strip()
        if text:
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
            except ValueError:
                pass
        return datetime.combine(fallback, datetime.min.time())

    def write(self, payload: dict) -> None:
        if not self.write_enabled:
            return
        write_atomic(self.dir / f"{payload['key']}.json", dumps(payload))


def resolve_snapshot(dois: list[str], cache: SnapshotCache, today: date) -> dict:
    """取被引／OA 定位读数：先集合键命中，再逐条复用重叠 DOI，最后才联网实查。

    逐条读数带 `status`：`covered`（有读数）／`no_coverage`（确定性阴性，记原因）／
    `transport_error`（传输类失败：不入缓存、`date` 为空——没读到的数据不配查询日期）。
    """
    ordered = sorted({d for d in dois if d})
    entries = cache.entries(today)
    stats = {"exact_hits": 0, "overlap_dois": 0, "network_chunks": 0, "network_dois": 0}
    readings: dict[str, dict] = {}  # doi -> {"record", "status", "reason", "date"}
    pool_index: dict[str, dict] = {}
    for entry in entries.values():
        for doi, rec in entry["records"].items():
            pool_index.setdefault(doi, {"record": rec, "status": "covered", "reason": None, "date": entry["query_date"]})
        for item in entry.get("no_coverage", []):
            pool_index.setdefault(item["doi"], {"record": None, "status": "no_coverage",
                                                "reason": item["reason"], "date": entry["query_date"]})
    transport_errors: list[dict] = []

    for chunk in chunks_of(ordered):
        entry = entries.get(snapshot_key(chunk))
        if entry is not None:  # 集合键整块命中
            stats["exact_hits"] += 1
            for doi, rec in entry["records"].items():
                readings[doi] = {"record": rec, "status": "covered", "reason": None, "date": entry["query_date"]}
            for item in entry.get("no_coverage", []):
                readings[item["doi"]] = {"record": None, "status": "no_coverage",
                                         "reason": item["reason"], "date": entry["query_date"]}
            continue
        pending = []
        for doi in chunk:
            known = pool_index.get(doi)
            if known is not None:  # 重叠 DOI 逐条复用（跨块划分照样命中）
                readings[doi] = dict(known)
                stats["overlap_dois"] += 1
            else:
                pending.append(doi)
        if not pending:
            continue
        stats["network_chunks"] += 1
        stats["network_dois"] += len(pending)
        records, no_cover, err = fetch_chunk(pending)
        if records is None:
            transport_errors.append({"dois": list(pending), "error": err})  # 传输类永不入缓存
            for doi in pending:
                readings[doi] = {"record": None, "status": "transport_error", "reason": None, "date": None}
            continue
        for doi in pending:
            reading = ({"record": records[doi], "status": "covered", "reason": None, "date": today.isoformat()}
                       if doi in records
                       else {"record": None, "status": "no_coverage", "reason": NO_COVERAGE_REASON,
                             "date": today.isoformat()})
            readings[doi] = reading
            pool_index[doi] = reading
        cache.write(
            {
                "schema": SNAPSHOT_SCHEMA,
                "key": snapshot_key(pending),
                "select": ENRICH_SELECT,
                "source": SNAPSHOT_SOURCE,
                "query_date": today.isoformat(),
                "queried_at": datetime.now().isoformat(timespec="seconds"),
                "records": records,
                "no_coverage": [{"doi": d, "reason": NO_COVERAGE_REASON} for d in no_cover],
            }
        )

    dates = sorted({r["date"] for r in readings.values() if r.get("date")})
    no_coverage = [{"doi": d, "reason": r["reason"]} for d, r in sorted(readings.items())
                   if r["status"] == "no_coverage"]
    return {"readings": readings, "dates": dates, "stats": stats, "no_coverage": no_coverage,
            "transport_errors": transport_errors}


def cited_outputs(snapshot: dict, today: date) -> dict[str, str]:
    """快照自身的两件产物（在段之前落盘）：被引快照与 OA 定位读数。

    `counts` 按本次实查的候选 DOI 集给（去重前的唯一 DOI 数≈终池，读数是终池的超集）；
    `batch_range` 记本轮实查／复用分布（运行读数，供筛选日志「批量范围」列）；
    `query_date`／`query_dates` 只取真正读到数据的日期（传输失败不配日期），
    一律为快照原始读数——复用不顺延、不写重跑日。
    """
    readings = snapshot["readings"]
    stats = snapshot["stats"]
    network_dois = stats["network_dois"]
    reused = len(readings) - network_dois
    return {
        CITED_NAME: dumps(
            {
                "snapshot": {
                    "query_date": snapshot["dates"][0] if snapshot["dates"] else None,
                    "query_dates": snapshot["dates"],
                    "source": SNAPSHOT_SOURCE,
                    "fields": ENRICH_SELECT,
                    "batch_range": f"{len(readings)} 条 DOI（本轮网络实查 {network_dois} 条／读回复用 {reused} 条），"
                                   f"按 {CHUNK_SIZE} 条一请求",
                    "cache": stats,
                    "no_coverage": snapshot["no_coverage"],
                    "transport_errors": snapshot["transport_errors"],
                    "note": SNAPSHOT_NOTE,
                },
                "counts": {d: (r.get("record") or {}).get("cited_by_count") for d, r in sorted(readings.items())},
            }
        ),
        ENRICH_NAME: dumps(
            {
                "records": {d: r.get("record") for d, r in sorted(readings.items())},
                "no_coverage": [item["doi"] for item in snapshot["no_coverage"]],
                "transport_errors": snapshot["transport_errors"],
                "cache": stats,
            }
        ),
    }


# ---------------------------------------------------------------- O-04 确定性段


def segment_key(pools_sha: str, records_sha: str, verdicts_sha: str, cur_year: int,
                snapshot: dict) -> tuple[str, dict]:
    """段键＝输入哈希（池＋记录＋裁定表＋脚本）＋权重版本＋年份＋快照内容与日期。

    快照内容指纹＝逐条读数的 {record, status, date}——覆盖「有读数／无覆盖／传输失败」三分与各自日期，
    段产物写进报告的读数（无覆盖条数、传输失败条数、查询日期）因此全部落在键内。
    队列口径（目标 N／上限）不进段键：选取集由 `step2_select.py` 按 sort-report 行集现算，不属本段产物。
    """
    content = {doi: {"record": r.get("record"), "status": r.get("status"), "date": r.get("date")}
               for doi, r in sorted(snapshot["readings"].items())}
    components = {
        "schema": SEGMENT_SCHEMA,
        "pools_sha256": pools_sha,
        "records_sha256": records_sha,
        "verdicts_sha256": verdicts_sha,
        "script_sha256": SCRIPT_SHA256,
        "weight_version": WEIGHT_VERSION,
        "weights": dict(WEIGHTS),
        "cur_year": cur_year,
        "snapshot_dates": snapshot["dates"],
        "snapshot_sha256": sha256_text(canonical_json(content)),
    }
    return sha256_text(canonical_json(components)), components


def verify_segment(outputs: dict, verdicts: dict) -> list[str]:
    """段产物验算：计数与明细交叉核对（守恒式）＋裁定逐条有当前依据、疑似不代填。

    守恒式不是数字自洽：`merged_count` 对 `len(removed)`、`suspected_count` 对 `len(suspected)`、
    终池 `len(final_records)` 对 R3 `kept_count` 三处交叉核对，改小计数不改明细即被拦。
    裁定三分：裁定表（人工位）／规则已判异篇（`decided_by: rule`，规则原文）／未裁定（照实列空）。
    """
    try:
        report = json.loads(outputs["dedupe_report"])
        decisions_doc = json.loads(outputs["dedupe_decisions"])
        table = json.loads(outputs["ingest_abstracts"])
    except (KeyError, json.JSONDecodeError) as exc:
        return [f"段产物不可解析：{exc}"]
    problems: list[str] = []
    rounds = report.get("rounds") or []
    if [str(r.get("round", ""))[:2] for r in rounds] != ["R1", "R2", "R3"]:
        problems.append("轮次不齐（需 R1／R2／R3）")
    for rnd in rounds:
        if rnd.get("input_count") != (rnd.get("kept_count") or 0) + (rnd.get("merged_count") or 0):
            problems.append(f"{rnd.get('round')} 计数不守恒：{rnd.get('input_count')} ≠ "
                            f"{rnd.get('kept_count')}＋{rnd.get('merged_count')}")
        if rnd.get("merged_count") != len(rnd.get("removed") or []):
            problems.append(f"{rnd.get('round')} 合并计数与合并明细不符："
                            f"{rnd.get('merged_count')} ≠ {len(rnd.get('removed') or [])}")
        if rnd.get("suspected_count") != len(rnd.get("suspected") or []):
            problems.append(f"{rnd.get('round')} 疑似计数与疑似列表不符")
    if rounds and len(report.get("final_records") or []) != rounds[-1].get("kept_count"):
        problems.append(f"终池明细与 R3 保留数不符：{len(report.get('final_records') or [])} ≠ "
                        f"{rounds[-1].get('kept_count')}")
    entries, missing = table.get("entries"), table.get("missing")
    coverage = table.get("coverage") if isinstance(table.get("coverage"), dict) else {}
    if not isinstance(entries, dict) or not isinstance(missing, list):
        problems.append("入库摘要表形态不符（缺 entries 映射或 missing 清单）")
    else:
        doi_records = [r for r in (report.get("final_records") or []) if (r or {}).get("doi")]
        unique_dois = {r["doi"] for r in doi_records}
        if coverage.get("denominator") != len(doi_records):
            problems.append(f"入库摘要表分母与终池标 DOI 记录数不符：{coverage.get('denominator')} ≠ "
                            f"{len(doi_records)}")
        if coverage.get("with_abstract") != len(entries):
            problems.append(f"入库摘要表覆盖计数与 entries 不符：{coverage.get('with_abstract')} ≠ "
                            f"{len(entries)}")
        if len(entries) + len(missing) != len(unique_dois):
            problems.append(f"入库摘要表 entries＋missing 与终池唯一 DOI 数不符："
                            f"{len(entries)}＋{len(missing)} ≠ {len(unique_dois)}")
        if report.get("coverage") != coverage:
            problems.append("去重报告的覆盖率字段与入库摘要表不一致")
    if decisions_doc.get("decisions_unused"):
        problems.append("有未使用的裁定（pair 须与疑似条目逐字对应，取 dedupe-decisions.json 的 "
                        f"unadjudicated[].pair）：{decisions_doc['decisions_unused'][:2]}")
    table = verdicts.get("decisions") or {}
    unadjudicated = {pair_tuple(u.get("pair")) for u in decisions_doc.get("unadjudicated") or []}
    for decision in decisions_doc.get("decisions") or []:
        key = pair_tuple(decision.get("pair"))
        item = table.get(key)
        if item is not None:
            if item["verdict"] != decision.get("verdict") or item["reason"] != (decision.get("reason") or "").strip():
                problems.append(f"裁定与裁定表不一致：{list(key)}")
            elif (decision.get("decided_by"), decision.get("decided_at")) != (verdicts.get("decided_by"),
                                                                             verdicts.get("decided_at")):
                problems.append(f"裁定的人工位字段与裁定表不一致：{list(key)}")
        elif decision.get("decided_by") != "rule":
            problems.append(f"裁定既无当前裁定表依据也非规则判定：{list(key)}")
    for rnd in rounds:
        for s in rnd.get("suspected") or []:
            key = pair_tuple(s.get("pair"))
            decision = (s.get("decision") or "").strip()
            item = table.get(key)
            if item is not None:  # 人工裁定优先
                if decision != f"{item['verdict']}（{item['reason']}）":
                    problems.append(f"疑似条目裁定字段与裁定表不符：{list(key)}")
            elif s.get("rule_verdict"):  # 规则已判：只能是规则原文渲染
                if decision != f"{s['rule_verdict']}（{s.get('rule_note', '')}）":
                    problems.append(f"规则判定条目被改写：{list(key)}")
            elif key in unadjudicated:
                if decision:
                    problems.append(f"未裁定条目被代填裁定：{list(key)}")
            else:
                problems.append(f"疑似条目既无裁定也未列未裁定：{list(key)}")
    return problems


class SegmentCache:
    """O-04 段缓存：内容寻址、无 TTL；一格一文件，写完即原子替换。"""

    def __init__(self, root: Path, enabled: bool, refresh: bool):
        self.dir = root / "segment"
        self.read_enabled = enabled and not refresh
        self.write_enabled = enabled

    def read(self, key: str) -> dict | None:
        if not self.read_enabled:
            return None
        data = read_json(self.dir / f"{key}.json")
        if not isinstance(data, dict) or data.get("schema") != SEGMENT_SCHEMA or data.get("key") != key:
            return None
        if not isinstance(data.get("outputs"), dict):
            return None
        return data

    def write(self, key: str, payload: dict) -> None:
        if not self.write_enabled:
            return
        write_atomic(self.dir / f"{key}.json", dumps(payload))


def segment_outputs(rounds: list[dict], final: list[dict], pool_records: list[dict], identity_rejected: list[dict],
                    params: dict, args, verdicts: dict,
                    inputs: dict[str, str]) -> tuple[dict[str, str], dict]:
    """确定性段产物：去重报告、裁定表结果、排序报告、入库摘要表。

    四桶报告／台账骨架／队列（＝研读选取集）由选取步 `step2_select.py` 定稿（ADR-0032）；
    本段只到排序行集为止，行集口径（N／必取判定）因此不进段键。
    """
    decisions_doc = render_decisions(rounds, verdicts)
    abstracts_text, coverage, missing_count = ingest_abstracts(final, pool_records, params["frozen_at"], inputs)
    rows = build_rows(final, params, args.cur_year)
    outputs = {
        "dedupe_report": dumps(
            {
                "rounds": [{k: v for k, v in r.items() if k != "kept"} for r in rounds],
                "decisions_unused": decisions_doc["decisions_unused"],
                "identity_rejected": identity_rejected,
                "final_pool_has_identityless": any(not c["doi"] and not three_keys(c)[2] for c in final),
                "final_count": len(final),
                "coverage": coverage,
                "final_records": [
                    {
                        "doi": c["doi"], "title": c.get("title"), "year": c.get("year"),
                        "sources": c["sources"], "origin": c["origin"], "family": c["family"],
                        "pool_ids": c["pool_ids"],
                    }
                    for c in final
                ],
            }
        ),
        "dedupe_decisions": dumps(decisions_doc),
        "sort_report": dumps(
            {
                "frozen": True,
                "weight_version": WEIGHT_VERSION,
                "weights": dict(WEIGHTS),
                "formula": "S=(Σw·s)/(Σ已知维w)；未知维记 ∅ 不计分母；证据等级与设计不进排序",
                "rows": rows,
            }
        ),
        "ingest_abstracts": abstracts_text,
    }
    summary = {
        "final_count": len(final), "identity_rejected": len(identity_rejected),
        "rows_count": len(rows),
        "rounds": [{"round": r["round"], "input": r["input_count"], "kept": r["kept_count"],
                    "merged": r["merged_count"], "suspected": r["suspected_count"],
                    "suspected_ambiguous": r["suspected_ambiguous_count"]} for r in rounds],
        "unadjudicated": len(decisions_doc["unadjudicated"]),
        "coverage": coverage,
        "missing_count": missing_count,
    }
    return outputs, summary


# ---------------------------------------------------------------- 主流程


def input_label(delivery: Path, path: Path) -> str:
    """覆盖率登记的件名：交付内记相对交付的路径（POSIX 斜杠，跨平台可比），交付外记原路径。"""
    try:
        return path.resolve().relative_to(delivery.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Step2 分类分桶：去重＋排序＋分桶备料（含被引快照复用与确定性段短路）；"
                    "研读选取集与四桶报告由 step2_select.py 定稿（ADR-0032）")
    ap.add_argument("--delivery", required=True, help="交付目录（workspace/YYYY-MM-DD-<slug>）")
    ap.add_argument("--cur-year", type=int, default=CUR_YEAR_DEFAULT, help=f"排序年份口径（默认 {CUR_YEAR_DEFAULT}）；进段键")
    ap.add_argument("--verdicts", help=f"疑似裁定表（默认 <交付>/{VERDICTS_NAME}；缺省＝无裁定，疑似照实列未裁定）")
    ap.add_argument("--cache-dir", help=f"缓存根目录（默认 {DEFAULT_CACHE_DIR}；须为被忽略的非交付路径）")
    ap.add_argument("--refresh", action="store_true", help="显式重跑：越缓存重查快照、重算确定性段（新读数照常写回缓存）")
    ap.add_argument("--no-cache", action="store_true", help="全程不读不写缓存（探针与排障用）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Step2 错误：{exc}", file=sys.stderr)
        return 2


def run(args) -> int:
    delivery = Path(args.delivery)
    if not delivery.is_dir():
        raise ValueError(f"交付目录不存在：{delivery}")
    pools_path, records_path = delivery / POOLS_NAME, delivery / RECORDS_NAME
    verdicts_path = Path(args.verdicts) if args.verdicts else delivery / VERDICTS_NAME
    if not pools_path.is_file():
        raise ValueError(f"发现清单不存在：{pools_path}")
    cache_root = Path(args.cache_dir) if args.cache_dir else DEFAULT_CACHE_DIR
    if cache_root.resolve() == delivery.resolve() or delivery.resolve() in cache_root.resolve().parents:
        raise ValueError(f"缓存目录不得落在交付内（缓存不是交付件）：{cache_root}")

    discovery, params = load_pools(pools_path)
    if not params["frozen_at"]:
        # 池采集时刻是入库摘要表 `collected_at` 的唯一真源：缺了不编造、不用重跑日顶替，当输入错误拒产出
        raise ValueError(f"发现清单缺 frozen_at（池采集时刻，入库摘要表的 collected_at）：{pools_path}")
    if records_path.is_file():
        chinese = load_chinese(records_path)
    else:
        # B1（中文库手动补充）是条件分支：缺席即按空表记（R2 记 0／0／0；未增 B1 时筛选日志按轮空行记账）
        chinese = []
        print(f"提示：中文补充缺席（{records_path}），按空表记（未增 B1）", file=sys.stderr)
    verdicts = load_verdicts(verdicts_path)
    if not params["pico_groups"]:
        print("提示：发现清单未给 pico_groups，相关维 s_rel 记 ∅（未知保持未知，不编造）", file=sys.stderr)

    # ---- O-03 被引／OA 定位快照：先集合键命中，再重叠 DOI，最后联网实查
    dois = sorted({norm_doi(c["doi"]) for c in discovery + chinese if norm_doi(c["doi"])})
    snapshot_cache = SnapshotCache(cache_root, enabled=not args.no_cache, refresh=args.refresh)
    snapshot = resolve_snapshot(dois, snapshot_cache, date.today())

    # ---- O-04 确定性段：键一致即整段短路（去重／排序／分桶备料／入库摘要表都不重算）
    pools_sha = sha256_bytes(pools_path.read_bytes())
    records_sha = sha256_bytes(records_path.read_bytes()) if records_path.is_file() else "absent"
    coverage_inputs = {
        input_label(delivery, pools_path): pools_sha,
        input_label(delivery, records_path): records_sha,
        input_label(delivery, verdicts["path"]): verdicts["sha256"],
    }
    cache_key, components = segment_key(
        pools_sha, records_sha,
        verdicts["sha256"], args.cur_year, snapshot,
    )
    segment_cache = SegmentCache(cache_root, enabled=not args.no_cache, refresh=args.refresh)
    cached = segment_cache.read(cache_key)
    hit = False
    if cached is not None:
        problems = verify_segment(cached["outputs"], verdicts)
        if problems:
            print("段缓存验算不过，改为重算：" + "；".join(problems[:3]), file=sys.stderr)
        else:
            hit = True
    if hit:
        outputs, summary = cached["outputs"], cached.get("summary") or {}
    else:
        # 快照读数回填进候选记录（排序与分桶都要用）
        rounds, final, identity_rejected = _dedupe(discovery, chinese, verdicts)
        for c in final:
            reading = snapshot["readings"].get(c["doi"]) or {}
            e = reading.get("record") or {}
            c["cited_by_count"] = e.get("cited_by_count")
            c["language"] = e.get("language")
            c["oa"] = {
                "is_oa": e.get("is_oa"), "oa_status": e.get("oa_status"), "oa_url": e.get("oa_url"),
                "best_pdf_url": e.get("best_pdf_url"), "best_source": e.get("best_source"),
                "best_source_type": e.get("best_source_type"), "best_is_repository": e.get("best_is_repository"),
            }
        outputs, summary = segment_outputs(rounds, final, discovery + chinese, identity_rejected, params, args,
                                           verdicts, coverage_inputs)
        problems = verify_segment(outputs, verdicts)
        if problems:
            raise ValueError("段产物验算不过（不落盘、不写缓存）：" + "；".join(problems[:3]))
        segment_cache.write(cache_key, {
            "schema": SEGMENT_SCHEMA, "key": cache_key, "components": components,
            "computed_at": datetime.now().isoformat(timespec="seconds"),
            "outputs": outputs, "summary": summary,
        })

    # 段产物落盘（逻辑名 → 交付内落点；缓存按逻辑名存文本）
    changed = [name for name, text in outputs.items() if write_if_changed(delivery / SEGMENT_OUTPUTS[name], text)]
    # 快照自身的两件产物（被引查询日期为原始读数，不写重跑日）
    cited = cited_outputs(snapshot, date.today())
    cited_changed = [name for name, text in cited.items() if write_if_changed(delivery / name, text)]
    # 版内首写件（已在位一律不覆盖）：身份列表（终池全量 DOI）；台账骨架与队列由选取步定稿
    seeded = []
    if write_if_absent(delivery / DOI_LIST_NAME, doi_list_of(outputs)):
        seeded.append(DOI_LIST_NAME)

    rounds_summary = summary.get("rounds") or []
    summary_coverage = summary.get("coverage") or {}
    print(
        "段键 %s（%s；段产物改动 %d 件：%s）\n%s\n终池 %s；无身份剔除 %s；排序行集 %s；未裁定疑似 %s\n"
        "入库摘要表覆盖 %s/%s（无摘要 %s 条；类型未知 %s 条）\n"
        "快照查询日期 %s（集合键命中 %s 块／重叠复用 %s 条／网络实查 %s 块 %s 条）；无覆盖 %s 条；传输失败未读 %s 条\n"
        "快照产物改动 %d 件：%s｜版内首写 %s\n"
        "下一步：选取步 `step2_select.py plan／apply` 定稿研读选取集（＝下载队列＝入库集，ADR-0032）；"
        "P1 路线用 `pool-ledger` 以全池行集产四桶报告与台账骨架"
        % (
            cache_key[:12], "缓存命中：整段短路" if hit else "重算", len(changed), "、".join(changed) or "无",
            "\n".join(f"{r['round']} {r['input']}→{r['kept']}（合并{r['merged']}／疑似{r['suspected']}"
                      f"（待裁定{r.get('suspected_ambiguous', '?')}））" for r in rounds_summary),
            summary.get("final_count"), summary.get("identity_rejected"), summary.get("rows_count"),
            summary.get("unadjudicated"),
            summary_coverage.get("with_abstract"), summary_coverage.get("denominator"),
            summary.get("missing_count"), summary_coverage.get("unknown_type"),
            "／".join(snapshot["dates"]) or "—", snapshot["stats"]["exact_hits"], snapshot["stats"]["overlap_dois"],
            snapshot["stats"]["network_chunks"], snapshot["stats"]["network_dois"],
            len(snapshot["no_coverage"]),
            sum(1 for r in snapshot["readings"].values() if r.get("status") == "transport_error"),
            len(cited_changed), "、".join(cited_changed) or "无",
            "、".join(seeded) or "无",
        )
    )
    if not hit and summary.get("unadjudicated"):
        print("⚠ 有疑似条目待人工裁定：照实列进 dedupe-decisions.json 的 unadjudicated（人工位不代填；"
              "规则已判异篇的对不占人工位）", file=sys.stderr)
    return 0


def doi_list_of(outputs: dict) -> str:
    """身份列表（终池全量 DOI，一行一条）：含未进研读选取集者，与队列件契约分离（workspace-guide §6）。

    与队列件的差别即契约差别：队列是研读选取集（Step3 输入、可被剪、与下载台账行数对齐），
    本件是终池身份清单，不随剪队列改写。终池在「DOI 同、标题异」未裁定时可含同 DOI
    两条记录，故落行前按 DOI 去重（与队列构建同口径）。
    """
    records = json.loads(outputs["dedupe_report"]).get("final_records") or []
    seen: set[str] = set()
    lines: list[str] = []
    for record in records:
        doi = record.get("doi")
        if not doi or doi in seen:
            continue
        seen.add(doi)
        lines.append(f"{doi}\n")
    return "".join(lines)


def caliber_of(delivery: Path) -> str:
    """交付口径版本：读 agent/manifest.json 的 caliber.grey_gate_version，缺省现行口径（与 step3_queue 同口径）。"""
    data = read_json(delivery / MANIFEST_NAME)
    if isinstance(data, dict):
        value = ((data.get("caliber") or {}).get("grey_gate_version") or "").strip()
        if value:
            return value
    return "post-0006-fastest"


def _dedupe(discovery: list[dict], chinese: list[dict], verdicts: dict) -> tuple[list[dict], list[dict], list[dict]]:
    r1 = dedupe_round(discovery, "R1 发现清单内部", verdicts)
    r2 = dedupe_round(chinese, "R2 中文手动补充内部", verdicts)
    r3 = dedupe_round(r1["kept"] + r2["kept"], "R3 两表合并", verdicts)
    identity_rejected = r1["identity_rejected"] + r2["identity_rejected"] + r3["identity_rejected"]
    return [r1, r2, r3], r3["kept"], identity_rejected


if __name__ == "__main__":
    raise SystemExit(main())
