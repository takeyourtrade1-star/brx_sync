"""Fail-closed authentication tests for /internal routes."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from app.api import internal_dependencies


@pytest.mark.asyncio
async def test_internal_token_fails_closed_when_not_configured(monkeypatch):
    monkeypatch.setattr(
        internal_dependencies,
        "get_settings",
        lambda: SimpleNamespace(INTERNAL_API_TOKEN=None),
    )
    with pytest.raises(HTTPException) as error:
        await internal_dependencies.verify_internal_token(None)
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_internal_token_rejects_wrong_value(monkeypatch):
    monkeypatch.setattr(
        internal_dependencies,
        "get_settings",
        lambda: SimpleNamespace(INTERNAL_API_TOKEN=SecretStr("correct-token")),
    )
    with pytest.raises(HTTPException) as error:
        await internal_dependencies.verify_internal_token("wrong-token")
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_internal_token_accepts_exact_value(monkeypatch):
    monkeypatch.setattr(
        internal_dependencies,
        "get_settings",
        lambda: SimpleNamespace(INTERNAL_API_TOKEN=SecretStr("correct-token")),
    )
    assert await internal_dependencies.verify_internal_token("correct-token") is None
