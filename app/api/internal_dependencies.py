"""Authentication for private service-to-service routes."""

import hmac

from fastapi import Header, HTTPException, status

from app.core.config import get_settings


async def verify_internal_token(
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
