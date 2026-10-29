# Phase 4 真实检索、权限与来源追溯结果

- 日期：2026-10-28
- 运行批次：`20261027_172403`
- 真实调用：后端 `ready-ids` → 真实 `text-embedding-v4` → 真实 Qdrant
  `ails_phase4_e2e_text-embedding-v4_1024`
- 阈值：`top_k=5`、`min_score=0.35`，**未临时调整**

## 1. 结论摘要

| 检查 | 结果 |
| --- | --- |
| 有标注检索查询 | 5 条（N=5；A1 3 条、A2 1 条、B1 1 条），K=5 |
| Recall@5 | **1.0** |
| MRR | **0.9** |
| 跨用户/跨空间 Qdrant 直查 | 3 个应零命中组合全部 0 命中；同空间对照 1 命中 |
| 非 READY 文档 | 不进 ready-ids；0 chunk / 0 point |
| 最终 Context 注入范围 | A1 只注入 18/19；A2 只注入 20；B1 只注入 21 |
| 来源映射 | 每个注入 chunk 都能回到真实 document_id/chunk_id；引用标签与注入 chunk 一一对应 |
| 无匹配查询 | `THRESHOLD_FILTERED`；候选分数 0.2392 / 0.2297，均低于 0.35 |

**这些数字只来自 5 条合成查询、每条 1 个标注 chunk 的小样本，不能外推为
生产 Recall/MRR。** 没有做阈值校准，也没有用不同 Embedding 模型对比。

## 2. 有标注查询结果

“标注”方法：每篇合成文档写入唯一标记（`P4-TARGET-7K2` 等），查询意图只对应
其中一个 chunk；人工按标记确定 expected chunk，不依赖模型自评。

| # | 查询 | 空间 | 期望 chunk | 状态 | 返回候选（chunk / 分数） | 名次 | RR |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | ORBIT-4721 新部署在失败后等待多久再重试？ | A1 | 18（目标） | SUCCESS | 18 / 0.7335；19 / 0.7134 | 1 | 1.0 |
| 2 | 2024 年以前 ORBIT-4721 的旧调度器重试窗口是多少秒？ | A1 | 19（干扰） | SUCCESS | 19 / 0.8761；18 / 0.8421 | 1 | 1.0 |
| 3 | ORBIT-4721 当前重试窗口是多少？它的仪表盘默认颜色是什么？ | A1 | 18（目标） | SUCCESS | 19 / 0.7500；18 / 0.7140 | 2 | 0.5 |
| 4 | ORBIT-4721 当前调度器的重试窗口是多少秒？ | B1 | 21（B 自己的） | SUCCESS | 21 / 0.6768 | 1 | 1.0 |
| 5 | ORBIT-4721 当前调度器的重试窗口是多少秒？ | A2 | 20（A2 的） | SUCCESS | 20 / 0.7414 | 1 | 1.0 |

计算：

- Recall@5 = 5/5 = **1.0**；
- MRR = (1 + 1 + 0.5 + 1 + 1) / 5 = **0.9**；
- 查询 3 的目标 chunk 排第 2，原因是问题同时包含“当前重试窗口”和“仪表盘颜色”，
  干扰文档在向量空间里更接近整句；这不是权限或索引故障。

## 3. 无匹配查询

查询：`镓的熔点是多少摄氏度？`（A1，ready IDs `[20, 21]`）

| 项 | 实测 |
| --- | --- |
| 状态 | `THRESHOLD_FILTERED` |
| Qdrant 候选数 | 2 |
| 候选分数 | `orbit_legacy` 0.2392；`orbit_current` 0.2297 |
| 阈值 | 0.35 |
| 注入 chunk | 0 |
| 最终 Context | 有 `knowledge_status` 区块，明确说明候选全部低于阈值 |

没有把低分候选强行注入，也没有出现“无匹配却给出资料引用”。真实模型回答见
[phase4-rag-agent-results.md](phase4-rag-agent-results.md)。

## 4. 权限与状态过滤

### 4.1 后端 ready-ids

见索引报告 §2.4。A1 只拿到 `[20, 21]`，A2 只拿到 `[22]`，B1 只拿到 `[23]`；
FAILED 2、PROCESSING 3、其他空间/其他用户文档都不在 A1 白名单。

### 4.2 Qdrant 防御性直查

即使绕过后端白名单，直接用真实查询向量调用 Qdrant：

| 组合 | 查询用户/空间 | 文档 ID | 命中 | 预期 | 结果 |
| --- | --- | --- | --- | --- | --- |
| A 用户 + A1 空间 + B 文档 | 3 / 3 | 23 | 0 | 0 | PASS |
| B 用户 + B1 空间 + A 文档 | 8 / 13 | 20 | 0 | 0 | PASS |
| A 用户 + A1 空间 + A2 文档 | 3 / 3 | 22 | 0 | 0 | PASS |
| A 用户 + A1 空间 + A1 文档（对照） | 3 / 3 | 20 | 1 | >0 | PASS |

Qdrant filter 同时要求 `user_id`、`space_id`、`document_id IN allowlist`，三者
缺一不可；空白名单直接返回空，不发起无过滤查询。

### 4.3 最终 Context 中的权限结果

补充 preview（`supplemental_context.json`）实际注入：

| 空间 | 注入 chunk | 是否只含本空间 READY |
| --- | --- | --- |
| A1 | 18, 19 | 是 |
| A2 | 20 | 是 |
| B1 | 21 | 是 |

这验证的是**最终上下文**，不只是检索参数：A1 的 system instruction 里没有
A2/B1 的文档标签；A2/B1 也没有对方文档。

## 5. 来源可追溯

### 5.1 注入 chunk → 真实来源

| chunk_id | document_id | 用户/空间 | 文件名 | MySQL 正文 = Qdrant payload |
| --- | --- | --- | --- | --- |
| 18 | 20 | 3 / 3 | `orbit_current_20261027_172403.md` | 是 |
| 19 | 21 | 3 / 3 | `orbit_legacy_20261027_172403.md` | 是 |
| 20 | 22 | 3 / 12 | `orbit_archive_20261027_172403.md` | 是 |
| 21 | 23 | 8 / 13 | `orbit_shared_20261027_172403.md` | 是 |

### 5.2 引用标签映射

`supplemental_context.json` 的 `label_mapping` 检查：

- 最终 Context 实际注入 18/19；
- Prompt 中出现的两个标签
  `资料《orbit_current_20261027_172403.md》#0`、
  `资料《orbit_legacy_20261027_172403.md》#0` 都能解析回 18/19；
- 没有标签指向未注入或不存在 chunk。

### 5.3 引用合法性

真实模型 6 例（5 个主用例 + 1 个补充错误前提）的引用校验：

- 非法引用：**0**；
- malformed 标签：**0**；
- 引用 chunk 不在最终上下文：**0**；
- 引用文档超出本轮注入范围：**0**。

这不等于“引用内容一定为真”。词面支持度只是启发式；其中 2 条引用出现
`numericConflict=true`，说明“句子里的数字不在被引 chunk 里”，详见
[phase4-failure-analysis.md](phase4-failure-analysis.md) 的 `SOURCE_MAPPING`
观察。

## 6. 证据与边界

- `evaluation/phase4/reports/20261027_172403/retrieval_results.json`
- `evaluation/phase4/reports/20261027_172403/context_results.json`
- `evaluation/phase4/reports/20261027_172403/supplemental_context.json`

边界：

- 5 条查询、每条 1 个相关 chunk，样本极小；不能据此声称生产召回率；
- 没有负样本池、没有多相关 chunk、没有 reranker；
- 没有校准 `min_score=0.35` 对不同模型/语料的适用性；
- 没有验证删除文档、过期文档、撤销权限后的即时过滤（本轮只验证 READY/
  FAILED/PROCESSING 与用户/空间隔离）。
