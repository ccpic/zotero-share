# -*- coding: utf-8 -*-
"""引文扩展轮执行器：直连 OpenAlex 与 Europe PMC，单轮 1-hop 引文追索并并池。

When: Step2 首轮排序完成后、规模确认之前（ADR-0019「扩展先于规模确认」）；单轮 1-hop 不迭代
      （二阶缺口归「迭代补漏」机制）。零改 jadense 源码、不经 MCP、每轮实查（本轮不设缓存位）。

Do: uv run python .claude/skills/med-lit-review/scripts/citation_expand.py \
        --delivery workspace/YYYY-MM-DD-<slug> [--seed-limit 20] [--budget 300] \
        [--fwd-cap 100] [--bwd-cap 50] [--cur-year 2026] [--no-filter] \
        [--openalex-base URL] [--epmc-base URL] [--s2-base URL]

读（交付内，只读）：`run/b1/sort-report.json`（`rows[].order` 1–20＝头部位次种子；终池不足 20 取全池并记
实际条数）、`run/b1/citation-seeds.json`（agent 产判定集，可缺；每条 `{doi, reason, evidence}`，脚本只
校验形态（字段齐、DOI 归一、理由非空）不代判）、`run/b1/step1-pools.json`（并池目标，顶层原键不动）。

写（交付内，`run/b1/` 忽略位）：`citation-expansion.json`（逐通道读数：请求数／命中数／截断／失败原因／
origin／stop_reason＋`filter_policy`、`filtered[]` 与 `abstract_rebuild`（摘要重建读数））、
`raw/citation-<通道>-<slug>.json`（通道级原始
响应）；并把扩展记录以池形态追加进
`step1-pools.json`（`pool_kind=引文扩展`、`pool_id` `cite-bwd-<slug>`／`cite-fwd-<slug>`、
`origin=citation_bwd|citation_fwd`、`family=引文扩展·后向|前向`、`window=引文邻域`）。零记录不建池；
重跑幂等——先撤同 `pool_kind` 的旧池再追加本轮。

源组合与取数形态（票 02 决议）：后向＝OpenAlex 主＋EPMC 证；前向＝OpenAlex＋EPMC；S2 前向为条件补通道
（仅当两源前向对同一种子双空读时补一次，任何时候不影响该轮结果；请求字段表不含 `references.authors.name`）。
OpenAlex 种子记录一跳拿 `referenced_works`＋`cited_by_count`＋`ids.pmid`；后向题录按 `filter=openalex_id:`
OR 批量（≤50 id／请求）；前向 `filter=cites:W…` 分页（只认 work id：`cites:doi:` 静默 0、`cites:pmid:` 400）；
EPMC 以 PMID 走 `/MED/{pmid}/references` 与 `/citations`（取 `hitCount` 与首页条目）。

请求字段表由 `SELECT_SEED`／`SELECT_WORK` 给定：题录表含 `abstract_inverted_index`（**同批响应多取一
字段，摘要由此还原，零额外请求**，见下「摘要重建」）；EPMC 的 `abstractText` 与 S2 的 `abstract` 由
各自通道原样给，不走重建。

读数三分（每通道）：`hits`＝通道取到的条目数、`pooled`＝该通道**可入库题录数（过滤前）**、`dropped_no_doi`＝
无标识丢弃数。通道级 `pooled` 先于方向桶裁窗与摄入过滤定值（两件都发生在并池前），故它不等于实际入池数；
实际入池以种子级 `pooled` 与读数件 `pools[].records` 为准。EPMC 的
references／citations 条目基本不给 DOI（实测 1480 条中 2 条），而无标识学术搜索记录按 Step2 一级规则禁入
持久化（入池只会变成 `identity_rejected` 噪声并虚增新增读数），故 EPMC 定位为「证」通道：只记读数，入池
只收 DOI 可归一者；新增题录读数（去重前／后、入池新增）只统计真正入池的记录。

摄入过滤（ADR-0024，默认开启，`--no-filter` 可关）：引文扩展记录入池前按被引下限裁决——**通过＝被引
≥100 或 发表年份 ≥ 参考年−3（含当年）**；被引读数缺失一律不入池（fail-closed，全通道含 EPMC），年份缺失
按「新」走豁免档。只作用于引文扩展池（检索池与追加轮池零改动）；裁决点在**上限窗之内**——上限仍按取回
条数裁（口径不变、不补取），被滤条目另计 `filtered`（doi｜title｜cited｜year｜direction｜channel），
**不得读成缺席或截断**。参考年份走显式 `--cur-year`（默认 2026，不用系统当年：跨年重跑读数才可比）；
口径随 `filter_policy`（版本＋阈值＋豁免窗口＋范围＋开关态）落盘，关闭时记「未过滤」不记 0。

摘要重建（ADR-0024 配套票 02，恒开、无开关）：OpenAlex 题录的正文从**同批响应**的
`abstract_inverted_index` 按 position 还原（`SELECT_WORK` 多取一字段，**零额外请求**）；EPMC／S2 的正文
由通道字段原样保留、不重建（通道未给即原位空串，无成因键）。索引形态出乎意料（非对象／空词表／空或非
列表的位置表／非整数位置／同位置两词／位置不连续）一律记 ∅ 并把原因按码写进记录 `abstractReason`
（`{class, message}`，与 `classify_http` 同形）——**不猜、不崩、不拿空串冒充**；不可还原者就记 ∅，未知维
语义不变（`comp`／`s_rel` 对空正文的判定照旧）。读数件 `abstract_rebuild` 把该批扩展记录分「可重建／
通道原样／无摘要」三列计数，无摘要按成因码计数（`missing_reasons`）。

判停与纪律：上限按方向执行——每种子每方向 前向 ≤100／后向 ≤50（`openalex-bwd` 的批量取数另受 ≤50 id／
请求夹取），合并方向桶时再按方向上限二次截取（主通道优先：OpenAlex→EPMC→S2）并记「截断」；整轮 ≤300 请求含重试，逼近即停
未跑的种子／方向并记「预算耗尽」；节流 EPMC ≈1 req/s、OpenAlex 串行（0.1 s 间隔）、S2 补通道 ≥1 s（EPMC 每种子每方向 1 请求、S2 仅双空读时
1 次，两者都计入 300 预算）；
单格瞬时错误（传输失败／5xx／429）退避重试一次（base 800 ms 含 jitter），400 与确定性阴性不重试，
仍失败记 ∅ 带原因；方向级／种子级失败隔离；连续 3 个种子各通道皆瞬时失败＝全通道不可用，中止该轮且不并池。

留痕：末尾打印可直接入筛选日志的机制行（种子／方向／请求／新增题录／摄入过滤／截断与失败／判停）与逐条
缺口行（含被滤条目按通道分列的一行——被滤只记「另计」，**不写成缺席、不写成截断**）；
词表生长循环触发点含本轮——词表范畴词（药物名／研发代号／疾病名／别名）当场
`uv run python scripts/term_expand.py "<词>" --append --source "引文扩展轮"` 落候选缓冲，
试验缩写与指南名不落缓冲、记筛选日志实体清单。

退出码：0 正常（零新增与部分失败如实进账，不算失败）；2 用法、输入文件或形态错误。
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import random
import re
import socket
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from step3_queue import norm_doi, read_json  # noqa: E402 - 同族脚本跨件复用（DOI 归一与容错读 JSON 同口径）

B1_DIR = "run/b1"
SORT_NAME = f"{B1_DIR}/sort-report.json"
SEEDS_NAME = f"{B1_DIR}/citation-seeds.json"
POOLS_NAME = f"{B1_DIR}/step1-pools.json"
EXPANSION_NAME = f"{B1_DIR}/citation-expansion.json"
RAW_DIR = "raw"

DEFAULT_OPENALEX_BASE = "https://api.openalex.org"
DEFAULT_EPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
DEFAULT_S2_BASE = "https://api.semanticscholar.org/graph/v1"
USER_AGENT = "med-lit-review-citation-expand/1.0"

HEAD_SEEDS = 20
REQ_BUDGET = 300
FWD_CAP = 100
BWD_CAP = 50
ID_BATCH = 50          # OpenAlex `filter=openalex_id:` OR 批量上限（实测 50 id／请求通过）
PAGE_SIZE = 50         # OpenAlex 前向分页每页条数
HTTP_TIMEOUT = 30.0
RETRY_BASE = 0.8       # 退避基准（秒）：800–1600 ms 含 jitter
MAX_ATTEMPTS = 2       # 单格一次重试
ABORT_AFTER_FAILED_SEEDS = 3

THROTTLE_SECONDS = {"openalex": 0.1, "epmc": 1.0, "s2": 1.0}
TRANSIENT_CLASSES = ("unavailable", "quota_exhausted")

ABSTRACT_INDEX_FIELD = "abstract_inverted_index"   # OpenAlex 摘要倒排索引：正文本就同一批响应给出，只多取一字段
SELECT_SEED = "id,ids,doi,display_name,publication_year,publication_date,type,cited_by_count,referenced_works"
SELECT_WORK = ("id,ids,doi,display_name,publication_year,publication_date,type,cited_by_count,"
               f"primary_location,authorships,{ABSTRACT_INDEX_FIELD}")
S2_FIELDS = "title,externalIds,year,venue,authors,publicationDate,publicationTypes,abstract"

FILTER_VERSION = "cite-ingest-filter-v1"   # 摄入过滤口径版本（随读数落盘，跨交付可比）
CUR_YEAR_DEFAULT = 2026     # 参考年份：显式口径，不用 `date.today().year`（跨年重跑读数才可比；换年须显式传参）
CITED_FLOOR = 100           # 被引下限
EXEMPT_YEARS = 3            # 分龄豁免窗口（含当年）
FILTER_RULE = (f"通过＝被引 ≥{CITED_FLOOR} 或 发表年份 ≥ 参考年−{EXEMPT_YEARS}（含当年）；"
               f"被引读数缺失一律不入池（fail-closed，全通道含 EPMC）；年份缺失按「新」走豁免档")

POOL_KIND = "引文扩展"
POOL_WINDOW = "引文邻域"
POOL_FAMILY = {"bwd": "引文扩展·后向", "fwd": "引文扩展·前向"}
ORIGIN = {"bwd": "citation_bwd", "fwd": "citation_fwd"}
POOL_PREFIX = {"bwd": "cite-bwd", "fwd": "cite-fwd"}
POOL_QUERY = {"bwd": "引文扩展（{doi} 的参考文献）", "fwd": "引文扩展（引用 {doi} 的文献）"}

DIRECTION_ZERO = {"available": None, "retrieved": 0, "pooled": 0, "filtered": 0, "truncated": False,
                  "channel_truncated": False}

STOP_DONE = "种子集跑完"
STOP_BUDGET = "预算耗尽"
STOP_ABORT = "全通道不可用（中止该轮）"

# 每种子计划通道：（通道名，方向）——未跑也逐格出行，读数不留空档
PLANNED_CHANNELS = (
    ("openalex-seed", "seed"),
    ("openalex-bwd", "bwd"),
    ("epmc-bwd", "bwd"),
    ("openalex-fwd", "fwd"),
    ("epmc-fwd", "fwd"),
    ("s2-fwd", "fwd"),
)
S2_TRIGGER_CHANNELS = ("openalex-fwd", "epmc-fwd")
TITLE_NOISE_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def quote_doi(doi: str) -> str:
    """DOI 进 OpenAlex 路径段：保留 DOI 自身结构字符，只转义会破坏 URL 的字符。"""
    return urllib.parse.quote(doi, safe="/:().-_~;+=@,")


def last_segment(url: str | None) -> str | None:
    text = (url or "").rstrip("/")
    if not text:
        return None
    return text.rsplit("/", 1)[-1] or None


def slug_for(doi: str) -> str:
    """种子 slug（池 id 与原始响应文件名共用）：可读形式，超长补哈希保唯一。"""
    base = re.sub(r"[^a-z0-9]+", "-", (doi or "").lower()).strip("-") or "seed"
    if len(base) <= 48:
        return base
    return f"{base[:40].rstrip('-')}-{hashlib.sha1(doi.encode('utf-8')).hexdigest()[:8]}"


def record_key(record: dict) -> str:
    """本轮读数用的题录身份键（DOI 一级、标题＋年二级）；正式去重归 Step2 R1。"""
    doi = norm_doi(record.get("doi"))
    if doi:
        return doi
    title = TITLE_NOISE_RE.sub("", unicodedata.normalize("NFKC", record.get("title") or "").lower())
    return f"{title}|{record.get('year') if record.get('year') is not None else ''}" if title else ""


# ---------------------------------------------------------------- 取数层


class BudgetExhausted(RuntimeError):
    """预算耗尽：调用方据此收手并记「预算耗尽」。"""


class Budget:
    """整轮请求预算（含重试）：逐次尝试先占位，逼近上限即停，不越预算发请求。"""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def charge(self) -> None:
        if self.used >= self.limit:
            raise BudgetExhausted()
        self.used += 1


class Throttle:
    """源级节流：EPMC ≈1 req/s、OpenAlex 串行（0.1 s 间隔防突发）、S2 补通道 ≥1 s。"""

    def __init__(self) -> None:
        self.last: dict[str, float] = {}

    def wait(self, source: str) -> None:
        gap = THROTTLE_SECONDS.get(source, 0.0)
        previous = self.last.get(source)
        if previous is not None:
            rest = gap - (time.monotonic() - previous)
            if rest > 0:
                time.sleep(rest)
        self.last[source] = time.monotonic()


def classify_http(status: int | None) -> dict:
    """错误类口径沿票 06：只对可瞬时恢复的类重试。"""
    if status == 429:
        return {"class": "quota_exhausted", "message": "HTTP 429（限流）", "http_status": status}
    if status is not None and 500 <= status < 600:
        return {"class": "unavailable", "message": f"HTTP {status}（服务端不可用）", "http_status": status}
    if status == 400:
        return {"class": "invalid_request", "message": "HTTP 400（请求非法，不重试）", "http_status": status}
    if status == 404:
        return {"class": "not_found", "message": "HTTP 404（无此记录）", "http_status": status}
    return {"class": f"http_{status}", "message": f"HTTP {status}", "http_status": status}


class HttpClient:
    """直连 JSON GET：逐次计入预算、源级节流、瞬时错误（传输失败／5xx／429）退避重试一次。"""

    def __init__(self, budget: Budget, throttle: Throttle) -> None:
        self.budget = budget
        self.throttle = throttle

    def get_json(self, source: str, url: str) -> dict:
        entry = {"url": url, "status": None, "attempts": 0, "retried": False,
                 "recovered": False, "error": None, "payload": None}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.budget.charge()
            self.throttle.wait(source)
            entry["attempts"] = attempt
            status, body, transport = self._once(url)
            entry["status"] = status
            if transport is None and status is not None and 200 <= status < 300:
                try:
                    entry["payload"] = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as err:
                    entry["error"] = {"class": "malformed_response", "message": f"响应非 JSON：{err}",
                                      "http_status": status}
                    return entry
                entry["error"] = None      # 重试成功即无失败态（首次尝试的错误不留在格上）
                entry["recovered"] = attempt > 1
                return entry
            failure = transport or classify_http(status)
            entry["error"] = failure
            if failure["class"] not in TRANSIENT_CLASSES or attempt == MAX_ATTEMPTS:
                return entry
            entry["retried"] = True
            time.sleep(RETRY_BASE * random.uniform(1.0, 2.0))
        return entry

    @staticmethod
    def _once(url: str) -> tuple[int | None, bytes, dict | None]:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return response.status, response.read(), None
        except urllib.error.HTTPError as err:
            try:
                body = err.read()
            except OSError:
                body = b""
            return err.code, body, None
        except (urllib.error.URLError, socket.timeout, OSError) as err:
            return None, b"", {"class": "unavailable", "message": f"传输失败：{type(err).__name__}: {err}",
                               "http_status": None}
        except http.client.HTTPException as err:
            # 协议层异常不止 OSError 一族：响应体截断（`IncompleteRead`）与状态行非法（`BadStatusLine`）都由
            # 服务端瞬时故障引起，按传输失败走重试；其余子类由请求构造／应用状态决定（如 `InvalidURL`），
            # 只记失败不重试。两类都不许冒泡——`guarded` 只隔离形态类异常，冒泡即单格异常中止整轮。
            transient = isinstance(err, (http.client.IncompleteRead, http.client.BadStatusLine))
            return None, b"", {"class": "unavailable" if transient else "protocol_error",
                               "message": f"传输失败：{type(err).__name__}: {err}", "http_status": None}


def failure_of(entries: list[dict]) -> dict | None:
    for entry in entries:
        error = entry.get("error")
        if error:
            return {"class": error["class"], "message": error["message"],
                    "attempts": entry.get("attempts"), "retried": bool(entry.get("retried"))}
    return None


# ---------------------------------------------------------------- 题录映射


def rebuild_abstract(index) -> tuple[str | None, dict | None]:
    """`abstract_inverted_index`（{词: [位置…]}）按 position 还原正文；不可还原记 ∅ 带原因。

    索引形态由源侧决定，任一处出乎意料都**不猜**：宁可记 ∅（未知保持未知），也不拿半截词表冒充正文、
    更不抛栈——单条畸形不许让整格（乃至整轮）读不出数。位置是一维的：位置齐备且恰为 0…n−1（无重号、
    无缺号）才拼，词序即位置升序。

    原因与 `classify_http`／`failure_of` 同形：`{"class": 成因码, "message": 人读详情}`。**分成因码是给
    聚合用的**——读数件按码计数才不会被违规词／位置值切成一条条，也才不把正文词元带进账；详情仍逐条留在
    记录上，复盘「这条为什么没有摘要」不必再翻原始响应。成因码：`field_absent`／`not_object`／
    `empty_index`／`bad_word_key`／`slots_not_list`／`empty_slots`／`position_not_int`／
    `position_duplicated`／`position_gap`。
    """
    if index is None:
        return None, {"class": "field_absent", "message": "字段缺席（源侧未给或为 null）"}
    if not isinstance(index, dict):
        return None, {"class": "not_object", "message": f"索引不是对象：{type(index).__name__}"}
    if not index:
        return None, {"class": "empty_index", "message": "词表为空"}
    positions: dict[int, str] = {}
    for word, slots in index.items():
        if not isinstance(word, str) or not word:
            return None, {"class": "bad_word_key", "message": f"词表键不是非空字符串：{word!r}"}
        if not isinstance(slots, list):
            return None, {"class": "slots_not_list",
                          "message": f"词「{word}」的位置表不是列表：{type(slots).__name__}"}
        if not slots:
            return None, {"class": "empty_slots", "message": f"词「{word}」的位置表为空"}
        for slot in slots:
            if not isinstance(slot, int) or isinstance(slot, bool):
                return None, {"class": "position_not_int",
                              "message": f"词「{word}」的位置不是整数：{slot!r}"}
            if slot in positions:
                return None, {"class": "position_duplicated",
                              "message": f"位置 {slot} 有两个词（「{positions[slot]}」与「{word}」）"}
            positions[slot] = word
    if sorted(positions) != list(range(len(positions))):
        return None, {"class": "position_gap", "message": "位置不连续（缺 position，词序无从还原）"}
    return " ".join(positions[slot] for slot in range(len(positions))), None


def openalex_record(work: dict) -> dict:
    location = work.get("primary_location") if isinstance(work.get("primary_location"), dict) else {}
    source = location.get("source") if isinstance(location.get("source"), dict) else {}
    authors = [((item.get("author") or {}).get("display_name") or "")
               for item in (work.get("authorships") or []) if isinstance(item, dict)]
    abstract, abstract_reason = rebuild_abstract(work.get(ABSTRACT_INDEX_FIELD))
    return {
        "doi": norm_doi(work.get("doi")),
        "title": work.get("display_name") or work.get("title"),
        "authors": [author for author in authors if author],
        "venue": source.get("display_name"),
        "year": work.get("publication_year"),
        "publishedDate": work.get("publication_date"),
        "type": work.get("type"),
        "abstract": abstract,
        "abstractReason": abstract_reason,
        "citationCount": work.get("cited_by_count"),
        "url": location.get("landing_page_url") or work.get("id"),
        "externalSource": "openalex",
        "externalId": last_segment(work.get("id")),
        "retrievalProvider": "openalex",
    }


def epmc_record(item: dict) -> dict:
    year = item.get("pubYear")
    identifier = item.get("id")
    source = item.get("source") or "MED"
    return {
        "doi": norm_doi(item.get("doi")),
        "title": item.get("title"),
        "authors": [author.strip() for author in (item.get("authorString") or "").split(",") if author.strip()],
        "venue": item.get("journalTitle") or item.get("journalAbbreviation"),
        "year": year if isinstance(year, int) else None,
        "publishedDate": item.get("firstPublicationDate"),
        "type": item.get("pubType"),
        "abstract": item.get("abstractText") or "",
        "citationCount": None,
        "url": f"https://europepmc.org/article/{source}/{identifier}" if identifier else None,
        "externalSource": "europepmc",
        "externalId": f"{source}:{identifier}" if identifier else None,
        "retrievalProvider": "europepmc",
    }


def s2_citing_record(item: dict) -> dict:
    paper = item.get("citingPaper")
    if not isinstance(paper, dict):
        raise ValueError("引文条目不带动引论文对象（citingPaper）")
    return s2_record(paper)


def s2_record(paper: dict) -> dict:
    ids = paper.get("externalIds") if isinstance(paper.get("externalIds"), dict) else {}
    pdf = paper.get("openAccessPdf") if isinstance(paper.get("openAccessPdf"), dict) else {}
    year = paper.get("year")
    paper_id = paper.get("paperId")
    return {
        "doi": norm_doi(ids.get("DOI")),
        "title": paper.get("title"),
        "authors": [item.get("name") for item in (paper.get("authors") or [])
                    if isinstance(item, dict) and item.get("name")],
        "venue": paper.get("venue"),
        "year": year if isinstance(year, int) else None,
        "publishedDate": paper.get("publicationDate"),
        "type": None,
        "abstract": paper.get("abstract") or "",
        "citationCount": None,
        "url": pdf.get("url") or (f"https://www.semanticscholar.org/paper/{paper_id}" if paper_id else None),
        "externalSource": "semanticscholar",
        "externalId": paper_id,
        "retrievalProvider": "semanticscholar",
    }


def payload_items(entry: dict, keys: tuple[str, ...]) -> list[dict]:
    """按路径取条目列表：缺键／`null`＝空读（确定性阴性）；键在而形态不是容器或列表＝形态异常。"""
    node = entry.get("payload")
    for index, key in enumerate(keys):
        if node is None:
            return []
        if not isinstance(node, dict):
            raise ValueError(f"响应路径 {'.'.join(keys[:index])} 不是对象：{type(node).__name__}")
        node = node.get(key)
    if node is None:
        return []
    if not isinstance(node, list):
        raise ValueError(f"响应路径 {'.'.join(keys)} 不是列表：{type(node).__name__}")
    return [item for item in node if isinstance(item, dict)]


CHANNEL_PARSE = {
    "openalex-bwd": (("results",), openalex_record),
    "epmc-bwd": (("referenceList", "reference"), epmc_record),
    "epmc-fwd": (("citationList", "citation"), epmc_record),
    "s2-fwd": (("data",), s2_citing_record),
}


def finish_channel(channel, direction, seed, ctx, entries, *, records=None, available=None,
                   truncated=False, failure=None) -> tuple[list[dict], dict]:
    """通道收尾：解析（或收下分页累计）→ 只留可入库题录 → 出行。

    读数三分：`hits`＝通道取到的条目数、`pooled`＝本通道可入库题录数（**过滤前**；方向桶裁窗与摄入过滤
    都在并池前发生，故该值不等于实际入池数）、`dropped_no_doi`＝无 DOI 丢弃数。
    EPMC 的 references／citations 条目基本不给 DOI（实测 1480 条中 2 条），而无标识学术搜索记录按
    Step2 一级规则禁入持久化——照收只会变成 identity_rejected 噪声并虚增新增读数，故只记读数不入池。
    """
    if records is None:
        records, parse_failure = parse_records(entries, *CHANNEL_PARSE[channel])
        failure = failure or parse_failure
    failure = failure or failure_of(entries)  # 请求层失败同样决定格态，不因取到 0 条降级为空读
    pooled = [record for record in records if record.get("doi")]
    status = "failed" if failure else ("ok" if records else "empty")
    row = channel_row(channel, direction, seed, ctx, entries=entries, hits=len(records),
                      pooled=len(pooled), dropped_no_doi=len(records) - len(pooled),
                      available=available, truncated=truncated, status=status, failure=failure)
    return pooled, row


def parse_records(entries: list[dict], keys: tuple[str, ...], mapper) -> tuple[list[dict], dict | None]:
    """解析单请求通道条目：形态出乎意料只记 ∅ 带原因，不猜、不崩；请求读数与原始响应照记。"""
    entry = entries[0]
    try:
        return [mapper(item) for item in payload_items(entry, keys)], None
    except (AttributeError, KeyError, TypeError, ValueError) as err:
        return [], {"class": "internal_error", "message": f"响应形态出乎意料：{type(err).__name__}: {err}",
                    "attempts": entry.get("attempts"), "retried": bool(entry.get("retried"))}


def payload_int(entry: dict, key: str) -> int | None:
    payload = entry.get("payload")
    value = payload.get(key) if isinstance(payload, dict) else None
    return value if isinstance(value, int) else None


# ---------------------------------------------------------------- 摄入过滤（ADR-0024）


def ingest_filtered(record: dict, cur_year: int) -> bool:
    """摄入过滤判据：True＝不入池。只作用于引文扩展池记录（检索池与追加轮池不由此路裁决）。

    通过＝被引 ≥`CITED_FLOOR` 或 发表年份落在豁免窗口（`cur_year − EXEMPT_YEARS` 起，含当年）。缺值口径：
    被引读数缺失**一律不入池**（fail-closed，全通道含 EPMC——其 `citationCount` 恒为 None，事实回归纯读数
    通道）；年份缺失按「新」走豁免档（未知不猜测，也不因未知而杀）。
    """
    cited = record.get("citationCount")
    if not isinstance(cited, int) or isinstance(cited, bool):
        return True
    if cited >= CITED_FLOOR:
        return False
    year = record.get("year")
    if not isinstance(year, int) or isinstance(year, bool):
        return False
    return cur_year - year > EXEMPT_YEARS


def filtered_row(record: dict, direction: str, channel: str) -> dict:
    """被滤条目读数行：清单字段冻结为 doi｜title｜cited｜year｜direction｜channel（复议凭此逐条回看）。"""
    return {"doi": record["doi"], "title": record.get("title"), "cited": record.get("citationCount"),
            "year": record.get("year"), "direction": direction, "channel": channel}


def filter_policy(ctx: dict) -> dict:
    """过滤口径随读数落盘：版本＋阈值＋豁免窗口＋范围＋开关态（跨交付可比；关闭时记「未过滤」不记 0）。"""
    return {"version": FILTER_VERSION, "enabled": ctx["filter"],
            "state": "启用" if ctx["filter"] else "未过滤",
            "cited_floor": CITED_FLOOR, "exempt_years": EXEMPT_YEARS, "cur_year": ctx["cur_year"],
            "scope": POOL_KIND, "rule": FILTER_RULE}


# ---------------------------------------------------------------- 通道


def stopped_failure(stop_reason: str) -> dict:
    """未跑格的原因：预算耗尽与轮级中止各有错误类，不外抛、不留 `failure: null`。"""
    if stop_reason == STOP_BUDGET:
        return {"class": "quota_exhausted", "message": "预算耗尽：该格未取", "attempts": 0, "retried": False}
    return {"class": "unavailable", "message": f"{stop_reason}：该格未跑", "attempts": 0, "retried": False}


def channel_row(channel, direction, seed, ctx, entries=None, hits=0, pooled=0, dropped_no_doi=0,
                available=None, truncated=False, status="ok", stop_reason=None, failure=None) -> dict:
    entries = entries or []
    if failure is None:
        failure = failure_of(entries) or (stopped_failure(stop_reason) if stop_reason else None)
    return {
        "channel": channel,
        "direction": direction,
        "origin": ORIGIN.get(direction),
        "source": channel.split("-", 1)[0],
        "seed": seed["doi"],
        "slug": seed["slug"],
        "pool_id": f"{POOL_PREFIX[direction]}-{seed['slug']}" if direction in POOL_PREFIX else None,
        "requests": sum(int(entry.get("attempts") or 0) for entry in entries),
        "hits": hits,
        "pooled": pooled,
        "dropped_no_doi": dropped_no_doi,
        "available": available,
        "truncated": bool(truncated),
        "status": status,
        "failure": failure,
        "stop_reason": stop_reason,
        "raw": raw_rel(channel, seed) if entries else None,
    }


def raw_rel(channel: str, seed: dict) -> str:
    return f"{B1_DIR}/{RAW_DIR}/citation-{channel}-{seed['slug']}.json"


def write_raw(ctx: dict, channel: str, seed: dict, entries: list[dict]) -> None:
    if not entries:
        return
    path = ctx["raw_dir"] / f"citation-{channel}-{seed['slug']}.json"
    write_atomic(path, dumps({"channel": channel, "seed": seed["doi"], "slug": seed["slug"],
                              "requests": entries}))


def channel_seed_record(seed, client, ctx) -> tuple[list[dict], dict]:
    """种子记录一跳：`referenced_works`＋`cited_by_count`＋`ids.pmid` 同源拿到。"""
    url = (f"{ctx['openalex_base']}/works/https://doi.org/{quote_doi(seed['doi'])}"
           f"?select={SELECT_SEED}")
    entries = [client.get_json("openalex", url)]
    write_raw(ctx, "openalex-seed", seed, entries)
    return entries, channel_row("openalex-seed", "seed", seed, ctx, entries=entries,
                                hits=1 if entries[0].get("payload") else 0, available=1,
                                status="ok" if entries[0].get("payload") else "failed",
                                stop_reason=None if entries[0].get("payload") else "种子记录不可取")


def channel_backward_openalex(seed, client, ctx, refs: list[str]) -> tuple[list[dict], dict]:
    channel = "openalex-bwd"
    cap = min(ctx["bwd_cap"], ID_BATCH)  # 批量取数上限：≤50 id／请求（实测通过）
    if not refs:
        return [], channel_row(channel, "bwd", seed, ctx, status="empty")
    available, truncated = len(refs), len(refs) > cap
    ids = refs[:cap]
    url = (f"{ctx['openalex_base']}/works?filter=openalex_id:{'|'.join(ids)}"
           f"&per-page={len(ids)}&select={SELECT_WORK}")
    entries = [client.get_json("openalex", url)]
    write_raw(ctx, channel, seed, entries)
    return finish_channel(channel, "bwd", seed, ctx, entries, available=available, truncated=truncated)


def channel_backward_epmc(seed, client, ctx, pmid: str | None) -> tuple[list[dict], dict]:
    channel, cap = "epmc-bwd", ctx["bwd_cap"]
    if not pmid:
        return [], channel_row(channel, "bwd", seed, ctx, status="skipped", stop_reason="无 PMID（EPMC 需 PMID）")
    url = f"{ctx['epmc_base']}/MED/{pmid}/references?format=json&pageSize={cap}"
    entries = [client.get_json("epmc", url)]
    write_raw(ctx, channel, seed, entries)
    pooled, row = finish_channel(channel, "bwd", seed, ctx, entries,
                                 available=payload_int(entries[0], "hitCount"))
    row["truncated"] = isinstance(row["available"], int) and row["available"] > row["hits"]
    return pooled, row


def channel_forward_openalex(seed, client, ctx, work_id: str | None) -> tuple[list[dict], dict]:
    channel, cap = "openalex-fwd", ctx["fwd_cap"]
    if not work_id:
        return [], channel_row(channel, "fwd", seed, ctx, status="skipped", stop_reason="无 OpenAlex work id")
    entries: list[dict] = []
    records: list[dict] = []
    available: int | None = None
    cursor, exhausted, failure = "*", False, None
    # 分页累计到方向上限即可（每页 ≤PAGE_SIZE，per-page 上限 200）
    while len(records) < cap:
        page = min(PAGE_SIZE, cap - len(records))
        url = (f"{ctx['openalex_base']}/works?filter=cites:{work_id}&per-page={page}"
               f"&select={SELECT_WORK}&cursor={urllib.parse.quote(cursor)}")
        try:
            entry = client.get_json("openalex", url)
        except BudgetExhausted:
            exhausted = True
            break
        entries.append(entry)
        meta = (entry.get("payload") or {}).get("meta") if isinstance(entry.get("payload"), dict) else None
        meta = meta if isinstance(meta, dict) else {}
        if available is None and isinstance(meta.get("count"), int):
            available = meta["count"]
        try:
            items = payload_items(entry, ("results",))
        except ValueError as err:
            failure = {"class": "internal_error", "message": f"响应形态出乎意料：{err}",
                       "attempts": entry.get("attempts"), "retried": bool(entry.get("retried"))}
            break
        if not items:
            break
        records.extend(openalex_record(item) for item in items)
        cursor = meta.get("next_cursor")
        if not cursor:
            break
    write_raw(ctx, channel, seed, entries)
    if exhausted and not entries:
        return records, channel_row(channel, "fwd", seed, ctx, status="skipped", stop_reason=STOP_BUDGET)
    pooled, row = finish_channel(channel, "fwd", seed, ctx, entries, records=records,
                                 available=available,
                                 truncated=isinstance(available, int) and available > len(records),
                                 failure=failure)
    if exhausted:
        row["stop_reason"] = STOP_BUDGET
    return pooled, row


def channel_forward_epmc(seed, client, ctx, pmid: str | None) -> tuple[list[dict], dict]:
    channel, cap = "epmc-fwd", ctx["fwd_cap"]
    if not pmid:
        return [], channel_row(channel, "fwd", seed, ctx, status="skipped", stop_reason="无 PMID（EPMC 需 PMID）")
    url = f"{ctx['epmc_base']}/MED/{pmid}/citations?format=json&pageSize={cap}"
    entries = [client.get_json("epmc", url)]
    write_raw(ctx, channel, seed, entries)
    pooled, row = finish_channel(channel, "fwd", seed, ctx, entries,
                                 available=payload_int(entries[0], "hitCount"))
    row["truncated"] = isinstance(row["available"], int) and row["available"] > row["hits"]
    return pooled, row


def channel_forward_s2(seed, client, ctx) -> tuple[list[dict], dict]:
    """S2 补通道：仅两源前向双空读时补一次；字段表不含 `references.authors.name`（该字段整请求 400）。"""
    channel, cap = "s2-fwd", ctx["fwd_cap"]
    url = (f"{ctx['s2_base']}/paper/DOI:{seed['doi']}/citations"
           f"?fields={S2_FIELDS}&limit={min(cap, 1000)}")
    entries = [client.get_json("s2", url)]
    write_raw(ctx, channel, seed, entries)
    payload = entries[0].get("payload")
    truncated = isinstance(payload, dict) and payload.get("next") is not None  # S2 无总量字段，只认游标
    return finish_channel(channel, "fwd", seed, ctx, entries, truncated=truncated)


def guarded(call, channel: str, direction: str, seed, ctx) -> tuple[list[dict], dict]:
    """方向级／种子级失败隔离：单格异常（预算耗尽／响应形态出乎意料）只让该格记 ∅ 带原因，不冒泡。"""
    try:
        return call()
    except BudgetExhausted:
        return [], channel_row(channel, direction, seed, ctx, status="skipped", stop_reason=STOP_BUDGET)
    except (AttributeError, KeyError, TypeError, ValueError) as err:
        return [], channel_row(channel, direction, seed, ctx, status="failed",
                               failure={"class": "internal_error", "attempts": None, "retried": False,
                                        "message": f"响应形态出乎意料：{type(err).__name__}: {err}"})


# ---------------------------------------------------------------- 单种子扩展


def pool_for(seed: dict, direction: str, records: list[dict]) -> dict:
    sources = {record["retrievalProvider"] for record in records}
    return {
        "pool_id": f"{POOL_PREFIX[direction]}-{seed['slug']}",
        "pool_kind": POOL_KIND,
        "family": POOL_FAMILY[direction],
        "window": POOL_WINDOW,
        "origin": ORIGIN[direction],
        "query": POOL_QUERY[direction].format(doi=seed["doi"]),
        "providers": len(sources),
        "results": records,
    }


def skipped_seed_rows(seed: dict, ctx: dict, reason: str) -> list[dict]:
    return [channel_row(channel, direction, seed, ctx, status="skipped", stop_reason=reason)
            for channel, direction in PLANNED_CHANNELS]


def expand_seed(seed, client, ctx) -> tuple[dict, list[dict], list[dict], list[dict]]:
    """单种子一跳：逐格取数→方向桶并池；被滤条目按方向×通道另计，不顶替池内位次。"""
    rows: list[dict] = []
    filtered_entries: list[dict] = []
    # 关闭过滤时逐方向 `filtered` 记 ∅（未过滤）——不记 0（未裁决不得读成实测零）
    zero = dict(DIRECTION_ZERO, filtered=0 if ctx["filter"] else None)
    summary = {"doi": seed["doi"], "slug": seed["slug"], "reasons": seed["reasons"],
               "sources": seed["sources"], "evidence": seed.get("evidence"),
               "openalex_id": None, "pmid": None, "status": "ok", "stop_reason": None,
               "backward": dict(zero), "forward": dict(zero),
               "channels": []}

    entries, seed_row = guarded(lambda: channel_seed_record(seed, client, ctx), "openalex-seed", "seed", seed, ctx)
    rows.append(seed_row)
    work = entries[0].get("payload") if entries else None
    work = work if isinstance(work, dict) else None
    if work is None:
        reason = seed_row["stop_reason"] or "种子记录不可取（seed 跳失败）"
        summary["status"] = "skipped" if seed_row["status"] == "skipped" else "failed"
        summary["failure"] = seed_row["failure"]
        summary["stop_reason"] = seed_row["stop_reason"]
        rows.extend([row for row in skipped_seed_rows(seed, ctx, reason)
                     if row["channel"] != "openalex-seed"])
        summary["channels"] = [row["channel"] for row in rows]
        return summary, rows, [], []

    ids = work.get("ids") if isinstance(work.get("ids"), dict) else {}
    work_id = last_segment(work.get("id"))
    pmid = last_segment(ids.get("pmid"))
    refs = [item for item in (work.get("referenced_works") or []) if isinstance(item, str)]
    summary["openalex_id"] = work_id
    summary["pmid"] = pmid
    summary["backward"] = dict(zero, available=len(refs))
    cited_by = work.get("cited_by_count")
    summary["forward"] = dict(zero, available=cited_by if isinstance(cited_by, int) else None)

    backward: list[tuple[str, dict]] = []
    forward: list[tuple[str, dict]] = []
    for call, channel, direction, bucket in (
        (lambda: channel_backward_openalex(seed, client, ctx, refs), "openalex-bwd", "bwd", backward),
        (lambda: channel_backward_epmc(seed, client, ctx, pmid), "epmc-bwd", "bwd", backward),
        (lambda: channel_forward_openalex(seed, client, ctx, work_id), "openalex-fwd", "fwd", forward),
        (lambda: channel_forward_epmc(seed, client, ctx, pmid), "epmc-fwd", "fwd", forward),
    ):
        records, row = guarded(call, channel, direction, seed, ctx)
        rows.append(row)
        bucket.extend((channel, record) for record in records)

    forward_cells = [row for row in rows if row["channel"] in S2_TRIGGER_CHANNELS]
    # 双空读＝两源前向都「取到且零条」（status empty）；失败／跳过不是空读，不触发补通道
    if len(forward_cells) == len(S2_TRIGGER_CHANNELS) and all(row["status"] == "empty" for row in forward_cells):
        records, row = guarded(lambda: channel_forward_s2(seed, client, ctx), "s2-fwd", "fwd", seed, ctx)
        rows.append(row)
        forward.extend(("s2-fwd", record) for record in records)
    else:
        rows.append(channel_row("s2-fwd", "fwd", seed, ctx, status="skipped",
                                stop_reason="前向非双空读（S2 补通道未触发）"))

    pools = []
    for direction, tagged, label in (("bwd", backward, "backward"), ("fwd", forward, "forward")):
        cap = ctx["bwd_cap"] if direction == "bwd" else ctx["fwd_cap"]
        summary[label]["retrieved"] = len(tagged)
        summary[label]["truncated"] = len(tagged) > cap  # 方向级二次截取（主通道在前：OpenAlex→EPMC→S2）
        summary[label]["channel_truncated"] = any(row["truncated"] for row in rows if row["direction"] == direction)
        # 上限窗按**取回条数**裁（ADR-0024：上限与截断口径不变，被滤条目不回填、不补取）；过滤只裁决窗内记录
        kept: list[dict] = []
        for channel, record in tagged[:cap]:
            if ctx["filter"] and ingest_filtered(record, ctx["cur_year"]):
                filtered_entries.append(filtered_row(record, direction, channel))
                continue
            kept.append(record)
        summary[label]["pooled"] = len(kept)
        summary[label]["filtered"] = (len(tagged[:cap]) - len(kept)) if ctx["filter"] else None
        if kept:
            pools.append(pool_for(seed, direction, kept))
    summary["channels"] = [row["channel"] for row in rows]
    return summary, rows, pools, filtered_entries


# ---------------------------------------------------------------- 种子拼装


def head_seeds(rows: list, limit: int) -> tuple[list[dict], list[str], dict]:
    """头部位次种子：`order` 升序前 limit 条；终池不足 limit 取全池并记实际条数。"""
    ordered = sorted((row for row in rows if isinstance(row, dict)),
                     key=lambda row: row.get("order") if isinstance(row.get("order"), int) else len(rows) + 1)
    taken = ordered[:limit]
    notes: list[str] = []
    if len(ordered) < limit:
        notes.append(f"引文扩展：排序终池 {len(ordered)} 条不足头部 {limit}，取全池 {len(ordered)} 条")
    seeds, missing = [], 0
    for row in taken:
        raw_doi = row.get("key") if isinstance(row.get("key"), str) else row.get("doi")
        doi = norm_doi(raw_doi) if isinstance(raw_doi, str) else ""
        if not doi:
            missing += 1
            continue
        seeds.append({"doi": doi, "reasons": [f"head-rank {row.get('order')}"], "sources": ["head"]})
    if missing:
        notes.append(f"引文扩展：头部 {missing} 条无可用 DOI，跳过（不计入种子）")
    return seeds, notes, {"available": len(ordered), "taken": len(seeds)}


def load_judgment(path: Path) -> tuple[list[dict], list[str], dict]:
    """判定集形态校验（只校验不代判）：字段齐、DOI 归一、理由非空；零命中记一行、继续不阻断。"""
    notes: list[str] = []
    info = {"present": path.is_file(), "shape_error": False, "valid": 0, "rejected": []}
    if not info["present"]:
        notes.append(f"引文扩展：判定集 0 条（缺 {path.name}），仅用头部位次种子")
        return [], notes, info
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("seeds"), list):
        info["shape_error"] = True
        notes.append(f"引文扩展：判定集 0 条（{path.name} 形态不符，需 {{\"seeds\": […]}}），继续不阻断")
        print(f"引文扩展：判定集形态不符（需 {{\"seeds\": […]}}）：{path}", file=sys.stderr)
        return [], notes, info

    seeds: list[dict] = []
    for index, item in enumerate(data["seeds"], start=1):
        problems = []
        raw_doi = item.get("doi") if isinstance(item, dict) else None
        doi = norm_doi(raw_doi) if isinstance(raw_doi, str) else ""
        if not isinstance(item, dict):
            problems.append("条目非对象")
        else:
            if not doi:
                problems.append("doi 缺或不可归一")
            if not str(item.get("reason") or "").strip():
                problems.append("reason 空")
            if not str(item.get("evidence") or "").strip():
                problems.append("evidence 空")
        if problems:
            info["rejected"].append({"index": index,
                                     "doi": item.get("doi") if isinstance(item, dict) else None,
                                     "problems": problems})
            continue
        seeds.append({"doi": doi, "reasons": [str(item.get("reason")).strip()],
                      "evidence": str(item.get("evidence")).strip(), "sources": ["judgment"]})
    info["valid"] = len(seeds)
    if not seeds:
        notes.append(f"引文扩展：判定集 0 条（{len(data['seeds'])} 条入表，全部不合形态："
                     f"字段齐／DOI 归一／理由非空）")
    elif info["rejected"]:
        notes.append(f"引文扩展：判定集 {len(data['seeds'])} 条入表，形态不合被拒 {len(info['rejected'])} 条，"
                     f"采用 {len(seeds)} 条")
    return seeds, notes, info


def assemble_seeds(candidates: list[dict]) -> list[dict]:
    """拼装＝头部 20 ∪ 判定集，按归一 DOI 去重（理由并列保留）；slug 冲突补哈希保唯一。"""
    merged: dict[str, dict] = {}
    for seed in candidates:
        existing = merged.get(seed["doi"])
        if existing is None:
            merged[seed["doi"]] = dict(seed)
            continue
        for reason in seed["reasons"]:
            if reason not in existing["reasons"]:
                existing["reasons"].append(reason)
        existing["sources"] = sorted(set(existing["sources"]) | set(seed["sources"]))
        if seed.get("evidence") and not existing.get("evidence"):
            existing["evidence"] = seed["evidence"]
    seeds = list(merged.values())
    used: dict[str, int] = {}
    for seed in seeds:
        slug = slug_for(seed["doi"])
        seen = used.get(slug, 0)
        used[slug] = seen + 1
        seed["slug"] = slug if seen == 0 else f"{slug}-{hashlib.sha1(seed['doi'].encode('utf-8')).hexdigest()[:6]}"
    return seeds


# ---------------------------------------------------------------- 并池与读数


def is_extension_pool(pool: object) -> bool:
    return isinstance(pool, dict) and pool.get("pool_kind") == POOL_KIND


def pool_record_keys(pools: list) -> set[str]:
    keys: set[str] = set()
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        for record in (pool.get("results") or pool.get("items") or []):
            if isinstance(record, dict):
                key = record_key(record)
                if key:
                    keys.add(key)
    return keys


def new_record_readings(pools: list[dict], existing_keys: set[str], identity_dropped: int) -> dict:
    """新增题录读数：只统计真正入池的记录（无标识丢弃单列，不计入去重前／后与入池新增）。"""
    keys = [record_key(record) for pool in pools for record in pool["results"]]
    unique = list(dict.fromkeys(key for key in keys if key))
    already = [key for key in unique if key in existing_keys]
    return {"before_dedupe": len(keys), "after_dedupe": len(unique),
            "already_in_pools": len(already), "new_to_pools": len(unique) - len(already),
            "identity_dropped": identity_dropped}


def abstract_readings(pools: list[dict]) -> dict:
    """引文扩展记录的摘要读数：可重建／通道原样／无摘要三分（取回行裁窗后、摄入过滤后的那一批量）。

    `rebuilt` 只可能出自 OpenAlex（正文由索引还原）；通道原样带正文者（EPMC `abstractText`／S2
    `abstract`）另计 `carried`，不并入重建数——两类来源不同，读数不混。`missing` 是 ∅：**不写空串冒充**，
    成因**按码**计数（记录 `abstractReason.class`，人读详情在 `.message`，故违规词与位置值不进读数件）；
    无 `abstractReason` 者即通道侧没给正文（EPMC／S2 无重建步骤），记 `channel_no_text`。
    """
    by_provider: dict[str, dict] = {}
    reasons: Counter = Counter()
    for pool in pools:
        for record in pool["results"]:
            provider = record.get("retrievalProvider") or "unknown"
            slot = by_provider.setdefault(provider, {"records": 0, "rebuilt": 0, "carried": 0, "missing": 0})
            slot["records"] += 1
            if not (record.get("abstract") or "").strip():
                slot["missing"] += 1
                reason = record.get("abstractReason")
                code = reason.get("class") if isinstance(reason, dict) else None
                reasons[code or "channel_no_text"] += 1
            elif provider == "openalex":
                slot["rebuilt"] += 1
            else:
                slot["carried"] += 1
    totals = {key: sum(slot[key] for slot in by_provider.values())
              for key in ("records", "rebuilt", "carried", "missing")}
    with_text = totals["rebuilt"] + totals["carried"]

    def rate(count: int) -> float | None:
        return round(count / totals["records"], 4) if totals["records"] else None

    return {
        "field": ABSTRACT_INDEX_FIELD,
        "scope": f"{POOL_KIND}记录（方向窗内裁窗后、摄入过滤后）",
        "note": "分母＝该批引文扩展记录（正常轮即 `pools[].results`；轮中止未并池时此处仍记取回事实，"
                "不冒充入池）；rebuilt＝OpenAlex 索引还原、carried＝通道原样带正文（EPMC abstractText／"
                "S2 abstract）、missing＝∅（不写空串冒充）；missing_reasons 按成因码计数"
                "（记录 abstractReason.class），逐条详情在 abstractReason.message",
        "records": totals["records"], "rebuilt": totals["rebuilt"], "rebuilt_rate": rate(totals["rebuilt"]),
        "carried": totals["carried"], "non_empty_rate": rate(with_text), "missing": totals["missing"],
        "by_provider": dict(sorted(by_provider.items())),
        "missing_reasons": dict(reasons.most_common()),
    }


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(path.suffix + ".staging")
    staging.write_text(text, encoding="utf-8")
    os.replace(staging, path)


def append_pools(path: Path, pools_data: dict, pools: list[dict]) -> None:
    """幂等并池：先撤同 pool_kind 的旧池，再按种子序追加本轮扩展池；顶层其余键与缩进照旧，写走原子替换。"""
    text = path.read_text(encoding="utf-8")
    match = re.search(r"\n( +)\"", text)
    kept = [pool for pool in pools_data["pools"] if not is_extension_pool(pool)]
    merged = dict(pools_data)
    merged["pools"] = kept + pools
    write_atomic(path, json.dumps(merged, ensure_ascii=False, indent=len(match.group(1)) if match else 1) + "\n")


# ---------------------------------------------------------------- 主流程


def ctx_for(args: argparse.Namespace) -> dict:
    delivery = Path(args.delivery)
    b1 = delivery / B1_DIR
    return {
        "delivery": delivery,
        "b1": b1,
        "raw_dir": b1 / RAW_DIR,
        "openalex_base": args.openalex_base.rstrip("/"),
        "epmc_base": args.epmc_base.rstrip("/"),
        "s2_base": args.s2_base.rstrip("/"),
        "fwd_cap": args.fwd_cap,
        "bwd_cap": args.bwd_cap,
        "cur_year": args.cur_year,
        "filter": not args.no_filter,
        "sort_path": Path(args.sort_report) if args.sort_report else b1 / Path(SORT_NAME).name,
        "seeds_path": Path(args.seeds_file) if args.seeds_file else b1 / Path(SEEDS_NAME).name,
        "pools_path": Path(args.pools) if args.pools else b1 / Path(POOLS_NAME).name,
        "report_path": Path(args.report) if args.report else b1 / Path(EXPANSION_NAME).name,
    }


def run_seeds(seeds: list[dict], client: HttpClient, ctx: dict
              ) -> tuple[list[dict], list[dict], list[dict], list[dict], str]:
    """种子集顺序跑：预算耗尽即停未跑的种子；连续多种子全通道瞬时失败＝中止该轮。"""
    rows: list[dict] = []
    summaries: list[dict] = []
    pools: list[dict] = []
    filtered: list[dict] = []
    streak, stop_reason = 0, STOP_DONE
    for seed in seeds:
        if client.budget.used >= client.budget.limit:
            stop_reason = STOP_BUDGET
            break
        summary, seed_rows, seed_pools, seed_filtered = expand_seed(seed, client, ctx)
        rows.extend(seed_rows)
        summaries.append(summary)
        pools.extend(seed_pools)
        filtered.extend(seed_filtered)
        attempted = [row for row in seed_rows if row["status"] in ("ok", "empty", "failed")]
        failed = [row for row in attempted if row["status"] == "failed"]
        all_transient = (bool(failed) and len(failed) == len(attempted)
                         and all((row["failure"] or {}).get("class") in TRANSIENT_CLASSES for row in failed))
        streak = streak + 1 if all_transient else 0
        if streak >= ABORT_AFTER_FAILED_SEEDS:
            stop_reason = STOP_ABORT
            break
    for seed in seeds[len(summaries):]:
        rows.extend(skipped_seed_rows(seed, ctx, stop_reason))
    return rows, summaries, pools, filtered, stop_reason


def print_notes(notes: list[str]) -> None:
    for note in notes:
        print(note)


def filtered_reason(entry: dict, cur_year: int) -> str:
    """被滤条目的不入池理由（与 `ingest_filtered` 的两条并列理由同口径，人读面不合并陈述）。"""
    if not isinstance(entry.get("cited"), int) or isinstance(entry.get("cited"), bool):
        return "被引读数缺失（fail-closed：读数缺一律不入池）"
    return f"被引 {entry['cited']} <{CITED_FLOOR} 且发表年早于 {cur_year - EXEMPT_YEARS}"


def print_gaps(rows: list[dict], report: dict) -> None:
    """缺口逐条带原因：截断／失败逐条，未跑按原因成组；被滤条目另计一行（非缺口、非缺席、非截断）。"""
    for row in rows:
        if row["truncated"] and row["status"] in ("ok", "empty"):
            print(f"引文扩展·截断：{row['channel']}｜{row['seed']}｜取 {row['hits']}／可及 {row['available']}"
                  f"（上限{'前向' if row['direction'] == 'fwd' else '后向'}）")
        if row["status"] == "failed":
            failure = row["failure"] or {}
            print(f"引文扩展·失败：{row['channel']}｜{row['seed']}｜∅ {failure.get('class')}："
                  f"{failure.get('message')}")
    grouped = Counter(row["stop_reason"] for row in rows
                      if row["status"] == "skipped" and row["stop_reason"])
    for reason, count in grouped.items():
        print(f"引文扩展·未跑：{reason}｜{count} 通道")
    dropped = Counter()
    for row in rows:
        if row.get("dropped_no_doi"):
            dropped[row["channel"]] += row["dropped_no_doi"]
    for channel, count in dropped.items():
        print(f"引文扩展·无标识丢弃：{channel}｜{count} 条（无 DOI 题录按 Step2 一级规则禁入持久化，只记读数）")
    policy = report["filter_policy"]
    if not policy["enabled"]:
        print("引文扩展·摄入过滤：未过滤（--no-filter：本轮不按被引下限裁决，读数记「未过滤」不记 0）")
        return
    grouped = Counter((entry["channel"], filtered_reason(entry, policy["cur_year"]))
                      for entry in report["filtered"])
    for (channel, reason), count in sorted(grouped.items()):
        print(f"引文扩展·摄入过滤：{channel}｜{count} 条（{reason}，按摄入过滤不入池——非源侧缺席、"
              f"非截断，另计 filtered）")
    missing_cited = sum(1 for entry in report["filtered"]
                        if not isinstance(entry.get("cited"), int) or isinstance(entry.get("cited"), bool))
    print(f"引文扩展·摄入过滤：合计 {len(report['filtered'])} 条"
          + (f"（其中缺被引读数 {missing_cited} 条按 fail-closed 不入池）" if missing_cited else "")
          + f"（口径 {policy['version']}，逐条见读数件的 filtered[]）")


def print_summary(report: dict) -> None:
    source = report["seed_source"]
    budget = report["budget"]
    channels = report["channels"]
    records = report["new_records"]
    policy = report["filter_policy"]
    abstracts = report["abstract_rebuild"]
    filter_text = (f"被滤 {len(report['filtered'])} 条" if policy["enabled"] else "未过滤")
    pct_text = ("（占比 ∅）" if abstracts["non_empty_rate"] is None else
                f"（非空占比 {100 * abstracts['non_empty_rate']:.1f}%，"
                f"其中可重建占 {100 * abstracts['rebuilt_rate']:.1f}%）")
    print(f"引文扩展：种子 {source['total_seeds']} 条（头部 {source['head_taken']}／判定集 "
          f"{source['judgment_valid']}）｜方向 后向+前向｜请求 {budget['used']}／{budget['limit']}"
          f"｜通道命中题录 {sum(row['hits'] for row in channels)} 条"
          f"｜入池题录 {sum(pool['records'] for pool in report['pools'])} 条"
          f"｜无标识丢弃 {records['identity_dropped']} 条"
          f"｜摄入过滤 {filter_text}"
          f"｜摘要 可重建 {abstracts['rebuilt']}／通道原样 {abstracts['carried']}／"
          f"无摘要 {abstracts['missing']}{pct_text}"
          f"｜新增题录 去重前 {records['before_dedupe']}／去重后 {records['after_dedupe']}／"
          f"入池新增 {records['new_to_pools']}"
          f"｜截断 {sum(1 for row in channels if row['truncated'])} 通道"
          f"｜失败 {sum(1 for row in channels if row['status'] == 'failed')} 通道"
          f"｜判停 {budget['stop_reason']}")
    if report["budget"]["aborted"]:
        print(f"引文扩展（并池）：本轮中止（{STOP_ABORT}），扩展池不并池（{POOL_KIND}池 0 个）")
    else:
        print(f"引文扩展（并池）：追加{POOL_KIND}池 {len(report['pools'])} 个进 step1-pools.json；"
              f"读数见 {report['report_path']}")


def cmd_expand(args: argparse.Namespace) -> int:
    ctx = ctx_for(args)
    if not ctx["sort_path"].is_file():
        print(f"引文扩展轮错误：排序报告缺失（{ctx['sort_path']}）——先跑 Step2 首轮排序", file=sys.stderr)
        return 2
    if not ctx["pools_path"].is_file():
        print(f"引文扩展轮错误：发现清单缺失（{ctx['pools_path']}）", file=sys.stderr)
        return 2
    sort_data = read_json(ctx["sort_path"])
    if not isinstance(sort_data, dict) or not isinstance(sort_data.get("rows"), list):
        print(f"引文扩展轮错误：排序报告形态不符（需 {{\"rows\": […]}}）：{ctx['sort_path']}", file=sys.stderr)
        return 2
    pools_data = read_json(ctx["pools_path"])
    if not isinstance(pools_data, dict) or not isinstance(pools_data.get("pools"), list):
        print(f"引文扩展轮错误：发现清单形态不符（需 {{\"pools\": […]}}）：{ctx['pools_path']}", file=sys.stderr)
        return 2

    head, head_notes, head_info = head_seeds(sort_data["rows"], args.seed_limit)
    judgment, judgment_notes, judgment_info = load_judgment(ctx["seeds_path"])
    seeds = assemble_seeds(head + judgment)
    notes = head_notes + judgment_notes

    client = HttpClient(Budget(args.budget), Throttle())
    rows, summaries, pools, filtered, stop_reason = run_seeds(seeds, client, ctx)
    aborted = stop_reason == STOP_ABORT
    # 新增数以**检索池**为基准：本轮扩展池自身不作基准，否则重跑同一交付必读 0（自指）
    retrieval_keys = pool_record_keys([pool for pool in pools_data["pools"] if not is_extension_pool(pool)])
    readings = new_record_readings(pools, retrieval_keys,
                                  sum(row["dropped_no_doi"] for row in rows))
    if pools and not aborted:
        append_pools(ctx["pools_path"], pools_data, pools)

    report = {
        "generated_at": date.today().isoformat(),
        "delivery": ctx["delivery"].as_posix(),
        "seed_source": {
            "sort_report": ctx["sort_path"].as_posix(),
            "head_available": head_info["available"],
            "head_taken": head_info["taken"],
            "judgment_path": ctx["seeds_path"].as_posix(),
            "judgment_present": judgment_info["present"],
            "judgment_shape_error": judgment_info["shape_error"],
            "judgment_valid": judgment_info["valid"],
            "judgment_rejected": judgment_info["rejected"],
            "total_seeds": len(seeds),
        },
        "budget": {"limit": args.budget, "used": client.budget.used,
                   "remaining": args.budget - client.budget.used, "stop_reason": stop_reason,
                   "aborted": aborted},
        "caps": {"forward": args.fwd_cap, "backward": args.bwd_cap,
                 "per_channel": {"openalex-bwd": min(args.bwd_cap, ID_BATCH), "epmc-bwd": args.bwd_cap,
                                 "openalex-fwd": args.fwd_cap, "epmc-fwd": args.fwd_cap,
                                 "s2-fwd": args.fwd_cap},
                 "note": "forward／backward＝每种子每方向入池上界（合并方向桶时二次截取，主通道优先）；"
                         "per_channel＝各通道取数上限；EPMC 每种子每方向 1 请求、S2 补通道仅双空读时 1 次"},
        # 摄入过滤（ADR-0024）：口径随读数落盘；被滤条目逐条留痕（非缺席、非截断，不占池内位次）
        "filter_policy": filter_policy(ctx),
        "filtered": filtered,
        # 摘要重建（票 02）：入池扩展记录按「可重建／通道原样／无摘要」三分，与「无摘要」分列
        "abstract_rebuild": abstract_readings(pools),

        "channels": rows,
        "seeds": summaries,
        "pools": [{"pool_id": pool["pool_id"], "origin": pool["origin"], "family": pool["family"],
                   "records": len(pool["results"]), "appended": bool(not aborted)} for pool in pools],
        "new_records": readings,
        "raw_dir": f"{B1_DIR}/{RAW_DIR}",
        "report_path": ctx["report_path"].as_posix(),
        "notes": notes,
    }
    write_atomic(ctx["report_path"], dumps(report))

    print_notes(notes)
    print_gaps(rows, report)
    print_summary(report)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="引文扩展轮执行器：OpenAlex＋Europe PMC 单轮引文追索与并池")
    parser.add_argument("--delivery", required=True, help="交付目录（workspace/YYYY-MM-DD-<slug>）")
    parser.add_argument("--seed-limit", type=int, default=HEAD_SEEDS, help=f"头部位次种子数（默认 {HEAD_SEEDS}）")
    parser.add_argument("--budget", type=int, default=REQ_BUDGET, help=f"整轮请求预算含重试（默认 {REQ_BUDGET}）")
    parser.add_argument("--fwd-cap", type=int, default=FWD_CAP, help=f"每种子前向上限（默认 {FWD_CAP}）")
    parser.add_argument("--bwd-cap", type=int, default=BWD_CAP, help=f"每种子后向上限（默认 {BWD_CAP}）")
    parser.add_argument("--cur-year", type=int, default=CUR_YEAR_DEFAULT,
                        help=f"过滤参考年份（豁免窗口＝参考年−{EXEMPT_YEARS} 起含当年；默认 {CUR_YEAR_DEFAULT}，"
                             f"不用系统当年——跨年重跑读数才可比；进 filter_policy）")
    parser.add_argument("--no-filter", action="store_true",
                        help="关闭引文扩展摄入过滤：被滤读数记「未过滤」不记 0（池内容与过滤前逐字一致）")
    parser.add_argument("--openalex-base", default=DEFAULT_OPENALEX_BASE, help="OpenAlex 基址（探针／代理用）")
    parser.add_argument("--epmc-base", default=DEFAULT_EPMC_BASE, help="Europe PMC 基址（探针／代理用）")
    parser.add_argument("--s2-base", default=DEFAULT_S2_BASE, help="Semantic Scholar 基址（探针／代理用）")
    parser.add_argument("--sort-report", help=f"排序报告（默认 <交付>/{SORT_NAME}）")
    parser.add_argument("--seeds-file", help=f"种子判定集（默认 <交付>/{SEEDS_NAME}）")
    parser.add_argument("--pools", help=f"发现清单（默认 <交付>/{POOLS_NAME}）")
    parser.add_argument("--report", help=f"读数落点（默认 <交付>/{EXPANSION_NAME}）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return cmd_expand(args)
    except (OSError, KeyError, ValueError) as exc:
        print(f"引文扩展轮错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
