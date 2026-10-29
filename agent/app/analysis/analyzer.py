"""LearningAnalyzer —— 把一轮对话变成「可以写进学习闭环的候选」。

它复用 Chat 链路的两个已有能力（概念识别、只读检索），但**不复用 Context Builder**：
分析要的是「这一轮暴露了什么」，不是「下一轮该怎么讲」，两者需要的上下文不同。

```text
AnalyzeRequest
    ↓
detect_concepts（与 Chat 同一套规则，保证两边看到的概念一致）
    ↓
读 Understanding / Memory / Evidence（只读，带用户自己的 JWT）
    ↓
LLM 结构化输出  ── 解析失败就报错，不静默降级成「没有学习信号」
    ↓
sanitize（裁剪 / 校验 conceptId / 收敛枚举）
    ↓
AnalyzeResult
```

**Mock LLM 是一个特例**：它的 `complete()` 只会回读上下文，不会输出 JSON。
如果走通用解析，离线模式下每次分析都会失败。所以这里按适配器名分流到
`_heuristic_analysis()` —— 它不是「假装有智能」，而是一份确定性的最小分析，
让整条写入链路（证据 → 理解 → 记忆 → 验证）在没有 API Key 时仍然可测。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from pydantic import ValidationError

from app.analysis.prompts import ANALYSIS_SYSTEM_INSTRUCTION
from app.config import Settings
from app.llm.base import ChatMessage, LLMAdapter
from app.retriever import build_retriever, detect_concepts
from app.schemas import (
    AnalyzeRequest,
    AnalyzeResult,
    Concept,
    EvidenceCandidate,
    EvidenceCorrectness,
    EvidenceItem,
    EvidenceType,
    MemoryCandidate,
    MemoryItem,
    MemoryReconcileAction,
    MemoryScopeType,
    MemoryType,
    QuestionType,
    Understanding,
    UnderstandingProposal,
    VerificationAction,
    VerificationDecision,
    VerificationTrigger,
)
from app.trace import RunTrace, TraceEventType

log = logging.getLogger(__name__)

ANALYZER_VERSION = "agent-v1"

# 输出体积上限。这些数字同时是「模型乱写」与「prompt 注入撑爆 payload」的护栏，
# 不是业务规则 —— 业务规则（去重、生命周期）在后端。
_MAX_EVIDENCE = 6
_MAX_EVIDENCE_PER_CONCEPT = 2
_MAX_MEMORIES = 3
_MAX_LIST_ITEMS = 8
_OBSERVATION_MAX = 200
_SENTENCE_MAX = 240
_EXPLANATION_MAX = 500
_MEMORY_CONTENT_MAX = 500
_REASON_MAX = 500

# 用户「在解释」的语言信号。规则版只认这些短语；真实模型不需要它们，
# 但离线分析需要一条可复现的判据。
_EXPLANATION_MARKERS = (
    "我理解",
    "我的理解",
    "我觉得",
    "我认为",
    "也就是说",
    "换句话说",
    "其实就是",
    "应该是",
    "因为",
    "所以",
)
_PREFERENCE_MARKERS = (
    "我喜欢",
    "我习惯",
    "我偏好",
    "我更希望",
    "我希望你",
    "请以后",
    "以后请",
    "不要用",
)

# AnalyzeResult 的每个字段都有默认值，所以「任意一个 JSON 对象」都能通过 pydantic
# 校验并变成一份空分析。这在后端看来是「成功但无信号」，不会触发 rules-v1 回退，
# 等于把一次格式漂移静默吞掉。模型输出必须至少命中一个顶层字段才算有效。
_ANALYSIS_KEYS = (
    "evidenceCandidates",
    "evidence_candidates",
    "understandingProposal",
    "understanding_proposal",
    "memoryCandidates",
    "memory_candidates",
    "verificationDecision",
    "verification_decision",
)


class AnalysisError(RuntimeError):
    """分析失败（模型输出不可解析 / 不符合契约）。

    故意不吞掉：后端捕获后会退回 `rules-v1`，那是**有记录**的降级；
    如果这里悄悄返回空结果，后端会以为「这一轮确实没有学习信号」，
    于是既没有证据、也没有任何错误日志。
    """

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class LearningAnalyzer:
    def __init__(self, settings: Settings, llm: LLMAdapter) -> None:
        self._settings = settings
        self._llm = llm

    async def analyze(self, request: AnalyzeRequest, trace: RunTrace) -> AnalyzeResult:
        if not (request.user_message or "").strip():
            # 空消息也要留下终态，否则 trace 只有 RUN_START、没有 RUN_END，
            # 排查时无法区分「没跑」与「跑了但立刻结束」。
            trace.record(TraceEventType.RUN_END, status="COMPLETED", reason="empty_message")
            return self._empty_result()

        # allow_degradation=False：分析结果会写进长期认知，不能拿 fixture 顶替真实检索。
        # 后端不可用时这里抛 RetrievalError → HTTP 503，后端回退 rules-v1。
        retriever = build_retriever(
            self._settings,
            request.auth_token,
            allow_degradation=False,
            # 异步 Worker 没有用户 JWT，用 Spring 放进任务里的委托声明。
            # Agent 不自己签发：没有 backend_identity 时 build_retriever 直接 fail closed。
            user_id=request.user_id,
            delegation_token=request.backend_identity,
        )
        try:
            concepts = await retriever.list_concepts(request.learning_space_id)
            detected = detect_concepts(request.user_message, request.history, concepts)
            trace.record(
                TraceEventType.CONCEPT_DETECT,
                detected=[concept.label for concept, _, _ in detected],
                matchedOn=[matched_on for _, _, matched_on in detected],
                conceptsAvailable=len(concepts),
            )
            if not detected:
                # 闲聊或全新知识点：没有 Concept 就没有可以挂证据的对象。
                trace.record(TraceEventType.RUN_END, status="COMPLETED", reason="no_concept")
                return self._empty_result()

            concept_ids = [concept.id for concept, _, _ in detected]
            understandings, memories, evidences = await asyncio.gather(
                retriever.get_understandings(request.user_id, request.learning_space_id, concept_ids),
                retriever.get_memories(
                    request.user_id, request.learning_space_id, concept_ids, request.user_message
                ),
                retriever.get_evidences(request.user_id, concept_ids, self._settings.evidence_top_k),
            )
            trace.record(
                TraceEventType.RETRIEVE,
                source=retriever.name,
                conceptIds=concept_ids,
                understandings=len(understandings),
                memories=len(memories),
                evidences=len(evidences),
            )

            if self._llm.name == "mock":
                raw = self._heuristic_analysis(request, detected, understandings)
                trace.record(TraceEventType.LLM_CALL, adapter="mock", strategy="heuristic")
            else:
                raw = await self._llm_analysis(request, detected, understandings, memories, evidences)
                trace.record(
                    TraceEventType.LLM_CALL,
                    adapter=self._llm.name,
                    strategy="structured-json",
                )

            result = self._sanitize(raw, request, detected)
            trace.record(
                TraceEventType.ANALYSIS_DECISION,
                evidence=len(result.evidence_candidates),
                memories=len(result.memory_candidates),
                hasProposal=result.understanding_proposal is not None,
                verification=result.verification_decision.action.value,
            )
            trace.record(
                TraceEventType.RUN_END,
                status="COMPLETED",
                evidence=len(result.evidence_candidates),
            )
            return result
        finally:
            await retriever.aclose()

    # ================= LLM 路径 =================

    async def _llm_analysis(
        self,
        request: AnalyzeRequest,
        detected: list[tuple[Concept, float, str]],
        understandings: list[Understanding],
        memories: list[MemoryItem],
        evidences: list[EvidenceItem],
    ) -> AnalyzeResult:
        messages = [
            ChatMessage(role="system", content=ANALYSIS_SYSTEM_INSTRUCTION),
            ChatMessage(
                role="user",
                content=_render_analysis_input(
                    request, detected, understandings, memories, evidences
                ),
            ),
        ]
        text = await self._llm.complete(messages, temperature=0.1, max_tokens=self._settings.llm_max_tokens)
        data = _extract_json_object(text)
        if not any(key in data for key in _ANALYSIS_KEYS):
            # 外层多包一层（{"result": {...}}）或字段名拼错都会落到这里。
            # 不能让它变成「空分析成功」—— 那在后端就是 COMPLETED + 零证据。
            raise AnalysisError(
                "模型输出没有任何分析字段（期望 evidenceCandidates / "
                "understandingProposal / memoryCandidates / verificationDecision）"
            )
        try:
            return AnalyzeResult.model_validate(data)
        except ValidationError as exc:
            raise AnalysisError(f"分析结果不符合契约：{_short(exc)}") from exc

    # ================= Mock / 离线路径 =================

    def _heuristic_analysis(
        self,
        request: AnalyzeRequest,
        detected: list[tuple[Concept, float, str]],
        understandings: list[Understanding],
    ) -> AnalyzeResult:
        """确定性的最小分析：不判断对错，只记录「用户说了什么」。

        它刻意不猜 CORRECT：真实语义判断是 LLM 的职责。这里只区分
        「提问 / 解释 / 单纯提及」三种可复现的形态，够后端把链路跑通。
        """
        message = request.user_message.strip()
        question = _is_question(message)
        explanation = (not question) and any(marker in message for marker in _EXPLANATION_MARKERS)

        candidates: list[EvidenceCandidate] = []
        for concept, _, _ in detected:
            observation = _observation_for(message, concept)
            if explanation:
                candidates.append(
                    EvidenceCandidate(
                        concept_id=concept.id,
                        evidence_type=EvidenceType.USER_EXPLANATION,
                        observation=observation,
                        correctness=EvidenceCorrectness.PARTIAL,
                        reasoning_quality=0.5,
                        specificity=0.5,
                        ai_confidence=0.6,
                        need_follow_up=False,
                        user_explanation=_truncate(message, _EXPLANATION_MAX),
                    )
                )
            else:
                candidates.append(
                    EvidenceCandidate(
                        concept_id=concept.id,
                        evidence_type=EvidenceType.OBSERVATION,
                        observation=observation,
                        correctness=EvidenceCorrectness.UNKNOWN,
                        need_follow_up=question,
                    )
                )

        proposal = None
        if explanation:
            proposal = UnderstandingProposal(
                concept_id=detected[0][0].id,
                latest_user_explanation=_truncate(message, _EXPLANATION_MAX),
            )

        verification = self._heuristic_verification(detected, understandings, question)

        return AnalyzeResult(
            evidence_candidates=candidates,
            understanding_proposal=proposal,
            memory_candidates=_heuristic_memories(message),
            verification_decision=verification,
            analyzer_version=ANALYZER_VERSION,
        )

    @staticmethod
    def _heuristic_verification(
        detected: list[tuple[Concept, float, str]],
        understandings: list[Understanding],
        question: bool,
    ) -> VerificationDecision:
        """首次接触一个概念且用户在提问时，记为 ASK_LATER。

        不选 ASK_NOW：用户刚问「这是什么」，此时插一道题只会打断教学。
        ASK_LATER 在后端只记录决策，不会立刻创建 TestTask —— 这正是
        「只有值得打断时才插入 Test Card」的落地方式。
        """
        if not question or not detected:
            return VerificationDecision(action=VerificationAction.SKIP)
        concept = detected[0][0]
        known = next((item for item in understandings if item.concept_id == concept.id), None)
        if known is not None and known.status.value != "UNKNOWN":
            return VerificationDecision(action=VerificationAction.SKIP)
        return VerificationDecision(
            action=VerificationAction.ASK_LATER,
            trigger=VerificationTrigger.NEW_CONCEPT,
            concept_id=concept.id,
            reason="首次接触该概念，先完成讲解，稍后再验证",
            question_type=QuestionType.SHORT_ANSWER,
            question_text=f"请用自己的话解释一下「{concept.label}」。",
        )

    # ================= 校验与收敛 =================

    def _sanitize(
        self,
        raw: AnalyzeResult,
        request: AnalyzeRequest,
        detected: list[tuple[Concept, float, str]],
    ) -> AnalyzeResult:
        by_id = {concept.id: concept for concept, _, _ in detected}
        primary = detected[0][0]

        evidence = _sanitize_evidence(raw.evidence_candidates, by_id)
        proposal = _sanitize_proposal(raw.understanding_proposal, by_id, primary)
        memories = _sanitize_memories(raw.memory_candidates, by_id, primary, request)
        verification = _sanitize_verification(raw.verification_decision, by_id, primary)

        return AnalyzeResult(
            evidence_candidates=evidence,
            understanding_proposal=proposal,
            memory_candidates=memories,
            verification_decision=verification,
            analyzer_version=ANALYZER_VERSION,
            detected_concepts=[concept.label for concept, _, _ in detected],
        )

    @staticmethod
    def _empty_result() -> AnalyzeResult:
        return AnalyzeResult(
            evidence_candidates=[],
            understanding_proposal=None,
            memory_candidates=[],
            verification_decision=VerificationDecision(action=VerificationAction.SKIP),
            analyzer_version=ANALYZER_VERSION,
        )


# ===================== 纯函数：渲染与校验 =====================


def _render_analysis_input(
    request: AnalyzeRequest,
    detected: list[tuple[Concept, float, str]],
    understandings: list[Understanding],
    memories: list[MemoryItem],
    evidences: list[EvidenceItem],
) -> str:
    lines = ["## 本轮对话", f"用户：{request.user_message.strip()}"]
    if request.assistant_message:
        lines.append(f"助手：{_truncate(request.assistant_message, 800)}")

    lines += ["", "## 命中概念"]
    for concept, score, matched_on in detected:
        lines.append(
            f"- id={concept.id} 名称={concept.label} canonical={concept.canonical_name}"
            f" 命中={matched_on} score={score:.2f}"
        )

    lines += ["", "## 当前理解（只有当轮之前的数据）"]
    if not understandings:
        lines.append("- （没有快照：这是一个新概念）")
    for understanding in understandings:
        parts = [f"- id={understanding.concept_id} 状态={understanding.status.value}"]
        if understanding.known_parts:
            parts.append(f"已理解：{'；'.join(understanding.known_parts[:5])}")
        if understanding.misconceptions:
            parts.append(
                "误区：" + "；".join(item.text for item in understanding.misconceptions[:4])
            )
        if understanding.uncertain_parts:
            parts.append(f"不确定：{'；'.join(understanding.uncertain_parts[:5])}")
        if understanding.latest_user_explanation:
            parts.append(f"最近自述：{_truncate(understanding.latest_user_explanation, 300)}")
        lines.append(" | ".join(parts))

    lines += ["", "## 最近证据（用于判断是否重复/矛盾）"]
    if not evidences:
        lines.append("- （无）")
    for evidence in evidences:
        lines.append(
            f"- id={evidence.id} concept={evidence.concept_id} "
            f"correctness={evidence.correctness.value} "
            f"observation={_truncate(evidence.observation, 160)}"
        )

    lines += ["", "## 已有 Memory（避免重复提出）"]
    if not memories:
        lines.append("- （无）")
    for memory in memories:
        lines.append(
            f"- id={memory.id} scope={memory.scope_type.value} "
            f"key={memory.memory_key or '-'} content={_truncate(memory.content, 160)}"
        )

    lines += ["", "请按 system 指令输出 JSON。"]
    return "\n".join(lines)


def _extract_json_object(text: str) -> dict:
    """从模型输出里取出第一个 JSON 对象。

    真实模型经常在 JSON 外面包一层 ```json 围栏或一句「好的，结果如下」。
    这里只做两件事：去围栏、截取最外层花括号，然后交给 `json.loads`。
    不做「修复引号」之类的猜测 —— 猜错的代价是把一条错误证据写进长期理解，
    比这一次分析失败严重得多。
    """
    if not text or not text.strip():
        raise AnalysisError("模型没有返回任何分析结果")

    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise AnalysisError("模型输出里找不到 JSON 对象")
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AnalysisError(f"模型输出不是合法 JSON：{exc.msg}") from exc
    if not isinstance(data, dict):
        raise AnalysisError("模型输出不是 JSON 对象")
    return data


def _sanitize_evidence(
    candidates: list[EvidenceCandidate], by_id: dict[int, Concept]
) -> list[EvidenceCandidate]:
    result: list[EvidenceCandidate] = []
    seen: set[tuple[int, EvidenceType, str]] = set()
    per_concept: dict[int, int] = {}
    for candidate in candidates:
        if candidate.concept_id not in by_id:
            # 编造的 conceptId 一律丢弃：证据挂错概念比少一条证据危险得多。
            continue
        observation = _truncate(candidate.observation, _OBSERVATION_MAX)
        if not observation:
            continue
        key = (candidate.concept_id, candidate.evidence_type, observation)
        if key in seen:
            continue
        if per_concept.get(candidate.concept_id, 0) >= _MAX_EVIDENCE_PER_CONCEPT:
            continue
        seen.add(key)
        per_concept[candidate.concept_id] = per_concept.get(candidate.concept_id, 0) + 1
        result.append(
            candidate.model_copy(
                update={
                    "observation": observation,
                    "user_explanation": _truncate(candidate.user_explanation, _EXPLANATION_MAX),
                    "reasoning_quality": _clamp(candidate.reasoning_quality),
                    "specificity": _clamp(candidate.specificity),
                    "ai_confidence": _clamp(candidate.ai_confidence),
                }
            )
        )
        if len(result) >= _MAX_EVIDENCE:
            break
    return result


def _sanitize_proposal(
    proposal: UnderstandingProposal | None,
    by_id: dict[int, Concept],
    primary: Concept,
) -> UnderstandingProposal | None:
    if proposal is None:
        return None
    concept_id = proposal.concept_id if proposal.concept_id in by_id else primary.id
    known = _clean_list(proposal.known_parts)
    uncertain = _clean_list(proposal.uncertain_parts)
    misconceptions = _clean_list(proposal.misconceptions)
    explanation = _truncate(proposal.latest_user_explanation, _EXPLANATION_MAX)
    if not (known or uncertain or misconceptions or explanation):
        return None
    return UnderstandingProposal(
        concept_id=concept_id,
        known_parts=known,
        uncertain_parts=uncertain,
        misconceptions=misconceptions,
        latest_user_explanation=explanation,
    )


def _sanitize_memories(
    candidates: list[MemoryCandidate],
    by_id: dict[int, Concept],
    primary: Concept,
    request: AnalyzeRequest,
) -> list[MemoryCandidate]:
    result: list[MemoryCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        content = _truncate(candidate.content, _MEMORY_CONTENT_MAX)
        if not content:
            continue
        # 同一个内容只提一次；真正跨轮次的去重由后端的 (scope_key, memory_key) 唯一约束负责。
        if content in seen:
            continue

        scope = candidate.scope_type
        concept_id: int | None = None
        space_id: int | None = None
        if scope is MemoryScopeType.CONCEPT:
            concept_id = candidate.concept_id if candidate.concept_id in by_id else primary.id
        elif scope is MemoryScopeType.SPACE:
            space_id = request.learning_space_id
        # GLOBAL 的两列必须为空 —— 后端的 scope_key 依赖这个不变量。

        seen.add(content)
        result.append(
            candidate.model_copy(
                update={
                    "learning_space_id": space_id,
                    "concept_id": concept_id,
                    "content": content,
                    "importance": _clamp(candidate.importance),
                    "memory_type": candidate.memory_type or MemoryType.LEARNING,
                    "action": candidate.action or MemoryReconcileAction.ADD,
                }
            )
        )
        if len(result) >= _MAX_MEMORIES:
            break
    return result


def _sanitize_verification(
    decision: VerificationDecision,
    by_id: dict[int, Concept],
    primary: Concept,
) -> VerificationDecision:
    if decision.action is VerificationAction.SKIP:
        return VerificationDecision(action=VerificationAction.SKIP, reason=_truncate(decision.reason, _REASON_MAX))

    concept = by_id.get(decision.concept_id) if decision.concept_id is not None else primary
    if concept is None:
        return VerificationDecision(action=VerificationAction.SKIP)

    question_type = decision.question_type or QuestionType.SHORT_ANSWER
    question_text = _truncate(decision.question_text, 300)
    if decision.action is VerificationAction.ASK_NOW and not question_text:
        # ASK_NOW 没有题目等于「打断用户但什么都不问」。补一个可作答的默认题，
        # 而不是把这个决策静默降级成 SKIP —— 那会让策略层看起来从未生效。
        question_type = QuestionType.SHORT_ANSWER
        question_text = f"请用自己的话解释一下「{concept.label}」。"

    return VerificationDecision(
        action=decision.action,
        trigger=decision.trigger,
        concept_id=concept.id,
        reason=_truncate(decision.reason, _REASON_MAX),
        question_type=question_type,
        question_text=question_text,
        correct_answer=_truncate(decision.correct_answer, 1000),
    )


def _heuristic_memories(message: str) -> list[MemoryCandidate]:
    sentence = _sentence_containing(message, _PREFERENCE_MARKERS)
    if not sentence:
        return []
    return [
        MemoryCandidate(
            scope_type=MemoryScopeType.GLOBAL,
            memory_type=MemoryType.PREFERENCE,
            content=_truncate(sentence, _MEMORY_CONTENT_MAX),
            importance=0.6,
            action=MemoryReconcileAction.ADD,
        )
    ]


def _observation_for(message: str, concept: Concept) -> str:
    names = [concept.canonical_name, concept.label]
    return _truncate(_sentence_containing(message, names) or message, _OBSERVATION_MAX)


def _sentence_containing(text: str, needles: tuple[str, ...] | list[str]) -> str | None:
    if not text:
        return None
    for sentence in re.split(r"(?<=[。！？!?；;\n])", text):
        candidate = sentence.strip()
        if candidate and any(needle and needle in candidate for needle in needles):
            return _truncate(candidate, _SENTENCE_MAX)
    return None


def _is_question(message: str) -> bool:
    return "?" in message or "？" in message


def _clean_list(items: list[str]) -> list[str]:
    cleaned: list[str] = []
    for item in items:
        text = _truncate(item, _OBSERVATION_MAX)
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= _MAX_LIST_ITEMS:
            break
    return cleaned


def _clamp(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(1.0, float(value)))


def _truncate(text: str | None, max_length: int) -> str | None:
    if text is None:
        return None
    trimmed = str(text).strip()
    if not trimmed:
        return None
    return trimmed if len(trimmed) <= max_length else trimmed[:max_length] + "…"


def _short(exc: object) -> str:
    return _truncate(str(exc).replace("\n", " "), 300) or "未知校验错误"
