# AI Learning Space — Learning Agent

Python 侧的 Learning Agent Runtime，**最小可用版本**。

它负责一件事：**在回答用户之前，先把「这个用户现在是怎么理解这个知识点的」读进来，
并据此决定从哪里讲起。**

```text
ChatRequest
     ↓
概念识别 → 检索（Understanding / Memory / Knowledge，按需 Evidence）
     ↓
Context Builder（排序、去重、控 Token、组织结构）
     ↓
Tutor → LLM Adapter
     ↓
SSE 流式回答
```

下游的**异步学习分析**（Evidence 抽取 → Understanding 更新 → Memory 收敛）**不在本轮范围**，
目前仍由后端 `LearningAnalysisService` 的规则版分析器（`rules-v1`）顶着。

- 各层职责与数据契约：[docs/pipeline.md](docs/pipeline.md)
- 完成进度、边界与下一步：[docs/progress.md](docs/progress.md)

---

## 一、它在整个系统里的位置

```text
Flutter
   │  REST / SSE
   ↓
Spring Boot  ────────────────  MySQL
   │  HTTP（本轮新增的一跳）
   ↓
Python Learning Agent
   │  HTTP 只读回调（Understanding / Memory / Evidence）
   └──────────────→ Spring Boot
```

边界只有两条，都不许绕：

| 原则                                           | 落地方式                                        |
| ---------------------------------------------- | ----------------------------------------------- |
| 业务数据由 Backend 管理，认知判断由 Agent 管理 | Agent **不连数据库**，读写都走后端接口          |
| Agent 不拥有 `DROP` / 删除能力                 | 本轮 Agent **一个写接口都没有**，全部是只读检索 |

第二条不是偷懒，是刻意的：先证明「检索 → 组装 → 教学」这一段真的有用，
再谈让 Agent 写回证据。写回一旦接上，链路会变长、出错面会变大，
所以把验证顺序倒过来做风险更低。

---

## 二、快速开始

```bash
cd agent
pip install -r requirements-dev.txt      # 运行时装 requirements.txt 即可
cp .env.example .env                     # 可选：不建也能跑，默认全离线
```

### 跑一次完整流水线（不起服务、不开后端）

```bash
python -m app.cli "TLB Miss 和 Page Fault 到底是什么关系？"
```

默认是 **Mock LLM + Fixture 数据**，所以不需要 API Key、不需要 MySQL、不需要登录。
Mock LLM 会把上下文里的认知背景回读出来，于是你能一眼看出这次到底检索到了什么：

```text
[meta] 命中概念：['TLB（快表）', '缺页异常'] ｜ 上下文约 1288 tokens
...
- 已经理解：TLB 是页表项的缓存；TLB Hit 与 TLB Miss 的区别；TLB Miss 之后需要继续查 Page Table
- [当前存在] 把 TLB Miss 与 Page Fault 混为一谈，认为 TLB 里没有就等于缺页
- 还不确定（本次可以重点补）：Page Table 本身存放在哪里
```

### 只看检索与组装（不调用模型）

```bash
python -m app.cli --preview "为什么你认为我理解了 TLB？"
```

输出每一段上下文的来源、优先级、Token 估算、是否被裁剪 —— 排查「Agent 到底有没有
召回用户的历史理解」时，这比读最终回答快得多。

### 起 HTTP 服务

```bash
python -m app.main          # 或 uvicorn app.main:app --port 8000
curl -s http://127.0.0.1:8000/health
```

### 接真实后端数据

后端的读接口按登录用户鉴权，所以要把用户自己的 JWT 透传进来：

```bash
TOKEN=$(curl -s -X POST http://localhost:8080/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"******"}' | python -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")

python -m app.cli --token "$TOKEN" --space 1 "那 TLB Miss 呢？"
```

### 接真实模型

```bash
# .env
AILS_LLM_PROVIDER=openai
AILS_LLM_BASE_URL=https://api.deepseek.com/v1   # 任何 OpenAI 兼容服务都可以
AILS_LLM_API_KEY=sk-xxx
AILS_LLM_MODEL=deepseek-chat
```

配了 `openai` 但没填 Key 会**启动即报错**，不会静默退回 Mock ——
否则你会以为模型在工作，实际看的是假输出。

### 测试

```bash
python -m pytest -q          # 29 个用例，0.4 秒，不需要数据库与网络
```

---

## 三、目录结构

```text
agent/
├── app/
│   ├── main.py                       HTTP 入口：/agent/chat、/agent/context/preview、/health
│   ├── cli.py                        命令行入口：不起服务跑一次流水线
│   ├── runtime.py                    ★ AgentRuntime：把各层串成一次运行
│   ├── config.py                     配置（环境变量前缀 AILS_）
│   ├── schemas.py                    ★ 与后端对齐的数据契约（唯一的数据边界）
│   ├── prompts.py                    教学契约（System Instruction）
│   ├── trace.py                      结构化运行轨迹（不存思维链）
│   │
│   ├── state/agent_state.py          ★ AgentState：运行时状态，用完即弃
│   │
│   ├── llm/                          ★ LLM Adapter
│   │   ├── base.py                   协议：stream_chat / complete
│   │   ├── openai_compatible.py      DeepSeek / OpenAI / Qwen / vLLM 通用实现
│   │   ├── mock.py                   确定性假输出，离线跑通链路 & 测试断言用
│   │   └── factory.py                按配置装配，配置不全直接报错
│   │
│   ├── retriever/                    ★ Retriever
│   │   ├── base.py                   协议：concepts / understandings / memories / knowledge / evidences
│   │   ├── concept_detector.py       概念识别（规则版，含名称别名与历史回看）
│   │   ├── http_backend.py           调后端只读接口（真实跨对话数据）
│   │   ├── fixture.py                读本地样例数据（离线兜底）
│   │   └── routing.py                选择与降级策略
│   │
│   ├── context/                      ★ Context Builder
│   │   ├── builder.py                去重 / 优先级 / Token 预算 / 区块渲染
│   │   └── tokens.py                 Token 估算与截断
│   │
│   └── tutor/tutor.py                ★ Tutor：守住教学契约与流式链路
│
├── fixtures/demo_space.json          样例认知数据（README 里那个 TLB 场景）
├── tests/                            29 个用例：概念识别 / 上下文组装 / 端到端
├── docs/pipeline.md                  各层职责与数据契约
├── docs/progress.md                  进度、边界与下一步
├── requirements.txt / requirements-dev.txt
├── pyproject.toml                    pytest 与 ruff 配置
└── .env.example
```

（★ 是这条链路上的六个核心组件，与 `agent-architecture.md` 的划分一一对应。）

---

## 四、一次请求发生了什么

以「那它没命中的时候呢？」为例（上一轮在聊 TLB）：

```text
1. AgentState 初始化          请求 → 运行时状态（run_id / user / space / message / history）
2. 概念识别                   本轮没提任何概念名 → 回看历史 → 命中 TLB
3. 检索                       并行取 Understanding、Memory、Knowledge；
                              Evidence 只在用户质疑判断时才取（本轮不取）
4. Context Builder            认知背景 → system message；
                              历史 + 本轮输入 → 对话消息；超预算时从低优先级开始丢
5. Tutor                      校验消息结构 → 交给 LLM Adapter
6. LLM Adapter                流式产出增量文本
7. SSE                        meta → delta × N → done
```

一个关键设计：**本轮输入永不从上下文里丢弃**。模型看不到问题，这次回答就没有意义，
所以即使它单独超出预算，也只截断而不丢。

---

## 五、四个关键设计决定

### 1. 为什么 LLM Adapter 这么薄

`agent-architecture.md` 开篇就写着 `LLM ≠ Agent`。适配器只认 messages 与 token 参数，
不认识 Understanding、Memory，也不认识 Tutor。所以：

- 换模型厂商，只改 `config.py` 的三个配置项；
- 换业务逻辑，不动适配器。

它同时提供 `stream_chat`（Tutor 用）与 `complete`（将来的 Evidence 抽取 / 简答判分用）。
`complete` 本轮**没有调用方**，留着是因为抽取是下一个明确要做的动作，先占住协议而已。

### 2. 为什么 AgentState 用完即弃

`AgentState` 刻意不落库、不跨请求复用：一次请求构造一个，结束就丢。

跨对话的连续性**全部来自后端数据**，不来自进程内缓存。否则同一个用户开两个对话时，
两个进程会各自看到不同的世界；多实例部署后问题会更难查。
这就是 `agent-architecture.md` 第三十九节的落地方式 ——
thread-scoped state 在内存里，跨 thread 的信息在 store（这里是后端数据库）里。

### 3. 为什么有 `/agent/context/preview`

这个项目最难排查的问题不是「模型答得不好」，而是「Agent 到底有没有召回用户的历史理解」。
如果只能从最终回答去猜，每次都得花掉一次模型调用，还不一定看得准。

preview 只检索与组装、**不调用模型**，把每一段的来源、优先级、Token、裁剪情况摊开。
它用的是和 `/agent/chat` 完全相同的 `runtime.prepare()`，所以两条路不可能跑偏。

### 4. 为什么默认跑在 Fixture 上

不是「假装后端已经跑起来了」，而是三个具体理由：

1. **离线可跑**：没有 MySQL、没有登录态时整条链路仍能演示与单测；
2. **可回归**：测试需要固定输入，而真实数据库是会变的；
3. **是文档**：`fixtures/demo_space.json` 用 README 那个 TLB 场景，
   把「Understanding / Memory / Knowledge 长什么样」写成了可执行的例子。

Fixture 与 HTTP 用的是**同一套 Pydantic 模型**，所以切换不需要改上层任何代码。

---

## 六、接口契约

### `POST /agent/chat`

请求体与后端 `ChatAgentRequest` 对齐（camelCase），另加一个 `authToken`：

```json
{
  "runId": 1024,
  "userId": 1,
  "conversationId": 7,
  "learningSpaceId": 3,
  "userMessage": "那 TLB Miss 呢？",
  "history": [{"role": "USER", "content": "TLB 是什么？"}],
  "authToken": "eyJhbGciOi..."
}
```

响应是 `text/event-stream`。**事件名与对前端的 `message.*` 刻意不同名** ——
这是 Backend 与 Agent 之间的内网一跳，同名会让排查时分不清一条事件是谁发的：

```text
meta    { runId, detectedConcepts, estimatedTokens, warnings }
delta   { delta }
done    { runId, detectedConcepts, estimatedTokens, elapsedMs }
error   { runId, code, message }     code: 500 内部错误 / 502 模型上游失败 / 503 后端检索不可用
```

`error` 里的 `code` 是 int，直接喂给后端的 `AgentStreamHandler.onError(int, String)` ——
网关在这一跳上不要再自己编一个常量，否则「谁的错」就丢了。

### `POST /agent/context/preview`

同样的请求体，返回检索与组装结果（不调用模型）：

```json
{
  "code": 0,
  "data": {
    "summary": { "detectedConcepts": ["TLB（快表）"], "retrieved": {"understandings": 1, "...": 0} },
    "retriever": "fixture:demo_space.json",
    "degraded": false,
    "context": { "estimatedTokens": 1298, "droppedSections": [], "sections": [], "messages": [] },
    "trace": { "events": [] }
  }
}
```

---

## 七、当前状态

**已完成**：这条同步链路是通的 —— LLM Adapter、AgentState、Retriever（含真实后端读接口）、
Context Builder、Tutor，以及 29 个不依赖数据库与网络的测试。

**明确没做**（`AgentState.pending_tasks` 里如实标着）：

| 项目                                        | 现状                                     | 计划阶段 |
| ------------------------------------------- | ---------------------------------------- | -------- |
| Evidence 抽取                               | 无。后端 `rules-v1` 规则版顶着           | M2/M3    |
| Understanding 更新、Revision、LearningState | 无，同上                                 | M3       |
| Memory 写入（`createCandidate`）            | 无调用方                                 | M3       |
| 语义概念识别                                | 规则版匹配名称                           | M2       |
| 向量检索 / RAG                              | 无。`get_knowledge` 走 HTTP 时固定返回空 | M6       |
| 主动验证（Evidence Gap → Test）             | 无                                       | M5       |
| 异步分析链路（HTTP 或 RabbitMQ）            | 无                                       | M2/M7    |

细节与优先级见 [docs/progress.md](docs/progress.md)。
