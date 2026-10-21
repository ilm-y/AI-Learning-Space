# Learning Agent — 进度、边界与下一步

> 最后更新：2026-10-21
> 当前目标（已完成）：在同步对话链路之上接上 `POST /agent/analyze`，
> 让 Agent 产出学习分析候选，由后端事务落库

---

## 一、一句话状态

**两条链路都通了，而且可验证。**

- `POST /agent/chat`：识别概念 → 召回跨对话认知 → 组装上下文 → 流式教学回答；
- `POST /agent/analyze`：同一套概念识别 + 只读检索 → 结构化分析 → Evidence /
  Understanding Proposal / Memory / Verification 候选。

42 个测试在 0.6 秒内跑完，不需要数据库、网络或 API Key。真实链路的端到端行为
由 `evaluation/` 的 offline + live 用例覆盖（当前 39 + 9 全绿）。

**边界仍然是「Agent 不写库」**：analyze 只返回候选，Evidence 去重、Understanding
合并、Memory 生命周期、LearningState 投影全部由后端事务完成。Agent 可以重跑、
模型可以出错，数据库不会因此产生重复或越权数据。

---

## 二、已完成

| 组件 | 文件 | 说明 |
| --- | --- | --- |
| LLM Adapter | `app/llm/` | 协议 + OpenAI 兼容实现 + Mock；配置不全时启动即报错，不静默降级 |
| Agent State | `app/state/agent_state.py` | 对齐架构文档第三十八节；刻意不落库、不跨请求复用 |
| Retriever | `app/retriever/` | 概念识别（规则版）+ HTTP / Fixture 两个实现 + auto 降级 |
| Context Builder | `app/context/` | 去重、优先级、Token 预算、区块渲染、裁剪留痕 |
| Tutor | `app/tutor/` | 教学契约 + 消息结构校验 + 流式兜底 |
| Learning Analyzer | `app/analysis/` | 结构化输出 → 校验 / conceptId 收敛 / 去重 / 裁剪 / 验证策略 |
| 编排 | `app/runtime.py` | `AgentRuntime`，无状态；chat / analyze / preview 共用检索与概念识别 |
| 入口 | `app/main.py`、`app/cli.py` | HTTP（chat SSE、analyze、preview、health）与命令行 |
| 观测 | `app/trace.py` | 结构化事件轨迹，不存思维链 |
| 样例数据 | `fixtures/demo_space.json` | README 里的 TLB 场景，可执行的文档 |
| 测试 | `tests/` | 42 个用例，见下表 |

### 测试覆盖

| 文件 | 用例数 | 钉住的行为 |
| --- | ---: | --- |
| `test_concept_detector.py` | 10 | 命中与不命中的边界、词边界、历史回看、用户轮优先于 AI 轮 |
| `test_context_builder.py` | 14 | messages 结构、误区渲染、空快照不注入、预算裁剪顺序、本轮输入永不丢弃 |
| `test_analysis.py` | 9 | 启发式分析、结构化输出解析、坏 JSON / 非法枚举、分数裁剪、conceptId 校验 |
| `test_pipeline.py` | 9 | 端到端：历史理解真的进了 prompt、CANDIDATE 不注入、Evidence 按需、错误路径 |

```bash
cd agent && python -m pytest -q
# 42 passed in 0.59s
```

### 已实际验证的场景

- `python -m app.cli "TLB Miss 和 Page Fault 到底是什么关系？"` —— 命中两个概念，
  把「已理解 / 当前误区 / 还不确定 / 用户自己的说法 / 长期偏好」都带进了回答；
- `python -m app.cli --preview "为什么你认为我理解了 TLB？"` —— Evidence 按需召回生效（2 条）；
- `uvicorn app.main:app` + curl —— `/health`、`/agent/context/preview`、
  `/agent/chat`（SSE）、`/agent/analyze` 四个接口都通，camelCase 与 snake_case
  请求体都能解析；
- `python -m evaluation --mode all` —— 39 条离线用例（分析器契约 / 去重 / 作用域 /
  提案收敛 / 验证策略）+ 9 条在线用例（真后端 + 真 Agent + MySQL）全绿。

---

## 三、明确没做

| 项目 | 现状 | 计划阶段 |
| --- | --- | --- |
| 语义概念识别 | 规则版匹配名称 + 别名表，会漏 | M2 |
| 向量检索 / RAG | `get_knowledge` 走 HTTP 时固定返回空（后端文档接口 501） | M6 |
| 验证任务的执行闭环 | Agent 已给出 `ASK_NOW / ASK_LATER / SKIP`，测试任务由后端创建；作答判分仍是后端启发式 | M5 |
| 异步分析链路 | 无队列。后端在 SSE 关闭后用线程池同步调用 analyze | M7 |
| Agent 运行落库到 `agent_run` | 分析结果由后端写入 `agent_run`；Agent 自己的轨迹只在进程内与日志里 | M2 |
| 用户的批注 / 测试回答进入本轮上下文 | 批注会触发独立的分析；analyze 的输入仍只有 Message 历史与本轮消息 | M3 |
| 多轮工具调用（Agent Tools） | 无。chat 与 analyze 都是线性流水线，不是模型决定检索什么 | 见下 |

### 关于「Agent Tools」

架构文档第三十三节列了一组 tools（`retrieve_*` / `create_evidence` / `update_understanding` …）。
本轮**没有实现 tool 调用循环** —— 检索是写死的三步，不是模型决定的。
这是有意的第一版选择：线性流水线可预测、可测试、延迟低，
而「让模型自己决定检索什么」的价值要等工具集（尤其是写工具）齐了才体现得出来。

`retrieve_*` 这组读工具其实已经以 `Retriever` 协议的形式存在了，
把它们包装成 LLM 可调用的 tool 是后面可以加的，不需要改检索实现。

---

## 四、与后端的契约（已落地）

### 1. 两个网关，两条链路

```text
ChatController → ChatService → AgentGateway ─┬→ StubAgentGateway（本地桩）
                                             └→ HttpPythonAgentGateway（POST /agent/chat）

ChatService（SSE 关闭后）→ TurnAnalysisService
    → HttpPythonAgentAnalysisGateway（POST /agent/analyze）
    → AgentAnalysisWriteService（事务写入）
```

网关切换由 `ails.agent.gateway` 决定（`stub` / `http`），Controller 与 Service 不变。

### 2. 用户 JWT 必须透传

HTTP 检索要读后端的读接口，而这些接口按登录用户鉴权，所以必须把用户自己的 JWT
一路透传下来：

```text
JwtAuthenticationFilter
    └─ AuthUser(id, username, token)             ← token 存进 SecurityContext
ChatController.sendMessage
    └─ SecurityUtil.currentToken()
ChatService.stream(..., authToken)
    └─ ChatAgentRequest(..., authToken)          ← 请求线程取好再带下去
HttpPythonAgentGateway
    └─ POST /agent/chat，authToken 放进请求体
```

两个容易踩的点：

- **token 必须在请求线程上取。** SSE 的生成跑在 `chat-stream-*` 线程池线程上，
  那里没有 SecurityContext（不属于任何请求），`SecurityUtil` 会直接抛 401。
- **`authToken` 是不可省的字段。** 缺了它 `auto` 模式下会静默退回 Fixture ——
  检索看起来接好了，实际一次都没跑。`http` 模式则会直接报错，这是有意为之。

### 3. 事件翻译

| Agent 事件 | 后端动作 |
| --- | --- |
| `meta` | 无对应回调，网关自己用（记录命中概念、上下文规模） |
| `delta` | `handler.onDelta(delta)` |
| `done` | `handler.onComplete()` |
| `error` | `handler.onError(code, message)` —— code 直接取事件里的 `code` |

`error` 事件带的是 **int 类型的 `code`**（500 内部错误 / 502 模型上游失败 /
503 后端检索不可用），正好对得上 `AgentStreamHandler.onError(int, String)` 与
前端 `message.error { runId, code, message }` 的 `code`。网关不要自己编常量。

### 4. Analyze 的落库语义在后端

Agent 只返回候选；`AgentAnalysisWriteService` 负责：

```text
Evidence Candidate  → dedupe_key upsert（append-only，数据库唯一约束兜底）
Understanding Proposal → 合并到快照 + 内容有变化才写 Revision
Memory Candidate     → CANDIDATE 状态 + scope/memory_key 冲突检查
VerificationDecision → ASK_NOW 才创建 TestTask
无提案但有证据       → markExposed（UNKNOWN → EXPOSED）
```

这条分工的回归用例在 `evaluation/cases/`，`BC-001` 记录了「只有证据、没有提案」
这一格曾经漏掉的历史。

---

## 五、建议的下一步顺序

1. **换真实模型跑一轮** —— Mock 只能证明链路通，证明不了回答质量与分析准确度。
   配 `AILS_LLM_PROVIDER=openai` 跑几个真实场景，先看 chat / analysis 两份 prompt；
2. **概念识别换成 LLM** —— 规则版会漏（例如用户说「地址翻译快表」时不一定命中 TLB），
   这是一次 `complete()` 结构化输出调用，签名不变；
3. **扩充评测集** —— 当前 48 条用例覆盖六类回归；下一步补真实模型输出的回放样本，
   让 prompt 改动能被量化比较；
4. **验证任务闭环** —— Agent 已能给出 ASK_NOW / ASK_LATER / SKIP，
   下一步是把作答结果作为 `TEST_RESULT` 证据重新喂回分析；
5. **异步化** —— 当前 analyze 在 SSE 关闭后的线程池里同步执行，
   量上来以后换成队列（RabbitMQ / Redis Stream）。

---

## 六、已知取舍与技术债

| 项 | 说明 |
| --- | --- |
| **概念识别是规则版** | 按名称与别名匹配，会漏召回。宁可漏也不误召回（与后端同一条原则） |
| **Token 估算不精确** | 中文 1 字 ≈ 1 token，非中文 4 字符 ≈ 1 token。刻意不引 tiktoken：各厂商分词器不同，引一个会让估算对别的模型不准 |
| **Evidence 触发是关键词** | 「为什么」「依据」这类词命中才召回。语义判断属于模型该干的事 |
| **Mock LLM 靠解析系统提示生成摘要** | 它只是显影液，不是能力。真实模型不依赖这段逻辑 |
| **Mock 走启发式分析** | `llm_provider=mock` 时不调用模型，直接跑确定性规则；这是为了让写入链路可测，不代表分析质量 |
| **`KnowledgeChunk.concept_id` 是 Agent 侧补的字段** | `document_chunk` 表里没有这一列，第一版靠它做筛选；接向量检索后由相似度与 metadata 取代 |
| **Agent HTTP 服务本身不鉴权** | 它是内网服务，默认绑 `127.0.0.1`。**对外暴露前必须加鉴权**，否则任何人都能用任意 `userId` 读别人的学习数据 |
| **没有超时与重试策略** | LLM 有 60s 超时，后端检索有 10s 超时；但没有重试。SSE 链路上一旦开始推流，重试也不现实；analyze 由后端按次调用，失败即回退 `rules-v1` |
| **分析结果整份校验** | 枚举值或结构不合法时整份结果作废（HTTP 502），而不是逐条丢弃 —— 逐条丢弃会静默掩盖模型输出格式的漂移 |
