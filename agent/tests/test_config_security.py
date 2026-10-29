"""配置层的 fail-closed 守卫：Agent 不得持有后端委托签发密钥。

这些测试只验证“运行配置里出现该变量时启动失败”，不把它描述成对拥有
改代码/改启动参数权限者的防护，也不测试身份协议本身。
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.config import Settings

SECRET = f"phase4-test-only-{uuid.uuid4().hex}"


def test_absent_delegation_secret_still_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AILS_INTERNAL_DELEGATION_SECRET", raising=False)

    settings = Settings(_env_file=None)

    assert settings.internal_delegation_secret == ""


def test_injected_delegation_secret_fails_closed_without_echoing_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AILS_INTERNAL_DELEGATION_SECRET", SECRET)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    message = str(excinfo.value)
    assert "AILS_INTERNAL_DELEGATION_SECRET" in message
    assert SECRET not in message


def test_blank_delegation_secret_is_not_usable_and_does_not_block_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AILS_INTERNAL_DELEGATION_SECRET", "   ")

    settings = Settings(_env_file=None)

    assert settings.internal_delegation_secret == ""


def test_delegation_secret_in_dotenv_fails_closed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AILS_INTERNAL_DELEGATION_SECRET", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"AILS_INTERNAL_DELEGATION_SECRET={SECRET}\n", encoding="utf-8"
    )

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=str(env_file))

    assert SECRET not in str(excinfo.value)


def test_delegation_secret_passed_as_kwarg_fails_closed() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, internal_delegation_secret=SECRET)

    assert SECRET not in str(excinfo.value)


def test_diagnostics_description_does_not_expose_guard_field() -> None:
    settings = Settings(_env_file=None)

    described = settings.describe()

    assert "internal_delegation_secret" not in described
    assert not any("DELEGATION_SECRET" in str(value) for value in described.values())
