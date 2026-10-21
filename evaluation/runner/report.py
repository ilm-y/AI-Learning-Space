"""报告生成。

每次运行覆盖 `reports/latest.md` / `reports/latest.json`，不堆历史文件：
历史对比应该靠 git（提交报告的那次 diff），而不是靠文件名里的时间戳。

Markdown 是给人看的（先看总览，再看失败项）；JSON 是给后续脚本用的。
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from ..metrics import summarize
from .models import STATUS_FAILED, STATUS_PASSED, STATUS_SKIPPED, CaseResult

REPO_ROOT = Path(__file__).resolve().parents[2]


def write_reports(
    results: list[CaseResult],
    reports_dir: Path,
    *,
    mode: str,
    backend: str | None = None,
    agent: str | None = None,
    started_at: datetime | None = None,
) -> tuple[Path, Path]:
    summary = summarize(results)
    generated_at = (started_at or datetime.now()).astimezone()
    payload = {
        "generatedAt": generated_at.isoformat(timespec="seconds"),
        "commit": _git_commit(),
        "mode": mode,
        "environment": {"backend": backend, "agent": agent},
        "summary": summary,
        "cases": [result.as_dict() for result in results],
    }

    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "latest.json"
    md_path = reports_dir / "latest.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_markdown(payload), encoding="utf-8")
    return md_path, json_path


def _markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Agent 回归评测报告",
        "",
        f"- 生成时间：{payload['generatedAt']}",
        f"- 代码版本：`{payload.get('commit') or 'unknown'}`",
        f"- 运行模式：`{payload['mode']}`",
    ]
    environment = payload.get("environment") or {}
    if environment.get("backend"):
        lines.append(f"- 后端：`{environment['backend']}`")
    if environment.get("agent"):
        lines.append(f"- Agent：`{environment['agent']}`")
    lines += [
        "",
        "## 总览",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 用例 | {summary['total']} |",
        f"| 通过 / 失败 / 跳过 | {summary['passed']} / {summary['failed']} / {summary['skipped']} |",
        f"| 用例通过率 | {_percent(summary['passRate'])} |",
        f"| 断言 | {summary['assertionPassed']} / {summary['assertionCount']} 通过"
        f"（{_percent(summary['assertionPassRate'])}） |",
        "",
        "## 分类",
        "",
        "| 类别 | 用例 | 通过 | 失败 | 跳过 | 用例通过率 | 断言通过率 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for category, item in summary["byCategory"].items():
        lines.append(
            f"| {item['label']} (`{category}`) | {item['total']} | {item['passed']} | "
            f"{item['failed']} | {item['skipped']} | {_percent(item['passRate'])} | "
            f"{_percent(item['assertionPassRate'])} |"
        )

    failures = [case for case in payload["cases"] if case["status"] == STATUS_FAILED]
    lines += ["", "## 失败用例", ""]
    if not failures:
        lines.append("无。")
    for case in failures:
        lines += [
            f"### `{case['id']}`",
            "",
            f"{case['description']}",
            "",
        ]
        if case.get("error"):
            lines.append(f"- 执行错误：`{case['error']}`")
        for check in case.get("checks", []):
            if check["passed"]:
                continue
            lines.append(
                f"- `{check['path']}`：期望 {check['op']} `{_inline(check['expected'])}`，"
                f"实际 `{_inline(check['actual'])}`"
                + (f"（{check['message']}）" if check.get("message") else "")
            )
        lines.append("")

    skipped = [case for case in payload["cases"] if case["status"] == STATUS_SKIPPED]
    if skipped:
        lines += ["## 跳过用例", ""]
        for case in skipped:
            lines.append(f"- `{case['id']}`：{case.get('error') or '环境未就绪'}")
        lines.append("")

    passed = [case for case in payload["cases"] if case["status"] == STATUS_PASSED]
    if passed:
        lines += ["## 通过用例", ""]
        for case in passed:
            lines.append(f"- `{case['id']}` — {case['description']}")
        lines.append("")

    lines += [
        "## 指标口径",
        "",
        "- **用例通过率**：一条用例全部断言通过才算通过；被跳过的用例不计入分母。",
        "- **断言通过率**：所有断言里通过的比例，用来区分「少数用例挂了很多断言」和"
        "「很多用例各挂一条」。",
        "- 不报 precision / recall：用例的 `expect` 就是人工标注的 gold，"
        "没有第二份独立标注可供计算。",
        "",
    ]
    return "\n".join(lines)


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _inline(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text.replace("`", "'")[:200]


def _git_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return completed.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None
