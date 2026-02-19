"""
Webhook signature validator for CardTrader webhooks.
Validates HMAC-SHA256 signature using shared_secret.
"""
import base64
import hashlib
import hmac
import logging

logger = logging.getLogger(__name__)


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
    
    try:
        # Decode base64 signature
        expected_signature = base64.b64decode(signature_header)
    except Exception as e:
        raise WebhookValidationError(f"Invalid signature format: {e}") from e
    
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
