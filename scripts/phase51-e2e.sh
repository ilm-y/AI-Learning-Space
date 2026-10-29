#!/usr/bin/env bash
# 阶段 5.1 跨会话学习闭环验收（inline 分析链路 / Mock LLM）。
#
# 前置：
#   1. MySQL 可连（凭证从 application-local.yml 读，不写死）；
#   2. 后端 8080 已启动，ails.agent.gateway=http；
#   3. Python Agent 8000 已启动，AILS_LLM_PROVIDER=mock、AILS_RETRIEVER_MODE=auto
#      （可选）AILS_INTERNAL_TOKEN 与后端一致，供异步链路使用。
#
# 用法：
#   bash scripts/phase51-e2e.sh
#
# 本脚本只通过公开 API / SSE / 分析链路产生数据；直接 SQL 只用于**核对**
# 数据库事实（来源、版本号、append-only），从不插入或修改业务数据。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_SRC="$ROOT/backend/backend/src/main/resources"
BASE="${BASE:-http://127.0.0.1:8080}"
AGENT="${AGENT:-http://127.0.0.1:8000}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$ROOT/backend/backend/target/phase51-e2e/$STAMP"
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

PASS=0; FAIL=0; SKIP=0
ok()   { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
skip() { SKIP=$((SKIP+1)); printf '  \033[33mSKIP\033[0m %s\n' "$1"; }
check(){ if [ "$2" = "$3" ]; then ok "$1（= $3）"; else bad "$1：实际 [$2]，期望 [$3]"; fi; }
checknot(){ if [ "$2" != "$3" ]; then ok "$1（≠ $3，实际 $2）"; else bad "$1：不应等于 [$3]"; fi; }
checkge(){ if [ "${2:-0}" -ge "$3" ] 2>/dev/null; then ok "$1（$2 ≥ $3）"; else bad "$1：实际 [$2]，至少应有 [$3]"; fi; }
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
code(){ cat "$HTTP_CODE_FILE"; }
jqv(){ printf '%s' "$1" | python -c "
import sys, json
d = json.load(sys.stdin)
expr = sys.argv[1]
print(eval(('d' + expr) if expr.startswith('[') else expr))
" "$2" 2>/dev/null; }

# 等待一次分析落库。inline 链路的分析在 SSE 结束后才启动，必须轮询，而不是立刻断言。
wait_evidence() { # wait_evidence <conceptId> <min> <seconds>
  local cid="$1" min="$2" secs="${3:-30}" i=0
  while [ "$i" -lt "$secs" ]; do
    local n
    n=$(jqv "$(api GET "/api/understandings/$cid/evidence")" "len(d['data'])")
    if [ "${n:-0}" -ge "$min" ]; then return 0; fi
    sleep 1; i=$((i+1))
  done
  return 1
}

mint_identity(){ # mint_identity <userId>
  ( cd "$ROOT/agent" && python -m app.internal_identity --user "$1" )
}

preview_context() { # preview_context <conversationId> <question> <outfile>
  local cid="$1" q="$2" out="$3"
  local ident
  ident="$(mint_identity "$USER_A")" || { echo "  无法签发内部身份" >&2; return 1; }
  sid=$(jqv "$(api GET "/api/conversations/$cid")" "['data']['sessionId']")
  sess_json=$(api GET "/api/sessions/$sid")
  payload="$OUT/.preview-payload-$$.json"
  python - "$USER_A" "$cid" "$SPACE_ID" "$q" "$TOKEN" "$sess_json" "$payload" <<'PY'
import json, sys
uid, cid, sid, question, token, sess, out = sys.argv[1:8]
s = json.loads(sess)["data"]
payload = {
    "runId": 910000,
    "userId": int(uid),
    "conversationId": int(cid),
    "learningSpaceId": int(sid),
    "userMessage": question,
    "authToken": token,
    "session": {"id": s["id"], "title": s.get("title"), "goal": s.get("goal"),
                "currentFocus": s.get("currentFocus")},
}
json.dump(payload, open(out, "w", encoding="utf-8"), ensure_ascii=False)
PY
  curl -s -X POST "$AGENT/agent/context/preview" -H 'Content-Type: application/json' \
    -H "X-Internal-Token: ${AILS_INTERNAL_TOKEN:-}" \
    -H "X-Internal-Identity: $ident" \
    -H "X-Internal-User-Id: $USER_A" \
    -d @"$payload" > "$out"
}

# 注册 + 登录。用户名带时间戳，避免跨次运行累积干扰。
register() {
  local name="$1"
  local reg login
  reg=$(curl -s -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$name\",\"password\":\"Passw0rd!23\"}")
  if [ "$(jqv "$reg" "['code']")" != "0" ]; then
    login=$(curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
      -d "{\"username\":\"$name\",\"password\":\"Passw0rd!23\"}")
    printf '%s' "$login"
  else
    printf '%s' "$reg"
  fi
}

# =====================================================================
section "0. 前置检查"
if ! curl -s -m 3 "$AGENT/health" > /dev/null; then
  echo "Python Agent 未启动：$AGENT"; exit 1
fi
if [ -z "${AILS_INTERNAL_TOKEN:-}" ] || [ -z "${AILS_INTERNAL_IDENTITY_SECRET:-}" ]; then
  echo "需要 AILS_INTERNAL_TOKEN（诊断）与 AILS_INTERNAL_IDENTITY_SECRET（preview 身份签名）。"; exit 1
fi
AHEALTH=$(curl -s -m 3 -H "X-Internal-Token: $AILS_INTERNAL_TOKEN" \
  "$AGENT/health/diagnostics")
if [ "$(printf '%s' "$AHEALTH" | python -c 'import sys,json;print(json.load(sys.stdin).get("llm_provider"))')" != "mock" ]; then
  echo "本脚本按 Mock LLM 设计；当前 provider 不是 mock，结果不能与 Mock 基线混在一起。"
  echo "$AHEALTH"
  exit 1
fi
if ! sql "SELECT 1" > /dev/null; then
  echo "MySQL 连接失败（读取 application-local.yml 的凭证）"; exit 1
fi
ok "Agent 可用（mock）且 MySQL 可连"

UA="p51a_$STAMP"
UB="p51b_$STAMP"
REG_A=$(register "$UA")
TOKEN=$(jqv "$REG_A" "['data']['token']")
USER_A=$(jqv "$REG_A" "['data']['user']['id']")
REG_B=$(register "$UB")
TOKEN_B=$(jqv "$REG_B" "['data']['token']")
USER_B=$(jqv "$REG_B" "['data']['user']['id']")
if [ -n "$TOKEN" ]; then ok "用户 A 注册并取得 token"; else bad "用户 A 注册未返回 token"; fi
if [ -n "$TOKEN_B" ]; then ok "用户 B 注册并取得 token"; else bad "用户 B 注册未返回 token"; fi

# =====================================================================
section "1. 空间 / 概念（场景使用项目已有的 TLB 概念）"
SPACE=$(api POST /api/spaces "{\"name\":\"阶段5.1验收 $STAMP\",\"description\":\"跨会话学习闭环\"}")
SPACE_ID=$(jqv "$SPACE" "['data']['id']")
checknot "创建学习空间" "$SPACE_ID" ""
CONCEPT=$(api POST "/api/spaces/$SPACE_ID/concepts" \
  '{"canonicalName":"TLB","displayName":"TLB","description":"页表项的高速缓存"}')
TLB=$(jqv "$CONCEPT" "['data']['id']")
checknot "创建概念 TLB" "$TLB" ""
PT=$(jqv "$(api POST "/api/spaces/$SPACE_ID/concepts" \
  '{"canonicalName":"Page Table","displayName":"Page Table","description":"页表"}')" "['data']['id']")
checknot "创建概念 Page Table" "$PT" ""

# =====================================================================
section "2. 会话 A：错误解释 → 异步分析 → Evidence / Understanding / Revision / LearningState"
CA=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"会话 A"}')" "['data']['id']")
checknot "创建会话 A" "$CA" ""
check "会话 A 初始没有 Session（懒创建）" \
  "$(jqv "$(api GET "/api/conversations/$CA")" "['data']['sessionId']")" "None"

WRONG='我理解 TLB 就是页表本身，它缓存的是整个页表；所以 TLB miss 就等于缺页中断。'
SSE_A="$OUT/session-a.sse"
curl -sN -m 60 -X POST "$BASE/api/conversations/$CA/messages" \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
  -d "$(python -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}, ensure_ascii=False))' "$WRONG")" \
  > "$SSE_A"

EVENTS=$(tr -d '\r' < "$SSE_A" | grep '^event:' | sed 's/^event: *//' | tr '\n' ' ')
echo "  SSE 事件：$EVENTS"
case "$EVENTS" in "message.start message.delta "*) ok "SSE 正常开始并流式输出";; *) bad "SSE 起始事件异常";; esac
case "$EVENTS" in *"message.end"*) ok "SSE 正常结束";; *) bad "SSE 未正常结束";; esac
case "$EVENTS" in *"message.error"*) bad "SSE 出现 message.error";; *) ok "SSE 无错误事件";; esac

MSGS_A="$OUT/session-a.messages.json"
api GET "/api/conversations/$CA/messages" > "$MSGS_A"
USER_MSG_ID=$(jqv "$(cat "$MSGS_A")" "next(m['id'] for m in d['data'] if m['role']=='USER')")
ASSIST_MSG_ID=$(jqv "$(cat "$MSGS_A")" "next(m['id'] for m in d['data'] if m['role']=='ASSISTANT')")
ASSIST_TEXT=$(jqv "$(cat "$MSGS_A")" "next(m['content'] for m in d['data'] if m['role']=='ASSISTANT')")
checknot "用户消息已落库并有 id" "$USER_MSG_ID" "None"
checknot "助手消息已落库并有 id" "$ASSIST_MSG_ID" "None"
check "收到的用户原话与请求一致" \
  "$(sql "SELECT content FROM message WHERE id=$USER_MSG_ID;")" "$WRONG"

if ! wait_evidence "$TLB" 1 45; then
  bad "45 秒内没有产生 Evidence（分析链路未完成）"
else
  ok "异步分析已落库 Evidence"
fi

EV_A="$OUT/session-a.evidence.json"
api GET "/api/understandings/$TLB/evidence" > "$EV_A"
EV_ID=$(jqv "$(cat "$EV_A")" "d['data'][0]['id']")
EV_OBS=$(jqv "$(cat "$EV_A")" "d['data'][0]['observation']")
EV_TYPE=$(jqv "$(cat "$EV_A")" "d['data'][0]['evidenceType']")
EV_CORR=$(jqv "$(cat "$EV_A")" "d['data'][0]['correctness']")
SRC_TYPE=$(jqv "$(cat "$EV_A")" "d['data'][0]['sources'][0]['sourceType']")
SRC_ID=$(jqv "$(cat "$EV_A")" "d['data'][0]['sources'][0]['sourceId']")
check "Evidence 有明确来源（MESSAGE）" "$SRC_TYPE" "MESSAGE"
check "Evidence 来源指向本轮用户消息" "$SRC_ID" "$USER_MSG_ID"
check "Evidence 的观察文本来自用户原话（含 TLB）" \
  "$(printf '%s' "$EV_OBS" | grep -c 'TLB')" "1"
check "Evidence 没有把助手回答当作证据" \
  "$(printf '%s' "$EV_OBS" | grep -c 'Mock LLM 输出')" "0"
check "助手消息 id 与证据来源不同" \
  "$([ "$ASSIST_MSG_ID" != "$SRC_ID" ] && echo yes || echo no)" "yes"
check "Evidence 由 Agent 分析器（agent-v1）产生" \
  "$(sql "SELECT analyzer_version FROM learning_evidence WHERE id=$EV_ID;")" "agent-v1"
echo "  Evidence: id=$EV_ID type=$EV_TYPE correctness=$EV_CORR observation=$EV_OBS"

UN_A="$OUT/session-a.understanding.json"
api GET "/api/understandings/$TLB" > "$UN_A"
UN_ID=$(jqv "$(cat "$UN_A")" "d['data']['id']")
STATUS_1=$(jqv "$(cat "$UN_A")" "d['data']['status']")
LATEST_1=$(jqv "$(cat "$UN_A")" "d['data']['latestUserExplanation']")
REV_A="$OUT/session-a.revisions.json"
api GET "/api/understandings/$TLB/revisions" > "$REV_A"
REV_COUNT_1=$(jqv "$(cat "$REV_A")" "len(d['data'])")
checknot "Understanding 快照已建立" "$UN_ID" "None"
checkge "理解确实发生了演进（至少 1 个 Revision）" "$REV_COUNT_1" "1"
check "currentRevisionId 指向最新 Revision" \
  "$(jqv "$(cat "$UN_A")" "d['data']['currentRevisionId']")" \
  "$(jqv "$(cat "$REV_A")" "d['data'][-1]['id']")"
check "Mock 分析不会把 PARTIAL 解释误判成 CORRECT" "$EV_CORR" "PARTIAL"
case "$STATUS_1" in EXPOSED|PARTIAL) ok "初始状态不虚构掌握（$STATUS_1）";; *) bad "初始状态异常：$STATUS_1";; esac
check "用户原话被保留到 Understanding" "$LATEST_1" "$WRONG"
check "Understanding 不是 Memory 的别名（该用户该概念没有 Memory）" \
  "$(sql "SELECT COUNT(*) FROM memory WHERE user_id=$USER_A AND concept_id=$TLB;")" "0"
check "Evidence 挂在 Understanding 上" \
  "$(sql "SELECT understanding_id FROM learning_evidence WHERE id=$EV_ID;")" "$UN_ID"

LS_A="$OUT/session-a.learning-state.json"
api GET "/api/learning-state/concepts/$TLB" > "$LS_A"
check "LearningState 是 Understanding 的投影（状态一致）" \
  "$(jqv "$(cat "$LS_A")" "d['data']['status']")" "$STATUS_1"
checknot "LearningState 记录了最近证据时间" \
  "$(jqv "$(cat "$LS_A")" "d['data']['lastEvidenceAt']")" "None"

# 用已有批注接口把「用户自己标出的误区」变成 INCORRECT 证据。
# 这不是绕过业务链路：批注是产品自有入口，规则版分析会走
# Evidence → Understanding → Revision → LearningState 的完整写入路径。
section "2b. 用户显式标注误区 → 误区生命周期进入 ACTIVE"
HL=$(api POST "/api/messages/$USER_MSG_ID/highlights" \
  "$(python -c 'import json; print(json.dumps({"markType":"MISCONCEPTION_CANDIDATE","selectedText":"TLB 就是页表本身，它缓存的是整个页表","note":""}, ensure_ascii=False))')")
echo "$HL" > "$OUT/session-a.highlight.json"
HL_ID=$(jqv "$HL" "['data']['id']")
checknot "误区标注创建成功" "$HL_ID" "None"
INCORRECT_ID=$(sql "SELECT id FROM learning_evidence WHERE user_id=$USER_A AND concept_id=$TLB AND correctness='INCORRECT' ORDER BY id DESC LIMIT 1;")
checknot "显式误区证据 id 可定位" "$INCORRECT_ID" ""

api GET "/api/understandings/$TLB" > "$OUT/session-a.understanding-misconception.json"
UN_M="$OUT/session-a.understanding-misconception.json"
check "误区进入当前快照" \
  "$(jqv "$(cat "$UN_M")" "len(d['data']['misconceptions'])")" "1"
check "误区状态为 ACTIVE" \
  "$(jqv "$(cat "$UN_M")" "d['data']['misconceptions'][0]['status']")" "ACTIVE"
check "理解状态变为 MISCONCEPTION" \
  "$(jqv "$(cat "$UN_M")" "d['data']['status']")" "MISCONCEPTION"
api GET "/api/understandings/$TLB/revisions" > "$OUT/session-a.revisions-misconception.json"
REV_COUNT_M=$(jqv "$(cat "$OUT/session-a.revisions-misconception.json")" "len(d['data'])")
checkge "误区建立也产生新的 Revision" "$REV_COUNT_M" "$((REV_COUNT_1 + 1))"
check "Evidence 来源指向 HIGHLIGHT" \
  "$(sql "SELECT source_type FROM evidence_source WHERE evidence_id=(SELECT id FROM learning_evidence WHERE user_id=$USER_A AND concept_id=$TLB AND correctness='INCORRECT' ORDER BY id DESC LIMIT 1);")" "HIGHLIGHT"

# =====================================================================
section "3. 独立会话 B：读取会话 A 留下的学习状态"
CB=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"会话 B"}')" "['data']['id']")
checknot "创建会话 B" "$CB" ""
QUESTION_B='那 TLB 里到底缓存的是什么？我之前好像理解错了，你能针对我的误区讲讲吗？'
EV_BEFORE_B=$(jqv "$(api GET "/api/understandings/$TLB/evidence")" "len(d['data'])")
REV_BEFORE_B=$(jqv "$(api GET "/api/understandings/$TLB/revisions")" "len(d['data'])")
SSE_B="$OUT/session-b.sse"
curl -sN -m 60 -X POST "$BASE/api/conversations/$CB/messages" \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
  -d "$(python -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}, ensure_ascii=False))' "$QUESTION_B")" \
  > "$SSE_B"
# 等这轮「提问」产生的 OBSERVATION 落库，再断言它没有改变理解。
if ! wait_evidence "$TLB" "$((EV_BEFORE_B + 1))" 30; then
  bad "会话 B 的提问没有在 30 秒内产生 OBSERVATION 证据"
fi
check "仅有提问（UNKNOWN 观察）不产生新 Revision" \
  "$(jqv "$(api GET "/api/understandings/$TLB/revisions")" "len(d['data'])")" "$REV_BEFORE_B"
MSGS_B="$OUT/session-b.messages.json"
api GET "/api/conversations/$CB/messages" > "$MSGS_B"
REPLY_B=$(jqv "$(cat "$MSGS_B")" "next(m['content'] for m in d['data'] if m['role']=='ASSISTANT')")
echo "  会话 B 的 mock 回答（前 500 字）："
printf '%s\n' "$REPLY_B" | head -c 500; echo
case "$REPLY_B" in
  *"TLB 就是页表本身"*) ok "实际对话上下文中带上了会话 A 的误区";;
  *) bad "实际对话回答里看不到会话 A 的误区";;
esac

CB_SESS=$(jqv "$(api GET "/api/conversations/$CB")" "['data']['sessionId']")
CA_SESS=$(jqv "$(api GET "/api/conversations/$CA")" "['data']['sessionId']")
checknot "会话 A 已归属学习活动" "$CA_SESS" "None"
check "会话 B 归入同一学习活动（不是新建）" "$CB_SESS" "$CA_SESS"

# preview 与真实 chat 共用 prepare()；用它核对「到底把哪些区块喂给了模型」。
SESS_JSON=$(api GET "/api/sessions/$CB_SESS")
PREVIEW_PAYLOAD=$(python - "$USER_A" "$CB" "$SPACE_ID" "$QUESTION_B" "$TOKEN" "$SESS_JSON" <<'PY'
import json, sys
uid, cid, sid, question, token, sess = sys.argv[1:7]
s = json.loads(sess)["data"]
print(json.dumps({
    "runId": 900000,
    "userId": int(uid),
    "conversationId": int(cid),
    "learningSpaceId": int(sid),
    "userMessage": question,
    "authToken": token,
    "session": {"id": s["id"], "title": s.get("title"), "goal": s.get("goal"),
                "currentFocus": s.get("currentFocus")},
}, ensure_ascii=False))
PY
)
PREVIEW="$OUT/session-b.preview.json"
curl -s -X POST "$AGENT/agent/context/preview" -H 'Content-Type: application/json' \
  -H "X-Internal-Token: ${AILS_INTERNAL_TOKEN:-}" \
  -H "X-Internal-User-Id: $USER_A" \
  -d "$PREVIEW_PAYLOAD" > "$PREVIEW"
python - "$PREVIEW" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))["data"]
print("  retriever:", d["retriever"], "| degraded:", d["degraded"])
print("  区块:", [s["name"] for s in d["context"]["sections"]])
print("  warnings:", d["context"]["warnings"])
PY
HAS_UN=$(python - "$PREVIEW" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))["data"]
print(any(s["name"].startswith("understanding:") for s in d["context"]["sections"]))
PY
)
check "Context Builder 注入了 understanding 区块" "$HAS_UN" "True"
SYS="$OUT/session-b.system.txt"
python - "$PREVIEW" "$SYS" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))["data"]
open(sys.argv[2], "w", encoding="utf-8").write(d["context"]["systemInstruction"])
PY
case "$(cat "$SYS")" in *"TLB 就是页表本身"*) ok "system instruction 里包含持久化的误区原文";; *) bad "system instruction 里没有误区原文";; esac
case "$(cat "$SYS")" in *"状态："*) ok "system instruction 里包含理解状态标签";; *) bad "system instruction 缺少状态标签";; esac
check "Context Builder 读到的状态与数据库一致" \
  "$(jqv "$(cat "$PREVIEW")" "any('MISCONCEPTION' in s['preview'] or '误区' in s['preview'] for s in d['data']['context']['sections'] if s['name'].startswith('understanding:'))")" "True"

# =====================================================================
section "4. 纠正证据：错误 → PENDING → 解除，历史仍保留"
# 4a. 先提出一道题并保留到纠正之后再答，验证「回答之前的问题」。
TEST=$(api POST /api/tests \
  "{\"conceptId\":$TLB,\"conversationId\":$CB,\"questionType\":\"SHORT_ANSWER\",\"questionText\":\"TLB 中缓存的是什么？\",\"correctAnswer\":\"页表项\"}")
TEST_ID=$(jqv "$TEST" "['data']['id']")
checknot "创建待作答问题" "$TEST_ID" "None"

# 4b. 用户重新给出正确解释，并主动标成「我的理解」（CORRECT 反向证据 1）。
CORRECT='我现在明白了：TLB 缓存的是页表项，不是整个页表；TLB miss 之后才需要查页表，缺页是页面不在内存。'
EV_BEFORE_CORRECTION=$(jqv "$(api GET "/api/understandings/$TLB/evidence")" "len(d['data'])")
curl -sN -m 60 -X POST "$BASE/api/conversations/$CB/messages" \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
  -d "$(python -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}, ensure_ascii=False))' "$CORRECT")" \
  > "$OUT/session-b.correction.sse"
MSGS_C="$OUT/session-b.correction.messages.json"
api GET "/api/conversations/$CB/messages" > "$MSGS_C"
CORRECT_MSG_ID=$(jqv "$(cat "$MSGS_C")" "max(m['id'] for m in d['data'] if m['role']=='USER')")
# 先等这轮消息的分析落库，避免「消息分析」与「批注分析」在同一概念上并发写入。
if ! wait_evidence "$TLB" "$((EV_BEFORE_CORRECTION + 1))" 30; then
  bad "纠正解释的消息分析没有在 30 秒内落库"
fi
HL2=$(api POST "/api/messages/$CORRECT_MSG_ID/highlights" \
  "$(python -c 'import json; print(json.dumps({"markType":"MY_UNDERSTANDING","selectedText":"TLB 缓存的是页表项，不是整个页表","note":""}, ensure_ascii=False))')")
echo "$HL2" > "$OUT/session-b.correct-highlight.json"
CORRECT_HL_ID=$(jqv "$HL2" "['data']['id']")
checknot "正确解释批注创建成功" "$CORRECT_HL_ID" "None"
CORRECT_HL_EV=$(sql "SELECT le.id FROM learning_evidence le JOIN evidence_source es ON es.evidence_id=le.id WHERE le.user_id=$USER_A AND le.concept_id=$TLB AND le.correctness='CORRECT' AND es.source_type='HIGHLIGHT' ORDER BY le.id DESC LIMIT 1;")
checknot "正确解释批注证据 id 可定位" "$CORRECT_HL_EV" ""

api GET "/api/understandings/$TLB" > "$OUT/session-b.after-first-correction.json"
UN_C1="$OUT/session-b.after-first-correction.json"
check "一次正确证据后误区降为 PENDING" \
  "$(jqv "$(cat "$UN_C1")" "d['data']['misconceptions'][0]['status']")" "PENDING"
check "一次正确证据后仍标记 MISCONCEPTION（尚未解除）" \
  "$(jqv "$(cat "$UN_C1")" "d['data']['status']")" "MISCONCEPTION"
check "误区记录了反向证据 id" \
  "$(jqv "$(cat "$UN_C1")" "len(d['data']['misconceptions'][0]['reversalEvidenceIds'])")" "1"

# 4c. 回答之前的问题（CORRECT 反向证据 2，独立来源 TEST_ANSWER），误区解除。
ANS=$(api POST "/api/tests/$TEST_ID/answer" '{"userAnswer":"TLB 缓存的是页表项，也就是最近用过的页表条目。"}')
echo "$ANS" > "$OUT/session-b.test-answer.json"
TEST_EV=$(jqv "$ANS" "d['data']['evidenceId']")
TEST_SOURCE_ID=$(sql "SELECT source_id FROM evidence_source WHERE evidence_id=$TEST_EV LIMIT 1;")
check "测试判分正确" "$(jqv "$ANS" "d['data']['isCorrect']")" "True"
check "测试回答产生 CORRECT TEST_RESULT 证据" \
  "$(sql "SELECT correctness FROM learning_evidence WHERE id=$(jqv "$ANS" "d['data']['evidenceId']");")" "CORRECT"
check "测试证据来源是 TEST_ANSWER" \
  "$(sql "SELECT source_type FROM evidence_source WHERE evidence_id=$(jqv "$ANS" "d['data']['evidenceId']");")" "TEST_ANSWER"

api GET "/api/understandings/$TLB" > "$OUT/session-b.after-correction.json"
UN_C2="$OUT/session-b.after-correction.json"
check "两次独立正确证据后误区解除" \
  "$(jqv "$(cat "$UN_C2")" "len(d['data']['misconceptions'])")" "0"
check "纠正后状态不再停留在 MISCONCEPTION" \
  "$([ "$(jqv "$(cat "$UN_C2")" "d['data']['status']")" != "MISCONCEPTION" ] && echo yes || echo no)" "yes"
check "纠正后快照包含正确理解" \
  "$(jqv "$(cat "$UN_C2")" "len(d['data']['knownParts'])")" "2"
check "原错误证据没有被删除或改写" \
  "$(sql "SELECT correctness FROM learning_evidence WHERE id=$EV_ID;")" "PARTIAL"
check "显式标注的 INCORRECT 证据仍然存在" \
  "$(sql "SELECT COUNT(*) FROM learning_evidence WHERE user_id=$USER_A AND concept_id=$TLB AND correctness='INCORRECT';")" "1"

api GET "/api/understandings/$TLB/revisions" > "$OUT/session-b.revisions-final.json"
REV_C="$OUT/session-b.revisions-final.json"
REV_COUNT_F=$(jqv "$(cat "$REV_C")" "len(d['data'])")
checkge "纠正过程新增 Revision" "$REV_COUNT_F" "$((REV_COUNT_M + 2))"
check "历史版本中仍能看到曾经的误区" \
  "$(jqv "$(cat "$REV_C")" "any(len(r['misconceptions'])>0 for r in d['data'])")" "True"
check "最新版本已经没有误区" \
  "$(jqv "$(cat "$REV_C")" "len(d['data'][-1]['misconceptions'])")" "0"
check "最新 Revision 与 currentRevisionId 一致" \
  "$(jqv "$(cat "$UN_C2")" "d['data']['currentRevisionId']")" \
  "$(jqv "$(cat "$REV_C")" "d['data'][-1]['id']")"
check "LearningState 同步为纠正后的状态" \
  "$(jqv "$(api GET "/api/learning-state/concepts/$TLB")" "d['data']['status']")" \
  "$(jqv "$(cat "$UN_C2")" "d['data']['status']")"

# =====================================================================
section "4d. 纠正后的下一次会话：当前状态与历史证据"
# 普通追问（不含证据触发词）：应读到「当前状态」，但 Context Builder 不注入 Revision 历史。
CC=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"纠正后普通追问"}')" "['data']['id']")
Q_C='请再讲讲 TLB 的要点。'
EV_BEFORE_CC=$(jqv "$(api GET "/api/understandings/$TLB/evidence")" "len(d['data'])")
curl -sN -m 60 -X POST "$BASE/api/conversations/$CC/messages" \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
  -d "$(python -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}, ensure_ascii=False))' "$Q_C")" \
  > "$OUT/session-c-normal.sse"
if wait_evidence "$TLB" "$((EV_BEFORE_CC + 1))" 30; then
  ok "普通追问产生了新的 OBSERVATION 证据"
else
  bad "普通追问没有在 30 秒内产生 OBSERVATION 证据"
fi
PREVIEW_C="$OUT/session-c.preview.json"
preview_context "$CC" "$Q_C" "$PREVIEW_C"
python - "$PREVIEW_C" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))["data"]
print("  普通追问区块:", [s["name"] for s in d["context"]["sections"]])
PY
HAS_UN_C=$(python - "$PREVIEW_C" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))["data"]
print(any(s["name"].startswith("understanding:") for s in d["context"]["sections"]))
PY
)
check "纠正后的普通追问仍注入当前理解" "$HAS_UN_C" "True"
SYS_C="$OUT/session-c.system.txt"
python - "$PREVIEW_C" "$SYS_C" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))["data"]
open(sys.argv[2], "w", encoding="utf-8").write(d["context"]["systemInstruction"])
PY
case "$(cat "$SYS_C")" in *"页表项"*) ok "当前理解已是纠正后的说法";; *) bad "当前理解没有纠正后的说法";; esac
case "$(cat "$SYS_C")" in *"TLB 就是页表本身"*) bad "普通追问仍把已解除的误区当作当前理解";; *) ok "普通追问不再把旧误区当作当前理解";; esac
HAS_EV_C=$(python - "$PREVIEW_C" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))["data"]
print(any(s["name"].startswith("evidence:") for s in d["context"]["sections"]))
PY
)
check "普通追问不自动注入历史证据（当前实现的边界）" "$HAS_EV_C" "False"
HAS_TRAJ_C=$(python - "$PREVIEW_C" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))["data"]
print(any(s["name"].startswith("trajectory:") for s in d["context"]["sections"]))
PY
)
check "普通追问不无条件注入完整学习轨迹" "$HAS_TRAJ_C" "False"

# 复盘类问题（含「复盘」触发词）：5.2 起历史证据合并进 trajectory 区块，
# 不再单独渲染 evidence 区块。这里验证实际内容、来源 ID 和状态标注，
# 而不是只检查区块名。
CD=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"纠正后复盘"}')" "['data']['id']")
Q_D='复盘一下：我之前对 TLB 的理解错在哪里？'
EV_BEFORE_CD=$(jqv "$(api GET "/api/understandings/$TLB/evidence")" "len(d['data'])")
curl -sN -m 60 -X POST "$BASE/api/conversations/$CD/messages" \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
  -d "$(python -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}, ensure_ascii=False))' "$Q_D")" \
  > "$OUT/session-d-review.sse"
if wait_evidence "$TLB" "$((EV_BEFORE_CD + 1))" 30; then
  ok "复盘问题产生了新的 OBSERVATION 证据"
else
  bad "复盘问题没有在 30 秒内产生 OBSERVATION 证据"
fi
PREVIEW_D="$OUT/session-d.preview.json"
preview_context "$CD" "$Q_D" "$PREVIEW_D"
python - "$PREVIEW_D" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))["data"]
retrieve = next((e for e in d.get("trace", {}).get("events", [])
                 if e.get("type") == "RETRIEVE"), {})
print("  复盘区块:", [s["name"] for s in d["context"]["sections"]])
print("  复盘轨迹证据 ids:", retrieve.get("trajectoryEvidenceIds"))
PY
HAS_EV_D=$(python - "$PREVIEW_D" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))["data"]
print(any(s["name"].startswith("evidence:") for s in d["context"]["sections"]))
PY
)
check "复盘不再重复注入独立 Evidence 区块" "$HAS_EV_D" "False"

# 5.2 的复盘证据在 trajectory 区块里。除了区块是否存在，还要核对
# trace 关联到的 evidence id、Context 原文和来源 id，避免把区块改名当成通过。
TRAJ_RESULTS=$(python - "$PREVIEW_D" "$INCORRECT_ID" "$CORRECT_HL_EV" "$TEST_EV" \
  "$USER_MSG_ID" "$HL_ID" "$CORRECT_HL_ID" "$TEST_SOURCE_ID" <<'PY'
import json, re, sys

raw = sys.argv[1:9]
preview = raw[0]
incorrect_id = int(raw[1] or 0)
correct_hl_ev = int(raw[2] or 0)
test_ev = int(raw[3] or 0)
user_msg_id = int(raw[4] or 0)
wrong_hl_id = int(raw[5] or 0)
correct_hl_id = int(raw[6] or 0)
test_source_id = int(raw[7] or 0)

d = json.load(open(preview, encoding="utf-8"))["data"]
sections = d["context"]["sections"]
text = d["context"]["systemInstruction"]
retrieve = next((e for e in d.get("trace", {}).get("events", [])
                 if e.get("type") == "RETRIEVE"), {})
ids = {int(x) for x in (retrieve.get("trajectoryEvidenceIds") or [])}

checks = [
    ("复盘注入 trajectory 区块",
     any(s["name"].startswith("trajectory:") for s in sections)),
    ("轨迹读取至少 3 个 Revision",
     int(retrieve.get("trajectoryRevisions") or 0) >= 3),
    ("轨迹在 trace 里关联原始错误证据",
     incorrect_id in ids),
    ("轨迹在 trace 里关联纠正批注证据",
     correct_hl_ev in ids),
    ("轨迹在 trace 里关联测试纠正证据",
     test_ev in ids),
    ("原始错误原文进入 Context",
     "TLB 就是页表本身" in text),
    ("原始错误保留显式误区批注来源（可读类型）",
     "答错/用户纠正批注" in text and "来源：批注" in text),
    ("两条纠正证据来源分别标为批注与测试回答",
     text.count("来源：批注") >= 2 and "来源：测试回答" in text),
    ("历史误区标注为「已纠正，历史」",
     "[已纠正，历史]" in text),
    ("当前状态来自持久化快照且为已能讲清",
     "当前状态（来自持久化快照）：已能讲清" in text),
    ("已纠正后不再出现「当前仍存在的误区」",
     "当前仍存在的误区" not in text),
    ("用户可见上下文不泄露内部证据 / 来源 id",
     not re.search(r"(?:消息|批注|测试回答|证据)#\d+", text)),
]
for label, passed in checks:
    print(f"{label}|{passed}")
PY
)
while IFS='|' read -r label value; do
  [ -z "$label" ] && continue
  # Windows 的 Python 写 stdout 时会把 \n 翻译成 \r\n；命令替换只去掉最后一个
  # 换行，因此除最后一行外 value 都带尾随 \r，必须先剥掉再比较。
  value=${value%$'\r'}
  if [ "$value" = "True" ]; then
    ok "$label"
  else
    bad "$label"
  fi
done <<< "$TRAJ_RESULTS"

# =====================================================================
section "5. 负向 / 边界"
# 5a. 闲聊不应产生学习结论。
REV_BEFORE=$(jqv "$(api GET "/api/understandings/$TLB/revisions")" "len(d['data'])")
EV_BEFORE=$(jqv "$(api GET "/api/understandings/$TLB/evidence")" "len(d['data'])")
CC=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"闲聊"}')" "['data']['id']")
curl -sN -m 60 -X POST "$BASE/api/conversations/$CC/messages" \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
  -d '{"content":"谢谢你的解释，辛苦了！"}' > "$OUT/greeting.sse"
sleep 3
check "闲聊不新增该概念的 Evidence" \
  "$(jqv "$(api GET "/api/understandings/$TLB/evidence")" "len(d['data'])")" "$EV_BEFORE"
check "闲聊不新增 Revision" \
  "$(jqv "$(api GET "/api/understandings/$TLB/revisions")" "len(d['data'])")" "$REV_BEFORE"
check "闲聊不产生学习会话" \
  "$(jqv "$(api GET "/api/conversations/$CC")" "['data']['sessionId']")" "None"

# 5b. 只有「提及/提问」时最多推进到 EXPOSED，不虚构掌握或误区。
CD=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"无信号"}')" "['data']['id']")
curl -sN -m 60 -X POST "$BASE/api/conversations/$CD/messages" \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
  -d '{"content":"Page Table 是什么？"}' > "$OUT/no-signal.sse"
if wait_evidence "$PT" 1 30; then
  UN_PT="$OUT/no-signal.understanding.json"
  api GET "/api/understandings/$PT" > "$UN_PT"
  STATUS_PT=$(jqv "$(cat "$UN_PT")" "d['data']['status']")
  case "$STATUS_PT" in EXPOSED|UNKNOWN) ok "仅提问只推进到接触状态（$STATUS_PT）";; *) bad "仅提问却得到 $STATUS_PT";; esac
  check "没有凭空产生已理解条目" "$(jqv "$(cat "$UN_PT")" "len(d['data']['knownParts'])")" "0"
  check "没有凭空产生误区" "$(jqv "$(cat "$UN_PT")" "len(d['data']['misconceptions'])")" "0"
else
  bad "Page Table 的提问没有产生 OBSERVATION 证据"
fi

# 5c. 旧 Memory 与新 Evidence 冲突：两者都保留，不静默覆盖。
MEM=$(api POST /api/memories \
  "{\"learningSpaceId\":$SPACE_ID,\"conceptId\":$TLB,\"memoryType\":\"LEARNING\",\"content\":\"用户认为 TLB 缓存整个页表\"}")
MEM_ID=$(jqv "$MEM" "['data']['id']")
checknot "创建一条与当前 Evidence 冲突的 ACTIVE Memory" "$MEM_ID" "None"
check "冲突 Memory 未被 Evidence 静默删除" \
  "$(sql "SELECT status FROM memory WHERE id=$MEM_ID;")" "ACTIVE"
check "冲突 Memory 内容未被改写" \
  "$(sql "SELECT content FROM memory WHERE id=$MEM_ID;")" "用户认为 TLB 缓存整个页表"
MEM_SEARCH=$(api GET "/api/memories/search?spaceId=$SPACE_ID&conceptId=$TLB&keyword=$(python -c 'import urllib.parse;print(urllib.parse.quote("TLB"))')")
check "冲突 Memory 仍可通过正式检索读到" \
  "$(jqv "$MEM_SEARCH" "len([m for m in d['data'] if m['id']==$MEM_ID])")" "1"

# 5d. 跨用户隔离：B 不能读 A 的认知数据，也不能往 A 的对话发消息。
TOKEN_SAVE="$TOKEN"; TOKEN="$TOKEN_B"
check "用户 B 读 A 的 Understanding 返回 404" "$(api GET "/api/understandings/$TLB" > /dev/null; code)" "404"
# Evidence 列表按登录用户过滤；B 得到的是空列表而不是 404。关键是「不泄露 A 的任何证据」。
B_EV=$(api GET "/api/understandings/$TLB/evidence")
check "用户 B 的 Evidence 列表为空（不泄露 A 的证据）" \
  "$(jqv "$B_EV" "len(d['data'])")" "0"
check "用户 B 用 A 的 evidenceId 读详情返回 404" \
  "$(api GET "/api/evidence/$EV_ID" > /dev/null; code)" "404"
check "用户 B 读 A 的 LearningState 返回 404" "$(api GET "/api/learning-state/concepts/$TLB" > /dev/null; code)" "404"
INTRUDE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/conversations/$CB/messages" \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN_B" -d '{"content":"越权写入"}')
check "用户 B 不能向 A 的对话发消息" "$INTRUDE" "404"
TOKEN="$TOKEN_SAVE"

# =====================================================================
section "6. 数据库事实核对（只读）"
echo "  -- Understanding / Revision / LearningState --"
sql "SELECT id, user_id, concept_id, status, current_revision_id, \
  LEFT(known_parts,80), LEFT(misconceptions,80) FROM learning_understanding WHERE user_id=$USER_A AND concept_id=$TLB;"
sql "SELECT revision_no, status, trigger_evidence_id, \
  LEFT(COALESCE(misconceptions,'[]'),60) FROM understanding_revision \
  WHERE understanding_id=$UN_ID ORDER BY revision_no;"
sql "SELECT concept_id, status, last_evidence_at, review_priority FROM learning_state \
  WHERE user_id=$USER_A AND concept_id IN ($TLB,$PT);"
echo "  -- Evidence 来源链（append-only）--"
sql "SELECT e.id, e.evidence_type, e.correctness, es.source_type, es.source_id \
  FROM learning_evidence e LEFT JOIN evidence_source es ON es.evidence_id=e.id \
  WHERE e.user_id=$USER_A AND e.concept_id=$TLB ORDER BY e.id;"

# =====================================================================
printf '\n\033[1m结果：PASS=%d FAIL=%d SKIP=%d\033[0m\n' "$PASS" "$FAIL" "$SKIP"
printf '产物目录：%s\n' "$OUT"
echo "用户 A：$UA (id=$USER_A)  space=$SPACE_ID  TLB=$TLB  PageTable=$PT"
echo "会话 A=$CA  B=$CB  学习活动=$CA_SESS"
echo
if [ "$FAIL" -gt 0 ]; then
  echo "存在失败项，先看上面的 FAIL 行与 $OUT 下的响应原文。"
  exit 1
fi
echo "全部通过。注意：本脚本使用 Mock LLM，只能证明链路与上下文注入，不能证明真实语义判断或教学效果。"
