"""JWT consumer contract shared with the Auth service."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.api import dependencies


_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_PEM = _PRIVATE_KEY.public_key().public_bytes(
    Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
).decode("ascii")


def _settings(*, require_issuer: bool = False, require_jti: bool = False):
    return SimpleNamespace(
        jwt_public_key_pem=_PUBLIC_PEM,
        JWT_MAX_TOKEN_BYTES=8192,
        JWT_VERIFY_MAX_CONCURRENCY=4,
        JWT_VERIFY_QUEUE_TIMEOUT_SECONDS=0.05,
        JWT_LEEWAY_SECONDS=0,
        JWT_ISSUER="ebartex-auth",
        JWT_AUDIENCE="ebartex-services",
        JWT_REQUIRE_ISSUER_AUDIENCE=require_issuer,
        JWT_REQUIRE_JTI=require_jti,
        JWT_MAX_ACCESS_TOKEN_SECONDS=3660,
    )


def _token(**overrides) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(uuid4()),
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "mfa_verified": True,
        "jti": str(uuid4()),
    }
    payload.update(overrides)
    return jwt.encode(payload, _PRIVATE_KEY, algorithm="RS256")


async def _decode(monkeypatch, token: str, settings) -> str:
    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)
    return await dependencies.get_current_user_id(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    )


@pytest.mark.asyncio
async def test_legacy_access_token_is_accepted_during_rollout(monkeypatch):
    subject = str(uuid4())
    assert await _decode(monkeypatch, _token(sub=subject), _settings()) == subject


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "attacker", "aud": "ebartex-services"},
        {"iss": "ebartex-auth", "aud": "other-service"},
        {"iss": 123, "aud": "ebartex-services"},
        {"iss": "ebartex-auth", "aud": 123},
        {"iss": "ebartex-auth", "aud": []},
        {"iss": "ebartex-auth", "aud": ["ebartex-services"] * 5},
        {"mfa_verified": "true"},
        {"sub": "not-a-uuid"},
    ],
)
async def test_invalid_access_claims_are_rejected(monkeypatch, claims):
    with pytest.raises(HTTPException) as caught:
        await _decode(monkeypatch, _token(**claims), _settings())
    assert caught.value.status_code == 401


@pytest.mark.asyncio
async def test_cutover_flags_require_issuer_audience_and_jti(monkeypatch):
    claims = {"iss": "ebartex-auth", "aud": "ebartex-services"}
    assert await _decode(
        monkeypatch, _token(**claims), _settings(require_issuer=True, require_jti=True)
    )

    missing_jti = _token(**claims)
    payload = jwt.decode(missing_jti, _PUBLIC_PEM, algorithms=["RS256"], options={"verify_aud": False})
    payload.pop("jti")
    without_jti = jwt.encode(payload, _PRIVATE_KEY, algorithm="RS256")
    with pytest.raises(HTTPException):
        await _decode(
            monkeypatch,
            without_jti,
            _settings(require_issuer=True, require_jti=True),
        )


@pytest.mark.asyncio
async def test_access_token_lifetime_is_bounded(monkeypatch):
    now = datetime.now(timezone.utc)
    with pytest.raises(HTTPException) as caught:
        await _decode(
            monkeypatch,
            _token(iat=now, exp=now + timedelta(hours=2)),
            _settings(),
        )
    assert caught.value.status_code == 401
