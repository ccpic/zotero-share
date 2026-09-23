"""§5.1 优先精读序草稿：名单与组序（草稿，不直写包内件；理由与维度由人工填）。"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import package_parse  # noqa: E402
from reader_html import split_subs  # noqa: E402

OUT_REL = "run/step5-priority.md"
FIRST_MIN, FIRST_MAX = 3, 5
ROW_POINTER_RE = re.compile(r"(?:文献|证据行|行)\s*(\d+[a-z]?)")
LEVEL_RE = re.compile(r"→\s*E([1-5])|·\s*E([1-5])")
RELATION_RANK = {"直接支撑结论": 0, "规范性依据": 2}
RELATION_TENSION = ("张力", "对话", "存在分歧", "tension")
THEME_RE = re.compile(r"§(4\.\d+)")


def relation_rank(rel: str) -> int:
    """主关系位次：只看主关系（「直接支撑结论」在最前即 0）。行内兼标的次要关系不参与比较。"""
    head = rel.split("；", 1)[0]
    if "直接支撑结论" in head:
        return 0
    if any(k in head for k in RELATION_TENSION):
        return 1
    if "规范性依据" in head:
        return 2
    return 3


def relation_of(text: str) -> str:
    m = re.search(r"^- 与本文关系[^\n]*", text, re.M)
    return m.group(0) if m else ""


def level_of_design(cell: str) -> str:
    """P4 决议：依据④的定级后 E 级读 §5「设计与等级」列——`→ EX（定级` 在先，
    无则退回 `· EX` 先验（模板：`类型 · E1–E5`（定级后，非初判））。

    卡全文到处是 E 记号（偏倚段工具名 `RoB 2.0`、升降档讨论里的 `E1/E2`），首个
    记号经常不是定级——读卡全文会把 15/32 判错。本函数只吃设计列整格。
    """
    hit = re.search(r"→\s*E([1-5])", cell)
    if hit:
        return "E" + hit.group(1)
    hit = re.search(r"·\s*E([1-5])", cell)
    return "E" + hit.group(1) if hit else "E9"


def level_of(text: str) -> str:
    """卡「设计与E级」行的定级（交叉核对用；与 `level_of_design` 不一致即打印）。"""
    m = re.search(r"^- 设计与E级[^\n]*", text, re.M)
    return level_of_design(m.group(0) if m else "")


def theme_of(text: str) -> str:
    line = relation_of(text)
    m = THEME_RE.search(line)
    return m.group(1) if m else ""


def cited_counts(delivery: Path) -> dict:
    """被引数（P2 决议）：只吃 `run/b1/cited-counts.json` 的 OpenAlex 批量实查快照。

    `openalex-enrich.json` 的 `cited_by_count` 是排序富集链的碰撞串行、不是文献真实被引，
    不得作回退（否则同一文献出现两套被引口径）。缺失记「被引未取到」，该轮排最后。
    """
    out = {}
    path = delivery / "run/b1/cited-counts.json"
    if not path.is_file():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return out
    items = data.get("counts") if isinstance(data, dict) else None
    if isinstance(items, dict):
        for doi, cites in items.items():
            if isinstance(cites, int):
                out[str(doi).lower()] = cites
    return out


def load_sort_order(delivery: Path) -> dict:
    """S 序（`run/b1/sort-report.json` 的 order）→ {DOI: order}；缺件即空表（该轮不参与排序）。"""
    path = delivery / "run/b1/sort-report.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    rows = data.get("rows") if isinstance(data, dict) else None
    out = {}
    for row in rows or []:
        if isinstance(row, dict) and row.get("key") is not None and row.get("order") is not None:
            out[str(row["key"]).lower()] = row["order"]
    return out
def wrap_lines(lines: list[str], width: int = 80) -> list[str]:
    """长段落软换行（只在 CJK／标点边界处断行，不增删任何字符）。

    逐行正则与逐行成链只看「行」：`review_knowledge.py` 的断言区规则（L-03／R3-01／
    L-08／M-01／M-02）与 `reader_html.py` 的 `core_excerpt`（`.{20,420}?` 行内匹配）
    都假设散文是短行书写的。超长单行（本次实测 §6.2 整段 1368 字节一行）会让 L-03
    整段豁免、摘录正则跨段失效，且与 R-02／F-03 的按行读数口径不一致。
    """
    out: list[str] = []
    for line in lines:
        while len(line) > width:
            cut = max([i for i, ch in enumerate(line[:width])
                       if "\u4e00" <= ch <= "\u9fff" or ch in "，。；：、）】”’！？"] or [0])
            cut = cut + 1 if cut else width
            out.append(line[:cut])
            line = line[cut:]
        out.append(line)
    return out


def refs_in(text: str) -> set:
    """块内行指针集合：逐个 `（…）`／`｜…｜` 括号各计一票（括号是引用条目的边界）。

    同一括号内 `（行 15、行 7）` 计两票；同一指针在块内多个括号出现只计一票
    （散文复述同一行不刷票——P1 决议）。
    """
    votes: set[str] = set()
    for m in re.finditer(r"[（(｜]([^）)｜]+)[）)｜]", text):
        for num in re.findall(r"(?:文献|证据行|行)\s*(\d+[a-z]?)", m.group(1)):
            votes.add(num)
    return votes


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="§5.1 优先精读序草稿（名单与组序；理由与维度人工填）")
    parser.add_argument("--delivery", required=True)
    parser.add_argument("--first", type=int, default=FIRST_MAX)
    args = parser.parse_args(argv)
    delivery = Path(args.delivery)

    package_md = (delivery / package_parse.PACKAGE_REL).read_text(encoding="utf-8")
    rows = package_parse.evidence_rows(package_md)
    subj64 = package_parse.find_h2(package_md, "结论") or ""
    cite_hits: dict[str, int] = {}
    subs = [(title, wrap_lines(lines)) for title, lines in split_subs(subj64)]
    for sub_title, sub_lines in subs[1:]:
        if not re.match(r"(?:§\s*)?6\.[24]", sub_title.strip()):
            continue
        if re.match(r"(?:§\s*)?6\.2", sub_title.strip()):
            for num in refs_in("\n".join(sub_lines)):
                cite_hits[num] = cite_hits.get(num, 0) + 1
        else:
            for line in sub_lines:
                if line.strip().startswith("-"):
                    for num in refs_in(line):
                        cite_hits[num] = cite_hits.get(num, 0) + 1
    cards = package_parse.parse_cards(delivery)
    _by_slug, by_doi, _by_num = package_parse.card_index(cards)
    row_cards = package_parse.row_card_map(rows, cards)
    cites = cited_counts(delivery)
    sort_order = load_sort_order(delivery)
    domain = sorted([num for num in rows if row_cards.get(num)],
                    key=package_parse.row_sort_key)
    items = []
    for num in domain:
        cids = [cid for cid in (row_cards[num] or [])
                if rows[num]["doi"] and cards[cid].get("doi") == rows[num]["doi"]]
        card = cards.get(cids[0]) if cids else None
        text = card_file_text(delivery, card) if card else ""
        rel = rows[num].get("关联") or relation_of(text)
        rank = relation_rank(rel)
        doi = (card.get("doi") or "").lower() if card else ""
        cites_n = cites.get(doi, 0) if isinstance(cites.get(doi), int) else 0
        slug = (card.get("slug", "") if card else "")
        level = level_of_design(rows[num].get("设计") or "")
        card_level = level_of(text)
        if card_level != "E9" and card_level != level:
            print(f"定级交叉核对：行 {num} 设计列 {level} ≠ 卡设计行 {card_level}（以设计列为准）")
        items.append({"row": num, "hits": cite_hits.get(num, 0), "rel_rank": rank,
                      "theme": theme_of(text), "level": level,
                      "cites": cites_n, "slug": slug, "doi": doi,
                      "order": sort_order.get(doi, 10 ** 9)})

    def level_rank(item) -> int:
        level = item["level"]
        return int(level[1]) if level in ("E1", "E2", "E3", "E4", "E5") else 9

    def sort_key(item):
        cites = item["cites"] if isinstance(item["cites"], int) else None
        cites_key = (1, 0) if cites is None or cites <= 0 else (0, -cites)
        return (-item["hits"], item["rel_rank"], level_rank(item), cites_key,
                item.get("order", 10 ** 9))

    def want_total(n: int) -> int:
        want = min(max(args.first, FIRST_MIN), FIRST_MAX, n) if n else 0
        return want if n > FIRST_MAX else n

    # P3 决议：先读层＝全域按依据序前 N 名，不做主题去重。模板 ③ 的「同等①②时尚未
    # 出现的 §4.x 优先」是人工在理由充分时的自由裁量，不是机器硬约束——机器去重会
    # 把 {1,7,19,32} 拆散（本次实测），反而与人工定稿不一致。
    ordered = sorted(items, key=sort_key)
    first, rest = ordered[:want_total(len(ordered))], ordered[want_total(len(ordered)):]
    themes = package_theme_catalog(package_md)
    groups: dict[str, list] = {}
    for item in sorted(rest, key=sort_key):
        groups.setdefault(item["theme"] or "", []).append(item)

    lines = ["### 5.1 优先精读序", "",
             f"先读层（依据序，{len(first)} 篇）：", "",
             "| 序 | 文献 | 卡 | 维度 | 理由 |",
             "|---|---|---|---|---|"]
    for i, item in enumerate(first, 1):
        lines.append(f"| {i} | 文献 {item['row']} | {item['slug']} | （人工填：结论关联／权衡价值／主题补位） "
                     f"| （人工填：≤40字，带（行 {item['row']}）出处指针） |")
    lines += ["", "其后层（按 `§4` 主题分组，组内按依据序，不标名次）：", ""]
    render_groups(lines, groups, themes)
    (delivery / OUT_REL).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8", newline="\n")
    print(f"§5.1 草稿：先读层 {len(first)} 篇｜其后层 {sum(len(v) for v in groups.values())} 篇 → {OUT_REL}（理由与维度人工填）")
    return 0

def card_file_text(delivery: Path, card) -> str:
    """卡文件正文：文件名即身份（`parse_cards` 的 file/slug 任一在场即可定位）。"""
    name = card.get("file") or (card.get("slug", "") + package_parse.CARD_SUFFIX)
    return (delivery / package_parse.CARDS_REL / name).read_text(encoding="utf-8")


def render_groups(lines, groups, themes) -> None:
    for group_theme in sorted(groups, key=lambda t: (t == "", t)):
        label = themes.get(group_theme, group_theme) if group_theme else "（未落主题）"
        lines.append(f"#### §{group_theme} {label}" if group_theme else "#### （未落主题）")
        lines.append("")
        lines.append("| 文献 | 卡 | 维度 | 理由 |")
        lines.append("|---|---|---|---|")
        for item in groups[group_theme]:
            lines.append(f"| 文献 {item['row']} | {item['slug']} | （人工填） | （人工填：≤40字，带（行 {item['row']}）出处指针） |")
        lines.append("")


def package_theme_catalog(package_md: str) -> dict:
    catalog = {}
    for title, _lines in split_subs(
            package_parse.find_h2(package_md, "主题") or package_parse.find_h2(package_md, "综合") or ""):
        m = re.match(r"(?:§\s*)?(4\.\d+)\s*(.*)$", title.strip())
        if m:
            catalog[m.group(1)] = m.group(2).strip()
    return catalog


if __name__ == "__main__":
    raise SystemExit(main())
