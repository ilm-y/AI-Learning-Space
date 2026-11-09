# Phase 5 Step 2 连通性验证记录

> 日期：2026-11-09
> 脚本：`scripts/phase5/connectivity_check.py`（不含密钥；从 `agent/.env` 与未跟踪的 `application-local.yml` 读取）
> 原始结果：`scripts/phase5/connectivity-result.json`、`scripts/phase5/llm-retry-result.json`
> 关联审计：[phase5-preflight-audit.md](phase5-preflight-audit.md)

## 0. 调用预算与执行口径

| 项目 | 任务书上限 | 实际使用 | 说明 |
|---|---:|---:|---|
| LLM 调用 | 2 | **2** | 探针 #1（`max_tokens=16`）失败；探针 #2（生效值 4096）成功。即 1 次显式重试，符合“重试不超过 1 次”；两次都是 `max_attempts=1`，适配器内部无自动重试 |
| Embedding 调用 | 2 | **1** | `max_retries=0`，单次批量 1 条 |
| Tavily 调用 | 1 | **0** | 无集成点，标记 BLOCKED，不伪造 |
| Qdrant 写入 | 2 | **1** | 1 个点；collection 创建/删除不计为向量写入 |
| MySQL 写入 | 2 | **1 个事务** | 事务内 3 行（user/space/message）；按“写批次”计为 1 |
| 重试 | ≤1 | **1 次显式重试** | 自动重试 0；LLM 探针 #2 是对 #1 的 1 次显式重试，未超过上限 |

所有真实调用均记录耗时、结果、错误类型；Qdrant/MySQL 测试数据均已清理并二次验证。

## 1. 运行前提

### 1.1 端口可达性

| 目标 | 结果 |
|---|---|
| `127.0.0.1:6333` Qdrant | OPEN |
| `127.0.0.1:8080` Spring Boot | OPEN |
| `127.0.0.1:3306` MySQL | OPEN |
| `127.0.0.1:8000` Agent | OPEN |

### 1.2 生效配置（来自 `Settings()`，不包含密钥）

| 项 | 值 |
|---|---|
| `llm_provider` / `llm_model` | `openai` / `deepseek-flash` |
| `llm_base_url` host | `api.deepseek.com` |
| `llm_max_tokens` | `4096` |
| `llm_timeout_seconds` | `60.0` |
| `embedding_provider` / `embedding_model` | `openai` / `text-embedding-v4` |
| `embedding_base_url` host | `dashscope.aliyuncs.com` |
| `embedding_dimension` / `batch_size` | `1024` / `10` |
| `qdrant_url` / `collection_prefix` | `http://127.0.0.1:6333` / `ails_documents` |
| `retriever_mode` | `auto` |
| LLM / Embedding Key 是否配置 | 均为是（不回显值） |

## 2. LLM

### 2.1 探针 #1：最小预算（失败，暴露空流原因）

- 请求：单条 user message，内容为任务书指定的最小测试句“回答一个字：好”；`max_tokens=16`，`temperature=0`，`max_attempts=1`
- 结果：

| 字段 | 值 |
|---|---|
| HTTP | 200 |
| `request_id` | `7372dec849984827956c3b86a7adf75c` |
| `finish_reason` | `length` |
| `truncated` | `true` |
| usage | prompt 34 / completion 16 / total 50；其中 reasoning 16 |
| 内容长度 | 0 |
| `emptyReason` | `reasoning_only` |
| `errorCategory` | `empty_stream` |
| 耗时 | 936 ms |
| 错误 | `LLMError: 模型返回空流（reason=reasoning_only, status=200, attempts=1）` |

**解释：** 16 个 completion token 全部被 reasoning 消耗，`finish_reason=length`，正文为空。这不是“服务不可用”，而是预算不足。

### 2.2 探针 #2：生效预算（成功）

- 请求：同一最小测试句；`max_tokens=4096`（当前 `.env` 生效值），`temperature=0`，`max_attempts=1`
- 结果：

| 字段 | 值 |
|---|---|
| HTTP | 200 |
| `request_id` | `46312708899940b092afc4312d6a2af9` |
| `finish_reason` | `stop` |
| `truncated` | `false` |
| usage | prompt 34 / completion 25 / total 59；其中 reasoning 23 |
| 内容 | `好`（1 字符，非空） |
| 耗时 | 1087 ms |
| `errorCategory` | `none` |

**结论：** LLM 真实连通，当前模型可用。Phase 1/2 记录的“HTTP 200 空流”至少在当前模型上可由 reasoning token 吃掉预算解释；Step 3 必须使用足够大的 `max_tokens` 并记录 usage。

## 3. Embedding

- 输入：固定短文本“TCP 三次握手”
- 结果：

| 字段 | 值 |
|---|---|
| Provider | `openai-compatible` |
| Model | `text-embedding-v4` |
| 维度 | 1024（与配置一致） |
| 返回条数 | 1 |
| 非空 | 是 |
| 向量范数 | 1.0 |
| 耗时 | 403 ms |
| 指纹 | `v1:e7464a6c…`（不包含密钥） |

**结论：** 真实 Embedding 连通；没有回退 Hash，没有截断/补零。

## 4. Qdrant

### 4.1 隔离策略

- Collection：`phase5-test-20261109135143-b2e57395`
- 前缀符合任务书 `phase5-test-` 要求；未触碰任何生产 collection。
- 向量来源：上一步真实 Embedding 结果（1024 维，`text-embedding-v4`），不是 Fixture，也不是 hash 向量。

### 4.2 写入与召回

| 操作 | HTTP | 耗时 | 结果 |
|---|---:|---:|---|
| 创建 collection（Cosine, 1024） | 200 | 110 ms | 成功 |
| 写入 1 个点（payload 含 `phase`/`marker`/`vector_source`） | 200 | 28 ms | 写入 1 点 |
| 按同一向量检索 top-1 | 200 | 24 ms | 命中 1 条，score=1.0 |
| 召回校验 | — | — | `recalled=true`，payload marker 一致 |

### 4.3 清理与验证

| 操作 | HTTP | 耗时 | 结果 |
|---|---:|---:|---|
| 删除 collection | 200 | 11 ms | 成功 |
| 删除后 GET | 404 | — | `verifiedGone=true` |
| 重新列出 collections | — | — | `phase5-test-*` 无残留 |

**结论：** Qdrant 真实写入/检索/删除全部通过，测试数据已清理。

## 5. Tavily

- 状态：**BLOCKED**
- 调用次数：0
- 原因：Agent 代码、依赖、配置、工具抽象中均不存在 Tavily；没有可调用的集成点。
- 证据：
  - `agent/requirements.txt` 无 `tavily`
  - `agent/app` 下无 Tavily 客户端、无 tool/function-calling 循环
  - `agent/.env` 无 `AILS_TAVILY_*`
  - 历史 phase1/2/2.1/3.1/53/54 均未集成

**没有伪造搜索结果，也没有把“未集成”写成“调用失败”。**

## 6. MySQL

### 6.1 隔离策略

- 独立数据库：`ails_phase5_test_5a2041e0dedb`
- 只在该库内建 3 张最小探针表（`phase5_user` / `phase5_space` / `phase5_message`），不是生产表结构；生产库 `ails` 未写入。
- 使用项目未跟踪的 `application-local.yml` 中的本机凭据连接；本文档不回显口令。

### 6.2 写入与读取

| 操作 | 耗时 | 结果 |
|---|---:|---|
| 连接 MySQL 8.0.37 | 16 ms | 成功 |
| 创建独立数据库 | 29 ms | 成功 |
| 创建 3 张探针表 | 151 ms | 成功 |
| 1 个事务写入 3 行（user/space/message） | 25 ms | 成功 |
| JOIN 读回 | 0 ms | 返回 `phase5_probe_user / phase5_probe_space / user / phase5 probe message` |

### 6.3 清理与验证

| 操作 | 耗时 | 结果 |
|---|---:|---|
| `DROP DATABASE` | 45 ms | 成功 |
| `SHOW DATABASES LIKE 'ails_phase5_test_%'` | — | 无残留 |

**结论：** MySQL 真实写入/读取/清理通过；测试数据完全隔离。

## 7. 端到端最小链路：SKIPPED

任务书要求“只有 1–5 全部成功才执行最小端到端链路”。本次：

- Tavily 为 **BLOCKED**（不是成功）；
- 运行中的 Agent 对当前 `agent/.env` 的内部凭据返回 **401**（`GET /health/diagnostics`），Spring → Agent 受保护接口当前不可用；
- 当前默认知识 collection `ails_documents_text-embedding-v4_1024` 不存在，RAG 知识路径未就绪。

因此**未执行** Message → Agent → Policy → ContextBuilder → LLM → AgentRun 的端到端链路，不把未验证的链路写成 PASS。

## 8. 合并结论

### 8.1 当前可用

| 能力 | 状态 |
|---|---|
| LLM 真实调用（DeepSeek 兼容） | ✅ PASS（需足够 `max_tokens`） |
| Embedding 真实调用（DashScope 兼容） | ✅ PASS |
| Qdrant 写入/检索/清理 | ✅ PASS（隔离 collection） |
| MySQL 写入/读取/清理 | ✅ PASS（隔离库） |
| Tavily | ⛔ BLOCKED |
| 最小端到端 | ⏭ SKIPPED（前置未全部满足） |

### 8.2 进入 Step 3 前必须处理

1. **对齐内部凭据并重启 Agent/Spring**：运行中的 Agent 与 `.env` 不一致，当前 E2E 一定过不了。
2. **准备知识 collection**：要么把测试文档索引到隔离的 `phase5-*` collection，要么明确使用哪个 `ails_documents_*` collection；当前默认 collection 不存在。
3. **Tavily 范围决策**：实现集成（单独任务）或明确从 Phase 5 范围移除；不能假装通过。
4. **固定 LLM 预算**：Step 3 的 `max_tokens` 不低于 4096，并记录 `finish_reason` / usage；否则会重现 reasoning-only 空流。
5. **补齐可审计证据**：AgentRun 没有 model/token 字段，Step 3 需要从 SSE/日志/Manifest 采集，或先做 Schema 变更（需另行批准）。

## 9. 建议的 Step 3 最小范围

> 下面是建议，不是已执行内容。Step 3 仍需人工批准预算与范围。

### 9.1 场景

1 个 Space、1 个 Concept、1 份短文档、1 个 Session；问题使用一个文档可支撑的解释题，例如“TCP 三次握手为什么需要第三次握手？”。避免使用用户敏感数据。

### 9.2 三种策略 × 各 1 次

| 策略 | 上下文 | 目的 |
|---|---|---|
| A：无上下文基线 | 只给 system + 用户问题 | 观察模型自身知识/幻觉基线 |
| B：D4 上下文，无知识 | Session + U/M/E + History，Knowledge SKIP | 验证 Policy/ContextBuilder 是否真的改善“懂用户” |
| C：D4 + 知识 RAG | 在 B 基础上注入 1 份文档的 Knowledge chunks | 验证检索、引用、Manifest 与预算裁剪 |

每次运行记录：`run_id`、`trace_id`、`intent`、`retrieval_status`、`injection_status`、`manifest`、`finish_reason`、usage、`citations`、首字/总耗时。

### 9.3 预算建议（待批准）

| 资源 | 建议上限 |
|---|---:|
| LLM 调用 | 6（3 次主运行 + 最多 3 次受控重生成） |
| Embedding 调用 | 15（查询 + 文档分块索引） |
| Tavily | 0（当前 BLOCKED） |
| Qdrant 写入 | 2 个隔离 collection 以内 |
| MySQL 写入 | 1 个隔离库、2 个写事务以内 |
| 重试 | 每 Provider 自动重试 ≤1 |

### 9.4 盲评建议

- 2 名评审，输出匿名化并随机打乱策略标签；
- 评分维度：事实正确性、文档依据/引用一致性、上下文利用、幻觉/越权断言；
- 记录评分者间一致性；历史人工复核为 0/9，Step 3 若声称质量结论，必须补上盲评。

## 10. 未做与风险

- 未执行端到端链路（原因见 §7）。
- 未调用 Tavily（无集成点）。
- 未向当前默认知识 collection 索引文档；当前 RAG 运行态未验证。
- 未修改运行中的 Agent/Spring 配置；401 的根因（stale 进程 vs 环境变量差异）未在运行态进一步定位。
- MySQL 探针表是最小结构，不是生产 Schema；Qdrant payload 是最小探针，不是真实文档 payload。
- LLM 探针是最小句子，不代表真实教学场景的质量。
- 本次真实调用成本：2 次 LLM + 1 次 Embedding + 1 次 Qdrant 写入 + 1 个 MySQL 写事务。
- 未 commit、未 push；生产代码/配置/测试/Policy/ContextBuilder/Manifest 未修改。

## 11. Git 状态

本次新增文件：

```text
?? docs/testing/phase5-preflight-audit.md
?? docs/testing/phase5-connectivity-check.md
?? scripts/phase5/
```

未修改任何已跟踪文件；未 commit、未 push。

## 12. 需要人工决策的点

1. **Tavily**：Phase 5 是否必须包含联网检索？若必须，需要单独立项实现 Tavily 客户端 + 工具抽象 + ContextBuilder 来源；若不必须，请在 Step 3 范围中明确排除。
2. **知识 collection**：Step 3 使用隔离的 `phase5-*` collection，还是修复/索引当前 `ails_documents_text-embedding-v4_1024`？
3. **内部凭据**：由谁提供/对齐 `AILS_INTERNAL_TOKEN`、`AILS_INTERNAL_IDENTITY_SECRET` 并重启 Agent/Spring？本次只读审计不修改。
4. **Step 3 预算**：是否批准 §9.3 的调用/写入上限？
5. **盲评组织**：评审人、评分表、匿名化方式是否就绪？
6. **LLM 预算下限**：是否确认 Step 3 固定 `max_tokens >= 4096`，并接受低预算探针失败属于预期行为？
7. **AgentRun 证据字段**：是否在 Step 3 前增加 model/token usage 列（生产 Schema 变更），还是接受从日志/SSE 采集？
