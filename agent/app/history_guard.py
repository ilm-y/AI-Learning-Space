"""显式复盘回答的证据约束（阶段 5.3.2）。

为什么需要这一层：显式复盘问题（「我之前错在哪」「后来怎么纠正的」）的回答
最容易出现两类无依据断言 —— 补写用户的心理过程（「你意识到」「你自己推导出」），
以及断言记录里根本没有的外部资料使用情况（「没有外部提示」「完全靠自己」）。
Prompt 已经写了这些规则，但只靠 prompt 时模型仍会偶尔补一个合理的故事。

而 SSE 是边生成边推送的：回答一旦推到前端，事后校验无法撤回。所以本模块只
服务**显式复盘**这一小类问题，在模型生成完成之后、Runtime 推送之前检查：

1. 回答里的历史断言能否由 trajectories / revisions / evidences 里可核对的事实支撑；
2. 当前没有历史轨迹时，回答有没有把「没有可见记录」写成「过去没有发生过」，
   或者从当前状态反推过去；
3. 记录条数、PARTIAL 状态、显式批注来源有没有被改写或遗漏。

它不是关键词清洗：记录原文里出现过的表述可以放行；检查的是**断言是否被记录支撑**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from app.schemas import (
    EvidenceCorrectness,
    EvidenceType,
    LearningTrajectory,
    UnderstandingStatus,
)
from app.state.agent_state import AgentState

_STATUS_LABEL = {
    UnderstandingStatus.UNKNOWN: "未接触",
    UnderstandingStatus.EXPOSED: "接触过",
    UnderstandingStatus.PARTIAL: "部分理解",
    UnderstandingStatus.DEMONSTRATED: "已能讲清",
    UnderstandingStatus.MISCONCEPTION: "存在误区",
}

_CORRECTNESS_LABEL = {
    EvidenceCorrectness.CORRECT: "答对",
    EvidenceCorrectness.INCORRECT: "答错",
    EvidenceCorrectness.PARTIAL: "部分正确",
    EvidenceCorrectness.UNKNOWN: "未判定",
}

_EVIDENCE_TYPE_LABEL = {
    EvidenceType.USER_EXPLANATION: "用户解释",
    EvidenceType.OBSERVATION: "观察",
    EvidenceType.TEST_RESULT: "测试结果",
    EvidenceType.USER_CORRECTION: "用户纠正批注",
    EvidenceType.HIGHLIGHT_SIGNAL: "批注信号",
}

_SOURCE_TYPE_LABEL = {
    "MESSAGE": "消息",
    "HIGHLIGHT": "批注",
    "TEST_ANSWER": "测试回答",
}

# 内省 / 心理过程。记录没有写出的，不能替用户补写。
_PROCESS_TERMS = (
    "意识到",
    "想通",
    "想明白",
    "顿悟",
    "领悟",
    "恍然大悟",
    "推导",
    "推演",
    "修正",
    "发现",
    "总结出",
    "归纳出",
    "摸索",
    "琢磨",
    "进一步想到",
    "举一反三",
)

_INTERNAL_PROCESS_RE = re.compile(
    r"(突然|忽然)?(意识到|想通|想明白|顿悟|领悟|恍然大悟)"
    r"|(自己|自行|独立|主动)(推导|推演|修正|发现|总结|归纳|摸索|琢磨)"
    r"|进一步想到|抓住(了)?(核心|要点|关键)|举一反三"
    r"|(很可能|可能|大概|也许|应该是|估计是|想必)[^。！？!?\n]{0,40}"
    r"(通过|借助|依靠|基于|由于|因为)"
    r"|(通过|借助|依靠)[^。！？!?\n]{0,24}"
    r"(重读|重新阅读|反复阅读|对比|迁移|类推|联想)"
    r"|你(之所以|之所以能)[^。！？!?\n]{0,24}(因为|是由于|是因为|源于)"
)

# internal_process 的语境识别（KI-2）。
#
# 「意识到」「想通」等词出现在否定 / 拒绝、引用或假设语境里时，模型并不是
# 在断言用户过去的心理过程。按字面命中会触发无谓重生成；重生成仍命中时
# 还会回退掉合法回答（Step 4 run 15、Batch 6 S5-B-1）。
#
# 判定只看同一句、短语前 _INTERNAL_CONTEXT_WINDOW 个字符，不跨句。它与
# partial_upgrade 的纯否定 / 对比规则是两套语义：这里还要识别引用与假设，
# 所以单独实现，不复用 _phrase_in_negated_context。
_INTERNAL_CONTEXT_WINDOW = 20
_INTERNAL_NEGATION_RE = re.compile(
    r"我不会|我不能|不能|不应该|不该|不认为|不觉得|不是|并非|并没有|"
    r"没有说|不说|谈不上|不意味着|不代表|不等于|别以为|不要以为|"
    r"我如果给你补|那是我编的|不是发生过的事实"
)
_INTERNAL_HYPOTHESIS_RE = re.compile(
    r"假设|如果|假如|倘若|若是|要是|按理说|比如|例如"
)
# 中文引号 + 英文双引号。『』也纳入，因为 Step 4 run 15 的真实首答用的
# 就是『你意识到……』。
_QUOTE_SPAN_RES = (
    re.compile(r"「[^」]*」"),
    re.compile(r"『[^』]*』"),
    re.compile(r"“[^”]*”"),
    re.compile(r'"[^"]*"'),
)
_QUOTE_REFERENCE_RE = re.compile(
    r"所谓|你说的|你当时说的|你曾说|记录里|记录中|原话|原文|引用|复述"
)

# 外部帮助 / 资料使用。只有记录明确写了才可以说「没有」。
_EXTERNAL_ABSENCE_RE = re.compile(
    r"(没有|未|无)[^。！？!?\n]{0,12}(外部|他人|别人|老师)[^。！？!?\n]{0,8}"
    r"(提示|资料|文档|帮助|指导|介入|参与|参考)"
    r"|(没有|未|无)[^。！？!?\n]{0,12}(提示|资料|文档|帮助|指导|介入|参与)"
    r"[^。！？!?\n]{0,6}(介入|参与|帮助|提示|指导)"
    r"|完全(靠|依靠|凭)(自己|自学)"
    r"|独立(完成|解决|想通|推导)"
    r"|无人(指导|帮助|提示)"
    r"|没有(查|查阅|查过|参考|看)(过)?(资料|文档|书|答案)"
    r"|没(有)?(借助|使用|依赖|参考)(过)?(任何)?(外部|他人)?"
    r"(提示|资料|文档|工具|帮助)"
    r"|不靠(任何)?(外部|他人|帮助|资料)"
)

# 即使同一句里在说明「记录没有写是否借助资料」，这些说法仍然是
# 对过去事实的肯定断言，不能被不确定性说明一并放行。
_STRONG_EXTERNAL_ABSENCE_RE = re.compile(
    r"完全(靠|依靠|凭)(自己|自学)"
    r"|独立(完成|解决|想通|推导)"
    r"|无人(指导|帮助|提示)"
    r"|没有(查|查阅|查过|参考|看)(过)?(资料|文档|书|答案)"
)

# 记录完整性：把多条证据说成「唯一 / 只有一条」。
_ONLY_EVIDENCE_RE = re.compile(
    r"(唯一|仅有|只有|仅)[^。！？!?\n]{0,8}(一|1)?[^。！？!?\n]{0,4}证据"
)

# 记录条数断言：「答对了三道题」「有 3 条记录」。断言数不能超过实际记录数。
_COUNT_CLAIM_RE = re.compile(
    r"(?P<num>[0-9]+|[一二两三四五六七八九十]+)\s*"
    r"(?P<unit>道|条|次|个)\s*(?P<kind>题|证据|记录|测试)"
)

# 把 PARTIAL 升级成绝对判断。
_ABSOLUTE_CORRECT_RE = re.compile(
    r"(完全|彻底|全部)(答对|正确|掌握|理解)|完全正确|完全答对|全部答对"
)
_WRONG_ANSWER_RE = re.compile(r"(答错|回答错误|答错了)")

# partial_upgrade 的否定 / 对比语境识别。
#
# 「不是完全答错，也不是完全答对」这类句子在语义上正是否认绝对判断，
# 但字面包含「答错」「完全答对」。如果直接按字面命中，会把合法复盘
# 判成「把部分正确升级成答错 / 完全答对」，触发无谓重生成，重生成仍
# 被误判时还会回退掉用户显式要求引用的原句。
#
# 判定规则（只作用于 partial_upgrade，不影响其他 issue kind）：
#   1. 短语前 12 个字符内最近的一个标记是否定词（不是 / 并非 / 并没有 /
#      也不算 …），则该短语不参与判定；
#   2. 若最近标记是肯定对比「而是」，则该短语仍按肯定判断处理；
#   3. 「不是 …… 也不是 ……」并列否定结构内的短语同样跳过。
_NEGATION_MARKERS_RE = re.compile(
    r"不是|并非|并没有|并未|并不表示|并不代表|并不等于|并不完全"
    r"|没有|不表示|不代表|不等于|也不算|不算|谈不上|不能算|称不上|绝非"
)
_CONTRAST_MARKERS_RE = re.compile(r"而是")
_PARALLEL_NEGATION_RE = re.compile(
    r"(?P<first>不是|并非)[^。！？!?\n]{0,40}(?P<second>也不是|也并非|也并不)"
)
_NEGATION_WINDOW = 12
_SENTENCE_BOUNDARY_CHARS = "。！？!?\n"

# 「记录没有写 / 无法确认是否借助过外部资料」是对记录状态的真实说明，
# 不是「没有借助」的断言。它可能包含「没有……外部资料」的字面片段。
_RECORD_UNCERTAINTY_RE = re.compile(
    r"没有记录|未记录|记录(?:里|中)?(?:都|也|均|全)?(?:没有|未|无法)|"
    r"无法确认|不能确认|看不出|没有写明|没有说明|未说明"
)

# 从句级判断：疑问 / 选择问 / 「是否」列举是在询问记录，不是在断言过去。
# 例：「那次纠正是独立完成的还是有人指出——同样没有记录」「是否意识到问题的根源」。
_CLAUSE_SPLIT_RE = re.compile(r"[，,；;。！？!?——\n]")
_INTERROGATIVE_CLAUSE_RE = re.compile(
    r"有没有|是否|是不是|还是|或者|到底|究竟|怎么|为什么|如何|"
    r"哪一?[个种些]|吗[\s？?。]*$"
)
_UNCERTAINTY_CLAUSE_RE = re.compile(
    r"没有记录|未记录|记录(?:里|中)?(?:都|也|均|全)?(?:没有|未|无法)|"
    r"无法确认|不能确认|看不出|没有写明|没有说明|未说明|不清楚|不知道"
)

# 当前没有历史轨迹时：否认过去 / 从当前状态反推过去。
_NO_HISTORY_DENIAL_RE = re.compile(
    r"你(以前|之前|过去|当时)[^。！？!?\n]{0,10}(没有|从未|从没|不曾)"
    r"[^。！？!?\n]{0,10}(错误|误解|问题|犯过|出现)"
    r"|你(以前|之前|过去)(从未|从没|没有)[^。！？!?\n]{0,8}(错|误解|问题)"
    r"|你(一直|本来就|一直都)(是)?(正确|理解|掌握)"
    r"|并不存在[^。！？!?\n]{0,8}(误解|错误|问题)"
    r"|你没有(犯过|出现过)[^。！？!?\n]{0,6}(错|误解)"
)

_NO_HISTORY_INFERENCE_RE = re.compile(
    r"你(以前|之前|过去|当时)[^。！？!?\n]{0,12}"
    r"(可能|大概|也许|应该|想必|估计|显然)[^。！？!?\n]{0,30}"
    r"(理解|知道|学过|接触|掌握|认为|以为|想|推导|纠正)"
    r"|(说明|可见|表明)你(以前|之前|过去|当时)[^。！？!?\n]{0,20}"
    r"(已经|就|一直|没有|可能|应该)"
    r"|你(之前|以前)(就)?已经(理解|掌握|知道)"
    # 「你已经越过 / 避开了某个误区」同样是在断言过去，不能由当前答对反推。
    r"|你(也|已经|已)?[^。！？!?\n]{0,4}"
    r"(越过|避开|避免|克服|走出|摆脱|改正|纠正)"
    r"[^。！？!?\n]{0,12}(误区|混淆|错误|问题|陷阱)"
)

# 没有轨迹时必须出现的「信息不足」表达。
_NO_HISTORY_ACK_MARKERS = (
    "可见",
    "记录",
    "历史",
    "没有检索到",
    "没有保存",
    "没有看到",
    "看不到",
    "无法确认",
    "不能确认",
    "不能断言",
    "当前状态",
    "目前",
)

# 有显式批注来源时，回答至少要把这条来源讲出来，不能只讲「错误 → 正确」。
_ANNOTATION_MARKERS = ("批注", "标注", "高亮", "勾画", "划出", "标记")


@dataclass(frozen=True, slots=True)
class HistoryIssue:
    kind: str
    excerpt: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "excerpt": self.excerpt, "reason": self.reason}


def check_history_answer(answer: str, state: AgentState) -> list[HistoryIssue]:
    """检查一段复盘回答里的断言是否被记录支撑。"""
    text = answer or ""
    if not text.strip():
        return [HistoryIssue("empty_answer", "", "回答为空")]

    corpus = _recorded_corpus(state)
    sentences = _sentences(text)
    issues: list[HistoryIssue] = []

    for sentence in sentences:
        issues.extend(_check_internal_process(sentence, corpus))
        issues.extend(_check_external_absence(sentence, corpus))

    # 「有记录」不等于「有轨迹」：按需召回的 evidence 也是记录。
    # 只看 trajectories 会把「只有 evidence」误判成「没有历史」。
    has_records = bool(state.trajectories) or bool(_recorded_evidences(state))
    if has_records:
        issues.extend(_check_record_facts(state, text))
    if not state.trajectories:
        # 没有轨迹时仍要防止否认过去 / 从当前状态反推；
        # 但只有在完全没有记录时，才要求回答先声明「信息不足」。
        issues.extend(
            _check_no_history(sentences, text, require_ack=not has_records)
        )

    return _dedupe(issues)


def retry_instruction(issues: list[HistoryIssue]) -> str:
    """一次受限重生成用的指令。只描述问题类别，不把原回答塞回去。"""
    guidance = {
        "internal_process": (
            "删除对用户心理过程、学习方式或变化原因的推断；记录没有写出的，"
            "写成「记录里看不出原因」。"
        ),
        "external_absence": (
            "不要断言用户没有使用外部提示、资料或帮助；记录没有写，就说明无法确认。"
        ),
        "record_completeness": "按记录的实际条数陈述，不要把多条证据概括成「唯一 / 只有一条」。",
        "partial_upgrade": "「部分正确」只能写成部分正确，不能升级成「答错」或「完全答对」。",
        "missing_source_mapping": "记录里有显式批注时，要说明这条批注记录，不能只讲错误和正确答案。",
        "no_history_denial": "当前没有历史轨迹：不要断言用户过去没有出现过这个误解。",
        "no_history_inference": "当前没有历史轨迹：不要从当前状态反推用户过去怎么想、怎么学过。",
        "missing_history_acknowledgement": (
            "当前没有历史轨迹：先说明当前可见信息不足以确认过去，再回答。"
        ),
    }
    lines = [
        "上一次回答包含当前记录无法支撑的断言。请整段重写，只陈述上下文里"
        "「学习轨迹 / 当前认知」可核对的事实："
    ]
    seen: list[str] = []
    for issue in issues:
        rule = guidance.get(issue.kind)
        if rule and rule not in seen:
            seen.append(rule)
            lines.append(f"- {rule}")
    lines.append(
        "- 无法确认的地方直接写「无法确认」，不要补一个合理的故事；"
        "保留对用户本来问题的回答，不要改写成泛泛的知识讲解。"
    )
    return "\n".join(lines)


def build_history_fallback(
    state: AgentState, *, first_answer: str | None = None
) -> str:
    """校验仍不通过时的确定性回退：只复述记录里可核对的事实。

    不包含任何内部 id、证据编号或模型推断，因此它一定不会比原始回答更不可靠。

    ``first_answer`` 是首答全文。用户在本轮问题里显式要求「引用当时的原话 /
    原文」时，首答里与历史记录逐字一致的原句不能被回退文本静默覆盖：
    这些句子本来就是可核对的事实，回退时优先保留它们，再拼接确定性文本。
    没有显式引用要求、或首答里找不到与记录一致的句子时，行为与原来一致。
    """
    base = _base_history_fallback(state)
    if not first_answer or not _quote_requested(state.user_message):
        return base
    quote = _preserved_quote_segment(state, first_answer)
    if not quote:
        return base
    return (
        "你要求我引用当时的原话。下面先保留本次回答里与历史记录逐字一致的原句：\n\n"
        f"{quote}\n\n"
        "以下只复述记录里可核对的事实：\n\n"
        f"{base}"
    )


def _base_history_fallback(state: AgentState) -> str:
    if state.trajectories:
        return _trajectory_fallback(state.trajectories)
    evidences = _recorded_evidences(state)
    if evidences:
        return _evidence_only_fallback(evidences)
    return _no_history_fallback(state)


_QUOTE_REQUEST_RE = re.compile(r"引用|复述|原话|原文|原句")
_SYNTHETIC_MARKER_RE = re.compile(r"【[^】]*】")
_QUOTED_SEGMENT_RE = re.compile(
    r"「[^」]{2,}」|『[^』]{2,}』|“[^”]{2,}”|\"[^\"]{2,}\""
)
_QUOTE_EDGE_PUNCT = "。，、；：！？!?.,;:…"
# 太短的片段（例如单个概念名）不构成「用户要引用的一句原话」。
_MIN_QUOTE_CHARS = 8


def _quote_requested(question: str | None) -> bool:
    """用户问题是否显式要求引用历史原话 / 原文。"""
    return bool(_QUOTE_REQUEST_RE.search(question or ""))


def _normalise_quote(text: str) -> str:
    """引用匹配用归一化：去掉空白 / 引号与合成标记（如 【S5-HISTORY】）。"""
    return _normalise(
        _SYNTHETIC_MARKER_RE.sub("", text or "")
    ).strip(_QUOTE_EDGE_PUNCT)


def _quote_candidates(state: AgentState) -> list[str]:
    """可以逐字引用的历史原句：历史用户消息 + 本轮可核对记录。"""
    texts: list[str] = []
    for message in state.history:
        if not message.is_user():
            continue
        texts.extend(_sentences(message.content or ""))
    texts.extend(_recorded_corpus(state))
    candidates = [_normalise_quote(text) for text in texts]
    return [item for item in candidates if len(item) >= _MIN_QUOTE_CHARS]


def _preserved_quote_segment(state: AgentState, first_answer: str) -> str:
    """首答中逐字引用了可核对历史原句的片段。

    优先保留首答里带引号的引用段（真实回答通常写成「……」）。若模型
    没有加引号、只是在句子里复述了记录原句，则用记录里的原句重建引用，
    避免把同句中的其他无依据断言一起带回回退文本。
    """
    candidates = _quote_candidates(state)
    if not candidates:
        return ""
    segments: list[str] = []
    covered: set[str] = set()
    for match in _QUOTED_SEGMENT_RE.finditer(first_answer):
        span = match.group(0)
        normalised = _normalise_quote(span)
        matched = [item for item in candidates if item in normalised]
        if matched:
            segments.append(span)
            covered.update(matched)
    answer_normalised = _normalise_quote(first_answer)
    for item in candidates:
        if item not in covered and item in answer_normalised:
            segments.append(f"「{item}」")
    return "\n".join(dict.fromkeys(segments))


# ---------- 检查 ----------


def _check_internal_process(sentence: str, corpus: list[str]) -> list[HistoryIssue]:
    issues: list[HistoryIssue] = []
    for match in _INTERNAL_PROCESS_RE.finditer(sentence):
        if _is_question_or_uncertainty(sentence, match.start(), match.end()):
            # 疑问 / 选择问 / 「是否」列举只是在问记录，不是对过去的断言。
            continue
        if _internal_process_in_skipped_context(sentence, match.start(), match.end()):
            # 否定 / 拒绝、引用、假设语境：不是在断言用户过去的心理过程。
            continue
        span = match.group(0)
        if _recorded_phrase(span, corpus):
            continue
        # 同一断言里的内省词若原样出现在记录中，视为引用记录，不判为编造。
        # 只在这个匹配片段内看，避免同一句里一句有记录的「意识到」把另一句
        # 没记录的「自己推导」也一起放行。
        if any(term in span and _recorded_phrase(term, corpus) for term in _PROCESS_TERMS):
            continue
        issues.append(
            HistoryIssue(
                "internal_process",
                _excerpt(sentence),
                f"记录没有写出的过程断言：「{span}」",
            )
        )
    return issues


def _check_external_absence(sentence: str, corpus: list[str]) -> list[HistoryIssue]:
    # 不能因为整句里出现「记录没有写」就放行所有外部帮助判断：
    # 「记录里没有写是否借助资料，你完全靠自己」后半句仍是肯定断言。
    # 因此逐匹配到所在从句判断（见 _is_question_or_uncertainty）。
    issues: list[HistoryIssue] = []
    for match in _EXTERNAL_ABSENCE_RE.finditer(sentence):
        if _is_question_or_uncertainty(sentence, match.start(), match.end()):
            # 记录是否写明外部帮助，属于「无法确认」，不是「没有帮助」的断言。
            # 同一从句里若有「完全靠自己」这类肯定断言，_STRONG_ 检查已在上方拦住。
            continue
        span = match.group(0)
        if _recorded_phrase(span, corpus):
            continue
        issues.append(
            HistoryIssue(
                "external_absence",
                _excerpt(sentence),
                "记录没有写明外部资料 / 帮助的使用情况",
            )
        )
    return issues


def _is_question_or_uncertainty(sentence: str, start: int, end: int) -> bool:
    """匹配片段所在的从句是否只是在询问 / 说明记录不足。"""
    left = 0
    right = len(sentence)
    for delimiter in _CLAUSE_SPLIT_RE.finditer(sentence):
        if delimiter.end() <= start:
            left = delimiter.end()
        elif delimiter.start() >= end:
            right = delimiter.start()
            break
    clause = sentence[left:right]
    if _INTERROGATIVE_CLAUSE_RE.search(clause):
        return True
    return bool(_UNCERTAINTY_CLAUSE_RE.search(clause))


def _internal_process_in_skipped_context(
    sentence: str, start: int, end: int
) -> bool:
    """内省词是否处在否定 / 拒绝、引用或假设语境中。

    只回看同一句内、短语前 ``_INTERNAL_CONTEXT_WINDOW`` 个字符。句子边界
    由调用方的 ``_sentences`` 保证（。！？!?\n 已切开），因此这里不会跨句。
    """
    window = sentence[max(0, start - _INTERNAL_CONTEXT_WINDOW) : start]
    if _INTERNAL_NEGATION_RE.search(window):
        return True
    if _INTERNAL_HYPOTHESIS_RE.search(window):
        return True
    return _phrase_quoted_with_reference(sentence, start, end)


def _phrase_quoted_with_reference(sentence: str, start: int, end: int) -> bool:
    """短语是否被引号包裹，且引号前带有「所谓 / 你说的 / 记录里的」等标记。"""
    for pattern in _QUOTE_SPAN_RES:
        for match in pattern.finditer(sentence):
            if not (match.start() < start and end <= match.end()):
                continue
            before = sentence[
                max(0, match.start() - _INTERNAL_CONTEXT_WINDOW) : match.start()
            ]
            if _QUOTE_REFERENCE_RE.search(before):
                return True
    return False


def internal_process_context_skips(text: str | None) -> list[str]:
    """观测用：列出因否定 / 引用 / 假设语境被跳过的内省词字面命中。

    返回值只用于实验记录与回归测试，不参与判定；与
    ``partial_upgrade_negation_skips`` 一样，它回答「语境识别实际拦下了
    多少次字面命中」。
    """
    value = text or ""
    skipped: list[str] = []
    for sentence in _sentences(value):
        for match in _INTERNAL_PROCESS_RE.finditer(sentence):
            if _internal_process_in_skipped_context(
                sentence, match.start(), match.end()
            ):
                skipped.append(match.group(0))
    return skipped


def _check_record_facts(state: AgentState, text: str) -> list[HistoryIssue]:
    issues: list[HistoryIssue] = []
    evidences = _recorded_evidences(state)

    count_match = _COUNT_CLAIM_RE.search(text)
    if count_match:
        claimed = _parse_count(count_match.group("num"))
        # 「三道题 / 三次测试」只能由测试类证据支撑；「三条记录 / 证据」
        # 才按全部记录计数。否则普通解释会把「答对三道题」蒙混过去。
        if count_match.group("kind") in {"题", "测试"}:
            pool = [
                evidence
                for evidence in evidences
                if evidence.evidence_type is EvidenceType.TEST_RESULT
            ]
            unit = "次测试"
        else:
            pool = evidences
            unit = "条记录"
        if claimed is not None and claimed > len(pool):
            issues.append(
                HistoryIssue(
                    "record_completeness",
                    _excerpt(count_match.group(0)),
                    f"回答声称有 {claimed} {unit}，实际只有 {len(pool)} {unit}",
                )
            )

    if len(evidences) > 1:
        match = _ONLY_EVIDENCE_RE.search(text)
        if match:
            issues.append(
                HistoryIssue(
                    "record_completeness",
                    _excerpt(match.group(0)),
                    f"本轮记录里有 {len(evidences)} 条证据，不是唯一一条",
                )
            )

    correctness = {evidence.correctness for evidence in evidences}
    absolute_matches = _effective_partial_upgrade_matches(text, _ABSOLUTE_CORRECT_RE)
    if absolute_matches and EvidenceCorrectness.CORRECT not in correctness:
        issues.append(
            HistoryIssue(
                "partial_upgrade",
                _excerpt(absolute_matches[0].group(0)),
                "记录里没有完全答对的证据",
            )
        )
    wrong_matches = _effective_partial_upgrade_matches(text, _WRONG_ANSWER_RE)
    if (
        wrong_matches
        and EvidenceCorrectness.INCORRECT not in correctness
        and EvidenceCorrectness.PARTIAL in correctness
    ):
        issues.append(
            HistoryIssue(
                "partial_upgrade",
                _excerpt(wrong_matches[0].group(0)),
                "记录只判定为部分正确，不能改写成答错",
            )
        )

    has_annotation = any(
        evidence.evidence_type is EvidenceType.USER_CORRECTION
        or any(source.source_type == "HIGHLIGHT" for source in evidence.sources)
        for evidence in evidences
    )
    if has_annotation and not any(marker in text for marker in _ANNOTATION_MARKERS):
        issues.append(
            HistoryIssue(
                "missing_source_mapping",
                _excerpt(text),
                "记录里有显式批注来源，回答没有提到这条记录",
            )
        )
    return issues


def _effective_partial_upgrade_matches(
    text: str, pattern: re.Pattern[str]
) -> list[re.Match[str]]:
    """返回真正构成「升级」的匹配，排除否定 / 对比语境中的字面命中。"""
    return [
        match
        for match in pattern.finditer(text)
        if not _phrase_in_negated_context(text, match.start(), match.end())
    ]


def _phrase_in_negated_context(text: str, start: int, end: int) -> bool:
    """短语是否处于否定 / 对比语境。

    只看同一句内、短语前 ``_NEGATION_WINDOW`` 个字符里**最近**的标记：
    「不是 / 并非 / 并没有 / 也不算」是否定；「而是」是肯定对比。
    这样既能识别「不是完全答错」，也不会把「不是完全答错，而是完全
    答对」里的后半句一起放行。
    """
    sentence_start = 0
    for char in _SENTENCE_BOUNDARY_CHARS:
        sentence_start = max(sentence_start, text.rfind(char, 0, start) + 1)
    window = text[max(sentence_start, start - _NEGATION_WINDOW) : start]
    negation_end = max(
        (match.end() for match in _NEGATION_MARKERS_RE.finditer(window)),
        default=-1,
    )
    contrast_end = max(
        (match.end() for match in _CONTRAST_MARKERS_RE.finditer(window)),
        default=-1,
    )
    if negation_end > contrast_end:
        return True
    return _in_parallel_negation(text, start, end)


def _in_parallel_negation(text: str, start: int, end: int) -> bool:
    """短语是否位于「不是 …… 也不是 ……」并列否定结构中。"""
    for match in _PARALLEL_NEGATION_RE.finditer(text):
        # 两个否定词之间的部分（「不是 A，也不是 B」里的 A）。
        if match.start() <= start < match.end():
            return True
        # 第二个否定词之后紧跟的并列项（B）。
        second_end = match.end("second")
        if second_end <= start <= second_end + _NEGATION_WINDOW:
            return True
    return False


def partial_upgrade_negation_skips(text: str | None) -> list[str]:
    """观测用：列出因否定 / 对比语境被跳过的字面短语。

    返回值只用于实验记录（``partialUpgradeNegationSkipped``），
    不参与判定。它回答「否定语境识别实际拦下了多少次字面命中」。
    """
    value = text or ""
    skipped: list[str] = []
    for pattern in (_ABSOLUTE_CORRECT_RE, _WRONG_ANSWER_RE):
        for match in pattern.finditer(value):
            if _phrase_in_negated_context(value, match.start(), match.end()):
                skipped.append(match.group(0))
    return skipped


def _check_no_history(
    sentences: list[str], text: str, *, require_ack: bool = True
) -> list[HistoryIssue]:
    issues: list[HistoryIssue] = []
    for sentence in sentences:
        for pattern, kind, reason in (
            (_NO_HISTORY_DENIAL_RE, "no_history_denial", "当前没有历史轨迹，不能断言过去没有错误"),
            (
                _NO_HISTORY_INFERENCE_RE,
                "no_history_inference",
                "当前没有历史轨迹，不能从当前状态反推过去",
            ),
        ):
            match = pattern.search(sentence)
            if match:
                issues.append(
                    HistoryIssue(kind, _excerpt(sentence), reason)
                )
    if require_ack and not any(marker in text for marker in _NO_HISTORY_ACK_MARKERS):
        issues.append(
            HistoryIssue(
                "missing_history_acknowledgement",
                _excerpt(text),
                "没有历史轨迹时，回答必须先说明当前可见信息不足",
            )
        )
    return issues


# ---------- 回退文本 ----------


def _trajectory_fallback(trajectories: list[LearningTrajectory]) -> str:
    lines = ["我只能按当前记录复述可以核对的部分，不推断记录之外的经过。"]
    for trajectory in trajectories:
        lines.extend(["", f"**{trajectory.concept_name}**"])
        status = _STATUS_LABEL.get(
            trajectory.current_status, trajectory.current_status.value
        )
        lines.append(f"- 当前状态：{status}")
        if trajectory.current_misconceptions:
            lines.append(
                "- 当前仍存在的误区：" + "；".join(trajectory.current_misconceptions)
            )

        historical: list[str] = []
        for revision in trajectory.revisions:
            for misconception in revision.misconceptions:
                if misconception.text not in historical:
                    historical.append(misconception.text)
        if historical:
            lines.append(
                "- 记录中的错误理解："
                + "；".join(f"「{text}」" for text in historical)
            )

        if trajectory.evidences:
            lines.append("- 可以核对的记录（按时间）：")
            for evidence in trajectory.evidences:
                lines.append("  - " + _render_evidence(evidence))

        correct = [
            evidence
            for evidence in trajectory.evidences
            if evidence.correctness is EvidenceCorrectness.CORRECT
        ]
        if correct:
            lines.append(f"- 后续正确记录：{len(correct)} 条。")

    lines.extend(
        [
            "",
            "这些记录能说明你的表述发生过哪些变化；但记录没有写你为什么改变看法，"
            "也没有记录是否借助过外部提示或资料，这两点无法确认。",
        ]
    )
    return "\n".join(lines)


def _no_history_fallback(state: AgentState) -> str:
    names = [item.concept_name for item in state.understandings if item.has_content]
    names = names or state.concept_names
    subject = "、".join(names) if names else "这个问题"
    lines = [
        f"当前可见的记录里，没有关于{subject}的过往错误表述、显式批注、测试回答或历史版本。",
        "因此我无法确认你过去是否出现过这个误解，也不能断言你过去没有出现过。",
    ]
    for understanding in state.understandings:
        if not understanding.has_content:
            continue
        status = _STATUS_LABEL.get(understanding.status, understanding.status.value)
        lines.append(f"- {understanding.concept_name} 的当前状态：{status}")
        if understanding.known_parts:
            lines.append("  - 已经理解：" + "；".join(understanding.known_parts))
        if understanding.misconceptions:
            lines.append(
                "  - 当前仍存在的误区："
                + "；".join(item.text for item in understanding.misconceptions)
            )
    lines.extend(
        [
            "",
            "记录里也没有你之前如何学习这个概念的经过；这部分无法补写。",
        ]
    )
    return "\n".join(lines)


def _evidence_only_fallback(evidences) -> str:
    """没有 Revision、只有按需 evidence 时的确定性回退。

    不能套用 `_no_history_fallback` —— 那会对着一条真实测试记录说
    「没有关于这个主题的过往错误表述、测试回答」。只复述证据本身。
    """
    lines = ["我只能按当前可见的记录复述可以核对的部分，不推断记录之外的原因或经过。"]
    lines.append("")
    lines.append("- 可以核对的记录（按时间）：")
    for evidence in sorted(
        evidences, key=lambda item: item.created_at or datetime.min
    ):
        lines.append("  - " + _render_evidence(evidence))
    lines.extend(
        [
            "",
            "这些记录能说明当时发生了什么；但记录没有写你为什么这样回答，"
            "也没有记录是否借助过外部提示或资料，这两点无法确认。",
        ]
    )
    return "\n".join(lines)


def _render_evidence(evidence) -> str:
    correctness = _CORRECTNESS_LABEL.get(
        evidence.correctness, evidence.correctness.value
    )
    evidence_type = _EVIDENCE_TYPE_LABEL.get(
        evidence.evidence_type, evidence.evidence_type.value
    )
    sources = "、".join(
        _SOURCE_TYPE_LABEL.get(source.source_type, source.source_type)
        for source in evidence.sources
    )
    parts = [
        _format_time(evidence.created_at),
        f"[{correctness}/{evidence_type}]",
        f"来源：{sources}" if sources else "",
        f"原记录：「{evidence.observation}」",
    ]
    return " ｜ ".join(part for part in parts if part)


# ---------- 工具 ----------


def _recorded_evidences(state: AgentState) -> list:
    """本轮可以核对的所有证据：轨迹关联的 + 按需召回的。

    同一 id 只保留一条，避免轨迹与 state.evidences 重叠时重复计数。
    """
    seen: set[int] = set()
    result = []
    for evidence in [
        *[item for trajectory in state.trajectories for item in trajectory.evidences],
        *state.evidences,
    ]:
        if evidence.id in seen:
            continue
        seen.add(evidence.id)
        result.append(evidence)
    return result


def _parse_count(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text == "十":
        return 10
    if "十" in text:
        tens, _, ones = text.partition("十")
        if tens and tens not in digits:
            return None
        if ones and ones not in digits:
            return None
        return (digits.get(tens, 1) if tens else 1) * 10 + (digits.get(ones, 0) if ones else 0)
    if all(char in digits for char in text):
        return int("".join(str(digits[char]) for char in text)) if len(text) == 1 else None
    return None


def _recorded_corpus(state: AgentState) -> list[str]:
    texts: list[str] = []
    for understanding in state.understandings:
        if understanding.summary:
            texts.append(understanding.summary)
        texts.extend(understanding.known_parts)
        texts.extend(item.text for item in understanding.misconceptions)
        if understanding.latest_user_explanation:
            texts.append(understanding.latest_user_explanation)
    for trajectory in state.trajectories:
        texts.extend(trajectory.current_misconceptions)
        for revision in trajectory.revisions:
            if revision.summary:
                texts.append(revision.summary)
            texts.extend(revision.known_parts)
            texts.extend(item.text for item in revision.misconceptions)
            if revision.user_explanation:
                texts.append(revision.user_explanation)
        texts.extend(evidence.observation for evidence in trajectory.evidences)
    # 按需召回的 evidence 也可能包含用户原话；它同样是可核对记录。
    texts.extend(evidence.observation for evidence in state.evidences)
    return [_normalise(text) for text in texts if text]


def _recorded_phrase(span: str, corpus: list[str]) -> bool:
    normalised = _normalise(span)
    if not normalised:
        return False
    return any(normalised in item for item in corpus)


def _normalise(text: str) -> str:
    return re.sub(r"[\s*_`「」『』\"'“”‘’]+", "", text or "")


def _sentences(text: str) -> list[str]:
    parts = re.split(r"[。！？!?\n]+", text)
    return [part.strip() for part in parts if part.strip()]


def _excerpt(text: str, limit: int = 80) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    return compact if len(compact) <= limit else compact[:limit] + "…"


def _dedupe(issues: list[HistoryIssue]) -> list[HistoryIssue]:
    seen: set[tuple[str, str]] = set()
    unique: list[HistoryIssue] = []
    for issue in issues:
        key = (issue.kind, issue.excerpt)
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    return unique


def _format_time(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "时间未记录"
