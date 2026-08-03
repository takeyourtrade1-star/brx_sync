"""
Dependency injection functions for FastAPI.

Provides reusable dependency functions for common use cases.
"""
import uuid
from typing import Optional

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.core.exceptions import ValidationError
from app.core.logging import LogContext, get_logger
from app.core.trace_ids import safe_trace_id
from app.core.validators import validate_uuid

logger = get_logger(__name__)


def get_trace_id(
    request: Request,
) -> str:
    """
    Extract or generate trace ID from request headers.
    
    Args:
        request: Raw ASGI request (needed to reject duplicate headers)
        
    Returns:
        Trace ID string
    """
    return safe_trace_id(
        request,
        accepted_headers=("x-trace-id", "x-request-id"),
    )


def get_user_id_from_path(user_id: str) -> uuid.UUID:
    """
    Validate and parse user_id from path parameter.
    
    Args:
        user_id: User ID string from path
        
    Returns:
        Parsed UUID
        
    Raises:
        ValidationError: If user_id is invalid
    """
    return validate_uuid(user_id, field_name="user_id")


def get_log_context(
    trace_id: str = Depends(get_trace_id),
    user_id: Optional[str] = None,
) -> LogContext:
    """
    Create log context for request.
    
    Args:
        trace_id: Trace ID
        user_id: Optional user ID
        
    Returns:
        LogContext instance
    """
    return LogContext(trace_id=trace_id, user_id=user_id)


def get_db_session_dependency() -> AsyncSession:
    """
    Dependency for database session.
    
    Returns:
        Database session
    """
    # This is a wrapper to make it easier to mock in tests
    return Depends(get_db_session)
