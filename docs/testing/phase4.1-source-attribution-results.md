# Phase 4.1 F3：事实级来源归属

- 日期：2026-10-28
- 范围：只处理「不同来源/版本的事实合并成一句、只标一个来源」；
  不引入 reranker、不换模型、不把 `numericConflict` 当成自动判错
- 结论：确定性来源归属回归 **8/8 通过**；Prompt 已要求拆句并分别引用；
  **事实级语义是否真的被来源支持，仍为 `PENDING`，没有人工评分**

## 1. 修复前证据

Phase 4 的 6 个真实模型用例共 16 条引用，全部语法合法、chunk 存在且在最终
Context 中；其中 2 条 `numericConflict=true`：

| 用例 | 引用 | 冲突数字 | 现象 |
| --- | --- | --- | --- |
| `insufficient_detail` | `[资料《orbit_current_20261027_172403.md》#0]` | 12 秒 | 一句里同时出现现行 17 秒与旧版 12 秒，只标现行规范；被引 chunk 没有 12 |
| `legacy_only` | `[资料《orbit_current_20261027_172403.md》#0]` | 12 秒 | 「不要改回 12 秒」所在句只标现行规范；12 来自历史迁移 chunk |

这两条不是非法引用（标签存在、chunk 存在），而是**来源归属不精确**：
句子包含另一个来源的数字，却只标注了一个来源。

证据：[phase4-rag-agent-results.md](phase4-rag-agent-results.md) §3.3、
[phase4-failure-analysis.md](phase4-failure-analysis.md) §4。

## 2. 诊断口径

`numericConflict` 保留为**诊断信号**，不是错误判定：

- `numeric_conflict=true`：标注句里的某个数字在被引 chunk 原文中找不到；
- 它可能是：
  - 真的把 A 来源的数字归给 B；
  - 同义改写或跨句组合；
  - 句子在讨论「旧值」但没有把旧值写全；
- `false` 也不等于事实为真，因为词面重合不是语义蕴含。

`CitationReport` 现在分别暴露：

| 字段 | 含义 | 不能说明什么 |
| --- | --- | --- |
| `malformedCount` / `syntaxValid` | 是否符合唯一合法标注格式 | 不能说明来源支持事实 |
| `invalid` / `invalidLabels` | 标签不存在、chunk 索引不存在 | 不能说明语义错误 |
| `unsupportedCount` | 合法引用但句子与 chunk 无词面重合 | 只是排查线索 |
| `numericConflictCount` | 数字不在被引 chunk 中 | 不是自动判错 |
| `documentId` | 引用实际指向的文档 | 不证明该文档支持该句 |

## 3. 修复内容

### 3.1 Prompt 规则

`BASE_SYSTEM_INSTRUCTION` 规则 7、8 改为：

- 同一句话里的事实如果来自不同资料、不同版本或不同时间，**必须拆成独立句子**，
  在每个事实后分别标注它自己的来源；
- 不得把两个来源的数字、版本或结论合并成一句，却只在句末标注其中一个来源；
- 不得让 A 资料的数字看起来由 B 资料支持；
- 不同资料冲突时，保留冲突并说明版本或时间背景，不得静默混合或只挑一个当真。

### 3.2 引用诊断

- `Citation` 增加 `document_id`，`as_dict()` 输出 `documentId`；
- 非法引用（chunk 索引不存在）的 `document_id` 为 `None`，不会被误读成
  「指向某个真实文档」；
- 新增 `malformed_count`、`syntax_valid`、`unsupported_count`、
  `numeric_conflict_count`；
- 修正中文数字启发式：「一个历史值」「一个片段」这类不定指不再被换算成
  数字 1；此前它会把正确归属的引用误报为 `numericConflict`。新增
  `test_indefinite_yi_ge_is_not_a_factual_numeric_conflict` 固定该行为。

代码位置：[agent/app/prompts.py](../../agent/app/prompts.py)、
[agent/app/citations.py](../../agent/app/citations.py)。

## 4. 确定性回归

[agent/tests/test_source_attribution.py](../../agent/tests/test_source_attribution.py)
共 8 条，全部通过：

| 测试 | 断言 |
| --- | --- |
| `test_prompt_requires_splitting_facts_from_different_sources` | Prompt 含「拆成独立句子」「分别标注它自己的来源」「只在句末标注其中一个来源」「静默混合」 |
| `test_single_source_supports_a_single_fact` | 1 条合法引用，`chunk_id=18`、`document_id=20`、`as_dict()["documentId"]=20`，无数字冲突 |
| `test_two_sources_each_support_their_own_fact_when_split_into_sentences` | 2 条引用分别指向 18/19 与 20/21，`numeric_conflict_count=0` |
| `test_merged_versions_in_one_sentence_are_flagged_as_numeric_conflict` | 两个版本合并、只标现行规范 → `valid=1`、`numeric_conflict_count=1` |
| `test_phase4_legacy_only_sentence_keeps_its_numeric_conflict_regression` | Phase 4 `legacy_only` 形态固定为 `numeric_conflict=true` |
| `test_format_valid_but_body_fact_not_supported_is_reported_as_unsupported` | 格式合法的「仪表盘颜色」引用 → `valid=1` 但 `unsupported_count=1` |
| `test_nonexistent_chunk_index_is_illegal_even_if_the_document_exists` | `#9` 标签 → `invalid=1`、`document_id=None` |
| `test_correct_fact_pointing_to_wrong_source_is_a_signal_not_a_legal_error` | 同一事实标现行规范 → `chunk_id=18`、`numeric_conflict=true`；标历史规范 → `chunk_id=19`、`false` |

执行结果：

- 定向测试：`71 passed`（含本文件 8 条）；
- 全量 Agent：`351 passed, 5 skipped, 1 warning`。

## 5. 实际 Phase 4 答案复核

### 5.1 `insufficient_detail`

回答把 17 秒与旧值 12 秒放在同一句：

> 同一份规范还特别提醒：历史迁移文档里的旧值（12 秒）只描述已下线的调度器，
> 不能当作现行值使用 [资料《orbit_current_20261027_172403.md》#0]。

现行规范 chunk 只写「不要使用历史迁移文档中的旧值」，没有出现「12 秒」；
12 秒来自历史迁移 chunk。因此这条 `numericConflict=true` 是合理的结构信号：
一句话混合了两个来源的数字，却只标了现行规范。

### 5.2 `legacy_only`

回答已经先拆成「旧版 12 秒 [legacy]」和「现行 17 秒 [current]」两句，
但最后一句仍包含 12：

> 如果你是在排查告警，并且发现 17 秒不符合预期，那么应先核对部署版本，
> 而不是改回 12 秒 [资料《orbit_current_20261027_172403.md》#0]。

这句话的结论（先核对版本）由现行规范支持，但「12 秒」来自历史迁移
chunk。`numericConflict=true` 反映的是来源标注粒度问题，不是「12 秒这个
事实错误」。是否需要更细的拆句，由人工评审判断。

## 6. 修复后真实模型对照

Phase 4 的两条回答生成于 Prompt 修改之前，因此不能证明新 Prompt 的效果。
本轮另用**修改后的 Prompt**、同一组固定 chunk、真实 `deepseek-flash`，
对两个问题各调用 3 次（共 6 次），无预算压力，两个 chunk 都进入上下文。

结构结果：

| 用例 | 3 次调用中的 `numericConflict` | `invalid` | `unsupported` |
| --- | --- | --- | --- |
| `insufficient_detail` | 0 / 3 | 0 | 0 |
| `legacy_only` | 0 / 3 | 0 | 1 次出现 2 条 |

- 6 次回答都把 17 秒（现行）与 12 秒（旧版）拆在不同句子，并分别标注
  `current` / `legacy`；
- 6 次都没有非法引用；
- `legacy_only` 的第 2 次出现 2 条 `unsupported`：一条是回答开头的
  `**12 秒** [资料《orbit_legacy…》#0]`，claim 只取到「12 秒」，词面
  重合为空；另一条是「12 秒只是历史值」的总结句，引用了历史记录。
  这两条更像词面启发式的漏报，而不是已确认的错误归属，仍需人工判断。

必须同时记录的反例：在正式 3+3 次采样之外，一次早期探测运行中
`legacy_only` 仍出现过 1 条合并句（同一句同时出现 12 秒和 17 秒，只标
`current`）。模型有随机性，**不能把 6 次 0 冲突写成「该问题已永久消除」**。

样本与原始回答：
`evaluation/phase4_1/source_attribution_llm.json`、
[phase4.1-agent-human-review.md](phase4.1-agent-human-review.md)
`P41-07`、`P41-08`。

## 7. 关键限制

- 修复后样本只有 1 个模型、2 个问题、每例 3 次；没有统计显著性；
- 早期探测运行出现过 1 条合并句，说明 Prompt 只能降低概率，不能保证；
- `lexical_support` 与 `numericConflict` 都是词面/数字启发式，不做语义蕴含；
- 没有人工评分：本轮 8 条 Phase 4/4.1 回答（含 `P41-07`、`P41-08`）仍是
  `PENDING`，因此不能计算来源归属准确率，也不能计算 Judge–human 一致性；
- 没有覆盖表格、多跳推理、同一文档多 chunk、数字单位换算等更复杂的归属。

## 8. 结论

F3 的**代码可判定部分**已通过固定回归：

- 单来源支持单事实；
- 两来源拆句后分别引用；
- 合并版本会被 `numericConflict` 标记；
- 合法格式但来源无词面支持会单独报 `unsupportedCount`；
- 不存在的索引是非法引用，`documentId=None`；
- 事实正确但指向错误来源会被标记，而不是被当成格式错误。

修复后真实模型 6 次样本中，两个来源的数字都拆到了不同句子并分别引用，
`numericConflict=0`；但早期探测运行出现过 1 条合并句，说明这不是硬保证。
「事实级来源归属是否正确」仍是语义问题：当前只有确定性信号、6 次结构样本
和待评人工队列，没有人工分。**不能把 8/8 确定性测试或 6/6 无冲突样本写成
「引用语义正确率 100%」或「F3 已完成语义验证」。**
