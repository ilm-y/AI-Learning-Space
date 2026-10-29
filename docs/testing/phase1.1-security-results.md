# Phase 1.1 安全修复结果（B07 / B06 与上下文缺陷）

- 日期：2026-10-27
- 范围：Agent HTTP 鉴权、Analyze Fixture 保护、Context Builder 预算、History Guard、
  auto 回退可观测性、`recent_history_limit=0` 契约
- 原则：只做最小安全隔离与缺陷修复；未改生产 JWT 策略、数据库 Schema、
  关键依赖或业务状态机；未自动提交 Git

## 0. 基线记录

Phase 1.1 开始时工作区已有未提交修改（38 个路径，含 `agent/`、`backend/`、
`evaluation/`、`scripts/`、`docs/`）。本阶段全部改动都叠加在这些修改之上，
没有执行 `git add` / `git commit`。

过程说明：在给演示脚本补内部请求头时，曾用行级补丁误删/错位
`scripts/phase51-e2e.sh`、`phase52-e2e.sh`、`regress-closed-loop.sh` 的少量行。
已将 `regress-closed-loop.sh` 恢复为 HEAD 后重新应用本阶段需要的修改；另外两个
脚本只补回被误删的请求体行和内部请求头。当前四个相关脚本均通过 `bash -n`，
`git diff` 中只保留预期改动。

## 1. B07：Agent HTTP 端点鉴权（P0）

### 1.1 修复前

用 FastAPI `TestClient` 直接请求实际 `app.main.app`：

| 检查 | 修复前 |
| --- | --- |
| `/agent/chat` 无凭据 | HTTP 200 |
| `/agent/context/preview` 无凭据 | HTTP 200 |
| 请求体任意 `userId` | 被接受 |
| `/health` | 返回 provider / backend 等内部配置 |

根因：端点不校验调用方身份，直接信任请求体 `userId`。任何能访问 Agent 端口的人
都可以读取指定用户的学习状态、Memory、Evidence，并触发模型调用。

### 1.2 修复内容

新增 `agent/app/internal_auth.py`：

- 敏感路由统一依赖 `require_internal_user`；
- 请求头：`X-Internal-Token` + `X-Internal-User-Id`；
- `AILS_INTERNAL_TOKEN` 未配置时 **fail closed**，返回 503；
- token 使用 `secrets.compare_digest` 常量时间比较；
- `X-Internal-User-Id` 必须是正整数；
- 业务请求体 `userId` 必须与头部身份一致，否则 403，且不会进入检索层。

路由变化：

- `/agent/chat`、`/agent/analyze`、`/agent/context/preview` 全部要求内部凭据；
- `/health` 只返回 `{"status":"ok"}`，不再暴露 provider / base URL / token 状态；
- 诊断信息移到受保护的 `/health/diagnostics`。

Spring Boot 侧：

- `HttpPythonAgentGateway`、`HttpPythonAgentAnalysisGateway` 发送
  `X-Internal-Token` 与 `X-Internal-User-Id`；
- 对应单元测试改为 4 参数构造并断言请求头；
- `SecurityContextIntegrationTest` 新增
  `forgedBodyUserIdCannotOverrideJwtIdentity`：JWT 用户 A 在请求体伪造用户 B 的
  `userId`，响应与数据库 `learning_space.user_id` 都必须是 A。

### 1.3 修复后：真实 HTTP 探针

对运行中的 FastAPI（`127.0.0.1:8010`）执行实际 HTTP 请求：

| 请求 | 实际结果 |
| --- | --- |
| `POST /agent/context/preview`，无 token | 401 |
| 同上，错误 token | 401 |
| 同上，正确 token + `X-Internal-User-Id: 2`，请求体 `userId=3` | 403 |
| 同上，正确 token，身份与 `userId` 一致 | 200 |
| `GET /health` | 200 `{"status":"ok"}` |
| `GET /health/diagnostics`，无 token | 401 |
| 同上，正确 token | 200 |

自动化回归：

- `agent/tests/test_security_phase11.py` + `agent/tests/test_phase11_regressions.py`：
  **31 passed**；
- `HttpPythonAgentGatewayTest`：9 passed；
- `HttpPythonAgentAnalysisGatewayTest`：9 passed；
- `SecurityContextIntegrationTest`：12 passed（真实 MySQL + JWT）。

覆盖点：

- chat / analyze / context preview / documents parse 的无凭据与错误凭据；
- 请求体伪造 `userId` 被 403；
- 正确凭据可达 chat / preview；
- analyze 正确凭据下仍被 Fixture 保护拦截；
- `/health` 与 `/health/diagnostics` 的边界；
- 跨用户 preview：Alice 的上下文不含 Bob 的记录，Bob 的上下文不含 Alice 的记录；
- 身份不匹配时后端 stub 收到 0 个请求。

### 1.4 残余风险

内部 token 是**共享 bearer 凭据**：持有它的一方可以为任意 `userId` 发起请求。
它解决的是「Agent 完全裸奔」，不是「强身份认证」。当前仍建议：

- 生产环境把 Agent 限制在 loopback / 受控内网；
- 不把 `AILS_INTERNAL_TOKEN` 写入日志、镜像层或前端；
- 后续改为短期签名身份 / mTLS / 服务网格身份，而不是长期共享 token。

`/health` 的返回体已收窄，既有监控若依赖旧字段需要改为调用
`/health/diagnostics` 并携带内部凭据。

## 2. B06：Analyze 链路禁止 Fixture 顶替真实检索（P0）

### 2.1 修复前

`build_retriever(..., allow_degradation=False, retriever_mode="fixture")`
直接返回 `FixtureRetriever`，没有抛 `RetrievalError`。Analyze 结果会写长期认知，
线上误配 `fixture` 可能把样例概念写进真实用户状态。

### 2.2 修复内容

`agent/app/retriever/routing.py`：

```python
if mode == MODE_FIXTURE:
    if not allow_degradation:
        raise RetrievalError(
            "分析链路禁止使用 Fixture 检索源：样例概念会被写进真实用户的长期认知。"
        )
    return fixture
```

调用链：

- `AgentRuntime.analyze` → `build_retriever(..., allow_degradation=False)`
  → `RetrievalError` → `/agent/analyze` 返回 503；
- 失败发生在任何 LLM 调用之前，不会产生 AnalyzeResult，也就没有候选可写库；
- Chat 链路 `allow_degradation=True` 时仍允许 Fixture，演示与测试不受影响。

### 2.3 验证

- `test_b06_fixture_mode_is_rejected_when_degradation_disallowed`；
- `test_b06_analyze_fails_before_any_llm_call_when_fixture_bypassed`：
  `CountingLLM.calls == 0`；
- `test_b06_fixture_is_still_allowed_for_chat_when_degradation_allowed`；
- 实际 HTTP：正确内部凭据调用 `/agent/analyze`，fixture 模式返回 503；
- 后端 `TurnAnalysisServiceTest.fallsBackAndMarksRunFailedWhenAgentFails`：
  Agent 失败时 `writeService.apply` 从未被调用，run 标记 FAILED，只走 rules-v1；
- 真实 MySQL 的写入路径由 Phase 2 B1–B4 / C1–C5 覆盖：只有在拿到 AnalyzeResult
  时才写 Evidence / Understanding / Revision。

没有观察到 Fixture 样例概念被写入任何业务表；分析失败时后端只记录
`agent_run` 失败并退回规则版，规则版只基于本轮用户消息产生证据。

## 3. A06 / A07：上下文预算口径

### 3.1 结论

`max_context_tokens` 定义为**动态上下文预算**，不是完整 prompt 预算：

- 计入：区块标题/说明、section 内容、历史消息、实际用户输入；
- 不计入：固定教学契约 `BASE_SYSTEM_INSTRUCTION`；
- `estimated_tokens`：完整 prompt 的真实估算，始终如实报告；
- `context_tokens`：动态上下文，受预算约束；
- `budget_exceeded` / `budget_overrun_reason`：动态内容仍超限时显式报告，
  不伪造「已满足预算」。

### 3.2 代码变化

- `BuiltContext` 新增 `budget_tokens` / `context_tokens` / `budget_exceeded` /
  `budget_overrun_reason`；
- 最终渲染后按标题、说明、实际消息再收紧，直到动态上下文装得下；
- 固定 prompt 本身超过预算时，warning 明确说明「完整 prompt 约 X tokens，
  其中固定系统提示约 Y tokens；预算约束的是动态上下文」；
- A06：被预算丢弃的认知区块不再触发「没有检索到任何历史认知记录」，而是说明
  「检索到了，但超出预算未包含」。

回归：A06、A07 PASS；`test_a06_*`、`test_a07_*` 覆盖两种预算值。

## 4. B03：History Guard 纳入按需 Evidence

- `_recorded_evidences` 合并轨迹证据与 `state.evidences`，按 id 去重；
- 记录条数断言不能超过实际记录数；
- 「答对三道题」必须由 `TEST_RESULT` 支撑，用户解释不算测试；
- 只有 Evidence 时使用 `_evidence_only_fallback`，只复述可核对记录，不否认记录。

回归：B03 PASS；包含混合证据的回归用例。

## 5. D07：auto 回退可观测

`PreparedRun` 与 `/agent/context/preview` 返回：

- `requestedMode`；
- `resolvedSource`；
- `degraded`；
- `fallbackVisible`；
- `fallbackReason`（`no_credentials` / `backend_unavailable`）。

回归：D07 PASS。生产分析链路不会静默使用 Fixture：analyze 使用
`allow_degradation=False`，由 B06 拦截。

## 6. E06：`recent_history_limit=0` 契约

采用契约：**0 = 不注入任何历史；负数非法**。

- `_usable_history` 在 `<= 0` 时返回空列表，避免 `[-0:]` 注入全部；
- 构造函数拒绝负数；
- 回归：E06 PASS。

## 7. B04：两个 Guard 说明的合并仍未定稿

B04 当前 PASS 的含义是：两段说明都存在、各自限定范围、不互相冒充。
**没有**把「当前两段文案」当成最终产品契约。仍待产品确认是否合并为一条统一说明。

## 8. 本阶段改动文件

| 文件 | 说明 |
| --- | --- |
| `agent/app/internal_auth.py` | 新增内部凭据校验与身份绑定 |
| `agent/app/main.py` | 敏感路由接入鉴权，`/health` 拆分 |
| `agent/app/context/builder.py` | 动态预算口径、A06/A07/E06 |
| `agent/app/history_guard.py` | 按需 Evidence、记录计数、fallback |
| `agent/app/retriever/routing.py` | Analyze Fixture 保护（B06） |
| `agent/app/runtime.py` | 回退结构化信息、trace 证据计数 |
| `agent/tests/test_security_phase11.py` | B07 真实 HTTP 回归（新增） |
| `agent/tests/test_phase11_regressions.py` | A06/A07/B03/B06/D07/E06 回归（新增） |
| `backend/.../HttpPythonAgentGateway.java` | 传播内部凭据 |
| `backend/.../HttpPythonAgentAnalysisGateway.java` | 传播内部凭据 |
| `backend/.../HttpPythonAgentGatewayTest.java` | 请求头断言 |
| `backend/.../HttpPythonAgentAnalysisGatewayTest.java` | 请求头断言 |
| `backend/.../SecurityContextIntegrationTest.java` | 伪造 body userId 回归 |
| `scripts/*.sh` | 演示/回归脚本携带内部凭据 |

## 9. 未解决与下一步

1. 共享内部 token 的长期替代方案（mTLS / 短期签名身份）；
2. 生产部署的 Agent 网络暴露面仍需确认；未确认前建议限制为 loopback / 内网；
3. B04 文案合并待产品决策；
4. 真实模型上游空流待排查（Phase 2 期间 9 次调用全部空流）；
5. 性能基线尚未执行，不生成任何估算数字。
