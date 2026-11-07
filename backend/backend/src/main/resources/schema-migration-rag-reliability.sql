-- =============================================================
-- AI Learning Space — RAG 索引可靠性：document / document_chunk 迁移
--
-- 用途：把【已有库】升级到“单活跃版本 + 分阶段构建”所需结构。
-- 本脚本不会被 spring.sql.init 执行（它只加载 schema.sql），必须手工运行：
--
--     mysql -uroot -p ails < schema-migration-rag-reliability.sql
--
-- 执行前提：
--   1. 先备份 document / document_chunk：
--        mysqldump -uroot -p ails document document_chunk > backup-rag.sql
--   2. 停止后端，避免迁移期间有上传/索引/删除写入。
--   3. MySQL 8.0（本地验证版本 8.0.37）。
--
-- 幂等性：
--   - 每一步先查 information_schema，已存在则跳过；
--   - DDL 会隐式提交，中途失败后修复原因重新执行即可从断点继续；
--   - 不删除、不改写任何业务数据，只新增列、回填版本号、替换 chunk 唯一键。
--
-- 旧数据策略：
--   - 现有 document_chunk 全部归到 document.index_version（默认 0）。
--     迁移后 document.index_version 与 chunk.index_version 一致，旧数据立即可读；
--   - 旧 READY 文档的 embedding_provider / dimension / fingerprint / collection
--     保持 NULL，检索侧按“legacy + model 匹配”兼容；下次重建会补齐；
--   - 旧 point ID 公式（不带版本）由 Agent 在 index_version = 0 时保留，
--     因此迁移本身不需要重写 Qdrant。
-- =============================================================

SET NAMES utf8mb4;

DROP PROCEDURE IF EXISTS `ails_migrate_rag_reliability`;

DELIMITER $$

CREATE PROCEDURE `ails_migrate_rag_reliability`()
BEGIN
    DECLARE v_dup INT DEFAULT 0;

    -- ---------------------------------------------------------
    -- 1. document：索引 / 删除 / 巡检所需的新列
    -- ---------------------------------------------------------
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND column_name = 'embedding_provider'
    ) THEN
        ALTER TABLE `document`
            ADD COLUMN `embedding_provider` VARCHAR(64) NULL
                COMMENT 'Embedding provider 名（hash / openai-compatible）'
                AFTER `embedding_model`;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND column_name = 'embedding_dimension'
    ) THEN
        ALTER TABLE `document`
            ADD COLUMN `embedding_dimension` INT NULL
                COMMENT '向量维度'
                AFTER `embedding_provider`;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND column_name = 'embedding_fingerprint'
    ) THEN
        ALTER TABLE `document`
            ADD COLUMN `embedding_fingerprint` VARCHAR(128) NULL
                COMMENT 'provider+model+dimension+endpoint 配置指纹'
                AFTER `embedding_dimension`;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND column_name = 'vector_collection'
    ) THEN
        ALTER TABLE `document`
            ADD COLUMN `vector_collection` VARCHAR(255) NULL
                COMMENT '实际写入的 Qdrant collection'
                AFTER `embedding_fingerprint`;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND column_name = 'index_started_at'
    ) THEN
        ALTER TABLE `document`
            ADD COLUMN `index_started_at` DATETIME NULL
                COMMENT '构建租约开始时间；非空表示有 writer'
                AFTER `index_version`;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND column_name = 'delete_attempts'
    ) THEN
        ALTER TABLE `document`
            ADD COLUMN `delete_attempts` INT NOT NULL DEFAULT 0
                COMMENT '删除重试次数'
                AFTER `indexed_at`;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND column_name = 'delete_error'
    ) THEN
        ALTER TABLE `document`
            ADD COLUMN `delete_error` VARCHAR(1024) NULL
                COMMENT '最近一次删除失败摘要'
                AFTER `delete_attempts`;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.statistics
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND index_name = 'idx_doc_status_started'
    ) THEN
        ALTER TABLE `document`
            ADD KEY `idx_doc_status_started` (`status`, `index_started_at`);
    END IF;

    -- ---------------------------------------------------------
    -- 2. document_chunk：增加版本维度
    -- ---------------------------------------------------------
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document_chunk'
           AND column_name = 'index_version'
    ) THEN
        ALTER TABLE `document_chunk`
            ADD COLUMN `index_version` INT NOT NULL DEFAULT 0
                COMMENT '所属索引构建令牌'
                AFTER `document_id`;
    END IF;

    -- 回填：现有 chunk 归到文档当前的活跃版本。
    -- 必须在替换唯一键之前完成，保证 (document_id, index_version, chunk_index) 唯一。
    UPDATE `document_chunk` c
      JOIN `document` d ON d.id = c.document_id
       SET c.index_version = d.index_version
     WHERE c.index_version <> d.index_version;

    -- ---------------------------------------------------------
    -- 3. 替换唯一键：先建新键（同时满足外键对 document_id 的左前缀要求），再删旧键
    -- ---------------------------------------------------------
    SELECT COUNT(*) INTO v_dup
      FROM (
          SELECT `document_id`, `index_version`, `chunk_index`
            FROM `document_chunk`
           GROUP BY `document_id`, `index_version`, `chunk_index`
          HAVING COUNT(*) > 1
      ) dup;

    IF v_dup > 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'duplicate (document_id, index_version, chunk_index) after backfill; resolve manually';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.statistics
         WHERE table_schema = DATABASE()
           AND table_name = 'document_chunk'
           AND index_name = 'uk_chunk_doc_ver_idx'
    ) THEN
        ALTER TABLE `document_chunk`
            ADD UNIQUE KEY `uk_chunk_doc_ver_idx`
                (`document_id`, `index_version`, `chunk_index`);
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.statistics
         WHERE table_schema = DATABASE()
           AND table_name = 'document_chunk'
           AND index_name = 'uk_chunk_doc_idx'
    ) THEN
        ALTER TABLE `document_chunk` DROP INDEX `uk_chunk_doc_idx`;
    END IF;
END$$

DELIMITER ;

CALL `ails_migrate_rag_reliability`();

DROP PROCEDURE IF EXISTS `ails_migrate_rag_reliability`;

-- =============================================================
-- 迁移后只读核对
-- =============================================================
SELECT `column_name`, `column_type`, `is_nullable`, `column_default`
  FROM information_schema.columns
 WHERE table_schema = DATABASE()
   AND table_name = 'document'
 ORDER BY `ordinal_position`;

SELECT `index_name`, `non_unique`, GROUP_CONCAT(`column_name` ORDER BY `seq_in_index`) AS columns
  FROM information_schema.statistics
 WHERE table_schema = DATABASE()
   AND table_name = 'document_chunk'
 GROUP BY `index_name`, `non_unique`
 ORDER BY `index_name`;
