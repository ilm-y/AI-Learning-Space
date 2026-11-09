"""准备 A/B/C 三组共用的唯一数据快照（隔离 MySQL）。

设计目标：让三组实验的**输入数据完全相同**，只允许上下文策略不同。

- 一个用户、一个空间、一个概念、一个会话、一个对话；
- 同一段两轮历史消息、同一份 Understanding / Revision / Evidence / Memory；
- 同一份 Knowledge 文档（与上面同一个空间）；
- 三组使用同一条复盘问题（``REVIEW_QUESTION``，intent=LEARNING_REVIEW）。

本脚本分两段能力：

1. 默认 ``--dry-run``：只读 state 文件、校验隔离、打印计划，不连库、不发请求；
2. ``--apply``：通过本机 Spring 公开 API 建 user/space/concept/session/conversation，
   并用**一个直接 SQL 事务**写入历史消息与个人上下文（与 pilot 同一模式）。

Knowledge 文档需要 Embedding + Qdrant 索引，单独由 ``--index-knowledge`` 触发，
且必须显式与 ``--apply`` 同时使用。本批次**不执行**该步骤，留给批次 3。

本脚本不打印、不写盘任何 API Key / internal token / DB 口令。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from snapshot_common import (
    DOCUMENT_TEXT,
    EVIDENCE_OBSERVATION,
    FIXTURE_MARKER,
    HISTORY_ASSISTANT,
    HISTORY_USER,
    MEMORY_CONTENT,
    PASSWORD,
    REVIEW_QUESTION,
    SESSION_FOCUS,
    SESSION_GOAL,
    UNDERSTANDING_EXPLANATION,
    UNDERSTANDING_KNOWN,
    UNDERSTANDING_SUMMARY,
    UNDERSTANDING_UNCERTAIN,
    assert_isolated_state,
    connect_mysql,
    generation_profile,
    load_state,
    sanitize_error,
    write_json,
)

SPACE_DESCRIPTION = "Phase 5 pre-Step3 A/B/C 统一快照：TCP 三次握手"


class SnapshotSetup:
    def __init__(
        self,
        state: dict[str, Any],
        *,
        index_knowledge: bool,
        snapshot_path: Path,
    ) -> None:
        self.state = state
        self.index_knowledge = index_knowledge
        self.snapshot_path = snapshot_path
        self.base = f"http://127.0.0.1:{state['springPort']}"
        self.token: str | None = None
        self.ids: dict[str, Any] = {}
        self.snapshot_time = time.strftime("%Y-%m-%d %H:%M:%S")

    # ---------- HTTP ----------

    def api(self, method: str, path: str, **kwargs: Any) -> Any:
        import requests

        headers = dict(kwargs.pop("headers", {}))
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = requests.request(
            method, self.base + path, headers=headers, timeout=120, **kwargs
        )
        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text[:500]}
        if response.status_code >= 400:
            raise RuntimeError(
                f"{method} {path} HTTP {response.status_code}: {str(data)[:400]}"
            )
        if isinstance(data, dict) and data.get("code") not in (0, None):
            raise RuntimeError(f"{method} {path} code={data.get('code')}: {str(data)[:400]}")
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    def register(self) -> None:
        username = "prestep3_" + self.state["stamp"].replace("-", "_")
        body = {"username": username, "password": PASSWORD}
        try:
            data = self.api("POST", "/api/auth/register", json=body)
        except RuntimeError:
            data = self.api("POST", "/api/auth/login", json=body)
        self.token = data["token"]
        self.ids["userId"] = int(data["user"]["id"])
        self.ids["username"] = username

    def create_space(self) -> None:
        data = self.api(
            "POST",
            "/api/spaces",
            json={"name": f"Phase 5 pre-Step3 {self.state['stamp']}", "description": SPACE_DESCRIPTION},
        )
        self.ids["spaceId"] = int(data["id"])
        self.ids["spaceName"] = data.get("name")

    def create_concept(self) -> None:
        data = self.api(
            "POST",
            f"/api/spaces/{self.ids['spaceId']}/concepts",
            json={
                "canonicalName": "TCP 三次握手",
                "displayName": "TCP 三次握手",
                "description": "TCP 建立连接的三次报文交换",
                "difficultyLevel": 2,
            },
        )
        self.ids["conceptId"] = int(data["id"])

    def create_session(self) -> None:
        data = self.api(
            "POST",
            "/api/sessions",
            json={
                "learningSpaceId": self.ids["spaceId"],
                "title": "TCP 三次握手：第三次 ACK",
                "goal": SESSION_GOAL,
            },
        )
        self.ids["sessionId"] = int(data["id"])
        self.api(
            "PATCH",
            f"/api/sessions/{self.ids['sessionId']}",
            json={"currentFocus": SESSION_FOCUS},
        )

    def create_conversation(self) -> None:
        data = self.api(
            "POST",
            f"/api/spaces/{self.ids['spaceId']}/conversations",
            json={"title": "pre-Step3 统一快照对话"},
        )
        self.ids["conversationId"] = int(data["id"])

    def upload_document(self) -> dict[str, Any]:
        """只有本步骤会调用 Embedding（经 Spring 上传并索引）。"""
        import requests

        response = requests.post(
            f"{self.base}/api/spaces/{self.ids['spaceId']}/documents",
            headers={"Authorization": f"Bearer {self.token}"},
            files={
                "file": (
                    "phase5-prestep3-tcp.md",
                    DOCUMENT_TEXT.encode("utf-8"),
                    "text/markdown",
                )
            },
            timeout=120,
        )
        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text[:500]}
        if response.status_code >= 400 or data.get("code") not in (0, None):
            raise RuntimeError(
                f"上传 Knowledge 文档失败 HTTP {response.status_code}: {str(data)[:400]}"
            )
        document_id = int(data["data"]["document"]["id"])
        deadline = time.monotonic() + 300
        last: dict[str, Any] = data["data"]["document"]
        while time.monotonic() < deadline:
            documents = self.api("GET", f"/api/spaces/{self.ids['spaceId']}/documents") or []
            for item in documents:
                if int(item["id"]) == document_id:
                    last = item
                    break
            if last.get("status") == "READY":
                break
            if last.get("status") == "FAILED":
                raise RuntimeError(f"文档索引失败：{last.get('indexError')}")
            time.sleep(2)
        else:
            raise RuntimeError(f"文档未在 300s 内 READY：{last.get('status')}")
        return {
            "indexed": True,
            "documentId": document_id,
            "status": last.get("status"),
            "chunkCount": last.get("chunkCount"),
            "vectorCollection": last.get("vectorCollection"),
            "fileChars": len(DOCUMENT_TEXT),
            "contentSha256": hashlib.sha256(
                DOCUMENT_TEXT.encode("utf-8")
            ).hexdigest(),
        }

    # ---------- SQL ----------

    def seed_personal_context(self) -> None:
        """1 个 SQL 事务：绑定 Session、写历史消息、U / E / R / M 与来源。"""
        now = self.snapshot_time
        knowledge_known = json.dumps(UNDERSTANDING_KNOWN, ensure_ascii=False)
        misconceptions = json.dumps([], ensure_ascii=False)
        uncertain = json.dumps(UNDERSTANDING_UNCERTAIN, ensure_ascii=False)

        with connect_mysql(self.state["db"]) as conn:
            try:
                conn.begin()
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE conversation SET session_id=%s, last_message_seq=2, "
                        "last_message_at=%s WHERE id=%s",
                        (self.ids["sessionId"], now, self.ids["conversationId"]),
                    )
                    message_ids: list[int] = []
                    for sequence, role, text in (
                        (1, "USER", HISTORY_USER),
                        (2, "ASSISTANT", HISTORY_ASSISTANT),
                    ):
                        cursor.execute(
                            "INSERT INTO message(conversation_id,role,message_type,"
                            "content,sequence_no,created_at) VALUES(%s,%s,'TEXT',%s,%s,%s)",
                            (self.ids["conversationId"], role, text, sequence, now),
                        )
                        message_ids.append(int(cursor.lastrowid))
                    self.ids["historyMessageIds"] = message_ids

                    cursor.execute(
                        "INSERT INTO learning_understanding(user_id,concept_id,summary,"
                        "known_parts,misconceptions,uncertain_parts,"
                        "latest_user_explanation,status,updated_at) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,'PARTIAL',%s)",
                        (
                            self.ids["userId"],
                            self.ids["conceptId"],
                            UNDERSTANDING_SUMMARY,
                            knowledge_known,
                            misconceptions,
                            uncertain,
                            UNDERSTANDING_EXPLANATION,
                            now,
                        ),
                    )
                    self.ids["understandingId"] = int(cursor.lastrowid)

                    dedupe = hashlib.sha256(
                        (
                            f"{self.ids['userId']}:{self.ids['conceptId']}:"
                            f"USER_EXPLANATION:{message_ids[0]}:{EVIDENCE_OBSERVATION}"
                        ).encode("utf-8")
                    ).hexdigest()
                    cursor.execute(
                        "INSERT INTO learning_evidence(user_id,concept_id,understanding_id,"
                        "evidence_type,observation,correctness,need_follow_up,"
                        "follow_up_status,dedupe_key,analyzer_version,created_at,updated_at) "
                        "VALUES(%s,%s,%s,'USER_EXPLANATION',%s,'PARTIAL',1,'PENDING',"
                        "%s,'phase5-prestep3-fixture',%s,%s)",
                        (
                            self.ids["userId"],
                            self.ids["conceptId"],
                            self.ids["understandingId"],
                            EVIDENCE_OBSERVATION,
                            dedupe,
                            now,
                            now,
                        ),
                    )
                    self.ids["evidenceId"] = int(cursor.lastrowid)
                    cursor.execute(
                        "INSERT INTO evidence_source(evidence_id,source_type,source_id,"
                        "created_at) VALUES(%s,'MESSAGE',%s,%s)",
                        (self.ids["evidenceId"], message_ids[0], now),
                    )
                    self.ids["evidenceSourceId"] = int(cursor.lastrowid)

                    cursor.execute(
                        "INSERT INTO understanding_revision(understanding_id,revision_no,"
                        "summary,known_parts,misconceptions,uncertain_parts,"
                        "user_explanation,status,trigger_evidence_id,created_at) "
                        "VALUES(%s,1,%s,%s,%s,%s,%s,'PARTIAL',%s,%s)",
                        (
                            self.ids["understandingId"],
                            UNDERSTANDING_SUMMARY,
                            knowledge_known,
                            misconceptions,
                            uncertain,
                            UNDERSTANDING_EXPLANATION,
                            self.ids["evidenceId"],
                            now,
                        ),
                    )
                    self.ids["revisionId"] = int(cursor.lastrowid)
                    # 显式写 updated_at：否则 ON UPDATE CURRENT_TIMESTAMP 可能
                    # 落到下一秒，使基线时间戳与 restore 使用的时间戳不一致。
                    cursor.execute(
                        "UPDATE learning_understanding SET current_revision_id=%s, "
                        "updated_at=%s WHERE id=%s",
                        (self.ids["revisionId"], now, self.ids["understandingId"]),
                    )

                    cursor.execute(
                        "INSERT INTO memory(user_id,scope_type,scope_key,learning_space_id,"
                        "concept_id,memory_type,memory_key,content,status,importance,version,"
                        "created_at,updated_at) VALUES(%s,'CONCEPT',%s,%s,%s,'LEARNING',"
                        "'phase5-prestep3-tcp-style',%s,'ACTIVE',0.9,0,%s,%s)",
                        (
                            self.ids["userId"],
                            f"CONCEPT:{self.ids['conceptId']}",
                            self.ids["spaceId"],
                            self.ids["conceptId"],
                            MEMORY_CONTENT,
                            now,
                            now,
                        ),
                    )
                    self.ids["memoryId"] = int(cursor.lastrowid)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ---------- 计划与清单 ----------

    def plan(self) -> dict[str, Any]:
        assert_isolated_state(self.state)
        return {
            "envId": self.state["collectionPrefix"],
            "db": self.state["db"],
            "docCollection": self.state["collection"],
            "memoryCollection": self.state["memoryCollection"],
            "springPort": self.state["springPort"],
            "agentPort": self.state["agentPort"],
            "question": REVIEW_QUESTION,
            "fixtureMarker": FIXTURE_MARKER,
            "knowledge": (
                "将通过 Spring 上传并索引（调用 Embedding）"
                if self.index_knowledge
                else "未索引：批次 3 需显式 --index-knowledge，否则 Knowledge 为空"
            ),
            "consumers": "运行期间必须关闭分析消费者（Understanding/Evidence/Memory 写回）",
            "snapshotPath": str(self.snapshot_path),
        }

    def manifest(self, knowledge: dict[str, Any]) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "kind": "phase5-pre-step3-snapshot",
            "fixtureMarker": FIXTURE_MARKER,
            "createdAt": datetime.now().isoformat(timespec="seconds"),
            "snapshotTime": self.snapshot_time,
            "env": {
                "stamp": self.state["stamp"],
                "db": self.state["db"],
                "docCollection": self.state["collection"],
                "memoryCollection": self.state["memoryCollection"],
                "agentPort": self.state["agentPort"],
                "springPort": self.state["springPort"],
            },
            "isolation": {
                "db": self.state["db"],
                "collectionPrefix": self.state["collectionPrefix"],
                "memoryCollectionPrefix": self.state["memoryCollectionPrefix"],
            },
            "dataset": dict(self.ids),
            "question": REVIEW_QUESTION,
            "generationProfile": generation_profile(),
            "knowledge": knowledge,
            "consumers": {
                "analysisWrites": "MUST_BE_DISABLED",
                "note": "三组运行期间关闭 Understanding/Evidence/Memory 写回，"
                "否则冻结表哈希必然漂移。",
            },
            "notes": [
                "三组必须使用同一个 conversationId 与同一条 question。",
                "A 组的“无个人上下文”由 context_strategy=A 实现，数据本身不删。",
                "每组运行前用 restore_snapshot.py 复原消息与个人上下文，"
                "并用 freeze_check.py 与 baseline 对比。",
            ],
        }

    def run(self) -> dict[str, Any]:
        self.register()
        self.create_space()
        self.create_concept()
        self.create_session()
        self.create_conversation()
        self.seed_personal_context()
        knowledge: dict[str, Any] = {
            "indexed": False,
            "documentId": None,
            "deferred": "未执行 --index-knowledge；批次 3 必须补做，否则 Knowledge 为空",
        }
        if self.index_knowledge:
            knowledge = self.upload_document()

        manifest = self.manifest(knowledge)
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.snapshot_path, manifest)
        return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_file", nargs="?", help="start_services.py 生成的 STATE_FILE")
    parser.add_argument("--apply", action="store_true", help="真正写入隔离 MySQL")
    parser.add_argument(
        "--index-knowledge",
        action="store_true",
        help="上传并索引 Knowledge 文档（调用 Embedding；必须与 --apply 同用）",
    )
    parser.add_argument("--json-out", help="快照清单输出路径（默认 <tempDir>/prestep3-snapshot.json）")
    parser.add_argument("--force", action="store_true", help="允许覆盖已存在的快照清单")
    parser.add_argument("--self-test", action="store_true", help="不连库、不发请求的自检")
    args = parser.parse_args()

    if args.self_test:
        print(json.dumps(self_test(), ensure_ascii=False, indent=2))
        return 0
    if not args.state_file:
        parser.error("需要 STATE_FILE（或使用 --self-test）")
    if args.index_knowledge and not args.apply:
        parser.error("--index-knowledge 必须与 --apply 同用")

    state = load_state(args.state_file)
    assert_isolated_state(state)
    snapshot_path = (
        Path(args.json_out)
        if args.json_out
        else Path(state["tempDir"]) / "prestep3-snapshot.json"
    )
    setup = SnapshotSetup(
        state, index_knowledge=args.index_knowledge, snapshot_path=snapshot_path
    )

    plan = setup.plan()
    print("=== 统一快照计划 ===")
    print(json.dumps(plan, ensure_ascii=False, indent=2))

    if not args.apply:
        print("\n[dry-run] 未写库、未发请求。确认后用 --apply 执行。")
        return 0

    if snapshot_path.exists() and not args.force:
        raise SystemExit(
            f"快照清单已存在：{snapshot_path}；确认要重建请加 --force"
        )

    started = time.monotonic()
    try:
        manifest = setup.run()
    except Exception as exc:  # noqa: BLE001 - 保留类型与短消息，避免泄露配置
        raise SystemExit("快照准备失败：" + sanitize_error(exc))
    elapsed = int((time.monotonic() - started) * 1000)

    print("\n=== 快照已写入 ===")
    print(json.dumps(manifest["dataset"], ensure_ascii=False, indent=2))
    print(f"knowledge.indexed = {manifest['knowledge'].get('indexed')}")
    print(f"snapshot = {snapshot_path}")
    print(f"elapsedMs = {elapsed}")
    print("\n下一步：python freeze_check.py <snapshot> --write-baseline")
    return 0


def self_test() -> dict[str, Any]:
    """纯函数自检：隔离断言与 question / profile 的确定性。"""
    good = {
        "db": "phase5_pilot_20261109120000_abcdef",
        "collectionPrefix": "phase5-pilot-20261109120000-abcdef",
        "memoryCollectionPrefix": "phase5-pilot-20261109120000-abcdef-memory",
    }
    env_id = assert_isolated_state(good)

    rejected: list[str] = []
    for broken in (
        {**good, "db": "ails"},
        {**good, "collectionPrefix": "ails_documents"},
        {**good, "memoryCollectionPrefix": "ails_memories"},
        {**good, "memoryCollectionPrefix": good["collectionPrefix"] + "-mem"},
    ):
        try:
            assert_isolated_state(broken)
        except SystemExit:
            rejected.append("rejected")
        else:
            rejected.append("ACCEPTED")

    profile = generation_profile()
    return {
        "envId": env_id,
        "isolationRejections": rejected,
        "question": REVIEW_QUESTION,
        "generationProfileKeys": sorted(profile),
        "baseSystemInstructionSha256": profile["baseSystemInstructionSha256"],
        "documentChars": len(DOCUMENT_TEXT),
    }


if __name__ == "__main__":
    sys.exit(main())
