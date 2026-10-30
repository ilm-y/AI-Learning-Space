-- =============================================================
-- AI Learning Space — Phase 5.4：对话内消息序号改用计数器分配
--
-- 用途：把【已有库】升级到使用 conversation.last_message_seq。本脚本不会被
-- spring.sql.init 执行（它只加载 schema.sql），必须手工运行：
--
--     mysql -uroot -p ails < schema-migration-phase54-message-seq.sql
--
-- 为什么需要：
--   旧实现用「锁 conversation 行 + SELECT MAX(sequence_no) ... FOR UPDATE」取下一个
--   序号。这条锁定读会在 message 的 uk_msg_conv_seq 上锁住索引间隙，空对话时相邻
--   对话的间隙重叠；并发写不同对话会互相等待，MySQL 判定死锁并回滚其中一个，
--   请求直接 500（2026-10-30 并发基线：12 请求 / 并发 6，3 个 500）。
--
--   新实现把序号存在 conversation.last_message_seq 上，用
--   `UPDATE ... SET last_message_seq = last_message_seq + 1` 原子分配。
--   只锁主键行，不产生间隙锁。
--
-- 执行前提：
--   1. 先备份 conversation / message；
--   2. 停止后端；
--   3. MySQL 8.0。
--
-- 旧数据回填：
--   取每个对话已有的 MAX(sequence_no)（含软删行，因为唯一索引也含它们）。
--   没有消息的对话保持 0，下次 append 从 1 开始。
--
-- 幂等：
--   先查 information_schema，列已存在则跳过；DDL 隐式提交，失败后修好可重跑。
-- =============================================================

SET NAMES utf8mb4;

DROP PROCEDURE IF EXISTS `ails_migrate_phase54_message_seq`;

DELIMITER $$

CREATE PROCEDURE `ails_migrate_phase54_message_seq`()
BEGIN
    DECLARE v_conversation_table INT DEFAULT 0;
    DECLARE v_column_exists INT DEFAULT 0;

    SELECT COUNT(*) INTO v_conversation_table
      FROM information_schema.tables
     WHERE table_schema = DATABASE()
       AND table_name = 'conversation';

    IF v_conversation_table = 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'conversation table not found: run schema.sql first';
    END IF;

    SELECT COUNT(*) INTO v_column_exists
      FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 'conversation'
       AND column_name = 'last_message_seq';

    IF v_column_exists = 0 THEN
        ALTER TABLE `conversation`
            ADD COLUMN `last_message_seq` INT NOT NULL DEFAULT 0
                COMMENT '最后分配的消息序号，0 = 尚未分配'
                AFTER `last_message_at`;

        -- 回填：每个对话已有的最大序号（包含软删行）。
        UPDATE `conversation` c
        LEFT JOIN (
            SELECT `conversation_id`, MAX(`sequence_no`) AS max_seq
              FROM `message`
             GROUP BY `conversation_id`
        ) m ON m.`conversation_id` = c.`id`
        SET c.`last_message_seq` = COALESCE(m.`max_seq`, 0);
    END IF;
END$$

DELIMITER ;

CALL `ails_migrate_phase54_message_seq`();

DROP PROCEDURE IF EXISTS `ails_migrate_phase54_message_seq`;

-- 迁移后只读核对
SELECT `column_name`, `column_type`, `is_nullable`, `column_default`
  FROM information_schema.columns
 WHERE table_schema = DATABASE()
   AND table_name = 'conversation'
   AND `column_name` = 'last_message_seq';
