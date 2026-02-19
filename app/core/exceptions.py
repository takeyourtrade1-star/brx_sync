"""
Exception hierarchy for BRX Sync microservice.

All exceptions inherit from BRXSyncError and include structured error information
for consistent error handling and logging.
"""
from typing import Any, Dict, Optional


class BRXSyncError(Exception):
    """
    Base exception for all BRX Sync errors.
    
    Attributes:
        status_code: HTTP status code for API responses
        error_code: Machine-readable error code
        detail: Human-readable error message
        context: Additional context (user_id, item_id, etc.)
    """
    
    def __init__(
        self,
        detail: str,
        status_code: int = 500,
        error_code: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize exception.
        
        Args:
            detail: Human-readable error message
            status_code: HTTP status code
            error_code: Machine-readable error code (defaults to class name)
            context: Additional context dictionary
        """
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.error_code = error_code or self.__class__.__name__
        self.context = context or {}
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert exception to dictionary for JSON responses.
        
        Returns:
            Dictionary with error information
        """
        return {
            "error": {
                "code": self.error_code,
                "message": self.detail,
                "context": self.context,
            }
        }


# Domain-specific exceptions

class SyncError(BRXSyncError):
    """Base exception for sync-related errors."""
    
    def __init__(
        self,
        detail: str,
        status_code: int = 500,
        error_code: str = "SYNC_ERROR",
        context: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(detail, status_code, error_code, context)


class SyncInProgressError(SyncError):
    """Sync operation already in progress."""
    
    def __init__(
        self,
        user_id: str,
        current_status: str,
        context: Optional[Dict[str, Any]] = None,
    ):
        detail = f"Sync already in progress for user {user_id}. Status: {current_status}"
        super().__init__(
            detail=detail,
            status_code=409,  # Conflict
            error_code="SYNC_IN_PROGRESS",
            context={"user_id": user_id, "current_status": current_status, **(context or {})},
        )


class SyncNotFoundError(SyncError):
    """Sync operation not found."""
    
    def __init__(
        self,
        user_id: str,
        context: Optional[Dict[str, Any]] = None,
    ):
        detail = f"Sync settings not found for user {user_id}"
        super().__init__(
            detail=detail,
            status_code=404,
            error_code="SYNC_NOT_FOUND",
            context={"user_id": user_id, **(context or {})},
        )


class InventoryError(BRXSyncError):
    """Base exception for inventory-related errors."""
    
    def __init__(
        self,
        detail: str,
        status_code: int = 500,
        error_code: str = "INVENTORY_ERROR",
        context: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(detail, status_code, error_code, context)


class InventoryItemNotFoundError(InventoryError):
    """Inventory item not found."""
    
    def __init__(
        self,
        item_id: int,
        user_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        detail = f"Inventory item {item_id} not found"
        if user_id:
            detail += f" for user {user_id}"
        super().__init__(
            detail=detail,
            status_code=404,
            error_code="INVENTORY_ITEM_NOT_FOUND",
            context={"item_id": item_id, "user_id": user_id, **(context or {})},
        )


class InventoryItemMissingExternalIdError(InventoryError):
    """Inventory item missing external_stock_id (CardTrader product ID)."""
    
    def __init__(
        self,
        item_id: int,
        user_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        detail = (
            f"Inventory item {item_id} missing external_stock_id. "
            "Cannot sync to CardTrader. Please run bulk sync first."
        )
        super().__init__(
            detail=detail,
            status_code=400,
            error_code="INVENTORY_ITEM_MISSING_EXTERNAL_ID",
            context={"item_id": item_id, "user_id": user_id, **(context or {})},
        )


class CardTraderAPIError(BRXSyncError):
    """Base exception for CardTrader API errors."""
    
    def __init__(
        self,
        detail: str,
        status_code: int = 502,  # Bad Gateway
        error_code: str = "CARDTRADER_API_ERROR",
        context: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(detail, status_code, error_code, context)


class RateLimitError(CardTraderAPIError):
    """Rate limit exceeded (429)."""
    
    def __init__(
        self,
        detail: str = "Rate limit exceeded",
        retry_after: Optional[float] = None,
        user_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        if retry_after:
            detail += f". Please retry after {retry_after:.2f} seconds"
        super().__init__(
            detail=detail,
            status_code=429,
            error_code="RATE_LIMIT_EXCEEDED",
            context={
                "retry_after": retry_after,
                "user_id": user_id,
                **(context or {}),
            },
        )


class CardTraderServiceUnavailableError(CardTraderAPIError):
    """CardTrader service unavailable (circuit breaker open)."""
    
    def __init__(
        self,
        detail: str = "CardTrader service temporarily unavailable",
        timeout: Optional[int] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        if timeout:
            detail += f". Retry in {timeout} seconds"
        super().__init__(
            detail=detail,
            status_code=503,  # Service Unavailable
            error_code="CARDTRADER_SERVICE_UNAVAILABLE",
            context={"timeout": timeout, **(context or {})},
        )


class ValidationError(BRXSyncError):
    """Input validation error."""
    
    def __init__(
        self,
        detail: str,
        field: Optional[str] = None,
        value: Optional[Any] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            detail=detail,
            status_code=400,
            error_code="VALIDATION_ERROR",
            context={"field": field, "value": value, **(context or {})},
        )


class NotFoundError(BRXSyncError):
    """Resource not found."""
    
    def __init__(
        self,
        resource_type: str,
        resource_id: Any,
        context: Optional[Dict[str, Any]] = None,
    ):
        detail = f"{resource_type} with id {resource_id} not found"
        super().__init__(
            detail=detail,
            status_code=404,
            error_code="NOT_FOUND",
            context={"resource_type": resource_type, "resource_id": resource_id, **(context or {})},
        )


class DatabaseError(BRXSyncError):
    """Database operation error."""
    
    def __init__(
        self,
        detail: str,
        operation: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            detail=detail,
            status_code=500,
            error_code="DATABASE_ERROR",
            context={"operation": operation, **(context or {})},
        )


class ConfigurationError(BRXSyncError):
    """Configuration error."""
    
    def __init__(
        self,
        detail: str,
        setting: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            detail=detail,
            status_code=500,
            error_code="CONFIGURATION_ERROR",
            context={"setting": setting, **(context or {})},
        )


class WebhookValidationError(BRXSyncError):
    """Webhook signature validation error."""
    
    def __init__(
        self,
        detail: str = "Webhook signature validation failed",
        context: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            detail=detail,
            status_code=401,  # Unauthorized
            error_code="WEBHOOK_VALIDATION_ERROR",
            context=context or {},
        )
