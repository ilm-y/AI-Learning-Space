# 失败与阻塞分析

- 日期：2026-10-27
- 范围：Phase 1、Phase 1.1、Phase 2
- 原则：每条失败/阻塞都给出用例或测试 ID、实际结果、预期、诊断与当前状态；
  修复后的 PASS 不抹掉修复前的失败证据；未执行不写成 PASS

## 1. 结论摘要

| 类别 | 数量 | 状态 |
| --- | ---: | --- |
| Phase 1 初始 FAIL | 7 | 全部修复，回归 PASS |
| Phase 1 统计口径错误 | 1 | 已修正并加自动校验 |
| 真实模型 BLOCKED | 3 条用例 / 9 次调用 | 未解决 |
| Testcontainers BLOCKED | 1 个测试类 / 0 tests | 未解决 |
| 未覆盖的 SSE 场景 | 2 | 未实现 |
| 测试实现过程中的问题 | 5 | 已修复 |
| Phase 2 断言 FAIL | 0 | 本次无 |

Phase 2 的 49 个 JUnit 没有 FAIL；这不等于系统没有缺陷。下面的缺陷来自
Phase 1 审计与修复过程，Phase 2 负责补真实数据库/消息/权限证据。

## 2. Phase 1 的 7 个 FAIL

### F-01 B07：Agent HTTP 端点无鉴权（P0）

- **用例**：B07
- **预期**：无内部凭据或身份不匹配时必须拒绝。
- **实际（修复前）**：`/agent/chat`、`/agent/context/preview` 无凭据返回
  HTTP 200；请求体 `userId` 被直接信任，可读取任意用户上下文。
- **诊断**：路由未声明身份依赖；`userId` 只来自请求体，没有调用方身份绑定。
- **影响**：任何能访问 Agent 端口的人都能读取学习状态、Memory、Evidence，
  并触发模型调用。
- **修复**：新增 `require_internal_user` / `require_matching_user`；
  `X-Internal-Token` + `X-Internal-User-Id`；未配置 token 时 503；
  身份与 body `userId` 不一致 403；`/health` 收窄，诊断移到受保护路由。
- **修复后证据**：真实 HTTP 401/403/200 探针；31 条 Python 测试；
  Gateway 18 条；`SecurityContextIntegrationTest` 12 条。
- **状态**：已修复；残余共享 token 风险见 R-P0-01。

### F-02 B06：Analyze 链路 Fixture 绕过降级保护（P0）

- **用例**：B06
- **预期**：`allow_degradation=False` 时不得使用 Fixture 检索源。
- **实际（修复前）**：`build_retriever(..., allow_degradation=False,
  mode="fixture")` 直接返回 `FixtureRetriever`；样例概念可能被写进真实用户的
  Evidence / Understanding / Revision / Memory。
- **诊断**：`mode == fixture` 分支没有检查 `allow_degradation`。
- **修复**：该分支在 `allow_degradation=False` 时抛 `RetrievalError`，
  且发生在任何 LLM 调用之前；Chat 链路仍允许显式 Fixture 降级。
- **修复后证据**：`CountingLLM.calls == 0`；`/agent/analyze` 返回 503；
  后端 `writeService.apply` 未调用，run FAILED，只走 rules-v1；
  Phase 2 真实库测试证明只有拿到 AnalyzeResult 才写业务行。
- **状态**：已修复。

### F-03 A06：预算丢弃后谎称「没有历史」

- **用例**：A06
- **预期**：历史认知被预算裁剪时，必须说明「检索到了但未包含」，
  不能说「没有检索到任何历史」。
- **实际（修复前）**：认知区块被全部裁掉后，system instruction 出现
  「本次没有检索到任何历史认知记录」。
- **诊断**：Context Builder 只根据最终渲染结果判断有无历史，没有区分
  「检索为空」与「检索到但被预算丢弃」。
- **修复**：记录被丢弃的区块名，渲染时明确说明超出预算；A06 回归覆盖。
- **状态**：已修复。

### F-04 A07：Token 预算口径不一致

- **用例**：A07
- **预期**：预算与实际估算必须是同一口径，或者明确说明两个口径。
- **实际（修复前）**：`max_context_tokens=300`，最终
  `estimatedTokens=2699`；前者不含固定系统提示与标题，后者是完整 prompt，
  但报告没有说明差异。
- **诊断**：预算只约束 section 内容，估算却覆盖完整 prompt；字段命名和
  语义没有区分。
- **修复**：`max_context_tokens` 明确定义为动态上下文预算；新增
  `budget_tokens` / `context_tokens` / `budget_exceeded` /
  `budget_overrun_reason`；`estimated_tokens` 保留为完整 prompt 估算；
  固定 prompt 本身超预算时如实报告，不伪造满足。
- **修复后证据**：`context_tokens <= 300`、`estimated_tokens > 300`，
  warning 明确解释两者关系。
- **状态**：已修复。限制：固定教学契约约 2266 tokens，预算小于该值时
  完整 prompt 必然超限，这是定义结果，不是裁剪失败。

### F-05 B03：History Guard 忽略按需 Evidence

- **用例**：B03
- **预期**：按需召回的 `state.evidences` 必须算作已有记录；记录计数不能超过
  实际记录数。
- **实际（修复前）**：只有 Evidence `1704`、没有 Revision 时，
  「答对了三道题」没有触发 `historyGuard.issues`。
- **诊断**：Guard 只看轨迹 Evidence，没有合并运行时按需 Evidence；
  计数断言也没有来源类型约束。
- **修复**：`_recorded_evidences` 合并轨迹与 `state.evidences`，按 id 去重；
  「题 / 测试」计数只由 `TEST_RESULT` 支撑；只有 Evidence 时使用
  `_evidence_only_fallback`，不套用「没有历史」文案。
- **修复后证据**：B03 PASS；混合证据回归通过。
- **残余限制**：句级/计数级规则不能证明所有语义蕴含，只防明显超出记录的断言。

### F-06 D07：auto 模式静默回退 Fixture

- **用例**：D07
- **预期**：回退必须能被调用方和日志识别。
- **实际（修复前）**：无凭据或后端不可用时静默使用 Fixture，调用方只看到
  正常结果，无法区分真实检索与样例数据。
- **诊断**：runtime 只返回 retriever 实例，没有回退原因。
- **修复**：`PreparedRun` 与 preview 返回 `requestedMode`、
  `resolvedSource`、`degraded`、`fallbackVisible`、`fallbackReason`
  （`no_credentials` / `backend_unavailable`）。
- **修复后证据**：D07 PASS。
- **状态**：已修复。生产 analyze 链路另有 B06 硬拦截，不会静默使用 Fixture。

### F-07 E06：`recent_history_limit=0` 注入全部历史

- **用例**：E06
- **预期**：0 表示不注入历史；负数非法。
- **实际（修复前）**：`history[-0:]` 等价于 `history[0:]`，0 被解释成全部。
- **诊断**：Python 负零切片语义与产品语义不一致；没有参数校验。
- **修复**：`_usable_history` 在 `<= 0` 时返回空列表；构造函数拒绝负数。
- **修复后证据**：E06 PASS；`test_e06_zero_history_limit_injects_no_history`、
  `test_e06_negative_history_limit_is_invalid`。
- **状态**：已修复；该契约由本阶段确定，产品若另有定义需重新评审。

### F-08 Phase 1 统计口径错误

- **位置**：Phase 1 汇总报告
- **实际（修复前）**：报告写「实际执行 37」，但 PASS 30 + FAIL 7 +
  BLOCKED 1 = 38；SKIPPED 2 被混入分母。
- **诊断**：没有区分 `executed`（真正发起执行）与 `completed`（得出明确结论）。
- **修复**：定义 `executed = PASS + FAIL + BLOCKED`、
  `completed = PASS + FAIL`、`executed + SKIPPED + NOT_IMPLEMENTED = 总数`；
  summary 自动校验，不一致直接报错。
- **修复后**：全量 35 PASS / 0 FAIL / 3 BLOCKED / 2 SKIPPED，
  executed=38，completed=35。
- **状态**：已修复。

## 3. BLOCKED

### B-01 真实模型上游空流（3 条用例，9 次调用）

- **用例**：B08、D08、E07
- **预期**：真实 provider 返回可用文本，再根据硬断言与 rubric 判断。
- **实际**：每条重试 3 次，共 9 次；全部 `callEmitted=true`、
  `httpStatus=200`、`outputChars=0`、`rawOutput=null`、`tokenUsage=null`。
- **诊断**：请求确实发出并到达 provider，但流里没有 content。尚未定位是
  限流、内容过滤、`max_tokens`、流式解析还是 provider 状态。
- **为什么不记 FAIL**：没有输出时无法判断 Agent 的教学/证据行为；
  这是上游可用性问题，不是可归因的 Agent 回答错误。
- **当前状态**：BLOCKED。真实模型质量没有证据，不能写进结论。
- **下一步**：用最小直接调用复现，检查响应头/错误体/流式 chunk；
  逐步排除 provider、模型名、参数与适配器解析。

### B-02 Testcontainers 无法连接 Docker

- **测试**：`AnalysisMqIntegrationTest`
- **预期**：启动独立 RabbitMQ 容器，验证隔离环境下的发布/消费。
- **实际**：`Tests run: 0`，测试类被 abort；Testcontainers 无法找到可用的
  Docker 环境。Docker CLI 本身可用，但 Testcontainers 的连接失败。
- **为什么不记 PASS**：0 个测试执行，没有任何断言运行。
- **替代覆盖**：`Phase2RabbitMqBrokerTest` 直连本机 RabbitMQ，覆盖发布确认、
  重复投递、未 ACK 重投、TTL 重试、死信；它不是容器隔离的等价物。
- **当前状态**：BLOCKED。
- **下一步**：修复 Docker 端点/权限，或改用 CI 提供的 broker；不要把
  `Tests run: 0` 当成通过。

## 4. NOT_COVERED

### N-01 客户端主动断开 SSE（落库前）

- **场景**：客户端在 `message.start` / 第一个 delta 后关闭连接，助手消息尚未落库。
- **当前证据**：Phase 2.1 已覆盖“助手消息落库并收到 `message.end` 后断开”
  （`ChatSseIntegrationTest.disconnectAfterAssistantPersistedStillAnalyzesAndReconnectIsNotDuplicated`），
  但还没有“落库前断开”的真实 HTTP 用例。
- **风险**：可能继续生成、重复落库或错误地触发分析。
- **状态**：落库后断开 PASS；落库前断开仍 **NOT_COVERED**，不计 PASS。

### N-02 SSE 断开但消息已落库时分析是否继续

- **场景**：助手消息已持久化，客户端断开，后台分析是否按契约执行。
- **当前证据**：`ChatSseIntegrationTest` 真实 HTTP + 真实 MySQL：关闭响应体后
  分析仍进入，Evidence=1；重连两次返回相同消息，Revision=1。
- **状态**：**PASS**（Phase 2.1）。

### N-03 真实模型错误/空流后的后端终态

- **场景**：Agent 返回空流或错误。
- **当前证据**：Phase 2.1 `ChatSseIntegrationTest` 用可控 AgentGateway
  覆盖空流与 `quota exceeded`：`message.start → message.error(502)`，
  run=FAILED，无助手消息/Evidence。真实模型语义质量仍 BLOCKED。
- **状态**：后端终态契约 **PASS（替身网关）**；真实模型质量 **BLOCKED**。
- **风险**：用户可能收到成功终态但没有任何内容，或消息未落库却被当成成功。

### N-04 非法状态迁移

- **场景**：例如 DEMONSTRATED 直接回退到 EXPOSED。
- **当前证据**：合法生命周期的单元测试存在；Phase 2 没有补真实库的反向迁移
  拒绝测试。
- **状态**：NOT_COVERED。不能从「合法路径通过」推出「非法路径被拒绝」。

### N-05 性能基线

- **状态**：NOT_RUN。
- **说明**：没有生成任何吞吐、延迟、P95/P99 数字；报告不包含估算性能。

## 5. 测试实现过程中的问题（均已修复）

### I-01 演示脚本 preview 401

- **实际**：接入鉴权后，`phase6-demo.sh` 的 preview 只带
  `X-Internal-Token`，返回 401「缺少 X-Internal-User-Id」。
- **修复**：补 `X-Internal-User-Id: $uid`，重跑 PASS=23 / FAIL=0。
- **状态**：已修复。

### I-02 脚本行级补丁误删/错位

- **实际**：给脚本补内部请求头时，`phase51-e2e.sh`、`phase52-e2e.sh`、
  `regress-closed-loop.sh` 出现请求体行丢失、重复尾部或提前退出。
- **诊断**：行级替换没有校验上下文，属于本阶段的操作失误。
- **修复**：`regress-closed-loop.sh` 从 HEAD 恢复后重新应用预期修改；
  另外两个脚本补回被误删的 `-d` 请求体与 `-X POST`；四个脚本均通过
  `bash -n`。
- **状态**：已修复；未提交 Git，改动保留在工作区。

### I-03 Phase 2 清表外键失败

- **实际**：`Phase2RealStateIntegrationTest` 的 `DELETE FROM learning_space`
  失败，因为共享 `ails_phase2_it` 的另一个测试留下了 `conversation` 行。
- **诊断**：清理顺序没有先删引用表。
- **修复**：按 `evidence_source` → `understanding_revision` →
  `learning_evidence` → `learning_understanding` → `learning_state` →
  `memory_source` → `memory` → `agent_run` → `message` → `conversation` →
  `concept` → `learning_space` → `user` 顺序清理。
- **状态**：已修复，6/6 通过。

### I-04 环境门控导致测试被跳过

- **实际**：组合运行初期，`SecurityContextIntegrationTest` 与文档测试因
  `AILS_SECURITY_IT`、`AILS_DOCUMENT_IT`、`AILS_DOCUMENT_INDEX_IT`、
  `AILS_DOCUMENT_E2E_IT` 未设置而被跳过。
- **诊断**：用了 `@EnabledIfEnvironmentVariable`，默认不执行。
- **修复**：显式设置所有门控为 `true` 后重跑；报告把「未设置门控」与
  「执行失败」分开。
- **状态**：已修复；最终报告中这些测试均为实际执行。

### I-05 测试代码中的默认密码字面量

- **实际**：5 个测试文件曾以 `"root"` / `"guest"` 作为数据库或 broker 密码默认值。
- **诊断**：违反「不把密码写入代码」的约束，即使只是本地默认值。
- **修复**：数据库密码默认改为空字符串，必须由 `AILS_IT_MYSQL_PASSWORD`
  等环境变量提供；RabbitMQ 用户名仍默认 `guest`（非密码）。
- **状态**：已修复；报告中不打印任何凭证。

### I-06 E2E Qdrant collection 名写错导致 404

- **测试**：`DocumentIndexE2EIntegrationTest`
- **实际**：完整套件第一次重跑设置
  `AILS_E2E_QDRANT_COLLECTION=ails_e2e_phase2_hash-v1_8`，测试在查询点数时
  返回 HTTP 404；结果为 208 tests / 1 failure。
- **预期**：查询本机 Agent 实际创建并写入的 collection。
- **诊断**：Agent 当前使用 `ails_documents_hash-v1_8`，测试命令传入了不存在的
  collection；这是测试调用参数错误，不是文档索引或 Qdrant 写入失败。
- **修复**：先把 collection 改成 Agent 实际使用的名字，确认测试本身通过；
  随后发现这会删除共享 collection（I-07），最终改为独立 prefix
  `ails_e2e_phase2` + `ails_e2e_phase2_hash-v1_8`，完整套件
  208 tests / 0 failures / 0 errors / 1 skipped。
- **保留原因**：第一次失败的 404 是真实执行结果，不删除、不改成 PASS；
  它说明 E2E 的环境参数必须与 Agent 配置一致。

### I-07 E2E 的 `@AfterAll` 会删除传入的 Qdrant collection

- **测试**：`DocumentIndexE2EIntegrationTest`
- **实际**：把 `AILS_E2E_QDRANT_COLLECTION` 设为共享的
  `ails_documents_hash-v1_8` 时，测试通过，但 `@AfterAll` 删除了该 collection；
  这会破坏演示/其他环境正在使用的检索数据。
- **诊断**：测试把外部传入的 collection 当成自己的临时资源，但没有强制隔离
  命名；使用共享名字时就会产生副作用。
- **修复方式**：测试用 Agent 使用独立 prefix
  `AILS_QDRANT_COLLECTION_PREFIX=ails_e2e_phase2`，E2E 使用匹配的
  `ails_e2e_phase2_hash-v1_8`；单独重跑 1/1 PASS，collection 只在该测试内
  创建和删除，共享 collection 不受影响。
- **残余建议**：测试代码应拒绝在非专用前缀上运行，或在测试内创建带随机后缀
  的 collection；目前依赖运行时配置，仍有误删风险。
- **状态**：本次已用隔离 prefix 规避；测试代码层面的强制隔离仍待补。

## 6. 待产品决策

### D-01 B04 两个 Guard 的兜底说明是否合并

- **当前行为**：History Guard 与 Evidence Guard 的说明同时存在，各自限定范围。
- **已证明**：两段说明都存在、不互相冒充；最终回答不会同时出现
  「没有历史记录」与「历史证据证明某事」这种直接矛盾。
- **未证明**：这是否是最终产品文案；是否应合并为一条统一说明。
- **状态**：待决策。B04 的 PASS 不应被解读为「文案已定稿」。

## 7. 失败排查方法

1. 先看用例/测试 ID 与硬断言，不看自然语言总结；
2. 区分 `FAIL`（断言不满足）、`BLOCKED`（环境/上游不可用）、
   `NOT_COVERED`（没写测试）、`NOT_IMPLEMENTED`（产品不支持）；
3. 对 FAIL 保留修复前日志/响应/DB 查询结果，再给修复后证据；
4. 对 BLOCKED 记录调用次数、HTTP 状态、输出字符数，不把「请求发出」
   当成「功能通过」；
5. 对权限/状态/事务/幂等结论，必须回到真实 DB 行或真实 HTTP 响应；
6. 任何数字都来自报告或查询结果，不手写估算。

---

# Phase 4.1 失败修复记录（2026-10-28）

本节只更新 Phase 4 的三个已确认缺陷，不改写前文 Phase 1/2 的证据。
Phase 4 的原始复现与 preview 见
[phase4-failure-analysis.md](phase4-failure-analysis.md)。

## 8.1 F1：检索成功但知识全被预算丢弃，Prompt 没有说明

- **修复前**：检索层 `SUCCESS`、2 个知识 chunk 全被预算丢弃，最终 Prompt
  只呈现「没有知识片段」，模型无法区分「没检索到」与「检索到但没放下」。
- **修复**：Context Builder 在预算裁剪后计算最终状态
  `SUCCESS` / `SUCCESS_PARTIAL` / `SUCCESS_BUT_DROPPED`；状态区块在裁剪后
  补入，不可再被预算丢弃；Guard 使用最终状态，不把预算丢弃说成「没有资料」。
- **验证**：
  - 确定性测试覆盖完整进入、全部丢弃、部分丢弃、无匹配，并断言渲染后的
    Prompt 与 `AgentRuntime.generate()` 的实际输出；
  - 受控真实模型样本（固定 chunk、12 条合成历史、4 条合成记忆、默认
    3000 token）得到 `SUCCESS_BUT_DROPPED`，注入 0、丢弃 18/19，
    回答明确说「检索确实命中了材料，但片段因上下文预算没能进入本轮对话」；
  - 定向 71 passed，全量 Agent 351 passed / 5 skipped。
- **状态**：结构缺陷已修复；受控样本通过。**不等于在线 RAG 或语义质量
  已验证**。

## 8.2 F2：进程中断后文档永久 PROCESSING

- **修复前**：文档 3 停在 `PROCESSING`、`index_error=NULL`、0 chunk/point，
  没有超时或启动回收。
- **修复**：复用 `updated_at` 作为索引启动时间近似值，增加可配置
  `stale-processing-timeout-ms`（默认 15 分钟）；`@Scheduled` 扫描超时行，
  用带 `status='PROCESSING' AND updated_at < cutoff` 的条件更新转 `FAILED`
  并写可诊断错误；不自动重试。
- **验证**：真实 MySQL 8.0.37 容器 + 独立库 `ails_document_index_it` +
  假 Agent HTTP，`DocumentIndexServiceIntegrationTest` 15/15：
  正常任务不回收、超时回收、失败后不进 ready-ids、重启入口、重复扫描幂等、
  超时边界、晚到完成不能覆盖 FAILED。
- **验证中发现的测试缺陷**：边界用例用 MySQL `NOW()` 造旧时间，容器会话
  UTC 与 JVM Asia/Shanghai 不一致，导致 30 秒前被算成约 8 小时前；
  已改为用 JVM 时钟写 `updated_at`，业务代码与预期未改。
- **状态**：本地隔离环境的恢复语义已修复并验证；未验证真实 15 分钟挂钟、
  多进程并发和主从延迟。

## 8.3 F3：多来源事实合并成一句、只标一个来源

- **修复前**：Phase 4 的 16 条合法引用中 2 条 `numericConflict=true`，
  同一句包含两个版本的数字，只标了一个来源。
- **修复**：Prompt 要求不同来源/版本/时间的事实拆成独立句子并分别标注；
  `Citation` 暴露 `documentId`，`CitationReport` 分开报告 `malformed`、
  `invalid`、`unsupported`、`numericConflict`；明确 `numericConflict`
  是诊断信号，不是自动判错；修正「一个」这类不定指被误算成数字 1 的
  启发式误报。
- **验证**：
  - 8 条确定性来源归属回归，覆盖单来源、两来源拆句、合并版本、无词面支持、
    不存在索引、事实正确但指向错误来源；
  - 修复后真实模型 2 个问题 × 3 次：6/6 把 12 秒与 17 秒拆句并分别引用，
    `numericConflict=0`、`invalid=0`；
  - 早期探测运行仍出现过 1 条合并句，说明 Prompt 只能降低概率，不能保证；
  - 1 次 `legacy_only` 出现 2 条 `unsupported`，属于词面启发式线索，需人工判断。
- **状态**：代码可判定部分通过；事实级语义仍 `PENDING`，人工评分 0/8。
  不能写成「引用语义正确率 100%」。

## 8.4 本轮统计口径

| 类别 | 本轮结果 |
| --- | --- |
| Agent 确定性测试 | 71 定向 / 351 全量，5 skipped |
| 真实 MySQL 集成 | 15/15（其中回收 6 条），独立库 + 假 Agent |
| 真实 Qdrant/Embedding | 本轮未新增；F1/F3 复用 Phase 4 固定 chunk |
| 真实 LLM | F1 1 次 + F3 6 次 + 1 次早期探测 |
| 人工语义评分 | 新增 8 条，完成 0；历史 9 条仍 0/9 |

## 8.5 仍然不能声称

- 「RAG 语义质量通过」：人工评分 0/17；
- 「Recall/MRR 是生产指标」：仍是 N=5、单 chunk、无负样本；
- 「索引恢复已在生产验证」：只在隔离 MySQL 和假 Agent 下验证；
- 「多来源引用已彻底修复」：真实模型仍有随机合并的可能；
- 「预算压力样本代表在线 RAG」：它是固定 chunk 的受控上下文。
