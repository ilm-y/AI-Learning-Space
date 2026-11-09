-- =============================================================
-- AI Learning Space — D4-4：agent_run 上下文 Manifest 持久化
--
-- 用途：把【已有库】的 agent_run 升级为带 context_manifest 列的结构。
-- 本脚本不会被 spring.sql.init 执行（它只加载 schema.sql），必须手工运行：
--
--     mysql -uroot -p ails < schema-migration-d4-4-context-manifest.sql
--
-- 执行前提：
--   1. 先备份 agent_run：
--        mysqldump -uroot -p ails agent_run > backup-d4-4-agent-run.sql
--   2. 停止后端，避免迁移期间有对话写入。
--   3. MySQL 8.0（项目现有验证版本 8.0.37）。
--
-- 为什么需要这个脚本：
--   schema.sql 全部是 CREATE TABLE IF NOT EXISTS，不会给已存在的表补列。
--   新库由 schema.sql 直接建出 context_manifest；已有库必须跑本脚本，
--   否则 ChatService 收尾时 UPDATE agent_run.context_manifest 会报
--   ERROR 1054 Unknown column 'context_manifest'，整轮对话被标记失败。
--
-- 边界（D4-3 契约）：
--   - 只存 schema_version / policy_version / intent / sources / budget；
--   - 每个 source 只存 type / source_id / retrieval_status / injection_status /
--     reason / estimated_tokens；
--   - 不存正文、完整 prompt、query 或异常原文。
--
-- 旧数据策略：
--   - 列允许 NULL；历史 run 没有 Manifest 是事实，不回填、不伪造空对象。
--   - 失败 run 也不会写入（Agent 只在 done 终态事件携带 Manifest）。
--
-- 幂等性：
--   - 先查 information_schema，列已存在则跳过；
--   - DDL 会隐式提交，中途失败后修复原因重跑即可从断点继续。
-- =============================================================

SET NAMES utf8mb4;

DROP PROCEDURE IF EXISTS `ails_migrate_d4_4_context_manifest`;

DELIMITER $$

CREATE PROCEDURE `ails_migrate_d4_4_context_manifest`()
BEGIN
    DECLARE v_agent_run INT DEFAULT 0;

    SELECT COUNT(*) INTO v_agent_run
      FROM information_schema.tables
     WHERE table_schema = DATABASE()
       AND table_name = 'agent_run';

    IF v_agent_run = 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'agent_run table not found: run schema.sql first on a new database';
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = 'agent_run'
           AND column_name = 'context_manifest'
    ) THEN
        ALTER TABLE `agent_run`
            ADD COLUMN `context_manifest` JSON DEFAULT NULL
                COMMENT '上下文 Manifest（D4-3 schema_version=1），只存来源与状态，不存正文'
                AFTER `output_summary`;
    END IF;
END $$

DELIMITER ;

CALL `ails_migrate_d4_4_context_manifest`();
DROP PROCEDURE `ails_migrate_d4_4_context_manifest`;
