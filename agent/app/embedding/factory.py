"""按配置构建 Embedding Provider。"""

from __future__ import annotations

from app.config import Settings
from app.embedding.base import EmbeddingProvider
from app.embedding.hash_provider import HashEmbeddingProvider
from app.embedding.openai_compatible import OpenAICompatibleEmbeddingProvider


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    provider = settings.embedding_provider.strip().lower()

    if provider == "hash":
        # Hash 的模型名固定，避免用户误配成真实模型名后与语义 collection 混用。
        return HashEmbeddingProvider(dimension=settings.embedding_dimension)

    if provider in {"openai", "openai-compatible"}:
        # api_key 允许为空：本机 OpenAI 兼容服务可能不校验鉴权。
        return OpenAICompatibleEmbeddingProvider(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
            timeout_seconds=settings.embedding_timeout_seconds,
            max_retries=settings.embedding_max_retries,
            retry_backoff_seconds=settings.embedding_retry_backoff_seconds,
            batch_size=settings.embedding_batch_size,
        )

    raise ValueError(
        f"未知的 Embedding provider: {settings.embedding_provider}（支持 hash / openai）"
    )
