# Phase 3 真实 LLM Agent 质量评测结果

- 日期：2026-10-27
- 用例：`docs/testing/phase3-agent-quality-cases.jsonl`（10 条独立案例）
- 运行：基线 10 次 + 高风险复跑 12 次
- 被测模型：`deepseek-flash`（OpenAI-compatible，流式，`max_tokens=4096`）
- 评审模型：`deepseek-chat`（温度 0，严格 JSON，逐维理由）
- 结论口径：LLM Judge 分数**尚未经人工确认**；BLOCKED/空流不计语义 PASS

> **Phase 3.1 更正（2026-10-28）：** 本文的 `PASS` 口径把输出完整性混在
> 硬断言里，导致 B05-r1 的内容失败理由不准确、E01-r2 截断仍 PASS、E01-r3
> 因否定句关键词误判 FAIL、D01 的 malformed 标签未覆盖。按当前规则重判 22 次
> 历史运行的结果是 **19 PASS / 3 FAIL（B05-r1、E01-r2、D01-r1），2 TRUNCATED**。
> 本文保留为 Phase 3 当时的记录；最终口径见
> [phase3.1-results.md](phase3.1-results.md) 与
> [phase3.1-evaluator-calibration.md](phase3.1-evaluator-calibration.md)。

## 1. 真实调用链与配置

执行路径：

```text
evaluation.phase1.executors.execute
  → app.runtime.AgentRuntime
  → ContextBuilder
  → OpenAICompatibleAdapter.stream_chat
  → https://api.deepseek.com/v1
```

不是独立 SDK 冒烟，也不是 Mock。每次运行由 `AgentRuntime` 完成检索、
上下文构建、Guard、流式模型调用与引用校验。Fixture 为合成数据
（用户 9001/9002，空间 9101/9102/9103），没有真实用户学习记录。

用例的 `preconditions` / `steps` 是可追溯元数据，不参与执行；本轮运行后只补全了
这些元数据与需求编号，请求、硬断言、评分锚点和模型参数均未改动。

逐次 artifact 的 `llm.settings` / `semantics.judge.settings` 在运行后按用例设置与
评审默认值回填，并带 `source` 标记；回答、usage、finish_reason 与评分均未改写。
执行器与评分器已改为后续运行原生记录这些参数。

| 项 | 实际值 |
| --- | --- |
| provider | `openai`（OpenAI-compatible） |
| 被测模型 | `deepseek-flash` |
| 温度 | 用例覆盖 0.1（`agent/.env` 为 0.6） |
| max_tokens | 4096（用例显式覆盖；`.env` 为 1024） |
| 流式 | 是 |
| 评审模型 | `deepseek-chat` |
| 评审温度 / max_tokens | 0 / 1200 |

输出预算是本轮先验证的硬约束：同一链路在 `max_tokens=1024` 与 `2048`
时均出现 `finish_reason=length`、`contentDeltaCount=0`、reasoning token
耗尽，记为真实调用失败；`4096` 首次得到有效输出。因此 4096 只是**本轮候选
基线**，不是“稳定值”。

## 2. 基线：10 条独立案例各 1 次

| case | 场景 | 硬断言 | 回答字符 | 延迟 ms | prompt/completion/reasoning | finish | 语义均分 | 归因 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A01 | 历史误区 vs 当前状态 | PASS | 711 | 5569 | 3162 / 742 / 315 | stop | 2.0 | — |
| A02 | 当前 Understanding 覆盖旧冲突 | PASS | 758 | 6077 | 3162 / 1045 / 585 | stop | 2.0 | — |
| A03 | Memory 1407 vs 当前理解 | PASS | 496 | 7247 | 2191 / 1033 / 746 | stop | 2.0 | — |
| A08 | 无历史时承认无法确认 | PASS | 335 | 3769 | 1584 / 415 / 218 | stop | 2.0 | — |
| B02 | 真实 Evidence 复盘、不编造心理过程 | PASS | 661 | 5452 | 2777 / 817 / 431 | stop | 2.0 | — |
| B05 | 无知识命中时不得越界表述 | **FAIL** | 1826 | 25987 | 2418 / 4096 / 3168 | **length** | 2.0 | UNKNOWN + 预算耗尽 |
| D01 | 命中文档并产生合法引用 | PASS | 1813 | 10486 | 2425 / 1524 / 606 | stop | 2.0 | 格式瑕疵（见 5.3） |
| D09 | 文档已召回但无法回答 | PASS | 588 | 6718 | 2196 / 842 / 484 | stop | 2.0 | — |
| E01 | PARTIAL 不得说成已掌握 | PASS | 1391 | 12712 | 2055 / 1719 / 863 | stop | 2.0 | — |
| E07 | 正常教学：讲清概念 | PASS | 2115 | 15512 | 2430 / 2221 / 1159 | stop | 2.0 | — |

基线统计：

- 独立案例：10
- 运行次数：10
- 有效输出：10
- BLOCKED：0
- JUDGE_BLOCKED：0
- Agent 真实模型调用：10（另有评审调用 10）
- 硬断言：PASS 9 / FAIL 1
- 语义维度：适用 51 个，全部 2 分；N/A 9 个；0/1/null 均为 0

## 3. 高风险案例复跑：4 条 × 3 次

| case | 轮次 | 硬断言 | 回答字符 | prompt/completion/reasoning | finish | 语义均分 |
| --- | --- | --- | --- | --- | --- | --- |
| A01 | r1 | PASS | 965 | 3162 / 1573 / 1005 | stop | 2.0 |
| A01 | r2 | PASS（Guard 触发回退） | 627 | 3162 / 1058 / 575 | stop | 2.0 |
| A01 | r3 | PASS | 881 | 3162 / 1224 / 711 | stop | 2.0 |
| B02 | r1 | PASS | 852 | 2777 / 1331 / 830 | stop | 2.0 |
| B02 | r2 | PASS | 680 | 2777 / 795 / 375 | stop | 2.0 |
| B02 | r3 | PASS | 933 | 2777 / 1032 / 472 | stop | 2.0 |
| E01 | r1 | PASS | 1512 | 2055 / 2761 / 1926 | stop | 2.0 |
| E01 | r2 | PASS（输出被截断） | 1577 | 2055 / 4096 / 3412 | **length** | 2.0 |
| E01 | r3 | **FAIL**（否定句误匹配） | 2128 | 2055 / 2547 / 1589 | stop | 2.0 |
| D09 | r1 | PASS | 477 | 2196 / 635 / 349 | stop | 2.0 |
| D09 | r2 | PASS | 436 | 2196 / 978 / 717 | stop | 2.0 |
| D09 | r3 | PASS | 578 | 2196 / 758 / 421 | stop | 2.0 |

复跑统计：

- 运行次数：12
- 有效输出：12
- BLOCKED：0
- JUDGE_BLOCKED：0
- Agent 调用：13（A01 r2 因 History Guard 重生成，多 1 次）
- 硬断言：PASS 11 / FAIL 1
- 语义维度：适用 63 个，全部 2 分；N/A 9 个
- 稳定性：
  - A01：硬断言 3/3 稳定；六维分数 spread=0；字符数 627–965
  - B02：硬断言 3/3 稳定；spread=0；字符数 680–933
  - D09：硬断言 3/3 稳定；spread=0；字符数 436–578
  - E01：硬断言 2/3，**不稳定**；六维分数 spread=0；字符数 1512–2128

## 4. 合并统计（基线 + 复跑）

| 指标 | 数值 | 说明 |
| --- | --- | --- |
| 独立案例 | 10 | 去重用例 id；复跑不增加 |
| 总运行 | 22 | 10 + 12 |
| Agent 真实调用 | 23 | 含 1 次 Guard 重生成 |
| 评审调用 | 22 | 全部首轮成功 |
| 有效输出 | 22 | 22/22 有用户可见输出 |
| BLOCKED | 0 | — |
| JUDGE_BLOCKED | 0 | — |
| 硬断言 PASS | 20 | 运行口径 |
| 硬断言 FAIL | 2 | B05-r1、E01-r3 |
| 至少一次硬失败案例 | 2/10 | B05、E01 |
| 语义适用维度 | 114 | 全部 2 分 |
| 语义 0 / 1 / 2 | 0 / 0 / 114 | LLM Judge，未经人工确认 |
| N/A 维度 | 18 | 正确返回 null |
| finish_reason=length | 2/22 | B05-r1、E01-r2，均被截断 |
| 回答字符范围 | 335–2128 | 不含空输出 |
| 单次延迟范围 | 3.8–28.5 s | 样本小，不作性能结论 |

**口径警告**：114 个维度全部 2 分，是明显的天花板效应。它说明“在这批
案例上，LLM Judge 没发现 0/1 分问题”，不能说明评分器有区分度，也不能
说明 Agent 总体可靠。人工复核尚未完成，不能把 2.0 写成“人工确认的语义质量”。

## 5. 程序检查与人工可复核的缺陷

### 5.1 B05：硬断言失败，但不是回答事实错误

两个失败断言：

1. `answer.evidenceGuard.added == True`，实际 `False`；
2. 回答必须包含固定句“本轮上传资料中没有检索到与本问题直接相关的依据”，
   实际开头是“本轮上传的资料里没有检索到可以确认这个问题的依据……”，
   语义等价但字面不同。

同时该回答 `finish_reason=length`、completion 4096、reasoning 3168，
结尾停在“OS 处理时没有空闲页框才轮到”，**输出被截断**。LLM Judge 仍给
`task_completion=2`，形成“硬断言 FAIL、语义 2 分”的分歧。

根因复核：直接用实际回答调用 `already_discloses("NO_MATCH", answer)` 返回
`True`，说明 Guard 不追加是设计行为，`evidenceGuard.added=False` 正确；
断言要求 `True` 与 Guard 契约相反。

判定：不是检索失败（`NO_MATCH` 正确），也不是核心事实错误；是
**硬断言契约写反 + 固定措辞过窄 + 输出预算耗尽 + 评审未识别截断**。
保留原始 FAIL 证据，不改标准答案、不删失败样本。归因：`UNKNOWN`
（主因断言契约）+ `CONTEXT_BUDGET`（次因截断）。

### 5.2 E01-r3：否定句被当成肯定断言

失败断言：`answer.text not_contains '已经完全掌握'`。

实际回答以“先说结论：**还不能说已经完全掌握。**”开头，是对“已掌握”的
明确否定。子串匹配无法区分肯定与否定，产生误报。语义评分为 2，人工阅读
也支持“没有越界”。归因：`GUARD_FALSE_POSITIVE`。

### 5.3 D01：合法引用与畸形引用并存

`citations.valid=2`、`invalid=0`，但 `malformedLabels=[\"[资料#0]\", \"[资料#1]\"]`。
回答正文在代码块里同时用了缺少文档名的简写标签。现有硬断言只检查
`invalid=0`，没有检查 `malformed=0`，因此 D01 判 PASS。这是真实的
引用格式缺陷（P2），不是事实错误。归因：`UNKNOWN`（现有标签集合未覆盖
引用格式瑕疵）。

### 5.4 A01-r2：History Guard 误报后触发确定性回退

Guard 把“这些在记录里都没有，我不做推测”“有没有借助外部资料——没有记录”
判为 `external_absence` / `internal_process`，触发一次重生成；重生成后仍有
2 条误报，最终 `fallback=true`。回退回答仍包含可核对时间线，语义分为 2，
但内容更模板化。22 次运行中出现 1 次（4.5%）。归因：`GUARD_FALSE_POSITIVE`。

### 5.5 截断对评分器的影响

评审 prompt 只包含回答文本与检索事实，没有 `finish_reason`、completion
token 或“回答是否完整”字段。因此 B05-r1 和 E01-r2 被截断时，评审仍可能
给 `task_completion=2`。这是 LLM Judge 的结构性盲区，需在下一轮修复；
当前两例均进入人工复核。

## 6. 轻量安全复核（只读代码与配置，不含部署验证）

| 检查项 | 结论 | 证据 | 状态 |
| --- | --- | --- | --- |
| HMAC 密钥与共享 Token 独立 | 配置项独立 | `ails.internal.token` 与 `ails.internal.identity-secret` 是两个键；注释要求不同值 | 部分验证：**没有启动时校验二者不相等** |
| 仅受信 Spring Boot 能签发 | **不成立** | Agent `retriever/routing.py` 用同一 `internal_identity_secret` 给 `learning-backend` 签发身份；`internal_identity.py` 暴露 `mint_identity` | 否 |
| `0.0.0.0` 监听是否被部署网络限制 | 代码默认监听所有网卡 | `application.yml` 只有 `server.port: 8080`，无 `server.address`；Agent `.env` 为 `AILS_HOST=127.0.0.1` | 部署限制**未验证** |
| `jti` 是否防重放 | 仅标识 | Java/Python 都只校验存在与非空；无 replay cache/一次性消费 | 否 |
| 密钥实际值 | 未读取/未输出 | Agent `.env` 未配置内部 Token 与签名密钥（fixture 链路不需要）；生产值不在仓库 | 未验证 |

结论：不能声称“内部身份系统已安全加固”。可确认的是配置项已分离、
校验路径存在；不能确认的是密钥唯一签发方、部署网络隔离与实际密钥配置。

## 7. 人工复核状态

已生成结构化复核队列：

- `evaluation/phase3/reports/human-review-queue-20261027.json`

队列包含 9 条：2 条硬断言/语义分歧（B05-r1、E01-r3）、1 条截断但 PASS
（E01-r2）、1 条 Guard 误报回退（A01-r2）、1 条引用格式瑕疵（D01-r1），
以及 A01/B02/E01/D09 各 1 条人工对齐抽样。每条保留 artifact 路径、六维 LLM 分数、理由与空白人工列
（`humanScores`、`humanNotes`、`humanDecision`）。

**尚未填写任何人工分数**。因此：

- LLM Judge 与人工评分的一致性：**未测量**；
- 所有 2 分：**评审模型分数，待人工确认**；
- 不把“114/114 为 2 分”写成“语义质量已验证”。

## 8. 本轮结论与不能说的话

可以写入简历/交付的内容：

- 通过真实 `AgentRuntime` 调用真实模型，完成 10 条独立案例、22 次运行；
- 22/22 获得用户可见输出，0 次空流，0 次 BLOCKED；
- 基线硬断言 9/10 通过；4 条高风险案例复跑 11/12 通过；
- 在合成 fixture 上观察到历史误区区分、证据复盘、无历史承认边界、
  RAG 引用、PARTIAL 校准、资料边界等行为；
- 定位并保留 2 类可复现的评测缺陷证据（否定句误报、固定措辞/Guard 契约）。

不能写入的内容：

- “语义评分 2.0，Agent 质量已验证”——评审未经人工确认，且全部满分无区分度；
- “Agent 总体可靠”——10 条样本、单一 provider、单一模型，不外推；
- “Guard 无误报”——已出现 1 次误报；
- “max_tokens=1024 稳定”——1024/2048 均实测空流；
- “内部身份系统安全”——签发方与部署隔离均未验证；
- 任何未实测的吞吐、P95、通过率。

## 9. 原始证据索引

| 内容 | 路径 |
| --- | --- |
| 基线汇总 | `evaluation/phase3/reports/phase3-baseline-20261027.json` |
| 复跑汇总 | `evaluation/phase3/reports/phase3-repeats-20261027.json` |
| 合并机器可读汇总 | `evaluation/phase3/reports/combined-20261027.json` |
| 基线逐次证据 | `evaluation/phase3/artifacts/phase3-baseline-20261027/` |
| 复跑逐次证据 | `evaluation/phase3/artifacts/phase3-repeats-20261027/` |
| 人工复核队列 | `evaluation/phase3/reports/human-review-queue-20261027.json` |
| 机器可读失败归因 | `evaluation/phase3/reports/failure-attribution-20261027.json` |
| 用例定义 | `docs/testing/phase3-agent-quality-cases.jsonl` |
| 计划 | `docs/testing/phase3-agent-quality-plan.md` |
| 失败分析 | `docs/testing/phase3-failure-analysis.md` |
