# Agent 回归评测

把「Agent 的行为是否符合契约」变成一组**可重复执行**的用例。它回答两个问题：

1. **离线**：给定一段对话，分析器产出的 Evidence / 提案 / 记忆 / 验证决策对不对？
2. **在线**：这些候选经过后端事务后，数据库里的理解、版本、记忆作用域真的变了吗？

设计取舍写在 `docs/analysis.md` 与各模块的注释里，这里只讲怎么用。

## 快速开始

```bash
# 只跑离线用例（不需要 MySQL / 后端 / API Key）
python -m evaluation

# 跑某一类
python -m evaluation --category verification

# 真后端 + 真 Agent（需要先启动两个服务）
python -m evaluation --mode all \
  --backend http://127.0.0.1:8080 \
  --agent http://127.0.0.1:8000
```

在 `evaluation/` 目录下也可以 `python -m runner`，两种入口等价。

退出码：有失败为 `1`；全部通过或只有 skipped 为 `0`。在线用例在服务没起时标记
`skipped` 而不是 `failed` —— **环境问题不能伪装成行为回归**。

## 目录

```text
cases/
  offline/*.jsonl   进程内用例（真实 Agent 代码 + 固定模型输出 / 规则分析）
  live/*.json       HTTP 用例（真后端 + 真 Agent + MySQL）
datasets/           额外的 fixture（当前复用 agent/fixtures/demo_space.json）
runner/             执行器、断言、报告
metrics/            通过率汇总
reports/            latest.md / latest.json（每次运行覆盖）
bad_cases/          线上问题 → 根因 → 修复 → 回归用例
```

## 两类用例

### offline：测「解析与决策」

| engine | 走哪条路 | 用来证明 |
| --- | --- | --- |
| `heuristic` | `MockLLMAdapter` + `LearningAnalyzer._heuristic_analysis` | 离线规则本身的行为（概念回看、验证策略） |
| `recorded` | `ScriptedLLMAdapter` 返回一段固定文本 | 解析、conceptId 校验、去重、裁剪、枚举收敛 |

`recorded` 用例里的 `llmResponse` **就是模型原始输出**，可以写字符串（带围栏、坏 JSON），
也可以直接写 JSON 对象（runner 会序列化）。被替换的只有「模型说了什么」，
其余全部是生产代码。

### live：测「状态真的变了」

每个用例独立注册用户（用户名带时间戳），建空间 / 概念，然后执行 step：

| action | 说明 |
| --- | --- |
| `sendMessage` | 发一条消息并读完 SSE；返回事件序列、拼接后的回复、`sessionId` |
| `highlight` | 对最近一条用户消息打批注（触发同步分析） |
| `contextPreview` | 调 Agent `/agent/context/preview`，看本轮到底检索到了什么 |
| `waitForUnderstanding` | 轮询理解快照直到条件满足（分析在 SSE 关闭后才跑，存在竞态） |
| `getUnderstanding` / `getRevisions` / `getLearningState` | 查询学习状态 |
| `createMemory` / `searchMemories` | 创建与按可见性检索记忆 |
| `pendingTests` / `answerTest` | 验证任务 |
| `sleep` | 极少数需要等待的场景 |

step 里的值支持变量：`${space.id}`、`${concept.tlb.id}`、`${conversation.a.sessionId}`。

## 断言写法

```json
{
  "expect": {
    "evidence.count": 1,
    "evidence.conceptIds": {"op": "contains", "value": 10},
    "data.status": "PARTIAL",
    "verification.action": "SKIP"
  }
}
```

不写 `op` 时等价于 `eq`。支持：`eq` `ne` `contains` `not_contains` `all` `any`
`subset` `length` `length_gte` `length_lte` `gte` `lte` `truthy` `falsy` `matches`
`startswith` `endswith`。

一个路径只能挂一条断言 —— 需要「既看长度又看结尾」时，用 `_actual` 里的派生字段
（如 `proposal.explanationLength`），而不是在同一个对象里重复写 key。

## 分类与覆盖边界

| 类别 | offline | live | 说明 |
| --- | --- | --- | --- |
| `cross_session` | ✅ | ✅ | 历史概念回看；跨对话 Session 归属与上下文检索 |
| `evidence_dedup` | ✅ | — | 结果内去重 / 配额 / 非法 conceptId。**数据库层 UNIQUE(dedupe_key)** 由 `EvidenceDedupeKeyTest` 与 `scripts/regress-closed-loop.sh` 第 8 步覆盖 |
| `memory_scope` | ✅ | ✅ | 作用域归一化；三类作用域的可见性与跨空间隔离 |
| `understanding_revision` | ✅ | ✅ | 提案收敛；Revision 追加、状态派生 |
| `memory_conflict` | ✅ | ✅ | ADD/UPDATE/IGNORE 语义；身份冲突 409；GLOBAL 与空间记忆并存 |
| `verification` | ✅ | ✅ | ASK_LATER / SKIP / ASK_NOW；首次提问不打断教学 |
| `analysis_contract` | ✅ | — | 坏 JSON、非法枚举、未知字段、越界分数 |

为什么 `evidence_dedup` 没有 live 用例：当前 HTTP 入口每发一条消息就是一个新来源
（`MESSAGE:{id}`），指纹天然不同；真正需要去重的是「同一来源被重跑分析」，
这条路径还没有公开入口。与其在评测里伪造一个入口，不如把这一层交给数据库
唯一索引的单测与回归脚本 —— 那是它真正的边界。

## 指标口径

`reports/latest.md` 里只有三个数：

- **用例通过率**：一条用例全部断言通过才算通过，skipped 不计入分母；
- **断言通过率**：用来区分「少数用例挂很多断言」和「很多用例各挂一条」；
- **跳过数**：环境不可用，不算失败，也不能当通过。

不报 precision / recall：`expect` 本身就是人工标注的 gold，没有第二份独立标注。

## 新增用例

1. 选类别，在 `cases/offline/<category>.jsonl` 追加一行，或在 `cases/live/<category>.json`
   的 `cases` 数组里加一项；
2. `id` 全局唯一，`description` 写清楚「这条用例防止什么回归」；
3. 跑 `python -m evaluation -v`；
4. 如果是一个线上问题，同时写 `bad_cases/BC-xxx-*.md`，把复现输入、根因、
   修复和本用例 id 串起来。

## 与其它测试的关系

| 层 | 位置 | 关注点 |
| --- | --- | --- |
| 单元测试 | `agent/tests/`、`backend/.../test/` | 函数 / 类级契约，mock 外部依赖 |
| 回归评测 | `evaluation/`（本目录） | Agent 端到端行为、跨会话状态 |
| 闭环验收 | `scripts/regress-closed-loop.sh` | 真 MySQL 下的 SSE、Session、SQL 约束 |

三者不是替代关系：单测保证「每个零件对」，评测保证「组合起来的行为对」，
闭环脚本保证「换一台机器、换一个数据库仍然对」。
