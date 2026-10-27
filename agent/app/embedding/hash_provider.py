"""确定性 Hash Embedding，仅用于离线开发与单元测试。

它不是语义模型：只能保证「同一文本得到同一向量」，不能保证语义相近。
因此模型名固定为 `hash-v1`，collection 也会带上该名字，避免和真实模型混用。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence

from app.embedding.base import EmbeddingProvider, validate_vectors

MODEL_NAME = "hash-v1"


class HashEmbeddingProvider(EmbeddingProvider):
    name = "hash"
    model = MODEL_NAME

    def __init__(self, dimension: int = 256) -> None:
        if dimension <= 0:
            raise ValueError("Hash Embedding dimension 必须为正数")
        self.dimension = dimension

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = [self._vector(text) for text in texts]
        return validate_vectors(
            vectors,
            expected_count=len(texts),
            dimension=self.dimension,
            provider=self.name,
        )

    def _vector(self, text: str) -> list[float]:
        seed = hashlib.sha256(f"{self.model}\x00{text}".encode("utf-8")).digest()
        values: list[float] = []
        block = 0
        while len(values) < self.dimension:
            digest = hashlib.sha256(seed + block.to_bytes(4, "big")).digest()
            for offset in range(0, len(digest), 4):
                integer = int.from_bytes(digest[offset : offset + 4], "big", signed=False)
                values.append((integer / 2**31) - 1.0)
                if len(values) == self.dimension:
                    break
            block += 1

        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0:
            # 理论上不可达；保底给一个确定的单位向量，仍然不引入随机性。
            values[0] = 1.0
            return values
        return [value / norm for value in values]
