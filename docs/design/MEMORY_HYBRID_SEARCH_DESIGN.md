# Memory 混合检索设计（FULLTEXT + Outbox + Qdrant + RRF）

- 审查对象：`D:\learningMatiarials\AI-Learning-Space`
- 分支 / 提交：`main` / `8490c1c`
- 审查日期：2026-11-07
- 本轮性质：**只读审查 + 设计**。未修改任何业务代码，未新增迁移脚本，未提交 Git。
- 交付物：本文件 `docs/design/MEMORY_HYBRID_SEARCH_DESIGN.md`
- 适用前提：MySQL 8.0（现有迁移脚本以 8.0.37 验证）、Spring Boot 3.3.5 / Java 17、
  Agent 侧 FastAPI + `qdrant-client`。

---

## 0. 结论摘要

### 0.1 一句话结论

当前 Memory 检索是**单路 LIKE 关键词匹配**（`MemoryService.search`），没有全文索引、
没有向量召回、没有融合；`memory.embedding_ref` 是空列，Qdrant 里没有任何 Memory 向量。
本设计在**不引入新中间件**的前提下，用「MySQL FULLTEXT + Transactional Outbox + Agent 侧
Qdrant 索引 + RRF 融合」补齐双路召回，并明确：**MySQL 始终是事实源，向量库只是可重建索引**。

### 0.2 关键设计决策

| # | 决策 | 选择 | 主要理由 | 被否决的替代方案 |
| --- | --- | --- | --- | --- |
| D1 | 向量读写归属 | 仍由 **Python Agent** 持有 Qdrant；Spring 通过内部 HTTP 调用 | 与现有 `AgentDocumentClient`、`QdrantDocumentStore`、Embedding Provider 保持一套实现；Spring 没有 Qdrant 客户端，也没有 Embedding 调用链 | Spring 直连 Qdrant + 自建 Embedding 客户端：等于把向量基础设施复制成两套，破坏「Agent 管认知/向量、Backend 管业务数据」的边界 |
| D2 | 一致性模型 | **Transactional Outbox**，Relay 用 `@Scheduled` 轮询 | 数据库写入与事件写入同一事务；不依赖 Kafka/Debezium（本轮约束） | 事务内直接调 Agent：长事务 + 外部调用不可回滚 + Agent 故障会拖垮用户写操作 |
| D3 | 索引范围 | 只索引 **ACTIVE** 记忆 | 与检索语义一致（两条路都只认 ACTIVE）；CANDIDATE 不消耗 Embedding 成本；归档即删点 | 索引全部状态：候选记忆产生无意义的向量与存储，且必须在检索侧反复过滤 |
| D4 | 向量召回返回什么 | 只返回 `memoryId + score + contentHash`，**不返回内容** | 内容永远从 MySQL 回源读取，杜绝陈旧副本、越权 payload 进入 LLM prompt | 直接返回 payload 中的 content：快，但事实源变成 Qdrant |
| D5 | 融合算法 | **RRF，k=60**，只看排名 | 关键词分数与余弦相似度量纲不同，RRF 不需要跨路归一化；缺一路天然降级 | 分数加权：需要归一化，对分布敏感，难调 |
| D6 | 对外接口 | **新增** `GET /api/memories/search/hybrid`，旧 `/search` 行为不变 | Agent 侧的调用契约不被破坏；可灰度切换 | 原地改 `/search` 返回结构：破坏现有调用方与测试 |
| D7 | 降级语义 | 显式状态 `OK / DEGRADED_VECTOR / DEGRADED_KEYWORD / UNAVAILABLE / BLANK_QUERY` | 不能把「检索不可用」伪装成「没有相关记忆」 | 失败返回空列表：与真的没有结果无法区分 |

### 0.3 本轮边界

- **只做设计**：不写生产代码、不改 `schema.sql`、不建迁移脚本。
- **不引入中间件**：不使用 Kafka / Debezium / Elasticsearch；Relay 基于 Spring `@Scheduled`。
- **不做 Reranker / 交叉编码器**：RRF 之后直接截断。
- **不做 Memory 的物理删除**：归档仍是终态，`DELETE` 语义不变。

---

## 1. 设计前提：当前真实实现

### 1.1 写入链

```text
HTTP 层
  POST   /api/memories          MemoryController.create  → MemoryService.create        (ACTIVE)
  PATCH  /api/memories/{id}     MemoryController.update  → MemoryService.update        (可改 status/content/scope)
  DELETE /api/memories/{id}     MemoryController.archive → MemoryService.archive       (仅 ACTIVE/CANDIDATE → ARCHIVED)

Agent 写入链
  MQ 分析结果 → AgentAnalysisWriteService.reconcileMemories
              → MemoryService.reconcileCandidate
              → insertCandidate(CANDIDATE) / updateCandidate(content only)

持久化
  MemoryMapper extends BaseMapper<Memory>（无自定义 SQL）
  MyBatis-Plus @Version + OptimisticLockerInnerInterceptor
  MySQL memory 表
```

关键代码位置：

| 层 | 文件 | 说明 |
| --- | --- | --- |
| Controller | `backend/backend/src/main/java/com/ails/backend/controller/MemoryController.java:50-60` | `GET /search` 目前是单路关键词 |
| Service | `MemoryService.java:78` `create` | 用户创建，直接 ACTIVE |
| Service | `MemoryService.java:94` `createCandidate` | Agent 候选，CANDIDATE |
| Service | `MemoryService.java:139` `reconcileCandidate` | Agent 意图收敛（ADD/UPDATE/IGNORE） |
| Service | `MemoryService.java:165` `insertCandidate` | 新候选落库 |
| Service | `MemoryService.java:192` `updateCandidate` | 只改 CANDIDATE 内容 |
| Service | `MemoryService.java:313` `search` | 当前检索：LIKE + 可见性 |
| Service | `MemoryService.java:359` `keywordTerms` | 切词 + 中文二元组 |
| Service | `MemoryService.java:395` `update` | 通用更新，可推进状态 |
| Service | `MemoryService.java:431` `archive` | 归档，ARCHIVED 幂等短路 |
| Service | `MemoryService.java:493` `persist` | 创建路径的最终落库 |
| Service | `MemoryService.java:594` `applyStatusTransition` | 状态迁移唯一入口 |
| Entity | `Memory.java` | `@TableName("memory")`、`@Version version`、`embeddingRef`（未使用） |
| DDL | `schema.sql:307-333` | `content TEXT`、`status ENUM`、`uk_memory_identity` |
| 乐观锁 | `config/MybatisPlusConfig.java:31` | 已注册 `OptimisticLockerInnerInterceptor` |

### 1.2 当前检索链

`MemoryService.search` 的行为，必须作为新方案的语义基线：

1. `user_id = :userId`、`status = ACTIVE`；
2. 可见性（设计文档 §二十一）：
   - `GLOBAL`：任意空间可见；
   - `SPACE`：`learning_space_id = :spaceId`；
   - `CONCEPT`：`learning_space_id = :spaceId AND concept_id = :conceptId`；
   - 未传 `spaceId` 时不做可见性过滤（返回该用户全部 ACTIVE）；
3. 关键词：按空白/标点切词，中文补相邻二元组，最多 16 个词，`OR` 连接 `LIKE '%term%'`；
4. 排序：`importance DESC, updated_at DESC`；
5. 命中任意词即返回，没有相关性分数。

Python 侧调用方：`agent/app/retriever/http_backend.py:121-145`，把用户原话截前 60 字作为
`keyword`，取前 `memory_top_k`（默认 5）条注入上下文。

### 1.3 向量基础设施现状

| 项 | 现状 | 位置 |
| --- | --- | --- |
| Qdrant 归属 | 只有 Python Agent 持有客户端 | `agent/app/vectorstore/qdrant_store.py:86-112` |
| collection 命名 | `collection_name_for(prefix, model, dimension)` → `ails_documents_<model>_<dim>` | `qdrant_store.py:79-83` |
| Embedding | `EmbeddingProvider` 接口 + hash / openai-compatible 实现 | `agent/app/embedding/` |
| 指纹 | `v1:sha256(provider|model|dimension|endpoint_identity)` | `agent/app/embedding/base.py` |
| 文档检索 | `KnowledgeSearchService.search`：白名单 → 查询向量 → Qdrant → 阈值 | `agent/app/retriever/knowledge.py:83` |
| 载荷索引 | `user_id/space_id/document_id/chunk_id/index_version/doc_version` | `qdrant_store.py:35-42` |
| Spring → Agent | `AgentDocumentClient`，`X-Internal-Token` + `X-Internal-User-Id` + `X-Internal-Identity` | `service/AgentDocumentClient.java:44-80` |
| Memory 向量 | **完全不存在**；`embedding_ref` 从未写入 | 全仓库检索确认 |

### 1.4 必须保持的不变量

这些是现有系统已经正确、本设计不得破坏的部分：

| # | 不变量 | 依据 |
| --- | --- | --- |
| I1 | 状态机 3×3 矩阵：`ARCHIVED` 是终态，`ACTIVE → CANDIDATE` 禁止 | `MemoryStatus.java:36-47`、`MemoryService.java:594-611` |
| I2 | `uk_memory_identity(user_id, scope_key, memory_key)` 是身份唯一性兜底 | `schema.sql:329` |
| I3 | 可见性由 `spaceId/conceptId` 推导，调用方不能直接指定 `scope_key` | `MemoryService.java:463-490` |
| I4 | 归属校验：跨用户资源一律 404 | `MemoryService.ownedMemory`、`resolveScope` |
| I5 | 乐观锁：`updateById` 返回 0 必须显式判错 | `MemoryService.java:561-565` |
| I6 | MySQL 是事实源；向量库可重建 | `docs/delivery/INTERVIEW-DEADLOCK-DEEP-DIVE.md` §7.2 |
| I7 | 旧接口 `GET /api/memories/search` 的返回结构与语义不变 | `MemoryController.java:50`、Agent `http_backend.py` |

### 1.5 本设计要新增的东西（一览）

| 层 | 新增 | 类型 |
| --- | --- | --- |
| MySQL | `memory.content` 的 FULLTEXT ngram 索引 | DDL |
| MySQL | `outbox_event` 表 | DDL |
| Spring | `MemoryOutboxPublisher`、`OutboxRelayWorker`、`OutboxEvent`/Mapper | 新增类 |
| Spring | `AgentMemoryClient`（调 Agent 记忆向量接口） | 新增类 |
| Spring | `HybridSearchService` + RRF + 回源复核 | 新增类 |
| Spring | `GET /api/memories/search/hybrid` | 新增端点 |
| Spring | `ails.memory.search.*` / `ails.memory.outbox.*` 配置 | 配置 |
| Agent | `/agent/memories/sync`、`/agent/memories/search` | 新增路由 |
| Agent | `QdrantMemoryStore`、`MemoryIndexer`、`MemoryVectorSearch` | 新增模块 |
| Agent | `ails_memories_<model>_<dim>` collection | 数据 |

---

## 2. 总体设计与数据流

### 2.1 组件图

```text
┌──────────────────────────── Spring Boot ────────────────────────────┐
│                                                                     │
│  MemoryController                                                   │
│    PATCH/POST/DELETE ──► MemoryService ──┐                          │
│                                           │ 同一 @Transactional      │
│                                           ▼                          │
│                                   ┌───────────────┐                  │
│                                   │ memory 表      │                  │
│                                   │ outbox_event  │                  │
│                                   └───────┬───────┘                  │
│                                           │ 轮询（@Scheduled）        │
│                                   OutboxRelayWorker                  │
│                                           │                          │
│                     ┌─────────────────────┴─────────────┐            │
│                     ▼                                   ▼            │
│            AgentMemoryClient                    （失败则退避重试）     │
│                     │                                               │
│  HybridSearchService│  GET /api/memories/search/hybrid              │
│    ├─ 词法路：MySQL FULLTEXT ──────────────┐                          │
│    └─ 向量路：AgentMemoryClient ──┐        │                          │
│                                   │        ▼                          │
│                              RRF 融合 + MySQL 回源复核                │
└───────────────────────────────────┼─────────────────────────────────┘
                                    │ HTTP（内部凭据）
┌───────────────────────────────────▼─────────────────────────────────┐
│                          Python Agent                                │
│  /agent/memories/sync   → Embedding Provider → QdrantMemoryStore     │
│  /agent/memories/search → Embedding Provider → Qdrant query_points   │
│                                                                      │
│  collection: ails_memories_<model>_<dimension>                       │
│  point id:   uuid5("ails-memory:{memoryId}")                         │
└──────────────────────────────┬───────────────────────────────────────┘
                               ▼
                           Qdrant
```

### 2.2 写入链（最终一致）

```text
用户/Agent 写 Memory
  │
  ├─ BEGIN
  │    ├─ INSERT/UPDATE memory
  │    └─ INSERT outbox_event（PENDING，payload = 写后快照）
  │  COMMIT
  │
  └─ 事务提交后（Relay 下一次轮询，默认 2s）
       ├─ 领取事件（PENDING → PROCESSING，attempt_count += 1）
       ├─ POST /agent/memories/sync（UPSERT / DELETE）
       │    ├─ 成功 → DONE
       │    └─ 失败 → PENDING + next_attempt_at 指数退避
       └─ 超过 max_attempts → DEAD + 告警日志
```

**为什么事件必须在同一事务里写**：Memory 写成功但事件丢失 → 向量索引永久落后；
事件写了但 Memory 回滚 → 索引出现一行不存在的内容。Outbox 与业务写共享同一个
InnoDB 事务，两种偏差都不会发生。

**为什么 payload 存快照而不是只存 id**：若只存 id，Relay 处理时再读库，会读到
「更新的内容」，导致事件语义漂移，且无法重放历史状态。快照让事件自包含、可重放。

### 2.3 检索链

```text
GET /api/memories/search/hybrid?spaceId=&conceptId=&keyword=&limit=
  │
  ├─ 并行（各自有超时，互不阻塞）
  │    ├─ 词法路：MemoryMapper.searchFulltext(userId, spaceId, conceptId, query, recallK)
  │    │            → List<MemoryLexicalHit{id, score}>（按 score DESC, importance DESC...）
  │    └─ 向量路：AgentMemoryClient.search(query, userId, spaceId, conceptId, recallK)
  │                 → List<MemoryVectorHit{memoryId, score, contentHash}>
  │
  ├─ RRF 融合：score(d) = Σ 1/(60 + rank_i(d))
  │
  ├─ 回源复核：SELECT ... WHERE id IN (:ids) AND user_id=:uid AND status='ACTIVE'
  │             AND <可见性谓词>；丢弃查不到的行
  │
  └─ 截断到 limit，返回 status + items + 每路排名
```

### 2.4 一致性模型与可接受的时间窗口

| 阶段 | 不一致窗口 | 影响 | 兜底 |
| --- | --- | --- | --- |
| Memory 已提交、事件未处理 | 默认 ≤ 2s（Relay 间隔），故障时更长 | 向量路暂不召回新内容；词法路已能召回（正文在 MySQL） | 双路 + RRF 保证不会「完全检索不到」 |
| 内容已更新、向量仍是旧版 | 同上 | 向量召回基于旧文本排序 | 回源返回 MySQL 最新内容；`contentHash` 不一致时记录并补发事件 |
| 已归档、向量点未删 | 同上 | 向量路可能召回归档项 | 回源复核 `status='ACTIVE'` 直接丢弃 |
| Agent/Qdrant 不可用 | 持续 | 只剩词法路 | `DEGRADED_VECTOR`，显式标记 |

结论：**最终一致是可接受的**，因为任何时刻检索都先经过 MySQL 复核，陈旧索引只影响
排序质量，不会把错误内容带进 prompt。

---

## 3. 数据库变更

### 3.1 `memory.content` 的 FULLTEXT ngram 索引

```sql
ALTER TABLE `memory`
    ADD FULLTEXT INDEX `ft_memory_content` (`content`) WITH PARSER ngram;
```

**为什么是 ngram**：MySQL 默认 FULLTEXT 分词按空格切分，中文整句会变成一个 token，
基本不可用。ngram 解析器按 `ngram_token_size`（默认 2）切二元组，中文才有召回能力。

**前置检查（部署前必须执行）**：

```sql
SHOW VARIABLES LIKE 'ngram_token_size';   -- 期望 2
SELECT VERSION();                          -- 期望 8.0.x
```

- `ngram_token_size` 在**建索引时**决定分词方式；同一张表的索引不会因之后修改参数而变化。
  多环境必须一致，否则同一段中文在不同环境召回不同。
- 单字查询（长度为 1）在 `ngram_token_size=2` 下**永远不命中**，这是 ngram 的硬限制。
- InnoDB 的 `ADD FULLTEXT INDEX` 不是零成本在线操作：表越大耗时越长，期间可能持有
  元数据锁/共享锁。**必须在维护窗口执行**，不要假设它完全 online。
- 索引体积：FULLTEXT + ngram 大致是正文的 1.5~3 倍（随词频变化）。`memory` 是小表，
  通常可忽略，但要在容量规划里显式记一笔。
- 索引随 `INSERT/UPDATE/DELETE` 同步维护，写放大存在；Memory 写入频率低，可接受。

**给新库的写法**（`schema.sql` 内，仅新库生效）：

```sql
CREATE TABLE IF NOT EXISTS `memory` (
    ...
    FULLTEXT KEY `ft_memory_content` (`content`) WITH PARSER ngram,
    ...
);
```

> 注意：`CREATE TABLE IF NOT EXISTS` 对已存在的表不生效，所以**老库必须执行迁移脚本**。
> `schema.sql` 每次启动都会跑，但不能依赖它给老表补索引。

### 3.2 `outbox_event` 表

```sql
CREATE TABLE IF NOT EXISTS `outbox_event` (
    `id`              BIGINT       NOT NULL AUTO_INCREMENT,
    -- 事件类型：MEMORY_UPSERT / MEMORY_DELETE
    `event_type`      VARCHAR(32)  NOT NULL,
    -- 聚合类型/ID：当前只有 MEMORY，预留给后续其他聚合
    `aggregate_type`  VARCHAR(32)  NOT NULL DEFAULT 'MEMORY',
    `aggregate_id`    BIGINT       NOT NULL COMMENT 'memory.id',
    -- 自包含快照：Relay 不依赖处理时的库内状态
    `payload`         JSON         NOT NULL,
    `status`          ENUM ('PENDING', 'PROCESSING', 'DONE', 'DEAD')
                                   NOT NULL DEFAULT 'PENDING',
    `attempt_count`   INT          NOT NULL DEFAULT 0,
    `max_attempts`    INT          NOT NULL DEFAULT 8,
    -- 下一次允许被领取的时间；指数退避写在这里
    `next_attempt_at` DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    -- 租约：PROCESSING 超过 lease 未完成会被回收
    `locked_by`       VARCHAR(64)           DEFAULT NULL,
    `locked_at`       DATETIME(3)           DEFAULT NULL,
    `last_error`      VARCHAR(500)          DEFAULT NULL COMMENT '只存错误摘要，不存堆栈/内容',
    -- 仅用于可重复执行的回填脚本；正常写入为 NULL
    `dedup_key`       VARCHAR(191)          DEFAULT NULL,
    `created_at`      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    `updated_at`      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                                   ON UPDATE CURRENT_TIMESTAMP(3),
    `completed_at`    DATETIME(3)           DEFAULT NULL,
    PRIMARY KEY (`id`),
    -- 回填脚本重复执行不会产生重复事件；MySQL 唯一索引允许多行 NULL
    UNIQUE KEY `uk_outbox_dedup` (`dedup_key`),
    -- 领取扫描：按状态 + 到期时间 + id 顺序
    KEY `idx_outbox_due` (`status`, `next_attempt_at`, `id`),
    -- 同聚合排序 / 排他检查
    KEY `idx_outbox_aggregate` (`aggregate_type`, `aggregate_id`, `id`),
    -- 租约回收扫描
    KEY `idx_outbox_lease` (`status`, `locked_at`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;
```

设计说明：

- **不建外键**。Outbox 是基础设施表，不应该被业务表的外键约束拖住；而且 Memory 永不物理删除。
- **`payload` 用 JSON 而不是 TEXT**：可用 `JSON_EXTRACT` 做人工排查，也能在脚本里构造。
- **`dedup_key` 只服务回填**：正常写入每次都是真实的新事件，不需要去重；回填脚本可能被
  重复执行，用 `backfill:{memoryId}` 兜底。
- **`PROCESSING` 不是终态**：租约到期会回到 `PENDING`，对应进程崩溃/重启。
- **索引顺序**：`(status, next_attempt_at, id)` 让「到期且待处理」的扫描走覆盖范围最小的区间。

### 3.3 迁移脚本

新增 `backend/backend/src/main/resources/schema-migration-memory-hybrid-search.sql`，
沿用现有 `schema-migration-rag-reliability.sql` 的风格：手工执行、`information_schema`
守卫、可重复执行、不删数据。

脚本结构：

```sql
-- =============================================================
-- AI Learning Space — Memory 混合检索：FULLTEXT + Outbox
--
-- 用途：把【已有库】升级到混合检索所需结构。
-- 本脚本不会被 spring.sql.init 执行，必须手工运行：
--     mysql -uroot -p ails < schema-migration-memory-hybrid-search.sql
--
-- 执行前提：
--   1. 备份 memory：mysqldump -uroot -p ails memory > backup-memory.sql
--   2. 维护窗口执行（ADD FULLTEXT INDEX 可能持锁并耗时）
--   3. MySQL 8.0，确认 SHOW VARIABLES LIKE 'ngram_token_size' 为 2
--
-- 幂等性：
--   - 索引/表均先查 information_schema，已存在则跳过；
--   - 回填事件用 dedup_key 唯一键保证重复执行不产生重复事件；
--   - DDL 会隐式提交，中途失败修复后可继续。
-- =============================================================

SET NAMES utf8mb4;
DROP PROCEDURE IF EXISTS `ails_migrate_memory_hybrid_search`;
DELIMITER $$

CREATE PROCEDURE `ails_migrate_memory_hybrid_search`()
BEGIN
    -- 1. FULLTEXT ngram 索引
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.statistics
         WHERE table_schema = DATABASE()
           AND table_name = 'memory'
           AND index_name = 'ft_memory_content'
    ) THEN
        ALTER TABLE `memory`
            ADD FULLTEXT INDEX `ft_memory_content` (`content`) WITH PARSER ngram;
    END IF;

    -- 2. outbox_event（结构与 schema.sql 完全一致）
    CREATE TABLE IF NOT EXISTS `outbox_event` ( ... );

    -- 3. 已有 ACTIVE 记忆回填索引事件（可重复执行）
    INSERT IGNORE INTO `outbox_event`
        (`event_type`, `aggregate_type`, `aggregate_id`, `payload`,
         `status`, `next_attempt_at`, `dedup_key`)
    SELECT
        'MEMORY_UPSERT', 'MEMORY', m.id,
        JSON_OBJECT(
            'memoryId', m.id,
            'userId', m.user_id,
            'content', m.content,
            'contentHash', SHA2(m.content, 256),
            'memoryVersion', m.version,
            'scopeType', m.scope_type,
            'scopeKey', m.scope_key,
            'learningSpaceId', m.learning_space_id,
            'conceptId', m.concept_id,
            'memoryType', m.memory_type,
            'status', m.status,
            'importance', m.importance,
            'updatedAt', DATE_FORMAT(m.updated_at, '%Y-%m-%dT%H:%i:%s')
        ),
        'PENDING', NOW(3),
        CONCAT('backfill:', m.id)
    FROM `memory` m
    WHERE m.status = 'ACTIVE';
END$$

DELIMITER ;
CALL `ails_migrate_memory_hybrid_search`();
DROP PROCEDURE IF EXISTS `ails_migrate_memory_hybrid_search`;
```

回填注意：

- 回填只覆盖 `ACTIVE`。CANDIDATE 不索引，符合 D3。
- 回填事件会一次性涌入；Relay 按批处理（默认 100/批，2s 间隔），
  1 万条记忆大约需要 3~4 分钟，期间向量路逐步补齐。
- 若希望「先确认管道通了再回填」，可先不执行第 3 步，改由运维手工触发。

### 3.4 `schema.sql` 同步

新库需要两处改动：

1. `memory` 表 `CREATE` 中加 `FULLTEXT KEY ... WITH PARSER ngram`；
2. 文件末尾加 `CREATE TABLE IF NOT EXISTS outbox_event (...)`。

> `spring.sql.init.mode: always` 每次启动都会执行 `schema.sql`，所以**代码先于迁移脚本**
> 上线时，`outbox_event` 会被自动创建，不会出现「写事件时表不存在」。FULLTEXT 索引则
> 不会补到老表上，必须靠迁移脚本 —— 这也是为什么全文路要设计成**可失败降级**：
> 索引没建时 `MATCH` 会报错 `Can't find FULLTEXT index matching the column list`，
> 混合检索必须捕获并退回单路。

---

## 4. 核心组件设计

### 4.1 `MemoryService` 改造

> 说明：需求里写的 `saveMemory` / `updateMemory` 在本仓库中不存在。实际入口是
> `create` / `createCandidate` / `reconcileCandidate` / `update` / `archive`，设计按真实方法名落地。

改造目标：**所有会改变「可检索状态」的写路径，在同一事务内追加一条 Outbox 事件。**

新增依赖（构造注入，`@RequiredArgsConstructor` 自动生成）：

```java
private final MemoryOutboxPublisher outboxPublisher;
```

新增一个「写前快照」值对象，用来判断是否真的需要发事件：

```java
/** 参与索引的字段快照：只有这些字段变化才需要更新向量。 */
private record IndexedSnapshot(
        String content, MemoryScopeType scopeType, String scopeKey,
        Long learningSpaceId, Long conceptId, MemoryType memoryType,
        Float importance, MemoryStatus status) {

    static IndexedSnapshot of(Memory m) { ... }
}
```

#### 4.1.1 各入口的事件规则

| 入口 | 写前状态 | 写后状态 | 索引字段是否变化 | 事件 |
| --- | --- | --- | --- | --- |
| `create` | 无 | ACTIVE | 是（新建） | `MEMORY_UPSERT` |
| `createCandidate` / `insertCandidate` | 无 | CANDIDATE | — | **无**（不索引候选） |
| `updateCandidate`（Agent） | CANDIDATE | CANDIDATE | 可能 | **无** |
| `update` | CANDIDATE | ACTIVE | 是 | `MEMORY_UPSERT` |
| `update` | ACTIVE | ACTIVE | 是 | `MEMORY_UPSERT` |
| `update` | ACTIVE | ACTIVE | 否（无实际变化） | **无** |
| `update` | ACTIVE | ARCHIVED | 是（状态） | `MEMORY_DELETE` |
| `update` | CANDIDATE | ARCHIVED | — | **无** |
| `archive` | ACTIVE | ARCHIVED | 是 | `MEMORY_DELETE` |
| `archive` | CANDIDATE | ARCHIVED | — | **无** |
| `archive` | ARCHIVED | ARCHIVED | — | **无**（幂等短路） |
| `reconcileCandidate`（ADD，新建） | 无 | CANDIDATE | — | **无** |
| `reconcileCandidate`（UPDATE，命中 CANDIDATE） | CANDIDATE | CANDIDATE | — | **无** |

「索引字段变化」= `content / memoryType / importance / scopeType / scopeKey /
learningSpaceId / conceptId` 中任意一项变化。**注意 `status` 不单独触发 ACTIVE→ACTIVE
的事件**，因为状态没变；ACTIVE→ARCHIVED 单独走 `MEMORY_DELETE`。

#### 4.1.2 代码形态

```java
@Transactional
public MemoryResponse create(Long userId, MemoryRequest request) {
    Memory memory = persist(userId, request, MemoryStatus.ACTIVE);
    // persist 已返回带自增 id 的实体；事件与插入共用当前事务
    outboxPublisher.publishUpsert(memory);
    return MemoryResponse.from(memory);
}

@Transactional
public MemoryResponse update(Long userId, Long memoryId, MemoryRequest request) {
    Memory memory = ownedMemory(userId, memoryId);
    IndexedSnapshot before = IndexedSnapshot.of(memory);     // 写前快照
    MemoryStatus statusBefore = memory.getStatus();

    if (request.status() != null) {
        applyStatusTransition(memory, request.status());
    }
    requireEditable(statusBefore);

    ... // 现有字段合并、作用域解析、身份校验，保持不变

    requireUpdated(memoryMapper.updateById(memory), "记忆已被其他请求修改，请刷新后重试");

    // 关键：必须在 updateById 成功之后发事件。
    // updateById 返回 0 会抛 409 → 事务回滚 → 事件一起回滚，不会残留。
    publishIndexChange(before, memory);
    return MemoryResponse.from(memory);
}

@Transactional
public void archive(Long userId, Long memoryId) {
    Memory memory = ownedMemory(userId, memoryId);
    if (memory.getStatus() == MemoryStatus.ARCHIVED) {
        return;                                   // 幂等短路：不发事件
    }
    MemoryStatus before = memory.getStatus();
    applyStatusTransition(memory, MemoryStatus.ARCHIVED);
    requireUpdated(memoryMapper.updateById(memory), "记忆已被其他请求修改，请刷新后重试");
    if (before == MemoryStatus.ACTIVE) {          // 只有索引过的才需要删点
        outboxPublisher.publishDelete(memory);
    }
}

/** 统一决定 UPSERT / DELETE / 不发。 */
private void publishIndexChange(IndexedSnapshot before, Memory memory) {
    if (before.status() == MemoryStatus.ARCHIVED) {
        return;                                   // 理论上不可达（requireEditable 已挡）
    }
    if (memory.getStatus() == MemoryStatus.ARCHIVED) {
        if (before.status() == MemoryStatus.ACTIVE) {
            outboxPublisher.publishDelete(memory);
        }
        return;
    }
    if (memory.getStatus() != MemoryStatus.ACTIVE) {
        return;                                   // CANDIDATE → CANDIDATE：不索引
    }
    if (!IndexedSnapshot.of(memory).equals(before)) {
        outboxPublisher.publishUpsert(memory);
    }
}
```

`reconcileCandidate` / `insertCandidate` / `updateCandidate` / `createCandidate`
**一行都不用改**。原因是它们产出的状态永远是 `CANDIDATE`，按 D3 不进入索引；
等用户通过 `update` 确认成 `ACTIVE` 时，由 `publishIndexChange` 补发 `UPSERT`。

#### 4.1.3 事务边界检查

| 调用场景 | 事务归属 | 事件何时可见 |
| --- | --- | --- |
| `MemoryController.create` | `MemoryService.create` 自己开启 | 方法返回并提交后 |
| `MemoryController.update/archive` | 自己开启 | 同上 |
| `AgentAnalysisWriteService.reconcileMemories` | 外层分析事务（`REQUIRED` 参与） | 外层提交后 |
| 未来新增的批量写 | 必须加入同一事务 | — |

**不要在 Publisher 上使用 `REQUIRES_NEW`**：那会让事件先于业务数据提交，Relay 可能读到
一条「指向还没提交的记忆」的事件。默认 `REQUIRED` 是正确的。

### 4.2 事件类型与 payload

```java
public enum MemoryEventType {
    MEMORY_UPSERT,   // 新建 ACTIVE / ACTIVE 内容或作用域变化
    MEMORY_DELETE    // ACTIVE → ARCHIVED
}
```

`payload`（JSON，自包含快照）：

```json
{
  "memoryId": 42,
  "userId": 7,
  "content": "用户偏好用递归解释分治",
  "contentHash": "9f2c...",
  "memoryVersion": 3,
  "scopeType": "SPACE",
  "scopeKey": "SPACE:9",
  "learningSpaceId": 9,
  "conceptId": null,
  "memoryType": "LEARNING",
  "status": "ACTIVE",
  "importance": 0.8,
  "updatedAt": "2026-11-07T12:34:56"
}
```

- `MEMORY_DELETE` 只需要 `memoryId / userId`，但统一携带完整快照便于排查。
- `contentHash` 用于检索阶段检测「向量索引是否落后于 MySQL」。算法固定为
  **SHA-256 of UTF-8 原始 content**：Java 用 `Hashing.sha256Hex(content)`，
  迁移脚本用 `SHA2(content, 256)`，两者对同一字符串必须得到相同结果
  （不要先做 `Hashing.normalize`，否则回填与运行时的事件无法比对）。
- **payload 不记录 Embedding 模型**：索引使用 Agent **当前生效**的 Embedding 配置。
  模型切换后需要重建索引（风险 R10）。

### 4.3 `OutboxRelayWorker`

#### 4.3.1 调度与开关

```java
@Slf4j
@Component
@RequiredArgsConstructor
public class OutboxRelayWorker {

    @Scheduled(
        fixedDelayString = "${ails.memory.outbox.interval-ms:2000}",
        initialDelayString = "${ails.memory.outbox.initial-delay-ms:10000}")
    public void relay() {
        if (!properties.enabled()) {
            return;
        }
        recoverExpiredLeases();
        int processed = 0;
        while (processed < properties.maxBatchPerTick()) {
            int n = relayOnce();
            processed += n;
            if (n < properties.batchSize()) {
                break;
            }
        }
    }
}
```

- 默认 `@Scheduled` 是**单线程**的，同名方法不会重入；这天然保证同一实例内的事件串行。
- `enabled` 默认 `true`；测试环境可关闭，避免后台线程干扰确定性断言。
- 单次 tick 最多处理 `maxBatchPerTick` 条，防止积压时长时间占用调度线程。

#### 4.3.2 领取（claim）

第一步：选出「到期的、且同聚合没有更早未完成事件」的候选。

```sql
SELECT e.id, e.event_type, e.aggregate_type, e.aggregate_id,
       e.payload, e.attempt_count, e.max_attempts
FROM outbox_event e
WHERE e.status = 'PENDING'
  AND e.next_attempt_at <= NOW(3)
  AND NOT EXISTS (
      SELECT 1
      FROM outbox_event p
      WHERE p.aggregate_type = e.aggregate_type
        AND p.aggregate_id   = e.aggregate_id
        AND p.id             < e.id
        AND p.status IN ('PENDING', 'PROCESSING')
  )
ORDER BY e.id
LIMIT #{batchSize}
```

> **head-of-line（同聚合有序）是正确性要求，不是优化。** 反例：事件 5（内容 A）先失败
> 进入退避，事件 6（内容 B）成功；若 5 之后被重试，会把索引写回 A，而 MySQL 里是 B。
> 只有「前一条未终态，后一条不许处理」才能避免这种倒退。

第二步：逐条条件更新领取，避免多实例重复处理。

```sql
UPDATE outbox_event
SET status = 'PROCESSING',
    locked_by = #{workerId},
    locked_at = NOW(3),
    attempt_count = attempt_count + 1
WHERE id = #{id}
  AND status = 'PENDING'
  AND next_attempt_at <= NOW(3)
```

返回 0 表示被别的实例抢走，直接跳过。这符合仓库既有的「条件状态 UPDATE 领取」
惯例（`AnalysisStaleRunCompensator`、`DocumentIndexRecoveryService`）。

> 多实例扩展：若未来部署多个后端实例，这套「候选查询 + 条件 UPDATE」即可工作，
> 不需要 `SELECT ... FOR UPDATE SKIP LOCKED`。`NOT EXISTS` 子查询在
> `idx_outbox_aggregate` 上走索引；上线前用 `EXPLAIN` 确认。

#### 4.3.3 处理

```java
private void process(OutboxEvent event) {
    MemorySyncRequest request = payloadMapper.toSyncRequest(event);
    try {
        AgentMemorySyncResponse response = agentMemoryClient.sync(request);
        requireValid(response);                       // 契约校验，防止“假成功”
        outboxMapper.markDone(event.id());
        log.info("Memory 索引事件完成 eventId={} type={} memoryId={}",
                 event.id(), event.type(), event.aggregateId());
    } catch (AgentPermanentException e) {
        // 401/403、4xx 契约错误：重试无意义，直接 DEAD 等人工介入
        outboxMapper.markDead(event.id(), truncate(e.getMessage(), 500));
        log.error("Memory 索引事件永久失败 eventId={} memoryId={}",
                  event.id(), event.aggregateId(), e);
    } catch (Exception e) {
        retryOrDead(event, e);
    }
}

private void retryOrDead(OutboxEvent event, Exception e) {
    int attempts = event.attemptCount() + 1;        // claim 时已 +1
    if (attempts >= event.maxAttempts()) {
        outboxMapper.markDead(event.id(), truncate(e.getMessage(), 500));
        log.error("Memory 索引事件耗尽重试 eventId={} attempts={}",
                  event.id(), attempts, e);
        return;
    }
    long delay = backoff(attempts);                 // 指数退避 + 抖动
    outboxMapper.markPending(event.id(), delay, truncate(e.getMessage(), 500));
}
```

**重试分类**：

| 失败 | 分类 | 处理 |
| --- | --- | --- |
| 连接超时 / 503 / 5xx | 可重试 | 指数退避 |
| 429 | 可重试 | 尊重 `Retry-After`（若提供），否则退避 |
| 401 / 403 | 永久 | 直接 DEAD，需运维修配置后手工重放 |
| 400 / 422（契约不合法） | 永久 | 直接 DEAD，通常是版本不匹配 |
| 响应体缺字段 / 类型错误 | 可重试一次，再永久 | 防止 Agent 半升级 |
| Qdrant 写入失败 | 可重试 | Agent 返回 5xx |

**退避公式**：

```text
delay = min(baseMs * 2^(attempt-1), maxMs) ± 20% 抖动
baseMs = 2000，maxMs = 300000（5 分钟），maxAttempts = 8
```

8 次大约覆盖 2s → 4s → 8s → 16s → 32s → 64s → 128s → 256s，合计约 8.5 分钟。
超过后进入 `DEAD`，保留 payload 供人工重放：

```sql
-- 修复 Agent/配置后，把死信重新排队
UPDATE outbox_event
SET status='PENDING', attempt_count=0, next_attempt_at=NOW(3),
    locked_by=NULL, locked_at=NULL, last_error=NULL
WHERE status='DEAD' AND id = ?;
```

#### 4.3.4 租约回收

```sql
UPDATE outbox_event
SET status = 'PENDING', locked_by = NULL, locked_at = NULL,
    next_attempt_at = NOW(3)
WHERE status = 'PROCESSING'
  AND locked_at < NOW(3) - INTERVAL #{leaseSeconds} SECOND
```

- 租约默认 120s，必须大于一次 `sync` 的最坏耗时（Agent 索引超时 + 网络）。
- 回收后 `attempt_count` 保持已累加值，避免「崩溃 → 立即重试 → 再崩溃」无限循环。
- 回收是**幂等**的：同一事件可能被处理两次，Agent 侧 upsert/delete 必须幂等（见 4.4）。

#### 4.3.5 幂等性

| 层面 | 保证 |
| --- | --- |
| 重复投递 | Agent 的 point id 由 `memoryId` 确定性生成，`upsert` 覆盖同一 point |
| 重复删除 | Qdrant 按 `memory_id` 删除，不存在时视为成功 |
| 重复领取 | 条件 `UPDATE ... WHERE status='PENDING'` 保证只有一个实例成功 |
| 顺序 | 同聚合 head-of-line；跨聚合无序无所谓 |
| 丢事件 | 不会：事件与业务写同事务；Relay 只在 DONE/DEAD 后停止 |

#### 4.3.6 清理

```sql
-- 每天一次：删除 7 天前完成的事件
DELETE FROM outbox_event
WHERE status = 'DONE'
  AND completed_at < NOW(3) - INTERVAL 7 DAY
LIMIT 1000;   -- 分批，避免长事务
```

`DEAD` **不自动删除**，保留到人工处理完；运维可定期导出后清理。

### 4.4 Agent 侧记忆向量接口

#### 4.4.1 路由

新增 `agent/app/memories/routes.py`，前缀 `/agent/memories`，复用
`require_internal_user`（`agent/app/internal_auth.py`），并校验
`payload.userId == internal_user_id`（与 `documents/routes.py:54` 同一模式）。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/agent/memories/sync` | UPSERT / DELETE 单条记忆向量 |
| POST | `/agent/memories/search` | 查询向量 → Qdrant 过滤检索 |

#### 4.4.2 同步请求/响应

```json
// POST /agent/memories/sync
{
  "eventId": 9001,
  "operation": "UPSERT",
  "memoryId": 42,
  "userId": 7,
  "content": "用户偏好用递归解释分治",
  "contentHash": "9f2c...",
  "memoryVersion": 3,
  "scopeType": "SPACE",
  "learningSpaceId": 9,
  "conceptId": null,
  "memoryType": "LEARNING",
  "importance": 0.8
}
```

```json
// 200
{ "code": 0, "data": { "pointId": "0b1e...", "collection": "ails_memories_hash-v1_256",
                        "skipped": false } }
```

- `operation=DELETE` 时只使用 `memoryId / userId`，其余字段可空。
- Agent 侧用**当前** Embedding 配置计算向量；`eventId` 只用于日志串联。
- `memoryVersion` 写入 payload，便于排查「索引落后几个版本」。
- **命名约定**：HTTP 请求/响应体是 camelCase（与 `CamelModel` 一致）；
  写入 Qdrant 的 payload 是 snake_case（与文档向量 payload 一致）。两者不要混用。

#### 4.4.3 collection 与 point

```python
# 复用现有命名规则
collection = collection_name_for("ails_memories", provider.model, provider.dimension)
# 例：ails_memories_hash-v1_256 / ails_memories_text-embedding-3-small_1536
```

```python
POINT_NAMESPACE = uuid.UUID("...固定的项目命名空间...")

def point_id_for_memory(memory_id: int) -> str:
    return str(uuid.uuid5(POINT_NAMESPACE, f"ails-memory:{memory_id}"))
```

- **确定性 point id**：同一记忆永远落在同一个点，`upsert` 是覆盖，重试不会产生重复。
- **collection 按模型+维度隔离**：切换 Embedding 不会把不同向量空间混在一起
  （与文档索引同一套约定）。
- 单条写入不需要批处理；若未来支持批量回填，复用 `qdrant_upsert_batch_size`。

#### 4.4.4 payload 与索引

```json
{
  "memory_id": 42,
  "user_id": 7,
  "scope_type": "SPACE",
  "space_id": 9,
  "concept_id": null,
  "memory_type": "LEARNING",
  "importance": 0.8,
  "content_hash": "9f2c...",
  "memory_version": 3,
  "embedding_fingerprint": "v1:sha256(...)"
}
```

需要建 payload 索引的字段（过滤用）：

| 字段 | 类型 | 理由 |
| --- | --- | --- |
| `user_id` | INTEGER | 强制隔离 |
| `scope_type` | KEYWORD | 可见性分支 |
| `space_id` | INTEGER | SPACE/CONCEPT 过滤 |
| `concept_id` | INTEGER | CONCEPT 过滤 |
| `status` | KEYWORD | 纵深防御（理论上只有 ACTIVE） |

`content` **不写入 payload**：检索只需要 id 与分数，正文从 MySQL 回源。这既避免
陈旧内容，也避免把用户长期记忆复制到向量库（降低数据面）。

#### 4.4.5 检索

```python
async def search(self, *, user_id, space_id, concept_id, query, top_k):
    vector = await provider.embed([query])          # 单条查询，失败抛依赖错误
    collection = collection_name_for("ails_memories", provider.model, provider.dimension)

    must = [
        FieldCondition(key="user_id", match=MatchValue(value=user_id)),
        FieldCondition(key="status",  match=MatchValue(value="ACTIVE")),
    ]
    should = []
    if space_id is not None:
        should = [
            Filter(must=[FieldCondition(key="scope_type", match=MatchValue(value="GLOBAL"))]),
            Filter(must=[FieldCondition(key="scope_type", match=MatchValue(value="SPACE")),
                         FieldCondition(key="space_id", match=MatchValue(value=space_id))]),
        ]
        if concept_id is not None:
            should.append(Filter(must=[
                FieldCondition(key="scope_type", match=MatchValue(value="CONCEPT")),
                FieldCondition(key="space_id", match=MatchValue(value=space_id)),
                FieldCondition(key="concept_id", match=MatchValue(value=concept_id)),
            ]))
    query_filter = Filter(must=must, should=should or None)

    hits = await client.query_points(
        collection_name=collection, query=vector, query_filter=query_filter,
        limit=top_k, with_payload=["memory_id", "content_hash"], with_vectors=False)
    return [MemoryVectorHit(memory_id=p["memory_id"], score=s, content_hash=p.get("content_hash"))
            for s, p in ...]
```

要点：

- `space_id is None` 时**不加 scope 过滤**，与现有 `MemoryService.search` 的语义一致。
- `should` 为空的 Qdrant `Filter` 等于不过滤，必须显式传 `None` 而不是空列表
  （否则不同客户端版本语义不一致）。
- **不做 `min_score` 阈值**：RRF 只用排名；阈值会把「唯一能召回的那条」删掉。
  若线上发现噪声过多，用可配置的 `memory_min_score`（默认 0.0）兜底。
- 每次检索一次 Embedding 调用；预算 2s，超时由 Spring 侧 `HttpClient` 控制。
- collection 不存在 → 返回空列表 + `warnings=["memory collection 未建立"]`，
  **不要**在检索路径里隐式建集合（会掩盖索引失败）。

#### 4.4.6 维度/指纹校验

写入前与文档索引一致：

1. `ensure_collection` 校验维度与距离函数（COSINE），不一致直接抛错；
2. 检索命中后校验 `embedding_fingerprint`（payload 内），不等于当前指纹的候选丢弃并告警；
3. 维度不一致时**绝不静默写入**。

### 4.5 `HybridSearchService`

#### 4.5.1 对外接口

```java
public record HybridMemorySearchResponse(
        MemoryHybridStatus status,
        List<HybridMemoryHit> items,
        List<String> warnings,
        long elapsedMs) {}

public record HybridMemoryHit(
        MemoryResponse memory,
        double rrfScore,
        Integer lexicalRank,   // 1-based；未命中为 null
        Integer vectorRank,    // 1-based；未命中为 null
        Double vectorScore) {}
```

```java
@GetMapping("/search/hybrid")
public ApiResponse<HybridMemorySearchResponse> hybridSearch(
        @RequestParam(required = false) Long spaceId,
        @RequestParam(required = false) Long conceptId,
        @RequestParam(required = false) String keyword,
        @RequestParam(required = false) Integer limit) {
    return ApiResponse.ok(hybridSearchService.search(
            SecurityUtil.currentUserId(), spaceId, conceptId, keyword, limit));
}
```

**为什么新增端点而不是改旧端点**：旧端点被 Python Agent 直接调用（`http_backend.py:138`），
返回结构是 `List<MemoryResponse>`。改成带 `status/ranks` 的结构会破坏契约。新端点可以
先灰度、再在 Agent 侧切换。

#### 4.5.2 并行执行

Java 17 没有虚拟线程，使用专用有界线程池：

```java
@Bean("memorySearchExecutor")
public TaskExecutor memorySearchExecutor(MemorySearchProperties p) {
    ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
    executor.setCorePoolSize(p.executorCoreSize());        // 4
    executor.setMaxPoolSize(p.executorMaxSize());          // 8
    executor.setQueueCapacity(p.executorQueueCapacity());  // 64
    executor.setThreadNamePrefix("memory-search-");
    executor.setRejectedExecutionHandler(new ThreadPoolExecutor.AbortPolicy());
    executor.initialize();
    return executor;
}
```

```java
CompletableFuture<List<MemoryLexicalHit>> lexicalFuture =
        CompletableFuture.supplyAsync(() -> lexicalSearch(...), executor)
                         .completeOnTimeout(List.of(), p.lexicalTimeoutMs(), MILLISECONDS)
                         .exceptionally(e -> { warn("词法路失败", e); return null; });

CompletableFuture<List<MemoryVectorHit>> vectorFuture =
        CompletableFuture.supplyAsync(() -> vectorSearch(...), executor)
                         .completeOnTimeout(List.of(), p.vectorTimeoutMs(), MILLISECONDS)
                         .exceptionally(e -> { warn("向量路失败", e); return null; });

List<MemoryLexicalHit> lexical = lexicalFuture.join();
List<MemoryVectorHit> vector = vectorFuture.join();
```

注意：`completeOnTimeout` 返回空列表后，底层任务仍在跑（HTTP 调用本身有超时，
会自行结束）；`AbortPolicy` 在队列满时抛 `RejectedExecutionException`，
必须捕获并降级为 `null`，不能让检索把请求线程打挂。

#### 4.5.3 词法路 SQL

```java
@Select("""
        <script>
        SELECT m.id,
               MATCH(m.content) AGAINST(#{query} IN NATURAL LANGUAGE MODE) AS score
        FROM memory m
        WHERE m.user_id = #{userId}
          AND m.status = 'ACTIVE'
          AND MATCH(m.content) AGAINST(#{query} IN NATURAL LANGUAGE MODE)
        <if test="spaceId != null">
          AND (
                m.scope_type = 'GLOBAL'
             OR (m.scope_type = 'SPACE'   AND m.learning_space_id = #{spaceId})
             <if test="conceptId != null">
             OR (m.scope_type = 'CONCEPT' AND m.learning_space_id = #{spaceId}
                                           AND m.concept_id = #{conceptId})
             </if>
          )
        </if>
        ORDER BY score DESC, m.importance DESC, m.updated_at DESC, m.id ASC
        LIMIT #{recallK}
        </script>
        """)
List<MemoryLexicalHit> searchFulltext(@Param("userId") Long userId,
                                      @Param("spaceId") Long spaceId,
                                      @Param("conceptId") Long conceptId,
                                      @Param("query") String query,
                                      @Param("recallK") int recallK);
```

`MemoryLexicalHit` 用 `@Results` 或 `map-underscore-to-camel-case` 映射 `id/score`。

**语义陷阱**：

- `MATCH` 的列必须与 FULLTEXT 索引列完全一致，否则报
  `Can't find FULLTEXT index matching the column list`。捕获该异常 → 词法路降级。
- `IN NATURAL LANGUAGE MODE` 会忽略停用词与长度 < `ngram_token_size` 的片段；
  查询为空或全是停用词时返回空集，这是正常的，不是故障。
- 中文长句会被切成大量二元组，召回偏宽；这正是要用 RRF 而不是阈值的原因。
- 排序里的 `importance/updated_at/id` 是**确定性 tie-break**，让同一查询的结果可复现。

#### 4.5.4 向量路

```java
public List<MemoryVectorHit> search(String query, Long userId, Long spaceId,
                                    Long conceptId, int recallK) {
    MemoryVectorSearchRequest request = new MemoryVectorSearchRequest(
            UUID.randomUUID().toString(), userId, spaceId, conceptId,
            truncate(query, 200), recallK);
    JsonNode data = send(baseUrl + MEMORY_SEARCH_PATH, request, vectorTimeout, "记忆向量检索");
    return parseHits(data);   // 契约校验：memoryId 必须为正整数、score 必须有限
}
```

复用 `AgentDocumentClient` 的 `HttpClient` 配置：HTTP/1.1、`connect-timeout-ms`、
`X-Internal-Token`、`X-Internal-Identity`（`InternalIdentitySigner.sign(userId)`）。
**不要复用 `parse/index` 的超时**（那是分钟级）；向量检索需要独立的 2s 级超时。

#### 4.5.5 RRF 融合

```java
private static final double DEFAULT_K = 60.0;

Map<Long, double[]> scores = new HashMap<>();   // [rrf, lexicalRank, vectorRank, vectorScore]
applyRanks(lexical, 1.0, k, scores, LEXICAL);
applyRanks(vector,  1.0, k, scores, VECTOR);

private void applyRanks(List<? extends Ranked> list, double weight, double k,
                        Map<Long, double[]> acc, Channel channel) {
    for (int i = 0; i < list.size(); i++) {
        long id = list.get(i).id();
        int rank = i + 1;
        acc.computeIfAbsent(id, x -> new double[]{0, -1, -1, Double.NaN});
        acc.get(id)[0] += weight / (k + rank);
        ...
    }
}
```

公式：

```text
rrf(d) = Σ_{通道 i} w_i / (k + rank_i(d))
k = 60，rank 从 1 开始，未命中该通道不贡献
```

示例（`w=1`）：

| 记忆 | 词法排名 | 向量排名 | RRF |
| --- | --- | --- | --- |
| B | 2 | 1 | 1/62 + 1/61 = 0.032522 |
| A | 1 | 3 | 1/61 + 1/63 = 0.032266 |
| D | — | 2 | 1/62 = 0.016129 |
| C | 3 | — | 1/63 = 0.015873 |

排序：`rrf DESC → importance DESC（null 最后）→ updatedAt DESC → id ASC`。
最后两级是为了**确定性**：相同 RRF 分数不能因为 HashMap 遍历顺序而抖动。

**为什么不用分数加权**：关键词分数是 MySQL 的相关性度量（无上界），向量是余弦
（[-1,1]）；直接加权必须先归一化，而归一化对分布敏感、跨数据集不可复用。
RRF 只用排名，天然跨量纲，且「缺一路就只加一路」，降级不需要特殊分支。

#### 4.5.6 回源复核（强制）

```sql
SELECT m.*
FROM memory m
WHERE m.id IN (:ids)
  AND m.user_id = :userId
  AND m.status = 'ACTIVE'
  AND ( m.scope_type = 'GLOBAL'
        OR (m.scope_type = 'SPACE'   AND m.learning_space_id = :spaceId)
        OR (m.scope_type = 'CONCEPT' AND m.learning_space_id = :spaceId
                                     AND m.concept_id = :conceptId) )
```

（`spaceId` 为空时去掉可见性谓词，与 `MemoryService.search` 一致。）

复核规则：

| 情况 | 处理 |
| --- | --- |
| 查不到 / `user_id` 不符 / 非 ACTIVE | **丢弃**，不计入结果 |
| 可见性不满足 | **丢弃** |
| `contentHash` 与向量命中返回的不一致 | 保留（正文以 MySQL 为准），记 `stale=true` 日志并补发一次 `UPSERT` |
| 同一 id 在两路重复 | RRF 已合并，只返回一条 |

**这一步是整套设计的安全底线**：只要 SQL 复核存在，Qdrant 里的任何陈旧、越权、
孤儿数据都不会进入 prompt。因此 Qdrant 的权限过滤是「优化」（减少无效召回），
SQL 复核才是「保证」。

#### 4.5.7 降级矩阵

| 词法路 | 向量路 | MySQL 复核 | status | 行为 |
| --- | --- | --- | --- | --- |
| 成功 | 成功 | 成功 | `OK` | RRF |
| 成功 | 失败/超时 | 成功 | `DEGRADED_VECTOR` | 只用词法排名 |
| 失败 | 成功 | 成功 | `DEGRADED_KEYWORD` | 只用向量排名 |
| 失败 | 失败 | — | `UNAVAILABLE` | 空列表 + warning |
| 成功/失败 | 成功/失败 | 失败 | `UNAVAILABLE` | 空列表（不能返回未复核数据） |
| 查询为空 | — | — | `BLANK_QUERY` | 按 `importance/updatedAt` 返回 MySQL 列表，不做融合 |

`BLANK_QUERY` 是对现有行为的兼容：旧 `search` 在 `keyword` 为空时返回全部 ACTIVE，
混合检索不能因为查询为空就返回空。

#### 4.5.8 参数与预算

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `recallK` | 50 | 每路召回上限 |
| `limit` | 10 | 最终条数；上限 50 |
| `rrfK` | 60 | RRF 常量 |
| `lexicalWeight` / `vectorWeight` | 1.0 / 1.0 | 预留给后续调权 |
| `lexicalTimeoutMs` | 1500 | MySQL 全文查询 |
| `vectorTimeoutMs` | 2000 | Agent 检索（含一次 Embedding） |
| `totalBudgetMs` | 2500 | 由两个 future 的 timeout 组合保证 |

延迟预期（单机、小数据量）：词法 10~50ms，向量 80~300ms；并行后 P95 约 300ms。
最坏情况 2.5s，由预算兜底。

---

## 5. 核心类 / 接口定义

### 5.1 Spring 侧

```text
com.ails.backend
├─ enums
│   ├─ MemoryEventType              MEMORY_UPSERT / MEMORY_DELETE
│   ├─ OutboxEventStatus            PENDING / PROCESSING / DONE / DEAD
│   └─ MemoryHybridStatus           OK / DEGRADED_VECTOR / DEGRADED_KEYWORD / UNAVAILABLE / BLANK_QUERY
├─ entity
│   └─ OutboxEvent                  @TableName("outbox_event")
├─ mapper
│   ├─ OutboxEventMapper            BaseMapper + 领取/领取确认/完成/重试/死信/回收 SQL
│   └─ MemoryMapper                 追加 searchFulltext / selectVisibleByIds
├─ service
│   ├─ MemoryOutboxPublisher        publishUpsert / publishDelete
│   ├─ OutboxRelayWorker            @Scheduled + claim + dispatch
│   ├─ AgentMemoryClient            sync / search（内部 HTTP）
│   ├─ HybridSearchService          search + RRF + 回源
│   └─ RrfFuser                     纯函数，便于单测
├─ config
│   ├─ MemorySearchProperties       @ConfigurationProperties("ails.memory.search")
│   ├─ MemoryOutboxProperties       @ConfigurationProperties("ails.memory.outbox")
│   └─ MemorySearchConfig           memorySearchExecutor
├─ dto/memory
│   ├─ HybridMemoryHit
│   ├─ HybridMemorySearchResponse
│   ├─ MemoryLexicalHit
│   └─ MemoryVectorHit
└─ controller
    └─ MemoryController             追加 GET /search/hybrid
```

### 5.2 关键接口（publisher / relay）

```java
@Component
@RequiredArgsConstructor
public class MemoryOutboxPublisher {

    private final OutboxEventMapper outboxEventMapper;
    private final ObjectMapper objectMapper;

    /** 必须由业务写事务调用；不要加 REQUIRES_NEW。 */
    public void publishUpsert(Memory memory) {
        enqueue(MemoryEventType.MEMORY_UPSERT, memory);
    }

    public void publishDelete(Memory memory) {
        enqueue(MemoryEventType.MEMORY_DELETE, memory);
    }

    private void enqueue(MemoryEventType type, Memory memory) {
        OutboxEvent event = new OutboxEvent();
        event.setEventType(type.name());
        event.setAggregateType("MEMORY");
        event.setAggregateId(memory.getId());
        event.setPayload(toPayloadJson(memory));       // 快照
        event.setStatus(OutboxEventStatus.PENDING.name());
        event.setAttemptCount(0);
        event.setMaxAttempts(properties.maxAttempts());
        event.setNextAttemptAt(LocalDateTime.now());
        outboxEventMapper.insert(event);
    }
}
```

> `publishUpsert` 里对 payload 的序列化**必须成功**：序列化失败应抛出并回滚业务写，
> 而不是吞掉。否则会出现「记忆存在但永远不会被索引」且无任何痕迹。

### 5.3 Agent 侧

```text
agent/app
├─ memories/
│   ├─ routes.py          POST /agent/memories/sync, POST /agent/memories/search
│   ├─ schemas.py         MemorySyncRequest / MemorySearchRequest / 响应模型
│   ├─ indexer.py         MemoryIndexer：content → embedding → upsert/delete
│   └─ search.py          MemoryVectorSearch：query → embedding → query_points
├─ vectorstore/
│   └─ memory_store.py    QdrantMemoryStore：ensure_collection / upsert / delete / search
└─ config.py              新增 memory_collection_prefix / memory_search_top_k /
                          memory_search_min_score / memory_search_timeout_seconds
```

```python
class QdrantMemoryStore:
    async def ensure_collection(self, *, model: str, dimension: int) -> str: ...
    async def upsert_memory(self, *, model: str, dimension: int,
                            point_id: str, vector: list[float],
                            payload: dict[str, Any]) -> None: ...
    async def delete_memory(self, *, model: str, dimension: int,
                            memory_id: int) -> None: ...
    async def search(self, *, model: str, dimension: int,
                     vector: list[float], user_id: int,
                     space_id: int | None, concept_id: int | None,
                     top_k: int) -> list[MemoryVectorHit]: ...
```

- `ensure_collection` 复用 `collection_name_for` 与现有的维度/距离校验逻辑；
  建议把 `QdrantDocumentStore._ensure_payload_indexes` 里的「按字段建索引」抽成
  模块级 helper，避免复制粘贴。
- `delete_memory` 用 `match memory_id` 过滤删除；`wait=True`，保证 Relay 收到成功
  时数据已落盘（否则「成功」后立即检索可能读不到）。

---

## 6. 关键流程（伪代码）

### 6.1 写入 + 事件（同一事务）

```text
BEGIN
  memory = INSERT/UPDATE memory ...
  IF 需要索引:
     INSERT outbox_event(type, aggregate_id, payload, status='PENDING')
COMMIT

-- 事务失败（唯一键冲突 / 乐观锁失败 / 任何异常）→ 事件随事务回滚
```

### 6.2 Relay 单轮

```text
1. 回收租约：PROCESSING 且 locked_at 过期 → PENDING
2. candidates = SELECT 到期的、同聚合无更早未完成事件的 PENDING（LIMIT batch）
3. FOR each candidate:
     claimed = UPDATE ... WHERE id=? AND status='PENDING'
     IF claimed == 0: continue          -- 别的实例拿走了
     TRY
        dispatch(event)
        UPDATE ... SET status='DONE'
     CATCH permanent:
        UPDATE ... SET status='DEAD'
     CATCH other:
        IF attempt_count >= max_attempts: DEAD
        ELSE: PENDING + backoff(next_attempt_at)
4. 返回处理条数；调用方决定是否再跑一轮
```

### 6.3 混合检索

```text
lexicalFuture = async fulltext(query, visibility, recallK)   timeout 1.5s
vectorFuture  = async agent.search(query, visibility, recallK) timeout 2s

lexical = lexicalFuture or null
vector  = vectorFuture  or null

IF lexical == null AND vector == null: return UNAVAILABLE
IF query blank: return BLANK_QUERY (MySQL list)

ranks = {}
for i, hit in lexical: ranks[hit.id] += 1/(60+i+1); lexicalRank[hit.id]=i+1
for i, hit in vector:  ranks[hit.id] += 1/(60+i+1); vectorRank[hit.id]=i+1

ids = top (recallK*2) by rrf score
mems = SELECT ... WHERE id IN ids AND user_id=? AND status='ACTIVE' AND visible

result = []
for id in sorted(ranks, by rrf, importance, updatedAt, id):
    if id in mems: result.append(hit(mem, rrf, ranks...))
    if len(result) == limit: break

status = OK / DEGRADED_VECTOR / DEGRADED_KEYWORD
```

### 6.4 Agent 向量检索

```text
vector = embedding_provider.embed([query])[0]
collection = collection_name_for("ails_memories", model, dimension)
must = [user_id == uid, status == "ACTIVE"]
should = (space_id is None) ? None : [GLOBAL, SPACE(space_id), CONCEPT(space_id, concept_id)]
hits = qdrant.query_points(collection, vector, filter=Filter(must, should),
                           limit=top_k, with_payload=["memory_id","content_hash"])
return hits
```

---

## 7. 风险点与对策

| # | 风险 | 表现 | 对策 | 残余风险 |
| --- | --- | --- | --- | --- |
| R1 | **双路可见性实现不一致**（SQL vs Qdrant filter） | 同一用户在两路看到的作用域集合不同，融合结果偏差 | 回源 SQL 复核是权威过滤；新增「可见性一致性」金标测试：对同一组 `(user, space, concept)` 断言两路候选 id 集合的可见性一致 | 向量路可能多召回一些随后被丢弃，浪费算力但不泄漏 |
| R2 | **Qdrant 陈旧/孤儿数据** | 已归档记忆仍被召回；内容已改但向量是旧的 | 回源 `status='ACTIVE'` + `contentHash` 比对；归档发 `DELETE`；定期对账 | 归档后的短窗口内仍会召回，但会被 SQL 丢弃 |
| R3 | **事件乱序** | 旧内容覆盖新内容 | 同聚合 head-of-line；Relay 单实例串行；`memoryVersion` 写 payload 便于排查 | 多实例下依赖 `NOT EXISTS` 查询的正确性，需 EXPLAIN 验证 |
| R4 | **重复投递** | 同一记忆被多次写向量 | 确定性 point id + `upsert`；删除幂等 | 无（结果幂等） |
| R5 | **Agent 不可用** | 向量路失败，只剩关键词 | `DEGRADED_VECTOR` + warning；HTTP 超时 2s；线程池隔离，不影响其他接口 | 检索质量下降，但不阻塞回答 |
| R6 | **FULLTEXT 索引未建/参数不一致** | `MATCH` 报错 → 词法路 500 | 捕获 `SQLException` 降级；启动时校验 `ngram_token_size`；迁移脚本前置检查 | 若词法路也失败 → `UNAVAILABLE` |
| R7 | **中文长句切出大量二元组，召回噪声大** | 无关记忆排名靠前 | RRF + 最终 limit；`importance` 作为 tie-break；后续可加 rerank | 首条可能不相关，需评测集验收 |
| R8 | **`ADD FULLTEXT INDEX` 锁表** | 迁移期间写入阻塞 | 维护窗口执行；先备份；表小 | 迁移窗口内写入延迟 |
| R9 | **写放大 / outbox 表膨胀** | 每次记忆写入多一次 INSERT | 与业务写同事务但表小；DONE 事件 7 天清理；`idx_outbox_due` 控制扫描 | 高频写入场景需观察 |
| R10 | **Embedding 模型/维度变更** | 新 collection 与旧 collection 并存，检索只查新集合 → 旧记忆召回为 0 | collection 名含 model+dimension；切换后必须重建：把所有 ACTIVE 记忆重新入队 | 重建完成前向量路缺失旧数据，词法路仍可用 |
| R11 | **越权（payload 伪造/过滤缺失）** | 别人的记忆进入 prompt | Agent 强制 `user_id` 过滤；后端回源再按 `user_id` + 可见性复核；内部凭据 + 身份签名 | 无（双保险） |
| R12 | **检索线程池打满** | `RejectedExecutionException` 冒到请求层 | `AbortPolicy` + 捕获后降级；池大小与队列容量可配；不与聊天/索引用同一池 | 高并发下退化为单路 |
| R13 | **回源 `IN` 列表过大** | SQL 计划退化 | RRF 后最多取 `2*recallK=100` 个 id；去重后回源 | 可控 |
| R14 | **归档后 `DELETE` 事件永久失败** | Qdrant 残留归档记忆 | 回源过滤保证不进入结果；DEAD 告警；对账任务清理 | 存储残留 |
| R15 | **Agent 响应契约漂移** | 字段改名导致解析失败 | `AgentMemoryClient` 严格校验；契约错误进入 DEAD 而不是静默跳过 | 需两端同步发布 |
| R16 | **旧 `/search` 与新的语义分叉** | Agent 仍走旧接口，看不到改进 | 分阶段切换；保留旧接口至少一个版本；文档显式标注 | 切换窗口内两套行为并存 |

### 7.1 重点风险的展开

**R3 事件乱序（最危险）**：

```text
t0  memory.content = A, 事件 #5 (A) 入队
t1  memory.content = B, 事件 #6 (B) 入队
t2  Relay 处理 #5 → Agent 超时，进入退避（PENDING，next=t2+4s）
t3  Relay 处理 #6 → 成功，Qdrant = B
t4  Relay 重试 #5 → 成功，Qdrant = A     ← 索引回退
```

head-of-line 让 #6 在 #5 未终态时**根本不会被领取**，从根上消除这个窗口。
代价是单条记忆的事件必须串行；Memory 写入频率低，完全可接受。

**R10 模型切换**：

```text
旧：ails_memories_hash-v1_256
新：ails_memories_text-embedding-3-small_1536
```

检索只查「当前 provider 的 collection」，旧集合里的点不会参与召回。切换操作必须是：

1. 改 Agent 配置并重启；
2. 执行「重建」脚本：`INSERT INTO outbox_event (...) SELECT ... FROM memory WHERE status='ACTIVE'`
   （`dedup_key = CONCAT('reindex:', id, ':', NOW())` 或先清空旧事件）；
3. 观察 Relay 把 pending 清零后再切流量；
4. 旧 collection 由运维确认后删除（不自动删，避免误删）。

---

## 8. 降级与可观测性

### 8.1 状态语义（必须进日志/响应）

| 状态 | 含义 | 调用方应如何呈现 |
| --- | --- | --- |
| `OK` | 双路成功 | 正常使用 |
| `DEGRADED_VECTOR` | 只有关键词 | 可提示「部分记忆检索不可用」 |
| `DEGRADED_KEYWORD` | 只有向量 | 同上 |
| `UNAVAILABLE` | 两路都失败或复核失败 | 不能表述为「没有相关记忆」 |
| `BLANK_QUERY` | 查询为空，返回列表 | 正常 |

Python 侧 `MemoryItem` 目前没有 `score` 字段；若 Agent 切到新端点，应新增
`MemorySearchResult` 包装 `status + items`，而不是把状态塞进 `MemoryItem`。

### 8.2 日志

```text
INFO  memory.hybrid.search userId=7 spaceId=9 status=OK lexical=12 vector=9 fused=15
      returned=8 elapsedMs=143 stale=1
INFO  memory.outbox.relay processed=12 pending=0 dead=0 elapsedMs=180
WARN  memory.hybrid.vector.failed cause=timeout elapsedMs=2000
ERROR memory.outbox.dead eventId=9001 memoryId=42 attempts=8 reason=401
```

约束：

- **不记录 memory content**（长期记忆可能包含隐私）。
- 记录 `memoryId / userId / eventId / 计数 / 耗时 / 状态` 足够定位问题。
- `last_error` 只存摘要（最多 500 字符），不存堆栈。

### 8.3 需要人工关注的信号

| 信号 | 阈值建议 | 含义 |
| --- | --- | --- |
| `outbox_event.status='PENDING'` 且 `next_attempt_at` 过期超过 5 分钟 | > 100 条 | Relay 停摆或 Agent 持续不可用 |
| `status='DEAD'` | > 0 | 需要人工重放 |
| `DEGRADED_VECTOR` 比例 | > 5% | Agent/Qdrant 稳定性问题 |
| `UNAVAILABLE` 比例 | > 0.1% | 严重故障 |
| 检索 P95 | > 1s | 需要检查 Embedding/Qdrant 延迟 |

当前项目没有 Actuator/Micrometer，建议先用**日志 + 一条内部诊断 SQL** 起步；
若引入指标栈，属于独立演进而非本设计的前置条件。

---

## 9. 测试清单

> 遵循仓库现有的「默认模式 / 门控全量模式」划分（`docs/audit/TEST_EVIDENCE_MODES.md`），
> 依赖 MySQL / Qdrant / Agent 的用例必须显式门控，不能在默认 `mvn test` 里静默变成 skip。

### 9.1 Spring 单元测试（默认模式可跑）

| # | 用例 | 断言 |
| --- | --- | --- |
| U1 | `RrfFuser`：两路各 3 条、无交集 | 分数与排名符合公式；顺序确定 |
| U2 | `RrfFuser`：同一 id 双路命中 | 分数为两路之和，`lexicalRank/vectorRank` 都非空 |
| U3 | `RrfFuser`：RRF 相同分数 | 按 `importance → updatedAt → id` 稳定排序 |
| U4 | `RrfFuser`：空列表 | 不抛异常，返回空 |
| U5 | `publishIndexChange`：CANDIDATE→ACTIVE | 发 `UPSERT` |
| U6 | `publishIndexChange`：ACTIVE→ARCHIVED | 发 `DELETE` |
| U7 | `publishIndexChange`：CANDIDATE→CANDIDATE | 不发 |
| U8 | `publishIndexChange`：ACTIVE→ACTIVE 无变化 | 不发 |
| U9 | `archive` 对已归档记忆 | 不写库、不发事件 |
| U10 | Publisher 序列化失败 | 抛出，调用方事务可回滚 |
| U11 | Relay 退避计算 | 2s 起、指数增长、封顶、带抖动范围 |
| U12 | Relay 永久错误分类 | 401/403/400 → DEAD，不重试 |
| U13 | `AgentMemoryClient` 响应解析 | 缺字段/类型错误 → 契约异常 |

### 9.2 Spring 集成测试（门控：MySQL）

| # | 用例 | 断言 |
| --- | --- | --- |
| I1 | `MemoryService.create` 后同一事务内 outbox 有 1 条 PENDING | 事务提交后可见 |
| I2 | `MemoryService.update` 触发 409（乐观锁） | 回滚；outbox 无新增 |
| I3 | `update` 把 ACTIVE 改内容 | 新增 UPSERT，payload 是改后内容 |
| I4 | `archive` ACTIVE | 新增 DELETE |
| I5 | `archive` CANDIDATE | 无事件 |
| I6 | `reconcileCandidate` ADD/UPDATE | 无事件 |
| I7 | `searchFulltext` 中文查询 | 命中预期记忆；`ngram_token_size` 不符时测试显式失败并提示 |
| I8 | `searchFulltext` 可见性矩阵 | GLOBAL/SPACE/CONCEPT 与 `MemoryService.search` 的可见集合一致 |
| I9 | `selectVisibleByIds` | 跨用户 id 被过滤；ARCHIVED 被过滤 |
| I10 | Relay 端到端（fake Agent server） | PENDING → DONE；重复执行不重复写 |
| I11 | Relay head-of-line | 事件 5 失败时事件 6 不被领取 |
| I12 | 租约回收 | PROCESSING 超时回到 PENDING，attempt 不重置 |

### 9.3 Agent 测试（门控：Qdrant）

| # | 用例 | 断言 |
| --- | --- | --- |
| A1 | `sync` UPSERT 两次 | 同 point id，count 仍为 1 |
| A2 | `sync` DELETE | 点被删除；重复删除成功 |
| A3 | `search` 过滤 | 只返回 `user_id` 匹配、可见作用域内的点 |
| A4 | `search` 无 `space_id` | 不加 scope 过滤，但仍按 user/status |
| A5 | 维度不一致 | `ensure_collection` 抛错，不写入 |
| A6 | fingerprint 不一致 | 候选被丢弃并告警 |
| A7 | collection 不存在 | 返回空 + warning，不自动建集合 |

### 9.4 端到端（门控：Agent + Qdrant + MySQL）

| # | 用例 |
| --- | --- |
| E1 | 创建 ACTIVE 记忆 → Relay → 向量检索能命中 |
| E2 | 改内容 → Relay → 旧内容不再命中（排序以新内容为准） |
| E3 | 归档 → Relay → 向量路不再返回，词法路也不返回 |
| E4 | 停掉 Agent → 混合检索返回 `DEGRADED_VECTOR`，词法结果仍正确 |
| E5 | 删掉 FULLTEXT 索引 → 返回 `DEGRADED_KEYWORD`，不 500 |

### 9.5 迁移脚本测试

| # | 用例 |
| --- | --- |
| M1 | 在空库上执行两次：不报错、不重复建索引/表 |
| M2 | 在已有 ACTIVE/CANDIDATE/ARCHIVED 数据的库上执行：只回填 ACTIVE |
| M3 | 重复执行：`dedup_key` 阻止重复事件 |
| M4 | `ngram_token_size != 2` 时：脚本前置检查失败并给出明确提示 |

### 9.6 评测（非阻塞）

用 20~50 条真实查询构造小评测集，记录：

- 词法单路 MRR / Recall@5
- 向量单路 MRR / Recall@5
- RRF 融合后的 MRR / Recall@5

**验收标准不应写成「RRF 一定更好」**：在数据量很小时，融合可能不如单路。
建议把「不劣于较好的单路」作为门槛，并保留 `lexicalWeight/vectorWeight` 调权能力。

---

## 10. 实施顺序（建议）

| 阶段 | 内容 | 可独立验证点 |
| --- | --- | --- |
| P0 | 迁移脚本 + `schema.sql` + FULLTEXT 索引 | 手工 `MATCH AGAINST` 能命中中文 |
| P1 | `OutboxEvent` / Mapper / Publisher + MemoryService 接入 | 单测 + MySQL 集成测试：事件与业务同事务 |
| P2 | Agent `/agent/memories/sync` + `QdrantMemoryStore` | Agent 单测 + Qdrant 集成 |
| P3 | `OutboxRelayWorker` + `AgentMemoryClient` | Relay 端到端（fake agent / 真 agent） |
| P4 | `HybridSearchService` + RRF + 回源 + 新端点 | 集成测试 + 手工对比新旧结果 |
| P5 | Agent 侧切换到 hybrid 端点（灰度） | 线上观察 `DEGRADED_*` 比例与延迟 |
| P6 | 调权 / 评测 / 考虑 rerank | 评测集报告 |

每个阶段都应满足：**默认模式测试全绿；门控测试在开启后真实执行**。

---

## 11. 明确不做的事

| 不做 | 原因 |
| --- | --- |
| Kafka / Debezium / Canal | 本轮约束；Outbox + 定时 Relay 已满足最终一致 |
| 引入 Java Qdrant 客户端 | 会让向量基础设施出现第二套实现；Agent 已是唯一入口 |
| Reranker / cross-encoder | 先验证召回质量，精排是下一步 |
| Memory 物理删除 | 与现有「归档是终态」语义冲突 |
| 修改旧 `GET /api/memories/search` | 破坏 Agent 契约；新端点灰度更安全 |
| 在 Qdrant payload 存正文 | 放大数据面、制造陈旧内容风险 |
| 自动删除旧 collection | 误删不可恢复；交给运维确认 |
| 分布式锁 / 选主 | 单实例 Relay + 条件 UPDATE 已够；多实例也无需额外中间件 |

---

## 附录 A：关键文件索引

| 主题 | 文件:行 |
| --- | --- |
| Memory 表 DDL | `backend/backend/src/main/resources/schema.sql:307-333` |
| 状态机 | `backend/backend/src/main/java/com/ails/backend/enums/MemoryStatus.java:36-47` |
| 写入 Service | `backend/backend/src/main/java/com/ails/backend/service/MemoryService.java:78,94,139,165,192,395,431` |
| 检索 Service | `MemoryService.java:313-383` |
| 状态迁移入口 | `MemoryService.java:594-611` |
| 乐观锁配置 | `backend/backend/src/main/java/com/ails/backend/config/MybatisPlusConfig.java:31` |
| Controller | `backend/backend/src/main/java/com/ails/backend/controller/MemoryController.java:50-60` |
| Spring → Agent 客户端 | `backend/backend/src/main/java/com/ails/backend/service/AgentDocumentClient.java:44-120` |
| Agent 内部鉴权 | `agent/app/internal_auth.py`、`agent/app/documents/routes.py:54` |
| Qdrant 文档存储 | `agent/app/vectorstore/qdrant_store.py:79-112,378-467` |
| Embedding | `agent/app/embedding/base.py`、`factory.py` |
| Agent 记忆检索调用方 | `agent/app/retriever/http_backend.py:121-145` |
| 定时任务惯例 | `backend/backend/src/main/java/com/ails/backend/mq/AnalysisStaleRunCompensator.java:59`、`service/DocumentIndexRecoveryService.java:50` |
| 迁移脚本风格 | `backend/backend/src/main/resources/schema-migration-rag-reliability.sql` |
| 双路检索既有论述 | `docs/delivery/INTERVIEW-DEADLOCK-DEEP-DIVE.md` §7.2 |

## 附录 B：与现有文档的关系

- 本设计补上了 `MEMORY_MINIMAL_FIX.md:8.4` 第 3 条「Memory 向量召回」缺口；
  **不改变**该文档定义的状态机与接口语义。
- 向量索引的可靠性约定（维度隔离、fingerprint、可重建、删除幂等）沿用
  `RAG_MINIMAL_RELIABILITY.md` 的结论，不重复设计。
- `KNOWN_LIMITATIONS.md` 中的 RAG-02（旧 collection 残留）同样适用于 Memory：
  切换 Embedding 后旧 `ails_memories_*` collection 需人工清理。
- 检索降级状态命名沿用 `INTERVIEW-DEADLOCK-DEEP-DIVE.md` §7.2 的
  `DEGRADED_VECTOR / DEGRADED_KEYWORD / UNAVAILABLE`。

---

## 附录 C：本轮未验证的事项（设计边界声明）

以下是设计成立所依赖、但**本次只读审查没有实际执行**的假设：

1. 目标环境的 `ngram_token_size` 为 2；
2. `memory` 表规模与 FULLTEXT 索引构建时间；
3. 现网 Qdrant 的可用性与 collection 现状（仓库内无运行数据）；
4. `NOT EXISTS` 领取查询在目标数据量下的执行计划；
5. Python Agent 与 Spring 的部署拓扑（是否同机、网络延迟）。

这些必须在实施阶段用真实环境验证，不能把本设计当作已验证结论。
