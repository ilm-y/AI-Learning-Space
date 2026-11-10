# Phase 5 已知问题登记

- 登记时间：2026-11-09
- 来源批次：Phase 5 Step 3（S5 数据重设计与 HISTORY_GUARD 判据调整后的重跑）
- 适用范围：仅登记，不在本批修复；不作为 Phase 5 Step 3 的通过/失败判据，但必须在结论中附带

> 本文 KI 编号与 phase5-final-report.md 存在差异：本文 KI-3（HISTORY_GUARD internal_process 误判）对应 final-report 的 KI-2；后续跟踪统一以 final-report 编号为准。

本文件登记两条已观测到的产品/模型行为问题。两条都已在
`docs/testing/phase5-abc-rerun.md` 第 11、12 节留痕。

## KI-1 模型历史使用不稳定

**现象**

模型在 Manifest 明确注入历史的情况下，仍可能声称历史原句没有被保存。

**证据**

- 上一批（2026-11-09 22:47 快照）S5-C-2（run 9）：
  - 回答声称「第一次表述的原文没有被保存下来」；
  - 但该次 Manifest 的 SELECTED 来源包含 `history:1`、`history:2`，
    以及 `revision:6`、`revision:7`、`evidence:5`；
  - 该次未触发 HISTORY_GUARD，回答是模型直接生成的内容。
- 本批（2026-11-09 23:05 快照）S5-B/C 的 6 次重跑未复现这条具体声明：
  最终交付回答中，S5-B-1、S5-B-2、S5-C-2、S5-C-3 均引用了历史原句；
  S5-B-3 与 S5-C-1 的最终回答缺少原句，但原因是产品侧 HISTORY_GUARD
  回退（见 KI-2），不是模型声称历史不存在。
- Batch 5（2026-11-09 23:45 快照）：对引用类请求条件追加逐字引用约束后，
  S5-B 3 次重跑全部包含历史原句，0/3 出现「引用 Understanding 而非历史
  原句」；相比 Batch 4 的 S5-B-2 缺原句，频次下降。但 3/3 仍在回答中
  把 Understanding 的 explanation（「我原以为慢启动增长很慢，后来明白
  是起点小、呈指数增长」）称为用户「更早的说法 / 另一句原话」。历史原句
  仍被引用，约束没有完全阻止模型把 Understanding 转述当成用户原话。
  详见 `docs/testing/phase5-abc-rerun.md` 第 14 节。
- Batch 6（2026-11-09 23:58 快照）：在约束中补充 history / Understanding /
  Memory / Evidence 的来源定义与标注规则后，S5-B 3 次重跑结果：
  - 3/3 最终回答逐字包含历史原句（`exactOriginalSentence=true`）；
  - 3/3 把 Understanding 标注为「系统记录的你当前理解 / 转述」，
    `understandingAsOriginal=false`，0/3 出现 Batch 5 的「更早的说法 /
    另一句原话」措辞；
  - S5-B-1 首答触发一次 HISTORY_GUARD（`internal_process`，假设列表中的
    「想通」被字面命中），重生成后仍正确标注，未回退。
  本批只做 3 次样本，且不确定「0/3」是来源定义的效果还是采样波动；
  Prompt 层只规范了「叫法」。详见 `docs/testing/phase5-abc-rerun.md`
  第 15 节。
- Batch 7（2026-11-10 00:09 快照）：已把 Understanding 区块中
  `latest_user_explanation` 的渲染标签从「用户自己的说法」改为
  「系统保存的用户解释记录（可能来自批注摘录，不是 history 逐字原话）」，
  不重写用户内容，不改检索 / 预算 / Manifest 逻辑。离线 D4-5 21/21；
  S10-B 预算场景 `problems=[]`、全部来源 SELECTED、used/effective=
  2777/3000，citation 与改动前一致；`systemSha256` 按预期变化，
  指标未退化。详见 `docs/testing/phase5-abc-rerun.md` 第 16 节。

**影响**

Phase 5 的核心问题之一是「模型是否真的理解上下文」。这条反例说明
「历史已注入 Manifest」不等于「模型会正确使用历史」：模型可能一边
拥有历史，一边否认历史存在。若只检查 Manifest 注入，会把这类回答
误判为「上下文理解正常」。

**建议**

1. 下一批全量实验统计「回答声称历史不存在 / 没有保存」与 Manifest
   实际注入历史的矛盾次数，作为独立观测指标；
2. 在更多样本前，不把该现象升级为 Step 3 的硬失败判据；
3. 若后续要修复，应优先在提示词/校验层要求模型在否认历史前先核对
   本轮注入来源，而不是只在 harness 侧判定。Batch 5 已按此方向在
   引用类请求上条件追加逐字引用约束；它降低了「用 Understanding 替代
   历史原句」的频次，但没有消除「把 Understanding 转述称为用户原话」。
   后续若要求零容忍，需要更细的提示词或产品侧校验（例如要求带引号的
   原话必须能在本轮注入的 history 消息中逐字找到），不在本批边界内。
4. Batch 6 已按上述方向补齐来源定义与标注规则，S5-B 3/3 未再误称
   Understanding 为原话；下一步应扩大样本统计 `quoteSources` /
   `understandingAsOriginal` 分布。
5. Batch 7 已实施 ContextBuilder 的 provenance 标签改动（见 rerun 报告
   16.2），使 `latest_user_explanation` 在上下文层不再以「用户自己的说法」
   呈现；该改动会改变 `systemSha256` / `contextHash`，需在后续全量基线
   中重新记录。

**状态**：开放，已通过 prompt 来源定义（Batch 6）与 ContextBuilder
provenance 标签（Batch 7）双重缓解；S5-B 仅有 3 次重跑样本，
未见误称，但不足以关闭。

## KI-2 HISTORY_GUARD partial_upgrade 否定语境误判

**现象**

产品侧 `HISTORY_GUARD` 的 `partial_upgrade` 校验按字面匹配
「完全正确」「完全答对」「答错」等短语，没有识别否定/对比语境，
导致合法回答被误判并触发重生成。

相关实现：`agent/app/history_guard.py:129-132` 的正则
（`_ABSOLUTE_CORRECT_RE` / `_WRONG_ANSWER_RE`）与
`agent/app/history_guard.py:389-409` 的校验分支。

**证据**

上一批：

- S5-B-3（run 17）首答「不是完全答错，也不是完全答对」被字面命中
  「完全答对」「答错」，触发重生成。

本批（2026-11-09 23:05 快照）：

| key | run_id | 首次命中 | 重生成命中 | 结果 |
|---|---|---|---|---|
| S5-B-3 | 5 | 「而不是完全正确」→ 命中「完全正确」 | 「不是答对，也不是答错」→ 命中「答错」 | 回退，最终回答缺原句 |
| S5-C-1 | 7 | 「不是完全答错」→ 命中「答错」 | 「不是答错，也不是完全答对」→ 命中「完全答对」「答错」 | 回退，最终回答缺原句 |
| S5-C-2 | 9 | 「并没有被写成『答错』」→ 命中「答错」 | 未再命中 | 重生成后交付，含原句 |

以上三次命中都出现在否定/对比语境中，模型实际表达的是
「部分正确，不是完全答对，也不是答错」。

**影响**

1. 合法回答被误判并重写，增加一次 LLM 调用、延迟与 token；
2. 本批 6 次重跑中 3 次触发守卫，实际 LLM 调用 9 次，超出
   「6 + 守卫余量 2 = 8」预算 1 次；
3. 更严重的是：当重生成回答仍被误判时，产品使用确定性回退文本，
   该文本不含用户显式要求的「历史原句」。本批 S5-B-3 与 S5-C-1
   的最终交付因此缺少原句，直接违反正向判据。

**修复（Batch 4，2026-11-09）**

1. **否定 / 对比语境识别**：`partial_upgrade` 的两个字面正则
   （`_ABSOLUTE_CORRECT_RE` / `_WRONG_ANSWER_RE`）改为先经过
   `_effective_partial_upgrade_matches`，再由 `_phrase_in_negated_context`
   判定短语是否处在否定语境：
   - 短语前 12 个字符（且不跨句号 / 问号 / 感叹号 / 换行）内出现
     否定标记（`不是 / 而不是 / 并非 / 并没有 / 并未 / 并不表示 /
     并不代表 / 并不等于 / 并不完全 / 没有 / 不表示 / 不代表 /
     不等于 / 也不算 / 不算 / 谈不上 / 不能算 / 称不上 / 绝非`）；
   - 短语位于「不是 …… 也不是 ……」并列否定结构中，或紧跟第二个
     否定词之后（`_in_parallel_negation`）；
   - 若最近的标记是「而是」，则按肯定判断处理，不放行
     （「不是答错，而是完全答对」仍触发）。
   其他 issue kind 的匹配逻辑未改动。
2. **回退保护（方案 A）**：`build_history_fallback(state, first_answer=...)`
   在用户显式要求「引用 / 复述 / 原话 / 原文 / 原句」时，检查首答里
   是否存在与历史用户消息或本轮记录逐字一致的原句；存在则保留首答
   中的引号引用段（或按记录原句重建引用），再拼接确定性回退正文。
   同句中的其他无依据断言不随引用带回。
3. **可观测性**：新增 `partial_upgrade_negation_skips(text)`；harness
   每次运行记录 `partialUpgradeNegationSkipped` /
   `partialUpgradeNegationSkippedPhrases`，并记录回退是否保留原句。

**回归与验证**

- `agent/tests/test_history_guard.py` + `test_history_guard_runtime.py`
  共 32 passed；新增否定用例（「不是完全答错」「而不是完全正确」
  「并没有被写成『答错』」「既不是完全答对也不是完全答错」
  「并不表示全部答对」「并不等于答错」）均不触发；肯定用例
  （「这是完全答对」「这是答错」「不是答错，而是完全答对」）仍触发；
  回退保护用例覆盖「保留被要求引用的原句」「未显式要求时不保留」
  「不保留记录中没有的句子」。
- 修复后重跑 S5-B/C 各 3 次：6 次运行均未触发 HISTORY_GUARD，
  `partialUpgradeNegationSkipped` 均为 0，未出现否定语境误判。
  首次修复前的 S5-B-1 运行曾在「并不表示全部答对」上误触发
  （`并不表示` 当时不在标记表中）；补齐标记并归档该次运行后重跑，
  详见 `docs/testing/phase5-abc-rerun.md` 第 13 节。
- 重跑本身另发现 S5-B-2 最终回答不含原句（模型引用了 Understanding
  解释而非注入的历史原句），属于模型历史使用问题（KI-1 同类观测），
  不是本缺陷的残留。

**剩余风险**

否定识别是规则匹配，只覆盖「前 12 字符否定词 / 并列否定 / 肯定对比」
三类显式句式；反讽、双重否定、跨句否定等更复杂表达仍可能漏判。
`partialUpgradeNegationSkipped` 只做观测，不参与最终判定，需配合人工复核。

**状态**：已修复（Batch 4，2026-11-09）。

## KI-3 HISTORY_GUARD internal_process 对「想通」的字面误判

- 登记时间：2026-11-10
- 来源批次：Phase 5 Step 4 前置条件第 6 项（只登记，本批不修）

**现象**

产品侧 `HISTORY_GUARD` 的 `internal_process` 校验按字面匹配
「想通 / 意识到 / 想明白」等内省词。当这些词出现在**假设、转述或
来源说明**里，而不是对用户过去心理过程的肯定断言时，仍可能被命中，
触发一次不必要的重生成（严重时触发确定性回退）。

**证据**

- Batch 6（2026-11-09 23:58 快照）S5-B-1 首答在假设列表中写了
  「想通」，被 `internal_process` 字面命中；重生成后回答正确标注，
  未回退。见 `docs/testing/phase5-abc-rerun.md` 第 15 节。
- Step 4（2026-11-10 00:37 快照）S4G-attempt2（run 15）再次复现：
  首答在解释「不能替用户补心理过程」时写了「你意识到重传意味着丢包」，
  被字面命中；重生成仍命中，最终走确定性回退。trace：
  `kind=internal_process`，`excerpt=我如果给你补一段「你意识到重传
  意味着丢包，所以……」这样的叙述，那是我编的，不是发生过的事实`，
  `regenerated=true`、`fallback=true`。该次是误判而非真实无依据断言；
  其余 3 次尝试（attempt1、attempt4、attempt5）模型均未产生可拦截的
  编造断言，未触发守卫。详见 `docs/testing/phase5-step4-results.md`
  第 4、6 节。
- 相关实现：`agent/app/history_guard.py:63-91` 的 `_PROCESS_TERMS`
  与 `_INTERNAL_PROCESS_RE`；`_check_internal_process` 在
  `agent/app/history_guard.py:392-413`。

**影响**

1. 合法回答多一次 LLM 调用、延迟与 token；
2. 若重生成仍被字面命中，会使用确定性回退文本，可能丢失用户要求的
   引用原句（与 KI-2 的回退路径相同）；
3. 影响 `historyGuardTriggered` 观测：触发次数不完全等于真实无依据
   断言次数。

**本批边界**

Step 4 不修改 `agent/app/history_guard.py`。异常场景中构造
`internal_process` 触发时，需同时记录产品 trace 的 `kind/excerpt`
与首答/重生成结果，区分「真实无依据断言」与「字面误判」。

**状态**：开放（只登记，不在 Step 4 修复）。
