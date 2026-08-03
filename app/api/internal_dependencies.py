"""Scoped, network-restricted authentication for private service routes."""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import ipaddress
import json
import re
import time
from typing import Annotated, Callable, Coroutine, Any

from fastapi import Depends, Header, HTTPException, Request, status

from app.core.config import get_settings
from app.core.redis_client import get_redis


_RATE_LIMIT_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
local ttl = redis.call('TTL', KEYS[1])
if current == 1 or ttl < 0 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
end
return current
"""
_CALLER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class InternalPrincipal:
    caller: str
    scopes: frozenset[str]


def _peer_is_allowed(peer: str, raw_cidrs: str) -> bool:
    try:
        address = ipaddress.ip_address(peer)
        return any(
            address in ipaddress.ip_network(cidr.strip(), strict=False)
            for cidr in raw_cidrs.split(",")
            if cidr.strip()
        )
    except ValueError:
        return False


def _resolve_principal(
    token: str | None,
    caller: str | None,
) -> InternalPrincipal | None:
    settings = get_settings()
    caller_map_secret = settings.INTERNAL_CALLER_TOKENS
    if caller_map_secret is not None:
        if not caller or not _CALLER_RE.fullmatch(caller):
            return None
        records = json.loads(caller_map_secret.get_secret_value())
        record = records.get(caller)
        if not isinstance(record, dict):
            return None
        expected = record.get("token")
        if not isinstance(expected, str) or token is None or not hmac.compare_digest(
            token, expected
        ):
            return None
        return InternalPrincipal(caller, frozenset(record.get("scopes", [])))

    if getattr(settings, "ENVIRONMENT", "test") in {"staging", "production"}:
        return None
    configured = settings.INTERNAL_API_TOKEN
    if configured is None or not configured.get_secret_value():
        return None
    expected = configured.get_secret_value()
    if token is None or not hmac.compare_digest(token, expected):
        return None
    scopes = frozenset(
        scope.strip()
        for scope in settings.INTERNAL_API_TOKEN_SCOPES.split(",")
        if scope.strip()
    )
    return InternalPrincipal("legacy", scopes)


async def authenticate_internal_caller(
    request: Request,
    x_internal_token: Annotated[
        str | None, Header(alias="X-Internal-Token")
    ] = None,
    x_internal_caller: Annotated[
        str | None, Header(alias="X-Internal-Caller")
    ] = None,
) -> InternalPrincipal:
    settings = get_settings()
    secure_environment = getattr(settings, "ENVIRONMENT", "test") in {
        "staging",
        "production",
    }
    has_auth = settings.INTERNAL_CALLER_TOKENS is not None or (
        not secure_environment
        and settings.INTERNAL_API_TOKEN is not None
        and bool(settings.INTERNAL_API_TOKEN.get_secret_value())
    )
    if not has_auth:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "INTERNAL_AUTH_NOT_CONFIGURED",
                "message": "Internal API authentication is not configured",
            },
        )

    peer = request.client.host if request.client else ""
    if not _peer_is_allowed(peer, settings.INTERNAL_API_ALLOWED_CIDRS):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "INTERNAL_PEER_DENIED", "message": "Internal peer denied"},
        )
    if x_internal_token is not None and len(x_internal_token) > 4096:
        principal = None
    else:
        principal = _resolve_principal(x_internal_token, x_internal_caller)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_INTERNAL_TOKEN", "message": "Invalid internal token"},
        )

    redis = await get_redis()
    if redis is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "INTERNAL_RATE_LIMIT_UNAVAILABLE",
                "message": "Internal API protection unavailable",
            },
        )
    minute = int(time.time() // 60)
    key = f"internal_api:rate:{principal.caller}:{peer}:{minute}"
    try:
        count = int(await redis.eval(_RATE_LIMIT_SCRIPT, 1, key, 65))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "INTERNAL_RATE_LIMIT_UNAVAILABLE",
                "message": "Internal API protection unavailable",
            },
        ) from exc
    if count > settings.INTERNAL_API_RATE_LIMIT_PER_MINUTE:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "INTERNAL_RATE_LIMITED",
                "message": "Too many internal requests",
            },
        )
    request.state.internal_caller = principal.caller
    return principal


def require_internal_scope(
    required_scope: str,
) -> Callable[..., Coroutine[Any, Any, InternalPrincipal]]:
    async def dependency(
        principal: Annotated[InternalPrincipal, Depends(authenticate_internal_caller)],
    ) -> InternalPrincipal:
        if required_scope not in principal.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "INTERNAL_SCOPE_DENIED",
                    "message": "Internal caller scope denied",
                },
            )
        return principal

    return dependency


async def verify_internal_token(
    request: Request,
    x_internal_token: Annotated[
        str | None, Header(alias="X-Internal-Token")
    ] = None,
    x_internal_caller: Annotated[
        str | None, Header(alias="X-Internal-Caller")
    ] = None,
) -> None:
    """Backward-compatible dependency; new routes must require an explicit scope."""
    await authenticate_internal_caller(request, x_internal_token, x_internal_caller)
