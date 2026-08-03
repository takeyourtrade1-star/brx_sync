"""
JWT-based authentication dependencies for BRX Sync Microservice.
Validates RS256 tokens from Auth Service using public key (Zero Trust Architecture).
"""
import logging
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings
from app.core.jwt_executor import (
    JwtVerificationCapacityError,
    run_bounded_jwt_verification,
)

logger = logging.getLogger(__name__)
security = HTTPBearer(auto_error=False)


def _get_public_key_for_verify(settings: object | None = None) -> str:
    """Return normalized PEM public key for jwt.decode."""
    active_settings = settings if settings is not None else get_settings()
    return active_settings.jwt_public_key_pem  # type: ignore[attr-defined]


def _decode_verified_payload(
    token: str,
    settings: object,
    require_issuer_audience: bool,
    require_jti: bool,
) -> dict[str, object]:
    public_key = _get_public_key_for_verify(settings)
    required_claims = ["exp", "sub", "type", "iat"]
    if require_jti:
        required_claims.append("jti")
    payload = jwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        leeway=settings.JWT_LEEWAY_SECONDS,  # type: ignore[attr-defined]
        options={
            "verify_signature": True,
            "verify_exp": True,
            "verify_iat": True,
            "verify_aud": False,
            "require": required_claims,
        },
    )
    issuer = payload.get("iss")
    audience = payload.get("aud")
    if issuer is not None and (
        not isinstance(issuer, str)
        or issuer != settings.JWT_ISSUER  # type: ignore[attr-defined]
    ):
        raise jwt.InvalidIssuerError("Invalid issuer")
    if audience is not None:
        if isinstance(audience, str):
            audiences = [audience]
        elif (
            isinstance(audience, list)
            and 1 <= len(audience) <= 4
            and all(
                isinstance(value, str) and 1 <= len(value) <= 255
                for value in audience
            )
        ):
            audiences = audience
        else:
            raise jwt.InvalidAudienceError("Invalid audience")
        if settings.JWT_AUDIENCE not in audiences:  # type: ignore[attr-defined]
            raise jwt.InvalidAudienceError("Invalid audience")
    if require_issuer_audience and (issuer is None or audience is None):
        raise jwt.MissingRequiredClaimError("iss" if issuer is None else "aud")
    if payload.get("jti") is not None:
        try:
            UUID(str(payload["jti"]))
        except (TypeError, ValueError) as exc:
            raise jwt.InvalidTokenError("Invalid jti") from exc
    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    if (
        isinstance(issued_at, bool)
        or isinstance(expires_at, bool)
        or not isinstance(issued_at, (int, float))
        or not isinstance(expires_at, (int, float))
        or expires_at <= issued_at
        or expires_at - issued_at > settings.JWT_MAX_ACCESS_TOKEN_SECONDS  # type: ignore[attr-defined]
    ):
        raise jwt.InvalidTokenError("Invalid access token lifetime")
    return payload


async def get_current_user_id(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> str:
    """
    Validate JWT from Authorization: Bearer <token>.
    
    Verifies:
    - Signature with Auth Service public key (RS256)
    - Token expiration (exp)
    - Token type is "access" (not refresh or pre_auth)
    - MFA is verified (mfa_verified == True)
    
    Returns:
        user_id (str): User ID from token payload (sub claim)
        
    Raises:
        HTTPException 401: If token is invalid, expired, or missing
        HTTPException 503: If JWT configuration is invalid
    """
    token = credentials.credentials if credentials else ""
    settings = get_settings()
    require_issuer_audience = getattr(
        settings,
        "jwt_require_issuer_audience",
        settings.JWT_REQUIRE_ISSUER_AUDIENCE,
    )
    require_jti = getattr(settings, "jwt_require_jti", settings.JWT_REQUIRE_JTI)
    if not token or len(token.encode("utf-8")) > settings.JWT_MAX_TOKEN_BYTES:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = await run_bounded_jwt_verification(
            _decode_verified_payload,
            token,
            settings,
            require_issuer_audience,
            require_jti,
            max_concurrency=settings.JWT_VERIFY_MAX_CONCURRENCY,
            queue_timeout=settings.JWT_VERIFY_QUEUE_TIMEOUT_SECONDS,
        )
    except JwtVerificationCapacityError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication verification temporarily saturated",
            headers={"Retry-After": "1"},
        ) from exc
    except jwt.ExpiredSignatureError:
        logger.warning("JWT token expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired. Please refresh your token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        logger.debug("Invalid JWT token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except ValueError:
        logger.error("JWT key configuration error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service configuration error",
        )

    # Verify token type is "access" (not refresh or pre_auth)
    token_type = payload.get("type")
    if token_type != "access":
        logger.warning("JWT rejected because its token type is not access")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type. Access token required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Verify MFA is verified
    mfa_verified = payload.get("mfa_verified", False)
    if mfa_verified is not True:
        logger.warning("Token without MFA verification")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MFA verification required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Extract user_id from sub claim
    user_id = payload.get("sub")
    if not user_id:
        logger.error("JWT payload missing 'sub' claim")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: missing user identifier",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        return str(UUID(str(user_id)))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: malformed user identifier",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def verify_user_id_match(
    user_id: str,  # FastAPI injects this from the URL path at request time
    user_id_from_token: str = Depends(get_current_user_id),
) -> str:
    """
    Verify that user_id from JWT token matches user_id from URL path.
    Use as: Depends(verify_user_id_match) (no parentheses with user_id).
    """
    try:
        token_uuid = UUID(user_id_from_token)
        path_uuid = UUID(user_id)
        if token_uuid != path_uuid:
            logger.warning("JWT subject does not match the requested user resource")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: User ID mismatch",
            )
    except ValueError:
        if user_id_from_token != user_id:
            logger.warning("JWT subject does not match the requested user resource")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: User ID mismatch",
            )
    return user_id_from_token
