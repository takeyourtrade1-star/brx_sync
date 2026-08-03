"""
Webhook signature validator for CardTrader webhooks.
Validates HMAC-SHA256 signature using shared_secret.
"""
import base64
import hashlib
import hmac
import logging
import time

from fastapi import HTTPException, Request, status

from app.core.config import get_settings
from app.core.redis_client import get_redis

logger = logging.getLogger(__name__)
_RATE_LIMIT_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
local ttl = redis.call('TTL', KEYS[1])
if current == 1 or ttl < 0 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
end
return current
"""


class WebhookValidationError(Exception):
    """Webhook validation failed."""
    pass


def validate_webhook_signature(
    body: bytes,
    signature_header: str,
    shared_secret: str
) -> bool:
    """
    Validate CardTrader webhook signature.
    
    Args:
        body: Raw request body bytes
        signature_header: Signature header value (base64 encoded HMAC-SHA256)
        shared_secret: Shared secret from CardTrader /info endpoint
        
    Returns:
        True if signature is valid, False otherwise
        
    Raises:
        WebhookValidationError: If validation fails
    """
    if not signature_header:
        raise WebhookValidationError("Missing signature header")
    
    if not shared_secret:
        raise WebhookValidationError("Missing shared_secret")
    
    if len(signature_header) > 128:
        raise WebhookValidationError("Invalid signature format")
    try:
        # Decode base64 signature
        expected_signature = base64.b64decode(signature_header, validate=True)
    except (ValueError, TypeError) as exc:
        raise WebhookValidationError("Invalid signature format") from exc
    if len(expected_signature) != hashlib.sha256().digest_size:
        raise WebhookValidationError("Invalid signature format")
    
    # Compute HMAC-SHA256
    computed_signature = hmac.new(
        shared_secret.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    
    # Compare signatures (constant-time comparison)
    if not hmac.compare_digest(expected_signature, computed_signature):
        logger.warning("Webhook signature validation failed")
        return False
    
    return True


def verify_webhook(
    body: bytes,
    signature_header: str,
    shared_secret: str
) -> None:
    """
    Verify webhook signature, raising exception if invalid.
    
    Args:
        body: Raw request body bytes
        signature_header: Signature header value
        shared_secret: Shared secret from CardTrader
        
    Raises:
        WebhookValidationError: If signature is invalid
    """
    if not validate_webhook_signature(body, signature_header, shared_secret):
        raise WebhookValidationError("Invalid webhook signature")


async def enforce_webhook_rate_limit(request: Request, user_id: str) -> None:
    """Bound HMAC/DB work per peer and per requested owner.

    The peer-wide bucket is intentionally consumed first. Without it, an attacker
    could rotate arbitrary UUIDs and create an unbounded number of Redis keys while
    bypassing the per-user quota.
    """
    redis = await get_redis()
    if redis is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook protection unavailable",
        )
    peer = request.client.host if request.client else "unknown"
    minute = int(time.time() // 60)
    peer_key = f"webhook:rate:peer:{peer}:{minute}"
    user_key = f"webhook:rate:user:{peer}:{user_id}:{minute}"
    try:
        peer_count = int(await redis.eval(_RATE_LIMIT_SCRIPT, 1, peer_key, 65))
        if peer_count > get_settings().WEBHOOK_PEER_RATE_LIMIT_PER_MINUTE:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many webhook requests",
            )
        count = int(await redis.eval(_RATE_LIMIT_SCRIPT, 1, user_key, 65))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook protection unavailable",
        ) from exc
    if count > get_settings().WEBHOOK_RATE_LIMIT_PER_MINUTE:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many webhook requests",
        )
