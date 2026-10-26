# Phase 4.4 基线核对：29/40 与 31/40

本报告只核对数字来源，不重新定义模型质量，也不覆盖任何历史报告。

## 结论

`real_20261026_full` 的 31/40 与 `real_20261026_full_revalidated` 的 29/40
**来自同一批模型输出**，差异全部来自引用校验器对裸标签 `[#n]` 的识别变化：

- 两次报告的 118 行答案逐行完全相同；
- 31/40 是 4.2 运行当时、由旧校验器写下的引用结果；
- 29/40 是用当前 `app.citations` 对同一批答案重算的结果，可以复现；
- 因此 **29/40 不是模型变差，31/40 也不代表当前代码下的可复现结果**。
  在查明之前，不能用这两个数字计算模型质量提升。

## 证据

### 1. 两次报告是同一批输出

| 项目 | `real_20261026_full` | `real_20261026_full_revalidated` |
| --- | --- | --- |
| `startedAt` | 2026-10-26T13:13:16Z | 同左 |
| 行数 | 118 | 118 |
| 用例 × 模式键集合 | 完全相同 | 完全相同 |
| 逐行答案文本 | 完全相同 | 完全相同 |

答案指纹（`sha256("\n".join(sorted(case|mode|answer)))`）：

```text
9fac8dea8af8e68760077150e140ae8010024af4fd5eebb94a02c37320f7e285
```

两次报告相同。`revalidated` 没有重新调用模型，只重跑了引用校验与评分。

### 2. 差异集中在三条 rag 用例

| 用例 | 模式 | 自动通过 | 引用层 | 新检出格式错误标签 |
| --- | --- | --- | --- | --- |
| A12 | rag | 失败 → 失败 | 通过 → 失败 | `[#1]` |
| C01 | rag | 通过 → 失败 | 通过 → 失败 | `[#5]` |
| C05 | rag | 通过 → 失败 | 通过 → 失败 | `[#5]`、`[#4]` |

- A12 在答案层本来就失败，所以引用变化没有改变自动通过；
- C01、C05 的答案与精确引用都合法，只是回答正文里还出现了裸标签 `[#n]`；
- 旧校验器把这些裸标签当成普通文本忽略，当前校验器把它们计为
  `malformed_label`，按 v1/v2 规则都导致引用层失败。

精确引用本身没有变化：例如 C01 的 5 条
`[资料《helios-compute.md》#n]` 在两次报告中都是 `valid=True`。

### 3. 汇总对比

| 模式 | 4.2 当时（旧校验器） | 当前校验器重算 | 差异原因 |
| --- | ---: | ---: | --- |
| plain | 10/37 | 10/37 | 无变化 |
| rag | **31/40** | **29/40** | C01、C05 新检出裸标签 |
| rag+state | 23/41 | 23/41 | 自动通过无变化 |

引用通过数：rag 从 36/40 降到 33/40；答案通过数保持 34/40。
这说明差异只发生在引用层，不在检索层、上下文层或答案层。

### 4. 和 v2 基线的关系

Phase 4.3 的 v2 离线重算使用相同 118 行答案，但规则版本不同：

| 报告 | 规则 | rag | rag+state |
| --- | --- | ---: | ---: |
| `real_20261026_full_revalidated` | v1 | 29/40 | 23/41 |
| `real_20261026_v2_offline` | v2 | 30/40 | 31/41 |

v2 的 30/40 与 31/41 是 Phase 4.3/4.4 使用的基线；它们与 v1 的 29/31
不能直接比较，因为 `requiredTermGroups`、`admissionPatterns` 与引用分类规则不同。

## 复现

```bash
python - <<'PY'
import hashlib, json

def digest(name):
    rows = [json.loads(line) for line in open(
        f"evaluation/rag/reports/{name}/runs.jsonl", encoding="utf-8") if line.strip()]
    text = "\n".join(sorted(
        f"{r['case']['id']}|{r['mode']}|{r['answer']['text']}" for r in rows))
    return len(rows), hashlib.sha256(text.encode()).hexdigest()

for name in ("real_20261026_full", "real_20261026_full_revalidated"):
    print(name, digest(name))
PY
```

```bash
# 用当前校验器重算 4.2 的答案（v1 规则），不覆盖源报告
python - <<'PY'
import json
from pathlib import Path

import evaluation.rag  # noqa: F401 —— 让 agent/ 可导入

from app.citations import validate_citations
from app.schemas import KnowledgeChunk
from evaluation.rag.models import load_cases
from evaluation.rag.runner import _record_to_score
from evaluation.rag.scoring import aggregate

source = Path("evaluation/rag/reports/real_20261026_full")
cases = {c.id: c for c in load_cases(Path("evaluation/rag/dataset.jsonl"))}
records = [json.loads(line) for line in
           (source / "runs.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
scores = []
for record in records:
    chunks = [
        KnowledgeChunk(
            id=item["chunkId"], document_id=item["documentId"],
            chunk_index=item["chunkIndex"], document_name=item["documentName"],
            heading_path=list(item.get("headingPath", [])),
            content=item.get("content", ""), score=item.get("score"),
        )
        for item in record["retrieval"].get("chunks", [])
    ]
    record["citation"] = validate_citations(
        record["answer"].get("text", ""), chunks,
        provided_labels=record["context"].get("providedLabels", []),
    ).as_dict()
    scores.append(_record_to_score(record, cases[record["case"]["id"]]))

for mode, data in aggregate(scores)["modes"].items():
    print(mode, data["autoPass"], "/", data["runs"], "citation=", data["citationPassed"])
PY
```

输出应为 `rag 29 / 40`，而不是 31/40。若需要历史 31/40 的口径，应明确标注
“4.2 运行当时的校验器版本”，不能与当前结果混用。

`python -m evaluation.rag.revalidate` 也能重算同一批答案，但它固定使用 v2 规则，
产出的是 `rag 30/40`，不能拿来复现 v1 的 29/40。
