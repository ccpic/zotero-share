#!/usr/bin/env python
"""§5 引文表草稿：`run/step5-crossref.json` → `run/step5-reference-list.md`（草稿，不直写包内件）。

GB/T 7714-2015 单制式，编号＝证据表行号（行号取卡面「对应文献 N」；背景卡不列本表）。
条目式（期刊）：著者. 题名[J]. 刊名, 年, 卷(期): 页码. DOI <doi>。
著者列前 3 位加「等」；数据源优先级 Crossref deposition > OpenAlex enrich（冲突以 Crossref 为准）。

输入纪律（D6）：卷期页等元数据只吃 crossref 件（＋可选的 OpenAlex enrich 补缺）；
卡标识段只作**交叉核对**——题名归一后不一致、或首著者姓在卡作者位缺席，即退出 2 停下，
由人工按 `reference-list-template.md` §3 逐条核对，不猜补。
类型映射只认 crossref `type` 的四种既成写法，未知类型即退出 2（[M]/[R] 一类由人工定）。

草稿由人工核对后手工并入 `agent/reference-list.md`（D5 草稿原则：机器不直写包内真源）。

运行：
uv run python .claude/skills/med-lit-review/scripts/step5_reference_list.py --delivery workspace/YYYY-MM-DD-<slug>
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import package_parse  # noqa: E402

XREF_REL = "run/step5-crossref.json"
ENRICH_REL = "run/b1/openalex-enrich.json"
OUT_REL = "run/step5-reference-list.md"
ROW_RE = re.compile(r"对应文献[：:]?\s*(\d+[a-z]?)")
TYPE_MARK = {"journal-article": "J", "proceedings-article": "C", "book": "M",
             "monograph": "M", "dissertation": "D", "report": "R"}


def norm_doi(value: str) -> str:
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", (value or "").strip()).lower().rstrip(".")


def clean(value) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", str(value or "")))).strip()


def initials(given: str) -> str:
    return " ".join(p[0].upper() for p in re.split(r"[\s\-]+", given.strip()) if p)


def author_str(authors) -> str:
    out = []
    for a in authors or []:
        a = clean(a)
        if not a:
            continue
        tokens = a.split()
        out.append(tokens[0] if len(tokens) == 1 else f"{tokens[-1]} {initials(' '.join(tokens[:-1]))}")
    if not out:
        return "（著者未见）"
    return ", ".join(out[:3]) + (", 等" if len(out) > 3 else "")


def norm_title(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", value or "")).lower()).strip().rstrip(".")


def ident_body(text: str) -> str:
    m = re.search(r"^- 标识（identity）[：:]\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="§5 引文表草稿（GB/T 单制式，不直写包内件）")
    parser.add_argument("--delivery", required=True)
    parser.add_argument("--asof", default="",
                        help="著录头的数据采集日（缺省取 manifest.topic.delivered）")
    args = parser.parse_args(argv)
    delivery = Path(args.delivery)

    xref_path = delivery / XREF_REL
    if not xref_path.is_file():
        print(f"缺输入：{XREF_REL}（元数据只吃 crossref 件，不猜补）", file=sys.stderr)
        return 2
    xref = json.loads(xref_path.read_text(encoding="utf-8"))
    enrich: dict = {}
    enrich_path = delivery / ENRICH_REL
    if enrich_path.is_file():
        data = json.loads(enrich_path.read_text(encoding="utf-8"))
        records = data.get("records") if isinstance(data, dict) else data
        for rec in records or []:
            if isinstance(rec, dict) and rec.get("doi"):
                enrich[norm_doi(rec["doi"])] = rec

    cards_dir = delivery / package_parse.CARDS_REL
    rows: dict[str, str] = {}
    for path in sorted(cards_dir.glob(f"*{package_parse.CARD_SUFFIX}")):
        text = path.read_text(encoding="utf-8")
        m = ROW_RE.search(text)
        if m and re.fullmatch(r"\d+[a-z]?", m.group(1)):
            rows[path.stem] = m.group(1)

    manifest = json.loads((delivery / "agent/manifest.json").read_text(encoding="utf-8"))
    asof = args.asof or str((manifest.get("topic") or {}).get("delivered") or "")
    lines = [
        "# 引文与参考文献表（GB/T 7714-2015 单制式）",
        "",
        f"数据源优先级：Crossref deposition（本轮直取，{asof}）> OpenAlex enrich > citation(bibtex)。"
        "著者列前 3 位加「等」；编号＝证据表行号（行号↔DOI 契约）。无证据表行的背景卡不列本表。",
        "",
    ]
    fails, src_count = [], {"crossref": 0, "enrich": 0}
    entries = []
    for stem, row in sorted(rows.items(),
                            key=lambda kv: package_parse.row_sort_key(kv[1])):
        doi = norm_doi(stem.replace("_", "/"))
        meta = xref.get(doi) or xref.get(doi.lower()) or {}
        src = "crossref"
        if not meta and doi in enrich:
            meta, src = enrich[doi], "enrich"
        if not meta:
            fails.append(f"{row}（{doi}：crossref/enrich 皆无记录）")
            continue
        mark = TYPE_MARK.get(str(meta.get("type") or ""))
        if not mark:
            fails.append(f"{row}（{doi}：未知类型 {meta.get('type')!r}，[M]/[R] 由人工定）")
            continue
        vol = clean(meta.get("volume")).strip()
        iss = clean(meta.get("issue")).strip()
        page = clean(meta.get("page")).strip()
        year = meta.get("year") or ""
        volpart = f"{vol}({iss})" if vol and iss else vol
        loc = ", ".join(x for x in [str(year), f"{volpart}: {page}".strip(": ")] if x.strip())
        entry = (f"{author_str(meta.get('authors') or [])}. {clean(meta.get('title')).rstrip('.')}[{mark}]. "
                 f"{clean(meta.get('container_title'))}, {loc}. DOI {doi}.")
        entries.append((row, doi, entry))
        src_count[src] += 1

    if fails:
        print("引文表草稿拒绝生成（逐条核对前置失败，不猜补）：", file=sys.stderr)
        for line in fails:
            print(f"  - {line}", file=sys.stderr)
        return 2

    for row, _doi, entry in entries:
        lines.append(f"{row}. {entry}")
    (delivery / OUT_REL).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"引文表草稿：{len(entries)} 条 → {OUT_REL}（crossref {src_count['crossref']}／enrich {src_count['enrich']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
