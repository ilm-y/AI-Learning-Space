# Phase 2 Spring Boot 测试执行结果

- 日期：2026-10-27
- 范围：真实 MySQL、真实 RabbitMQ、真实 Qdrant、JWT/Spring Security、
  SSE 持久化、文档生命周期与权限
- 原则：只统计真实执行；Mock、真实依赖、E2E 分开；BLOCKED / NOT_COVERED
  不计 PASS；未自动提交 Git

> **Phase 2.1 更新（2026-10-27，保留本节为 Phase 2 时点快照）**
>
> - D6“客户端中途断开 SSE”与 D8“断开后分析是否继续”已由
>   `ChatSseIntegrationTest` 7/7 覆盖（落库并收到 `message.end` 后断开）；
>   落库前断开仍未覆盖。
> - `SecurityContextIntegrationTest` 由 12 条扩展到 **15/15**，新增仅共享
>   token 不能冒充、声明与 Header 必须一致、篡改/错 audience 被拒绝。
> - 新增 `GlobalExceptionHandlerTest` 2/2、`InternalIdentitySignerTest` 4/4、
>   `ChatSseIntegrationTest` 7/7。
> - 当前完整后端套件（默认门控）**226 tests / 0 failures / 0 errors /
>   56 skipped / 170 executed**；Phase 2.1 专项与 MQ E2E 见
>   [phase2.1-integration-results.md](phase2.1-integration-results.md) 与
>   [phase2.1-security-results.md](phase2.1-security-results.md)。
> - 下方 208 个测试是 Phase 2 时点的全门控结果，不代表 Phase 2.1 的最终数量。

## 1. 计划与执行总览

### 1.1 场景矩阵

按 A–F 的 45 个场景组统计（一个场景组可能由多个测试方法支撑）：

| 状态 | 数量 | 说明 |
| --- | ---: | --- |
| PASS | 41 | 有可复核断言并实际通过 |
| PARTIAL | 1 | E5 清理只做到「隔离 + 专用库清理」，演示脚本不删历史运行 |
| FAIL | 0 | 本次执行没有失败断言 |
| BLOCKED | 1 | D7 真实模型空流后的后端终态 |
| NOT_COVERED | 2 | D6 SSE 主动断开、D8 断开后分析是否继续 |
| 计划合计 | 45 | 41 + 1 + 1 + 2 |

说明：PARTIAL 不是 PASS；它表示同一场景里一部分要求已证明、另一部分未做。
`A9` 的 Agent 侧证据来自 Python 测试，不是 Spring JUnit，单独标注。

### 1.2 JUnit 实际执行

| 指标 | 数量 |
| --- | ---: |
| 计划执行的 Phase 2 JUnit 测试方法 | 49 |
| 实际执行 | 49 |
| PASS | 49 |
| FAIL | 0 |
| BLOCKED | 0 |
| SKIPPED | 0 |

分层：

| 层级 | 数量 | 测试类 |
| --- | ---: | --- |
| UNIT | 4 | `ChatServiceTest` |
| INTEGRATION | 44 | Security 12、Phase2RealState 6、MqProcessor 5、RabbitMqBroker 5、DocumentService 7、DocumentIndexService 9 |
| END_TO_END | 1 | `DocumentIndexE2EIntegrationTest` |

最终完整后端套件（2026-10-27 21:04–21:05，所有真实依赖门控打开，E2E 使用
隔离 Qdrant prefix）执行 **208 个测试，0 failures / 0 errors，1 skipped**
（`BackendApplicationTests` 被 `@Disabled`）。
`AnalysisMqIntegrationTest` 显示 `Tests run: 0`，是 Testcontainers 环境 abort，
不是 PASS，也不在 208 个已执行测试内。

E2E 隔离过程：

1. 第一次重跑把 `AILS_E2E_QDRANT_COLLECTION` 写成不存在的
   `ails_e2e_phase2_hash-v1_8`，实测 404，结果 208 tests / 1 failure；
2. 改用 Agent 当时实际使用的 `ails_documents_hash-v1_8` 后通过，但该测试的
   `@AfterAll` 会删除传入的 collection，等于删除了共享检索数据；
3. 因此重启测试用 Agent，设置
   `AILS_QDRANT_COLLECTION_PREFIX=ails_e2e_phase2`，用匹配的
   `ails_e2e_phase2_hash-v1_8` 单独重跑 E2E：1/1 PASS，collection 在测试内
   创建并在 `@AfterAll` 删除，不触碰共享 collection；
4. 在该隔离配置下重跑完整套件，得到上述 208 / 0 / 1 skipped。

第一次 404 与「删除共享 collection」都保留在
[failure-analysis.md](failure-analysis.md) I-06 / I-07，不改成 PASS。

Agent 侧同一阶段执行：

- `agent/tests/test_security_phase11.py` + `test_phase11_regressions.py`：
  **31 passed**；
- `HttpPythonAgentGatewayTest` 9、`HttpPythonAgentAnalysisGatewayTest` 9；
- `phase6-demo.sh` 真实 HTTP：**PASS=23 / FAIL=0**。

## 2. 依赖真实性

| 依赖 | 实际状态 | 哪些测试用了它 |
| --- | --- | --- |
| MySQL 8.0.37 | 真实，专用库 | Security、Phase2RealState、MqProcessor、DocumentService、DocumentIndex、E2E |
| RabbitMQ | 真实本地容器 | Phase2RabbitMqBroker；MqProcessor 只测消费/事务，不连 broker |
| Qdrant | 真实 6333 | DocumentIndexE2E |
| Spring Security / JWT | 真实过滤器链 | SecurityContextIntegrationTest |
| 真实 LLM | 请求发出但空流 | Phase 1 的 B08/D08/E07，全部 BLOCKED |
| Agent | 本地 FastAPI，LLM 为 mock，embedding 为 hash | 文档 E2E、HTTP 演示、鉴权测试 |
| Docker / Testcontainers | CLI 可用，Testcontainers 无法连接 | `AnalysisMqIntegrationTest` BLOCKED（0 tests） |

没有使用真实用户数据；测试使用合成用户、合成空间、合成文档。

## 3. A–F 关键结论

### A. 鉴权与权限

- 无 JWT 访问受保护接口返回 401，不再返回业务数据；
- JWT 用户 A 读取用户 B 的空间/文档返回 404，不泄露存在性；
- 跨空间 `ready-ids` 只返回当前空间且状态、索引、模型都合格的文档；
- 请求体伪造 `userId` 不能覆盖 JWT 身份；
- 内部服务 token 能以绑定用户身份通过过滤器，但不绕过空间归属；
- 非法内部 token 被拒绝，过滤器顺序正确。

证据：`SecurityContextIntegrationTest` 12/12；
`DocumentServiceIntegrationTest.listAndDetailRespectOwnership` /
`readyIdsOnlyReturnFullyIndexedReadyDocumentsOfTheSpace`。

### B. 状态与事务

- 同一观察重复写入只留一条 Evidence，`last_seen_run_id` 被推进，来源不丢；
- 内容完全相同的 Understanding 不新增 Revision，`updated_at` 不变；
- 内容变化时，当前快照与 Revision 的 summary / known_parts / misconceptions /
  status / 时间一致；
- 事务回滚后没有半完成的 Evidence / Understanding / Revision 行；
- Memory 用户侧为 ACTIVE、Agent 候选为 CANDIDATE，存在一条来源行；
- 新会话重新通过服务查库，仍能读到之前的 Understanding、Evidence、State。

证据：`Phase2RealStateIntegrationTest` 6/6。

### C. RabbitMQ 与幂等

- 发布确认后消息真实进入请求队列并可被消费；
- 同一任务重复发布会产生两条消息，最终由消费端幂等收敛；
- 未 ACK 消息在连接断开后重投；
- 失败消息进入重试队列，TTL 到期回到请求队列；
- 被拒绝的消息进入死信队列；
- 重复投递被识别为 DUPLICATE，只留一条 Evidence / Revision；
- 并发重复投递只有一次 APPLIED，没有 IGNORED，业务行不重复；
- FAILED 结果将 run 标为 FAILED，不写业务行；
- 写入中途异常触发整笔回滚，run 保持 PENDING，不留下半条 Evidence；
- traceId 不一致时拒绝处理，run FAILED，无写入。

证据：`Phase2MqProcessorIntegrationTest` 5/5、
`Phase2RabbitMqBrokerTest` 5/5。

### D. SSE 与持久化

- 正常流：助手消息只落库一次，分析只触发一次；
- 首个 delta 发送异常：仍落库并触发分析；
- 持久化后 `complete()` 异常：仍分析，且不重复保存；
- 助手消息落库失败：不启动分析；
- 真实 HTTP：`message.start` → delta → `message.end`，无 `message.error`。

未证明：

- 客户端主动断开时服务端是否停止生成、是否仍持久化；
- 断开但消息已落库时，分析是否按契约继续；
- 真实模型错误 / 空流时后端最终事件与数据库状态。

D1–D4 是单元测试（Mock SSE / 持久化），不能替代真实断连测试。
D5 是真实 HTTP，但没有主动断开客户端。

### E. 持久化 E2E

- 首次对话产生 Evidence 并落库；
- 同一用户新会话仍能读取 Understanding / Memory / State；
- Agent 上下文中的区块与数据库只读查询一致；
- Memory 按作用域可读（服务层 + Agent 侧）；
- Phase 2 测试在专用库 `@BeforeEach` 清表，不残留测试数据。

限制：`phase6-demo.sh` 每次新建用户/空间，但不删除历史运行；
数据留在隔离库 `ails_phase2_e2e`，没有污染开发库或真实用户，但
「绝对无残留」不成立。

### F. 文档生命周期与 RAG

- 上传后初始状态为 UPLOADING，索引成功后 READY；
- 解析失败、分块落库失败、embedding/Qdrant 失败都不会进入 READY；
- 只有 READY、已写完全部 chunk、模型匹配、属于当前用户/空间的文档进入候选；
- 跨用户、跨空间、软删文档都被过滤；
- 重复索引幂等，版本只在成功时递增；
- 真实 Agent + 真实 Qdrant 的一致性 E2E 通过。

证据：Document 相关 17 个集成测试 + 1 个 E2E。

## 4. 未解决与风险

### P0

| 项 | 状态 | 说明 |
| --- | --- | --- |
| Agent 内部共享 token | 已修复裸奔，仍有残余风险 | 持有 token 者可为任意 `userId` 发起请求；不是用户级认证 |

生产部署应限制 Agent 只监听 loopback / 受控内网，后续改为短期签名身份或 mTLS。
当前没有改动生产 JWT 策略、数据库 Schema 或业务状态机。

### P1

| 项 | 状态 | 影响 |
| --- | --- | --- |
| 真实模型空流 | 未解决 | 9 次调用 HTTP 200、0 输出；3 条真实模型用例 BLOCKED |
| SSE 主动断开 / 断开后分析 | NOT_COVERED | 无法证明断连时的持久化与分析契约 |
| Testcontainers MQ 隔离 | BLOCKED | 本地 broker 测试不能替代容器隔离 |
| E2E 删除传入 Qdrant collection | 已规避，待代码强制隔离 | 本次用独立 prefix；误传共享名仍有删除风险 |
| B04 两段 Guard 文案 | 待产品决策 | 当前文案不是最终契约 |
| 非法状态迁移 | NOT_COVERED | 没有真实库的反向迁移拒绝测试 |
| 性能基线 | NOT_RUN | 没有生成任何性能数字 |

### 其他

- 语义 rubric 仍无 LLM Judge；不覆盖 DB / 权限 / 状态断言；
- 文档 E2E 的 Agent 使用 mock LLM 与 hash embedding，不代表真实模型质量；
- 测试代码中的默认 DB/RabbitMQ 密码已移除，凭证只从环境变量读取。

## 5. 修复前后对比

| 项 | Phase 1 / 修复前 | Phase 1.1 + Phase 2 后 |
| --- | --- | --- |
| Agent 敏感路由 | 无凭据 200，任意 userId | 401 / 403 / 503；身份绑定 |
| Analyze Fixture | 可写真实认知 | 检索阶段抛错，LLM 0 调用，后端不写 |
| 预算丢弃文案 | 谎称「没有历史」 | 说明「检索到但未包含」 |
| 预算口径 | 300 配置 vs 2699 估算 | 动态上下文 ≤ 预算，完整 prompt 如实报告 |
| History Guard | 忽略按需 Evidence | 合并去重、计数校验 |
| auto 回退 | 静默 | 结构化 `fallbackReason` |
| `recent_history_limit=0` | 注入全部历史 | 不注入，负数非法 |
| 后端权限测试 | Mock 为主 | 真实 JWT + MySQL 12 条 |
| 事务/幂等 | 无真实库证据 | 真实 MySQL 6 条 + MQ 10 条 |
| 文档检索权限 | Mock | 真实 MySQL + 真实 Qdrant |

## 6. 可复现命令

```bash
export AILS_IT_MYSQL_PASSWORD=...
export AILS_SECURITY_IT=true AILS_DOCUMENT_IT=true \
       AILS_DOCUMENT_INDEX_IT=true AILS_DOCUMENT_E2E_IT=true \
       AILS_PHASE2_IT=true AILS_PHASE2_MQ_IT=true AILS_PHASE2_MQ_PASSWORD=...
export AILS_E2E_INTERNAL_TOKEN=... AILS_E2E_QDRANT_COLLECTION=...

./backend/backend/mvnw -Dtest=\
SecurityContextIntegrationTest,DocumentServiceIntegrationTest,\
DocumentIndexServiceIntegrationTest,DocumentIndexE2EIntegrationTest,\
Phase2RealStateIntegrationTest,Phase2MqProcessorIntegrationTest,\
Phase2RabbitMqBrokerTest,ChatServiceTest test
```

演示脚本需要后端、Agent、MySQL 同时运行，凭证从环境变量传入：

```bash
BASE=http://127.0.0.1:8099 AGENT=http://127.0.0.1:8010 \
AILS_INTERNAL_TOKEN=... MYSQL_DB=ails_phase2_e2e \
bash scripts/phase6-demo.sh
```

不要在命令、报告或 Git 中写入真实密码、Token、API Key 或个人数据。
