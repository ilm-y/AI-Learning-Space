# Phase 4.3 RAG 评测结果（v2 规则）

- 开始时间：2026-10-26T14:11:09.283535+00:00
- 后端：`http://127.0.0.1:18080`
- 用户：`10`，主空间：`16`，空空间：`17`
- LLM：`openai` / `qwen-plus`（temperature=0.0）
- Embedding：`text-embedding-v4` / 1024 维
- Qdrant collection：`ails_phase42_real_e2e_text-embedding-v4_1024`
- 检索基线：top_k=5，min_score=0.35，max_context_tokens=3000
- 文档：7 个，ready ids=[57, 58, 59, 60, 61, 62, 63]
- 规则版本：v2（v1 数据集与 4.2 报告保留不变）

## 按模式汇总

| 模式 | 运行 | 自动通过 | 检索通过 | 上下文通过 | 答案通过 | 引用通过 | 平均延迟(ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| plain | 37 | 15 | N/A | 100.0% | 30.0% | 89.2% | 13718.1 |
| rag | 40 | 32 | 100.0% | 100.0% | 90.0% | 85.0% | 9074.6 |
| rag+state | 41 | 33 | 100.0% | 100.0% | 82.9% | 90.2% | 9827.4 |

> `citation_pass_rate` 的分母是运行数；下面六个指标的分母各不相同，必须按各自的 numerator/denominator 阅读，不能横向比较百分比。
> 来源支持度是词面启发式线索，只用于人工复核，不是事实准确率。

## 引用质量指标（分子 / 分母）

| 模式 | 非法标签率 | 格式错误率 | 缺必需来源率 | 禁止来源率 | 额外合法来源率 | 支持警告率 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| plain | 100.0% (7/7) | 0.0% (0/7) | N/A (0/0) | 0.0% (0/1) | N/A (0/0) | N/A (0/0) |
| rag | 0.0% (0/57) | 3.4% (2/59) | 10.0% (3/30) | 0.0% (0/1) | 10.3% (3/29) | 7.0% (4/57) |
| rag+state | 0.0% (0/75) | 1.3% (1/76) | 3.2% (1/31) | 100.0% (1/1) | 18.2% (6/33) | 20.0% (15/75) |

## 指标定义

- `illegal_label_rate`：非法精确标签 / 精确标签总数。
- `malformed_label_rate`：格式错误标签 / 全部标签出现次数（精确 + 格式错误）。
- `required_source_missing_rate`：缺必需来源的运行 / 有必需来源要求的运行。
- `forbidden_source_rate`：引用了禁止来源的运行 / 配置了禁止来源的运行。
- `extra_valid_source_rate`：含额外合法来源的运行 / 至少有一条合法引用的运行。
- `citation_support_warning_rate`：来源支持度为 none/numeric_conflict 的引用 / 合法引用数。

## 按类别 × 模式

| 模式 | 类别 | 运行 | 自动通过 | 答案通过 | 引用通过 |
| --- | --- | ---: | ---: | ---: | ---: |
| plain | A | 12 | 4 | 33.3% | 75.0% |
| plain | B | 8 | 0 | 0.0% | 100.0% |
| plain | C | 6 | 3 | 50.0% | 100.0% |
| plain | D | 6 | 5 | N/A | 83.3% |
| plain | E | 4 | 2 | 50.0% | 100.0% |
| plain | F | 1 | 1 | N/A | 100.0% |
| rag | A | 12 | 10 | 100.0% | 83.3% |
| rag | B | 8 | 8 | 100.0% | 100.0% |
| rag | C | 6 | 4 | 83.3% | 66.7% |
| rag | D | 6 | 3 | 66.7% | 66.7% |
| rag | E | 4 | 4 | 100.0% | 100.0% |
| rag | F | 4 | 3 | 75.0% | 100.0% |
| rag+state | A | 12 | 12 | 100.0% | 100.0% |
| rag+state | B | 8 | 8 | 100.0% | 100.0% |
| rag+state | C | 6 | 3 | 66.7% | 66.7% |
| rag+state | D | 6 | 3 | 50.0% | 66.7% |
| rag+state | E | 5 | 5 | 100.0% | 100.0% |
| rag+state | F | 4 | 2 | 50.0% | 100.0% |

## 需人工复核

- `E05` / `rag+state`：自动评分=通过。专门测试 Memory 中的历史判断不能被当成文档原文；自动词面检查之外需人工复核。

## 自动评分未通过

- `A02` / `plain`：层=['answer']；检查=['required_terms', 'required_bindings', 'required_documents_cited']
- `A03` / `plain`：层=['answer', 'citation']；检查=['required_numbers', 'citation_expectation', 'no_illegal_labels', 'required_documents_cited']
- `A04` / `plain`：层=['answer']；检查=['required_terms', 'required_bindings', 'required_documents_cited']
- `A05` / `plain`：层=['answer', 'citation']；检查=['required_terms', 'required_bindings', 'citation_expectation', 'no_illegal_labels', 'required_documents_cited']
- `A06` / `plain`：层=['answer']；检查=['required_bindings', 'required_numbers', 'required_documents_cited']
- `A06` / `rag`：层=['citation']；检查=['citation_expectation', 'no_malformed_labels']
- `A07` / `plain`：层=['answer', 'citation']；检查=['required_bindings', 'required_numbers', 'citation_expectation', 'no_illegal_labels', 'required_documents_cited']
- `A08` / `plain`：层=['answer']；检查=['required_bindings', 'required_numbers', 'required_documents_cited']
- `A09` / `plain`：层=['answer']；检查=['required_numbers', 'required_documents_cited']
- `A10` / `rag`：层=['citation']；检查=['citation_expectation', 'required_documents_cited']
- `B01` / `plain`：层=['answer']；检查=['required_terms', 'required_bindings', 'required_documents_cited']
- `B02` / `plain`：层=['answer']；检查=['required_terms', 'required_bindings', 'required_documents_cited']
- `B03` / `plain`：层=['answer']；检查=['required_terms', 'required_bindings', 'required_documents_cited']
- `B04` / `plain`：层=['answer']；检查=['required_bindings', 'required_numbers', 'required_documents_cited']
- `B05` / `plain`：层=['answer']；检查=['required_terms', 'required_bindings', 'required_numbers', 'required_documents_cited']
- `B06` / `plain`：层=['answer']；检查=['required_bindings', 'required_numbers', 'required_documents_cited']
- `B07` / `plain`：层=['answer']；检查=['required_terms', 'required_bindings', 'required_numbers', 'required_documents_cited']
- `B08` / `plain`：层=['answer']；检查=['required_numbers', 'required_documents_cited']
- `C01` / `plain`：层=['answer']；检查=['required_terms', 'required_documents_cited']
- `C01` / `rag`：层=['citation']；检查=['citation_expectation', 'required_documents_cited']
- `C01` / `rag+state`：层=['citation']；检查=['citation_expectation', 'required_documents_cited']
- `C02` / `rag`：层=['answer', 'citation']；检查=['forbidden_patterns', 'citation_expectation', 'required_documents_cited']
- `C02` / `rag+state`：层=['answer', 'citation']；检查=['forbidden_patterns', 'citation_expectation', 'no_malformed_labels']
- `C03` / `plain`：层=['answer']；检查=['required_terms', 'required_documents_cited']
- `C05` / `plain`：层=['answer']；检查=['required_terms', 'required_numbers', 'required_phrases_any', 'required_documents_cited']
- `C05` / `rag+state`：层=['answer']；检查=['required_numbers']
- `D01` / `rag`：层=['answer']；检查=['admission_evidence']
- `D01` / `rag+state`：层=['answer']；检查=['admission_evidence']
- `D02` / `rag`：层=['answer', 'citation']；检查=['admission_evidence', 'citation_expectation']
- `D02` / `rag+state`：层=['answer', 'citation']；检查=['admission_evidence', 'citation_expectation']
- `D05` / `plain`：层=['citation']；检查=['citation_expectation', 'no_illegal_labels']
- `D06` / `rag`：层=['citation']；检查=['citation_expectation', 'no_malformed_labels']
- `D06` / `rag+state`：层=['answer', 'citation']；检查=['admission_evidence', 'citation_expectation', 'forbidden_documents_not_cited']
- `E01` / `plain`：层=['answer']；检查=['required_bindings', 'required_numbers', 'required_phrases_any', 'required_documents_cited']
- `E02` / `plain`：层=['answer']；检查=['required_bindings', 'required_numbers', 'required_phrases_any', 'required_documents_cited']
- `F02` / `rag`：层=['answer']；检查=['admission_evidence']
- `F02` / `rag+state`：层=['answer']；检查=['admission_evidence']
- `F04` / `rag+state`：层=['answer']；检查=['admission_evidence']

## 原始记录

- `runs.jsonl`：每条用例 × 模式一行，含检索状态、最终上下文、模型原文、引用校验逐条结果、v2 四层评分与 trace。
- `summary.json`：机器可读汇总，含每个指标的 numerator / denominator。
