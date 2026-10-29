# Phase 3.1 安全结果：内部身份签发边界

- 日期：2026-10-28
- 范围：Spring Boot 内部身份签发、Agent 转发、后端验证、`jti` 语义、启动配置
- 原则：只记录实际执行的测试；未执行的部署网络限制标为“未验证”；不输出任何密钥值

## 1. 结论摘要

| 问题 | 结论 | 证据 |
| --- | --- | --- |
| Agent 能否用自己的签名材料签发后端接受的任意用户身份？ | **不能**。Agent 运行路径不再自签 `aud=learning-backend`；后端只接受 `delegation-secret` 签发的声明，而该密钥不进入 Agent 进程 | `agent/app/retriever/routing.py`；`test_agent_identity_secret_cannot_authorize_backend_reads`；`InternalIdentitySignerTest.delegationTokenBindsScopeAndUsesSeparateKey` |
| 用户身份的可信签发方是谁？ | Spring Boot。它用仅自己持有的 `delegation-secret` 签发 `iss=learning-backend`、`aud=learning-backend`、`sub=userId`、`scope=analysis:read` 的短期声明 | `InternalIdentitySigner.signDelegation`；`AnalysisTaskService`；真实 HTTP `validDelegationReadsAsUserOverHttp` |
| 其他服务为什么信任这些 claims？ | 后端用同一把 delegation 密钥做 HMAC 校验，并逐项校验 `iss/aud/sub/iat/exp/jti/scope/TTL`；共享 `X-Internal-Token` 只证明“调用方是内部服务”，单独存在不建立用户身份 | `InternalServiceAuthenticationFilter`；`InternalIdentitySigner.verifyDelegation`；`sharedTokenWithoutSignedIdentityCannotImpersonateAUser` |
| `jti` 是什么语义？ | **短期多用的关联 id，不是重放防护**。校验只要求非空；没有 replay cache，声明在 `exp` 前可重复使用 | `InternalIdentitySigner` 类注释与 `verify` 实现；本报告第 6 节 |
| 内部凭据能否写业务数据？ | **不能**。过滤器只为 `GET /api/**` 建立内部身份；POST/PUT/PATCH/DELETE 不走这条分支 | `InternalServiceAuthenticationFilter.doFilterInternal`；`internalCredentialCannotWriteBusinessData` |
| 配置缺失/冲突时是否 fail closed？ | **是**。内部入口启用但密钥缺失、两把密钥相同、密钥与 token 相同、两个 audience 相同时，启动直接失败 | `InternalSecurityConfigValidator`；`InternalSecurityConfigValidatorTest` 5/5 |
| 部署网络限制是否有证据？ | **未验证**。代码中后端只配置了 `server.port=8080`，没有 `server.address`；Agent 默认 `127.0.0.1`。实际容器端口映射/防火墙未在本阶段取证 | `application.yml`；`agent/app/config.py` |

## 2. 修正前后的链路

### 修正前

```text
Spring --(identity-secret, aud=learning-agent)--> Agent
Agent   --(同一把 identity-secret, 自签 aud=learning-backend)--> 后端
```

Agent 只要持有 HMAC 密钥，就能为任意 `sub` 生成后端接受的声明。共享密钥在这里
同时承担了“服务认证”和“用户身份签发”，边界不成立。

### 修正后

```text
同步 chat/analyze:
  Spring --(identity-secret, aud=learning-agent)--> Agent
  Agent  --(用户 JWT，原样透传)--> 后端

异步 MQ:
  Spring --(delegation-secret, aud=learning-backend, scope=analysis:read)-->
          AnalysisTaskMessage.backendIdentity
  Worker --(X-Internal-Token + 原样转发 backendIdentity)--> 后端只读接口
```

- `identity-secret`：Spring 与 Agent 共享，只用于 `aud=learning-agent`；Agent 即使
  能签这个 audience，也只影响 Agent 自己的入口，后端不接受。
- `delegation-secret`：**只存在于 Spring 进程**。Agent 配置不读取它；运行路径也不
  调用任何后端 audience 的签发函数。
- Agent 的 `internal_identity.py` 开发 CLI 仍可在显式提供
  `AILS_INTERNAL_DELEGATION_SECRET` 时签发声明，但这不是 Agent 请求路径，正常部署
  不设置该变量。它只用于本地运维/测试，且需要主动提供密钥。

## 3. claims 与校验

声明格式：`v1.<base64url(payload)>.<base64url(HMAC-SHA256)>`。

| claim | 签发 | 校验 | 说明 |
| --- | --- | --- | --- |
| `iss` | `learning-backend` | 必须精确匹配 | 防止其他签发方复用同一密钥格式 |
| `sub` | 目标 userId（正整数） | 非空、可转 long、>0 | 可信用户身份只来自这里 |
| `aud` | `learning-agent` 或 `learning-backend` | 必须等于调用方期望的 audience | 两把密钥、两个 audience 分离 |
| `iat` | 当前秒 | 不能晚于 `now + clockSkew` | 防止未来签发 |
| `exp` | `iat + ttl` | 必须晚于 `now - clockSkew`，且 `exp > iat` | 过期即拒绝 |
| `jti` | UUID | **只校验非空** | 审计关联；不是重放防护 |
| `scope` | 委托声明固定 `analysis:read` | 期望 scope 时精确匹配 | 只读分析委托，不含写权限 |
| TTL 上界 | — | 委托声明 `exp-iat <= 3600s`；Agent 身份 `<=300s` | 防止签发超长令牌 |

后端还把 `X-Internal-User-Id` 与声明的 `sub` 做交叉核对；不一致时拒绝建立
SecurityContext。请求体里的 `userId` 从不参与身份建立。

## 4. 实际执行的测试

### 4.1 Agent（确定性测试，不调用真实模型）

执行：

```text
cd agent
python -m pytest tests/test_security_phase11.py tests/test_internal_identity.py \
  tests/test_phase31_evaluator_regressions.py tests/test_history_guard.py -q
```

结果：**81 passed**。

关键用例：

| 用例 | 验证内容 |
| --- | --- |
| `test_agent_identity_secret_cannot_authorize_backend_reads` | 用 Agent 的 `identity-secret` 伪造 `aud=learning-backend` 声明，请求失败且后端 stub 收到 0 次请求 |
| `test_analyze_without_delegation_token_fails_closed` | 只有共享 token + 入站身份、没有 `backendIdentity` 时返回 503，不访问后端、不退回 Fixture |
| `test_body_user_id_mismatch_never_reaches_backend` | body `userId` 与签名身份不一致时，后端 stub 收到 0 次请求 |
| `test_invalid_identity_never_reaches_backend` | 错误 audience 的声明被拒绝，且不进入检索 |
| `test_cross_user_preview_uses_bound_internal_identity` | 两个用户分别使用 Spring 签发的委托声明，只能读到各自的 stub 记录 |
| `test_missing_signed_identity_is_rejected` / `test_tampered_signed_identity_is_rejected` | 缺签名、篡改签名均 401 |
| `test_expired_wrong_audience_or_wrong_key_identity_is_rejected` | 过期、错 audience、错密钥三组均拒绝 |
| `test_identity_and_plain_user_header_must_agree` | 声明 `sub` 与明文 Header 不一致时拒绝 |
| `test_backend_audience_token_requires_the_delegation_key` | 后端 audience 的声明只能用 delegation 密钥签发/校验；Agent 密钥签的被拒绝 |
| `test_issuer_is_required_and_must_match` | 缺失或错误 issuer 被拒绝 |
| `test_scope_is_preserved_and_validated` | `scope` 往返保留，错误 scope 被拒绝 |
| `test_schema_v2_task_without_backend_identity_is_invalid` | `schemaVersion=2` 但 `backendIdentity` 为 `None`/空串/纯空白时，消息模型直接拒绝（3 组参数） |

MQ 消息入口的校验：`schemaVersion>=2` 的消息缺少可信用户身份属于合同错误，
在 pydantic 模型层就抛出 `ValidationError`，Worker 归类为 `MESSAGE_INVALID`
（不可重试）并进入死信，而不是先接受、到 Analyze 阶段才 503。对应测试
`test_analysis_messages.py` + Worker 可靠性/消息测试共 **26 passed, 2 skipped**
（2 条真实 Broker 测试未启用）。

### 4.2 Spring Boot 单元测试

执行：

```text
cd backend/backend
./mvnw -Dtest=InternalIdentitySignerTest,InternalSecurityConfigValidatorTest,\
HttpPythonAgentAnalysisGatewayTest,HttpPythonAgentGatewayTest,AnalysisTaskServiceTest test
```

结果：**39/39 passed**（其中 signer 6、validator 5、两个 Gateway 各 10、
AnalysisTaskService 8）。

| 用例组 | 验证内容 |
| --- | --- |
| `agentTokenRoundTripBindsSubjectAudienceAndIssuer` | `aud=learning-agent` 声明不能用于后端 audience |
| `delegationTokenBindsScopeAndUsesSeparateKey` | 委托声明绑定 `analysis:read`，且 Agent 密钥签的后端声明被拒绝 |
| `tamperedTokenIsRejected` | 篡改 payload/签名、非 token、null 均拒绝 |
| `unconfiguredSignerFailsClosed` | 未配置密钥时不能签发、不能验证 |
| `invalidDelegationClaimsAreRejected` | 错 scope、错 issuer、错 audience、超 TTL 均拒绝 |
| `expiredAndFutureTokensAreRejected` | 过期与未来签发均拒绝 |
| `InternalSecurityConfigValidatorTest` | 未启用不校验；缺密钥、密钥相同、token 相同、audience 相同均拒绝启动 |

### 4.3 真实 HTTP + 真实 MySQL

环境：MySQL 8.0.37 容器，独立库 `ails_security_it`，真实 Tomcat 随机端口，
`AILS_SECURITY_IT=true`。执行：

```text
AILS_SECURITY_IT=true AILS_SECURITY_IT_MYSQL_URL=jdbc:mysql://127.0.0.1:3307/ails_security_it \
AILS_IT_MYSQL_PASSWORD='' ./mvnw -Dtest=SecurityContextIntegrationTest,InternalIdentityHttpIntegrationTest test
```

结果：**24/24 passed**。

| 测试类 | 数量 | 覆盖 |
| --- | --- | --- |
| `InternalIdentityHttpIntegrationTest` | 5 | 真实 HTTP：无凭据 401；合法委托 200；Agent 密钥伪造后端身份 401；篡改/错 audience/错 issuer/过期 401；body `userId` 不覆盖 JWT 身份 |
| `SecurityContextIntegrationTest` | 19 | 完整 SecurityFilterChain：无凭据、错 token、缺签名、篡改、错 audience/issuer/scope、过期、声明与 Header 不一致、内部凭据只读、body `userId`、正常 JWT 权限 |

重点用例：

- `agentIdentitySecretCannotForgeBackendAcceptedUser`：用 identity 密钥签
  `aud=learning-backend`，后端 401。
- `expiredDelegationIsRejectedBeforeBusinessAccess`：过期委托在进入业务前被拒绝。
- `internalCredentialCannotWriteBusinessData`：有效的委托声明 + POST 仍返回 401，
  且数据库中没有新增 `learning_space` 行。
- `bodyUserIdCannotOverrideJwtIdentityOverHttp`：真实 HTTP 下 body 传入他人
  `userId`，创建出的空间仍属于 JWT 用户。
- `internalServiceTokenActsAsUserWithoutBypassingSpaceOwnership`：内部身份也不能
  绕过空间归属校验。

### 4.4 全量后端回归

执行：`./mvnw test`（MySQL 指向独立容器，`AILS_SECURITY_IT=true`）。

结果：**243 tests, 0 failures, 0 errors, 41 skipped**。跳过的 41 条是未启用的
外部依赖测试（RabbitMQ broker、文档索引 E2E、SSE 等），不能计为 PASS；本阶段
没有把它们计入安全结论。

## 5. 被拒绝的请求不会进入业务检索或写库

| 层 | 证据 |
| --- | --- |
| Agent 请求体 `userId` 与签名不一致 | `test_body_user_id_mismatch_never_reaches_backend`：后端 stub 请求数 0 |
| Agent 收到错误 audience | `test_invalid_identity_never_reaches_backend`：不进入检索 |
| Agent 用自身密钥伪造后端身份 | `test_agent_identity_secret_cannot_authorize_backend_reads`：stub 请求数 0 |
| 后端声明缺失/无效 | 过滤器不建立 SecurityContext，下游返回 401；真实 HTTP 用例覆盖 |
| 后端收到有效内部身份但尝试写 | `internalCredentialCannotWriteBusinessData`：401 且数据库行数不变 |

## 6. `jti` 的实际语义与残余风险

- 签发：每个声明有随机 UUID `jti`。
- 校验：只要求 `jti` 非空字符串；**没有**记录已使用的 `jti`，没有 replay cache。
- 因此：同一份声明在 `exp` 之前可以重复使用。`jti` 只用于日志/审计关联，
  不能对外宣称“一次性令牌”或“已阻止重放”。

**接受的残余风险：**

1. **委托声明在 MQ 中持久化**。schemaVersion=2 的任务消息携带 `backendIdentity`；
   消息在队列/死信中的拷贝在 TTL 内仍可用。当前 TTL 默认 900 秒，最大 3600 秒。
2. **短期重放窗口**。拿到声明的一方可在过期前重复读取该用户允许的 GET 资源。
   影响范围受 `scope=analysis:read`、用户空间归属校验和只读方法限制。
3. **共享 token 仍是服务级 bearer**。它不能单独建立用户身份，但与有效委托声明
   同时泄露时，攻击者可在 TTL 内代表该用户读取。
4. **Agent 仍持有 `identity-secret`**。这是 Spring → Agent 入站校验所必需；
   Agent 可以签发 `aud=learning-agent` 的声明，但后端不接受该 audience。

**未实现（明确标记）：** 重放检测、`jti` 一次性消费、mTLS/工作负载身份、
密钥轮换。未做的工作不写成已具备。

## 7. 未验证项

| 项目 | 状态 | 原因 |
| --- | --- | --- |
| 生产部署的实际网络隔离（安全组、容器端口映射、防火墙） | **未验证** | 本阶段没有部署环境证据；代码只显示后端未设置 `server.address` |
| Spring → Agent → 后端三进程完整链路 | **未执行** | 本阶段分别验证了 Spring 真实 HTTP、Agent 请求路径与后端验证；MQ E2E 脚本已更新为 v2，但未在本阶段运行 |
| 密钥轮换/泄露后的撤销 | **未实现** | 当前只有 TTL 自然过期 |
| 真实用户越权测试 | **未执行且禁止** | 所有测试使用合成 fixture / 独立测试库 |

## 8. 结论

内部身份签发边界已按最小改动修正：Agent 不再能用自己的密钥签发后端接受的用户
身份；后端只信任 Spring 用独立 delegation 密钥签发的、短期、只读、audience/scope
受限的声明。真实 HTTP 24/24、启动校验 5/5、Agent 定向安全测试 81/81 均通过。
`jti` 仍不是重放防护，委托声明在 TTL 内可重放，这是当前最重要的安全残余风险。
