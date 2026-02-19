"""
Prometheus metrics integration for BRX Sync.

Exports metrics in Prometheus format for monitoring and alerting.
"""
from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    generate_latest,
    CONTENT_TYPE_LATEST,
)
from typing import Optional

# Application Metrics
http_requests_total = Counter(
    "brx_sync_http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status_code"],
)

http_request_duration_seconds = Histogram(
    "brx_sync_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "endpoint"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)

# Database Metrics
db_connections_active = Gauge(
    "brx_sync_db_connections_active",
    "Active database connections",
    ["database", "pool"],
)

db_connections_idle = Gauge(
    "brx_sync_db_connections_idle",
    "Idle database connections",
    ["database", "pool"],
)

db_query_duration_seconds = Histogram(
    "brx_sync_db_query_duration_seconds",
    "Database query duration in seconds",
    ["database", "operation"],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0],
)

# Celery Metrics
celery_tasks_total = Counter(
    "brx_sync_celery_tasks_total",
    "Total Celery tasks",
    ["task_name", "status"],
)

celery_task_duration_seconds = Histogram(
    "brx_sync_celery_task_duration_seconds",
    "Celery task duration in seconds",
    ["task_name"],
    buckets=[1.0, 5.0, 10.0, 30.0, 60.0, 300.0, 600.0, 1800.0],
)

celery_queue_depth = Gauge(
    "brx_sync_celery_queue_depth",
    "Celery queue depth",
    ["queue_name"],
)

# CardTrader API Metrics
cardtrader_api_requests_total = Counter(
    "brx_sync_cardtrader_api_requests_total",
    "Total CardTrader API requests",
    ["method", "endpoint", "status_code"],
)

cardtrader_api_request_duration_seconds = Histogram(
    "brx_sync_cardtrader_api_request_duration_seconds",
    "CardTrader API request duration in seconds",
    ["method", "endpoint"],
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 180.0],
)

cardtrader_rate_limit_hits = Counter(
    "brx_sync_cardtrader_rate_limit_hits_total",
    "Total CardTrader rate limit hits (429)",
    ["user_id"],
)

# Rate Limiting Metrics
rate_limiter_requests_total = Counter(
    "brx_sync_rate_limiter_requests_total",
    "Total rate limiter requests",
    ["user_id", "allowed"],
)

# Circuit Breaker Metrics
circuit_breaker_state = Gauge(
    "brx_sync_circuit_breaker_state",
    "Circuit breaker state (0=CLOSED, 1=OPEN, 2=HALF_OPEN)",
    ["service"],
)

circuit_breaker_failures_total = Counter(
    "brx_sync_circuit_breaker_failures_total",
    "Total circuit breaker failures",
    ["service", "error_type"],
)

# Sync Operations Metrics
sync_operations_total = Counter(
    "brx_sync_operations_total",
    "Total sync operations",
    ["operation_type", "status"],
)

sync_operation_duration_seconds = Histogram(
    "brx_sync_operation_duration_seconds",
    "Sync operation duration in seconds",
    ["operation_type"],
    buckets=[1.0, 5.0, 10.0, 30.0, 60.0, 300.0, 600.0, 1800.0],
)

sync_items_processed = Counter(
    "brx_sync_items_processed_total",
    "Total items processed in sync operations",
    ["operation_type", "action"],  # action: created, updated, skipped
)

# Redis Metrics
redis_operations_total = Counter(
    "brx_sync_redis_operations_total",
    "Total Redis operations",
    ["operation", "status"],
)

redis_operation_duration_seconds = Histogram(
    "brx_sync_redis_operation_duration_seconds",
    "Redis operation duration in seconds",
    ["operation"],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0],
)


def get_metrics_response():
    """
    Get Prometheus metrics in text format.
    
    Returns:
        Tuple of (metrics_text, content_type)
    """
    return generate_latest(), CONTENT_TYPE_LATEST
