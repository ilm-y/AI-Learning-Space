# Phase 2.1 集成与端到端结果

- 日期：2026-10-27
- 范围：真实 HTTP SSE 契约、真实 RabbitMQ + 真实 MySQL + 真实 Worker 消费链、
  失败/重试/死信/幂等、清理与残留
- 原则：Mock、真实依赖、端到端分开统计；未实现的能力标 `NOT_IMPLEMENTED`，
  未覆盖的场景标 `NOT_COVERED`，不计 PASS；不生成未实测的性能数字

## 1. 环境

| 组件 | 实际使用 | 说明 |
| --- | --- | --- |
| MySQL | 8.0.37，隔离库 `ails_chat_sse_it` / `ails_phase21_mq` | 测试前建库，测试后核对/清理 |
| RabbitMQ | 本地容器 `ails-rabbitmq`，`127.0.0.1:5672` | 真实 broker，非内存队列 |
| Spring Boot | 3.3.5 / Java 17，`RANDOM_PORT`（SSE）与 `8081`（MQ） | 真实 Tomcat、Security、MyBatis |
| Agent | `127.0.0.1:8000`，`AILS_LLM_PROVIDER=mock`、hash embedding | Worker 真实进程；LLM 是 Mock |
| LLM | Mock | 不把 Mock 输出当真实模型质量 |
| Qdrant | 未参与本轮 SSE/MQ 判定 | MQ 检索走真实后端；知识文档为 0 |

原始证据：

- SSE：`backend/backend/target/phase2.1-evidence/TEST-com.ails.backend.service.ChatSseIntegrationTest.xml`
  与同名 `.txt`（`Tests run: 7, Failures: 0, Errors: 0, Skipped: 0`）；
- MQ：`backend/backend/target/phase51-mq-e2e/20261027-222238/`，
  完整输出 `script-output.log`，结果 `PASS=46 FAIL=0`。

## 2. 真实 HTTP SSE（7 个用例）

执行：`AILS_CHAT_SSE_IT=true AILS_IT_MYSQL_PASSWORD=... ./mvnw -Dtest=ChatSseIntegrationTest test`

测试使用真实 Tomcat + 真实 MySQL + 真实 `ChatService`/`MessageService`/
`TurnAnalysisService`/rules-v1；通过 `@Primary` 注入可控 `AgentGateway` 模拟
LLM 流，通过 `@SpyBean` 在分析入口/持久化入口做可控阻塞与故障注入。
**没有用 MockMvc 代替 HTTP，也没有用 mock consumer 代替 SSE 链路。**

| case_id | 场景 | 实际 | 期望 | 真实依赖 vs 替身 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SSE-2.1-01 | 正常流顺序与“先落库后 end” | `message.start → delta* → message.end`；end 到达时库中已有 2 条消息（1 USER/1 ASSISTANT），run=COMPLETED；分析被阻塞时 Evidence=0，放行后=1 | 顺序正确、先持久化、后分析 | 真实 HTTP/MySQL/Security；Agent 流为可控替身 | PASS |
| SSE-2.1-02 | 助手消息落库并收到 end 后客户端断开 | 关闭响应体后分析仍进入并写入 1 条 Evidence；重连两次 GET 返回相同 2 条消息；Revision=1 | 断开不取消已提交分析；重连不重复 | 真实 HTTP/MySQL；分析入口受阻 | PASS |
| SSE-2.1-03 | 助手消息持久化失败 | `message.start → delta* → message.error(code=500,"回复保存失败")`；无 ASSISTANT；run=FAILED；Evidence=0；`analysisDispatchService.dispatch(...)` 从未调用 | 未落库禁止分析 | 真实 HTTP/MySQL；持久化入口故障注入 | PASS |
| SSE-2.1-04 | LLM 空流 | `message.start → message.error(code=502)`，消息含 `empty_stream`；run=FAILED；无助手消息/Evidence | 可识别终态，不伪装成功 | 真实 HTTP/MySQL；Agent 网关返回空流 | PASS |
| SSE-2.1-05 | 模型错误 | `message.start → message.error(code=502)`，消息含 `quota exceeded`；run=FAILED；无助手消息/Evidence | 可识别终态 | 真实 HTTP/MySQL；Agent 网关返回 502 | PASS |
| SSE-2.1-06 | 无幂等键的重复 POST | 两次相同 POST → USER 2、ASSISTANT 2、CHAT run 2；Evidence 1、Revision 1（去重键收敛） | 若支持幂等应只产生一轮消息 | 真实 HTTP/MySQL | **NOT_IMPLEMENTED**（测试断言的是现状，不计能力通过） |
| SSE-2.1-07 | 向他人对话发消息 | HTTP 404 JSON，错误信息为“对话不存在/学习空间不存在”；目标对话消息数=0 | 建立流前拒绝，不写任何消息 | 真实 HTTP/MySQL/Security | PASS |

补充说明：

- 7 个测试方法全部通过（7/7），但 SSE-2.1-06 只证明“重复 POST 会重复落库”这一缺口，
  **没有**幂等能力，因此能力状态为 `NOT_IMPLEMENTED`，不能写成“重复请求已处理”。
- 客户端在**第一个 delta 之后、助手消息落库之前**断开的行为未单独覆盖
  （`NOT_COVERED`）。现有测试只覆盖“助手消息已落库并收到 end 后断开”。
- 未执行性能/并发压测，不报告吞吐、P95 等数字。

### 2.1 本轮发现并修复的 HTTP 错误表示缺陷

**现象**：跨用户 POST 带 `Accept: text/event-stream`，期望 404 JSON，实际得到
401“未登录或 Token 已过期”。

**根因**：`@ExceptionHandler` 返回 `ApiResponse` 时按 `Accept` 做内容协商；
客户端只接受 `text/event-stream`，JSON 没有可接受的表示，抛出
`HttpMediaTypeNotAcceptableException`，随后被 Spring Security 的
`ExceptionTranslationFilter` 改写成 401。业务错误被误报成认证失败。

**修复**：所有错误响应显式设置 `Content-Type: application/json`
（`GlobalExceptionHandler.java`）。

**回归**：

- `GlobalExceptionHandlerTest` 2/2：业务 404 与未知 500 在只接受
  `text/event-stream` 时仍返回 JSON；
- `ChatSseIntegrationTest.crossUserConversationIsRejectedBeforePersistence`：
  真实 HTTP 下返回 404，且不写入任何消息。

## 3. 真实 RabbitMQ + MySQL + Worker 端到端（46 项检查）

执行：

```bash
MYSQL_DB=ails_phase21_mq \
AILS_INTERNAL_TOKEN=... AILS_INTERNAL_IDENTITY_SECRET=... \
BACKEND_LOG=/tmp/ails-backend-mq.log \
bash scripts/phase51-mq-e2e.sh
```

链路：真实后端（`AILS_ANALYSIS_MODE=mq`）→ 真实 RabbitMQ →
真实 `python -m app.worker` → 真实后端 Result Consumer → 真实 MySQL。
Worker 使用真实内部签名身份调用后端只读检索接口；LLM 为 Mock，不参与质量结论。

### 3.1 分组结果

| 组 | case_id | 数量 | 关键结论 |
| --- | --- | ---: | --- |
| 前置、SSE、PENDING 窗口 | MQ-2.1-01–09 | 9 | 对话 SSE 完成时分析尚未执行；run=PENDING；无 Evidence |
| Worker 消费与写入 | MQ-2.1-10–19 | 10 | run=SUCCEEDED；Evidence=1；Revision=1；Memory=CANDIDATE/GLOBAL；来源绑定消息 |
| 重复投递幂等 | MQ-2.1-20–25 | 6 | 第二条相同 taskId 消息被消费，但 Evidence/Revision/Memory 均不新增；Result Consumer 记录 DUPLICATE |
| 临时失败与重试 | MQ-2.1-26–34 | 9 | 401 归类为可重试；retry 队列副本；等待期间仍 PENDING；新 Worker 消费后 SUCCEEDED，retries=1 |
| 永久失败、兜底与死信 | MQ-2.1-35–44 | 10 | run=FAILED、output_summary 为空、error_message 记录 `TASK_SCHEMA_UNSUPPORTED`；独立 rules-v1 兜底 run；证据 UNKNOWN；状态未推进；DLQ +1 |
| 清理与残留 | MQ-2.1-45–46 | 2 | 用户/空间/会话/run/Evidence/Memory 残留=0；五个测试队列残留=0 |

### 3.2 等待策略

最终状态全部使用有界轮询，不假设固定 `sleep` 后一定完成：

- `wait_for_run_status <runId> <status> <seconds>`：每秒查库，直到状态匹配或超时；
- `wait_for_analysis_row`：等待 ANALYSIS run 出现；
- 本轮新增 `wait_for_log_count`：等待 Worker 处理完两条相同 taskId 的消息、
  等待 Result Consumer 打出 DUPLICATE 日志；
- 本轮新增 `wait_for_fallback_run`：等待 rules-v1 兜底 run 出现。

原有的固定 `sleep 4` / `sleep 2` 已替换为上述轮询。Worker 启动、retry 队列深度
仍使用带超时的轮询；没有把“等一会儿应该好了”当成通过条件。

### 3.3 全部 46 项检查

| case_id | 检查 | 结果 |
| --- | --- | --- |
| MQ-2.1-01 | Agent / mq 后端 / RabbitMQ 均可用 | PASS |
| MQ-2.1-02 | 注册并取得 token（日志不回显 token 值） | PASS |
| MQ-2.1-03 | 创建空间与概念 | PASS |
| MQ-2.1-04 | SSE 对话完成（分析尚未完成） | PASS |
| MQ-2.1-05 | 创建了 ANALYSIS agent_run | PASS |
| MQ-2.1-06 | Worker 未启动时任务保持 PENDING | PASS |
| MQ-2.1-07 | Worker 未启动时没有 Evidence 落库 | PASS |
| MQ-2.1-08 | 已向 request 队列投递同 taskId 的重复副本 | PASS |
| MQ-2.1-09 | Worker 已连接 RabbitMQ | PASS |
| MQ-2.1-10 | Worker 消费后 ANALYSIS run 变为 SUCCEEDED | PASS |
| MQ-2.1-11 | Evidence 落库且只有一条 | PASS |
| MQ-2.1-12 | Understanding 只产生一条 Revision | PASS |
| MQ-2.1-13 | run 的 output_summary 记录写入摘要 | PASS |
| MQ-2.1-14 | Memory 候选落库且只有一条 | PASS |
| MQ-2.1-15 | Agent 推断的 Memory 只能是 CANDIDATE | PASS |
| MQ-2.1-16 | 偏好 Memory 的作用域是 GLOBAL | PASS |
| MQ-2.1-17 | 偏好 Memory 保留了用户原句 | PASS |
| MQ-2.1-18 | Memory 来源绑定本轮用户消息 | PASS |
| MQ-2.1-19 | Memory 写入计入 run 摘要 | PASS |
| MQ-2.1-20 | 重复任务没有新增 Evidence | PASS |
| MQ-2.1-21 | 重复任务没有新增 Revision | PASS |
| MQ-2.1-22 | 重复任务没有新增 Memory | PASS |
| MQ-2.1-23 | 重复任务没有新增 Memory 来源 | PASS |
| MQ-2.1-24 | Worker 处理了两条 taskId 相同的消息 | PASS |
| MQ-2.1-25 | Result Consumer 第二次识别为 DUPLICATE | PASS |
| MQ-2.1-26 | 创建重试场景概念 | PASS |
| MQ-2.1-27 | 重试场景先进入 PENDING | PASS |
| MQ-2.1-28 | 可重试失败后 retry 队列收到副本 | PASS |
| MQ-2.1-29 | Worker 将 401 归类为可重试的 RETRIEVAL_UNAVAILABLE | PASS |
| MQ-2.1-30 | 重试等待期间任务仍是 PENDING（没有提前判死） | PASS |
| MQ-2.1-31 | 重试副本被新 Worker 消费后任务 SUCCEEDED | PASS |
| MQ-2.1-32 | 成功消费时 retries=1 | PASS |
| MQ-2.1-33 | 重试成功后 Evidence 落库 | PASS |
| MQ-2.1-34 | 重试场景没有留下 FAILED run | PASS |
| MQ-2.1-35 | 失败场景任务先进入 PENDING | PASS |
| MQ-2.1-36 | Worker 永久失败后 run 变为 FAILED | PASS |
| MQ-2.1-37 | 失败 run 的 output_summary 为空 | PASS |
| MQ-2.1-38 | run 的 error_message 记录失败分类 | PASS |
| MQ-2.1-39 | 存在独立的 rules-v1 兜底 run | PASS |
| MQ-2.1-40 | 兜底证据只声明 UNKNOWN 观察 | PASS |
| MQ-2.1-41 | 兜底证据标记为 rules-v1 | PASS |
| MQ-2.1-42 | 失败 + 兜底没有新增 Revision | PASS |
| MQ-2.1-43 | 失败后学习状态没有被推进为掌握 | PASS |
| MQ-2.1-44 | 坏消息进入死信队列（深度 +1） | PASS |
| MQ-2.1-45 | 用户/空间/会话/run/Evidence/Memory 残留为 0 | PASS |
| MQ-2.1-46 | 五个测试队列残留为 0 | PASS |

### 3.4 首次重跑失败与修复记录

**失败现象**：一次重跑得到 `PASS=30 FAIL=14`，表现为 ANALYSIS run FAILED、
Memory=0、retry 未发生。

**原始失败证据**：
`backend/backend/target/phase51-mq-e2e/20261027-215006/worker-retry-wrong.log`

```text
HTTP Request: GET http://127.0.0.1:8081/api/spaces/5/concepts "HTTP/1.1 401"
WARNING app.runtime | analyze run 27 失败：后端返回 401：凭据无效
WARNING app.messaging.worker | 分析失败 ... code=RETRIEVAL_UNAVAILABLE
```

**根因**：Worker 启动时使用默认 `phase51-internal-token` / 默认签名密钥，
而后端与 Agent 使用 `phase21-internal-token` / `phase21-identity-secret`，
Worker 回访后端被 401。

**修复**：用与后端一致的环境变量复跑；没有修改业务逻辑、没有放宽鉴权。
复跑后 `PASS=46 FAIL=0`。

该失败是**环境配置错误**，不是代码缺陷；它同时证明“错误签名身份确实被后端拒绝”，
与安全报告的 D-13 一致。

### 3.5 幂等边界

- 消息级：相同 `taskId` 的重复投递由 `agent_run` 状态与 Result Consumer 收敛，
  不重复写 Evidence/Revision/Memory（MQ-2.1-20–25）。
- 请求级：SSE POST 没有幂等键，重复请求会重复创建 USER/ASSISTANT 消息与 CHAT run
  （SSE-2.1-06）。这是**未实现**，不是已修复。
- 并发级：本轮没有做高并发压测；只验证了重复投递的最终一致性，不能声称“高并发安全”。

## 4. 清理与残留

最终 MQ E2E 轮次：

```text
用户/空间/会话/run/Evidence/Memory 残留为 0
五个测试队列残留为 0
```

隔离库 `ails_chat_sse_it`、`ails_phase21_mq` 在测试结束后删除；没有改动开发库
`ails`。测试使用合成账号，没有真实用户数据。未执行 Git 提交。

## 5. 未覆盖 / 未实现

| 项目 | 状态 | 原因 |
| --- | --- | --- |
| SSE 第一个 delta 后、落库前断开 | NOT_COVERED | 现有用例只覆盖落库并 end 后断开 |
| SSE POST 幂等键 | NOT_IMPLEMENTED | 请求 DTO 无幂等键，服务端无法区分重试与再次提问 |
| 真实 LLM 质量 | BLOCKED | 见 `phase2.1-llm-stream-diagnostics.md`，只验证可识别输出 |
| 高并发 / 压测 | NOT_MEASURED | 没有基线，不生成数字 |
| Qdrant 知识链路本轮 | NOT_COVERED | MQ 场景知识文档数为 0；不把空知识当 RAG 通过 |
| Testcontainers 隔离 broker | BLOCKED | 本机 Testcontainers 仍无法连接 Docker；使用本地真实 RabbitMQ |
