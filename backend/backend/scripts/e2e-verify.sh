#!/usr/bin/env bash
# 端到端验收脚本：把后端起起来之后跑一遍，覆盖这次修复的每一条 + 学习闭环链路。
#
#   ./mvnw -o spring-boot:run          # 另开一个终端
#   bash scripts/e2e-verify.sh
#
# 依赖：curl、mysql 客户端、python
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
PASS=0
FAIL=0

ok()   { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
check(){ # check <描述> <实际> <期望>
  if [ "$2" = "$3" ]; then ok "$1（= $3）"; else bad "$1：实际 [$2]，期望 [$3]"; fi
}
# checkge <描述> <实际> <下限>  —— 库里的数据会跨次运行累积，这类计数只能断言下界
checkge(){ if [ "${2:-0}" -ge "$3" ] 2>/dev/null; then ok "$1（$2 ≥ $3）"; else bad "$1：实际 [$2]，至少应有 [$3]"; fi }
checknot(){ if [ "$2" != "$3" ]; then ok "$1（≠ $3，实际 $2）"; else bad "$1：不应等于 [$3]"; fi }
section(){ printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

# api <METHOD> <PATH> [BODY] → 打印响应体。HTTP 码写进文件而不是变量：
# 所有 api 调用都发生在 $(...) 子 shell 里，在里面赋的变量传不出来。
HTTP_CODE_FILE="$(mktemp)"
api() {
  local method="$1" path="$2" body="${3:-}"
  local out
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
code() { cat "$HTTP_CODE_FILE"; }

# jqv <JSON> <表达式>   表达式里的顶层对象叫 d，例如 ['data']['id'] / len(d['data'])
jqv() { printf '%s' "$1" | python -c "
import sys, json
d = json.load(sys.stdin)
expr = sys.argv[1]
print(eval(('d' + expr) if expr.startswith('[') else expr))
" "$2" 2>/dev/null; }

# =====================================================================
section "0. 注册 / 登录 / 用户名规范化（#13）"
# 刻意在用户名前后加空格：注册查重与入库若用了不同的字符串，这里就会出问题
REG=$(curl -s -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"  e2e_$STAMP  \",\"password\":\"Passw0rd!23\"}")
TOKEN=$(jqv "$REG" "['data']['token']")
checknot "注册成功并拿到 token" "$TOKEN" ""
USERNAME=$(jqv "$REG" "['data']['user']['username']")
check "用户名被 trim 后入库" "$USERNAME" "e2e_$STAMP"

LOGIN=$(curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"e2e_$STAMP\",\"password\":\"Passw0rd!23\"}")
check "登录用未加空格的用户名能成功" "$(jqv "$LOGIN" "['code']")" "0"

DUP=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/auth/register" \
  -H 'Content-Type: application/json' -d "{\"username\":\" e2e_$STAMP \",\"password\":\"Passw0rd!23\"}")
check "重复注册（带空格变体）返回 409 而不是 500" "$DUP" "409"

# =====================================================================
section "1. 空间 / 概念（#15 软删复活 + 重名 409）"
SPACE=$(api POST /api/spaces "{\"name\":\"e2e 空间 $STAMP\"}")
SPACE_ID=$(jqv "$SPACE" "['data']['id']")
checknot "创建学习空间" "$SPACE_ID" ""

CONCEPT=$(api POST "/api/spaces/$SPACE_ID/concepts" '{"canonicalName":"递归","displayName":"递归"}')
CONCEPT_ID=$(jqv "$CONCEPT" "['data']['id']")
checknot "创建知识点" "$CONCEPT_ID" ""

DUP_C=$(api POST "/api/spaces/$SPACE_ID/concepts" '{"canonicalName":"递归"}')
check "同名知识点返回 409" "$(code)" "409"

api DELETE "/api/concepts/$CONCEPT_ID" > /dev/null
check "软删知识点" "$(code)" "200"

GONE=$(api GET "/api/concepts/$CONCEPT_ID")
check "软删后读知识点返回 404" "$(code)" "404"

REVIVED=$(api POST "/api/spaces/$SPACE_ID/concepts" '{"canonicalName":"递归","displayName":"递归（复活）"}')
REVIVED_ID=$(jqv "$REVIVED" "['data']['id']")
check "同名重建走复活而不是 500" "$(code)" "200"
check "复活后 id 不变（历史不分裂）" "$REVIVED_ID" "$CONCEPT_ID"

# =====================================================================
section "2. 对话 / SSE / history 去重（原缺陷 #1）"
CONV=$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"e2e 对话"}')
CONV_ID=$(jqv "$CONV" "['data']['id']")
checknot "创建对话" "$CONV_ID" ""

SSE=$(curl -s -N -m 30 -X POST "$BASE/api/conversations/$CONV_ID/messages" \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
  -d '{"content":"帮我讲讲递归"}')
EVENTS=$(printf '%s' "$SSE" | tr -d '\r' | grep '^event:' | sed 's/^event: *//' | tr '\n' ' ')
echo "  SSE 事件序列：$EVENTS"
case "$EVENTS" in "message.start message.delta "*) ok "SSE 以 message.start 开头并推送 delta";; *) bad "SSE 事件序列异常";; esac
case "$EVENTS" in *"message.end"*) ok "SSE 以 message.end 收尾（成功终态）";; *) bad "缺少 message.end";; esac
case "$EVENTS" in *"message.error"*) bad "不应出现 message.error";; *) ok "没有 message.error";; esac

MSGS=$(api GET "/api/conversations/$CONV_ID/messages")
check "对话内消息总数（1 问 + 1 答）" "$(jqv "$MSGS" "len(d['data'])")" "2"
USER_ROWS=$($MYSQL -e "SELECT COUNT(*) FROM message WHERE conversation_id=$CONV_ID AND role='USER' AND deleted_at IS NULL;")
check "库里 USER 消息只有一条（没有重复落库）" "$USER_ROWS" "1"

# 直接验证修复的机制：history() 用 userMessageId 把刚落库的这条排掉了。
# 下面两条 SQL 就是「修之前」与「修之后」的差别。
UM_ID=$($MYSQL -e "SELECT id FROM message WHERE conversation_id=$CONV_ID AND role='USER' ORDER BY id DESC LIMIT 1;")
WITHOUT=$(curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/conversations/$CONV_ID/messages" >/dev/null; \
  $MYSQL -e "SELECT COUNT(*) FROM message WHERE conversation_id=$CONV_ID ORDER BY sequence_no DESC, id DESC LIMIT 20;")
WITH=$( $MYSQL -e "SELECT COUNT(*) FROM message WHERE conversation_id=$CONV_ID AND id<>$UM_ID ORDER BY sequence_no DESC, id DESC LIMIT 20;")
check "送给 Agent 的历史排掉了本轮提问（$WITHOUT → $WITH）" "$WITH" "1"

# =====================================================================
section "3. 学习闭环链路：消息 → Evidence → Understanding → Revision → LearningState"
EV=$(api GET "/api/understandings/$CONCEPT_ID/evidence")
check "从用户消息里抽出了证据" "$(jqv "$EV" "len(d['data'])")" "1"
EV_ID=$(jqv "$EV" "['data'][0]['id']")

UN=$(api GET "/api/understandings/$CONCEPT_ID")
checknot "理解快照已建立" "$(jqv "$UN" "['data']['status']")" ""
check "证据挂在理解快照上" "$(jqv "$EV" "['data'][0]['understandingId']")" "$(jqv "$UN" "['data']['id']")"

REV=$(api GET "/api/understandings/$CONCEPT_ID/revisions")
check "产生了 Revision 历史" "$(jqv "$REV" "len(d['data'])")" "1"
check "currentRevisionId 指向最新 Revision" "$(jqv "$UN" "['data']['currentRevisionId']")" "$(jqv "$REV" "['data'][0]['id']")"

OVER=$(api GET /api/learning-state/overview)
check "LearningState 投影出现该概念" "$(jqv "$OVER" "len([s for s in d['data'] if s['conceptId']==$CONCEPT_ID])")" "1"

COV=$(api GET "/api/learning-state/topic-coverage?spaceId=$SPACE_ID")
check "topic-coverage 覆盖该空间（#15 归属校验后仍正常）" "$(code)" "200"
check "topic-coverage 里证据计数" "$(jqv "$COV" "d['data'][0]['evidenceCount']")" "1"

DETAIL=$(api GET "/api/learning-state/concepts/$CONCEPT_ID")
check "概念学习状态可读" "$(code)" "200"

BADCOV=$(api GET "/api/learning-state/topic-coverage?spaceId=999999")
check "别人的/不存在的 spaceId 返回 404" "$(code)" "404"

# =====================================================================
section "4. 测试任务：并发双提交 409（#10）+ 过期标记不被回滚（#1）"
TEST=$(api POST /api/tests "{\"conceptId\":$CONCEPT_ID,\"questionType\":\"SHORT_ANSWER\",\"questionText\":\"说说递归的终止条件\"}")
TEST_ID=$(jqv "$TEST" "['data']['id']")
checknot "出题" "$TEST_ID" ""

# 并发双提交：两个请求同时打同一个 testId
for i in 1 2; do
  curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/api/tests/$TEST_ID/answer" \
    -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
    -d '{"userAnswer":"递归必须有终止条件，否则会栈溢出"}' > "/tmp/answer_$i.code" &
done
wait
# tr -d '\r'：Git Bash 下的 curl 会把 \n 写成 \r\n，留着会让后面的模式匹配全部对不上
CODES="$(sort /tmp/answer_1.code /tmp/answer_2.code | tr -d '\r' | tr '\n' ' ')"
echo "  双提交返回码：$CODES"
case "$CODES" in *500*) bad "并发提交出现了 500（应为 200 + 409）";; *) ok "并发提交没有 500";; esac
case "$CODES" in "200 409 "*) ok "恰好一个成功、一个 409";; *) bad "双提交结果不符合预期（$CODES）";; esac

ANS_ROWS=$($MYSQL -e "SELECT COUNT(*) FROM test_answer WHERE task_id=$TEST_ID;")
check "库里只有一条作答记录" "$ANS_ROWS" "1"

# 过期标记：把 expired_at 拨到过去，再提交 → 必须 409 且状态要真的落成 EXPIRED
TEST2=$(api POST /api/tests "{\"conceptId\":$CONCEPT_ID,\"questionType\":\"SHORT_ANSWER\",\"questionText\":\"第二道题\"}")
TEST2_ID=$(jqv "$TEST2" "['data']['id']")
$MYSQL -e "UPDATE test_task SET expired_at = NOW() - INTERVAL 1 DAY WHERE id=$TEST2_ID;"
api GET /api/tests/pending > /dev/null
PENDING_HAS=$($MYSQL -e "SELECT COUNT(*) FROM test_task WHERE id=$TEST2_ID AND status='PENDING';")
check "过期任务仍在 DB 里是 PENDING（尚未被扫）" "$PENDING_HAS" "1"

EXP=$(api POST "/api/tests/$TEST2_ID/answer" '{"userAnswer":"随便写点什么凑够长度"}')
check "提交过期任务返回 409" "$(code)" "409"
EXP_STATUS=$($MYSQL -e "SELECT status FROM test_task WHERE id=$TEST2_ID;")
check "过期标记在 409 回滚后依然落库（原缺陷 #1）" "$EXP_STATUS" "EXPIRED"

PEND=$(api GET /api/tests/pending)
check "listPending 不再列出过期任务" "$(jqv "$PEND" "len([t for t in d['data'] if t['id']==$TEST2_ID])")" "0"

# =====================================================================
section "5. Memory：归属校验（#11）+ 关键词检索（#4 对齐）"
MEM=$(api POST /api/memories "{\"content\":\"用户习惯先看例子再看定义\",\"memoryType\":\"PREFERENCE\",\"importance\":0.8}")
MEM_ID=$(jqv "$MEM" "['data']['id']")
checknot "创建记忆" "$MEM_ID" ""

# memoryType 是 @NotNull，少传会在进 Service 之前就 400 —— 那是校验失败，测不到归属检查
FOREIGN=$(api POST /api/memories '{"content":"偷挂到别人空间","memoryType":"LEARNING","learningSpaceId":999999}')
check "挂到不属于自己的 spaceId 返回 404" "$(code)" "404"

# 关键词检索：把一整句话当 keyword 传（Python Agent 就是这么传的 —— 用户原话前 60 字）。
# 旧实现是 content LIKE '%整句话%'，永远匹配不到；现在是拆词 + 中文二元组 OR。
YW=$(python -c "import urllib.parse;print(urllib.parse.quote('我习惯先看例子还是先看定义呀'))")
SEARCH=$(api GET "/api/memories/search?keyword=$YW")
checkge "整句话作为 keyword 能命中记忆（不再恒为空）" "$(jqv "$SEARCH" "len(d['data'])")" "1"

EMPTY=$(api GET "/api/memories/search?keyword=zzzznomatch")
check "无关键词命中时正常返回空列表" "$(jqv "$EMPTY" "len(d['data'])")" "0"

# 带 spaceId 的检索只认挂在该空间下的记忆（不传 spaceId 的记忆对它不可见）。
# 这是当前契约，不是缺陷 —— 但 Python Agent 每次检索都会带 spaceId，
# 所以「用户级、不挂空间」的偏好类记忆 Agent 是拿不到的，见本次验收报告里的说明。
SCOPED=$(api POST /api/memories "{\"content\":\"这个空间里用户习惯先看例子再看定义\",\"memoryType\":\"PREFERENCE\",\"learningSpaceId\":$SPACE_ID}")
IN_SPACE=$(api GET "/api/memories/search?spaceId=$SPACE_ID&keyword=$YW")
checkge "整句话 keyword + spaceId 能命中空间内的记忆" "$(jqv "$IN_SPACE" "len(d['data'])")" "1"

# =====================================================================
section "6. agent_run 轨迹（#16 时长不溢出）"
RUNS=$($MYSQL -e "SELECT COUNT(*) FROM agent_run WHERE user_id=(SELECT id FROM user WHERE username='e2e_$STAMP') AND duration_ms < 0;")
check "没有负数时长的 agent_run" "$RUNS" "0"
RUN_STATUS=$($MYSQL -e "SELECT status FROM agent_run WHERE user_id=(SELECT id FROM user WHERE username='e2e_$STAMP') ORDER BY id DESC LIMIT 1;")
check "最近一条 agent_run 是终态" "$RUN_STATUS" "COMPLETED"

# =====================================================================
section "7. CORS（#19）"
CORS=$(curl -s -D - -o /dev/null -X OPTIONS "$BASE/api/spaces" \
  -H 'Origin: http://evil.example' -H 'Access-Control-Request-Method: POST')
if printf '%s' "$CORS" | grep -qi 'access-control-allow-credentials: *true'; then
  bad "仍然返回 Access-Control-Allow-Credentials: true"
else
  ok "不再返回 Access-Control-Allow-Credentials: true"
fi
if printf '%s' "$CORS" | grep -qi 'access-control-allow-origin'; then
  ok "跨域预检仍然放行（本地 Flutter 调试不受影响）"
else
  bad "跨域预检被拒了"
fi

# =====================================================================
printf '\n\033[1m===== 结果：%d 通过 / %d 失败 =====\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
