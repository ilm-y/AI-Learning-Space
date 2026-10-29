-- =============================================================
-- AI Learning Space — Phase 7：流式截断（message.truncated / agent_run.TRUNCATED）
--
-- 用途：把【已有库】升级到 Phase 7 的结构。本脚本不会被 spring.sql.init 执行
-- （它只加载 schema.sql），必须手工运行：
--
--     mysql -uroot -p ails < schema-migration-phase7-truncation.sql
--
-- 执行前提：
--   1. 先备份 message / agent_run：
--        mysqldump -uroot -p ails message agent_run > backup-phase7.sql
--   2. 停止后端，避免迁移期间有对话写入。
--   3. MySQL 8.0（本地验证版本 8.0.37）。
--
-- 为什么需要这个脚本：
--   schema.sql 全部是 CREATE TABLE IF NOT EXISTS，不会给已存在的表补列。
--   agent_run.status 在 schema.sql 末尾有一条幂等的 MODIFY COLUMN，启动时
--   会自动补上 TRUNCATED；但 message.truncated 没有对应的自动 DDL。
--   老库直接启动新版本后，截断回复的 INSERT 会报
--   ERROR 1054 Unknown column 'truncated'，整轮对话失败。
--   本脚本只补这两处结构，不改写任何原有业务数据。
--
-- 为什么不用 `ADD COLUMN IF NOT EXISTS`：
--   MySQL 8.0 不支持该写法（MariaDB 才支持）。所以先查 information_schema
--   再执行 DDL，保证脚本可重复执行。
--
-- 旧数据策略：
--   - message.truncated 为 NOT NULL DEFAULT 0：历史回复都产生于功能上线前，
--     没有截断这回事，统一按 0（完整）读取，语义正确。
--   - agent_run.status 只扩展枚举，原有状态值原样保留。
--
-- 幂等与失败恢复：
--   - 每一步先查 information_schema，已存在则跳过；
--   - MySQL 的 DDL 会隐式提交，中途失败后修复原因重跑即可从断点继续。
-- =============================================================

SET NAMES utf8mb4;

DROP PROCEDURE IF EXISTS `ails_migrate_phase7_truncation`;

DELIMITER $$

CREATE PROCEDURE `ails_migrate_phase7_truncation`()
BEGIN
    DECLARE v_message_table INT DEFAULT 0;
    DECLARE v_bad_status INT DEFAULT 0;
    DECLARE v_status_type VARCHAR(255) DEFAULT NULL;

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
    -- 1. message.truncated：助手回复是否因 Token 上限被截断
    --    历史行由 DEFAULT 0 自动补齐，不需要回填 UPDATE。
    -- ---------------------------------------------------------
    IF NOT EXISTS (
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'message'
           AND column_name = 'truncated'
    ) THEN
        ALTER TABLE `message`
            ADD COLUMN `truncated` TINYINT(1) NOT NULL DEFAULT 0
                COMMENT '助手回复是否因 Token 上限被截断'
                AFTER `tokens`;
    END IF;

    -- ---------------------------------------------------------
    -- 2. agent_run.status 枚举扩展
    --    先确认没有未知值，否则 MODIFY 在严格模式下会截断数据。
    -- ---------------------------------------------------------
    SELECT COUNT(*) INTO v_bad_status
      FROM `agent_run`
     WHERE CAST(`status` AS CHAR) NOT IN
           ('PENDING', 'STALE', 'RUNNING', 'COMPLETED', 'TRUNCATED', 'SUCCEEDED', 'FAILED');

    IF v_bad_status > 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'agent_run.status contains unknown value; fix data before migration';
    END IF;

    SELECT `column_type` INTO v_status_type
      FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 'agent_run'
       AND column_name = 'status';

    IF v_status_type NOT LIKE '%TRUNCATED%' THEN
        ALTER TABLE `agent_run`
            MODIFY COLUMN `status`
                ENUM ('PENDING', 'STALE', 'RUNNING', 'COMPLETED', 'TRUNCATED', 'SUCCEEDED', 'FAILED')
                NOT NULL DEFAULT 'RUNNING';
    END IF;
END$$

DELIMITER ;

CALL `ails_migrate_phase7_truncation`();

DROP PROCEDURE IF EXISTS `ails_migrate_phase7_truncation`;

-- =============================================================
-- 迁移后只读核对（不会修改数据）
-- =============================================================
SELECT `column_name`, `column_type`, `is_nullable`, `column_default`
  FROM information_schema.columns
 WHERE table_schema = DATABASE()
   AND table_name = 'message'
   AND `column_name` = 'truncated';

SELECT `column_type` AS agent_run_status
  FROM information_schema.columns
 WHERE table_schema = DATABASE()
   AND table_name = 'agent_run'
   AND `column_name` = 'status';
