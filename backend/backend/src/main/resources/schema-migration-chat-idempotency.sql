-- =============================================================
-- AI Learning Space — Chat 幂等：message / agent_run 迁移
--
-- 用途：把【已有库】升级到 Chat POST 幂等所需的表结构。
-- 本脚本不会被 spring.sql.init 执行（它只加载 schema.sql），必须手工运行：
--
--     mysql -uroot -p ails < schema-migration-chat-idempotency.sql
--
-- 执行前提：
--   1. 先备份 message / agent_run：
--        mysqldump -uroot -p ails message agent_run > backup.sql
--   2. 停止后端，避免迁移期间有新的聊天写入。
--   3. MySQL 8.0（本地验证版本 8.0.37）。
--
-- 幂等与失败恢复：
--   - MySQL 8.0 不支持 `ADD COLUMN IF NOT EXISTS`，所以每一步先查
--     information_schema，再执行 DDL；脚本可重复执行。
--   - MySQL 的 DDL 会隐式提交，中途失败不会回滚已完成的步骤；
--     修复原因后重新执行即可从断点继续。
--
-- 旧数据策略（重要）：
--   - 新增列全部允许 NULL。历史 message 与 agent_run 不会被打上任何
--     伪造的 client_request_id —— 旧请求没有这个键，无法还原。
--   - MySQL 唯一索引允许多个 NULL，因此历史行不会互相冲突，也不会
--     阻止其它消息写入。
--   - 新唯一键只约束「带幂等键的 USER 消息 / CHAT run」；ASSISTANT、
--     SYSTEM、TOOL 消息以及 ANALYSIS 等 run 的该列保持 NULL。
-- =============================================================

SET NAMES utf8mb4;

DROP PROCEDURE IF EXISTS `ails_migrate_chat_idempotency`;

DELIMITER $$

CREATE PROCEDURE `ails_migrate_chat_idempotency`()
BEGIN
    -- ---------------------------------------------------------
    -- 1. message.client_request_id + 唯一键
    -- ---------------------------------------------------------
    IF NOT EXISTS (
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'message'
           AND column_name = 'client_request_id'
    ) THEN
        ALTER TABLE `message`
            ADD COLUMN `client_request_id` VARCHAR(64) NULL
                COMMENT '客户端幂等键；仅 USER 消息写入，其余为 NULL'
                AFTER `sequence_no`;
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM information_schema.statistics
         WHERE table_schema = DATABASE()
           AND table_name = 'message'
           AND index_name = 'uk_msg_conv_client_request'
    ) THEN
        ALTER TABLE `message`
            ADD UNIQUE KEY `uk_msg_conv_client_request` (`conversation_id`, `client_request_id`);
    END IF;

    -- ---------------------------------------------------------
    -- 2. agent_run 的幂等键与助手消息引用
    -- ---------------------------------------------------------
    IF NOT EXISTS (
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'agent_run'
           AND column_name = 'client_request_id'
    ) THEN
        ALTER TABLE `agent_run`
            ADD COLUMN `client_request_id` VARCHAR(64) NULL
                COMMENT 'CHAT run 的幂等键，与触发它的 USER 消息一致'
                AFTER `error_message`;
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'agent_run'
           AND column_name = 'assistant_message_id'
    ) THEN
        ALTER TABLE `agent_run`
            ADD COLUMN `assistant_message_id` BIGINT NULL
                COMMENT '本轮最终落库的助手消息 id，供幂等回放定位'
                AFTER `client_request_id`;
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM information_schema.statistics
         WHERE table_schema = DATABASE()
           AND table_name = 'agent_run'
           AND index_name = 'uk_agent_conv_client_request'
    ) THEN
        ALTER TABLE `agent_run`
            ADD UNIQUE KEY `uk_agent_conv_client_request` (`conversation_id`, `client_request_id`);
    END IF;
END$$

DELIMITER ;

CALL `ails_migrate_chat_idempotency`();

DROP PROCEDURE IF EXISTS `ails_migrate_chat_idempotency`;

-- =============================================================
-- 迁移后只读核对（不会修改数据）
-- =============================================================
SELECT `table_name`, `column_name`, `column_type`, `is_nullable`, `column_default`
  FROM information_schema.columns
 WHERE table_schema = DATABASE()
   AND table_name IN ('message', 'agent_run')
   AND column_name IN ('client_request_id', 'assistant_message_id', 'sequence_no')
 ORDER BY `table_name`, `ordinal_position`;

SELECT `table_name`, `index_name`, `non_unique`,
       GROUP_CONCAT(`column_name` ORDER BY `seq_in_index`) AS columns
  FROM information_schema.statistics
 WHERE table_schema = DATABASE()
   AND table_name IN ('message', 'agent_run')
   AND index_name IN ('uk_msg_conv_client_request', 'uk_agent_conv_client_request')
 GROUP BY `table_name`, `index_name`, `non_unique`
 ORDER BY `table_name`, `index_name`;
