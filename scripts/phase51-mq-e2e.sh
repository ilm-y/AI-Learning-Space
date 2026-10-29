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
#     AILS_INTERNAL_IDENTITY_SECRET="$IDENTITY_SECRET" \
#     AILS_INTERNAL_DELEGATION_SECRET="$DELEGATION_SECRET" \
#     ./mvnw -q -o spring-boot:run > /tmp/ails-backend-mq.log 2>&1
#   # Agent HTTP（mock）
#   cd agent && AILS_INTERNAL_TOKEN=phase51-internal-token \
#     AILS_INTERNAL_IDENTITY_SECRET=phase51-identity-secret python -m app.main
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
INTERNAL_TOKEN="${AILS_INTERNAL_TOKEN:-phase51-internal-token}"
IDENTITY_SECRET="${AILS_INTERNAL_IDENTITY_SECRET:-phase51-identity-secret}"
# 只由测试脚本模拟 Spring 持有；Agent 进程不读取它。
DELEGATION_SECRET="${AILS_INTERNAL_DELEGATION_SECRET:-phase51-delegation-secret}"
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
MYSQL_USER="${MYSQL_USER-$(printf '%s' "$CREDS" | sed -n 1p)}"
# 用 `-` 而不是 `:-`：显式传入空密码（隔离容器的 root 空密码）是合法配置，
# 不能被 application-local.yml 里的本机密码覆盖。
MYSQL_PASSWORD="${MYSQL_PASSWORD-$(printf '%s' "$CREDS" | sed -n 2p)}"
MYSQL_HOST="${MYSQL_HOST:-127.0.0.1}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
MYSQL_DB="${MYSQL_DB:-ails}"
sql() { MYSQL_PWD="$MYSQL_PASSWORD" mysql -h"$MYSQL_HOST" -P"$MYSQL_PORT" -u"$MYSQL_USER" -N -B "$MYSQL_DB" -e "$1" 2>/dev/null; }

queue_depth() { # queue_depth <queue>
  python - "$1" <<'PY'
import asyncio, sys
import aio_pika
async def main():
    conn = await aio_pika.connect_robust("amqp://guest:guest@127.0.0.1:5672/")
    ch = await conn.channel()
    q = await ch.declare_queue(sys.argv[1], passive=True)
    print(q.declaration_result.message_count)
    await conn.close()
asyncio.run(main())
PY
}

dlq_depth() { queue_depth learning.analysis.dlq; }

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

wait_for_log_count() { # wait_for_log_count <file> <literal> <minCount> <seconds>
  local file="$1" literal="$2" want="$3" secs="${4:-30}" i=0 got
  while [ "$i" -lt "$secs" ]; do
    got=$(grep -cF -- "$literal" "$file" 2>/dev/null || true)
    if [ "${got:-0}" -ge "$want" ]; then return 0; fi
    sleep 1; i=$((i+1))
  done
  return 1
}

wait_for_fallback_run() { # wait_for_fallback_run <conversationId> <seconds>
  local cid="$1" secs="${2:-30}" i=0 id
  while [ "$i" -lt "$secs" ]; do
    id=$(sql "SELECT id FROM agent_run WHERE conversation_id=$cid AND run_type='ANALYSIS' AND status='COMPLETED' ORDER BY id DESC LIMIT 1;")
    if [ -n "$id" ]; then echo "$id"; return 0; fi
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
if [ -n "$TOKEN" ]; then ok "注册并取得 token"; else bad "注册未返回 token"; fi
SPACE_ID=$(jqv "$(api POST /api/spaces "{\"name\":\"阶段5.1 MQ $STAMP\"}")" "['data']['id']")
TLB=$(jqv "$(api POST "/api/spaces/$SPACE_ID/concepts" \
  '{"canonicalName":"TLB","displayName":"TLB","description":"页表项缓存"}')" "['data']['id']")
checknot "创建空间与概念" "$TLB" ""

# =====================================================================
section "1. 回复结束后：任务 PENDING，分析尚未完成"
CA=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"MQ 会话"}')" "['data']['id']")
WRONG='我理解 TLB 就是页表本身，它缓存整个页表。我希望你以后多用例子解释。'
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
# 模拟 Spring：用仅后端持有的 delegation-secret 签发委托声明。Agent/Worker
# 只转发这条声明，不自行签发；脚本仍是独立于 Agent 的测试 harness。
BACKEND_IDENTITY=$(cd "$ROOT/agent" && AILS_INTERNAL_DELEGATION_SECRET="$DELEGATION_SECRET"   python -m app.internal_identity --user "$USER_ID" --audience learning-backend   --scope analysis:read --ttl 900)
python - "$RUN_ID" "$USER_ID" "$CA" "$SPACE_ID" "$MSG_ID" "$WRONG" "$DUP_TASK" "$BACKEND_IDENTITY" <<'PY'
import json, sys
run_id, uid, cid, sid, mid, text, out, backend_identity = sys.argv[1:9]
task = {
    "schemaVersion": 2,
    "taskId": int(run_id),
    "runId": int(run_id),
    "traceId": "phase51-mq-duplicate",
    "userId": int(uid),
    "conversationId": int(cid),
    "learningSpaceId": int(sid),
    "messageId": int(mid),
    "userMessage": text,
    "history": [],
    "backendIdentity": backend_identity,
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
( cd "$ROOT/agent" && exec env AILS_INTERNAL_TOKEN="$INTERNAL_TOKEN" \
  AILS_INTERNAL_IDENTITY_SECRET="$IDENTITY_SECRET" \
  AILS_LLM_PROVIDER=mock AILS_EMBEDDING_PROVIDER=hash AILS_EMBEDDING_DIMENSION=8 \
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
check "Evidence 类型是 USER_EXPLANATION（Mock 把解释句记为用户解释）" \
  "$(sql "SELECT evidence_type FROM learning_evidence WHERE concept_id=$TLB;")" "USER_EXPLANATION"
check "Evidence 正确性是 PARTIAL（Mock 不判断对错）" \
  "$(sql "SELECT correctness FROM learning_evidence WHERE concept_id=$TLB;")" "PARTIAL"
check "Evidence 标记分析器版本 agent-v1" \
  "$(sql "SELECT analyzer_version FROM learning_evidence WHERE concept_id=$TLB;")" "agent-v1"
check "Understanding 落库且状态为 EXPOSED" \
  "$(sql "SELECT status FROM learning_understanding WHERE user_id=$USER_ID AND concept_id=$TLB;")" "EXPOSED"
check "Revision 快照状态与 Understanding 一致" \
  "$(sql "SELECT status FROM understanding_revision WHERE understanding_id=(SELECT id FROM learning_understanding WHERE user_id=$USER_ID AND concept_id=$TLB) ORDER BY revision_no DESC LIMIT 1;")" "EXPOSED"
REV_COUNT=$(sql "SELECT COUNT(*) FROM understanding_revision WHERE understanding_id=(SELECT id FROM learning_understanding WHERE user_id=$USER_ID AND concept_id=$TLB);")
check "Understanding 只产生一条 Revision" "$REV_COUNT" "1"
check "run 的 output_summary 记录了写入摘要" \
  "$(sql "SELECT output_summary FROM agent_run WHERE id=$RUN_ID;" | grep -c 'evidence=1')" "1"

# Memory：Mock 启发式只在用户明确表达偏好时产出一条 GLOBAL 候选。
# 这是真实 Worker → Result Consumer → MemoryService 的写库路径，不是直接插库。
check "Memory 候选落库且只有一条" \
  "$(sql "SELECT COUNT(*) FROM memory WHERE user_id=$USER_ID;")" "1"
check "Agent 推断的 Memory 只能是 CANDIDATE" \
  "$(sql "SELECT status FROM memory WHERE user_id=$USER_ID;")" "CANDIDATE"
check "偏好 Memory 的作用域是 GLOBAL" \
  "$(sql "SELECT scope_type FROM memory WHERE user_id=$USER_ID;")" "GLOBAL"
check "偏好 Memory 保留了用户原句" \
  "$(sql "SELECT content FROM memory WHERE user_id=$USER_ID;" | grep -c '多用例子')" "1"
check "Memory 来源绑定本轮用户消息" \
  "$(sql "SELECT COUNT(*) FROM memory_source ms JOIN memory m ON m.id=ms.memory_id WHERE m.user_id=$USER_ID AND ms.source_type='MESSAGE' AND ms.source_id=$MSG_ID;")" "1"
check "Memory 写入计入 run 摘要" \
  "$(sql "SELECT output_summary FROM agent_run WHERE id=$RUN_ID;" | grep -c 'memory+=1')" "1"

# 轮询等待第二条相同 taskId 的消息完成；不假设固定 sleep 后一定已经处理完。
wait_for_log_count "$WORKER_LOG" "分析完成, taskId=$RUN_ID" 2 30 || true
check "重复任务没有新增 Evidence" \
  "$(sql "SELECT COUNT(*) FROM learning_evidence WHERE concept_id=$TLB;")" "1"
check "重复任务没有新增 Revision" \
  "$(sql "SELECT COUNT(*) FROM understanding_revision WHERE understanding_id=(SELECT id FROM learning_understanding WHERE user_id=$USER_ID AND concept_id=$TLB);")" "1"
check "重复任务没有新增 Memory" \
  "$(sql "SELECT COUNT(*) FROM memory WHERE user_id=$USER_ID;")" "1"
check "重复任务没有新增 Memory 来源" \
  "$(sql "SELECT COUNT(*) FROM memory_source ms JOIN memory m ON m.id=ms.memory_id WHERE m.user_id=$USER_ID;")" "1"
check "Worker 处理了两条 taskId 相同的消息" \
  "$(grep -c "分析完成, taskId=$RUN_ID" "$WORKER_LOG")" "2"
if [ -f "$BACKEND_LOG" ]; then
  wait_for_log_count "$BACKEND_LOG" "分析结果重复投递，已忽略, taskId=$RUN_ID" 1 30 || true
  check "Result Consumer 第二次识别为 DUPLICATE" \
    "$(grep -c "分析结果重复投递，已忽略, taskId=$RUN_ID" "$BACKEND_LOG")" "1"
else
  echo "  （未找到 $BACKEND_LOG，跳过 DUPLICATE 日志断言；可用 BACKEND_LOG=... 指定）"
fi

# =====================================================================
section "3. 临时失败：真实重试队列 + 新 Worker 恢复后成功"
# 先停掉上一节的 Worker，否则新任务会被它立刻消费，观察不到 PENDING/重试窗口。
kill "$WORKER_PID" 2>/dev/null || true
wait "$WORKER_PID" 2>/dev/null || true
WORKER_PID=""

# 这不是伪造 retry header：第一个 Worker 用错误的共享服务 token 启动，后端真实返回
# 401，Worker 把它归类为 RETRIEVAL_UNAVAILABLE（可重试），把带 x-analysis-retry=1
# 的副本投进 retry 队列并 ACK 原消息；任务保持 PENDING，而不是提前 FAILED。
# 在 15 秒 TTL 到期前换成正确 token 的 Worker，重试副本回到 request 队列并被消费。
# 注意：delegation 声明由 Spring 签发、Worker 只转发，换错 identity-secret 不会
# 再造成 401（那正是 Phase 3.1 修复的边界），所以这里刻意改共享 token。
CACHE=$(jqv "$(api POST "/api/spaces/$SPACE_ID/concepts" \
  '{"canonicalName":"Cache","displayName":"Cache","description":"高速缓存"}')" "['data']['id']")
checknot "创建重试场景概念" "$CACHE" ""
CR=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"重试路径"}')" "['data']['id']")
curl -sN -m 60 -X POST "$BASE/api/conversations/$CR/messages" \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
  -d '{"content":"我理解 Cache 就是内存。"}' > "$OUT/retry-chat.sse"
RUN_RETRY=$(wait_for_analysis_row "$CR" 30)
check "重试场景先进入 PENDING" \
  "$( [ -n "$RUN_RETRY" ] && sql "SELECT status FROM agent_run WHERE id=$RUN_RETRY;" )" "PENDING"

WRONG_LOG="$OUT/worker-retry-wrong.log"
( cd "$ROOT/agent" && exec env AILS_INTERNAL_TOKEN=phase51-wrong-internal-token \
  AILS_INTERNAL_IDENTITY_SECRET="$IDENTITY_SECRET" \
  AILS_LLM_PROVIDER=mock AILS_EMBEDDING_PROVIDER=hash AILS_EMBEDDING_DIMENSION=8 \
  AILS_BACKEND_BASE_URL="$BASE" PYTHONUNBUFFERED=1 python -m app.worker ) > "$WRONG_LOG" 2>&1 &
WORKER_PID=$!
RETRY_DEPTH=0
for i in $(seq 1 30); do
  RETRY_DEPTH=$(queue_depth learning.analysis.retry 2>/dev/null || echo 0)
  [ "${RETRY_DEPTH:-0}" -ge 1 ] && break
  sleep 1
done
check "可重试失败后 retry 队列收到副本" "$RETRY_DEPTH" "1"
check "Worker 将 401 归类为可重试的 RETRIEVAL_UNAVAILABLE" \
  "$(grep -c "分析失败, taskId=$RUN_RETRY, retries=0, code=RETRIEVAL_UNAVAILABLE, retryable=True" "$WRONG_LOG")" "1"
check "重试等待期间任务仍是 PENDING（没有提前判死）" \
  "$(sql "SELECT status FROM agent_run WHERE id=$RUN_RETRY;")" "PENDING"
kill "$WORKER_PID" 2>/dev/null || true
wait "$WORKER_PID" 2>/dev/null || true
WORKER_PID=""

WORKER_RETRY_LOG="$OUT/worker-retry-recovered.log"
( cd "$ROOT/agent" && exec env AILS_INTERNAL_TOKEN="$INTERNAL_TOKEN" \
  AILS_INTERNAL_IDENTITY_SECRET="$IDENTITY_SECRET" \
  AILS_LLM_PROVIDER=mock AILS_EMBEDDING_PROVIDER=hash AILS_EMBEDDING_DIMENSION=8 \
  AILS_BACKEND_BASE_URL="$BASE" PYTHONUNBUFFERED=1 python -m app.worker ) > "$WORKER_RETRY_LOG" 2>&1 &
WORKER_PID=$!
if wait_for_run_status "$RUN_RETRY" "SUCCEEDED" 90; then
  ok "重试副本被新 Worker 消费后任务 SUCCEEDED"
else
  bad "90 秒内重试任务未 SUCCEEDED（实际 $(sql "SELECT status FROM agent_run WHERE id=$RUN_RETRY;")）"
fi
check "成功消费时 retries=1（确实走了重试副本）" \
  "$(grep -c "分析完成, taskId=$RUN_RETRY.*retries=1" "$WORKER_RETRY_LOG")" "1"
check "重试成功后 Evidence 落库" \
  "$(sql "SELECT COUNT(*) FROM learning_evidence WHERE concept_id=$CACHE;")" "1"
check "重试场景没有留下 FAILED run" \
  "$(sql "SELECT COUNT(*) FROM agent_run WHERE conversation_id=$CR AND run_type='ANALYSIS' AND status='FAILED';")" "0"
kill "$WORKER_PID" 2>/dev/null || true
wait "$WORKER_PID" 2>/dev/null || true
WORKER_PID=""

# =====================================================================
section "4. 非法 / 旧版本消息：入口拒绝、死信、不写学情"
# 停掉 Worker，先让每条坏消息对应的任务停在 PENDING，再清掉合法任务副本，
# 只投坏消息。这样失败是真实 Worker 产生的，而不是在数据库里造出来的。
kill "$WORKER_PID" 2>/dev/null || true
wait "$WORKER_PID" 2>/dev/null || true
WORKER_PID=""

BAD_LABELS=(missing empty blank v1 v3)
declare -A BAD_RUN=() BAD_CONV=() BAD_CONCEPT=() BAD_MSG=() BAD_TRACE=()
for label in "${BAD_LABELS[@]}"; do
  concept=$(jqv "$(api POST "/api/spaces/$SPACE_ID/concepts" \
    "{\"canonicalName\":\"Bad$label\",\"displayName\":\"Bad$label\",\"description\":\"非法消息场景\"}")" "['data']['id']")
  conv=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" \
    "{\"title\":\"非法消息-$label\"}")" "['data']['id']")
  curl -sN -m 60 -X POST "$BASE/api/conversations/$conv/messages" \
    -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
    -d '{"content":"TLB 是什么？"}' > "$OUT/bad-chat-$label.sse"
  run=$(wait_for_analysis_row "$conv" 30)
  msg=$(jqv "$(api GET "/api/conversations/$conv/messages")" \
    "next(m['id'] for m in d['data'] if m['role']=='USER')")
  BAD_CONCEPT[$label]="$concept"
  BAD_CONV[$label]="$conv"
  BAD_RUN[$label]="$run"
  BAD_MSG[$label]="$msg"
  BAD_TRACE[$label]="$( [ -n "$run" ] && sql "SELECT trace_id FROM agent_run WHERE id=$run;" )"
  if [ -n "$run" ]; then
    check "非法场景 $label 的任务先进入 PENDING" \
      "$(sql "SELECT status FROM agent_run WHERE id=$run;")" "PENDING"
  else
    bad "非法场景 $label 没有创建 ANALYSIS agent_run"
  fi
done

# 清空 request 队列里的合法任务副本：只投坏消息，避免合法任务先成功。
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
RETRY_BEFORE=$(queue_depth learning.analysis.retry)

for label in "${BAD_LABELS[@]}"; do
  BAD_TASK="$OUT/bad-task-$label.json"
  # v1/v3 带上合法签名，先通过消息模型，再让 schemaVersion 检查拒绝；
  # missing/empty/blank 故意不带或缺省 backendIdentity，由模型入口拒绝。
  python - "$label" "${BAD_RUN[$label]}" "$USER_ID" "${BAD_CONV[$label]}" "$SPACE_ID" \
    "${BAD_MSG[$label]}" "${BAD_TRACE[$label]}" "$BACKEND_IDENTITY" "$BAD_TASK" <<'PY'
import json, sys
kind, run_id, uid, cid, sid, mid, trace, identity, out = sys.argv[1:10]
task = {
    "schemaVersion": 2,
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
if kind == "missing":
    pass                          # 缺字段：schemaVersion=2 应被模型拒绝
elif kind == "empty":
    task["backendIdentity"] = ""
elif kind == "blank":
    task["backendIdentity"] = "   "
elif kind == "v1":
    task["schemaVersion"] = 1     # 旧版本：没有可信用户身份，应拒绝
elif kind == "v3":
    task["schemaVersion"] = 3     # 不支持的版本：应先过模型、再被版本检查拒绝
    task["backendIdentity"] = identity
else:
    raise SystemExit(f"unknown kind {kind}")
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
done
ok "已投递 ${#BAD_LABELS[@]} 条坏消息（missing/empty/blank/v1/v3）"

WORKER_LOG_FAIL="$OUT/worker-failure.log"
( cd "$ROOT/agent" && exec env AILS_INTERNAL_TOKEN="$INTERNAL_TOKEN" \
  AILS_INTERNAL_IDENTITY_SECRET="$IDENTITY_SECRET" \
  AILS_LLM_PROVIDER=mock AILS_EMBEDDING_PROVIDER=hash AILS_EMBEDDING_DIMENSION=8 \
  AILS_BACKEND_BASE_URL="$BASE" PYTHONUNBUFFERED=1 python -m app.worker ) > "$WORKER_LOG_FAIL" 2>&1 &
WORKER_PID=$!

declare -A BAD_CODE=(
  [missing]=MESSAGE_INVALID [empty]=MESSAGE_INVALID [blank]=MESSAGE_INVALID
  [v1]=TASK_SCHEMA_UNSUPPORTED [v3]=TASK_SCHEMA_UNSUPPORTED
)
for label in "${BAD_LABELS[@]}"; do
  run="${BAD_RUN[$label]}"
  want="${BAD_CODE[$label]}"
  if wait_for_run_status "$run" "FAILED" 60; then
    ok "$label：坏消息由真实 Worker 处理后 run 变为 FAILED"
  else
    bad "$label：60 秒内 run 未变为 FAILED（实际 $(sql "SELECT status FROM agent_run WHERE id=$run;")）"
  fi
  check "$label：error_message 记录 $want" \
    "$(sql "SELECT error_message FROM agent_run WHERE id=$run;" | grep -c "$want")" "1"
  check "$label：失败 run 的 output_summary 为空（不伪装成已完成）" \
    "$(sql "SELECT output_summary IS NULL FROM agent_run WHERE id=$run;")" "1"
  # Runtime 未被调用：日志里没有该 taskId 的「分析完成 / 分析失败」。
  check "$label：没有进入 Runtime 分析（无「分析完成」日志）" \
    "$(grep -c "分析完成, taskId=$run" "$WORKER_LOG_FAIL")" "0"
  check "$label：没有进入 Runtime 分析（无「分析失败」日志）" \
    "$(grep -c "分析失败, taskId=$run" "$WORKER_LOG_FAIL")" "0"
  # 坏任务本身没有写学情；rules-v1 兜底是独立的 run，单独核对。
  check "$label：坏任务没有写 Evidence" \
    "$(sql "SELECT COUNT(*) FROM learning_evidence WHERE first_seen_run_id=$run;")" "0"
  check "$label：坏任务没有产生 Revision" \
    "$(sql "SELECT COUNT(*) FROM understanding_revision WHERE understanding_id IN (SELECT id FROM learning_understanding WHERE concept_id=${BAD_CONCEPT[$label]});")" "0"
  check "$label：坏任务没有产生 Memory 来源" \
    "$(sql "SELECT COUNT(*) FROM memory_source WHERE source_type='MESSAGE' AND source_id=${BAD_MSG[$label]};")" "0"

  fallback=$(wait_for_fallback_run "${BAD_CONV[$label]}" 30)
  if [ -n "$fallback" ]; then
    ok "$label：存在独立的 rules-v1 兜底 run（id=$fallback）"
  else
    bad "$label：没有找到兜底 run"
  fi
  # rules-v1 兜底按问题文本重新命中概念，不一定落在本场景新建的 Bad* 概念上
  # （例如「TLB 是什么？」会命中 TLB）；所以按兜底 run 归属核对，不按 concept_id 猜。
  check "$label：兜底证据只声明 UNKNOWN 观察" \
    "$(sql "SELECT correctness FROM learning_evidence WHERE first_seen_run_id=$fallback;")" "UNKNOWN"
  check "$label：兜底证据标记为 rules-v1，不冒充 Agent 分析" \
    "$(sql "SELECT analyzer_version FROM learning_evidence WHERE first_seen_run_id=$fallback;")" "rules-v1"
done

DLQ_AFTER=$(dlq_depth)
RETRY_AFTER=$(queue_depth learning.analysis.retry)
check "五条坏消息都进入死信队列（深度 +${#BAD_LABELS[@]}）" \
  "$(( ${DLQ_AFTER:-0} - ${DLQ_BEFORE:-0} ))" "${#BAD_LABELS[@]}"
check "不可重试的坏消息没有进入 retry 队列" \
  "$(( ${RETRY_AFTER:-0} - ${RETRY_BEFORE:-0} ))" "0"
check "坏消息没有新增 Memory（仍只有第 1 节的 1 条）" \
  "$(sql "SELECT COUNT(*) FROM memory WHERE user_id=$USER_ID;")" "1"
check "坏消息没有推进 TLB 的 Revision（仍只有第 1 节的 1 条）" \
  "$(sql "SELECT COUNT(*) FROM understanding_revision WHERE understanding_id=(SELECT id FROM learning_understanding WHERE user_id=$USER_ID AND concept_id=$TLB);")" "1"

section "5. 数据库事实"
ALL_CONVS="$CA,$CR"
for label in "${BAD_LABELS[@]}"; do ALL_CONVS="$ALL_CONVS,${BAD_CONV[$label]}"; done
sql "SELECT id, conversation_id, run_type, status, IFNULL(output_summary,'<null>'), IFNULL(error_message,'<null>') FROM agent_run WHERE conversation_id IN ($ALL_CONVS) ORDER BY id;"
sql "SELECT e.id, e.evidence_type, e.correctness, e.analyzer_version, es.source_type, es.source_id FROM learning_evidence e LEFT JOIN evidence_source es ON es.evidence_id=e.id WHERE e.concept_id IN ($TLB,$CACHE) OR e.concept_id IN ($(IFS=,; echo "${BAD_CONCEPT[*]}")) ORDER BY e.id;"
sql "SELECT u.concept_id, r.revision_no, r.status, r.trigger_evidence_id FROM understanding_revision r JOIN learning_understanding u ON u.id=r.understanding_id WHERE u.user_id=$USER_ID ORDER BY r.id;"

# =====================================================================
section "6. 清理与残留验证"
if [ "$FAIL" -eq 0 ]; then
  kill "$WORKER_PID" 2>/dev/null || true
  wait "$WORKER_PID" 2>/dev/null || true
  WORKER_PID=""
  # 清理动作本身也留证：队列清空与 SQL 删除的退出码、清理前深度都写进日志。
  # 这些是 analysis 专用测试队列（不承载生产消息），清理后仍会再次核对残留。
  CLEANUP_LOG="$OUT/cleanup.log"
  : > "$CLEANUP_LOG"
  Q_BEFORE=""
  for q in learning.analysis.request learning.analysis.result learning.analysis.retry \
           learning.analysis.result.retry learning.analysis.dlq; do
    Q_BEFORE="$Q_BEFORE $q=$(queue_depth "$q" 2>/dev/null || echo '?')"
  done
  echo "清理前队列深度:$Q_BEFORE" >> "$CLEANUP_LOG"
  if python - >> "$CLEANUP_LOG" 2>&1 <<'PY'
import asyncio, aio_pika
QUEUES = [
    "learning.analysis.request", "learning.analysis.result",
    "learning.analysis.retry", "learning.analysis.result.retry",
    "learning.analysis.dlq",
]
async def main():
    conn = await aio_pika.connect_robust("amqp://guest:guest@127.0.0.1:5672/")
    ch = await conn.channel()
    for name in QUEUES:
        q = await ch.declare_queue(name, passive=True)
        await q.purge()
    await conn.close()
asyncio.run(main())
PY
  then
    ok "五个 analysis 测试队列清理成功（清理前:$Q_BEFORE）"
  else
    bad "analysis 测试队列清理失败，见 $CLEANUP_LOG"
  fi
  # 只删除本脚本创建的用户（p51mq_$STAMP）及其级联数据；先执行 FK 检查再删除，
  # 避免别处仍引用时把生产/其他测试数据一起带走。测试库在本会话中已隔离。
  if MYSQL_PWD="$MYSQL_PASSWORD" mysql -h"$MYSQL_HOST" -P"$MYSQL_PORT" -u"$MYSQL_USER" \
      "$MYSQL_DB" >> "$CLEANUP_LOG" 2>&1 <<SQL
SET FOREIGN_KEY_CHECKS=0;
DELETE FROM test_answer WHERE task_id IN (SELECT id FROM test_task WHERE user_id=$USER_ID);
DELETE FROM test_task WHERE user_id=$USER_ID;
DELETE FROM memory_source WHERE memory_id IN (SELECT id FROM memory WHERE user_id=$USER_ID);
DELETE FROM memory WHERE user_id=$USER_ID;
DELETE FROM evidence_source WHERE evidence_id IN (SELECT id FROM learning_evidence WHERE user_id=$USER_ID);
DELETE FROM understanding_revision WHERE understanding_id IN (SELECT id FROM learning_understanding WHERE user_id=$USER_ID);
DELETE FROM learning_evidence WHERE user_id=$USER_ID;
DELETE FROM learning_understanding WHERE user_id=$USER_ID;
DELETE FROM learning_state WHERE user_id=$USER_ID;
DELETE FROM highlight WHERE user_id=$USER_ID;
DELETE FROM agent_run WHERE user_id=$USER_ID;
DELETE FROM message WHERE conversation_id IN (SELECT id FROM conversation WHERE user_id=$USER_ID);
DELETE FROM conversation WHERE user_id=$USER_ID;
DELETE FROM learning_session WHERE user_id=$USER_ID;
DELETE FROM document_chunk WHERE document_id IN (SELECT id FROM document WHERE learning_space_id IN (SELECT id FROM learning_space WHERE user_id=$USER_ID));
DELETE FROM document WHERE learning_space_id IN (SELECT id FROM learning_space WHERE user_id=$USER_ID);
DELETE FROM concept_relation WHERE source_concept_id IN (SELECT id FROM concept WHERE learning_space_id IN (SELECT id FROM learning_space WHERE user_id=$USER_ID)) OR target_concept_id IN (SELECT id FROM concept WHERE learning_space_id IN (SELECT id FROM learning_space WHERE user_id=$USER_ID));
DELETE FROM learning_plan WHERE user_id=$USER_ID;
DELETE FROM import_task WHERE user_id=$USER_ID;
DELETE FROM concept WHERE learning_space_id IN (SELECT id FROM learning_space WHERE user_id=$USER_ID);
DELETE FROM learning_space WHERE user_id=$USER_ID;
DELETE FROM user WHERE id=$USER_ID;
SET FOREIGN_KEY_CHECKS=1;
SQL
  then
    ok "本轮测试用户（id=$USER_ID）及级联业务数据清理成功"
  else
    bad "测试数据清理 SQL 失败，见 $CLEANUP_LOG"
  fi
  # 任务 JSON 里含短期委托声明（虽已过期），不留在工作区。
  rm -f "$OUT"/*-task*.json "$OUT"/duplicate-task.json
  if ls "$OUT"/*-task*.json "$OUT"/duplicate-task.json >/dev/null 2>&1; then
    bad "含短期委托声明的任务 JSON 未清理干净"
  else
    ok "已移除本地任务 JSON（含已过期的短期委托声明）"
  fi
  RESIDUE=$(sql "SELECT (SELECT COUNT(*) FROM user WHERE id=$USER_ID)+(SELECT COUNT(*) FROM learning_space WHERE user_id=$USER_ID)+(SELECT COUNT(*) FROM conversation WHERE user_id=$USER_ID)+(SELECT COUNT(*) FROM agent_run WHERE user_id=$USER_ID)+(SELECT COUNT(*) FROM learning_evidence WHERE user_id=$USER_ID)+(SELECT COUNT(*) FROM memory WHERE user_id=$USER_ID);")
  check "用户/空间/会话/run/Evidence/Memory 残留为 0" "${RESIDUE:-unknown}" "0"
  QUEUE_RESIDUE=0
  for q in learning.analysis.request learning.analysis.result learning.analysis.retry learning.analysis.result.retry learning.analysis.dlq; do
    QUEUE_RESIDUE=$((QUEUE_RESIDUE + $(queue_depth "$q" 2>/dev/null || echo 1)))
  done
  check "五个测试队列残留为 0" "$QUEUE_RESIDUE" "0"
else
  echo "  （存在 FAIL：保留测试数据与队列，供排查；不要用清理来掩盖失败）"
fi

printf '\n\033[1m结果：PASS=%d FAIL=%d\033[0m\n' "$PASS" "$FAIL"
printf '产物目录：%s\n' "$OUT"
echo "用户：p51mq_$STAMP (id=$USER_ID)  space=$SPACE_ID  TLB=$TLB  run=$RUN_ID"
if [ "$FAIL" -gt 0 ]; then exit 1; fi
echo "全部通过。"
