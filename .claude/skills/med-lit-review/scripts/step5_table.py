#!/usr/bin/env python
"""§5 七列表草稿：卡面逐张投影成一行 ＋ 「文献」列取引文表同号条目（草稿，不直写包内件）。

六列（设计与等级／人群／干预与暴露／结局与效应量／偏倚备注／与结论的关联）从提取卡逐张投影
（卡→表无损搬运，口径见 `reading-card-template.md` §3；行内 `|` 转 `/` 防破表）。
「文献」列**只取** `agent/reference-list.md` 同号条目全文（去尾点），不另拼简式——
自拼「作者，年，期刊」类简式会让题名在页面上整段消失（门禁 W-24 守此形态）。

行序＝行号升序；行集＝卡面「对应文献 N」有行号者（背景卡「对应文献：无」不进表）。
引文表缺行号即退出 2（不退回自拼）；版本行（P1 模板取数）不认。

草稿（`run/step5-table.md`＋`run/step5-row-mapping.json`）由人工核对后手工并入
`agent/knowledge-package.md` 的 §5 七列表（D5 草稿原则：机器不直写包内真源）。

运行：
uv run python .claude/skills/med-lit-review/scripts/step5_table.py --delivery workspace/YYYY-MM-DD-<slug>
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

CARDS_GLOB = f"{package_parse.CARDS_REL}/*{package_parse.CARD_SUFFIX}"
REF_REL = "agent/reference-list.md"
OUT_REL = "run/step5-table.md"
MAP_REL = "run/step5-row-mapping.json"
BACKGROUND_RE = re.compile(r"对应文献[：:]?\s*无")
ROW_RE = re.compile(r"对应文献[：:]?\s*(\d+[a-z]?)")

FIELD_LABELS = (
    ("设计与E级（design and evidence level）", "设计与等级"),
    ("人群（population）", "人群"),
    ("干预/暴露（intervention/exposure）", "干预与暴露"),
    ("结局/效应量（outcomes/effects）", "结局与效应量"),
    ("偏倚与升降级（bias and upgrading/downgrading）", "偏倚备注"),
    ("与本文关系（relation to this review）", "与结论的关联"),
)


def field(text: str, label: str) -> str:
    m = re.search(rf"^- {re.escape(label)}[^\n]*", text, re.M)
    if not m:
        return ""
    line = m.group(0)
    val = line.split("：", 1)[1] if "：" in line else (line.split(":", 1)[1] if ":" in line else "")
    return re.sub(r"\s+", " ", val).strip()


def cell(value: str) -> str:
    return value.replace("|", "/").replace("\n", " ").strip()


def load_reference_rows(path: Path) -> dict:
    """引文表（行号即 §5 行号）→ {行号: 单条引文（去尾点）}；缺件／0 条即退出 2。"""
    if not path.is_file():
        print(f"缺输入：{REF_REL}（「文献」列的唯一取数来源，不自拼）", file=sys.stderr)
        raise SystemExit(2)
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*(\d+[a-z]?)\s*[.．]\s*(.+?)\s*$", line)
        if m:
            out[m.group(1)] = html.unescape(m.group(2).rstrip().rstrip("."))
    if not out:
        print(f"引文表解析到 0 条：{REF_REL}", file=sys.stderr)
        raise SystemExit(2)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="§5 七列表草稿（卡面投影＋引文表取数，不直写包内件）")
    parser.add_argument("--delivery", required=True)
    args = parser.parse_args(argv)
    delivery = Path(args.delivery)

    refs = load_reference_rows(delivery / REF_REL)
    rows: list[tuple[str, str]] = []
    skipped = []
    for path in sorted((delivery / package_parse.CARDS_REL).glob(f"*{package_parse.CARD_SUFFIX}")):
        text = path.read_text(encoding="utf-8")
        if BACKGROUND_RE.search(text):
            skipped.append(f"{path.stem}（背景卡）")
            continue
        m = ROW_RE.search(text)
        if not m:
            skipped.append(f"{path.stem}（无行号）")
            continue
        rows.append((m.group(1), text))
    rows.sort(key=lambda kv: package_parse.row_sort_key(kv[0]))

    missing = [num for num, _text in rows if num not in refs]
    if missing:
        print(f"引文表缺行号 {missing}：「文献」列无处可取，拒绝生成（不退回自拼）", file=sys.stderr)
        return 2

    lines = [
        "| 行号 | 文献 | 设计与等级 | 人群 | 干预与暴露 | 结局与效应量 | 偏倚备注 | 与结论的关联 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    mapping = {}
    for num, text in rows:
        mapping[num] = refs[num]
        lines.append("| " + " | ".join([
            str(num),
            cell(refs[num]),
            *(cell(field(text, label)) for label, _name in FIELD_LABELS),
        ]) + " |")
    (delivery / OUT_REL).write_text("## 5 证据表\n\n" + "\n".join(lines) + "\n",
                                    encoding="utf-8", newline="\n")
    (delivery / MAP_REL).write_text(json.dumps(
        {"note": "§5 草稿行↔引文表同号条目（「文献」列单源取数）",
         "rows": {num: f"reference-list.md:{num}" for num, _ in rows}},
        ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"§5 草稿：{len(rows)} 行 → {OUT_REL}（跳过 {len(skipped)}：{('、'.join(skipped[:3])) or '无'}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
