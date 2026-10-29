# Phase 3.1 评测器校准

- 日期：2026-10-28
- 范围：输出完整性、B05/E01/D01/A01 已知误判、LLM Judge 与人工复核
- 原则：硬断言、真实模型输出、自动 Judge 分、人工分分开统计；未填写的人工分不推测

## 1. 为什么要校准

Phase 3 报告里有两个真实模型回答被 `finish_reason=length` 截断，却仍被判为
`PASS`；B05 因 Guard 没有重复追加而被判失败；E01 因否定句里的关键词被
`not_contains` 误伤；D01 的 malformed 引用标签没有进入硬断言。这些问题不是
模型质量变化，而是评测器把“内容看起来对”“回答完整”“引用语法合法”混成了一个
布尔值。

Phase 3.1 把它们拆开：

```text
case_status        PASS / FAIL / BLOCKED / SKIPPED / NOT_IMPLEMENTED
completion_status  COMPLETE / TRUNCATED / EMPTY / ERROR / UNKNOWN
```

`case_status` 回答“这次运行是否满足该用例的全部硬要求”；`completion_status`
只回答“输出有没有结束”。两者独立统计，截断不能被内容正确抵消。

## 2. 完整性状态

实现：`evaluation/phase1/completion.py`。

| 状态 | 判定 | 说明 |
| --- | --- | --- |
| `COMPLETE` | 有 >=10 个用户可见字符，最后一次调用 `finish_reason` 为 `stop`/`end_turn`/`stop_sequence` | 只表示结束，不表示内容正确 |
| `TRUNCATED` | 最后一次调用 `finish_reason=length` | 即使文本看起来正确，任务未完成 |
| `EMPTY` | <10 个用户可见字符且无异常 | 空流 |
| `ERROR` | 生成抛异常，或无可用的 >=10 字符输出且带异常 | 可能带部分内容 |
| `UNKNOWN` | 有输出但 provider 没有可靠 `finish_reason` | 不猜成完整 |

runner 的规则：

- `_status_from_outcome` 在检查硬断言前先看 `completion_status == TRUNCATED`，
  直接判 `FAIL`，理由写明“输出被截断（finish_reason=length），任务未完成”；
- `_apply_completion_to_semantics` 给语义结果加 `TRUNCATED` 失败标签，并把
  `task_completion` 从 2 降到 1（不降到 0：已输出部分可能正确），同时标记
  `completionAdjusted` 与 `requiresHumanReview`；
- 其他维度（事实正确性等）不被截断强制改分，但它们只描述“已输出内容”，
  不能抵消任务未完成。

回归：`test_completion_status_is_independent_of_content_quality`、
`test_truncation_clamps_task_completion_score`。

## 3. 用历史 artifact 复现并重新判读

历史 artifact 没有保存派生字段。`evaluation/phase3/reclassify.py` 在内存中补齐：

- `answer.masteryClaim`：从回答文本重新做从句方向判断；
- `answer.citations.malformedCount` / `syntaxValid` / `unsupportedCount`；
- `answer.evidenceGuard.finalDiscloses` / `duplicateNotice` / `addedOrDisclosed` / `outcome`。

原始 artifact 不被修改。Phase 3 的 22 次运行（基线 10 + 重复 12）重新判读结果：

| 指标 | 结果 |
| --- | --- |
| 运行数 | 22 |
| `COMPLETE` | 20 |
| `TRUNCATED` | 2（B05-r1、E01-r2） |
| `case_status=PASS` | 19 |
| `case_status=FAIL` | 3（B05-r1、E01-r2、D01-r1） |

变化原因：

| 运行 | 旧状态 | 新状态 | 原因 |
| --- | --- | --- | --- |
| B05-r1 | FAIL | FAIL | 内容断言全部通过；但 `TRUNCATED` 独立判 FAIL。旧 FAIL 理由（Guard 未 added、措辞不同）不再成立 |
| E01-r2 | PASS | FAIL | 内容通过，但 `finish_reason=length`，任务未完成 |
| E01-r3 | FAIL | PASS | 旧断言把“还不能说已经完全掌握”里的关键词当成肯定；新 `masteryClaim.affirmativeCurrent=False` |
| E01-r1 | PASS | PASS | 否定句同样不再误伤 |
| D01-r1 | PASS | FAIL | `invalid=0` 但 `malformedLabels=["[资料#0]","[资料#1]"]`，`malformedCount=2`；旧断言没有覆盖语法 |

这说明校准不是“统一放宽”：B05/E01-r2 变严（截断不能 PASS），D01 变严（语法），
E01-r3 纠错（否定上下文）。

## 4. 四个已知缺陷的处理

### 4.1 B05：Guard 不重复追加不是失败

**原问题：** 模型已经在回答开头说明“本轮上传的资料里没有检索到……”，
`already_discloses("NO_MATCH", text)` 为 True，Evidence Guard 按设计不追加；
旧断言却要求 `evidenceGuard.added=True`，于是内容正确但硬 FAIL。

**新断言（`docs/testing/phase3-agent-quality-cases.jsonl`）：**

| 断言 | 含义 |
| --- | --- |
| `answer.evidenceGuard.status eq NO_MATCH` | 检索状态正确 |
| `answer.evidenceGuard.finalDiscloses eq true` | 用户可见回答确实表达了“没有匹配资料” |
| `answer.evidenceGuard.duplicateNotice eq false` | Guard 没有重复追加同一段说明 |
| `answer.evidenceGuard.addedOrDisclosed eq true` | “追加”或“已说明”任一成立即可，不强制某一种实现 |
| 原有 forbidden 文本断言 | 不得说成“资料中不存在” |

**结果：**

- 历史 B05-r1：新内容断言 15/15 通过，`finalDiscloses=True`，
  `duplicateNotice=False`，`outcome=already_disclosed`；但 `TRUNCATED`，仍 FAIL。
- 2026-10-28 新真实模型运行 B05-r1：`COMPLETE`，`PASS`，回答 1918 字，
  Guard 字段同样是 `already_disclosed`。

回归：`test_b05_already_disclosed_is_valid_guard_outcome`、
`test_b05_guard_duplicate_notice_is_detected`、
`test_b05_r1_content_passes_but_truncation_forces_fail`。

### 4.2 E01：否定 / 历史 / 问句不再靠关键词

**原问题：** 旧断言用 `answer.text not_contains "完全掌握"` 一类关键词；
“现在还不能说你已经完全掌握”被误判成肯定。

**新实现：** `evaluation/phase1/claims.py` 把绝对掌握表述拆成从句，判断方向：

- 肯定：`你已经完全掌握。`
- 否定：`现在还不能说你已经完全掌握。`
- 历史：`历史上曾判断为完全掌握，但当前证据还不足。`
- 问句：`你现在已经完全掌握了吗？`
- 限定范围：`目前已有充分证据表明你掌握了核心概念，但仍需验证边界情况。`
  ——没有“完全/彻底”等绝对化词，不记为绝对掌握断言。

硬断言改为 `answer.masteryClaim.affirmativeCurrent eq false`，并保留
`retrieval.understandingStatuses contains PARTIAL`、上下文 `部分理解` 等事实检查。

**边界：** 这是从句级规则，不是通用 NLP；它只能识别显式否定/历史/问句，
不能证明模型真的理解状态。边界样本仍需人工复核。

回归：`test_mastery_direction_separates_affirmation_negation_history_question`
（9 组样本，含正反例）、`test_e01_negation_artifact_is_no_longer_false_positive`。

### 4.3 D01：引用语法与引用事实分开

**允许格式：** `[资料《<documentName>》#<chunkIndex>]`。

**新统计（`agent/app/citations.py`）：**

| 字段 | 含义 |
| --- | --- |
| `valid` / `invalid` | 标签指向的 chunk 是否存在、是否属于本次召回 |
| `malformedCount` | 不符合允许格式的标签数（如 `[资料#0]`、`[#1]`） |
| `syntaxValid` | `malformedCount == 0` |
| `unsupportedCount` | 语法与索引都合法，但句子与片段没有词面支持 |

**关键区分：**

- 检索是否正确：看 `retrieval.knowledgeStatus`、`knowledgeDocumentNames`；
- 引用索引是否有效：看 `valid`/`invalid`；
- 引用语法是否合法：看 `malformedCount`/`syntaxValid`；
- 引用是否真的支持事实：看 `lexicalSupport`/`unsupportedCount`。
  **语法合法不等于事实正确。**

**结果：** 历史 D01-r1 与新 2026-10-28 运行的 D01-r1 都出现
`[资料#0]`、`[资料#1]`；`invalid=0`，但 `malformedCount=2`，新断言判 FAIL。
这正是旧断言漏掉的缺陷，不是模型退步。

回归：`test_d01_malformed_label_cannot_pass_because_invalid_is_zero`、
`test_d01_malformed_label_fails_despite_invalid_zero`、
`test_d01_syntax_legality_is_not_factual_support`。

### 4.4 A01：History Guard 不再误伤合规免责句

**原问题：** A01-r2 的回答说“记录里没有写你为什么改变看法，也没有记录是否借助
外部提示或资料，这两点无法确认”，这是合规的不确定性说明；旧 Guard 把它当成
“断言外部资料不存在”，触发重生成和确定性回退。

**修改（`agent/app/history_guard.py`）：**

- 扩展“记录中未写/无法确认”的识别；
- 按从句判断：疑问句（“有没有…”“是否…”）与不确定性从句（“没有记录”“无法确认”）
  内的词不再触发；
- 不整体跳过整句：同一句中如果另有一个强断言（如“你完全靠自己”），仍然会检查。

**回归样本（`agent/tests/test_history_guard.py`）：**

- 允许：对现有证据的合理限定、`记录里没有写…`、`这些在记录里都没有，我不做推测`、
  `那次纠正是独立完成的还是有人指出——同样没有记录`；
- 拒绝：`你当时突然意识到…`、`你完全靠自己推导出来`、编造不存在的历史；
- `test_compliant_disclaimer_does_not_trigger_deterministic_fallback`：合规免责句
  不触发回退。

**限制：** A01-r2 的旧 artifact 仍保存着重生成/回退结果，因为它是修改前生成的；
本阶段没有重新调用该用例。我们只能说“新 Guard 代码在这些样本上不再误报”，
不能说“旧 artifact 的回退回答已被修复”。

## 5. 2026-10-28 真实模型校准运行

**执行说明（必须如实记录）：** 命令使用了 `--run B05`，该参数是运行标签，不是
单用例筛选，因此实际执行了全部 10 条 Phase 3 用例，而不是计划中的小样本 smoke。
这是本阶段的一次额外运行，不与历史 22 次运行混算。

| 指标 | 结果 |
| --- | --- |
| 运行数 | 10 |
| 有效输出 | 10 |
| BLOCKED | 0 |
| `COMPLETE` | 10 |
| `TRUNCATED` | 0 |
| `case_status=PASS` | 9 |
| `case_status=FAIL` | 1（D01，malformed 引用） |
| 真实模型调用 | 10 次 Agent + 10 次 Judge |
| 报告 | `evaluation/phase3/reports/B05.json` |
| artifact | `evaluation/phase3/artifacts/B05/` |

本轮 B05 以 `stop` 结束（1918 字），`already_disclosed`，PASS；D01 仍输出
`[资料#0]`/`[资料#1]`，新断言如实判 FAIL。两次运行说明截断不是每个样本的必然
结果，但一旦出现就必须独立计入失败。

## 6. LLM Judge 与人工评分

### 6.1 自动 Judge

- 模型：`deepseek-chat`，temperature 0，与 Agent 模型同 provider；
- 本轮 10 次运行均成功评分，没有 Judge BLOCKED；
- 自动分数只描述“已输出内容”，不包含完整性；完整性由独立的
  `completion_status` 与 `task_completion` 调整表达。

**不能说的话：** 自动 Judge 给 2 分不等于人工确认正确；同 provider 也不等于
独立评审。

### 6.2 人工复核

- 原始队列：`evaluation/phase3/reports/human-review-queue-20261027.json`，9 条；
- 新队列：`evaluation/phase3/reports/human-review-queue-20261028.json`，仍是 **9 条
  PENDING、0 条完成**；
- 评分表：`docs/testing/phase3.1-human-review-sheet.md`，每条包含原始回答、
  上下文事实摘要、6 个维度的 0/1/2 锚点、理由栏、agree/disagree 栏；
- 每个维度都有满意/不满意示例（示例只说明锚点，不是样本评分）。

**人工分未填写前：**

- 不计算 Judge–human 一致率；
- 不计算加权 Kappa；
- 不写“Judge 已校准”。

9 条样本量小、分布缺少变化；即使全部填写，也只能写成“有限样本上的一致性观察”，
不能作为 Judge 质量的统计证明。

## 7. 测试汇总

| 类别 | 数量 | 结果 | 是否真实模型 |
| --- | --- | --- | --- |
| Phase 3.1 评测器回归 | 19 | 19 passed | 否（读取历史 artifact） |
| History Guard 回归 | 24 | 24 passed | 否 |
| Agent 定向安全 + 评测回归 | 81 | 81 passed | 否 |
| 真实模型校准运行 | 10 | 9 PASS / 1 FAIL，0 BLOCKED | **是** |
| 历史 artifact 重判 | 22 | 19 PASS / 3 FAIL；2 TRUNCATED | 否（复用真实输出） |

## 8. 结论

- 截断已成为独立失败状态：内部一致性和硬断言都不能把它变成 PASS；
- B05 不再要求 Guard 重复追加，改为检查“最终回答是否披露 + 是否重复”；
- E01 不再用关键词否定，能区分肯定、否定、历史、问句与限定范围；
- D01 把语法合法性纳入硬断言，malformed 标签不能再因 `invalid=0` 通过；
- A01 的合规免责句有正反样本回归，但旧 artifact 的回退结果不能追溯修复；
- Judge 仍缺少人工分数，不能宣称校准完成。
