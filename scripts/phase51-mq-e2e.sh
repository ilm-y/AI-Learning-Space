#!/usr/bin/env bash
# 阶段 5.1 的 MQ 异步链路验收：真实 RabbitMQ + 真 Worker + mq 模式后端。
#
# 与 phase51-e2e.sh 的分工：
#   - phase51-e2e.sh 走默认的 inline 模式，验证学习闭环本身；
#   - 本脚本只验证「异步机制」的四件事：
#       1. 回复结束后任务处于 PENDING、分析尚未完成；
#       2. Worker 消费后任务 SUCCEEDED，Evidence/Revision 落库；
#       3. 同一 task 的重复投递不会产生第二条 Evidence / Revision；
#       4. 重复结果被 Result Consumer 识别为 DUPLICATE（后端日志）。
#
# 前置（本脚本不负责启动这两个长驻进程）：
#   # 后端（mq 模式，8081）
#   cd backend/backend && SERVER_PORT=8081 AILS_ANALYSIS_MODE=mq \
#     AILS_ANALYSIS_COMPENSATION_ENABLED=false AILS_INTERNAL_TOKEN=phase51-internal-token \
#     ./mvnw -q -o spring-boot:run > /tmp/ails-backend-mq.log 2>&1
#   # Agent HTTP（mock）
#   cd agent && AILS_INTERNAL_TOKEN=phase51-internal-token python -m app.main
#   # RabbitMQ
#   cd docker && docker compose up -d
#
# 用法：
#   bash scripts/phase51-mq-e2e.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_SRC="$ROOT/backend/backend/src/main/resources"
BASE="${BASE:-http://127.0.0.1:8081}"
AGENT="${AGENT:-http://127.0.0.1:8000}"
BACKEND_LOG="${BACKEND_LOG:-/tmp/ails-backend-mq.log}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$ROOT/backend/backend/target/phase51-mq-e2e/$STAMP"
mkdir -p "$OUT"

read_creds() {
  python - "$BACKEND_SRC" <<'PY'
import os, re, sys
base = sys.argv[1]
def find(key):
    for name in ("application-local.yml", "application.yml"):
        path = os.path.join(base, name)
        if not os.path.exists(path):
            continue
        m = re.search(r'^\s*%s:\s*(.+?)\s*(?:#.*)?$' % key,
                      open(path, encoding="utf-8").read(), re.M)
        if not m:
            continue
        raw = m.group(1).strip().strip('"').strip("'")
        env = re.fullmatch(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}', raw)
        return os.environ.get(env.group(1), env.group(2) or "") if env else raw
    return ""
print(find("username"))
print(find("password"))
PY
}
CREDS="$(read_creds)"
MYSQL_USER="${MYSQL_USER:-$(printf '%s' "$CREDS" | sed -n 1p)}"
MYSQL_PASSWORD="${MYSQL_PASSWORD:-$(printf '%s' "$CREDS" | sed -n 2p)}"
MYSQL_HOST="${MYSQL_HOST:-127.0.0.1}"
MYSQL_DB="${MYSQL_DB:-ails}"
sql() { MYSQL_PWD="$MYSQL_PASSWORD" mysql -h"$MYSQL_HOST" -u"$MYSQL_USER" -N -B "$MYSQL_DB" -e "$1" 2>/dev/null; }

dlq_depth() {
  python - <<'PY'
import asyncio, aio_pika
async def main():
    conn = await aio_pika.connect_robust("amqp://guest:guest@127.0.0.1:5672/")
    ch = await conn.channel()
    q = await ch.declare_queue("learning.analysis.dlq", passive=True)
    print(q.declaration_result.message_count)
    await conn.close()
asyncio.run(main())
PY
}

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
check(){ if [ "$2" = "$3" ]; then ok "$1（= $3）"; else bad "$1：实际 [$2]，期望 [$3]"; fi; }
checknot(){ if [ "$2" != "$3" ]; then ok "$1（≠ $3，实际 $2）"; else bad "$1：不应等于 [$3]"; fi; }
section(){ printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

HTTP_CODE_FILE="$(mktemp)"
api() {
  local method="$1" path="$2" body="${3:-}" out
  if [ -n "$body" ]; then
    out=$(curl -s -w '\n%{http_code}' -X "$method" "$BASE$path" \
      -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" -d "$body")
  else
    out=$(curl -s -w '\n%{http_code}' -X "$method" "$BASE$path" \
      -H "Authorization: Bearer $TOKEN")
  fi
  printf '%s' "${out##*$'\n'}" > "$HTTP_CODE_FILE"
  printf '%s' "${out%$'\n'*}"
}
jqv(){ printf '%s' "$1" | python -c "
import sys, json
d = json.load(sys.stdin)
expr = sys.argv[1]
print(eval(('d' + expr) if expr.startswith('[') else expr))
" "$2" 2>/dev/null; }

wait_for_analysis_row() { # wait_for_analysis_row <conversationId> <seconds>
  local cid="$1" secs="${2:-30}" i=0
  while [ "$i" -lt "$secs" ]; do
    local id
    id=$(sql "SELECT id FROM agent_run WHERE conversation_id=$cid AND run_type='ANALYSIS' ORDER BY id DESC LIMIT 1;")
    if [ -n "$id" ]; then echo "$id"; return 0; fi
    sleep 1; i=$((i+1))
  done
  return 1
}

wait_for_run_status() { # wait_for_run_status <runId> <status> <seconds>
  local rid="$1" want="$2" secs="${3:-60}" i=0
  while [ "$i" -lt "$secs" ]; do
    if [ "$(sql "SELECT status FROM agent_run WHERE id=$rid;")" = "$want" ]; then return 0; fi
    sleep 1; i=$((i+1))
  done
  return 1
}

WORKER_PID=""
cleanup() {
  if [ -n "$WORKER_PID" ]; then
    kill "$WORKER_PID" 2>/dev/null || true
    wait "$WORKER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# =====================================================================
section "0. 前置检查"
if ! curl -s -m 3 "$AGENT/health" > /dev/null; then echo "Agent 未启动：$AGENT"; exit 1; fi
if ! curl -s -m 3 "$BASE/api/auth/login" -X POST -H 'Content-Type: application/json' -d '{}' > /dev/null; then
  echo "mq 模式后端未启动：$BASE"; exit 1
fi
if ! docker ps --format '{{.Names}}' | grep -q 'ails-rabbitmq'; then echo "RabbitMQ 容器未启动"; exit 1; fi
# 前置必须没有 Worker 在消费，否则无法观察 PENDING 窗口。
if tasklist 2>/dev/null | grep -qi 'python'; then
  : # 不粗暴判定：下面用队列/运行状态观察，而不是依赖进程名
fi
if sql "SELECT COUNT(*) FROM agent_run WHERE status='PENDING';" | grep -qv '^0$'; then
  echo "提示：库中已有 PENDING 的 agent_run（可能是上一次运行遗留），本次用新对话隔离。"
fi
ok "Agent / mq 后端 / RabbitMQ 均可用"

REG=$(curl -s -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"p51mq_$STAMP\",\"password\":\"Passw0rd!23\"}")
TOKEN=$(jqv "$REG" "['data']['token']")
USER_ID=$(jqv "$REG" "['data']['user']['id']")
checknot "注册并取得 token" "$TOKEN" ""
SPACE_ID=$(jqv "$(api POST /api/spaces "{\"name\":\"阶段5.1 MQ $STAMP\"}")" "['data']['id']")
TLB=$(jqv "$(api POST "/api/spaces/$SPACE_ID/concepts" \
  '{"canonicalName":"TLB","displayName":"TLB","description":"页表项缓存"}')" "['data']['id']")
checknot "创建空间与概念" "$TLB" ""

# =====================================================================
section "1. 回复结束后：任务 PENDING，分析尚未完成"
CA=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"MQ 会话"}')" "['data']['id']")
WRONG='我理解 TLB 就是页表本身，它缓存整个页表。'
SSE="$OUT/chat.sse"
curl -sN -m 60 -X POST "$BASE/api/conversations/$CA/messages" \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
  -d "$(python -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}, ensure_ascii=False))' "$WRONG")" \
  > "$SSE"
case "$(tr -d '\r' < "$SSE" | grep '^event:' | tr '\n' ' ')" in
  *"message.end"*) ok "SSE 对话完成（分析尚未完成）";;
  *) bad "SSE 未正常结束";;
esac

RUN_ID=$(wait_for_analysis_row "$CA" 30)
if [ -z "$RUN_ID" ]; then
  bad "没有创建 ANALYSIS agent_run"
else
  ok "创建了 ANALYSIS agent_run（id=$RUN_ID）"
fi
STATUS_BEFORE=$(sql "SELECT status FROM agent_run WHERE id=$RUN_ID;")
check "Worker 未启动时任务保持 PENDING（未被伪造成已完成）" "$STATUS_BEFORE" "PENDING"
sleep 2
check "Worker 未启动时没有 Evidence 落库" \
  "$(sql "SELECT COUNT(*) FROM learning_evidence WHERE concept_id=$TLB;")" "0"

# 构造一条与原任务同 task_id 的重复投递（模拟 MQ 至少一次投递）。
MSG_ID=$(jqv "$(api GET "/api/conversations/$CA/messages")" "next(m['id'] for m in d['data'] if m['role']=='USER')")
DUP_TASK="$OUT/duplicate-task.json"
python - "$RUN_ID" "$USER_ID" "$CA" "$SPACE_ID" "$MSG_ID" "$WRONG" "$DUP_TASK" <<'PY'
import json, sys
run_id, uid, cid, sid, mid, text, out = sys.argv[1:8]
task = {
    "schemaVersion": 1,
    "taskId": int(run_id),
    "runId": int(run_id),
    "traceId": "phase51-mq-duplicate",
    "userId": int(uid),
    "conversationId": int(cid),
    "learningSpaceId": int(sid),
    "messageId": int(mid),
    "userMessage": text,
    "history": [],
}
json.dump(task, open(out, "w", encoding="utf-8"), ensure_ascii=False)
PY
python - "$DUP_TASK" <<'PY'
import asyncio, json, sys
import aio_pika
from aio_pika import DeliveryMode, Message
async def main():
    body = open(sys.argv[1], "rb").read()
    conn = await aio_pika.connect_robust("amqp://guest:guest@127.0.0.1:5672/")
    ch = await conn.channel()
    ex = await ch.declare_exchange("learning.analysis.exchange", aio_pika.ExchangeType.DIRECT, durable=True)
    await ex.publish(
        Message(body=body, content_type="application/json",
                delivery_mode=DeliveryMode.PERSISTENT,
                headers={"x-analysis-duplicate": True}),
        routing_key="analysis.request", mandatory=True,
    )
    await conn.close()
asyncio.run(main())
print("duplicate task published")
PY
ok "已向 request 队列投递同 taskId 的重复副本"

# =====================================================================
section "2. 启动 Worker：消费、写库、重复结果被识别"
WORKER_LOG="$OUT/worker.log"
# exec 让子 shell 被 python 替换，$! 才是 Worker 本体的 PID —— 否则 kill 只杀掉
# 外层 bash，python 会继续消费队列，后续的「失败场景」就观察不到 PENDING 窗口。
( cd "$ROOT/agent" && exec env AILS_INTERNAL_TOKEN=phase51-internal-token \
  AILS_BACKEND_BASE_URL="$BASE" PYTHONUNBUFFERED=1 python -m app.worker ) > "$WORKER_LOG" 2>&1 &
WORKER_PID=$!
for i in $(seq 1 30); do grep -q "分析 Worker 已启动" "$WORKER_LOG" 2>/dev/null && break; sleep 1; done
grep -q "分析 Worker 已启动" "$WORKER_LOG" && ok "Worker 已连接 RabbitMQ" || bad "Worker 未在 30 秒内启动"

if wait_for_run_status "$RUN_ID" "SUCCEEDED" 90; then
  ok "Worker 消费后 ANALYSIS run 变为 SUCCEEDED"
else
  bad "90 秒内 run 未变为 SUCCEEDED（实际 $(sql "SELECT status FROM agent_run WHERE id=$RUN_ID;")）"
fi
EV_COUNT=$(sql "SELECT COUNT(*) FROM learning_evidence WHERE concept_id=$TLB;")
check "Evidence 落库且只有一条" "$EV_COUNT" "1"
REV_COUNT=$(sql "SELECT COUNT(*) FROM understanding_revision WHERE understanding_id=(SELECT id FROM learning_understanding WHERE user_id=$USER_ID AND concept_id=$TLB);")
check "Understanding 只产生一条 Revision" "$REV_COUNT" "1"
check "run 的 output_summary 记录了写入摘要" \
  "$(sql "SELECT output_summary FROM agent_run WHERE id=$RUN_ID;" | grep -c 'evidence=1')" "1"

sleep 4
check "重复任务没有新增 Evidence" \
  "$(sql "SELECT COUNT(*) FROM learning_evidence WHERE concept_id=$TLB;")" "1"
check "重复任务没有新增 Revision" \
  "$(sql "SELECT COUNT(*) FROM understanding_revision WHERE understanding_id=(SELECT id FROM learning_understanding WHERE user_id=$USER_ID AND concept_id=$TLB);")" "1"
check "Worker 处理了两条 taskId 相同的消息" \
  "$(grep -c "分析完成, taskId=$RUN_ID" "$WORKER_LOG")" "2"
if [ -f "$BACKEND_LOG" ]; then
  check "Result Consumer 第二次识别为 DUPLICATE" \
    "$(grep -c "分析结果重复投递，已忽略, taskId=$RUN_ID" "$BACKEND_LOG")" "1"
else
  echo "  （未找到 $BACKEND_LOG，跳过 DUPLICATE 日志断言；可用 BACKEND_LOG=... 指定）"
fi

# =====================================================================
section "3. Worker 永久失败：任务落 FAILED，不写学情"
# 停掉 Worker，先让新任务停在 PENDING，再清掉合法任务，只投一条 schemaVersion
# 不受支持的坏消息。这样失败是真实 Worker 产生的，而不是在数据库里造出来的。
kill "$WORKER_PID" 2>/dev/null || true
wait "$WORKER_PID" 2>/dev/null || true
WORKER_PID=""

CF=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"失败路径"}')" "['data']['id']")
curl -sN -m 60 -X POST "$BASE/api/conversations/$CF/messages" \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
  -d '{"content":"TLB 是什么？"}' > "$OUT/failure-chat.sse"
RUN_FAIL=$(wait_for_analysis_row "$CF" 30)
if [ -z "$RUN_FAIL" ]; then
  bad "失败场景没有创建 ANALYSIS agent_run"
else
  check "失败场景任务先进入 PENDING" \
    "$(sql "SELECT status FROM agent_run WHERE id=$RUN_FAIL;")" "PENDING"
fi
FAIL_MSG_ID=$(jqv "$(api GET "/api/conversations/$CF/messages")" "next(m['id'] for m in d['data'] if m['role']=='USER')")

# 清空 request 队列：只投坏消息，避免合法任务先成功把 run 推进到 SUCCEEDED。
python - <<'PY'
import asyncio, aio_pika
async def main():
    conn = await aio_pika.connect_robust("amqp://guest:guest@127.0.0.1:5672/")
    ch = await conn.channel()
    q = await ch.declare_queue("learning.analysis.request", passive=True)
    n = await q.purge()
    print(f"purged {getattr(n, 'message_count', '?')} request message(s)")
    await conn.close()
asyncio.run(main())
PY
DLQ_BEFORE=$(dlq_depth)

FAIL_TRACE=$(sql "SELECT trace_id FROM agent_run WHERE id=$RUN_FAIL;")
BAD_TASK="$OUT/bad-task.json"
python - "$RUN_FAIL" "$USER_ID" "$CF" "$SPACE_ID" "$FAIL_MSG_ID" "$FAIL_TRACE" "$BAD_TASK" <<'PY'
import json, sys
run_id, uid, cid, sid, mid, trace, out = sys.argv[1:8]
task = {
    "schemaVersion": 2,          # 当前只支持 1：Worker 应直接落 FAILED + 死信
    "taskId": int(run_id),
    "runId": int(run_id),
    "traceId": trace,
    "userId": int(uid),
    "conversationId": int(cid),
    "learningSpaceId": int(sid),
    "messageId": int(mid),
    "userMessage": "TLB 是什么？",
    "history": [],
}
json.dump(task, open(out, "w", encoding="utf-8"), ensure_ascii=False)
PY
python - "$BAD_TASK" <<'PY'
import asyncio, sys
import aio_pika
from aio_pika import DeliveryMode, Message
async def main():
    body = open(sys.argv[1], "rb").read()
    conn = await aio_pika.connect_robust("amqp://guest:guest@127.0.0.1:5672/")
    ch = await conn.channel()
    ex = await ch.declare_exchange("learning.analysis.exchange", aio_pika.ExchangeType.DIRECT, durable=True)
    await ex.publish(Message(body=body, content_type="application/json",
                             delivery_mode=DeliveryMode.PERSISTENT),
                     routing_key="analysis.request", mandatory=True)
    await conn.close()
asyncio.run(main())
PY

WORKER_LOG_FAIL="$OUT/worker-failure.log"
( cd "$ROOT/agent" && exec env AILS_INTERNAL_TOKEN=phase51-internal-token \
  AILS_BACKEND_BASE_URL="$BASE" PYTHONUNBUFFERED=1 python -m app.worker ) > "$WORKER_LOG_FAIL" 2>&1 &
WORKER_PID=$!
if wait_for_run_status "$RUN_FAIL" "FAILED" 60; then
  ok "Worker 永久失败后 run 变为 FAILED（没有被伪装成 SUCCEEDED）"
else
  bad "60 秒内 run 未变为 FAILED（实际 $(sql "SELECT status FROM agent_run WHERE id=$RUN_FAIL;")）"
fi
check "失败 run 的 output_summary 为空（不伪装成已完成）" \
  "$(sql "SELECT output_summary IS NULL FROM agent_run WHERE id=$RUN_FAIL;")" "1"
check "run 的 error_message 记录了失败分类" \
  "$(sql "SELECT error_message FROM agent_run WHERE id=$RUN_FAIL;" | grep -c 'TASK_SCHEMA_UNSUPPORTED')" "1"

# 现有设计：Agent 分析失败后，Result Processor 会用 rules-v1 兜底写一条
# OBSERVATION，避免这一轮证据彻底丢失。它不是「把失败伪装成成功」——
# 失败的 run 仍是 FAILED，兜底 run 单独存在，证据正确性是 UNKNOWN。
sleep 2
RUN_FALLBACK=$(sql "SELECT id FROM agent_run WHERE conversation_id=$CF AND run_type='ANALYSIS' AND status='COMPLETED' ORDER BY id DESC LIMIT 1;")
if [ -n "$RUN_FALLBACK" ]; then
  ok "存在独立的 rules-v1 兜底 run（id=$RUN_FALLBACK）"
else
  bad "没有找到兜底 run"
fi
check "兜底证据只声明 UNKNOWN 观察，不虚构掌握" \
  "$(sql "SELECT correctness FROM learning_evidence WHERE user_id=$USER_ID AND concept_id=$TLB AND first_seen_run_id=$RUN_FALLBACK;")" "UNKNOWN"
check "兜底证据标记为 rules-v1，不冒充 Agent 分析" \
  "$(sql "SELECT analyzer_version FROM learning_evidence WHERE user_id=$USER_ID AND concept_id=$TLB AND first_seen_run_id=$RUN_FALLBACK;")" "rules-v1"
check "失败 + 兜底没有新增 Revision（不产生学习结论）" \
  "$(sql "SELECT COUNT(*) FROM understanding_revision WHERE understanding_id=(SELECT id FROM learning_understanding WHERE user_id=$USER_ID AND concept_id=$TLB);")" "1"
check "失败后学习状态没有被推进为掌握" \
  "$(sql "SELECT status FROM learning_state WHERE user_id=$USER_ID AND concept_id=$TLB;")" "EXPOSED"
DLQ_AFTER=$(dlq_depth)
check "坏消息进入死信队列（深度 +1）" "$(( ${DLQ_AFTER:-0} - ${DLQ_BEFORE:-0} ))" "1"

section "4. 数据库事实"
sql "SELECT id, run_type, status, IFNULL(output_summary,'<null>'), IFNULL(error_message,'<null>') FROM agent_run WHERE conversation_id IN ($CA,$CF) ORDER BY id;"
sql "SELECT e.id, e.evidence_type, e.correctness, es.source_type, es.source_id FROM learning_evidence e LEFT JOIN evidence_source es ON es.evidence_id=e.id WHERE e.concept_id=$TLB;"
sql "SELECT revision_no, status, trigger_evidence_id FROM understanding_revision WHERE understanding_id=(SELECT id FROM learning_understanding WHERE user_id=$USER_ID AND concept_id=$TLB);"

printf '\n\033[1m结果：PASS=%d FAIL=%d\033[0m\n' "$PASS" "$FAIL"
printf '产物目录：%s\n' "$OUT"
echo "用户：p51mq_$STAMP (id=$USER_ID)  space=$SPACE_ID  TLB=$TLB  run=$RUN_ID"
if [ "$FAIL" -gt 0 ]; then exit 1; fi
echo "全部通过。"
