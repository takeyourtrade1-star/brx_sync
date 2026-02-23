"""
Celery tasks for synchronizing inventory between Ebartex and CardTrader.
"""
import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

# Note: nest_asyncio is NOT applied at module level to avoid conflicts with uvloop.
# We use isolated event loops in run_async() instead.

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import get_encryption_manager
from app.core.database import get_db_session_context, get_isolated_db_session
from app.models.inventory import (
    SyncStatusEnum,
    SyncOperation,
    UserInventoryItem,
    UserSyncSettings,
)
from app.services.blueprint_mapper import get_blueprint_mapper
from app.services.cardtrader_client import (
    CardTraderAPIError,
    CardTraderClient,
    RateLimitError,
)
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)
CHUNK_SIZE = 5000


def _log_to_file(message: str, data: dict = None):
    """Helper to log to file safely. Disabled when SYNC_LOG_TO_FILE=False (recommended in production with many workers to avoid file contention)."""
    from app.core.config import get_settings
    if not get_settings().SYNC_LOG_TO_FILE:
        return
    import json
    import os
    from datetime import datetime

    log_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "logs",
        "brx_sync.log",
    )
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    log_entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "message": message,
        "data": data or {},
    }
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")
    except Exception:
        pass


def run_async(coro):
    """
    Safely run async code in Celery tasks.
    Uses asyncio.run() which creates a new event loop, runs the coroutine,
    and properly cleans up all async resources (including SQLAlchemy connections)
    before closing the loop. This prevents "Task attached to different loop" errors.
    """
    _log_to_file("Running async coroutine with asyncio.run()")
    
    try:
        # Use asyncio.run() which creates a new loop, runs the coro, and cleans up properly
        # This is safer than manually managing the loop lifecycle
        # asyncio.run() ensures all async resources are properly disposed before closing
        result = asyncio.run(coro)
        _log_to_file("Coroutine completed successfully")
        return result
    except Exception as e:
        _log_to_file("Error in coroutine", {
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": str(e.__traceback__) if hasattr(e, '__traceback__') else None
        })
        raise


@celery_app.task(bind=True, max_retries=10, default_retry_delay=60)
def initial_bulk_sync(self, user_id: str) -> Dict[str, Any]:
    """
    Initial bulk sync: export all products from CardTrader and populate PostgreSQL.
    
    Args:
        user_id: User UUID as string
        
    Returns:
        Dict with sync results
    """
    user_uuid = uuid.UUID(user_id)
    # Use Celery task id so GET /task/{task_id} can verify ownership via SyncOperation.operation_id
    operation_id = self.request.id

    # Create SyncOperation immediately so get_task_status can verify ownership before async work runs
    from app.core.database import get_sync_db_engine
    from sqlalchemy import text
    try:
        engine = get_sync_db_engine()
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO sync_operations (user_id, operation_id, operation_type, status)
                    VALUES (CAST(:user_id AS uuid), :operation_id, 'bulk_sync', 'pending')
                    ON CONFLICT (operation_id) DO NOTHING
                """),
                {"user_id": str(user_uuid), "operation_id": operation_id}
            )
    except Exception as e:
        logger.warning(f"Could not pre-create SyncOperation for task {operation_id}: {e}")
        # Continue anyway; async path will create it (may cause brief 403 on early polls)

    try:
        # Run async code in sync context - use helper to avoid event loop conflicts
        result = run_async(_initial_bulk_sync_async(user_uuid, operation_id))
        return result
    except RateLimitError as e:
        # Retry with exponential backoff
        logger.warning(f"Rate limit error in bulk sync for user {user_id}: {e}")
        raise self.retry(exc=e, countdown=min(300, 2 ** self.request.retries))
    except Exception as e:
        logger.error(f"Error in bulk sync for user {user_id}: {e}", exc_info=True)
        # Update sync status to error - use sync database connection to avoid event loop issues
        try:
            # Use sync database connection to update status without async
            from app.core.database import get_sync_db_engine
            from sqlalchemy import text
            
            engine = get_sync_db_engine()
            with engine.begin() as conn:  # begin() automatically commits or rolls back
                conn.execute(
                    text("""
                        UPDATE user_sync_settings 
                        SET sync_status = CAST(:status AS sync_status_enum),
                            last_error = :error,
                            updated_at = NOW()
                        WHERE user_id = CAST(:user_id AS uuid)
                    """),
                    {
                        "status": SyncStatusEnum.ERROR.value,
                        "error": str(e),
                        "user_id": str(user_uuid)
                    }
                )
            logger.info(f"Updated sync status to error for user {user_uuid}")
        except Exception as update_error:
            logger.error(f"Failed to update sync status to error: {update_error}", exc_info=True)
        raise


async def _initial_bulk_sync_async(
    user_uuid: uuid.UUID,
    operation_id: str
) -> Dict[str, Any]:
    """Async implementation of bulk sync."""
    encryption_manager = get_encryption_manager()
    blueprint_mapper = get_blueprint_mapper()
    
    async with get_db_session_context() as session:
        # Get user sync settings
        stmt = select(UserSyncSettings).where(UserSyncSettings.user_id == user_uuid)
        result = await session.execute(stmt)
        sync_settings = result.scalar_one_or_none()
        
        if not sync_settings:
            raise ValueError(f"User sync settings not found for user {user_uuid}")
        
        # Decrypt token
        token = encryption_manager.decrypt(sync_settings.cardtrader_token_encrypted)
        
        # Update status to initial_sync - use update statement with cast for PostgreSQL enum
        from sqlalchemy import cast
        from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
        
        update_stmt = (
            update(UserSyncSettings)
            .where(UserSyncSettings.user_id == user_uuid)
            .values(
                sync_status=cast(SyncStatusEnum.INITIAL_SYNC.value, PG_ENUM(SyncStatusEnum, name="sync_status_enum"))
            )
        )
        await session.execute(update_stmt)
        await session.commit()
        
        # Load SyncOperation (created at task start) for progress/metadata updates
        stmt_op = select(SyncOperation).where(SyncOperation.operation_id == operation_id)
        res_op = await session.execute(stmt_op)
        sync_op = res_op.scalar_one_or_none()
        
        try:
            # Initialize CardTrader client
            async with CardTraderClient(token, str(user_uuid)) as client:
                # Export all products
                logger.info(f"Starting bulk export for user {user_uuid}")
                products = await client.get_products_export()
                logger.info(f"Exported {len(products)} products from CardTrader")
                
                # Process in chunks with optimized commit strategy and parallelization
                total_processed = 0
                total_created = 0
                total_updated = 0
                total_skipped = 0
                
                total_chunks = (len(products) + CHUNK_SIZE - 1) // CHUNK_SIZE
                chunks = [
                    products[i:i + CHUNK_SIZE]
                    for i in range(0, len(products), CHUNK_SIZE)
                ]
                
                # Process chunks in parallel batches (3-5 at a time)
                # This significantly speeds up processing while not overwhelming the DB
                PARALLEL_CHUNKS = 3
                
                for batch_start in range(0, len(chunks), PARALLEL_CHUNKS):
                    batch_chunks = chunks[batch_start:batch_start + PARALLEL_CHUNKS]
                    batch_indices = range(batch_start, min(batch_start + PARALLEL_CHUNKS, len(chunks)))
                    
                    # Process chunks in parallel (each chunk uses its own isolated DB session)
                    chunk_tasks = [
                        _process_products_chunk(
                            user_uuid, chunk, blueprint_mapper
                        )
                        for chunk in batch_chunks
                    ]
                    
                    batch_results = await asyncio.gather(*chunk_tasks)
                    
                    # Aggregate results
                    for idx, chunk_result in zip(batch_indices, batch_results):
                        total_processed += chunk_result["processed"]
                        total_created += chunk_result["created"]
                        total_updated += chunk_result["updated"]
                        total_skipped += chunk_result["skipped"]
                        
                        logger.info(
                            f"Processed chunk {idx + 1}/{total_chunks}: "
                            f"{chunk_result['processed']} items "
                            f"(+{chunk_result['created']} created, "
                            f"+{chunk_result['updated']} updated, "
                            f"{chunk_result['skipped']} skipped)"
                        )
                    
                    # Update progress in sync operation (using main session)
                    if sync_op:
                        progress_pct = int((batch_start + len(batch_chunks)) / total_chunks * 100)
                        sync_op.operation_metadata = {
                            "total_products": len(products),
                            "total_chunks": total_chunks,
                            "processed_chunks": batch_start + len(batch_chunks),
                            "progress_percent": progress_pct,
                            "processed": total_processed,
                            "created": total_created,
                            "updated": total_updated,
                            "skipped": total_skipped,
                        }
                    await session.commit()
                
                # Update sync status - use update statement with cast for PostgreSQL enum
                from sqlalchemy import cast
                from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
                
                update_stmt = (
                    update(UserSyncSettings)
                    .where(UserSyncSettings.user_id == user_uuid)
                    .values(
                        sync_status=cast(SyncStatusEnum.ACTIVE.value, PG_ENUM(SyncStatusEnum, name="sync_status_enum")),
                        last_sync_at=datetime.utcnow(),
                        last_error=None
                    )
                )
                await session.execute(update_stmt)
                
                # Update sync operation (sync_op loaded above)
                if sync_op:
                    sync_op.status = "completed"
                    sync_op.completed_at = datetime.utcnow()
                    sync_op.operation_metadata = {
                        "total_products": len(products),
                        "processed": total_processed,
                        "created": total_created,
                        "updated": total_updated,
                        "skipped": total_skipped,
                    }
                
                await session.commit()
                
                return {
                    "status": "completed",
                    "total_products": len(products),
                    "processed": total_processed,
                    "created": total_created,
                    "updated": total_updated,
                    "skipped": total_skipped,
                }
                
        except Exception as e:
            # Update sync status to error - try with async session first, fallback to sync
            try:
                from sqlalchemy import cast
                from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
                
                update_stmt = (
                    update(UserSyncSettings)
                    .where(UserSyncSettings.user_id == user_uuid)
                    .values(
                        sync_status=cast(SyncStatusEnum.ERROR.value, PG_ENUM(SyncStatusEnum, name="sync_status_enum")),
                        last_error=str(e)
                    )
                )
                await session.execute(update_stmt)
                await session.commit()
            except Exception as update_error:
                # If async update fails, use sync connection as fallback
                logger.warning(f"Failed to update error status with async session: {update_error}")
                try:
                    from app.core.database import get_sync_db_engine
                    from sqlalchemy import text
                    
                    engine = get_sync_db_engine()
                    with engine.begin() as conn:
                        conn.execute(
                            text("""
                                UPDATE user_sync_settings 
                                SET sync_status = CAST(:status AS sync_status_enum),
                                    last_error = :error,
                                    updated_at = NOW()
                                WHERE user_id = CAST(:user_id AS uuid)
                            """),
                            {
                                "status": SyncStatusEnum.ERROR.value,
                                "error": str(e),
                                "user_id": str(user_uuid)
                            }
                        )
                    logger.info(f"Updated sync status to error using sync connection for user {user_uuid}")
                except Exception as sync_update_error:
                    logger.error(f"Failed to update error status even with sync connection: {sync_update_error}", exc_info=True)
            raise


async def _process_products_chunk(
    user_uuid: uuid.UUID,
    products: List[Dict[str, Any]],
    blueprint_mapper,
) -> Dict[str, int]:
    """
    Process a chunk of products using optimized batch operations.
    
    This function uses:
    - Batch SELECT to find existing items (single query instead of N queries)
    - Bulk INSERT/UPDATE operations for maximum performance
    - Isolated DB session to prevent race conditions with parallel chunks
    """
    from sqlalchemy import tuple_
    from app.core.database import get_isolated_db_session
    
    created = 0
    updated = 0
    skipped = 0
    
    # Step 1: Filter and prepare products
    valid_products = []
    blueprint_ids = []
    
    for product in products:
        blueprint_id = product.get("blueprint_id")
        product_id = product.get("id")
        
        if not blueprint_id or not product_id:
            logger.debug(
                "Sync skip: prodotto senza blueprint_id o id (blueprint_id=%s, product_id=%s)",
                blueprint_id, product_id
            )
            skipped += 1
            continue
        
        valid_products.append({
            "blueprint_id": blueprint_id,
            "external_stock_id": str(product_id),
            "quantity": product.get("quantity", 0),
            "price_cents": product.get("price_cents", 0),
            "properties": product.get("properties_hash", {}),
        })
        blueprint_ids.append(blueprint_id)
    
    if not valid_products:
        return {
            "processed": len(products),
            "created": 0,
            "updated": 0,
            "skipped": skipped,
        }
    
    # Step 2: Batch map blueprint_ids
    mappings = blueprint_mapper.batch_map_blueprint_ids(blueprint_ids)
    
    # Step 3: Filter products that have valid blueprint mappings (escludi One Piece per ora)
    products_to_process = []
    for product in valid_products:
        blueprint_id = product["blueprint_id"]
        mapping = mappings.get(blueprint_id)
        # mapping is (print_id, table_name); escludi op_prints (One Piece)
        if mapping and mapping[1] != "op_prints":
            products_to_process.append(product)
        else:
            reason = "One Piece (op_prints)" if mapping and mapping[1] == "op_prints" else "nessun mapping nel catalogo"
            logger.info(
                "Sync skip: blueprint_id=%s external_stock_id=%s — %s",
                blueprint_id, product.get("external_stock_id"), reason
            )
            skipped += 1
    
    if not products_to_process:
        return {
            "processed": len(products),
            "created": 0,
            "updated": 0,
            "skipped": skipped,
        }
    
    # Step 4–8: Use isolated DB session for this chunk (prevents race conditions with parallel chunks)
    async with get_isolated_db_session() as session:
        # Batch SELECT to find existing items (ONE query instead of N)
        lookup_keys = [
            (user_uuid, p["blueprint_id"], p["external_stock_id"])
            for p in products_to_process
        ]
        existing_items_stmt = select(
            UserInventoryItem.id,
            UserInventoryItem.blueprint_id,
            UserInventoryItem.external_stock_id,
        ).where(
            tuple_(
                UserInventoryItem.user_id,
                UserInventoryItem.blueprint_id,
                UserInventoryItem.external_stock_id,
            ).in_(lookup_keys)
        )
        result = await session.execute(existing_items_stmt)
        existing_items = result.all()
        existing_keys = {
            (item.blueprint_id, item.external_stock_id): item.id
            for item in existing_items
        }
        # Step 5: Separate products into INSERT and UPDATE batches
        items_to_insert = []
        items_to_update = []
        now = datetime.utcnow()
        for product in products_to_process:
            key = (product["blueprint_id"], product["external_stock_id"])
            if key in existing_keys:
                items_to_update.append({
                    "id": existing_keys[key],
                    "quantity": product["quantity"],
                    "price_cents": product["price_cents"],
                    "properties": product["properties"],
                    "external_stock_id": product["external_stock_id"],
                    "updated_at": now,
                })
            else:
                items_to_insert.append({
                    "user_id": user_uuid,
                    "blueprint_id": product["blueprint_id"],
                    "quantity": product["quantity"],
                    "price_cents": product["price_cents"],
                    "properties": product["properties"],
                    "external_stock_id": product["external_stock_id"],
                    "created_at": now,
                    "updated_at": now,
                })
        from sqlalchemy import insert

        if items_to_insert:
            # Nuova sintassi per bulk insert in SQLAlchemy 2.0 Async
            await session.execute(insert(UserInventoryItem), items_to_insert)
            created = len(items_to_insert)

        if items_to_update:
            # Nuova sintassi per bulk update in SQLAlchemy 2.0 Async
            for item_data in items_to_update:
                item_id = item_data.pop('id')
                stmt = update(UserInventoryItem).where(UserInventoryItem.id == item_id).values(**item_data)
                await session.execute(stmt)
            updated = len(items_to_update)
        # commit is done by get_isolated_db_session context

    return {
        "processed": len(products),
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }


async def _update_sync_status(
    user_uuid: uuid.UUID,
    status: str,
    error: Optional[str] = None,
) -> None:
    """Update sync status for user."""
    async with get_db_session_context() as session:
        # Use cast to ensure PostgreSQL enum type
        from sqlalchemy import cast
        from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
        
        stmt = (
            update(UserSyncSettings)
            .where(UserSyncSettings.user_id == user_uuid)
            .values(
                sync_status=cast(status, PG_ENUM(SyncStatusEnum, name="sync_status_enum")),
                last_error=error,
                updated_at=datetime.utcnow(),
            )
        )
        await session.execute(stmt)
        await session.commit()




@celery_app.task(bind=True, max_retries=5, default_retry_delay=30)
def update_product_quantity(
    self,
    user_id: str,
    external_stock_id: str,
    delta: int,
) -> Dict[str, Any]:
    """
    Update product quantity by delta.
    
    Args:
        user_id: User UUID as string
        external_stock_id: CardTrader product.id
        delta: Quantity change (positive or negative)
        
    Returns:
        Dict with update result
    """
    user_uuid = uuid.UUID(user_id)
    
    try:
        result = run_async(
            _update_product_quantity_async(user_uuid, external_stock_id, delta)
        )
        return result
    except Exception as e:
        logger.error(
            f"Error updating product quantity for user {user_id}, "
            f"product {external_stock_id}: {e}",
            exc_info=True,
        )
        raise self.retry(exc=e, countdown=min(300, 2 ** self.request.retries))


async def _update_product_quantity_async(
    user_uuid: uuid.UUID,
    external_stock_id: str,
    delta: int,
) -> Dict[str, Any]:
    """Async implementation of product quantity update."""
    async with get_db_session_context() as session:
        stmt = select(UserInventoryItem).where(
            UserInventoryItem.user_id == user_uuid,
            UserInventoryItem.external_stock_id == external_stock_id,
        )
        result = await session.execute(stmt)
        item = result.scalar_one_or_none()
        
        if not item:
            return {"status": "not_found", "external_stock_id": external_stock_id}
        
        old_quantity = item.quantity
        new_quantity = max(0, old_quantity + delta)
        item.quantity = new_quantity
        item.updated_at = datetime.utcnow()
        
        await session.commit()
        
        return {
            "status": "updated",
            "external_stock_id": external_stock_id,
            "old_quantity": old_quantity,
            "new_quantity": new_quantity,
            "delta": delta,
        }


@celery_app.task(bind=True, max_retries=3, default_retry_delay=10)
def process_webhook_notification(
    self,
    webhook_id: str,
    payload: Dict[str, Any],
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Process webhook notification from CardTrader (order create/update).
    
    Args:
        webhook_id: Webhook UUID
        payload: Webhook payload with order data
        user_id: Optional user UUID (if provided in URL path, otherwise extracted from payload)
        
    Returns:
        Dict with processing result
    """
    try:
        result = run_async(
            _process_webhook_notification_async(webhook_id, payload, user_id)
        )
        return result
    except Exception as e:
        logger.error(f"Error processing webhook {webhook_id}: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=min(60, 2 ** self.request.retries))


async def _process_webhook_notification_async(
    webhook_id: str,
    payload: Dict[str, Any],
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Async implementation of webhook processing.
    
    Uses the WebhookProcessor for better organization and error handling.
    
    Args:
        webhook_id: Webhook UUID
        payload: Webhook payload
        user_id: Optional user UUID (from URL path or extracted from payload)
    """
    from app.services.webhook_processor import WebhookProcessor
    
    processor = WebhookProcessor()
    return await processor.process_order_webhook(webhook_id, payload, user_id)


@celery_app.task(bind=True, max_retries=5, default_retry_delay=30)
def sync_update_product_to_cardtrader(
    self,
    user_id: str,
    item_id: int,
    price_cents: Optional[int] = None,
    quantity: Optional[int] = None,
    description: Optional[str] = None,
    user_data_field: Optional[str] = None,
    graded: Optional[bool] = None,
    properties: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Synchronize product update to CardTrader.
    
    Args:
        user_id: User UUID as string
        item_id: Inventory item ID
        price_cents: New price in cents (optional, will read from DB if None)
        quantity: New quantity (optional, will read from DB if None)
        description: New description (optional, will read from DB if None)
        user_data_field: New user_data_field (optional, will read from DB if None)
        graded: New graded value (optional, will read from DB if None)
        properties: New properties dict (optional, will read from DB if None)
        
    Returns:
        Dict with sync result
    """
    _log_to_file("Celery task sync_update_product_to_cardtrader started", {
        "user_id": user_id,
        "item_id": item_id,
        "price_cents": price_cents,
        "quantity": quantity,
        "description": description,
        "user_data_field": user_data_field,
        "graded": graded,
        "properties": properties,
        "task_id": self.request.id
    })
    
    user_uuid = uuid.UUID(user_id)
    
    try:
        result = run_async(
            _sync_update_product_async(
                user_uuid, item_id, price_cents, quantity,
                description, user_data_field, graded, properties
            )
        )
        _log_to_file("Celery task sync_update_product_to_cardtrader completed", {
            "user_id": user_id,
            "item_id": item_id,
            "result": result
        })
        return result
    except RateLimitError as e:
        logger.warning(f"Rate limit error syncing product update: {e}")
        _log_to_file("Rate limit error", {"error": str(e), "task_id": self.request.id})
        raise self.retry(exc=e, countdown=min(300, 2 ** self.request.retries))
    except Exception as e:
        error_msg = f"Error syncing product update for user {user_id}, item {item_id}: {e}"
        logger.error(error_msg, exc_info=True)
        _log_to_file("Error in sync_update_product_to_cardtrader", {
            "error": str(e),
            "error_type": type(e).__name__,
            "user_id": user_id,
            "item_id": item_id,
            "task_id": self.request.id
        })
        raise


async def _sync_update_product_async(
    user_uuid: uuid.UUID,
    item_id: int,
    price_cents: Optional[int],
    quantity: Optional[int],
    description: Optional[str] = None,
    user_data_field: Optional[str] = None,
    graded: Optional[bool] = None,
    properties: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Async implementation of product update sync."""
    encryption_manager = get_encryption_manager()
    
    # Use isolated session to avoid event loop conflicts
    async with get_isolated_db_session() as session:
        # Get inventory item
        stmt = select(UserInventoryItem).where(
            UserInventoryItem.id == item_id,
            UserInventoryItem.user_id == user_uuid,
        )
        result = await session.execute(stmt)
        item = result.scalar_one_or_none()
        
        if not item:
            raise ValueError(f"Inventory item {item_id} not found")
        
        # CRITICAL DEBUG: Log what we read from DB
        print(f"\n{'='*80}")
        print(f"📖 CELERY TASK: Reading item {item_id} from DB")
        print(f"📖 Item properties from DB: {item.properties}")
        print(f"📖 Condition in DB: {item.properties.get('condition') if item.properties else 'NO PROPERTIES'}")
        print(f"{'='*80}\n")
        
        logger.warning(
            f"📖 CELERY TASK - item_id={item_id}, "
            f"properties_from_db={item.properties}, "
            f"condition_from_db={item.properties.get('condition') if item.properties else None}"
        )
        
        # Get user sync settings for token
        stmt = select(UserSyncSettings).where(UserSyncSettings.user_id == user_uuid)
        result = await session.execute(stmt)
        sync_settings = result.scalar_one_or_none()
        
        if not sync_settings:
            raise ValueError(f"User sync settings not found for user {user_uuid}")
        
        # Decrypt token
        token = encryption_manager.decrypt(sync_settings.cardtrader_token_encrypted)
        
        # Check if we have external_stock_id (CardTrader product ID)
        if not item.external_stock_id:
            logger.warning(
                f"Inventory item {item_id} has no external_stock_id, "
                "cannot sync to CardTrader. Item may need to be re-synced."
            )
            return {
                "status": "skipped",
                "reason": "no_external_stock_id",
                "message": "Item has no CardTrader product ID"
            }
        
        # Prepare update data for CardTrader
        # Use values from parameters if provided, otherwise use current values from database
        update_data = {"id": int(item.external_stock_id)}
        
        # Use provided values or fall back to database values
        final_price_cents = price_cents if price_cents is not None else item.price_cents
        final_quantity = quantity if quantity is not None else item.quantity
        final_description = description if description is not None else item.description
        final_user_data_field = user_data_field if user_data_field is not None else item.user_data_field
        final_graded = graded if graded is not None else item.graded
        final_properties = properties if properties is not None else item.properties
        
        # Log properties for debugging
        _log_to_file("Properties before filtering", {
            "item_id": item_id,
            "final_properties": final_properties,
            "properties_type": type(final_properties).__name__ if final_properties else "None",
            "has_condition": "condition" in final_properties if final_properties else False,
            "condition_value": final_properties.get("condition") if final_properties and "condition" in final_properties else None
        })
        
        logger.info(
            f"Sync update for item {item_id}: "
            f"properties={final_properties}, "
            f"has_condition={'condition' in final_properties if final_properties else False}, "
            f"condition_value={final_properties.get('condition') if final_properties and 'condition' in final_properties else None}"
        )
        
        # Always send price and quantity to ensure CardTrader is in sync
        update_data["price"] = final_price_cents / 100.0  # Convert cents to currency
        update_data["quantity"] = final_quantity
        
        # Add description if present
        if final_description is not None:
            update_data["description"] = final_description
        
        # Add user_data_field if present
        if final_user_data_field is not None:
            update_data["user_data_field"] = final_user_data_field
        
        # Add graded (top-level field, not inside properties)
        if final_graded is not None:
            update_data["graded"] = final_graded
        
        # Import property validation functions
        from app.core.cardtrader_properties import (
            validate_and_normalize_properties,
            filter_properties_for_cardtrader,
            normalize_condition,
        )
        
        # Include properties if present (e.g., condition, signed, altered, etc.)
        # CardTrader expects properties inside a "properties" object
        if final_properties:
            # First, normalize and validate properties
            # This will normalize condition values, validate booleans, etc.
            normalized_properties = validate_and_normalize_properties(
                final_properties,
                strict=False  # Non-strict: skip invalid values instead of raising
            )
            
            # Then filter out read-only and top-level properties
            properties_to_send = filter_properties_for_cardtrader(
                normalized_properties,
                include_read_only=False
            )
            
            # CRITICAL: Ensure condition is normalized and included if present
            if "condition" in final_properties:
                original_condition = final_properties["condition"]
                normalized_condition = normalize_condition(original_condition)
                if normalized_condition:
                    properties_to_send["condition"] = normalized_condition
                    if original_condition != normalized_condition:
                        logger.info(
                            f"Normalized condition for item {item_id}: "
                            f"'{original_condition}' -> '{normalized_condition}'"
                        )
                else:
                    logger.warning(
                        f"Invalid condition value for item {item_id}: '{original_condition}'. "
                        f"Valid values: Mint, Near Mint, Slightly Played, Moderately Played, "
                        f"Played, Heavily Played, Poor"
                    )
            
            # Boolean properties: signed and altered can be sent as true or false.
            # mtg_foil: CardTrader ignores "mtg_foil: false" ("Not allowed value false for mtg_foil has been ignored").
            # To remove foil you must OMIT mtg_foil from the payload; send mtg_foil only when True.
            for bool_prop in ("signed", "altered"):
                if bool_prop in final_properties:
                    value = final_properties[bool_prop]
                    if isinstance(value, bool):
                        bool_val = value
                    elif isinstance(value, str):
                        bool_val = value.lower() in ("true", "1", "yes", "on")
                    else:
                        bool_val = bool(value)
                    properties_to_send[bool_prop] = bool_val
            if "mtg_foil" in final_properties:
                value = final_properties["mtg_foil"]
                if isinstance(value, bool):
                    foil_val = value
                elif isinstance(value, str):
                    foil_val = value.lower() in ("true", "1", "yes", "on")
                else:
                    foil_val = bool(value)
                if foil_val:
                    properties_to_send["mtg_foil"] = True  # Only send when True
                else:
                    properties_to_send.pop("mtg_foil", None)  # Omit = non-foil (CardTrader ignores false)
            elif properties_to_send.get("mtg_foil") is False:
                properties_to_send.pop("mtg_foil", None)  # In case filter added it; never send false
            
            # CRITICAL: Always include mtg_language if present
            if "mtg_language" in final_properties:
                lang_value = final_properties["mtg_language"]
                if isinstance(lang_value, str) and lang_value.strip():
                    properties_to_send["mtg_language"] = lang_value.strip()[:2].lower()
            
            # Always send properties if we have any, even if it's just booleans set to False
            if properties_to_send:
                update_data["properties"] = properties_to_send
                print(f"\n{'='*80}")
                print(f"📤 SENDING TO CARDTRADER - Item {item_id}")
                print(f"Properties to send: {properties_to_send}")
                print(f"Has condition: {'condition' in properties_to_send}")
                print(f"Condition value: {properties_to_send.get('condition')}")
                print(f"Has signed: {'signed' in properties_to_send}")
                print(f"Has altered: {'altered' in properties_to_send}")
                print(f"mtg_foil: {'sent=True' if properties_to_send.get('mtg_foil') else 'omitted (non-foil)'}")
                print(f"Has mtg_language: {'mtg_language' in properties_to_send}")
                print(f"{'='*80}\n")
                
                _log_to_file("Properties to send to CardTrader", {
                    "item_id": item_id,
                    "properties": properties_to_send,
                    "has_condition": "condition" in properties_to_send,
                    "condition_value": properties_to_send.get("condition"),
                    "has_signed": "signed" in properties_to_send,
                    "signed_value": properties_to_send.get("signed"),
                    "has_altered": "altered" in properties_to_send,
                    "altered_value": properties_to_send.get("altered"),
                    "mtg_foil_sent": "mtg_foil" in properties_to_send,
                    "mtg_foil_value": properties_to_send.get("mtg_foil"),
                    "has_mtg_language": "mtg_language" in properties_to_send,
                    "mtg_language_value": properties_to_send.get("mtg_language"),
                })
                
                logger.warning(
                    f"📤 SENDING TO CARDTRADER - item_id={item_id}, "
                    f"properties={properties_to_send}, "
                    f"condition={properties_to_send.get('condition')}, "
                    f"signed={properties_to_send.get('signed')}, "
                    f"altered={properties_to_send.get('altered')}, "
                    f"mtg_foil={'sent' if 'mtg_foil' in properties_to_send else 'omitted'}, "
                    f"mtg_language={properties_to_send.get('mtg_language')}"
                )
            else:
                print(f"\n{'='*80}")
                print(f"⚠️ NO PROPERTIES TO SEND - Item {item_id}")
                print(f"Final properties from DB: {final_properties}")
                print(f"Normalized properties: {normalized_properties if 'normalized_properties' in locals() else 'N/A'}")
                print(f"Condition in final_properties: {'condition' in final_properties if final_properties else False}")
                print(f"Condition value: {final_properties.get('condition') if final_properties else None}")
                print(f"{'='*80}\n")
                
                _log_to_file("No properties to send to CardTrader", {
                    "item_id": item_id,
                    "final_properties": final_properties,
                    "normalized_properties": normalized_properties if 'normalized_properties' in locals() else None,
                    "has_condition": "condition" in final_properties if final_properties else False,
                    "condition_value": final_properties.get("condition") if final_properties else None,
                    "reason": "All properties filtered out or empty"
                })
                
                logger.warning(
                    f"⚠️ NO PROPERTIES TO SEND - item_id={item_id}, "
                    f"final_properties={final_properties}, "
                    f"has_condition={'condition' in final_properties if final_properties else False}, "
                    f"condition_value={final_properties.get('condition') if final_properties else None}"
                )
        
        # Ensure graded is not in properties (graded is top-level). Keep both foil and mtg_foil for CardTrader.
        if "properties" in update_data:
            props = update_data["properties"]
            props.pop("graded", None)
            if not props:
                del update_data["properties"]
        
        # Update on CardTrader
        _log_to_file("Calling CardTrader bulk_update_products", {
            "item_id": item_id,
            "external_stock_id": item.external_stock_id,
            "update_data": update_data
        })
        
        async with CardTraderClient(token, str(user_uuid)) as client:
            # Use bulk_update (CardTrader supports single product updates via bulk_update)
            job_result = await client.bulk_update_products([update_data])
            job_uuid = job_result.get("job")
            
            _log_to_file("CardTrader bulk_update_products response", {
                "item_id": item_id,
                "external_stock_id": item.external_stock_id,
                "job_uuid": job_uuid,
                "job_result": job_result
            })
            
            # No polling: CardTrader rate limit (429) su GET job status allunga la sync di ~13s.
            # Ritorniamo subito dopo 202 Accepted; l'update è in coda su CardTrader e viene processato.
            if job_uuid:
                logger.info(
                    f"Product update synced to CardTrader: item_id={item_id}, "
                    f"external_stock_id={item.external_stock_id}, job={job_uuid}"
                )
                _log_to_file("Product update queued (no poll)", {
                    "item_id": item_id,
                    "job_uuid": job_uuid,
                })
                result = {
                    "status": "synced",
                    "item_id": item_id,
                    "external_stock_id": item.external_stock_id,
                    "job_uuid": job_uuid,
                    "message": "Update queued on CardTrader",
                }
                return result

            result = {
                "status": "synced",
                "item_id": item_id,
                "external_stock_id": item.external_stock_id,
                "job_uuid": None,
                "message": "Update sent to CardTrader",
            }
            return result


@celery_app.task(bind=True, max_retries=5, default_retry_delay=30)
def sync_delete_product_to_cardtrader(
    self,
    user_id: str,
    external_stock_id: int,
) -> Dict[str, Any]:
    """
    Synchronize product deletion to CardTrader.
    
    Args:
        user_id: User UUID as string
        external_stock_id: CardTrader product ID (external_stock_id from inventory item)
        
    Returns:
        Dict with sync result
    """
    user_uuid = uuid.UUID(user_id)
    
    try:
        result = run_async(_sync_delete_product_async(user_uuid, external_stock_id))
        return result
    except RateLimitError as e:
        logger.warning(f"Rate limit error syncing product deletion: {e}")
        raise self.retry(exc=e, countdown=min(300, 2 ** self.request.retries))
    except Exception as e:
        logger.error(
            f"Error syncing product deletion for user {user_id}, "
            f"external_stock_id {external_stock_id}: {e}",
            exc_info=True,
        )
        raise


async def _sync_delete_product_async(
    user_uuid: uuid.UUID,
    external_stock_id: int,
) -> Dict[str, Any]:
    """Async implementation of product deletion sync."""
    encryption_manager = get_encryption_manager()
    
    _log_to_file("Starting product deletion sync", {
        "user_uuid": str(user_uuid),
        "external_stock_id": external_stock_id
    })
    
    # Use isolated session to avoid event loop conflicts
    async with get_isolated_db_session() as session:
        # Get user sync settings for token
        stmt = select(UserSyncSettings).where(UserSyncSettings.user_id == user_uuid)
        result = await session.execute(stmt)
        sync_settings = result.scalar_one_or_none()
        
        if not sync_settings:
            error_msg = f"User sync settings not found for user {user_uuid}"
            _log_to_file("Error in deletion sync", {"error": error_msg})
            raise ValueError(error_msg)
        
        # Decrypt token
        try:
            token = encryption_manager.decrypt(sync_settings.cardtrader_token_encrypted)
        except Exception as e:
            error_msg = f"Failed to decrypt token: {e}"
            _log_to_file("Error decrypting token", {"error": error_msg})
            raise ValueError(error_msg) from e
        
        # Delete from CardTrader
        try:
            async with CardTraderClient(token, str(user_uuid)) as client:
                _log_to_file("Calling CardTrader delete_product", {
                    "external_stock_id": external_stock_id
                })
                
                delete_response = await client.delete_product(external_stock_id)
                # delete_response is a dict from the API (or empty dict if no body)
                response_data = delete_response if isinstance(delete_response, dict) else {}
                
                if response_data.get("status") == "already_deleted":
                    logger.info(
                        f"Product {external_stock_id} was already deleted on CardTrader. "
                        f"Sync completed successfully."
                    )
                    _log_to_file("Product already deleted on CardTrader", {
                        "external_stock_id": external_stock_id,
                        "status": "already_deleted"
                    })
                else:
                    logger.info(
                        f"Product deleted from CardTrader: external_stock_id={external_stock_id}"
                    )
                    _log_to_file("Product deleted successfully", {
                        "external_stock_id": external_stock_id,
                        "status": "deleted"
                    })
                
                return {
                    "status": "success",
                    "external_stock_id": external_stock_id,
                    "message": response_data.get("message", "Product deleted from CardTrader"),
                    "already_deleted": response_data.get("status") == "already_deleted"
                }
        except CardTraderAPIError as e:
            # Check if it's a 404 error (product not found)
            error_str = str(e).lower()
            if "404" in str(e) or "not_found" in error_str:
                logger.info(
                    f"Product {external_stock_id} not found on CardTrader (already deleted). "
                    f"Sync completed successfully."
                )
                _log_to_file("Product already deleted on CardTrader", {
                    "external_stock_id": external_stock_id,
                    "status": "already_deleted"
                })
                return {
                    "status": "success",
                    "external_stock_id": external_stock_id,
                    "message": "Product was already deleted on CardTrader",
                    "already_deleted": True
                }
            # Re-raise other CardTrader API errors
            error_msg = f"Failed to delete product from CardTrader: {e}"
            logger.error(error_msg, exc_info=True)
            _log_to_file("Error deleting from CardTrader", {
                "external_stock_id": external_stock_id,
                "error": str(e)
            })
            raise
        except Exception as e:
            error_msg = f"Failed to delete product from CardTrader: {e}"
            logger.error(error_msg, exc_info=True)
            _log_to_file("Error deleting from CardTrader", {
                "external_stock_id": external_stock_id,
                "error": str(e)
            })
            raise
