# Phase 3.3 安全边界：委托声明有效期、队列延迟与部署面

- 日期：2026-10-28
- 范围：`backendIdentity` 的签发/存储/过期行为、Agent 密钥边界、后端监听与可达性
- 原则：只写实际检查到的内容；不输出任何密钥、Token 或签名值；不修改现有网络配置
- 结论：过期委托声明会导致**有界重试后失败并死信**，不会无限重试，也不会写成
  Agent 成功；但过期声明会随消息保留在 DLQ。部署网络隔离仍**未验证**；Agent 侧
  **没有**启动检查阻止 delegation 密钥被注入。

## 1. 结论摘要

| 问题 | 结论 | 证据 |
| --- | --- | --- |
| `backendIdentity` 的实际有效期与签发时机 | 委托声明 TTL 默认 **900s**（最大 3600s）；Spring 在 `createRun` 之后、`publish` 之前立即签发 | `application.yml:171-172`、`AnalysisTaskService.java:81-97` |
| 它在队列 / retry / DLQ 中如何存储 | 作为任务 JSON 的 `backendIdentity` 字段随 body 持久化；retry 与 DLQ 都复制原始 body，不做脱敏 | `worker.py:_publish_request_retry/_dead_letter`；Phase 3.2 E2E |
| 排队/重试/延迟超过 `exp` 会怎样 | 后端返回 401；Worker 归类为 `RETRIEVAL_UNAVAILABLE`（可重试），最多 **3 次 Runtime 调用、2 次 retry 等待（约 30s）**，随后 `FAILED` + 死信 | 源码 + 新增回归测试 + Phase 3.2 真实 401 场景 |
| 是否会无限重试 | **不会**。`analysis_max_retries=3` 在入口和最后一次失败处双重限制 | `worker.py:243-275`；新增测试 |
| 是否会产生错误业务状态 | **不会**。失败任务不写 Evidence/Revision/Memory；后端将 run 置 `FAILED`，rules-v1 兜底是独立 run | Phase 3.2 E2E；结果处理器 |
| `jti` 是否防重放 | **否**。只校验非空；没有已用集合/缓存 | `InternalIdentitySigner`；Phase 3.2 安全复核 |
| Agent 运行态是否持有 delegation 密钥 | 正常启动环境**没有**；但注入该变量后进程仍能启动，**没有启动门禁** | 本轮进程环境探针 |
| 后端监听与网络边界 | 监听 `0.0.0.0:8081` / `[::]:8081`；容器可经宿主 LAN IP 访问；外部主机可达性未测 | `netstat`、容器探针、防火墙查询 |

## 2. 委托声明的签发时机与有效期

`AnalysisTaskService.enqueue` 的顺序：

1. `createRun(...)` 创建 `agent_run`；
2. `identitySigner.signDelegation(conversation.getUserId())` 签发 `backendIdentity`；
3. `publisher.publish(buildMessage(...))` 发布任务。

也就是说，声明在**发布前一刻**签发，不是用户发消息时、也不是 Worker 消费时。
这已经是最小化“签发到入队”年龄的做法，没有需要前移的空间。

配置默认值：

| 配置 | 默认 | 上限/说明 |
| --- | --- | --- |
| `ails.internal.delegation-ttl-seconds` | 900s | 最大 3600s |
| `ails.internal.delegation-max-ttl-seconds` | 3600s | 校验端允许的 TTL 上界 |
| `ails.internal.identity-clock-skew-seconds` | 5s | `iat`/`exp` 容忍的时钟漂移 |
| `ails.internal.identity-ttl-seconds` | 60s | 这是 Spring→Agent 入站声明，不是 MQ 里的 `backendIdentity` |

`backendIdentity` 的 claims 为 `iss=learning-backend`、`sub=userId`、
`aud=learning-backend`、`scope=analysis:read`、`iat/exp/jti`，由
`delegation-secret` 做 HMAC-SHA256。Agent 进程不持有该密钥，只转发。

## 3. 队列、retry 与 DLQ 中的存储

| 位置 | body 中的 `backendIdentity` | 保留时间 | 备注 |
| --- | --- | --- | --- |
| request queue | 原样 | durable，无 TTL | 未被消费前一直存在 |
| retry queue | 原样复制 | durable，`x-message-ttl=15000` | 到期回 request；副本仍带原声明 |
| DLQ | 原样复制 | durable，无 TTL | 不会自动重放；可长期保留过期声明 |
| result queue | 结果消息不含 `backendIdentity` | durable | 结果 schema 仍为 1 |

重试与死信的实现都是“复制原 body + 最小排查 header”，没有对
`backendIdentity` 做删除、加密或替换。因此：

- 声明在队列中是**明文持久化**的（取决于 RabbitMQ 的磁盘/传输配置，本轮未验证
  队列加密或 TLS）；
- 过期后，DLQ 里的声明仍然存在，直到人工清理或归档；
- 任何能读取 DLQ/队列文件或管理接口的人，在 `exp` 之前都可以重放该声明
  （`jti` 不提供重放保护）。

这不是本轮引入的问题，但升级/排障时不能把 DLQ 当作“安全归档”。

## 4. 超过 `exp` 后的实际行为

### 4.1 调用链

1. Worker 把 `backendIdentity` 放进后端内部请求；
2. 后端 `InternalIdentitySigner.verify` 校验 `exp`（允许 5s skew），过期返回 401；
3. `HttpBackendRetriever._get` 对 401 抛 `RetrievalError`（即使 `fatal=False`）；
4. `classify_failure` 把 `RetrievalError` 映射为
   `("RETRIEVAL_UNAVAILABLE", True)`；
5. Worker 在 `retries=0`、`retries=1` 时各发一份 retry 副本（每份等待 15s）；
6. `retries=2` 是第三次也是最后一次 Runtime 调用；失败后发布 `FAILED`
   （`retryable=False`）并投死信；
7. 后端把 run 置 `FAILED`；rules-v1 兜底生成独立的 `COMPLETED` run，不冒充
   Agent 分析。

**次数口径：** `analysis_max_retries=3` 在当前实现中表示“最多 3 次执行”，
不是“3 次重试”。实际是 **3 次 Runtime 调用、2 次 retry 队列等待**，约 30s
（不含单次分析耗时），不是 45s。这一点容易在文档和监控中读错。

### 4.2 新增的有界、可重复用例

本轮新增 3 个确定性测试，不依赖真实模型/后端，可重复执行：

| 测试 | 证明什么 |
| --- | --- |
| `agent/tests/test_worker_reliability.py::test_expired_backend_identity_is_bounded_and_dead_lettered` | 401 过期声明：2 次 retry 副本、3 次 Runtime 调用后 `FAILED` + 死信；`retryable=False`；死信保留原始 body |
| `agent/tests/test_internal_identity.py::test_expired_delegation_claim_is_rejected_for_backend_audience` | `aud=learning-backend` 的委托声明在 `exp + skew` 后确实被拒绝 |
| `agent/tests/test_knowledge_retrieval.py::test_fatal_401_from_backend_is_retrieval_error` | 后端 401 在致命读取路径上映射为 `RetrievalError`，能被 Worker 归类 |

执行结果：

```text
cd agent
python -m pytest -q tests/test_worker_reliability.py \
  tests/test_internal_identity.py tests/test_knowledge_retrieval.py
55 passed
```

全量 Agent：

```text
331 passed, 5 skipped, 1 warning
```

**证据边界：** 上述是 Worker/映射层的确定性测试，不是“真实过期 token 跑完整
E2E”。Phase 3.2 的真实 401 场景证明了 Worker 在真实后端 401 时会进入
retry 队列并在恢复后成功（`retries=1`），但没有把真实 401 跑到耗尽；耗尽路径由
本轮确定性测试覆盖。两者分开报告，不把单测算作 E2E。

### 4.3 是否会写错业务状态

- Worker 在重试期间不发布结果，后端 run 保持 `PENDING`；
- 最终发布 `FAILED`，`retryable=False`，后端结果处理器按失败落终态；
- 失败任务不写 Evidence、Revision、Memory 或 Memory 来源；
- rules-v1 兜底是独立 run，带 `OBSERVATION/UNKNOWN/rules-v1`，不会推进
  Understanding/Revision/Memory。

Phase 3.2 的非法消息场景已经用真实 MySQL 断言了“失败 run 的
`output_summary IS NULL`、无 Agent 学情写入”；过期 401 走同一条失败/兜底路径。

## 5. 是否调整签名时机、委托 TTL 或任务契约

| 项目 | 本轮决定 | 理由 |
| --- | --- | --- |
| 签名时机 | **不调整** | 已在发布前一刻签发；再前移只会增加暴露时间 |
| 委托 TTL 900s | **不调整** | 远大于 2×15s 重试窗口；缩短会增加正常排队过期的概率 |
| `delegation-max-ttl` 3600s | **不调整** | 这是校验上界，不是默认签发值；收紧需要单独评估现有调用方 |
| 401 重试分类 | **本轮不改代码** | 现有重试有界且不写错误状态；改成非重试需要区分 401 的瞬态/配置错误，属于契约变更，留待单独评审 |
| 过期错误码 | **本轮不改** | 当前统一为 `RETRIEVAL_UNAVAILABLE`；诊断依赖 `error_message`。建议后续增加更具体的鉴权错误码，但不在本轮改 |
| DLQ 中过期声明 | **记录风险** | DLQ 无 TTL；建议限制访问、定期归档/清理；不自动重放 |

**监控建议：** 对 request/retry 队列的最老消息年龄设告警（例如 5 分钟），
而不是只看深度。900s 的 TTL 足以覆盖正常重试，但覆盖不了“队列长时间不消费”；
后者应作为部署/容量事件处理。

## 6. Agent 的 delegation 密钥边界（运行态）

### 6.1 正常环境

用真实 `uvicorn app.main:app` 在受控环境中启动（显式移除该键），环境只包含：

```text
AILS_INTERNAL_IDENTITY_SECRET, AILS_INTERNAL_TOKEN,
AILS_LLM_PROVIDER, AILS_RETRIEVER_MODE
```

读取该进程的环境变量键（只打印键名）：

```text
normal_env: alive=True delegation_key=False
```

即：在这种启动方式下，Agent **没有** `AILS_INTERNAL_DELEGATION_SECRET`。
Phase 3.2 对当时实际运行的 Agent 进程（PID 34812）做环境检查时也没有发现该键；
仓库内 `agent/.env` 也不含该键（只检查键名，没有读取值）。
源码中 `agent/app/config.py` 也不读取该变量；`Settings` 使用
`extra="ignore"`，未知的 `AILS_*` 变量会被忽略。正常请求路径只转发 MQ 任务里的
`backendIdentity`，不会自行签发后端接受的声明。

### 6.2 注入检查：没有启动门禁

用同样的启动方式，额外注入 `AILS_INTERNAL_DELEGATION_SECRET`：

```text
injected_delegation_env: alive=True delegation_key=True
```

进程**正常启动并保持存活**，没有拒绝该变量。也就是说：

- 运行态正常配置没有密钥：**已验证**；
- “部署配置检查能阻止注入”：**未实现**。Agent 侧没有启动校验会因该变量存在而
  失败关闭；
- `agent/app/internal_identity.py` 的开发 CLI 在显式提供该变量时能签发后端声明；
  这不是请求路径，但说明“注入后完全无害”不成立。

**建议（未实施）：** 在 Agent 启动时检查 `AILS_INTERNAL_DELEGATION_SECRET`
是否存在，存在则拒绝启动；或在部署层用独立 secret 作用域/准入策略保证它只挂载到
Spring。当前不能对外宣称“Agent 不可能获得 delegation 密钥”。

Spring 侧在探针启动命令中设置了该变量（值未打印），这与 Phase 3.2 的
“密钥只在 Spring 进程”结论一致。

## 7. 后端监听地址与真实可达性

### 7.1 实测

以 `SERVER_PORT=8081`、未设置 `server.address` 启动 Spring Boot 后：

```text
TCP    0.0.0.0:8081    LISTENING    13488
TCP    [::]:8081       LISTENING    13488
```

`application.yml` 只有 `server.port: 8080`，没有 `server.address`，因此使用
默认绑定（所有网卡）。本次用 8081 覆盖端口。

从宿主机访问 LAN 地址：

```text
host_lan_http=401
```

从独立的 `ubuntu:24.04` 容器（Docker bridge 网络命名空间）访问同一 LAN 地址：

```text
LAN_8081_OPEN
```

对照：同一容器访问自己的 `127.0.0.1:8081`：

```text
CONTAINER_LOOPBACK_CLOSED
```

这说明：

- 后端不是只监听 loopback；从另一个网络命名空间可以经宿主 LAN IP 建立 TCP 连接；
- 容器探针没有误连到容器自己的端口（对照为 CLOSED）；
- 应用层返回 401，说明连接进入了 Spring Security，而不是某个占位端口。

探针结束后已停止后端进程，确认 `8081` 不再监听。

### 7.2 容器端口映射

```text
ails-mysql-it       127.0.0.1:3307->3306/tcp
ails-rabbitmq       127.0.0.1:5672->5672/tcp, 127.0.0.1:15672->15672/tcp
learning-space-qdrant 127.0.0.1:6333->6333/tcp
```

本地基础设施只绑定 loopback；后端是宿主进程，不在上述容器编排中。仓库的
`docker/docker-compose.yml` 也只定义本地 RabbitMQ，不包含后端/Agent 的生产部署。

### 7.3 防火墙

```text
Domain   Enabled=True  DefaultInboundAction=NotConfigured
Private  Enabled=True  DefaultInboundAction=NotConfigured
Public   Enabled=True  DefaultInboundAction=NotConfigured
NO_RULE_FOR_TCP_8081
```

- 三个配置文件都启用；
- 没有显式的 TCP 8081 入站规则；
- `DefaultInboundAction=NotConfigured` 不能单独推出“外部一定被阻断”或“外部一定
  可达”，需要实际从另一台主机测试。

### 7.4 哪些是已证实、哪些仍未验证

| 结论 | 状态 |
| --- | --- |
| 后端监听所有网卡（`0.0.0.0` / `[::]`） | **已实测** |
| 从同宿主容器经 LAN IP 可建立 TCP 连接 | **已实测** |
| 从另一台物理主机可访问 | **未验证** |
| 生产防火墙/安全组/反向代理已隔离 | **未验证**；仓库无配置 |
| MySQL/RabbitMQ/Qdrant 本地端口仅 loopback | **已实测**（docker ps） |
| Agent 只监听 loopback | 配置默认 `127.0.0.1`，Phase 3.2 运行态 `127.0.0.1:8000`；本轮未重启 8000 |

因此不能写“后端只在内网”或“生产网络已隔离”。可以写“应用默认监听所有网卡，
且在本机容器网络下可达；外部边界未取证”。

## 8. 部署拓扑建议（仅建议，未改动现有配置）

如果要在现有架构上收紧边界，建议按以下顺序评估：

1. 后端 `server.address` 绑定到明确的私网地址或 `127.0.0.1`，由反向代理/网关
   暴露必要接口；
2. RabbitMQ/MySQL/Qdrant 继续只绑定 loopback 或独立内部网络，不暴露管理端口；
3. Agent HTTP 只监听私网/loopback，Worker 不需要公网入口；
4. 在防火墙/安全组只放行反向代理到后端的端口，不放行 8081 到办公网/公网；
5. 队列与 DLQ 的访问用独立账号/权限控制，避免过期委托声明被随意读取。

以上均未在本轮实施，也没有改变用户现有网络配置。

## 9. 结论

- `backendIdentity` 在发布前一刻签发，TTL 900s；随消息明文持久化到 request、
  retry 和 DLQ。
- 过期后后端 401，Worker 有界重试（3 次执行、2 次等待，约 30s），最终
  `FAILED` + 死信；不会无限重试，不会写 Agent 成功状态；DLQ 会保留过期声明。
- `jti` 仍不是重放防护；本轮没有引入一次性令牌。
- 正常 Agent 进程没有 delegation 密钥；但注入该变量不会被启动检查拒绝，
  这是未修复的部署配置缺口。
- 后端监听所有网卡，且在同宿主容器网络下可达；外部主机与生产隔离仍未验证。
