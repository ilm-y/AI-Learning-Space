"""来源引用校验。

模型回答里的 `[资料《文件名》#片段序号]` 是**可机器核对**的契约：它必须与
本轮 Context Builder 实际渲染进 system instruction 的知识片段一一对应。
本模块只做两件事：

1. **合法性校验（客观）**：解析回答里的标注，与本轮真正进入上下文的
   `资料《文件名》#序号` 做精确比较。上下文里不存在的标注一律算非法引用；
   被 Token 预算丢掉的知识片段也算「不在上下文里」。
2. **来源支持度（启发式）**：把标注所在句子与片段原文做词面重合与数字一致性
   检查，给出 `strong / weak / none / numeric_conflict` 的**信号**。

第 2 项不是事实正确性证明。词面重合高不代表推论成立，词面重合低也不代表
模型错了（它可能在做同义改写）。所以报告里必须把两者分开：非法引用率可以
作为硬指标，支持度只能作为排查线索。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.schemas import KnowledgeChunk

# 只接受 Prompt 里约定的精确格式。文件名里不允许出现 `》` 或换行；
# 片段序号只接受非负整数 —— 其他写法一律进 malformed，而不是被宽松放过。
CITATION_PATTERN = re.compile(r"\[资料《(?P<name>[^》\n]+)》#(?P<index>\d+)\]")
# 除了 `[资料…]`，模型还会写 `[#1]` / `[#1, #2]` 这种残缺标注。
# 它们不符合 Prompt 约定的精确格式，同样应进入 malformed，而不是被放过。
CITATION_LIKE_PATTERN = re.compile(r"\[(?:资料[^\]\n]*|#\d[^\]\n]*)\]")
_BARE_REFERENCE = re.compile(r"\[#\d+(?:\s*[,，、–—-]\s*#?\d+)*\]")

# 中文按二元组、英文按单词切。二元组对中文短文本足够稳定，也不需要分词依赖。
_CJK_RUN = re.compile(r"[一-鿿]+")
_ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z0-9._+-]*")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_SENTENCE_BREAK = re.compile(r"[。！？!?\n；;]")
_CLAIM_DECORATION = re.compile(r"[\s>#*_`~\-—–:：;；,，。.!！?？|（）()\[\]【】\"'“”‘’…]+")
# 只在后面紧跟量词时换算，避免把「两者 / 二级缓存」里的「两 / 二」当成数字 2。
# 不收「个」：「一个历史值 / 一个片段」是不定指，不是可核对的事实数字；
# 真实模型回答里这种用法会把 `numeric_conflict` 误报成来源冲突。
_CJK_NUMBER_RUN = re.compile(
    r"([零一二两三四五六七八九十]+)"
    r"(?=(?:次|条|天|秒|分|分钟|小时|级|页|位|张|台|卡|倍|年|月|日|周|轮|层))"
)
_CJK_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "into",
    "are",
    "is",
    "of",
    "to",
    "in",
    "on",
    "as",
    "by",
    "资料",
}


@dataclass(frozen=True, slots=True)
class Citation:
    """一条标注及其校验结果。"""

    raw: str
    document_name: str
    chunk_index: int
    valid: bool
    chunk_id: int | None
    document_id: int | None
    claim: str
    source_excerpt: str | None
    lexical_support: str
    overlap_terms: tuple[str, ...] = ()
    numeric_conflict: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "raw": self.raw,
            "documentName": self.document_name,
            "chunkIndex": self.chunk_index,
            "valid": self.valid,
            "chunkId": self.chunk_id,
            "documentId": self.document_id,
            "claim": self.claim,
            "sourceExcerpt": self.source_excerpt,
            "lexicalSupport": self.lexical_support,
            "overlapTerms": list(self.overlap_terms),
            "numericConflict": self.numeric_conflict,
        }


@dataclass
class CitationReport:
    """一次回答的引用校验汇总。"""

    provided_labels: list[str] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    invalid_labels: list[str] = field(default_factory=list)
    malformed_labels: list[str] = field(default_factory=list)
    duplicate_labels: list[str] = field(default_factory=list)
    cited_documents: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.citations)

    @property
    def valid(self) -> int:
        return sum(1 for citation in self.citations if citation.valid)

    @property
    def invalid(self) -> int:
        return self.total - self.valid

    @property
    def illegal_rate(self) -> float | None:
        """非法引用占全部标注的比例；没有标注时返回 None（不是 0）。"""
        return round(self.invalid / self.total, 4) if self.total else None

    @property
    def has_citations(self) -> bool:
        return self.total > 0

    @property
    def malformed_count(self) -> int:
        """不符合 `[资料《名》#序号]` 精确格式的疑似标注数量。"""
        return len(self.malformed_labels)

    @property
    def syntax_valid(self) -> bool:
        """所有疑似标注都符合唯一合法格式；不是「引用内容正确」。"""
        return self.malformed_count == 0

    @property
    def unsupported_count(self) -> int:
        """格式与索引都合法、但所在句子与片段没有词面重合的标注数。

        词面支持度只是排查线索，不是事实正确性证明；因此单独报数，
        不能和 `invalid`（索引不存在）混为一谈。
        """
        return sum(
            1
            for citation in self.citations
            if citation.valid and citation.lexical_support == "none"
        )

    @property
    def numeric_conflict_count(self) -> int:
        """标注句里的数字在对应片段中找不到的条数。

        这是来源归属的诊断信号，不是错误判定：同义改写、跨句组合都可能触发，
        真正的语义是否由该来源支持仍需人工复核。
        """
        return sum(
            1
            for citation in self.citations
            if citation.valid and citation.numeric_conflict
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "providedLabels": self.provided_labels,
            "total": self.total,
            "valid": self.valid,
            "invalid": self.invalid,
            "illegalRate": self.illegal_rate,
            "invalidLabels": self.invalid_labels,
            "malformedLabels": self.malformed_labels,
            "malformedCount": self.malformed_count,
            "syntaxValid": self.syntax_valid,
            "unsupportedCount": self.unsupported_count,
            "numericConflictCount": self.numeric_conflict_count,
            "duplicateLabels": self.duplicate_labels,
            "citedDocuments": self.cited_documents,
            "citations": [citation.as_dict() for citation in self.citations],
        }


def format_source_label(chunk: KnowledgeChunk) -> str:
    """Context Builder 与校验器共用的来源标注（不含方括号）。

    两边必须调用同一个函数：只要格式漂移，模型照着 Prompt 引用也会被
    校验器判成非法，最后只能靠人肉发现。
    """
    name = (chunk.document_name or "").strip()
    if name:
        if chunk.chunk_index is not None:
            return f"资料《{name}》#{chunk.chunk_index}"
        return f"资料《{name}》片段#{chunk.id}"
    return f"资料片段#{chunk.id}"


def structured_citations(report: CitationReport) -> list[dict[str, object]]:
    """把校验通过的标注收敛成可追溯到 chunk 的结构化来源。

    只保留**合法且带数据库身份**的标注：非法标注已经被流式过滤器丢弃，不进入
    交付文本；Fixture / 旧数据没有 document_id 或 chunk_id 时也无法生成可靠链接，
    宁可不下发，也不让客户端拿到一条指不到任何来源的引用。

    同一 `(document_id, chunk_id)` 在回答中被引用多次时只保留一次，顺序按首次
    出现。字段命名与 Spring 侧的 DTO 对齐（camelCase）：
    `documentId / chunkId / chunkIndex / sourceTitle`。

    `chunkIndex` 不是元数据装饰：正文里的 `[资料《标题》#序号]` 靠它映射到结构化
    来源，客户端才能把文字标签替换成可点击引用。
    """
    seen: set[tuple[int, int]] = set()
    result: list[dict[str, object]] = []
    for citation in report.citations:
        if not citation.valid:
            continue
        if citation.document_id is None or citation.chunk_id is None:
            continue
        key = (citation.document_id, citation.chunk_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "documentId": citation.document_id,
                "chunkId": citation.chunk_id,
                "chunkIndex": citation.chunk_index,
                "sourceTitle": citation.document_name,
            }
        )
    return result


def provided_labels_from_context(context, chunks: list[KnowledgeChunk]) -> list[str]:
    """只返回**真正没有被 Token 预算丢掉**的知识标注。

    `state.knowledge` 是检索结果；`context.sections` 才是模型实际看到的。
    被 `_fit_budget` 丢掉的 knowledge section 仍然在 state 里，但模型看不到它，
    所以模型不可能合法引用它 —— 这里必须按 section 过滤。
    """
    by_id = {str(chunk.id): chunk for chunk in chunks}
    labels: list[str] = []
    for section in getattr(context, "sections", []):
        if not str(section.name).startswith("knowledge:") or section.dropped:
            continue
        chunk = by_id.get(str(section.name).split(":", 1)[1])
        if chunk is None:
            continue
        label = format_source_label(chunk)
        if label not in labels:
            labels.append(label)
    return labels


def validate_citations(
    answer: str,
    chunks: list[KnowledgeChunk],
    *,
    provided_labels: list[str] | None = None,
) -> CitationReport:
    """校验回答中的来源标注。

    `provided_labels` 是「本轮实际进入上下文的标注」。不传时退化为全部
    `chunks` —— 这只适合单元测试或调用方已经确认 chunks 全部进入上下文的场景。
    """
    allowed = _allowed_map(chunks, provided_labels)
    report = CitationReport(provided_labels=sorted(allowed.keys()))

    matches = list(CITATION_PATTERN.finditer(answer or ""))
    exact_spans = {(match.start(), match.end()) for match in matches}
    for match in CITATION_LIKE_PATTERN.finditer(answer or ""):
        if (match.start(), match.end()) not in exact_spans:
            report.malformed_labels.append(match.group(0))

    counts: dict[str, int] = {}
    cited_documents: list[str] = []
    for match in matches:
        raw = match.group(0)
        name = match.group("name").strip()
        index = int(match.group("index"))
        key = f"资料《{name}》#{index}"
        counts[key] = counts.get(key, 0) + 1
        chunk = allowed.get(key)
        claim = _claim_sentence(answer or "", match.start(), match.end())
        if chunk is None:
            report.citations.append(
                Citation(
                    raw=raw,
                    document_name=name,
                    chunk_index=index,
                    valid=False,
                    chunk_id=None,
                    document_id=None,
                    claim=claim,
                    source_excerpt=None,
                    lexical_support="none",
                )
            )
            report.invalid_labels.append(raw)
            continue

        support, overlap, numeric_conflict = _lexical_support(claim, chunk.content)
        report.citations.append(
            Citation(
                raw=raw,
                document_name=name,
                chunk_index=index,
                valid=True,
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                claim=claim,
                source_excerpt=_excerpt(chunk.content, overlap),
                lexical_support=support,
                overlap_terms=tuple(overlap),
                numeric_conflict=numeric_conflict,
            )
        )
        if name not in cited_documents:
            cited_documents.append(name)

    report.duplicate_labels = sorted(
        label for label, count in counts.items() if count > 1
    )
    report.cited_documents = cited_documents
    return report


def _allowed_map(
    chunks: list[KnowledgeChunk], provided_labels: list[str] | None
) -> dict[str, KnowledgeChunk]:
    by_label = {
        format_source_label(chunk): chunk
        for chunk in chunks
        if chunk.document_name and chunk.chunk_index is not None
    }
    if provided_labels is None:
        return by_label
    allowed: dict[str, KnowledgeChunk] = {}
    for label in provided_labels:
        label = label.strip()
        if label.startswith("[") and label.endswith("]"):
            label = label[1:-1]
        chunk = by_label.get(label)
        if chunk is not None:
            allowed[label] = chunk
    return allowed


def _claim_sentence(answer: str, start: int, end: int) -> str:
    """取标注所支持的那一句。

    中文引用通常写在句号之后：`……。 [资料《文件》#0]`。如果只按「标注所在的
    两个句读之间」取，拿到的会是一个只有标注的空句，词面支持度必然为 none。
    因此当标注独占一段时，回退到前一个非空句子；标注夹在句子中间时仍取整句。
    """
    spans: list[tuple[int, int]] = []
    left = 0
    for match in _SENTENCE_BREAK.finditer(answer):
        spans.append((left, match.start()))
        left = match.end()
    spans.append((left, len(answer)))

    index = 0
    for position, (span_start, span_end) in enumerate(spans):
        if span_start <= start < span_end:
            index = position
            break

    def meaningful(span: tuple[int, int]) -> str:
        text = CITATION_PATTERN.sub("", answer[span[0] : span[1]]).strip()
        # 模型常把标注单独写成 `> 来源：[资料…]`、`（来源：[资料…]）`、`—— [资料…]`。
        # 去掉引用、Markdown/标点和来源引导词后若什么都不剩，就不是「被支持的那句话」，
        # 应回退到上一句；否则 claim 会退化成标注装饰本身，支持度必然 none。
        text = _CLAIM_DECORATION.sub("", text)
        text = re.sub(r"^(来源|引用|参考|出处|依据|资料来源|引自)", "", text)
        return text.strip()

    if meaningful(spans[index]):
        return answer[spans[index][0] : spans[index][1]].strip()
    for position in range(index - 1, -1, -1):
        if meaningful(spans[position]):
            return answer[spans[position][0] : spans[position][1]].strip()
    return answer[spans[index][0] : spans[index][1]].strip()


def _normalize_cjk_numbers(text: str) -> str:
    """把 0–99 的简单中文数字换成阿拉伯数字，减少「一次 / 1 次」误报。

    只用于数字冲突检查，不改变展示文本；复杂数字（如「一百二十三」）保持原样。
    """

    def convert(match: re.Match[str]) -> str:
        raw = match.group(0)
        if "十" in raw:
            tens, _, ones = raw.partition("十")
            if len(tens) > 1 or len(ones) > 1:
                return raw
            tens_value = _CJK_DIGITS.get(tens, 1) if tens else 1
            ones_value = _CJK_DIGITS.get(ones, 0) if ones else 0
            return str(tens_value * 10 + ones_value)
        if len(raw) == 1:
            return str(_CJK_DIGITS[raw])
        return raw

    return _CJK_NUMBER_RUN.sub(convert, text)


def _lexical_support(claim: str, source: str) -> tuple[str, list[str], bool]:
    """词面支持信号；返回 `(level, overlap_terms, numeric_conflict)`。

    这是启发式，不是事实判断：
    - `strong` / `weak` 表示标注句与片段有较多 / 少量共同词；
    - `none` 表示几乎没有共同词，值得人工复核；
    - `numeric_conflict` 表示标注句里的数字在片段里找不到。
    """
    claim_text = _BARE_REFERENCE.sub("", CITATION_PATTERN.sub("", claim))
    claim_numbers = set(_NUMBER.findall(_normalize_cjk_numbers(claim_text)))
    source_numbers = set(_NUMBER.findall(_normalize_cjk_numbers(source)))
    numeric_conflict = bool(claim_numbers - source_numbers) if claim_numbers else False

    claim_tokens = _tokens(claim_text)
    source_tokens = _tokens(source)
    overlap = sorted(claim_tokens & source_tokens)
    ratio = len(overlap) / max(1, len(claim_tokens))
    if numeric_conflict:
        return "numeric_conflict", overlap[:12], True
    if ratio >= 0.35:
        return "strong", overlap[:12], False
    if ratio >= 0.15:
        return "weak", overlap[:12], False
    return "none", overlap[:12], False


def _tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for run in _CJK_RUN.findall(text):
        for index in range(len(run) - 1):
            bigram = run[index : index + 2]
            if bigram not in _STOPWORDS:
                tokens.add(bigram)
    for word in _ASCII_WORD.findall(text.lower()):
        if len(word) >= 2 and word not in _STOPWORDS:
            tokens.add(word)
    return tokens


def _excerpt(source: str, overlap: list[str], limit: int = 240) -> str:
    """优先截取命中词附近的内容；没有命中就取开头，方便人快速核对。"""
    if not source:
        return ""
    if not overlap:
        return source[:limit]
    positions = [source.find(term) for term in overlap if source.find(term) >= 0]
    start = max(0, min(positions) - 60) if positions else 0
    return source[start : start + limit]
