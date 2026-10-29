#!/usr/bin/env bash
# =============================================================
# Phase 8：旧库 → Phase 7 结构升级的可复现验证
#
# 做三件事：
#   1) 用 Phase 7 之前的 schema 建一个「老库」，写入真实形态的旧数据；
#   2) 先不迁移，直接跑当前 schema.sql —— 复现 message.truncated 缺失导致的
#      ERROR 1054（证明不能只靠 CREATE TABLE IF NOT EXISTS）；
#   3) 执行 schema-migration-phase7-truncation.sql，逐条验收五项要求。
#
# 用法（需要一个有建库权限的 MySQL 账号）：
#   MYSQL_PASSWORD=*** ./verify-upgrade.sh
#
# 可用环境变量覆盖连接信息：MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / DB
# 脚本会 DROP 并重建 $DB，不要指向业务库。
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

MYSQL_HOST="${MYSQL_HOST:-127.0.0.1}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
MYSQL_USER="${MYSQL_USER:-root}"
DB="${DB:-ails_p8_migration}"
: "${MYSQL_PASSWORD:?请设置 MYSQL_PASSWORD（本机 MySQL 密码）}"

export MYSQL_PWD="$MYSQL_PASSWORD"
MYSQL=(mysql -h"$MYSQL_HOST" -P"$MYSQL_PORT" -u"$MYSQL_USER" --default-character-set=utf8mb4)

OLD_SCHEMA="$SCRIPT_DIR/schema-before-phase7.sql"
CURRENT_SCHEMA="$REPO_ROOT/backend/backend/src/main/resources/schema.sql"
MIGRATION="$REPO_ROOT/backend/backend/src/main/resources/schema-migration-phase7-truncation.sql"

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "== 0. 用 Phase 7 之前的 schema 建库（模拟线上老库）=="
"${MYSQL[@]}" -e "DROP DATABASE IF EXISTS \`$DB\`; CREATE DATABASE \`$DB\` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
"${MYSQL[@]}" "$DB" < "$OLD_SCHEMA"
echo "   old schema loaded: $(grep -c '^CREATE TABLE' "$OLD_SCHEMA") tables"

echo "== 1. 写入老数据（迁移前就已存在的消息 / 运行记录）=="
"${MYSQL[@]}" "$DB" <<'SQL'
INSERT INTO `user`(username, password_hash) VALUES ('legacy', 'x');
INSERT INTO learning_space(user_id, name) VALUES (1, 'legacy space');
INSERT INTO conversation(user_id, learning_space_id, title) VALUES (1, 1, 'legacy conv');
INSERT INTO message(conversation_id, role, message_type, content, sequence_no)
VALUES (1, 'USER', 'TEXT', '历史问题', 1),
       (1, 'ASSISTANT', 'TEXT', '历史回答', 2);
INSERT INTO agent_run(user_id, conversation_id, run_type, status)
VALUES (1, 1, 'CHAT', 'COMPLETED');
SQL

echo "== 2. 未迁移，直接执行当前 schema.sql（Spring 启动会做的事）=="
"${MYSQL[@]}" "$DB" < "$CURRENT_SCHEMA"

has_col=$("${MYSQL[@]}" -N -B "$DB" -e "SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema='$DB' AND table_name='message' AND column_name='truncated';")
[ "$has_col" = "0" ] || fail "预期未迁移时 message.truncated 不存在，实际已存在"
echo "   message.truncated 缺失（schema.sql 的 CREATE IF NOT EXISTS 不会补列）"

enum_now=$("${MYSQL[@]}" -N -B "$DB" -e "SELECT column_type FROM information_schema.columns
  WHERE table_schema='$DB' AND table_name='agent_run' AND column_name='status';")
case "$enum_now" in
  *TRUNCATED*) echo "   agent_run.status 已由 schema.sql 末尾的幂等 ALTER 补上 TRUNCATED" ;;
  *) fail "agent_run.status 未包含 TRUNCATED: $enum_now" ;;
esac

if "${MYSQL[@]}" "$DB" -e "INSERT INTO message(conversation_id, role, message_type, content, truncated, sequence_no)
     VALUES (1, 'ASSISTANT', 'TEXT', '截断消息', 1, 3);" 2>/tmp/p8_mig_err; then
  fail "未迁移时写入 truncated 竟然成功了，缺口复现失败"
else
  grep -q "1054" /tmp/p8_mig_err || fail "预期 ERROR 1054，实际: $(cat /tmp/p8_mig_err)"
  echo "   复现 ERROR 1054: $(cat /tmp/p8_mig_err)"
fi

echo "== 3. 执行迁移脚本（连续两次，验证幂等）=="
"${MYSQL[@]}" "$DB" < "$MIGRATION"
"${MYSQL[@]}" "$DB" < "$MIGRATION" >/dev/null
echo "   migration applied twice"

echo "== 4. 五项验收 =="

old_readable=$("${MYSQL[@]}" -N -B "$DB" -e "SELECT COUNT(*) FROM message WHERE id IN (1,2) AND content IN ('历史问题','历史回答');")
[ "$old_readable" = "2" ] || fail "旧消息读取失败（期望 2 行）"
echo "   [1] 旧消息仍可读取: $old_readable / 2"

old_flag=$("${MYSQL[@]}" -N -B "$DB" -e "SELECT COUNT(*) FROM message WHERE id IN (1,2) AND truncated = 0;")
[ "$old_flag" = "2" ] || fail "旧消息 truncated 期望全为 0"
echo "   [2] 旧消息 truncated 默认 0: $old_flag / 2"

"${MYSQL[@]}" "$DB" -e "INSERT INTO message(conversation_id, role, message_type, content, sequence_no)
     VALUES (1, 'ASSISTANT', 'TEXT', '升级后正常新消息', 4);"
normal=$("${MYSQL[@]}" -N -B "$DB" -e "SELECT COUNT(*) FROM message WHERE sequence_no=4 AND truncated=0;")
[ "$normal" = "1" ] || fail "正常新消息持久化失败"
echo "   [3] 正常新消息持久化: ok（truncated=0）"

"${MYSQL[@]}" "$DB" -e "INSERT INTO message(conversation_id, role, message_type, content, truncated, sequence_no)
     VALUES (1, 'ASSISTANT', 'TEXT', '截断消息', 1, 5);"
trunc=$("${MYSQL[@]}" -N -B "$DB" -e "SELECT COUNT(*) FROM message WHERE sequence_no=5 AND truncated=1;")
[ "$trunc" = "1" ] || fail "截断消息持久化失败"
echo "   [4] 截断消息持久化: ok（truncated=1）"

"${MYSQL[@]}" "$DB" -e "INSERT INTO agent_run(user_id, conversation_id, run_type, status)
     VALUES (1, 1, 'CHAT', 'TRUNCATED');"
truncated_run=$("${MYSQL[@]}" -N -B "$DB" -e "SELECT COUNT(*) FROM agent_run WHERE status='TRUNCATED';")
[ "$truncated_run" = "1" ] || fail "agent_run 不接受 TRUNCATED"
echo "   [5] agent_run.status 接受 TRUNCATED: ok"

old_run=$("${MYSQL[@]}" -N -B "$DB" -e "SELECT COUNT(*) FROM agent_run WHERE status='COMPLETED';")
[ "$old_run" = "1" ] || fail "旧 agent_run 状态被改动"
echo "   [5b] 旧 agent_run 状态保持 COMPLETED: ok"

if "${MYSQL[@]}" "$DB" -e "INSERT INTO agent_run(user_id, conversation_id, run_type, status)
     VALUES (1, 1, 'CHAT', 'NOT_A_STATUS');" 2>/dev/null; then
  fail "未知状态被数据库接受，约束失效"
fi
echo "   [5c] 未知状态仍被数据库约束拒绝: ok"

echo
echo "ALL MIGRATION CHECKS PASSED ($DB)"
