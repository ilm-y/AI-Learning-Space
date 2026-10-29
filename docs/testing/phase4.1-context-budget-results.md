# Phase 4.1 F1：检索状态与预算后最终状态

- 日期：2026-10-28
- 范围：只修复 Phase 4 的 `CONTEXT_BUDGET` 缺口，不改检索阈值、不换模型、
  不调整 Guard 的既有状态集合语义
- 结论：结构修复与确定性回归已完成；受控真实模型样本能明确说出
  「检索到了，但预算没放进去」；**这不等于在线 RAG 或语义质量已验证**

## 1. 修复前证据

Phase 4 的预算压力 preview：

- 检索层状态：`SUCCESS`，找到 2 个知识片段；
- 动态上下文约 2836 / 3000 tokens；
- 最终注入 0 个知识片段，`knowledge:18`、`knowledge:19` 都在
  `dropped_sections`；
- system instruction 中没有任何「已检索到但被预算丢弃」的说明；
- 模型只能看到「没有知识片段」，无法区分「没检索到」和「没放下」。

证据：[phase4-rag-agent-results.md](phase4-rag-agent-results.md) §2.3、
[failure-analysis.md](failure-analysis.md) 的 Phase 4.1 记录。

## 2. 状态设计

检索层状态与预算后的最终状态分开保存：

| 字段 | 含义 | 来源 |
| --- | --- | --- |
| `knowledge_status` | 检索层原始状态，例如 `SUCCESS` / `NO_MATCH` / `THRESHOLD_FILTERED` | Retriever |
| `knowledge_state` | 预算裁剪后模型实际看到的知识状态 | Context Builder |
| `knowledge_injected_chunk_ids` | 最终进入 Prompt 的知识 chunk | Context Builder |
| `knowledge_dropped_chunk_ids` | 被预算丢弃的知识 chunk | Context Builder |

只有检索层为 `SUCCESS` 时才可能派生：

- `SUCCESS`：知识片段全部进入最终上下文；
- `SUCCESS_PARTIAL`：部分进入，部分被预算丢弃；
- `SUCCESS_BUT_DROPPED`：全部被预算丢弃。

`NO_MATCH`、`NO_VALID_DOCUMENTS`、`THRESHOLD_FILTERED`、`DEGRADED` 保持
检索层原值，因为它们本来就表示「没有知识片段」，预算不能再把不存在的片段
丢掉。

## 3. 实现要点

1. **最终状态在预算裁剪之后计算**，不是拿检索层的 `SUCCESS` 直接当作
   「知识已进入上下文」；
2. 在裁剪结束后补一个 `knowledge_status` 区块，内容由最终状态生成：
   - `SUCCESS_BUT_DROPPED`：说明检索成功、片段数量、全部因 Token 预算
     未放入，并明确「不等于没有检索到资料」；
   - `SUCCESS_PARTIAL`：说明进入/丢弃的数量，要求只依据实际出现的片段；
3. 状态区块在预算裁剪之后追加，不会再次因为没有预算而消失；
4. Guard 使用 `prepared.context.knowledge_state`，而不是检索层状态；
5. `SUCCESS_PARTIAL` / `SUCCESS_BUT_DROPPED` **不加入**
   `GUARD_STATUSES`。它们的说明已经在生成前写入 Prompt，若 Guard 再追加
   「本轮没有可用资料」会与 Context 的真实状态直接矛盾。

代码位置：
[agent/app/context/builder.py](../../agent/app/context/builder.py)、
[agent/app/runtime.py](../../agent/app/runtime.py)、
[agent/app/evidence_guard.py](../../agent/app/evidence_guard.py)。

## 4. 确定性回归

新增/覆盖的测试：

| 测试 | 断言的实际输出 |
| --- | --- |
| `test_success_with_injected_knowledge_stays_success` | `knowledge_state=SUCCESS`，注入 `[210]`，Prompt 含真实来源标签 |
| `test_success_but_all_knowledge_dropped_is_not_reported_as_no_match` | `SUCCESS_BUT_DROPPED`，注入 `[]`，丢弃 `[211]`；Prompt 含「检索成功」「全部因上下文 Token 预算未能放入」「不等于没有检索到资料」；**不含**「没有可用且已就绪的上传文档」「没有找到可用的文档片段」 |
| `test_partial_drop_reports_counts_and_only_injected_chunks_are_usable` | `SUCCESS_PARTIAL`，注入 `[212]`、丢弃 `[213]`；Prompt 写明 1 个进入、1 个未放入；只出现 `chunk 212` 的来源标签 |
| `test_no_match_keeps_retrieval_state_and_has_no_knowledge_chunks` | `NO_MATCH` 保持不变，无知识 chunk |
| `test_budget_dropped_success_is_explained_by_context_not_contradicted_by_guard` | 真实 `AgentRuntime.generate()` 输出不被 Guard 追加「没有检索到」；`EVIDENCE_GUARD` 事件不存在；`SUCCESS_BUT_DROPPED` 不在 `GUARD_STATUSES` |
| `test_status_survives_budget_drop_of_low_priority_knowledge_chunk` | 知识区块被丢弃后，状态区块仍在最终 Prompt |

这些断言读取的是 **Context Builder 渲染后的 system instruction 和
`AgentRuntime.generate()` 的实际输出**，不是内部布尔值。

执行结果：

- 定向测试：`71 passed`（`test_source_attribution.py`、`test_citations.py`、
  `test_context_builder.py`、`test_evidence_guard.py`、
  `test_runtime_citations.py`）；
- 全量 Agent：`351 passed, 5 skipped, 1 warning`。

## 5. 真实模型受控样本

由于 Phase 4 只保存了 preview、没有在该上下文下调用真实模型，本轮补了一个
**受控预算压力样本**：

- 知识片段：固定复用 Phase 4 的合成 chunk 18（现行规范，17 秒）与
  chunk 19（历史迁移，12 秒）；
- 压力来源：12 条合成历史 + 4 条合成长期记忆；
- 默认 `max_context_tokens=3000`；
- 真实模型：`deepseek-flash`；
- 检索来源：固定合成片段，**没有再次调用 Qdrant/Embedding**。

实际结果：

| 指标 | 实测 |
| --- | --- |
| 检索层状态 | `SUCCESS` |
| 预算后状态 | `SUCCESS_BUT_DROPPED` |
| 注入 chunk | `[]` |
| 丢弃 chunk | `[18, 19]` |
| 动态上下文 | 2780 / 3000 tokens |
| 完整 prompt 估算 | 约 5169 tokens（固定系统提示约 2389） |
| 引用 | 0 条 |
| 回答长度 | 562 字符 |

回答开头原样摘录：

> 关于 ORBIT-4721 部署失败后的重试等待时间，本轮我没法给你一个可靠的具体数值。
>
> 原因是：这次检索确实命中了材料，但片段因上下文预算没能进入本轮对话，
> 我看不到里面的内容；而当前可见的上下文里也没有任何写明重试窗口的地方。

这证明在本样本中，模型没有把「预算丢弃」说成「没有检索到」，也没有编造
引用或数字。它**不能**证明所有模型、所有提问都会这样表述。

证据：[phase4.1-agent-human-review.md](phase4.1-agent-human-review.md)
`P41-03-budget-insufficient`、`evaluation/phase4_1/budget_pressure_llm.json`、
`evaluation/phase4_1/run_budget_pressure_llm.py`。

## 6. 边界与未覆盖

- 这是**受控上下文**样本，不是在线 RAG 召回；检索结果没有重新从 Qdrant 取；
- 长历史与长期记忆是合成压力数据，不代表真实用户长度分布；
- 只有一个模型、一个问题、一次调用；没有统计成功率；
- 语义质量仍为 `PENDING`，没有人工评分，也没有 Judge 结论；
- 没有验证 `SUCCESS_PARTIAL` 在真实模型下的回答质量；
- 没有验证流式 SSE 下状态区块的最终顺序（本轮为 `complete()` 调用）；
- 没有把 Phase 4 的 2836 tokens 与本轮 2780 tokens 当成同一基线比较：
  压力数据不同，只能分别记录。

## 7. 结论

F1 的三种知识状态已经能在 **实际 Context Builder 输出**中区分：

1. `SUCCESS`：知识进入上下文；
2. `SUCCESS_PARTIAL`：部分进入，只允许引用实际出现的片段；
3. `SUCCESS_BUT_DROPPED`：检索成功但全部未进入，Prompt 明确说明；
4. `NO_MATCH`：检索没有命中，仍按原语义说明。

Guard 不再与 Context 状态矛盾。受控真实模型样本与确定性测试均通过；
但语义质量和在线 RAG 效果仍未验证，不能写成「RAG 回答质量已通过」。
