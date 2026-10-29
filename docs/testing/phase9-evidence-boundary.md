# Phase 9：Agent 输出证据边界收口与项目冻结

- 日期：2026-10-29
- 代码基线：`6d05486`（`6d054869ebfb7b13fd89053530cf64bd4e45dd7f`）
- 工作区：包含 Phase 6–9 未提交修改
- 原始日志：`evaluation/reports/phase9-20261029/`
- 本轮不新增产品功能、不引入新框架，只处理两条预算契约 XFAIL 与文档收口

> 本文把「确定性可验证的行为」「需要语义判断的自然语言」「当前架构无法可靠保证的
> 行为」分开写。没有把未执行的验证记为通过，也没有用关键词拦截或硬编码答案让测试变绿。

---

## 1. 阶段一：当前状态复核

### 1.1 运行路径与交付时机

普通对话：

```text
AgentRuntime.stream
  → prepare()（检索 + ContextBuilder 预算裁剪）
  → generate()
      → Tutor.stream() 逐 delta 产出
      → StreamingCitationFilter.feed(delta)  ← 本轮新增的执行点
      → yield 安全文本（SSE delta）
  → validate_citations(prepared)（只校验**已交付文本**并写 trace）
  → done
```

关键事实：**模型内容在 `validate_citations` 之前就已经逐段发送给用户。**
因此「事后校验发现非法引用」无法撤回任何已发送文本；本轮把判定提前到
`generate()` 内的每个 delta。

复盘回答（`history_review_requested`）本来就先完整生成、再推送，因此直接对
完整文本调用 `filter_text()`，不增加额外缓冲。

### 1.2 来源集合是否与最终注入上下文一致

修改前不一致：`validate_citations` 默认对全部 `state.knowledge` 判合法，
而被 Token 预算裁掉的 chunk 仍在 `state.knowledge` 里。现在两条入口都改为
`_provided_labels(prepared)`：

```python
provided_labels_from_context(prepared.context, prepared.state.knowledge)
```

该函数只遍历 `context.sections` 中 `knowledge:*` 且 `dropped=False` 的区块，
因此合法集合 = **本轮实际进入 system instruction 的片段**，不是检索结果。

### 1.3 合法集合的生命周期

`_provided_labels()` 每次调用都从当前 `PreparedRun` 重新计算，不读缓存；
`StreamingCitationFilter` 在每次 `generate()` 内新建。不存在跨请求、跨用户、
跨 Space 复用的路径。`test_filter_is_per_request_no_cross_request_reuse` 与
`test_runtime_does_not_reuse_source_set_between_requests` 钉住这一点。

### 1.4 `SUCCESS_BUT_DROPPED` 的 Guard 路径

代码事实：`SUCCESS_BUT_DROPPED` / `SUCCESS_PARTIAL` **不在** `GUARD_STATUSES` 中。
这是正确的：

- `GUARD_STATUSES` 表示「本轮确实没有可用资料」，追加固定说明不会与事实矛盾；
- `SUCCESS_BUT_DROPPED` 表示「检索到了，但片段没进最终上下文」，追加
  「没有检索到资料」反而会制造新的错误陈述；
- 这两种状态的准确说明已经在 Context Builder 阶段写入 system instruction
  （`SUCCESS_BUT_DROPPED` / `SUCCESS_PARTIAL` 状态文案与计数）。

`test_budget_states_do_not_trigger_no_material_guard` 断言这两种状态不会产生
`EVIDENCE_GUARD` 事件，也不会追加「没有检索到」说明。

### 1.5 两条 XFAIL 的原始行为

Phase 5/6 的原始日志（`evaluation/phase5_budget/reports/`、
`evaluation/reports/phase6-20261029/`）记录：

```text
tests/test_runtime_budget_contract.py
3 passed, 2 xfailed
```

两条 strict XFAIL 分别是：

1. `test_runtime_must_not_deliver_dropped_chunk_citation`：越界引用仍被交付；
2. `test_runtime_must_not_deliver_no_retrieval_claim_when_chunks_dropped`：
   模型误述「没有检索到资料」仍被交付。

本轮实现过滤后第一次运行定向测试：

```text
FAILED test_runtime_budget_contract.py::test_validate_citations_rejects_dropped_chunk_as_source
FAILED test_runtime_budget_contract.py::test_runtime_must_not_deliver_dropped_chunk_citation  # strict XPASS
FAILED test_runtime_citations.py::test_runtime_records_invalid_citation_in_trace
3 failed, 3 passed, 1 xfailed
```

- 第 2 条变成 **XPASS**，说明「不交付越界引用」已经从目标变成事实；
- 第 1、3 条失败是因为旧测试仍按「事后校验能看到非法引用」断言，需要调整为
  「交付文本干净 + 原始错误在 `CITATION_FILTER` trace」；
- 第 2 条（语义误述）仍然 XFAIL。

为了证明这两条测试不是「写了就过」的空断言，另做了一次**负向对照**：用临时
pytest 插件把 `StreamingCitationFilter` 替换为直通实现（等价于改动前的交付行为），
再跑同两条测试：

```text
FAILED test_runtime_must_not_deliver_dropped_chunk_citation
1 failed, 1 xfailed in 0.24s
```

日志：`prefix-behaviour-control.log`。这说明：

- 去掉过滤后，`test_runtime_must_not_deliver_dropped_chunk_citation` 确实失败，
  测试能区分旧行为；
- 语义误述那条在直通行为下仍是 XFAIL，和 Phase 5/6 的基线一致。

随后按新契约调整测试，结果见第 4 节。

---

## 2. 阶段二：来源引用边界

### 2.1 方案比较

| 方案 | 正确性 | 复杂度 | 流式延迟 | 失败行为 | 结论 |
| --- | --- | --- | --- | --- | --- |
| A. 完整缓冲后再校验交付 | 高：可在交付前拦下整段回答 | 高：需要处理重生成、回退、超时 | 首字延迟 = 完整生成 | 校验失败只能重生成或回退 | 仅用于已有的显式复盘路径；普通对话不采用 |
| B. 事后校验 + error 事件 | 低：内容已发送，无法撤回 | 低 | 无 | 用户已看到非法引用 | 不采用 |
| C. 流式标签过滤（本轮） | 中高：对可机器核对的来源标签确定有效 | 中：需处理跨 delta 与未闭合前缀 | 仅在被过滤的标签附近增加一个标签宽度 | 拿不准时丢弃而不是放行 | **采用** |
| D. 按句缓冲 + 句子级语义检查 | 可能更高，但仍无法判断任意自然语言 | 高：需要语义模型/规则 | 增加一句的延迟 | 误判风险高 | 不做；超出本轮范围 |

选择 C 的理由：它把**唯一可客观核对的对象**（`资料《文件名》#序号`）在离开
Runtime 之前处理掉，同时保留正常流式体验。它不试图解决语义误述——那是第 3 节
单独记录的风险。

### 2.2 跨 delta 处理

`StreamingCitationFilter` 不是逐块字符串匹配：

- 每个 delta 追加到 `_pending`；
- 扫描完整的 `[ ... ]`：
  - 精确匹配 `CITATION_PATTERN`：在合法集合内放行，否则记入 `dropped`；
  - 匹配 `CITATION_LIKE_PATTERN`（`[资料…]`、`[#1]`）：记入 `malformed`，不交付；
  - 普通方括号（Markdown 链接、数组下标）：原样放行；
- 尾部未闭合时，只有「可能是标注开头」的前缀（`[`、`[资`、`[资料`、`[#`）
  才被扣住；其余文本立即放行；
- 流结束时 `finish()` 处理残留：`[资料…` / `[#…` 按残缺标注丢弃，
  普通 `[0` 等文本放行；
- 未闭合缓冲超过 512 字符时按残缺标注丢弃，避免无限缓冲。

因此 `[资料《or` + `bit.md》#0]` 会先扣住，拼完后再判定。新增测试覆盖了
合法与越界两种跨 delta 情况。

### 2.3 延迟与失败行为

- 正常文本不缓冲；只有「看起来像标注开头」的尾巴最多等待一个标签宽度；
- 若模型在标注中途异常结束，`finish()` 不会被调用，未完成的标注不会交付；
- 若标注在正常结束时仍未闭合，按 malformed 丢弃；
- 丢弃标注后，标注前的空白已经发送，无法回撤。交付文本可能出现
  「字 空格。」的轻微排版痕迹，但**标注本身不会泄漏**。这是流式取舍，
  已写入 README 已知限制。

### 2.4 生产代码变更

| 文件 | 变更 |
| --- | --- |
| [`agent/app/citation_filter.py`](../../agent/app/citation_filter.py) | 新增 `StreamingCitationFilter` / `filter_text` / `CitationFilterReport` |
| [`agent/app/runtime.py`](../../agent/app/runtime.py) | 普通路径逐 delta 过滤；复盘路径完整过滤；`validate_citations` 改用本轮实际注入来源；新增 `CITATION_FILTER` trace |
| [`agent/app/trace.py`](../../agent/app/trace.py) | 新增 `CITATION_FILTER` 事件类型 |

没有修改 SSE 事件类型、Java 网关协议或数据库结构，因此不需要 Java 侧适配。

---

## 3. 阶段三：知识状态误述

### 3.1 四种状态

| 状态 | 检索 | 最终 Prompt | 合法来源集合 | Runtime 兜底 |
| --- | --- | --- | --- | --- |
| 没有检索到资料 | `NO_MATCH` / `NO_VALID_DOCUMENTS` / `THRESHOLD_FILTERED` / `DEGRADED` | 状态说明 | 空 | `EVIDENCE_GUARD` 可追加固定说明 |
| 检索到且注入 | `SUCCESS` | 片段 + 状态 | 全部注入片段 | 不追加 |
| 检索到但未注入 | `SUCCESS_BUT_DROPPED` | 状态 + 计数，**不渲染片段** | 空 | 不追加「没有资料」，否则与事实矛盾 |
| 部分注入 | `SUCCESS_PARTIAL` | 状态 + 注入/丢弃计数 | 仅注入片段 | 不追加 |

四种状态都有确定性字段：`context.knowledge_state`、
`knowledge_injected_chunk_ids`、`knowledge_dropped_chunk_ids`，
以及 `provided_labels_from_context()` 的结果。

### 3.2 可确定验证 vs 语义判断

**可由运行时确定验证：**

- 检索/注入状态字符串与 chunk ID 集合；
- 哪些标签可以合法交付；
- 交付文本中是否还存在越界/残缺标签；
- trace 中 `CITATION_FILTER` 记录的原始越界标签；
- `EVIDENCE_GUARD` 是否被错误触发。

**需要语义判断：**

- 模型是否用自然语言说「没有检索到资料」「资料里没有这个信息」；
- 同义改写、反问、暗示等表达；
- 模型是否在引用合法来源的同时作出超出该来源的推论。

**当前架构无法可靠保证：**

- 任意自然语言陈述与系统内部检索状态的一致性。要可靠识别，需要语义模型或
  关键词/模板拦截；后者正是本轮明确禁止的「简单关键词拦截」，且会误伤
  「资料里确实没有这一条」等正确表述。

### 3.3 第二条 XFAIL 的去留

保留为 `xfail(strict=True)`，理由写入测试：

```text
已记录的剩余风险，不是待修复缺陷：检索成功但被裁剪时，模型可能用自然语言
误述「没有检索到资料」。该句是语义断言，确定性状态与引用 ID 无法验证；
按契约不允许用关键词拦截或硬编码改写，因此 Runtime 不做纠正。
```

Prompt 已经写入 `SUCCESS_PARTIAL` / `SUCCESS_BUT_DROPPED` 的准确状态；
这提高了模型遵守的概率，但**不是运行时保证**。因此：

- 不把这条 XFAIL 改成 PASS；
- 不删除断言；
- 不通过追加固定说明来「覆盖」模型原文（那会产生自相矛盾的交付文本）；
- 在 README 与本文中明确记录为已知风险。

---

## 4. 阶段四：测试与回归

### 4.1 定向测试

```bash
cd agent
python -m pytest tests/test_citation_filter.py \
  tests/test_runtime_budget_contract.py tests/test_runtime_citations.py -v
```

结果：**22 passed, 1 xfailed, exit 0**（日志 `agent-citation-targeted.log`）。

覆盖矩阵：

| 场景 | 测试 |
| --- | --- |
| 正常注入 / 合法来源 | `test_valid_citation_passes_through`、`test_runtime_delivers_valid_citation_from_injected_source` |
| 部分注入 | `test_partial_budget_exposes_only_injected_chunk_as_source`、`test_partial_budget_delivers_only_injected_citation` |
| 全部裁剪 | `test_success_but_dropped_state_reaches_final_model_context` |
| 无检索结果 | `test_runtime_with_no_retrieval_drops_citation` |
| 非法来源 | `test_out_of_scope_citation_is_dropped_but_reported`、`test_runtime_must_not_deliver_dropped_chunk_citation` |
| 跨 delta 引用 | `test_citation_split_across_deltas_is_reassembled`、`test_out_of_scope_citation_split_across_deltas_is_dropped`、`test_runtime_drops_citation_split_across_deltas` |
| 残缺标注 | `test_malformed_citation_like_is_dropped`、`test_unclosed_citation_prefix_is_not_delivered` |
| 普通方括号 | `test_plain_brackets_are_not_citations`、`test_incomplete_short_bracket_at_stream_end_is_delivered` |
| 跨请求不复用 | `test_filter_is_per_request_no_cross_request_reuse`、`test_runtime_does_not_reuse_source_set_between_requests` |
| 预算状态不误触 Guard | `test_budget_states_do_not_trigger_no_material_guard` |
| 校验器仍拒绝越界标签 | `test_validate_citations_rejects_dropped_chunk_as_source` |
| 语义误述（开放） | `test_runtime_must_not_deliver_no_retrieval_claim_when_chunks_dropped`（XFAIL） |

### 4.2 全量 Agent

```bash
cd agent && python -m pytest -q
```

结果：**382 passed, 5 skipped, 1 xfailed, exit 0**（`agent-full-suite.log`）。

5 个 skip 仍是环境门控：3 条真实 Knowledge/Qdrant（需显式开关），
2 条真实 RabbitMQ（需 `AILS_TEST_RABBITMQ_URL`）。

### 4.3 离线评测

```bash
PYTHONPATH="$PWD:$PWD/agent" python -m evaluation -v
```

结果：**40/40 用例，124/124 断言，exit 0**（`offline-eval.log`）。
报告已由本次运行重新生成：`evaluation/reports/latest.md` / `latest.json`。

Phase 6 的显式评测替身注入与生产 fail-closed 约束未受影响：

```bash
cd agent && python -m pytest tests/test_evaluation_injection.py \
  tests/test_phase11_regressions.py -q
```

结果：**16 passed, exit 0**（`phase6-injection-safety.log`）。

### 4.4 后端

本轮没有修改 Python/Java 协议，但为确认工作区一致，仍跑了默认全量：

```bash
cd backend/backend && ./mvnw -B test
```

结果：**Tests run: 255, Failures: 0, Errors: 0, Skipped: 73, BUILD SUCCESS, exit 0**
（`backend-full-suite.log`）。

Surefire XML 聚合（`surefire-reconciliation.txt`）：

```text
report_files=39
total=255 executed=182 passed=182 skipped=73 failures=0 errors=0
```

即：`Tests run: 255` 包含跳过，不能写成 255 passed；实际执行并通过 182 条。

---

## 5. 阶段五：报告与冻结

### 5.1 Phase 2.1 历史表述修订

[`phase2.1-integration-results.md`](phase2.1-integration-results.md) 的
SSE-2.1-01 原文写「end 到达时 … run=COMPLETED」。本轮：

- 保留原始观测与测试结果；
- 在表格单元内加「Phase 8 修订」标记；
- 新增 §2.1 修订说明，记录 `ChatSseOrderingProbeTest` 的 20/20 RUNNING 证据；
- 明确 `message.end` 只表示「消息已落库、文本流结束」，不保证 `agent_run` 已终态。

### 5.2 Phase 5–8 报告一致性

| 报告 | 原数字/结论 | 本轮处理 |
| --- | --- | --- |
| Phase 5 | 2 xfailed；越界引用仍交付 | 追加 §10.6：第 1 条已关闭，第 2 条仍开放；新全量 382 |
| Phase 6 | 2 xfailed；40/40、124/124 | 追加修订注记；重跑仍 40/40、124/124 |
| Phase 7 | Python 365 / 5 skipped / 2 xfailed；Java 254 条（182 执行 / 72 skip） | 追加 Phase 9 数字；注明 Phase 8 新增探针后为 255 条（182 执行 / 73 skip） |
| Phase 8 | Python 365 / 2 xfailed；Java 255 条 | 追加 §5.1；Java 255 口径不变 |

历史数字保持原样，只加修订标记，不做「用新结果覆盖旧报告」。

### 5.3 手工数据库迁移步骤

已有库升级必须先备份，再按版本顺序执行：

```bash
# 1) 文档 RAG（早于 Phase 4.1 的库需要）
mysql -h 127.0.0.1 -u root -p ails \
  < backend/backend/src/main/resources/schema-migration-document-rag.sql

# 2) Phase 7 截断字段
mysql -h 127.0.0.1 -u root -p ails \
  < backend/backend/src/main/resources/schema-migration-phase7-truncation.sql
```

核对：

```sql
SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'message'
  AND COLUMN_NAME = 'truncated';

SHOW COLUMNS FROM agent_run LIKE 'status';
```

要点：

- 两个脚本都可重复执行；
- `schema.sql` 的 `CREATE TABLE IF NOT EXISTS` **不会**给旧表补列；
- 漏跑 Phase 7 脚本时，新版 MyBatis 查询会报
  `Unknown column 'truncated' in 'field list'`；
- 历史消息的 `truncated` 由默认值 0 补齐，语义是「功能上线前的历史回复视为完整」，
  不是从内容推断；
- Spring **不会**自动执行迁移脚本。

### 5.4 验证状态分层

| 状态 | 内容 | 证据 |
| --- | --- | --- |
| **已验证** | 流式来源过滤、跨 delta、每请求来源集合、预算状态与 Guard 边界 | 定向 22 passed / 1 xfailed；全量 382 passed |
| **已验证** | 离线评测与分析链路注入不受影响 | 40/40、124/124；injection/safety 16 passed |
| **已验证** | Java 默认全量无回归 | 255 条 / 182 执行通过 / 73 skip / 0 fail |
| **已验证（Phase 8）** | 旧库迁移脚本、真实 MySQL 上的 MQ processor | Phase 8 日志与 §2 |
| **部分验证** | 真实模型 `finish_reason=length` | 仅 Python runtime；未跑通 Java 整链 |
| **部分验证** | RabbitMQ | 外部真实 broker + processor 通过；Testcontainers profile 未通过（Docker 不可用） |
| **未验证** | Flutter 截断提示 / SSE 消费 | 前端仍是脚手架 |
| **未验证** | 生产库迁移执行 | 只在本机旧库验证，未触碰真实部署 |
| **未验证（架构边界）** | 自然语言误述的自动识别 | strict XFAIL，无可靠确定性方案 |

### 5.5 项目声明

本轮没有新增产品功能、没有引入新框架。核心契约中：

- 「被裁剪片段不得作为来源交付」已由生产代码与确定性测试关闭；
- 「检索成功但被裁剪时，模型不得误述没有资料」**仍是开放风险**，
  当前架构不能在不做关键词拦截的前提下可靠保证；
- 其余开放项（真实模型整链、前端、Testcontainers MQ、生产迁移）保持原状态，
  没有把未执行验证写成通过。

---

## 6. 契约清单

### 6.1 已关闭

| 契约 | 实现 | 测试证据 |
| --- | --- | --- |
| 合法来源集合只包含本轮实际注入片段 | `runtime._provided_labels` + `provided_labels_from_context` | `test_partial_budget_exposes_only_injected_chunk_as_source` |
| 越界来源标签不得交付 | `StreamingCitationFilter` | `test_runtime_must_not_deliver_dropped_chunk_citation` |
| 跨 delta 标签正确处理 | `_pending` 缓冲与 `finish()` | `test_citation_split_across_deltas_is_reassembled` 等 |
| 残缺标签不得交付 | `malformed` 分支 | `test_malformed_citation_like_is_dropped` |
| 合法标签原样交付 | 精确匹配 `CITATION_PATTERN` | `test_runtime_delivers_valid_citation_from_injected_source` |
| 来源集合不跨请求复用 | 每次 `generate()` 新建 | `test_runtime_does_not_reuse_source_set_between_requests` |
| 预算裁剪状态不误触「没有资料」兜底 | `GUARD_STATUSES` 不含 `SUCCESS_PARTIAL` / `SUCCESS_BUT_DROPPED` | `test_budget_states_do_not_trigger_no_material_guard` |
| 模型原始越界内容仍可追溯 | `CITATION_FILTER` trace | `test_runtime_filters_invalid_citation_and_records_it_in_trace` |

### 6.2 仍开放

| 契约 | 状态 | 原因 |
| --- | --- | --- |
| 检索成功但被裁剪时不得误述「没有检索到资料」 | 开放（strict XFAIL） | 自然语言语义，无法用确定性状态验证；禁止关键词拦截 |
| 真实模型 → Java → 浏览器整链 | 未验证 | 只验证到 Python runtime |
| Flutter 截断提示与 SSE 消费 | 未验证 | 前端未实现 |
| Testcontainers MQ E2E | 未验证 | 本机 Docker 不可用 |
| 生产库迁移执行 | 未验证 | 只验证本机旧库 |
| `message.end` 时 `agent_run` 已终态 | 未承诺、未修改 | Phase 8 提出两种方案，等待产品/协议确认 |

---

## 7. 简历候选描述与证据

### 7.1 Agent 版

> 为 RAG 对话实现流式来源证据边界：在 Runtime 生成阶段按「本轮实际注入的
> 知识片段」动态构建合法来源集合，逐 delta 过滤越界与残缺引用，跨 delta 标签
> 先拼后判；将 Token 预算裁剪、无检索、部分注入等状态与引用校验统一到同一条
> 生产路径，并把模型原始越界内容保留在 `CITATION_FILTER` trace 中。新增 16 条
> 确定性回归（定向套件共 22 条通过 + 1 条 XFAIL）覆盖合法/越界/跨 delta/无检索/
> 部分注入/跨请求隔离；Agent 全量
> 382 passed / 5 skipped / 1 xfailed；离线评测 40/40、124/124 断言；
> Phase 6 分析链路显式注入与生产 fail-closed 回归 16 passed。

对应证据：

- 代码：[`citation_filter.py`](../../agent/app/citation_filter.py)、
  [`runtime.py`](../../agent/app/runtime.py)（`generate` / `_record_citation_filter` /
  `_provided_labels`）；
- 测试：[`test_citation_filter.py`](../../agent/tests/test_citation_filter.py)、
  [`test_runtime_budget_contract.py`](../../agent/tests/test_runtime_budget_contract.py)、
  [`test_runtime_citations.py`](../../agent/tests/test_runtime_citations.py)；
- 日志：`agent-citation-targeted.log`、`agent-full-suite.log`、`offline-eval.log`、
  `phase6-injection-safety.log`。

### 7.2 后端版

> 主导 LLM 流式回答的终态与证据边界：在 Java 侧解析 `finish_reason`，新增
> `agent_run.TRUNCATED` 与 `message.truncated`，使截断回答可被识别且不触发后续
> 学习分析；为已有数据库补充幂等迁移脚本，在真实 MySQL 旧库上验证
> `Unknown column 'truncated'` 的复现与修复；用真实 Spring + MySQL 探针定位
> `message.end` 与任务终态的顺序窗口。默认全量 255 条（182 执行通过 / 73 跳过 /
> 0 失败）；旧库迁移后 MQ processor 5/5 通过。

对应证据：

- 代码：`backend/backend/src/main/java/com/ails/backend/service/ChatService.java`、
  `.../agent/StreamCompletion.java`、`.../enums/AgentRunStatus.java`、
  `backend/backend/src/main/resources/schema-migration-phase7-truncation.sql`；
- 测试：`ChatServiceTest`、`HttpPythonAgentGatewayTest`、
  `ChatSseOrderingProbeTest`、`Phase2MqProcessorIntegrationTest`；
- 日志：`backend-full-suite.log`、`surefire-reconciliation.txt`、
  `evaluation/reports/phase8-20261029/`。

### 7.3 不能写进简历的内容

- 不写「已解决模型误述检索状态」——该契约仍开放；
- 不写「真实模型整链 / 前端截断提示已验证」——未执行；
- 不写「MQ Testcontainers E2E 已通过」——Docker 不可用；
- 不写「生产库已迁移」——只在本机旧库验证。
