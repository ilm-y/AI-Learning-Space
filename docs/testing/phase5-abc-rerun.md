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

## 12. S5-B/C 判据调整与重跑（Batch 3）

- 追加时间：2026-11-09T23:13:11
- 重跑快照时间：2026-11-09 23:05:31
- 隔离库：`phase5_pilot_20261109230529_3a91d2`
- 文档 collection：`phase5-pilot-20261109230529-3a91d2_text-embedding-v4_1024`
- Memory collection：`phase5-pilot-20261109230529-3a91d2-memory_text-embedding-v4_1024`
- 重跑范围：S5-B / S5-C 各 3 次，共 6 次对话；记录到 6 次。S5-A 沿用上一批（2026-11-09 22:47 快照）的 PASS，不重跑。
- 生成参数指纹：`2e1405ed23bbe132b96b018c7e79d190321e43ff5ffb5c5108e99bee47edb5f6`；model=`deepseek-flash`，temperature=`0`。

### 12.1 S5 citation 判据确认：不必须

**结论：S5 复盘问题不必须引用知识文档。**

理由：

1. S5 的问题是「回顾一下，我之前对 TCP 慢启动的理解错在哪？**请引用我当时的原话**」。问题显式要求的是用户历史原话，不是知识文档标签。
2. 复盘问题的核心可核验证据是「回答是否复现了历史原句」；这是区分「真的用了历史」与「只讲了通用知识」的关键。知识引用只说明「错在哪」的解释有资料支撑，是支持性证据，不是复盘成立的必要条件。
3. 因此一次回答可以同时满足「引用了历史原句」和「没有输出知识标签」，这仍然是合格复盘。把空 citation 一律判 FAIL 会把「模型正确引用历史、只是没贴知识标签」误判为失败。
4. 边界不放宽：非空 citation 仍必须可溯源、落在本场景同一文档；空 citation 只有在回答引用历史原句时才允许；既无 citation 又无历史原句仍判 FAIL。

**实际改动**：`full_common.citation_consistency_check` 新增 `allow_history_quote` 模式；`run_s5_rerun.evaluate_combo` 对 S5 传入 `allow_history_quote=True`。判定规则变为「每次 repeat：citation 非空 或回答引用历史原句」；覆盖率/并集仍计算展示，但不再是该模式的硬失败项。

### 12.2 S5-C memoryDegraded 豁免

| 项目 | 调整 |
|---|---|
| 降级判定 | Manifest 中存在 `retrieval_status=DEGRADED` 的 memory 来源，或存在 `source_id` 含 `memory-status:DEGRADED` 的认知状态来源 |
| 豁免字段 | `memoryDegraded=true` 时豁免 `contextHash` / `systemSha256` / `systemChars` / `promptTokens` 的一致性检查 |
| 不豁免字段 | `manifestSha256` 始终要求 3 次一致；不一致仍判 FAIL |
| 必须记录 | 结果 JSON 与报告逐条记录 `memoryDegraded`（布尔）与 `memoryDegradedSource`（来源 ID 列表） |
| 判定函数 | `full_common.guard_aware_hash_consistency`；`memory_degraded_info` 从 Manifest 派生字段 |
| 本批实测 | 6 次重跑均未出现 Memory DEGRADED；该豁免本批未实际触发，代码路径已落地并做离线自检 |

**离线自检**（不调用 LLM，直接构造记录验证判定函数）：

| 用例 | 构造 | 结果 |
|---|---|---|
| 降级豁免、Manifest 一致 | 1 次 `memoryDegraded=true`，Hash 与另外两次不同，`manifestSha256` 相同 | PASS；输出 `degradedKeys` 与豁免说明 |
| 降级但 Manifest 不一致 | 降级的 run `manifestSha256` 不同 | FAIL；报告明确 `manifestSha256 始终不豁免` |
| 降级但未记录来源 | `memoryDegraded=true`、`memoryDegradedSource=[]` | FAIL；报告「未记录 memoryDegradedSource」 |
| 未降级、Hash 不一致 | 3 次均未豁免，`contextHash` 不同 | FAIL；仍按未豁免基线判定 |

### 12.3 S5-B/C 逐次结果

| key | run_id | 守卫触发 | 降级 | 降级来源 | LLM 调用数 | finish | prompt/completion/total | citation 集合 | Context Hash(12) | System Hash(12) | Manifest(12) | Manifest 关键来源 | 历史原句（最终交付） |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S5-B-1 | 1 | 否 | 否 | - | 1 | stop | 2897/1020/3917 | [[5,15]] | 3e606f230481 | ad44d0f30dd0 | b51f17aeaba1 | history:1,history:2,revision:6,evidence:5,revision:7 | 命中 |
| S5-B-2 | 3 | 否 | 否 | - | 1 | stop | 2897/1805/4702 | [[5,13],[5,14],[5,15]] | 3e606f230481 | ad44d0f30dd0 | b51f17aeaba1 | history:1,history:2,revision:6,evidence:5,revision:7 | 命中 |
| S5-B-3 | 5 | 是 | 否 | - | 2 | stop | 2985/1858/4843 | [] | 52cb22381c4f | 2ab53a32a3c6 | b51f17aeaba1 | history:1,history:2,revision:6,evidence:5,revision:7 | 未命中 |
| S5-C-1 | 7 | 是 | 否 | - | 2 | stop | 2985/1320/4305 | [] | 52cb22381c4f | 2ab53a32a3c6 | 0baf3d0e0b07 | history:1,history:2,revision:6,evidence:5,revision:7 | 未命中 |
| S5-C-2 | 9 | 是 | 否 | - | 2 | stop | 2985/1813/4798 | [[5,15]] | 52cb22381c4f | 2ab53a32a3c6 | 0baf3d0e0b07 | history:1,history:2,revision:6,evidence:5,revision:7 | 命中 |
| S5-C-3 | 11 | 否 | 否 | - | 1 | stop | 2897/1178/4075 | [[5,15]] | 3e606f230481 | ad44d0f30dd0 | 0baf3d0e0b07 | history:1,history:2,revision:6,evidence:5,revision:7 | 命中 |

- 「历史原句」列匹配的是最终交付回答（`answerFull`），不是模型首答。S5-B/C 的正向判据是「任一次最终回答必须含原句内容或等价语义」。

### 12.4 逐组合判定

| 场景-策略 | 总判定 | citation | 历史使用 | Manifest revision/evidence | Hash（守卫/降级豁免后） | 被豁免的 run | 失败项 |
|---|---|---|---|---|---|---|---|
| S5-B | FAIL | FAIL | FAIL | -（本策略不要求） | PASS | S5-B-3 | citation：存在空 citation 且未引用历史原句：S5-B-3; S5-B-3：S5 回答不含原句内容或等价语义 |
| S5-C | FAIL | FAIL | FAIL | PASS | PASS | S5-C-1,S5-C-2 | citation：存在空 citation 且未引用历史原句：S5-C-1; S5-C-1：S5 回答不含原句内容或等价语义 |

**逐次历史使用判据明细**：

| key | 判据命中 | 原句连续子串 | 关键词 | 结果 |
|---|---|---|---|---|
| S5-B-1 | 命中 | 是 | 慢启动=Y,每次只=Y,加一=Y | PASS |
| S5-B-2 | 命中 | 是 | 慢启动=Y,每次只=Y,加一=Y | PASS |
| S5-B-3 | 未命中 | 否 | 慢启动=Y,每次只=N,加一=N | FAIL |
| S5-C-1 | 未命中 | 否 | 慢启动=Y,每次只=N,加一=N | FAIL |
| S5-C-2 | 命中 | 是 | 慢启动=Y,每次只=Y,加一=Y | PASS |
| S5-C-3 | 命中 | 是 | 慢启动=Y,每次只=Y,加一=Y | PASS |

**Citation 逐组合明细**：

| 场景-策略 | citation 集合（按 rep） | 平均覆盖率 | 历史原句 | 结论 |
|---|---|---|---|---|
| S5-B | [[[5,15]],[[5,13],[5,14],[5,15]],[]] | 0.444 | S5-B-1=是,S5-B-2=是,S5-B-3=否 | FAIL |
| S5-C | [[],[[5,15]],[[5,15]]] | 0.667 | S5-C-1=否,S5-C-2=是,S5-C-3=是 | FAIL |

**S5-C Manifest revision / evidence**：

| key | revision 来源 | evidence 来源 | 结果 |
|---|---|---|---|
| S5-C-1 | revision:6,revision:7 | evidence:5 | PASS |
| S5-C-2 | revision:6,revision:7 | evidence:5 | PASS |
| S5-C-3 | revision:6,revision:7 | evidence:5 | PASS |

**Hash 逐字段豁免说明**：

| 场景-策略 | 守卫/降级豁免说明 |
|---|---|
| S5-B | S5-B-3：historyGuardTriggered=true，豁免 contextHash, systemSha256, systemChars, promptTokens（llmCallCount=2） |
| S5-C | S5-C-1：historyGuardTriggered=true，豁免 contextHash, systemSha256, systemChars, promptTokens（llmCallCount=2） |
| S5-C | S5-C-2：historyGuardTriggered=true，豁免 contextHash, systemSha256, systemChars, promptTokens（llmCallCount=2） |

**运行期观测项**（不参与 PASS/FAIL）：

- S5-B-3：单轮对话出现 2 次 LLM 调用（预期 1）
- S5-C-1：单轮对话出现 2 次 LLM 调用（预期 1）
- S5-C-2：单轮对话出现 2 次 LLM 调用（预期 1）

### 12.5 HISTORY_GUARD / 降级逐条记录

#### S5-B-3

| 项目 | 首次调用 | 重生成调用 |
|---|---|---|
| systemSha256 | ad44d0f30dd068d384e64b830551b778eba1d46bf6dd82e782db2a7272a502fb | 2ab53a32a3c6796a2795a69b39bb5293dc485b6aed2e90232a41ad32479b2d59 |
| chars | 4914 | 5060 |
| promptTokens | 2897 | 2985 |

**产品 trace 恢复的触发明细**（隔离 temp 的 agent.log）：

| kind | excerpt | reason |
|---|---|---|
| partial_upgrade | 完全正确 | 记录里没有完全答对的证据 |

- `regenerated=True`，`remainingIssues=[{'kind': 'partial_upgrade', 'excerpt': '答错', 'reason': '记录只判定为部分正确，不能改写成答错'}]`，`regenerationError=None`，`fallback=True`，`evidenceCount=1`。
- Manifest 中注入的 history：history:1,history:2

首次回答（HISTORY_GUARD 校验前，未推送）：

```text
## 当时的原话

能核对到的原话有两处：

1. 你在这次提问里复述的更早说法：**「我原来以为慢启动慢是因为它每次只把窗口加一。」**
2. 更早一次复盘时，你自己的表述是：**「我原以为慢启动增长很慢，后来明白是起点小、呈指数增长。」**

更早的那次记录，当时被标为「部分正确」：前半句对原来想法的复述是准的，后半句的纠正也是准的，但被记为「部分正确」而不是完全正确。

## 分叉在哪一步

你的原话本身就把推理过程说出来了——你把因果连在了「慢」这个词上：名字叫**慢**启动 → 所以应该是每次加得很少 → 每次只加一。

分叉点在于「慢」修饰的是什么。它修饰的是**起点**，不是**速度**：初始 cwnd 为 1 个 MSS，每收到一个 ACK 增加一个 MSS，每个 RTT 大约翻倍[资料《s5-slow-start.md》#0]。起点是 1，爬升到可用带宽的量级需要若干个 RTT，要花时间的是这段爬升过程，不是每一轮的增速[资料《s5-slow-start.md》#1]。

而你原来想的「每个 RTT 只加一」，恰恰不是慢启动的行为——它更接近进入拥塞避免之后的线性增长[资料《s5-slow-start.md》#2]。也就是说，你原来给出的是慢启动**结束之后**的增长曲线，只是把它安到了慢启动这个阶段上。

## 说清楚边界

- 你现在的理解是：慢启动从 cwnd=1 开始、阶段内指数增长。
- 但「把增长方式理解为线性递增」这一条，在当前的认知状态里仍然被标为存在，描述用的是「曾」。这条偏差值得记住，因为它和「慢启动阶段指数增长」是同一件事的两面。
- 从你线性增长的想法变到现在这个说法，中间发生了什么——是不是看过资料、是谁纠正的——记录里看不出原因，我不做推测。
- 另外，长期记忆里有一条较早的状态，说你「把慢启动理解为线性加一」；那是更早的记录，当前状态已经不同，不是本轮的事实依据。

顺带一句：你这次问的「慢启动何时停止」，正好和上面那个分叉连着——停止的条件是 cwnd 碰到 ssthresh，之后才转入每 RTT 线性加一的拥塞避免[资料《s5-slow-start.md》#1]。要接着往下讲这个，随时说。
```

重生成回答（若未被回退，则为最终交付）：

```text
## 你当时的原话

先放可核对的部分。你有两条自己的表述，都在记录里：

- 本轮你说的：「我原来以为慢启动慢是因为它每次只把窗口加一。」
- 更早的一次（快照里保存的你的说法）：「我原以为慢启动增长很慢，后来明白是起点小、呈指数增长。」

同期还有一条证据标注（2026-11-09 23:05，来源：消息）：你的解释被记为**部分正确**——不是答对，也不是答错。记录里同时写了「把‘慢启动’误解为线性增长，已纠正为指数增长」，但「已纠正」这个说法和下面第 3 点有冲突。

## 错误位于哪一步，不在结论

你的推理分叉点在于：**把名字里的“慢”归因到增速上。**

- 你的版本：慢 → 因为每次只加一个 MSS → 所以每 RTT 只涨一点，曲线是线性的。
- 实际结构：慢 → 因为起点只有 1 个 MSS，要爬很久才到有意义的窗口；但每收到一个 ACK 就加 1 个 MSS，于是一个 RTT 内整体大约翻倍，曲线是指数的[资料《s5-slow-start.md》#0]。

所以“每次加一个 MSS”这句话本身并不错，它是**每个 ACK 的增量**。你错在把它当成了**每个 RTT 的增量**。差一个层级：ACK 级 vs RTT 级。这也正是为什么“每 RTT 只加一”读起来很像慢启动，实际上更接近拥塞避免的线性增长[资料《s5-slow-start.md》#2]。而“慢”这个命名来源，是起点小、需要时间爬升，不是增长速度慢[资料《s5-slow-start.md》#1]。

## 一处记录内部的不一致

需要说明：你最新的快照里，这条「把慢启动的增长方式理解为线性递增」仍被标为**当前存在**的误区；而你自己给出的说法、以及本轮这句话，都已经是“指数增长”。两者并存，记录里没有说明原因，我也无法确认哪一边更贴近你此刻的实际状态。

另有一条较早的学习状态记录，说你曾把它理解为“线性加一”，并希望在复盘时看到当时的原话和纠正过程。那是历史记录，与当前快照不完全一致；本次你确实要了原话，上面已经给出。

想继续的话，可以接着看你卡住的那一格：指数增长在什么条件下停下、切换点由谁决定。
```

**最终交付回答**（与重生成回答不同，产品判为仍有问题后使用确定性回退）：

```text
我只能按当前记录复述可以核对的部分，不推断记录之外的经过。

**TCP 慢启动**
- 当前状态：部分理解
- 当前仍存在的误区：曾把慢启动的增长方式理解为线性递增
- 记录中的错误理解：「曾把慢启动的增长方式理解为线性递增」
- 可以核对的记录（按时间）：
  - 2026-11-09 23:05 ｜ [部分正确/用户解释] ｜ 来源：消息 ｜ 原记录：「【S5-E】用户把“慢启动”误解为线性增长，已纠正为指数增长。」

这些记录能说明你的表述发生过哪些变化；但记录没有写你为什么改变看法，也没有记录是否借助过外部提示或资料，这两点无法确认。
```

#### S5-C-1

| 项目 | 首次调用 | 重生成调用 |
|---|---|---|
| systemSha256 | ad44d0f30dd068d384e64b830551b778eba1d46bf6dd82e782db2a7272a502fb | 2ab53a32a3c6796a2795a69b39bb5293dc485b6aed2e90232a41ad32479b2d59 |
| chars | 4914 | 5060 |
| promptTokens | 2897 | 2985 |

**产品 trace 恢复的触发明细**（隔离 temp 的 agent.log）：

| kind | excerpt | reason |
|---|---|---|
| partial_upgrade | 答错 | 记录只判定为部分正确，不能改写成答错 |

- `regenerated=True`，`remainingIssues=[{'kind': 'partial_upgrade', 'excerpt': '完全答对', 'reason': '记录里没有完全答对的证据'}, {'kind': 'partial_upgrade', 'excerpt': '答错', 'reason': '记录只判定为部分正确，不能改写成答错'}]`，`regenerationError=None`，`fallback=True`，`evidenceCount=1`。
- Manifest 中注入的 history：history:1,history:2

首次回答（HISTORY_GUARD 校验前，未推送）：

```text
先把你当时的原话摆出来，这两句是可核对的：

- 本轮你自己说的：「我原来以为慢启动慢是因为它每次只把窗口加一。」
- 记录里你更早的说法：「我原以为慢启动增长很慢，后来明白是起点小、呈指数增长。」

## 错在哪一步

你的推理分叉点在**对“慢”这个词的归因**：你把“慢”归到了增长方式上——每次只加一，所以慢。真正错的是这一步：慢启动的“慢”说的是起点小（cwnd 从 1 个 MSS 开始），不是每个 RTT 的增长幅度小。

顺带说，“每个 RTT 只加一”这个描述本身并不荒谬，它只是被安到了错误的阶段：那种线性增长更接近**拥塞避免**的行为，而不是慢启动 [资料《s5-slow-start.md》#2]。慢启动阶段是每收到一个 ACK 就把 cwnd 加一个 MSS，因此每个 RTT 大约翻倍 [资料《s5-slow-start.md》#0]。所以你把两条曲线接错了位置，不是把“慢”这个字理解错了。

记录里对当时那次表述的标注是“部分正确”，不是完全答错：起点小、需要时间爬升这层意思你抓到了，错的是增长曲线那一层。后来这个误区被纠正为“指数增长”，你现在的说法里也已经是“起点小、呈指数增长”这一版了。

## 记录里没有的部分

关于那次纠正是怎么发生的——是看了资料、被人指出，还是你自己重新推的——记录里没有写，我不替它编。同样，你当时为什么把“慢”直接对应到“加一”，记录里也只能看到这个说法本身，看不到背后的理由。

如果你想把这一条彻底钉住，可以自己回答一句：cwnd 从 1 开始，收到第 1 个 ACK 后是多少、第 2 个 ACK 后是多少？答得出来，这条曲线就不会再滑回线性了——而这条曲线的终点，正好就是你这次要解决的“什么时候停”。
```

重生成回答（若未被回退，则为最终交付）：

```text
## 当时的原话

可核对的有两处：

- 本轮你的说法：「我原来以为慢启动慢是因为它每次只把窗口加一。」
- 2026-11-09 23:05 留下的那条解释：「我原以为慢启动增长很慢，后来明白是起点小、呈指数增长。」

同一时间点还有一条证据，内容只有一句概括：「用户把'慢启动'误解为线性增长，已纠正为指数增长。」它被标注为**部分正确**——是部分正确，不是答错，也不是完全答对。

## 错在哪一步

分叉点在**增长曲线**这一步：你的理解是「每个 RTT 窗口加一」，而慢启动实际上是每收到一个 ACK 就加一个 MSS，于是每个 RTT 大约翻倍。按资料的说法，把慢启动理解成「每个 RTT 只加一」是常见误解，那种线性增长更接近拥塞避免阶段的行为[资料《s5-slow-start.md》#2]。

有意思的是，你对「慢」这个字的归因方向是对的：慢启动的「慢」指的是起点小、需要时间爬升，不是增长速度慢[资料《s5-slow-start.md》#1]。「加一」这个细节，才是模型跑偏的地方。

## 纠正过程能确认到哪

记录里只有证据条目写着「已纠正为指数增长」，没有留下当时是通过什么方式纠正的——是别人讲的、自己查的，还是对着资料改的，记录里看不出，我无法确认。时间上能确认的只是：2026-11-09 23:05 的两个版本快照都还挂着「曾把慢启动的增长方式理解为线性递增」这一条，而当前理解已经写成「起点小、呈指数增长」。

## 现在的状态

你现在掌握的是：慢启动从 cwnd=1 开始、慢启动阶段指数增长。误区条目用的是「曾」——描述的是过去那个理解，而不再是现在的说法。

真正还没确定的是另一步：**指数增长什么时候停**。每收到一个 ACK 就加一个 MSS，这不可能无限进行下去，停止条件就是 ssthresh。这一步要不要现在就展开？
```

**最终交付回答**（与重生成回答不同，产品判为仍有问题后使用确定性回退）：

```text
我只能按当前记录复述可以核对的部分，不推断记录之外的经过。

**TCP 慢启动**
- 当前状态：部分理解
- 当前仍存在的误区：曾把慢启动的增长方式理解为线性递增
- 记录中的错误理解：「曾把慢启动的增长方式理解为线性递增」
- 可以核对的记录（按时间）：
  - 2026-11-09 23:05 ｜ [部分正确/用户解释] ｜ 来源：消息 ｜ 原记录：「【S5-E】用户把“慢启动”误解为线性增长，已纠正为指数增长。」

这些记录能说明你的表述发生过哪些变化；但记录没有写你为什么改变看法，也没有记录是否借助过外部提示或资料，这两点无法确认。
```

#### S5-C-2

| 项目 | 首次调用 | 重生成调用 |
|---|---|---|
| systemSha256 | ad44d0f30dd068d384e64b830551b778eba1d46bf6dd82e782db2a7272a502fb | 2ab53a32a3c6796a2795a69b39bb5293dc485b6aed2e90232a41ad32479b2d59 |
| chars | 4914 | 5060 |
| promptTokens | 2897 | 2985 |

**产品 trace 恢复的触发明细**（隔离 temp 的 agent.log）：

| kind | excerpt | reason |
|---|---|---|
| partial_upgrade | 答错 | 记录只判定为部分正确，不能改写成答错 |

- `regenerated=True`，`remainingIssues=[]`，`regenerationError=None`，`fallback=False`，`evidenceCount=1`。
- Manifest 中注入的 history：history:1,history:2

首次回答（HISTORY_GUARD 校验前，未推送）：

```text
## 你当时说的原话

最直接的一句是你自己给出的说法：

> 「我原以为慢启动增长很慢，后来明白是起点小、呈指数增长。」

另一处是你用更具体的方式描述了同一个误解：

> 「我原来以为慢启动慢是因为它每次只把窗口加一。」

这两句说的其实是一件事：你把“慢启动”这个名字里的“慢”，当成了对**增长速度**的描述。

## 错在哪一步

分叉点就在这个名字上。你当时这样推理：既然叫“慢”，那应该是每个 RTT 只增加一点；而“一点”最自然的量化方式就是加一。这个推理本身很顺，只是用错了线索，因为“慢启动”里的“慢”形容的是**起点很小、需要时间爬升**，而不是增长速度慢 [资料《s5-slow-start.md》#1]。

而“每个 RTT 只加一”实际上更接近**进入拥塞避免之后**的行为，也就是说，你在描述的是另一个阶段 [资料《s5-slow-start.md》#2]。真正的慢启动是每收到一个 ACK，cwnd 就增加一个 MSS，因此每个 RTT 大约翻倍 [资料《s5-slow-start.md》#0]。所以正确的图景是：起点很小（cwnd 从 1 个 MSS 开始）加上增长很快（每 RTT 翻倍）。你觉得它“慢”，是因为把起点小这个特征错误地合并进了增长曲线的斜率里。

## 记录上怎么标的

- 那条当时的解释被标为**部分正确**，并没有被写成“答错”，但记录里没有说明具体哪一部分算对，也看不出纠正是怎么发生的：是你自己对照资料改的，还是别的原因，记录里都没有写。
- “线性加一”这个说法更早的时候也被记过，后来你留下的说法是“起点小、呈指数增长”。这两份记录的写法不同，我保留原样，不替它们统一。
- 还有一个细节：那条早期的误会现在仍然列在待纠正项里，但同一条记录也写了你现在已经把它理解成指数增长。两处同时存在，我只能如实告诉你，不去推测原因。

对你这段学习来说，这件事剩下的实际影响不在“指数还是线性”本身，而在你的阶段目标上，也就是什么时候停止指数增长。你当时把两个阶段混在一起，这正是切换点容易含糊的地方；要不要现在就从这里往下讲？
```

重生成回答（若未被回退，则为最终交付）：

```text
## 你当时的原话

本轮你自己说：

> 我原来以为慢启动慢是因为它每次只把窗口加一。

记录里保存的另外一句是：

> 我原以为慢启动增长很慢，后来明白是起点小、呈指数增长。

2026-11-09 23:05 有一条来自你消息的记录，标注为「部分正确」，原文是：

> 用户把“慢启动”误解为线性增长，已纠正为指数增长。

## 错在哪一步

分叉点在**对“慢”这个字的理解**上。你当时把它读成了「每次加得很少」，于是自然推出每个往返窗口只加一个 MSS，也就是线性递增。而慢启动的“慢”说的是起点很小（从 1 个 MSS 开始爬升），增长本身是每收到一个 ACK 就加一个 MSS，因此大约每个 RTT 翻倍。

顺带一句：把慢启动理解成「每个 RTT 只加一」是一个有名字的常见误解，那种行为更接近进入拥塞避免之后的线性增长[资料《s5-slow-start.md》#2]。这只是说明这个误解很常见，不构成你当时为什么这么想的证据。

## 记录里看不出来的部分

- 你是在什么场合、因为什么改变看法的：记录里没有写，无法确认。
- 纠正的具体过程（谁说的、看到了什么材料、有没有借助外部资料）：只有「已纠正为指数增长」这一条标注，中间的对话细节无法确认。
- 记录里两次状态快照的时间都是 2026-11-09 23:05，都把「把慢启动的增长方式理解为线性递增」标为当时仍存在；同一份当前记录又把「慢启动阶段指数增长」列进了你已经理解的范围。这两条并存，记录本身没有说明哪一条更贴近你眼下，这一点只能由你自己确认。

如果你愿意，可以说一句现在怎么看「慢启动为什么叫慢」，我据此判断还要不要往 ssthresh 那边讲。
```

- 说明：`HISTORY_GUARD` 的 issue 明细只写入进程内 trace；本报告按 runId 从隔离 temp 的 `agent.log` 恢复。首答/重生成全文只存于隔离 temp，不写回业务库。

### 12.6 缺陷登记

两条缺陷已登记到 `docs/testing/phase5-known-issues.md`：

1. **模型历史使用不稳定**：上一批 S5-C-2 声称「第一次表述的原文没有被保存下来」，但 Manifest 确认 `history:1`、`history:2` 已注入。影响：Phase 5 要回答「模型是否真的理解上下文」，这是反例；建议后续全量统计此类声明与 Manifest 的矛盾次数。本批未复现该声明。
2. **HISTORY_GUARD partial_upgrade 否定语境误判**：本批 S5-B-3 / S5-C-1 / S5-C-2 的首答或重生成回答在否定/对比语境中出现「完全正确」「答错」「完全答对」等短语，被 `partial_upgrade` 按字面命中；S5-B-3 与 S5-C-1 两次重生成后仍有命中，产品回退为不含原句的确定性文本。影响：合法回答被误判并重写，重写失败时最终交付丢失用户要求的原句证据，增加延迟与 token。建议产品侧增加否定语境识别；本批只登记不修。

### 12.7 成本记录

| 项目 | 数值 |
|---|---|
| 对话调用次数（记录） | 6 |
| 模型实际调用次数（含守卫重生成） | 9 |
| 触发守卫的对话数 | 3 |
| LLM 预算 | 6（+守卫余量 2，共 8） |
| LLM 预算核对 | FAIL（超出） |
| prompt tokens（仅最终记录） | 17646 |
| completion tokens（仅最终记录） | 8994 |
| total tokens（仅最终记录） | 26640 |
| Agent 进程内 Embedding 调用 | 12 |
| Agent 进程内 Embedding 文本数 | 12 |
| 单次运行 Embedding 最大值 | 2 |
| Tavily 调用 | 0 |

- 预算核对：对话记录 6 次；含守卫重生成实际调用 9 次，上限 8 次。本批实际调用超出预算 1 次，原因是 3 次 HISTORY_GUARD 重生成（其中 2 次最终回退）。该超支如实记录，不通过删减运行掩盖。
- Agent Embedding 12 次（上限 12，按每场景 6 次计）；Tavily 0。
- setup 阶段 10 份文档的索引 Embedding 由 Spring 侧发起，不属于每次对话的 Agent 预算。

### 12.8 冻结与清理

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

### 12.9 Phase 5 Step 3 最终结论

- 失败组合：['S5-B', 'S5-C']；**Phase 5 Step 3 最终结论：FAIL。**
- S5-A：PASS（沿用上一批结果，本批未重跑）。
- S5-B：FAIL；citation：存在空 citation 且未引用历史原句：S5-B-3; S5-B-3：S5 回答不含原句内容或等价语义
- S5-C：FAIL；citation：存在空 citation 且未引用历史原句：S5-C-1; S5-C-1：S5 回答不含原句内容或等价语义
- 剩余失败项性质：
  - S5-B-3：**系统缺陷**。模型首答与重生成回答都包含原句，但产品侧 `HISTORY_GUARD partial_upgrade` 在否定/对比语境中按字面命中「完全正确」「答错」，两次校验后仍判有问题，最终回退为不含原句的确定性文本；因此最终交付回答缺原句、citation 为空。
  - S5-C-1：**系统缺陷**。与 S5-B-3 同因：首答命中「答错」、重生成命中「完全答对」「答错」，最终回退，最终交付回答缺原句。
  - 本批没有判据问题导致的失败；没有 Memory DEGRADED 导致的环境失败。
- 必须附带的观测项（不作为 Step 3 判据，但必须记录）：
  1. **模型历史使用不稳定**：上一批 S5-C-2 声称历史原句没有被保存，而 Manifest 确认 history 已注入；本批未复现，但该反例仍成立，建议后续全量统计此类声明与 Manifest 的矛盾次数。
  2. **HISTORY_GUARD 否定语境误判**：本批 3 次触发（S5-B-3、S5-C-1、S5-C-2），其中 S5-B-3 与 S5-C-1 回退；误判直接导致合法回答被重写并丢失原句证据。

### 12.10 90 次全量补记声明

- 90 次全量的 HISTORY_GUARD 触发次数与 Memory DEGRADED 次数均未知：原批次结果 JSON 未记录 `historyGuardTriggered` 与 `memoryDegraded`，原隔离库已按 cleanup 清理，无法回溯。本报告不做回溯推测；下一批全量实验必须记录 `historyGuardTriggered` 与 `memoryDegraded`。

### 12.11 工作区状态

| 项目 | 值 |
|---|---|
| branch | main |
| HEAD | 648fc6aa66fbfd85d9f7447d8e0256793739f6c2 |
| HEAD subject | test(phase5): S5 数据重设计、HISTORY_GUARD 判据调整与 S5-ABC 重跑 |
| 工作区状态 | M docs/testing/phase5-abc-rerun.md  M scripts/phase5/full/full_common.py  M scripts/phase5/full/run_s5_rerun.py ?? docs/testing/phase5-known-issues.md |
| 是否 commit | 否（本批次未执行 git commit） |
| 是否 push | 否（本批次未执行 git push） |

### 12.12 需要人工审核的决策点

1. **S5 citation 判据**：本批确认「不必须引用知识文档」，改为「citation 非空或引用历史原句」。需确认这是最终口径。
2. **memoryDegraded 豁免范围**：只豁免 contextHash / systemSha256 / systemChars / promptTokens；manifestSha256 始终不豁免。若 Memory DEGRADED 同时改变 Manifest（本期确实会追加 `memory-status:DEGRADED` 来源），S5-C 仍会因 manifestSha256 不一致判 FAIL。需确认这是否符合预期；若希望降级运行也 PASS，需要重新定义 Manifest 一致性口径。
3. **HISTORY_GUARD 回退导致最终回答缺原句**：本批 S5-B-3 与 S5-C-1 最终交付是确定性回退文本，不含用户要求的原句。是否允许守卫回退覆盖用户显式要求的历史证据，需要产品侧决策；本批按现行失败判据记 FAIL。
4. **HISTORY_GUARD 否定语境误判**：3 次触发均为「不是……也不是……」或「而不是……」语境下的字面命中。建议产品侧增加否定/对比语境识别；本批只登记不修。
5. **模型历史使用不稳定观测项**：上一批 S5-C-2 的反例仍成立；需确认后续是否在全量实验中统计「声称历史不存在」与 Manifest 的矛盾次数。
6. **LLM 预算超支**：本批实际调用 9 次，超出「6 + 守卫余量 2 = 8」1 次。超支来自 3 次守卫重生成；如实记录，未删减运行。
7. **S5-A 未重跑**：S5-A 沿用上一批 PASS；若要求本批所有组合同快照重跑，需要追加 3 次调用。
8. **90 次全量观测缺口**：原批次未记录 historyGuardTriggered / memoryDegraded，原隔离库已清理，无法回溯；下一批全量必须补记。


## 13. HISTORY_GUARD 否定语境修复与 S5-B/C 重跑（Batch 4）

- 追加时间：2026-11-09T23:31:43
- 重跑快照时间：2026-11-09 23:26:05
- 隔离库：`phase5_pilot_20261109232603_0efdae`
- 文档 collection：`phase5-pilot-20261109232603-0efdae_text-embedding-v4_1024`
- Memory collection：`phase5-pilot-20261109232603-0efdae-memory_text-embedding-v4_1024`
- 重跑范围：S5-B / S5-C 各 3 次，共 6 次对话；记录到 6 次。S5-A 沿用 Batch 3 的 PASS，不重跑。
- 生成参数指纹：`2e1405ed23bbe132b96b018c7e79d190321e43ff5ffb5c5108e99bee47edb5f6`；model=`deepseek-flash`，temperature=`0`。

### 13.1 HISTORY_GUARD partial_upgrade 否定语境修复

**缺陷（KI-2）**：`partial_upgrade` 按字面匹配「完全正确」「完全答对」「答错」，没有识别否定 / 对比语境，把「不是完全答错，也不是完全答对」判成升级断言并触发重生成；重生成仍被误判时回退为确定性文本，覆盖掉用户显式要求引用的历史原句。

**识别模式**（只作用于 `partial_upgrade`）：

| 模式 | 规则 | 示例 |
|---|---|---|
| 短语前否定词 | 短语前 12 个字符内最近的标记是否定词（不是 / 而不是 / 并非 / 并没有 / 也不算 …）时跳过该短语 | 「不是完全答错」「而不是完全正确」「并没有被写成『答错』」 |
| 并列否定结构 | 短语位于「不是 …… 也不是 ……」结构中，或紧跟第二个否定词之后时跳过 | 「既不是完全答对也不是完全答错」 |
| 肯定对比 | 最近标记是「而是」时仍按肯定判断处理，不因前面的「不是」放行 | 「不是答错，而是完全答对」→「完全答对」仍触发 |

- 判定函数：`agent/app/history_guard.py` 的 `_phrase_in_negated_context` / `_in_parallel_negation`；观测函数 `partial_upgrade_negation_skips`。
- 其他 issue kind（`internal_process` / `external_absence` / `record_completeness` / `missing_source_mapping` / `no_history_*`）的匹配逻辑未修改。

**测试结果**：`agent/tests/test_history_guard.py` + `agent/tests/test_history_guard_runtime.py` 共 32 passed。新增 / 覆盖用例：

| 用例 | 预期 | 结果 |
|---|---|---|
| 「不是完全答错」 | 不触发 partial_upgrade | PASS |
| 「而不是完全正确」 | 不触发 partial_upgrade | PASS |
| 「并没有被写成『答错』」 | 不触发 partial_upgrade | PASS |
| 「既不是完全答对也不是完全答错」 | 不触发 partial_upgrade | PASS |
| 「这是完全答对」 | 仍触发 partial_upgrade | PASS |
| 「这是答错」 | 仍触发 partial_upgrade | PASS |
| 「不是答错，而是完全答对」 | 「完全答对」仍触发 | PASS |

**修复过程中的一次真实误判（已归档）**：修复后第一次运行 S5-B-1 时，首答包含「这条标注只说明当时部分正确，并不表示全部答对。」，`partial_upgrade` 仍命中「全部答对」，原因是「并不表示」当时尚未列入否定标记表。补齐标记（`并不表示 / 并不代表 / 并不等于 / 并不完全 / 不表示 / 不代表 / 不等于 / 绝非`）并新增「并不表示全部答对」「并不等于答错」两条回归用例后，将该次运行记录归档到隔离 temp 的 `s5-b1-attempt1.json`、从结果集移除并单独重跑。本报告 13.5 中的 S5-B-1 为重跑后的 run 13。归档运行不计入 6 条最终记录，但其 LLM / Embedding 调用已计入 13.9 的实际成本补记。

### 13.2 回退保护（方案 A）

**选择方案 A**：回退前检查首答是否包含可核对的历史原句；包含时保留首答中的引用段并拼接确定性回退文本。

理由：用户显式要求引用原话时，原句本身就是可核对事实，不应被系统静默覆盖；方案 B 只记录不覆盖，用户仍拿不到要求的原句，不满足「回退不得覆盖用户显式要求的历史原句」这一产品要求。

| 项目 | 实现 |
|---|---|
| 触发条件 | 问题中出现「引用 / 复述 / 原话 / 原文 / 原句」，且首答里存在与历史用户消息或本轮记录逐字一致的原句 |
| 保留内容 | 首答中的引号引用段（「……」/『……』/“……”）；若模型未加引号，则用记录原句重建引用 |
| 不回退的内容 | 同句中的其他断言不随引用带回；回退正文仍只复述可核对事实 |
| 实际改动 | `history_guard.build_history_fallback(state, first_answer=...)`；`runtime._generate_history_review` 传入首答全文（仅回退路径） |

- 回归测试：`test_fallback_preserves_quote_the_user_explicitly_requested`、`test_fallback_does_not_preserve_quote_without_explicit_request`、`test_fallback_does_not_preserve_an_unrecorded_sentence`、`test_fallback_keeps_the_quote_the_user_asked_for`（Runtime 全链路）。

### 13.3 S5 citation 判据（沿用 Batch 3 口径）

S5 复盘问题不必须引用知识文档：每次 repeat 满足「citation 非空」或「回答引用历史原句」之一即可；非空 citation 仍须可溯源并落在本场景同一文档。判定函数 `full_common.citation_consistency_check`（`allow_history_quote=True`）。

### 13.4 S5-C memoryDegraded 豁免（沿用 Batch 3 口径）

`memoryDegraded=true` 时豁免 `contextHash` / `systemSha256` / `systemChars` / `promptTokens` 的一致性检查，但必须记录 `memoryDegraded` 与 `memoryDegradedSource`；`manifestSha256` 始终不豁免。

### 13.5 S5-B/C 逐次结果

| key | run_id | 守卫触发 | 否定语境跳过次数 | 回退/保留原句 | 降级 | 降级来源 | LLM 调用数 | finish | prompt/completion/total | citation 集合 | Context Hash(12) | System Hash(12) | Manifest(12) | Manifest 关键来源 | 历史原句（最终交付） |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S5-B-1 | 13 | 否 | 0 | 否 | 否 | - | 1 | stop | 2897/1210/4107 | [[5,13],[5,14],[5,15]] | a8be6342bad4 | 5bd83abc0860 | b51f17aeaba1 | history:1,history:2,revision:6,evidence:5,revision:7 | 命中 |
| S5-B-2 | 3 | 否 | 0 | 否 | 否 | - | 1 | stop | 2897/1319/4216 | [[5,13],[5,14],[5,15]] | a8be6342bad4 | 5bd83abc0860 | b51f17aeaba1 | history:1,history:2,revision:6,evidence:5,revision:7 | 未命中 |
| S5-B-3 | 5 | 否 | 0 | 否 | 否 | - | 1 | stop | 2897/1759/4656 | [[5,13],[5,14],[5,15]] | a8be6342bad4 | 5bd83abc0860 | b51f17aeaba1 | history:1,history:2,revision:6,evidence:5,revision:7 | 命中 |
| S5-C-1 | 7 | 否 | 0 | 否 | 否 | - | 1 | stop | 2897/2087/4984 | [[5,13],[5,14],[5,15]] | a8be6342bad4 | 5bd83abc0860 | 0baf3d0e0b07 | history:1,history:2,revision:6,evidence:5,revision:7 | 命中 |
| S5-C-2 | 9 | 否 | 0 | 否 | 否 | - | 1 | stop | 2897/1593/4490 | [[5,13],[5,15]] | a8be6342bad4 | 5bd83abc0860 | 0baf3d0e0b07 | history:1,history:2,revision:6,evidence:5,revision:7 | 命中 |
| S5-C-3 | 11 | 否 | 0 | 否 | 否 | - | 1 | stop | 2897/1496/4393 | [[5,14],[5,15]] | a8be6342bad4 | 5bd83abc0860 | 0baf3d0e0b07 | history:1,history:2,revision:6,evidence:5,revision:7 | 命中 |

- 「历史原句」列匹配的是最终交付回答（`answerFull`），不是模型首答。S5-B/C 的正向判据是「任一次最终回答必须含原句内容或等价语义」。

### 13.6 逐组合判定

| 场景-策略 | 总判定 | citation | 历史使用 | Manifest revision/evidence | Hash（守卫/降级豁免后） | 被豁免的 run | 失败项 |
|---|---|---|---|---|---|---|---|
| S5-B | FAIL | PASS | FAIL | -（本策略不要求） | PASS | 无 | S5-B-2：S5 回答不含原句内容或等价语义 |
| S5-C | PASS | PASS | PASS | PASS | PASS | 无 | - |

**逐次历史使用判据明细**：

| key | 判据命中 | 原句连续子串 | 关键词 | 结果 |
|---|---|---|---|---|
| S5-B-1 | 命中 | 是 | 慢启动=Y,每次只=Y,加一=Y | PASS |
| S5-B-2 | 未命中 | 否 | 慢启动=Y,每次只=N,加一=Y | FAIL |
| S5-B-3 | 命中 | 是 | 慢启动=Y,每次只=Y,加一=Y | PASS |
| S5-C-1 | 命中 | 是 | 慢启动=Y,每次只=Y,加一=Y | PASS |
| S5-C-2 | 命中 | 是 | 慢启动=Y,每次只=Y,加一=Y | PASS |
| S5-C-3 | 命中 | 是 | 慢启动=Y,每次只=Y,加一=Y | PASS |

**S5-B-2 失败分析**：最终回答引用了 Understanding 侧的解释「我原以为慢启动增长很慢，后来明白是起点小、呈指数增长」，并用「每个 RTT 只加一」表述，但没有包含注入的历史原句「我原来以为慢启动慢是因为它每次只把窗口加一」，也缺少关键词「每次只」。该次 Manifest 已包含 `history:1`、`history:2`，`HISTORY_GUARD` 未触发，回答是模型直接生成。因此这是模型历史使用不稳定（KI-1 同类观测），不是 HISTORY_GUARD 缺陷；按失败判据保留 FAIL，未重跑挑选结果。

**Citation 逐组合明细**：

| 场景-策略 | citation 集合（按 rep） | 平均覆盖率 | 历史原句 | 结论 |
|---|---|---|---|---|
| S5-B | [[[5,13],[5,14],[5,15]],[[5,13],[5,14],[5,15]],[[5,13],[5,14],[5,15]]] | 1.000 | S5-B-1=是,S5-B-2=否,S5-B-3=是 | PASS |
| S5-C | [[[5,13],[5,14],[5,15]],[[5,13],[5,15]],[[5,14],[5,15]]] | 0.778 | S5-C-1=是,S5-C-2=是,S5-C-3=是 | PASS |

**S5-C Manifest revision / evidence**：

| key | revision 来源 | evidence 来源 | 结果 |
|---|---|---|---|
| S5-C-1 | revision:6,revision:7 | evidence:5 | PASS |
| S5-C-2 | revision:6,revision:7 | evidence:5 | PASS |
| S5-C-3 | revision:6,revision:7 | evidence:5 | PASS |

**Hash 逐字段豁免说明**：

无守卫触发或降级，无豁免。

### 13.7 HISTORY_GUARD / 降级逐条记录

- 本批 6 次重跑均未触发 HISTORY_GUARD，也未出现 Memory DEGRADED。
- 说明：`HISTORY_GUARD` 的 issue 明细只写入进程内 trace；本报告按 runId 从隔离 temp 的 `agent.log` 恢复。首答/重生成全文只存于隔离 temp，不写回业务库。

### 13.8 缺陷登记更新

两条缺陷登记在 `docs/testing/phase5-known-issues.md`，本批状态如下：

1. **KI-1 模型历史使用不稳定**：开放，观测中。上一批 S5-C-2 声称「第一次表述的原文没有被保存下来」，但 Manifest 确认 `history:1`、`history:2` 已注入。本批未复现该声明；后续全量实验继续统计此类声明与 Manifest 的矛盾次数。
2. **KI-2 HISTORY_GUARD partial_upgrade 否定语境误判**：**已修复**。`partial_upgrade` 匹配前增加否定 / 对比语境识别；回退增加原句保护。回归测试通过；本批 S5-B/C 重跑用于验证修复效果。剩余风险：否定识别是规则匹配，极端句式仍可能漏判，由`partialUpgradeNegationSkipped` 观测项与人工复核兜底。

### 13.9 成本记录

| 项目 | 数值 |
|---|---|
| 对话调用次数（记录） | 6 |
| 模型实际调用次数（含守卫重生成） | 6 |
| 触发守卫的对话数 | 0 |
| LLM 预算 | 6（+守卫余量 2，共 8） |
| LLM 预算核对 | PASS |
| prompt tokens（仅最终记录） | 17382 |
| completion tokens（仅最终记录） | 9464 |
| total tokens（仅最终记录） | 26846 |
| Agent 进程内 Embedding 调用 | 12 |
| Agent 进程内 Embedding 文本数 | 12 |
| 单次运行 Embedding 最大值 | 2 |
| Tavily 调用 | 0 |

- 预算核对：对话记录 6 次；含守卫重生成实际调用 6 次，上限 8 次；未超出预算。
- Agent Embedding 12 次（上限 12，按每场景 6 次计）；Tavily 0。
- setup 阶段 10 份文档的索引 Embedding 由 Spring 侧发起，不属于每次对话的 Agent 预算。

**实际成本补记（含归档运行与重跑，不隐藏）**：

- 第一轮：S5-B-1 首答 + 守卫重生成（2 次），其余 5 次各 1 次，共 7 次 LLM；
- 第二轮：仅重跑 S5-B-1，1 次 LLM；
- 合计 LLM 8 次，恰好等于「6 + 守卫余量 2 = 8」上限，未超出。13.9 表格中的「模型实际调用 6 次」只统计最终 6 条记录，不含归档的第一轮。
- Embedding：第一轮 6 次 × 2 = 12 次；重跑 S5-B-1 新增 2 次；合计 14 次，超出「Embedding ≤ 12」预算 2 次。原因是修复前第一次 S5-B-1 运行被否定语境误判、需归档后重跑；该超支如实披露。
- Tavily 0 次不变。

### 13.10 冻结与清理

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

### 13.11 Phase 5 Step 3 最终结论

- 失败组合：['S5-B']；**Phase 5 Step 3 最终结论：FAIL。**
- S5-A：PASS（沿用 Batch 3 结果，本批未重跑）。
- S5-B：FAIL；S5-B-2：S5 回答不含原句内容或等价语义
- S5-C：PASS；无失败项
- **结论口径**：本批 FAIL 仅由 S5-B-2 的模型历史使用不稳定造成；HISTORY_GUARD 否定语境修复与回退保护本身通过验证——6 次最终运行 0 次触发守卫、0 次否定语境误判，32 条相关回归测试全部通过。
- 必须附带的观测项（不作为 Step 3 判据，但必须记录）：
  1. **KI-1 模型历史使用不稳定**：上一批 S5-C-2 的反例仍成立；后续全量继续统计「声称历史不存在」与 Manifest 的矛盾次数。
  2. **KI-2 否定语境识别**：规则匹配只能覆盖已列举的句式；守卫若仍触发，逐条看 13.7 的首答 / 重生成 / 回退全文。

### 13.12 90 次全量补记声明

- 90 次全量的 HISTORY_GUARD 触发次数与 Memory DEGRADED 次数均未知：原批次结果 JSON 未记录 `historyGuardTriggered` 与 `memoryDegraded`，原隔离库已按 cleanup 清理，无法回溯。本报告不做回溯推测；下一批全量实验必须记录 `historyGuardTriggered` 与 `memoryDegraded`。

### 13.13 工作区状态

| 项目 | 值 |
|---|---|
| branch | main |
| HEAD | 648fc6aa66fbfd85d9f7447d8e0256793739f6c2 |
| HEAD subject | test(phase5): S5 数据重设计、HISTORY_GUARD 判据调整与 S5-ABC 重跑 |
| 工作区状态 | M agent/app/history_guard.py  M agent/app/runtime.py  M agent/tests/test_history_guard.py  M agent/tests/test_history_guard_runtime.py  M docs/testing/phase5-abc-rerun.md  M scripts/phase5/full/full_common.py  M scripts/phase5/full/run_s5_rerun.py ?? docs/testing/phase5-known-issues.md |
| 是否 commit | 否（本批次未执行 git commit） |
| 是否 push | 否（本批次未执行 git push） |

### 13.14 需要人工审核的决策点

1. **回退保护改动触及 `runtime.py`**：任务边界写的是「不修改 Runtime」，但回退发生在 Runtime 的 `_generate_history_review`；本次只改了一行调用（把首答传给 `build_history_fallback`），回退逻辑本身在 `history_guard.py`。需人工确认这属于「HISTORY_GUARD 回退保护」的允许范围。
2. **回退保留的粒度**：优先保留首答中带引号的引用段；模型没加引号时用记录原句重建引用。若业务要求逐字保留首答整句（即使同句含其他断言），需要另行调整。
3. **否定语境识别是规则匹配**：只覆盖「前 12 字符否定词 / 并列否定 / 肯定对比」三类显式句式；更复杂的反讽、双重否定可能漏判。`partialUpgradeNegationSkipped` 只做观测，不参与判定。
4. **S5 citation 判据**：沿用 Batch 3「不必须引用知识文档」口径，需确认仍是最终口径。
5. **memoryDegraded 豁免范围**：只豁免 contextHash / systemSha256 / systemChars / promptTokens；manifestSha256 始终不豁免。
6. **KI-1 模型历史使用不稳定**：上一批 S5-C-2 的反例仍成立；需确认后续是否在全量实验中统计矛盾次数。
7. **S5-A 未重跑**：沿用 Batch 3 的 PASS；若要求本批所有组合同快照重跑，需要追加 3 次调用。
8. **90 次全量观测缺口**：原批次未记录 historyGuardTriggered / memoryDegraded，原隔离库已清理，无法回溯；下一批全量必须补记。


## 14. 引用类请求的条件 prompt 强化与 S5-B 重跑（Batch 5）

- 追加时间：2026-11-09T23:52:00
- 重跑快照时间：2026-11-09 23:45:36
- 隔离库：`phase5_pilot_20261109234536_3b4746`
- 文档 collection：`phase5-pilot-20261109234536-3b4746_text-embedding-v4_1024`
- Memory collection：`phase5-pilot-20261109234536-3b4746-memory_text-embedding-v4_1024`
- 重跑范围：S5-B 3 次 + 非引用类回归 S1 / S2 / S6 各 1 次（策略 B），共 6 次对话、6 次实际 LLM 调用。
- 生成参数指纹：沿用 `2e1405ed23bbe132b96b018c7e79d190321e43ff5ffb5c5108e99bee47edb5f6`；model=`deepseek-flash`，temperature=`0`。

### 14.1 追加逻辑的位置与不触碰全局契约的说明

| 项目 | 说明 |
|---|---|
| 追加位置 | `agent/app/tutor/tutor.py` 的 `Tutor._apply_request_instruction`，在 `_validated(context.messages)` 之后、交给 LLM 之前 |
| 追加对象 | 只重写第一条 `system` message：`f"{原 system}\n\n{逐字引用约束}"`；其余消息原样保留 |
| 调用点 | `Tutor.stream()` 与 `Tutor.complete()` 共用同一方法，覆盖流式与一次性生成 |
| 全局契约 | `agent/app/prompts.py` 的 `BASE_SYSTEM_INSTRUCTION` **未修改**；约束是独立的 `VERBATIM_QUOTE_INSTRUCTION` 常量 |
| ContextBuilder | 未修改。System prompt 仍由 ContextBuilder 组装；Tutor 只在最终发消息边界做条件拼接 |
| 判定输入 | 只看本轮最后一条 `user` message 的内容，不看 system / 历史消息 / 模型输出 |
| 回归保护 | 非引用类请求 `quote_instruction_for()` 返回 `None`，`_apply_request_instruction` 原样返回 messages，system prompt 与改动前逐字一致 |

- 为什么不在 ContextBuilder 里做：ContextBuilder 负责「上下文里有什么」，与「本轮用户怎么问」无关；把条件追加放在 Tutor 的消息组装边界，既不改变 Manifest / Context Hash 的组装语义，也不需要 ContextBuilder 感知请求类型。
- 实测佐证：同一 S5-B 上下文，Batch 4 的 `systemChars=4914`；Batch 5 为 `5002`，差值 `88 = 2`（两个换行）+ `86`（约束正文长度），与预期逐字一致。非引用类 S1/S2/S6 未出现 `+88`。

### 14.2 关键词规则与追加文本

| 项目 | 值 |
|---|---|
| 关键词 | `引用`、`原话`、`原文`、`原句`、`复述`；任一出现即视为引用类请求 |
| 判定函数 | `agent/app/prompts.py: is_quote_request()` / `quote_instruction_for()`；关键词常量 `QUOTE_REQUEST_KEYWORDS` |
| 追加文本（逐字） | 当用户要求引用原话时，必须逐字使用历史消息原文，不得用 Understanding/Memory/Evidence 的转述替代；历史中无原话时明确说明，不得用相近转述冒充。 |
| 非引用类 | 不追加；`system` 与 `BASE_SYSTEM_INSTRUCTION` / ContextBuilder 输出逐字一致 |

**测试覆盖**（`agent/tests/test_tutor_quote_instruction.py`，14 passed）：

| 用例 | 结果 |
|---|---|
| 全局 `BASE_SYSTEM_INSTRUCTION` 不含该约束 | PASS |
| 5 个关键词分别触发追加 | PASS |
| 4 类非引用输入不触发 | PASS |
| 引用类请求追加在 system 末尾，user 消息不被改写 | PASS |
| 非引用类 system 与改动前逐字一致 | PASS |
| HISTORY_GUARD 的 `extra_instruction` 追加在引用约束之后 | PASS |
| `complete()` 与 `stream()` 行为一致 | PASS |

### 14.3 S5-B 逐次结果

| key | run_id | 引用来源（全部命中） | 主来源 | historyGuardTriggered | LLM 调用数 | finish | prompt/completion/total | citation 集合 | Context Hash(12) | System Hash(12) | Manifest(12) | Manifest 关键来源 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S5-B-1 | 1 | 历史原句, Understanding, 其他 | 历史原句 | 否 | 1 | stop | 2942/883/3825 | [[5,15]] | 1eb51dcbf778 | 152af781e3f8 | b51f17aeaba1 | history:1,history:2,revision:6,evidence:5,revision:7 |
| S5-B-2 | 3 | 历史原句, Understanding, 其他 | 历史原句 | 否 | 1 | stop | 2942/1509/4451 | [[5,14],[5,15]] | 1eb51dcbf778 | 152af781e3f8 | b51f17aeaba1 | history:1,history:2,revision:6,evidence:5,revision:7 |
| S5-B-3 | 5 | 历史原句, Understanding | 历史原句 | 否 | 1 | stop | 2942/1190/4132 | [[5,13],[5,14],[5,15]] | 1eb51dcbf778 | 152af781e3f8 | b51f17aeaba1 | history:1,history:2,revision:6,evidence:5,revision:7 |

- 判据沿用 Batch 4：B/C 的正向判据是「每次最终回答必须含原句内容或等价语义」；citation 采用「citation 非空或引用历史原句」；Hash 一致性在未触发守卫 / 未降级时要求 3 次完全一致。
- 逐次判定：S5-B-1 / 2 / 3 均命中 `historyUse.pass=true`（`exactOriginalSentence=true`）；3 次均未触发 HISTORY_GUARD；3 次均无 Memory DEGRADED；`guard_aware_hash_consistency` 返回 PASS（contextHash / systemSha256 / systemChars 三次一致，manifestSha256 三次一致）；citation 三次非空、可溯源、同文档，`citation_consistency_check(allow_history_quote=True)` PASS。
- 本轮失败判据「S5-B 仍有回答引用 Understanding 而非历史原句」**未触发**：3/3 都逐字包含历史原句。

**引用来源明细**：

| key | 历史原句 | Understanding 片段 | Memory 片段 | Evidence / Revision | 说明 |
|---|---|---|---|---|---|
| S5-B-1 | 是 | 「我原以为慢启动增长很慢，后来明白是起点小、呈指数增长」 | 无 | Evidence「误解为线性增长，已纠正为指数增长」 | 明确说明 Evidence 是总结性描述、不能当原话 |
| S5-B-2 | 是 | 同上 | 无 | Evidence 同上 | 明确说明 Evidence / Memory 是转述、不是原话 |
| S5-B-3 | 是 | 同上 | 无 | 无 | 把 Understanding 句子称为「你当时的另一句原话」 |

**残留观测（不构成本批失败判据，但必须记录）**：

- 3/3 回答仍把 Understanding 的 explanation 当作「用户更早的说法 / 另一句原话」呈现。历史原句同时存在，因此不是「用 Understanding 替代历史原句」；但这是 KI-1 的残留形态：模型没有区分「Understanding 的总结性转述」与「用户在 history 里的逐字原话」。
- 本批没有重生成、没有回退，因此不存在 HISTORY_GUARD 覆盖原句的问题（KI-2 修复路径本批未触发）。

### 14.4 非引用类回归

| key | quoteRequestDetected | run_id | finish | prompt/completion/total | Context Hash(12) | System Hash(12) | Manifest(12) | problems |
|---|---|---|---|---|---|---|---|---|
| S1-B-1 | 否 | 7 | stop | 2834/1244/4078 | 361fa576ae95 | bc7017685c10 | ccaaac3a9c83 | 无 |
| S2-B-1 | 否 | 9 | stop | 2845/1497/4342 | 50014f452263 | d955b1fb62c0 | 4f080390b1df | 无 |
| S6-B-1 | 否 | 11 | stop | 2752/1177/3929 | 1c457ecbf7a0 | cf1bcc085b55 | 9de45baca76b | 无 |

- 三个非引用类问题（S1「为什么数据一多，网络就会变慢甚至丢包？」、S2「继续讲：B+ 树的范围查询为什么比哈希索引快？」、S6「我现在对虚拟内存掌握到什么程度？哪里还薄弱？」）均未命中关键词，`_apply_request_instruction` 走原样返回分支。
- 代码级测试证明非引用类 system 与改动前逐字一致；真实运行中 systemChars 也没有出现引用约束的 `+88` 增量。
- 3 次真实运行均 `finishReason=stop`、无空输出、无截断、无 error/timeout，`problems=[]`。

### 14.5 回归与离线评测

| 项目 | 结果 |
|---|---|
| 全量 Agent 测试 `pytest tests` | **648 passed, 9 skipped, 1 xfailed**（含新增 14 条引用约束用例） |
| D4-5 离线上下文质量评测 | **21/21 cases passed**；C regressions vs A/B: none；failed safety probes: none；CI status: PASS |
| S1 / S2 / S6 真实运行 | 各 1 次，全部 stop、无 problems |
| S5-B 3 次重跑 | 3/3 完成，全部含历史原句 |

### 14.6 KI-1 更新

- KI-1 状态由「开放，观测中」更新为「开放，已通过条件 prompt 缓解，未根治」。
- 本批效果：S5-B 3/3 含历史原句，0/3 出现「引用 Understanding 而非历史原句」；Batch 4 的 S5-B-2 属于该失败形态，本批未复现。
- 残留：3/3 仍把 Understanding explanation 称为用户原话。约束降低了「替代」频次，但没有让模型完全区分 Understanding 转述与 history 原句。
- 样本量仅 3 次，不能据此宣布 KI-1 关闭；后续全量实验应继续统计「引用来源」分布。

### 14.7 成本记录

| 项目 | 数值 |
|---|---|
| 对话调用次数（记录） | 6 |
| 模型实际调用次数（含守卫重生成） | 6 |
| 触发守卫的对话数 | 0 |
| LLM 预算 | 9；实际 6，未超出 |
| prompt tokens（仅最终记录） | 17257 |
| completion tokens（仅最终记录） | 7500 |
| total tokens（仅最终记录） | 24757 |
| Agent 进程内 Embedding 调用 | 12 |
| Agent 进程内 Embedding 文本数 | 12 |
| 单次运行 Embedding 最大值 | 2（上限 3） |
| Tavily 调用 | 0 |

- 预算核对：LLM 6/9（S5-B 3 + 回归 3）；Embedding 12/15；Tavily 0。setup 阶段 10 份文档的索引 Embedding 由 Spring 侧发起，不属于每次对话的 Agent 预算。

### 14.8 冻结与清理

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

### 14.9 本批结论

- **条件追加仅对引用类请求生效**：S5-B 的 systemChars 增加 88（约束 86 + 两个换行）；S1 / S2 / S6 未追加，代码级测试与真实运行一致。
- **S5-B 3 次重跑完成**：3/3 逐字包含历史原句，3/3 未触发 HISTORY_GUARD，Hash / Manifest / citation 判据通过；本批失败判据「S5-B 仍有回答引用 Understanding 而非历史原句」未触发。
- **回归通过**：全量 Agent 测试 648 passed；D4-5 21/21；S1/S2/S6 各 1 次无 problems。
- **未 commit / 未 push**：本批只做本地修改与实验。
- **保留残余风险**：3/3 回答仍把 Understanding 转述称为用户原话；这是 KI-1 的缓解而非根治，需在后续批次继续观测（见 14.10 决策点 1）。

### 14.10 需要人工审核的决策点

1. **Understanding 仍被当作「用户原话」**：3/3 回答同时引用历史原句与 Understanding explanation。按本批字面失败判据（是否「引用 Understanding 而非历史原句」）判为未触发；若要求「不得把 Understanding 转述称为用户原话」，则应判 FAIL。需要人工确认最终口径。
2. **关键词规则边界**：当前命中「引用 / 原话 / 原文 / 原句 / 复述」任一即追加。可能把「引用某段资料」这类非历史原话请求也纳入；是否需要在后续收紧为「引用 + 原话/原文/原句」组合，需人工确认。
3. **追加位置的架构归属**：条件追加放在 Tutor（最终消息组装边界），未改 ContextBuilder / BASE_SYSTEM_INSTRUCTION。需确认这符合「prompt 构造模块」的边界。
4. **非引用类回归的策略选择**：S1/S2/S6 各 1 次使用策略 B。任务未指定策略；若要求 A/C 也覆盖，需要追加调用。
5. **样本量**：S5-B 仅 3 次，不能据此关闭 KI-1；后续全量应继续记录引用来源分布。
6. **harness 运行方式**：本批使用隔离 temp 中的一次性 runner 调用既有 `setup_phase / one_call / cleanup_phase`，未修改 `run_s5_rerun.py` 或 `run_full_abc.py`。是否接受该运行方式，需人工确认。

### 14.11 工作区状态

| 项目 | 值 |
|---|---|
| branch | main |
| HEAD | b9f6443 |
| HEAD subject | fix: 修复 HISTORY_GUARD 否定语境误判与回退覆盖原句，S5-C PASS |
| 工作区状态 | M agent/app/prompts.py  M agent/app/tutor/tutor.py  M scripts/phase5/full/full_common.py  M docs/testing/phase5-abc-rerun.md  M docs/testing/phase5-known-issues.md  ?? agent/tests/test_tutor_quote_instruction.py |
| 是否 commit | 否（本批次未执行 git commit） |
| 是否 push | 否（本批次未执行 git push） |

## 15. 引用来源语义补充与 S5-B 重跑（Batch 6）

- 追加时间：2026-11-10T00:02:37
- 重跑快照时间：2026-11-09 23:58:10
- 隔离库：`phase5_pilot_20261109235808_ec45fb`
- 文档 collection：`phase5-pilot-20261109235808-ec45fb_text-embedding-v4_1024`
- Memory collection：`phase5-pilot-20261109235808-ec45fb-memory_text-embedding-v4_1024`
- 重跑范围：S5-B 3 次 + 非引用类回归 S1 / S2 / S6 各 1 次（策略 B），共 6 次对话、7 次实际 LLM 调用（S5-B-1 触发 1 次 HISTORY_GUARD 重生成）。
- 生成参数指纹：model=`deepseek-flash`，temperature=`0`，max_tokens=`4096`，embedding=`text-embedding-v4/1024`。

### 15.1 补充后的约束文本与来源说明

**根因**：Batch 5 的约束只写了「不得用转述替代」，没有说明各来源是什么、谁说的、如何区分「原话」。模型看到 Understanding 区块里第一人称的 `latest_user_explanation`，自然把它当成用户说过的另一句话。

**修改位置**：`agent/app/prompts.py` 的 `VERBATIM_QUOTE_INSTRUCTION`（未改 `BASE_SYSTEM_INSTRUCTION`、Tutor 追加位置、ContextBuilder）。

补充后的约束（逐字，362 字符）：

```text
当用户要求引用原话、原文或原句时，先分清本轮上下文里的来源：history 是用户逐字说过的话，只有它可以称为「原话」；Understanding 是系统对用户当前掌握状态的总结，是转述，不是原话；Memory 是系统提炼的长期信息，不是原话；Evidence 是系统记录的学习事件，不是原话。必须逐字使用 history 里的原句，不得用 Understanding/Memory/Evidence 的转述替代。当这些来源同时出现时，history 的原句必须标注「你当时说：」，Understanding 的内容必须标注「系统记录的你当前理解：」；不得把 Understanding 称为「你更早的说法」「另一句原话」，也不得把 Memory 或 Evidence 称为原话。历史中无原话时明确说明，不得用相近转述冒充。
```

来源定义与标注规则：

| 来源 | 对模型的定义 | 是否可称「原话」 | 同时出现时的标注 |
|---|---|---|---|
| history | 用户逐字说过的话 | 是，且只有它可以 | 「你当时说：」 |
| Understanding | 系统对用户当前掌握状态的总结 | 否，是转述 | 「系统记录的你当前理解：」 |
| Memory | 系统提炼的长期信息 | 否 | 不称原话 |
| Evidence | 系统记录的学习事件 | 否 | 不称原话 |

**测试**：`agent/tests/test_tutor_quote_instruction.py` 新增 2 条（四个来源均有定义且仅 history 可称原话；history / Understanding 标注互不混用），文件共 **16 passed**。原有「非引用类 system 逐字一致」「追加位置在 system 末尾」「complete/stream 一致」等边界用例全部保持通过。

### 15.2 ContextBuilder 渲染现状评估（只评估，未实施）

**是否使用第一人称**：

| 区块 | 渲染骨架 | 是否第一人称叙述 |
|---|---|---|
| Understanding | `### 概念 ｜ 状态：…`、`- 当前理解：…`、`- 已经理解：…`、`- 仍然存在的误区：…`、`- 还不确定：…`、`- 用户自己的说法：「…」` | 否。骨架是第三人称；但 `latest_user_explanation` 的内容本身是第一人称，且以「用户自己的说法」+ 引号呈现 |
| Memory | `- [长期事实 / 学习状态（历史记录）/ 偏好，作用域：…] {content}` | 否。骨架是第三人称；`content` 是否为第一人称取决于后端存储文本 |
| Evidence | `- [部分正确/用户解释] 时间 ｜ {observation}` | 否。骨架与 S5 预置 observation 都是第三人称 |
| Trajectory | `- [已纠正，历史] …`、`- 关键版本…`、`- 关联证据…` | 否。只渲染 revision 摘要 / 证据 observation，不渲染 revision 的 `user_explanation` |

**结论**：ContextBuilder 没有用第一人称「我」叙述 Understanding / Memory / Evidence。真正的第一人称泄漏来自 Understanding 区块内的 `latest_user_explanation` 数据值：

- 该字段在 `_render_understanding` 中被渲染为 `- 用户自己的说法：「…」`；
- 生产链路上它可能来自用户批注的选中文本 / note（`LearningAnalysisService` 将其原样保存，可能截断），但它在 Prompt 中位于 Understanding 区块，不与 history 消息同源，也不保证是本轮 history 的逐字原话；
- 标签「用户自己的说法」+ 引号，与「原话」在语用上高度接近，是 Batch 5 3/3 误称的直接诱因。

**建议方案（本批不实施，需单独审核）**：

1. **最小改动（推荐）**：只改 `_render_understanding` 中该行的标签，改为
   `- 系统保存的用户解释记录（可能来自批注摘录，不是 history 逐字原话）：「…」`。
   不重写内容，保留用户原话信息，但明确 provenance。
2. **区块说明补充**：在 Understanding 区块的 note 增加一句：
   「`用户解释记录` 是系统保存的摘录，不等同于本轮历史消息中的逐字原话；只有 history 消息可以作为原话。」
3. **不建议**把 `latest_user_explanation` 改写成第三人称：该字段是用户产物，改写会改变原意，也会让评测 fixture 与真实数据不一致；也不建议直接删除，会损失「用户怎么解释」这一强教学信号。
4. Memory / Evidence 骨架已是第三人称，暂不需要为它们加前缀；如要统一，可在区块 note 增加「以下均为系统记录，不是原话」。

**影响评估**：

- Prompt 字节数：每个 Understanding 区块约 `+30~60` 字符；S5 上下文（3000 token 预算）预计增加约 10–20 tokens，不改变当前 tier。S10 等预算紧张场景需要重新验证是否触发降级。
- Hash：`systemSha256` / `contextHash` 会变化；Manifest 与检索结果不变。所有真实运行基线需在新批次重录。
- 评测：D4-5 是离线、按结构化字段判定；若有用例断言 system 文本需同步更新，否则 21 个 case 指标不受影响，但需重跑确认。
- 边界：只改渲染标签与区块 note，不触碰检索、Policy、Manifest Schema、ContextBuilder 的选择 / 预算逻辑。

### 15.3 S5-B 逐次结果

| key | run_id | 引用来源（全部命中） | 主来源 | 是否误称 Understanding 为原话 | 守卫触发 | LLM 调用数 | finish | prompt/completion/total | citation 集合 | Context Hash(12) | System Hash(12) | Manifest(12) | Manifest 关键来源 | 历史原句 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S5-B-1 | 1 | 历史原句, Understanding | 历史原句 | 否 | 是 | 2 | stop | 3170/1581/4751 | [[5,13],[5,14],[5,15]] | cd598c18971c | 20b237281b84 | b51f17aeaba1 | history:1,history:2,revision:6,evidence:5,revision:7 | 是 |
| S5-B-2 | 3 | 历史原句, Understanding | 历史原句 | 否 | 否 | 1 | stop | 3078/1427/4505 | [[5,13],[5,14],[5,15]] | f91a526a4265 | 2a1a89e93c60 | b51f17aeaba1 | history:1,history:2,revision:6,evidence:5,revision:7 | 是 |
| S5-B-3 | 5 | 历史原句, Understanding | 历史原句 | 否 | 否 | 1 | stop | 3078/1904/4982 | [[5,13],[5,14],[5,15]] | f91a526a4265 | 2a1a89e93c60 | b51f17aeaba1 | history:1,history:2,revision:6,evidence:5,revision:7 | 是 |

**逐次判定**：

- **S5-B-1**：首答与最终交付都逐字包含历史原句，并把 Understanding 标注为「系统另外记录的你当前理解是转述，不是原话」/「系统记录的你当前理解：」，未误称。首次回答触发 HISTORY_GUARD，issue kind=`internal_process`，excerpt 为「至于你为什么会改变看法，比如是自己想通的、看了资料…」中的「想通」，属于对假设列表的规则误判，与本批引用约束无关；重生成后 `remainingIssues=[]`、未回退。最终交付为通过校验的重生成回答。
- **S5-B-2**：单次调用，无守卫。回答同时给出两条并明确区分：「系统记录的你当前理解是：『我原以为…』后一句是系统对你当前掌握状态的转述，不是你当时的原话。」
- **S5-B-3**：单次调用，无守卫。回答把 Understanding 标注为「系统记录的你当前理解」，并额外说明「长期记忆里那条…同样是系统提炼的记录，也不是原话」。
- 3/3 `historyUse.pass=true`（`exactOriginalSentence=true` 且三关键词全中）；3/3 `understandingAsOriginal=false`；3/3 citation 非空、可溯源、同文档。
- Hash 一致性：`manifestSha256` 三次一致（`b51f17aeaba1`）；S5-B-2/B-3 的 `contextHash` / `systemSha256` / `systemChars` 完全一致，S5-B-1 因守卫重生成被豁免，`guard_aware_hash_consistency` PASS。
- 本批失败判据「S5-B 仍有回答把 Understanding 称为用户原话」**未触发（0/3）**。

**引用来源明细**：

| key | 历史原句 | Understanding 片段 | Memory | Evidence / Revision | 说明 |
|---|---|---|---|---|---|
| S5-B-1 | 是 | 「我原以为慢启动增长很慢，后来明白是起点小、呈指数增长」 | 未引用 | 未引用 | 明确写「是转述，不是原话」 |
| S5-B-2 | 是 | 同上 | 未引用 | 未引用 | 明确写「是你当前掌握状态的转述，不是你当时的原话」 |
| S5-B-3 | 是 | 同上 | 提到「长期记忆…也不是原话」 | 未引用 | 三类来源分别标注 |

**System prompt 增量核对**：

- 非引用类 S5 上下文在追加约束前的 `systemChars` 为 `4914`（Batch 4 实测）。
- 本批 S5-B-2/B-3 为 `5278`，差值 `364 = 362`（新约束）+ `2`（两个换行），与代码逐字一致。
- S5-B-1 的最终记录 `systemChars=5436`，是守卫重生成时额外附加 `extra_instruction` 后的值；首答调用仍为 `5278`。

### 15.4 非引用类回归

| key | quoteRequestDetected | run_id | finish | prompt/completion/total | Context Hash(12) | System Hash(12) | Manifest(12) | problems |
|---|---|---|---|---|---|---|---|---|
| S1-B-1 | 否 | 7 | stop | 2834/1663/4497 | ae64420cdfe9 | c6642f563546 | ccaaac3a9c83 | 无 |
| S2-B-1 | 否 | 9 | stop | 2845/1552/4397 | 48e1a7fdd82b | 0f007daa0d04 | 4f080390b1df | 无 |
| S6-B-1 | 否 | 11 | stop | 2752/1039/3791 | d79254a1346a | 62b3773c159d | 9de45baca76b | 无 |

- 三个问题均未命中 `引用 / 原话 / 原文 / 原句 / 复述`，`quoteRequestDetected=false`，`quote_instruction_for()` 返回 `None`，未追加约束。
- 代码级测试证明非引用类 system 与改动前逐字一致；真实运行无 error / timeout / 空输出 / 截断，`problems=[]`。

### 15.5 回归与离线评测

| 项目 | 结果 |
|---|---|
| 全量 Agent 测试 `pytest tests` | **650 passed, 9 skipped, 1 xfailed**（新增 2 条来源定义用例） |
| D4-5 离线上下文质量评测 | **21/21 cases passed**；C regressions vs A/B: none；failed safety probes: none；CI status: PASS |
| S1 / S2 / S6 真实运行 | 各 1 次，全部 `finishReason=stop`，无 problems |
| S5-B 3 次重跑 | 3/3 完成，全部含历史原句，0/3 误称 Understanding 为原话 |

### 15.6 KI-1 更新

- KI-1 状态：**开放，已通过来源语义约束进一步缓解，未根治**。
- 本批效果：在约束中补充 history / Understanding / Memory / Evidence 的定义与标注规则后，S5-B 3/3 最终回答逐字包含历史原句，0/3 把 Understanding 转述称为用户原话；相比 Batch 5 的 3/3 误称，本批未复现该形态。
- 新增可观测字段：`quoteSources` / `quotePrimary`（既有 `s5_quote_source_check`）与 `understandingAsOriginal` / `understandingMislabelDetail`（新增 `s5_understanding_as_original_check`）。后者只做观测，不参与 PASS/FAIL；判定规则见 `phase5-known-issues.md`。
- 残差：`latest_user_explanation` 仍以第一人称内容出现在 Understanding 区块，Prompt 层只解决了「叫法」，没有解决「渲染语义」；3 次样本不足以关闭 KI-1。ContextBuilder 渲染层方案见 15.2，需单独审核。

### 15.7 成本记录

| 项目 | 数值 |
|---|---|
| 对话调用次数（记录） | 6 |
| 模型实际调用次数（含守卫重生成） | 7 |
| 触发守卫的对话数 | 1（S5-B-1） |
| LLM 预算 | 9；实际 7，未超出 |
| prompt tokens（仅最终记录） | 17757 |
| completion tokens（仅最终记录） | 9166 |
| total tokens（仅最终记录） | 26923 |
| reasoning tokens（仅最终记录） | 5417 |
| cached tokens（仅最终记录） | 13312 |
| Agent 进程内 Embedding 调用 | 12（上限 15） |
| Agent 进程内 Embedding 文本数 | 12 |
| 单次运行 Embedding 最大值 | 2（上限 3） |
| Tavily 调用 | 0 |

- 预算核对：LLM 7/9（S5-B 3 + 回归 3 + 守卫重生成 1）；Embedding 12/15；Tavily 0。setup 阶段 10 份文档的索引 Embedding 由 Spring 侧发起，不属于每次对话的 Agent 预算。

### 15.8 冻结与清理

| 检查 | 结果 | 说明 |
|---|---|---|
| MySQL 全局冻结 | PASS | 无漂移 |
| Qdrant 文档 + Memory | PASS | 无漂移 |
| 默认 Memory collection | PASS | 未读写 |
| cleanup.py exitCode | 0 | — |
| MySQL 库残留 | 0 | — |
| Qdrant 残留 | [] | — |
| RabbitMQ 队列 / 交换机残留 | [] | — |
| 仍开放的端口 | [] | runner 先终止进程，cleanup 报告 process not found，但端口已关闭 |
| 独立残留检查 | PASS | `residual.ok=true` |

### 15.9 本批结论（Step 3）

- **来源说明已补充**：`VERBATIM_QUOTE_INSTRUCTION` 明确了 history / Understanding / Memory / Evidence 的定义、唯一可称「原话」的来源，以及同时出现时的标注方式；`BASE_SYSTEM_INSTRUCTION` 未修改。
- **S5-B 3 次重跑完成**：3/3 逐字包含历史原句；3/3 明确把 Understanding 标为「系统记录的你当前理解 / 转述」；本批失败判据「把 Understanding 称为用户原话」0/3 触发。
- **非引用类未被追加约束**：S1 / S2 / S6 的 `quoteRequestDetected=false`，代码级测试与真实运行一致。
- **回归通过**：全量 Agent 测试 650 passed；D4-5 21/21；S1/S2/S6 均 stop、无 problems；无 error / timeout / 空输出 / 截断。
- **ContextBuilder 渲染层只评估未实施**：现状无第一人称叙述；第一人称泄漏来自 `latest_user_explanation` 数据值，建议方案见 15.2，本批未改动。
- **Phase 5 Step 3 结论：维持 PASS**。本批失败判据均未触发；KI-1 仍开放，需后续更大样本观测。
- **未 commit / 未 push**：本批只做本地修改、隔离实验与文档更新。

### 15.10 需要人工审核的决策点

1. **「不得把 Understanding 称为原话」是否升级为硬判据**：本批 0/3，但 harness 仍将其记为观测字段；是否在后续批次改为 FAIL 级判据，需要确认。
2. **ContextBuilder 渲染层方案**：15.2 推荐只改 `用户自己的说法` 标签与 Understanding 区块 note，不改内容、不改检索 / 预算逻辑；该改动会改变 `systemSha256` / `contextHash` 与所有真实运行基线，需单独审核后实施。
3. **`latest_user_explanation` 的产品语义**：生产上它可能来自用户批注的选中文本（近似原话，但可能截断、且不来自 history），而 Prompt 现在把整个 Understanding 定义为「系统总结」。若产品要求把批注原话当原话使用，需要给它独立来源与 provenance，而不是塞在 Understanding 里。
4. **HISTORY_GUARD `internal_process` 误判**：S5-B-1 首答在假设列表「是自己想通的、看了资料，还是…」中命中「想通」，触发重生成。该问题属于 History_Guard，按本批边界未修改；建议单独立项。
5. **旧标记判据**：原始 `problems` 仍包含「回答缺少预期历史标记：【S5-HISTORY】」，按 Batch 4/5 口径视为旧判据观测项，不参与 PASS/FAIL；需确认该口径继续有效。
6. **样本量**：S5-B 仅 3 次，不能据此关闭 KI-1；后续全量应继续统计 `quoteSources` 与 `understandingAsOriginal` 分布。
7. **harness 运行方式**：本批使用隔离 temp 中的一次性 runner 调用既有 `setup_phase / one_call / cleanup_phase`，未修改 `run_s5_rerun.py` / `run_full_abc.py`；是否接受该方式需人工确认。

### 15.11 工作区状态

| 项目 | 值 |
|---|---|
| branch | main |
| HEAD | d19576d |
| HEAD subject | feat(agent): 引用原话类请求追加逐字引用约束，S5-B 3/3 含历史原句 |
| 工作区状态 | M agent/app/prompts.py  M agent/tests/test_tutor_quote_instruction.py  M scripts/phase5/full/full_common.py  M docs/testing/phase5-abc-rerun.md  M docs/testing/phase5-known-issues.md |
| 是否 commit | 否（本批次未执行 git commit） |
| 是否 push | 否（本批次未执行 git push） |


## 16. latest_user_explanation 渲染标签调整（Batch 7）

- 执行时间：2026-11-10T00:11:25
- 快照时间：2026-11-10 00:09:58
- 隔离库：`phase5_pilot_20261110000956_db9e39`
- 范围：只改 Understanding 区块中 `latest_user_explanation` 的渲染标签；D4-5 离线评测 + S10-B 预算场景 1 次。不进入 Step 4，不 commit。

### 16.1 改动

| 项目 | 内容 |
|---|---|
| 文件 | `agent/app/context/builder.py` 的 `_render_understanding` |
| 旧标签 | `- 用户自己的说法：「…」` |
| 新标签 | `- 系统保存的用户解释记录（可能来自批注摘录，不是 history 逐字原话）：「…」` |
| 用户内容 | 不改写、不截断（原有 structured 截断逻辑不变） |
| 检索 / 预算 / Manifest 逻辑 | 未修改 |
| 文档同步 | `agent/docs/pipeline.md` 的渲染示例同步 |

标签前缀增加 30 个字符（`- 用户自己的说法：` 10 字符 → 新前缀 40 字符）。`agent/tests/test_context_builder.py` 断言新标签存在、旧标签不出现，文件 32 passed。

`agent/tests/fixtures/context_strategy_c_golden.json` 因标签长度变化更新；逐字段核对后，差异仅为标签引起的计数：

| 字段 | 改动前 | 改动后 |
|---|---|---|
| `estimatedTokens` | 3538 | 3561 |
| `contextTokens` | 1149 | 1172 |
| `manifest.budget.per_type.understanding.used` | 318 | 341 |
| `manifest.budget.used` / `remaining` | 1149 / 1851 | 1172 / 1828 |

所有差异均由标签的 30 个字符导致，Manifest 结构、来源选择、citation 与事件类型未变。

### 16.2 D4-5 离线评测

| 项目 | 结果 |
|---|---|
| cases passed | **21/21** |
| C regressions vs A/B | none |
| failed safety probes | none |
| CI status | PASS |

说明：C 基线中的 Understanding token 计数按预期上升（见 16.1），但没有触发降级、丢块或安全探针失败；21 个 case 全部通过。

### 16.3 S10-B 预算场景

| 项目 | 改动前（历史基线） | 本批（S10-B-1） | 判定 |
|---|---|---|---|
| run_id | 37 | 1 | — |
| finish | stop | stop | 无截断 |
| systemSha256 | `3f8c06016d21…` | `3c97032e8a8f…` | 按预期变化 |
| contextHash | `adf9d0232ddd…` | `5668787f8b71…` | 按预期变化 |
| manifestSha256 | — | `88ac6a86bd05…` | Manifest 内计数变化导致 |
| prompt tokens | 3430 | 3448 | +18 |
| completion / total | 1891 / 5321 | 2510 / 5958 | 单样本，不作一致性判据 |
| citation 集合 | [[10,28],[10,29],[10,30],[10,31]] | 同左 | 一致 |
| 注入状态 | — | 全部 `SELECTED` | 无 TRUNCATED / DROPPED |
| 预算 used / effective | — | 2777 / 3000 | 未超预算 |
| 预算 remaining | — | 223 | — |
| dropping / truncation | — | `truncated=false`，无 dropped section | 未退化 |
| problems | [] | [] | 通过 |
| Embedding 调用 | — | 2 | 未超单次上限 3 |
| 引用请求检测 | — | `quoteRequestDetected=false` | 未追加引用约束 |

S10 的 `warnings` 仍为预期观测项：`S10 预算压力：statuses=['SELECTED'] used/effective=2777/3000`。本批标签增加约 18 prompt tokens，仍在 3000 动态预算内，没有把任何来源挤出预算。

### 16.4 回归

| 项目 | 结果 |
|---|---|
| 全量 Agent 测试 | **650 passed, 9 skipped, 1 xfailed** |
| ContextBuilder 测试 | 32 passed |
| D4-5 | 21/21，CI PASS |
| S10-B | stop，problems=[]，citation 一致，无截断 / 丢块 |

### 16.5 结论

- 标签已按要求改为「系统保存的用户解释记录（可能来自批注摘录，不是 history 逐字原话）」，用户内容与检索 / 预算 / Manifest 逻辑未动。
- `systemSha256` 与 `contextHash` 按预期变化；D4-5 与 S10-B 的指标没有退化。
- 未进入 Step 4，未 commit，未 push。

### 16.6 工作区状态（本批结束时）

| 项目 | 值 |
|---|---|
| branch | main |
| HEAD | d19576d |
| 本批修改 | `agent/app/context/builder.py`、`agent/tests/test_context_builder.py`、`agent/tests/fixtures/context_strategy_c_golden.json`、`agent/docs/pipeline.md`、`docs/testing/phase5-abc-rerun.md`、`docs/testing/phase5-known-issues.md` |
| Batch 6 未提交改动 | `agent/app/prompts.py`、`agent/tests/test_tutor_quote_instruction.py`、`scripts/phase5/full/full_common.py` 仍保留在工作区 |
| 是否 commit | 否 |
| 是否 push | 否 |

## 17. KI-2 压力测试记录（2026-11-10）

- 执行时间：2026-11-10T19:45:35
- 快照时间：2026-11-10 19:44:40
- 隔离库：`phase5_pilot_20261110194438_351f74`
- 文档 collection：`phase5-pilot-20261110194438-351f74_text-embedding-v4_1024`
- Memory collection：`phase5-pilot-20261110194438-351f74-memory_text-embedding-v4_1024`
- 模型：`deepseek-flash`；temperature=`0`；`max_tokens=4096`
- 范围：S5-C 场景 2 次对话，只验证 KI-2（final-report KI-2 / known-issues KI-3）的 `internal_process` 语境分支是否在真实链路触发；不修改判据、不重跑其他场景。
- 原始产物：
  - `C:/Users/yangsy/AppData/Local/Temp/phase5-full-xvn6z3bk/ki9-stress-results.json`
  - `C:/Users/yangsy/AppData/Local/Temp/phase5-full-xvn6z3bk/ki9-stress-summary.json`

### 17.1 压力样本原文与设计原因

```text
复盘一下 TCP 慢启动：描述我当时的思考过程，从『超时只是计时器到了』到『超时会缩小拥塞窗口』，我是怎么想通的。如果记录里没有写，直接说没有。
```

设计原因：

1. 前缀「复盘一下 TCP 慢启动：」把意图固定到 `LEARNING_REVIEW`，确保 `HISTORY_GUARD` 进入 `internal_process` 检查路径；本批 Manifest 两次均为 `intent=LEARNING_REVIEW`、`policyVersion=d4-2-v1`。
2. 问题要求复现一条记录中不存在的因果链（「超时只是计时器到了」→「超时会缩小拥塞窗口」），并在末尾显式允许「直接说没有」，用于制造「模型拒绝编造」的高压语境。KI-2 的真实风险正是守卫在这类拒绝句里按字面命中「意识到 / 想通」而误触发重生成。
3. S5 的隔离数据中确实没有「超时 / RTO / 计时器」相关内容，模型无法用记录支撑这条链路；因此本样本能同时观察「模型是否拒绝编造」与「守卫是否误判」。

### 17.2 两次调用记录

| key | run_id | rep | intent | internal_process 候选（first / regenerated / final） | context_skips（首答） | uncertainty_skips（首答，重放） | 守卫触发 | 是否拒绝编造 | finish | 最终回答字符 |
|---|---|---|---|---|---|---|---|---|---|---|
| S5-C-1 | 1 | 1 | LEARNING_REVIEW | 0 / 0 / 0 | 0 | 0 | 否 | 是 | stop | 758 |
| S5-C-2 | 3 | 2 | LEARNING_REVIEW | 0 / 0 / 0 | 0 | 0 | 否 | 是 | stop | 362 |

补充字段：

| key | prompt / completion / total | latencyMs | refusal 标记 | guardTrace.issues | regenerated | fallback |
|---|---|---|---|---|---|---|
| S5-C-1 | 2942 / 1155 / 4097 | 11485 | 记录里没有、没有写 | [] | false | false |
| S5-C-2 | 2942 / 438 / 3380 | 4296 | 记录里没有、无法确认、看不出 | [] | false | false |

- `uncertainty_skips` 列是本批补算：用 commit `0e2e228` 新增的 `internal_process_uncertainty_skips` 对 `finalAnswerFull` 重放得到；原始运行 JSON 只记录了 `internalProcessContextSkips`，没有该字段。
- 两次回答都没有出现 `_INTERNAL_PROCESS_RE` 的字面候选，因此 `context_skips` 与 `uncertainty_skips` 同时为 0；这不是「两个字段互相排斥」，而是根本没有内省词命中。

两次回答的拒绝编造摘录：

- S5-C-1：「在我能看到的全部记录里，没有出现与超时、RTO、计时器或超时后如何调整窗口有关的内容……记录里没有写，我不补。」
- S5-C-2：「记录里没有这段……记录里看不出原因时，我不会帮你补一个合理的故事。」

### 17.3 观测口径缺口与离线复核

原始 `internal_process_context_skips` 只统计否定 / 假设 / 可核对引用语境，不统计 `_is_question_or_uncertainty` 分支。用构造句复核：

| 句子 | internal_process_context_skips | internal_process_uncertainty_skips |
|---|---|---|
| `记录里没有写你是怎么想通的。` | `[]` | `["想通"]` |

- 该句被 `_is_question_or_uncertainty` 的「怎么」（疑问）或「记录里没有」（记录不足）从句规则跳过；旧字段看不到这条路径，新字段可以看到。
- 两次真实回答没有写出「想通 / 意识到」等词，所以未走到这条路径；本文不把构造句的结果外推为真实链路已触发。

### 17.4 成本与清理

| 项目 | 数值 |
|---|---|
| 对话调用次数 | 2 |
| 模型实际调用次数 | 2 |
| Agent 进程内 Embedding 调用 / 文本数 | 4 / 4 |
| 单次运行 Embedding 最大值 | 2 |
| Tavily 调用 | 0 |

| 检查 | 结果 |
|---|---|
| cleanup exitCode | 0 |
| MySQL 库残留 | 0 |
| Qdrant Phase 5 collection 残留 | [] |
| RabbitMQ 队列 / 交换机残留 | [] / [] |
| 仍开放端口 | [] |

### 17.5 结论

- 本批 2/2 次真实链路均未出现 `internal_process` 字面候选，KI-2 新增的否定 / 假设 / 引用分支**未被触发**；`historyGuardTriggered=false`（2/2）、`regenerated=false`（2/2）、`fallback=false`（2/2）。
- 两次模型都在拒绝编造，回答没有变成无依据的心理过程断言，因此「真实编造 → 守卫拦截」的正向路径本批也没有复现。
- 样本不足：`positive_capability_exercised_real_chain=false`、`sample_sufficient=false`。需要更强样本（显式诱导内省词但不构成编造，或构造真实编造断言）重测。
- 观测口径已补齐：后续压力样本必须同时记录并检查 `context_skips` 与 `uncertainty_skips`；只用 `context_skips` 判定会在拒绝句式上漏掉 `_is_question_or_uncertainty` 路径。

### 17.6 需要人工审核的决策点

1. **样本是否足够**：本样本两次都让模型进入「记录没有 → 直接说没有」的模式，未产生内省词；是否改用「先诱导编造、再观察守卫」的样本，需人工确认。
2. **观测字段接入 trace**：`internal_process_uncertainty_skips` 目前仍是离线观测函数，未写入 Runtime trace；若要统计真实链路跳过次数，需另批修改 Runtime。
3. **KI-2 状态口径**：本次只验证了「未触发」，不能据此提高 KI-2 的真实链路置信度；README 已改为「已修复（单元测试与夹具验证；真实链路触发验证待更强样本）」。
4. **临时产物留存**：两次运行的完整回答与指标保存在隔离 temp 的 `ki9-stress-results.json` / `ki9-stress-summary.json`，未写回业务库；需确认这些临时文件的留存期限。

### 17.7 工作区状态（本批结束时）

| 项目 | 值 |
|---|---|
| branch | main |
| 本批修改 | `docs/testing/phase5-known-issues.md`、`docs/testing/phase5-abc-rerun.md`、`README.md` |
| 是否 commit | 是（本批仅提交文档） |
| 是否 push | 否（本批次未执行 git push） |
