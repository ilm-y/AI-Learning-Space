#!/usr/bin/env bash
# 阶段 6：TLB 学习状态闭环演示（Mock LLM，稳定可重复）。
#
# 这不是产品功能，而是把现有公开 API 串成一条可复现的演示：
#   1. 暴露错误理解 → 2. 记录 Evidence + 来源 → 3. Understanding / Revision 更新
#   → 4. 纠正后 status 变化 → 5. 新会话读取当前理解
#   → 6. 显式复盘读取历史错误 / 纠正证据 → 7. 区分历史误区与当前掌握
#
# 前置：
#   - MySQL 可连（凭证从 application-local.yml 读取）；
#   - 后端 8080 已启动，ails.agent.gateway=http；
#   - Agent 8000 已启动，AILS_LLM_PROVIDER=mock、AILS_RETRIEVER_MODE=auto。
#
# 用法：
#   bash scripts/phase6-demo.sh
#
# 数据隔离：每次注册新用户 + 新空间，不删除、不手工修改任何业务数据。
# 直接 SQL 只用于只读核对。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_SRC="$ROOT/backend/backend/src/main/resources"
BASE="${BASE:-http://127.0.0.1:8080}"
AGENT="${AGENT:-http://127.0.0.1:8000}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$ROOT/backend/backend/target/phase6-demo/$STAMP"
mkdir -p "$OUT"

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$1"; }
check(){ if [ "$2" = "$3" ]; then ok "$1（= $3）"; else bad "$1：实际 [$2]，期望 [$3]"; fi; }
section(){ printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

read_creds() {
  python - "$BACKEND_SRC" <<'PY'
import os, re, sys
base = sys.argv[1]
def find(key):
    for name in ("application-local.yml", "application.yml"):
        path = os.path.join(base, name)
        if not os.path.exists(path):
            continue
        text = open(path, encoding="utf-8").read()
        m = re.search(r'^\s*%s:\s*(.+?)\s*(?:#.*)?$' % key, text, re.M)
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

jqv(){ printf '%s' "$1" | python -c "
import sys, json
d = json.load(sys.stdin)
expr = sys.argv[1]
print(eval(('d' + expr) if expr.startswith('[') else expr))
" "$2" 2>/dev/null; }

# 读 JSON 文件：路径作为 argv 传入，Git Bash 会转换成 Windows 路径。
# 不要把 /d/... 之类的 POSIX 路径写进 python -c 的代码字符串里。
jf(){ python - "$1" "$2" <<'PY'
import json, re, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(eval(sys.argv[2]))
PY
}

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

preview_context() { # preview_context <userId> <conversationId> <spaceId> <question> <outfile>
  local uid="$1" cid="$2" sid="$3" q="$4" out="$5"
  python - "$uid" "$cid" "$sid" "$q" "$TOKEN" > "$out.payload" <<'PY'
import json, sys
uid, cid, sid, question, token = sys.argv[1:6]
print(json.dumps({
    "runId": 960000,
    "userId": int(uid),
    "conversationId": int(cid),
    "learningSpaceId": int(sid),
    "userMessage": question,
    "authToken": token,
}, ensure_ascii=False))
PY
  curl -s -m 90 -X POST "$AGENT/agent/context/preview" \
    -H 'Content-Type: application/json' -d @"$out.payload" > "$out"
}

send_message() { # send_message <conversationId> <text> <sse-file>
  curl -sN -m 90 -X POST "$BASE/api/conversations/$1/messages" \
    -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
    -d "$(python -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}, ensure_ascii=False))' "$2")" \
    > "$3"
}

section_names(){ jqv "$(cat "$1")" "','.join(s['name'] for s in d['data']['context']['sections'])"; }
has_section(){ jqv "$(cat "$1")" "any(s['name'].startswith('$2') for s in d['data']['context']['sections'])"; }

# =====================================================================
section "0. 前置检查"
AHEALTH=$(curl -s -m 3 "$AGENT/health")
PROVIDER=$(printf '%s' "$AHEALTH" | python -c 'import sys,json;print(json.load(sys.stdin).get("llm_provider"))' 2>/dev/null)
if [ "$PROVIDER" != "mock" ]; then
  echo "演示要求 Agent 处于 mock 模式（当前 provider=[$PROVIDER]）。" >&2
  echo "真实模型输出不稳定，不能作为可重复演示。" >&2
  exit 1
fi
if ! curl -s -m 3 -o /dev/null "$BASE/api/auth/login"; then
  echo "后端不可用：$BASE" >&2
  exit 1
fi
if ! sql "SELECT 1" > /dev/null; then
  echo "MySQL 连接失败（读取 application-local.yml 的凭证）" >&2
  exit 1
fi
ok "Agent=$AGENT provider=mock，后端=$BASE，MySQL 可读"

# =====================================================================
section "1. 准备干净测试数据（新用户 / 新空间 / TLB / Page Table）"
SETUP_OUT="$OUT/setup"
OUT="$SETUP_OUT" bash "$ROOT/scripts/phase53-setup-clean.sh" > "$OUT/setup.log" 2>&1
SETUP_EXIT=$?
SUMMARY="$SETUP_OUT/summary-clean.json"
if [ "$SETUP_EXIT" -eq 0 ] && [ -f "$SUMMARY" ]; then
  ok "干净数据集准备完成（$SUMMARY）"
else
  bad "数据准备失败，见 $OUT/setup.log"
  tail -30 "$OUT/setup.log" >&2
  exit 1
fi

USER_NAME=$(python - "$SUMMARY" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["user_c_name"])
PY
)
USER_ID=$(python - "$SUMMARY" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["user_c_id"])
PY
)
SPACE_ID=$(python - "$SUMMARY" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["space_c_id"])
PY
)
TLB=$(python - "$SUMMARY" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["tlb_c"])
PY
)
UN_ID=$(python - "$SUMMARY" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["understanding_c_id"])
PY
)

LOGIN=$(curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"password\":\"Passw0rd!23\"}")
TOKEN=$(jqv "$LOGIN" "['data']['token']")
if [ -n "$TOKEN" ] && [ "$TOKEN" != "None" ]; then ok "登录成功（user=$USER_ID space=$SPACE_ID TLB=$TLB）"; else bad "登录失败"; exit 1; fi

# =====================================================================
section "2. Evidence / Understanding / Revision 已落库"
api GET "/api/understandings/$TLB" > "$OUT/understanding.json"
api GET "/api/understandings/$TLB/evidence" > "$OUT/evidence.json"
api GET "/api/understandings/$TLB/revisions" > "$OUT/revisions.json"
api GET "/api/learning-state/concepts/$TLB" > "$OUT/learning-state.json"

python - "$OUT/evidence.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))["data"]
for e in d:
    srcs = ", ".join(f"{s['sourceType']}#{s['sourceId']}" for s in e.get("sources", []))
    print(f"  Evidence #{e['id']} [{e['evidenceType']}/{e['correctness']}] 来源：{srcs} ｜ {e['observation'][:60]}")
PY
python - "$OUT/revisions.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))["data"]
for r in d:
    ms = ", ".join(f"{m['text'][:30]}({m['status']})" for m in r.get("misconceptions", []))
    print(f"  Revision #{r['revisionNo']} status={r['status']} 误区：[{ms}]")
PY

check "当前状态 = DEMONSTRATED" "$(jqv "$(cat "$OUT/understanding.json")" "['data']['status']")" "DEMONSTRATED"
check "当前快照不再包含误区" "$(jqv "$(cat "$OUT/understanding.json")" "len(d['data']['misconceptions'])")" "0"
check "历史 Revision 仍保留 ACTIVE 误区" \
  "$(jqv "$(cat "$OUT/revisions.json")" "any(m['status']=='ACTIVE' for r in d['data'] for m in r['misconceptions'])")" "True"
check "LearningState 同步 DEMONSTRATED" \
  "$(jqv "$(cat "$OUT/learning-state.json")" "['data']['status']")" "DEMONSTRATED"
check "存在原始错误证据（INCORRECT）" \
  "$(jqv "$(cat "$OUT/evidence.json")" "any(e['correctness']=='INCORRECT' for e in d['data'])")" "True"
check "存在纠正后的正确证据（CORRECT）" \
  "$(jqv "$(cat "$OUT/evidence.json")" "any(e['correctness']=='CORRECT' for e in d['data'])")" "True"
check "错误证据来源包含显式批注（HIGHLIGHT）" \
  "$(jqv "$(cat "$OUT/evidence.json")" "any(e['correctness']=='INCORRECT' and any(s['sourceType']=='HIGHLIGHT' for s in e['sources']) for e in d['data'])")" "True"

# =====================================================================
section "3. 新会话：读取当前理解，不注入历史轨迹"
NEW_C=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"阶段6 演示：新会话"}')" "['data']['id']")
NORMAL_Q="TLB 缓存的是什么？"
preview_context "$USER_ID" "$NEW_C" "$SPACE_ID" "$NORMAL_Q" "$OUT/normal.preview.json"
echo "  普通问题区块：$(section_names "$OUT/normal.preview.json")"
check "普通问题注入当前 Understanding" "$(has_section "$OUT/normal.preview.json" "understanding:")" "True"
check "普通问题不注入历史轨迹" "$(has_section "$OUT/normal.preview.json" "trajectory:")" "False"
check "上下文中的当前状态是「已能讲清」" \
  "$(jf "$OUT/normal.preview.json" "'状态：已能讲清' in d['data']['context']['systemInstruction']")" "True"

# =====================================================================
section "4. 显式复盘：读取历史错误 / 纠正证据"
REVIEW_C=$(jqv "$(api POST "/api/spaces/$SPACE_ID/conversations" '{"title":"阶段6 演示：显式复盘"}')" "['data']['id']")
REVIEW_Q="我之前对 TLB 的理解错在哪里？后来是怎么纠正的？"
preview_context "$USER_ID" "$REVIEW_C" "$SPACE_ID" "$REVIEW_Q" "$OUT/review.preview.json"
echo "  复盘区块：$(section_names "$OUT/review.preview.json")"
check "复盘注入学习轨迹" "$(has_section "$OUT/review.preview.json" "trajectory:")" "True"
check "复盘轨迹关联到 Revision 历史" \
  "$(jf "$OUT/review.preview.json" "any(any(src.startswith('revision:') for src in s['sourceIds']) for s in d['data']['context']['sections'] if s['name'].startswith('trajectory:'))")" "True"

# =====================================================================
section "5. 生产 SSE：复盘回答的事件契约"
send_message "$REVIEW_C" "$REVIEW_Q" "$OUT/review.sse"
python - "$OUT/review.sse" > "$OUT/review.sse.json" <<'PY'
import json, sys
events, text, errors = [], [], []
current = None
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.rstrip("\n")
    if line.startswith("event:"):
        current = line[6:].strip(); events.append(current)
    elif line.startswith("data:"):
        try: payload = json.loads(line[5:])
        except json.JSONDecodeError: continue
        if current == "message.delta": text.append(payload.get("delta", ""))
        elif current == "message.error": errors.append(payload)
print(json.dumps({"events": events, "answer": "".join(text), "errors": errors}, ensure_ascii=False))
PY
python - "$OUT/review.sse.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"  事件数：{len(d['events'])}，首事件：{d['events'][0] if d['events'] else 'NONE'}，"
      f"末事件：{d['events'][-1] if d['events'] else 'NONE'}，回答 {len(d['answer'])} 字")
print("  回答节选：")
print("  " + d["answer"][:300].replace("\n", "\n  "))
PY
check "首事件是 message.start" "$(jf "$OUT/review.sse.json" "d['events'][0]")" "message.start"
check "末事件是 message.end" "$(jf "$OUT/review.sse.json" "d['events'][-1]")" "message.end"
check "没有 message.error" "$(jf "$OUT/review.sse.json" "len(d['errors'])")" "0"
check "回答非空" "$(jf "$OUT/review.sse.json" "len(d['answer'])>0")" "True"
check "回答不泄露内部 Evidence / 来源 ID" \
  "$(jf "$OUT/review.sse.json" "bool(re.search(r'#[0-9]+', d['answer']))")" "False"

# =====================================================================
section "6. 数据库只读核对（不是手工写入）"
DB_STATE=$(sql "SELECT status FROM learning_state WHERE user_id=$USER_ID AND concept_id=$TLB LIMIT 1;")
DB_EV=$(sql "SELECT COUNT(*) FROM learning_evidence WHERE user_id=$USER_ID AND concept_id=$TLB;")
DB_REV=$(sql "SELECT COUNT(*) FROM understanding_revision WHERE understanding_id=$UN_ID;")
DB_ACTIVE=$(sql "SELECT COUNT(*) FROM learning_understanding WHERE id=$UN_ID AND JSON_SEARCH(misconceptions, 'one', 'ACTIVE') IS NOT NULL;")
echo "  learning_state.status=$DB_STATE"
echo "  learning_evidence 行数=$DB_EV"
echo "  understanding_revision 行数=$DB_REV"
echo "  learning_understanding 中仍为 ACTIVE 的误区行数=$DB_ACTIVE"
check "数据库中的 LearningState 状态与 API 一致" "$DB_STATE" "DEMONSTRATED"
check "数据库中的当前快照没有 ACTIVE 误区" "$DB_ACTIVE" "0"
check "Evidence 至少 4 条（原始 / 批注 / 纠正 / 测试）" \
  "$([ "${DB_EV:-0}" -ge 4 ] && echo True || echo False)" "True"

# =====================================================================
section "7. 演示结论"
cat <<'TXT'
  历史误区：原始错误解释 → 显式批注为 MISCONCEPTION_CANDIDATE → 当前快照中已移出，
            但 Revision 历史仍保留 ACTIVE 快照。
  当前掌握：两条独立正确证据（纠正批注 + 测试回答）→ DEMONSTRATED；
            LearningState 只是该状态的物化投影。
  新会话：  普通问题只看到当前 Understanding，不注入完整历史轨迹。
  显式复盘：才读取 Revision 轨迹与关联 Evidence；回答受 history_guard 约束，
            不得编造未记录的心理过程或外部资料使用。
TXT
printf '\n\033[1m结果：PASS=%d FAIL=%d\033[0m\n产物目录：%s\n' "$PASS" "$FAIL" "$OUT"
[ "$FAIL" -eq 0 ] || exit 1
