# -*- coding: utf-8 -*-
"""Step1 格级读数投影：把检索 CLI 的逐格读数投影成 **ADR-0023 冻结名形态**的 `run/b1/step1-diagnostics.json`。

When: Step1 装配之后（发现层跑完、Step2 之前或之后皆可；覆盖账产出之前必须已投影）。
Do: uv run python .claude/skills/med-lit-review/scripts/step1_diagnostics.py \
        --delivery workspace/YYYY-MM-DD-<slug> [--report F]

Why: 格级读数唯一家在 `run/b1/step1-diagnostics.json`，其字段名由 ADR-0023 冻结为 snake_case
（`hit_count｜total_available｜cap_hit｜cap_policy｜date_filtered_count｜date_unknown_count｜date_rejected_count｜
attempts｜retried｜recovered_by_retry｜cache_hit｜collected_at`）。检索 CLI 的原始响应按 TS 命名写 camelCase
（`queryPlan.queryStatuses[]` 的 `hitCount／totalAvailable／capHit／capPolicy／dateFilteredCount／
dateUnknownCount／dateRejectedCount／attempts／retried／recoveredByRetry／cacheHit／collectedAt`），
逐池原样复制会让下游（覆盖账的命中上限披露／日期过滤／源失败三块、§8 缺口分句）恒读不到数——
故投影是**契约落地件**，不是可选的整理动作：冻结名必须由本脚本产出，不做驼峰兼容分支。

读（交付内，只读）：`run/b1/raw/<池>.json`（Step1 逐池原始响应：`queryPlan.queryStatuses[]` 与顶层
`diagnostics[]`）、`run/b1/step1-pools.json`（**检索池**名全集）。**只投影检索池**（`pool_kind` 缺省按 `检索`
计）——机制取数件（`citation-*.json`／`gap-fill-w*.json`／`high-cited-*.json`）不进格级件：引文扩展轮的通道截断
在其读数件 `citation-expansion.json` 的逐通道 `truncated`，追加轮波次读数在 `gap-fill.json` 的 `waves[]`，
格级件的对象是「Step1 检索池 × 源」这一层。
写（交付内，`run/b1/` 忽略位）：`step1-diagnostics.json`（逐池逐格一行，冻结名 16 键恒在场；CLI 行其余键
原样透传，如失败格的 `error`）。

字段映射（驼峰 → 冻结名）：`hitCount→hit_count`、`totalAvailable→total_available`、`capHit→cap_hit`、
`capPolicy→cap_policy`、`dateFilteredCount→date_filtered_count`、`dateUnknownCount→date_unknown_count`、
`dateRejectedCount→date_rejected_count`、`recoveredByRetry→recovered_by_retry`、`cacheHit→cache_hit`、
`collectedAt→collected_at`；`pool_id`（取池文件名）／`query`／`provider`／`status` 同名。
CLI 行缺的冻结键记 `null`（＝未取数，**不得读作 false／0**）；`cap_hit: boolean|null` 语义沿 ADR-0023。

退出码：0 正常；2 用法、交付目录缺、或零格可投影（原始响应缺则如实报缺，不代造格）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from step3_queue import read_json  # noqa: E402 - 同族脚本跨件复用（容错读 JSON 同口径）
from citation_expand import dumps, write_atomic  # noqa: E402 - 原子写与 JSON 落盘同口径，不复制第二份

B1_DIR = "run/b1"
DEFAULT_POOL_KIND = "检索"   # 缺省口径沿 ADR-0023：池无 `pool_kind` 即检索池
RAW_DIR = f"{B1_DIR}/raw"
POOLS_NAME = f"{B1_DIR}/step1-pools.json"
REPORT_NAME = f"{B1_DIR}/step1-diagnostics.json"

CELL_FIELDS = ("query", "provider", "status", "hit_count", "total_available", "cap_hit", "cap_policy",
               "date_filtered_count", "date_unknown_count", "date_rejected_count", "attempts", "retried",
               "recovered_by_retry", "cache_hit", "collected_at")
CELL_ALIASES = {"hitCount": "hit_count", "totalAvailable": "total_available", "capHit": "cap_hit",
                "capPolicy": "cap_policy", "dateFilteredCount": "date_filtered_count",
                "dateUnknownCount": "date_unknown_count", "dateRejectedCount": "date_rejected_count",
                "recoveredByRetry": "recovered_by_retry", "cacheHit": "cache_hit", "collectedAt": "collected_at"}
UNREAD = ("cap_hit", "date_unknown_count", "date_rejected_count")


def project_cell(pool_id: str, row: dict) -> dict:
    """单格投影：冻结名恒在场（缺者记 ∅），其余键原样透传。"""
    cell = {"pool_id": pool_id}
    cell.update({CELL_ALIASES.get(key, key): value for key, value in row.items()})
    for field in CELL_FIELDS:
        cell.setdefault(field, None)
    return cell


def project(delivery: Path) -> tuple[list[dict], dict]:
    raw_dir = delivery / RAW_DIR
    if not raw_dir.is_dir():
        raise ValueError(f"原始响应目录缺（{RAW_DIR}）——先跑 Step1 检索")
    cells: list[dict] = []
    covered, skipped, renamed = [], [], 0
    retrieval_ids = set()
    pools_data = read_json(delivery / POOLS_NAME)
    if isinstance(pools_data, dict):
        retrieval_ids = {str(pool.get("pool_id")) for pool in pools_data.get("pools") or []
                         if isinstance(pool, dict)
                         and (pool.get("pool_kind") or DEFAULT_POOL_KIND) == DEFAULT_POOL_KIND}
    for path in sorted(raw_dir.glob("*.json")):
        data = read_json(path)
        plan = data.get("queryPlan") if isinstance(data, dict) else None
        if not isinstance(plan, dict) or (retrieval_ids and path.stem not in retrieval_ids):
            skipped.append(path.stem)
            continue
        pool_id = path.stem
        covered.append(pool_id)
        statuses = [row for row in (plan.get("queryStatuses") or []) if isinstance(row, dict)]
        for row in statuses:
            renamed += sum(1 for key in row if key in CELL_ALIASES)
            cells.append(project_cell(pool_id, row))
        for diag in [row for row in (data.get("diagnostics") or []) if isinstance(row, dict)]:
            cells.append({"pool_id": pool_id, "diagnostic": diag})
    if not cells:
        raise ValueError(f"零格可投影（{RAW_DIR} 内无 `queryPlan.queryStatuses`）——如实报缺，不代造格")
    real_cells = [cell for cell in cells if cell.get("provider")]
    info = {"pools_covered": len(covered),
            "pools_missing": sorted(pool_id for pool_id in retrieval_ids if pool_id not in covered),
            "skipped_raw": skipped, "cells": len(real_cells), "diagnostics": len(cells) - len(real_cells),
            "renamed": renamed,
            "unread": {field: sum(1 for cell in real_cells if cell.get(field) is None) for field in UNREAD}}
    return cells, info


def print_summary(cells: list[dict], info: dict, report: str) -> None:
    print(f"格级投影：检索池 {info['pools_covered']} 个｜格 {info['cells']} 条（冻结名 16 键恒在场；"
          f"驼峰改名 {info['renamed']} 处）＋非格诊断 {info['diagnostics']} 条"
          f"｜跳过的非检索／机制原始响应 {len(info['skipped_raw'])} 件"
          f"｜缺读数记 ∅：cap_hit {info['unread']['cap_hit']}／date_unknown_count "
          f"{info['unread']['date_unknown_count']}／date_rejected_count {info['unread']['date_rejected_count']}")
    if info["pools_missing"]:
        print(f"格级投影·注：检索池有 {len(info['pools_missing'])} 个无原始响应（读数缺，记 ∅ 不代造）："
              f"{'、'.join(info['pools_missing'])}")
    print(f"格级投影：读取数件 {report}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Step1 格级读数投影：检索 CLI 读数 → ADR-0023 冻结名形态")
    parser.add_argument("--delivery", required=True, help="交付目录（workspace/YYYY-MM-DD-<slug>）")
    parser.add_argument("--report", help=f"落点（默认 <交付>/{REPORT_NAME}）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    delivery = Path(args.delivery)
    report = Path(args.report) if args.report else delivery / REPORT_NAME
    try:
        cells, info = project(delivery)
    except (OSError, ValueError) as exc:
        print(f"格级投影错误：{exc}", file=sys.stderr)
        return 2
    write_atomic(report, dumps(cells))
    print_summary(cells, info, report.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
