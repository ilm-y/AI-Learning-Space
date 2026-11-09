# Phase 5 Step 3：10 场景 × 3 策略 × 3 次全量 A/B/C 对照结果

- 生成时间：2026-11-09T22:02:10
- 快照时间：2026-11-09 21:32:13
- 隔离库：`phase5_pilot_20261109213211_7e3790`
- 文档 collection：`phase5-pilot-20261109213211-7e3790_text-embedding-v4_1024`
- Memory collection：`phase5-pilot-20261109213211-7e3790-memory_text-embedding-v4_1024`
- 生成参数指纹：`2e1405ed23bbe132b96b018c7e79d190321e43ff5ffb5c5108e99bee47edb5f6`
- 实际完成调用：90 / 90

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

- 3 次 C 策略真实验证：依据仓库既有记录 `docs/testing/phase5-c-verification*.md` 与 commit `87aa182`（本报告不重新执行）。
- A/B/C 开关、C golden 回归、统一快照脚本、Memory 隔离与内部凭据均按既有提交使用；本实验未改生产代码。
- LLM 生成参数：model=`deepseek-flash`，max_tokens=`4096`，temperature 在子进程强制为 0（见第 12 节决策点）。

## 2. 10 个场景数据说明

| ID | 场景 | Space | Concept | Session | Conversation | 文档 | Chunk | C intent |
|---|---|---|---|---|---|---|---|---|
| S1 | 普通知识问答 | 1 | 1 | 1 | 1 | 1 | 3 | LEARNING_EXPLANATION |
| S2 | 同一 Session 连续学习 | 2 | 2 | 2 | 2 | 2 | 3 | CONTINUE_TASK |
| S3 | 跨 Session 学习 | 3 | 3 | 3,4 | 3,4 | 3 | 3 | LEARNING_EXPLANATION |
| S4 | 跨 Conversation 同 Concept（TLB） | 4 | 4 | 5,6,7,8 | 5,6,7,8 | 4 | 3 | LEARNING_EXPLANATION |
| S5 | 历史错误回顾 | 5 | 5 | 9 | 9 | 5 | 3 | LEARNING_REVIEW |
| S6 | 当前掌握程度 | 6 | 6 | 10 | 10 | 6 | 3 | META_QUESTION |
| S7 | 话题切换 | 7 | 7,8 | 11 | 11 | 7 | 3 | LEARNING_EXPLANATION |
| S8 | 新旧知识冲突 | 8 | 9 | 12 | 12 | 8 | 3 | LEARNING_EXPLANATION |
| S9 | 检索异常（DEGRADED） | 9 | 10 | 13 | 13 | 9 | 3 | LEARNING_EXPLANATION |
| S10 | 上下文预算压力 | 10 | 11 | 14 | 14 | 10 | 4 | LEARNING_EXPLANATION |

- **S1 普通知识问答**：为什么数据一多，网络就会变慢甚至丢包？
  - 预期：A 清空 history/session 后问题本身不命中概念，退化为 KNOWLEDGE_QUERY，只注入 Knowledge；B/C 能从历史里恢复 TCP 概念，注入个人上下文。这是 A 与 B/C 在普通问答中的非干扰性差异。
- **S2 同一 Session 连续学习**：继续讲：B+ 树的范围查询为什么比哈希索引快？
  - 预期：「继续」+ 会话概念匹配触发 CONTINUE_TASK；C 注入 Session goal/focus。
- **S3 跨 Session 学习**：B+ 树的叶子节点链表有什么作用？
  - 预期：个人上下文来自上一 Session；当前 Session 只提供 goal/focus。
- **S4 跨 Conversation 同 Concept（TLB）**：TLB 是什么？请沿用我常用的那个比喻来说明。
  - 预期：TLB 案例：A 错误、B 纠正、C 切 Redis、D 重新问。当前问题不触发复盘词；个人上下文来自 B 会话的纠正结果。「小托盘」只存在于 U/M，A 无法看到，是可判定的历史使用标记。
- **S5 历史错误回顾**：回顾一下，我之前对 TCP 慢启动的理解错在哪？请引用我当时的原话。
  - 预期：复盘意图要求 C 的 Manifest 同时含 revision 与 evidence；回答必须逐字引用含 【S5-HISTORY】 的原话，作为历史使用硬证据。
- **S6 当前掌握程度**：我现在对虚拟内存掌握到什么程度？哪里还薄弱？
  - 预期：META_QUESTION：C 读取当前 Understanding + Evidence，不读 Revision。
- **S7 话题切换**：Raft 共识的领导者选举是怎么实现的？
  - 预期：当前问题命中 Raft，Session 文本只含 Redis，Policy 判定概念不匹配，C 不注入旧 Session；B 走 plan=None 仍注入旧 Session，是预期的近似缺陷。
- **S8 新旧知识冲突**：CAP 定理是不是必须三选二？
  - 预期：个人记忆中的旧口诀与文档新解释冲突；检查回答是否采用文档口径。
- **S9 检索异常（DEGRADED）**：进程调度有哪些常见算法？
  - 预期：Understanding 行使用非法 status。C 的 Manifest 应出现 cognitive_status 与 understanding-status:DEGRADED；B 把该状态折叠为 EMPTY，因此不应出现同一 source。此场景同时验证 B 的近似的已知缺陷。
- **S10 上下文预算压力**：文件系统的日志（journaling）为什么能保证一致性？
  - 预期：14 条长历史 + 长 Understanding/Memory，预期 Manifest 出现 TRUNCATED/OMITTED 或 budget.used 接近 effective；用于验证预算压力。

### 2.1 预置数据与预期注入（摘要）

| 场景 | 概念锚点 | Session（goal / focus） | 预置 Understanding / Memory / Evidence / Revision | Knowledge 文档 | 预期注入与硬判据 |
|---|---|---|---|---|---|
| S1 普通知识问答 | TCP 拥塞控制 | TCP 拥塞控制：从丢包到拥塞窗口（goal=理解 TCP 拥塞控制为什么需要慢启动和拥塞避免；focus=说不清拥塞窗口和丢包之间的关系） | U[PARTIAL] 【S1-U】用户知道拥塞是流量超过链路容量，但仍把原因主要归为带宽不足。<br>M: 【S1-M】用户偏好用带宽和队列长度理解 TCP 拥塞，容易忽略拥塞窗口的作用。<br>E[PARTIAL] 【S1-E】用户尚未把 TCP 发送行为与拥塞联系起来。<br>R×1: 【S1-R1】用户把拥塞主要归因于带宽不足。 | s1-tcp-congestion.md（3 chunk） | A: knowledge+user_input；不注入 understanding, memory, evidence, trajectory, session, history<br>B: knowledge, understanding, memory, history<br>C: intent=LEARNING_EXPLANATION，注入 knowledge, understanding, memory, evidence, session |
| S2 同一 Session 连续学习 | B+ 树 | B+ 树：叶子节点与范围查询（goal=掌握 B+ 树的叶子节点与范围查询；focus=不清楚叶子链表如何支持范围扫描） | U[PARTIAL] 【S2-U】用户理解 B+ 树数据在叶子、叶子有链表；还说不清范围查询的磁盘 IO 优势。<br>M: 【S2-M】用户喜欢用磁盘页和顺序 IO 解释 B+ 树，连续追问时希望接着上一轮的叶子链表讲。<br>E[PARTIAL] 【S2-E】用户能复述结构，但未解释范围查询的磁盘 IO 优势。<br>R×1: 【S2-R1】用户已掌握内部节点与叶子节点的分工。 | s2-bplus-range.md（3 chunk） | A: knowledge+user_input；不注入 understanding, memory, evidence, trajectory, session, history<br>B: knowledge, understanding, memory, history<br>C: intent=CONTINUE_TASK，注入 knowledge, understanding, memory, session |
| S3 跨 Session 学习 | B+ 树 | B+ 树基础：分裂与合并（goal=理解 B+ 树的分裂与合并；focus=分裂时中间元素如何上移）；B+ 树范围查询：叶子链表（goal=理解叶子链表与范围扫描；focus=叶… | U[PARTIAL] 【S3-U】用户在上一阶段理解了分裂与合并，也记住了叶子链表；但还没把链表和范围扫描联系起来。<br>M: 【S3-M】用户习惯把 B+ 树和磁盘页大小一起考虑；跨阶段学习时希望先回忆上一阶段的结论。<br>E[PARTIAL] 【S3-E】上一会话记录：用户能解释分裂，未解释范围扫描。<br>R×1: 【S3-R1】用户在上一阶段掌握了分裂与合并。 | s3-bplus-leaf.md（3 chunk） | A: knowledge+user_input；不注入 understanding, memory, evidence, trajectory, session, history<br>B: knowledge, understanding, memory, history<br>C: intent=LEARNING_EXPLANATION，注入 knowledge, understanding, memory, session |
| S4 跨 Conversation 同 Concept（TLB） | TLB | TLB 初学：A 会话（goal=理解 TLB 与页表的关系；focus=TLB 到底缓存什么）；TLB 纠错：B 会话（goal=纠正 TLB 缓存对象的错误理解；focus=… | U[PARTIAL] 【S4-U】用户已纠正：TLB 缓存最近用到的页表项；常用比喻是“鞋柜门口的小托盘”，只放最近穿的鞋。<br>M: 【S4-M】用户用“鞋柜门口的小托盘”比喻 TLB，偏好用比喻理解缓存；容易把 TLB 和数据缓存混淆。<br>E[PARTIAL] 【S4-E】用户在 B 会话完成了对 A 会话错误理解的纠正。<br>R×2: 【S4-R1】用户认为 TLB 是 CPU 的数据缓存。；【S4-R2】用户纠正为 T… | s4-tlb.md（3 chunk） | A: knowledge+user_input；不注入 understanding, memory, evidence, trajectory, session, history<br>B: knowledge, understanding, memory, history<br>C: intent=LEARNING_EXPLANATION，注入 knowledge, understanding, memory, session<br>答案标记：小托盘 |
| S5 历史错误回顾 | TCP 慢启动 | TCP 慢启动：错在哪（goal=理解 TCP 慢启动与拥塞避免；focus=慢启动的指数增长何时停止） | U[PARTIAL] 【S5-U】用户原先误以为慢启动每次只加一；现已理解慢启动从 cwnd=1 开始指数增长。<br>M: 【S5-M】用户曾把 TCP 慢启动理解为线性加一，复盘时希望看到当时的原话和纠正过程。<br>E[PARTIAL] 【S5-E】用户把“慢启动”误解为线性增长，已纠正为指数增长。<br>R×2: 【S5-R1】用户认为慢启动每次只把窗口加一。；【S5-R2】用户已纠正：慢启动从 1 开始指数增长。 | s5-slow-start.md（3 chunk） | A: knowledge+user_input；不注入 understanding, memory, evidence, trajectory, session, history<br>B: knowledge, understanding, memory, trajectory<br>C: intent=LEARNING_REVIEW，注入 knowledge, understanding, memory, trajectory, evidence<br>逐字引用：【S5-HISTORY】 |
| S6 当前掌握程度 | 虚拟内存 | 虚拟内存：地址转换与 TLB（goal=理解虚拟内存与页表；focus=TLB 与页表的分工） | U[PARTIAL] 【S6-U】能解释虚拟地址到物理地址的转换，但说不清 TLB miss 后页表遍历的代价。<br>M: 【S6-M】用户偏好先画地址转换流程图再记细节；在虚拟内存上已经会画流程，但对 TLB miss 代价还不确定。<br>E[PARTIAL] 【S6-E】能讲清地址转换流程，未量化 TLB miss 代价。<br>R×1: 【S6-R1】用户掌握了地址转换流程。 | s6-virtual-memory.md（3 chunk） | A: knowledge+user_input；不注入 understanding, memory, evidence, trajectory, session, history<br>B: knowledge, understanding, memory<br>C: intent=META_QUESTION，注入 knowledge, understanding, memory, evidence, session；不注入 trajectory |
| S7 话题切换 | Redis 持久化、Raft 共识 | Redis 持久化：AOF 重写（goal=理解 Redis 持久化与 AOF；focus=AOF 重写会不会阻塞主线程） | U[PARTIAL] 【S7-U-REDIS】用户理解了 AOF 重写由子进程完成。<br>M: 【S7-M】用户喜欢把 Redis 持久化和主线程阻塞一起考虑。<br>E[PARTIAL] 【S7-E】用户已理解 AOF 重写的基本并发模型。<br>R×1: 【S7-R1】用户理解了 AOF 重写由子进程完成。 | s7-raft-election.md（3 chunk） | A: knowledge+user_input；不注入 understanding, memory, evidence, trajectory, session, history<br>B: knowledge, session, history<br>C: intent=LEARNING_EXPLANATION，注入 knowledge；不注入 session, understanding, memory |
| S8 新旧知识冲突 | CAP 定理 | CAP 定理：三选二还是取舍（goal=理解 CAP 定理；focus=一致性与可用性的取舍） | U[PARTIAL] 【S8-U】用户原先认为 CAP 是三选二；现在仍在核对这个说法是否准确。<br>M: 【S8-M】用户用“三选二”记忆 CAP，需要把旧口诀和分布式系统的实际取舍区分开。<br>E[PARTIAL] 【S8-E】用户对 CAP 的理解停留在“三选二”的口号。<br>R×1: 【S8-R1】用户把 CAP 记成三选二。 | s8-cap.md（3 chunk） | A: knowledge+user_input；不注入 understanding, memory, evidence, trajectory, session, history<br>B: knowledge, understanding, memory, history<br>C: intent=LEARNING_EXPLANATION，注入 knowledge, understanding, memory |
| S9 检索异常（DEGRADED） | 进程调度 | 进程调度：算法与指标（goal=理解常见进程调度算法；focus=时间片与优先级的取舍） | U[CORRUPT] 【S9-U】用户知道优先级调度，但还不清楚轮转与多级反馈队列。<br>M: 【S9-M】用户偏好用时间片和优先级理解进程调度。<br>E[PARTIAL] 【S9-E】用户尚未区分轮转与优先级调度的适用场景。<br>R×1: 【S9-R1】用户知道优先级调度。 | s9-scheduling.md（3 chunk） | A: knowledge+user_input；不注入 understanding, memory, evidence, trajectory, session, history<br>B: knowledge, memory<br>C: intent=LEARNING_EXPLANATION，注入 knowledge, memory, cognitive_status；不注入 trajectory<br>硬判据：C 保留 understanding-status:DEGRADED；B 折叠为 EMPTY |
| S10 上下文预算压力 | 文件系统 | 文件系统：日志与一致性（goal=理解文件系统日志（journaling）如何保证一致性；focus=日志提交与检查点的顺序） | U[PARTIAL] 【S10-U】用户对文件系统的理解分散在 inode、目录项、页缓存、日志提交和检查点几个部分，能说清单个概念，但还没…<br>M: 【S10-M】用户偏好从“崩溃后会发生什么”倒推文件系统设计，习惯把日志、检查点和写回顺序画成时间线；在学习文件系统时…<br>E[PARTIAL] 【S10-E】用户能复述日志先写后提交的步骤，但没有解释为什么提交顺序能避免部分更新。<br>R×1: 【S10-R1】用户已读过 … | s10-journaling.md（4 chunk） | A: knowledge+user_input；不注入 understanding, memory, evidence, trajectory, session, history<br>B: knowledge, understanding, memory, history<br>C: intent=LEARNING_EXPLANATION，注入 knowledge, understanding, memory, session, history<br>预期预算压力：历史截断/丢弃 |

- 每个场景使用独立的 Space / Concept / Session / Conversation；同一场景内 A/B/C 使用同一问题、同一文档、同一用户与同一预置数据，只有策略不同。
- 完整字段（known/misconceptions/uncertain、revision explanation、文档全文）在 `full-snapshot.json` 与 `scripts/phase5/full/scenarios.py` 中，报告只列摘要。

## 3. 90 次调用汇总

| 场景 | 策略 | rep | run_id | request | finish | prompt/completion/total | Usage | latency_ms | 实际注入来源 | 引用数 | 结果 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| S1 | A | 1 | 1 | fc8ba131 | stop | 1904/1661/3565 | REAL | 15640 | knowledge,user_input | 3 | OK |
| S1 | A | 2 | 7 | 1c0a6e53 | stop | 1904/2090/3994 | REAL | 17702 | knowledge,user_input | 3 | OK |
| S1 | A | 3 | 9 | b540fc75 | stop | 1904/1766/3670 | REAL | 16858 | knowledge,user_input | 3 | OK |
| S1 | C | 1 | 3 | 74b0c3ad | stop | 2632/1157/3789 | REAL | 10983 | evidence,history,knowledge,memory,session,understanding,user_input | 3 | OK |
| S1 | C | 2 | 11 | 74ae9721 | stop | 2632/1419/4051 | REAL | 13608 | evidence,history,knowledge,memory,session,understanding,user_input | 3 | OK |
| S1 | C | 3 | 13 | 74e4f951 | stop | 2632/1642/4274 | REAL | 14312 | evidence,history,knowledge,memory,session,understanding,user_input | 3 | OK |
| S1 | B | 1 | 5 | 9f1bff48 | stop | 2834/2602/5436 | REAL | 22891 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | OK |
| S1 | B | 2 | 15 | 6c1364cf | stop | 2834/2184/5018 | REAL | 19000 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | OK |
| S1 | B | 3 | 17 | a5c6e8be | stop | 2834/2322/5156 | REAL | 20765 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | OK |
| S2 | A | 1 | 19 | ad7dab28 | stop | 1901/1752/3653 | REAL | 15047 | knowledge,user_input | 3 | OK |
| S2 | A | 2 | 21 | 208d66e5 | stop | 1901/1600/3501 | REAL | 14640 | knowledge,user_input | 3 | OK |
| S2 | A | 3 | 23 | 964a7cf0 | stop | 1901/1343/3244 | REAL | 12688 | knowledge,user_input | 3 | OK |
| S2 | C | 1 | 25 | f8a12bc8 | stop | 2644/2129/4773 | REAL | 17797 | evidence,history,knowledge,memory,session,understanding,user_input | 3 | OK |
| S2 | C | 2 | 27 | be302d91 | stop | 2644/1867/4511 | REAL | 16281 | evidence,history,knowledge,memory,session,understanding,user_input | 3 | OK |
| S2 | C | 3 | 29 | 07f26371 | stop | 2644/1038/3682 | REAL | 9764 | evidence,history,knowledge,memory,session,understanding,user_input | 3 | OK |
| S2 | B | 1 | 31 | bf75d7dc | stop | 2845/2552/5397 | REAL | 21766 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | OK |
| S2 | B | 2 | 33 | dbb14bbb | stop | 2845/1413/4258 | REAL | 12672 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | OK |
| S2 | B | 3 | 35 | b5da998a | stop | 2845/1660/4505 | REAL | 14640 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | OK |
| S3 | A | 1 | 37 | 450bf115 | stop | 1844/759/2603 | REAL | 7936 | knowledge,user_input | 3 | OK |
| S3 | A | 2 | 39 | ba6d3c0d | stop | 1844/570/2414 | REAL | 6047 | knowledge,user_input | 3 | OK |
| S3 | A | 3 | 41 | bfa1ed1a | stop | 1844/908/2752 | REAL | 9047 | knowledge,user_input | 3 | OK |
| S3 | C | 1 | 43 | 6b83b10e | stop | 2484/847/3331 | REAL | 8813 | history,knowledge,memory,session,understanding,user_input | 3 | OK |
| S3 | C | 2 | 45 | 34c9e350 | stop | 2484/1429/3913 | REAL | 13453 | history,knowledge,memory,session,understanding,user_input | 3 | OK |
| S3 | C | 3 | 47 | c1d65197 | stop | 2484/1573/4057 | REAL | 14843 | history,knowledge,memory,session,understanding,user_input | 3 | OK |
| S3 | B | 1 | 49 | 0b232b9c | stop | 2757/1284/4041 | REAL | 12125 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | OK |
| S3 | B | 2 | 51 | 8a17f906 | stop | 2757/845/3602 | REAL | 8094 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | OK |
| S3 | B | 3 | 53 | 080eb6f5 | stop | 2757/1099/3856 | REAL | 10921 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | OK |
| S4 | A | 1 | 55 | 3f96ffa3 | stop | 1892/901/2793 | REAL | 8500 | knowledge,user_input | 3 | OK |
| S4 | A | 2 | 57 | 3ef097d0 | stop | 1892/870/2762 | REAL | 7967 | knowledge,user_input | 3 | OK |
| S4 | A | 3 | 59 | f4c3f832 | stop | 1892/890/2782 | REAL | 7891 | knowledge,user_input | 3 | OK |
| S4 | C | 1 | 61 | a4a0e2bc | stop | 2561/1659/4220 | REAL | 15000 | history,knowledge,memory,session,understanding,user_input | 3 | OK |
| S4 | C | 2 | 63 | a980922f | stop | 2561/1948/4509 | REAL | 16516 | history,knowledge,memory,session,understanding,user_input | 3 | OK |
| S4 | C | 3 | 65 | b056d608 | stop | 2561/1606/4167 | REAL | 14234 | history,knowledge,memory,session,understanding,user_input | 3 | OK |
| S4 | B | 1 | 67 | 53a16cd1 | stop | 2862/1072/3934 | REAL | 10187 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | OK |
| S4 | B | 2 | 69 | 69708974 | stop | 2862/1052/3914 | REAL | 10500 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | OK |
| S4 | B | 3 | 71 | 29ed839d | stop | 2862/1727/4589 | REAL | 16094 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | OK |
| S5 | A | 1 | 73 | 969c55e8 | stop | 1843/704/2547 | REAL | 6453 | knowledge,user_input | 3 | OK |
| S5 | A | 2 | 75 | 1c0abb99 | stop | 1843/350/2193 | REAL | 4516 | knowledge,user_input | 1 | OK |
| S5 | A | 3 | 77 | 9f634525 | stop | 1843/888/2731 | REAL | 7702 | knowledge,user_input | 3 | OK |
| S5 | C | 1 | 79 | 65347a6c | stop | 2906/1634/4540 | REAL | 14391 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | FAIL(1) |
| S5 | C | 2 | 81 | 2e6b25fb | stop | 2906/1432/4338 | REAL | 12733 | history,knowledge,memory,session,trajectory,understanding,user_input | 2 | FAIL(1) |
| S5 | C | 3 | 83 | 86fafc3c | stop | 2906/2206/5112 | REAL | 18140 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | FAIL(1) |
| S5 | B | 1 | 85 | ad20c1b4 | stop | 2906/1311/4217 | REAL | 11358 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | FAIL(1) |
| S5 | B | 2 | 87 | b304ca04 | stop | 2906/1293/4199 | REAL | 11813 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | FAIL(1) |
| S5 | B | 3 | 89 | b48a3eb3 | stop | 2906/1420/4326 | REAL | 12141 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | FAIL(1) |
| S6 | A | 1 | 91 | a5de71c7 | stop | 1822/1119/2941 | REAL | 9812 | knowledge,user_input | 3 | OK |
| S6 | A | 2 | 93 | 1d1ac359 | stop | 1822/1790/3612 | REAL | 14718 | knowledge,user_input | 2 | OK |
| S6 | A | 3 | 95 | 2fd3b53d | stop | 1822/966/2788 | REAL | 8437 | knowledge,user_input | 2 | OK |
| S6 | C | 1 | 97 | c4d89982 | stop | 2552/948/3500 | REAL | 7952 | evidence,history,knowledge,memory,session,understanding,user_input | 3 | OK |
| S6 | C | 2 | 99 | 117c205f | stop | 2552/1050/3602 | REAL | 9577 | evidence,history,knowledge,memory,session,understanding,user_input | 3 | OK |
| S6 | C | 3 | 101 | 705d3997 | stop | 2552/1001/3553 | REAL | 7875 | evidence,history,knowledge,memory,session,understanding,user_input | 3 | OK |
| S6 | B | 1 | 103 | e9b06b64 | stop | 2752/956/3708 | REAL | 9062 | history,knowledge,memory,session,trajectory,understanding,user_input | 2 | OK |
| S6 | B | 2 | 105 | 6b9e9039 | stop | 2752/927/3679 | REAL | 8329 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | OK |
| S6 | B | 3 | 107 | e181cde7 | stop | 2752/982/3734 | REAL | 8655 | history,knowledge,memory,session,trajectory,understanding,user_input | 2 | OK |
| S7 | A | 1 | 109 | f7feb43d | stop | 1828/1368/3196 | REAL | 12000 | knowledge,user_input | 3 | OK |
| S7 | A | 2 | 111 | 639a3290 | stop | 1828/1180/3008 | REAL | 10000 | knowledge,user_input | 3 | OK |
| S7 | A | 3 | 113 | 2c62aa40 | stop | 1828/1316/3144 | REAL | 10782 | knowledge,user_input | 3 | OK |
| S7 | C | 1 | 115 | f4b6dc42 | stop | 1907/1575/3482 | REAL | 14625 | history,knowledge,user_input | 3 | OK |
| S7 | C | 2 | 117 | 120db295 | stop | 1907/1481/3388 | REAL | 12687 | history,knowledge,user_input | 3 | OK |
| S7 | C | 3 | 119 | 5b6fbd41 | stop | 1907/1024/2931 | REAL | 9811 | history,knowledge,user_input | 3 | OK |
| S7 | B | 1 | 121 | 2741d44b | stop | 1963/958/2921 | REAL | 9391 | history,knowledge,session,user_input | 3 | OK |
| S7 | B | 2 | 123 | 2c7e6ad9 | stop | 1963/2536/4499 | REAL | 21452 | history,knowledge,session,user_input | 3 | OK |
| S7 | B | 3 | 125 | 9274fd34 | stop | 1963/1288/3251 | REAL | 11827 | history,knowledge,session,user_input | 3 | OK |
| S8 | A | 1 | 127 | 2e783e6d | stop | 1858/705/2563 | REAL | 7922 | knowledge,user_input | 3 | OK |
| S8 | A | 2 | 129 | b11c92ac | stop | 1858/983/2841 | REAL | 9718 | knowledge,user_input | 3 | OK |
| S8 | A | 3 | 131 | 8e47c9e7 | stop | 1858/1064/2922 | REAL | 9875 | knowledge,user_input | 3 | OK |
| S8 | C | 1 | 133 | b59661d9 | stop | 2530/1041/3571 | REAL | 10717 | history,knowledge,memory,session,understanding,user_input | 3 | OK |
| S8 | C | 2 | 135 | 145016fb | stop | 2530/1239/3769 | REAL | 11390 | history,knowledge,memory,session,understanding,user_input | 3 | OK |
| S8 | C | 3 | 137 | 75ceae2d | stop | 2530/1251/3781 | REAL | 11436 | history,knowledge,memory,session,understanding,user_input | 3 | OK |
| S8 | B | 1 | 139 | 884bd082 | stop | 2833/921/3754 | REAL | 8406 | history,knowledge,memory,session,trajectory,understanding,user_input | 2 | OK |
| S8 | B | 2 | 141 | eb40cc44 | stop | 2833/1224/4057 | REAL | 11062 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | OK |
| S8 | B | 3 | 143 | c6d2ff17 | stop | 2833/1148/3981 | REAL | 10906 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | OK |
| S9 | A | 1 | 145 | 095e32b4 | stop | 1819/1203/3022 | REAL | 9750 | knowledge,user_input | 3 | OK |
| S9 | A | 2 | 147 | 639b2df0 | stop | 1819/579/2398 | REAL | 4906 | knowledge,user_input | 3 | OK |
| S9 | A | 3 | 149 | ed54ba5c | stop | 1819/602/2421 | REAL | 5188 | knowledge,user_input | 3 | OK |
| S9 | C | 1 | 151 | 61fdd221 | stop | 2235/1022/3257 | REAL | 7375 | cognitive_status,history,knowledge,memory,session,user_input | 3 | OK |
| S9 | C | 2 | 153 | 0a0d5729 | stop | 2235/1868/4103 | REAL | 13516 | cognitive_status,history,knowledge,memory,session,user_input | 3 | OK |
| S9 | C | 3 | 155 | b79f8acf | stop | 2235/1256/3491 | REAL | 9967 | cognitive_status,history,knowledge,memory,session,user_input | 3 | OK |
| S9 | B | 1 | 157 | 341a6eef | stop | 2158/1234/3392 | REAL | 10202 | evidence,history,knowledge,memory,session,user_input | 3 | OK |
| S9 | B | 2 | 159 | b92b29c8 | stop | 2158/1499/3657 | REAL | 11484 | evidence,history,knowledge,memory,session,user_input | 3 | OK |
| S9 | B | 3 | 161 | 1f2cc525 | stop | 2158/1419/3577 | REAL | 10515 | evidence,history,knowledge,memory,session,user_input | 3 | OK |
| S10 | A | 1 | 163 | ba30e6eb | stop | 1918/1143/3061 | REAL | 9561 | knowledge,user_input | 4 | OK |
| S10 | A | 2 | 165 | b1deab27 | stop | 1918/1422/3340 | REAL | 11516 | knowledge,user_input | 4 | OK |
| S10 | A | 3 | 167 | 614f4274 | stop | 1918/1212/3130 | REAL | 9406 | knowledge,user_input | 4 | OK |
| S10 | C | 1 | 169 | 84f667a5 | stop | 3230/2757/5987 | REAL | 22016 | evidence,history,knowledge,memory,session,understanding,user_input | 4 | OK |
| S10 | C | 2 | 171 | d2e5c85b | stop | 3230/2544/5774 | REAL | 19375 | evidence,history,knowledge,memory,session,understanding,user_input | 4 | OK |
| S10 | C | 3 | 173 | 8eb95fe1 | stop | 3230/1870/5100 | REAL | 15516 | evidence,history,knowledge,memory,session,understanding,user_input | 4 | OK |
| S10 | B | 1 | 175 | 36faf345 | stop | 3430/1923/5353 | REAL | 16125 | history,knowledge,memory,session,trajectory,understanding,user_input | 3 | OK |
| S10 | B | 2 | 177 | 15070cb4 | stop | 3430/1932/5362 | REAL | 15858 | history,knowledge,memory,session,trajectory,understanding,user_input | 4 | OK |
| S10 | B | 3 | 179 | 73fb9f63 | stop | 3430/3366/6796 | REAL | 26906 | history,knowledge,memory,session,trajectory,understanding,user_input | 4 | OK |

## 4. 一致性检查

| 场景-策略 | 次数 | Context Hash | contextStrategy | promptTokens（3 次是否一致） | 引用集合（rep1 / rep2 / rep3，顺序无关） | 结果 |
|---|---|---|---|---|---|---|
| S1-A | 3 | `74fd3eabc2f6` | A | 1904（是） | 1:1,1:2,1:3 / 1:1,1:2,1:3 / 1:1,1:2,1:3 | PASS |
| S1-C | 3 | `2761f6b317ea` | C | 2632（是） | 1:1,1:2,1:3 / 1:1,1:2,1:3 / 1:1,1:2,1:3 | PASS |
| S1-B | 3 | `88f566c91dcc` | B | 2834（是） | 1:1,1:2,1:3 / 1:1,1:2,1:3 / 1:1,1:2,1:3 | PASS |
| S2-A | 3 | `a29b7a2ded0e` | A | 1901（是） | 2:4,2:5,2:6 / 2:4,2:5,2:6 / 2:4,2:5,2:6 | PASS |
| S2-C | 3 | `8904cfb62068` | C | 2644（是） | 2:4,2:5,2:6 / 2:4,2:5,2:6 / 2:4,2:5,2:6 | PASS |
| S2-B | 3 | `a83cb5214ff9` | B | 2845（是） | 2:4,2:5,2:6 / 2:4,2:5,2:6 / 2:4,2:5,2:6 | PASS |
| S3-A | 3 | `8d03d72960f9` | A | 1844（是） | 3:7,3:8,3:9 / 3:7,3:8,3:9 / 3:7,3:8,3:9 | PASS |
| S3-C | 3 | `11c1b65af5e4` | C | 2484（是） | 3:7,3:8,3:9 / 3:7,3:8,3:9 / 3:7,3:8,3:9 | PASS |
| S3-B | 3 | `ae16d7670a47` | B | 2757（是） | 3:7,3:8,3:9 / 3:7,3:8,3:9 / 3:7,3:8,3:9 | PASS |
| S4-A | 3 | `af3da11e71a9` | A | 1892（是） | 4:10,4:11,4:12 / 4:10,4:11,4:12 / 4:10,4:11,4:12 | PASS |
| S4-C | 3 | `eea8a58cd690` | C | 2561（是） | 4:10,4:11,4:12 / 4:10,4:11,4:12 / 4:10,4:11,4:12 | PASS |
| S4-B | 3 | `4ea9d6d3d1ac` | B | 2862（是） | 4:10,4:11,4:12 / 4:10,4:11,4:12 / 4:10,4:11,4:12 | PASS |
| S5-A | 3 | `a2e4a16d42dc` | A | 1843（是） | 5:13,5:14,5:15 / 5:15 / 5:13,5:14,5:15 | FAIL |
| S5-C | 3 | `ba83568ae5cf` | C | 2906（是） | 5:13,5:14,5:15 / 5:13,5:15 / 5:13,5:14,5:15 | FAIL |
| S5-B | 3 | `ba83568ae5cf` | B | 2906（是） | 5:13,5:14,5:15 / 5:13,5:14,5:15 / 5:13,5:14,5:15 | PASS |
| S6-A | 3 | `f3fc2b67604f` | A | 1822（是） | 6:16,6:17,6:18 / 6:17,6:18 / 6:17,6:18 | FAIL |
| S6-C | 3 | `20847f973d79` | C | 2552（是） | 6:16,6:17,6:18 / 6:16,6:17,6:18 / 6:16,6:17,6:18 | PASS |
| S6-B | 3 | `5ee36741029b` | B | 2752（是） | 6:17,6:18 / 6:16,6:17,6:18 / 6:16,6:17 | FAIL |
| S7-A | 3 | `05f42fb7e210` | A | 1828（是） | 7:19,7:20,7:21 / 7:19,7:20,7:21 / 7:19,7:20,7:21 | PASS |
| S7-C | 3 | `5f3fb8ab0652` | C | 1907（是） | 7:19,7:20,7:21 / 7:19,7:20,7:21 / 7:19,7:20,7:21 | PASS |
| S7-B | 3 | `d6c095d014bb` | B | 1963（是） | 7:19,7:20,7:21 / 7:19,7:20,7:21 / 7:19,7:20,7:21 | PASS |
| S8-A | 3 | `b96471c9c65f` | A | 1858（是） | 8:22,8:23,8:24 / 8:22,8:23,8:24 / 8:22,8:23,8:24 | PASS |
| S8-C | 3 | `261dd31ff137` | C | 2530（是） | 8:22,8:23,8:24 / 8:22,8:23,8:24 / 8:22,8:23,8:24 | PASS |
| S8-B | 3 | `072a02bf9dd2` | B | 2833（是） | 8:23,8:24 / 8:22,8:23,8:24 / 8:22,8:23,8:24 | FAIL |
| S9-A | 3 | `dbaa87729365` | A | 1819（是） | 9:25,9:26,9:27 / 9:25,9:26,9:27 / 9:25,9:26,9:27 | PASS |
| S9-C | 3 | `1f565276a1b5` | C | 2235（是） | 9:25,9:26,9:27 / 9:25,9:26,9:27 / 9:25,9:26,9:27 | PASS |
| S9-B | 3 | `f77be8556c61` | B | 2158（是） | 9:25,9:26,9:27 / 9:25,9:26,9:27 / 9:25,9:26,9:27 | PASS |
| S10-A | 3 | `edefc644812b` | A | 1918（是） | 10:28,10:29,10:30,10:31 / 10:28,10:29,10:30,10:31 / 10:28,10:29,10:30,10:31 | PASS |
| S10-C | 3 | `18356b59aafe` | C | 3230（是） | 10:28,10:29,10:30,10:31 / 10:28,10:29,10:30,10:31 / 10:28,10:29,10:30,10:31 | PASS |
| S10-B | 3 | `22356af77c85` | B | 3430（是） | 10:28,10:29,10:30 / 10:28,10:29,10:30,10:31 / 10:28,10:29,10:30,10:31 | FAIL |

- `run_id` 取自 chat SSE 的 `message.start`，按接口语义即 `run_type=CHAT` 的运行；`agent_run.run_type` 未单独落盘，清理后无法补验（见第 12 节）。

策略间 Context Hash 是否不同（应全部为「是」）：

| 场景 | A vs B | A vs C | B vs C |
|---|---|---|---|
| S1 | 是 | 是 | 是 |
| S2 | 是 | 是 | 是 |
| S3 | 是 | 是 | 是 |
| S4 | 是 | 是 | 是 |
| S5 | 是 | 是 | 否 |
| S6 | 是 | 是 | 是 |
| S7 | 是 | 是 | 是 |
| S8 | 是 | 是 | 是 |
| S9 | 是 | 是 | 是 |
| S10 | 是 | 是 | 是 |

- 唯一相同的一组是 **S5 的 B vs C**：两者实际注入的 section 相同（history / knowledge / memory / session / trajectory（含 `trajectory_source:evidence:*`）/ understanding / user_input），因此送入模型的 messages 完全一致；差异只体现在 Manifest 的 `intent`、`policy_version` 与 `per_type` 的 required/cap。这是复盘场景下两种策略的等价注入结果，不是策略串号。A 与 B/C 在全部 10 个场景都不同。

## 5. A/B/C 分场景对照

### S1 普通知识问答

| 策略 | 实际注入来源 | intent | 答案历史标记 | 引用次数 | 引用溯源 | 预算 used/effective |
|---|---|---|---|---|---|---|
| A | knowledge,user_input | null | - | 9/3 | 可溯源 | 534/3000 |
| C | evidence,history,knowledge,memory,session,understanding,user_input | LEARNING_EXPLANATION | - | 9/3 | 可溯源 | 1588/2700 |
| B | history,knowledge,memory,session,trajectory,understanding,user_input | null | - | 9/3 | 可溯源 | 1869/3000 |

- **A**：无失败项。 回答字数：1075, 1479, 1349。
- **C**：无失败项。 回答字数：1039, 1335, 1352。
- **B**：无失败项。 回答字数：1090, 1101, 1108。

### S2 同一 Session 连续学习

| 策略 | 实际注入来源 | intent | 答案历史标记 | 引用次数 | 引用溯源 | 预算 used/effective |
|---|---|---|---|---|---|---|
| A | knowledge,user_input | null | - | 9/3 | 可溯源 | 534/3000 |
| C | evidence,history,knowledge,memory,session,understanding,user_input | CONTINUE_TASK | - | 9/3 | 可溯源 | 1603/2700 |
| B | history,knowledge,memory,session,trajectory,understanding,user_input | null | - | 9/3 | 可溯源 | 1880/3000 |

- **A**：无失败项。 回答字数：1452, 1470, 1188。
- **C**：无失败项。 回答字数：1337, 1091, 1209。
- **B**：无失败项。 回答字数：1183, 888, 1495。

### S3 跨 Session 学习

| 策略 | 实际注入来源 | intent | 答案历史标记 | 引用次数 | 引用溯源 | 预算 used/effective |
|---|---|---|---|---|---|---|
| A | knowledge,user_input | null | - | 9/3 | 可溯源 | 473/3000 |
| C | history,knowledge,memory,session,understanding,user_input | LEARNING_EXPLANATION | - | 9/3 | 可溯源 | 1427/2700 |
| B | history,knowledge,memory,session,trajectory,understanding,user_input | null | - | 9/3 | 可溯源 | 1791/3000 |

- **A**：无失败项。 回答字数：883, 723, 954。
- **C**：无失败项。 回答字数：827, 1031, 1046。
- **B**：无失败项。 回答字数：1152, 874, 1060。

### S4 跨 Conversation 同 Concept（TLB）

| 策略 | 实际注入来源 | intent | 答案历史标记 | 引用次数 | 引用溯源 | 预算 used/effective |
|---|---|---|---|---|---|---|
| A | knowledge,user_input | null | 小托盘=N | 9/3 | 可溯源 | 515/3000 |
| C | history,knowledge,memory,session,understanding,user_input | LEARNING_EXPLANATION | 小托盘=Y | 9/3 | 可溯源 | 1461/2700 |
| B | history,knowledge,memory,session,trajectory,understanding,user_input | null | 小托盘=Y | 9/3 | 可溯源 | 1843/3000 |

- **A**：无失败项。 回答字数：695, 821, 761。
- **C**：无失败项。 回答字数：1237, 1215, 996。
- **B**：无失败项。 回答字数：1131, 1020, 1317。

### S5 历史错误回顾

| 策略 | 实际注入来源 | intent | 答案历史标记 | 引用次数 | 引用溯源 | 预算 used/effective |
|---|---|---|---|---|---|---|
| A | knowledge,user_input | null | 【S5-HISTORY】=N | 7/3 | 可溯源 | 442/3000 |
| C | history,knowledge,memory,session,trajectory,understanding,user_input | LEARNING_REVIEW | 【S5-HISTORY】=N | 8/3 | 可溯源 | 1915/2700 |
| B | history,knowledge,memory,session,trajectory,understanding,user_input | null | 【S5-HISTORY】=N | 9/3 | 可溯源 | 1915/3000 |

- **A**：无失败项。 回答字数：605, 326, 597。
- **C**：回答缺少预期历史标记：【S5-HISTORY】。 回答字数：839, 649, 804。
- **B**：回答缺少预期历史标记：【S5-HISTORY】。 回答字数：708, 656, 971。

### S6 当前掌握程度

| 策略 | 实际注入来源 | intent | 答案历史标记 | 引用次数 | 引用溯源 | 预算 used/effective |
|---|---|---|---|---|---|---|
| A | knowledge,user_input | null | - | 7/3 | 可溯源 | 435/3000 |
| C | evidence,history,knowledge,memory,session,understanding,user_input | META_QUESTION | - | 9/3 | 可溯源 | 1470/2700 |
| B | history,knowledge,memory,session,trajectory,understanding,user_input | null | - | 7/3 | 可溯源 | 1750/3000 |

- **A**：无失败项。 回答字数：912, 675, 663。
- **C**：无失败项。 回答字数：836, 720, 797。
- **B**：无失败项。 回答字数：690, 873, 839。

### S7 话题切换

| 策略 | 实际注入来源 | intent | 答案历史标记 | 引用次数 | 引用溯源 | 预算 used/effective |
|---|---|---|---|---|---|---|
| A | knowledge,user_input | null | - | 9/3 | 可溯源 | 452/3000 |
| C | history,knowledge,user_input | LEARNING_EXPLANATION | - | 9/3 | 可溯源 | 557/2700 |
| B | history,knowledge,session,user_input | null | - | 9/3 | 可溯源 | 623/3000 |

- **A**：无失败项。 回答字数：1903, 1332, 1795。
- **C**：无失败项。 回答字数：1875, 1330, 1139。
- **B**：无失败项。 回答字数：971, 2172, 1509。

### S8 新旧知识冲突

| 策略 | 实际注入来源 | intent | 答案历史标记 | 引用次数 | 引用溯源 | 预算 used/effective |
|---|---|---|---|---|---|---|
| A | knowledge,user_input | null | - | 9/3 | 可溯源 | 492/3000 |
| C | history,knowledge,memory,session,understanding,user_input | LEARNING_EXPLANATION | - | 9/3 | 可溯源 | 1471/2700 |
| B | history,knowledge,memory,session,trajectory,understanding,user_input | null | - | 8/3 | 可溯源 | 1868/3000 |

- **A**：无失败项。 回答字数：775, 853, 892。
- **C**：无失败项。 回答字数：1063, 989, 932。
- **B**：无失败项。 回答字数：967, 936, 799。

### S9 检索异常（DEGRADED）

| 策略 | 实际注入来源 | intent | 答案历史标记 | 引用次数 | 引用溯源 | 预算 used/effective |
|---|---|---|---|---|---|---|
| A | knowledge,user_input | null | - | 9/3 | 可溯源 | 448/3000 |
| C | cognitive_status,history,knowledge,memory,session,user_input | LEARNING_EXPLANATION | - | 9/3 | 可溯源 | 1066/2700 |
| B | evidence,history,knowledge,memory,session,user_input | null | - | 9/3 | 可溯源 | 934/3000 |

- **A**：无失败项。 回答字数：1285, 714, 772。
- **C**：无失败项。 回答字数：826, 1401, 1297。
- **B**：无失败项。 回答字数：1269, 1420, 1227。

### S10 上下文预算压力

| 策略 | 实际注入来源 | intent | 答案历史标记 | 引用次数 | 引用溯源 | 预算 used/effective |
|---|---|---|---|---|---|---|
| A | knowledge,user_input | null | - | 12/3 | 可溯源 | 604/3000 |
| C | evidence,history,knowledge,memory,session,understanding,user_input | LEARNING_EXPLANATION | - | 12/3 | 可溯源 | 2474/2700 |
| B | history,knowledge,memory,session,trajectory,understanding,user_input | null | - | 11/3 | 可溯源 | 2754/3000 |

- **A**：无失败项。 回答字数：1370, 1259, 1250。
- **C**：无失败项。 回答字数：1544, 1498, 1406。
- **B**：无失败项。 回答字数：1644, 1270, 1537。

### 5.1 七维对照（人工判读）

> 说明：完整回答在每次调用后被 `restore_scenario` 按隔离规则删除，只保留了前 300 字预览；因此下表的准确性维度是**结构性判读**（结合 Manifest、引用、标记、预算与预览），不是逐句人工评分。第三列「历史准确性」只对含个人历史断言的场景判定，「当前掌握准确性」只对 S6 判定，其余标「不适用」。

| 场景 | 上下文相关性 | 历史准确性 | 当前掌握准确性 | 上下文非干扰 | 引用准确性 | 失败可见性 | 预算利用（A/C/B） |
|---|---|---|---|---|---|---|---|
| S1 | A 仅知识；B/C 额外注入同主题 TCP 历史与会话 | 不适用 | 不适用 | A 无个人上下文；B/C 仅同主题上下文，无干扰 | 9/9 引用可溯源 | 无异常，不适用 | 534/3000；1588/2700；1869/3000 |
| S2 | C 注入匹配 Session 的 goal/focus；B 旧路径也注入 Session | B/C 复述上一轮「内部节点只存导航键」的结论，未编造 | 不适用 | A 无个人上下文；C 会话概念匹配 | 9/9 可溯源 | 不适用 | 534/3000；1603/2700；1880/3000 |
| S3 | C 跨 Session 召回上一 Session 的个人上下文；B 旧路径同样召回 | B/C 引用上一 Session 的叶子链表结论 | 不适用 | A 无个人上下文；C 当前 Session 只提供 goal/focus | 9/9 可溯源 | 不适用 | 473/3000；1427/2700；1791/3000 |
| S4 | A 只有知识；C/B 命中 U/M 中的「小托盘」比喻 | A 明确声明看不到历史、拒绝编造比喻；C/B 正确复用用户比喻 | 不适用 | A 未泄漏「小托盘」；C/B 只使用同一 Concept 的个人上下文 | 9/9 可溯源 | 不适用 | 515/3000；1461/2700；1843/3000 |
| S5 | C 注入 history + trajectory（含 revision 与 `trajectory_source:evidence:*`）；B 旧路径注入 history/U/M/trajectory | B/C 均引用了原句内容，但都省略合成标记 `【S5-HISTORY】` → 按预注册判据 FAIL；A 正确声明无记录 | 不适用 | A 无个人上下文；B/C 使用同一用户历史，无跨场景串号 | 可溯源（引用数 7/8/9，分母为 3 次） | 不适用 | 442/3000；1915/2700；1915/3000 |
| S6 | C 读 Understanding + Evidence 判定薄弱点；B 读 U/M + trajectory；A 只给自测题 | 不适用 | C/B 均指出「TLB miss 之后遍历页表的代价」为薄弱点，与预置 Understanding 一致；A 未给出掌握度 | A 不注入个人上下文；B/C 仅本 Concept | 可溯源 | 不适用 | 435/3000；1470/2700；1750/3000 |
| S7 | C 判定 Session 概念（Redis）与当前问题（Raft）不匹配，不注入 Session；B 注入旧 Redis Session；A 无 | 不适用 | 不适用 | C 无旧话题干扰；B 有旧 Session 干扰（预期近似缺陷）；A 无 | 9/9 可溯源 | 不适用 | 452/3000；557/2700；623/3000 |
| S8 | C/B 引用个人旧口诀并纠正；A 仅知识 | B/C 明确引用「CAP 就是三选二」的旧说法并纠正 | 不适用 | A 无个人上下文；B/C 同 Concept | 可溯源（引用数 9/9/8） | 不适用 | 492/3000；1471/2700；1868/3000 |
| S9 | C 注入 `cognitive_status`；B 把 DEGRADED 折叠为 EMPTY；A 无个人状态 | 不适用 | 不适用 | A 无个人上下文 | 9/9 可溯源 | **C 保留 `understanding-status:DEGRADED`；B 不可见（预期近似）；A 无个人状态** | 448/3000；1066/2700；934/3000 |
| S10 | 三者都注入知识；C/B 注入长历史与个人上下文；历史被截到最近 12/28 条 | 不适用 | 不适用 | A 无个人上下文；B/C 同 Concept | C/A 引用稳定 4 条；B 出现 3 vs 4 条波动但均可溯源 | 不适用 | 604/3000；2474/2700（91.6%）；2754/3000（91.8%） |

## 6. 失败用例

| 场景-策略 | rep | 失败项 |
|---|---|---|
| S5-C-1 | 1 | 回答缺少预期历史标记：【S5-HISTORY】 |
| S5-C-2 | 2 | 回答缺少预期历史标记：【S5-HISTORY】 |
| S5-C-3 | 3 | 回答缺少预期历史标记：【S5-HISTORY】 |
| S5-B-1 | 1 | 回答缺少预期历史标记：【S5-HISTORY】 |
| S5-B-2 | 2 | 回答缺少预期历史标记：【S5-HISTORY】 |
| S5-B-3 | 3 | 回答缺少预期历史标记：【S5-HISTORY】 |
| S10-B | - | 一致性：citations 不一致：['[[10, 28], [10, 29], [10, 30]]', '[[10, 28], [10, 29], [10, 30], [10, 31]]', '[[10, 28], [10, 29], [10, 30], [10, 31]]'] |
| S5-A | - | 一致性：citations 不一致：['[[5, 13], [5, 14], [5, 15]]', '[[5, 15]]', '[[5, 13], [5, 14], [5, 15]]'] |
| S5-C | - | 一致性：citations 不一致：['[[5, 13], [5, 14], [5, 15]]', '[[5, 13], [5, 15]]', '[[5, 13], [5, 14], [5, 15]]'] |
| S6-A | - | 一致性：citations 不一致：['[[6, 16], [6, 17], [6, 18]]', '[[6, 17], [6, 18]]', '[[6, 17], [6, 18]]'] |
| S6-B | - | 一致性：citations 不一致：['[[6, 17], [6, 18]]', '[[6, 16], [6, 17], [6, 18]]', '[[6, 16], [6, 17]]'] |
| S8-B | - | 一致性：citations 不一致：['[[8, 23], [8, 24]]', '[[8, 22], [8, 23], [8, 24]]', '[[8, 22], [8, 23], [8, 24]]'] |

- 判据复核：报告阶段用修正后的 Manifest 判据复核了已存 Manifest（Evidence 允许以 `trajectory_source:evidence:*` 出现，与顶层 evidence 等价；见 docs/testing/phase5-pilot-results.md §10.1），其余运行期判据沿用原始记录；原始 `problems` 未从 results.json 删除。

观测性告警（不构成本批次失败判据）：

| 场景-策略 | 告警 |
|---|---|
| S10-A-1 | S10 预算压力：statuses=['SELECTED'] used/effective=604/3000 |
| S10-A-2 | S10 预算压力：statuses=['SELECTED'] used/effective=604/3000 |
| S10-A-3 | S10 预算压力：statuses=['SELECTED'] used/effective=604/3000 |
| S10-C-1 | S10 预算压力：statuses=['SELECTED'] used/effective=2474/2700 |
| S10-C-2 | S10 预算压力：statuses=['SELECTED'] used/effective=2474/2700 |
| S10-C-3 | S10 预算压力：statuses=['SELECTED'] used/effective=2474/2700 |
| S10-B-1 | S10 预算压力：statuses=['SELECTED'] used/effective=2754/3000 |
| S10-B-2 | S10 预算压力：statuses=['SELECTED'] used/effective=2754/3000 |
| S10-B-3 | S10 预算压力：statuses=['SELECTED'] used/effective=2754/3000 |

## 7. 成本记录

| 项目 | 数值 |
|---|---|
| LLM 调用次数（上下文记录） | 90 |
| prompt tokens | 214950 |
| completion tokens | 125366 |
| total tokens | 340316 |
| Usage=UNKNOWN 的调用 | 0 |
| 模型侧重试次数（attemptHistory 超出首轮） | 0 |
| Agent 进程内 Embedding 调用 | 150 |
| Agent 进程内 Embedding 文本数 | 150 |
| 单次运行 Embedding 最大值 | 2 |
| 单次请求文本数（上界） | 1 |
| Tavily 调用 | 0 |
| 预计费用 | 无法验证：仓库未提供可核对的单价与计费口径 |

- Embedding 口径：① 单次运行（一轮对话）≤ 3 次；② 单次 Embedding 请求携带文本数 ≤ 10，本批允许上限 100（10 × 场景数）。本批次 Agent 计数为 150 次调用 / 150 条文本，单次运行最大 2 次，单次请求文本数上界 1，预算=PASS。
- 整批总调用数 150 次，与 docs/testing/phase5-pilot-results.md §10.2 的预估 120-150 次一致（建议硬上限 200）；该总数不是「批量」判据。
- setup 阶段 10 份文档的索引 Embedding 由 Spring 侧发起，不在 Agent 计数内；该阶段只执行一次，不属于每次对话的预算。

| 场景 | 耗时 ms（含 3 策略切换与 9 次调用） |
|---|---|
| S1 | 126671 |
| S2 | 161500 |
| S3 | 117750 |
| S4 | 133406 |
| S5 | 125938 |
| S6 | 111157 |
| S7 | 139281 |
| S8 | 118282 |
| S9 | 109438 |
| S10 | 173094 |

## 8. 冻结一致性

| 检查 | 结果 | 说明 |
|---|---|---|
| MySQL 全局冻结 | PASS | 10 个 Space 与 U/E/R/M/文档均未漂移 |
| Qdrant 文档 + Memory | PASS | point 集合与 payload 哈希未变 |
| 默认 Memory collection | PASS | 未读写共享默认 collection |

- 冻结基线 combined：`8394ca7307e493b7696b5245384cc487d584050064e74222ac8eb79713a809d1`；结束后 combined：`8394ca7307e493b7696b5245384cc487d584050064e74222ac8eb79713a809d1`。
- 分析链路：`AILS_ANALYSIS_MODE=mq`，Agent analysis worker 未启动；请求/重试/死信队列消费者为 0，Spring 结果队列消费者 1（收不到 Agent 结果）。90 次调用每次前后都做了冻结与 Qdrant 哈希核对，未发现任何认知数据或向量写回。

## 9. 清理状态

| 项目 | 结果 |
|---|---|
| cleanup.py exitCode | 0 |
| cleanup 自检 ok | True |
| MySQL 库残留 | 0 |
| Qdrant phase5-pilot 残留 | [] |
| RabbitMQ 队列残留 | [] |
| RabbitMQ 交换机残留 | [] |
| 仍开放的端口 | [] |
| 独立残留检查 | PASS |

## 10. 结论

- 失败项总数：12。
- 结论：**FAIL**（存在满足失败判据的用例，详见第 6 节）。
- 失败归因：
  - **6 条为 S5 B/C 答案未逐字包含 `【S5-HISTORY】` 合成标记**（3 次 × 2 策略）。B/C 都引用了原句内容（例如 B：「我原来以为慢启动慢是因为它每次只把窗口加一。」），只是省略了注入消息的人工括号标记。按本实验预注册的硬标记判据计为 FAIL；是否接受「引用原句内容但不复制合成标记」属于判据设计问题，见第 12 节决策点 8。
  - **6 条为 3 次 repeat 的引用集合不一致**（S5-A、S5-C、S6-A、S6-B、S8-B、S10-B）。体现为同一上下文下模型有时标注 2 条、有时 3 条引用；所有被标注的引用都可溯源，未出现伪造来源。该差异来自供应商在 temperature=0 下仍存在的输出非确定性，不是上下文组装差异（Manifest / Context Hash / prompt tokens 在 30 个「场景 × 策略」组内全部一致）。
- 未触发的失败判据：C 的 Manifest 未缺少 revision/evidence（S5 的 Evidence 以 `trajectory_source:evidence:5` 注入，已在报告阶段按既有 schema 复核）；A 未注入个人上下文；B 未出现 intent/policy（`null`）且 DEGRADED 折叠符合近似；无不可溯源引用；无 Usage 伪造；无空回答/截断/超时/HTTP 错误；冻结与清理全部 PASS。
- 质量判断的边界：本报告依据可观测信号（Manifest 来源、历史标记、引用溯源、预算、usage）评估，不包含人工对答案正确性的逐条评分；完整回答未保留（隔离 restore 删除），第 5.1 节为结构性判读。

## 11. Git 状态

| 项目 | 值 |
|---|---|
| branch | main |
| HEAD | 87aa1822d425eefdc2fc63991470e473402215e5 |
| HEAD subject | docs(phase5): 完成 3 次 C 策略真实验证，全部 PASS |
| 工作区状态 | M scripts/phase5/pilot/cleanup.py；?? scripts/phase5/full/；?? docs/testing/phase5-abc-results.md |
| 是否 commit | 否（本批次未执行 git commit） |
| 是否 push | 否（本批次未执行 git push） |

## 12. 需要人工复核的决策点

1. **temperature=0**：全量实验只在 Agent 子进程注入 `AILS_LLM_TEMPERATURE=0`，未修改 `agent/.env`。目的是让同一场景 + 策略的 3 次 repeat 可对照；但这与生产默认温度不同，若评审要求保留生产温度，需要接受 3 次 repeat 的 Context Hash 仍可一致、但引用集合与 prompt tokens 可能波动。
2. **S9 人为故障方式**：在隔离库中向 `learning_understanding.status` 写入非法枚举（空串），使 Spring 读取该行失败，从而触发 Agent 的 DEGRADED。该行只存在于隔离库，清理时随库删除；需要人工确认这是可接受的「检索异常」注入方式。
3. **策略切换模型**：A/B/C 只能进程级切换，因此每个「场景 × 策略」块重启一次 Agent（共 30 次），块内 3 次 repeat 共用同一进程；没有做「每次调用都重启」的更强隔离。
4. **Memory 向量来源**：实验用 Spring 公开 API 建 Memory 行，并用 Agent 内部 `/agent/memories/sync` 建向量；未经过生产 Outbox Relay。需要确认这等价于生产检索路径。
5. **Embedding 计数口径**：只统计 Agent 进程内运行时检索的 Embedding；setup 阶段文档索引由 Spring 侧调用，单独说明、不计入每次对话预算。
6. **分析写回**：以 `AILS_ANALYSIS_MODE=mq` 启动但未启动 Agent analysis worker，Spring 结果消费者存在但收不到结果；冻结表用于验证没有任何认知数据写回。
7. **Citation 一致性**：3 次 repeat 要求引用集合一致；temperature=0 下仍有 6/30 组出现 2 条 vs 3 条引用的波动（Manifest、Context Hash、prompt tokens 均一致），报告按失败判据如实标记。需要确认「引用集合必须逐次相同」是否应保留为硬判据，还是改为「引用必须可溯源 + 覆盖率阈值」。
8. **S5 硬标记判据**：预注册要求回答逐字包含 `【S5-HISTORY】`，实际 B/C 都引用了标记后的原句内容但没有复制合成标记。若判据本意是「引用用户原话」，建议将判据改为「原句内容出现」；若本意是「验证历史消息逐字注入并被复述」，则应保留 FAIL。本次未修改判据，结论按预注册口径记为 FAIL。
9. **完整回答留存**：`restore_scenario` 在每次调用后删除新消息以保证隔离，因此 90 条完整回答没有落盘，报告只能保留 300 字预览并做结构性判读。若后续需要逐句人工评分，应在 `one_call` 中额外保存完整回答到隔离 temp 目录（不写回业务库），并把它纳入下一批实验的前置条件。
10. **run_type 未落盘**：本次 `run_id` 取自 chat SSE 的 `message.start`，按接口语义就是 `run_type=CHAT` 的运行，且每条都关联到 COMPLETED 的 agent_run 与 assistant message；但脚本没有把 `agent_run.run_type` 单独写入结果，库已按清理要求删除，无法再补验。下一批建议在查询 agent_run 时显式带上 `run_type` 并写入记录。

