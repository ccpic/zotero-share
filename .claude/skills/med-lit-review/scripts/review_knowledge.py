"""知识包事后确定性校验：15 条规则子集 + 三档级别 + 五字段报告（纯本地文本检查）。

When: Step5 输出八节知识包（含七列表）之后，供人读 text 或供机器断言 json。
Do: uv run python scripts/review_knowledge.py <包.md> [--format text|json] [--cur-year YYYY] [--rules R1-01,...] [--lexicon 词表路径]
Why: 结构、编号、可追溯与占位这类事实机器可断言，避免“篇幅长”冒充过门；语义判断仍交人工。
退出码：0 = 无 error；1 = 有 error；2 = 用法或文件错误（warning / info 不阻断）。
缓存：词表解析结果按「词表文件 sha256 ＋ 解析器版本」跨进程复用（skill 包内 .cache/lexicon/，内容寻址、无 TTL）；
      `--no-cache` 显式绕过（不读不写）、`--cache-dir` 改位；表缺失／不可用按无表处理（T-01 不报），缓存 miss 不回退错表。
边界：只查确定性规则，不判说服力 / 逻辑连贯 / 引用准确性（交人工 + 方法论，沿 R4 自查）。
T-01 术语一致读 references/lexicon/term-lexicon.yaml（唯一真源）：同义家族中文多形混用报 warning、上下位不报、
易混并现报 info、正字变体静默归一；词表缺失或不可解析时按无表处理（T-01 不报，宁漏勿噪）。
规则说明与词表人读版见 references/protocols/review-rules.md；本文件是可执行唯一定义处，两者不一致以本文件为准。
复用来源：报告形态（逐行扫描 → findings → text/json）与边界句式参考 MIT 许可的 text-review 思想，
正则与词表按医学知识包重写。
"""
import argparse
import datetime
import json
import os
import re
import sys

SEVERITIES = ("error", "warning", "info")

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
DEFAULT_LEXICON = os.path.join(SKILL_DIR, "references", "lexicon", "term-lexicon.yaml")
DEFAULT_CACHE_DIR = os.path.join(SKILL_DIR, ".cache")  # 词表解析缓存位（skill 包内忽略位；键与布局由 term_expand 定）
if HERE not in sys.path:
    sys.path.insert(0, HERE)  # term_expand 与本脚本同目录：YAML 子集解析只有一处实现

# 规则表：id → (默认级别, 中文名)。--rules 按 id 过滤；T-01 另有 info 级子项（易混词提示）。
RULES = (
    ("ST-01", "error", "缺八节要素"),
    ("ST-02", "warning", "缺研究问题陈述"),
    ("F-02", "warning", "GB/T 7714 类型标识"),
    ("F-03", "info", "中英标点"),
    ("F-04", "warning", "标题编号"),
    ("R1-01", "warning", "模糊引用与待补占位"),
    ("R1-03", "error", "未来年份"),
    ("R3-01", "warning", "过度断言确定性子集"),
    ("T-01", "warning", "术语一致（中文混用）"),
    ("L-03", "error", "断言后无证据表行"),
    ("L-08", "warning", "强度词与证据级别不匹配"),
    ("L-09", "error", "仅摘要行未进局限声明"),
    ("M-01", "error", "背景区禁数字"),
    ("M-02", "error", "核心段数字可查"),
    ("M-03", "error", "结论四块要素不全"),
)
RULE_ORDER = {rule_id: i for i, (rule_id, _, _) in enumerate(RULES)}
ALL_RULES = frozenset(rule_id for rule_id, _, _ in RULES)

# 八节要素：节名 → 标题同义词（子串匹配，允许同义措辞，不查措辞优劣）。
SECTIONS = {
    "执行摘要": ("执行摘要", "执行概要", "摘要", "概要"),
    "研究问题": ("研究问题",),
    "检索策略": ("检索策略", "检索方案", "检索方法"),
    "主题式证据综合": ("主题式证据综合", "主题式综合", "主题综合", "证据综合"),
    "证据表": ("证据表", "证据汇总"),
    "结论": ("结论",),
    "方法附录": ("方法附录", "方法学附录", "附录"),
    "局限声明": ("局限声明", "局限与未知", "局限"),
}
CLAIM_ROLES = ("执行摘要", "主题式证据综合", "结论")  # 断言区：断言类规则只扫这三节散文

# 研究问题陈述（ST-02，ADR-0012）：§2 内有问句或「研究问题：」标签行即算在；旧形态不兼容（内容已离线迁移）。
QUESTION_MARK_RE = re.compile(r"？")
QUESTION_LABEL_RE = re.compile(r"^[ \t]*(?:[-*·][ \t]*)?(?:系统化)?研究问题[ \t]*[：:][ \t]*\S.{5,}$", re.M)

# 模糊引用词表（长词优先匹配；命中且同句无引文号 / 行指针才报）。
VAGUE_SOURCES = (
    "多项研究表明", "多项研究显示", "有研究表明", "有研究显示", "大量研究表明",
    "若干研究表明", "研究表明", "研究显示", "某研究", "专家认为", "专家表示",
    "普遍认为", "一般认为",
)
VAGUE_RE = re.compile("|".join(re.escape(w) for w in sorted(VAGUE_SOURCES, key=len, reverse=True)))
PLACEHOLDERS = ("[待补充出处]", "[待查证]")

# 全文可及性状态词（ADR-0018／SKILL.md R5）：只认证据表「设计与等级」列的标注，漂到别列即漏判（交人工）。
STATUS_MARK_RE = re.compile(r"仅摘要|仅题录")

# 过度断言绝对词表（R3-01 确定性子集）与强度词表（L-08）。
ABSOLUTE_RE = re.compile("|".join((
    "彻底证明", "彻底证实", "证明", "证实", "治愈", "根治", "首选", "金标准",
    "彻底", "必然", "毫无风险", "绝对安全", "完全消除",
)))
LOW_GRADES = ("E3", "E4", "E5")
STRONG_CLAIM_RE = re.compile(
    r"显著(?:降低|下降|减少|改善|提高|获益|优于|延长)"
    r"|明确(?:降低|下降|减少|改善|提高|获益|优于|支持)"
    r"|(?<!性)证实|(?<!性)证明|确证(?!性)"
)
LOW_GRADE_RE = re.compile(r"\bE[345]\b|E[345]（|配 E[345]")
NEGATION_CHARS = "无未不非否"

# 术语一致（T-01）：真源为 references/lexicon/term-lexicon.yaml（无表即不报，宁漏勿噪），见 lexicon_families()。

# GB/T 7714 类型标识（F-02）。
CITATION_MARK_RE = re.compile(r"\[\d+(?:\s*[-,–]\s*\d+)*\]")
TYPE_ID_RE = re.compile(r"\[(?:EB/OL|J/OL|M/OL|C/OL|DB/OL|J|M|D|C|N|R|S|P|G|Z|DB|CD)\]")
PUBLISHER_CUES = ("出版社", "Press", "编著", "主编", "学位论文")
JOURNAL_CUES = ("DOI", "doi", "学报", "杂志", "期刊", "Journal", "BMJ", "NEJM", "Lancet", "Circulation", "JAMA")

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
PAREN_RE = re.compile(r"[（(][^（()）]*[)）]")
NUMBERED_RE = re.compile(r"^§?\s*(\d+)(?:[.、]\s*|\s+)(?!\d)(.*)$")  # 节号可选带 § 前缀（读者面词表）
YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
PAGE_RANGE_RE = re.compile(r"[:：]\s*\d{4}\s*[-–]\s*\d{4}(?!\d)")
ROW_REF_RE = re.compile(r"(?:文献|证据行|行)\s*\d+[a-z]?")  # 行指针并集词（ADR-0028）
ROW_LABEL_RE = re.compile(r"(?:文献|证据行|行)\s*(\d+[a-z]?)")
E_LEVEL_RE = re.compile(r"E([1-5])")
TABLE_LABEL_RE = re.compile(r"^\d+[a-z]?$")
HALFWIDTH_PUNCT_RE = re.compile(r"[\u4e00-\u9fff][,;:!?]|[\u4e00-\u9fff]\(|\([\u4e00-\u9fff]")
INLINE_CODE_RE = re.compile(r"`[^`]*`")
URL_RE = re.compile(r"https?://\S+")

# §6 四块小标题同义词（M-03 认块；6.4 另收“支持结论”类措辞）。
BLOCK_SYNONYMS = {
    "6.1": ("6.1", "背景"),
    "6.2": ("6.2", "核心结论"),
    "6.3": ("6.3", "讨论"),
    "6.4": ("6.4", "支持结论"),
}
# M-01：6.1／6.3 散文禁任何 ASCII／全角数字。
M01_DIGIT_RE = re.compile(r"[0-9０-９]")
# M-02：效应量类数字（%／小数／n= 后数值／HR·RR·OR·CI·I² 附近数值）；年份与标识符另行豁免。
M02_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
M02_IDENTIFIER_RE = re.compile(r"[A-Za-z](?:[A-Za-z0-9.／/\-]*[0-9])|[0-9][A-Za-z0-9.／/\-]*(?:/[0-9A-Za-z]|[A-Za-z])")
M02_EFFECT_NUM_RE = re.compile(
    r"(?:\d[\d,.\s]*(?:–|—|-|~|～)[\d,.]+\s*(?:％|%))"  # 区间百分比：80.9–57.9％
    r"|(?:\d+\.\d+\s*(?:％|%))"  # 百分比：69.4％
    r"|(?:(?<![\d.])(?:\d+\.\d+|\d+)(?![\d.]))"  # 小数与整数（含 n=1000、CI 上下限）
)
M02_EFFECT_CUE_RE = re.compile(r"HR|RR|OR|CI|I²|I\^2|％|%|n\s*=|p\s*[=＜<]|\bE[1-5]\b")

# L-03 豁免：过渡 / 方法 / 局限类句子不判（确定性近似，完整语义判断交人工）。
EXEMPT_CLAIM_WORDS = (
    "未知", "未见", "待查", "待补", "待核", "局限", "未核", "未评", "未做",
    "不据此", "见 §", "见§", ".md", "断点", "HITL", "跳过", "记 log", "口径",
)

SUGGEST_ST01 = "按八节补齐：执行摘要／研究问题／检索策略／主题式证据综合／证据表／结论／方法附录／局限声明"
SUGGEST_ST02 = "在 §2 写一句系统化研究问题（「研究问题：…」或句末带「？」）；与入口确认稿一致"
SUGGEST_F02 = "按 GB/T 7714 补／改类型标识（J 期刊／M 专著／D 学位论文／C 会议论文／EB/OL 电子）；定稿前人工核对整条"
SUGGEST_F03 = "中文语境改全角标点（，。；：（））；英文术语与数字之间保持半角"
SUGGEST_F04 = "按八节补齐／去重／顺号；改动编号后人工复核正文行指针与节引用"
SUGGEST_R101 = "点名文献并加引文序号／证据表行；暂补不上用 [待补充出处] 占位，不空挂"
SUGGEST_R103 = "核对是否为笔误，并以原文出版年／指南现行版／说明书版为准"
SUGGEST_R301 = "去绝对词或补高级别证据行；规范性断言改引监管原文并注文号／版本；完整语义判断交人工复核"
SUGGEST_T01 = "首次给出中文（英文，缩写）定义，全篇统一中文主名；缩写大小写／连字符统一"
SUGGEST_L03 = "给断言加引文序号与证据行（文献 x），或降为背景句／进局限声明；语义判断交人工复核"
SUGGEST_L08 = "降措辞或补高级别证据与升降级理由；E4 只写观察信号；语义判断交人工复核"
SUGGEST_L09 = "在局限声明逐行交代该行只到仅摘要／仅题录（行号＋原因），或拿到全文后改标有全文"
SUGGEST_M01 = "去掉背景区数字，剂量与效应量走 §5 监管行与 6.4 条目；语义判断交人工复核"
SUGGEST_M02 = "删掉证据层查不到的数字，或补证据表行／进局限声明；语义判断交人工复核"
SUGGEST_M03 = "按 §6 四块补齐：6.1 背景／6.2 核心结论／6.3 讨论／6.4 支持结论；模板见 references/templates/conclusion-template.md"

# 术语一致（T-01）：真源为本 skill 的 references/lexicon/term-lexicon.yaml（无表即不报，宁漏勿噪；关系数据只有真源一份）。
# 判定口径：同义家族出现两个及以上不同形报 warning；上下位不报；易混对同篇并现报 info；正字变体静默归一不报。
T01_SEVERITY = {"synonym": "warning", "confusable": "info"}
SUGGEST_T01_CONFUSABLE = "人工确认两词是否同一概念后再统一；易混词不参与自动扩展"
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def is_chinese_form(term):
    """T-01 只查中文混用：纯拉丁形（英文与缩写）保留不判，只作报告计数展示。"""
    return bool(CJK_RE.search(term))


def count_form(text, term):
    """拉丁形按词边界计数（避免 MI 被 STEMI／AMI 里的子串抬高），中文形按子串计数。"""
    if is_chinese_form(term):
        return text.count(term)
    return len(re.findall(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text))


def select_spans(path, spans):
    """按行号范围抽出词表片段 → 可独立加载的真源文档（只含 terms 行，供定点回归；不含 synonym_sets）。"""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    version = next((ln for ln in lines if ln.startswith("version:")), "version: 片段")
    keep = set()
    for spec in spans:
        parts = spec.split("-")
        lo, hi = int(parts[0]), int(parts[-1])
        keep.update(range(lo, hi + 1))
    picked = [ln for n, ln in enumerate(lines, 1) if n in keep and ln.strip() and not ln.lstrip().startswith("#")]
    if not picked:
        raise ValueError(f"词表片段为空：{spans}")
    indent = min(len(ln) - len(ln.lstrip(" ")) for ln in picked)
    body = "\n".join("  " + ln[indent:] for ln in picked)
    return f"{version}\nterms:\n{body}\n"


def lexicon_families(path=None, cache_dir=DEFAULT_CACHE_DIR):
    """读词表真源 → (同义家族, 易混对, 版本)。表缺失或不可解析时按无表处理（T-01 不报，宁漏勿噪）。

    解析结果按「文件 sha256 ＋ 解析器版本」跨进程复用（`cache_dir=None`＝不读不写，`--no-cache` 走此）；
    表缺失不查缓存、不回退错表；候选缓冲是写路径，与本缓存无关（N-09）。
    """
    if path and os.path.exists(path):
        try:
            import term_expand
            data, source = term_expand.load_lexicon_doc(path, cache_dir)
        except (OSError, ValueError, UnicodeDecodeError, ImportError) as exc:
            # 只兜可预期的表问题（缺文件／坏格式／同目录脚本缺失）；代码缺陷照常抛出，不伪装成无表
            return [], [], f"无表（词表不可用：{exc}）"
        print(f"CACHE lexicon source={source} file={os.path.basename(path)} "
              f"parser={term_expand.LEXICON_PARSER_VERSION}", file=sys.stderr)
        families, confusables = [], []
        for entry in data.get("terms") or []:
            if not isinstance(entry, dict):
                continue
            for edge in entry.get("relations") or []:
                if not isinstance(edge, dict):
                    continue
                pair = (entry.get("term"), edge.get("target"))
                if edge.get("type") == "confusable" and all(pair):
                    if not any(a in b or b in a for a, b in ((pair[0], pair[1]), (pair[1], pair[0]))) \
                            and pair not in confusables and (pair[1], pair[0]) not in confusables:
                        confusables.append(pair)
        for group in data.get("synonym_sets") or []:
            if not isinstance(group, dict):
                continue
            if group.get("report") is False:  # 家族标记不判（如纯英文与缩写保留）
                continue
            members = [m for m in (group.get("members") or []) if isinstance(m, str) and m]
            if len(members) < 2:
                continue
            families.append((tuple(members), group.get("canonical") or members[0]))
        version = data.get("version") or "未知版本"
        if families or confusables:
            return families, confusables, str(version)
        return [], [], f"{version}（无可用条目：T-01 不报）"
    return [], [], "无表"


def check_t01(doc, ctx):
    families, confusable_pairs, _ = ctx["lexicon"]
    text = doc.full_text()
    out = []
    for members, canonical in families:
        counts = [(m, count_form(text, m)) for m in members]
        chinese = [(m, n) for m, n in counts if n > 0 and is_chinese_form(m)]
        latin = [(m, n) for m, n in counts if n > 0 and not is_chinese_form(m)]
        if len(chinese) < 2:
            continue  # 只出现一种中文形（或只出现英文/缩写）不判中文混用
        line_no, line = last_hit(doc, *[m for m, _ in chinese + latin])
        pos = min(p for p in (line.find(m) for m, _ in chinese) if p >= 0)
        shown = "／".join(f"{m}（{n} 处）" for m, n in chinese)
        tail = ("；英文与缩写保留不判：" + "／".join(f"{m}（{n} 处）" for m, n in latin)) if latin else ""
        out.append(finding(
            line_no, "T-01", T01_SEVERITY["synonym"],
            f"同一概念中文多形混用：{shown}，未统一主名（{canonical}）{tail}",
            clip_around(line, pos), SUGGEST_T01))
    for a, b in confusable_pairs:
        if a not in text or b not in text:
            continue
        line_no, line = last_hit(doc, a, b)
        pos = min(p for p in (line.find(a), line.find(b)) if p >= 0)
        out.append(finding(
            line_no, "T-01", T01_SEVERITY["confusable"],
            f"形近易混词同篇出现：{a}／{b}（不自动归一）",
            clip_around(line, pos), SUGGEST_T01_CONFUSABLE))
    return out


def clip(text, limit=60):
    """压缩空白并按长度截断（超长补省略号）。"""
    t = re.sub(r"\s+", " ", text).strip()
    return t if len(t) <= limit else t[:limit] + "……"


def clip_around(text, pos, limit=60):
    """取命中位置附近窗口做上下文。"""
    start = max(0, pos - limit // 3)
    window = re.sub(r"\s+", " ", text[start:start + limit]).strip()
    return ("……" if start else "") + window + ("……" if start + limit < len(text) else "")


def sentences(text):
    """按中文句号切分，保留句尾标点（行指针通常与断言同句）。"""
    out, buf = [], ""
    for ch in text:
        buf += ch
        if ch == "。":
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def sentence_around(text, pos):
    """取包含 pos 的整句。"""
    for s in sentences(text):
        start = text.find(s)
        if start <= pos < start + len(s):
            return s
    return text


def finding(line, rule, severity, message, context, suggestion):
    return {
        "line": line,
        "rule": rule,
        "severity": severity,
        "message": message,
        "context": context,
        "suggestion": suggestion,
    }


def has_citation_or_row(text):
    """句子是否带引文序号、证据表行指针或表指称（可追溯锚点）。"""
    return bool(CITATION_MARK_RE.search(text) or ROW_REF_RE.search(text) or "证据表" in text)


def has_grade_anchor(text):
    """是否带高级别证据锚点：E1 引用 / 证据表行 / 指南强推荐。"""
    return bool(ROW_REF_RE.search(text) or "E1" in text or "强推荐" in text or "证据表" in text)


class Doc:
    """知识包文本视图：八节认节、表行等级、断言行与全篇文本。"""

    def __init__(self, path, text):
        self.name = os.path.basename(path)
        self.lines = text.splitlines()
        self.code = set()
        fenced = False
        for i, line in enumerate(self.lines, 1):
            if line.lstrip().startswith("```"):
                fenced = not fenced
                self.code.add(i)
                continue
            if fenced:
                self.code.add(i)
        self.headings = []  # (行号, 级别, 编号或 None, 标题)
        for i, line in enumerate(self.lines, 1):
            if i in self.code:
                continue
            m = HEADING_RE.match(line)
            if not m:
                continue
            level, title = len(m.group(1)), m.group(2)
            num = None
            n = NUMBERED_RE.match(title)
            if n and int(n.group(1)) <= 99:  # 只认节号，不认年份起首标题
                num = int(n.group(1))
            self.headings.append((i, level, num, title))
        self.role_line = {}
        self.role_title = {}
        for i, level, num, title in self.headings:
            role = role_of(title, level)
            if role and role not in self.role_line:
                self.role_line[role] = i
                self.role_title[role] = title
        self.role_span = {}
        found = sorted(self.role_line.values())
        for role, line in self.role_line.items():
            after = [x for x in found if x > line]
            self.role_span[role] = (line + 1, (min(after) - 1) if after else len(self.lines))

    def clean(self, i):
        """去掉行内代码与 URL 后的行文本（位置仍与原文同序，用于上下文）。"""
        if i in self.code:
            return ""
        return URL_RE.sub("", INLINE_CODE_RE.sub("", self.lines[i - 1]))

    def scan_lines(self, tables=True):
        for i in range(1, len(self.lines) + 1):
            if i in self.code:
                continue
            if not tables and self.is_table(i):
                continue
            yield i, self.clean(i)

    def is_table(self, i):
        return self.lines[i - 1].lstrip().startswith("|")

    def claim_prose(self):
        """断言区散文行（跳过标题、空行、表行、引用块）。"""
        for role in CLAIM_ROLES:
            lo, hi = self.role_span.get(role, (0, -1))
            for i in range(lo, hi + 1):
                if i in self.code:
                    continue
                raw = self.lines[i - 1].strip()
                if not raw or raw.startswith(("#", "|", ">")):
                    continue
                yield i, self.clean(i)

    def full_text(self):
        return "\n".join(self.clean(i) for i in range(1, len(self.lines) + 1))

    def section_text(self, role):
        lo, hi = self.role_span.get(role, (0, -1))
        return "\n".join(self.clean(i) for i in range(lo, hi + 1))

    def conclusion_blocks(self):
        """§6 内四块子区间：{块id: (起, 止)}；块标题行不计入区间。"""
        lo, hi = self.role_span.get("结论", (0, -1))
        if lo > hi:
            return {}
        heads = []  # (行号, 块id)
        for ln in range(lo, hi + 1):
            if ln in self.code:
                continue
            text = self.lines[ln - 1].strip()
            if not text.startswith("#"):
                continue
            hm = HEADING_RE.match(text)
            head = PAREN_RE.sub("", hm.group(2) if hm else text)
            for bid, alts in BLOCK_SYNONYMS.items():
                num, keys = alts[0], alts[1:]
                if num in head and any(k in head for k in keys):
                    heads.append((ln, bid))
                    break
        spans = {}
        for n, (ln, bid) in enumerate(heads):
            end = (heads[n + 1][0] - 1) if n + 1 < len(heads) else hi
            spans[bid] = (ln + 1, end)
        return spans

    def exempt_lines(self):
        """6.1–6.3 子区间散文行号集合（L-03／L-08 豁免；6.4 仍扫）。"""
        out = set()
        blocks = self.conclusion_blocks()
        for bid in ("6.1", "6.2", "6.3"):
            span = blocks.get(bid)
            if not span:
                continue
            blo, bhi = span
            for ln in range(blo, bhi + 1):
                if ln in self.code:
                    continue
                raw_line = self.lines[ln - 1].strip()
                if not raw_line or raw_line.startswith(("#", "|", ">")):
                    continue
                out.add(ln)
        return out

    def row_grades(self):
        """证据表 行号 → 定级后 E 级别（取该行最后一个 E 级，降档行以定级为准）。"""
        grades = {}
        for i, line in self.scan_lines():
            s = line.strip()
            if not s.startswith("|"):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not cells or not TABLE_LABEL_RE.match(cells[0]):
                continue
            levels = E_LEVEL_RE.findall(s)
            if levels:
                grades[cells[0]] = "E" + levels[-1]
        return grades


def role_of(title, level=None):
    """认节：括号内文字不参与匹配（避免“（可复现审计摘要）”被当成摘要节）。
    四块小标题（### 6.x）不认节：它们含“结论”二字，必须先排除，否则 F-04 误报重复。"""
    head = PAREN_RE.sub("", title)
    if level is not None and level >= 3 and re.search(r"6\.[1-4]", head):
        return None
    for role, alternatives in SECTIONS.items():
        if any(alt in head for alt in alternatives):
            return role
    return None


def check_st01(doc, ctx):
    missing = [role for role in SECTIONS if role not in doc.role_line]
    if not missing:
        return []
    anchor = max(doc.role_line.values()) if doc.role_line else 1
    out = []
    for role in missing:
        out.append(finding(
            anchor, "ST-01", "error",
            f"缺节：{role}（八节要素不全）",
            f"全文未见“{role}”节标题（同义措辞：{'／'.join(SECTIONS[role])}）",
            SUGGEST_ST01))
    return out


def check_st02(doc, ctx):
    line = doc.role_line.get("研究问题")
    if not line:
        return []  # 缺整节由 ST-01 报，此处不重复
    body = doc.section_text("研究问题")
    if QUESTION_MARK_RE.search(body) or QUESTION_LABEL_RE.search(body):
        return []
    return [finding(line, "ST-02", "warning",
                    "研究问题节缺研究问题陈述（无问句、无「研究问题：」行）",
                    clip(doc.role_title.get("研究问题", "")),
                    SUGGEST_ST02)]


def check_f02(doc, ctx):
    out = []
    for i, line in doc.scan_lines():
        for m in CITATION_MARK_RE.finditer(line):
            nxt = CITATION_MARK_RE.search(line, m.end())
            end = min(m.start() + 200, nxt.start() if nxt else len(line))
            entry = line[m.start():end]
            tid = TYPE_ID_RE.search(entry)
            if not tid:
                out.append(finding(i, "F-02", "warning",
                                   f"引文 {m.group(0)} 缺 GB/T 7714 类型标识",
                                   clip(entry), SUGGEST_F02))
                continue
            kind = tid.group(0)[1:-1]
            if kind == "J" and any(cue in entry for cue in PUBLISHER_CUES):
                out.append(finding(i, "F-02", "warning",
                                   f"引文 {m.group(0)} 类型标识疑似错配：[J] 用于专著／学位论文条目",
                                   clip(entry), SUGGEST_F02))
            elif kind in ("M", "D", "C") and any(cue in entry for cue in JOURNAL_CUES):
                out.append(finding(i, "F-02", "warning",
                                   f"引文 {m.group(0)} 类型标识疑似错配：{tid.group(0)} 用于期刊条目",
                                   clip(entry), SUGGEST_F02))
    return out


def check_f03(doc, ctx):
    out = []
    for i, line in doc.scan_lines():
        hits = HALFWIDTH_PUNCT_RE.findall(line)
        if hits:
            m = HALFWIDTH_PUNCT_RE.search(line)
            out.append(finding(i, "F-03", "info",
                               f"中文语境混用半角标点（本行 {len(hits)} 处，如“{m.group(0)}”）",
                               clip_around(line, m.start()), SUGGEST_F03))
    return out


def check_f04(doc, ctx):
    out = []
    by_role = {}
    for i, level, num, title in doc.headings:
        role = role_of(title, level)
        if role:
            by_role.setdefault(role, []).append((i, title))
    for role, hits in by_role.items():
        if len(hits) > 1:
            places = "、".join(f"第 {i} 行" for i, _ in hits)
            out.append(finding(hits[1][0], "F-04", "warning",
                               f"节标题重复：{role}出现 {len(hits)} 次（{places}）",
                               clip(hits[1][1]), SUGGEST_F04))
    numbered = [(i, num, title) for i, level, num, title in doc.headings if num is not None]
    order = [num for _, num, _ in numbered]
    expected = sorted(order)
    # 设计序例外：§6 结论整体排在 §5 证据表之前（SKILL.md 八节口径），唯一允许的一处倒序
    if 5 in expected and 6 in expected:
        at5, at6 = expected.index(5), expected.index(6)
        expected[at5], expected[at6] = 6, 5
    if order != expected:
        seq = "→".join(str(n) for n in order)
        want = "→".join(str(n) for n in expected)
        out.append(finding(numbered[0][0], "F-04", "warning",
                           f"标题编号序异常：{seq}（应为 {want}；§6 结论先于 §5 证据表为设计序）",
                           clip(numbered[0][2]), SUGGEST_F04))
    return out


def check_r101(doc, ctx):
    out = []
    for i, line in doc.scan_lines(tables=False):
        for m in VAGUE_RE.finditer(line):
            sent = sentence_around(line, m.start())
            if has_citation_or_row(sent):
                continue
            out.append(finding(i, "R1-01", "warning",
                               f"模糊引用“{m.group(0)}”：未点名文献、无引文号、无证据表行",
                               clip(sent), SUGGEST_R101))
    for i, line in doc.scan_lines():
        for placeholder in PLACEHOLDERS:
            count = line.count(placeholder)
            if not count:
                continue
            sent = sentence_around(line, line.index(placeholder))
            suffix = f"（本行 {count} 处）" if count > 1 else ""
            out.append(finding(i, "R1-01", "warning",
                               f"待补占位 {placeholder} 尚未补齐{suffix}",
                               clip(sent), "补上文献／数字／页码出处，或进局限声明"))
    return out


def check_r103(doc, ctx):
    out = []
    cur_year = ctx["cur_year"]
    for i, line in doc.scan_lines():
        # 卷(期):起页-止页 形态的页码区间不判（四位数字易被误读为年份）
        page_spans = [m.span() for m in PAGE_RANGE_RE.finditer(line)]
        seen = set()
        for m in YEAR_RE.finditer(line):
            year = int(m.group(1))
            if year <= cur_year or year in seen:
                continue
            if any(lo <= m.start() and m.end() <= hi for lo, hi in page_spans):
                continue
            seen.add(year)
            out.append(finding(i, "R1-03", "error",
                               f"年份 {year} 超出当前年份（{cur_year}），疑似笔误或非出版年份",
                               clip_around(line, m.start()), SUGGEST_R103))
    return out


def check_r301(doc, ctx):
    out = []
    for i, line in doc.claim_prose():
        if has_grade_anchor(line):
            continue  # 同段已有高级别证据锚点（E1 行／指南强推荐），不报
        for sent in sentences(line.lstrip("-*+ ").strip()):
            m = ABSOLUTE_RE.search(sent)
            if not m:
                continue
            out.append(finding(i, "R3-01", "warning",
                               f"绝对词“{m.group(0)}”：同段无高级别证据锚点（E1 行／指南强推荐）",
                               clip(sent), SUGGEST_R301))
    return out


def last_hit(doc, *needles):
    """含任一关键词的最后一行：(行号, 行文本)。术语并现的落点通常在后者所在行。"""
    hit = (1, "")
    for i, line in doc.scan_lines():
        if any(n in line for n in needles):
            hit = (i, line)
    return hit


def block_prose(doc, bid):
    """某四块子区间散文行（跳过标题、空行、表行、引用块与代码块）。"""
    span = doc.conclusion_blocks().get(bid)
    if not span:
        return
    lo, hi = span
    for ln in range(lo, hi + 1):
        if ln in doc.code:
            continue
        raw = doc.lines[ln - 1].strip()
        if not raw or raw.startswith(("#", "|", ">")):
            continue
        yield ln, doc.clean(ln)


def check_m01(doc, ctx):
    out = []
    for bid in ("6.1", "6.3"):
        for ln, line in block_prose(doc, bid):
            for sent in sentences(line.lstrip("-*+ ").strip()):
                m = M01_DIGIT_RE.search(sent)
                if not m:
                    continue
                out.append(finding(ln, "M-01", "error",
                               "背景区出现数字（" + bid + " 禁数字）：" + clip_around(sent, m.start()),
                               clip(sent), SUGGEST_M01))
    return out


def norm_num(text):
    """数字归一：全角数字／全角小数点／千分位分隔符统一为半角连续数字。"""
    conv = {"０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
            "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
            "．": ".", "，": "", ",": "", "、": "", " ": "", "　": ""}
    return "".join(conv.get(ch, ch) for ch in text)


def exclude_conclusion(doc):
    """全文去掉结论节正文（M-02 fallback 用；§4／§5／6.4 照常保留）。"""
    lo, hi = doc.role_span.get("结论", (0, -1))
    drop = set(range(lo, hi + 1)) if lo <= hi else set()
    drop.add(doc.role_line.get("结论", -1))
    return norm_num("\n".join(doc.clean(ln) for ln in range(1, len(doc.lines) + 1) if ln not in drop))


def evidence_text(doc):
    """证据层全文：§4＋§5＋6.4＋引文表（## 参考文献节；无该节时退为全文）。"""
    parts = [doc.section_text(r) for r in ("主题式证据综合", "证据表")]
    span = doc.conclusion_blocks().get("6.4")
    if span:
        blo, bhi = span
        parts.append("\n".join(doc.clean(ln) for ln in range(blo, bhi + 1)))
    ref = None
    for ln, level, num, title in doc.headings:
        if "参考文献" in PAREN_RE.sub("", title):
            ref = ln
            break
    if ref is not None:
        parts.append("\n".join(doc.clean(ln) for ln in range(ref + 1, len(doc.lines) + 1)))
    else:
        parts.append(exclude_conclusion(doc))  # 无引文表时退为“全文去结论节”，防 6.2 数字自匹配
    return norm_num("\n".join(parts))


def check_m02(doc, ctx):
    out = []
    ev = evidence_text(doc)
    seen = set()
    for ln, line in block_prose(doc, "6.2"):
        text = line.lstrip("-*+ ").strip()
        id_spans = [m.span() for m in M02_IDENTIFIER_RE.finditer(text)]
        skip_year = {m.group(0) for m in M02_YEAR_RE.finditer(text)}
        for sent in sentences(text):
            if not M02_EFFECT_CUE_RE.search(sent):
                continue
            base = text.index(sent)
            for m in M02_EFFECT_NUM_RE.finditer(sent):
                tok = m.group(0)
                if tok in skip_year:
                    continue  # 四位年份豁免
                lo = base + m.start()
                if any(a <= lo and lo + len(tok) <= b for a, b in id_spans):
                    continue  # 字母紧邻的标识符内数字豁免
                if norm_num(tok) in ev:
                    continue
                key = (ln, norm_num(tok))
                if key in seen:
                    continue
                seen.add(key)
                out.append(finding(ln, "M-02", "error",
                               "核心段数字在证据层查不到：" + tok,
                               clip(sent), SUGGEST_M02))
    return out


def check_m03(doc, ctx):
    if "结论" not in doc.role_line:
        return []  # 缺整节由 ST-01 报，此处不重复
    blocks = doc.conclusion_blocks()
    missing = [b for b in ("6.1", "6.2", "6.3", "6.4") if b not in blocks]
    if not missing:
        return []
    anchor = doc.role_line["结论"]
    names = {"6.1": "6.1 背景", "6.2": "6.2 核心结论", "6.3": "6.3 讨论", "6.4": "6.4 支持结论"}
    want = "、".join(names[b] for b in missing)
    return [finding(anchor, "M-03", "error",
                    "结论四块不全：缺" + want,
                    "§6 内未见" + want + "小标题",
                    SUGGEST_M03)]


def check_l03(doc, ctx):
    exempt = doc.exempt_lines()  # 6.1–6.3 豁免 L-03／L-08：不写行号的代价由 M-01／M-02 接住
    out = []
    for i, line in doc.claim_prose():
        if i in exempt:
            continue
        for sent in sentences(line.lstrip("-*+ ").strip()):
            if len(sent) < 12 or has_citation_or_row(sent):
                continue
            if any(word in sent for word in EXEMPT_CLAIM_WORDS):
                continue
            out.append(finding(i, "L-03", "error",
                               "实质断言句缺引文序号与证据表行",
                               clip(sent), SUGGEST_L03))
    return out


def check_l08(doc, ctx):
    exempt = doc.exempt_lines()  # 6.1–6.3 豁免 L-03／L-08（票面逐字口径；代价由 M-01／M-02 接住）
    out = []
    grades = ctx["row_grades"]
    for i, line in doc.claim_prose():
        if i in exempt:
            continue
        for sent in sentences(line.lstrip("-*+ ").strip()):
            m = next((x for x in STRONG_CLAIM_RE.finditer(sent) if not _negated(sent, x.start())), None)
            if m is None:
                continue  # “无显著差异／未见显著关联”类否定用法不报
            levels = [grades.get(label) for label in ROW_LABEL_RE.findall(sent)]
            levels = [lv for lv in levels if lv]
            literal = LOW_GRADE_RE.search(sent)
            if levels and all(lv in LOW_GRADES for lv in levels):
                shown = "／".join(sorted(set(levels)))
            elif not levels and literal:
                shown = literal.group(0)
            else:
                continue
            out.append(finding(i, "L-08", "warning",
                               f"强措辞“{m.group(0)}”配低级别证据（{shown}）：级别与措辞强度不匹配",
                               clip(sent), SUGGEST_L08))
    return out


def evidence_status_rows(doc):
    """证据表「设计与等级」列带可及性状态词的行：{行号: (表内行位置, 状态词)}。"""
    out = {}
    for i, line in doc.scan_lines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 3 or not TABLE_LABEL_RE.match(cells[0]):
            continue
        m = STATUS_MARK_RE.search(cells[2])
        if m:
            out[cells[0]] = (i, m.group(0))
    return out


def check_l09(doc, ctx):
    """仅摘要／仅题录行须在 §8 局限声明逐行交代（R5／ADR-0018）。"""
    rows = evidence_status_rows(doc)
    if not rows or "局限声明" not in doc.role_line:
        return []  # 缺整节由 ST-01 报，此处不重复
    limits = doc.section_text("局限声明")
    out = []
    for label, (line_no, mark) in rows.items():
        if re.search(rf"(?:文献|证据行|行)\s*{re.escape(label)}(?![\w])", limits):
            continue
        out.append(finding(line_no, "L-09", "error",
                           f"证据表 {mark} 行 {label} 未在局限声明逐行交代",
                           f"证据表「设计与等级」列标 {mark}，§8 未见 `行 {label}` 指针",
                           SUGGEST_L09))
    return out


def _negated(text, pos):
    """强措辞前 3 字内出现否定词即视为否定用法。"""
    return text[max(0, pos - 3):pos].rstrip()[-1:] in NEGATION_CHARS


CHECKERS = (
    ("ST-01", check_st01),
    ("ST-02", check_st02),
    ("F-02", check_f02),
    ("F-03", check_f03),
    ("F-04", check_f04),
    ("R1-01", check_r101),
    ("R1-03", check_r103),
    ("R3-01", check_r301),
    ("T-01", check_t01),
    ("L-03", check_l03),
    ("L-08", check_l08),
    ("L-09", check_l09),
    ("M-01", check_m01),
    ("M-02", check_m02),
    ("M-03", check_m03),
)


def build_parser():
    ap = argparse.ArgumentParser(
        prog="review_knowledge.py",
        description="知识包事后确定性校验（纯本地文本检查；退出码 0 无 error／1 有 error／2 用法或文件错误）")
    ap.add_argument("input", help="知识包 Markdown 路径")
    ap.add_argument("--format", choices=("text", "json"), default="text", help="输出格式（默认 text）")
    ap.add_argument("--cur-year", type=int, default=datetime.date.today().year,
                    help="参考年份（默认系统当前年；e2e 固定传入保证可复现）")
    ap.add_argument("--rules", default="", help="规则子集，逗号分隔（默认全部规则）")
    ap.add_argument("--lexicon", default=DEFAULT_LEXICON,
                    help="术语词表真源路径（默认 skill 内 references/lexicon/term-lexicon.yaml；无表即不报）")
    ap.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR,
                    help="词表解析缓存目录（缺省＝skill 包内 .cache/；须为被忽略的非交付路径）")
    ap.add_argument("--no-cache", action="store_true",
                    help="显式绕过词表解析缓存：不读不写（照旧每次解析）")
    return ap


def select_rules(spec, ap):
    if not spec.strip():
        return set(ALL_RULES)
    asked = [x.strip() for x in spec.split(",") if x.strip()]
    unknown = [x for x in asked if x not in RULE_ORDER]
    if unknown:
        ap.error(f"未知规则：{'、'.join(unknown)}"
                 f"（可选：{'、'.join(rule_id for rule_id, _, _ in RULES)}）")
    return set(asked)


def render_text(name, findings, lexicon_version=None):
    lines = []
    if lexicon_version:
        lines.append(f"词表：{lexicon_version}")
    for f in findings:
        lines.append(f"{name}:{f['line']} [{f['rule']}/{f['severity']}] {f['message']}"
                     f" | 上下文：{f['context']} | 建议：{f['suggestion']}")
    counts = {sev: sum(1 for f in findings if f["severity"] == sev) for sev in SEVERITIES}
    lines.append(f"小结：error={counts['error']} warning={counts['warning']} info={counts['info']}")
    return "\n".join(lines)


def review_package(path, cur_year, rules, lexicon=None, cache_dir=DEFAULT_CACHE_DIR):
    """一次校验：读包 → 加载词表 → 跑规则子集 → {"name", "findings", "lexicon_version", "summary"}。

    两种渲染（`json_payload`／`render_text`）都从这一份结果出——门禁「一次执行、两种渲染」走此入口；
    文件不可读／非 UTF-8 照 CLI 口径抛 OSError／UnicodeDecodeError（调用方记退出码 2）。
    """
    with open(path, encoding="utf-8") as f:
        text = f.read()
    doc = Doc(path, text)
    lexicon = lexicon_families(lexicon, cache_dir)
    ctx = {"cur_year": cur_year, "row_grades": doc.row_grades(), "lexicon": lexicon}
    findings = []
    for rule_id, checker in CHECKERS:
        if rule_id in rules:
            findings.extend(checker(doc, ctx))
    findings.sort(key=lambda f: (f["line"], RULE_ORDER[f["rule"]], f["message"]))
    summary = {sev: sum(1 for f in findings if f["severity"] == sev) for sev in SEVERITIES}
    return {"name": doc.name, "findings": findings, "lexicon_version": lexicon[2], "summary": summary}


def json_payload(result):
    """机器契约三键（review-rules.md §2）：file 只记 basename；findings 已按行号／规则序／消息排序。"""
    return {"file": result["name"], "summary": result["summary"], "findings": result["findings"]}


def exit_code(result):
    """退出码：有 error 即 1，否则 0（用法与文件错误的 2 由调用方按异常记）。"""
    return 1 if any(f["severity"] == "error" for f in result["findings"]) else 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = build_parser()
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    rules = select_rules(args.rules, ap)
    try:
        result = review_package(args.input, args.cur_year, rules, args.lexicon,
                                None if args.no_cache else args.cache_dir)
    except OSError as e:
        print(f"文件错误：{args.input}（{e.strerror or e}）", file=sys.stderr)
        return 2
    except UnicodeDecodeError:
        print(f"文件错误：{args.input} 不是 UTF-8 文本，无法校验", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(json_payload(result), ensure_ascii=False, indent=2))
    else:
        print(render_text(result["name"], result["findings"], result["lexicon_version"]))
    return exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
