# Phase 5 Step 5：A/B/C 对照、盲审准备与 L1/L2/L3 分层判定

- 执行时间：2026-11-10T01:26:41
- 测试戳：`20261110010931-be50e1`
- 执行脚本：`scripts/phase5/full/run_step5.py`
- 隔离库：`phase5_pilot_20261110010931_be50e1`
- 文档 collection：`phase5-pilot-20261110010931-be50e1_text-embedding-v4_1024`；Memory collection：`phase5-pilot-20261110010931-be50e1-memory_text-embedding-v4_1024`
- 生成参数：model=`deepseek-flash`，max_tokens=`4096`，temperature 在子进程强制为 0
- Tavily：0 次调用

> 本批结论：**BLOCKED**（人工盲审未完成：未安排至少 2 名评审）。30 次调用与盲审材料已执行；L2/L3 不伪造评分。

## 1. 前置条件确认

| 检查项 | 结果 | 说明 |
|---|---|---|
| springJar | PASS | backend-0.0.1-SNAPSHOT.jar |
| internalToken | PASS | set |
| llmMaxTokens>=4096 | PASS | 4096 |
| llmModel | PASS | deepseek-flash |
| embeddingModel | PASS | text-embedding-v4 |
| agentDelegationSecretAbsent | PASS | agent .env 不持有 delegation secret |
| mysql | PASS | 127.0.0.1:3306 |
| qdrant | PASS | 200 |
| rabbitmq | PASS | 200 |
| portsFree | PASS | 8001/8081 |
| 人工盲审（≥2 名评审） | BLOCKED | 本执行环境未收到评审人安排、评分表回收或评审签名；按任务前置条件标记 BLOCKED |

- 隔离环境、内部凭据、分析消费者关闭、`max_tokens >= 4096` 均沿用 Step 3/Step 4 已通过的 setup preflight。
- 人工盲审：**未安排**。本批生成材料但不评分；L2/L3 的 PASS/FAIL 必须等至少 2 名评审独立完成后才能给出。

## 2. 30 次调用汇总（场景 × 策略 + L2 观测字段）

每个场景使用同一问题、同一数据快照、同一 Knowledge 文档；A/B/C 顺序按场景索引轮转（S1=A→B→C，S2=B→C→A，S3=C→A→B，依次循环），因此三个策略在整批中交错出现。

| 场景 | 策略 | run_id | finish | HISTORY_GUARD | Memory 降级 | prompt/completion/total | 注入个人来源 | 个人上下文被使用 | 引用来源 | Understanding 误称原话 | 忽略注入（字面代理） | 字数 | 调用健康 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S1 | A | 1 | stop | 否 | 否 | 1904/1510/3414 | - | 否 | knowledge | 否 | 否 | 1078 | OK |
| S1 | B | 3 | stop | 否 | 否 | 2852/1770/4622 | evidence,history,memory,session,trajectory,understanding | 否 | knowledge | 否 | 是 | 1077 | OK |
| S1 | C | 5 | stop | 否 | 否 | 2650/1258/3908 | evidence,history,memory,session,understanding | 是 | history,knowledge | 否 | 否 | 1561 | OK |
| S2 | B | 7 | stop | 否 | 否 | 2863/1348/4211 | evidence,history,memory,session,trajectory,understanding | 否 | knowledge | 否 | 是 | 1230 | OK |
| S2 | C | 9 | stop | 否 | 否 | 2662/1276/3938 | evidence,history,memory,session,understanding | 否 | knowledge | 否 | 是 | 1477 | OK |
| S2 | A | 11 | stop | 否 | 否 | 1901/1322/3223 | - | 否 | knowledge | 否 | 否 | 1201 | OK |
| S3 | C | 13 | stop | 否 | 否 | 2502/958/3460 | history,memory,session,understanding | 否 | knowledge | 否 | 是 | 1044 | OK |
| S3 | A | 15 | stop | 否 | 否 | 1844/1054/2898 | - | 否 | knowledge | 否 | 否 | 1026 | OK |
| S3 | B | 17 | stop | 否 | 否 | 2775/1253/4028 | evidence,history,memory,session,trajectory,understanding | 是 | memory,knowledge | 否 | 否 | 1195 | OK |
| S4 | A | 19 | stop | 否 | 否 | 1892/812/2704 | - | 否 | knowledge | 否 | 否 | 864 | OK |
| S4 | B | 21 | stop | 否 | 否 | 2880/1851/4731 | evidence,history,memory,session,trajectory,understanding | 是 | history,knowledge | 否 | 否 | 1044 | OK |
| S4 | C | 23 | stop | 否 | 否 | 2579/1382/3961 | history,memory,session,understanding | 是 | history,knowledge | 否 | 否 | 754 | OK |
| S5 | B | 25 | stop | 否 | 否 | 3096/1507/4603 | evidence,history,memory,session,trajectory,understanding | 是 | history,understanding,memory,evidence,knowledge | 否 | 否 | 1001 | OK |
| S5 | C | 27 | stop | 否 | 否 | 3096/1268/4364 | evidence,history,memory,session,trajectory,understanding | 是 | history,understanding,memory,knowledge | 否 | 否 | 921 | OK |
| S5 | A | 29 | stop | 否 | 否 | 2024/495/2519 | - | 否 | knowledge | 否 | 否 | 409 | OK |
| S6 | C | 31 | stop | 否 | 否 | 2570/742/3312 | evidence,history,memory,session,understanding | 否 | knowledge | 否 | 是 | 730 | OK |
| S6 | A | 33 | stop | 否 | 否 | 1822/977/2799 | - | 否 | knowledge | 否 | 否 | 661 | OK |
| S6 | B | 35 | stop | 否 | 否 | 2770/1023/3793 | evidence,history,memory,session,trajectory,understanding | 否 | knowledge | 否 | 是 | 639 | OK |
| S7 | A | 37 | stop | 否 | 否 | 1828/1329/3157 | - | 否 | knowledge | 否 | 否 | 1445 | OK |
| S7 | B | 39 | stop | 否 | 否 | 1963/1258/3221 | history,session | 否 | knowledge | 否 | 是 | 1104 | OK |
| S7 | C | 41 | stop | 否 | 否 | 1907/1203/3110 | history | 否 | knowledge | 否 | 是 | 1345 | OK |
| S8 | B | 43 | stop | 否 | 否 | 2851/1184/4035 | evidence,history,memory,session,trajectory,understanding | 是 | history,knowledge | 否 | 否 | 882 | OK |
| S8 | C | 45 | stop | 否 | 否 | 2548/898/3446 | history,memory,session,understanding | 否 | knowledge | 否 | 是 | 908 | OK |
| S8 | A | 47 | stop | 否 | 否 | 1858/793/2651 | - | 否 | knowledge | 否 | 否 | 999 | OK |
| S9 | C | 49 | stop | 否 | 否 | 2235/964/3199 | history,memory,session | 是 | history,knowledge | 否 | 否 | 1066 | OK |
| S9 | A | 51 | stop | 否 | 否 | 1819/1031/2850 | - | 否 | knowledge | 否 | 否 | 802 | OK |
| S9 | B | 53 | stop | 否 | 否 | 2158/862/3020 | evidence,history,memory,session | 是 | history,knowledge | 否 | 否 | 1214 | OK |
| S10 | A | 55 | stop | 否 | 否 | 1918/1534/3452 | - | 否 | knowledge | 否 | 否 | 1510 | OK |
| S10 | B | 57 | stop | 否 | 否 | 3448/3126/6574 | evidence,history,memory,session,trajectory,understanding | 否 | knowledge | 否 | 是 | 1370 | OK |
| S10 | C | 59 | stop | 否 | 否 | 3248/1735/4983 | evidence,history,memory,session,understanding | 否 | knowledge | 否 | 是 | 1331 | OK |

- 「个人上下文被使用」与「引用来源」是**字面观测**：只检查回答是否出现预置数据中的可区分片段，不做语义推断。
- 「忽略注入（字面代理）」只表示「注入了个人来源但回答没有出现任何可区分个人片段」，**不等于**人工盲审认定的「忽略」；最终以盲审为准。

### 2.1 调用健康与成本

| 项目 | 数值/结果 |
|---|---|
| 调用记录 | 30 / 30 |
| LLM 调用（含守卫重生成） | 30 |
| LLM 预算 | ≤ 30 |
| Embedding 调用（Agent 进程内） | 50 |
| Embedding 文本数 | 50 |
| Embedding 预算 | ≤ 60 |
| Tavily | 0 |
| error / timeout / 空输出 / 截断 | 无 |

## 3. 盲审材料说明

- 材料文件：`docs/testing/phase5-blind-review.md`
- 30 条回答全局随机编号 R01–R30，随机化摘要（映射校验值）：`d45cd3c9fe8494c3982597f81d0b72866d4434b1bfce8dd03d58230ec3299d68`
- 每条附：问题、回答全文、Citation（如有）、Manifest sourceIds（仅评审用）。不附策略名、intent、policy_version 或上下文版本指纹。
- 策略与编号的映射保存在隔离临时目录 `step5-blind-mapping.json`；不写入仓库、不随盲审材料分发。
- L3 对照：同一问题的 3 个匿名版本用「甲/乙/丙」表示，组内顺序随机；甲/乙/丙与上下文的对应关系同样只在隔离映射表中。
- 策略标识泄漏扫描：**PASS**；命中 token=[]；命中 run key=[]。
- 评审表维度：L2（是否使用个人上下文、是否误称来源、是否忽略注入内容、引用了哪些来源、证据）；L3（准确性 1–5、与用户历史贴合度 1–5、组内排序）。

## 4. L1 / L2 / L3 矩阵

### 4.1 L1

L1 沿用 Step 4 结果，本批不重复判定。

| 层级 | 范围 | 状态 | 证据 |
|---|---|---|---|
| L1 | 真实 MySQL + Qdrant + Embedding + LLM 全链路、隔离、冻结、清理 | PASS（沿用 Step 4） | `docs/testing/phase5-step4-results.md` §7.1–§7.2；本批 30 次调用未出现 error/timeout/空输出/截断，链路健康与 Step 4 一致 |

### 4.2 L2

L2 的 PASS/FAIL 必须由人工盲审给出。本批只提供逐场景机器观测与盲审入口；由于未完成人工盲审，10 个场景一律标记 **BLOCKED**。

| 场景 | 注入过的个人来源 | 字面使用个人上下文的策略 | Understanding 误称原话的策略 | 忽略注入（字面代理）的策略 | L2 状态 |
|---|---|---|---|---|---|
| S1 | evidence,history,memory,session,trajectory,understanding | C | - | B | BLOCKED（待≥2 名评审） |
| S2 | evidence,history,memory,session,trajectory,understanding | - | - | B,C | BLOCKED（待≥2 名评审） |
| S3 | evidence,history,memory,session,trajectory,understanding | B | - | C | BLOCKED（待≥2 名评审） |
| S4 | evidence,history,memory,session,trajectory,understanding | B,C | - | - | BLOCKED（待≥2 名评审） |
| S5 | evidence,history,memory,session,trajectory,understanding | B,C | - | - | BLOCKED（待≥2 名评审） |
| S6 | evidence,history,memory,session,trajectory,understanding | - | - | B,C | BLOCKED（待≥2 名评审） |
| S7 | history,session | - | - | B,C | BLOCKED（待≥2 名评审） |
| S8 | evidence,history,memory,session,trajectory,understanding | B | - | C | BLOCKED（待≥2 名评审） |
| S9 | evidence,history,memory,session | B,C | - | - | BLOCKED（待≥2 名评审） |
| S10 | evidence,history,memory,session,trajectory,understanding | - | - | B,C | BLOCKED（待≥2 名评审） |

- 「字面使用」只说明回答出现了预置数据的可区分片段；没有出现不等于语义上没有使用。L2 的最终判定必须由评审阅读回答全文后给出。

### 4.3 L3

| 层级 | 判定内容 | 状态 | 证据 |
|---|---|---|---|
| L3 | 个性化是否带来收益：对比 A/B/C 的人工准确性/贴合度评分 | BLOCKED | 未完成人工盲审，无 A/B/C 人工评分；不得用机器字面观测替代 |

## 5. L2 失败模式归纳（候选，未经盲审确认）

| 场景 | 策略 | 候选模式 | 证据 |
|---|---|---|---|
| S1 | B | 注入个人来源但回答未出现可区分个人片段 | {"injected": ["evidence", "history", "memory", "session", "trajectory", "understanding"], "answerChars": 1077} |
| S2 | B | 注入个人来源但回答未出现可区分个人片段 | {"injected": ["evidence", "history", "memory", "session", "trajectory", "understanding"], "answerChars": 1230} |
| S2 | C | 注入个人来源但回答未出现可区分个人片段 | {"injected": ["evidence", "history", "memory", "session", "understanding"], "answerChars": 1477} |
| S3 | C | 注入个人来源但回答未出现可区分个人片段 | {"injected": ["history", "memory", "session", "understanding"], "answerChars": 1044} |
| S6 | B | 注入个人来源但回答未出现可区分个人片段 | {"injected": ["evidence", "history", "memory", "session", "trajectory", "understanding"], "answerChars": 639} |
| S6 | C | 注入个人来源但回答未出现可区分个人片段 | {"injected": ["evidence", "history", "memory", "session", "understanding"], "answerChars": 730} |
| S7 | B | 注入个人来源但回答未出现可区分个人片段 | {"injected": ["history", "session"], "answerChars": 1104} |
| S7 | C | 注入个人来源但回答未出现可区分个人片段 | {"injected": ["history"], "answerChars": 1345} |
| S8 | C | 注入个人来源但回答未出现可区分个人片段 | {"injected": ["history", "memory", "session", "understanding"], "answerChars": 908} |
| S10 | B | 注入个人来源但回答未出现可区分个人片段 | {"injected": ["evidence", "history", "memory", "session", "trajectory", "understanding"], "answerChars": 1370} |
| S10 | C | 注入个人来源但回答未出现可区分个人片段 | {"injected": ["evidence", "history", "memory", "session", "understanding"], "answerChars": 1331} |

- 本批「注入个人来源但未出现可区分个人片段」出现在 7/10 场景（11 条 B/C 记录），跨场景重复；但每个「场景 × 策略」仅 1 次，且字面片段可能漏检语义使用，仍需盲审确认。
- 这些只是**候选失败模式**，用于提示评审重点关注，不构成 L2 FAIL。
- 稳定性口径：本批每个「场景 × 策略」仅 1 次调用，无法判断同一格内的重复稳定性；跨场景重复只能说明该现象不局限于单题，仍需盲审与 repeat 验证。
- S5 复核：B、C 逐字引用了历史原句「我原来以为慢启动慢是因为它每次只把窗口加一」；B/C 未包含 Step 3 的合成标记 `【S5-HISTORY】`，但按 Step 3 修正后的「原句内容或三关键词」判据不构成失败。A 明确声明无法引用，符合无个人上下文的预期。
- 已知的预期差异：S7 的 C 按 Policy 不注入旧 Session/Understanding/Memory，因此 C 未使用个人上下文是**设计预期**，不是失败；B 在该场景仍注入旧 Session，是 B 近似策略的已知缺陷。

## 6. L3 对照结果

**BLOCKED**。本批没有 A/B/C 的人工评分，因此不能判定个性化是否带来收益。为避免伪造结论，本节不提供替代评分；只记录机器可复现的非质量指标：

| 场景 | A（字数/引用） | B（字数/引用） | C（字数/引用） |
|---|---|---|---|
| S1 | 1078 字 / 3 引用 | 1077 字 / 3 引用 | 1561 字 / 3 引用 |
| S2 | 1201 字 / 3 引用 | 1230 字 / 3 引用 | 1477 字 / 3 引用 |
| S3 | 1026 字 / 3 引用 | 1195 字 / 3 引用 | 1044 字 / 3 引用 |
| S4 | 864 字 / 3 引用 | 1044 字 / 3 引用 | 754 字 / 3 引用 |
| S5 | 409 字 / 1 引用 | 1001 字 / 3 引用 | 921 字 / 3 引用 |
| S6 | 661 字 / 3 引用 | 639 字 / 3 引用 | 730 字 / 0 引用 |
| S7 | 1445 字 / 3 引用 | 1104 字 / 3 引用 | 1345 字 / 3 引用 |
| S8 | 999 字 / 3 引用 | 882 字 / 3 引用 | 908 字 / 3 引用 |
| S9 | 802 字 / 3 引用 | 1214 字 / 3 引用 | 1066 字 / 3 引用 |
| S10 | 1510 字 / 4 引用 | 1370 字 / 4 引用 | 1331 字 / 4 引用 |

- 字数与引用数**不是** L3 质量评分，不能用于判断「更准确」或「更贴合历史」。

## 7. 最小干预建议（本批不实施）

以下建议只在人工盲审确认失败模式后才考虑实施；本批未修改任何 Prompt / ContextBuilder / Policy / Schema。

| 触发条件 | 最小干预建议 |
|---|---|
| 若盲审确认 B/C 在多场景「注入了个人来源但回答完全不用」 | 优先在 Prompt 层增加「回答需显式结合用户已有理解/记忆」的约束；不要先改 ContextBuilder（Manifest 已证明来源注入成功，Step 4 D2 同因）。 |
| 若盲审确认「Understanding 被误称为原话」 | 在 Prompt 中要求区分「系统记录的你当前理解」与「你的原话」；Step 4 已记录同类缺口，仍应先改 Prompt，不改 Schema。 |
| 若盲审确认 S5 复盘场景未引用历史原句 | 检查 Prompt 对「引用原话」的约束是否足够强；Step 3 已把判据从「逐字包含合成标记」修正为「原句内容或三关键词」，本批沿用该边界。 |
| 若盲审确认 B 在话题切换场景被无关旧 Session 干扰 | 这是 B「近似旧策略」的已知设计差异；是否需要修 B 取决于 B 的定位（对照组还是可上线策略），不应在本批直接改 Policy。 |
| 若盲审确认 S9 DEGRADED 场景回答质量下降 | 保留 C 的 DEGRADED 显式状态，不把降级伪装成 EMPTY；若需要改善回答，应先确认降级提示是否足够清晰，再考虑 Prompt 层说明。 |
| 若两名评审的一致性低（例如同一场景结论相反） | 先校准评分表与证据要求，再增加评审人数；不要用第三人评分强行凑 PASS。 |

## 8. Step 5 结论

### 8.1 失败判据逐条检查

| 失败判据 | 结果 | 证据 |
|---|---|---|
| 30 次调用有 error / timeout / 空输出 / 截断 | PASS（未命中） | 见 §2.1；失败项：无 |
| 盲审材料暴露策略标识 | PASS（未命中） | 泄漏扫描：{"ok": true, "forbiddenTokenHits": [], "runKeyPatternHits": []} |
| L2 判定缺场景或无法归因 | PASS（10/10 场景均有记录与机器证据） | 见 §4.2；每个场景均有记录与机器证据，L2 状态统一为 BLOCKED，未缺场景 |
| 人工盲审未完成却标记 PASS | PASS（未发生） | L2/L3 均标记 BLOCKED，未给出任何 PASS 评分 |

### 8.2 结论

**Step 5：BLOCKED（人工盲审未完成：未安排至少 2 名评审）。**

- 已执行：30 次真实调用、逐场景 L2 机器观测、随机化盲审材料、L1/L2/L3 矩阵、候选失败模式与最小干预建议。
- 未完成：至少 2 名评审的独立 L2 判定与 L3 评分回收。
- 因此 Phase 5 关闭条件**未满足**：不能在盲审完成前声称 L2/L3 PASS，也不能用机器字面观测替代人工质量判断。
- 下一步：按 `docs/testing/phase5-blind-review.md` 组织 2 名评审；回收后由执行者用隔离映射表解盲，再补写 L2/L3 矩阵与结论。

## 9. 工作区状态

| 项目 | 值 |
|---|---|
| branch | main |
| HEAD | 45d5d2ca20aab50bb4b3edf375e66417b24036f7 |
| HEAD subject | test: Step 4 真实全链路测试 PASS，带个人来源 Citation 缺口 |
| 工作区状态 | ?? docs/testing/phase5-blind-review.md ?? docs/testing/phase5-step5-results.md ?? scripts/phase5/full/run_step5.py |
| 是否 commit | 否（本批次未执行 git commit） |
| 是否 push | 否（本批次未执行 git push） |

- 新增允许文件：`scripts/phase5/full/run_step5.py`、`docs/testing/phase5-step5-results.md`、`docs/testing/phase5-blind-review.md`。
- 未修改：Prompt、ContextBuilder、Policy、Schema、Runtime、HISTORY_GUARD、Tavily。
- 清理：cleanup exitCode=0，独立残留检查=PASS；MySQL 残留=0，Qdrant 残留=[]，RabbitMQ 队列/交换机残留=[]/[]，端口残留=[]。

## 10. 待审核决策点

1. **评审人安排**：请提供至少 2 名评审的姓名/排期；在评分回收前 L2/L3 保持 BLOCKED。
2. **L2 判定口径**：是否接受「字面片段命中」作为评审辅助，但最终以评审阅读全文为准？
3. **L3 收益阈值**：A/B/C 的人工评分差达到多少才算「个性化带来收益」？需要预注册。
4. **单次调用 versus 稳定性**：本批每个「场景 × 策略」只有 1 次，无法判断失败模式是否稳定；是否需要在盲审前补 repeat？
5. **S7 的 B 近似缺陷**：B 在话题切换场景注入旧 Session 是已知设计差异；是否仍把 B 当作可上线策略？
6. **S5 原话判据**：本批沿用 Step 3 修正后的「原句内容或三关键词」，不再要求逐字复制 `【S5-HISTORY】`；请确认。
7. **Step 4 D1/D2**：个人来源 Citation 继续按 Manifest 追溯（D1），回答个性化按本批盲审验收（D2）；请确认。
8. **盲审完成后的动作**：若 L2 FAIL 稳定出现，是否批准下一批只改 Prompt（不改 ContextBuilder/Policy/Schema）？

