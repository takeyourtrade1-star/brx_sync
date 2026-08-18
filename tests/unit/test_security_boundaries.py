"""Regression tests for protocol, crypto and asynchronous trust boundaries."""

import base64
import hashlib
import hmac
import inspect
import json
import logging
import ssl
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet, MultiFernet
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.api.v1.routes.sync import get_task_status
from app.api.v1.schemas import (
    InventoryItemResponse,
    SyncStatusResponse,
    UpdateInventoryItemRequest,
)
from app.core import crypto, exception_handlers
from app.core.logging import StructuredFormatter
from app.core.config import Settings
from app.core.http_security import RequestBodyLimitMiddleware, SecurityHeadersMiddleware
from app.core.webhook_validator import (
    WebhookValidationError,
    enforce_webhook_rate_limit,
    validate_webhook_signature,
)
from app.maintenance.rotate_credentials import rotate_row_credentials
from app.tasks.sync_tasks import (
    _process_webhook_notification_async,
    process_webhook_notification,
)


async def _collect(middleware, *, headers=(), chunks=(b"",)):
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/sync/webhook/user/test",
            "headers": list(headers),
        },
        receive,
        send,
    )
    return sent


async def _read_body_app(_scope, receive, send):
    while True:
        message = await receive()
        if not message.get("more_body", False):
            break
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


@pytest.mark.asyncio
async def test_request_limit_rejects_ambiguous_framing_and_stream_overflow():
    middleware = RequestBodyLimitMiddleware(_read_body_app, max_bytes=4)
    ambiguous = await _collect(
        middleware,
        headers=((b"content-length", b"4"), (b"transfer-encoding", b"chunked")),
    )
    oversized = await _collect(middleware, chunks=(b"123", b"45"))
    assert ambiguous[0]["status"] == 400
    assert oversized[0]["status"] == 413


@pytest.mark.asyncio
async def test_request_limit_rejects_duplicate_te_and_invalid_lengths():
    middleware = RequestBodyLimitMiddleware(_read_body_app, max_bytes=4)
    cases = (
        ((b"transfer-encoding", b"chunked"), (b"transfer-encoding", b"chunked")),
        ((b"transfer-encoding", b"gzip"),),
        ((b"content-length", b"not-a-number"),),
        ((b"content-length", b"9" * 21),),
    )
    for headers in cases:
        response = await _collect(middleware, headers=headers)
        assert response[0]["status"] == 400


@pytest.mark.asyncio
async def test_request_limit_emits_one_413_and_never_consumes_after_overflow():
    source = [
        {"type": "http.request", "body": b"123", "more_body": True},
        {"type": "http.request", "body": b"45", "more_body": True},
        {"type": "http.request", "body": b"must-not-be-read", "more_body": False},
    ]
    reads = 0
    downstream = []
    sent = []

    async def parser_app(_scope, receive, send):
        downstream.extend([await receive(), await receive(), await receive()])
        await send({"type": "http.response.start", "status": 500, "headers": []})
        await send({"type": "http.response.body", "body": b"internal error"})

    async def receive():
        nonlocal reads
        message = source[reads]
        reads += 1
        return message

    async def send(message):
        sent.append(message)

    await RequestBodyLimitMiddleware(parser_app, max_bytes=4)(
        {"type": "http", "method": "POST", "path": "/api/v1/sync", "headers": []},
        receive,
        send,
    )
    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert reads == 2
    assert [message["type"] for message in downstream] == [
        "http.request",
        "http.disconnect",
        "http.disconnect",
    ]
    assert len(starts) == 1 and starts[0]["status"] == 413
    assert b"internal error" not in [message.get("body") for message in sent]


@pytest.mark.asyncio
async def test_request_limit_rejects_empty_fragment_flood_and_bodyless_framing():
    flood = await _collect(
        RequestBodyLimitMiddleware(_read_body_app, max_bytes=4, max_messages=2),
        chunks=(b"", b"", b""),
    )
    assert flood[0]["status"] == 413

    called = False

    async def app(_scope, _receive, _send):
        nonlocal called
        called = True

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await RequestBodyLimitMiddleware(app, max_bytes=4)(
        {
            "type": "http",
            "method": "GET",
            "path": "/health",
            "headers": [(b"content-length", b"1")],
        },
        receive,
        send,
    )
    assert not called
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_security_headers_include_hsts_in_production_mode():
    middleware = SecurityHeadersMiddleware(_read_body_app, hsts=True)
    response = await _collect(middleware)
    headers = dict(response[0]["headers"])
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"x-frame-options"] == b"DENY"
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"strict-transport-security"].startswith(b"max-age=31536000")


def test_webhook_signature_is_strict_base64_sha256():
    body = b'{"event":"inventory.updated"}'
    secret = "test-secret"
    signature = base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()
    assert validate_webhook_signature(body, signature, secret)
    for malformed in ("not base64!", base64.b64encode(b"short").decode()):
        with pytest.raises(WebhookValidationError):
            validate_webhook_signature(body, malformed, secret)


@pytest.mark.asyncio
async def test_webhook_peer_bucket_prevents_rotating_user_quota_bypass():
    class Redis:
        def __init__(self):
            self.counts = {}

        async def eval(self, _script, _keys, key, _ttl):
            self.counts[key] = self.counts.get(key, 0) + 1
            return self.counts[key]

    redis = Redis()
    request = SimpleNamespace(client=SimpleNamespace(host="203.0.113.9"))
    limits = SimpleNamespace(
        WEBHOOK_PEER_RATE_LIMIT_PER_MINUTE=1,
        WEBHOOK_RATE_LIMIT_PER_MINUTE=100,
    )
    with (
        patch("app.core.webhook_validator.get_redis", return_value=redis),
        patch("app.core.webhook_validator.get_settings", return_value=limits),
        patch("app.core.webhook_validator.time.time", return_value=60),
    ):
        await enforce_webhook_rate_limit(request, "first-user")
        with pytest.raises(HTTPException) as denied:
            await enforce_webhook_rate_limit(request, "rotated-user")
    assert denied.value.status_code == 429
    assert not any("rotated-user" in key for key in redis.counts)


@pytest.mark.asyncio
async def test_webhook_rate_limit_repairs_an_existing_key_without_ttl():
    peer_key = "webhook:rate:peer:203.0.113.9:1"

    class Redis:
        def __init__(self):
            self.counts = {peer_key: 4}
            self.ttls = {peer_key: -1}
            self.expirations = []

        async def eval(self, script, _keys, key, ttl):
            self.counts[key] = self.counts.get(key, 0) + 1
            current_ttl = self.ttls.get(key, -2)
            if "redis.call('TTL', KEYS[1])" in script and (
                self.counts[key] == 1 or current_ttl < 0
            ):
                self.ttls[key] = ttl
                self.expirations.append((key, ttl))
            return self.counts[key]

    redis = Redis()
    request = SimpleNamespace(client=SimpleNamespace(host="203.0.113.9"))
    limits = SimpleNamespace(
        WEBHOOK_PEER_RATE_LIMIT_PER_MINUTE=100,
        WEBHOOK_RATE_LIMIT_PER_MINUTE=100,
    )
    with (
        patch("app.core.webhook_validator.get_redis", return_value=redis),
        patch("app.core.webhook_validator.get_settings", return_value=limits),
        patch("app.core.webhook_validator.time.time", return_value=60),
    ):
        await enforce_webhook_rate_limit(request, "test-user")

    assert (peer_key, 65) in redis.expirations


def test_previous_fernet_key_is_decrypt_only_and_rotates_to_primary():
    primary = Fernet.generate_key()
    previous = Fernet.generate_key()
    legacy = Fernet(previous).encrypt(b"cardtrader-token").decode()
    settings = SimpleNamespace(
        FERNET_KEY=primary.decode(),
        fernet_previous_keys=(previous,),
    )
    with patch.object(crypto, "settings", settings):
        manager = crypto.EncryptionManager()
    assert manager.decrypt(legacy) == "cardtrader-token"
    rotated = manager.rotate(legacy)
    assert Fernet(primary).decrypt(rotated.encode()) == b"cardtrader-token"


def test_maintenance_encrypts_plaintext_webhook_and_rotates_token_atomically_in_row():
    primary = Fernet.generate_key()
    previous = Fernet.generate_key()
    manager = crypto.EncryptionManager.__new__(crypto.EncryptionManager)
    manager._primary = Fernet(primary)
    manager.fernet = MultiFernet([manager._primary, Fernet(previous)])
    row = SimpleNamespace(
        cardtrader_token_encrypted=Fernet(previous).encrypt(b"token").decode(),
        webhook_secret="legacy-plaintext-secret",
    )
    rotate_row_credentials(row, manager)
    assert Fernet(primary).decrypt(row.cardtrader_token_encrypted.encode()) == b"token"
    assert row.webhook_secret.startswith("fernet:")
    assert manager.decrypt_at_rest_secret(row.webhook_secret) == "legacy-plaintext-secret"


def test_celery_webhook_task_accepts_only_durable_inbox_identifier():
    assert tuple(inspect.signature(process_webhook_notification).parameters) == (
        "webhook_id",
    )
    assert tuple(inspect.signature(_process_webhook_notification_async).parameters) == (
        "webhook_id",
    )


def test_inventory_update_rejects_unbounded_or_nested_property_payloads():
    with pytest.raises(ValidationError):
        UpdateInventoryItemRequest.model_validate(
            {"properties": {"condition": {"nested": ["payload"]}}}
        )
    with pytest.raises(ValidationError):
        UpdateInventoryItemRequest.model_validate({"quantity": 2**31})
    with pytest.raises(ValidationError):
        UpdateInventoryItemRequest.model_validate(
            {"quantity": 1, "unexpected_mutation": True}
        )


def test_response_contract_keeps_execution_fences_visible_to_clients():
    status = SyncStatusResponse(
        user_id="9ffb278f-837c-4a9f-b74a-f61e509037c6",
        sync_status="active",
        execution_mode="real",
        mode_version=7,
        writes_enabled=True,
    )
    assert status.model_dump()["mode_version"] == 7
    item = InventoryItemResponse(
        id=1,
        blueprint_id=2,
        quantity=1,
        price_cents=100,
        source="cardtrader",
        environment="real",
        lifecycle_status="active",
        sync_state="synced",
        mapping_status="mapped",
        row_version=3,
        updated_at="2026-08-01T00:00:00Z",
    )
    assert item.model_dump()["row_version"] == 3


def _sync_settings(**overrides):
    values = {
        "AWS_SSM_ENABLED": False,
        "ENVIRONMENT": "test",
        "DATABASE_URL": "postgresql+asyncpg://db-user:db-password@db/test",
        "MYSQL_HOST": "db",
        "MYSQL_USER": "mysql-user",
        "MYSQL_PASSWORD": "mysql-password",
        "MYSQL_DATABASE": "cards",
        "REDIS_URL": "redis://:redis-password@redis:6379/0",
        "FERNET_KEY": Fernet.generate_key().decode(),
        "JWT_PUBLIC_KEY": "test-public-key",
        "INTERNAL_API_TOKEN": "test-internal-token",
        "PUBLIC_BASE_URL": "http://localhost:8000",
    }
    if overrides.get("ENVIRONMENT") in {"staging", "production"}:
        values["INTERNAL_API_TOKEN"] = None
        values["INTERNAL_CALLER_TOKENS"] = json.dumps(
            {
                "auction": {
                    "token": "a" * 32,
                    "scopes": ["inventory:write"],
                }
            }
        )
    values.update(overrides)
    return Settings(**values)


def test_settings_repr_redacts_connection_credentials_and_wildcard_hosts_fail_prod():
    representation = repr(_sync_settings())
    assert "db-password" not in representation
    assert "redis-password" not in representation
    assert "mysql-password" not in representation
    with pytest.raises(ValueError, match="Wildcard TRUSTED_HOSTS"):
        _sync_settings(ENVIRONMENT="production", TRUSTED_HOSTS="*")
    with pytest.raises(ValueError, match="DEBUG must be false"):
        _sync_settings(ENVIRONMENT="production", DEBUG=True)
    with pytest.raises(ValueError, match="ENABLE_TEST_ENDPOINTS must be false"):
        _sync_settings(ENVIRONMENT="production", ENABLE_TEST_ENDPOINTS=True)
    for invalid_environment in ("prod", "Production", "stagingg"):
        with pytest.raises(ValueError):
            _sync_settings(ENVIRONMENT=invalid_environment)


def test_production_jwt_defaults_strict_and_rollout_is_bounded():
    production = {
        "ENVIRONMENT": "production",
        "PUBLIC_BASE_URL": "https://sync.ebartex.com",
        "ALLOWED_ORIGINS": "https://www.ebartex.com",
        "REDIS_URL": "redis://brx-sync-redis:6379/0",
    }
    strict = _sync_settings(**production, JWT_REQUIRE_ISSUER_AUDIENCE=True, JWT_REQUIRE_JTI=True)
    assert strict.jwt_require_issuer_audience is True
    assert strict.jwt_require_jti is True
    postgres_tls = strict.postgres_connect_args["ssl"]
    mysql_tls = strict.mysql_ssl_context
    assert postgres_tls.check_hostname is True
    assert postgres_tls.verify_mode == ssl.CERT_REQUIRED
    assert postgres_tls.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert mysql_tls is not None and mysql_tls.check_hostname is True
    assert mysql_tls.verify_mode == ssl.CERT_REQUIRED
    assert strict.postgres_sync_connect_args["sslmode"] == "verify-full"

    default_settings = _sync_settings(**production)
    assert default_settings.jwt_require_issuer_audience is False
    assert default_settings.jwt_require_jti is False


def test_production_rejects_oversubscribed_per_process_database_pools():
    production = {
        "ENVIRONMENT": "production",
        "PUBLIC_BASE_URL": "https://sync.ebartex.com",
        "ALLOWED_ORIGINS": "https://www.ebartex.com",
        "REDIS_URL": "redis://brx-sync-redis:6379/0",
    }
    for overrides in (
        {"DB_POOL_SIZE": 10, "DB_MAX_OVERFLOW": 1},
        {"MYSQL_POOL_SIZE": 10, "MYSQL_POOL_MAX_OVERFLOW": 1},
    ):
        with pytest.raises(ValueError, match="pool capacity"):
            _sync_settings(**production, **overrides)


def test_staging_uses_the_same_transport_and_authentication_boundaries():
    staging = _sync_settings(
        ENVIRONMENT="staging",
        PUBLIC_BASE_URL="https://staging-sync.ebartex.com",
        ALLOWED_ORIGINS="https://staging.ebartex.com",
        REDIS_URL="redis://brx-sync-redis:6379/0",
    )
    assert staging.jwt_require_issuer_audience is True
    assert staging.jwt_require_jti is True
    assert staging.postgres_connect_args["ssl"].verify_mode == ssl.CERT_REQUIRED
    assert staging.postgres_sync_connect_args["sslmode"] == "verify-full"
    assert staging.mysql_ssl_context is not None

    with pytest.raises(ValueError, match="plain Redis"):
        _sync_settings(
            ENVIRONMENT="staging",
            PUBLIC_BASE_URL="https://staging-sync.ebartex.com",
            ALLOWED_ORIGINS="https://staging.ebartex.com",
            REDIS_URL="redis://cache.example.test:6379/0",
        )


def test_production_external_redis_requires_tls():
    with pytest.raises(ValueError, match="plain Redis"):
        _sync_settings(
            ENVIRONMENT="production",
            PUBLIC_BASE_URL="https://sync.ebartex.com",
            ALLOWED_ORIGINS="https://www.ebartex.com",
            REDIS_URL="redis://cache.example.test:6379/0",
        )
    secure = _sync_settings(
        ENVIRONMENT="production",
        PUBLIC_BASE_URL="https://sync.ebartex.com",
        ALLOWED_ORIGINS="https://www.ebartex.com",
        REDIS_URL="rediss://cache.example.test:6379/0",
    )
    assert secure.REDIS_URL.startswith("rediss://")
    assert secure.redis_tls_kwargs["ssl_cert_reqs"] == ssl.CERT_REQUIRED
    assert secure.redis_tls_kwargs["ssl_check_hostname"] is True


def test_production_forbids_legacy_internal_token_and_excess_scopes():
    production = {
        "ENVIRONMENT": "production",
        "PUBLIC_BASE_URL": "https://sync.ebartex.com",
        "ALLOWED_ORIGINS": "https://www.ebartex.com",
        "REDIS_URL": "redis://brx-sync-redis:6379/0",
    }
    with pytest.raises(ValueError, match="Legacy INTERNAL_API_TOKEN"):
        _sync_settings(
            **production,
            INTERNAL_CALLER_TOKENS=None,
            INTERNAL_API_TOKEN="i" * 32,
        )
    excessive = json.dumps(
        {
            "auction": {
                "token": "a" * 32,
                "scopes": ["inventory:write", "metrics:read"],
            }
        }
    )
    with pytest.raises(ValueError, match="minimum service scope"):
        _sync_settings(**production, INTERNAL_CALLER_TOKENS=excessive)


def test_untrusted_trace_ids_are_replaced_and_structured_extra_is_redacted():
    request = SimpleNamespace(headers={"X-Trace-Id": "bad\nlog-injection"})
    generated = exception_handlers.get_trace_id(request)
    uuid.UUID(generated)

    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="ok",
        args=(),
        exc_info=None,
    )
    record.extra = {"api_token": "super-secret", "safe": "value"}
    payload = json.loads(StructuredFormatter().format(record))
    assert payload["api_token"] == "[REDACTED]"
    assert "super-secret" not in json.dumps(payload)


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _TaskSession:
    def __init__(self, operation):
        self.operation = operation

    async def execute(self, _statement):
        return _ScalarResult(self.operation)


@pytest.mark.asyncio
async def test_task_status_does_not_expose_backend_exception_or_foreign_existence():
    task_id = "3bc4f5e2-d6c2-4e7f-a93a-3f68a7453fab"
    user_id = "9ffb278f-837c-4a9f-b74a-f61e509037c6"
    failed = SimpleNamespace(
        status="failed",
        operation_metadata={"error": "private database hostname and token"},
    )
    response = await get_task_status(task_id, user_id, _TaskSession(failed))
    assert response["error"] == "Task failed"
    assert "private database" not in str(response)

    with pytest.raises(HTTPException) as missing:
        await get_task_status(task_id, user_id, _TaskSession(None))
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_unhandled_exception_response_is_generic_and_hardened_even_in_debug():
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/private",
            "headers": [],
            "query_string": b"",
        }
    )
    for debug in (False, True):
        safe_settings = SimpleNamespace(DEBUG=debug, ENVIRONMENT="production")
        with patch.object(exception_handlers, "settings", safe_settings):
            response = await exception_handlers.generic_exception_handler(
                request, RuntimeError("private database hostname")
            )
        assert response.status_code == 500
        assert b"private database hostname" not in response.body
        assert response.headers["cache-control"] == "no-store"
        assert "max-age=31536000" in response.headers["strict-transport-security"]
