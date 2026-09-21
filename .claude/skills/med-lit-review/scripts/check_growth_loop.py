"""生长循环队列覆盖检查（事后确定性检查，供门禁 A-08 与人工复核调用）。

契约（references/protocols/term-lexicon-protocol.md §6）：七处触发只检出候选进候选队列；
`term_expand.py --log` 报「未命中」即当场 `--append`（命中无边与有扩展者不需落队）；
**筛查日志文字行不算已检出，以真源或队列条目为准**；本交付声明的词所在家族若 ≥2 条术语且全体无边、
又未声明 `no_edge` → 机械生长，判红（ADR-0022 修订；逐条口径）。

断言：交付件 `agent/screening-log.md` 的「术语词表引用与候选」节内各表（按表头认列）
  - 「查询词」列 → 若该词状态为「未命中」（词表三态之 miss），必须已落真源或队列
  - 「候选词」列 → 必须已落真源或队列
  - 逐条红线 → 本交付声明且已落库的术语不得无边无声明（无边须 `--no-edge`）
结构不可解析（文件缺失／节缺失／零行）时不判绿——不能验证即失败，防真空通过。

用法：uv run python scripts/check_growth_loop.py <delivery_dir> [--json]
      uv run python scripts/check_growth_loop.py --selftest    # 机制自检（沙箱，不碰真源）
退出码：0 = 绿；1 = 红（有声明未落真源／队列、已落术语无边未声明、结构不可解析，或自检不过）。
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
LEXICON_SOURCE = SKILL_DIR / "references/lexicon/term-lexicon.yaml"
LEXICON = SKILL_DIR / "references/lexicon/term-lexicon.candidates.yaml"
EXPAND = SKILL_DIR / "scripts/term_expand.py"
if str(SKILL_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(SKILL_DIR / "scripts"))  # YAML 子集解析只有一处实现（同 review_knowledge）
import term_expand as te  # noqa: E402

SECTION = "术语词表引用与候选"
SKIP = {"", "—", "无", "查询词", "候选词", "触发点", "方向", "带出词", "记录处", "信号", "去向"}


def table_rows(delivery: Path):
    """产出 (表头, 数据行) —— 「术语词表引用与候选」节内每张 markdown 表。"""
    p = delivery / "agent" / "screening-log.md"
    if not p.exists():
        return
    lines = p.read_text(encoding="utf-8").splitlines()
    start = next((i for i, l in enumerate(lines) if SECTION in l), None)
    if start is None:
        return
    section = []
    for l in lines[start + 1:]:
        if l.startswith("## "):
            break
        section.append(l)

    def cells_of(line):
        return [c.strip() for c in line.strip().strip("|").split("|")]

    def is_sep(line):
        cs = cells_of(line)
        return bool(cs) and all(set(c) <= set("-: ") and c for c in cs)

    header, rows = None, []
    for i, l in enumerate(section):
        if not l.strip().startswith("|"):
            continue
        cs = cells_of(l)
        if is_sep(l):
            continue
        if header is None:
            header = cs
            continue
        nxt = section[i + 1] if i + 1 < len(section) else ""
        if nxt.startswith("|") and is_sep(nxt):
            yield header, rows
            header, rows = cs, []
            continue
        rows.append(cs)
    if header is not None:
        yield header, rows


def clean(cell: str) -> str:
    cell = re.sub(r"\*\*|`", "", cell)
    cell = re.sub(r"（[^）]*）|\([^)]*\)", "", cell)
    return cell.strip()


def status_of(term: str):
    """词表三态 expanded／hit_no_expansion／miss；None = 脚本执行失败。"""
    r = subprocess.run([sys.executable, str(EXPAND), term, "--format", "json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout).get("status")
    except (ValueError, AttributeError):
        return None


def buffered():
    if not LEXICON.exists():
        return set()
    return set(re.findall(r"^- term:\s*(.+?)\s*$",
                          LEXICON.read_text(encoding="utf-8"), re.M))


def truth_terms():
    """真源现值里的本形集——晋升后条目从队列出账，故「已落」须并读真源（terms 下逐条缩进）。"""
    if not LEXICON_SOURCE.exists():
        return set()
    return set(re.findall(r"^\s*- term:\s*(.+?)\s*$",
                          LEXICON_SOURCE.read_text(encoding="utf-8"), re.M))


def gap_rows(delivery: Path):
    """返回 (check 行, 缺口词列表, 结构是否可解析, 本表声明的词列表)。"""
    buf = buffered()
    truth = truth_terms()
    checks = []
    for header, rows in table_rows(delivery):
        for col, rule in (("查询词", "when-miss"), ("候选词", "always")):
            if col not in header:
                continue
            idx = header.index(col)
            for cells in rows:
                if idx >= len(cells):
                    continue
                for term in re.split(r"[/、,；;]", clean(cells[idx])):
                    term = term.strip()
                    if term and term not in SKIP:
                        checks.append((col, term, rule))

    rows, gaps = [], []
    for src, term, rule in checks:
        status = status_of(term) if rule == "when-miss" else None
        if rule == "when-miss" and status is None:
            rows.append({"col": src, "term": term, "state": "error",
                         "note": "term_expand 执行失败"})
            gaps.append(term)
            continue
        need = True if rule == "always" else (status == "miss")
        in_queue, in_truth = term in buf, term in truth
        state = "RED" if (need and not (in_queue or in_truth)) else "GREEN"
        rows.append({"col": src, "term": term, "status": status,
                     "buffered": in_queue, "in_truth": in_truth,
                     "need_buffer": need, "state": state})
        if state == "RED":
            gaps.append(term)
    return rows, gaps, bool(checks), [term for _, term, _ in checks]


def edgeless_terms(declared):
    """机械生长红线（逐条口径）：本交付声明且已落真源或队列的术语，**无边又未声明 no_edge** 即违规。

    逐条口径覆盖单成员家族（旧家族口径 `家族 ≥2 且全体无边` 漏掉「新族只登记一个词」的情形）；
    只查本交付声明的词——队列是 skill 级共享队列，别家未晋升的候选不该把本交付拖红。
    真源不可解析即视为违规（fail-closed）。
    """
    try:
        doc = te.load_yaml_subset(str(LEXICON_SOURCE))
        queue = te.read_buffer(str(LEXICON))
    except (OSError, ValueError, UnicodeDecodeError) as e:
        return [{"term": "（真源不可解析）", "where": str(e)[:120]}]
    record = {}
    for t in doc.get("terms") or []:
        if isinstance(t, dict) and t.get("term"):
            record.setdefault(t["term"], {"edges": bool(t.get("relations")),
                                          "no_edge": bool(t.get("no_edge")), "where": "真源"})
    for e in queue:
        if e.get("term"):
            record.setdefault(e["term"], {"edges": bool(e.get("edges")),
                                          "no_edge": bool(e.get("no_edge")), "where": "队列"})
    return [{"term": w, "where": record[w]["where"]}
            for w in dict.fromkeys(declared)
            if w in record and not record[w]["edges"] and not record[w]["no_edge"]]


def selftest() -> int:
    """机制自检：沙箱副本上「--append --edge／--no-edge → 晋升 → --log 扩展」全链必须通。

    这是「只长词不长边」故障（2026-09-18）的回归测试：边的写入入口一旦丢失或失效，本条即红。
    """
    global LEXICON_SOURCE, LEXICON  # 沙箱回归段把两个常量指向副本，结束即还原
    tmp = Path(tempfile.mkdtemp(prefix="lexselftest-"))
    lex, queue = tmp / "lex.yaml", tmp / "queue.yaml"
    shutil.copy(LEXICON_SOURCE, lex)
    queue.write_text("", encoding="utf-8")

    def call(*args):
        r = subprocess.run([sys.executable, str(EXPAND), *args, "--lexicon", str(lex),
                            "--buffer", str(queue), "--no-cache"],
                           capture_output=True, text=True, encoding="utf-8")
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    appends = (
        ("自检词甲", ["--family", "自检家族", "--edge", "hierarchy:自检词乙:⊃"]),
        ("自检词乙", ["--family", "自检家族", "--canonical", "自检词甲", "--edge", "hierarchy:自检词甲:⊂"]),
        ("自检词丙", ["--family", "自检家族", "--canonical", "自检词甲", "--edge", "synonym:自检词甲:="]),
        ("自检词丁", ["--family", "自检家族", "--no-edge"]),
    )
    for term, opts in appends:
        rc, out = call(term, "--append", *opts, "--source", "自检")
        if rc != 0 or "候选已入队" not in out:
            print(f"SELFTEST RED：--append 失败（{term}）：{out.strip()[:200]}")
            return 1
    rc, out = call("--promote-all-candidates")
    if rc != 0 or "晋升完成" not in out:
        print(f"SELFTEST RED：晋升失败：{out.strip()[:200]}")
        return 1
    left = te.read_buffer(str(queue))
    if left:
        print(f"SELFTEST RED：晋升未出账：{left}")
        return 1
    for q, want in (("自检词甲", "自检词乙"), ("自检词乙", "自检词甲"), ("自检词丙", "自检词甲")):
        rc, out = call(q, "--log")
        if rc != 0 or want not in out:
            print(f"SELFTEST RED：晋升后 --log {q} 未带出 {want}：{out.strip()[:200]}")
            return 1
    doc = te.load_yaml_subset(str(lex))
    by_term = {t.get("term"): t for t in doc.get("terms") or [] if isinstance(t, dict)}
    if not (by_term.get("自检词丁") or {}).get("no_edge"):
        print("SELFTEST RED：no_edge 未随晋升落真源")
        return 1
    if (by_term.get("自检词丙") or {}).get("canonical") != "自检词甲":
        print("SELFTEST RED：canonical 未随晋升落真源")
        return 1
    # 逐条红线回归（模块常量指向沙箱）：无边未声明 → 抓；no_edge 已声明 → 不误抓
    saved = (LEXICON_SOURCE, LEXICON)
    try:
        LEXICON_SOURCE, LEXICON = lex, queue
        if edgeless_terms(["自检词丁"]):
            print("SELFTEST RED：已声明 no_edge 的术语被误判为机械生长")
            return 1
        rc, out = call("自检词戊", "--append", "--family", "自检家族", "--source", "自检")
        if rc != 0 or "候选已入队" not in out or "--no-edge" not in out:
            print(f"SELFTEST RED：入队回执缺「生成侧 nudge」：{out.strip()[:200]}")
            return 1
        if not edgeless_terms(["自检词戊"]):
            print("SELFTEST RED：无边未声明的术语未被逐条红线抓到")
            return 1
    finally:
        LEXICON_SOURCE, LEXICON = saved
    print("SELFTEST GREEN：边入口／晋升出账／扩展／无边与主名／逐条红线 全链通过")
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    as_json = "--json" in sys.argv
    if "--selftest" in sys.argv:
        return selftest()
    if len(args) != 1:
        print("usage: check_growth_loop.py <delivery_dir> [--json] ｜ check_growth_loop.py --selftest",
              file=sys.stderr)
        return 2
    delivery = Path(args[0])
    rows, gaps, parsed, declared = gap_rows(delivery)
    bad_terms = edgeless_terms(declared)

    if not parsed:
        result = {"verdict": "RED", "reason": "structure_unverifiable",
                  "message": "未解析到「术语词表引用与候选」表的任何行"
                             "（文件缺失／节缺失／无候选行）——不能验证即不判绿",
                  "gaps": [], "edgeless_terms": bad_terms, "rows": []}
    else:
        result = {"verdict": "RED" if (gaps or bad_terms) else "GREEN",
                  "reason": ("misses_not_recorded" if gaps
                             else ("edgeless_terms" if bad_terms else "ok")),
                  "gaps": gaps, "edgeless_terms": bad_terms, "rows": rows}

    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(f"delivery={delivery}")
        if not parsed:
            print(f"  RED   [结构] {result['message']}")
        for r in rows:
            print(f"  {r['state']:5} [{r['col']}] {r['term']}: "
                  f"状态={r.get('status')}｜队列命中={r.get('buffered')}"
                  f"｜真源命中={r.get('in_truth')}｜需落={r.get('need_buffer')}")
        for t in bad_terms:
            print(f"  RED   [术语] {t['term']}（{t['where']}）：无边且未声明 no_edge"
                  f"——补 --edge，或 --no-edge 显式声明")
        print(f"\nRESULT: {result['verdict']} | 落真源／队列缺口={len(gaps)} {gaps}"
              f" | 无边无声明术语={[t['term'] for t in bad_terms]}")
    return 0 if result["verdict"] == "GREEN" else 1


if __name__ == "__main__":
    raise SystemExit(main())
