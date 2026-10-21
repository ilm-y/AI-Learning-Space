# BC-001 纯提问轮次没有推进理解状态

- **发现日期**：2026-10-21
- **影响范围**：接入 Agent 分析之后的所有「只提问、没有自述」的轮次
- **严重程度**：中 —— 不影响回答，但会让学习状态页长期显示「未接触」，
  并让后续验证策略误判为「首次接触」

## 复现输入

```text
用户：缺页异常是什么？
助手：（正常回答）
```

## 错误行为

1. `learning_evidence` 里有一条 `OBSERVATION`，证明用户已经碰到这个概念；
2. `learning_understanding.status` 仍然是 `UNKNOWN`；
3. `understanding_revision` 没有新增记录；
4. 学习状态页看不到这次接触。

## 预期行为

```text
Evidence：1 条（OBSERVATION）
Understanding.status：EXPOSED
Revision：至少 1 条（trigger = 这条 Evidence）
```

`EXPOSED` 的语义是「接触过，但 Agent 还没有形成判断」—— 它不等于理解，
但也不能继续停在 `UNKNOWN`。

## 根因

接入 Agent 分析之前，后端走 `rules-v1`：`LearningAnalysisService` 把每条证据交给
`UnderstandingService.apply`，即使正确性是 `UNKNOWN`，`resolveStatus` 也会把
`UNKNOWN → EXPOSED`。

接入 Agent 之后，写入链路改成：

```text
Evidence Upsert → Understanding Proposal → LearningState
```

`applyProposal` 只在 Agent 给出 `knownParts / uncertainParts / misconceptions /
latestUserExplanation` 时才生效。纯提问轮次只有一条 `OBSERVATION`，
Agent 的提案是空的（这是正确的：它确实还没有判断），于是：

- Evidence 写了；
- 提案为空，Understanding 没动；
- 状态停在 `UNKNOWN`。

这不是 Agent 的错，也不是 `applyProposal` 的错 —— 是**新链路漏掉了 apply 的
「接触过」语义**。旧链路里它藏在 `resolveStatus` 的兜底分支里，替换时没有显式保留。

## 修复

1. `UnderstandingService.markExposed(userId, conceptId, triggerEvidenceId)`：
   仅当状态是 `UNKNOWN` 时推进到 `EXPOSED`，并写一条 Revision。
   更强的状态（`PARTIAL` / `DEMONSTRATED` / `MISCONCEPTION`）要求提案或测试，
   不能因为「有证据」就自动升级；
2. `AgentAnalysisWriteService.projectState`：对有新证据、但没有提案的概念调用
   `markExposed`，再投影 LearningState；
3. 有提案的概念不走 `markExposed` —— 提案本身已经把状态推进到正确的位置，
   再调一次只会多写一条无意义的 Revision。

## 回归用例

`live-revision-question-exposes-concept`（`evaluation/cases/live/understanding_revision.json`）

断言：

```text
waitForUnderstanding(page_fault) → status == EXPOSED
getRevisions(page_fault)         → count >= 1 且包含 EXPOSED
```

同一条用例同时覆盖「有证据、无提案」和「验证策略是 ASK_LATER 不打断」两个行为。
