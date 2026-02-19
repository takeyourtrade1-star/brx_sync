"""
Input validators for BRX Sync.

Provides validation functions for UUIDs, blueprint IDs, external stock IDs,
and business rule validation.
"""
import uuid
from typing import Optional

from app.core.exceptions import ValidationError


def validate_uuid(value: str, field_name: str = "id") -> uuid.UUID:
    """
    Validate and parse UUID string.
    
    Args:
        value: UUID string
        field_name: Field name for error messages
        
    Returns:
        Parsed UUID
        
    Raises:
        ValidationError: If UUID is invalid
    """
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError) as e:
        raise ValidationError(
            detail=f"Invalid {field_name} format: must be a valid UUID",
            field=field_name,
            value=value,
        ) from e


def validate_blueprint_id(value: int, field_name: str = "blueprint_id") -> int:
    """
    Validate blueprint ID.
    
    Args:
        value: Blueprint ID
        field_name: Field name for error messages
        
    Returns:
        Validated blueprint ID
        
    Raises:
        ValidationError: If blueprint ID is invalid
    """
    if not isinstance(value, int):
        raise ValidationError(
            detail=f"{field_name} must be an integer",
            field=field_name,
            value=value,
        )
    
    if value <= 0:
        raise ValidationError(
            detail=f"{field_name} must be positive",
            field=field_name,
            value=value,
        )
    
    return value


def validate_external_stock_id(value: Optional[str], field_name: str = "external_stock_id") -> Optional[str]:
    """
    Validate external stock ID (CardTrader product ID).
    
    Args:
        value: External stock ID string
        field_name: Field name for error messages
        
    Returns:
        Validated external stock ID (stripped) or None
        
    Raises:
        ValidationError: If external stock ID format is invalid
    """
    if value is None:
        return None
    
    if not isinstance(value, str):
        raise ValidationError(
            detail=f"{field_name} must be a string",
            field=field_name,
            value=value,
        )
    
    value = value.strip()
    
    if not value:
        return None
    
    # External stock ID should be numeric (CardTrader product ID)
    if not value.isdigit():
        raise ValidationError(
            detail=f"{field_name} must be numeric (CardTrader product ID)",
            field=field_name,
            value=value,
        )
    
    return value


def validate_quantity(value: Optional[int], field_name: str = "quantity") -> Optional[int]:
    """
    Validate quantity.
    
    Args:
        value: Quantity value
        field_name: Field name for error messages
        
    Returns:
        Validated quantity
        
    Raises:
        ValidationError: If quantity is invalid
    """
    if value is None:
        return None
    
    if not isinstance(value, int):
        raise ValidationError(
            detail=f"{field_name} must be an integer",
            field=field_name,
            value=value,
        )
    
    if value < 0:
        raise ValidationError(
            detail=f"{field_name} must be >= 0",
            field=field_name,
            value=value,
        )
    
    return value


def validate_price_cents(value: Optional[int], field_name: str = "price_cents") -> Optional[int]:
    """
    Validate price in cents.
    
    Args:
        value: Price in cents
        field_name: Field name for error messages
        
    Returns:
        Validated price in cents
        
    Raises:
        ValidationError: If price is invalid
    """
    if value is None:
        return None
    
    if not isinstance(value, int):
        raise ValidationError(
            detail=f"{field_name} must be an integer",
            field=field_name,
            value=value,
        )
    
    if value < 0:
        raise ValidationError(
            detail=f"{field_name} must be >= 0",
            field=field_name,
            value=value,
        )
    
    return value


def sanitize_string(value: Optional[str], max_length: Optional[int] = None) -> Optional[str]:
    """
    Sanitize string input (XSS prevention, length validation).
    
    Args:
        value: String value
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
    
    # Check length
    if max_length and len(value) > max_length:
        value = value[:max_length]
    
    # Basic XSS prevention: remove script tags and dangerous characters
    # Note: For production, consider using a proper HTML sanitizer library
    dangerous_patterns = ["<script", "javascript:", "onerror=", "onload="]
    value_lower = value.lower()
    for pattern in dangerous_patterns:
        if pattern in value_lower:
            # Remove dangerous content
            import re
            value = re.sub(re.escape(pattern), "", value, flags=re.IGNORECASE)
    
    return value
