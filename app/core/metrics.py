"""
Metrics collection for BRX Sync.

Provides counters, histograms, and gauges for monitoring application
performance and behavior.
"""
import time
from functools import wraps
from typing import Any, Callable, Dict, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)

# In-memory metrics store (in production, use Prometheus or CloudWatch)
_metrics: Dict[str, Any] = {
    "counters": {},
    "histograms": {},
    "gauges": {},
}


def increment_counter(name: str, value: int = 1, labels: Optional[Dict[str, str]] = None) -> None:
    """
    Increment a counter metric.
    
    Args:
        name: Metric name
        value: Increment value (default: 1)
        labels: Optional labels for the metric
    """
    key = f"{name}:{labels}" if labels else name
    _metrics["counters"][key] = _metrics["counters"].get(key, 0) + value
    
    logger.debug(
        f"Counter incremented: {name}",
        extra={"metric": name, "value": value, "labels": labels},
    )


def record_histogram(name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
    """
    Record a histogram value.
    
    Args:
        name: Metric name
        value: Value to record
        labels: Optional labels for the metric
    """
    key = f"{name}:{labels}" if labels else name
    if key not in _metrics["histograms"]:
        _metrics["histograms"][key] = []
    _metrics["histograms"][key].append(value)
    
    # Keep only last 1000 values
    if len(_metrics["histograms"][key]) > 1000:
        _metrics["histograms"][key] = _metrics["histograms"][key][-1000:]
    
    logger.debug(
        f"Histogram recorded: {name}",
        extra={"metric": name, "value": value, "labels": labels},
    )


def set_gauge(name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
    """
    Set a gauge value.
    
    Args:
        name: Metric name
        value: Gauge value
        labels: Optional labels for the metric
    """
    key = f"{name}:{labels}" if labels else name
    _metrics["gauges"][key] = value
    
    logger.debug(
        f"Gauge set: {name}",
        extra={"metric": name, "value": value, "labels": labels},
    )


def get_metrics() -> Dict[str, Any]:
    """
    Get all metrics.
    
    Returns:
        Dict with all metrics
    """
    # Calculate histogram statistics
    histograms_stats = {}
    for key, values in _metrics["histograms"].items():
        if values:
            histograms_stats[key] = {
                "count": len(values),
                "min": min(values),
                "max": max(values),
                "avg": sum(values) / len(values),
            }
    
    return {
        "counters": _metrics["counters"].copy(),
        "histograms": histograms_stats,
        "gauges": _metrics["gauges"].copy(),
    }


def reset_metrics() -> None:
    """Reset all metrics (for testing)."""
    _metrics["counters"].clear()
    _metrics["histograms"].clear()
    _metrics["gauges"].clear()


def measure_time(metric_name: str, labels: Optional[Dict[str, str]] = None):
    """
    Decorator to measure function execution time.
    
    Args:
        metric_name: Metric name for histogram
        labels: Optional labels
        
    Usage:
        @measure_time("sync_operation", labels={"operation": "bulk_sync"})
        def sync():
            ...
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            start_time = time.time()
            try:
                result = await func(*args, **kwargs)
                return result
            finally:
                duration = time.time() - start_time
                record_histogram(metric_name, duration, labels)
        
        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
                return result
            finally:
                duration = time.time() - start_time
                record_histogram(metric_name, duration, labels)
        
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper
    
    return decorator
