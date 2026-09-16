# Learning Agent — 进度、边界与下一步

> 最后更新：2026-09-13
> 本轮目标（已完成）：打通 `Python → LLM Adapter → Agent State → Retriever → Context Builder → Tutor`

---

## 一、一句话状态

**同步对话链路已经通了，而且可验证。** 一次请求能完成「识别概念 → 召回跨对话认知 →
组装上下文 → 流式教学回答」，29 个测试在 0.4 秒内跑完，不需要数据库、网络或 API Key。

**下游的异步学习分析整段没做。** Evidence 抽取、Understanding 更新、Memory 写入
目前仍由后端的规则版分析器（`rules-v1`）承担 —— Agent 本轮**没有任何写接口**。

这条边界是刻意的：先证明「检索 → 组装 → 教学」这一段真的比不带背景的回答好，
再谈让 Agent 写回状态。写回一旦接上，链路会变长、出错面会变大。

---

## 二、已完成

| 组件 | 文件 | 说明 |
| --- | --- | --- |
| LLM Adapter | `app/llm/` | 协议 + OpenAI 兼容实现 + Mock；配置不全时启动即报错，不静默降级 |
| Agent State | `app/state/agent_state.py` | 对齐架构文档第三十八节；刻意不落库、不跨请求复用 |
| Retriever | `app/retriever/` | 概念识别（规则版）+ HTTP / Fixture 两个实现 + auto 降级 |
| Context Builder | `app/context/` | 去重、优先级、Token 预算、区块渲染、裁剪留痕 |
| Tutor | `app/tutor/` | 教学契约 + 消息结构校验 + 流式兜底 |
| 编排 | `app/runtime.py` | `AgentRuntime`，无状态，chat 与 preview 共用同一条 `prepare()` |
| 入口 | `app/main.py`、`app/cli.py` | HTTP（含 SSE）与命令行两条入口 |
| 观测 | `app/trace.py` | 结构化事件轨迹，不存思维链 |
| 样例数据 | `fixtures/demo_space.json` | README 里的 TLB 场景，可执行的文档 |
| 测试 | `tests/` | 29 个用例，见下表 |

### 代码量

```text
app/    ~2587 行（含 docstring 与注释）
tests/   ~474 行
```

### 测试覆盖

| 文件 | 用例数 | 钉住的行为 |
| --- | ---: | --- |
| `test_concept_detector.py` | 10 | 命中与不命中的边界、词边界、历史回看、用户轮优先于 AI 轮 |
| `test_context_builder.py` | 12 | messages 结构、误区渲染、空快照不注入、预算裁剪顺序、本轮输入永不丢弃 |
| `test_pipeline.py` | 7 | 端到端：历史理解真的进了 prompt、CANDIDATE 不注入、Evidence 按需、错误路径 |

```bash
cd agent && python -m pytest -q
# 29 passed in 0.35s
```

### 已实际验证的场景

- `python -m app.cli "TLB Miss 和 Page Fault 到底是什么关系？"` —— 命中两个概念，
  把「已理解 / 当前误区 / 还不确定 / 用户自己的说法 / 长期偏好」都带进了回答；
- `python -m app.cli --preview "为什么你认为我理解了 TLB？"` —— Evidence 按需召回生效（2 条）；
- `uvicorn app.main:app` + curl —— `/health`、`/agent/context/preview`、
  `/agent/chat`（SSE）三个接口都通，camelCase 与 snake_case 请求体都能解析。

---

## 三、明确没做

| 项目 | 现状 | 计划阶段 |
| --- | --- | --- |
| Evidence 抽取 | 无。后端 `rules-v1` 规则版顶着 | M2 / M3 |
| Understanding 更新 + Revision | 无，同上 | M3 |
| LearningState 投影 | 无，后端已有 | M3 |
| Memory 写入（`createCandidate`） | 后端接口已就绪，**无调用方** | M3 |
| 语义概念识别 | 规则版匹配名称 + 别名表，会漏 | M2 |
| 向量检索 / RAG | `get_knowledge` 走 HTTP 时固定返回空（后端文档接口 501） | M6 |
| 主动验证（Evidence Gap → Verification Policy → Test） | 无 | M5 |
| 异步分析链路 | 无（HTTP 或 RabbitMQ） | M2 / M7 |
| Agent 运行落库到 `agent_run` | 无。轨迹只在进程内与日志里 | M2 |
| 用户的批注 / 测试回答进入本轮上下文 | 无。只读了 Message 历史 | M3 |
| 多轮工具调用（Agent Tools） | 无。本轮没有 tool 循环，是一次线性流水线 | 见下 |

### 关于「Agent Tools」

架构文档第三十三节列了一组 tools（`retrieve_*` / `create_evidence` / `update_understanding` …）。
本轮**没有实现 tool 调用循环** —— 检索是写死的三步，不是模型决定的。
这是有意的第一版选择：线性流水线可预测、可测试、延迟低，
而「让模型自己决定检索什么」的价值要等工具集（尤其是写工具）齐了才体现得出来。

`retrieve_*` 这组读工具其实已经以 `Retriever` 协议的形式存在了，
把它们包装成 LLM 可调用的 tool 是后面可以加的，不需要改检索实现。

---

## 四、接后端还需要什么

### 1. 把用户 JWT 透传给 Agent（后端侧已做）

HTTP 检索要读后端的读接口，而这些接口按登录用户鉴权，所以必须把用户自己的 JWT
一路透传下来。**后端侧已经接完**，链路是：

```text
JwtAuthenticationFilter
    └─ AuthUser(id, username, token)             ← token 存进 SecurityContext
ChatController.sendMessage
    └─ SecurityUtil.currentToken()
ChatService.stream(userId, conversationId, content, authToken)
    └─ new ChatAgentRequest(..., authToken)      ← 请求线程取好再带下去
HttpPythonAgentGateway implements AgentGateway
    └─ POST /agent/chat，把 authToken 放进请求体   ← 只剩这一步
```

两个容易踩的点：

- **token 必须在请求线程上取。** SSE 的生成跑在 `chat-stream-*` 线程池线程上，
  那里没有 SecurityContext（不属于任何请求），`SecurityUtil` 会直接抛 401。
- **`ChatAgentRequest.authToken` 是不可省的字段**，不是可选的优化。缺了它
  `auto` 模式下会静默退回 Fixture —— 检索看起来接好了，实际一次都没跑，
  而且日志里只有一条 INFO。`http` 模式则会直接报错，这是有意为之。

在 `HttpPythonAgentGateway` 落地之前，这一跳仍然断着，所以线上跑的依然是 Fixture。

### 2. 用 `HttpPythonAgentGateway` 替换 `StubAgentGateway`

后端 `agent/` 包已经为此留好了边界，`ChatController` 与 `ChatService` 一行都不用改：

```text
ChatController → ChatService → AgentGateway ─┬→ StubAgentGateway       （现在）
                                             └→ HttpPythonAgentGateway  （接入）
```

新实现需要做的事件翻译（内网一跳 → 对前端的一跳）：

| Agent 事件 | 后端动作 |
| --- | --- |
| `meta` | 无对应回调，网关自己用（记录命中概念、上下文规模） |
| `delta` | `handler.onDelta(delta)` |
| `done` | `handler.onComplete()` |
| `error` | `handler.onError(code, message)` —— code 直接取事件里的 `code` |

`error` 事件带的是 **int 类型的 `code`**（`runtime.py` 里的 `_ERROR_CODE_*`：
500 内部错误 / 502 模型上游失败 / 503 后端检索不可用），
正好对得上 `AgentStreamHandler.onError(int, String)` 与前端契约里
`message.error { runId, code, message }` 的 `code`。

网关不要在这一跳上自己编一个常量 code —— 那会把「谁的错」这个信息丢掉，
前端的 `message.error` 就只剩一句无法分类的文本了。

### 3. 让 Agent 接管抽取（下一步之后）

做完上面两步，用户拿到的回答就已经是基于历史认知的了，但学习闭环仍由后端规则版跑。
要让 Agent 接管，需要补：

- Agent 侧：`POST /agent/analyze`，用 `llm.complete()` 产出结构化 Evidence JSON；
- 后端侧：内部写入接口（当前 `learning_evidence` **没有任何 REST 写入口**，
  这是 `EvidenceService` 的 append-only 设计决定的，需要专门开一条供 Agent 调用的路径）；
- 幂等：沿用后端已有的 `agent_run_id + concept_id + observation_hash` 去重键。

---

## 五、建议的下一步顺序

1. **接 `HttpPythonAgentGateway` + token 透传** —— 最小改动，把「跨对话个性化」
   从 Fixture 演示变成真实体验。这一步做完才谈得上验收 PRD 的 Case 2。
2. **换真实模型跑一轮** —— Mock 只能证明链路通，证明不了回答质量。
   配 `AILS_LLM_PROVIDER=openai` 跑几个真实场景，先看 prompt 需不需要调。
3. **概念识别换成 LLM** —— 规则版会漏（比如用户说「地址翻译快表」时不一定命中 TLB）。
   这是一次 `complete()` 结构化输出调用，签名不变。
4. **Evidence 抽取 + 写回** —— 补 `/agent/analyze` 与后端的内部写入口，完成异步闭环。
5. **建最小评测集** —— 架构文档第四十九节列的 12 个 Case。没有它，
   第 2、3 步的改动只能靠感觉判断好坏。

---

## 六、已知取舍与技术债

| 项 | 说明 |
| --- | --- |
| **概念识别是规则版** | 按名称与别名匹配，会漏召回。宁可漏也不误召回（与后端同一条原则） |
| **Token 估算不精确** | 中文 1 字 ≈ 1 token，非中文 4 字符 ≈ 1 token。刻意不引 tiktoken：各厂商分词器不同，引一个会让估算对别的模型不准 |
| **Evidence 触发是关键词** | 「为什么」「依据」这类词命中才召回。语义判断属于模型该干的事 |
| **Mock LLM 靠解析系统提示生成摘要** | 它只是显影液，不是能力。真实模型不依赖这段逻辑 |
| **`KnowledgeChunk.concept_id` 是 Agent 侧补的字段** | `document_chunk` 表里没有这一列，第一版靠它做筛选；接向量检索后由相似度与 metadata 取代 |
| **Agent HTTP 服务本身不鉴权** | 它是内网服务，默认绑 `127.0.0.1`。**对外暴露前必须加鉴权**，否则任何人都能用任意 `userId` 读别人的学习数据 |
| **没有超时与重试策略** | LLM 有 60s 超时，后端检索有 10s 超时；但没有重试。SSE 链路上一旦开始推流，重试也不现实 |
| **`complete()` 目前无调用方** | 为下一步的抽取预留，已在 README 与本文档标注，不会让人误以为已经用上了 |
| **`AgentState.pending_tasks` 是硬编码的** | 如实反映「闭环未完成」，接上异步分析后应改为由实际执行情况填充 |
