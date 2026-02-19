"""
Security utilities for BRX Sync.

Provides input sanitization, XSS prevention, and security helpers.
"""
import re
from typing import Optional
from urllib.parse import quote, unquote

from app.core.logging import get_logger

logger = get_logger(__name__)


def sanitize_string(value: Optional[str], max_length: Optional[int] = None) -> Optional[str]:
    """
    Sanitize string input to prevent XSS and other attacks.
    
    Args:
        value: String value to sanitize
        max_length: Maximum length (None for no limit)
        
    Returns:
        Sanitized string or None
    """
    if value is None:
        return None
    
    if not isinstance(value, str):
        return None
    
    # Strip whitespace
    value = value.strip()
    
    if not value:
        return None
    
    # Remove null bytes
    value = value.replace("\x00", "")
    
    # Remove dangerous patterns
    dangerous_patterns = [
        r"<script[^>]*>.*?</script>",
        r"javascript:",
        r"on\w+\s*=",  # onclick=, onerror=, etc.
        r"vbscript:",
        r"data:text/html",
    ]
    
    for pattern in dangerous_patterns:
        value = re.sub(pattern, "", value, flags=re.IGNORECASE | re.DOTALL)
    
    # Check length
    if max_length and len(value) > max_length:
        value = value[:max_length]
        logger.warning(f"String truncated to {max_length} characters")
    
    return value


def sanitize_path(value: str) -> str:
    """
    Sanitize file path to prevent path traversal attacks.
    
    Args:
        value: Path string
        
    Returns:
        Sanitized path
        
    Raises:
        ValueError: If path contains dangerous patterns
    """
    if not isinstance(value, str):
        raise ValueError("Path must be a string")
    
    # Remove path traversal attempts
    if ".." in value or value.startswith("/"):
        raise ValueError("Path traversal detected")
    
    # Remove null bytes
    value = value.replace("\x00", "")
    
    # Normalize path
    value = value.replace("\\", "/")
    value = "/".join(part for part in value.split("/") if part and part != ".")
    
    return value


def validate_sql_injection_safe(value: str) -> bool:
    """
    Basic check for SQL injection patterns.
    
    Note: This is a basic check. SQLAlchemy's parameterized queries
    provide the real protection. This is just an additional layer.
    
    Args:
        value: String to check
        
    Returns:
        True if safe, False if potentially dangerous
    """
    dangerous_patterns = [
        r"';?\s*(DROP|DELETE|UPDATE|INSERT|ALTER|CREATE|EXEC|EXECUTE)",
        r"UNION\s+SELECT",
        r"--",
        r"/\*",
        r"\*/",
    ]
    
    value_upper = value.upper()
    for pattern in dangerous_patterns:
        if re.search(pattern, value_upper, re.IGNORECASE):
            logger.warning(f"Potential SQL injection pattern detected: {pattern}")
            return False
    
    return True


def encode_url_safe(value: str) -> str:
    """
    URL-encode a string safely.
    
    Args:
        value: String to encode
        
    Returns:
        URL-encoded string
    """
    return quote(value, safe="")


def decode_url_safe(value: str) -> str:
    """
    URL-decode a string safely.
    
    Args:
        value: String to decode
        
    Returns:
        URL-decoded string
    """
    try:
        return unquote(value)
    except Exception as e:
        logger.warning(f"Failed to decode URL: {e}")
        return value
