# Agent 回归评测报告

- 生成时间：2026-10-21T21:01:21+08:00
- 代码版本：`cdab3b6`
- 运行模式：`all`
- 后端：`http://127.0.0.1:8080`
- Agent：`http://127.0.0.1:8000`

## 总览

| 指标 | 值 |
| --- | --- |
| 用例 | 48 |
| 通过 / 失败 / 跳过 | 48 / 0 / 0 |
| 用例通过率 | 100.0% |
| 断言 | 193 / 193 通过（100.0%） |

## 分类

| 类别 | 用例 | 通过 | 失败 | 跳过 | 用例通过率 | 断言通过率 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 跨会话上下文 (`cross_session`) | 6 | 6 | 0 | 0 | 100.0% | 100.0% |
| 证据去重 (`evidence_dedup`) | 6 | 6 | 0 | 0 | 100.0% | 100.0% |
| 记忆作用域 (`memory_scope`) | 8 | 8 | 0 | 0 | 100.0% | 100.0% |
| 理解演进 (`understanding_revision`) | 8 | 8 | 0 | 0 | 100.0% | 100.0% |
| 记忆冲突收敛 (`memory_conflict`) | 7 | 7 | 0 | 0 | 100.0% | 100.0% |
| 验证策略 (`verification`) | 7 | 7 | 0 | 0 | 100.0% | 100.0% |
| 分析输出契约 (`analysis_contract`) | 6 | 6 | 0 | 0 | 100.0% | 100.0% |

## 失败用例

无。
## 通过用例

- `contract-fenced-json` — 模型把 JSON 包在 ```json 围栏里时，仍能取出对象并解析
- `contract-malformed-json` — 模型输出里没有 JSON 对象时，抛 AnalysisError，而不是静默返回空结果
- `contract-invalid-enum` — 未知的 evidenceType 属于契约违约：整份结果作废，不能把半份写进库
- `contract-empty-input` — 空白消息直接返回空结果，不检索、不调用模型
- `contract-no-concept` — 闲聊不命中任何 Concept 时返回空结果（没有可挂证据的对象）
- `contract-unknown-field-and-clamp` — 未知字段被忽略；越界质量分被裁剪到 [0,1]；analyzerVersion 由分析器决定而不是模型决定
- `cross-session-history-user-hit` — 本轮消息不含概念名时，从最近的用户历史里找回 TLB（跨轮上下文）
- `cross-session-prefers-user-over-assistant` — 用户与 AI 的历史都提到概念时，优先采信用户自己说的（避免被 AI 的措辞带偏）
- `cross-session-lookback-window` — 回看窗口只有最近 4 条：更早提到的概念不再影响本轮识别
- `cross-session-assistant-fallback` — 用户历史没有线索时才回看 AI 历史：虚拟内存仍能被识别
- `dedup-identical-candidates` — 同一结果里的重复候选（概念+类型+观察都相同）只保留一条
- `dedup-same-text-different-type` — 文本相同但证据类型不同，是两条不同的观察，不能合并
- `dedup-per-concept-cap` — 单个概念最多保留 2 条证据，防止一次分析把状态刷屏
- `dedup-total-cap` — 整份结果最多 6 条证据：三个概念各 3 条，最后只留下 6 条
- `dedup-invalid-concept-dropped` — 模型编造的 conceptId 一律丢弃；同一结果里合法的候选仍然保留
- `dedup-blank-observation-dropped` — 空白观察不构成证据，不能占用去重名额
- `memory-action-add-preserved` — ADD 语义原样保留，由后端判断同身份记忆是否已存在
- `memory-action-update-preserved` — UPDATE 语义原样保留；后端只会更新仍是 CANDIDATE 的那条
- `memory-action-ignore-preserved` — IGNORE 也保留在结果里 —— 让后端日志能看出「模型想过但不写」，而不是凭空消失
- `memory-conflict-blank-content-dropped` — 空白内容不是一条可写的记忆，不能进入后端 Reconcile
- `memory-conflict-importance-clamped` — importance 越界被裁剪到 [0,1]，避免影响后端排序与阈值判断
- `memory-scope-global-clears-ids` — GLOBAL 记忆必须清空 spaceId / conceptId，否则 scope_key 会出现两种真相
- `memory-scope-space-uses-current-space` — SPACE 记忆的空间取自当前请求，模型给别的空间 id 会被纠正
- `memory-scope-concept-keeps-id` — CONCEPT 记忆保留本轮命中的 conceptId，且不带 spaceId（由后端按概念反查）
- `memory-scope-fabricated-concept-falls-back` — 模型给了一个没命中的 conceptId 时，CONCEPT 记忆回落到本轮主概念，而不是被丢弃或写错
- `memory-scope-defaults-to-concept` — 未声明 scope 时默认 CONCEPT；未给 conceptId 时回落到主概念
- `memory-scope-duplicate-content` — 同一结果里内容相同的两条记忆只提一次（跨轮去重由后端唯一约束负责）
- `revision-proposal-known-parts` — 理解提案是增量：known / uncertain / explanation 原样进入候选，交给后端合并
- `revision-empty-proposal-dropped` — 空提案（全是空列表 / 空白文本）不产生 Revision，也不能制造一次假的理解变化
- `revision-misconception-preserved` — 误区作为提案的一部分进入候选；是否新增 / 解除由后端生命周期决定
- `revision-fabricated-concept-falls-back` — 提案指向未命中的 conceptId 时回落到主概念，避免把理解写到别处
- `revision-explanation-truncated` — 过长的用户自述被截断到 500 字（保留省略号），防止 prompt 与数据库字段被撑爆
- `revision-heuristic-explanation-proposal` — 离线规则路径：用户自述会产生只带 latestUserExplanation 的提案（不猜 knownParts）
- `verification-new-concept-ask-later` — 首次接触一个概念且用户在提问时，只记 ASK_LATER，不打断教学
- `verification-known-concept-skip` — 已有理解快照的概念不重复触发验证（TLB 在 fixture 里是 MISCONCEPTION）
- `verification-explanation-skips` — 用户在解释而不是提问时，不插入验证（这一轮的任务是把话说完）
- `verification-recorded-ask-now-preserved` — 模型给出 ASK_NOW 时，题型 / 题干 / 正确答案 / trigger 原样保留
- `verification-fabricated-concept-skipped` — 验证决策引用未命中的概念时降级为 SKIP：宁可不出题，也不能给别的知识点出题
- `verification-ask-now-default-question` — ASK_NOW 没给题干时补一个可作答的默认题，而不是静默降级成 SKIP
- `live-cross-session-attaches` — 对话 A 里写下的自述，会进入对话 B 的 Agent 上下文（跨会话理解检索）
- `live-cross-session-session-attach` — 同一个学习活动下的第二个对话应 ATTACH 到已有 Session，而不是新建一个
- `live-memory-identity-conflict` — 同作用域同身份（缺省 key = content 哈希）重复创建返回 409；显式给不同 key 才能并存
- `live-memory-global-visible-everywhere` — GLOBAL 记忆对任意空间可见，且不会被空间的同名 scope_key 覆盖
- `live-memory-scopes-visible` — 同一空间 + 概念下检索时，GLOBAL / SPACE / CONCEPT 三种作用域都应可见
- `live-memory-scope-isolation` — 带别的 spaceId 检索时只能看到 GLOBAL：SPACE / CONCEPT 记忆不能跨空间泄漏
- `live-revision-question-exposes-concept` — Bad Case BC-001 回归：用户只是提问、Agent 没有形成判断时，状态也必须从 UNKNOWN 推进到 EXPOSED
- `live-revision-correction-appends` — 用户先自述（CORRECT）再自我纠正（INCORRECT）时，Revision 只追加不覆盖，状态推进到 MISCONCEPTION
- `live-verification-no-interrupt-on-first-question` — 首次接触概念且用户在提问时，Agent 只记 ASK_LATER，不创建 TestTask（不打断教学）

## 指标口径

- **用例通过率**：一条用例全部断言通过才算通过；被跳过的用例不计入分母。
- **断言通过率**：所有断言里通过的比例，用来区分「少数用例挂了很多断言」和「很多用例各挂一条」。
- 不报 precision / recall：用例的 `expect` 就是人工标注的 gold，没有第二份独立标注可供计算。
