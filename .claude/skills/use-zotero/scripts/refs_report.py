"""某篇论文的参考文献对照表：Crossref 取 reference DOI → OpenAlex 批量取元数据与被引数 → 库内 DOI 比对判在库 → 默认按被引数降序出表。

When: 已知一篇论文 DOI，需列其参考文献（标题/作者/被引数/是否在库）时。
Do: python refs_report.py --doi <DOI> [--sort cites|orig] [--top N] [--output refs.md]
Why: 有 DOI 时读 Crossref deposition 的结构化 reference[].DOI，比解析 PDF 双栏文本更完整不断裂；OpenAlex 一次 filter 批量取 cited_by_count，避免逐篇循环；在库判定只认 DOI 字段（大小写与前缀无关），附件文本命中不算收录。
只读，不写库。退出码 0=成功；1=取数失败或无参考文献。
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request

from pyzotero import zotero as zmod

CROSSREF = "https://api.crossref.org/works/"
OPENALEX = "https://api.openalex.org/works"
UA = "zotero-seed/1.0 (mailto:research@local)"  # 联系邮箱按需替换
NON_FORMAL = ("attachment", "note", "annotation")
DOI_PREFIX_RE = re.compile(r"^https?://(dx\.)?doi\.org/", re.I)


def norm_doi(text):
    """归一 DOI：小写、去前缀（与 seed_from_doi.py 同式，本包内各脚本独立实现）。"""
    return DOI_PREFIX_RE.sub("", (text or "").strip()).lower()


def http_json(url, params=None):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def clean_title(t):
    t = re.sub(r"</?[^>]+>", "", t or "")
    return re.sub(r"\s+", " ", t).strip()


def crossref_ref_dois(doi):
    m = http_json(CROSSREF + urllib.parse.quote(doi))["message"]
    seen, out = set(), []
    for r in m.get("reference", []) or []:
        d = (r.get("DOI") or "").strip()
        if d and d.lower() not in seen:
            seen.add(d.lower())
            out.append(d)
    return out


def openalex_batch(dois):
    found = {}
    for i in range(0, len(dois), 50):
        chunk = dois[i:i + 50]
        r = http_json(OPENALEX, {
            "filter": "doi:" + "|".join(chunk),
            "select": "doi,title,publication_year,primary_location,locations,cited_by_count,authorships",
            "per-page": 50,
        })
        for w in r.get("results", []):
            key = (w.get("doi") or "").lower().replace("https://doi.org/", "")
            if key:
                found[key] = w
    return found


def strip_orcid(name):
    """OpenAlex 作者名偶带 ORCID 残留（如 'Marco; https://orcid.org/... Roffi'）。"""
    s = re.sub(r"\s*https?://orcid\.org/\S+", "", name or "").strip()
    parts = [p.strip() for p in s.split(";") if p.strip()]
    if len(parts) == 2 and len(parts[0].split()) == 1 and len(parts[1].split()) == 1:
        return parts[0] + " " + parts[1]  # 名在前姓在后：'Marco; Roffi' → 'Marco Roffi'
    if len(parts) == 2 and len(parts[0].split()) > 1 and len(parts[1].split()) == 1:
        return parts[1] + " " + parts[0]  # 姓在前名在后：'Roffi; Marco' → 'Marco Roffi'
    return s


def crossref_meta(doi):
    """Crossref deposition 的标题/作者/期刊/年份（OpenAlex 缺失或降级时的权威来源）。"""
    m = http_json(CROSSREF + urllib.parse.quote(doi))["message"]
    title = clean_title((m.get("title") or [doi])[0])
    authors = [strip_orcid((a.get("given", "") + " " + a.get("family", "")).strip())
               for a in m.get("author", [])]
    authors = [a for a in authors if a]
    venue = (m.get("container-title") or [None])[0]
    parts = ((m.get("published") or {}).get("date-parts") or [[None]])[0]
    year = parts[0] if parts and parts[0] else None
    return title, authors, venue, year


def journal_venue(w):
    locs = [w.get("primary_location") or {}] + list(w.get("locations") or [])
    for loc in locs:
        src = loc.get("source") or {}
        if src.get("type") == "journal" and src.get("display_name"):
            return src["display_name"]
    for loc in locs:
        name = (loc.get("source") or {}).get("display_name")
        if name:
            return name
    return None


def first_author_label(authors):
    if not authors:
        return "?"
    return authors[0] + (" 等" if len(authors) > 1 else "")


def library_lookup(z, doi, limit=25):
    """DOI 检索 + 归一 DOI 字段比对；返回 (key_or_None, ok)。"""
    try:
        try:
            hits = z.items(q=doi, qmode="everything", limit=limit)
        except TypeError:  # 旧版 pyzotero 无 qmode 参数
            hits = z.items(q=doi, limit=limit)
    except Exception as e:
        print(f"WARN zotero lookup failed {doi}: {e}", file=sys.stderr)
        return None, False
    for x in hits:
        dd = x.get("data", {})
        if norm_doi(dd.get("DOI") or "") != norm_doi(doi):
            continue
        if dd.get("itemType") in NON_FORMAL:
            continue
        return x.get("key"), True
    return None, True


def main() -> int:
    ap = argparse.ArgumentParser(description="论文参考文献对照表（默认按被引数降序）")
    ap.add_argument("--doi", required=True, help="待查论文的 DOI")
    ap.add_argument("--sort", default="cites", choices=("cites", "orig"),
                    help="cites=按被引数降序（默认）；orig=保持 Crossref 原序")
    ap.add_argument("--top", type=int, default=0, help="只取前 N 篇，0=全部")
    ap.add_argument("--output", help="同时写 markdown 文件路径")
    ap.add_argument("--library-id", default="0", help="本地库固定传 0")
    args = ap.parse_args()

    try:
        ref_dois = crossref_ref_dois(args.doi)
    except Exception as e:
        print(f"crossref failed: {e}", file=sys.stderr)
        return 1
    if not ref_dois:
        print("no references with DOI in crossref deposition", file=sys.stderr)
        return 1

    try:
        works = openalex_batch(ref_dois)
    except Exception as e:
        print(f"openalex failed: {e}", file=sys.stderr)
        return 1

    z = zmod.Zotero(library_id=args.library_id, library_type="user", local=True)

    rows = []
    for pos, d in enumerate(ref_dois, 1):
        w = works.get(d.lower())
        title, authors, year, venue, cites = "", [], None, None, None
        if w:
            title = clean_title(w.get("title") or "")
            authors = [strip_orcid(a) for a in
                       (a.get("author", {}).get("display_name", "")
                        for a in w.get("authorships", []))]
            authors = [a for a in authors if a]
            year = w.get("publication_year")
            venue = journal_venue(w)
            cites = w.get("cited_by_count")
        # OpenAlex 记录缺标题/作者，或期刊年份明显降级（如机构仓储代实际期刊）时回退 Crossref deposition。
        oa_venue_ok = bool(venue) and "epository" not in venue and "rchive" not in venue and "PubMed" not in venue
        oa_year_ok = bool(year) and 1800 < year <= 2100
        if not title or not authors or not oa_venue_ok or not oa_year_ok:
            try:
                ft, fa, fv, fy = crossref_meta(d)
                title = title or ft
                authors = authors or fa
                venue = venue if oa_venue_ok else (fv or venue)
                year = year if oa_year_ok else (fy or year)
            except Exception as e:
                print(f"WARN crossref fallback failed {d}: {e}", file=sys.stderr)
        key, ok = library_lookup(z, d)
        vy = " ".join(s for s in (venue or "", str(year or "")) if s).strip() or "?"
        cell = "?" if not ok else (f"✓ {key}" if key else "✗")
        rows.append({"pos": pos, "title": title or d,
                     "first": first_author_label(authors),
                     "vy": vy, "doi": d, "cites": cites,
                     "cell": cell, "key": key, "ok": ok})

    if args.sort == "cites":
        rows.sort(key=lambda r: (r["cites"] is None, -(r["cites"] or 0), r["pos"]))
    if args.top and args.top > 0:
        rows = rows[:args.top]

    def esc(s):
        return (s or "").replace("|", "\\|").replace("\n", " ")

    lines = ["| # | 文献名称 | 第一作者 | 期刊年份 | DOI | 被引数 | 在库 |",
             "|---|---|---|---|---|---|---|"]
    for n, r in enumerate(rows, 1):
        cites_s = "?" if r["cites"] is None else str(r["cites"])
        lines.append(f"| {n} | {esc(r['title'])} | {esc(r['first'])} "
                     f"| {esc(r['vy'])} | {r['doi']} | {cites_s} | {r['cell']} |")
    unknown = sum(1 for r in rows if not r["ok"])
    in_lib = sum(1 for r in rows if r["key"])
    report = "\n".join(lines) + f"\n\n共 {len(rows)} 篇，在库 {in_lib} 篇" \
        + (f"，{unknown} 篇在库状态未知" if unknown else "") + "。"
    print(report)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
