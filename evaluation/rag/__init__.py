"""RAG 回答质量与引用可靠性评测。

与仓库根目录的 `evaluation/`（Agent 行为回归）分开：这里评的是**回答质量**，
不是分析链路契约。目录布局：

```text
corpus/            评测用文档（人工编写，事实固定）
learning_state.json 三模式对比用的受控学习状态
dataset.jsonl      固定问题集（A–F 六类，不含标准答案以外的模型输入）
models.py          用例/观测/评分的数据结构
scoring.py         四层评分：检索、上下文、答案事实、引用
runner.py          真实 Spring + Agent + 真实 LLM 的执行器
```

`dataset.jsonl` 是给 runner 和 reviewer 看的，不会整份塞进模型 Prompt；
每轮只把当前问题、检索到的文档片段和受控学习状态发给模型。
"""

from __future__ import annotations

from pathlib import Path

from evaluation import ensure_agent_importable

RAG_EVAL_DIR = Path(__file__).resolve().parent
CORPUS_DIR = RAG_EVAL_DIR / "corpus"
DATASET_PATH = RAG_EVAL_DIR / "dataset.jsonl"
DATASET_V2_PATH = RAG_EVAL_DIR / "dataset_v2.jsonl"
LEARNING_STATE_PATH = RAG_EVAL_DIR / "learning_state.json"

ensure_agent_importable()
