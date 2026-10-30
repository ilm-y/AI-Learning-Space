#!/usr/bin/env python3
"""Phase 5.4：真实 Qdrant + 真实 Embedding + 真实模型的文档引用 E2E。

范围：
  1. 通过 Spring 公开 API 上传两篇合成文档（现行规范 / 历史迁移），等待 READY；
  2. 直接只读核对 Qdrant collection、point 数与 payload；
  3. 通过 Agent `/agent/context/preview` 确认知识片段真的进入上下文；
  4. 通过 Spring SSE 问「现行值 / 历史值 / 无关问题」，核对回答、引用标签与拒绝。

不做的事：不清理数据库或 Qdrant；不使用 Mock LLM / hash embedding；不把 HTTP 200
当成引用正确。脚本会保留合成数据与产物，便于复查。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TIMEOUT = (10, 300)
STAMP = time.strftime("%Y%m%d-%H%M%S")
PASSWORD = "Passw0rd!23"

TARGET_FILE = f"orbit_current_{STAMP}.md"
LEGACY_FILE = f"orbit_legacy_{STAMP}.md"
TARGET_TEXT = f"""# ORBIT-4721 当前调度规范

本文是 ORBIT-4721 当前部署的现行规范，标记 P4-TARGET-7K2。

当前调度器使用 17 秒的重试窗口（retry window）。该值从 2025 版开始生效，适用于所有新部署。校验种子是 0x7F3A。

运维注意：不要使用历史迁移文档中的旧值。旧值只描述已经下线的调度器。
"""
LEGACY_TEXT = f"""# ORBIT-4721 历史迁移记录

标记 P4-DISTRACTOR-3M8。2024 年及以前的 ORBIT-4721 旧调度器使用 12 秒的重试窗口（retry window）。

该版本已在 2025 年下线。本记录只用于迁移考古，不适用于当前部署。不要把 12 秒当作现行值。
"""

LABEL_RE = re.compile(r"资料《([^》]+)》#(\d+)")


class Run:
    def __init__(self, base: str, agent: str, qdrant: str) -> None:
        self.base = base.rstrip("/")
        self.agent = agent.rstrip("/")
        self.qdrant = qdrant.rstrip("/")
        self.out_dir = ROOT / "backend" / "backend" / "target" / "phase54-rag-e2e" / STAMP
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.checks: list[dict[str, Any]] = []
        self.token = ""
        self.user_id = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"（{detail}）" if detail else ""), flush=True)
        return bool(ok)

    def section(self, title: str) -> None:
        print(f"\n== {title} ==", flush=True)

    def api(self, method: str, path: str, body: dict[str, Any] | None = None,
            *, token: str | None = None, files: dict[str, Any] | None = None) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {token if token is not None else self.token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        response = requests.request(
            method, urljoin(self.base, path), headers=headers, json=body,
            files=files, timeout=DEFAULT_TIMEOUT,
        )
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text

    def api_ok(self, method: str, path: str, body: dict[str, Any] | None = None,
               *, token: str | None = None) -> Any:
        status, data = self.api(method, path, body, token=token)
        if status != 200 or not isinstance(data, dict) or data.get("code") != 0:
            raise RuntimeError(f"{method} {path} 失败：HTTP {status} {str(data)[:400]}")
        return data.get("data")

    def register(self) -> None:
        user = self.api_ok("POST", "/api/auth/register", {
            "username": f"p54rag_{STAMP}", "password": PASSWORD,
        }, token="")
        self.token = user["token"]
        self.user_id = int(user["user"]["id"])

    def upload(self, space_id: int, filename: str, text: str) -> dict[str, Any]:
        status, data = self.api(
            "POST", f"/api/spaces/{space_id}/documents",
            files={"file": (filename, text.encode("utf-8"), "text/markdown")},
        )
        if status != 200 or not isinstance(data, dict) or data.get("code") != 0:
            raise RuntimeError(f"上传 {filename} 失败：HTTP {status} {str(data)[:400]}")
        return data["data"]["document"]

    def documents(self, space_id: int) -> list[dict[str, Any]]:
        return list(self.api_ok("GET", f"/api/spaces/{space_id}/documents") or [])

    def wait_ready(self, space_id: int, document_ids: set[int], timeout: int = 240) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        last: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            last = self.documents(space_id)
            ready = {int(d["id"]) for d in last if d.get("status") == "READY"}
            if document_ids <= ready:
                return last
            time.sleep(2)
        raise TimeoutError(f"文档未在 {timeout}s 内 READY；最后状态={[(d.get('id'), d.get('status'), d.get('indexError')) for d in last]}")

    def chat(self, conversation_id: int, content: str, filename: str) -> dict[str, Any]:
        started = time.monotonic()
        events: list[str] = []
        deltas: list[str] = []
        payloads: list[dict[str, Any]] = []
        raw_lines: list[str] = []
        first_delta_at: float | None = None
        current_event: str | None = None
        data_lines: list[str] = []

        def flush() -> None:
            nonlocal current_event, data_lines, first_delta_at
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
                    if first_delta_at is None:
                        first_delta_at = time.monotonic() - started
                    deltas.append(delta)
            current_event = None
            data_lines = []

        response = requests.post(
            f"{self.base}/api/conversations/{conversation_id}/messages",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            json={"content": content}, stream=True, timeout=DEFAULT_TIMEOUT,
        )
        content_type = response.headers.get("Content-Type", "")
        try:
            response.encoding = "utf-8"
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
        finally:
            response.close()

        result = {
            "httpStatus": response.status_code,
            "contentType": content_type,
            "events": events,
            "payloads": payloads,
            "answer": "".join(deltas),
            "deltaCount": len(deltas),
            "firstDeltaMs": int(first_delta_at * 1000) if first_delta_at is not None else None,
            "elapsedMs": int((time.monotonic() - started) * 1000),
        }
        (self.out_dir / f"{filename}.sse").write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
        (self.out_dir / f"{filename}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result

    def preview(self, space_id: int, conversation_id: int, question: str) -> dict[str, Any]:
        secret = os.getenv("AILS_INTERNAL_IDENTITY_SECRET", "")
        token = os.getenv("AILS_INTERNAL_TOKEN", "")
        if not secret or not token:
            raise RuntimeError("缺少 AILS_INTERNAL_TOKEN / AILS_INTERNAL_IDENTITY_SECRET")
        env = os.environ.copy()
        env["AILS_INTERNAL_IDENTITY_SECRET"] = secret
        completed = subprocess.run(
            [sys.executable, "-m", "app.internal_identity", "--user", str(self.user_id)],
            cwd=ROOT / "agent", env=env, capture_output=True, text=True, encoding="utf-8",
        )
        if completed.returncode != 0:
            raise RuntimeError(f"内部身份签发失败：{completed.stderr[-300:]}")
        identity = completed.stdout.strip()
        response = requests.post(
            f"{self.agent}/agent/context/preview",
            headers={
                "Content-Type": "application/json",
                "X-Internal-Token": token,
                "X-Internal-Identity": identity,
                "X-Internal-User-Id": str(self.user_id),
            },
            json={
                "runId": 640000 + conversation_id,
                "userId": self.user_id,
                "conversationId": conversation_id,
                "learningSpaceId": space_id,
                "userMessage": question,
                "authToken": self.token,
            },
            timeout=(10, 180),
        )
        if response.status_code != 200:
            raise RuntimeError(f"preview 失败：HTTP {response.status_code} {response.text[:400]}")
        payload = response.json()
        data = payload.get("data") or {}
        (self.out_dir / f"preview-{conversation_id}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return data


def collection_name(prefix: str, model: str, dimension: int) -> str:
    safe_model = re.sub(r"[^a-zA-Z0-9_-]+", "_", model).strip("_") or "model"
    safe_prefix = re.sub(r"[^a-zA-Z0-9_-]+", "_", prefix).strip("_") or "ails_documents"
    return f"{safe_prefix}_{safe_model}_{dimension}"[:255]


def qdrant_points(qdrant: str, collection: str, document_id: int) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    offset = None
    while True:
        body: dict[str, Any] = {
            "limit": 1000,
            "with_payload": True,
            "with_vector": False,
            "filter": {"must": [{"key": "document_id", "match": {"value": document_id}}]},
        }
        if offset is not None:
            body["offset"] = offset
        response = requests.post(
            f"{qdrant}/collections/{collection}/points/scroll", json=body, timeout=30
        )
        response.raise_for_status()
        result = response.json()["result"]
        points.extend(result.get("points") or [])
        offset = result.get("next_page_offset")
        if offset is None:
            break
    return points


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.getenv("BASE", "http://127.0.0.1:8081"))
    parser.add_argument("--agent", default=os.getenv("AGENT", "http://127.0.0.1:8000"))
    parser.add_argument("--qdrant", default=os.getenv("QDRANT", "http://127.0.0.1:6333"))
    parser.add_argument("--prefix", default=os.getenv("AILS_QDRANT_COLLECTION_PREFIX", "ails_phase54"))
    parser.add_argument("--embedding-model", default=os.getenv("AILS_EMBEDDING_MODEL", "text-embedding-v4"))
    parser.add_argument("--embedding-dimension", type=int, default=int(os.getenv("AILS_EMBEDDING_DIMENSION", "1024")))
    args = parser.parse_args()

    run = Run(args.base, args.agent, args.qdrant)
    print(f"产物目录：{run.out_dir}", flush=True)
    collection = collection_name(args.prefix, args.embedding_model, args.embedding_dimension)

    run.section("0. 前置")
    probes = (
        ("Spring", lambda: requests.post(f"{run.base}/api/auth/login", json={}, timeout=5)),
        ("Agent", lambda: requests.get(f"{run.agent}/health", timeout=5)),
        ("Qdrant", lambda: requests.get(f"{run.qdrant}/healthz", timeout=5)),
    )
    for name, probe in probes:
        try:
            response = probe()
            ok = response.status_code < 500
            run.check(f"{name} 可连接", ok, f"HTTP {response.status_code}")
            if not ok:
                return 1
        except Exception as exc:
            run.check(f"{name} 可连接", False, f"{type(exc).__name__}: {exc}")
            return 1

    run.register()
    space = run.api_ok("POST", "/api/spaces", {"name": f"Phase54 RAG {STAMP}", "description": "真实 Qdrant 引用"})
    space_id = int(space["id"])
    conv = run.api_ok("POST", f"/api/spaces/{space_id}/conversations", {"title": "RAG 引用"})
    conversation_id = int(conv["id"])
    run.check("注册 / 空间 / 会话创建", bool(run.token and space_id and conversation_id),
              f"user={run.user_id} space={space_id} conv={conversation_id}")

    run.section("1. 上传与索引")
    target = run.upload(space_id, TARGET_FILE, TARGET_TEXT)
    legacy = run.upload(space_id, LEGACY_FILE, LEGACY_TEXT)
    target_id, legacy_id = int(target["id"]), int(legacy["id"])
    try:
        documents = run.wait_ready(space_id, {target_id, legacy_id})
    except TimeoutError as exc:
        run.check("两篇合成文档 READY", False, str(exc)[:400])
        return 1
    by_id = {int(d["id"]): d for d in documents}
    run.check("两篇合成文档 READY", by_id[target_id]["status"] == "READY" == by_id[legacy_id]["status"],
              f"target={by_id[target_id].get('chunkCount')} legacy={by_id[legacy_id].get('chunkCount')}")

    info = requests.get(f"{run.qdrant}/collections/{collection}", timeout=30)
    run.check("Qdrant collection 存在", info.status_code == 200, f"HTTP {info.status_code} {collection}")
    if info.status_code != 200:
        return 1
    vectors = info.json()["result"]["config"]["params"]["vectors"]
    if isinstance(vectors, dict) and "size" not in vectors:
        vectors = next(iter(vectors.values()))
    run.check("collection 维度与 Embedding 一致", int(vectors["size"]) == args.embedding_dimension,
              f"{vectors['size']} vs {args.embedding_dimension}")

    for role, doc_id, filename in (("target", target_id, TARGET_FILE), ("legacy", legacy_id, LEGACY_FILE)):
        points = qdrant_points(run.qdrant, collection, doc_id)
        payloads = [p.get("payload") or {} for p in points]
        expected_chunks = int(by_id[doc_id].get("chunkCount") or 0)
        run.check(f"{role} 文档 Qdrant point 数与 chunkCount 一致",
                  len(points) == expected_chunks and expected_chunks > 0,
                  f"points={len(points)} chunks={expected_chunks}")
        run.check(f"{role} 文档 payload 归属正确",
                  all(p.get("document_id") == doc_id and p.get("file_name") == filename
                      and p.get("user_id") == run.user_id and p.get("space_id") == space_id
                      for p in payloads),
                  f"{len(payloads)} points")
        run.check(f"{role} 文档 payload 有正文",
                  all(isinstance(p.get("content"), str) and p["content"] for p in payloads),
                  "")

    allowed_labels = {
        f"资料《{p.get('file_name')}》#{p.get('chunk_index')}"
        for doc_id in (target_id, legacy_id)
        for p in (qp.get("payload") or {} for qp in qdrant_points(run.qdrant, collection, doc_id))
    }

    run.section("2. 当前值：检索与引用")
    current_question = "ORBIT-4721 当前调度器的重试窗口是多少秒？只依据上传资料回答，并标注资料出处。"
    preview = run.preview(space_id, conversation_id, current_question)
    context = preview.get("context") or {}
    sections = context.get("sections") or []
    knowledge_sections = [s for s in sections if str(s.get("name", "")).startswith("knowledge:")]
    run.check("preview 知识状态为 SUCCESS*", str(context.get("knowledgeStatus", "")).startswith("SUCCESS"),
              str(context.get("knowledgeStatus")))
    run.check("preview 注入了知识片段", bool(context.get("knowledgeInjectedChunkIds")) and bool(knowledge_sections),
              f"ids={context.get('knowledgeInjectedChunkIds')} sections={[s.get('name') for s in sections]}")
    run.check("preview 注入现行文档标记",
              any("P4-TARGET-7K2" in (str(s.get("preview", "")) + str(s.get("content", "")))
                  for s in knowledge_sections), "")

    current = run.chat(conversation_id, current_question, "current")
    labels = LABEL_RE.findall(current["answer"])
    target_labels = [f"资料《{f}》#{i}" for f, i in labels if f == TARGET_FILE]
    run.check("当前值 SSE 正常结束", current["httpStatus"] == 200
              and current["events"][:1] == ["message.start"] and current["events"][-1:] == ["message.end"]
              and "message.error" not in current["events"], f"{current['httpStatus']} / {len(current['events'])} events")
    run.check("当前值回答包含 17 秒", "17" in current["answer"], f"{len(current['answer'])} chars")
    run.check("当前值引用了现行文档", bool(target_labels), f"labels={labels[:5]}")
    run.check("当前值引用标签都能回跳到 Qdrant point", all(f"资料《{f}》#{i}" in allowed_labels for f, i in labels),
              f"labels={labels[:5]}")
    # 回答可以为了对比提到旧值，但必须把它框定为已下线 / 历史值，不能当成现行值。
    mentions_legacy = "12" in current["answer"]
    framed_as_legacy = any(word in current["answer"] for word in ("旧", "历史", "下线", "不适用"))
    run.check("当前值没有把 12 秒当成现行值", (not mentions_legacy) or framed_as_legacy,
              f"mentionsLegacy={mentions_legacy} framed={framed_as_legacy}")

    run.section("3. 历史值：版本区分")
    legacy_question = "ORBIT-4721 在 2024 年及以前的旧调度器使用多少秒的重试窗口？请引用资料。"
    legacy_result = run.chat(conversation_id, legacy_question, "legacy")
    legacy_labels = LABEL_RE.findall(legacy_result["answer"])
    run.check("历史值 SSE 正常结束", legacy_result["httpStatus"] == 200
              and legacy_result["events"][:1] == ["message.start"] and legacy_result["events"][-1:] == ["message.end"],
              f"{legacy_result['httpStatus']} / {len(legacy_result['events'])} events")
    run.check("历史值回答包含 12 秒", "12" in legacy_result["answer"], f"{len(legacy_result['answer'])} chars")
    run.check("历史值引用了历史文档", any(f == LEGACY_FILE for f, _ in legacy_labels),
              f"labels={legacy_labels[:5]}")
    run.check("历史值引用标签都能回跳", all(f"资料《{f}》#{i}" in allowed_labels for f, i in legacy_labels),
              f"labels={legacy_labels[:5]}")

    run.section("4. 无关问题：不伪造引用")
    unrelated_question = "镓的熔点是多少？如果上传资料里没有，请直接说明没有，不要编造。"
    unrelated = run.chat(conversation_id, unrelated_question, "unrelated")
    unrelated_labels = LABEL_RE.findall(unrelated["answer"])
    run.check("无关问题 SSE 正常结束", unrelated["httpStatus"] == 200
              and unrelated["events"][:1] == ["message.start"] and unrelated["events"][-1:] == ["message.end"],
              f"{unrelated['httpStatus']} / {len(unrelated['events'])} events")
    run.check("无关问题没有资料引用标签", not unrelated_labels, f"labels={unrelated_labels[:5]}")
    run.check("无关问题明确说明资料不足", any(word in unrelated["answer"] for word in ("没有", "未", "无法", "资料")),
              f"{len(unrelated['answer'])} chars")

    passed = sum(1 for c in run.checks if c["ok"])
    failed = len(run.checks) - passed
    summary = {
        "stamp": STAMP, "base": run.base, "agent": run.agent, "qdrant": run.qdrant,
        "collection": collection, "userId": run.user_id, "spaceId": space_id,
        "conversationId": conversation_id,
        "documents": {"target": target, "legacy": legacy},
        "pass": passed, "fail": failed, "checks": run.checks,
        "answers": {"current": current, "legacy": legacy_result, "unrelated": unrelated},
    }
    (run.out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果：PASS={passed} FAIL={failed}")
    print(f"产物目录：{run.out_dir}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
