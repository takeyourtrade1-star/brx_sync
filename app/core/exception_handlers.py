"""
Exception handlers for FastAPI.

Centralized exception handling with structured error responses and logging.
"""
import logging
import traceback
from typing import Any, Dict

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.exceptions import (
    BRXSyncError,
    CardTraderAPIError,
    CardTraderServiceUnavailableError,
    ConfigurationError,
    DatabaseError,
    InventoryError,
    NotFoundError,
    RateLimitError,
    SyncError,
    ValidationError as BRXValidationError,
    WebhookValidationError,
)

settings = get_settings()
logger = logging.getLogger(__name__)


def get_trace_id(request: Request) -> str:
    """
    Extract trace ID from request headers or generate one.
    
    Args:
        request: FastAPI request object
        
    Returns:
        Trace ID string
    """
    # Check for trace ID in headers (X-Trace-Id, X-Request-Id, etc.)
    trace_id = (
        request.headers.get("X-Trace-Id")
        or request.headers.get("X-Request-Id")
        or request.headers.get("X-Correlation-Id")
    )
    
    if not trace_id:
        # Generate a simple trace ID (in production, use proper UUID)
        import uuid
        trace_id = str(uuid.uuid4())
    
    return trace_id


async def brx_sync_error_handler(
    request: Request,
    exc: BRXSyncError,
) -> JSONResponse:
    """
    Handle BRXSyncError exceptions.
    
    Args:
        request: FastAPI request object
        exc: BRXSyncError exception
        
    Returns:
        JSONResponse with error details
    """
    trace_id = get_trace_id(request)
    
    # Log error with context
    logger.error(
        f"BRXSyncError: {exc.error_code} - {exc.detail}",
        extra={
            "trace_id": trace_id,
            "error_code": exc.error_code,
            "status_code": exc.status_code,
            "context": exc.context,
            "path": request.url.path,
            "method": request.method,
        },
        exc_info=settings.DEBUG,  # Include traceback only in debug mode
    )
    
    response_data = exc.to_dict()
    response_data["error"]["trace_id"] = trace_id
    
    return JSONResponse(
        status_code=exc.status_code,
        content=response_data,
        headers={"X-Trace-Id": trace_id},
    )


async def sync_error_handler(
    request: Request,
    exc: SyncError,
) -> JSONResponse:
    """Handle SyncError exceptions."""
    return await brx_sync_error_handler(request, exc)


async def inventory_error_handler(
    request: Request,
    exc: InventoryError,
) -> JSONResponse:
    """Handle InventoryError exceptions."""
    return await brx_sync_error_handler(request, exc)


async def cardtrader_api_error_handler(
    request: Request,
    exc: CardTraderAPIError,
) -> JSONResponse:
    """Handle CardTraderAPIError exceptions."""
    trace_id = get_trace_id(request)
    
    # Log with additional CardTrader context
    logger.warning(
        f"CardTrader API Error: {exc.error_code} - {exc.detail}",
        extra={
            "trace_id": trace_id,
            "error_code": exc.error_code,
            "status_code": exc.status_code,
            "context": exc.context,
            "path": request.url.path,
            "method": request.method,
            "service": "cardtrader",
        },
    )
    
    return await brx_sync_error_handler(request, exc)


async def rate_limit_error_handler(
    request: Request,
    exc: RateLimitError,
) -> JSONResponse:
    """Handle RateLimitError exceptions."""
    trace_id = get_trace_id(request)
    
    # Log rate limit with retry information
    logger.warning(
        f"Rate limit exceeded: {exc.detail}",
        extra={
            "trace_id": trace_id,
            "error_code": exc.error_code,
            "retry_after": exc.context.get("retry_after"),
            "user_id": exc.context.get("user_id"),
            "path": request.url.path,
            "method": request.method,
        },
    )
    
    response_data = exc.to_dict()
    response_data["error"]["trace_id"] = trace_id
    
    # Add Retry-After header if available
    headers = {"X-Trace-Id": trace_id}
    retry_after = exc.context.get("retry_after")
    if retry_after:
        headers["Retry-After"] = str(int(retry_after))
    
    return JSONResponse(
        status_code=exc.status_code,
        content=response_data,
        headers=headers,
    )


async def validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """
    Handle Pydantic validation errors from FastAPI.
    
    Args:
        request: FastAPI request object
        exc: RequestValidationError exception
        
    Returns:
        JSONResponse with validation error details
    """
    trace_id = get_trace_id(request)
    
    # Format validation errors
    errors = []
    for error in exc.errors():
        field = ".".join(str(loc) for loc in error.get("loc", []))
        errors.append({
            "field": field,
            "message": error.get("msg"),
            "type": error.get("type"),
        })
    
    logger.warning(
        f"Validation error: {len(errors)} field(s) failed validation",
        extra={
            "trace_id": trace_id,
            "errors": errors,
            "path": request.url.path,
            "method": request.method,
        },
    )
    
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed",
                "errors": errors,
                "trace_id": trace_id,
            }
        },
        headers={"X-Trace-Id": trace_id},
    )


async def pydantic_validation_error_handler(
    request: Request,
    exc: ValidationError,
) -> JSONResponse:
    """
    Handle Pydantic ValidationError (from model validation).
    
    Args:
        request: FastAPI request object
        exc: Pydantic ValidationError
        
    Returns:
        JSONResponse with validation error details
    """
    trace_id = get_trace_id(request)
    
    errors = []
    for error in exc.errors():
        field = ".".join(str(loc) for loc in error.get("loc", []))
        errors.append({
            "field": field,
            "message": error.get("msg"),
            "type": error.get("type"),
        })
    
    logger.warning(
        f"Pydantic validation error: {len(errors)} field(s) failed validation",
        extra={
            "trace_id": trace_id,
            "errors": errors,
            "path": request.url.path,
            "method": request.method,
        },
    )
    
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Model validation failed",
                "errors": errors,
                "trace_id": trace_id,
            }
        },
        headers={"X-Trace-Id": trace_id},
    )


async def generic_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """
    Handle unexpected exceptions.
    
    Args:
        request: FastAPI request object
        exc: Exception
        
    Returns:
        JSONResponse with generic error message
    """
    trace_id = get_trace_id(request)
    
    # Log full exception with traceback
    logger.error(
        f"Unhandled exception: {type(exc).__name__}: {str(exc)}",
        extra={
            "trace_id": trace_id,
            "exception_type": type(exc).__name__,
            "path": request.url.path,
            "method": request.method,
        },
        exc_info=True,  # Always include traceback for unhandled exceptions
    )
    
    error_detail = str(exc) if settings.DEBUG else "An internal error occurred"
    
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "code": "INTERNAL_SERVER_ERROR",
                "message": error_detail,
                "trace_id": trace_id,
            }
        },
        headers={"X-Trace-Id": trace_id},
    )


# Exception handler mapping
EXCEPTION_HANDLERS: Dict[Any, Any] = {
    # BRX Sync exceptions
    BRXSyncError: brx_sync_error_handler,
    SyncError: sync_error_handler,
    InventoryError: inventory_error_handler,
    CardTraderAPIError: cardtrader_api_error_handler,
    RateLimitError: rate_limit_error_handler,
    CardTraderServiceUnavailableError: cardtrader_api_error_handler,
    ValidationError: brx_sync_error_handler,
    NotFoundError: brx_sync_error_handler,
    DatabaseError: brx_sync_error_handler,
    ConfigurationError: brx_sync_error_handler,
    WebhookValidationError: brx_sync_error_handler,
    BRXValidationError: brx_sync_error_handler,
    # FastAPI/Pydantic exceptions
    RequestValidationError: validation_error_handler,
    ValidationError: pydantic_validation_error_handler,
    # Generic exception (must be last)
    Exception: generic_exception_handler,
}
