# 已知限制与未完成验收

这份清单是交付的一部分。它记录**当前代码确实做不到的事**，避免把设计目标、
Mock 结果或历史报告当成已完成验收。

## 1. 真实模型：已完成 Runtime 复验和服务端 E2E，但不是完整产品验收

2026-10-30 使用 `deepseek-flash` 完成 5 场景、6 次真实模型调用。数据播种仍由
Mock Agent 完成；真实模型只负责生成。完整原始输出与判定见
[`docs/testing/phase53-real-model-20261030.md`](../testing/phase53-real-model-20261030.md)。

同日另起 Spring 8081 + Agent 8000，通过公开 API 完成 3 轮服务端 E2E
（普通问题 / 错误解释 / 显式复盘），**47 PASS / 0 FAIL**。三次聊天均走
`message.start → delta → end`，消息与 `agent_run` 落库，inline 分析返回
`agent-v1`；显式复盘读到 3 个 Revision、`history_guard issues=[]`、未 fallback。
证据见
[`docs/testing/phase53-spring-e2e-20261030.md`](../testing/phase53-spring-e2e-20261030.md)。

| 场景 | 2026-10-30 结果 | 证据 |
| --- | --- | --- |
| A 当前误区 | **PASS** | 区分页表 / 页表项 / 缺页，未假设已掌握 |
| B 纠正后普通问题 | **PASS** | 保留 `TLB miss ≠ 缺页`，没有把扩展内容写成已掌握 |
| C 显式复盘 | **PASS** | 只陈述记录，明确「记录里看不出原因」 |
| C-only 无轨迹复盘 | **PASS（fallback）** | 模型两次拒绝编造，但被 `no_history_denial` 误判，最终走确定性 fallback |
| D Memory 冲突 | **PASS** | 明确旧 Memory 是历史，当前状态不同 |

历史失败样本仍保留：5.3.1 的 C 曾虚构「很可能通过重读定义迁移」「没有外部资料
介入」，B 曾为 `PARTIAL`。这些是 2026-10-27 的结果，不因本轮通过而删除。

2026-10-30 阶段 5.4 又补跑了四类链路：文档引用 **28 / 0**（真实 Qdrant +
Embedding + `deepseek-flash`）、MQ 真实模型 **19 / 0**、同一服务端场景连续
**3 轮 × 47 / 0**、并发 **6/12、12/24、24/24 均 10 / 0**。压测还暴露并修复了
两个数据库死锁和一个 `message.end` 顺序竞态。完整报告见
[`docs/testing/phase54-real-model-20261030.md`](../testing/phase54-real-model-20261030.md)。

因此：

- 可以声称「已完成单轮真实模型 Agent Runtime 行为复验」；
- 可以声称「已完成 Spring SSE → Agent → 真实模型的 inline 服务端 E2E」；
- 可以声称「已完成一次 MQ → Worker → 真实模型 → Result Consumer → MySQL」；
- 可以声称「已完成一次真实 Qdrant / Embedding / LLM 的文档引用 E2E」；
- 可以声称「已完成 3 轮重复采样和 6/12、12/24、24/24 的单机并发基线」；
- **不能**声称「真实模型已经稳定遵守证据边界」：C-only 依赖 fallback，
  3 轮重复采样只覆盖 3 个场景，且 MQ 轮出现过 `ConnectError` + 非法 JSON；
- **不能**声称「教学效果已被真实模型验证」：没有人工评分、学习者对照或留存指标；
- **不能**声称「Flutter 前端可用」：前端未测试、未修改；
- **不能**声称「RAG 引用质量通过」：28 / 0 只证明标签可回跳，
  只用了两篇各 1 个 chunk 的合成文档，没有事实核查；
- **不能**声称「已通过并发压测」：没有 SLO、持续负载、多实例、限流或断线重连验证。

### 仍需完成的最低验收

1. 对引用回答做人工评分，覆盖多 chunk、干扰片段、无依据问题；
2. 扩大重复采样与并发规模，记录 `finish_reason`、错误率、P95/P99，而不是
   只记录脚本判定；
3. 对 C / C-only 做人工评分或双人复核，而不是单次执行者判定；
4. 修复 `history_guard` 对「不能反过来断言你以前没有这个错误」的误报，
   再确认 C-only 不再依赖 fallback；
5. 启动 Flutter 前端，验证 SSE 消费、断线重连和渲染；
6. 给序号分配 / Session 首次归属补单元或集成回归测试；
7. 用 Testcontainers 把 MQ 链路纳入 CI，而不是依赖手工 broker + Worker。

### 五个已确认的工程限制

- **预算截断**：`deepseek-flash` 的推理内容也消耗生成预算。`max_tokens=1024`
  时 A/B/C/D 的回答都在句子中间被截断；`4096` 时全部 `finish_reason=stop`。
  本地 `.env` 已改为 4096。
- **守卫误报**：C-only 首答「不能反过来断言『你以前没有这个错误』」被
  `_NO_HISTORY_DENIAL_RE` 当成「断言过去没有错误」，触发一次重生成；重生成
  仍被误判，最终 fallback。安全性未受损，但多了一次生成，且掩盖了模型本身
  已经正确处理历史缺失的事实。
- **分析枚举漂移**：服务端 E2E 首轮发现真实模型把 `verificationDecision.trigger`
  写成自由文本，Pydantic 校验失败，整轮分析退回 `rules-v1`。已增加
  `_coerce_verification_enums()`、prompt 枚举和契约测试；证据、理解、记忆的枚举
  仍严格校验。这个修复避免「后端 fallback 让链路看起来通过」的误判。
- **供应商连接 / JSON 不稳定**：5.4 MQ 轮首次调用 `ConnectError`（可重试），
  重试拿到 HTTP 200 但 JSON 非法（不可重试），run 落 `FAILED`，由 `rules-v1`
  写 `UNKNOWN` 观察。并发轮也出现过上游 `RemoteProtocolError`，单请求约 80 s 后
  `message.error`。这不是数据库死锁，也没有被修复；失败样本保留在
  `backend/backend/target/phase54-mq-real-e2e/20261030-220047/`。
- **并发下的锁顺序**：压测发现两处真实死锁——消息序号在
  `message(conversation_id, sequence_no)` 上做锁定范围读会锁住相邻对话的索引间隙；
  首次 Session 归属会在 `conversation.session_id` 外键检查与
  `learning_session` 更新之间形成 S→X 锁升级。已分别改为
  `conversation.last_message_seq` 计数器、锁空间行 + 先写 session 再写 conversation。
  24 并发复测通过，但这仍是单机短问题基线，不能外推到更高负载。

## 2. 历史校验本身的边界

`agent/app/history_guard.py` 是**句子级启发式**，不是语义证明：

- 它检查断言是否被 trajectory / revision / evidence 记录支撑，
  不是关键词黑名单；
- 它能识别未记录的内省过程、外部帮助缺失、`PARTIAL` 升级、证据数量概括、
  显式批注未映射、无历史时的否认/反推；
- 它**不能**保证覆盖所有同义改写。例如把「自己推导」换成更隐蔽的说法，
  或把外部资料使用写成不触发词表的表述，仍可能漏检。
- 它只对**显式个人复盘**生效；普通知识问题不缓冲、不校验。
  如果用户用非常规措辞问历史，可能不触发 `_is_history_review`，从而不做校验。

当前保护是「发现问题 → 一次受限重生成 → 仍不合格则确定性回退」。
重生成最多一次，不会无限重试；回退文本只来自记录，不含内部 Evidence ID。

### 代价

显式复盘的首字延迟从「第一个 token」推迟到「完整回答生成完」；
如果触发一次重生成，约等于两次完整生成。普通对话不受影响。

## 3. 前端未实现

`flutter/ails_flutter` 仍是 `flutter create` 的计数器模板：

- 没有登录、空间、对话、SSE 消费、批注、复习页面；
- `pubspec.yaml` 没有业务依赖；
- 不能把「Flutter 前端」写进已完成能力。

**封版更新（2026-11-08）：** `web/` 的 Vue 3 客户端已闭环，是官方推荐客户端
（登录守卫、SSE 对话、文档 / 记忆 / 学习状态管理）。它仍未实现注册页与学习空间
管理页，这两步暂用 API 完成。Postman / curl 仅用于 API 调试；`flutter/` 保持脚手架。

## 4. 业务能力缺口

| 能力 | 现状 | 影响 |
| --- | --- | --- |
| 文档导出 / 导入 / 学习计划 | Controller 返回 501 | 表结构存在，但没有可用接口 |
| 测试作答判分 | 长度 + 是否点到概念名的启发式 | 不是语义判分，可能误判 |
| Memory 检索 | MySQL FULLTEXT + Qdrant 向量 + RRF 融合 + 回源复核 | 向量路依赖 Agent / Qdrant，不可用时降级为词法路并返回 `DEGRADED_*` |
| Memory 收敛 | `run_type=MEMORY` 枚举存在但无调用方 | 长期记忆不会自动收敛 |
| 分析异步化 | inline 与聊天共用 `chat-stream-*` 线程池 | 突发分析会挤占回答线程 |
| Session 归属 | 文本命中近似 | 不是「与 Session 内 Evidence 的 Concept 交集」 |
| 复习调度 | 硬编码状态映射 | 不是真正的调度算法 |

## 5. 检索与引用限制

- 文档 RAG 检索基线是 `top_k=5`、`min_score=0.35`，没有 Reranker、没有文档侧
  Hybrid Search；Memory 检索已使用 FULLTEXT + 向量 + RRF（见第 4 节），两者的
  限制不能互相套用；
- Embedding 默认 `hash` 只能用于开发；真实语义检索必须显式用 `openai`；
- 引用校验是格式 + 上下文标签 + 词面来源支持，不是事实正确性证明；
- 来源支持度是同义改写敏感的词面启发式，会误报；
- 越界 / 残缺来源标签会在流式生成阶段被丢弃（`citation_filter.py`），
  被 Token 预算裁剪的片段不能作为有效来源；这解决的是**标签可核对性**，
  不是「回答内容一定由来源支持」；
- **语义误述仍是开放风险**：检索成功但片段被裁剪时，模型仍可能用自然语言说
  「没有检索到资料」。Runtime 不做关键词拦截，也不改写模型原文；该行为以
  strict XFAIL 记录，不能写成「已解决」；
- 过滤丢弃标注后，标注前的空白已经发送、无法回撤，可能出现轻微排版痕迹；
- 5.4 的文档引用 E2E 只有两篇合成文档、每篇 1 个 chunk；28 / 0 证明的是
  链路和标签可回跳，不是多 chunk 排序、干扰片段或事实正确性；
- RAG 评测的通过率不能直接当成「回答正确率」，也不能把 v1 / v2 合并。

## 6. 数据库与部署限制

- 表结构用 `schema.sql` 手工维护，没有 Flyway / Liquibase；
- `CREATE TABLE IF NOT EXISTS` 不会给已有表加列；升级必须按顺序手工执行
  `schema-migration-document-rag.sql` → `schema-migration-rag-reliability.sql` →
  `schema-migration-memory-fulltext.sql` → `schema-migration-phase7-truncation.sql` →
  `schema-migration-phase3-citations.sql` → `schema-migration-phase54-message-seq.sql`，
  或清库重建。漏跑前置脚本分别在对应列 / 表上报 `Unknown column` 或 `Unknown table`；
  `phase3-citations` 必须晚于 `phase7-truncation`（它使用 `AFTER truncated`）；
- `schema-dev-reset.sql` 会 DROP 数据，只能在开发库使用；
- `application-local.yml` 与 `agent/.env` 含本机凭证，绝不能提交；
- 当前没有多实例部署的分布式协调；SSE 连接、分析线程池都是单进程假设；
- RabbitMQ 只有本地 compose，没有生产级高可用 / 监控 / 告警。

## 7. 测试口径限制

- 2026-11-08 全量：Agent **437 passed / 9 skipped / 1 xfailed**；Spring **388 条
  （300 执行通过 / 88 跳过 / 0 失败）**。跳过项是真实 MySQL / Docker / 外部服务的
  环境门控，不等于用例通过。更早的 E2E 101 / MQ E2E 27 / E2E 94 / Demo 23
  是 2026-10-27 的 Mock 结果；
  Spring 服务端 E2E 47/0、重复采样 3 × 47/0 是 2026-10-30 的真实模型结果；
  5.4 另有文档引用 28/0、MQ 19/0（及 15/4 失败样本）、并发 3 × 10/0；
- 1 个 xfail 是语义误述风险，不是通过，也不是普通跳过；
- 真实 Qdrant、真实 broker、真实 MySQL 集成测试的环境门控 skip 计入上述
  9 + 88 条；
  5.4 的文档引用 / MQ E2E 是另起真实服务验证，**不改变这些 skip 的状态**；
- `AnalysisMqIntegrationTest` 默认 0 个用例执行；5.4 MQ E2E 另起真实 broker + Worker
  验证，但它不等于 Testcontainers 集成测试，也没有接进 CI；
- `python -m evaluation --mode all` 本轮未重跑；49/49 是 2026-10-21、`34b59b5` 的历史结果；
- `scripts/regress-closed-loop.sh` 本轮未重跑；
- GitHub Actions CI 本轮未触发；
- Flutter 测试未执行。

## 8. 数据与演示限制

- 演示必须显式标明 LLM 是 Mock；Mock 输出是确定性的假输出，不能展示成真实模型能力；
- 真实模型演示需要本地 Key，且结果不稳定；当前没有把预录的成功回答冒充实时生成；
- 演示数据用「新用户 + 新空间」隔离，不依赖手工改库；但测试库会累积历史用户 / 空间；
- `phase53-setup*.sh` 创建的密码是固定的 `Passw0rd!23`，仅用于本地测试，不能用于任何真实环境。

## 9. 不要做的事

- 不要为了让演示好看而修改真实失败样本或删除历史报告；
- 不要把 Mock 结果写成真实模型验收；
- 不要把环境门控 skip 写成通过；
- 不要把不同时间、不同评分版本、不同测试语义的通过率算成一个总体提升率；
- 不要在没有 Key 的情况下继续重试真实模型调用。
