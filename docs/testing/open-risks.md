# 风险与待决策（Phase 2.1 后）

- 日期：2026-10-27
- 范围：Agent 评测、安全边界、真实 HTTP SSE、真实 RabbitMQ/MySQL 消费链
- 原则：未执行 / 被阻塞的项目不计 PASS；未确认的产品契约不写成「正确」

## 状态总览

| 编号 | 风险 | 状态 |
| --- | --- | --- |
| R-P0-01 | Agent HTTP 端点无鉴权 | 已修复：共享 token + 短期签名身份；共享 token 仍有服务级残余风险 |
| R-P0-02 | Analyze 链路 Fixture 绕过降级保护 | 已修复 |
| R-P1-01 | 预算丢弃后谎称「没有历史」 | 已修复 |
| R-P1-02 | Token 预算口径不一致 | 已明确并修复 |
| R-P1-03 | History Guard 忽略按需 Evidence | 已修复 |
| R-P1-04 | auto 回退不可观测 | 已修复 |
| R-P2-01 | `recent_history_limit=0` 语义 | 已定契约：0 = 不注入，负数非法 |
| R-P2-02 | 两个 Guard 说明是否合并 | 待产品决策 |
| R-P2-03 | 真实模型上游空流 | **根因已确认**：reasoning 耗尽小预算 + `finish_reason=length`；语义质量仍 BLOCKED |
| R-P2-04 | 缺少 DB / SSE E2E | **已补齐**：SSE 7 用例、MQ 46 项；仍有未覆盖项 |
| R-P2-05 | 语义评分没有 LLM Judge | 仍用固定 rubric，未引入 Judge |
| R-P2-06 | SSE POST 无幂等键 | **已修复**：必填 `clientRequestId` + DB 唯一键；A/B/C、并发、回滚均有真实 MySQL 测试 |
| R-P2-07 | SSE 预流错误被内容协商改写成 401 | **已修复**，2 条回归测试 |
| R-P2-08 | preview 404 被标成 `backend_unavailable` 并回退 Fixture | 低危可观测性缺陷，非数据泄露 |
| R-ENV-01 | 测试替身不等于真实权限 | Phase 2.1 已用真实 HTTP + 真实 MySQL 补证 |
| R-ENV-02 | Fixture 规模有限 | 未变 |
| R-ENV-03 | Testcontainers 无法访问 Docker | 用本地真实 broker 测试替代 |
| R-ENV-04 | E2E 的 `@AfterAll` 删除传入的 Qdrant collection | 本次用独立 prefix 规避；测试代码强制隔离待补 |
| R-SEC-01 | 报告/日志密钥边界 | 已修复脚本回显 JWT；报告/日志无 token |
| R-SEC-02 | 后端默认监听 `0.0.0.0` | 部署风险；Agent/RabbitMQ 已限制回环 |

## P0

### R-P0-01 Agent HTTP 端点的鉴权（已修复，残余风险）

**修复前证据：** B07，无 Authorization 调用 `/chat`、`/chat/preview` 返回 200，
请求体 `userId` 被直接信任。

**Phase 2.1 后的机制：**

- `/agent/chat`、`/agent/analyze`、`/agent/context/preview` 要求
  `X-Internal-Token` + 短期签名身份 `X-Internal-Identity`；
- 用户身份只来自签名声明的 `sub`，`X-Internal-User-Id` 仅交叉核对；
- 未配置 token 时 fail closed（503）；缺少/篡改/过期/错 audience/错密钥均 401；
- 声明与 Header 或请求体 `userId` 不一致时 403，且到不了检索层；
- `/health` 只返回存活状态，诊断移到受保护的 `/health/diagnostics`。

**残余风险：** 共享 token 仍是服务级 bearer；同时持有 token 与签名密钥的一方
可以代表任意用户。它不是用户级认证，不能替代 mTLS / 服务网格 / 网络隔离。

**建议：** 生产环境限制 Agent 只监听 loopback 或受控内网；不要把 token/secret
写入日志、镜像层、前端；优先用 mTLS / 工作负载身份替代长期共享密钥。

证据：[phase2.1-security-results.md](phase2.1-security-results.md)。

### R-P0-02 Analyze 链路 Fixture 保护（已修复）

`build_retriever(..., allow_degradation=False, mode="fixture")` 现在抛
`RetrievalError`，且发生在任何 LLM 调用之前。Chat 链路仍可在
`allow_degradation=True` 时使用 Fixture。

验证：B06 PASS；`CountingLLM.calls == 0`；后端 Agent 失败路径
`writeService.apply` 从未调用，run 标记 FAILED，只退回 rules-v1。

## P1

### R-P1-01 预算裁剪后的「没有历史」文案（已修复）

被预算丢弃的认知区块现在会明确说明「检索到了，但超出预算未包含」，
不再说「没有检索到任何历史认知记录」。A06 PASS。

### R-P1-02 Token 预算口径（已明确并修复）

`max_context_tokens` = 动态上下文预算（标题/说明、section、消息、用户输入），
固定教学契约不计入；`estimated_tokens` 是完整 prompt 的真实估算。
超限时报告 `budget_exceeded` 与原因，不伪造数字。A07 PASS。

**已知限制：** 固定教学契约本身约 2266 tokens；当 `max_context_tokens` 小于
该值时，完整 prompt 必然超预算。这不是裁剪失败，而是预算定义如此，报告会明确写出。

### R-P1-03 History Guard 与按需 Evidence（已修复）

Guard 现在合并轨迹与 `state.evidences`，按 id 去重；记录计数不能超过实际记录；
「答对三道题」必须由 `TEST_RESULT` 支撑。B03 PASS。

**仍需注意：** 规则是句级/计数级的，不能证明所有语义蕴含都正确；它防的是
明显超出记录的事实断言，不是完整的自然语言推理校验。

### R-P1-04 auto 回退可观测（已修复）

preview / runtime 返回 `requestedMode`、`resolvedSource`、`degraded`、
`fallbackVisible`、`fallbackReason`。D07 PASS。

## P2 / 待决策

### R-P2-01 `recent_history_limit=0`（已定契约）

0 = 不注入任何历史；负数非法（构造时抛 `ValueError`）。E06 PASS。
该契约由本阶段确定，若产品另有定义需要重新评审。

### R-P2-02 两个 Guard 的兜底说明是否合并（待决策）

B04 当前 PASS 只表示两段说明都存在、范围各自限定、不互相冒充。
当前两段文案**不是**最终产品契约。仍需产品确认是否合并。

验证目标：最终回答不能同时声称「没有历史记录」和「历史证据证明某事」。

### R-P2-03 真实模型上游空流（根因已确认，质量仍 BLOCKED）

Phase 1 修复后重跑：B08 / D08 / E07 各 3 次，共 9 次 HTTP 200，全部
`outputChars=0`。

Phase 2.1 对照实测确认根因：`deepseek-flash` 先输出 `reasoning_content`；
`max_tokens=64` 时预算在 reasoning 阶段耗尽，`finish_reason=length`，
用户可见 `content` 为空。相同端点与模型在 `max_tokens=1024` 下可稳定返回
`finish_reason=stop` 的内容。旧实现把流式空内容报成笼统错误、把非流式空内容
静默返回 `""`；现已分别归类为 `empty_stream` / `reasoning_exhausted_budget` /
`length_limited` / `finished_without_content`，并加入有界重试。

**仍未确认：** 真实模型回答的语义质量。本次只验证“是否有可识别输出”，
没有跑 40 条评测集，也没有质量评分；不得写成 Agent 质量通过。

证据：[phase2.1-llm-stream-diagnostics.md](phase2.1-llm-stream-diagnostics.md)。

### R-P2-04 DB / SSE E2E（已补齐主体，仍有边界）

已执行并通过：

- 真实 MySQL：Evidence 去重、Revision 幂等、快照一致性、事务回滚、
  Memory 生命周期、跨会话读取；
- 真实 RabbitMQ 本地 broker：发布确认、重复投递、未 ACK 重投、TTL 重试、死信；
- 真实 Qdrant + 本地 mock Agent：文档索引 E2E；
- 真实 HTTP SSE（真实 Tomcat/MySQL/Security）：7/7 用例，覆盖正常顺序、
  落库后断开、持久化失败禁止分析、空流/模型错误终态、跨用户预流拒绝；
- 真实 RabbitMQ + MySQL + Worker：46/46 检查，含重复投递、重试、死信、
  FAILED 终态与 rules-v1 兜底、清理残留；
- 真实 HTTP 安全边界：20/20 检查（Agent 13 + 后端 7）。

仍未执行 / 未实现：

- SSE 第一个 delta 后、落库前客户端断开（NOT_COVERED）；
- SSE POST 幂等键（**已修复**，见 R-P2-06；Web 入口请求体大小 / 速率限制仍未实现）；
- 性能基线 / 压测（NOT_MEASURED）。

证据：[phase2.1-integration-results.md](phase2.1-integration-results.md)。

### R-P2-05 语义评分没有 LLM Judge（未变）

Phase 1 使用固定 rubric（关键词 + 禁止表述），确定性、可复现，但对同义表达
不敏感。rubric 不参与 PASS/FAIL。若引入 Judge，必须使用不同模型，且不能覆盖
DB / 权限 / 状态 / 路径断言。

### R-P2-06 SSE POST 没有幂等键（已修复，残余风险保留）

**原现象：** `MessageRequest` 只有 `content`，重复 POST 同一内容会创建两条 USER、
两条 ASSISTANT、两个 CHAT run；Evidence 由 dedupe_key 收敛，Revision 不重复
（SSE-2.1-06 实测）。

**修复（2026-11-08）：**

- `MessageRequest` 新增必填 `clientRequestId`（8–64 字符）；缺失 / 越界 / 内容超过
  8000 字符均在 DTO 校验层返回 400。
- `message` 增加 `UNIQUE(conversation_id, client_request_id)`，`agent_run` 增加
  `client_request_id` + `assistant_message_id` 及同样的唯一键；迁移见
  `schema-migration-chat-idempotency.sql`。
- 重放不重新生成，按状态返回 JSON：处理中 202 `PROCESSING`、已完成 200
  `COMPLETED/TRUNCATED`、USER 无 run 409 `INCONSISTENT`、失败 409 `FAILED`、
  同 id 不同内容 409 `CLIENT_REQUEST_CONFLICT`。详见
  [`../audit/KNOWN_LIMITATIONS.md#chat-01chat-post-无幂等键本批已修复残余风险见下`](../audit/KNOWN_LIMITATIONS.md)。
- 同一批把 USER 消息 + Session 归属 + CHAT run 收进一个预创建事务（CHAT-02），
  run 创建失败时整体回滚，不再留下「有提问、无 run」的半截数据。

**回归（真实 Tomcat + 真实 MySQL，`ChatSseIntegrationTest`，17/17 通过）：**

- `completedReplayReturns200AndDoesNotWrite`
- `inFlightReplayReturns202Processing`
- `userMessageWithoutRunReturns409Inconsistent`
- `sameClientRequestIdWithDifferentContentReturns409`
- `differentClientRequestIdsCreateSeparateTurns`
- `concurrentSameClientRequestIdCreatesExactlyOneTurn`
- `duplicateClientRequestIdRejectedByDatabaseConstraint`
- `runCreationFailureRollsBackWholeTurn`
- `invalidRequestFieldsReturn400`

旧的 `duplicatePostWithoutIdempotencyKeyCreatesDuplicateMessages` 已删除：它断言的
正是「没有幂等键时重复落库」这一待修复行为，修复后语义不再成立。其余 SSE 用例的
断言未改变，只是在请求体里补了随机 `clientRequestId`。

**仍存在的风险：** 预创建提交后、生成线程启动前进程崩溃会留下永久 `RUNNING` 的
CHAT run（无 stale 补偿）；`content ≤ 8000` 只是 DTO 校验，Web 入口的请求体大小
限制（`server.max-http-request-size` / 网关）与速率限制**尚未实现**，属于 TODO。

### R-P2-07 SSE 预流错误被改写成 401（已修复）

**现象：** 跨用户 POST 带 `Accept: text/event-stream`，期望 404，实际 401。

**根因：** 错误体按 `Accept` 协商失败，抛
`HttpMediaTypeNotAcceptableException`，被 Security 入口点改写。

**修复：** `GlobalExceptionHandler` 的错误响应强制
`Content-Type: application/json`。

**回归：** `GlobalExceptionHandlerTest` 2/2；真实 HTTP SSE 跨用户 404 用例。

### R-P2-08 preview 的降级标签与 Fixture 回退（低危）

**现象：** 合法身份 user 9001 访问不存在的 space1 时，preview 返回 200 与
`understandings=1, memories=2`；响应同时标注 `degraded=true`、
`fallbackReason=backend_unavailable`，Agent 日志显示后端实际返回 404。

**结论：** 不是跨用户泄露。身份校验通过后，检索按 9001 访问后端；404 触发
`FallbackRetriever` 回退到本地 Fixture，数据是样例而非他人数据。user6/space6
的 preview 走真实 HTTP（`degraded=false`），可对照。

**风险：** 404（无数据/无权）被标成“后端不可用”，且 preview 在身份已绑定时仍
展示样例，容易误判。分析链路 `allow_degradation=False`，不会把 Fixture 写进
真实认知。建议后续区分 4xx 与连接失败，并在 debug preview 中显式标注“样例数据”。

## 环境与数据风险

### R-ENV-01 测试替身不等于真实权限（已部分缓解）

Phase 2 已用真实 MySQL、JWT、Spring Security 过滤器补证跨用户/跨空间；
Agent 侧 A09/A10 仍只证明 Agent 按身份调用检索。

### R-ENV-02 Fixture 规模有限（未变）

2 用户 / 3 空间 / 5 文档，不覆盖多文档同概念、分页、超长文档、
真实向量阈值、并发权限变更。

### R-ENV-03 Testcontainers 无法访问 Docker

`AnalysisMqIntegrationTest` 在本机被 abort（0 tests），记 BLOCKED。
已新增 `Phase2RabbitMqBrokerTest` 直连本地 RabbitMQ，覆盖同样的 broker 语义；
但 Testcontainers 隔离环境仍未验证。

### R-ENV-04 文档 E2E 会删除传入的 Qdrant collection

`DocumentIndexE2EIntegrationTest` 的 `@AfterAll` 会删除
`AILS_E2E_QDRANT_COLLECTION` 指定的 collection。若传入共享名字，会删除演示/
其他环境正在使用的向量数据。本次已改用独立 prefix
`ails_e2e_phase2` 规避；测试代码仍未强制拒绝非专用前缀，存在误删风险。
建议在测试内生成随机后缀，或在启动时校验 prefix 必须包含 `e2e`/`test`。

### R-SEC-01 报告与日志的密钥边界

- 没有把 API Key、密码、Token、个人信息写入报告或 Git；
- 已移除测试代码中的默认数据库密码字面量，改为必须由
  `AILS_IT_MYSQL_PASSWORD` 提供；
- `RecordingLLMAdapter` 只记录 prompt 字符数与 SHA-256，不记录完整 prompt；
- 原始输出截断并脱敏 Bearer、`sk-*`、key/token/password/secret、邮箱；
- Phase 2.1 发现 `phase51-e2e.sh` / `phase51-mq-e2e.sh` / `phase52-e2e.sh`
  的“注册并取得 token”检查会把 JWT 打进日志，已改为只输出“已取得 token”；
  历史 artifact 中的 JWT 已脱敏，最终复跑日志无 `eyJ`；
- 建议提交前运行 `git diff --cached` 与密钥扫描，确认没有误带 `.env`。

### R-SEC-02 后端默认监听所有网卡（部署风险）

实测 Agent `127.0.0.1:8000/8010`、RabbitMQ `127.0.0.1:5672/15672`，但 Spring
Boot 使用默认绑定 `0.0.0.0:8081/8099`。内部接口仍需 token + 签名，JWT 接口仍需
用户认证；但在没有反向代理/防火墙/容器网络策略证明边界前，不能声称“只在内网”。

**建议：** 生产通过 `server.address=127.0.0.1` 或反向代理隔离后端；限制
`AILS_INTERNAL_TOKEN` / 签名密钥的分发范围；部署后用外部主机验证端口不可达。

## 建议的下一步

1. ~~为 SSE POST 增加幂等键并补真实 HTTP 重复请求回归（R-P2-06）~~ —— 已完成
   （2026-11-08，见 R-P2-06）；剩余：Web 入口请求体大小 / 速率限制与 CHAT run
   stale 补偿；
2. 补 SSE 第一个 delta 后断开的用例，明确“已开始生成但未落库”的契约；
3. 在独立环境做性能基线，单独报告；不要用本轮小样本推算；
4. 确认 B04 文案合并；
5. 评估 mTLS / 工作负载身份替代共享 token，并落实后端监听地址策略。

---

# Phase 3.1 风险更新（2026-10-28）

本节补充内部身份签发边界与评测器校准后的风险。前文的 Phase 2.1 结论保留为历史记录；
与本节冲突时，以本节为准。

## 状态总览

| 编号 | 风险 | 状态 |
| --- | --- | --- |
| R-SEC-03 | Agent 可用共享 HMAC 密钥自签后端接受的任意用户身份 | **已修复**：delegation-secret 只给 Spring，Agent 只转发 |
| R-SEC-04 | `jti` 不是重放防护，委托声明在 TTL 内可重复使用 | **接受的风险**：无 replay cache、无撤销 |
| R-SEC-05 | 委托声明随 MQ 消息持久化，队列/死信中可被读取 | **接受的风险**：TTL 900s、最大 3600s；依赖队列访问控制 |
| R-SEC-06 | 共享 `X-Internal-Token` 仍是服务级 bearer | **未消除**：不能单独建立用户身份，但与委托声明同时泄露可放大 |
| R-SEC-07 | Agent 运行环境若被注入 `AILS_INTERNAL_DELEGATION_SECRET`，CLI 可重开后端签发能力 | **部署约束**：正常部署不设置；需配置审计 |
| R-SEC-08 | 后端未设置 `server.address`，默认监听 `0.0.0.0` | **未验证**：实际容器映射/防火墙未取证 |
| R-SEC-09 | 无密钥轮换/撤销 | **未实现**：只靠短期 `exp` 自然过期 |
| R-EVAL-01 | 截断回答被当成完整任务 PASS | **已修复**：`completion_status` 独立，TRUNCATED 强制 FAIL |
| R-EVAL-02 | B05 要求 Guard 重复追加，E01 关键词误判 | **已修复**：结构化 Guard 字段、从句方向判断 |
| R-EVAL-03 | D01 malformed 引用因 `invalid=0` 通过 | **已修复断言**；模型仍会输出 `[资料#0]`，新断言判 FAIL |
| R-EVAL-04 | A01 合规免责句触发 Guard 回退 | **代码已修复**；旧 artifact 的回退结果未追溯修复 |
| R-EVAL-05 | Judge 与 Agent 同 provider，且没有人工分 | **未验证**：不能宣称校准或区分度 |
| R-EVAL-06 | 9 条人工样本量小、分布缺少变化 | **未解决**：只能做初步观察，不能算加权 Kappa 证明 |
| R-MQ-01 | 任务消息 schema 1→2，旧消息被拒绝 | **已接受**：v1 消息转死信；不可滚动升级，需协调切换（见 Phase 3.3 R-MQ-07） |
| R-ENV-05 | 安全集成测试使用空密码 MySQL 容器 | **仅本地测试**：独立库、独立端口；不是生产配置 |

## P0

### R-SEC-03 Agent 自签后端身份（已修复）

**修复前：** Agent 使用 `internal_identity_secret` 签发 `aud=learning-backend`
声明，后端接受并建立用户上下文。持有 Agent 密钥即可代表任意用户。

**修复后：** `delegation-secret` 只存在于 Spring；Spring 在
`AnalysisTaskMessage.backendIdentity` 中下发，Agent/Worker 原样转发。真实 HTTP
测试 `agentIdentitySecretCannotForgeBackendAcceptedUser` 返回 401；
Agent 测试 `test_agent_identity_secret_cannot_authorize_backend_reads` 确认后端
stub 收到 0 次请求。

**残余：** 见 R-SEC-04/R-SEC-05/R-SEC-06。修复的是“签发权”，不是“重放”。

### R-SEC-04 委托声明可重放（接受的风险）

`jti` 只校验非空；没有已用 `jti` 存储。声明在 `exp` 前可重复使用。
默认 TTL 900 秒、最大 3600 秒。影响范围受 `scope=analysis:read`、GET-only、
空间归属校验限制。

**如果要进一步降低：** 缩短 TTL；按 `jti` 做一次性消费；把声明绑定到请求
（如 mTLS 通道绑定）；建立撤销列表。当前均未实现。

### R-SEC-05 MQ 中的声明副本（接受的风险）

任务消息在队列、重试队列和死信中持久化。能读取这些队列的一方可以在 TTL 内
复用 `backendIdentity`。当前依赖 RabbitMQ 访问控制与内网隔离，未在本阶段验证。

### R-SEC-06 共享 token 的放大效应（未消除）

共享 token 单独不能建立用户身份（`sharedTokenWithoutSignedIdentityCannotImpersonateAUser`
401）。但若同时拿到有效委托声明，就能在 TTL 内代表该用户读取。它不是用户级
认证，不能替代 mTLS/服务网格。

### R-SEC-07 delegation 密钥误注入 Agent（部署约束）

`agent/app/internal_identity.py` 的开发 CLI 在显式提供
`AILS_INTERNAL_DELEGATION_SECRET` 时能签发后端声明。Agent 的运行时配置
（`agent/app/config.py`）不读取该变量，正常请求路径也不调用签发函数。需要在
部署检查中确认该变量没有进入 Agent 容器/环境。

## P1

### R-EVAL-01 截断（已修复）

`completion_status` 独立于 `case_status`；`finish_reason=length` 直接 FAIL，
`task_completion` 从 2 降到 1，并标记 `requiresHumanReview`。
B05-r1 / E01-r2 的旧 artifact 已重判为 TRUNCATED。

### R-EVAL-02 B05/E01（已修复）

- B05：不再要求 `added=True`；检查 `finalDiscloses`、`duplicateNotice`、
  `addedOrDisclosed`；
- E01：`masteryClaim.affirmativeCurrent` 从句方向判断，区分肯定/否定/历史/问句。

### R-EVAL-03 D01 引用语法（断言已修复，模型行为仍存在）

新断言能抓住 `malformedLabels`，历史与新真实运行都因此 FAIL。
这是**真实缺陷暴露**，不是评测器退步。后续应修改提示词/后处理，或明确允许
额外格式；在决定前不把 malformed 标签判为通过。

### R-EVAL-04 A01 History Guard（代码已修复，artifact 未重跑）

新 Guard 在合规免责句样本上不再误报，且有“不得放过编造历史”的反例。
但 A01-r2 的旧 artifact 仍保存着重生成/回退结果；本阶段没有重新调用该用例，
因此不能声称该次回答已被修复。

### R-EVAL-05/06 Judge 与人工评分（未验证）

- 自动 Judge 10/10 成功，但适用维度几乎全是 2 分，缺少 0/1 变化；
- 人工评分 0/9，未计算一致率/Kappa；
- Judge 与 Agent 同 provider，独立性不足；
- 9 条样本只够做初步观察，不足以证明校准。

## P2

### R-MQ-01 schema v2 的升级顺序

v2 任务消息要求 `backendIdentity`。旧 Worker 不认识 v2 会拒绝并转死信；
新 Worker 认识 v2、拒绝 v1。滚动升级时应先升级 Worker，再让后端投递 v2。
本阶段未执行真实 MQ E2E，升级顺序只是代码层面的结论。

### R-SEC-08 监听地址（未验证）

`backend/backend/src/main/resources/application.yml` 只配置了
`server.port: 8080`，没有 `server.address`；Spring Boot 默认可能绑定
`0.0.0.0`。Agent 默认 `127.0.0.1`。实际容器端口映射、防火墙、安全组没有在
本阶段取证，不能写“已限制为内网”。

### R-ENV-05 测试数据库凭据

安全集成测试使用本地 MySQL 8.0.37 容器（127.0.0.1:3307），root 空密码，
独立库 `ails_security_it`。它只用于测试，不是生产配置；不得复制到部署环境。

---

# Phase 3.2 风险更新（2026-10-28）

本节补充完整 MQ E2E 与跳过测试审计后的状态。前文保留为历史记录；与本节冲突时，
以本节为准。

## 状态总览

| 编号 | 风险 | 状态 |
| --- | --- | --- |
| R-MQ-01 | schema v2 升级顺序 | **已用真实 E2E 验证拒绝路径**；Phase 3.3 进一步确认“先升 Worker”不足以保证安全，必须协调切换（见 R-MQ-07） |
| R-MQ-02 | 生产/消费链路未端到端验证 | **已消除（本轮范围）**：真实 Spring→RabbitMQ→Worker→HTTP→MySQL，109 PASS / 0 FAIL |
| R-MQ-03 | 非法/旧版本消息可能写入学情 | **已验证拒绝**：5 个场景 FAILED、死信 +5、retry +0、无 Agent 学情写入 |
| R-MQ-04 | 失败消息 `error_message` 可能回显消息字段 | **低危观察（未修改）**：本轮未出现 token；建议后续对 pydantic `input_value` 脱敏 |
| R-MQ-05 | MQ E2E 仍为手工脚本，未进 CI | **未解决** |
| R-EVAL-07 | 评分表泄露自动 Judge 分数，产生锚定 | **已缓解**：新增盲评版，移除自动分/自动状态/判读理由 |
| R-EVAL-08 | 人工评分仍为 0/9 | **未解决**：不能算一致性、不能称校准 |
| R-SKIP-01 | 关键链路测试仍被门控跳过 | **已审计并列为未覆盖风险**：SSE、RAG/Qdrant、索引状态机、MQ 回滚/trace |
| R-SEC-04/05/08 | 委托声明可重放、MQ 持久化副本、监听地址未取证 | **未变化**：E2E 不改变这些安全结论 |

## P1

### R-MQ-02 完整 MQ E2E（已完成本轮范围）

执行时间 2026-10-28，产物
`backend/backend/target/phase51-mq-e2e/20261028-004526/`。

- 真实 Spring 生产者（`delegation-secret` 签发 v2）→ 真实 RabbitMQ → 真实
  Worker → 真实后端 HTTP → 隔离 MySQL；
- 合法场景：ANALYSIS `SUCCEEDED`，Evidence 1（`USER_EXPLANATION/PARTIAL/agent-v1`）、
  Understanding `EXPOSED`、Revision 1、Memory 1（`CANDIDATE/GLOBAL`，绑定消息来源）；
- 非法场景：missing/empty/blank → `MESSAGE_INVALID`；v1/v3 →
  `TASK_SCHEMA_UNSUPPORTED`；全部 `FAILED`、死信 +5、retry +0、无 Agent 学情写入；
- 重复投递：2 次消费，1 份业务行，1 次 `DUPLICATE`；
- 401 重试：retry 副本被新 Worker 消费后 `SUCCEEDED`，`retries=1`；
- 清理：队列与用户数据残留 0，任务 JSON（含短期委托声明）已删除。

**边界：** Mock LLM、hash embedding、无知识文档命中、单 Worker、单节点 broker；
因此只证明契约/状态/落库，不证明语义质量、RAG 或性能。

证据：[phase3.2-mq-e2e-results.md](phase3.2-mq-e2e-results.md)。

### R-MQ-03 旧版本消息没有兼容路径（已接受）

`schemaVersion=1` 与 `3` 都返回 `TASK_SCHEMA_UNSUPPORTED` 并进死信；没有
v1→v2 转换分支。这是明确契约，不是缺陷。滚动升级必须先升级消费者/Worker，
再让 Spring 发 v2；降级时旧 Worker 会拒绝 v2。已在本轮真实 E2E 中复现。

### R-MQ-04 错误栏可能回显输入片段（低危，未修改）

非法消息的 `error_message` 保存了 pydantic 校验错误文本，其中包含
`input_value` 的截断摘要。本轮 missing/empty/blank 只出现
`backendIdentity` 的空值或空白，**没有出现签名 token**；但这属于通用模式，
未来其他字段校验失败可能把消息字段带入数据库错误栏。

**建议：** 在 Worker 记录/发布失败原因时，只保留字段路径与错误类型，或对
`input_value` 做白名单/脱敏。本轮没有因此判定生产者/消费者不兼容，也没有
修改业务逻辑。

### R-SKIP-01 关键跳过测试（已审计，仍未覆盖）

41 条后端 skipped 与 5 条 Agent skipped 已逐条审计，关键未覆盖项：

- **S-01 SSE 真实 HTTP**：`ChatSseIntegrationTest` 7 条全跳过；顺序、断开、
  持久化失败、空流/模型错误终态没有真实证据；
- **S-02 RAG/Qdrant**：`DocumentIndexE2EIntegrationTest` 1 条 + Agent 3 条；
  真实索引、检索权限、来源可追溯未验证；本轮 E2E 知识检索为 `NO_MATCH`；
- **S-03 文档索引状态机**：`DocumentIndexServiceIntegrationTest` 9 条；
  READY/回滚/幂等没有真实 MySQL 证据；
- **S-04 MQ 写库回滚与 trace 不一致**：`Phase2MqProcessorIntegrationTest` 2 条；
- **S-05 Revision 幂等与跨会话**：`Phase2RealStateIntegrationTest` 2 条；
- **S-06 Broker 未 ACK 重投/TTL**：2 条后端 + 2 条 Agent；仅部分覆盖；
- **S-07 文档归属/软删**：`DocumentServiceIntegrationTest` 7 条；部分覆盖。

**不能因为 MQ E2E 109/0 就说这些链路已通过。**
证据：[phase3.2-skipped-audit.md](phase3.2-skipped-audit.md)。

### R-EVAL-07 评分锚定（已缓解）

Phase 3.1 评分表在每条记录中展示自动 Judge 分数、自动 `case_status` 与判读理由。
新增 [phase3.2-human-review-blind.md](phase3.2-human-review-blind.md) 盲评版
（准备情况见 [phase3.2-agent-review-readiness.md](phase3.2-agent-review-readiness.md)）：
移除自动分、自动状态、判读理由与复核原因，保留回答全文、上下文事实、
完成状态、rubric、理由与备注栏。原表保持不变，供完成盲评后对照。

### R-EVAL-08 人工评分仍为 0/9（未解决）

9 条记录全部 `PENDING`；没有人工分，因此：

- 不能计算 Judge–human 一致率或 Kappa；
- 不能宣称 Judge 已校准或有区分度；
- 不能宣称 Agent 语义质量已通过。

样本只有 9 条，即使全部填写，也只能做“有限样本的一致性观察”。

## P2

### R-MQ-05 E2E 未进 CI（未解决）

`scripts/phase51-mq-e2e.sh` 需要真实后端、RabbitMQ、MySQL、Agent 与 Worker，
仍是手工脚本。建议在 CI 中至少跑“合法 v2 + 非法 v2 + 重复投递”子集，并保留
完整脚本作为发布前检查。

### R-MQ-06 Mock LLM / hash embedding 的证明边界

本轮 E2E 的 Agent 使用 Mock LLM 与 hash embedding。它能证明：

- 消息契约、身份转发、状态机、幂等、死信、清理；
- Evidence/Understanding/Revision/Memory 的最终落库形状。

它不能证明：

- 真实模型的事实正确性、教学效果、引用质量；
- 真实 embedding/Qdrant 的召回、权限过滤与排序；
- 真实模型的流式行为与 `finish_reason` 分布。

### R-SEC-04/05/08 安全结论未因 E2E 改变

- `jti` 仍不是重放防护；
- 委托声明仍随 MQ 消息持久化，TTL 内可重放；
- 后端仍默认监听 `0.0.0.0`，部署网络隔离仍未取证；
- E2E 全程 loopback，不能作为生产网络边界的证据。

---

# Phase 3.3 风险更新（2026-10-28）

本节补充 schema 升级评审、过期委托声明与部署面实测。与本节冲突时以本节为准。

## 状态总览

| 编号 | 风险 | 状态 |
| --- | --- | --- |
| R-MQ-07 | v1/v2 混合窗口没有安全滚动升级 | **已确认**：两个方向都拒绝；必须协调切换 |
| R-MQ-08 | 过期 `backendIdentity` 重试与 DLQ 保留 | **有界但未消除**：3 次执行/2 次等待后 FAILED+死信；DLQ 仍存过期声明 |
| R-SEC-09 | 后端监听所有网卡、外部网络边界 | **部分取证**：容器经 LAN IP 可达；外部主机/生产隔离未验证 |
| R-SEC-10 | Agent 无启动门禁阻止 delegation 密钥注入 | **Phase 4 已修复**：非空密钥启动失败；只覆盖运行配置误注入 |
| R-EVAL-09 | 人工评分仍为 0/9 | **未解决** |
| R-TEST-01 | 下一阶段测试优先级 | **已排序**：先真实 Qdrant + 来源追溯，再 SSE 边界、索引/回滚 |

## P1

### R-MQ-07 混合版本窗口（已确认，不可滚动升级）

当前工作树：

- Spring 生产者 `AnalysisTaskMessage.SCHEMA_VERSION = 2`，发布前用
  `delegation-secret` 签发 `backendIdentity`；
- Python Worker `TASK_SCHEMA_VERSION = 2`，只接受 v2；
- `git HEAD` 的旧版本是 producer/consumer 都为 v1，旧 Worker 只认
  `schemaVersion == 1`。

四种组合中，v1→新消费者、v2→旧消费者都会得到
`TASK_SCHEMA_UNSUPPORTED` 并进死信。因此“先升消费者、再升生产者”只保证消费者
就绪，不保证窗口安全；窗口内所有分析任务都会失败并触发 rules-v1 兜底。

**要求：** 采用协调切换——停生产者 → request/retry 深度排空（等待至少
retry TTL 15s）→ 升消费者 → 一次性升生产者 → 恢复流量。DLQ 归档，不重放。
回滚同样要先停生产者并隔离 v2 任务。

**当前流程不能证明可安全升级：** 仓库没有部署流水线、蓝绿/金丝雀、队列深度
门禁或版本握手；CI 只跑测试，`docker-compose.yml` 只有本地基础设施。该契约是
“已评审的部署要求”，不是已验证的生产行为。

证据：[phase3.3-schema-rollout-review.md](phase3.3-schema-rollout-review.md)。

### R-MQ-08 过期委托声明（有界，未消除）

- 签发：`AnalysisTaskService.enqueue` 在 `createRun` 后、`publish` 前立即调用
  `signDelegation`；默认 TTL 900s，最大 3600s，skew 5s；
- 存储：`backendIdentity` 随任务 JSON 原样进入 request/retry/DLQ；retry 和
  DLQ 都复制 body，不脱敏；DLQ 无 TTL；
- 过期：后端 401 → `RetrievalError` → `RETRIEVAL_UNAVAILABLE`（可重试）；
- `analysis_max_retries=3` 实际是“最多 3 次执行、2 次 retry 等待”，约 30s；
- 结果：`FAILED` + 死信，`retryable=False`；不写 Evidence/Revision/Memory，
  不产生 Agent 成功状态；rules-v1 兜底是独立 run；
- 不会无限重试，但过期声明会留在 DLQ，`jti` 仍不防重放。

本轮新增确定性回归：
`test_expired_backend_identity_is_bounded_and_dead_lettered`、
`test_expired_delegation_claim_is_rejected_for_backend_audience`、
`test_fatal_401_from_backend_is_retrieval_error`。全量 Agent：
**331 passed / 5 skipped**。

**未做：** 没有把真实过期 token 跑到耗尽的完整 E2E；没有改 401 分类、TTL 或
错误码。建议下一阶段评估更具体的 `AUTH_*` 错误码，并对队列最老消息年龄告警。

证据：[phase3.3-security-boundary.md](phase3.3-security-boundary.md)。

### R-SEC-09 监听地址与网络边界（部分取证）

- 后端实测监听 `0.0.0.0:8081` 与 `[::]:8081`；`application.yml` 无
  `server.address`；
- 从 `ubuntu:24.04` 容器经宿主 LAN IP `10.98.121.179:8081` 建立 TCP 成功；
  容器自身 loopback 8081 为 CLOSED（对照）；
- 宿主访问 LAN IP 返回 401；
- 防火墙 Domain/Private/Public 均启用，默认入站 `NotConfigured`，没有显式
  TCP 8081 规则；
- Docker 本地 MySQL/RabbitMQ/Qdrant 均只绑定 `127.0.0.1`；
- **未验证：** 另一台物理主机是否可达、生产防火墙/安全组/反向代理/TLS。

不能写“后端只在内网”或“生产网络已隔离”。

证据：[phase3.3-security-boundary.md](phase3.3-security-boundary.md)。

### R-SEC-10 Agent 启动门禁缺失（Phase 4 已修复，边界有限）

**Phase 3.3 的原始观察：**

- 正常启动的 Agent 进程环境没有 `AILS_INTERNAL_DELEGATION_SECRET`；
- 但额外注入该变量后，Agent 进程仍正常启动并保持存活；
- `Settings` 使用 `extra="ignore"`，不会因未知变量失败；
- `internal_identity.py` 的开发 CLI 在显式提供该变量时可签发后端声明。

**Phase 4 修复：** `agent/app/config.py` 增加
`internal_delegation_secret` 启动校验。缺失/空白正常启动；进程环境、`.env`
或 kwargs 中出现非空值则 `ValidationError`，HTTP/Worker 均无法启动；错误信息
不回显值；`describe()` 不包含该字段。

**证据：** 6 个配置单测通过；Agent 全量 **337 passed / 5 skipped**；注入值
真实启动 exit code 3、密钥值出现 0 次；不注入时 `/health` 200，停止后端口
关闭。见 [phase4-rag-index-results.md](phase4-rag-index-results.md) §0。

**边界：** 这不是身份协议的一部分，也不能阻止拥有修改代码或启动参数权限的
人。它只覆盖“运行配置里被误注入该密钥”。不要把它描述成对恶意启动者的防护。

### R-EVAL-09 人工评分仍为 0/9（未解决）

盲评表 9 条结构完整、可读；`humanScores`/`humanNotes`/`humanDecision` 全为
null，状态全 `PENDING`；没有自动分、没有预填、没有一致性结论。继续禁止把
自动 Judge 分改名为人工分。

证据：[phase3.3-agent-review-status.md](phase3.3-agent-review-status.md)。

## P2

### R-TEST-01 下一阶段优先级

1. **Agent 真实 Qdrant 检索 + 文档来源可追溯**（第一优先）：当前 E2E 是 hash
   embedding、知识 `NO_MATCH`，RAG 没有真实证据；本地已有 Qdrant，不需要新依赖。
2. **后端真实 SSE 边界**：7 条 `ChatSseIntegrationTest` 仍跳过；补顺序、断开、
   落库失败、空流/错误终态。
3. **文档索引状态机 + MQ 写失败回滚/状态一致性**：9 + 2 + 2 条跳过；
   在真实检索链路跑通后补。
4. 语义评分/Judge 校准、跨会话、并发/吞吐暂缓；没有人工分或真实环境时不生成
   结论或数字。

### R-SEC-04/05/08 仍然成立

- `jti` 不是重放防护；
- 委托声明随 MQ 持久化，TTL 内可重放，DLQ 中可能超过 TTL 仍保留；
- 后端仍监听所有网卡；部署隔离未取证。

### R-MQ-05 E2E 仍未进 CI

Phase 3.2 的 109 PASS / 0 FAIL 仍是手工脚本结果；Phase 3.3 没有改变这一点。
建议至少把“合法 v2 + 非法 v2 + 混合版本拒绝”纳入发布前检查。

---

# Phase 4 风险更新（2026-10-28）

本节补充真实 Qdrant/Embedding 的 RAG 端到端与来源追溯实测。与本节冲突时
以本节为准；Phase 3.3 的 R-SEC-10 已在本轮修复，其余历史条目仍然成立。

## 状态总览

| 编号 | 风险 | 状态 |
| --- | --- | --- |
| R-RAG-01 | 真实索引/检索链路 | **已验证（隔离合成小样本）**：4 READY 文档、7/7 索引检查、Recall@5 1.0、MRR 0.9 |
| R-RAG-02 | SUCCESS + 知识被预算全丢时无说明 | **未修复**：`CONTEXT_BUDGET` 缺口 |
| R-RAG-03 | 中断索引永久停在 PROCESSING | **未修复**：无租约/超时/启动回收 |
| R-RAG-04 | 合法引用可能把多来源句子标到单一来源 | **已观测**：2 条 `numericConflict`；未改校验器 |
| R-RAG-05 | Recall/MRR 样本极小、阈值未校准 | **未校准**：N=5、每条 1 chunk |
| R-EVAL-10 | 真实模型语义质量 | **未评审**：6 例硬断言通过，无人工分 |
| R-SEC-10 | Agent delegation 密钥启动门禁 | **已修复**：边界见 Phase 4 索引报告 §0 |

## P1

### R-RAG-01 真实 RAG 主链路（已验证，范围有限）

在隔离 DB/Qdrant prefix 上使用真实 `text-embedding-v4`（1024 维）和真实
Qdrant，跑通：

```text
Spring 上传 → PROCESSING → Agent parse/chunk → 真实 Embedding
→ MySQL chunk + Qdrant point → READY → ready-ids
→ 真实查询向量 + user/space/document 过滤 → Context Builder → 真实 LLM
```

证据：

- 4 个 READY 文档的 DB/Qdrant 文档 ID、chunk ID、正文、用户/空间、文件名
  全部一致；collection 维度 1024；
- FAILED 文档无 chunk/point、不进 ready-ids；PROCESSING 文档同样；
- A1 的 ready-ids 精确为 `[20, 21]`，A2 为 `[22]`，B1 为 `[23]`；
- 5 条标注查询 Recall@5=1.0、MRR=0.9（N=5，K=5，小样本，不可外推）；
- 跨用户/跨空间 Qdrant 直查 3 个应零命中组合全部 0 命中；
- 6 个真实模型用例硬断言通过，16 条引用 0 非法、0 malformed、0 非注入。

**不能写：** “生产 RAG 已通过”“Recall/MRR 代表生产效果”“语义质量已验证”。
本轮全部是合成数据、单 chunk 文档，没有并发/吞吐/大规模/真实用户数据。

证据：[phase4-rag-index-results.md](phase4-rag-index-results.md)、
[phase4-rag-retrieval-results.md](phase4-rag-retrieval-results.md)、
[phase4-rag-agent-results.md](phase4-rag-agent-results.md)。

### R-RAG-02 SUCCESS 检索 + 知识被预算全丢（Phase 4.1 已处理，见文末）

12 条长历史下，`contextTokens=2836/3000`；两个知识 chunk 都被丢弃
（`knowledge:18`、`knowledge:19`），但 `knowledge_status` section 不存在。
原因是 `_KNOWLEDGE_STATUS_NOTES` 不含 `SUCCESS`，`GUARD_STATUSES` 也不含
`SUCCESS`。模型看到“没有知识 + 没有状态说明”，无法区分“没检索到”和
“检索到但预算放不下”。

本轮没有在该上下文下调用真实 LLM，因此不能声称已经造成具体错误回答；这是
结构性 `CONTEXT_BUDGET` 缺口。建议单独注入“检索到但预算未注入”的说明，
不要把 `SUCCESS` 直接塞进现有 Guard。证据：
[phase4-rag-agent-results.md](phase4-rag-agent-results.md) §2.3、
[phase4-failure-analysis.md](phase4-failure-analysis.md) §2。

### R-RAG-03 中断索引永久 PROCESSING（Phase 4.1 已处理，见文末）

后端在索引中被杀后，文档 3 保持 `PROCESSING`、`chunk_count=0`、无 chunk/
point、`index_error=NULL`。它不会进 ready-ids，所以不会污染检索；但没有
租约/超时/启动扫描把它转成 FAILED，用户会看到永久“处理中”。

建议：加 `processing_started_at` + 最大时长，超时转 FAILED（可重试）；
启动时扫描孤儿；加回归测试。证据：
[phase4-rag-index-results.md](phase4-rag-index-results.md) §2.3。

## P2

### R-RAG-04 多来源单引用（Phase 4.1 已缓解，见文末）

6 个真实模型用例的 16 条引用全部合法，但 2 条 `numericConflict=true`：
句子包含“12 秒”，被引的现行规范 chunk 只写 17 秒和“旧值”，没有 12。
这不是非法引用，而是把两个来源的事实合并成一句、只标了一个来源。保留
`numericConflict` 作为信号；建议 Prompt 要求多来源数字分别标注，或拆句。
证据：[phase4-rag-agent-results.md](phase4-rag-agent-results.md) §3.3。

### R-RAG-05 小样本与阈值未校准

Recall@5=1.0、MRR=0.9 来自 5 条查询、每条 1 个相关 chunk；无负样本池、
无多相关 chunk、无 reranker；`min_score=0.35` 未校准。不能外推为生产指标。
下一阶段应扩到多 chunk、多文档、负样本和不同模型，再谈阈值。

### R-EVAL-10 真实模型语义质量仍缺人工证据

6 个真实模型用例（5 个主用例 + 1 个错误前提补充）硬断言 6/6 通过；人工
快速阅读未发现事实错误，但：

- 没有人工评分；
- 没有校准 Judge；
- 没有一致性/显著性结论；
- 语义质量状态仍是 `UNREVIEWED`。

继续禁止把硬断言通过、引用合法或自动分写成“语义质量通过”。人工评审队列
仍需保留；Phase 3.3 的 9 条盲评仍是 0/9，本轮新增用例也未评分。

### 安全与部署边界没有因本轮改变

- R-SEC-09 的后端监听所有网卡、外部主机/生产隔离未验证仍然成立；
- R-SEC-10 的运行配置守卫已修复，但不防御可改代码/启动参数的人；
- 没有调整身份系统、委托 TTL、MQ 契约或网络配置。

---

# Phase 4.1 风险更新（2026-10-28）

本节更新 Phase 4 的 `R-RAG-02/03/04`，不改写前文的原始证据；前文标题已加
指向本节的说明。

## 状态总览

| 风险 | Phase 4.1 后状态 | 证据边界 |
| --- | --- | --- |
| R-RAG-02 预算丢弃无说明 | **已修复（结构）**，有受控真实模型样本 | 非在线 RAG、单一模型、单样本 |
| R-RAG-03 索引永久 PROCESSING | **已修复（隔离环境）** | 真实 MySQL + 假 Agent；无真实 Qdrant/生产调度 |
| R-RAG-04 多来源单引用 | **已缓解**，Prompt + 诊断 + 6 次真实样本 | 仍有随机合并反例；语义未人工评分 |
| R-EVAL-10 真实模型语义质量 | **仍未解决** | 人工评分 0/17，无 Judge 校准 |
| R-RAG-05 小样本/阈值未校准 | **未改变** | 仍是 N=5、单 chunk、无负样本 |

## P1

### R-RAG-02 已修复：检索成功但预算全丢时明确说明

- Context Builder 在预算裁剪后计算 `knowledge_state`：
  `SUCCESS` / `SUCCESS_PARTIAL` / `SUCCESS_BUT_DROPPED`；
- 预算后补入状态区块，明确「检索成功，片段全部因 Token 预算未放入」；
- Guard 使用最终状态，`SUCCESS_BUT_DROPPED` 不进入 `GUARD_STATUSES`，
  不再追加与 Context 矛盾的「没有资料」；
- 确定性测试覆盖全部进入、全丢、部分丢、无匹配，并检查实际渲染 Prompt
  与 `AgentRuntime.generate()` 输出；
- 受控真实模型样本（固定 chunk、12 条合成历史、4 条合成记忆、3000 token）
  得到 `SUCCESS_BUT_DROPPED`，注入 0、丢弃 18/19，回答明确承认「检索命中但
  预算没放进来」。

**残余风险**：仍只有一个模型、一个问题、一次调用；没有在线 RAG 重跑；
`SUCCESS_PARTIAL` 没有真实模型样本；语义质量未评分。

证据：[phase4.1-context-budget-results.md](phase4.1-context-budget-results.md)。

### R-RAG-03 已修复：超时 PROCESSING 收敛到 FAILED

- 复用 `updated_at` 作为索引启动时间近似值，未改表；
- 默认阈值 15 分钟，可配置；定时扫描首次延迟 60 秒、间隔 5 分钟；
- 条件更新 `status='PROCESSING' AND updated_at < cutoff` 保证幂等，
  不覆盖 READY，不重写已回收错误；
- 只转 `FAILED` 并写可诊断错误，**不自动重试**；
- 真实 MySQL 8.0.37 容器 + 独立库 + 假 Agent HTTP，15/15；回收 6 条覆盖
  正常、超时、重启入口、幂等、边界、晚到完成。

**残余风险**：没有用真实 15 分钟挂钟等待调度器；多实例并发只靠条件更新
语义和重复扫描测试，没有启动两个进程；没有验证主从延迟、锁等待、极端慢任务；
不是生产环境证据。

验证中还发现并修复了一个**测试辅助方法**的时区缺陷（MySQL `NOW()` 与 JVM
时区不一致），业务代码与预期未改。

证据：[phase4.1-index-recovery-results.md](phase4.1-index-recovery-results.md)。

## P2

### R-RAG-04 已缓解：Prompt 拆句 + 诊断字段 + 真实样本

- Prompt 明确要求不同来源/版本/时间的事实拆成独立句子，分别标注；
- `Citation` 暴露 `documentId`；报告分开 `malformed` / `invalid` /
  `unsupported` / `numericConflict`；
- 8 条确定性来源归属回归通过；
- 修复后真实模型 2 个问题 × 3 次：6/6 拆句并分别引用，`numericConflict=0`；
- 但早期探测运行仍出现 1 条合并句（12/17 同句、只标 current），说明
  Prompt 只能降低概率，不能保证；
- `numericConflict` 与 `unsupported` 都是启发式，不是语义判定。

**残余风险**：没有人工评分；6 次样本不构成稳定行为证明；表格、多跳、
单位换算等未覆盖；不能声称「引用语义正确」。

证据：[phase4.1-source-attribution-results.md](phase4.1-source-attribution-results.md)。

### R-EVAL-10 仍未解决：人工语义质量 0/17

- Phase 4.1 新增 8 条盲评（6 条 Phase 4 代表回答 + 2 条修复后样本），
  全部 `PENDING`，完成 **0/8**；
- Phase 3.2/3.3 的 9 条仍为 **0/9**；
- 两批合计 **0/17**；
- 没有计算 Judge–human 一致率、Kappa 或「Judge 已校准」；
- 盲评表不展示自动 Judge 分、`case_status`、`numericConflict`。

### R-RAG-05 未改变：小样本与阈值

Recall@5=1.0、MRR=0.9 仍是 N=5、每条 1 个相关 chunk、无负样本池、
无 reranker；`min_score=0.35` 未校准。不能外推为生产指标。

### 安全与部署边界没有因 Phase 4.1 改变

- 后端监听地址、外部网络隔离、生产部署结论不变；
- 没有调整身份系统、MQ 契约或密钥；
- 没有新增生产性能数据或吞吐结论。

## 进入下一阶段前的建议

1. 如果想谈「语义质量」，先完成 17 条人工盲评，再单独报告样本量与一致性；
2. 扩 RAG 评测到多 chunk、负样本、不同文档类型，再谈阈值；
3. 若要上线索引回收，补齐真实调度等待、多实例并发与运维告警验证；
4. F3 需要更多模型/更多问题样本，不能凭 6 次调用宣称问题根除。
