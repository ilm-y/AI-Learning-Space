# Agent 评测方案（Phase 1 / Phase 1.1）

- 版本：Phase 1.1
- 适用代码：当前 `main` 工作区（含未提交改动）
- 状态：40 条用例已全部可执行；7 个缺陷已修复并保留回归；
  真实模型 3 条仍因上游空流 BLOCKED
- 用例文件：[`agent-evaluation-cases.jsonl`](agent-evaluation-cases.jsonl)
- 执行入口：`python -m evaluation.phase1`
- 结果报告：`evaluation/phase1/reports/`

## 1. 目标与边界

Phase 1 要回答的不是「回答听起来好不好」，而是：

1. Agent 是否真的沿既定链路工作：Message → Context Builder → LLM → Evidence →
   Understanding → Revision → Memory；
2. 历史状态是否被正确区分：过去的误区不能当成当前状态，旧 Memory 不能覆盖
   当前 Understanding；
3. 证据、知识边界与跨用户/跨空间隔离是否由**代码断言**验证；
4. 真实模型、Mock、Fixture、Fallback 四条路径是否分别可识别、可复核。

不在 Phase 1 范围内：

- 不修改业务逻辑、数据库 Schema、鉴权策略或关键依赖；
- 不把未执行的后端 E2E 记为 PASS；
- 不用 LLM Judge 替代状态、权限、路径断言；
- 不为了凑满 40 条复制用例。

## 2. 40 条用例的结构

固定配额，runner 启动时校验，缺一条或重复一条都会直接拒绝运行。

| 类别 | 主题 | 数量 |
| --- | --- | ---: |
| A | 跨会话学习与状态演化 | 10 |
| B | 证据约束与回答可靠性 | 8 |
| C | Memory 与显式批注 | 6 |
| D | RAG 检索与知识边界 | 8 |
| E | 教学任务与对话质量 | 8 |

用例是数据，不是 Markdown 说明。每行一条 JSON，包含：

- `id` / `category` / `severity` / `title`
- `executionMode`：`fixture_mock`、`fixture_scripted`、`fixture_context`、
  `real_llm`、`designed_only`
- `probe`：`chat`、`analyze`、`context`、`auth_probe`、`auto_fallback`、
  `retriever_guard`
- `profile`：fixture 的受控变体
- `request`：`userMessage` / `userId` / `learningSpaceId` / `conversationId`，
  可选 `history` / `session` / `messageId` / `assistantMessage`
- `hardAssertions`：决定 PASS/FAIL 的可核对事实
- `semanticRubric`：固定 0/1/2 评分标准与正负例信号，仅辅助阅读
- `expectedBehavior` / `forbiddenBehavior` / `dependencies` / `cleanup`
- `targetsDefect` / `contractStatus` / `notes`

### 2.1 硬断言

硬断言由 `evaluation/phase1/scoring.py` 逐条执行，支持：

`eq`、`ne`、`contains`、`not_contains`、`all`、`any`、`subset`、`length`、
`length_gte`、`length_lte`、`gte`、`lte`、`truthy`、`falsy`、`matches`、
`startswith`、`endswith`。

每条断言单独执行，不短路，也不会因为同一个 path 出现两次而互相覆盖。
断言对象是**本轮实际发生的事**：

- `run.*`：本次请求与概念识别；
- `retrieval.*`：本轮实际检索到的 Understanding / Memory / Knowledge /
  Evidence / Revision，以及 Retriever 调用与越界尝试；
- `context.*`：实际进入模型的消息、区块、预算、裁剪与 warning；
  其中 `budget_tokens` 是动态上下文预算，`context_tokens` 是动态上下文实际量，
  `estimated_tokens` 是完整 prompt 的真实估算；三者不是同一口径，不能互相冒充；
- `answer.*`：用户可见文本、History Guard / Evidence Guard 事件、引用报告；
- `llm.*`：调用是否发出、HTTP 状态、延迟、字符数、原始输出摘要；
- `isolation.*`：越界检索计数。

### 2.2 语义 rubric

每条用例至少一个评分项，固定三档：

- `2`：满足正向标准；
- `1`：部分满足；
- `0`：命中禁止表述或缺失关键标准。

`signals2` 是「每组至少命中一个词」的组列表，`signals1` 是弱信号，
`forbidden` 任一出现直接 0 分。rubric 输出理由、命中和缺失信号，
**不改变 PASS/FAIL**。

Phase 1 不使用 LLM Judge。原因是：

- 当前 40 条里真正需要语义判断的比例不高；
- LLM Judge 会引入第二套模型可用性与非确定性；
- 权限、状态、路径类结论必须继续由硬断言承担。

这是一个明确的开放项，见 [`open-risks.md`](open-risks.md)。

## 3. 隔离 Fixture

文件：[`fixtures/phase1_two_users_two_spaces.json`](fixtures/phase1_two_users_two_spaces.json)

| 维度 | 内容 |
| --- | --- |
| 用户 | Alice `9001`、Bob `9002` |
| 空间 | Alice：操作系统 `9101`、数据库 `9104`；Bob：网络 `9102` |
| 文档 | `tlb.md`、`broken.md`（FAILED）、`redis.md`、`tcp.md` |
| 概念 | TLB、Page Fault、Page Table、Redis、TCP |
| 关键状态 | TLB 误区 → 纠正 → DEMONSTRATED；Page Fault PARTIAL；Redis PARTIAL |
| 关键记录 | 旧 Memory `1407`、显式批注 Evidence `1703`、测试回答 `1704` |
| 知识边界 | READY / FAILED、无匹配、阈值过滤、降级、无可用文档 |

所有 id 都在测试号段，不含真实用户数据。每个用例都会对 fixture 做深拷贝，
执行后 `aclose()`，不写数据库；runner 汇总 `cleanupFailures`，Phase 1 全部为 0。

Fixture 还包含 `phase1_auto_space.json`，只用于验证产品 `FixtureRetriever`
的 `auto` 回退路径；它不是主 fixture 的替代。

## 4. 身份隔离如何验证

产品现有 `FixtureRetriever` 只读一份单空间 JSON，并且忽略 `user_id`。
如果直接用它跑跨用户用例，「没有串数据」可能只是数据里恰好没有。

因此 Phase 1 使用 `EvalRetriever`：

- 每次实例都绑定一个 `userId + spaceId`；
- 每个检索方法都检查请求身份与绑定身份；
- 不一致时 fail-closed 返回空，并记录 `isolation_violations`；
- `get_memories` 按 GLOBAL / SPACE / CONCEPT 过滤；
- `get_knowledge` 只返回当前空间、当前概念命中的片段。

这不是业务代码的替代品，而是**测试替身**。它验证的是 Agent 是否按身份
调用检索、是否把检索结果正确渲染；真实后端权限过滤仍由 Phase 2 的
Spring Boot / MySQL E2E 覆盖。

## 5. 执行类别与状态

### 5.1 执行类别

| 类别 | 含义 |
| --- | --- |
| `REAL_LLM` | 真实 provider 被调用，记录 HTTP 状态、延迟、输出字符数 |
| `MOCK` | 进程内 Agent + fixture 数据 + Mock / 固定输出模型 |
| `FIXTURE` | 只跑检索 / Context Builder / 产品 Retriever 工厂，不调用模型 |
| `FALLBACK` | 验证 `auto` 模式的回退行为 |
| `NOT_EXECUTED` | 需要 Spring Boot / MySQL / SSE 的登记用例 |

### 5.2 状态

| 状态 | 含义 |
| --- | --- |
| `PASS` | 所有硬断言通过 |
| `FAIL` | 至少一条硬断言不通过；缺陷确认用例的预期 FAIL 也是 FAIL |
| `BLOCKED` | 环境或上游不可用，不能得出 Agent 行为结论 |
| `SKIPPED` | 设计登记，Phase 1 不执行 |
| `NOT_IMPLEMENTED` | 产品明确不支持该能力 |

`BLOCKED` / `SKIPPED` 不计入 PASS 数。统计定义在 runner 与 summary 中自动校验：

- `executed = PASS + FAIL + BLOCKED`：真正发起过执行；
- `completed = PASS + FAIL`：得出了明确行为结论；
- `executed + SKIPPED + NOT_IMPLEMENTED = 总用例数`；
- `executed = completed + BLOCKED`。

因此「实际执行 38 条」不等于「38 条得出 PASS/FAIL」：其中 35 条完成，
3 条 BLOCKED。这两个数字必须分开写。

## 6. RecordingLLMAdapter

真实模型链路最容易出现「看起来跑了，但其实没调模型」或「调了但没输出」。
`evaluation/phase1/recording.py` 在真实 Adapter 外包一层透明记录：

- 每次调用记录 `caseId`、`requestId`、`callIndex`、adapter/provider/model；
- `callEmitted`：请求是否真的发出；
- `httpStatus`、`latencyMs`、`errorType`、`errorMessage`；
- `promptChars`、`promptSha256`、`estimatedPromptTokens`、`messageRoles`；
- `outputEmitted`、`outputChars`、`rawOutput`（截断 4000 字符并脱敏）；
- `tokenUsage`：适配器没有暴露时写 `null`，不编造；
- 原始输出会脱敏 `Bearer`、`sk-*`、`api_key/token/password/secret`、邮箱；
- 不记录完整 prompt、Authorization 或用户敏感内容。

真实模型连续返回空流时，runner 最多重试 3 次。三次仍无有效输出则记
`BLOCKED`，而不是把上游抖动算成 Agent 的回答质量失败。

Phase 1.1 实测：B08 / D08 / E07 各 3 次，共 9 次请求均 `callEmitted=true`、
HTTP 200，但 `outputChars=0`、`tokenUsage=null`。这证明请求确实到达 provider，
不能证明回答质量。空流原因（限流、内容过滤、`max_tokens`、流式实现、provider
状态）尚未定位，见 [`open-risks.md`](open-risks.md) R-P2-03。

## 7. 运行方式

在仓库根目录执行：

```bash
# 重新生成 40 条用例（只在修改 build_cases.py 后需要）
python -m evaluation.phase1.build_cases

# 只列用例，不执行
python -m evaluation.phase1 --list

# 确定性套件：Mock / Scripted / Context / Fixture，不调用真实模型
python -m evaluation.phase1 --no-real

# 全量：包含 3 条真实模型用例
python -m evaluation.phase1

# 只跑指定用例
python -m evaluation.phase1 --cases A01,B07,D01

# 只跑某一类
python -m evaluation.phase1 --category D

# 只跑某类执行模式
python -m evaluation.phase1 --mode real_llm
```

输出：

- `evaluation/phase1/reports/latest.json` / `latest.md`
- `evaluation/phase1/reports/deterministic.*`（`--no-real` 归档）
- `evaluation/phase1/reports/real-llm.*`（真实模型子集归档）
- `evaluation/phase1/reports/full.*`（全量归档）
- `evaluation/phase1/artifacts/<runId>/<caseId>.json`（逐用例原始证据）

## 8. 缺陷到用例的映射与修复结果

| 审计缺陷 | 用例 | 修复前 | Phase 1.1 结果 |
| --- | --- | --- | --- |
| Agent HTTP 端点无鉴权 | B07 | FAIL（P0） | PASS；401 / 403 / 503 + 身份绑定 |
| `auto` 模式无凭据静默回退 Fixture | D07 | FAIL | PASS；结构化回退元数据 |
| fixture 模式绕过 analyze 的 `allow_degradation=False` | B06 | FAIL（P0） | PASS；LLM 调用前抛 `RetrievalError` |
| Context Builder 预算口径不一致 | A07 | FAIL | PASS；动态上下文预算 + 完整 prompt 如实报告 |
| 认知区块被预算丢光后谎称没有历史 | A06 | FAIL | PASS；区分「检索为空」与「预算丢弃」 |
| History Guard 未考虑按需 `state.evidences` | B03 | FAIL | PASS；合并、去重、计数校验 |
| History Guard 与 Evidence Guard 兜底说明口径 | B04 | PASS（当前文案） | 仍 PASS，但文案是否合并待产品决定 |
| `recent_history_limit=0` 被 `[-0:]` 解释成全部历史 | E06 | FAIL（契约待确认） | PASS；契约定为「0 = 不注入，负数非法」 |

7 个 FAIL 全部修复；修复后的全量回归为 **35 PASS / 0 FAIL / 3 BLOCKED /
2 SKIPPED**。这不表示 40 条全部通过：3 条真实模型用例被上游空流阻塞，
A05 / C06 在 Phase 1 设计上不执行（后端 Phase 2 另行覆盖）。

## 9. 已知限制

1. Phase 1 的 40 条用例仍不直接连接 Spring Boot / MySQL / SSE；A05、C06 在
   Phase 1 保持 `designed_only`。Phase 2 已用真实 MySQL 服务层测试与真实 HTTP
   演示补证跨会话读取，但那属于后端证据，不能回填成 Agent 用例 PASS；
2. 并发、重复投递、事务回滚、性能基线属于 Phase 2；Phase 2 已执行一部分，
   性能基线仍未执行；
3. 真实模型只有 3 条冒烟，且 9 次调用全部空流，不能代表整体回答质量；
4. `Mock` 回答会复述 system prompt 的部分内容，语义 rubric 在 Mock 用例上
   只供参考，不能当作模型教学质量；
5. 跨用户隔离在 Phase 1 验证的是 Agent 是否按身份检索；真实 JWT / MySQL
   权限过滤由 Phase 2 的 `SecurityContextIntegrationTest` 补证；
6. `recent_history_limit=0` 的契约由本阶段确定为「0 = 不注入，负数非法」；
   若产品另有定义，E06 需要重新评审，不能把当前实现当成唯一正确语义；
7. 两个 Guard 兜底文案是否合并仍待产品决策，B04 的 PASS 只代表当前两段
   说明各自限定范围、不互相冒充。
