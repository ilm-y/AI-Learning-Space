#!/usr/bin/env bash
# 阶段 5.3 场景 B/C 的干净数据集：DEMONSTRATED，且不创建冲突 Memory。
# 与 phase53-setup.sh 使用同一套公开 API；输出 summary-clean.json。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_SRC="$ROOT/backend/backend/src/main/resources"
BASE="${BASE:-http://127.0.0.1:8080}"
AGENT="${AGENT:-http://127.0.0.1:8000}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="${OUT:-$ROOT/backend/backend/target/phase53-eval/$STAMP}"
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
highlight() {
  api POST "/api/messages/$1/highlights" \
    "$(python -c 'import json,sys; print(json.dumps({"markType":sys.argv[1],"selectedText":sys.argv[2],"note":""}, ensure_ascii=False))' "$2" "$3")"
}

if [ -z "${AILS_INTERNAL_TOKEN:-}" ]; then
  echo "需要 AILS_INTERNAL_TOKEN 读取 /health/diagnostics（/health 只回存活状态）。" >&2; exit 1
fi
AHEALTH=$(curl -s -m 3 -H "X-Internal-Token: $AILS_INTERNAL_TOKEN" \
  -H "X-Internal-User-Id: 1" "$AGENT/health/diagnostics")
PROVIDER=$(printf '%s' "$AHEALTH" | python -c 'import sys,json;print(json.load(sys.stdin).get("llm_provider"))' 2>/dev/null)
if [ "$PROVIDER" != "mock" ]; then
  echo "数据准备要求 Agent 处于 mock 模式；当前 provider=[$PROVIDER]。" >&2
  exit 1
fi
if ! sql "SELECT 1" > /dev/null; then
  echo "MySQL 连接失败" >&2
  exit 1
fi

UC="p53c_$STAMP"
REG=$(curl -s -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$UC\",\"password\":\"Passw0rd!23\"}")
if [ "$(jqv "$REG" "['code']")" != "0" ]; then
  REG=$(curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$UC\",\"password\":\"Passw0rd!23\"}")
fi
TOKEN=$(jqv "$REG" "['data']['token']")
USER_C=$(jqv "$REG" "['data']['user']['id']")
check "干净用户注册" "$(jqv "$REG" "['code']")" "0"

SPACE=$(api POST /api/spaces "{\"name\":\"阶段5.3 BC 干净 $STAMP\",\"description\":\"已纠正、无冲突 Memory\"}")
SPACE_ID=$(jqv "$SPACE" "['data']['id']")
TLB=$(jqv "$(api POST "/api/spaces/$SPACE_ID/concepts" \
  '{"canonicalName":"TLB","displayName":"TLB","description":"页表项的高速缓存"}')" "['data']['id']")
PT=$(jqv "$(api POST "/api/spaces/$SPACE_ID/concepts" \
  '{"canonicalName":"Page Table","displayName":"Page Table","description":"页表"}')" "['data']['id']")
check "干净空间与概念创建" "$( [ -n "$SPACE_ID" ] && [ -n "$TLB" ] && echo ok )" "ok"

C0=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"C 错误解释"}')" "['data']['id']")
WRONG='我理解 TLB 就是页表本身，它缓存的是整个页表；所以 TLB miss 就等于缺页中断。'
send_message "$C0" "$WRONG" > "$OUT/c-wrong.sse"
if wait_evidence "$TLB" 1 45; then ok "错误解释已产生 Evidence"; else bad "错误解释未落库"; fi
api GET "/api/conversations/$C0/messages" > "$OUT/c-wrong.messages.json"
WRONG_MSG=$(jqv "$(cat "$OUT/c-wrong.messages.json")" "next(m['id'] for m in d['data'] if m['role']=='USER' and 'TLB' in m['content'])")
HL1=$(highlight "$WRONG_MSG" "MISCONCEPTION_CANDIDATE" "TLB 就是页表本身，它缓存的是整个页表")
HL1_ID=$(jqv "$HL1" "['data']['id']")
if wait_status "$TLB" "MISCONCEPTION" 15; then ok "误区建立"; else bad "误区未建立"; fi
WRONG_EV=$(jqv "$(api GET "/api/understandings/$TLB/evidence")" "next(e['id'] for e in d['data'] if e['correctness']=='INCORRECT')")

C1=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"C 纠正"}')" "['data']['id']")
CORRECT='我认为我现在明白了：TLB 缓存的是页表项，不是整个页表；TLB miss 之后才需要查页表，缺页是页面不在内存。'
BEFORE=$(jqv "$(api GET "/api/understandings/$TLB/evidence")" "len(d['data'])")
send_message "$C1" "$CORRECT" > "$OUT/c-correct.sse"
if wait_evidence "$TLB" "$((BEFORE + 1))" 45; then ok "纠正解释已产生 Evidence"; else bad "纠正解释未落库"; fi
api GET "/api/conversations/$C1/messages" > "$OUT/c-correct.messages.json"
CORRECT_MSG=$(jqv "$(cat "$OUT/c-correct.messages.json")" "max(m['id'] for m in d['data'] if m['role']=='USER')")
HL2=$(highlight "$CORRECT_MSG" "MY_UNDERSTANDING" "TLB 缓存的是页表项，不是整个页表")
HL2_ID=$(jqv "$HL2" "['data']['id']")
if wait_status "$TLB" "MISCONCEPTION" 15; then ok "误区转为 PENDING"; else bad "状态异常"; fi
api GET "/api/understandings/$TLB" > "$OUT/c.after-first.json"
check "误区 PENDING" "$(jqv "$(cat "$OUT/c.after-first.json")" "d['data']['misconceptions'][0]['status']")" "PENDING"

TEST=$(api POST /api/tests \
  "{\"conceptId\":$TLB,\"conversationId\":$C1,\"questionType\":\"SHORT_ANSWER\",\"questionText\":\"TLB 中缓存的是什么？\",\"correctAnswer\":\"页表项\"}")
TEST_ID=$(jqv "$TEST" "['data']['id']")
ANS=$(api POST "/api/tests/$TEST_ID/answer" '{"userAnswer":"TLB 缓存的是页表项，也就是最近用过的页表条目。"}')
TEST_EV=$(jqv "$ANS" "['data']['evidenceId']")
if wait_status "$TLB" "DEMONSTRATED" 20; then ok "进入 DEMONSTRATED"; else bad "未进入 DEMONSTRATED"; fi
api GET "/api/understandings/$TLB" > "$OUT/c.understanding.json"
api GET "/api/understandings/$TLB/evidence" > "$OUT/c.evidence.json"
api GET "/api/understandings/$TLB/revisions" > "$OUT/c.revisions.json"
api GET "/api/learning-state/concepts/$TLB" > "$OUT/c.learning-state.json"
UN_ID=$(jqv "$(cat "$OUT/c.understanding.json")" "['data']['id']")
check "当前状态 = DEMONSTRATED" "$(jqv "$(cat "$OUT/c.understanding.json")" "['data']['status']")" "DEMONSTRATED"
check "历史 Revision 保留 ACTIVE 误区" \
  "$(jqv "$(cat "$OUT/c.revisions.json")" "any(m['status']=='ACTIVE' for r in d['data'] for m in r['misconceptions'])")" "True"
check "空间中不存在冲突 Memory" \
  "$(jqv "$(api GET "/api/memories/search?spaceId=$SPACE_ID&conceptId=$TLB&keyword=TLB")" "len(d['data'])")" "0"

python - "$OUT" "$STAMP" "$UC" "$USER_C" "$SPACE_ID" "$TLB" "$PT" "$UN_ID" \
  "$WRONG_MSG" "$HL1_ID" "$WRONG_EV" "$CORRECT_MSG" "$HL2_ID" "$TEST_EV" <<'PY'
import json, sys
out, stamp, user_name, user_id, space_id, tlb, pt, un_id, \
    wrong_msg, wrong_hl, wrong_ev, correct_msg, correct_hl, test_ev = sys.argv[1:15]
data = {
    "out_dir": out, "stamp": stamp, "user_c_name": user_name, "user_c_id": user_id,
    "space_c_id": space_id, "tlb_c": tlb, "pt_c": pt, "understanding_c_id": un_id,
    "wrong_message_c": wrong_msg, "wrong_highlight_c": wrong_hl,
    "wrong_evidence_c": wrong_ev, "correct_message_c": correct_msg,
    "correct_highlight_c": correct_hl, "test_evidence_c": test_ev,
    "password": "Passw0rd!23",
    "questions": {
        "B": "TLB 的核心要点是什么？",
        "C": "我之前对 TLB 的理解错在哪里？后来又是怎么纠正的？",
    },
}
with open(out + "/summary-clean.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print(json.dumps(data, ensure_ascii=False, indent=2))
PY

printf '\n\033[1m结果：PASS=%d FAIL=%d\033[0m\n产物目录：%s\n' "$PASS" "$FAIL" "$OUT"
[ "$FAIL" -eq 0 ] || exit 1
