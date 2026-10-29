# Phase 3.2 安全复核：身份边界与部署面

- 日期：2026-10-28
- 范围：仅复核 Phase 3.1 的身份边界结论；不新增功能、不改密钥、不改 schema
- 原则：只写本轮实际检查到的内容；部署网络隔离仍未验证；不输出任何密钥/Token 值

## 1. 复核结论

| 问题 | 结论 | 本轮证据 |
| --- | --- | --- |
| Agent 进程是否真的没有 delegation 签名密钥？ | **没有**。运行中的 Agent 进程环境变量不含 `AILS_INTERNAL_DELEGATION_SECRET`；该变量只出现在 Spring 进程 | 进程环境读取（只打印变量名/长度）；源码引用审计 |
| 启动配置是否防止密钥/Token 复用？ | **是**。5/5 单测通过：缺密钥、两把密钥相同、密钥与 token 相同、两个 audience 相同均拒绝启动 | `InternalSecurityConfigValidatorTest` 重跑 |
| issuer / audience / 用户 / 有效期是否真的校验？ | **是**。逐项校验 `iss/aud/sub/iat/exp/exp>iat/TTL 上界/scope/jti 非空`；真实 HTTP 24/24 通过 | 源码 + 重跑 `SecurityContextIntegrationTest`（19）与 `InternalIdentityHttpIntegrationTest`（5） |
| `jti` 是否提供重放保护？ | **否**。只校验非空，没有已用 `jti` 存储、没有 replay cache | `InternalIdentitySigner` 源码；全局搜索无其他 `jti` 消费点 |
| 后端是否绑定 `0.0.0.0`？ | **是**，实测 `0.0.0.0:8081` 与 `[::]:8081` 监听 | `netstat -ano` |
| 是否有部署网络隔离证据？ | **没有**。全部测试在 loopback；没有容器端口映射、防火墙或安全组取证 | 本报告第 6 节 |

## 2. Agent 进程的密钥边界（运行态）

运行中的 Agent HTTP 进程（PID 34812）环境变量：

```text
AILS_BACKEND_BASE_URL, AILS_EMBEDDING_DIMENSION, AILS_EMBEDDING_PROVIDER,
AILS_HOST, AILS_INTERNAL_IDENTITY_SECRET, AILS_INTERNAL_TOKEN,
AILS_LLM_PROVIDER, AILS_PORT, AILS_RETRIEVER_MODE
```

- **不包含** `AILS_INTERNAL_DELEGATION_SECRET`；
- 包含 `AILS_INTERNAL_IDENTITY_SECRET`（Spring↔Agent 入站校验所需）；
- 包含 `AILS_INTERNAL_TOKEN`（服务级共享 token）。

同一时刻的 Spring 进程（PID 30020）环境变量包含：

```text
AILS_INTERNAL_DELEGATION_SECRET, AILS_INTERNAL_IDENTITY_SECRET,
AILS_INTERNAL_TOKEN, AILS_AGENT_*, AILS_ANALYSIS_*, SPRING_DATASOURCE_*
```

只打印了变量名与长度，没有打印值。结论：**运行态下 delegation 密钥只在 Spring
进程**；Agent 即使持有 identity-secret，也不能签发后端接受的 `aud=learning-backend`
声明。

源码复核：

- `agent/app/config.py` 不读取 `AILS_INTERNAL_DELEGATION_SECRET`；
- `agent/app/internal_identity.py` 的开发 CLI 在显式提供该变量时能签发后端声明，
  但这不是请求路径；正常部署不设置该变量；
- `agent/app/messaging/task.py` 明确注释 Agent 不持有 delegation-secret；
- Worker/Agent 只把 MQ 任务里的 `backendIdentity` 原样转发。

**残余约束：** 如果运维把 `AILS_INTERNAL_DELEGATION_SECRET` 注入 Agent 环境，
CLI 路径会重新获得签发能力。这是部署配置审计项，不是当前运行态事实。

## 3. 启动配置 fail closed（5/5）

执行：

```text
cd backend/backend
./mvnw -q -o -Dtest=InternalSecurityConfigValidatorTest test
```

结果：`Tests run: 5, Failures: 0, Errors: 0, Skipped: 0`（2026-10-28 00:52）。

覆盖：

| 场景 | 期望 | 结果 |
| --- | --- | --- |
| 内部入口未启用 | 不校验 | 通过 |
| 启用但缺 identity/delegation 密钥 | 拒绝启动 | 通过 |
| identity 与 delegation 密钥相同 | 拒绝启动 | 通过 |
| 密钥与共享 token 相同 | 拒绝启动 | 通过 |
| 两个 audience 相同 | 拒绝启动 | 通过 |

## 4. claims 校验（源码 + 真实 HTTP）

`InternalIdentitySigner.verify` 逐项检查：

| claim | 校验 | 失败结果 |
| --- | --- | --- |
| `iss` | 必须等于 `learning-backend` | 拒绝 |
| `sub` | 必须可转 long 且 > 0 | 拒绝 |
| `aud` | 必须等于调用方期望 audience | 拒绝 |
| `iat` | 不能晚于 `now + clockSkew` | 拒绝 |
| `exp` | 必须晚于 `now - clockSkew`，且 `exp > iat` | 拒绝 |
| TTL | `exp - iat <= maxAllowedTtl` | 拒绝 |
| `scope` | 期望时精确匹配 | 拒绝 |
| `jti` | 只要求非空 | 拒绝空值 |

后端过滤器还把声明 `sub` 与 `X-Internal-User-Id` 交叉核对；不一致时拒绝建立
SecurityContext。请求体 `userId` 不参与身份建立。

真实 HTTP 重跑（独立库 `ails_security_it`，真实 Tomcat 随机端口）：

```text
AILS_SECURITY_IT=true AILS_SECURITY_IT_MYSQL_URL=jdbc:mysql://127.0.0.1:3307/ails_security_it \
AILS_IT_MYSQL_PASSWORD='' ./mvnw -q -o \
  -Dtest=SecurityContextIntegrationTest,InternalIdentityHttpIntegrationTest test
```

结果：**24/24 passed**（19 + 5，2026-10-28 00:52）。覆盖无凭据、错 token、
缺签名、篡改、错 audience/issuer/scope、过期、声明与 Header 不一致、body `userId`
覆盖、内部凭据只读、Agent 密钥伪造后端身份等。

## 5. `jti` 的真实语义

- 签发：`UUID.randomUUID().toString()`；
- 校验：`payload.path("jti").asText("").isBlank()` 时拒绝，**仅此**；
- 没有把 `jti` 写入任何已用集合/表，没有 TTL 缓存，没有消费即失效；
- 全局搜索后端源码，`jti` 只出现在签名器的签发/校验与类注释中。

因此：**同一份委托声明在 `exp` 之前可以重复使用**。`jti` 只用于日志/审计关联，
不能对外宣称“一次性令牌”或“已阻止重放”。委托声明还随 MQ 消息持久化到队列/
死信，TTL 内可被读取。当前默认 TTL 900 秒、最大 3600 秒。缓解措施（更短 TTL、
`jti` 一次性消费、mTLS、轮换/撤销）均未实现。

## 6. 监听地址与部署面

实测：

```text
TCP    0.0.0.0:8081     LISTENING    30020   (Spring Boot)
TCP    [::]:8081        LISTENING    30020
TCP    127.0.0.1:8000   LISTENING    34812   (Agent HTTP)
```

`application.yml` 只有 `server.port: 8080`，没有 `server.address`；本轮以
`SERVER_PORT=8081` 覆盖端口。因此 Spring Boot 使用默认绑定，**确实监听所有网卡**。

Agent 显式 `AILS_HOST=127.0.0.1`，只监听回环。

**未验证：** 容器端口映射、宿主机防火墙、安全组、反向代理、服务网格。本轮所有
请求都走 loopback，不能据此证明生产网络隔离，也不能证明外部主机不可达。不能写
“后端只在内网”。

## 7. 结论

- 运行态证据支持“Agent 没有 delegation 密钥”：Agent 环境变量无该键，Spring
  环境变量有；加上源码路径审计与真实 HTTP 24/24。
- 启动校验 5/5，密钥/Token 复用会失败关闭。
- `iss/aud/sub/iat/exp/scope` 逐项校验，`jti` 只做非空校验，**不是重放防护**。
- 后端绑定 `0.0.0.0`，部署网络边界仍未取证；安全结论仅限应用层，不外推到部署层。
