-- =============================================================
-- AI Learning Space — RAG 第一阶段：document 表迁移
--
-- 用途：把【已有库】的 document 表升级到 RAG 所需结构。
-- 本脚本不会被 spring.sql.init 执行（它只加载 schema.sql），必须手工运行：
--
--     mysql -uroot -p ails < schema-migration-document-rag.sql
--
-- 执行前提：
--   1. 先备份 document / document_chunk / learning_space：
--        mysqldump -uroot -p ails document document_chunk learning_space > backup.sql
--   2. 停止后端，避免迁移期间有上传/索引写入。
--   3. MySQL 8.0（本地验证版本 8.0.37）。
--
-- 为什么不用 `ADD COLUMN IF NOT EXISTS`：
--   MySQL 8.0 不支持该写法（MariaDB 才支持）。所以这里先查
--   information_schema，再执行 DDL，保证脚本可重复执行。
--
-- 幂等与失败恢复：
--   - 每一步先查 information_schema，已存在则跳过；
--   - MySQL 的 DDL 会隐式提交，脚本中途失败不会回滚已完成的步骤；
--     修复原因后重新执行即可从断点继续；
--   - user_id 回填失败、发现有效记录重复 content_hash、发现未知 status 时，
--     用 SIGNAL 中止。脚本不删除、不改写任何原有业务数据。
--
-- active-only 去重（本版新增）：
--   - active_content_hash 是 STORED 生成列：
--       deleted_at IS NULL  -> content_hash
--       deleted_at 非 NULL  -> NULL
--   - 唯一键从 (learning_space_id, content_hash) 改为
--       (learning_space_id, active_content_hash)
--     于是软删记录不再占用 hash，删除后可以重新上传相同内容；
--     多条软删记录、以及 content_hash 为 NULL 的旧记录可以共存。
--   - 若库里已有上一版迁移创建的 uk_doc_space_hash，本脚本会先检查有效记录
--     是否重复，再删除旧索引、创建新索引。旧索引存在时会阻止
--     “软删后重新上传”，所以必须替换。
--
-- 旧数据策略（重要）：
--   - content_hash 允许 NULL：旧文档若没有原始文件，无法计算 SHA-256，
--     绝不伪造 hash。生成列和唯一索引都允许 NULL，历史行不会互相冲突。
--   - user_id 只从 learning_space.user_id 回填，不读取任何客户端输入。
--   - chunk_count 从 document_chunk 实际行数回填，仅在本次新加列时执行，
--     避免覆盖服务端后续写入的值。
--   - 旧文档的 status 原样保留。没有向量的旧 READY 文档在检索侧只会
--     返回空结果，不会被误用；需要时可手工触发后续的重建索引流程。
-- =============================================================

SET NAMES utf8mb4;

DROP PROCEDURE IF EXISTS `ails_migrate_document_rag`;

DELIMITER $$

CREATE PROCEDURE `ails_migrate_document_rag`()
BEGIN
    DECLARE v_count INT DEFAULT 0;
    DECLARE v_null_user INT DEFAULT 0;
    DECLARE v_dup_active INT DEFAULT 0;
    DECLARE v_bad_status INT DEFAULT 0;
    DECLARE v_added_chunk_count TINYINT DEFAULT 0;
    DECLARE v_status_type VARCHAR(255) DEFAULT NULL;
    DECLARE v_active_extra VARCHAR(64) DEFAULT NULL;
    DECLARE v_active_expr VARCHAR(1024) DEFAULT NULL;

    -- ---------------------------------------------------------
    -- 1. user_id：先可空，从 space 所有者回填，再收紧为 NOT NULL
    -- ---------------------------------------------------------
    IF NOT EXISTS (
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND column_name = 'user_id'
    ) THEN
        ALTER TABLE `document`
            ADD COLUMN `user_id` BIGINT NULL
                COMMENT '冗余 owner；由 learning_space.user_id 回填'
                AFTER `learning_space_id`;
    END IF;

    -- 只有当前仍可空时才回填并收紧；已 NOT NULL 说明上一次已成功
    SELECT COUNT(*) INTO v_count
      FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 'document'
       AND column_name = 'user_id'
       AND is_nullable = 'YES';

    IF v_count > 0 THEN
        UPDATE `document` d
          JOIN `learning_space` s ON s.id = d.learning_space_id
           SET d.user_id = s.user_id
         WHERE d.user_id IS NULL;

        SELECT COUNT(*) INTO v_null_user
          FROM `document`
         WHERE `user_id` IS NULL;

        IF v_null_user > 0 THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'document.user_id backfill failed: document without learning_space owner';
        END IF;

        ALTER TABLE `document`
            MODIFY COLUMN `user_id` BIGINT NOT NULL
                COMMENT '冗余 owner；由 learning_space.user_id 回填';
    END IF;

    -- ---------------------------------------------------------
    -- 2. content_hash：允许 NULL，兼容没有原始文件的旧数据
    -- ---------------------------------------------------------
    IF NOT EXISTS (
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND column_name = 'content_hash'
    ) THEN
        ALTER TABLE `document`
            ADD COLUMN `content_hash` CHAR(64)
                CHARACTER SET ascii COLLATE ascii_bin NULL
                COMMENT '原始文件 SHA-256；旧数据无原文件时为 NULL'
                AFTER `user_id`;
    END IF;

    -- 注意：唯一索引不在这里创建。它必须建在 active_content_hash 生成列上，
    -- 见第 3.1 节；旧版的 uk_doc_space_hash 也会在那里被替换。

    -- ---------------------------------------------------------
    -- 3. 索引生命周期字段
    -- ---------------------------------------------------------
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND column_name = 'chunk_count'
    ) THEN
        ALTER TABLE `document`
            ADD COLUMN `chunk_count` INT NOT NULL DEFAULT 0
                COMMENT '已落库的 chunk 数；索引完成后由服务端更新'
                AFTER `parser_version`;
        SET v_added_chunk_count = 1;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND column_name = 'index_attempts'
    ) THEN
        ALTER TABLE `document`
            ADD COLUMN `index_attempts` INT NOT NULL DEFAULT 0
                COMMENT '索引尝试次数（含手工重试）'
                AFTER `chunk_count`;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND column_name = 'index_error'
    ) THEN
        ALTER TABLE `document`
            ADD COLUMN `index_error` VARCHAR(1024) NULL
                COMMENT '最近一次索引失败摘要；成功时清空'
                AFTER `index_attempts`;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND column_name = 'embedding_model'
    ) THEN
        ALTER TABLE `document`
            ADD COLUMN `embedding_model` VARCHAR(128) NULL
                COMMENT '生成当前向量的 embedding 模型'
                AFTER `index_error`;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND column_name = 'index_version'
    ) THEN
        ALTER TABLE `document`
            ADD COLUMN `index_version` INT NOT NULL DEFAULT 0
                COMMENT '索引版本；每次成功重建 +1'
                AFTER `embedding_model`;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND column_name = 'indexed_at'
    ) THEN
        ALTER TABLE `document`
            ADD COLUMN `indexed_at` DATETIME NULL
                COMMENT '最近一次索引成功时间'
                AFTER `index_version`;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND column_name = 'deleted_at'
    ) THEN
        ALTER TABLE `document`
            ADD COLUMN `deleted_at` DATETIME NULL
                COMMENT '软删标记，MyBatis-Plus @TableLogic'
                AFTER `indexed_at`;
    END IF;

    -- ---------------------------------------------------------
    -- 3.1 active-only 去重：生成列 + 唯一索引
    -- ---------------------------------------------------------
    -- 先检查【未软删】记录里是否已有重复 hash。
    -- 软删记录与 NULL hash 不参与检查，符合 active-only 语义。
    -- 必须在删除旧索引 / 创建新索引之前完成；有重复就中止，数据保持原样。
    SELECT COUNT(*) INTO v_dup_active
      FROM (
          SELECT `learning_space_id`, `content_hash`
            FROM `document`
           WHERE `deleted_at` IS NULL
             AND `content_hash` IS NOT NULL
           GROUP BY `learning_space_id`, `content_hash`
          HAVING COUNT(*) > 1
      ) dup;

    IF v_dup_active > 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'duplicate active (learning_space_id, content_hash); resolve manually before migration';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND column_name = 'active_content_hash'
    ) THEN
        ALTER TABLE `document`
            ADD COLUMN `active_content_hash` CHAR(64)
                CHARACTER SET ascii COLLATE ascii_bin
                GENERATED ALWAYS AS (
                    CASE WHEN `deleted_at` IS NULL THEN `content_hash` ELSE NULL END
                ) STORED
                COMMENT '仅未软删记录的 content_hash；用于 active-only 唯一约束'
                AFTER `deleted_at`;
    END IF;

    -- 防御：若同名普通列已存在，唯一索引不会随软删自动释放，必须人工处理
    SELECT `extra`, `generation_expression`
      INTO v_active_extra, v_active_expr
      FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 'document'
       AND column_name = 'active_content_hash';

    IF v_active_extra NOT LIKE '%GENERATED%'
       OR v_active_expr IS NULL
       OR v_active_expr NOT LIKE '%deleted_at%'
       OR v_active_expr NOT LIKE '%content_hash%' THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'active_content_hash is not the expected generated column; resolve manually';
    END IF;

    -- 确保 fk_doc_space 仍有可用的最左前缀索引。
    -- 否则 MySQL 可能把旧唯一索引当成外键依赖的索引，拒绝 DROP INDEX。
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.statistics
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND index_name = 'idx_doc_space'
    ) THEN
        ALTER TABLE `document`
            ADD KEY `idx_doc_space` (`learning_space_id`);
    END IF;

    -- 替换上一版迁移创建的“包含软删记录”的唯一索引。
    -- 旧索引存在时会阻止“软删后重新上传”，所以必须删除。
    IF EXISTS (
        SELECT 1 FROM information_schema.statistics
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND index_name = 'uk_doc_space_hash'
    ) THEN
        ALTER TABLE `document` DROP INDEX `uk_doc_space_hash`;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.statistics
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND index_name = 'uk_doc_space_active_hash'
    ) THEN
        ALTER TABLE `document`
            ADD UNIQUE KEY `uk_doc_space_active_hash` (`learning_space_id`, `active_content_hash`);
    END IF;

    -- 只在本次新加 chunk_count 时回填，重复执行不会覆盖服务端写入
    IF v_added_chunk_count = 1 THEN
        UPDATE `document` d
          LEFT JOIN (
              SELECT `document_id`, COUNT(*) AS c
                FROM `document_chunk`
               GROUP BY `document_id`
          ) x ON x.document_id = d.id
           SET d.chunk_count = COALESCE(x.c, 0);
    END IF;

    -- ---------------------------------------------------------
    -- 4. status 枚举扩展
    -- ---------------------------------------------------------
    SELECT COUNT(*) INTO v_bad_status
      FROM `document`
     WHERE CAST(`status` AS CHAR) NOT IN
           ('UPLOADING', 'PROCESSING', 'READY', 'FAILED', 'DELETING', 'DELETED');

    IF v_bad_status > 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'document.status contains unknown value; fix data before migration';
    END IF;

    SELECT `column_type` INTO v_status_type
      FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 'document'
       AND column_name = 'status';

    IF v_status_type NOT LIKE '%DELETING%' OR v_status_type NOT LIKE '%DELETED%' THEN
        ALTER TABLE `document`
            MODIFY COLUMN `status`
                ENUM ('UPLOADING', 'PROCESSING', 'READY', 'FAILED', 'DELETING', 'DELETED')
                NOT NULL DEFAULT 'UPLOADING';
    END IF;

    -- ---------------------------------------------------------
    -- 5. 检索 / 归属相关索引
    -- ---------------------------------------------------------
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.statistics
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND index_name = 'idx_doc_space_status'
    ) THEN
        ALTER TABLE `document`
            ADD KEY `idx_doc_space_status` (`learning_space_id`, `status`);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.statistics
         WHERE table_schema = DATABASE()
           AND table_name = 'document'
           AND index_name = 'idx_doc_user'
    ) THEN
        ALTER TABLE `document`
            ADD KEY `idx_doc_user` (`user_id`);
    END IF;
END$$

DELIMITER ;

CALL `ails_migrate_document_rag`();

DROP PROCEDURE IF EXISTS `ails_migrate_document_rag`;

-- =============================================================
-- 迁移后只读核对（不会修改数据）
-- =============================================================
SELECT `column_name`, `column_type`, `is_nullable`, `column_default`
  FROM information_schema.columns
 WHERE table_schema = DATABASE()
   AND table_name = 'document'
 ORDER BY `ordinal_position`;

SELECT `index_name`, `non_unique`, GROUP_CONCAT(`column_name` ORDER BY `seq_in_index`) AS columns
  FROM information_schema.statistics
 WHERE table_schema = DATABASE()
   AND table_name = 'document'
 GROUP BY `index_name`, `non_unique`
 ORDER BY `index_name`;
