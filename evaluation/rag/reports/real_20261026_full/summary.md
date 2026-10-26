# Phase 4.2 RAG 评测结果

- 开始时间：2026-10-26T13:13:16.677993+00:00
- 后端：`http://127.0.0.1:18080`
- 用户：`4`，主空间：`7`，空空间：`8`
- LLM：`openai` / `qwen-plus`（temperature=0.0）
- Embedding：`text-embedding-v4` / 1024 维
- Qdrant collection：`ails_phase42_real_e2e_text-embedding-v4_1024`
- 检索基线：top_k=5，min_score=0.35，max_context_tokens=3000
- 文档：7 个，ready ids=[22, 23, 24, 25, 26, 27, 28]

## 按模式汇总

| 模式 | 运行 | 自动通过 | 检索通过 | 上下文通过 | 答案通过 | 引用通过 | 非法引用率 | 来源支持不足率 | 平均延迟(ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| plain | 37 | 10 | N/A | 100.0% | 20.0% | 70.3% | 100.0% | 0.0% | 14470.4 |
| rag | 40 | 31 | 100.0% | 100.0% | 85.0% | 90.0% | 0.0% | 16.4% | 8535.5 |
| rag+state | 41 | 23 | 100.0% | 100.0% | 80.5% | 61.0% | 2.7% | 27.0% | 9728.2 |

> 检索/上下文/答案/引用是四层独立指标；引用通过只表示标签合法且必需来源被引用，“来源支持不足率”是词面启发式信号，不是事实正确性证明。

## 按类别 × 模式

| 模式 | 类别 | 运行 | 自动通过 | 答案通过 | 引用通过 | 非法引用率 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| plain | A | 12 | 2 | 25.0% | 41.7% | 100.0% |
| plain | B | 8 | 0 | 0.0% | 75.0% | 100.0% |
| plain | C | 6 | 1 | 16.7% | 100.0% | N/A |
| plain | D | 6 | 5 | N/A | 83.3% | 100.0% |
| plain | E | 4 | 1 | 50.0% | 75.0% | N/A |
| plain | F | 1 | 1 | N/A | 100.0% | N/A |
| rag | A | 12 | 10 | 91.7% | 91.7% | 0.0% |
| rag | B | 8 | 8 | 100.0% | 100.0% | 0.0% |
| rag | C | 6 | 6 | 100.0% | 100.0% | 0.0% |
| rag | D | 6 | 2 | 66.7% | 66.7% | 0.0% |
| rag | E | 4 | 4 | 100.0% | 100.0% | 0.0% |
| rag | F | 4 | 1 | 25.0% | 75.0% | N/A |
| rag+state | A | 12 | 6 | 91.7% | 58.3% | 0.0% |
| rag+state | B | 8 | 7 | 100.0% | 87.5% | 0.0% |
| rag+state | C | 6 | 4 | 100.0% | 66.7% | 0.0% |
| rag+state | D | 6 | 2 | 50.0% | 33.3% | 0.0% |
| rag+state | E | 5 | 4 | 80.0% | 80.0% | 0.0% |
| rag+state | F | 4 | 0 | 25.0% | 25.0% | 100.0% |

## 需人工复核

- `E05` / `rag+state`：自动评分=未通过。专门测试 Memory 中的历史判断不能被当成文档原文；自动词面检查之外需人工复核。

## 自动评分未通过

- `A01` / `plain`：层=['citation']；检查=['citation_expectation', 'no_malformed_labels', 'required_documents_cited']
- `A01` / `rag+state`：层=['citation']；检查=['citation_expectation', 'cited_documents_allowed']
- `A02` / `plain`：层=['answer']；检查=['required_terms', 'required_bindings', 'required_documents_cited']
- `A02` / `rag+state`：层=['citation']；检查=['citation_expectation', 'cited_documents_allowed']
- `A03` / `plain`：层=['answer', 'citation']；检查=['required_numbers', 'citation_expectation', 'no_illegal_labels', 'required_documents_cited', 'cited_documents_allowed']
- `A04` / `plain`：层=['answer', 'citation']；检查=['required_terms', 'required_bindings', 'citation_expectation', 'no_illegal_labels', 'required_documents_cited', 'cited_documents_allowed']
- `A04` / `rag`：层=['citation']；检查=['citation_expectation', 'cited_documents_allowed']
- `A04` / `rag+state`：层=['citation']；检查=['citation_expectation', 'cited_documents_allowed']
- `A05` / `plain`：层=['answer']；检查=['required_terms', 'required_bindings', 'required_documents_cited']
- `A05` / `rag+state`：层=['citation']；检查=['citation_expectation', 'cited_documents_allowed']
- `A06` / `plain`：层=['answer', 'citation']；检查=['required_bindings', 'required_numbers', 'citation_expectation', 'no_illegal_labels', 'required_documents_cited', 'cited_documents_allowed']
- `A06` / `rag+state`：层=['citation']；检查=['citation_expectation', 'cited_documents_allowed']
- `A07` / `plain`：层=['answer', 'citation']；检查=['required_bindings', 'required_numbers', 'citation_expectation', 'no_malformed_labels', 'required_documents_cited']
- `A08` / `plain`：层=['answer', 'citation']；检查=['required_bindings', 'required_numbers', 'citation_expectation', 'no_malformed_labels', 'required_documents_cited']
- `A09` / `plain`：层=['answer', 'citation']；检查=['required_terms', 'required_bindings', 'required_numbers', 'citation_expectation', 'no_illegal_labels', 'required_documents_cited', 'cited_documents_allowed']
- `A12` / `plain`：层=['answer']；检查=['required_bindings', 'required_documents_cited']
- `A12` / `rag`：层=['answer']；检查=['required_bindings']
- `A12` / `rag+state`：层=['answer']；检查=['required_bindings']
- `B01` / `plain`：层=['answer']；检查=['required_terms', 'required_bindings', 'required_documents_cited']
- `B02` / `plain`：层=['answer', 'citation']；检查=['required_terms', 'required_bindings', 'citation_expectation', 'no_illegal_labels', 'required_documents_cited', 'cited_documents_allowed']
- `B03` / `plain`：层=['answer']；检查=['required_terms', 'required_bindings', 'required_documents_cited']
- `B04` / `plain`：层=['answer']；检查=['required_bindings', 'required_numbers', 'required_documents_cited']
- `B05` / `plain`：层=['answer']；检查=['required_terms', 'required_bindings', 'required_numbers', 'required_documents_cited']
- `B05` / `rag+state`：层=['citation']；检查=['citation_expectation', 'cited_documents_allowed']
- `B06` / `plain`：层=['answer']；检查=['required_terms', 'required_bindings', 'required_numbers', 'required_documents_cited']
- `B07` / `plain`：层=['answer']；检查=['required_terms', 'required_bindings', 'required_documents_cited']
- `B08` / `plain`：层=['answer', 'citation']；检查=['required_numbers', 'citation_expectation', 'no_illegal_labels', 'required_documents_cited', 'cited_documents_allowed']
- `C01` / `plain`：层=['answer']；检查=['required_terms', 'required_documents_cited']
- `C02` / `plain`：层=['answer']；检查=['required_bindings', 'required_documents_cited']
- `C02` / `rag+state`：层=['citation']；检查=['citation_expectation', 'no_malformed_labels', 'required_documents_cited']
- `C03` / `plain`：层=['answer']；检查=['required_terms', 'required_documents_cited']
- `C04` / `plain`：层=['answer']；检查=['required_terms', 'required_documents_cited']
- `C05` / `plain`：层=['answer']；检查=['required_terms', 'required_numbers', 'required_phrases_any', 'required_documents_cited']
- `C06` / `rag+state`：层=['citation']；检查=['citation_expectation', 'required_documents_cited']
- `D01` / `rag`：层=['answer']；检查=['admission_terms_any']
- `D01` / `rag+state`：层=['answer', 'citation']；检查=['admission_terms_any', 'citation_expectation', 'no_malformed_labels']
- `D04` / `rag`：层=['answer']；检查=['admission_terms_any']
- `D04` / `rag+state`：层=['answer', 'citation']；检查=['admission_terms_any', 'citation_expectation', 'no_malformed_labels']
- `D05` / `plain`：层=['citation']；检查=['citation_expectation', 'no_illegal_labels']
- `D05` / `rag`：层=['citation']；检查=['citation_expectation', 'no_malformed_labels']
- `D05` / `rag+state`：层=['answer', 'citation']；检查=['admission_terms_any', 'citation_expectation', 'no_malformed_labels']
- `D06` / `rag`：层=['citation']；检查=['citation_expectation', 'no_malformed_labels']
- `D06` / `rag+state`：层=['citation']；检查=['citation_expectation', 'forbidden_documents_not_cited', 'cited_documents_allowed']
- `E01` / `plain`：层=['answer']；检查=['required_bindings', 'required_numbers', 'required_phrases_any', 'required_documents_cited']
- `E02` / `plain`：层=['answer']；检查=['required_bindings', 'required_numbers', 'required_documents_cited']
- `E03` / `plain`：层=['citation']；检查=['citation_expectation', 'no_malformed_labels', 'required_documents_cited']
- `F01` / `rag`：层=['answer']；检查=['admission_terms_any']
- `F01` / `rag+state`：层=['answer', 'citation']；检查=['admission_terms_any', 'citation_expectation', 'no_illegal_labels']
- `F02` / `rag`：层=['answer']；检查=['admission_terms_any']
- `F02` / `rag+state`：层=['answer']；检查=['admission_terms_any']
- `F03` / `rag`：层=['answer', 'citation']；检查=['admission_terms_any', 'citation_expectation', 'no_malformed_labels']
- `F03` / `rag+state`：层=['answer', 'citation']；检查=['admission_terms_any', 'citation_expectation', 'no_malformed_labels']
- `F04` / `rag+state`：层=['citation']；检查=['citation_expectation', 'no_illegal_labels']

## 原始记录

- `runs.jsonl`：每条用例 × 模式一行，含检索状态、最终上下文、模型原文、引用校验逐条结果、四层评分与 trace。
- `summary.json`：机器可读汇总。
