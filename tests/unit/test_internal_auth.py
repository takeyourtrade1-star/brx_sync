"""Fail-closed authentication tests for /internal routes."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from starlette.requests import Request

from app.api import internal_dependencies


def _request(host: str = "127.0.0.1") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/inventory/reservations",
            "headers": [],
            "client": (host, 12345),
            "server": ("sync", 8000),
            "scheme": "http",
            "query_string": b"",
        }
    )


class _FakeRedis:
    def __init__(self, initial_count: int = 0, *, fail: bool = False) -> None:
        self.count = initial_count
        self.fail = fail
        self.expirations: list[tuple[str, int]] = []

    async def incr(self, _key: str) -> int:
        if self.fail:
            raise ConnectionError("redis unavailable")
        self.count += 1
        return self.count

    async def expire(self, key: str, ttl: int) -> None:
        self.expirations.append((key, ttl))

    async def eval(self, _script: str, _keys: int, _key: str, _ttl: int) -> int:
        if self.fail:
            raise ConnectionError("redis unavailable")
        self.count += 1
        return self.count


def _settings(token: str | None = "correct-token", limit: int = 300) -> SimpleNamespace:
    return SimpleNamespace(
        INTERNAL_API_TOKEN=SecretStr(token) if token is not None else None,
        INTERNAL_CALLER_TOKENS=None,
        INTERNAL_API_TOKEN_SCOPES="inventory:write,metrics:read",
        INTERNAL_API_ALLOWED_CIDRS=(
            "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
        ),
        INTERNAL_API_RATE_LIMIT_PER_MINUTE=limit,
    )


def _set_redis(monkeypatch, value: _FakeRedis | None) -> None:
    async def fake_get_redis():
        return value

    monkeypatch.setattr(internal_dependencies, "get_redis", fake_get_redis)


@pytest.mark.asyncio
async def test_internal_token_fails_closed_when_not_configured(monkeypatch):
    monkeypatch.setattr(
        internal_dependencies,
        "get_settings",
        lambda: _settings(token=None),
    )
    with pytest.raises(HTTPException) as error:
        await internal_dependencies.verify_internal_token(_request(), None)
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_internal_token_rejects_wrong_value(monkeypatch):
    monkeypatch.setattr(
        internal_dependencies,
        "get_settings",
        _settings,
    )

    async def unexpected_redis_call():
        raise AssertionError("Redis must not be queried for an invalid token")

    monkeypatch.setattr(internal_dependencies, "get_redis", unexpected_redis_call)
    with pytest.raises(HTTPException) as error:
        await internal_dependencies.verify_internal_token(_request(), "wrong-token")
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_internal_token_accepts_exact_value(monkeypatch):
    monkeypatch.setattr(
        internal_dependencies,
        "get_settings",
        _settings,
    )
    redis = _FakeRedis()
    _set_redis(monkeypatch, redis)

    assert (
        await internal_dependencies.verify_internal_token(_request(), "correct-token") is None
    )
    assert redis.count == 1


@pytest.mark.asyncio
async def test_internal_token_fails_closed_when_rate_limit_store_is_unavailable(monkeypatch):
    monkeypatch.setattr(internal_dependencies, "get_settings", _settings)
    _set_redis(monkeypatch, None)

    with pytest.raises(HTTPException) as error:
        await internal_dependencies.verify_internal_token(_request(), "correct-token")

    assert error.value.status_code == 503
    assert error.value.detail["code"] == "INTERNAL_RATE_LIMIT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_internal_token_rejects_requests_over_the_peer_limit(monkeypatch):
    monkeypatch.setattr(
        internal_dependencies,
        "get_settings",
        lambda: _settings(limit=10),
    )
    redis = _FakeRedis(initial_count=10)
    _set_redis(monkeypatch, redis)

    with pytest.raises(HTTPException) as error:
        await internal_dependencies.verify_internal_token(
            _request("10.0.1.25"), "correct-token"
        )

    assert error.value.status_code == 429
    assert error.value.detail["code"] == "INTERNAL_RATE_LIMITED"


@pytest.mark.asyncio
async def test_internal_token_fails_closed_on_rate_limit_redis_error(monkeypatch):
    monkeypatch.setattr(internal_dependencies, "get_settings", _settings)
    _set_redis(monkeypatch, _FakeRedis(fail=True))

    with pytest.raises(HTTPException) as error:
        await internal_dependencies.verify_internal_token(_request(), "correct-token")

    assert error.value.status_code == 503
    assert error.value.detail["code"] == "INTERNAL_RATE_LIMIT_UNAVAILABLE"
