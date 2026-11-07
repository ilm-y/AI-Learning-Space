"""OpenAI 兼容 `/embeddings` HTTP Provider。

只做一件事：把文本批量发给配置的服务并严格校验响应。
网络错误、HTTP 错误、返回数量/维度/数值异常都会显式失败；
绝不在这里改成 Hash，也绝不截断或补零。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from urllib.parse import urlsplit

import httpx

from app.embedding.base import EmbeddingProvider, embedding_fingerprint, validate_vectors
from app.documents.errors import DocumentEmbeddingError, DocumentEmbeddingUnavailable

log = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimension: int,
        timeout_seconds: float,
        max_retries: int,
        retry_backoff_seconds: float = 1.0,
        batch_size: int = 10,
    ) -> None:
        if not base_url.strip():
            raise ValueError("embedding base_url 不能为空")
        if not model.strip():
            raise ValueError("embedding model 不能为空")
        if dimension <= 0:
            raise ValueError("embedding dimension 必须为正数")
        if max_retries < 0:
            raise ValueError("embedding max_retries 不能为负数")
        if batch_size <= 0:
            raise ValueError("embedding batch_size 必须为正数")

        self.model = model.strip()
        self.dimension = dimension
        self._endpoint_identity = _endpoint_identity(base_url)
        self._url = f"{base_url.rstrip('/')}/embeddings"
        self._api_key = api_key.strip()
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff_seconds
        self._batch_size = batch_size
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
        )

    @property
    def fingerprint(self) -> str:
        return embedding_fingerprint(
            provider=self.name,
            model=self.model,
            dimension=self.dimension,
            endpoint_identity=self._endpoint_identity,
        )

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        # 不同兼容服务的单请求批量上限不同（百炼 v4 为 10），按配置分批，
        # 并保持与输入完全相同的顺序。
        all_texts = list(texts)
        vectors: list[list[float]] = []
        for start in range(0, len(all_texts), self._batch_size):
            batch = all_texts[start : start + self._batch_size]
            payload = {"model": self.model, "input": batch}
            response = await self._post_with_retry(payload, headers)
            vectors.extend(
                self._parse_response(response, expected_count=len(batch))
            )
        return vectors

    async def _post_with_retry(
        self, payload: dict[str, object], headers: dict[str, str]
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(self._url, json=payload, headers=headers)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
                if attempt < self._max_retries:
                    await self._backoff(attempt)
                    continue
                raise DocumentEmbeddingUnavailable("无法连接 Embedding 服务") from exc
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self._max_retries:
                    await self._backoff(attempt)
                    continue
                raise DocumentEmbeddingUnavailable("Embedding 请求失败") from exc

            if response.status_code in _RETRYABLE_STATUS and attempt < self._max_retries:
                log.warning(
                    "Embedding 服务返回 %s，第 %s 次重试",
                    response.status_code,
                    attempt + 1,
                )
                await self._backoff(attempt)
                continue
            if response.status_code >= 400:
                raise DocumentEmbeddingError(
                    "Embedding 服务失败（HTTP "
                    f"{response.status_code}）：{response.text[:300]}"
                )
            return response

        raise DocumentEmbeddingUnavailable(f"Embedding 请求失败：{last_error}")

    def _parse_response(self, response: httpx.Response, *, expected_count: int) -> list[list[float]]:
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("响应必须为 JSON 对象")
            returned_model = body.get("model")
            if returned_model is not None:
                if not isinstance(returned_model, str) or not returned_model.strip():
                    raise ValueError("model 字段非法")
                if returned_model.strip() != self.model:
                    raise ValueError(
                        f"响应 model 不匹配：期望 {self.model}，实际 {returned_model}"
                    )
            data = body["data"]
            if not isinstance(data, list) or len(data) != expected_count:
                raise ValueError("data 数量不匹配")
            ordered = sorted(data, key=lambda item: int(item["index"]))
            indexes = [int(item["index"]) for item in ordered]
            if indexes != list(range(expected_count)):
                raise ValueError("index 必须从 0 连续递增")
            vectors = [item["embedding"] for item in ordered]
        except Exception as exc:
            raise DocumentEmbeddingError("Embedding 响应不符合 OpenAI /embeddings 契约") from exc

        return validate_vectors(
            vectors,
            expected_count=expected_count,
            dimension=self.dimension,
            provider=self.name,
        )

    async def _backoff(self, attempt: int) -> None:
        await asyncio.sleep(self._retry_backoff * (2**attempt))

    async def aclose(self) -> None:
        await self._client.aclose()


def _endpoint_identity(base_url: str) -> str:
    """规范化 endpoint 身份，忽略 query、用户名/密码与末尾斜杠。"""
    parts = urlsplit(base_url.strip())
    if parts.scheme and parts.netloc:
        return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"
    return base_url.strip().rstrip("/")
