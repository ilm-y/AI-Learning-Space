# Phase 4 失败与风险分析

- 日期：2026-10-28
- 运行批次：`20261027_172403`
- 原则：只分析实际观察到的证据；不把“没有执行”写成 PASS；不声称语义质量已验证

## 1. 结论

本轮真实 RAG 主链路（上传 → 索引 → READY → 权限过滤 → 检索 → Context →
真实 LLM）**主路径通过**，但发现 3 个需要处理的问题：

| 编号 | 问题 | 分类 | 严重度 | 状态 |
| --- | --- | --- | --- | --- |
| F1 | SUCCESS 检索 + 知识被预算全丢时，没有状态说明 | `CONTEXT_BUDGET` | 中 | **未修复** |
| F2 | 后端在索引中被杀后，文档永久停在 PROCESSING | `INDEX_FAILURE`（中断/无回收） | 中 | **未修复** |
| F3 | 合法性通过的引用仍可能把多来源句子标到单一来源 | `SOURCE_MAPPING` | 低-中 | **未修复，仅观测** |

没有观察到 `RETRIEVAL_MISS`、`ACCESS_FILTER`、`GENERATION_ERROR` 导致的错误
回答；6 个真实模型用例硬断言全部通过，但语义质量仍未人工评审。

## 2. F1：预算丢弃知识但没有说明（CONTEXT_BUDGET）

### 2.1 证据

`context_results.json` 的 `budget_pressure`：

```text
contextTokens = 2836 / budgetTokens = 3000
knowledge_injected = []
dropped_sections = ["knowledge:18", "knowledge:19"]
knowledge_status_section_present = false
warnings = ["以下片段因超出 Token 预算被丢弃：knowledge:18, knowledge:19", ...]
```

### 2.2 根因

- `ContextBuilder._KNOWLEDGE_STATUS_NOTES` 只包含
  `NO_VALID_DOCUMENTS`、`NO_MATCH`、`THRESHOLD_FILTERED`、`DEGRADED`；
  `SUCCESS` 没有 note，因此 `_render_knowledge_status` 返回空；
- `Runtime.generate` 的 `GUARD_STATUSES` 同样不包含 `SUCCESS`，所以不会追加
  “资料未注入”的说明；
- 预算裁剪按优先级丢弃 `knowledge:*`，但只把丢弃结果放进 warnings（给调试者
  看），没有进入 system instruction（给模型看）。

结果是：检索层知道“找到了资料”，Context 层把资料全丢了，模型层既看不到
资料，也看不到“检索成功但被预算裁掉”。这不是明确的假话，但会让模型把
“预算丢弃”误判成“没有资料”。

### 2.3 影响

- 长历史/长会话下，可能回答“没有检索到资料”，而实际是检索成功但预算不足；
- 本轮没有在这个上下文下调用真实 LLM，因此**不能**声称已经造成某个具体错误
  回答；这是结构性缺口 + preview 证据。

### 2.4 建议

1. 当 `state.knowledge` 非空、但最终 Context 没有任何 `knowledge:*` section
   时，注入一条不同于“没有检索到”的说明，例如：
   “本轮检索到了相关资料，但因上下文预算未注入；不要声称资料中没有答案。”
2. 不要简单地把 `SUCCESS` 加进现有 `GUARD_STATUSES`：现有 Guard 的语义是
   “没有知识片段 + 依赖失败/无匹配”，直接加会混淆“没检索到”和“检索到但
   没放下”。
3. 加回归测试：长历史 + 预算不足 + SUCCESS → 断言存在“检索到但被预算丢弃”
   的说明，且不出现“没有资料/没有检索到”的假声明。

## 3. F2：PROCESSING 孤儿状态（INDEX_FAILURE / 无回收）

### 3.1 证据

文档 3：

```text
status = PROCESSING
chunk_count = 0
MySQL chunks = 0
Qdrant points = 0
index_error = NULL
index_attempts = 1
ready-ids = 不包含
```

构造方式：假 TCP 服务挂住 parse，上传后杀掉后端；重启后端，文档仍停在
PROCESSING。

### 3.2 影响

- 该文档不会进 ready-ids，因此不会污染检索结果；
- 但它也不会自动失败、重试或清理，用户会看到一个永远“处理中”的文档；
- 没有租约/超时/恢复扫描，后端重启不会把中断的索引标记为 FAILED；
- 本轮没有验证“重新上传同一文档会不会覆盖/恢复”，也没有验证后台重试。

### 3.3 建议

1. 给索引任务加租约/超时（例如 `processing_started_at` + 最大时长），超时后
   由后台扫描转 `FAILED`，写明确错误（如“索引中断，可重试”），不伪造 READY；
2. 或在启动时扫描长时间 PROCESSING 的文档，按策略标记 FAILED/可重试；
3. 加回归测试：模拟进程中断 → 重启/扫描 → 状态有终态，ready-ids 仍排除。

## 4. F3：合法引用的多来源归因（SOURCE_MAPPING）

### 4.1 证据

6 个真实模型用例中，引用客观合法率 100%（16/16），但有 2 条
`numericConflict=true`：

| 用例 | 引用句 | 被引 chunk | 冲突 |
| --- | --- | --- | --- |
| insufficient_detail | “历史迁移文档里的旧值（12 秒）……不能当作现行值” | 现行规范 chunk 18 | 现行规范没有“12” |
| legacy_only | “发现 17 秒不符合预期……而不是改回 12 秒” | 现行规范 chunk 18 | 现行规范没有“12” |

### 4.2 解释

- 这两条不是非法引用：标签存在、chunk 存在且已注入；
- 问题在于一句话合并了两个来源的事实（现行规范 + 旧版记录），只标了现行
  规范；现行规范支持“不要用旧值/先核对版本”，但不包含“12 秒”这个数字；
- 现有校验器的 `numericConflict` 是启发式信号，不是硬失败；它正确提示了
  “被引 chunk 里没有句子中的数字”。

### 4.3 建议

1. Prompt 明确要求：一句话里出现来自不同资料的具体数字时，分别标注来源，
   或拆成两句；
2. 校验器保留 `numericConflict` 作为排查信号，不要直接判非法（避免把同义
   改写误杀）；
3. 在报告中继续区分“引用合法率”和“来源支持度”，不把前者当事实正确率。

## 5. 未观察到的问题（不能写成已验证）

| 类别 | 本轮结果 |
| --- | --- |
| `RETRIEVAL_MISS` | 5 条有标注查询全部召回；不支持外推 |
| `ACCESS_FILTER` | 跨用户/跨空间/非 READY 过滤全部 PASS；仅限本轮合成数据 |
| `INDEX_FAILURE` 导致假 READY | 未观察到；FAILED/PROCESSING 都无 chunk/point、都被 ready-ids 排除 |
| `GENERATION_ERROR` | 6 个真实模型用例硬断言通过；语义质量未评审 |
| 伪造引用/不存在 chunk | 0 条；16 条引用全部可解析到注入 chunk |
| 无匹配却引用资料 | 0 条；镓问题 0 引用 |

## 6. 其他限制（不是失败，但不能外推）

- **样本极小：** 5 条标注查询、每条 1 个 chunk；Recall@5=1.0、MRR=0.9 只能
  说明这组合成数据，不是生产指标；
- **批量边界未触发：** 每个文档 1 个 chunk，`embedding_batch_size=10` 没有
  真正触发多批请求；
- **阈值未校准：** 0.35 是配置默认起点；无匹配候选 0.2392/0.2297，支持查询
  0.67-0.88，样本不足以校准；
- **人工语义评审缺失：** 没有人工分、没有校准 Judge；语义质量仍为
  `UNREVIEWED`；
- **生产/真实数据未验证：** 本轮全部为隔离合成数据；没有验证真实用户数据、
  生产 Qdrant/MySQL、并发、吞吐、删除/过期文档的即时过滤；
- **安全修复边界：** 配置守卫只覆盖运行配置误注入，不防御能修改代码或启动
  参数的人，也不是新的身份协议。

## 7. 下一阶段优先级（建议前三项）

1. **修复 F1（CONTEXT_BUDGET）**：SUCCESS + 知识被预算全丢时，向模型明确
   说明“检索到但未注入”，并加长历史回归测试；这是最容易误导回答的缺口。
2. **修复 F2（PROCESSING 孤儿）**：加索引租约/超时/启动扫描，让中断任务有
   终态和可重试路径；不让文档无限“处理中”。
3. **处理 F3 并扩大真实样本**：Prompt 要求多来源数字分别标注，保留
   `numericConflict` 信号；同时把真实 RAG 用例从 6 条扩到多 chunk、多文档、
   负样本，并补人工语义评分。没有人工评分前，继续禁止把硬断言通过写成
   “语义质量通过”。

## 8. 证据

- `evaluation/phase4/reports/20261027_172403/summary.json`
- `evaluation/phase4/reports/20261027_172403/context_results.json`
- `evaluation/phase4/reports/20261027_172403/agent_results.json`
- `evaluation/phase4/reports/20261027_172403/index_results.json`
- `evaluation/phase4/reports/20261027_172403/retrieval_results.json`
- `evaluation/phase4/reports/20261027_172403/supplemental_*.json`
