"""
Periodic sync tasks for bidirectional synchronization.

These tasks run periodically to sync changes from CardTrader
that might not come through webhooks (e.g., direct edits on CardTrader UI).
"""
import logging
import uuid
from typing import Dict, Any

from app.tasks.celery_app import celery_app
from app.services.webhook_processor import WebhookProcessor
from app.tasks.sync_tasks import run_async

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3)
def periodic_sync_from_cardtrader(
    self,
    user_id: str,
    blueprint_id: int = None,
) -> Dict[str, Any]:
    """
    Periodically sync products from CardTrader to local database.
    
    This catches changes made directly on CardTrader (not via our API)
    and ensures our local database stays in sync.
    
    Args:
        user_id: User UUID string
        blueprint_id: Optional blueprint_id to sync specific product
        
    Returns:
        Sync result
    """
    try:
        user_uuid = uuid.UUID(user_id)
        result = run_async(
            _periodic_sync_from_cardtrader_async(user_uuid, blueprint_id)
        )
        return result
    except Exception as e:
        logger.error(
            f"Error in periodic sync for user {user_id}: {e}",
            exc_info=True
        )
        raise self.retry(exc=e, countdown=300)  # Retry after 5 minutes


async def _periodic_sync_from_cardtrader_async(
    user_uuid: uuid.UUID,
    blueprint_id: int = None,
) -> Dict[str, Any]:
    """Async implementation of periodic sync."""
    processor = WebhookProcessor()
    return await processor.sync_products_from_cardtrader(
        user_uuid,
        blueprint_id=blueprint_id
    )
