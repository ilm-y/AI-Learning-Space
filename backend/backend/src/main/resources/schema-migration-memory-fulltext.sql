-- =============================================================
-- AI Learning Space — Memory FULLTEXT 基础改造（Phase 1 迁移）
--
-- 用途：把【已有库】升级到「MySQL 全文检索 + Outbox 占位表」所需结构。
-- 本脚本不会被 spring.sql.init 执行（它只加载 schema.sql），必须手工运行：
--
--     mysql -uroot -p ails < schema-migration-memory-fulltext.sql
--
-- 执行前提：
--   1. 先备份 memory：
--        mysqldump -uroot -p ails memory > backup-memory.sql
--   2. 确认 MySQL 8.0：
--        SELECT VERSION();
--   3. 确认中文分词粒度为 2（本设计与测试均以 2 为前提）：
--        SHOW VARIABLES LIKE 'ngram_token_size';
--      ngram_token_size 在【建索引时】生效；之后修改参数不会改变已有索引的分词方式。
--      多环境必须一致，否则同一段中文在不同环境召回不同。
--   4. 维护窗口执行：InnoDB 的 ADD FULLTEXT INDEX 不是零成本在线操作，
--      表越大耗时越长，期间可能持有元数据锁 / 共享锁。
--
-- 幂等性：
--   - 索引与表都先查 information_schema，已存在则跳过；
--   - 重复执行不会报错，也不会产生重复结构；
--   - DDL 会隐式提交，中途失败后修复原因重新执行即可从断点继续。
--
-- 本轮不涉及：
--   - 不写 Outbox Publisher / Relay（只建表占位）；
--   - 不回填历史记忆的索引事件（等接入 Relay 后再做，避免产生无人消费的事件）；
--   - 不改写任何业务数据。
-- =============================================================

SET NAMES utf8mb4;

DROP PROCEDURE IF EXISTS `ails_migrate_memory_fulltext`;

DELIMITER $$

CREATE PROCEDURE `ails_migrate_memory_fulltext`()
BEGIN
    -- ---------------------------------------------------------
    -- 1. memory.content 的 FULLTEXT ngram 索引
    -- ---------------------------------------------------------
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.statistics
         WHERE table_schema = DATABASE()
           AND table_name = 'memory'
           AND index_name = 'ft_memory_content'
    ) THEN
        ALTER TABLE `memory`
            ADD FULLTEXT INDEX `ft_memory_content` (`content`) WITH PARSER ngram;
    END IF;

    -- ---------------------------------------------------------
    -- 2. outbox_event 占位表
    --    结构与 schema.sql 完全一致：新库由 schema.sql 建，老库由本脚本建。
    -- ---------------------------------------------------------
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
END$$

DELIMITER ;

CALL `ails_migrate_memory_fulltext`();
DROP PROCEDURE IF EXISTS `ails_migrate_memory_fulltext`;

-- ---------------------------------------------------------
-- 执行后自检（人工确认，不参与幂等判断）
-- ---------------------------------------------------------
--   1. 索引存在：
--        SHOW INDEX FROM memory WHERE Key_name = 'ft_memory_content';
--   2. 中文可以命中：
--        SELECT id, MATCH(content) AGAINST('递归' IN NATURAL LANGUAGE MODE) AS score
--          FROM memory
--         WHERE MATCH(content) AGAINST('递归' IN NATURAL LANGUAGE MODE)
--         LIMIT 5;
--   3. Outbox 表为空且结构正确：
--        SHOW CREATE TABLE outbox_event;
