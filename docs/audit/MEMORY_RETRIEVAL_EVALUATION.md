# Phase 4：Memory 检索召回率离线评测报告

- 日期：2026-11-07
- 范围：Memory 检索的 Keyword / Dense / Hybrid 三种策略离线对比
- 设计依据：[`../design/MEMORY_HYBRID_SEARCH_DESIGN.md`](../design/MEMORY_HYBRID_SEARCH_DESIGN.md)
- 前置阶段：[`PHASE3_OUTBOX_RRF.md`](PHASE3_OUTBOX_RRF.md)
- 评测脚本：[`../../evaluation/memory/run_eval.py`](../../evaluation/memory/run_eval.py)
- 原始结果：[`../../evaluation/memory/reports/latest.json`](../../evaluation/memory/reports/latest.json)
- 运行日志：[`artifacts/memory-retrieval-eval-20261107T2215.txt`](artifacts/memory-retrieval-eval-20261107T2215.txt)

> **状态：已完成，未修改任何 Java 生产代码，未提交 Git，等待审查。**

---

## 1. 评测背景

Phase 3 之前，Memory 检索只有 MySQL `LIKE` / FULLTEXT 一条路径。它的痛点是**只能匹配字面词**：用户问“推荐夜宵”，系统无法联想到“用户不吃香菜，点外卖要提醒商家”这条记忆；只有查询里恰好出现“香菜”或“外卖”时才会命中。

Phase 3 落地了 Hybrid 检索：MySQL FULLTEXT + Agent/Qdrant 向量召回，再用 RRF(k=60) 融合并回源复核。本轮要回答四个问题：

1. 关键词检索在同义/模糊表达上到底失败多少？
2. 向量检索能否补上这部分召回？
3. Hybrid 是否同时保留字面匹配能力，并维持可接受的延迟？
4. 无关查询会不会被误召回？

本轮只做离线评测：**不修改 `MemoryService` 等任何 Java 业务代码**，评测脚本全部位于 `evaluation/memory/`。

---

## 2. 评测环境

| 组件 | 实际值 |
| --- | --- |
| 日期 | 2026-11-07（本地时间 22:15 左右完成） |
| Python | 3.10.11 |
| MySQL | 8.0.37，`ngram_token_size=2` |
| MySQL 评测库 | `ails_memory_eval`（专用库，默认每次重置） |
| Qdrant | 1.20.0 |
| Qdrant collection | `ails_memory_eval_text-embedding-v4_1024`（专用前缀） |
| Embedding | `text-embedding-v4`，1024 维，OpenAI 兼容接口 |
| 生产库 / collection | **未触碰**；评测不连接 `ails` 开发库，也不写入 `ails_memories_*` |

评测凭据只从环境变量 / 命令行读取，不写入本报告、日志或结果 JSON。脚本对数据库名和 collection 名做了前缀守卫：只允许重置 `ails_memory_eval*`，拒绝误删其他库或 collection。

**本报告的所有指标都来自上述本地真实环境的一次实际运行**（MySQL 8.0.37 + Qdrant 1.20.0 + 真实 `text-embedding-v4` API），不是模拟数据或手填数字；原始逐查询结果、未过滤分数和完整运行日志均已归档。

---

## 3. 评测集说明

### 3.1 规模与文件

| 文件 | 内容 |
| --- | --- |
| [`evaluation/memory/datasets/memory_queries.jsonl`](../../evaluation/memory/datasets/memory_queries.jsonl) | 30 条查询：10 字面 / 10 同义 / 10 无关 |
| [`evaluation/memory/datasets/memory_fixtures.jsonl`](../../evaluation/memory/datasets/memory_fixtures.jsonl) | 34 条记忆：20 条目标 + 10 条干扰 + 3 条跨用户 + 1 条已归档 |

主用户 ACTIVE 记忆共 30 条（20 条目标 + 10 条干扰），另有：

- **跨用户负样本 3 条**（user_id=9002，id 20001–20003）：内容故意与 S1/S2/S3 接近，用来验证 `user_id` 隔离；
- **已归档负样本 1 条**（id 10031，status=ARCHIVED）：同时写入 Qdrant，用来验证 `status='ACTIVE'` 过滤；
- 所有记忆为 `GLOBAL` 作用域；查询不传 `spaceId`，因此本评测不覆盖 SPACE / CONCEPT 可见性矩阵，但 `user_id` 与 `status` 过滤仍然生效。

稳定 ID 便于逐条复核：L1–L10 对应 10001–10010，S1–S10 对应 10011–10020，干扰项 10021–10030。

### 3.2 三类查询的构造原则

| 类别 | 条数 | 构造原则 | 目标行为 |
| --- | --- | --- | --- |
| 字面匹配 `literal` | 10 | 查询与目标记忆共享明确词/二元组，例如“递归和分治”“事务隔离级别” | Keyword、Dense 都应命中 |
| 同义表达 `synonym` | 10 | 查询与目标记忆**语义相关但没有任何共同二元组**，例如“推荐夜宵”→“不吃香菜” | Keyword 应失败；Dense/Hybrid 应命中 |
| 无关查询 `irrelevant` | 10 | 与全部 34 条记忆无字面或语义关联，例如“梵高的星空用了哪些颜色” | 三种策略都不应返回结果 |

同义类是否真的“无字面重叠”，不是靠人工假设：实测 Keyword 在同义类上**目标命中 0/10**（S9 还出现一次无关假阳性，见 §8.5），说明这组数据确实把“关键词失败”复现出来了。无关类在最终数据上 Keyword 也 **10 条全部为空**；唯一一次早期草稿中的误命中（“上海明天的天气”共享二元组“天的”）已在评测集修正为“上海明天天气怎么样”，修正后重跑。

几条代表性样本：

| ID | 查询 | 目标记忆 | 关联方式 |
| --- | --- | --- | --- |
| S1 | 推荐夜宵 | 用户不吃香菜，点外卖时需要提醒商家不要放香菜。 | 夜宵推荐要考虑忌口 |
| S2 | 买零食要看配料表吗 | 用户对花生过敏，选餐时必须避开含花生的菜品。 | 过敏 → 看配料 |
| S3 | 怎样避免半夜被亮醒 | 用户睡眠很浅，任何光线都会让自己醒来。 | 怕光 → 遮光 |
| S6 | 不用鼠标怎么操作电脑 | 用户更习惯在黑框里敲指令完成开发，不喜欢拖拽式界面。 | 无鼠标 → 命令行 |
| S9 | 上班路上怎么避开拥堵 | 用户开车通勤，早高峰走文一路会堵很久。 | 通勤 → 避开拥堵 |

---

## 4. 三种 Retriever 的实现

### 4.1 KeywordRetriever

直接连 MySQL，执行与生产 `MemoryMapper.searchFulltext` 同表、同索引的查询：

```sql
SELECT id,
       MATCH(content) AGAINST(%s IN NATURAL LANGUAGE MODE) AS relevance
FROM memory
WHERE user_id = %s
  AND status = 'ACTIVE'
  AND MATCH(content) AGAINST(%s IN NATURAL LANGUAGE MODE)
ORDER BY relevance DESC, importance DESC, updated_at DESC, id ASC
LIMIT %s
```

`memory.content` 上建的是 `FULLTEXT ... WITH PARSER ngram`，`ngram_token_size=2`。因此中文查询会被切成二元组；只要查询与记忆共享一个二元组就可能命中——这既是关键词的召回来源，也是 §8.5 假阳性的来源。

### 4.2 DenseRetriever

复用 Agent 生产代码，不另写一套向量逻辑：

- `build_embedding_provider(Settings)` → `text-embedding-v4`；
- `QdrantMemoryStore.search()`：过滤 `user_id`、`status='ACTIVE'`，空间为空时不做作用域过滤；
- `MemoryVectorSearch.search()`：查询 Embedding → Qdrant → 只返回 `memoryId / score / contentHash`。

生产配置的 `memory_search_min_score=0.0`（RRF 只用排名）。本评测为了把“无关查询不该召回”变成可度量指标，在**客户端**额外施加统一阈值 `τ=0.42`：低于 τ 的候选不进入结果。原始未过滤分数保留在结果 JSON 中，用于 §7 的敏感性分析。

### 4.3 HybridRetriever

按 Phase 3 的设计实现：

1. `asyncio.gather` **并行**执行 Keyword（`to_thread`）与 Dense；
2. RRF：`score(d) = Σ w_i / (k + rank_i(d))`，`k=60`，两路权重均为 1.0，rank 从 1 开始；
3. 对融合后的 id 回 MySQL 复核：`id IN (...) AND user_id = ? AND status = 'ACTIVE'`；
4. 排序：RRF 分数降序 → importance 降序 → updated_at 降序 → id 升序，与 `HybridSearchService` 的稳定排序一致。

任意一路失败时 Hybrid 仍使用另一路（状态会标记降级）；本评测实际运行中两路均成功，状态全部为 `OK`。

### 4.4 指标定义

| 指标 | 定义 |
| --- | --- |
| Recall@5 | 在 20 条可回答查询上，Top-5 是否包含目标记忆的均值；每条只有 1 个目标，因此单条取 0/1 |
| MRR@5 | 目标记忆首次出现排名的倒数；Top-5 未出现记 0；在 20 条可回答查询上取平均 |
| P95 延迟 | 每条查询每个策略重复 2 次，共 60 个延迟样本；nearest-rank 95 分位 |
| 无关空结果率 | 10 条无关查询中 Top-5 为空的比例 |

每条查询重复 2 次，指标取第 1 次结果；脚本同时比较两次的 id 序列，最终质量检查为“无不一致”。

---

## 5. 主结果

| Retriever | Recall@5（20 可回答） | MRR@5（20 可回答） | 无关空结果率（10） | P95 延迟 | 平均延迟 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Keyword | **0.500** | **0.500** | 1.000 | 1.6 ms | 1.2 ms |
| Dense | **1.000** | **0.975** | 1.000 | 633.1 ms | 335.7 ms |
| Hybrid | **1.000** | **0.950** | 1.000 | 624.1 ms | 324.7 ms |

### 5.1 分类别

| Retriever | 字面 Recall@5 | 字面 MRR@5 | 同义 Recall@5 | 同义 MRR@5 | 无关空结果率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Keyword | 1.000 | 1.000 | **0.000** | **0.000** | 1.000 |
| Dense | 1.000 | 1.000 | **1.000** | 0.950 | 1.000 |
| Hybrid | 1.000 | 1.000 | **1.000** | 0.900 | 1.000 |

### 5.2 怎么读这张表

- **Keyword 的 0.500 正好是“字面 10 条全中、同义目标 10 条全灭、无关 10 条全空”**（同义类中 S9 有一次无关假阳性，但目标仍未命中）。这不是数据集偏袒，而是关键词检索的机制性结果：同义查询与目标记忆没有共同二元组，FULLTEXT 无从匹配。
- **Dense 把同义类从 0.000 拉到 1.000**，整体 Recall@5 达到 1.000。唯一不是第 1 名的是 S1（目标排第 2），因此 MRR 为 0.975。
- **Hybrid 的 Recall@5 与 Dense 持平（1.000）**，同义类同样 10/10；字面类仍是第 1 名。它的 MRR 为 0.950，比 Dense 低 0.025，原因只有一个：S9 的关键词假阳性被 RRF 排到了目标前面（详见 §8.5）。这是“等权 RRF + 通用二元组”的真实副作用，不应隐藏。
- **延迟**：Keyword 约 1 ms；Dense 与 Hybrid 的平均延迟都在 325–336 ms、P95 都在 624–633 ms。Hybrid 没有把两路延迟相加，因为两路并行执行，融合与回源复核的开销很小；与 Dense 的差异在测量噪声范围内。三者都远低于 Phase 3 配置的 1500 ms（词法）/ 2000 ms（向量）超时。

---

## 6. 阈值敏感性（后验分析）

Dense 的原始（未过滤）分数中：

- 10 条无关查询的最高分是 I3 的 **0.4179**（“推荐一款适合编程的机械键盘”误撞到 S6 的“黑框/指令”记忆）；
- 20 条可回答查询中，目标记忆的最低分是 S8 的 **0.4476**（“番茄钟应该设多长时间”→“学习超过四十分钟需要休息”）；
- 两者之间只有 **0.0297** 的间隔。

| τ | Recall@5 | MRR@5 | 无关空结果率 |
| ---: | ---: | ---: | ---: |
| 0.30 | 1.000 | 0.975 | 0.400 |
| 0.35 | 1.000 | 0.975 | 0.700 |
| 0.40 | 1.000 | 0.975 | 0.800 |
| **0.42** | **1.000** | **0.975** | **1.000** |
| 0.45 | 0.950 | 0.925 | 1.000 |
| 0.50 | 0.850 | 0.850 | 1.000 |
| 0.55 | 0.650 | 0.650 | 1.000 |

这张表要诚实解读：

- τ=0.42 是本次评测用来让“无关查询返回空”可度量的门控值，它落在上述 0.0297 的窄间隔内；
- τ=0.40 时 I3 会误召回；τ=0.45 时 S8 的目标会被误删。**这个阈值不是可以跨模型/跨语料硬编码的常数**，必须按实际 Embedding 模型和业务语料重新校准；
- 结论对阈值不敏感：即使把 τ 提高到 0.50，Dense 的 Recall@5 仍有 **0.850**，远高于 Keyword 的 **0.500**；τ=0.55 的极端值才降到 0.650。语义分支带来的召回优势不是靠 0.42 这一点“调”出来的；
- 生产当前 `memory_search_min_score=0.0`，所以“无关查询空结果率 1.000”是**评测门控下的结果**，不是当前生产行为。如果产品要求“无关查询返回空”，需要把相关性门控纳入配置并做校准。

---

## 7. 逐查询排名（完整 30 条）

表中数字为目标记忆首次出现的排名；“未命中”表示 Top-5 内没有目标；“空/非空”用于无关查询。

| ID | 类别 | 查询 | Keyword | Dense | Hybrid |
| --- | --- | --- | ---: | ---: | ---: |
| L1 | literal | 递归和分治 | 1 | 1 | 1 |
| L2 | literal | 代码示例 | 1 | 1 | 1 |
| L3 | literal | 复习英语单词 | 1 | 1 | 1 |
| L4 | literal | 数学证明 严格推导 | 1 | 1 | 1 |
| L5 | literal | 图表和流程图 | 1 | 1 | 1 |
| L6 | literal | 先写测试 再实现功能 | 1 | 1 | 1 |
| L7 | literal | 咖啡因 失眠 | 1 | 1 | 1 |
| L8 | literal | 事务隔离级别 | 1 | 1 | 1 |
| L9 | literal | 费曼学习法 | 1 | 1 | 1 |
| L10 | literal | 杭州西湖跑步 | 1 | 1 | 1 |
| S1 | synonym | 推荐夜宵 | 未命中 | 2 | 2 |
| S2 | synonym | 买零食要看配料表吗 | 未命中 | 1 | 1 |
| S3 | synonym | 怎样避免半夜被亮醒 | 未命中 | 1 | 1 |
| S4 | synonym | 腿脚不方便还能去登山吗 | 未命中 | 1 | 1 |
| S5 | synonym | 我想领养一只布偶猫 | 未命中 | 1 | 1 |
| S6 | synonym | 不用鼠标怎么操作电脑 | 未命中 | 1 | 1 |
| S7 | synonym | 可以讲得通俗一点吗 | 未命中 | 1 | 1 |
| S8 | synonym | 番茄钟应该设多长时间 | 未命中 | 1 | 1 |
| S9 | synonym | 上班路上怎么避开拥堵 | 未命中 | 1 | 2 |
| S10 | synonym | 写文章没思路怎么办 | 未命中 | 1 | 1 |
| I1 | irrelevant | 量子计算机的退相干时间如何测量 | 空 | 空 | 空 |
| I2 | irrelevant | 如何用望远镜观测木星大红斑 | 空 | 空 | 空 |
| I3 | irrelevant | 推荐一款适合编程的机械键盘 | 空 | 空 | 空 |
| I4 | irrelevant | 上海明天天气怎么样 | 空 | 空 | 空 |
| I5 | irrelevant | 怎么给多肉植物换盆 | 空 | 空 | 空 |
| I6 | irrelevant | 比特币今年的价格走势 | 空 | 空 | 空 |
| I7 | irrelevant | 怎么挑选降噪耳机 | 空 | 空 | 空 |
| I8 | irrelevant | 梵高的星空用了哪些颜色 | 空 | 空 | 空 |
| I9 | irrelevant | 如何修复漏水的水龙头 | 空 | 空 | 空 |
| I10 | irrelevant | 珠穆朗玛峰的海拔是多少 | 空 | 空 | 空 |

跨用户负样本（20001–20003）与已归档负样本（10031）没有出现在任何策略的 Top-5 中。评测 collection 实际写入 **34 个 point**（20 目标 + 10 干扰 + 3 跨用户 + 1 归档），说明负样本确实进了 Qdrant；它们没有出现在结果里，是 Qdrant 的 `user_id/status` 过滤和 Hybrid 的 MySQL 回源复核按预期工作的证据，而不是“负样本根本没写进去”。

---

## 8. 案例分析

### 8.1 S1：推荐夜宵 → 用户不吃香菜

- 目标：10011「用户不吃香菜，点外卖时需要提醒商家不要放香菜。」
- Keyword：**空**。查询“推荐夜宵”与记忆没有共同二元组，FULLTEXT 无匹配。
- Dense：10007（咖啡因敏感/晚上不喝咖啡）0.4923 第 1，10011 目标 0.4871 第 2。
- Hybrid：同 Dense，目标第 2，Recall@5 命中，MRR=0.5。

这个例子最能说明“语义召回”的价值，也暴露了它的边界：模型确实把“夜宵”与“外卖忌口”联系起来，但 L7 的“晚上/咖啡”同样是夜间饮食语境，分数略高。目标仍在 Top-5，业务可用；如果要求 Top-1，需要更强的重排或引入“忌口/推荐”意图特征。

### 8.2 S2：买零食要看配料表吗 → 用户对花生过敏

- 目标：10012「用户对花生过敏，选餐时必须避开含花生的菜品。」
- Keyword：**空**。查询与记忆没有共同二元组。
- Dense：10012 第 1，分数 **0.5127**；Hybrid：10012 第 1。
- 这是最干净的一条：查询完全没有“过敏/花生”字样，向量模型通过“看配料表”推断出“避开花生”，Dense 和 Hybrid 都直接命中第 1。

### 8.3 S3：怎样避免半夜被亮醒 → 用户睡眠浅、怕光

- 目标：10013「用户睡眠很浅，任何光线都会让自己醒来。」
- Keyword：**空**。
- Dense：10013 第 1，分数 **0.6492**；Top-5 里还有 10007（咖啡因）、10030（屏幕时长）、10018（学习休息）、10003（英语单词）等干扰项，但目标分数明显领先。
- 说明向量路不仅在“同义词”上有效，也能在多个语义相近的干扰项中把正确记忆排到第 1。

### 8.4 S6：不用鼠标怎么操作电脑 → 黑框里敲指令

- 目标：10016「用户更习惯在黑框里敲指令完成开发，不喜欢拖拽式界面。」
- Keyword：**空**（查询没有“黑框/指令/拖拽”等词）。
- Dense：10016 第 1，分数 **0.4737**；它是唯一超过 τ=0.42 的候选。
- 这条展示的是“关联推理”而非简单同义：不用鼠标 → 命令行/黑框操作。模型给出了可用的语义桥。

### 8.5 S9：Hybrid 唯一一次输给 Dense —— 关键词假阳性

- 目标：10019「用户开车通勤，早高峰走文一路会堵很久。」
- 查询：“上班路上怎么避开拥堵”。
- Keyword：返回 **10012**（“用户对花生过敏，选餐时必须避开含花生的菜品。”），MySQL relevance=2.3454。原因是查询与 10012 共享二元组 **“避开”**；这是一个完全合法的字面命中，但与通勤无关。
- Dense：目标 10019 第 1，分数 **0.6944**；10012 不在 Dense 结果中（低于 τ）。
- Hybrid：10012 与 10019 都只出现在一路，RRF 分数同为 `1/(60+1)=0.01639`；稳定排序用 importance 打破平局，10012 的 importance=0.90 高于 10019 的 0.71，于是 10012 排第 1、目标第 2。Hybrid 的 MRR 因此从 Dense 的 0.975 降到 0.950。

这是等权 RRF 的已知特性：**只看排名，不看词法分数大小**。一个通用二元组“避开”就能让无关记忆拿到与目标相同的 RRF 分数，再被 importance 推到前面。可行的改进方向（本轮未改代码）：

1. 给词法路加最低 relevance 或查询词覆盖率门槛，过滤“只共享一个通用二元组”的候选；
2. 降低词法权重，或对短查询/通用词做 IDF 加权；
3. 在 RRF 平局时优先用向量分数，而不是 importance。

这条案例说明 Hybrid 的“召回”没有问题（目标仍在 Top-5），但“排序”还有优化空间；不能把 Hybrid 说成在所有指标上都优于 Dense。

### 8.6 字面查询：Exact match 仍然重要

L1“递归和分治”：

- Keyword：目标 10001 第 1，relevance **4.6909**；
- Dense：目标 10001 第 1，分数 **0.7632**；
- Hybrid：目标 10001 第 1，RRF 0.0328。

字面类 10/10 三种策略都是第 1。Hybrid 保留了 Keyword 的精确匹配能力，没有因为引入向量而牺牲字面排序。

### 8.7 无关查询：门控在做什么

- I2“如何用望远镜观测木星大红斑”：Keyword 空；Dense 原始最高分 10030（屏幕时长）0.4047，低于 τ=0.42 → 空。
- I3“推荐一款适合编程的机械键盘”：Keyword 空；Dense 原始最高分 10016（黑框/指令）0.4179，仍低于 τ=0.42 → 空。这是全部无关查询中离阈值最近的一条，只差 0.0021。

所以“无关空结果率 1.000”不是免费的：它依赖一个刚好落在 0.4179 与 0.4476 之间的阈值。生产若要启用这个行为，必须用真实业务语料校准，不能直接抄 0.42。

---

## 9. 结论

### 9.1 最初的业务痛点被复现了

Keyword 在同义类上 **Recall@5 = 0.000（0/10）**，整体 Recall@5 只有 **0.500**。这不是实现 bug，而是 FULLTEXT 的机制边界：查询与记忆没有共同二元组时，字面检索无从命中。用户问“推荐夜宵”找不到“不吃香菜”，正是这个机制的结果。

### 9.2 Hybrid 的语义分支解决了它

Dense / Hybrid 在同义类上都是 **10/10，Recall@5 = 1.000**；整体 Recall@5 从 0.500 提升到 1.000。Dense 除 S1 排第 2 外其余 9 条都是第 1；Hybrid 除 S1、S9 排第 2 外其余 8 条都是第 1。对“不吃香菜/花生过敏/怕光/膝盖手术/猫毛过敏/命令行/通俗解释/番茄钟/写作提纲”等没有字面重叠的表达，向量路都能召回正确记忆。

### 9.3 Hybrid 同时保留了字面精确匹配

字面类三种策略都是 10/10 第 1；Hybrid 的 RRF 没有削弱 Keyword 的精确命中。延迟上，Hybrid 平均 **324.7 ms**、P95 **624.1 ms**，与 Dense 的 335.7 / 633.1 ms 基本持平，因为两路并行；远低于 Phase 3 的 1500/2000 ms 超时。它比 Keyword 慢约 300 ms，但换来了 10 条同义查询的召回，这是业务上值得的交换。

### 9.4 不能夸大的地方

- Hybrid 的 MRR@5 是 **0.950**，比 Dense 的 **0.975** 低 0.025；原因是 S9 的关键词假阳性（§8.5）。Hybrid 在“召回”上追平 Dense，但在“排序”上并非全面胜出。
- “无关查询空结果率 1.000”依赖 τ=0.42 的评测门控；生产默认 `min_score=0.0`。要把“无关查询返回空”变成产品行为，需要单独配置和校准阈值。
- 本评测语料只有 30 条主用户 ACTIVE 记忆、20 条可回答查询；绝对数值会随语料规模、Embedding 模型和阈值变化，不能直接把 1.000 / 0.950 当成生产 SLA。

### 9.5 总体判断

**Hybrid 方案解决了最初的业务痛点。** 它的语义分支把关键词完全无法处理的 10 条同义查询全部召回，字面分支保留了精确匹配，双路并行使延迟与纯 Dense 基本持平。当前的主要改进点不是“要不要 Hybrid”，而是“如何抑制词法假阳性、如何校准相关性门控”；这两点都可以在现有架构内通过词法门槛、权重或平局规则调整，不需要推翻设计。

同时要如实说明：**在本数据集上，Dense 单独也已经能解决同义召回，Hybrid 的离线指标是与 Dense 基本持平（Recall 相同、MRR 低 0.025），而不是全面超越。** Hybrid 的架构价值主要在词法兜底、单路故障降级和沿用现有 FULLTEXT 索引；这些价值在 Phase 3 的单测里覆盖，但本轮没有做故障注入，因此不能用这组离线指标声称“Hybrid 一定优于 Dense-only”。

---

## 10. 复现方式

```powershell
cd D:/learningMatiarials/AI-Learning-Space

# PowerShell：密码只放环境变量，不写入脚本 / 报告
$env:MYSQL_PASSWORD = "<本机 MySQL 密码>"
python -m evaluation.memory.run_eval --repeats 2
```

或 Bash：

```bash
cd /d/learningMatiarials/AI-Learning-Space
MYSQL_PASSWORD='<本机 MySQL 密码>' python -m evaluation.memory.run_eval --repeats 2
```

脚本默认：

- 重置专用库 `ails_memory_eval`；
- 重置专用 collection `ails_memory_eval_text-embedding-v4_1024`；
- 从 `backend/backend/src/main/resources/schema.sql` 建表；
- 写入 34 条 fixture；
- 运行 30 条查询 × 3 策略 × 2 次；
- 输出 `evaluation/memory/reports/latest.json` 与 `latest.md`。

安全守卫：数据库名必须以 `ails_memory_eval` 开头，collection 名必须以 `ails_memory_eval` 开头；否则脚本拒绝重置。`--no-reset` 可用于保留已有评测数据，但需要自行保证 fixture 一致。

---

## 11. 已知限制

1. **小语料**：30 条主用户 ACTIVE 记忆、20 条可回答查询、10 条无关查询；这是离线对照，不是大规模 benchmark。没有计算置信区间，指标是点估计。
2. **单一 Embedding 模型**：只验证了 `text-embedding-v4` 1024 维；换模型后分数分布和 τ 都会变化。
3. **阈值后验选择**：τ=0.42 是在本次语料上观察到的窄间隔（0.4179–0.4476）内选取的，§6 已给出敏感性表；不要把它当作跨语料常数。
4. **评测路径与生产路径不是同一份 Java 代码**：Keyword 直接执行等价 SQL；Dense 复用 Agent 的 `MemoryVectorSearch` / `QdrantMemoryStore`；Hybrid 的 RRF 与回源复核在 Python 中按同一公式重写。公式与过滤条件一致，但没有通过调用 Spring `HybridSearchService` 做端到端 HTTP 对比。
5. **延迟是单请求串行测量**：没有并发压力，P95 是单查询延迟，不是吞吐；Embedding 走外部 API，网络抖动会影响绝对值。
6. **未评测答案质量**：本轮只评测“召回/排序”，不评测最终 LLM 回答是否正确；后者属于 RAG 端到端评测的范围。
7. **无关查询的“空”依赖门控**：生产 `memory_search_min_score=0.0` 时 Dense 会返回 Top-k，不会返回空；这是配置差异，不是代码缺陷。

---

## 12. 产物清单

| 产物 | 说明 |
| --- | --- |
| [`evaluation/memory/datasets/memory_queries.jsonl`](../../evaluation/memory/datasets/memory_queries.jsonl) | 30 条人工评测查询 |
| [`evaluation/memory/datasets/memory_fixtures.jsonl`](../../evaluation/memory/datasets/memory_fixtures.jsonl) | 34 条记忆 fixture（含跨用户 / 归档负样本） |
| [`evaluation/memory/run_eval.py`](../../evaluation/memory/run_eval.py) | 三种 Retriever + 指标 + 阈值敏感性 + 报告生成 |
| [`evaluation/memory/reports/latest.json`](../../evaluation/memory/reports/latest.json) | 完整逐查询原始结果（含 raw dense scores） |
| [`evaluation/memory/reports/latest.md`](../../evaluation/memory/reports/latest.md) | 自动生成的摘要表 |
| [`artifacts/memory-retrieval-eval-20261107T2215.txt`](artifacts/memory-retrieval-eval-20261107T2215.txt) | 本次真实运行日志 |

**本轮未修改任何 Java 生产代码；未执行 `git add` / `git commit`，工作区保留改动等待审查。**
