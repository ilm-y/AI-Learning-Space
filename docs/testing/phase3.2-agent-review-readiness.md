# Phase 3.2 Agent 人工评审准备情况

- 日期：2026-10-28
- 范围：Phase 3 的 9 条待复核记录、评分表结构、盲评版本
- 结论：**9 条记录结构完整，可以开始人工评分；当前完成 0/9，没有任何人工分或
  Judge–human 一致性结论**

## 1. 当前状态

| 指标 | 数量 |
| --- | --- |
| 待复核记录 | 9 |
| 已完成人工评分 | **0** |
| 待填写 | **9** |
| 已计算 Judge–human 一致率 / Kappa | **否**（没有人打分） |
| 由 AI 预填的人工分 | **0** |

机器可读队列：`evaluation/phase3/reports/human-review-queue-20261028.json`。
每条记录的 `humanScores` 全为 `null`，`humanNotes` / `humanDecision` 为 `null`，
`humanReviewStatus` 为 `PENDING`。原始 artifact 未被修改。

## 2. 结构审计：9 条是否具备评分所需信息

| 记录 | 回答字符 | 上下文摘要字符 | Evidence 行 | completion | finish_reason | rubric | 人工状态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| B05-r1 | 1826 | 567 | 无（0 条） | TRUNCATED | length | 6/6 | PENDING |
| E01-r3 | 2128 | 353 | 无（0 条） | COMPLETE | stop | 6/6 | PENDING |
| E01-r2 | 1577 | 353 | 无（0 条） | TRUNCATED | length | 6/6 | PENDING |
| D01-r1 | 1813 | 579 | 无（0 条） | COMPLETE | stop | 6/6 | PENDING |
| A01-r1 | 711 | 1427 | 有（5 条） | COMPLETE | stop | 6/6 | PENDING |
| B02-r1 | 661 | 1251 | 有（4 条） | COMPLETE | stop | 6/6 | PENDING |
| E01-r1 | 1391 | 353 | 无（0 条） | COMPLETE | stop | 6/6 | PENDING |
| D09-r1 | 588 | 443 | 无（0 条） | COMPLETE | stop | 6/6 | PENDING |
| A01-r2 | 627 | 1427 | 有（5 条） | COMPLETE | stop | 6/6 | PENDING |

逐项核对：

| 要求 | 结果 |
| --- | --- |
| 实际用户可见回答 | 9/9 有完整 `answerText`（588–2128 字符），未被评分者改写 |
| 前置状态摘要 | 9/9 有概念、Understanding 状态与“已理解/不确定”明细；需要历史的样本含轨迹与 Memory |
| 必要证据 | 3 条有显式 Evidence 行；其余 6 条该轮没有按需 Evidence，盲评版已显式写“Evidence：0 条”，避免把缺失误读成漏填 |
| 完成状态 | 9/9 有 `completionStatus` 与 `finishReason`；2 条为 `TRUNCATED` / `length` |
| 0/1/2 标准 | 9/9 的 6 个维度都有 `score0/score1/score2` 锚点 |
| 理由与备注栏 | 盲评版每个维度都有“理由”和“备注”列，另有总体理由 |
| 人工分未被预填 | 9/9 的 `humanScores` 全为 null |

**一个事实性限制：** 6 条记录没有按需 Evidence（`evidenceIds` 为空），
上下文仍包含 Understanding、Memory 和/或轨迹。评分者应按“该轮实际有什么”
评分，不能因为 Evidence 缺失就自动给低分；`historical_evidence_use` 应看轨迹与
Understanding，`uncertainty_citation_honesty` 应看回答是否如实说明“本轮资料没有
检索到”。

## 3. 原评分表的问题与盲评版本

### 3.1 问题

`docs/testing/phase3.1-human-review-sheet.md` 每条记录包含
`### 自动 Judge（未确认）`，列出原始分、完整性调整后分数与 Judge 理由。这会
让评分者在打分前看到自动结论，产生锚定效应；同时“人工结论：agree/disagree”
隐含了一个未展示的自动判定，容易引导评分者去猜自动结果。

原始表和队列是 Phase 3.1 的产物，**保持原样不修改**，供完成盲评后对照。

### 3.2 盲评版本

新增：[phase3.2-human-review-blind.md](phase3.2-human-review-blind.md)
（由 `evaluation/phase3/build_blind_review.py` 生成，可重复执行）。

| 内容 | 原表 | 盲评版 |
| --- | --- | --- |
| 回答全文 | 有 | 有 |
| 上下文事实摘要 | 有 | 有（空 Evidence 显式写 0 条） |
| completion / finish_reason | 有 | 有（事实状态，任务完成度需要） |
| 0/1/2 rubric | 有 | 有 |
| 自动 Judge 分数 | 有 | **移除** |
| 自动 `case_status` / 判读理由 | 有 | **移除** |
| 人工复核原因（含“硬 FAIL 与语义分冲突”等提示） | 有 | **移除** |
| 自动 Judge 理由 | 有 | **移除** |
| 人工判定 | `agree/disagree` | 改为独立的 `PASS/FAIL/NEEDS-THIRD-REVIEW` |

盲评版仍保留用例编号（如 B05、E01）和 `completionStatus`。前者是样本关联所必需，
后者是客观事实；填写说明明确要求“不要根据用例编号或完成状态倒推分数”。

### 3.3 盲评流程建议

1. 评分者只打开盲评版，逐条填写 6 个维度的分数、理由、备注和总体判定；
2. 9 条全部完成后，再打开 Phase 3.1 原表对照自动分；
3. 对照结果单独记录为“一致/不一致”，不修改已填的人工分；
4. 如果同一批样本有第二位评分者，再计算一致率；单人评分不能计算 Kappa；
5. 任何“Judge 已校准/有区分度”的结论都必须等到人工分完成，并说明样本量与
   分布限制。

## 4. 评分指标与锚点

6 个维度均使用 0/1/2：

| 维度 | 0 | 1 | 2 |
| --- | --- | --- | --- |
| `factual_correctness` | 核心事实错误 | 部分正确或有重要遗漏 | 事实正确且边界清楚 |
| `task_completion` | 没有回答任务 | 部分回答或截断 | 完整回答任务 |
| `historical_evidence_use` | 忽略/编造历史 | 使用了部分历史 | 准确使用关键历史与版本 |
| `mastery_calibration` | 把未掌握说成已掌握（或反向） | 大致准确但不够具体 | 与记录状态一致并说明缺口 |
| `uncertainty_citation_honesty` | 编造引用或否认有资料 | 表述含糊 | 明确区分资料与通用知识 |
| `no_fabricated_personal_history` | 编造动机/心理过程/事件 | 有轻微推断 | 只陈述记录中的内容 |

具体到每条样本的完整锚点见盲评版各节的表格；`score0/score1/score2` 来自
`docs/testing/phase3-agent-quality-cases.jsonl` 的 rubric，不是本轮新编的。

**截断规则：** `TRUNCATED` 的 `task_completion` 最高 1。B05-r1 与 E01-r2 适用；
即使内容看起来正确，也不能给 2。

## 5. 不能声称的内容

| 声称 | 当前是否成立 | 原因 |
| --- | --- | --- |
| “9 条人工评分已完成” | **否** | 0/9 |
| “Judge 与人工一致率高/Kappa 高” | **否** | 没有人工分 |
| “Judge 已校准/有区分度” | **否** | 自动分几乎全是 2，缺 0 分样本，且 Judge 与 Agent 同 provider |
| “Agent 语义质量已通过” | **否** | 只有自动 Judge 分，未人工确认 |
| “盲评版没有泄露自动分” | **是（结构上）** | 已移除自动分/自动状态/判读理由；但用例编号与完成状态仍可见，已在说明中限制其使用 |
| “人工分由 AI 预填” | **否** | 全部 null，生成器不写人工字段 |

## 6. 产物

| 产物 | 路径 |
| --- | --- |
| 原始评分表（保留，不修改） | [phase3.1-human-review-sheet.md](phase3.1-human-review-sheet.md) |
| 盲评版评分表 | [phase3.2-human-review-blind.md](phase3.2-human-review-blind.md) |
| 机器可读队列 | `evaluation/phase3/reports/human-review-queue-20261028.json` |
| 盲评生成器 | `evaluation/phase3/build_blind_review.py` |
| 原始评分表生成器 | `evaluation/phase3/build_human_review.py` |

## 7. 结论

9 条记录具备“回答全文 + 上下文事实 + 完成状态 + 6 维 0/1/2 锚点 + 理由/备注栏”，
可以开始人工评审。当前完成度为 **0/9**；在人工填写前，不计算一致性、不宣称
校准，也不把自动 Judge 分数改名为人工分数。原始 Phase 3.1 评分表保持不变，
盲评版单独提供，以避免自动分数锚定。
