# Phase 3.2 结果：MQ 契约端到端验证

- 日期：2026-10-28
- 状态：**完整链路已实际执行，109 PASS / 0 FAIL**
- 范围：真实 Spring 生产者 → 真实 RabbitMQ → 真实 Python Worker → 真实后端 HTTP →
  隔离 MySQL；合法 v2、非法 v2、旧版本消息、重复投递、重试、死信与清理
- 原则：不把 Worker 单测当 E2E；Mock LLM 只证明契约与落库，不证明语义质量；
  未执行/未覆盖的项目不计 PASS

## 1. 结论摘要

| 问题 | 结论 | 证据 |
| --- | --- | --- |
| 是否真的经过 Spring 生产者、RabbitMQ、Worker、MySQL？ | **是**。合法场景由真实 Spring 分析派发器签名并发布任务，真实 Worker 消费，结果经真实 HTTP 回写后端，最终状态用 MySQL 查询断言 | 本报告第 3 节；`e2e-console.log` 第 1–2 节 |
| 合法 v2 是否成功？ | **是**。ANALYSIS run 40 变为 `SUCCEEDED`，Evidence / Understanding / Revision / Memory 均按预期落库 | 第 3.1 节 |
| 非法 v2 是否按契约拒绝？ | **是**。缺字段/空串/纯空白均 `FAILED` + `MESSAGE_INVALID`；v1/v3 为 `TASK_SCHEMA_UNSUPPORTED`，全部进死信、不进 retry 队列 | 第 3.2 节 |
| 重复投递是否重复写业务行？ | **否**。同一 taskId 投递两次，Worker 处理 2 次，Evidence/Revision/Memory/来源均只有 1 份，Result Consumer 识别 1 次 `DUPLICATE` | 第 3.3 节 |
| 是否改动数据库 schema？ | **否**。本轮只查询与清理数据，没有 DDL 变更 | `git diff` 与脚本 |
| 是否有未覆盖项？ | **有**。Mock LLM、hash embedding、无真实知识文档、单 Worker、单节点 RabbitMQ；语义质量未验证 | 第 6 节 |

最终统计：

```text
结果：PASS=109 FAIL=0
产物目录：backend/backend/target/phase51-mq-e2e/20261028-004526
用户：p51mq_20261028-004526 (id=3)  space=3  TLB=15  run=40
全部通过。
```

分段统计：前置检查 3、PENDING 窗口 5、Worker 消费与落库 22、真实重试 9、
非法/旧版本 65、清理残留 5，合计 **109**。第 5 节是数据库事实导出，不计检查数。

## 2. 运行环境与边界

| 组件 | 实际配置 |
| --- | --- |
| Spring Boot | 本地进程，`SERVER_PORT=8081`，`AILS_ANALYSIS_MODE=mq`，真实 Tomcat |
| RabbitMQ | 本地 broker `127.0.0.1:5672`，真实队列/交换机/死信 |
| Agent HTTP | 本地 `127.0.0.1:8000`，`AILS_LLM_PROVIDER=mock`、`AILS_EMBEDDING_PROVIDER=hash`、维度 8 |
| Agent Worker | 独立真实进程 `python -m app.worker`，消费 `learning.analysis.request` |
| MySQL | 独立容器 `127.0.0.1:3307`，库 `ails_phase32_e2e`，root 空密码 |
| 身份 | `AILS_INTERNAL_TOKEN` + `identity-secret`（Spring↔Agent）+ `delegation-secret`（仅 Spring/测试脚本） |
| 网络 | 全部 loopback；**不是**部署环境，不能据此证明网络隔离 |

**必须明确的边界：**

- LLM 是 **Mock**，本轮证明的是消息契约、身份转发、状态机、幂等与最终落库，
  **不证明**真实模型的回答质量、事实正确性或教学效果；
- Embedding 是 hash，合法场景没有真实知识文档命中，RAG 知识链路未在本轮验证；
- 单 Worker、`prefetch=1`、单节点 RabbitMQ，不构成并发/吞吐/高可用结论；
- 这是隔离测试库，不是生产数据；测试用户是本轮新注册的合成账号。

## 3. 场景与断言

### 3.1 合法 v2：真实生产者 → Broker → Worker → MySQL

执行路径：

1. 通过真实后端 HTTP 完成一轮 SSE 对话，后端创建 CHAT run 39 与 ANALYSIS run 40；
2. Worker 未启动时，run 40 保持 `PENDING`，没有 Evidence（证明未提前伪造完成）；
3. Spring 分析派发器用 `delegation-secret` 签发 `backendIdentity`，发布 schema v2 任务；
4. 启动真实 Worker，消费任务，经真实 HTTP 调用后端只读接口；
5. 后端 Result Consumer 写库；用 MySQL 查询断言最终状态。

| 断言 | 实际值 |
| --- | --- |
| ANALYSIS run 状态 | `SUCCEEDED` |
| Evidence 数量 | 1 |
| Evidence 类型 / 正确性 / 分析器版本 | `USER_EXPLANATION` / `PARTIAL` / `agent-v1` |
| Understanding 状态 | `EXPOSED` |
| Revision 数量 / 状态 | 1 / `EXPOSED`（与 Understanding 一致） |
| Memory 数量 | 1，`CANDIDATE`，作用域 `GLOBAL`，保留用户原句 |
| Memory 来源 | 1 条，`MESSAGE`，绑定本轮用户消息 |
| run `output_summary` | `evidence=1, proposal=1, memory+=1, memory~=0, test=-` |

`Memory` 只能是 `CANDIDATE`、偏好作用域是 `GLOBAL`，说明 Agent 推断没有直接
升级为已确认长期记忆；这与现有产品契约一致。本轮没有把 Mock 输出当成语义质量证据。

### 3.2 非法 v2 与旧版本：入口拒绝、死信、不写 Agent 学情

这一组消息由测试脚本直接发布到真实交换机，**不是** Spring 生产者生成——因为
合法 Spring 生产者不会发出契约违规消息。消费方仍是真实 Worker，失败结果仍由
真实后端消费并落库。

| 场景 | schemaVersion | backendIdentity | run | 终态 | 错误码 | retry 队列 | 死信 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 缺字段 | 2 | 缺失 | 44 | `FAILED` | `MESSAGE_INVALID` | +0 | +1 |
| 空串 | 2 | `""` | 46 | `FAILED` | `MESSAGE_INVALID` | +0 | +1 |
| 纯空白 | 2 | `"   "` | 48 | `FAILED` | `MESSAGE_INVALID` | +0 | +1 |
| 旧版本 | 1 | 缺失 | 50 | `FAILED` | `TASK_SCHEMA_UNSUPPORTED` | +0 | +1 |
| 未知版本 | 3 | 合法签名 | 52 | `FAILED` | `TASK_SCHEMA_UNSUPPORTED` | +0 | +1 |

每个场景都核对了：

- 失败 run 的 `output_summary IS NULL`，没有伪装成已完成；
- Worker 日志中没有该 taskId 的「分析完成」或「分析失败」，即 **Runtime 调用为 0**；
- 坏任务本身没有写 Evidence、Revision 或 Memory 来源；
- 死信深度合计 +5，retry 队列增量为 0；
- 全库 Memory 仍为 1 条，TLB 的 Revision 仍为 1 条。

**`retryable=false` 的证据边界：** E2E 从外部观察到的是「FAILED + 死信 + 不进
retry」，与不可重试路径一致；`retryable` 字段本身由 Worker 单元测试
`test_v2_task_without_backend_identity_is_rejected_at_the_entry` 断言为 `False`，
本轮 E2E 没有直接解析结果消息体。两处证据分开报告。

**rules-v1 兜底是设计行为，不是 Agent 成功：** 每个失败 run 之后，后端会创建
独立的 `COMPLETED` ANALYSIS run（id 53–57），写入 1 条
`OBSERVATION / UNKNOWN / rules-v1` 证据。它不冒充 Agent 分析，也不会推进
Understanding/Revision/Memory。报告不会把兜底 run 计为 Agent 成功。

### 3.3 重复投递

- 同一 `taskId` 的两个副本都被 Worker 消费（日志计数 2）；
- Evidence、Revision、Memory、Memory 来源均没有新增（各保持 1）；
- Result Consumer 第二次识别为 `DUPLICATE`（日志计数 1）；
- 后端用条件更新抢占 run，业务写入按键去重，不依赖内存去重。

### 3.4 真实重试与恢复

- 第一个 Worker 使用错误的共享服务 token，后端真实返回 401；
- Worker 归类为 `RETRIEVAL_UNAVAILABLE`（可重试），日志
  `retries=0, retryable=True`；
- retry 队列收到 1 个副本，等待期间 run 仍为 `PENDING`，没有提前判死；
- 新 Worker 消费重试副本后 run `SUCCEEDED`，`retries=1`，Evidence 落库；
- 场景没有留下 `FAILED` run。

### 3.5 清理与隔离

| 检查 | 结果 |
| --- | --- |
| 五个 analysis 测试队列清理 | 成功；清理前 `dlq=5`，其余为 0 |
| 本轮用户及级联业务数据删除 | 成功（按 `user_id=3`） |
| 本地任务 JSON 删除 | 成功（其中含短期委托声明） |
| 用户/空间/会话/run/Evidence/Memory 残留 | 0 |
| 五个队列残留 | 0 |

清理动作本身写入了 `cleanup.log`，成功/失败都有显式记录；本轮没有出现清理失败。
隔离库 `ails_phase32_e2e` 保留，数据库实例未删除。

## 4. MQ 契约核对

| 核对项 | 结论 | 证据 |
| --- | --- | --- |
| 合法 v2 完整生产者→消费者路径 | 通过 | 3.1 |
| `MESSAGE_INVALID` 终态与重试策略一致 | 通过 | 3.2；单测断言 `retryable=False` |
| 重复投递不重复写业务行 | 通过 | 3.3 |
| 密钥/内部身份不打印到普通日志 | 通过 | 第 5 节扫描 |
| 失败消息可追溯到唯一测试请求 | 通过 | Worker 日志 `taskId=44/46/48/50/52` 与 run id 一致；`agent_run.trace_id` 有索引；失败行保留错误码 |
| 是否改动 schema | 否 | 无 DDL |
| 生产者/消费者是否兼容 | v2 兼容；v1/v3 明确拒绝，无兼容分支 | `AnalysisTaskMessage`、`agent/app/messaging/task.py` |

**滚动升级约束（已存在，不是新缺陷）：** 当前没有 v1 → v2 的兼容路径。
部署顺序必须是先升级 Worker/消费者，再让 Spring 生产者发 v2；否则旧 Worker 会把
v2 消息当未知版本拒绝。该约束已记录在 [open-risks.md](open-risks.md) 的 R-MQ-01。

**补充（Phase 3.3）：** “先升消费者”只是必要条件，不是充分条件。消费者升级后、
生产者升级前，旧生产者发出的 v1 会被新 Worker 拒绝；因此不能称为安全的滚动升级，
需要协调切换并排空 request/retry。见
[phase3.3-schema-rollout-review.md](phase3.3-schema-rollout-review.md)。

**低危观察（未修改代码）：** `error_message` 保存了 pydantic 校验错误的
`input_value` 摘要。本轮 missing/empty/blank 的输出只包含 `backendIdentity` 的
空值或空白，没有出现签名值；但该模式在别的字段校验失败时可能把消息字段带进
数据库错误栏。建议后续对 `input_value` 做字段级脱敏或只保留字段名。本轮没有
因此判定契约不兼容，也没有为此改动业务逻辑。

## 5. 日志与凭证卫生

对以下位置做了字面量扫描：

- `backend/backend/target/phase51-mq-e2e/20261028-004526/` 全部文件；
- `/tmp/ails-agent-mq.log`、`/tmp/ails-backend-mq.log`；
- Worker 正常/失败/重试日志。

结果：本轮使用的共享服务 token、identity 密钥、delegation 密钥三个字面量均
**0 次出现**；`v1.` 开头的身份串在 artifact 目录 **0 次出现**（任务 JSON 已在
清理阶段删除）。失败日志只记录 `taskId`、错误码和 pydantic 字段名/片段，没有
记录 token 或签名。

## 6. 未验证 / 不能声称

| 项目 | 状态 | 原因 |
| --- | --- | --- |
| 真实模型语义质量 | **未验证** | 本轮 LLM 为 Mock |
| RAG 知识检索正确性 | **未验证** | hash embedding，合法场景无知识文档命中 |
| 并发/吞吐/高可用 | **未验证** | 单 Worker、prefetch=1、单节点 broker |
| 生产网络隔离 | **未验证** | 全部 loopback；无部署映射/防火墙证据 |
| 旧版本消息兼容 | **不存在** | v1/v3 明确拒绝，需按顺序升级 |
| 清理后的长期残留 | **本轮为 0** | 只证明本轮；没有持续清理任务 |
| E2E 纳入 CI | **未实现** | 仍为手工脚本 |

## 7. 产物索引

| 产物 | 路径 |
| --- | --- |
| 控制台完整输出（109 行检查） | `backend/backend/target/phase51-mq-e2e/20261028-004526/e2e-console.log` |
| Worker 正常日志 | `.../20261028-004526/worker.log` |
| Worker 重试失败/恢复日志 | `.../worker-retry-wrong.log`、`.../worker-retry-recovered.log` |
| 非法消息 Worker 日志 | `.../worker-failure.log` |
| 清理日志 | `.../cleanup.log` |
| 脚本 | [scripts/phase51-mq-e2e.sh](../../scripts/phase51-mq-e2e.sh) |
| 跳过的风险审计 | [phase3.2-skipped-audit.md](phase3.2-skipped-audit.md) |
| 人工评审准备 | [phase3.2-agent-review-readiness.md](phase3.2-agent-review-readiness.md) |
