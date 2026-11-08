-- =============================================================
-- AI Learning Space — RAG-02 / RAG-03 迁移
--
-- 用途：把【已有库】升级到“按 index_version 记录 collection + Qdrant 反向对账”所需结构。
-- 本脚本不会被 spring.sql.init 执行（它只加载 schema.sql），必须手工运行：
--
--     mysql -uroot -p ails < schema-migration-rag-02-03.sql
--
-- 执行前提：
--   1. 先备份 document / document_chunk：
--        mysqldump -uroot -p ails document document_chunk > backup-rag-02-03.sql
--   2. 停止后端，避免迁移期间有上传/索引/删除写入。
--   3. MySQL 8.0（本地验证版本 8.0.37）。
--
-- 幂等性：
--   - 每一步先查 information_schema，已存在则跳过；
--   - DDL 会隐式提交，中途失败后修复原因重新执行即可从断点继续；
--   - 只新增列/表并做一次保守回填，不删除、不改写任何业务数据。
--
-- 回填边界（重要）：
--   - 只回填“当前活跃版本”的 collection，因为 document.vector_collection 就是
--     该版本成功切换时写入的，语义准确；
--   - 历史上已经存在的非活跃版本没有可靠位置信息，不回填、不猜测 collection 名。
--     这些旧版本 points 属于“无法定位的 legacy 残留”，见
--     docs/audit/KNOWN_LIMITATIONS.md 的 RAG-02 边界说明；
--   - document.vector_collection 为 NULL 的 legacy 文档同样不回填。
-- =============================================================

SET NAMES utf8mb4;

DROP PROCEDURE IF EXISTS `ails_migrate_rag_02_03`;

DELIMITER $$

CREATE PROCEDURE `ails_migrate_rag_02_03`()
BEGIN
    -- ---------------------------------------------------------
    -- 1. document：反向对账状态（不改变文档 status）
    -- ---------------------------------------------------------
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND column_name = 'vector_reconciliation_status'
    ) THEN
        ALTER TABLE `document`
            ADD COLUMN `vector_reconciliation_status` VARCHAR(16) NULL
                COMMENT '反向对账结果：OK / MISMATCH / UNREACHABLE / UNLOCATABLE'
                AFTER `vector_collection`;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND column_name = 'vector_reconciliation_checked_at'
    ) THEN
        ALTER TABLE `document`
            ADD COLUMN `vector_reconciliation_checked_at` DATETIME NULL
                COMMENT '最近一次反向对账时间'
                AFTER `vector_reconciliation_status`;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND column_name = 'vector_reconciliation_detail'
    ) THEN
        ALTER TABLE `document`
            ADD COLUMN `vector_reconciliation_detail` VARCHAR(512) NULL
                COMMENT '最近一次反向对账明细（expected/actual/collection）'
                AFTER `vector_reconciliation_checked_at`;
    END IF;

    -- ---------------------------------------------------------
    -- 2. document_index_version：每个索引版本 -> collection
    -- ---------------------------------------------------------
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_schema = DATABASE()
           AND table_name = 'document_index_version'
    ) THEN
        CREATE TABLE `document_index_version` (
            `document_id`       BIGINT       NOT NULL,
            `index_version`     INT          NOT NULL,
            `vector_collection` VARCHAR(255) NOT NULL COMMENT '该版本实际写入的 Qdrant collection',
            `created_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (`document_id`, `index_version`),
            KEY `idx_div_collection` (`vector_collection`),
            CONSTRAINT `fk_div_document` FOREIGN KEY (`document_id`)
                REFERENCES `document` (`id`) ON DELETE CASCADE
        ) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;
    END IF;

    -- 保守回填：只记录当前活跃版本。vector_collection 由同一次 complete 写入，
    -- 与该版本匹配；非活跃版本与 legacy NULL 保持无记录。
    INSERT INTO `document_index_version`
        (`document_id`, `index_version`, `vector_collection`)
    SELECT d.`id`, d.`index_version`, d.`vector_collection`
      FROM `document` d
     WHERE d.`vector_collection` IS NOT NULL
       AND d.`vector_collection` <> ''
       AND d.`index_version` IS NOT NULL
    ON DUPLICATE KEY UPDATE
        `vector_collection` = VALUES(`vector_collection`);

    -- ---------------------------------------------------------
    -- 3. scheduled_task_lease：多实例定时任务互斥
    -- ---------------------------------------------------------
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_schema = DATABASE()
           AND table_name = 'scheduled_task_lease'
    ) THEN
        CREATE TABLE `scheduled_task_lease` (
            `task_name`  VARCHAR(64) NOT NULL,
            `owner`      VARCHAR(128) NOT NULL,
            `expires_at` DATETIME    NOT NULL,
            `updated_at` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`task_name`)
        ) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;
    END IF;
END$$

DELIMITER ;

CALL `ails_migrate_rag_02_03`();

DROP PROCEDURE IF EXISTS `ails_migrate_rag_02_03`;

-- =============================================================
-- 迁移后只读核对
-- =============================================================
SELECT `column_name`, `column_type`, `is_nullable`, `column_default`
  FROM information_schema.columns
 WHERE table_schema = DATABASE()
   AND table_name = 'document'
   AND `column_name` LIKE 'vector_reconciliation%'
 ORDER BY `ordinal_position`;

SELECT COUNT(*) AS backfilled_version_rows
  FROM `document_index_version`;

SELECT COUNT(*) AS documents_without_version_record
  FROM `document` d
 WHERE d.`deleted_at` IS NULL
   AND d.`vector_collection` IS NOT NULL
   AND NOT EXISTS (
       SELECT 1 FROM `document_index_version` v
        WHERE v.`document_id` = d.`id`
          AND v.`index_version` = d.`index_version`
   );
