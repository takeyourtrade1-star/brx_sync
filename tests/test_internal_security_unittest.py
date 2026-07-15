"""Stdlib security regression tests runnable in the production image."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


def _settings(token: str | None = "correct-token", limit: int = 300) -> SimpleNamespace:
    return SimpleNamespace(
        INTERNAL_API_TOKEN=SecretStr(token) if token is not None else None,
        INTERNAL_API_RATE_LIMIT_PER_MINUTE=limit,
    )


class InternalSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_token_is_rejected_before_redis(self) -> None:
        with (
            patch.object(internal_dependencies, "get_settings", _settings),
            patch.object(
                internal_dependencies,
                "get_redis",
                AsyncMock(side_effect=AssertionError("Redis must not be queried")),
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                await internal_dependencies.verify_internal_token(
                    _request(), "wrong-token"
                )

        self.assertEqual(caught.exception.status_code, 401)

    async def test_valid_token_uses_existing_redis_limit(self) -> None:
        redis = _FakeRedis()
        with (
            patch.object(internal_dependencies, "get_settings", _settings),
            patch.object(
                internal_dependencies, "get_redis", AsyncMock(return_value=redis)
            ),
        ):
            result = await internal_dependencies.verify_internal_token(
                _request(), "correct-token"
            )

        self.assertIsNone(result)
        self.assertEqual(len(redis.expirations), 1)
        self.assertEqual(redis.expirations[0][1], 65)

    async def test_missing_redis_fails_closed(self) -> None:
        with (
            patch.object(internal_dependencies, "get_settings", _settings),
            patch.object(
                internal_dependencies, "get_redis", AsyncMock(return_value=None)
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                await internal_dependencies.verify_internal_token(
                    _request(), "correct-token"
                )

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(
            caught.exception.detail["code"], "INTERNAL_RATE_LIMIT_UNAVAILABLE"
        )

    async def test_redis_error_fails_closed(self) -> None:
        with (
            patch.object(internal_dependencies, "get_settings", _settings),
            patch.object(
                internal_dependencies,
                "get_redis",
                AsyncMock(return_value=_FakeRedis(fail=True)),
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                await internal_dependencies.verify_internal_token(
                    _request(), "correct-token"
                )

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(
            caught.exception.detail["code"], "INTERNAL_RATE_LIMIT_UNAVAILABLE"
        )

    async def test_peer_rate_limit_is_enforced(self) -> None:
        redis = _FakeRedis(initial_count=10)

        def settings() -> SimpleNamespace:
            return _settings(limit=10)

        with (
            patch.object(internal_dependencies, "get_settings", settings),
            patch.object(
                internal_dependencies, "get_redis", AsyncMock(return_value=redis)
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                await internal_dependencies.verify_internal_token(
                    _request("10.0.1.25"), "correct-token"
                )

        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.detail["code"], "INTERNAL_RATE_LIMITED")


if __name__ == "__main__":
    unittest.main()
