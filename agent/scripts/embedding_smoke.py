#!/usr/bin/env python3
"""真实 Embedding Provider + Qdrant 的开发验证脚本（不是自动化测试）。

用途：

1. 用同一个真实 Embedding 模型生成两份不同主题文档的向量；
2. 为其中一份文档生成问题向量（单条输入）；
3. 校验数量、维度、数值有效性；
4. 写入一个**专用临时 Qdrant collection**，执行相似度查询；
5. 验证相关主题排在无关主题之前。

安全约束：

* 只从环境变量 / `agent/.env` 读取配置，不硬编码、不打印 API Key；
* collection 前缀带随机后缀，绝不使用现有文档 collection；
* 结束时删除本次创建的 collection，不删除任何已有数据；
* 真实调用失败时直接失败，不回退到 Hash Embedding。

运行示例（百炼 text-embedding-v4，北京区域）：

```bash
cd agent
export AILS_EMBEDDING_PROVIDER=openai
export AILS_EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
export AILS_EMBEDDING_MODEL=text-embedding-v4
export AILS_EMBEDDING_DIMENSION=1024
export AILS_EMBEDDING_API_KEY=...   # 只放在本机环境，不要提交
export AILS_QDRANT_URL=http://127.0.0.1:6333
python scripts/embedding_smoke.py
```

如果只想做离线检查，运行现有单测：

```bash
python -m pytest tests/test_embedding.py -q
```
"""

from __future__ import annotations

import asyncio
import math
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

# 允许直接执行 `python scripts/embedding_smoke.py`。
AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from qdrant_client import AsyncQdrantClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.embedding import build_embedding_provider  # noqa: E402
from app.vectorstore.qdrant_store import (  # noqa: E402
    ChunkVector,
    QdrantDocumentStore,
    collection_name_for,
)

DOC_A_ID = 900_001
DOC_B_ID = 900_002
DOC_A = (
    "光合作用是绿色植物利用光能，把二氧化碳和水转化为有机物并释放氧气的过程。"
    "叶绿体是进行光合作用的主要场所。"
)
DOC_B = (
    "二分查找在已排序数组中反复比较中间元素，把搜索区间缩小一半。"
    "它的时间复杂度是 O(log n)。"
)
QUERY = "植物怎样利用阳光制造有机物？"


def _is_loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _sanitize(message: str, settings: Settings) -> str:
    for secret in (settings.embedding_api_key, settings.qdrant_api_key):
        if secret:
            message = message.replace(secret, "***")
    return message


async def main() -> int:
    settings = Settings()
    provider_name = settings.embedding_provider.strip().lower()

    print(
        "config: provider={} base_url={} model={} dimension={} batch_size={}".format(
            settings.embedding_provider,
            settings.embedding_base_url,
            settings.embedding_model,
            settings.embedding_dimension,
            settings.embedding_batch_size,
        )
    )
    print(f"config: qdrant_url={settings.qdrant_url}")

    if provider_name not in {"openai", "openai-compatible"}:
        print(
            "FAILED: smoke test 必须使用真实 Embedding Provider；"
            "当前 AILS_EMBEDDING_PROVIDER 不是 openai/openai-compatible。"
        )
        return 2
    if not settings.embedding_api_key and not _is_loopback(settings.embedding_base_url):
        print(
            "FAILED: 缺少 AILS_EMBEDDING_API_KEY。请在本机环境或 agent/.env 中配置后重试；"
            "本轮不会发起未认证的真实请求。"
        )
        return 2

    provider = build_embedding_provider(settings)
    prefix = f"ails_embedding_smoke_{uuid.uuid4().hex[:10]}"
    collection = collection_name_for(prefix, provider.model, provider.dimension)
    store = QdrantDocumentStore(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout_seconds=settings.qdrant_timeout_seconds,
        collection_prefix=prefix,
        upsert_batch_size=settings.qdrant_upsert_batch_size,
    )
    qdrant = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key or None,
        timeout=max(1, int(settings.qdrant_timeout_seconds)),
        prefer_grpc=False,
        check_compatibility=False,
    )
    created = False

    try:
        if await qdrant.collection_exists(collection):
            raise RuntimeError(
                f"临时 collection 已存在，拒绝覆盖：{collection}"
            )

        document_vectors = await provider.embed([DOC_A, DOC_B])
        query_vector = (await provider.embed([QUERY]))[0]

        if len(document_vectors) != 2:
            raise RuntimeError(
                f"文档向量数量错误：期望 2，实际 {len(document_vectors)}"
            )
        if len(query_vector) != provider.dimension:
            raise RuntimeError(
                f"查询向量维度错误：期望 {provider.dimension}，实际 {len(query_vector)}"
            )
        for index, vector in enumerate(document_vectors):
            if len(vector) != provider.dimension:
                raise RuntimeError(
                    f"文档 {index} 维度错误：期望 {provider.dimension}，实际 {len(vector)}"
                )
            if not all(math.isfinite(value) for value in vector):
                raise RuntimeError(f"文档 {index} 包含 NaN/Infinity")
        if not all(math.isfinite(value) for value in query_vector):
            raise RuntimeError("查询向量包含 NaN/Infinity")

        collection = await store.ensure_collection(
            model=provider.model,
            dimension=provider.dimension,
        )
        created = True
        await store.index_document(
            collection_name=collection,
            document_id=DOC_A_ID,
            user_id=1,
            space_id=1,
            document_name="光合作用.md",
            file_type="md",
            chunks=[ChunkVector(1, 0, DOC_A, document_vectors[0])],
        )
        await store.index_document(
            collection_name=collection,
            document_id=DOC_B_ID,
            user_id=1,
            space_id=1,
            document_name="二分查找.md",
            file_type="md",
            chunks=[ChunkVector(2, 0, DOC_B, document_vectors[1])],
        )

        result = await qdrant.query_points(
            collection_name=collection,
            query=query_vector,
            limit=2,
            with_payload=True,
        )
        points = result.points
        if len(points) != 2:
            raise RuntimeError(f"检索结果数量错误：期望 2，实际 {len(points)}")

        top_id = points[0].payload.get("document_id")
        second_id = points[1].payload.get("document_id")
        if top_id != DOC_A_ID:
            raise RuntimeError(
                f"相关主题未排在第一位：top document_id={top_id}，期望 {DOC_A_ID}"
            )
        if second_id != DOC_B_ID:
            raise RuntimeError(
                f"检索结果标签异常：second document_id={second_id}，期望 {DOC_B_ID}"
            )
        if points[0].score is None or points[1].score is None:
            raise RuntimeError("Qdrant 未返回相似度分数")
        if points[0].score <= points[1].score:
            raise RuntimeError(
                "相关主题分数没有高于无关主题："
                f"{points[0].score} <= {points[1].score}"
            )

        print(f"PASS: provider={provider.name} model={provider.model} "
              f"dimension={provider.dimension}")
        print(f"PASS: collection={collection}")
        print("PASS: document_vectors=2 query_vectors=1 finite=true")
        print(
            "PASS: retrieval top={} score={:.6f} second={} score={:.6f} margin={:.6f}".format(
                top_id,
                points[0].score,
                second_id,
                points[1].score,
                points[0].score - points[1].score,
            )
        )
        return 0
    except Exception as exc:  # noqa: BLE001 —— 开发脚本需要输出脱敏的失败摘要
        print(f"FAILED: {type(exc).__name__}: {_sanitize(str(exc), settings)}")
        return 1
    finally:
        if created:
            try:
                await qdrant.delete_collection(collection)
                print(f"cleanup: deleted temporary collection={collection}")
            except Exception as exc:  # noqa: BLE001
                print(
                    "WARNING: 删除临时 collection 失败："
                    f"{type(exc).__name__}: {_sanitize(str(exc), settings)}"
                )
        await qdrant.close()
        await store.aclose()
        await provider.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
