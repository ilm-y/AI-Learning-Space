"""测试共用的固定装置。

原则：**测试不依赖 MySQL、不依赖后端服务、不依赖 API Key。**
所以默认走 Fixture 检索源 + Mock LLM，跑一次 pytest 只需要几百毫秒。
真实 LLM 的行为不进单测 —— 那属于离线评测集（agent-architecture.md 第四十九节）的事。
"""

from __future__ import annotations

import pytest

from app.config import Settings


@pytest.fixture
def settings() -> Settings:
    """强制 Fixture + Mock，并屏蔽开发机上的 .env，保证结果可复现。"""
    return Settings(
        _env_file=None,
        retriever_mode="fixture",
        llm_provider="mock",
        max_context_tokens=3000,
        recent_history_limit=12,
    )
