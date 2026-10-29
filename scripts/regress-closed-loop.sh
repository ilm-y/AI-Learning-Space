#!/usr/bin/env bash
# 闭环回归：Phase B 固定回归顺序的第 5~8 步 + Session 分项验证点。
#
# 用法：先在另一个终端起好后端（gateway=http）与 Python Agent，然后
#   bash scripts/regress-closed-loop.sh
#
# 这个脚本会真的注册一个用户、建空间和概念、发消息、打批注、查库，
# 跑完 DROP 不掉任何东西 —— 它只增不删，用一个时间戳用户名隔离每次运行。

set -uo pipefail

B=${B:-http://127.0.0.1:8080}
AGENT=${AGENT:-http://127.0.0.1:8000}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
YML="$ROOT/backend/backend/src/main/resources/application-local.yml"

TOKEN=""
U="reg$(date +%s)"
PASS=0
FAIL=0

ok()   { echo "  PASS  $1"; PASS=$((PASS + 1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL + 1)); }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1（期望 [$3]，实际 [$2]）"; fi; }

api() { curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' "$@"; }

jq_() { python -c "import sys,json;d=json.load(sys.stdin);$1"; }

mint_identity() { # mint_identity <userId>
  ( cd "$ROOT/agent" && python -m app.internal_identity --user "$1" )
}

# 从 gitignore 的 application-local.yml 里读密码，不在脚本里硬编码
q() {
  local pwd_
  pwd_=$(sed -n 's/^ *password: *"\(.*\)"/\1/p' "$YML" | head -1)
  MYSQL_PWD="$pwd_" mysql -uroot ails -N -e "$1" 2>/dev/null
}

step() { echo; echo "=== $* ==="; }

# ---------------------------------------------------------------- 0. 前置

step "0. 前置检查"
if [ -z "${AILS_INTERNAL_TOKEN:-}" ] || [ -z "${AILS_INTERNAL_IDENTITY_SECRET:-}" ]; then
  echo "  需要 AILS_INTERNAL_TOKEN（诊断）与 AILS_INTERNAL_IDENTITY_SECRET（preview 身份签名）"; exit 1
fi
curl -s -m 3 -H "X-Internal-Token: $AILS_INTERNAL_TOKEN" \
  "$AGENT/health/diagnostics" | jq_ "print('  agent:', d.get('llm_provider'), '| retriever:', d.get('retriever_mode'), '| backend:', d.get('backend_base_url'))" \
  || { echo "  Python Agent 没起或诊断不可用（$AGENT/health/diagnostics）"; exit 1; }
curl -s "$B/actuator/health" > /dev/null 2>&1 || true

curl -s -X POST "$B/api/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$U\",\"password\":\"pass123456\",\"email\":\"$U@t.com\"}" > /dev/null
TOKEN=$(curl -s -X POST "$B/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$U\",\"password\":\"pass123456\"}" | jq_ "print(d['data']['token'])")
[ -n "$TOKEN" ] && ok "注册并登录 $U" || { bad "登录失败，后面全部跳过"; exit 1; }

# ---------------------------------------------------------------- 1. 空间与概念

step "1. 建空间 + 概念（没有 Concept，检索会整体跳过）"
SID=$(api -X POST "$B/api/spaces" -d '{"name":"操作系统","description":"OS"}' | jq_ "print(d['data']['id'])")
TLB=$(api -X POST "$B/api/spaces/$SID/concepts" \
  -d '{"canonicalName":"tlb","displayName":"TLB","description":"页表项的高速缓存"}' | jq_ "print(d['data']['id'])")
PT=$(api -X POST "$B/api/spaces/$SID/concepts" \
  -d '{"canonicalName":"page table","displayName":"Page Table","description":"页表"}' | jq_ "print(d['data']['id'])")
echo "  spaceId=$SID  TLB=$TLB  PageTable=$PT"
[ -n "$SID" ] && [ -n "$TLB" ] && ok "space / concept 建好了" || bad "space 或 concept 没建出来"

# ---------------------------------------------------------------- 2. 跨对话验收 1~2 步

step "2. Conversation A：用户自己解释 TLB（验收标准第 1 步）"
CA=$(api -X POST "$B/api/spaces/$SID/conversations" -d '{}' | jq_ "print(d['data']['id'])")
CA_SESS_BEFORE=$(api "$B/api/conversations/$CA" | jq_ "print(d['data']['sessionId'])")
check "新对话的 sessionId 应为 NULL" "$CA_SESS_BEFORE" "None"

# SSE 落盘，顺便就是第 8 步要查的事件序列
curl -sN -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -X POST "$B/api/conversations/$CA/messages" \
  -d '{"content":"我理解 TLB 就是 Page Table 的缓存"}' > /tmp/reg_sse_a.txt

MA=$(api "$B/api/conversations/$CA/messages" | jq_ "
print(next(m['id'] for m in d['data'] if m['role']=='USER'))")
echo "  conversationA=$CA  userMessageId=$MA"

CA_SESS_AFTER=$(api "$B/api/conversations/$CA" | jq_ "print(d['data']['sessionId'])")
[ "$CA_SESS_AFTER" != "None" ] && ok "发过含 Concept 的消息后 sessionId 被填上（=$CA_SESS_AFTER）" \
  || bad "sessionId 仍是 NULL —— 消息里的 Concept 没被识别，或 Resolver 没跑"
api "$B/api/sessions/$CA_SESS_AFTER" | jq_ "
s=d['data']; print('  session:', s['id'], '|', s['title'], '|', s['goal'], '|', s['status'])"

step "3. 打 MY_UNDERSTANDING 批注 —— 只有批注才能填满 Understanding 快照"
api -X POST "$B/api/messages/$MA/highlights" -H 'Content-Type: application/json' \
  -d '{"markType":"MY_UNDERSTANDING","selectedText":"我理解 TLB 就是 Page Table 的缓存","note":"我自己琢磨出来的"}' \
  | jq_ "print('  highlight:', d['data']['id'], d['data']['markType'])"

step "4. 验收标准第 2 步：GET /api/understandings/{conceptId}"
api "$B/api/understandings/$TLB" | jq_ "
d=d['data']
print('  status                =', d['status'])
print('  knownParts            =', d['knownParts'])
print('  latestUserExplanation =', repr(d['latestUserExplanation']))"
KP=$(api "$B/api/understandings/$TLB" | jq_ "print(d['data']['knownParts'])")
LUE=$(api "$B/api/understandings/$TLB" | jq_ "print(d['data']['latestUserExplanation'])")
[ "$KP" != "[]" ] && [ "$KP" != "None" ] && ok "knownParts 非空" || bad "knownParts 是空的"
[ "$LUE" != "None" ] && [ -n "$LUE" ] && ok "latestUserExplanation 非空" || bad "latestUserExplanation 是空的"

step "5. 验收标准第 3 步：Agent 上下文里存在 understanding:{conceptId} 区块"
# 不落盘、直接管道给 python：Git Bash 的 /tmp 与原生 Windows Python 的 /tmp 不是同一个目录，
# 中间转一手文件就会 FileNotFoundError（踩过一次）
PREVIEW=$(api -X POST "$AGENT/agent/context/preview" -H 'Content-Type: application/json' \
  -H "X-Internal-Token: ${AILS_INTERNAL_TOKEN:-}" -H "X-Internal-User-Id: 1" \
  -H "X-Internal-Identity: $(mint_identity 1)" \
  -d "{\"runId\":1,\"userId\":1,\"conversationId\":$CA,\"learningSpaceId\":$SID,\"userMessage\":\"TLB Miss 和 Page Fault 什么关系\",\"authToken\":\"$TOKEN\"}")
echo "$PREVIEW" | jq_ "
ctx = d['data']['context']
print('  retriever:', d['data']['retriever'], '| degraded:', d['data']['degraded'])
print('  区块:', [s['name'] for s in ctx['sections']])
print('  dropped:', ctx['droppedSections'], '| warnings:', ctx['warnings'])
print('  estimatedTokens:', ctx['estimatedTokens'])
print('  检索到的影子:', d['data']['summary']['retrieved'])
"
HAS=$(echo "$PREVIEW" | jq_ "print(any(s['name'].startswith('understanding:') for s in d['data']['context']['sections']))")
check "understanding 区块进了上下文" "$HAS" "True"
# 这一条不只是形式检查：断言里必须真的出现「用户自己的说法」，否则「进了区块」可能是个空壳
echo "$PREVIEW" | jq_ "
bad = [s for s in d['data']['context']['sections'] if s['name'].startswith('understanding:')]
print('  understanding 区块预览:', bad[0]['preview'][:160] if bad else '（没有）')
"
SYS=$(echo "$PREVIEW" | jq_ "print(d['data']['context']['systemInstruction'])")
case "$SYS" in
  *"我理解 TLB 就是 Page Table 的缓存"*) ok "用户自己的说法原样出现在 system instruction 里" ;;
  *) bad "system instruction 里没有用户自己的说法 —— 个性化是空的" ;;
esac
echo "$SYS" | grep -oE '## [^ ]+.*' | head -6 | sed 's/^/    区块标题: /'

# ---------------------------------------------------------------- 3. 跨对话

step "6. 验收标准第 4~5 步：新建 Conversation B，应 ATTACH 到同一个 session"
CB=$(api -X POST "$B/api/spaces/$SID/conversations" -d '{}' | jq_ "print(d['data']['id'])")
curl -sN -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -X POST "$B/api/conversations/$CB/messages" \
  -d '{"content":"那 Page Table 到底放在哪里？那 TLB 又是在哪一级？"}' > /tmp/reg_sse_b.txt
CB_SESS=$(api "$B/api/conversations/$CB" | jq_ "print(d['data']['sessionId'])")
echo "  conversationB=$CB  sessionId=$CB_SESS"
check "跨对话归入同一个学习活动（ATTACH 而不是新建）" "$CB_SESS" "$CA_SESS_AFTER"

step "7. 边界：Conversation C 发一句不含任何 Concept 的闲聊 → NONE"
CC=$(api -X POST "$B/api/spaces/$SID/conversations" -d '{}' | jq_ "print(d['data']['id'])")
curl -sN -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -X POST "$B/api/conversations/$CC/messages" \
  -d '{"content":"今天天气不错，随便聊聊吧"}' > /tmp/reg_sse_c.txt
CC_SESS=$(api "$B/api/conversations/$CC" | jq_ "print(d['data']['sessionId'])")
check "闲聊不产生学习活动，sessionId 仍为 NULL（NONE 分支）" "$CC_SESS" "None"

# ---------------------------------------------------------------- 4. Evidence 去重

step "8. Evidence 去重（B1）：dedupe_key 是唯一身份，且不含 agent_run_id"
echo "  本用户的 evidence："
q "SELECT id, concept_id, evidence_type, correctness, first_seen_run_id, last_seen_run_id, LEFT(dedupe_key,12) FROM learning_evidence WHERE user_id=(SELECT id FROM user WHERE username='$U') ORDER BY id;"
echo "  每条 evidence 挂了几条来源："
q "SELECT es.evidence_id, COUNT(*) FROM evidence_source es JOIN learning_evidence le ON le.id=es.evidence_id WHERE le.user_id=(SELECT id FROM user WHERE username='$U') GROUP BY es.evidence_id;"

# 旧口径是 UNIQUE(agent_run_id, concept_id, observation_hash)：跨 run 的重复
# **连唯一索引都拦不住**（AUDIT_NOTES.md §1）。新口径把 agent_run_id 从指纹里摘掉，
# 所以下面这次手工重复插入必须被 MySQL 拒掉（1062）—— 这就是「换个 run 再看一遍
# 同一条观察不会多出一行」在数据库层面的硬保证。
DUP_KEY=$(q "SELECT dedupe_key FROM learning_evidence WHERE user_id=(SELECT id FROM user WHERE username='$U') ORDER BY id LIMIT 1;")
echo "  拿现有某行的 dedupe_key($(echo "$DUP_KEY" | cut -c1-12)…) 再插一行，期望被唯一索引拒绝："
# 这次要看到 stderr 里的报错，所以不走 q()（q 把 stderr 吞了）
MYSQL_PWD=$(sed -n 's/^ *password: *"\(.*\)"/\1/p' "$YML" | head -1) \
  mysql -uroot ails -e "INSERT INTO learning_evidence
      (user_id, concept_id, evidence_type, correctness, observation, dedupe_key,
       first_seen_run_id, last_seen_run_id, analyzer_version, created_at)
    SELECT user_id, concept_id, evidence_type, correctness, observation, dedupe_key,
           9999, 9999, analyzer_version, NOW()
      FROM learning_evidence WHERE dedupe_key='$DUP_KEY';" 2>&1 > /dev/null | head -2
if [ "$(q "SELECT COUNT(*) FROM learning_evidence WHERE last_seen_run_id=9999;")" = "0" ]; then
  ok "UNIQUE(dedupe_key) 生效：跨 run 重复写入被数据库拦下（1062 Duplicate entry）"
else
  bad "重复 dedupe_key 竟然插进去了 —— 唯一索引不在，跨 run 去重无从谈起"
fi
check "uk_evidence_dedupe 索引存在" \
  "$(q "SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema='ails' AND table_name='learning_evidence' AND index_name='uk_evidence_dedupe';")" "1"

echo
echo "  说明：同一 message / 同一 highlight 被重复分析这条路径，通过 HTTP 走不到 ——"
echo "  主来源是行 id（MESSAGE:{id} / HIGHLIGHT:{id}），每次触发都是一行新来源，"
echo "  所以指纹必然不同。dedupe_key 拦的是**重跑分析**（回填、换分析器版本、批量重算）"
echo "  这类尚无 HTTP 入口的路径。指纹本身的规则由 EvidenceDedupeKeyTest 的 9 条单测覆盖"
echo "  （其中「换个 run 指纹不变」就是 B1 的核心断言）。"

# ---------------------------------------------------------------- 5. Memory scope

step "9. Memory scope：GLOBAL 记忆能不能被带 spaceId 的检索查到"
api -X POST "$B/api/memories" \
  -d '{"memoryType":"PREFERENCE","content":"用户喜欢从硬件实际执行路径理解操作系统","importance":0.9}' \
  | jq_ "m=d['data']; print('  GLOBAL :', m['scopeType'], '| spaceId=', m['learningSpaceId'], '|', m['memoryKey'])"
api -X POST "$B/api/memories" \
  -d "{\"learningSpaceId\":$SID,\"memoryType\":\"LEARNING\",\"content\":\"这个空间里已经学过虚拟内存基础\"}" \
  | jq_ "m=d['data']; print('  SPACE  :', m['scopeType'], '| spaceId=', m['learningSpaceId'], '|', m['memoryKey'])"
api -X POST "$B/api/memories" \
  -d "{\"learningSpaceId\":$SID,\"conceptId\":$TLB,\"memoryType\":\"LEARNING\",\"content\":\"TLB 这条线用户已经能自己讲清楚\"}" \
  | jq_ "m=d['data']; print('  CONCEPT:', m['scopeType'], '| spaceId=', m['learningSpaceId'], '| conceptId=', m['conceptId'], '|', m['memoryKey'])"

echo "  -- search?spaceId=$SID&conceptId=$TLB（应同时含 GLOBAL + SPACE + CONCEPT）--"
api "$B/api/memories/search?spaceId=$SID&conceptId=$TLB" | jq_ "
[print('   ', m['scopeType'], m['id'], m['content'][:22]) for m in d['data']]"
N=$(api "$B/api/memories/search?spaceId=$SID&conceptId=$TLB" | jq_ "print(len(d['data']))")
[ "$N" -ge 3 ] && ok "三类作用域都检索到了（$N 条）" || bad "只检索到 $N 条，GLOBAL 或 SPACE 泄漏了"

echo "  -- search?spaceId=99999（别的空间：只应剩 GLOBAL，不能漏出别人空间的书签）--"
OTHER=$(api "$B/api/memories/search?spaceId=99999" | jq_ "print(sum(1 for m in d['data'] if m['scopeType']!='GLOBAL'))")
check "跨空间不泄漏非 GLOBAL 记忆" "$OTHER" "0"

echo "  -- identity 撞车：同作用域同 memoryKey 再来一次 --"
api -X POST "$B/api/memories" \
  -d '{"memoryType":"PREFERENCE","content":"用户喜欢从硬件实际执行路径理解操作系统"}' \
  | jq_ "print('   code=', d['code'], '|', d['message'])"
echo "  -- 显式给 memoryKey 后可以并存 --"
api -X POST "$B/api/memories" \
  -d '{"memoryType":"PREFERENCE","memoryKey":"hardware-first-2","content":"用户喜欢从硬件实际执行路径理解操作系统"}' \
  | jq_ "print('   code=', d['code'], '| memoryKey=', (d['data'] or {}).get('memoryKey'))"

# ---------------------------------------------------------------- 6. SSE

step "10. SSE 闭环：事件序列必须是 message.start → message.delta… → message.end"
for f in /tmp/reg_sse_a.txt /tmp/reg_sse_b.txt; do
  echo "  --- $f ---"
  grep -o '^event:[a-z.]*' "$f" | sed 's/event://' | tr '\n' ' '; echo
done
E_A=$(grep -o '^event:[a-z.]*' /tmp/reg_sse_a.txt | head -1)
E_Z=$(grep -o '^event:[a-z.]*' /tmp/reg_sse_a.txt | tail -1)
check "首事件是 message.start" "$E_A" "event:message.start"
check "终事件是 message.end"   "$E_Z" "event:message.end"
grep -c '^event:message.delta' /tmp/reg_sse_a.txt | xargs -I{} echo "  delta 事件数：{}"

step "11. Agent 侧确实收到了 session / trace（B3/B4 的注入契约）"
# Agent 的日志路径允许外部覆盖：这个脚本只负责断言，不规定你怎么起 Agent。
# 找不到文件时直接说清楚，而不是让下面四条 grep 静默全空、报成「契约没传到」。
LOG=${AGENT_LOG:-/tmp/agent.log}
[ -f "$LOG" ] || echo "  ! 找不到 Agent 日志 $LOG（用 AGENT_LOG=/path/to/log 指定）"

echo "  -- 显式带上 traceId/session 的 preview：字段是否原样落到 Agent 侧 --"
api -X POST "$AGENT/agent/context/preview" -H 'Content-Type: application/json' \
  -H "X-Internal-Token: ${AILS_INTERNAL_TOKEN:-}" -H "X-Internal-User-Id: 1" \
  -H "X-Internal-Identity: $(mint_identity 1)" \
  -d "{\"runId\":1,\"userId\":1,\"conversationId\":$CA,\"learningSpaceId\":$SID,\"userMessage\":\"TLB\",\"authToken\":\"$TOKEN\",\"traceId\":\"0123456789abcdef0123456789abcdef\",\"session\":{\"id\":$CA_SESS_AFTER,\"title\":\"虚拟内存\",\"goal\":\"理解地址转换\",\"currentFocus\":\"卡在 TLB Miss\"}}" \
  | jq_ "
print('  trace.traceId     =', d['data']['trace']['traceId'])
print('  summary.sessionId =', d['data']['summary']['sessionId'])
print('  本阶段区块         =', 'session' in [s['name'] for s in d['data']['context']['sections']])
"

echo "  -- 真实 SSE 那一跳：从 agent.log 的 RUN_START / RUN_END 反查 --"
grep -o "'type': 'RUN_START'[^}]*}" "$LOG" | tail -3 | sed 's/^/    /'

# 三条断言，恰好就是 Resolver 的三个分支在 Agent 侧的样子
SESS_A=$(grep -o "'conversationId': $CA, 'spaceId': $SID, 'sessionId': [0-9None]*" "$LOG" | tail -1 | grep -o "[0-9None]*$")
SESS_B=$(grep -o "'conversationId': $CB, 'spaceId': $SID, 'sessionId': [0-9None]*" "$LOG" | tail -1 | grep -o "[0-9None]*$")
SESS_C=$(grep -o "'conversationId': $CC, 'spaceId': $SID, 'sessionId': [0-9None]*" "$LOG" | tail -1 | grep -o "[0-9None]*$")
check "Conversation A 首次发消息时 sessionId 已注入（CREATE 分支，session 在派发前就解析好了）" "$SESS_A" "$CA_SESS_AFTER"
check "Conversation B 注入的是同一个 sessionId（ATTACH 分支）" "$SESS_B" "$CA_SESS_AFTER"
check "Conversation C 的 sessionId 是 None（NONE 分支，闲聊不产生学习活动）" "$SESS_C" "None"

TRACES=$(grep -c "'traceId': '[0-9a-f]\{32\}'" "$LOG" 2>/dev/null || true)
TRACES=${TRACES:-0}
[ "$TRACES" -ge 3 ] && ok "每一次 run 都带了 32 位 hex 的 trace_id（本段 $TRACES 条）" \
  || bad "有 run 没带 trace_id（只找到 $TRACES 条）"
echo "  -- 后端 agent_run 里落库的 trace_id（两跳对得上的那个键）--"
q "SELECT id, run_type, status, trace_id FROM agent_run WHERE user_id=(SELECT id FROM user WHERE username='$U') ORDER BY id;" | sed 's/^/    /'

# ---------------------------------------------------------------- 汇总

step "汇总"
echo "  PASS=$PASS  FAIL=$FAIL"
[ "$FAIL" -eq 0 ] && echo "  闭环回归通过。" || echo "  有 $FAIL 项未通过，往上翻 FAIL 行。"
exit "$FAIL"
