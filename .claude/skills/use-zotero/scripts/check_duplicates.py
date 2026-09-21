# -*- coding: utf-8 -*-
"""下载前入库预检（ADR-0020／ADR-0032）：DOI 清单 ⇒ DUP／DUP-GAP／NEW 行，供编排层剪下载队列
（队列＝研读选取集＝入库集：预检范围＝研读选取集条目，预检条数＝选取集条目数）。

When: 全文链要按 DOI 清单（如研读选取集（队列））取 PDF 之前；任何“添加前先查库”的批量场合。
Do:   uv run --isolated --no-project --with pyzotero python check_duplicates.py \
          --dois-file <清单>          # 一行一个 DOI；# 注释与空行跳过
      输出逐条三态与汇总：
        MODE probe|index（索引模式附扫描条数与构建秒数）
        DUP <doi> key=<key>        已在库且含正文附件 → 重复取消，剔出本轮下载（研读选取集成员已在库）
        DUP-GAP <doi> key=<key>    已在库但缺正文附件 → 保留下载，挂载指向既有条目
        NEW <doi>                  未在库 → 照常
        OK total=N dup=D gap=G new=W in Xs
      `in Xs` 自门构建起计（不含进程启动），对应性能契约（探针≤3s；含正文附件核的
      完整判定≤5s）。exit 0＝完成（重复不是失败）；预检异常 fail-closed：报错即中止、
      exit 1，不得当未命中继续。
Why:  下载前判库可连下载都省；单探针≈2.2s、命中含附件核≈4.3s；≥10 篇走进程内全库
      索引（一次≈21s）后逐篇 O(1)。门槛语义单源在 dedup_gate.py；规则见
      `references/local-api.md`，决策见 ADR-0020／ADR-0032。
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dedup_gate import PreflightGate, norm_doi  # noqa: E402 同族脚本跨件复用（门槛语义单源）
from pyzotero import zotero as zmod  # noqa: E402


def build_parser():
    ap = argparse.ArgumentParser(description="入库预检：DOI 清单 ⇒ DUP／DUP-GAP／NEW")
    ap.add_argument("--dois", nargs="*", default=[], help="DOI 清单")
    ap.add_argument("--dois-file", help="每行一个 DOI 的文本文件（# 注释与空行跳过）")
    ap.add_argument("--library-id", default="0", help="本地库固定传 0")
    ap.add_argument("--api-key", default=os.environ.get("ZOTERO_LOCAL_API_KEY"),
                    help="本地写 key，默认读 ZOTERO_LOCAL_API_KEY")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    dois = list(args.dois)
    if args.dois_file:
        dois += [l.strip() for l in open(args.dois_file, encoding="utf-8")
                 if l.strip() and not l.startswith("#")]
    seen, uniq = set(), []
    for d in dois:
        key = norm_doi(d) or d
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    dois = uniq
    if not dois:
        print("no DOIs given", file=sys.stderr)
        return 2

    z = zmod.Zotero(library_id=args.library_id, library_type="user", local=True,
                    local_api_key=args.api_key)
    t0 = time.monotonic()
    counts = {"DUP": 0, "DUP-GAP": 0, "NEW": 0}
    try:
        gate = PreflightGate(z, len(dois))
        suffix = f" | {gate.note}" if gate.note else ""
        if gate.mode == "index":
            print(f"MODE index scanned={gate.scanned} in {gate.build_s:.1f}s{suffix}")
        else:
            print(f"MODE probe{suffix}")
        for doi in dois:
            verdict, key = gate.check(doi)
            counts[verdict] += 1
            print(f"{verdict} {doi}" + (f" key={key}" if key else ""))
    except Exception as e:  # fail-closed（ADR-0020）：中止报错，不得当未命中
        print(f"PRECHECK-FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"OK total={len(dois)} dup={counts['DUP']} gap={counts['DUP-GAP']} "
          f"new={counts['NEW']} in {time.monotonic() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
