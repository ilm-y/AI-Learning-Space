# 5.3 服务端 E2E：Spring → Agent → 真实模型（2026-10-30）

> **结论先行：** 在独立端口上启动了当前 Spring Boot（8081）和真实模型 Agent（8000），
> 通过公开 API 完成 3 轮对话：普通问题、错误解释、显式复盘。Spring SSE 契约、
> 真实 `deepseek-flash` 生成、消息与 `agent_run` 持久化、inline `POST /agent/analyze`
> 分析、Evidence / Understanding / Revision / LearningState 落库均通过。
>
> 本轮 **47 PASS / 0 FAIL**，三次聊天生成均 `finish_reason=stop`、`truncated=false`，
> 服务端没有 `message.error`。显式复盘读到了 Revision 轨迹，`history_guard` 无 issue、
> 未触发重生成或 fallback。
>
> 这不是完整产品验收：**没有测试 Flutter 前端**，没有 MQ 真实模型链路，没有真实文档
> 检索 / 引用，也没有重复采样、人工评分或并发压测。所有回答都是
> `knowledgeStatus=NO_VALID_DOCUMENTS`。
>
> **后续（5.4，2026-10-30）：** MQ 真实模型、文档引用、重复采样和并发基线已补跑；
> 重复采样还抓到一个 `message.end` 早于 `agent_run` 终态的顺序竞态，修复后连续
> 3 轮 47 / 0。证据见
> [`phase54-real-model-20261030.md`](phase54-real-model-20261030.md)。

---

## 1. 验收范围

| 项目 | 是否覆盖 | 说明 |
| --- | --- | --- |
| Spring 公开 API（注册 / 空间 / 概念 / 会话） | **是** | 新用户、新空间、TLB / Page Table 两个概念 |
| `POST /api/conversations/{id}/messages` SSE | **是** | 3 轮：普通问题、错误解释、显式复盘 |
| Spring → Python Agent `/agent/chat` | **是** | `AILS_AGENT_GATEWAY=http`，后端 8081，Agent 8000 |
| 真实模型生成 | **是** | `deepseek-flash`，`temperature=0.6`，`max_tokens=4096` |
| 助手消息 / `agent_run` 持久化 | **是** | 只读 SQL 核对 `message`、`agent_run` 终态、耗时、`trace_id`、`truncated` |
| inline `POST /agent/analyze` | **是** | 3 轮分析均调用真实模型，分析器返回 `agent-v1` |
| Evidence / Understanding / Revision / LearningState | **是** | 公开 API + 只读 SQL 双向核对 |
| 普通问题 vs 显式复盘的上下文差异 | **是（辅助）** | `/agent/context/preview`：普通问题无 `trajectory:`，显式复盘有 `trajectory:177` |
| Flutter 前端渲染 | **否** | 本次明确搁置；只验证服务端数据契约 |
| MQ 模式 / Worker | **否** | 本轮 `AILS_ANALYSIS_MODE=inline` |
| 文档 RAG / 引用合法性 | **否** | Qdrant 未启动，所有场景 `NO_VALID_DOCUMENTS` |
| 重复采样 / 人工评分 / 并发压测 | **否** | 每个场景 1 轮；判定由执行脚本完成 |

### 1.1 为什么可以认为「前端收到数据后渲染也应该没问题」

本轮验证的是前端依赖的服务端事实：

- SSE 事件名、顺序和字段稳定：`message.start` → 多个 `message.delta` → `message.end`；
- `message.start` 带 `runId` / `conversationId`，`message.end` 带 `messageId` / `runId`；
- `message.delta` 拼接结果与 `GET /messages` 落库内容逐字一致；
- 没有 `message.error`，没有 `truncated` 标记；
- 中文按 UTF-8 正确传输（脚本第一次按 ISO-8859-1 解码时出现乱码，修正客户端解码后一致）。

前端渲染代码仍未验证；如果前端自身有编码、事件解析或状态管理缺陷，本轮不能发现。

---

## 2. 环境与拓扑

| 组件 | 配置 |
| --- | --- |
| MySQL | 本机 8.x，库 `ails`；账号从 gitignored `application-local.yml` 读取，不写入报告 |
| Spring Boot | 当前工作树，端口 **8081**（避开用户原有 8080 服务）；`AILS_AGENT_GATEWAY=http`、`AILS_ANALYSIS_MODE=inline` |
| Python Agent | 当前工作树，端口 **8000**；`AILS_LLM_PROVIDER=openai`、`AILS_LLM_MODEL=deepseek-flash`、`AILS_LLM_MAX_TOKENS=4096` |
| 真实模型 | DeepSeek OpenAI 兼容接口；Key 只在 gitignored `agent/.env` / 进程环境，不打印、不落盘 |
| Qdrant | **未启动**；检索状态均为 `NO_VALID_DOCUMENTS`，不是「检索成功但无引用」 |
| 前端 | 未启动、未修改 |

内部凭据只使用本轮临时值；Agent 只持有 `AILS_INTERNAL_TOKEN` 和
`AILS_INTERNAL_IDENTITY_SECRET`，**不持有** `AILS_INTERNAL_DELEGATION_SECRET`。
后端持有 delegation secret。

---

## 3. 场景与结果

数据由公开 API 创建，SQL 只做只读核对。每次运行使用新用户 / 新空间，避免与历史数据混在一起。

| 轮次 | 用户输入 | HTTP | SSE 事件 | delta 数 | 首 delta | 总耗时 | 回答字数 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 普通问题 | 「TLB 缓存的是什么？请用一两句话说明。」 | 200 | 74 | 72 | 1328 ms | 1828 ms | 123 |
| 2 错误解释 | 「我理解 TLB 就是页表本身，它缓存的是整个页表；所以 TLB miss 就等于缺页中断。」 | 200 | 886 | 884 | 9420 ms | 17686 ms | 1476 |
| 3 显式复盘 | 「我之前对 TLB 的理解错在哪里？请只根据这次学习记录回答。」 | 200 | 11 | 9 | 10672 ms | 10688 ms | 1035 |

事件数 = 1 个 `message.start` + N 个 `message.delta` + 1 个 `message.end`。
第二轮 delta 特别多，是因为模型输出较长（1476 字），Spring 按 Agent 增量逐段转发。
第三轮只有 9 个 delta，但首 delta 在 10.6 秒；这是 `deepseek-flash` 先完成内部推理、
再开始输出可见文本，不是服务端丢包。

### 3.1 持久化与 run 终态

| 轮次 | CHAT run | 状态 | 耗时 | `output_summary` | trace_id（截断） | 助手 message | message.truncated |
| --- | ---: | --- | ---: | --- | --- | ---: | ---: |
| 1 | 536 | COMPLETED | 1812 ms | `chars=123` | `38d6e1df4fbb…` | 415 | 0 |
| 2 | 538 | COMPLETED | 17654 ms | `chars=1476` | `2e0cc18d5e8a…` | 417 | 0 |
| 3 | 541 | COMPLETED | 10661 ms | `chars=1035` | `6c93da277475…` | 419 | 0 |

三次 `message.end` 都没有 `truncated` 字段；数据库 `message.truncated=0`；
Agent 日志三次都是 `finishReason=stop`、`truncated=false`。

### 3.2 inline 分析

| 轮次 | ANALYSIS run | 耗时 | 输出摘要 | 证据 |
| --- | ---: | ---: | --- | --- |
| 1 | 537 | 5327 ms | `evidence=1, proposal=1, memory+=0, memory~=0, test=-` | Evidence 398：`OBSERVATION / UNKNOWN / MESSAGE 414 / agent-v1` |
| 2 | 539 | 6094 ms | `evidence=3, proposal=2, memory+=0, memory~=0, test=-` | Evidence 399、400：`USER_CORRECTION / INCORRECT / MESSAGE 416 / agent-v1` |
| 3 | 542 | 1577 ms | `evidence=0, proposal=0, memory+=0, memory~=0, test=-` | 没有新增 Evidence；复盘问题本身没有新的学习信号，允许 |

第 2 轮另外通过公开批注接口创建了 `HIGHLIGHT` 来源的误区证据 402（`rules-v1`）。
这是产品自有的显式批注入口，不是伪造的 Agent 证据。

最终当前状态：

- `learning_understanding.id=133`，`status=MISCONCEPTION`；
- Revision 1：`EXPOSED`，0 条误区；
- Revision 2：`MISCONCEPTION`，5 条误区；
- Revision 3：`MISCONCEPTION`，6 条误区；
- `learning_state.status=MISCONCEPTION`，`last_evidence_at=2026-10-30 21:11:58`。

### 3.3 显式复盘上下文

通过 `/agent/context/preview` 读取的两组上下文：

| 场景 | sections |
| --- | --- |
| 普通问题 | `user_input`, `understanding:177`, `knowledge_status` |
| 显式复盘 | `user_input`, `understanding:177`, **`trajectory:177`**, `knowledge_status` |

显式复盘 Agent trace 记录：`historyReviewRequested=true`、`trajectoryRevisions=3`、
`trajectoryEvidenceIds=[398,400,402]`、`history_guard issues=[]`、
`regenerated=false`、`fallback=false`。模型回答逐条引用了批注原话与 Revision 版本，
没有泄露内部 Evidence ID。

---

## 4. 第一轮暴露并修复的真实模型契约漂移

第一次正式运行（21:09）并非全绿：错误解释轮的分析 run 531 失败：

```text
AnalysisError: 分析结果不符合契约：
verificationDecision.trigger 输入值是一段自由文本，不在枚举
NEW_CONCEPT / CONCEPT_CONFLICT / REPEATED_ERROR / CONTRADICTORY_EVIDENCE /
OVERCONFIDENCE / LONG_UNVERIFIED 中
```

后端按设计退回 `rules-v1`，所以用户仍拿到了可用证据；但这意味着这一轮
**没有真正完成真实模型分析**，不能算服务端 E2E 通过。

修复方式：

1. `agent/app/analysis/analyzer.py` 增加 `_coerce_verification_enums()`：
   只收敛 `verificationDecision` 的 `action` / `trigger` / `questionType`；
   无法识别的 trigger / questionType 置空，无法识别的 action 降为 `SKIP`。
   证据、理解、记忆等会写进长期状态的枚举仍然严格校验。
2. `agent/app/analysis/prompts.py` 明确列出三个枚举字段的合法值。
3. `agent/tests/test_analysis.py` 增加「trigger 是自由文本时仍保留 Evidence」的契约测试。

修复后重跑：**47 PASS / 0 FAIL**，错误解释轮的分析由 `agent-v1` 完成，
不再退回 `rules-v1`。首轮失败产物保留在
`backend/backend/target/phase53-spring-e2e/20261030-210931/`，没有被删除或改写。

这个问题的价值在于：**后端 fallback 会让链路看起来「能用」，但真实模型契约失败被
降级掩盖了。** 如果没有核对 `analyzer_version` 和分析 run，只看 HTTP 200 会误判通过。

---

## 5. 边界与仍不能声称的内容

可以声称：

- 已完成一次真实 `deepseek-flash` 的 Spring SSE → Agent → LLM 服务端 E2E；
- 3 轮聊天均完成 SSE 契约、消息持久化、`agent_run` 终态与 inline 分析；
- 普通问题与显式复盘的上下文注入符合设计，显式复盘 `history_guard` 无 issue。

不能声称：

- **完整产品验收**：Flutter 未测试；前端渲染、交互、断线重连、移动端行为都未验证；
- **MQ 真实模型链路通过**：本轮只跑 inline；
- **文档 RAG / 引用质量通过**：Qdrant 未启动，全部 `NO_VALID_DOCUMENTS`；
- **真实模型稳定通过**：每个场景只跑 1 次，没有重复采样、温度扫描、人工评分或失败率；
- **并发 / 性能 SLO 通过**：没有压测；
- **每次请求都有完整模型 provenance**：聊天 Agent trace 仍缺少 `LLM_CALL`、
  usage、provider/model 字段；本轮只能靠 Agent 日志、`finishReason`、输出与配置交叉证明；
- **`history_guard` 无误报**：C-only 的否定句误报仍是已知限制，未在本轮修复。

显式复盘轮的分析 `evidence=0` 是允许结果，不是失败：用户只是在询问历史，
本轮没有新的解释、纠错或测试回答。不能用「每轮必须新增 Evidence」作为验收条件。

---

## 6. 复现

### 6.1 启动 Agent（8000，真实模型）

```bash
cd agent
AILS_BACKEND_BASE_URL=http://127.0.0.1:8081 \
AILS_INTERNAL_TOKEN=<与后端一致> \
AILS_INTERNAL_IDENTITY_SECRET=<与后端一致> \
AILS_PORT=8000 \
python -m app.main
```

`agent/.env` 里配置 `AILS_LLM_PROVIDER=openai`、`AILS_LLM_MODEL=deepseek-flash`、
`AILS_LLM_MAX_TOKENS=4096` 和本地 Key。不要设置
`AILS_INTERNAL_DELEGATION_SECRET`；Agent 检测到该变量会拒绝启动。

### 6.2 启动 Spring（8081，避开原有 8080）

```bash
cd backend/backend
SERVER_PORT=8081 \
AILS_AGENT_GATEWAY=http \
AILS_AGENT_BASE_URL=http://127.0.0.1:8000 \
AILS_ANALYSIS_MODE=inline \
AILS_INTERNAL_TOKEN=<与 Agent 一致> \
AILS_INTERNAL_IDENTITY_SECRET=<与 Agent 一致> \
AILS_INTERNAL_DELEGATION_SECRET=<只给后端> \
./mvnw -q -DskipTests spring-boot:run
```

### 6.3 运行 E2E

```bash
cd /path/to/AI-Learning-Space
AILS_INTERNAL_TOKEN=<与后端一致> \
AILS_INTERNAL_IDENTITY_SECRET=<与 Agent 一致> \
python scripts/phase53-spring-e2e.py \
  --base http://127.0.0.1:8081 \
  --agent http://127.0.0.1:8000
```

脚本只通过公开 API 写业务数据；MySQL 只做只读核对。输出目录：

```text
backend/backend/target/phase53-spring-e2e/<stamp>/
├── summary.json
├── normal.sse / normal.json
├── wrong.sse / wrong.json
└── review.sse / review.json
```

---

## 7. 产物索引

正式轮（2026-10-30 21:11:25，47 PASS / 0 FAIL）：

```text
backend/backend/target/phase53-spring-e2e/20261030-211125/
├── summary.json
├── normal.sse / normal.json
├── wrong.sse / wrong.json
└── review.sse / review.json
```

服务日志：

```text
evaluation/reports/phase54-spring-real-20261030/
├── backend.log
├── agent.log
├── e2e.log                 # 首轮（含分析契约失败）
└── e2e-final.log           # 修复后正式轮
```

首轮失败样本：

```text
backend/backend/target/phase53-spring-e2e/20261030-210931/
├── summary.json            # 45 PASS / 3 FAIL（含乱码解码问题与规则回退）
├── normal.sse / normal.json
├── wrong.sse / wrong.json
└── review.sse / review.json
```

相关报告：

- Runtime 级 5 场景复验：[`phase53-real-model-20261030.md`](phase53-real-model-20261030.md)
- 测试矩阵：[`../delivery/TEST_MATRIX.md`](../delivery/TEST_MATRIX.md)
- 已知限制：[`../delivery/KNOWN_LIMITATIONS.md`](../delivery/KNOWN_LIMITATIONS.md)
