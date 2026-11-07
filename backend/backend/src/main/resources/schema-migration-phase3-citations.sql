-- =============================================================
-- AI Learning Space — Phase 3：消息来源引用（message.citations）
--
-- 用途：把【已有库】升级到 Phase 3 的结构。本脚本不会被 spring.sql.init 执行
-- （它只加载 schema.sql），必须手工运行：
--
--     mysql -uroot -p ails < schema-migration-phase3-citations.sql
--
-- 执行前提：
--   1. 先备份 message：
--        mysqldump -uroot -p ails message > backup-phase3-citations.sql
--   2. 停止后端，避免迁移期间有对话写入。
--   3. MySQL 8.0（本地验证版本 8.0.37）。
--
-- 为什么需要这个脚本：
--   schema.sql 全部是 CREATE TABLE IF NOT EXISTS，不会给已存在的表补列。
--   老库直接启动新版本后，助手消息的 INSERT 会带上 citations 列并报
--   ERROR 1054 Unknown column 'citations'，整轮对话落库失败。
--
-- 为什么不用 `ADD COLUMN IF NOT EXISTS`：
--   MySQL 8.0 不支持该写法（MariaDB 才支持）。所以先查 information_schema
--   再执行 DDL，保证脚本可重复执行。
--
-- 旧数据策略：
--   - citations 允许 NULL。功能上线前产生的历史消息没有结构化来源，
--     读取时按空数组返回，不伪造引用。
--   - 本脚本不新增、不改写任何原有业务数据。
--
-- 幂等与失败恢复：
--   - 先查 information_schema，列已存在则跳过；
--   - MySQL 的 DDL 会隐式提交，中途失败后修复原因重跑即可从断点继续。
-- =============================================================

SET NAMES utf8mb4;

DROP PROCEDURE IF EXISTS `ails_migrate_phase3_citations`;

DELIMITER $$

CREATE PROCEDURE `ails_migrate_phase3_citations`()
BEGIN
    DECLARE v_message_table INT DEFAULT 0;

    -- ---------------------------------------------------------
    -- 0. 前置检查：库必须是已有业务库
    -- ---------------------------------------------------------
    SELECT COUNT(*) INTO v_message_table
      FROM information_schema.tables
     WHERE table_schema = DATABASE()
       AND table_name = 'message';

    IF v_message_table = 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'message table not found: run schema.sql first on a new database';
    END IF;

    -- ---------------------------------------------------------
    -- 1. message.citations：助手回复的结构化来源引用
    --    JSON 列只存身份与标题快照，不存 chunk 正文。
    -- ---------------------------------------------------------
    IF NOT EXISTS (
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'message'
           AND column_name = 'citations'
    ) THEN
        ALTER TABLE `message`
            ADD COLUMN `citations` JSON DEFAULT NULL
                COMMENT '助手回复的结构化来源 [{documentId,chunkId,chunkIndex,sourceTitle}]；不存 chunk 正文'
                AFTER `truncated`;
    END IF;
END$$

DELIMITER ;

CALL `ails_migrate_phase3_citations`();

DROP PROCEDURE IF EXISTS `ails_migrate_phase3_citations`;

-- =============================================================
-- 迁移后只读核对（不会修改数据）
-- =============================================================
SELECT `column_name`, `column_type`, `is_nullable`, `column_default`
  FROM information_schema.columns
 WHERE table_schema = DATABASE()
   AND table_name = 'message'
   AND `column_name` = 'citations';
