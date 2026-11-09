"""Phase 5 pre-Step3：Memory Collection 隔离最小验证。

目的不是验证语义检索质量，而是证明：
1. 测试环境可通过 `AILS_MEMORY_COLLECTION_PREFIX` 指向独立 collection；
2. 写入只进隔离 collection，默认 collection 未被写入；
3. 查询只读隔离 collection（同 user 命中 1 条，换 user 命中 0 条），默认 collection 未被读取/创建；
4. 删除只删隔离 collection，删除后 404，且无 `phase5-prestep3-*` 残留；
5. 默认/共享 memory collection 在写入、查询、删除前后均未变化。

成本边界（本脚本严格自限）：
- 不调用 LLM / Embedding / Tavily；
- Qdrant 数据点写入 1 次、查询 1 次、删除 1 次（删除 = 删除整块临时 collection）；
- collection 创建属于元数据操作，不计入数据点写入；
- 只读取默认 collection 的元信息/计数，不写入、不删除；
- 结束后删除临时 collection 并复查 404。

不输出任何密钥；Qdrant API Key 只从 Settings 读取，不打印。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "agent"))

from app.config import Settings  # noqa: E402
from app.vectorstore.memory_store import (  # noqa: E402
    QdrantMemoryStore,
    point_id_for_memory,
)
from app.vectorstore.qdrant_store import collection_name_for  # noqa: E402

MODEL = "text-embedding-v4"
DIMENSION = 1024
FINGERPRINT = "phase5-prestep3-isolation-proof"
USER_ID = 990001
SPACE_ID = 990001
CONCEPT_ID = 990001
MEMORY_ID = 990001


def _collection_state(base: str, name: str) -> dict[str, object]:
    response = requests.get(f"{base}/collections/{name}", timeout=5)
    if response.status_code == 404:
        return {"exists": False, "httpStatus": 404}
    response.raise_for_status()
    info = response.json().get("result", {})
    return {
        "exists": True,
        "httpStatus": response.status_code,
        "pointsCount": info.get("points_count"),
    }


def _point_count(base: str, name: str) -> int | None:
    """精确点数；collection 不存在时返回 None。"""
    response = requests.post(
        f"{base}/collections/{name}/points/count",
        json={"exact": True},
        timeout=10,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json().get("result", {}).get("count")


def _list_collections(base: str, prefix: str) -> list[str]:
    response = requests.get(f"{base}/collections", timeout=10)
    response.raise_for_status()
    names = response.json().get("result", {}).get("collections", [])
    return sorted(
        str(item.get("name"))
        for item in names
        if str(item.get("name", "")).startswith(prefix)
    )


def _same_state(before: dict[str, object], after: dict[str, object]) -> bool:
    return (
        before.get("exists") == after.get("exists")
        and before.get("pointsCount") == after.get("pointsCount")
        and before.get("httpStatus") == after.get("httpStatus")
    )


def _create_collection(base: str, name: str) -> int:
    response = requests.put(
        f"{base}/collections/{name}",
        json={"vectors": {"size": DIMENSION, "distance": "Cosine"}},
        timeout=30,
    )
    return response.status_code


def _delete_collection(base: str, name: str) -> dict[str, object]:
    response = requests.delete(f"{base}/collections/{name}", timeout=30)
    check = requests.get(f"{base}/collections/{name}", timeout=5)
    return {
        "deleteHttp": response.status_code,
        "goneAfterDelete": check.status_code == 404,
    }


async def _prove(settings: Settings) -> dict[str, object]:
    base = settings.qdrant_url.rstrip("/")
    default_name = collection_name_for(
        settings.memory_collection_prefix, MODEL, DIMENSION
    )
    stamp = time.strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
    isolated_prefix = f"phase5-prestep3-{stamp}-memory"
    isolated_name = collection_name_for(isolated_prefix, MODEL, DIMENSION)

    result: dict[str, object] = {
        "defaultPrefix": settings.memory_collection_prefix,
        "defaultCollection": default_name,
        "isolatedPrefix": isolated_prefix,
        "isolatedCollection": isolated_name,
        "namesDiffer": isolated_name != default_name,
        "notSharedDefaultName": isolated_name != default_name,
        "defaultBefore": _collection_state(base, default_name),
        "residualBefore": _list_collections(base, "phase5-prestep3-"),
        "createHttp": _create_collection(base, isolated_name),
        "upserts": 0,
        "queries": 0,
        "deletes": 0,
    }

    store = QdrantMemoryStore(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout_seconds=settings.qdrant_timeout_seconds,
        collection_prefix=isolated_prefix,
    )
    try:
        vector = [0.0] * DIMENSION
        vector[0] = 1.0
        payload = {
            "memory_id": MEMORY_ID,
            "user_id": USER_ID,
            "status": "ACTIVE",
            "scope_type": "CONCEPT",
            "space_id": SPACE_ID,
            "concept_id": CONCEPT_ID,
            "memory_type": "LEARNING",
            "importance": 0.5,
            "content_hash": "prestep3-proof",
            "memory_version": 1,
            "embedding_fingerprint": FINGERPRINT,
        }
        await store.upsert_memory(
            collection_name=isolated_name,
            point_id=point_id_for_memory(MEMORY_ID),
            vector=vector,
            payload=payload,
        )
        result["upserts"] = 1
        # 写入只进隔离 collection：隔离 collection 恰好 1 个点，默认 collection 未变。
        result["isolatedPointsAfterWrite"] = _point_count(base, isolated_name)
        result["defaultAfterWrite"] = _collection_state(base, default_name)

        hits, warnings = await store.search(
            query_vector=vector,
            user_id=USER_ID,
            space_id=SPACE_ID,
            concept_id=CONCEPT_ID,
            top_k=1,
            model=MODEL,
            dimension=DIMENSION,
            embedding_fingerprint=FINGERPRINT,
        )
        result["queries"] = 1
        result["searchWarnings"] = warnings
        result["hitCount"] = len(hits)
        result["hitMemoryIds"] = [hit.memory_id for hit in hits]

        # 查询只读隔离 collection：换一个 user_id 必须命中 0 条。
        mismatch_hits, mismatch_warnings = await store.search(
            query_vector=vector,
            user_id=USER_ID + 1,
            space_id=SPACE_ID,
            concept_id=CONCEPT_ID,
            top_k=1,
            model=MODEL,
            dimension=DIMENSION,
            embedding_fingerprint=FINGERPRINT,
        )
        result["mismatchedUserHitCount"] = len(mismatch_hits)
        result["mismatchedUserWarnings"] = mismatch_warnings
        # 查询完成后、删除隔离 collection 前，再次只读默认 collection。
        result["defaultAfterQuery"] = _collection_state(base, default_name)
    finally:
        await store.aclose()
        result["cleanup"] = _delete_collection(base, isolated_name)
        result["deletes"] = 1

    default_after_delete = _collection_state(base, default_name)
    result["defaultAfterDelete"] = default_after_delete
    result["defaultAfter"] = default_after_delete
    result["defaultUnchanged"] = all(
        _same_state(result["defaultBefore"], state)
        for state in (
            result["defaultAfterWrite"],
            result["defaultAfterQuery"],
            default_after_delete,
        )
    )
    result["isolatedGoneAfterDelete"] = _point_count(base, isolated_name) is None
    result["residualAfter"] = _list_collections(base, "phase5-prestep3-")
    result["phase5PilotResidual"] = _list_collections(base, "phase5-pilot-")
    result["noResidual"] = not result["residualAfter"]

    result["passed"] = bool(
        result["namesDiffer"]
        and result["createHttp"] in (200, 201)
        and result["upserts"] == 1
        and result["isolatedPointsAfterWrite"] == 1
        and result["hitCount"] == 1
        and result["hitMemoryIds"] == [MEMORY_ID]
        and result["mismatchedUserHitCount"] == 0
        and result["cleanup"]["deleteHttp"] in (200, 202, 404)
        and result["cleanup"]["goneAfterDelete"]
        and result["isolatedGoneAfterDelete"]
        and result["defaultUnchanged"]
        and result["noResidual"]
    )
    return result


def main() -> int:
    settings = Settings()
    try:
        result = asyncio.run(_prove(settings))
    except Exception as exc:  # noqa: BLE001
        result = {
            "passed": False,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
