-- =============================================================
-- AI Learning Space — 开发环境清库脚本
--
-- 本脚本【只负责 DROP】，不会被 spring.sql.init 执行，需要时手工运行：
--     mysql -uroot -p ails < schema-dev-reset.sql
--     mysql -uroot -p ails < schema.sql
--
-- 之所以和 schema.sql 分开：schema.sql 由 spring.sql.init 在【每次启动】时执行，
-- 如果把 DROP 写进去，`./mvnw spring-boot:run` 每重启一次就会清空一次数据库。
--
-- 会同时清掉 v1 遗留的 verification_task / verification_answer
-- （它们已按文档更名为 test_task / test_answer）。
-- =============================================================

SET FOREIGN_KEY_CHECKS = 0;

-- v1 遗留表（已更名，仅需清理一次，加上 IF EXISTS 保证可重复执行）
DROP TABLE IF EXISTS `verification_answer`;
DROP TABLE IF EXISTS `verification_task`;

-- 当前 22 张表，按外键依赖逆序
DROP TABLE IF EXISTS `document_chunk`;
DROP TABLE IF EXISTS `document`;
DROP TABLE IF EXISTS `agent_run`;
DROP TABLE IF EXISTS `test_answer`;
DROP TABLE IF EXISTS `test_task`;
DROP TABLE IF EXISTS `memory_source`;
DROP TABLE IF EXISTS `memory`;
DROP TABLE IF EXISTS `evidence_source`;
DROP TABLE IF EXISTS `understanding_revision`;
DROP TABLE IF EXISTS `learning_evidence`;
DROP TABLE IF EXISTS `learning_understanding`;
DROP TABLE IF EXISTS `learning_state`;
DROP TABLE IF EXISTS `learning_session`;
DROP TABLE IF EXISTS `learning_plan`;
DROP TABLE IF EXISTS `import_task`;
DROP TABLE IF EXISTS `highlight`;
DROP TABLE IF EXISTS `message`;
DROP TABLE IF EXISTS `conversation`;
DROP TABLE IF EXISTS `concept_relation`;
DROP TABLE IF EXISTS `concept`;
DROP TABLE IF EXISTS `learning_space`;
DROP TABLE IF EXISTS `user`;

SET FOREIGN_KEY_CHECKS = 1;
