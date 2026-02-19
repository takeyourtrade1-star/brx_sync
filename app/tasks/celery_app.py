"""
Celery application configuration with priority queues and exponential backoff.
"""
from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

settings = get_settings()

# Create Celery app
celery_app = Celery(
    "brx_sync",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.tasks.sync_tasks"],
)

# Celery configuration
celery_app.conf.update(
    # Task settings
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    
    # Queue configuration
    task_routes={
        "app.tasks.sync_tasks.process_webhook_notification": {"queue": "high-priority"},
        "app.tasks.sync_tasks.update_product_quantity": {"queue": "high-priority"},
        "app.tasks.sync_tasks.initial_bulk_sync": {"queue": "bulk-sync"},
        "app.tasks.sync_tasks.sync_update_product_to_cardtrader": {"queue": "high-priority"},
        "app.tasks.sync_tasks.sync_delete_product_to_cardtrader": {"queue": "high-priority"},
    },
    
    # Retry configuration with exponential backoff
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_time_limit=1800,  # 30 minutes max
    task_soft_time_limit=1500,  # 25 minutes soft limit
    
    # Retry settings
    task_default_retry_delay=60,  # Initial retry delay (seconds)
    task_max_retries=10,
    
    # Exponential backoff: 2^retry_count seconds, max 300s
    task_retry_backoff=True,
    task_retry_backoff_max=300,
    task_retry_jitter=True,
    
    # Result backend
    result_expires=3600,  # Results expire after 1 hour
    
    # Worker settings
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=1000,
)

# Define queues
celery_app.conf.task_queues = {
    "high-priority": {
        "exchange": "high-priority",
        "routing_key": "high-priority",
    },
    "bulk-sync": {
        "exchange": "bulk-sync",
        "routing_key": "bulk-sync",
    },
    "default": {
        "exchange": "default",
        "routing_key": "default",
    },
}
