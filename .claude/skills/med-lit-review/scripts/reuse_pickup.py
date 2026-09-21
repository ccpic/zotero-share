# -*- coding: utf-8 -*-
"""Step3 取件链：跨交付同 DOI 原文复用（形态 B：在先交付被忽略件指针）。

动词：

  pick   对消费方交付的待取条目逐条走五步取件链——按归一 DOI 定位指针 → 源件 sha256
         全等校验 → 物化复制进 `run/fulltext/` → 副本 sha256 复验 → 身份核验（全键等
         复用「通过」，任一不等转本地重跑）。命中写 `agent/manifest.json` 的复用指针段与
         再生条目；任一环失败＝ miss，落常规下载链路（不改写研读选取集（队列）、不补写旧交付、
         不自动改指针，错件与隔离件不入链）。`--dry-run` 只定位与报告、零写；
         `--force` 无条件绕过既有指针重新定位（动作进收据）；`--doi` 可对显式条目跑
         （擦除再生演练与再生命令用同一入口）。

口径句（ADR-0032）：待取集合＝研读选取集（队列）＝入库集＝研读对象——成员一次选定、三处同用；件＝
`agent/download-queue.txt`（消费方 `agent/`、源侧按 basename 先新后旧），命中＝该成员在先交付已有可复用件；
本文件各处「队列」一律读作研读选取集。

规则真源：`references/protocols/workspace-guide.md` §14（形态 B、失效与正确性规则、
指针字段、台账复用行编码）；做法与产出物位置：`references/protocols/step3-download-chain.md`。

纪律：旧交付严格只读；不建中心目录、不建跨 topic 索引（只在消费方交付内读写）；
指针、收据与台账格只写 basename 与相对路径（绝对路径零命中）。

期望值（`--expect`，可选）＝消费方声明，形如
  {"<归一 DOI>": {"title": "…", "authors": ["…"], "volume": "…", "pages": "…", "method": "…"}}
题名／作者／卷期 pins 与方法与在先交付身份收据逐项相等才复用原「通过」；任一缺项或
不等即转本地重跑（不猜、不传递 False／None）。

miss 原因码：no_candidate（在先交付无该 DOI 已采用件）｜artifact_missing（登记了件而
件不在场）｜source_missing（指针的来源交付缺失）｜pointer_invalid（指针字段缺项）｜
identity_not_pass（在先判决为拦截态）｜blocked_artifact（路径落在隔离件、监管留档或抽取文本位，不入链）｜
hash_mismatch_source（源件哈希与指针不符）｜
hash_mismatch_copy（副本复验不符，残副本已清除）｜materialize_failed（复制失败）｜
target_conflict（消费方同名件字节不同，不覆盖）｜pointer_conflict（--force 定位到与既有指针
不同的字节，不自动改指针）。

退出码：0 正常；2 用法或文件错误。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

_SCRIPTS_HOME = Path(__file__).resolve().parent
if str(_SCRIPTS_HOME) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_HOME))

from step3_queue import DEFAULT_CALIBER, doi_slug, norm_doi, parse_time, read_json, read_queue, rel_posix  # noqa: E402

AGENT_DIR = "agent"  # 新形态真源与账目件的归档目录（workspace-guide §6）
# 下三件按 basename 记：源侧经 delivery_file() 拼 AGENT_DIR 后先新后旧（历史包在交付根），消费方恒 `agent/`。
QUEUE_NAME = "download-queue.txt"
FULLTEXT_DIR = "run/fulltext"
MANIFEST_NAME = "manifest.json"
LEDGER_NAME = "bucketing-and-sources.md"
IDENTITY_NAME = "run/fulltext-identity.json"
RECEIPT_NAME = "run/reuse-receipt.json"
REGEN_CLASS = "fulltext_reuse"
PICKUP_SCRIPT = ".claude/skills/med-lit-review/scripts/reuse_pickup.py"

LEDGER_COLUMNS = ("doi", "bucket", "expected_source", "source", "adopted",
                  "reject_reason", "grey_hit", "identity", "file", "caliber")
PIN_FIELDS = ("title", "authors", "volume", "pages")
PASS_VERDICTS = {"match", "pass", "true", "yes", "ok", "hit", "是", "通过"}
NON_PASS_VERDICTS = {"mismatch", "false", "no", "fail", "reject", "否", "未通过"}
BLANK_CELLS = {"", "—", "-", "–", "无", "n/a", "na", "none"}
BLOCKED_PARTS = {"quarantine", "b2", "text"}  # 禁复用位：隔离件、监管留档、抽取文本（§14.3）


def hash_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def remove_quietly(path: Path) -> None:
    """清残件：删不掉也不吞掉主流程的失败语义（miss 照落，收据照写）。"""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        print(f"提示：残件未清（{path.name}：{exc}），请人工核对", file=sys.stderr)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def delivery_rel(delivery: Path) -> str:
    """交付目录的仓库根相对路径；不在仓库内时退当前工作目录相对路径，再退 basename。"""
    resolved = delivery.resolve()
    for root in (repo_root(), Path.cwd()):
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError:
            continue
    return resolved.name


def blank(value) -> bool:
    return " ".join(str(value if value is not None else "").split()).lower() in BLANK_CELLS


def verdict_segment(cell) -> str:
    """短串 `verdict｜checked_at｜方法` 的判决段（也认空串与单段读数）。"""
    return str(cell if cell is not None else "").split("｜")[0].split("|")[0].strip().lower()


def verdict_is_pass(cell) -> bool:
    return verdict_segment(cell) in PASS_VERDICTS


def norm_text(value) -> str:
    return " ".join(str(value if value is not None else "").split()).casefold()


def norm_authors(value) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [part for part in value.replace("；", ";").replace("，", ",").split(";")]
    elif isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        items = []
    cleaned = [" ".join(part.split()) for part in items if part.strip()]
    return tuple(sorted(item.casefold() for item in cleaned))


def norm_pin(name: str, value):
    return norm_authors(value) if name == "authors" else norm_text(value)


def delivery_file(delivery: Path, rel: str) -> Path:
    """交付内件定位（只用于**读在先交付**）：新形态在 `agent/` 直下，冻结历史包仍在交付根——先新后旧。"""
    nested = delivery / AGENT_DIR / rel
    return nested if nested.is_file() else delivery / rel


def is_delivery(path: Path) -> bool:
    """目录是否为一个交付包：清单或台账任一在位即算（两种形态都认，用于在先交付扫描）。"""
    return any(delivery_file(path, name).is_file() for name in (MANIFEST_NAME, LEDGER_NAME))


def load_manifest(delivery: Path) -> dict:
    """读在先交付的清单（源侧解析：新形态在 `agent/`、冻结历史包在交付根）。

    消费方清单不走本函数——消费者恒为新交付，其清单读写一律 `agent/`（见 `cmd_pick`）。
    """
    manifest = read_json(delivery_file(delivery, MANIFEST_NAME))
    return manifest if isinstance(manifest, dict) else {}


def render_manifest(raw: bytes, manifest: dict) -> bytes:
    """按原件换行／缩进风格重排（CRLF 与缩进宽度照旧），不把全文件重格式化。"""
    text = raw.decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    suffix = "\n" if text.endswith(("\n", "\r")) else ""
    payload = json.dumps(manifest, ensure_ascii=False, indent=indentation_unit(text))
    return (payload + suffix).replace("\n", newline).encode("utf-8")


def indentation_unit(text: str) -> str:
    for line in text.splitlines()[1:]:
        if line[:1] in (" ", "\t"):
            return line[: len(line) - len(line.lstrip())]
    return "  "


def matching_bracket(text: str, start: int) -> int:
    """`start` 处开括号的配对闭合符位置（跳过字符串与转义）。结构不闭合即报错。"""
    opener, closer = text[start], {"[": "]", "{": "}"}[text[start]]
    depth, index = 0, start
    while index < len(text):
        char = text[index]
        if char == '"':
            index += 1
            while index < len(text) and text[index] != '"':
                index += 2 if text[index] == "\\" else 1
            index += 1
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise ValueError("清单 JSON 结构不闭合")


def splice_member(text: str, opener: int, closer: int, block: str, unit: str,
                  newline: str = "\n") -> str:
    """在容器闭合符前追加成员／元素块：沿用原缩进、换行与标点，只增不改。"""
    line_start = text.rfind("\n", 0, opener) + 1
    prefix = text[line_start:opener]
    indent = prefix[: len(prefix) - len(prefix.lstrip(" \t"))]
    element_indent = indent + unit
    last = closer - 1
    while last >= 0 and text[last] in " \t\r\n":
        last -= 1
    indented = block.replace("\n", newline + element_indent)
    if text[last] in "[{":  # 空容器：不补逗号
        if "\n" not in text[last + 1:closer]:  # 同行空容器（如 `[]`）：自造换行与缩进
            return (f"{text[:last + 1]}{newline}{element_indent}{indented}"
                    f"{newline}{indent}{text[last + 1:]}")
        return f"{text[:last + 1]}{newline}{element_indent}{indented}{text[last + 1:]}"
    return f"{text[:last + 1]},{newline}{element_indent}{indented}{text[last + 1:]}"


def write_manifest(path: Path, raw: bytes, manifest: dict) -> bool:
    """写清单：优先最小差异拼接（只追加复用指针段与再生条目），无法拼接才整体重排并告警。

    返回是否发生写入；拼接结果必须能被 json 解析回同一份数据，否则退重排（不让清单损坏）。
    """
    text = raw.decode("utf-8")
    unit = indentation_unit(text)
    newline = "\r\n" if "\r\n" in text else "\n"
    if raw and "\n" in text:
        try:
            spliced = text
            pointers = manifest.get("reuse_pointers") or {}
            existing = json.loads(text).get("reuse_pointers") or {}
            fresh = {key: value for key, value in pointers.items() if key not in existing}
            for key in pointers:
                if key in existing and existing[key] != pointers[key]:
                    print(f"提示：既有指针与本次读数不同（{key}），按「指针不自动改」保留原件",
                          file=sys.stderr)
            if fresh:  # 只追加新 DOI：键已在位时进旧对象，不整段重挂（避免重复键）
                block = ",\n".join(f"{json.dumps(key, ensure_ascii=False)}: "
                                   f"{json.dumps(value, ensure_ascii=False, indent=unit)}"
                                   for key, value in fresh.items())
                marker = spliced.find('"reuse_pointers"')
                if marker >= 0:
                    opener = min(index for index in (spliced.find("{", marker),
                                                     spliced.find("[", marker)) if index >= 0)
                    spliced = splice_member(spliced, opener, matching_bracket(spliced, opener),
                                            block, unit, newline)
                else:
                    block = json.dumps(fresh, ensure_ascii=False, indent=unit)
                    spliced = splice_member(spliced, spliced.find("{"), spliced.rfind("}"),
                                            f'"reuse_pointers": {block}', unit, newline)
            generated = json.loads(spliced)
            entries = manifest.get("regeneration") or []
            missing = [entry for entry in entries if entry not in (generated.get("regeneration") or [])]
            if missing:
                marker = spliced.find('"regeneration"')
                if marker >= 0:
                    opener = spliced.index("[", marker)
                    closer = matching_bracket(spliced, opener)
                    block = ",\n".join(json.dumps(entry, ensure_ascii=False, indent=unit) for entry in missing)
                    spliced = splice_member(spliced, opener, closer, block, unit, newline)
            if json.loads(spliced) == manifest:
                payload = spliced.encode("utf-8")
                if payload != raw:
                    path.write_bytes(payload)
                return payload != raw
        except (ValueError, KeyError, IndexError) as exc:
            print(f"提示：清单无法按最小差异拼接（{exc}），改为整体重排写入", file=sys.stderr)
    payload = render_manifest(raw, manifest)
    if payload != raw:
        path.write_bytes(payload)
    return payload != raw


def caliber_of(manifest: dict, override: str | None) -> str:
    if override:
        return override
    caliber = (manifest.get("caliber") or {}).get("grey_gate_version")
    return str(caliber) if caliber else DEFAULT_CALIBER


def scan_sources(consumer: Path, extra: list[str]) -> list[Path]:
    """在先交付清单＝消费方同级目录（`ls workspace/` 即全局视图）＋显式 `--sources`；不建索引。"""
    found: dict[str, Path] = {}
    roots = [consumer.resolve().parent] + [Path(item).resolve() for item in extra]
    for root in roots:
        if is_delivery(root):
            entries = [root]
        else:
            entries = sorted(root.iterdir()) if root.is_dir() else []
        for entry in entries:
            if not entry.is_dir() or entry.resolve() == consumer.resolve():
                continue
            if is_delivery(entry):
                found.setdefault(entry.name, entry)
    return [found[name] for name in sorted(found)]


def load_ledger_row(delivery: Path, doi: str) -> dict | None:
    """下载台账按 DOI 取行（十列）；只认表格行，不整读厚件。"""
    path = delivery_file(delivery, LEDGER_NAME)
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < len(LEDGER_COLUMNS) or set(cells[0]) <= set("-: "):
            continue
        if norm_doi(cells[0]) == doi:
            return dict(zip(LEDGER_COLUMNS, cells[: len(LEDGER_COLUMNS)]))
    return None


def blocked_relative(rel: str) -> bool:
    """隔离件、监管留档（`b2/`）与抽取文本（`run/text/`）永不进复用链（workspace-guide §14.3）。"""
    return bool(BLOCKED_PARTS & set(Path(rel).parts))


def locate_artifact(delivery: Path, name: str) -> Path | None:
    """按 basename 在 `run/` 内找件；禁复用位（隔离件、监管留档、抽取文本）不入链。"""
    root = delivery / "run"
    if not name or not root.is_dir():
        return None
    for path in root.rglob("*"):
        if path.is_file() and path.name == name and not blocked_relative(path.relative_to(delivery).as_posix()):
            return path
    return None


def index_retrieved_at(delivery: Path, doi: str) -> str | None:
    """上游目录索引里的采集时刻（`.doi_index.json` 的 `ts`）；无索引即 None。"""
    root = delivery / "run"
    if not root.is_dir():
        return None
    for path in sorted(root.rglob(".doi_index.json")):
        data = read_json(path)
        entry = data.get(doi) if isinstance(data, dict) else None
        stamp = entry.get("ts") if isinstance(entry, dict) else None
        try:
            return datetime.fromtimestamp(float(stamp)).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            continue
    return None


def identity_readings(delivery: Path, doi: str, file_name: str) -> dict:
    """在先交付的身份核验收据：结构化期望值（pins）＋方法＋原核验时刻；缺件即无键。"""
    data = read_json(delivery / IDENTITY_NAME)
    rows = data.get("entries") if isinstance(data, dict) else None
    entry = None
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("doi") and norm_doi(row.get("doi")) == doi:
                entry = row
                break  # 只认归一 DOI 全等：同名 basename 不构成同一篇（§14.1 四项键之一）
    if not isinstance(entry, dict):
        return {"entry": None, "pins": {}, "method": None, "checked_at": None,
                "verdict": None, "sha256": None}
    pins = {name: entry.get(name) for name in PIN_FIELDS}
    recorded = entry.get("pins")
    if isinstance(recorded, dict):
        for name in PIN_FIELDS:
            if not blank(recorded.get(name)):
                pins[name] = recorded[name]
    return {
        "entry": entry,
        "pins": {name: value for name, value in pins.items() if not blank(value)},
        "method": data.get("method"),
        "checked_at": data.get("checked_at") or entry.get("checked_at"),
        "verdict": entry.get("verdict"),
        "sha256": entry.get("sha256"),
    }


def identity_short_string(reading, structured: dict) -> str:
    """台账身份核验短串：原读数已带 `｜` 即原样沿用；否则由原收据装配 `verdict｜checked_at｜方法`。"""
    text = str(reading if reading is not None else "")
    if "｜" in text or "|" in text:
        return text
    verdict = verdict_segment(text) or verdict_segment(structured.get("verdict"))
    parts = [verdict, structured.get("checked_at"), structured.get("method")]
    if not any(not blank(part) for part in parts):
        return text or "—"
    return "｜".join(str(part) if not blank(part) else "—" for part in parts)


def candidate_from_source(source: Path, doi: str) -> dict | None:
    """在一个在先交付内定位候选：优先其清单复用指针（带 sha256），退其台账已采用行。

    返回 None＝该交付与这个 DOI 无关；`blocked` 非空＝有登记但进不了链
    （`artifact_missing`／`identity_not_pass`），`artifact` 为 None 亦按悬空处理。
    """
    manifest = load_manifest(source)
    manifest_caliber = (manifest.get("caliber") or {}).get("grey_gate_version")
    pointers = manifest.get("reuse_pointers")
    pointer = pointers.get(doi) if isinstance(pointers, dict) else None
    if isinstance(pointer, dict) and not blank(pointer.get("path")):
        rel = str(pointer.get("reused_into") or pointer.get("path"))
        artifact = source / rel
        candidate = {
            "source_delivery": source.name,
            "kind": "pointer",
            "artifact": artifact if artifact.is_file() else None,
            "sha256": str(pointer.get("sha256") or "") or None,
            "source": pointer.get("source"),
            "grey_hit": pointer.get("grey_hit"),
            "caliber": pointer.get("caliber") or manifest_caliber,
            "retrieved_at": pointer.get("retrieved_at"),
            "identity": pointer.get("identity"),
            "artifact_name": artifact.name,
            "blocked": "",
        }
        if blocked_relative(rel):
            candidate["blocked"] = "blocked_artifact"
        elif verdict_segment(pointer.get("identity")) in NON_PASS_VERDICTS:
            candidate["blocked"] = "identity_not_pass"
        return candidate
    row = load_ledger_row(source, doi)
    if row is None or blank(row["file"]) or not blank(row["reject_reason"]):
        return None
    artifact = locate_artifact(source, row["file"])
    candidate = {
        "source_delivery": source.name,
        "kind": "ledger",
        "artifact": artifact,
        "sha256": None,
        "source": row["source"],
        "grey_hit": row["grey_hit"],
        "caliber": row["caliber"] or manifest_caliber,
        "retrieved_at": index_retrieved_at(source, doi) or (manifest.get("topic") or {}).get("delivered"),
        "identity": row["identity"],
        "artifact_name": artifact.name if artifact else row["file"],
        "blocked": "",
    }
    readings = identity_readings(source, doi, candidate["artifact_name"])
    if (verdict_segment(row["identity"]) in NON_PASS_VERDICTS
            or verdict_segment((readings["entry"] or {}).get("verdict")) in NON_PASS_VERDICTS):
        candidate["blocked"] = "identity_not_pass"
    return candidate


def decide_identity(expect: dict | None, readings: dict, sha256: str) -> dict:
    """身份核验跑跳：归一 DOI＋字节 sha256＋期望值 pins＋方法四项全等才复用「通过」。"""
    declared = {name: (expect or {}).get(name) for name in PIN_FIELDS}
    declared = {name: value for name, value in declared.items() if not blank(value)}
    recorded = readings["pins"]
    missing = sorted(name for name in set(declared) | set(recorded) if name not in declared or name not in recorded)
    mismatch = sorted(name for name in declared
                      if name in recorded and norm_pin(name, declared[name]) != norm_pin(name, recorded[name]))
    source_verdict = (readings["entry"] or {}).get("verdict")
    method_declared = norm_text((expect or {}).get("method"))
    method_recorded = norm_text(readings["method"])
    reasons = []
    if readings["entry"] is None:
        reasons.append("在先交付无身份核验收据条目")
    if not verdict_is_pass(source_verdict):
        reasons.append(f"在先判决非「通过」（{verdict_segment(source_verdict)}）")
    if expect is None:
        reasons.append("消费方未声明期望值（--expect 缺省）")
    else:
        if missing:
            reasons.append("期望值缺项：" + "、".join(missing))
        if mismatch:
            reasons.append("期望值不等：" + "、".join(mismatch))
        if not method_declared or not method_recorded:
            reasons.append("方法缺项")
        elif method_declared != method_recorded:
            reasons.append("方法不等")
    if readings.get("sha256") and readings["sha256"] != sha256:
        reasons.append("收据字节哈希不等")
    return {
        "mode": "rerun" if reasons else "reuse",
        "reason": "；".join(reasons),
        "verdict": source_verdict if not reasons else None,
        "checked_at": readings["checked_at"],
        "method": readings["method"],
        "compared": {"declared": sorted(declared), "recorded": sorted(recorded),
                     "missing": missing, "mismatch": mismatch},
    }


def ledger_cells(doi: str, readings: dict, caliber: str, file_name: str) -> dict:
    """下载台账复用命中行（workspace-guide §14.4）：复用相关六格，其余列本次交付照常填。"""
    adopted = f"是（复用自 {readings['source_delivery']}）"
    original = str(readings.get("caliber") or "")
    if original and original != caliber:
        adopted += f"；原口径 {original}"
    grey = readings.get("grey_hit")
    grey_text = "是" if grey is True else "否" if grey is False else (str(grey) if not blank(grey) else "—")
    return {
        "doi": doi,
        "actual_source": str(readings.get("source") or "—"),
        "adopted": adopted,
        "grey_hit": grey_text,
        "identity": str(readings.get("identity") or "—"),
        "caliber": caliber,
        "file": file_name,
    }


def ledger_row(cells: dict) -> str:
    """可粘贴的十列台账行（桶／预计来源由本次研读选取集（队列）数据补，命中行未采用原因留空）。"""
    return (f"| {cells['doi']} | <桶> | <预计来源> | {cells['actual_source']} | {cells['adopted']} |  | "
            f"{cells['grey_hit']} | {cells['identity']} | {cells['file']} | {cells['caliber']} |")


def materialize(source_file: Path, target: Path, sha256: str) -> tuple[str, str]:
    """物化（复制）＋副本 sha256 复验；副本不符即清残副本。返回 (mode, miss_reason)。"""
    if target.is_file():
        return ("noop", "") if hash_file(target) == sha256 else ("", "target_conflict")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(source_file, target)
    except OSError:
        remove_quietly(target)  # 半截副本必须清掉，否则后续每次取件都判 target_conflict
        return "", "materialize_failed"
    if hash_file(target) != sha256:
        remove_quietly(target)
        return "", "hash_mismatch_copy"
    return "copied", ""


def ensure_regen(manifest: dict, path: str, sha256: str, command: str) -> None:
    """物化件登记进再生命令（定位→源哈希→物化→复验链）；已有同路径条目即不改写。"""
    entries = manifest.setdefault("regeneration", [])
    for entry in entries:
        if isinstance(entry, dict):
            for artifact in entry.get("artifacts") or []:
                if isinstance(artifact, dict) and artifact.get("path") == path:
                    return
    entries.append({"class": REGEN_CLASS, "step": "Step3", "command": command,
                    "artifacts": [{"path": path, "sha256": sha256}]})


def replay_step(pointer: dict, source_dir: Path | None) -> tuple[Path | None, str]:
    """定位既有指针并做源件哈希全等校验；不补写、不改指针。"""
    missing = [field for field in ("source_delivery", "path", "sha256") if blank(pointer.get(field))]
    if missing:
        return None, "pointer_invalid:" + "、".join(missing)
    if source_dir is None:
        return None, "source_missing"
    if blocked_relative(str(pointer["path"])):
        return None, "blocked_artifact"
    if verdict_segment(pointer.get("identity")) in NON_PASS_VERDICTS:
        return None, "identity_not_pass"
    artifact = source_dir / str(pointer["path"])
    if not artifact.is_file():
        return None, "artifact_missing"
    if hash_file(artifact) != str(pointer["sha256"]):
        return None, "hash_mismatch_source"
    return artifact, ""


def pick_one(doi: str, consumer: Path, manifest: dict, sources: list[Path], caliber: str,
             now: datetime, expect: dict | None, force: bool, dry_run: bool,
             source_args: str = "") -> dict:
    """单条取件链：定位 → 源哈希 → 物化 → 副本复验 → 身份核验；返回收据条目并按需登记清单。"""
    pointers = manifest.get("reuse_pointers")
    pointer = pointers.get(doi) if isinstance(pointers, dict) else None
    pointer = pointer if isinstance(pointer, dict) else None
    record = {"doi": doi, "mode": "replay" if pointer and not force else "pickup",
              "result": "miss", "reason": "", "identity": None, "ledger": None,
              "pointer_written": False, "materialized": None, "materialize": ""}
    if record["mode"] == "replay":
        source_dir = next((source for source in sources
                           if source.name == str(pointer.get("source_delivery") or "")), None)
        source_file, reason = replay_step(pointer, source_dir)
        if source_file is None:
            record["reason"] = reason
            return record
        source_sha = str(pointer["sha256"])
        source_path = str(pointer["path"])
        identity_source = source_dir
        readings = {"source_delivery": source_dir.name, "source": pointer.get("source"),
                    "grey_hit": pointer.get("grey_hit"), "caliber": pointer.get("caliber"),
                    "retrieved_at": pointer.get("retrieved_at"), "identity": pointer.get("identity"),
                    "artifact_name": source_file.name}
    else:
        readings, source_file, blocked = None, None, ""
        for source in sources:
            candidate = candidate_from_source(source, doi)
            if candidate is None:
                continue
            if candidate["blocked"] or candidate["artifact"] is None:
                blocked = blocked or candidate["blocked"] or "artifact_missing"
                continue
            computed = hash_file(candidate["artifact"])
            if candidate["sha256"] and computed != candidate["sha256"]:
                blocked = blocked or "hash_mismatch_source"
                continue
            source_file, source_sha = candidate["artifact"], candidate["sha256"] or computed
            source_path = source_file.relative_to(source).as_posix()
            identity_source, readings = source, candidate
            break
        if readings is None:
            record["reason"] = blocked or "no_candidate"
            return record
        if pointer and str(pointer.get("sha256") or "") != source_sha:
            record["reason"] = "pointer_conflict"
            return record

    structured = identity_readings(identity_source, doi, readings.get("artifact_name") or source_file.name)
    readings["identity"] = identity_short_string(readings.get("identity"), structured)
    if expect is None and record["mode"] == "replay":
        identity = {"mode": "carried", "reason": "指针在位：身份决策属首次取件读数，本趟不改",
                    "verdict": None, "checked_at": structured["checked_at"], "method": structured["method"],
                    "compared": None}
    else:
        identity = decide_identity(expect, structured, source_sha)
    target = consumer / FULLTEXT_DIR / f"{doi_slug(doi)}.pdf"
    record.update({"identity": identity, "source_delivery": readings["source_delivery"],
                   "source_path": source_path, "source_sha256": source_sha, "copy_sha256": None,
                   "sha256": source_sha, "materialized": f"{FULLTEXT_DIR}/{target.name}"})

    if dry_run:
        if target.is_file() and hash_file(target) != source_sha:
            record["reason"] = "target_conflict"
            return record
        record.update({"result": "hit", "materialize": "dry-run"})
        record["ledger"] = ledger_cells(doi, readings, caliber, target.name)
        return record

    mode, reason = materialize(source_file, target, source_sha)
    if not mode:
        record["reason"] = reason
        return record
    record.update({"result": "hit", "materialize": mode, "copy_sha256": hash_file(target)})
    record["ledger"] = ledger_cells(doi, readings, caliber, target.name)
    command = (f"uv run python {PICKUP_SCRIPT} pick --delivery {delivery_rel(consumer)}"
               f" --doi {doi}{source_args}")
    if record["mode"] == "pickup" and not pointer:
        manifest.setdefault("reuse_pointers", {})[doi] = {
            "source_delivery": readings["source_delivery"],
            "path": source_path,
            "sha256": source_sha,
            "source": readings.get("source"),
            "grey_hit": None if blank(readings.get("grey_hit")) else readings.get("grey_hit"),
            "caliber": readings.get("caliber") or caliber,
            "retrieved_at": readings.get("retrieved_at"),
            "identity": readings.get("identity"),
            "verified_at": now.isoformat(),
            "reused_into": record["materialized"],
        }
        record["pointer_written"] = True
    ensure_regen(manifest, record["materialized"], source_sha, command)
    return record


def load_expect(path: str | None) -> dict:
    if not path:
        return {}
    data = read_json(Path(path))
    if not isinstance(data, dict):
        raise ValueError(f"期望值文件不可解析：{rel_posix(Path(path))}")
    return {norm_doi(doi): value for doi, value in data.items() if isinstance(value, dict)}


def collect_dois(args: argparse.Namespace, consumer: Path) -> list[str]:
    ordered = args.dois or read_queue(Path(args.queue) if args.queue else consumer / AGENT_DIR / QUEUE_NAME)
    dois: list[str] = []
    for item in ordered:
        doi = norm_doi(item)
        if doi and doi not in dois:
            dois.append(doi)
    return dois


def write_receipt(consumer: Path, payload: dict) -> Path:
    path = consumer / RECEIPT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def cmd_pick(args: argparse.Namespace) -> int:
    consumer = Path(args.delivery)
    manifest_path = consumer / AGENT_DIR / MANIFEST_NAME
    manifest_raw = manifest_path.read_bytes() if manifest_path.is_file() else b""
    manifest = read_json(manifest_path)
    manifest = manifest if isinstance(manifest, dict) else {}
    if not manifest:
        print(f"消费方交付缺 {AGENT_DIR}/{MANIFEST_NAME}：复用指针段无处登记（先有清单再有指针）", file=sys.stderr)
        return 2
    dois = collect_dois(args, consumer)
    if not dois:
        print("无可取条目：研读选取集（队列）为空且未给 --doi", file=sys.stderr)
        return 2
    now = parse_time(args.now) if args.now else datetime.now().astimezone()
    caliber = caliber_of(manifest, args.caliber)
    sources = scan_sources(consumer, args.sources)
    source_args = ""
    for item in args.sources:
        try:
            source_args += " --sources " + Path(item).resolve().relative_to(repo_root()).as_posix()
        except ValueError:
            print(f"提示：显式来源 {rel_posix(Path(item))} 在仓库根之外，再生命令不记该来源"
                  "（复跑需手工补 --sources）", file=sys.stderr)
    expect = load_expect(args.expect)

    records = [pick_one(doi, consumer, manifest, sources, caliber, now, expect.get(doi),
                        args.force, args.dry_run, source_args) for doi in dois]
    hits = [record for record in records if record["result"] == "hit"]
    misses = [record for record in records if record["result"] != "hit"]
    payload = {
        "delivery": delivery_rel(consumer),
        "caliber": caliber,
        "generated_at": now.isoformat(),
        "sources_scanned": [source.name for source in sources],
        "totals": {
            "entered": len(records),
            "hits": len(hits),
            "misses": len(misses),
            "replay": sum(1 for record in records if record["mode"] == "replay"),
            "copied": sum(1 for record in records if record["materialize"] == "copied"),
            "noop": sum(1 for record in records if record["materialize"] == "noop"),
            "identity_reuse": sum(1 for record in records
                                  if record["identity"] and record["identity"]["mode"] == "reuse"),
            "identity_rerun": sum(1 for record in records
                                  if record["identity"] and record["identity"]["mode"] == "rerun"),
        },
        "entries": records,
        "note": "命中写 agent/manifest.json 复用指针段与再生条目；miss 落常规下载链路，研读选取集（队列）不改写、旧交付不补写、指针不自动改。",
    }
    if not args.dry_run:
        write_receipt(consumer, payload)
        write_manifest(manifest_path, manifest_raw, manifest)

    scope = "、".join(source.name for source in sources) or "无"
    print(f"取件链{'（dry-run）' if args.dry_run else ''}：进入 {len(records)} 条 → 命中 {len(hits)}／miss {len(misses)}"
          f"（口径 {caliber}｜扫描在先交付 {len(sources)} 个：{scope}）")
    for record in hits:
        print(f"  命中 {record['doi']} ← {record['source_delivery']}:{record['source_path']}"
              f"（sha256 {record['sha256'][:12]}…；物化 {record['materialize']} → {record['materialized']}"
              f"；身份 {record['identity']['mode']}）")
        print(f"    台账复用行：{ledger_row(record['ledger'])}")
    for record in misses:
        print(f"  miss {record['doi']} → {record['reason']}（落常规下载链路）")
    if not args.dry_run:
        registered = "；复用指针段与再生条目已登记进 agent/manifest.json" if any(
            record.get("pointer_written") for record in records) else ""
        print(f"收据：{RECEIPT_NAME}{registered}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Step3 取件链：跨交付同 DOI 原文复用（形态 B）")
    sub = parser.add_subparsers(dest="command", required=True)

    node = sub.add_parser("pick", help="对研读选取集（队列）逐条走取件链五步")
    node.add_argument("--delivery", required=True, help="消费方交付目录（workspace/YYYY-MM-DD-<slug>）")
    node.add_argument("--queue", help=f"待取研读选取集（队列；默认 <delivery>/{AGENT_DIR}/{QUEUE_NAME}）")
    node.add_argument("--doi", dest="dois", action="append", default=[], help="显式条目（可重复；擦除再生演练用）")
    node.add_argument("--sources", action="append", default=[], help="显式在先交付目录（可重复；默认消费方同级目录）")
    node.add_argument("--expect", help="消费方期望值 JSON（题名／作者／卷期 pins 与方法；缺即转本地重跑）")
    node.add_argument("--caliber", help=f"本次交付口径（默认读 agent/manifest.json，缺省 {DEFAULT_CALIBER}）")
    node.add_argument("--force", action="store_true", help="显式重抓：无条件绕过既有指针重新定位")
    node.add_argument("--dry-run", action="store_true", help="只定位与报告，零写")
    node.add_argument("--now", help="时钟钩子（YYYY-MM-DD 或 ISO 8601；探针与演练用）")
    node.set_defaults(func=cmd_pick)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (OSError, KeyError, ValueError) as exc:
        print(f"取件链错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
