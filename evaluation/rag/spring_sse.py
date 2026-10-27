"""真实 Spring SSE 契约验收。

评测主链路（`runner.py`）在进程内复用 Agent 的 prepare/generate/validate；
本脚本单独走一次**真实 HTTP + Spring SSE**，确认：

- `message.start` 必为首事件，`message.end` 必为成功终态；
- `message.delta` 能拼出包含真实检索事实的完整回答；
- 引用标签格式与 `[资料《文件名》#序号]` 一致；
- 无证据问题仍能正常 `message.end`，且不伪造引用；
- 事件名/字段没有被本次改动破坏。

它不评测模型质量（那是 runner 的职责），也不替代 runner 的四层评分。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .runner import BackendClient, CORPUS_DIR, REPORTS_DIR, BackendError

CITATION_PATTERN = re.compile(r"\[资料《(?P<name>[^》\n]+)》#(?P<index>\d+)\]")
CITATION_LIKE_PATTERN = re.compile(r"\[资料[^\]\n]*\]")
ALLOWED_EVENTS = {"message.start", "message.delta", "message.end", "message.error"}

SSE_CASES: list[dict[str, Any]] = [
    {
        "id": "S01",
        "question": "ZenFrame 的提醒模块通过什么协议发送云端定时提醒？",
        "requiredTerms": ["MQTT"],
        "requiredCitationDocuments": ["zenframe.md"],
        "expectNoCitation": False,
        "expectAdmission": False,
    },
    {
        "id": "S02",
        "question": "ZealDeck 的离线提醒最多缓存多少条？",
        "requiredTerms": ["5"],
        "requiredCitationDocuments": ["zealdeck.md"],
        "expectNoCitation": False,
        "expectAdmission": False,
    },
    {
        "id": "S03",
        "question": "请比较 ZenFrame 和 ZealDeck 的近场传输技术和传输上限。",
        "requiredTerms": ["星闪", "BLE", "8", "4"],
        "requiredCitationDocuments": ["zenframe.md", "zealdeck.md"],
        "expectNoCitation": False,
        "expectAdmission": False,
    },
    {
        "id": "S04",
        "question": "请解释量子色动力学中的渐进自由。",
        "requiredTerms": [],
        "requiredCitationDocuments": [],
        "expectNoCitation": True,
        "expectAdmission": True,
        # 与 dataset_v2.jsonl 的 D01/F02 一致：必须是否定词 + 所问对象，
        # 不能因为回答里出现「没有 / 无法」就判定为承认资料不足。
        "admissionPatterns": [
            r"(?:资料|文档|上传)[^。！？\n]{0,15}(?:没有|未|不)(?:直接|相关|命中|检索到|找到|包含|提供|提及|覆盖)",
            r"(?:没有|未)(?:找到|检索到|命中|检索出)[^。！？\n]{0,15}(?:资料|文档|内容|相关|依据)",
            r"没有直接答案",
        ],
    },
]


def analyze_events(events: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    names = [name for name, _ in events]
    start_payloads = [payload for name, payload in events if name == "message.start"]
    end_payloads = [payload for name, payload in events if name == "message.end"]
    error_payloads = [payload for name, payload in events if name == "message.error"]
    delta_payloads = [payload for name, payload in events if name == "message.delta"]
    answer = "".join(str(payload.get("delta") or "") for payload in delta_payloads)

    format_ok = bool(names) and all(name in ALLOWED_EVENTS for name in names)
    start_ok = (
        len(start_payloads) == 1
        and names[0] == "message.start"
        and {"runId", "conversationId"} <= set(start_payloads[0])
    )
    end_ok = (
        len(end_payloads) == 1
        and names[-1] == "message.end"
        and {"messageId", "runId"} <= set(end_payloads[0])
    )
    delta_ok = all("delta" in payload for payload in delta_payloads)
    citations = [match.group(0) for match in CITATION_PATTERN.finditer(answer)]
    malformed = [
        match.group(0)
        for match in CITATION_LIKE_PATTERN.finditer(answer)
        if match.group(0) not in citations
    ]
    return {
        "eventNames": names,
        "eventCount": len(events),
        "formatOk": format_ok,
        "startOk": start_ok,
        "endOk": end_ok,
        "deltaOk": delta_ok,
        "hasError": bool(error_payloads),
        "errorPayloads": error_payloads,
        "deltaCount": len(delta_payloads),
        "answer": answer,
        "citations": citations,
        "citedDocuments": sorted({m.group("name") for m in CITATION_PATTERN.finditer(answer)}),
        "malformedCitations": malformed,
        "startPayload": start_payloads[0] if start_payloads else None,
        "endPayload": end_payloads[0] if end_payloads else None,
    }


def judge(case: dict[str, Any], analysis: dict[str, Any]) -> list[dict[str, Any]]:
    answer = analysis["answer"]
    checks = [
        {"name": "sse_format", "passed": analysis["formatOk"]},
        {"name": "start_first", "passed": analysis["startOk"]},
        {"name": "end_last", "passed": analysis["endOk"]},
        {"name": "no_message_error", "passed": not analysis["hasError"]},
        {"name": "delta_fields", "passed": analysis["deltaOk"]},
        {"name": "answer_not_empty", "passed": bool(answer.strip())},
        {
            "name": "required_terms",
            "passed": all(term in answer for term in case["requiredTerms"]),
            "detail": case["requiredTerms"],
        },
        {
            "name": "required_citation_documents",
            "passed": set(case["requiredCitationDocuments"])
            <= set(analysis["citedDocuments"]),
            "detail": case["requiredCitationDocuments"],
        },
        {
            "name": "no_malformed_citations",
            "passed": not analysis["malformedCitations"],
            "detail": analysis["malformedCitations"],
        },
    ]
    if case["expectNoCitation"]:
        checks.append(
            {"name": "no_fabricated_citation", "passed": not analysis["citations"]}
        )
    if case["expectAdmission"]:
        patterns = case.get("admissionPatterns") or []
        found = [pattern for pattern in patterns if re.search(pattern, answer)]
        checks.append(
            {
                "name": "admits_missing_document",
                "passed": bool(patterns) and bool(found),
                "detail": {"patterns": patterns, "found": found},
            }
        )
    return checks


async def run(
    *,
    backend_url: str,
    embedding_model: str,
    output_dir: Path | None,
    cases: list[dict[str, Any]] | None = None,
) -> int:
    cases = cases if cases is not None else SSE_CASES
    backend = BackendClient(backend_url)
    started_at = datetime.now(timezone.utc).isoformat()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    try:
        token, user_id = await backend.register_or_login(
            f"phase42_sse_{stamp}", "Phase42-SSE-2026!"
        )
        space_id = await backend.create_space(
            token, f"Phase 4.2 SSE {stamp}", "Spring SSE 验收专用；请勿用于真实业务"
        )
        document_ids: list[int] = []
        documents: list[dict[str, Any]] = []
        for path in sorted(CORPUS_DIR.glob("*.md")):
            document_id = await backend.upload_document(token, space_id, path)
            document_ids.append(document_id)
            # 与 runner 一样串行等待，避免并发建索引在测试库上触发死锁。
            documents.extend(await backend.wait_ready(token, space_id, [document_id]))
        documents = await backend.wait_ready(token, space_id, document_ids)
        ready_ids = await backend.ready_ids(token, space_id, embedding_model)
        conversation_id = await backend.create_conversation(
            token, space_id, "Phase 4.2 SSE Acceptance"
        )

        records: list[dict[str, Any]] = []
        for case in cases:
            events = await backend.stream_message(
                token, conversation_id, case["question"]
            )
            analysis = analyze_events(events)
            checks = judge(case, analysis)
            record = {
                "caseId": case["id"],
                "question": case["question"],
                "conversationId": conversation_id,
                "events": [
                    {"event": name, "data": payload} for name, payload in events
                ],
                "analysis": analysis,
                "checks": checks,
                "passed": all(check["passed"] for check in checks),
            }
            records.append(record)
            print(
                f"[{case['id']}] passed={record['passed']} "
                f"events={analysis['eventCount']} deltas={analysis['deltaCount']} "
                f"citations={len(analysis['citations'])} error={analysis['hasError']}",
                flush=True,
            )

        if output_dir is None:
            output_dir = REPORTS_DIR / f"spring_sse_{stamp}"
        output_dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "startedAt": started_at,
            "backendUrl": backend_url,
            "userId": user_id,
            "spaceId": space_id,
            "conversationId": conversation_id,
            "documents": [
                {
                    "id": doc.get("id"),
                    "name": doc.get("name"),
                    "status": doc.get("status"),
                    "chunkCount": doc.get("chunkCount"),
                    "embeddingModel": doc.get("embeddingModel"),
                }
                for doc in documents
            ],
            "readyIds": ready_ids,
            "cases": records,
            "passed": all(record["passed"] for record in records),
        }
        (output_dir / "spring_sse.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (output_dir / "spring_sse.md").write_text(
            _markdown(summary), encoding="utf-8"
        )
        print(f"报告：{output_dir}")
        return 0 if summary["passed"] else 1
    finally:
        await backend.aclose()


def _markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Phase 4.2 Spring SSE 验收",
        "",
        f"- 后端：`{summary['backendUrl']}`",
        f"- Space：`{summary['spaceId']}`，Conversation：`{summary['conversationId']}`",
        f"- ready ids：`{summary['readyIds']}`",
        f"- 总体：{'通过' if summary['passed'] else '未通过'}",
        "",
        "| 用例 | 问题 | 通过 | 事件数 | delta 数 | 引用数 | error |",
        "| --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for record in summary["cases"]:
        analysis = record["analysis"]
        lines.append(
            f"| {record['caseId']} | {record['question']} | "
            f"{'是' if record['passed'] else '否'} | {analysis['eventCount']} | "
            f"{analysis['deltaCount']} | {len(analysis['citations'])} | "
            f"{'有' if analysis['hasError'] else '无'} |"
        )
    lines.extend(["", "## 逐项检查", ""])
    for record in summary["cases"]:
        lines.append(f"### {record['caseId']}")
        lines.append("")
        lines.append("回答：")
        lines.append("")
        lines.append("```text")
        lines.append(record["analysis"]["answer"])
        lines.append("```")
        lines.append("")
        for check in record["checks"]:
            status = "通过" if check["passed"] else "失败"
            detail = f"（{check.get('detail')}）" if check.get("detail") else ""
            lines.append(f"- {check['name']}：{status}{detail}")
        lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 4.2 Spring SSE acceptance")
    parser.add_argument("--backend-url", default="http://127.0.0.1:8080")
    parser.add_argument("--embedding-model", default="text-embedding-v4")
    parser.add_argument("--output-dir", default="")
    parser.add_argument(
        "--cases",
        default="",
        help="只跑指定 SSE 用例，逗号分隔（如 S04）；默认全部",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        import asyncio

        selected = [item.strip() for item in args.cases.split(",") if item.strip()]
        by_id = {case["id"]: case for case in SSE_CASES}
        unknown = [case_id for case_id in selected if case_id not in by_id]
        if unknown:
            raise RuntimeError(f"未知 SSE 用例：{unknown}")
        cases = [by_id[case_id] for case_id in selected] if selected else SSE_CASES
        return asyncio.run(
            run(
                backend_url=args.backend_url,
                embedding_model=args.embedding_model,
                output_dir=Path(args.output_dir) if args.output_dir else None,
                cases=cases,
            )
        )
    except KeyboardInterrupt:
        return 130
    except (BackendError, RuntimeError) as exc:
        print(f"SSE 验收失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
