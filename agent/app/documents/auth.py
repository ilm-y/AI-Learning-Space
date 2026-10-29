"""内部文档接口鉴权。

实现已抽到 `app.internal_auth`，供 chat / analyze / preview / diagnostics 共用。
保留这个模块是为了不破坏现有 `from app.documents.auth import require_internal_user`
的调用点。
"""

from __future__ import annotations

from app.internal_auth import require_internal_user

__all__ = ["require_internal_user"]
