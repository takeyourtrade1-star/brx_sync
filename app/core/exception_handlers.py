"""
Exception handlers for FastAPI.

Centralized exception handling with structured error responses and logging.
"""
import logging
from typing import Any, Dict

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.trace_ids import safe_trace_id
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
    return safe_trace_id(request)


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
    
    logger.error(
        "BRX Sync request failed error_type=%s status_code=%d trace_id=%s",
        type(exc).__name__,
        exc.status_code,
        trace_id,
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
    return await brx_sync_error_handler(request, exc)


async def rate_limit_error_handler(
    request: Request,
    exc: RateLimitError,
) -> JSONResponse:
    """Handle RateLimitError exceptions."""
    trace_id = get_trace_id(request)
    
    logger.warning(
        "Rate limit exceeded trace_id=%s",
        trace_id,
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
        "Request validation failed fields=%d trace_id=%s",
        len(errors),
        trace_id,
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
        "Model validation failed fields=%d trace_id=%s",
        len(errors),
        trace_id,
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
    
    # Keep exception values out of both logs and HTTP responses: driver errors
    # can contain connection strings, SQL fragments or upstream credentials.
    logger.error(
        "Unhandled exception: %s",
        type(exc).__name__,
        extra={
            "trace_id": trace_id,
            "exception_type": type(exc).__name__,
        },
    )
    
    error_detail = "An internal error occurred"
    
    response_headers = {
        "X-Trace-Id": trace_id,
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    }
    if settings.ENVIRONMENT in {"staging", "production"}:
        response_headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "code": "INTERNAL_SERVER_ERROR",
                "message": error_detail,
                "trace_id": trace_id,
            }
        },
        headers=response_headers,
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
