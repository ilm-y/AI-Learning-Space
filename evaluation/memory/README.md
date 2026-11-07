# Memory 检索离线评测（Phase 4）

独立于 Java 生产代码的召回率评测，对比：

- `KeywordRetriever`：MySQL FULLTEXT（`MATCH ... AGAINST`，ngram 索引）；
- `DenseRetriever`：`text-embedding-v4` + Qdrant（复用 Agent 生产模块）；
- `HybridRetriever`：两路并行 + RRF(k=60) + MySQL 回源复核。

## 文件

| 文件 | 说明 |
| --- | --- |
| `datasets/memory_queries.jsonl` | 30 条查询：10 字面 / 10 同义 / 10 无关 |
| `datasets/memory_fixtures.jsonl` | 34 条记忆：20 目标 + 10 干扰 + 3 跨用户 + 1 归档 |
| `run_eval.py` | 建库建表、灌数据、跑三种策略、算指标、生成报告 |
| `reports/latest.json` | 完整逐查询结果（含未过滤的 Dense 原始分数） |
| `reports/latest.md` | 自动生成的摘要表 |

## 运行

```powershell
cd D:/learningMatiarials/AI-Learning-Space
$env:MYSQL_PASSWORD = "<本机 MySQL 密码>"
python -m evaluation.memory.run_eval --repeats 2
```

Bash：

```bash
cd /d/learningMatiarials/AI-Learning-Space
MYSQL_PASSWORD='<本机 MySQL 密码>' python -m evaluation.memory.run_eval --repeats 2
```

默认使用专用库 `ails_memory_eval` 和专用 Qdrant collection 前缀
`ails_memory_eval`，每次重置；脚本拒绝重置不以前缀开头的库/collection。
`--no-reset` 可保留已有数据。

Embedding / Qdrant 连接沿用 `agent/.env`（`AILS_EMBEDDING_*` / `AILS_QDRANT_*`）。
如果 `AILS_EMBEDDING_PROVIDER=hash`，脚本会拒绝运行，除非显式加
`--allow-hash-embedding`（此时只能做离线冒烟，指标不代表语义模型效果）。

## 主结果（2026-11-07 实跑）

| Retriever | Recall@5 (20) | MRR@5 (20) | 无关空结果率 (10) | P95 延迟 |
| --- | ---: | ---: | ---: | ---: |
| Keyword | 0.500 | 0.500 | 1.000 | 1.6 ms |
| Dense | 1.000 | 0.975 | 1.000 | 633.1 ms |
| Hybrid | 1.000 | 0.950 | 1.000 | 624.1 ms |

Dense 的 τ=0.42 是评测门控；生产 `memory_search_min_score=0.0` 时不会
自动返回空。详细分析、阈值敏感性和案例见
[`docs/audit/MEMORY_RETRIEVAL_EVALUATION.md`](../../docs/audit/MEMORY_RETRIEVAL_EVALUATION.md)。
