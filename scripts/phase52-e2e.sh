#!/usr/bin/env bash
# 阶段 5.2 学习轨迹上下文与状态一致性验收（inline 分析链路 / Mock LLM）。
#
# 前置：
#   1. MySQL 可连（凭证从 application-local.yml 读，不写死）；
#   2. 后端 8080 已启动，ails.agent.gateway=http；
#   3. Python Agent 8000 已启动，AILS_LLM_PROVIDER=mock、AILS_RETRIEVER_MODE=auto。
#
# 用法：
#   bash scripts/phase52-e2e.sh
#
# 本脚本只通过公开 API / SSE / 分析链路产生数据；直接 SQL 只用于**核对**
# 数据库事实（来源、版本号、append-only、权限隔离），从不插入或修改业务数据。
#
# 覆盖场景：
#   A 当前误区：新会话的当前 Understanding + 历史证据，但不得当成已掌握；
#   B 已纠正误区：两条独立正确证据 → DEMONSTRATED，普通问题不注入历史；
#   C 显式复盘：原始错误 / 误区标注 / 纠正证据 / 当前状态 / 时间关系；
#   D 旧 Memory 与新 Evidence 冲突：保留来源、不静默覆盖、不当当前事实；
#   E 权限与无关问题：跨用户 / 跨空间隔离，闲聊不注入历史、不虚构掌握。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_SRC="$ROOT/backend/backend/src/main/resources"
BASE="${BASE:-http://127.0.0.1:8080}"
AGENT="${AGENT:-http://127.0.0.1:8000}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$ROOT/backend/backend/target/phase52-e2e/$STAMP"
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
# 从保存的 JSON 文件取字段。表达式里的 d 是完整响应体。
jf(){ python - "$1" "$2" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(eval(sys.argv[2]))
PY
}
has_section(){ python - "$1" "$2" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))["data"]["context"]["sections"]
print("yes" if any(s["name"].startswith(sys.argv[2]) for s in d) else "no")
PY
}
section_names(){ jf "$1" "','.join(s['name'] for s in d['data']['context']['sections'])"; }
system_text(){ jf "$1" "d['data']['context']['systemInstruction']"; }
retrieve_detail(){ python - "$1" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
e = next(x for x in d["data"]["trace"]["events"] if x["type"] == "RETRIEVE")
print(json.dumps(e, ensure_ascii=False))
PY
}

wait_evidence() { # wait_evidence <conceptId> <min> <seconds>
  local cid="$1" min="$2" secs="${3:-45}" i=0 n
  while [ "$i" -lt "$secs" ]; do
    n=$(jqv "$(api GET "/api/understandings/$cid/evidence")" "len(d['data'])")
    if [ "${n:-0}" -ge "$min" ]; then return 0; fi
    sleep 1; i=$((i+1))
  done
  return 1
}
wait_status() { # wait_status <conceptId> <status> <seconds>
  local cid="$1" want="$2" secs="${3:-30}" i=0 s
  while [ "$i" -lt "$secs" ]; do
    s=$(jqv "$(api GET "/api/understandings/$cid")" "['data']['status']")
    if [ "$s" = "$want" ]; then return 0; fi
    sleep 1; i=$((i+1))
  done
  return 1
}
preview_context() { # preview_context <userId> <conversationId> <spaceId> <question> <outfile>
  local uid="$1" cid="$2" sid="$3" q="$4" out="$5"
  python - "$uid" "$cid" "$sid" "$q" "$TOKEN" > "$out.payload" <<'PY'
import json, sys
uid, cid, sid, question, token = sys.argv[1:6]
print(json.dumps({
    "runId": 920000,
    "userId": int(uid),
    "conversationId": int(cid),
    "learningSpaceId": int(sid),
    "userMessage": question,
    "authToken": token,
}, ensure_ascii=False))
PY
  curl -s -m 90 -X POST "$AGENT/agent/context/preview" -H 'Content-Type: application/json' \
    -d @"$out.payload" > "$out"
}
send_message() { # send_message <conversationId> <text> <sse-file>
  curl -sN -m 90 -X POST "$BASE/api/conversations/$1/messages" \
    -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
    -d "$(python -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}, ensure_ascii=False))' "$2")" \
    > "$3"
}
register() {
  local name="$1" reg login
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
AHEALTH=$(curl -s "$AGENT/health")
PROVIDER=$(printf '%s' "$AHEALTH" | python -c 'import sys,json;print(json.load(sys.stdin).get("llm_provider"))')
if [ "$PROVIDER" != "mock" ]; then
  echo "本脚本按 Mock LLM 设计；当前 provider=$PROVIDER，结果不能与 Mock 基线混在一起。"
  echo "$AHEALTH"; exit 1
fi
if ! sql "SELECT 1" > /dev/null; then
  echo "MySQL 连接失败（读取 application-local.yml 的凭证）"; exit 1
fi
if [ "$(curl -s -o /dev/null -w '%{http_code}' -m 3 "$BASE/api/auth/login")" = "000" ]; then
  echo "后端不可用：$BASE"; exit 1
fi
ok "Agent（mock）、后端与 MySQL 均可用"

UA="p52a_$STAMP"
UB="p52b_$STAMP"
REG_A=$(register "$UA")
TOKEN=$(jqv "$REG_A" "['data']['token']")
USER_A=$(jqv "$REG_A" "['data']['user']['id']")
REG_B=$(register "$UB")
TOKEN_B=$(jqv "$REG_B" "['data']['token']")
USER_B=$(jqv "$REG_B" "['data']['user']['id']")
checknot "用户 A 注册并取得 token" "$TOKEN" ""
checknot "用户 B 注册并取得 token" "$TOKEN_B" ""

# =====================================================================
section "1. 场景 A：当前误区进入新会话"
SPACE=$(api POST /api/spaces "{\"name\":\"阶段5.2验收 $STAMP\",\"description\":\"学习轨迹与状态一致性\"}")
SPACE_ID=$(jqv "$SPACE" "['data']['id']")
TLB=$(jqv "$(api POST "/api/spaces/$SPACE_ID/concepts" \
  '{"canonicalName":"TLB","displayName":"TLB","description":"页表项的高速缓存"}')" "['data']['id']")
PT=$(jqv "$(api POST "/api/spaces/$SPACE_ID/concepts" \
  '{"canonicalName":"Page Table","displayName":"Page Table","description":"页表"}')" "['data']['id']")
checknot "创建学习空间与 TLB/Page Table 概念" "$TLB" ""

CA=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"A 错误解释"}')" "['data']['id']")
WRONG='我理解 TLB 就是页表本身，它缓存的是整个页表；所以 TLB miss 就等于缺页中断。'
SSE_A="$OUT/a-wrong.sse"
send_message "$CA" "$WRONG" "$SSE_A"
EVENTS=$(tr -d '\r' < "$SSE_A" | grep '^event:' | sed 's/^event: *//' | tr '\n' ' ')
case "$EVENTS" in "message.start message.delta "*) ok "SSE 正常开始并流式输出";; *) bad "SSE 起始事件异常：$EVENTS";; esac
case "$EVENTS" in *"message.end"*) ok "SSE 正常结束";; *) bad "SSE 未正常结束";; esac
if ! wait_evidence "$TLB" 1 45; then
  bad "45 秒内没有产生第一轮 Evidence"
else
  ok "第一轮异步分析已落库 Evidence"
fi
MSGS_A="$OUT/a-wrong.messages.json"
api GET "/api/conversations/$CA/messages" > "$MSGS_A"
WRONG_MSG=$(jqv "$(cat "$MSGS_A")" "next(m['id'] for m in d['data'] if m['role']=='USER' and 'TLB' in m['content'])")
checknot "错误解释消息已落库" "$WRONG_MSG" "None"

# 用产品自有的批注入口把「用户自己标出的误区」变成 INCORRECT 证据。
HL=$(api POST "/api/messages/$WRONG_MSG/highlights" \
  "$(python -c 'import json; print(json.dumps({"markType":"MISCONCEPTION_CANDIDATE","selectedText":"TLB 就是页表本身，它缓存的是整个页表","note":""}, ensure_ascii=False))')")
HL_ID=$(jqv "$HL" "['data']['id']")
checknot "误区标注创建成功" "$HL_ID" "None"
if wait_status "$TLB" "MISCONCEPTION" 10; then
  ok "当前理解进入 MISCONCEPTION"
else
  bad "当前理解没有进入 MISCONCEPTION"
fi
EV_A="$OUT/a.evidence.json"
api GET "/api/understandings/$TLB/evidence" > "$EV_A"
UN_A="$OUT/a.understanding.json"
api GET "/api/understandings/$TLB" > "$UN_A"
UN_ID=$(jqv "$(cat "$UN_A")" "['data']['id']")
REV_A="$OUT/a.revisions.json"
api GET "/api/understandings/$TLB/revisions" > "$REV_A"
WRONG_PARTIAL=$(jqv "$(cat "$EV_A")" "next(e['id'] for e in d['data'] if e['correctness']=='PARTIAL')")
WRONG_HL_EV=$(jqv "$(cat "$EV_A")" "next(e['id'] for e in d['data'] if e['correctness']=='INCORRECT')")
check "当前误区状态为 ACTIVE" \
  "$(jqv "$(cat "$UN_A")" "d['data']['misconceptions'][0]['status']")" "ACTIVE"
check "当前没有把未验证内容当成已掌握" \
  "$(jqv "$(cat "$UN_A")" "len(d['data']['knownParts'])")" "0"
check "误区证据来源是批注" \
  "$(sql "SELECT source_type FROM evidence_source WHERE evidence_id=$WRONG_HL_EV;")" "HIGHLIGHT"

CB_A=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"A 新会话普通提问"}')" "['data']['id']")
PREVIEW_A1="$OUT/a-plain.preview.json"
preview_context "$USER_A" "$CB_A" "$SPACE_ID" "TLB 缓存的是什么？" "$PREVIEW_A1"
echo "  A 普通提问区块：$(section_names "$PREVIEW_A1")"
check "普通提问注入当前 Understanding" "$(has_section "$PREVIEW_A1" "understanding:")" "yes"
check "普通提问不注入学习轨迹" "$(has_section "$PREVIEW_A1" "trajectory:")" "no"
check "普通提问不注入 Evidence 历史" "$(has_section "$PREVIEW_A1" "evidence:")" "no"
SYS_A1=$(system_text "$PREVIEW_A1")
case "$SYS_A1" in *"状态：存在误区"*) ok "上下文明确标出当前是误区状态";; *) bad "上下文没有标出误区状态";; esac
case "$SYS_A1" in *"当前存在"*"TLB 就是页表本身"*) ok "当前误区原文进入上下文";; *) bad "当前误区原文没有进入上下文";; esac
check "普通提问的轨迹检索为空（未按时间乱取历史）" \
  "$(jqv "$(retrieve_detail "$PREVIEW_A1")" "['trajectoryRevisions']")" "0"
check "普通提问没有把未验证内容当成已掌握" \
  "$(jqv "$(api GET "/api/understandings/$TLB")" "['data']['status']")" "MISCONCEPTION"

CB_A2=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"A 针对误区提问"}')" "['data']['id']")
PREVIEW_A2="$OUT/a-history.preview.json"
preview_context "$USER_A" "$CB_A2" "$SPACE_ID" \
  "TLB 缓存的是什么？我以前好像理解错了，能针对我的误区讲讲吗？" "$PREVIEW_A2"
echo "  A 带历史意图提问区块：$(section_names "$PREVIEW_A2")"
check "带复盘意图时注入学习轨迹" "$(has_section "$PREVIEW_A2" "trajectory:")" "yes"
SYS_A2=$(system_text "$PREVIEW_A2")
case "$SYS_A2" in *"当前仍存在的误区"*) ok "历史轨迹仍把该误区标为当前存在";; *) bad "当前误区被错误地标成历史已纠正";; esac
case "$SYS_A2" in *"[已纠正，历史]"*) bad "尚未纠正的误区被标成已纠正";; *) ok "尚未纠正的误区没有冒充已纠正历史";; esac
TRAJ_EV_A2=$(jqv "$(retrieve_detail "$PREVIEW_A2")" "' '.join(map(str, d['trajectoryEvidenceIds']))")
case " $TRAJ_EV_A2 " in *" $WRONG_HL_EV "*) ok "轨迹通过 Revision 关联找回原始误区证据 #$WRONG_HL_EV";; *) bad "轨迹没有找回原始误区证据 #$WRONG_HL_EV（实际 $TRAJ_EV_A2）";; esac
case " $TRAJ_EV_A2 " in *" $WRONG_PARTIAL "*) ok "轨迹保留了同一轮用户错误解释 #$WRONG_PARTIAL";; *) bad "轨迹缺少用户错误解释 #$WRONG_PARTIAL";; esac

# =====================================================================
section "2. 场景 B：两条独立正确证据 → DEMONSTRATED"
CB=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"B 纠正"}')" "['data']['id']")
CORRECT='我认为我现在明白了：TLB 缓存的是页表项，不是整个页表；TLB miss 之后才需要查页表，缺页是页面不在内存。'
EV_BEFORE_CORR=$(jqv "$(api GET "/api/understandings/$TLB/evidence")" "len(d['data'])")
send_message "$CB" "$CORRECT" "$OUT/b-correct.sse"
if wait_evidence "$TLB" "$((EV_BEFORE_CORR + 1))" 45; then
  ok "纠正解释的消息分析已落库"
else
  bad "纠正解释的消息分析没有在 45 秒内落库"
fi
MSGS_B="$OUT/b-correct.messages.json"
api GET "/api/conversations/$CB/messages" > "$MSGS_B"
CORRECT_MSG=$(jqv "$(cat "$MSGS_B")" "max(m['id'] for m in d['data'] if m['role']=='USER')")
HL2=$(api POST "/api/messages/$CORRECT_MSG/highlights" \
  "$(python -c 'import json; print(json.dumps({"markType":"MY_UNDERSTANDING","selectedText":"TLB 缓存的是页表项，不是整个页表","note":""}, ensure_ascii=False))')")
HL2_ID=$(jqv "$HL2" "['data']['id']")
checknot "正确解释批注创建成功（独立来源 1）" "$HL2_ID" "None"
if wait_status "$TLB" "MISCONCEPTION" 10; then
  UN_C1="$OUT/b.after-first.json"
  api GET "/api/understandings/$TLB" > "$UN_C1"
  check "一条正确证据后误区降为 PENDING" \
    "$(jqv "$(cat "$UN_C1")" "d['data']['misconceptions'][0]['status']")" "PENDING"
else
  bad "第一条正确证据没有把误区推进到 PENDING"
fi

TEST=$(api POST /api/tests \
  "{\"conceptId\":$TLB,\"conversationId\":$CB,\"questionType\":\"SHORT_ANSWER\",\"questionText\":\"TLB 中缓存的是什么？\",\"correctAnswer\":\"页表项\"}")
TEST_ID=$(jqv "$TEST" "['data']['id']")
ANS=$(api POST "/api/tests/$TEST_ID/answer" '{"userAnswer":"TLB 缓存的是页表项，也就是最近用过的页表条目。"}')
TEST_EV=$(jqv "$ANS" "['data']['evidenceId']")
check "测试判分正确（独立来源 2）" "$(jqv "$ANS" "['data']['isCorrect']")" "True"
check "测试证据来源是 TEST_ANSWER" \
  "$(sql "SELECT source_type FROM evidence_source WHERE evidence_id=$TEST_EV;")" "TEST_ANSWER"
if wait_status "$TLB" "DEMONSTRATED" 15; then
  ok "两条独立正确证据后状态变为 DEMONSTRATED"
else
  bad "两条独立正确证据后没有变为 DEMONSTRATED（实际 $(jqv "$(api GET "/api/understandings/$TLB")" "['data']['status']")）"
fi
UN_B="$OUT/b.final.json"
api GET "/api/understandings/$TLB" > "$UN_B"
REV_B="$OUT/b.revisions.json"
api GET "/api/understandings/$TLB/revisions" > "$REV_B"
check "纠正后当前快照没有误区" \
  "$(jqv "$(cat "$UN_B")" "len(d['data']['misconceptions'])")" "0"
checkge "纠正后已理解条目 ≥ 2" \
  "$(jqv "$(cat "$UN_B")" "len(d['data']['knownParts'])")" "2"
check "历史 Revision 仍保留曾经的 ACTIVE 误区" \
  "$(jqv "$(cat "$REV_B")" "any(m['status']=='ACTIVE' for r in d['data'] for m in r['misconceptions'])")" "True"
check "LearningState 与 Understanding 状态一致" \
  "$(jqv "$(api GET "/api/learning-state/concepts/$TLB")" "['data']['status']")" "DEMONSTRATED"
EV_B="$OUT/b.evidence.json"
api GET "/api/understandings/$TLB/evidence" > "$EV_B"
CORRECT_HL_EV=$(jqv "$(cat "$EV_B")" "next(e['id'] for e in d['data'] if e['correctness']=='CORRECT' and any(s['sourceType']=='HIGHLIGHT' for s in e['sources']))")
CORRECT_TEST_EV=$(jqv "$(cat "$EV_B")" "next(e['id'] for e in d['data'] if e['correctness']=='CORRECT' and any(s['sourceType']=='TEST_ANSWER' for s in e['sources']))")
checknot "两条正确证据来源不同（批注 / 测试回答）" "$CORRECT_HL_EV" "$CORRECT_TEST_EV"

CB2=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"B 纠正后的普通提问"}')" "['data']['id']")
PREVIEW_B="$OUT/b-plain.preview.json"
preview_context "$USER_A" "$CB2" "$SPACE_ID" "TLB 缓存的是什么？" "$PREVIEW_B"
echo "  B 普通提问区块：$(section_names "$PREVIEW_B")"
check "纠正后的普通问题仍注入当前 Understanding" "$(has_section "$PREVIEW_B" "understanding:")" "yes"
check "纠正后的普通问题不注入学习轨迹" "$(has_section "$PREVIEW_B" "trajectory:")" "no"
check "纠正后的普通问题不注入全部历史证据" "$(has_section "$PREVIEW_B" "evidence:")" "no"
SYS_B=$(system_text "$PREVIEW_B")
case "$SYS_B" in *"状态：已能讲清"*) ok "普通问题看到的是当前已掌握状态";; *) bad "普通问题没有看到当前状态";; esac
case "$SYS_B" in *"TLB 就是页表本身"*) bad "普通问题仍把已纠正的历史误区注入当前上下文";; *) ok "普通问题没有把历史误区当成当前 ACTIVE";; esac
check "普通问题没有触发 Revision 读取" \
  "$(jqv "$(retrieve_detail "$PREVIEW_B")" "['trajectoryRevisions']")" "0"

# =====================================================================
section "3. 场景 C：显式复盘走正式读取路径"
CC=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"C 显式复盘"}')" "['data']['id']")
Q_C='我之前对 TLB 的理解错在哪里？后来是怎么纠正的？'
PREVIEW_C="$OUT/c-review.preview.json"
preview_context "$USER_A" "$CC" "$SPACE_ID" "$Q_C" "$PREVIEW_C"
SYS_C=$(system_text "$PREVIEW_C")
RETRIEVE_C=$(retrieve_detail "$PREVIEW_C")
echo "  C 复盘区块：$(section_names "$PREVIEW_C")"
echo "  C 轨迹证据 ids：$(jqv "$RETRIEVE_C" "d['trajectoryEvidenceIds']")"
check "复盘注入学习轨迹" "$(has_section "$PREVIEW_C" "trajectory:")" "yes"
check "复盘不再重复注入通用 Evidence 区块" "$(has_section "$PREVIEW_C" "evidence:")" "no"
check "复盘走 HTTP 检索源（不是 fixture）" \
  "$(jqv "$(cat "$PREVIEW_C")" "d['data']['retriever'].startswith('http')")" "True"
checkge "复盘至少读取 3 个关键 Revision" \
  "$(jqv "$RETRIEVE_C" "['trajectoryRevisions']")" "3"
check "轨迹包含原始错误证据 #$WRONG_PARTIAL" \
  "$(jqv "$RETRIEVE_C" "$WRONG_PARTIAL in d['trajectoryEvidenceIds']")" "True"
check "轨迹包含显式误区标注 #$WRONG_HL_EV" \
  "$(jqv "$RETRIEVE_C" "$WRONG_HL_EV in d['trajectoryEvidenceIds']")" "True"
check "轨迹包含纠正证据 #$CORRECT_HL_EV" \
  "$(jqv "$RETRIEVE_C" "$CORRECT_HL_EV in d['trajectoryEvidenceIds']")" "True"
check "轨迹包含纠正证据 #$CORRECT_TEST_EV" \
  "$(jqv "$RETRIEVE_C" "$CORRECT_TEST_EV in d['trajectoryEvidenceIds']")" "True"
# 逐条确认这些证据能通过正式详情接口读到，且原文出现在 Context 中。
FORMAL_OK=1
for eid in $(jqv "$RETRIEVE_C" "' '.join(map(str, d['trajectoryEvidenceIds']))"); do
  DETAIL="$OUT/c.evidence-$eid.json"
  api GET "/api/evidence/$eid" > "$DETAIL"
  if [ "$(code)" != "200" ]; then FORMAL_OK=0; echo "    证据 #$eid 详情接口返回 $(code)"; continue; fi
  OBS=$(jf "$DETAIL" "d['data']['observation']")
  case "$SYS_C" in *"$OBS"*) : ;; *) FORMAL_OK=0; echo "    证据 #$eid 的原文没有进入 Context";; esac
done
check "复盘中的每条历史证据都能通过 GET /api/evidence/{id} 读到并进入 Context" "$FORMAL_OK" "1"

case "$SYS_C" in *"学习轨迹（历史）"*) ok "上下文明确区分学习轨迹（历史）";; *) bad "上下文缺少学习轨迹区块";; esac
case "$SYS_C" in *"当前状态（来自持久化快照）：已能讲清"*) ok "轨迹带出当前持久化状态（已能讲清）";; *) bad "轨迹没有带出当前状态";; esac
case "$SYS_C" in *"[已纠正，历史]"*"TLB 就是页表本身"*) ok "历史误区带「已纠正，历史」标注与原时间";; *) bad "历史误区没有标成已纠正历史";; esac
case "$SYS_C" in *"当时 ACTIVE"*) ok "Revision 保留了误区当时的 ACTIVE 状态";; *) bad "Revision 没有保留当时状态";; esac
case "$SYS_C" in *"后续正确证据（2 条）"*) ok "轨迹列出两条后续正确证据";; *) bad "轨迹没有列出两条后续正确证据";; esac
case "$SYS_C" in *"来源：批注"*) ok "原始误区证据带显式批注来源";; *) bad "原始误区证据缺少显式批注来源";; esac
case "$SYS_C" in *"来源：测试回答"*) ok "纠正证据带测试回答来源";; *) bad "纠正证据缺少测试回答来源";; esac
# 5.3.1 起用户可见上下文只用可读来源类型，不再泄露数据库证据 / 来源 id。
case "$SYS_C" in
  *"来源：批注#"*|*"来源：测试回答#"*|*"#$WRONG_HL_EV"*|*"#$CORRECT_TEST_EV"*)
    bad "上下文泄露内部证据 / 来源 id";;
  *) ok "上下文没有泄露内部证据 / 来源 id";;
esac
case "$SYS_C" in *"不是整个页表"*) ok "当前正确理解进入上下文";; *) bad "当前正确理解没有进入上下文";; esac
case "$SYS_C" in *"当前状态以「学习者的当前认知」区块为准，历史不能覆盖它"*) ok "上下文声明历史不得覆盖当前状态";; *) bad "上下文缺少「历史不得覆盖当前」的约束";; esac

# 时间/状态关系：ACTIVE 版本在 DEMONSTRATED 之前；错误证据在纠正证据之前。
if python - "$PREVIEW_C" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))["data"]
text = d["context"]["systemInstruction"].splitlines()
def idx(pred, label):
    for i, line in enumerate(text):
        if pred(line):
            return i
    raise SystemExit(f"missing: {label}")
active = idx(lambda l: "当时 ACTIVE" in l, "当时 ACTIVE")
demo = idx(lambda l: "版本" in l and "状态：已能讲清" in l, "DEMONSTRATED 版本")
old = idx(lambda l: "答错" in l and "来源：批注" in l, "错误证据（批注）")
new1 = idx(lambda l: "答对" in l and "来源：批注" in l, "纠正证据（批注）")
new2 = idx(lambda l: "答对" in l and "来源：测试回答" in l, "纠正证据（测试回答）")
assert active < demo, (active, demo)
assert old < new1 and old < new2, (old, new1, new2)
PY
then ok "轨迹中的时间/状态顺序正确（错误 → 纠正 → 当前）"; else bad "轨迹中的时间/状态顺序不正确"; fi
check "复盘不改变当前状态" \
  "$(jqv "$(api GET "/api/understandings/$TLB")" "['data']['status']")" "DEMONSTRATED"

# =====================================================================
section "4. 场景 D：旧 Memory 与新 Evidence 冲突"
MEM=$(api POST /api/memories \
  "{\"learningSpaceId\":$SPACE_ID,\"conceptId\":$TLB,\"memoryType\":\"LEARNING\",\"content\":\"用户认为 TLB 缓存整个页表\"}")
MEM_ID=$(jqv "$MEM" "['data']['id']")
checknot "创建一条与当前理解冲突的 ACTIVE Memory" "$MEM_ID" "None"
PREVIEW_D="$OUT/d-memory.preview.json"
preview_context "$USER_A" "$CC" "$SPACE_ID" "$Q_C" "$PREVIEW_D"
SYS_D=$(system_text "$PREVIEW_D")
MEM_STATUS=$(sql "SELECT status FROM memory WHERE id=$MEM_ID;")
MEM_CONTENT=$(sql "SELECT content FROM memory WHERE id=$MEM_ID;")
check "冲突 Memory 未被静默删除" "$MEM_STATUS" "ACTIVE"
check "冲突 Memory 内容未被改写" "$MEM_CONTENT" "用户认为 TLB 缓存整个页表"
check "冲突 Memory 仍可通过正式检索读到" \
  "$(jqv "$(api GET "/api/memories/search?spaceId=$SPACE_ID&conceptId=$TLB&keyword=TLB")" "len([m for m in d['data'] if m['id']==$MEM_ID])")" "1"
check "上下文注入 Memory 区块" "$(has_section "$PREVIEW_D" "memory:")" "yes"
case "$SYS_D" in *"记录于 "*) ok "Memory 带记录时间";; *) bad "Memory 缺少记录时间";; esac
case "$SYS_D" in *"用户认为 TLB 缓存整个页表"*) ok "冲突 Memory 原文被保留在上下文";; *) bad "冲突 Memory 原文没有进入上下文";; esac
case "$SYS_D" in *"不要删除或改写长期记忆"*) ok "上下文明确要求保留 Memory 来源";; *) bad "上下文没有 Memory 冲突处理约束";; esac
python - "$PREVIEW_D" "$MEM_ID" <<'PY' && ok "旧 Memory 没有被当成当前理解" || bad "旧 Memory 被写进了当前 Understanding"
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))["data"]["context"]
mem_id = sys.argv[2]
memory = [s for s in d["sections"] if s["name"] == f"memory:{mem_id}"]
understanding = [s for s in d["sections"] if s["name"].startswith("understanding:")]
assert memory, "memory section missing"
assert "用户认为 TLB 缓存整个页表" in memory[0]["preview"]
assert understanding, "understanding section missing"
assert all("用户认为 TLB 缓存整个页表" not in s["preview"] for s in understanding)
PY
# 新证据不是全局真理：同一用户的另一个空间、另一用户都读不到这次纠正。
SPACE2=$(jqv "$(api POST /api/spaces "{\"name\":\"阶段5.2隔离空间 $STAMP\"}")" "['data']['id']")
TLB2=$(jqv "$(api POST "/api/spaces/$SPACE2/concepts" \
  '{"canonicalName":"TLB","displayName":"TLB","description":"另一个空间里的同名概念"}')" "['data']['id']")
CB2=$(jqv "$(api POST "/api/spaces/$SPACE2/conversations" '{"title":"隔离空间提问"}')" "['data']['id']")
PREVIEW_SPACE2="$OUT/d-space2.preview.json"
preview_context "$USER_A" "$CB2" "$SPACE2" "TLB 缓存的是什么？" "$PREVIEW_SPACE2"
check "新证据不跨空间（另一空间不出现 Understanding）" "$(has_section "$PREVIEW_SPACE2" "understanding:")" "no"
check "新证据不跨空间（另一空间不出现 Memory）" "$(has_section "$PREVIEW_SPACE2" "memory:")" "no"
check "新证据不跨空间（另一空间不出现轨迹）" "$(has_section "$PREVIEW_SPACE2" "trajectory:")" "no"

# =====================================================================
section "5. 场景 E：权限隔离与无关问题"
TOKEN_SAVE="$TOKEN"; TOKEN="$TOKEN_B"
check "用户 B 读 A 的 Understanding 返回 404" "$(api GET "/api/understandings/$TLB" > /dev/null; code)" "404"
check "用户 B 的 Evidence 列表为空（不泄露 A 的证据）" \
  "$(jqv "$(api GET "/api/understandings/$TLB/evidence")" "len(d['data'])")" "0"
check "用户 B 用 A 的 evidenceId 读详情返回 404" \
  "$(api GET "/api/evidence/$WRONG_HL_EV" > /dev/null; code)" "404"
check "用户 B 读 A 的 LearningState 返回 404" \
  "$(api GET "/api/learning-state/concepts/$TLB" > /dev/null; code)" "404"
B_SPACE=$(jqv "$(api POST /api/spaces "{\"name\":\"B 的空间 $STAMP\"}")" "['data']['id']")
B_TLB=$(jqv "$(api POST "/api/spaces/$B_SPACE/concepts" \
  '{"canonicalName":"TLB","displayName":"TLB","description":"B 自己的 TLB 概念"}')" "['data']['id']")
B_CONV=$(jqv "$(api POST "/api/spaces/$B_SPACE/conversations" '{"title":"B 普通提问"}')" "['data']['id']")
PREVIEW_B_USER="$OUT/e-user-b.preview.json"
preview_context "$USER_B" "$B_CONV" "$B_SPACE" "TLB 缓存的是什么？" "$PREVIEW_B_USER"
echo "  E 用户 B 提问区块：$(section_names "$PREVIEW_B_USER")"
check "用户 B 看不到 A 的 Understanding" "$(has_section "$PREVIEW_B_USER" "understanding:")" "no"
check "用户 B 看不到 A 的历史轨迹" "$(has_section "$PREVIEW_B_USER" "trajectory:")" "no"
check "用户 B 看不到 A 的 Memory" "$(has_section "$PREVIEW_B_USER" "memory:")" "no"
check "用户 B 看不到 A 的 Evidence" "$(has_section "$PREVIEW_B_USER" "evidence:")" "no"
TOKEN="$TOKEN_SAVE"

REV_BEFORE_E=$(jqv "$(api GET "/api/understandings/$TLB/revisions")" "len(d['data'])")
EV_BEFORE_E=$(jqv "$(api GET "/api/understandings/$TLB/evidence")" "len(d['data'])")
for q in "谢谢你的解释，辛苦了！" "今天天气怎么样？"; do
  CONV=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"无关问题"}')" "['data']['id']")
  P="$OUT/e-unrelated-$(printf '%s' "$q" | md5sum | cut -c1-6).preview.json"
  preview_context "$USER_A" "$CONV" "$SPACE_ID" "$q" "$P"
  echo "  无关问题「$q」区块：$(section_names "$P")"
  check "无关问题「$q」不注入学习轨迹" "$(has_section "$P" "trajectory:")" "no"
  check "无关问题「$q」不注入历史证据" "$(has_section "$P" "evidence:")" "no"
  check "无关问题「$q」不注入长期记忆" "$(has_section "$P" "memory:")" "no"
  check "无关问题「$q」不触发 Revision 读取" \
    "$(jqv "$(retrieve_detail "$P")" "['trajectoryRevisions']")" "0"
done
check "无关问题不新增该概念的 Evidence" \
  "$(jqv "$(api GET "/api/understandings/$TLB/evidence")" "len(d['data'])")" "$EV_BEFORE_E"
check "无关问题不新增 Revision" \
  "$(jqv "$(api GET "/api/understandings/$TLB/revisions")" "len(d['data'])")" "$REV_BEFORE_E"
check "无关问题不改变当前掌握状态" \
  "$(jqv "$(api GET "/api/understandings/$TLB")" "['data']['status']")" "DEMONSTRATED"

# =====================================================================
section "6. 数据库事实核对（只读）"
echo "  -- 当前快照 / Revision / LearningState --"
sql "SELECT id, status, current_revision_id, LEFT(known_parts,120), uncertain_parts \
  FROM learning_understanding WHERE user_id=$USER_A AND concept_id=$TLB;"
sql "SELECT revision_no, status, trigger_evidence_id, LEFT(COALESCE(misconceptions,'[]'),80) \
  FROM understanding_revision WHERE understanding_id=$UN_ID ORDER BY revision_no;"
sql "SELECT concept_id, status, last_evidence_at, review_priority \
  FROM learning_state WHERE user_id=$USER_A AND concept_id=$TLB;"
echo "  -- Evidence 来源链（append-only）--"
sql "SELECT e.id, e.evidence_type, e.correctness, e.created_at, es.source_type, es.source_id \
  FROM learning_evidence e LEFT JOIN evidence_source es ON es.evidence_id=e.id \
  WHERE e.user_id=$USER_A AND e.concept_id=$TLB ORDER BY e.id;"
echo "  -- Memory --"
sql "SELECT id, status, memory_type, scope_type, LEFT(content,40), created_at, updated_at \
  FROM memory WHERE id=$MEM_ID;"

printf '\n\033[1m结果：PASS=%d FAIL=%d SKIP=%d\033[0m\n' "$PASS" "$FAIL" "$SKIP"
printf '产物目录：%s\n' "$OUT"
echo "用户 A：$UA (id=$USER_A)  space=$SPACE_ID  TLB=$TLB  PageTable=$PT"
echo "用户 B：$UB (id=$USER_B)  space=$B_SPACE  TLB=$B_TLB"
echo "关键证据：错误解释=#$WRONG_PARTIAL  误区批注=#$WRONG_HL_EV  纠正批注=#$CORRECT_HL_EV  测试=#$CORRECT_TEST_EV"
echo "Memory：$MEM_ID"
echo
if [ "$FAIL" -gt 0 ]; then
  echo "存在失败项，先看上面的 FAIL 行与 $OUT 下的响应原文。"
  exit 1
fi
echo "全部通过。注意：本脚本使用 Mock LLM，只能证明检索路径、上下文注入与持久化状态，"
echo "不能证明真实模型的语义判断或教学效果。"
