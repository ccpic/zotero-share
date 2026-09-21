# -*- coding: utf-8 -*-
"""Step3 下载队列层：失败记忆（否定缓存）与调用侧纪律。

四个动词，全部只在交付目录内读写；不碰上游 scansci-pdf 的磁盘缓存与键：

  plan          读研读选取集（队列；默认 `agent/download-queue.txt`），按失败记忆剪队列，写
                `run/step3-queue-pruned.txt`；每条剪队列动作进运行日志
                （错误类＋原始失败日期），便于台账「未采用原因」列逐条落字。
  collect       读上游批量收据（`batch_results*.json`／`download_results*.json`），
                按返回的文件定位字段把成功件搬进 `run/fulltext/`（不依赖 --output 目录，
                也不假设暂存目录内有件），写 `run/download-results.json`，把
                「全通道穷尽型确定性失败」逐条记进失败记忆（传输／凭证／配置类永不记），
                并在件已到手时清掉同一 DOI 的旧否定条目。
  retry         只从上轮收据或显式 DOI 造重试队列（没有全量重提入口）；条目按研读选取集行序
                （＝S 序）排序，非覆盖位次；错文错源恢复换显式 strategy（逐条单篇，天然 miss、
                免清缓存）。
  probe-guard   渠道状态每会话最多一次：同会话（同日）同 kind 二次调用打印 SKIP；
                登录后 `--reset` 显式放行一次，动作照常进日志。

口径句（ADR-0032）：`agent/download-queue.txt`＝研读选取集＝入库集＝研读对象（三集合一，机器账＝
`run/b1/selection.json`）；本文件其余各处的「队列」一律读作研读选取集，件行序＝S 序（剪队列不改写本件）。

规则真源：`references/protocols/workspace-guide.md` §14.1（错误类二分、显式绕过）与 §14.2（失败记忆 14 天）；
调用侧纪律与产出物位置：`references/protocols/step3-download-chain.md`。

到期基准：原始失败时刻＝该 DOI 收据落盘时刻（上游收据无逐条时间戳，取该 DOI 各收据 mtime 的最早读数）。
跨交付携带（`--carry`）只读在先交付的 `run/step3-failure-memory.json`：属缓存携带、非形态 B 取件链
（无字节件、不进 manifest 指针段）；悬空即忽略，不补写、不反向登记。

退出码：0 正常；2 用法或文件错误。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

TTL_DAYS = 14
DEFAULT_CALIBER = "post-0006-fastest"
DEFAULT_STRATEGY = "fastest"
GREY_SOURCES = ("sci-hub", "scihub", "libgen", "z-lib", "zlibrary", "annas-archive")
GREY_ORIENTED = ("scihub_only", "grey_only", "scihub_first")

QUEUE_NAME = "agent/download-queue.txt"
MANIFEST_NAME = "agent/manifest.json"
FULLTEXT_DIR = "run/fulltext"
BATCHES_DIR = "run/batches"
MEMORY_NAME = "run/step3-failure-memory.json"
LOG_NAME = "run/step3-queue-log.jsonl"
PRUNED_NAME = "run/step3-queue-pruned.txt"
RETRY_NAME = "run/step3-queue-retry.txt"
RESULT_NAME = "run/download-results.json"
IDENTITY_NAME = "run/fulltext-identity.json"
PROBE_NAME = "run/step3-session-probes.json"

# 错误类二分（workspace-guide §14.1）：可记忆＝全通道穷尽型确定性失败（无来源类）；
# 其余三类一律不写负面缓存，防「登录／修配置／网络恢复后仍被剪」。
NO_SOURCE_TYPES = {"not_found"}
NO_SOURCE_REASONS = {"no pdf found", "no source"}
TRANSPORT_TYPES = {
    "timeout", "rate_limited", "server_error", "ssl_error", "network_blocked",
    "dns_blocked", "in_progress", "unknown",
}
CREDENTIAL_TYPES = {
    "paywall", "forbidden", "not_entitled", "login_required", "cloudflare_blocked", "captcha",
}
CONFIG_TYPES = {"config_needed", "browser_unavailable", "config_error", "not_configured"}
VETO_TYPES = {
    **{t: "transport" for t in TRANSPORT_TYPES},
    **{t: "credential" for t in CREDENTIAL_TYPES},
    **{t: "config" for t in CONFIG_TYPES},
}
VETO_KEYWORDS = (
    ("transport", ("timeout", "timed out", "rate limit", "rate-limit", "429", "too many requests",
                   "server error", "ssl", "dns", "connection reset",
                   "connection refused", "connection error", "network", "超时", "限流", "连接", "域名")),
    ("credential", ("paywall", "not_entitled", "not entitled", "forbidden", "403", "401",
                    "unauthorized", "login", "captcha", "cloudflare", "blocked", "no access",
                    "未授权", "被挡", "登录")),
    ("config", ("config_needed", "browser_unavailable", "not installed", "not configured",
                "config_error", "配置", "未安装")),
)
HTTP_CODE_RE = re.compile(r"(?<![\w.])(4\d\d|5\d\d)(?![\w.])")
DOI_PREFIX_RE = re.compile(r"^https?://(dx\.)?doi\.org/", re.I)
DOI_TRAIL_RE = re.compile(r"[。，,;；)）\]]+$")
DOI_KEY_RE = re.compile(r"^10\.\d{4,9}/\S+$")


def norm_doi(text: str | None) -> str:
    """归一 DOI：小写、去前缀与尾部标点（与去重键、复用键同口径）。"""
    return DOI_TRAIL_RE.sub("", DOI_PREFIX_RE.sub("", (text or "").strip())).lower()


def is_doi_key(key: str) -> bool:
    """失败记忆只收 DOI 主键：无标识记录禁入持久化（SKILL 可追溯链）。"""
    return bool(DOI_KEY_RE.match(key))


def doi_slug(doi: str) -> str:
    return norm_doi(doi).replace("/", "_")


def parse_time(value: str) -> datetime:
    """接受 YYYY-MM-DD 或 ISO 8601；作为 14 天窗口与探针的时钟钩子。"""
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return datetime.fromisoformat(text).astimezone()
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone()


def fmt_date(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m-%d")


def read_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_rows(path: Path) -> list[dict]:
    """容忍上游三种收据形态：行列表、{results:[…]}（竞速引擎）、{entries:[…]}（CLI 批量）。"""
    data = read_json(path)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("results", "entries"):
            rows = data.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def iter_receipts(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.rglob("*.json") if p.is_file())


def veto_class_of(row: dict) -> str | None:
    """返回该行的不可记忆类（transport／credential／config）；无否决证据返回 None。"""
    for field in ("error_type", "action"):
        kind = VETO_TYPES.get(str(row.get(field, "")).strip().lower())
        if kind:
            return kind
    for failure in row.get("source_failures") or []:
        if isinstance(failure, dict):
            kind = VETO_TYPES.get(str(failure.get("error_type", "")).strip().lower())
            if kind:
                return kind
    blob = " ".join(str(row.get(field, "")) for field in ("error", "reason")).lower()
    for kind, keywords in VETO_KEYWORDS:
        if any(keyword in blob for keyword in keywords):
            return kind
    return "transport" if HTTP_CODE_RE.search(blob) else None


def classify(rows: list[dict]) -> str:
    """按交付侧的全部收据行判定失败类。

    no_source＝有肯定证据（error_type not_found 或 reason「no PDF found」类）且无任何否决证据，
    即「全通道穷尽型确定性失败」；其余一律不记忆（否决类名或 unproven）。
    """
    veto, positive = None, False
    for row in rows:
        if row.get("success"):
            continue
        kind = veto_class_of(row)
        if kind and veto is None:
            veto = kind
        error_type = str(row.get("error_type", "")).strip().lower()
        reason = str(row.get("reason", "")).strip().lower()
        if error_type in NO_SOURCE_TYPES or reason in NO_SOURCE_REASONS:
            positive = True
    if veto:
        return veto
    return "no_source" if positive else "unproven"


def is_grey(source: str) -> bool:
    text = (source or "").lower()
    return any(grey in text for grey in GREY_SOURCES)


def read_memory(path: Path) -> list[dict]:
    data = read_json(path)
    entries = data.get("entries") if isinstance(data, dict) else None
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def entry_time(entry: dict) -> datetime | None:
    """记忆条目时刻；缺字段或不可解析的条目当损坏处理（跳过，不让缓存文件挡住下载链）。"""
    try:
        return parse_time(str(entry.get("failed_at", "")))
    except ValueError:
        return None


def write_memory(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "note": "Step3 否定缓存：只记全通道穷尽型确定性失败（workspace-guide §14.1）；剪队列不写回本件。",
               "entries": entries}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def memory_key(entry: dict) -> tuple:
    """记忆键：归一 DOI＋错误类＋口径版本；同键下按 strategy 收敛（显式 strategy 天然 miss）。"""
    return (norm_doi(entry.get("doi")), entry.get("error_class"), entry.get("caliber"), entry.get("strategy"))


def live_entry(entries: list[dict], doi: str, caliber: str, strategy: str, now: datetime) -> dict | None:
    """命中＝同键且原始失败时刻在 14 天内（窗口自原始失败时刻起算，剪队列不顺延）。"""
    wanted = (norm_doi(doi), "no_source", caliber, strategy)
    for entry in entries:
        if memory_key(entry) != wanted:
            continue
        failed_at = entry_time(entry)
        if failed_at is not None and now - failed_at <= timedelta(days=TTL_DAYS):
            return {**entry, "failed_at_dt": failed_at}
    return None


def load_carry(paths: list[Path], now: datetime) -> list[dict]:
    """读在先交付的失败记忆（只读，悬空即忽略）；窗口仍自原始失败时刻起算，不续期。"""
    carried: list[dict] = []
    for path in paths:
        for entry in read_memory(path):
            failed_at = entry_time(entry)
            if (failed_at is not None and entry.get("error_class") == "no_source"
                    and now - failed_at <= timedelta(days=TTL_DAYS)):
                carried.append({**entry, "carried_from": rel_posix(path)})
    return carried


def merge_memory(entries: list[dict], incoming: list[dict], cleared: set[str], now: datetime) -> list[dict]:
    """写入合并：同键以本次失败时刻为准；成功件对应条目清账；顺带回收 14 天外条目（只删不改时刻）。"""
    merged = {memory_key(e): e for e in entries}
    for entry in incoming:
        merged[memory_key(entry)] = entry
    kept = []
    for entry in merged.values():
        moment = entry_time(entry)
        if moment is None or now - moment > timedelta(days=TTL_DAYS):
            continue
        if norm_doi(entry.get("doi")) in cleared:
            continue
        kept.append(entry)
    return sorted(kept, key=lambda e: (norm_doi(e.get("doi")), str(e.get("strategy"))))


def append_log(path: Path, records: list[dict]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_queue(path: Path) -> list[str]:
    dois, seen = [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        doi = norm_doi(line)
        if doi and not doi.startswith("#") and doi not in seen:
            seen.add(doi)
            dois.append(doi)
    return dois


def resolve_caliber(delivery: Path, override: str | None) -> str:
    if override:
        return override
    manifest = read_json(delivery / MANIFEST_NAME)
    caliber = manifest.get("caliber", {}).get("grey_gate_version") if isinstance(manifest, dict) else None
    return caliber or DEFAULT_CALIBER


def resolve_locator(locator: str, delivery: Path, batches: Path) -> Path | None:
    """按返回的文件定位字段找件：原样 → 交付／暂存目录下的同名件；找不到即 miss。"""
    text = (locator or "").strip().strip('"')
    if not text:
        return None
    given = Path(text)
    candidates = [given] if given.is_absolute() else [Path.cwd() / given, delivery / given]
    candidates += [delivery / FULLTEXT_DIR / given.name, batches / given.name]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def rel_posix(path: Path) -> str:
    """台账／收据／日志只许相对路径或 basename：绝不写绝对路径（机密与本地路径零泄漏）。"""
    for root in (Path.cwd(),):
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            continue
    return path.name


def relative_to_delivery(path: Path, delivery: Path) -> str:
    try:
        return path.relative_to(delivery).as_posix()
    except ValueError:
        return rel_posix(path)


def short(text: str, limit: int = 90) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def batch_command(queue_file: str, out_dir: str) -> str:
    """默认车道的批量命令：不带 --no-lanes／--scihub；机构车道串行沿用上游 config。"""
    return f"scansci-pdf batch {queue_file} --output {out_dir} --format json"


def single_command(doi: str, strategy: str) -> str:
    return f"scansci-pdf get {doi} --strategy {strategy} --output {BATCHES_DIR}/out --format json"


def load_identity(delivery: Path) -> dict[str, str]:
    """身份核验收据按 DOI 取短串（verdict），供收据 identity_check 回填；缺件即不回填。"""
    data = read_json(delivery / IDENTITY_NAME)
    rows = data.get("entries") if isinstance(data, dict) else None
    verdicts: dict[str, str] = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("doi") and row.get("verdict"):
                verdicts[norm_doi(row["doi"])] = str(row["verdict"])
    return verdicts


def load_stage(args: argparse.Namespace) -> dict:
    """三个动词共用的装载：交付路径、时钟、口径、失败记忆（本交付＋跨交付携带）、队列路径。"""
    delivery = Path(args.delivery)
    now = parse_time(args.now) if getattr(args, "now", None) else datetime.now().astimezone()
    memory_path = Path(args.memory) if args.memory else delivery / MEMORY_NAME
    carry_paths = [Path(p) for p in args.carry]
    carried = load_carry(carry_paths, now)
    return {
        "delivery": delivery,
        "now": now,
        "caliber": resolve_caliber(delivery, args.caliber),
        "strategy": args.strategy,
        "memory_path": memory_path,
        "entries": read_memory(memory_path) + carried,
        "carried": carried,
        "queue_path": Path(args.queue) if args.queue else delivery / QUEUE_NAME,
    }


def prune_dois(dois: list[str], entries: list[dict], caliber: str, strategy: str,
               force: bool, now: datetime, source: str) -> tuple[list[str], list[dict]]:
    """剪队列：命中失败记忆即剪；`force`＝显式重抓，无条件绕过并留痕。"""
    kept, records = [], []
    for doi in dois:
        hit = None if force else live_entry(entries, doi, caliber, strategy, now)
        if hit is None:
            kept.append(doi)
            if force:
                existing = next((e for e in entries if norm_doi(e.get("doi")) == doi), None)
                if existing is not None:
                    records.append({"at": now.isoformat(), "action": "force-retry", "doi": doi,
                                    "error_class": existing.get("error_class"),
                                    "failed_at": existing.get("failed_at"), "source": source,
                                    "reason": "显式重抓绕过失败记忆（workspace-guide §14.1）"})
            continue
        records.append({"at": now.isoformat(), "action": "pruned", "doi": doi,
                        "error_class": hit.get("error_class"), "failed_at": hit["failed_at"],
                        "failed_on": fmt_date(hit["failed_at_dt"]),
                        "age_days": round((now - hit["failed_at_dt"]).total_seconds() / 86400, 2),
                        "source": source, "caliber": caliber, "strategy": strategy})
    return kept, records


def report_carry(carried: list[dict]) -> None:
    if carried:
        sources = sorted({entry.get("carried_from", "") for entry in carried})
        print(f"携带在先交付记忆 {len(carried)} 条（{'、'.join(sources)}；只读，悬空即忽略）")


def cmd_plan(args: argparse.Namespace) -> int:
    stage = load_stage(args)
    delivery, now, caliber, strategy = stage["delivery"], stage["now"], stage["caliber"], stage["strategy"]
    queue_path = stage["queue_path"]
    if not queue_path.is_file():
        print(f"队列文件不存在：{queue_path}", file=sys.stderr)
        return 2

    dois = read_queue(queue_path)
    kept, records = prune_dois(dois, stage["entries"], caliber, strategy, args.force, now, "queue")
    pruned = len(dois) - len(kept)

    pruned_path = delivery / PRUNED_NAME
    pruned_path.parent.mkdir(parents=True, exist_ok=True)
    header = (f"# Step3 剪队列产物：源 {relative_to_delivery(queue_path, delivery)}｜口径 {caliber}"
              f"｜strategy {strategy}｜剪 {pruned}／{len(dois)}（{fmt_date(now)}）\n")
    pruned_path.write_text(header + "".join(f"{doi}\n" for doi in kept), encoding="utf-8")
    append_log(delivery / LOG_NAME, records)

    report_carry(stage["carried"])
    print(f"队列 {len(dois)} 条 → 保留 {len(kept)}／剪 {pruned}"
          f"（口径 {caliber}｜strategy {strategy}{'｜显式重抓' if args.force else ''}）")
    print(f"剪后队列：{relative_to_delivery(pruned_path, delivery)}")
    for record in records[:20]:
        print(f"  剪队列 {record['doi']}｜错误类 {record['error_class']}｜原始失败 {record.get('failed_on', '')}")
    if len(records) > 20:
        print(f"  …共 {len(records)} 条动作，逐条见 {LOG_NAME}")
    print(f"台账「未采用原因」列逐条写：失败记忆剪队列（<错误类>；原始失败 <YYYY-MM-DD>）"
          f"；台账行数与队列条目数守恒（＝研读选取集条目数，三集合一，ADR-0032），剪队列不改写 {QUEUE_NAME}")
    print(f"批量命令（默认车道；默认路径不得加 --no-lanes／--scihub）："
          f"{batch_command(relative_to_delivery(pruned_path, delivery), f'{BATCHES_DIR}/out')}")
    print("车道纪律：批量保持默认车道（上游 batch_default_lanes=true）；机构车道串行（上游 institutional_workers=1）不改。")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    stage = load_stage(args)
    delivery, now, caliber, strategy = stage["delivery"], stage["now"], stage["caliber"], stage["strategy"]
    queue_path = stage["queue_path"]
    if not queue_path.is_file():
        print(f"队列文件不存在：{queue_path}", file=sys.stderr)
        return 2
    batches = Path(args.batches) if args.batches else delivery / BATCHES_DIR

    rows_by_doi: dict[str, list[tuple[dict, Path]]] = {}
    receipts: list[Path] = []
    for receipt in iter_receipts(batches):
        rows = load_rows(receipt)
        if rows:
            receipts.append(receipt)
        for row in rows:
            doi = norm_doi(row.get("doi") or row.get("identifier"))
            if doi:
                rows_by_doi.setdefault(doi, []).append((row, receipt))

    fulltext = delivery / FULLTEXT_DIR
    fulltext.mkdir(parents=True, exist_ok=True)
    identity = load_identity(delivery)
    queue = read_queue(queue_path)
    results, recorded, expired, cleared, withheld, records = [], [], [], set(), {}, []
    for doi in queue:
        rows = rows_by_doi.pop(doi, [])
        plain = [row for row, _ in rows]
        winner, locator, best_rank = None, None, 0
        for row, _receipt in rows:
            if not row.get("success"):
                continue
            candidate = resolve_locator(str(row.get("file", "")), delivery, batches)
            rank = 0 if candidate is None else (1 if is_grey(str(row.get("source", ""))) else 2)
            if rank > best_rank:
                winner, locator, best_rank = row, candidate, rank
        source = str(winner.get("source", "")) if winner else "none"
        grey_hit = is_grey(source)
        adopted = winner is not None and (not grey_hit or caliber != "pre-0006-oa_first")
        failure_class = "" if adopted else classify(plain)
        reason = ""
        if not adopted:
            if not plain:
                reason = "本轮未取得任何来源的全文（无上游收据；待接机构后重试）"
            else:
                detail = next((short(r.get("reason") or r.get("error")) for r in plain if r.get("reason") or r.get("error")), "")
                reason = detail or ("no PDF found" if failure_class == "no_source" else failure_class)
            if grey_hit:
                reason = f"灰色来源命中，不采用（口径 {caliber}）"
        file_rel = ""
        if adopted and locator is not None:
            target = fulltext / f"{doi_slug(doi)}.pdf"
            if locator.resolve() != target.resolve():
                try:
                    shutil.copyfile(locator, target)
                except OSError as exc:
                    print(f"搬运失败 {doi}：{exc}", file=sys.stderr)
                    adopted, reason = False, f"搬运失败：{short(str(exc))}"
            file_rel = relative_to_delivery(target, delivery) if adopted else ""
        if adopted:
            cleared.add(doi)
        elif (failure_class == "no_source" and is_doi_key(doi)
              and not any(r.get("success") for r in plain)):
            stamp = min((p.stat().st_mtime for _r, p in rows), default=now.timestamp())
            failed_at = min(datetime.fromtimestamp(stamp).astimezone(), now)
            entry = {"doi": doi, "error_class": "no_source", "caliber": caliber, "strategy": strategy,
                     "failed_at": failed_at.isoformat(), "reason": reason or "no PDF found",
                     "receipt": relative_to_delivery(rows[0][1], delivery) if rows else ""}
            if now - failed_at > timedelta(days=TTL_DAYS):
                expired.append(entry)
            else:
                recorded.append(entry)
                records.append({"at": now.isoformat(), "action": "remember", "doi": doi,
                                "error_class": "no_source", "failed_at": failed_at.isoformat(),
                                "failed_on": fmt_date(failed_at), "caliber": caliber, "strategy": strategy})
        elif not adopted:
            withheld[failure_class or "unproven"] = withheld.get(failure_class or "unproven", 0) + 1
        results.append({
            "identifier": doi,
            "doi": doi,
            "success": adopted,
            "tool_success": any(row.get("success") for row in plain),
            "adopted": adopted,
            "source": source if winner else "none",
            "reason": reason,
            "file": file_rel,
            "grey_hit": grey_hit,
            "error_class": failure_class,
            "identity_check": identity.get(doi, ""),
        })

    unqueued = sorted(rows_by_doi)
    payload = {
        "queue": relative_to_delivery(queue_path, delivery),
        "total": len(queue),
        "succeeded": sum(1 for e in results if e["success"]),
        "adopted": sum(1 for e in results if e["adopted"]),
        "grey_hits": sum(1 for e in results if e["grey_hit"]),
        "batches_read": [rel_posix(p) for p in receipts],
        "unqueued_receipts": unqueued,
        "entries": results,
    }
    (delivery / RESULT_NAME).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    memory_dois = {norm_doi(e.get("doi")) for e in stage["entries"]}
    cleared_hits = sorted(doi for doi in cleared if doi in memory_dois)
    if recorded or cleared or stage["memory_path"].is_file():
        write_memory(stage["memory_path"], merge_memory(stage["entries"], recorded, cleared, now))
    append_log(delivery / LOG_NAME, records + [{"at": now.isoformat(), "action": "cleared", "doi": doi}
                                               for doi in cleared_hits])

    report_carry(stage["carried"])
    print(f"队列 {len(queue)} 条 → 采用 {payload['adopted']}／未采用 {len(queue) - payload['adopted']}"
          f"（灰色命中 {payload['grey_hits']}｜口径 {caliber}）")
    print(f"收据：{RESULT_NAME}；全文目录：{FULLTEXT_DIR}（按返回的文件定位字段搬运，与 --output 目录无关）")
    if recorded:
        print(f"失败记忆写入 {len(recorded)} 条（no_source）：")
        for entry in recorded[:20]:
            print(f"  {entry['doi']}｜原始失败 {fmt_date(parse_time(entry['failed_at']))}｜{short(entry['reason'], 60)}")
    else:
        print("失败记忆写入 0 条")
    if cleared_hits:
        print(f"失败记忆清账 {len(cleared_hits)} 条（件已到手，否定条目作废）")
    if expired:
        print(f"过期未记 {len(expired)} 条（原始失败已超 14 天，窗口外允许重试，不留否定条目）")
    if withheld:
        detail = "、".join(f"{kind} {count}" for kind, count in sorted(withheld.items()))
        print(f"不入失败记忆 {sum(withheld.values())} 条（{detail}）——非全通道穷尽型确定性失败，永不写负面缓存")
    if unqueued:
        print(f"队列外收据 {len(unqueued)} 条未收：{'、'.join(unqueued[:5])}{'…' if len(unqueued) > 5 else ''}")
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    stage = load_stage(args)
    delivery, now, caliber, strategy = stage["delivery"], stage["now"], stage["caliber"], stage["strategy"]

    targets, done = [], set()
    if args.dois:
        targets = [norm_doi(d) for d in args.dois if norm_doi(d)]
    else:
        collected = delivery / RESULT_NAME
        receipts = ([Path(p) for p in args.receipts]
                    or ([collected] if collected.is_file() else iter_receipts(delivery / BATCHES_DIR)))
        if not receipts:
            print("无上轮收据可依据：重试只走上轮收据或显式 DOI，禁全量重提", file=sys.stderr)
            return 2
        for receipt in receipts:
            for row in load_rows(receipt):
                doi = norm_doi(row.get("doi") or row.get("identifier"))
                if not doi:
                    continue
                if bool(row.get("adopted", row.get("success"))):
                    done.add(doi)
                elif doi not in targets:
                    targets.append(doi)
        # 重试排序依据＝研读选取集件行序（＝S 序；ADR-0032），不是「覆盖位次」；件外条目按显式 DOI 序附后
        order = read_queue(stage["queue_path"]) if stage["queue_path"].is_file() else []
        position = {doi: index for index, doi in enumerate(order)}
        targets = sorted((d for d in targets if d not in done), key=lambda d: position.get(d, len(position)))

    kept, records = prune_dois(targets, stage["entries"], caliber, strategy, args.force, now, "retry")
    out_path = Path(args.out) if args.out else delivery / RETRY_NAME
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(f"{doi}\n" for doi in kept), encoding="utf-8")
    append_log(delivery / LOG_NAME, records)

    report_carry(stage["carried"])
    print(f"重试条目 {len(targets)} 条 → 保留 {len(kept)}／剪 {len(targets) - len(kept)}"
          f"（口径 {caliber}｜strategy {strategy}）")
    print(f"重试队列：{relative_to_delivery(out_path, delivery)}（只含上轮未采用或显式指定条目，无全量重提入口）")
    if strategy != DEFAULT_STRATEGY:
        grey = "；灰导向策略逐条竞速，不与默认车道混用" if strategy in GREY_ORIENTED else ""
        print(f"显式 strategy {strategy}：上游 batch 无 strategy 位，逐条走单篇（天然 miss，免清缓存）{grey}：")
        for doi in kept:
            print(f"  {single_command(doi, strategy)}")
        print("确需清理才动三件套：cache_clear＋删错文件＋清索引条目（错文错源恢复优先本路径）。")
    elif kept:
        print("续跑命令（默认车道）：")
        print(f"  {batch_command(relative_to_delivery(out_path, delivery), f'{BATCHES_DIR}/out')}")
    return 0


def cmd_probe_guard(args: argparse.Namespace) -> int:
    delivery = Path(args.delivery)
    now = parse_time(args.now) if args.now else datetime.now().astimezone()
    path = delivery / PROBE_NAME
    state = read_json(path)
    state = state if isinstance(state, dict) else {"probes": {}}
    probes = state.setdefault("probes", {})
    previous = probes.get(args.kind)
    same_session = bool(previous) and str(previous.get("at", ""))[:10] == fmt_date(now)
    if same_session and not args.reset:
        print(f"SKIP 渠道状态探针 kind={args.kind}（本会话已于 {previous['at']} 探过；每会话最多一次）")
        return 0
    probes[args.kind] = {"at": now.isoformat(), "reset": bool(previous and args.reset)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    action = "probe" if not previous else "probe-reset"
    append_log(delivery / LOG_NAME, [{"at": now.isoformat(), "action": action, "kind": args.kind}])
    if same_session and args.reset:
        note = f"（登录后显式放行，本会话前次 {previous['at']}）"
    elif previous:
        note = f"（前次 {previous['at']} 属上一次会话，本会话重新探）"
    else:
        note = ""
    print(f"PROBE 渠道状态探针 kind={args.kind}{note}——本次可探一次；来源健康交由上游自适应，repo 侧不建健康缓存。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Step3 下载队列层：失败记忆与调用侧纪律")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(node: argparse.ArgumentParser) -> None:
        node.add_argument("--delivery", required=True, help="交付目录（workspace/YYYY-MM-DD-<slug>）")
        node.add_argument("--queue", help=f"队列文件（默认 <delivery>/{QUEUE_NAME}）")
        node.add_argument("--memory", help=f"失败记忆（默认 <delivery>/{MEMORY_NAME}）")
        node.add_argument("--carry", action="append", default=[], help="在先交付的失败记忆（只读，可重复）")
        node.add_argument("--caliber", help=f"本次交付口径（默认读 agent/manifest.json，缺省 {DEFAULT_CALIBER}）")
        node.add_argument("--strategy", default=DEFAULT_STRATEGY, help=f"下载 strategy（默认 {DEFAULT_STRATEGY}）")
        node.add_argument("--now", help="时钟钩子（YYYY-MM-DD 或 ISO 8601；探针用）")

    plan = sub.add_parser("plan", help="按失败记忆剪队列")
    common(plan)
    plan.add_argument("--force", action="store_true", help="显式重抓：无条件绕过失败记忆")
    plan.set_defaults(func=cmd_plan)

    collect = sub.add_parser("collect", help="搬运全文并记失败记忆")
    common(collect)
    collect.add_argument("--batches", help=f"上游收据目录（默认 <delivery>/{BATCHES_DIR}）")
    collect.set_defaults(func=cmd_collect)

    retry = sub.add_parser("retry", help="从上轮收据或显式 DOI 造重试队列")
    common(retry)
    retry.add_argument("--receipts", action="append", default=[], help="上轮收据（可重复；默认用 run 收据）")
    retry.add_argument("--dois", action="append", default=[], help="显式条目（错文错源恢复用，可重复）")
    retry.add_argument("--force", action="store_true", help="显式重抓：无条件绕过失败记忆")
    retry.add_argument("--out", help=f"重试队列落点（默认 <delivery>/{RETRY_NAME}）")
    retry.set_defaults(func=cmd_retry)

    guard = sub.add_parser("probe-guard", help="渠道状态每会话最多一次")
    guard.add_argument("--delivery", required=True, help="交付目录")
    guard.add_argument("--kind", default="channel_status", help="探针种类（默认 channel_status）")
    guard.add_argument("--reset", action="store_true", help="登录后显式放行一次")
    guard.add_argument("--now", help="时钟钩子（探针用）")
    guard.set_defaults(func=cmd_probe_guard)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (OSError, KeyError, ValueError) as exc:
        print(f"Step3 队列层错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
