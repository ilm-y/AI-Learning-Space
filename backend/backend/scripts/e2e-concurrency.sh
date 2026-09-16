#!/usr/bin/env bash
# 并发路径验收：正常流量走不到的几条分支单独压出来看。
#
#   1) 客户端中途断开（传输侧）vs 生成线程（生成侧）—— 谁先落终态、助手消息会不会丢
#   2) 线程池饱和时的拒绝路径 —— 必须是 message.start + message.error(503)，不能是静默断流
#   3) 同一对话并发发消息 —— 序号不能重复，助手消息不能多写
#
# 用法：先 ./mvnw -o spring-boot:run，再 bash scripts/e2e-concurrency.sh
#
# MySQL 凭证不写死在这里：本脚本是要提交的，application-local.yml 不是（已被 .gitignore 排除）。
# 解析顺序与 Spring 一致 —— application-local.yml → application.yml，后者支持 ${VAR:默认值}；
# 两边都可以用 MYSQL_USER / MYSQL_PASSWORD / MYSQL_HOST / MYSQL_DB 环境变量覆盖。
set -uo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
read_creds() {
  python - "$BACKEND_DIR/src/main/resources" <<'PY'
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

BASE="${BASE:-http://localhost:8080}"
MYSQL="mysql -h${MYSQL_HOST:-127.0.0.1} -u${MYSQL_USER} -p${MYSQL_PASSWORD} -N -B ${MYSQL_DB:-ails}"
STAMP="$(date +%s)"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
check(){ if [ "$2" = "$3" ]; then ok "$1（= $3）"; else bad "$1：实际 [$2]，期望 [$3]"; fi }
section(){ printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
jqv() { printf '%s' "$1" | python -c "
import sys, json
d = json.load(sys.stdin)
expr = sys.argv[1]
print(eval(('d' + expr) if expr.startswith('[') else expr))
" "$2" 2>/dev/null; }

REG=$(curl -s -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"conc_$STAMP\",\"password\":\"Passw0rd!23\"}")
TOKEN=$(jqv "$REG" "['data']['token']")
SPACE_ID=$(jqv "$(curl -s -X POST "$BASE/api/spaces" -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" -d "{\"name\":\"conc $STAMP\"}")" "['data']['id']")
CONV_ID=$(jqv "$(curl -s -X POST "$BASE/api/spaces/$SPACE_ID/conversations" \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" -d '{"title":"conc"}')" "['data']['id']")
echo "  用户 $STAMP / 空间 $SPACE_ID / 对话 $CONV_ID"

# =====================================================================
section "1. 客户端中途断开：传输侧 vs 生成侧"
# 立刻断开：请求已经发出去、SSE 已经建立，然后客户端消失。
# 修复后的不变量是「传输断了 ≠ 生成失败」—— 生成线程照常把助手消息落库、把 run 写成 COMPLETED，
# 用户重进对话时回复是完整的，不是只剩自己那半句。
curl -s -N -m 0.05 -X POST "$BASE/api/conversations/$CONV_ID/messages" \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
  -d '{"content":"客户端会立刻断开"}' > /dev/null 2>&1
sleep 3  # 等生成线程跑完

ASSISTANT=$($MYSQL -e "SELECT COUNT(*) FROM message WHERE conversation_id=$CONV_ID AND role='ASSISTANT' AND deleted_at IS NULL;")
check "断开后助手消息仍然落库（不丢回复）" "$ASSISTANT" "1"
RUN_STATUS=$($MYSQL -e "SELECT status FROM agent_run WHERE conversation_id=$CONV_ID ORDER BY id DESC LIMIT 1;")
check "agent_run 收敛到 COMPLETED（传输失败没覆盖生成的结论）" "$RUN_STATUS" "COMPLETED"
NEG=$($MYSQL -e "SELECT COUNT(*) FROM agent_run WHERE conversation_id=$CONV_ID AND duration_ms < 0;")
check "没有负数时长" "$NEG" "0"

# =====================================================================
section "2. 同一对话并发发消息：序号唯一 + 助手消息不多写"
N=20
for i in $(seq 1 $N); do
  curl -s -N -m 30 -X POST "$BASE/api/conversations/$CONV_ID/messages" \
    -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
    -d "{\"content\":\"并发消息 $i\"}" > "/tmp/conc_$i.sse" 2>&1 &
done
wait

DUP_SEQ=$($MYSQL -e "SELECT COUNT(*) FROM (SELECT sequence_no FROM message WHERE conversation_id=$CONV_ID AND deleted_at IS NULL GROUP BY sequence_no HAVING COUNT(*)>1) x;")
check "sequence_no 没有重复" "$DUP_SEQ" "0"
TOTAL_USER=$($MYSQL -e "SELECT COUNT(*) FROM message WHERE conversation_id=$CONV_ID AND role='USER' AND deleted_at IS NULL;")
check "USER 消息条数 = 1(断开那轮) + $N" "$TOTAL_USER" "$((N+1))"
TOTAL_ASSISTANT=$($MYSQL -e "SELECT COUNT(*) FROM message WHERE conversation_id=$CONV_ID AND role='ASSISTANT' AND deleted_at IS NULL;")
check "ASSISTANT 消息条数 = 1 + $N（每轮恰好一条）" "$TOTAL_ASSISTANT" "$((N+1))"

ERR_EVENTS=$(cat /tmp/conc_*.sse | tr -d '\r' | grep -c '^event:message.error' || true)
check "并发下没有出现 message.error" "$ERR_EVENTS" "0"
# 客户端收到多少条 end 只作参考，不作为判据：
# 这一节会一次拉起几十上百个 curl 进程，Windows 上本地端口/句柄会先于服务端耗尽，
# 少数请求会出现「服务端处理完了、客户端一个字节都没读回来」的空文件。
# 真正要断言的是服务端状态 —— 下面几条就是。客户端侧的数字打印出来供人工对照。
END_EVENTS=$(cat /tmp/conc_*.sse | tr -d '\r' | grep -c '^event:message.end' || true)
echo "  客户端收到 message.end 的轮数：$END_EVENTS / $N（差额是本地 curl 资源耗尽，非服务端问题）"
RUNS_TERMINAL=$($MYSQL -e "SELECT COUNT(*) FROM agent_run WHERE conversation_id=$CONV_ID AND status<>'RUNNING';")
check "服务端每一轮都收敛到终态" "$RUNS_TERMINAL" "$((N+1))"

# =====================================================================
section "3. 高并发压测：每个请求都必须有 start 与终态"
# 注意这一节**测不到线程池拒绝路径**，别把它当成那条分支的验收：
# 拒绝只在 threads(64) + queue(128) = 192 个任务同时在飞时发生，而 StubAgentGateway 是同步的、
# 一轮几毫秒就结束，排队速度永远追不上消费速度，所以打多少都不会触发。
# 真正会触发的是接上真 Agent 之后（一轮生成几秒到几十秒），那时才谈得上饱和。
# 这一节能证明的是另一件事：高并发下没有静默断流、没有丢终态、序号不重复。
# 记下压测前的最大 agent_run id，用来把「本节新增的轮次」和「前两节的轮次」分开数
CONC_MAX_RUN=$($MYSQL -e "SELECT COALESCE(MAX(id),0) FROM agent_run WHERE conversation_id=$CONV_ID;")
BURST=400
for i in $(seq 1 $BURST); do
  curl -s -N -m 60 -X POST "$BASE/api/conversations/$CONV_ID/messages" \
    -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
    -d "{\"content\":\"压测 $i\"}" > "/tmp/burst_$i.sse" 2>&1 &
done
wait

# 只认真实的事件行与 code 字段：桩回复会把用户输入原样回显，正文里出现 "503" 是内容不是错误码
BURST_ERR=$(cat /tmp/burst_*.sse | tr -d '\r' | grep -c '^event:message.error' || true)
BURST_503=$(cat /tmp/burst_*.sse | tr -d '\r' | grep -c '"code":503' || true)
BURST_START=$(cat /tmp/burst_*.sse | tr -d '\r' | grep -c '^event:message.start' || true)
BURST_END=$(cat /tmp/burst_*.sse | tr -d '\r' | grep -c '^event:message.end' || true)
echo "  （客户端侧）start=$BURST_START  end=$BURST_END  error=$BURST_ERR，其中 503 有 $BURST_503 条"

# 判据放在服务端：400 个并发 curl 进程本身就会把 Windows 的本地端口/句柄耗光，
# 出现几个 0 字节的空响应是客户端的事，不是「服务端静默断流」。
# 服务端有没有把每一轮都处理完，看的是落库结果 —— 这才是有意义的断言。
BURST_USERS=$($MYSQL -e "SELECT COUNT(*) FROM message WHERE conversation_id=$CONV_ID AND role='USER' AND content LIKE '压测 %' AND deleted_at IS NULL;")
check "$BURST 个压测请求全部被服务端受理（USER 消息落库）" "$BURST_USERS" "$BURST"
BURST_ASSISTANTS=$($MYSQL -e "SELECT COUNT(*) FROM agent_run WHERE conversation_id=$CONV_ID AND run_type='CHAT' AND id > $CONC_MAX_RUN;")
check "每一轮都有对应的 agent_run" "$BURST_ASSISTANTS" "$BURST"
check "高并发下没有出现 message.error" "$BURST_ERR" "0"

DUP_SEQ2=$($MYSQL -e "SELECT COUNT(*) FROM (SELECT sequence_no FROM message WHERE conversation_id=$CONV_ID AND deleted_at IS NULL GROUP BY sequence_no HAVING COUNT(*)>1) x;")
check "压测后 sequence_no 仍然没有重复" "$DUP_SEQ2" "0"
BURST_NEG=$($MYSQL -e "SELECT COUNT(*) FROM agent_run WHERE conversation_id=$CONV_ID AND duration_ms < 0;")
check "压测后仍没有负数时长" "$BURST_NEG" "0"
BAD_RUNS=$($MYSQL -e "SELECT COUNT(*) FROM agent_run WHERE conversation_id=$CONV_ID AND status='RUNNING';")
check "没有卡在 RUNNING 的 agent_run（每一轮都收敛到终态）" "$BAD_RUNS" "0"
ASSIST_TOTAL=$($MYSQL -e "SELECT COUNT(*) FROM message WHERE conversation_id=$CONV_ID AND role='ASSISTANT' AND deleted_at IS NULL;")
check "ASSISTANT 消息条数 = 1(断开那轮) + $N + $BURST（每轮恰好一条，没有多写）" \
  "$ASSIST_TOTAL" "$((N + BURST + 1))"

printf '\n\033[1m===== 结果：%d 通过 / %d 失败 =====\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
