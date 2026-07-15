"""Authentication for private service-to-service routes."""

import hmac
import time

from fastapi import Header, HTTPException, Request, status

from app.core.config import get_settings
from app.core.redis_client import get_redis


async def verify_internal_token(
    request: Request,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """Fail closed unless the caller presents the configured shared token."""
    configured = get_settings().INTERNAL_API_TOKEN
    if configured is None or not configured.get_secret_value():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "INTERNAL_AUTH_NOT_CONFIGURED",
                "message": "Internal API authentication is not configured",
            },
        )

    if x_internal_token is None or not hmac.compare_digest(
        x_internal_token, configured.get_secret_value()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_INTERNAL_TOKEN", "message": "Invalid internal token"},
        )

    # Shared-token endpoints mutate sellable stock. Rate-limit them in the
    # existing Redis so a leaked token cannot create an unbounded write storm.
    redis = await get_redis()
    if redis is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "INTERNAL_RATE_LIMIT_UNAVAILABLE",
                "message": "Internal API protection unavailable",
            },
        )
    peer = request.client.host if request.client else "unknown"
    minute = int(time.time() // 60)
    key = f"internal_api:rate:{peer}:{minute}"
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, 65)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "INTERNAL_RATE_LIMIT_UNAVAILABLE",
                "message": "Internal API protection unavailable",
            },
        ) from exc
    if count > get_settings().INTERNAL_API_RATE_LIMIT_PER_MINUTE:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "INTERNAL_RATE_LIMITED",
                "message": "Too many internal inventory requests",
            },
        )
