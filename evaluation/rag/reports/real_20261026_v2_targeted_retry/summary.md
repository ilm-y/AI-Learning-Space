# Phase 4.3 RAG 评测结果（v2 规则）

- 开始时间：2026-10-26T14:06:17.972021+00:00
- 后端：`http://127.0.0.1:18080`
- 用户：`8`，主空间：`13`，空空间：`14`
- LLM：`openai` / `qwen-plus`（temperature=0.0）
- Embedding：`text-embedding-v4` / 1024 维
- Qdrant collection：`ails_phase42_real_e2e_text-embedding-v4_1024`
- 检索基线：top_k=5，min_score=0.35，max_context_tokens=3000
- 文档：7 个，ready ids=[43, 44, 45, 46, 47, 48, 49]
- 规则版本：v2（v1 数据集与 4.2 报告保留不变）

## 按模式汇总

| 模式 | 运行 | 自动通过 | 检索通过 | 上下文通过 | 答案通过 | 引用通过 | 平均延迟(ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| rag | 2 | 0 | 100.0% | 100.0% | 0.0% | 100.0% | 22079.5 |
| rag+state | 2 | 0 | 100.0% | 100.0% | 0.0% | 100.0% | 20429.5 |

> `citation_pass_rate` 的分母是运行数；下面六个指标的分母各不相同，必须按各自的 numerator/denominator 阅读，不能横向比较百分比。
> 来源支持度是词面启发式线索，只用于人工复核，不是事实准确率。

## 引用质量指标（分子 / 分母）

| 模式 | 非法标签率 | 格式错误率 | 缺必需来源率 | 禁止来源率 | 额外合法来源率 | 支持警告率 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| rag | N/A (0/0) | N/A (0/0) | N/A (0/0) | N/A (0/0) | N/A (0/0) | N/A (0/0) |
| rag+state | N/A (0/0) | N/A (0/0) | N/A (0/0) | N/A (0/0) | N/A (0/0) | N/A (0/0) |

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
| rag | D | 1 | 0 | 0.0% | 100.0% |
| rag | F | 1 | 0 | 0.0% | 100.0% |
| rag+state | D | 1 | 0 | 0.0% | 100.0% |
| rag+state | F | 1 | 0 | 0.0% | 100.0% |

## 自动评分未通过

- `D01` / `rag`：层=['answer']；检查=['admission_evidence']
- `D01` / `rag+state`：层=['answer']；检查=['admission_evidence']
- `F02` / `rag`：层=['answer']；检查=['admission_evidence']
- `F02` / `rag+state`：层=['answer']；检查=['admission_evidence']

## 原始记录

- `runs.jsonl`：每条用例 × 模式一行，含检索状态、最终上下文、模型原文、引用校验逐条结果、v2 四层评分与 trace。
- `summary.json`：机器可读汇总，含每个指标的 numerator / denominator。
