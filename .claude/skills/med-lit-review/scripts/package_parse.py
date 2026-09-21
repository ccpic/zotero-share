#!/usr/bin/env python
"""包面与卡面共享解析：一份实现、两处消费（`evals/acceptance/run_gate.py` 与 `scripts/check_delivery.py`）。

四类能力：卡目录解析（`agent/cards/*.md` 一文件一卡）／行↔卡索引／§5 七列表解析／正文行指针与节锚解析。
口径（本模块只实现、不复述）：`references/protocols/workspace-guide.md` §6（进版表：单卡 canonical）、
§8 `R3`（卡片单形态：`agent/cards/<DOI-slug>.md` 唯一家、无派生合卡）、§11（抽跑只判解析器健康）。

防再犯口径：卡面解析条数须 ＝ `agent/cards/*.md` 件数；0 卡与目录缺各抛 `CardParseError`，文案含
读取路径与解析条数——替代「行 1 没有对应提取卡」那类误导性首错（先报解析面，再报行↔卡面）。
"""
from __future__ import annotations

import re
from pathlib import Path

PACKAGE_REL = "agent/knowledge-package.md"  # 八节包（§5 证据表 canonical）在交付内的落点
CARDS_REL = "agent/cards"  # 单卡 canonical 家（`agent/` 唯一子目录）
CARD_SUFFIX = ".md"
CARDS_GLOB = f"{CARDS_REL}/*{CARD_SUFFIX}"  # 读取路径写法（报错文案与抽跑读数共用）
CARD_HEAD_RE = re.compile(r"^#{1,6}[ \t]*提取卡([^\n]*)$", re.M)  # 每文件主标题（一文件一卡）
# DOI 容许括号段（`10.1016/s0140-6736(25)00721-4` 一类）——旧字符类遇 `(` 即截断，是 E-12「挂行不齐」假红的根因
DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s，,;；|｜）)（(\]]+(?:\([^)\s]*\)[^\s，,;；|｜）)（(\]]*)*)")
NUM_RE = re.compile(r"(BLA \d+|EU/1/\d+/\d+)")


class CardParseError(Exception):
    """卡面解析失败：文案含读取路径与解析条数（0 卡／目录缺／件数不符各一种，见 `parse_cards`）。"""


# ---------------------------------------------------------------- 通用解析（文本级）


def norm_doi(text):
    return (text or "").strip().rstrip("。，,;；)）]").lower()


def doi_slug(doi):
    """DOI → 卡文件名 stem（`agent/cards/<DOI-slug>.md`）：小写；`/`、括号一类非 `[a-z0-9.-]` 段归一为 `_`。

    口径以在场卡文件为准（`-` 保留、`(` `)` 归一为 `_`，如 `10.1016/S0140-6736(22)02036-0` →
    `10.1016_s0140-6736_22_02036-0`）；`doi_slugs` 再补 INDEX 寻址命令写的「只换 `/`」写法。
    """
    return re.sub(r"[^a-z0-9.-]+", "_", norm_doi(doi))


def doi_slugs(doi):
    """DOI 的候选卡文件名 stem（两种既成写法都认：括号归一为 `_`、只把 `/` 换 `_`）。"""
    normalized = norm_doi(doi)
    return {doi_slug(normalized), normalized.replace("/", "_")}


def row_sort_key(row_no):
    """行号排序键：`1`、`12`、`13a` 一类混排也按数字＋字母后缀定序（不因非纯数字崩）。"""
    m = re.match(r"(\d+)([a-z]?)", str(row_no))
    return (int(m.group(1)), m.group(2)) if m else (10 ** 6, str(row_no))


def split_h2(md):
    """按二级标题切节，返回 [(标题, 正文)]。"""
    parts = re.split(r"^##\s+", md, flags=re.M)[1:]
    out = []
    for part in parts:
        title, _, body = part.partition("\n")
        out.append((title.strip(), body))
    return out


def find_h2(md, key):
    for title, body in split_h2(md):
        if key in title:
            return body
    return None


def table_cells(line):
    """表格行 → 单元格（去首尾竖线与空白）。"""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def is_separator(cells):
    """分隔行判定（`|---|---|` 一类）：全单元格由 `-`／`:`／空格组成。"""
    return bool(cells) and all(set(c) <= set("-: ") and c for c in cells)


def md_tables_with_header(block):
    """解析 markdown 表格：返回 [(表头, 数据行)]（分隔行跳过）。"""
    tables, header, rows, in_table = [], None, [], False
    for line in block.splitlines():
        s = line.strip()
        if s.startswith("|") and s.count("|") >= 3:
            cells = table_cells(s)
            if not in_table:
                in_table, header, rows = True, cells, []
                continue
            if is_separator(cells):
                continue
            rows.append(cells)
            continue
        if in_table:
            tables.append((header or [], rows))
            header, rows, in_table = None, [], False
    if in_table:
        tables.append((header or [], rows))
    return tables


def md_tables(block):
    """解析 markdown 表格：返回每个表格的数据行（跳过表头与分隔行）。"""
    return [rows for _, rows in md_tables_with_header(block)]


def md_table(block):
    return [row for table in md_tables(block) for row in table]


def evidence_rows(package_md):
    """§5 七列表行：{行号: {文献, 设计, 人群, 干预, 结局, 偏倚, 关联, doi, 文号}}。"""
    body = find_h2(package_md, "证据表") or ""
    table = re.split(r"^###\s", body, flags=re.M)[0]
    rows = {}
    for cells in md_table(table):
        if not re.fullmatch(r"\d+[a-z]?", cells[0]):
            continue
        lit = cells[1] if len(cells) > 1 else ""
        doi = DOI_RE.search(lit)
        rows[cells[0]] = {
            "文献": lit,
            "设计": cells[2] if len(cells) > 2 else "",
            "人群": cells[3] if len(cells) > 3 else "",
            "干预": cells[4] if len(cells) > 4 else "",
            "结局": cells[5] if len(cells) > 5 else "",
            "偏倚": cells[6] if len(cells) > 6 else "",
            "关联": cells[7] if len(cells) > 7 else "",
            "doi": norm_doi(doi.group(1)) if doi else "",
            "num": NUM_RE.findall(lit),
        }
    return rows


# ---------------------------------------------------------------- §5.1 优先精读序（口径见证据表模板 §5.1）


PRIORITY_HEAD_RE = re.compile(r"(?m)^###\s+§?\s*5\.1\b[^\n]*$")
PRIORITY_HEAD_LINE_RE = re.compile(r"^###\s+§?\s*5\.1\s*(.*)$")
PRIORITY_BOUNDARY_RE = re.compile(r"(?m)^(?:##\s|###\s)")
PRIORITY_GROUP_RE = re.compile(r"(?m)^####\s+(.*)$")
# 行指针：契约记号 `行 N`；读者面词「文献 N」（ADR-0028）与旧写法「证据行 N」都认——读者面词是契约记号的超集，子串匹配天然兼容
ROW_POINTER_RE = re.compile(r"(?:文献|证据行|行)\s*(\d+[a-z]?)")


def block_of_priority(package_md):
    """§5.1 子段原文（不含标题行）与标题；缺席＝(None, "")。"""
    m = PRIORITY_HEAD_RE.search(package_md)
    if m is None:
        return None, ""
    title = PRIORITY_HEAD_LINE_RE.match(m.group(0)).group(1).strip()
    rest = package_md[m.end():]
    nxt = PRIORITY_BOUNDARY_RE.search(rest)
    return (rest[:nxt.start()] if nxt else rest), title


def strip_priority_section(body):
    """从 §5 正文摘除 §5.1 子段：该层在人读主件里独立成区（§6 后），不在 §5 内二次渲染。"""
    m = PRIORITY_HEAD_RE.search(body)
    if m is None:
        return body
    rest = body[m.end():]
    nxt = PRIORITY_BOUNDARY_RE.search(rest)
    return body[:m.start()] + (rest[nxt.start():] if nxt else "")


def _priority_entry(cells, header, group, rank_col):
    """一行 → 条目：列按表头名取（`序`／`文献`／`行`／`证据行`／`卡`／`维度`／`理由`），缺 `理由` 列的表不认。"""
    def col(*names):
        return next((i for i, cell in enumerate(header) if cell.strip() in names), None)

    reason_at = col("理由")
    if reason_at is None or reason_at >= len(cells):
        return None
    row_at = col("文献", "行", "证据行")
    row_cell = cells[row_at] if row_at is not None and row_at < len(cells) else ""
    m = ROW_POINTER_RE.search(row_cell) or re.fullmatch(r"(\d+[a-z]?)", row_cell.strip())
    if not m:
        return None
    rank = None
    if rank_col is not None:
        rank_at = col("序", "名次")
        if rank_at is not None and rank_at < len(cells) and cells[rank_at].strip().isdigit():
            rank = int(cells[rank_at].strip())
    card_at, dim_at = col("卡"), col("维度")
    return {
        "rank": rank, "row": m.group(1), "group": group,
        "card": cells[card_at].strip() if card_at is not None and card_at < len(cells) else "",
        "dim": cells[dim_at].strip() if dim_at is not None and dim_at < len(cells) else "",
        "reason": cells[reason_at].strip(),
    }


def reading_priority(package_md):
    """§5.1 优先精读序解析 → {"present", "title", "sequence", "prose", "first", "groups", "entries"}。

    形态（唯一原文在 `references/templates/evidence-table-template.md` §5.1）：先读层表带 `序` 列（名次只此一处），
    其后层表无 `序` 列、按 `####` 主题分组。`sequence` 保留真源块序（散文／先读层／分组），供渲染器照原文渲染；
    `first`／`groups`／`entries` 是聚合读数，供门禁 W-06 与软项读数消费。本函数只做形态解析，不判依据序
    （排序正确性按 07 §7 不入机器门禁）。
    """
    block, title = block_of_priority(package_md)
    if block is None:
        return {"present": False, "title": "", "sequence": [], "prose": [],
                "first": [], "groups": [], "entries": []}
    sequence, prose, first, groups, entries = [], [], [], [], []
    prose_buf, table_lines, current_group = [], [], None

    def flush_prose():
        nonlocal prose_buf
        if prose_buf:
            prose.extend(prose_buf)
            sequence.append({"kind": "prose", "lines": list(prose_buf)})
            prose_buf = []

    def flush_table():
        nonlocal table_lines, current_group
        if not table_lines:
            return
        rows = []
        for line in table_lines:
            cells = table_cells(line)
            if is_separator(cells):
                continue
            rows.append(cells)
        table_lines = []
        if not rows:
            return
        header, data = rows[0], rows[1:]
        rank_col = next((i for i, cell in enumerate(header) if cell.strip() in ("序", "名次")), None)
        bucket = []
        for cells in data:
            entry = _priority_entry(cells, header, current_group or "", rank_col)
            if entry is None:
                continue
            entries.append(entry)
            bucket.append(entry)
            if rank_col is not None:
                first.append(entry)
        if not bucket:
            return
        if rank_col is not None:
            sequence.append({"kind": "first", "entries": bucket})
        else:
            label = current_group or ""
            if label not in [group["label"] for group in groups]:
                groups.append({"label": label, "entries": []})
            next(group for group in groups if group["label"] == label)["entries"].extend(bucket)
            sequence.append({"kind": "group", "label": label, "entries": bucket})

    for line in block.splitlines():
        stripped = line.strip()
        m = PRIORITY_GROUP_RE.match(line)
        if m:
            flush_prose()
            flush_table()
            current_group = m.group(1).strip()
            continue
        if stripped.startswith("|"):
            flush_prose()
            table_lines.append(line)
            continue
        flush_table()
        if stripped:
            prose_buf.append(stripped)
        else:
            flush_prose()
    flush_prose()
    flush_table()
    return {"present": True, "title": title or "优先精读序", "sequence": sequence,
            "prose": prose, "first": first, "groups": groups, "entries": entries}


def prose_row_refs(package_md):
    """包内正文（去掉表格行）里出现的所有行号引用与区间，返回 [(行号, 上下文)]。"""
    refs = []
    for line in package_md.splitlines():
        if line.strip().startswith("|"):
            continue
        for m in re.finditer(r"(?:文献|证据行|行)\s*((?:\d+[a-z]?)(?:\s*[–\-]\s*\d+[a-z]?)?(?:\s*[／、,，]\s*\d+[a-z]?)*)", line):
            refs.append((m.group(1), line.strip()[:90]))
    return refs


def expand_row_ref(expr):
    """把 '11'、'13–21'、'11／13–21／23、25' 展开成行号集合。"""
    ids = set()
    for part in re.split(r"[／、,，]", expr):
        part = part.strip()
        m = re.fullmatch(r"(\d+[a-z]?)\s*[–\-]\s*(\d+[a-z]?)", part)
        if m:
            a, b = m.group(1), m.group(2)
            if a.isdigit() and b.isdigit():
                ids.update(str(n) for n in range(int(a), int(b) + 1))
            else:
                ids.update({a, b})
        elif re.fullmatch(r"\d+[a-z]?", part):
            ids.add(part)
    return ids


# ---------------------------------------------------------------- 卡目录解析（`agent/cards/*.md`）


def card_files(delivery):
    """卡目录内的卡文件（`agent/cards/*.md`，按名排序）；目录缺＝空表（报错归 `parse_cards`）。"""
    cards_dir = Path(delivery) / CARDS_REL
    return sorted(cards_dir.glob(f"*{CARD_SUFFIX}")) if cards_dir.is_dir() else []


def parse_cards(delivery):
    """卡目录解析：{卡号: {doi, num, 全文状态, 设计句, 偏倚句, 声明句, 行, 无行声明, 别名, 页码范围标注, slug, 文件}}。

    一文件一卡、按每文件主标题（`#…提取卡 …`）解析；解析条数须 ＝ 卡文件件数——目录缺、0 卡
    （目录在场无卡文件，或卡文件皆无主标题）、件数不符各抛 `CardParseError`，文案含读取路径与解析条数。
    `卡号` 取自主标题（重复时加 `′` 保唯一，不静默覆盖）；`文件名即身份`：`slug` 记文件 stem（行↔卡主索引）。
    """
    delivery = Path(delivery)
    cards_dir = delivery / CARDS_REL
    if not cards_dir.is_dir():
        raise CardParseError(
            f"卡目录缺：{CARDS_REL}（读取路径 {CARDS_GLOB}；解析条数 0）——卡片单形态要求该目录为唯一家"
        )
    files = card_files(delivery)
    out, unparsed = {}, []
    for path in files:
        text = path.read_text(encoding="utf-8")
        m = CARD_HEAD_RE.search(text)
        if m is None:
            unparsed.append(path.name)
            continue
        head = m.group(1)
        body = text[m.end():]
        head_clean = head.strip().lstrip("：:").strip()
        card_id = re.split(r"[：:（(]", head_clean)[0].strip() or head_clean or "未编号卡"
        while card_id in out:  # 卡号重复时加序号，防 dict 静默覆盖（模板允许「短引」当卡号）
            card_id += "′"
        doi = DOI_RE.search(body)
        num = NUM_RE.search(body)
        declared = re.search(r"(?:对应文献|拟证据表行)[^\n]*", body)
        line_decl = declared.group(0) if declared else ""
        row_expr = re.search(r"(?:对应文献|拟证据表行)\s*[:：]?\s*((?:\d+[a-z]?)(?:\s*[／/、,，]\s*\d+[a-z]?)*)", line_decl)
        rows = re.findall(r"\d+[a-z]?", row_expr.group(1)) if row_expr else []
        out[card_id] = {
            "doi": norm_doi(doi.group(1)) if doi else "",
            "num": num.group(1) if num else "",
            "全文状态": head,
            "设计句": next((l for l in body.splitlines() if l.startswith("- 设计与")), ""),
            "偏倚句": next((l for l in body.splitlines() if l.startswith("- 偏倚")), ""),
            "声明句": line_decl,
            "行": rows,
            "无行声明": bool(re.search(r"无(?:拟)?证据表行|无对应文献|(?:对应文献|拟证据表行)\\s*[:：]?\\s*无|不进(?:证据)?表", body)),
            "别名": "不重复建卡" in body or "同一文献" in body,
            "页码范围标注": bool(re.search(r"(PDF p\.|p\.\d|摘要|官网题录)", body)),
            "slug": path.stem,
            "文件": f"{CARDS_REL}/{path.name}",
        }
    if not out:
        raise CardParseError(
            f"卡面解析到 0 张卡：{CARDS_REL}（读取路径 {CARDS_GLOB}；解析条数 0；卡文件 {len(files)} 件）"
        )
    if unparsed:
        raise CardParseError(
            f"卡面解析条数 {len(out)} ≠ 卡文件件数 {len(files)}（读取路径 {CARDS_GLOB}；"
            f"无主标题件：{'、'.join(unparsed[:3])}{'…' if len(unparsed) > 3 else ''}）"
        )
    return out


# ---------------------------------------------------------------- 行↔卡索引


def card_index(cards_):
    """行↔卡索引三级表：`(by_slug, by_doi, by_num)`——文件名为身份主索引，卡内 DOI 与文号各一级。"""
    by_slug, by_doi, by_num = {}, {}, {}
    for cid, card in cards_.items():
        if card.get("slug"):
            by_slug.setdefault(card["slug"], []).append(cid)
        if card.get("doi"):
            by_doi.setdefault(card["doi"], []).append(cid)
        if card.get("num"):
            by_num.setdefault(card["num"], []).append(cid)
    return by_slug, by_doi, by_num


def row_card_map(rows, cards_):
    """行↔卡映射 {行号: [卡号]}：文件名的候选 DOI-slug、卡内 DOI、文号三处命中取并集（同一卡只留一次）；别名卡不参与行归属。"""
    by_slug, by_doi, by_num = card_index(cards_)
    row_cards = {}
    for rid, row in rows.items():
        ids = []
        if row["doi"]:
            ids += by_doi.get(row["doi"], [])
            for slug in doi_slugs(row["doi"]):
                ids += by_slug.get(slug, [])
        for num in row["num"]:
            ids += by_num.get(num, [])
        seen, uniq = set(), []
        for cid in ids:
            if cid not in seen:
                seen.add(cid)
                uniq.append(cid)
        row_cards[rid] = [cid for cid in uniq if not cards_[cid]["别名"]]
    return row_cards
