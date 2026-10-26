# 阶段 4.4：非成功检索状态的可靠性保护

日期：2026-10-26。本阶段没有调整 Prompt 规则、`top_k` / `min_score` / Token 预算，
没有引入 Reranker、Hybrid Search、Self-RAG、微调或向量库迁移，也没有修改
RabbitMQ、analyzer、Evidence、LearningUnderstanding、Memory 的核心逻辑。

## 1. 现有状态处理机制的审查结论

### 状态从哪里来

`agent/app/retriever/base.py:37` 定义了六种状态：

| 状态 | 产生条件（`agent/app/retriever/knowledge.py`） |
| --- | --- |
| `SUCCESS` | 有命中且至少一条达到阈值 |
| `NO_VALID_DOCUMENTS` | 白名单为空：空间为空或没有 READY 文档 |
| `NO_MATCH` | 检索返回空命中 |
| `THRESHOLD_FILTERED` | 有候选，但全部低于 `min_score` |
| `DEGRADED` | 超时、依赖不可用、返回格式异常 |
| `NOT_RETRIEVED` | 评测 plain 模式，根本没有检索 |

### 状态怎么流动

`AgentRuntime._retrieve`（`agent/app/runtime.py:385`）把状态写进
`AgentState.knowledge_status`，并记录到 trace 的 `RETRIEVE` 事件。
`ContextBuilder.build` 在知识片段之前插入一个 `knowledge_status` 段
（`agent/app/context/builder.py:57`、`:298`），`_KNOWLEDGE_STATUS_NOTES`
（`:75`）已经为四种非成功状态写了不同措辞，明确要求：

- `NO_VALID_DOCUMENTS`：不能声称“资料中不存在”，不能编造引用；
- `NO_MATCH` / `THRESHOLD_FILTERED`：没检索到不等于不存在，通用知识要标明；
- `DEGRADED`：不能判断资料是否包含答案。

### 缺什么

状态**已经正确传播**，但只进入了模型可见的 system instruction 和 trace，
**没有任何运行时决策**：

1. 模型可以完全忽略 `knowledge_status` 段，直接给出完整答案；
2. `validate_citations` 在生成之后才运行，且只写 trace，不改变回答；
3. 因此 D01/F02/S04 这类“问题本身可用通用知识回答”的场景，最终回答可以
   一个字都不提本轮没有资料依据。

所以本阶段补的不是“再写一遍 Prompt”，而是一个**确定性的运行时兜底**。

### 来源顺序与冲突

现有 Context Builder 的顺序是固定优先级：
Current Input 100 > History 90 > Session 85 > Understanding 70 > Memory 50 >
Knowledge Status 45 > Knowledge 40 > Evidence 20。多个知识片段按检索顺序拼接，
**没有**显式的“多来源冲突 / 相似主题 / Memory 与当前资料冲突”处理规则。
本阶段没有新增这类优先级规则；模型仍然自己决定如何取舍。

## 2. 具体实现及影响范围

### 新增 `agent/app/evidence_guard.py`

- `GUARD_STATUSES`：只包含 `NO_VALID_DOCUMENTS` / `NO_MATCH` /
  `THRESHOLD_FILTERED` / `DEGRADED`。`SUCCESS` 与 `NOT_RETRIEVED` 不处理。
- `_STATUS_NOTICE`：四种状态各一条固定说明。每条都以“本轮”限定，并明确
  “以上回答不基于上传资料”；`DEGRADED` 的措辞是“无法确认上传资料是否包含答案”，
  不推断资料不存在。
- `already_discloses`：用“否定词 + 对象”的模式判断模型是否已经自己说明，
  避免重复追加。
- `is_knowledge_request`：窄口径的问题形态判断，包含“解释 / 定义 / 协议 / 配置 /
  是什么 / 哪些 / 多少”等；**故意不包含**“怎么 / 为什么 / 如何”，并用
  `_CASUAL_RE` 排除寒暄、感谢、确认、继续。

### 修改 `agent/app/runtime.py:220`

`generate()` 在模型增量全部产出之后、`done` 之前执行：

1. 状态不在 `GUARD_STATUSES` → 直接返回；
2. 有知识片段 → 不追加（兜底只处理“没有可用资料”）；
3. 问题不像知识请求，或模型已经说明 → 不追加；
4. 否则追加 `\n\n{notice}`，作为一个普通 delta 产出，并写入 trace
   `EVIDENCE_GUARD`（状态、是否追加、是否有知识片段）。

`stream()` 与评测 runner 都走 `generate()`，因此行为一致。`done` 的
`responseChars`、`elapsedMs` 与引用校验都包含这条说明。

### 修改 `agent/app/trace.py:33`

新增 `EVIDENCE_GUARD` 事件类型，只记录，不改变 SSE 事件类型。

### 新增测试 `agent/tests/test_evidence_guard.py`

覆盖：四种状态的追加说明、普通聊天不追加、已有说明不重复、`SUCCESS` 不变、
SSE 序列仍为 `meta → delta… → done`、trace detail、问题形态启发式。

### 没有改动

- `prompts.py`、检索参数、Context Builder 的段落与预算；
- `app.citations` 的校验规则；
- SSE 事件类型、顺序与 Spring 契约；
- 任何整段缓冲、引用改写、回答拦截。

### 影响范围与限制

- 正常 RAG（`SUCCESS`）完全不变；普通短寒暄不变；
- 非成功状态下，回答末尾会多一条约 30–45 字的说明；
- **无法可靠区分“依赖上传资料的问题”和“普通通用知识问题”**。当前只按问题
  形态判断：带“解释 / 是什么 / 哪些”等标记的问题会被加说明；不带标记的
  （例如“谈谈渐进自由”）不会加。这是有意保留的限制，没有扩大成状态门禁；
- 说明只覆盖“本轮没有资料依据”，不能删除模型正文里已经写下的归因；
  如果模型同时编造了引用，引用层仍会失败（见第 7 节）。

## 3. D01、F02、S04、F04 修复前后

### 真实定向运行（qwen-plus，v2 规则）

报告：`evaluation/rag/reports/real_20261026_phase44_guard_targeted/`

| 用例 | 模式 | 状态 | 兜底是否追加 | 结果 | 引用 |
| --- | --- | --- | --- | --- | --- |
| D01 | rag | `THRESHOLD_FILTERED` | 否（模型自己说明） | 通过 | 0/0 |
| D01 | rag+state | `THRESHOLD_FILTERED` | 是 | 通过 | 0/0 |
| F02 | rag | `THRESHOLD_FILTERED` | 是 | 通过 | 0/0 |
| F02 | rag+state | `THRESHOLD_FILTERED` | 是 | 通过 | 0/0 |
| F04 | rag | `DEGRADED` | 是 | 通过 | 0/0 |
| F04 | rag+state | `DEGRADED` | 是 | 通过 | 0/0 |

6/6 自动通过。需要如实说明：D01/rag 这次是模型自己先写了“上传资料中没有检索到”，
兜底按设计跳过（trace `added=false`），因此这一条不是兜底改写的；其余 5 条
都追加了说明。F04 的 `DEGRADED` 说明是“知识检索不可用，无法确认上传资料是否包含答案”。

### S04（真实 Spring SSE）

报告：`evaluation/rag/reports/real_20261026_phase44_guard_sse/`

| 时间点 | 结果 | 失败检查 |
| --- | --- | --- |
| 4.3 的两次 S04 基线 | 失败 | `admits_missing_document` |
| 4.4 本次 | **通过** | 无 |

事件序列：`message.start` → 227 个 `message.delta` → `message.end`；
`hasError=false`，无伪造引用。末尾是
“（本轮检索到的文档片段相关度不足，没有检索到可确认的资料依据；以上回答不基于上传资料。）”。

### 离线模拟（同一批已保存回答，不调用模型）

报告：`evaluation/rag/reports/real_20261026_phase44_guard_offline/guard_offline.json`

对 `real_20261026_v2_offline` 的 118 行回答套用同一兜底规则后：

| 模式 | 模拟前 | 模拟后 |
| --- | ---: | ---: |
| plain | 11/37 | 11/37 |
| rag | 30/40 | **34/40** |
| rag+state | 31/41 | **32/41** |

- 10 行答案层由失败变为通过（D01/F01/F02/F03/F04 各两种模式）；
- 其中 5 行自动通过：D01/rag、F01/rag、F02/rag、F02/rag+state、F04/rag；
- 另外 5 行（D01/rag+state、F01/rag+state、F03 两种模式、F04/rag+state）
  答案层已修好，但引用层仍失败：旧回答里有裸标签、非法标签或格式错误标签。
  这说明兜底只解决“不提资料状态”，**不解决引用伪造**。

真实定向运行 6/6 与离线模拟 5/10 的差异来自新一轮模型输出：本轮 6 条都没有
产生引用标签（0/0），所以引用层没有失败；离线模拟用的是 4.2 那一批旧回答。
两者不能混为“兜底修好了引用”。

## 4. 真实模型调用次数、延迟与失败

| 项目 | 数量 |
| --- | ---: |
| LLM completion（runner 8 + SSE 1） | 9 |
| Embedding 查询（runner 5 + SSE 1） | 6 |
| 失败 / 超时 / error 事件 | 0 |

- 模型：`qwen-plus`，temperature=0.0，max_tokens=1024；
- Embedding：`text-embedding-v4`，1024 维；
- runner 8 次平均延迟：**14958.7 ms**；
- 定向 6 次（D01/F02/F04）平均延迟：**21036.7 ms**；
- 对照 2 次（F01 空空间、A01 成功检索）平均延迟：**6724.0 ms**；
- S04 SSE 的报告没有记录单次总延迟，按事件数 229 记录，未估算。

## 5. Agent / Spring 回归

### Agent

`python -m pytest -q`（`agent/`）：

```text
200 passed, 5 skipped, 1 warning in 5.71s
```

跳过项（均为环境变量门禁，跳过不等于通过）：

- `test_knowledge_e2e.py`：`AILS_KNOWLEDGE_E2E`
- `test_qdrant_integration.py`：2 项，`AILS_QDRANT_IT`
- `test_worker_broker.py`：2 项，`AILS_TEST_RABBITMQ_URL`

### Spring

`mvn test`（`backend/backend`）：

```text
Tests run: 180, Failures: 0, Errors: 0, Skipped: 29
BUILD SUCCESS
```

跳过的 29 项全部是需要外部数据库 / 完整 Spring 上下文的集成测试类：
`BackendApplicationTests`(1)、`SecurityContextIntegrationTest`(11)、
`DocumentIndexE2EIntegrationTest`(1)、`DocumentIndexServiceIntegrationTest`(9)、
`DocumentServiceIntegrationTest`(7)。未新增跳过。

### 对照验证

- F01（空空间，`NO_VALID_DOCUMENTS`）：通过，trace `added=false`（模型自己说明）；
- A01（`SUCCESS`）：通过，无 `EVIDENCE_GUARD` 事件，回答未被追加任何内容；
- 用户 / 空间隔离：定向运行与对照运行使用不同的测试用户与空间，未复用上一批；
- SSE 契约：S04 的 `start → delta… → end` 与无 error 均通过。

## 6. v1 基线差异结论

详见 `evaluation/rag/reports/baseline_audit_phase44/README.md`。

- `real_20261026_full` 与 `real_20261026_full_revalidated` 是**同一批 118 条
  模型输出**，答案逐行相同，指纹 `9fac8dea…f7e285`；
- 31/40 是 4.2 运行当时旧校验器写下的 rag 结果；
- 29/40 是用当前 `app.citations` 对同一批答案重算的结果，可复现；
- 差异只涉及 A12、C01、C05 三条 rag 用例的裸标签 `[#1]` / `[#5]` / `[#4]`；
  A12 答案层本来就失败，所以自动通过只差 C01、C05 两条；
- **29/40 是当前代码下的 v1 口径，31/40 是历史快照**。在统一校验器版本之前，
  不能用这两个数字计算模型质量提升。

## 7. 未解决风险与复现命令

### 未解决风险

1. **引用伪造没有被兜底修复**。旧回答在 `rag+state` 下仍出现
   `[资料《粒子物理导论》#7.4]`、`[#5]`、`[#4]` 等标签，导致引用层失败。
   本阶段按要求没有做引用规范化、静默改写或整段拦截；这些失败保留。
2. **说明与正文可能冲突**。如果模型正文已经用 `[资料《…》#n]` 把通用知识归因给
   上传文档，末尾说明只能声明“以上回答不基于上传资料”，无法删除该归因。
3. **问题形态启发式有边界**。带“解释 / 是什么 / 哪些 / 协议 / 配置”等标记的问题
   会被加说明；不带标记的请求（“谈谈渐进自由”）不会。寒暄、感谢、继续不打扰。
   这不是语义分类，无法保证覆盖所有“依赖上传资料”的问题。
4. **既有说明可能误判**。`already_discloses` 是“否定 + 对象”的正则；如果模型
   在回答另一个子问题时说了“资料中没有提到 X”，可能被当成已说明而跳过兜底。
5. **兜底只在本轮没有知识片段时触发**。若未来出现“非成功状态但仍带片段”的
   实现，当前条件会跳过；这是有意的保守边界。

### 复现命令

```bash
# Agent 全量测试
cd agent && python -m pytest -q

# 离线估算兜底影响（不调用模型、不改源报告）
cd ..
python -m evaluation.rag.guard_offline \
  --source evaluation/rag/reports/real_20261026_v2_offline \
  --target evaluation/rag/reports/real_20261026_phase44_guard_offline

# 真实定向用例（需要测试 Spring + 真实 Agent；Key 只从 agent/.env 注入）
python "$HOME/AppData/Local/Temp/ails_phase42/run_eval.py" \
  --backend-url http://127.0.0.1:18080 --rules-version v2 \
  --cases D01,F02,F04 --modes rag,rag+state \
  --output-dir evaluation/rag/reports/real_20261026_phase44_guard_targeted

# 空空间与成功检索对照
python "$HOME/AppData/Local/Temp/ails_phase42/run_eval.py" \
  --backend-url http://127.0.0.1:18080 --rules-version v2 \
  --cases F01,A01 --modes rag \
  --output-dir evaluation/rag/reports/real_20261026_phase44_guard_controls

# 真实 Spring SSE
python -m evaluation.rag.spring_sse --backend-url http://127.0.0.1:18080 \
  --embedding-model text-embedding-v4 --cases S04 \
  --output-dir evaluation/rag/reports/real_20261026_phase44_guard_sse

# Spring 回归
cd backend/backend && mvn test
```

真实运行前需要测试 Spring（18080）、真实 Agent（18100）与测试 Qdrant；
`run_eval.py` 从本地 gitignored `agent/.env` 读取 Key，不打印、不写入报告。
