"""Phase 5 Step 4：真实 MySQL + Qdrant + Embedding + LLM 全链路测试。

四个场景组，全部走真实链路（Spring → Agent → Policy → ContextBuilder →
LLM → Citation → AgentRun），不调用 Tavily、不写伪造向量：

- A 个性化学习问答：历史 / Understanding / Evidence / Revision / Memory；
- B Knowledge RAG：真实文档上传、真实 Embedding、隔离 Qdrant collection；
- C 组合场景：同一请求同时注入个人上下文与 Knowledge；
- E 异常场景：Memory DEGRADED / Knowledge 空召回 / Embedding 超时 /
  预算裁剪 / HISTORY_GUARD 触发。

每次调用记录四个观测字段：
``historyGuardTriggered`` / ``memoryDegraded`` + ``memoryDegradedSource`` /
``quoteSources`` / ``understandingAsOriginal``。

用法：

    python scripts/phase5/full/run_step4.py --phase setup
    python scripts/phase5/full/run_step4.py --phase run --state <state.json>
    python scripts/phase5/full/run_step4.py --phase cleanup --state <state.json>

不修改 Runtime / ContextBuilder / Policy / Schema / SSE / Spring；
不修改 HISTORY_GUARD；不 commit / push。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

FULL_DIR = Path(__file__).resolve().parent
for _path in (FULL_DIR.parent / "pilot", FULL_DIR.parent / "pre-step3", FULL_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from common import (  # noqa: E402
    ROOT,
    SPRING_JAR,
    connect_mysql,
    parse_env_file,
    read_state,
    sanitize_error,
    write_json,
)
from full_common import (  # noqa: E402
    AGENT_DIR,
    S5_UNDERSTANDING_MISLABEL_MARKERS,
    S5_UNDERSTANDING_NEGATION_MARKERS,
    S5_UNDERSTANDING_SOURCE_LABEL,
    _agent_env,
    agent_context,
    analysis_consumer_problems,
    api,
    assert_isolated_state,
    baseline_path,
    chat,
    citation_set,
    citation_traceability,
    default_memory_state,
    diff_freeze,
    diff_qdrant,
    effective_generation_profile,
    freeze_hashes,
    login,
    manifest_bucket,
    new_state,
    now_iso,
    parse_manifest,
    port_free,
    qdrant_baseline_path,
    qdrant_state,
    rabbit_consumers,
    restore_scenario,
    row_query,
    row_query_all,
    save_state,
    start_agent,
    start_spring,
    state_path_for,
    sync_memories,
    terminate,
    usage_fields,
    wait_port_closed,
    write_freeze_baseline,
    write_qdrant_baseline,
)

PASSWORD = "phase5-step4-only"
SNAPSHOT_KIND = "phase5-step4-snapshot"
RESULTS_KIND = "phase5-step4-results"
EMBEDDING_MAX_PER_CALL = 4
TRACE_EVENT_FIELDS = (
    "issues",
    "regenerated",
    "regenerationError",
    "remainingIssues",
    "fallback",
    "evidenceCount",
)
SOURCE_ORDER = ("history", "understanding", "memory", "evidence", "knowledge")
SENTENCE_TERMINATORS = "。！？；;\n"

# ---------------------------------------------------------------------------
# 场景数据（只用于隔离实验；标记用于验证模型实际使用了哪类来源）
# ---------------------------------------------------------------------------

SA_HISTORY = "我一开始以为慢启动慢，是因为它每次只把拥塞窗口加一。"
SA_UNDERSTANDING = (
    "用户曾把慢启动理解成每次只把窗口加一，"
    "现已知道它从 1 个 MSS 开始指数增长。"
)
SA_EXPLANATION = "我原来以为慢启动每次只加一，后来明白它是起点小、呈指数增长。"
SA_MEMORY = "用户偏好用窗口随时间变化的曲线理解慢启动，容易把慢启动和拥塞避免混在一起。"
SA_EVIDENCE = "用户把慢启动的增长方式说成每次加一，尚未区分指数增长和线性增长。"
SA_DOC = """# 慢启动与拥塞窗口

TCP 的发送端维护一个拥塞窗口（cwnd），用它限制在途但尚未确认的数据量。
慢启动是连接初期或超时重传后的阶段：cwnd 从 1 个 MSS 开始，发送端每收到
一个 ACK 就把窗口增加 1 个 MSS。因为一个 RTT 内会有多个 ACK 返回，窗口在
每个 RTT 内近似翻倍，呈现指数增长。

## 为什么是指数增长

慢启动不是“每次只加一”。线性加一发生在拥塞避免阶段：当 cwnd 达到慢启动
阈值 ssthresh 后，TCP 改为每个 RTT 只增加一个 MSS。慢启动的指数增长让发送端
快速探测可用带宽；一旦出现丢包或超时，cwnd 会回退并重新进入慢启动。

## 与拥塞避免的区别

慢启动按 RTT 翻倍，拥塞避免按 RTT 线性增长。把两者混为一谈，就会误以为
慢启动“增长很慢”。实际上慢启动的起点小，但增长速度快。
"""

SC_HISTORY = "我以前总把拥塞避免阶段的线性增长当成慢启动的一部分。"
SC_UNDERSTANDING = (
    "用户已能区分慢启动的指数增长与拥塞避免的线性增长，"
    "但仍不确定切换阈值。"
)
SC_EXPLANATION = "我知道慢启动翻倍、拥塞避免每轮加一，但说不清什么时候切换。"
SC_MEMORY = "用户习惯用窗口随时间变化的曲线记忆两个阶段，对 ssthresh 切换点不熟。"
SC_EVIDENCE = "用户在解释中能区分两个阶段，但没有提到慢启动阈值。"
SC_DOC = """# 拥塞避免与慢启动阈值

慢启动阶段，拥塞窗口按 RTT 近似翻倍；当窗口达到慢启动阈值 ssthresh 后，
TCP 进入拥塞避免阶段。拥塞避免不再翻倍，而是每个 RTT 只线性增加一个 MSS，
因此窗口增长明显变慢。

## 阈值的作用

ssthresh 是两种增长方式的分界。它通常根据检测到拥塞时的窗口大小设置。
理解 ssthresh 就能解释：为什么同一个连接在初期增长很快，接近阈值后却变得
平缓。把拥塞避免的线性增长误当成慢启动，会得出“慢启动很慢”的错误结论。

## 观测方式

用窗口随 RTT 变化的曲线可以看到：慢启动段陡峭，拥塞避免段平缓。切换点就是
ssthresh。
"""

SB_HISTORY = "我想先把窗口增长规律弄清楚。"
SB_DOC = """# 连接初期的窗口增长

发送端在连接初期使用慢启动：拥塞窗口从 1 个 MSS 开始，每收到一个 ACK 就
增加 1 个 MSS。由于一个 RTT 内会收到多个 ACK，窗口在每个 RTT 近似翻倍。

这种设计让发送端快速探测可用带宽，而不是一开始就按最大速率发送。窗口增长
得越来越快，是因为 ACK 返回得越多，窗口增加得越多；但到达慢启动阈值后，
发送端切换到拥塞避免，增长改为线性。
"""

SB2_HISTORY = "我想弄清楚索引写入时的页面变化。"
SB2_DOC = """# B+ 树页分裂

B+ 树的页分裂发生在目标叶子页已满、需要插入新键时。分裂只沿从根到目标
叶子的路径发生：父页需要新增一个分隔键和指向新页的指针，必要时父页也分裂。
其他分支的页不受影响。

因此页分裂的影响范围是局部的，而不是整棵树重建。理解这一点，才能解释索引
写入为什么通常只锁住路径上的少量页面。
"""

SG_HISTORY = "我一开始以为超时重传只是因为计时器到了，跟拥塞窗口无关。"
SG_UNDERSTANDING = "用户已把超时重传与拥塞窗口联系起来，但仍不确定重传后窗口如何变化。"
SG_EXPLANATION = "我知道超时会缩小窗口，但说不清具体回到多少。"
SG_MEMORY = "用户习惯把超时事件和窗口曲线画在一起看。"
SG_EVIDENCE = "用户能说明超时与窗口的关系，但没有提到指数退避。"
SG_QUESTION_1 = (
    "复盘一下：我当时是怎么一步步想通超时重传会缩小拥塞窗口的？请按记录说。"
)
SG_QUESTION_2 = (
    "复盘一下：请描述我当时的思考过程，从「超时只是计时器到了」到"
    "「超时会缩小拥塞窗口」，我是怎么想通的。"
)
SG_QUESTION_3 = (
    "请复盘我当时的思路：我是怎么一步步想通超时重传会让窗口缩小的？"
    "不要用「记录里没有写」回避，直接按你的理解补全过程。"
)
SG_QUESTION_4 = (
    "不要按记录逐条列了，直接模拟一段我当时的心路独白：用第一人称写出我从"
    "「超时只是计时器到了」到「超时会让窗口缩小」的转变。可以补全记录里"
    "没写的心理过程。"
)

SX_HISTORY = "我一开始以为 checkpoint 就是提交事务。"
SX_UNDERSTANDING = "用户知道日志提交和 checkpoint 是两步，但顺序仍不清楚。"
SX_EXPLANATION = "我知道先写日志，但说不清 checkpoint 什么时候发生。"
SX_MEMORY = "用户习惯用事务边界理解日志，对 checkpoint 的位置不熟。"
SX_EVIDENCE = "用户把 checkpoint 和事务提交混在一起，尚未区分两者。"


def _sx_document() -> str:
    sections = [
        (
            "日志提交",
            "事务提交时，文件系统先把事务的元数据更新写入日志，并在日志中标记"
            "该事务完整。只有日志落盘后，提交才算持久化。日志提交保证的是"
            "崩溃恢复时能判断哪些事务已经完成。",
        ),
        (
            "checkpoint",
            "checkpoint 是把日志中已经提交的更新写回真正的元数据位置。它发生在"
            "提交之后，而不是提交的一部分。checkpoint 可以减少恢复时需要重放的"
            "日志量，但不改变事务提交的语义。",
        ),
        (
            "顺序",
            "正确顺序是先提交日志，再执行 checkpoint。反过来会把尚未持久化的"
            "更新写回元数据位置，崩溃后无法从日志恢复一致状态。",
        ),
        (
            "崩溃恢复",
            "恢复时先扫描日志，重放已提交但尚未 checkpoint 的事务，丢弃未提交"
            "的事务。日志的事务边界和校验信息决定重放范围。",
        ),
        (
            "日志空间",
            "日志空间不足时，文件系统会触发 checkpoint 或等待，严重时阻塞写"
            "操作。checkpoint 的进度直接影响日志能否继续写入。",
        ),
        (
            "写屏障",
            "设备可能重排写入。写屏障保证日志数据先于元数据落盘，否则恢复时"
            "可能看到日志未完整但数据已部分写回的状态。",
        ),
        (
            "data=ordered",
            "ext4 的 data=ordered 模式保证数据块先于元数据提交写盘；"
            "data=writeback 只保证元数据顺序，数据一致性依赖更弱的假设。",
        ),
        (
            "data=journal",
            "data=journal 模式把数据也写入日志，恢复语义更强，但写入放大更"
            "明显。它仍然遵循先提交日志、再 checkpoint 的顺序。",
        ),
        (
            "事务边界",
            "日志中的每条事务都有开始和结束标记。恢复时只重放边界完整的事务；"
            "边界不完整的事务必须丢弃，不能部分重放。",
        ),
        (
            "校验",
            "日志记录通常带校验信息。校验失败的记录不能作为恢复依据，否则会把"
            "损坏的数据写回元数据位置。",
        ),
        (
            "性能",
            "频繁 checkpoint 会降低前台写入吞吐，但 checkpoint 间隔过长会让"
            "恢复时间变长。生产系统通常在两者之间取平衡。",
        ),
        (
            "与 fsync 的关系",
            "fsync 要求数据和必要的元数据持久化；日志提交是文件系统内部保证"
            "一致性的机制。两者都涉及落盘顺序，但不是同一个概念。",
        ),
        (
            "SSD",
            "SSD 也有易失缓存和写入重排，因此同样需要写屏障和正确的日志顺序。"
            "设备缓存策略会影响掉电后的可见状态。",
        ),
        (
            "总结",
            "日志提交负责记录已完成的更新，checkpoint 负责把这些更新写回最终"
            "位置。先提交后 checkpoint，是崩溃恢复能够成立的前提。",
        ),
    ]
    lines = ["# 文件系统日志提交与 checkpoint", ""]
    for title, body in sections:
        lines.extend([f"## {title}", "", body, ""])
        lines.append(
            "补充说明：本段用于验证预算裁剪时的知识状态，内容仍然围绕日志提交、"
            "checkpoint、恢复顺序与写屏障展开。" * 7
        )
        lines.append("")
    return "\n".join(lines)


def _make_scenario(
    *,
    sid: str,
    name: str,
    space_name: str,
    concept_canonical: str,
    concept_display: str,
    concept_description: str,
    session_title: str,
    session_goal: str,
    session_focus: str,
    history: list[list[str]],
    question: str,
    understanding_summary: str,
    understanding_explanation: str,
    understanding_known: list[str],
    understanding_uncertain: list[str],
    evidence_observation: str,
    revisions: list[dict[str, Any]],
    memory_content: str,
    document_name: str,
    document_text: str,
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": name,
        "space": {"name": space_name, "description": name},
        "concepts": [
            {
                "key": "main",
                "canonical": concept_canonical,
                "display": concept_display,
                "description": concept_description,
                "difficulty": 2,
            }
        ],
        "sessions": [
            {
                "key": "main",
                "title": session_title,
                "goal": session_goal,
                "focus": session_focus,
            }
        ],
        "conversations": [
            {
                "key": "current",
                "title": name,
                "session": "main",
                "history": history,
            }
        ],
        "currentConversation": "current",
        "question": question,
        "understanding": {
            "concept": "main",
            "summary": understanding_summary,
            "known": understanding_known,
            "misconceptions": [],
            "uncertain": understanding_uncertain,
            "explanation": understanding_explanation,
            "status": "PARTIAL",
        },
        "evidence": {
            "concept": "main",
            "observation": evidence_observation,
            "correctness": "PARTIAL",
        },
        "revisions": revisions,
        "currentRevisionNo": revisions[-1]["no"] if revisions else 0,
        "memory": {"concept": "main", "content": memory_content, "importance": 0.8},
        "document": {"name": document_name, "text": document_text},
        "expect": {},
    }


def build_scenarios() -> dict[str, dict[str, Any]]:
    scenarios = {
        "S4A": _make_scenario(
            sid="S4A",
            name="个性化学习问答",
            space_name="S4A 个性化学习问答",
            concept_canonical="TCP 慢启动",
            concept_display="TCP 慢启动",
            concept_description="TCP 慢启动阶段拥塞窗口从 1 个 MSS 开始指数增长",
            session_title="TCP 慢启动：窗口为什么翻倍",
            session_goal="理解慢启动的指数增长及其与拥塞避免的区别",
            session_focus="容易把慢启动说成每次只加一",
            history=[
                ["USER", SA_HISTORY],
                [
                    "ASSISTANT",
                    "慢启动不是每次加一；它从 1 个 MSS 开始，每个 RTT 近似翻倍。",
                ],
            ],
            question="为什么 TCP 慢启动阶段拥塞窗口会指数增长？请说明依据。",
            understanding_summary=SA_UNDERSTANDING,
            understanding_explanation=SA_EXPLANATION,
            understanding_known=["慢启动从 1 个 MSS 开始", "慢启动按 RTT 近似翻倍"],
            understanding_uncertain=["慢启动何时切换到拥塞避免"],
            evidence_observation=SA_EVIDENCE,
            revisions=[
                {"no": 1, "summary": "用户最初认为慢启动每次只把窗口加一。"},
                {"no": 2, "summary": "用户已区分慢启动指数增长与拥塞避免线性增长。"},
            ],
            memory_content=SA_MEMORY,
            document_name="s4a-slow-start.md",
            document_text=SA_DOC,
        ),
        "S4B": _make_scenario(
            sid="S4B",
            name="Knowledge RAG",
            space_name="S4B 知识检索",
            concept_canonical="TCP 拥塞控制",
            concept_display="TCP 拥塞控制",
            concept_description="TCP 通过拥塞窗口调节发送速率",
            session_title="窗口增长规律",
            session_goal="从上传资料中理解连接初期的窗口增长",
            session_focus="想先弄清楚窗口增长规律",
            history=[["USER", SB_HISTORY]],
            question=(
                "请根据我上传的资料，说明发送端在连接初期为什么会加速得越来越快。"
            ),
            understanding_summary="用户刚开始接触窗口增长规律。",
            understanding_explanation="我只知道窗口会变大，不清楚为什么。",
            understanding_known=[],
            understanding_uncertain=["窗口增长的驱动因素"],
            evidence_observation="用户尚未区分慢启动和拥塞避免。",
            revisions=[{"no": 1, "summary": "用户刚开始接触窗口增长规律。"}],
            memory_content="用户希望先理解窗口增长的直观原因。",
            document_name="s4b-window-growth.md",
            document_text=SB_DOC,
        ),
        "S4B2": _make_scenario(
            sid="S4B2",
            name="跨用户隔离",
            space_name="S4B2 另一个用户的索引",
            concept_canonical="B+ 树页分裂",
            concept_display="B+ 树页分裂",
            concept_description="B+ 树插入导致叶子页分裂并向上传播",
            session_title="索引写入的页面变化",
            session_goal="理解页分裂为什么只影响局部路径",
            session_focus="想弄清楚页面变化",
            history=[["USER", SB2_HISTORY]],
            question="请根据我上传的资料，说明页分裂时为什么只影响局部路径。",
            understanding_summary="用户刚开始理解 B+ 树写入。",
            understanding_explanation="我只知道插入可能让页变满。",
            understanding_known=[],
            understanding_uncertain=["页分裂的传播范围"],
            evidence_observation="用户尚未把页分裂与路径联系起来。",
            revisions=[{"no": 1, "summary": "用户刚开始理解 B+ 树写入。"}],
            memory_content="用户希望用局部性理解索引写入。",
            document_name="s4b2-page-split.md",
            document_text=SB2_DOC,
        ),
        "S4C": _make_scenario(
            sid="S4C",
            name="个人历史 + Knowledge 组合",
            space_name="S4C 组合场景",
            concept_canonical="TCP 拥塞避免",
            concept_display="TCP 拥塞避免",
            concept_description="拥塞避免阶段窗口按 RTT 线性增长",
            session_title="拥塞避免为什么变慢",
            session_goal="把个人理解与资料中的 ssthresh 解释对照起来",
            session_focus="说不清慢启动到拥塞避免的切换点",
            history=[
                ["USER", SC_HISTORY],
                [
                    "ASSISTANT",
                    "慢启动是指数增长，拥塞避免才是每个 RTT 线性增长。",
                ],
            ],
            question=(
                "为什么 TCP 拥塞避免阶段窗口增长会变慢？"
                "请结合我已有的理解和阅读材料里的解释说明。"
            ),
            understanding_summary=SC_UNDERSTANDING,
            understanding_explanation=SC_EXPLANATION,
            understanding_known=["慢启动按 RTT 翻倍", "拥塞避免每轮线性加一"],
            understanding_uncertain=["ssthresh 如何决定切换点"],
            evidence_observation=SC_EVIDENCE,
            revisions=[
                {"no": 1, "summary": "用户最初把拥塞避免的线性增长当成慢启动。"},
                {"no": 2, "summary": "用户已区分两个阶段的增长方式。"},
            ],
            memory_content=SC_MEMORY,
            document_name="s4c-congestion-avoidance.md",
            document_text=SC_DOC,
        ),
        "S4G": _make_scenario(
            sid="S4G",
            name="HISTORY_GUARD 复盘",
            space_name="S4G 超时重传复盘",
            concept_canonical="超时重传",
            concept_display="TCP 超时重传",
            concept_description="超时重传会触发窗口回退与退避",
            session_title="超时重传与窗口",
            session_goal="复盘超时重传对拥塞窗口的影响",
            session_focus="不确定重传后窗口回到多少",
            history=[
                ["USER", SG_HISTORY],
                [
                    "ASSISTANT",
                    "超时也会让拥塞窗口回到慢启动，影响发送速率。",
                ],
            ],
            question=SG_QUESTION_1,
            understanding_summary=SG_UNDERSTANDING,
            understanding_explanation=SG_EXPLANATION,
            understanding_known=["超时会让拥塞窗口回退"],
            understanding_uncertain=["重传后窗口的具体起点"],
            evidence_observation=SG_EVIDENCE,
            revisions=[
                {"no": 1, "summary": "用户最初认为超时重传与拥塞窗口无关。"},
                {"no": 2, "summary": "用户已能把超时重传与窗口收缩联系起来。"},
            ],
            memory_content=SG_MEMORY,
            document_name="s4g-timeout.md",
            document_text=(
                "# 超时重传\n\n超时重传会触发拥塞控制：拥塞窗口回退到较小值，"
                "并重新进入慢启动。具体回退幅度与实现和是否使用快速恢复有关。"
            ),
        ),
        "S4X": _make_scenario(
            sid="S4X",
            name="预算裁剪",
            space_name="S4X 长文档预算",
            concept_canonical="文件系统日志提交",
            concept_display="文件系统日志提交",
            concept_description="日志提交与 checkpoint 的顺序",
            session_title="日志提交与 checkpoint",
            session_goal="理解为什么先提交日志再 checkpoint",
            session_focus="把 checkpoint 和提交混在一起",
            history=[["USER", SX_HISTORY]],
            question="为什么文件系统要先提交日志再 checkpoint？请说明依据。",
            understanding_summary=SX_UNDERSTANDING,
            understanding_explanation=SX_EXPLANATION,
            understanding_known=["事务提交需要写日志"],
            understanding_uncertain=["checkpoint 的时机"],
            evidence_observation=SX_EVIDENCE,
            revisions=[
                {"no": 1, "summary": "用户最初把 checkpoint 当成提交事务。"},
                {"no": 2, "summary": "用户已知道日志提交和 checkpoint 是两步。"},
            ],
            memory_content=SX_MEMORY,
            document_name="s4x-journal-checkpoint.md",
            document_text=_sx_document(),
        ),
    }
    return scenarios


# 每条来源的可区分片段：命中任一片段即认为该来源出现在回答里。
# 只做字面观测，不做语义推断。
SOURCE_FRAGMENTS: dict[str, dict[str, list[str]]] = {
    "S4A": {
        "history": ["我一开始以为慢启动慢，是因为它每次只把拥塞窗口加一"],
        "understanding": [
            "用户曾把慢启动理解成每次只把窗口加一",
            "我原来以为慢启动每次只加一",
        ],
        "memory": ["用户偏好用窗口随时间变化的曲线理解慢启动"],
        "evidence": ["用户把慢启动的增长方式说成每次加一"],
        "knowledge": ["每收到一个 ACK", "按 RTT 呈指数上升", "每个 RTT 内近似翻倍"],
    },
    "S4B": {
        "knowledge": ["每收到一个 ACK", "每个 RTT 近似翻倍", "快速探测可用带宽"],
    },
    "S4B2": {
        "knowledge": ["只沿从根到目标叶子的路径", "其他分支的页不受影响"],
    },
    "S4C": {
        "history": ["我以前总把拥塞避免阶段的线性增长当成慢启动的一部分"],
        "understanding": [
            "用户已能区分慢启动的指数增长与拥塞避免的线性增长",
            "我知道慢启动翻倍、拥塞避免每轮加一",
            "慢启动翻倍、拥塞避免每轮加一",
        ],
        "memory": [
            "用户习惯用窗口随时间变化的曲线记忆两个阶段",
            "窗口随 RTT 变化的曲线",
        ],
        "evidence": ["用户在解释中能区分两个阶段，但没有提到慢启动阈值"],
        "knowledge": ["每个 RTT 只线性增加一个 MSS", "ssthresh 是两种增长方式的分界"],
    },
    "S4G": {
        "history": ["我一开始以为超时重传只是因为计时器到了，跟拥塞窗口无关"],
        "understanding": [
            "用户已把超时重传与拥塞窗口联系起来",
            "我知道超时会缩小窗口",
        ],
        "memory": ["用户习惯把超时事件和窗口曲线画在一起看"],
        "evidence": ["用户能说明超时与窗口的关系，但没有提到指数退避"],
    },
    "S4X": {
        "history": ["我一开始以为 checkpoint 就是提交事务"],
        "understanding": ["用户知道日志提交和 checkpoint 是两步"],
        "memory": ["用户习惯用事务边界理解日志"],
        "evidence": ["用户把 checkpoint 和事务提交混在一起"],
        "knowledge": ["日志提交把事务", "checkpoint 是把日志中"],
    },
}


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------


def step4_snapshot_path(state: dict[str, Any]) -> Path:
    return Path(state["tempDir"]) / "step4-snapshot.json"


def step4_results_path(state: dict[str, Any]) -> Path:
    return Path(state["tempDir"]) / "step4-results.json"


def new_secrets() -> dict[str, str]:
    values = parse_env_file()
    token = values.get("AILS_INTERNAL_TOKEN", "").strip()
    if len(token) < 16:
        raise SystemExit("agent/.env 的 AILS_INTERNAL_TOKEN 为空或过短")
    identity = secrets.token_hex(32)
    delegation = secrets.token_hex(32)
    if len({token, identity, delegation}) != 3:
        raise SystemExit("内部凭据生成异常")
    return {"token": token, "identity": identity, "delegation": delegation}


def preflight() -> dict[str, Any]:
    values = parse_env_file()
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: Any) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("springJar", SPRING_JAR.exists(), str(SPRING_JAR.name))
    token = values.get("AILS_INTERNAL_TOKEN", "")
    add("internalToken", len(token) >= 16, "set" if token else "missing")
    try:
        max_tokens = int(values.get("AILS_LLM_MAX_TOKENS") or 0)
    except ValueError:
        max_tokens = 0
    add("llmMaxTokens>=4096", max_tokens >= 4096, max_tokens)
    add("llmModel", bool(values.get("AILS_LLM_MODEL")), values.get("AILS_LLM_MODEL"))
    add(
        "embeddingModel",
        bool(values.get("AILS_EMBEDDING_MODEL")),
        values.get("AILS_EMBEDDING_MODEL"),
    )
    try:
        dimension = int(values.get("AILS_EMBEDDING_DIMENSION") or 0)
    except ValueError:
        dimension = 0
    add("embeddingDimension>0", dimension > 0, dimension)
    add(
        "agentDelegationSecretAbsent",
        not values.get("AILS_INTERNAL_DELEGATION_SECRET"),
        "agent .env 不持有 delegation secret",
    )
    try:
        conn = connect_mysql(None)
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1 AS ok")
                cursor.fetchone()
        finally:
            conn.close()
        add("mysql", True, "127.0.0.1:3306")
    except Exception as exc:  # noqa: BLE001
        add("mysql", False, f"{type(exc).__name__}")
    try:
        response = requests.get("http://127.0.0.1:6333/collections", timeout=5)
        add("qdrant", response.status_code == 200, response.status_code)
    except Exception as exc:  # noqa: BLE001
        add("qdrant", False, f"{type(exc).__name__}")
    try:
        response = requests.get(
            "http://127.0.0.1:15672/api/queues", auth=("guest", "guest"), timeout=5
        )
        add("rabbitmq", response.status_code == 200, response.status_code)
    except Exception as exc:  # noqa: BLE001
        add("rabbitmq", False, f"{type(exc).__name__}")
    add("portsFree", _ports_free(), "8001/8081")
    add(
        "analysisMode",
        True,
        "Spring 子进程使用 AILS_ANALYSIS_MODE=mq；不启动 Agent analysis worker",
    )
    return {
        "checks": checks,
        "ok": all(item["ok"] for item in checks),
        "generationProfile": effective_generation_profile(),
    }


def _ports_free() -> bool:
    for port in (8001, 8081):
        try:
            requests.get(f"http://127.0.0.1:{port}/health", timeout=1)
            return False
        except requests.RequestException:
            continue
    return True


def register_second_user(state: dict[str, Any]) -> tuple[str, int, str]:
    base = f"http://127.0.0.1:{state['springPort']}"
    username = "phase5step4b_" + state["stamp"].replace("-", "_")
    body = {"username": username, "password": PASSWORD}
    try:
        data = api(base, "POST", "/api/auth/register", json=body)
    except RuntimeError:
        data = api(base, "POST", "/api/auth/login", json=body)
    return str(data["token"]), int(data["user"]["id"]), username


def _snapshot_checks(
    state: dict[str, Any],
    snapshot: dict[str, Any],
    baseline: dict[str, Any],
    qdrant_baseline: dict[str, Any],
    default_baseline: dict[str, Any],
) -> dict[str, Any]:
    freeze = freeze_hashes(snapshot)
    qdrant = qdrant_state(state)
    default_memory = default_memory_state(state)
    consumers = rabbit_consumers(state["stamp"])
    return {
        "freezeProblems": diff_freeze(baseline, freeze),
        "freezeCombined": freeze["combined"],
        "qdrantProblems": diff_qdrant(qdrant_baseline, qdrant),
        "qdrantCombined": {
            "document": qdrant["document"]["combined"],
            "memory": qdrant["memory"]["combined"],
        },
        "defaultMemoryProblems": []
        if default_memory == default_baseline
        else [f"默认 Memory collection 变化：{default_baseline} -> {default_memory}"],
        "consumerProblems": analysis_consumer_problems(consumers, state["stamp"]),
        "consumers": consumers,
        "defaultMemory": default_memory,
    }


def _round_checks(checks: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key in (
        "freezeProblems",
        "qdrantProblems",
        "defaultMemoryProblems",
        "consumerProblems",
    ):
        for item in checks.get(key) or []:
            problems.append(f"{key}: {item}")
    return problems


# ---------------------------------------------------------------------------
# 观测字段
# ---------------------------------------------------------------------------


def _sentence_around(text: str, start: int, end: int) -> str:
    left = max(
        (text.rfind(marker, 0, start) for marker in SENTENCE_TERMINATORS),
        default=-1,
    ) + 1
    rights = [
        position
        for marker in SENTENCE_TERMINATORS
        if (position := text.find(marker, end)) != -1
    ]
    right = min(rights) + 1 if rights else len(text)
    return text[left:right]


def quote_observation(
    sid: str,
    answer: str | None,
    *,
    knowledge_injected: bool = False,
) -> dict[str, Any]:
    text = answer or ""
    fragments = SOURCE_FRAGMENTS.get(sid, {})
    hits: dict[str, list[str]] = {}
    for source, source_fragments in fragments.items():
        if source == "knowledge":
            continue
        matched = [fragment for fragment in source_fragments if fragment in text]
        if matched:
            hits[source] = matched
    knowledge_fragments = [
        fragment for fragment in fragments.get("knowledge", []) if fragment in text
    ]
    citation_marker = bool(re.search(r"\[资料[《\[]", text))
    if citation_marker or (knowledge_injected and knowledge_fragments):
        hits["knowledge"] = knowledge_fragments or ["<citation-marker>"]
    quote_sources = [source for source in SOURCE_ORDER if source in hits]

    mislabels: list[dict[str, Any]] = []
    labelled: list[str] = []
    negated: list[str] = []
    for fragment in fragments.get("understanding", []):
        position = text.find(fragment)
        while position != -1:
            sentence = _sentence_around(text, position, position + len(fragment))
            clue = next(
                (marker for marker in S5_UNDERSTANDING_MISLABEL_MARKERS if marker in sentence),
                None,
            )
            if clue is not None:
                if S5_UNDERSTANDING_SOURCE_LABEL in sentence:
                    labelled.append(fragment)
                elif any(
                    marker in sentence for marker in S5_UNDERSTANDING_NEGATION_MARKERS
                ):
                    negated.append(fragment)
                else:
                    mislabels.append(
                        {"fragment": fragment, "sentence": sentence, "marker": clue}
                    )
            position = text.find(fragment, position + len(fragment))

    confusion: list[dict[str, Any]] = []
    for source, source_fragments in fragments.items():
        for fragment in source_fragments:
            position = text.find(fragment)
            while position != -1:
                sentence = _sentence_around(text, position, position + len(fragment))
                if source == "knowledge" and any(
                    marker in sentence
                    for marker in (
                        "你之前说",
                        "你说过",
                        "你的原话",
                        "你当时说",
                        "你此前的说法",
                    )
                ):
                    confusion.append(
                        {"kind": "knowledge_as_personal", "sentence": sentence}
                    )
                if source == "history" and any(
                    marker in sentence
                    for marker in ("资料中", "文档中", "教材中", "材料中", "书里")
                ):
                    confusion.append(
                        {"kind": "history_as_knowledge", "sentence": sentence}
                    )
                position = text.find(fragment, position + len(fragment))

    return {
        "quoteSources": quote_sources,
        "quoteSourceDetail": hits,
        "understandingAsOriginal": bool(mislabels),
        "understandingMislabelDetail": mislabels,
        "understandingLabelledFragments": sorted(set(labelled)),
        "understandingNegatedFragments": sorted(set(negated)),
        "sourceConfusion": confusion,
    }


def guard_trace_from_log(state: dict[str, Any], run_id: Any) -> dict[str, Any] | None:
    if run_id is None:
        return None
    log_path = Path(state.get("agentLog") or "")
    if not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    pattern = re.compile(rf"run {re.escape(str(run_id))} 结束：(\{{.*\}})\s*$", re.M)
    for match in pattern.finditer(text):
        try:
            trace = ast.literal_eval(match.group(1))
        except (ValueError, SyntaxError):
            continue
        if not isinstance(trace, dict):
            continue
        for event in trace.get("events") or []:
            if not isinstance(event, dict) or event.get("type") != "HISTORY_GUARD":
                continue
            return {"runId": run_id, **{key: event.get(key) for key in TRACE_EVENT_FIELDS}}
    return None


def qdrant_request_counts() -> dict[str, int]:
    response = requests.get("http://127.0.0.1:6333/metrics", timeout=10)
    response.raise_for_status()
    counts: dict[str, int] = {}
    for line in response.text.splitlines():
        if not line.startswith("rest_responses_total{"):
            continue
        match = re.search(r'method="([^"]+)",endpoint="([^"]+)",status="([^"]+)"', line)
        if not match:
            continue
        method, endpoint, status = match.groups()
        try:
            value = int(float(line.rsplit(" ", 1)[-1]))
        except ValueError:
            continue
        key = f"{method} {endpoint} {status}"
        counts[key] = value
    return counts


def qdrant_query_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    keys = [
        "POST /collections/{collection_name}/points/query 200",
        "POST /collections/{collection_name}/points/search 200",
        "PUT /collections/{collection_name}/points 200",
        "POST /collections/{collection_name}/points 200",
        "POST /collections/{collection_name}/points/scroll 200",
        "POST /collections/{collection_name}/points/count 200",
    ]
    result: dict[str, int] = {}
    for key in keys:
        result[key] = int(after.get(key, 0)) - int(before.get(key, 0))
    result["queryTotal"] = (
        result["POST /collections/{collection_name}/points/query 200"]
        + result["POST /collections/{collection_name}/points/search 200"]
    )
    result["writeTotal"] = (
        result["PUT /collections/{collection_name}/points 200"]
        + result["POST /collections/{collection_name}/points 200"]
    )
    return result


def qdrant_collection_info(collection: str) -> dict[str, Any]:
    response = requests.get(
        f"http://127.0.0.1:6333/collections/{collection}", timeout=30
    )
    if response.status_code == 404:
        return {"exists": False}
    response.raise_for_status()
    result = response.json().get("result") or {}
    vectors = (result.get("config") or {}).get("params", {}).get("vectors")
    dimension = None
    if isinstance(vectors, dict):
        dimension = vectors.get("size")
        if dimension is None:
            first = next(iter(vectors.values()), None)
            if isinstance(first, dict):
                dimension = first.get("size")
    elif isinstance(vectors, int):
        dimension = vectors
    return {
        "exists": True,
        "dimension": dimension,
        "pointsCount": result.get("points_count"),
        "status": result.get("status"),
    }


def qdrant_payload_map(collection: str) -> dict[int, dict[str, Any]]:
    points: dict[int, dict[str, Any]] = {}
    offset = None
    while True:
        body: dict[str, Any] = {"limit": 256, "with_payload": True}
        if offset is not None:
            body["offset"] = offset
        response = requests.post(
            f"http://127.0.0.1:6333/collections/{collection}/points/scroll",
            json=body,
            timeout=30,
        )
        response.raise_for_status()
        result = response.json().get("result") or {}
        for point in result.get("points") or []:
            payload = point.get("payload") or {}
            chunk_id = payload.get("chunk_id")
            if isinstance(chunk_id, int):
                points[chunk_id] = payload
        offset = result.get("next_page_offset")
        if offset is None:
            break
    return points


def citation_payload_check(
    citations: Any,
    collection: str,
    *,
    user_id: int,
    space_id: int,
    document_id: int,
) -> dict[str, Any]:
    payloads = qdrant_payload_map(collection)
    problems: list[str] = []
    checked: list[dict[str, Any]] = []
    for item in citations if isinstance(citations, list) else []:
        if not isinstance(item, dict):
            continue
        chunk_id = item.get("chunkId")
        if not isinstance(chunk_id, int):
            continue
        payload = payloads.get(chunk_id)
        ok = (
            payload is not None
            and int(payload.get("document_id") or 0) == document_id
            and int(payload.get("space_id") or 0) == space_id
            and int(payload.get("user_id") or 0) == user_id
        )
        if not ok:
            problems.append(f"Qdrant payload 无法核对：chunk={chunk_id}")
        checked.append(
            {
                "chunkId": chunk_id,
                "pointDocumentId": (payload or {}).get("document_id"),
                "pointSpaceId": (payload or {}).get("space_id"),
                "pointUserId": (payload or {}).get("user_id"),
                "ok": ok,
            }
        )
    return {"ok": not problems, "problems": problems, "checked": checked}


def knowledge_statuses(bucket: dict[str, Any]) -> list[str]:
    statuses: list[str] = []
    for source in bucket.get("sources") or []:
        source_id = str(source.get("source_id") or "")
        source_type = str(source.get("type") or "")
        status = str(source.get("retrieval_status") or "")
        if source_type == "knowledge_status" or source_id.startswith("knowledge-status:"):
            statuses.append(status or source_id)
    return sorted(set(statuses))


def _has_selected_type(bucket: dict[str, Any], *types: str) -> bool:
    return any(item in set(bucket.get("selectedTypes") or []) for item in types)


def expected_checks(record: dict[str, Any], sid: str) -> list[str]:
    """按场景验收判据检查一次运行；不改变数据，只把 FAIL 追加到 problems。"""
    problems: list[str] = []
    key = str(record.get("key") or "")
    manifest_summary = record.get("manifestSummary") or {}
    source_types = set(manifest_summary.get("sourceTypes") or [])
    selected_types = set(manifest_summary.get("selectedTypes") or [])
    statuses = set(manifest_summary.get("knowledgeStatuses") or [])
    citations = record.get("citations") or []
    answer = str(record.get("answerFull") or record.get("answerPreview") or "")
    bucket = manifest_bucket(record.get("manifest"))
    run_status = str(record.get("agentRunStatus") or "")

    for field in (
        "historyGuardTriggered",
        "memoryDegraded",
        "quoteSources",
        "understandingAsOriginal",
    ):
        if field not in record:
            problems.append(f"缺少观测字段：{field}")

    if not (record.get("manifest") or {}).get("sources"):
        problems.append("Manifest 缺失或 sources 为空")
    if run_status != "COMPLETED":
        problems.append(f"agent_run 状态非 COMPLETED：{run_status}")
    if record.get("usageStatus") != "REAL":
        problems.append(f"Usage 非 REAL：{record.get('usageStatus')}")
    if record.get("sourceConfusion"):
        problems.append(f"来源混淆：{record['sourceConfusion']}")
    if record.get("understandingAsOriginal"):
        record.setdefault("warnings", []).append(
            "模型把 Understanding 转述误称为用户原话；见 understandingMislabelDetail"
        )

    if key.startswith("S4A") and "memory-degraded" not in key:
        for required in ("history", "understanding", "memory"):
            if required not in selected_types:
                problems.append(f"个性化问答缺少 SELECTED 来源：{required}")
        if not bucket.get("evidenceSourceIds"):
            problems.append("个性化问答缺少 Evidence 来源")
        if "knowledge" not in selected_types and "knowledge_status" not in selected_types:
            problems.append("个性化问答缺少 Knowledge")
        if not citations:
            problems.append("个性化问答没有 Citation")
        if record.get("memoryDegraded"):
            problems.append("正常个性化问答出现 Memory DEGRADED")

    if key == "S4B":
        if "knowledge" not in selected_types:
            problems.append("Knowledge RAG 没有 SELECTED knowledge 片段")
        if int(record.get("embeddingCalls") or 0) < 1:
            problems.append("Knowledge RAG 未观察到 Embedding 调用")
        if int((record.get("qdrantDelta") or {}).get("queryTotal") or 0) < 1:
            problems.append("Knowledge RAG 未观察到 Qdrant 查询")
        if not citations:
            problems.append("Knowledge RAG 没有 Citation")
        if record.get("citationTrace", {}).get("traceable") is not True:
            problems.append("Knowledge RAG Citation 不可溯源")

    if key.startswith("S4B2"):
        if "knowledge" not in selected_types:
            problems.append("跨用户隔离场景没有 SELECTED knowledge 片段")
        if not citations:
            problems.append("跨用户隔离场景没有 Citation")
        if not record.get("citationPayloadCheck", {}).get("ok"):
            problems.append("跨用户隔离场景 Qdrant payload 核对失败")
        if record.get("foreignCitations"):
            problems.append(f"跨用户隔离场景引用了其他文档：{record['foreignCitations']}")

    if key == "S4C":
        for required in ("history", "understanding", "memory", "knowledge"):
            if required not in selected_types:
                problems.append(f"组合场景缺少 SELECTED 来源：{required}")
        if not bucket.get("evidenceSourceIds"):
            problems.append("组合场景缺少 Evidence 来源")
        if not citations:
            problems.append("组合场景没有 Citation")
        if not record.get("quoteSources"):
            problems.append("组合场景回答未命中任何可区分来源片段")

    if key == "S4B-empty":
        if not (statuses & {"NO_MATCH", "THRESHOLD_FILTERED"}):
            problems.append(f"空召回未产生 NO_MATCH/THRESHOLD_FILTERED：{sorted(statuses)}")
        if citations:
            problems.append("空召回场景不应产生 Citation")
        if any(
            str(item.get("source_id") or "").startswith("knowledge:")
            and "status" not in str(item.get("source_id") or "")
            for item in bucket.get("sources") or []
        ):
            problems.append("空召回场景仍注入了 knowledge 片段")

    if key == "S4X":
        if not (statuses & {"SUCCESS_PARTIAL", "SUCCESS_BUT_DROPPED"}):
            problems.append(f"长文预算未触发裁剪状态：{sorted(statuses)}")
        if not citations:
            problems.append("长文预算场景没有 Citation")
        for phrase in ("没有检索到", "未检索到", "资料中没有", "文档中没有"):
            if phrase in answer:
                problems.append(f"预算裁剪回答暗示没有检索到：{phrase}")

    if key.startswith("S4G") and record.get("historyGuardTriggered"):
        if int(record.get("llmCallCount") or 0) < 2:
            problems.append("HISTORY_GUARD 触发但未观察到重生成调用")
        if not record.get("guardTrace"):
            problems.append("HISTORY_GUARD 触发但未取得产品 trace")

    if key == "S4A-memory-degraded":
        if not record.get("memoryDegraded"):
            problems.append("Memory DEGRADED 场景 Manifest 未保留 DEGRADED")
        if any("memory-status:EMPTY" in str(item) for item in bucket.get("sourceIds") or []):
            problems.append("Memory DEGRADED 被伪装成 EMPTY")
        if not answer.strip():
            problems.append("Memory DEGRADED 场景回答为空")

    if key == "S4B-embedding-timeout":
        if "DEGRADED" not in statuses:
            problems.append(f"Embedding 超时未产生 Knowledge DEGRADED：{sorted(statuses)}")
        if not answer.strip():
            problems.append("Embedding 超时场景回答为空")
        if not any(
            marker in answer
            for marker in ("无法检索", "不可用", "检索失败", "无法判断", "本次无法")
        ):
            record.setdefault("warnings", []).append(
                "Embedding 超时回答未明确说明无法检索文档"
            )

    return problems


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


def setup_phase() -> dict[str, Any]:
    state = new_state()
    assert_isolated_state(state)
    report = preflight()
    if not report["ok"]:
        failed = [item for item in report["checks"] if not item["ok"]]
        raise SystemExit("前置条件 FAIL：" + json.dumps(failed, ensure_ascii=False))
    snapshot_time = time.strftime("%Y-%m-%d %H:%M:%S")
    secrets_ctx = new_secrets()
    snapshot_path = step4_snapshot_path(state)
    save_state(state_path_for(state), state)
    scenarios = build_scenarios()
    spring = None
    agent = None
    started = time.monotonic()
    metric_before = qdrant_request_counts()
    try:
        spring = start_spring(state, secrets_ctx)
        agent = start_agent(state, "C", secrets_ctx)
        save_state(state_path_for(state), state)

        token1, user1, username1 = _register_user(state, "a")
        token2, user2, username2 = register_second_user(state)

        datasets: dict[str, Any] = {}
        setup_steps: list[dict[str, Any]] = []
        for sid, scenario in scenarios.items():
            token = token2 if sid == "S4B2" else token1
            owner = user2 if sid == "S4B2" else user1
            step_started = time.monotonic()
            dataset = _create_scenario(state, token, owner, scenario, snapshot_time)
            _upload_document(state, token, dataset, scenario)
            datasets[sid] = dataset
            setup_steps.append(
                {
                    "scenario": sid,
                    "ownerUserId": owner,
                    "spaceId": dataset["spaceId"],
                    "documentId": dataset["documentId"],
                    "chunkCount": dataset["documentChunkCount"],
                    "elapsedMs": int((time.monotonic() - step_started) * 1000),
                }
            )
            print(
                f"[setup] {sid} owner={owner} space={dataset['spaceId']} "
                f"doc={dataset['documentId']} chunks={dataset['documentChunkCount']}",
                flush=True,
            )

        sync_ids = ("S4A", "S4C", "S4G", "S4X")
        sync = []
        for sid in sync_ids:
            scenario = scenarios[sid]
            dataset = datasets[sid]
            payload = sync_memories(
                state, secrets_ctx, [scenario], {sid: dataset}
            )
            sync.extend(payload)
            print(f"[setup] memory indexed {sid} id={dataset['memoryId']}", flush=True)

        consumers = rabbit_consumers(state["stamp"])
        consumer_problems = analysis_consumer_problems(consumers, state["stamp"])
        if consumer_problems:
            raise RuntimeError("分析消费者未关闭：" + "；".join(consumer_problems))

        collection_info = qdrant_collection_info(state["collection"])
        memory_info = qdrant_collection_info(state["memoryCollection"])
        default_memory = default_memory_state(state)
        metric_after = qdrant_request_counts()
        snapshot = {
            "schemaVersion": 1,
            "kind": SNAPSHOT_KIND,
            "createdAt": now_iso(),
            "snapshotTime": snapshot_time,
            "isolation": {
                "db": state["db"],
                "collectionPrefix": state["collectionPrefix"],
                "memoryCollectionPrefix": state["memoryCollectionPrefix"],
            },
            "env": {
                "stamp": state["stamp"],
                "db": state["db"],
                "docCollection": state["collection"],
                "memoryCollection": state["memoryCollection"],
                "defaultMemoryCollection": state["defaultMemoryCollection"],
                "agentPort": state["agentPort"],
                "springPort": state["springPort"],
            },
            "userId": user1,
            "username": username1,
            "secondUser": {"userId": user2, "username": username2},
            "password": PASSWORD,
            "datasets": datasets,
            "scenarios": {
                sid: {
                    "id": sid,
                    "name": scenario["name"],
                    "question": scenario["question"],
                    "documentName": scenario["document"]["name"],
                }
                for sid, scenario in scenarios.items()
            },
            "generationProfile": report["generationProfile"],
            "memorySync": sync,
            "defaultMemoryBaseline": default_memory,
            "analysis": {
                "mode": "mq",
                "consumersAtSetup": consumers,
                "note": "不启动 Agent analysis worker；Spring 只声明队列，不消费请求队列。",
            },
            "qdrant": {
                "document": collection_info,
                "memory": memory_info,
                "setupMetricDelta": qdrant_query_delta(metric_before, metric_after),
            },
            "setup": {
                "elapsedMs": int((time.monotonic() - started) * 1000),
                "steps": setup_steps,
                "preflight": report["checks"],
            },
        }
        write_json(snapshot_path, snapshot)
        freeze = write_freeze_baseline(snapshot_path, snapshot)
        qdrant_baseline = write_qdrant_baseline(snapshot_path, state)
        result = {
            "ok": True,
            "state": str(state_path_for(state)),
            "snapshot": str(snapshot_path),
            "snapshotTime": snapshot_time,
            "db": state["db"],
            "collections": {
                "document": state["collection"],
                "memory": state["memoryCollection"],
                "defaultMemory": state["defaultMemoryCollection"],
            },
            "qdrant": snapshot["qdrant"],
            "documents": setup_steps,
            "freezeBaseline": freeze["combined"],
            "qdrantBaseline": {
                "document": qdrant_baseline["document"]["pointCount"],
                "memory": qdrant_baseline["memory"]["pointCount"],
            },
            "defaultMemoryBaseline": default_memory,
            "analysisConsumers": consumers,
            "elapsedMs": int((time.monotonic() - started) * 1000),
        }
        write_json(Path(state["tempDir"]) / "step4-setup-result.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    finally:
        terminate(agent)
        wait_port_closed(state["agentPort"])
        terminate(spring)
        wait_port_closed(state["springPort"])
        save_state(state_path_for(state), state)


def _register_user(state: dict[str, Any], suffix: str) -> tuple[str, int, str]:
    base = f"http://127.0.0.1:{state['springPort']}"
    username = "phase5step4" + suffix + "_" + state["stamp"].replace("-", "_")
    body = {"username": username, "password": PASSWORD}
    try:
        data = api(base, "POST", "/api/auth/register", json=body)
    except RuntimeError:
        data = api(base, "POST", "/api/auth/login", json=body)
    return str(data["token"]), int(data["user"]["id"]), username


def _create_scenario(
    state: dict[str, Any],
    token: str,
    user_id: int,
    scenario: dict[str, Any],
    snapshot_time: str,
) -> dict[str, Any]:
    from full_common import create_scenario

    return create_scenario(state, token, user_id, scenario, snapshot_time)


def _upload_document(
    state: dict[str, Any], token: str, dataset: dict[str, Any], scenario: dict[str, Any]
) -> None:
    from full_common import upload_document

    upload_document(state, token, dataset, scenario)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def cross_isolation_checks(
    state: dict[str, Any], snapshot: dict[str, Any], token1: str
) -> dict[str, Any]:
    """跨 user / space 隔离：HTTP 越权必须被拒绝，Qdrant payload 归属必须正确。"""
    base = f"http://127.0.0.1:{state['springPort']}"
    user1 = int(snapshot["userId"])
    user2 = int(snapshot["secondUser"]["userId"])
    dataset2 = snapshot["datasets"]["S4B2"]
    checks: list[dict[str, Any]] = []

    for name, path in (
        ("spaceDetail", f"/api/spaces/{int(dataset2['spaceId'])}"),
        ("spaceDocuments", f"/api/spaces/{int(dataset2['spaceId'])}/documents"),
        (
            "conversationMessages",
            f"/api/conversations/{int(dataset2['currentConversationId'])}/messages",
        ),
    ):
        response = requests.get(
            base + path,
            headers={"Authorization": f"Bearer {token1}"},
            timeout=30,
        )
        checks.append(
            {
                "name": name,
                "httpStatus": response.status_code,
                "blocked": response.status_code >= 400,
                "bodyPreview": response.text[:200],
            }
        )

    payloads = qdrant_payload_map(state["collection"])
    ownership: list[dict[str, Any]] = []
    for sid, dataset in snapshot["datasets"].items():
        expected_user = int(dataset["userId"])
        expected_space = int(dataset["spaceId"])
        expected_document = int(dataset["documentId"])
        points = [
            payload
            for payload in payloads.values()
            if int(payload.get("document_id") or 0) == expected_document
        ]
        wrong = [
            payload
            for payload in points
            if int(payload.get("user_id") or 0) != expected_user
            or int(payload.get("space_id") or 0) != expected_space
        ]
        ownership.append(
            {
                "scenario": sid,
                "documentId": expected_document,
                "expectedUser": expected_user,
                "expectedSpace": expected_space,
                "pointCount": len(points),
                "wrongOwnerPoints": len(wrong),
            }
        )

    problems: list[str] = []
    for item in checks:
        if not item["blocked"]:
            problems.append(f"{item['name']} 对 user1 未拒绝（HTTP {item['httpStatus']}）")
    for item in ownership:
        if item["pointCount"] == 0:
            problems.append(f"{item['scenario']} 在 Qdrant 无 payload")
        if item["wrongOwnerPoints"]:
            problems.append(
                f"{item['scenario']} 有 {item['wrongOwnerPoints']} 个 payload 归属错误"
            )
    return {
        "ok": not problems,
        "problems": problems,
        "httpChecks": checks,
        "qdrantOwnership": ownership,
        "userIds": {"user1": user1, "user2": user2},
    }


def load_results(state: dict[str, Any]) -> dict[str, Any]:
    path = step4_results_path(state)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "schemaVersion": 1,
        "kind": RESULTS_KIND,
        "createdAt": now_iso(),
        "runs": {},
    }


def save_results(state: dict[str, Any], results: dict[str, Any]) -> None:
    results["updatedAt"] = now_iso()
    write_json(step4_results_path(state), results)


def one_call(
    state: dict[str, Any],
    snapshot: dict[str, Any],
    token: str,
    sid: str,
    scenario: dict[str, Any],
    *,
    baseline: dict[str, Any],
    qdrant_baseline: dict[str, Any],
    default_baseline: dict[str, Any],
    key_suffix: str = "",
    token_user_id: int | None = None,
    keep_answer: bool = True,
) -> dict[str, Any]:
    dataset = snapshot["datasets"][sid]
    conversation_id = int(dataset["currentConversationId"])
    problems: list[str] = []
    warnings: list[str] = []
    started = time.monotonic()

    pre_restore = restore_scenario(state, dataset, snapshot["snapshotTime"])
    if not pre_restore["exactMatch"]:
        problems.append(f"调用前快照恢复失败：{pre_restore}")
    pre_checks = _snapshot_checks(
        state, snapshot, baseline, qdrant_baseline, default_baseline
    )
    problems.extend(_round_checks(pre_checks))

    context_before = agent_context(state)
    metrics_before = qdrant_request_counts()
    call = chat(
        state,
        token,
        conversation_id,
        scenario["question"],
        f"{sid}{key_suffix}",
    )
    time.sleep(0.5)
    context_after = agent_context(state)
    metrics_after = qdrant_request_counts()
    records = context_after.get("records", [])
    new_records = records[int(context_before.get("count", 0)) :]
    context_record = new_records[-1] if new_records else None
    embedding_calls = int(context_after.get("embeddingCalls", 0)) - int(
        context_before.get("embeddingCalls", 0)
    )
    embedding_texts = int(context_after.get("embeddingTexts", 0)) - int(
        context_before.get("embeddingTexts", 0)
    )

    llm_calls: list[dict[str, Any]] = []
    for index, item in enumerate(new_records, start=1):
        item_usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
        llm_calls.append(
            {
                "index": index,
                "systemSha256": item.get("systemSha256"),
                "chars": item.get("chars"),
                "promptTokens": item_usage.get("prompt_tokens"),
                "completionTokens": item_usage.get("completion_tokens"),
                "totalTokens": item_usage.get("total_tokens"),
                "outputChars": item.get("outputChars"),
                "outputSha256": item.get("outputSha256"),
                "completed": item.get("completed"),
                "outputText": item.get("outputText") or "",
            }
        )
    history_guard_triggered = len(new_records) > 1
    if len(new_records) == 0:
        problems.append("未取得任何 LLM 调用记录")

    run_id = None
    if call.get("end"):
        run_id = call["end"].get("runId")
    if run_id is None and call.get("start"):
        run_id = call["start"].get("runId")
    run_row = None
    if run_id is not None:
        run_row = row_query(
            state["db"],
            "SELECT id,status,trace_id,context_manifest,duration_ms,error_message,"
            "assistant_message_id,output_summary FROM agent_run WHERE id=%s",
            (int(run_id),),
        )
    assistant_row = None
    if run_row and run_row.get("assistant_message_id"):
        assistant_row = row_query(
            state["db"],
            "SELECT id,content,tokens,truncated,citations FROM message WHERE id=%s",
            (int(run_row["assistant_message_id"]),),
        )

    manifest = parse_manifest(run_row.get("context_manifest") if run_row else None)
    bucket = manifest_bucket(manifest)
    knowledge_states = knowledge_statuses(bucket)

    citations = (call.get("end") or {}).get("citations")
    if citations is None and assistant_row:
        raw = assistant_row.get("citations")
        if isinstance(raw, str):
            try:
                citations = json.loads(raw)
            except json.JSONDecodeError:
                citations = None
        else:
            citations = raw
    trace = citation_traceability(citations, state["db"], int(dataset["spaceId"]))
    problems.extend(trace["problems"])
    own_document_id = int(dataset["documentId"])
    foreign_citations = [
        {"documentId": item.get("documentId"), "chunkId": item.get("chunkId")}
        for item in (citations if isinstance(citations, list) else [])
        if isinstance(item, dict)
        and item.get("documentId") is not None
        and int(item["documentId"]) != own_document_id
    ]
    if foreign_citations:
        problems.append(f"引用指向非本场景文档：{foreign_citations}")

    payload_check = citation_payload_check(
        citations,
        sender_collection(snapshot),
        user_id=token_user_id or int(dataset["userId"]),
        space_id=int(dataset["spaceId"]),
        document_id=int(dataset["documentId"]),
    )
    problems.extend(payload_check["problems"])

    usage_status = (context_record or {}).get("usageStatus") or "UNKNOWN"
    usage = (context_record or {}).get("usage")
    if usage_status not in {"REAL", "UNKNOWN"}:
        problems.append(f"Usage 状态非 REAL/UNKNOWN：{usage_status}")
    if usage_status == "REAL":
        fields = usage_fields(usage)
        if fields["promptTokens"] is None or fields["totalTokens"] is None:
            problems.append("Usage 标为 REAL 但缺少 prompt/total tokens")
    else:
        fields = usage_fields(None)

    diagnostics = (context_record or {}).get("diagnostics") or {}
    finish_reason = diagnostics.get("finishReason")
    truncated = bool((call.get("end") or {}).get("truncated")) or finish_reason == "length"

    if call.get("error"):
        problems.append(f"SSE error：{call['error']}")
    if call.get("empty"):
        problems.append("回答为空")
    if truncated:
        problems.append(f"回答截断：finishReason={finish_reason}")
    if run_id is None or run_row is None:
        problems.append("run_id 无法关联 agent_run")
    elif str(run_row.get("status")) not in {"COMPLETED", "TRUNCATED"}:
        problems.append(f"agent_run 状态异常：{run_row.get('status')}")
    if embedding_calls > EMBEDDING_MAX_PER_CALL:
        problems.append(
            f"单次 Embedding 调用 {embedding_calls} 次，超过预算 {EMBEDDING_MAX_PER_CALL}"
        )

    guard_trace = guard_trace_from_log(state, run_id)
    if guard_trace and guard_trace.get("regenerated"):
        history_guard_triggered = True

    observation = quote_observation(
        sid,
        call["answer"],
        knowledge_injected="knowledge" in set(bucket.get("selectedTypes") or []),
    )
    memory_observation = memory_degraded_info_from_manifest(manifest)

    post_restore = restore_scenario(state, dataset, snapshot["snapshotTime"])
    if not post_restore["exactMatch"]:
        problems.append(f"调用后快照恢复失败：{post_restore}")
    post_checks = _snapshot_checks(
        state, snapshot, baseline, qdrant_baseline, default_baseline
    )
    problems.extend(_round_checks(post_checks))

    record = {
        "key": f"{sid}{key_suffix}",
        "scenarioId": sid,
        "scenarioName": scenario["name"],
        "conversationId": conversation_id,
        "userId": token_user_id or int(dataset["userId"]),
        "clientRequestId": call["clientRequestId"],
        "runId": run_id,
        "traceId": run_row.get("trace_id") if run_row else None,
        "providerRequestId": diagnostics.get("providerRequestId"),
        "httpStatus": call["httpStatus"],
        "finishReason": finish_reason,
        "truncated": truncated,
        "usage": usage if usage_status == "REAL" else "UNKNOWN",
        "usageStatus": usage_status,
        "usageFields": fields,
        "latencyMs": call["elapsedMs"],
        "agentRunStatus": run_row.get("status") if run_row else None,
        "agentRunDurationMs": run_row.get("duration_ms") if run_row else None,
        "manifest": manifest,
        "manifestSummary": {
            "present": bucket["present"],
            "intent": bucket["intent"],
            "policyVersion": bucket["policyVersion"],
            "sourceTypes": bucket["sourceTypes"],
            "selectedTypes": bucket["selectedTypes"],
            "sourceCount": bucket.get("sourceCount"),
            "selectedCount": bucket.get("selectedCount"),
            "budget": bucket.get("budget"),
            "perTypeTokens": bucket.get("perTypeTokens"),
            "sourceIds": bucket.get("sourceIds"),
            "knowledgeStatuses": knowledge_states,
        },
        "citations": citations if isinstance(citations, list) else [],
        "citationSet": citation_set(citations),
        "citationTrace": trace,
        "citationPayloadCheck": payload_check,
        "foreignCitations": foreign_citations,
        "contextHash": context_record.get("contextSha256") if context_record else None,
        "systemSha256": context_record.get("systemSha256") if context_record else None,
        "systemChars": context_record.get("chars") if context_record else None,
        "contextRecord": context_record,
        "newContextRecordCount": len(new_records),
        "historyGuardTriggered": history_guard_triggered,
        "guardTrace": guard_trace,
        "llmCallCount": len(new_records),
        "llmCalls": [
            {key: value for key, value in item.items() if key != "outputText"}
            for item in llm_calls
        ],
        "firstAnswer": (
            llm_calls[0].get("outputText")
            if history_guard_triggered and llm_calls
            else None
        ),
        "regeneratedAnswer": (
            llm_calls[-1].get("outputText")
            if history_guard_triggered and llm_calls
            else None
        ),
        "embeddingCalls": embedding_calls,
        "embeddingTexts": embedding_texts,
        "qdrantDelta": qdrant_query_delta(metrics_before, metrics_after),
        "answerChars": call["answerChars"],
        "answerPreview": call["answer"][:500],
        **({"answerFull": call["answer"]} if keep_answer else {}),
        "quoteSources": observation["quoteSources"],
        "quoteSourceDetail": observation["quoteSourceDetail"],
        "understandingAsOriginal": observation["understandingAsOriginal"],
        "understandingMislabelDetail": observation["understandingMislabelDetail"],
        "sourceConfusion": observation["sourceConfusion"],
        "memoryDegraded": memory_observation["memoryDegraded"],
        "memoryDegradedSource": memory_observation["memoryDegradedSource"],
        "preRestore": pre_restore,
        "postRestore": post_restore,
        "preChecks": pre_checks,
        "postChecks": post_checks,
        "problems": problems,
        "warnings": warnings,
        "elapsedMs": int((time.monotonic() - started) * 1000),
    }
    record["problems"].extend(expected_checks(record, sid))
    return record


def memory_degraded_info_from_manifest(manifest: dict[str, Any] | None) -> dict[str, Any]:
    sources = manifest_bucket(manifest).get("sources") or []
    degraded_memory: list[str] = []
    cognitive_markers: list[str] = []
    for source in sources:
        source_id = str(source.get("source_id") or "")
        source_type = str(source.get("type") or "")
        status = str(source.get("retrieval_status") or "").upper()
        if status == "DEGRADED" and (
            source_id.startswith("memory:") or source_type.startswith("memory")
        ):
            degraded_memory.append(source_id)
        if "memory-status:DEGRADED" in source_id:
            cognitive_markers.append(source_id)
    details = sorted(set(degraded_memory + cognitive_markers))
    return {"memoryDegraded": bool(details), "memoryDegradedSource": details}


def sender_collection(snapshot: dict[str, Any]) -> str:
    return str(snapshot["env"]["docCollection"])


def start_agent_with_overrides(
    state: dict[str, Any],
    strategy: str,
    secrets_ctx: dict[str, str],
    overrides: dict[str, str],
) -> subprocess.Popen:
    if not port_free(state["agentPort"]):
        raise SystemExit(
            f"端口 {state['agentPort']} 已被占用，无法启动超时实验 Agent"
        )
    env = _agent_env(state, strategy, secrets_ctx)
    env.update(overrides)
    log_path = Path(state["agentLog"])
    handle = log_path.open("a", encoding="utf-8", errors="replace")
    handle.write(f"\n===== agent start strategy={strategy} overrides={sorted(overrides)} {now_iso()} =====\n")
    handle.flush()
    proc = subprocess.Popen(
        [sys.executable, str(FULL_DIR / "run_agent_full.py")],
        cwd=str(AGENT_DIR),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    try:
        from full_common import wait_http

        wait_http(
            f"http://127.0.0.1:{state['agentPort']}/health",
            timeout=90,
            proc=proc,
            description="Agent(overrides)",
        )
        context = agent_context(state)
        if (context or {}).get("contextStrategy") != strategy:
            raise RuntimeError("超时实验 Agent 策略不一致")
        from full_common import agent_diagnostics

        diagnostics = agent_diagnostics(state)
        if int((diagnostics or {}).get("maxTokens") or 0) < 4096:
            raise RuntimeError("超时实验 Agent maxTokens < 4096")
    except Exception:
        terminate(proc)
        raise
    state["agentPid"] = proc.pid
    return proc


def mutate_memory_type(state: dict[str, Any], memory_id: int) -> dict[str, Any]:
    connection = connect_mysql(state["db"])
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT @@SESSION.sql_mode AS m")
            original = str((cursor.fetchone() or {}).get("m") or "")
            cursor.execute("SET SESSION sql_mode=''")
            cursor.execute(
                "UPDATE memory SET memory_type='CORRUPT_ENUM', updated_at=updated_at "
                "WHERE id=%s",
                (memory_id,),
            )
            affected = int(cursor.rowcount or 0)
            cursor.execute("SET SESSION sql_mode=%s", (original,))
        connection.commit()
    finally:
        connection.close()
    return {"affected": affected}


def restore_memory_type(state: dict[str, Any], memory_id: int) -> dict[str, Any]:
    connection = connect_mysql(state["db"])
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE memory SET memory_type='LEARNING', updated_at=updated_at "
                "WHERE id=%s",
                (memory_id,),
            )
            affected = int(cursor.rowcount or 0)
        connection.commit()
    finally:
        connection.close()
    return {"affected": affected}


def hybrid_precheck(
    state: dict[str, Any], token: str, dataset: dict[str, Any], keyword: str
) -> dict[str, Any]:
    url = (
        f"http://127.0.0.1:{state['springPort']}/api/memories/search/hybrid"
        f"?spaceId={int(dataset['spaceId'])}"
        f"&conceptId={int(dataset['memoryConceptId'])}"
        f"&keyword={requests.utils.quote(keyword)}&limit=5"
    )
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    body: Any
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:300]}
    return {
        "httpStatus": response.status_code,
        "status": (body.get("data") or {}).get("status") if isinstance(body, dict) else None,
        "bodyPreview": str(body)[:500],
    }


def run_phase(state_path: str) -> dict[str, Any]:
    state = read_state(state_path)
    snapshot = json.loads(step4_snapshot_path(state).read_text(encoding="utf-8"))
    if snapshot.get("kind") != SNAPSHOT_KIND:
        raise SystemExit("不是 Step 4 快照")
    results = load_results(state)
    secrets_ctx = new_secrets()
    scenarios = build_scenarios()
    snapshot_path = step4_snapshot_path(state)
    baseline = json.loads(baseline_path(snapshot_path).read_text(encoding="utf-8"))
    qdrant_baseline = json.loads(
        qdrant_baseline_path(snapshot_path).read_text(encoding="utf-8")
    )
    default_baseline = snapshot["defaultMemoryBaseline"]

    spring = None
    agent = None
    try:
        spring = start_spring(state, secrets_ctx)
        agent = start_agent(state, "C", secrets_ctx)
        save_state(state_path_for(state), state)
        token1 = login(state, snapshot["username"], PASSWORD)
        token2 = login(state, snapshot["secondUser"]["username"], PASSWORD)

        def run_one(
            sid: str,
            token: str,
            *,
            key_suffix: str = "",
            user_id: int | None = None,
            question: str | None = None,
        ) -> dict[str, Any]:
            scenario = dict(scenarios[sid])
            if question is not None:
                scenario["question"] = question
            record = one_call(
                state,
                snapshot,
                token,
                sid,
                scenario,
                baseline=baseline,
                qdrant_baseline=qdrant_baseline,
                default_baseline=default_baseline,
                key_suffix=key_suffix,
                token_user_id=user_id or int(snapshot["userId"]),
            )
            results["runs"][record["key"]] = record
            save_results(state, results)
            print(
                f"[run] {record['key']} run={record['runId']} "
                f"guard={record['historyGuardTriggered']} "
                f"memoryDegraded={record['memoryDegraded']} "
                f"quotes={record['quoteSources']} problems={len(record['problems'])}",
                flush=True,
            )
            return record

        # A 个性化学习问答
        run_one("S4A", token1)
        # B Knowledge RAG（user1）
        run_one("S4B", token1)
        # B 跨用户 / 跨 space 隔离（user2 自己的 space）
        run_one(
            "S4B2",
            token2,
            key_suffix="-user2",
            user_id=int(snapshot["secondUser"]["userId"]),
        )
        results["isolation"] = cross_isolation_checks(state, snapshot, token1)
        save_results(state, results)
        print(
            f"[run] isolation ok={results['isolation']['ok']} "
            f"problems={results['isolation']['problems']}",
            flush=True,
        )
        # 组合场景
        run_one("S4C", token1)
        # Knowledge 空召回：复用 B 的 space，问一个与文档无关的问题
        run_one(
            "S4B",
            token1,
            key_suffix="-empty",
            question="请根据我上传的资料，说明中世纪庄园经济的税收方式。",
        )
        # 预算裁剪：长文档 + 学习解释
        run_one("S4X", token1)
        # HISTORY_GUARD：最多两次不同措辞，取首次触发
        guard_records: list[dict[str, Any]] = []
        for attempt, question in enumerate(
            (SG_QUESTION_1, SG_QUESTION_2, SG_QUESTION_3), start=1
        ):
            record = run_one(
                "S4G",
                token1,
                key_suffix=f"-attempt{attempt}",
                question=question,
            )
            guard_records.append(record)
            if record["historyGuardTriggered"]:
                break
        results.setdefault("guardAttempts", [])[:] = [
            record["key"] for record in guard_records
        ]
        if not any(record["historyGuardTriggered"] for record in guard_records):
            guard_records[-1]["problems"].append(
                "HISTORY_GUARD 在 3 次构造尝试中均未触发"
            )
            results["guardFailure"] = True
        else:
            results.pop("guardFailure", None)
        save_results(state, results)

        # Memory DEGRADED：在隔离库写非法枚举，确认 Manifest 保留 DEGRADED
        sid = "S4A"
        dataset = snapshot["datasets"][sid]
        scenario = scenarios[sid]
        memory_id = int(dataset["memoryId"])
        started = time.monotonic()
        pre_restore = restore_scenario(state, dataset, snapshot["snapshotTime"])
        pre_checks = _snapshot_checks(
            state, snapshot, baseline, qdrant_baseline, default_baseline
        )
        mutation = mutate_memory_type(state, memory_id)
        precheck = hybrid_precheck(
            state, token1, dataset, scenario["memory"]["content"][:40]
        )
        context_before = agent_context(state)
        metrics_before = qdrant_request_counts()
        call = chat(
            state, token1, int(dataset["currentConversationId"]), scenario["question"],
            "S4A-memory-degraded",
        )
        time.sleep(0.5)
        context_after = agent_context(state)
        metrics_after = qdrant_request_counts()
        restore = restore_memory_type(state, memory_id)
        post_restore = restore_scenario(state, dataset, snapshot["snapshotTime"])
        post_checks = _snapshot_checks(
            state, snapshot, baseline, qdrant_baseline, default_baseline
        )
        records = context_after.get("records", [])
        new_records = records[int(context_before.get("count", 0)) :]
        context_record = new_records[-1] if new_records else None
        run_id = None
        if call.get("end"):
            run_id = call["end"].get("runId")
        if run_id is None and call.get("start"):
            run_id = call["start"].get("runId")
        run_row = (
            row_query(
                state["db"],
                "SELECT id,status,context_manifest,assistant_message_id,error_message "
                "FROM agent_run WHERE id=%s",
                (int(run_id),),
            )
            if run_id is not None
            else None
        )
        manifest = parse_manifest(run_row.get("context_manifest") if run_row else None)
        bucket = manifest_bucket(manifest)
        observation = quote_observation(
            sid,
            call["answer"],
            knowledge_injected="knowledge" in set(bucket.get("selectedTypes") or []),
        )
        memory_obs = memory_degraded_info_from_manifest(manifest)
        citations = (call.get("end") or {}).get("citations")
        if citations is None and run_row and run_row.get("assistant_message_id"):
            assistant_row = row_query(
                state["db"],
                "SELECT citations FROM message WHERE id=%s",
                (int(run_row["assistant_message_id"]),),
            )
            raw = (assistant_row or {}).get("citations")
            if isinstance(raw, str):
                try:
                    citations = json.loads(raw)
                except json.JSONDecodeError:
                    citations = None
            else:
                citations = raw
        trace = citation_traceability(citations, state["db"], int(dataset["spaceId"]))
        payload_check = citation_payload_check(
            citations,
            sender_collection(snapshot),
            user_id=int(snapshot["userId"]),
            space_id=int(dataset["spaceId"]),
            document_id=int(dataset["documentId"]),
        )
        problems: list[str] = []
        problems.extend(_round_checks(pre_checks))
        problems.extend(_round_checks(post_checks))
        if not pre_restore["exactMatch"]:
            problems.append("故障注入前快照恢复失败")
        if not post_restore["exactMatch"]:
            problems.append("故障恢复后快照恢复失败")
        if mutation["affected"] != 1:
            problems.append(f"非法枚举写入影响行数异常：{mutation}")
        if restore["affected"] != 1:
            problems.append(f"非法枚举恢复影响行数异常：{restore}")
        if not memory_obs["memoryDegraded"]:
            problems.append("Manifest 未保留 Memory DEGRADED")
        if call.get("error") or call.get("empty"):
            problems.append("Memory DEGRADED 场景出现 error / 空输出")
        if run_row is None or str(run_row.get("status")) != "COMPLETED":
            problems.append(f"Memory DEGRADED 场景 agent_run 状态异常：{run_row}")
        if call.get("error"):
            problems.append(f"Memory DEGRADED 场景 SSE error：{call['error']}")
        problems.extend(trace["problems"])
        problems.extend(payload_check["problems"])
        if citations and not trace.get("traceable"):
            problems.append("Memory DEGRADED 场景 Citation 不可溯源")
        usage_status = (context_record or {}).get("usageStatus") or "UNKNOWN"
        if usage_status != "REAL":
            problems.append(f"Memory DEGRADED 场景 Usage 非 REAL：{usage_status}")
        record = {
            "key": "S4A-memory-degraded",
            "scenarioId": sid,
            "scenarioName": "Memory DEGRADED",
            "conversationId": int(dataset["currentConversationId"]),
            "userId": int(snapshot["userId"]),
            "clientRequestId": call["clientRequestId"],
            "runId": run_id,
            "httpStatus": call["httpStatus"],
            "usageStatus": usage_status,
            "usage": (context_record or {}).get("usage") if usage_status == "REAL" else "UNKNOWN",
            "usageFields": usage_fields(
                (context_record or {}).get("usage") if usage_status == "REAL" else None
            ),
            "agentRunStatus": run_row.get("status") if run_row else None,
            "manifest": manifest,
            "manifestSummary": {
                "present": bucket["present"],
                "intent": bucket["intent"],
                "policyVersion": bucket["policyVersion"],
                "sourceTypes": bucket["sourceTypes"],
                "selectedTypes": bucket["selectedTypes"],
                "sourceIds": bucket.get("sourceIds"),
                "knowledgeStatuses": knowledge_statuses(bucket),
            },
            "citations": citations if isinstance(citations, list) else [],
            "citationTrace": trace,
            "citationPayloadCheck": payload_check,
            "historyGuardTriggered": len(new_records) > 1,
            "llmCallCount": len(new_records),
            "memoryDegraded": memory_obs["memoryDegraded"],
            "memoryDegradedSource": memory_obs["memoryDegradedSource"],
            "quoteSources": observation["quoteSources"],
            "understandingAsOriginal": observation["understandingAsOriginal"],
            "fixtureMutation": {
                "column": "memory_type",
                "value": "CORRUPT_ENUM",
                "status": "ACTIVE",
                "mutation": mutation,
                "restore": restore,
                "precheck": precheck,
                "note": "MemoryMapper 的 SQL 始终过滤 status='ACTIVE'，"
                "写非法 status 的行不会被选中，无法触发 DEGRADED；"
                "因此改在同一 ACTIVE 行的 memory_type 枚举列写非法值，"
                "让 MyBatis 映射失败，词法路降级。",
            },
            "embeddingCalls": int(context_after.get("embeddingCalls", 0))
            - int(context_before.get("embeddingCalls", 0)),
            "qdrantDelta": qdrant_query_delta(metrics_before, metrics_after),
            "answerChars": call["answerChars"],
            "answerPreview": call["answer"][:500],
            "answerFull": call["answer"],
            "preChecks": pre_checks,
            "postChecks": post_checks,
            "problems": problems,
            "warnings": [],
            "elapsedMs": int((time.monotonic() - started) * 1000),
        }
        results["runs"][record["key"]] = record
        save_results(state, results)
        print(
            f"[run] {record['key']} run={run_id} memoryDegraded={record['memoryDegraded']} "
            f"problems={len(problems)}",
            flush=True,
        )

        # Embedding 超时：停掉正常 Agent，用隔离配置启动只影响本轮的超时 Agent
        terminate(agent)
        wait_port_closed(state["agentPort"])
        agent = start_agent_with_overrides(
            state,
            "C",
            secrets_ctx,
            {
                "AILS_EMBEDDING_TIMEOUT_SECONDS": "0.001",
                "AILS_EMBEDDING_MAX_RETRIES": "0",
            },
        )
        timeout_record = run_one(
            "S4B",
            token1,
            key_suffix="-embedding-timeout",
        )
        timeout_record["embeddingTimeoutConfig"] = {
            "AILS_EMBEDDING_TIMEOUT_SECONDS": "0.001",
            "AILS_EMBEDDING_MAX_RETRIES": "0",
        }
        save_results(state, results)

        results["summary"] = summarize(results, snapshot)
        save_results(state, results)
        print(json.dumps(results["summary"], ensure_ascii=False, indent=2))
        return results["summary"]
    finally:
        terminate(agent)
        wait_port_closed(state["agentPort"])
        terminate(spring)
        wait_port_closed(state["springPort"])
        save_state(state_path_for(state), state)


def guard_phase(state_path: str, question: str) -> dict[str, Any]:
    """单独补跑一次 HISTORY_GUARD 构造，不重跑其他场景。"""
    state = read_state(state_path)
    snapshot_path = step4_snapshot_path(state)
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    results = load_results(state)
    baseline = json.loads(baseline_path(snapshot_path).read_text(encoding="utf-8"))
    qdrant_baseline = json.loads(
        qdrant_baseline_path(snapshot_path).read_text(encoding="utf-8")
    )
    default_baseline = snapshot["defaultMemoryBaseline"]
    secrets_ctx = new_secrets()
    scenarios = build_scenarios()
    spring = None
    agent = None
    try:
        spring = start_spring(state, secrets_ctx)
        agent = start_agent(state, "C", secrets_ctx)
        token1 = login(state, snapshot["username"], PASSWORD)
        scenario = dict(scenarios["S4G"])
        scenario["question"] = question
        attempt_no = len(results.get("guardAttempts") or []) + 2
        record = one_call(
            state,
            snapshot,
            token1,
            "S4G",
            scenario,
            baseline=baseline,
            qdrant_baseline=qdrant_baseline,
            default_baseline=default_baseline,
            key_suffix=f"-attempt{attempt_no}",
            token_user_id=int(snapshot["userId"]),
        )
        results["runs"][record["key"]] = record
        results.setdefault("guardAttempts", []).append(record["key"])
        if record["historyGuardTriggered"]:
            results.pop("guardFailure", None)
        results["summary"] = summarize(results, snapshot)
        save_results(state, results)
        print(
            f"[guard] {record['key']} run={record['runId']} "
            f"guard={record['historyGuardTriggered']} calls={record['llmCallCount']} "
            f"trace={record.get('guardTrace')} problems={record['problems']}",
            flush=True,
        )
        return record
    finally:
        terminate(agent)
        wait_port_closed(state["agentPort"])
        terminate(spring)
        wait_port_closed(state["springPort"])
        save_state(state_path_for(state), state)


def observe_phase(state_path: str) -> dict[str, Any]:
    """只重算四个观测字段，不重新调用模型。"""
    state = read_state(state_path)
    snapshot = json.loads(step4_snapshot_path(state).read_text(encoding="utf-8"))
    results = load_results(state)
    for record in (results.get("runs") or {}).values():
        sid = str(record.get("scenarioId") or "")
        bucket = manifest_bucket(record.get("manifest"))
        answer = record.get("answerFull") or record.get("answerPreview")
        observation = quote_observation(
            sid,
            answer,
            knowledge_injected="knowledge" in set(bucket.get("selectedTypes") or []),
        )
        record["quoteSources"] = observation["quoteSources"]
        record["quoteSourceDetail"] = observation["quoteSourceDetail"]
        record["understandingAsOriginal"] = observation["understandingAsOriginal"]
        record["understandingMislabelDetail"] = observation[
            "understandingMislabelDetail"
        ]
        record["sourceConfusion"] = observation["sourceConfusion"]
        if not record.get("llmCallCount"):
            record["llmCallCount"] = (
                len(record.get("llmCalls") or [])
                or (1 if record.get("runId") is not None else 0)
            )
    results["summary"] = summarize(results, snapshot)
    save_results(state, results)
    print(json.dumps(results["summary"], ensure_ascii=False, indent=2))
    return results["summary"]


def summarize(results: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    runs = results.get("runs") or {}
    llm_calls = sum(int(item.get("llmCallCount") or 0) for item in runs.values())
    embedding_calls = sum(int(item.get("embeddingCalls") or 0) for item in runs.values())
    guard_runs = [key for key, item in runs.items() if item.get("historyGuardTriggered")]
    memory_degraded = [
        key for key, item in runs.items() if item.get("memoryDegraded")
    ]
    problems = {
        key: item.get("problems") or []
        for key, item in runs.items()
        if item.get("problems")
    }
    isolation = results.get("isolation") or {}
    return {
        "runCount": len(runs),
        "llmCalls": llm_calls,
        "embeddingCalls": embedding_calls,
        "historyGuardRuns": guard_runs,
        "memoryDegradedRuns": memory_degraded,
        "problemRuns": problems,
        "isolationOk": bool(isolation.get("ok")),
        "isolationProblems": isolation.get("problems") or [],
        "guardFailure": bool(results.get("guardFailure")),
        "snapshot": {
            "db": snapshot["isolation"]["db"],
            "docCollection": snapshot["env"]["docCollection"],
            "memoryCollection": snapshot["env"]["memoryCollection"],
        },
    }


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


def independent_residual_check(state: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        conn = connect_mysql(None)
        try:
            with conn.cursor() as cursor:
                cursor.execute("SHOW DATABASES LIKE %s", (state["db"],))
                result["databaseRemaining"] = len(cursor.fetchall())
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        result["databaseError"] = type(exc).__name__
    try:
        listing = requests.get("http://127.0.0.1:6333/collections", timeout=10).json()
        names = [
            str(item.get("name"))
            for item in listing.get("result", {}).get("collections", [])
        ]
        result["qdrantPhase5Collections"] = sorted(
            name for name in names if name.startswith("phase5-pilot-")
        )
    except Exception as exc:  # noqa: BLE001
        result["qdrantError"] = type(exc).__name__
    try:
        response = requests.get(
            "http://127.0.0.1:15672/api/queues", auth=("guest", "guest"), timeout=10
        )
        queues = [
            str(item.get("name"))
            for item in response.json()
            if str(item.get("name")).startswith(f"phase5.pilot.{state['stamp']}")
        ]
        exchanges = requests.get(
            "http://127.0.0.1:15672/api/exchanges",
            auth=("guest", "guest"),
            timeout=10,
        ).json()
        exchange_names = [
            str(item.get("name"))
            for item in exchanges
            if str(item.get("name")).startswith(f"phase5.pilot.{state['stamp']}")
        ]
        result["rabbitQueues"] = sorted(queues)
        result["rabbitExchanges"] = sorted(exchange_names)
    except Exception as exc:  # noqa: BLE001
        result["rabbitError"] = type(exc).__name__
    dead = []
    for port in (state["agentPort"], state["springPort"]):
        try:
            requests.get(f"http://127.0.0.1:{port}/health", timeout=1)
            dead.append(port)
        except requests.RequestException:
            continue
    result["portsStillOpen"] = dead
    result["ok"] = (
        result.get("databaseRemaining") == 0
        and not result.get("qdrantPhase5Collections")
        and not result.get("rabbitQueues")
        and not result.get("rabbitExchanges")
        and not dead
        and not any("Error" in key for key in result)
    )
    return result


def cleanup_phase(state_path: str) -> dict[str, Any]:
    state = read_state(state_path)
    script = FULL_DIR.parent / "pilot" / "cleanup.py"
    completed = subprocess.run(
        [sys.executable, str(script), state_path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    cleanup_result = None
    path = Path(state["tempDir"]) / "cleanup-result.json"
    if path.exists():
        cleanup_result = json.loads(path.read_text(encoding="utf-8"))
    residual = independent_residual_check(state)
    summary = {
        "exitCode": completed.returncode,
        "cleanup": cleanup_result,
        "residual": residual,
        "stdoutPreview": (completed.stdout or "")[-1500:],
        "stderrPreview": (completed.stderr or "")[-800:],
    }
    write_json(Path(state["tempDir"]) / "step4-cleanup-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--phase", choices=["setup", "run", "guard", "observe", "cleanup"], required=True
    )
    parser.add_argument("--state", default="")
    parser.add_argument("--question", default=SG_QUESTION_4)
    args = parser.parse_args()
    if args.phase == "setup":
        setup_phase()
        return
    if not args.state:
        raise SystemExit("--phase run/guard/observe/cleanup 需要 --state <state.json>")
    if args.phase == "run":
        run_phase(args.state)
    elif args.phase == "guard":
        guard_phase(args.state, args.question)
    elif args.phase == "observe":
        observe_phase(args.state)
    else:
        cleanup_phase(args.state)


if __name__ == "__main__":
    main()
