# Phase 2.1 安全边界与真实 HTTP 验证结果

- 日期：2026-10-27
- 范围：Spring Boot → FastAPI 内部身份、FastAPI 鉴权边界、跨用户/跨空间授权、
  内部身份与用户 JWT 的一致性、失败请求是否触及后端数据层
- 原则：只记录实际执行结果；Mock / Stub / 真实依赖分开；未执行、被阻塞、
  被跳过的项目不计 PASS；不使用真实用户数据；不自动提交 Git

## 1. 结论先行

| 问题 | 结论 | 证据 |
| --- | --- | --- |
| Agent 是否限制在可信网络？ | **本机部署限制在回环**：Agent `127.0.0.1:8000/8010`，RabbitMQ 容器端口绑定 `127.0.0.1`；后端 Spring 默认监听 `0.0.0.0`，属于部署风险 | `netstat` + 配置/容器文件 |
| 用户身份是否与内部凭据可验证绑定？ | **是**：共享 token 只证明调用方是内部服务；用户身份来自 HMAC 签名的短期声明 `sub`，绑定 `aud/iat/exp/jti` | 真实 HTTP 20/20 + 52 条 Agent 安全测试 |
| 伪造 `userId` 能否越权？ | **不能**：只带共享 token + 明文 Header 被 401；声明与 Header 不一致被 403/401；跨用户空间/理解读取返回 404 | L11/L12/L16–L19 |
| 失败请求是否触及后端数据层？ | **没有**：身份校验失败的 Agent 请求在路由依赖阶段终止，后端 stub 收到 0 个请求；后端跨用户读取在 Controller 前由归属校验拒绝 | C-49/C-50、L16/L17 |
| 内部身份与 JWT 权限是否一致？ | **一致**：两者都落到 `SecurityUtil.currentUserId()`，空间/对话归属规则只有一份实现 | D-04–D-09、L14–L20 |
| 是否有残余风险？ | **有**：共享 token 仍是服务级 bearer；后端默认全网卡监听；preview 在 404 时会降级到 Fixture | 第 6 节 |

## 2. 信任边界与凭据持有者

### 2.1 调用方向

| 方向 | 凭据 | 用户身份来源 | 校验点 |
| --- | --- | --- | --- |
| 浏览器 → Spring Boot | 用户 JWT | JWT `userId` | `JwtAuthenticationFilter` |
| Spring Boot → Agent（chat/analyze） | `X-Internal-Token` + `X-Internal-Identity`（`aud=learning-agent`） | 签名声明 `sub` | `agent/app/internal_auth.py` |
| Agent Worker → Spring Boot（只读检索） | `X-Internal-Token` + Spring 签发并随 MQ 下发的 `X-Internal-Identity`（`aud=learning-backend`） | 签名声明 `sub` | `InternalServiceAuthenticationFilter`；Agent 只转发，见 [Phase 3.1](phase3.1-security-results.md) |
| Agent Worker → RabbitMQ | 本机 AMQP 凭据 | 任务消息 `userId` | Worker 内任务 schema + 后端重新鉴权 |

### 2.2 声明格式与有效期

```text
X-Internal-Identity: v1.<base64url(payload)>.<base64url(hmac-sha256)>
payload: {"sub": <userId>, "aud": "learning-agent" | "learning-backend",
          "iat": <epoch seconds>, "exp": <epoch seconds>, "jti": "<uuid>"}
```

- 签名密钥与共享 token 分离；泄露 token 不等于获得伪造身份能力。
- 默认 TTL 60 秒，最大 300 秒，允许 5 秒时钟漂移；`aud` 必须匹配接收方。
- `X-Internal-User-Id` 只用于交叉核对；没有合法签名声明时**不建立用户上下文**，
  不会退回“只信明文 Header”的旧路径。
- 未配置签名密钥时后端拒绝签发，Agent 拒绝接收内部用户请求（fail closed）。

### 2.3 持有者

| 秘密 | 持有者 | 不持有 |
| --- | --- | --- |
| `AILS_INTERNAL_TOKEN` | Spring Boot 进程、Agent 进程 | 前端、浏览器、URL、报告 |
| `AILS_INTERNAL_IDENTITY_SECRET` | Spring Boot 进程、Agent 进程 | 前端、浏览器、Agent 对外响应、日志 |
| 用户 JWT | 浏览器 / 用户 | Agent 的持久化 MQ 消息（Worker 不使用过期 JWT） |

代码与文档中没有把 token/secret 写入仓库；测试脚本从环境变量读取。诊断接口只返回
`internal_auth_configured` / `internal_identity_configured` 布尔值，不返回秘密本身。

### 2.4 实际监听地址

2026-10-27 实测：

```text
TCP 127.0.0.1:8000  LISTENING   Agent（开发）
TCP 127.0.0.1:8010  LISTENING   Agent（演示）
TCP 0.0.0.0:8081    LISTENING   Spring Boot（mq 模式）
TCP 0.0.0.0:8099    LISTENING   Spring Boot（inline 演示）
```

- Agent 默认 `AILS_HOST=127.0.0.1`，未发现 Agent 容器端口映射。
- RabbitMQ `docker/docker-compose.yml` 显式绑定 `127.0.0.1:5672` / `127.0.0.1:15672`。
- **后端仍按 Spring 默认绑定所有网卡**。内部接口需要 token + 签名，JWT 接口需要
  用户认证，但这不等于生产网络边界已经证明；在没有反向代理/防火墙策略前，
  记为部署风险 R-SEC-02。

## 3. 真实 HTTP 用例

执行方式：本机真实进程，不是 MockMvc；后端使用隔离测试库
`ails_phase21_mq`，测试用户均为脚本生成的合成账号。原始输出：
`backend/backend/target/phase2.1-evidence/live-security-checks.txt`
（结果行 `PASS=20 FAIL=0`）。

### 3.1 Agent 侧（127.0.0.1:8000）

| case_id | 请求 | 实际 | 期望 | 结果 |
| --- | --- | --- | --- | --- |
| SEC-2.1-L01 | `GET /health` 无凭据 | 200 `{"status":"ok"}` | 公开最小存活探针 | PASS |
| SEC-2.1-L02 | `GET /health/diagnostics` 无凭据 | 401 | 拒绝 | PASS |
| SEC-2.1-L03 | 同上 + 正确共享 token | 200，仅配置布尔值 | 诊断受保护，token 足够 | PASS |
| SEC-2.1-L04 | `POST /agent/context/preview` 无凭据 | 401 | 拒绝 | PASS |
| SEC-2.1-L05 | 同上 + 错误 token + 合法身份 | 401 | 拒绝 | PASS |
| SEC-2.1-L06 | 同上 + 正确 token、缺少签名身份 | 401 `内部身份声明无效` | 仅共享 token 不能建立身份 | PASS |
| SEC-2.1-L07 | 同上 + 被篡改签名 | 401 | 拒绝 | PASS |
| SEC-2.1-L08 | 同上 + 已过期声明 | 401 | 拒绝 | PASS |
| SEC-2.1-L09 | 同上 + `aud=some-other-service` | 401 | 拒绝 | PASS |
| SEC-2.1-L10 | 同上 + 错误密钥签发 | 401 | 拒绝 | PASS |
| SEC-2.1-L11 | 身份 `sub=9001`，Header `9002` | 403 `X-Internal-User-Id 与签名身份不一致` | 拒绝 | PASS |
| SEC-2.1-L12 | 身份 `sub=9001`，请求体 `userId=9002` | 403 `请求 userId 与内部身份不一致` | 拒绝 | PASS |
| SEC-2.1-L13 | 身份 `sub=9001`，Header/请求体均 9001 | 200（认证通过；数据检索见 3.2） | 放行 | PASS |

说明：L13 的 200 只证明**认证通过**，不证明数据归属。该请求在测试库中
`degraded=true` 并回退 Fixture，原因与结论见第 5 节。

### 3.2 后端侧（127.0.0.1:8081，真实 Spring Security + MySQL）

| case_id | 请求 | 实际 | 期望 | 结果 |
| --- | --- | --- | --- | --- |
| SEC-2.1-L14 | 身份 user1 → `GET /api/user/me` | 200，`data.id=1` | 身份映射正确 | PASS |
| SEC-2.1-L15 | 身份 user1 → 自己的 space1 文档 | 200 | 允许 | PASS |
| SEC-2.1-L16 | 身份 user1 → user6 的 space6 文档 | 404 `学习空间不存在` | 拒绝且不泄露存在性 | PASS |
| SEC-2.1-L17 | 身份 user1 → user6 的 understanding 10 | 404 `学习空间不存在` | 拒绝 | PASS |
| SEC-2.1-L18 | 共享 token + 伪造 `X-Internal-User-Id: 6`，无签名 | 401 | 拒绝旧共享 token 冒充路径 | PASS |
| SEC-2.1-L19 | 身份 user1 + Header `6` | 401 | 拒绝不一致 | PASS |
| SEC-2.1-L20 | 身份 user6 → 自己的 space6 文档 | 200 | 允许 | PASS |

补充只读对照：身份 user6 读取自己的 understanding 10 返回
`conceptName=Cache`；身份 user1 读取同一 id 返回 404。两份响应均不含对方用户内容。

### 3.3 验收项映射

| 要求 | 覆盖用例 | 结论 |
| --- | --- | --- |
| 缺失/错误凭据被拒绝 | L02、L04、L05、L06、L18 | PASS |
| 正确凭据 + 合法身份成功 | L03、L13、L14、L15、L20 | PASS |
| 伪造 `userId` 不能访问他人 | L11、L12、L16、L17、L19 | PASS |
| 篡改/过期/错误 audience 被拒绝 | L07、L08、L09、L10 | PASS |
| 失败请求不调用后端数据逻辑 | C-49、C-50（stub 收到 0 请求）；L16/L17 在归属校验处终止 | PASS |
| JWT 权限与 Agent 内部身份一致 | D-04–D-09 + L14–L20 | PASS |

## 4. 自动化回归

### 4.1 Agent 侧 52 条

执行：`cd agent && python -m pytest tests/test_security_phase11.py tests/test_internal_identity.py tests/test_phase11_regressions.py -q`
结果：**52 passed**（属于全量 `296 passed, 5 skipped` 的一部分）。

| 套件 | 条数 | 真实依赖 / 替身 | 主要断言 |
| --- | ---: | --- | --- |
| `test_security_phase11.py` | 26 | FastAPI `TestClient`；跨用户用例使用本地 HTTP stub 后端 | 无凭据/错 token/缺签名/篡改/过期/错 audience/错密钥/Header 与 body 不一致；跨用户 preview 不混入对方记录；失败请求不触及 stub |
| `test_internal_identity.py` | 14 | 纯 Python 签名/校验 | 绑定 `sub/aud/jti`；错密钥、篡改、过期、未来时间、超长 TTL、畸形 payload、非法 subject |
| `test_phase11_regressions.py` | 12 | 纯函数/构造器 | Fixture 保护、预算、History Guard、`recent_history_limit=0` 等旧缺陷回归 |

用例清单（case_id 与 pytest node id 一一对应，全部 PASS）：

| case_id | node id |
| --- | --- |
| SEC-2.1-C01–C04 | `test_sensitive_routes_reject_missing_credentials[4 个路由]` |
| SEC-2.1-C05–C08 | `test_sensitive_routes_reject_wrong_token[4 个路由]` |
| SEC-2.1-C09 | `test_missing_internal_user_header_is_rejected` |
| SEC-2.1-C10 | `test_missing_signed_identity_is_rejected` |
| SEC-2.1-C11 | `test_tampered_signed_identity_is_rejected` |
| SEC-2.1-C12–C14 | `test_expired_wrong_audience_or_wrong_key_identity_is_rejected[3 组]` |
| SEC-2.1-C15 | `test_identity_and_plain_user_header_must_agree` |
| SEC-2.1-C16 | `test_document_endpoints_stay_protected` |
| SEC-2.1-C17–C19 | `test_body_user_id_cannot_override_internal_identity[3 个路由]` |
| SEC-2.1-C20 | `test_correct_credential_reaches_chat_and_preview` |
| SEC-2.1-C21 | `test_correct_credential_reaches_analyze_but_fixture_guard_blocks_it` |
| SEC-2.1-C22 | `test_health_is_safe_liveness_only` |
| SEC-2.1-C23 | `test_health_diagnostics_requires_internal_credential` |
| SEC-2.1-C24 | `test_cross_user_preview_uses_bound_internal_identity` |
| SEC-2.1-C25 | `test_body_user_id_mismatch_never_reaches_backend` |
| SEC-2.1-C26 | `test_invalid_identity_never_reaches_backend` |
| SEC-2.1-C27 | `test_round_trip_binds_user_audience_and_jti` |
| SEC-2.1-C28 | `test_wrong_secret_audience_and_tampering_are_rejected` |
| SEC-2.1-C29 | `test_expired_and_future_tokens_are_rejected` |
| SEC-2.1-C30 | `test_clock_skew_tolerates_small_drift_only` |
| SEC-2.1-C31 | `test_lifetime_above_maximum_is_rejected` |
| SEC-2.1-C32–C36 | `test_malformed_tokens_are_rejected[5 组]` |
| SEC-2.1-C37–C40 | `test_invalid_subject_is_rejected[4 组]` |
| SEC-2.1-C41–C52 | `test_phase11_regressions.py` 的 12 条（A06/A07/B03/B06/D07/E06） |

### 4.2 Spring Boot 侧

| 套件 | 条数 | 真实依赖 | 结果 | 原始证据 |
| --- | ---: | --- | --- | --- |
| `SecurityContextIntegrationTest` | 15 | 真实 Spring 上下文、真实 MySQL `ails_security_it`、真实 SecurityFilterChain/JWT | 15/15 PASS | `target/phase2.1-evidence/TEST-...SecurityContextIntegrationTest.xml` |
| `InternalIdentitySignerTest` | 4 | 纯签名器 | 4/4 PASS | 同目录 |
| `GlobalExceptionHandlerTest` | 2 | 独立 MockMvc + 真实 Advice | 2/2 PASS | 同目录 |

`SecurityContextIntegrationTest` 的 15 条：

| case_id | 测试方法 | 覆盖 |
| --- | --- | --- |
| SEC-2.1-D01 | `contextLoadsWithSecurityFilterChain` | 过滤器链可启动 |
| SEC-2.1-D02 | `unauthenticatedRequestToProtectedEndpointReturns401Json` | 未登录 401 JSON |
| SEC-2.1-D03 | `jwtAuthenticatedUserCanAccessOwnProfile` | JWT 身份 |
| SEC-2.1-D04 | `forgedBodyUserIdCannotOverrideJwtIdentity` | 请求体伪造 userId 无效 |
| SEC-2.1-D05 | `jwtAuthenticatedUserKeepsDocumentSpacePermissionChecks` | JWT 跨空间 404 |
| SEC-2.1-D06 | `readyIdsEndpointUsesJwtIdentityAndKeepsOwnershipCheck` | ready-ids 归属 |
| SEC-2.1-D07 | `unauthenticatedReadyIdsRequestReturns401` | 未登录拒绝 |
| SEC-2.1-D08 | `internalServiceTokenActsAsUserWithoutBypassingSpaceOwnership` | 内部身份不绕过归属 |
| SEC-2.1-D09 | `internalServiceTokenCanReadReadyIdsOnlyForItsUser` | 内部身份只读自己空间 |
| SEC-2.1-D10 | `internalFilterStillRunsBeforeJwtFilter` | 过滤器顺序 |
| SEC-2.1-D11 | `invalidInternalTokenIsRejected` | 错 token 401 |
| SEC-2.1-D12 | `sharedTokenWithoutSignedIdentityCannotImpersonateAUser` | 仅共享 token 不能冒充 |
| SEC-2.1-D13 | `tamperedOrWrongAudienceIdentityIsRejected` | 篡改/错 audience 401 |
| SEC-2.1-D14 | `identityAndPlainUserHeaderMustAgree` | 声明与 Header 不一致 401 |
| SEC-2.1-D15 | `publicAuthEndpointRemainsReachableWithoutAuthentication` | 登录接口公开 |

### 4.3 “失败请求不触及后端数据/写入”的具体证据

- `test_body_user_id_mismatch_never_reaches_backend`：请求体 `userId=9002`、身份
  `9001`，返回 403，且 HTTP stub 的 `requests == []`；
- `test_invalid_identity_never_reaches_backend`：错误 audience，返回 401，
  stub `requests == []`；
- Java `sharedTokenWithoutSignedIdentityCannotImpersonateAUser`：只有 token 时
  SecurityContext 不写入，请求在 Controller 前被 401；
- SSE 持久化失败用例额外断言 `analysisDispatchService.dispatch(...)` 从未调用，
  见 `phase2.1-integration-results.md` S-03。

## 5. “userId 9001 预览返回数据”排查记录

### 5.1 现象

L13 用合法签名身份 `sub=9001` 调用 preview，返回 200，`retrieved` 包含
`understandings=1, memories=2`。测试库中并不存在 user 9001，因此需要排除越权。

### 5.2 实际证据

- 同一响应字段：`retriever="http:http://127.0.0.1:8081"`、
  `degraded=true`、`fallbackReason="backend_unavailable"`；
- Agent 日志：`后端检索不可用，本次运行降级到 Fixture：后端返回 404：/api/spaces/1/concepts`；
- 后端直连：身份 user1 读取 user6 的 space6 返回 404；身份 user6 读取自己的
  understanding 10 返回 `Cache`；不存在 user 9001；
- 身份 user6、space6 的 preview：`degraded=false`，走真实 HTTP 检索。

### 5.3 结论

**不是跨用户数据泄露**。身份校验通过后，检索按 user 9001 访问后端；后端因为该用户
没有 space1 返回 404，`FallbackRetriever` 随后回退到本地 Fixture。返回的
`understandings=1, memories=2` 是 Fixture 样例，不是 user6 的真实数据。

**但这是一个可观测性缺陷**：404（数据不存在/无权）被标记为
`backend_unavailable`，且 preview 在身份已绑定时仍可展示样例数据，容易让人误以为
“读到了该用户的数据”。记录为 R-P2-08（低危，非数据泄露）；本次不修改检索语义，
避免为通过测试而改变业务逻辑。

## 6. 残余风险

1. **共享 token 仍是服务级 bearer**：持有 token + 签名密钥的一方可以代表任意用户。
   它不是用户级认证；生产应使用 mTLS/服务网格/网络策略，并限制密钥分发。
2. **后端默认监听 `0.0.0.0`**：Agent 与 RabbitMQ 已限制回环，但后端没有显式
   `server.address=127.0.0.1`。在内网/容器部署前必须由反向代理、防火墙或
   Service Mesh 证明边界；否则记为部署风险。
3. **诊断接口只校验共享 token**：`/health/diagnostics` 不要求签名身份，返回
   provider、base URL、布尔配置项，不含用户数据或秘密。若认为配置元数据也敏感，
   应再加签名身份或网络 ACL。
4. **preview 降级标签不准确**（R-P2-08）：404 被写成 `backend_unavailable`；
   预览可能展示 Fixture。分析链路 `allow_degradation=False`，不受此影响。
5. **内部过滤器未配置 token 时是空操作**：不会开放内部访问，但也不会主动报错；
   生产部署应把“未配置”视为配置失败并告警。

## 7. 修复记录

| 修复 | 文件 | 回归 |
| --- | --- | --- |
| 内部用户身份改为短期 HMAC 签名声明，共享 token 单独存在时不再建立身份 | `backend/.../InternalIdentitySigner.java`、`InternalServiceAuthenticationFilter.java`、`agent/app/internal_identity.py`、`internal_auth.py` | Security IT D-12/D-13/D-14；Signer 4/4；Agent C-10–C14 |
| Spring → Agent 与 Worker → Spring 两条方向分别签发 `learning-agent` / `learning-backend` audience | `HttpPythonAgentGateway`、`HttpPythonAgentAnalysisGateway`、`AgentDocumentClient`、`agent/app/retriever/routing.py` | Gateway 单测；52 条 Agent 安全测试 |
| SSE 请求在建立流前失败时，错误响应强制 JSON，避免 `Accept: text/event-stream` 被内容协商改写成 401 | `GlobalExceptionHandler.java` | `GlobalExceptionHandlerTest` 2/2；SSE 跨用户 404 用例 |
| 测试脚本不再把 JWT 写进日志 | `scripts/phase51-e2e.sh`、`phase51-mq-e2e.sh`、`phase52-e2e.sh` | `bash -n`；MQ E2E 复跑无 `eyJ` 输出 |
| 修复 `phase51-e2e.sh` 既有的行拼接语法错误 | `scripts/phase51-e2e.sh` | `bash -n` 通过 |

没有修改 JWT 策略、数据库 Schema、业务状态机或接口契约来适配测试。

## 8. 清理

- 20 条真实 HTTP 检查使用的都是隔离测试库中的合成账号，没有真实用户数据；
- 测试脚本创建的 MQ E2E 用户/空间/会话/run/Evidence/Memory 已清理，
  五个测试队列残留为 0（见集成报告）；
- 报告与日志不包含 API Key、密码、用户 JWT、内部 token 或签名密钥；
- 本阶段未执行 `git add` / `git commit`，未删除任何用户未提交修改。


---

## 9. Phase 3.1 对本文结论的更正（2026-10-28）

本文第 1–8 节记录的是 Phase 2.1 当时的状态。Phase 3.1 修正了两处不能继续沿用的结论：

1. **“Worker → Spring 由 Worker 签发 `aud=learning-backend` 声明”不再成立。**
   Phase 2.1 的实现让 Agent 使用与 Spring 共享的 HMAC 密钥自行签发后端身份，
   等于持有 Agent 密钥即可代表任意用户。现在 `delegation-secret` 只存在于
   Spring 进程；Spring 在投递 MQ 任务时生成 `backendIdentity`，Worker 只原样转发。
   `Agent Worker → Spring Boot` 一行已按此更正。
2. **`jti` 不是重放防护。** 声明有效期内可多次使用；`jti` 只用于日志/审计关联。
   泄露的委托声明在 `exp` 前仍可重放，这是明确接受的残余风险，不能表述为
   “一次性令牌”或“已阻止重放”。

Phase 3.1 的新增证据见 `docs/testing/phase3.1-security-results.md`：真实 HTTP
24/24、启动配置校验 5/5、Agent 测试 322 passed / 5 skipped（统计口径见该报告）。
