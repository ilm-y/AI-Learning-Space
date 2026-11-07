"""Embedding Provider 抽象与返回值校验。"""

from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.documents.errors import DocumentEmbeddingError

FINGERPRINT_PREFIX = "v1"


def embedding_fingerprint(
    *,
    provider: str,
    model: str,
    dimension: int,
    endpoint_identity: str = "",
) -> str:
    """Embedding 配置指纹：provider + model + dimension + endpoint 身份。

    只包含「换一个会导致向量空间不兼容」的字段，绝不包含 API key。
    endpoint 对 Hash 为空；对 OpenAI 兼容服务使用规范化后的
    scheme + host + path（不含 query / 凭据），同一个网关部署得到同一指纹。
    """
    raw = f"{provider.strip()}|{model.strip()}|{int(dimension)}|{endpoint_identity.strip()}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{FINGERPRINT_PREFIX}:{digest}"


class EmbeddingProvider(ABC):
    """可替换的文本向量化实现。

    实现不得在失败时静默换成另一种算法：调用方依赖 `name` / `model` / `fingerprint`
    区分 Hash 与真实语义模型，混用会污染 Qdrant collection。
    """

    name: str
    model: str
    dimension: int

    @property
    def fingerprint(self) -> str:
        """当前配置的稳定指纹；不同向量空间必须不同。"""
        return embedding_fingerprint(
            provider=self.name,
            model=self.model,
            dimension=self.dimension,
        )

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
