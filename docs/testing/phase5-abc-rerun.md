# Phase 5 Step 3：判据修正与受影响场景重跑结果

- 生成时间：2026-11-09T22:24:32
- 重跑快照时间：2026-11-09 22:13:27
- 隔离库：`phase5_pilot_20261109221325_f4fa5a`
- 文档 collection：`phase5-pilot-20261109221325-f4fa5a_text-embedding-v4_1024`
- Memory collection：`phase5-pilot-20261109221325-f4fa5a-memory_text-embedding-v4_1024`
- 重跑对话：21 次（7 个场景×策略块 × 3 次）；其中 S5-B-2 触发一次 `HISTORY_GUARD` 重生成，模型实际调用 22 次
- 原 90 次全量结果保留，不修改；仅重跑受判据修正影响的组合。

## 1. 判据修正说明

本批次只修正判据，不修改实验数据、预置内容、问题、Runtime / ContextBuilder / Policy / Schema。

### 1.1 S5 历史使用判据

| 项目 | 原判据（预注册） | 修正后判据 |
|---|---|---|
| 判定对象 | S5-B / S5-C 的回答 | S5-B / S5-C 的回答 |
| 必须出现 | 逐字包含合成标记 `【S5-HISTORY】` | 原句内容出现：连续子串`我原来以为慢启动慢是因为它每次只把窗口加一`，或同时包含「慢启动」「每次只」「加一」三个关键词 |
| 理由 | 合成标记不是用户真实说过的话 | 验证目标是「历史被使用」；原句内容出现即可证明，模型省略人工括号标记是合理行为 |
| 可执行匹配 | `marker in answer` | `S5_EXPECTED_ORIGINAL in answer` 或 `all(k in answer for k in ('慢启动','每次只','加一'))` |

- 判定函数：`full_common.s5_history_use_check`；重跑时完整回答保存在隔离 temp 的 `rerun-results.json` 中逐字匹配，不写回业务库。

### 1.2 Citation 一致性判据

| 项目 | 原判据（预注册） | 修正后判据 |
|---|---|---|
| 判定对象 | 3 次 repeat 的 citation 集合 | 3 次 repeat 的 citation 集合 |
| 必须满足 | 3 次集合严格一致 | ① 每次至少 1 条引用；② 全部可溯源；③ 全部指向本场景同一文档；④ 并集 ≥ 2 个 chunk；⑤ 平均覆盖率 ≥ 2/3 且每轮覆盖率 ≥ 1/3，或 ≥ 2 次集合完全相同 |
| 覆盖率定义 | 无 | `U = C1 ∪ C2 ∪ C3`，`c_r = \|C_r\| / \|U\|` |
| 理由 | temperature=0 下仍存在供应商侧非确定性 | 关键要求是「引用可溯源、不伪造」；2~4 条引用波动不等于系统缺陷 |
| 可执行匹配 | `len(set(citationSets)) == 1` | `full_common.citation_consistency_check(records, document_id)` |

- 判据边界：不允许空引用集合、不允许跨文档、不允许不可溯源引用；Manifest / Context Hash / prompt_tokens 的一致性判据保持不变。

## 2. 重跑环境与范围

| 项目 | 值 |
|---|---|
| 旧快照状态 | 原隔离库已按 cleanup 清理，无法原地复用 |
| 处理方式 | 用同一 setup 脚本重新建立隔离快照，再重跑受影响组合 |
| 新快照时间 | 2026-11-09 22:13:27 |
| 新隔离库 | phase5_pilot_20261109221325_f4fa5a |
| 生成参数指纹 | 2e1405ed23bbe132b96b018c7e79d190321e43ff5ffb5c5108e99bee47edb5f6 |
| LLM 模型 | deepseek-flash |
| temperature | 0 |
| 重跑组合 | S5-A,S5-C,S5-B,S6-A,S6-B,S8-B,S10-B |

- Context Hash 说明：原快照的库已删除，重跑使用新隔离快照；Context Hash 可能因快照时间等元数据变化而与原批次不同。本报告把新快照内的 3 次 repeat 一致性作为硬判据，并将新哈希与原哈希并列展示（见第 3、4 节）。
- 同一问题、同一预置数据、同一模型参数；S5-B/C 重跑仍使用 `AILS_LLM_TEMPERATURE=0` 子进程注入。

## 3. 重跑逐次结果

| key | run_id | request | finish | prompt/completion/total | citation 集合 | Context Hash | Manifest(12) | 原始 problems |
|---|---|---|---|---|---|---|---|---|
| S5-A-1 | 1 | eede36d9 | stop | 1843/682/2525 | [[5,13],[5,14],[5,15]] | a2e4a16d42dc66044a7289074a024c828af224646da7fa3e671b1c0e2d7fa584 | 41df9bd8a5a3 | OK |
| S5-A-2 | 3 | 89c6e0b2 | stop | 1843/883/2726 | [[5,13],[5,14],[5,15]] | a2e4a16d42dc66044a7289074a024c828af224646da7fa3e671b1c0e2d7fa584 | 41df9bd8a5a3 | OK |
| S5-A-3 | 5 | 469589af | stop | 1843/822/2665 | [[5,15]] | a2e4a16d42dc66044a7289074a024c828af224646da7fa3e671b1c0e2d7fa584 | 41df9bd8a5a3 | OK |
| S5-C-1 | 7 | 90d052d1 | stop | 2906/861/3767 | [[5,13],[5,14],[5,15]] | 24d30da3edd17bf67e2834fb9e92e7ba69fc6d5f227236e92e44b34921b390b0 | 3a9983ecff04 | 原始判据 FAIL(1) |
| S5-C-2 | 9 | 68ba25de | stop | 2906/1275/4181 | [[5,13],[5,14],[5,15]] | 24d30da3edd17bf67e2834fb9e92e7ba69fc6d5f227236e92e44b34921b390b0 | 3a9983ecff04 | 原始判据 FAIL(1) |
| S5-C-3 | 11 | a11c74c6 | stop | 2906/1067/3973 | [[5,15]] | 24d30da3edd17bf67e2834fb9e92e7ba69fc6d5f227236e92e44b34921b390b0 | 3a9983ecff04 | 原始判据 FAIL(1) |
| S5-B-1 | 13 | c6770e6c | stop | 2906/1222/4128 | [[5,13],[5,14],[5,15]] | 24d30da3edd17bf67e2834fb9e92e7ba69fc6d5f227236e92e44b34921b390b0 | 60594f8161ff | 原始判据 FAIL(1) |
| S5-B-2 | 15 | 6131d2dd | stop | 2992/1162/4154 | [[5,13],[5,15]] | 6fbc1bd65c7fc6003aa2ef7711d9d848b7da1b0c639f4bbf04a505251c135ff9 | 60594f8161ff | 原始判据 FAIL(2) |
| S5-B-3 | 17 | 49c05fcb | stop | 2906/1350/4256 | [[5,13],[5,14],[5,15]] | 24d30da3edd17bf67e2834fb9e92e7ba69fc6d5f227236e92e44b34921b390b0 | 60594f8161ff | 原始判据 FAIL(1) |
| S6-A-1 | 19 | 91ad2eab | stop | 1822/1358/3180 | [[6,16],[6,17],[6,18]] | f3fc2b67604f86e00c74bd11331132a0041a73d84eb04449b212d2c5cf88af47 | e24e0c72c565 | OK |
| S6-A-2 | 21 | 99c85416 | stop | 1822/1215/3037 | [[6,16],[6,17],[6,18]] | f3fc2b67604f86e00c74bd11331132a0041a73d84eb04449b212d2c5cf88af47 | e24e0c72c565 | OK |
| S6-A-3 | 23 | c895d1e8 | stop | 1822/1018/2840 | [[6,16],[6,17],[6,18]] | f3fc2b67604f86e00c74bd11331132a0041a73d84eb04449b212d2c5cf88af47 | e24e0c72c565 | OK |
| S6-B-1 | 25 | 132d9707 | stop | 2752/1436/4188 | [[6,17],[6,18]] | 665b335e52597cbf2211ee83c8c3fccc4a090a9f93730a7a774ea1686b2ae055 | 9de45baca76b | OK |
| S6-B-2 | 27 | c9e504da | stop | 2752/903/3655 | [[6,16],[6,17],[6,18]] | 665b335e52597cbf2211ee83c8c3fccc4a090a9f93730a7a774ea1686b2ae055 | 9de45baca76b | OK |
| S6-B-3 | 29 | 6c7747b5 | stop | 2752/930/3682 | [[6,16],[6,17],[6,18]] | 665b335e52597cbf2211ee83c8c3fccc4a090a9f93730a7a774ea1686b2ae055 | 9de45baca76b | OK |
| S8-B-1 | 31 | 03c8e4a9 | stop | 2833/1287/4120 | [[8,22],[8,23],[8,24]] | 9ad86e1539e7152009986976d6fc5e2c30faced590d601c134ab05669f3fbdba | 9de7400bd507 | OK |
| S8-B-2 | 33 | 63780383 | stop | 2833/978/3811 | [[8,22],[8,23],[8,24]] | 9ad86e1539e7152009986976d6fc5e2c30faced590d601c134ab05669f3fbdba | 9de7400bd507 | OK |
| S8-B-3 | 35 | 87d8c100 | stop | 2833/811/3644 | [[8,22],[8,23],[8,24]] | 9ad86e1539e7152009986976d6fc5e2c30faced590d601c134ab05669f3fbdba | 9de7400bd507 | OK |
| S10-B-1 | 37 | 3be0d1e6 | stop | 3430/1891/5321 | [[10,28],[10,29],[10,30],[10,31]] | adf9d0232ddd6a58b5975a57e7b0660a3119e9957f431a7d48c18ae711d09df5 | 3f8c06016d21 | OK |
| S10-B-2 | 39 | cc547877 | stop | 3430/3145/6575 | [[10,28],[10,29],[10,30],[10,31]] | adf9d0232ddd6a58b5975a57e7b0660a3119e9957f431a7d48c18ae711d09df5 | 3f8c06016d21 | OK |
| S10-B-3 | 41 | c7697a2a | stop | 3430/1973/5403 | [[10,28],[10,29],[10,30],[10,31]] | adf9d0232ddd6a58b5975a57e7b0660a3119e9957f431a7d48c18ae711d09df5 | 3f8c06016d21 | OK |

- 「原始 problems」列保留预注册判据下的运行期结果；S5-B/C 的旧标记问题在新判据中重新判定（见第 4 节），原始 `problems` 未从 `rerun-results.json` 删除。

## 4. 新判据逐项判定

| 场景-策略 | 总判定 | citation 覆盖率 | 溯源 | 文档 | citation 判据 | 失败项 |
|---|---|---|---|---|---|---|
| S5-A | PASS | avg=0.778 per-rep=[1.0, 1.0, 0.333] | 可溯源 | 同文档 | PASS | - |
| S5-C | FAIL | avg=0.778 per-rep=[1.0, 1.0, 0.333] | 可溯源 | 同文档 | PASS | S5-C-2：S5 回答不含原句内容或等价语义 |
| S5-B | FAIL | avg=0.889 per-rep=[1.0, 0.667, 1.0] | 可溯源 | 同文档 | PASS | 一致性：contextHash 不一致 ['24d30da3edd17bf67e2834fb9e92e7ba69fc6d5f227236e92e44b34921b390b0', '6fbc1bd65c7fc6003aa2ef7711d9d848b7da1b0c639f4bbf04a505251c135ff9', '24d30da3edd17bf67e2834fb9e92e7ba69fc6d5f227236e92e44b34921b390b0']; 一致性：promptTokens 不一致 [2906, 2992, 2906] |
| S6-A | PASS | avg=1.000 per-rep=[1.0, 1.0, 1.0] | 可溯源 | 同文档 | PASS | - |
| S6-B | PASS | avg=0.889 per-rep=[0.667, 1.0, 1.0] | 可溯源 | 同文档 | PASS | - |
| S8-B | PASS | avg=1.000 per-rep=[1.0, 1.0, 1.0] | 可溯源 | 同文档 | PASS | - |
| S10-B | PASS | avg=1.000 per-rep=[1.0, 1.0, 1.0] | 可溯源 | 同文档 | PASS | - |

### 4.1 S5 历史使用（修正后判据）

| key | 结果 | 原句连续子串 | 关键词 | 等价语义 |
|---|---|---|---|---|
| S5-C-1 | PASS | 是 | 慢启动=Y,每次只=Y,加一=Y | 是 |
| S5-C-2 | FAIL | 否 | 慢启动=Y,每次只=N,加一=Y | 否 |
| S5-C-3 | PASS | 是 | 慢启动=Y,每次只=Y,加一=Y | 是 |
| S5-B-1 | PASS | 是 | 慢启动=Y,每次只=Y,加一=Y | 是 |
| S5-B-2 | PASS | 否 | 慢启动=Y,每次只=Y,加一=Y | 是 |
| S5-B-3 | PASS | 是 | 慢启动=Y,每次只=Y,加一=Y | 是 |

### 4.2 Citation 逐组合明细

- **S5-A**（必需）：sets=[[[5, 13], [5, 14], [5, 15]], [[5, 13], [5, 14], [5, 15]], [[5, 15]]]；union=[[5, 13], [5, 14], [5, 15]]；平均覆盖率=0.778；完全相同次数=2；结论=PASS。
- **S5-C**（必需）：sets=[[[5, 13], [5, 14], [5, 15]], [[5, 13], [5, 14], [5, 15]], [[5, 15]]]；union=[[5, 13], [5, 14], [5, 15]]；平均覆盖率=0.778；完全相同次数=2；结论=PASS。
- **S5-B**（附加观测）：sets=[[[5, 13], [5, 14], [5, 15]], [[5, 13], [5, 15]], [[5, 13], [5, 14], [5, 15]]]；union=[[5, 13], [5, 14], [5, 15]]；平均覆盖率=0.889；完全相同次数=2；结论=PASS。
- **S6-A**（必需）：sets=[[[6, 16], [6, 17], [6, 18]], [[6, 16], [6, 17], [6, 18]], [[6, 16], [6, 17], [6, 18]]]；union=[[6, 16], [6, 17], [6, 18]]；平均覆盖率=1.000；完全相同次数=3；结论=PASS。
- **S6-B**（必需）：sets=[[[6, 17], [6, 18]], [[6, 16], [6, 17], [6, 18]], [[6, 16], [6, 17], [6, 18]]]；union=[[6, 16], [6, 17], [6, 18]]；平均覆盖率=0.889；完全相同次数=2；结论=PASS。
- **S8-B**（必需）：sets=[[[8, 22], [8, 23], [8, 24]], [[8, 22], [8, 23], [8, 24]], [[8, 22], [8, 23], [8, 24]]]；union=[[8, 22], [8, 23], [8, 24]]；平均覆盖率=1.000；完全相同次数=3；结论=PASS。
- **S10-B**（必需）：sets=[[[10, 28], [10, 29], [10, 30], [10, 31]], [[10, 28], [10, 29], [10, 30], [10, 31]], [[10, 28], [10, 29], [10, 30], [10, 31]]]；union=[[10, 28], [10, 29], [10, 30], [10, 31]]；平均覆盖率=1.000；完全相同次数=3；结论=PASS。

### 4.3 Manifest / Context Hash / prompt_tokens

| 场景-策略 | 结果 | 说明 |
|---|---|---|
| S5-A | PASS | - |
| S5-C | PASS | - |
| S5-B | FAIL | 一致性：contextHash 不一致 ['24d30da3edd17bf67e2834fb9e92e7ba69fc6d5f227236e92e44b34921b390b0', '6fbc1bd65c7fc6003aa2ef7711d9d848b7da1b0c639f4bbf04a505251c135ff9', '24d30da3edd17bf67e2834fb9e92e7ba69fc6d5f227236e92e44b34921b390b0']; 一致性：promptTokens 不一致 [2906, 2992, 2906] |
| S6-A | PASS | - |
| S6-B | PASS | - |
| S8-B | PASS | - |
| S10-B | PASS | - |

### 4.4 运行期观测项（不构成本批次失败判据）

| 场景-策略 | 观测 |
|---|---|
| S5-B | S5-B-2：单轮对话出现 2 次 LLM 调用（预期 1） |

- S5-B-2 的第二次 LLM 调用来自产品内置 `HISTORY_GUARD` 重新生成：首答声称有「两条记录」而上下文只有一条，guard 触发重写，最终回答 `finishReason=stop`、无截断、无空输出。该行为不是 error/timeout/空输出/截断，故不计为本批次硬失败；但它使本批 LLM 实际调用数变为 22（预算 21/24），列为决策点。

新旧 Context Hash 对照（仅供观测，不参与 PASS/FAIL）：

| 场景-策略 | 原 Context Hash | 新 Context Hash |
|---|---|---|
| S5-A | a2e4a16d42dc66044a7289074a024c828af224646da7fa3e671b1c0e2d7fa584 | a2e4a16d42dc66044a7289074a024c828af224646da7fa3e671b1c0e2d7fa584 |
| S5-C | ba83568ae5cf0259e213f6ec183767b73e82b1e4ac57efa0b7eb4169da80a01e | 24d30da3edd17bf67e2834fb9e92e7ba69fc6d5f227236e92e44b34921b390b0 |
| S5-B | ba83568ae5cf0259e213f6ec183767b73e82b1e4ac57efa0b7eb4169da80a01e | 24d30da3edd17bf67e2834fb9e92e7ba69fc6d5f227236e92e44b34921b390b0,6fbc1bd65c7fc6003aa2ef7711d9d848b7da1b0c639f4bbf04a505251c135ff9 |
| S6-A | f3fc2b67604f86e00c74bd11331132a0041a73d84eb04449b212d2c5cf88af47 | f3fc2b67604f86e00c74bd11331132a0041a73d84eb04449b212d2c5cf88af47 |
| S6-B | 5ee36741029b9cc27114ed0bf2e6eb73e5146e27d45d45adddf5c0b8c1e10c32 | 665b335e52597cbf2211ee83c8c3fccc4a090a9f93730a7a774ea1686b2ae055 |
| S8-B | 072a02bf9dd2d7797ace4800b7cf96cf6c206bb615b4d66768336c20c19d0cab | 9ad86e1539e7152009986976d6fc5e2c30faced590d601c134ab05669f3fbdba |
| S10-B | 22356af77c853da16f0e8ebd43807f09976aaa30a89038f9a144cfc32f24f0a1 | adf9d0232ddd6a58b5975a57e7b0660a3119e9957f431a7d48c18ae711d09df5 |

## 5. 原 12 条失败的最终归因

| key | 原失败项 | 归因 | 修正判据复核 | 复核结果 | 证据 |
|---|---|---|---|---|---|
| S5-B-1 | 回答缺少预期历史标记：【S5-HISTORY】 | 判据设计（合成标记不是用户原话） | 原句内容出现 | PASS | 原 300 字预览已含原句：是；重跑完整回答见新判据逐项判定 |
| S5-B-2 | 回答缺少预期历史标记：【S5-HISTORY】 | 判据设计（合成标记不是用户原话） | 原句内容出现 | PASS | 原 300 字预览已含原句：是；重跑完整回答见新判据逐项判定 |
| S5-B-3 | 回答缺少预期历史标记：【S5-HISTORY】 | 判据设计（合成标记不是用户原话） | 原句内容出现 | PASS | 原 300 字预览已含原句：是；重跑完整回答见新判据逐项判定 |
| S5-C-1 | 回答缺少预期历史标记：【S5-HISTORY】 | 判据设计（合成标记不是用户原话） | 原句内容出现 | PASS | 原 300 字预览已含原句：是；重跑完整回答见新判据逐项判定 |
| S5-C-2 | 回答缺少预期历史标记：【S5-HISTORY】 | 判据设计（合成标记不是用户原话） | 原句内容出现 | PASS | 原 300 字预览已含原句：是；重跑完整回答见新判据逐项判定 |
| S5-C-3 | 回答缺少预期历史标记：【S5-HISTORY】 | 判据设计（合成标记不是用户原话） | 原句内容出现 | PASS | 原 300 字预览已含原句：是；重跑完整回答见新判据逐项判定 |
| S5-A | 一致性：citations 不一致：['[[5, 13], [5, 14], [5, 15]]', '[[5, 15]]', '[[5, 13], [5, 14], [5, 15]]'] | 判据设计（temperature=0 下 LLM 输出波动，引用全部可溯源） | 平均覆盖率 0.778，每轮 [1.0, 0.333, 1.0]，可溯源=True，同文档=True | PASS | 原结果中所有引用均可溯源；重跑 3 次见新判据逐项判定 |
| S5-C | 一致性：citations 不一致：['[[5, 13], [5, 14], [5, 15]]', '[[5, 13], [5, 15]]', '[[5, 13], [5, 14], [5, 15]]'] | 判据设计（temperature=0 下 LLM 输出波动，引用全部可溯源） | 平均覆盖率 0.889，每轮 [1.0, 0.667, 1.0]，可溯源=True，同文档=True | PASS | 原结果中所有引用均可溯源；重跑 3 次见新判据逐项判定 |
| S6-A | 一致性：citations 不一致：['[[6, 16], [6, 17], [6, 18]]', '[[6, 17], [6, 18]]', '[[6, 17], [6, 18]]'] | 判据设计（temperature=0 下 LLM 输出波动，引用全部可溯源） | 平均覆盖率 0.778，每轮 [1.0, 0.667, 0.667]，可溯源=True，同文档=True | PASS | 原结果中所有引用均可溯源；重跑 3 次见新判据逐项判定 |
| S6-B | 一致性：citations 不一致：['[[6, 17], [6, 18]]', '[[6, 16], [6, 17], [6, 18]]', '[[6, 16], [6, 17]]'] | 判据设计（temperature=0 下 LLM 输出波动，引用全部可溯源） | 平均覆盖率 0.778，每轮 [0.667, 1.0, 0.667]，可溯源=True，同文档=True | PASS | 原结果中所有引用均可溯源；重跑 3 次见新判据逐项判定 |
| S8-B | 一致性：citations 不一致：['[[8, 23], [8, 24]]', '[[8, 22], [8, 23], [8, 24]]', '[[8, 22], [8, 23], [8, 24]]'] | 判据设计（temperature=0 下 LLM 输出波动，引用全部可溯源） | 平均覆盖率 0.889，每轮 [0.667, 1.0, 1.0]，可溯源=True，同文档=True | PASS | 原结果中所有引用均可溯源；重跑 3 次见新判据逐项判定 |
| S10-B | 一致性：citations 不一致：['[[10, 28], [10, 29], [10, 30]]', '[[10, 28], [10, 29], [10, 30], [10, 31]]', '[[10, 28], [10, 29], [10, 30], [10, 31]]'] | 判据设计（temperature=0 下 LLM 输出波动，引用全部可溯源） | 平均覆盖率 0.917，每轮 [0.75, 1.0, 1.0]，可溯源=True，同文档=True | PASS | 原结果中所有引用均可溯源；重跑 3 次见新判据逐项判定 |

- 逐条确认：12 条失败中，12 条归因为判据设计问题，0 条归因为真实系统缺陷。
- S5 的 6 条：合成标记 `【S5-HISTORY】` 不属于用户真实话语；原 6 次回答的 300 字预览均含原句内容；重跑 6 次中 5 次含原句，S5-C-2 不含（见第 8 节），因此重跑仍记为一条新失败。
- Citation 的 6 条：原结果中所有引用均可溯源、均指向本场景文档；差异仅为 2~4 条引用条数波动，重跑在修正判据下全部达标。

## 6. 成本记录

| 项目 | 数值 |
|---|---|
| 对话调用次数（记录） | 21 |
| 模型实际调用次数（含 HISTORY_GUARD 重生成） | 22 |
| prompt tokens（仅最终记录） | 55562 |
| completion tokens（仅最终记录） | 26269 |
| total tokens（仅最终记录） | 81831 |
| Usage=UNKNOWN 的调用 | 0 |
| Agent 进程内 Embedding 调用 | 36 |
| Agent 进程内 Embedding 文本数 | 36 |
| 单次运行 Embedding 最大值 | 2 |
| 单次请求文本数（上界） | 1 |
| Tavily 调用 | 0 |

- 单次运行 Embedding 上限 3，单次请求文本数上限 10，本批实测单次运行最大 2、单次请求文本数上界 1，均满足运行期硬约束。
- 成本口径说明：任务预估「Embedding ≤ 30 次」按每场景 2 次估算；本批实际 36 次（21 次对话中 S5-A/S6-A 仅需 1 次，其余 2 次）。该差异来自按对话实际检索路径计数，不改生产行为，列为本报告决策点。

## 7. 冻结与清理

| 检查 | 结果 | 说明 |
|---|---|---|
| MySQL 全局冻结 | PASS | 无漂移 |
| Qdrant 文档 + Memory | PASS | 无漂移 |
| 默认 Memory collection | PASS | 未读写 |

| 项目 | 结果 |
|---|---|
| cleanup.py exitCode | 0 |
| MySQL 库残留 | 0 |
| Qdrant 残留 | [] |
| RabbitMQ 队列残留 | [] |
| RabbitMQ 交换机残留 | [] |
| 仍开放的端口 | [] |
| 独立残留检查 | PASS |

## 8. 结论

- 修正判据下仍有失败：['S5-C', 'S5-B']；Phase 5 Step 3 结论为 **FAIL**。
- 失败明细：S5-C：S5-C-2：S5 回答不含原句内容或等价语义；S5-B：一致性：contextHash 不一致 ['24d30da3edd17bf67e2834fb9e92e7ba69fc6d5f227236e92e44b34921b390b0', '6fbc1bd65c7fc6003aa2ef7711d9d848b7da1b0c639f4bbf04a505251c135ff9', '24d30da3edd17bf67e2834fb9e92e7ba69fc6d5f227236e92e44b34921b390b0']; 一致性：promptTokens 不一致 [2906, 2992, 2906]
- 具体到 S5-C-2：3 次 repeat 的上下文（history / trajectory / understanding）完全一致，但该次回答引用了 Understanding 中的另一句「我原来以为慢启动是每次加一，其实是起点小」，没有出现历史原句「我原来以为慢启动慢是因为它每次只把窗口加一」，也不满足「慢启动 / 每次只 / 加一」三关键词规则。这是 temperature=0 下的 LLM 输出波动，不是上下文注入缺失（Manifest 已确认 `history:1`、`history:2` 均被注入）。
- 具体到 S5-B-2：首答声称「两条记录」而上下文只有一条，产品内置 `HISTORY_GUARD` 触发了重新生成；第二次调用在 system prompt 中追加了 guard 指令，因此最终记录的 systemSha256 / chars / prompt_tokens 与 其余两次不同（Manifest 本身三次相同）。按本批次「重跑中出现新的 Manifest/Hash 不一致」的失败判据，S5-B 记为 FAIL；这不是上下文组装随机性，而是产品侧重生成路径的确定性影响，需要人工确认它是否应作为可接受的运行期行为。
- 因此本批次不能宣布 Phase 5 全量实验 PASS。可选处置需要人工决策：（a）对 S5-B / S5-C 追加 repeat 后按新样本判定；（b）明确接受 HISTORY_GUARD 重生成导致的 Hash 差异，并调整判据；（c）明确把 S5 判据改为「3 次中至少 2 次出现原句」的组合级规则并重跑；（d）维持当前结果。以上任一处置都需重新记录，不能直接沿用本结果宣布 PASS。
- 说明：这是对预注册判据的事后修正；修正理由、可执行规则、新旧对照与受影响场景重跑均已在本文档公开记录，未隐藏。

## 9. 工作区状态

| 项目 | 值 |
|---|---|
| branch | main |
| HEAD | efe29ae5feaeec4141d4a7fb8cfb776064abf266 |
| HEAD subject | docs(phase5): 全量 90 次 A/B/C 实验完成，预注册判据 FAIL，12 条失败均为判据设计/LLM 波动 |
| 工作区状态 | M docs/testing/phase5-abc-results.md  M scripts/phase5/full/full_common.py  M scripts/phase5/full/run_full_abc.py ?? docs/testing/phase5-abc-rerun.md ?? scripts/phase5/full/run_rerun.py |
| 是否 commit | 否（本批次未执行 git commit） |
| 是否 push | 否（本批次未执行 git push） |

## 10. 需要人工审核的决策点

1. **判据修正的效力**：本次为事后修正判据并重跑受影响场景；需要人工确认该修正是否可作为 Phase 5 Step 3 的最终判据。
2. **S5 等价语义规则**：同时包含「慢启动」「每次只」「加一」三个关键词即视为原句内容出现；该规则可能被知识侧表述命中，需确认是否接受。
3. **Citation 覆盖率阈值**：平均 ≥ 2/3 且每轮 ≥ 1/3（或 ≥2 次集合相同）。本批全部达标；阈值是否合适需要人工确认。
4. **Embedding 统计口径**：任务预估 ≤30 次，实测为 36 次；该差异来自实际检索路径，需确认是否接受。
5. **快照不同**：原隔离库已清理，重跑使用新快照；Context Hash 可能与原批次不同，组内 3 次一致性已单独验证。
6. **完整回答留存范围**：本批次把完整回答写入隔离 temp 的 `rerun-results.json` 用于逐字判定，未写回业务库；需确认留存边界。
7. **重跑新增两条失败**：S5-C-2 未出现历史原句；S5-B-2 因 `HISTORY_GUARD` 重生成导致 Context Hash / prompt_tokens 与同组另外两次不一致。需要人工决定是追加 repeat、接受重生成路径、改为组合级规则，还是维持 FAIL；在决策前不得宣布 Phase 5 PASS。
8. **LLM 调用数 22 而非 21**：S5-B-2 触发一次 `HISTORY_GUARD` 重新生成。仍在上限 24 内，但需确认该运行期行为是否可接受。

## 11. S5 数据重设计与 HISTORY_GUARD 判据调整（追加）

- 追加时间：2026-11-09T22:57:14
- 重跑快照时间：2026-11-09 22:47:53
- 隔离库：`phase5_pilot_20261109224751_5cdc2f`
- 文档 collection：`phase5-pilot-20261109224751-5cdc2f_text-embedding-v4_1024`
- Memory collection：`phase5-pilot-20261109224751-5cdc2f-memory_text-embedding-v4_1024`
- 重跑范围：S5-A / S5-C / S5-B 各 3 次，共 9 次对话；记录到 9 次。
- 生成参数指纹：`2e1405ed23bbe132b96b018c7e79d190321e43ff5ffb5c5108e99bee47edb5f6`；model=`deepseek-flash`，temperature=`0`。

### 11.1 S5 预置 Understanding 重设计

旧版 Understanding 与历史原句在措辞上高度重叠，导致模型引用 Understanding 时无法区分「是否使用了历史」。本批次只改 S5 的 Understanding，历史原句、问题、其他场景与判据匹配规则均未修改。

| 字段 | 旧版（会与历史原句混淆） | 新版（与历史原句可区分） |
|---|---|---|
| summary | 【S5-U】用户原先误以为慢启动每次只加一；现已理解慢启动从 cwnd=1 开始指数增长。 | 【S5-U】用户曾把慢启动理解为线性增长，现已理解它是指数增长。 |
| misconceptions | 曾认为慢启动每次只把窗口加一 | 曾把慢启动的增长方式理解为线性递增 |
| explanation | 【S5-U】我原来以为慢启动是每次加一，其实是起点小。 | 【S5-U】我原以为慢启动增长很慢，后来明白是起点小、呈指数增长。 |
| 历史原句（不变） | 【S5-HISTORY】我原来以为慢启动慢是因为它每次只把窗口加一。 | 【S5-HISTORY】我原来以为慢启动慢是因为它每次只把窗口加一。 |

**可判定性论证**：

1. 判据的匹配对象是原句连续子串「我原来以为慢启动慢是因为它每次只把窗口加一」，或「慢启动」「每次只」「加一」三关键词同时出现。
2. 新版 Understanding 的 summary / misconceptions / explanation 均不含「每次只」，也不含原句的连续子串，因此引用 Understanding 不会命中正向判据。
3. 历史原句本身仍逐字保留在 history 中；B/C 回答引用它时必然命中。
4. 未改动 `s5_history_use_check` 的匹配规则，只改数据。

**未改动的部分**：S5 的 revisions、memory、question 与其他场景数据均未修改。revision 文本中仍可能出现「每次只」等词，因此正向关键词判据在理论上仍可能被 revision 引用命中；这一点列为人工审核决策点。

### 11.2 HISTORY_GUARD 判据调整

| 项目 | 原判据 | 调整后判据 |
|---|---|---|
| 记录字段 | 仅通过「单轮出现 2 次 LLM 调用」间接看出 | 结果 JSON 增加 `historyGuardTriggered`（布尔）、`llmCallCount`、`llmCalls`（逐次 system 指纹/长度/usage） |
| Hash 一致性 | 3 次 `contextHash` / `promptTokens` 必须完全一致 | 未触发守卫的 repeat 之间必须一致；`historyGuardTriggered=true` 的 repeat 豁免 `contextHash` / `systemSha256` / `systemChars` / `promptTokens`，但必须在结果中逐条标注 |
| Manifest | 3 次一致 | 保持 3 次一致（不豁免） |
| 边界 | 无豁免 | 只豁免守卫触发的运行，不放宽为「Hash 可以随意不同」；3 次均未触发守卫时仍要求完全一致 |

- 判定函数：`full_common.guard_aware_hash_consistency`；守卫触发检测：`one_call` 观察到同一轮对话出现多次 LLM 调用，本运行期唯一的多调用路径是产品内置 HISTORY_GUARD 重生成。
- 全量观测缺口：90 次全量的 HISTORY_GUARD 触发次数未知：原批次结果 JSON 未记录 `historyGuardTriggered`，原隔离库已按 cleanup 清理，无法回溯。本报告不做回溯推测，建议下一批全量实验补记该字段。

### 11.3 S5-A/B/C 逐次结果

| key | run_id | 守卫触发 | LLM 调用数 | finish | prompt/completion/total | citation 集合 | Context Hash(12) | System Hash(12) | Manifest(12) | Manifest 关键来源 | 历史原句 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| S5-A-1 | 1 | 否 | 1 | stop | 1843/746/2589 | [[5,15]] | a2e4a16d42dc | a6a8c202e2db | 41df9bd8a5a3 | - | 未命中 |
| S5-A-2 | 3 | 否 | 1 | stop | 1843/585/2428 | [[5,13],[5,14],[5,15]] | a2e4a16d42dc | a6a8c202e2db | 41df9bd8a5a3 | - | 未命中 |
| S5-A-3 | 5 | 否 | 1 | stop | 1843/833/2676 | [[5,13],[5,14],[5,15]] | a2e4a16d42dc | a6a8c202e2db | 41df9bd8a5a3 | - | 未命中 |
| S5-C-1 | 7 | 否 | 1 | stop | 2897/2211/5108 | [[5,13],[5,14],[5,15]] | 983b915eda59 | b64e36169ed6 | 0baf3d0e0b07 | history:1,history:2,revision:6,evidence:5,revision:7 | 命中 |
| S5-C-2 | 9 | 否 | 1 | stop | 3045/1312/4357 | [[5,14],[5,15]] | ad698647046a | c1db01f6c16f | 2e3b8435300b | history:1,history:2,revision:6,evidence:5,revision:7 | 未命中 |
| S5-C-3 | 11 | 否 | 1 | stop | 2897/1622/4519 | [[5,15]] | 983b915eda59 | b64e36169ed6 | 0baf3d0e0b07 | history:1,history:2,revision:6,evidence:5,revision:7 | 命中 |
| S5-B-1 | 13 | 否 | 1 | stop | 2897/900/3797 | [[5,13],[5,15]] | 983b915eda59 | b64e36169ed6 | b51f17aeaba1 | history:1,history:2,revision:6,evidence:5,revision:7 | 命中 |
| S5-B-2 | 15 | 否 | 1 | stop | 2897/1294/4191 | [] | 983b915eda59 | b64e36169ed6 | b51f17aeaba1 | history:1,history:2,revision:6,evidence:5,revision:7 | 命中 |
| S5-B-3 | 17 | 是 | 2 | stop | 2985/1825/4810 | [[5,13],[5,14],[5,15]] | 6d0c32fecc12 | bf158d3a0df8 | b51f17aeaba1 | history:1,history:2,revision:6,evidence:5,revision:7 | 命中 |

- 历史原句列表示是否命中 `s5_history_use_check`（原句连续子串或三关键词）。S5-A 的预期是「未命中」（A 不注入 history）；S5-B/C 的预期是「命中」。

### 11.4 逐组合判定

| 场景-策略 | 总判定 | citation | 历史使用 | Manifest revision/evidence | Hash（守卫豁免后） | 本组有守卫触发 | 失败项 |
|---|---|---|---|---|---|---|---|
| S5-A | PASS | PASS | PASS | -（本策略不要求） | PASS | 否 | - |
| S5-C | FAIL | PASS | FAIL | PASS | FAIL | 否 | 一致性：manifestSha256 不一致 ['0baf3d0e0b075951cf6d19e7746b1b9317bd25445188e2c9f6bfb49375b39bfc', '2e3b8435300b2c15780d40a23758632cc181e79fd7905049e84617e508707a0d', '0baf3d0e0b075951cf6d19e7746b1b9317bd25445188e2c9f6bfb49375b39bfc']; 一致性：contextHash 在未触发守卫的 3 次之间不一致 ['983b915eda599694f6401f8f31d45f71d6c6af94d7c1dcef29aba07d0fe10cb9', 'ad698647046a288b0697385bfa22c10b97c284ee91bd121d5d7638fc10bce0e7', '983b915eda599694f6401f8f31d45f71d6c6af94d7c1dcef29aba07d0fe10cb9']; 一致性：systemSha256 在未触发守卫的 3 次之间不一致 ['b64e36169ed6401277ab9f8ca387e71ae6ea4c2248ef446de64061eadf5278ba', 'c1db01f6c16fc56a4718d75a73c0fb187a44fef0aeb962e6cf9a6b4d438dbac2', 'b64e36169ed6401277ab9f8ca387e71ae6ea4c2248ef446de64061eadf5278ba']; 一致性：systemChars 在未触发守卫的 3 次之间不一致 [4914, 5171, 4914]; S5-C-2：S5 回答不含原句内容或等价语义 |
| S5-B | FAIL | FAIL | PASS | -（本策略不要求） | PASS | 是 | citation：存在空 citation 集合; citation：覆盖率不足：average=0.556（阈值 0.667），per-rep=[0.667, 0.0, 1.0]（每轮阈值 0.333）; citation：且没有 2/3 次 citation 集合完全相同 |

**逐次历史使用判据明细**：

| key | 策略 | 判据命中 | 原句连续子串 | 关键词 | 结果 |
|---|---|---|---|---|---|
| S5-A-1 | A | 未命中 | 否 | 慢启动=Y,每次只=N,加一=Y | PASS |
| S5-A-2 | A | 未命中 | 否 | 慢启动=Y,每次只=N,加一=Y | PASS |
| S5-A-3 | A | 未命中 | 否 | 慢启动=Y,每次只=N,加一=Y | PASS |
| S5-C-1 | C | 命中 | 是 | 慢启动=Y,每次只=Y,加一=Y | PASS |
| S5-C-2 | C | 未命中 | 否 | 慢启动=Y,每次只=N,加一=Y | FAIL |
| S5-C-3 | C | 命中 | 是 | 慢启动=Y,每次只=Y,加一=Y | PASS |
| S5-B-1 | B | 命中 | 是 | 慢启动=Y,每次只=Y,加一=Y | PASS |
| S5-B-2 | B | 命中 | 是 | 慢启动=Y,每次只=Y,加一=Y | PASS |
| S5-B-3 | B | 命中 | 是 | 慢启动=Y,每次只=Y,加一=Y | PASS |

**S5-C Manifest revision / evidence**：

| key | revision 来源 | evidence 来源 | 结果 |
|---|---|---|---|
| S5-C-1 | revision:6,revision:7 | evidence:5 | PASS |
| S5-C-2 | revision:6,revision:7 | evidence:5 | PASS |
| S5-C-3 | revision:6,revision:7 | evidence:5 | PASS |

**Hash 逐字段**：

| 场景-策略 | 守卫豁免说明 |
|---|---|
| S5-B | S5-B-3：historyGuardTriggered=true，豁免 contextHash, systemSha256, systemChars, promptTokens（llmCallCount=2） |

**失败项诊断**：

- **S5-C-2 的 Hash 不一致来自记忆检索降级**：Manifest 中出现 DEGRADED 来源 `memory:5`，并因此注入了额外的 `cognitive_status_source:memory-status:DEGRADED`，使该次的 Manifest / Context / System 指纹与另外两次不同。该次未触发 HISTORY_GUARD，按调整后判据仍属 Hash 不一致 FAIL；这不是模型随机性，而是检索依赖的运行期降级。
- **S5-C-2 未命中历史原句**：回答引用了新版 Understanding（「我原以为慢启动增长很慢，后来明白是起点小、呈指数增长。」），并声称「第一次表述的原文没有被保存下来」，但该次 Manifest 实际注入了 `history:1`、`history:2`。这是回答内容错误，不是判据误判；按「任一次回答缺少原句内容」判 FAIL。
- **S5-B-2 的 citation 为空**：`citationSet=[]`，产品侧 `CITATION_CHECK` 的 `total=0`。该次未触发 HISTORY_GUARD（`historyGuardTriggered=False`），因此不是守卫重生成导致的记录丢失；回答使用了 memory/history，但没有给出任何可溯源的知识标签。按「引用判据（不变）」判 FAIL。

**旧标记判据的保留记录**（不参与本批 PASS/FAIL）：

- S5-C-1：回答缺少预期历史标记：【S5-HISTORY】
- S5-C-2：回答缺少预期历史标记：【S5-HISTORY】
- S5-C-3：回答缺少预期历史标记：【S5-HISTORY】
- S5-B-1：回答缺少预期历史标记：【S5-HISTORY】
- S5-B-2：回答缺少预期历史标记：【S5-HISTORY】
- 旧判据要求回答逐字包含合成标记 `【S5-HISTORY】`；该标记不是用户真实话语，本批判定改用原句内容。

**运行期观测项**：

- S5-B-3：单轮对话出现 2 次 LLM 调用（预期 1）

### 11.5 HISTORY_GUARD 触发分析

#### S5-B-3

| 项目 | 首次调用 | 重生成调用 |
|---|---|---|
| systemSha256 | b64e36169ed6401277ab9f8ca387e71ae6ea4c2248ef446de64061eadf5278ba | bf158d3a0df816446466786cecd4cf7066358a6cf1693c1f29736e0f43d4a588 |
| chars | 4914 | 5060 |
| promptTokens | 2897 | 2985 |

**产品 trace 中恢复的触发明细**（来自隔离 temp 的 agent.log）：

| kind | excerpt | reason |
|---|---|---|
| partial_upgrade | 完全答对 | 记录里没有完全答对的证据 |
| partial_upgrade | 答错 | 记录只判定为部分正确，不能改写成答错 |

- `regenerated=True`，`remainingIssues=[]`，`regenerationError=None`，`fallback=False`，`evidenceCount=1`。
- 触发原因（按首答原文与 trace 对照）：首答实际写的是「这是一次部分正确的解释，不是完全答错，也不是完全答对」，即明确否定「完全答对」与「答错」；但产品侧 `partial_upgrade` 校验仍按字面命中这两个短语并拒绝，疑似未处理「不是……也不是……」的否定语境。该次因此追加修复指令并重生成一次，重生成后 `remainingIssues=[]`。
- 这说明守卫本次触发更像**否定语境下的疑似误判**，而不是模型真的把「部分正确」升级成了「完全答对/答错」；需在产品侧复核校验逻辑（见 11.10 决策点 9）。
- Manifest 中注入的 history：history:1,history:2

首次回答（HISTORY_GUARD 校验前，未推送给用户）：

```text
先给原话，再说错在哪一步。

## 你当时的原话

- 本轮你自己复述的版本：「我原来以为慢启动慢是因为它每次只把窗口加一。」
- 2026-11-09 22:47 那条记录里，你后来的说法是：「我原以为慢启动增长很慢，后来明白是起点小、呈指数增长。」

两条可以对上：前一条是错误版本，后一条是修正后的版本。记录里那条证据被标注为**部分正确**，并把它概括为「把慢启动误解为线性增长，已纠正为指数增长」——所以这是一次部分正确的解释，不是完全答错，也不是完全答对。

## 分叉发生在哪一步

你的推理里藏了一个隐含前提：**「慢」这个词是在描述增长速度**。既然名字叫慢启动，增长速度就应该很慢，于是自然落到「每个 RTT 只加一」这种最慢的加法上。

分叉就在这个前提。慢启动的「慢」修饰的是**起点**，不是**速率**：初始 cwnd 通常为 1 个 MSS，每收到一个 ACK 增加一个 MSS，因此每个 RTT 大约翻倍 [资料《s5-slow-start.md》#0]。名字来自「起点很小、需要时间爬升」，而不是增长速度慢 [资料《s5-slow-start.md》#1]。

而「每个 RTT 只加一」恰恰不是慢启动的行为，那更接近拥塞避免的线性增长 [资料《s5-slow-start.md》#2]。也就是说，你当时描述的不是「慢启动慢」，而是「慢启动结束后进入的那个阶段」。

## 关于纠正过程，能确认与不能确认的

能核对的是：从「每次只把窗口加一」这个说法，到记录里被标注为误区，再到后来的表述变成「起点小、呈指数增长」；同一个时间点还有一条较早的学习状态记录写着「曾把慢启动理解为线性加一」，那是更早的记录，当前状态里已经把你列为理解「从 cwnd=1 开始」和「指数增长」这两点。

不能确认的是：**记录里看不出是什么让你改变看法的**——是重读材料、是自己回推 ACK 的累加，还是别的途径，都没有留下依据，我不替这段补原因。如果你自己还记得当时是怎么转过弯的，可以说说，那对后面理清切换到拥塞避免的时机更有用。
```

重生成回答（最终交付）：

```text
## 你当时的原话

直接可核对的有两处：

**本轮你自己写的**（`【S5-HISTORY】`）：

> 我原来以为慢启动慢是因为它每次只把窗口加一。

**2026-11-09 22:47 记录下的那句**（`【S5-U】`）：

> 我原以为慢启动增长很慢，后来明白是起点小、呈指数增长。

另外，同一条时间戳下还有一条证据条目，它是对你那句解释的**概括**，不是逐字引用：内容为「用户把『慢启动』误解为线性增长，已纠正为指数增长」，标注是**部分正确 / 用户解释**。

## 具体问题在哪

问题出在**把「慢」的原因归到了增长速度上**，而不是把增长曲线写错这么简单。

- 「每个 RTT 只加一」这种描述并不是凭空错，因为它确实是一种 TCP 行为，只不过那是**拥塞避免**阶段的线性增长，不是慢启动 [资料《s5-slow-start.md》#2]。
- 慢启动里「慢」指的是起点只有 1 个 MSS，每个 RTT 大约翻倍，属于指数增长 [资料《s5-slow-start.md》#0]；名字来自「起点很小、需要时间爬升」，与增速无关 [资料《s5-slow-start.md》#1]。

所以用一句话概括这两者：**「慢」用来描述起点低（也就是「从很小的地方开始」），不用来描述增速慢。**

## 关于「怎么纠正的」

这一点记录只支持到很有限的程度：能确认的是同一时间点那条证据标注为**部分正确**，并记下了从「线性增长」到「指数增长」的变化。**纠正的原因、方式，以及有没有借助别的材料，记录里都看不出来，无法确认**，我不做补充。

另外说明一下状态口径：你现在的理解里，**慢启动从 cwnd=1 开始**和**慢启动阶段指数增长**都在已掌握范围内；而「曾把慢启动的增长方式理解为线性递增」这一条，在记录里仍然挂在**当前存在的误区**条目下（措辞用的是「曾」）。也就是说，两条记录是并列保留的，不是一条覆盖另一条。
```

- 说明：产品侧 `HISTORY_GUARD` 的 issue 明细只写入进程内 trace，未持久化到 `agent_run`；本 harness 能确定的是「同一轮对话发生了第二次 LLM 调用，且第二次 system 指纹/长度变化」，与追加重生成指令一致。首答全文来自 harness 对 LLM 流的只读记录，仅存于隔离 temp。

### 11.6 成本记录

| 项目 | 数值 |
|---|---|
| 对话调用次数（记录） | 9 |
| 模型实际调用次数（含守卫重生成） | 10 |
| 触发守卫的对话数 | 1 |
| prompt tokens（仅最终记录） | 23147 |
| completion tokens（仅最终记录） | 11328 |
| total tokens（仅最终记录） | 34475 |
| Agent 进程内 Embedding 调用 | 15 |
| Agent 进程内 Embedding 文本数 | 15 |
| 单次运行 Embedding 最大值 | 2 |
| Tavily 调用 | 0 |

- 预算核对：LLM 记录 9 次（上限 9），含守卫重生成 10 次（上限 12）；Agent Embedding 15 次（上限 18，按每场景 6 次计）；Tavily 0。
- setup 阶段 10 份文档的索引 Embedding 由 Spring 侧发起，不属于每次对话的 Agent 预算。

### 11.7 冻结与清理

| 检查 | 结果 | 说明 |
|---|---|---|
| MySQL 全局冻结 | PASS | 无漂移 |
| Qdrant 文档 + Memory | PASS | 无漂移 |
| 默认 Memory collection | PASS | 未读写 |

| 项目 | 结果 |
|---|---|
| cleanup.py exitCode | 0 |
| MySQL 库残留 | 0 |
| Qdrant 残留 | [] |
| RabbitMQ 队列残留 | [] |
| RabbitMQ 交换机残留 | [] |
| 仍开放的端口 | [] |
| 独立残留检查 | PASS |

### 11.8 修正后 Phase 5 Step 3 结论

- 失败组合：['S5-C', 'S5-B']；Phase 5 Step 3 结论为 **FAIL**。
- S5-A：PASS；无失败项
- S5-C：FAIL；一致性：manifestSha256 不一致 ['0baf3d0e0b075951cf6d19e7746b1b9317bd25445188e2c9f6bfb49375b39bfc', '2e3b8435300b2c15780d40a23758632cc181e79fd7905049e84617e508707a0d', '0baf3d0e0b075951cf6d19e7746b1b9317bd25445188e2c9f6bfb49375b39bfc']; 一致性：contextHash 在未触发守卫的 3 次之间不一致 ['983b915eda599694f6401f8f31d45f71d6c6af94d7c1dcef29aba07d0fe10cb9', 'ad698647046a288b0697385bfa22c10b97c284ee91bd121d5d7638fc10bce0e7', '983b915eda599694f6401f8f31d45f71d6c6af94d7c1dcef29aba07d0fe10cb9']; 一致性：systemSha256 在未触发守卫的 3 次之间不一致 ['b64e36169ed6401277ab9f8ca387e71ae6ea4c2248ef446de64061eadf5278ba', 'c1db01f6c16fc56a4718d75a73c0fb187a44fef0aeb962e6cf9a6b4d438dbac2', 'b64e36169ed6401277ab9f8ca387e71ae6ea4c2248ef446de64061eadf5278ba']; 一致性：systemChars 在未触发守卫的 3 次之间不一致 [4914, 5171, 4914]; S5-C-2：S5 回答不含原句内容或等价语义
- S5-B：FAIL；citation：存在空 citation 集合; citation：覆盖率不足：average=0.556（阈值 0.667），per-rep=[0.667, 0.0, 1.0]（每轮阈值 0.333）; citation：且没有 2/3 次 citation 集合完全相同
- S5-A 通过 citation 与反向历史判据；S5-B 的唯一失败是 S5-B-2 没有输出任何 citation（与守卫无关）；S5-C 同时存在 S5-C-2 历史原句漏引与 Hash 不一致，后者由该次记忆检索 `DEGRADED` 引入。
- 因此本批结论是 **FAIL**，不是「判据设计导致的全量失败」：S5-B-2 与 S5-C-2 都是可复现到具体 run 的回答/检索问题；HISTORY_GUARD 豁免规则本身在 S5-B-3 上按预期生效，但该次触发疑似是否定语境下的误判（见 11.5）。

### 11.9 工作区状态

| 项目 | 值 |
|---|---|
| branch | main |
| HEAD | 6609097e26b905369a7e9641e86d5a86732f0e5a |
| HEAD subject | test: 修正 S5 历史使用与 citation 一致性判据，重跑受影响场景 |
| 工作区状态 | M docs/testing/phase5-abc-rerun.md  M scripts/phase5/full/full_common.py  M scripts/phase5/full/run_agent_full.py  M scripts/phase5/full/run_full_abc.py  M scripts/phase5/full/scenarios.py ?? scripts/phase5/full/run_s5_rerun.py |
| 是否 commit | 否（本批次未执行 git commit） |
| 是否 push | 否（本批次未执行 git push） |

### 11.10 需要人工审核的决策点

1. **S5-A 反向判据**：本批按人工选择，S5-A 不要求出现原句，而是要求「不出现」；需确认这是最终判据口径。
2. **Understanding 重设计边界**：只改了 Understanding；revision / memory 文本未改，其中仍可能出现「每次只」等词，理论上可能被 positive 关键词规则命中，需确认是否需要下一批同步收紧。
3. **HISTORY_GUARD 豁免**：只豁免守卫触发的 repeat 的 contextHash / systemSha256 / systemChars / promptTokens；manifest 始终要求一致。需确认该豁免范围是否可作为最终判据。
4. **守卫触发检测方式**：本 harness 用「同一轮对话出现多次 LLM 调用」判定守卫触发；产品 trace 未持久化到数据库，如后续增加其他多调用路径，检测方式需要同步调整。
5. **全量观测缺口**：90 次全量的 HISTORY_GUARD 触发次数未知：原批次结果 JSON 未记录 `historyGuardTriggered`，原隔离库已按 cleanup 清理，无法回溯。本报告不做回溯推测，建议下一批全量实验补记该字段。
6. **首答全文留存**：首答与重生成全文写入隔离 temp 的 `s5-rerun-results.json`，不写回业务库；需确认留存边界。
7. **S5-B-2 空 citation**：该次回答没有输出任何知识标签，按调整后判据判 FAIL；是否需要在下一批单独重跑 B-2，还是把「无 citation」视为产品行为问题，需人工决定。本批预算已用满，未补跑。
8. **S5-C-2 检索降级**：`memory:5` 返回 `DEGRADED`，并附带 `memory-status:DEGRADED` 来源，导致 Hash 与另外两次不同。是否把检索降级单独归类为环境失败并重跑，还是维持「未触发守卫即 FAIL」的现行口径，需人工决定。
9. **HISTORY_GUARD 否定语境误判（新发现）**：S5-B-3 首答写的是「不是完全答错，也不是完全答对」，`partial_upgrade` 仍按字面命中「完全答对」「答错」并触发重生成。需产品侧确认校验是否应识别否定语境；这属于 Step 3 观测到的产品行为问题，不在本批修改边界内。

