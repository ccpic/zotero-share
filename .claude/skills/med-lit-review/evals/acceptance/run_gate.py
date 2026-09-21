# -*- coding: utf-8 -*-
"""固定选题端到端质量门禁：锚点锁定 + 分步断言 + 硬门禁／软项判定 + 中文报告。

When: 每次端到端验收跑（含隔离盲测）跑完后，对这轮的产物做机器可断言的验收。
Why:  门禁只看可观察产物（知识包、日志、报告、脚本 JSON、下载收据、走查矩阵）；锚点用包含断言，
      计数／排序／被引用容差或顺序断言，provider 漂移写漂移日志而不是判假失败；
      硬门禁全过且软项缺口全披露才算过。
Do:   uv run python .claude/skills/med-lit-review/evals/acceptance/run_gate.py \
          --run <验收跑目录> --out <结果目录> [--no-live] [--baseline <上次的 assertion-results.json>] \
          [--walkthrough <HITL 走查校验脚本路径>]
      子进程用当前解释器直接调 skill 内脚本（不嵌套 uv），联网核对只走标准库。
      校验脚本一次执行、两种渲染：同进程走 `review_package`（`__script__` 与 `__text_run__` 同源同一轮
      findings），分步断言 F-08 记实测执行次数与两渲染对账；脚本自身命令行契约不变。

锚点锁：逐条核对结果写结果目录的 anchor-lock.json；复用条件与留痕形态见 `anchors.json` `rule.lock`
      与 `references/protocols/workspace-guide.md` §14.2（本文件只实现，不复述口径）。

退出码：0 = 硬门禁全过且软项缺口全披露；1 = 硬门禁失败、软项缺口未披露、断言执行错误或联网独立核对缺位；2 = 用法或输入缺失。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parents[1]
ANCHORS_PATH = HERE / "anchors.json"
# 共享解析模块（卡面／包面解析一处实现；另一消费方＝`scripts/check_delivery.py`）：
# 按脚本自身 skill 家导入——副本目录内的执行体配同副本的解析器（口径见 `scripts/package_parse.py` 文件头）。
if str(SKILL / "scripts") not in sys.path:
    sys.path.insert(0, str(SKILL / "scripts"))
import package_parse  # noqa: E402  （须先补 sys.path 才能导入）

CJK = re.compile(r"[\u4e00-\u9fff]")
DOI_RE = package_parse.DOI_RE
GREY_SOURCES = ("sci-hub", "scihub", "libgen", "z-lib", "zlibrary", "annas-archive")
CALIBER_RE = re.compile(r"口径版本[：:]\s*`?([a-z0-9_-]+)`?")
CURRENT_CALIBER = "post-0006-fastest"
CALIBER_MEANING = {
    "pre-0006-oa_first": "历史口径（ADR-0006 之前：灰色命中不采用），旧口径行不判现行失败",
    "post-0006-fastest": "现行口径（ADR-0006：灰色竞速采用 + 如实报告）",
}
LOCK_WINDOW_HOURS = 168  # 锚点锁复用窗口（workspace-guide §14.2：锁龄自原锁定时刻算，复用不顺延）
LOCK_REUSE_NOTE = "锚点锁复用"  # 复用留痕说明行前缀：run_lock 产出，main 按它把锁龄与原锁定时刻打到 stdout
REUSE_DETAIL_RE = re.compile(r"^复用（锁龄 [\d.]+h）＋原锁定 \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}：")
TOPIC_CALIBER_RE = re.compile(r"本次合集口径[：:]\s*`?([a-z0-9_-]+)`?")
CURRENT_TOPIC_CALIBER = "present"
TOPIC_CALIBER_MEANING = {
    "absent": "历史口径（ADR-0013 之前：无本次合集列），旧口径包不判现行失败",
    "present": "现行口径（ADR-0013：默认创建本次合集＋双挂）",
}
# 摘要列「已填：来源」口径表（ADR-0021 取数次序；库内＝条目本已有 abstractNote、本轮未取数），计数行分源与逐条来源共用一份
ABSTRACT_SOURCES = ("table", "crossref", "pubmed", "openalex", "库内")
ABSTRACT_SOURCES_RE = "|".join(ABSTRACT_SOURCES)
ABSTRACT_COUNT_RE = re.compile(
    r"摘要[：:]\s*已填\s*(\d+)\s*[（(]([^）)]*)[）)]\s*[／/]\s*未填\s*(\d+)\s*[／/]\s*不适用\s*(\d+)"
)
ABSTRACT_CALIBER_RE = re.compile(r"摘要口径[：:]\s*`?([a-z0-9_-]+)`?")
CURRENT_ABSTRACT_CALIBER = "present"
ABSTRACT_CALIBER_MEANING = {
    "absent": "历史口径（ADR-0021 之前：落点报告无摘要列），旧口径包不判现行失败",
    "present": "现行口径（ADR-0021：落点报告带摘要列与摘要计数行）",
}
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|apikey|cookie|password|passwd|secret|token|credential)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9]{10,}"),
    re.compile(r"\b(?:https?|ftp)://[^/\s:@]+:[^/\s@]+@"),
    re.compile(r"\b[CDEFGH]:\\[^\\/\s\"']"),  # 本机盘符绝对路径（不含 JSON 里 \n 之类的转义）
    re.compile(r"\b[CDEFGH]:\\\\[^\\/\s\"']"),  # JSON 转义形态的同一路径（C:\\Users\\…），单独抓防漏报
    re.compile(r"(?i)\b(webvpn|carsi|ezproxy)\b"),
    re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}"),
)
TEXT_SUFFIXES = (".md", ".json", ".txt", ".yaml", ".yml", ".py", ".csv", ".tsv", ".html")
# W-13 机密面归一化（人读主件纳入 G-06 面）：扫描前剥离样式／脚本块、内联数据 URI 与 CSS `url(...)`
HTML_STRIP_RE = re.compile(r"(?is)<(?:style|script)\b[^>]*>.*?</(?:style|script)\s*>")
DATA_URI_RE = re.compile(r"(?is)data:[^\"'\s)]{1,4000}")
CSS_URL_RE = re.compile(r"(?is)url\([^)]{0,4000}\)")
TOOL_NAMES = ("RoB 2", "ROBINS-I", "AMSTAR-2", "AGREE II", "JBI", "CINeMA")
UA = {"User-Agent": "med-lit-review-acceptance-gate/1.0"}
REVIEW_TIMEOUT_SECONDS = 300  # 同进程校验的有界线（沿原 subprocess.run(timeout=300)）
# ---------------------------------------------------------------- 冻结口径（账类断言／分量口径／排序面）
LEDGER_PATH = "run/b1/coverage-ledger.json"  # 覆盖账在交付内的落点（ADR-0023，run/b1/ 忽略位）
BUCKET_REPORT = "run/b1/bucketing-report.md"  # 四桶报告（Step2 选取步产出，行集＝研读选取集；B-09 读件，ADR-0032 §3）
# ADR-0023 账字段集（含 ADR-0024 增补的 `filtered`／`filtered_pointer`、ADR-0024 配套票 03 增补的机制贡献归因读数
# 与 ADR-0032 的第九块「入库集守恒」）：
# 块 → 冻结名。子集语义＝冻结名须在场；note 可选、pointer 必在（两者单列核；`*_pointer` 形态同核）。
LEDGER_BLOCK_FIELDS = {
    "citation_expansion": ("ran", "seed_head", "seed_judged", "directions", "channels", "new_before_dedupe",
                           "new_after_dedupe", "stop_reason", "filtered", "filtered_pointer", "gap_reasons",
                           "contributed_final_count", "contributed_queue_count", "contributed_queue_kinds",
                           "contributed_queue_origins", "contribution_pointer"),
    "iterative_gap_fill": ("ran", "entity_checklist_count", "gap_entity_count", "waves", "query_count",
                           "new_before_dedupe", "new_after_dedupe", "closure", "unclosed_entities",
                           "contributed_final_count", "contributed_queue_count", "contributed_queue_kinds",
                           "contributed_queue_origins", "contribution_pointer"),
    "high_cited_absence": ("ran", "queries", "absent_total", "absent_relevant", "absent_off_topic",
                           "absent_out_of_window", "user_disposition"),
    "classic_segment": ("checked", "found", "unrecovered"),
    "source_failure": ("failed_cell_count", "recovered_cell_count", "cells"),
    "cap_truncation": ("truncated_cell_count", "by_source_type", "cap_hit_unknown_count"),
    "date_filter": ("date_unknown_candidate_count", "out_of_window_candidate_count"),
    "source_declaration": ("sources", "not_included", "gs_enabled", "identity_rejected_count"),
    # ADR-0032：第九块由「队列截断」改「入库集守恒」（块名 queue → selection，字段表冻结在 ADR-0032 执行契约 §5）
    "selection": ("selected_total", "must_take_count", "rank_fill_count", "unselected_total",
                  "unselected_by_rank_band", "unselected_by_origin", "classic_segment_cut"),
}
# 逐行块的内层冻结名（ADR-0023 逐项列出者；`unclosed_entities` 未冻内层名，只核在场）。
LEDGER_ROW_FIELDS = {
    "channels": ("channel", "requests", "hits"),
    "queries": ("query", "depth"),
    "unrecovered": ("id", "reason"),
    "cells": ("pool", "provider", "error", "attempts", "recovered", "net_effect"),
    "not_included": ("source", "reason"),
}
# 覆盖 fixture 场景集（ADR-0023 抽查轨 1）：定义与核出读数的路径／字段契约见 topic.md（供票 08 对齐）。
DEFAULT_COVERAGE_FIXTURES = SKILL / "evals" / "coverage"
COVERAGE_CHECK_PATH = "run/b1/coverage-check.json"
FIXTURE_KINDS = ("known-hit", "classic", "high-cited")  # known-hit → A-12；classic／high-cited → A-13
FIXTURE_SCENARIO_FIELDS = ("topic", "kind", "expect_shape", "applies_to", "known_hits", "baseline")
POOL_KIND_SEARCH = "检索"
POOL_KINDS = ("检索", "引文扩展", "追加轮")
DISCOVERY_ORIGIN = "discovery"
SORT_WEIGHT_VERSION = "sort-weights-v2"
SORT_DIMS = ("rel", "rec", "imp", "comp", "cn")

# 人读渲染的逐行 finding 行：`<文件>:<行> [<规则>/<级别>] …`——F-06（有无逐行）与 F-08（逐行与 json 对账）共用一份
FINDING_ROW_RE = re.compile(r"[:：]\d+\s*\[[A-Z0-9]+-\d+/(?:error|warning|info)\]")
# 行提及（ADR-0028）：读者面词「文献 N」与旧写法「证据行 N」、契约记号「行 N」都认；
# 用 findall ＋集合判等，避免 `1` 误命中 `12`。
ROW_MENTION_RE = re.compile(r"(?:文献|证据行|行)\s*(\d+[a-z]?)")
FINDING_ROW_FULL_RE = re.compile(r"^.+?" + FINDING_ROW_RE.pattern)

SPECS: list[dict] = []


def spec(aid, group, kind, gate, title, target, expect, tol):
    """登记一条断言：id／组／断言类别／门／断言／断言对象／期望／容差。"""

    def deco(fn):
        SPECS.append(
            {
                "id": aid,
                "group": group,
                "kind": kind,
                "gate": gate,
                "title": title,
                "target": target,
                "expect": expect,
                "tol": tol,
                "fn": fn,
            }
        )
        return fn

    return deco


# ---------------------------------------------------------------- 通用工具（解析原语＝共享模块，见 scripts/package_parse.py）


def ok(observed, metric=None, drift=False):
    return {"ok": True, "observed": observed, "metric": metric, "drift": drift}


def bad(observed):
    return {"ok": False, "observed": observed}


def unverified(observed):
    """联网独立核对未执行：live 模式下算硬失败（fail-closed），--no-live 下由操作方显式放弃。"""
    return {"ok": True, "observed": observed, "unverified": True}


# 文本级解析原语与共享模块同源：本文件只绑定名字，不自带第二套实现（防解析器漂移）
norm_doi = package_parse.norm_doi
split_h2 = package_parse.split_h2
find_h2 = package_parse.find_h2
md_tables_with_header = package_parse.md_tables_with_header
md_table = package_parse.md_table
expand_row_ref = package_parse.expand_row_ref


def plan_attachment_cells(block):
    """落点计划行的（条目键, 附件列）对：附件列按表头名定位，列序变化不影响断言。

    返回 (col, cells)：col 为附件列序号（表头无名者记 None）；cells 为（条目键, 附件列取值）。
    """
    picked = None
    for header, rows in md_tables_with_header(block):
        if picked is None:
            picked = (header, rows)
        if any(c.startswith("附件") for c in header):
            picked = (header, rows)
            break
    header, rows = picked or ([], [])
    col = next((i for i, c in enumerate(header) if c.startswith("附件")), None)
    cells = []
    for r in rows:
        m = re.match(r"^([A-Z0-9]{8})\b", r[0] if r else "")
        if m:
            cells.append((m.group(1), r[col] if col is not None and len(r) > col else ""))
    return col, cells


def plan_topic_cells(block):
    """落点计划行的（条目键, 本次合集列）对：本次合集列按表头名定位，列序变化不影响断言。

    返回 (col, cells)：col 为本次合集列序号（表头无名者记 None）；cells 为（条目键, 本次合集列取值）。
    旧口径包无本次合集列时 col 为 None 且 cells 为空，调用方按当时口径读记通过。
    """
    picked = None
    for header, rows in md_tables_with_header(block):
        if picked is None:
            picked = (header, rows)
        if any(c.startswith("本次合集") for c in header):
            picked = (header, rows)
            break
    header, rows = picked or ([], [])
    col = next((i for i, c in enumerate(header) if c.startswith("本次合集")), None)
    if col is None:
        return None, []
    cells = []
    for r in rows:
        m = re.match(r"^([A-Z0-9]{8})\b", r[0] if r else "")
        if m:
            cells.append((m.group(1), r[col] if len(r) > col else ""))
    return col, cells


def plan_abstract_tables(block):
    """落点计划里带「摘要」列的表的（摘要列序号, 逐条取值）：摘要列按表头名定位，列序不影响断言。

    返回 [(col, cells), …]：col 为该表内摘要列序号，cells 为（条目键, 摘要列取值）；不带该列的表不参与，
    全无则空列表（调用方按当时口径读记通过，ADR-0008）。主表与「既有条目」另表一并返回——只取首张带
    该列的表会把后表的声明放行（票 07 复核修复）。
    """
    tables = []
    for header, rows in md_tables_with_header(block):
        col = next((i for i, c in enumerate(header) if c.startswith("摘要")), None)
        if col is None:
            continue
        cells = []
        for r in rows:
            m = re.match(r"^([A-Z0-9]{8})\b", r[0] if r else "")
            if m:
                cells.append((m.group(1), r[col] if len(r) > col else ""))
        tables.append((col, cells))
    return tables


# ---------------------------------------------------------------- 可及性清单（ADR-0023）

ACCESSIBILITY_MANIFEST = "run/step5-accessibility.json"
ACCESSIBILITY_VALUES = ("有全文", "仅摘要", "仅题录")
ABSTRACT_ABSENT = ("none", "")


def accessibility_entries(ctx):
    """可及性清单条目：{归一 DOI: 条目}；无清单返回 None（历史包口径，见 ADR-0023）。

    清单是 Step5 的研读集逐条记录（DOI｜可及性三值｜摘要来源｜取数时刻），属 `run/` 忽略类。
    摘要在三级取数（库内 `abstractNote` → 入库摘要表 → 联网）皆无所获才判仅题录，故清单里
    标仅题录而 `abstract_source` 非 `none` 的条目是自相矛盾，E-10 直接判不过。
    """
    if not ctx.has(ACCESSIBILITY_MANIFEST):
        return None
    data = ctx.json(ACCESSIBILITY_MANIFEST)
    out = {}
    for e in data.get("entries") or []:
        if not isinstance(e, dict):
            continue
        doi = norm_doi(e.get("doi") or "")
        if doi:
            out.setdefault(doi, e)
    return out


def plan_key_dois(ctx):
    """落点计划主表：{条目键: 归一 DOI}（DOI 列按表头名定位，缺列回退行内正则）。"""
    out = {}
    for header, rows in md_tables_with_header(ctx.text("agent/placement-plan.md")):
        col = next((i for i, c in enumerate(header) if c.strip().upper().startswith("DOI")), None)
        for r in rows:
            m = re.match(r"^([A-Z0-9]{8})\b", r[0] if r else "")
            if not m:
                continue
            cell = r[col] if col is not None and len(r) > col else ""
            dm = DOI_RE.search(cell) or DOI_RE.search(" ".join(r))
            if dm:
                out.setdefault(m.group(1), norm_doi(dm.group(1)))
    return out


def plan_abstract_dois(ctx, key_doi):
    """落点报告摘要列标「已填」的 DOI：{归一 DOI: 出处说明}（摘要列按表头名定位）。"""
    out = {}
    for _col, cells in plan_abstract_tables(ctx.text("agent/placement-plan.md")):
        for key, cell in cells:
            if cell.startswith("已填：") and key in key_doi:
                out.setdefault(key_doi[key], f"落点报告摘要列「{cell.strip()[:14]}」")
    return out


def plan_topic_line(block):
    """落点计划表头的本次合集行：返回（集名, 建／复用／AFK 自动建, 一致 M, 总 N）。

    只从含「本次合集：」的行内取值，不扫全文（全文扫会把正文里的「建」「一致」误判为模式与计数）。
    """
    line = next((l for l in block.splitlines() if "本次合集：" in l or "本次合集:" in l), "")
    m = re.search(r"`?(med-lit-review/[^\s`｜|，,）)]+)`?", line)
    name = m.group(1).strip() if m else ""
    mode = next((k for k in ("AFK 自动建", "复用", "建") if k in line), "")
    n = re.search(r"(?:双挂／追挂一致|一致)\s*(\d+)\s*[／/｜|]\s*(\d+)", line)
    return name, mode, (int(n.group(1)), int(n.group(2))) if n else (None, None)


class Ctx:
    def __init__(self, run, skill, repo, cur_year, live, baseline, out, fixture=None, coverage_fixtures=None):
        self.run = Path(run).resolve()
        self.zotero_fixture = fixture  # 自检专用：库读夹具路径（--selftest 孪生对照离线跑用）；正常跑为 None
        # 抽查轨 1（覆盖 fixture 场景）只在**显式**给 `--coverage-fixtures` 时启用：固定选题验收跑不传该参数，
        # 故机制 fixture 不会改写该跑的判定面（ADR-0023「fixture 不进固定选题集」；Spec 复核 P2）。
        # `--selftest` 全程指向合成场景目录，不读真实件。
        self.coverage_fixtures_enabled = coverage_fixtures is not None
        self.coverage_fixtures = Path(coverage_fixtures or DEFAULT_COVERAGE_FIXTURES)
        self.skill = Path(skill).resolve()
        self.repo = Path(repo).resolve()
        self.cur_year = int(cur_year)
        self.live = live
        self.baseline = json.loads(Path(baseline).read_text(encoding="utf-8")) if baseline else None
        self.out = Path(out).resolve()
        self._text: dict[str, str] = {}
        self._json: dict[str, object] = {}
        self._results: dict[str, dict] = {}
        self.notes: list[str] = []  # 未执行项与说明
        self._caliber: str | None = None

    def has(self, rel):
        return (self.run / rel).exists()

    def text(self, rel):
        key = str(rel)
        if key not in self._text:
            self._text[key] = (self.run / rel).read_text(encoding="utf-8")
        return self._text[key]

    def json(self, rel):
        key = str(rel)
        if key not in self._json:
            self._json[key] = json.loads(self.text(rel))
        return self._json[key]

    def atext(self, rel):
        return (self.skill / rel).read_text(encoding="utf-8")

    @property
    def anchors(self):
        return json.loads(ANCHORS_PATH.read_text(encoding="utf-8"))

    def note(self, msg):
        if msg not in self.notes:
            self.notes.append(msg)

    @property
    def caliber(self):
        """口径版本：先读下载报告文件头的口径注记，再看 manifest，最后按现行口径。

        注记是历史包的防误读键（门禁先读它）：`pre-0006-oa_first` 的「灰色不采用」按当时口径解读。
        """
        if self._caliber is None:
            m = CALIBER_RE.search(self.text("agent/bucketing-and-sources.md")) if self.has("agent/bucketing-and-sources.md") else None
            if m and m.group(1) in CALIBER_MEANING:
                self._caliber = m.group(1)
            elif m:
                self.note(f"下载报告口径注记取值不在口径表内（{m.group(1)}）：按现行口径判")
                self._caliber = CURRENT_CALIBER
            elif self.has("agent/manifest.json"):
                manifest_caliber = (self.json("agent/manifest.json").get("caliber") or {}).get("grey_gate_version")
                if manifest_caliber in CALIBER_MEANING:
                    self._caliber = manifest_caliber
                else:
                    self.note(f"manifest 的 caliber.grey_gate_version 取值不在口径表内（{manifest_caliber}）：按现行口径判")
                    self._caliber = CURRENT_CALIBER
            else:
                self._caliber = CURRENT_CALIBER
        return self._caliber

    @property
    def historical_caliber(self):
        """历史口径包：灰色「不采用」是当时的默认链路，不判现行失败。"""
        return self.caliber == "pre-0006-oa_first"

    @property
    def topic_caliber(self):
        """本次合集口径：先读落点计划的口径注记，再看 manifest，最后按有无本次合集列推断。

        注记是历史包的防误读键：`absent` 的无本次合集列按当时口径解读，不判现行失败。
        """
        if getattr(self, "_topic_caliber", None) is None:
            m = TOPIC_CALIBER_RE.search(self.text("agent/placement-plan.md")) if self.has("agent/placement-plan.md") else None
            if m and m.group(1) in TOPIC_CALIBER_MEANING:
                self._topic_caliber = m.group(1)
            elif m:
                self.note(f"落点计划口径注记取值不在口径表内（{m.group(1)}）：按现行口径判")
                self._topic_caliber = CURRENT_TOPIC_CALIBER
            elif self.has("agent/manifest.json"):
                manifest_caliber = (self.json("agent/manifest.json").get("caliber") or {}).get("topic_collection")
                if manifest_caliber in TOPIC_CALIBER_MEANING:
                    self._topic_caliber = manifest_caliber
                elif manifest_caliber is None:
                    _col, _cells = plan_topic_cells(self.text("agent/placement-plan.md")) if self.has("agent/placement-plan.md") else (None, [])
                    self._topic_caliber = CURRENT_TOPIC_CALIBER if _col is not None else "absent"
                else:
                    self.note(f"manifest 的 caliber.topic_collection 取值不在口径表内（{manifest_caliber}）：按现行口径判")
                    self._topic_caliber = CURRENT_TOPIC_CALIBER
            else:
                self._topic_caliber = CURRENT_TOPIC_CALIBER
        return self._topic_caliber

    @property
    def historical_topic(self):
        """历史口径包：无本次合集列是当时的默认形态，不判现行失败。"""
        return self.topic_caliber == "absent"

    def _resolve_abstract_caliber(self):
        """摘要列口径（三级读法）：落点计划注记 → manifest caliber.abstract_column → 有无该列推断。

        返回（口径值, 判定层）：`absent` 的包无摘要列，按当时口径读记通过（ADR-0008）；`present` 则要求
        摘要列在场——写库块漏列不得按历史包豁免；判定层进留痕文案，故豁免不是静默的。
        """
        if getattr(self, "_abstract_caliber", None) is None:
            m = ABSTRACT_CALIBER_RE.search(self.text("agent/placement-plan.md")) if self.has("agent/placement-plan.md") else None
            mc = (
                (self.json("agent/manifest.json").get("caliber") or {}).get("abstract_column")
                if self.has("agent/manifest.json")
                else None
            )
            if m and m.group(1) in ABSTRACT_CALIBER_MEANING:
                self._abstract_caliber = (m.group(1), "落点计划注记")
            elif mc in ABSTRACT_CALIBER_MEANING:
                self._abstract_caliber = (mc, "manifest caliber.abstract_column")
            elif m:
                self.note(f"落点计划摘要口径注记取值不在口径表内（{m.group(1)}）：按现行口径判")
                self._abstract_caliber = (CURRENT_ABSTRACT_CALIBER, "落点计划注记（越表）")
            elif mc is not None:
                self.note(f"manifest 的 caliber.abstract_column 取值不在口径表内（{mc}）：按现行口径判")
                self._abstract_caliber = (CURRENT_ABSTRACT_CALIBER, "manifest caliber.abstract_column（越表）")
            else:
                tables = plan_abstract_tables(self.text("agent/placement-plan.md")) if self.has("agent/placement-plan.md") else []
                self._abstract_caliber = (
                    CURRENT_ABSTRACT_CALIBER if tables else "absent",
                    "有无该列推断",
                )
        return self._abstract_caliber

    @property
    def abstract_caliber(self):
        """摘要列口径：`absent`＝无摘要列的历史包口径，`present`＝带摘要列与计数行的现行口径。"""
        return self._resolve_abstract_caliber()[0]

    @property
    def abstract_caliber_source(self):
        """摘要口径判定来自哪一层（注记／manifest／有无该列推断）：只进留痕文案。"""
        return self._resolve_abstract_caliber()[1]

    @property
    def historical_abstract(self):
        """历史口径包：摘要列入档前无该列是当时的默认形态，不判现行失败。"""
        return self.abstract_caliber == "absent"


# ---------------------------------------------------------------- 解析：包／卡／日志


def package_sections(ctx):
    return split_h2(ctx.text("agent/knowledge-package.md"))


def evidence_rows(ctx):
    """§5 七列表行：{行号: {文献, 设计, 人群, 干预, 结局, 偏倚, 关联, doi, 文号}}（共享模块解析）。"""
    return package_parse.evidence_rows(ctx.text("agent/knowledge-package.md"))


def cards(ctx):
    """提取卡：`agent/cards/*.md` 一文件一卡；0 卡／目录缺抛 `CardParseError`（文案含读取路径与解析条数）。"""
    return package_parse.parse_cards(ctx.run)


def row_card_map(ctx):
    """返回 (证据表行, 行→卡号, 全部卡)；文件名主索引＋卡内 DOI／文号交叉核对，别名卡不参与行归属。"""
    rows = evidence_rows(ctx)
    cards_ = cards(ctx)
    return rows, package_parse.row_card_map(rows, cards_), cards_


def prose_row_refs(ctx):
    """包内正文（去掉表格行）里出现的所有行号引用与区间，返回 [(行号, 上下文)]。"""
    return package_parse.prose_row_refs(ctx.text("agent/knowledge-package.md"))


# ---------------------------------------------------------------- 联网核对


def http_get(url, timeout=30, attempts=3):
    last = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read(), None
        except Exception as exc:  # 传输层失败：重试后如实上报，不判假失败
            last = describe_transport_error(exc)
            time.sleep(2 * (i + 1))
    return None, last


def describe_transport_error(exc):
    """把传输异常压成一句中文（报告要可读，不贴原始堆栈）。"""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, str):
            return f"连接失败（{reason[:60]}）"
        return f"连接失败（{type(reason).__name__ if reason else '未知原因'}）"
    return f"{type(exc).__name__}"


def lock_crossref(anchor):
    body, err = http_get(anchor["url"] if "url" in anchor else f"https://api.crossref.org/works/{anchor['identifier']}")
    if body is None:
        return {"status": "unverified", "detail": f"未取到注册记录（{err}）"}
    try:
        msg = json.loads(body.decode("utf-8"))["message"]
    except Exception:
        return {"status": "unverified", "detail": "注册记录解析失败"}
    title = (msg.get("title") or [""])[0]
    year = None
    for key in ("published-print", "published-online", "published", "issued"):
        parts = (msg.get(key) or {}).get("date-parts") or []
        if parts and parts[0]:
            year = parts[0][0]
            break
    expect = anchor["lock"]
    years = expect.get("expect_years") or [expect["expect_year"]]
    title_ok = expect["expect_title_contains"].lower() in title.lower()
    year_ok = year in years
    if title_ok and year_ok:
        return {"status": "valid", "detail": f"Crossref 记录命中：{title[:60]}｜{year}｜{msg.get('type')}"}
    if not title_ok:
        return {"status": "missing", "detail": f"注册记录题名不符：{title[:80]}"}
    return {"status": "drifted", "detail": f"年份漂移：记录 {year}（pins {expect.get('expect_years') or expect['expect_year']}）"}


def lock_openfda(anchor):
    lock = anchor["lock"]
    query = urllib.parse.quote(f'openfda.brand_name:"{lock["brand"]}"')
    body, err = http_get(f"https://api.fda.gov/drug/label.json?search={query}&limit=1")
    if body is None:
        return {"status": "unverified", "detail": f"未取到说明书记录（{err}）"}
    try:
        rec = json.loads(body.decode("utf-8"))["results"][0]
    except Exception:
        return {"status": "unverified", "detail": "说明书记录解析失败"}
    version, set_id = str(rec.get("version", "")), rec.get("set_id", "")
    if version == lock["expect_version"] and set_id == lock["expect_set_id"]:
        return {"status": "valid", "detail": f"SPL 版本 {version}｜set_id {set_id[:8]}…｜effective_time {rec.get('effective_time')}"}
    return {"status": "drifted", "detail": f"说明书版本漂移：live 版本 {version}（pins {lock['expect_version']}）"}


def lock_document_digest(ctx, anchor):
    lock = anchor["lock"]
    body, err = http_get(lock["url"], timeout=120)
    if body is None:
        return {"status": "unverified", "detail": f"未取到官方文档（{err}）"}
    live = hashlib.sha256(body).hexdigest()
    archive = ctx.run / lock["archive_path"]
    if not archive.exists():
        return {"status": "missing", "detail": f"缺原文留档：{lock['archive_path']}"}
    disk = sha256_file(archive)
    if live == disk:
        return {"status": "valid", "detail": f"官方文档字节指纹一致（sha256 {live[:12]}…）"}
    return {"status": "drifted", "detail": f"官方文档已改版（live {live[:12]}… ≠ 留档 {disk[:12]}…）；需核对修订号并复跑锁点"}


def lock_record(ctx, anchor):
    missing = [rel for rel in anchor["lock"]["evidence"] if not ctx.has(rel)]
    if missing:
        return {"status": "missing", "detail": f"缺仅题录证据：{'、'.join(missing)}"}
    return {"status": "valid", "detail": anchor["lock"]["expect_status"] + "（题录证据在场：" + "、".join(anchor["lock"]["evidence"]) + "）"}


def lock_anchor(ctx, anchor):
    if not ctx.live:
        return {"status": "unverified", "detail": "本轮以 --no-live 运行，未联网核对"}
    method = anchor["lock"]["method"]
    if method == "crossref":
        return lock_crossref(anchor)
    if method == "openfda":
        return lock_openfda(anchor)
    if method == "document_digest":
        return lock_document_digest(ctx, anchor)
    if method == "record":
        return lock_record(ctx, anchor)
    return {"status": "unverified", "detail": f"未知锁定方式：{method}"}


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def lock_pins_key(ctx, anchor):
    """锁定输入指纹：任一 pin 或运行目录内受检件变化即换键，键不等＝重核。

    受检件专名入键——文档指纹锚点带上本轮留档字节哈希、题录锚点带上各证据件在场情况；
    否则换一轮或换一包复用，会把「本轮受检件缺席」误判成 valid。
    """
    lock = dict(anchor.get("lock") or {})
    parts = {
        "id": anchor["id"],
        "identifier": anchor["identifier"],
        "kind": anchor["kind"],
        "method": lock.get("method"),
        "lock": lock,
    }
    if lock.get("method") == "document_digest":
        archive = ctx.run / str(lock.get("archive_path", ""))
        parts["archive_sha256"] = sha256_file(archive) if archive.exists() else None
    elif lock.get("method") == "record":
        parts["evidence_present"] = {rel: ctx.has(rel) for rel in lock.get("evidence", [])}
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def read_prior_lock(ctx):
    """同一结果目录里上一轮的锚点锁（复用载体）；缺失、不可解析或空记录＝不复用（按重核走）。"""
    path = ctx.out / "anchor-lock.json"
    if not path.exists():
        return None
    try:
        prior = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ctx.note("锚点锁复用未启用：结果目录内 anchor-lock.json 不可解析，本轮逐条重核")
        return None
    return prior if isinstance(prior, dict) and prior.get("anchors") else None


def parse_lock_time(text):
    try:
        return time.mktime(time.strptime(str(text), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return None


def reuse_verdict(prior_entry, key, now):
    """窗口内复用原 valid 结论；键不等、原结论非 valid、锁龄超窗或时刻不可解析 → None（重核）。

    checked_at 沿用原锁定时刻（复用不顺延锁龄：连续复用仍按最初那次 live 核对起算）；
    留痕只记「当前锁龄＋原锁定时刻＋原始读数」——已在复用中的条目再复用不会叠出第二层前缀。
    """
    if prior_entry.get("pins_key") != key or prior_entry.get("status") != "valid":
        return None
    checked_at = str(prior_entry.get("checked_at") or "")
    ts = parse_lock_time(checked_at)
    if ts is None:
        return None
    age_hours = (now - ts) / 3600
    if age_hours > LOCK_WINDOW_HOURS:
        return None
    reading = REUSE_DETAIL_RE.sub("", str(prior_entry.get("detail") or ""))
    return {
        "status": "valid",
        "detail": f"复用（锁龄 {age_hours:.1f}h）＋原锁定 {checked_at}：{reading}",
        "checked_at": checked_at,
        "reused": True,
        "lock_age_hours": round(age_hours, 1),
    }


def norm_label(text):
    """中文文档里的全角斜杠／空格与库内名称对齐后再比。"""
    return re.sub(r"[\s／/]", "", (text or "")).lower()


def zotero_recheck(ctx):
    """只读回查落点计划里的条目（本地 Zotero API）：收藏集、标题、DOI、摘要与 PDF 附件；不可达记未执行，不判失败。

    结果缓存在 ctx 上：D-07（收藏集一致）、D-08（附件一致）与 D-11（摘要声明一致）共用同一次回查；
    收藏集名按 id 在 run 内去重（逐条往返由 条目数×收藏集数 降为 条目数＋收藏集数，取不到照旧逐条重试）。
    ctx.zotero_fixture 非空时改读夹具（形如 {"abstract_notes": {条目键: 摘要}}）——只供自检的孪生对照离线跑，
    正常跑（含 live）不设该值，一律走真实本地库。
    """
    cached = getattr(ctx, "_zotero_cache", None)
    if cached is not None:
        return cached
    if getattr(ctx, "zotero_fixture", None):
        notes = json.loads(Path(ctx.zotero_fixture).read_text(encoding="utf-8"))["abstract_notes"]
        ctx.note(
            "本地库只读回查由自检夹具代替（--zotero-fixture，非真实库读）："
            "D-07／D-08／D-11 共用同一次回查，三个断言的库内读数同源"
        )
        out = (
            [
                {"key": k, "title": "", "doi": "", "collections": [], "pdf_attachments": [], "abstract_note": v}
                for k, v in notes.items()
            ],
            "自检夹具代替本地库只读回查（--selftest 专用，非真实库读）",
        )
        ctx._zotero_cache = out
        return out
    plan = ctx.text("agent/placement-plan.md")
    keys = []
    for row in md_table(plan):
        m = re.match(r"^([A-Z0-9]{8})\b", row[0])
        if m and m.group(1) not in keys:
            keys.append(m.group(1))
    if not keys:
        out = (None, "落点计划未记录条目键，跳过在库回查")
        ctx._zotero_cache = out
        return out
    results = []
    col_names = {}
    for key in keys:
        body, err = http_get(f"http://127.0.0.1:23119/api/users/0/items/{key}", timeout=8, attempts=1)
        if body is None:
            out = (None, f"本地 Zotero API 不可达（{err}）")
            ctx._zotero_cache = out
            return out
        try:
            data = json.loads(body.decode("utf-8"))["data"]
        except Exception:
            out = (None, "本地 Zotero API 返回无法解析")
            ctx._zotero_cache = out
            return out
        cols = []
        for cid in data.get("collections", []):
            name = col_names.get(cid)
            if name is None:  # 取到的名进 run 内缓存；取不到的照旧逐条重试（失败语义不变）
                cbody, _ = http_get(f"http://127.0.0.1:23119/api/users/0/collections/{cid}", timeout=8, attempts=1)
                name = json.loads(cbody.decode("utf-8"))["data"]["name"] if cbody else None
                if name is not None:
                    col_names[cid] = name
            if name is not None:
                cols.append(name)
        pdfs = []
        chbody, _ = http_get(f"http://127.0.0.1:23119/api/users/0/items/{key}/children?limit=50", timeout=8, attempts=1)
        if chbody:
            try:
                for child in json.loads(chbody.decode("utf-8")):
                    cd = child.get("data") or {}
                    if cd.get("itemType") != "attachment":
                        continue
                    if cd.get("contentType") == "application/pdf" or (cd.get("filename") or "").lower().endswith(".pdf"):
                        pdfs.append(cd.get("filename") or cd.get("title") or "attachment")
            except Exception:
                pass
        results.append(
            {
                "key": key,
                "title": data.get("title", "")[:60],
                "doi": data.get("DOI", ""),
                "abstract_note": data.get("abstractNote", ""),  # 摘要读数与收藏集／附件同一次响应，不增往返（D-11）
                "collections": cols,
                "pdf_attachments": pdfs,
            }
        )
    out = (results, "本地 Zotero API 只读回查完成（含附件）")
    ctx._zotero_cache = out
    return out


# ---------------------------------------------------------------- 读数：池级／终池分量（ADR-0023 分量口径）


def pool_kind_counts(pools):
    """池级分列读数：{pool_kind: 池数}；未写 `pool_kind` 的池按缺省口径计检索池（票 06 冻结）。"""
    counts: dict[str, int] = {}
    for pool in pools:
        kind = pool.get("pool_kind") or POOL_KIND_SEARCH
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def pool_kind_split(counts):
    """池分量读数串：三值固定序，契约外的 kind 单列（不静默丢弃）。"""
    parts = [f"{k} {counts.get(k, 0)}" for k in POOL_KINDS]
    parts += [f"{k}（契约外）{n}" for k, n in sorted(counts.items()) if k not in POOL_KINDS]
    return "｜".join(parts)


def ledger_mechanisms(ctx, aid):
    """读覆盖账 `mechanisms` 段；缺件／形态不符返回 (None, 定位信息)（账类两条断言共用）。"""
    if not ctx.has(LEDGER_PATH):
        return None, f"{aid} 覆盖账缺件：{LEDGER_PATH}（ADR-0023 硬门禁；账产出命令见 SKILL.md Step2 行）"
    ledger = ctx.json(LEDGER_PATH)
    mech = ledger.get("mechanisms") if isinstance(ledger, dict) else None
    if not isinstance(mech, dict):
        return None, f"{aid} 覆盖账缺 mechanisms 段（{LEDGER_PATH}）"
    return mech, None


def load_skill_module(skill, name):
    """按 `--skill` 定位 skill 内脚本并导入（同进程复用：读数格式不复制第二套实现）。"""
    scripts = str(Path(skill) / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module(name)


def load_ledger_module(ctx):
    """按 `--skill` 定位覆盖账脚本并导入：§8 缺口分句与账同源，不复制第二套三段式实现。"""
    return load_skill_module(ctx.skill, "coverage_ledger")


def coverage_scenarios(ctx):
    """覆盖 fixture 场景定义（ADR-0023 抽查轨 1）：读 `--coverage-fixtures` 目录的 `*.json`（每场景一文件）。

    返回 (scenarios, problems)：problems 为定义不合契约的定位信息（字段契约见 `topic.md`，供票 08 对齐）。
    目录不在场＝无场景集（该交付不适用，不阻断）。
    """
    root = Path(ctx.coverage_fixtures)
    if not root.is_dir():
        return [], []
    scenarios, problems = [], []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{path.name} 不可解析（{type(exc).__name__}）")
            continue
        if not isinstance(data, dict):
            problems.append(f"{path.name} 不是对象")
            continue
        if data.get("id") != path.stem:
            problems.append(f"{path.name} 的 id 与文件名不符（{data.get('id')}）")
            continue
        miss = [f for f in FIXTURE_SCENARIO_FIELDS if f not in data]
        if miss:
            problems.append(f"{path.name} 缺字段 {'、'.join(miss)}")
            continue
        if data["kind"] not in FIXTURE_KINDS:
            problems.append(f"{path.name} 的 kind 不在契约三值（{'／'.join(FIXTURE_KINDS)}）：{data['kind']}")
            continue
        applies = data["applies_to"]
        if not isinstance(applies, list) or not applies or any(not str(t).strip() for t in applies):
            problems.append(f"{path.name} 的 applies_to 形态不符（需非空 token 表；空表会让场景永不适用）")
            continue
        hits = data.get("known_hits")
        if not isinstance(hits, list) or not hits or any(not isinstance(h, dict) or not h.get("id") for h in hits):
            problems.append(f"{path.name} 的 known_hits 形态不符（需非空且逐条带 id）")
            continue
        scenarios.append(data)
    return scenarios, problems


def scenario_applies(ctx, scenario):
    """场景是否适用本交付：`applies_to` 任一 token 命中**交付目录名**即适用（ADR-0023：fixture 不进固定选题集）。

    只认交付目录名，不扫包内文本：固定选题包的研究问题节含多题材词（如 PCSK9 包里出现 LDL-C／他汀／ASCVD），
    按文本匹配会把机制 fixture 拖进固定选题跑、改写该跑的判定面（Spec 复核 P2）。
    """
    name = ctx.run.name.lower()
    return any(str(t).strip() and str(t).lower() in name for t in scenario["applies_to"])


def coverage_fixture_check(ctx, aid, kinds):
    """抽查轨 1 的核出／取回判定：适用场景的已知必中清单须逐条核出，缺口逐条可定位。

    读数件＝`run/b1/coverage-check.json`（核出器产出，形态见 `topic.md`）：适用场景未记录读数即缺口。
    """
    if not ctx.coverage_fixtures_enabled:  # 固定选题验收跑此路：抽查轨 1 不适用，不因机制 fixture 增删判定
        return ok(f"覆盖 fixture：本轮未启用抽查轨 1（{aid} 不适用；`--coverage-fixtures` 显式启用）")
    scenarios, problems = coverage_scenarios(ctx)
    if problems:
        return bad(f"{aid} 覆盖 fixture 场景定义不合契约（字段契约见 topic.md）：{'；'.join(problems)}")
    mine = [s for s in scenarios if s["kind"] in kinds]
    if not mine:
        return ok(f"覆盖 fixture：无 {aid} 类场景（不适用）")
    applicable = [s for s in mine if scenario_applies(ctx, s)]
    if not applicable:
        return ok(f"覆盖 fixture：{len(mine)} 个场景均不适用本交付（{'、'.join(s['id'] for s in mine)}）")
    if not ctx.has(COVERAGE_CHECK_PATH):
        return bad(f"覆盖 fixture：适用场景 {'、'.join(s['id'] for s in applicable)} 未记录核出读数"
                   f"（缺 {COVERAGE_CHECK_PATH}；ADR-0023 抽查轨 1 要求适用场景在验收时跑）")
    data = ctx.json(COVERAGE_CHECK_PATH)
    recorded = {r.get("id"): r for r in (data.get("scenarios") or []) if isinstance(r, dict)}
    gaps, parts = [], []
    for s in applicable:
        row = recorded.get(s["id"])
        if row is None:
            gaps.append(f"{s['id']} 未记录核出读数")
            continue
        found = {f.get("id") for f in (row.get("found") or []) if isinstance(f, dict)}
        want = [h["id"] for h in s["known_hits"]]
        lack = [h for h in want if h not in found]
        if lack:
            gaps.append(f"{s['id']} 核出失败：缺 {'、'.join(lack)}")
        if row.get("expect_shape") != s["expect_shape"]:
            gaps.append(f"{s['id']} 期望形态不符：核出块记 {row.get('expect_shape')} vs 场景 {s['expect_shape']}")
        parts.append(f"{s['id']} 核出 {len(want) - len(lack)}/{len(want)}（形态 {s['expect_shape']}）")
    if gaps:
        return bad("；".join(gaps))
    return ok("；".join(parts) + f"（场景 {len(mine)} 个，适用 {len(applicable)} 个）")


def origin_counts(dedupe):
    """终池构成分列读数：{origin: 条数}；缺 `final_records` 的旧形态件返回 None（ADR-0008 历史口径）。"""
    records = dedupe.get("final_records")
    if records is None:
        return None
    counts: dict[str, int] = {}
    for rec in records:
        origin = rec.get("origin") or DISCOVERY_ORIGIN
        counts[origin] = counts.get(origin, 0) + 1
    return counts


# ---------------------------------------------------------------- 断言：发现（Step1）


@spec("A-01", "发现", "精确", "分步", "双窗各跑一遍（全窗＋近两年窗）", "agent/search-strategy.md＋run/b1/step1-pools.json", "两窗记录齐且 Step1 检索池数 ≥ 9（扩展／追加池分列读数、不断言）", "—")
def a01(ctx):
    ss = ctx.text("agent/search-strategy.md")
    pools = ctx.json("run/b1/step1-pools.json")["pools"]
    if "全窗" not in ss or "近两年窗" not in ss:
        return bad("检索策略缺全窗或近两年窗记录")
    counts = pool_kind_counts(pools)
    search = counts.get(POOL_KIND_SEARCH, 0)
    if search < 9:
        return bad(f"Step1 检索池仅 {search} 个（期望 ≥ 9；池分量 {pool_kind_split(counts)}）")
    return ok(f"双窗记录齐；Step1 检索池 {search} 个（池分量 {pool_kind_split(counts)}；扩展／追加池分列不断言）")


@spec("A-02", "发现", "精确", "分步", "检索式逐条含来源／窗口／日期／命中", "agent/search-strategy.md", "六族检索式逐行含来源／窗口／命中，执行日期按轮记录", "—")
def a02(ctx):
    ss = ctx.text("agent/search-strategy.md")
    rows = [r for r in md_table(ss) if len(r) >= 4]
    query_rows = [r for r in rows if "PCSK9" in " ".join(r)]
    if len(query_rows) < 8:
        return bad(f"检索式行仅 {len(query_rows)} 行（期望 ≥ 8，覆盖六族与双窗）")
    undated = [r for r in query_rows if not re.search(r"全窗|近两年|窗口|20\d\d-", " ".join(r[2:]))]
    if undated:
        return bad(f"{len(undated)} 行检索式缺窗口或日期：{undated[0][:2]}")
    if ss.count("执行日期") < 3:
        return bad("扩展轮次未记录执行日期")
    return ok(f"检索式 {len(query_rows)} 行覆盖六族与双窗，逐行带窗口／命中，三轮执行日期在案")


@spec("A-03", "发现", "精确", "分步", "queryPlan 原样摘录与来源诊断", "agent/search-strategy.md", "含 providers／dateRange／命中计数／queryStatuses／diagnostics", "—")
def a03(ctx):
    ss = ctx.text("agent/search-strategy.md")
    keys = ["providers", "queryStatuses", "diagnostics"]
    miss = [k for k in keys if k not in ss]
    if miss:
        return bad(f"queryPlan 摘录缺字段：{'、'.join(miss)}")
    return ok("queryPlan 摘录含 providers／命中计数／queryStatuses／diagnostics")


@spec("A-04", "发现", "精确", "分步", "零命中回退链条已记录", "agent/search-strategy.md", "有零命中回退记录行（触发或未触发都记）", "—")
def a04(ctx):
    ss = ctx.text("agent/search-strategy.md")
    if "零命中回退" not in ss:
        return bad("检索策略无零命中回退记录")
    return ok("零命中回退链条已记录（本轮未触发，如实记 log）")


@spec("A-05", "发现", "精确", "硬门禁", "无稳定身份记录未进持久化", "run/b1/dedupe-report.json", "identity_rejected 非空且 final_pool_has_identityless 为假", "—")
def a05(ctx):
    d = ctx.json("run/b1/dedupe-report.json")
    if not d["identity_rejected"]:
        return bad("无稳定身份探针未产生 identity_rejected 记录")
    if d["final_pool_has_identityless"]:
        return bad("最终池含无稳定身份条目")
    return ok(f"探针 {len(d['identity_rejected'])} 条判 identity_rejected；池内无无身份条目")


@spec("A-06", "发现", "精确", "分步", "未知日期处理已记录", "agent/search-strategy.md", "有未知日期与 dateRejectedCandidateCount 记录", "—")
def a06(ctx):
    ss = ctx.text("agent/search-strategy.md")
    if "未知日期" not in ss or "dateRejectedCandidateCount" not in ss:
        return bad("缺未知日期处理记录")
    return ok("未知日期记录在案（未知保持未知，未以推断日期补全）")


@spec("A-07", "发现", "容差", "软项", "检索命中数为正（容差下界）", "agent/search-strategy.md", "每窗总命中 ≥ 1；不断言精确值", "下界 1；provider 漂移只记日志")
def a07(ctx):
    ss = ctx.text("agent/search-strategy.md")
    hits = [int(n) for n in re.findall(r"总命中\s*(\d+)", ss)]
    if not hits:
        return bad("检索策略未记录总命中数")
    if min(hits) < 1:
        return bad(f"存在零命中窗口：{hits}")
    return ok(f"记录命中数 {len(hits)} 处，最小 {min(hits)}（下界 1）", metric=min(hits))


@spec("A-08", "发现", "精确", "硬门禁", "术语扩展与词表版本引用", "agent/screening-log.md／agent/search-strategy.md／references/lexicon/term-lexicon.candidates.yaml", "含词表版本号、扩展方向与候选／晋升记录；已落词以真源或队列条目为准；无边未声明即红（逐条口径）", "—")
def a08(ctx):
    log = ctx.text("agent/screening-log.md")
    ss = ctx.text("agent/search-strategy.md")
    lexicon_version = re.search(r"version:\s*(\S.+)", ctx.atext("references/lexicon/term-lexicon.yaml")).group(1).strip()
    if lexicon_version not in log or lexicon_version not in ss:
        return bad(f"筛选日志或检索策略未引用词表版本 {lexicon_version}")
    for key in ("扩展方向", "候选", "生长循环"):
        if key not in log:
            return bad(f"筛选日志缺术语段：{key}")
    # 文字行不算已检出：声明的未命中／候选必须真落真源或候选队列（references/protocols/term-lexicon-protocol.md §6）
    proc = subprocess.run(
        [sys.executable, str(ctx.skill / "scripts/check_growth_loop.py"), str(ctx.run), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=300)
    try:
        res = json.loads(proc.stdout)
    except Exception:
        return bad(f"生长循环检查执行失败：{proc.stderr.strip()[:180]}")
    if res.get("verdict") != "GREEN":
        gaps = "、".join(dict.fromkeys(res.get("gaps") or []))
        fams = "、".join(t.get("term", "?") for t in (res.get("edgeless_terms") or []))
        detail = "；".join(x for x in (
            f"未落真源／队列：{gaps}" if gaps else "",
            f"无边未声明（机械生长）：{fams}" if fams else "") if x) or res.get("reason", "?")
        return bad(f"生长循环未闭合（文字行不算已检出）：{detail}")
    return ok(f"词表版本 {lexicon_version} 在检索策略与筛选日志双引用；"
              f"方向／候选／生长循环记录齐；声明的未命中／候选全部落真源或候选队列；无边术语均已声明")


@spec("A-09", "发现", "一致性", "硬门禁", "词表版本与真源一致", "run 记录 vs references/lexicon/term-lexicon.yaml", "记录版本 == 真源 version", "—")
def a09(ctx):
    src = re.search(r"version:\s*(\S.+)", ctx.atext("references/lexicon/term-lexicon.yaml")).group(1).strip()
    log = ctx.text("agent/screening-log.md")
    found = re.findall(r"(lexicon v\d+\.\d+)", log)
    if not found:
        return bad("筛选日志未出现 lexicon vX.Y 版本号")
    if any(v.strip() != src for v in found):
        return bad(f"版本不一致：日志 {set(found)} vs 真源 {src}")
    return ok(f"版本一致：{src}")


@spec("A-10", "发现", "精确", "硬门禁", "覆盖账在场与九块字段齐", "run/b1/coverage-ledger.json（字段集＝ADR-0023 ＋ ADR-0024／配套票 03 增补 ＋ ADR-0032 第九块）", "账在场；九块齐；逐块冻结名＋pointer 在场（子集语义、note 可选）", "—")
def a10(ctx):
    mech, why = ledger_mechanisms(ctx, "A-10")
    if mech is None:
        return bad(why)
    missing_blocks = [b for b in LEDGER_BLOCK_FIELDS if b not in mech]
    if missing_blocks:
        return bad(f"覆盖账缺块：{'、'.join(missing_blocks)}（期望九块齐）")
    checked = 0
    for block, fields in LEDGER_BLOCK_FIELDS.items():
        got = mech[block]
        if not isinstance(got, dict):
            return bad(f"覆盖账块 {block} 不是对象：{type(got).__name__}")
        miss = [f for f in fields if f not in got]
        if miss:
            return bad(f"覆盖账块 {block} 缺冻结字段：{'、'.join(miss)}")
        if "pointer" not in got:
            return bad(f"覆盖账块 {block} 缺 pointer（ADR-0023：每机制一块＋指向各自产物的指针）")
        for name in (key for key in got if key == "pointer" or key.endswith("_pointer")):
            if not isinstance(got[name], list):
                return bad(f"覆盖账块 {block} 的 {name} 不是列表：{type(got[name]).__name__}")
        checked += len(fields) + 1
        for name, row_fields in LEDGER_ROW_FIELDS.items():
            if name not in fields:
                continue
            rows = got.get(name)
            if not isinstance(rows, list):
                return bad(f"覆盖账块 {block} 的 {name} 不是列表：{type(rows).__name__}")
            for row in rows:
                if not isinstance(row, dict):
                    return bad(f"覆盖账块 {block} 的 {name} 行不是对象")
                miss_row = [f for f in row_fields if f not in row]
                if miss_row:
                    return bad(f"覆盖账块 {block} 的 {name} 行缺冻结字段：{'、'.join(miss_row)}")
    extra = sorted(set(mech) - set(LEDGER_BLOCK_FIELDS))
    return ok(f"覆盖账在场：九块齐、冻结名与 pointer 全在场（{checked} 项核对，"
              f"字段集＝ADR-0023 ＋ ADR-0024／配套票 03 增补 ＋ ADR-0032 第九块「入库集守恒」）"
              + (f"；另见契约外块 {extra}" if extra else ""))


@spec("A-11", "发现", "一致性", "硬门禁", "账上缺口⇒§8 三段式分句且数字一致", "run/b1/coverage-ledger.json＋agent/knowledge-package.md 第八节", "十类机制各一行三段式（机制｜读数｜缺口与补法），读数数字与账一致", "—")
def a11(ctx):
    mech, why = ledger_mechanisms(ctx, "A-11")
    if mech is None:
        return bad(why)
    lim = find_h2(ctx.text("agent/knowledge-package.md"), "局限声明")
    if lim is None:
        return bad("知识包缺第八节局限声明，覆盖缺口块无处落")
    if "覆盖缺口" not in lim:
        return bad("第八节缺「覆盖缺口」块（ADR-0023：逐机制一行、三段式）")
    try:  # §8 缺口分句与账同源：跑账脚本自己的三段式产出，不复制第二套读数格式
        expected = load_ledger_module(ctx).gap_lines(ctx.json(LEDGER_PATH))
    except (KeyError, TypeError, AttributeError) as exc:
        return bad(f"账块缺字段或形态不符，§8 覆盖缺口块无从对账（{type(exc).__name__}: {exc}；先过 A-10）")
    lines = [line.strip() for line in lim.splitlines() if line.strip().startswith("-")]
    missing, mismatched = [], []
    for name, reading, _fix in expected:
        line = next((cand for cand in lines if cand.startswith(f"- {name}｜")), None)  # 逐机制一行：按行首锁定，不做子串命中
        if line is None:
            missing.append(name)
            continue
        if line.count("｜") < 2:
            missing.append(f"{name}（非三段式：缺「｜」分隔）")
            continue
        if reading not in line:  # 读数段逐字核对：行内「数字子串包含」会把「3 格」放行在「13 格」上（Standards 复核发现）
            mismatched.append(f"{name} 读数段与账不符（期望「{reading}」）")
    if missing:
        return bad(f"§8 覆盖缺口块缺分句：{'、'.join(missing)}")
    if mismatched:
        return bad("§8 覆盖缺口读数与账不一致：" + "；".join(mismatched))
    return ok(f"§8 覆盖缺口块十类机制分句齐、读数数字与账一致（{'、'.join(name for name, _r, _f in expected)}）")


@spec("A-12", "发现", "精确", "软项", "已知必中核出（fixture 场景）", "evals/coverage/*.json＋run/b1/coverage-check.json", "适用场景的 known_hits 逐条核出／取回；不适用或未定义场景记不适用", "缺口须进局限声明")
def a12(ctx):
    return coverage_fixture_check(ctx, "A-12", ("known-hit",))


@spec("A-13", "发现", "精确", "软项", "经典／高被引核出（fixture 场景）", "evals/coverage/*.json＋run/b1/coverage-check.json", "适用场景的 known_hits 逐条核出；不适用或未定义场景记不适用", "缺口须进局限声明")
def a13(ctx):
    return coverage_fixture_check(ctx, "A-13", ("classic", "high-cited"))


# ---------------------------------------------------------------- 断言：分桶（Step2）


@spec("B-01", "分桶", "精确", "分步", "三轮去重计数完整", "run/b1/dedupe-report.json", "R1／R2／R3 各记输入／保留／合并", "—")
def b01(ctx):
    rounds = ctx.json("run/b1/dedupe-report.json")["rounds"]
    if [r["round"][:2] for r in rounds] != ["R1", "R2", "R3"]:
        return bad(f"轮次不齐：{[r['round'] for r in rounds]}")
    for r in rounds:
        for key in ("input_count", "kept_count", "merged_count", "suspected_count"):
            if not isinstance(r.get(key), int):
                return bad(f"{r['round']} 缺计数 {key}")
    return ok("；".join(f"{r['round'][:2]} {r['input_count']}／{r['kept_count']}／{r['merged_count']}／{r['suspected_count']}" for r in rounds))


@spec("B-02", "分桶", "精确", "分步", "合并不丢来源（来源轨迹）", "run/b1/dedupe-report.json", "有合并的轮次里，被合并条目的来源并入保留条目", "—")
def b02(ctx):
    rounds = ctx.json("run/b1/dedupe-report.json")["rounds"]
    merged_rounds = [r for r in rounds if r["merged_count"] > 0]
    if not merged_rounds:
        return bad("三轮去重没有任何合并，无法验证来源轨迹")
    trails = [rec for r in merged_rounds for rec in r.get("removed", []) if rec.get("dropped_sources")]
    if not trails:
        return bad("合并记录缺 dropped_sources 来源轨迹")
    multi = [t for t in trails if len(set(t["kept_sources"]) | set(t["dropped_sources"])) > 1]
    if not multi:
        return bad("来源轨迹未体现跨来源合并")
    return ok(f"合并 {len(trails)} 条带来源轨迹，其中 {len(multi)} 条为跨来源合并")


@spec("B-03", "分桶", "精确", "分步", "疑似重复进人工确认、不自动合并", "run/b1/dedupe-report.json＋dedupe-decisions.json", "suspected 全部有裁定，decisions_unused 为空", "—")
def b03(ctx):
    d = ctx.json("run/b1/dedupe-report.json")
    if d["decisions_unused"]:
        return bad(f"有未使用的裁定：{d['decisions_unused']}")
    for r in d["rounds"]:
        if r["suspected_count"] != len(r.get("suspected", [])):
            return bad(f"{r['round']} 疑似计数与疑似列表不一致")
    suspected = [s for r in d["rounds"] for s in r.get("suspected", [])]
    if not suspected:
        return bad("三轮去重没有疑似记录，无法验证人工确认路径")
    undecided = [s for s in suspected if not (s.get("decision") or "").strip()]
    if undecided:
        return bad(f"疑似条目未落人工裁定：{len(undecided)} 条")
    decisions = ctx.json("run/b1/dedupe-decisions.json")["decisions"]
    verdicts = {dec["verdict"] for dec in decisions}
    if not {"合并", "判异篇"} <= verdicts:
        return bad(f"裁定类型不全（需含合并与判异篇）：{verdicts}")
    return ok(f"疑似 {len(suspected)} 条全部有裁定；裁定 {len(decisions)} 条含合并与判异篇；decisions_unused 为空")


@spec("B-04", "分桶", "精确", "分步", "去重键三层就位", "run/b1/dedupe-report.json／agent/screening-log.md", "DOI 一级键、标题二级键、中文三段键均在案", "—")
def b04(ctx):
    log = ctx.text("agent/screening-log.md")
    for key in ("DOI", "标题归一", "三段键"):
        if key not in log:
            return bad(f"筛选日志缺去重键说明：{key}")
    return ok("三层键（DOI／标题归一／中文三段键）在筛选日志在案，去重报告按轮记数")


@spec("B-05", "分桶", "容差", "分步", "去重计数守恒与检索池分量", "run/b1/dedupe-report.json", "每轮 input = kept + merged；检索池分量（origin=discovery）在 [200, 550]；扩展／追加分量分列读数、不断言", "检索池分量 200–550（范围）；上界由检索宽度模型给：检索池 12 个（六族×双窗）×双源 ×≤30 原始命中 ≈ 700 上限，三轮去重保留率实测 0.68 → ≈480，留约 15% provider 波动余量；上界只圈**检索池分量**——引文扩展／追加轮分量分列读数、不参与判定，故机制扩展不再顶穿上界、基线宽度保持告警意义；计数守恒为派生等式")
def b05(ctx):
    d = ctx.json("run/b1/dedupe-report.json")
    for r in d["rounds"]:
        if r["input_count"] != r["kept_count"] + r["merged_count"]:
            return bad(f"{r['round']} 计数不守恒：{r['input_count']} ≠ {r['kept_count']}＋{r['merged_count']}")
    final = d["final_count"]
    counts = origin_counts(d)
    if counts is None:  # 旧形态件（无 final_records）：分量不可分，按终池总数记（ADR-0008，读数在报告可见）
        ctx.note("B-05：dedupe-report 无 final_records（旧形态件），检索池分量按终池总数记，机制分量不可分（ADR-0008）")
        if not 200 <= final <= 550:
            return bad(f"终池 {final} 落在容差范围 [200, 550] 之外（旧形态件，检索池分量按终池总数记）")
        return ok(f"三轮计数守恒；终池 {final}（旧形态件无 final_records：检索池分量取终池总数；范围 200–550）", metric=final)
    if sum(counts.values()) != final:
        return bad(f"终池构成与 final_count 不符：{sum(counts.values())} ≠ {final}")
    split = "｜".join(f"{o} {n}" for o, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    discovery = counts.get(DISCOVERY_ORIGIN, 0)
    if not 200 <= discovery <= 550:
        return bad(f"检索池分量（origin=discovery）{discovery} 落在容差范围 [200, 550] 之外（分量 终池 {final}：{split}）")
    return ok(f"三轮计数守恒；检索池分量（origin=discovery）{discovery}（范围 200–550）；分量分列 终池 {final}：{split}"
              f"（扩展／追加分量分列不断言）", metric=discovery)


@spec("B-06", "分桶", "精确", "分步", "排序列：五维＋冻结权重＋未知维＋E 级不进排序", "run/b1/sort-report.json", "权重版本冻结在 sort-weights-v2、逐行记五分项（rel／rec／imp／comp／cn）与未知维、无 E 级字段", "—")
def b06(ctx):
    s = ctx.json("run/b1/sort-report.json")
    if not s.get("frozen") or SORT_WEIGHT_VERSION not in s.get("weight_version", ""):
        return bad(f"排序权重未冻结或版本非 {SORT_WEIGHT_VERSION}")
    want = set(SORT_DIMS)
    if set(s["weights"]) != want:
        return bad(f"排序维度不是五维（{'／'.join(SORT_DIMS)}）：{sorted(s['weights'])}")
    for row in s["rows"]:
        if set(row.get("dims", {})) != want:
            return bad(f"行 {row['key']} 缺五分项（{'／'.join(SORT_DIMS)}）：{sorted(row.get('dims', {}))}")
        if "unknown_dims" not in row or "weight_version" not in row:
            return bad(f"行 {row['key']} 缺未知维或权重版本标记")
        if any(k in row for k in ("grade", "e_level", "design_level")):
            return bad(f"行 {row['key']} 携带证据等级字段，E 级混入排序")
    return ok(f"{len(s['rows'])} 行五分项齐（{'／'.join(SORT_DIMS)}）、权重版本逐行在案（{SORT_WEIGHT_VERSION}）、"
              f"无 E 级字段；未知维标记 {sum(1 for r in s['rows'] if r['unknown_dims'])} 行")


@spec("B-07", "分桶", "精确", "分步", "被引快照带查询日期与来源", "run/b1/cited-counts.json", "snapshot 含查询日期与来源，逐条被引带时间戳口径", "—")
def b07(ctx):
    c = ctx.json("run/b1/cited-counts.json")["snapshot"]
    if not c.get("query_date") or not c.get("source"):
        return bad("被引快照缺查询日期或来源")
    return ok(f"被引快照：{c['query_date']}｜{c['source'][:32]}…；无覆盖 {len(c.get('no_coverage', []))} 条记未知维")


@spec("B-08", "分桶", "容差", "软项", "S 公式未知维不计分母且归一在 [0,1]", "run/b1/sort-report.json", "所有 S ≤ 1；缺覆盖条目不因未知维降分", "S ∈ [0,1]（范围）")
def b08(ctx):
    s = ctx.json("run/b1/sort-report.json")
    over = [r["key"] for r in s["rows"] if not 0.0 <= r["score"] <= 1.0]
    if over:
        return bad(f"越界分数：{over[:3]}")
    unknown = [r for r in s["rows"] if r["unknown_dims"]]
    negative = [r["key"] for r in unknown if r["score"] <= 0]
    if negative:
        return bad(f"缺覆盖条目被打成零分：{negative[:3]}")
    return ok(f"S ∈ [0,1]；{len(unknown)} 行含未知维仍按已知维计分", metric=round(max(r["score"] for r in s["rows"]), 3))


def queue_entries(ctx):
    """研读选取集条目（`agent/download-queue.txt` 一行一条 DOI）。

    首行是口径注记行（`# 研读选取集…`，ADR-0032 §3）：`#` 开头的行不是条目，计数与逐条比对都不算
    （读法沿 `step3_queue.read_queue`）。
    """
    return [line.strip() for line in ctx.text("agent/download-queue.txt").splitlines()
            if line.strip() and not line.strip().startswith("#")]


@spec("B-09", "分桶", "精确", "分步", "四桶报告与研读选取集一致", f"{BUCKET_REPORT}＋agent/download-queue.txt", "桶计数之和 == 队列条目数（＝研读选取集条目数）；需机构／灰色候选桶如实", "—")
def b09(ctx):
    b = ctx.text(BUCKET_REPORT)
    bucket = re.search(r"开放直下（OA direct）\s*\|\s*(\d+)", b)
    repo = re.search(r"仓储副本（repository copy）\s*\|\s*(\d+)", b)
    inst = re.search(r"需机构（institutional）\s*\|\s*(\d+)", b)
    pending = re.search(r"(?:待用户定夺（pending，含灰色源）|灰色候选（grey[^）]*）)\s*\|\s*(\d+)", b)
    if not all((bucket, repo, inst, pending)):
        return bad("四桶报告缺桶计数")
    queue = len(queue_entries(ctx))
    total = sum(int(m.group(1)) for m in (bucket, repo, inst, pending))
    if total != queue:
        return bad(f"桶计数之和 {total} ≠ 队列条目数 {queue}")
    return ok(f"四桶 {bucket.group(1)}／{repo.group(1)}／{inst.group(1)}／{pending.group(1)}；"
              f"队列条目数 {queue}（＝研读选取集），与桶计数一致")


@spec("B-10", "分桶", "精确", "分步", "中文补充动作 log 六要素与分列计数", "agent/b1-and-regulatory-supplement.md", "库名／检索词／日期／命中／导出／解析／汇入齐，中英分别计数后再报合并", "—")
def b10(ctx):
    log = ctx.text("agent/b1-and-regulatory-supplement.md")
    for key in ("库名", "检索词", "执行日期", "命中", "导出", "解析", "汇入"):
        if key not in log:
            return bad(f"中文补充动作 log 缺要素：{key}")
    if "中英分别计数" not in log or "合并去重后" not in log:
        return bad("中文补充 log 缺中英分别计数或合并计数")
    for db in ("CNKI", "万方", "维普"):
        if db not in log:
            return bad(f"三库动作缺：{db}")
    return ok("三库动作 log 齐六要素，中英分别计数后报合并计数")


@spec("B-11", "分桶", "精确", "分步", "分桶输出即下载输入", "agent/download-queue.txt vs run/download-results*.json", "队列（研读选取集）条目在下载收据中有对应记录", "—")
def b11(ctx):
    queue = {norm_doi(line) for line in queue_entries(ctx)}
    receipts = {norm_doi(e.get("doi") or e.get("identifier", "")) for e in ctx.json("run/download-results.json")["entries"]}
    missing = sorted(q for q in queue if q not in receipts)
    if missing:
        return bad(f"队列条目无下载收据：{missing}")
    return ok(f"队列（研读选取集）{len(queue)} 条全部有下载收据（第一批）")


@spec("B-12", "分桶", "顺序", "分步", "排序列按 S 降序（相对顺序断言）", "run/b1/sort-report.json", "rows 按 score 非升序；order 字段与行位置一致", "顺序断言：只判相对先后，不断言名次与分值")
def b12(ctx):
    rows = ctx.json("run/b1/sort-report.json")["rows"]
    scores = [r["score"] for r in rows]
    bad_i = next((i for i, (a, b) in enumerate(zip(scores, scores[1:])) if a < b), None)
    if bad_i is not None:
        return bad(f"第 {bad_i + 1} 与第 {bad_i + 2} 行逆序：{scores[bad_i]} < {scores[bad_i + 1]}")
    orders = [r.get("order") for r in rows]
    if all(isinstance(o, int) for o in orders) and orders != sorted(orders):
        return bad("order 字段与行位置不一致")
    return ok(f"{len(rows)} 行按 S 降序（首行 {scores[0]}，尾行 {scores[-1]}）；只判相对先后")


# ---------------------------------------------------------------- 断言：下载（Step3）


def receipts(ctx):
    out = []
    for rel in ("run/download-results.json", "run/download-results-03.json"):
        for e in ctx.json(rel)["entries"]:
            e = dict(e)
            e["_file"] = rel
            e["_ident"] = norm_doi(e.get("doi") or e.get("identifier", ""))
            e["_ok"] = bool(e.get("success", e.get("adopted")))
            out.append(e)
    return out


@spec("C-01", "下载", "一致性", "硬门禁", "实际来源如实（报告与收据逐条一致）", "agent/bucketing-and-sources.md vs run/download-results*.json", "每条收据的标识都在报告中有行，且报告来源与收据一致（或标不采用）", "—")
def c01(ctx):
    report_lines = [l.lower() for l in ctx.text("agent/bucketing-and-sources.md").splitlines()]
    checked, missing = 0, []
    for e in receipts(ctx):
        dois = [norm_doi(t) for t in re.findall(r"10\.\d{4,9}/[^\s，,;；|｜]+", e["_ident"])]
        if not dois:
            continue
        source = (e.get("source") or "").strip()
        for doi in dois:
            hits = [l for l in report_lines if doi in l]
            if not hits:
                missing.append(f"{doi} 未在下载报告中出现（收据来源 {source or '无'}）")
                continue
            checked += 1
            if source and source.lower() != "none" and not any(source.lower() in l or "不采用" in l for l in hits):
                missing.append(f"{doi} 报告行未如实出现收据来源 {source}")
    if missing:
        return bad(f"{len(missing)} 处来源台账不符：{missing[0]}")
    if checked == 0:
        return bad("报告与收据无可比对条目")
    return ok(f"逐条比对 {checked} 条 DOI，报告实际来源与收据一致，无漏报")


@spec("C-02", "下载", "精确", "分步", "灰色命中如实报告且渠道口径在案", "agent/bucketing-and-sources.md＋收据", "灰色来源条目逐条如实（采纳或标不采用）；报告声明本轮灰色渠道口径（默认启用或明确关闭），历史包文件头口径注记（pre-0006）同样计数", "—")
def c02(ctx):
    report = ctx.text("agent/bucketing-and-sources.md")
    if not re.search(r"灰色.{0,40}?(默认启用|默认关闭|不启用|未启用)", report) and not ctx.historical_caliber:
        return bad("下载报告未声明灰色渠道口径")
    grey = [e for e in receipts(ctx) if any(g in (e.get("source") or "").lower() for g in GREY_SOURCES)]
    if not grey:
        return ok("本轮无灰色命中；灰色渠道口径在案")
    lines = [l.lower() for l in report.splitlines()]
    for e in grey:
        dois = [norm_doi(t) for t in re.findall(r"10\.\d{4,9}/[^\s，,;；|｜]+", e["_ident"])]
        hits = [l for l in lines if any(d in l for d in dois)]
        source = (e.get("source") or "").lower()
        if not hits:
            return bad(f"灰色命中未在报告中出现：{e.get('identifier')}")
        if not any(source in l or "不采用" in l for l in hits):
            return bad(f"灰色命中未如实标注来源或采纳状态：{e.get('identifier')}｜{e.get('source')}")
    adopted = [e.get("identifier") for e in grey if e["_ok"]]
    caliber = "（历史口径注记在案：pre-0006，灰色不采用按当时口径读，不判现行失败）" if ctx.historical_caliber else ""
    return ok(f"灰色命中 {len(grey)} 条逐条如实（采纳 {len(adopted)} 条）；灰色渠道口径在案{caliber}")


@spec("C-03", "下载", "精确", "硬门禁", "默认通道之外无静默改动", "run/download-results*.json＋报告", "默认之外（机构登录／设置改动）零静默发生；报告声明通道口径", "—")
def c03(ctx):
    b = ctx.text("agent/bucketing-and-sources.md")
    if not any(k in b for k in ("静默", "零改动", "未改", "不改")):
        return bad("下载报告未声明默认通道之外的改动纪律")
    srcs = " ".join((e.get("source") or "").lower() for e in receipts(ctx))
    if any(k in srcs for k in ("webvpn", "carsi", "ezproxy", "institutional", "机构")):
        if not any(k in b for k in ("登录", "机构")):
            return bad("收据出现机构通道来源但报告未记登录动作")
    return ok("默认之外零静默改动声明在案；机构通道若出现均有记录")


@spec("C-04", "下载", "精确", "分步", "成功计数与收据一致", "run/download-results*.json", "成功／采用计数与条目、队列条目数一致", "—")
def c04(ctx):
    first = ctx.json("run/download-results.json")
    if first["succeeded"] != sum(1 for e in first["entries"] if e.get("success")):
        return bad("第一批成功计数与条目不一致")
    if first["total"] != len(first["entries"]):
        return bad(f"第一批 total {first['total']} ≠ 条目数 {len(first['entries'])}")
    third = ctx.json("run/download-results-03.json")
    s = third["summary"]
    queue = len([l for l in ctx.text("doi-list-03.txt").splitlines() if l.strip()])
    if s["attempted"] != queue:
        return bad(f"第二批 attempted {s['attempted']} ≠ 队列条目数 {queue}")
    if s["adopted"] != sum(1 for e in third["entries"] if e.get("adopted")):
        return bad("第二批 adopted 计数与条目不一致")
    return ok(f"第一批 {first['succeeded']}/{first['total']}；第二批 attempted {s['attempted']}／adopted {s['adopted']}，与队列 {queue} 条一致")


@spec("C-05", "下载", "精确", "分步", "付费墙回退：待重试且访问设置零改动", "agent/bucketing-and-sources.md＋agent/screening-log.md", "被挡住条目记待机构后重试；未改任何访问设置", "—")
def c05(ctx):
    b = ctx.text("agent/bucketing-and-sources.md")
    if "待机构后重试" not in b and "待接机构后重试" not in b:
        return bad("被挡住条目未记待重试")
    if "访问设置" not in b and "未改任何访问配置" not in b and "未改配置" not in b:
        return bad("下载报告缺访问设置零改动的记录")
    if "paywall" not in b and "付费墙" not in b and "no PDF found" not in b:
        return bad("下载报告未记录被挡住的原因")
    return ok("付费墙条目走 AFK 回退：记待机构后重试、访问设置零改动、原因在案")


@spec("C-06", "下载", "容差", "硬门禁", "开放类锚点全部成功获取且来源如实", "锚点清单 vs run 收据", "oa_journal 锚点在收据中 success=true；来源不限但逐篇如实（允许灰色）", "命中即过；来源只判如实，不锁定具体 provider 与渠道")
def c06(ctx):
    anchors = ctx.anchors["anchors"]
    oa = [a for a in anchors if a["kind"] == "oa_journal"]
    if not oa:
        return bad("锚点清单没有开放类锚点")
    receipts_ = {e["_ident"]: e for e in receipts(ctx)}
    miss = []
    grey_hit = 0
    for a in oa:
        e = receipts_.get(norm_doi(a["identifier"]))
        if not e:
            miss.append(f"{a['id']} 无收据")
        elif not e["_ok"]:
            miss.append(f"{a['id']} 未成功获取（{e.get('reason') or e.get('source')}）")
        elif any(g in (e.get("source") or "").lower() for g in GREY_SOURCES):
            grey_hit += 1
    if miss:
        return bad("；".join(miss))
    note = f"，其中 {grey_hit} 条来源为灰色（如实报告）" if grey_hit else ""
    return ok(f"开放类锚点 {len(oa)}/{len(oa)} 成功获取{note}", metric=len(oa))


@spec("C-07", "下载", "精确", "分步", "采用条目逐篇有身份核验", "run/download-results-03.json", "adopted=true 条目的 identity_check 非空", "—")
def c07(ctx):
    third = ctx.json("run/download-results-03.json")
    adopted = [e for e in third["entries"] if e.get("adopted")]
    if not adopted:
        return bad("没有 adopted 条目可核")
    empty = [e["identifier"] for e in adopted if not (e.get("identity_check") or "").strip()]
    if empty:
        return bad(f"采用条目缺身份核验：{empty}")
    return ok(f"{len(adopted)} 条采用条目全部有身份核验记录")


# ---------------------------------------------------------------- 断言：落点（Step4）


@spec("D-01", "落点", "精确", "分步", "写库前有 dry-run 计划表", "agent/placement-plan.md", "计划表逐行给候选落点且首个为推荐位", "—")
def d01(ctx):
    p = ctx.text("agent/placement-plan.md")
    rows = [r for r in md_table(p) if r and r[0] and ("DOI" in r[0] or "待建" in r[0] or r[0].startswith("X") or r[0].startswith("Y") or r[0].startswith("K"))]
    if not rows:
        rows = [r for r in md_table(p) if len(r) >= 4]
    if not rows:
        return bad("落点计划表为空")
    first = rows[0]
    if len(first) < 4 or "推荐" not in " ".join(first):
        return bad("计划表未标推荐位")
    if "未分类" not in p:
        return bad("计划表缺未分类破冰项（AFK 回退落点）")
    return ok(f"计划表 {len(rows)} 行，含推荐位与未分类破冰项")


@spec("D-02", "落点", "精确", "分步", "写库一次确认（单篇不例外）", "agent/placement-plan.md", "确认结果列非空且声明单篇同样先问", "—")
def d02(ctx):
    p = ctx.text("agent/placement-plan.md")
    if "确认结果" not in p:
        return bad("计划表缺确认结果列")
    if "单篇不例外" not in p:
        return bad("计划未声明单篇不例外")
    if not re.search(r"确认已发生|用户选定|一次全定", p):
        return bad("未记录一次确认已发生")
    return ok("确认门在案：计划→一次确认（含单篇不例外）")


@spec("D-03", "落点", "精确", "硬门禁", "落点回查一致（计划＝写入＝回查）", "agent/placement-plan.md", "计划数、写入数、回查一致数三数一致", "报告级事实断言；独立核对由 D-07（本地库只读回查）承担")
def d03(ctx):
    p = ctx.text("agent/placement-plan.md")
    m = re.search(r"计划数\s*(\d+)\s*[｜|]\s*写入数\s*(\d+)\s*[｜|]\s*回查一致数\s*(\d+)", p)
    if not m:
        return bad("未记录计划／写入／回查三数")
    plan, wrote, rechecked = (int(x) for x in m.groups())
    if not (plan == wrote == rechecked):
        return bad(f"回查不一致：计划 {plan}／写入 {wrote}／回查 {rechecked}")
    if plan < 1:
        return bad("计划数为零，未验证回查闭环")
    return ok(f"计划 {plan}＝写入 {wrote}＝回查一致 {rechecked}")


@spec("D-04", "落点", "精确", "分步", "在库判定只认 DOI 字段", "agent/placement-plan.md", "声明在库判定口径为 DOI，附件文本命中不算", "—")
def d04(ctx):
    p = ctx.text("agent/placement-plan.md")
    if "只认 DOI" not in p and "只认DOI" not in p:
        return bad("未声明在库判定只认 DOI")
    if "附件文本" not in p:
        return bad("未声明附件文本命中不算收录")
    return ok("在库判定口径在案：只认 DOI 字段，附件文本命中不算")


@spec("D-05", "落点", "精确", "分步", "落点沿内部落点指南（指南分流／多归类并存）", "agent/placement-plan.md vs references/protocols/placement-guide.md", "候选含指南目录；多归类并存只在语义成立时出现", "—")
def d05(ctx):
    p = ctx.text("agent/placement-plan.md")
    guide = ctx.atext("references/protocols/placement-guide.md")
    if "placement-guide.md" not in p:
        return bad("计划表未指向 skill 内落点指南")
    if "指南" not in guide or "多归类并存" not in guide:
        return bad("落点指南缺指南分流或多归类并存条")
    if "多归类并存" not in p:
        return bad("计划表未体现多归类并存候选")
    return ok("计划表按 skill 内落点指南计算，指南分流与多归类并存候选在案")


@spec("D-06", "落点", "精确", "分步", "监管条目落点：有子集进子集、无子集暂落不新建", "agent/placement-plan.md＋agent/b1-and-regulatory-supplement.md", "结论含监管子集判定与不擅自新建", "—")
def d06(ctx):
    p = ctx.text("agent/placement-plan.md")
    b = ctx.text("agent/b1-and-regulatory-supplement.md")
    if "监管子集" not in p or ("暂落" not in p and "不擅自新建" not in p):
        return bad("落点计划缺监管子集结论")
    if "不擅自新建" not in b and "不擅自新建" not in p:
        return bad("监管落点未声明不擅自新建")
    return ok("监管落点结论在案：库内无监管子集 → 暂落药品目录，不擅自新建")


@spec("D-07", "落点", "一致性", "硬门禁", "在库回查与报告一致（本地只读，含本次合集）", "本地 Zotero API vs agent/placement-plan.md", "计划里的条目键在库存在且收藏集与报告一致（全角／半角与大小写归一后比）；标本次合集已挂者库内必含本次合集名", "联网只读；live 模式下取不到即判不过（fail-closed），--no-live 显式放弃")
def d07(ctx):
    if not ctx.live:
        return unverified("未执行（--no-live）：本地 Zotero 回查跳过；本轮由 D-03 承担文档级回查一致")
    results, note = zotero_recheck(ctx)
    if results is None:
        ctx.note(f"在库回查未执行：{note}")
        return unverified(f"独立核对缺位：{note}")
    p = ctx.text("agent/placement-plan.md")
    plan_norm = norm_label(p)
    name, _mode, _mn = plan_topic_line(p)
    _col, topic_cells = plan_topic_cells(p)
    topic_hung = {k for k, c in topic_cells if c.startswith("已挂")}
    wanted = {norm_label(name), norm_label(name.split("/")[-1])} if name else set()
    for item in results:
        if item["key"] not in p:
            return bad(f"回查条目 {item['key']} 未出现在计划表")
        for col in item["collections"]:
            if norm_label(col) not in plan_norm:
                return bad(f"{item['key']} 的收藏集 {col} 与报告不一致")
        if name and item["key"] in topic_hung and wanted.isdisjoint({norm_label(c) for c in item["collections"]}):
            return bad(f"{item['key']} 标本次合集已挂但库内无 {name}")
    return ok("；".join(f"{i['key']}（{'＋'.join(i['collections'])}）" for i in results))


@spec("D-08", "落点", "一致性", "硬门禁", "附件回查一致（附件列 vs 库内 children）", "本地 Zotero API vs agent/placement-plan.md 附件列", "附件列逐条「已挂：文件」或「无 PDF：去向」；标已挂者库内必有 PDF 附件，标无 PDF 者库内无 PDF 附件；live 取不到即判不过（fail-closed）", "联网只读；live 模式下取不到即判不过（fail-closed），--no-live 显式放弃")
def d08(ctx):
    p = ctx.text("agent/placement-plan.md")
    col, plan_cells = plan_attachment_cells(p)
    if not plan_cells:
        return bad("附件回查：计划表未取到条目行")
    if col is None:
        return bad("附件回查：计划表缺附件列（挂载结果）")
    cells = {}
    for key, cell in plan_cells:
        if not cell:
            return bad(f"附件回查：{key} 行缺附件列（挂载结果）")
        if not (cell.startswith("已挂") or cell.startswith("无 PDF")):
            return bad(f"附件回查：{key} 附件列取值不合口径：{cell[:40]}")
        cells[key] = cell
    if "附件回查：" not in p:
        return bad("附件回查：计划表缺附件计数行（附件回查：已挂 N｜无 PDF K）")
    if not ctx.live:
        return unverified("未执行（--no-live）：附件在库回查跳过；本轮以计划表附件列为准")
    results, note = zotero_recheck(ctx)
    if results is None:
        ctx.note(f"附件在库回查未执行：{note}")
        return unverified(f"独立核对缺位：{note}")
    by_key = {i["key"]: i for i in results}
    missing = [k for k in cells if k not in by_key]
    if missing:
        return bad(f"附件回查：条目不在库：{missing[:3]}")
    for key, cell in cells.items():
        pdfs = by_key[key].get("pdf_attachments") or []
        if cell.startswith("已挂") and not pdfs:
            return bad(f"附件回查：{key} 标已挂但库内无 PDF 附件")
        if cell.startswith("无 PDF") and pdfs:
            return bad(f"附件回查：{key} 标无 PDF 但库内已有 PDF 附件（{pdfs[0][:40]}）")
    n_att = sum(1 for c in cells.values() if c.startswith("已挂"))
    return ok(f"附件回查一致：已挂 {n_att}／无 PDF {len(cells) - n_att}")


@spec("D-09", "落点", "精确", "硬门禁", "附件缺口进局限声明（无 PDF 条目披露）", "agent/placement-plan.md 附件列 vs agent/knowledge-package.md 局限声明", "每个「无 PDF」条目的条目键出现在局限声明；无 PDF 为 0 时可省", "—")
def d09(ctx):
    p = ctx.text("agent/placement-plan.md")
    _col, plan_cells = plan_attachment_cells(p)
    no_pdf = []
    for key, cell in plan_cells:
        if cell.startswith("无 PDF") and key not in no_pdf:
            no_pdf.append(key)
    if not no_pdf:
        return ok("无 PDF 条目为 0，无附件缺口披露义务")
    lim = find_h2(ctx.text("agent/knowledge-package.md"), "局限声明") or ""
    undisclosed = [k for k in no_pdf if k not in lim]
    if undisclosed:
        return bad(f"附件缺口未进局限声明：{undisclosed[:4]}")
    return ok(f"无 PDF {len(no_pdf)} 条均在局限声明披露")


@spec("D-10", "落点", "精确", "硬门禁", "本次合集回查一致（集行＋逐条挂载）", "agent/placement-plan.md 本次合集行与本次合集列", "集名形如 med-lit-review/<交付目录名>且建／复用／AFK 自动建在案；逐条「已挂：集名」或「未挂：去向」；双挂／追挂一致 M／N 且 M＝N；P2／P3 记不建集", "旧口径包无本次合集列按当时口径读记通过（本次合集口径三级读法：落点计划注记 → manifest caliber.topic_collection → 有无本次合集列推断）；独立库内一致由 D-07 承担")
def d10(ctx):
    p = ctx.text("agent/placement-plan.md")
    if "不建集（写库设计跳过）" in p:
        _col, _cells = plan_topic_cells(p)
        if _cells:
            return bad("本次合集：写库设计跳过却有本次合集逐条挂载")
        return ok("本次合集：不建集（写库设计跳过，P2／P3）")
    name, mode, (m, n) = plan_topic_line(p)
    if not name or not name.startswith("med-lit-review/"):
        _col, _cells = plan_topic_cells(p)
        if ctx.historical_topic or (_col is None and not _cells):
            caliber = "（历史口径注记在案：absent，按当时口径读）" if ctx.historical_topic else ""
            return ok(f"本次合集：旧口径包（无本次合集列），按当时口径读记通过{caliber}")
        return bad("本次合集：未记录集名 med-lit-review/<交付目录名>")
    if mode not in ("建", "复用", "AFK 自动建"):
        return bad("本次合集：未声明建／复用／AFK 自动建")
    col, cells = plan_topic_cells(p)
    if col is None or not cells:
        return bad("本次合集：缺逐条本次合集列")
    bad_cells = [(k, c[:40]) for k, c in cells if not (c.startswith("已挂") or c.startswith("未挂"))]
    if bad_cells:
        return bad(f"本次合集：{bad_cells[0][0]} 本次合集列取值不合口径：{bad_cells[0][1]}")
    if m is None or n is None:
        return bad("本次合集：未记录双挂／追挂一致 M／N")
    if m != n or n != len(cells):
        return bad(f"本次合集：双挂／追挂一致 {m}／{n}，逐条 {len(cells)} 行（须三者相等）")
    hung = sum(1 for _, c in cells if c.startswith("已挂"))
    if hung != n:
        return bad(f"本次合集：一致数 {n} 与已挂数 {hung} 不符")
    lim = find_h2(ctx.text("agent/knowledge-package.md"), "局限声明") or ""
    unhung = [k for k, c in cells if c.startswith("未挂")]
    undisclosed = [k for k in unhung if k not in lim]
    if undisclosed:
        return bad(f"本次合集缺口未进局限声明：{undisclosed[:4]}")
    if mode == "AFK 自动建" and "AFK 自动建挂" not in lim and "AFK 自动建挂" not in p:
        return bad("本次合集：AFK 自动建挂未在落点报告与局限声明留痕")
    return ok(f"本次合集一致：{name}｜{mode}｜双挂／追挂一致 {m}／{n}")


@spec("D-11", "落点", "一致性", "硬门禁", "摘要声明↔库内一致（摘要列与摘要计数行 vs 库内 abstractNote）", "本地 Zotero API vs agent/placement-plan.md 摘要列", "摘要列逐条取值须在口径内（已填：来源／未填：状态／不适用；来源取 table／crossref／pubmed／openalex／库内——「库内」＝条目本已有 abstractNote、本轮未取数），取值不在口径内即判不过；凡标「已填」（含「已填：库内」）者库内 abstractNote 必须非空；摘要计数行须与逐条解析一致——计数行「摘要：已填 N（table a／crossref b／pubmed c／openalex d／库内 e）／未填 M／不适用 K」里五分源之和＝N、N／M／K 分别等于三类行数、N+M+K＝逐条行数（逐条＝全部带「摘要」列的表汇总后逐行）；live 模式取不到即判不过（fail-closed）", "联网只读；live 模式下取不到即判不过（fail-closed），--no-live 显式放弃；无「摘要」列者按摘要口径三级读法判（落点计划注记「摘要口径：absent／present」→ manifest caliber.abstract_column → 有无该列推断；ADR-0008）——`absent` 按当时口径读记通过并列判定来源、`present` 的现行包漏列即判不过（不静默豁免）；写库设计跳过的块（P1／P2／P3）自报不含计划表与摘要列——自报属实则记明原因不判失败，该块仍带计划表行即判不过（镜像 D-10）；带「摘要」列的表全部计入：同一键两表声明不一致即判不过，正文多条摘要计数行须条条与全表逐条解析一致；--zotero-fixture 代替库读时读数在观察文案与留痕里标明（非真实库读）；缺口非阻断（未填／不适用不进失败，ADR-0021）；独立断言，不进 H-04 汇总")
def d11(ctx):
    p = ctx.text("agent/placement-plan.md")
    tables = plan_abstract_tables(p)
    plan_rows = [line for line in p.splitlines() if re.match(r"^\|\s*[A-Z0-9]{8}(?![A-Z0-9])", line)]
    if not tables:
        if "写库设计跳过" in p:  # 未写库块（P1／P2／P3 设计跳过）：该块自报不含计划表与摘要列，自报不实即判不过（镜像 D-10）
            if plan_rows:
                return bad(f"摘要一致：自报写库设计跳过（本块不含计划表）却取到 {len(plan_rows)} 行计划表（条目键行）")
            return ok("摘要一致：写库设计跳过（本块不含计划表与摘要列），本轮无摘要声明可核")
        if not plan_rows:
            return bad("摘要一致：落点计划未取到计划表行（条目键行），无从核摘要声明")
        if ctx.historical_abstract:
            return ok(
                "摘要一致：旧口径包（无「摘要」列），按当时口径读记通过（ADR-0008）；"
                f"判定依据 摘要口径＝absent（{ctx.abstract_caliber_source}）"
            )
        return bad(f"摘要一致：现行口径包漏列「摘要」列（摘要口径＝{ctx.abstract_caliber}，依据 {ctx.abstract_caliber_source}；write block 漏列不豁免）")
    declared = {}
    for _col, table in tables:
        for key, cell in table:
            if key in declared and declared[key] != cell:
                return bad(f"摘要一致：{key} 在两张带「摘要」列的表里声明不一致（{declared[key][:24]}／{cell[:24]}）")
            declared[key] = cell
    cells = list(declared.items())
    filled, unfilled, na = [], [], []
    for key, cell in cells:
        if cell.startswith("已填："):
            src = cell[len("已填："):].strip()
            if src not in ABSTRACT_SOURCES:
                return bad(f"摘要一致：{key} 摘要列来源不在口径表内（{cell[:40]}）")
            filled.append((key, src))
        elif cell.startswith("未填："):
            if not cell[len("未填："):].strip():
                return bad(f"摘要一致：{key} 摘要列「未填」未记状态（{cell[:40]}）")
            unfilled.append(key)
        elif cell.startswith("不适用"):
            na.append(key)
        else:
            return bad(f"摘要一致：{key} 摘要列取值不合口径（{cell[:40]}）")
    counts = ABSTRACT_COUNT_RE.findall(p)
    if not counts:
        return bad("摘要一致：缺摘要计数行（摘要：已填 N（table a／crossref b／pubmed c／openalex d／库内 e）／未填 M／不适用 K）")
    scope = ""
    if len(tables) > 1 or len(counts) > 1:  # 多表／多计数行：全表汇总后逐条核，不做单表取证
        scope = f"（带「摘要」列的表 {len(tables)} 张、逐条 {len(cells)} 行、摘要计数行 {len(counts)} 条）"
        ctx.note(f"落点计划的「摘要」列分布在 {len(tables)} 张表、正文有 {len(counts)} 条摘要计数行：按全表逐条汇总核")
    for n_line, inner, m_line, k_line in counts:
        n_i, m_i, k_i = int(n_line), int(m_line), int(k_line)
        per_src = {name: int(n) for name, n in re.findall(rf"({ABSTRACT_SOURCES_RE})\s*(\d+)", inner)}
        if set(per_src) != set(ABSTRACT_SOURCES) or sum(per_src.values()) != n_i:
            return bad(f"摘要一致：计数行分源数与已填总数不符（{inner.strip()}｜已填 {n_i}）{scope}")
        if (n_i, m_i, k_i) != (len(filled), len(unfilled), len(na)):
            return bad(
                f"摘要一致：计数行 已填 {n_i}／未填 {m_i}／不适用 {k_i} "
                f"与逐条解析 {len(filled)}／{len(unfilled)}／{len(na)} 不符{scope}"
            )
        for name in ABSTRACT_SOURCES:
            got = sum(1 for _k, s in filled if s == name)
            if per_src[name] != got:
                return bad(f"摘要一致：计数行 {name} {per_src[name]} 与逐条解析 {got} 不符{scope}")
        if n_i + m_i + k_i != len(cells):
            return bad(f"摘要一致：计数行三数之和 {n_i + m_i + k_i} 与逐条行数 {len(cells)} 不符{scope}")
    if not ctx.live and not getattr(ctx, "zotero_fixture", None):
        return unverified("未执行（--no-live）：摘要声明与库内 abstractNote 的一致核对跳过（live 下取不到即判不过）")
    results, note = zotero_recheck(ctx)
    if results is None:
        ctx.note(f"摘要一致回查未执行：{note}")
        return unverified(f"独立核对缺位：{note}")
    via = ""
    if getattr(ctx, "zotero_fixture", None):  # 夹具代替真实库读：读数可辨（替代事实与共用关系由 zotero_recheck 记入留痕）
        via = "（库内读数由自检夹具代替，非真实库读）"
    by_key = {i["key"]: i for i in results}
    for key, src in filled:
        item = by_key.get(key)
        if item is None:
            return bad(f"摘要一致：{key} 标已填（{src}）但库内未取到该条目{via}")
        if not (item.get("abstract_note") or "").strip():
            return bad(f"摘要一致：{key} 标已填（{src}）但库内 abstractNote 为空{via}")
    return ok(
        f"摘要声明与库内一致：已填 {len(filled)} 条条目 abstractNote 均非空"
        f"（{'／'.join(f'{n} {per_src[n]}' for n in ABSTRACT_SOURCES)}）；未填 {m_line}／不适用 {k_line} 照记不阻断{via}"
    )


# ---------------------------------------------------------------- 断言：研读（Step5）


@spec("E-01", "研读", "精确", "硬门禁", "八节齐全且新顺序固定（结论在证据表之前）", "agent/knowledge-package.md", "1–8 八节按新顺序出现：执行摘要→研究问题→检索策略→主题式证据综合→结论→证据表→方法附录→局限声明", "—")
def e01(ctx):
    titles = [t for t, _ in package_sections(ctx)]
    want = ["执行摘要", "研究问题", "检索策略", "主题式证据综合", "结论", "证据表", "方法附录", "局限声明"]
    got = []
    for w in want:
        hit = next((t for t in titles if t.endswith(w) or w in t), None)
        if not hit:
            return bad(f"缺节：{w}")
        got.append(hit)
    pos = [titles.index(h) for h in got]
    if pos != sorted(pos):
        return bad("节序不符：须按 执行摘要→研究问题→检索策略→主题式证据综合→结论→证据表→方法附录→局限声明（结论在证据表之前）")
    return ok("八节齐且新顺序固定： " + "／".join(got))


@spec("E-02", "研读", "精确", "分步", "七列证据表表头一致", "agent/knowledge-package.md", "表头为文献／设计与等级／人群／干预与暴露／结局与效应量／偏倚备注／与结论的关联", "—")
def e02(ctx):
    body = find_h2(ctx.text("agent/knowledge-package.md"), "证据表") or ""
    head = next(([c.strip() for c in l.strip().strip("|").split("|")] for l in body.splitlines() if l.strip().startswith("|")), [])
    want = ["行号", "文献", "设计与等级", "人群", "干预与暴露", "结局与效应量", "偏倚备注", "与结论的关联"]
    if head != want:
        return bad(f"表头不符：{head}")
    return ok("七列（＋行号）表头一致")


@spec("E-03", "研读", "精确", "硬门禁", "每条实质断言点到证据表行", "agent/knowledge-package.md＋校验脚本 JSON", "正文断言句行号引用可解析；脚本 L-03 零命中", "—")
def e03(ctx):
    rows = evidence_rows(ctx)
    refs = prose_row_refs(ctx)
    if not refs:
        return bad("正文没有任何行号引用，无法验证可追溯")
    dangling = [(expr, ctx_) for expr, ctx_ in refs for rid in expand_row_ref(expr) if rid not in rows]
    if dangling:
        expr, ctx_ = dangling[0]
        return bad(f"行号引用落空：行 {expr}｜{ctx_}")
    findings = (ctx._json.get("__script__") or {}).get("findings", [])
    l03 = [f for f in findings if f.get("rule") == "L-03"]
    if l03:
        return bad(f"L-03 断言后无证据表行：{len(l03)} 条")
    return ok(f"正文行号引用 {len(refs)} 处全部可解析；脚本 L-03 零命中")


@spec("E-04", "研读", "精确", "硬门禁", "行↔卡双向一致（卡是综合唯一输入）", "agent/knowledge-package.md vs agent/cards/*.md", "每行有卡、卡声明行无悬空、无行卡不得声明行号", "—")
def e04(ctx):
    try:
        rows, row_cards, cards_ = row_card_map(ctx)
    except package_parse.CardParseError as exc:  # 卡面先报：0 卡／目录缺不得落到「行 1 没有对应提取卡」
        return bad(str(exc))
    for rid, ids in row_cards.items():
        if not ids:
            row = rows[rid]
            return bad(f"行 {rid} 没有对应提取卡（{row['doi'] or '／'.join(row['num'])}）")
    for cid, c in cards_.items():
        for rid in c["行"]:
            if rid not in rows:
                return bad(f"卡 {cid} 声明行 {rid} 在证据表中不存在")
            if row_cards.get(rid) and cid not in row_cards[rid]:
                return bad(f"卡 {cid} 声明行 {rid}，但该行属于 {row_cards[rid]}")
        owner = [rid for rid, ids in row_cards.items() if cid in ids]
        if owner:
            if not set(c["行"]) & set(owner):
                return bad(f"卡 {cid} 对应的行 {'、'.join(owner)} 未被卡声明（卡声明：{c['声明句'] or '无'}）")
        elif not (c["无行声明"] or c["别名"]):
            return bad(f"卡 {cid} 不在证据表内，但未声明「无证据表行／不进表」")
    declared = sum(len(c["行"]) for c in cards_.values())
    return ok(f"{len(rows)} 行全部有卡且被卡声明；卡声明行 {declared} 处无悬空；表外卡均标不进表")


@spec("E-05", "研读", "精确", "分步", "行内容可在卡中找到（等级／工具／页码出处）", "agent/knowledge-package.md vs agent/cards/*.md", "行的 E 级与工具名被对应卡覆盖；有全文行的卡带页码", "—")
def e05(ctx):
    try:
        rows, row_cards, cards_ = row_card_map(ctx)
    except package_parse.CardParseError as exc:  # 同 E-04：卡面解析失败先报读取路径与解析条数
        return bad(str(exc))
    for rid, row in rows.items():
        ids = row_cards.get(rid)
        if not ids:
            return bad(f"行 {rid} 无卡可核")
        card = cards_[ids[0]]
        levels = set(re.findall(r"E[1-5]", row["设计"]))
        card_levels = set(re.findall(r"E[1-5]", card["设计句"]))
        if levels and not levels <= card_levels:
            return bad(f"行 {rid} 等级 {levels} 未被卡 {ids[0]} 覆盖（卡：{card_levels or '无'}）")
        tools = [t for t in TOOL_NAMES if t.lower() in row["偏倚"].lower()]
        for t in tools:
            if t.lower() not in card["偏倚句"].lower():
                return bad(f"行 {rid} 工具 {t} 未出现在卡 {ids[0]} 的偏倚句")
        if "有全文" in card["全文状态"] and not card["页码范围标注"]:
            return bad(f"卡 {ids[0]} 标有全文但无页码／出处标注")
    return ok(f"{len(rows)} 行逐行核对等级与工具名；有全文行的卡均带页码标注")


@spec("E-06", "研读", "精确", "分步", "可及性标注与局限交代", "agent/knowledge-package.md", "仅摘要／仅题录条目在设计列有标注或入可及性清单，且逐条进局限声明（无此类条目时不适用）", "—")
def e06(ctx):
    rows = evidence_rows(ctx)
    kp = ctx.text("agent/knowledge-package.md")
    lim = find_h2(kp, "局限声明") or ""
    marked = [rid for rid, r in rows.items() if re.search(r"仅摘要|仅题录", r["设计"])]
    manifest = accessibility_entries(ctx)
    key_doi = plan_key_dois(ctx)
    doi_key = {d: k for k, d in key_doi.items()}
    tertiary = [d for d, e in (manifest or {}).items() if str(e.get("accessibility", "")).strip() == "仅题录"]
    if not marked and not tertiary:  # 全采全文的包不该踩这条（ADR-0023：假阳性修复）
        return ok("可及性标注与局限交代：本包无仅摘要／仅题录条目（不适用）")
    lim_rows = set(ROW_MENTION_RE.findall(lim))
    missing = [f"行 {rid}" for rid in marked if rid not in lim_rows]
    for doi in tertiary:  # 仅题录已不占表行（ADR-0023）：按 DOI 交代，历史形态的表内行号亦认
        key = doi_key.get(doi)
        if doi in lim or (key and key in lim_rows):
            continue
        missing.append(f"仅题录 {doi}")
    if missing:
        return bad(f"局限声明未逐条交代可及性条目：{'、'.join(missing[:6])}")
    return ok(
        f"可及性标注与局限交代：表内标注 {len(marked)} 行（{'、'.join(marked) or '无'}）"
        f"＋清单仅题录 {len(tertiary)} 条，局限声明逐条交代"
    )


@spec("E-07", "研读", "精确", "分步", "类型约束（叙述综述／病例／监管）", "agent/knowledge-package.md", "叙述综述不进表；安全性行只填安全结局；监管行限规范性", "—")
def e07(ctx):
    rows = evidence_rows(ctx)
    narration = [rid for rid, r in rows.items() if "叙述综述" in r["设计"]]
    if narration:
        return bad(f"叙述综述进表：{narration}")
    safety = [r for r in rows.values() if "仅安全性行" in r["设计"] or "安全信号" in r["关联"]]
    if not safety:
        return bad("无安全性行，无法验证类型约束")
    for r in safety:
        if not re.search(r"安全|不良|反应|耐受|毒性|事件", r["结局"]):
            return bad("安全性行的结局列不是安全性内容")
    reg = [r for r in rows.values() if "监管" in r["设计"]]
    if not reg:
        return bad("无监管行")
    for r in reg:
        if "规范性" not in r["设计"] and "规范性" not in r["关联"]:
            return bad("监管行未限规范性断言")
    return ok(f"叙述综述零进表；安全性行 {len(safety)} 行；监管行 {len(reg)} 行均限规范性")


@spec("E-08", "研读", "精确", "软项", "分级：先验等级＋工具名＋升降限一档", "agent/knowledge-package.md", "每行有 E 级或明确待定；升降 ≤1 档且有理由与工具", "—")
def e08(ctx):
    rows = evidence_rows(ctx)
    gaps = []
    no_tool = re.compile(r"监管|方案行|仅安全性行|待定|待查")
    for rid, r in rows.items():
        if not re.search(r"E\d|待定|待查|方案行|仅安全性行", r["设计"]):
            gaps.append(f"行 {rid} 无等级标注")
        if no_tool.search(r["设计"]):
            continue
        if not any(t.lower() in r["偏倚"].lower() for t in TOOL_NAMES) and "待" not in r["偏倚"] and "不评" not in r["偏倚"]:
            gaps.append(f"行 {rid} 无评价工具名")
    if gaps:
        return bad("；".join(gaps[:3]))
    return ok(f"{len(rows)} 行等级与工具名齐；升降理由写在偏倚备注（限一档）")


@spec("E-09", "研读", "精确", "分步", "R4 生成后自查痕迹", "agent/knowledge-package.md", "方法附录含 R4 自查记录及其证据指针", "—")
def e09(ctx):
    app = find_h2(ctx.text("agent/knowledge-package.md"), "方法附录") or ""
    if "R4" not in app or "自查" not in app:
        return bad("方法附录缺 R4 生成后自查记录")
    refs = re.findall(r"(?:run|agent)/[\w./*-]+", app)
    missing = []
    for r in sorted(set(refs)):
        if "*" in r:
            if not list(ctx.run.glob(r)):
                missing.append(r)
        elif not ctx.has(r):
            missing.append(r)
    if missing:
        return bad(f"自查记录的证据指针落空：{missing[:3]}")
    return ok("R4 自查记录在方法附录，全部 run/／agent/ 指针在场")


@spec("E-10", "研读", "精确", "分步", "近两年窗说明", "agent/knowledge-package.md＋agent/screening-log.md", "包内与筛选日志都交代近两年窗纳入排除", "—")
def e10(ctx):
    kp = ctx.text("agent/knowledge-package.md")
    log = ctx.text("agent/screening-log.md")
    if "近两年窗" not in kp:
        return bad("包内缺近两年窗说明")
    if "近两年窗" not in log:
        return bad("筛选日志缺近两年窗说明")
    return ok("包内与筛选日志均交代近两年窗（含纳入排除理由）")


@spec("E-11", "研读", "精确", "硬门禁", "单制式引文与待核对条目不进可追溯列", "agent/reference-list.md＋run/b1/refs-report.json", "全 GB/T 7714；未核对标 [待核对] 且 traceable_row 为空", "—")
def e11(ctx):
    r = ctx.json("run/b1/refs-report.json")
    if "GB/T 7714" not in r["format"]:
        return bad(f"引文制式非 GB/T 7714：{r['format']}")
    if r["excluded_from_traceable"] != r["pending"]:
        return bad("待核对条目与不进可追溯列清单不一致")
    for e in r["entries"]:
        if e["id"] in r["pending"] and e.get("traceable_row"):
            return bad(f"待核对条目挂了行号：{e['id']} → {e['traceable_row']}")
        if e["id"] not in r["pending"] and not e.get("verified"):
            return bad(f"未核对条目未标待核对：{e['id']}")
    reflist = ctx.text("agent/reference-list.md")
    for bad_style in ("Chicago", "MLA", "APA 第", "温哥华"):
        if bad_style in reflist:
            return bad(f"引文表出现第二制式：{bad_style}")
    if "[待核对]" not in reflist:
        return bad("待核对条目的标记缺 [待核对]")
    if r["formatted"] < 30 or len(r["pending"]) > 2:
        return bad(f"引文条数越出容差：已核对 {r['formatted']}（下界 30）／待核对 {len(r['pending'])}（上限 2）")
    return ok(f"格式 {r['format']}；已核对 {r['formatted']} 条（下界 30）／待核对 {len(r['pending'])} 条（不进可追溯列）")


@spec("E-12", "研读", "一致性", "硬门禁", "引文挂行与证据表逐条对齐", "run/b1/refs-report.json＋agent/reference-list.md", "挂行行号存在且文献相符；每行至少被一条引文覆盖；未挂行条目在引文表标 —", "—")
def e12(ctx):
    rows = evidence_rows(ctx)
    refs = ctx.json("run/b1/refs-report.json")
    reflist = ctx.text("agent/reference-list.md")
    covered = set()
    for e in refs["entries"]:
        expr = re.sub(r"[（(].*?[)）]", "", (e.get("traceable_row") or "").replace("行", "")).strip()
        ids = expand_row_ref(expr) if expr else set()
        for rid in ids:
            if rid not in rows:
                return bad(f"{e['id']} 挂到不存在的行 {rid}")
            row = rows[rid]
            doi = norm_doi(e.get("doi") or "")
            matched = (doi and row["doi"] == doi) or any(n and n in e["ref"] for n in row["num"])
            if not matched:
                return bad(f"{e['id']}（{doi or e['ref'][:20]}）挂行 {rid}，该行文献是 {row['doi'] or row['num']}")
            covered.add(rid)
        if not ids:
            line = next((l for l in reflist.splitlines() if l.startswith(f"| {e['id']} ")), "")
            if not line.rstrip().endswith("— |"):
                return bad(f"{e['id']} 未挂行但引文表未标 —（{line[-40:]!r}）")
    uncovered = [rid for rid in rows if rid not in covered]
    if uncovered:
        return bad(f"证据表行无引文覆盖：{uncovered}")
    return ok(f"{len(refs['entries'])} 条引文挂行逐条对齐；{len(rows)} 行全部被引文覆盖；未挂行条目在引文表标 —")


@spec("E-13", "研读", "精确", "分步", "警告类复核痕迹（人工核对留痕）", "脚本 JSON vs agent/screening-log.md／§8", "F-02／F-04／L-08／R3-01／T-01 命中时必须有复核痕迹；规则须在规则说明里在案", "—")
def e13(ctx):
    data = ctx._json.get("__script__") or {}
    rules_doc = ctx.atext("references/protocols/review-rules.md")
    watch = ("F-02", "F-04", "L-08", "R3-01", "T-01")
    missing_rules = [r for r in watch if r not in rules_doc]
    if missing_rules:
        return bad(f"规则说明缺规则：{missing_rules}")
    findings = [f for f in data["findings"] if f["rule"] in watch]
    if not findings:
        return ok(f"本轮无 {'／'.join(watch)} 警告；五条规则在规则说明在案，无待核项")
    trace = ctx.text("agent/screening-log.md") + (find_h2(ctx.text("agent/knowledge-package.md"), "局限声明") or "")
    if not re.search(r"核对|复核|人工", trace):
        return bad(f"{len(findings)} 条警告（{[f['rule'] for f in findings]}）无复核痕迹")
    return ok(f"警告 {len(findings)} 条（{[f['rule'] for f in findings]}）在筛选日志／局限声明有复核痕迹")


@spec("E-14", "研读", "精确", "硬门禁", "§6 四块齐且有序", "agent/knowledge-package.md §6", "6.1 背景／6.2 核心结论／6.3 讨论／6.4 支持结论四个小标题齐且有序", "—")
def e14(ctx):
    body = find_h2(ctx.text("agent/knowledge-package.md"), "结论") or ""
    heads = []
    for m in re.finditer(r"(?m)^#{1,6}\s+(.*)$", body):
        head = re.sub(r"[（(][^（()）]*[)）]", "", m.group(1))
        for bid, keys in (("6.1", ("6.1", "背景")), ("6.2", ("6.2", "核心结论")),
                          ("6.3", ("6.3", "讨论")), ("6.4", ("6.4", "支持结论"))):
            if bid in head and any(k in head for k in keys[1:]):
                heads.append(bid)
                break
    want = ["6.1", "6.2", "6.3", "6.4"]
    if heads != want:
        return bad(f"四块不齐或无序：实测 {heads or '无'}（须 {want}，数字＋关键词同匹配）")
    return ok("§6 四块齐且有序：6.1 背景／6.2 核心结论／6.3 讨论／6.4 支持结论")


@spec("E-15", "研读", "精确", "硬门禁", "核心结论下限与三类要素", "agent/knowledge-package.md 6.2", "6.2 正文 ≥300 字，且含趋势／证据空白／待补证据三类要素词", "—")
def e15(ctx):
    body = find_h2(ctx.text("agent/knowledge-package.md"), "结论") or ""
    m62 = re.search(r"(?ms)^###\s+6\.2\s+\S+.*?(?=^###\s+|\Z)", body)
    if not m62:
        return bad("§6 内缺 6.2 核心结论块")
    text = re.sub(r"\s+", "", m62.group(0).split("\n", 1)[1] if "\n" in m62.group(0) else "")
    if len(text) < 300:
        return bad(f"6.2 正文 {len(text)} 字（下限 300 字）")
    need = {"趋势": ("趋势", "主流"), "证据空白": ("证据空白", "不确定"), "待补证据": ("还需", "待补", "需要")}
    missing = [k for k, alts in need.items() if not any(a in m62.group(0) for a in alts)]
    if missing:
        return bad(f"6.2 缺要素：{'、'.join(missing)}")
    return ok(f"6.2 正文 {len(text)} 字，三类要素齐（趋势／证据空白／待补证据）", metric=len(text))


@spec("E-16", "研读", "精确", "硬门禁", "核心段数字可在证据层查到", "agent/knowledge-package.md＋校验脚本 JSON", "6.2 的效应量类数字全部能在 §4／§5／6.4／引文表找到；脚本 M-02 零命中", "—")
def e16(ctx):
    findings = (ctx._json.get("__script__") or {}).get("findings", [])
    m02 = [f for f in findings if f.get("rule") == "M-02"]
    if m02:
        return bad(f"M-02 核心段数字不可查：{len(m02)} 条（{m02[0]['message'][:60]}）")
    return ok("6.2 效应量类数字全部在证据层可查；脚本 M-02 零命中")


@spec("E-17", "研读", "精确", "硬门禁", "支持结论条目全带行号", "agent/knowledge-package.md 6.4", "6.4 条目 ≥5 条，每条含至少一个行 x／文献 x 指针", "—")
def e17(ctx):
    body = find_h2(ctx.text("agent/knowledge-package.md"), "结论") or ""
    m64 = re.search(r"(?ms)^###\s+§?\s*6\.4\s+\S+.*?(?=^###\s+|\Z)", body)
    if not m64:
        return bad("§6 内缺 6.4 支持结论块")
    items = [l for l in m64.group(0).splitlines() if l.strip().startswith(("-", "*", "+"))]
    if len(items) < 5:
        return bad(f"6.4 条目 {len(items)} 条（下限 5 条）")
    norow = [l.strip()[:40] for l in items if not ROW_MENTION_RE.search(l)]
    if norow:
        return bad(f"6.4 有 {len(norow)} 条无行号指针：{norow[0]}")
    return ok(f"6.4 条目 {len(items)} 条全部带行号", metric=len(items))


@spec("E-18", "研读", "精确", "分步", "背景区无数字（M-01 口径复核）", "agent/knowledge-package.md＋校验脚本 JSON", "6.1／6.3 无数字；脚本 M-01／M-03 零命中", "—")
def e18(ctx):
    findings = (ctx._json.get("__script__") or {}).get("findings", [])
    hits = [f for f in findings if f.get("rule") in ("M-01", "M-03")]
    if hits:
        kinds = sorted({f["rule"] for f in hits})
        return bad(f"{'／'.join(kinds)} 有命中：{len(hits)} 条（{hits[0]['message'][:60]}）")
    return ok("6.1／6.3 无数字、四块齐；脚本 M-01／M-03 零命中")


@spec(
    "E-19",
    "研读",
    "一致性",
    "硬门禁",
    "仅题录须可证无摘要（可及性标注 vs 摘要可得性）",
    "agent/knowledge-package.md＋agent/placement-plan.md＋run/step5-accessibility.json",
    "标仅题录者不得在可及性清单、落点报告摘要列或库内 abstractNote 上取到摘要；清单须覆盖证据表行",
    "—",
)
def e19(ctx):
    """仅摘要义务化（ADR-0023）的机器闭合：仅题录＝三级取数皆无，故凡标仅题录而摘要可得即误标。

    核心交叉核对离线可算（清单／落点报告摘要列都在包内，`--no-live` 不得降级为未执行）；
    库内 `abstractNote` 只读回查是 live 加强项，取不到照旧判不过。
    """
    rows = evidence_rows(ctx)
    key_doi = plan_key_dois(ctx)
    manifest = accessibility_entries(ctx)

    marked = {}
    if manifest is not None:
        for doi, e in manifest.items():
            if str(e.get("accessibility", "")).strip() == "仅题录":
                marked[doi] = "可及性清单"
    for rid, r in rows.items():  # 历史形态：未出表前的仅题录行（表内残留）一并核
        if "仅题录" in r["设计"] and r["doi"] and r["doi"] not in marked:
            marked[r["doi"]] = f"证据表行 {rid}"

    if manifest is not None:
        uncovered = [rid for rid, r in rows.items() if r["doi"] and r["doi"] not in manifest]
        if uncovered:
            return bad(
                f"可及性清单未覆盖证据表行：{'、'.join(f'行 {x}' for x in uncovered[:6])}"
                "（清单须逐条记研读集条目，ADR-0023）"
            )

    available = plan_abstract_dois(ctx, key_doi)
    if manifest is not None:
        for doi, e in manifest.items():
            src = str(e.get("abstract_source", "")).strip()
            if src not in ABSTRACT_ABSENT:
                available.setdefault(doi, f"可及性清单自报来源 {src}")

    if ctx.live or getattr(ctx, "zotero_fixture", None):
        results, note = zotero_recheck(ctx)
        if results is None:
            ctx.note(f"仅题录摘要可得性的库内加强项未执行：{note}")
            live_note = f"；库内加强项未执行（{note}）"
        else:
            hits = 0
            for item in results:
                if not (item.get("abstract_note") or "").strip():
                    continue
                hits += 1
                doi = key_doi.get(item["key"])
                if doi:
                    available.setdefault(doi, f"库内 abstractNote（{item['key']}）")
            live_note = f"；库内加强项已执行（{hits} 条条目 abstractNote 非空）"
    else:
        live_note = "；库内加强项跳过（--no-live；离线交叉核对已完成，不降级为未执行）"

    conflict = [(d, marked[d], available[d]) for d in marked if d in available]
    if conflict:
        show = "；".join(f"{d}（{mark} 标仅题录，但{src}）" for d, mark, src in conflict[:4])
        more = f"；另 {len(conflict) - 4} 条" if len(conflict) > 4 else ""
        return bad(f"仅题录须可证无摘要：{len(conflict)} 条标仅题录却摘要可得——{show}{more}")
    if not marked:
        return ok(f"仅题录须可证无摘要：本包无仅题录条目（不适用）{live_note}")
    return ok(
        f"仅题录须可证无摘要：{len(marked)} 条仅题录条目均无可取摘要"
        f"（{'、'.join(list(marked)[:4])}）；摘要可得读数 {len(available)} 条{live_note}"
    )


@spec("F-01", "校验脚本", "精确", "硬门禁", "脚本退出信号为零", "scripts/review_knowledge.py --format json", "退出码 0（无 error）", "—")
def f01(ctx):
    run = ctx._json.get("__script_run__") or {}
    if run.get("returncode") != 0:
        return bad(f"退出码 {run.get('returncode')}（stderr：{(run.get('stderr') or '')[:120]}）")
    return ok(f"退出码 0；命令：{run.get('cmd_hint')}")


@spec("F-02", "校验脚本", "精确", "分步", "JSON 契约五字段齐", "脚本 JSON 输出", "file + summary{error,warning,info} + findings[]{line,rule,severity,message,context,suggestion}", "—")
def f02(ctx):
    data = ctx._json.get("__script__") or {}
    if not data:
        return bad("没有取到脚本 JSON")
    if not data.get("file"):
        return bad("缺 file 字段")
    if set(data.get("summary", {})) != {"error", "warning", "info"}:
        return bad(f"summary 字段不符：{sorted(data.get('summary', {}))}")
    need = {"line", "rule", "severity", "message", "context", "suggestion"}
    for f in data["findings"]:
        if not need <= set(f):
            return bad(f"finding 缺字段：{sorted(need - set(f))}")
    return ok(f"契约齐：file={data['file']}；summary={data['summary']}；findings {len(data['findings'])} 条六字段全")


@spec("F-03", "校验脚本", "精确", "硬门禁", "零阻断错误（七条 error 规则清零）", "脚本 JSON summary", "summary.error = 0（含 R1-03／ST-01／L-03／L-09／M-01／M-02／M-03）", "—")
def f03(ctx):
    data = ctx._json.get("__script__") or {}
    if data.get("summary", {}).get("error", 1) != 0:
        return bad(f"summary.error = {data['summary'].get('error')}")
    hit = {f["rule"] for f in data["findings"]}
    blocked = {"R1-03", "ST-01", "L-03", "L-09", "M-01", "M-02", "M-03"} & hit
    if blocked:
        return bad(f"error 类规则仍有命中：{sorted(blocked)}")
    return ok("summary.error = 0；R1-03／ST-01／L-03／L-09／M-01／M-02／M-03 零命中")


@spec("F-04", "校验脚本", "精确", "硬门禁", "占位清零或已进局限", "脚本 JSON R1-01 findings vs 局限声明", "每条占位 warning 都在局限声明按行号或原文逐条对上", "—")
def f04(ctx):
    data = ctx._json.get("__script__") or {}
    lim = find_h2(ctx.text("agent/knowledge-package.md"), "局限声明") or ""
    placeholders = [f for f in data["findings"] if f["rule"] == "R1-01"]
    if not placeholders:
        return ok("R1-01 占位零命中")
    undisclosed = []
    for f in placeholders:
        context = (f.get("context") or "").strip()
        row = re.match(r"\|\s*(\d+[a-z]?)\s*\|", context)
        if row and row.group(1) in set(ROW_MENTION_RE.findall(lim)):
            continue  # 表内占位：局限声明按行号交代
        tokens = re.findall(r"\[[^\]]{2,}\]", context)
        head = context[:24]
        if (head and head in lim) or any(t in lim for t in tokens):
            continue  # 正文占位：局限声明按原文或占位标记交代
        undisclosed.append(f"{f['line']}｜{context[:30]}")
    if undisclosed:
        return bad(f"占位未在局限声明逐条交代：{undisclosed[:3]}")
    return ok(f"R1-01 占位 {len(placeholders)} 条，逐条对上局限声明（表内按行号、正文按原文／占位标记）")


@spec("F-05", "校验脚本", "精确", "分步", "参考年份固定", "脚本调用", "以固定 --cur-year 调用", "—")
def f05(ctx):
    run = ctx._json.get("__script_run__") or {}
    if str(ctx.cur_year) not in (run.get("cmd_hint") or ""):
        return bad("调用未固定 --cur-year")
    return ok(f"固定 --cur-year {ctx.cur_year}（{run.get('cmd_hint')}）")


@spec("F-06", "校验脚本", "精确", "分步", "人读行与小结为中文", "脚本 text 输出", "逐行 finding 行与中文小结齐；零 finding 时以「小结：error=0 warning=0 info=0」形态替代逐行", "—")
def f06(ctx):
    run = ctx._json.get("__text_run__") or {}
    out = run.get("stdout") or ""
    if not CJK.search(out):
        return bad("text 输出无中文")
    has_rows = bool(FINDING_ROW_RE.search(out))
    zero_summary = bool(re.search(r"小结[：:]\s*error=0\s+warning=0\s+info=0", out))
    if not (has_rows or zero_summary or "无 finding" in out):
        return bad("text 输出既无「文件:行 [规则/级别]」逐行形态，也无零 finding 小结形态")
    if "小结" not in out:
        return bad("text 输出缺小结行")
    return ok(f"text 输出为中文{'逐行 finding＋' if has_rows else '零 finding 小结形态＋'}小结；契约符合")


@spec("F-07", "校验脚本", "精确", "分步", "词表目录被校验脚本引用", "脚本调用与 T-01 输出", "传入词表真源；T-01 命中可解释", "—")
def f07(ctx):
    run = ctx._json.get("__script_run__") or {}
    if "references/lexicon/term-lexicon.yaml" not in (run.get("cmd_hint") or ""):
        return bad("调用未引用词表真源")
    data = ctx._json.get("__script__") or {}
    t01 = [f for f in data["findings"] if f["rule"] == "T-01"]
    return ok(f"词表真源已传入；T-01 命中 {len(t01)} 条（同义混用 warning／易混 info）")


@spec("F-08", "校验脚本", "精确", "分步", "一次执行两渲染", "scripts/review_knowledge.py（门禁调用位）", "同一轮 findings 出 json 与人读两处；两处消费字段在场、逐行与计数一致；执行次数实测为 1", "—")
def f08(ctx):
    """证据断言（分步，不改硬门禁口径）：门禁本轮对校验脚本的调用次数与两处渲染的一致性。

    执行次数读 `__script_run__["executions"]`——由 `run_script_json` 包装入口**实测**自增（非自报常量）；
    两渲染对账用与 F-06 同一份行格式正则。若实现退回两次子进程或两渲染分叉，本行判未过（表内可见）。
    """
    run = ctx._json.get("__script_run__") or {}
    if run.get("executions") != 1:
        return bad(f"校验入口本轮被调用 {run.get('executions')} 次（要求一次执行、两种渲染）")
    data = ctx._json.get("__script__") or {}
    text = (ctx._json.get("__text_run__") or {}).get("stdout") or ""
    if not data.get("file") or not text:
        return bad("两处消费字段不全：机器可读／人读其一缺席")
    counts = data.get("summary") or {}
    m = re.search(r"小结：error=(\d+) warning=(\d+) info=(\d+)", text)
    if not m:
        return bad("人读渲染缺小结行")
    shown = {"error": int(m.group(1)), "warning": int(m.group(2)), "info": int(m.group(3))}
    if shown != {sev: counts.get(sev) for sev in ("error", "warning", "info")}:
        return bad(f"两渲染计数不一致：人读 {shown} ｜ json {counts}")
    rows = [ln for ln in text.splitlines() if FINDING_ROW_FULL_RE.match(ln)]
    findings = data.get("findings") or []
    if len(rows) != len(findings):
        return bad(f"两渲染逐行不一致：人读 {len(rows)} 行 ｜ json {len(findings)} 条")
    return ok(f"一次执行两渲染：findings {len(findings)} 条计数与逐行一致（{shown}）")


# ---------------------------------------------------------------- 断言：交互（06 契约）


@spec("G-01", "交互", "精确", "分步", "协议问法两组＋灰色默认节＋本次合集齐且中文可读", "references/protocols/hitl-protocol.md", "两处断点＋两组补问问法齐、无已废交互点一、灰色默认节与本次合集（交互点三）在案", "—")
def g01(ctx):
    proto = ctx.atext("references/protocols/hitl-protocol.md")
    heads = re.findall(r"^#{2,3}\s+(.*)$", proto, flags=re.M)
    for k in ("交互点二", "交互点三"):
        if not any(k in h for h in heads):
            return bad(f"交互协议缺问法：{k}")
    if any("交互点一" in h for h in heads):
        return bad("协议仍含已废的交互点一标题")
    if not any("灰色默认" in h for h in heads):
        return bad("协议缺灰色默认节")
    if "本次合集" not in proto or "med-lit-review/<交付目录名>" not in proto:
        return bad("协议缺本次合集问法（交互点三须含集名形态）")
    if "AFK 全自动" not in proto:
        return bad("协议缺本次合集 AFK 全自动回退")
    if "推荐" not in proto:
        return bad("协议缺推荐位标记")
    if not CJK.search(proto):
        return bad("协议非中文")
    return ok(f"两处断点问法＋灰色默认节＋本次合集在案（标题 {len(heads)} 个）；推荐位与中文可读性在案")


@spec("G-02", "交互", "精确", "分步", "灰色桶默认不打断、灰色口径在案", "走查矩阵与运行日志", "W1 系列场景齐且灰色桶 0 次打断、灰色默认启用记录在案；历史包文件头口径注记（pre-0006）同样计数", "—")
def g02(ctx):
    wt = ctx._json.get("__walkthrough__") or {}
    if wt.get("scenarios"):
        sc = {s.get("id"): s for s in wt["scenarios"]}
        for sid in ("W1-01", "W1-02", "W1-03"):
            if sid not in sc:
                return bad(f"走查矩阵缺 {sid}")
        blob = json.dumps([sc["W1-01"], sc["W1-02"], sc["W1-03"]], ensure_ascii=False)
        if not re.search(r"0 次|未打断|不打断", blob):
            return bad("灰色桶场景未记 0 次打断")
        if "默认启用" not in blob:
            return bad("走查矩阵未记灰色默认启用")
        return ok("W1-01～W1-03：灰色桶 0 次打断、灰色默认启用记录在案")
    log = ctx.text("agent/screening-log.md")
    b = ctx.text("agent/bucketing-and-sources.md")
    if not (("默认启用" in b and "灰色" in b) or "未打断" in b or "空桶不问" in b) and not ctx.historical_caliber:
        return bad("下载报告未记灰色渠道口径")
    if "断点" not in log:
        return bad("筛选日志未记断点")
    caliber = "（历史口径注记在案：pre-0006，按当时口径读）" if ctx.historical_caliber else ""
    return ok(f"按运行日志核对：灰色口径在案、断点在案（无走查矩阵时的退化核对）{caliber}")


@spec("G-03", "交互", "精确", "硬门禁", "写库顺序：计划表→确认→写→回查（含本次合集）", "agent/placement-plan.md", "顺序与确认门在案，有人值守未确认不写（AFK 全自动除外）", "—")
def g03(ctx):
    p = re.sub(r"\s+", "", ctx.text("agent/placement-plan.md"))
    if not re.search(r"dry-run.{0,120}?(一次全定|一次确认|确认).{0,120}?写库.{0,120}?回查", p):
        return bad("落点报告未出现「先 dry-run 计划 → 一次确认 → 写库 → 回查」的顺序叙述")
    if not re.search(r"确认门|一次全定|确认已发生|AFK自动建", p):
        return bad("落点报告未记录写库确认门")
    if "单篇不例外" not in p and "未确认不写" not in p and "AFK自动建" not in p:
        return bad("未声明未确认不写库／单篇不例外／AFK 自动建")
    return ok("写库顺序在案：dry-run 计划 → 一次确认 → 写库 → 逐条回查（含本次合集，单篇不例外；AFK 全自动除外）")


@spec("G-04", "交互", "精确", "分步", "AFK 回退记断点不记失败（含本次合集）", "agent/screening-log.md／agent/placement-plan.md／agent/bucketing-and-sources.md", "两处断点以断点／局限记录，未记失败；现行口径须记本次合集建挂口径", "旧口径包无本次合集注记按当时口径读记通过")
def g04(ctx):
    joined = ctx.text("agent/screening-log.md") + ctx.text("agent/placement-plan.md") + ctx.text("agent/bucketing-and-sources.md")
    hits = [k for k in ("待机构后重试", "留未分类", "断点") if k in joined]
    if len(hits) < 2:
        return bad(f"AFK 回退记录不足：{hits}")
    if "跳过不记失败" not in joined and "不记失败" not in joined:
        return bad("未声明跳过／回退不记失败")
    if "本次合集" not in joined:
        if ctx.historical_topic:
            return ok(f"AFK 回退记录在案：{'、'.join(hits)}；明示不记失败（历史口径注记在案：absent，按当时口径读）")
        return bad("未记录本次合集建挂口径（建／复用／AFK 自动建／不建集）")
    return ok(f"AFK 回退记录在案：{'、'.join(hits)}＋本次合集；明示不记失败")


@spec("G-05", "交互", "精确", "分步", "追问与补问至多一次", "references/protocols/hitl-protocol.md＋walkthrough", "协议写明至多一次，走查覆盖", "—")
def g05(ctx):
    proto = ctx.atext("references/protocols/hitl-protocol.md")
    if "最多一次" not in proto and "至多一次" not in proto and "一次" not in proto:
        return bad("协议未写补问／追问上限")
    wt = ctx._json.get("__walkthrough__") or {}
    okc = wt.get("exit_code")
    if wt and okc not in (0, None):
        return bad(f"走查校验退出码 {okc}")
    return ok(f"补问／追问上限在案；走查校验{'已跑（退出码 0）' if wt else '未在场（记未执行）'}")


@spec("G-06", "交互", "一致性", "硬门禁", "机密四样与本地路径零命中（走查＋运行日志＋人读主件）", "全 run 产物与 `agent/`／根件扫描（HTML 先归一化）", "密钥／票据／代理凭证／机构信息与绝对路径零命中", "—")
def g06(ctx):
    hits = secret_scan(ctx)
    if hits:
        return bad(f"机密或本地路径命中 {len(hits)} 处：{hits[0]}")
    return ok("密钥／票据／代理凭证／机构信息与绝对路径零命中")


def secret_scan(ctx):
    hits = []
    files = [p for p in ctx.run.rglob("*") if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES]
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if path.suffix.lower() == ".html":
            # W-13：样式／脚本块与内联数据 URI、CSS `url(...)` 先剥离（内联样式里的冒号串与 data: 载荷易假红）
            text = CSS_URL_RE.sub("", DATA_URI_RE.sub("", HTML_STRIP_RE.sub("", text)))
        for pat in SECRET_PATTERNS:
            m = pat.search(text)
            if m:
                hits.append(f"{path.relative_to(ctx.run)}｜{pat.pattern[:24]}｜{m.group(0)[:40]}")
                break
    return hits


# ---------------------------------------------------------------- 断言：锚点（P）


@spec("P-01", "锚点", "容差", "分步", "锚点数量在 5–8 之间", "anchors.json", "5 ≤ 锚点数 ≤ 8", "范围 5–8")
def p01(ctx):
    n = len(ctx.anchors["anchors"])
    if not 5 <= n <= 8:
        return bad(f"锚点数 {n} 超出 5–8")
    return ok(f"锚点 {n} 条（范围 5–8）", metric=n)


@spec("P-02", "锚点", "包含", "硬门禁", "锚点标识在证据表与引文表命中", "agent/knowledge-package.md＋agent/reference-list.md", "每条锚点的标识两处均命中", "包含断言：命中即过，不判位置")
def p02(ctx):
    rows = evidence_rows(ctx)
    kp = ctx.text("agent/knowledge-package.md")
    reflist = ctx.text("agent/reference-list.md")
    miss = []
    for a in ctx.anchors["anchors"]:
        ident = a["identifier"]
        if a["kind"] == "regulatory":
            if ident not in kp or ident not in reflist:
                miss.append(f"{a['id']} 监管标识未两处命中（{ident}）")
            continue
        key = norm_doi(ident)
        in_table = any(r["doi"] == key for r in rows.values())
        in_reflist = key in reflist.lower()
        if not in_table:
            miss.append(f"{a['id']} 不在证据表（{ident}）")
        if not in_reflist:
            miss.append(f"{a['id']} 不在引文表（{ident}）")
    if miss:
        return bad("；".join(miss))
    return ok(f"{len(ctx.anchors['anchors'])} 条锚点在证据表与引文表全部命中")


@spec("P-03", "锚点", "精确", "分步", "锚点锁定记录完整", "anchor-lock.json", "逐条记 locked_at／method／status／证据／pins 指纹／核对时刻；复用块含窗口与载体", "—")
def p03(ctx):
    lock = ctx._json.get("__lock__") or {}
    if not lock.get("locked_at"):
        return bad("锁定记录缺 locked_at")
    for a in lock.get("anchors", []):
        for key in ("id", "method", "status", "detail", "pins_key", "checked_at"):
            if not a.get(key):
                return bad(f"锚点锁定记录缺 {key}：{a.get('id')}")
    if not isinstance(lock.get("reuse"), dict) or not lock["reuse"].get("window_hours"):
        return bad("锁定记录缺复用块（window_hours 与载体）")
    if len(lock.get("anchors", [])) != len(ctx.anchors["anchors"]):
        return bad("锁定记录条数与锚点清单不一致")
    counts = {}
    for a in lock["anchors"]:
        counts[a["status"]] = counts.get(a["status"], 0) + 1
    reused = [a["id"] for a in lock["anchors"] if a.get("reused")]
    return ok(f"锁定 {len(lock['anchors'])} 条，状态分布 {counts}；复用 {len(reused)} 条（窗口 {lock['reuse']['window_hours']}h）")


@spec("P-04", "锚点", "精确", "硬门禁", "版本锚点声明与钉子一致", "agent/knowledge-package.md vs anchors.json", "run 侧版本号命中 version_pin", "—")
def p04(ctx):
    kp = ctx.text("agent/knowledge-package.md")
    b = ctx.text("agent/b1-and-regulatory-supplement.md")
    joined = kp + b
    for a in ctx.anchors["anchors"]:
        pin = a.get("version_pin")
        if not pin:
            continue
        if pin not in joined:
            return bad(f"{a['id']} 声明版本 {pin} 未在包或监管 log 中出现")
    return ok("；".join(f"{a['id']} {a['version_pin']}" for a in ctx.anchors["anchors"] if a.get("version_pin")))


@spec("P-05", "锚点", "一致性", "软项", "锁定现行性核对（联网）", "anchor-lock.json", "crossref／openfda／文档指纹／题录四法逐条核对（窗口内复用原 valid 结论，留痕记锁龄与原锁定时刻）；漂移记日志", "联网；未取到记未执行，不判失败")
def p05(ctx):
    lock = ctx._json.get("__lock__") or {}
    anchors_ = lock.get("anchors", [])
    states = {}
    for a in anchors_:
        states[a["status"]] = states.get(a["status"], 0) + 1
    reused = [a["id"] for a in anchors_ if a.get("reused")]
    tag = ""
    if reused:
        tag = f"；复用 {len(reused)} 条（锁龄 ≤{lock.get('reuse', {}).get('window_hours')}h，原锁定 {lock.get('reuse', {}).get('source_locked_at')}）"
    if states.get("missing"):
        return bad(f"锚点注册记录缺失：{[a['id'] for a in anchors_ if a['status'] == 'missing']}{tag}")
    if states.get("unverified"):
        ids = [a["id"] for a in anchors_ if a["status"] == "unverified"]
        if not ctx.live:
            return unverified(f"未执行（--no-live）：锚点现行性未核对 {ids}{tag}")
        return bad(f"锚点现行性未取到（传输失败，独立核对缺位）：{ids}——重跑或改 --no-live 显式放弃{tag}")
    if states.get("drifted"):
        return ok(f"现行性漂移（记漂移日志，不判假失败）：{[a['id'] for a in anchors_ if a['status'] == 'drifted']}{tag}", drift=True)
    return ok(f"逐条核对通过：{states}{tag}")


@spec("P-06", "锚点", "精确", "分步", "监管锚点有版本记录与原文留档", "agent/b1-and-regulatory-supplement.md＋run/b2", "版本号与文号齐；EMA 原文 PDF 在场", "—")
def p06(ctx):
    b = ctx.text("agent/b1-and-regulatory-supplement.md")
    if "原文留档" not in b and "留档" not in b:
        return bad("监管 log 未记原文留档")
    if not ctx.has("run/b2/ema-repatha-spc.pdf"):
        return bad("缺 EMA 产品特征概要原文留档")
    return ok("监管两条版本与文号在案；EMA 原文 PDF 已留档")


@spec("P-07", "锚点", "精确", "分步", "仅摘要／近两年锚点标注", "agent/knowledge-package.md＋agent/screening-log.md", "无开放全文的锚点在设计列标仅摘要／仅题录；近两年锚点标近两年窗", "—")
def p07(ctx):
    rows = evidence_rows(ctx)
    log = ctx.text("agent/screening-log.md")
    lim = find_h2(ctx.text("agent/knowledge-package.md"), "局限声明") or ""
    manifest = accessibility_entries(ctx)
    for a in ctx.anchors["anchors"]:
        if a["kind"] != "abstract":
            continue
        mark = a.get("expect_level_mark", "")
        rid = a["expect_rows"][0]
        row = rows.get(rid)
        doi = norm_doi(a["identifier"])
        entry = (manifest or {}).get(doi)
        if entry is not None and str(entry.get("accessibility", "")).strip() == mark:
            continue  # 现行形态：可及性清单逐条记载（仅题录已不占表行，ADR-0023）
        if row is None:
            if mark == "仅题录" and doi and doi in lim:
                continue  # 仅题录出表后按 DOI 交代：局限声明在案即算在
            return bad(f"{a['id']} 的期望行 {rid} 不存在（可及性清单与局限声明亦未见 {mark} 交代）")
        if not re.search(r"仅摘要|仅题录", row["设计"]):
            return bad(f"{a['id']} 行未在设计列标可及性状态（期望标注：{mark}）")
    recent = next((a for a in ctx.anchors["anchors"] if a.get("recent")), None)
    if recent is None:
        return bad("锚点清单未标近两年窗锚点（recent）")
    if "VESALIUS" not in log and recent["identifier"].lower() not in log.lower():
        return bad("筛选日志未记近两年锚点的纳入理由")
    if "近两年窗纳入" not in rows[recent["expect_rows"][0]]["设计"]:
        return bad("VESALIUS-CV 行未标近两年窗纳入")
    return ok("摘要类锚点在「设计与等级」列标可及性状态；VESALIUS-CV 行与筛选日志标近两年窗")


# ---------------------------------------------------------------- 结构不变式


@spec("K-01", "结构", "精确", "硬门禁", "references 按职能三分且根下无零散件", "references/ 目录树 vs SKILL.md 指针索引", "references/ 下只有 templates／protocols／lexicon 三个子目录且三者俱在；SKILL.md 点到的桶名都真实存在", "裸文件名指针会静默失效，故此条只守「件必须落进桶」，不要求每件都有入站指针")
def k01(ctx):
    refs = ctx.skill / "references"
    if not refs.is_dir():
        return bad("references/ 目录缺失")
    want = ("templates", "protocols", "lexicon")
    dirs = sorted(d.name for d in refs.iterdir() if d.is_dir())
    loose = sorted(d.name for d in refs.iterdir() if d.is_file())
    if loose:
        return bad(f"references/ 根下有零散件（应全部落入三桶）：{loose}")
    missing = [d for d in want if d not in dirs]
    if missing:
        return bad(f"references/ 缺约定桶：{missing}")
    extra = [d for d in dirs if d not in want]
    if extra:
        return bad(f"references/ 出现约定外的桶：{extra}")
    skill_md = ctx.skill / "SKILL.md"
    if not skill_md.exists():
        return bad("SKILL.md 缺失，无法核对指针索引")
    named = set(re.findall(r"references/([A-Za-z0-9_-]+)/", skill_md.read_text(encoding="utf-8")))
    ghost = sorted(named - set(want))
    if ghost:
        return bad(f"SKILL.md 指针索引点到了不存在的桶：{ghost}")
    counts = {d: sum(1 for x in (refs / d).iterdir() if x.is_file()) for d in want}
    return ok(f"references/ 三桶齐、根下无零散件、索引桶名可达；桶内件数 {counts}")


# ---------------------------------------------------------------- 硬门禁与软项


@spec("H-01", "硬门禁", "汇总", "硬门禁", "可追溯 100%", "E-03／E-04／E-11／E-12／E-14–E-18／F-03", "断言点到行、行↔卡一致、引文挂行对齐、§6 四块与数字可查、脚本无 error", "—")
def h01(ctx):
    return aggregate(ctx, ["E-03", "E-04", "E-11", "E-12", "E-14", "E-15", "E-16", "E-17", "E-18", "F-03"])


@spec("H-02", "硬门禁", "汇总", "硬门禁", "零阻断错误", "F-01／F-03", "脚本退出码 0 且 summary.error = 0", "—")
def h02(ctx):
    return aggregate(ctx, ["F-01", "F-03"])


@spec("H-03", "硬门禁", "汇总", "硬门禁", "占位清零或已披露", "F-04", "R1-01 占位逐条对得上局限声明", "—")
def h03(ctx):
    return aggregate(ctx, ["F-04"])


@spec("H-04", "硬门禁", "汇总", "硬门禁", "落点与附件回查一致（含本次合集）", "D-03／D-07／D-08／D-09／D-10", "计划＝写入＝回查；在库只读回查一致；附件列与库内 children 一致；无 PDF 与本次合集未挂条目进局限声明", "—")
def h04(ctx):
    return aggregate(ctx, ["D-03", "D-07", "D-08", "D-09", "D-10"])


@spec("H-05", "硬门禁", "汇总", "硬门禁", "机密零泄漏", "G-06", "四处产物零命中密钥／票据／凭证／机构信息与绝对路径", "—")
def h05(ctx):
    return aggregate(ctx, ["G-06"])


@spec("H-06", "硬门禁", "汇总", "硬门禁", "实际来源如实", "C-01／C-02／C-03／C-06", "报告来源与收据一致、灰色如实、开放锚点命中", "—")
def h06(ctx):
    return aggregate(ctx, ["C-01", "C-02", "C-03", "C-06"])

@spec("H-07", "硬门禁", "汇总", "硬门禁", "术语生长循环闭合", "A-08／A-09", "声明的未命中／候选全部落真源或候选队列且已落术语无「无边未声明」；词表版本在检索策略与筛选日志双引用且与真源一致", "—")
def h07(ctx):
    return aggregate(ctx, ["A-08", "A-09"])


@spec("S-01", "软项", "汇总", "软项", "可复现包齐", "八件套在场", "检索方案／检索式／来源诊断／筛选日志／队列／落点报告／版本日期戳", "缺口须进局限声明")
def s01(ctx):
    parts = {
        "检索式": ctx.has("agent/search-strategy.md"),
        "来源诊断": "diagnostics" in ctx.text("agent/search-strategy.md"),
        "筛选日志": ctx.has("agent/screening-log.md"),
        "队列文件": ctx.has("agent/download-queue.txt"),
        "落点报告": ctx.has("agent/placement-plan.md"),
        "被引日期戳": bool(ctx.json("run/b1/cited-counts.json")["snapshot"].get("query_date")),
        "权重版本戳": SORT_WEIGHT_VERSION in ctx.json("run/b1/sort-report.json")["weight_version"],
        "收据": ctx.has("run/download-results.json") and ctx.has("run/download-results-03.json"),
    }
    missing = [k for k, v in parts.items() if not v]
    if missing:
        return bad(f"可复现包缺件：{'、'.join(missing)}")
    return ok("可复现包八件套齐")


@spec("S-02", "软项", "汇总", "软项", "五类路由覆盖", "agent/knowledge-package.md＋agent/screening-log.md", "指南／关键试验／系统综合／中文／监管五类各 ≥1 行，近两年窗必查有说明", "缺口须进局限声明")
def s02(ctx):
    rows = evidence_rows(ctx)
    routes = {
        "指南": any("指南" in r["设计"] for r in rows.values()),
        "关键试验": any("III 期" in r["设计"] or "pivotal" in r["设计"].lower() for r in rows.values()),
        "系统综合": any("荟萃" in r["设计"] or "系统综述" in r["设计"] for r in rows.values()),
        "中文条目": any("中华" in r["文献"] or "中国" in r["文献"] or "药物不良反应" in r["文献"] for r in rows.values()),
        "监管文件": any("监管" in r["设计"] for r in rows.values()),
        "近两年窗说明": "近两年窗" in ctx.text("agent/knowledge-package.md"),
    }
    missing = [k for k, v in routes.items() if not v]
    if missing:
        return bad(f"覆盖缺口：{'、'.join(missing)}")
    return ok("五类路由各有行；近两年窗说明在包内")


@spec("S-03", "软项", "汇总", "软项", "分级准确（先验＋升降限一档＋工具名）", "agent/knowledge-package.md", "等级／工具／升降理由齐", "缺口须进局限声明")
def s03(ctx):
    r = ctx._results.get("E-08")
    if r and not r["ok"]:
        return bad(r["observed"])
    return ok("每行等级与工具名齐，升降理由写在偏倚备注")


@spec("S-04", "软项", "汇总", "软项", "时效性（现行版豁免与旧证据降档）", "agent/knowledge-package.md＋agent/screening-log.md", "现行版豁免在案；被后续 pivotal 超越的旧荟萃降档", "缺口须进局限声明")
def s04(ctx):
    kp = ctx.text("agent/knowledge-package.md")
    if "现行版豁免" not in kp:
        return bad("包内缺现行版豁免说明")
    rows = evidence_rows(ctx)
    downgraded = [rid for rid, r in rows.items() if "降一档" in r["设计"] or "降档" in r["偏倚"]]
    if not downgraded:
        return bad("没有降档行，时效性无法验证")
    return ok(f"现行版豁免在案；降档行 {'、'.join(downgraded)}")


def aggregate(ctx, ids):
    bads = [f"{i}：{ctx._results[i]['observed']}" for i in ids if not ctx._results.get(i, {}).get("ok")]
    if bads:
        return bad("；".join(bads))
    return ok("／".join(ids) + " 全部通过")


# ---------------------------------------------------------------- 执行与报告


def load_review_module(ctx):
    """按 `--skill` 定位校验脚本并导入：同进程调用＝一次执行两渲染（O-10；脚本命令行契约不变）。"""
    return load_skill_module(ctx.skill, "review_knowledge")


def run_script_json(ctx):
    """校验脚本一次执行、两种渲染：`__script__`（json）与 `__text_run__`（人读）同源同一轮 findings。

    原实现对同一包调两次子进程（json＋text）；现走脚本自己的单执行入口（`review_package`）一次跑、
    两种渲染——两处消费字段照旧逐项在场，退出码语义不变（文件错误＝2、脚本缺陷＝1、超时＝1，均记非零
    信号）。执行次数是**实测计数**（包装入口逐次自增，F-08 对账），不是自报常量；脚本仍在有界线
    （`REVIEW_TIMEOUT_SECONDS`，沿原 `subprocess.run(timeout=300)`）内跑完，超时用守护线程弃等、
    不阻塞整轮门禁。
    """
    pkg = ctx.run / "agent/knowledge-package.md"
    lexicon = ctx.skill / "references/lexicon/term-lexicon.yaml"
    cmd_hint = ("review_knowledge.py agent/knowledge-package.md --format json --cur-year "
                f"{ctx.cur_year} --lexicon references/lexicon/term-lexicon.yaml")
    stderr = io.StringIO()
    data, text_out, returncode = {}, "", 0
    calls = 0
    try:
        review = load_review_module(ctx)

        def counted(path, cur_year, rules, lex):
            """包装一次校验入口：实测执行次数（F-08 的判据来自这里，不来自常量）。"""
            nonlocal calls
            calls += 1
            return review.review_package(path, cur_year, rules, lex)

        box: dict = {}

        def run_review():
            try:
                box["result"] = counted(str(pkg), ctx.cur_year, review.ALL_RULES, str(lexicon))
            except BaseException as exc:  # 交回主线程按 CLI 口径分类
                box["error"] = exc

        with contextlib.redirect_stderr(stderr):
            worker = threading.Thread(target=run_review, daemon=True)  # 超时后不再拖住解释器退出
            worker.start()
            worker.join(REVIEW_TIMEOUT_SECONDS)
        if worker.is_alive():
            # 不用内建 TimeoutError：它是 OSError 子类，会落进上面的「文件错误＝2」分支
            raise RuntimeError(f"校验脚本超过 {REVIEW_TIMEOUT_SECONDS}s 未返回，按超时记非零信号")
        if "error" in box:
            raise box["error"]
        result = box["result"]
        data = review.json_payload(result)
        text_out = review.render_text(result["name"], result["findings"], result["lexicon_version"]) + "\n"
        returncode = review.exit_code(result)
    except (OSError, UnicodeDecodeError) as exc:  # 照 CLI 口径：文件错误＝退出码 2
        returncode = 2
        print(f"文件错误：{pkg}（{exc}）", file=stderr)
    except Exception as exc:  # 脚本自身缺陷或超时：记非零信号，不中断整轮门禁其余断言
        returncode = 1
        print(f"执行错误：{type(exc).__name__}: {exc}", file=stderr)
    for line in stderr.getvalue().splitlines():  # 词表解析缓存的命中读数进 payload 与报告（O-11 留痕）
        if line.startswith("CACHE lexicon "):
            ctx.note(f"词表解析缓存：{line.strip()}")
    ctx._json["__script__"] = data
    ctx._json["__script_run__"] = {
        "returncode": returncode,
        "stderr": stderr.getvalue()[-400:],
        "cmd_hint": cmd_hint,
        "executions": calls,  # 实测：本轮校验入口被调用几次（一次执行两渲染＝1）
    }
    ctx._json["__text_run__"] = {"returncode": returncode, "stdout": text_out}


def run_walkthrough(ctx, walkthrough=None):
    if not walkthrough:
        ctx.note("走查校验未执行：未提供 --walkthrough（隔离盲测环境按约定剪裁，或本轮不跑走查矩阵）")
        return
    path = Path(walkthrough).resolve()
    if not path.exists():
        ctx.note(f"走查校验未执行：--walkthrough 指向的脚本不在场（{path.name}）")
        return
    proc = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, encoding="utf-8", timeout=300)
    scenarios = json.loads((path.parent / "scenarios.json").read_text(encoding="utf-8"))
    ctx._json["__walkthrough__"] = {
        "exit_code": proc.returncode,
        "stdout_tail": proc.stdout.strip().splitlines()[:2],
        "scenarios": scenarios if isinstance(scenarios, list) else scenarios.get("scenarios", []),
    }
    if proc.returncode != 0:
        ctx.note(f"走查校验失败：退出码 {proc.returncode}")


def run_lock(ctx):
    """锚点锁定：窗口内复用原 valid 结论（pins 全等＋锁龄 ≤ 窗口），其余逐条（重）核对。

    复用载体＝结果目录里的 anchor-lock.json（上一轮所写）；复用留痕记「复用（锁龄 Xh）＋原锁定时刻」，
    漂移／缺失／未取到永不复用为通过；超窗、pins 任一项不等或清单版本变即整轮重核。
    """
    now = time.time()
    locked_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    prior = read_prior_lock(ctx)
    prior_entries = None
    if prior is not None:
        stale = []
        if prior.get("pins") != ctx.anchors.get("pins"):
            stale.append("pins 不等")
        if prior.get("anchors_version") != ctx.anchors.get("version"):
            stale.append(f"锚点清单版本不等（{prior.get('anchors_version')}）")
        if stale:
            ctx.note(f"锚点锁不复用（{'、'.join(stale)}）：本轮逐条重核")
        else:
            prior_entries = {a.get("id"): a for a in prior["anchors"] if isinstance(a, dict)}
    entries, reused_ids, rechecked_ids = [], [], []
    for anchor in ctx.anchors["anchors"]:
        key = lock_pins_key(ctx, anchor)
        entry = {
            "id": anchor["id"],
            "label": anchor["label"],
            "identifier": anchor["identifier"],
            "kind": anchor["kind"],
            "method": anchor["lock"]["method"],
            "version_pin": anchor.get("version_pin", ""),
            "pins_key": key,
        }
        replayed = reuse_verdict((prior_entries or {}).get(anchor["id"], {}), key, now) if prior_entries else None
        if replayed is not None:
            entry.update(replayed)
            reused_ids.append(anchor["id"])
        else:
            res = lock_anchor(ctx, anchor)
            entry.update({"status": res["status"], "detail": res["detail"], "checked_at": locked_at, "reused": False})
            rechecked_ids.append(anchor["id"])
        entries.append(entry)
    lock = {
        "locked_at": locked_at,
        "anchors_version": ctx.anchors["version"],
        "pins": ctx.anchors.get("pins"),
        "reuse": {
            "window_hours": LOCK_WINDOW_HOURS,
            "carrier": "anchor-lock.json" if prior is not None else "",
            # 原锁定时刻＝被承接那批条目的核对时刻（连续复用时取最早者），不是上一轮运行时刻；
            # 零复用轮（超窗、pins 不等、旧形态载体）记空串——不回落上一轮运行时刻，免得报出无依据的复用基准
            "source_locked_at": min(
                (e["checked_at"] for e in entries if e.get("reused")), default=""
            ),
            "reused": reused_ids,
            "rechecked": rechecked_ids,
        },
        "anchors": entries,
    }
    ctx._json["__lock__"] = lock
    failed = [e["id"] for e in entries if e["status"] == "unverified"]
    if failed and ctx.live:
        ctx.note(
            f"锚点现行性未取到（传输／解析失败）：{'、'.join(failed)}——联网模式 fail-closed（退出码 1）；"
            "要显式放弃独立核对请用 --no-live"
        )
    if reused_ids:
        ctx.note(
            f"{LOCK_REUSE_NOTE}：{len(reused_ids)} 条（{'、'.join(reused_ids)}）锁龄 ≤{LOCK_WINDOW_HOURS}h，"
            f"原锁定 {lock['reuse']['source_locked_at']}；本轮重核 {len(rechecked_ids)} 条"
        )
    return lock


def table_markdown(results):
    lines = [
        "| 断言 | 组 | 断言类别 | 门 | 断言 | 断言对象 | 期望 | 容差 | 实测 | 判定 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in SPECS:
        r = results[s["id"]]
        verdict = "通过" if r["ok"] else "未通过"
        if r.get("error"):
            verdict = "执行错误"
        observed = (r["observed"] or "").replace("|", "／")[:220]
        lines.append(
            f"| {s['id']} | {s['group']} | {s['kind']} | {s['gate']} | {s['title']} | {s['target']} | "
            f"{s['expect']} | {s['tol']} | {observed} | {verdict} |"
        )
    return "\n".join(lines) + "\n"


def drift_rows(ctx, results):
    """与基线对比：容差类断言的实测值变化记漂移日志（不改变判定）。"""
    if not ctx.baseline:
        return []
    base = {r["id"]: r.get("metric") for r in ctx.baseline.get("results", [])}
    rows = []
    for s in SPECS:
        if s["kind"] not in ("容差", "顺序"):
            continue
        now = results[s["id"]].get("metric")
        was = base.get(s["id"])
        if now is None or was is None or now == was:
            continue
        rows.append({"id": s["id"], "title": s["title"], "baseline": was, "current": now})
    return rows


def run_label(ctx):
    """报告里的跑目录用仓库相对路径：产物不写本机绝对路径，门禁扫自身输出时也不误伤。"""
    try:
        rel = str(ctx.run.relative_to(ctx.repo)).replace("\\", "/")
    except ValueError:
        rel = ""
    return rel or ctx.run.name


def write_report(ctx, results, drifts, exit_code):
    hard_ok = [s for s in SPECS if s["gate"] == "硬门禁" and results[s["id"]]["ok"]]
    hard_all = [s for s in SPECS if s["gate"] == "硬门禁"]
    soft_fail = [s for s in SPECS if s["gate"] == "软项" and not results[s["id"]]["ok"]]
    errors = [s for s in SPECS if results[s["id"]].get("error")]
    lines = [
        "# 固定选题验收门禁报告（run_gate）",
        "",
        f"- 验收跑目录：`{run_label(ctx)}`",
        f"- 断言总数：{len(SPECS)}（硬门禁 {len(hard_all)} 条，其中汇总门 {sum(1 for s in hard_all if s['kind'] == '汇总')} 条）",
        f"- 判定：硬门禁 {len(hard_ok)}/{len(hard_all)} 通过；软项未通过 {len(soft_fail)} 条；执行错误 {len(errors)} 条",
        f"- 门禁退出码：{exit_code}（0＝硬门禁全过且软项缺口全披露；1＝未过；2＝用法或输入缺失）",
        f"- 口径版本：{ctx.caliber}（{CALIBER_MEANING[ctx.caliber]}）",
        f"- 锁定时刻：{(ctx._json.get('__lock__') or {}).get('locked_at', '未执行')}；联网核对：{'启用' if ctx.live else '关闭（--no-live）'}",
        "",
        "## 硬门禁",
        "",
        "| 门 | 断言 | 判定 | 实测 |",
        "|---|---|---|---|",
    ]
    for s in hard_all:
        r = results[s["id"]]
        lines.append(f"| {s['id']} {s['title']} | {s['expect']} | {'通过' if r['ok'] else '未通过'} | {r['observed'][:180]} |")
    lines += ["", "## 软项（缺口须进局限声明）", "", "| 软项 | 判定 | 实测 |", "|---|---|---|"]
    for s in [x for x in SPECS if x["gate"] == "软项"]:
        r = results[s["id"]]
        lines.append(f"| {s['id']} {s['title']} | {'通过' if r['ok'] else '未通过'} | {r['observed'][:180]} |")
    lines += ["", "## 锚点锁定", "", "| 锚点 | 路由 | 标识 | 锁定方式 | 状态 | 证据 |", "|---|---|---|---|---|---|"]
    lock = ctx._json.get("__lock__") or {}
    for a in lock.get("anchors", []):
        lines.append(
            f"| {a['id']} {a['label']} | {a['kind']} | {a['identifier']} | {a['method']} | {a['status']} | {a['detail'][:160]} |"
        )
    reuse = lock.get("reuse") or {}
    if reuse.get("reused"):
        lines += [
            "",
            f"- 复用：{len(reuse['reused'])} 条（{'／'.join(reuse['reused'])}）锁龄 ≤{reuse.get('window_hours')}h，"
            f"原锁定 {reuse.get('source_locked_at')}（载体 `{reuse.get('carrier')}`）；本轮重核 {len(reuse.get('rechecked', []))} 条",
            "- 本轮复用只承接原 `valid` 条目：漂移／缺失／未取到者已在本轮重核，未复用为通过。",
        ]
    lines += ["", "## 漂移日志（容差内变化，不判失败）", ""]
    if ctx.baseline is None:
        lines.append("- 未传 `--baseline`，本轮只记录实测值，不做漂移对比。")
    elif not drifts:
        lines.append("- 与基线相比无容差类数值变化。")
    else:
        for d in drifts:
            lines.append(f"- {d['id']} {d['title']}：基线 {d['baseline']} → 本轮 {d['current']}（在容差内，判定不变）")
    lines += ["", "## 未执行项（独立核对缺位）", ""]
    unverified = [s for s in SPECS if results[s["id"]].get("unverified")]
    if not unverified:
        lines.append("- 无：联网独立核对（锚点现行性、本地库回查）本轮全部执行。")
    else:
        for s in unverified:
            lines.append(f"- {s['id']} {s['title']}：{results[s['id']]['observed']}")
        lines.append("- live 模式下存在未执行项即不过（fail-closed）；要显式放弃独立核对，用 `--no-live` 并在归档中记明。")
    lines += ["", "## 说明", ""]
    lines += [f"- {n}" for n in ctx.notes] or ["- 无"]
    lines += [
        "",
        "## 软项缺口披露",
        "",
        "- 软项判定未通过的项，其缺口必须能在知识包第八节局限声明中找到对应条目；未通过的软项见上表，对应披露原文见 `agent/knowledge-package.md` §8。",
        "",
        "## 断言表",
        "",
        table_markdown(results).strip(),
        "",
    ]
    return "\n".join(lines)


def soft_disclosure_check(ctx, results):
    """软项缺口必须进局限声明：按缺口关键词在第八节找披露；未披露则软项算失败。"""
    kp = ctx.text("agent/knowledge-package.md")
    lim = find_h2(kp, "局限声明") or ""
    undisclosed = []
    keys = {
        "A-07": ("检索", "命中", "查窗"),
        "B-08": ("排序", "分数", "权重"),
        "E-08": ("分级", "等级", "工具"),
        "P-05": ("锚点", "版本", "现行"),
        "S-01": ("复现", "检索式", "被引"),
        "S-02": ("覆盖", "路由", "近两年"),
        "S-03": ("分级", "仅摘要", "工具"),
        "S-04": ("时效", "现行版", "降档"),
        # A-12／A-13 的缺口须由交付自己一句话披露，故关键词避开 A-11 强制块（coverage_ledger 三段式）的用词：
        # 块内「经典段」行必含「核出」、「高被引缺席核对」行必含「高被引」——若沿用这些词，凡 A-11 过的交付
        # 都会把两条抽查软项的缺口判成「已披露」，软项阻断失效（Standards 复核发现）。
        "A-12": ("已知必中", "抽查轨"),
        "A-13": ("未核出", "未取回", "核出未达"),
    }
    for s in SPECS:
        if s["gate"] != "软项" or results[s["id"]]["ok"]:
            continue
        if not any(k in lim for k in keys.get(s["id"], ())):
            undisclosed.append(f"{s['id']} {s['title']}｜{results[s['id']]['observed'][:80]}")
    return undisclosed


def evaluate(ctx, results):
    for s in SPECS:
        if s["id"] in results:
            continue
        try:
            results[s["id"]] = s["fn"](ctx)
        except Exception as exc:  # 逐条隔离：执行错误单列，不掩盖其余判定
            results[s["id"]] = {"ok": False, "observed": f"执行错误：{type(exc).__name__}: {exc}"[:200], "error": True}
    return results


def gate_exit_code(hard_fail, undisclosed, errors, live_unverified):
    """退出码：0＝硬门禁全过且软项缺口全披露；1＝硬门禁未过、缺口未披露、执行错误或联网独立核对缺位。

    live_unverified＝联网模式下未执行到的独立核对（锚点现行性、本地库回查）：fail-closed；
    显式放弃独立核对（--no-live）由调用方传空表，语义不变。
    """
    return 0 if not (hard_fail or undisclosed or errors or live_unverified) else 1


def main():
    ap = argparse.ArgumentParser(description="固定选题端到端质量门禁")
    ap.add_argument("--run", required=True, help="验收跑目录（含知识包与 run/ 证据）")
    ap.add_argument("--skill", default=str(SKILL), help="skill 根目录")
    ap.add_argument("--repo", default=".", help="仓库根（用于定位可选的走查矩阵）")
    ap.add_argument("--out", default=str(HERE / "_results"), help="结果目录")
    ap.add_argument("--cur-year", type=int, default=2026, help="固定参考年份")
    ap.add_argument("--baseline", default=None, help="上一次 assertion-results.json（打开漂移对比）")
    ap.add_argument("--no-live", action="store_true", help="跳过联网核对（锚点锁定与在库回查记未执行；结果目录里窗口内的原 valid 锁仍按复用承接）")
    ap.add_argument("--walkthrough", default=None, help="可选：HITL 走查校验脚本路径（缺省记未执行）")
    ap.add_argument(
        "--zotero-fixture",
        default=None,
        help="自检专用：本地库只读回查的确定性夹具路径（JSON 形如 {\"abstract_notes\": {条目键: 摘要}}）；"
        "只供 --selftest 的孪生对照离线跑，正常跑不传该参数，一律走真实本地库",
    )
    ap.add_argument(
        "--coverage-fixtures",
        nargs="?",
        const=str(DEFAULT_COVERAGE_FIXTURES),
        default=None,
        help="显式启用覆盖 fixture 场景定义目录（ADR-0023 抽查轨 1；不带值＝skill 内 evals/coverage）；"
        "**不传该参数＝该轨不适用**（固定选题验收跑即此路，机制 fixture 不改写其判定）；"
        "`--selftest` 全程改指合成场景目录",
    )
    ap.add_argument("--selftest", action="store_true", help="用变异副本验证门禁有牙（孪生对照：锚点缺失／容差内漂移／软缺口未披露／附件缺口未披露／本次合集缺口／生长循环未落缓冲／旧顺序节序／摘要声明与库内不一致／仅题录摘要可得／覆盖账缺字段／缺口未披露／fixture 核出失败／锁窗口内复用／锁龄超窗重核／pins 指纹不等重核／漂移缺失未取到不复用为通过）")
    args = ap.parse_args()

    out = Path(args.out)
    if args.selftest:
        return selftest(args)

    ctx = Ctx(
        args.run, args.skill, args.repo, args.cur_year, not args.no_live, args.baseline, out,
        args.zotero_fixture, coverage_fixtures=args.coverage_fixtures,
    )
    if not (ctx.run / "agent/knowledge-package.md").exists():
        # 新形态件不见时先探测旧位同名件：在则本包仍是重排前形态（重排未接线），而非泛化的「输入缺失」
        legacy = (ctx.run / "knowledge-package.md").exists()
        why = "重排未接线" if legacy else "输入缺失"
        print(f"{why}：{ctx.run}/agent/knowledge-package.md（本包{'是' if legacy else '不是'}重排前形态）", file=sys.stderr)
        return 2
    ctx.note(f"口径版本：{ctx.caliber}（{CALIBER_MEANING[ctx.caliber]}）")
    ctx._results = {}
    ctx._json["__lock__"] = run_lock(ctx)
    run_script_json(ctx)
    run_walkthrough(ctx, args.walkthrough)
    evaluate(ctx, ctx._results)
    for s in SPECS:  # 汇总门在明细之后算
        if s["kind"] == "汇总":
            try:
                ctx._results[s["id"]] = s["fn"](ctx)
            except Exception as exc:
                ctx._results[s["id"]] = {"ok": False, "observed": f"执行错误：{exc}"[:200], "error": True}
    results = ctx._results
    gate_of = {s["id"]: s["gate"] for s in SPECS}
    unverified_ids = [s["id"] for s in SPECS if results[s["id"]].get("unverified")]
    hard_unverified = [i for i in unverified_ids if gate_of[i] == "硬门禁"]
    hard_fail = [s["id"] for s in SPECS if s["gate"] == "硬门禁" and not results[s["id"]]["ok"]]
    undisclosed = soft_disclosure_check(ctx, results)
    errors = [s["id"] for s in SPECS if results[s["id"]].get("error")]
    lock_unverified = [
        a["id"] for a in ((ctx._json.get("__lock__") or {}).get("anchors") or []) if a["status"] == "unverified"
    ]
    # live 模式下独立核对（锚点现行性、本地库回查）缺位即不过；--no-live 是显式放弃，语义不变
    exit_code = gate_exit_code(
        hard_fail,
        undisclosed,
        errors,
        (hard_unverified + lock_unverified) if ctx.live else [],
    )

    out.mkdir(parents=True, exist_ok=True)
    drifts = drift_rows(ctx, results)
    payload = {
        "run": run_label(ctx),
        "curve": {
            "assertions": len(SPECS),
            "hard_fail": hard_fail,
            "soft_fail": [s["id"] for s in SPECS if s["gate"] == "软项" and not results[s["id"]]["ok"]],
            "undisclosed_soft_gaps": undisclosed,
            "unverified": unverified_ids,
            "unverified_is_explicit_optout": bool(args.no_live),
            "anchor_lock_unverified": lock_unverified,  # 锚点锁逐条未取到（断言 P-05 之外的单列读数）
            "execution_errors": errors,
            "exit_code": exit_code,
        },
        "anchor_lock": ctx._json.get("__lock__"),
        "drift": drifts,
        "notes": ctx.notes,
        "results": [
            {
                "id": s["id"],
                "group": s["group"],
                "kind": s["kind"],
                "gate": s["gate"],
                "title": s["title"],
                "ok": results[s["id"]]["ok"],
                "observed": results[s["id"]]["observed"],
                "metric": results[s["id"]].get("metric"),
                "unverified": bool(results[s["id"]].get("unverified")),
                "error": bool(results[s["id"]].get("error")),
            }
            for s in SPECS
        ],
        "script_summary": (ctx._json.get("__script__") or {}).get("summary", {}),
    }
    (out / "assertion-results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "anchor-lock.json").write_text(json.dumps(ctx._json.get("__lock__"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "assertion-table.md").write_text(table_markdown(results), encoding="utf-8")
    (out / "gate-report.md").write_text(write_report(ctx, results, drifts, exit_code), encoding="utf-8")
    (out / "baseline.json").write_text(
        json.dumps(
            {"results": [{"id": s["id"], "kind": s["kind"], "metric": results[s["id"]].get("metric")} for s in SPECS]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"断言 {len(SPECS)} 条：硬门禁未过 {len(hard_fail)}｜软项未过 {payload['curve']['soft_fail']}｜"
        f"未披露缺口 {len(undisclosed)}｜执行错误 {len(errors)}｜未执行（独立核对）{len(unverified_ids)}｜"
        f"锚点锁复用 {len(payload['anchor_lock']['reuse']['reused'])} 条"
    )
    for note in ctx.notes:
        if note.startswith(LOCK_REUSE_NOTE):
            print(note)  # 复用标注（锁龄＋原锁定时刻）随 stdout 一并可见，不只落报告与 JSON
    print(f"结果目录：{out}（退出码 {exit_code}）")
    return exit_code


# ---------------------------------------------------------------- 门禁自检


def set_conclusion_order(text, *, conclusion_first):
    """把「结论」节块放到「证据表」节块之前／之后；返回 (新文本, 目标顺序是否成立)。

    节块按二级标题切；两节缺一即原样返回、目标顺序不成立。自检只用它构造临时孪生副本，不动归档包。
    """
    parts = re.split(r"(?m)^(##\s+.*)$", text)
    if len(parts) < 3:
        return text, False
    blocks = [(parts[n], parts[n + 1] if n + 1 < len(parts) else "") for n in range(1, len(parts), 2)]
    tail = parts[-1] if len(parts) % 2 == 1 else ""
    ci = next((n for n, b in enumerate(blocks) if "结论" in b[0]), None)
    ei = next((n for n, b in enumerate(blocks) if "证据表" in b[0]), None)
    if ci is None or ei is None:
        return text, False
    if (ci < ei) if conclusion_first else (ei < ci):
        return text, True
    a = min(ci, ei)
    first, second = (blocks[ci], blocks[ei]) if conclusion_first else (blocks[ei], blocks[ci])
    rest = [blk for n, blk in enumerate(blocks) if n != ci and n != ei]
    merged = rest[:a] + [first, second] + rest[a:]
    return parts[0] + "".join("".join(blk) for blk in merged) + tail, True


GROWTH_TWIN_SECTION = (
    "## 术语词表引用与候选\n"
    "\n"
    "- 词表版本：`{version}`（真源 `references/lexicon/term-lexicon.yaml` 的 version 字段）\n"
    "- 扩展方向与生长循环：下表逐词记录方向；未命中词按契约当场 `--append` 进候选队列（未落即门禁红；命中无边与有扩展者不需落队）——"
    "自检孪生副本只列词表命中的查询词，不造词表外词。\n"
    "\n"
    "| 查询词 | 方向 | 带出词 | 记录处 |\n"
    "|---|---|---|---|\n"
    "| 心肌梗死 | 上级→下级／同义／正字归一 | AMI、MI、NSTEMI、STEMI、心梗、心肌梗塞 | agent/search-strategy.md 关键词行 |\n"
    "| 心梗 | 同义／正字归一 | AMI、MI、NSTEMI、STEMI、心肌梗死、心肌梗塞 | agent/search-strategy.md 关键词行 |\n"
)


def align_lexicon_version(text, version):
    """把词表版本号对齐到真源现值；返回 (新文本, 原文是否有词表版本引用)。"""
    new, n = re.subn(r"lexicon v\d+\.\d+", version, text)
    return new, n > 0


def align_growth_section(text, version):
    """把词表小节整形为当前契约的合规节（节名＋查询词表）并对齐版本；返回 (新文本, 是否可动)。"""
    version_re = re.compile(r"lexicon v\d+\.\d+")
    lines = [version_re.sub(version, l) for l in text.splitlines()]
    start = next((n for n, l in enumerate(lines) if l.startswith("## ") and ("术语词表" in l or "生长循环" in l)), None)
    if start is None:
        return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), False
    end = next((n for n in range(start + 1, len(lines)) if lines[n].startswith("## ")), len(lines))
    merged = lines[:start] + GROWTH_TWIN_SECTION.format(version=version).splitlines() + lines[end:]
    return "\n".join(merged) + ("\n" if text.endswith("\n") else ""), True


def inject_plan_column(text, header_cell, value):
    """变异构造：给落点计划主表加一列（表头＋逐条取值）；主表缺表头即断言失败。"""
    lines = text.splitlines()
    head = next((n for n, l in enumerate(lines) if l.startswith("|") and "条目键" in l), None)
    assert head is not None, "副本缺落点计划主表表头"
    out, rows = [], 0
    for n, line in enumerate(lines):
        if n == head:
            out.append(f"{line.rstrip().rstrip('|').rstrip()} | {header_cell} |")
        elif n > head and re.match(r"^\|\s*[A-Z0-9]{8}(?![A-Z0-9])", line):
            out.append(f"{line.rstrip().rstrip('|').rstrip()} | {value} |")
            rows += 1
        else:
            out.append(line)
    assert rows >= 1, "副本缺计划表数据行（条目键列）"
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def abstract_twin_facts(pkg):
    """自检构造：落点计划与证据表现读的（条目键→DOI, 证据表 DOI 序列, 包内标仅题录的 DOI 集合）。

    仅题录＝三级取数皆无，故孪生副本要让「标仅题录者摘要不可得」这一前提成立——摘要列、可及性清单、
    库读夹具三处同源：摘要列记「未填」、清单自报 `abstract_source: none`、夹具摘要留空。
    """
    key_doi = {}
    for header, rows in md_tables_with_header((pkg / "agent" / "placement-plan.md").read_text(encoding="utf-8")):
        col = next((i for i, c in enumerate(header) if c.strip().upper().startswith("DOI")), None)
        for r in rows:
            m = re.match(r"^([A-Z0-9]{8})\b", r[0] if r else "")
            if not m:
                continue
            cell = r[col] if col is not None and len(r) > col else ""
            dm = DOI_RE.search(cell) or DOI_RE.search(" ".join(r))
            if dm:
                key_doi.setdefault(m.group(1), norm_doi(dm.group(1)))
    body = find_h2((pkg / "agent" / "knowledge-package.md").read_text(encoding="utf-8"), "证据表") or ""
    ev_dois, tertiary_dois = [], set()
    for cells in md_table(re.split(r"^###\s", body, flags=re.M)[0]):
        if not re.fullmatch(r"\d+[a-z]?", cells[0]):
            continue
        dm = DOI_RE.search(cells[1] if len(cells) > 1 else "")
        if not dm:
            continue
        doi = norm_doi(dm.group(1))
        ev_dois.append(doi)
        if any("仅题录" in c for c in cells):
            tertiary_dois.add(doi)
    return key_doi, ev_dois, tertiary_dois


def accessibility_manifest_for(pkg, tertiary_key):
    """自检构造：按包内证据表与落点计划现造一份可及性清单；仅题录条目（`tertiary_key` 与包内已标仅题录者）自报来源 none。

    只供 `--selftest` 的孪生对照：清单与包内其它读数自洽（仅题录条目三级取数皆无），故它只考
    「标仅题录者摘要可得」这一条（ADR-0023／E-19）；变异侧只改清单自报的 `abstract_source`。
    """
    key_doi, ev_dois, tertiary_dois = abstract_twin_facts(pkg)
    tertiary = key_doi.get(tertiary_key) or (ev_dois[0] if ev_dois else "")
    assert tertiary, "基线包既无落点计划 DOI 列也无证据表 DOI，无法构造可及性清单"
    tertiary_dois = set(tertiary_dois) | {tertiary}  # 包内已标仅题录者一并向清单自报（前提与包内读数同源）
    entries = []
    for doi in dict.fromkeys(ev_dois + list(key_doi.values())):
        is_tertiary = doi in tertiary_dois
        entries.append(
            {
                "doi": doi,
                "accessibility": "仅题录" if is_tertiary else "有全文",
                "abstract_source": "none" if is_tertiary else "table",
                "fetched_at": "2026-09-19",
            }
        )
    return {"checked_at": "2026-09-19", "source_order": ["library", "table", "network"], "entries": entries}


def flip_manifest_source(path):
    """变异构造：把清单里标仅题录那条的 `abstract_source` 由 `none` 改成有来源（自相矛盾）。"""
    raw = path.read_text(encoding="utf-8")
    new, n = re.subn(r'("abstract_source":\s*)"none"', r'\1"table"', raw, count=1)
    return new, n


def inject_present_caliber(text):
    """变异构造：在落点计划里伪写「本次合集口径：present」（无集名与逐条列）。"""
    return text.rstrip("\n") + "\n\n本次合集口径：present（自检变异：声称现行口径但无集名与逐条列）\n"


ABSTRACT_TWIN_HEADER = "摘要（已填：来源 / 未填：状态 / 不适用；来源取 table／crossref／pubmed／openalex／库内，状态取 empty／no-identifier）"


def inject_abstract_column(text, empty_key=None, tertiary_keys=()):
    """孪生构造：给落点计划主表加「摘要」列与摘要计数行，取值按自检夹具的库内摘要状态给。

    夹具是本地库读的替身（只带 abstract_note），只供 --selftest 的孪生对照离线跑；返回 (新文本, 夹具, 库内为空的条目键)。
    逐条「已填：来源」按口径五值轮转（含「库内」）、计数行由这些取值现算——孪生副本的声明与计数自洽，故它只考「声明↔库内一致」这一条。
    `tertiary_keys`＝包内标仅题录的计划条目：记「未填：no-identifier」且夹具摘要留空（仅题录＝三级取数皆无，
    否则孪生一造就把「无摘要」写成「已填」，E-19 的绿路会依赖基线包干净）。
    """
    lines = text.splitlines()
    head = next((n for n, l in enumerate(lines) if l.startswith("|") and "条目键" in l), None)
    assert head is not None, "副本缺落点计划主表表头"
    keys = [m.group(1) for n, l in enumerate(lines) if n > head and (m := re.match(r"^\|\s*([A-Z0-9]{8})(?![A-Z0-9])", l))]
    assert keys, "副本缺计划表数据行（条目键列）"
    empty_key = empty_key or keys[-1]  # 夹具里故意留空的那条：孪生记「未填」，变异把它伪写成「已填」
    tertiary = {k for k in keys if k in set(tertiary_keys)} - {empty_key}
    claimable = [k for k in keys if k != empty_key and k not in tertiary]
    values = {k: f"已填：{ABSTRACT_SOURCES[i % len(ABSTRACT_SOURCES)]}" for i, k in enumerate(claimable)}
    values.update({k: "未填：no-identifier" for k in tertiary})
    values[empty_key] = "未填：empty"
    per_src = {s: sum(1 for v in values.values() if v == f"已填：{s}") for s in ABSTRACT_SOURCES}
    n_filled = sum(1 for v in values.values() if v.startswith("已填："))
    count_line = (
        f"- 摘要计数行（自检孪生）：摘要：已填 {n_filled}（"
        + "／".join(f"{s} {per_src[s]}" for s in ABSTRACT_SOURCES)
        + f"）／未填 {sum(1 for v in values.values() if v.startswith('未填：'))}／不适用 0。"
    )
    out = []
    for n, l in enumerate(lines):
        if n == head:
            out += [count_line, f"{l.rstrip().rstrip('|').rstrip()} | {ABSTRACT_TWIN_HEADER} |"]
        elif n > head and re.match(r"^\|\s*[A-Z0-9]{8}(?![A-Z0-9])", l):
            key = re.match(r"^\|\s*([A-Z0-9]{8})", l).group(1)
            out.append(f"{l.rstrip().rstrip('|').rstrip()} | {values[key]} |")
        else:
            out.append(l)
    blank = {empty_key} | tertiary  # 仅题录与「库内为空」那条：夹具里都留空
    fixture = {"abstract_notes": {k: ("" if k in blank else f"自检夹具摘要 {k}") for k in keys}}
    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), fixture, empty_key


def flip_abstract_claim(text, key):
    """变异构造：把库内为空的条目伪写成「已填」，并同步改计数行——假账内部自洽，只有库内读回能抓到。

    返回 (新文本, 翻转处数)：只动这一条声明（外加计数行），不碰其余条目。
    声明取第五来源「库内」（条目本已有摘要、本轮未取数）——该 token 同样受库内读回约束。
    """
    src = ABSTRACT_SOURCES[-1]
    out, n = [], 0
    for line in text.splitlines():
        m = re.match(r"^(\|\s*" + re.escape(key) + r"(?![A-Z0-9]).*)\|\s*未填：empty\s*\|\s*$", line)
        if m:
            out.append(f"{m.group(1)}| 已填：{src} |")
            n += 1
        else:
            out.append(line)
    new = "\n".join(out) + ("\n" if text.endswith("\n") else "")

    def bump(m):
        inner = re.sub(rf"{src}\s*(\d+)", lambda x: f"{src} {int(x.group(1)) + 1}", m.group(2))
        return f"摘要：已填 {int(m.group(1)) + 1}（{inner}）／未填 {int(m.group(3)) - 1}／不适用 {m.group(4)}"

    new, n_line = re.subn(ABSTRACT_COUNT_RE, bump, new)
    return new, (n if n_line == 1 else 0)


def prior_lock_fixture(anchors, holder, *, age_hours=3.0, statuses=None, tamper_id=None):
    """自检夹具：造一份「上一轮」锚点锁（pins 指纹按当前钉子现算，状态与锁龄按参数给）。

    holder 是只为算指纹用的 Ctx（受检件以 holder.run 为准）。
    """
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - age_hours * 3600))
    entries = []
    for anchor in anchors["anchors"]:
        key = lock_pins_key(holder, anchor)
        if tamper_id == anchor["id"]:  # 指纹不等＝该条必须重核
            key = ("0" if key[0] != "0" else "1") + key[1:]
        status = (statuses or {}).get(anchor["id"], "valid")
        entries.append(
            {
                "id": anchor["id"],
                "label": anchor["label"],
                "identifier": anchor["identifier"],
                "kind": anchor["kind"],
                "method": anchor["lock"]["method"],
                "version_pin": anchor.get("version_pin", ""),
                "pins_key": key,
                "status": status,
                "detail": f"自检夹具：上一轮 {status} 读数",
                "checked_at": stamp,
                "reused": False,
            }
        )
    return {
        "locked_at": stamp,
        "anchors_version": anchors["version"],
        "pins": anchors.get("pins"),
        "reuse": {
            "window_hours": LOCK_WINDOW_HOURS,
            "carrier": "anchor-lock.json",
            "source_locked_at": stamp,
            "reused": [],
            "rechecked": [a["id"] for a in anchors["anchors"]],
        },
        "anchors": entries,
    }


LEDGER_FIELD_MUTATION = ("classic_segment", "found")  # 账类变异：删掉该块的冻结字段
LEDGER_GAP_MUTATION = "未选取"  # §8 缺口块变异：删掉该机制（ADR-0032 第九块）的三段式分句
# 自检合成场景（票 08 落真实场景前的合成夹具）：`applies_to` 只圈自检副本目录，
# 其余副本（clean／drift／…）据此走「不适用」绿路——自检不依赖真实 evals/coverage 的内容。
COVERAGE_TWIN_SCENARIOS = (
    {
        "id": "twin-classic-segment",
        "topic": "他汀-LDL-C 经典段（自检合成场景）",
        "kind": "classic",
        "expect_shape": "核出",
        "applies_to": ["twin-coverage", "mut-coverage"],
        "known_hits": [{"id": "WOSCOPS", "doi": "10.1056/nejm199510123331501"},
                       {"id": "CURVES", "doi": "10.1016/s0140-6736(98)04326-8"}],
        "baseline": {"note": "自检合成：基线读数只作形态示例，不断言"},
    },
    {
        "id": "twin-known-hit-persist1",
        "topic": "骨髓纤维化 PERSIST-1 回归（自检合成场景）",
        "kind": "known-hit",
        "expect_shape": "取回",
        "applies_to": ["twin-coverage", "mut-coverage"],
        "known_hits": [{"id": "PERSIST-1", "doi": "10.1016/s2352-3026(17)30027-3", "pmid": "28336242"}],
        "baseline": {"note": "自检合成：2026-09-17 交付池 242 条中缺席"},
    },
)


def synthetic_ledger(delivery):
    """自检夹具：字段齐全的覆盖账（九块＋冻结名＋pointer＋可选 note）。

    合成而非实跑：孪生只需「账在场且字段齐」这一前提；读数只需能被 `coverage_ledger.gap_lines`
    解成三段式分句（故每个冻结读数都填了可解的值）。
    """
    diag = ["run/b1/step1-diagnostics.json"]
    return {
        "generated_at": "2026-09-19",
        "delivery": delivery.as_posix(),
        "contract": "ADR-0023",
        "notes": ["自检合成账：只供 --selftest 的孪生对照"],
        "mechanisms": {
            "citation_expansion": {
                "ran": True, "seed_head": 20, "seed_judged": 4, "directions": ["后向", "前向"],
                "channels": [{"channel": "openalex-bwd", "requests": 24, "hits": 310}],
                "new_before_dedupe": 310, "new_after_dedupe": 268, "stop_reason": "种子集跑完",
                "filtered": 194, "filtered_pointer": ["run/b1/citation-expansion.json"],
                "contributed_final_count": 1426, "contributed_queue_count": 136,
                "contributed_queue_kinds": {"检索＋引文扩展": 80, "引文扩展": 56},
                "contributed_queue_origins": {"discovery": 80, "citation_bwd": 30, "citation_fwd": 26},
                "contribution_pointer": ["run/b1/dedupe-report.json", "run/b1/step1-pools.json"],
                "gap_reasons": ["截断 1 通道"], "pointer": ["run/b1/citation-expansion.json"],
            },
            "iterative_gap_fill": {
                "ran": True, "entity_checklist_count": 15, "gap_entity_count": 4, "waves": 2, "query_count": 7,
                "new_before_dedupe": 80, "new_after_dedupe": 80, "closure": "未闭合 2",
                "unclosed_entities": [{"name": "CURVES", "kind": "关键试验", "reason": "全窗检索 0 题录",
                                       "queries": ["CURVES statin"], "waves": [1]}],
                "contributed_final_count": 80, "contributed_queue_count": 5,
                "contributed_queue_kinds": {"追加轮": 4, "检索＋追加轮": 1},
                "contributed_queue_origins": {"gapfill_w1": 4, "discovery": 1},
                "contribution_pointer": ["run/b1/dedupe-report.json", "run/b1/step1-pools.json"],
                "pointer": ["run/b1/gap-fill.json"],
            },
            "high_cited_absence": {
                "ran": True, "queries": [{"query": "PCSK9 cited:desc", "depth": 200}],
                "absent_total": 364, "absent_relevant": 6, "absent_off_topic": 8, "absent_out_of_window": 0,
                "user_disposition": "不补", "pointer": ["run/b1/high-cited-check.json"],
            },
            "classic_segment": {
                "checked": 9, "found": 4, "unrecovered": [{"id": "CURVES", "reason": "标题词面 0 命中"}],
                "pointer": ["run/b1/high-cited-check.json"], "note": "自检合成：经典段被截 2 条",
            },
            "source_failure": {
                "failed_cell_count": 2, "recovered_cell_count": 1,
                "cells": [{"pool": "trials-full-window", "provider": "pubmed", "error": "unavailable",
                           "attempts": 2, "recovered": True, "net_effect": "重试取回（无净损失）"}],
                "pointer": diag,
            },
            "cap_truncation": {"truncated_cell_count": 3, "by_source_type": {"exact_set": 3, "recall_form": 0},
                               "cap_hit_unknown_count": 1, "pointer": diag},
            "date_filter": {"date_unknown_candidate_count": 4, "out_of_window_candidate_count": 7, "pointer": diag},
            "source_declaration": {
                "sources": ["pubmed", "openalex"],
                "not_included": [{"source": "google_scholar", "reason": "默认关"}],
                "gs_enabled": False, "identity_rejected_count": 15,
                "pointer": ["run/b1/source-declaration.json"],
            },
            "selection": {"selected_total": 60, "must_take_count": 24, "rank_fill_count": 36,
                          "unselected_total": 1504,
                          "unselected_by_rank_band": {"1-100": 0, "101-300": 0, "301-600": 0,
                                                      "601-1000": 120, "1001+": 1384, "无位次": 0},
                          "unselected_by_origin": {"discovery": 1504}, "classic_segment_cut": [],
                          "pointer": ["run/b1/selection.json", "run/b1/sort-report.json",
                                      "run/b1/dedupe-report.json"],
                          "note": "自检合成：读数派生自 run/b1/selection.json（账不复制逐条）"},
        },
    }


def write_ledger_twin(pkg, skill):
    """把合成账写进孪生副本，并按账把 §8「覆盖缺口」块补进知识包（三段式与账同源，照抄即合规）。"""
    ledger = synthetic_ledger(pkg)
    absent = [f"{b}.{f}" for b, fields in LEDGER_BLOCK_FIELDS.items() for f in fields
              if f not in ledger["mechanisms"][b]]
    assert not absent, f"自检合成账缺冻结字段（LEDGER_BLOCK_FIELDS 与夹具须同步）：{absent}"
    target = pkg / LEDGER_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    module = load_skill_module(skill, "coverage_ledger")
    append_coverage_gaps(pkg, module.format_lines(module.gap_lines(ledger)))
    return ledger


def append_coverage_gaps(pkg, lines):
    """按账把 §8「覆盖缺口」块（逐机制一行、三段式）补进局限声明末尾（无该节即断言失败）。"""
    path = pkg / "agent" / "knowledge-package.md"
    text = path.read_text(encoding="utf-8")
    head = re.search(r"(?m)^##\s+.*局限声明.*$", text)
    assert head is not None, "孪生副本缺第八节局限声明，无法补覆盖缺口块"
    tail = text[head.end():]
    nxt = re.search(r"(?m)^##\s+", tail)
    at = head.end() + (nxt.start() if nxt else len(tail))
    block = "\n覆盖缺口（ADR-0023 三段式，逐机制一行；读数以覆盖账为准）：\n\n" + "\n".join(lines) + "\n"
    path.write_text(text[:at].rstrip("\n") + block + text[at:], encoding="utf-8")


def drop_ledger_field(pkg, block, field):
    """变异构造：从合成账的某块删掉一个冻结字段；返回删除处数（0＝该字段本不在场）。"""
    path = pkg / LEDGER_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    hit = 1 if field in data["mechanisms"][block] else 0
    data["mechanisms"][block].pop(field, None)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return hit


def drop_gap_line(pkg, name):
    """变异构造：删掉 §8 里「- <机制名>｜…」那条三段式分句；返回删掉的行数。"""
    path = pkg / "agent" / "knowledge-package.md"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not line.startswith(f"- {name}｜")]
    path.write_text("".join(kept), encoding="utf-8")
    return len(lines) - len(kept)


def inflate_gap_reading(pkg, name):
    """变异构造：把 §8 里「- <机制名>｜…」那行的第一个数字加一位（如 1565 → 11565）；返回改动处数。

    读数数字漂移的代表变异：子串包含会把「1565」放行在「11565」里，逐字核对必须拦住。
    """
    path = pkg / "agent" / "knowledge-package.md"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    hit = 0
    for n, line in enumerate(lines):
        if not line.startswith(f"- {name}｜"):
            continue
        new = re.sub(r"\d+", lambda m: "1" + m.group(0), line, count=1)
        if new != line:
            lines[n] = new
            hit += 1
    path.write_text("".join(lines), encoding="utf-8")
    return hit


def write_coverage_check(pkg, scenarios, missing=()):
    """写核出读数件：`missing` 里的条目落成「未核出」（孪生侧为空＝逐条核出）。"""
    rows = []
    for s in scenarios:
        found = [{"id": h["id"], "where": s["expect_shape"], "evidence": "自检合成读数"}
                 for h in s["known_hits"] if h["id"] not in missing]
        rows.append({
            "id": s["id"], "expect_shape": s["expect_shape"], "found": found,
            "missing": [{"id": h["id"], "reason": "自检变异：核出失败"}
                        for h in s["known_hits"] if h["id"] in missing],
            "pointer": "run/b1/high-cited-check.json",
        })
    target = pkg / COVERAGE_CHECK_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"checked_at": "2026-09-19", "scenarios": rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_selftest_variants(base, tmp, skill):
    """构造自检全部副本（基线／变异副本／孪生对照）；前置不满足即 AssertionError（调用方如实记红）。"""
    clean = tmp / "clean"
    shutil.copytree(base, clean)
    # 1) 锚点缺失：删掉 A-04 期望行（行 6）
    missing = tmp / "missing-anchor"
    shutil.copytree(base, missing)
    pkg = (missing / "agent" / "knowledge-package.md").read_text(encoding="utf-8")
    pkg2 = re.sub(r"^\|\s*6\s*\|.*$", "", pkg, count=1, flags=re.M)
    (missing / "agent" / "knowledge-package.md").write_text(pkg2, encoding="utf-8")
    # 2) 容差内漂移：检索总命中数全部下降 2（provider 抖动，A-07 取最小值记漂移）
    drift = tmp / "drift"
    shutil.copytree(base, drift)
    ss = (drift / "agent" / "search-strategy.md").read_text(encoding="utf-8")
    m_hit = re.findall(r"总命中\s*(\d+)", ss)
    assert m_hit, "副本缺「总命中 N」读数"
    assert min(int(x) for x in m_hit) >= 3, "副本「总命中」读数不足以构造 −2 变异"
    ss2 = re.sub(r"总命中\s*(\d+)", lambda m: f"总命中 {int(m.group(1)) - 2}", ss)
    (drift / "agent" / "search-strategy.md").write_text(ss2, encoding="utf-8")
    # 3) 软缺口未披露：抽掉时效性证据（现行版豁免），并清掉局限声明内的时效关键词
    undisclosed = tmp / "undisclosed"
    shutil.copytree(base, undisclosed)
    kp = (undisclosed / "agent" / "knowledge-package.md").read_text(encoding="utf-8")
    assert "现行版豁免" in kp, "副本缺「现行版豁免」时效证据，无法构造软缺口未披露变异"
    kp2 = kp.replace("现行版豁免", "")

    def scrub_lim(m):
        body = m.group(0)
        for kw in ("时效", "现行版", "降档"):
            body = body.replace(kw, "")
        return body

    kp2 = re.sub(r"(?ms)^##\s+.*局限声明.*?(?=^##\s|\Z)", scrub_lim, kp2)
    (undisclosed / "agent" / "knowledge-package.md").write_text(kp2, encoding="utf-8")
    # 4) 附件缺口未披露：给计划表加附件列（逐条「无 PDF」）且局限声明不记这些键
    gap = tmp / "undisclosed-attach"
    shutil.copytree(base, gap)
    plan_path = gap / "agent" / "placement-plan.md"
    plan_path.write_text(
        inject_plan_column(
            plan_path.read_text(encoding="utf-8"),
            "附件（已挂：文件 / 无 PDF：去向）",
            "无 PDF：自检变异未披露",
        ),
        encoding="utf-8",
    )
    # 5) 本次合集缺口：伪写「口径 present」并加本次合集列（逐条「未挂」），集名与计数全缺
    topicgap = tmp / "undisclosed-topic"
    shutil.copytree(base, topicgap)
    topic_path = topicgap / "agent" / "placement-plan.md"
    topic_path.write_text(
        inject_present_caliber(
            inject_plan_column(
                topic_path.read_text(encoding="utf-8"),
                "本次合集（已挂：集名 / 未挂：去向）",
                "未挂：自检变异未披露",
            )
        ),
        encoding="utf-8",
    )
    # 6) 旧顺序（孪生对照）：孪生＝结论前移到证据表之前（ADR-0008 新顺序）；变异＝移回旧顺序
    order_twin = tmp / "twin-order"
    shutil.copytree(base, order_twin)
    kp_path = order_twin / "agent" / "knowledge-package.md"
    kp_new, ok_order = set_conclusion_order(kp_path.read_text(encoding="utf-8"), conclusion_first=True)
    assert ok_order, "副本缺结论／证据表节，无法构造顺序孪生"
    kp_path.write_text(kp_new, encoding="utf-8")
    order_mut = tmp / "mut-order"
    shutil.copytree(order_twin, order_mut)
    kp_old, ok_back = set_conclusion_order(
        (order_mut / "agent" / "knowledge-package.md").read_text(encoding="utf-8"), conclusion_first=False
    )
    assert ok_back, "顺序变异未发生（未移回旧顺序）"
    (order_mut / "agent" / "knowledge-package.md").write_text(kp_old, encoding="utf-8")
    # 7) 生长循环（孪生对照）：孪生＝词表节整形为合规节＋版本对齐；变异＝查询词换成未落缓冲的词
    lexicon_version = re.search(
        r"version:\s*(\S.+)", (Path(skill) / "references/lexicon/term-lexicon.yaml").read_text(encoding="utf-8")
    ).group(1).strip()
    growth_twin = tmp / "twin-growth"
    shutil.copytree(base, growth_twin)
    log_path = growth_twin / "agent" / "screening-log.md"
    ss_path = growth_twin / "agent" / "search-strategy.md"
    log_new, ok_log = align_growth_section(log_path.read_text(encoding="utf-8"), lexicon_version)
    ss_new, ok_ss = align_lexicon_version(ss_path.read_text(encoding="utf-8"), lexicon_version)
    assert ok_log and ok_ss, "副本缺词表小节或检索策略未引用词表版本，无法构造生长循环孪生"
    log_path.write_text(log_new, encoding="utf-8")
    ss_path.write_text(ss_new, encoding="utf-8")
    growth_mut = tmp / "mut-growth"
    shutil.copytree(growth_twin, growth_mut)
    log_mut, n_swap = re.subn(
        r"(?m)^\|\s*心肌梗死\s*\|.*$",
        "| zqx-unbuffered-term | 同义 | MI（自检变异：未落真源／队列的查询词） | agent/search-strategy.md 关键词行 |",
        (growth_mut / "agent" / "screening-log.md").read_text(encoding="utf-8"),
        count=1,
    )
    assert n_swap == 1, "生长循环变异未发生（孪生副本缺心肌梗死查询词行）"
    (growth_mut / "agent" / "screening-log.md").write_text(log_mut, encoding="utf-8")
    # 8) 摘要声明↔库内一致（孪生对照）：孪生＝按夹具库内摘要逐条声明（库内为空者记「未填」）；变异＝把该条伪写成「已填」
    key_doi, _ev_dois, tertiary_dois = abstract_twin_facts(base)
    tertiary_keys = {k for k, doi in key_doi.items() if doi in tertiary_dois}
    abstract_twin = tmp / "twin-abstract"
    shutil.copytree(base, abstract_twin)
    abstract_plan = abstract_twin / "agent" / "placement-plan.md"
    plan_new, abstract_fixture, empty_key = inject_abstract_column(
        abstract_plan.read_text(encoding="utf-8"), tertiary_keys=tertiary_keys
    )
    abstract_plan.write_text(plan_new, encoding="utf-8")
    fixture_path = tmp / "abstract-fixture.json"
    fixture_path.write_text(json.dumps(abstract_fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    abstract_mut = tmp / "mut-abstract"
    shutil.copytree(abstract_twin, abstract_mut)
    mut_plan = abstract_mut / "agent" / "placement-plan.md"
    mut_text, n_flip = flip_abstract_claim(mut_plan.read_text(encoding="utf-8"), empty_key)
    assert n_flip == 1, "摘要一致变异未发生（孪生副本缺库内为空的「未填」行）"
    mut_plan.write_text(mut_text, encoding="utf-8")
    # 9) 仅题录须可证无摘要（孪生对照，E-19）：孪生＝清单里那条仅题录自报「无可取摘要」（来源 none）；
    #    变异＝把该条自报来源改成有摘要（自相矛盾），离线交叉核对即可抓到
    acc_twin = tmp / "twin-accessibility"
    shutil.copytree(abstract_twin, acc_twin)  # 复用带「摘要」列的孪生（摘要列与清单两处读数同源）
    acc_manifest = acc_twin / "run/step5-accessibility.json"
    acc_manifest.write_text(
        json.dumps(accessibility_manifest_for(acc_twin, empty_key), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    # 10) 覆盖账类断言（硬门禁 A-10／A-11；孪生对照）：孪生＝合成账＋按账补的 §8 覆盖缺口块；
    #     变异①＝账块缺一个冻结字段；变异②＝§8 少一条机制分句（缺口未披露）
    ledger_twin = tmp / "twin-ledger"
    shutil.copytree(base, ledger_twin)
    write_ledger_twin(ledger_twin, skill)
    ledger_field_mut = tmp / "mut-ledger-field"
    shutil.copytree(ledger_twin, ledger_field_mut)
    assert drop_ledger_field(ledger_field_mut, *LEDGER_FIELD_MUTATION) == 1, "账字段变异未发生（合成账缺该字段）"
    ledger_gap_mut = tmp / "mut-ledger-gap"
    shutil.copytree(ledger_twin, ledger_gap_mut)
    assert drop_gap_line(ledger_gap_mut, LEDGER_GAP_MUTATION) == 1, "缺口分句变异未发生（孪生副本 §8 缺该行）"
    ledger_digits_mut = tmp / "mut-ledger-digits"
    shutil.copytree(ledger_twin, ledger_digits_mut)
    assert inflate_gap_reading(ledger_digits_mut, LEDGER_GAP_MUTATION) == 1, "读数漂移变异未发生（孪生副本 §8 缺该行）"
    # 11) 覆盖 fixture 场景（软项 A-12／A-13；孪生对照）：合成场景目录＋核出读数件；孪生副本取自**带 §8 覆盖缺口块**
    #     的账类孪生（块在场时缺口仍须进未披露——否则 A-11 强制的块会把两条抽查软项的披露判成已披露）；
    #     变异＝把某个已知必中落成「未核出」（已知必中一条、经典段一条，两条软项各拿一次翻转）
    coverage_fixtures = tmp / "coverage-fixtures"
    coverage_fixtures.mkdir(parents=True, exist_ok=True)
    for scenario in COVERAGE_TWIN_SCENARIOS:
        (coverage_fixtures / f"{scenario['id']}.json").write_text(
            json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    coverage_twin = tmp / "twin-coverage"
    shutil.copytree(ledger_twin, coverage_twin)
    write_coverage_check(coverage_twin, COVERAGE_TWIN_SCENARIOS)
    coverage_known_mut = tmp / "mut-coverage-known-hit"
    shutil.copytree(coverage_twin, coverage_known_mut)
    write_coverage_check(coverage_known_mut, COVERAGE_TWIN_SCENARIOS, missing=("PERSIST-1",))
    coverage_classic_mut = tmp / "mut-coverage-classic"
    shutil.copytree(coverage_twin, coverage_classic_mut)
    write_coverage_check(coverage_classic_mut, COVERAGE_TWIN_SCENARIOS, missing=("CURVES",))
    acc_mut = tmp / "mut-accessibility"
    shutil.copytree(acc_twin, acc_mut)
    mut_manifest, acc_flip = flip_manifest_source(acc_mut / "run/step5-accessibility.json")
    assert acc_flip == 1, "仅题录可得性变异未发生（孪生副本的可及性清单缺 abstract_source=none 条目）"
    (acc_mut / "run/step5-accessibility.json").write_text(mut_manifest, encoding="utf-8")
    # 12) 卡目录形态（硬门禁 E-04；孪生对照）：孪生＝`agent/cards/*.md` 逐件可解析；
    #     变异①＝卡目录清空（0 卡）；变异②＝卡目录删除（目录缺）——两侧都要求 E-04 未通过，
    #     且报错含读取路径与解析条数（§11 防再犯：先报解析面，不落「行 1 没有对应提取卡」）
    cards_dir = base / package_parse.CARDS_REL
    assert cards_dir.is_dir() and any(cards_dir.glob(f"*{package_parse.CARD_SUFFIX}")), \
        "基线缺 `agent/cards/*.md`，无法构造卡目录形态孪生（基座须为新落点带卡形态）"
    cards_twin = tmp / "twin-cards"
    shutil.copytree(base, cards_twin)
    cards_empty_mut = tmp / "mut-cards-empty"
    shutil.copytree(cards_twin, cards_empty_mut)
    for path in (cards_empty_mut / "agent" / "cards").glob("*.md"):
        path.unlink()
    cards_missing_mut = tmp / "mut-cards-missing"
    shutil.copytree(cards_twin, cards_missing_mut)
    shutil.rmtree(cards_missing_mut / "agent" / "cards")
    return {
        "clean": clean,
        "missing": missing,
        "drift": drift,
        "undisclosed": undisclosed,
        "gap": gap,
        "topicgap": topicgap,
        "order_twin": order_twin,
        "order_mut": order_mut,
        "growth_twin": growth_twin,
        "growth_mut": growth_mut,
        "abstract_twin": abstract_twin,
        "abstract_mut": abstract_mut,
        "abstract_fixture": fixture_path,
        "accessibility_twin": acc_twin,
        "accessibility_mut": acc_mut,
        "ledger_twin": ledger_twin,
        "ledger_field_mut": ledger_field_mut,
        "ledger_gap_mut": ledger_gap_mut,
        "ledger_digits_mut": ledger_digits_mut,
        "coverage_fixtures": coverage_fixtures,
        "coverage_twin": coverage_twin,
        "coverage_known_mut": coverage_known_mut,
        "coverage_classic_mut": coverage_classic_mut,
        "cards_twin": cards_twin,
        "cards_empty_mut": cards_empty_mut,
        "cards_missing_mut": cards_missing_mut,
    }


def write_selftest_report(out_root, scenarios):
    """落自检摘要（JSON＋中文报告）并打印；返回退出码（全过 0，否则 1）。"""
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "selftest-results.json").write_text(
        json.dumps({"scenarios": scenarios}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# 门禁自检（变异副本）",
        "",
        "自检口径＝孪生对照：每个场景先造只对齐该断言前提的孪生副本（临时副本，不动归档），要求孪生上目标断言通过、"
        "施加变异后未通过——故自检不依赖基线包是否当前口径；历史包在新门禁下未过属预期（ADR-0008），基线行只如实报读数。",
        "每条被考断言都在自检内被观测到「通过」与「未通过」两侧（孪生侧为绿路），绿路不靠基线包干净。",
        "",
        "| 场景 | 期望 | 实测 | 判定 |",
        "|---|---|---|---|",
    ]
    for s in scenarios:
        mark = "基线" if s.get("control") else ("通过" if s["ok"] else "未通过")
        lines.append(f"| {s['name']} | {s['expect']} | {s['got']} | {mark} |")
    (out_root / "selftest-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(
        f"{'基线' if s.get('control') else ('通过' if s['ok'] else '未通过')}｜{s['name']}｜{s['got']}" for s in scenarios
    ))
    return 0 if all(s["ok"] for s in scenarios) else 1


def card_parse_reading_reports_path(result):
    """卡面解析失败的读数须含读取路径与解析条数（0 卡／目录缺各一条；§11 防再犯的可观测面）。"""
    text = (result or {}).get("observed") or ""
    return "agent/cards/*.md" in text and "解析条数" in text


def selftest(args):
    """用变异副本验证门禁有牙（孪生对照口径）。

    每个场景先造只对齐该断言前提的孪生副本、断言目标断言在孪生上通过，再施加变异、断言其未通过；
    覆盖：锚点缺失、容差内漂移、软缺口未披露、附件缺口未披露、本次合集缺口、生长循环未落缓冲、
    旧顺序节序、摘要声明与库内不一致、仅题录摘要可得（E-19）、覆盖账缺字段（A-10）、缺口未披露（A-11）、
    fixture 核出失败（A-12／A-13）、卡目录形态（0 卡／目录缺，E-04）、锁窗口内复用、锁龄超窗重核、
    pins 指纹不等重核、漂移／缺失／未取到不复用为通过。
    基线行只如实报读数——历史包在新门禁下未过属预期（ADR-0008），故自检不依赖基线包口径新旧。
    """
    base = Path(args.run).resolve()
    out_root = Path(args.out)
    tmp = Path(tempfile.mkdtemp(prefix="gate-selftest-"))
    scenarios = []
    try:
        try:
            variants = build_selftest_variants(base, tmp, args.skill)
        except (AssertionError, OSError) as exc:  # 基线不支持某变异构造（缺件含旧落点基座）：如实记红，不把整轮自检崩掉
            detail = f"{type(exc).__name__}: {exc}"
            if not (base / "agent/knowledge-package.md").exists():
                detail += "（基座非新落点形态：孪生夹具按 `agent/` 读写——重排未接线）"
            return write_selftest_report(
                out_root,
                [
                    {
                        "name": "基线可构造性（前置检查）",
                        "expect": "基线须是新落点形态且支持全部变异构造（结论／证据表节、计划主表、词表小节与词表版本引用、`agent/cards/*.md` 带卡形态）",
                        "got": f"未构造成功：{detail}",
                        "ok": False,
                    }
                ],
            )

        # 自检全程用合成场景目录（不读真实 evals/coverage）：自检的读数不依赖票 08 场景件的内容
        args.coverage_fixtures = str(variants["coverage_fixtures"])
        # 跑：基线（对照）／漂移（比基线）／六个变异副本／六对孪生对照／四个锁龄变异
        anchors = json.loads(ANCHORS_PATH.read_text(encoding="utf-8"))
        clean = variants["clean"]
        clean_out = out_root / "selftest/clean"
        run_once(args, clean, clean_out, baseline=None)
        drift_out = out_root / "selftest/drift"
        run_once(args, variants["drift"], drift_out, baseline=clean_out / "assertion-results.json")
        missing_out = out_root / "selftest/missing-anchor"
        run_once(args, variants["missing"], missing_out, baseline=None)
        undisclosed_out = out_root / "selftest/undisclosed"
        run_once(args, variants["undisclosed"], undisclosed_out, baseline=None)
        gap_out = out_root / "selftest/undisclosed-attach"
        run_once(args, variants["gap"], gap_out, baseline=None)
        topicgap_out = out_root / "selftest/undisclosed-topic"
        run_once(args, variants["topicgap"], topicgap_out, baseline=None)
        order_twin_out = out_root / "selftest/twin-order"
        run_once(args, variants["order_twin"], order_twin_out, baseline=None)
        order_mut_out = out_root / "selftest/mut-order"
        run_once(args, variants["order_mut"], order_mut_out, baseline=None)
        growth_twin_out = out_root / "selftest/twin-growth"
        run_once(args, variants["growth_twin"], growth_twin_out, baseline=None)
        growth_mut_out = out_root / "selftest/mut-growth"
        run_once(args, variants["growth_mut"], growth_mut_out, baseline=None)
        abstract_twin_out = out_root / "selftest/twin-abstract"
        run_once(
            args, variants["abstract_twin"], abstract_twin_out, baseline=None, zotero_fixture=variants["abstract_fixture"]
        )
        abstract_mut_out = out_root / "selftest/mut-abstract"
        run_once(
            args, variants["abstract_mut"], abstract_mut_out, baseline=None, zotero_fixture=variants["abstract_fixture"]
        )
        acc_twin_out = out_root / "selftest/twin-accessibility"
        run_once(
            args,
            variants["accessibility_twin"],
            acc_twin_out,
            baseline=None,
            zotero_fixture=variants["abstract_fixture"],
        )
        acc_mut_out = out_root / "selftest/mut-accessibility"
        run_once(
            args,
            variants["accessibility_mut"],
            acc_mut_out,
            baseline=None,
            zotero_fixture=variants["abstract_fixture"],
        )
        ledger_twin_out = out_root / "selftest/twin-ledger"
        run_once(args, variants["ledger_twin"], ledger_twin_out, baseline=None)
        ledger_field_out = out_root / "selftest/mut-ledger-field"
        run_once(args, variants["ledger_field_mut"], ledger_field_out, baseline=None)
        ledger_gap_out = out_root / "selftest/mut-ledger-gap"
        run_once(args, variants["ledger_gap_mut"], ledger_gap_out, baseline=None)
        ledger_digits_out = out_root / "selftest/mut-ledger-digits"
        run_once(args, variants["ledger_digits_mut"], ledger_digits_out, baseline=None)
        coverage_twin_out = out_root / "selftest/twin-coverage"
        run_once(args, variants["coverage_twin"], coverage_twin_out, baseline=None)
        coverage_known_out = out_root / "selftest/mut-coverage-known-hit"
        run_once(args, variants["coverage_known_mut"], coverage_known_out, baseline=None)
        coverage_classic_out = out_root / "selftest/mut-coverage-classic"
        run_once(args, variants["coverage_classic_mut"], coverage_classic_out, baseline=None)
        cards_twin_out = out_root / "selftest/twin-cards"
        run_once(args, variants["cards_twin"], cards_twin_out, baseline=None)
        cards_empty_out = out_root / "selftest/mut-cards-empty"
        run_once(args, variants["cards_empty_mut"], cards_empty_out, baseline=None)
        cards_missing_out = out_root / "selftest/mut-cards-missing"
        run_once(args, variants["cards_missing_mut"], cards_missing_out, baseline=None)
        lock_seeds = {
            "lock-window": {"age_hours": 3.0},
            "lock-over-window": {"age_hours": LOCK_WINDOW_HOURS + 32},
            "lock-pins-key": {"age_hours": 3.0, "tamper_id": "A-01"},
            "lock-non-valid": {
                "age_hours": 3.0,
                "statuses": {"A-01": "drifted", "A-02": "missing", "A-03": "unverified"},
            },
        }
        lock_holder = Ctx(clean, args.skill, args.repo, args.cur_year, False, None, HERE)
        lock_seed_data = {}
        for name, opts in lock_seeds.items():
            lock_seed_data[name] = prior_lock_fixture(anchors, lock_holder, **opts)
            run_once(args, clean, out_root / f"selftest/{name}", baseline=None, prior_lock=lock_seed_data[name])

        def load(p):
            return json.loads((p / "assertion-results.json").read_text(encoding="utf-8"))

        def verdict(payload, aid):
            return next((r for r in payload["results"] if r["id"] == aid), {})

        def flip(control, mutant, aid):
            """目标断言：对照通过 → 变异未通过（该断言被变异抓到）。"""
            return bool(verdict(control, aid).get("ok")) and not verdict(mutant, aid).get("ok")

        base_res, drift_res = load(clean_out), load(drift_out)
        missing_res, und_res = load(missing_out), load(undisclosed_out)
        gap_res, topicgap_res = load(gap_out), load(topicgap_out)
        order_twin_res, order_mut_res = load(order_twin_out), load(order_mut_out)
        growth_twin_res, growth_mut_res = load(growth_twin_out), load(growth_mut_out)
        abstract_twin_res, abstract_mut_res = load(abstract_twin_out), load(abstract_mut_out)
        acc_twin_res, acc_mut_res = load(acc_twin_out), load(acc_mut_out)
        ledger_twin_res, ledger_field_res = load(ledger_twin_out), load(ledger_field_out)
        ledger_gap_res = load(ledger_gap_out)
        ledger_digits_res = load(ledger_digits_out)
        coverage_twin_res, coverage_known_res = load(coverage_twin_out), load(coverage_known_out)
        coverage_classic_res = load(coverage_classic_out)
        cards_twin_res, cards_empty_res = load(cards_twin_out), load(cards_empty_out)
        cards_missing_res = load(cards_missing_out)
        base_hard, drift_hard = sorted(base_res["curve"]["hard_fail"]), sorted(drift_res["curve"]["hard_fail"])
        anchor_ids = [a["id"] for a in anchors["anchors"]]
        lock_res = {name: load(out_root / f"selftest/{name}")["anchor_lock"] for name in lock_seeds}
        win_lock, over_lock = lock_res["lock-window"], lock_res["lock-over-window"]
        key_lock, novalid_lock = lock_res["lock-pins-key"], lock_res["lock-non-valid"]
        win_entries = {a["id"]: a for a in win_lock["anchors"]}
        seed_stamp = {a["id"]: a["checked_at"] for a in lock_seed_data["lock-window"]["anchors"]}
        win_ok = sorted(win_lock["reuse"]["reused"]) == anchor_ids and all(
            win_entries[i]["status"] == "valid"
            and win_entries[i].get("reused") is True
            and "复用（锁龄" in win_entries[i]["detail"]
            and "＋原锁定" in win_entries[i]["detail"]
            and win_entries[i]["checked_at"] == seed_stamp[i]
            for i in anchor_ids
        )
        over_ok = over_lock["reuse"]["reused"] == [] and all(
            a.get("reused") is False and a["checked_at"] == over_lock["locked_at"] for a in over_lock["anchors"]
        )
        key_bad = [a for a in key_lock["anchors"] if a["id"] == "A-01"][0]
        key_ok = key_lock["reuse"]["reused"] == [i for i in anchor_ids if i != "A-01"] and key_bad.get("reused") is False
        nonvalid_ids = ("A-01", "A-02", "A-03")
        novalid_entries = {a["id"]: a for a in novalid_lock["anchors"]}
        nonvalid_ok = novalid_lock["reuse"]["reused"] == [i for i in anchor_ids if i not in nonvalid_ids] and all(
            novalid_entries[i]["status"] != "valid" and novalid_entries[i].get("reused") is False
            for i in nonvalid_ids
        )
        scenarios = [
            {
                "name": "基线（对照，不作判定）",
                "expect": "如实报基线读数；历史包在新门禁下未过属预期（ADR-0008），各场景用孪生对照证明有牙",
                "got": f"退出码 {base_res['curve']['exit_code']}；硬门禁未过 {len(base_hard)} 项"
                + (f"（{'、'.join(base_hard[:6])}{'…' if len(base_hard) > 6 else ''}）" if base_hard else ""),
                "ok": True,
                "control": True,
            },
            {
                "name": "锚点缺失（删行 6）",
                "expect": "P-02 对照通过 → 变异未通过，变异退出码 1",
                "got": f"P-02 对照={'通过' if verdict(base_res, 'P-02').get('ok') else '未通过'}／"
                f"变异={'未通过' if not verdict(missing_res, 'P-02').get('ok') else '通过'}"
                f"（{(verdict(missing_res, 'P-02').get('observed') or '')[:50]}）；退出码 {missing_res['curve']['exit_code']}",
                "ok": flip(base_res, missing_res, "P-02") and missing_res["curve"]["exit_code"] == 1,
            },
            {
                "name": "容差内漂移（总命中 −2）",
                "expect": "漂移日志非空且判定集与对照一致（容差不改判定）",
                "got": "；".join(f"{d['id']} 基线 {d['baseline']}→本轮 {d['current']}" for d in drift_res["drift"])
                + f"；判定集{'一致' if drift_hard == base_hard else f'不等（{drift_hard} vs {base_hard}）'}",
                "ok": len(drift_res["drift"]) >= 1 and drift_hard == base_hard,
            },
            {
                "name": "软缺口未披露（抽掉现行版豁免且局限无时效条目）",
                "expect": "S-04 对照通过 → 变异未通过，缺口未披露且退出码 1",
                "got": f"S-04 变异={'未通过' if not verdict(und_res, 'S-04').get('ok') else '通过'}；"
                f"未披露 {len(und_res['curve']['undisclosed_soft_gaps'])} 项；退出码 {und_res['curve']['exit_code']}",
                "ok": flip(base_res, und_res, "S-04")
                and bool(und_res["curve"]["undisclosed_soft_gaps"])
                and und_res["curve"]["exit_code"] == 1,
            },
            {
                "name": "附件缺口未披露（加附件列逐条无 PDF 且局限无该键）",
                "expect": "D-09 对照通过 → 变异未通过，变异退出码 1",
                "got": f"D-09 变异={'未通过' if not verdict(gap_res, 'D-09').get('ok') else '通过'}"
                f"（{(verdict(gap_res, 'D-09').get('observed') or '')[:50]}）；退出码 {gap_res['curve']['exit_code']}",
                "ok": flip(base_res, gap_res, "D-09") and gap_res["curve"]["exit_code"] == 1,
            },
            {
                "name": "本次合集缺口（口径注记伪写成 present）",
                "expect": "D-10 对照通过 → 变异未通过，变异退出码 1",
                "got": f"D-10 变异={'未通过' if not verdict(topicgap_res, 'D-10').get('ok') else '通过'}"
                f"（{(verdict(topicgap_res, 'D-10').get('observed') or '')[:50]}）；退出码 {topicgap_res['curve']['exit_code']}",
                "ok": flip(base_res, topicgap_res, "D-10") and topicgap_res["curve"]["exit_code"] == 1,
            },
            {
                "name": "旧顺序（孪生＝结论在证据表之前）",
                "expect": "E-01 孪生通过 → 变异（移回旧顺序）未通过",
                "got": f"E-01 孪生={'通过' if verdict(order_twin_res, 'E-01').get('ok') else '未通过'}／"
                f"变异={'未通过' if not verdict(order_mut_res, 'E-01').get('ok') else '通过'}"
                f"（{(verdict(order_mut_res, 'E-01').get('observed') or '')[:50]}）",
                "ok": bool(verdict(order_twin_res, "E-01").get("ok")) and not verdict(order_mut_res, "E-01").get("ok"),
            },
            {
                "name": "生长循环（孪生＝词表节合规且版本对齐）",
                "expect": "A-08 孪生通过 → 变异（查询词未落真源／队列）未通过",
                "got": f"A-08 孪生={'通过' if verdict(growth_twin_res, 'A-08').get('ok') else '未通过'}"
                f"（{(verdict(growth_twin_res, 'A-08').get('observed') or '')[:40]}）／"
                f"变异={'未通过' if not verdict(growth_mut_res, 'A-08').get('ok') else '通过'}"
                f"（{(verdict(growth_mut_res, 'A-08').get('observed') or '')[:40]}）",
                "ok": bool(verdict(growth_twin_res, "A-08").get("ok")) and not verdict(growth_mut_res, "A-08").get("ok"),
            },
            {
                "name": "摘要声明↔库内一致（孪生＝按自检夹具逐条声明）",
                "expect": "D-11 孪生通过 → 变异（把库内为空的那条伪写成「已填：库内」，计数行同步自洽）未通过，变异退出码 1",
                "got": f"D-11 孪生={'通过' if verdict(abstract_twin_res, 'D-11').get('ok') else '未通过'}"
                f"（{(verdict(abstract_twin_res, 'D-11').get('observed') or '')[:40]}）／"
                f"变异={'未通过' if not verdict(abstract_mut_res, 'D-11').get('ok') else '通过'}"
                f"（{(verdict(abstract_mut_res, 'D-11').get('observed') or '')[:50]}）；"
                f"退出码 {abstract_mut_res['curve']['exit_code']}",
                "ok": flip(abstract_twin_res, abstract_mut_res, "D-11")
                and abstract_mut_res["curve"]["exit_code"] == 1,
            },
            {
                "name": "仅题录摘要可得（孪生＝清单自报无可取摘要）",
                "expect": "E-19 孪生通过 → 变异（清单把 `abstract_source` 由 none 改成有来源，自相矛盾）未通过，变异退出码 1",
                "got": f"E-19 孪生={'通过' if verdict(acc_twin_res, 'E-19').get('ok') else '未通过'}"
                f"（{(verdict(acc_twin_res, 'E-19').get('observed') or '')[:40]}）／"
                f"变异={'未通过' if not verdict(acc_mut_res, 'E-19').get('ok') else '通过'}"
                f"（{(verdict(acc_mut_res, 'E-19').get('observed') or '')[:60]}）；"
                f"退出码 {acc_mut_res['curve']['exit_code']}",
                "ok": flip(acc_twin_res, acc_mut_res, "E-19") and acc_mut_res["curve"]["exit_code"] == 1,
            },
            {
                "name": "覆盖账字段齐（孪生＝合成账九块齐）",
                "expect": f"A-10 孪生通过 → 变异（账块 {LEDGER_FIELD_MUTATION[0]} 缺冻结字段 {LEDGER_FIELD_MUTATION[1]}）未通过，变异退出码 1",
                "got": f"A-10 孪生={'通过' if verdict(ledger_twin_res, 'A-10').get('ok') else '未通过'}"
                f"／变异={'未通过' if not verdict(ledger_field_res, 'A-10').get('ok') else '通过'}"
                f"（{(verdict(ledger_field_res, 'A-10').get('observed') or '')[:60]}）；"
                f"退出码 {ledger_field_res['curve']['exit_code']}",
                "ok": flip(ledger_twin_res, ledger_field_res, "A-10")
                and ledger_field_res["curve"]["exit_code"] == 1,
            },
            {
                "name": "缺口未披露（孪生＝§8 覆盖缺口块十句齐）",
                "expect": f"A-11 孪生通过 → 变异（删「{LEDGER_GAP_MUTATION}」分句）未通过，变异退出码 1",
                "got": f"A-11 孪生={'通过' if verdict(ledger_twin_res, 'A-11').get('ok') else '未通过'}"
                f"（{(verdict(ledger_twin_res, 'A-11').get('observed') or '')[:40]}）／"
                f"变异={'未通过' if not verdict(ledger_gap_res, 'A-11').get('ok') else '通过'}"
                f"（{(verdict(ledger_gap_res, 'A-11').get('observed') or '')[:60]}）；"
                f"退出码 {ledger_gap_res['curve']['exit_code']}",
                "ok": flip(ledger_twin_res, ledger_gap_res, "A-11")
                and ledger_gap_res["curve"]["exit_code"] == 1,
            },
            {
                "name": f"读数漂移（孪生＝§8 读数逐字对齐；变异＝{LEDGER_GAP_MUTATION} 读数加一位）",
                "expect": "A-11 孪生通过 → 变异（§8 读数数字加一位）未通过，变异退出码 1",
                "got": f"A-11 孪生={'通过' if verdict(ledger_twin_res, 'A-11').get('ok') else '未通过'}／"
                f"变异={'未通过' if not verdict(ledger_digits_res, 'A-11').get('ok') else '通过'}"
                f"（{(verdict(ledger_digits_res, 'A-11').get('observed') or '')[:70]}）；"
                f"退出码 {ledger_digits_res['curve']['exit_code']}",
                "ok": flip(ledger_twin_res, ledger_digits_res, "A-11")
                and ledger_digits_res["curve"]["exit_code"] == 1,
            },
            {
                "name": "fixture 核出失败（已知必中；孪生＝逐条核出，副本带 §8 覆盖缺口块）",
                "expect": "A-12 孪生通过 → 变异（PERSIST-1 落成未核出）未通过，该缺口未披露且退出码 1",
                "got": f"A-12 孪生={'通过' if verdict(coverage_twin_res, 'A-12').get('ok') else '未通过'}"
                f"（{(verdict(coverage_twin_res, 'A-12').get('observed') or '')[:40]}）／"
                f"变异={'未通过' if not verdict(coverage_known_res, 'A-12').get('ok') else '通过'}"
                f"（{(verdict(coverage_known_res, 'A-12').get('observed') or '')[:60]}）；"
                f"未披露 {[x for x in coverage_known_res['curve']['undisclosed_soft_gaps'] if x.startswith('A-12')]}；"
                f"退出码 {coverage_known_res['curve']['exit_code']}",
                "ok": flip(coverage_twin_res, coverage_known_res, "A-12")
                and any(x.startswith("A-12") for x in coverage_known_res["curve"]["undisclosed_soft_gaps"])
                and coverage_known_res["curve"]["exit_code"] == 1,
            },
            {
                "name": "fixture 核出失败（经典段；孪生＝逐条核出，副本带 §8 覆盖缺口块）",
                "expect": "A-13 孪生通过 → 变异（CURVES 落成未核出）未通过，该缺口未披露且退出码 1",
                "got": f"A-13 孪生={'通过' if verdict(coverage_twin_res, 'A-13').get('ok') else '未通过'}"
                f"（{(verdict(coverage_twin_res, 'A-13').get('observed') or '')[:40]}）／"
                f"变异={'未通过' if not verdict(coverage_classic_res, 'A-13').get('ok') else '通过'}"
                f"（{(verdict(coverage_classic_res, 'A-13').get('observed') or '')[:60]}）；"
                f"未披露 {[x for x in coverage_classic_res['curve']['undisclosed_soft_gaps'] if x.startswith('A-13')]}；"
                f"退出码 {coverage_classic_res['curve']['exit_code']}",
                "ok": flip(coverage_twin_res, coverage_classic_res, "A-13")
                and any(x.startswith("A-13") for x in coverage_classic_res["curve"]["undisclosed_soft_gaps"])
                and coverage_classic_res["curve"]["exit_code"] == 1,
            },
            {
                "name": "卡目录形态（孪生＝`agent/cards/*.md` 逐件可解析）",
                "expect": "E-04 孪生通过 → 变异（0 卡／目录缺）两侧均未通过，报错含读取路径与解析条数",
                "got": f"E-04 孪生={'通过' if verdict(cards_twin_res, 'E-04').get('ok') else '未通过'}"
                f"／0 卡={'未通过' if not verdict(cards_empty_res, 'E-04').get('ok') else '通过'}"
                f"（{(verdict(cards_empty_res, 'E-04').get('observed') or '')[:70]}）"
                f"／目录缺={'未通过' if not verdict(cards_missing_res, 'E-04').get('ok') else '通过'}"
                f"（{(verdict(cards_missing_res, 'E-04').get('observed') or '')[:70]}）",
                "ok": flip(cards_twin_res, cards_empty_res, "E-04")
                and flip(cards_twin_res, cards_missing_res, "E-04")
                and card_parse_reading_reports_path(verdict(cards_empty_res, "E-04"))
                and card_parse_reading_reports_path(verdict(cards_missing_res, "E-04")),
            },
            {
                "name": f"锁窗口内复用（锁龄 {lock_seeds['lock-window']['age_hours']}h）",
                "expect": "全部条目复用原 valid 结论，留痕记「复用（锁龄 Xh）＋原锁定时刻」，checked_at 不回填本轮",
                "got": f"复用 {len(win_lock['reuse']['reused'])}／{len(anchor_ids)} 条；"
                f"留痕样例 {win_entries[anchor_ids[0]]['detail'][:56]}；checked_at 沿用原锁定时刻",
                "ok": win_ok,
            },
            {
                "name": f"锁龄超窗重核（{lock_seeds['lock-over-window']['age_hours']}h）",
                "expect": "超窗不复用，逐条重核并照常写锁（checked_at＝本轮）",
                "got": f"复用 {len(over_lock['reuse']['reused'])} 条；重核 {len(over_lock['anchors'])} 条（checked_at＝本轮 {over_lock['locked_at']}）",
                "ok": over_ok,
            },
            {
                "name": "pins 指纹不等重核（A-01）",
                "expect": "指纹不等者重核、其余照旧复用",
                "got": f"复用 {len(key_lock['reuse']['reused'])} 条（{','.join(key_lock['reuse']['reused'])}）；"
                f"A-01 重核后状态 {key_bad['status']}",
                "ok": key_ok,
            },
            {
                "name": "漂移／缺失／未取到不复用为通过",
                "expect": "原非 valid 条目一律重核，永不复用为通过",
                "got": f"复用 {len(novalid_lock['reuse']['reused'])} 条；{('／'.join(nonvalid_ids))} 重核后状态 "
                f"{'／'.join(novalid_entries[i]['status'] for i in nonvalid_ids)}（--no-live 故记未执行）",
                "ok": nonvalid_ok,
            },
        ]
        out_root.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(out_root / "selftest", ignore_errors=True)  # 只留摘要与读数，不留逐场景全量报告
        return write_selftest_report(out_root, scenarios)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_once(args, run_dir, out_dir, baseline=None, prior_lock=None, zotero_fixture=None):
    """自检内部：按给定副本目录跑一次门禁（不打印、不退出）。

    prior_lock 非空时预置结果目录里的锚点锁——锁龄变异以此造「上一轮」复用载体。
    zotero_fixture 非空时把自检夹具路径交给门禁——摘要断言（D-11）的孪生对照离线跑以此造确定性库读；
    正常跑不传该值，门禁一律走真实本地库（--no-live 下记未执行）。
    """
    out = Path(out_dir)
    if prior_lock is not None:
        out.mkdir(parents=True, exist_ok=True)
        (out / "anchor-lock.json").write_text(
            json.dumps(prior_lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    argv = [
        sys.argv[0],
        "--run",
        str(run_dir),
        "--skill",
        args.skill,
        "--repo",
        args.repo,
        "--out",
        str(out_dir),
        "--cur-year",
        str(args.cur_year),
        "--no-live",
    ]
    if baseline:
        argv += ["--baseline", str(baseline)]
    if zotero_fixture is not None:
        argv += ["--zotero-fixture", str(zotero_fixture)]
    if getattr(args, "coverage_fixtures", None):
        argv += ["--coverage-fixtures", str(args.coverage_fixtures)]
    proc = subprocess.run([sys.executable] + argv, capture_output=True, text=True, encoding="utf-8", timeout=900)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
