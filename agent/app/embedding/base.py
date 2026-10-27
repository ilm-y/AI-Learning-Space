"""Embedding Provider 抽象与返回值校验。"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.documents.errors import DocumentEmbeddingError


class EmbeddingProvider(ABC):
    """可替换的文本向量化实现。

    实现不得在失败时静默换成另一种算法：调用方依赖 `name` / `model` 区分
    Hash 与真实语义模型，混用会污染 Qdrant collection。
    """

    name: str
    model: str
    dimension: int

    @abstractmethod
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """返回与输入等长、顺序一致的向量列表。"""

    async def aclose(self) -> None:
        """释放连接等资源；无资源时可以不覆盖。"""
        return None


def validate_vectors(
    vectors: Sequence[Sequence[float]] | None,
    *,
    expected_count: int,
    dimension: int,
    provider: str,
) -> list[list[float]]:
    """校验数量、维度与数值；任何异常都显式抛出，绝不补零 / 截断。"""
    if vectors is None:
        raise DocumentEmbeddingError(f"{provider} Embedding 返回空结果")
    if len(vectors) != expected_count:
        raise DocumentEmbeddingError(
            f"{provider} Embedding 数量不匹配：期望 {expected_count}，实际 {len(vectors)}"
        )

    validated: list[list[float]] = []
    for index, vector in enumerate(vectors):
        if len(vector) != dimension:
            raise DocumentEmbeddingError(
                f"{provider} Embedding 维度不匹配：第 {index} 条期望 {dimension}，"
                f"实际 {len(vector)}"
            )
        converted: list[float] = []
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise DocumentEmbeddingError(
                    f"{provider} Embedding 第 {index} 条包含非数值"
                )
            number = float(value)
            if not math.isfinite(number):
                raise DocumentEmbeddingError(
                    f"{provider} Embedding 第 {index} 条包含 NaN/Infinity"
                )
            converted.append(number)
        validated.append(converted)
    return validated
