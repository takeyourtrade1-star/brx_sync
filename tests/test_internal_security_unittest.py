"""Stdlib security regression tests runnable in the production image."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import SecretStr
from starlette.requests import Request

from app.api import internal_dependencies
from app.core import webhook_validator


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
    def __init__(
        self,
        initial_count: int = 0,
        *,
        initial_ttl: int = -2,
        fail: bool = False,
    ) -> None:
        self.count = initial_count
        self.ttl = initial_ttl
        self.fail = fail
        self.expirations: list[tuple[str, int]] = []

    async def incr(self, _key: str) -> int:
        if self.fail:
            raise ConnectionError("redis unavailable")
        self.count += 1
        return self.count

    async def expire(self, key: str, ttl: int) -> None:
        self.expirations.append((key, ttl))

    async def eval(self, script: str, _keys: int, key: str, ttl: int) -> int:
        if self.fail:
            raise ConnectionError("redis unavailable")
        self.count += 1
        if "redis.call('TTL', KEYS[1])" in script and (
            self.count == 1 or self.ttl < 0
        ):
            self.ttl = ttl
            self.expirations.append((key, ttl))
        return self.count


class _PerKeyFakeRedis:
    def __init__(self, orphaned_key: str, initial_count: int) -> None:
        self.counts = {orphaned_key: initial_count}
        self.ttls = {orphaned_key: -1}
        self.expirations: list[tuple[str, int]] = []

    async def eval(self, script: str, _keys: int, key: str, ttl: int) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        current_ttl = self.ttls.get(key, -2)
        if "redis.call('TTL', KEYS[1])" in script and (
            self.counts[key] == 1 or current_ttl < 0
        ):
            self.ttls[key] = ttl
            self.expirations.append((key, ttl))
        return self.counts[key]


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


class InternalSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_caller_map_disables_legacy_and_enforces_scope(self) -> None:
        caller_settings = _settings()
        caller_settings.INTERNAL_CALLER_TOKENS = SecretStr(
            json.dumps(
                {
                    "auction": {
                        "token": "auction-specific-token",
                        "scopes": ["inventory:write"],
                    }
                }
            )
        )
        redis = _FakeRedis()
        with (
            patch.object(
                internal_dependencies, "get_settings", return_value=caller_settings
            ),
            patch.object(
                internal_dependencies, "get_redis", AsyncMock(return_value=redis)
            ),
        ):
            with self.assertRaises(HTTPException) as legacy:
                await internal_dependencies.authenticate_internal_caller(
                    _request(), "correct-token", None
                )
            principal = await internal_dependencies.authenticate_internal_caller(
                _request(), "auction-specific-token", "auction"
            )

        self.assertEqual(legacy.exception.status_code, 401)
        self.assertEqual(principal.caller, "auction")
        self.assertEqual(principal.scopes, frozenset({"inventory:write"}))
        dependency = internal_dependencies.require_internal_scope("metrics:read")
        with self.assertRaises(HTTPException) as denied:
            await dependency(principal)
        self.assertEqual(denied.exception.status_code, 403)

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
        self.assertEqual(redis.count, 1)

    async def test_rate_limit_repairs_an_existing_key_without_ttl(self) -> None:
        redis = _FakeRedis(initial_count=4, initial_ttl=-1)
        with (
            patch.object(internal_dependencies, "get_settings", _settings),
            patch.object(
                internal_dependencies, "get_redis", AsyncMock(return_value=redis)
            ),
            patch.object(internal_dependencies.time, "time", return_value=60),
        ):
            await internal_dependencies.verify_internal_token(
                _request("10.0.1.25"), "correct-token"
            )

        self.assertEqual(
            redis.expirations,
            [("internal_api:rate:legacy:10.0.1.25:1", 65)],
        )

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


class WebhookRateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_repairs_an_existing_key_without_ttl(self) -> None:
        peer_key = "webhook:rate:peer:203.0.113.9:1"
        redis = _PerKeyFakeRedis(peer_key, initial_count=4)
        request = SimpleNamespace(client=SimpleNamespace(host="203.0.113.9"))
        limits = SimpleNamespace(
            WEBHOOK_PEER_RATE_LIMIT_PER_MINUTE=100,
            WEBHOOK_RATE_LIMIT_PER_MINUTE=100,
        )
        with (
            patch.object(webhook_validator, "get_settings", return_value=limits),
            patch.object(
                webhook_validator, "get_redis", AsyncMock(return_value=redis)
            ),
            patch.object(webhook_validator.time, "time", return_value=60),
        ):
            await webhook_validator.enforce_webhook_rate_limit(
                request, "test-user"
            )

        self.assertIn((peer_key, 65), redis.expirations)


if __name__ == "__main__":
    unittest.main()
