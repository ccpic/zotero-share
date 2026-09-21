#!/usr/bin/env python
"""人读主件渲染器：从版内 markdown 真源确定性渲染单页 HTML（标准库、零网络、零第三方）。

口径（本执行体只实现，不复述）：`references/protocols/reader-html.md`（渲染契约唯一家）。
输入分档与失败分档见该件 §2／§7：必需集＝`agent/knowledge-package.md`／`agent/cards/*.md`／
`agent/reference-list.md`／`agent/manifest.json`，缺件、解析失败、卡面 0 张、行号两处条数不一致
各**退出 2、不产页**；`run/` 数据件缺只让对应通道降级（降级块／∅ 标注），照常产页。

用法：uv run python scripts/reader_html.py --delivery workspace/YYYY-MM-DD-<slug> [--route P0|P1|P2|P3]
      uv run python scripts/reader_html.py --delivery <目录> --check   # 复算比对＋对账，不写盘
退出码：0 = 产页（或 `--check` 一致）；1 = `--check` 不一致（页字节或登记漂移）；2 = 用法或输入／解析错误。
"""
from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import package_parse

REPO = Path(__file__).resolve().parents[4]  # <repo>/.claude/skills/med-lit-review/scripts/reader_html.py
PACKAGE_REL = package_parse.PACKAGE_REL
CARDS_REL = package_parse.CARDS_REL
REFERENCE_LIST_REL = "agent/reference-list.md"
MANIFEST_REL = "agent/manifest.json"
SCOPING_NOTE_REL = "agent/scoping-note.md"
QUEUE_REL = "agent/download-queue.txt"
SELECTION_REL = "run/b1/selection.json"  # 研读选取读数件（ADR-0032；首屏「本次研读」取 selected_total）
LEDGER_REL = "agent/bucketing-and-sources.md"
# 可降级输入（存在即视为本次消费，进页脚指纹与 derived.reader_html.inputs[]）
OPTIONAL_INPUTS = ("run/b1/step1-pools.json", "run/b1/dedupe-report.json", "run/b1/citation-expansion.json",
                   "run/b1/citation-graph.json", "run/b1/cited-counts.json", "run/b1/openalex-enrich.json",
                   "run/b1/high-cited-check.json", "run/step5-accessibility.json", QUEUE_REL, SELECTION_REL)

NODE_CAP = 120  # 硬兜底：可读性预算（画布可用面积 ÷ 单点方格）已是主判据，常量只防病态大包
EDGE_CAP = 250  # 硬兜底：只约束「画出来的线」；走廊带承载的个体引文不占这条预算
# 年份档（ADR-0027）：绝对日历档界（等量分位会把「年份」偷换成「本包最老的四分之一」，跨包不可比）。
# 档间透明度按「填充叠在 --surface-soft 上仍 ≥3.15:1」反解；缺年单列一档而不并入最新档。
YEAR_BANDS = (("≤1999", 1999, 0.85), ("2000–2009", 2009, 0.90), ("2010–2019", 2019, 0.95),
              ("≥2020", None, 1.00))
UNKNOWN_YEAR_BAND = "年份未取到"
# 卡内段的固定读者序（第 7 轮）：前七段默认直出，其余折叠；未登记段保文档序殿后（ADR-0029）。
# 段序不跟各卡的文档序走——仅摘要族把研究要素写成 `##` 小节、排在关系段之后，保文档序会让
# 同一个名字在这张卡直出、在那张卡折叠（读者无法建立预期）。直出集按「段名」判，字段与小节一视同仁。
CARD_READER_ORDER = ("标识", "研究要素", "设计与E级", "人群", "干预/暴露", "结局/效应量", "与本文关系",
                     "偏倚与升降级", "未见/未知", "可引用摘录", "作者自认局限")
CARD_INLINE_FIELDS = CARD_READER_ORDER[:7]  # 含「研究要素」（第 7 轮：重要段不默认折叠）
# 卡内身份段（第 7 轮）：按位置切成四行 ＋ 余段归附注；位置校验（年份位／DOI 位）不成立整段回退原样。
# 位分隔符只在括注外算分隔符（括注里的竖线是字段内容）；DOI 位收 DOI 值与带 `DOI` 记号的两种写法。
IDENT_LABELS = ("题名", "作者", "来源", "DOI")
IDENT_TAIL_LABEL = "其他标识与附注"
IDENT_PAGE_NOTE = "本轮读到"  # 「全文状态」行并入附注后，页码仍须可核
# 字段正文切分（第 10 轮）：字段体现是一整段长句流，读者找不到「纳入标准／排除标准／主要终点」这类分界。
# 判据＝按卡文已有结构切（子块头＋条款），**字不改**（ADR-0029 边界：真源只约束文字，不约束呈现形态）：
# 子块头＝短名（≤16 字、不含顿逗句分号）＋可选括注＋终止符（`：` 或 `——`），且起于从句边界；
# 子块内容按 `；` 分条、≥3 条才分（切太碎是另一种糊）；分条处 `；` 删、句末 `。` 留。
BODY_LABEL_MAX = 16
BODY_LABEL_STOP = "、，。；？！「」“”…"
BODY_LABEL_TERMS = ("：", "——")
BODY_CLAUSE_MIN = 3
BODY_BOUNDARY_CHARS = "。；"  # 子块头只能起于正文开头或紧跟句号／分号之后
# 名首的序号（`1)`／`1）`／`①`／`（1）`）不进子块名：它归条款序，不归标签名
BODY_LABEL_ORDINAL_RE = re.compile(r"^(?:\d{1,2}[)）、.]|[①-⑳]|（\d{1,2}）)")
AUTHOR_NAME_MAX = 32  # 派生的首位作者名超此长度（机构署名一类）改用卡首行兜底
GRAPH_DEGRADE_COMMAND = (
    "uv run python .claude/skills/med-lit-review/scripts/citation_expand.py --delivery <交付目录>"
)
P1_DEGRADE_COMMAND = (
    "uv run python .claude/skills/med-lit-review/scripts/step2_dedupe_sort_bucket.py --delivery <交付目录>"
)
STATUS_VALUES = ("有全文", "仅摘要", "仅题录")  # 读者面状态词（ADR-0028；账与卡头同词）

# 读者面词表（本文件是唯一家；口径见 references/protocols/reader-html.md §3）：机器面记号 → 读者用词。
# 机器面契约记号（`行 N`、`#row-N`、`§x.y`）只出现在 markdown 真源、锚点 id 与内联数据块；
# 读者可见文本一律走本表右列，且不得出现占位符、锚点记号与机器符号（门禁 W-21）。
ROW_LABEL = "文献"  # 证据表的一行（契约记号仍是 `行 N`；页上一律记「文献 N」）
MISSING = "未取到"  # 读数缺失（机器面记 ∅）
ROUTE_WORDS = {"P0": "完整路线", "P1": "查新摸底", "P2": "免写库", "P3": "已有文献写总结"}
CALIBER_WORDS = {"post-0006-fastest": "灰色渠道默认启用", "pre-0006-oa_first": "灰色渠道未启用"}
# 卡内字段名 → 证据行七格词表（ADR-0028「词表唯一家＝执行体」：卡是真源，页上按读者面统一）
CARD_FIELD_WORDS = {
    "标识": "文献",
    "设计与E级": "设计与等级",
    "干预/暴露": "干预与暴露",
    "结局/效应量": "结局与效应量",
    "偏倚与升降级": "偏倚备注",
    "与本文关系": "与结论的关联",
}


def card_field_word(name: str) -> str:
    """卡内字段名映射到七格词表（未登记的段名原样返回）：同一个东西在页上只有一个名字。"""
    return CARD_FIELD_WORDS.get(name.strip(), name)


# 等级记号（第 7 轮）：只认孤立的 E1–E5（前后非字母数字），避免吃掉 `E2E` 一类的编号。
LEVEL_RE = re.compile(r"(?<![A-Za-z0-9])E([1-5])(?![0-9A-Za-z])")


def level_ref(match) -> str:
    code = "E" + match.group(1)
    return f'<span class="lv" data-lv="{code}">{code}</span>'


# 首屏三个数字的 hover 口径（ADR-0032 §8）：三数是一条递进关系——检索候选 → 本次研读 → 证据表，
# 逐枚写清它在链上的位置；「下载队列」「文献卡」两枚数字退场（队列已改为研读选取集本身）。
# 标签与口径按读者面词表落词（ADR-0028／门禁 W-21）：「终池」「研读选取集」「download-queue」等内部词不进页面。
STAT_NOTES = {
    "检索候选": "检索与沿参考文献补检后的候选篇数（去重后、含未取得全文者），是本次研读的来源",
    "本次研读": "从检索候选里选定本次要读的篇数：下载、入库与研读同此一批；未取得全文者按摘要级或题录级读",
    "证据表": "本次研读里写进证据表的篇数（每篇一行）",
}


GRADE_LEGEND = ("等级＝按研究类型先给的证据等级（E1 最高、E5 最低），每行另注本次是否调整等级；"
                "全文获取＝这篇本轮读到哪一层（有全文／仅摘要＝只读到摘要／仅题录＝只读到题录；"
                "未评＝未做偏倚评估）。同一组内多选＝或，跨组同选＝且。")
# 等级解释（第 7 轮）：五档含义 ＋ 话术四句。档位含义只写协议与本交付实测能支撑的——
# E4 不发明先验映射（本次落在 E4 的四篇全部由 E3 降一档而来，逐卡核实过）。
LEVEL_POP_TITLE = "证据等级（E1 最高、E5 最低）"
LEVEL_ROWS = (
    ("E1", "最高档：系统综述与荟萃分析、III 期确证性随机对照试验；指南与监管文件的规范性表述按同档。"),
    ("E2", "IIb 期剂量探索试验；前瞻性队列与登记库研究。"),
    ("E3", "病例对照、回顾性队列等观察性设计。"),
    ("E4", "观察性设计经降档后的落点（本次落在 E4 的文献都由 E3 降一档而来）。"),
    ("E5", "最低档（托底）：叙述综述、动物实验一类无确证性人体证据的文献。"),
)
LEVEL_NOTES = (
    "「先验」是按研究类型先给的档，不是最终档；「初判 → 定级」才是收口后的档。",
    "升降在一档以内：降档理由是过时／异质／发表偏倚；升档只对观察性研究开放，"
    "需大效应、剂量反应与无负向混杂同时成立。",
    "「封顶／托底」＝E1 之上不再升、E5 之下不再降。",
    "「不升降档」＝本次没有可降的理由，不是「未评估」。",
)
# 等级解释的键盘入口（第 7 轮）：每卡一枚，落在「设计与等级」段名旁——卡文里的每个 E 记号都可悬停，
# 但键盘不逐记号给停靠点（一张卡多出 3–4 个 tab 停靠点，卡区累计上百个）。
LEVEL_OPEN_BUTTON = ('<button class="lv-open" type="button" aria-expanded="false" '
                     'aria-controls="lv-pop" aria-describedby="lv-pop">等级说明</button>')
REVIEW_FILES = (PACKAGE_REL, "agent/search-strategy.md", "agent/screening-log.md", LEDGER_REL)
# 同一批文献的另几处切法（先读／关系图／文献卡索引三处共用同一句，口径单一）
SAME_SOURCE_NOTE = "同一批文献的另一种切法；主线在 §5 证据表。"
# 件名链接的目的地提示（链接文案要承诺它到底通向哪里）
DEST_HINT = "（页尾校验信息）"
# 先读行的行解剖（ADR-0031 决定 2）：理由末尾的出处指针指向本行时不在读者面重复显示——编号已进副行、
# 状态词已进「全文获取」；指向别行的保留成链（那不是重复，是跨行引用）。真源形态与 W-06 判据不动。
PRIO_SELF_PTR_RE = re.compile(r"（\s*(?:文献|证据行|行)\s*(\d{1,3})\s*(?:[，,][^（）]*)?）\s*$")
# 层标签句（模板 §5.1 的形态标签）：读者面已由节首说明句与折叠 summary 承担，不再照原渲染。
PRIO_LABEL_PROSE_RE = re.compile(r"^(?:先读层|其后层)")

# 交付件读者名：正文提到内部路径时换成读者能懂的件名，原文路径集中进页尾校验信息
FILE_LABELS = {
    "agent/knowledge-package.md": "知识包正文",
    "agent/search-strategy.md": "检索策略",
    "agent/screening-log.md": "筛选日志",
    "agent/bucketing-and-sources.md": "来源与下载记录",
    "agent/placement-plan.md": "入库计划",
    "agent/reference-list.md": "引文表",
    "agent/gate-summary.md": "质检摘要",
    "agent/manifest.json": "文件清单与校验",
    "agent/doi-list.txt": "DOI 清单",
    "agent/download-queue.txt": "下载队列",
    "agent/scoping-note.md": "查新范围说明",
    "agent/cards/": "文献卡目录",
    "run/b1/citation-expansion.json": "沿参考文献补检记录",
    "run/b1/citation-graph.json": "引用关系数据",
    "run/b1/step1-diagnostics.json": "逐格检索记录",
    "run/b1/step1-pools.json": "检索命中集合",
    "run/b1/coverage-ledger.json": "检索覆盖记录",
    "run/b1/high-cited-check.json": "高被引对账记录",
    "run/b1/gap-fill.json": "补检记录",
    "run/quarantine-record.json": "剔除记录",
    "run/download-results.json": "下载结果",
    "run/fulltext-identity.json": "全文身份核验记录",
    "run/step5-accessibility.json": "全文获取清单",
}
FILE_LABEL_FALLBACKS = (
    (re.compile(r"^run/b1/raw/"), "引文接口原始响应"),
    (re.compile(r"^run/b1/"), "检索记录"),
    (re.compile(r"^run/batches/"), "批次续跑记录"),
    (re.compile(r"^run/fulltext/"), "下载的全文"),
    (re.compile(r"^run/text/"), "提取的文本"),
    (re.compile(r"^run/quarantine/"), "剔除目录"),
    (re.compile(r"^run/b2/"), "监管原文件"),
    (re.compile(r"^run/"), "运行记录"),
    (re.compile(r"^references/protocols/"), "规范说明"),
    (re.compile(r"^references/templates/"), "格式模板"),
    (re.compile(r"^agent/cards/"), "文献卡目录"),
    (re.compile(r"^agent/"), "交付文件"),
)
# 池面（数据块 `pool_face`）→ 读者词：图节点提示里的「来源」一栏用它
POOL_FACE_WORDS = {"检索": "检索命中集合", "引文扩展": "沿参考文献补检",
                   "citation_bwd": "沿参考文献（后向）", "citation_fwd": "被引关系（前向）",
                   "discovery": "检索命中集合"}


def pool_face_label(value: str) -> str:
    """池面 → 读者词（多池并列按 `／` 拆开后逐段映射；未登记段原样）。"""
    return "／".join(POOL_FACE_WORDS.get(part, part) for part in value.split("／")) if value else ""


# 行内代码里出现「命令或件名」＝复核面内容：页内换成指向页尾校验信息的链接
CODE_REF_RE = re.compile(r"^(?:uv run\b|python\b|\S*\.(?:py|json|ya?ml|tsv|txt|xpi|md|csv)\b"
                         r"|(?:agent|run|references|scripts|evals)/)")
PATH_CODE_RE = re.compile(r"(?:agent|run|references|scripts|evals)/[\w./*\-]+")


def file_anchor(label: str) -> str:
    """件名 → 页尾锚点 id（两端各算一次、不靠传参耦合：正文链接与页尾行用同一函数）。"""
    return "file-" + re.sub(r"\W+", "-", label).strip("-")


def file_label(rel: str) -> str:
    """交付件读者名（未登记的件按基名兜底；登记面＝`FILE_LABELS`／`FILE_LABEL_FALLBACKS`）。"""
    if rel in FILE_LABELS:
        return FILE_LABELS[rel]
    for pattern, label in FILE_LABEL_FALLBACKS:
        if pattern.match(rel):
            return label
    return f"{Path(rel).stem} 件"


class RenderError(Exception):
    """程序级失败（必需件缺／解析失败／卡面 0 张／行号两处条数不一致）：退出 2、不产页。"""


# ---------------------------------------------------------------- 基础工具


def sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def sha256_of(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def display_path(path: Path) -> str:
    """读数用仓库根相对路径（相对路径可迁移，也避开 G-06 的绝对路径面）。"""
    try:
        return path.resolve().relative_to(REPO).as_posix()
    except ValueError:
        return Path(path).as_posix()


def esc(text: str) -> str:
    return html_lib.escape(text or "", quote=False)


def attresc(text: str) -> str:
    return html_lib.escape(text or "", quote=True)


def dumps_json(obj) -> str:
    """内联数据块序列化：键序固定、分隔符固定、`<` 转义（防 `</script>` 提前收块）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).replace("<", "\\u003c")


# ---------------------------------------------------------------- 版内必需件解析


def parse_reference_list(text: str) -> dict:
    """引文表解析：`N. <GB/T 7714 著录>… DOI <doi>.` → {行号: DOI}（行号↔DOI 契约，§5 行号一一对应）。

    著录行以句点收尾，`DOI_RE` 会把句点一并吃进捕获（字符类不含 `。` 但含 `.`）；此处显式去尾点。
    """
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(\d+[a-z]?)\s*[.．]\s*(.+)$", line)
        if not m:
            continue
        doi = package_parse.DOI_RE.search(m.group(2))
        if doi:
            out[m.group(1)] = package_parse.norm_doi(doi.group(1)).rstrip(".")
    return out


def parse_evidence_rows(package_md: str) -> dict:
    rows = package_parse.evidence_rows(package_md)
    if not rows:
        raise RenderError(
            f"§5 证据表解析到 0 行：{PACKAGE_REL}（读取路径 {PACKAGE_REL} 的 §5 七列表；解析条数 0）"
        )
    return rows


def make_status_of(access_map):
    """可及性档位取值器：优先 `run/step5-accessibility.json`（按 DOI），退回证据表「设计与等级」列。"""

    def status_of(doi: str, design: str, card_status: str = "") -> str:
        """档位取值：清单 → 证据表「设计与等级」列状态词 → 卡首行「全文状态」→ 未评（缺记 ∅ 不猜）。"""
        if doi and access_map.get(doi) in STATUS_VALUES:
            return access_map[doi]
        for source in (design, card_status):
            for value in STATUS_VALUES:
                if value in (source or ""):
                    return value
        return "未评"

    return status_of


def parse_accessibility(delivery: Path) -> dict:
    """`run/step5-accessibility.json`（可降级件）→ {DOI: 档位}；缺件/解析失败＝空表（通道降级）。"""
    path = delivery / "run/step5-accessibility.json"
    if not path.exists():
        return {}
    try:
        payload = read_json(path)
    except (OSError, ValueError):
        return {}
    out = {}
    for entry in (payload.get("entries") if isinstance(payload, dict) else None) or []:
        if not isinstance(entry, dict):
            continue
        value = str(entry.get("accessibility") or "")
        doi = norm_citation_doi(entry.get("doi"))
        if doi and value in STATUS_VALUES:
            out[doi] = value
    return out


CARD_SECTION_RE = re.compile(r"^#{2,6}\s+(.+?)\s*$")
CARD_NAME_MAX = 16  # 段名上限：更长的段首短语按无名段渲染（读者面：不拿整句当段名）
CARD_NAME_STOP = "、，。；「」“”"


def parse_card_blocks(text: str) -> list:
    """卡体分段：`## 小节` 成段（段名＝小节名），`- 名称（…）：正文` 成字段 ＋ 缩进子项。

    读者面口径（reader-html.md §3）：小节不并进上一条正文；无名段不显示段名；
    标题后、首个字段前的散段不丢。段身份解析（卡号／全文状态）走 `package_parse`。
    """
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if package_parse.CARD_HEAD_RE.match(line)), None)
    body = lines[(start + 1) if start is not None else 0:]
    blocks = []
    for line in body:
        head = CARD_SECTION_RE.match(line.strip())
        if head:
            blocks.append({"kind": "section", "name": head.group(1).strip(), "md": "", "subs": []})
            continue
        if line.startswith("- "):
            blocks.append({"kind": "field", "md": line[2:].strip(), "subs": []})
            continue
        sub = re.match(r"^\s{2,}-?\s*(?:\d+[.)]\s*)?(\S.*)$", line) if line.startswith("  ") else None
        if sub and blocks and blocks[-1]["kind"] == "field":
            marker = re.match(r"^\s*(\d+)[.)]\s+", line)
            text_ = re.sub(r"^\s*-\s+", "", sub.group(1)).strip()
            blocks[-1]["subs"].append(f"{marker.group(1)}. {text_}" if marker else text_)
            continue
        if line.strip():
            if blocks and blocks[-1]["kind"] == "field":
                blocks[-1]["md"] += " " + line.strip()
            else:  # 标题后、首个字段前的散段：自成无名段，不丢
                blocks.append({"kind": "field", "md": line.strip(), "subs": []})
    for block in blocks:
        if block["kind"] == "section":
            block["body"] = ""
            block["paren"] = section_note(block["name"])[1]
            continue
        head, rest = split_label(block["md"])
        m = re.match(r"([^（(：:]+)", head) if head else None
        name = m.group(1).strip() if m else ""
        # 段首字段名（含括注）不重复进正文：`- 标识（identity）：X` → 名「标识」、正文「X」；
        # 无「（…）：」分隔的段无名（渲染为普通段落），不再拿整句当段名
        label_ok = bool(name) and len(name) <= CARD_NAME_MAX and not any(ch in name for ch in CARD_NAME_STOP)
        block["name"] = name if label_ok else ""
        # 括注单独留住：研究要素的括注里带着「对应研究问题」，丢了就是丢内容（第 7 轮）
        paren = re.match(r"[^（(]*[（(](.*)[）)]\s*[：:]?\s*$", head, re.S) if label_ok else None
        block["paren"] = paren.group(1).strip() if paren else ""
        block["body"] = rest if label_ok else block["md"]
    return blocks


def section_note(name: str) -> tuple[str, str]:
    """`## 研究要素（P/I/C/O，按摘要）` → (「研究要素」, 「P/I/C/O，按摘要」)。

    小节名与字段名同规：名字用于排序与直出判定，括注降为段名旁的小注（内容不丢、名字不飘）。
    """
    m = re.match(r"([^（(]+)(?:[（(](.*)[）)])?\s*$", name.strip(), re.S)
    if not m:
        return name.strip(), ""
    return m.group(1).strip(), (m.group(2) or "").strip()


def split_label(md: str) -> tuple[str, str]:
    """`名称（括注）：正文` → (「名称（括注）：」, 「正文」)；没有可认的段首标签就返回 ("", md)。

    括注里还会套括号（`研究要素（人群 Population / …；对应研究问题：…ARNI（sacubitril/valsartan）…）：P＝…`），
    正则的 `[^）)]*` 会在第一个右括号处失配——36/60 张卡的字段名曾因此整段丢失（第 7 轮修）。
    这里按括号深度扫描：深度归零且紧随冒号才算切点。宁可不切，也不要把正文当段名或把正文吃掉。
    """
    depth = 0
    for i, ch in enumerate(md):
        if ch in "（(":
            depth += 1
        elif ch in "）)":
            depth -= 1
            if depth < 0:
                return "", md
        elif ch in "：:" and depth == 0:
            return md[:i + 1], md[i + 1:]
    return "", md


CARD_HEAD_PARTS = re.compile(
    r"^(?P<cid>[^：:（(]*)[：:]\s*(?P<who>[^（(]*?)\s*"
    r"[（(]\s*全文状态[：:]\s*(?P<status>[^）)]*)[）)]\s*$"
)


def parse_card_head(head: str) -> dict:
    """卡主标题（`提取卡` 之后的部分）→ {cid, who, status}；形态容错，缺项记空串。"""
    m = CARD_HEAD_PARTS.match(head.strip())
    if m:
        return {k: m.group(k).strip() for k in ("cid", "who", "status")}
    cid = re.split(r"[：:（(]", head.strip())[0].strip()
    status = ""
    ms = re.search(r"全文状态[：:]\s*([^）)]*)", head)
    if ms:
        status = ms.group(1).strip()
    return {"cid": cid, "who": head.strip(), "status": status}


# ------------------------------------------------------- 卡头与身份段（第 7 轮）
IDENT_SLOT_SEPS = "｜|"  # 位分隔符（全角 `｜` 与半角 `|` 同权，卡模板两可）
IDENT_YEAR_RE = re.compile(r"^[（(]?\s*(?:19|20)\d{2}\b")
# 第 5 位收两种写法：DOI 值本身（`10.<registrant>/…`——卡模板的位约定写的就是「第 5 位 DOI」）
# 与带 `DOI` 记号的写法。判据是「这一位是不是标识值」，不是「有没有写 DOI 三个字母」——
# 只认记号会把照位约定写裸值的卡判成不合格，整段静默回退（ADR-0033）。
IDENT_SLOT_RE = re.compile(r"^(?:DOI\b|10\.\d{4,9}/)", re.I)
ORG_RE = re.compile(r"(Collaborative|Group|Committee|Association|Investigators|Programme)", re.I)
CARD_CID_RE = re.compile(r"^[A-Z]{1,3}\d+[a-z]?\s*[：:]\s*")
CARD_STATUS_NOTE_RE = re.compile(r"\s*[（(]\s*全文状态[：:][^）)]*[）)]")


def split_identity_slots(text: str) -> list:
    """身份段按位切分：位分隔符只在括注外算分隔符。

    括注里的竖线是字段内容、不是位分隔符——`来源` 位常带原文页眉一类的原样摘录
    （`…（原文页眉：Nature Reviews Rheumatology | Volume 20 | April 2024 | 241–251）`），
    全量切分会把这类卡切错位、第 5 位落到「Volume 20」上，整段被判不合格而静默回退。
    """
    parts, buf, depth = [], [], 0
    for ch in text or "":
        if ch in "（(":
            depth += 1
        elif ch in "）)":
            depth = max(0, depth - 1)
        if ch in IDENT_SLOT_SEPS and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf).strip())
    return parts


def parse_identity(text: str) -> dict:
    """卡内身份段 → 位置切分（[0]作者 [1]年份 [2]题名 [3]来源 [4]DOI [5:]附注）。

    位约定（`reading-card-template.md` §2）＝作者｜年｜题名｜来源｜DOI｜附注：年份位以四位数年份
    开头、DOI 位是 DOI 值与带 `DOI` 记号的任一写法；两处校验任一不成立即返回空 dict——调用点
    整段回退原样，不拿猜测的分段误导读者（保真优先）。
    """
    parts = split_identity_slots(text)
    if len(parts) < 5 or not IDENT_YEAR_RE.match(parts[1]) or not IDENT_SLOT_RE.match(parts[4]):
        return {}
    return {"authors": parts[0], "year": parts[1], "title": parts[2], "venue": parts[3],
            "doi": re.sub(r"^DOI\s*", "", parts[4], flags=re.I), "tail": [p for p in parts[5:] if p]}


AUTHOR_INITIALS_RE = re.compile(r"^[A-Z](?:[.\-\u2010-\u2015]?[A-Z])*\.?$")


def is_author_initials(token: str) -> bool:
    """缩写式尾段（`R`／`FR`／`H-H`／`H‑H`）：只由大写字母、点与连字符组成，且很短。

    两处都踩过：`[A-Za-z]{1,3}` 认不出连字符缩写（`Parving H-H` 成了「H-H 等 2001」），
    而放宽到「首字母大写 + 任意字母」又会把姓当缩写（`Jui‑Yi Chen` 成了「Jui‑Yi 等 2025」）。
    """
    return len(token) <= 4 and bool(AUTHOR_INITIALS_RE.match(token))


def author_year(year: str) -> str:
    """身份段年份位 → 卡头用的四位数年份（段里常带「（收稿 …、接受 …）」一类的注，卡头不要）。"""
    return re.sub(r"[（(][^）)]*[）)]", "", year or "").strip(" ,，") or (year or "").strip()


def body_label_at(text: str, start: int):
    """从句边界试读一个子块头 → (名, 括注, 终止符, 终止位置, 名首序号)；读不出返回 None。

    形态＝［序号］短名（1–16 字、不含顿逗句分号引号）＋可选括注（可内套括号）＋终止符（`：` 或 `——`）；
    括注与终止符之间只允许空白——夹了字就不是标签而是普通句子（宁可不切，也不把正文当名字）。
    名首序号（`1)`／`①`／`（1）`）不在名字里出：它归条款序、不归标签名，由调用点记进删除账。
    """
    j = start
    while j < len(text) and text[j] in " \u3000":
        j += 1
    ordinal = BODY_LABEL_ORDINAL_RE.match(text, j)
    prefix = ordinal.group(0) if ordinal else ""
    if ordinal:
        j = ordinal.end()
    name_start = j
    while j < len(text):
        ch = text[j]
        if ch in BODY_LABEL_STOP or ch in "（(：:" or text.startswith(BODY_LABEL_TERMS[1], j):
            break
        if j - name_start >= BODY_LABEL_MAX:
            return None
        j += 1
    name = text[name_start:j].strip()
    if not name:
        return None
    note, k = "", j
    if k < len(text) and text[k] in "（(":
        depth = 0
        while k < len(text):
            if text[k] in "（(":
                depth += 1
            elif text[k] in "）)":
                depth -= 1
                if depth == 0:
                    k += 1
                    break
            k += 1
        else:
            return None
        note = text[j:k]
    gap = k
    while gap < len(text) and text[gap] in " \u3000":
        gap += 1
    for term in BODY_LABEL_TERMS:
        if text.startswith(term, gap):
            return name, note, term, gap + len(term), prefix
    return None


def split_clauses(text: str) -> tuple[list, list]:
    """子块内容按 `；` 分条（深度 0 处）：≥3 条才分——只 1–2 条的保持原句成段，切太碎是另一种糊。

    返回（条款, 被删的分隔标点）：分条时 `；` 退出正文（换行已表达分条），句末 `。` 保留。
    """
    parts, buf, depth, dropped = [], [], 0, []
    for ch in (text or ""):
        if ch in "（(":
            depth += 1
        elif ch in "）)":
            depth = max(0, depth - 1)
        if ch == "；" and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            dropped.append(ch)
        else:
            buf.append(ch)
    parts.append("".join(buf).strip())
    items = [part for part in parts if part]
    if not items:  # 空片（子块头正好在边界上）：不留空段
        return [], []
    if len(items) < BODY_CLAUSE_MIN:
        return [(text or "").strip()], []
    return items, dropped


def split_card_body(body: str) -> dict:
    """字段正文 → 结构：导语段 ＋ 子块（名／括注／条款）＋ 被删的分隔标点。

    子块头起于正文开头或紧跟 `。`／`；` 之后（从句边界）；子块内容＝该头终止符到下一个子块头之间；
    头之前、头之后的零散文字归导语段／条款，一律照 `split_clauses` 处理（同规，不特判）。
    """
    text = body or ""
    heads, i = [], 0
    while i < len(text):
        prev = i - 1
        while prev >= 0 and text[prev] in " \u3000":
            prev -= 1
        if prev < 0 or text[prev] in BODY_BOUNDARY_CHARS:  # 从句边界才试读，避免切进句子中间
            hit = body_label_at(text, i)
            if hit:
                name, note, term, end, prefix = hit
                heads.append({"start": i, "name": name, "note": note, "term": term, "content": end,
                              "prefix": prefix})
                i = end
                continue
        i += 1
    dropped = [ch for head in heads for ch in head["term"] + head["prefix"]]  # 终止符与名首序号按字核销
    blocks = []
    for index, head in enumerate(heads):
        stop = heads[index + 1]["start"] if index + 1 < len(heads) else len(text)
        items, gone = split_clauses(text[head["content"]:stop])
        dropped += gone
        blocks.append({"name": head["name"], "note": head["note"], "items": items})
    lead_items, gone = split_clauses(text[:heads[0]["start"]] if heads else text)
    dropped += gone
    return {"lead": lead_items, "blocks": blocks, "dropped": dropped}


def body_text_of(html_text: str) -> str:
    """渲染片段 → 可见文本（去标签、还原实体）：守恒自检与读数都用它。"""
    return html_lib.unescape(re.sub(r"<[^>]+>", "", html_text or ""))


def plan_pieces(plan: dict) -> str:
    """切分产物按原位序拼回（子块名＋括注＋条款，导语段在最前）：守恒自检①的左侧。"""
    return "".join(list(plan["lead"])
                   + [sub["name"] + sub["note"] + "".join(sub["items"]) for sub in plan["blocks"]])


def normalize_chars(text: str) -> list:
    """守恒比较用字符序列：去空白（换行与缩进是呈现层的事，不是内容）。"""
    return [ch for ch in (text or "") if not ch.isspace()]


def assert_body_conserved(plan: dict, source: str, html_out: str, baseline: str, name: str) -> None:
    """切分守恒自检（两道，任一不过即程序级失败、不产页）：

    ① 结构面：切分产物按原位序拼回 ＝ 源文本核销被删分隔标点——抓切分器丢字／重复；
    ② 页面面：渲染文本 ＝ 读者面基线（同一段文字经 `ctx.inline` 未切分的形态）核销被删标点——
       抓装配丢块（漏渲染一个子块或导语段）。
    两侧都按**多重集**比：删除账按字符记（`；`／`：` 在文中多处出现，按值核销无法还原删的是哪一处，
    故逐位比对会把「删了后面那一个」误判成错位）；`inline` 还会改内部路径等字样、研究要素的
    「对应研究问题」行有既有的前置重排（第 7 轮）。保序由构造保证（片段按原文次序产出并输出）。
    切分器一旦丢条款，读者看到的就不是卡文，故按保真优先退出 2（reader-html.md §6⑤）。
    """
    dropped = list(plan["dropped"])

    def credited(text: str, side: str) -> list:
        chars = sorted(normalize_chars(text))
        for ch in dropped:  # 被删的分隔标点按次从该侧核销
            try:
                chars.remove(ch)
            except ValueError:
                raise RenderError(
                    f"字段「{name}」的删除账不平：{side}侧没有可核销的 `{ch}`（切分器与账不同步）") from None
        return chars

    source_chars, plan_chars = credited(source, "源"), sorted(normalize_chars(plan_pieces(plan)))
    if plan_chars != source_chars:
        missing = "".join(ch for ch in source_chars if ch not in plan_chars)[:12]
        extra = "".join(ch for ch in plan_chars if ch not in source_chars)[:12]
        raise RenderError(
            f"字段「{name}」切分不守恒（产物 {len(plan_chars)} 字 vs 源 {len(source_chars)} 字；"
            f"丢「{missing}」／多「{extra}」）——切分器有 bug，按保真优先不产页")
    base, seen = credited(body_text_of(baseline), "基线"), sorted(normalize_chars(body_text_of(html_out)))
    if seen != base:
        missing = "".join(ch for ch in base if ch not in seen)[:12]
        raise RenderError(
            f"字段「{name}」切分后装配不守恒（渲染 {len(seen)} 字 vs 基线 {len(base)} 字；"
            f"丢「{missing}」）——装配有 bug，按保真优先不产页")


def strip_notes(text: str) -> str:
    """剥括注（含嵌套）：括注里常有 `；`／`、` 一类的分隔符，先剥再切才不把名字切碎。

    `Geusens P（第一作者；通讯地址 …）` 先按 `；` 切会切在括注里，末段落到「P（第一作者」——
    名不是名、还夹着仓内用语（形同把卡片原文塞进卡头）。
    """
    out, depth = [], 0
    for ch in text or "":
        if ch in "（(":
            depth += 1
        elif ch in "）)":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def short_author(authors: str) -> str:
    """身份段作者串 → 首位作者短名（页面上的一行标签，不是引文）。

    先剥括注再切分隔符。缩写式「姓 首字母」（`Haynes R`／`Mc Causland FR`）取末段之前；
    名前姓后（`Jui‑Yi Chen`）取末段；机构署名（协作组／委员会一类）整段照用。派生名过长时
    调用点改走卡首行兜底。
    """
    seg = re.split(r"[、,，;；]", strip_notes(authors))[0].strip(" *　")
    if not seg:
        return ""
    if ORG_RE.search(seg):
        return seg.rstrip("*").strip()
    toks = seg.split()
    if len(toks) > 1 and is_author_initials(toks[-1]):
        return " ".join(toks[:-1])
    return toks[-1]


def strip_card_head_noise(head: str) -> str:
    """卡首行解析失配时的兜底：剥内部卡号前缀与「（全文状态：…）」。

    卡号是机器身份、不进读者面；状态另在徽章与附注里出，不该挤进卡头标题。
    """
    cleaned = CARD_STATUS_NOTE_RE.sub("", CARD_CID_RE.sub("", (head or "").strip())).strip()
    return cleaned or (head or "").strip()


def card_head_texts(card: dict) -> dict:
    """卡头与索引的主数据：题名（身份段第 3 段为主，卡首行为兜底）与「作者 等 年份」一行。"""
    ident = parse_identity(card.get("_ident_body", ""))
    fallback = strip_card_head_noise(card.get("_who", ""))
    who = ""
    if ident:
        name = short_author(ident["authors"])
        year = author_year(ident["year"])
        if name and len(name) <= AUTHOR_NAME_MAX:
            who = f"{name} {year}" if ORG_RE.search(name) else f"{name} 等 {year}"
    return {"title": (ident.get("title") or "").strip() or fallback, "who": who or fallback, "ident": ident}


def identity_readings(cards: dict) -> dict:
    """身份段逐行覆盖读数：逐行张数 ＋ 回退名单（件名与成因）。

    回退本身是保真口径（位约定不成立即整段回退原样），但**不得静默**：张数与名单随读数进
    stdout 与 `derived.reader_html.counts`，交付侧由门禁 W-23 对账——「优化没生效」不再靠肉眼发现。
    """
    rows, fallback = 0, []
    for slug, card in sorted(cards.items()):
        body = card.get("_ident_body", "")
        if parse_identity(body):
            rows += 1
        else:
            fallback.append({"card": slug,
                             "reason": "无身份段（`- 标识（identity）：` 段缺）" if not (body or "").strip()
                                       else "身份段不符位约定（作者｜年｜题名｜来源｜DOI｜附注）"})
    return {"cards": len(cards), "ident_rows": rows, "ident_fallback": fallback}


def body_split_readings(cards: dict) -> dict:
    """字段正文切分读数：子块／条款／最长条款／仍为单段的字段数（只出读数，不阻断）。

    独立于渲染重算一遍（读卡文结构，不读渲染输出）：读数与页面同源、但不是页面的自述。
    """
    fields = subs = clauses = 0
    longest = 0
    single = 0
    for card in cards.values():
        for block in card.get("_blocks") or []:
            # 标识段走身份块逐行渲染（`identity_field_html`），不经切分器——计入会虚报子块数
            if block.get("kind") != "field" or block.get("name") == "标识" or not block.get("body"):
                continue
            fields += 1
            plan = split_card_body(block["body"])
            items = list(plan["lead"]) + [item for sub in plan["blocks"] for item in sub["items"]]
            clauses += len(items)
            subs += len(plan["blocks"])
            longest = max([longest] + [len(item) for item in items])
            if not plan["blocks"] and len(plan["lead"]) <= 1:
                single += 1
    return {"fields": fields, "sub_blocks": subs, "clauses": clauses, "longest_clause": longest,
            "single_paragraph_fields": single}


def identity_body(blocks: list) -> str:
    """卡内身份段的正文（`- 标识（identity）：…` 的「…」）：卡头题名与作者年份的真源。"""
    for block in blocks:
        if block["kind"] == "field" and block["name"] == "标识" and block["body"]:
            return block["body"]
    for block in blocks:  # 段名没剥离干净的卡：第一段即身份段（卡模板约定）
        if block["kind"] == "field" and block["body"]:
            return block["body"]
    return ""


def split_status(status: str) -> tuple[str, str]:
    """卡首行全文状态 → （三值状态词, 页码一类的余项）。三值词表是唯一家（ADR-0028）。"""
    text = (status or "").strip()
    for word in STATUS_VALUES:
        if text.startswith(word):
            return word, text[len(word):].strip(" ··；;，,")
    return "", text


def base_name(name: str) -> str:
    """段名归一（去括注）：排序与直出判定按它，显示时括注另作小注。"""
    return section_note(name)[0] if name else ""


CHIP_TITLE_MAX = 56


def chip_title(title: str) -> str:
    """卡片索引 chip 的题名单行截断：先按标点语义截断（副题前停），否则按词边界截断并补省略号。

    上限 `CHIP_TITLE_MAX`＝56（索引是 40 行的紧凑列表，chip 只作认人的第一眼）；
    **先读行不再用本函数**——那里是「认文献」的主场，题名整条呈现（ADR-0031 窄修订）。
    旧的 `[:48]` 硬切会切在括注中间留下残句（critique 第 6 轮 P2），读者正要用它认文献。
    """
    text = (title or "").strip()
    for sep in ("：", "——", ": ", "?", "？"):
        cut = text.find(sep)
        if 0 < cut <= CHIP_TITLE_MAX:
            return text[:cut] + "…"
    if len(text) <= CHIP_TITLE_MAX:
        return text
    cut = text[:CHIP_TITLE_MAX]
    if " " in cut[CHIP_TITLE_MAX // 2:]:
        cut = cut[:cut.rfind(" ")]
    return cut + "…"


def strip_self_pointer(reason: str, row_no: str) -> str:
    """理由末尾的出处指针：指向本行的不在读者面重复显示（ADR-0031 决定 2）。

    编号与状态词都已在同一行的副行里出现，句尾再挂一次就是本次要治的重复；指向别行的不删。
    """
    m = PRIO_SELF_PTR_RE.search(reason)
    if m and m.group(1) == row_no:
        return reason[:m.start()].rstrip()
    return reason


def level_table_html() -> str:
    """五档含义表（弹层与打印面的同源内容）。"""
    rows = "".join(f'<li><span class="lv-code">{esc(code)}</span> {esc(meaning)}</li>'
                   for code, meaning in LEVEL_ROWS)
    notes = "".join(f"<li>{esc(note)}</li>" for note in LEVEL_NOTES)
    return (f'<h3>{esc(LEVEL_POP_TITLE)}</h3><ul class="lv-rows">{rows}</ul>'
            f'<ol class="lv-notes">{notes}</ol>')


def level_pop_html() -> str:
    """等级解释弹层：全页唯一一份（悬停任一处等级记号或聚焦每卡的「等级说明」出它）。

    单例而非逐记号复制——同一份释义若随 160 多处记号各印一份，页体积与读屏噪声都不可接受。
    """
    return (f'<div class="lv-pop screen-only" id="lv-pop" role="tooltip" hidden>{level_table_html()}</div>')


# ---------------------------------------------------------------- 行内标记与块级渲染


class Ctx:
    """一份渲染上下文：行号集、卡 slug 集、节锚集与主题槽，供行内成链与块级渲染共用。"""

    def __init__(self, rows, cards, section_ids, themes):
        self.rows = rows
        self.cards = cards
        self.section_ids = set(section_ids)
        self.themes = themes
        self.links = {"row": 0, "section": 0, "card": 0, "file": 0}
        self.files = []  # 正文提到过的交付件（页尾复核入口按此清单展开原文路径，按首现序）


    def theme_label(self, key: str) -> str:
        """主题名（§4 子标题去掉序号）：读者面写名字，号由调用点决定给不给（名与号分工）。"""
        for tkey, tlabel in self.themes:
            if tkey == key:
                return re.sub(r"^§\s*\d+(?:\.\d+)?\s*", "", tlabel).strip()
        return ""

    def sec_anchor(self, key: str) -> str:
        """§x.y → 节锚 id（不在场返回空串）：与 sec_ref 同一算法，供自定义链接文案处复用。"""
        target = "s" + key.replace(".", "-")
        return target if target in self.section_ids else ""

    def inline_quiet(self, text: str) -> str:
        """只取读者面文本、不记账的 `inline`：守恒自检的基线用（叠链接账的调用一律走 `inline`）。

        基线必须经同一套行内变换——`inline` 会把内部件路径换成件名／「见校验信息」、把反引号收掉，
        拿原始卡文当基线会把既有变换误判成丢字。链接账与件清单在出口回滚，页面读数不被自检污染。
        """
        saved_links, saved_files, saved_hint = dict(self.links), list(self.files), self._hint_used
        try:
            return self.inline(text)
        finally:
            self.links.clear()
            self.links.update(saved_links)
            self.files[:] = saved_files
            self._hint_used = saved_hint

    def inline(self, text: str) -> str:
        self._hint_used = False  # 同一段只承诺一次目的地（第 6 轮 minor：30 处重复成了噪声）
        t = esc(text)
        # 等级记号先包（第 7 轮）：此刻 t 还是纯文本，包出来的标记不会被后面的替换误伤；
        # 正文、七列与卡文里的 E1–E5 一律成为可悬停的记号，悬停出释义。
        t = LEVEL_RE.sub(level_ref, t)
        slug_re = re.compile(r"`(10\.[0-9A-Za-z._()/-]+)`")
        code_re = re.compile(r"`([^`]+)`")
        bold_re = re.compile(r"\*\*(.+?)\*\*")
        row_re = re.compile(r"(?:文献|证据行|行)\s*(\d{1,3})(?![0-9\u2013\u2014-])")
        sec_re = re.compile(r"§(\d)(?:\.(\d))?")
        path_re = re.compile(r"(?:agent|run|references|scripts|evals)/[\w./*\-]+")
        file_re = re.compile(r"\b[\w-]+\.(?:py|json|ya?ml|tsv|txt|xpi|md|csv)\b(?:\s+--?[\w-]+)*")

        def card_ref(m):
            slug = m.group(1)
            if slug in self.cards:
                self.links["card"] += 1
                return (f'<a class="cardref" href="#card-{attresc(slug)}" '
                        f'data-open-card="{attresc(slug)}">{esc(slug)}</a>')
            return m.group(0)

        def row_ref(m):
            num = m.group(1)
            if num in self.rows:
                self.links["row"] += 1
                return f'<a class="rowref" href="#row-{num}">{ROW_LABEL} {num}</a>'
            return m.group(0)

        def sec_ref(m):
            key = m.group(1) + ("." + m.group(2) if m.group(2) else "")
            target = "s" + key.replace(".", "-")
            if target in self.section_ids:
                self.links["section"] += 1
                return f'<a class="secref" href="#{target}">§{key}</a>'
            return m.group(0)

        def review_link(label: str = "校验信息", dest: bool = False) -> str:
            self.links["file"] += 1
            # dest 的链接落到它点名的那一件上（页尾按同一 label 出锚点）：只写件名不承诺目的地视同误导，
            # 一律落到附录顶部同样算没承诺（第 5 轮 P2）。
            target = "#" + file_anchor(label) if dest else "#review"
            hint = DEST_HINT if (dest and not self._hint_used) else ""
            self._hint_used = self._hint_used or bool(dest)
            return f'<a class="fileref" href="{target}">{label}{hint}</a>'

        def code_ref(m):
            inner = m.group(1)
            if not CODE_REF_RE.match(inner):
                return f"<code>{inner}</code>"
            rel = inner.strip()
            if PATH_CODE_RE.fullmatch(rel):  # 件路径（反引号形态）→ 件名，原文路径进复核入口
                if rel not in self.files:
                    self.files.append(rel)
                return review_link(file_label(rel), dest=True)
            return review_link()

        def path_ref(m):
            rel = m.group(0)
            if rel not in self.files:
                self.files.append(rel)
            return review_link(file_label(rel), dest=True)

        t = slug_re.sub(card_ref, t)
        t = code_re.sub(code_ref, t)
        t = bold_re.sub(r"<strong>\1</strong>", t)
        t = path_re.sub(path_ref, t)
        t = file_re.sub(lambda m: review_link("见校验信息"), t)
        t = row_re.sub(row_ref, t)
        t = sec_re.sub(sec_ref, t)
        return t

    def blocks(self, lines) -> str:
        """块级渲染：`###`/`####` 标题、`- ` 列表、表格、普通段落（内容不裁剪）。"""
        out, i, n = [], 0, len(lines)
        while i < n:
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            if line.startswith("#### "):
                out.append(f'<h5 class="h5">{self.inline(line[5:].strip())}</h5>')
                i += 1
                continue
            if line.startswith("### "):
                title = line[4:].strip()
                sid = sub_anchor(title)
                attr = f' id="{sid}"' if sid else ""
                out.append(f'<h3 class="h3"{attr}>{self.inline(title)}</h3>')
                i += 1
                continue
            if line.startswith("- "):
                items = []
                while i < n and lines[i].startswith("- "):
                    items.append(f"<li>{self.inline(lines[i][2:].strip())}</li>")
                    i += 1
                out.append('<ul class="mdul">' + "".join(items) + "</ul>")
                continue
            if line.strip().startswith("|"):
                table = []
                while i < n and lines[i].strip().startswith("|"):
                    table.append(lines[i])
                    i += 1
                out.append(self.table(table))
                continue
            buf = [line.strip()]
            i += 1
            while i < n and lines[i].strip() and not lines[i].startswith(("- ", "|", "###", "####", "# ")):
                buf.append(lines[i].strip())
                i += 1
            out.append("<p>" + self.inline(" ".join(buf)) + "</p>")
        return "\n".join(out)

    def table(self, lines) -> str:
        rows = []
        for line in lines:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and all(set(c) <= set("-: ") and c for c in cells):
                continue
            rows.append(cells)
        if not rows:
            return ""
        head, body = rows[0], rows[1:]
        th = "".join(f"<th>{self.inline(c)}</th>" for c in head)
        trs = "".join(
            "<tr>" + "".join(f"<td>{self.inline(c)}</td>" for c in cells) + "</tr>" for cells in body
        )
        return (f'<div class="table-wrap"><table class="mdtbl"><thead><tr>{th}</tr></thead>'
                f"<tbody>{trs}</tbody></table></div>")


def sub_anchor(title: str) -> str:
    m = re.match(r"(?:§\s*)?(\d+)\.(\d+)", title)
    return f"s{m.group(1)}-{m.group(2)}" if m else ""


def split_subs(body: str):
    """节内 `###` 子节 → [(子标题, 子正文行)]；无子节＝整段正文。"""
    parts = re.split(r"(?m)^###\s+", body)
    if len(parts) == 1:
        return [("", body.split("\n"))]
    out = [("", parts[0].split("\n"))]
    for part in parts[1:]:
        title, _, rest = part.partition("\n")
        out.append((title.strip(), rest.split("\n")))
    return out


# ---------------------------------------------------------------- 版内真源装配


def theme_catalog(package_md: str):
    """§4 主题目录：按 §4 子节出现序给 (`4.1`, 全名) 与色槽（不写死主题名）。"""
    body = package_parse.find_h2(package_md, "主题式证据综合") or ""
    themes = []
    for title, _lines in split_subs(body)[1:]:
        m = re.match(r"(?:§\s*)?(\d+\.\d+)\s*(.*)", title)
        if m:
            themes.append((m.group(1), f"§{m.group(1)} {m.group(2)}".strip()))
    return themes


def theme_of_row(row: dict, card: dict, themes) -> str:
    """主题归属单值真源＝卡内「落主题节」，无卡行退回证据表关联列的 §4 串（03 §4）。"""
    known = {key for key, _ in themes}
    if card:
        m = re.search(r"落主题节[^\n。]*", card["_text"])
        for key in re.findall(r"§(4\.\d)", m.group(0) if m else ""):
            if key in known:
                return key
    for key in re.findall(r"§(4\.\d)", row.get("关联") or ""):
        if key in known:
            return key
    return ""


def parse_years(delivery: Path) -> dict:
    """发表年份（可降级件）：`run/b1/dedupe-report.json` 的 `final_records[].year`（透明度通道与簇内定序键）。"""
    out = {}
    path = delivery / "run/b1/dedupe-report.json"
    if not path.exists():
        return out
    try:
        payload = read_json(path)
    except (OSError, ValueError):
        return out
    for record in (payload or {}).get("final_records") or []:
        if isinstance(record, dict) and isinstance(record.get("year"), int):
            out.setdefault(norm_citation_doi(record.get("doi")), record["year"])
    return out


def parse_cites(delivery: Path) -> dict:
    """被引数（可降级件）：`cited-counts.json` 的 `counts` ＋ `openalex-enrich.json` 的 `records`。"""
    out = {}
    for name in ("run/b1/cited-counts.json", "run/b1/openalex-enrich.json"):
        path = delivery / name
        if not path.exists():
            continue
        try:
            payload = read_json(path)
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        for key, value in dict_of(payload.get("counts")).items():
            if isinstance(value, int) and value >= 0:
                out.setdefault(norm_citation_doi(key), value)
        for key, record in dict_of(payload.get("records")).items():
            if isinstance(record, dict) and isinstance(record.get("cited_by_count"), int):
                out.setdefault(norm_citation_doi(key), record["cited_by_count"])
    return out


def first_screen_count(delivery: Path, rel: str, key: str) -> str:
    """首屏计数读数（可降级件）→ 页面文字：`run/` 读数件的顶层整数键，未取数记「未取到」。

    ADR-0032 §8 三数＝检索候选（`dedupe-report.final_count`）／本次研读（`selection.selected_total`）／
    证据表（表行数）。前两项任一件缺件、解析失败、顶部非 dict 或该键不是非负整数一律记 ∅——不猜 0
    （「未取到」与「0 篇」是两回事），也不为旧包留旧标签分支。
    """
    path = delivery / rel
    if not path.exists():
        return MISSING
    try:
        payload = read_json(path)
    except (OSError, ValueError):
        return MISSING
    value = payload.get(key) if isinstance(payload, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return MISSING
    return str(value)


def load_delivery(delivery: Path):
    """版内真源装配：必需件齐 → 行／卡／引文表 join；任一处失败抛 `RenderError`（退出 2、不产页）。"""
    package_path = delivery / PACKAGE_REL
    if not package_path.exists():
        raise RenderError(f"必需件缺：{PACKAGE_REL}（八节包；读取路径 {PACKAGE_REL}）")
    package_md = package_path.read_text(encoding="utf-8")
    cards = package_parse.parse_cards(delivery)  # 目录缺／0 卡／件数不符 → CardParseError（含读取路径与条数）
    for card in cards.values():
        card["_text"] = (delivery / card["文件"]).read_text(encoding="utf-8")
        head = parse_card_head(card["全文状态"])
        card["_who"] = head["who"] or card["doi"]
        card["_cid"] = head["cid"] or card["卡号"]
        card["_status"] = head["status"]
        card["_blocks"] = parse_card_blocks(card["_text"])
        card["_ident_body"] = identity_body(card["_blocks"])
        card["_head"] = card_head_texts(card)
    ref_path = delivery / REFERENCE_LIST_REL
    if not ref_path.exists():
        raise RenderError(f"必需件缺：{REFERENCE_LIST_REL}（行号↔DOI join 的版内真源；读取路径 {REFERENCE_LIST_REL}）")
    refs = parse_reference_list(ref_path.read_text(encoding="utf-8"))
    if not refs:
        raise RenderError(f"引文表解析到 0 条：{REFERENCE_LIST_REL}（读取路径 {REFERENCE_LIST_REL}；解析条数 0）")
    rows = parse_evidence_rows(package_md)
    if len(refs) != len(rows):
        raise RenderError(
            f"行号两处条数不一致：{REFERENCE_LIST_REL} 解析 {len(refs)} 条 ≠ §5 证据表 {len(rows)} 行"
            f"（读取路径 {REFERENCE_LIST_REL} 与 {PACKAGE_REL}）"
        )
    missing = sorted(set(rows) - set(refs), key=package_parse.row_sort_key)
    if missing:
        raise RenderError(f"行号缺引文表对应条：{'、'.join(missing)}（读取路径 {REFERENCE_LIST_REL}）")
    return package_md, cards, rows, refs


def parse_p1(delivery: Path):
    """P1 查新页：`agent/scoping-note.md` 必需；四桶构成取下载台账（可降级）。

    四桶计数两形态：四桶报告（表头含「计数」→ 逐桶一行取计数列）与下载台账（十列 → 按桶列逐行计数）。
    """
    path = delivery / SCOPING_NOTE_REL
    if not path.exists():
        raise RenderError(f"必需件缺：{SCOPING_NOTE_REL}（P1 快线的主要结论件；读取路径 {SCOPING_NOTE_REL}）")
    text = path.read_text(encoding="utf-8")
    buckets = {}
    ledger = delivery / LEDGER_REL
    if ledger.exists():
        rows, header = [], []
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if not line.strip().startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if not header:
                header = cells
                continue
            if cells and all(set(cell) <= set("-: ") and cell for cell in cells):
                continue
            rows.append(cells)
        counted = any("计数" in cell for cell in header)
        for cells in rows:
            if len(cells) < 2:
                continue
            if counted and re.fullmatch(r"\d+", cells[1]):
                buckets[re.sub(r"\s*（.*$", "", cells[0]) or "未分桶"] = int(cells[1])
            elif not counted and len(cells) >= 2 and not cells[0].upper().startswith("DOI"):
                buckets[cells[1] or "未分桶"] = buckets.get(cells[1] or "未分桶", 0) + 1
    return text, buckets


# ---------------------------------------------------------------- 图（run/b1 引文面）

RAW_DIR = "run/b1/raw"
EXPANSION_REL = "run/b1/citation-expansion.json"
GRAPH_READINGS_REL = "run/b1/citation-graph.json"
POOLS_REL = "run/b1/step1-pools.json"
DEDUPE_REL = "run/b1/dedupe-report.json"


def norm_citation_doi(text) -> str:
    """引文面 DOI 归一：去 `https://doi.org/` 前缀＋尾标点，小写（与池面/台账口径一致）。"""
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(text or "").strip().lower())
    return package_parse.norm_doi(value)


def last_segment(value) -> str:
    text = str(value or "").rstrip("/")
    return text.rsplit("/", 1)[-1] if text else ""


def dict_of(value) -> dict:
    """可降级件顶部形态守卫：非 dict 一律当 ∅（坏形态降级、不整体崩）。"""
    return value if isinstance(value, dict) else {}


def list_of(value) -> list:
    """可降级件列表形态守卫：非 list 一律当 ∅。"""
    return value if isinstance(value, list) else []


def raw_envelope(path: Path) -> dict:
    """raw 件信封（`channel`／`seed`／`slug`／`requests`）：缺件／解析失败／非 dict 即 {}（通道降级）。"""
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def raw_payload(path: Path, keys=()):
    """raw 原始响应取首请求载荷：缺件／形态不符即 None（读数记 ∅，不猜）。"""
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return None
    requests = data.get("requests") if isinstance(data, dict) else None
    if not isinstance(requests, list) or not requests or not isinstance(requests[0], dict):
        return None
    payload = requests[0].get("payload")
    for key in keys:
        payload = payload.get(key) if isinstance(payload, dict) else None
    return payload if isinstance(payload, (dict, list)) else None


def merge_work(works: dict, work_id: str, row: dict) -> None:
    """同 work id 合并题录：只补缺项、不覆盖已有值（池记录与引文题录各有缺面，互相补）。"""
    existing = works.get(work_id)
    if existing is None:
        works[work_id] = dict(row)
        return
    for key, value in row.items():
        if existing.get(key) in (None, "") and value not in (None, ""):
            existing[key] = value


def work_row(item: dict) -> dict:
    year = item.get("publication_year")
    if not isinstance(year, int):
        published = str(item.get("publication_date") or item.get("publishedDate") or "")
        year = int(published[:4]) if published[:4].isdigit() else None
    return {"doi": norm_citation_doi(item.get("doi") or (item.get("ids") or {}).get("doi")),
            "title": item.get("display_name") or item.get("title"),
            "year": year, "cites": item.get("cited_by_count") or item.get("citationCount")}


def collect_citation(delivery: Path) -> dict:
    """run/b1 引文面（只读、零请求）：题录索引、种子引用表、边集、池面与缺失读数。

    边取自 `raw/citation-*.json`（03 §1：读法件只承载后向面且含截断，唯一完备引用面是 raw）；
    `citation-expansion.json`／`citation-graph.json` 只补读数（种子规模与部分失败），缺件不上抛。
    """
    raw_dir = delivery / RAW_DIR
    files = sorted(raw_dir.glob("citation-*.json")) if raw_dir.is_dir() else []
    out = {"present": bool(files), "files": [f"{RAW_DIR}/{p.name}" for p in files],
           "works": {}, "kinds": {}, "origins": {}, "seeds": {}, "edges": set(),
           "in_pool_citations": {}, "notes": []}
    if not out["present"]:
        out["notes"].append("包内无 run/b1/raw/citation-*.json（引文面未取数）")
        return out
    works, kinds, origins = out["works"], out["kinds"], out["origins"]
    pools_path = delivery / POOLS_REL
    if pools_path.exists():
        try:
            pools = dict_of(read_json(pools_path))
        except (OSError, ValueError):
            pools = {}
            out["notes"].append(f"{POOLS_REL} 解析失败：池面判定记 ∅")
        for pool in list_of(pools.get("pools")):
            if not isinstance(pool, dict):
                continue
            kind = str(pool.get("pool_kind") or "检索")
            for record in list_of(pool.get("results")):
                if not isinstance(record, dict):
                    continue
                doi = norm_citation_doi(record.get("doi"))
                if doi:
                    kinds.setdefault(doi, set()).add(kind)
                work_id = str(record.get("externalId") or "")
                if work_id.startswith("W"):
                    merge_work(works, work_id, {"doi": doi, "title": record.get("title"),
                                                "year": record.get("year") or work_row(record)["year"],
                                                "cites": record.get("citationCount")})
    else:
        out["notes"].append(f"缺 {POOLS_REL}：池面判定记 ∅")
    dedupe_path = delivery / DEDUPE_REL
    if dedupe_path.exists():
        try:
            dedupe = dict_of(read_json(dedupe_path))
        except (OSError, ValueError):
            dedupe = {}
            out["notes"].append(f"{DEDUPE_REL} 解析失败：在池判定记 ∅")
        for record in list_of(dedupe.get("final_records")):
            if isinstance(record, dict) and norm_citation_doi(record.get("doi")):
                origins[norm_citation_doi(record.get("doi"))] = str(record.get("origin") or "discovery")
    for path in files:
        name = path.name
        try:
            consume_raw_file(path, name, out, works)
        except (OSError, ValueError, AttributeError, TypeError, KeyError) as exc:
            out["notes"].append(f"{RAW_DIR}/{name}：形态不符（{type(exc).__name__}），本条记 ∅")
            continue
    for slug, seed in out["seeds"].items():
        for work_id in seed["refs"]:
            target = norm_citation_doi((works.get(work_id) or {}).get("doi"))
            if target and seed["doi"]:
                out["edges"].add((seed["doi"], target, "bwd"))
    # 被池内引用计数（03 §2 排序键）：逐条款目被多少条已知引用表（种子）引用
    cited = {}
    for seed in out["seeds"].values():
        for work_id in seed["refs"]:
            cited[work_id] = cited.get(work_id, 0) + 1
    out["in_pool_citations"] = {
        norm_citation_doi((works.get(work_id) or {}).get("doi")): count
        for work_id, count in cited.items() if norm_citation_doi((works.get(work_id) or {}).get("doi"))
    }
    return out


def consume_raw_file(path: Path, name: str, out: dict, works: dict) -> None:
    """单件 raw 消费（可降级集）：任一步形态不符即抛，由调用方记 note 并继续（通道降级、页照产）。"""
    if name.startswith("citation-openalex-seed-"):
        payload = raw_payload(path)
        if payload is None:
            out["notes"].append(f"{RAW_DIR}/{name}：载荷缺失或形态不符（该种子无引用表）")
            return
        payload = dict_of(payload)
        slug = last_segment(payload.get("id")) or path.stem
        seed_doi = norm_citation_doi(payload.get("doi"))
        refs = [last_segment(item) for item in list_of(payload.get("referenced_works")) if isinstance(item, str)]
        out["seeds"][slug] = {"doi": seed_doi, "refs": [r for r in refs if r]}
        if last_segment(payload.get("id")):
            merge_work(works, last_segment(payload.get("id")),
                       {"doi": seed_doi, "title": payload.get("display_name"),
                        "year": payload.get("publication_year"), "cites": payload.get("cited_by_count")})
    elif name.startswith("citation-openalex-bwd-"):
        for item in list_of(raw_payload(path, ("results",))):
            if not isinstance(item, dict):
                continue
            work_id = last_segment(item.get("id"))
            if work_id:
                merge_work(works, work_id, work_row(item))
    elif name.startswith("citation-openalex-fwd-"):
        seed_doi = norm_citation_doi(raw_envelope(path).get("seed"))
        for item in list_of(raw_payload(path, ("results",))):
            if not isinstance(item, dict):
                continue
            work_id = last_segment(item.get("id"))
            if work_id:
                merge_work(works, work_id, work_row(item))
            citing = norm_citation_doi(item.get("doi") or (item.get("ids") or {}).get("doi"))
            if citing and seed_doi:
                out["edges"].add((citing, seed_doi, "fwd"))
    else:  # epmc 通道：载荷内 referenceList.reference[]／citationList.citation[] 带 DOI 者成边
        data = dict_of(raw_payload(path))
        seed_doi = norm_citation_doi(data.get("doi"))
        for bucket, direction in (("referenceList", "bwd"), ("citationList", "fwd")):
            block = data.get(bucket)
            entries = ((block or {}).get("reference") or (block or {}).get("citation") or []) \
                if isinstance(block, dict) else []
            for item in list_of(entries):
                if not isinstance(item, dict):
                    continue
                other = norm_citation_doi(item.get("doi"))
                if not other or not seed_doi:
                    continue
                out["edges"].add((seed_doi, other, "bwd") if direction == "bwd" else (other, seed_doi, "fwd"))


def citations_readings(delivery: Path) -> dict:
    """部分失败如实标注（03 §7）：`citation-graph.json`／`citation-expansion.json` 的种子读数，缺件记 ∅。"""
    readings = {"seeds_with_refs": None, "seeds_total": None}
    try:
        graph = read_json(delivery / GRAPH_READINGS_REL)
    except (OSError, ValueError):
        graph = None
    if isinstance(graph, dict):
        base = graph.get("base") or {}
        if isinstance(base.get("seeds_with_refs"), int):
            readings["seeds_with_refs"] = base["seeds_with_refs"]
    try:
        expansion = read_json(delivery / EXPANSION_REL)
    except (OSError, ValueError):
        expansion = None
    if isinstance(expansion, dict) and isinstance(expansion.get("seeds"), list):
        readings["seeds_total"] = len(expansion["seeds"])
    return readings


# ---------------------------------------------------------------- 关系图：几何与布局（ADR-0030）

# 画布与环带（决定 2／3）：可用区 1108×808（边距 46）的内切椭圆 a=554／b=404。带序（内→外）＝
# 主带 → 邻域带 → 边走廊 → 主题标签圈；走廊 88uu 与标签圈 30uu 按纵向（b 轴）占用，节点带因此止于
# 404−88−30＝286uu。极坐标先落「按 b 归一」的圆（R＝b·ρ），再经 x=a·ρ·cosθ、y=b·ρ·sinθ 铺到椭圆；
# 环容量与单点方格都在那个圆里算——方格是「最紧方向」的方格，那里量够才是真的够。
CANVAS_W, CANVAS_H = 1200.0, 900.0
CANVAS_MARGIN = 46.0
ELL_A, ELL_B = 554.0, 404.0
CENTRE_X, CENTRE_Y = 600.0, 450.0
CORRIDOR_W = 88.0
LABEL_RING_W = 30.0
NODE_BAND_R = ELL_B - CORRIDOR_W - LABEL_RING_W
SIZE_MAIN = (7.0, 23.0)                # 主节点半径量程 uu（点径 14–46）
SIZE_NEIGH = (4.0, 10.0)               # 邻域半径量程 uu（点径 8–20）
ROW_NUM_PLACE = 14.0                   # 行号占位（字号 11px 是下限，故占位不随量程收缩）
NEIGH_GAP = 6.0
SIZE_SHRINK_STEPS = (1.0, 0.85, 0.7, 0.55, 0.4)  # 超容量时的量程收缩梯（决定 4：先减邻域、再缩量程）


def geometry_of(shrink: float) -> tuple:
    """量程收缩梯上的（面积账方格, 最紧圆心距）：预算与铺开两套数同源反解，预算恒在保守侧。

    面积账方格＝点径上限 ＋ 占位（行号 14uu／邻域 6uu），用于分角与可读性预算；铺开时的最紧圆心距
    由点径反解（两点不相交 ＋ 命中圈余量 4uu／2uu），比方格更紧。跨带另按「主径＋邻径＋2」退让。
    """
    cells = (2 * SIZE_MAIN[1] * shrink + ROW_NUM_PLACE, 2 * SIZE_NEIGH[1] * shrink + NEIGH_GAP)
    spacings = (2 * SIZE_MAIN[1] * shrink + 4.0, 2 * SIZE_NEIGH[1] * shrink + 2.0,
                (SIZE_MAIN[1] + SIZE_NEIGH[1]) * shrink + 2.0)
    return cells, spacings


CELL_MAIN, CELL_NEIGH = geometry_of(1.0)[0]          # 60／26
SPACING_MAIN, SPACING_NEIGH, CROSS_GAP = geometry_of(1.0)[1]   # 50／22／35
CELLS = (CELL_MAIN, CELL_NEIGH)                      # 面积账：分角与可读性预算都用它
SPACINGS = (SPACING_MAIN, SPACING_NEIGH, CROSS_GAP)  # 铺开时的最紧圆心距

ROW_NUM_FONT = 11.0                    # 行号字号下限（决定 7）
ROW_NUM_BOX_H = 13.0
ROW_NUM_CHAR_W = 6.9
LABEL_PAD = 2.0
FRAME_PAD = 48.0
CORRIDOR_STROKE = 0.6                  # 最薄的走廊带也看得见（只 1 条边的带不消失）
LABEL_FONT = 12.0
THEME_LABEL_FALLBACK = "相邻文献（未归主题）"
RING_STEP_FACTOR = math.sqrt(3) / 2.0  # 六方密排的径向步／方格


def pow_ratio(cites, lo, hi, power=1.0 / 3.0):
    """被引数 → [0,1] 幂归一（本包 min/max 域）；`c≤0` 落 0（最小号），故无编码倒挂。"""
    if not isinstance(cites, int):
        return None
    if not isinstance(lo, int) or not isinstance(hi, int) or hi <= lo:
        return 1.0
    if cites <= 0:
        return 0.0
    return min(1.0, max(0.0, (cites ** power - lo ** power) / (hi ** power - lo ** power)))


def node_radius(cites, lo, hi, scale=SIZE_MAIN) -> float:
    """尺寸通道（决定 5／6）：`r ∝ c^(1/3)`，本包 min/max 归一，两层各自一把尺。

    缺被引数⇒最小号（`None`），`c≤0` 也落最小号——`r(0) ≤ r(1) ≤ r(2)` 由此成立（旧实现让
    `c≤0` 与缺值共用兜底 4.0，比 `c=2` 的 3.0 还大，是编码倒挂）。量程退化（全包同值）给满号：
    单值包不该被画成「都最小」。
    """
    r_min, r_max = scale
    ratio = pow_ratio(cites, lo, hi)
    return r_min if ratio is None else r_min + (r_max - r_min) * ratio


def year_band(year) -> tuple:
    """年份 → (档名, 透明度)；纯函数、无随机（确定性口径见 reader-html.md §4）。

    四档等宽日历＋缺年单列档（ADR-0027）。工单 03 原本的 0.55–1.0 线性把 27 个年份压进 0.45 的
    alpha 区间＝每年 0.0167，实测落成 15 个值、相邻档差约 0.03 对比度——那条通道本就不可辨。
    现取值使每档填充叠在关系图底色上仍 ≥3.15:1，档间差 +0.25~+0.29 对比度。
    """
    if not isinstance(year, int):
        return (UNKNOWN_YEAR_BAND, 1.0)
    for label, upper, opacity in YEAR_BANDS:
        if upper is None or year <= upper:
            return (label, opacity)
    return (YEAR_BANDS[-1][0], YEAR_BANDS[-1][2])


def ell_xy(rho: float, theta: float) -> tuple:
    """椭圆映射（决定 2 的可复算式）：x＝圆心x ＋ a·ρ·cosθ、y＝圆心y ＋ b·ρ·sinθ。"""
    return (CENTRE_X + ELL_A * rho * math.cos(theta), CENTRE_Y + ELL_B * rho * math.sin(theta))


def cluster_order(ids, connections, rank) -> list:
    """簇内定序＝纯函数贪心（决定 9）：起点按 `rank`，其后每步选「与已放点相连最多 → rank」。

    `rank` 自带唯一末位（DOI），故无并列歧义；同输入同序、无随机、无局部优化。
    """
    remaining, placed, placed_set = list(ids), [], set()
    while remaining:
        pick = min(remaining, key=lambda ident: (-len(connections.get(ident, frozenset()) & placed_set),
                                                 *rank(ident)))
        remaining.remove(pick)
        placed.append(pick)
        placed_set.add(pick)
    return placed


def ring_capacity(span, spacing, first_ring, r_max) -> tuple:
    """环带计划（环心列表, 每环容量）：环容量＝该环弧长 ÷ 最紧圆心距（同环相邻点的最小可见间距）。

    径向步长＝一个间距（行式密排）：同环相邻距＝间距、相邻环同角距＝间距，故层内任意两点圆心距 ≥ 间距。
    不做六方错位：错位要求相邻环的点数相当，而环容量取整后相邻环常差一倍，错半格反而会把同角距压回
    0.87 个间距（实测 4.3 扇区出现过 43.5uu 的最近距）。间距由点径反解（两点不相交 ＋ 命中圈与行号的
    余量，见 reader-html.md §3.6），比面积账的单点方格更紧，故实际容量略高于预算——预算永远是保守侧。
    """
    radii, caps, radius = [], [], first_ring
    while radius <= r_max + 1e-9:
        radii.append(radius)
        caps.append(int(span * radius / spacing))
        radius += spacing
    return radii, caps


def band_outer(span, spacing, first_ring, r_max, count) -> float:
    """按点数反推环带末环环心（决定 1：环带半径由点数与最小可视间距反推）；装不下返回 0。"""
    radii, caps = ring_capacity(span, spacing, first_ring, r_max)
    total = 0
    for radius, cap in zip(radii, caps):
        total += cap
        if total >= count:
            return radius
    return 0.0


def _ring_share(count, caps) -> list:
    """点数按环由内向外摊（贪心、确定性）：里满外散，带的径向占地因此最小（把空间留给邻域带）。"""
    take, rest = [], count
    for cap in caps:
        use = min(cap, rest)
        take.append(use)
        rest -= use
    return [] if rest else take


def _share_even(count, rings) -> list:
    """降级铺法：点数按环均摊（不按弧长卡位），确定性；只在整个收缩梯走尽后使用。"""
    base, extra = divmod(count, rings)
    return [base + (1 if index < extra else 0) for index in range(rings)]


def place_band(points, span, spacing, first_ring, angle0, overflow=False) -> float:
    """把点按子环由内向外铺进扇区，返回末环环心（空带返回 first_ring）；装不下返回 0。

    `overflow=True`＝降级铺法（按环均摊、不再按弧长卡位）：只在收缩梯走尽后使用，保证病态大包也落图。
    """
    if not points:
        return first_ring
    radii, caps = ring_capacity(span, spacing, first_ring, NODE_BAND_R)
    if not radii:
        return 0.0
    if overflow:
        rings, take = len(radii), _share_even(len(points), len(radii))
    else:
        total, rings = 0, 0
        for cap in caps:
            total += cap
            rings += 1
            if total >= len(points):
                break
        if total < len(points):
            return 0.0
        take = _ring_share(len(points), caps[:rings])
    cursor = 0
    for radius, slots in zip(radii[:rings], take):
        for slot in range(slots):
            node = points[cursor]
            cursor += 1
            rho = radius / ELL_B
            theta = angle0 + span * (slot + 0.5) / slots
            node["rho"] = round(rho, 4)
            node["theta"] = round(theta, 6)
            node["x"] = round(CENTRE_X + ELL_A * rho * math.cos(theta), 1)
            node["y"] = round(CENTRE_Y + ELL_B * rho * math.sin(theta), 1)
    return radii[rings - 1]


def sector_spans(counts, cells) -> dict:
    """扇区张角（决定 1）：按 `Σ 点数×单点方格` 分配——主 3600、邻域 676，取代「3×主＋邻」启发式。"""
    weights = {key: main * cells[0] ** 2 + neigh * cells[1] ** 2
               for key, (main, neigh) in counts.items()}
    total = float(sum(weights.values())) or 1.0
    return {key: 2 * math.pi * weight / total for key, weight in weights.items()}


def sector_rooms(main_counts, quota_counts, cells, spacings) -> dict:
    """各扇区的邻域容量（准入用）：张角先按「主节点 ＋ 该扇区的邻域配额」的面积账分，再问外弧带装得下几个。

    配额＝可读性预算按名次切给本扇区的点数（决定 4）。容量按配额分角来算，因此准入不会自造死角：
    真排布若把某个扇区裁窄，它的外弧带随之外移，裁掉的那几个记进降级读数，不悄悄挤掉别的层。
    """
    spans = sector_spans({key: (main_counts.get(key, 0), quota_counts.get(key, 0))
                          for key in set(main_counts) | set(quota_counts)}, cells)
    rooms = {}
    for key, count in main_counts.items():
        span = spans.get(key, 0.0)
        last = band_outer(span, spacings[0], spacings[0] * RING_STEP_FACTOR / 2, NODE_BAND_R, count)
        radii, caps = ring_capacity(span, spacings[1], last + spacings[2], NODE_BAND_R) if last else ([], [])
        rooms[key] = float(sum(caps))
    return rooms


def layout(nodes, theme_keys, connections, rank, cells=CELLS, spacings=SPACINGS,
           allow_overflow=False) -> dict:
    """扇面布局（决定 1／2／3）：扇区张角按「Σ 点数×单点方格」分配；主带在内、邻域带紧接其外。

    半径按展开态一次定死——折叠只换 viewBox、不重排，故两态主节点坐标逐点相同。主带装不下即整轮
    作废（`main_failed`，调用方收缩量程重来）；邻域带装不下就从名次末位起减点数（决定 10：外弧带
    放不下就减邻域点数，不压缩主节点），被减的点记进 `dropped`。`allow_overflow=True`＝收缩梯走尽后的
    兜底：主带按环均摊落图并置 `crowded`（宁可挨近也不丢点、不崩），事实由调用方进读数。
    """
    clusters = list(theme_keys) + [""]
    groups = {key: {"main": [], "neigh": []} for key in clusters}
    for node in nodes:
        key = node["theme"] if node["theme"] in groups else ""
        groups[key]["main" if node["main"] else "neigh"].append(node)
    counts = {key: (len(group["main"]), len(group["neigh"])) for key, group in groups.items()}
    spans = sector_spans(counts, cells)
    weights = {key: main * cells[0] ** 2 + neigh * cells[1] ** 2 for key, (main, neigh) in counts.items()}
    starts, cursor = {}, -math.pi / 2
    for key in clusters:
        starts[key] = cursor
        cursor += spans[key]
    sectors, dropped, crowded = [], [], False
    for key in clusters:
        group, span, start = groups[key], spans[key], starts[key]
        by_id = {node["id"]: node for node in group["main"]}
        mains = [by_id[ident] for ident in cluster_order(list(by_id), connections, rank)]
        last_main = place_band(mains, span, spacings[0], spacings[0] * RING_STEP_FACTOR / 2, start)
        if mains and not last_main:
            if not allow_overflow:
                return {"main_failed": True}
            last_main = place_band(mains, span, spacings[0], spacings[0] * RING_STEP_FACTOR / 2, start,
                                   overflow=True)
            if not last_main:
                return {"main_failed": True}
            crowded = True
        neigh = sorted(group["neigh"], key=lambda node: rank(node["id"]))
        last_neigh = last_main
        while neigh:
            outer = place_band(neigh, span, spacings[1], last_main + spacings[2], start)
            if outer:
                last_neigh = outer
                break
            dropped.append(neigh.pop())
        # 铺满带：点数少于带容量时（例如邻域受节点上限限制），本扇区最外层环按比例外推至带外沿——
        # 只沿同一角度外移、径向与角向间距只增不减，故「两点不相交／命中圈不互吞」不变。
        placed = list(mains) + list(neigh)
        outer = last_neigh or last_main or 0.0
        if placed and outer and outer < NODE_BAND_R * 0.98:
            factor = NODE_BAND_R * 0.98 / outer
            for node in placed:
                rho = node["rho"] * factor
                node["rho"] = round(rho, 4)
                node["x"] = round(CENTRE_X + ELL_A * rho * math.cos(node["theta"]), 1)
                node["y"] = round(CENTRE_Y + ELL_B * rho * math.sin(node["theta"]), 1)
            last_main = (last_main or 0.0) * factor
            last_neigh = outer * factor
        sectors.append({
            "key": key, "span": round(span, 6), "start": round(start, 6),
            "weight": round(weights[key], 2), "main": len(mains), "neigh": len(neigh),
            "rho_main": round((last_main or 0.0) / ELL_B, 4),
            "rho_neigh": round((last_neigh or 0.0) / ELL_B, 4),
        })
    return {
        "main_failed": False,
        "crowded": crowded,
        "sectors": sectors,
        "centre": [CENTRE_X, CENTRE_Y], "a": ELL_A, "b": ELL_B,
        "corridor_w": CORRIDOR_W, "label_ring_w": LABEL_RING_W, "node_band_r": NODE_BAND_R,
        "cells": {"main": round(cells[0], 2), "neigh": round(cells[1], 2)},
        "spacings": {"main": round(spacings[0], 2), "neigh": round(spacings[1], 2),
                     "cross": round(spacings[2], 2)},
        "dropped": [node["id"] for node in dropped],
    }


def text_box(text: str, font: float, height: float) -> tuple:
    """标签框尺寸：中文按 1em、其余按 0.62em 估宽（确定性；门禁只核框位置，不核字宽算法）。"""
    width = sum(font if ord(char) > 0x2E80 else font * 0.62 for char in text if char != " ")
    return round(max(width, font), 1), height


def box_of(node) -> tuple:
    return (node["x"] - node["radius"], node["y"] - node["radius"],
            node["x"] + node["radius"], node["y"] + node["radius"])


def boxes_hit(one, other) -> bool:
    """两个轴对齐框相交：标签避让与门禁共用同一判据。"""
    return one[0] < other[2] and other[0] < one[2] and one[1] < other[3] and other[1] < one[3]


def in_canvas(box) -> bool:
    return box[0] >= 0.0 and box[1] >= 0.0 and box[2] <= CANVAS_W and box[3] <= CANVAS_H


def frame_of(boxes, pad=FRAME_PAD) -> str:
    """一组框 → viewBox 串（`x y w h`，0 位小数）：折叠态取景＝同一批坐标的纯裁剪。"""
    x0 = min(box[0] for box in boxes) - pad
    y0 = min(box[1] for box in boxes) - pad
    x1 = max(box[2] for box in boxes) + pad
    y1 = max(box[3] for box in boxes) + pad
    return f"{x0:.0f} {y0:.0f} {x1 - x0:.0f} {y1 - y0:.0f}"


def label_box(label) -> tuple:
    return (label["x"] - label["w"] / 2, label["y"] - label["h"], label["x"] + label["w"] / 2, label["y"])


def row_number_place(node, occupied) -> None:
    """行号落位（决定 7）：圆内放得下就放圆内，放不下按「上／右／下／左」固定次序外置避让。

    每个主节点都带号（守「每行一篇」契约）：外置位由单点方格保证放得下，禁「放不下就静默不标」。
    落位同时记下框（`box`）：门禁按框判「不压邻点、行号互不相交」，字宽不靠门禁猜。
    """
    text = str(node["evidence_row"])
    width, height = round(len(text) * ROW_NUM_CHAR_W, 1), ROW_NUM_BOX_H
    node["num"] = {"text": text, "w": width, "h": height}
    if math.hypot(width / 2, height / 2) + 0.5 <= node["radius"]:
        node["num"].update({"mode": "inside", "x": node["x"],
                            "y": round(node["y"] + ROW_NUM_FONT * 0.36, 1),
                            "box": [round(node["x"] - width / 2, 1), round(node["y"] - height / 2, 1),
                                    round(node["x"] + width / 2, 1), round(node["y"] + height / 2, 1)]})
        return
    gap = LABEL_PAD + height / 2
    for mode, dx, dy in (("up", 0.0, -1.0), ("right", 1.0, 0.0), ("down", 0.0, 1.0), ("left", -1.0, 0.0)):
        cx = node["x"] + dx * (node["radius"] + gap)
        cy = node["y"] + dy * (node["radius"] + gap)
        box = (cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2)
        if in_canvas(box) and not any(boxes_hit(box, other) for other in occupied):
            node["num"].update({"mode": mode, "x": round(cx, 1),
                                "y": round(cy + ROW_NUM_FONT * 0.36, 1),
                                "box": [round(box[0], 1), round(box[1], 1),
                                        round(box[2], 1), round(box[3], 1)]})
            occupied.append(box)
            return
    box = (node["x"] - width / 2, node["y"] + node["radius"] + LABEL_PAD,
           node["x"] + width / 2, node["y"] + node["radius"] + LABEL_PAD + height)
    node["num"].update({"mode": "down", "x": round(node["x"], 1),
                        "y": round(node["y"] + node["radius"] + gap + ROW_NUM_FONT * 0.36, 1),
                        "box": [round(box[0], 1), round(box[1], 1), round(box[2], 1), round(box[3], 1)]})
    occupied.append(box)


def build_labels(sectors) -> list:
    """主题标签圈（决定 3）：标签落在最外圈、角度取扇区中心，位置从布局自身推导（不硬编码圆心）。"""
    labels, rho = [], (ELL_B - LABEL_RING_W / 2) / ELL_B
    for sector in sectors:
        if not sector["main"] and not sector["neigh"]:
            continue
        width, height = text_box(sector["label"], LABEL_FONT, 18.0)
        x, y = ell_xy(rho, sector["start"] + sector["span"] / 2)
        x = min(max(x, width / 2 + 2.0), CANVAS_W - width / 2 - 2.0)
        y = min(max(y + LABEL_FONT * 0.36, height), CANVAS_H - 2.0)
        labels.append({"key": sector["key"], "text": sector["label"], "x": round(x, 1),
                       "y": round(y, 1), "w": width, "h": height})
    return labels


def build_corridors(pair_counts, angle_of) -> list:
    """边走廊（决定 8）：跨主题引文边按主题对聚成带，带宽 ∝ √条数（只 1 条的带也不消失）。

    带堆在走廊环内（按条数 desc、并列按主题号），厚度之和恰为环厚，故走廊环被填满；两端各打一枚
    主题色端点、带中标条数。个体引文不再各画一条线（降为数据块里的读数）。
    """
    if not pair_counts:
        return []
    roots = {pair: math.sqrt(count) for pair, count in pair_counts.items()}
    total = sum(roots.values()) or 1.0
    corridors, offset = [], 0.0
    for key_a, key_b in sorted(pair_counts, key=lambda pair: (-pair_counts[pair], pair[0], pair[1])):
        width = CORRIDOR_W * roots[(key_a, key_b)] / total
        start = angle_of(key_a)
        delta = (angle_of(key_b) - start + math.pi) % (2 * math.pi) - math.pi
        end = start + delta
        rho_in, rho_out = (NODE_BAND_R + offset) / ELL_B, (NODE_BAND_R + offset + width) / ELL_B
        centre_rho = (rho_in + rho_out) / 2
        path = [ell_xy(rho_out, start + delta * index / 24) for index in range(25)]
        path += [ell_xy(rho_in, start + delta * index / 24) for index in range(24, -1, -1)]
        corridors.append({
            "a": key_a, "b": key_b, "count": pair_counts[(key_a, key_b)], "width": round(width, 2),
            "rho_in": round(rho_in, 4), "rho_out": round(rho_out, 4),
            "theta0": round(start, 6), "theta1": round(end, 6),
            "path": " ".join(f"{x:.1f},{y:.1f}" for x, y in path),
            "ends": [{"key": key_a, "x": round(ell_xy(centre_rho, start)[0], 1),
                      "y": round(ell_xy(centre_rho, start)[1], 1)},
                     {"key": key_b, "x": round(ell_xy(centre_rho, end)[0], 1),
                      "y": round(ell_xy(centre_rho, end)[1], 1)}],
            "label": {"x": round(ell_xy(centre_rho, (start + end) / 2)[0], 1),
                      "y": round(ell_xy(centre_rho, (start + end) / 2)[1], 1)},
        })
        offset += width
    return corridors


def corridor_boxes(corridors) -> list:
    """走廊带在画布上的包围盒（采样路径的极值；供取景、墨水量与门禁复算共用）。"""
    boxes = []
    for corridor in corridors:
        pairs = [item.split(",") for item in corridor["path"].split()]
        xs = [float(item[0]) for item in pairs]
        ys = [float(item[1]) for item in pairs]
        boxes.append((min(xs), min(ys), max(xs), max(ys)))
    return boxes


def extent(boxes) -> tuple:
    """一组框的并集包围盒（左/上取 min、右/下取 max）：墨水量与取景共用。"""
    return (min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes))


def graph_data_block(delivery: Path, rows, row_dois, cards, row_cards, themes, cites_map, status_of,
                     prio_rank=None) -> dict:
    """关系图数据块（ADR-0030）：节点域分层、编码与量程、扇面坐标、走廊带、边分类、截断与降级读数。

    年份通道取 `dedupe-report.final_records[].year`（主节点与邻域都可得，缺记 ∅→透明度 1.0）；
    主题冲突读数（03 §4）：fill 取卡值，表列值另记 `theme_table` 并置 `theme_conflict`（hover 并列）。
    """
    citation = collect_citation(delivery)
    readings = citations_readings(delivery)
    years_map = parse_years(delivery)
    theme_keys = [key for key, _label in themes]
    theme_index = {key: index for index, key in enumerate(theme_keys)}
    theme_label = {key: label for key, label in themes}
    # 卡归属：优先「卡内声明行」等于本行者，未命中再按行序回落（03｜文件名为身份、声明行为归属）
    row_card, used_slugs = {}, set()
    for require_declared in (True, False):
        for row_no in sorted(row_cards, key=package_parse.row_sort_key):
            if row_no in row_card:
                continue
            for cid in row_cards.get(row_no) or []:
                card = cards.get(cid)
                if not card or card["slug"] in used_slugs:
                    continue
                if require_declared and card["行"] and row_no not in card["行"]:
                    continue
                row_card[row_no] = card["slug"]
                used_slugs.add(card["slug"])
                break
    used_ids = set()

    def unique_id(base: str, row_no: str) -> str:
        ident = base if base and base not in used_ids else f"{base or 'row'}#{row_no}"
        used_ids.add(ident)
        return ident

    mains = []
    for row_no, row in sorted(rows.items(), key=lambda kv: package_parse.row_sort_key(kv[0])):
        doi = row_dois.get(row_no, "")
        card = cards.get(row_cards.get(row_no, [""])[0]) if row_cards.get(row_no) else None
        theme_card = theme_of_row(row, card, themes)
        theme_table = next((key for key in re.findall(r"§(4\.\d)", row.get("关联") or "")
                            if key in theme_keys), "")
        mains.append({
            "id": unique_id(doi, row_no), "doi": doi, "main": True, "evidence_row": row_no,
            "card": row_card.get(row_no),
            # 读屏名/悬停名给短名单（契约 §3.6）：题名优先取自卡内身份段，卡不在场才退回整条引文
            "title": (card["_head"]["title"] if card else "") or row.get("文献") or "",
            "citation": row.get("文献") or "", "year": years_map.get(doi),
            "cited_by_count": cites_map.get(doi), "in_pool_citations": citation["in_pool_citations"].get(doi),
            "theme": theme_card, "pool_face": "最终名单",
            "tier": status_of(doi, row.get("设计"), card["_status"] if card else ""),
            "theme_table": theme_table or None,
            "theme_conflict": bool(theme_table and theme_card and theme_table != theme_card),
        })
    for cid, card in sorted(cards.items(), key=lambda kv: kv[1]["slug"]):
        if card["slug"] in used_slugs:
            continue
        doi = card["doi"]
        mains.append({
            "id": unique_id(doi, card["slug"]), "doi": doi, "main": True, "evidence_row": None,
            "card": card["slug"], "title": card["_head"]["title"], "citation": "",
            "year": years_map.get(doi), "cited_by_count": cites_map.get(doi),
            "in_pool_citations": citation["in_pool_citations"].get(doi),
            "theme": theme_of_row({}, card, themes), "pool_face": "最终名单",
            "tier": status_of(doi, "", card["_status"]),
            "theme_table": None, "theme_conflict": False,
        })
    # 边：出图前先剔自环（决定 13；源侧剔除归 search-coverage 线，图侧只保证不画 0 长度线）
    all_edges = {(src, tgt, kind) for src, tgt, kind in citation["edges"]}
    edges_raw = sorted({(src, tgt, kind) for src, tgt, kind in all_edges if src != tgt})
    self_loops = len(all_edges) - len(edges_raw)
    main_ids = {node["id"] for node in mains}
    node_theme = {node["id"]: node["theme"] for node in mains}
    connections = {}
    for src, tgt, _kind in edges_raw:  # 簇内定序只吃主-主边，故不受邻域准入影响
        if src in main_ids and tgt in main_ids:
            connections.setdefault(src, set()).add(tgt)
            connections.setdefault(tgt, set()).add(src)
    adjacency = {}
    for src, tgt, _kind in edges_raw:
        if src in main_ids and tgt not in main_ids:
            adjacency.setdefault(tgt, set()).add(src)
        if tgt in main_ids and src not in main_ids:
            adjacency.setdefault(src, set()).add(tgt)
    neigh_theme = {}
    for doi in adjacency:
        found = sorted({node_theme[item] for item in adjacency[doi] if node_theme.get(item)},
                       key=lambda key: (theme_index.get(key, 99), key))
        neigh_theme[doi] = found[0] if found else ""

    def rank(ident: str) -> tuple:
        cited = cites_map.get(ident)
        return (-(citation["in_pool_citations"].get(ident, 0)),
                -(cited if isinstance(cited, int) else -1), ident)

    def theme_of(ident: str) -> str:
        return node_theme.get(ident, neigh_theme.get(ident, ""))

    # 邻域候选＝有边者：至少有一条连到主节点、且与本点同主题（只有同主题边才画得出线；跨主题边
    # 聚成走廊带，不产生「有候选却无落点」的假点）。
    candidates = sorted((doi for doi in adjacency if doi in citation["kinds"]
                         and any(node_theme.get(item) == neigh_theme[doi] for item in adjacency[doi])),
                        key=rank)
    records = {}
    for doi in candidates:
        work = next((item for item in citation["works"].values() if item.get("doi") == doi), {})
        kinds = citation["kinds"].get(doi) or set()
        records[doi] = {
            "id": unique_id(doi, doi), "doi": doi, "main": False, "evidence_row": None, "card": None,
            "title": work.get("title") or "", "year": work.get("year"),
            "cited_by_count": cites_map.get(doi) if cites_map.get(doi) is not None else work.get("cites"),
            "in_pool_citations": citation["in_pool_citations"].get(doi),
            "theme": neigh_theme[doi],
            "pool_face": "最终名单" if doi in citation["origins"] else ("／".join(sorted(kinds)) or ""),
            "tier": status_of(doi, ""), "theme_table": None, "theme_conflict": False,
        }
    main_counts = {}
    for node in mains:
        key = node["theme"] if node["theme"] in theme_index else ""
        main_counts[key] = main_counts.get(key, 0) + 1
    candidate_counts = {}
    for doi in candidates:
        candidate_counts[neigh_theme[doi]] = candidate_counts.get(neigh_theme[doi], 0) + 1
    # 容纳梯（决定 4／10）：先按满量程试排；主带装不下就收缩直径量程重排（主节点永不截断）。
    shrink, planned, trial, no_room = 1.0, None, list(mains), []
    for shrink in SIZE_SHRINK_STEPS:  # 超容量梯（决定 4）：先减邻域、再缩量程；主节点永不截断
        cells, spacings = geometry_of(shrink)
        # 可读性预算（决定 4）：节点带面积 ÷ 单点方格 − 主节点已占 ⇒ 邻域点数上限
        budget = int(max(0.0, math.pi * NODE_BAND_R ** 2
                         - sum(main_counts.values()) * cells[0] ** 2) // (cells[1] ** 2))
        quota = {}
        for doi in candidates[:budget]:
            quota[neigh_theme[doi]] = quota.get(neigh_theme[doi], 0) + 1
        rooms = sector_rooms(main_counts, quota, cells, spacings)
        admitted, no_room = [], list(candidates[budget:])
        for doi in candidates[:budget]:
            key = neigh_theme[doi]
            if rooms.get(key, 0.0) >= 1.0:
                rooms[key] -= 1.0
                admitted.append(doi)
            else:
                no_room.append(doi)
        # 硬兜底（NODE_CAP）：可读性预算可能大于本执行体的节点上限——超限部分按名次末位先减邻域，
        # 主节点永不截断；读数记 `dropped_node_cap`（与「外弧带放不下」的 no_room 分列，各记各的因）。
        cap_room = max(0, NODE_CAP - len(mains))
        cap_dropped = []
        if len(admitted) > cap_room:
            cap_dropped = admitted[cap_room:]
            admitted = admitted[:cap_room]
        trial = list(mains) + [records[doi] for doi in admitted]
        planned = layout(trial, theme_keys, connections, rank, cells, spacings)
        if not planned["main_failed"]:
            break
    if planned["main_failed"]:  # 满梯仍装不下（病态大包）：退回「只有主节点、允许挤位」并记降级
        trial, shrink = list(mains), 1.0
        planned = layout(trial, theme_keys, connections, rank, *geometry_of(1.0), allow_overflow=True)
        no_room = list(candidates)
        cap_dropped = []
    dropped = set(planned.get("dropped") or [])
    nodes = [node for node in trial if node["id"] not in dropped]
    for node in nodes:  # 精读序名次只标先读层主节点（03 §9；其后层与邻域不标）
        rank_value = (prio_rank or {}).get(node["evidence_row"]) if node["evidence_row"] else None
        if rank_value:
            node["prio"] = rank_value
    scale_main = tuple(value * shrink for value in SIZE_MAIN)
    scale_neigh = tuple(value * shrink for value in SIZE_NEIGH)
    domains = {}
    for layer in (True, False):
        found = [node["cited_by_count"] for node in nodes if node["main"] is layer
                 and isinstance(node["cited_by_count"], int) and node["cited_by_count"] > 0]
        domains[layer] = (min(found), max(found)) if found else (None, None)
    for node in nodes:
        layer = bool(node["main"])
        node["shape"] = "card" if node["card"] else "plain"
        node["radius"] = round(node_radius(node["cited_by_count"], *domains[layer],
                                           scale=scale_main if layer else scale_neigh), 2)
        node["cites_missing"] = not isinstance(node["cited_by_count"], int)  # c=0 归最小档，不算「未取到」
        band_label, band_opacity = year_band(node["year"])
        node["opacity"] = round(band_opacity, 2)
        node["year_band"] = band_label
    band_counts = {label: 0 for label, _upper, _opacity in YEAR_BANDS}
    band_counts[UNKNOWN_YEAR_BAND] = 0
    for node in nodes:
        band_counts[node["year_band"]] += 1
    kept = {node["id"] for node in nodes}
    order_index = {ident: 0 for ident in main_ids}
    for index, ident in enumerate(node["id"] for node in nodes if not node["main"]):
        order_index[ident] = index + 1
    interior = sorted({(src, tgt, kind) for src, tgt, kind in edges_raw
                       if src in order_index and tgt in order_index})
    # 走廊只承载「主-主跨主题」引文（主题对＝11 条带）；邻域层的边逐条画，随该层展开才出现。
    def into_band(edge) -> bool:
        return theme_of(edge[0]) != theme_of(edge[1]) and edge[0] in main_ids and edge[1] in main_ids
    band_kept = sorted({edge for edge in interior if into_band(edge)})
    # 逐条线按「两端名次的最大值」排序：先主-主、再按邻域名次逐个成套——每条被画出的边都不会越过
    # 更高名次邻域的首条边，「邻域只收有边者」由排序本身保证，不靠事后补救。
    line_edges = sorted((edge for edge in interior if not into_band(edge)),
                        key=lambda edge: (max(order_index[edge[0]], order_index[edge[1]]),
                                          min(order_index[edge[0]], order_index[edge[1]]),
                                          edge[0], edge[1], edge[2]))
    # 每条被画出的边先保底一条：先主-主同主题线，再按名次给每个邻域点占一条，最后补其余（决定 10：
    # 「邻域只收有边者」是硬条件，不能让边预算把某个点整条切掉）。
    main_main = [edge for edge in line_edges if not order_index[edge[0]] and not order_index[edge[1]]]
    first_edge, extra_edges = {}, []
    for edge in line_edges:
        if not order_index[edge[0]] and not order_index[edge[1]]:
            continue
        owner = edge[0] if order_index.get(edge[0], 0) else edge[1]
        if owner not in first_edge:
            first_edge[owner] = edge
        else:
            extra_edges.append(edge)
    line_kept = (main_main + list(first_edge.values()) + extra_edges)[:EDGE_CAP]
    kept_line = {(edge[0], edge[1], edge[2]) for edge in line_kept}
    no_edge = [node["id"] for node in nodes if not node["main"]
               and not any(node["id"] in (edge[0], edge[1]) for edge in line_kept)]
    if no_edge:  # 常态下为空（排序保证首条边先入）；真出现即按「只收有边者」剔除并列读数
        gone = set(no_edge)
        nodes = [node for node in nodes if node["id"] not in gone]
        kept = {node["id"] for node in nodes}
        band_kept = [edge for edge in band_kept if edge[0] in kept and edge[1] in kept]
    for sector in planned["sectors"]:
        sector["label"] = _theme_name(theme_label.get(sector["key"], "")) or THEME_LABEL_FALLBACK
    labels = build_labels(planned["sectors"])
    angles = {sector["key"]: sector["start"] + sector["span"] / 2 for sector in planned["sectors"]}
    pair_counts = {}
    for src, tgt, _kind in band_kept:
        pair = tuple(sorted((theme_of(src), theme_of(tgt))))
        pair_counts[pair] = pair_counts.get(pair, 0) + 1
    corridors = build_corridors(pair_counts, lambda key: angles.get(key, 0.0))
    occupied = [box_of(node) for node in nodes]
    for node in sorted((item for item in nodes if item["main"] and item["evidence_row"]),
                       key=lambda item: str(item["evidence_row"])):  # 背景卡不编号（ADR-0028 义务 15）
        if node.get("prio"):  # 先读徽章先落位：行号避让时把徽章也当占用（两者不叠字）
            node["badge"] = {"x": round(node["x"] + node["radius"] * 0.72 + 3.5, 1),
                             "y": round(node["y"] - node["radius"] * 0.72 - 3.5, 1), "r": 7.5}
            occupied.append((node["badge"]["x"] - 9.0, node["badge"]["y"] - 9.0,
                             node["badge"]["x"] + 9.0, node["badge"]["y"] + 9.0))
        row_number_place(node, occupied)
    main_nodes = [node for node in nodes if node["main"]]
    for node in nodes:  # 命中圈（决定 13）：≥ 点半径＋2uu 且 ≤ 最近邻距的一半
        near = [math.hypot(node["x"] - other["x"], node["y"] - other["y"])
                for other in main_nodes if other["id"] != node["id"]]
        node["hit"] = round(min(node["radius"] + 2.0, min(near) / 2) if near
                             else node["radius"] + 2.0, 2)
    kept_neigh = [node for node in nodes if not node["main"]]
    frames = [box_of(node) for node in main_nodes] + [label_box(label) for label in labels]
    frames += corridor_boxes(corridors)
    node_boxes = extent([box_of(node) for node in nodes])
    all_boxes = extent([box_of(node) for node in nodes] + [label_box(label) for label in labels]
                       + corridor_boxes(corridors))
    linked = {end for edge in line_kept for end in edge[:2]}
    related = {end for edge in list(line_kept) + list(band_kept) for end in edge[:2]}
    usable = (CANVAS_W - 2 * CANVAS_MARGIN, CANVAS_H - 2 * CANVAS_MARGIN)
    ratio = lambda box: (round((box[2] - box[0]) / usable[0], 4), round((box[3] - box[1]) / usable[1], 4))
    truncated = {
        "main_nodes": len(main_nodes),
        "kept_neighbourhood": len(kept_neigh),
        "kept_nodes": len(nodes),
        "neighbourhood_candidates": len(candidates),
        "dropped_neighbourhood": len(candidates) - len(kept_neigh),
        "dropped_no_room": len(no_room) + len(dropped),
        "dropped_node_cap": len(cap_dropped),
        "dropped_no_edge": len(no_edge),
        "line_edges_total": len(line_edges),
        "line_edges_kept": len(line_kept),
        "band_edges": len(band_kept),
        "edges_total": len(interior),
        "edges_kept": len(line_kept) + len(band_kept),
        "dropped_edges": len(interior) - len(line_kept) - len(band_kept),
        "self_loops": self_loops,
        "size_shrink": round(shrink, 2),
        "crowded": bool(planned.get("crowded")),
        "isolated_kept": sum(1 for node in nodes if node["id"] not in related),
        "band_only_kept": sum(1 for node in nodes if node["id"] not in linked and node["id"] in related),
        "cap_hit": bool(len(line_edges) > len(line_kept) or len(candidates) > len(kept_neigh)),
        "budget_hit": bool(no_room or dropped or shrink < 1.0 or planned.get("crowded")),
    }
    seeds_n, edges_n = len(citation["seeds"]), len(citation["edges"])
    degraded = (not citation["present"]) or (seeds_n == 0 and edges_n == 0)
    if degraded and citation["present"]:
        citation["notes"].insert(0, "run/b1/raw/citation-*.json 在场但无任何可解析载荷"
                                    "（整轮请求失败或形态不符）：按引文数据不可得出降级块")
    frame_main = frame_of(frames) if frames else f"0 0 {CANVAS_W:.0f} {CANVAS_H:.0f}"
    layout_reading = {key: value for key, value in planned.items() if key != "dropped"}
    size_reading = lambda layer: {  # 两层各一把尺：门禁按此复算直径
        "min_r": round((scale_main if layer else scale_neigh)[0], 2),
        "max_r": round((scale_main if layer else scale_neigh)[1], 2),
        "lo": domains[layer][0], "hi": domains[layer][1]}
    return {
        "spec": "reader-html-graph-v2（ADR-0030：扇面布局＋可读性预算＋走廊带；坐标＝输入纯函数）",
        "profile": "full",
        "degraded": degraded,
        "degraded_reason": citation["notes"][0] if degraded and citation["notes"] else None,
        "caps": {"nodes": NODE_CAP, "edges": EDGE_CAP},
        "graph_truncated": truncated,
        # 年份档（ADR-0027）：档定义随数据落盘，图例只画本包真有节点的档；四档定义本身跨包不变。
        "year_bands": [{"label": label, "opacity": opacity, "nodes": band_counts[label]}
                       for label, _upper, opacity in YEAR_BANDS]
                      + [{"label": UNKNOWN_YEAR_BAND, "opacity": 1.0,
                          "nodes": band_counts[UNKNOWN_YEAR_BAND]}],
        # 邻域层＝背景层（ADR-0027／0030）：默认收起，原位写这两个真实计数。
        "layer": {
            "neigh_nodes": len(kept_neigh),
            "neigh_edges": sum(1 for edge in line_kept
                               if not (order_index.get(edge[0], 0) == 0 and order_index.get(edge[1], 0) == 0)),
        },
        "readings": {"seeds": len(citation["seeds"]), "edges_collected": len(citation["edges"]),
                     "seeds_with_refs": readings["seeds_with_refs"], "files": citation["files"],
                     "notes": citation["notes"],
                     "ink": {"nodes": [round(value, 1) for value in node_boxes],
                             "all": [round(value, 1) for value in all_boxes], "usable": list(usable),
                             "ratio_nodes": ratio(node_boxes), "ratio_all": ratio(all_boxes)}},
        "layout": layout_reading,
        "frame": {"main": frame_main, "all": f"0 0 {CANVAS_W:.0f} {CANVAS_H:.0f}"},
        "sectors": planned["sectors"],
        "corridors": corridors,
        "labels": labels,
        "nodes": nodes,
        "edges": [{"source": src, "target": tgt, "kind": kind, "arrow": "source_cites_target",
                   "draw": "line" if (src, tgt, kind) in kept_line else "band"}
                  for src, tgt, kind in sorted(set(line_kept) | set(band_kept))],
        "encoding": {
            "size": {"kind": "pow", "power": 1.0 / 3.0, "domain": "本包内被引数 min/max（跨包不可比）",
                     "main": size_reading(True), "neigh": size_reading(False),
                     "missing": "缺被引数单列「未取到」档（固定最小号、空心问号描边）；c=0 归最小档"},
            "opacity": ("年份档（ADR-0027）：≤1999／2000–2009／2010–2019／≥2020 → "
                        "0.85／0.90／0.95／1.00（越老越淡）；缺年单列「年份未取到」档，不并入最新档"),
            "stroke": "全文获取档（有全文／仅摘要／仅题录；缺记 ∅ 不猜）",
            "shape": "有无文献卡（card／plain）",
            "edge": "引文方向：实线＝后向（source 引用 target）、虚线＝前向；箭头恒指被引方",
            "corridor": "跨主题引文按主题对聚成走廊带：带宽 ∝ √条数，两端主题色端点、带上标条数",
        },
    }


def graph_svg(data: dict, themes, ctx=None) -> str:
    """内联 SVG（坐标来自数据块）：走廊带、主线、节点与主题标签；节点交互与证据行／卡锚点对齐。"""
    corridors = data.get("corridors") or []
    theme_slot = {key: index + 1 for index, (key, _label) in enumerate(themes)}
    band_parts, edge_parts, neigh_edge_parts = [], [], []
    main_parts, neigh_parts, label_parts = [], [], []
    for corridor in corridors:
        token = lambda key: f"var(--c{theme_slot[key]})" if key in theme_slot else "var(--c-neutral)"
        slot_a, slot_b = token(corridor["a"]), token(corridor["b"])
        band_parts.append(f'<polygon class="band" points="{corridor["path"]}"/>')
        for end, fill in zip(corridor["ends"], (slot_a, slot_b)):
            band_parts.append(f'<circle class="band-end" cx="{end["x"]:.1f}" cy="{end["y"]:.1f}" '
                              f'r="3.5" fill="{fill}"/>')
        band_parts.append(f'<text class="band-text" x="{corridor["label"]["x"]:.1f}" '
                          f'y="{corridor["label"]["y"] + 3.4:.1f}" text-anchor="middle">'
                          f'{corridor["count"]}</text>')
    is_main = {node["id"]: node["main"] for node in data["nodes"]}
    by_id = {node["id"]: node for node in data["nodes"]}
    for edge in data["edges"]:
        if edge.get("draw") != "line":
            continue                      # 跨主题引文聚成走廊带，不再各画一条线（决定 8）
        src, tgt = by_id.get(edge["source"]), by_id.get(edge["target"])
        if not src or not tgt:
            continue
        marker = "arw-bwd" if edge["kind"] == "bwd" else "arw-fwd"
        line = (f'<line class="edge edge-{edge["kind"]}" x1="{src["x"]:.1f}" y1="{src["y"]:.1f}" '
                f'x2="{tgt["x"]:.1f}" y2="{tgt["y"]:.1f}" marker-end="url(#{marker})"/>')
        # 邻域层的边跟着该层一起收起：折叠后画面上不该留下指向已隐藏节点的线。
        if is_main.get(edge["source"]) and is_main.get(edge["target"]):
            edge_parts.append(line)
        else:
            neigh_edge_parts.append(line)
    for node in data["nodes"]:
        slot = "var(--c-neutral)"  # 03 §4：邻域节点恒 neutral（主题色只给主节点）
        if node["main"] and node["theme"] in theme_slot:
            slot = f"var(--c{theme_slot[node['theme']]})"
        tier_class = {"有全文": "tier-full", "仅摘要": "tier-abstract"}.get(node["tier"], "tier-tert")
        classes = f'node node-{"card" if node["shape"] == "card" else "plain"} {tier_class}'
        if node.get("cites_missing"):
            classes += " node-unknown"      # 缺被引数单列一档（决定 6）：固定最小号＋空心问号
        target = ""
        if node["main"]:  # 只有主节点有落点；邻域层是背景，不给目标也不给手型
            if node["card"]:
                target = f' data-open-card="{attresc(node["card"])}"'
            elif node["evidence_row"]:
                target = f' data-scroll-row="{node["evidence_row"]}"'
        label = node["title"] or node["id"] or "未题名条目"
        tip = (f'{esc(label)}｜' + (f'{esc(node["citation"])}｜' if node.get("citation") else "")
               + f'{ROW_LABEL} {node["evidence_row"] if node["evidence_row"] else "—"}｜'
               f'DOI {esc(node["id"]) or MISSING}｜'
               f'{esc(str(node["year"])) if node["year"] else "年 " + MISSING}｜{node["tier"]}｜'
               f'被引 {node["cited_by_count"] if node["cited_by_count"] is not None else MISSING}｜'
               f'本次交付内被引 {node["in_pool_citations"] if node["in_pool_citations"] is not None else MISSING}｜'
               f'来源 {esc(pool_face_label(node["pool_face"])) or MISSING}'
               + (f'｜主题冲突：卡 §{node["theme"]}／表 §{node["theme_table"]}' if node.get("theme_conflict") else "")
               + (f'｜先读 {node["prio"]}' if node.get("prio") else ""))
        # 图上几十个点要逐点听完：短名单给 AT，长详情留给 <title>（第 6 轮 minor／义务 17）。
        aria_tip = (f'{esc(label)}｜{ROW_LABEL} {node["evidence_row"] if node["evidence_row"] else "—"}｜'
                    f'{esc(str(node["year"])) if node["year"] else "年 " + MISSING}｜{node["tier"]}｜'
                    f'被引 {node["cited_by_count"] if node["cited_by_count"] is not None else MISSING}')
        number = ""
        if node.get("num"):
            num = node["num"]
            extra = "" if num["mode"] == "inside" else " node-num-out"
            number = (f'<text class="node-num{extra}" x="{num["x"]:.1f}" y="{num["y"]:.1f}" '
                      f'text-anchor="middle">{esc(num["text"])}</text>')
        if node.get("cites_missing"):
            number += (f'<text class="node-q" x="{node["x"]:.1f}" y="{node["y"] + 4.2:.1f}" '
                       f'text-anchor="middle">?</text>')
        dot = (f'<circle class="node-dot" cx="{node["x"]:.1f}" cy="{node["y"]:.1f}" '
               f'r="{node["radius"]:.2f}" fill-opacity="{node["opacity"]:.2f}"/>')
        badge = ""
        if node.get("badge"):  # 徽章落在圆右上方 45°：色底＋描边，与圆内行号在字面上可区分
            box = node["badge"]
            badge = (f'<circle class="node-prio-dot" cx="{box["x"]:.1f}" cy="{box["y"]:.1f}" r="7.5"/>'
                     f'<text class="node-prio-num" x="{box["x"]:.1f}" y="{box["y"] + 3.4:.1f}">'
                     f'{node["prio"]}</text>')
        if node["main"]:
            main_parts.append(
                f'<g class="{classes}"{target} tabindex="0" role="button" '
                f'aria-label="{attresc(aria_tip)}" style="--nc:{slot}">'
                f'<circle class="node-hit" cx="{node["x"]:.1f}" cy="{node["y"]:.1f}" r="{node["hit"]:.1f}"/>'
                + dot + number + badge + f"<title>{tip}</title></g>")
        else:
            # 背景层：无 data-*／无 tabindex／无 aria-label／无命中圈（不给落点，也不假给点击面）
            neigh_parts.append(f'<g class="{classes}" style="--nc:{slot}">' + dot + number + "</g>")
    for label in data.get("labels") or []:
        label_parts.append(f'<text class="cluster-text" x="{label["x"]:.1f}" y="{label["y"]:.1f}" '
                           f'text-anchor="middle">{esc(label["text"])}</text>')
    layer = data.get("layer") or {}
    control = ""
    if layer.get("neigh_nodes"):
        control = ('<button class="btn screen-only" id="graph-fold" type="button" aria-expanded="false" '
                   'aria-controls="graph-layers">展开相邻文献</button>'
                   f'<p class="note" id="graph-fold-note">已收起 {layer["neigh_nodes"]} 个相邻文献点'
                   f'与 {layer["neigh_edges"]} 条连线（这一层只作背景：不承载主题色、不进键盘与读屏）。'
                   "展开后它们出现在各自主题的外圈，图上已有的文献与连线不动。</p>")
    frame = data.get("frame") or {}
    main_view = frame.get("main") or f"0 0 {CANVAS_W:.0f} {CANVAS_H:.0f}"
    all_view = frame.get("all") or f"0 0 {CANVAS_W:.0f} {CANVAS_H:.0f}"
    return (control
            + '<p class="note graph-hint screen-only">图较宽：可横向滑动查看（字号不缩小）</p>'
            + '<div class="graph-scroll">'
            + f'<svg id="graph-layers" class="graph" viewBox="{main_view}" '
              f'data-viewbox-all="{all_view}" data-viewbox-main="{main_view}" role="img" '
              'aria-label="文献关系图（默认只画本次交付的文献与它们的引用关系，相邻文献按需展开）">'
            + '<defs>'
            '<marker id="arw-bwd" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" '
            'orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#8e8b82"/></marker>'
            '<marker id="arw-fwd" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" '
            'orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#8e8b82"/></marker>'
            "</defs>"
            + '<g class="bands">' + "".join(band_parts) + "</g>"
            + '<g data-layer="neigh" aria-hidden="true" class="folded">'
            + "".join(neigh_edge_parts) + "".join(neigh_parts) + "</g>"
            + "".join(edge_parts) + "".join(main_parts)
            + "".join(label_parts)   # 主题标签最后绘制：先画时会被节点圆点压印（第 6 轮 P2）
            + "</svg>"
            + "</div>"
            + graph_legend(data, themes) + graph_truncation_note(data, ctx))


def _legend_item(sample: str, text: str) -> str:
    return f'<span class="lg">{sample}{text}</span>'


def _theme_name(label: str) -> str:
    """主题名（读者面）：去掉章节号——`§4.2 关键试验与头对头证据` → `关键试验与头对头证据`（ADR-0028）。"""
    return re.sub(r"^\s*§?\s*\d+(?:\.\d+)?\s*", "", label or "").strip() or (label or "")


def graph_legend(data: dict, themes) -> str:
    """图例＝色样键＋短标签，不是一段散文。

    每条编码都要在几十个圆点旁边逐条对照，写成一段话等于要求读者先背下来再看图。色样一律沿用
    在位 token，图与图例同源；只画本交付真有的取值（同 ADR-0027 的年份档口径）。
    """
    def dot(slot: str, extra: str = "") -> str:
        return f'<span class="kp" style="background:{slot};{extra}"></span>'

    used = {node["theme"] for node in data["nodes"] if node["main"] and node["theme"]}
    items = [_legend_item(dot(f"var(--c{index + 1})"), esc(_theme_name(label)))
             for index, (key, label) in enumerate(themes) if key in used]
    if any(not node["main"] for node in data["nodes"]):
        items.append(_legend_item(dot("var(--c-neutral)"), "相邻文献（未归主题、只作背景）"))
    items.append(_legend_item(dot("var(--c-neutral)", "width:6px;height:6px")
                              + dot("var(--c-neutral)", "width:17px;height:17px"),
                              "圆越大＝本包内被引越多（只在本包内可比、跨包不可比）"))
    bands = [band for band in (data.get("year_bands") or []) if band["nodes"] > 0]
    if bands:  # 只画本包真有节点的档；四档定义本身跨包不变（ADR-0027）
        pairs = []
        for band in bands:
            tail = "（记满透明度，不代表新）" if band["label"] == UNKNOWN_YEAR_BAND else ""
            pairs.append(dot("var(--c-neutral)", f"opacity:{band['opacity']}")
                         + esc(band["label"]) + tail)
        items.append('<span class="lg lg-wide">发表年份（越老越淡）：' + " ".join(pairs) + "</span>")
    if any(node.get("cites_missing") for node in data["nodes"] if node["main"]):
        items.append(_legend_item('<span class="kp kp-ring kp-question">?</span>',
                                  "空心问号＝被引数未取到（不并入任何数值档）"))
    if any(node["main"] and not node["card"] for node in data["nodes"]):  # 只有真有「无卡」主点才画
        items.append(_legend_item(dot("var(--c-neutral)") + '<span class="kp kp-ring"></span>',
                                  "实心＝有文献卡、空心＝无卡"))
    tier_keys = [("有全文", '<span class="kp kp-ring"></span>'),
                 ("仅摘要", '<span class="kp kp-ring tier-abstract"></span>'),
                 ("仅题录", '<span class="kp kp-ring tier-tert"></span>')]
    tier_present = [label for label, _ in tier_keys if any(node["tier"] == label for node in data["nodes"])]
    if tier_present:  # 只画本交付真有的取值（同 ADR-0027 的年份档口径）
        items.append(_legend_item("".join(sample for label, sample in tier_keys if label in tier_present),
                                  "圈线＝" + "／".join(tier_present)))
    if data.get("corridors"):
        items.append(_legend_item('<span class="kp kp-band"></span>',
                                  "粗带＝跨主题引用聚成的带（两端圆点＝两端主题色，带上数字＝条数）"))
    items.append(_legend_item('<span class="kl"></span>', "本文献引用了对方"))
    items.append(_legend_item('<span class="kl kl-fwd"></span>', "对方引用了本文献（箭头恒指被引方）"))
    items.append(_legend_item('<span class="kp kp-num">7</span>', "数字＝文献号（圆内放不下时在圆旁）"))
    items.append(_legend_item('<span class="kp kp-prio"></span>', "小圆牌＝先读名次"))
    return '<p class="legend">' + "".join(items) + "</p>"


def graph_truncation_note(data: dict, ctx=None) -> str:
    """图上自述：默认只画主节点、跨主题边聚成走廊、孤点与缺读数都要在图旁说清。

    「读数只有一处真源」的另一半是「缺口只有一处也不够」——读者看图问的是「覆盖全了吗」，
    答案必须就在图旁边，而不是要他去数据块或页尾拼。
    """
    truncated = data.get("graph_truncated") or {}
    layer = data.get("layer") or {}
    corridors = data.get("corridors") or []
    sentences = []
    if layer.get("neigh_nodes"):
        sentences.append(f'本图默认只画本次文献点（{truncated.get("main_nodes")} 个）及其引用关系；'
                         f'另有 {layer["neigh_nodes"]} 个相邻文献点与 {layer["neigh_edges"]} 条连线'
                         "收在「展开相邻文献」按钮里（它们只作背景，不与主图争地位）。")
    if corridors:
        sentences.append(f'跨主题的引用不逐条画线：按主题对聚成 {len(corridors)} 条带——'
                         "带宽随条数、两端圆点是两端主题色、带上数字是条数。")
    if truncated.get("crowded"):
        sentences.append("本次点位密度超出可读性预算，已按最紧铺法落图（个别点可能挨得近）——"
                         "本次文献一篇不减，缺口读数见页尾校验信息。")
    if truncated.get("budget_hit"):
        cap_dropped = truncated.get("dropped_node_cap") or 0
        reason = ("余下的放不下：外圈放得下几个由画布尺寸决定" if not cap_dropped
                  else f"其中 {cap_dropped} 个受单页可读点数上限所限（按名次自末位减）")
        sentences.append(f'相邻文献候选 {truncated["neighbourhood_candidates"]} 个，本次上 '
                         f'{truncated["kept_neighbourhood"]} 个（{reason}，本次文献一个不减）。')
    elif truncated.get("cap_hit"):
        sentences.append(f'连线上 {truncated["line_edges_kept"]}／{truncated["line_edges_total"]} 条，'
                         f'未上 {truncated["dropped_edges"]} 条。')
    missing = sum(1 for node in data["nodes"] if node.get("cites_missing"))
    if missing:
        sentences.append(f'有 {missing} 个点的被引数未取到：它们单列一档（空心问号、最小号），'
                         "不按数值参与比较。")
    isolated = truncated.get("isolated_kept") or 0
    band_only = truncated.get("band_only_kept") or 0
    if isolated or band_only:  # 03 §8：孤立节点保留可见即因「未与邻域相连」是读数——那就得写出来
        text = (f"本次有 {isolated} 个点没有任何引用连线" if isolated else "")
        if band_only:
            text += ("；另有 " if isolated else "本次有 ") + \
                    f"{band_only} 个点只以跨主题的引用带参与（图上不落单独线）"
        sentences.append(text + "——「没和任何文献相连」本身就是一条信息，已保留可见、不剔除。")
    if not sentences:
        return ""
    text = "".join(sentences)
    return f'<p class="note">{ctx.inline(text) if ctx else esc(text)}</p>'


def page_shell(title: str, body: str, script: str, graph_json: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)}</title>
<style>
{PAGE_CSS}
</style>
</head>
<body>
{body}
<script type="application/json" id="graph-data">{graph_json}</script>
<script>
{script}
</script>
</body>
</html>
"""


PAGE_CSS = r"""
:root {
  /* 颜色不是按观感调的，是按 WCAG AA 的下限反解的：正文文字 ≥4.5（--muted 对 --surface-card、
     --accent-active 对 --surface-card、--accent-strong 上的白字），图形与焦点圈 ≥3.0
     （--teal 对四种底色、--c1..--c-neutral 对关系图底色 --surface-soft）。
     改任一个值前先按同一口径复算——本页的对比度是产品承诺（reader-html.md §3.8），不是偏好。 */
  --canvas: #faf9f5; --ink: #141413; --body: #3d3d3a; --body-strong: #252523;
  --muted: #67655f; --muted-soft: #8e8b82; --hairline: #e6dfd8; --hairline-soft: #ebe6df;
  --surface-soft: #f5f0e8; --surface-card: #efe9de; --surface-cream-strong: #e8e0d2;
  --surface-dark: #181715; --surface-dark-elevated: #252320;
  /* --accent 只用于深底上的文字与装饰；浅底上的文字用 --accent-active（对 --surface-card 4.79）、
     承载白字的珊瑚底用 --accent-strong（白字 5.79）。三者不是同一个用途，别互相替换。 */
  --accent: #cc785c; --accent-active: #9e4f36; --accent-strong: #9e4f36; --teal: #358a7a;
  --on-primary: #ffffff; --on-dark: #faf9f5; --on-dark-soft: #a09d96;
  --radius-sm: 8px; --radius-md: 12px; --radius-pill: 9999px;
  --font-display: "Cormorant Garamond", Georgia, "Times New Roman", serif;
  --font-body: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Consolas, monospace;
  /* 主题色是关系图的「主题」通道：按填充叠在 --surface-soft 上的**合成值**取值，
     且在年份透明度下限（0.85）处仍 ≥3.15:1——只按 token 本身在画布上的观感调色是不够的。 */
  --c1: #b0603c; --c2: #328374; --c3: #af641e; --c4: #637697; --c5: #906890; --c6: #77756d;
  --c-neutral: #79746b;
  /* 粘性装置实测高度（桌面：顶栏 48＋筛选条 88；窄屏见下方 900px 档）。
     锚点落点与滚动边距全部按这两个值算——写死 60px 会让每个锚点把标题送进顶栏底下。 */
  --nav-h: 52px; --sticky-stack: 148px;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
html.overlay-open { overflow: hidden; }  /* 抽屉打开时锁背景滚动（第 5 轮 P2） */
body { margin: 0; background: var(--canvas); color: var(--body);
  font-family: var(--font-body); font-size: 16px; line-height: 1.6; overflow-wrap: break-word; }
a { color: var(--accent-active); text-decoration: none; }
a:hover { text-decoration: underline; }
.hero a, footer a { color: var(--accent); }
a:focus-visible, button:focus-visible, summary:focus-visible, [tabindex]:focus-visible {
  outline: 3px solid var(--teal); outline-offset: 2px; border-radius: 2px; }
.print-toggle { white-space: nowrap; }  /* 1440 下勾选框会被折行甩到离标签 1,100px 的第二行 */
.fp-list { columns: 2; column-gap: 28px; }
.fp-list .fp { break-inside: avoid; }
.skip { position: absolute; left: -9999px; }
.skip:focus { left: 8px; top: 8px; z-index: 99; background: var(--surface-dark); color: var(--on-dark);
  padding: 8px 14px; border-radius: var(--radius-sm); }
.wrap { max-width: 1200px; margin: 0 auto; padding: 0 24px; }
.hero { background: var(--surface-dark); color: var(--on-dark); padding: 40px 0 36px; }
.hero h1 { font-family: var(--font-display); font-weight: 400; letter-spacing: -0.02em;
  font-size: clamp(26px, 4.2vw, 40px); line-height: 1.12; margin: 0 0 12px; }
.hero .meta { color: var(--on-dark-soft); font-size: 14px; margin: 0 0 14px; overflow-wrap: anywhere; }
.hero .q { background: var(--surface-dark-elevated); border-radius: var(--radius-sm);
  padding: 12px 16px; font-size: 15px; margin: 0 0 14px; }
.hero .q strong { color: var(--on-dark); }
.hero .answer { border-left: 1px solid var(--accent); padding: 4px 0 4px 14px; margin: 0 0 16px;
  font-size: 16px; color: var(--on-dark); }
.hero .answer .lab { display: block; font-size: 11px; letter-spacing: 1px; text-transform: uppercase;
  color: var(--accent); margin-bottom: 4px; }
.hero .answer a { color: var(--accent); }
.stat-row { display: flex; flex-wrap: wrap; gap: 12px; }
.stat { background: var(--surface-dark-elevated); border-radius: var(--radius-sm); padding: 10px 16px; min-width: 104px; }
.stat .num { font-family: var(--font-display); font-size: 26px; line-height: 1; color: var(--accent); }
.stat .lab { font-size: 12px; color: var(--on-dark-soft); margin-top: 6px; }
.topnav { position: sticky; top: 0; z-index: 30; background: var(--surface-dark);
  border-top: 1px solid #2a2824; border-bottom: 1px solid #2a2824; }
.topnav .wrap { display: flex; flex-wrap: wrap; align-items: center; gap: 2px 14px; padding: 8px 24px; font-size: 13.5px; }
.topnav a { color: var(--on-dark-soft); padding: 4px 2px; }
.topnav a:hover { color: var(--on-dark); text-decoration: none; }
.topnav a.active { color: var(--accent); }
.topnav .brand { color: var(--on-dark); font-weight: 500; margin-right: 6px; }
main { padding: 28px 0 64px; }
#main:focus { outline: none; }  /* 跳过链接的程序化落点：要能接收焦点，但不该画整块描边 */
.section { margin: 0 0 44px; }
.h2 { font-family: var(--font-display); font-weight: 400; font-size: 30px; letter-spacing: -0.01em;
  color: var(--ink); margin: 40px 0 14px; scroll-margin-top: var(--nav-h); }
.h3 { font-family: var(--font-display); font-weight: 400; font-size: 23px; color: var(--ink);
  margin: 28px 0 10px; scroll-margin-top: var(--nav-h); }
/* 锚点落点：导航目标是 section，滚动边距必须挂在 section 上——挂在子标题上等于没挂。 */
.section { scroll-margin-top: var(--nav-h); }
#main { scroll-margin-top: var(--nav-h); }  /* 跳过链接落点：别把 §1 标题送进顶栏底下 */
#s5 { scroll-margin-top: var(--sticky-stack); }  /* 证据表另有筛选条粘在顶栏下面 */
.h5 { font-size: 15px; margin: 14px 0 6px; color: var(--body-strong); }
.section p { margin: 0 0 12px; }
.mdul { margin: 0 0 14px; padding-left: 22px; }
.mdul li { margin: 3px 0; }
code { font-family: var(--font-mono); font-size: 0.86em; background: var(--surface-soft);
  border: 1px solid var(--hairline-soft); border-radius: 4px; padding: 0 4px; overflow-wrap: anywhere; }
.rowref { border-bottom: 1px dotted var(--accent); }
.fileref { border-bottom: 1px dotted var(--accent); }
.secref { border-bottom: 1px dotted var(--muted-soft); color: inherit; }
.secref:hover { color: var(--accent-active); }
.cardref { font-family: var(--font-mono); font-size: 0.84em; }
.badge { display: inline-block; font-size: 12px; font-weight: 500; padding: 2px 10px;
  border-radius: var(--radius-pill); background: var(--surface-cream-strong); color: var(--ink); }
.badge.coral { background: var(--accent-strong); color: var(--on-primary); }
.badge.level { background: var(--ink); color: var(--on-dark); }
.badge.status-ok { background: #eaf4ec; color: #2c6b3f; }
.badge.status-miss { background: #f7ecec; color: #8a3b3b; }
.controls { display: flex; flex-wrap: wrap; gap: 8px 10px; align-items: center;
  background: var(--surface-soft); border: 1px solid var(--hairline); border-radius: var(--radius-md);
  padding: 10px 14px; margin: 0 0 16px; position: sticky; top: var(--nav-h); z-index: 20; }
.chip { font-size: 12.5px; border: 1px solid var(--hairline); background: #fff; color: var(--body);
  border-radius: var(--radius-pill); padding: 3px 11px; cursor: pointer; }
.chip[aria-pressed="true"] { background: var(--ink); color: var(--on-dark); border-color: var(--ink); }
.chip:hover { border-color: var(--accent-active); color: var(--accent-active); }
.controls .sep { width: 1px; height: 20px; background: var(--hairline); }
.controls .count { font-size: 12.5px; color: var(--muted); margin-left: auto; }
.controls input[type="search"] { font: inherit; font-size: 13px; padding: 4px 10px;
  border: 1px solid var(--hairline); border-radius: var(--radius-sm); background: #fff; width: 180px; }
.controls select { font: inherit; font-size: 13px; padding: 4px 8px; border: 1px solid var(--hairline);
  border-radius: var(--radius-sm); background: #fff; }
.erow { background: var(--surface-card); border: 1px solid var(--hairline); border-radius: var(--radius-md);
  padding: 14px 16px; margin: 0 0 12px; scroll-margin-top: var(--sticky-stack); }
.erow.hide { display: none; }
.erow.flash { animation: flashbg 1.6s ease-out; }
/* 深链落点自证：从 IM 点开 #row-N／#card-… 的读者，第一眼就该看见"就是这一条"。 */
.erow:target, .card:target { outline: 3px solid var(--accent-strong); outline-offset: 2px; }
@keyframes flashbg { 0% { background: #ffe3d5; } 100% { background: var(--surface-card); } }
/* 搜索命中：用与行闪光同一档的浅珊瑚，别用浏览器默认的荧光黄。 */
mark { background: #ffe3d5; color: var(--ink); border-radius: 2px; padding: 0 1px; }
.erow-head { display: flex; gap: 12px; align-items: baseline; }
.rownum { font-family: var(--font-mono); font-size: 12px; background: var(--ink); color: var(--on-dark);
  border-radius: var(--radius-sm); padding: 2px 8px; white-space: nowrap; }
.erow-title { min-width: 0; }
.t-authors { font-weight: 500; color: var(--body-strong); font-size: 14.5px; }
.t-meta { color: var(--muted); font-size: 12.5px; }
.t-doi { font-family: var(--font-mono); font-size: 12px; color: var(--muted); overflow-wrap: anywhere; }
.t-doi a { color: var(--accent-active); }  /* 引文链的最后一跳：DOI 成链，读者不必自己拼检索式 */
.prio-list { list-style: none; margin: 8px 0 18px; padding: 0; }
/* 先读行＝三行块（ADR-0031）：题名行（含名次与卡按钮）／副行／理由行；题名＋副行是一个链，
   所以整块的可点范围与卡片索引 chip 同构，区别只在它通向证据表行而不是卡。 */
.prio-item { display: grid; grid-template-columns: minmax(0, 1fr) auto; column-gap: 12px;
  row-gap: 5px; align-items: start;
  padding: 10px 12px; border: 1px solid var(--hairline); border-radius: var(--radius-sm);
  background: var(--surface-soft); margin-bottom: 8px; }
.prio-head { grid-column: 1; grid-row: 1; display: flex; flex-wrap: wrap; align-items: baseline;
  gap: 4px 10px; min-width: 0; }
.prio-main { flex: 1 1 auto; min-width: 0; display: block; }
/* 题名整条呈现（可折行）：先读行是「认文献」的地方，砍成半句或砍掉副题等于让人拿着残句去认
   （长题名在本行实测 963px 下多为两行，行高代价换读者拿到完整题名）。 */
.prio-title { display: block; font-size: 14.5px; font-weight: 500; color: var(--accent-active);
  overflow-wrap: anywhere; }
.prio-main:hover .prio-title { text-decoration: underline; }
.prio-meta { display: block; margin-top: 3px; font-size: 12.5px; color: var(--muted); }
.prio-dim { font-size: 12px; color: var(--body); border: 1px solid var(--hairline);
  border-radius: var(--radius-pill); padding: 1px 8px; white-space: nowrap; }
.prio-reason { grid-column: 1 / -1; grid-row: 2; margin: 0; font-size: 14px; color: var(--body-strong); }
.prio-card { grid-column: 2; grid-row: 1; align-self: start; white-space: nowrap; }
.prio-rank { display: inline-flex; align-items: center; justify-content: center;
  padding: 2px 9px; border-radius: var(--radius-pill); white-space: nowrap;
  background: var(--accent-strong); color: var(--on-primary); font-weight: 700; font-size: 12px; }
.prio-fold > summary { cursor: pointer; color: var(--accent-active); font-size: 13px; padding: 8px 0 4px; }
.prio-group-head { margin-top: 18px; }
.erow-badges { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 6px; }
.erow-rel { font-size: 13.5px; color: var(--body); }
.erow-actions { display: flex; gap: 8px; margin-top: 8px; }
.btn { font: inherit; font-size: 12.5px; padding: 3px 12px; border-radius: var(--radius-sm);
  border: 1px solid var(--hairline); background: #fff; color: var(--body-strong); cursor: pointer; }
.btn:hover { border-color: var(--accent-active); color: var(--accent-active); }
.btn.disabled { color: var(--muted-soft); cursor: default; }
.btn.disabled:hover { border-color: var(--hairline); color: var(--muted-soft); }
.erow-more { margin-top: 10px; border-top: 1px dashed var(--hairline); padding-top: 8px; }
.cell { margin: 8px 0; }
.cell-name { font-size: 11.5px; letter-spacing: 0.5px; text-transform: uppercase; color: var(--muted); }
.cell-body { font-size: 13.5px; overflow-wrap: anywhere; }
.grouphead { font-family: var(--font-display); font-size: 19px; color: var(--ink);
  margin: 22px 0 10px; border-bottom: 1px solid var(--hairline); padding-bottom: 4px; }
.mdtbl { border-collapse: collapse; width: 100%; }
.mdtbl th, .mdtbl td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--hairline-soft);
  vertical-align: top; font-size: 12.5px; }
.table-wrap { overflow-x: auto; border: 1px solid var(--hairline); border-radius: var(--radius-md); background: var(--canvas); }
/* 窄屏：顶栏折成三行（111px）而筛选条 227px 还钉在 44px 上——自遮 67px，且两条常驻吃掉
   视口 40%。让筛选条随文滚动（回到 900px 以上再粘），并把锚点余量改成只算顶栏。 */
@media (max-width: 900px) {
  .graph-scroll svg.graph { min-width: 900px; }
  :root { --nav-h: 118px; --sticky-stack: 118px; }
  .controls { position: static; }
  /* 先读行改单列：卡按钮落到理由之后，题名不再被按钮列挤压（能折行就不缩字号） */
  .prio-item { grid-template-columns: minmax(0, 1fr); }
  .prio-card { grid-column: 1; grid-row: 3; justify-self: start; }
}
.graph-scroll { overflow-x: auto; }  /* 窄屏图不缩字号：可横向滑动（见下方 900px 档） */
@media screen and (min-width: 901px) { .graph-hint { display: none; } }  /* 窄屏才提示可横向滑动 */
.graph { width: 100%; height: auto; background: var(--surface-soft); border: 1px solid var(--hairline);
  border-radius: var(--radius-md); }
svg.graph g.folded { display: none; }  /* 邻域层整层收起（SVG 元素不认 hidden 属性，用类） */
.cluster-hull { fill: none; stroke: var(--hairline); stroke-dasharray: 3 4; }
.node-prio-dot { fill: var(--accent-strong); stroke: var(--canvas); stroke-width: 1.5; }
.node-prio-num { fill: var(--on-primary); font-size: 10px; font-weight: 700; text-anchor: middle; }
.cluster-text { font-size: 12px; font-weight: 500; paint-order: stroke; stroke: var(--surface-soft);
  stroke-width: 4px; stroke-linejoin: round; }  /* 标签最后绘制＋纸色衬底：压过点与线也读得出 */
.node { cursor: default; }
.node[data-open-card], .node[data-scroll-row] { cursor: pointer; }  /* 只有真有目标的圆点才是手型：邻域点不可点，别给假承诺 */
.node .node-hit { fill: transparent; }
.node .node-dot { fill: var(--nc); stroke: var(--ink); stroke-width: 1.6; }
.node.tier-abstract .node-dot { stroke: var(--muted); stroke-width: 1.6; stroke-dasharray: 4 2; }
.node.tier-tert .node-dot { stroke: var(--muted-soft); stroke-width: 1.4; stroke-dasharray: 1.5 2; }
.node.node-plain .node-dot { fill: var(--canvas); }
.node .node-num { font-size: 11px; fill: var(--ink); font-family: var(--font-mono); }
/* 缺被引数（决定 6）：单列一档——空心问号，不进任何数值档 */
.node-unknown .node-dot { fill: var(--canvas); stroke: var(--muted); stroke-width: 1.4; stroke-dasharray: 2 2; }
.node .node-q { font-size: 12px; font-weight: 700; fill: var(--muted); }
/* 圆内放不下就外置（决定 7）：号码在圆旁，描边式光晕保证压在连线上也读得出 */
.node .node-num-out { fill: var(--ink); paint-order: stroke; stroke: var(--canvas); stroke-width: 2.5px;
  stroke-linejoin: round; }
.node.node-plain .node-num { fill: var(--body); }
.node:hover .node-dot, .node:focus-visible .node-dot { opacity: 1; }
/* 走廊带（决定 8）：跨主题引文按主题对聚成带，宽随条数；带端圆点＝两端主题色 */
.band { fill: var(--c-neutral); fill-opacity: 0.28; stroke: var(--canvas); stroke-width: 0.6; }
.band-end { stroke: var(--canvas); stroke-width: 0.8; }
.band-text { font-size: 9.5px; fill: var(--body-strong); font-family: var(--font-mono);
  paint-order: stroke; stroke: var(--surface-soft); stroke-width: 2.5px; stroke-linejoin: round; }
.edge { fill: none; }
.edge-bwd { stroke: #8e8b82; stroke-width: 1.1; }
.edge-fwd { stroke: #8e8b82; stroke-width: 1.1; stroke-dasharray: 4 3; }
.legend { font-size: 12px; color: var(--muted); margin: 10px 0 0; line-height: 2.1; }
.legend .lg { display: inline-block; white-space: nowrap; margin: 0 14px 0 0; }
.legend .lg-wide { white-space: normal; }  /* 年份档一行放四档时允许折行，别把图例顶出容器 */
.legend .kp-num { background: var(--surface-soft); color: var(--ink); font-size: 9px;
  font-weight: 700; line-height: 11px; text-align: center; }
.legend .kp { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin: 0 5px 0 0;
  vertical-align: -1px; border: 1px solid var(--ink); }
.legend .kp-ring { background: var(--canvas); }
.legend .kp-ring.tier-abstract { border-style: dashed; }
.legend .kp-ring.tier-tert { border-style: dotted; border-width: 1.4px; }
.legend .kp-prio { background: var(--accent-strong); }
.legend .kp-band { width: 22px; height: 6px; border-radius: 3px; background: var(--c-neutral);
  border-color: var(--muted); }
.legend .kp-question { color: var(--muted); font-size: 11px; font-weight: 700; text-align: center;
  line-height: 11px; }
.legend .kl { display: inline-block; width: 24px; height: 0; border-top: 1.1px solid #8e8b82;
  vertical-align: 3px; margin: 0 5px 0 0; }
.legend .kl-fwd { border-top-style: dashed; }
.degrade { background: #fff6ef; border: 1px dashed var(--accent); border-radius: var(--radius-md);
  padding: 12px 16px; margin: 0 0 12px; font-size: 13.5px; }
.degrade h3 { margin: 0 0 6px; font-size: 15px; color: var(--accent-active); }
.degrade code { overflow-wrap: anywhere; }
.note { font-size: 12.5px; color: var(--muted); margin: 10px 0 0; }
.card-index { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 10px; margin: 0 0 18px; }
.card-chip { display: block; background: var(--surface-card); border: 1px solid var(--hairline);
  border-radius: var(--radius-md); padding: 10px 12px; font-size: 13px; cursor: pointer; text-align: left;
  font-family: inherit; color: var(--body); text-decoration: none; }
.card-chip:hover { border-color: var(--accent); }
/* 索引主行＝题名（读者靠它认文献），副行＝编号·作者年份·等级（第 7 轮） */
.card-chip .cc-title { font-weight: 500; color: var(--body-strong); display: block; }
.card-chip .cc-who { color: var(--muted); font-size: 12px; display: block; margin-top: 2px; }
.card { background: var(--surface-card); border: 1px solid var(--hairline); border-radius: var(--radius-md);
  margin: 0 0 10px; scroll-margin-top: var(--sticky-stack); }
/* 卡头三段式（第 7 轮）：题名一行、作者年份一行、徽章一行；三角占左侧整列 */
.card > summary { cursor: pointer; padding: 12px 16px; display: grid; grid-template-columns: auto 1fr;
  gap: 3px 10px; align-items: baseline; list-style: none; }
.card > summary::-webkit-details-marker { display: none; }
.card > summary::before { content: "▸"; color: var(--accent-active); margin-right: 2px; grid-row: 1 / span 3; }
.card[open] > summary::before { content: "▾"; }
.card-title { grid-column: 2; font-weight: 600; color: var(--body-strong); }
.card-who { grid-column: 2; font-size: 12.5px; color: var(--muted); }
.card-badges { grid-column: 2; display: inline-flex; flex-wrap: wrap; gap: 6px; }
.card-body { padding: 0 18px 16px; border-top: 1px dashed var(--hairline); }
.backlink { display: inline-block; margin: 10px 0 4px; font-size: 12.5px; }
.card-prio { margin: 8px 0 2px; }
.badge.prio { background: var(--accent-strong); color: var(--on-primary); }
.card-fields { list-style: none; margin: 0; padding: 0; }
.card-rest { margin-top: 8px; border-top: 1px dashed var(--hairline); }
.card-rest > summary { cursor: pointer; color: var(--accent-active); font-size: 13px; padding: 8px 0 4px; }
.card-rest > summary:focus-visible { outline: 3px solid var(--teal); outline-offset: 2px; }
.card-fields > li { padding: 8px 0; border-top: 1px solid var(--hairline-soft); }
.fname { font-size: 11.5px; letter-spacing: 0.5px; text-transform: uppercase; color: var(--muted); margin-bottom: 3px; }
.fbody p { margin: 0 0 6px; font-size: 13.5px; overflow-wrap: anywhere; }
/* 字段体内的两级（第 10 轮）：子块名（.bsub）＋条款（.bitems）——把卡文里已有的 `名称：` 与 `；` 分条显形，
   字与标点内容不改（ADR-0029 边界）。层次＝小节名（.fsec）＞ 段名（.fname）＞ 子块名（.bsub）＞ 正文：
   子块名比段名重（段名淡小、子块名深半粗）、比小节名淡，条款用项目符号、不加序号。 */
.fblock { margin: 0 0 10px; }
.fblock:last-child { margin-bottom: 0; }
.bsub { font-size: 12.5px; font-weight: 600; color: var(--body); margin: 0 0 3px; }
.bsub .bnote { margin-left: 6px; font-size: 12px; font-weight: 400; color: var(--muted); }
.bitems { margin: 4px 0 0; padding-left: 18px; }
.bitems > li { font-size: 13.5px; margin: 3px 0; overflow-wrap: anywhere; }
.subul { margin: 4px 0 0; padding-left: 20px; }
.subul li { font-size: 13px; margin: 3px 0; overflow-wrap: anywhere; }
.field-key .fbody p { color: var(--body-strong); }
/* 身份段逐行（第 7 轮）：题名／作者／来源／DOI 各占一行，第 5 段起归「其他标识与附注」 */
.id-row { display: grid; grid-template-columns: 46px 1fr; gap: 2px 10px; margin: 0 0 4px; }
.id-lab { font-size: 11.5px; color: var(--muted); letter-spacing: 0.5px; }
.id-val { font-size: 13.5px; color: var(--body-strong); overflow-wrap: anywhere; }
.id-tail { margin: 8px 0 0; }
.id-tail ul { margin: 4px 0 0; padding-left: 0; list-style: none; }
.id-tail li { font-size: 12.5px; color: var(--muted); margin: 3px 0; overflow-wrap: anywhere; }
.fnote { margin-left: 8px; font-weight: 400; letter-spacing: 0; text-transform: none; color: var(--muted); }
.rq { margin: 0 0 6px; font-size: 13.5px; color: var(--body-strong); }
/* 等级记号与释义（第 7 轮）：悬停任一处记号出弹层；键盘走每卡的「等级说明」 */
.lv { border-bottom: 1px dotted var(--muted); cursor: help; }
.lv-open { font: inherit; font-size: 11.5px; letter-spacing: 0; text-transform: none; margin-left: 8px;
  border: 1px solid var(--hairline); background: var(--surface-card); border-radius: var(--radius-sm);
  padding: 1px 7px; cursor: pointer; color: var(--accent-active); }
.lv-open:hover { border-color: var(--accent); }
.lv-open:focus-visible { outline: 3px solid var(--teal); outline-offset: 2px; }
.lv-pop { position: fixed; z-index: 80; max-width: 430px; background: var(--surface-card);
  border: 1px solid var(--hairline); border-radius: var(--radius-md); padding: 12px 14px;
  box-shadow: 0 8px 24px rgba(20, 20, 19, 0.16); pointer-events: none; font-size: 12.5px;
  color: var(--body); line-height: 1.55; }
.lv-pop[hidden] { display: none; }
.lv-pop h3 { margin: 0 0 6px; font-size: 13px; color: var(--body-strong); }
.lv-pop ul { margin: 0 0 8px; padding-left: 0; list-style: none; }
.lv-pop li { margin: 3px 0; }
.lv-pop .lv-code { font-weight: 600; color: var(--body-strong); }
.lv-pop ol { margin: 0; padding-left: 18px; }
.level-legend { margin: 10px 0 0; font-size: 12.5px; color: var(--muted); }
.level-legend > summary { cursor: pointer; color: var(--accent-active); font-size: 12.5px; }
.level-legend > summary:focus-visible { outline: 3px solid var(--teal); outline-offset: 2px; }
.level-legend .lv-rows { margin: 8px 0; padding-left: 0; list-style: none; }
.level-legend li { margin: 4px 0; }
.level-legend .lv-code { font-weight: 600; color: var(--body-strong); }
.level-legend ol { margin: 0; padding-left: 18px; }
.field-unknown .fbody p { color: var(--muted); }
.card-sec { padding: 10px 0 0; border-top: 1px solid var(--hairline-soft); }
.fsec { font-size: 12.5px; font-weight: 600; color: var(--body-strong); }
.field-plain .fbody p { color: var(--muted); }
.h2-ft { font-family: var(--font-display); font-size: 17px; font-weight: 500; color: var(--on-dark); margin: 0 0 8px; }
.overlay { position: fixed; inset: 0; background: rgba(20, 20, 19, 0.45); display: none; z-index: 60; }
.overlay.open { display: block; }
.overlay-panel { position: absolute; right: 0; top: 0; bottom: 0; width: min(760px, 94vw);
  background: var(--canvas); box-shadow: -8px 0 24px rgba(20,20,19,0.18); display: flex; flex-direction: column; }
.overlay-head { display: flex; align-items: center; gap: 10px; padding: 12px 18px;
  border-bottom: 1px solid var(--hairline); background: var(--surface-soft); }
.overlay-title { font-weight: 600; color: var(--body-strong); font-size: 14.5px; overflow-wrap: anywhere; }
.overlay-headtext { flex: 1; min-width: 0; }
.overlay-sub { font-size: 12.5px; color: var(--muted); margin-top: 2px; }
.overlay-badges { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.overlay-badges:empty { display: none; }
.overlay-close { font: inherit; font-size: 14px; border: 1px solid var(--hairline); background: #fff;
  border-radius: var(--radius-sm); padding: 3px 12px; cursor: pointer; }
.overlay-body { overflow: auto; padding: 4px 18px 28px; }
footer { background: var(--surface-dark); color: var(--on-dark-soft); padding: 28px 0 40px; font-size: 12.5px; }
footer code { background: #2a2824; border-color: #2a2824; color: var(--on-dark-soft); }
footer .fp { font-family: var(--font-mono); font-size: 11.5px; overflow-wrap: anywhere; }
footer .review-fold > summary { cursor: pointer; color: var(--on-dark); margin: 0 0 10px; }
footer .review-fold[open] > summary { margin-bottom: 14px; }
@media print {
  body { background: #fff; }
  /* 打印白名单（第 6 轮 P2）：屏上装置逐个挂 `screen-only` 退场——打印件是只读装置，
     不是屏幕的镜像；以后新增屏上控件只需挂类，不必再来改 print 块。 */
  .screen-only { display: none !important; }
  /* 卡片折叠三角在纸上无意义（summary 只留标题文字）。 */
  details.card > summary::marker, details.card-rest > summary::marker { content: ""; }
  details.card > summary::-webkit-details-marker,
  details.card-rest > summary::-webkit-details-marker { display: none; }
  .fp-list { columns: 2; }
  .topnav, .controls, .overlay, .skip, .erow-actions, .card-chip { display: none !important; }
  .hero { background: #fff; color: #000; padding: 0 0 12px; border-bottom: 2px solid #000; }
  .hero .answer .lab { color: #000; }
  .hero .q, .stat { background: #f2f2f2; color: #000; }
  /* 行卡米底压平：不压平的话每行印一条约 1,100×300px 浅底，64 页变灰块（第 5 轮 minor） */
  .erow, .erow-more { background: #fff; }
  /* 先读行同理：浅底压平成白＋发丝线；「其余层」在纸上自动展开，折叠控件（summary 与三角）退场——
     纸面不出现任何只能点／只能滑／只能勾的东西。 */
  .prio-item { background: #fff; }
  .prio-fold > summary { display: none; }
  .hero .meta, .hero .q strong, .stat .lab, .hero .answer { color: #000; }
  .stat .num { color: #000; }
  /* 链接色：`.hero a`／`footer a`（0,1,1）与 `.hero .answer a`（0,2,1）会盖过 `a`（0,0,1），
     深底上的珊瑚色落到白纸上只有 3.28:1——打印里必须逐个压平，不能只写 `a`。 */
  a, .hero a, .hero .answer a, footer a, .prio-title { color: #000; text-decoration: underline; }
  /* 页脚标题自带 `color: var(--on-dark)`，靠继承改不动，得点名。 */
  .h2-ft { color: #000; }
  .h2 { break-before: page; break-after: avoid; }
  .card, .erow, .table-wrap, .fblock { break-inside: avoid; }
  .erow-more[hidden] { display: none; }
  /* 深底反白是引用锚点（证据行号／等级／先读名次）的唯一载体，而导出 PDF 默认不印背景图形，
     反白会整块消失——与 .hero 在打印里改成白底同理，这里换成描边 chip，黑白与彩色打印都成立。 */
  .rownum, .badge.level, .badge.prio, .badge.coral, .prio-rank {
    background: none; color: #000; border: 1px solid #000; }
  .h2 > a.secref { text-decoration: none; }  /* 章节号是标题的一部分，不是链接 */
  .legend .kp, .band-end, mark { print-color-adjust: exact; -webkit-print-color-adjust: exact; }
  /* 搜索高亮也得印出来：归档件上"为什么这行命中"同样是信息。 */
  .graph { background: #fff; }
  .graph-scroll { overflow: visible; }
  footer { background: #fff; color: #000; }
  footer .review-fold > summary { display: none; }
  footer code { background: none; color: #000; }
}
@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  * { transition: none !important; animation: none !important; }
}
"""


PAGE_JS = r"""
(function () {
  "use strict";
  var $ = function (s, el) { return (el || document).querySelector(s); };
  var $$ = function (s, el) { return Array.prototype.slice.call((el || document).querySelectorAll(s)); };

  /* ---------- 等级释义（第 7 轮）：悬停任一处等级记号出弹层；键盘走每卡的「等级说明」 ---------- */
  var lvPop = document.getElementById("lv-pop");
  var lvTimer = null, lvAnchor = null;
  function lvPlace(el) {
    var r = el.getBoundingClientRect();
    var left = Math.max(8, Math.min(r.left, window.innerWidth - lvPop.offsetWidth - 8));
    var top = r.bottom + 8;
    if (top + lvPop.offsetHeight > window.innerHeight - 8) top = Math.max(8, r.top - lvPop.offsetHeight - 8);
    lvPop.style.left = left + "px";
    lvPop.style.top = top + "px";
  }
  function lvShow(el) {
    if (!lvPop) return;
    lvAnchor = el;
    lvPop.hidden = false;   // 同一任务里量宽高并定位，不发生中间绘制
    lvPlace(el);
    $$(".lv-open").forEach(function (b) { b.setAttribute("aria-expanded", b === el ? "true" : "false"); });
  }
  function lvHide() {
    if (!lvPop || lvPop.hidden) return;
    lvPop.hidden = true;
    lvAnchor = null;
    $$(".lv-open").forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
  }
  function lvDelayed(ms) {
    // 焦点仍停在记号或入口上时不收（鼠标移开就把键盘读者的释义收掉是两套输入的冲突）；
    clearTimeout(lvTimer);
    lvTimer = setTimeout(function () {
      var active = document.activeElement;
      if (active && active.closest && active.closest("[data-lv], .lv-open")) return;
      lvHide();
    }, ms);
  }
  if (lvPop) {
    document.addEventListener("mouseover", function (e) {
      var t = e.target.closest("[data-lv]");
      if (!t) return;
      clearTimeout(lvTimer);
      lvShow(t);
    });
    document.addEventListener("mouseout", function (e) {
      if (e.target.closest("[data-lv]")) lvDelayed(160);
    });
    document.addEventListener("focusin", function (e) {
      var t = e.target.closest("[data-lv], .lv-open");
      if (t) { clearTimeout(lvTimer); lvShow(t); }
    });
    document.addEventListener("focusout", function (e) {
      if (e.target.closest("[data-lv], .lv-open")) lvDelayed(120);
    });
    document.addEventListener("click", function (e) {
      var b = e.target.closest(".lv-open");
      if (b) { e.preventDefault(); if (lvPop.hidden) { lvShow(b); } else { lvHide(); } return; }
      // 记号本身不是控件：它落在链接或按钮里时让那些元素照常干活，只保留悬停释义
      var t = e.target.closest("[data-lv]");
      if (t && !t.closest("a, button")) { lvPop.hidden ? lvShow(t) : lvHide(); }
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !lvPop.hidden) {
        e.stopPropagation();   // 释义开着时 ESC 先收释义，别顺手把抽屉也关了
        lvHide();
        if (lvAnchor && lvAnchor.classList && lvAnchor.classList.contains("lv-open")) lvAnchor.focus();
      }
    }, true);
    window.addEventListener("scroll", function () { if (!lvPop.hidden && lvAnchor) lvPlace(lvAnchor); },
                            { passive: true });
  }

  /* ---------- 提取卡弹层：三通道关闭（✕／ESC／返回链）＋焦点管理 ---------- */
  var lastOpener = null;
  var savedScrollY = 0;
  function setBackgroundInert(on) {
    /* aria-modal 说"背后不可达"就得真的不可达：把 body 下除弹层以外的东西统统 inert
       （包含跳过链接——它挂在 body 上，不在 main 里，只点四个区块名会漏掉它）。
       inert 让背景既不可聚焦也不进 AT；老引擎没有 inert 时退回下面那个 Tab 循环兜底。 */
    /* 抽屉开着还能滚背景＝关掉后回到的不是原处（27.8k 长页，第 5 轮 P2）：开时锁 html 滚动并记位置，
       关时解锁并复位；复位用 instant，别让页面的 scroll-behavior:smooth 把读者慢慢送回去。 */
    if (on) {
      savedScrollY = window.scrollY;
      document.documentElement.classList.add("overlay-open");
    } else {
      document.documentElement.classList.remove("overlay-open");
      window.scrollTo({ top: savedScrollY, behavior: "instant" });
    }
    Array.prototype.forEach.call(document.body.children, function (el) {
      if (el.id === "overlay" || el.id === "lv-pop") return;  // 释义弹层不随抽屉一起 inert
      try { el.inert = !!on; } catch (e) {}
    });
  }
  function overlayOpen(slug, opener) {
    var card = document.getElementById("card-" + slug);
    if (!card) return;
    var panel = $("#overlay-body"), head = $("#overlay-title");
    var body = $(".card-body", card);
    var clone = body.cloneNode(true);
    panel.innerHTML = "";
    bindCloseOverlay(clone);
    panel.appendChild(clone);
    // 抽屉头与卡头同源（第 7 轮）：题名作可访问名，作者年份与徽章各一行；不再另造一套标题
    var titleEl = $(".card-title", card), whoEl = $(".card-who", card), badgesEl = $(".card-badges", card);
    head.textContent = titleEl ? titleEl.textContent : (whoEl ? whoEl.textContent : slug);
    var sub = $("#overlay-sub");
    if (sub) sub.textContent = whoEl ? whoEl.textContent : "";
    var badgeBox = $("#overlay-badges");
    if (badgeBox) {
      badgeBox.innerHTML = badgesEl ? badgesEl.innerHTML : "";
      bindCloseOverlay(badgeBox);
    }
    $("#overlay").classList.add("open");
    setBackgroundInert(true);
    lastOpener = opener || null;
    $(".overlay-close").focus();
    if (history.replaceState) history.replaceState(null, "", "#card-" + slug);
  }
  function bindCloseOverlay(root) {
    $$("[data-close-overlay]", root).forEach(function (a) {
      a.addEventListener("click", function (e) {
        e.preventDefault();
        overlayClose(a.getAttribute("href"));
      });
    });
    $$("a[href^='#row-']", root).forEach(function (a) {
      if (a.getAttribute("data-close-overlay")) return;
      a.setAttribute("data-close-overlay", "1");
      a.addEventListener("click", function (e) {
        e.preventDefault();
        overlayClose(a.getAttribute("href"));
      });
    });
  }
  function overlayClose(targetHash) {
    $("#overlay").classList.remove("open");
    $("#overlay-body").innerHTML = "";
    lvHide();  // 释义弹层可能挂在新清空的抽屉里，随抽屉一起收
    setBackgroundInert(false);
    if (lastOpener && lastOpener.focus) { try { lastOpener.focus({ preventScroll: true }); } catch (e) {} }
    if (targetHash) {
      var t = document.getElementById(targetHash.slice(1));
      if (t) {
        t.scrollIntoView({ block: "center" });
        t.classList.add("flash");
        setTimeout(function () { t.classList.remove("flash"); }, 1700);
      }
    }
    if (history.replaceState) history.replaceState(null, "", location.pathname + location.search);
    lastOpener = null;
  }
  function openCard(slug, opener) { overlayOpen(slug, opener); }

  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-open-card]");
    if (t) { e.preventDefault(); openCard(t.getAttribute("data-open-card"), t); return; }
    var s = e.target.closest("[data-scroll-row]");
    if (s) {
      e.preventDefault();
      var row = document.getElementById("row-" + s.getAttribute("data-scroll-row"));
      if (row) {
        row.scrollIntoView({ block: "center" });
        row.classList.add("flash");
        setTimeout(function () { row.classList.remove("flash"); }, 1700);
      }
    }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && $("#overlay").classList.contains("open")) { overlayClose(null); return; }
    if ((e.key === "Enter" || e.key === " ") && e.target.closest && e.target.closest("[data-open-card],[data-scroll-row]")) {
      var t = e.target.closest("[data-open-card],[data-scroll-row]");
      if (t.tagName && t.tagName.toLowerCase() === "a" && t.getAttribute("href")) return;  // 锚点走原生行为
      e.preventDefault();
      /* SVG 元素无 .click()：派发 MouseEvent（02 缺陷 2） */
      t.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    }
  });
  $("#overlay").addEventListener("click", function (e) { if (e.target.id === "overlay") overlayClose(null); });
  $(".overlay-close").addEventListener("click", function () { overlayClose(null); });
  $("#overlay").addEventListener("wheel", function (e) {
    if (e.target.closest && e.target.closest(".overlay-body")) return;
    e.preventDefault();  // 遮罩上滚轮只该滚抽屉或什么都不滚，不该带走背后的页面
  }, { passive: false });
  document.addEventListener("keydown", function (e) {
    /* 没有 inert 的引擎：自己把 Tab 圈在面板里，别把键盘读者丢到遮罩后面 */
    /* 不管引擎有没有 inert 都把 Tab 圈在面板里：否则最后一个元素之后再按 Tab 会掉到 body
       上，焦点环消失一次（第 6 轮 minor）。 */
    if (e.key !== "Tab" || !$("#overlay").classList.contains("open")) return;
    var focusable = $$('a[href], button, input, select, summary, [tabindex]:not([tabindex="-1"])',
                       $("#overlay-panel"));
    if (!focusable.length) return;
    var first = focusable[0], last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });

  /* ---------- 证据表：筛选 / 排序 / 分组 / 搜索 ---------- */
  var state = { themes: new Set(), levels: new Set(), status: new Set(), card: new Set(), q: "" };
  function visible(el) {
    var th = (el.getAttribute("data-themes") || "").split(",");
    if (state.themes.size && !th.some(function (t) { return state.themes.has(t); })) return false;
    if (state.levels.size && !state.levels.has(el.getAttribute("data-level"))) return false;
    if (state.status.size && !state.status.has(el.getAttribute("data-status"))) return false;
    if (state.card.size && !state.card.has(el.getAttribute("data-card"))) return false;
    if (state.q && el.textContent.toLowerCase().indexOf(state.q) === -1) return false;
    return true;
  }
  /* ---------- 命中揭示：命中藏在折叠列里就替读者展开，并把命中处标出来 ---------- */
  var autoExpanded = [], hitMarks = [];
  function clearMarks() {
    var parents = [];
    hitMarks.forEach(function (m) {
      var parent = m.parentNode;
      if (!parent) return;
      parent.replaceChild(document.createTextNode(m.textContent), m);
      if (parents.indexOf(parent) === -1) parents.push(parent);
    });
    hitMarks = [];
    /* 把被 mark 切开的文本节点并回一个：不并，跨接缝的词就再也匹配不上——
       逐字输入时每个键都切一次，第二次起就一个高亮都出不来。 */
    parents.forEach(function (parent) { if (parent.normalize) parent.normalize(); });
  }
  function collapseAuto() {
    autoExpanded.forEach(function (row) {
      var more = $(".erow-more", row), btn = $(".expand", row);
      if (more) more.setAttribute("hidden", "");
      if (btn) { btn.setAttribute("aria-expanded", "false"); btn.textContent = "展开全部字段"; }
    });
    autoExpanded = [];
  }
  function visibleText(row) {
    return $$(".erow-head, .erow-badges, .erow-rel", row)
      .map(function (el) { return el.textContent; }).join(" ").toLowerCase();
  }
  function markHits(row, needle) {
    var walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT, null), nodes = [], node;
    while ((node = walker.nextNode())) {
      if (node.nodeValue.toLowerCase().indexOf(needle) !== -1) nodes.push(node);
    }
    nodes.forEach(function (t) {
      var lower = t.nodeValue.toLowerCase(), frag = document.createDocumentFragment();
      var from = 0, at, mark;
      while ((at = lower.indexOf(needle, from)) !== -1) {
        if (at > from) frag.appendChild(document.createTextNode(t.nodeValue.slice(from, at)));
        mark = document.createElement("mark");
        mark.textContent = t.nodeValue.slice(at, at + needle.length);
        frag.appendChild(mark);
        hitMarks.push(mark);
        from = at + needle.length;
      }
      if (from < t.nodeValue.length) frag.appendChild(document.createTextNode(t.nodeValue.slice(from)));
      t.parentNode.replaceChild(frag, t);
    });
  }
  function applyFilters() {
    clearMarks();     // 先拆上一轮的标记，否则 textContent 会被自己插的 mark 污染
    collapseAuto();   // 也先收回上一轮替读者展开的行
    var all = $$(".erow"), n = 0;
    all.forEach(function (el) {
      var ok = visible(el);
      el.classList.toggle("hide", !ok);
      if (!ok) return;
      n++;
      if (!state.q) return;
      if (visibleText(el).indexOf(state.q) === -1) {  // 命中只在折叠列里：展开，让理由看得见
        var more = $(".erow-more", el), btn = $(".expand", el);
        if (more) more.removeAttribute("hidden");
        if (btn) { btn.setAttribute("aria-expanded", "true"); btn.textContent = "收起全部字段"; }
        autoExpanded.push(el);
      }
      markHits(el, state.q);
    });
    var c = $("#visible-count");
    if (c) c.textContent = n + " / " + all.length + " 篇";
    var empty = $("#empty-state");
    if (empty) empty.toggleAttribute("hidden", n !== 0);
    regroup();   // 分组标题随筛选重算（P2）
    keepControlsInView();
  }
  /* 结果收缩会把粘性筛选条推出视口（第 6 轮 P1：读者点完 chip 就失去计数与筛选态）：
     收缩后条已不在粘性位就把它带回粘性位——只补差额，不做整节跳转。 */
  function keepControlsInView() {
    var bar = $(".controls");
    if (!bar) return;
    var after = bar.getBoundingClientRect();
    var sticky = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--sticky-stack")) || 0;
    if (after.bottom >= sticky) return;          // 仍在粘性位（含条本来就在视口里），不动
    window.scrollTo({ top: Math.max(0, window.scrollY - (sticky - after.bottom + 8)), behavior: "instant" });
  }
  $$(".chip[data-facet]").forEach(function (chip) {
    chip.addEventListener("click", function () {
      var set = state[chip.getAttribute("data-facet")], val = chip.getAttribute("data-value");
      if (set.has(val)) { set.delete(val); chip.setAttribute("aria-pressed", "false"); }
      else { set.add(val); chip.setAttribute("aria-pressed", "true"); }
      applyFilters();
    });
  });
  var q = $("#filter-q");
  if (q) q.addEventListener("input", function () { state.q = q.value.trim().toLowerCase(); applyFilters(); });
  var clearBtn = $("#filter-clear");
  if (clearBtn) clearBtn.addEventListener("click", function () {
    state.themes.clear(); state.levels.clear(); state.status.clear(); state.card.clear();
    state.q = "";
    if (q) q.value = "";
    $$(".chip[data-facet]").forEach(function (chip) { chip.setAttribute("aria-pressed", "false"); });
    applyFilters();
  });
  var sortSel = $("#sort-mode");
  if (sortSel) sortSel.addEventListener("change", function () { sortRows(sortSel.value); });
  function sortRows(mode) {
    var list = $("#erow-list");
    var rows = $$(".erow", list);
    rows.sort(function (a, b) {
      if (mode === "cites") return (+b.getAttribute("data-cites")) - (+a.getAttribute("data-cites"));
      if (mode === "level") {
        var la = a.getAttribute("data-level"), lb = b.getAttribute("data-level");
        if (la === lb) return (+a.getAttribute("data-row")) - (+b.getAttribute("data-row"));
        if (la === "未评") return 1;
        if (lb === "未评") return -1;
        return la < lb ? -1 : 1;
      }
      return (+a.getAttribute("data-row")) - (+b.getAttribute("data-row"));
    });
    rows.forEach(function (r) { list.appendChild(r); });
    regroup();
  }
  var groupSel = $("#group-mode");
  if (groupSel) groupSel.addEventListener("change", regroup);
  function regroup() {
    var list = $("#erow-list");
    $$(".grouphead", list).forEach(function (h) { h.remove(); });
    if (!groupSel || groupSel.value !== "theme") return;
    var heads = {}, order = [], labelsByKey = {};
    $$(".erow", list).forEach(function (r) {
      if (r.classList.contains("hide")) return;   // 标题按可见行派生：零行分组不再渲染标题（P2）
      var key = (r.getAttribute("data-themes") || "").split(",")[0] || "无";
      if (!labelsByKey[key]) labelsByKey[key] = (r.getAttribute("data-theme-labels") || "").split(",")[0] || "";
      if (!heads[key]) { heads[key] = []; order.push(key); }
      heads[key].push(r);
    });
    order.sort();
    order.forEach(function (key) {
      var h = document.createElement("h3");
      h.className = "grouphead";
      var nm = labelsByKey[key] || "";
      h.textContent = key === "无" ? "主题：未落主题" : (nm ? "§" + key + " " + nm : "主题 §" + key);
      list.appendChild(h);
      heads[key].forEach(function (r) { list.appendChild(r); });
    });
  }
  $$(".erow .expand").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var more = $(".erow-more", btn.closest(".erow"));
      var open = more.hasAttribute("hidden");
      if (open) more.removeAttribute("hidden"); else more.setAttribute("hidden", "");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      btn.textContent = open ? "收起全部字段" : "展开全部字段";
    });
  });

  /* ---------- 打印：行七列默认全展；卡片由开关控制 ---------- */
  var printCards = $("#print-expand");
  var openedByPrint = [], rowsByPrint = [];
  window.addEventListener("beforeprint", function () {
    openedByPrint = []; rowsByPrint = [];
    $$(".erow-more[hidden]").forEach(function (m) { m.removeAttribute("hidden"); rowsByPrint.push(m); });
    var reviewFold = document.querySelector("details.review-fold");
    if (reviewFold && !reviewFold.open) { reviewFold.open = true; openedByPrint.push(reviewFold); }
    // 先读「其余层」在纸上展开（summary 与三角随 print 退场）：归档件里名单是完整的
    var prioFold = document.getElementById("prio-fold");
    if (prioFold && !prioFold.open) { prioFold.open = true; openedByPrint.push(prioFold); }
    // 纸上没有悬停（第 7 轮）：等级含义表在打印件里展开一次，复核者翻到筛选条下就有完整释义
    var legend = document.getElementById("level-legend");
    if (legend && !legend.open) { legend.open = true; openedByPrint.push(legend); }
    if (printCards && printCards.checked) {
      $$("details.card, details.card-rest").forEach(function (d) { if (!d.open) { d.open = true; openedByPrint.push(d); } });
    }
  });
  window.addEventListener("afterprint", function () {
    openedByPrint.forEach(function (d) { d.open = false; });
    rowsByPrint.forEach(function (m) { m.setAttribute("hidden", ""); });
    openedByPrint = []; rowsByPrint = [];
  });

  /* ---------- 校验信息：正文里的「见校验信息」要能直达（折叠着就先展开） ---------- */
  var reviewFold = document.querySelector("details.review-fold");
  if (reviewFold) {
    var openReview = function () {
      if (location.hash === "#review" || location.hash.indexOf("#file-") === 0) { reviewFold.open = true; }
    };
    openReview();
    window.addEventListener("hashchange", openReview);
    document.addEventListener("click", function (event) {
      var link = event.target.closest && event.target.closest("a.fileref");
      if (link) { reviewFold.open = true; }
    }, true);
  }

  /* ---------- 关系图：默认只画本次文献点，相邻文献层按需展开（ADR-0030 决定 10） ---------- */
  var foldBtn = $("#graph-fold");
  if (foldBtn) {
    var foldNote = $("#graph-fold-note"), layers = $$('g[data-layer="neigh"]');
    foldBtn.addEventListener("click", function () {
      /* 两态位置恒定：折叠＝纯缩放，只换 viewBox——重排过的坐标一个不动 */
      var expand = foldBtn.getAttribute("aria-expanded") === "false";  /* 首帧＝收起 */
      layers.forEach(function (g) { g.classList.toggle("folded", !expand); });
      var svg = $("#graph-layers");
      if (svg) svg.setAttribute("viewBox", expand ? svg.getAttribute("data-viewbox-all")
                                                  : svg.getAttribute("data-viewbox-main"));
      foldBtn.setAttribute("aria-expanded", expand ? "true" : "false");
      foldBtn.textContent = expand ? "收起相邻文献" : "展开相邻文献";
      if (foldNote) foldNote.toggleAttribute("hidden", expand);  /* 收起时才显示「已收起 N 个…」 */
    });
  }

  /* ---------- 导航 scroll-spy ---------- */
  var navLinks = $$(".topnav a[href^='#']");
  var targets = navLinks.map(function (a) { return document.getElementById(a.getAttribute("href").slice(1)); }).filter(Boolean);
  if ("IntersectionObserver" in window && targets.length) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) {
          navLinks.forEach(function (a) { a.classList.toggle("active", a.getAttribute("href") === "#" + en.target.id); });
        }
      });
    }, { rootMargin: "-90px 0px -70% 0px", threshold: 0 });
    targets.forEach(function (t) { io.observe(t); });
  }
  /* ---------- 粘性装置实测：锚点余量按真实高度写，不用写死的常数 ----------
     顶栏在 901–1075px 会折成两行、320px 折成四行，写死一个值就等于把标题送进栏底。 */
  function syncStickyMetrics() {
    var nav = document.querySelector(".topnav"), ctl = document.querySelector("#s5 .controls");
    if (!nav) return;
    var navH = Math.round(nav.getBoundingClientRect().height);
    document.documentElement.style.setProperty("--nav-h", navH + "px");
    var pinned = ctl && getComputedStyle(ctl).position !== "static";
    document.documentElement.style.setProperty(
      "--sticky-stack", (navH + (pinned ? Math.round(ctl.getBoundingClientRect().height) : 0)) + "px");
  }
  syncStickyMetrics();
  if (window.ResizeObserver) {
    var stickyRO = new ResizeObserver(syncStickyMetrics);
    [".topnav", "#s5 .controls"].forEach(function (sel) {
      var el = document.querySelector(sel);
      if (el) stickyRO.observe(el);
    });
  }
  window.addEventListener("resize", syncStickyMetrics);
  window.addEventListener("orientationchange", syncStickyMetrics);
  if (document.fonts && document.fonts.ready && document.fonts.ready.then) {
    document.fonts.ready.then(syncStickyMetrics);   // 字体就位后顶栏行高可能变
  }

  /* 冷启动深链：从 IM 点开 #card-… 的读者该直接看到那张卡，而不是一条折叠行。 */
  function openFromHash() {
    var m = (location.hash || "").match(/^#card-(.+)$/);
    if (!m) return;
    var slug = decodeURIComponent(m[1]);
    if (document.getElementById("card-" + slug)) openCard(slug, null);
  }
  window.addEventListener("hashchange", openFromHash);
  openFromHash();
  applyFilters();
})();
"""




# ---------------------------------------------------------------- 页面装配


def section_index(sections) -> list:
    ids = []
    for title, body in sections:
        m = re.match(r"(?:§\s*)?(\d+)", title.strip())
        if m:
            ids.append(f"s{m.group(1)}")
        for sub_title, _lines in split_subs(body)[1:]:
            sid = sub_anchor(sub_title)
            if sid:
                ids.append(sid)
    return ids


# `文献` 的两条分档路径：模板形态按 `｜` 分四段；本仓多数包写成一条 GB/T 7714 引文，
# 按引文自身的「[J]. 期刊, 年, 卷(期): 页. DOI …」兜底拆档。DOI 由 `.t-doi` 单独成链呈现一次。
LIT_DOI_TAIL_RE = re.compile(r"\s*\.?\s*DOI\s+10\.\S+\s*$", re.I)
LIT_GB_T_RE = re.compile(r"^(?P<head>.+?\[[A-Za-z]+\]\.)\s*(?P<venue>[^,]+?)\s*,\s*"
                         r"(?P<year>(?:19|20)\d{2})\s*(?P<tail>[^.]*)")


def lit_parts(lit: str) -> tuple:
    """`文献` → (主行, 年份, 期刊, 卷页)；拆不出年份档位时年份位为空串。

    主行＝「作者. 题名[J].」，年份／期刊／卷页进副行（`.t-authors` 粗体、`.t-meta` 弱化）。
    空档由调用方决定不渲染：`.t-meta` 无条件渲染时，拆不出档位就在每一行留下一个孤立的「·」。
    """
    text = (lit or "").strip()
    parts = [part.strip() for part in text.split("｜")]
    if len(parts) > 1:
        return (parts[0], parts[1], parts[2] if len(parts) > 2 else "",
                " ｜ ".join(parts[3:]) if len(parts) > 3 else "")
    stripped = LIT_DOI_TAIL_RE.sub("", text)
    m = LIT_GB_T_RE.match(stripped)
    if not m:
        return (stripped, "", "", "")
    return (m.group("head"), m.group("year"), m.group("venue").strip(),
            m.group("tail").strip().strip(",;").strip())


def lit_full(lit: str) -> str:
    """七列「文献」列的全引文：模板形态按四段重排，GB/T 单条则原文去 DOI 尾。

    DOI 已在 `.t-doi` 成链出现一次；引文里再留一份，每行就多一个读者要跳过的重复串。
    """
    text = (lit or "").strip()
    if "｜" in text:
        return " ｜ ".join(part for part in lit_parts(text) if part)
    return LIT_DOI_TAIL_RE.sub("", text).strip()


def research_question(package_md: str) -> str:
    body = package_parse.find_h2(package_md, "研究问题") or ""
    for line in body.splitlines():
        m = re.search(r"研究问题[^：:]*[：:]\s*(.+)$", line)
        if m:
            return m.group(1).strip().strip("*")
    return next((re.sub(r"^-\s*", "", line.strip()) for line in body.splitlines() if line.strip()), "")


def core_excerpt(package_md: str) -> str:
    """答案先行摘录：§6.2 首段起、到第一条「（行 N）」收（机器抽取，不手写）。

    子标题的 `§` 前缀与正文里的读者面写法（「证据行 N」）都认——这两种形态正是本渲染器自己
    产出的（`split_subs` 出来的子标题、`Ctx.inline` 认的行引用）。匹配比它们更窄＝摘录静默为空，
    而首屏的答案先行块是渲染义务，不是可选装饰。
    """
    body = package_parse.find_h2(package_md, "结论") or ""
    sub = next((lines for title, lines in split_subs(body)[1:]
                if re.match(r"(?:§\s*)?6\.2", title.strip())), None)
    if sub is None:
        return ""
    para = " ".join(line.strip() for line in sub
                    if line.strip() and not line.strip().startswith(("-", "|", "#")))
    m = re.search(r".{20,420}?（(?:文献|证据行|行)[^）]*）。", para)
    return m.group(0) if m else para[:260] + ("…" if len(para) > 260 else "")


def build_row_meta(rows, refs, cards, row_cards, themes, status_of, cites_map, prio_rank=None,
                   years_map=None) -> dict:
    meta = {}
    prio_rank = prio_rank or {}
    years_map = years_map or {}
    for row_no, row in rows.items():
        doi = refs.get(row_no, "")
        cids = row_cards.get(row_no) or []
        card = cards.get(cids[0]) if cids else None
        level = re.search(r"E[1-5]", row.get("设计") or "")
        meta[row_no] = {"doi": doi, "level": level.group(0) if level else "未评",
                        "status": status_of(doi, row.get("设计"),
                                            card["_status"] if card else ""), "cites": cites_map.get(doi),
                        "year": years_map.get(doi),  # 先读行副行的年份（可降级件：缺记 ∅）
                        "theme": theme_of_row(row, card, themes), "card": card,
                        "prio": prio_rank.get(str(row_no))}
    return meta


def hero_html(manifest, package_md, ctx, stats, route_label, question, answer) -> str:
    topic = manifest.get("topic") or {}
    title = next((line[2:].strip() for line in package_md.splitlines() if line.startswith("# ")),
                 str(topic.get("slug") or ""))
    caliber = str((manifest.get("caliber") or {}).get("grey_gate_version") or MISSING)
    caliber_word = CALIBER_WORDS.get(caliber)
    route_word = ROUTE_WORDS.get(str(route_label))
    meta = (f"交付 {topic.get('delivered') or MISSING} ｜ "
            f"检索路线 {route_word or route_label} ｜ "
            f"渠道 {caliber_word or caliber}")
    stat_html = "".join(f'<div class="stat" title="{attresc(STAT_NOTES.get(label, ""))}">'
                        f'<div class="num">{esc(str(value))}</div>'
                        f'<div class="lab">{esc(label)}</div></div>' for label, value in stats)
    return f"""<header class="hero" id="top">
<div class="wrap">
<h1>{ctx.inline(title)}</h1>
<p class="meta">{esc(meta)}</p>
<div class="q"><strong>研究问题：</strong>{ctx.inline(question)}</div>
<div class="answer"><span class="lab">先给结论（摘自 §6.2）</span>{ctx.inline(answer)}</div>
<div class="stat-row">{stat_html}</div>
</div>
</header>"""


def generic_section(ctx, title, body, sid, pre_note: str = "") -> str:
    subs = split_subs(body)
    html = [f'<section class="section" id="{sid}"><h2 class="h2" id="{sid}-h">{ctx.inline(title)}</h2>']
    if pre_note:
        html.append(pre_note)
    html.append(ctx.blocks(subs[0][1]))
    for sub_title, sub_lines in subs[1:]:
        anchor = sub_anchor(sub_title)
        attr = f' id="{anchor}"' if anchor else ""
        html.append(f'<h3 class="h3"{attr}>{ctx.inline(sub_title)}</h3>')
        html.append(ctx.blocks(sub_lines))
    html.append("</section>")
    return "\n".join(html)


def split_table(body_lines):
    """§5 段切分：表前散文、表本体、表后散文。"""
    start = next((i for i, line in enumerate(body_lines) if line.strip().startswith("|")), None)
    if start is None:
        return body_lines, [], []
    end = start
    while end < len(body_lines) and body_lines[end].strip().startswith("|"):
        end += 1
    return body_lines[:start], body_lines[start:end], body_lines[end:]


def row_card_html(ctx, row_no, row, meta) -> str:
    head, year, venue, rest = lit_parts(row["文献"])
    meta_line = " · ".join(part for part in (year, venue, rest) if part)
    doi = meta["doi"]
    doi_line = (f'<a href="https://doi.org/{attresc(doi)}">DOI {esc(doi)}</a>'
                if doi else f'DOI {MISSING}')
    card = meta["card"]
    badges = [f'<span class="badge level" data-lv="{attresc(meta["level"])}">{esc(meta["level"])}</span>',
              f'<span class="badge {"status-ok" if meta["status"] == "有全文" else "status-miss"}">'
              f'{esc(meta["status"])}</span>']
    if meta["cites"] is not None:
        badges.append(f'<span class="badge neutral">被引 {meta["cites"]}</span>')
    theme_key = meta["theme"]
    theme_label = ctx.theme_label(theme_key) if theme_key else ""
    theme_target = ctx.sec_anchor(theme_key) if theme_key else ""
    if theme_label and theme_target:
        theme_html = (f'<a class="secref" href="#{theme_target}" '
                      f'title="§{attresc(theme_key)} {attresc(theme_label)}">{esc(theme_label)}</a>')
    elif theme_key:
        theme_html = f'§{esc(theme_key)}'
    else:
        theme_html = "未落主题"
    badges.append('<span class="badge neutral">主题 · ' + theme_html + '</span>')
    card_link = (f'<a class="btn cardlink" href="#card-{attresc(card["slug"])}" '
                 f'data-open-card="{attresc(card["slug"])}">打开文献卡</a>') if card else \
        '<span class="btn disabled">无文献卡（仅题录）</span>'
    cells = [("文献", lit_full(row["文献"])), ("设计与等级", row["设计"]),
             ("人群", row["人群"]), ("干预与暴露", row["干预"]), ("结局与效应量", row["结局"]),
             ("偏倚备注", row["偏倚"]), ("与结论的关联", row["关联"])]
    more = "".join(f'<div class="cell"><div class="cell-name">{esc(name)}</div>'
                   f'<div class="cell-body">{ctx.inline(text)}</div></div>' for name, text in cells)
    excerpt = row["关联"]
    excerpt = excerpt[:220] + ("…" if len(excerpt) > 220 else "")
    return (f'<article class="erow" id="row-{row_no}" data-row="{row_no}" data-level="{attresc(meta["level"])}" '
            f'data-status="{attresc(meta["status"])}" data-card="{1 if card else 0}" '
            f'data-themes="{attresc(meta["theme"])}" '
            f'data-theme-labels="{attresc(ctx.theme_label(meta["theme"]))}" '
            f'data-cites="{meta["cites"] if meta["cites"] is not None else -1}">'
            f'<div class="erow-head"><span class="rownum">{ROW_LABEL} {row_no}</span>'
            f'<div class="erow-title"><div class="t-authors">{ctx.inline(head)}</div>'
            + (f'<div class="t-meta">{ctx.inline(meta_line)}</div>' if meta_line else "")
            + f'<div class="t-doi">{doi_line}</div></div></div>'
            f'<div class="erow-badges">{"".join(badges)}</div>'
            f'<div class="erow-rel">{ctx.inline(excerpt)}</div>'
            f'<div class="erow-actions">{card_link}'
            f'<button class="btn expand" type="button" aria-expanded="false">展开全部字段</button></div>'
            f'<div class="erow-more" hidden>{more}</div></article>')


def evidence_section(ctx, title, body, sid, rows, meta, themes) -> str:
    subs = split_subs(body)
    pre, _table, post = split_table(subs[0][1])
    levels = sorted({meta[row_no]["level"] for row_no in rows})
    statuses = [value for value in STATUS_VALUES if any(meta[row_no]["status"] == value for row_no in rows)]
    chips = []
    for key, label in themes:
        # 读者不该为了点一个主题先去解码 §4.x（第 5 轮 P1）：chip 正文写主题名，章节号留给悬停提示。
        chips.append(f'<button class="chip" data-facet="themes" data-value="{attresc(key)}" '
                     f'aria-pressed="false">§{esc(key)} {esc(ctx.theme_label(key))}</button>')
    chips.append('<span class="sep"></span><span class="lab">等级</span>')
    chips += [f'<button class="chip" data-facet="levels" data-value="{attresc(level)}" '
              f'data-lv="{attresc(level)}" aria-pressed="false">{esc(level)}</button>' for level in levels]
    chips.append('<span class="sep"></span><span class="lab">全文获取</span>')
    chips += [f'<button class="chip" data-facet="status" data-value="{attresc(value)}" aria-pressed="false">'
              f'{esc(value)}</button>' for value in statuses]
    chips.append('<span class="sep"></span><span class="lab">文献卡</span>')
    # 卡面按实际取值派生（与主题／等级／状态三面同规）：本交付没有的面不出控件，
    # 否则读者点下去必得空结果、还会怪自己的关键词（P2）
    card_faces = [value for value in ("1", "0")
                  if any(("1" if meta[row_no]["card"] else "0") == value for row_no in rows)]
    chips += [f'<button class="chip" data-facet="card" data-value="{value}" aria-pressed="false">'
              f'{"有文献卡" if value == "1" else "无文献卡"}</button>' for value in card_faces]
    rows_html = "\n".join(row_card_html(ctx, row_no, rows[row_no], meta[row_no])
                          for row_no in sorted(rows, key=package_parse.row_sort_key))
    html = [f'<section class="section" id="{sid}"><h2 class="h2" id="{sid}-h">{ctx.inline(title)}</h2>',
            ctx.blocks(pre),
            '<div class="controls" role="group" aria-label="证据表筛选与排序">'
            '<span class="lab">主题</span>' + "".join(chips) +
            '<span class="sep"></span>'
            '<label>排序 <select id="sort-mode"><option value="row">文献</option>'
            '<option value="cites">被引数</option><option value="level">等级</option></select></label>'
            '<label>分组 <select id="group-mode"><option value="none">平铺</option>'
            '<option value="theme">按主题</option></select></label>'
            '<input type="search" id="filter-q" placeholder="搜索文献、数值与结论" aria-label="证据表全文搜索">'
            '<button class="chip" id="filter-clear" type="button">清除筛选</button>'
            f'<span class="count" id="visible-count" role="status" aria-live="polite">'
            f'{len(rows)} / {len(rows)} 篇</span></div>',
            # 定义与筛选条同屏：首次用筛选的人先看到 E1…E5 与三值，而不是按完键再读到（第 5 轮 P2）；
            # 完整含义表收进同一处的展开件（第 7 轮）：屏上它同时是悬停弹层的键盘落点，纸上由打印展开。
            f'<details class="level-legend" id="level-legend"><summary>{GRADE_LEGEND}</summary>'
            f'{level_table_html()}</details>',
            '<p class="note" id="empty-state" role="status" hidden>没有匹配的文献——换一个关键词，'
            f'或点「清除筛选」回到全部 {len(rows)} 篇。（搜索只覆盖证据表；文献卡与页面正文不在范围内。）</p>',
            f'<div id="erow-list">{rows_html}</div>',
            ctx.blocks(post),
            '<p class="note">等级与全文获取的定义见上方筛选条下。</p>',
            '<p class="note">每篇文献与每张文献卡都有稳定的直达链接，可直接分享给他人；'
            '正文里指向文献与章节的引用都可以点开。</p>']
    for sub_title, sub_lines in subs[1:]:
        anchor = sub_anchor(sub_title)
        attr = f' id="{anchor}"' if anchor else ""
        html.append(f'<h3 class="h3"{attr}>{ctx.inline(sub_title)}</h3>')
        html.append(ctx.blocks(sub_lines))
    html.append("</section>")
    return "\n".join(html)


def card_units(blocks: list) -> list:
    """卡内段 → 渲染单元：字段自成单元，`##` 小节连同其后字段成员成单元（成员跟随小节）。

    单元按固定读者序重排（ADR-0029）：未登记的段保文档序殿后——同一名字在各卡上位置一致，
    读者才建立得起预期（仅摘要族把研究要素写成小节、排在关系段之后，保文档序就做不到）。
    """
    units = []
    for block in blocks:
        if block["kind"] == "section":
            units.append({"block": block, "members": []})
        elif units and units[-1]["block"]["kind"] == "section":
            units[-1]["members"].append(block)
        else:
            units.append({"block": block, "members": []})
    order = {name: i for i, name in enumerate(CARD_READER_ORDER)}
    return [unit for _, unit in sorted(enumerate(units),
                                       key=lambda pair: (order.get(base_name(pair[1]["block"]["name"]),
                                                                   len(CARD_READER_ORDER)), pair[0]))]


def split_research_paren(paren: str) -> tuple[str, str]:
    """研究要素的括注 → （要素名称对照, 对应研究问题）。

    问题单列成段内首行（逐卡不同、也是本读本分题判断的依据）；名称对照降为段名旁小注。
    原样一段的正文不动，只把这两件从长句里摘出来（第 7 轮）。
    """
    m = re.search(r"对应研究问题\s*[：:]\s*(.+)$", paren or "", re.S)
    if not m:
        return (paren or "").strip(), ""
    return paren[:m.start()].strip(" ；;、"), m.group(1).strip()


def identity_field_html(ctx, block, status_note: str) -> str:
    """身份段逐行渲染（题名／作者／来源／DOI ＋「其他标识与附注」）。

    位置校验失败（parse_identity 返回空）即整段回退原样——保真优先，不拿猜测的分段误导读者。
    """
    ident = parse_identity(block["body"])
    if not ident:
        return f'<div class="fbody"><p>{ctx.inline(block["body"])}</p></div>'
    values = (ident["title"], ident["authors"], ident["venue"], ident["doi"])
    rows = "".join(f'<div class="id-row"><span class="id-lab">{esc(label)}</span>'
                   f'<span class="id-val">{ctx.inline(value)}</span></div>'
                   for label, value in zip(IDENT_LABELS, values))
    tail = list(ident["tail"])
    if status_note:
        tail.insert(0, f'{IDENT_PAGE_NOTE}：{status_note}')
    if tail:
        rows += (f'<div class="id-tail"><div class="id-lab">{esc(IDENT_TAIL_LABEL)}</div><ul>'
                 + "".join(f'<li>{ctx.inline(item)}</li>' for item in tail) + "</ul></div>")
    return f'<div class="fbody ident">{rows}</div>'


def card_body_html(ctx, block) -> str:
    """字段正文 → 导语段／子块（小标＋括注＋条款）／条款列表；出口前做守恒自检（不等即程序级失败）。

    形态取家门既有的两级：段名（`.fname`）之下再分一层——子块名比段名重、比正文轻（`.bsub`），
    其下条款用项目符号（`.bitems`，无序号）；只有一条时不套列表（单个符号不是清单）。
    """
    plan = split_card_body(block["body"] or "")
    chunks = []

    def items_html(items):
        if len(items) > 1:
            return '<ul class="bitems">' + "".join(f"<li>{ctx.inline(item)}</li>" for item in items) + "</ul>"
        return f"<p>{ctx.inline(items[0])}</p>" if items else ""

    if plan["lead"]:
        chunks.append(items_html(plan["lead"]))
    for sub in plan["blocks"]:
        note = f'<span class="bnote">{ctx.inline(sub["note"])}</span>' if sub["note"] else ""
        chunks.append(f'<div class="fblock"><div class="bsub">{ctx.inline(sub["name"])}{note}</div>'
                      f'{items_html(sub["items"])}</div>')
    out = "".join(chunks)
    assert_body_conserved(plan, block["body"] or "", out, ctx.inline_quiet(block["body"] or ""), block["name"])
    return out


def block_field_html(ctx, block, status_note: str) -> str:
    """字段 → `<li class="field">`：身份段与等级段各有专属形态，其余按段名＋正文（含子项）。"""
    name = block["name"]
    classes = "field"
    if name in ("标识", "设计与E级", "与本文关系"):
        classes += " field-key"
    if name == "未见/未知":
        classes += " field-unknown"
    if not name:
        classes += " field-plain"
    if name == "标识":
        return (f'<li class="{classes}"><div class="fname">{esc(card_field_word(name))}</div>'
                f'{identity_field_html(ctx, block, status_note)}</li>')
    body_html = card_body_html(ctx, block) if block["body"] else ""
    if block["subs"]:
        body_html += '<ul class="subul">' + "".join(
            f"<li>{ctx.inline(sub)}</li>" for sub in block["subs"]) + "</ul>"
    if name == "研究要素":
        legend, question = split_research_paren(block.get("paren", ""))
        note = f'<span class="fnote">{ctx.inline(legend)}</span>' if legend else ""
        pre = f'<p class="rq">对应研究问题：{ctx.inline(question)}</p>' if question else ""
        return (f'<li class="{classes}"><div class="fname">{esc(card_field_word(name))}{note}</div>'
                f'<div class="fbody">{pre}{body_html}</div></li>')
    name_html = (f'<div class="fname">{esc(card_field_word(name))}'
                 + (LEVEL_OPEN_BUTTON if name == "设计与E级" else "")
                 + "</div>") if name else ""
    return f'<li class="{classes}">{name_html}<div class="fbody">{body_html}</div></li>'


def card_unit_html(ctx, unit, status_note: str) -> str:
    block = unit["block"]
    if block["kind"] == "section":
        base, note = section_note(block["name"])
        note_html = f'<span class="fnote">{ctx.inline(note)}</span>' if note else ""
        members = "".join(block_field_html(ctx, member, status_note) for member in unit["members"])
        inner = f'<ul class="card-fields">{members}</ul>' if members else ""
        return (f'<li class="card-sec"><div class="fsec">{esc(card_field_word(base))}{note_html}</div>'
                f'{inner}</li>')
    return block_field_html(ctx, block, status_note)


def card_element_html(ctx, card, meta, cites) -> str:
    rows_ = card["行"]
    row_no = rows_[0] if rows_ else None
    row_meta = meta.get(row_no) if row_no else None
    head = card["_head"]
    status_word, status_note = split_status(card["_status"])
    badges = []
    if row_no:
        # 卡头不重复「文献 N」（题名行之下已有作者年份、返回链与索引副行都带编号）；
        # 徽章行只承担三个读数：等级、被引、全文获取。
        if row_meta:
            badges.append(f'<span class="badge level" data-lv="{attresc(row_meta["level"])}">'
                          f'{esc(row_meta["level"])}</span>')
            if row_meta["cites"] is not None:
                badges.append(f'<span class="badge neutral">被引 {row_meta["cites"]}</span>')
    else:
        badges.append('<span class="badge neutral">背景文献（未列入证据表）</span>')
    if status_word:
        badges.append(f'<span class="badge neutral">{esc(status_word)}</span>')
    key_items, rest_items, rest_names = [], [], []
    for unit in card_units(card["_blocks"]):
        name = base_name(unit["block"]["name"])
        item = card_unit_html(ctx, unit, status_note)
        if name and name in CARD_INLINE_FIELDS:
            key_items.append(item)
        else:
            rest_items.append(item)
            if name:
                rest_names.append(card_field_word(name))
    fields_html = f'<ul class="card-fields">{"".join(key_items)}</ul>'
    if rest_items:
        names = "、".join(esc(name) for name in rest_names)
        fields_html += (f'<details class="card-rest"><summary>展开完整卡：其余 {len(rest_items)} 段'
                        f'{f"（{names}）" if names else ""}</summary>'
                        f'<ul class="card-fields">{"".join(rest_items)}</ul></details>')
    back = (f'<a class="backlink" href="#row-{row_no}" data-close-overlay="1">← 返回{ROW_LABEL} {row_no}</a>'
            if row_no else '<span class="backlink muted">背景文献（未列入证据表）</span>')
    return (f'<details class="card" id="card-{attresc(card["slug"])}" data-slug="{attresc(card["slug"])}">'
            f'<summary><span class="card-title">{esc(head["title"])}</span>'
            f'<span class="card-who">{esc(head["who"])}</span>'
            f'<span class="card-badges">{"".join(badges)}</span></summary>'
            f'<div class="card-body">{back}'
            + (f'<p class="card-prio"><a class="badge prio" href="#priority" data-close-overlay="1">'
               f'先读 {row_meta["prio"]}</a></p>' if row_meta and row_meta.get("prio") else "")
            + f'{fields_html}</div></details>')


def cards_section(ctx, cards, meta, cites_map) -> str:
    order = sorted(cards.values(),
                   key=lambda card: (package_parse.row_sort_key(card["行"][0]) if card["行"]
                                     else (10 ** 6, ""), card["slug"]))
    chips = []
    for card in order:
        row_no = card["行"][0] if card["行"] else None
        row_meta = meta.get(row_no) if row_no else None
        head = card["_head"]
        bits = [f"{ROW_LABEL} {row_no}" if row_no else "背景文献"]
        if head["who"]:
            bits.append(head["who"])
        if row_meta and row_meta["level"]:
            # 索引副行的等级也是同一处释义的入口（第 7 轮：全页凡出现等级处同交互）
            bits.append(f'<span class="lv" data-lv="{attresc(row_meta["level"])}">'
                        f'{esc(row_meta["level"])}</span>')
        # 索引 chip 是真链接（可右键复制／新标签打开），抽屉行为由 data-open-card 渐进增强；
        # 主行是题名（读者靠它认文献），副行给编号／作者年份／等级，被引降为悬停提示。
        cites = row_meta["cites"] if row_meta else None
        tip = f' title="被引 {cites}"' if cites is not None else ""
        chips.append(f'<a class="card-chip" href="#card-{attresc(card["slug"])}" '
                     f'data-open-card="{attresc(card["slug"])}"{tip}>'
                     f'<span class="cc-title">{esc(chip_title(head["title"]))}</span>'
                     f'<span class="cc-who">{" · ".join(bits)}</span></a>')
    elements = "\n".join(card_element_html(ctx, card, meta, cites_map) for card in order)
    return (f'<section class="section" id="cards"><h2 class="h2" id="cards-h">文献卡（{len(cards)} 张 · '
            f'原样呈现卡内原文）</h2>'
            '<p class="note">三种方式都能打开文献卡：证据表行的「打开文献卡」、关系图里的圆点、下面的卡片索引；'
            '关闭方式：右上角 ✕／ESC 键／卡片里的返回链接。' + SAME_SOURCE_NOTE
            + '<label class="print-toggle screen-only">打印含全部卡片 '
              '<input type="checkbox" id="print-expand"></label></p>'
            f'<div class="card-index">{"".join(chips)}</div>'
            f"{elements}</section>")


def priority_section(ctx, priority, meta, cards) -> str:
    """先读区（三处呼应的第一处；ADR-0031）：三行块的行解剖＋其余层收起。

    行＝名次圆牌（仅先读层）／题名＋副行（两者合成一个链到 `#row-N`）／理由，卡按钮在右。
    副行＝编号·年份·等级·全文获取，与卡片索引 chip 同一批字段；题名取卡内身份段的源题名，
    **整条呈现**（不截断、不砍副题，长题名折行——ADR-0031 窄修订：这里是「认文献」的主场，
    索引 chip 才用 `chip_title()` 单行截断）。理由里指向本行的出处指针不在读者面重复显示（决定 2）。
    其后层按真源 `####` 主题分组、组内不标名次，整层收进一个原生 `<details>`（决定 3）。
    """
    by_cid = {card["_cid"]: card["slug"] for card in cards.values()}

    def item_html(entry, rank=None) -> str:
        row_no = entry["row"]
        row_meta = meta.get(row_no) or {}
        slug = by_cid.get(entry["card"]) or (row_meta.get("card") or {}).get("slug") or ""
        card = cards.get(slug) or row_meta.get("card")
        title = (card or {}).get("_head", {}).get("title") or ""
        rank_html = (f'<span class="prio-rank" aria-label="先读第 {rank} 篇">先读 {rank}</span>'
                     if rank else "")
        bits = [f'{ROW_LABEL} {esc(row_no)}']
        if row_meta.get("year"):
            bits.append(esc(str(row_meta["year"])))
        level = row_meta.get("level") or ""
        if re.fullmatch(r"E[1-5]", level):
            # 等级记号与证据表行内、卡片索引副行同交互：悬停出全页唯一一份释义（义务 17）
            bits.append(f'<span class="lv" data-lv="{attresc(level)}">{esc(level)}</span>')
        elif level:
            bits.append(esc(level))
        if row_meta.get("status"):
            bits.append(esc(row_meta["status"]))
        if entry["dim"]:
            bits.append(f'<span class="prio-dim">{esc(entry["dim"])}</span>')
        card_html = (f'<a class="btn prio-card screen-only" href="#card-{attresc(slug)}" '
                     f'data-open-card="{attresc(slug)}">打开文献卡</a>' if slug else "")
        return (f'<li class="prio-item">'
                f'<div class="prio-head">{rank_html}'
                f'<a class="prio-main" href="#row-{attresc(row_no)}" '
                f'data-scroll-row="{attresc(row_no)}">'
                f'<span class="prio-title">'
                f'{esc(title) or f"{ROW_LABEL} {esc(row_no)}"}</span>'
                f'<span class="prio-meta">{" · ".join(bits)}</span></a></div>'
                f'{card_html}'
                f'<p class="prio-reason">'
                f'{ctx.inline(strip_self_pointer(entry["reason"], row_no))}</p></li>')

    first_n = len(priority["first"])
    later_n = len(priority["entries"]) - first_n
    groups_n = len(priority["groups"])
    # 标题问「先读哪几篇」而本节含全部篇目：一句话说清节内容，免得名实不符（第 5 轮 minor）
    scope_note = (f"其余 {later_n} 篇按主题分 {groups_n} 组列出、不标名次。" if groups_n
                  else f"其余 {later_n} 篇不标名次。")
    html = [f'<section class="section" id="priority">'
            f'<h2 class="h2" id="priority-h">{ctx.inline(priority["title"])}</h2>',
            f'<p class="note">本节含全部 {first_n + later_n} 篇：先读 {first_n} 篇带名次，' + scope_note + '</p>',
            # 维度三词当场给定义（第 8 轮）：读者先读到判据，再逐行看理由；纸面与屏上同文
            '<p class="note">理由各带一个归类：结论关联＝结论里直接引到它；'
            '权衡价值＝它给出代价或反证；主题补位＝它补上某个主题的空白。</p>']
    def group_order(block) -> tuple:
        match = re.search(r"§\s*(\d+)(?:\.(\d+))?", block.get("label") or "")
        return (0, int(match.group(1)), int(match.group(2) or 0)) if match else (1, 0, 0)

    # 分组按主题号排序：真源块序可能先出 §4.2 再出 §4.1，与「按 §4 主题分组」的宣称不符（第 6 轮 minor）。
    groups_sorted = iter(sorted((b for b in priority["sequence"] if b["kind"] not in ("prose", "first")),
                                key=group_order))
    sequence = [b if b["kind"] in ("prose", "first") else next(groups_sorted)
                for b in priority["sequence"]]
    later_html = []
    for block in sequence:
        if block["kind"] == "prose":
            # 层标签句（「先读层（依据序，N 篇）：」／「其后层（按 §4 主题分组…）：」）由节首说明与
            # 折叠 summary 承担；其余散文照原渲染，不吞真源内容（ADR-0031 决定 3）。
            lines = [line for line in block["lines"] if not PRIO_LABEL_PROSE_RE.match(line.strip())]
            if lines:
                html.append(f'<p class="note">{ctx.inline(" ".join(lines))}</p>')
        elif block["kind"] == "first":
            html.append('<ol class="prio-list">'
                        + "".join(item_html(entry, entry["rank"]) for entry in block["entries"])
                        + "</ol>")
        else:
            later_html.append(f'<h3 class="h3 prio-group-head">其余篇目 · '
                              f'{ctx.inline(block["label"] or "未分组")}</h3>')
            later_html.append('<ul class="prio-list">'
                              + "".join(item_html(entry) for entry in block["entries"]) + "</ul>")
    if later_html:
        html.append(f'<details class="prio-fold" id="prio-fold">'
                    f'<summary>其余 {later_n} 篇 · 按主题分 {groups_n} 组</summary>'
                    + "".join(later_html) + '</details>')
    html.append('<p class="note"><span class="screen-only">点题名跳到证据表对应行、点「打开文献卡」看卡；</span>'
                '同一份名单也标在关系图的圆点与卡片上，名次只给先读层。'
                + SAME_SOURCE_NOTE + '</p>')
    html.append("</section>")
    return "\n".join(html)


def review_entries(delivery: Path, manifest: dict, graph, referenced=()) -> tuple:
    """校验信息的条目：交付件清单＋复核命令（按在场件裁剪）。

    件清单＝正文提到过的件（`Ctx.files`，按首现序）＋关键四件（在场者）。
    原在正文降级块与先读说明里的重跑命令按读者面口径集中到这里——页内只有本区允许出现
    仓库路径与可粘贴命令（`reader-html.md` §3）。
    """
    files = list(referenced)
    for rel in REVIEW_FILES:
        if rel not in files and (delivery / rel).exists():
            files.append(rel)
    rel_dir = display_path(delivery)
    commands = []
    if (delivery / PACKAGE_REL).exists():
        year = str((manifest.get("topic") or {}).get("delivered") or "")[:4]
        suffix = f" --cur-year {year}" if year.isdigit() else ""
        commands.append("uv run python .claude/skills/med-lit-review/scripts/review_knowledge.py "
                        f"{rel_dir}/{PACKAGE_REL}{suffix}")
    if (delivery / "agent/screening-log.md").exists():
        commands.append(f"uv run python .claude/skills/med-lit-review/scripts/check_growth_loop.py {rel_dir}")
    commands.append(f"uv run python .claude/skills/med-lit-review/scripts/check_delivery.py --delivery {rel_dir}")
    commands.append("uv run python .claude/skills/med-lit-review/scripts/reader_html.py "
                    f"--delivery {rel_dir} --check")
    if graph and graph.get("degraded"):
        rerun = P1_DEGRADE_COMMAND if graph.get("profile") == "p1" else GRAPH_DEGRADE_COMMAND
        commands.append(rerun.replace("<交付目录>", rel_dir))
    return files, commands


def footer_html(delivery: Path, manifest: dict, inputs, graph=None, referenced=(), route_label="") -> str:
    files, commands = review_entries(delivery, manifest, graph, referenced)
    topic = manifest.get("topic") or {}
    caliber = str((manifest.get("caliber") or {}).get("grey_gate_version") or MISSING)
    file_rows, seen_labels = [], set()
    for rel in files:
        label = file_label(rel)
        attr = "" if label in seen_labels else f' id="{attresc(file_anchor(label))}"'
        seen_labels.add(label)
        file_rows.append(f'<div class="fp"{attr}>{esc(label)} · {esc(rel)}</div>')
    file_lines = "".join(file_rows)
    command_lines = "".join(f'<div class="fp"><code>{esc(line)}</code></div>' for line in commands)
    fingerprints = "".join(f'<div class="fp">{esc(rel)} · sha256:{digest[:12]}</div>'
                           for rel, digest in inputs)
    return (f'<footer><div class="wrap">'
            f'<h2 class="h2-ft" id="review">校验信息（交付件与命令）</h2>'
            f'<details class="review-fold"><summary>展开校验信息：交付件清单、复核命令与本页输入件指纹</summary>'
            f'<p>交付目录 {esc(str(topic.get("dir") or MISSING))} ｜ 路线码 {esc(route_label or MISSING)} ｜ '
            f'渠道码 {esc(caliber)}（单文件外发自证的身份码）</p>'
            f'<p>交付件（仓库根相对路径）：</p>{file_lines}'
            f'<p>复核命令：</p>{command_lines}'
            f'<p>输入件指纹（{len(inputs)} 件 · sha256 前 12 位）：</p>'
            f'<div class="fp-list">{fingerprints}</div>'
            f'<p>零外部依赖：无外链 CSS／JS／字体／图片；断网 <code>file://</code> 直开。</p>'
            f'</details></div></footer>')


def skip_link() -> str:
    return '<a class="skip" href="#main">跳到正文</a>'


def overlay_html() -> str:
    return ('<div class="overlay" id="overlay" role="dialog" aria-modal="true" aria-labelledby="overlay-title">'
            '<div class="overlay-panel" id="overlay-panel"><div class="overlay-head">'
            '<div class="overlay-headtext">'
            '<div class="overlay-title" id="overlay-title"></div>'
            '<div class="overlay-sub" id="overlay-sub"></div>'
            '<div class="overlay-badges" id="overlay-badges"></div>'
            '</div><button class="overlay-close" type="button">关闭 ✕</button></div>'
            '<div class="overlay-body" id="overlay-body"></div></div></div>'
            + level_pop_html())


def collect_inputs(delivery: Path, cards, graph) -> list:
    """本次实际消费的输入件（页脚指纹与 `derived.reader_html.inputs[]` 同源）。

    `agent/manifest.json` 不进本清单：渲染器自登记会改其字节，纳入即自指（`--check` 恒不一致）。
    """
    rels = [PACKAGE_REL, REFERENCE_LIST_REL]
    rels += [card["文件"] for card in sorted(cards.values(), key=lambda card: card["文件"])]
    rels += list(OPTIONAL_INPUTS)
    rels += list(graph["readings"]["files"])
    out, seen = [], set()
    for rel in rels:
        if rel in seen or not (delivery / rel).exists():
            continue
        seen.add(rel)
        out.append((rel, sha256_of(delivery / rel)))
    return out




def graph_section(graph, themes, ctx=None) -> str:
    """图区（恒在场）：引文面可得＝SVG；不可得＝降级块＋主节点图（主节点来自版内件，不依赖引文面）。"""
    if not graph["degraded"]:
        return graph_svg(graph, themes, ctx)
    block = ('<div class="degrade" id="graph-degrade"><h3>关系图数据本次未取到</h3>'
             '<p>原因：引文面数据缺件或不可解析。图位保留：缺的是数据，不是承诺。</p>'
             '<p>重跑命令与逐条取数数字见页尾「校验信息」。</p></div>')
    return block + graph_svg(graph, themes, ctx)


def assemble_full(delivery: Path, manifest: dict, ctx, package_md, sections, rows, meta, cards,
                  cites_map, themes, graph, route_label, stats, question, answer, inputs,
                  priority) -> str:
    toc, main = [], []

    def section_key(title: str) -> str:
        m = re.match(r"(?:§\s*)?(\d+)", title.strip())
        return m.group(1) if m else ""

    has6 = any(section_key(title) == "6" for title, _ in sections)  # §6 在则先读紧随其后（02 决议 7）
    # 阅读顺序说明：序数保留规范编号、顺序按阅读任务（结论先于证据表，ADR-0008）——
    # 在读者遇到「§6 之后才是 §5」的那一刻给出解释与直达链。
    order_keys = [section_key(title) for title, _ in sections]
    order_note = ""
    if "6" in order_keys and "5" in order_keys and order_keys.index("6") < order_keys.index("5"):
        order_note = ('<p class="note">本篇按阅读任务排序：结论先于证据表（§6 → §5）——先给判断、再给支撑；'
                      '想直接看证据，<a class="secref" href="#s5">跳到 §5 证据表</a>。</p>')
    inserted = False
    for title, body in sections:
        m = re.match(r"(?:§\s*)?(\d+)\s*(.*)", title.strip())
        key = m.group(1) if m else ""
        sid = f"s{key}" if key else (re.sub(r"\W+", "-", title.strip().lower()) or "section")
        label = f"§{key} {m.group(2)}".strip() if key else title.strip()
        if "证据表" in title:
            body = package_parse.strip_priority_section(body)  # §5.1 移到 §6 后独立成区，不在 §5 内二次渲染
            toc.append(("graph", "关系图"))
            main_dots = [node for node in graph["nodes"] if node["main"]]
            dot_paths = []
            if any(node["card"] for node in main_dots):
                dot_paths.append("有文献卡的圆点打开文献卡")
            if any(node["evidence_row"] and not node["card"] for node in main_dots):
                dot_paths.append("其余圆点跳到对应文献")
            # 落点句只描述本包真有的行为：写一个本交付不可达的落点＝让读者点了没反应（同图例派生口径）
            dot_paths_note = ("；".join(dot_paths) + "；") if dot_paths else ""
            main.append('<section class="section" id="graph"><h2 class="h2" id="graph-h">文献关系图</h2>'
                        + graph_section(graph, themes, ctx) +
                        '<p class="note">点＝本次文献与有文献卡的背景文献（实心＝有文献卡）；'
                        '空心点＝它们的相邻文献，只作背景、不可点。'
                        + dot_paths_note + '键盘 Enter／Space 与鼠标等价。'
                        + SAME_SOURCE_NOTE + '</p></section>')
            toc.append((sid, label))
            main.append(evidence_section(ctx, title, body, sid, rows, meta, themes))
            toc.append(("cards", f"文献卡 {len(cards)}"))
            main.append(cards_section(ctx, cards, meta, cites_map))
        else:
            toc.append((sid, label))
            main.append(generic_section(ctx, title, body, sid, order_note if key == "6" else ""))
        if priority["present"] and not inserted and (key == "6" or (key == "4" and not has6)):
            toc.append(("priority", f'{priority["title"]}（§5.1）'))
            main.append(priority_section(ctx, priority, meta, cards))
            inserted = True
    if priority["present"] and not inserted:
        toc.append(("priority", f'{priority["title"]}（§5.1）'))
        main.append(priority_section(ctx, priority, meta, cards))
    nav = ('<nav class="topnav" aria-label="章节导航"><div class="wrap"><span class="brand">知识包</span>'
           + "".join(f'<a href="#{sid}">{esc(text)}</a>' for sid, text in toc) + "</div></nav>")
    return "\n".join([skip_link(),
                      hero_html(manifest, package_md, ctx, stats, route_label, question, answer),
                      nav, '<main id="main" class="wrap" tabindex="-1">', *main, "</main>",
                      overlay_html(), footer_html(delivery, manifest, inputs, graph, ctx.files, route_label)])


def render_full(delivery: Path, manifest: dict, route_label: str):
    """八节全页（P0／P2／P3）：答案先行＋八节正文＋证据表行卡＋提取卡＋关系图。"""
    package_md, cards, rows, refs = load_delivery(delivery)
    sections = package_parse.split_h2(package_md)
    priority = package_parse.reading_priority(package_md)
    prio_rank = {entry["row"]: entry["rank"] for entry in priority["first"] if entry["rank"]}
    themes = theme_catalog(package_md)
    row_cards = package_parse.row_card_map(rows, cards)
    status_of = make_status_of(parse_accessibility(delivery))
    cites_map = parse_cites(delivery)
    ctx = Ctx(rows=rows, cards={card["slug"] for card in cards.values()},
              section_ids=section_index(sections), themes=themes)
    meta = build_row_meta(rows, refs, cards, row_cards, themes, status_of, cites_map, prio_rank,
                          years_map=parse_years(delivery))
    graph = graph_data_block(delivery, rows, refs, cards, row_cards, themes, cites_map, status_of, prio_rank)
    heading = next((line[2:].strip() for line in package_md.splitlines() if line.startswith("# ")),
                   str((manifest.get("topic") or {}).get("slug") or ""))
    # 首屏三数（ADR-0032 §8）：检索候选（终池条目数）→ 本次研读（研读选取集）→ 证据表（表行数）；
    # 前两项是可降级读数（缺件记 ∅ → 读者面「未取到」），故不沿「队列件必在」的旧读法。
    stats = [("检索候选", first_screen_count(delivery, DEDUPE_REL, "final_count")),
             ("本次研读", first_screen_count(delivery, SELECTION_REL, "selected_total")),
             ("证据表", len(rows))]
    inputs = collect_inputs(delivery, cards, graph)
    question = research_question(package_md)
    answer = core_excerpt(package_md)
    if not answer.strip():
        raise RenderError(f"§6.2 核心结论摘录取不到（{PACKAGE_REL}）：首屏「答案先行」块是渲染义务"
                          "（reader-html.md §3.2）——不产只剩标签的空壳页，也不静默降级")
    body = assemble_full(delivery, manifest, ctx, package_md, sections, rows, meta, cards, cites_map,
                         themes, graph, route_label, stats, question, answer, inputs, priority)
    page = page_shell(heading, body, PAGE_JS, dumps_json(graph))
    readings = {"profile": "full", "stats": dict(stats), "rows": len(rows), "cards": len(cards),
                "nodes": len(graph["nodes"]),
                "edges": len(graph["edges"]), "truncated": graph["graph_truncated"],
                "degraded": graph["degraded"], "links": dict(ctx.links),
                "sections": len(sections), "inputs": len(inputs),
                "identity": identity_readings(cards),
                "body_split": body_split_readings(cards),
                "priority": {"present": priority["present"], "first": len(priority["first"]),
                             "later": len(priority["entries"]) - len(priority["first"]),
                             "groups": len(priority["groups"]),
                             "rows": len(prio_rank)}}
    return page, readings, inputs


BUCKET_ORDER = ("开放直下", "仓储副本", "需机构", "灰色候选")


def composition_svg(composition: dict) -> str:
    """P1 检索构成图（03 §7：换「构成」语义，不画关系图）：四桶计数横条。"""
    buckets = composition["buckets"]
    names = [name for name in BUCKET_ORDER if name in buckets] + \
            [name for name in sorted(buckets) if name not in BUCKET_ORDER]
    total = composition["total"] or 1
    rows = []
    for index, name in enumerate(names):
        value = buckets[name]
        width = 720 * value / total
        y = 40 + index * 46
        rows.append(f'<text class="cluster-text" x="8" y="{y + 14}">{esc(name)}</text>'
                    f'<rect x="200" y="{y}" width="{width:.1f}" height="20" rx="3" fill="var(--c3)"/>'
                    f'<text class="node-num" x="{200 + width + 8:.1f}" y="{y + 15}">{value}</text>')
    height = 40 + len(names) * 46 + 20
    return (f'<svg class="graph" viewBox="0 0 1000 {height}" role="img" '
            f'aria-label="检索构成图（四桶计数）">' + "".join(rows) + "</svg>")


def render_p1(delivery: Path, manifest: dict, route_label: str):
    """P1 查新页：查新结论＋四桶与计数＋检索构成图（数据不可得时降级块恒在用图位）。"""
    note_text, buckets = parse_p1(delivery)
    sections = package_parse.split_h2(note_text)
    ctx = Ctx(rows={}, cards=set(), section_ids=section_index(sections), themes=[])
    main, toc = [], []
    for title, body in sections:
        m = re.match(r"(?:§\s*)?(\d+)\s*(.*)", title.strip())
        sid = f"s{m.group(1)}" if m else (re.sub(r"\W+", "-", title.strip().lower()) or "section")
        toc.append((sid, f"§{m.group(1)} {m.group(2)}".strip() if m else title.strip()))
        main.append(generic_section(ctx, title, body, sid))
    composition = {"buckets": dict(sorted(buckets.items())), "total": sum(buckets.values())}
    degraded = not buckets
    graph = {"spec": "reader-html-graph-v2（P1 构成图档）", "profile": "p1", "degraded": degraded,
             "degraded_reason": ("缺 " + LEDGER_REL + "（四桶构成无从取数）") if degraded else None,
             "caps": {"nodes": NODE_CAP, "edges": EDGE_CAP},
             "graph_truncated": {"main_nodes": 0, "kept_neighbourhood": 0, "kept_nodes": 0,
                                 "neighbourhood_candidates": 0, "dropped_neighbourhood": 0,
                                 "edges_total": 0, "edges_kept": 0, "dropped_edges": 0,
                                 "isolated_kept": 0, "cap_hit": False},
             "readings": {"seeds": 0, "edges_collected": 0, "seeds_with_refs": None, "files": []},
             "layout": {"main_failed": False, "sectors": [], "centre": [CENTRE_X, CENTRE_Y],
                        "a": ELL_A, "b": ELL_B, "corridor_w": CORRIDOR_W, "label_ring_w": LABEL_RING_W,
                        "node_band_r": NODE_BAND_R, "cells": {"main": CELL_MAIN, "neigh": CELL_NEIGH},
                        "dropped": []},
             "frame": {"main": f"0 0 {CANVAS_W:.0f} {CANVAS_H:.0f}",
                       "all": f"0 0 {CANVAS_W:.0f} {CANVAS_H:.0f}"},
             "sectors": [], "corridors": [], "labels": [],
             "nodes": [], "edges": [], "composition": composition,
             "encoding": {"bucket": "四桶计数取自下载台账（`agent/bucketing-and-sources.md`）逐行桶列计数"}}
    if degraded:
        chart = ('<div class="degrade" id="graph-degrade"><h3>检索构成图数据本次未取到</h3>'
                 '<p>原因：下载台账缺件（四桶构成未取到）。图位保留：缺的是数据，不是承诺。</p>'
                 '<p>重跑命令与数据件见页尾「校验信息」。</p></div>')
    else:
        chart = composition_svg(composition)
    graph_block = ('<section class="section" id="graph"><h2 class="h2" id="graph-h">检索构成</h2>'
                   + chart +
                   '<p class="note">本页是查新档（不产文献卡）：此图只呈现候选构成，不作关系解读。</p></section>')
    toc.append(("graph", "检索构成"))
    heading = next((line[2:].strip() for line in note_text.splitlines() if line.startswith("# ")),
                   str((manifest.get("topic") or {}).get("slug") or ""))
    stats = [("候选合计", composition["total"] or MISSING), ("构成图", MISSING if degraded else "在场")]
    inputs = []
    for rel in [SCOPING_NOTE_REL, LEDGER_REL, "run/b1/step1-diagnostics.json"]:
        if (delivery / rel).exists():
            inputs.append((rel, sha256_of(delivery / rel)))
    body = "\n".join([skip_link(),
                      hero_html(manifest, note_text, ctx, stats, route_label,
                                "本次查新：研究范围内的候选构成与可研判性（详见证件）",
                                next((line.strip("- ").strip() for line in note_text.splitlines()
                                      if line.strip().startswith("-")), "")),
                      '<nav class="topnav" aria-label="章节导航"><div class="wrap"><span class="brand">知识包</span>'
                      + "".join(f'<a href="#{sid}">{esc(text)}</a>' for sid, text in toc)
                      + "</div></nav>",
                      '<main id="main" class="wrap">', *main, graph_block, "</main>",
                      overlay_html(), footer_html(delivery, manifest, inputs, graph, ctx.files, route_label)])
    page = page_shell(f"{heading} · 查新摸底", body, PAGE_JS, dumps_json(graph))
    readings = {"profile": "p1", "rows": 0, "cards": 0, "nodes": 0, "edges": 0,
                "truncated": graph["graph_truncated"], "degraded": degraded,
                "links": dict(ctx.links), "sections": len(sections), "inputs": len(inputs)}
    return page, readings, inputs


# ---------------------------------------------------------------- 写盘与登记


def write_atomic(path: Path, text: str) -> None:
    """原子写（同族 `write_atomic` 口径）：LF 字节、`os.replace` 落位。"""
    staging = path.with_suffix(path.suffix + ".staging")
    with staging.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(staging, path)


def indentation_unit(text: str) -> int:
    """清单缩进单位：取首个缩进行的前导空白宽（缺省 2），回写时不把整件重排成另一种风格。"""
    m = re.search(r"(?m)^(\s+)\S", text)
    return len(m.group(1)) if m else 2


def render_manifest(path: Path, manifest: dict) -> str:
    """按原文件的缩进与换行风格序列化（CRLF 保 CRLF、indent 保 indent；仅改动的行才变）。"""
    text = path.read_text(encoding="utf-8")
    payload = json.dumps(manifest, ensure_ascii=False, indent=indentation_unit(text))
    suffix = "\n" if text.endswith(("\n", "\r\n")) else ""
    if "\r\n" in text:
        return (payload + suffix).replace("\n", "\r\n")
    return payload + suffix


def register_manifest(delivery: Path, slug: str, page_hash: str, inputs, route_label: str,
                      counts: dict | None = None) -> bool:
    """登记（05 §6）：`versioned_artifacts[]` 增根件项、`derived.reader_html` 换成员；语义不变则零写。

    `counts` ＝身份段逐行覆盖读数（`identity_readings`）：交付侧 W-23 拿它与页面块数、卡件数三方对账。
    P1 档（无卡域）记空表，不伪造张数。
    """
    path = delivery / MANIFEST_REL
    manifest = read_json(path)
    rel = f"{slug}.html"
    artifacts = [entry for entry in manifest.get("versioned_artifacts") or [] if isinstance(entry, dict)]
    artifacts = [entry for entry in artifacts if entry.get("path") != rel]
    index = next((i for i, entry in enumerate(artifacts) if entry.get("path") == "INDEX.md"), len(artifacts) - 1)
    artifacts.insert(index + 1, {"path": rel, "sha256": page_hash})
    command = ("uv run python .claude/skills/med-lit-review/scripts/reader_html.py "
               f"--delivery {display_path(delivery)}")
    if route_label == "P1":
        command += " --route P1"
    derived = dict(manifest.get("derived") or {})
    derived.pop("merged_cards", None)  # 合卡已退场（03 票）：派生段语义＝派生渲染件
    derived["reader_html"] = {"path": rel, "sha256": page_hash,
                              "inputs": [{"path": name, "sha256": digest} for name, digest in inputs],
                              "command": command, "machine_reads": False}
    if counts is not None:
        derived["reader_html"]["counts"] = counts
    if manifest.get("versioned_artifacts") == artifacts and manifest.get("derived") == derived:
        return False
    manifest["versioned_artifacts"] = artifacts
    manifest["derived"] = derived
    write_atomic(path, render_manifest(path, manifest))
    return True


def reconciliation(delivery: Path, slug: str, page_hash: str):
    """对账三方（W-08 口径）：页哈希 ＝ `versioned_artifacts[]` 项 ＝ `derived.reader_html.sha256`。"""
    rel = f"{slug}.html"
    try:
        manifest = read_json(delivery / MANIFEST_REL)
    except (OSError, ValueError) as exc:
        return False, [f"{MANIFEST_REL} 读取失败：{type(exc).__name__}: {exc}"]
    registered = next((entry.get("sha256") for entry in manifest.get("versioned_artifacts") or []
                       if isinstance(entry, dict) and entry.get("path") == rel), None)
    derived = ((manifest.get("derived") or {}).get("reader_html") or {})
    lines, ok = [], True
    if registered != page_hash:
        ok = False
        lines.append(f"versioned_artifacts 的 {rel} 登记不符（登记 {str(registered)[:12]}／页 {page_hash[:12]}）")
    if derived.get("sha256") != page_hash:
        ok = False
        lines.append(f"derived.reader_html.sha256 不符（登记 {str(derived.get('sha256'))[:12]}／页 {page_hash[:12]}）")
    if ok:
        lines.append(f"对账三方相等（versioned_artifacts／derived.reader_html／页 sha256 {page_hash[:12]}）")
    return ok, lines


def input_drift(delivery: Path) -> list:
    """登记输入清单 vs 盘上件（缺席跳过、只报漂移）：`--check` 的读数行，不判失败。"""
    try:
        manifest = read_json(delivery / MANIFEST_REL)
    except (OSError, ValueError):
        return []
    recorded = (manifest.get("derived") or {}).get("reader_html") or {}
    drift = []
    for entry in recorded.get("inputs") or []:
        if not isinstance(entry, dict):
            continue
        path = delivery / str(entry.get("path"))
        if not path.exists():
            drift.append(f"{entry.get('path')}（缺席）")
        elif sha256_of(path) != entry.get("sha256"):
            drift.append(f"{entry.get('path')}（{str(entry.get('sha256'))[:12]} → {sha256_of(path)[:12]}）")
    return drift


def check_page(delivery: Path, slug: str, page_text: str, started: float = 0.0) -> int:
    """`--check`：同盘复算比对＋对账；一致打 `MATCH` 退出 0，不一致退出 1、不写盘。"""
    page_path = delivery / f"{slug}.html"
    recomputed = sha256_bytes(page_text.encode("utf-8"))
    lines, ok = [], True
    if not page_path.exists():
        ok = False
        lines.append(f"盘上页缺件：{slug}.html（复算 sha256 {recomputed[:12]}）")
    else:
        disk = page_path.read_bytes()
        fresh = page_text.encode("utf-8")
        same = disk == fresh
        ok = ok and same
        lines.append(f"页字节{'一致' if same else '不一致'}（盘上 {sha256_bytes(disk)[:12]}／{len(disk)} 字节；"
                     f"复算 {recomputed[:12]}／{len(fresh)} 字节）")
    reconciled, reconcile_lines = reconciliation(delivery, slug, recomputed)
    ok = ok and reconciled
    lines.extend(reconcile_lines)
    drift = input_drift(delivery)
    lines.append(f"输入漂移 {len(drift)} 件" + (f"：{'、'.join(drift[:4])}" if drift else "（登记输入清单与盘上件全等）"))
    if ok:
        print(f"MATCH {recomputed}")
    else:
        print("MISMATCH（差异摘要）")
    for line in lines:
        print(f"  {line}")
    if started:
        print(f"  耗时 {time.perf_counter() - started:.2f}s（只走 stdout，不进页）")
    return 0 if ok else 1


# ---------------------------------------------------------------- CLI


def auto_route(delivery: Path) -> str:
    if (delivery / PACKAGE_REL).exists():
        return "P0"
    if (delivery / SCOPING_NOTE_REL).exists():
        return "P1"
    entries = sorted(path.name for path in (delivery / "agent").iterdir()) if (delivery / "agent").is_dir() else []
    raise RenderError(
        "路线判定失败：既无 " + PACKAGE_REL + "（P0–P3 八节包）也无 " + SCOPING_NOTE_REL +
        "（P1 快线）；实测 agent/ 条目：" + ("、".join(entries) or "无")
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="人读主件渲染器：从版内 markdown 真源确定性渲染单页 HTML")
    parser.add_argument("--delivery", required=True, help="交付目录（workspace/YYYY-MM-DD-<slug>）")
    parser.add_argument("--check", action="store_true", help="同盘复算比对＋对账，不写盘")
    parser.add_argument("--route", choices=("P0", "P1", "P2", "P3"), default=None,
                        help="覆盖路线判定（缺省按件存在性自动判）")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    delivery = Path(args.delivery)
    if not delivery.is_dir():
        print(f"输入缺失：{args.delivery}（交付目录不在场）", file=sys.stderr)
        return 2
    try:
        manifest = read_json(delivery / MANIFEST_REL)
    except (OSError, ValueError) as exc:
        print(f"必需件缺或解析失败：{MANIFEST_REL}（{type(exc).__name__}: {exc}）", file=sys.stderr)
        return 2
    slug = str((manifest.get("topic") or {}).get("slug") or "")
    if not slug:
        print(f"{MANIFEST_REL} 缺 topic.slug（输出名无从定），不产页", file=sys.stderr)
        return 2
    try:
        route_label = args.route or auto_route(delivery)
        if route_label == "P1":
            page, readings, inputs = render_p1(delivery, manifest, route_label)
        else:
            page, readings, inputs = render_full(delivery, manifest, route_label)
    except package_parse.CardParseError as exc:
        print(f"卡面解析失败：{exc}", file=sys.stderr)
        return 2
    except RenderError as exc:
        print(f"渲染前置失败：{exc}", file=sys.stderr)
        return 2
    started = time.perf_counter()
    if args.check:
        return check_page(delivery, slug, page, started)
    page_path = delivery / f"{slug}.html"
    write_atomic(page_path, page)
    page_hash = sha256_bytes(page.encode("utf-8"))
    registered = register_manifest(delivery, slug, page_hash, inputs, route_label,
                                   counts=readings.get("identity"))
    size = page_path.stat().st_size
    try:
        return report_readings(delivery, slug, page_path, size, page_hash, inputs, readings,
                               registered, route_label, started)
    except OSError:  # stdout 提前关闭（`| head`、pager 退出）：退出码只反映渲染与登记结果
        return 0


def report_readings(delivery, slug, page_path, size, page_hash, inputs, readings,
                    registered, route_label, started) -> int:
    print(f"人读主件：{display_path(page_path)}（{size} 字节／{size / 1024:.0f}KB；路线 {route_label}；"
          f"sha256 {page_hash[:12]}）")
    print(f"读数：行 {readings['rows']}｜卡 {readings['cards']}｜图节点 {readings['nodes']}"
          f"｜图边 {readings['edges']}｜截断 cap_hit={readings['truncated']['cap_hit']}"
          f"｜引文面{'缺（降级块在场）' if readings['degraded'] else '在场'}")
    stats = readings.get("stats")  # P1 档无三数（该档不产证据表与选取集，读数不适用）
    if stats:
        # 首屏三数照读数打印：缺失在机器面记 ∅（读者面才是「未取到」，ADR-0032 §8）
        print("首屏三数：" + "｜".join(
            f"{label} {'∅' if value == MISSING else value}" for label, value in stats.items()))
    prio = readings.get("priority")
    if prio is None:  # P1 档不产该层（无八节包／无卡域）：读数照记「不适用」，不猜、不留空壳
        print("精读序：不适用（本档路线不产该层：无八节包／无卡域）")
    elif prio["present"]:
        print(f"精读序：先读层 {prio['first']} 篇｜其后层 {prio['later']} 篇｜分组 {prio['groups']} 组")
    else:
        print("精读序：缺席（版内无 §5.1：不渲染该区、不留空壳）")
    print(f"正文自动成链：{ROW_LABEL} N {readings['links']['row']} 处｜"
          f"件名链接 {readings['links']['file']} 处｜§x.y {readings['links']['section']} 处"
          f"｜卡引用 {readings['links']['card']} 处")
    body_split = readings.get("body_split")
    if body_split:  # 字段正文切分读数（第 10 轮）：切分只出读数、不加门（验收＝读者一眼）
        print(f"卡片正文切分：子块 {body_split['sub_blocks']}｜条款 {body_split['clauses']}"
              f"（最长 {body_split['longest_clause']} 字）"
              f"｜仍为单段 {body_split['single_paragraph_fields']}／{body_split['fields']} 字段")
    ident = readings.get("identity")
    if ident:  # 身份段逐行覆盖（卡头题名与卡内「文献」段的真源）：回退逐卡点名，不靠肉眼发现
        fallback = ident["ident_fallback"]
        note = "；回退＝整段回退原样，保真优先，卡文件按位约定补正即可回到逐行"
        names = "、".join(item["card"] for item in fallback[:3]) + ("…" if len(fallback) > 3 else "")
        print(f"身份段逐行：{ident['ident_rows']}／{ident['cards']} 张"
              + (f"｜回退 {len(fallback)} 张（{names}）{note}" if fallback else ""))
    else:
        print("身份段逐行：不适用（本档路线不产卡域）")
    print(f"输入件指纹（{len(inputs)} 件）：")
    for name, digest in inputs:
        print(f"  {name} sha256:{digest[:12]}")
    print(f"登记：versioned_artifacts 增 {slug}.html；derived.reader_html "
          f"{'已更新' if registered else '无变化（零写）'}")
    print(f"耗时 {time.perf_counter() - started:.2f}s（只走 stdout，不进页——确定性口径见 reader-html.md §4）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
