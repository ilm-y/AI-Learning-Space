"""E2 结构化 Concept Resolver —— 规则识别不足时，用 LLM 提出候选概念。

**边界先说清楚：LLM 只提议，不裁决。**

规则版 `concept_detector` 零成本、可预测，但覆盖不了同义表达（「求导」不是
「导数」）和模糊指代（「那个东西」）。这里补一层 LLM 语义识别，但最终进入
`concept_ids` 的每一个 id 都必须通过服务端确定性校验：

1. 必须属于**本次实际呈现给模型的当前 Space 候选列表**（跨 Space / 编造一律丢弃）；
2. `confidence` 必须 >= 配置阈值（默认 0.6）；
3. 每个字段最多采纳 3 个；
4. `current_concepts` 与 `related_concepts` 语义分离：`related` 不会进入
   `concept_ids`，最多作为 E1 受控关系扩展的**排序提示**；
5. 与规则识别合并时规则优先，Resolver 只补充规则未命中的概念。

`related_concepts` 永远不能绕过 `concept_relation`：Runtime 只会用它在
后端真实返回的关系候选里做排序，绝不会据此新增 Evidence 查询范围。这一点由
`runtime._select_expansion_concepts` 与 `test_concept_resolver.py::test_related_hint_cannot_bypass_relation_table`
共同钉住。

调用策略（按需调用，不每轮调用）：

| 规则命中 | intent | 是否调用 |
| --- | --- | --- |
| 是 | prerequisite_question / review_request | 是 |
| 是 | 其他 | 否 |
| 否 | knowledge_question / prerequisite_question / review_request | 是 |
| 否 | chitchat / other | 否 |

失败语义：超时 / 5xx / 非法 JSON 统一 `DEGRADED`，主链路继续使用规则识别；
Resolver 自身未预期异常为 `FAILED`，同样不阻塞。`NOT_CALLED` 表示策略决定不调用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from enum import Enum

from app.context.tokens import estimate_tokens, truncate_to_tokens
from app.llm.base import ChatMessage, LLMAdapter, LLMError
from app.retriever.concept_detector import ALIASES
from app.schemas import Concept, HistoryMessage

log = logging.getLogger(__name__)

INTENT_KNOWLEDGE = "knowledge_question"
INTENT_PREREQUISITE = "prerequisite_question"
INTENT_REVIEW = "review_request"
INTENT_CHITCHAT = "chitchat"
INTENT_OTHER = "other"

INTENTS = (
    INTENT_KNOWLEDGE,
    INTENT_PREREQUISITE,
    INTENT_REVIEW,
    INTENT_CHITCHAT,
    INTENT_OTHER,
)

# 这些 intent 允许 Resolver 返回 needs_history_reference=true。
_HISTORY_REFERENCE_INTENTS = (INTENT_PREREQUISITE, INTENT_REVIEW)

# 只做「这句话像不像在问知识」的粗分类，不替代模型理解：
# 它只决定要不要花一次 Resolver 调用，判错最多多一次调用或漏一次语义补充。
_PREREQUISITE_MARKERS = (
    "前置",
    "先修",
    "先学",
    "先掌握",
    "需要先",
    "应该先",
    "从哪开始学",
    "从哪里开始学",
    "基础是什么",
    "基础是",
    "前面的基础",
    "更基础",
    "预备知识",
    " prerequisites",
)
_REVIEW_MARKERS = (
    "上次",
    "之前",
    "以前",
    "曾经",
    "过去",
    "后来",
    "刚才",
    "复盘",
    "回顾",
    "学习轨迹",
    "误区",
    "错在哪",
    "哪里错",
    "哪一步错",
    "搞错",
    "说错",
    "误解",
    "纠正",
    "改过来",
)
_REVIEW_CLEAR_MARKERS = (
    "误区",
    "复盘",
    "回顾",
    "学习轨迹",
    "错在哪",
    "哪里错",
    "哪一步错",
    "搞错",
    "说错",
    "纠正",
    "改过来",
    "上次那个",
    "之前那个",
)
_PERSONAL_MARKERS = ("我", "自己")
_AMBIGUOUS_MARKERS = (
    "那个",
    "这个",
    "那玩意",
    "这东西",
    "它",
    "上面说",
    "刚才那",
    "前面说",
    "上次",
    "之前",
)
_KNOWLEDGE_MARKERS = (
    "是什么",
    "什么",
    "为什么",
    "怎么",
    "如何",
    "解释",
    "定义",
    "区别",
    "原理",
    "讲讲",
    "说说",
    "理解",
    "意思",
    "作用",
    "关系",
    "求",
    "证明",
    "？",
    "?",
)
_CHITCHAT_EXACT = {
    "你好",
    "您好",
    "嗨",
    "hi",
    "hello",
    "在吗",
    "继续",
    "嗯",
    "哦",
    "噢",
    "好的",
    "好",
    "行",
    "可以",
    "谢谢",
    "多谢",
    "ok",
    "okay",
    "再见",
    "拜拜",
    "收到",
}
_CHITCHAT_MARKERS = (
    "天气",
    "吃饭",
    "电影",
    "笑话",
    "闲聊",
    "随便聊",
    "今天怎么样",
)


class ResolverStatus(str, Enum):
    """一次 Resolver 决策的状态。

    - NOT_CALLED：按需调用策略决定不调用；
    - SUCCESS：调用成功并完成服务端校验（即使有候选被丢弃）；
    - DEGRADED：超时 / 模型错误 / 非法 JSON / 结构化输出不可用，主链路继续；
    - FAILED：Resolver 自身未预期异常，同样不阻塞主链路。

    **DEGRADED 不等于「用户没有相关历史」**，ContextBuilder 会明确区分。
    """

    NOT_CALLED = "NOT_CALLED"
    SUCCESS = "SUCCESS"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ResolvedConcept:
    """通过服务端校验的一个 Resolver 候选。"""

    concept_id: int
    confidence: float
    reason: str = ""


@dataclass(slots=True)
class ResolverDecision:
    """Resolver 的完整决策结果，供 Runtime 写 state / trace / 日志。"""

    status: ResolverStatus
    intent: str = INTENT_OTHER
    current_concepts: list[ResolvedConcept] = field(default_factory=list)
    related_concepts: list[ResolvedConcept] = field(default_factory=list)
    needs_history_reference: bool = False
    warning: str | None = None
    called: bool = False
    elapsed_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    token_source: str = "estimated"
    raw_output: str | None = None
    presented_concept_ids: list[int] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)

    @classmethod
    def not_called(cls, *, intent: str, reason: str) -> "ResolverDecision":
        return cls(
            status=ResolverStatus.NOT_CALLED,
            intent=intent,
            warning=reason,
        )


@dataclass(frozen=True, slots=True)
class ResolverCallDecision:
    """按需调用策略的判定结果。"""

    call: bool
    intent: str
    reason: str
    allow_history_reference: bool


def classify_intent(message: str) -> str:
    """规则版意图粗分类，只服务「要不要调用 Resolver」这一件事。"""
    text = (message or "").strip()
    if not text:
        return INTENT_OTHER

    if _is_review_query(text):
        return INTENT_REVIEW
    if any(marker in text for marker in _PREREQUISITE_MARKERS):
        return INTENT_PREREQUISITE
    if _is_chitchat(text):
        return INTENT_CHITCHAT
    if any(marker in text for marker in _KNOWLEDGE_MARKERS):
        return INTENT_KNOWLEDGE
    if any(marker in text for marker in _AMBIGUOUS_MARKERS):
        return INTENT_KNOWLEDGE
    return INTENT_OTHER


def decide_resolver_call(
    message: str,
    *,
    rule_hit_count: int,
    candidate_count: int,
    enabled: bool = True,
) -> ResolverCallDecision:
    """按需调用策略。

    规则识别命中且不是前置 / 复盘问题时，不调用；纯闲聊、短消息与明显
    非知识问题时也不调用。其余情况调用一次，用来补规则漏掉的概念。
    """
    intent = classify_intent(message)
    if not enabled:
        return ResolverCallDecision(False, intent, "disabled", False)
    if candidate_count <= 0:
        return ResolverCallDecision(False, intent, "no_candidates", False)
    if intent == INTENT_CHITCHAT:
        return ResolverCallDecision(False, intent, "chitchat", False)

    ambiguous = _has_ambiguous_reference(message)
    allow_history_reference = intent in _HISTORY_REFERENCE_INTENTS or ambiguous

    if rule_hit_count > 0:
        if intent in _HISTORY_REFERENCE_INTENTS:
            return ResolverCallDecision(
                True, intent, "rule_hit_but_prerequisite_or_review", allow_history_reference
            )
        return ResolverCallDecision(False, intent, "rule_hit_normal_intent", False)

    if intent in (INTENT_KNOWLEDGE, INTENT_PREREQUISITE, INTENT_REVIEW):
        reason = "ambiguous_reference" if ambiguous else "rule_miss_knowledge_question"
        return ResolverCallDecision(True, intent, reason, allow_history_reference)
    return ResolverCallDecision(False, intent, "rule_miss_non_knowledge_intent", False)


class ConceptResolver:
    """一次调用一个实例；无状态、可复用。"""

    def __init__(
        self,
        llm: LLMAdapter,
        *,
        confidence_threshold: float = 0.6,
        max_per_field: int = 3,
        timeout_seconds: float = 5.0,
        max_tokens: int = 400,
        max_prompt_tokens: int = 1600,
        max_candidates: int = 60,
        history_turns: int = 4,
        history_chars_per_message: int = 160,
    ) -> None:
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold 必须在 [0, 1] 之间")
        if not 1 <= max_per_field <= 3:
            raise ValueError("max_per_field 必须在 [1, 3] 之间")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须为正数")
        if max_tokens <= 0:
            raise ValueError("max_tokens 必须为正数")
        if max_prompt_tokens <= 0:
            raise ValueError("max_prompt_tokens 必须为正数")
        if max_candidates <= 0:
            raise ValueError("max_candidates 必须为正数")
        if history_turns < 0:
            raise ValueError("history_turns 不能为负数")
        if history_chars_per_message <= 0:
            raise ValueError("history_chars_per_message 必须为正数")

        self._llm = llm
        self._threshold = confidence_threshold
        self._max_per_field = max_per_field
        self._timeout = timeout_seconds
        self._max_tokens = max_tokens
        self._max_prompt_tokens = max_prompt_tokens
        self._max_candidates = max_candidates
        self._history_turns = history_turns
        self._history_chars = history_chars_per_message

    async def resolve(
        self,
        *,
        run_id: int,
        user_id: int,
        space_id: int,
        user_message: str,
        history: list[HistoryMessage],
        candidates: list[Concept],
        allow_history_reference: bool,
    ) -> ResolverDecision:
        """调用一次 Resolver 并完成确定性校验。

        `user_id` / `space_id` 不写进提示词（模型不需要、也不该据此推断权限），
        只用于日志与审计；候选集已由 Runtime 按当前 Space 读取。
        """
        started = time.monotonic()
        system_prompt, user_prompt, presented = self._build_prompt(
            user_message=user_message,
            history=history,
            candidates=candidates,
        )
        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_prompt),
        ]
        prompt_tokens = sum(estimate_tokens(message.content) for message in messages)
        presented_ids = [concept.id for concept in presented]

        try:
            content = await asyncio.wait_for(
                self._llm.complete(
                    messages,
                    temperature=0.0,
                    max_tokens=self._max_tokens,
                ),
                timeout=self._timeout,
            )
        except (asyncio.TimeoutError, TimeoutError):
            elapsed = _elapsed_ms(started)
            log.warning(
                "概念解析超时（%.2fs），降级为规则识别：run=%s user=%s space=%s",
                self._timeout,
                run_id,
                user_id,
                space_id,
            )
            return self._degraded(
                ResolverStatus.DEGRADED,
                f"概念解析超时（{self._timeout:.1f}s），已降级为规则识别。",
                prompt_tokens=prompt_tokens,
                elapsed=elapsed,
                presented_ids=presented_ids,
            )
        except LLMError as exc:
            elapsed = _elapsed_ms(started)
            log.warning(
                "概念解析模型调用失败，降级为规则识别：run=%s space=%s error=%s",
                run_id,
                space_id,
                exc,
            )
            return self._degraded(
                ResolverStatus.DEGRADED,
                f"概念解析模型调用失败，已降级为规则识别：{exc}",
                prompt_tokens=prompt_tokens,
                elapsed=elapsed,
                presented_ids=presented_ids,
            )
        except Exception as exc:  # noqa: BLE001 —— Resolver 不得打挂主链路
            elapsed = _elapsed_ms(started)
            log.exception(
                "概念解析未预期失败：run=%s space=%s", run_id, space_id
            )
            return self._degraded(
                ResolverStatus.FAILED,
                f"概念解析内部错误（{type(exc).__name__}），已降级为规则识别。",
                prompt_tokens=prompt_tokens,
                elapsed=elapsed,
                presented_ids=presented_ids,
            )

        completion_tokens = estimate_tokens(content)
        token_source = "estimated"
        prompt_tokens, completion_tokens, token_source = _provider_usage(
            self._llm, prompt_tokens, completion_tokens
        )
        elapsed = _elapsed_ms(started)

        try:
            payload = _extract_json_object(content)
        except (ValueError, json.JSONDecodeError) as exc:
            log.warning(
                "概念解析返回非法 JSON，降级为规则识别：run=%s space=%s error=%s raw=%r",
                run_id,
                space_id,
                exc,
                (content or "")[:200],
            )
            return self._degraded(
                ResolverStatus.DEGRADED,
                "概念解析返回非法 JSON，已降级为规则识别。",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                token_source=token_source,
                elapsed=elapsed,
                presented_ids=presented_ids,
                raw_output=content,
            )

        if not isinstance(payload, dict):
            log.warning(
                "概念解析返回的不是 JSON 对象，降级为规则识别：run=%s space=%s",
                run_id,
                space_id,
            )
            return self._degraded(
                ResolverStatus.DEGRADED,
                "概念解析返回的不是 JSON 对象，已降级为规则识别。",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                token_source=token_source,
                elapsed=elapsed,
                presented_ids=presented_ids,
                raw_output=content,
            )

        allowed_ids = set(presented_ids)
        current, current_dropped = _validate_field(
            payload.get("current_concepts"),
            allowed_ids=allowed_ids,
            threshold=self._threshold,
            max_items=self._max_per_field,
            field_name="current_concepts",
        )
        related, related_dropped = _validate_field(
            payload.get("related_concepts"),
            allowed_ids=allowed_ids,
            threshold=self._threshold,
            max_items=self._max_per_field,
            field_name="related_concepts",
        )

        current_ids = {item.concept_id for item in current}
        filtered_related: list[ResolvedConcept] = []
        for item in related:
            if item.concept_id in current_ids:
                related_dropped.append(
                    f"related conceptId={item.concept_id} 与 current 重复，已从 related 移除"
                )
                continue
            filtered_related.append(item)
        related = filtered_related

        warnings = [*current_dropped, *related_dropped]
        intent = _validate_intent(payload.get("intent"), warnings)
        needs_history, history_warning = _validate_history_reference(
            payload.get("needs_history_reference"), allow=allow_history_reference
        )
        if history_warning:
            warnings.append(history_warning)

        if not current and not related and not warnings:
            warnings.append("模型未返回候选概念（当前 Space 无匹配概念）。")

        warning = "；".join(dict.fromkeys(item for item in warnings if item)) or None
        if warning:
            log.warning(
                "概念解析完成但有候选被服务端过滤：run=%s space=%s detail=%s",
                run_id,
                space_id,
                warning,
            )
        log.info(
            "概念解析完成：run=%s space=%s intent=%s current=%s related=%s needsHistory=%s tokens=%s/%s source=%s elapsed=%sms",
            run_id,
            space_id,
            intent,
            [item.concept_id for item in current],
            [item.concept_id for item in related],
            needs_history,
            prompt_tokens,
            completion_tokens,
            token_source,
            elapsed,
        )
        return ResolverDecision(
            status=ResolverStatus.SUCCESS,
            intent=intent,
            current_concepts=current,
            related_concepts=related,
            needs_history_reference=needs_history,
            warning=warning,
            called=True,
            elapsed_ms=elapsed,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            token_source=token_source,
            raw_output=(content or "")[:400],
            presented_concept_ids=presented_ids,
            dropped=[*current_dropped, *related_dropped],
        )

    # ---------- 提示词 ----------

    def _build_prompt(
        self,
        *,
        user_message: str,
        history: list[HistoryMessage],
        candidates: list[Concept],
    ) -> tuple[str, str, list[Concept]]:
        """在提示词预算内构造 system / user prompt。

        候选按输入顺序截断；**校验白名单使用同一份截断后的列表** ——
        模型没看到的概念，即使恰好是合法 id，也不接受它「猜」出来。
        """
        presented = list(candidates[: self._max_candidates])
        turns = list(history[-self._history_turns :]) if self._history_turns else []
        message = user_message or ""

        system_prompt = RESOLVER_SYSTEM_PROMPT
        user_prompt = _render_user_prompt(
            message,
            turns,
            presented,
            history_chars=self._history_chars,
        )
        budget = self._max_prompt_tokens
        while estimate_tokens(system_prompt) + estimate_tokens(user_prompt) > budget:
            if presented:
                presented.pop()
            elif turns:
                turns.pop(0)
            else:
                available = max(
                    80,
                    budget
                    - estimate_tokens(system_prompt)
                    - estimate_tokens(_render_user_prompt("", [], [], history_chars=self._history_chars)),
                )
                message = truncate_to_tokens(message, available)
            user_prompt = _render_user_prompt(
                message,
                turns,
                presented,
                history_chars=self._history_chars,
            )
            if not presented and not turns and estimate_tokens(message) <= 80:
                break
        return system_prompt, user_prompt, presented

    def _degraded(
        self,
        status: ResolverStatus,
        warning: str,
        *,
        prompt_tokens: int,
        elapsed: int,
        presented_ids: list[int],
        completion_tokens: int = 0,
        token_source: str = "estimated",
        raw_output: str | None = None,
    ) -> ResolverDecision:
        return ResolverDecision(
            status=status,
            warning=warning,
            called=True,
            elapsed_ms=elapsed,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            token_source=token_source,
            raw_output=raw_output,
            presented_concept_ids=list(presented_ids),
        )


# ---------- 提示词模板 ----------

RESOLVER_SYSTEM_PROMPT = """你是学习空间中的「概念解析器」。你只负责从给定候选概念中提出候选，最终是否采纳由服务端确定性校验决定。

规则：
1. 只能选择候选列表里出现过的 conceptId，禁止编造；不在列表中的概念必须返回空数组，并在 reason 中写「当前 Space 无匹配概念」。
2. current_concepts = 用户当前这句话直接指向的概念；related_concepts = 理解 current 所需的前置或相关概念。两者必须分开，不能把前置/相关概念塞进 current，也不能把 current 塞进 related。
3. 每个数组最多 3 项；confidence 取值 0..1；reason 用一句短文本说明依据。
4. needs_history_reference 只有在用户明显依赖前文时才为 true，例如「上次那个」「之前的误区」「那个东西」。
5. intent 只能是：knowledge_question | prerequisite_question | review_request | chitchat | other。
6. 只输出一个 JSON 对象，不要 Markdown 代码块，不要解释，不要输出 JSON 之外的任何文字。

输出结构：
{"current_concepts":[{"conceptId":123,"confidence":0.9,"reason":"..."}],"related_concepts":[{"conceptId":456,"confidence":0.8,"reason":"..."}],"needs_history_reference":false,"intent":"knowledge_question"}"""


def _render_user_prompt(
    message: str,
    history: list[HistoryMessage],
    candidates: list[Concept],
    *,
    history_chars: int,
) -> str:
    lines: list[str] = ["当前 Space 的候选概念（只能从这里选）："]
    if candidates:
        for concept in candidates:
            aliases = ALIASES.get(concept.canonical_name.strip().lower(), ())
            parts = [f"id={concept.id}", f"name={concept.label}"]
            if concept.canonical_name.strip().lower() != concept.label.strip().lower():
                parts.append(f"canonical={concept.canonical_name}")
            if aliases:
                parts.append("aliases=" + "/".join(aliases))
            description = (concept.description or "").strip()
            if description:
                if len(description) > 80:
                    description = description[:80] + "…"
                parts.append(f"desc={description}")
            lines.append("- " + " | ".join(parts))
    else:
        lines.append("- （无候选概念）")

    lines.append("")
    lines.append("最近对话（每轮已截断，仅作指代消解参考）：")
    if history:
        for item in history:
            role = "用户" if item.is_user() else "助手"
            content = (item.content or "").strip().replace("\n", " ")
            if len(content) > history_chars:
                content = content[:history_chars] + "…"
            lines.append(f"- {role}：{content}")
    else:
        lines.append("- （无历史对话）")

    lines.append("")
    lines.append("当前用户消息：")
    lines.append(message.strip() or "（空）")
    lines.append("")
    lines.append("请按系统提示的 JSON 结构输出，只输出 JSON。")
    return "\n".join(lines)


# ---------- 解析与校验 ----------


def _extract_json_object(content: str) -> dict:
    text = (content or "").strip()
    if not text:
        raise ValueError("空响应")
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", text)
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("响应中没有 JSON 对象")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("JSON 顶层不是对象")
    return payload


def _validate_field(
    raw: object,
    *,
    allowed_ids: set[int],
    threshold: float,
    max_items: int,
    field_name: str,
) -> tuple[list[ResolvedConcept], list[str]]:
    dropped: list[str] = []
    if raw is None:
        dropped.append(f"{field_name} 缺失，按空数组处理")
        return [], dropped
    if not isinstance(raw, list):
        dropped.append(f"{field_name} 不是数组，按空数组处理")
        return [], dropped

    valid: list[ResolvedConcept] = []
    for item in raw:
        if not isinstance(item, dict):
            dropped.append(f"{field_name} 中存在非对象条目")
            continue
        concept_id = item.get("conceptId")
        if isinstance(concept_id, bool) or not isinstance(concept_id, int):
            dropped.append(f"{field_name} 中存在非整数 conceptId")
            continue
        if concept_id not in allowed_ids:
            # 同时覆盖「编造 id」与「跨 Space 同名概念」：服务端只认本次
            # 实际呈现的当前 Space 候选列表，不查询其他 Space。
            dropped.append(
                f"{field_name} 丢弃 conceptId={concept_id}：不在当前 Space 候选列表"
            )
            continue
        confidence = item.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
        ):
            dropped.append(f"{field_name} 丢弃 conceptId={concept_id}：confidence 非法")
            continue
        confidence = max(0.0, min(1.0, float(confidence)))
        if confidence < threshold:
            dropped.append(
                f"{field_name} 丢弃 conceptId={concept_id}："
                f"confidence={confidence:.2f} 低于阈值 {threshold:.2f}"
            )
            continue
        reason = item.get("reason")
        reason_text = str(reason).strip()[:200] if reason is not None else ""
        valid.append(
            ResolvedConcept(
                concept_id=concept_id,
                confidence=confidence,
                reason=reason_text,
            )
        )

    # 稳定排序：置信度高者优先，同分保持模型输出顺序；按 conceptId 去重。
    valid.sort(key=lambda item: item.confidence, reverse=True)
    deduped: list[ResolvedConcept] = []
    seen: set[int] = set()
    for item in valid:
        if item.concept_id in seen:
            dropped.append(
                f"{field_name} 丢弃重复 conceptId={item.concept_id}"
            )
            continue
        seen.add(item.concept_id)
        deduped.append(item)

    if len(deduped) > max_items:
        for item in deduped[max_items:]:
            dropped.append(
                f"{field_name} 丢弃 conceptId={item.concept_id}：超过每字段上限 {max_items}"
            )
        deduped = deduped[:max_items]
    return deduped, dropped


def _validate_intent(raw: object, warnings: list[str]) -> str:
    if isinstance(raw, str) and raw.strip() in INTENTS:
        return raw.strip()
    if raw is not None:
        warnings.append(f"intent={raw!r} 非法，按 other 处理")
    else:
        warnings.append("intent 缺失，按 other 处理")
    return INTENT_OTHER


def _validate_history_reference(
    raw: object, *, allow: bool
) -> tuple[bool, str | None]:
    if raw is None:
        return False, None
    if not isinstance(raw, bool):
        return False, "needs_history_reference 不是布尔值，已按 false 处理"
    if raw and not allow:
        return False, "needs_history_reference=true 但当前意图不允许依赖历史，已按 false 处理"
    return raw, None


def _provider_usage(
    llm: LLMAdapter, prompt_estimate: int, completion_estimate: int
) -> tuple[int, int, str]:
    """优先使用适配器报告的真实 usage；没有则保留估算值。

    离线 Mock / Fake 适配器没有 usage，报告里必须写清 ``token_source=estimated``，
    不能把估算值伪装成供应商计数。
    """
    diagnostics = getattr(llm, "last_completion_diagnostics", None)
    usage = getattr(diagnostics, "usage", None)
    if not isinstance(usage, dict):
        return prompt_estimate, completion_estimate, "estimated"
    prompt = _usage_int(usage, "prompt_tokens", "promptTokens")
    completion = _usage_int(usage, "completion_tokens", "completionTokens")
    if prompt is None or completion is None:
        return prompt_estimate, completion_estimate, "estimated"
    return prompt, completion, "provider"


def _usage_int(usage: dict, *keys: str) -> int | None:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
    return None


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _is_chitchat(text: str) -> bool:
    normalized = text.strip().lower().strip("。！？!?，,~～ ")
    if normalized in _CHITCHAT_EXACT:
        return True
    if len(normalized) <= 2:
        return True
    return any(marker in text for marker in _CHITCHAT_MARKERS)


def _is_review_query(text: str) -> bool:
    if not any(marker in text for marker in _REVIEW_MARKERS):
        return False
    return (
        any(marker in text for marker in _PERSONAL_MARKERS)
        or any(marker in text for marker in _REVIEW_CLEAR_MARKERS)
    )


def _has_ambiguous_reference(text: str) -> bool:
    return any(marker in text for marker in _AMBIGUOUS_MARKERS)
