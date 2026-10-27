#!/usr/bin/env bash
# 阶段 5.3 真实模型验收的固定数据集准备（Mock 分析链路）。
#
# 只通过公开 API 产生数据；直接 SQL 不写业务数据。脚本结束后输出 summary.json，
# 供 phase53_real_model_eval.py 读取用户名、空间、概念与预期证据 id。
#
# 前置：
#   1. 后端 8080（inline 分析）与 Python Agent 8000（provider=mock）已启动；
#   2. MySQL 可连（仅用于只读核对，不插入/修改）。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_SRC="$ROOT/backend/backend/src/main/resources"
BASE="${BASE:-http://127.0.0.1:8080}"
AGENT="${AGENT:-http://127.0.0.1:8000}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$ROOT/backend/backend/target/phase53-eval/$STAMP"
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

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
check(){ if [ "$2" = "$3" ]; then ok "$1（= $3）"; else bad "$1：实际 [$2]，期望 [$3]"; fi; }
section(){ printf '\n\033[1m== %s\033[0m\n' "$1"; }

jqv(){ printf '%s' "$1" | python -c "
import sys, json
d = json.load(sys.stdin)
expr = sys.argv[1]
print(eval(('d' + expr) if expr.startswith('[') else expr))
" "$2" 2>/dev/null; }

TOKEN=""
api() {
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -s -X "$method" "$BASE$path" -H 'Content-Type: application/json' \
      -H "Authorization: Bearer $TOKEN" -d "$body"
  else
    curl -s -X "$method" "$BASE$path" -H "Authorization: Bearer $TOKEN"
  fi
}
send_message() {
  curl -sN -m 90 -X POST "$BASE/api/conversations/$1/messages" \
    -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
    -d "$(python -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}, ensure_ascii=False))' "$2")"
}
register_user() {
  local name="$1" reg login
  reg=$(curl -s -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$name\",\"password\":\"Passw0rd!23\"}")
  if [ "$(jqv "$reg" "['code']")" = "0" ]; then
    printf '%s' "$reg"
  else
    login=$(curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
      -d "{\"username\":\"$name\",\"password\":\"Passw0rd!23\"}")
    printf '%s' "$login"
  fi
}
wait_evidence() {
  local cid="$1" min="$2" secs="${3:-45}" i=0 n
  while [ "$i" -lt "$secs" ]; do
    n=$(jqv "$(api GET "/api/understandings/$cid/evidence")" "len(d['data'])")
    if [ "${n:-0}" -ge "$min" ]; then return 0; fi
    sleep 1; i=$((i+1))
  done
  return 1
}
wait_status() {
  local cid="$1" want="$2" secs="${3:-30}" i=0 s
  while [ "$i" -lt "$secs" ]; do
    s=$(jqv "$(api GET "/api/understandings/$cid")" "['data']['status']")
    if [ "$s" = "$want" ]; then return 0; fi
    sleep 1; i=$((i+1))
  done
  return 1
}
highlight() { # highlight <messageId> <markType> <selectedText>
  api POST "/api/messages/$1/highlights" \
    "$(python -c 'import json,sys; print(json.dumps({"markType":sys.argv[1],"selectedText":sys.argv[2],"note":""}, ensure_ascii=False))' "$2" "$3")"
}
# =====================================================================
section "0. 前置检查"
AHEALTH=$(curl -s -m 3 "$AGENT/health")
PROVIDER=$(printf '%s' "$AHEALTH" | python -c 'import sys,json;print(json.load(sys.stdin).get("llm_provider"))' 2>/dev/null)
if [ "$PROVIDER" != "mock" ]; then
  echo "数据准备要求 Agent 处于 mock 模式；当前 provider=[$PROVIDER]。" >&2
  exit 1
fi
if ! sql "SELECT 1" > /dev/null; then
  echo "MySQL 连接失败（读取 application-local.yml 的凭证）" >&2
  exit 1
fi
ok "Agent（mock）、后端与 MySQL 可用"

# =====================================================================
section "1. 场景 A：当前仍存在 ACTIVE 误区"
UA="p53a_$STAMP"
REG_A=$(register_user "$UA")
TOKEN=$(jqv "$REG_A" "['data']['token']")
USER_A=$(jqv "$REG_A" "['data']['user']['id']")
check "场景 A 用户注册" "$(jqv "$REG_A" "['code']")" "0"
SPACE_A=$(api POST /api/spaces "{\"name\":\"阶段5.3 A $STAMP\",\"description\":\"当前 ACTIVE 误区\"}")
SPACE_A_ID=$(jqv "$SPACE_A" "['data']['id']")
TLB_A=$(jqv "$(api POST "/api/spaces/$SPACE_A_ID/concepts" \
  '{"canonicalName":"TLB","displayName":"TLB","description":"页表项的高速缓存"}')" "['data']['id']")
PT_A=$(jqv "$(api POST "/api/spaces/$SPACE_A_ID/concepts" \
  '{"canonicalName":"Page Table","displayName":"Page Table","description":"页表"}')" "['data']['id']")
check "场景 A 空间与概念创建" "$( [ -n "$SPACE_A_ID" ] && [ -n "$TLB_A" ] && echo ok )" "ok"

CA=$(jqv "$(api POST "/api/spaces/$SPACE_A_ID/conversations" '{"title":"A 错误解释"}')" "['data']['id']")
WRONG='我理解 TLB 就是页表本身，它缓存的是整个页表；所以 TLB miss 就等于缺页中断。'
send_message "$CA" "$WRONG" > "$OUT/a-wrong.sse"
if wait_evidence "$TLB_A" 1 45; then ok "错误解释已产生 Evidence"; else bad "45 秒内没有产生 Evidence"; fi
api GET "/api/conversations/$CA/messages" > "$OUT/a.messages.json"
WRONG_MSG=$(jqv "$(cat "$OUT/a.messages.json")" "next(m['id'] for m in d['data'] if m['role']=='USER' and 'TLB' in m['content'])")
HL_A=$(highlight "$WRONG_MSG" "MISCONCEPTION_CANDIDATE" "TLB 就是页表本身，它缓存的是整个页表")
HL_A_ID=$(jqv "$HL_A" "['data']['id']")
if wait_status "$TLB_A" "MISCONCEPTION" 15; then ok "当前 Understanding 进入 MISCONCEPTION"; else bad "未进入 MISCONCEPTION"; fi
api GET "/api/understandings/$TLB_A" > "$OUT/a.understanding.json"
api GET "/api/understandings/$TLB_A/evidence" > "$OUT/a.evidence.json"
api GET "/api/understandings/$TLB_A/revisions" > "$OUT/a.revisions.json"
UN_A_ID=$(jqv "$(cat "$OUT/a.understanding.json")" "['data']['id']")
check "当前状态 = MISCONCEPTION" "$(jqv "$(cat "$OUT/a.understanding.json")" "['data']['status']")" "MISCONCEPTION"
check "误区状态 = ACTIVE" "$(jqv "$(cat "$OUT/a.understanding.json")" "d['data']['misconceptions'][0]['status']")" "ACTIVE"
check "未把未验证内容当成已掌握" "$(jqv "$(cat "$OUT/a.understanding.json")" "len(d['data']['knownParts'])")" "0"
WRONG_EV_A=$(jqv "$(cat "$OUT/a.evidence.json")" "next(e['id'] for e in d['data'] if e['correctness']=='INCORRECT')")
check "误区证据来源为批注" \
  "$(jqv "$(cat "$OUT/a.evidence.json")" "next(s['sourceType'] for e in d['data'] if e['id']==$WRONG_EV_A for s in e['sources'])")" "HIGHLIGHT"

# =====================================================================
section "2. 场景 B/C/D：误区已纠正 + 冲突 Memory"
UB="p53b_$STAMP"
REG_B=$(register_user "$UB")
TOKEN=$(jqv "$REG_B" "['data']['token']")
USER_B=$(jqv "$REG_B" "['data']['user']['id']")
check "场景 B 用户注册" "$(jqv "$REG_B" "['code']")" "0"
SPACE_B=$(api POST /api/spaces "{\"name\":\"阶段5.3 BCD $STAMP\",\"description\":\"历史轨迹与当前状态\"}")
SPACE_B_ID=$(jqv "$SPACE_B" "['data']['id']")
TLB_B=$(jqv "$(api POST "/api/spaces/$SPACE_B_ID/concepts" \
  '{"canonicalName":"TLB","displayName":"TLB","description":"页表项的高速缓存"}')" "['data']['id']")
PT_B=$(jqv "$(api POST "/api/spaces/$SPACE_B_ID/concepts" \
  '{"canonicalName":"Page Table","displayName":"Page Table","description":"页表"}')" "['data']['id']")
check "场景 B 空间与概念创建" "$( [ -n "$SPACE_B_ID" ] && [ -n "$TLB_B" ] && echo ok )" "ok"

CB0=$(jqv "$(api POST "/api/spaces/$SPACE_B_ID/conversations" '{"title":"B 错误解释"}')" "['data']['id']")
send_message "$CB0" "$WRONG" > "$OUT/b-wrong.sse"
if wait_evidence "$TLB_B" 1 45; then ok "错误解释已产生 Evidence"; else bad "45 秒内没有产生 Evidence"; fi
api GET "/api/conversations/$CB0/messages" > "$OUT/b-wrong.messages.json"
WRONG_MSG_B=$(jqv "$(cat "$OUT/b-wrong.messages.json")" "next(m['id'] for m in d['data'] if m['role']=='USER' and 'TLB' in m['content'])")
HL_B1=$(highlight "$WRONG_MSG_B" "MISCONCEPTION_CANDIDATE" "TLB 就是页表本身，它缓存的是整个页表")
HL_B1_ID=$(jqv "$HL_B1" "['data']['id']")
if wait_status "$TLB_B" "MISCONCEPTION" 15; then ok "误区建立"; else bad "误区未建立"; fi
WRONG_EV_B=$(jqv "$(api GET "/api/understandings/$TLB_B/evidence")" "next(e['id'] for e in d['data'] if e['correctness']=='INCORRECT')")

CB1=$(jqv "$(api POST "/api/spaces/$SPACE_B_ID/conversations" '{"title":"B 纠正"}')" "['data']['id']")
CORRECT='我认为我现在明白了：TLB 缓存的是页表项，不是整个页表；TLB miss 之后才需要查页表，缺页是页面不在内存。'
EV_BEFORE=$(jqv "$(api GET "/api/understandings/$TLB_B/evidence")" "len(d['data'])")
send_message "$CB1" "$CORRECT" > "$OUT/b-correct.sse"
if wait_evidence "$TLB_B" "$((EV_BEFORE + 1))" 45; then ok "纠正解释已产生 Evidence"; else bad "纠正解释 45 秒内未落库"; fi
api GET "/api/conversations/$CB1/messages" > "$OUT/b-correct.messages.json"
CORRECT_MSG=$(jqv "$(cat "$OUT/b-correct.messages.json")" "max(m['id'] for m in d['data'] if m['role']=='USER')")
HL_B2=$(highlight "$CORRECT_MSG" "MY_UNDERSTANDING" "TLB 缓存的是页表项，不是整个页表")
HL_B2_ID=$(jqv "$HL_B2" "['data']['id']")
if wait_status "$TLB_B" "MISCONCEPTION" 15; then ok "第一条正确证据后仍为 MISCONCEPTION（误区转 PENDING）"; else bad "状态异常"; fi
api GET "/api/understandings/$TLB_B" > "$OUT/b.after-first.json"
check "误区降为 PENDING" "$(jqv "$(cat "$OUT/b.after-first.json")" "d['data']['misconceptions'][0]['status']")" "PENDING"

TEST=$(api POST /api/tests \
  "{\"conceptId\":$TLB_B,\"conversationId\":$CB1,\"questionType\":\"SHORT_ANSWER\",\"questionText\":\"TLB 中缓存的是什么？\",\"correctAnswer\":\"页表项\"}")
TEST_ID=$(jqv "$TEST" "['data']['id']")
ANS=$(api POST "/api/tests/$TEST_ID/answer" '{"userAnswer":"TLB 缓存的是页表项，也就是最近用过的页表条目。"}')
TEST_EV=$(jqv "$ANS" "['data']['evidenceId']")
if wait_status "$TLB_B" "DEMONSTRATED" 20; then ok "两条独立正确证据后进入 DEMONSTRATED"; else bad "未进入 DEMONSTRATED"; fi
api GET "/api/understandings/$TLB_B" > "$OUT/b.understanding.json"
api GET "/api/understandings/$TLB_B/evidence" > "$OUT/b.evidence.json"
api GET "/api/understandings/$TLB_B/revisions" > "$OUT/b.revisions.json"
api GET "/api/learning-state/concepts/$TLB_B" > "$OUT/b.learning-state.json"
UN_B_ID=$(jqv "$(cat "$OUT/b.understanding.json")" "['data']['id']")
check "当前状态 = DEMONSTRATED" "$(jqv "$(cat "$OUT/b.understanding.json")" "['data']['status']")" "DEMONSTRATED"
check "当前快照无误区" "$(jqv "$(cat "$OUT/b.understanding.json")" "len(d['data']['misconceptions'])")" "0"
check "LearningState 同步 DEMONSTRATED" "$(jqv "$(cat "$OUT/b.learning-state.json")" "['data']['status']")" "DEMONSTRATED"
check "历史 Revision 保留 ACTIVE 误区" \
  "$(jqv "$(cat "$OUT/b.revisions.json")" "any(m['status']=='ACTIVE' for r in d['data'] for m in r['misconceptions'])")" "True"
CORRECT_HL_EV=$(jqv "$(cat "$OUT/b.evidence.json")" "next(e['id'] for e in d['data'] if e['correctness']=='CORRECT' and any(s['sourceType']=='HIGHLIGHT' for s in e['sources']))")
check "纠正批注证据存在" "$( [ -n "$CORRECT_HL_EV" ] && [ "$CORRECT_HL_EV" != "None" ] && echo ok )" "ok"

MEM=$(api POST /api/memories \
  "{\"learningSpaceId\":$SPACE_B_ID,\"conceptId\":$TLB_B,\"memoryType\":\"LEARNING\",\"content\":\"用户认为 TLB 缓存整个页表\"}")
MEM_ID=$(jqv "$MEM" "['data']['id']")
check "冲突 Memory 创建成功且为 ACTIVE" "$(jqv "$MEM" "['data']['status']")" "ACTIVE"
api GET "/api/memories/search?spaceId=$SPACE_B_ID&conceptId=$TLB_B&keyword=TLB" > "$OUT/b.memories.json"

# =====================================================================
section "3. 写入 summary"
python - "$OUT" "$STAMP" "$UA" "$USER_A" "$SPACE_A_ID" "$TLB_A" "$PT_A" "$UN_A_ID" \
  "$WRONG_MSG" "$HL_A_ID" "$WRONG_EV_A" \
  "$UB" "$USER_B" "$SPACE_B_ID" "$TLB_B" "$PT_B" "$UN_B_ID" \
  "$WRONG_MSG_B" "$HL_B1_ID" "$WRONG_EV_B" "$CORRECT_MSG" "$HL_B2_ID" \
  "$CORRECT_HL_EV" "$TEST_EV" "$MEM_ID" <<'PY'
import json, sys
out = sys.argv[1]
keys = [
    "out_dir", "stamp", "user_a_name", "user_a_id", "space_a_id", "tlb_a", "pt_a",
    "understanding_a_id", "wrong_message_a", "wrong_highlight_a", "wrong_evidence_a",
    "user_b_name", "user_b_id", "space_b_id", "tlb_b", "pt_b", "understanding_b_id",
    "wrong_message_b", "wrong_highlight_b", "wrong_evidence_b", "correct_message_b",
    "correct_highlight_b", "correct_highlight_evidence_b", "test_evidence_b", "memory_b_id",
]
vals = sys.argv[2:]
data = dict(zip(keys[1:], vals))
data["out_dir"] = out
data["password"] = "Passw0rd!23"
data["questions"] = {
    "A": "TLB 缓存的是什么？",
    "B": "TLB 的核心要点是什么？",
    "C": "我之前对 TLB 的理解错在哪里？后来又是怎么纠正的？",
    "D": "当前应如何理解 TLB？",
}
with open(out + "/summary.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print(json.dumps(data, ensure_ascii=False, indent=2))
PY

printf '\n\033[1m结果：PASS=%d FAIL=%d\033[0m\n产物目录：%s\n' "$PASS" "$FAIL" "$OUT"
[ "$FAIL" -eq 0 ] || exit 1
