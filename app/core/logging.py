"""
Structured logging configuration for BRX Sync.

Provides centralized logging with context propagation, structured JSON output,
and integration with CloudWatch/ELK.
"""
import json
import logging
import sys
from contextvars import ContextVar
from typing import Any, Dict, Optional

from app.core.config import get_settings

settings = get_settings()

# Context variables for request-scoped data
trace_id_var: ContextVar[Optional[str]] = ContextVar("trace_id", default=None)
user_id_var: ContextVar[Optional[str]] = ContextVar("user_id", default=None)
request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)


class StructuredFormatter(logging.Formatter):
    """
    JSON formatter for structured logging.
    
    Outputs logs in JSON format with context information for easy parsing
    by log aggregation systems (CloudWatch, ELK, etc.).
    """
    
    def format(self, record: logging.LogRecord) -> str:
        """
        Format log record as JSON.
        
        Args:
            record: Log record
            
        Returns:
            JSON string
        """
        # Base log data
        log_data: Dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        
        # Add context from context variables
        trace_id = trace_id_var.get()
        if trace_id:
            log_data["trace_id"] = trace_id
        
        user_id = user_id_var.get()
        if user_id:
            log_data["user_id"] = user_id
        
        request_id = request_id_var.get()
        if request_id:
            log_data["request_id"] = request_id
        
        # Add extra fields from record
        if hasattr(record, "extra"):
            log_data.update(record.extra)
        
        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]) if record.exc_info[1] else None,
                "traceback": self.formatException(record.exc_info) if record.exc_info else None,
            }
        
        # Add any additional fields from record
        for key, value in record.__dict__.items():
            if key not in (
                "name", "msg", "args", "created", "filename", "funcName",
                "levelname", "levelno", "lineno", "module", "msecs",
                "message", "pathname", "process", "processName", "relativeCreated",
                "thread", "threadName", "exc_info", "exc_text", "stack_info",
            ):
                if not key.startswith("_"):
                    log_data[key] = value
        
        return json.dumps(log_data, default=str, ensure_ascii=False)


def setup_logging() -> None:
    """
    Configure logging for the application.
    
    Sets up structured JSON logging for production and human-readable
    logging for development.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if settings.DEBUG else logging.INFO)
    
    # Remove existing handlers
    root_logger.handlers.clear()
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if settings.DEBUG else logging.INFO)
    
    if settings.DEBUG:
        # Human-readable format for development
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    else:
        # JSON format for production
        formatter = StructuredFormatter()
    
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # Set levels for third-party libraries
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("celery").setLevel(logging.INFO)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.DEBUG else logging.WARNING
    )


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance with the given name.
    
    Args:
        name: Logger name (typically __name__)
        
    Returns:
        Logger instance
    """
    return logging.getLogger(name)


class LogContext:
    """
    Context manager for adding context to logs.
    
    Usage:
        with LogContext(trace_id="abc", user_id="123"):
            logger.info("This log will include trace_id and user_id")
    """
    
    def __init__(
        self,
        trace_id: Optional[str] = None,
        user_id: Optional[str] = None,
        request_id: Optional[str] = None,
        **kwargs: Any,
    ):
        """
        Initialize log context.
        
        Args:
            trace_id: Trace ID for distributed tracing
            user_id: User ID
            request_id: Request ID
            **kwargs: Additional context fields
        """
        self.trace_id = trace_id
        self.user_id = user_id
        self.request_id = request_id
        self.additional_context = kwargs
        self._tokens: list = []
    
    def __enter__(self) -> "LogContext":
        """Enter context."""
        if self.trace_id:
            token = trace_id_var.set(self.trace_id)
            self._tokens.append(("trace_id", token))
        
        if self.user_id:
            token = user_id_var.set(self.user_id)
            self._tokens.append(("user_id", token))
        
        if self.request_id:
            token = request_id_var.set(self.request_id)
            self._tokens.append(("request_id", token))
        
        return self
    
    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit context and reset variables."""
        for var_name, token in reversed(self._tokens):
            if var_name == "trace_id":
                trace_id_var.reset(token)
            elif var_name == "user_id":
                user_id_var.reset(token)
            elif var_name == "request_id":
                request_id_var.reset(token)


def log_operation(
    logger: logging.Logger,
    operation: str,
    level: int = logging.INFO,
    **context: Any,
) -> None:
    """
    Log an operation with context.
    
    Args:
        logger: Logger instance
        operation: Operation name
        level: Log level
        **context: Additional context fields
    """
    logger.log(
        level,
        f"Operation: {operation}",
        extra={"operation": operation, **context},
    )


def log_performance(
    logger: logging.Logger,
    operation: str,
    duration_seconds: float,
    **context: Any,
) -> None:
    """
    Log operation performance.
    
    Args:
        logger: Logger instance
        operation: Operation name
        duration_seconds: Duration in seconds
        **context: Additional context fields
    """
    logger.info(
        f"Operation {operation} completed in {duration_seconds:.3f}s",
        extra={
            "operation": operation,
            "duration_seconds": duration_seconds,
            "performance": True,
            **context,
        },
    )


# Initialize logging on module import
setup_logging()
