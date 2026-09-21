"""按 DOI 清单批量补录正式条目：入库预检（DOI 权威、先于元数据）→ 命中 DUP／DUP-GAP 即取消 → Crossref 取元数据 → 摘要取数 → 慢路径复核 → 建条目 → 回查。

When: 外部检索拿到 DOI 清单，需批量补录正式条目（如 journalArticle）。
Do: python seed_from_doi.py --dois 10.1056/NEJMoa1913805 [...] [--tag PCSK9 --dry-run]
    [--abstracts-file run/b1/ingest-abstracts.json] [--no-fetch]
Why: 手组 dict 易缺字段、易与快照类条目撞重复；统一走闭环可复用、可回查。
写库 key 经参数或 ZOTERO_LOCAL_API_KEY 环境变量传入；无绝对路径与密钥落盘。
只读核验：--dry-run 走同一预检—复核链并报告结论，不写库（元数据缓存写入不属于写库）。
预检先行（ADR-0020）：命中零网络元数据；输出行 DUP <doi> key=（重复取消）／DUP-GAP <doi> key=（缺正文附件，转挂载链）／PLAN、NEW（未命中）；预检异常 fail-closed 中止。
摘要（ADR-0021）：建条目前按「入库摘要表 → Crossref → PubMed → OpenAlex」取正文，取到即写条目 abstractNote；
载荷服从判定——状态非 filled（empty／na）时条目不留摘要正文（crossref_meta 已放入的 Crossref 文本一并摘除）；
不翻译、不截断、不写占位文本；缺口非阻断——状态如实留痕、条目照建。
- --abstracts-file 传 Step2 的「入库摘要表」（不传＝不启用表，手工调用行为不变）；--no-fetch 显式关联网补漏
  （表与已取到的 Crossref 元数据照用——都不是本次请求；联网级一律不发）。表读错 fail-closed 中止（静默降级成
  空表会伪装成「全库无摘要」）。
- 留痕 ABS <doi> source=table|crossref|pubmed|openalex|none status=filled|empty|na|no-identifier：每个进到
  元数据解析的 DOI 一行；预检取消的 DOI 零元数据工作，只出 DUP／DUP-GAP 行、不出 ABS 行。
缓存只省读、不省确认——写库确认门与写路径不变：
- Crossref 元数据按归一 DOI（小写、去前缀，§14 口径）缓存在 skill 包内 .cache/（--cache-dir
  须为被忽略的非交付路径，--no-cache 显式绕过）；TTL 168 小时自写入时刻，命中不重写、不顺延；
  同次运行内本进程读数无条件复用。留痕：每个 DOI 打一行 META，记该元数据的补录日期与来源
  （cache＝跨进程缓存命中；memo＝同 run 已读；fetch＝本次取数）。
- 缓存条目带 schema 位（META_CACHE_SCHEMA）：该位缺失或不同的历史条目一律当 miss 重取——旧条目的 meta
  没有 abstractNote，复用会被误读成「Crossref 无摘要」（真值与「没查过」无法区分）。
- 去重结论仅进程内 memo（键＝归一 DOI，与去重比对所用的键一致）：同 run 重复 DOI 只读一次，
  写库后对应键失效重查；跨运行永不缓存（库状态在变，他会话可能写库）。
回查（建条目后唯一读数）：逐条只读回查库内条目，行形如 OK   <doi> key=<key> abstract=ok|empty|na|?
（?＝条目不在库，无从判定；na＝文种不适用，不算缺口）。
退出码 0=全建成或全已收录（含 DUP／DUP-GAP 取消）；1=有失败／需手动迁移的快照条目／预检或摘要表异常（fail-closed）。
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from pyzotero import zotero as zmod

sys.path.insert(0, str(Path(__file__).resolve().parent))
import abstracts  # noqa: E402 摘要取数与清洗共享模块（ADR-0021；同包脚本，与 dedup_gate 同式）
from dedup_gate import PreflightGate, find_by_doi, has_pdf_child, norm_doi  # noqa: E402 同族脚本跨件复用（门槛语义单源）

CROSSREF = "https://api.crossref.org/works/"
UA = "zotero-seed/1.0 (mailto:research@local)"  # 联系邮箱按需替换
META_TTL_HOURS = 168
META_CACHE_SCOPE = "crossref"
META_CACHE_SCHEMA = 1  # 缓存载荷 schema 位；缺该位（历史条目）或不同一律当 miss，见 read_meta_cache
DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache"  # skill 包内忽略位


def meta_cache_path(cache_dir, key):
    return Path(cache_dir) / META_CACHE_SCOPE / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")


def read_meta_cache(cache_dir, key, now):
    """命中返回 (meta, 补录日期)；缺失／过期／schema 位不符＝静默 miss，损坏／键不符／形状不符＝通报后 miss。

    schema 位不符按静默处理：条目是升级前的常态，不算损坏（读没读缓存，META 行的 source 已如实留痕）。
    """
    if cache_dir is None:
        return None
    path = meta_cache_path(cache_dir, key)
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        key_ok = entry["key"] == key
        # 旧条目 meta 无 abstractNote，复用会被误读成「Crossref 无摘要」，故 schema 位不符一律 miss
        schema_ok = entry.get("schema") == META_CACHE_SCHEMA
        collected = datetime.fromisoformat(entry["collected_at"])
        meta = entry["meta"]
        expired = now - collected > timedelta(hours=META_TTL_HOURS)
    except FileNotFoundError:
        return None
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as e:
        print(f"WARN meta cache malformed: {path}: {e}", file=sys.stderr)
        return None
    if not key_ok or not isinstance(meta, dict) or not meta.get("title"):
        print(f"WARN meta cache malformed: {path}: key or value shape mismatch", file=sys.stderr)
        return None
    return None if expired or not schema_ok else (meta, entry["collected_at"])


def write_meta_cache(cache_dir, key, meta, collected_at):
    if cache_dir is None:
        return
    path = meta_cache_path(cache_dir, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_name(path.name + ".tmp")
        staging.write_text(
            json.dumps({"schema": META_CACHE_SCHEMA, "key": key, "collected_at": collected_at, "meta": meta},
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        staging.replace(path)
    except OSError as e:  # 缓存故障只通报，不中断补录
        print(f"WARN meta cache write failed: {path}: {e}", file=sys.stderr)


class MetaStore:
    """Crossref 元数据取数：进程内 memo 无条件复用；跨进程按 TTL 缓存（只省读）。"""

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.now = datetime.now().astimezone()
        self.memo = {}

    def get(self, doi):
        """返回 (meta, 补录日期, 来源)；来源 ∈ cache／memo／fetch；取数失败照旧抛出。"""
        key = norm_doi(doi)
        if key in self.memo:
            meta, collected_at, _ = self.memo[key]
            return meta, collected_at, "memo"
        hit = read_meta_cache(self.cache_dir, key, self.now)
        if hit is not None:
            self.memo[key] = (hit[0], hit[1], "cache")
        else:
            collected_at = self.now.isoformat(timespec="seconds")
            meta = crossref_meta(doi)
            write_meta_cache(self.cache_dir, key, meta, collected_at)
            self.memo[key] = (meta, collected_at, "fetch")
        return self.memo[key]


def crossref_meta(doi):
    req = urllib.request.Request(
        CROSSREF + urllib.parse.quote(doi), headers={"User-Agent": UA}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        m = json.load(r)["message"]
    parts = ((m.get("published") or {}).get("date-parts") or [[None]])[0]
    date = "-".join(f"{p:04d}" if i == 0 else f"{p:02d}" for i, p in enumerate(parts))
    creators = [
        {"creatorType": "author", "firstName": a.get("given", ""),
         "lastName": a.get("family") or a.get("name", "")}
        for a in m.get("author", [])
    ]
    creators = [c for c in creators if c["firstName"] or c["lastName"]]  # 空名条目（组织作者）不得进库
    issn = (m.get("ISSN") or [None])[0]
    abstract = abstracts.clean(m.get("abstract"))  # Crossref 摘要为 JATS 片段；清洗口径与取数梯级同一份实现
    item = {
        "itemType": "journalArticle",
        "title": (m.get("title") or [doi])[0],
        "creators": creators,
        "publicationTitle": (m.get("container-title") or [None])[0],
        "volume": m.get("volume"),
        "pages": m.get("page"),
        "date": date,
        "DOI": m.get("DOI"),
        "url": "https://doi.org/" + m.get("DOI", doi),
        "language": "en",
    }
    if issn:
        item["ISSN"] = issn
    if abstract:
        item["abstractNote"] = abstract
    return {k: v for k, v in item.items() if v}


class DedupeMemo:
    """去重结论仅进程内 memo：写库后对应键失效重查；跨运行永不缓存。

    键＝归一 DOI（norm_doi：小写、去前缀），与 find_by_doi 的权威比对键一致。
    """

    def __init__(self, z):
        self.z = z
        self.memo = {}

    def find(self, title, doi):
        key = norm_doi(doi)
        if not key:  # 无标识记录不进 memo，避免空键互相顶替
            return find_by_doi(self.z, title, doi)
        if key not in self.memo:
            self.memo[key] = find_by_doi(self.z, title, doi)
        return self.memo[key]

    def invalidate(self, *dois):
        for doi in dois:
            self.memo.pop(norm_doi(doi), None)


def readback_abstract(item):
    """写后回查的摘要判定（读库内那条，不看本进程的预期）：`ok`／`empty`／`na`／`?`（不在库，无从判定）。

    判定面即 ADR-0021 的缺口口径：文种不适用记 `na`（不算缺口），可摘要文种按 `abstractNote` 是否非空记
    `ok`／`empty`；na 由**库内条目的 itemType** 判，不由本进程的载荷判——回查是建条目后的唯一读数。
    """
    if not item:
        return "?"
    data = item.get("data") or {}
    if not abstracts.applicability(data.get("itemType")):
        return "na"
    return "ok" if (data.get("abstractNote") or "").strip() else "empty"


def main() -> int:
    ap = argparse.ArgumentParser(description="按 DOI 清单批量补录 Zotero 正式条目")
    ap.add_argument("--dois", nargs="*", default=[], help="DOI 清单")
    ap.add_argument("--dois-file", help="每行一个 DOI 的文本文件")
    ap.add_argument("--tag", action="append", default=[], help="附加标签，可重复")
    ap.add_argument("--library-id", default="0", help="本地库固定传 0")
    ap.add_argument("--api-key", default=os.environ.get("ZOTERO_LOCAL_API_KEY"),
                    help="本地写 key，默认读 ZOTERO_LOCAL_API_KEY")
    ap.add_argument("--dry-run", action="store_true", help="只核验去重，不写库")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR),
                    help="Crossref 元数据缓存目录（缺省＝skill 包内 .cache/；须为被忽略的非交付路径）")
    ap.add_argument("--no-cache", action="store_true", help="显式绕过元数据缓存：不读不写（本进程读数照用）")
    ap.add_argument("--abstracts-file", help="「入库摘要表」路径（Step2 产物；不传即不启用表）")
    ap.add_argument("--no-fetch", action="store_true",
                    help="显式关联网补漏：摘要只从入库摘要表与已取到的 Crossref 元数据来")
    args = ap.parse_args()

    dois = list(args.dois)
    if args.dois_file:
        dois += [l.strip() for l in open(args.dois_file, encoding="utf-8")
                 if l.strip() and not l.startswith("#")]
    dois = list(dict.fromkeys(dois))
    if not dois:
        print("no DOIs given", file=sys.stderr)
        return 1

    try:  # 表读错必须炸（ADR-0021）：静默降级成空表会伪装成「全库无摘要」
        # 只取 load_table 首返回值（归一 DOI → 记录）；无摘要清单属 Step2 覆盖率面，取数梯级用不到
        table = abstracts.load_table(args.abstracts_file)[0] if args.abstracts_file else None
    except Exception as e:
        print(f"ABSTRACT-TABLE-FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    allow_fetch = not args.no_fetch

    z = zmod.Zotero(library_id=args.library_id, library_type="user", local=True,
                    local_api_key=args.api_key)
    store = MetaStore(None if args.no_cache else Path(args.cache_dir))
    memo = DedupeMemo(z)
    tags = [{"tag": t} for t in args.tag]
    to_create, bad, migrate = [], [], []
    dup = gap = 0
    try:
        gate = PreflightGate(z, len(dois))
    except Exception as e:  # fail-closed（ADR-0020）：预检故障不得当未命中
        print(f"PRECHECK-FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    for doi in dois:
        try:
            verdict, key = gate.check(doi)
        except Exception as e:  # fail-closed（ADR-0020）：预检异常中止报错
            print(f"PRECHECK-FAIL {doi}: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        if verdict in ("DUP", "DUP-GAP"):
            if verdict == "DUP":
                dup += 1
            else:
                gap += 1
            print(f"{verdict} {doi} key={key}")
            continue
        try:
            meta, collected_at, source = store.get(doi)
        except Exception as e:
            bad.append((doi, f"crossref failed: {e}"))
            continue
        print(f"META {doi} collected={collected_at[:10]} source={source}")
        # 交底：crossref_meta 已把 message.abstract 清洗成 abstractNote（clean 幂等），交的就是「调用方已取到的
        # Crossref 正文」；条目无该字段＝Crossref 确实没有，交 None 跳级、不重发同一条请求
        try:
            abstract_text, abstract_source, abstract_status = abstracts.resolve(
                doi, None, meta.get("itemType"), table, allow_fetch, crossref_handoff=meta.get("abstractNote"))
        except Exception as e:  # 摘要抓取故障不阻断建条目：降级为空，正文稍后可补
            abstract_text, abstract_source, abstract_status = None, "error", "empty"
        print(f"ABS {doi} source={abstract_source} status={abstract_status}")
        if abstract_status == "filled":  # 只写正文；empty／na 一律不写占位文本（ADR-0021）
            meta["abstractNote"] = abstract_text  # Crossref 缓存已在 store.get 内落盘，不回流
        else:  # 载荷服从判定：非 filled 时不得残留 crossref_meta 放进来的正文（否则状态说 na／empty、条目却有正文）
            meta.pop("abstractNote", None)
        if tags:
            meta["tags"] = tags
        try:
            formal, snapshots = memo.find(meta["title"], meta.get("DOI", doi))
        except Exception as e:  # fail-closed：探针故障不当作未命中，记 bad 跳过本条，由重跑收敛
            bad.append((doi, f"dedupe probe failed: {type(e).__name__}: {e}"))
            print(f"ERR {doi} dedupe probe failed: {type(e).__name__}")
            continue
        if formal is not None:  # 慢路径兜底命中（单探针预检漏网时仍须取消）
            key = formal.get("key")
            verdict = "DUP" if has_pdf_child(z, key) else "DUP-GAP"
            if verdict == "DUP":
                dup += 1
            else:
                gap += 1
            print(f"{verdict} {doi} key={key}")
        else:
            print(f"{'PLAN' if args.dry_run else 'NEW '} {doi} {meta['title'][:60]}")
            if snapshots:
                migrate.append((doi, [s.get("key") for s in snapshots]))
            to_create.append(meta)

    for doi, keys in migrate:
        print(f"MIGRATE {doi} snapshots={keys} 建正式条目后手动挂附件再删旧条目")

    if args.dry_run or not to_create:
        print(f"OK plan={len(to_create)} dup={dup} gap={gap} bad={len(bad)}")
        return 1 if bad or migrate else 0

    chunk_size = 50  # 本地 API 单次建条目上限（TooManyItemsError 防护）
    failed: dict[str, dict] = {}
    for start in range(0, len(to_create), chunk_size):
        chunk = to_create[start:start + chunk_size]
        response = z.create_items(chunk, timeout=60)
        for idx, entry in (response.get("failed") or {}).items():
            failed[str(int(idx) + start)] = entry
    for idx, entry in sorted(failed.items(), key=lambda kv: int(kv[0])):
        item = to_create[int(idx)]
        print(f"FAIL-CREATE {item.get('DOI')} code={entry.get('code')} "
              f"{(entry.get('message') or '')[:160]}", file=sys.stderr)
    memo.invalidate(*(m.get("DOI") for m in to_create))
    ok = True
    for meta in to_create:
        try:
            formal, _ = memo.find(meta["title"], meta.get("DOI", ""))
        except Exception:  # 终验探针故障记为 FAIL（exit 1），由重跑收敛
            formal = None
        key = formal.get("key") if formal else None
        print(f"{'OK  ' if key else 'FAIL'} {meta.get('DOI')} key={key} abstract={readback_abstract(formal)}")
        ok = ok and bool(key)
    return 0 if ok and not bad and not migrate else 1


if __name__ == "__main__":
    raise SystemExit(main())
