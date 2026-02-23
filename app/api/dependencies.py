"""
JWT-based authentication dependencies for BRX Sync Microservice.
Validates RS256 tokens from Auth Service using public key (Zero Trust Architecture).
"""
import logging
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings

logger = logging.getLogger(__name__)
security = HTTPBearer(auto_error=True)


def _get_public_key_for_verify() -> str:
    """Return normalized PEM public key for jwt.decode."""
    settings = get_settings()
    return settings.jwt_public_key_pem


async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(security),
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
    token = credentials.credentials
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        public_key = _get_public_key_for_verify()
        settings = get_settings()
        algorithm = settings.JWT_ALGORITHM or "RS256"
        
        payload = jwt.decode(
            token,
            public_key,
            algorithms=[algorithm],
            options={
                "verify_signature": True,
                "verify_exp": True,
                "require": ["exp", "sub", "type"],
            },
        )
    except jwt.ExpiredSignatureError:
        logger.warning("JWT token expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired. Please refresh your token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as e:
        logger.debug(f"Invalid JWT token: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except ValueError as e:
        logger.error(f"JWT key configuration error: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service configuration error",
        )

    # Verify token type is "access" (not refresh or pre_auth)
    token_type = payload.get("type")
    if token_type != "access":
        logger.warning(f"Invalid token type: {token_type}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type. Access token required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Verify MFA is verified
    mfa_verified = payload.get("mfa_verified", False)
    if not mfa_verified:
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

    return str(user_id)


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
            logger.warning(
                f"User ID mismatch: token={user_id_from_token}, path={user_id}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: User ID mismatch",
            )
    except ValueError:
        if user_id_from_token != user_id:
            logger.warning(
                f"User ID mismatch: token={user_id_from_token}, path={user_id}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: User ID mismatch",
            )
    return user_id_from_token
