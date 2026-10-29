# Phase 3.3 Agent 人工评审状态与下一阶段测试优先级

- 日期：2026-10-28
- 范围：`phase3.2-human-review-blind.md` 的 9 条记录、人工评分状态、下一阶段测试排序
- 结论：9 条记录结构完整、可读；人工评分仍为 **0/9**；没有预填、没有计算
  Judge–human 一致性。下一阶段第一优先级是 **真实 Qdrant 检索 + 文档来源可追溯**。

## 1. 当前状态

| 指标 | 数量 |
| --- | --- |
| 待复核记录 | 9 |
| 已完成人工评分 | **0** |
| 待填写 | **9** |
| 已计算 Judge–human 一致率 / Kappa | **否** |
| 由 AI 预填的人工分 | **0** |
| 盲评表是否显示自动 Judge 分数 | **否**（只在说明中提及“已移除”） |
| 盲评表是否显示自动 `case_status` / 判读理由 | **否** |

机器可读队列 `evaluation/phase3/reports/human-review-queue-20261028.json`：

- 9 条 `items`；
- `humanScores` 的所有值均为 `null`；
- `humanNotes`、`humanDecision` 均为 `null`；
- `humanReviewStatus` 全部为 `PENDING`。

原始 Phase 3.1 评分表与原始 artifact 未被修改。盲评表由
`evaluation/phase3/build_blind_review.py` 生成，可重复执行。

## 2. 9 条记录的结构复核

本轮重新检查 `docs/testing/phase3.2-human-review-blind.md`：

| 检查 | 结果 |
| --- | --- |
| 记录标题（`## 1.` … `## 9.`） | 9 条 |
| “上下文事实摘要”小节 | 9 个 |
| “原始回答”小节 | 9 个 |
| “人工评分（待填写）”小节 | 9 个 |
| 回答正文为空 | 0 条 |
| 每条的 6 维 rubric | 9/9 完整 |
| `completionStatus` / `finishReason` | 9/9 保留 |
| `Evidence：0 条` 显式标注 | 6 条（其余 3 条有 Evidence 行） |
| 自动 Judge 分数 / 自动判定 | 0 处 |
| 人工分 / 理由 / 判定 | 0 处已填写 |

记录列表与原 Phase 3.2 审计一致：

| 记录 | completion | finish_reason | Evidence |
| --- | --- | --- | --- |
| B05-r1 | TRUNCATED | length | 0 条 |
| E01-r3 | COMPLETE | stop | 0 条 |
| E01-r2 | TRUNCATED | length | 0 条 |
| D01-r1 | COMPLETE | stop | 0 条 |
| A01-r1 | COMPLETE | stop | 5 条 |
| B02-r1 | COMPLETE | stop | 4 条 |
| E01-r1 | COMPLETE | stop | 0 条 |
| D09-r1 | COMPLETE | stop | 0 条 |
| A01-r2 | COMPLETE | stop | 5 条 |

**说明：** 盲评表第 7 行有一句“本表不显示自动 Judge 分数、自动 `case_status`……”，
这是对“已移除内容”的说明，不是自动评分数据。每条记录的分数/理由/备注栏均为空。

## 3. 盲评版是否仍保留必要信息

保留：

- 用户输入全文；
- 上下文事实摘要（概念、Understanding、Memory、轨迹；无 Evidence 时显式写 0 条）；
- `completionStatus` 与 `finishReason`（`TRUNCATED`/`length` 是客观事实，影响
  `task_completion` 上限）；
- 6 个维度的 0/1/2 锚点；
- 每条记录的分数、理由、备注、总体判定、评分人/日期栏。

移除：

- 自动 Judge 分数；
- 自动 `case_status`；
- 自动判读理由；
- 人工复核原因中的自动结论提示；
- 原来的 `agree/disagree` 人工判定（改为独立的 `PASS` / `FAIL` /
  `NEEDS-THIRD-REVIEW`）。

仍可见的用例编号（B05、E01 等）和完成状态是样本关联/客观事实所必需；填写说明
明确要求“不要根据用例编号、完成状态或模型自我评价倒推分数”。

## 4. 不能声称的内容

| 声称 | 当前是否成立 | 原因 |
| --- | --- | --- |
| “9 条人工评分已完成” | **否** | 0/9 |
| “Judge 与人工一致率高/ Kappa 高” | **否** | 没有人工分 |
| “Judge 已校准/有区分度” | **否** | 自动分几乎全是 2，缺 0 分样本，且 Judge 与 Agent 同 provider |
| “Agent 语义质量已通过” | **否** | 只有自动 Judge 分，未人工确认 |
| “人工分由 AI 预填” | **否** | 全部 null，生成器不写人工字段 |
| “盲评表完全没有自动结论痕迹” | **结构上成立** | 自动分/状态/理由已移除；说明文字中提到“已移除”不等于展示分数 |

## 5. 下一阶段测试优先级（Part 5）

不建议同时铺开。按“风险 × 角色 × 现有证据缺口”排序：

### 第一优先级：Agent 真实 Qdrant 检索 + 文档来源可追溯

**为什么先做：**

1. 这是当前最大的核心能力缺口。Phase 3.2 的 MQ E2E 使用 **hash embedding**，
   合法场景没有真实知识文档命中，知识检索是 `NO_MATCH`；因此“RAG 能工作”
   目前没有真实证据。
2. 它直接决定回答是否有依据、引用是否可追溯；没有这条证据，不能把“知识库问答/
   引用”写进简历。
3. 本地已有真实 Qdrant 容器（`127.0.0.1:6333`），不需要新增依赖或外部服务。
4. 它会自然产出后续语义评分所需的真实上下文，避免在 fixture 上继续调评分器。

**最小验收：**

- 用真实文档走完“上传/索引 → 检索 → 回答引用”的链路；
- 断言命中的 document/chunk 属于当前学习空间，且 citation 能回到来源；
- 断言无权文档不会进入检索结果；
- 记录 `NO_MATCH`、低分过滤、超时降级各自的行为；
- 不把 Mock LLM 的回答质量当成检索正确性。

### 第二优先级：后端真实 SSE 边界

**为什么排第二：**

- `ChatSseIntegrationTest` 的 7 条仍被门控跳过；顺序、客户端断开、持久化失败、
  空流/模型错误终态缺少真实 HTTP 证据。
- 它影响用户可见体验和状态一致性，但不涉及“答案是否有知识依据”，所以排在
  RAG 之后。

**最小验收：** 真实 Tomcat + 真实数据库，覆盖正常完成顺序、客户端中断、
下游错误终态、落库失败、空流；断言不重复消息、不悬挂 run。

### 第三优先级：文档索引状态机 + MQ 写失败回滚/状态一致性

**为什么排第三：**

- `DocumentIndexServiceIntegrationTest` 9 条、`Phase2MqProcessorIntegrationTest`
  2 条、`Phase2RealStateIntegrationTest` 2 条仍跳过；属于“写入正确性/恢复”风险。
- 它们重要，但可以在真实检索链路跑通后，用同一套文档/数据库夹具补，成本更低。

**最小验收：** READY/FAILED/回滚/幂等；MQ 结果写库失败时 run 不被误标成功；
重复结果不重复写业务行；Revision 幂等与跨会话隔离。

### 暂缓

- **语义评分 / Judge 校准**：需要人工评分或第二位评分者；在 0/9 的情况下继续
  调 Judge 只会增加自动分锚定。
- **跨会话长期记忆**：需要先有稳定的真实检索与状态机证据，否则容易把 fixture
  行为当成产品行为。
- **并发/吞吐/高可用**：当前单 Worker、prefetch=1、单节点 broker，没有性能
  目标与压测环境，不能生成数字。

## 6. 结论

9 条盲评记录结构完整、可读，人工评分为 **0/9**，本轮没有填写任何人工分、
没有计算一致性、没有把自动 Judge 分改名为人工分。下一阶段先把真实 Qdrant
检索与来源可追溯跑通，再补 SSE 边界和索引/回滚状态机；语义评分与跨会话能力
在检索证据稳定后再评估。
