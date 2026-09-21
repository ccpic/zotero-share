"""scansci-pdf 下载的 PDF 挂到 Zotero 正式条目（子附件上传）。

When: 文献 PDF 已落盘（如 scansci-pdf 缓存目录），需作为 stored file 子附件挂到对应正式条目。
Do: python attach_pdfs.py --map attach_map.json [--dry-run]
Why: upload_attachments 对键顺序敏感（linkMode 先于 filename/contentType），且 filename 须为 basedir 相对路径；统一走脚本避免 400。
key 经参数或 ZOTERO_LOCAL_API_KEY 环境变量传入；无绝对路径与密钥落盘。
attach_map.json 格式：
  {"basedir": "<PDF 目录>",
   "items": [{"parent": "<条目key>", "file": "<相对文件名>", "title": "<附件标题>"}]}
退出码 0=全部挂上且回查 children 非空；1=有失败。
只读核验：--dry-run 只检查父条目存在、文件可读、是否已有同名子附件，不写库。
缓存只省读、不省核验——挂载写路径、既有条目补挂与回查口径不变（ADR-0007）：
- children 列表按父条目键单次运行内 memo（同一相位内同父只取一次；上传后对应键失效、回查必
  重取）——进程内、写操作即失效、跨跑永 miss（§14.2）。
- 身份核验结论按全键（方法＋归一 DOI＋字节 sha256＋父条目键＋父条目版本＋期望值题名／作者／
  卷期）复用「通过」：全等才跳核验（不重开 PDF），任一键项变即重跑；False/None 拦截态永不入
  缓存、永不作为通过复用（不挂／转人工照旧）；缺 PyMuPDF 仍 fail-closed（缓存命中同样要求依赖
  在位）。缓存位＝skill 包内 .cache/（--cache-dir 须为被忽略的非交付路径；--no-cache 显式绕过、
  不读不写）；键即正确性、无 TTL、跨运行复用。留痕：每行 `identity=… src=cache|verify|
  unreadable checked=<原核验时刻>`，收尾一行 SUMMARY 记 children 读数与缓存命中数。
"""
import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

from pyzotero import zotero as zmod

API_CHILDREN = "http://127.0.0.1:23119/api/users/0/items/{parent}/children?limit=50"
VERIFY_METHOD = "attach-identity-v1"  # 方法版本：挂前验身份判据变即换值（全键之一）
CACHE_SCOPE = "attach-identity"
DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache"  # skill 包内忽略位
DOI_PREFIX_RE = re.compile(r"^https?://(dx\.)?doi\.org/", re.I)


def norm_doi(text):
    """归一 DOI：小写、去前缀（§14 口径，本包独立实现、跨包不 import）。"""
    return DOI_PREFIX_RE.sub("", (text or "").strip()).lower()


def load_plan(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fitz_module():
    """加载 PyMuPDF；缺依赖即抛 ImportError（调用方 fail-closed，缓存命中同样要求依赖在位）。"""
    try:
        import fitz
    except ImportError:
        import pymupdf as fitz  # 缺库即失败，不静默跳过
    return fitz


def hash_file(path, chunk=1 << 20):
    """文件 sha256（流式）；mtime 与体积不作校验证据（§14.1）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def read_children(parent):
    """读父条目 children（本地 API）；失败即抛出，由调用方按其既有口径处理。"""
    with urllib.request.urlopen(API_CHILDREN.format(parent=parent), timeout=15) as r:
        return json.load(r)


class ChildrenMemo:
    """children 列表：按父条目键单次运行内 memo；上传（含写失败）后对应键失效重取。"""

    def __init__(self):
        self.memo = {}
        self.reads = 0

    def get(self, parent):
        if parent not in self.memo:
            self.memo[parent] = read_children(parent)  # 抛出即未入 memo：失败读数不缓存
            self.reads += 1
        return self.memo[parent]

    def invalidate(self, parent):
        self.memo.pop(parent, None)


class VerdictStore:
    """身份核验结论缓存：全键等复用「通过」，只存 True；False/None 永不入缓存也不作通过复用。

    键＝方法版本＋归一 DOI＋字节 sha256＋父条目键＋父条目版本＋期望值（题名／作者／卷期）；
    键即正确性、无 TTL，跨运行复用（skill 包内 .cache/ 忽略位，§14.2 脚本自管缓存位）。
    条目自身缺项的 pin 按同侧值进键：缺项不单独产出「通过」（核验判据三项至少两项命中），
    故缓存中的「通过」必有 ≥2 组 pin 命中，同字节同 pin 重跑即同判决。
    """

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir  # None＝显式绕过（--no-cache）：不读不写
        self.lookups = 0
        self.hits = 0

    @property
    def enabled(self):
        return self.cache_dir is not None

    def verdict(self, path, parent, parent_version, expect):
        """全键等复用「通过」（不重开 PDF）；否则本地重跑核验、只把「通过」写进缓存。

        缺 PyMuPDF 抛 ImportError：缓存命中同样要求依赖在位（fail-closed，由调用方转为失败）。
        """
        fitz_module()
        key = None
        if self.enabled:  # --no-cache 时不读不写、也不多算哈希
            key = self._key(hash_file(path), parent, parent_version, expect)
            self.lookups += 1
            hit = self._read(key)
            if hit is not None:
                self.hits += 1
                return {"value": True, "source": "cache", "checked_at": hit[1]}
        checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
        value = verify_pdf_identity(path, expect["title"], expect["authors"],
                                    expect["volume"], expect["pages"])
        if value is True and key is not None:  # 只缓存「通过」；False/None 永不作为通过复用
            self._write(key, checked_at)
        return {"value": value, "source": "verify", "checked_at": checked_at}

    def _key(self, sha256, parent, parent_version, expect):
        return json.dumps({"method": VERIFY_METHOD, "sha256": sha256, "parent": parent,
                           "parent_version": parent_version, "expect": expect},
                          ensure_ascii=False, sort_keys=True)

    def _read(self, key):
        """命中返回 (True, 原核验时刻)；缺失＝静默 miss；损坏／键不符／非「通过」＝通报后 miss。"""
        path = self._path(key)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            key_ok = entry["key"] == key
            verdict = entry["verdict"]
            checked_at = entry["checked_at"]
        except FileNotFoundError:
            return None
        except (AttributeError, KeyError, OSError, TypeError, ValueError) as e:
            print(f"WARN verdict cache malformed: {path}: {e}", file=sys.stderr)
            return None
        if not key_ok:
            print(f"WARN verdict cache malformed: {path}: key mismatch", file=sys.stderr)
            return None
        if verdict is not True:  # 拦截态（False/None）永不作「通过」复用
            print(f"WARN verdict cache malformed: {path}: non-pass verdict", file=sys.stderr)
            return None
        return True, checked_at

    def _write(self, key, checked_at):
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            staging = path.with_name(path.name + ".tmp")
            staging.write_text(
                json.dumps({"key": key, "verdict": True, "checked_at": checked_at},
                           ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            staging.replace(path)
        except OSError as e:  # 缓存故障只通报，不中断挂载
            print(f"WARN verdict cache write failed: {path}: {e}", file=sys.stderr)

    def _path(self, key):
        return (Path(self.cache_dir) / CACHE_SCOPE
                / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"))


def verify_pdf_identity(path, title, authors=(), volume=None, pages=None):
    """抽正文核对 PDF 与目标条目为同一篇（见 attachments.md“挂前验身份”）。

    标题分词多数命中 + 作者姓 + 卷期页，三项至少两项命中才算通过；
    全文无文本层（总字符 < 200）返回 None 转人工（先于页数门，以免真扫描文被判 False）；
    页数 <=3 直接 False；
    缺 PyMuPDF 时抛 ImportError 由调用方转为失败。
    """
    fitz = fitz_module()
    d = fitz.open(path)
    full_early = chr(10).join((pg.get_text() or '') for pg in d)
    if len(''.join(full_early.split())) < 200:
        return None  # 扫描版/无文本层，转人工（先于页数门）
    if d.page_count <= 3:
        return False
    full = full_early
    norm = lambda s: ''.join(s.lower().split())
    text = norm(full)
    hits = 0
    stop = {'the', 'of', 'in', 'and', 'a', 'an', 'for', 'with', 'to', 'on',
            'from', 'by', 'vs', 'versus', 'et', 'al'}
    words = [w.strip('.,;:()[]') for w in (title or '').split()]
    words = [w for w in words if len(w) > 2 and w.lower() not in stop]
    if words:
        need = len(words) if len(words) <= 3 else max(3, int(len(words) * 0.6))
        if sum(1 for w in words if norm(w) in text) >= need:
            hits += 1
    last_names = [a.split()[-1] for a in authors if a.split()]
    if last_names and sum(1 for n in last_names if norm(n) in text) >= min(2, len(last_names)):
        hits += 1
    if volume and str(volume) in full and pages and pages.split('-')[0].strip() in full:
        hits += 1
    return hits >= 2


def main() -> int:
    ap = argparse.ArgumentParser(description="PDF 挂 Zotero 正式条目子附件")
    ap.add_argument("--map", required=True, help="挂载计划 JSON 路径")
    ap.add_argument("--library-id", default="0", help="本地库固定传 0")
    ap.add_argument("--api-key", default=os.environ.get("ZOTERO_LOCAL_API_KEY"),
                    help="本地写 key，默认读 ZOTERO_LOCAL_API_KEY")
    ap.add_argument("--dry-run", action="store_true", help="只检查计划，不写库")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR),
                    help="身份核验结论缓存目录（缺省＝skill 包内 .cache/；须为被忽略的非交付路径）")
    ap.add_argument("--no-cache", action="store_true",
                    help="显式绕过核验结论缓存：不读不写、本趟逐条重跑核验（children memo 属进程内，照用）")
    args = ap.parse_args()

    memo = ChildrenMemo()
    verdicts = VerdictStore(None if args.no_cache else Path(args.cache_dir))
    code = attach(args, memo, verdicts)
    print(f"SUMMARY children_reads={memo.reads} verdict_lookups={verdicts.lookups} "
          f"verdict_cache_hits={verdicts.hits}")
    return code


def attach(args, memo, verdicts) -> int:
    plan = load_plan(args.map)
    basedir = plan["basedir"]
    entries = plan["items"]

    z = zmod.Zotero(library_id=args.library_id, library_type="user", local=True,
                    local_api_key=args.api_key)
    ok = True
    for e in entries:
        parent, fname = e["parent"], e["file"]
        title = e.get("title") or fname
        try:
            item = z.item(parent)
            p = item["data"]
        except Exception as ex:
            ok = False
            print(f"FAIL parent {parent} read: {type(ex).__name__}")
            continue
        fpath = os.path.join(basedir, fname)
        readable = os.path.isfile(fpath)
        try:
            children = memo.get(parent)
        except Exception as ex:
            children = []
            print(f"WARN {parent} children read failed: {ex}")
        dup = [c for c in children
               if (c.get("data", {}).get("filename") == fname
                   or (c.get("data", {}).get("title") or "") == title)]
        expect = {"doi": norm_doi(p.get("DOI")), "title": p.get("title") or "",
                  "authors": [c.get("lastName", "") for c in p.get("creators", [])],
                  "volume": p.get("volume"), "pages": p.get("pages")}
        parent_version = p.get("version") if p.get("version") is not None else item.get("version")
        if not readable:
            verdict = {"value": False, "source": "unreadable", "checked_at": None}
        else:
            try:
                verdict = verdicts.verdict(fpath, parent, parent_version, expect)
            except ImportError:
                print('FAIL pymupdf missing: uv run --with pymupdf ...; 校验无法运行，不挂库')
                return 1
        mark = "PLAN" if args.dry_run else "DO  "
        print(f"{mark} attach {fname} -> {parent} "
              f"{(p.get('title') or '')[:45]} file_ok={readable} dup={len(dup)} "
              f"identity={verdict['value']} src={verdict['source']} "
              f"checked={verdict['checked_at'] or '—'}")
        if not readable:
            ok = False
        e['_identity'] = verdict["value"]
    if args.dry_run or not ok:
        return 0 if ok else 1

    blocked = [e for e in entries if e.get('_identity') is False]
    unknown = [e for e in entries if e.get('_identity') is None]
    if blocked:
        for e in blocked:
            print(f"FAIL identity {e['file']} 与父条目不是同一篇，先换 strategy 重下，不挂库")
    if unknown:
        for e in unknown:
            print(f"HOLD unverified {e['file']} 无文本层或无法验证，转人工复核后再挂")
    if blocked or unknown:
        return 1
    for e in entries:
        parent, fname = e["parent"], e["file"]
        title = e.get("title") or fname
        # 键顺序：itemType/linkMode 在前，filename/contentType 在后
        att = {"itemType": "attachment", "linkMode": "imported_file",
               "title": title, "filename": fname,
               "contentType": "application/pdf"}
        try:
            z.upload_attachments([att], parentid=parent, basedir=basedir)
        finally:
            memo.invalidate(parent)  # 上传（含写失败）后该父条目 children 失效，回查必重取
        print(f"OK   uploaded {fname} -> {parent}")

    bad = 0
    for e in entries:
        parent, fname = e["parent"], e["file"]
        children = memo.get(parent)
        hit = any(c.get("data", {}).get("filename") in (fname, fname.split("/")[-1])
                  for c in children)
        print(f"{'OK  ' if hit else 'FAIL'} verify {parent} children={len(children)} want={fname}")
        bad += 0 if hit else 1
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
