# Phase 5 Step 1 预检审计

> 日期：2026-11-09
> 仓库：`D:\learningMatiarials\AI-Learning-Space`，基线提交 `1180705`
> 方式：只读审计（读取代码、配置、Schema、历史测试文档；未修改生产代码/配置/测试）
> 关联文档：[phase5-connectivity-check.md](phase5-connectivity-check.md)

## 0. 结论先行

| 结论 | 内容 |
|---|---|
| 可以直接进入 Step 3 的 | LLM（真实 DeepSeek 兼容接口）、Embedding（真实 DashScope 兼容接口）、Qdrant 写入/检索、MySQL 写入/读取 |
| 明确 BLOCKED 的 | Tavily：代码、依赖、配置、工具抽象全部不存在，没有可调用的集成点 |
| 进入 Step 3 前必须处理的 | ① Spring ↔ Agent 内部凭据不一致；② 当前默认知识 collection 不存在；③ Tavily 是否在 Phase 5 范围内需要人决策；④ AgentRun 缺 model/token 证据字段；⑤ Step 3 必须把 `max_tokens` 固定在足够大的值 |
| 历史 PASS 能否直接复用 | **不能**。phase53/phase54 早于 D4 上下文改造，且使用隔离 Qdrant collection / 隔离 MySQL 库；它们的 PASS 不能证明当前默认配置下的 D4 链路 |

本审计刻意区分两类结论：

- **[已复现]**：本次真实调用或运行态观测到的事实；
- **[静态证据]**：只由代码/配置/Schema 推导，尚未在运行态复现，不能当成已修复或已损坏。

## 1. 架构与真实数据流

```text
Flutter/Web
   │  HTTP + SSE
   ▼
Spring Boot (:8080) ────────────────► MySQL (ails / Hikari)
   │  1. 鉴权、会话、message、agent_run
   │  2. 签发短期 X-Internal-Identity
   │  3. 透传 user/space/conversation/history
   ▼
Python Agent (:8000, FastAPI)
   │
   ├─ AgentRuntime.prepare
   │    ├─ Retriever 路由 (auto/http/fixture)
   │    ├─ Concept 识别 + D4 plan_retrieval
   │    ├─ U / M / E / Revision / Related 并行检索
   │    ├─ KnowledgeSearchService ──► Embedding Provider ──► DashScope /embeddings
   │    │                              └─► Qdrant (:6333) 文档 collection
   │    └─ ContextBuilder.build ──► 最小 Manifest + 受保护上下文
   │
   ├─ LLM Adapter ──► DeepSeek /chat/completions (SSE)
   └─ done: { citations, contextManifest, finishReason, truncated }
            │
            ▼
Spring ChatService ──► message.citations
                    └─► agent_run.context_manifest / output_summary / assistant_message_id
```

关键边界：

- **MySQL 只由 Spring 访问**；Agent 不直连 MySQL（`agent/requirements.txt` 无 MySQL 驱动）。Agent 需要的历史数据一律经 Spring HTTP 读接口获得。
- **Qdrant 由 Agent 访问**；Spring 通过 Agent 的文档/记忆接口间接写入和删除向量。
- **Tavily 不在任何一段链路里**。

## 2. 配置读取路径表

Agent 使用 `pydantic-settings`：

- 环境变量前缀 `AILS_`：`agent/app/config.py:22`
- `.env` 固定读取 `agent/.env`：`agent/app/config.py:23`
- 校验失败不回显输入值（避免密钥进异常）：`agent/app/config.py:25`
- 当前仓库根目录、`backend/`、`backend/backend/` 均无 `.env`；唯一生效的是 `agent/.env`。

| Provider | 配置项 | 默认值（代码） | 当前 `.env` 生效值 | 读取入口 | 真实出口 |
|---|---|---|---|---|---|
| LLM | `AILS_LLM_PROVIDER` | `mock` (`config.py:77`) | `openai` (`agent/.env:23`) | `build_llm_adapter` (`agent/app/llm/factory.py:17`) | `{base}/chat/completions` (`agent/app/llm/openai_compatible.py:434`) |
| LLM | `AILS_LLM_BASE_URL` | `https://api.deepseek.com/v1` (`config.py:78`) | `https://api.deepseek.com/v1` (`agent/.env:24`) | 同上 | 同上 |
| LLM | `AILS_LLM_MODEL` | `deepseek-chat` (`config.py:80`) | `deepseek-flash` (`agent/.env:26`) | 同上 | 同上 |
| LLM | `AILS_LLM_MAX_TOKENS` | `1024` (`config.py:82`) | `4096` (`agent/.env:28`) | 同上 | 同上 |
| LLM | `AILS_LLM_API_KEY` | 空 | 已配置（不在此文档回显） | `build_llm_adapter` 显式校验非空 | Bearer Header |
| Embedding | `AILS_EMBEDDING_PROVIDER` | `hash` (`config.py:141`) | `openai` (`agent/.env:39`) | `build_embedding_provider` (`agent/app/embedding/factory.py:11`) | `{base}/embeddings` (`agent/app/embedding/openai_compatible.py:54`) |
| Embedding | `AILS_EMBEDDING_BASE_URL` | `https://api.openai.com/v1` (`config.py:142`) | `https://dashscope.aliyuncs.com/compatible-mode/v1` (`agent/.env:40`) | 同上 | 同上 |
| Embedding | `AILS_EMBEDDING_MODEL` | `text-embedding-3-small` (`config.py:144`) | `text-embedding-v4` (`agent/.env:41`) | 同上 | 同上 |
| Embedding | `AILS_EMBEDDING_DIMENSION` | `256` (`config.py:146`) | `1024` (`agent/.env:42`) | 同上 | 严格校验维度 |
| Embedding | `AILS_EMBEDDING_API_KEY` | 空 | 已配置（不在此文档回显） | 允许为空（本机兼容服务可不鉴权） | Bearer Header |
| Qdrant | `AILS_QDRANT_URL` | `http://127.0.0.1:6333` (`config.py:155`) | 未覆盖 | `QdrantDocumentStore` (`agent/app/vectorstore/qdrant_store.py:133`) | `AsyncQdrantClient` (`:152`) |
| Qdrant | `AILS_QDRANT_COLLECTION_PREFIX` | `ails_documents` (`config.py:159`) | 未覆盖 | `collection_name_for` (`:79`) | `<prefix>_<model>_<dimension>` |
| Qdrant | `AILS_QDRANT_API_KEY` | 空 | 未配置 | `AsyncQdrantClient` | 可选的 `api-key` |
| MySQL | `MYSQL_HOST/PORT/DB/USER/PASSWORD` | `localhost/3306/ails/root/root` (`backend/.../application.yml:14-16`) | 由 Spring 启动环境与未跟踪的 `application-local.yml` 覆盖 | Spring `DataSource` | Hikari → MySQL |
| Tavily | 无 | 无 | 无 | 无 | 无 |

补充：

- `application-local.yml` 被 `backend/backend/.gitignore:9` 忽略，未跟踪；本文档不复述其中的口令。
- 当前 `.env` 没有 `AILS_INTERNAL_TOKEN`、`AILS_INTERNAL_IDENTITY_SECRET` 等内部凭据项；这不代表运行中的 Agent 没有这些配置（见 §8 P0-3 的运行态 401）。
- 当前 `.env` 没有 `AILS_QDRANT_URL` / `AILS_QDRANT_COLLECTION_PREFIX` 覆盖，因此默认 collection 为：

```text
ails_documents_text-embedding-v4_1024
```

## 3. Provider 入口表

| Provider | 工厂/装配 | 真实实现 | 是否允许静默回退 | 证据 |
|---|---|---|---|---|
| LLM | `build_llm_adapter` | `OpenAICompatibleAdapter` | **否**。`openai` 无 Key 直接 `ValueError`；未知 provider 直接报错 | `agent/app/llm/factory.py:17-38` |
| Embedding | `build_embedding_provider` | `OpenAICompatibleEmbeddingProvider` | **否**。`openai` 路径失败显式抛 `DocumentEmbeddingError/Unavailable`，不会切 Hash | `agent/app/embedding/factory.py:11-38`；`agent/app/embedding/openai_compatible.py:91-157` |
| Qdrant | `QdrantDocumentStore` / `KnowledgeSearchService` | 真实 `AsyncQdrantClient` | **`FallbackRetriever` 路径的 Knowledge 不回退 Fixture**；但 `auto` 无凭据 / 显式 `fixture` 模式会整体使用 `FixtureRetriever`（含样例 Knowledge）。U/M/E 在 `auto` 后端失败时可回退 Fixture 但留轨迹 | `agent/app/vectorstore/qdrant_store.py:133-160`；`agent/app/retriever/routing.py:70-109,141-155`；`agent/app/retriever/fixture.py:95-105` |
| Tavily | 无 | 无 | 不适用 | 全仓库检索无实现 |

## 4. Tavily 状态：BLOCKED

[静态证据] 全仓库检索（排除 `.git`、`node_modules`、构建产物）后，除本次新增的 Phase 5 脚本与结果文件外，**没有任何 Tavily 引用**。

| 检查项 | 结果 | 证据 |
|---|---|---|
| Python 依赖 | 无 | `agent/requirements.txt` 无 `tavily` |
| 客户端/工具抽象 | 无 | `agent/app` 下无 Tavily 客户端、无 tool/function-calling 循环 |
| 配置项 | 无 | `agent/.env` 无 `AILS_TAVILY_*`；`config.py` 无字段 |
| 历史测试 | 无 | phase1/2/2.1/3.1/53/54 均未调用 Tavily |
| ContextBuilder 来源 | 无 | Manifest 的 `type` 只来自 U/M/E/Knowledge/Session/History 等策略来源 |

**结论：** 不存在“Tavily 调用失败”这回事，因为根本没有集成点。按任务书必须标记 **BLOCKED**，不能伪造搜索结果，也不能把它当成“可选但已通过”。

## 5. MySQL / Qdrant 的连接与隔离

### 5.1 MySQL

- Spring 连接串：`jdbc:mysql://${MYSQL_HOST:localhost}:${MYSQL_PORT:3306}/${MYSQL_DB:ails}`，`createDatabaseIfNotExist=true`（`application.yml:14`）。
- 当前本机 3306 端口开放；使用未跟踪的 `application-local.yml` 中的项目凭据可连接，MySQL 版本 8.0.37。
- 现有隔离测试库很多（`ails_*_it`、`ails_phase54_rag/mq` 等），说明项目已有“每类集成测试一个独立库”的约定。
- **没有专门的 Phase 5 库**；本次连通性验证临时创建 `ails_phase5_test_<uuid>`，验证后已 DROP。

### 5.2 Qdrant

- 当前 6333 端口开放，已有 collection：
  - `ails_documents_hash-v1_1024`
  - `ails_knowledge_e2e_verify_text-embedding-v4_1024`
  - `ails_memory_eval_text-embedding-v4_1024`
- **当前配置推导出的文档知识 collection `ails_documents_text-embedding-v4_1024` 不存在**。[已复现：Qdrant collection 列表]
- Knowledge 检索的 collection 名由 `model + dimension + prefix` 决定；当前默认前缀是 `ails_documents`，与 phase54 使用的 `ails_phase54` 不同。
- 本次连通性验证使用独立 `phase5-test-<timestamp>-<uuid>` collection，写入 1 个真实 Embedding 向量、检索召回、删除并验证 404；无残留。

## 6. Phase 1–4 能力/缺口逐项审计

| # | 能力 | 结论 | 证据与说明 |
|---|---|---|---|
| 1 | LLM 模型/API 支持 | **PASS** | 支持任意 OpenAI 兼容 `/chat/completions`；当前 `deepseek-flash` 真实调用成功（第二次探针，`finish_reason=stop`，内容“好”）。见 connectivity 文档。 |
| 2 | Embedding 真实外部调用 vs FakeEmbedding 回退 | **PASS** | 当前走 `OpenAICompatibleEmbeddingProvider`，真实返回 1024 维；工厂与实现都没有 Hash 静默回退。 |
| 3 | Qdrant 真实 collection/向量 vs Fixture 绕过 | **PARTIAL** | 真实 Qdrant 写入/检索 PASS；但当前默认知识 collection 不存在，运行态知识检索会 DEGRADED/空。需要区分两条路径：① `auto` **有凭据**时走 `FallbackRetriever`，后端失败后 Knowledge 返回 DEGRADED、不回退 Fixture（`routing.py:141-155`）；② `auto` **无凭据**或显式 `fixture` 模式会直接返回 `FixtureRetriever`（`routing.py:79,95-96`），此时 `FixtureRetriever.get_knowledge` 会返回样例知识（`fixture.py:95-105`）。因此“Knowledge 永不使用 Fixture”不成立，准确说法是“`FallbackRetriever` 路径不回退 Fixture”。当前运行态由 Spring 透传凭据，理论上走①；Step 3 必须断言 retriever 名称/降级原因，防止无凭据时把样例知识当成真实文档。 |
| 4 | Tavily 在工具链中 | **BLOCKED** | 无任何集成点（§4）。 |
| 5 | Tavily 结果进入 ContextBuilder | **BLOCKED** | 无 Tavily 来源类型；ContextBuilder 只接收策略来源。 |
| 6 | 工具失败静默回退 | **PASS（有可观测降级，但存在 Fixture 分支需约束）** | 无工具层；检索降级不静默：`FallbackRetriever` 记录 `degraded` 与 warning；Analyze 链路 `allow_degradation=False`。但 chat 的 `auto` 无凭据 / 显式 `fixture` 模式会直接使用 Fixture 检索源（`routing.py:79,95-96`），其中包含样例 Knowledge。Step 3 必须校验 `retriever_name` / `fallback_reason`，不能只看最终回答。静态证据：`routing.py:63-109,141-155`。 |
| 7 | Policy 区分 NOT_QUERIED / EMPTY / SUCCESS / DEGRADED | **PASS** | 枚举定义见 `agent/app/retriever/base.py:45-60`；未发起的检索保持 `NOT_RETRIEVED/NOT_QUERIED`（`runtime.py:827`）；Knowledge 最终状态区分 `SUCCESS_PARTIAL / SUCCESS_BUT_DROPPED`（`builder.py:2038-2065`）。 |
| 8 | ContextBuilder 记录注入/排除/截断/省略 | **PASS** | `INJECTION_SELECTED / TRUNCATED / OMITTED / EXCLUDED`，含 reason、estimated_tokens、per_type budget（`builder.py:1176-1260`）。 |
| 9 | Manifest 与 AgentRun 绑定 | **PASS** | Agent `done.contextManifest` → Spring `HttpPythonAgentGateway` 解析（`:299,317-329`）→ `ChatService.finishRun` 写 `agent_run.context_manifest`（`ChatService.java:560-571`）；Schema 为 JSON 列（`schema.sql:573`）；只读查询接口存在。 |
| 10 | Citation 传播/持久化 | **PASS** | `done.citations` → `StreamCompletion` → `CitationMetadata` → `message.citations`（`ChatService.java:324-343`；`MessageService.java:213-225`）；`GET /messages` 计算 `available`（`MessageService.java:62-66`）。 |
| 11 | AgentRun 记录 model/context/result | **PARTIAL** | context 有 `context_manifest`；result 有 `output_summary` + `assistant_message_id`；**model 与 token usage 没有列**（`schema.sql:562-585`；`AgentRun.java` 无对应字段）。`trace_id` 只用于跨进程关联，Agent trace 不落 MySQL。 |
| 12 | 隔离的真实集成测试环境 | **PARTIAL** | 项目已有大量 `ails_*_it` 隔离库与 env-gated 测试；phase54 用 `ails_phase54_rag/mq`；但没有 Phase 5 专用命名空间。本次临时库/collection 已验证可隔离并清理。 |

## 7. 历史真实模型测试的适用边界与配置漂移

### 7.1 各阶段实际验证了什么

| 文档 | 真实依赖 | 验证内容 | 不能证明什么 |
|---|---|---|---|
| `phase1-results.md` | 曾尝试真实 LLM；B08/D08/E07 BLOCKED（HTTP 200 空流） | 基础行为/边界 | 空流原因未定位；不代表 LLM 不可用 |
| `phase2-results.md` | 真实 MySQL/RabbitMQ/Qdrant | 后端 208 测试、集成路径 | 真实 LLM 仍空流；不是 Agent 端到端 |
| `phase2.1-integration/security` | SSE、MQ、安全 | 传输、鉴权、幂等（有 NOT_IMPLEMENTED） | 不覆盖模型质量；不覆盖 D4 上下文 |
| `phase3.1-results.md` | 10 次真实模型调用，9 PASS/1 FAIL | 模型级行为；人工复核 0/9 | 不是 D4 策略/Manifest 验证 |
| `phase53-real-model` | 真实 `deepseek-flash`，`max_tokens=4096` | A/B/C/C-only/D 行为；截断与 history_guard 发现 | 早于 D4 上下文改造；不能证明当前 Manifest/Policy |
| `phase54-real-model` | 真实 Qdrant + `text-embedding-v4` 1024 + `deepseek-flash` | RAG E2E 28/0、MQ 19/0、并发 | 使用隔离 collection/库；早于 D4 改造；不能证明当前默认配置 |
| `phase5-budget-validation.md` | 离线/构造测试（非真实 Provider 连通性） | 27 条查询、46 次运行；预算裁剪下 `SUCCESS_PARTIAL / SUCCESS_BUT_DROPPED` 与引用边界 | 它不是 Provider 连通性验证；不能替代本轮 Step 2，也不能证明当前 Qdrant collection / Tavily / MySQL 可用 |
| `D4-5_CONTEXT_QUALITY_EVALUATION.md` | 离线、无真实依赖 | 21/21 上下文质量 | 不能证明任何 Provider 连通性 |

### 7.2 漂移点

| 漂移点 | 历史测试 | 当前配置 | 影响 |
|---|---|---|---|
| Qdrant collection | phase54 使用 `ails_phase54_text-embedding-v4_1024` | 默认前缀 `ails_documents` → `ails_documents_text-embedding-v4_1024`（当前不存在） | 当前 RAG 知识路径会空/DEGRADED；phase54 PASS 不能直接复用 |
| MySQL 库 | phase54 使用 `ails_phase54_rag` / `ails_phase54_mq` | Spring 默认 `ails` | Step 3 必须显式使用隔离库，否则会写生产数据 |
| 内部凭据 | phase54 脚本自行注入 | 当前 `agent/.env` 无 `AILS_INTERNAL_TOKEN`；运行中的 Agent 对当前 `.env` 凭据返回 401 | E2E 需要先对齐并重启，不能在当前状态直接跑 |
| LLM `max_tokens` | phase53/54 文档记录 4096 | `.env` 当前也是 4096 | **无漂移**（修正此前“缺失”的静态推断）；但本次低预算探针复现了空流，说明该值不能随意调低 |
| LLM 模型 / Embedding 模型 | `deepseek-flash` / `text-embedding-v4` 1024 | 相同 | 模型层面可复用历史结论，但仍需在新上下文链路复验 |
| Tavily | 历史从未集成 | 仍未集成 | 没有历史 PASS 可继承 |

### 7.3 关于 “HTTP 200 空流” 的新证据

[已复现] 本次 LLM 探针 #1 使用 `max_tokens=16`：

- HTTP 200
- `finish_reason=length`
- `completion_tokens=16`，其中 `reasoning_tokens=16`
- 内容 0 字符
- `emptyReason=reasoning_only`

探针 #2 使用当前生效的 `max_tokens=4096`：

- HTTP 200
- `finish_reason=stop`
- `completion_tokens=25`，其中 `reasoning_tokens=23`
- 内容“好”，1 字符

**结论：** 至少对当前 `deepseek-flash` 配置，Phase 1/2 的“HTTP 200 空流”很可能不是服务不可用，而是 **reasoning token 吃掉了 completion 预算**。这是“根因候选”，不是对所有历史环境的最终定论；Step 3 必须记录 `finish_reason` 与 usage，并把 `max_tokens` 固定在不低于 4096。

## 8. 阻塞项分级

### P0（不解决就不能声称 Phase 5 连通）

1. **Tavily 未集成**：无代码/依赖/配置/工具抽象。若 Phase 5 要求联网检索，此项直接 BLOCKED。
2. **当前知识 collection 不存在**：`ails_documents_text-embedding-v4_1024` 不在 Qdrant 列表中；当前知识检索会返回 DEGRADED/空，不能拿 phase54 的隔离 collection PASS 代替。
3. **Spring ↔ Agent 内部凭据不一致**：[已复现] 用当前 `agent/.env` 调用 `GET /health/diagnostics` 返回 401；说明运行中的 Agent 持有另一份内部 token。当前状态下 Spring → Agent 的受保护接口无法通过，E2E 不可执行。

### P1（影响证据完整性/可解释性）

4. **AgentRun 缺 model/token usage 字段**：无法从数据库回答“这次用了哪个模型、多少 token、是否截断”。Step 3 只能依赖 SSE/日志/manifest。
5. **低 `max_tokens` 导致 reasoning-only 空流**：必须在 Step 3 固定预算并记录 usage。
6. **phase53/54 的 PASS 早于 D4**：D4 Policy/ContextBuilder/Manifest/Citation 在真实模型下的组合未被历史测试覆盖；D4-5 只是离线评测。

### P2（治理/可重复性）

7. 没有 Phase 5 专用 Qdrant 前缀/MySQL 库命名空间；目前靠脚本临时创建与清理。
8. 人工盲评历史为 0/9；Step 3 若需要质量结论，必须组织盲评，不能只看自动断言。
9. `application-local.yml` 含本机 MySQL 凭据但未被 Git 跟踪（`backend/backend/.gitignore:9`）。不是仓库泄漏，但属于本机配置卫生风险；本文档不回显该值。
10. Agent 诊断接口必须带内部 token（`internal_auth.py:35-45`），没有匿名只读诊断；这是安全设计，但增加了预检门槛。

## 9. 本次审计未做的事

- 未修改任何生产代码、配置、测试、Policy、ContextBuilder、Manifest。
- 未进入 Step 3 的 A/B/C Pilot。
- 未创建 Phase 5 数据集。
- 未向生产 `ails` 库或任何生产 Qdrant collection 写入数据。
- 未调用 Tavily（因为没有集成点）。
- 未 commit、未 push。
