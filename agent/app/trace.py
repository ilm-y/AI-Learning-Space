"""Agent Run 的结构化轨迹。

对应 `agent-architecture.md` 第三十六、三十七节：一次 Agent 执行留下一条 run 记录，
记录的是 **Input Summary / Output Summary / Tool 调用 / 状态 / 耗时**，
**不是** LLM 的内部思维链。

这里只做进程内的轻量记录，给两个用途：
1. `POST /agent/context/preview` 把「这次到底检索到了什么、上下文怎么组装的」摊开给人看；
2. 日志与排查 —— 每一步的耗时与命中数量。

持久化到后端 `agent_run` 表由 Backend 负责，Agent 不直接写库。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class TraceEventType(str, Enum):
    RUN_START = "RUN_START"
    CONCEPT_DETECT = "CONCEPT_DETECT"
    RETRIEVE = "RETRIEVE"
    CONTEXT_BUILT = "CONTEXT_BUILT"
    LLM_CALL = "LLM_CALL"
    # 回答生成后的来源标注校验。它只记录、不阻断 SSE —— 非法引用目前是
    # 评估与排查信号，而不是把用户可见回答拦掉的开关。
    CITATION_CHECK = "CITATION_CHECK"
    # 非成功知识检索状态的运行时兜底：记录本轮状态、是否追加了说明、以及
    # 触发/跳过原因。它不改变 SSE 事件类型，只影响回答末尾的固定说明。
    EVIDENCE_GUARD = "EVIDENCE_GUARD"
    # 显式复盘回答的证据约束：记录校验发现的问题、是否重生成、是否回退。
    # 只在 generate() 里记录；prepare() 不产生它，上下文预览的事件序列保持不变。
    HISTORY_GUARD = "HISTORY_GUARD"
    ANALYSIS_DECISION = "ANALYSIS_DECISION"
    RUN_END = "RUN_END"
    RUN_ERROR = "RUN_ERROR"


@dataclass(slots=True)
class TraceEvent:
    type: TraceEventType
    detail: dict[str, object]
    elapsed_ms: int

    def as_dict(self) -> dict[str, object]:
        return {"type": self.type.value, "elapsedMs": self.elapsed_ms, **self.detail}


@dataclass
class RunTrace:
    """一次 run 的轨迹。所有事件都带「距 run 开始的毫秒数」，方便看耗时分布。"""

    run_id: int
    # 后端生成的跨进程关联键（run_id 只在各自进程里有效，它才能把两跳的日志对上）。
    # 老版本后端不带这个字段，所以是可选的：缺失只影响排查，不影响执行。
    trace_id: str | None = None
    started_monotonic: float = field(default_factory=time.monotonic)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    events: list[TraceEvent] = field(default_factory=list)

    def record(self, event_type: TraceEventType, **detail: object) -> TraceEvent:
        event = TraceEvent(
            type=event_type,
            detail=detail,
            elapsed_ms=int((time.monotonic() - self.started_monotonic) * 1000),
        )
        self.events.append(event)
        return event

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_monotonic) * 1000)

    def event_types(self) -> list[str]:
        return [event.type.value for event in self.events]

    def as_dict(self) -> dict[str, object]:
        return {
            "runId": self.run_id,
            "traceId": self.trace_id,
            "startedAt": self.started_at.isoformat(),
            "elapsedMs": self.elapsed_ms,
            "events": [event.as_dict() for event in self.events],
        }
