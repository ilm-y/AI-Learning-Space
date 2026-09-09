-- =============================================================
-- AI Learning Space — 表结构（对齐 README_v1 的 17 张表）
-- MyBatis-Plus 不自动建表，由 spring.sql.init 每次启动执行本脚本。
-- 全部为 CREATE TABLE IF NOT EXISTS，幂等。枚举列直接使用 MySQL ENUM，
-- Java 侧枚举常量名（即 code）与 ENUM 值一致。
--
-- 注意：learning_state 按 README_v1 原设计为复合主键 (user_id, concept_id)，
-- 但 MyBatis-Plus 不支持复合主键，故增加自增代理主键 id + UNIQUE(user_id, concept_id)。
-- 表名 `user` 在 MySQL 8 中为非保留关键字，这里统一加反引号。
-- =============================================================

-- 模块 1：用户
CREATE TABLE IF NOT EXISTS `user` (
    `id`            BIGINT       NOT NULL AUTO_INCREMENT,
    `username`      VARCHAR(50)  NOT NULL,
    `password_hash` VARCHAR(255) NOT NULL COMMENT 'bcrypt 加密',
    `email`         VARCHAR(100)          DEFAULT NULL,
    `created_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_user_username` (`username`),
    UNIQUE KEY `uk_user_email` (`email`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 2：学习空间
CREATE TABLE IF NOT EXISTS `learning_space` (
    `id`          BIGINT      NOT NULL AUTO_INCREMENT,
    `user_id`     BIGINT      NOT NULL,
    `name`        VARCHAR(100) NOT NULL COMMENT '如 操作系统内核',
    `description` TEXT,
    `visibility`  ENUM ('PRIVATE', 'SHARED') NOT NULL DEFAULT 'PRIVATE',
    `created_at`  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_space_user` (`user_id`),
    CONSTRAINT `fk_space_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 3：知识点
CREATE TABLE IF NOT EXISTS `concept` (
    `id`                BIGINT       NOT NULL AUTO_INCREMENT,
    `learning_space_id` BIGINT       NOT NULL,
    `canonical_name`    VARCHAR(255) NOT NULL COMMENT '规范名，如 Translation_Lookaside_Buffer',
    `display_name`      VARCHAR(100)          DEFAULT NULL COMMENT '显示名，如 TLB',
    `description`       TEXT,
    `difficulty_level`  INT                   DEFAULT NULL COMMENT '1~5',
    `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_concept_space` (`learning_space_id`),
    CONSTRAINT `fk_concept_space` FOREIGN KEY (`learning_space_id`) REFERENCES `learning_space` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 3：概念关系
CREATE TABLE IF NOT EXISTS `concept_relation` (
    `id`                BIGINT NOT NULL AUTO_INCREMENT,
    `source_concept_id` BIGINT NOT NULL,
    `target_concept_id` BIGINT NOT NULL,
    `relation_type`     ENUM ('PREREQUISITE_OF', 'RELATED_TO', 'IS_A', 'CONTRASTS_WITH') NOT NULL,
    `weight`            FLOAT          DEFAULT NULL COMMENT '0~1',
    `created_at`        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_rel_source` (`source_concept_id`),
    KEY `idx_rel_target` (`target_concept_id`),
    CONSTRAINT `fk_rel_source` FOREIGN KEY (`source_concept_id`) REFERENCES `concept` (`id`) ON DELETE CASCADE,
    CONSTRAINT `fk_rel_target` FOREIGN KEY (`target_concept_id`) REFERENCES `concept` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 4：对话
CREATE TABLE IF NOT EXISTS `conversation` (
    `id`                BIGINT       NOT NULL AUTO_INCREMENT,
    `user_id`           BIGINT       NOT NULL,
    `learning_space_id` BIGINT       NOT NULL,
    `title`             VARCHAR(200)          DEFAULT NULL COMMENT 'AI 自动总结的标题',
    `status`            ENUM ('ACTIVE', 'ARCHIVED') NOT NULL DEFAULT 'ACTIVE',
    `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后消息时间',
    PRIMARY KEY (`id`),
    KEY `idx_conv_user` (`user_id`),
    KEY `idx_conv_space` (`learning_space_id`),
    CONSTRAINT `fk_conv_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`) ON DELETE CASCADE,
    CONSTRAINT `fk_conv_space` FOREIGN KEY (`learning_space_id`) REFERENCES `learning_space` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 4：消息
CREATE TABLE IF NOT EXISTS `message` (
    `id`              BIGINT      NOT NULL AUTO_INCREMENT,
    `conversation_id` BIGINT      NOT NULL,
    `role`            ENUM ('USER', 'ASSISTANT') NOT NULL,
    `content`         TEXT        NOT NULL COMMENT 'Markdown 格式',
    `tokens`          INT                  DEFAULT NULL,
    `created_at`      DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_msg_conversation` (`conversation_id`),
    CONSTRAINT `fk_msg_conversation` FOREIGN KEY (`conversation_id`) REFERENCES `conversation` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 5：高亮
CREATE TABLE IF NOT EXISTS `highlight` (
    `id`            BIGINT NOT NULL AUTO_INCREMENT,
    `user_id`       BIGINT NOT NULL,
    `message_id`    BIGINT NOT NULL,
    `selected_text` TEXT   NOT NULL,
    `type`          ENUM ('IMPORTANT', 'QUESTION', 'MY_UNDERSTANDING') NOT NULL,
    `note`          TEXT,
    `created_at`    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_hl_user` (`user_id`),
    KEY `idx_hl_message` (`message_id`),
    CONSTRAINT `fk_hl_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`) ON DELETE CASCADE,
    CONSTRAINT `fk_hl_message` FOREIGN KEY (`message_id`) REFERENCES `message` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 6：文档
CREATE TABLE IF NOT EXISTS `document` (
    `id`                BIGINT       NOT NULL AUTO_INCREMENT,
    `learning_space_id` BIGINT       NOT NULL,
    `name`              VARCHAR(255) NOT NULL,
    `file_name`         VARCHAR(255) NOT NULL,
    `storage_uri`       VARCHAR(500) NOT NULL COMMENT '文件系统路径或 OSS key',
    `file_type`         ENUM ('PDF', 'MD', 'TXT', 'DOCX') NOT NULL,
    `size`              BIGINT                DEFAULT NULL,
    `status`            ENUM ('UPLOADING', 'PARSING', 'CHUNKING', 'INDEXING', 'READY', 'FAILED') NOT NULL DEFAULT 'UPLOADING',
    `parser_version`    VARCHAR(20)           DEFAULT NULL,
    `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_doc_space` (`learning_space_id`),
    CONSTRAINT `fk_doc_space` FOREIGN KEY (`learning_space_id`) REFERENCES `learning_space` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 6：文档切片
CREATE TABLE IF NOT EXISTS `document_chunk` (
    `id`            BIGINT NOT NULL AUTO_INCREMENT,
    `document_id`   BIGINT NOT NULL,
    `chunk_index`   INT    NOT NULL,
    `content`       TEXT   NOT NULL,
    `page_number`   INT             DEFAULT NULL,
    `token_count`   INT             DEFAULT NULL,
    `metadata_json` JSON            DEFAULT NULL,
    `created_at`    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_chunk_doc_idx` (`document_id`, `chunk_index`),
    KEY `idx_chunk_document` (`document_id`),
    CONSTRAINT `fk_chunk_document` FOREIGN KEY (`document_id`) REFERENCES `document` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 7：学习计划
CREATE TABLE IF NOT EXISTS `learning_plan` (
    `id`                BIGINT       NOT NULL AUTO_INCREMENT,
    `user_id`           BIGINT       NOT NULL,
    `learning_space_id` BIGINT       NOT NULL,
    `title`             VARCHAR(200) NOT NULL COMMENT '如 7天掌握Kafka Consumer',
    `description`       TEXT,
    `goal`              TEXT,
    `status`            ENUM ('ACTIVE', 'COMPLETED', 'PAUSED') NOT NULL DEFAULT 'ACTIVE',
    `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_plan_user` (`user_id`),
    KEY `idx_plan_space` (`learning_space_id`),
    CONSTRAINT `fk_plan_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`) ON DELETE CASCADE,
    CONSTRAINT `fk_plan_space` FOREIGN KEY (`learning_space_id`) REFERENCES `learning_space` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 7：学习课程（短期，一次学习活动）
CREATE TABLE IF NOT EXISTS `learning_session` (
    `id`                BIGINT       NOT NULL AUTO_INCREMENT,
    `user_id`           BIGINT       NOT NULL,
    `learning_space_id` BIGINT       NOT NULL,
    `learning_plan_id`  BIGINT                DEFAULT NULL,
    `title`             VARCHAR(200) NOT NULL,
    `session_type`      ENUM ('GUIDED', 'REVIEW', 'FREE') NOT NULL DEFAULT 'FREE',
    `status`            ENUM ('STARTED', 'FINISHED', 'ABANDONED') NOT NULL DEFAULT 'STARTED',
    `started_at`        DATETIME     NOT NULL,
    `finished_at`       DATETIME              DEFAULT NULL,
    `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_sess_user` (`user_id`),
    KEY `idx_sess_space` (`learning_space_id`),
    KEY `idx_sess_plan` (`learning_plan_id`),
    CONSTRAINT `fk_sess_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`) ON DELETE CASCADE,
    CONSTRAINT `fk_sess_space` FOREIGN KEY (`learning_space_id`) REFERENCES `learning_space` (`id`) ON DELETE CASCADE,
    CONSTRAINT `fk_sess_plan` FOREIGN KEY (`learning_plan_id`) REFERENCES `learning_plan` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 8：验证任务
CREATE TABLE IF NOT EXISTS `verification_task` (
    `id`                 BIGINT  NOT NULL AUTO_INCREMENT,
    `user_id`            BIGINT  NOT NULL,
    `concept_id`         BIGINT  NOT NULL,
    `learning_session_id` BIGINT          DEFAULT NULL,
    `question_type`      ENUM ('MULTIPLE_CHOICE', 'TRUE_FALSE', 'FILL_BLANK', 'SHORT_ANSWER') NOT NULL,
    `question_text`      TEXT    NOT NULL,
    `options`            JSON            DEFAULT NULL COMMENT '选择题选项，如 ["A. 会","B. 不会"]',
    `correct_answer`     TEXT    NOT NULL,
    `hint`               TEXT            DEFAULT NULL,
    `status`             ENUM ('PENDING', 'ANSWERED', 'EXPIRED', 'SKIPPED') NOT NULL DEFAULT 'PENDING',
    `created_at`         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `expired_at`         DATETIME        DEFAULT NULL COMMENT '过期时间，如 24 小时后失效',
    PRIMARY KEY (`id`),
    KEY `idx_vt_user` (`user_id`),
    KEY `idx_vt_concept` (`concept_id`),
    KEY `idx_vt_session` (`learning_session_id`),
    CONSTRAINT `fk_vt_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`) ON DELETE CASCADE,
    CONSTRAINT `fk_vt_concept` FOREIGN KEY (`concept_id`) REFERENCES `concept` (`id`) ON DELETE CASCADE,
    CONSTRAINT `fk_vt_session` FOREIGN KEY (`learning_session_id`) REFERENCES `learning_session` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 8：验证回答
CREATE TABLE IF NOT EXISTS `verification_answer` (
    `id`           BIGINT  NOT NULL AUTO_INCREMENT,
    `task_id`      BIGINT  NOT NULL,
    `user_answer`  TEXT    NOT NULL,
    `is_correct`   BOOLEAN NOT NULL,
    `answered_at`  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_answer_task` (`task_id`) COMMENT '一个任务只允许一次有效回答',
    KEY `idx_answer_task` (`task_id`),
    CONSTRAINT `fk_answer_task` FOREIGN KEY (`task_id`) REFERENCES `verification_task` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 9：学习状态（规格为复合主键 user_id+concept_id，MP 不支持，改用代理主键）
CREATE TABLE IF NOT EXISTS `learning_state` (
    `id`               BIGINT       NOT NULL AUTO_INCREMENT,
    `user_id`          BIGINT       NOT NULL,
    `concept_id`       BIGINT       NOT NULL,
    `mastery`          DOUBLE       NOT NULL DEFAULT 0 COMMENT '掌握度 0~1',
    `confidence`       DOUBLE       NOT NULL DEFAULT 0 COMMENT '置信度 0~1',
    `stability`        DOUBLE       NOT NULL DEFAULT 0.5,
    `status`           ENUM ('NEW', 'EXPLORING', 'VERIFYING', 'UNDERSTANDING', 'PRACTICING', 'MASTERED', 'MISCONCEPTION') NOT NULL DEFAULT 'NEW',
    `last_evidence_at` DATETIME              DEFAULT NULL,
    `next_review_at`   DATETIME              DEFAULT NULL,
    `review_priority`  INT          NOT NULL DEFAULT 0,
    `updated_at`       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_state_user_concept` (`user_id`, `concept_id`),
    KEY `idx_state_concept` (`concept_id`),
    CONSTRAINT `fk_state_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`) ON DELETE CASCADE,
    CONSTRAINT `fk_state_concept` FOREIGN KEY (`concept_id`) REFERENCES `concept` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 9：证据链
CREATE TABLE IF NOT EXISTS `learning_evidence` (
    `id`            BIGINT  NOT NULL AUTO_INCREMENT,
    `user_id`       BIGINT  NOT NULL,
    `concept_id`    BIGINT  NOT NULL,
    `source_type`   ENUM ('MESSAGE', 'HIGHLIGHT', 'VERIFICATION', 'IMPORTED') NOT NULL,
    `source_id`     BIGINT           DEFAULT NULL COMMENT 'message.id / highlight.id / verification_answer.id',
    `observation`   TEXT    NOT NULL,
    `correctness`   ENUM ('CORRECT', 'PARTIALLY_CORRECT', 'INCORRECT', 'UNKNOWN') NOT NULL DEFAULT 'UNKNOWN',
    `weight`        DOUBLE  NOT NULL DEFAULT 0.8 COMMENT '对话0.8 / 主动验证1.2 / 导入0.6',
    `ai_confidence` DOUBLE  NOT NULL DEFAULT 0.5,
    `created_at`    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_ev_user` (`user_id`),
    KEY `idx_ev_concept` (`concept_id`),
    CONSTRAINT `fk_ev_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`) ON DELETE CASCADE,
    CONSTRAINT `fk_ev_concept` FOREIGN KEY (`concept_id`) REFERENCES `concept` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 10：导入任务
CREATE TABLE IF NOT EXISTS `import_task` (
    `id`                BIGINT       NOT NULL AUTO_INCREMENT,
    `user_id`           BIGINT       NOT NULL,
    `learning_space_id` BIGINT       NOT NULL,
    `file_name`         VARCHAR(255) NOT NULL,
    `source_type`       ENUM ('CHATGPT_JSON', 'GEMINI_JSON', 'CLAUDE_JSON', 'MARKDOWN', 'TXT') NOT NULL,
    `status`            ENUM ('PENDING', 'PROCESSING', 'SUCCESS', 'FAILED') NOT NULL DEFAULT 'PENDING',
    `progress`          INT          NOT NULL DEFAULT 0,
    `error_message`     TEXT,
    `total_messages`    INT          NOT NULL DEFAULT 0,
    `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_import_user` (`user_id`),
    KEY `idx_import_space` (`learning_space_id`),
    CONSTRAINT `fk_import_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`) ON DELETE CASCADE,
    CONSTRAINT `fk_import_space` FOREIGN KEY (`learning_space_id`) REFERENCES `learning_space` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- 模块 10：Agent 执行轨迹
CREATE TABLE IF NOT EXISTS `agent_run` (
    `id`                  BIGINT  NOT NULL AUTO_INCREMENT,
    `user_id`             BIGINT  NOT NULL,
    `conversation_id`     BIGINT           DEFAULT NULL,
    `learning_session_id` BIGINT           DEFAULT NULL,
    `agent_type`          ENUM ('TUTOR', 'LEARNING_STATE', 'RAG', 'REVIEW', 'SUPERVISOR') NOT NULL,
    `status`              ENUM ('STARTED', 'THINKING', 'TOOL_CALLING', 'FINISHED', 'TIMEOUT', 'ERROR') NOT NULL DEFAULT 'STARTED',
    `input_summary`       TEXT,
    `output_summary`      TEXT,
    `duration_ms`         INT,
    `error_stack`         TEXT,
    `created_at`          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `finished_at`         DATETIME         DEFAULT NULL,
    PRIMARY KEY (`id`),
    KEY `idx_agent_user` (`user_id`),
    KEY `idx_agent_conv` (`conversation_id`),
    KEY `idx_agent_session` (`learning_session_id`),
    CONSTRAINT `fk_agent_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`) ON DELETE CASCADE,
    CONSTRAINT `fk_agent_conv` FOREIGN KEY (`conversation_id`) REFERENCES `conversation` (`id`) ON DELETE CASCADE,
    CONSTRAINT `fk_agent_session` FOREIGN KEY (`learning_session_id`) REFERENCES `learning_session` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;
