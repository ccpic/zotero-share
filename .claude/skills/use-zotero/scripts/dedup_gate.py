# -*- coding: utf-8 -*-
"""入库预检核心（ADR-0020）：DOI 权威的快速查重门，三态输出 DUP／DUP-GAP／NEW。

When: 需在取元数据与写库（或下载）之前，最快判定文献是否已在库时。
Do:   `PreflightGate(z, len(dois))` 后逐条 `gate.check(doi)` → `(verdict, key)`：
      · 逐条探针（<10 篇）或全库 DOI 索引（≥10 篇、库版本探针验鲜）二选一；
      · 命中正式条目后查正文附件：有 → `DUP`（重复取消）；无 → `DUP-GAP`（转挂载链）；
      · 未命中 → `NEW`。
Why:  先取元数据再查重会白付网络与多次检索（原路径 ≈8.8s/篇，实测）；本模块是
      `seed_from_doi.py` 与 `check_duplicates.py` 的门槛语义单源——探针顺序、正文附件
      口径、索引阈值与降级规则只在这里定义，不许分叉。
契约（ADR-0020）：探针 ≤3s；含正文附件核的完整判定 ≤5s；一切异常向上抛，调用方
      fail-closed（中止报错，不得当未命中）；`SKIP` 不在本模块产出。
性能（本机 666 条实测）：单次本地检索 ≈2.1s 且与结果大小无关；全库索引一次 ≈19s。
"""
import re
import time

DOI_PREFIX_RE = re.compile(r"^https?://(dx\.)?doi\.org/", re.I)


def norm_doi(text):
    """归一 DOI：小写、去前缀（与各脚本同式；本模块为预检门槛单源）。"""
    return DOI_PREFIX_RE.sub("", (text or "").strip()).lower()


def search_hits(z, query, modes=("titleCreatorYear", "everything"), limit=25):
    """多模式检索并集（按 key 去重）；无 qmode 的旧版 pyzotero 退回默认检索。

    DOI 探针只需 everything（DOI 字段仅在该模式被索引）；标题探针两模式并集——
    单用 everything 会因排序与 limit 截断漏掉完全匹配的条目（实测长标题场景）。
    """
    out, seen = [], set()
    for mode in modes:
        try:
            hits = z.items(q=query, qmode=mode, limit=limit)
        except TypeError:  # 旧版 pyzotero 无 qmode 参数
            hits = z.items(q=query, limit=limit)
        for x in hits:
            key = (x.get("data") or {}).get("key") or id(x)
            if key not in seen:
                seen.add(key)
                out.append(x)
    return out


def doi_probes(doi):
    """DOI 权威探针串：归一整串 + 后缀片段（大小写由 API 检索天然不敏感）。"""
    d = norm_doi(doi)
    probes = [d] if d else []
    tail = d.split("/", 1)[1] if "/" in d else ""
    if tail and tail != d:
        probes.append(tail)
    return probes


def find_by_doi(z, title, doi):
    """慢路径复核：DOI 探针（整串+后缀）+ 标题探针；命中集内只比对条目 DOI 字段。

    只服务未命中预检后的建库路径：补后缀/标题探针并收集快照（迁移用）；命中即停
    （formal 已找到时不再跑后续探针——调用方将取消建库，快照清单不再需要）。
    返回 (formal_hit, snapshot_hits)。
    """
    want = norm_doi(doi)
    formal, snapshots, seen = None, [], set()

    def consume(hits):
        nonlocal formal
        for x in hits:
            dd = x.get("data", {})
            key = dd.get("key") or id(x)
            if key in seen:
                continue
            seen.add(key)
            if not want or norm_doi(dd.get("DOI") or "") != want:
                continue
            if dd.get("itemType") == "journalArticle":
                formal = x
            else:
                snapshots.append(x)

    for probe in doi_probes(doi):
        if formal is not None:
            break
        consume(search_hits(z, probe, modes=("everything",)))
    if title and formal is None:
        consume(search_hits(z, title))
    return formal, snapshots


def has_pdf_child(z, key):
    """正文附件判定（CONTEXT.md）：children 含 stored PDF（imported_file／imported_url）；
    linked_url 书签、网页快照与笔记不算。"""
    start = 0
    while True:
        kids = z.children(key, limit=100, start=start)
        if not kids:
            return False
        for k in kids:
            d = k.get("data", {})
            if (d.get("itemType") == "attachment"
                    and d.get("contentType") == "application/pdf"
                    and d.get("linkMode") in ("imported_file", "imported_url")):
                return True
        if len(kids) < 100:
            return False
        start += 100


class PreflightGate:
    """入库预检门（ADR-0020）：逐条探针或全库索引，三态输出。

    索引模式（批次 ≥ INDEX_THRESHOLD）构建前后各取一次库版本探针；构建期间库被写
    则重建一次，再变即抛（fail-closed）。版本探针不可得（无响应头等）时降级逐条
    探针——宁慢勿陈（不拿未经验鲜的索引判 NEW）。
    """

    INDEX_THRESHOLD = 10

    def __init__(self, z, batch_size):
        self.z = z
        self.mode = "index" if batch_size >= self.INDEX_THRESHOLD else "probe"
        self.note = ""
        self.scanned = 0
        self.build_s = 0.0
        self._index = None
        if self.mode == "index":
            self._build_index()

    def _version(self):
        try:
            return self.z.last_modified_version()
        except Exception:
            return None

    def _scan(self):
        items, start = [], 0
        while True:
            page = self.z.items(limit=100, start=start)
            if not page:
                return items
            items.extend(page)
            start += 100

    def _build_index(self):
        t0 = time.monotonic()
        v1 = self._version()
        if v1 is None:
            self.mode = "probe"
            self.note = "version probe unavailable; degraded to per-item probes"
            return
        items = self._scan()
        v2 = self._version()
        if v2 != v1:
            items = self._scan()
            if self._version() != v2:
                raise RuntimeError("library changed during index build twice; retry preflight")
        self.scanned = len(items)
        index = {}
        for it in items:
            d = it.get("data", {})
            doi = norm_doi(d.get("DOI") or "")
            if doi and d.get("key"):
                index.setdefault(doi, []).append((d["key"], d.get("itemType")))
        self._index = index
        self.build_s = time.monotonic() - t0

    def check(self, doi):
        """命中判定：返回 (verdict, key)；verdict ∈ DUP／DUP-GAP／NEW。异常向上抛。"""
        want = norm_doi(doi)
        if not want:
            raise ValueError(f"empty DOI: {doi!r}")
        if self.mode == "index" and self._index is not None:
            formals = sorted(k for k, t in self._index.get(want, []) if t == "journalArticle")
        else:
            hits = search_hits(self.z, want, modes=("everything",))
            formals = sorted(
                x["data"]["key"] for x in hits
                if x.get("data", {}).get("itemType") == "journalArticle"
                and norm_doi(x["data"].get("DOI") or "") == want)
        if not formals:
            return "NEW", None
        key = formals[0]  # 同 DOI 多条正式条目时取稳定序首个（库内已存在的重复不属本门处理）
        return ("DUP" if has_pdf_child(self.z, key) else "DUP-GAP"), key
