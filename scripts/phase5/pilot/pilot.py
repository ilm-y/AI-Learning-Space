"""Phase 5 Step 3 Pilot：A/B/C 三次真实调用。

前置：start_services.py 已启动隔离 Agent(8001) + Spring(8081)，并输出 STATE_FILE。

本脚本：
- 在隔离 MySQL 库中通过 Spring 公开 API 建 user/space/concept/conversation/session；
- 用 1 个直接 SQL 事务写入 B/C 需要的确定性个人上下文 fixture
  （Understanding / Revision / Evidence / Memory / History）与 session 绑定；
- A/B/C 各调用一次真实 LLM（经 Spring → Agent → DeepSeek）；
- 记录 SSE、AgentRun、Manifest、Citation、LLM diagnostics；
- 不删除数据；清理由 cleanup.py 单独执行。

不打印、不写盘任何 API Key / internal token / DB 口令。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import connect_mysql, read_state, sanitize_error, write_json  # noqa: E402

PASSWORD = secrets.token_urlsafe(18)
A_QUESTION = "TCP 三次握手中，为什么第三次握手是必要的？"
B_QUESTION = (
    "帮我回顾一下我目前对 TCP 三次握手为什么需要第三次握手的理解；"
    "请引用我之前说过的原话作为证据，指出我卡在哪里。"
)
C_QUESTION = (
    "请结合我刚上传的资料，解释 TCP 三次握手为什么需要第三次握手，"
    "并引用证据说明我当前理解里还缺什么。"
)

HISTORY_USER = "我先说说我的理解：TCP 三次握手是客户端发 SYN，服务端回 SYN-ACK，客户端再回 ACK。"
HISTORY_ASSISTANT = "你抓住了三步顺序，但还没解释第三次 ACK 的必要性。"
OBSERVATION = (
    "用户说：客户端先发 SYN，服务端回 SYN-ACK，客户端再回 ACK，"
    "但不清楚为什么还要第三次。"
)


class Pilot:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.base = f"http://127.0.0.1:{state['springPort']}"
        self.agent_base = f"http://127.0.0.1:{state['agentPort']}"
        self.qdrant = "http://127.0.0.1:6333"
        self.temp = Path(state["tempDir"])
        self.token: str | None = None
        self.user_id: int | None = None
        self.space_id: int | None = None
        self.concept_id: int | None = None
        self.session_id: int | None = None
        self.conv_a: int | None = None
        self.conv_b: int | None = None
        self.conv_c: int | None = None
        self.results: dict[str, Any] = {
            "stamp": state["stamp"],
            "db": state["db"],
            "collection": state["collection"],
            "questions": {"A": A_QUESTION, "B": B_QUESTION, "C": C_QUESTION},
            "calls": {},
            "dataset": {},
            "checks": {},
        }

    # ---------- HTTP ----------

    def api(self, method: str, path: str, *, token: str | None = None, **kwargs) -> Any:
        headers = dict(kwargs.pop("headers", {}))
        token = self.token if token is None else token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = requests.request(
            method, self.base + path, headers=headers, timeout=60, **kwargs
        )
        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text[:500]}
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path} HTTP {response.status_code}: {str(data)[:400]}")
        if isinstance(data, dict) and data.get("code") not in (0, None):
            raise RuntimeError(f"{method} {path} code={data.get('code')}: {str(data)[:400]}")
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    def register(self) -> None:
        username = "phase5pilot_" + self.state["stamp"].replace("-", "_")
        body = {"username": username, "password": PASSWORD}
        try:
            data = self.api("POST", "/api/auth/register", token="", json=body)
        except RuntimeError:
            data = self.api("POST", "/api/auth/login", token="", json=body)
        self.token = data["token"]
        self.user_id = int(data["user"]["id"])
        self.results["dataset"]["user"] = {"id": self.user_id, "username": username}

    def create_space(self) -> None:
        data = self.api(
            "POST",
            "/api/spaces",
            json={
                "name": f"Phase 5 Pilot {self.state['stamp']}",
                "description": "隔离 Pilot：TCP 三次握手",
            },
        )
        self.space_id = int(data["id"])
        self.results["dataset"]["space"] = {"id": self.space_id, "name": data.get("name")}

    def create_concept(self) -> None:
        data = self.api(
            "POST",
            f"/api/spaces/{self.space_id}/concepts",
            json={
                "canonicalName": "TCP 三次握手",
                "displayName": "TCP 三次握手",
                "description": "TCP 建立连接的三次报文交换",
                "difficultyLevel": 2,
            },
        )
        self.concept_id = int(data["id"])
        self.results["dataset"]["concept"] = {
            "id": self.concept_id,
            "canonicalName": data.get("canonicalName"),
            "displayName": data.get("displayName"),
        }

    def create_conversation(self, title: str) -> int:
        data = self.api(
            "POST",
            f"/api/spaces/{self.space_id}/conversations",
            json={"title": title},
        )
        return int(data["id"])

    def create_session(self) -> None:
        data = self.api(
            "POST",
            "/api/sessions",
            json={
                "learningSpaceId": self.space_id,
                "goal": "真正理解 TCP 三次握手，尤其是第三次 ACK 的必要性",
                "title": "TCP 三次握手：第三次 ACK",
            },
        )
        self.session_id = int(data["id"])
        self.api(
            "PATCH",
            f"/api/sessions/{self.session_id}",
            json={"currentFocus": "说不清第三次握手为什么不能省"},
        )
        self.results["dataset"]["session"] = {
            "id": self.session_id,
            "goal": data.get("goal"),
            "currentFocus": "说不清第三次握手为什么不能省",
        }

    # ---------- SSE ----------

    def chat(self, conversation_id: int, content: str, label: str) -> dict[str, Any]:
        client_request_id = str(uuid.uuid4())
        started = time.monotonic()
        first_delta_ms: int | None = None
        current_event: str | None = None
        data_lines: list[str] = []
        raw_lines: list[str] = []
        events: list[str] = []
        payloads: list[dict[str, Any]] = []
        deltas: list[str] = []
        end_payload: dict[str, Any] | None = None
        error_payload: dict[str, Any] | None = None

        def flush() -> None:
            nonlocal current_event, data_lines, end_payload, error_payload, first_delta_ms
            if current_event is None:
                data_lines = []
                return
            event = current_event
            raw = "\n".join(data_lines)
            payload: dict[str, Any] = {}
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        payload = parsed
                except json.JSONDecodeError:
                    payload = {"raw": raw}
            events.append(event)
            payloads.append(payload)
            if event == "message.delta":
                delta = payload.get("delta")
                if isinstance(delta, str) and delta:
                    if first_delta_ms is None:
                        first_delta_ms = int((time.monotonic() - started) * 1000)
                    deltas.append(delta)
            elif event == "message.end":
                end_payload = payload
            elif event == "message.error":
                error_payload = payload
            current_event = None
            data_lines = []

        url = f"{self.base}/api/conversations/{conversation_id}/messages"
        with requests.post(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            json={"content": content, "clientRequestId": client_request_id},
            stream=True,
            timeout=240,
        ) as response:
            response.encoding = "utf-8"
            http_status = response.status_code
            content_type = response.headers.get("Content-Type", "")
            for raw in response.iter_lines(decode_unicode=True):
                if raw is None:
                    continue
                line = raw.rstrip("\r")
                raw_lines.append(line)
                if line == "":
                    flush()
                elif line.startswith("event:"):
                    current_event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            flush()

        elapsed_ms = int((time.monotonic() - started) * 1000)
        diagnostics = self._diagnostics()
        call = {
            "label": label,
            "conversationId": conversation_id,
            "clientRequestId": client_request_id,
            "httpStatus": http_status,
            "contentType": content_type,
            "events": events,
            "answer": "".join(deltas),
            "empty": not "".join(deltas).strip(),
            "firstDeltaMs": first_delta_ms,
            "elapsedMs": elapsed_ms,
            "end": end_payload,
            "error": error_payload,
            "diagnostics": diagnostics,
        }
        (self.temp / f"{label}.sse").write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
        write_json(self.temp / f"{label}.json", call)
        self.results["calls"][label] = call
        return call

    def _diagnostics(self) -> dict[str, Any] | None:
        for _ in range(20):
            try:
                response = requests.get(
                    f"{self.agent_base}/phase5/llm-diagnostics", timeout=5
                )
                if response.status_code == 200:
                    return response.json()
            except requests.RequestException:
                pass
            time.sleep(0.2)
        return None

    # ---------- DB ----------

    def db_conn(self):
        return connect_mysql(self.state["db"])

    def run_row(self, conversation_id: int) -> dict[str, Any] | None:
        with self.db_conn() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT id,status,trace_id,context_manifest,duration_ms,error_message,"
                "assistant_message_id,output_summary "
                "FROM agent_run WHERE conversation_id=%s AND run_type='CHAT' "
                "ORDER BY id DESC LIMIT 1",
                (conversation_id,),
            )
            row = cursor.fetchone()
        if not row:
            return None
        manifest = row["context_manifest"]
        if isinstance(manifest, str) and manifest:
            try:
                manifest = json.loads(manifest)
            except json.JSONDecodeError:
                pass
        row["context_manifest"] = manifest
        return row

    def seed_personal_context(self) -> dict[str, Any]:
        """1 个直接 SQL 事务：绑定 session、写 history、U/R/E/M。"""
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        history_ids: dict[str, list[int]] = {}
        with self.db_conn() as conn:
            try:
                conn.begin()
                with conn.cursor() as cursor:
                    for conv in (self.conv_b, self.conv_c):
                        cursor.execute(
                            "UPDATE conversation SET session_id=%s, last_message_seq=2, "
                            "last_message_at=%s WHERE id=%s",
                            (self.session_id, now, conv),
                        )
                        ids = []
                        for seq, role, text in (
                            (1, "USER", HISTORY_USER),
                            (2, "ASSISTANT", HISTORY_ASSISTANT),
                        ):
                            cursor.execute(
                                "INSERT INTO message(conversation_id,role,message_type,content,"
                                "sequence_no,created_at) VALUES(%s,%s,'TEXT',%s,%s,%s)",
                                (conv, role, text, seq, now),
                            )
                            ids.append(int(cursor.lastrowid))
                        history_ids[str(conv)] = ids

                    known_parts = json.dumps(
                        ["能说出 SYN、SYN-ACK、ACK 的顺序"], ensure_ascii=False
                    )
                    misconceptions = json.dumps([], ensure_ascii=False)
                    uncertain_parts = json.dumps(
                        ["第三次握手是否只是为了确认服务端收到 ACK"], ensure_ascii=False
                    )
                    explanation = (
                        "客户端先发 SYN，服务端回 SYN-ACK，客户端再回 ACK。"
                    )
                    cursor.execute(
                        "INSERT INTO learning_understanding(user_id,concept_id,summary,"
                        "known_parts,misconceptions,uncertain_parts,latest_user_explanation,"
                        "status,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,'PARTIAL',%s)",
                        (
                            self.user_id,
                            self.concept_id,
                            "知道三次握手的顺序，但不确定第三次握手的必要性。",
                            known_parts,
                            misconceptions,
                            uncertain_parts,
                            explanation,
                            now,
                        ),
                    )
                    understanding_id = int(cursor.lastrowid)

                    dedupe = hashlib.sha256(
                        f"{self.user_id}:{self.concept_id}:USER_EXPLANATION:"
                        f"{history_ids[str(self.conv_b)][0]}:{OBSERVATION}".encode("utf-8")
                    ).hexdigest()
                    cursor.execute(
                        "INSERT INTO learning_evidence(user_id,concept_id,understanding_id,"
                        "evidence_type,observation,correctness,need_follow_up,follow_up_status,"
                        "dedupe_key,analyzer_version,created_at,updated_at) "
                        "VALUES(%s,%s,%s,'USER_EXPLANATION',%s,'PARTIAL',1,'PENDING',%s,"
                        "'phase5-pilot-fixture',%s,%s)",
                        (
                            self.user_id,
                            self.concept_id,
                            understanding_id,
                            OBSERVATION,
                            dedupe,
                            now,
                            now,
                        ),
                    )
                    evidence_id = int(cursor.lastrowid)
                    cursor.execute(
                        "INSERT INTO evidence_source(evidence_id,source_type,source_id,created_at) "
                        "VALUES(%s,'MESSAGE',%s,%s)",
                        (evidence_id, history_ids[str(self.conv_b)][0], now),
                    )
                    cursor.execute(
                        "INSERT INTO understanding_revision(understanding_id,revision_no,summary,"
                        "known_parts,misconceptions,uncertain_parts,user_explanation,status,"
                        "trigger_evidence_id,created_at) "
                        "VALUES(%s,1,%s,%s,%s,%s,%s,'PARTIAL',%s,%s)",
                        (
                            understanding_id,
                            "知道三次握手的顺序，但不确定第三次握手的必要性。",
                            known_parts,
                            misconceptions,
                            uncertain_parts,
                            explanation,
                            evidence_id,
                            now,
                        ),
                    )
                    revision_id = int(cursor.lastrowid)
                    cursor.execute(
                        "UPDATE learning_understanding SET current_revision_id=%s WHERE id=%s",
                        (revision_id, understanding_id),
                    )
                    memory_content = (
                        "用户偏好先用报文顺序理解 TCP 三次握手，再补协议细节；"
                        "目前对第三次握手的必要性有疑问。"
                    )
                    cursor.execute(
                        "INSERT INTO memory(user_id,scope_type,scope_key,learning_space_id,"
                        "concept_id,memory_type,memory_key,content,status,importance,version,"
                        "created_at,updated_at) VALUES(%s,'CONCEPT',%s,%s,%s,'LEARNING',"
                        "'phase5-pilot-tcp-style',%s,'ACTIVE',0.9,0,%s,%s)",
                        (
                            self.user_id,
                            f"CONCEPT:{self.concept_id}",
                            self.space_id,
                            self.concept_id,
                            memory_content,
                            now,
                            now,
                        ),
                    )
                    memory_id = int(cursor.lastrowid)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "directWriteTransactions": 1,
            "historyMessageIds": history_ids,
            "understandingId": understanding_id,
            "revisionId": revision_id,
            "evidenceId": evidence_id,
            "memoryId": memory_id,
        }

    def agent_run_columns(self) -> list[str]:
        with self.db_conn() as conn, conn.cursor() as cursor:
            cursor.execute("SHOW COLUMNS FROM agent_run")
            return [row["Field"] for row in cursor.fetchall()]

    # ---------- Qdrant / documents ----------

    def create_collection(self) -> dict[str, Any]:
        name = self.state["collection"]
        response = requests.put(
            f"{self.qdrant}/collections/{name}",
            json={"vectors": {"size": 1024, "distance": "Cosine"}},
            timeout=30,
        )
        if response.status_code not in (200, 201, 409):
            raise RuntimeError(f"创建 Qdrant collection 失败 HTTP {response.status_code}")
        info = requests.get(f"{self.qdrant}/collections/{name}", timeout=30)
        if info.status_code != 200:
            raise RuntimeError(f"读取 Qdrant collection 失败 HTTP {info.status_code}")
        return {
            "name": name,
            "createdHttp": response.status_code,
            "existsAfter": True,
            "pointsAtStart": self.qdrant_count(),
        }

    def qdrant_count(self) -> int | None:
        response = requests.post(
            f"{self.qdrant}/collections/{self.state['collection']}/points/count",
            json={"exact": True},
            timeout=30,
        )
        if response.status_code != 200:
            return None
        return int(response.json()["result"]["count"])

    def upload_document(self) -> dict[str, Any]:
        fixture = Path(__file__).with_name("fixture_tcp.md").read_text(encoding="utf-8")
        response = requests.post(
            f"{self.base}/api/spaces/{self.space_id}/documents",
            headers={"Authorization": f"Bearer {self.token}"},
            files={"file": ("phase5-pilot-tcp.md", fixture.encode("utf-8"), "text/markdown")},
            timeout=60,
        )
        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text[:500]}
        if response.status_code >= 400 or data.get("code") not in (0, None):
            raise RuntimeError(f"上传文档失败 HTTP {response.status_code}: {str(data)[:400]}")
        document = data["data"]["document"]
        document_id = int(document["id"])
        deadline = time.monotonic() + 240
        last: dict[str, Any] = document
        while time.monotonic() < deadline:
            docs = self.api("GET", f"/api/spaces/{self.space_id}/documents") or []
            for item in docs:
                if int(item["id"]) == document_id:
                    last = item
                    break
            if last.get("status") == "READY":
                break
            if last.get("status") == "FAILED":
                raise RuntimeError(f"文档索引失败：{last.get('indexError')}")
            time.sleep(2)
        else:
            raise RuntimeError(f"文档未在 240s 内 READY：{last}")
        return {
            "document": last,
            "fileChars": len(fixture),
            "pointsAfterIndex": self.qdrant_count(),
        }

    def messages(self, conversation_id: int) -> list[dict[str, Any]]:
        return list(self.api("GET", f"/api/conversations/{conversation_id}/messages") or [])

    def assistant_message_row(self, conversation_id: int) -> dict[str, Any] | None:
        with self.db_conn() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT id,content,COALESCE(tokens,-1) AS tokens,truncated,citations "
                "FROM message WHERE conversation_id=%s AND role='ASSISTANT' "
                "ORDER BY id DESC LIMIT 1",
                (conversation_id,),
            )
            row = cursor.fetchone()
        if row and isinstance(row.get("citations"), str):
            try:
                row["citations"] = json.loads(row["citations"])
            except json.JSONDecodeError:
                pass
        return row


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: pilot.py <STATE_FILE>")
    state = read_state(sys.argv[1])
    pilot = Pilot(state)
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        pilot.results["collectionCreated"] = pilot.create_collection()
        pilot.register()
        pilot.create_space()
        pilot.conv_a = pilot.create_conversation("Pilot A：无个人上下文基线")
        pilot.results["dataset"]["conversations"] = {"A": pilot.conv_a}

        # A：space 内尚无 concept / document，Session 为 null，History 为空。
        call_a = pilot.chat(pilot.conv_a, A_QUESTION, "A")
        run_a = pilot.run_row(pilot.conv_a)
        pilot.results["calls"]["A"]["run"] = run_a

        # 之后才创建 concept；这样 A 不会被 Spring/Agent 的概念识别命中。
        pilot.create_concept()
        pilot.create_session()
        pilot.conv_b = pilot.create_conversation("Pilot B：D4 个人上下文，无知识")
        pilot.conv_c = pilot.create_conversation("Pilot C：D4 个人上下文 + 知识")
        pilot.results["dataset"]["conversations"].update({"B": pilot.conv_b, "C": pilot.conv_c})
        pilot.results["dataset"].update(pilot.seed_personal_context())

        call_b = pilot.chat(pilot.conv_b, B_QUESTION, "B")
        run_b = pilot.run_row(pilot.conv_b)
        pilot.results["calls"]["B"]["run"] = run_b

        pilot.results["dataset"]["document"] = pilot.upload_document()

        call_c = pilot.chat(pilot.conv_c, C_QUESTION, "C")
        run_c = pilot.run_row(pilot.conv_c)
        pilot.results["calls"]["C"]["run"] = run_c

        pilot.results["messages"] = {
            "A": pilot.messages(pilot.conv_a),
            "B": pilot.messages(pilot.conv_b),
            "C": pilot.messages(pilot.conv_c),
        }
        pilot.results["assistantRows"] = {
            "A": pilot.assistant_message_row(pilot.conv_a),
            "B": pilot.assistant_message_row(pilot.conv_b),
            "C": pilot.assistant_message_row(pilot.conv_c),
        }
        columns = pilot.agent_run_columns()
        pilot.results["checks"] = {
            "agentRunColumns": columns,
            "agentRunHasModelColumn": any("model" in c.lower() for c in columns),
            "agentRunHasTokenUsageColumn": any(
                "token" in c.lower() or "usage" in c.lower() for c in columns
            ),
            "aAnswerEmpty": call_a["empty"],
            "bAnswerEmpty": call_b["empty"],
            "cAnswerEmpty": call_c["empty"],
            "aEnd": call_a["end"],
            "bEnd": call_b["end"],
            "cEnd": call_c["end"],
            "aError": call_a["error"],
            "bError": call_b["error"],
            "cError": call_c["error"],
        }
    except Exception as exc:  # noqa: BLE001
        pilot.results["fatalError"] = sanitize_error(exc)
        write_json(pilot.temp / "pilot-results.json", pilot.results)
        print(json.dumps({"ok": False, "error": pilot.results["fatalError"]}, ensure_ascii=False))
        return 1

    pilot.results["startedAt"] = started
    pilot.results["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    write_json(pilot.temp / "pilot-results.json", pilot.results)
    summary = {
        "ok": True,
        "resultsFile": str(pilot.temp / "pilot-results.json"),
        "dataset": pilot.results["dataset"],
        "runs": {
            label: {
                "runId": (pilot.results["calls"][label].get("run") or {}).get("id"),
                "status": (pilot.results["calls"][label].get("run") or {}).get("status"),
                "manifestPresent": (pilot.results["calls"][label].get("run") or {}).get(
                    "context_manifest"
                )
                is not None,
                "answerChars": len(pilot.results["calls"][label]["answer"]),
                "empty": pilot.results["calls"][label]["empty"],
                "finishReason": (
                    (pilot.results["calls"][label].get("diagnostics") or {})
                    .get("stream", {})
                    .get("finishReason")
                ),
                "usage": (
                    (pilot.results["calls"][label].get("diagnostics") or {})
                    .get("stream", {})
                    .get("usage")
                ),
                "citations": (pilot.results["calls"][label].get("end") or {}).get(
                    "citations"
                ),
            }
            for label in ("A", "B", "C")
        },
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
