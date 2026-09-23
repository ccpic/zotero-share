#!/usr/bin/env python
"""交付形态契约检查（W 组；交付必跑三件套之三：零网络、零锚点）。

对象是**交付形态**而非包内文本：机读纯索引骨架、门禁摘要骨架、台账指针一致性、卡面解析健康与入库集守恒
（`INDEX.md`／`agent/gate-summary.md`／`agent/manifest.json` 的 `ledger_pointers` 与 `regeneration`／
`agent/cards/*.md`／`agent/placement-plan.md`）进硬门禁；结构／封顶类（顶层四项、`agent/` 直下件数、体积）
与禁词面只出读数并告警、**不阻断**。

口径（本执行体只实现，不复述）：`references/protocols/workspace-guide.md` §6（卡片单形态的唯一家）、
§8 `R3`／`R6`、§9（索引单层形态）、§10（再生清单字段）、§11（门禁摘要 verdict 头与抽跑一条）；
抽查轨与档位见 `evals/acceptance/topic.md`。卡面／包面解析与验收门共用 `scripts/package_parse.py`（一份实现）。

用法：uv run python scripts/check_delivery.py --delivery workspace/YYYY-MM-DD-<slug> [--json]
      uv run python scripts/check_delivery.py --selftest        # 孪生场景自证（沙箱，不碰交付）
      uv run python scripts/check_delivery.py --card-probe [<真实包>]  # 漂移抽跑（§11：只判解析器健康）
退出码：0 = 硬门禁全过（软项读数照打；抽跑为「样本缺位」亦记 0）；1 = 硬门禁未过或抽跑红；2 = 用法或输入缺失。
"""
from __future__ import annotations

import argparse
import hashlib
import html as html_lib
from html.parser import HTMLParser
import json
import math
import re
import shutil
import sys
import tempfile
from pathlib import Path

import package_parse
import reader_html  # W-23 复用渲染器的位切分与位校验（身份段口径的唯一家＝渲染执行体）

REPO = Path(__file__).resolve().parents[4]  # <repo>/.claude/skills/med-lit-review/scripts/check_delivery.py
INDEX_NAME = "INDEX.md"
GATE_SUMMARY = "agent/gate-summary.md"
MANIFEST = "agent/manifest.json"
AGENT_DIR = "agent"
RUN_DIR = "run"
SCOPING_NOTE = "agent/scoping-note.md"  # P1 快线的主要结论件（`workspace-guide` §6：无八节包、无卡面）
PLACEMENT_PLAN = "agent/placement-plan.md"  # 写库在组的落点计划（W-22 守恒等式的一侧）
WRITE_SKIP_MARK = "写库设计跳过"  # 写库设计跳过（P1／P2／P3 无落点计划）的注记词（`hitl-protocol.md` 记录节）
PLAN_ENTRY_RE = re.compile(r"^[A-Z0-9]{8}(?![A-Z0-9])")  # 落点计划行的条目键（8 位 Zotero key）
DIFF_SHOW = 6  # W-22 差异逐条列举条数（前若干条＋总数，差异多时截断显示）
# `ledger_pointers` 的必在键与可缺键（§10）——值带 `agent/` 前缀，缺席记「无…」
POINTER_KEYS = ("download_ledger", "placement_plan", "gate_summary", "reference_list")
POINTER_OPTIONAL = ("regulatory_version_record",)
FULL_GATE_CLASS = "full_gate"
# `R6` 枚举制：顶层固定三项＋人读主件（第四项名随 topic slug）；`agent/` 直下 ≤14 条目（`cards/` 计目录本身）；体积 ≤2.5MB
TOP_LEVEL = (INDEX_NAME, f"{AGENT_DIR}/", f"{RUN_DIR}/")
AGENT_MAX_ENTRIES = 14
VOLUME_MAX_BYTES = int(2.5 * 1024 * 1024)
STATUS_LINE_RE = re.compile(r"状态计数\s*[:：]")
# W-14 禁词面（不阻断）：判定词与因果词——表格／代码块内不判
BANNED_WORDS = ("结论", "判定", "建议", "优先级", "优于", "推荐", "因果", "因此", "由于")
LABEL_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s*\*{0,2}(判定|未过条目|缺口去向)\*{0,2}\s*[:：]")
VERDICT_HEAD_RE = re.compile(r"^##\s+.*Verdict.*先读我")
FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


def ok(observed, metric=None):
    return {"ok": True, "observed": observed, "metric": metric}


def bad(observed):
    return {"ok": False, "observed": observed, "metric": None}


def cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def is_table_row(line):
    return line.strip().startswith("|") and len(cells(line)) > 1


def body_lines(text):
    """非空行（含行号），供骨架判定与禁词面共用。"""
    return [(n, line) for n, line in enumerate(text.splitlines(), 1) if line.strip()]


def entry_names(directory: Path):
    """目录直下条目名（子目录带尾斜杠），供结构类读数共用。"""
    return sorted(p.name + ("/" if p.is_dir() else "") for p in directory.iterdir())


def free_paragraphs(text):
    """自由段落：不在标题／表格／代码块／块引用／列表项内的正文行（状态计数行豁免）。

    返回 (行号, 原文) 列表——`INDEX.md` 的「除状态计数行外零自由段落」即此判据（沿用交付形态探针口径：
    标题／块引用／列表项与表格同属结构化行，自由段落＝落不进任何结构化行的散文）。
    """
    out, fenced = [], False
    for n, line in body_lines(text):
        stripped = line.strip()
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        if STATUS_LINE_RE.search(line):
            continue
        if stripped.startswith(("#", "|", ">", "-", "*", "+")) or re.match(r"^\d+[.)]\s", stripped):
            continue
        out.append((n, stripped))
    return out


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 硬门禁（W 组）


# ---------------- 人读主件与渲染对账（W-01..W-08；随 04 号票挂进本执行体）

PAGE_FORBIDDEN = (
    (re.compile(r"(?i)<link\b"), "link 元素（外链样式）"),
    (re.compile(r"(?i)<(?:iframe|object|embed|video|audio|source|track)\b"), "外链媒体／框架元素"),
    (re.compile(r"(?i)<[a-zA-Z][^>]*\bsrc\s*="), "src 属性（外链资源）"),
    (re.compile(r"(?i)\bsrcset\s*="), "srcset 属性（响应式外链图片）"),
    (re.compile(r"(?i)\bposter\s*="), "poster 属性（外链视频封面）"),
    (re.compile(r"(?i)<base\b"), "base 元素（改写基准 URL）"),
    (re.compile(r"(?i)@import\b"), "@import"),
    (re.compile(r"(?is)url\(\s*['\"]?(?!data:|#)[^)]*\)"), "CSS url() 外链"),
    (re.compile(r"(?i)\bimage-set\s*\("), "CSS image-set() 外链"),
    (re.compile(r"(?i)\b(?:src|srcset|poster)\s*[:=]\s*['\"]?https?://"), "资源属性／字号 src 指向 http(s)"),
    (re.compile(r"(?i)<(?:link|base)\b[^>]*\bhref\s*=\s*['\"]?https?://"), "link／base 的 href 指向 http(s)"),
)
ROW_POINTER_RE = package_parse.ROW_POINTER_RE  # 与 §5.1 解析同源（解析模块一处定义）
ROW_POINTER_PAREN_RE = re.compile(r"[（(][^）)]{0,4}行\s*\d[^）)]*[）)]")
NODE_CAP = 120  # 03 §8：主图节点上限（与渲染器同数，门禁按契约常量判，不取页内自报）
EDGE_CAP = 250  # 03 §8：主图边上限
GRAPH_BLOCK_RE = re.compile(r'(?is)<script[^>]*\bid="graph-data"[^>]*>(.*?)</script>')
HTML_ID_RE = re.compile(r'\bid="([^"]+)"')
HTML_HREF_RE = re.compile(r'\bhref="#([^"]+)"')
HTML_CARD_ENTRY_RE = re.compile(r'\bdata-open-card="([^"]+)"')
DEGRADE_BLOCK_MARKER = 'id="graph-degrade"'


def page_path_of(delivery: Path, slug: str) -> Path:
    return delivery / f"{slug}.html"


def read_page(delivery: Path):
    """返回页文本：先按 manifest.topic.slug 定位；manifest 缺或 slug 缺即 None（读数由 W-01 出）。"""
    manifest_path = delivery / MANIFEST
    if not manifest_path.exists():
        return None
    try:
        slug = str((read_json(manifest_path).get("topic") or {}).get("slug") or "")
    except (OSError, ValueError):
        return None
    if not slug:
        return None
    path = page_path_of(delivery, slug)
    return path.read_text(encoding="utf-8") if path.exists() else None


def w01_main_artifact(delivery: Path):
    """W-01：人读主件在场与命名——`<slug>.html` 在场，`<slug>` ＝ `manifest.topic.slug`（不含日期与 `-rN`）。"""
    manifest_path = delivery / MANIFEST
    if not manifest_path.exists():
        return bad(f"{MANIFEST} 缺件，主件命名无从核对")
    try:
        slug = str((read_json(manifest_path).get("topic") or {}).get("slug") or "")
    except (OSError, ValueError) as exc:
        return bad(f"{MANIFEST} 解析失败：{type(exc).__name__}: {exc}")
    if not slug:
        return bad(f"{MANIFEST} 缺 topic.slug（主件名无从定）")
    if re.match(r"^\d{4}-\d{2}-\d{2}-", slug) or re.search(r"-r\d+$", slug):
        return bad(f"topic.slug 带日期或重跑后缀：{slug}（人读主件名不含日期与 -rN）")
    path = page_path_of(delivery, slug)
    if not path.exists():
        return bad(f"人读主件缺件：{slug}.html（交付根固定四项之一，由渲染器从版内真源产出）")
    return ok(f"人读主件在场：{slug}.html（{path.stat().st_size / 1024:.0f}KB；slug＝manifest.topic.slug）")


def w02_self_contained(delivery: Path):
    """W-02：单文件自足——无 link／script src／img src／外链媒体元素／@import／外链 CSS url()。"""
    page = read_page(delivery)
    if page is None:
        return bad("人读主件缺件（页不在场，自足性无从核对）")
    for pattern, label in PAGE_FORBIDDEN:
        m = pattern.search(page)
        if m:
            return bad(f"命中外链资源：{label}（{m.group(0)[:60]!r}）——单文件自足、断网可开")
    return ok(f"自足性：无外链样式／脚本／字体／图片（{len(page) / 1024:.0f}KB 全内联）")


def w03_anchors(delivery: Path):
    """W-03：锚点完整——`#row-N` ⊇ §5 行号、`#card-<slug>` ⊇ 卡文件件、页内链接目标全在场。"""
    page = read_page(delivery)
    if page is None:
        return bad("人读主件缺件（页不在场，锚点无从核对）")
    package_path = delivery / package_parse.PACKAGE_REL
    rows = {}
    if package_path.exists():
        text = package_path.read_text(encoding="utf-8")
        try:
            rows = package_parse.evidence_rows(text)
        except (ValueError, IndexError):
            rows = {}
    cards = package_parse.card_files(delivery)
    ids = set(HTML_ID_RE.findall(page))
    hrefs = set(HTML_HREF_RE.findall(page))
    missing_rows = [n for n in sorted(rows, key=package_parse.row_sort_key) if f"row-{n}" not in ids]
    missing_cards = [path.stem for path in cards if f"card-{path.stem}" not in ids]
    if missing_rows:
        return bad(f"行锚点缺 {len(missing_rows)} 个：{'、'.join(missing_rows[:6])}（#row-N 须覆盖 §5 全部行）")
    if missing_cards:
        return bad(f"卡锚点缺 {len(missing_cards)} 个：{'、'.join(missing_cards[:4])}（#card-<DOI-slug> 须覆盖每件）")
    dangling = sorted(target for target in hrefs if target not in ids)
    if dangling:
        return bad(f"页内链接悬空 {len(dangling)} 处：{'、'.join(dangling[:6])}（目标不在页内）")
    scope = "P1 快线：无卡面无表行，本条不适用" if not rows and not cards else \
        f"行锚 {len(rows)} 个、卡锚 {len(cards)} 个、页内链接 {len(hrefs)} 处全部可解析"
    return ok(f"锚点完整：{scope}")


def w04_doi_locatable(delivery: Path):
    """W-04：DOI 可定位——§5 每行 DOI 在页内出现（身份链起点）。"""
    page = read_page(delivery)
    if page is None:
        return bad("人读主件缺件（页不在场，DOI 无从核对）")
    package_path = delivery / package_parse.PACKAGE_REL
    if not package_path.exists():
        return ok("P1 快线：无证据表，本条不适用")
    rows = package_parse.evidence_rows(package_path.read_text(encoding="utf-8"))
    lowered = page.lower()
    missing = [row["doi"] for _n, row in sorted(rows.items(), key=lambda kv: package_parse.row_sort_key(kv[0]))
               if row["doi"] and row["doi"].rstrip(".") not in lowered]
    if missing:
        return bad(f"§5 行 DOI 未在页内出现 {len(missing)} 处：{'、'.join(missing[:4])}")
    return ok(f"DOI 可定位：§5 行 DOI {sum(1 for r in rows.values() if r['doi'])} 处在页内全部出现")


def w05_card_coverage(delivery: Path):
    """W-05：卡全覆盖——每张单卡至少在页内有一处入口（表行／图节点／卡片索引任一 `data-open-card`）。"""
    page = read_page(delivery)
    if page is None:
        return bad("人读主件缺件（页不在场，卡覆盖无从核对）")
    cards = package_parse.card_files(delivery)
    if not cards:
        return ok("P1 快线：无卡面，本条不适用")
    entries = set(HTML_CARD_ENTRY_RE.findall(page))
    href_entries = set(re.findall(r'href="#card-([^"]+)"', page))
    missing = [path.stem for path in cards if path.stem not in entries and path.stem not in href_entries]
    if missing:
        return bad(f"卡缺入口 {len(missing)} 张：{'、'.join(missing[:4])}（三入口至少一处在场）")
    return ok(f"卡全覆盖：{len(cards)} 张卡在页内各有入口（`data-open-card` 或卡锚点链接）")


def w06_reading_priority(delivery: Path):
    """W-06：精读序三条（读 markdown 真源，07 §7）——存在性／出处／域封闭。

    - 存在性：域（有卡 ∩ 有表行）内每篇在 §5.1 中恰好出现一次；先读层 3–5 篇（域内不足 5 篇则全部入层）、名次 1..N 连续。
    - 出处：每条理由含 `行 N` 指针且全部落地（不悬空）。
    - 域封闭：入序行号 ⊆ 域——仅题录、会议摘要、无表行的背景卡不得入序。
    **不查排序正确性**（07 §7）：依据链含 agent 判断项与引用计数，把「谁该排第几」写成机器硬断言＝
    把主观排序固化成脆弱契约，任何依据微调都会误红；排序合理性留 R4 生成后自查与人读复核。
    """
    package = delivery / package_parse.PACKAGE_REL
    if not package.exists():
        if (delivery / SCOPING_NOTE).exists():
            return ok("不适用：P1 快线无八节包（无卡路线不产该层，页面亦不留空壳）")
        return bad(f"八节包与 {SCOPING_NOTE} 皆无，精读序无主")
    text = package.read_text(encoding="utf-8")
    rows = package_parse.evidence_rows(text)
    try:
        cards = package_parse.parse_cards(delivery)
    except package_parse.CardParseError as exc:
        return bad(f"卡面解析失败，精读序的域无从取：{exc}")
    row_cards = package_parse.row_card_map(rows, cards)
    domain = {row_no for row_no in rows if row_cards.get(row_no)}
    if not domain:
        return ok("不适用：域为空（无「有卡 ∩ 有表行」篇目）——同快线处理，不产该层")
    priority = package_parse.reading_priority(text)
    if not priority["present"]:
        return bad(f"缺 §5.1 优先精读序子段（{package_parse.PACKAGE_REL}；域内 {len(domain)} 篇待入序）")
    entries, first = priority["entries"], priority["first"]
    counts = {}
    for entry in entries:
        counts[entry["row"]] = counts.get(entry["row"], 0) + 1
    problems = []
    duplicated = sorted((row for row, n in counts.items() if n > 1), key=package_parse.row_sort_key)
    missing = sorted(domain - set(counts), key=package_parse.row_sort_key)
    stray = sorted(set(counts) - domain, key=package_parse.row_sort_key)
    if duplicated:
        problems.append(f"入序重复 {'、'.join('行 ' + r for r in duplicated[:6])}")
    if missing:
        problems.append(f"域内未入序 {'、'.join('行 ' + r for r in missing[:6])}")
    if stray:
        problems.append(f"域外篇目入序 {'、'.join('行 ' + r for r in stray[:6])}（仅题录／无表行卡不得入序）")
    # 档位：域 ≥ 5 篇时先读层 3–5 篇；域不足 5 篇则**全部入层**（域 4 篇即 4 篇，不许留 1 篇在其后层）
    if len(domain) < 5:
        low = high = len(domain)
    else:
        low, high = 3, 5
    if not low <= len(first) <= high:
        span = f"{low}–{high}" if low != high else str(low)
        problems.append(f"先读层 {len(first)} 篇，超出档位（域 {len(domain)} 篇 → 应 {span} 篇）")
    ranks = sorted(entry["rank"] for entry in first if entry["rank"])
    if ranks != list(range(1, len(first) + 1)):
        problems.append(f"先读层名次非 1..{len(first)} 连续（实测 {ranks}）")
    lacking = [entry["row"] for entry in entries if not ROW_POINTER_RE.search(entry["reason"])]
    dangling = sorted({ref for entry in entries for ref in ROW_POINTER_RE.findall(entry["reason"])
                       if ref not in rows}, key=package_parse.row_sort_key)
    if lacking:
        problems.append(f"理由缺行指针 {'、'.join('行 ' + r for r in lacking[:6])}")
    if dangling:
        problems.append(f"行指针悬空 {'、'.join('行 ' + r for r in dangling[:6])}")
    if problems:
        return bad("；".join(problems[:3]) + f"（读取路径 {package_parse.PACKAGE_REL}）")
    return ok(f"精读序：域 {len(domain)} 篇全部入序恰一次；先读层 {len(first)} 篇（名次 1..{len(first)}）；"
              f"其后层 {len(entries) - len(first)} 篇分 {len(priority['groups'])} 组；理由行指针全部落地、域封闭无越界")


PRIORITY_GROUP_MARK = "####"
PRIORITY_BANNED = ("重要", "经典", "高质量", "值得一读", "必读", "里程碑", "权威", "顶级", "优秀", "出色", "重磅")


def w20_reading_priority_form(delivery: Path):
    """W-20（软项读数，不阻断）：精读序形态——理由正文 ≤40 字、无纯评价词、仅摘要带状态词、其后层不标名次、分组在场。"""
    package = delivery / package_parse.PACKAGE_REL
    if not package.exists():
        return ok("不适用：无八节包（快线不产该层）")
    text = package.read_text(encoding="utf-8")
    priority = package_parse.reading_priority(text)
    if not priority["present"]:
        return bad("读数列缺 §5.1（硬门禁 W-06 判该条；此处只记读数）")
    rows = package_parse.evidence_rows(text)
    block, _title = package_parse.block_of_priority(text)
    notes, entries = [], priority["entries"]
    long_reasons = [entry["row"] for entry in entries
                    if len(ROW_POINTER_PAREN_RE.sub("", entry["reason"]).strip()) > 40]
    if long_reasons:
        notes.append(f"理由正文 >40 字 {len(long_reasons)} 条（行 {'、'.join(long_reasons[:5])}）")
    banned = [entry["row"] for entry in entries
              if any(word in entry["reason"] for word in PRIORITY_BANNED)]
    if banned:
        notes.append(f"纯评价词命中 {len(banned)} 条（行 {'、'.join(banned[:5])}）")
    abstract_missing = [entry["row"] for entry in entries
                        if "仅摘要" in (rows.get(entry["row"], {}).get("设计") or "")
                        and "仅摘要" not in entry["reason"]]
    if abstract_missing:
        notes.append(f"仅摘要未带状态词 {len(abstract_missing)} 条（行 {'、'.join(abstract_missing[:5])}）")
    after_group = False
    for line in (block or "").splitlines():
        if line.startswith(PRIORITY_GROUP_MARK):
            after_group = True
            continue
        if after_group and line.strip().startswith("|") and "序" in [c.strip() for c in line.strip().strip("|").split("|")]:
            notes.append("其后层表带「序」列（名次只应在先读层）")
            break
    if any(not group["label"] for group in priority["groups"]):
        notes.append("其后层条目未落在 `####` 主题分组标题下（分组标题缺）")
    later = len(entries) - len(priority["first"])
    reading = (f"先读层 {len(priority['first'])} 篇／其后层 {later} 篇分 {len(priority['groups'])} 组；"
               f"理由 {len(entries)} 条")
    return bad(f"{reading}；" + "；".join(notes)) if notes else ok(reading + "，形态读数零告警")


# W-21 读者面文案（`reader-html.md` §3）：读者可见文本零机器记号；页尾复核区是唯一豁免区
FOOTER_BLOCK_RE = re.compile(r"(?is)<footer\b.*?</footer>")
STYLE_BLOCK_RE = re.compile(r"(?is)<style\b.*?</style>")
SCRIPT_BLOCK_RE = re.compile(r"(?is)<script\b.*?</script>")
TAG_RE = re.compile(r"(?s)<[^>]+>")
READER_FORBIDDEN = (
    (re.compile(r"#(?:row|card)-"), "锚点记号"),
    (re.compile(r"<(?:DOI-slug|交付目录|slug|topic|路径|值)>"), "尖括号占位符"),
    (re.compile(r"1\.\.N|(?<![A-Za-z])行\s*N(?![A-Za-z])"), "占位符 N"),
    (re.compile("∅"), "机器符号 ∅"),
    (re.compile(r"(?:agent|run|references|scripts|evals)/[\w./*\-]+"), "内部件路径"),
    (re.compile(r"\b[\w-]+\.(?:py|json|ya?ml|tsv|txt|xpi|md|csv)\b"), "脚本或数据件名"),
    (re.compile(r"\buv run\b"), "复核命令"),
    (re.compile(r"`"), "未渲染的 markdown 反引号"),
    (re.compile(r"(?m)^#{2,6}\s"), "未渲染的 markdown 标题"),
)

# W-21 第二判据面（第 5 轮补）：符号之外还得管词。只卡「我们自己写的」文本——卡片正文与证据表行里的
# 英文摘要／引文是照录的来源内容，拿词表去卡原文是错的（原文里出现 schema 一类词很正常）。
READER_FORBIDDEN_PROSE = (
    (re.compile(r"(?<![A-Za-z0-9_])[a-z][a-z0-9]*_[a-z0-9_]+(?![A-Za-z0-9_])"), "机器标识符"),
    (re.compile(r"(?<![A-Za-z])verbatim(?![A-Za-z])"), "英文机器词 verbatim"),
    (re.compile(r"(?<![A-Za-z])schema(?![A-Za-z])"), "英文机器词 schema"),
    (re.compile("证据表行号"), "内部词 证据表行号"),
    (re.compile("页码指针"), "内部词 页码指针"),
)
QUOTE_CLASS_RE = re.compile(r"\b(?:card|erow|card-chip)\b")
VOID_TAGS = {"br", "img", "input", "hr", "meta", "link", "area", "base", "col", "embed",
             "source", "track", "wbr"}


class _ProseOnly(HTMLParser):
    """照录来源容器（卡片、证据表行、卡片索引 chip）之外的读者面文本。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if self.depth:
            if tag not in VOID_TAGS:
                self.depth += 1
            return
        if QUOTE_CLASS_RE.search(dict(attrs).get("class") or ""):
            self.depth = 1

    def handle_endtag(self, tag):
        if self.depth and tag not in VOID_TAGS:
            self.depth -= 1

    def handle_data(self, data):
        if not self.depth:
            self.parts.append(data)


def prose_text(page: str) -> str:
    """读者面里「我们自己写的」文本：剥样式／脚本／页尾复核区与照录来源容器。"""
    text = FOOTER_BLOCK_RE.sub(" ", page)
    text = STYLE_BLOCK_RE.sub(" ", text)
    text = SCRIPT_BLOCK_RE.sub(" ", text)
    parser = _ProseOnly()
    parser.feed(text)
    return " ".join("".join(parser.parts).split())


def reader_text(page: str) -> str:
    """读者可见文本：剥页尾复核区与样式／脚本块后去标签、还原实体（`Ctx.inline` 的反函数面）。"""
    text = FOOTER_BLOCK_RE.sub(" ", page)
    text = STYLE_BLOCK_RE.sub(" ", text)
    text = SCRIPT_BLOCK_RE.sub(" ", text)
    return html_lib.unescape(TAG_RE.sub(" ", text))


def w21_reader_copy(delivery: Path):
    """W-21：读者面零机器记号——占位符、锚点记号、内部路径、命令与未渲染 markdown 一律不得出现。

    判定面＝剥样式／脚本与页尾复核区后的页文本；页内只有复核区允许出现仓库路径与可粘贴命令
    （`reader-html.md` §3 读者面词表）。判据零网络零锚点，同本执行体其余硬门禁。
    """
    page = read_page(delivery)
    if page is None:
        return bad("人读主件缺件（页不在场，读者面文案无从核对）")
    text = reader_text(page)
    hits = []
    for pattern, label in READER_FORBIDDEN:
        m = pattern.search(text)
        if m:
            seg = " ".join(text[max(0, m.start() - 16):m.end() + 16].split())
            hits.append(f"{label}：{seg}")
    prose = prose_text(page)
    for pattern, label in READER_FORBIDDEN_PROSE:
        m = pattern.search(prose)
        if m:
            seg = " ".join(prose[max(0, m.start() - 16):m.end() + 16].split())
            hits.append(f"{label}：{seg}")
    if hits:
        return bad(f"读者面命中机器记号／词 {len(hits)} 类（{'；'.join(hits[:4])}）")
    return ok(f"读者面零机器记号（剥复核区后 {len(text)} 字符：占位符／锚点记号／∅／内部路径／"
              f"命令／未渲染 markdown 全零；自写文本 {len(prose)} 字符零机器词与内部词）")


# ---------------- 图几何复算（W-07 用；口径＝ADR-0030 与 `reader-html.md` §3.6）

GRAPH_CANVAS = (1200.0, 900.0)
GRAPH_CENTRE = (600.0, 450.0)
GRAPH_ELLIPSE = (554.0, 404.0)                       # 可用区 1108×808 的内切椭圆半轴
GRAPH_CELLS = {"main": 3600.0, "neigh": 676.0}       # 单点方格面积（60²／26²）：分角与预算的面积账
GRAPH_CELLS_SIDE = {"main": 60.0, "neigh": 26.0}     # 同上的边长（数据块按边长自报，门禁复算）
GRAPH_CELL_PLACEHOLDER = {"main": 14.0, "neigh": 6.0}   # 方格里的占位：行号 14uu／邻域间隙 6uu
GRAPH_SIZE_RANGE = {"main": (7.0, 23.0), "neigh": (4.0, 10.0)}   # 半径量程（点径 14–46／8–20）
GRAPH_POWER = 1.0 / 3.0                              # r ∝ c^(1/3)（ADR-0030 决定 5）
GRAPH_USABLE = (1108.0, 808.0)                       # 可用区
GRAPH_INK_MIN = 0.85                                 # 墨水铺满下限（全墨水包围盒 ÷ 可用区）
CORRIDOR_W = 88.0                                    # 走廊环厚（决定 3）
NODE_BAND_R = GRAPH_ELLIPSE[1] - CORRIDOR_W - 30.0   # 节点带外径（404−88−30）
FRAME_PAD = 48.0
GRAPH_TIERS = ("有全文", "仅摘要", "仅题录", "未评", "")   # 缺件退回「未评」：缺记不猜
GRAPH_YEAR_BANDS = (("≤1999", 0.85), ("2000–2009", 0.90), ("2010–2019", 0.95), ("≥2020", 1.00))
GRAPH_UNKNOWN_YEAR = "年份未取到"


def graph_radius(cites, lo, hi, scale, power):
    """按数据块自报的幂与量程复算半径：点径须能由被引数复算，门禁不采信节点里的 radius。"""
    r_min, r_max = scale
    if not isinstance(cites, int) or not isinstance(lo, int) or not isinstance(hi, int) or hi <= lo:
        return r_min
    if cites <= 0:
        return r_min
    ratio = min(1.0, max(0.0, (cites ** power - lo ** power) / (hi ** power - lo ** power)))
    return r_min + (r_max - r_min) * ratio


def graph_box(node) -> tuple:
    return (node["x"] - node["radius"], node["y"] - node["radius"],
            node["x"] + node["radius"], node["y"] + node["radius"])


def graph_box_hit(one, other) -> bool:
    return one[0] < other[2] and other[0] < one[2] and one[1] < other[3] and other[1] < one[3]


def graph_extent(boxes) -> tuple:
    return (min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes))


def graph_label_box(label) -> tuple:
    return (label["x"] - label["w"] / 2, label["y"] - label["h"], label["x"] + label["w"] / 2, label["y"])


def graph_corridor_boxes(corridors) -> list:
    boxes = []
    for corridor in corridors:
        pairs = [item.split(",") for item in corridor["path"].split()]
        xs = [float(item[0]) for item in pairs]
        ys = [float(item[1]) for item in pairs]
        boxes.append((min(xs), min(ys), max(xs), max(ys)))
    return boxes


def graph_pairs(edges, theme_of) -> dict:
    """边集 → 主题对计数（门的走廊带数就是按它核的：`n == 主题对数`）。"""
    pairs = {}
    for edge in edges:
        key = tuple(sorted((theme_of(edge.get("source")), theme_of(edge.get("target")))))
        pairs[key] = pairs.get(key, 0) + 1
    return pairs


def w07_graph_contract(delivery: Path):
    """W-07：图契约＝**几何不变量**（ADR-0030 决定 11；取代「节点 ≤120／边 ≤250／`caps` 与常量全等」）。

    判项：主节点数＝证据行＋背景卡数、任意两点不相交且行号不压邻点、所有点与标签在画布内、
    点径与被引数按口径可复算一致、扇区张角 ∝ Σ 点数×单点方格、走廊带数＝主题对数、两态取景＝同一批
    坐标、墨水铺满可用区、截断与降级事实在场。可读性预算（可用面积 ÷ 单点方格）是主判据，旧常量只作
    硬兜底。P1 档改判构成图／降级块在场。
    """
    page = read_page(delivery)
    if page is None:
        return bad("人读主件缺件（页不在场，图数据块无从核对）")
    m = GRAPH_BLOCK_RE.search(page)
    if m is None:
        return bad("缺 `graph-data` 数据块（关系图数据以内联 JSON 落页）")
    try:
        data = json.loads(m.group(1))
    except ValueError as exc:
        return bad(f"`graph-data` 不可解析：{exc}")
    ids = set(HTML_ID_RE.findall(page))
    degraded = bool(data.get("degraded"))
    if data.get("profile") == "p1":
        has_chart = "检索构成图" in page or "composition" in data
        if not (has_chart or DEGRADE_BLOCK_MARKER in page):
            return bad("P1 构成图与降级块皆不在场（图位恒在：构成图或降级块）")
        return ok(f"P1 构成图档：{'构成图在场' if has_chart else '降级块在场'}"
                  f"（{len(data.get('composition', {}).get('buckets', {}))} 桶）")
    nodes, edges = data.get("nodes") or [], data.get("edges") or []
    truncated = data.get("graph_truncated")
    if not isinstance(truncated, dict) or "cap_hit" not in truncated:
        return bad("`graph_truncated` 缺场或缺 `cap_hit`（截断事实须随数据块在场）")
    if not nodes:
        if degraded and DEGRADE_BLOCK_MARKER in page:
            return ok("引文面缺：降级块在场（图数据块无节点，P0 降级档）")
        return bad("`graph-data` 零节点且未标降级（引文面不可得须出降级块）")
    seen, dups = set(), []
    for node in nodes:
        ident = node.get("id")
        if ident in seen:
            dups.append(ident)
        seen.add(ident)
    if dups:
        return bad(f"节点 id 重复 {len(dups)} 个：{'、'.join(str(x) for x in dups[:3])}（节点身份须唯一）")
    lines = [edge for edge in edges if edge.get("draw") != "band"]
    if len(nodes) > NODE_CAP:
        return bad(f"节点 {len(nodes)} 超硬兜底 {NODE_CAP}（可读性预算是主判据，兜底仍须守；"
                   f"上限按本执行体常量判，不取页内自报）")
    if len(lines) > EDGE_CAP:
        return bad(f"画出的连线 {len(lines)} 超硬兜底 {EDGE_CAP}（带承载的个体引文不占这条预算；"
                   f"上限按本执行体常量判，不取页内自报）")

    theme_of = {node.get("id"): node.get("theme") or "" for node in nodes}
    main_nodes = [node for node in nodes if node.get("main")]

    # ① 覆盖性：主节点数 ＝ 证据行数 ＋ 背景卡数（守「每行一篇」，背景卡不遗漏）
    package = delivery / package_parse.PACKAGE_REL
    if package.exists():
        rows = package_parse.evidence_rows(package.read_text(encoding="utf-8"))
        try:
            cards = package_parse.parse_cards(delivery)
        except package_parse.CardParseError as exc:
            return bad(f"卡面解析失败，主节点覆盖性无从核对：{exc}")
        mapped = {cid for cids in package_parse.row_card_map(rows, cards).values() for cid in cids}
        expected = len(rows) + sum(1 for cid in cards if cid not in mapped)
        if len(main_nodes) != expected:
            return bad(f"主节点 {len(main_nodes)} ≠ 证据行 {len(rows)} ＋ 背景卡 "
                       f"{expected - len(rows)}（守「每行一篇」：两处都要在图上有位）")
    without = [node.get("id") for node in main_nodes
               if not node.get("evidence_row") and not node.get("card")]
    if without:
        return bad(f"主节点缺证据行与卡 {len(without)} 个：{'、'.join(str(x) for x in without[:4])}")
    anchorless = [node.get("card") for node in nodes if node.get("card") and f"card-{node['card']}" not in ids]
    if anchorless:
        return bad(f"`card` 形状节点无对应卡锚点 {len(anchorless)} 个：{'、'.join(anchorless[:4])}")

    # ② 点与标签在画布内、两点不相交、行号不压邻点
    for node in nodes:
        box = graph_box(node)
        if box[0] < -0.5 or box[1] < -0.5 or box[2] > GRAPH_CANVAS[0] + 0.5 or box[3] > GRAPH_CANVAS[1] + 0.5:
            return bad(f"节点越出画布：{node.get('id')}（{box[0]:.1f},{box[1]:.1f}–{box[2]:.1f},{box[3]:.1f}）")
    for index, one in enumerate(nodes):
        for other in nodes[index + 1:]:
            gap = math.hypot(one["x"] - other["x"], one["y"] - other["y"]) - one["radius"] - other["radius"]
            if gap < -0.4:
                return bad(f"两点相交：{one.get('id')} 与 {other.get('id')} 圆心距 "
                           f"{math.hypot(one['x'] - other['x'], one['y'] - other['y']):.1f} ＜ 半径和 "
                           f"{one['radius'] + other['radius']:.1f}")
    for node in main_nodes:
        num = node.get("num")
        if not node.get("evidence_row"):
            if num:
                return bad(f"无表行的背景卡带了行号：{node.get('id')}（背景文献不编号）")
            continue
        if not isinstance(num, dict) or not isinstance(num.get("box"), list) or len(num["box"]) != 4:
            return bad(f"主节点缺行号落位：{node.get('id')}（每行一篇都要带号，且要记下落位框）")
        fits = math.hypot(float(num["w"]) / 2, float(num["h"]) / 2) + 0.5 <= node["radius"]
        if fits and num.get("mode") != "inside":
            return bad(f"行号放得进圆内却外置：{node.get('id')}（r={node['radius']}，框 {num['w']}×"
                       f"{num['h']}）——放得下就该放圆内")
        if not fits and num.get("mode") == "inside":
            return bad(f"行号放不进圆内却标了圆内：{node.get('id')}（r={node['radius']}，框 {num['w']}×"
                       f"{num['h']}）——放不下要外置避让，不许压字")
        box = [float(value) for value in num["box"]]
        for other in nodes:
            if other.get("id") == node.get("id"):
                continue
            if graph_box_hit(box, graph_box(other)):
                return bad(f"行号压住邻点：{node.get('id')} 的文献号框与 {other.get('id')} 的圆相交")
    boxes = [(node.get("id"), [float(value) for value in node["num"]["box"]])
             for node in main_nodes if node.get("evidence_row")]
    for index, (one_id, one) in enumerate(boxes):
        for other_id, other in boxes[index + 1:]:
            if graph_box_hit(one, other):
                return bad(f"两个行号框相交：{one_id} 与 {other_id}")
    for label in data.get("labels") or []:
        box = graph_label_box(label)
        if box[0] < -0.5 or box[1] < -0.5 or box[2] > GRAPH_CANVAS[0] + 0.5 or box[3] > GRAPH_CANVAS[1] + 0.5:
            return bad(f"主题标签越出画布：{label.get('text')}（{box[0]:.1f},{box[1]:.1f}–{box[2]:.1f},{box[3]:.1f}）")
        for node in nodes:
            if graph_box_hit(box, graph_box(node)):
                return bad(f"主题标签压住节点：{label.get('text')} 与 {node.get('id')} 的圆相交")

    for node in nodes:  # 极坐标 ⇄ 画布坐标可复算（决定 2 的映射式）
        rho, theta = float(node.get("rho") or 0.0), float(node.get("theta") or 0.0)
        want_x = GRAPH_CENTRE[0] + GRAPH_ELLIPSE[0] * rho * math.cos(theta)
        want_y = GRAPH_CENTRE[1] + GRAPH_ELLIPSE[1] * rho * math.sin(theta)
        if abs(want_x - node["x"]) > 0.3 or abs(want_y - node["y"]) > 0.3:
            return bad(f"椭圆映射不可复算：{node.get('id')} 记 ({node['x']}, {node['y']})，按 ρ={rho}／"
                       f"θ={theta} 应为 ({want_x:.1f}, {want_y:.1f})"
                       f"（x＝600＋554·ρ·cosθ、y＝450＋404·ρ·sinθ）")
    if len({round(float(node.get("rho") or 0.0), 6) for node in main_nodes}) < 2:
        return bad("主节点仍共圆：归一极径只有 1 个取值（决定 1：区内按子环铺开）")

    # ③ 编码：点径与被引数按口径可复算一致；量程与幂是契约（ADR-0030 决定 5／6／12）
    size = (data.get("encoding") or {}).get("size") or {}
    shape_cells = (data.get("layout") or {}).get("cells") or {}
    if abs(float(size.get("power") or 0.0) - GRAPH_POWER) > 1e-6:
        return bad(f"尺寸口径不是 `r ∝ c^(1/3)`：power={size.get('power')}（决定 5 的幂映射）")
    shrink = float(truncated.get("size_shrink") or 1.0)
    for layer, key in ((True, "main"), (False, "neigh")):
        spec = size.get(key) or {}
        scale = (float(spec.get("min_r") or 0.0), float(spec.get("max_r") or 0.0))
        want = GRAPH_SIZE_RANGE[key]
        if abs(shrink - 1.0) < 0.01:
            if abs(scale[0] - want[0]) > 0.01 or abs(scale[1] - want[1]) > 0.01:
                return bad(f"{key} 半径量程 {scale} ≠ 契约 {want}（两层各自一把尺，量程由点径上限定）")
        elif not 0.0 < scale[0] < scale[1] <= want[1] + 0.01:
            return bad(f"{key} 收缩后的半径量程 {scale} 越界（收缩只能更小：0 ＜ min ＜ max ≤ {want[1]}）")
        cell = float(shape_cells.get(key) or 0.0)
        if abs(cell - (2 * scale[1] + GRAPH_CELL_PLACEHOLDER[key])) > 0.05:
            return bad(f"{key} 单点方格 {cell} ≠ 点径上限×2 ＋ 占位 "
                       f"{GRAPH_CELL_PLACEHOLDER[key]}（应 {2 * scale[1] + GRAPH_CELL_PLACEHOLDER[key]:.1f}）")
        domain = (spec.get("lo"), spec.get("hi"))
        for node in nodes:
            if bool(node.get("main")) != layer:
                continue
            cites = node.get("cited_by_count")
            expect = graph_radius(cites, domain[0], domain[1], scale, GRAPH_POWER)
            if abs(float(node.get("radius") or 0.0) - expect) > 0.05:
                return bad(f"点径与被引数不符：{node.get('id')} 被引 {cites} 应得 r={expect:.2f}，"
                           f"实记 {node.get('radius')}")
            missing = not isinstance(cites, int)   # c=0 归最小档（不并入「未取到」档）
            if bool(node.get("cites_missing")) != missing:
                return bad(f"缺被引档标记不符：{node.get('id')} 被引 {cites}（c≤0 与缺值同落最小档）")
    spec_main = (size.get("main") or {})
    ladder = [graph_radius(cites, spec_main.get("lo"), spec_main.get("hi"),
                           (float(spec_main.get("min_r") or 0.0), float(spec_main.get("max_r") or 0.0)),
                           GRAPH_POWER) for cites in (0, 1, 2)]
    if not (ladder[0] <= ladder[1] <= ladder[2]):
        return bad(f"尺寸编码倒挂：c=0／1／2 得半径 {ladder}（c=0 须归最小档，r 单调不减）")
    if any(node.get("shape") not in ("card", "plain") for node in nodes):
        return bad("节点形状取值不在 card／plain（形状＝有无提取卡）")
    if any(node.get("shape") == "card" and not node.get("card") for node in nodes):
        return bad("`shape=card` 的节点没有卡身份（形状与有无卡须一致）")
    for node in nodes:
        if node.get("tier") not in GRAPH_TIERS:
            return bad(f"可及性描边取值非法：{node.get('id')} tier={node.get('tier')}")
    bands = {band.get("label"): band for band in data.get("year_bands") or []}
    for label, opacity in GRAPH_YEAR_BANDS:
        if label not in bands or abs(float(bands[label].get("opacity") or 0.0) - opacity) > 1e-9:
            return bad(f"年份档定义走样：{label} 应为 {opacity}（ADR-0027 的四档绝对日历）")
    if GRAPH_UNKNOWN_YEAR not in bands:
        return bad(f"年份档缺「{GRAPH_UNKNOWN_YEAR}」单列档（缺年不并入最新档）")
    for node in nodes:
        want = bands.get(node.get("year_band"))
        if want is None or abs(float(node.get("opacity") or 0.0) - float(want["opacity"])) > 1e-9:
            return bad(f"透明度与年份档不符：{node.get('id')} 档 {node.get('year_band')} "
                       f"opacity={node.get('opacity')}")

    # ④ 扇区张角 ∝ Σ 点数×单点方格（决定 1：面积式分角，不再用「3×主＋邻」）
    sectors = data.get("sectors") or []
    if not sectors:
        return bad("缺 `sectors`（扇区张角与点数的面积账是图的几何契约）")
    cells_now = (float(shape_cells.get("main") or GRAPH_CELLS_SIDE["main"]),
                 float(shape_cells.get("neigh") or GRAPH_CELLS_SIDE["neigh"]))
    total_weight = sum(float(sector.get("weight") or 0.0) for sector in sectors) or 1.0
    span_sum = 0.0
    for sector in sectors:
        weight = (int(sector.get("main") or 0) * cells_now[0] ** 2
                  + int(sector.get("neigh") or 0) * cells_now[1] ** 2)
        if abs(float(sector.get("weight") or 0.0) - weight) > 0.5:
            return bad(f"扇区权重 ≠ Σ 点数×单点方格：{sector.get('key')} 记 {sector.get('weight')}，"
                       f"按 {sector.get('main')} 主／{sector.get('neigh')} 邻应为 {weight:.0f}")
        want = 2 * math.pi * weight / total_weight
        if abs(float(sector.get("span") or 0.0) - want) > 0.002:
            return bad(f"扇区张角不按面积分配：{sector.get('key')} 记 {sector.get('span')}，"
                       f"按权重应为 {want:.6f}")
        span_sum += float(sector.get("span") or 0.0)
    if abs(span_sum - 2 * math.pi) > 0.01:
        return bad(f"扇区张角之和 {span_sum:.4f} ≠ 2π（整圈须分完）")
    counts = {}
    for node in nodes:
        key = ((node.get("theme") or ""), bool(node.get("main")))
        counts[key] = counts.get(key, 0) + 1
    for sector in sectors:
        for layer, name in ((True, "main"), (False, "neigh")):
            declared = int(sector.get(name) or 0)
            actual = counts.get(((sector.get("key") or ""), layer), 0)
            if declared != actual:
                return bad(f"扇区计数与节点不符：{sector.get('key') or '未归主题'}"
                           f"（{'主' if layer else '邻'}）扇区记 {declared}，数据块实有 {actual}")

    # ⑤ 走廊带：数＝主题对数、条数＝该对边数、带宽 ∝ √条数（决定 8）
    main_ids = {node.get("id") for node in main_nodes}
    for edge in edges:
        if (edge.get("draw") == "line" and edge.get("source") in main_ids
                and edge.get("target") in main_ids
                and theme_of.get(edge.get("source")) != theme_of.get(edge.get("target"))):
            return bad(f"主-主跨主题边仍逐条出线：{edge.get('source')} → {edge.get('target')}"
                       f"（跨主题引文须按主题对聚成带；只与背景点相连的边留在邻域层，默认收起）")
    corridors = data.get("corridors") or []
    band_pairs = graph_pairs([edge for edge in edges if edge.get("draw") == "band"], theme_of.get)
    if len(corridors) != len(band_pairs):
        return bad(f"走廊带 {len(corridors)} 条 ≠ 主题对数 {len(band_pairs)}"
                   f"（跨主题引文须按主题对聚成带，不逐条画线）")
    roots = {tuple(sorted((item.get("a"), item.get("b")))): math.sqrt(item.get("count") or 0)
             for item in corridors}
    total_root = sum(roots.values()) or 1.0
    for corridor in corridors:
        pair = tuple(sorted((corridor.get("a"), corridor.get("b"))))
        if pair not in band_pairs:
            return bad(f"走廊带 {pair} 在图数据里没有对应边（带只承载真有的主题对）")
        if int(corridor.get("count") or 0) != band_pairs[pair]:
            return bad(f"走廊带 {pair} 标 {corridor.get('count')} 条，实有 {band_pairs[pair]} 条")
        want = CORRIDOR_W * roots[pair] / total_root
        if abs(float(corridor.get("width") or 0.0) - want) > 0.05:
            return bad(f"走廊带宽不随 √条数：{pair} 记 {corridor.get('width')}，应为 {want:.2f}")
        if len(corridor.get("ends") or []) != 2 or not isinstance(corridor.get("label"), dict):
            return bad(f"走廊带 {pair} 缺两端端点或条数标注（带上必须能读出这是哪两个主题、多少条）")

    # ⑥ 边：无自环；每条被画出的线都要落在场节点上；邻域层只收有边者
    for edge in edges:
        if edge.get("source") == edge.get("target"):
            return bad(f"自环边未剔除：{edge.get('source')}（出图前须剔除并进读数）")
        if edge.get("source") not in theme_of or edge.get("target") not in theme_of:
            return bad(f"边的端点不在图上：{edge.get('source')} → {edge.get('target')}")
        if edge.get("draw") not in ("line", "band"):
            return bad(f"边缺 `draw` 取值：{edge.get('source')} → {edge.get('target')}")
    drawn_lines = {end for edge in lines for end in (edge.get("source"), edge.get("target"))}
    orphan = [node.get("id") for node in nodes if not node.get("main") and node.get("id") not in drawn_lines]
    if orphan:
        return bad(f"邻域点没有可画的边 {len(orphan)} 个：{'、'.join(orphan[:4])}"
                   f"（邻域层只收「至少有一条被画出的边」者）")
    if not isinstance(truncated.get("isolated_kept"), int):
        return bad("`graph_truncated.isolated_kept` 缺（孤点读数须进数据块）")
    if not isinstance(truncated.get("self_loops"), int):
        return bad("`graph_truncated.self_loops` 缺（自环剔除读数须进数据块）")

    # ⑦ 页内装置：行号数＝主节点数、先读徽章数＝prio 数、字号下限、命中圈、两态取景
    numbered = [node for node in main_nodes if node.get("evidence_row")]
    numbers = len(re.findall(r'class="node-num', page))
    if numbers != len(numbered):
        return bad(f"页面 `.node-num` 数 {numbers} ≠ 有证据行的主节点数 {len(numbered)}"
                   f"（每行一篇都带号；无表行的背景卡按读者面口径不编号）")
    prio = sum(1 for node in main_nodes if node.get("prio"))
    if (len(re.findall(r'class="node-prio-dot"', page)) != prio
            or len(re.findall(r'class="node-prio-num"', page)) != prio):
        return bad(f"先读徽章数 ≠ 有先读名次的主节点数 {prio}（色底＋描边的圆牌与行号在字面上要分得开）")
    font = re.search(r"\.node \.node-num\s*\{[^}]*font-size:\s*([\d.]+)px", page)
    if font is None or float(font.group(1)) < 11.0:
        return bad(f"行号字号 {font.group(1) if font else '缺'}px ＜ 11px 下限（决定 7）")
    if len(re.findall(r'class="node-hit"', page)) != len(main_nodes):
        return bad("命中圈数与主节点数不等（只有主节点有落点，命中圈按主节点算）")
    for node in main_nodes:
        near = [math.hypot(node["x"] - other["x"], node["y"] - other["y"])
                for other in main_nodes if other.get("id") != node.get("id")]
        hit = float(node.get("hit") or 0.0)
        if hit < node["radius"] + 2.0 - 0.05:
            return bad(f"命中圈小于可见点：{node.get('id')} hit={hit} ＜ 半径 {node['radius']} ＋ 2")
        if near and hit > min(near) / 2 + 0.05:
            return bad(f"命中圈大于最近邻距的一半：{node.get('id')} hit={hit} ＞ {min(near) / 2:.2f}"
                       f"（命中区互吞会点错）")
    if len(re.findall(r'class="edge edge-', page)) != len(lines):
        return bad(f"画出的线 {len(re.findall(r'class=.edge edge-', page))} ≠ 数据块里 "
                   f"`draw=line` 的边 {len(lines)}（跨主题引文聚成走廊带后不再各画一条线）")
    if 'id="graph-fold"' not in page or "展开相邻文献" not in page:
        return bad("缺「展开相邻文献」按钮（默认只画主节点，相邻文献按需展开）")
    layer = data.get("layer") or {}
    note = re.search(r'id="graph-fold-note"[^>]*>([^<]*)<', page)
    if layer.get("neigh_nodes") and note and str(layer["neigh_nodes"]) not in note.group(1):
        return bad("收放说明没写真实计数（须写「已收起 N 个相邻文献点与 M 条连线」）")
    if not re.search(r'<g[^>]*data-layer="neigh"[^>]*aria-hidden="true"', page):
        return bad("邻域层未整层 aria-hidden（背景层不进键盘与读屏）")
    if len(re.findall(r'<g class="node[^"]*"[^>]*tabindex="0"', page)) != len(main_nodes):
        return bad("主节点缺 tabindex=0（键盘等价：Enter／Space 与点击同效）")
    for label in data.get("labels") or []:
        if "§" in str(label.get("text") or ""):
            return bad(f"主题标签带章节号：{label.get('text')}（读者面只写名字，ADR-0028 义务 15）")
    if not re.search(r"\.node-prio-dot\s*\{[^}]*stroke\s*:", page):
        return bad("先读徽章缺描边（色底＋描边才与圆内行号在字面上分得开）")
    if not re.search(r"^.*\.band-end.*print-color-adjust.*$", page, re.M):
        return bad("打印面缺 `print-color-adjust: exact` 覆盖走廊端点色（端点色是引用标记，导出 PDF 要印得出）")
    frame = data.get("frame") or {}
    expect = graph_extent([graph_box(node) for node in main_nodes]
                          + [graph_label_box(label) for label in data.get("labels") or []]
                          + graph_corridor_boxes(corridors))
    want_frame = (f"{expect[0] - FRAME_PAD:.0f} {expect[1] - FRAME_PAD:.0f} "
                  f"{expect[2] - expect[0] + 2 * FRAME_PAD:.0f} {expect[3] - expect[1] + 2 * FRAME_PAD:.0f}")
    view = re.search(r'<svg[^>]*\bviewBox="([^"]+)"[^>]*\bdata-viewbox-main="([^"]+)"', page)
    if (frame.get("main") != want_frame or view is None
            or view.group(1) != want_frame or view.group(2) != want_frame):
        return bad(f"折叠态取景不是同一批坐标的纯裁剪：记 {frame.get('main')}／首帧 "
                   f"{(view.group(1) if view else '缺')}，按主节点＋走廊＋标签＋{FRAME_PAD:.0f} 应为 {want_frame}")
    if frame.get("all") != f"0 0 {GRAPH_CANVAS[0]:.0f} {GRAPH_CANVAS[1]:.0f}":
        return bad(f"展开态取景 {frame.get('all')} ≠ 画布（两态只换 viewBox、坐标不动）")

    # ⑧ 墨水铺满：全墨水包围盒（点 ∪ 走廊 ∪ 标签）÷ 可用区
    ink = graph_extent([graph_box(node) for node in nodes]
                       + [graph_label_box(label) for label in data.get("labels") or []]
                       + graph_corridor_boxes(corridors))
    ratio = ((ink[2] - ink[0]) / GRAPH_USABLE[0], (ink[3] - ink[1]) / GRAPH_USABLE[1])
    node_ink = graph_extent([graph_box(node) for node in nodes])
    band = (2 * GRAPH_ELLIPSE[0] * NODE_BAND_R / GRAPH_ELLIPSE[1], 2 * NODE_BAND_R)
    node_ratio = ((node_ink[2] - node_ink[0]) / band[0], (node_ink[3] - node_ink[1]) / band[1])
    if min(node_ratio) < GRAPH_INK_MIN:
        return bad(f"点未铺满自己的带：节点包围盒占节点带（{band[0]:.0f}×{band[1]:.0f}）"
                   f"{node_ratio[0]:.1%}×{node_ratio[1]:.1%} ＜ {GRAPH_INK_MIN:.0%}")
    if min(ratio) < GRAPH_INK_MIN:
        return bad(f"墨水未铺满：全墨水包围盒占可用区 {ratio[0]:.1%}×{ratio[1]:.1%} ＜ "
                   f"{GRAPH_INK_MIN:.0%}（节点带按环心止于 NODE_BAND_R，画满画布的那一层靠走廊环与标签圈）")

    # ⑨ 降级事实：引文面不可得须出降级块；可得却零种子零边须标降级
    raw_present = bool(list((delivery / "run/b1/raw").glob("citation-*.json"))) \
        if (delivery / "run/b1/raw").is_dir() else False
    if not raw_present and DEGRADE_BLOCK_MARKER not in page:
        return bad("引文数据不可得但缺降级块（图位恒在：降级块须在场）")
    if raw_present and not degraded and 'class="graph"' not in page:
        return bad("页内缺关系图 SVG（数据可得却无图）")
    seeds_reading = (data.get("readings") or {}).get("seeds")
    if raw_present and not degraded and not seeds_reading and not edges:
        return bad("引文 raw 件在场但数据块零种子零边，且未标降级——整轮请求失败／载荷不可解析须出降级块")
    return ok(f"图几何：主节点 {len(main_nodes)}（＝证据行＋背景卡）／邻域 {len(nodes) - len(main_nodes)}、"
              f"点与标签全在画布内且互不相交（行号 {len(numbered)} 个不压邻点、背景卡不编号）、"
              f"扇区 {len(sectors)} 个张角 ∝ 点数×单点方格、走廊 {len(corridors)} 条＝主题对数、"
              f"点铺满节点带 {node_ratio[0]:.1%}×{node_ratio[1]:.1%}、全墨水占可用区 "
              f"{ratio[0]:.1%}×{ratio[1]:.1%}、点径按 c^(1/3) 复算一致；"
              f"截断事实在场（cap_hit={truncated.get('cap_hit')}、孤点 {truncated.get('isolated_kept')}）"
              f"{'；降级块在场（引文面缺）' if degraded else ''}")


def w08_render_reconciliation(delivery: Path):
    """W-08：渲染对账——页 sha256 ＝ `versioned_artifacts[]` 项 ＝ `derived.reader_html.sha256`；
    登记输入清单逐件核对（缺席跳过、在场不一致即红）。"""
    manifest_path = delivery / MANIFEST
    if not manifest_path.exists():
        return bad(f"{MANIFEST} 缺件（渲染对账的家）")
    try:
        manifest = read_json(manifest_path)
        slug = str((manifest.get("topic") or {}).get("slug") or "")
    except (OSError, ValueError) as exc:
        return bad(f"{MANIFEST} 解析失败：{type(exc).__name__}: {exc}")
    if not slug:
        return bad(f"{MANIFEST} 缺 topic.slug（对账键无从定）")
    path = page_path_of(delivery, slug)
    if not path.exists():
        return bad(f"人读主件缺件：{slug}.html（对账无从做）")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    registered = next((entry.get("sha256") for entry in manifest.get("versioned_artifacts") or []
                       if isinstance(entry, dict) and entry.get("path") == f"{slug}.html"), None)
    if registered is None:
        return bad(f"versioned_artifacts 缺 {slug}.html 项（进版件逐件登记）")
    if registered != sha:
        return bad(f"versioned_artifacts 登记与页哈希不符（登记 {str(registered)[:12]}／页 {sha[:12]}）")
    derived = (manifest.get("derived") or {}).get("reader_html") or {}
    if not derived:
        return bad("`derived.reader_html` 缺场（派生渲染件的来源、输入清单与命令登记处）")
    if derived.get("sha256") != sha:
        return bad(f"derived.reader_html.sha256 与页哈希不符（登记 {str(derived.get('sha256'))[:12]}／页 {sha[:12]}）")
    if derived.get("path") != f"{slug}.html":
        return bad(f"derived.reader_html.path 与主件名不符：{derived.get('path')}")
    inputs = derived.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        return bad("`derived.reader_html.inputs[]` 缺场（输入清单结构化，可逐件复核）")
    checked = drift = 0
    for entry in inputs:
        rel = str((entry or {}).get("path") or "")
        target = delivery / rel
        if not rel or not target.exists():
            continue
        checked += 1
        if hashlib.sha256(target.read_bytes()).hexdigest() != entry.get("sha256"):
            drift += 1
    if drift:
        return bad(f"输入清单与盘上件不符 {drift}／{checked} 件（漂移即红）")
    return ok(f"对账三方相等（versioned_artifacts／derived.reader_html／页 sha256 {sha[:12]}）；"
              f"输入清单 {len(inputs)} 件、在场核对 {checked} 件全等")


def w09_index_skeleton(delivery: Path):
    """W-09：`INDEX.md` 骨架——文件表＋寻址命令表＋唯一一行状态计数＋零自由段落（workspace-guide §9）。"""
    path = delivery / INDEX_NAME
    if not path.exists():
        return bad(f"{INDEX_NAME} 缺件（机读纯索引是交付根固定四项之一）")
    text = path.read_text(encoding="utf-8")
    file_head = next((line for _n, line in body_lines(text)
                      if is_table_row(line) and cells(line)[0] == "路径" and "内容" in cells(line)), None)
    cmd_head = next((line for _n, line in body_lines(text)
                     if is_table_row(line) and cells(line)[0] == "任务" and "命令" in cells(line)), None)
    if file_head is None:
        return bad("缺文件表表头（期望表头含「路径｜内容」两列）")
    if cmd_head is None:
        return bad("缺寻址命令表表头（期望表头含「任务｜命令」两列）")
    if not re.search(r"(?m)^#{1,6}\s.*待查", text):
        return bad("缺「待查」段（本件查不到时的指针纪律）")
    status = [(n, line) for n, line in body_lines(text) if STATUS_LINE_RE.search(line)]
    if len(status) != 1:
        return bad(f"状态计数行 {len(status)} 行（要求恰好一行）")
    free = free_paragraphs(text)
    if free:
        return bad(f"自由段落 {len(free)} 处（第 {free[0][0]} 行：{free[0][1][:40]}）——除状态计数行外不得出现")
    return ok(f"{INDEX_NAME} 骨架齐：文件表＋寻址命令表＋待查段＋状态计数 1 行＋零自由段落"
              f"（{len(body_lines(text))} 行正文）")


def _verdict_section(text):
    """`## Verdict（先读我）` 段（到下一个二级标题或文件末）。"""
    lines = text.splitlines()
    start = next((n for n, line in enumerate(lines) if VERDICT_HEAD_RE.match(line.strip())), None)
    if start is None:
        return None
    end = next((n for n in range(start + 1, len(lines)) if lines[n].startswith("## ")), len(lines))
    return lines[start + 1:end]


def w10_gate_summary_skeleton(delivery: Path):
    """W-10：`agent/gate-summary.md` 骨架＋全量门指针可在 `manifest.regeneration[]` 解（§11、`R7`）。"""
    path = delivery / GATE_SUMMARY
    if not path.exists():
        return bad(f"{GATE_SUMMARY} 缺件（`R7`：门禁摘要常驻）")
    section = _verdict_section(path.read_text(encoding="utf-8"))
    if section is None:
        return bad("缺 `## Verdict（先读我）` 段（§11：verdict 在文件最前）")
    labels = [m.group(1) for line in section if (m := LABEL_RE.match(line))]
    want = ["判定", "未过条目", "缺口去向"]
    if labels[:3] != want:
        return bad(f"verdict 头三行不是固定三项：实测 {labels or '无'}（须 {want}）")
    manifest_path = delivery / MANIFEST
    if not manifest_path.exists():
        return bad(f"{MANIFEST} 缺件，全量门指针无从核对")
    regen = read_json(manifest_path).get("regeneration")
    if not isinstance(regen, list):
        return bad(f"{MANIFEST} 缺 regeneration 段（全量门指针的落点）")
    full = [e for e in regen if isinstance(e, dict) and e.get("class") == FULL_GATE_CLASS]
    if not full:
        return bad(f"{MANIFEST}.regeneration 无 class={FULL_GATE_CLASS} 条目（钻取指针无从解）")
    if not full[0].get("command"):
        return bad(f"{MANIFEST}.regeneration 的 {FULL_GATE_CLASS} 条目缺 command")
    return ok(f"{GATE_SUMMARY} verdict 头固定三行齐（{'／'.join(want)}）；"
              f"全量门指针可解（regeneration 的 {FULL_GATE_CLASS} 条目在场，artifacts "
              f"{len(full[0].get('artifacts') or [])} 件）")


def w11_ledger_pointers(delivery: Path):
    """W-11：`ledger_pointers` 各值带 `agent/` 前缀且目标件在场；`regeneration[].artifacts[].path` 仍是 `run/`。"""
    path = delivery / MANIFEST
    if not path.exists():
        return bad(f"{MANIFEST} 缺件（台账指针的家）")
    manifest = read_json(path)
    pointers = manifest.get("ledger_pointers")
    if not isinstance(pointers, dict):
        return bad(f"{MANIFEST} 缺 ledger_pointers 段（§10：版内台账位置）")
    missing_keys = [k for k in POINTER_KEYS if k not in pointers]
    if missing_keys:
        return bad(f"ledger_pointers 缺键：{'、'.join(missing_keys)}（须 {'／'.join(POINTER_KEYS)}）")
    checked = 0
    for key in POINTER_KEYS + POINTER_OPTIONAL:
        if key not in pointers:
            continue
        value = str(pointers[key]).strip()
        if not value:
            return bad(f"ledger_pointers.{key} 空值（缺席须记「无…」，不得留空）")
        if value.startswith("无"):
            continue
        if not value.startswith(f"{AGENT_DIR}/"):
            return bad(f"ledger_pointers.{key} 未带 {AGENT_DIR}/ 前缀：{value}")
        if not (delivery / value).exists():
            return bad(f"ledger_pointers.{key} 指向的件不在场：{value}")
        checked += 1
    regen = manifest.get("regeneration")
    if not isinstance(regen, list):
        return bad(f"{MANIFEST} 缺 regeneration 段")
    stray = []
    for entry in regen:
        for artifact in (entry.get("artifacts") or []) if isinstance(entry, dict) else []:
            artifact_path = str((artifact or {}).get("path") or "")
            if not artifact_path.startswith(f"{RUN_DIR}/"):
                stray.append(artifact_path or "（空路径）")
    if stray:
        return bad(f"regeneration 的 artifacts[].path 须保持 {RUN_DIR}/ 前缀：{stray[:3]}")
    return ok(f"ledger_pointers 在场件前缀与在场各就位（逐键核对 {checked} 条）；"
              f"regeneration 被忽略件路径前缀不变（{sum(len(e.get('artifacts') or []) for e in regen)} 件）")


def w12_card_parse_health(delivery: Path):
    """W-12：卡面解析健康——解析条数＝`agent/cards/*.md` 件数且 > 0；0 卡／目录缺显式报错（含读取路径与解析条数）。

    P1 快线（有 `agent/scoping-note.md`、无八节包）无卡面，本条记「不适用」；两者皆无＝卡面无主，判不过。
    """
    if not (delivery / package_parse.PACKAGE_REL).exists():
        if (delivery / SCOPING_NOTE).exists():
            return ok(f"P1 快线形态（{SCOPING_NOTE} 在、无 {package_parse.PACKAGE_REL}）：无卡面，本条不适用")
        return bad(f"卡面无主：既无 {package_parse.PACKAGE_REL}（P0–P3 八节包）也无 {SCOPING_NOTE}（P1 快线）")
    try:
        cards_ = package_parse.parse_cards(delivery)
    except package_parse.CardParseError as exc:  # 0 卡／目录缺＝硬门禁失败，读数带读取路径与解析条数
        return bad(str(exc))
    return ok(f"卡面解析 {len(cards_)} 张（＝{package_parse.CARDS_GLOB} 件数，解析器自校验；文件名即身份）")


def placement_plan_entries(delivery: Path):
    """落点计划条目集 `[(条目键, 归一 DOI)]`；计划件缺＝None（表解析原语＝共享模块，不另立第二套）。

    DOI 列按表头名定位、缺列回退行内正则；行集＝首格为 8 位条目键者（表头的本次合集行、计数行不计）。
    """
    path = delivery / PLACEMENT_PLAN
    if not path.exists():
        return None
    entries = []
    for header, rows in package_parse.md_tables_with_header(path.read_text(encoding="utf-8")):
        col = next((i for i, c in enumerate(header) if c.strip().upper().startswith("DOI")), None)
        for row in rows:
            if not row or not PLAN_ENTRY_RE.match(row[0]):
                continue
            cell = row[col] if col is not None and len(row) > col else ""
            found = package_parse.DOI_RE.search(cell) or package_parse.DOI_RE.search(" ".join(row))
            entries.append((row[0], package_parse.norm_doi(found.group(1)) if found else ""))
    return entries


def card_identities(delivery: Path):
    """卡面身份集 `{归一 DOI（卡内无 DOI 时退文件 stem）: 卡号}`（解析复用 `package_parse.parse_cards`）。"""
    cards = package_parse.parse_cards(delivery)
    return {(card["doi"] or card["slug"]): cid for cid, card in cards.items()}


def placement_caliber(delivery: Path, plan_text):
    """写库在组口径三级读法（D-10 同式）：落点计划注记 → 包清单指针 → 有无计划件推断。

    返回 `(判定值, 判定层)`；`skip` ＝写库设计跳过（P1／P2／P3 无落点计划，本条不判）——判定层进读数，
    豁免不静默（与 D-10／D-11 的读法同形）。
    """
    if plan_text is not None and WRITE_SKIP_MARK in plan_text:
        return "skip", "落点计划注记"
    manifest_path = delivery / MANIFEST
    if manifest_path.exists():
        try:
            pointer = str(((read_json(manifest_path).get("ledger_pointers") or {}).get("placement_plan")) or "")
        except (OSError, ValueError):
            pointer = ""
        if pointer and not pointer.startswith(f"{AGENT_DIR}/"):
            return "skip", f"包清单 ledger_pointers.placement_plan 记「无」（{pointer}）"
    if plan_text is None:
        return "skip", "有无计划件推断（落点计划缺件）"
    return "present", "有无计划件推断（落点计划在场）"


def capped_items(items):
    """差异逐条列举：前 `DIFF_SHOW` 条，超出截断（总数由调用方的读数句另给）。"""
    return "、".join(items[:DIFF_SHOW]) + ("…" if len(items) > DIFF_SHOW else "")


def w22_inclusion_set(delivery: Path):
    """W-22（硬门禁，ADR-0032）：入库集守恒——落点计划条目集 ≡ 卡面 DOI 集（逐键比对）。

    适用门＝写库在组（含 `agent/placement-plan.md`）：写库设计跳过的路线（P1／P2／P3 无落点计划）记「不适用」
    不阻断，判据沿 D-10 同式三级读法（落点计划注记 → 包清单指针 → 有无计划件推断），判定层进读数、豁免不静默；
    自报不实——自报写库设计跳过却仍带计划表条目行——即判不过（镜像 D-10）。差异分两列：缺卡（有计划行无卡）、
    多卡（有卡无计划行），各列前若干条并给总数；两侧皆空记「不适用（两侧皆空）」。卡面解析健康归 W-12，本条不重判。
    """
    plan_exists = (delivery / PLACEMENT_PLAN).exists()
    plan_text = (delivery / PLACEMENT_PLAN).read_text(encoding="utf-8") if plan_exists else None
    entries = placement_plan_entries(delivery) or []
    caliber, layer = placement_caliber(delivery, plan_text)
    if caliber == "skip":
        if entries:
            return bad(f"自报写库设计跳过却仍带计划表条目行 {len(entries)} 条（判定层：{layer}；"
                       f"首条 {entries[0][0]}）——自报不实即判不过")
        return ok(f"不适用：写库设计跳过（判定层：{layer}）——无落点计划，本条不判")
    try:
        card_keys = card_identities(delivery)
    except package_parse.CardParseError as exc:  # 卡面健康归 W-12：解析不可用时不重复判同一条
        return ok(f"不适用：卡面解析不可用，守恒等式本轮不作判（卡面健康归 W-12：{exc}）")
    plan_keys = {}
    for key, doi in entries:
        plan_keys.setdefault(doi or key, key)
    if not plan_keys and not card_keys:
        return ok("不适用：落点计划与卡面两侧皆空（写库在组但无等式可判）")
    missing = sorted(k for k in plan_keys if k not in card_keys)
    extra = sorted(k for k in card_keys if k not in plan_keys)
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"缺卡 {len(missing)}（有计划行无卡：{capped_items(missing)}）")
        if extra:
            parts.append(f"多卡 {len(extra)}（有卡无计划行：{capped_items(extra)}）")
        return bad(f"入库集不守恒：{'、'.join(parts)}；计划 {len(plan_keys)} 键／卡面 {len(card_keys)} 张")
    return ok(f"入库集守恒：落点计划 {len(plan_keys)} 键 ≡ 卡面 {len(card_keys)} 张"
              f"（DOI 逐键比对；缺卡 0／多卡 0）")


IDENT_BLOCK_NEEDLE = 'class="fbody ident"'  # 卡内「文献」段逐行身份块的页内记号（执行体同源）
PRIO_ITEM_SPLIT = '<li class="prio-item">'  # 先读行块（ADR-0031）：题名与卡链接同块的界
PRIO_TITLE_RE = re.compile(r'<span class="prio-title">(.*?)</span>', re.S)
CARD_LINK_RE = re.compile(r'data-open-card="([^"]+)"')


def w23_identity_slots(delivery: Path):
    """W-23（硬门禁）：身份段消费面——逐行块对账、先读行题名逐字、回退披露。

    卡头题名、卡片索引 chip、先读行题名与卡内「文献」段同源于卡内身份段；位约定不成立时整段回退原样
    是保真口径，但**回退不得静默**：逐行张数与回退名单登记在 `derived.reader_html.counts`，本条拿它与
    页面块数、卡文件数对账；另判**合规数据不得被拒**——身份段按位切分具备形态（≥5 位且第 2 位年份开头）
    的卡若多于逐行张数，即渲染器拒了照位约定写的卡（卡头题名与身份块一并静默失效）。
    **先读行题名逐字取自源题名**：先读行是「认文献」的主场，砍半句或砍掉副题即判不过
    （ADR-0031 窄修订；索引 chip 的 56 字单行截断不受本条约束）。
    无卡域（P1 快线）记「不适用」；卡面解析健康归 W-12、渲染对账归 W-08，本条不重判。
    """
    cards_dir = delivery / package_parse.CARDS_REL
    files = sorted(cards_dir.glob(f"*{package_parse.CARD_SUFFIX}")) if cards_dir.is_dir() else []
    if not files:
        return ok("不适用：无卡域（P1 快线／无卡路线不产该层）")
    manifest_path = delivery / MANIFEST
    if not manifest_path.exists():
        return bad(f"{MANIFEST} 缺件（身份段覆盖读数的登记处）")
    try:
        derived = (read_json(manifest_path).get("derived") or {}).get("reader_html") or {}
    except (OSError, ValueError) as exc:
        return bad(f"{MANIFEST} 解析失败：{type(exc).__name__}: {exc}")
    counts = derived.get("counts")
    if not isinstance(counts, dict):
        return bad("`derived.reader_html.counts` 缺场（渲染器须登记身份段逐行张数与回退名单——回退不静默）")
    rows, fallback = counts.get("ident_rows"), counts.get("ident_fallback")
    if not isinstance(rows, int) or isinstance(rows, bool) or not isinstance(fallback, list):
        return bad(f"`counts` 形态不符：ident_rows={rows!r}／ident_fallback={type(fallback).__name__}")
    page = read_page(delivery)
    if page is None:
        return bad("人读主件读不到（页面块数无从核对；主件在场性归 W-01）")
    shaped = 0
    for path in files:
        body = reader_html.identity_body(reader_html.parse_card_blocks(path.read_text(encoding="utf-8")))
        slots = reader_html.split_identity_slots(body)
        if len(slots) >= 5 and reader_html.IDENT_YEAR_RE.match(slots[1]):
            shaped += 1
    issues = []
    blocks = page.count(IDENT_BLOCK_NEEDLE)
    if rows != blocks:
        issues.append(f"登记逐行 {rows} ≠ 页面逐行块 {blocks}")
    if rows + len(fallback) != len(files):
        issues.append(f"逐行 {rows}＋回退 {len(fallback)} ≠ 卡件数 {len(files)}")
    if rows < shaped:
        issues.append(f"身份段具备位形态的卡 {shaped} 张，其中 {shaped - rows} 张未逐行渲染"
                      f"（合规数据被拒：卡头题名与身份块静默失效）")
    titles, title_hits = priority_title_check(delivery, page)
    if title_hits:
        issues.append(f"先读行题名非源题名 {len(title_hits)}／{titles} 行（{capped_items(title_hits)}）"
                      f"——先读行是认文献的主场，题名须整条呈现（ADR-0031 窄修订）")
    if issues:
        names = "、".join(str((item or {}).get("card")) for item in fallback[:3])
        return bad("；".join(issues) + (f"；回退名单：{names}" if fallback else ""))
    return ok(f"身份段逐行 {rows}／{len(files)} 张＝页面块 {blocks}；回退 {len(fallback)} 张"
              f"（位形态卡 {shaped} 张全数逐行；回退逐卡点名在案）；"
              f"先读行题名 {titles} 行逐字取自源题名")


def priority_title_check(delivery: Path, page: str) -> tuple:
    """先读行题名 vs 卡内源题名：返回（先读行数, 不符行的描述）。

    左侧＝页面 `.prio-title` 文本，右侧＝该行卡片的 `card_head_texts()` 题名（身份段源题名为先、
    卡首行兜底为后）——两侧都用渲染器的同一函数，判的是「有没有在渲染时被改写」而不是词面口径。
    行与卡的对应由同一块里的 `data-open-card` 给出（无卡按钮的行按卡锚点链接回退取值）。
    """
    try:
        cards = package_parse.parse_cards(delivery)
    except package_parse.CardParseError as exc:  # 卡面健康归 W-12
        return 0, []
    expected = {}
    for card in cards.values():
        blocks = reader_html.parse_card_blocks((delivery / card["文件"]).read_text(encoding="utf-8"))
        head = reader_html.card_head_texts({"_ident_body": reader_html.identity_body(blocks), "_who": ""})
        expected[card["slug"]] = (head["title"] or "").strip()
    items = page.split(PRIO_ITEM_SPLIT)[1:]
    hits = []
    for item in items:
        title = PRIO_TITLE_RE.search(item)
        slug = CARD_LINK_RE.search(item)
        if not title or not slug:
            continue
        shown = html_lib.unescape(title.group(1)).strip()
        want = expected.get(html_lib.unescape(slug.group(1)))
        if want is not None and shown != want:
            hits.append(f"{html_lib.unescape(slug.group(1))[:24]}「{shown[:40]}」")
    return len(items), hits


# GB/T 类型标识（`[J].`／`[M].`／`[EB/OL].` 一族）：真源列是 GB/T 单条的判据。
# 页面行头呈题目置前（`lit_parts` 拆出的题名位），与真源列顺序无关——本门只判真源列形态。
LIT_MARK_RE = re.compile(r"\[[A-Za-z]{1,3}(?:/[A-Z]{2})?\]\s*[.．]")
LIT_MAIN_MIN = 6  # 去掉标识与作者段后题名位的可见字符下限；只剩作者串即题名丢失


def w24_title_of(lit: str) -> str:
    """真源「文献」列 → 题名位：`著者. 题名[X]. …` 取著者段之后、类型标识之前。"""
    text = re.sub(r"\s*\.?\s*DOI\s+10\.\S+\s*$", "", (lit or "").strip(), flags=re.I)
    m = re.search(r"^(?P<authors>.+?\.)\s*(?P<title>.+?)\s*\[[A-Za-z]{1,3}(?:/[A-Z]{2})?\]\s*[.．]", text)
    return (m.group("title") if m else "").strip()


def w24_literature_column(delivery: Path):
    """W-24（硬门禁）：证据表「文献」列可拆档且有题名。

    渲染器把「文献」列拆成三行：主行＝题名、副行＝作者 · 年份 · 期刊 · 卷页、末行＝DOI。
    形态不合（`作者，年，期刊，DOI` 一类自拼简式）时它**静默退化**——整格塞进主行、
    副行不渲染：读者看到的正是「作者＋期刊」，题名整段消失，而 W-04 的 DOI 检查照过。
    故本条守三件：① 拆档成功（年份位四位年、期刊位非空）；② 真源列带 GB/T 类型标识；
    ③ 题名位非空（只剩作者串＝题名缺位）。
    P1 快线（无八节包）记「不适用」；引文表落点归 W-11、题名派生与身份段归 W-23。
    """
    package_path = delivery / package_parse.PACKAGE_REL
    if not package_path.exists():
        return ok("P1 快线：无证据表，本条不适用")
    rows = package_parse.evidence_rows(package_path.read_text(encoding="utf-8"))
    if not rows:
        return bad("§5 证据表解析到 0 行（「文献」列无从核对）")
    offenders = []
    for num, row in sorted(rows.items(), key=lambda kv: package_parse.row_sort_key(kv[0])):
        _title, _authors, year, venue, _rest = reader_html.lit_parts(row.get("文献") or "")
        title = w24_title_of(row.get("文献") or "")
        if not re.fullmatch(r"(?:19|20)\d{2}", year or "") or not venue:
            offenders.append(f"{num}（拆不出年份／期刊位：页面退化为整格塞进主行、副行不渲染）")
        elif not LIT_MARK_RE.search(row.get("文献") or ""):
            offenders.append(f"{num}（真源列缺 GB/T 类型标识：不是 GB/T 单条）")
        elif len(re.sub(r"\s+", "", title)) < LIT_MAIN_MIN:
            offenders.append(f"{num}（题名位缺位：只剩作者串）")
    if offenders:
        return bad(f"§5「文献」列不合法 {len(offenders)}／{len(rows)} 行：{'；'.join(offenders[:3])}"
                   f"——「文献」列须为 GB/T 7714 单条（作者. 题名[J]. 期刊, 年, 卷(期): 页），"
                   f"自拼「作者，年，期刊」类简式会让题名在页面上整段消失")
    return ok(f"文献列可拆档且有题名：§5 行 {len(rows)} 行的年份·期刊位与题名位全部在场")


# ---------------------------------------------------------------- 软项读数（不阻断）


def w14_index_banned_words(delivery: Path):
    path = delivery / INDEX_NAME
    if not path.exists():
        return bad(f"{INDEX_NAME} 缺件，禁词面无从核对")
    hits = []
    for n, line in body_lines(path.read_text(encoding="utf-8")):
        if is_table_row(line) or STATUS_LINE_RE.search(line):
            continue
        hits += [f"第 {n} 行「{w}」" for w in BANNED_WORDS if w in line]
    if hits:
        return bad(f"禁词面命中 {len(hits)} 处（{hits[0]}）——索引只列文件与命令，判定词属人读层")
    return ok(f"{INDEX_NAME} 禁词面零命中（{'／'.join(BANNED_WORDS)}）")


def w15_top_level(delivery: Path):
    entries = entry_names(delivery)
    manifest_path = delivery / MANIFEST
    slug, manifest_note = None, ""
    if manifest_path.exists():
        try:
            slug = ((read_json(manifest_path).get("topic") or {}).get("slug")) or None
        except (OSError, ValueError) as exc:  # 读数降级不静默：把读不出的件与原因打进读数
            manifest_note = f"（{MANIFEST} 的 topic.slug 读取失败：{type(exc).__name__}: {exc}）"
    want = sorted([*TOP_LEVEL, f"{slug}.html" if slug else "<slug>.html"])
    if entries == want:
        return ok(f"顶层恰四项：{'、'.join(entries)}" + manifest_note)
    return bad(f"顶层 {len(entries)} 条目：{'、'.join(entries)}（期望四项：{'、'.join(want)}）" + manifest_note)


def w16_agent_entries(delivery: Path):
    agent_dir = delivery / AGENT_DIR
    if not agent_dir.is_dir():
        return bad(f"{AGENT_DIR}/ 缺件")
    entries = entry_names(agent_dir)
    if len(entries) <= AGENT_MAX_ENTRIES:
        return ok(f"{AGENT_DIR}/ 直下 {len(entries)} 条目（封顶 {AGENT_MAX_ENTRIES}；`cards/` 逐卡不计）")
    return bad(f"{AGENT_DIR}/ 直下 {len(entries)} 条目，超封顶 {AGENT_MAX_ENTRIES}：{'、'.join(entries)}")


def w17_volume(delivery: Path):
    page = next((p for p in delivery.iterdir() if p.is_file() and p.suffix.lower() == ".html"), None)
    page_size = page.stat().st_size if page else 0
    agent_size = sum(p.stat().st_size for p in (delivery / AGENT_DIR).rglob("*") if p.is_file())
    total = page_size + agent_size
    reading = (f"人读主件 {page.name if page else '缺'} {page_size / 1024:.0f}KB"
               f"＋{AGENT_DIR}/ {agent_size / 1024:.0f}KB＝{total / 1024 / 1024:.2f}MB（封顶 2.5MB）")
    if total <= VOLUME_MAX_BYTES:
        return ok(reading)
    return bad(reading + "——超封顶，记读数不阻断")


def w18_verdict_names_executor(delivery: Path):
    """W-18：verdict 头**判定行**须引用本执行体读数（§11：判定行引用三件套读数）。"""
    path = delivery / GATE_SUMMARY
    if not path.exists():
        return bad(f"{GATE_SUMMARY} 缺件，判定行无从核对")
    section = _verdict_section(path.read_text(encoding="utf-8")) or []
    line = next((line for line in section if LABEL_RE.match(line) and LABEL_RE.match(line).group(1) == "判定"), None)
    if line is None:
        return bad("verdict 头缺判定行")
    if Path(__file__).name not in line:
        return bad(f"verdict 头判定行未含本执行体读数（{Path(__file__).name}）——交付门禁已改三件套")
    return ok(f"verdict 头判定行含本执行体读数（{Path(__file__).name}）")


def w19_probe_reading(delivery: Path):
    """W-19：摘要读数列须含抽跑读数（§11 抽跑一条：`--card-probe` 输出行照录，含「样本缺位」）。

    判据＝摘要里存在引用 `--card-probe` 且带 `RESULT: …` 或「样本缺位」的行；不判该行之外的格式。
    """
    path = delivery / GATE_SUMMARY
    if not path.exists():
        return bad(f"{GATE_SUMMARY} 缺件，抽跑读数无从核对")
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if "card-probe" in line]
    if not lines:
        return bad("摘要未记抽跑读数（§11：`--card-probe` 输出行照录进「交付门禁读数」表）")
    reading = next((line for line in lines if "RESULT:" in line or "样本缺位" in line), None)
    if reading is None:
        return bad(f"摘要引了抽跑但未记读数行（须含 `RESULT: …` 或「样本缺位」）：{lines[0].strip()[:60]}")
    return ok(f"抽跑读数在案：{reading.strip()[:80]}")


HARD_CHECKS = (
    ("W-01", "人读主件在场与命名", w01_main_artifact),
    ("W-02", "单文件自足", w02_self_contained),
    ("W-03", "锚点完整性", w03_anchors),
    ("W-04", "DOI 可定位", w04_doi_locatable),
    ("W-05", "卡全覆盖", w05_card_coverage),
    ("W-06", "精读序三条", w06_reading_priority),
    ("W-07", "图契约", w07_graph_contract),
    ("W-08", "渲染对账", w08_render_reconciliation),
    ("W-09", "机读纯索引骨架", w09_index_skeleton),
    ("W-10", "门禁摘要骨架", w10_gate_summary_skeleton),
    ("W-11", "落点一致（台账指针与再生路径）", w11_ledger_pointers),
    ("W-12", "卡面解析健康", w12_card_parse_health),
    ("W-21", "读者面文案零机器记号", w21_reader_copy),
    ("W-22", "入库集守恒（计划≡卡面）", w22_inclusion_set),
    ("W-23", "身份段消费面（逐行块／先读行题名／回退披露）", w23_identity_slots),
    ("W-24", "证据表文献列可拆档且有题名", w24_literature_column),
)
SOFT_CHECKS = (
    ("W-14", "索引禁词面（告警）", w14_index_banned_words),
    ("W-15", "顶层结构读数", w15_top_level),
    ("W-16", "agent/ 件数读数", w16_agent_entries),
    ("W-17", "体积读数", w17_volume),
    ("W-18", "verdict 头判定行含本执行体读数（告警）", w18_verdict_names_executor),
    ("W-19", "摘要含抽跑读数（告警）", w19_probe_reading),
    ("W-20", "精读序形态读数（告警）", w20_reading_priority_form),
)


def evaluate(delivery: Path):
    """跑全部检查：逐条隔离（单条异常不掩盖其余判定），返回 [{id, gate, title, ok, observed}]。"""
    results = []
    for gate, checks in (("硬门禁", HARD_CHECKS), ("软项", SOFT_CHECKS)):
        for aid, title, fn in checks:
            try:
                got = fn(delivery)
            except Exception as exc:  # 逐条隔离：执行错误单列
                got = bad(f"执行错误：{type(exc).__name__}: {exc}") | {"error": True}
            results.append({"id": aid, "gate": gate, "title": title, **got})
    return results


def report(results, *, as_json: bool):
    hard_fail = [r for r in results if r["gate"] == "硬门禁" and not r["ok"]]
    soft_fail = [r for r in results if r["gate"] == "软项" and not r["ok"]]
    if as_json:
        print(json.dumps({"results": results,
                          "hard_fail": [r["id"] for r in hard_fail],
                          "soft_readings": [r["id"] for r in soft_fail]},
                         ensure_ascii=False, indent=1))
    else:
        for r in results:
            mark = "PASS" if r["ok"] else ("FAIL" if r["gate"] == "硬门禁" else "WARN")
            print(f"  {mark}  [{r['id']}] {r['title']}：{r['observed']}")
        print(f"\nRESULT: {'GREEN' if not hard_fail else 'RED'} | 硬门禁未过 {len(hard_fail)} "
              f"{[r['id'] for r in hard_fail]} | 软项读数告警 {len(soft_fail)} {[r['id'] for r in soft_fail]}")
    return 0 if not hard_fail else 1


# ---------------------------------------------------------------- 自证（孪生场景）


TWIN_PACKAGE = (
    "# 自证知识包（self-test）\n"
    "\n"
    "## 1 执行摘要\n"
    "\n"
    "自证摘要（行 1、行 2）。\n"
    "\n"
    "## 2 研究问题\n"
    "\n"
    "- 研究问题（一句话，冻结）：自证问题是什么？\n"
    "\n"
    "## 5 证据表\n"
    "\n"
    "表内七列依次为：文献｜设计与等级｜人群｜干预与暴露｜结局与效应量｜偏倚备注｜与结论的关联。\n"
    "\n"
    "| 行号 | 文献 | 设计与等级 | 人群 | 干预与暴露 | 结局与效应量 | 偏倚备注 | 与结论的关联 |\n"
    "|---|---|---|---|---|---|---|---|\n"
    "| 1 | 自证一 等. 自证题名一号研究[J]. 自证期刊, 2026, 1(1): 1-9. DOI 10.0000/self-test-001 | "
    "随机对照试验 · E1 | 人群 | 干预 | 结局 | 无 | 拟落 §4.1 |\n"
    "| 2 | 自证二 等. 自证题名二号研究[J]. 自证期刊, 2026, 1(2): 11-19. DOI 10.0000/self-test-002 | "
    "系统综述 · E1 · 仅摘要 | 人群 | 干预 | 结局 | 无 | 拟落 §4.1 |\n"
    "| 3 | 自证三 等. 自证题名三号研究[J]. 自证期刊, 2026, 2(1): 21-29. DOI 10.0000/self-test-003 | "
    "随机对照试验 · E1 | 人群 | 干预 | 结局 | 无 | 拟落 §4.2 |\n"
    "| 4 | 自证四 等. 自证题名四号研究[J]. 自证期刊, 2026, 2(2): 31-39. DOI 10.0000/self-test-004 | "
    "队列研究 · E2 | 人群 | 干预 | 结局 | 无 | 拟落 §4.3 |\n"
    "\n"
    "### 5.1 优先精读序\n"
    "\n"
    "先读层（依据序；域内不足 5 篇则全部入层）：\n"
    "\n"
    "| 序 | 行 | 卡 | 维度 | 理由 |\n"
    "|---|---|---|---|---|\n"
    "| 1 | 行 1 | ST1 | 结论关联 | 自证结论的证据底座：两行对齐入层（行 1） |\n"
    "| 2 | 行 2 | ST2 | 主题补位 | 自证对照：仅摘要行亦可入先读层（行 2） |\n"
    "| 3 | 行 3 | ST3 | 权衡价值 | 自证权衡读数：组内序按依据链（行 3） |\n"
    "| 4 | 行 4 | ST4 | 主题补位 | 自证主题补位：域内四篇全入层（行 4） |\n"
    "\n"
    "## 6 结论\n"
    "\n"
    "### 6.2 核心结论\n"
    "\n"
    "自证结论（行 1、行 2）。\n"
)
TWIN_REFERENCE_LIST = (
    "# 引文表（自证孪生）\n"
    "\n"
    "1. 自证一 等. 自证题名一号研究[J]. 自证期刊, 2026, 1(1): 1-9. DOI 10.0000/self-test-001.\n"
    "2. 自证二 等. 自证题名二号研究[J]. 自证期刊, 2026, 1(2): 11-19. DOI 10.0000/self-test-002.\n"
    "3. 自证三 等. 自证题名三号研究[J]. 自证期刊, 2026, 2(1): 21-29. DOI 10.0000/self-test-003.\n"
    "4. 自证四 等. 自证题名四号研究[J]. 自证期刊, 2026, 2(2): 31-39. DOI 10.0000/self-test-004.\n"
)
# W-22 守恒等式的另一侧：计划条目与卡面 DOI 一一对应（缺一行／多一行都是变异）
TWIN_PLAN = (
    "# 落点计划（自证孪生；写库在组）\n"
    "\n"
    "| 条目键 | DOI（纸张身份／join 键） | 标题 | 候选落点 | 确认结果 | 收藏集 | 父条目 | 回查 | 本次合集 | 附件 |\n"
    "|---|---|---|---|---|---|---|---|---|---|\n"
    "| SELF0001 | 10.0000/self-test-001 | 自证卡一 | SELFCOL | 按推荐位（交互点三确认） | "
    "SELFCOL／self-test-topic |  | 一致 | 已挂：med-lit-review/self-test-topic | 已挂：10.0000_self-test-001.pdf |\n"
    "| SELF0002 | 10.0000/self-test-002 | 自证卡二 | SELFCOL | 按推荐位（交互点三确认） | "
    "SELFCOL／self-test-topic |  | 一致 | 已挂：med-lit-review/self-test-topic | "
    "无 PDF：未取得正文（待接机构后重试） |\n"
    "| SELF0003 | 10.0000/self-test-003 | 自证卡三 | SELFCOL | 按推荐位（交互点三确认） | "
    "SELFCOL／self-test-topic |  | 一致 | 已挂：med-lit-review/self-test-topic | 已挂：10.0000_self-test-003.pdf |\n"
    "| SELF0004 | 10.0000/self-test-004 | 自证卡四 | SELFCOL | 按推荐位（交互点三确认） | "
    "SELFCOL／self-test-topic |  | 一致 | 已挂：med-lit-review/self-test-topic | 已挂：10.0000_self-test-004.pdf |\n"
)
def twin_graph_data() -> dict:
    """孪生图数据（自证用）：4 个主节点（4 主题各 1）＋ 1 个邻域点 ＋ 1 条走廊带。

    由本执行体自己算（门禁不借渲染器实现自证）：x＝圆心＋a·ρ·cosθ、y＝圆心＋b·ρ·sinθ；半径按
    `r ∝ c^(1/3)`；张角按 `Σ 点数×单点方格`。四个扇区中心落在四个正方向，故全墨水包围盒按定义铺满。
    """
    counts = (("4.1", 1, 1), ("4.2", 1, 0), ("4.3", 1, 0), ("4.4", 1, 0))
    weights = [main * GRAPH_CELLS["main"] + neigh * GRAPH_CELLS["neigh"] for _key, main, neigh in counts]
    total = float(sum(weights))
    spans = [2 * math.pi * weight / total for weight in weights]
    nodes, sectors, labels, edges = [], [], [], []
    starts, cursor = [], -math.pi / 4
    for span in spans:
        starts.append(cursor)
        cursor += span
    cites_all, rows = [100, 200, 300, 400], ["1", "2", "3", "4"]   # 行号是字符串（与渲染器同制）
    for index, (key, _main, neigh) in enumerate(counts):
        theta = starts[index] + spans[index] / 2
        rho = 0.70 if index % 2 == 0 else 0.65   # 分落两层子环（决定 1）且贴到带外，供墨水读数判定
        x = round(GRAPH_CENTRE[0] + GRAPH_ELLIPSE[0] * rho * math.cos(theta), 1)
        y = round(GRAPH_CENTRE[1] + GRAPH_ELLIPSE[1] * rho * math.sin(theta), 1)
        radius = round(graph_radius(cites_all[index], cites_all[0], cites_all[-1],
                                    GRAPH_SIZE_RANGE["main"], GRAPH_POWER), 2)
        width, height = round(len(str(rows[index])) * 6.9, 1), 13.0
        if math.hypot(width / 2, height / 2) + 0.5 <= radius:
            num_x, mode = x, "inside"
        else:
            num_x, mode = round(x + radius + 2.0 + height / 2, 1), "right"
        nodes.append({
            "id": f"10.0000/self-test-00{rows[index]}", "doi": f"10.0000/self-test-00{rows[index]}",
            "main": True, "evidence_row": rows[index], "card": f"10.0000_self-test-00{rows[index]}",
            "title": f"自证卡{rows[index]}", "year": 2020, "cited_by_count": cites_all[index],
            "cites_missing": False, "in_pool_citations": rows[index], "theme": key,
            "pool_face": "最终名单", "tier": "有全文", "theme_table": None, "theme_conflict": False,
            "shape": "card", "radius": radius, "hit": round(radius + 2.0, 2), "opacity": 1.0,
            "year_band": "≥2020", "rho": rho, "theta": round(theta, 6), "x": x, "y": y,
            "prio": 1 if rows[index] == "1" else None,
            "num": {"text": str(rows[index]), "mode": mode, "x": num_x, "y": round(y + 4.0, 1),
                    "w": width, "h": height,
                    "box": [round(num_x - width / 2, 1), round(y - height / 2, 1),
                            round(num_x + width / 2, 1), round(y + height / 2, 1)]},
        })
        sectors.append({"key": key, "span": round(spans[index], 6), "start": round(starts[index], 6),
                        "weight": round(weights[index], 2), "main": 1, "neigh": neigh,
                        "rho_main": rho, "rho_neigh": rho, "label": key.replace("4.", "主题 ")})
        label_theta = theta
        label_x = GRAPH_CENTRE[0] + GRAPH_ELLIPSE[0] * 0.963 * math.cos(label_theta)
        label_y = GRAPH_CENTRE[1] + GRAPH_ELLIPSE[1] * 0.963 * math.sin(label_theta)
        labels.append({"key": key, "text": f"主题 {key}", "x": round(label_x, 1),
                       "y": round(label_y + 4.3, 1), "w": 60.0, "h": 18.0})
    # 邻域点：挂在 4.1 扇区里，被引数未取到（单列「未取到」档：固定最小号）
    neigh_theta = starts[0] + spans[0] / 2
    neigh_x = round(GRAPH_CENTRE[0] + GRAPH_ELLIPSE[0] * 0.62 * math.cos(neigh_theta), 1)
    neigh_y = round(GRAPH_CENTRE[1] + GRAPH_ELLIPSE[1] * 0.62 * math.sin(neigh_theta), 1)
    nodes.append({
        "id": "10.0000/self-test-neigh", "doi": "10.0000/self-test-neigh", "main": False,
        "evidence_row": None, "card": None, "title": "自证邻域", "year": 2021, "cited_by_count": None,
        "cites_missing": True, "in_pool_citations": 1, "theme": "4.1", "pool_face": "最终名单",
        "tier": "", "theme_table": None, "theme_conflict": False, "shape": "plain",
        "radius": GRAPH_SIZE_RANGE["neigh"][0], "hit": 6.0, "opacity": 1.0, "year_band": "≥2020",
        "rho": 0.62, "theta": round(neigh_theta, 6), "x": neigh_x, "y": neigh_y,
    })
    edges.append({"source": "10.0000/self-test-neigh", "target": "10.0000/self-test-001",
                  "kind": "bwd", "arrow": "source_cites_target", "draw": "line"})
    edges.append({"source": "10.0000/self-test-001", "target": "10.0000/self-test-003",
                  "kind": "bwd", "arrow": "source_cites_target", "draw": "band"})
    corridor = twin_corridor("4.1", "4.3", 1, starts[0] + spans[0] / 2, starts[2] + spans[2] / 2)
    frames = ([graph_box(node) for node in nodes if node["main"]]
              + [graph_label_box(label) for label in labels] + graph_corridor_boxes([corridor]))
    extent = graph_extent(frames)
    frame_main = (f"{extent[0] - FRAME_PAD:.0f} {extent[1] - FRAME_PAD:.0f} "
                  f"{extent[2] - extent[0] + 2 * FRAME_PAD:.0f} {extent[3] - extent[1] + 2 * FRAME_PAD:.0f}")
    ink = graph_extent([graph_box(node) for node in nodes]
                       + [graph_label_box(label) for label in labels] + graph_corridor_boxes([corridor]))
    ratio = ((ink[2] - ink[0]) / GRAPH_USABLE[0], (ink[3] - ink[1]) / GRAPH_USABLE[1])
    return {
        "spec": "self-test", "profile": "full", "degraded": True,
        "degraded_reason": "引文面缺件（自证孪生）",
        "caps": {"nodes": 120, "edges": 250},
        "graph_truncated": {"cap_hit": False, "main_nodes": 4, "kept_nodes": 5, "kept_neighbourhood": 1,
                            "neighbourhood_candidates": 1, "dropped_neighbourhood": 0,
                            "dropped_no_room": 0, "dropped_no_edge": 0, "line_edges_total": 1,
                            "line_edges_kept": 1, "band_edges": 1, "edges_total": 2, "edges_kept": 2,
                            "dropped_edges": 0, "self_loops": 0, "size_shrink": 1.0, "isolated_kept": 0,
                            "band_only_kept": 0, "budget_hit": False},
        "year_bands": [{"label": label, "opacity": opacity, "nodes": 5 if label == "≥2020" else 0}
                       for label, opacity in GRAPH_YEAR_BANDS]
                      + [{"label": GRAPH_UNKNOWN_YEAR, "opacity": 1.0, "nodes": 0}],
        "layer": {"neigh_nodes": 1, "neigh_edges": 1},
        "readings": {"seeds": 0, "edges_collected": 0, "seeds_with_refs": None, "files": [], "notes": [],
                     "ink": {"nodes": list(graph_extent([graph_box(node) for node in nodes])),
                             "all": list(ink), "usable": list(GRAPH_USABLE),
                             "ratio_nodes": [round(ratio[0], 4), round(ratio[1], 4)],
                             "ratio_all": [round(ratio[0], 4), round(ratio[1], 4)]}},
        "layout": {"main_failed": False, "sectors": sectors, "centre": list(GRAPH_CENTRE),
                   "a": GRAPH_ELLIPSE[0], "b": GRAPH_ELLIPSE[1], "corridor_w": CORRIDOR_W,
                   "label_ring_w": 30.0, "node_band_r": NODE_BAND_R,
                   "cells": dict(GRAPH_CELLS_SIDE), "dropped": []},
        "frame": {"main": frame_main, "all": f"0 0 {GRAPH_CANVAS[0]:.0f} {GRAPH_CANVAS[1]:.0f}"},
        "sectors": sectors, "corridors": [corridor], "labels": labels, "nodes": nodes, "edges": edges,
        "encoding": {"size": {"kind": "pow", "power": GRAPH_POWER, "domain": "自证量程",
                              "main": {"min_r": GRAPH_SIZE_RANGE["main"][0],
                                       "max_r": GRAPH_SIZE_RANGE["main"][1],
                                       "lo": cites_all[0], "hi": cites_all[-1]},
                              "neigh": {"min_r": GRAPH_SIZE_RANGE["neigh"][0],
                                        "max_r": GRAPH_SIZE_RANGE["neigh"][1], "lo": None, "hi": None},
                              "missing": "缺被引数单列「未取到」档"},
                     "opacity": "年份档", "stroke": "全文获取档", "shape": "有无文献卡",
                     "edge": "引文方向", "corridor": "跨主题引文按主题对聚成带"},
    }


def twin_corridor(key_a: str, key_b: str, count: int, theta_a: float, theta_b: float) -> dict:
    """孪生走廊带：与渲染器同口径（带宽 ∝ √条数、环厚 88、两端端点＋条数标注）。"""
    width = CORRIDOR_W
    delta = (theta_b - theta_a + math.pi) % (2 * math.pi) - math.pi
    start, end = theta_a, theta_a + delta
    rho_in, rho_out = NODE_BAND_R / GRAPH_ELLIPSE[1], (NODE_BAND_R + width) / GRAPH_ELLIPSE[1]
    centre_rho = (rho_in + rho_out) / 2
    point = lambda rho, theta: (round(GRAPH_CENTRE[0] + GRAPH_ELLIPSE[0] * rho * math.cos(theta), 1),
                                round(GRAPH_CENTRE[1] + GRAPH_ELLIPSE[1] * rho * math.sin(theta), 1))
    path = [point(rho_out, start + delta * index / 24) for index in range(25)]
    path += [point(rho_in, start + delta * index / 24) for index in range(24, -1, -1)]
    return {"a": key_a, "b": key_b, "count": count, "width": round(width, 2),
            "rho_in": round(rho_in, 4), "rho_out": round(rho_out, 4),
            "theta0": round(start, 6), "theta1": round(end, 6),
            "path": " ".join(f"{x:.1f},{y:.1f}" for x, y in path),
            "ends": [{"key": key_a, "x": point(centre_rho, start)[0], "y": point(centre_rho, start)[1]},
                     {"key": key_b, "x": point(centre_rho, end)[0], "y": point(centre_rho, end)[1]}],
            "label": {"x": point(centre_rho, (start + end) / 2)[0],
                      "y": point(centre_rho, (start + end) / 2)[1]}}


def twin_graph_svg(data: dict) -> str:
    """孪生页内的关系图 SVG（最小件）：只出 W-07 要数的装置（行号、命中圈、先读徽牌、走廊带、取景）。"""
    parts = []
    for corridor in data["corridors"]:
        parts.append(f'<polygon class="band" points="{corridor["path"]}"/>')
        for end in corridor["ends"]:
            parts.append(f'<circle class="band-end" cx="{end["x"]:.1f}" cy="{end["y"]:.1f}" r="3.5"/>')
        parts.append(f'<text class="band-text" x="{corridor["label"]["x"]:.1f}" '
                     f'y="{corridor["label"]["y"]:.1f}">{corridor["count"]}</text>')
    for edge in data["edges"]:
        if edge["draw"] != "line":
            continue
        src = next(node for node in data["nodes"] if node["id"] == edge["source"])
        tgt = next(node for node in data["nodes"] if node["id"] == edge["target"])
        parts.append(f'<line class="edge edge-bwd" x1="{src["x"]:.1f}" y1="{src["y"]:.1f}" '
                     f'x2="{tgt["x"]:.1f}" y2="{tgt["y"]:.1f}"/>')
    for node in data["nodes"]:
        if not node["main"]:
            continue
        parts.append(f'<g class="node" tabindex="0" role="button">'
                     f'<circle class="node-hit" cx="{node["x"]:.1f}" cy="{node["y"]:.1f}" '
                     f'r="{node["hit"]:.1f}"/>'
                     f'<circle class="node-dot" cx="{node["x"]:.1f}" cy="{node["y"]:.1f}" '
                     f'r="{node["radius"]:.2f}"/>'
                     f'<text class="node-num" x="{node["num"]["x"]:.1f}" y="{node["num"]["y"]:.1f}" '
                     f'text-anchor="middle">{node["num"]["text"]}</text>'
                     + (f'<circle class="node-prio-dot" cx="{node["x"]:.1f}" cy="{node["y"] - 9:.1f}" r="7.5"/>'
                        f'<text class="node-prio-num" x="{node["x"]:.1f}" y="{node["y"] - 5:.1f}">'
                        f'{node["prio"]}</text>' if node.get("prio") else "") + "</g>")
    for label in data["labels"]:
        parts.append(f'<text class="cluster-text" x="{label["x"]:.1f}" y="{label["y"]:.1f}" '
                     f'text-anchor="middle">{label["text"]}</text>')
    return (f'<svg id="graph-layers" class="graph" viewBox="{data["frame"]["main"]}" '
            f'data-viewbox-all="{data["frame"]["all"]}" data-viewbox-main="{data["frame"]["main"]}" '
            f'role="img" aria-label="自证关系图">' + "".join(parts)
            + '<g data-layer="neigh" aria-hidden="true" class="folded"></g></svg>')


def twin_page(slug: str) -> str:
    """孪生主件页：骨架合规（自足、锚点齐、图数据块与图 SVG 在场），供 W-01…W-08 的孪生对照。"""
    data = twin_graph_data()
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN"><head><meta charset="UTF-8">'
        f"<title>{slug} · 人读主件（自证孪生）</title>"
        "<style>.erow{color:#333}"
        ".node .node-num { font-size: 11px; }"
        ".node-prio-dot { fill: #333; stroke: #fff; }"
        ".band{fill:none}"
        ".legend .kp, .band-end, mark { print-color-adjust: exact; }</style></head>\n<body>\n"
        f"<h1>{slug}</h1>\n"
        '<nav><a href="#s5">证据表</a><a href="#s6">结论</a><a href="#cards">文献卡</a></nav>\n'
        "<main>\n"
        '<section id="s6"><h2>6 结论</h2><p>自证结论（行 1、行 2）。</p></section>\n'
        '<section id="priority"><h2>优先精读序</h2><ol class="prio-list">'
        '<li class="prio-item"><div class="prio-head"><span class="prio-rank">先读 1</span>'
        '<a class="prio-main" href="#row-1" data-scroll-row="1"><span class="prio-title">自证卡一</span>'
        '<span class="prio-meta">文献 1 · 2026</span></a></div>'
        '<a class="btn prio-card" href="#card-10.0000_self-test-001" '
        'data-open-card="10.0000_self-test-001">打开文献卡</a>'
        '<p class="prio-reason">自证理由</p></li></ol></section>\n'
        '<section id="s5"><h2>5 证据表</h2>\n'
        '<article class="erow" id="row-1" data-row="1">'
        '<a data-open-card="10.0000_self-test-001" href="#card-10.0000_self-test-001">打开文献卡</a>'
        "<p>DOI 10.0000/self-test-001</p></article>\n"
        '<article class="erow" id="row-2" data-row="2">'
        '<a data-open-card="10.0000_self-test-002" href="#card-10.0000_self-test-002">打开文献卡</a>'
        "<p>DOI 10.0000/self-test-002</p></article>\n"
        '<article class="erow" id="row-3" data-row="3">'
        '<a data-open-card="10.0000_self-test-003" href="#card-10.0000_self-test-003">打开文献卡</a>'
        "<p>DOI 10.0000/self-test-003</p></article>\n"
        '<article class="erow" id="row-4" data-row="4">'
        '<a data-open-card="10.0000_self-test-004" href="#card-10.0000_self-test-004">打开文献卡</a>'
        "<p>DOI 10.0000/self-test-004</p></article>\n"
        "</section>\n"
        '<section id="cards"><h2>文献卡</h2>\n'
        '<button class="card-chip" data-open-card="10.0000_self-test-001">自证卡一</button>\n'
        + "".join(
            f'<details class="card" id="card-10.0000_self-test-00{n}"><summary>自证卡{cn}</summary>'
            f'<div class="fbody ident"><span class="id-lab">题名</span>'
            f'<span class="id-val">自证卡{cn}</span></div></details>\n'
            + (f'<button class="card-chip" data-open-card="10.0000_self-test-00{n}">自证卡{cn}</button>\n'
               if n > 2 else "")
            for n, cn in ((1, "一"), (2, "二"), (3, "三"), (4, "四")))
        + "</section>\n"
        '<button class="btn" id="graph-fold" type="button" aria-expanded="false" '
        'aria-controls="graph-layers">展开相邻文献</button>\n'
        f'{twin_graph_svg(data)}\n'
        '<div class="degrade" id="graph-degrade">引文面缺件（自证孪生）：图位保留</div>\n'
        "</main>\n"
        f'<script type="application/json" id="graph-data">'
        f"{json.dumps(data, ensure_ascii=False, sort_keys=True)}</script>\n"
        "</body></html>\n"
    )


def page_of(delivery: Path) -> Path:
    """孪生／变异交付的主件路径（目录名 `YYYY-MM-DD-<slug>` → `<slug>.html`）。"""
    return delivery / f"{delivery.name[11:]}.html"


def twin_delivery(root: Path, slug: str = "self-test-topic") -> Path:
    """造一份骨架合规的最小交付（孪生侧）：顶层四项＋`agent/` 直下件＋主件页＋manifest 登记齐。"""
    delivery = root / f"2026-01-01-{slug}"
    (delivery / AGENT_DIR).mkdir(parents=True)
    (delivery / RUN_DIR).mkdir()
    (delivery / INDEX_NAME).write_text(
        "# INDEX（self-test-topic，P0 完整路线）\n"
        "\n"
        "> 机读纯索引：只列本交付的文件与寻址命令。人读入口＝[`self-test-topic.html`](self-test-topic.html)。\n"
        "> 状态计数：候选 3 → 去重后 3 → 交付队列 2 → 证据表行 2 → 写库 1 → 附件 已挂 1\n"
        "\n"
        "## 文件表（一行一件）\n"
        "\n"
        "| 路径 | 内容 | 进版 |\n"
        "|---|---|---|\n"
        "| `self-test-topic.html` | 人读主件 | 是 |\n"
        "| `agent/knowledge-package.md` | 八节知识包；§5 证据表 canonical | 是 |\n"
        "\n"
        "## 寻址（任务 → 切片命令，仓库根执行）\n"
        "\n"
        "| 任务 | 命令 |\n"
        "|---|---|\n"
        "| 结论 | `sed -n '/## 6 结论/,/## 5 证据表/p' workspace/<dir>/agent/knowledge-package.md` |\n"
        "\n"
        "## 待查（本件查不到时）\n"
        "\n"
        "- 定向只读本件；查不到再按 `agent/manifest.json` 的清单指针走。\n",
        encoding="utf-8",
    )
    (delivery / GATE_SUMMARY).write_text(
        "# 门禁摘要（self-test-topic）\n"
        "\n"
        "## Verdict（先读我）\n"
        "\n"
        "- 判定：**过**——三件套 `review_knowledge.py` error=0／`check_growth_loop.py` GREEN／"
        "`check_delivery.py` GREEN。\n"
        "- 未过条目：无。\n"
        "- 缺口去向：`agent/knowledge-package.md` §8 局限声明。\n"
        "\n"
        "## 交付门禁读数\n"
        "\n"
        "| 执行体 | 读数 |\n"
        "|---|---|\n"
        "| `check_delivery.py --card-probe` | RESULT: 样本缺位（无同形态包） |\n",
        encoding="utf-8",
    )
    for name, text in (("knowledge-package.md", TWIN_PACKAGE),
                       ("placement-plan.md", TWIN_PLAN),
                       ("reference-list.md", TWIN_REFERENCE_LIST)):
        (delivery / AGENT_DIR / name).write_text(text, encoding="utf-8")
    # `agent/cards/*.md`：一文件一卡（W-12 解析条数＝件数的前提；文件名即身份）
    (delivery / package_parse.CARDS_REL).mkdir(parents=True, exist_ok=True)
    for stem, text in (
        ("10.0000_self-test-001", "## 提取卡 ST1：自证卡一（全文状态：有全文 PDF p.1–9）\n\n"
                                  "- 标识（identity）：自证｜2026｜自证卡一｜J 期刊｜DOI 10.0000/self-test-001\n"
                                  "- 对应文献 1\n"),
        ("10.0000_self-test-002", "## 提取卡 ST2：自证卡二（全文状态：仅摘要）\n\n"
                                  "- 标识（identity）：自证｜2026｜自证卡二｜J 期刊｜DOI 10.0000/self-test-002\n"
                                  "- 对应文献 2\n"),
        ("10.0000_self-test-003", "## 提取卡 ST3：自证卡三（全文状态：有全文 PDF p.1–4）\n\n"
                                  "- 标识（identity）：自证｜2026｜自证卡三｜J 期刊｜DOI 10.0000/self-test-003\n"
                                  "- 对应文献 3\n"),
        ("10.0000_self-test-004", "## 提取卡 ST4：自证卡四（全文状态：有全文 PDF p.1–6）\n\n"
                                  "- 标识（identity）：自证｜2026｜自证卡四｜J 期刊｜DOI 10.0000/self-test-004\n"
                                  "- 对应文献 4\n"),
    ):
        (delivery / package_parse.CARDS_REL / f"{stem}{package_parse.CARD_SUFFIX}").write_text(
            text, encoding="utf-8")
    (delivery / "agent/bucketing-and-sources.md").write_text("# 四桶报告＋下载队列路由\n", encoding="utf-8")
    page = twin_page(slug)
    with (delivery / f"{slug}.html").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(page)
    (delivery / MANIFEST).write_text(
        json.dumps(
            {
                "topic": {"slug": slug, "delivered": "2026-01-01", "dir": delivery.name},
                "caliber": {"grey_gate_version": "post-0006-fastest"},
                "ledger_pointers": {
                    "download_ledger": "agent/bucketing-and-sources.md",
                    "placement_plan": "agent/placement-plan.md",
                    "gate_summary": "agent/gate-summary.md",
                    "reference_list": "agent/reference-list.md",
                    "regulatory_version_record": "无（本 topic 无监管收录）",
                },
                "versioned_artifacts": [
                    {"path": INDEX_NAME,
                     "sha256": hashlib.sha256((delivery / INDEX_NAME).read_bytes()).hexdigest()},
                    {"path": f"{slug}.html", "sha256": hashlib.sha256(page.encode("utf-8")).hexdigest()},
                ],
                "derived": {"reader_html": {
                    "path": f"{slug}.html",
                    "sha256": hashlib.sha256(page.encode("utf-8")).hexdigest(),
                    "inputs": [{"path": f"{AGENT_DIR}/knowledge-package.md",
                                "sha256": hashlib.sha256(
                                    (delivery / AGENT_DIR / "knowledge-package.md").read_bytes()).hexdigest()}],
                    "command": f"uv run python .claude/skills/med-lit-review/scripts/reader_html.py "
                               f"--delivery workspace/{delivery.name}",
                    "machine_reads": False,
                    # 身份段逐行覆盖读数（W-23 的左侧）：四张卡的身份段全按位合规 → 全数逐行、零回退
                    "counts": {"cards": 4, "ident_rows": 4, "ident_fallback": []}}},
                "regeneration": [
                    {"class": "full_gate", "step": "门禁",
                     "command": "uv run python .claude/skills/med-lit-review/evals/acceptance/run_gate.py "
                                f"--run workspace/{delivery.name} --out run/gate-acceptance --no-live",
                     "artifacts": [{"path": "run/gate-acceptance/assertion-results.json", "sha256": "1" * 64}]},
                ],
            },
            ensure_ascii=False, indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return delivery


def _rewrite(path: Path, transform):
    text = path.read_text(encoding="utf-8")
    new = transform(text)
    path.write_text(new, encoding="utf-8")
    return new != text


def _read_result(delivery: Path, aid: str):
    return next(r for r in evaluate(delivery) if r["id"] == aid)


def run_selftest(out_root: Path):
    """孪生对照自证：每条硬断言先造合规孪生（要求通过），再施加变异（要求未通过）。"""
    work = Path(tempfile.mkdtemp(prefix="delivery-selftest-"))
    scenarios = []
    try:
        delivery = twin_delivery(work)
        scenarios.append({
            "name": "孪生（骨架合规的最小交付）",
            "expect": "三条硬断言在孪生上全部通过",
            "got": "；".join(f"{r['id']}={'通过' if r['ok'] else '未通过'}" for r in evaluate(delivery)),
            "ok": all(r["ok"] for r in evaluate(delivery) if r["gate"] == "硬门禁"),
        })

        def flip(name, aid, expect, mutate):
            mutant = twin_delivery(work, slug=f"mut-{len(scenarios):02d}")
            assert mutate(mutant), f"自证变异未发生：{name}"
            twin_ok = _read_result(delivery, aid)["ok"]
            got = _read_result(mutant, aid)
            scenarios.append({
                "name": name,
                "expect": expect,
                "got": f"孪生={'通过' if twin_ok else '未通过'}／变异={'未通过' if not got['ok'] else '通过'}"
                       f"（{got['observed'][:80]}）",
                "ok": twin_ok and not got["ok"] and not got.get("error"),
            })


        flip("主件缺件（页删）", "W-01",
             "W-01 孪生通过 → 变异未通过",
             lambda d: (page_of(d).unlink() or True))
        flip("外链资源注入（link 元素）", "W-02",
             "W-02 孪生通过 → 变异未通过",
             lambda d: _rewrite(page_of(d), lambda t: t.replace(
                 "</head>", '<link rel="stylesheet" href="https://example.com/a.css"></head>')))
        flip("锚点缺失（行 1 锚改名）", "W-03",
             "W-03 孪生通过 → 变异未通过",
             lambda d: _rewrite(page_of(d), lambda t: t.replace('id="row-1"', 'id="row-1x"')))
        flip("DOI 未在页内出现", "W-04",
             "W-04 孪生通过 → 变异未通过",
             lambda d: _rewrite(page_of(d), lambda t: t.replace(
                 "10.0000/self-test-001", "10.0000/self-test-999")))
        flip("卡缺入口（卡二三入口全去）", "W-05",
             "W-05 孪生通过 → 变异未通过",
             lambda d: _rewrite(page_of(d), lambda t: t.replace(
                 '<a data-open-card="10.0000_self-test-002" href="#card-10.0000_self-test-002">打开文献卡</a>', "")))
        def drop_priority(target):
            return _rewrite(target / AGENT_DIR / "knowledge-package.md",
                            lambda t: re.sub(r"(?ms)^### 5\.1.*?(?=^## )", "", t))

        flip("精读序缺层（§5.1 摘除）", "W-06",
             "W-06 孪生通过 → 变异未通过（缺 §5.1 子段）",
             drop_priority)
        flip("入序重复（行 1 两处）", "W-06",
             "W-06 孪生通过 → 变异未通过（域内未恰一次）",
             lambda d: _rewrite(d / AGENT_DIR / "knowledge-package.md",
                                lambda t: t.replace("| 2 | 行 2 | ST2 |", "| 2 | 行 1 | ST2 |")))
        flip("先读层不足（域 4 篇仅 3 篇入层：档位边界）", "W-06",
             "W-06 孪生通过 → 变异未通过（域内未入序 + 先读层应全入层）",
             lambda d: _rewrite(d / AGENT_DIR / "knowledge-package.md",
                                lambda t: t.replace(
                                    "| 2 | 行 2 | ST2 | 主题补位 | 自证对照：仅摘要行亦可入先读层（行 2） |\n", "")))
        flip("理由行指针悬空（行 9）", "W-06",
             "W-06 孪生通过 → 变异未通过（出处：行指针不落地）",
             lambda d: _rewrite(d / AGENT_DIR / "knowledge-package.md",
                                lambda t: t.replace("入先读层（行 2） |", "入先读层（行 9） |")))

        def stray_priority_row(target):
            """域外篇目入序：加一条无卡行 5（会议摘要·仅题录），把先读层第 2 条指向它。"""
            changed = _rewrite(
                target / AGENT_DIR / "knowledge-package.md",
                lambda t: t.replace(
                    "| 4 | 自证四 ｜ 2026 ｜ J ｜ DOI 10.0000/self-test-004 | 队列研究 · E2 | 人群 | 干预 | 结局 | 无 | 拟落 §4.3 |\n",
                    "| 4 | 自证四 ｜ 2026 ｜ J ｜ DOI 10.0000/self-test-004 | 队列研究 · E2 | 人群 | 干预 | 结局 | 无 | 拟落 §4.3 |\n"
                    "| 5 | 自证五 ｜ 2026 ｜ J ｜ DOI 10.0000/self-test-005 | 会议摘要 · E5 · 仅题录 | 人群 | 干预 | 结局 | 无 | 拟落 §4.3 |\n"
                ).replace("| 2 | 行 2 | ST2 | 主题补位 | 自证对照：仅摘要行亦可入先读层（行 2） |",
                          "| 2 | 行 5 | ST2 | 主题补位 | 自证对照：仅摘要行亦可入先读层（行 5） |"))
            changed |= _rewrite(page_of(target), lambda t: t.replace(
                "</section>\n<section id=\"cards\">",
                '<article class="erow" id="row-5" data-row="5"><p>DOI 10.0000/self-test-005</p></article>\n'
                "</section>\n<section id=\"cards\">"))
            return changed

        flip("域外篇目入序（无卡行 5）", "W-06",
             "W-06 孪生通过 → 变异未通过（域封闭：仅题录行不得入序）",
             stray_priority_row)

        flip("读者面注入机器记号（占位符与锚点记号）", "W-21",
             "W-21 孪生通过 → 变异未通过",
             lambda d: _rewrite(page_of(d), lambda t: t.replace(
                 "</main>", '<p>正文引用见 行 N 与 #row-N，路径 agent/cards/x.md，缺失记 ∅。</p></main>')))
        flip("读者面注入英文机器词（照录容器之外）", "W-21",
             "W-21 孪生通过 → 变异未通过（自写文本零机器词）",
             lambda d: _rewrite(page_of(d), lambda t: t.replace(
                 "</main>", "<p>可及性清单 abstract_source 为空，续跑记录按工具 schema 重建。</p></main>")))

        def mutate_graph(target, mutate):
            """改孪生页里的 `graph-data`：图几何变异都在数据块上做（页内装置照原样留在页上）。"""
            def edit(text):
                m = GRAPH_BLOCK_RE.search(text)
                data = json.loads(m.group(1))
                mutate(data)
                return (text[:m.start(1)] + json.dumps(data, ensure_ascii=False, sort_keys=True)
                        + text[m.end(1):])
            return _rewrite(page_of(target), edit)

        flip("图数据块缺场", "W-07",
             "W-07 孪生通过 → 变异未通过",
             lambda d: _rewrite(page_of(d), lambda t: re.sub(
                 r'<script type="application/json" id="graph-data">.*?</script>\n', "", t, flags=re.S)))
        def inflate_nodes(data):
            """把孪生节点复制到 130 个（唯一 id）：顶穿本执行体的硬兜底 120。"""
            base = list(data["nodes"])
            data["nodes"] = [dict(node, id=f"{node['id']}#{index}")
                             for index, node in enumerate(base * 26)]

        flip("节点数顶穿硬兜底", "W-07",
             "W-07 孪生通过 → 变异未通过（节点 130 ＞ 本执行体常量 120）",
             lambda d: mutate_graph(d, inflate_nodes))
        flip("主-主跨主题边仍逐条出线", "W-07",
             "W-07 孪生通过 → 变异未通过（跨主题引文须聚成带）",
             lambda d: mutate_graph(d, lambda data: [
                 edge.update({"draw": "line"}) for edge in data["edges"] if edge["draw"] == "band"]))
        flip("降级块缺场（引文面不可得）", "W-07",
             "W-07 孪生通过 → 变异未通过",
             lambda d: _rewrite(page_of(d), lambda t: t.replace(
                 '<div class="degrade" id="graph-degrade">引文面缺件（自证孪生）：图位保留</div>', "")))
        flip("主节点缺一（覆盖性）", "W-07",
             "W-07 孪生通过 → 变异未通过（主节点数 ≠ 证据行＋背景卡数）",
             lambda d: mutate_graph(d, lambda data: data["nodes"].pop(
                 next(i for i, n in enumerate(data["nodes"]) if n.get("evidence_row") == "4"))))
        flip("两点相交", "W-07",
             "W-07 孪生通过 → 变异未通过（圆心距 ＜ 半径和）",
             lambda d: mutate_graph(d, lambda data: data["nodes"][1].update(
                 {"x": data["nodes"][0]["x"], "y": data["nodes"][0]["y"]})))
        flip("点越出画布", "W-07",
             "W-07 孪生通过 → 变异未通过（点须在画布内）",
             lambda d: mutate_graph(d, lambda data: data["nodes"][0].update({"x": 1199.0})))
        flip("点径与被引数不符", "W-07",
             "W-07 孪生通过 → 变异未通过（r 须由被引数复算）",
             lambda d: mutate_graph(d, lambda data: data["nodes"][0].update(
                 {"radius": data["nodes"][0]["radius"] + 4.0})))
        flip("行号压住邻点", "W-07",
             "W-07 孪生通过 → 变异未通过（行号框不压邻点）",
             lambda d: mutate_graph(d, lambda data: data["nodes"][0]["num"].update(
                 {"box": [data["nodes"][1]["x"] - 4, data["nodes"][1]["y"] - 4,
                          data["nodes"][1]["x"] + 4, data["nodes"][1]["y"] + 4]})))
        flip("走廊带数不符", "W-07",
             "W-07 孪生通过 → 变异未通过（带数 ＝ 主题对数）",
             lambda d: mutate_graph(d, lambda data: data.update({"corridors": []})))
        flip("命中圈小于可见点", "W-07",
             "W-07 孪生通过 → 变异未通过（命中圈 ≥ 点半径＋2）",
             lambda d: mutate_graph(d, lambda data: data["nodes"][0].update({"hit": 1.0})))
        flip("背景卡带行号", "W-07",
             "W-07 孪生通过 → 变异未通过（背景文献不编号）",
             lambda d: mutate_graph(d, lambda data: data["nodes"].append(
                 dict(data["nodes"][0], id="10.0000/self-test-bg", evidence_row=None, card=None,
                      num=dict(data["nodes"][0]["num"])))))
        flip("先读徽章缺场", "W-07",
             "W-07 孪生通过 → 变异未通过（圆牌数 ＝ 有先读名次的点数）",
             lambda d: mutate_graph(d, lambda data: data["nodes"][0].update({"prio": None})))
        flip("页哈希与登记漂移", "W-08",
             "W-08 孪生通过 → 变异未通过",
             lambda d: _rewrite(page_of(d), lambda t: t.replace("</body>", "<!-- drift --></body>")))

        flip("自由段落注入（INDEX 写观点）", "W-09",
             "W-09 孪生通过 → 变异未通过",
             lambda d: _rewrite(d / INDEX_NAME, lambda t: t + "\n本包结论可靠，建议优先阅读。\n"))
        flip("状态计数行缺失", "W-09",
             "W-09 孪生通过 → 变异未通过",
             lambda d: _rewrite(d / INDEX_NAME, lambda t: t.replace("> 状态计数：", "> 计数（变异）：")))
        flip("verdict 头缺「缺口去向」行", "W-10",
             "W-10 孪生通过 → 变异未通过",
             lambda d: _rewrite(d / GATE_SUMMARY, lambda t: t.replace("- 缺口去向：", "- 遗留：")))
        flip("manifest 缺全量门条目", "W-10",
             "W-10 孪生通过 → 变异未通过",
             lambda d: _rewrite(d / MANIFEST, lambda t: t.replace('"class": "full_gate"', '"class": "run"')))

        def strip_prefix(text):
            return text.replace('"agent/placement-plan.md"', '"placement-plan.md"')

        flip("台账指针去 agent/ 前缀", "W-11",
             "W-11 孪生通过 → 变异未通过",
             lambda d: _rewrite(d / MANIFEST, strip_prefix))
        flip("再生命令被忽略件路径带上 agent/", "W-11",
             "W-11 孪生通过 → 变异未通过",
             lambda d: _rewrite(d / MANIFEST, lambda t: t.replace("run/gate-acceptance/", "agent/gate-acceptance/")))
        flip("台账指针指向不在场的件", "W-11",
             "W-11 孪生通过 → 变异未通过",
             lambda d: _rewrite(
                 d / MANIFEST, lambda t: t.replace("agent/reference-list.md", "agent/not-in-place.md")))
        def strip_card_heads(target, only=None):
            """把卡文件主标题改成普通行（解析不到卡头）：主标题正则与解析模块同源。"""
            changed = False
            for path in sorted((target / package_parse.CARDS_REL).glob(f"*{package_parse.CARD_SUFFIX}")):
                if only is not None and path.name != only:
                    continue
                changed |= _rewrite(
                    path, lambda t: package_parse.CARD_HEAD_RE.sub(lambda m: f"提取卡{m.group(1)}", t, count=1))
            return changed

        flip("卡面 0 张（卡文件皆无主标题）", "W-12",
             "W-12 孪生通过 → 变异未通过（解析条数 0，读数含读取路径与解析条数）",
             strip_card_heads)

        def strip_one_card_head(target):
            return strip_card_heads(target, only=f"10.0000_self-test-001{package_parse.CARD_SUFFIX}")

        flip("卡面条数≠件数（两件中一件无主标题）", "W-12",
             "W-12 孪生通过 → 变异未通过（解析条数 1 ≠ 件数 2）",
             strip_one_card_head)

        def drop_cards_dir(target):
            shutil.rmtree(target / package_parse.CARDS_REL)
            return not (target / package_parse.CARDS_REL).exists()

        flip("卡目录缺", "W-12",
             "W-12 孪生通过 → 变异未通过（目录缺，读数含读取路径与解析条数）",
             drop_cards_dir)

        def drop_plan_row(target):
            """落点计划删一行而卡还在：守恒等式按「有卡无计划行」记多卡。"""
            return _rewrite(
                target / AGENT_DIR / "placement-plan.md",
                lambda t: "".join(line for line in t.splitlines(keepends=True)
                                  if "10.0000/self-test-002" not in line))

        flip("落点计划缺一行（有卡无计划行）", "W-22",
             "W-22 孪生通过 → 变异未通过（多卡计 1）",
             drop_plan_row)
        flip("卡面缺一件（有计划行无卡）", "W-22",
             "W-22 孪生通过 → 变异未通过（缺卡计 1）",
             lambda d: (d / package_parse.CARDS_REL
                        / f"10.0000_self-test-004{package_parse.CARD_SUFFIX}").unlink() or True)
        flip("自报写库设计跳过却仍带计划表行", "W-22",
             "W-22 孪生通过 → 变异未通过（自报不实即判不过）",
             lambda d: _rewrite(d / AGENT_DIR / "placement-plan.md",
                                lambda t: t.replace("（自证孪生；写库在组）", "（自证孪生；写库设计跳过）")))

        def tamper_ident_counts(target, rows=None, fallback=None):
            """改登记的身份段覆盖读数（模拟渲染器报了别的数）：W-23 的左侧被篡改。"""
            path = target / MANIFEST
            data = read_json(path)
            counts = data["derived"]["reader_html"]["counts"]
            if rows is not None:
                counts["ident_rows"] = rows
            if fallback is not None:
                counts["ident_fallback"] = fallback
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return True

        flip("身份段逐行计数与页面不符（登记 3／页面 4）", "W-23",
             "W-23 孪生通过 → 变异未通过（登记逐行 ≠ 页面块）",
             lambda d: tamper_ident_counts(d, rows=3))

        def reject_compliant_card(target):
            """合规身份段被拒：第 5 位改成非 DOI 标识值（仍五位、年份位成立），并照实披露回退。

            页面上该卡的逐行块一并撤掉——模拟渲染器真回退后的页面，让本条只踩「位形态卡多于逐行张数」。
            """
            ok_ = _rewrite(target / package_parse.CARDS_REL / f"10.0000_self-test-002"
                                                             f"{package_parse.CARD_SUFFIX}",
                           lambda t: t.replace("J 期刊｜DOI 10.0000/self-test-002",
                                               "J 期刊｜PMID 12345678"))
            ok_ &= _rewrite(page_of(target), lambda t: t.replace(
                '<div class="fbody ident"><span class="id-lab">题名</span>'
                '<span class="id-val">自证卡二</span></div>', ""))
            return ok_ and tamper_ident_counts(
                target, rows=3,
                fallback=[{"card": "10.0000_self-test-002",
                           "reason": "身份段不符位约定（作者｜年｜题名｜来源｜DOI｜附注）"}])

        flip("合规身份段被拒却已披露（第 5 位非 DOI 值）", "W-23",
             "W-23 孪生通过 → 变异未通过（位形态卡多于逐行张数）",
             reject_compliant_card)
        flip("先读行题名被截断（砍成简写）", "W-23",
             "W-23 孪生通过 → 变异未通过（先读行题名 ≠ 源题名）",
             lambda d: _rewrite(page_of(d), lambda t: t.replace(
                 '<span class="prio-title">自证卡一</span>', '<span class="prio-title">自证卡一…</span>')))
        flip("§5 文献列自拼简式（题名整段丢失）", "W-24",
             "W-24 孪生通过 → 变异未通过（拆不出年份／期刊位）",
             lambda d: _rewrite(d / AGENT_DIR / "knowledge-package.md",
                                lambda t: t.replace(
                                    "| 1 | 自证一 等. 自证题名一号研究[J]. 自证期刊, 2026, 1(1): 1-9. DOI ",
                                    "| 1 | 自证一，2026，自证期刊，DOI ")))
        flip("§5 文献列主行只剩作者串（题名缺位）", "W-24",
             "W-24 孪生通过 → 变异未通过（主行只剩作者串）",
             lambda d: _rewrite(d / AGENT_DIR / "knowledge-package.md",
                                lambda t: t.replace(
                                    "| 1 | 自证一 等. 自证题名一号研究[J]. 自证期刊, 2026, 1(1): 1-9. DOI ",
                                    "| 1 | 自证一 等[J]. 自证期刊, 2026, 1(1): 1-9. DOI ")))
        writeskip = twin_delivery(work, slug="writeskip-case")
        (writeskip / AGENT_DIR / "placement-plan.md").unlink()
        shutil.rmtree(writeskip / package_parse.CARDS_REL)
        res_writeskip = _read_result(writeskip, "W-22")
        scenarios.append({
            "name": "写库不在组（无落点计划亦无卡面）",
            "expect": "W-22 记「不适用」：写库设计跳过的路线不判守恒",
            "got": f"{'通过' if res_writeskip['ok'] else '未通过'}（{res_writeskip['observed'][:80]}）",
            "ok": res_writeskip["ok"] and "不适用" in res_writeskip["observed"],
        })
        p1 = twin_delivery(work, slug="p1-case")
        (p1 / AGENT_DIR / "knowledge-package.md").unlink()
        (p1 / AGENT_DIR / "scoping-note.md").write_text("# 查新摸底（自证）\n", encoding="utf-8")
        res_p1 = _read_result(p1, "W-06")
        scenarios.append({
            "name": "P1 快线（无八节包）",
            "expect": "W-06 记「不适用」：无卡路线不产该层",
            "got": f"{'通过' if res_p1['ok'] else '未通过'}（{res_p1['observed'][:70]}）",
            "ok": res_p1["ok"] and "不适用" in res_p1["observed"],
        })

        flip("摘要未记抽跑读数", "W-19",
             "W-19 孪生通过 → 变异未通过（读数列缺抽跑读数行）",
             lambda d: _rewrite(d / GATE_SUMMARY, lambda t: t.replace(
                 "| `check_delivery.py --card-probe` | RESULT: 样本缺位（无同形态包） |\n", "")))

        if out_root is not None:
            out_root.mkdir(parents=True, exist_ok=True)
            (out_root / "delivery-selftest.json").write_text(
                json.dumps({"scenarios": scenarios}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            lines = ["# 交付形态检查自证（孪生对照）", "",
                     "口径＝孪生对照：先造骨架合规的最小交付（要求硬断言全过），再逐条施加变异（要求该条未通过）。", "",
                     "| 场景 | 期望 | 实测 | 判定 |", "|---|---|---|---|"]
            for s in scenarios:
                lines.append(f"| {s['name']} | {s['expect']} | {s['got']} | {'通过' if s['ok'] else '未通过'} |")
            (out_root / "delivery-selftest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(f"{'通过' if s['ok'] else '未通过'}｜{s['name']}｜{s['got']}" for s in scenarios))
        if out_root is not None:
            print(f"自证报告：{out_root}（退出码 {0 if all(s['ok'] for s in scenarios) else 1}）")
        return 0 if all(s["ok"] for s in scenarios) else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- 漂移抽跑（§11：只判解析器健康）


def display_path(path: Path) -> str:
    """读数用仓库根相对路径：绝对路径不进摘要（既便于迁移，也避开 G-06 的绝对路径面）。"""
    try:
        return path.resolve().relative_to(REPO).as_posix()
    except ValueError:
        return Path(path).as_posix()


def same_form_sample(root: Path):
    """同形态样本：`root` 下目录名日期最新、且带八节包（`agent/knowledge-package.md`）的交付目录；无＝None。"""
    if not root.is_dir():
        return None
    dated = [
        (path.name, path)
        for path in root.iterdir()
        if path.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}-", path.name)
        and (path / package_parse.PACKAGE_REL).exists()
    ]
    return max(dated, key=lambda item: item[0])[1] if dated else None


def card_probe(root: Path, target: "str | None") -> int:
    """抽跑一条（§11）：用门禁自己的解析器跑最新**同形态**真实包，只判解析器健康。

    判据：解析条数 > 0 且 ＝ `agent/cards/*.md` 件数；0 卡／目录缺即红——不判该包的其他契约
    （旧形态差异不构成失败）。无可抽样本记「样本缺位」：不报错、不阻断（退出码 0）。
    读数一行 `RESULT: …`（含样本路径与判据），逐字进 `agent/gate-summary.md` 的「交付门禁读数」表。
    """
    if target:
        delivery = Path(target)
        if not delivery.is_dir():
            print(f"输入缺失：{target}（抽跑样本目录不在场）", file=sys.stderr)
            return 2
        note = "显式样本"
    else:
        delivery = same_form_sample(root)
        if delivery is None:
            print(f"RESULT: 样本缺位 | {display_path(root)} 下无同形态包（判据：带 {package_parse.PACKAGE_REL} "
                  f"的目录名日期最新者）——不报错、不阻断")
            return 0
        note = "最新同形态"
    try:
        cards_ = package_parse.parse_cards(delivery)
    except package_parse.CardParseError as exc:  # 0 卡／目录缺即红（当次交付门禁红，钻取指本读数）
        print(f"RESULT: RED | {exc} | 样本 {display_path(delivery)}（{note}）")
        return 1
    print(f"RESULT: GREEN | 卡面解析 {len(cards_)} 张＝{package_parse.CARDS_GLOB} 件数（自校验） "
          f"| 样本 {display_path(delivery)}（{note}）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="交付形态契约检查（W 组）")
    ap.add_argument("--delivery", help="交付目录（workspace/YYYY-MM-DD-<slug>）")
    ap.add_argument("--selftest", action="store_true", help="孪生场景自证（沙箱，不碰交付）")
    ap.add_argument("--card-probe", nargs="?", const="", default=None, metavar="<真实包>",
                    help="漂移抽跑：只判解析器健康（缺省样本＝最新同形态包；无样本记「样本缺位」）")
    ap.add_argument("--workspace", default=str(REPO / "workspace"),
                    help="抽跑缺省样本根（默认仓库根 workspace/）")
    ap.add_argument("--out", default=None, help="自证报告输出目录（默认只打印）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()
    if args.selftest:
        return run_selftest(Path(args.out) if args.out else None)
    if args.card_probe is not None:
        return card_probe(Path(args.workspace), args.card_probe or None)
    if not args.delivery:
        print("用法：check_delivery.py --delivery <交付目录> [--json] ｜ check_delivery.py --selftest [--out <目录>]"
              " ｜ check_delivery.py --card-probe [<真实包>]", file=sys.stderr)
        return 2
    delivery = Path(args.delivery)
    if not delivery.is_dir():
        print(f"输入缺失：{delivery}（交付目录不在场）", file=sys.stderr)
        return 2
    return report(evaluate(delivery), as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
