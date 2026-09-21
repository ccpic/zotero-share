# -*- coding: utf-8 -*-
"""摘要取数与清洗共享模块（ADR-0021）：入库摘要表 → Crossref → PubMed → OpenAlex 逐级取，取到即清洗为纯文本。

When: 写库前需要条目 `abstractNote` 正文时（新建补录、既有条目仅空补齐）。
Do:   `table, missing = load_table(path)`（不启用表传 `None`）→ 逐条
      `resolve(doi, pmid, item_type, table, allow_fetch)` → `(摘要文本或 None, 来源, 状态)`：
      状态 `filled`（取到）／`empty`（表未命中且各级无正文或取数失败）／`na`（文种不适用）／
      `no-identifier`（无 DOI 且无 PMID）；来源 `table`／`crossref`／`pubmed`／`openalex`／`none`。
Why:  取数与清洗只此一份实现，`seed_from_doi.py` 与 `fill_abstracts.py` 共用；分两套口径写同一个
      字段，就会出现「同一篇文献两个库内文本」。摘要入的是正文，取到哪一份、哪一级给的，必须能进留痕行。
契约（ADR-0021）：
      · 次序固定：入库摘要表 → Crossref（`message.abstract`，JATS）→ PubMed（EFetch；无 PMID 时
        先 ESearch 由 DOI 换 PMID）→ OpenAlex（`abstract_inverted_index` 按词位升序重建）。
      · 每一级取到的正文都过 `clean()`；不翻译、不截断、不写占位文本。
      · 判定次序：文种不适用判最前（记 `na`，不算缺口），标识缺失判其后（记 `no-identifier`）；
        类型缺失按 ADR-0021「类型未知计入覆盖率分母」同一口径计入可摘要侧，取不到记 `empty`。
      · `allow_fetch=False` 零网络 I/O（表照读；调用方交底的 Crossref 正文同样照用——都不是本次请求）；
        单级取数异常不阻断，留痕后降级到下一级，全失败记 `empty`；唯一向上抛的是表形态错误——表读错必须炸，
        静默降级成空表会伪装成「全库无摘要」。
      · `crossref_handoff` 交底三态：缺省哨兵＝调用方没查过、照旧自取；显式 `None`＝调用方查过、
        Crossref 无摘要、跳级不重发；文本＝调用方已取到、清洗后即返 `crossref`、零额外请求。
        （形参刻意不与模块函数 `crossref_abstract` 同名：同名遮蔽会让梯级绕开模块全局，替换属性的桩桩不住。）
      · 只依赖 stdlib（`urllib.request`／`json`／`re`／`html`／`xml.etree`），每个请求带 UA。
      · 跨 skill 包不 import：本模块只供本包脚本用；DOI 归一沿用 `dedup_gate.norm_doi`（单源）。
"""
import html
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dedup_gate import norm_doi  # noqa: E402 同族脚本跨件复用（DOI 归一单源，ADR-0020）

CROSSREF = "https://api.crossref.org/works/"
OPENALEX = "https://api.openalex.org/works"
ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
UA = "zotero-seed/1.0 (mailto:research@local)"  # 联系邮箱按需替换
TIMEOUT = 30
ABSTRACTABLE_TYPES = ("journalArticle", "preprint", "conferencePaper")  # 文种适用性单源（ADR-0021）
UNSET = object()  # `resolve` 的 `crossref_handoff` 哨兵：调用方未交底 → 本模块自己联网取 Crossref

_ABSTRACTABLE = frozenset(t.casefold() for t in ABSTRACTABLE_TYPES)
# 标签只认形态完整者：`<` + 可选 `/` + 字母开头的名 + 一律带 `=` 的属性 + 可选 `/` + `>`。
# 「属性一律带 `=`」使 `<T cells>` 不成标签，「名以字母开头」使 `< 0.05`／`p<0.001` 不成标签。
_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9:._-]*"
                    r"(?:\s+[A-Za-z_:][-A-Za-z0-9_:.]*\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+))*"
                    r"\s*/?>")
_LABEL_RE = re.compile(r"^(?:abstract|摘要)\s*[:：]?$", re.I)
# 归一后仍须形如 DOI（`10.<registrant>/<suffix>`）：键写歪了宁可当场炸，也不静默永不命中
_DOI_SHAPE_RE = re.compile(r"^10\.[^/\s]+/\S+$")


def load_table(path):
    """读「入库摘要表」（Step2 产物：`entries` 归一 DOI → `{abstract, source, collected_at}` + `missing`）。

    返回 `(entries, missing)`。形态不符一律抛 `ValueError`：表是取数第一级，读错就炸，不静默降级
    （缺 `entries`／`missing`、条目类型不对、摘要空串、键归一后不成 DOI 形态、归一后撞键、清单含非 DOI 项）。
    文件不存在与 JSON 破损按原样抛（`FileNotFoundError`／`json.JSONDecodeError`）。
    """
    if not path:
        raise ValueError("入库摘要表路径为空")
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"入库摘要表形态不符（顶层应为对象）: {path}")
    records, missing_raw = raw.get("entries"), raw.get("missing")
    if not isinstance(records, dict):
        raise ValueError(f"入库摘要表形态不符（缺 entries 映射）: {path}")
    if not isinstance(missing_raw, list):
        raise ValueError(f"入库摘要表形态不符（缺 missing 清单）: {path}")

    entries = {}
    for key, rec in records.items():
        # 先判类型再归一：norm_doi 对非字符串键抛 AttributeError，会绕过本函数「表形态不符一律 ValueError」的契约
        if not isinstance(key, str):
            raise ValueError(f"入库摘要表条目键不是 DOI: {key!r} ({path})")
        doi = norm_doi(key)
        if not _DOI_SHAPE_RE.match(doi):
            raise ValueError(f"入库摘要表条目键不是 DOI: {key!r} ({path})")
        if doi in entries:
            raise ValueError(f"入库摘要表条目键归一后撞键: {key!r} → {doi} ({path})")
        if not isinstance(rec, dict):
            raise ValueError(f"入库摘要表条目形态不符（应为对象）: {doi} ({path})")
        abstract = rec.get("abstract")
        if not isinstance(abstract, str) or not abstract.strip():
            raise ValueError(f"入库摘要表条目缺摘要文本: {doi} ({path})")
        source = rec.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"入库摘要表条目缺来源: {doi} ({path})")
        collected_at = rec.get("collected_at")
        if not isinstance(collected_at, str) or not collected_at.strip():
            raise ValueError(f"入库摘要表条目缺采集时刻: {doi} ({path})")
        entries[doi] = {"abstract": abstract, "source": source, "collected_at": collected_at}

    missing = []
    for doi in missing_raw:
        if not isinstance(doi, str) or not _DOI_SHAPE_RE.match(norm_doi(doi)):
            raise ValueError(f"入库摘要表无摘要清单含非 DOI 项: {doi!r} ({path})")
        missing.append(norm_doi(doi))
    return entries, missing


def clean(text):
    """JATS/HTML 正文 → 纯文本（ADR-0021 清洗口径）。

    标签转换行（段落与分节因此成行）→ 解 HTML 实体 → 逐行压空白、去空行 → 首行是独立的
    `Abstract`／`摘要` 标签行（可带冒号）则剔掉。结构化摘要的分节标题原样留在正文首位、不转
    markdown；不翻译、不截断、不加占位文本。`None`／空串／全空白 → `""`；对已清洗文本幂等。
    只剔「自己一行」的标签行，粘在正文上的前缀词是内容、不动。
    标签识别取「形态完整」口径：`<` + 可选 `/` + 字母开头的名 + 一律带 `=` 的属性 + 可选 `/` + `>`。
    PubMed（`itertext()` 已解码）与 OpenAlex（逐词重建）送来的都是纯文本，其中的 `p < 0.001`、
    `<T cells>`、`< 2 × 10-16` 若按「`<` 到下一个 `>`」整段删，就会连正文一并吞掉、还把两段无关文字
    粘成一句，而状态照记 `filled`——正文被改坏却无声入库。标签先删、后解实体，故 `&lt;` 不受影响。
    """
    if not isinstance(text, str) or not text.strip():
        return ""
    lines = [re.sub(r"\s+", " ", line).strip() for line in html.unescape(_TAG_RE.sub("\n", text)).splitlines()]
    lines = [line for line in lines if line]
    if lines and _LABEL_RE.match(lines[0]):
        del lines[0]
    return "\n".join(lines)


def applicability(item_type):
    """文种适用性（ADR-0021）：可摘要 True／不适用 False（`resolve` 据此记 `na`）。

    可摘要＝`journalArticle`／`preprint`／`conferencePaper`（本元组为唯一出处）；指南、监管文件、
    书章等非摘要型文种一律不适用。类型缺失（`None`／空）**不判不适用**：ADR-0021 把「类型未知」
    计入覆盖率分母（可摘要侧），取不到记 `empty` 而不是记 `na` 把它伪装成「不需要摘要」。
    """
    kind = (item_type or "").strip().casefold()
    return not kind or kind in _ABSTRACTABLE


def http_get(url, params=None):
    """唯一网络出口：GET → bytes（每请求带 UA、超时 30s；探针桩替换此处即可全量计次）。"""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def crossref_abstract(doi):
    """Crossref `message.abstract`（JATS 片段，未 deposit 摘要的记录为空）。"""
    payload = json.loads(http_get(CROSSREF + urllib.parse.quote(doi)).decode("utf-8"))
    return (payload.get("message") or {}).get("abstract")


def esearch_pmid(doi):
    """按 DOI 换 PMID（ESearch `term=<doi>[DOI]`）；查无记录返回 `None`。"""
    payload = json.loads(http_get(ESEARCH, {"db": "pubmed", "term": f"{doi}[DOI]", "retmode": "json"}).decode("utf-8"))
    ids = (payload.get("esearchresult") or {}).get("idlist") or []
    return ids[0] if ids else None


def pubmed_abstract(pmid=None, doi=None):
    """PubMed EFetch 摘要正文（结构化分节按 `LABEL: 正文` 逐行留；无 PMID 时先 ESearch 换）。

    `retmode=xml` 是唯一给得出结构化 `AbstractText`／`Label` 节点的返回形态（text 形态也给摘要正文，
    但拿不到分节结构），故走 `xml.etree`（stdlib）解析。
    """
    if not pmid:
        pmid = esearch_pmid(doi)
        if not pmid:
            return None
    root = ET.fromstring(http_get(EFETCH, {"db": "pubmed", "id": pmid, "retmode": "xml"}))
    article = root.find(".//PubmedArticle")
    if article is None:
        return None
    parts = []
    for node in article.findall("./MedlineCitation/Article/Abstract/AbstractText"):
        body = " ".join("".join(node.itertext()).split())
        if not body:
            continue
        label = (node.get("Label") or "").strip()
        parts.append(f"{label}: {body}" if label else body)
    return "\n".join(parts) or None


def openalex_abstract(doi=None, pmid=None):
    """OpenAlex 摘要：`abstract_inverted_index`（词 → 词位列表）按词位升序重建为正文。"""
    key = f"doi:{urllib.parse.quote(doi)}" if doi else (f"pmid:{pmid}" if pmid else None)
    if not key:
        return None
    work = json.loads(http_get(f"{OPENALEX}/{key}").decode("utf-8"))
    inverted = work.get("abstract_inverted_index")
    if not isinstance(inverted, dict):
        return None
    slots = {}
    for word, positions in inverted.items():
        for position in positions or []:
            slots[position] = word
    return " ".join(slots[position] for position in sorted(slots)) or None


def resolve(doi, pmid, item_type, table, allow_fetch, crossref_handoff=UNSET):
    """逐级取摘要（ADR-0021 次序：表 → Crossref → PubMed → OpenAlex）。

    返回 `(摘要文本或 None, 来源, 状态)`；来源 `table|crossref|pubmed|openalex|none`，状态
    `filled|empty|na|no-identifier`。`table` 传 `load_table` 的首返回值（归一 DOI → 记录 的映射），
    不启用表传 `None`；`allow_fetch=False` 零网络 I/O──但表与交底均在关网闸之前参与判定，二者均未命中
    才记 `empty`。有 PMID 时 PubMed 直接 EFetch（不再由 DOI 换），只给 DOI 时先 ESearch 换 PMID。

    `crossref_handoff` 是调用方对自己那次 Crossref 取数的交底，三态：`UNSET`（缺省）＝没查过、
    本模块照旧自取；`None`＝查过、Crossref 无 `message.abstract` → **跳过 Crossref 级**（不重发），
    从 PubMed 续；文本（原始 JATS，或已清洗文本——`clean()` 幂等）＝已取到 → 清洗后即返
    `crossref/filled`、零额外请求（清洗后为空也按「已查、无可入库正文」跳级，仍不重发）。
    `seed_from_doi.py` 取元数据时已拿到 `message.abstract`，不交底就得为同一 DOI 再发一次同样的请求。
    交底文本与入库摘要表都不是本次网络请求，故 `allow_fetch=False` 下照用。
    形参不叫 `crossref_abstract`（与模块函数同名会把函数遮蔽掉，梯级就取不到真身）：Crossref 级在
    调用时解析模块全局，替换 `abstracts.crossref_abstract` 的桩才拦得住。
    """
    if table is not None and not isinstance(table, dict):
        raise ValueError("table 应为 load_table 的首返回值（归一 DOI → 记录 的映射）")
    if crossref_handoff is not UNSET and crossref_handoff is not None and not isinstance(crossref_handoff, str):
        raise ValueError("crossref_handoff 应为 UNSET（未交底）／None（已查无）／原始 JATS 文本")
    if not applicability(item_type):
        return None, "none", "na"
    key, pubmed_id = norm_doi(doi), (pmid or "").strip()
    if not key and not pubmed_id:
        return None, "none", "no-identifier"
    hit = table.get(key) if key and table else None
    if hit:
        text = clean(hit.get("abstract"))
        if text:
            return text, "table", "filled"
    known = crossref_handoff is not UNSET
    if known:
        text = clean(crossref_handoff)
        if text:
            return text, "crossref", "filled"
    if not allow_fetch:
        return None, "none", "empty"
    ladder = []
    if key and not known:
        ladder.append(("crossref", lambda: crossref_abstract(key)))  # 调用时解析模块全局，属性替换式桩才拦得住
    pubmed = (lambda: pubmed_abstract(pmid=pubmed_id)) if pubmed_id else (lambda: pubmed_abstract(doi=key))
    ladder.append(("pubmed", pubmed))
    ladder.append(("openalex", lambda: openalex_abstract(doi=key or None, pmid=pubmed_id or None)))
    for source, fetch in ladder:
        try:
            raw = fetch()
        except Exception as error:  # 单级取数失败不阻断：留痕后降级到下一级（ADR-0021）
            print(f"WARN abstract fetch failed source={source} id={key or pubmed_id}: "
                  f"{type(error).__name__}: {error}", file=sys.stderr)
            continue
        text = clean(raw)
        if text:
            return text, source, "filled"
    return None, "none", "empty"
