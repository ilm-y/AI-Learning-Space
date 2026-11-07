-- =============================================================
-- AI Learning Space — 表结构（对齐 database&API_design.md 的 22 张表）
--
-- 本脚本【只负责 CREATE】，不含任何 DROP，由 spring.sql.init 每次启动执行，
-- 全部为 CREATE TABLE IF NOT EXISTS，幂等。
--
-- 需要清库重建时执行同目录下的 schema-dev-reset.sql（只负责 DROP）：
--     mysql -uroot -p ails < schema-dev-reset.sql
--     mysql -uroot -p ails < schema.sql
--
-- 已有库升级不要清库：执行同目录下的 schema-migration-document-rag.sql
-- （手工、幂等；本脚本的 CREATE TABLE IF NOT EXISTS 不会给已有表加列）。
--
-- 核心模型（谁是 source of truth）：
--     Evidence        append-only，认知判断的原子依据，永不修改、不提供删除
--     Understanding   当前理解快照（可更新）+ 每次真正变化产生 Revision（历史）
--     Memory          Agent 长期上下文，用 CANDIDATE / ACTIVE / ARCHIVED 管生命周期
--     LearningState   Understanding.status 的 Materialized Projection，只读派生
--
-- 与文档列清单的五处刻意偏离（详见 README 第八节）：
--   1. message 加 UNIQUE(conversation_id, sequence_no) —— 防并发撞号
--   2. deleted_at 软删只覆盖用户内容（learning_space / conversation / message / concept）；
--      证据链不设软删、也不提供删除入口
--   3. misconceptions 语义收窄为「当前未解除」并存对象（JSON 列，不影响表结构）
--   4. learning_understanding 用 SELECT ... FOR UPDATE 而不是乐观锁（version 列保留但只作诊断）
--   5. memory.memory_key 可为 NULL，缺省由 content 哈希兜底（见该表注释）
--
-- 反过来，下面这些是 B 阶段「代码向文档靠齐」补上的、曾经缺失的列，不是偏离：
--   evidence_type / dedupe_key / first_seen_run_id / last_seen_run_id（learning_evidence）
--   scope_type / scope_key / version（memory，path 更名 memory_key）
--   session_id（conversation，可空）、goal（learning_session）、PAUSED（learning_session.status）
--   trace_id（agent_run）、run_type 枚举改名 EXTRACTION → ANALYSIS
--
-- 建表顺序有约束：conversation 有指向 learning_session 的外键，所以
-- learning_session（模块 13）必须排在 conversation（模块 4）之前，不能按模块号排。
--
-- 约定：枚举列直接使用 MySQL ENUM，Java 侧枚举常量名与 ENUM 值一致
--      （MyBatis-Plus 按 Enum.name() 存取）。
--      表名 `user` 在 MySQL 8 中为非保留关键字，这里统一加反引号。
-- =============================================================

-- 模块 1：用户
CREATE TABLE IF NOT EXISTS `user` (
    `id`            BIGINT       NOT NULL AUTO_INCREMENT,
    `username`      VARCHAR(64)  NOT NULL,
    `password_hash` VARCHAR(255) NOT NULL COMMENT 'bcrypt 加密',
    `email`         VARCHAR(255)          DEFAULT NULL,
    `created_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_user_username` (`username`),
    UNIQUE KEY `uk_user_email` (`email`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 2：学习空间（软删）
CREATE TABLE IF NOT EXISTS `learning_space` (
    `id`          BIGINT       NOT NULL AUTO_INCREMENT,
    `user_id`     BIGINT       NOT NULL,
    `name`        VARCHAR(128) NOT NULL,
    `description` TEXT,
    `visibility`  ENUM ('PRIVATE', 'SHARED') NOT NULL DEFAULT 'PRIVATE',
    `deleted_at`  DATETIME              DEFAULT NULL COMMENT '软删标记，MyBatis-Plus @TableLogic',
    `created_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_space_user` (`user_id`),
    CONSTRAINT `fk_space_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 3：知识点（软删）
CREATE TABLE IF NOT EXISTS `concept` (
    `id`                BIGINT       NOT NULL AUTO_INCREMENT,
    `learning_space_id` BIGINT       NOT NULL,
    `canonical_name`    VARCHAR(255) NOT NULL COMMENT '标准名称，如 Translation_Lookaside_Buffer',
    `display_name`      VARCHAR(255)          DEFAULT NULL COMMENT '显示名称，如 TLB',
    `description`       TEXT,
    `difficulty_level`  INT                   DEFAULT NULL COMMENT '1~5',
    `deleted_at`        DATETIME              DEFAULT NULL COMMENT '软删标记',
    `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_concept_space_name` (`learning_space_id`, `canonical_name`),
    KEY `idx_concept_space` (`learning_space_id`),
    CONSTRAINT `fk_concept_space` FOREIGN KEY (`learning_space_id`) REFERENCES `learning_space` (`id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 3：概念关系
CREATE TABLE IF NOT EXISTS `concept_relation` (
    `id`                BIGINT NOT NULL AUTO_INCREMENT,
    `source_concept_id` BIGINT NOT NULL,
    `target_concept_id` BIGINT NOT NULL,
    `relation_type`     ENUM ('PREREQUISITE', 'RELATED', 'PART_OF', 'CONTRAST') NOT NULL,
    `weight`            FLOAT          DEFAULT NULL COMMENT '0~1',
    `created_at`        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_relation` (`source_concept_id`, `target_concept_id`, `relation_type`),
    KEY `idx_rel_source` (`source_concept_id`),
    KEY `idx_rel_target` (`target_concept_id`),
    CONSTRAINT `fk_rel_source` FOREIGN KEY (`source_concept_id`) REFERENCES `concept` (`id`),
    CONSTRAINT `fk_rel_target` FOREIGN KEY (`target_concept_id`) REFERENCES `concept` (`id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 13：学习活动容器（不承担长期记忆）
CREATE TABLE IF NOT EXISTS `learning_session` (
    `id`                BIGINT       NOT NULL AUTO_INCREMENT,
    `user_id`           BIGINT       NOT NULL,
    `learning_space_id` BIGINT       NOT NULL,
    `title`             VARCHAR(255)          DEFAULT NULL,
    `goal`              TEXT                  DEFAULT NULL COMMENT '这一阶段最终要解决什么（长期），见 docs/session-design.md §一.1',
    `current_focus`     VARCHAR(255)          DEFAULT NULL COMMENT '当前卡在哪里 / 正在解决什么（短期）',
    -- PAUSED：暂时停下但目标未完成。一周没学不该被当成「学完了」，也不该被自动复活成新 Session
    `status`            ENUM ('ACTIVE', 'PAUSED', 'COMPLETED', 'ABANDONED') NOT NULL DEFAULT 'ACTIVE',
    `started_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `last_active_at`    DATETIME              DEFAULT NULL,
    `completed_at`      DATETIME              DEFAULT NULL,
    PRIMARY KEY (`id`),
    KEY `idx_sess_user` (`user_id`),
    KEY `idx_sess_space` (`learning_space_id`),
    CONSTRAINT `fk_sess_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`),
    CONSTRAINT `fk_sess_space` FOREIGN KEY (`learning_space_id`) REFERENCES `learning_space` (`id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;


-- 模块 4：对话（软删）
CREATE TABLE IF NOT EXISTS `conversation` (
    `id`                BIGINT       NOT NULL AUTO_INCREMENT,
    `user_id`           BIGINT       NOT NULL,
    `learning_space_id` BIGINT       NOT NULL,
    -- 懒创建：建对话时还不知道用户要学什么，第一条有效学习消息出现后由 SessionResolver 决定归属。
    -- NULL 是正常业务状态，不是「数据缺失」，见 docs/session-design.md §四。
    `session_id`        BIGINT                DEFAULT NULL COMMENT '所属学习活动；NULL = 尚未归入任何 Session',
    `title`             VARCHAR(255)          DEFAULT NULL,
    `status`            ENUM ('ACTIVE', 'ARCHIVED') NOT NULL DEFAULT 'ACTIVE',
    `last_message_at`   DATETIME              DEFAULT NULL,
    -- 对话内消息序号的分配计数器：每次 append 原子 +1 后读回，0 表示尚未分配。
    -- 不用 SELECT MAX(sequence_no) + FOR UPDATE：那会锁住相邻对话之间的索引间隙，
    -- 并发写不同对话时互相等待，实测会死锁。
    `last_message_seq`  INT          NOT NULL DEFAULT 0 COMMENT '最后分配的消息序号，0 = 尚未分配',
    `deleted_at`        DATETIME              DEFAULT NULL COMMENT '软删标记',
    `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_conv_user_space_updated` (`user_id`, `learning_space_id`, `updated_at`),
    KEY `idx_conv_session` (`session_id`, `updated_at`),
    CONSTRAINT `fk_conv_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`),
    CONSTRAINT `fk_conv_space` FOREIGN KEY (`learning_space_id`) REFERENCES `learning_space` (`id`),
    CONSTRAINT `fk_conv_session` FOREIGN KEY (`session_id`) REFERENCES `learning_session` (`id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 4：消息（软删）
CREATE TABLE IF NOT EXISTS `message` (
    `id`              BIGINT      NOT NULL AUTO_INCREMENT,
    `conversation_id` BIGINT      NOT NULL,
    `role`            ENUM ('USER', 'ASSISTANT', 'SYSTEM', 'TOOL') NOT NULL,
    `message_type`    ENUM ('TEXT', 'TEST', 'EVENT') NOT NULL DEFAULT 'TEXT',
    `content`         LONGTEXT    NOT NULL COMMENT 'Markdown 格式',
    `tokens`          INT                  DEFAULT NULL,
    `truncated`       TINYINT(1)  NOT NULL DEFAULT 0 COMMENT '助手回复是否因 Token 上限被截断',
    `citations`       JSON                 DEFAULT NULL COMMENT '助手回复的结构化来源 [{documentId,chunkId,chunkIndex,sourceTitle}]；不存 chunk 正文',
    `sequence_no`     INT         NOT NULL COMMENT '对话内顺序，从 1 开始',
    `deleted_at`      DATETIME             DEFAULT NULL COMMENT '软删标记',
    `created_at`      DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    -- 刻意加强：文档只写普通 INDEX，这里加 UNIQUE 以防并发写入撞号（见 plan.md §8）
    UNIQUE KEY `uk_msg_conv_seq` (`conversation_id`, `sequence_no`),
    CONSTRAINT `fk_msg_conversation` FOREIGN KEY (`conversation_id`) REFERENCES `conversation` (`id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- Phase 7 新增 message.truncated（标记助手回复因 Token 上限不完整）。
-- CREATE TABLE IF NOT EXISTS 不会给已存在的表补列；已有库需手工执行一次：
--   mysql -uroot -p ails < schema-migration-phase7-truncation.sql
-- 或按 README 用 schema-dev-reset.sql 清库重建。新库由上面的 CREATE 直接带上。

-- 模块 5：批注
CREATE TABLE IF NOT EXISTS `highlight` (
    `id`            BIGINT   NOT NULL AUTO_INCREMENT,
    `user_id`       BIGINT   NOT NULL,
    `message_id`    BIGINT   NOT NULL,
    `selected_text` TEXT     NOT NULL,
    `start_offset`  INT               DEFAULT NULL COMMENT '选区起点，用于可靠定位原文',
    `end_offset`    INT               DEFAULT NULL COMMENT '选区终点',
    `mark_type`     ENUM ('IMPORTANT', 'QUESTION', 'MY_UNDERSTANDING', 'MISCONCEPTION_CANDIDATE', 'CONCEPT_LINK') NOT NULL,
    `note`          TEXT,
    `created_at`    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_hl_user` (`user_id`),
    KEY `idx_hl_message` (`message_id`),
    CONSTRAINT `fk_hl_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`),
    CONSTRAINT `fk_hl_message` FOREIGN KEY (`message_id`) REFERENCES `message` (`id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 6：当前理解快照（一个 user × concept 只有一行；current_revision_id 不加 FK 以避开循环依赖）
CREATE TABLE IF NOT EXISTS `learning_understanding` (
    `id`                      BIGINT   NOT NULL AUTO_INCREMENT,
    `user_id`                 BIGINT   NOT NULL,
    `concept_id`              BIGINT   NOT NULL,
    `summary`                 TEXT,
    `known_parts`             JSON              DEFAULT NULL COMMENT '已理解部分',
    `misconceptions`          JSON              DEFAULT NULL COMMENT '当前仍成立的误区 [{text,status,firstDetectedAt,lastEvidenceAt,reversalEvidenceIds}]；已解除的不在此处',
    `uncertain_parts`         JSON              DEFAULT NULL COMMENT '不确定部分',
    `latest_user_explanation` TEXT              DEFAULT NULL COMMENT '最近一次用户自己的解释',
    `status`                  ENUM ('UNKNOWN', 'EXPOSED', 'PARTIAL', 'DEMONSTRATED', 'MISCONCEPTION') NOT NULL DEFAULT 'UNKNOWN',
    `current_revision_id`     BIGINT            DEFAULT NULL COMMENT '当前 Revision（无 FK，避免与 understanding_revision 循环）',
    `updated_at`              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_understanding_user_concept` (`user_id`, `concept_id`),
    KEY `idx_understanding_concept` (`concept_id`),
    CONSTRAINT `fk_understanding_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`),
    CONSTRAINT `fk_understanding_concept` FOREIGN KEY (`concept_id`) REFERENCES `concept` (`id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 7：证据链（append-only，无软删、无删除入口）
CREATE TABLE IF NOT EXISTS `learning_evidence` (
    `id`                        BIGINT  NOT NULL AUTO_INCREMENT,
    `user_id`                   BIGINT  NOT NULL,
    `concept_id`                BIGINT  NOT NULL,
    `understanding_id`          BIGINT           DEFAULT NULL COMMENT '关联理解（getOrCreateUnderstanding 保证有值）',
    `evidence_type`             ENUM ('USER_EXPLANATION', 'OBSERVATION', 'TEST_RESULT', 'USER_CORRECTION', 'HIGHLIGHT_SIGNAL') NOT NULL COMMENT '这条证据怎么来的，参与 dedupe_key',
    `observation`               TEXT    NOT NULL,
    `correctness`               ENUM ('CORRECT', 'INCORRECT', 'PARTIAL', 'UNKNOWN') NOT NULL DEFAULT 'UNKNOWN',
    `reasoning_quality`         FLOAT            DEFAULT NULL COMMENT '推理质量，内部分析信号',
    `specificity`               FLOAT            DEFAULT NULL COMMENT '针对性，内部分析信号',
    `ai_confidence`             FLOAT            DEFAULT NULL COMMENT 'AI 置信度，内部分析信号',
    `need_follow_up`            BOOLEAN          DEFAULT NULL,
    `follow_up_status`          ENUM ('NONE', 'PENDING', 'RESOLVED') NOT NULL DEFAULT 'NONE',
    `follow_up_conversation_id` BIGINT           DEFAULT NULL,
    -- Evidence 身份与溯源。去重完全由 dedupe_key 决定，两个 run_id 只回答「哪次分析看到的」
    `dedupe_key`                CHAR(64)         NOT NULL COMMENT 'SHA256(user+concept+type+主来源+归一化 observation)，Evidence 的唯一身份',
    `first_seen_run_id`         BIGINT           DEFAULT NULL COMMENT '首次看到本条观测的 run',
    `last_seen_run_id`          BIGINT           DEFAULT NULL COMMENT '最近一次「又看到同一条观测」的 run',
    `analyzer_version`          VARCHAR(64)      DEFAULT NULL COMMENT '分析器版本，仅溯源，不参与去重',
    `created_at`                DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`                DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    -- 数据库唯一约束是「重复分析不产生重复 Evidence」的最终保证，应用层查重只是优化
    UNIQUE KEY `uk_evidence_dedupe` (`dedupe_key`),
    KEY `idx_ev_user` (`user_id`),
    KEY `idx_ev_concept` (`concept_id`),
    KEY `idx_ev_understanding` (`understanding_id`),
    KEY `idx_ev_concept_created` (`user_id`, `concept_id`, `created_at`),
    CONSTRAINT `fk_ev_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`),
    CONSTRAINT `fk_ev_concept` FOREIGN KEY (`concept_id`) REFERENCES `concept` (`id`),
    CONSTRAINT `fk_ev_understanding` FOREIGN KEY (`understanding_id`) REFERENCES `learning_understanding` (`id`),
    CONSTRAINT `fk_ev_follow_up_conv` FOREIGN KEY (`follow_up_conversation_id`) REFERENCES `conversation` (`id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 8：理解历史版本（每次真正变化产生一条）
CREATE TABLE IF NOT EXISTS `understanding_revision` (
    `id`                  BIGINT   NOT NULL AUTO_INCREMENT,
    `understanding_id`    BIGINT   NOT NULL,
    `revision_no`         INT      NOT NULL COMMENT '从 1 开始，同 understanding 内递增',
    `summary`             TEXT,
    `known_parts`         JSON              DEFAULT NULL,
    `misconceptions`      JSON              DEFAULT NULL COMMENT '当时仍成立的误区（快照）',
    `uncertain_parts`     JSON              DEFAULT NULL,
    `user_explanation`    TEXT              DEFAULT NULL,
    `status`              ENUM ('UNKNOWN', 'EXPOSED', 'PARTIAL', 'DEMONSTRATED', 'MISCONCEPTION') NOT NULL,
    `trigger_evidence_id` BIGINT            DEFAULT NULL COMMENT '导致本次变化的 Evidence',
    `created_at`          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_revision_no` (`understanding_id`, `revision_no`),
    KEY `idx_revision_understanding` (`understanding_id`),
    KEY `idx_revision_evidence` (`trigger_evidence_id`),
    CONSTRAINT `fk_revision_understanding` FOREIGN KEY (`understanding_id`) REFERENCES `learning_understanding` (`id`),
    CONSTRAINT `fk_revision_evidence` FOREIGN KEY (`trigger_evidence_id`) REFERENCES `learning_evidence` (`id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 9：Evidence 的真实来源（多态引用，source_id 由应用层校验真实对象存在）
CREATE TABLE IF NOT EXISTS `evidence_source` (
    `id`          BIGINT   NOT NULL AUTO_INCREMENT,
    `evidence_id` BIGINT   NOT NULL,
    `source_type` ENUM ('MESSAGE', 'HIGHLIGHT', 'TEST_ANSWER') NOT NULL,
    `source_id`   BIGINT   NOT NULL,
    `created_at`  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_evidence_source` (`evidence_id`, `source_type`, `source_id`),
    KEY `idx_source` (`source_type`, `source_id`),
    CONSTRAINT `fk_es_evidence` FOREIGN KEY (`evidence_id`) REFERENCES `learning_evidence` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 10：学习状态（Understanding 的 Materialized Projection，只由 LearningStateService 写）
-- 规格原设计为复合主键 (user_id, concept_id)，MyBatis-Plus 不支持复合主键，
-- 故增加自增代理主键 id + UNIQUE(user_id, concept_id)。
CREATE TABLE IF NOT EXISTS `learning_state` (
    `id`               BIGINT   NOT NULL AUTO_INCREMENT,
    `user_id`          BIGINT   NOT NULL,
    `concept_id`       BIGINT   NOT NULL,
    `status`           ENUM ('UNKNOWN', 'EXPOSED', 'PARTIAL', 'DEMONSTRATED', 'MISCONCEPTION') NOT NULL DEFAULT 'UNKNOWN',
    `next_review_at`   DATETIME          DEFAULT NULL,
    `review_priority`  INT      NOT NULL DEFAULT 0 COMMENT '由状态派生：误区9/正在解除的误区6/PARTIAL 5/EXPOSED 4/UNKNOWN 3/DEMONSTRATED 1',
    `last_evidence_at` DATETIME          DEFAULT NULL,
    `updated_at`       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_state_user_concept` (`user_id`, `concept_id`),
    KEY `idx_state_concept` (`concept_id`),
    CONSTRAINT `fk_state_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`),
    CONSTRAINT `fk_state_concept` FOREIGN KEY (`concept_id`) REFERENCES `concept` (`id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 11：Agent 长期记忆（不物理删除，用 status 管生命周期）
--
-- 刻意偏离设计文档 §十八：文档的 memory_key 是 NOT NULL，这里放宽为可空。
-- 理由：memory_key 表示「同作用域内这是哪一条记忆」，而调用方（前端 / Agent）
-- 经常只知道内容、不关心标识。放开为可空后，服务层在缺省时用 content 的哈希兜底，
-- 于是缺少 memory_key **不会**变成「每次 PUT 都插一条新记忆」，
-- 唯一约束 uk_memory_identity 仍然真的在兜底。
CREATE TABLE IF NOT EXISTS `memory` (
    `id`                BIGINT       NOT NULL AUTO_INCREMENT,
    `user_id`           BIGINT       NOT NULL,
    `scope_type`        ENUM ('GLOBAL', 'SPACE', 'CONCEPT') NOT NULL COMMENT '作用域类型，由学习空间/知识点推导，不接受调用方指定',
    `scope_key`         VARCHAR(128) NOT NULL COMMENT 'GLOBAL / SPACE:{id} / CONCEPT:{id}；见文档 §十九',
    `learning_space_id` BIGINT                DEFAULT NULL COMMENT 'SPACE / CONCEPT 作用域所属空间；GLOBAL 时为 NULL',
    `concept_id`        BIGINT                DEFAULT NULL COMMENT 'CONCEPT 作用域的知识点；其余为 NULL',
    `memory_type`       ENUM ('USER', 'LEARNING', 'PREFERENCE') NOT NULL,
    `memory_key`        VARCHAR(255)          DEFAULT NULL COMMENT '同作用域内的记忆标识，如 os/teaching-style；缺省时由 content 哈希兜底',
    `content`           TEXT         NOT NULL,
    `status`            ENUM ('CANDIDATE', 'ACTIVE', 'ARCHIVED') NOT NULL DEFAULT 'CANDIDATE',
    `importance`        FLOAT                 DEFAULT NULL,
    `version`           INT          NOT NULL DEFAULT 0 COMMENT '乐观锁；需要 MybatisPlusConfig 注册 OptimisticLockerInnerInterceptor 才生效',
    `embedding_ref`     VARCHAR(255)          DEFAULT NULL COMMENT '向量存储引用',
    `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_memory_identity` (`user_id`, `scope_key`, `memory_key`),
    KEY `idx_memory_retrieval` (`user_id`, `scope_type`, `learning_space_id`, `concept_id`, `status`),
    KEY `idx_memory_user` (`user_id`),
    KEY `idx_memory_space` (`learning_space_id`),
    KEY `idx_memory_concept` (`concept_id`),
    KEY `idx_memory_type_status` (`memory_type`, `status`),
    -- 中文全文检索：默认 FULLTEXT 按空格分词，中文整句会变成一个 token，必须用 ngram。
    -- 分词粒度由建索引时的 ngram_token_size（默认 2）决定；单字查询永远不命中。
    -- CREATE TABLE IF NOT EXISTS 不会给已存在的表补索引，老库必须执行
    -- schema-migration-memory-fulltext.sql。
    FULLTEXT KEY `ft_memory_content` (`content`) WITH PARSER ngram,
    CONSTRAINT `fk_memory_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`),
    CONSTRAINT `fk_memory_space` FOREIGN KEY (`learning_space_id`) REFERENCES `learning_space` (`id`),
    CONSTRAINT `fk_memory_concept` FOREIGN KEY (`concept_id`) REFERENCES `concept` (`id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 11：Memory 的来源
CREATE TABLE IF NOT EXISTS `memory_source` (
    `id`          BIGINT   NOT NULL AUTO_INCREMENT,
    `memory_id`   BIGINT   NOT NULL,
    `source_type` ENUM ('MESSAGE', 'HIGHLIGHT', 'EVIDENCE', 'TEST_ANSWER') NOT NULL,
    `source_id`   BIGINT   NOT NULL,
    `created_at`  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_memory_source` (`memory_id`, `source_type`, `source_id`),
    KEY `idx_ms_memory` (`memory_id`),
    CONSTRAINT `fk_ms_memory` FOREIGN KEY (`memory_id`) REFERENCES `memory` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 11：Transactional Outbox（Memory → 向量索引的可靠事件表）
--
-- 本轮（Phase 1）只建表，不写代码使用：先让结构就位，后续阶段接入 Publisher / Relay。
-- 同一个业务事务里写 memory 与 outbox_event，由后台 Relay 轮询投递到 Agent，
-- 保证「记忆写成功」与「向量索引事件已记录」不会只发生一半。
--
-- 刻意不建外键：Outbox 是基础设施表，Memory 也永不物理删除。
CREATE TABLE IF NOT EXISTS `outbox_event` (
    `id`              BIGINT       NOT NULL AUTO_INCREMENT,
    `event_type`      VARCHAR(32)  NOT NULL COMMENT 'MEMORY_UPSERT / MEMORY_DELETE',
    `aggregate_type`  VARCHAR(32)  NOT NULL DEFAULT 'MEMORY',
    `aggregate_id`    BIGINT       NOT NULL COMMENT 'memory.id',
    `payload`         JSON         NOT NULL COMMENT '自包含快照，Relay 不依赖处理时的库内状态',
    `status`          ENUM ('PENDING', 'PROCESSING', 'DONE', 'DEAD') NOT NULL DEFAULT 'PENDING',
    `attempt_count`   INT          NOT NULL DEFAULT 0,
    `max_attempts`    INT          NOT NULL DEFAULT 8,
    `next_attempt_at` DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    `locked_by`       VARCHAR(64)           DEFAULT NULL,
    `locked_at`       DATETIME(3)           DEFAULT NULL,
    `last_error`      VARCHAR(500)          DEFAULT NULL COMMENT '只存错误摘要，不存堆栈/内容',
    `dedup_key`       VARCHAR(191)          DEFAULT NULL COMMENT '仅回填脚本使用；正常写入为 NULL',
    `created_at`      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    `updated_at`      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                                   ON UPDATE CURRENT_TIMESTAMP(3),
    `completed_at`    DATETIME(3)           DEFAULT NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_outbox_dedup` (`dedup_key`),
    KEY `idx_outbox_due` (`status`, `next_attempt_at`, `id`),
    KEY `idx_outbox_aggregate` (`aggregate_type`, `aggregate_id`, `id`),
    KEY `idx_outbox_lease` (`status`, `locked_at`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 12：主动验证任务（测试嵌入当前对话）
CREATE TABLE IF NOT EXISTS `test_task` (
    `id`                BIGINT   NOT NULL AUTO_INCREMENT,
    `user_id`           BIGINT   NOT NULL,
    `concept_id`        BIGINT   NOT NULL,
    `conversation_id`   BIGINT            DEFAULT NULL COMMENT '测试嵌入的对话',
    `question_type`     ENUM ('SINGLE_CHOICE', 'TRUE_FALSE', 'SHORT_ANSWER') NOT NULL,
    `question_text`     TEXT     NOT NULL,
    `options`           JSON              DEFAULT NULL COMMENT '选择题选项',
    `correct_answer`    TEXT              DEFAULT NULL,
    `evaluation_rubric` JSON              DEFAULT NULL COMMENT '简答题评价标准',
    `hint`              TEXT              DEFAULT NULL,
    `status`            ENUM ('PENDING', 'ANSWERED', 'EXPIRED') NOT NULL DEFAULT 'PENDING',
    `created_at`        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `expired_at`        DATETIME          DEFAULT NULL,
    PRIMARY KEY (`id`),
    KEY `idx_test_user` (`user_id`),
    KEY `idx_test_concept` (`concept_id`),
    KEY `idx_test_conversation` (`conversation_id`),
    CONSTRAINT `fk_test_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`),
    CONSTRAINT `fk_test_concept` FOREIGN KEY (`concept_id`) REFERENCES `concept` (`id`),
    CONSTRAINT `fk_test_conversation` FOREIGN KEY (`conversation_id`) REFERENCES `conversation` (`id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 12：测试回答
CREATE TABLE IF NOT EXISTS `test_answer` (
    `id`          BIGINT   NOT NULL AUTO_INCREMENT,
    `task_id`     BIGINT   NOT NULL,
    `user_answer` TEXT     NOT NULL,
    `is_correct`  BOOLEAN           DEFAULT NULL,
    `evaluation`  JSON              DEFAULT NULL COMMENT '判分信息，如 {correctness, feedback}',
    `answered_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_answer_task` (`task_id`) COMMENT '一个任务只允许一次有效回答',
    CONSTRAINT `fk_answer_task` FOREIGN KEY (`task_id`) REFERENCES `test_task` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 14：文档
--
-- RAG 相关字段说明：
--   user_id       冗余 owner，由 learning_space.user_id 回填；归属校验仍以 space 为准
--   content_hash  原始文件 SHA-256；旧数据无原文件时允许 NULL（不伪造 hash）
--   deleted_at    软删标记；DELETING / DELETED 状态配合删除向量流程使用
--
-- active-only 去重：
--   active_content_hash 是 STORED 生成列：deleted_at IS NULL 时等于 content_hash，
--   软删后自动变为 NULL。唯一键建在 (learning_space_id, active_content_hash) 上，
--   因此：
--     - 同一 Space 的有效文件仍然唯一；
--     - 软删记录不再占用 hash，可以重新上传相同内容；
--     - 多条软删记录、以及 content_hash 为 NULL 的旧记录可以共存。
--   生成列只服务数据库约束，不映射到实体。
CREATE TABLE IF NOT EXISTS `document` (
    `id`                BIGINT        NOT NULL AUTO_INCREMENT,
    `learning_space_id` BIGINT        NOT NULL,
    `user_id`           BIGINT        NOT NULL COMMENT '冗余 owner；由 learning_space.user_id 回填',
    `content_hash`      CHAR(64) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL COMMENT '原始文件 SHA-256；旧数据无原文件时为 NULL',
    `name`              VARCHAR(255)  NOT NULL,
    `file_name`         VARCHAR(255)  NOT NULL,
    `storage_uri`       VARCHAR(1024) NOT NULL,
    `file_type`         VARCHAR(64)            DEFAULT NULL,
    `size`              BIGINT                 DEFAULT NULL,
    `status`            ENUM ('UPLOADING', 'PROCESSING', 'READY', 'FAILED', 'DELETING', 'DELETED') NOT NULL DEFAULT 'UPLOADING',
    `parser_version`    VARCHAR(64)            DEFAULT NULL,
    `chunk_count`       INT           NOT NULL DEFAULT 0,
    `index_attempts`    INT           NOT NULL DEFAULT 0,
    `index_error`       VARCHAR(1024)          DEFAULT NULL,
    `embedding_model`   VARCHAR(128)           DEFAULT NULL,
    `embedding_provider` VARCHAR(64)            DEFAULT NULL COMMENT 'hash / openai-compatible',
    `embedding_dimension` INT                  DEFAULT NULL COMMENT '向量维度',
    `embedding_fingerprint` VARCHAR(128)        DEFAULT NULL COMMENT 'provider+model+dimension+endpoint 指纹',
    `vector_collection` VARCHAR(255)           DEFAULT NULL COMMENT '实际写入的 Qdrant collection',
    `index_version`     INT           NOT NULL DEFAULT 0 COMMENT '当前活跃构建令牌，与 document_chunk.index_version 对应',
    `index_started_at`  DATETIME               DEFAULT NULL COMMENT '构建租约开始时间；非空表示有 writer',
    `indexed_at`        DATETIME               DEFAULT NULL,
    `delete_attempts`   INT           NOT NULL DEFAULT 0,
    `delete_error`      VARCHAR(1024)          DEFAULT NULL,
    `deleted_at`        DATETIME               DEFAULT NULL COMMENT '软删标记，MyBatis-Plus @TableLogic',
    `active_content_hash` CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
        GENERATED ALWAYS AS (CASE WHEN `deleted_at` IS NULL THEN `content_hash` ELSE NULL END) STORED
        COMMENT '仅未软删记录的 content_hash；用于 active-only 唯一约束',
    `created_at`        DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`        DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_doc_space_active_hash` (`learning_space_id`, `active_content_hash`),
    KEY `idx_doc_space` (`learning_space_id`),
    KEY `idx_doc_space_status` (`learning_space_id`, `status`),
    KEY `idx_doc_status_started` (`status`, `index_started_at`),
    KEY `idx_doc_user` (`user_id`),
    CONSTRAINT `fk_doc_space` FOREIGN KEY (`learning_space_id`) REFERENCES `learning_space` (`id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 14：文档切片
CREATE TABLE IF NOT EXISTS `document_chunk` (
    `id`            BIGINT       NOT NULL AUTO_INCREMENT,
    `document_id`   BIGINT       NOT NULL,
    `index_version` INT          NOT NULL DEFAULT 0 COMMENT '所属索引构建令牌',
    `chunk_index`   INT          NOT NULL,
    `content`       TEXT         NOT NULL,
    `page_number`   INT                   DEFAULT NULL,
    `token_count`   INT                   DEFAULT NULL,
    `vector_ref`    VARCHAR(255)          DEFAULT NULL COMMENT '向量库引用',
    `metadata_json` JSON                  DEFAULT NULL,
    `created_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_chunk_doc_ver_idx` (`document_id`, `index_version`, `chunk_index`),
    CONSTRAINT `fk_chunk_document` FOREIGN KEY (`document_id`) REFERENCES `document` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 15：学习计划（第一版只做目标清单）
CREATE TABLE IF NOT EXISTS `learning_plan` (
    `id`                BIGINT       NOT NULL AUTO_INCREMENT,
    `user_id`           BIGINT       NOT NULL,
    `learning_space_id` BIGINT       NOT NULL,
    `title`             VARCHAR(255) NOT NULL,
    `description`       TEXT,
    `goal`              TEXT,
    `status`            ENUM ('ACTIVE', 'COMPLETED', 'ARCHIVED') NOT NULL DEFAULT 'ACTIVE',
    `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_plan_user` (`user_id`),
    KEY `idx_plan_space` (`learning_space_id`),
    CONSTRAINT `fk_plan_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`),
    CONSTRAINT `fk_plan_space` FOREIGN KEY (`learning_space_id`) REFERENCES `learning_space` (`id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 16：历史对话导入任务
CREATE TABLE IF NOT EXISTS `import_task` (
    `id`                BIGINT       NOT NULL AUTO_INCREMENT,
    `user_id`           BIGINT       NOT NULL,
    `learning_space_id` BIGINT       NOT NULL,
    `file_name`         VARCHAR(255) NOT NULL,
    `source_type`       ENUM ('CHATGPT', 'CLAUDE', 'GEMINI', 'OTHER') NOT NULL,
    `status`            ENUM ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED') NOT NULL DEFAULT 'PENDING',
    `progress`          INT          NOT NULL DEFAULT 0,
    `total_messages`    INT          NOT NULL DEFAULT 0,
    `error_message`     TEXT,
    `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_import_user` (`user_id`),
    KEY `idx_import_space` (`learning_space_id`),
    CONSTRAINT `fk_import_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`),
    CONSTRAINT `fk_import_space` FOREIGN KEY (`learning_space_id`) REFERENCES `learning_space` (`id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 17：Agent 执行轨迹（一次 SSE 请求 / 一次 analysis run 对应一条）
CREATE TABLE IF NOT EXISTS `agent_run` (
    `id`              BIGINT   NOT NULL AUTO_INCREMENT,
    `user_id`         BIGINT   NOT NULL,
    `conversation_id` BIGINT            DEFAULT NULL,
    `session_id`      BIGINT            DEFAULT NULL,
    `run_type`        ENUM ('CHAT', 'ANALYSIS', 'TEST', 'MEMORY') NOT NULL,
    `status`          ENUM ('PENDING', 'STALE', 'RUNNING', 'COMPLETED', 'TRUNCATED', 'SUCCEEDED', 'FAILED') NOT NULL DEFAULT 'RUNNING',
    -- 两跳链路（Postman → 后端 → Agent）里唯一能跨进程把两半日志对上的键
    `trace_id`        CHAR(32)           DEFAULT NULL COMMENT '由后端生成并透传给 Agent',
    `input_summary`   TEXT,
    `output_summary`  TEXT,
    `duration_ms`     INT               DEFAULT NULL,
    `error_message`   TEXT              COMMENT '给人看的失败说明，不存堆栈',
    `created_at`      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `finished_at`     DATETIME          DEFAULT NULL,
    PRIMARY KEY (`id`),
    KEY `idx_agent_user` (`user_id`),
    KEY `idx_agent_conv` (`conversation_id`),
    KEY `idx_agent_session` (`session_id`),
    KEY `idx_agent_run_trace` (`trace_id`),
    CONSTRAINT `fk_agent_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`),
    CONSTRAINT `fk_agent_conv` FOREIGN KEY (`conversation_id`) REFERENCES `conversation` (`id`),
    CONSTRAINT `fk_agent_session` FOREIGN KEY (`session_id`) REFERENCES `learning_session` (`id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 异步分析任务状态：老库的 status ENUM 只有 RUNNING/COMPLETED/FAILED。
-- CREATE TABLE IF NOT EXISTS 不会修改已存在的表，所以这里补一条幂等的 ALTER。
-- 必须放在上面的 CREATE 之后：新库先建表，老库表已存在，两种情况下都成立。
-- STALE 表示「可能遗留」，不是终态：结果消费者仍允许把 STALE 抢占为 RUNNING。
-- TRUNCATED 表示同步对话因 Token 上限提前停止：内容可展示，但不是完整回答。
ALTER TABLE `agent_run`
    MODIFY COLUMN `status`
        ENUM ('PENDING', 'STALE', 'RUNNING', 'COMPLETED', 'TRUNCATED', 'SUCCEEDED', 'FAILED')
        NOT NULL DEFAULT 'RUNNING';
