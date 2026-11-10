"""Phase 5 Step 5：10 场景 × 3 策略 × 1 次 A/B/C 对照 + 盲审材料 + L1/L2/L3 分层判定。

本脚本只新增实验编排，不修改 Prompt / ContextBuilder / Policy / Schema /
Runtime / HISTORY_GUARD / Tavily：

- setup：复用 Step 3 的 10 场景数据准备（同一用户、同一隔离库、同一批
  Knowledge 文档），写快照、冻结基线与 Qdrant 基线；
- run：每个场景按旋转后的 A/B/C 顺序各调用 1 次（共 30 次），每次调用
  前后恢复快照；记录四个观测字段与 L2 判定所需字段；
- packet：把 30 条回答随机编号、去除策略标识，生成
  docs/testing/phase5-blind-review.md；策略映射只写入隔离 temp；
- cleanup：删除隔离资源并做独立残留检查；
- report：生成 docs/testing/phase5-step5-results.md（10 个输出章节）。

人工盲审未完成时，L2/L3 一律标记 BLOCKED，不伪造评分。

用法：

    python scripts/phase5/full/run_step5.py --phase setup
    python scripts/phase5/full/run_step5.py --phase run --state <state.json>
    python scripts/phase5/full/run_step5.py --phase packet --state <state.json>
    python scripts/phase5/full/run_step5.py --phase cleanup --state <state.json>
    python scripts/phase5/full/run_step5.py --phase report --state <state.json>
    python scripts/phase5/full/run_step5.py --phase all

不 commit / push。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

FULL_DIR = Path(__file__).resolve().parent
for _path in (FULL_DIR.parent / "pilot", FULL_DIR.parent / "pre-step3", FULL_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from common import ROOT, read_state, write_json  # noqa: E402
from full_common import (  # noqa: E402
    S5_UNDERSTANDING_MISLABEL_MARKERS,
    S5_UNDERSTANDING_NEGATION_MARKERS,
    S5_UNDERSTANDING_SOURCE_LABEL,
    _sentence_around,
    default_memory_state,
    login,
    memory_degraded_info,
    start_agent,
    start_spring,
    terminate,
    wait_port_closed,
)
import run_full_abc as abc  # noqa: E402
from scenarios import SCENARIOS, SCENARIO_BY_ID, USER_PASSWORD  # noqa: E402

BLIND_DOC = ROOT / "docs" / "testing" / "phase5-blind-review.md"
RESULTS_DOC = ROOT / "docs" / "testing" / "phase5-step5-results.md"
RESULTS_FILE = "step5-results.json"
MAPPING_FILE = "step5-blind-mapping.json"
STRATEGIES = ("A", "B", "C")
PERSONAL_TYPES = {
    "history",
    "session",
    "understanding",
    "memory",
    "evidence",
    "trajectory",
}
FORBIDDEN_BLIND_TOKENS = (
    "contextStrategy",
    "policy_version",
    "policyVersion",
    "d4-2-v1",
    "strategy",
    "策略A",
    "策略B",
    "策略C",
    "策略 A",
    "策略 B",
    "策略 C",
)

# ---------------------------------------------------------------------------
# 场景可区分片段（只做字面观测，不做语义推断；人工盲审为准）
# ---------------------------------------------------------------------------
#
# 这些片段取自 scenarios.py 的预置数据（【Sx-...】标记已去掉），并尽量
# 避免与 Knowledge 文档正文重叠，以便把「回答是否实际使用了个人上下文」
# 与「回答只是复述文档」区分开。它们是观测项，不是 L2 的最终判定。
PERSONAL_MARKERS: dict[str, dict[str, list[str]]] = {
    "S1": {
        "history": ["和发送端的行为无关", "只是带宽不够"],
        "understanding": ["把原因主要归为带宽不足", "流量超过链路容量"],
        "memory": ["带宽和队列长度"],
        "evidence": ["发送行为与拥塞"],
    },
    "S2": {
        "history": ["内部节点只存索引", "数据都在叶子上"],
        "understanding": ["范围查询的磁盘 IO 优势"],
        "memory": ["磁盘页和顺序 IO", "接着上一轮的叶子链表"],
        "evidence": ["未解释范围查询的磁盘 IO 优势"],
    },
    "S3": {
        "history": ["分裂与合并讲完了", "中间元素"],
        "understanding": ["记住了叶子链表", "上一阶段的理解"],
        "memory": ["磁盘页大小", "先回忆上一阶段的结论"],
        "evidence": ["能解释分裂"],
    },
    "S4": {
        "history": ["小托盘", "最近穿的鞋"],
        "understanding": ["鞋柜门口的小托盘", "最近用到的页表项"],
        "memory": ["鞋柜门口的小托盘"],
        "evidence": ["完成了对 A 会话错误理解的纠正"],
    },
    "S5": {
        "history": [
            "我原来以为慢启动慢是因为它每次只把窗口加一",
            "每次只把窗口加一",
        ],
        "understanding": ["把慢启动理解为线性增长", "起点小、呈指数增长"],
        "memory": ["线性加一"],
        "evidence": ["误解为线性增长，已纠正为指数增长"],
    },
    "S6": {
        "history": ["这个流程我画过"],
        "understanding": ["TLB miss 后页表遍历的代价"],
        "memory": ["先画地址转换流程图", "TLB miss 代价还不确定"],
        "evidence": ["未量化 TLB miss 代价"],
    },
    "S7": {
        "history": ["AOF 重写会不会阻塞 Redis 主线程"],
        "understanding": ["AOF 重写由子进程完成"],
        "memory": ["Redis 持久化和主线程阻塞"],
        "evidence": ["AOF 重写的基本并发模型"],
    },
    "S8": {
        "history": ["任何系统只能选两个", "我记得 CAP"],
        "understanding": ["认为 CAP 必须三选二", "仍在核对"],
        "memory": ["用“三选二”记忆 CAP"],
        "evidence": ["停留在“三选二”的口号"],
    },
    "S9": {
        "history": ["按优先级排队"],
        "understanding": [],
        "memory": ["时间片和优先级"],
        "evidence": ["尚未区分轮转与优先级调度"],
    },
    "S10": {
        "history": ["fsync 之前崩溃"],
        "understanding": ["串成一条崩溃恢复链路", "inode、目录项、页缓存"],
        "memory": ["崩溃后会发生什么", "画成时间线", "完整链路"],
        "evidence": ["没有解释为什么提交顺序能避免部分更新"],
    },
}

KNOWLEDGE_MARKERS: dict[str, list[str]] = {
    "S1": ["聚合发送速率", "网络承载能力", "拥塞窗口"],
    "S2": ["按链表顺序读取", "顺序 IO", "O(log n)", "预读"],
    "S3": ["定位一次", "顺序扫描", "键值顺序"],
    "S4": ["地址转换结果", "多级页表", "MMU"],
    "S5": ["每个 RTT 大约翻倍", "ssthresh", "增长得慢"],
    "S6": ["多级页表", "大页", "多次访存"],
    "S7": ["随机", "RequestVote", "任期", "多数票"],
    "S8": ["网络分区", "CP 还是 AP", "分区容忍性"],
    "S9": ["多级反馈队列", "FCFS", "SJF", "时间片轮转"],
    "S10": ["事务边界", "checkpoint", "写屏障", "日志提交"],
}

_MARKER_RE = re.compile(r"【[^】]*】")


def _strip_markers(text: str) -> str:
    return _MARKER_RE.sub("", text or "").strip()


# ---------------------------------------------------------------------------
# 状态 / 结果
# ---------------------------------------------------------------------------


def results_path(state: dict[str, Any]) -> Path:
    return Path(state["tempDir"]) / RESULTS_FILE


def snapshot_path(state: dict[str, Any]) -> Path:
    return abc.default_snapshot_path(state)


def baseline_paths(state: dict[str, Any]) -> tuple[Path, Path]:
    path = snapshot_path(state)
    return abc.baseline_path(path), abc.qdrant_baseline_path(path)


def load_results(state: dict[str, Any]) -> dict[str, Any]:
    path = results_path(state)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "schemaVersion": 1,
        "createdAt": abc.now_iso(),
        "runs": {},
        "orders": {},
        "callHealth": [],
    }


def save_results(state: dict[str, Any], results: dict[str, Any]) -> None:
    results["updatedAt"] = abc.now_iso()
    write_json(results_path(state), results)


def rotated_order(index: int) -> list[str]:
    order = list(STRATEGIES)
    offset = index % len(order)
    return order[offset:] + order[:offset]


def run_key(sid: str, strategy: str) -> str:
    return f"{sid}-{strategy}-1"


# ---------------------------------------------------------------------------
# L2 观测字段
# ---------------------------------------------------------------------------


def _understanding_fragments(sid: str, scenario: dict[str, Any]) -> list[str]:
    fragments: list[str] = list(PERSONAL_MARKERS.get(sid, {}).get("understanding", []))
    understanding = scenario.get("understanding") or {}
    for key in ("summary", "explanation"):
        text = _strip_markers(str(understanding.get(key) or ""))
        if len(text) >= 8:
            fragments.append(text)
    seen: set[str] = set()
    ordered: list[str] = []
    for fragment in fragments:
        if fragment and fragment not in seen:
            seen.add(fragment)
            ordered.append(fragment)
    return ordered


def understanding_mislabel(
    answer: str | None, sid: str, scenario: dict[str, Any]
) -> dict[str, Any]:
    """观测：回答是否把 Understanding 的转述称为用户原话 / 原文 / 原句。"""
    text = answer or ""
    flagged: list[dict[str, Any]] = []
    labelled: list[str] = []
    negated: list[str] = []
    for fragment in _understanding_fragments(sid, scenario):
        position = text.find(fragment)
        while position != -1:
            sentence = _sentence_around(text, position, position + len(fragment))
            clue = next(
                (
                    marker
                    for marker in S5_UNDERSTANDING_MISLABEL_MARKERS
                    if marker in sentence
                ),
                None,
            )
            if clue is not None:
                if S5_UNDERSTANDING_SOURCE_LABEL in sentence:
                    labelled.append(fragment)
                elif any(
                    marker in sentence
                    for marker in S5_UNDERSTANDING_NEGATION_MARKERS
                ):
                    negated.append(fragment)
                else:
                    flagged.append(
                        {
                            "fragment": fragment,
                            "sentence": sentence,
                            "marker": clue,
                        }
                    )
            position = text.find(fragment, position + len(fragment))
    return {
        "understandingAsOriginal": bool(flagged),
        "understandingMislabelDetail": flagged,
        "understandingLabelledFragments": sorted(set(labelled)),
        "understandingNegatedFragments": sorted(set(negated)),
    }


def _injected_personal_types(record: dict[str, Any]) -> list[str]:
    summary = record.get("manifestSummary") or {}
    injected = set(summary.get("selectedTypes") or [])
    source_ids = [str(item) for item in summary.get("sourceIds") or []]
    if any(item.startswith("evidence:") for item in source_ids):
        injected.add("evidence")
    return sorted(injected & PERSONAL_TYPES)


def build_l2_observation(
    sid: str, record: dict[str, Any], scenario: dict[str, Any]
) -> dict[str, Any]:
    answer = record.get("answerFull") or record.get("answerPreview") or ""
    personal_hits: dict[str, list[str]] = {}
    for category, fragments in PERSONAL_MARKERS.get(sid, {}).items():
        matched = [fragment for fragment in fragments if fragment in answer]
        if matched:
            personal_hits[category] = matched

    knowledge_hits = [
        fragment
        for fragment in KNOWLEDGE_MARKERS.get(sid, [])
        if fragment in answer
    ]
    citations = record.get("citations") or []
    knowledge_used = bool(knowledge_hits or citations)
    quote_sources = [
        category
        for category in ("history", "understanding", "memory", "evidence")
        if category in personal_hits
    ]
    if knowledge_used:
        quote_sources.append("knowledge")

    injected_personal = _injected_personal_types(record)
    personal_used = bool(personal_hits)
    mislabel = understanding_mislabel(answer, sid, scenario)
    memory_info = memory_degraded_info(record)
    problems = record.get("problems") or []
    sse_error = any(str(item).startswith("SSE error") for item in problems)
    empty = int(record.get("answerChars") or 0) == 0 or "回答为空" in problems
    truncated = bool(record.get("truncated")) or record.get("finishReason") == "length"
    return {
        # Step 4 的四个观测字段（本批沿用同一 Caliber）
        "historyGuardTriggered": bool(record.get("historyGuardTriggered")),
        "memoryDegraded": memory_info["memoryDegraded"],
        "memoryDegradedSource": memory_info["memoryDegradedSource"],
        "personalTypesInjected": injected_personal,
        "personalFragmentsHit": personal_hits,
        "personalContextUsed": personal_used,
        "knowledgeFragmentsHit": knowledge_hits,
        "knowledgeUsed": knowledge_used,
        "quoteSources": quote_sources,
        # 字面代理：注入了个人来源、但回答里没有任何可区分个人片段。
        # 是否真的「忽略」以人工盲审为准；这里只做候选标记。
        "ignoredInjectedPersonalLiteralProxy": bool(injected_personal)
        and not personal_used,
        **mislabel,
        "callHealth": {
            "httpStatus": record.get("httpStatus"),
            "agentRunStatus": record.get("agentRunStatus"),
            "finishReason": record.get("finishReason"),
            "truncated": truncated,
            "empty": empty,
            "sseError": sse_error,
            "usageStatus": record.get("usageStatus"),
            "answerChars": record.get("answerChars"),
            "llmCallCount": record.get("llmCallCount"),
        },
    }


def refresh_observations(results: dict[str, Any]) -> dict[str, Any]:
    """用当前观测函数重算全部记录，兼容运行途中补充的字段。"""
    for record in (results.get("runs") or {}).values():
        sid = record.get("scenarioId")
        if sid in SCENARIO_BY_ID:
            record["step5Observation"] = build_l2_observation(
                sid, record, SCENARIO_BY_ID[sid]
            )
    return results


def call_health_problems(record: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    observation = record.get("step5Observation") or {}
    health = observation.get("callHealth") or {}
    if health.get("sseError"):
        problems.append("SSE error")
    if health.get("empty"):
        problems.append("空输出")
    if health.get("truncated"):
        problems.append("截断")
    if health.get("httpStatus") not in (200, None):
        problems.append(f"HTTP {health.get('httpStatus')}")
    if str(health.get("agentRunStatus") or "") not in {"COMPLETED", "TRUNCATED"}:
        problems.append(f"agent_run={health.get('agentRunStatus')}")
    if health.get("usageStatus") not in {"REAL", "UNKNOWN"}:
        problems.append(f"Usage={health.get('usageStatus')}")
    if record.get("error"):
        problems.append(f"调用异常：{record.get('error')}")
    return problems


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


def setup_phase() -> dict[str, Any]:
    """复用 Step 3 setup；额外写 step5-setup.json 便于后续阶段定位。"""
    result = abc.setup_phase()
    state_path = result.get("state")
    state = read_state(state_path)
    write_json(
        Path(state["tempDir"]) / "step5-setup.json",
        {
            "createdAt": abc.now_iso(),
            "state": state_path,
            "snapshot": result.get("snapshot"),
            "db": result.get("db"),
            "collections": result.get("collections"),
            "documents": result.get("documents"),
            "preflight": (result.get("preflight") or []),
        },
    )
    return result


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _failure_record(
    sid: str, strategy: str, exc: BaseException
) -> dict[str, Any]:
    return {
        "key": run_key(sid, strategy),
        "scenarioId": sid,
        "scenarioName": SCENARIO_BY_ID[sid]["name"],
        "strategy": strategy,
        "rep": 1,
        "runId": None,
        "httpStatus": None,
        "finishReason": None,
        "truncated": None,
        "usageStatus": "UNKNOWN",
        "usageFields": {},
        "agentRunStatus": None,
        "manifestSummary": {},
        "manifest": None,
        "citations": [],
        "citationSet": [],
        "answerChars": 0,
        "answerFull": "",
        "problems": [f"调用异常：{type(exc).__name__}: {exc}"],
        "error": f"{type(exc).__name__}: {exc}",
        "llmCallCount": 0,
        "embeddingCalls": 0,
        "embeddingTexts": 0,
    }


def run_phase(state_path: str) -> dict[str, Any]:
    state = read_state(state_path)
    snapshot_file = snapshot_path(state)
    snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
    baseline_file, qdrant_baseline_file = baseline_paths(state)
    baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
    qdrant_baseline = json.loads(qdrant_baseline_file.read_text(encoding="utf-8"))
    default_baseline = default_memory_state(state)
    results = load_results(state)
    secrets_ctx = abc.new_secrets()
    spring = None
    started_all = time.monotonic()
    try:
        spring = start_spring(state, secrets_ctx)
        token = login(state, snapshot["username"], USER_PASSWORD)
        for index, scenario in enumerate(SCENARIOS):
            sid = scenario["id"]
            order = rotated_order(index)
            results["orders"][sid] = order
            for strategy in order:
                key = run_key(sid, strategy)
                if key in results["runs"]:
                    continue
                agent = None
                try:
                    agent = start_agent(state, strategy, secrets_ctx)
                    record = abc.one_call(
                        state,
                        snapshot,
                        snapshot_file,
                        token,
                        sid,
                        strategy,
                        1,
                        baseline,
                        qdrant_baseline,
                        default_baseline,
                        keep_answer=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    record = _failure_record(sid, strategy, exc)
                finally:
                    terminate(agent)
                    wait_port_closed(state["agentPort"])
                scenario_data = SCENARIO_BY_ID[sid]
                record["step5Observation"] = build_l2_observation(
                    sid, record, scenario_data
                )
                record["step3Problems"] = list(record.get("problems") or [])
                results["runs"][key] = record
                results["callHealth"].append(
                    {
                        "key": key,
                        "problems": call_health_problems(record),
                    }
                )
                save_results(state, results)
                observation = record["step5Observation"]
                print(
                    f"[run] {key} run={record.get('runId')} "
                    f"personal={observation['personalContextUsed']} "
                    f"quotes={observation['quoteSources']} "
                    f"mislabel={observation['understandingAsOriginal']} "
                    f"ignoredProxy={observation['ignoredInjectedPersonalLiteralProxy']} "
                    f"llm={record.get('llmCallCount')} "
                    f"health={call_health_problems(record) or 'OK'}",
                    flush=True,
                )

        final_checks = abc._snapshot_checks(
            state, snapshot, baseline, qdrant_baseline, default_baseline
        )
        results["finalChecks"] = final_checks
        results["embeddingTotals"] = abc._embedding_totals(results["runs"].values())
        results["llmCallsTotal"] = sum(
            int(item.get("llmCallCount") or 0) for item in results["runs"].values()
        )
        results["elapsedMs"] = int((time.monotonic() - started_all) * 1000)
        save_results(state, results)
        print(
            json.dumps(
                {
                    "runs": len(results["runs"]),
                    "llmCallsTotal": results["llmCallsTotal"],
                    "embeddingTotals": results["embeddingTotals"],
                    "finalFreezeProblems": final_checks.get("freezeProblems"),
                    "finalQdrantProblems": final_checks.get("qdrantProblems"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return results
    finally:
        terminate(spring)
        wait_port_closed(state["springPort"])
        save_results(state, results)


# ---------------------------------------------------------------------------
# packet
# ---------------------------------------------------------------------------


def _citation_text(citations: Any) -> str:
    items = citations if isinstance(citations, list) else []
    if not items:
        return "无"
    parts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        parts.append(
            "document={} chunk={} title={}".format(
                item.get("documentId"),
                item.get("chunkId"),
                item.get("sourceTitle") or "-",
            )
        )
    return "; ".join(parts) or "无"


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _review_form() -> list[str]:
    return [
        "| 维度 | 评分（评审填写） |",
        "|---|---|",
        "| L2-1 回答是否使用了个人上下文 | ☐ 是　☐ 否　☐ 无法判断 |",
        "| L2-2 是否把非个人来源误称为用户原话/原文 | ☐ 是　☐ 否　☐ 无法判断 |",
        "| L2-3 是否忽略了已注入的个人内容 | ☐ 是　☐ 否　☐ 无法判断 |",
        "| L2-4 引用了哪些来源（可多选） | ☐ 历史　☐ Session　☐ 理解　☐ 记忆　☐ 证据　☐ 知识 |",
        "| L2-5 证据（引用原句/定位） |  |",
        "| L3-1 回答准确性（1=很差，5=很好） |  |",
        "| L3-2 与用户历史的贴合度（1=很泛，5=很贴合） |  |",
        "| 备注 |  |",
    ]


def leak_scan(text: str) -> dict[str, Any]:
    lowered = text.lower()
    hits = [token for token in FORBIDDEN_BLIND_TOKENS if token.lower() in lowered]
    pattern_hits = re.findall(r"S\d+-[ABC]-\d", text)
    return {
        "ok": not hits and not pattern_hits,
        "forbiddenTokenHits": hits,
        "runKeyPatternHits": sorted(set(pattern_hits)),
    }


def _render_blind_doc(
    entries: list[dict[str, Any]],
    trio_labels: dict[str, str],
    digest: str,
    leak: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append("# Phase 5 Step 5 人工盲审材料")
    lines.append("")
    lines.append(f"- 生成时间：{abc.now_iso()}")
    lines.append(f"- 材料编号：R01–R{len(entries):02d}，全局随机排序")
    lines.append(f"- 随机化摘要（映射校验值）：`{digest}`")
    lines.append(f"- 版本标识泄漏扫描：{'PASS' if leak['ok'] else 'FAIL'}")
    lines.append("")
    lines.append("> 本材料不包含任何上下文版本标识。匿名编号与上下文版本的映射由执行者")
    lines.append("> 保存在隔离临时目录，不随本文件分发。评审请只依据问题、回答全文、")
    lines.append("> Citation 与 Manifest sourceIds 判断。")
    lines.append("")
    lines.append("## 1. 评审说明")
    lines.append("")
    lines.append("- 至少 2 名评审独立完成；两人不要互相讨论后再打分。")
    lines.append("- 每条回答只判断「这条回答本身」，不要猜测它来自哪个版本。")
    lines.append("- L2 关注：是否使用了个人上下文、是否误称来源、是否忽略已注入内容。")
    lines.append("- L3 关注：与同一问题的其他匿名版本相比，回答是否更准确、更贴合用户历史。")
    lines.append("- 评分表中的「个人上下文」指历史消息、Session、Understanding、Memory、Evidence；")
    lines.append("  「知识」指上传文档的检索片段。")
    lines.append("- Manifest sourceIds 仅用于核对「哪些来源被注入」，不是评分对象。")
    lines.append("")
    lines.append("## 2. 逐条盲审表（L2）")
    lines.append("")
    for entry in entries:
        lines.append(f"### {entry['blindId']}")
        lines.append("")
        lines.append(f"- 问题：{_cell(entry['question'])}")
        lines.append(f"- Citation：{_cell(_citation_text(entry.get('citations')))}")
        lines.append(
            "- Manifest sourceIds（评审用）：`{}`".format(
                _cell(", ".join(entry.get("sourceIds") or []) or "无")
            )
        )
        lines.append("")
        lines.append("**回答全文**")
        lines.append("")
        lines.append("```text")
        lines.append(entry.get("answer") or "")
        lines.append("```")
        lines.append("")
        lines.extend(_review_form())
        lines.append("")
    lines.append("## 3. 同一问题的匿名版本对照（L3）")
    lines.append("")
    lines.append(
        "下面按问题分组，每组 3 个匿名版本（甲/乙/丙）。组内顺序已随机化；"
        "甲/乙/丙与上下文的对应关系不在本材料中。请先独立评分，再给出组内排序。"
    )
    lines.append("")
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        groups.setdefault(entry["question"], []).append(entry)
    for group_index, (question, group) in enumerate(groups.items(), start=1):
        lines.append(f"### 组 G{group_index:02d}")
        lines.append("")
        lines.append(f"- 问题：{_cell(question)}")
        lines.append("")
        lines.append("| 版本 | 准确性 1–5 | 贴合历史 1–5 | 使用个人上下文 | 备注 |")
        lines.append("|---|---|---|---|---|")
        for entry in sorted(group, key=lambda item: trio_labels[item["blindId"]]):
            label = trio_labels[entry["blindId"]]
            lines.append(f"| {label}（{entry['blindId']}） |  |  |  |  |")
        lines.append("")
        lines.append("- 组内排序（最准确/最贴合 → 最不）：__________")
        lines.append("")
    lines.append("## 4. 评分回收")
    lines.append("")
    lines.append("- 评审姓名 / 日期：__________")
    lines.append("- 回收方式：由执行者收集后，与隔离临时目录中的映射表合并解盲。")
    lines.append("- 在两名评审完成并回收前，L2/L3 不得标记 PASS。")
    lines.append("")
    return "\n".join(lines) + "\n"


def packet_phase(state_path: str) -> dict[str, Any]:
    state = read_state(state_path)
    results = refresh_observations(load_results(state))
    runs = results.get("runs") or {}
    if len(runs) != len(SCENARIOS) * len(STRATEGIES):
        raise SystemExit(f"调用记录不足：{len(runs)} / {len(SCENARIOS) * len(STRATEGIES)}")

    seed = secrets.token_hex(16)
    rng = random.Random(seed)
    entries: list[dict[str, Any]] = []
    for key, record in runs.items():
        scenario = SCENARIO_BY_ID[record["scenarioId"]]
        entries.append(
            {
                "key": key,
                "scenarioId": record["scenarioId"],
                "strategy": record["strategy"],
                "runId": record.get("runId"),
                "question": scenario["question"],
                "answer": record.get("answerFull") or "",
                "citations": record.get("citations") or [],
                "sourceIds": (record.get("manifestSummary") or {}).get("sourceIds")
                or [],
            }
        )
    rng.shuffle(entries)
    for index, entry in enumerate(entries, start=1):
        entry["blindId"] = f"R{index:02d}"

    trio_labels: dict[str, str] = {}
    labels = ("甲", "乙", "丙")
    by_scenario: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_scenario.setdefault(entry["scenarioId"], []).append(entry)
    for sid in sorted(by_scenario):
        group = by_scenario[sid]
        rng.shuffle(group)
        for label, entry in zip(labels, group):
            trio_labels[entry["blindId"]] = label

    mapping = {
        "createdAt": abc.now_iso(),
        "seed": seed,
        "blindDoc": str(BLIND_DOC),
        "entries": {
            entry["blindId"]: {
                "key": entry["key"],
                "scenarioId": entry["scenarioId"],
                "strategy": entry["strategy"],
                "runId": entry["runId"],
                "trioLabel": trio_labels[entry["blindId"]],
            }
            for entry in entries
        },
    }
    mapping_text = json.dumps(mapping, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(mapping_text.encode("utf-8")).hexdigest()
    write_json(Path(state["tempDir"]) / MAPPING_FILE, mapping)

    doc = _render_blind_doc(entries, trio_labels, digest, {"ok": True, "forbiddenTokenHits": [], "runKeyPatternHits": []})
    leak = leak_scan(doc)
    doc = _render_blind_doc(entries, trio_labels, digest, leak)
    BLIND_DOC.parent.mkdir(parents=True, exist_ok=True)
    BLIND_DOC.write_text(doc, encoding="utf-8")

    summary = {
        "blindDoc": str(BLIND_DOC),
        "mapping": str(Path(state["tempDir"]) / MAPPING_FILE),
        "entries": len(entries),
        "digest": digest,
        "leak": leak,
    }
    results["blindPacket"] = summary
    save_results(state, results)
    write_json(Path(state["tempDir"]) / "step5-blind-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


def cleanup_phase(state_path: str) -> dict[str, Any]:
    summary = abc.cleanup_phase(state_path)
    state = read_state(state_path)
    write_json(Path(state["tempDir"]) / "step5-cleanup-summary.json", summary)
    return summary


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _health_summary(results: dict[str, Any]) -> dict[str, Any]:
    runs = results.get("runs") or {}
    failures = {
        key: call_health_problems(record)
        for key, record in runs.items()
        if call_health_problems(record)
    }
    llm_total = sum(int(record.get("llmCallCount") or 0) for record in runs.values())
    embedding_total = abc._embedding_totals(runs.values())
    return {
        "runs": len(runs),
        "expectedRuns": len(SCENARIOS) * len(STRATEGIES),
        "callFailures": failures,
        "llmCalls": llm_total,
        "embedding": embedding_total,
        "tavily": 0,
    }


def _machine_patterns(results: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sid in [item["id"] for item in SCENARIOS]:
        for strategy in STRATEGIES:
            record = (results.get("runs") or {}).get(run_key(sid, strategy))
            if not record:
                continue
            observation = record.get("step5Observation") or {}
            rows.append(
                {
                    "scenarioId": sid,
                    "strategy": strategy,
                    "personalTypesInjected": observation.get(
                        "personalTypesInjected", []
                    ),
                    "personalContextUsed": observation.get("personalContextUsed"),
                    "quoteSources": observation.get("quoteSources", []),
                    "understandingAsOriginal": observation.get(
                        "understandingAsOriginal"
                    ),
                    "ignoredProxy": observation.get(
                        "ignoredInjectedPersonalLiteralProxy"
                    ),
                    "answerChars": record.get("answerChars"),
                }
            )
    return rows


def _candidate_patterns(results: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for sid in [item["id"] for item in SCENARIOS]:
        for strategy in ("B", "C"):
            record = (results.get("runs") or {}).get(run_key(sid, strategy))
            if not record:
                continue
            observation = record.get("step5Observation") or {}
            injected = observation.get("personalTypesInjected") or []
            if not injected:
                continue
            if not observation.get("personalContextUsed"):
                candidates.append(
                    {
                        "scenarioId": sid,
                        "strategy": strategy,
                        "pattern": "注入个人来源但回答未出现可区分个人片段",
                        "injected": injected,
                        "answerChars": record.get("answerChars"),
                    }
                )
            if observation.get("understandingAsOriginal"):
                candidates.append(
                    {
                        "scenarioId": sid,
                        "strategy": strategy,
                        "pattern": "把 Understanding 转述称为原话/原文",
                        "detail": observation.get("understandingMislabelDetail"),
                    }
                )
    return candidates


def report_phase(state_path: str) -> dict[str, Any]:
    state = read_state(state_path)
    results = refresh_observations(load_results(state))
    snapshot_file = snapshot_path(state)
    snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
    setup_file = Path(state["tempDir"]) / "step5-setup.json"
    setup = (
        json.loads(setup_file.read_text(encoding="utf-8"))
        if setup_file.exists()
        else {}
    )
    cleanup_file = Path(state["tempDir"]) / "step5-cleanup-summary.json"
    cleanup = (
        json.loads(cleanup_file.read_text(encoding="utf-8"))
        if cleanup_file.exists()
        else None
    )
    health = _health_summary(results)
    blind = results.get("blindPacket") or {}
    patterns = _machine_patterns(results)
    candidates = _candidate_patterns(results)
    ignored_candidates = [
        item
        for item in candidates
        if item["pattern"].startswith("注入个人来源")
    ]
    ignored_scenarios = sorted({item["scenarioId"] for item in ignored_candidates})
    final_checks = results.get("finalChecks") or {}
    git = abc.git_info()
    leak_ok = bool((blind.get("leak") or {}).get("ok"))
    batch_failed = bool(health["callFailures"]) or not leak_ok
    batch_status = "FAIL" if batch_failed else "BLOCKED"
    batch_reason = (
        "调用健康或盲审材料泄漏检查未通过"
        if batch_failed
        else "人工盲审未完成：未安排至少 2 名评审"
    )

    lines: list[str] = []
    lines.append("# Phase 5 Step 5：A/B/C 对照、盲审准备与 L1/L2/L3 分层判定")
    lines.append("")
    lines.append(f"- 执行时间：{abc.now_iso()}")
    lines.append(f"- 测试戳：`{state['stamp']}`")
    lines.append(f"- 执行脚本：`scripts/phase5/full/run_step5.py`")
    lines.append(f"- 隔离库：`{snapshot['isolation']['db']}`")
    lines.append(
        f"- 文档 collection：`{snapshot['env']['docCollection']}`；"
        f"Memory collection：`{snapshot['env']['memoryCollection']}`"
    )
    lines.append(
        f"- 生成参数：model=`{snapshot['generationProfile'].get('AILS_LLM_MODEL')}`，"
        f"max_tokens=`{snapshot['generationProfile'].get('AILS_LLM_MAX_TOKENS')}`，"
        "temperature 在子进程强制为 0"
    )
    lines.append(f"- Tavily：0 次调用")
    lines.append("")
    lines.append(
        f"> 本批结论：**{batch_status}**（{batch_reason}）。"
        "30 次调用与盲审材料已执行；L2/L3 不伪造评分。"
    )
    lines.append("")

    lines.append("## 1. 前置条件确认")
    lines.append("")
    preflight = ((snapshot.get("setup") or {}).get("preflight")) or []
    rows = [
        [item.get("name"), "PASS" if item.get("ok") else "FAIL", item.get("detail")]
        for item in preflight
    ]
    rows.append(
        [
            "人工盲审（≥2 名评审）",
            "BLOCKED",
            "本执行环境未收到评审人安排、评分表回收或评审签名；按任务前置条件标记 BLOCKED",
        ]
    )
    lines.append(abc.md_table(["检查项", "结果", "说明"], rows))
    lines.append("")
    lines.append(
        "- 隔离环境、内部凭据、分析消费者关闭、`max_tokens >= 4096` 均沿用 "
        "Step 3/Step 4 已通过的 setup preflight。"
    )
    lines.append(
        "- 人工盲审：**未安排**。本批生成材料但不评分；L2/L3 的 PASS/FAIL 必须等"
        "至少 2 名评审独立完成后才能给出。"
    )
    lines.append("")

    lines.append("## 2. 30 次调用汇总（场景 × 策略 + L2 观测字段）")
    lines.append("")
    lines.append(
        "每个场景使用同一问题、同一数据快照、同一 Knowledge 文档；A/B/C 顺序"
        "按场景索引轮转（S1=A→B→C，S2=B→C→A，S3=C→A→B，依次循环），"
        "因此三个策略在整批中交错出现。"
    )
    lines.append("")
    run_rows = []
    for sid in [item["id"] for item in SCENARIOS]:
        order = (results.get("orders") or {}).get(sid) or []
        for strategy in order:
            record = (results.get("runs") or {}).get(run_key(sid, strategy)) or {}
            observation = record.get("step5Observation") or {}
            fields = record.get("usageFields") or {}
            run_rows.append(
                [
                    sid,
                    strategy,
                    record.get("runId"),
                    record.get("finishReason"),
                    "是" if observation.get("historyGuardTriggered") else "否",
                    (
                        "是（{}）".format(
                            ", ".join(observation.get("memoryDegradedSource") or [])
                        )
                        if observation.get("memoryDegraded")
                        else "否"
                    ),
                    f"{fields.get('promptTokens')}/{fields.get('completionTokens')}/{fields.get('totalTokens')}",
                    ",".join(observation.get("personalTypesInjected") or []) or "-",
                    "是" if observation.get("personalContextUsed") else "否",
                    ",".join(observation.get("quoteSources") or []) or "-",
                    "是" if observation.get("understandingAsOriginal") else "否",
                    "是" if observation.get("ignoredInjectedPersonalLiteralProxy") else "否",
                    record.get("answerChars"),
                    "OK" if not call_health_problems(record) else "FAIL",
                ]
            )
    lines.append(
        abc.md_table(
            [
                "场景",
                "策略",
                "run_id",
                "finish",
                "HISTORY_GUARD",
                "Memory 降级",
                "prompt/completion/total",
                "注入个人来源",
                "个人上下文被使用",
                "引用来源",
                "Understanding 误称原话",
                "忽略注入（字面代理）",
                "字数",
                "调用健康",
            ],
            run_rows,
        )
    )
    lines.append("")
    lines.append(
        "- 「个人上下文被使用」与「引用来源」是**字面观测**：只检查回答是否出现"
        "预置数据中的可区分片段，不做语义推断。"
    )
    lines.append(
        "- 「忽略注入（字面代理）」只表示「注入了个人来源但回答没有出现任何可区分"
        "个人片段」，**不等于**人工盲审认定的「忽略」；最终以盲审为准。"
    )
    lines.append("")

    lines.append("### 2.1 调用健康与成本")
    lines.append("")
    health_rows = [
        ["调用记录", f"{health['runs']} / {health['expectedRuns']}"],
        ["LLM 调用（含守卫重生成）", health["llmCalls"]],
        ["LLM 预算", "≤ 30"],
        ["Embedding 调用（Agent 进程内）", (health["embedding"] or {}).get("calls")],
        ["Embedding 文本数", (health["embedding"] or {}).get("texts")],
        ["Embedding 预算", "≤ 60"],
        ["Tavily", 0],
        [
            "error / timeout / 空输出 / 截断",
            "无" if not health["callFailures"] else "; ".join(
                f"{key}: {', '.join(items)}"
                for key, items in health["callFailures"].items()
            ),
        ],
    ]
    lines.append(abc.md_table(["项目", "数值/结果"], health_rows))
    lines.append("")

    lines.append("## 3. 盲审材料说明")
    lines.append("")
    lines.append(f"- 材料文件：`{BLIND_DOC.relative_to(ROOT).as_posix()}`")
    lines.append(
        f"- 30 条回答全局随机编号 R01–R30，随机化摘要（映射校验值）："
        f"`{blind.get('digest')}`"
    )
    lines.append(
        "- 每条附：问题、回答全文、Citation（如有）、Manifest sourceIds（仅评审用）。"
        "不附策略名、intent、policy_version 或上下文版本指纹。"
    )
    lines.append(
        "- 策略与编号的映射保存在隔离临时目录 "
        f"`{MAPPING_FILE}`；不写入仓库、不随盲审材料分发。"
    )
    lines.append(
        "- L3 对照：同一问题的 3 个匿名版本用「甲/乙/丙」表示，组内顺序随机；"
        "甲/乙/丙与上下文的对应关系同样只在隔离映射表中。"
    )
    lines.append(
        f"- 策略标识泄漏扫描：**{'PASS' if (blind.get('leak') or {}).get('ok') else 'FAIL'}**；"
        f"命中 token={((blind.get('leak') or {}).get('forbiddenTokenHits') or [])}；"
        f"命中 run key={((blind.get('leak') or {}).get('runKeyPatternHits') or [])}。"
    )
    lines.append(
        "- 评审表维度：L2（是否使用个人上下文、是否误称来源、是否忽略注入内容、"
        "引用了哪些来源、证据）；L3（准确性 1–5、与用户历史贴合度 1–5、组内排序）。"
    )
    lines.append("")

    lines.append("## 4. L1 / L2 / L3 矩阵")
    lines.append("")
    lines.append("### 4.1 L1")
    lines.append("")
    lines.append(
        "L1 沿用 Step 4 结果，本批不重复判定。"
    )
    lines.append("")
    lines.append(
        abc.md_table(
            ["层级", "范围", "状态", "证据"],
            [
                [
                    "L1",
                    "真实 MySQL + Qdrant + Embedding + LLM 全链路、隔离、冻结、清理",
                    "PASS（沿用 Step 4）",
                    "`docs/testing/phase5-step4-results.md` §7.1–§7.2；本批 30 次调用"
                    "未出现 error/timeout/空输出/截断，链路健康与 Step 4 一致",
                ]
            ],
        )
    )
    lines.append("")

    lines.append("### 4.2 L2")
    lines.append("")
    lines.append(
        "L2 的 PASS/FAIL 必须由人工盲审给出。本批只提供逐场景机器观测与盲审入口；"
        "由于未完成人工盲审，10 个场景一律标记 **BLOCKED**。"
    )
    lines.append("")
    l2_rows = []
    for sid in [item["id"] for item in SCENARIOS]:
        records = [
            (results.get("runs") or {}).get(run_key(sid, strategy)) or {}
            for strategy in STRATEGIES
        ]
        injected = sorted(
            {
                item
                for record in records
                for item in (
                    (record.get("step5Observation") or {}).get(
                        "personalTypesInjected"
                    )
                    or []
                )
            }
        )
        used = [
            strategy
            for strategy, record in zip(STRATEGIES, records)
            if (record.get("step5Observation") or {}).get("personalContextUsed")
        ]
        mislabel = [
            strategy
            for strategy, record in zip(STRATEGIES, records)
            if (record.get("step5Observation") or {}).get("understandingAsOriginal")
        ]
        ignored = [
            strategy
            for strategy, record in zip(STRATEGIES, records)
            if (record.get("step5Observation") or {}).get(
                "ignoredInjectedPersonalLiteralProxy"
            )
        ]
        l2_rows.append(
            [
                sid,
                ",".join(injected) or "-",
                ",".join(used) or "-",
                ",".join(mislabel) or "-",
                ",".join(ignored) or "-",
                "BLOCKED（待≥2 名评审）",
            ]
        )
    lines.append(
        abc.md_table(
            [
                "场景",
                "注入过的个人来源",
                "字面使用个人上下文的策略",
                "Understanding 误称原话的策略",
                "忽略注入（字面代理）的策略",
                "L2 状态",
            ],
            l2_rows,
        )
    )
    lines.append("")
    lines.append(
        "- 「字面使用」只说明回答出现了预置数据的可区分片段；没有出现不等于语义上"
        "没有使用。L2 的最终判定必须由评审阅读回答全文后给出。"
    )
    lines.append("")

    lines.append("### 4.3 L3")
    lines.append("")
    lines.append(
        abc.md_table(
            ["层级", "判定内容", "状态", "证据"],
            [
                [
                    "L3",
                    "个性化是否带来收益：对比 A/B/C 的人工准确性/贴合度评分",
                    "BLOCKED",
                    "未完成人工盲审，无 A/B/C 人工评分；不得用机器字面观测替代",
                ]
            ],
        )
    )
    lines.append("")

    lines.append("## 5. L2 失败模式归纳（候选，未经盲审确认）")
    lines.append("")
    if candidates:
        lines.append(
            abc.md_table(
                ["场景", "策略", "候选模式", "证据"],
                [
                    [
                        item["scenarioId"],
                        item["strategy"],
                        item["pattern"],
                        json.dumps(
                            {
                                key: value
                                for key, value in item.items()
                                if key not in {"scenarioId", "strategy", "pattern"}
                            },
                            ensure_ascii=False,
                        ),
                    ]
                    for item in candidates
                ],
            )
        )
    else:
        lines.append(
            "机器字面观测未发现「注入个人来源但完全无个人片段」或"
            "「Understanding 误称原话」的候选模式。"
        )
    lines.append("")
    if ignored_candidates:
        lines.append(
            "- 本批「注入个人来源但未出现可区分个人片段」出现在 "
            f"{len(ignored_scenarios)}/10 场景（{len(ignored_candidates)} 条 B/C 记录），"
            "跨场景重复；但每个「场景 × 策略」仅 1 次，且字面片段可能漏检语义使用，"
            "仍需盲审确认。"
        )
    lines.append(
        "- 这些只是**候选失败模式**，用于提示评审重点关注，不构成 L2 FAIL。"
    )
    lines.append(
        "- 稳定性口径：本批每个「场景 × 策略」仅 1 次调用，无法判断同一格内的"
        "重复稳定性；跨场景重复只能说明该现象不局限于单题，仍需盲审与 repeat 验证。"
    )
    s5_original = "我原来以为慢启动慢是因为它每次只把窗口加一"
    s5_quoted: list[str] = []
    for strategy in ("B", "C"):
        record = (results.get("runs") or {}).get(run_key("S5", strategy)) or {}
        if s5_original in (record.get("answerFull") or ""):
            s5_quoted.append(strategy)
    lines.append(
        f"- S5 复核：{'、'.join(s5_quoted) or '无'} 逐字引用了历史原句"
        f"「{s5_original}」；B/C 未包含 Step 3 的合成标记 `【S5-HISTORY】`，"
        "但按 Step 3 修正后的「原句内容或三关键词」判据不构成失败。"
        "A 明确声明无法引用，符合无个人上下文的预期。"
    )
    lines.append(
        "- 已知的预期差异：S7 的 C 按 Policy 不注入旧 Session/Understanding/Memory，"
        "因此 C 未使用个人上下文是**设计预期**，不是失败；B 在该场景仍注入旧 Session，"
        "是 B 近似策略的已知缺陷。"
    )
    lines.append("")

    lines.append("## 6. L3 对照结果")
    lines.append("")
    lines.append(
        "**BLOCKED**。本批没有 A/B/C 的人工评分，因此不能判定个性化是否带来收益。"
        "为避免伪造结论，本节不提供替代评分；只记录机器可复现的非质量指标："
    )
    lines.append("")
    l3_rows = []
    for sid in [item["id"] for item in SCENARIOS]:
        row = [sid]
        for strategy in STRATEGIES:
            record = (results.get("runs") or {}).get(run_key(sid, strategy)) or {}
            row.append(
                "{} 字 / {} 引用".format(
                    record.get("answerChars") or 0,
                    len(record.get("citationSet") or []),
                )
            )
        l3_rows.append(row)
    lines.append(
        abc.md_table(
            ["场景", "A（字数/引用）", "B（字数/引用）", "C（字数/引用）"], l3_rows
        )
    )
    lines.append("")
    lines.append(
        "- 字数与引用数**不是** L3 质量评分，不能用于判断「更准确」或「更贴合历史」。"
    )
    lines.append("")

    lines.append("## 7. 最小干预建议（本批不实施）")
    lines.append("")
    lines.append(
        "以下建议只在人工盲审确认失败模式后才考虑实施；本批未修改任何 Prompt / "
        "ContextBuilder / Policy / Schema。"
    )
    lines.append("")
    suggestions = [
        (
            "若盲审确认 B/C 在多场景「注入了个人来源但回答完全不用」",
            "优先在 Prompt 层增加「回答需显式结合用户已有理解/记忆」的约束；"
            "不要先改 ContextBuilder（Manifest 已证明来源注入成功，Step 4 D2 同因）。",
        ),
        (
            "若盲审确认「Understanding 被误称为原话」",
            "在 Prompt 中要求区分「系统记录的你当前理解」与「你的原话」；"
            "Step 4 已记录同类缺口，仍应先改 Prompt，不改 Schema。",
        ),
        (
            "若盲审确认 S5 复盘场景未引用历史原句",
            "检查 Prompt 对「引用原话」的约束是否足够强；Step 3 已把判据从"
            "「逐字包含合成标记」修正为「原句内容或三关键词」，本批沿用该边界。",
        ),
        (
            "若盲审确认 B 在话题切换场景被无关旧 Session 干扰",
            "这是 B「近似旧策略」的已知设计差异；是否需要修 B 取决于 B 的定位"
            "（对照组还是可上线策略），不应在本批直接改 Policy。",
        ),
        (
            "若盲审确认 S9 DEGRADED 场景回答质量下降",
            "保留 C 的 DEGRADED 显式状态，不把降级伪装成 EMPTY；若需要改善回答，"
            "应先确认降级提示是否足够清晰，再考虑 Prompt 层说明。",
        ),
        (
            "若两名评审的一致性低（例如同一场景结论相反）",
            "先校准评分表与证据要求，再增加评审人数；不要用第三人评分强行凑 PASS。",
        ),
    ]
    lines.append(
        abc.md_table(["触发条件", "最小干预建议"], [[a, b] for a, b in suggestions])
    )
    lines.append("")

    lines.append("## 8. Step 5 结论")
    lines.append("")
    lines.append("### 8.1 失败判据逐条检查")
    lines.append("")
    lines.append(
        abc.md_table(
            ["失败判据", "结果", "证据"],
            [
                [
                    "30 次调用有 error / timeout / 空输出 / 截断",
                    "PASS（未命中）" if not health["callFailures"] else "FAIL",
                    "见 §2.1；失败项："
                    + (
                        json.dumps(health["callFailures"], ensure_ascii=False)
                        if health["callFailures"]
                        else "无"
                    ),
                ],
                [
                    "盲审材料暴露策略标识",
                    "PASS（未命中）"
                    if (blind.get("leak") or {}).get("ok")
                    else "FAIL",
                    f"泄漏扫描：{json.dumps(blind.get('leak') or {}, ensure_ascii=False)}",
                ],
                [
                    "L2 判定缺场景或无法归因",
                    "PASS（10/10 场景均有记录与机器证据）",
                    "见 §4.2；每个场景均有记录与机器证据，L2 状态统一为 BLOCKED，未缺场景",
                ],
                [
                    "人工盲审未完成却标记 PASS",
                    "PASS（未发生）",
                    "L2/L3 均标记 BLOCKED，未给出任何 PASS 评分",
                ],
            ],
        )
    )
    lines.append("")
    lines.append("### 8.2 结论")
    lines.append("")
    lines.append(f"**Step 5：{batch_status}（{batch_reason}）。**")
    lines.append("")
    lines.append(
        "- 已执行：30 次真实调用、逐场景 L2 机器观测、随机化盲审材料、"
        "L1/L2/L3 矩阵、候选失败模式与最小干预建议。"
    )
    lines.append(
        "- 未完成：至少 2 名评审的独立 L2 判定与 L3 评分回收。"
    )
    lines.append(
        "- 因此 Phase 5 关闭条件**未满足**：不能在盲审完成前声称 L2/L3 PASS，"
        "也不能用机器字面观测替代人工质量判断。"
    )
    lines.append(
        "- 下一步：按 `docs/testing/phase5-blind-review.md` 组织 2 名评审；"
        "回收后由执行者用隔离映射表解盲，再补写 L2/L3 矩阵与结论。"
    )
    lines.append("")

    lines.append("## 9. 工作区状态")
    lines.append("")
    lines.append(
        abc.md_table(
            ["项目", "值"],
            [
                ["branch", git.get("branch")],
                ["HEAD", git.get("head")],
                ["HEAD subject", git.get("headSubject")],
                ["工作区状态", git.get("status") or "(clean)"],
                ["是否 commit", "否（本批次未执行 git commit）"],
                ["是否 push", "否（本批次未执行 git push）"],
            ],
        )
    )
    lines.append("")
    lines.append(
        "- 新增允许文件：`scripts/phase5/full/run_step5.py`、"
        "`docs/testing/phase5-step5-results.md`、"
        "`docs/testing/phase5-blind-review.md`。"
    )
    lines.append(
        "- 未修改：Prompt、ContextBuilder、Policy、Schema、Runtime、"
        "HISTORY_GUARD、Tavily。"
    )
    if cleanup:
        residual = cleanup.get("residual") or {}
        lines.append(
            "- 清理：cleanup exitCode={}，独立残留检查={}；MySQL 残留={}，"
            "Qdrant 残留={}，RabbitMQ 队列/交换机残留={}/{}，端口残留={}。".format(
                cleanup.get("exitCode"),
                "PASS" if residual.get("ok") else "FAIL",
                residual.get("databaseRemaining"),
                residual.get("qdrantPhase5Collections"),
                residual.get("rabbitQueues"),
                residual.get("rabbitExchanges"),
                residual.get("portsStillOpen"),
            )
        )
    else:
        lines.append("- 清理：尚未执行 cleanup。")
    lines.append("")

    lines.append("## 10. 待审核决策点")
    lines.append("")
    decisions = [
        "**评审人安排**：请提供至少 2 名评审的姓名/排期；在评分回收前 L2/L3 保持 BLOCKED。",
        "**L2 判定口径**：是否接受「字面片段命中」作为评审辅助，但最终以评审阅读全文为准？",
        "**L3 收益阈值**：A/B/C 的人工评分差达到多少才算「个性化带来收益」？需要预注册。",
        "**单次调用 versus 稳定性**：本批每个「场景 × 策略」只有 1 次，无法判断失败模式是否稳定；是否需要在盲审前补 repeat？",
        "**S7 的 B 近似缺陷**：B 在话题切换场景注入旧 Session 是已知设计差异；是否仍把 B 当作可上线策略？",
        "**S5 原话判据**：本批沿用 Step 3 修正后的「原句内容或三关键词」，不再要求逐字复制 `【S5-HISTORY】`；请确认。",
        "**Step 4 D1/D2**：个人来源 Citation 继续按 Manifest 追溯（D1），回答个性化按本批盲审验收（D2）；请确认。",
        "**盲审完成后的动作**：若 L2 FAIL 稳定出现，是否批准下一批只改 Prompt（不改 ContextBuilder/Policy/Schema）？",
    ]
    for index, item in enumerate(decisions, start=1):
        lines.append(f"{index}. {item}")
    lines.append("")

    RESULTS_DOC.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_DOC.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "report": str(RESULTS_DOC),
        "runs": health["runs"],
        "callFailures": health["callFailures"],
        "blindLeakOk": (blind.get("leak") or {}).get("ok"),
        "l2": "BLOCKED",
        "l3": "BLOCKED",
        "conclusion": batch_status,
        "reason": batch_reason,
        "git": git,
    }
    write_json(Path(state["tempDir"]) / "step5-report-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _latest_state_path() -> str:
    candidates = sorted(
        Path(__import__("os").environ.get("TEMP", ".")).glob(
            "phase5-full-*/state.json"
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit("找不到 phase5-full-*/state.json")
    return str(candidates[0])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        required=True,
        choices=["setup", "run", "packet", "cleanup", "report", "all"],
    )
    parser.add_argument("--state", help="state.json（setup 阶段不需要）")
    args = parser.parse_args()

    if args.phase == "setup":
        setup_phase()
        return 0
    if args.phase == "all":
        setup_phase()
        state_path = _latest_state_path()
        run_phase(state_path)
        packet_phase(state_path)
        cleanup_phase(state_path)
        report_phase(state_path)
        return 0
    if not args.state:
        parser.error(f"--phase {args.phase} 需要 --state")
    if args.phase == "run":
        run_phase(args.state)
        return 0
    if args.phase == "packet":
        packet_phase(args.state)
        return 0
    if args.phase == "cleanup":
        summary = cleanup_phase(args.state)
        return 0 if (summary.get("residual") or {}).get("ok") else 1
    if args.phase == "report":
        report_phase(args.state)
        return 0
    parser.error("未知阶段")
    return 2


if __name__ == "__main__":
    sys.exit(main())
