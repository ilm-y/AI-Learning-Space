"""MQ 消息契约的纯单测：不装 aio_pika、不连 RabbitMQ 也能跑。"""

from __future__ import annotations

from app.analysis import AnalysisError
from app.llm.base import LLMError
from app.messaging import (
    AnalysisResultMessage,
    AnalysisResultStatus,
    AnalysisTaskMessage,
    classify_failure,
    retry_count,
)
from app.retriever.base import RetrievalError
from app.schemas import MessageRole


def test_task_message_accepts_backend_camel_case_and_builds_analyze_request():
    task = AnalysisTaskMessage.model_validate(
        {
            "schemaVersion": 1,
            "taskId": 42,
            "runId": 42,
            "traceId": "trace-1",
            "userId": 7,
            "conversationId": 9,
            "learningSpaceId": 3,
            "sessionId": 5,
            "messageId": 100,
            "assistantMessageId": 101,
            "userMessage": "TLB 是什么？",
            "assistantMessage": "TLB 是……",
            "history": [
                {"role": "USER", "content": "上一轮"},
                {"role": "ASSISTANT", "content": "上一答"},
            ],
            "session": {
                "id": 5,
                "title": "分页",
                "goal": "理解 TLB",
                "currentFocus": None,
            },
        }
    )

    assert task.task_id == 42
    assert task.history[0].role is MessageRole.USER

    request = task.to_analyze_request()
    assert request.run_id == 42
    assert request.user_id == 7
    assert request.message_id == 100
    assert request.user_message == "TLB 是什么？"
    assert request.session is not None and request.session.id == 5
    # 异步链路绝不携带用户 JWT：Worker 用服务端凭据检索
    assert request.auth_token is None


def test_result_message_serializes_to_backend_camel_case():
    result = AnalysisResultMessage(
        task_id=42,
        trace_id="trace-1",
        status=AnalysisResultStatus.SUCCEEDED,
        result={"evidenceCandidates": [], "analyzerVersion": "agent-v1"},
    ).model_dump(by_alias=True, mode="json")

    assert result["schemaVersion"] == 1
    assert result["taskId"] == 42
    assert result["status"] == "SUCCEEDED"
    assert result["result"]["evidenceCandidates"] == []
    assert "task_id" not in result


def test_classify_failure_marks_only_environment_errors_retryable():
    assert classify_failure(RetrievalError("backend down")) == ("RETRIEVAL_UNAVAILABLE", True)
    assert classify_failure(LLMError("model down")) == ("LLM_UNAVAILABLE", True)
    # 模型输出不可解析是确定性的，重放没有意义
    assert classify_failure(AnalysisError("bad json")) == ("ANALYSIS_INVALID", False)
    assert classify_failure(ValueError("boom")) == ("INTERNAL", True)


def test_retry_count_only_counts_the_retry_queue():
    headers = {
        "x-death": [
            {"queue": "learning.analysis.retry", "count": 2},
            {"queue": "some.other.queue", "count": 9},
        ]
    }
    assert retry_count(headers, "learning.analysis.retry") == 2
    assert retry_count(None, "learning.analysis.retry") == 0
    assert retry_count({"x-death": "not-a-list"}, "learning.analysis.retry") == 0


def test_explicit_retry_header_wins_over_x_death():
    headers = {
        "x-analysis-retry": 1,
        "x-death": [{"queue": "learning.analysis.retry", "count": 9}],
    }
    # 应用重投的计数才是准的；x-death 可能被其他死信路径污染
    assert retry_count(headers, "learning.analysis.retry") == 1
    # 非法值退回 x-death
    assert retry_count(
        {"x-analysis-retry": "oops",
         "x-death": [{"queue": "learning.analysis.retry", "count": 2}]},
        "learning.analysis.retry",
    ) == 2
