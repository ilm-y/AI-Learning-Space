# 现状审计报告（Phase 0）

- 审计日期：2026-10-27
- 代码版本：`67172e4`（工作区另有 `evaluation/reports/latest.*` 的评测产物变更）
- 审计范围：仓库结构、真实调用链、数据关系、RAG 链路、现有测试与基线、环境依赖、真实/模拟边界
- 审计方式：只读代码与配置 + 实际执行现有测试 + 只读服务健康检查/数据库查询；**未修改业务代码，未提交 Git**

> 本报告只写实际执行或代码中可直接确认的事实。历史文档中的结果会明确标注“本轮未重跑”，
> 不与本轮基线合并。被跳过、被环境门控、未执行的测试一律不计为 PASS。

> **2026-10-30 更新：** 本报告是 2026-10-27 的现状快照。此后已用 `deepseek-flash`
> 完成 5.3 真实模型 Agent Runtime 复验（A/B/C/D `PASS`；C-only 因守卫误判走
> fallback），并另起 Spring 8081 + Agent 8000 完成 3 轮服务端 E2E
> （47 PASS / 0 FAIL，覆盖 SSE、消息与 `agent_run` 落库、inline 分析、复盘轨迹注入）。
> 文中「5.3.2 为 0 次调用」「真实模型证据缺失」「8000 端口是 Mock」等结论只代表
> 审计当时；最新证据见
> [`phase53-real-model-20261030.md`](phase53-real-model-20261030.md) 和
> [`phase53-spring-e2e-20261030.md`](phase53-spring-e2e-20261030.md)。
> 服务端 E2E 只覆盖 inline，未覆盖 MQ、Flutter、文档引用与重复采样。

---

## 1. 结论速览

| 问题 | 当前答案 |
|---|---|
| 当前 8000 端口上的 Agent 是否在调用真实 LLM？ | **否。** `/health` 返回 `llm_provider=mock`、`llm_model=mock`。这与 `agent/.env` 中请求的 `openai` / `deepseek-flash` 不一致，说明运行进程被显式 mock 参数覆盖，或并非由该 `.env` 启动。 |
| 仓库是否具备真实模型调用能力？ | **具备。** 代码支持 OpenAI 兼容协议；本轮已用 curl 验证 DeepSeek `deepseek-flash`、DashScope `text-embedding-v4`、`qwen-plus` 均返回 HTTP 200（密钥未落盘、未写入本报告）。 |
| 现有测试能否证明真实模型质量？ | **审计时不能；2026-10-30 已补跑 5 场景 + 3 轮服务端 E2E。** 默认测试、离线评测、live 评测、E2E 脚本仍使用 Mock LLM 或录制输出；真实模型只有单轮 Runtime 复验和一次 3 轮 inline E2E，不是重复采样或人工评分。 |
| 现有测试能否证明后端链路？ | **部分能，且已有真实模型服务端 E2E。** 2026-10-30 的 Spring 8081 → Agent 8000 → `deepseek-flash` 3 轮 E2E 47/0，验证了 SSE 契约、消息/run 落库、inline 分析与复盘轨迹；Mock 下另有 SSE、持久化、Evidence/Revision、权限隔离、MQ 机制测试。真实 MySQL/RabbitMQ/Qdrant 集成测试仍大量被环境门控跳过。 |
| 是否存在可上简历的指标？ | **有，但必须按层标注。** 例如审计时的 232 个 Agent 测试、189 个 Spring 测试、40/40 离线评测、9/9 live Mock 评测；2026-10-30 复跑 Agent 为 383 passed，并新增真实模型服务端 E2E 47/0。真实模型语义质量仍没有重复采样或人工评分证据。 |

---

## 2. 仓库与技术栈

| 模块 | 路径 | 技术 | 作用 |
|---|---|---|---|
| Spring 后端 | `backend/backend/` | Spring Boot 3、Java 17、MyBatis-Plus、MySQL 8、JWT、SSE、RabbitMQ | 用户/空间/会话/消息、分析结果落库、文档上传与索引编排、SSE 输出 |
| Python Agent | `agent/` | FastAPI、Python 3.10、httpx、pydantic-settings、aio-pika、qdrant-client | 上下文组装、LLM 生成、Evidence/Understanding/Memory 候选、RAG 检索、文档解析/切片/向量化 |
| 评测 | `evaluation/` | Python | 离线 40 例、live 9 例、RAG 41 例、报告生成 |
| 脚本 | `scripts/` | Bash | Phase 5.1/5.2/5.3/6 的 E2E、演示、真实模型数据准备 |
| 文档 | `docs/delivery/` | Markdown | 测试矩阵、环境、生命周期、已知限制 |
| Flutter | 仓库内脚手架 | Flutter | **无业务代码**，本轮不纳入测试 |

服务端口：Spring `8080`（默认 inline 分析）、Agent `8000`、MySQL `3306`、Qdrant `6333`、RabbitMQ `5672`。

---

## 3. 已确认的调用链

### 3.1 聊天 → SSE → 分析 → 状态落库

```text
POST /api/conversations/{id}/messages/stream
  → ChatService.stream()
      校验会话归属
      同步写入 user message
      beginTurn()
      创建 RUNNING agent_run
      创建 SseEmitter + 线程池执行
  → streamReply()
      SSE: message.start
      逐段缓冲 Agent delta
      onComplete:
        持久化 assistant message（失败则不分析）
        SSE: message.end
        agent_run → COMPLETED
        analyzeQuietly() → AnalysisDispatchService
  → inline: TurnAnalysisService
    mq:     RabbitMQ → Agent worker → AnalysisResultConsumer
  → AgentAnalysisWriteService.apply()  @Transactional
      Evidence 追加/去重（dedupe_key 唯一索引）
      Understanding 当前快照
      UnderstandingRevision 仅真实变化时追加
      LearningState 单向投影
      Memory 作用域校验与 reconcile
      Verification
  → 任一步失败：整笔回滚；调用方回退 rules-v1 分析
```

关键事实：

- SSE 先发 `message.start`，再发 delta，最后 `message.end`；失败发 `message.error`。
- SSE 超时 5 分钟；Agent 聊天读超时 240 秒，分析超时 90 秒。聊天线程池 core 16 / max 64 / queue 128。
- **inline 分析复用聊天线程池**：SSE 关闭后，同一个 `chat-stream-*` 线程继续跑最长 90 秒的分析与写库，
  高峰期会挤占聊天容量。
- user message 在 session 解析和 agent_run 创建**之前**就独立提交；若后续步骤失败，会留下
  “有用户消息、没有助手回复、没有 run”的孤儿记录。
- 客户端断开时 `send()` 异常被静默吞掉，但 Agent 线程继续生成，assistant 消息仍会持久化并触发分析。
  没有取消令牌或 `onCompletion` 清理；断连期间上游调用可占用线程直到 240 秒超时。
- 分析写入是**单事务**；失败不会留下半份 Evidence/Revision。
- `analyzeQuietly` 只对已持久化的 assistant 消息触发，异常被吞掉，不影响聊天响应。
- assistant / TEST 消息落库不再复核会话归属或 ACTIVE 状态，只信任调用方。
- CHAT run 用 `RUNNING → COMPLETED/FAILED`；ANALYSIS MQ run 用 `PENDING → RUNNING → SUCCEEDED/FAILED`，
  `STALE` 是“可能孤儿”的非终态。补偿器只扫描 ANALYSIS，CHAT run 没有 stale 恢复。
- `UnderstandingService.persistIfChanged` 先归一化 JSON 再比较全部字段；没有真实变化不写 Revision。
- `markExposed` 只允许 `UNKNOWN → EXPOSED`，不会重复写 Revision。
- `resolveStatus`：存在未解除误区 → `MISCONCEPTION`；`knownParts >= 2` 且 `uncertainParts` 为空 → `DEMONSTRATED`；其余非空 known → `PARTIAL`；此前为 `UNKNOWN/EXPOSED` → `EXPOSED`。

### 3.2 Agent 生成与两道 Guard

```text
AgentRuntime.stream()
  prepare()
    concept 检测
    Understanding / Memory / Evidence / Knowledge 并行取数
    Knowledge 查询与 concept 命中解耦（无概念也查知识库）
    history_review_requested 时才取 Revision + 历史 Evidence 明细
  ContextBuilder.build()
    优先级：user_input 100 > history 90 > session 85 > understanding 70
            > trajectory 68 > memory 50 > knowledge_status 45
            > knowledge 40 > evidence 20
    预算 3000 tokens；从低优先级开始丢弃；user_input 永不丢弃
  generate()
    普通问答：直接流式输出
    显式个人复盘：先缓冲 → History Guard 校验 → 最多重试 1 次
                    → 仍不通过则确定性 fallback
  Evidence Guard
    知识状态属于 NO_VALID_DOCUMENTS / NO_MATCH /
    THRESHOLD_FILTERED / DEGRADED 且回答未披露时，
    追加固定中文状态说明（作为普通 delta）
  validate_citations()
    只记录 trace，不改写、不重试、不拦截
  SSE: done
```

触发词是启发式：`_EVIDENCE_TRIGGERS`（为什么/依据/证据/复盘等）、`_REVIEW_TRIGGERS`（之前/以前/纠正/误解等）；
`_is_history_review` 还要求出现个人标记或错误标记。History Guard 是句级正则校验，不是语义判断。

补充的运行时事实：

- **Agent 的 `/agent/chat`、`/agent/analyze`、`/agent/context/preview`、`/health` 没有鉴权依赖**；
  只有文档 parse/index 端点校验 internal token。若 HTTP 进程配置了 `AILS_INTERNAL_TOKEN`，
  未认证调用方可以用任意 `userId` 且不传 `authToken`，借内部 token 读取该用户的
  concepts/understanding/memory/evidence；`/agent/context/preview` 会直接返回完整上下文。
  本地 `.env` 未设置 internal token，因此当前本地部署先落到 fixture，而不是读取真实用户数据。
- 普通聊天是增量流式；显式复盘会**先缓冲整段生成**，可能重试一次，再用 120 字分块回放，
  首字节延迟等于一整次生成（加一次重试）。
- 没有 heartbeat；首个 SSE 事件在检索完成后才发出；SSE 事件不含 `traceId`、provider、model、usage。
- LLM 适配器不采集 usage；`last_status_code` 是共享字段、调用前不重置，并发下会串；没有任何
  HTTP 端点或 SSE 字段能证明“这一次请求确实到了真实模型”。
- Evidence Guard 在 History Guard **之后**执行；它可以把“无文档”说明追加到确定性历史 fallback 上，
  两条 fallback 文本可能互相矛盾。
- History Guard 的 `_recorded_corpus` 不包含 `state.evidences`；会出现“模型在上下文里看得到证据，
  Guard 却按无历史处理并强制 fallback”的情况；常见复盘措辞“错在哪”也不在 evidence 触发词里。

### 3.3 文档 → RAG → 引用

```text
POST /api/spaces/{spaceId}/documents
  → DocumentService.upload()
      空间归属校验 → 文件校验/10 MiB → 同空间同 contentHash 去重
      → 落盘 → document 行（UPLOADING）→ 异步 submit
  → DocumentIndexService.runIndex()
      Agent /agent/documents/parse（内部 token + userId 一致性）
      → 校验 chunk → 替换 MySQL chunks
      → Agent /agent/documents/index
           embed（批量 10）→ ensure_collection → Qdrant upsert
      → 校验 vector_ref → 写回 document_chunk.vector_ref
      → document → READY
      任一步失败 → FAILED
  → 检索时
      GET /documents/ready-ids?embeddingModel=...
        SQL：owner + space + READY + 未删 + 已索引
             + chunk_count > 0 且 vector_ref 覆盖完整
      → Qdrant filter: user_id + space_id + document_id IN allowlist
      → 取 top_k 后再按 min_score 过滤
      → 无文档 NO_VALID_DOCUMENTS；无命中 NO_MATCH；
        全低于阈值 THRESHOLD_FILTERED；依赖异常 DEGRADED
  → ContextBuilder 渲染为 `- [资料《文件名》#index] 内容`
  → 生成后校验引用标签；非法/捏造只记录，不拦截
```

已确认的权限纵深：MySQL 空间归属 → ready-ids 白名单 → Qdrant payload filter → 返回 chunk 的 `document_id` 再校验。
非 READY、软删、未索引、vector_ref 不完整的文档都会在 ready-ids 层被排除。

---

## 4. 数据语义与关键约束

| 语义 | 表 | 约束 |
|---|---|---|
| 原始记录 | `message` / `highlight` / `test_answer` | 用户内容可软删；已生成 Evidence 不随之删除 |
| 追加事实 | `learning_evidence` + `evidence_source` | append-only；`dedupe_key` 唯一索引 |
| 当前快照 | `learning_understanding` | `(user, concept)` 唯一；只放当前仍成立的误区 |
| 不可变历史 | `understanding_revision` | `(understanding, revision_no)` 唯一；只插入 |
| 物化投影 | `learning_state` | `(user, concept)` 唯一；只复制 status |
| 长期上下文 | `memory` / `memory_source` | `(user, scope_key, memory_key)` 唯一；用 status 管生命周期 |

其他约束：`message(conversation, sequence_no)` 唯一、`document_chunk(document, chunk_index)` 唯一、
`agent_run` 有 RUNNING/COMPLETED/FAILED/SUCCEEDED 状态。当前 schema 共 22 张表。

---

## 5. 本轮实际执行的基线

### 5.1 命令与结果

| 层 | 命令 | 本轮结果 | 性质 |
|---|---|---:|---|
| Agent 测试 | `cd agent && python -m pytest -q` | **232 passed, 5 skipped, 1 warning in 8.08s** | Mock/fixture，默认 CI |
| Spring 测试 | `cd backend/backend && ./mvnw test` | **189 tests, 0 failures, 0 errors, 29 skipped，BUILD SUCCESS，28.174s** | 单元 + Mockito + 少量真实 MySQL（被门控） |
| 离线评测 | `python -m evaluation` | **40/40，0 失败，0 跳过；124/124 断言** | 录制输出 + 启发式，不调真实模型 |
| live 评测 | `python -m evaluation --mode live` | **9/9，0 失败，0 跳过；70/70 断言** | 真实 Spring + MySQL + SSE，但 **Mock LLM** |
| E2E 脚本 | `scripts/phase51/52/51-mq/6-demo` | **本轮未重跑**；文档记录 101/27/94/23 PASS | Mock LLM；历史结果 |
| RAG 真实模型 | `evaluation/rag` | **本轮未重跑**；历史 v2：plain 11/37、rag 30/40、rag+state 31/41 | 真实模型历史产物，不能当本轮结果 |
| 5.3 真实模型（2026-10-27） | `target/phase53-eval/.../REPORT-5.3.1.md` | 历史 8 次调用；A 通过、B PARTIAL、C FAIL、C-only 历史处理通过但技术未通过、D 通过 | 真实模型历史结果 |
| 5.3.2（2026-10-27） | `REPORT-5.3.2.md` | 当时 **0 次真实调用**；只有离线回放 + Mock SSE | 审计时结果 |
| 5.3 复验（2026-10-30） | `docs/testing/phase53-real-model-20261030.md` | 6 次调用 / 5 场景；A/B/C/D PASS，C-only PASS（fallback） | 真实模型单轮复验，不是完整产品验收 |
| Spring 服务端 E2E（2026-10-30） | `scripts/phase53-spring-e2e.py` | **47 PASS / 0 FAIL**；3 轮（普通 / 错误解释 / 显式复盘） | Spring 8081 + Agent 8000 + `deepseek-flash`；只覆盖 inline |
| CI | `.github/workflows/ci.yml` | **本轮未执行** | CI 不含真实 LLM、不含真实 MySQL 集成 |

离线评测日志中的 `analyze run 2/3/4 失败：...` 是 `analysis_contract` 的**负向用例预期输出**，
最终汇总仍为 40/40；不能按“有失败日志”误读为失败。

### 5.2 跳过/未执行清单（不计入 PASS）

| 项目 | 数量/状态 | 原因 |
|---|---|---:|
| Agent 真实 Qdrant 集成 | 2 skipped | 未设 `AILS_QDRANT_IT=true` |
| Agent 真实 broker | 2 skipped | 未设 `AILS_TEST_RABBITMQ_URL` |
| Agent 真实知识 E2E | 1 skipped | 未设 `AILS_KNOWLEDGE_E2E=true` |
| Spring MySQL/文档集成 | 29 skipped | 未设 `AILS_SECURITY_IT` / `AILS_DOCUMENT_IT` / `AILS_DOCUMENT_INDEX_IT` / `AILS_DOCUMENT_E2E_IT` |
| Spring MQ Testcontainers | 10 个用例未执行 | 未跑 `-Pmq-integration` / Docker 门控 |
| `BackendApplicationTests` | 1 disabled | 本地 MySQL 上下文 |
| 真实模型 5.3.2（2026-10-27） | 0 次调用 | 当时缺少 Key；2026-10-30 已补跑真实模型 5 场景 |
| Flutter | 未执行 | 仍是脚手架，无业务代码 |
| 性能/压测 | 未执行 | 仓库无基准 |

---

## 6. 环境与真实模型连通性

### 6.1 服务检查（2026-10-27）

| 服务 | 检查 | 结果 |
|---|---|---|
| MySQL | 端口 3306 | 可连 |
| Qdrant | `GET /healthz` | `healthz check passed` |
| RabbitMQ | 端口 5672 | 可连 |
| Spring | `POST /api/auth/login`（空凭据） | HTTP 401，服务正常；`GET` 返回 500 属未支持方法 |
| Agent | `GET /health` | `status=ok`；**`llm_provider=mock`**；embedding=`openai/text-embedding-v4/1024`；retriever=`auto` |

### 6.2 真实外部模型（已用 curl 验证，密钥未记录）

| 用途 | Provider / Model | 结果 |
|---|---|---|
| 被测模型 | DeepSeek OpenAI 兼容 / `deepseek-flash` | HTTP 200；短 `max_tokens` 下 completion 全为 reasoning token，正文为空；说明可连通且需给足输出预算 |
| 向量 | DashScope OpenAI 兼容 / `text-embedding-v4` | HTTP 200，维度 1024 |
| Judge 候选 | DashScope / `qwen-plus` | HTTP 200，返回 `OK`；与被测模型不同，满足“尽量异模型评审” |
| Judge 备选 | `qwen-turbo` | HTTP 429，不作为默认 Judge |

**结论：真实模型不是 BLOCKED，但当前 8000/8080 上的运行实例是 Mock。**
Phase 3 必须另起真实模型 Agent 实例（独立端口、独立 collection 前缀），不能覆盖用户当前服务。

另一个必须提前说明的限制：**产品接口没有单次请求的模型 provenance。**
聊天 trace 不记录 `LLM_CALL`，SSE 不含 provider/model/usage/status，`/health` 只回配置。
Phase 3 证明“这一次请求到了真实模型”只能使用外部 `RecordingLLMAdapter` 记录
HTTP 状态、延迟、原始输出与 usage，并辅以供应商侧日志；不能仅凭 `/health` 下结论。

### 6.3 历史只读数据快照（上一轮查询，供隔离设计参考）

用户 130、空间 127、概念 160、会话 302、消息 388、Evidence 361、Understanding 121、
Revision 253、LearningState 121、Memory 127、`memory_source` 0、Document 0、DocumentChunk 0、
AgentRun 493（CHAT COMPLETED 194；ANALYSIS COMPLETED 256、SUCCEEDED 10、FAILED 7）。

值得注意但**尚未定性**：

- MySQL 中 Document 为 0，但 Qdrant 仍存在 `ails_phase41/42_...`、`space_isolation_test` 集合 →
  与“无删除/无向量清理路径”的代码事实一致，至少存在历史残留向量。
- `memory` 有 127 行而 `memory_source` 为 0 → 需在 Phase 4 核实是历史数据未回填、某条写入路径漏写，
  还是查询口径问题；在核实前不作为缺陷结论。

---

## 7. 风险与待验证假设

### 7.1 高影响、已由代码确认

1. **审计时真实模型证据缺失。** 运行中的 Agent 是 Mock；README/TEST_MATRIX 中“真实模型”结果全部是历史产物。
   2026-10-30 已补跑 5 场景，但仍不能写“真实 LLM 稳定语义通过率”，只能写“单轮复验”。
2. **引用校验只观测、不拦截。** `validate_citations` 在回答流式输出之后执行，非法/捏造引用不会阻止到达用户；
   生产只留 trace，评测层才把它变成失败。
3. **无文档删除/向量清理路径。** 无 DELETE 端点；`DELETING/DELETED` 未被生产代码设置；
   软删空间不级联 Qdrant。检索靠 ready-ids 白名单遮蔽，一旦白名单回归就会暴露旧内容。
4. **同内容失败文档无法重传恢复。** 去重查询 `findActive` 忽略 status；FAILED 后重传相同字节仍返回旧 FAILED 文档，
   且没有重试端点。
5. **分析写入整笔回滚 + rules-v1 回退。** 任一 Evidence/Revision/Memory 写入失败会丢掉整轮 Agent 分析，
   回退分析可能产生不同状态；目前无“分析写事务中途失败”的集成测试。
6. **客户端断开后仍继续生成与落库。** 单测已确认：断开时 `send()` 异常被吞，assistant 消息仍持久化、
   分析仍触发。未验证上游 HTTP 流是否中止、是否产生无效成本。
7. **History Guard 是句级正则启发式。** 能拦截已知失败样本，但不能证明语义合规；
   5.3.1 的 C 失败样本仍保留。2026-10-30 已真实复验：C 通过，C-only 因
   `no_history_denial` 否定句误报走 fallback，说明校验器仍会误杀正确回答。

### 7.2 高影响、需隔离环境验证的假设

1. **跨环境 Qdrant 点 ID 冲突 → 跨租户覆盖/泄漏。** 点 ID 只由 `document_id:chunk_index` 决定；
   若两套后端数据库共用同一 Qdrant 实例、同一 collection 前缀/模型/维度，自增 document_id 可能碰撞，
   重索引会删除并覆盖另一环境的点（payload 含 user_id/space_id/content）。需要两库共享 Qdrant 的实验确认。
2. **嵌入维度变更不被 ready-ids 发现。** MySQL 只存 `embedding_model`，不存维度/provider；
   同模型名改维度会命中白名单但查不到新 collection，表现为静默 `NO_MATCH`。
3. **同名文件引用标签冲突。** 标签仅 `资料《file_name》#chunk_index`；同空间同名文件会产生相同标签，
   `_allowed_map` 后者覆盖前者，引用可能校验到错误 chunk。
4. **payload 中的 user_id/space_id 未在解析层复核。** 当前只靠 Qdrant filter + document_id 白名单；
   filter 回归不会被解析层兜底。
5. **ready-ids → Qdrant 查询存在 TOCTOU 窗口。** 两次调用之间文档被删/改，仍可能被本次检索命中。
6. **`AILS_RETRIEVER_MODE=auto` 且无 token 时回退 fixture。** 演示知识可能被当成用户资料展示（不涉及真实用户数据泄漏）。
7. **内部 parse/index 端点信任调用方对 space 的所有权。** 只校验 internal token 与 userId 一致，
   不校验 learning_space_id 归属；安全依赖 Spring 是唯一调用方且 token 足够强。

### 7.3 中影响、已确认的检索/工程质量问题

- 先取 top_k 再按 `min_score` 过滤，低排名但相关的片段没有机会进入。
- ready 文档超过 10,000 时 `_normalize_allowed_ids` 返回 `None` → 静默 `DEGRADED`。
- 代码块永不切分；超大 code fence 可超过嵌入模型输入上限并导致整篇索引失败。
- 大文档按 10 条/批顺序嵌入，后端 120s 超时可能不足。
- `KnowledgeSearchService.search` 捕获所有异常返回 `DEGRADED`，编程错误会伪装成依赖故障。
- 默认 `min_score=0.35` 未按模型校准；v2 也未重新调参。
- collection 名对模型名做字符替换并截断，极端情况下不同模型名可能映射到同一 collection。
- 引用支撑度是词面启发式，不是事实核验；不能表述为“事实正确率”。

### 7.4 后端链路专项发现（Spring 深挖）

按 Phase 4 需要直接建测试的优先级排序：

| 级别 | 确认事实 | 影响 | Phase 4 测试重点 |
|---|---|---|---|
| 高 | `MessageRequest` 只有 `@NotBlank`，无长度上限、无限流、无客户端幂等键；`message.content` 为 `LONGTEXT` | 超大输入放大 prompt 成本；重试 POST 会生成重复用户消息与 run | 超长内容边界、重复提交、限流缺失标记 |
| 高 | 用户消息先独立提交，之后才 `beginTurn` / 创建 run | 后续失败留下孤儿用户消息，无助手回复、无 run 追溯 | 注入 session/run 失败，断言最终事务状态 |
| 高 | MQ 先插入 PENDING run 再发布；二者非原子；`OUTCOME_UNKNOWN` 只记录不重试；补偿器只把超时任务标 `STALE`，不重发也不标 FAILED | 进程崩溃/消息丢失后任务永久悬挂 | 模拟发布未知结果、崩溃窗口、补偿器行为 |
| 高 | `markFailed` 仅在 PENDING/RUNNING 上更新，行仍为 RUNNING 时返回 false；调用方随即回退 inline | Worker 仍在处理时又跑一次 inline，两个模型结果写同一 turn | 构造 RUNNING 竞争，断言只产生一份业务数据 |
| 高 | 结果消费者只在 PENDING/STALE 上抢锁；重复结果在首个消费者已置 RUNNING 时仍可进入；`VerificationService` 先查后插且 `test_task` 无“每用户每概念仅一个 PENDING”唯一索引；`markSucceeded` 不检查更新行数 | 并发下可能重复创建 TEST task/message；run 状态可能停在非 SUCCEEDED 而数据已提交 | 并发重复投递、重复 TEST task、最终 run 状态 |
| 高 | FAILED 文档无法恢复：去重不按 status 过滤，同内容重传返回旧 FAILED 行；无 retry/reindex/delete；PROCESSING 卡死无 watchdog | 用户永久卡在坏文档；运维只能改库 | 重传同内容、索引失败后状态、卡死扫描 |
| 高 | 重索引先 `replaceChunks` 删除旧 chunk，再调用 Agent 建新索引 | 新索引失败后旧 chunk 已丢失，文档 FAILED 且不可重试 | 索引失败后的 chunk/vector_ref/状态断言 |
| 高 | Memory update 接受客户端任意 `status`，可从 ARCHIVED 恢复 ACTIVE 或降级为 CANDIDATE | 绕过生命周期与确认流程 | 非法状态迁移应被拒绝（当前会失败） |
| 高 | rules-v1 分析失败时，`@Transactional` 回滚连 FAILED run 也不留；Agent 路径则保留 FAILED run | 两套分析路径的可观测性不一致 | 对比两条路径的失败留痕 |
| 中 | assistant 消息持久化后、`finishRun` 前崩溃会留下 RUNNING；补偿器只扫 ANALYSIS | CHAT run 永久 RUNNING | 断点注入/状态断言 |
| 中 | `fallbackIfNeeded` 捕获 REQUIRED 事务参与者异常，但事务已 rollback-only | 提交时 `UnexpectedRollbackException`，消费者重试直至 DLQ | 回退路径的事务语义测试 |
| 中 | `EvidenceService.listByConcept`、`MemoryService.list/search`、`SessionService.current/list` 只按 `user_id` 过滤，不校验传入的 space/concept 归属 | 不跨用户泄漏，但返回 200 空列表，可探测 ID 存在性 | 跨用户/跨空间 404 vs 空列表一致性 |
| 中 | 内部服务 token 可冒充任意用户；默认值为空；用户 JWT 会转发给 Python Agent | 信任边界集中在共享 token 与 Agent 进程 | 安全配置检查；不写入报告密钥 |
| 低 | SSE 无 `Last-Event-ID`/续传；断线重试会新建用户消息与 run | 客户端重连语义不完整 | 标记 NOT_IMPLEMENTED，不伪造通过 |
| 低 | `EvidenceService.append` 不校验 source 记录存在（多态、无 FK） | 直接调用方可写悬挂来源；现有调用方均传合法 ID | 仅作为边界风险记录 |

文档状态以代码为准：实际只有 `UPLOADING → PROCESSING → READY / FAILED`；
`DELETING/DELETED` 在枚举中存在但无写入路径；`UPLOADED/PARSING/INDEXING` 不存在。

### 7.5 Agent 运行时专项发现（Python 深挖）

| 级别 | 确认事实 | 影响 | 测试/验证重点 |
|---|---|---|---|
| 严重（部署相关） | `/agent/chat`、`/analyze`、`/context/preview`、`/health` 无鉴权；配置了 internal token 时，调用方可传任意 `userId` 且不传 `authToken` 借内部身份读取该用户数据；preview 返回完整上下文 | 若 8000 端口对不可信网络开放，构成越权读取；当前本地未设 internal token，先落 fixture | 带 internal token 启动 Agent，匿名请求任意 userId，断言 401/403（当前会成功） |
| 高 | 聊天 trace 不记录 `LLM_CALL`，SSE 无 provider/model/usage/status；`last_status_code` 共享且不重置；`AnalyzeResult.analyzerVersion` 固定 `agent-v1`，Mock 与真实结构化分析无法区分 | **无法从产品接口证明某次请求到了真实模型**；验收只能靠外部录制适配器 + 供应商日志 | 要求每次真实调用有可归档证据：provider、model、HTTP 状态、延迟、usage、原始输出 |
| 高 | `auto` 模式无 token 时静默回退 `FixtureRetriever`；chat 的 `meta/done` 不暴露 retriever/degraded；`AILS_RETRIEVER_MODE=fixture` 还会绕过 analyze 的 `allow_degradation=False` | 聊天可能完全由演示知识支撑却看起来正常；与 README“analyze 不回退 fixture”矛盾 | 真实模式断言 retriever 非 fixture；在 meta/done 暴露来源 |
| 高 | ContextBuilder 预算只算 section 内容，不算 `BASE_SYSTEM_INSTRUCTION` 和块标题；认知 section 被预算丢弃后会渲染“本次没有检索到任何历史认知记录” | 实际 prompt 可超 3000 token；`estimatedTokens` 低估；模型被错误告知“没有历史” | 测量真实 prompt token；仅在确实无认知数据时出现无历史提示 |
| 高 | History Guard 的 `_recorded_corpus` 不含 `state.evidences`；`build_history_fallback` 只用 trajectory 或无历史 fallback；“错在哪”不在 evidence 触发词中 | 模型看得到证据，Guard 却可能强制“无历史”fallback；合法事实被误杀 | 构造“有 evidence、无 Revision”的复盘，断言最终回答使用证据而非否认历史 |
| 中 | Evidence Guard 在 History Guard 之后，可把“无文档”说明追加到历史 fallback | 两条确定性文本可能矛盾 | 同时触发两个 Guard，断言输出一致性 |
| 中 | `ChatRequest/AnalyzeRequest` 的 ID 没有 `gt=0`；`recent_history_limit=0` 因 `[-0:]` 返回全部历史；多个 LLM/retriever/settings 字段未校验；`extra="ignore"` 静默吞掉拼写错误 | 负 ID 导致 SSE 500/preview 500；0 历史限制反而注入全部历史 | 边界输入与配置校验测试 |
| 中 | analyze prompt 不包含 `history`/`session`；JSON 取首个 `{` 到末个 `}`，无修复/重试；复用 `llm_max_tokens=1024` | 分析缺上下文；长 JSON 被截断 → 502 | 长分析输出、缺字段、历史相关分析 |
| 中 | 复盘/证据触发是子串启发式；无历史确认依赖少量标记词；`Tutor.stream` 接受纯空白输出为非空 | 误触发/漏触发；空白回答被当成功 | 正负样本、空白输出 |
| 中 | 无 heartbeat；首个事件在检索后；HTTP 检索按概念串行；`get_memories` 只查第一个概念；无整体请求超时 | 首字节慢，复盘更慢；多概念时延迟线性增长 | 性能基线、首字节/总时长、多概念 |
| 中 | LLM 适配器无重试/使用量；流式 `choices` 非 dict 会抛 `AttributeError` 变 500；`last_status_code` 并发串扰 | 生产错误分类不稳定；无法核算 token/成本 | 异常响应、并发状态、usage 采集 |
| 低 | FastAPI `/docs` 暴露；无速率限制；启动失败可能泄漏 embedding/Qdrant 客户端；`FallbackRetriever` 只捕获 `RetrievalError`；Mock 回显 system prompt 可能抑制 Evidence Guard | 运维/成本/测试噪声 | 安全配置检查；Mock 测试用 PlainMockAdapter |

### 7.6 测试覆盖缺口（按用户要求的主题）

| 主题 | 已有 | 缺口 |
|---|---|---|
| 跨会话状态演进 | SessionResolver 16、pipeline、trajectory、离线 4、live 2、E2E | 超过两个会话/长时间衰减；失败回滚后状态不变；真实模型行为 |
| 证据约束/不编造 | citations 18、Evidence/History Guard、knowledge retrieval、RAG D 类 | 默认 CI 无真实模型“看似正确但无依据”拒绝测试；有依据却被忽略的 groundedness |
| Memory 冲突 | 离线 5、source 并发 3、live 2、E2E | 同 key 冲突的 DB 级 UPDATE/IGNORE、GLOBAL/SPACE/CONCEPT 优先级、审计轨迹 |
| RAG 权限 | Qdrant 单测、gated 集成 2、ready-ids 集成、E2E | 真实 Qdrant 隔离测试默认跳过；删除/改归属后的陈旧向量；HTTP 层空/缺白名单 |
| 教学质量 | 结构化状态与 RAG 事实/引用 | 无教学评分 rubric、无难度/提示渐进/误解纠正质量测试 |
| 幂等/并发 | worker 16+2、MQ 单测、Evidence/Memory 并发、文档并发上传 | 同一 message/run 的并发分析、Understanding 并发 Revision、MySQL↔Qdrant 部分失败 |
| SSE 断开 | ChatServiceTest 模拟 send/complete 异常、异步鉴权 | 真实网络断开、emitter timeout、上游流中止、断连期间 MQ 分析 |
| 事务回滚 | 文档 chunk 回滚、Evidence/Memory 冲突不整笔回滚 | 分析写事务中途失败；MySQL↔Qdrant 删除/索引补偿 |
| 性能 | 仅记录零散 latency | 无基线、无 p95/吞吐/SLO、无检索/流式延迟基准 |

---

## 8. 可确定性自动化 vs 必须真实模型

### 8.1 可确定性自动化（Mock/fixture/录制输出）

- ContextBuilder 的优先级、预算、截断、去重、状态披露。
- Evidence Guard / History Guard 的触发、重试上限、fallback 文本、SSE 事件契约。
- Evidence dedupe_key、Understanding 状态规则、Revision 只在真实变化时追加、Memory 作用域。
- 文档解析/切片、ready-ids SQL、权限隔离、上传并发、索引幂等、事务回滚。
- MQ 发布/消费/重试/重复投递/DLQ、SSE 完成与失败顺序。
- RAG 的检索状态、上下文注入、引用合法性与词面支撑（离线重打分）。
- API 校验、错误码、跨用户 404/403。

### 8.2 必须真实模型才能验证

- 回答的事实正确性、教学适配、提示渐进、误解纠正质量。
- 是否遵守“不能推断未记录的个人历史/心理过程”。
- 真实模型下的引用合法率、来源支持度、无证据问题的承认率。
- History Guard 在真实输出上的误杀/漏杀。
- Judge 评分与被测模型的一致性（需异模型 + 人工抽检）。

### 8.3 当前无法验证/不应声称

- 生产多租户、多实例共享 Qdrant 的真实隔离性（需专门实验）。
- 真实网络断开、代理超时、慢消费者背压。
- 真实并发压力与性能 SLO（无基线、无压测）。
- Flutter 端行为（无业务代码）。
- 真实用户规模下的数据隐私与成本。
- 5.3.2 的“真实模型已稳定通过”（2026-10-30 只完成单轮 5 场景 + 3 轮 inline 服务端 E2E，
  C-only 依赖 fallback，且没有重复采样或人工评分）。

---

## 9. BLOCKED / NOT_IMPLEMENTED / 未执行

| 项目 | 状态 | 说明 |
|---|---|---|
| 真实模型完整评测 | **部分执行（2026-10-30）** | 5 场景 Agent Runtime 复验 + 3 轮 Spring SSE inline E2E 完成；MQ 端到端、文档引用、Flutter、重复采样未执行 |
| 5.3.2 真实复验 | **已执行（2026-10-30）** | C 通过；C-only 因守卫误判走 fallback；见复验报告 |
| Spring 服务端 E2E | **已执行（2026-10-30）** | 8081 + 8000 + `deepseek-flash`，3 轮 47/0；覆盖 SSE、落库、inline 分析、复盘轨迹；见服务端 E2E 报告 |
| 真实 MySQL/RabbitMQ/Qdrant 集成 | **可解阻，默认跳过** | 本机服务可用；需设置 `AILS_*_IT` 与 `-Pmq-integration` |
| 真实 RAG 全量 | **可解阻，未重跑** | 需要真实 LLM/embedding、Qdrant 独立前缀、重新上传语料 |
| Flutter | **NOT_IMPLEMENTED** | 脚手架，无业务代码 |
| 文档删除/向量清理 | **NOT_IMPLEMENTED** | 无端点、无生产调用 |
| 失败文档重试/重建索引 | **NOT_IMPLEMENTED** | 无重试端点，去重还会挡住同内容重传；无 PROCESSING 卡死 watchdog |
| 消息长度上限/限流/客户端幂等键 | **NOT_IMPLEMENTED** | 仅 `@NotBlank`，字段为 `LONGTEXT`；重试会重复建消息与 run |
| SSE 断线续传（Last-Event-ID） | **NOT_IMPLEMENTED** | 每次请求都新建生成；无回放协议 |
| Memory 状态迁移校验 | **NOT_IMPLEMENTED** | update 接受任意 status，可绕过 ARCHIVED 生命周期 |
| Agent HTTP 端点鉴权 | **NOT_IMPLEMENTED** | chat/analyze/preview/health 无鉴权依赖；仅文档端点校验 internal token |
| 单次请求的真实模型 provenance | **NOT_IMPLEMENTED** | 无 `LLM_CALL` trace、无 usage、SSE 无 provider/model/status；只能靠外部录制适配器与供应商日志 |
| 聊天 retriever/degraded 来源披露 | **NOT_IMPLEMENTED** | 只有 context/preview 暴露；fixture 回退在 chat 中不可见 |
| SSE heartbeat / 服务 readiness | **NOT_IMPLEMENTED** | 首个事件在检索后；`/health` 只回配置，不做依赖探活 |
| 请求 ID 与配置校验 | **部分实现** | ID 无 `gt=0`；`recent_history_limit=0` 等于全部历史；多个 LLM/retriever 字段未校验 |
| 导出/导入/学习计划等 501 能力 | **NOT_IMPLEMENTED** | 见 `docs/delivery/KNOWN_LIMITATIONS.md` |
| 真实网络断开/背压 | **未验证** | 只有单测模拟 |
| 性能基线 | **未验证** | 无压测与阈值 |

---

## 10. Phase 1 计划（下一步）

1. 建立 `docs/testing/agent-evaluation-cases.jsonl`，固定 40 例：
   - A 跨会话学习与状态演进 10
   - B 证据约束与回答可靠性 8
   - C Memory 与显式批注 6
   - D RAG 检索与知识边界 8
   - E 教学任务与对话质量 8
2. 每例字段：`case_id / category / severity / mode / fixtures / steps / expected / forbidden /
   hard_assertions / rubric / cleanup / diagnostics`；成功、边界、失败场景都要有。
3. 测试数据隔离：独立时间戳用户 + 独立空间/概念前缀；数据库只读核对；不删除用户既有数据。
4. 离线确定性 runner 覆盖上下文/Guard/状态规则；真实模型用例单独标记，不与 Mock 合并统计。
5. RAG 8 例中，对有可靠文档级标注的样本计算 Recall@K 与 MRR，并报告样本量；
   不把文档级指标包装成 chunk 级指标。
6. 先写计划与用例，不修改业务逻辑；每个失败保留 `case_id / 原因 / 日志 / 断言`。

---

## 11. 本轮产物与改动

| 类型 | 路径 | 说明 |
|---|---|---|
| 新增 | `docs/testing/current-state-audit.md` | 本报告 |
| 新增 | `evaluation/reports/live-20261027.md/.json` | 归档 live 9/9 结果，避免被后续覆盖 |
| 重新生成 | `evaluation/reports/latest.md/.json` | 已恢复为离线 40/40 报告 |
| 未改动 | 业务代码、schema、依赖、配置 | Phase 0 不做重构 |

Git 未提交；`agent/.env`、`application-local.yml` 等 gitignored 文件中的密钥、密码未写入本报告或任何日志。
