# Phase 1 执行结果（Phase 1.1 修复后）

- 日期：2026-10-27
- 环境：Windows 11 / Python 3.10.11
- 用例：40 条（A10 / B8 / C6 / D8 / E8）
- 全量报告：`evaluation/phase1/reports/full.json` / `full.md`
- 确定性报告：`evaluation/phase1/reports/deterministic.json` / `.md`
- 真实模型子集：`evaluation/phase1/reports/real-llm.json` / `.md`
- 证据目录：`evaluation/phase1/artifacts/<runId>/`

> 结论只来自实际运行。`BLOCKED` / `SKIPPED` 不计 PASS；Mock、Fixture、
> 真实 LLM、Fallback 分别统计。

## 1. 总览

### 1.1 全量运行（含真实模型）

- 运行 ID：`20261027T123127Z`
- 时间：2026-10-27T20:31:27+08:00
- 耗时：46.3s
- 命令：`python -m evaluation.phase1`

| 状态 | 数量 |
| --- | ---: |
| PASS | 35 |
| FAIL | 0 |
| BLOCKED | 3 |
| SKIPPED | 2 |
| NOT_IMPLEMENTED | 0 |
| 合计 | 40 |

统计口径（已写入 runner，并由 `summary.validation` 自动校验）：

- `executed = PASS + FAIL + BLOCKED = 38`，表示真正发起过执行；
- `completed = PASS + FAIL = 35`，表示得出了明确行为结论；
- `executed + SKIPPED + NOT_IMPLEMENTED = 40`；
- `executed = completed + BLOCKED`。

因此正确表述是：**40 条设计中实际执行 38 条，得出明确结论 35 条，
3 条被上游阻塞，2 条设计登记未执行。** 不再使用含糊的「实际执行 37」。

### 1.2 确定性套件（不含真实模型）

- 运行 ID：`20261027T125600Z`
- 命令：`python -m evaluation.phase1 --no-real`
- 耗时：约 1s

| 状态 | 数量 |
| --- | ---: |
| PASS | 35 |
| FAIL | 0 |
| BLOCKED | 0 |
| SKIPPED | 5（A05、B08、C06、D08、E07） |
| 合计 | 40 |

### 1.3 真实模型子集

- 运行 ID：`20261027T125656Z`
- 命令：`python -m evaluation.phase1 --cases B08,D08,E07`

| 用例 | 状态 | 调用次数 | HTTP | 输出字符 |
| --- | --- | ---: | --- | --- |
| B08 | BLOCKED | 3 | 200 / 200 / 200 | 0 / 0 / 0 |
| D08 | BLOCKED | 3 | 200 / 200 / 200 | 0 / 0 / 0 |
| E07 | BLOCKED | 3 | 200 / 200 / 200 | 0 / 0 / 0 |

真实模型请求确实发出（`RecordingLLMAdapter` 记录了 provider、model、requestId、
prompt 哈希与 HTTP 200），但流里没有 content。上游返回空流属于可用性问题，
不能据此评价 Agent 回答质量，因此三条都记 BLOCKED，而不是 PASS 或 FAIL。

## 2. 分类结果（全量）

| 类别 | 名称 | 总数 | PASS | FAIL | BLOCKED | SKIPPED |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| A | 跨会话学习与状态演化 | 10 | 9 | 0 | 0 | 1 |
| B | 证据约束与回答可靠性 | 8 | 7 | 0 | 1 | 0 |
| C | Memory 与显式批注 | 6 | 5 | 0 | 0 | 1 |
| D | RAG 检索与知识边界 | 8 | 7 | 0 | 1 | 0 |
| E | 教学任务与对话质量 | 8 | 7 | 0 | 1 | 0 |

执行类别：

| 类别 | 数量 |
| --- | ---: |
| MOCK（含固定输出模型） | 30 |
| FIXTURE（检索 / Context / 产品工厂，无模型调用） | 4 |
| REAL_LLM | 3 |
| FALLBACK | 1 |
| NOT_EXECUTED | 2 |

35 条 PASS 的构成：30 条 MOCK + 4 条 FIXTURE + 1 条 FALLBACK；3 条
REAL_LLM 全部 BLOCKED；2 条设计登记用例 SKIPPED。

## 3. 逐用例结果

| 用例 | 严重度 | 执行类别 | 状态 | 标题 |
| --- | --- | --- | --- | --- |
| A01 | P0 | MOCK | PASS | 历史误区被识别为过去，而不是当前状态 |
| A02 | P0 | MOCK | PASS | 当前 Understanding 覆盖旧的冲突状态 |
| A03 | P1 | MOCK | PASS | 旧 Memory 与当前 Understanding 冲突时的提示 |
| A04 | P1 | MOCK | PASS | 复盘轨迹包含版本、时间、来源与完整证据 |
| A05 | P0 | NOT_EXECUTED | SKIPPED | 跨会话持久化：第二天重新进入仍能看到学习状态 |
| A06 | P1 | FIXTURE | PASS | 认知区块被预算丢弃后不得谎称「没有历史」 |
| A07 | P1 | FIXTURE | PASS | 动态上下文预算必须生效，完整 prompt 估算必须如实报告 |
| A08 | P0 | MOCK | PASS | 空历史时不得编造过去 |
| A09 | P0 | MOCK | PASS | 跨用户隔离：Bob 的请求不能读到 Alice 的认知 |
| A10 | P1 | MOCK | PASS | 跨空间隔离：数据库空间不串入操作系统记忆 |
| B01 | P1 | MOCK | PASS | 被问「凭什么」时按需召回证据 |
| B02 | P0 | MOCK | PASS | 复盘回答不得补写心理过程，违规时重生成或确定性回退 |
| B03 | P1 | MOCK | PASS | History Guard 应把按需召回的 evidence 也算作记录 |
| B04 | P2 | MOCK | PASS | History 与 Evidence 两个兜底说明同时出现时的口径 |
| B05 | P0 | MOCK | PASS | 没有知识命中时不得说成「资料中不存在」 |
| B06 | P0 | FIXTURE | PASS | Analyze 链路不得在 fixture 模式下绕过降级保护 |
| B07 | P0 | MOCK | PASS | Agent HTTP 端点必须要求内部凭据并绑定 userId |
| B08 | P1 | REAL_LLM | BLOCKED | 真实模型证据问答冒烟 |
| C01 | P1 | MOCK | PASS | Memory 按 GLOBAL/SPACE/CONCEPT 作用域过滤 |
| C02 | P1 | MOCK | PASS | 冲突 Memory 被标注为历史记录 |
| C03 | P0 | MOCK | PASS | 显式 HIGHLIGHT 批注必须在复盘回答中被追溯 |
| C04 | P1 | MOCK | PASS | Analyze 产生 GLOBAL PREFERENCE 候选 |
| C05 | P1 | MOCK | PASS | 偏好 Memory 不能覆盖当前掌握状态 |
| C06 | P1 | NOT_EXECUTED | SKIPPED | Memory 写入后下一次会话可读（E2E） |
| D01 | P0 | MOCK | PASS | READY 文档命中后可产生合法引用 |
| D02 | P0 | MOCK | PASS | 没有可用文档时追加固定说明 |
| D03 | P1 | MOCK | PASS | NO_MATCH 与 NO_VALID_DOCUMENTS 的措辞必须区分 |
| D04 | P1 | MOCK | PASS | THRESHOLD_FILTERED 说明候选低于阈值 |
| D05 | P0 | MOCK | PASS | DEGRADED 说明检索不可用，不冒充「没有资料」 |
| D06 | P1 | MOCK | PASS | FAILED 文档不进入知识片段 |
| D07 | P1 | FALLBACK | PASS | auto 模式无凭据回退 Fixture 必须可观测 |
| D08 | P1 | REAL_LLM | BLOCKED | 真实 RAG 引用冒烟 |
| E01 | P1 | MOCK | PASS | PARTIAL 状态不得被说成已掌握 |
| E02 | P1 | MOCK | PASS | 纠正误区时指出分叉点，而不是只说「你错了」 |
| E03 | P1 | MOCK | PASS | Session 学习目标进入上下文并对齐回答 |
| E04 | P0 | MOCK | PASS | 渲染给模型的上下文不含内部 id |
| E05 | P1 | MOCK | PASS | 空历史 + 新概念按新知识教学 |
| E06 | P2 | FIXTURE | PASS | `recent_history_limit=0` 的契约 |
| E07 | P1 | REAL_LLM | BLOCKED | 真实模型教学冒烟 |
| E08 | P0 | MOCK | PASS | 没有轨迹时不得断言过去没有错误 |

## 4. Phase 1 的 7 个 FAIL 修复结果

### 4.1 A06：预算丢弃不再谎称「没有历史」

修复前：认知区块被预算丢弃后，system instruction 出现
「本次没有检索到任何历史认知记录」。

修复后：

- 区分「检索为空」与「检索到但被预算丢弃」；
- 后者明确写出「检索到了历史认知记录，但以下区块超出上下文预算、未包含」，
  并列出区块名；
- 回归：`agent/tests/test_phase11_regressions.py::test_a06_*`，用例 A06 PASS。

### 4.2 A07：预算口径明确为「动态上下文」

修复前：`max_context_tokens=300`，最终 `estimatedTokens=2699`，两个数字不是
同一个口径。

修复后契约：

- `max_context_tokens` 约束**动态上下文**：区块标题/说明、section 内容、
  历史消息、实际用户输入；
- 固定教学契约 `BASE_SYSTEM_INSTRUCTION` 不计入动态预算，但计入完整 prompt；
- `estimated_tokens` 仍是完整 prompt 的真实估算；
- 新增 `budget_tokens`、`context_tokens`、`budget_exceeded`、
  `budget_overrun_reason`；
- 若固定 prompt 本身已超过预算，报告 `budget_exceeded` 与超限原因，不伪造数字。

回归：A07 PASS；`test_a07_budget_limits_dynamic_context_and_reports_full_prompt_separately`
断言 `context_tokens <= 300`、`estimated_tokens > 300` 且 warning 明确说明两者关系。

### 4.3 B03：History Guard 纳入按需 Evidence

修复前：只有 Evidence `1704`、没有 Revision 时，「答对了三道题」没有触发
`historyGuard.issues`。

修复后：

- `_recorded_evidences` 合并轨迹 Evidence 与 `state.evidences`，按 id 去重；
- 记录条数断言（「三道题」「3 条记录」）不能超过实际记录数；
- 「题 / 测试」类计数只由 `TEST_RESULT` 证据支撑，用户解释不能冒充测试；
- 只有 Evidence 时使用 `_evidence_only_fallback`，不再套用「没有历史」文案。

回归：B03 PASS；混合证据（测试 + 用户解释）的回归测试也在
`test_b03_explanation_evidence_does_not_support_question_count` 中。

### 4.4 B06：Analyze 链路禁止 Fixture 顶替真实检索（P0）

修复前：`build_retriever(..., allow_degradation=False, mode="fixture")`
直接返回 `FixtureRetriever`。

修复后：

- `mode == fixture` 分支先检查 `allow_degradation`；为 `False` 时抛
  `RetrievalError`；
- 失败发生在任何 LLM 调用之前（`CountingLLM.calls == 0`）；
- Chat 链路在 `allow_degradation=True` 时仍可使用 Fixture，不影响演示/测试；
- analyze 的真实调用链不会把样例数据写入 Evidence / Understanding /
  Revision / Memory：检索阶段就失败，写库阶段不会开始；
- 回归：B06 PASS，`test_b06_fixture_mode_is_rejected_when_degradation_disallowed`、
  `test_b06_analyze_fails_before_any_llm_call_when_fixture_bypassed`。

### 4.5 B07：Agent HTTP 端点鉴权（P0）

修复前：`/agent/chat`、`/agent/context/preview` 无凭据返回 200，请求体
`userId` 被直接信任。

修复后：

- 敏感路由统一要求 `X-Internal-Token` + `X-Internal-User-Id`；
- token 未配置时 fail closed（503），不会裸奔；
- token 使用 `secrets.compare_digest` 常量时间比较；
- 请求体 `userId` 必须与内部身份一致，否则 403，且请求到不了检索层；
- `/health` 只返回 `{"status":"ok"}`；详细配置移到受保护的
  `/health/diagnostics`；
- Spring Boot → Agent 的 gateway 同步发送两个内部请求头；
- 文档解析/索引接口仍保持内部鉴权。

详见 `docs/testing/phase1.1-security-results.md`。回归：B07 PASS；实时 HTTP
探针与 31 条 Agent 安全/回归测试通过。

### 4.6 D07：auto 回退结构化可观测

修复后 `PreparedRun` / preview 返回：

- `requestedMode`；
- `resolvedSource`；
- `degraded`；
- `fallbackVisible`；
- `fallbackReason`（`no_credentials` / `backend_unavailable`）。

回归：D07 PASS；`test_d07_auto_without_credentials_exposes_fallback_metadata`。

### 4.7 E06：`recent_history_limit=0` 契约

采用契约：**0 = 不注入任何历史，负数非法**。

修复前 `[-0:]` 等价于 `[0:]`，把 0 解释成全部。

修复后：

- `_usable_history` 在 `<= 0` 时直接返回空列表；
- 构造函数拒绝负数；
- 回归：E06 PASS，`test_e06_zero_history_limit_injects_no_history`、
  `test_e06_negative_history_limit_is_invalid`。

## 5. 真实模型证据

配置（不打印密钥）：

- provider：`openai`
- adapter：`openai-compatible:deepseek-flash`
- model：`deepseek-flash`
- 记录层：`RecordingLLMAdapter`
- 调用路径：真实 `AgentRuntime` → `OpenAICompatibleAdapter.stream_chat`

本次真实模型子集的实际结果：

- B08 / D08 / E07 各调用 3 次；
- 9 次请求全部 `callEmitted=true`、`httpStatus=200`；
- 9 次 `outputChars=0`、`rawOutput=null`、`tokenUsage=null`；
- 因此 3 条都记 BLOCKED。

可以确认：调用确实发出了，provider/model 是真实配置；不能确认：真实模型的
回答质量、引用质量、教学效果。上游空流需要单独排查（限流、内容过滤、
`max_tokens`、流式实现等）。

## 6. 隔离与清理

- Fixture：2 用户 / 3 空间 / 5 文档（其中 `broken.md` 为 FAILED）；
- 所有请求只使用 9001 / 9002 / 9101 / 9102 / 9104 测试号段；
- A09 断言 Bob 的上下文不含 Alice 的 TLB 记录，实际通过；
- A10 断言数据库空间不串入操作系统记忆，实际通过；
- `isolation.violationCount = 0`；
- 40 条用例的 `cleanupFailures` 均为空；
- 没有写数据库，没有读取真实用户数据。

## 7. 不能从本阶段得出的结论

1. 不能得出「后端权限过滤正确」：Phase 1 的隔离由测试替身完成，Phase 2 才用
   真实 MySQL/JWT 补证；
2. 不能得出「跨会话持久化正常」：A05 在 Phase 1 未执行，虽然后端已有 E1 /
   Phase 6 演示覆盖，但那是后端链路，不是 Agent 评测；
3. 不能得出「Memory 写入后可读」：C06 在 Phase 1 未执行，Phase 2 的 B5/E1
   只覆盖服务层持久化；
4. 不能得出「真实模型整体质量」：3 条冒烟全部因上游空流 BLOCKED；
5. 不能因为 35 条 PASS 就说系统整体 87.5% 通过：PASS 的分母是 40 条设计用例，
   其中 2 条未执行、3 条被上游阻塞，且大量用例使用 Mock / Fixture。

## 8. 产物

| 文件 | 说明 |
| --- | --- |
| `docs/testing/agent-evaluation-cases.jsonl` | 40 条可执行用例 |
| `docs/testing/agent-evaluation-plan.md` | 评测设计 |
| `docs/testing/phase1-results.md` | 本文件 |
| `docs/testing/open-risks.md` | 风险与待决策 |
| `docs/testing/fixtures/phase1_two_users_two_spaces.json` | 主 fixture |
| `docs/testing/fixtures/phase1_auto_space.json` | auto 回退 fixture |
| `evaluation/phase1/` | runner、fixture、断言、记录器 |
| `evaluation/phase1/reports/full.*` | 全量报告（35 PASS / 0 FAIL / 3 BLOCKED / 2 SKIPPED） |
| `evaluation/phase1/reports/deterministic.*` | 无真实模型报告（35 PASS / 0 FAIL / 5 SKIPPED） |
| `evaluation/phase1/reports/real-llm.*` | 真实模型子集报告（3 BLOCKED） |
