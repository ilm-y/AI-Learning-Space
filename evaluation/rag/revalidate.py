"""离线重算已保存的模型输出，不重新调用模型。

用途：4.3 先证明 v1 → v2 的差异来自评分规则，而不是模型变好。
输入是 `reports/.../runs.jsonl`（含模型原文、本轮实际进入上下文的标签），
输出是一份新的 v2 报告目录；源报告不会被修改。

用法：

```bash
python -m evaluation.rag.revalidate \
  --source evaluation/rag/reports/real_20261026_full_revalidated \
  --target evaluation/rag/reports/real_20261026_v2_offline
```

引用校验会按当前 `app.citations` 重新执行一次；如果只是规则变化，结果应与源记录
的引用逐条结果一致。若不一致，报告里的 citation 字段以本次重算为准并在 stdout 提示。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.citations import validate_citations
from app.schemas import KnowledgeChunk

from .models import load_cases
from .runner import _markdown_summary_v2, _record_to_score
from .scoring_v2 import aggregate_v2

EVAL_ROOT = Path(__file__).resolve().parent


def revalidate(source: Path, target: Path, dataset: Path) -> int:
    if not (source / "runs.jsonl").exists():
        raise SystemExit(f"源报告缺少 runs.jsonl：{source}")
    cases = {case.id: case for case in load_cases(dataset)}
    records = [
        json.loads(line)
        for line in (source / "runs.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    scores = []
    citation_changed = 0
    for record in records:
        case_id = record["case"]["id"]
        case = cases.get(case_id)
        if case is None:
            raise SystemExit(f"数据集 {dataset.name} 缺少用例 {case_id}")
        # 规则重算后的报告应带当前规则快照，否则同一份 runs.jsonl 里
        # 「评分用的规则」和「case 字段」可能来自不同版本，复核时容易误读。
        record["case"] = case.as_dict()
        chunks = [
            KnowledgeChunk(
                id=item["chunkId"],
                document_id=item["documentId"],
                chunk_index=item["chunkIndex"],
                document_name=item["documentName"],
                heading_path=list(item.get("headingPath", [])),
                content=item.get("content", ""),
                score=item.get("score"),
            )
            for item in record["retrieval"].get("chunks", [])
        ]
        previous = record.get("citation", {})
        report = validate_citations(
            record["answer"].get("text", ""),
            chunks,
            provided_labels=record["context"].get("providedLabels", []),
        )
        current = report.as_dict()
        for key in ("total", "valid", "invalid", "invalidLabels", "malformedLabels"):
            if previous.get(key) != current.get(key):
                citation_changed += 1
                break
        record["citation"] = current
        score = _record_to_score(record, case, rules_version="v2")
        record["score"] = score.as_dict()
        scores.append(score)

    metadata = json.loads((source / "summary.json").read_text(encoding="utf-8"))["metadata"]
    metadata["rulesVersion"] = "v2"
    metadata["revalidatedFrom"] = str(source)
    metadata["revalidatedAt"] = _timestamp()
    summary = aggregate_v2(scores)

    target.mkdir(parents=True, exist_ok=True)
    with (target / "runs.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    (target / "summary.json").write_text(
        json.dumps({"metadata": metadata, "aggregate": summary}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (target / "summary.md").write_text(
        _markdown_summary_v2(metadata, summary, records, scores), encoding="utf-8"
    )

    print(f"revalidated {len(records)} records -> {target}")
    print(f"citation records changed by revalidation: {citation_changed}")
    for mode, data in summary["modes"].items():
        print(
            mode,
            {
                "runs": data["runs"],
                "autoPass": data["autoPass"],
                "failed": data["failed"],
                "answerPassRate": data["answerPassRate"],
                "citationPassRate": data["citationPassRate"],
                "metrics": {
                    key: {
                        "rate": value["rate"],
                        "n": value["numerator"],
                        "d": value["denominator"],
                    }
                    for key, value in data["metrics"].items()
                },
            },
        )
    return 0


def _timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline v2 re-scoring of saved RAG answers")
    parser.add_argument("--source", required=True, help="源报告目录（含 runs.jsonl）")
    parser.add_argument("--target", required=True, help="v2 输出目录")
    parser.add_argument(
        "--dataset",
        default=str(EVAL_ROOT / "dataset_v2.jsonl"),
        help="v2 数据集路径",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return revalidate(Path(args.source), Path(args.target), Path(args.dataset))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
