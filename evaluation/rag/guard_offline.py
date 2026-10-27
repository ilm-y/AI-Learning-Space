"""离线估算运行时兜底对已保存回答的影响，不调用任何模型。

Phase 4.4 的兜底在 `app.evidence_guard` 与 `AgentRuntime.generate` 里：当知识检索
状态属于 `NO_VALID_DOCUMENTS` / `NO_MATCH` / `THRESHOLD_FILTERED` / `DEGRADED`，
本轮没有知识片段，且模型自己没说明时，在回答末尾追加一条固定说明。

本脚本把同一条规则套到**已经保存的模型原文**上，再用 v2 规则重新评分，用来回答
两个问题：

1. 兜底能修好哪些用例的答案层；
2. 哪些用例即使答案层修好，仍会因为引用层失败而不通过。

它不替代真实调用：模型在新一轮里可能给出不同回答，引用也可能不同。真实结果以
`runner.py` 产出的报告为准。源报告只读，不覆盖。

```bash
python -m evaluation.rag.guard_offline \
  --source evaluation/rag/reports/real_20261026_v2_offline \
  --target evaluation/rag/reports/real_20261026_phase44_guard_offline
```
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.evidence_guard import GUARD_STATUSES, evidence_status_notice

from . import DATASET_V2_PATH
from .models import load_cases
from .runner import _record_to_score
from .scoring_v2 import aggregate_v2

SECTIONS = ("retrieval", "context", "answer", "citation")


def _guard_notice(record: dict[str, Any]) -> str | None:
    """按 Runtime 的同一组条件判断这行记录会不会被追加说明。"""
    retrieval = record["retrieval"]
    if retrieval.get("status") not in GUARD_STATUSES:
        return None
    if retrieval.get("chunks"):
        return None
    return evidence_status_notice(
        retrieval["status"],
        record["case"]["question"],
        record["answer"].get("text", ""),
    )


def _layer_summary(score) -> dict[str, Any]:
    return {
        name: {
            "applicable": getattr(score, name).applicable,
            "passed": getattr(score, name).passed,
            "failedChecks": [
                check.name
                for check in getattr(score, name).checks
                if check.passed is False
            ],
        }
        for name in SECTIONS
    }


def simulate(source: Path, target: Path, dataset: Path) -> int:
    runs_path = source / "runs.jsonl"
    if not runs_path.exists():
        raise SystemExit(f"源报告缺少 runs.jsonl：{source}")

    cases = {case.id: case for case in load_cases(dataset)}
    records = [
        json.loads(line)
        for line in runs_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    rows: list[dict[str, Any]] = []
    before_scores = []
    after_scores = []
    for record in records:
        case_id = record["case"]["id"]
        case = cases.get(case_id)
        if case is None:
            raise SystemExit(f"数据集 {dataset.name} 缺少用例 {case_id}")

        before = _record_to_score(record, case, rules_version="v2")
        before_scores.append(before)

        notice = _guard_notice(record)
        patched = json.loads(json.dumps(record))
        if notice:
            patched["answer"]["text"] = (
                record["answer"].get("text", "") + "\n\n" + notice
            )
        after = _record_to_score(patched, case, rules_version="v2")
        after_scores.append(after)

        rows.append(
            {
                "caseId": case_id,
                "mode": record["mode"],
                "knowledgeStatus": record["retrieval"].get("status"),
                "noticeAdded": bool(notice),
                "notice": notice,
                "before": {"status": before.status, **_layer_summary(before)},
                "after": {"status": after.status, **_layer_summary(after)},
                "flipped": before.auto_pass != after.auto_pass,
            }
        )

    before_agg = aggregate_v2(before_scores)
    after_agg = aggregate_v2(after_scores)
    summary = {
        "source": str(source),
        "dataset": str(dataset),
        "note": "离线模拟：只把兜底说明套到已保存回答上，不调用模型。",
        "before": {m: {"autoPass": v["autoPass"], "runs": v["runs"]}
                   for m, v in before_agg["modes"].items()},
        "after": {m: {"autoPass": v["autoPass"], "runs": v["runs"]}
                  for m, v in after_agg["modes"].items()},
        "changes": [row for row in rows if row["flipped"]],
    }

    target.mkdir(parents=True, exist_ok=True)
    (target / "guard_offline.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"源报告：{source}")
    print(f"输出：{target / 'guard_offline.json'}")
    for label, agg in (("模拟前", before_agg), ("模拟后", after_agg)):
        modes = ", ".join(
            f"{mode}={item['autoPass']}/{item['runs']}"
            for mode, item in agg["modes"].items()
        )
        print(f"{label}自动通过：{modes}")
    for row in summary["changes"]:
        print(
            f"  {row['caseId']}/{row['mode']} "
            f"{row['before']['status']} -> {row['after']['status']}"
        )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="离线模拟非成功检索状态兜底（不调用模型）"
    )
    parser.add_argument(
        "--source",
        default="evaluation/rag/reports/real_20261026_v2_offline",
        help="含 runs.jsonl 的报告目录（只读）",
    )
    parser.add_argument(
        "--target",
        default="evaluation/rag/reports/real_20261026_phase44_guard_offline",
        help="输出目录",
    )
    parser.add_argument(
        "--dataset", default=str(DATASET_V2_PATH), help="v2 数据集路径"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return simulate(Path(args.source), Path(args.target), Path(args.dataset))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
