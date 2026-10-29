# Phase 4：真实 RAG 端到端验证与来源可追溯性（计划与实际执行口径）

- 日期：2026-10-28
- 范围：Spring 文档上传 → 异步索引 → FastAPI 解析/切块 → 真实 Embedding →
  MySQL Chunk + 真实 Qdrant Point → READY → 用户提问 → 权限过滤 → 向量检索 →
  Context Builder → 真实 LLM
- 原则：只用合成数据；真实 Qdrant + 真实 Embedding；没有用 hash embedding、
  Mock LLM 或测试替身冒充通过；未执行/被跳过的不计 PASS；不提交 Git。

## 1. 开始前检查

- 工作树在开始前已有大量未提交修改（`agent/`、`backend/`、`evaluation/`、
  `docs/` 等）。本轮没有回滚、覆盖或提交这些修改。
- 真实调用路径以当前工作树为准，不假设“上次报告的配置仍然有效”。实际读取了
  `DocumentService`、`DocumentIndexService`、`DocumentMapper`、
  `AgentDocumentClient`、Agent `documents/routes.py`、`QdrantDocumentStore`、
  `KnowledgeSearchService`、`ContextBuilder`、`AgentRuntime`、`citations.py`。
- 后端与 Agent 分别用隔离端口/隔离数据库/隔离 Qdrant prefix 启动；没有改动
  生产配置或原有 collection。

## 2. 实际调用链与代码位置

| 步骤 | 实际实现位置 | 观察到的输入/输出 |
| --- | --- | --- |
| 上传入口 | `backend/.../controller/DocumentController.java`；`DocumentService.upload`（约 L100） | 校验 Space 归属；落盘；插入 `document` 行，状态 `UPLOADING`；提交异步索引；返回真实状态 |
| 异步索引 | `DocumentIndexService.submit`（L76）、`runIndex`、`complete`（L134）、`fail`（L151） | `submit` 先 `markProcessing`；`runIndex` 调 Agent parse → 校验 chunk → 替换 MySQL chunk → Agent index → 校验 vector ref → `complete(READY)`；异常 → `fail(FAILED)` |
| 就绪白名单 | `DocumentMapper.selectReadyIndexedIds`（L51） | 条件：`READY`、未删除、`indexed_at`/`embedding_model` 非空、`chunk_count > 0`、`chunk_count ==` 有 `vector_ref` 的 chunk 数；可选模型精确匹配 |
| 解析/切块 | `agent/app/documents/routes.py` `/agent/documents/parse`（L31）；`agent/app/documents/parser.py::parse_document`（L43） | 严格 UTF-8；拒绝空/NUL/非 md/txt；切块返回 chunk 列表 |
| 索引 | `agent/app/documents/routes.py` `/agent/documents/index`（L67）；`QdrantDocumentStore.index_document`（L153） | 用真实 Embedding 逐批向量化；`ensure_collection(model, dimension)`；按 `document_id` 先删旧 point，再 upsert；point ID 由 `(document_id, chunk_index)` 的 UUIDv5 生成 |
| Qdrant payload | `QdrantDocumentStore.index_document` L182-L193 | `user_id`、`space_id`、`document_id`、`chunk_id`、`chunk_index`、`document_name`、`file_name`、`file_type`、`content`、`metadata` |
| 查询白名单 | `agent/app/retriever/http_backend.py::get_knowledge`（L144）；后端 `/ready-ids`（L155） | Agent 先取后端 READY 文档 ID；失败/空白名单 fail closed，不退化无过滤搜索 |
| 向量检索 | `agent/app/retriever/knowledge.py::KnowledgeSearchService.search`；`QdrantDocumentStore.search_knowledge`（L226） | 查询 embedding → Qdrant filter `user_id AND space_id AND document_id IN allowlist` → top-k → `min_score` → payload 再校验 |
| 上下文组装 | `agent/app/context/builder.py` | 优先级：user input 100、history 90、session 85、understanding 70、trajectory 68、memory 50、knowledge status 45、knowledge 40、evidence 20；超预算按优先级丢弃 |
| 回答与引用 | `agent/app/runtime.py::prepare`（L476）、`generate`（L272）、`validate_citations`（L402）；`agent/app/citations.py::validate_citations`（L217） | 真实 LLM 输出；引用标签必须与**实际进入上下文且未被丢弃**的 `资料《文件名》#序号` 精确匹配 |
| Guard | `agent/app/evidence_guard.py::GUARD_STATUSES`；`runtime.generate` L294-L307 | 仅 `NO_VALID_DOCUMENTS` / `NO_MATCH` / `THRESHOLD_FILTERED` / `DEGRADED` 且没有知识片段时追加说明；`SUCCESS` 不追加 |

## 3. 合成数据设计

所有内容都是虚构的 ORBIT-4721 调度规范；没有真实用户数据。

| 角色 | 用户/空间 | 文件 | 唯一事实/标记 | 预期 |
| --- | --- | --- | --- | --- |
| A1 目标 | A / A1 | `orbit_current_*.md` | `P4-TARGET-7K2`，当前重试窗口 **17 秒**，校验种子 `0x7F3A` | READY；应被当前值问题召回 |
| A1 相似干扰 | A / A1 | `orbit_legacy_*.md` | `P4-DISTRACTOR-3M8`，旧调度器 **12 秒**，已下线 | READY；可被历史问题召回，不能当现行值 |
| A2 同用户其他空间 | A / A2 | `orbit_archive_*.md` | `P4-OTHERSPACE-5Q1`，同样写 17 秒 | READY；A1 检索必须排除 |
| B1 其他用户 | B / B1 | `orbit_shared_*.md` | `P4-OTHERUSER-8N4`，同样写 17 秒 | READY；A 的检索必须排除 |
| FAILED | A / A1 | `phase4_failed_*.md` | `P4-FAILED-6H9`，值 83 | 状态 FAILED；无 chunk/point；不得进 ready-ids |
| PROCESSING | A / A1 | `phase4_processing.md` | `P4-PROCESSING-2V6`，值 41 | 状态 PROCESSING；无 chunk/point；不得进 ready-ids |
| 无匹配查询 | A / A1 | 无 | 问镓的熔点 | 不应把 ORBIT 文档当依据 |

**实际最终运行 ID（2026-10-28 01:24:03 起）：**

| 对象 | ID |
| --- | --- |
| A 用户 / B 用户 | 3 / 8 |
| A1 / A2 / B1 空间 | 3 / 12 / 13 |
| 目标文档 / chunk | 20 / 18 |
| 干扰文档 / chunk | 21 / 19 |
| A2 文档 / chunk | 22 / 20 |
| B1 文档 / chunk | 23 / 21 |
| FAILED / PROCESSING 文档 | 2 / 3 |
| Qdrant collection | `ails_phase4_e2e_text-embedding-v4_1024` |

FAILED 状态是**故障注入**：在 Agent 停止时上传文档，后端得到“Agent 服务未启动或
无法连接”，落 `FAILED`。PROCESSING 状态是**中断注入**：用一个假 TCP 服务挂住
parse 请求，再杀掉后端；后端重启后该行仍停在 PROCESSING、`chunk_count=0`。
它不是正常运行中的稳定状态，具体风险见失败分析。

## 4. 测试矩阵

### A. 索引/生命周期

- 4 个 READY 文档：状态、`chunk_count`、`indexed_at`、`embedding_model`、
  `vector_ref` 非空；
- MySQL chunk 数与 Qdrant point 数一致；
- Qdrant payload 的 `user_id`、`space_id`、`document_id`、`chunk_id`、
  `chunk_index`、`document_name`、`file_name`、`content` 与 MySQL/文档一致；
- collection 维度 1024，与 Embedding 维度一致；
- FAILED/PROCESSING：0 chunk、0 point、不进 ready-ids；
- ready-ids 只返回各自空间 READY 文档。

### B. 检索/排序

- 5 条各自空间内有标注的合成查询（3 条 A1、1 条 A2、1 条 B1），K=5；
  跨用户/跨空间另做 Qdrant 防御性直查；
- 计算 Recall@5 与 MRR；记录每条候选的分数与名次；
- 无匹配查询记录候选数与低于阈值的证据；
- 不调整 `min_score=0.35`、`top_k=5`，不使用临时阈值。

### C. 权限/状态

- 用户 A 的 A1 检索不能返回 B1/A2 文档；
- Qdrant 层用“A 用户 + A1 空间 + B 文档 ID”等交叉组合做防御性直查；
- 非 READY 文档不进入 ready-ids，且没有 point；
- 最终 Context 的注入 chunk 只来自本空间 READY 白名单。

### D. 来源可追溯

- 每个注入 chunk 都能映射到真实 `document_id` / `chunk_id` / 文件名；
- 引用标签必须对应实际注入且未丢弃的 chunk；
- 不存在 chunk、未检索 chunk、未注入 chunk 的引用算非法；
- 合法引用只证明“标注对得上”，不证明事实为真。

### E. Context Builder 与真实模型

- `/agent/context/preview`：支持问题的知识 section 未被丢弃；无匹配时出现状态说明；
- 长历史预算压力：检查知识是否被丢弃、是否仍告知“检索成功”；
- 来源标签与真实 chunk 的映射；
- 用户问题/历史顺序与优先级；
- 5 个真实模型用例 + 1 个补充“错误前提”用例，走 Spring SSE；
- 硬断言：SSE 完整、非空回答、引用语法/合法性、引用 chunk 在最终上下文、
  授权范围；语义正确性只做人工观察，不冒充已校准质量。

## 5. 隔离与清理

- 数据库：`ails_phase4_e2e`（本轮创建，22 张表）；只含合成用户/空间/文档；
- Qdrant：`ails_phase4_e2e_text-embedding-v4_1024`（本轮创建）；
- 文件存储：`backend/backend/target/phase4-documents`（本轮创建）；
- 端口：后端 `127.0.0.1:8081`、Agent `127.0.0.1:8000`；均为本轮进程；
- 不删除原有 collection：`ails_phase41_real_e2e_text-embedding-v4_1024`、
  `ails_phase42_real_e2e_text-embedding-v4_1024`、`space_isolation_test`。

**实际清理结果：** 删除 `ails_phase4_e2e` 数据库、删除本轮 Qdrant collection、
删除本轮存储目录与临时合成文件；清理后 Qdrant 只剩上述三个原有 collection，
8000/8081 无监听。清理前该库只有 6 条合成文档记录（4 READY + 1 FAILED +
1 PROCESSING）。清理只删除本轮资源，没有删除原有 collection。

## 6. 结果文件与判定规则

- 原始机器可读证据：`evaluation/phase4/reports/20261027_172403/`
  - `manifest.json`、`index_results.json`、`retrieval_results.json`、
    `context_results.json`、`agent_results.json`、`summary.json`
  - `supplemental_context.json`、`supplemental_agent.json`
- 执行脚本：`evaluation/phase4/run_rag_e2e.py`、
  `supplemental_context_probe.py`、`supplemental_agent_probe.py`。
- PASS：硬断言全部通过，且实际调用了真实 Qdrant/Embedding/LLM；
- FAIL：硬断言失败；
- BLOCKED：真实服务不可用或没有有效输出；
- SKIPPED / NOT_COVERED：未执行，不计 PASS；
- 语义质量：没有人工评分时一律 `UNREVIEWED`，不写成 PASS。

## 7. 本轮不覆盖的内容

- 多 chunk/长文档、批量 Embedding 边界（本轮每个文档 1 个 chunk，batch=10
  的批量边界没有被真正触发）；
- 大规模 Recall/MRR、不同 Embedding 模型/阈值校准；
- 并发、吞吐、重排器、混合检索；
- 生产数据、真实用户数据、生产 Qdrant/DB；
- 语义质量人工评审（仍为 0 条人工评分）。
