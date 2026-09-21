#!/usr/bin/env python
"""Step3 中断批收据恢复：断点检查点投影 + 快车道来源补读（做法见 references/protocols/step3-download-chain.md §6）。

When: 分片下载批被作业时限／崩溃中断、未写 batch_results.json 时（collect 无收据可认）。
Do:   uv run --isolated --no-project --with pymupdf python .claude/skills/med-lit-review/scripts/step3_recover_receipts.py \
          --delivery workspace/YYYY-MM-DD-<slug> [--progress-dir ~/.scansci-pdf/batch_progress] [--write]
      `--write` 才落盘；缺省与 `--dry-run` 等价（只打印计划）。

流程：① 从各片日志抓 batch_id（行形如 `Batch <12hex>`）；② 读检查点 <batch_id>.jsonl，按 DOI 合并
（成功且文件在场者优先）；③ 投影 `run/batches/<out-dir>/batch_results.reconstructed.json`（同 schema、
逐行 `_reconstructed`）；④ 对文件在场但无成功行的件补读来源：`.doi_index.json` → 文件名来源后缀 →
正文 DOI／题名前缀（∩ 池）；三者皆无记 `unrecorded`（不冒充来源）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>()\[\],;]+", re.I)
BATCH_RE = re.compile(r"Batch ([0-9a-f]{12})")
SUFFIX_SRC = ("LibGen", "OpenAIRE", "CORE", "CrossrefPage", "OpenAlexOA", "Sci-Hub",
              "FrontiersDirect", "PublisherPDF", "DOAJ", "EuropePMC", "SemanticScholar")


def norm_doi(v: str) -> str:
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", (v or "").strip()).lower().rstrip(".")


def norm_text(v: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (v or "").lower())


def head_text(pdf: pathlib.Path, pages: int = 3) -> str:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return ""
    try:
        doc = fitz.open(pdf)
        text = "\n".join(doc[i].get_text() for i in range(min(pages, doc.page_count)))
        doc.close()
        return text
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="中断批收据恢复（检查点投影＋来源补读）")
    ap.add_argument("--delivery", required=True)
    ap.add_argument("--progress-dir", default=str(pathlib.Path.home() / ".scansci-pdf" / "batch_progress"))
    ap.add_argument("--batches", default="run/batches")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    deliv = pathlib.Path(args.delivery)
    batches = deliv / args.batches
    progress = pathlib.Path(args.progress_dir)
    pool_file = deliv / "run" / "b1" / "step1-pools.json"
    pool: set[str] = set()
    titles: dict[str, str] = {}
    if pool_file.is_file():
        pools = json.loads(pool_file.read_text(encoding="utf-8"))
        for p in pools.get("pools", []):
            for r in p.get("results", []):
                if r.get("doi"):
                    d = norm_doi(r["doi"])
                    pool.add(d)
                    titles.setdefault(d, r.get("title") or "")

    # ① 片 → batch_id（从片日志抓）
    id_by_dir: dict[str, list[str]] = {}
    for log in sorted(batches.glob("slice-*.log")):
        m = re.match(r"slice-(\d+)[a-z]?\.log$", log.name)
        if not m:
            continue
        dirs = [d for d in (f"out-s{m.group(1)}",) if (batches / d).is_dir()]
        for d in dirs:
            ids = sorted(set(BATCH_RE.findall(log.read_text(encoding="utf-8", errors="replace"))))
            id_by_dir.setdefault(d, [])
            for i in ids:
                if i not in id_by_dir[d]:
                    id_by_dir[d].append(i)
    if not id_by_dir:
        print("未发现 out-sNN 目录或片日志：无片可恢复", file=sys.stderr)
        return 2

    total_added = 0
    for out_name, batch_ids in sorted(id_by_dir.items()):
        out = batches / out_name
        merged: dict[str, dict] = {}
        for bid in batch_ids:
            pf = progress / f"{bid}.jsonl"
            if not pf.is_file():
                continue
            for line in pf.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                doi = norm_doi(row.get("doi") or row.get("identifier") or "")
                if not doi:
                    continue
                base = pathlib.Path(str(row.get("file") or "").replace("\\", "/")).name
                row["_file_ok"] = bool(base) and (out / base).is_file()
                prev = merged.get(doi)
                better = prev is None or (row.get("success") and row["_file_ok"]
                                          and not (prev.get("success") and prev.get("_file_ok")))
                if better:
                    merged[doi] = row
        rows, have = [], set()
        for doi, row in sorted(merged.items()):
            ok = bool(row.get("success") and row.get("_file_ok"))
            rows.append({"identifier": row.get("identifier") or doi, "doi": row.get("doi") or doi,
                         "success": ok, "source": row.get("source") or "none",
                         "file": str(row.get("file") or ""), "_reconstructed": True})
            if ok:
                have.add(doi)

        # ② 快车道来源补读（文件在场但无成功行）
        idx = {}
        ip = out / ".doi_index.json"
        if ip.is_file():
            for doi, v in json.loads(ip.read_text(encoding="utf-8")).items():
                idx[norm_doi(doi)] = v
        added = 0
        for pdf in sorted(out.glob("*.pdf")):
            stem = pdf.stem
            doi, basis, src = None, "unrecorded", ""
            if stem.lower() in idx:
                doi, basis = stem.lower(), "index"
            else:
                m = re.match(r"^(10\.\d{4,9})_([a-z0-9.\-()]+)$", stem.lower())
                if m and f"{m.group(1)}/{m.group(2)}" in pool:
                    doi, basis = f"{m.group(1)}/{m.group(2)}", "filename"
            if doi is None and pool:
                head = head_text(pdf)
                for mm in DOI_RE.finditer(head):
                    d = norm_doi(mm.group(0).rstrip("."))
                    if d in pool:
                        doi, basis = d, "content"
                        break
                if doi is None and titles:
                    tn = norm_text(head)
                    for d, title in titles.items():
                        prefix = norm_text(title[:60])
                        if prefix and prefix in tn:
                            doi, basis = d, "content"
                            break
            if doi is None or doi in have:
                continue
            iv = idx.get(doi)
            if iv and pathlib.Path(str(iv.get("file", "")).replace("\\", "/")).name == pdf.name:
                src = str(iv.get("source") or "")
            if not src:
                for suf in SUFFIX_SRC:
                    if f"_{suf}" in stem:
                        src = suf
                        break
            if not src:
                basis = "unrecorded"
            rows.append({"identifier": doi, "doi": doi, "success": True,
                         "source": src or "unrecorded", "file": str(pdf).replace("\\", "/"),
                         "_reconstructed": True, "_source_basis": basis})
            have.add(doi)
            added += 1
        ok = sum(1 for r in rows if r["success"])
        print(f"{out_name}: rows={len(rows)} success={ok}（补读 {added}）batch_ids={batch_ids}")
        if args.write:
            payload = {"batch_key": out_name, "batch_ids": batch_ids, "reconstructed": True,
                       "note": "由工具自写的断点检查点（batch_progress/<batch_id>.jsonl）投影，并补读快车道来源；"
                               "与自然完成所写 batch_results.json 同 schema（做法见 references/protocols/step3-download-chain.md §6）",
                       "total": len(rows), "unique": len(rows), "succeeded": ok,
                       "failed": len(rows) - ok, "results": rows}
            (out / "batch_results.reconstructed.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        total_added += added
    print(f"完成：补读合计 {total_added}；{'已落盘' if args.write else 'dry-run（加 --write 落盘）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
