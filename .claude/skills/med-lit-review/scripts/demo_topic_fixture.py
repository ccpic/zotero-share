# -*- coding: utf-8 -*-
"""演示 topic 的忽略缓存再生器（验收演练用，不是管线）。

真实 topic 的忽略件由管线脚本产生：来源在网，字节不复现，`manifest.json` 的哈希是完整性钉子。
演示 topic 没有真实管线输出，本件按清单里记录的命令逐字节重建演练缓存，供
「擦除缓存 → 只按版内指针再生 → 哈希逐条复核」的验收演练实跑。

用法（工作目录＝仓库根）：

    uv run python .claude/skills/med-lit-review/scripts/demo_topic_fixture.py --topic <topic 目录> --class <类名>

类名与 topic 的 `manifest.json` 的 `regeneration[].class` 一一对应；未登记的 topic 或类名报错退出（码 2）。
唯一例外（监管原文 PDF）不在再生范围：它自带字节，靠版本记录的 `archive_sha256` 证明。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TITLE1 = "Demo Paper One: LDL-C lowering with demo drug A"
TITLE2 = "Demo Paper Two: MACE outcomes with demo drug A"

# 每个演示 topic 的参数：slug（manifest 的 topic.slug）、label（件内自称）、
# 两条队列条目的 DOI 尾号与 Zotero 条目键、以及本 topic 独有的缓存件（extras）。
TOPICS = {
    "2026-09-17-exclusion-gate-drill": {
        "slug": "exclusion-gate-drill",
        "label": "演示骨架",
        "num": ("001", "002"),
        "item_keys": ("DEMO0001", "DEMO0002"),
        "extras": frozenset({"driver_log", "review_json", "gate_report", "revision_check"}),
    },
    "2026-09-17-pcsk9-ascvd": {
        "slug": "pcsk9-ascvd",
        "label": "演示件",
        "num": ("101", "102"),
        "item_keys": ("DEMO1001", "DEMO1002"),
        "extras": frozenset(),
    },
}


def _json(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _text(text: str) -> bytes:
    return text.encode("utf-8")


def _minimal_pdf(title: str, doi: str) -> bytes:
    """最小可读 PDF（单页、Helvetica、三行文本）：xref 按实际偏移计算。"""
    lines = [title, f"DOI: {doi}", "Demo fixture placeholder for a downloaded full text \\(ignored cache; never versioned\\)."]
    stream = "BT /F1 11 Tf 40 720 Td 14 TL\n" + "".join(f"({line}) Tj T*\n" for line in lines) + "ET\n"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}endstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = [b"%PDF-1.4\n"]
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(sum(len(part) for part in out))
        out.append(f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1"))
    xref_at = sum(len(part) for part in out)
    xref = [b"xref\n0 6\n", b"0000000000 65535 f \n"]
    xref += [f"{offset:010d} 00000 n \n".encode("latin-1") for offset in offsets]
    xref.append(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode("latin-1"))
    return b"".join(out + xref)


def build(topic: dict, topic_dir: str) -> dict[str, dict[str, bytes]]:
    """返回 {class: {topic 相对路径: 字节}}；路径与 manifest.json 的 artifacts.path 同形。"""
    slug, label = topic["slug"], topic["label"]
    n1, n2 = topic["num"]
    k1, k2 = topic["item_keys"]
    doi1, doi2 = f"10.0000/demo-{n1}", f"10.0000/demo-{n2}"
    du1, du2 = doi1.replace("/", "_"), doi2.replace("/", "_")
    extras = topic["extras"]
    classes: dict[str, dict[str, bytes]] = {}

    classes["text"] = {
        f"run/text/{du1}.txt": _text(
            f"{TITLE1}（{label}抽取文本，被忽略）\nDOI: {doi1}\n"
            f"Abstract: 本件为演示用抽取文本占位，不承载真实结论；抽取文本是最大体量的忽略类（真实运行 6.5MB 量级）。\n"
        ),
        f"run/text/{du2}.txt": _text(
            f"{TITLE2}（{label}抽取文本，被忽略）\nDOI: {doi2}\n"
            f"Abstract: 本件为演示用抽取文本占位，不承载真实结论；真实运行时逐 PDF 抽取并按 DOI-slug 命名。\n"
        ),
    }

    classes["raw_pool_and_intermediates"] = {
        "run/b1/dedupe-report.json": _json(
            {
                "topic": slug,
                "rounds": [
                    {"round": 1, "key": "doi_normalized", "removed": []},
                    {"round": 2, "key": "title_normalized", "removed": []},
                    {"round": 3, "key": "suspected_manual_review", "removed": []},
                ],
                "removed": [],
                "kept": 2,
                "note": f"{label}：逐条去重轨迹为演示数据；本件被忽略，版内只留 screening-log 计数与 manifest 再生链",
            }
        ),
        "run/b1/sort-report.json": _json(
            {
                "topic": slug,
                "dimensions": ["evidence_level", "recency", "design", "population_fit"],
                "consumes_grades": False,
                "order": [doi1, doi2],
                "note": f"{label}：四维排序不消费等级；本件被忽略，再生见 manifest.json",
            }
        ),
        "run/b1/step1-pools.json": _json(
            {
                "strategy": f"六族检索式 x 双窗（{label}，非真实执行）",
                "providers": ["pubmed", "openalex"],
                "limit_per_query": 50,
                "frozen_at": "2026-09-17",
                "pools": [
                    {
                        "family": "1 药名族",
                        "window": "full",
                        "provider": "pubmed",
                        "count": 1,
                        "items": [{"doi": doi1, "title": TITLE1}],
                    },
                    {
                        "family": "2 通用名族",
                        "window": "full",
                        "provider": "openalex",
                        "count": 1,
                        "items": [{"doi": doi2, "title": TITLE2}],
                    },
                ],
                "note": f"{label}：条目为演示数据；真实运行只落 provider 原样返回字段，未知日期保持未知；本件被忽略，再生见 manifest.json",
            }
        ),
    }

    batches: dict[str, bytes] = {
        "run/batches/out-chunk-00/batch-results.json": _json(
            {
                "chunk": "out-chunk-00",
                "queue": "doi-list.txt",
                "results": [
                    {"doi": doi1, "success": True, "source": "demo-source", "file": f"{du1}.pdf"},
                    {"doi": doi2, "success": False, "reason": "no source hit (demo)"},
                ],
                "note": f"{label}：批处理中间结果为演示数据；本件被忽略，再生见 manifest.json",
            }
        ),
    }
    if "driver_log" in extras:
        batches["run/batches-driver.log"] = _text(
            f"2026-09-17T00:00:00Z demo driver start（{label}）\n"
            f"2026-09-17T00:00:01Z chunk out-chunk-00 → 1 成功 / 1 未取得\n"
            f"2026-09-17T00:00:02Z demo driver done\n"
        )
    classes["batch_intermediates"] = batches

    classes["queues"] = {
        "run/queues/url15.txt": _text(
            f"# url15（{label}：中间队列，被忽略）\nhttps://example.invalid/demo-{n1}\nhttps://example.invalid/demo-{n2}\n"
        ),
    }

    classes["receipts"] = {
        "run/download-results.json": _json(
            {
                "queue": "doi-list.txt",
                "total": 2,
                "succeeded": 1,
                "adopted": 1,
                "grey_hits": 0,
                "batches_read": ["batches", "out-chunk-00"],
                "entries": [
                    {
                        "identifier": doi1,
                        "doi": doi1,
                        "success": True,
                        "tool_success": True,
                        "adopted": True,
                        "source": "demo-source",
                        "reason": "",
                        "file": f"run/fulltext/{du1}.pdf",
                        "grey_hit": False,
                        "identity_check": "match",
                    },
                    {
                        "identifier": doi2,
                        "doi": doi2,
                        "success": False,
                        "tool_success": True,
                        "adopted": False,
                        "source": None,
                        "reason": "本轮未取得任何来源的全文（演示）",
                        "file": None,
                        "grey_hit": False,
                        "identity_check": None,
                    },
                ],
                "note": f"{label}：本件被忽略，版内投影为 bucketing-and-sources.md 逐条台账行",
            }
        ),
        "run/fulltext-identity.json": _json(
            {
                "checked_at": "2026-09-17",
                "method": "抽取 PDF 前 3 页文本，检查目标 DOI 或归一题名前 60 字是否命中；只做机械命中，语义一致性交人工",
                "counts": {"checked": 1, "match": 1, "mismatch": 0, "unreadable": 0},
                "entries": [
                    {
                        "file": f"{du1}.pdf",
                        "doi": doi1,
                        "title": TITLE1,
                        "source": "demo-source",
                        "verdict": "match",
                        "doi_in_head": True,
                        "title_in_head": True,
                        "head_first_lines": [TITLE1, f"DOI: {doi1}", f"{label}：本件为完整证据留档位（被忽略）"],
                    }
                ],
                "note": f"{label}：完整身份证据留本件（被忽略），版内只在台账行记短串，manifest 记本件哈希",
            }
        ),
        "run/placement-write-report.json": _json(
            {
                "confirmed_by": f"{label}：无真实确认发生",
                "confirmation": "演示：不写库",
                "allowed_collections": {"DEMOCOLL1": "演示收藏集一", "DEMOCOLL2": "演示收藏集二"},
                "test_root": "DEMOROOT",
                "topic_collection": {"name": f"med-lit-review/{topic_dir}", "mode": "演示：不写库", "mounted": 0, "total": 2},
                "counts": {"plan": 2, "written": 2, "rechecked_consistent": 2, "preexisting_not_rewritten": 0},
                "preexisting_items": [],
                "note": f"{label}：本件被忽略，版内投影为 placement-plan.md 表尾计数行（含本次合集行）",
            }
        ),
        "run/zotero-snapshot.json": _json(
            {
                "read_only": True,
                "captured_at": "2026-09-17",
                "api": "本机本地 API（只读 GET）",
                "counts": {"collections": 2, "items": 2, "items_with_doi": 2, "items_without_doi": 0},
                "transport_errors": {"collections": None, "items": None},
                "items": [
                    {"key": k1, "doi": doi1, "collections": ["DEMOCOLL1"], "children": ["DEMOATCH1"]},
                    {"key": k2, "doi": doi2, "collections": ["DEMOCOLL2"], "children": []},
                ],
                "note": f"{label}：库快照全库 inventory 不进版（被忽略）",
            }
        ),
    }

    classes["downloaded_fulltexts"] = {f"run/fulltext/{du1}.pdf": _minimal_pdf(TITLE1, doi1)}

    review: dict[str, bytes] = {
        "run/review-text.txt": _text(
            "review_knowledge 演示输出（被忽略缓存）\n"
            "规则集：R1-01..R4-02（15 条子集，error/warning/info 三档）\n"
            "findings: 0 error, 0 warning, 0 info\n"
            f"summary: error=0 warning=0 info=0（{label}：未真实执行）\n"
        ),
    }
    if "review_json" in extras:
        review["run/review-json.json"] = _json(
            {
                "script": "review_knowledge.py",
                "format": "json",
                "cur_year": 2026,
                "lexicon": "references/lexicon/term-lexicon.yaml",
                "findings": [],
                "summary": {"error": 0, "warning": 0, "info": 0},
                "exit_code": 0,
                "note": f"{label}：本件被忽略；再生命令见 manifest.json",
            }
        )
    classes["review_texts"] = review

    gate: dict[str, bytes] = {
        "run/gate/assertion-results.json": _json(
            {
                "run_dir": f"workspace/{topic_dir}",
                "assertions_total": 0,
                "hard_gates": 0,
                "passed": 0,
                "failed": [],
                "soft_gaps": [],
                "execution_errors": [],
                "exit_code": 0,
                "note": f"{label}：门禁执行器未真实运行（0 断言）；真实运行结果落本件（被忽略），版内只留 gate-summary.md verdict 头",
            }
        ),
    }
    if "gate_report" in extras:
        gate["run/gate/gate-report.md"] = _text(
            f"# 门禁全量报告（{label}，被忽略）\n\n"
            "- 断言总数：0（演示件未真实执行门禁执行器）\n"
            "- 判定：演示通过；硬门禁 0/0；软项未通过 0\n"
            "- 退出码：0\n"
            "- 说明：本件为 full gate 全量输出位（被忽略缓存）；版内 verdict 头在 `gate-summary.md`，钻取经 `manifest.json` 的 full gate 指针。\n"
        )
    classes["full_gate"] = gate

    classes["per_run_scripts"] = {
        "run/scripts/demo-pipeline.py": _text(
            "# -*- coding: utf-8 -*-\n"
            f'"""per-run 运行脚本占位（{label}，被忽略）。\n\n'
            "权威脚本家为 skill 脚本区；本目录内的副本一律忽略，manifest.json 的再生命令不引用本副本。\n"
            '"""\n'
            "from __future__ import annotations\n\n\n"
            "def main() -> None:\n"
            f'    print("{label}：本脚本不执行真实管线")\n\n\n'
            'if __name__ == "__main__":\n'
            "    main()\n"
        ),
    }

    if "revision_check" in extras:
        classes["regulatory_receipts"] = {
            "run/b2/ema-revision-check.json": _json(
                {
                    "source": "EMA EPAR 官方页面（Authorisation details 段）",
                    "revision_live": "28",
                    "last_updated_live": "04/09/2026",
                    "spc_pdf_has_revision_string": False,
                    "spc_pdf_created": "D:20260817180744+05'30'",
                    "note": "SPC 原文 PDF 正文与元数据均不含 revision 串，revision 取官方页面读数；页面读数与冻结钉子一致",
                }
            ),
        }

    return classes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="演示 topic 的忽略缓存再生器（验收演练用）")
    parser.add_argument("--topic", required=True, help="topic 目录（仓库根相对路径，如 workspace/2026-09-17-<slug>）")
    parser.add_argument("--class", dest="cls", required=True, help="regen 类名（与 manifest.json 的 regeneration[].class 一致）")
    args = parser.parse_args(argv)

    root = Path(args.topic)
    topic = TOPICS.get(root.name)
    if topic is None:
        print(f"demo_topic_fixture: 未登记的演示 topic：{root.name}（已登记：{'、'.join(sorted(TOPICS))}）", file=sys.stderr)
        return 2

    classes = build(topic, root.name)
    files = classes.get(args.cls)
    if files is None:
        print(f"demo_topic_fixture: {root.name} 无此类：{args.cls}（有：{'、'.join(sorted(classes))}）", file=sys.stderr)
        return 2

    for rel, payload in sorted(files.items()):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    print(f"demo_topic_fixture: {root.name} class={args.cls} → {len(files)} 件已再生")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
