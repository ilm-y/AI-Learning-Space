# 可重复演示：TLB 学习状态闭环

这条演示证明的是**现有代码如何把一次错误理解变成可追踪的证据、当前快照和历史轨迹**，
以及**下一次会话怎样区分「历史上错过」和「现在已掌握」**。它不是教学效果验收。

> **LLM 状态必须写明：本演示只使用 Mock LLM。**
> Mock 输出是确定性的，不是真实模型能力。2026-10-30 已完成真实模型 Agent Runtime
> 单轮 5 场景复验，以及 Spring SSE 3 轮服务端 E2E（47/0）；但完整产品验收仍未完成，
> 见 [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) 第 1 节、
> [phase53-real-model-20261030.md](../testing/phase53-real-model-20261030.md) 和
> [phase53-spring-e2e-20261030.md](../testing/phase53-spring-e2e-20261030.md)。
> 不要把本演示的 SSE 回答当成「真实模型已经稳定遵守证据边界」。

## 1. 前置

按 [ENVIRONMENT.md](ENVIRONMENT.md) 启动：

1. MySQL 8 可连；
2. Agent `8000`，`AILS_LLM_PROVIDER=mock`、`AILS_RETRIEVER_MODE=auto`；
3. Spring Boot `8080`（inline 分析），`ails.agent.gateway=http`；
4. 不需要 RabbitMQ、Qdrant、真实 API Key。

先确认：

```bash
curl -s http://127.0.0.1:8000/health
# 存活探针：{"status":"ok"}
curl -s -H "X-Internal-Token: $AILS_INTERNAL_TOKEN" \
  -H "X-Internal-User-Id: 1" \
  http://127.0.0.1:8000/health/diagnostics
# 必须包含 "llm_provider":"mock"

curl -s http://127.0.0.1:8080/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"nobody","password":"wrong"}'
# 返回 JSON（401 也可以），说明后端在监听
```

如果 Agent 不在 `mock` 模式，脚本会直接退出，不会用不稳定的真实输出冒充可重复演示。

## 2. 一条命令运行

```bash
cd /path/to/AI-Learning-Space
bash scripts/phase6-demo.sh
```

成功时最后一行类似：

```text
结果：PASS=23 FAIL=0
产物目录：.../backend/backend/target/phase6-demo/20261027-021939
```

失败时脚本返回非 0，并在对应步骤打印 `FAIL` 与实际值。产物目录保留，便于复查。

### 数据隔离与可重复性

- 每次运行注册**新用户**、创建**新空间**，不复用上一次的数据；
- 所有业务数据都通过公开 HTTP API 产生；
- 直接 SQL 只用于**只读核对**，不手工插入、不手工改状态；
- 不删除任何历史用户 / 空间 / 证据；测试库会累积数据，这是有意的，避免演示脚本
  拥有破坏性权限；
- 固定密码 `Passw0rd!23` 只用于本地测试，不能用于任何真实环境。

## 3. 七个步骤与对应产物

脚本按下面顺序执行。每一步都有实际的 API 结果、SSE、Trace 或数据库记录。

### 步骤 1：用户暴露错误理解

- 输入（记录在 `setup/c-wrong.sse`）：
  > 我理解 TLB 就是页表本身，它缓存的是整个页表；所以 TLB miss 就等于缺页中断。
- 产生：用户消息（原始记录）+ `USER_EXPLANATION` 证据。
- 脚本随后通过公开批注接口标出误区原文：
  `MISCONCEPTION_CANDIDATE` → 当前 `Understanding.status = MISCONCEPTION`。

核对：

```bash
python -m json.tool <产物目录>/setup/summary-clean.json
```

### 步骤 2：Evidence 与来源被记录

`setup/c.evidence.json` 里可以看到：

| Evidence | 类型 / 正确性 | 来源 |
| --- | --- | --- |
| 原始错误解释 | `USER_EXPLANATION` / `PARTIAL` | `MESSAGE` |
| 显式误区批注 | `USER_CORRECTION` / `INCORRECT` | `HIGHLIGHT` |
| 纠正解释 | `USER_EXPLANATION` / `PARTIAL` | `MESSAGE` |
| 纠正批注 | `USER_EXPLANATION` / `CORRECT` | `HIGHLIGHT` |
| 测试回答 | `TEST_RESULT` / `CORRECT` | `TEST_ANSWER` |

关键点：误区不是模型「猜」出来的，而是先有原始消息，再有用户显式批注；
每条 Evidence 都能回跳到一个 `sourceType + sourceId`。

### 步骤 3：Understanding 与 Revision 更新

`setup/c.revisions.json` 应出现类似序列（编号可能不同）：

```text
#1 EXPOSED       误区：[]
#2 MISCONCEPTION 误区：[TLB 就是页表本身…(ACTIVE)]
#3 MISCONCEPTION 误区：[TLB 就是页表本身…(ACTIVE)]
#4 MISCONCEPTION 误区：[TLB 就是页表本身…(PENDING)]
#5 DEMONSTRATED  误区：[]
```

- `learning_understanding` 是**当前快照**，只保存现在仍成立的误区；
- `understanding_revision` 是**不可变历史**，每次真正变化追加一条；
- `trigger_evidence_id` 指向触发变化的那条 Evidence；
- 内容没变时不写 Revision（脚本不断言具体条数，只断言历史里仍有 `ACTIVE`）。

### 步骤 4：纠正后当前状态变化

两条独立正确证据（纠正批注 + 测试回答）后：

- 当前快照 `status = DEMONSTRATED`，`misconceptions = []`；
- 历史 Revision 仍保留当时的 `ACTIVE` 误区；
- `learning_state` 物化投影也是 `DEMONSTRATED`。

「已解除」不是第三个枚举值，而是**从当前快照的误区数组中移出**。
`MisconceptionStatus` 只有 `ACTIVE` / `PENDING`。

### 步骤 5：新会话读取当前理解

脚本创建新会话，用普通问题预览上下文：

> TLB 缓存的是什么？

`normal.preview.json` 的区块应为：

```text
user_input, understanding:150, knowledge_status
```

断言：

- 注入了 `understanding:*`；
- **没有** `trajectory:*`，即不把完整历史轨迹塞进普通问题；
- `systemInstruction` 中包含「状态：已能讲清」。

### 步骤 6：显式复盘读取历史错误与纠正证据

同一空间新建会话，问：

> 我之前对 TLB 的理解错在哪里？后来是怎么纠正的？

`review.preview.json` 的区块变为：

```text
user_input, understanding:150, trajectory:150, knowledge_status
```

断言轨迹区块的 `sourceIds` 含 `revision:*`，证明历史 Revision 确实进入上下文，
而不是只看当前快照。

### 步骤 7：区分历史误区与当前掌握

生产 SSE（`review.sse`）通过 Spring 的公开聊天接口发出，事件契约：

```text
message.start → message.delta × N → message.end
```

本次验证结果（Mock）：

- 首事件 `message.start`，末事件 `message.end`，没有 `message.error`；
- 回答非空，且不包含内部 `#证据ID`；
- 回答第一句是「我只能按当前记录复述可以核对的部分，不推断记录之外的经过。」
  这是 `history_guard` 在 Mock 输出未通过句子级校验后产生的**确定性回退文本**，
  不是真实模型生成。

回答会分开陈述：

- **历史**：按时间列出记录中的错误理解、显式批注、纠正解释、测试回答；
- **当前**：`DEMONSTRATED`，当前快照没有误区；
- **不写**：未记录的心理过程、外部资料使用、改变原因。

## 4. 只读数据库核对

脚本最后直接查库（只读）：

```sql
SELECT status FROM learning_state WHERE user_id=? AND concept_id=?;
SELECT COUNT(*) FROM learning_evidence WHERE user_id=? AND concept_id=?;
SELECT COUNT(*) FROM understanding_revision WHERE understanding_id=?;
SELECT COUNT(*) FROM learning_understanding
 WHERE id=? AND JSON_SEARCH(misconceptions, 'one', 'ACTIVE') IS NOT NULL;
```

期望：

- `learning_state.status = DEMONSTRATED`；
- `learning_understanding` 中没有仍为 `ACTIVE` 的误区；
- Evidence 至少 4 条（原始 / 批注 / 纠正 / 测试）；
- Revision 至少一条历史快照保留 `ACTIVE`。

## 5. 产物阅读顺序

```text
<产物目录>/
├── setup/
│   ├── c-wrong.sse               # 步骤 1 的 SSE
│   ├── c-correct.sse             # 纠正解释的 SSE
│   ├── c.evidence.json           # 步骤 2 证据与来源
│   ├── c.revisions.json          # 步骤 3 历史快照
│   ├── c.understanding.json      # 步骤 4 当前快照
│   ├── c.learning-state.json     # 物化投影
│   └── summary-clean.json        # 本次用户 / 空间 / 概念 ID
├── understanding.json            # 复盘前当前快照
├── evidence.json                 # 复盘前证据列表
├── revisions.json                # 复盘前 Revision
├── learning-state.json           # 复盘前学习状态
├── normal.preview.json           # 步骤 5 普通问题上下文
├── review.preview.json           # 步骤 6 复盘上下文
├── review.sse                    # 步骤 7 生产 SSE 原文
└── review.sse.json               # 事件 / 回答 / 错误的结构化摘要
```

## 6. 这条演示能证明什么、不能证明什么

**能证明：**

- 公开 API 能完成「原始记录 → 批注 → Evidence → Understanding → Revision」；
- 当前快照与历史 Revision 分离，纠正后两者不会互相覆盖；
- 普通问题与显式复盘走不同的上下文注入路径；
- SSE 事件契约在 Mock 下保持 `start → delta → end`；
- `history_guard` 的确定性回退不会泄露内部 Evidence ID。

**不能证明：**

- 真实模型会稳定遵守证据边界（2026-10-30 只做了单轮 5 场景复验 + 3 轮服务端 E2E；
  C-only 依赖 fallback，服务端 E2E 未覆盖 MQ、Flutter、文档引用）；
- 教学效果；
- Flutter 前端可用（仍是脚手架）；
- 测试作答判分是语义判分（当前是启发式）。

真实模型复验的最低要求与失败样本见 [TEST_MATRIX.md](TEST_MATRIX.md) 第 7、8 节和
[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md)。服务端 E2E 证据见
[phase53-spring-e2e-20261030.md](../testing/phase53-spring-e2e-20261030.md)。
