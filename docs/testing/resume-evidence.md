# 简历证据清单（Phase 2.1 后）

- 日期：2026-10-27
- 用途：把可验证的工作与「不能声称」的部分分开
- 原则：只写有报告、测试或真实查询支撑的内容；不把 Mock、BLOCKED、
  NOT_IMPLEMENTED、NOT_COVERED 写成已交付成果

## 1. 可以写进 Agent / LLM 应用简历

### 1.1 评测体系

- 为有状态学习 Agent 设计并落地 **40 条固定评测用例**，覆盖跨会话状态演化、
  证据约束、Memory、RAG 知识边界、教学对话五类；
- 用例是机器可读 JSONL，包含前置条件、执行模式、硬断言、禁止行为、
  0/1/2 语义 rubric、依赖与清理；
- 实现确定性 runner，支持按用例/类别/执行模式筛选，硬断言逐条执行、
  不短路，输出机器可读报告与逐用例原始证据；
- 修复统计口径：区分 `executed = PASS + FAIL + BLOCKED` 与
  `completed = PASS + FAIL`，解决「实际执行 37 但 30+7+1=38」的矛盾；
- 最终结果：**35 PASS / 0 FAIL / 3 BLOCKED / 2 SKIPPED**；
  不把 BLOCKED/SKIPPED 计为通过。

证据：[phase1-results.md](phase1-results.md)、
[agent-evaluation-cases.jsonl](agent-evaluation-cases.jsonl)、
`evaluation/phase1/`。

### 1.2 缺陷发现与修复

发现并修复 7 个 Agent 缺陷，其中 2 个 P0：

- Agent HTTP 端点无鉴权，任意 `userId` 可读他人学习状态；
- Analyze 链路允许 Fixture 检索源，样例概念可能写入真实用户长期认知；
- 预算裁剪后谎称「没有历史」；
- Token 预算口径不一致（动态上下文 vs 完整 prompt）；
- History Guard 忽略按需 Evidence；
- `auto` 模式静默回退 Fixture；
- `recent_history_limit=0` 被解释成全部历史。

修复后均有回归用例；安全修复保留真实 HTTP 修复前/后证据。

证据：[phase1.1-security-results.md](phase1.1-security-results.md)、
[failure-analysis.md](failure-analysis.md)。

### 1.3 内部服务鉴权与用户身份绑定

- 在共享 `X-Internal-Token` 之外，实现 **短期 HMAC 签名身份**
  `X-Internal-Identity: v1.<payload>.<sig>`，绑定
  `sub / aud / iat / exp / jti`；
- Spring Boot → Agent（`aud=learning-agent`）与 Worker → Spring
  （`aud=learning-backend`）分别签发、分别校验；
- 用户身份只来自签名声明；明文 `X-Internal-User-Id` 仅交叉核对，
  单独存在时不再建立身份；
- 请求体伪造 `userId` 返回 403，且到不了检索层；
- 真实 HTTP 20/20：无凭据、错 token、缺签名、篡改、过期、错 audience、
  错密钥、Header/body 不一致全部被拒绝；跨用户空间/理解读取 404；
- 52 条 Agent 安全测试（含 14 条签名单元测试）、15 条 Spring Security
  集成测试、4 条签名器测试。

证据：[phase2.1-security-results.md](phase2.1-security-results.md)、
`agent/app/internal_identity.py`、`agent/app/internal_auth.py`、
`InternalIdentitySigner.java`、`InternalServiceAuthenticationFilter.java`。

### 1.4 真实模型空流诊断

- 用 `httpx.MockTransport` 写 24 条确定性解析测试，覆盖正常 delta、
  多 delta、finish、usage、空 choices、reasoning-only、格式不兼容、
  流内 error、HTTP 错误、超时、非法 JSON、有界重试；
- 为非流式空内容加入可识别失败，不再静默返回 `""`；
- 真实 API 对照确认：`deepseek-flash` 的 `reasoning_content` 在小
  `max_tokens` 下耗尽预算，`finish_reason=length`，content 为空；
  `max_tokens=1024` 可正常返回；
- 分类为 `empty_stream` / `reasoning_exhausted_budget` / `length_limited` /
  `finished_without_content`，带 request id、HTTP 状态、延迟、event 计数、
  finish reason、usage 等脱敏诊断；
- 有界重试（最多 2 次）后仍无内容返回 502，不把 HTTP 200 当成功；
- 真实模型**语义质量仍 BLOCKED**，没有质量评分或成功率结论。

证据：[phase2.1-llm-stream-diagnostics.md](phase2.1-llm-stream-diagnostics.md)、
`agent/app/llm/openai_compatible.py`、`agent/tests/test_llm_openai_compatible.py`。

### 1.5 上下文预算与证据约束

- 明确 `max_context_tokens` 是动态上下文预算，固定教学契约单独计入完整
  prompt 估算；新增 `budget_tokens` / `context_tokens` /
  `budget_exceeded` / `budget_overrun_reason`；
- 区分「检索为空」与「检索到但被预算丢弃」，避免错误否认历史；
- History Guard 合并轨迹与按需 Evidence，记录计数只由 `TEST_RESULT` 支撑；
- 对有状态 Agent 的「过去误区 vs 当前状态」「旧 Memory vs 新 Understanding」
  做硬断言，而不是只看回答是否流畅。

### 1.6 真实依赖 E2E 与 SSE 契约

- 用真实 Tomcat + 真实 MySQL + 真实 Security 写 7 条 SSE 集成测试：
  正常 `start→delta→end`、end 前已落库、落库后客户端断开仍继续分析、
  持久化失败禁止分析、空流/模型错误终态、跨用户预流 404；
- 用真实 RabbitMQ + 真实 MySQL + 真实 Worker 跑 46 项 MQ 检查：
  PENDING 窗口、消费写入、重复投递不重复写、401 重试、DLQ、FAILED 终态、
  rules-v1 兜底、清理残留；
- 最终状态用有界轮询，不假设固定 sleep；
- 发现并修复 SSE 预流 404 被内容协商改写成 401 的错误表示缺陷。

证据：[phase2.1-integration-results.md](phase2.1-integration-results.md)、
`ChatSseIntegrationTest`、`scripts/phase51-mq-e2e.sh`。

## 2. 可以写进 Java / Spring Boot 后端简历

### 2.1 真实依赖集成测试

- 使用 **真实 MySQL 8.0.37** 验证 Evidence 去重、Understanding 幂等、
  Revision 快照一致性、事务回滚、Memory 生命周期、新会话读取；
- 使用 **真实 RabbitMQ** 验证发布确认、重复投递、未 ACK 重投、TTL 重试、
  死信队列；
- 使用 **真实 Qdrant** 验证文档索引一致性、幂等与版本递增；
- 用 **真实 JWT + Spring Security + MySQL** 验证跨用户、跨空间隔离与
  请求体伪造 `userId`；
- 用 **真实 HTTP + 真实 Tomcat + 真实 MySQL** 验证 SSE 顺序、断开、
  持久化失败、空流/模型错误终态；
- 当前完整后端套件 **226 个测试，0 failures / 0 errors，56 skipped**
  （默认门控关闭时；170 个实际执行）。开启门控的专项结果：
  `SecurityContextIntegrationTest` 15/15、`ChatSseIntegrationTest` 7/7、
  `GlobalExceptionHandlerTest` 2/2、`InternalIdentitySignerTest` 4/4；
- Phase 2 目标集仍为 **49/49 PASS**；Phase 2.1 新增测试单独统计。

证据：[phase2-results.md](phase2-results.md)、
[phase2.1-integration-results.md](phase2.1-integration-results.md)、
[phase2.1-security-results.md](phase2.1-security-results.md)、
`Phase2RealStateIntegrationTest`、`Phase2MqProcessorIntegrationTest`、
`Phase2RabbitMqBrokerTest`、`SecurityContextIntegrationTest`、
`DocumentIndexE2EIntegrationTest`。

### 2.2 幂等、事务与最终状态

- 同一观察重复分析只留一条 Evidence，推进 `last_seen_run_id`，保留来源；
- 相同内容重复 apply 不新增 Revision，不推进 `updated_at`；
- 内容变化时当前快照与 Revision 的字段、时间一致；
- 事务回滚后没有半完成行；
- 重复/并发投递只产生一条业务记录，一次 APPLIED；
- 写入失败整笔回滚，run 保持 PENDING；
- traceId 不一致拒绝处理，run FAILED，无写入；
- 断言最终数据库状态，而不是只看 HTTP 200 或消息已发送；
- MQ 重试、死信、FAILED 终态、rules-v1 兜底均有真实库断言。

### 2.3 SSE 与持久化

- 真实 HTTP 覆盖：正常流只落库一次、`end` 前已落库、客户端在落库后断开
  仍继续分析、`complete()` 后不重复保存、落库失败不启动分析；
- 轮询读取 MySQL 断言 `message` / `agent_run` / `evidence` /
  `understanding_revision` 的最终状态；
- 明确区分已覆盖与未覆盖：第一个 delta 后、落库前断开仍为
  `NOT_COVERED`；SSE POST 幂等仍为 `NOT_IMPLEMENTED`。

### 2.4 文档生命周期与 RAG 权限

- 上传 → UPLOADING → READY/FAILED，解析/分块/embedding/Qdrant 任一步失败
  都不进入 READY；
- 只有 READY、chunk 全部写入、模型匹配、归属正确的文档进入检索候选；
- 跨用户、跨空间、软删文档被过滤；
- 重复索引幂等，版本只在成功时递增。

### 2.5 工程规范

- 测试数据使用合成账号与专用库，不使用真实用户数据；
- 测试代码不写默认数据库密码，凭证从环境变量读取；
- 未自动提交 Git，未改写现有业务逻辑来适配测试；
- 未执行的性能测试不生成任何数字；
- 发现测试脚本回显 JWT 后改为只输出“已取得 token”，并脱敏历史 artifact。

## 3. 不能写进简历（没有证据）

| 声称 | 为什么不能写 |
| --- | --- |
| 「真实 LLM 回答质量通过 / 提升 X%」 | 只验证了可识别输出；语义质量 BLOCKED |
| 「40 条评测全部通过」 | 35 PASS，3 BLOCKED，2 SKIPPED |
| 「226 个测试全部通过」 | 226 个中 56 个因环境门控 skipped；应说 170 执行、0 失败 |
| 「SSE 断连已完全处理」 | 只覆盖落库并 end 后断开；落库前断开未覆盖 |
| 「SSE 重复请求已幂等」 | 无幂等键，重复 POST 会重复落消息与 CHAT run |
| 「性能优化 X%」 | 没有性能基线，未执行 |
| 「高并发验证」 | 只有重复投递/2 线程测试，不构成压测 |
| 「Testcontainers MQ 集成通过」 | `AnalysisMqIntegrationTest` 0 tests，BLOCKED |
| 「生产级鉴权已完成」 | 共享 token 仍是服务级 bearer；后端默认监听 0.0.0.0 |
| 「LLM Judge 已接入」 | 未接入，仍用固定 rubric |
| 「B04 文案已定稿」 | 待产品决策 |
| 「非法状态迁移已被拒绝」 | 没有真实库反向迁移测试 |
| 「数据库 Schema 已优化」 | 本阶段没有改 Schema |
| 「RAG 知识链路本轮通过」 | MQ/SSE 场景知识文档数为 0，知识检索未验证 |

## 4. 推荐在简历中使用的表述

### Agent 方向

> 为有状态学习 Agent 构建 40 条机器可读评测集与确定性 runner，覆盖跨会话
> 状态、证据约束、Memory、RAG 边界与教学对话；通过硬断言发现并修复 7 个缺陷
> （含 2 个 P0：内部接口越权、Fixture 污染真实认知），最终 35 PASS /
> 0 FAIL / 3 BLOCKED。为 Agent 内部接口实现绑定用户、接收方、有效期与
> jti 的 HMAC 短期身份，并用真实 HTTP 验证 20 项鉴权/越权边界；定位真实模型
> 空流为 reasoning 预算耗尽，而不是把 HTTP 200 当成质量通过。

### 后端方向

> 基于 Spring Boot 3 + MySQL 8 + RabbitMQ + Qdrant 编写真实依赖集成测试，
> 验证 JWT 与内部签名身份的跨用户隔离、Evidence 去重、Revision 幂等、
> 事务回滚、MQ 重复投递/重试/死信、文档索引与检索权限；用真实 HTTP SSE
> 覆盖落库顺序、断开后分析、持久化失败禁止分析、空流/模型错误终态；
> 当前套件 226 个测试（170 执行、0 失败、56 环境门控跳过），专项安全
> 15/15、SSE 7/7、MQ 46/46；对未覆盖的落库前断开、SSE 幂等、性能与
> 真实模型质量明确标注边界。

## 5. 面试时可展开的细节

1. 为什么 Fixture 不能用于 analyze：样例概念会进入长期认知，必须是硬拦截，
   不是 warning；
2. 为什么共享 token 不等于用户身份：token 证明“内部服务”，签名声明才绑定
   “代表哪个用户”，两者分离降低 token 泄露的后果；
3. 为什么签名要绑定 audience：同一密钥为不同接收方签发，防止 Agent 的身份
   声明被拿去调用后端；
4. 为什么错误响应要强制 JSON：SSE 客户端只接受 `text/event-stream` 时，
   内容协商失败会把 404 改写成 401；
5. Evidence 去重键如何设计，`last_seen_run_id` 与来源表如何配合；
6. 重复 MQ 投递如何在数据库层收敛，为什么不能只靠内存去重；
7. 为什么 `Tests run: 0` 不能算通过；
8. 为什么真实模型 HTTP 200 但 0 输出要记 BLOCKED，而不是 FAIL 或 PASS；
9. 为什么最终状态要用有界轮询而不是固定 sleep；
10. 哪些结论必须由 DB 行或真实 HTTP 响应支撑，语义评分不能替代。

---

# Phase 3.1 简历证据更新（2026-10-28）

本节新增可验证内容，并更正前文 1.3 中已过时的表述。前文保留为 Phase 2.1 历史记录。

## 更正

前文 1.3 写“Spring Boot → Agent（`aud=learning-agent`）与 Worker → Spring
（`aud=learning-backend`）分别签发、分别校验”。Phase 3.1 起，
**Worker 不再签发后端声明**：`delegation-secret` 只存在于 Spring，Spring 在 MQ
任务中下发 `backendIdentity`，Worker 只转发。以本节为准。

## 1. 可以写进简历（有真实执行/测试支撑）

### 1.1 内部身份签发边界

- 发现并修复“Agent 持有共享 HMAC 密钥即可签发后端接受的任意用户身份”的越权边界；
- 把密钥拆成 `identity-secret`（Spring ↔ Agent，`aud=learning-agent`）与
  `delegation-secret`（仅 Spring，`aud=learning-backend`、`scope=analysis:read`）；
- Spring 独占委托签发权；Agent/Worker 只原样转发 MQ 任务中的声明，不能改 `sub`
  或重新签名；
- 后端只对 `GET /api/**` 用内部凭据建立用户上下文；写方法不建立内部身份；
- 启动期 fail closed：密钥缺失、两把密钥相同、密钥与 token 相同、audience 相同
  时拒绝启动。

**证据：** `agent/app/retriever/routing.py`、`InternalIdentitySigner.java`、
`InternalSecurityConfigValidator.java`、[phase3.1-security-results.md](phase3.1-security-results.md)。

### 1.2 真实 HTTP 安全回归

- **24/24** 真实 HTTP + 真实 MySQL 测试通过：
  - 无凭据 401；
  - 合法委托 200；
  - Agent 密钥伪造后端身份 401；
  - 篡改签名、过期、错 audience、错 issuer、错 scope 401；
  - 声明 `sub` 与 `X-Internal-User-Id` 不一致 401；
  - body `userId` 不能覆盖 JWT 身份；
  - 内部凭据 POST 写操作 401，数据库无新增行。

**证据：** `InternalIdentityHttpIntegrationTest`（5）、
`SecurityContextIntegrationTest`（19）。

### 1.3 启动配置校验

- **5/5** 单元测试：未启用内部入口不校验；启用但缺密钥、密钥相同、token 相同、
  audience 相同均拒绝启动。

**证据：** `InternalSecurityConfigValidatorTest`。

### 1.4 输出完整性独立状态

- 把 `completion_status`（COMPLETE/TRUNCATED/EMPTY/ERROR/UNKNOWN）从
  `case_status` 中拆出；
- `finish_reason=length` 直接判 FAIL，即使已输出内容看起来正确；
- 语义 `task_completion` 从 2 降到 1，并标记 `completionAdjusted` 与
  `requiresHumanReview`；
- 用历史 artifact 复现 B05-r1、E01-r2 截断；E01-r2 从旧 PASS 改为 FAIL。

**证据：** `evaluation/phase1/completion.py`、
`evaluation/phase3/runner.py`、
[phase3.1-evaluator-calibration.md](phase3.1-evaluator-calibration.md)。

### 1.5 评测器误判修复

- B05：不再要求 Evidence Guard 必须追加；检查最终回答是否披露、是否重复；
- E01：从句方向判断肯定/否定/历史/问句，消除关键词误判；
- D01：新增 `malformedCount`/`syntaxValid`，语法合法与事实支持分开；
- A01：合规免责句不再触发 History Guard 回退，同时保留对编造历史的拒绝。

**证据：** `agent/tests/test_phase31_evaluator_regressions.py`（19）、
`agent/tests/test_history_guard.py`（24）。

### 1.6 测试规模

- Agent 确定性测试：**328 passed / 5 skipped / 0 failed**；
- 后端全量：**243 tests / 0 failures / 0 errors / 41 skipped**；
- 真实模型校准运行：**10 次，10 个有效输出，0 BLOCKED，9 PASS / 1 FAIL**。

## 2. 不能写进简历（仍需验证）

- “Agent 语义质量已通过”或“教学质量已证明”：只有自动 Judge 分，人工评分 0/9；
- “Judge 已校准/有区分度”：分布几乎全是 2 分，且与 Agent 同 provider；
- “完整 Spring → Agent → 后端 MQ E2E 已通过”：脚本已更新为 schema v2，但本阶段未运行；
- “部署网络已隔离”：后端未显式设置 `server.address`，实际映射未取证；
- “已阻止重放”：`jti` 只校验非空，没有 replay cache；
- “密钥可轮换/撤销”：未实现；
- 任何真实用户越权测试：未执行且禁止。

## 3. 一句话版本

> 修正了内部身份签发边界：Agent 不再能自签后端接受的用户身份，委托声明由
> Spring 独占签发并经真实 HTTP 24/24 安全回归验证；同时把输出完整性从评测
> 通过状态中拆出，修复 B05/E01/D01/A01 的确定性误判。语义质量与 Judge 校准
> 仍待人工评分，不作结论。

---

# Phase 3.2 简历证据更新（2026-10-28）

本节补充完整 MQ 契约 E2E 与跳过测试审计。前文 Phase 3.1 的“完整三进程 MQ E2E
未执行”已被本轮实测取代；语义质量与部署安全结论不变。

## 1. 可以写进简历（有真实执行/测试支撑）

### 1.1 完整 MQ 契约 E2E（真实依赖）

- 用 **真实 Spring 生产者 → 真实 RabbitMQ → 真实 Python Worker → 真实后端 HTTP
  → 隔离 MySQL** 跑通 schema v2 分析链路，**109 项检查 / 0 失败**；
- 合法 v2：ANALYSIS `SUCCEEDED`，Evidence 1 条（`USER_EXPLANATION/PARTIAL/agent-v1`）、
  Understanding `EXPOSED`、Revision 1 条、Memory 1 条（`CANDIDATE/GLOBAL`，绑定
  消息来源），最终状态全部用 SQL 断言；
- 非法 v2：`backendIdentity` 缺失/空串/纯空白 → `MESSAGE_INVALID`；
  `schemaVersion=1/3` → `TASK_SCHEMA_UNSUPPORTED`；5 条全部 `FAILED`、死信 +5、
  retry 队列 +0、Runtime 调用 0、无 Agent 学情写入；
- 重复投递：同一 `taskId` 消费 2 次，Evidence/Revision/Memory 各保持 1 份，
  Result Consumer 识别 1 次 `DUPLICATE`；
- 真实 401 重试：retry 副本被新 Worker 消费后 `SUCCEEDED`，`retries=1`；
- 清理：队列与用户级联数据残留 0，任务 JSON（含短期委托声明）删除；
- 日志凭证扫描：三个测试密钥与 `v1.` 身份串在 artifact/日志中 0 次出现。

**证据：** [phase3.2-mq-e2e-results.md](phase3.2-mq-e2e-results.md)、
`backend/backend/target/phase51-mq-e2e/20261028-004526/`、
`scripts/phase51-mq-e2e.sh`。

### 1.2 跳过测试审计

- 逐条审计后端 41 条 skipped 与 Agent 5 条 skipped，标明门控变量、真实依赖、
  覆盖风险与是否有等价真实证据；
- 明确区分“已有等价覆盖”（如 `contextLoads`）与“关键未覆盖”（SSE、RAG/Qdrant、
  索引状态机、MQ 回滚/trace、Revision 幂等/跨会话）；
- 没有把 41 条统称为“不重要的外部依赖测试”，也没有把 skipped 计入 PASS。

**证据：** [phase3.2-skipped-audit.md](phase3.2-skipped-audit.md)。

### 1.3 人工评审可复现准备

- 9 条待复核记录均含回答全文、上下文事实摘要、完成状态/`finish_reason`、
  6 维 0/1/2 锚点、理由与备注栏；
- 发现原评分表泄露自动 Judge 分数会造成锚定，生成独立盲评版，移除自动分、
  自动 `case_status`、判读理由与复核原因，原表保留供事后对照；
- 人工分仍为 **0/9**，没有预填、没有把自动分改名成人工分、没有计算一致性。

**证据：** [phase3.2-agent-review-readiness.md](phase3.2-agent-review-readiness.md)、
[phase3.2-human-review-blind.md](phase3.2-human-review-blind.md)、
`evaluation/phase3/build_blind_review.py`。

### 1.4 更新后的测试规模

- Agent 确定性测试：**328 passed / 5 skipped / 0 failed**；
- 后端全量：**243 tests / 0 failures / 0 errors / 41 skipped**；
- 完整 MQ E2E：**109 PASS / 0 FAIL**（Mock LLM、hash embedding、隔离 MySQL）；
- 真实 HTTP 安全：**24/24**；启动配置校验 **5/5**；
- 真实模型校准：**10 次，9 PASS / 1 FAIL**（语义质量仍未人工确认）。

## 2. 不能写进简历（仍需验证）

| 声称 | 为什么不能写 |
| --- | --- |
| “Agent 语义质量已通过 / 教学质量已证明” | 人工评分 0/9；自动 Judge 分布几乎全是 2 分 |
| “Judge 已校准 / 有区分度” | 没有人工分；Judge 与 Agent 同 provider；缺 0 分样本 |
| “完整 MQ E2E 证明了真实模型链路” | E2E 使用 Mock LLM 与 hash embedding，只证明契约与落库 |
| “RAG 知识链路已通过” | 本轮知识检索 `NO_MATCH`；真实 Qdrant/索引 E2E 仍跳过 |
| “SSE 断连/顺序已完全验证” | `ChatSseIntegrationTest` 7 条仍跳过，无真实 HTTP 证据 |
| “部署网络已隔离” | 后端未显式设置 `server.address`；E2E 全程 loopback |
| “已阻止委托声明重放” | `jti` 只校验非空，无 replay cache |
| “密钥可轮换/撤销” | 未实现 |
| “旧版本消息可兼容” | v1/v3 明确拒绝，无转换分支；必须先升消费者 |
| “并发/吞吐/高可用已验证” | 单 Worker、prefetch=1、单节点 broker |

## 3. 更新后的一句话版本

> 用真实 Spring、RabbitMQ、Python Worker 与 MySQL 跑通 schema v2 分析链路
> （109 项检查 / 0 失败），验证合法消息落库、非法/旧版本消息入口拒绝与死信、
> 重复投递幂等、401 重试恢复和清理；逐条审计 46 条跳过测试，区分已有等价覆盖
> 与 SSE、RAG/Qdrant、事务回滚等关键未覆盖项；为人评审准备无自动分锚定的盲评表，
> 同时明确语义质量与部署安全仍未验证。

---

# Phase 3.3 简历证据更新（2026-10-28）

本节补充消息升级评审、过期委托声明与部署面实测。前文结论仍然成立；新增证据
只覆盖本轮实际执行的范围。

## 1. 可以写进简历（有真实执行/测试支撑）

### 1.1 版本升级契约评审

- 对照当前工作树与 `git HEAD`，确认 producer/consumer 都是严格版本匹配：
  v1 任务被新 Worker 拒绝，v2 任务被旧 Worker 拒绝，不存在安全的混合版本窗口；
- 明确四种组合与队列 durable/TTL/DLX 行为，给出“停生产者 → 排空 request/retry
  → 升消费者 → 一次性升生产者 → 恢复流量”的协调切换 runbook 与回滚顺序；
- 识别出当前仓库没有部署流水线、蓝绿/金丝雀或队列深度门禁，因此把该 runbook
  标记为“已评审的部署要求”，没有把它写成已验证的生产行为。

**证据：** [phase3.3-schema-rollout-review.md](phase3.3-schema-rollout-review.md)。

### 1.2 过期委托声明的有界失败

- 用 3 个确定性回归测试覆盖：过期 `aud=learning-backend` 声明被拒、后端 401
  映射为 `RetrievalError`、Worker 在 3 次执行/2 次 retry 等待后 `FAILED` + 死信；
- 明确 `analysis_max_retries=3` 的实际语义是“最多 3 次执行”，约 30s，不是
  无限重试或 3 次等待；
- 断言失败任务不写 Evidence/Revision/Memory，`retryable=False`，不会产生
  Agent 成功状态；死信保留原始 body/过期声明，说明 DLQ 的数据保留风险。

**证据：** [phase3.3-security-boundary.md](phase3.3-security-boundary.md)、
`agent/tests/test_worker_reliability.py`、
`agent/tests/test_internal_identity.py`、
`agent/tests/test_knowledge_retrieval.py`。

### 1.3 网络与密钥边界的实测

- 后端实测监听 `0.0.0.0:8081` / `[::]:8081`；宿主访问 LAN IP 返回 401；
- 从独立 `ubuntu:24.04` 容器经宿主 LAN IP 建立 TCP 成功，容器自身 loopback
  8081 为 CLOSED（对照），证明不是只监听 loopback；
- Docker 本地 MySQL/RabbitMQ/Qdrant 均只绑定 `127.0.0.1`；
- 正常启动的 Agent 进程没有 `AILS_INTERNAL_DELEGATION_SECRET`；
- 但没有把“容器可达”扩大成“外部主机可达/生产已隔离”；外部边界仍未验证。

**证据：** [phase3.3-security-boundary.md](phase3.3-security-boundary.md)。

### 1.4 人工评审状态复核

- 盲评表 9 条记录结构完整：9 个上下文摘要、9 个回答块、9 个评分表，无空回答；
- 6 条显式标注“Evidence：0 条”，3 条有 Evidence；
- 队列 `humanScores`/`humanNotes`/`humanDecision` 全为 null，状态全 `PENDING`；
- 人工评分仍为 **0/9**，没有预填、没有计算一致性。

**证据：** [phase3.3-agent-review-status.md](phase3.3-agent-review-status.md)。

### 1.5 更新后的测试规模

- Agent 全量：**331 passed / 5 skipped / 1 warning**（本轮新增 3 个回归测试）；
- 本轮新增测试的定向执行：**55 passed**；
- 后端本轮未重跑全量；Phase 3.2 的 243 tests / 41 skipped 仍作为历史基线，
  不作为 Phase 3.3 的新执行结果。

## 2. 不能写进简历（仍需验证）

| 声称 | 为什么不能写 |
| --- | --- |
| “已完成安全的滚动升级” | 没有混合版本兼容窗口；协调切换 runbook 未在生产执行 |
| “部署流程已防止旧/新版本混跑” | 仓库没有部署流水线、队列深度门禁或版本握手 |
| “生产网络已隔离 / 后端只在内网” | 只证明同宿主容器经 LAN IP 可达；外部主机、防火墙、安全组未验证 |
| “Agent 配置能阻止 delegation 密钥注入” | 注入该变量后 Agent 仍启动；没有启动门禁 |
| “过期委托声明不会留下凭据” | DLQ 无 TTL，原样保留过期声明 |
| “人工评审已完成 / Judge 已校准” | 0/9；没有人工分 |
| “RAG 知识链路已通过” | 仍是 hash embedding、`NO_MATCH`；真实 Qdrant 用例未跑 |
| “SSE 边界已完全验证” | 7 条 `ChatSseIntegrationTest` 仍跳过 |
| “并发/吞吐/高可用已验证” | 单 Worker、prefetch=1、单节点 broker |
| “已阻止重放” | `jti` 只校验非空，无 replay cache |

## 3. 更新后的一句话版本

> 评审并明确了 schema v1→v2 的升级边界：两个方向的混合版本都会被 Worker
> 拒绝，因此只能协调切换，不能滚动升级；用确定性测试证明过期委托声明会在
> 3 次执行/2 次等待后 `FAILED` + 死信、不会无限重试；实测后端监听所有网卡且
> 同宿主容器可达，但外部网络隔离、Agent 启动门禁和人工评分（0/9）仍未完成。

---

# Phase 4 简历证据更新（2026-10-28）

本节补充真实 Qdrant + 真实 Embedding 的 RAG 端到端与来源追溯。前文“RAG 仍是
hash embedding”的说法已被本节的真实链路证据取代，但仅限隔离合成数据与
小样本；语义质量仍未人工评审。

## 1. 可以写进简历（有真实执行/测试支撑）

### 1.1 Agent 配置 fail-closed 守卫

- 在 `agent/app/config.py` 增加 `AILS_INTERNAL_DELEGATION_SECRET` 启动校验：
  缺失/空白正常启动，进程环境、`.env` 或 kwargs 中非空则拒绝启动；
- 错误信息不回显密钥值，`describe()` 不暴露该字段；
- 6 个配置单测通过；全量 Agent **337 passed / 5 skipped / 1 warning**；
- 真实启动验证：注入值 exit code 3、密钥值出现 0 次；不注入 `/health` 200，
  停止后端口关闭；
- 明确边界：这不是身份协议，也不防御能改代码/启动参数的人。

**证据：** [phase4-rag-index-results.md](phase4-rag-index-results.md) §0、
`agent/tests/test_config_security.py`、`agent/app/config.py`。

### 1.2 真实 RAG 索引/检索链路（隔离合成数据）

- 用隔离 MySQL DB、隔离 Qdrant prefix、真实 `text-embedding-v4`（1024 维）
  跑通 Spring 上传 → 异步索引 → FastAPI 解析/切块 → Embedding → MySQL chunk
  + Qdrant point → READY → ready-ids → 真实查询向量 → Context Builder；
- 两个用户、三个空间、4 个 READY 文档，另加 FAILED 与中断 PROCESSING 各 1；
- 7/7 索引/生命周期检查通过：DB/Qdrant 的 document ID、chunk ID、正文、
  用户/空间、文件名一致；collection 维度 1024；FAILED/PROCESSING 无
  chunk/point、不进 ready-ids；
- 5 条标注查询（N=5，K=5）：Recall@5 **1.0**、MRR **0.9**；
- 跨用户/跨空间 Qdrant 直查 3 个应零命中组合全部 0 命中，同空间对照 1 命中；
- 最终 Context 注入范围：A1 只 18/19，A2 只 20，B1 只 21；每个注入 chunk
  都能回到真实 document/chunk/文件名。

**证据：** [phase4-rag-index-results.md](phase4-rag-index-results.md)、
[phase4-rag-retrieval-results.md](phase4-rag-retrieval-results.md)、
`evaluation/phase4/reports/20261027_172403/`。

### 1.3 真实模型引用合法性

- 6 个真实模型用例（支持、冲突版本、信息不足、旧版、无匹配、错误前提补充）
  走 Spring SSE 与真实 `deepseek-flash`；
- 6/6 硬断言通过：SSE 完整、非空回答、引用语法/合法性、引用 chunk 在最终
  Context、授权范围、无匹配不引用；
- 16 条合法引用，0 非法、0 malformed、0 非注入；
- 错误前提用例模型明确纠正“当前不是 12 秒，是 17 秒”。

**证据：** [phase4-rag-agent-results.md](phase4-rag-agent-results.md)、
`agent_results.json`、`supplemental_agent.json`。

### 1.4 发现并定位的真实缺陷

- **`CONTEXT_BUDGET`**：长历史下 SUCCESS 检索的两个知识 chunk 全被预算丢弃，
  但 `knowledge_status` 不渲染 SUCCESS，Guard 也不处理 SUCCESS，模型无法区分
  “没检索到”和“检索到但没放下”；
- **`INDEX_FAILURE`（中断无回收）**：后端在索引中被杀后，文档永久停在
  PROCESSING、0 chunk/point、无错误信息；
- **`SOURCE_MAPPING`**：2 条合法引用出现 `numericConflict`，一句话合并了
  两个来源的数字但只标了一个来源。

这些都有 preview/JSON 证据，并已写入
[phase4-failure-analysis.md](phase4-failure-analysis.md)。

### 1.5 隔离与清理

- 只使用本轮创建的 DB `ails_phase4_e2e`、Qdrant collection
  `ails_phase4_e2e_text-embedding-v4_1024`、存储目录与临时合成文件；
- 清理后原有三个 Qdrant collection 保留，8000/8081 无监听；没有删除用户
  原有 collection，没有提交 Git。

## 2. 不能写进简历（仍需验证）

| 声称 | 为什么不能写 |
| --- | --- |
| “生产 RAG 已通过 / 可直接上线” | 只验证隔离合成数据、单 chunk 文档、小样本；无并发/吞吐/真实用户数据 |
| “Recall@5=1.0 / MRR=0.9 代表生产效果” | N=5、每条 1 个相关 chunk、无负样本池、无 reranker；阈值未校准 |
| “语义质量已验证 / 正确率 100%” | 6 例只有硬断言与人工快速阅读；无人工评分、无校准 Judge，状态 `UNREVIEWED` |
| “引用合法就说明事实正确” | 2 条 `numericConflict` 已证明合法标签仍可能多来源单引用 |
| “预算裁剪不会误导回答” | SUCCESS + 知识全丢时缺少状态说明，`CONTEXT_BUDGET` 未修复 |
| “索引中断会自动恢复” | PROCESSING 孤儿无租约/超时/启动回收 |
| “配置守卫能防恶意注入” | 只覆盖运行配置误注入；不防御改代码/启动参数的人 |
| “批次/多 chunk/大规模已验证” | 每文档 1 chunk，`batch_size=10` 未触发；无大规模样本 |
| “人工评审已完成” | 原 9 条盲评仍 0/9；本轮 6 例也未评分 |

## 3. 更新后的一句话版本

> 在隔离合成数据上用真实 `text-embedding-v4` 与真实 Qdrant 跑通文档索引、
> READY 白名单、权限过滤、向量检索、Context Builder 和真实 LLM 引用校验：
> 7/7 索引检查、Recall@5 1.0 / MRR 0.9（N=5，不可外推）、6/6 真实模型硬断言、
> 16 条引用 0 非法；同时定位了 SUCCESS 预算丢弃无说明、PROCESSING 孤儿和
> 多来源单引用三个缺口。语义质量仍无人工证据，生产规模与网络边界未验证。

---

# Phase 4.1 简历证据更新（2026-10-28）

本节补充 Phase 4 三个缺陷的修复与验证。前文 Phase 4 的“未修复”表述已被
本节取代；只改写有本轮证据支撑的范围。

## 1. 可以写进简历（有真实执行/测试支撑）

### 1.1 检索状态与预算后状态分离

- 把检索层 `knowledge_status` 与预算裁剪后的 `knowledge_state` 分开，
  新增 `SUCCESS_PARTIAL` / `SUCCESS_BUT_DROPPED`；
- 状态说明在预算裁剪后补入，不可再被预算丢弃；Guard 使用最终状态，
  避免「检索到了但没放下」被说成「没有检索到」；
- 确定性测试覆盖完整进入、全丢、部分丢、无匹配，并断言实际渲染 Prompt
  与运行时输出，而不是只断言内部布尔值；
- 受控真实模型样本：3000 token 预算、固定 chunk、12 条合成历史、
  4 条合成记忆，得到 `SUCCESS_BUT_DROPPED`，注入 0、丢弃 18/19，
  回答明确说「检索确实命中了材料，但片段因上下文预算没能进入本轮对话」。

**证据：** [phase4.1-context-budget-results.md](phase4.1-context-budget-results.md)、
`evaluation/phase4_1/budget_pressure_llm.json`。

### 1.2 中断索引的超时回收

- 用现有 `updated_at` 近似索引启动时间，未改表；
- 增加可配置超时（默认 15 分钟）与定时扫描；
- 条件更新 `status='PROCESSING' AND updated_at < cutoff` 保证重复扫描/
  多实例幂等，不覆盖 READY；
- 超时转 `FAILED` 并写可诊断错误，不静默自动重试；
- 真实 MySQL 8.0.37 + 独立库 + 假 Agent HTTP，`DocumentIndexServiceIntegrationTest`
  15/15；回收 6 条覆盖正常、超时、重启入口、幂等、边界、晚到完成；
- 验证中发现并修复测试辅助方法的时区缺陷（MySQL `NOW()` vs JVM），
  业务代码与预期未改。

**证据：** [phase4.1-index-recovery-results.md](phase4.1-index-recovery-results.md)、
`backend/.../DocumentIndexRecoveryService.java`、`DocumentMapper.java`。

### 1.3 事实级来源归属的确定性回归

- Prompt 要求不同来源/版本/时间的事实拆句并分别标注，禁止合并两个来源的
  数字只标一个来源；
- `Citation` 暴露 `documentId`；报告分开 `malformed` / `invalid` /
  `unsupported` / `numericConflict`，明确 `numericConflict` 是诊断信号，
  不是自动判错；
- 8 条确定性回归覆盖单来源、两来源拆句、合并版本、无词面支持、不存在索引、
  事实正确但指向错误来源；
- 修复后真实模型 2 个问题 × 3 次：6/6 拆句并分别引用，`numericConflict=0`；
- 早期探测运行仍出现 1 条合并句，明确记录为随机性反例。

**证据：** [phase4.1-source-attribution-results.md](phase4.1-source-attribution-results.md)、
`agent/tests/test_source_attribution.py`、`evaluation/phase4_1/source_attribution_llm.json`。

### 1.4 更新后的测试规模

- Agent 定向：**71 passed**（来源归属、引用、Context Builder、Guard、
  runtime citations）；
- Agent 全量：**351 passed / 5 skipped / 1 warning**；
- 真实 MySQL 索引集成：**15/15**（回收 6 条），独立库 + 假 Agent；
- 后端全量：**249 tests / 178 executed / 71 skipped / 0 failures /
  0 errors**（默认门控关闭）；
- 真实 LLM：Phase 4.1 新增 F1 1 次 + F3 6 次，另有 1 次早期探测；
- 人工语义评分：新增 8 条，完成 **0/8**；历史 9 条仍 **0/9**；合计 **0/17**。

### 1.5 隔离与清理

- 索引集成只用专用库 `ails_document_index_it`、容器 `ails-mysql-it`
  （`127.0.0.1:3307`）与本地假 Agent；
- 测试结束后清理本轮合成 `user` / `learning_space` / `document` 行，
  验证计数为 0；
- F1/F3 样本只复用 Phase 4 固定 chunk，没有新建 Qdrant collection；
- 没有自动提交 Git，没有删除用户未提交修改。

## 2. 不能写进简历（仍需验证）

| 声称 | 为什么不能写 |
| --- | --- |
| “RAG 语义质量已通过” | 人工评分 0/17；6 次 F3 样本不是质量评分 |
| “引用语义正确率 100%” | 8/8 是确定性结构测试；真实模型仍有合并反例 |
| “预算丢弃已被永久消除” | 受控单模型单样本；无在线 RAG 重跑 |
| “索引恢复已在生产验证” | 真实 MySQL + 假 Agent；无真实 Qdrant、无 15 分钟挂钟、无多进程 |
| “多实例并发回收已压测” | 只有条件更新语义与重复扫描测试 |
| “Recall@5=1.0 / MRR=0.9 是生产指标” | N=5、单 chunk、无负样本；未校准 |
| “F3 已根除” | 早期探测运行仍出现 1 条合并句 |
| “Judge 已校准 / 一致性已计算” | 0/17，无人工分 |

## 3. 更新后的一句话版本

> 修复 RAG 上下文预算、索引孤儿与来源归属三个缺陷：把检索状态与预算后状态
> 分离并补入不可丢弃的状态说明，用受控真实模型样本证明能区分“检索到但没
> 放下”；用 `updated_at` + 可配置超时 + 条件更新回收中断索引，真实 MySQL
> 集成 15/15；用 Prompt 拆句规则与引用诊断字段做 8 条确定性回归，并在 6 次
> 真实模型样本中观察到两来源拆句分别引用。人工语义评分仍为 0/17，生产规模、
> 在线 RAG 与长期调度未验证。
