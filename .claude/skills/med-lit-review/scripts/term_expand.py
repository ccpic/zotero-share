"""术语词表发现侧工具：按四分关系做方向化扩展、记录候选（只检出不合并）。

When: Step1 关键词表扩展（同义与上下位双向扩展并记方向，易混永不自动扩展）；
      生长循环检出候选（--append 落候选队列，绝不改真源）；按需人读取表（--dump）。
Do: uv run python scripts/term_expand.py "<查询词>" [--format text|json] [--log]
    uv run python scripts/term_expand.py "<候选词>" --append --source "Step1/日志行 12"
    uv run python scripts/term_expand.py --promote "<候选词>" [--dry-run]
Why: 方向是判据——上级→下级与下级→上级落不同的检索式与日志；易混词一旦自动扩展就会污染检索面。
退出码：0 = 正常；2 = 用法错误或显式给出的 `--lexicon` 缺失／坏格式（失败闭合，不静默用错表）。
      未传 `--lexicon` 而默认表缺失时不算错：按无表姿态继续（不扩展、不猜关系）；`--fallback` 强制走无表姿态。
边界：只读词表与只写候选队列，不发网络请求、不碰 Zotero 库；不判语义，词表外的词一律不猜。
缓存：词表解析结果按「词表文件 sha256 ＋ 解析器版本」跨进程复用（skill 包内 .cache/lexicon/，内容寻址、无 TTL）；
      `--no-cache` 显式绕过（不读不写）、`--cache-dir` 改缓存位；候选队列是写路径，永不进缓存键也不被缓存。
机器契约：status ∈ expanded／hit_no_expansion（命中无边）／miss（未命中）；expanded[] 每项含 term／relation／direction／source 四字段；notes[] 记回退、归一与易混排除说明。
规则与生长循环见 references/protocols/term-lexicon-protocol.md；机器唯一真源 references/lexicon/term-lexicon.yaml（只消费不写），候选队列 references/lexicon/term-lexicon.candidates.yaml（--append／晋升只写它）。
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
DEFAULT_LEXICON = os.path.join(SKILL_DIR, "references", "lexicon", "term-lexicon.yaml")
DEFAULT_BUFFER = os.path.join(SKILL_DIR, "references", "lexicon", "term-lexicon.candidates.yaml")

# 词表解析缓存（O-11）：跨进程复用解析结果，键＝词表文件 sha256 ＋ 解析器版本。
# 内容寻址、无 TTL——键项任一变即 miss（PRD 故事 23）；候选队列是写路径，永不进键也不被缓存（N-09）。
LEXICON_PARSER_VERSION = "lexicon-parse-v2"  # 解析或结构校验语义变即换值（键项之一）；v2＝relations 允许空序列（无边术语）
CACHE_SCHEMA = "lexicon-parse-cache-v1"  # 缓存格的文件格式版本：布局变才换值（独立于解析器版本）
CACHE_SCOPE = "lexicon"
DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache"  # skill 包内忽略位（workspace-guide §15）

LIST_ITEM_RE = re.compile(r"^-\s*(.*)$")
PUNCT_RE = re.compile(r"[\s!-/:-@\[-`{-~，。；：、“”‘’（）【】《》？！、—…·]+")

# 关系数据只有真源一份（ADR-0022 修订）：代码不留内置副本；无表即不扩展、不猜任何关系。

CANDIDATE_HEADER = (
    "# 术语候选队列：文件即队列——在队＝待晋升，任务内真源冻结，晋升一次成型并出账（条目移出）。\n"
    "# 字段：term／family／canonical（非本形时）／english／edges／source／version_candidate／no_edge（显式无边）。\n"
    "# 记录方式：uv run python scripts/term_expand.py \"<候选词>\" --append --source \"<任务/日志行>\"\n"
    "# 规则见 references/protocols/term-lexicon-protocol.md。\n"
)


def parse_scalar(raw):
    """解析 YAML 子集标量：引号原样取值，裸值去行尾注释后见布尔/空序列，其余为字符串。"""
    s = raw.strip()
    if not s:
        return ""
    if s[0] in ("'", '"'):
        quote, value, i = s[0], "", 1
        while i < len(s):
            if s[i] == quote:
                if quote == "'" and i + 1 < len(s) and s[i + 1] == quote:  # '' 转义为单引号
                    value += quote
                    i += 2
                    continue
                tail = re.sub(r"\s+#.*$", "", s[i + 1:]).strip()  # 闭引号后只允许注释
                if tail:
                    raise ValueError(f"引号闭合后有多余内容：{s}")
                return value
            value += s[i]
            i += 1
        raise ValueError(f"引号未闭合：{s}")
    s = re.sub(r"\s+#.*$", "", s).strip()
    if s == "[]":
        return []
    if s.startswith("[") and s.endswith("]"):
        body = s[1:-1].strip()
        if not body:
            return []
        parts, buf, quote = [], "", ""
        for ch in body:
            if quote:
                if ch == quote:
                    quote = ""
                else:
                    buf += ch
            elif ch in ("'", '"'):
                quote = ch
            elif ch == ",":
                parts.append(buf.strip())
                buf = ""
            else:
                buf += ch
        parts.append(buf.strip())
        return parts
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    return s


def role_relation(role):
    """同义/正字变体两类的标记 → relation 取值（relation 与 direction 分开，便于下游按类分流）。"""
    return "正字变体" if role == "正字归一" else "同义"


def tokenize(lines):
    """去掉空行与注释，返回 [(绝对缩进, 内容)]（绝对缩进便于子块对齐重建）。"""
    items = []
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        width = len(raw) - len(raw.lstrip(" "))
        items.append((width, raw.strip()))
    return items


def parse_node(tokens, i, indent):
    """解析一个块节点（映射或序列）；indent 为该块首行的缩进。"""
    if i >= len(tokens):
        return None, i
    if LIST_ITEM_RE.match(tokens[i][1]):
        return parse_sequence(tokens, i, indent)
    return parse_mapping(tokens, i, indent)


def _child_indent(tokens, i, indent):
    """子块缩进：序列可与所属键同缩进（YAML 常规写法），其余子块须严格更深。"""
    if i >= len(tokens):
        return None
    if tokens[i][0] == indent and LIST_ITEM_RE.match(tokens[i][1]):
        return indent
    if tokens[i][0] > indent:
        return tokens[i][0]
    return None


def parse_sequence(tokens, i, indent):
    """块序列：`- 值` 与 `- 键: 值`（含同项续行）。续行按等量平移重建，保持一致网格。"""
    out = []
    while i < len(tokens):
        cur, text = tokens[i]
        if cur != indent or not LIST_ITEM_RE.match(text):
            break
        rest = LIST_ITEM_RE.match(text).group(1).strip()
        if not rest:
            i += 1
            child = _child_indent(tokens, i, indent)
            if child is None:
                out.append("")
                continue
            value, i = parse_node(tokens, i, child)
            out.append(value)
            continue
        if ":" not in rest:
            out.append(parse_scalar(rest))
            i += 1
            continue
        head = cur + 2  # 内容列位（`- ` 之后），也是本项键列的基准
        sub, i = [(head, rest)], i + 1
        while i < len(tokens) and tokens[i][0] > indent:
            offset = tokens[i][0] - head
            if offset % 2:
                raise ValueError(f"缩进不在 2 的整数倍键列上：{tokens[i][1]}")
            sub.append((head + offset, tokens[i][1]))
            i += 1
        value, _ = parse_mapping(sub, 0, head)
        out.append(value)
    return out, i


def parse_mapping(tokens, i, indent):
    """块映射：`键: 值` 或 `键:` 后接更深缩进的子块（序列可与键同缩进）。"""
    out = {}
    while i < len(tokens):
        cur, text = tokens[i]
        if cur != indent:
            break
        key, sep, tail = text.partition(":")
        if not sep:
            raise ValueError(f"行内容既不是映射也不是序列项：{text}")
        tail = tail.strip()
        i += 1
        if tail:
            out[key.strip()] = parse_scalar(tail)
        else:
            child = _child_indent(tokens, i, indent)
            if child is None:
                out[key.strip()] = ""
                continue
            out[key.strip()], i = parse_node(tokens, i, child)
    return out, i


def parse_document(lines):
    """整篇文本 → 顶层值（用于 YAML 片段；空文档返回 None）。"""
    tokens = tokenize(lines)
    if not tokens:
        return None
    value, _ = parse_node(tokens, 0, tokens[0][0])
    return value


def validate_lexicon_doc(value):
    """词表结构校验：缺 version／terms 形状不对／关系边缺字段即报错。解析与缓存命中两条路共用。"""
    if not isinstance(value, dict):
        raise ValueError("顶层不是映射")
    if not isinstance(value.get("version"), str):
        raise ValueError("缺 version 字符串")
    terms = value.get("terms")
    if not isinstance(terms, list) or not terms:
        raise ValueError("terms 不是非空序列")
    for entry in terms:
        if not isinstance(entry, dict) or not isinstance(entry.get("term"), str) or not entry["term"].strip():
            raise ValueError("terms 项不是含 term 的映射")
        edges = entry.get("relations")
        if not isinstance(edges, list):
            raise ValueError(f"{entry['term']} 缺 relations 序列（须为序列，可为空）")
        for edge in edges:
            if not isinstance(edge, dict) or not isinstance(edge.get("type"), str) \
                    or not isinstance(edge.get("target"), str):
                raise ValueError(f"{entry.get('term')} 的关系边缺 type／target 字符串")
    for group in value.get("synonym_sets") or []:
        if not isinstance(group, dict) or not isinstance(group.get("members"), list):
            raise ValueError("synonym_sets 项不是含 members 的映射")


def load_yaml_subset(path):
    """读词表用的 YAML 子集（块映射／块序列／内联 [a, b] 序列／标量／注释），读入时校验结构，坏表一律报错。

    不经解析缓存：晋升（写真源）与临时片段走这里，缓存只在读侧 `load_lexicon_doc`。候选队列不走这里——
    它读走 `read_buffer`、写走 `append_candidate`，是序列而非词表映射，`validate_lexicon_doc` 不适用于它。
    """
    with open(path, encoding="utf-8") as f:
        text = f.read()
    value = parse_document(text.splitlines())
    validate_lexicon_doc(value)
    return value


def lexicon_cache_key(file_sha256, parser=LEXICON_PARSER_VERSION):
    """缓存键：文件字节 sha256 ＋ 解析器版本（内容寻址；mtime 与体积不进键）。"""
    payload = json.dumps({"parser": parser, "sha256": file_sha256}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def read_lexicon_cache(cache_dir, key, file_sha256, parser=LEXICON_PARSER_VERSION):
    """命中返回解析后文档；缺失＝静默 miss；损坏／键不符／结构不符＝通报后 miss（照常重新解析）。"""
    path = Path(cache_dir) / CACHE_SCOPE / (key + ".json")
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        key_ok = (entry["key"] == key and entry["file_sha256"] == file_sha256
                  and entry["parser"] == parser and entry.get("schema") == CACHE_SCHEMA)
    except FileNotFoundError:
        return None
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as e:
        print(f"WARN lexicon cache malformed: {path}: {e}", file=sys.stderr)
        return None
    doc = entry.get("doc")
    if not key_ok or not isinstance(doc, dict):
        print(f"WARN lexicon cache malformed: {path}: key or value shape mismatch", file=sys.stderr)
        return None
    try:
        validate_lexicon_doc(doc)  # 命中同样过结构校验：坏值不因键对而放行
    except ValueError as e:
        print(f"WARN lexicon cache malformed: {path}: {e}", file=sys.stderr)
        return None
    return doc


def write_lexicon_cache(cache_dir, key, file_sha256, doc, parser=LEXICON_PARSER_VERSION):
    """写解析结果（临时文件＋改名）；缓存故障只通报，不中断查询。内容寻址，故不记时间字段。"""
    path = Path(cache_dir) / CACHE_SCOPE / (key + ".json")
    entry = {"schema": CACHE_SCHEMA, "key": key, "file_sha256": file_sha256,
             "parser": parser, "doc": doc}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_name(path.name + ".tmp")
        staging.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        os.replace(staging, path)
    except OSError as e:
        print(f"WARN lexicon cache write failed: {path}: {e}", file=sys.stderr)


def load_lexicon_doc(path, cache_dir=DEFAULT_CACHE_DIR):
    """读词表真源 → (解析后文档, 来源)；来源 ∈ `cache`／`parse`／`off`（`cache_dir=None`＝不读不写）。

    键＝文件 sha256 ＋ 解析器版本；文件缺失或坏表照常抛出（OSError／ValueError／UnicodeDecodeError），
    由调用方按 CLI 口径处理——显式 `--lexicon` 缺失仍 fail-closed，缓存 miss 不回退任何表。
    """
    raw = Path(path).read_bytes()
    file_sha256 = hashlib.sha256(raw).hexdigest()
    key = lexicon_cache_key(file_sha256)
    if cache_dir is not None:
        hit = read_lexicon_cache(cache_dir, key, file_sha256)
        if hit is not None:
            return hit, "cache"
    value = parse_document(raw.decode("utf-8").splitlines())
    validate_lexicon_doc(value)
    if cache_dir is not None:
        write_lexicon_cache(cache_dir, key, file_sha256, value)
    return value, ("parse" if cache_dir is not None else "off")


def norm(term):
    """去重键归一（沿 09 资产 §1.2）：NFKC → 小写 → 去标点 → 空白归一。不做术语归一。"""
    folded = unicodedata.normalize("NFKC", term).lower()
    return re.sub(r"\s+", " ", PUNCT_RE.sub("", folded)).strip()


class Lexicon:
    """词表视图：本形/别名/缩写 → 主条目，供扩展与归一查询。"""

    def __init__(self, data, fallback=False):
        if not isinstance(data, dict):
            raise ValueError("顶层不是映射")
        self.version = data.get("version") or "未知版本"
        self.fallback = fallback
        self.terms = data.get("terms") or []
        if not isinstance(self.terms, list):
            raise ValueError("terms 不是序列")
        self.synonym_sets = data.get("synonym_sets") or []
        self._graph = None
        self._names = None
        self.by_term = {}
        for entry in self.terms:
            if not isinstance(entry, dict):
                continue
            keys = [entry.get("term")] + list(entry.get("chinese_aliases") or []) \
                + list(entry.get("abbreviations") or [])
            for key in keys:
                if not key:
                    continue
                current = self.by_term.get(key)
                # 同形多条目时以「本形即该形」的条目为准，别名不夺主
                if current is None or (entry.get("term") == key and current.get("term") != key):
                    self.by_term[key] = entry

    def lookup(self, term):
        return self.by_term.get(term)

    def canonical_of(self, term):
        found = self.lookup(term)
        return (found or {}).get("canonical") or term

    def name_map(self):
        """同主名的同义/正字变体成员表（主名 → [(形, 角色)]），缓存一次构建。"""
        if self._names is not None:
            return self._names
        names = {}
        for entry in self.terms:
            if not isinstance(entry, dict):
                continue
            names.setdefault(entry.get("canonical") or entry.get("term"), []).append(
                (entry.get("term"), "同义"))
            for edge in entry.get("relations") or []:
                if not isinstance(edge, dict) or edge.get("type") not in ("synonym", "variant"):
                    continue
                target = edge.get("target")
                if target:
                    names.setdefault(entry.get("canonical") or entry.get("term"), []).append(
                        (target, "正字归一" if edge.get("type") == "variant" else "同义"))
        self._names = names
        return names

    def canonical_graph(self):
        """主名级上下位图：同义与正字变体先折叠到主名，上下位边记方向，可反向走。"""
        if self._graph is not None:
            return self._graph
        graph = {}
        for entry in self.terms:
            if not isinstance(entry, dict):
                continue
            head = self.canonical_of(entry.get("term") or "")
            for edge in entry.get("relations") or []:
                if not isinstance(edge, dict) or edge.get("type") != "hierarchy":
                    continue
                tail = self.canonical_of(edge.get("target") or "")
                if tail == head:
                    continue
                if (edge.get("direction") or "") == "⊃":
                    graph.setdefault(head, (set(), set()))[0].add(tail)
                    graph.setdefault(tail, (set(), set()))[1].add(head)
                else:
                    graph.setdefault(head, (set(), set()))[1].add(tail)
                    graph.setdefault(tail, (set(), set()))[0].add(head)
        self._graph = graph
        return graph

    def expand(self, term, notes):
        """发现侧扩展：从查询词主名出发，主名级上下位双向走 + 同义/正字变体同行；易混只提示不扩。

        每项四字段（term/relation/direction/source）；direction 是「查询词 → 该项」的方向，
        非相邻项注明「（经 中间词）」；主名级折叠使同级词不会互相冒充上下位。
        """
        found = self.lookup(term)
        if found is None:
            notes.append(f"未命中词表：{term}（不猜扩，按未知记日志；新词进候选队列）")
            return []
        canonical = found.get("canonical") or term
        if canonical != found.get("term"):
            notes.append(f"查询词 {found.get('term')} 为主名 {canonical} 的同义／变体形式")
        graph = self.canonical_graph()
        names = self.name_map()
        expanded, confusables, emitted = [], [], set()

        # 一、主名级上下位：先按连通分量收齐可达主名与代数（只走上下位边，不串家族）
        depths, origins, queue = {canonical: 0}, {}, [canonical]
        while queue:
            node = queue.pop(0)
            children, ancestors = graph.get(node, (set(), set()))
            for target in sorted(children | ancestors):
                if target not in depths:
                    depths[target] = depths[node] + 1
                    origins[target] = node
                    queue.append(target)

        # 二、方向以查询词主名为主语：只给查询词主名的直接上级与直接下级定向；
        #     经上级到手的同辈（表亲）标「同级（经 X）」，不虚构包含关系
        anchor = graph.get(canonical, (set(), set()))
        kin = {canonical} | anchor[0] | anchor[1]
        if canonical != term:
            emitted.add(canonical)
            expanded.append({"term": canonical, "relation": "同义", "direction": "同义",
                             "source": "lexicon"})
        for node, depth in sorted(depths.items()):
            if node == canonical or node == term:
                continue
            via = "" if depth < 2 else f"（经 {origins.get(node, canonical)}）"
            if node not in kin:
                direction = "同级"
            elif node in anchor[0]:
                direction = "上级→下级"
            else:
                direction = "下级→上级"
            if node not in emitted:
                emitted.add(node)
                expanded.append({"term": node, "relation": "上下位", "direction": direction + via,
                                 "source": "lexicon"})

        # 三、同义与正字变体：与查询词同主名的其余表形（含变体形）；纯同义的查询词只给主名
        for name, role in names.get(canonical, []):
            if not name or name in (term, canonical) or name in emitted:
                continue
            emitted.add(name)
            expanded.append({"term": name, "relation": role_relation(role), "direction": role,
                             "source": "lexicon"})

        # 四、易混同主名的其余表形只收集不扩展（永不自动扩展）
        for entry in self.terms:
            if not isinstance(entry, dict) or self.canonical_of(entry.get("term") or "") not in depths:
                continue
            for edge in entry.get("relations") or []:
                if isinstance(edge, dict) and edge.get("type") == "confusable":
                    target = edge.get("target")
                    if target and target != term and target not in confusables:
                        confusables.append(target)
        if confusables:
            notes.append("易混词不自动扩展（只作提示）：" + "、".join(confusables))
        return sorted(expanded, key=lambda e: e["term"])

    def normalize(self, term):
        """命中的本形与调用方给的形式是否归一一致（正字变体静默归一用）。"""
        found = self.lookup(term)
        if not found:
            return None
        return norm(term) == norm(found.get("term")), found.get("canonical") or found.get("term")


def empty_lexicon():
    """无表姿态：不扩展、不猜任何关系（关系数据只有真源一份）。"""
    return Lexicon({"version": "无表", "terms": [], "synonym_sets": []}, fallback=True)


EDGE_TYPES = {"synonym": ("=",), "hierarchy": ("⊃", "⊂"), "confusable": ("≠",), "variant": ("≈",)}
SYNONYM_LIKE = ("synonym", "variant")


def parse_edge_spec(spec):
    """`--edge "type:target:direction"` → {type,target,direction}；类型与方向不匹配即报错（用法错误）。"""
    parts = spec.split(":")
    if len(parts) != 3:
        raise ValueError(f"边格式应为 type:target:direction（target 不含冒号）：{spec}")
    etype, target, direction = (p.strip() for p in parts)
    if etype not in EDGE_TYPES:
        raise ValueError(f"未知关系类型 {etype}（可用：{'／'.join(EDGE_TYPES)}）：{spec}")
    if not target:
        raise ValueError(f"边缺目标词：{spec}")
    if direction not in EDGE_TYPES[etype]:
        raise ValueError(f"{etype} 的方向应为 {'／'.join(EDGE_TYPES[etype])}：{spec}")
    return {"type": etype, "target": target, "direction": direction}


def build_parser():
    ap = argparse.ArgumentParser(
        prog="term_expand.py",
        description="术语词表发现侧扩展与候选记录（方向化扩展；易混永不自动扩展；退出码 0 正常／2 用法或词表错误）")
    ap.add_argument("term", nargs="?", help="查询词（本形、别名、缩写皆可）")
    ap.add_argument("--format", choices=("text", "json"), default="text", help="输出格式（默认 text）")
    ap.add_argument("--log", action="store_true", help="只输出一行筛选日志记法")
    ap.add_argument("--append", action="store_true",
                    help="把该词记为候选进候选队列（默认 references/lexicon/term-lexicon.candidates.yaml）")
    ap.add_argument("--family", default=None,
                    help="--append 时显式指定家族（新家族建族用；缺省从真源推断，推断不到记未归类家族）")
    ap.add_argument("--edge", action="append", default=[], metavar="TYPE:TARGET:DIR",
                    help='--append 时的初判边（可重复），如 --edge "synonym:钙通道阻滞剂:="'
                         '、--edge "hierarchy:钙通道阻滞剂:⊂"')
    ap.add_argument("--canonical", default=None,
                    help="--append 时指定主名（同义/变体形用；缺省＝真源主名或本形）")
    ap.add_argument("--english", default=None, metavar="EN",
                    help="--append 时记该条的英文形（确证才填，不猜补，如 dyslipidemia）")
    ap.add_argument("--no-edge", action="store_true",
                    help="--append 时声明本词无边（AFK 下可作提案；晋升计划表带表确认，红线据此豁免）")
    ap.add_argument("--promote", action="append", default=[], metavar="TERM",
                    help="晋升指定候选（可重复）；先配 --dry-run 出计划表，确认后去掉 --dry-run 落盘")
    ap.add_argument("--promote-all-candidates", action="store_true",
                    help="晋升队列里全部候选")
    ap.add_argument("--dry-run", action="store_true", help="只出晋升计划表，不写任何文件")
    ap.add_argument("--dump", nargs="?", const="", default=None, metavar="FAMILY",
                    help="按家族打印词表表（不带家族＝全部；人读取表用）")
    ap.add_argument("--source", default="", help="候选来源（任务名与日志行）")
    ap.add_argument("--buffer", default=DEFAULT_BUFFER, help="候选队列路径（默认 skill 内 references/lexicon/term-lexicon.candidates.yaml）")
    ap.add_argument("--lexicon", default=DEFAULT_LEXICON, help="词表路径（默认 skill 内 references/lexicon/term-lexicon.yaml）")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR),
                    help="词表解析缓存目录（缺省＝skill 包内 .cache/；须为被忽略的非交付路径）")
    ap.add_argument("--no-cache", action="store_true",
                    help="显式绕过词表解析缓存：不读不写（照旧每次解析）")
    ap.add_argument("--fallback", action="store_true", help="强制走无表姿态（演练：不扩展、不猜关系）")
    return ap


def family_peers(lexicon, buffer_path, family, exclude):
    """本族其它术语（真源∪队列，按本形去重）：[(term, 边数, 是否声明无边)]——给入队提示用。"""
    peers = {}
    for t in lexicon.terms:
        if isinstance(t, dict) and t.get("family") == family and t.get("term") != exclude:
            peers[t.get("term")] = (len(t.get("relations") or []), bool(t.get("no_edge")))
    for e in read_buffer(buffer_path):
        if e.get("family") == family and e.get("term") != exclude:
            peers[e.get("term")] = (len(e.get("edges") or []), bool(e.get("no_edge")))
    return sorted(peers.items())


def read_buffer(path):
    """读候选队列（机器 yaml 片段；文件即队列，在队＝待晋升）。"""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        value = parse_document(f.read().splitlines())
    return value if isinstance(value, list) else []


def quote_scalar(value):
    """写回队列时的标量转义：空值、含冒号或井号、首尾空白一律加单引号，保证可被重新解析。"""
    s = "" if value is None else str(value)
    if not s or s != s.strip() or any(c in s for c in ":#'\"") or s[0] in "-?*&!%@`[]{}|>":
        return "'" + s.replace("'", "''") + "'"
    return s


def render_buffer(entries):
    """整写队列：头部注释块 + 逐条候选（字段序即契约序）。"""
    out = [CANDIDATE_HEADER]
    for e in entries:
        out.append(f"- term: {quote_scalar(e.get('term'))}\n")
        out.append(f"  family: {quote_scalar(e.get('family'))}\n")
        out.append(f"  version_candidate: {quote_scalar(e.get('version_candidate'))}\n")
        if e.get("canonical") and e.get("canonical") != e.get("term"):
            out.append(f"  canonical: {quote_scalar(e.get('canonical'))}\n")
        if e.get("english"):
            out.append(f"  english: {quote_scalar(e.get('english'))}\n")
        if e.get("no_edge"):
            out.append("  no_edge: true\n")
        out.append(f"  source: {quote_scalar(e.get('source'))}\n")
        edges = e.get("edges") or []
        if not edges:
            out.append("  edges: []\n")
            continue
        out.append("  edges:\n")
        for edge in edges:
            out.append(f"    - type: {quote_scalar(edge.get('type'))}\n")
            out.append(f"      target: {quote_scalar(edge.get('target'))}\n")
            out.append(f"      direction: {quote_scalar(edge.get('direction'))}\n")
    return "".join(out)


def edge_key(edge):
    return (edge.get("type"), edge.get("target") or "", edge.get("direction") or "")


def merge_edges(old_edges, new_edges):
    """并集合并（幂等：以 term 为键合并时的 edges 合并）。"""
    merged, seen = [], set()
    for edge in list(old_edges or []) + list(new_edges or []):
        key = edge_key(edge)
        if key in seen:
            continue
        seen.add(key)
        merged.append({"type": key[0], "target": key[1], "direction": key[2]})
    return merged


def append_candidate(path, term, args, lexicon, explicit_edges):
    """候选只进候选队列：不碰真源、不触去重判定。

    edges ＝ 真源已有边的镜像（对既有词）＋ `--edge` 初判边（对任何词，含未命中新词）；
    没有边就是没有边——不再写占位；确要无边须 `--no-edge` 显式声明。
    幂等（ADR-0016）：以 term 为键合并——edges 取并集、family／canonical 以新值覆盖、source 追加来源轨迹。
    version_candidate 按边推断（沿 references/protocols/term-lexicon-protocol.md §6.2）：新家族、新关系边、关系改类一律 minor
    （须确认）；只有正字变体与纯正字层级别名写法算 patch（可静默进）；判不准按 minor。
    """
    found = lexicon.by_term.get(term)  # 按本形取条目，不经别名或主名折叠
    in_lexicon = bool(found)
    family = (found or {}).get("family") or "未归类家族"
    if args.family:                      # 显式指定家族（新家族建族用）
        family = args.family
    if not in_lexicon:
        family = family if args.family else "未归类家族"
    known = in_lexicon and family != "未归类家族"
    canonical = args.canonical or (found or {}).get("canonical") or term
    raw = [
        {"type": e.get("type"), "target": e.get("target"), "direction": e.get("direction")}
        for e in ((found or {}).get("relations") or []) if isinstance(e, dict)
    ]

    def already_mirrored(edge):
        """表里已有反向边即视为已覆盖，不重复计新边（同义/变体/易混互为反向，上下位 ⊃ 对 ⊂）。"""
        other = lexicon.by_term.get(edge.get("target") or "")
        if not other:
            return False
        rtype = edge.get("type")
        for m in other.get("relations") or []:
            if not isinstance(m, dict) or m.get("target") != (found or {}).get("term"):
                continue
            if rtype == "hierarchy":
                if m.get("type") == "hierarchy" and (edge.get("direction") or "⊃") != (m.get("direction") or "⊃"):
                    return True
            elif rtype == "confusable":
                return True
            elif m.get("type") in ("synonym", "variant"):
                return True
        return False

    edges = merge_edges([e for e in raw if not already_mirrored(e)], explicit_edges)
    if not in_lexicon:
        kind = "minor"                # 词表外的新词＝改检索面，必须确认
    elif not edges:
        kind = "patch"  # 无新边：只剩写法层／无边声明记录，静默进
    elif all(e["type"] in SYNONYM_LIKE for e in edges):
        kind = "patch"
    else:
        kind = "minor"
    entries = read_buffer(path)
    prev = next((e for e in entries if e.get("term") == term), None)
    if prev is None:
        entry = {
            "term": term, "family": family, "version_candidate": kind,
            "source": args.source or "未记来源", "edges": edges,
        }
        if canonical != term:
            entry["canonical"] = canonical
        if args.english:
            entry["english"] = args.english
        if args.no_edge:
            entry["no_edge"] = True
        entries.append(entry)
        created = not os.path.exists(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(render_buffer(entries))
        return created, entry, False

    # 合并（幂等）：edges 并集、family 覆盖、source 追轨迹
    merged = dict(prev)
    merged["edges"] = merge_edges(prev.get("edges"), edges)
    if known:
        merged["family"] = family
    if canonical != term:
        merged["canonical"] = canonical
    if args.english:
        merged["english"] = args.english
    if args.no_edge:
        merged["no_edge"] = True
    merged["version_candidate"] = prev.get("version_candidate") or kind
    new_src = args.source or "未记来源"
    old_src = str(prev.get("source") or "")
    if new_src and new_src not in old_src.split(" ｜ "):
        merged["source"] = f"{old_src} ｜ {new_src}" if old_src else new_src
    entries[entries.index(prev)] = merged
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_buffer(entries))
    return False, merged, True


RELATION_SYMBOL = {"synonym": "=", "confusable": "≠", "variant": "≈"}


def relation_cell(edges):
    """把边渲染成 MD 关系列（同义 =；上下位按方向 ⊃／⊂；易混 ≠；变体 ≈）。

    同符号的多条边用「、」连，不同符号之间用「；」——与既有手写表风格一致。
    """
    groups = []  # [(symbol, [target, ...])]
    for e in edges or []:
        etype = e.get("type")
        if etype == "hierarchy":
            symbol = e.get("direction") or "⊃"
        else:
            symbol = RELATION_SYMBOL.get(etype, "?")
        target = e.get("target") or ""
        if groups and groups[-1][0] == symbol:
            groups[-1][1].append(target)
        else:
            groups.append((symbol, [target]))
    parts = []
    for symbol, targets in groups:
        parts.append("、".join(f"{symbol} {t}" if t else "—" for t in targets))
    return "；".join(parts) or "—"


def join_aliases(values):
    return "、".join(v for v in (values or []) if v) or "—"


def render_family_table(lexicon_doc, family):
    """从真源渲染某家族的 MD 表（受管区块内容）。"""
    terms = [t for t in lexicon_doc.get("terms") or []
             if isinstance(t, dict) and t.get("family") == family]
    terms.sort(key=lambda t: (str(t.get("canonical") or ""), str(t.get("term") or "")))
    lines = ["| 本形 | 主名 | 中文别名 | 英文 | 缩写 | 关系 |", "|---|---|---|---|---|---|"]
    for t in terms:
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            t.get("term") or "", t.get("canonical") or t.get("term") or "",
            join_aliases(t.get("chinese_aliases")), t.get("english") or "—",
            join_aliases(t.get("abbreviations")), relation_cell(t.get("relations"))))
    return "\n".join(lines) + "\n"


def render_dump(lexicon_doc, family=None):
    """按家族打印词表表（--dump；人读取表用）。"""
    families = sorted({t.get("family") for t in lexicon_doc.get("terms") or []
                       if isinstance(t, dict) and t.get("family")})
    if family:
        families = [f for f in families if f == family]
    if not families:
        return ""
    return "\n".join(f"### {fam}\n\n{render_family_table(lexicon_doc, fam)}" for fam in families)


def synonym_set_id(canonical):
    ascii_letters = "".join(ch for ch in (canonical or "") if ch.isascii() and ch.isalnum())
    return ascii_letters.lower()[:8] or "set"


def yaml_term_block(entry):
    """生成真源里的术语块（缩进与既有风格一致）。"""
    aliases = ", ".join(entry.get("chinese_aliases") or [])
    abbrevs = ", ".join(entry.get("abbreviations") or [])
    lines = [f"  - term: {entry['term']}",
             f"    canonical: {entry['canonical']}",
             f"    chinese_aliases: [{aliases}]",
             f"    english: {entry.get('english') or ''}",
             f"    abbreviations: [{abbrevs}]",
             f"    family: {entry['family']}",
             f"    report: {'true' if entry.get('report', True) else 'false'}",
             f"    rank: {'true' if entry.get('rank', True) else 'false'}",
             f"    source: {entry.get('source') or ''}"]
    if entry.get("no_edge"):
        lines.append("    no_edge: true")
    real_edges = list(entry.get("edges") or [])
    if not real_edges:
        lines.append("    relations: []")
        return "\n".join(lines) + "\n"
    lines.append("    relations:")
    for e in real_edges:
        lines += [f"      - type: {e['type']}",
                  f"        target: {e.get('target') or ''}",
                  f"        direction: \"{e.get('direction') or ''}\""]
    return "\n".join(lines) + "\n"


def entry_delta(entry, truth_entry):
    """队列条目相对真源条目的净新增：有新边、新 no_edge 声明或主名变更即为有增量。

    无增量＝陈旧遗留（可出账）；有增量＝对已有术语的修订（须并入已有块，不可当陈旧丢掉）。
    """
    have = {(x.get("type"), x.get("target") or "", x.get("direction") or "")
            for x in (truth_entry.get("relations") or []) if isinstance(x, dict)}
    want = {(x.get("type"), x.get("target") or "", x.get("direction") or "")
            for x in (entry.get("edges") or []) if isinstance(x, dict)}
    if want - have:
        return True
    if entry.get("no_edge") and not truth_entry.get("no_edge"):
        return True
    canonical = truth_entry.get("canonical") or truth_entry.get("term")
    if entry.get("canonical") and entry["canonical"] != canonical:
        return True
    return False


def merge_existing_block(lex_text, candidate, truth_entry, today):
    """把候选的新边／主名／无边声明并进真源已有术语块（整块重渲染；原字段与来源保留）。

    真源是手写 YAML 子集（无回写器），故按 `  - term: <词>` 定位块边界后整块替换；
    块内注释不保（术语块内本不放注释）。
    """
    term = candidate.get("term")
    edges = merge_edges(
        [{"type": e.get("type"), "target": e.get("target"), "direction": e.get("direction")}
         for e in (truth_entry.get("relations") or []) if isinstance(e, dict)],
        candidate.get("edges") or [])
    add = f"{today} 生长循环晋升（{candidate.get('source') or '未记来源'}）"
    old_source = str(truth_entry.get("source") or "")
    block = yaml_term_block({
        "term": term,
        "canonical": candidate.get("canonical") or truth_entry.get("canonical") or term,
        "chinese_aliases": truth_entry.get("chinese_aliases") or [],
        "english": candidate.get("english") or truth_entry.get("english") or "",
        "abbreviations": truth_entry.get("abbreviations") or [],
        "family": truth_entry.get("family") or candidate.get("family") or "未归类家族",
        "report": truth_entry.get("report", True),
        "rank": truth_entry.get("rank", True),
        "source": f"{old_source}；{add}" if old_source else add,
        "edges": edges,
        "no_edge": bool(candidate.get("no_edge") or truth_entry.get("no_edge")) or None,
    })
    m = re.search(rf"^  - term: {re.escape(str(term))}\s*$", lex_text, re.M)
    if not m:
        raise ValueError(f"真源里找不到术语块：{term}")
    nxt = re.search(r"^  - term: ", lex_text[m.end():], re.M)
    end = m.end() + nxt.start() if nxt else len(lex_text)
    return lex_text[:m.start()] + block + lex_text[end:]


def bump_version(text, kind):
    """minor → vX+1.0；patch → vX.Y+1。"""
    m = re.search(r"^version:\s*lexicon v(\d+)\.(\d+)\s*$", text, re.M)
    if not m:
        raise ValueError("真源缺 version: lexicon vX.Y 行")
    major, minor = int(m.group(1)), int(m.group(2))
    if kind == "minor":
        major, minor = major + 1, 0
    else:
        minor += 1
    new_version = f"lexicon v{major}.{minor}"
    text = re.sub(r"^version:\s*lexicon v\d+\.\d+\s*$", f"version: {new_version}", text, count=1, flags=re.M)
    text = re.sub(r"^frozen_at:.*$", f"frozen_at: {datetime.date.today().isoformat()}",
                  text, count=1, flags=re.M)
    return text, new_version


def promote_plan(candidates):
    """算晋升计划：待写边、家族、类型（minor 优先）、当前／新版本。"""
    kinds = {c.get("version_candidate") or "minor" for c in candidates}
    kind = "minor" if "minor" in kinds else "patch"
    edges_desc, new_sets = [], {}
    for c in candidates:
        term = c.get("term")
        real = list(c.get("edges") or [])
        for e in real:
            edges_desc.append(f"{term} {RELATION_SYMBOL.get(e['type'], e.get('direction') or '?')} {e.get('target')}")
        if any((e.get("type") == "synonym") for e in real):
            canonical = c.get("canonical") or term
            members = sorted({term, *(e.get("target") for e in real if e.get("type") == "synonym")})
            new_sets.setdefault(canonical, set()).update(m for m in members if m)
    return {"kind": kind, "edges_desc": edges_desc,
            "synonym_sets": {k: sorted(v) for k, v in new_sets.items()},
            "count": len(candidates)}


def apply_promotion(lexicon_path, buffer_path, candidates, lexicon_doc, plan):
    """一次成型：真源（术语块／同义集／version 与 frozen_at）＋队列出账。"""
    lex_text = Path(lexicon_path).read_text(encoding="utf-8")
    today = datetime.date.today().isoformat()
    lex_text, new_version = bump_version(lex_text, plan["kind"])

    truth_by_term = {t.get("term"): t for t in lexicon_doc.get("terms") or [] if isinstance(t, dict)}
    merges = [c for c in candidates if c.get("term") in truth_by_term]
    blocks = []
    for c in candidates:
        term = c.get("term")
        if term in truth_by_term:
            continue
        edges = list(c.get("edges") or [])
        blocks.append(yaml_term_block({
            "term": term,
            "canonical": c.get("canonical") or term,
            "chinese_aliases": [],
            "english": c.get("english") or "",
            "abbreviations": [],
            "family": c.get("family") or "未归类家族",
            "source": f"{today} 生长循环晋升（{c.get('source') or '未记来源'}）",
            "edges": edges, "no_edge": c.get("no_edge"),
        }))
    if blocks:
        lex_text = lex_text.rstrip("\n") + "\n" + "".join(blocks)
    for c in merges:
        lex_text = merge_existing_block(lex_text, c, truth_by_term[c["term"]], today)

    if plan["synonym_sets"]:
        set_blocks = []
        for canonical, members in plan["synonym_sets"].items():
            set_blocks.append("\n".join(
                [f"  - id: {synonym_set_id(canonical)}",
                 f"    canonical: {canonical}",
                 "    report: true",
                 f"    note: {canonical} 家族同义成员（生长循环晋升自动建集，id 可按需手调）",
                 "    members:"] + [f"      - {m}" for m in members]) + "\n")
        marker = "\nterms:\n"
        i = lex_text.index(marker) + 1
        lex_text = lex_text[:i] + "".join(set_blocks) + lex_text[i:]

    Path(lexicon_path).write_text(lex_text, encoding="utf-8")
    entries = read_buffer(buffer_path)
    promoted = {c.get("term") for c in candidates}
    Path(buffer_path).write_text(
        render_buffer([e for e in entries if e.get("term") not in promoted]),
        encoding="utf-8")
    return new_version, len(promoted)



def run_promotion(args):
    """晋升：先出计划表，确认后一次成型（ADR-0016／ADR-0022）。

    队列出账：**无增量**的条目（真源已含该词且无新边／新声明）跳过晋升、确认后移出；
    有增量的已有术语按「并入」处理——新边并进原块，不重建、不丢原字段与来源。
    """
    lex_doc = load_yaml_subset(args.lexicon)
    truth_by_term = {t.get("term"): t for t in lex_doc.get("terms") or [] if isinstance(t, dict)}
    all_entries = read_buffer(args.buffer)
    stale = [e for e in all_entries
             if e.get("term") in truth_by_term and not entry_delta(e, truth_by_term[e["term"]])]
    candidates = [e for e in all_entries if e not in stale]

    if args.promote:
        want = set(args.promote)
        have = {e.get("term") for e in candidates}
        missing = sorted(t for t in want if t not in have)
        if missing:
            print(f"晋升失败：队列里没有这些候选（无增量的陈旧条目会被出账，不在此列）：{missing}",
                  file=sys.stderr)
            return 2
        candidates = [e for e in candidates if e.get("term") in want]

    if stale:
        print(f"队列出账：{len(stale)} 条无增量（真源已含），跳过晋升、直接移出 —— "
              + "、".join(str(e.get("term")) for e in stale)
              + ("（dry-run：未写盘）" if args.dry_run else ""))
        if not args.dry_run:
            Path(args.buffer).write_text(
                render_buffer([e for e in all_entries if e not in stale]), encoding="utf-8")
    if not candidates:
        if not stale:
            print("晋升：队列为空（无可晋升候选）")
        return 0

    unclassified = [c.get("term") for c in candidates
                    if (c.get("family") or "未归类家族") == "未归类家族"]
    if unclassified:
        print("晋升失败：候选尚未归类家族——先 `--append --family <家族>` 归类再晋升："
              f"{unclassified}", file=sys.stderr)
        return 2

    plan = promote_plan(candidates)
    lex_text = Path(args.lexicon).read_text(encoding="utf-8")
    _, new_version = bump_version(lex_text, plan["kind"])
    current = re.search(r"^version:\s*(lexicon v\d+\.\d+)", lex_text, re.M)
    current = current.group(1) if current else "?"

    print(f"晋升计划（{'dry-run，不写盘' if args.dry_run else '执行'}）")
    print(f"  条目 {plan['count']} 条 ｜ 类型 {plan['kind']} ｜ {current} → {new_version}")
    for c in candidates:
        edges = "；".join(f"{e.get('type')} {e.get('target') or '—'} {e.get('direction') or ''}".strip()
                          for e in (c.get("edges") or [])) or "（无边：仅收录）"
        mark = "并入" if c.get("term") in truth_by_term else "新增"
        print(f"  - [{mark}] {c.get('term')} ｜ family={c.get('family')} ｜ {edges}"
              + ("｜no_edge（待你确认）" if c.get("no_edge") else ""))
    if plan["synonym_sets"]:
        for canonical, members in plan["synonym_sets"].items():
            print(f"  同义集：{canonical} ← {'、'.join(members)}")
    print(f"  写盘面：真源＋version/frozen_at → {new_version}；队列出账 {plan['count']} 条"
          + (f"（另出账已在真源 {len(stale)} 条）" if stale else ""))
    if args.dry_run:
        print("dry-run 结束：未写任何文件。确认后重跑去掉 --dry-run。")
        return 0

    new_version, dropped = apply_promotion(args.lexicon, args.buffer, candidates, lex_doc, plan)
    print(f"晋升完成：真源 → {new_version} ｜ 队列出账 {dropped} 条"
          + (f"（另出账已在真源 {len(stale)} 条）" if stale else ""))
    return 0


def run_dump(args):
    """按家族打印词表表（--dump；读真源现值）。"""
    cache_dir = None if args.no_cache else Path(args.cache_dir)
    try:
        doc, source = load_lexicon_doc(args.lexicon, cache_dir)
    except OSError as e:
        print(f"词表文件错误：{args.lexicon}（{e.strerror or e}）", file=sys.stderr)
        return 2
    except (ValueError, UnicodeDecodeError) as e:
        print(f"词表文件错误：{args.lexicon} 不是本工具支持的 YAML 子集（{e}）", file=sys.stderr)
        return 2
    print(f"CACHE lexicon source={source} file={os.path.basename(args.lexicon)} "
          f"parser={LEXICON_PARSER_VERSION}", file=sys.stderr)
    print(f"词表：{doc.get('version')} ｜ frozen_at：{doc.get('frozen_at') or '—'}")
    text = render_dump(doc, args.dump or None)
    if not text:
        print(f"未找到家族：{args.dump}", file=sys.stderr)
        return 2
    print(text, end="")
    return 0


def render_text(term, lexicon, expanded, notes, log_line, status="expanded"):
    lines = [f"查询词：{term} ｜ 词表：{lexicon.version}"]
    if not expanded:
        lines.append("扩展：无（命中无边，不扩展）" if status == "hit_no_expansion"
                     else "扩展：无（宁漏勿噪，不猜扩）")
    for e in expanded:
        lines.append(f"{e['term']} ｜ {e['relation']} ｜ {e['direction']} ｜ 来源：{e['source']}")
    for note in notes:
        lines.append(f"说明：{note}")
    lines.append(f"log：{log_line}")
    return "\n".join(lines)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = build_parser()
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if args.promote or args.promote_all_candidates:
        return run_promotion(args)
    if args.dump is not None:
        return run_dump(args)
    if not args.term:
        print("用法错误：给出查询词，或用 --promote／--promote-all-candidates 走晋升",
              file=sys.stderr)
        return 2
    if not args.append and (args.edge or args.canonical or args.english or args.no_edge):
        print("用法错误：--edge／--canonical／--english／--no-edge 仅与 --append 同用", file=sys.stderr)
        return 2

    if args.fallback:
        lexicon, fallback = empty_lexicon(), True
    else:
        cache_dir = None if args.no_cache else Path(args.cache_dir)
        try:
            doc, source = load_lexicon_doc(args.lexicon, cache_dir)
            lexicon, fallback = Lexicon(doc), False
        except OSError as e:
            if args.lexicon != DEFAULT_LEXICON:  # 显式给出的表缺失：失败闭合，不静默用错表
                print(f"词表文件错误：{args.lexicon}（{e.strerror or e}）", file=sys.stderr)
                return 2
            lexicon, fallback = empty_lexicon(), True
        except (ValueError, UnicodeDecodeError) as e:
            if args.lexicon != DEFAULT_LEXICON:
                print(f"词表文件错误：{args.lexicon} 不是本工具支持的 YAML 子集（{e}）", file=sys.stderr)
                return 2
            lexicon, fallback = empty_lexicon(), True
            print(f"词表文件不可用（{e}），按无表姿态继续（不扩展）", file=sys.stderr)
        else:
            print(f"CACHE lexicon source={source} file={os.path.basename(args.lexicon)} "
                  f"parser={LEXICON_PARSER_VERSION}", file=sys.stderr)

    notes = []
    if fallback:
        notes.append("无可用词表：不扩展、不猜关系（宁漏勿噪）")
    hit = lexicon.lookup(args.term)
    expanded = lexicon.expand(args.term, notes)
    if expanded:
        status = "expanded"
    elif hit is not None:
        status = "hit_no_expansion"
        notes.append(f"命中无边：{args.term} 已收录但无可扩展边（不扩展；未命中者才进候选队列）")
    else:
        status = "miss"
        notes.append("未命中时不猜扩，只在候选队列留条目（检出即录）")
    if hit is not None:
        same, canonical = lexicon.normalize(args.term)
        note = "归一一致（静默）" if same else "归一不一致"
        notes.append(f"归一化：norm(\"{args.term}\")={norm(args.term)} ｜ 与主名 {canonical} {note}（去重键只归一不合并）")

    excluded = [n.split("：", 1)[1] for n in notes if n.startswith("易混词不自动扩展（只作提示）：")]
    drawn = "、".join(f"{e['term']}({e['direction']})" for e in expanded) or "无"
    core = (f"扩展 {len(expanded)} 个 ｜ 带出 {drawn}" if status == "expanded"
            else ("命中无边（不扩展）" if status == "hit_no_expansion" else "未命中（不扩展）"))
    log_line = (
        f"词表 {lexicon.version} ｜ {datetime.date.today().isoformat()} ｜ 查询词 {args.term} ｜ "
        + core
        + (f" ｜ 排除易混 {'、'.join(excluded)}" if excluded else "")
    )

    if args.append:
        try:
            explicit = [parse_edge_spec(s) for s in args.edge or []]
        except ValueError as e:
            print(f"用法错误：{e}", file=sys.stderr)
            return 2
        try:
            created, entry, merged = append_candidate(args.buffer, args.term, args, lexicon, explicit)
        except OSError as e:
            print(f"候选队列写入失败：{args.buffer}（{e.strerror or e}）", file=sys.stderr)
            return 2
        verb = "新建" if created else ("合并（幂等）" if merged else "追加")
        print(f"候选已入队（{verb}，未改真源）：{args.buffer} ｜ term={args.term} ｜ "
              f"family={entry.get('family')} ｜ version_candidate={entry.get('version_candidate')} ｜ "
              f"edges={len(entry.get('edges') or [])}"
              + (f" ｜ canonical={entry.get('canonical')}" if entry.get("canonical") else "")
              + (f" ｜ english={entry.get('english')}" if entry.get("english") else "")
              + (" ｜ no_edge=true" if entry.get("no_edge") else ""))
        peers = family_peers(lexicon, args.buffer, entry.get("family"), args.term)
        if peers:
            print("  同族现有：" + "、".join(
                f"{t}（{n} 边）" if n else f"{t}（无边{'，已声明' if ne else ''}）"
                for t, (n, ne) in peers))
        print("  本词与表内术语的关系（同义／上下位／易混／正字）用 --edge \"type:target:方向\" 记初判边；"
              "确无关系用 --no-edge 声明——二者皆无的术语在交付门禁判红")
        return 0

    if args.log:
        print(log_line)
        return 0
    if args.format == "json":
        print(json.dumps({
            "term": args.term,
            "canonical": (hit or {}).get("canonical", args.term),
            "version": lexicon.version,
            "status": status,
            "expanded": expanded,
            "notes": notes,
            "log": log_line,
        }, ensure_ascii=False, indent=2))
        return 0
    print(render_text(args.term, lexicon, expanded, notes, log_line, status))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
