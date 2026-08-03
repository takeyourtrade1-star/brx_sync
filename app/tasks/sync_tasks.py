"""
Celery tasks for synchronizing inventory between Ebartex and CardTrader.
"""

import asyncio
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

# Note: nest_asyncio is NOT applied at module level to avoid conflicts with uvloop.
# We use isolated event loops in run_async() instead.
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.crypto import get_encryption_manager
from app.core.database import get_isolated_db_session
from app.models.inventory import (
    SyncOperation,
    SyncSnapshot,
    SyncStatusEnum,
    UserInventoryItem,
    UserSyncSettings,
    WebhookInbox,
)
from app.services.blueprint_mapper import get_blueprint_mapper
from app.services.cardtrader_client import (
    CardTraderClient,
    RateLimitError,
)
from app.services.marketplace_projection import project_inventory_to_marketplace
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)
CHUNK_SIZE = 5000


def _log_to_file(message: str, data: dict = None):
    """Helper to log to file safely. Disabled when SYNC_LOG_TO_FILE=False (recommended in production with many workers to avoid file contention)."""
    from app.core.config import get_settings
    from app.core.logging import redact_log_value

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
        "data": redact_log_value(data or {}),
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
    except Exception as exc:
        _log_to_file(
            "Error in coroutine",
            {
                "error_type": type(exc).__name__,
            },
        )
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

    try:
        engine = get_sync_db_engine()
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO sync_operations (user_id, operation_id, operation_type, status)
                    VALUES (CAST(:user_id AS uuid), :operation_id, 'bulk_sync', 'pending')
                    ON CONFLICT (operation_id) DO NOTHING
                """),
                {"user_id": str(user_uuid), "operation_id": operation_id},
            )
    except Exception as exc:
        logger.warning(
            "Could not pre-create SyncOperation (%s)", type(exc).__name__
        )
        # Continue anyway; async path will create it (may cause brief 403 on early polls)

    try:
        # Run async code in sync context - use helper to avoid event loop conflicts
        result = run_async(_initial_bulk_sync_async(user_uuid, operation_id))
        if result.get("status") == "superseded":
            from app.tasks.periodic_sync import reconcile_user

            reconcile_user.delay(user_id)
        return result
    except RateLimitError as exc:
        # Retry with exponential backoff
        logger.warning("Rate limit during bulk sync for user %s", user_id)
        raise self.retry(exc=exc, countdown=min(300, 2**self.request.retries))
    except Exception as exc:
        logger.error("Bulk sync failed for user %s (%s)", user_id, type(exc).__name__)
        # Update sync status to error - use sync database connection to avoid event loop issues
        try:
            # Use sync database connection to update status without async
            from app.core.database import get_sync_db_engine

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
                        "error": type(exc).__name__,
                        "user_id": str(user_uuid),
                    },
                )
                conn.execute(
                    text("""
                        UPDATE sync_operations
                        SET status = 'failed',
                            operation_metadata = CAST(:metadata AS jsonb),
                            completed_at = NOW()
                        WHERE operation_id = :operation_id
                          AND status IN ('pending','processing')
                        """),
                    {
                        "operation_id": operation_id,
                        "metadata": '{"error": "initial bulk sync failed"}',
                    },
                )
            logger.info(f"Updated sync status to error for user {user_uuid}")
        except Exception as update_error:
            logger.error(
                "Failed to persist bulk-sync failure (%s)",
                type(update_error).__name__,
            )
        raise


async def _initial_bulk_sync_async(user_uuid: uuid.UUID, operation_id: str) -> Dict[str, Any]:
    """Run initial import under the same per-user lease as outbound writes."""

    from app.services.cardtrader_mutation_lease import cardtrader_mutation_lease
    from app.services.reconciler import _refresh_mutation_lease

    async with cardtrader_mutation_lease(user_uuid) as mutation_lease:
        stopped = asyncio.Event()
        lost_lease: list[BaseException] = []
        heartbeat = asyncio.create_task(
            _refresh_mutation_lease(
                mutation_lease,
                stopped,
                lost_lease,
            )
        )
        try:
            return await _initial_bulk_sync_locked(
                user_uuid,
                operation_id,
                mutation_lease,
                lost_lease,
            )
        finally:
            stopped.set()
            await heartbeat


async def _initial_bulk_sync_locked(
    user_uuid: uuid.UUID,
    operation_id: str,
    mutation_lease,
    lost_lease: list[BaseException],
) -> Dict[str, Any]:
    """Async implementation of bulk sync."""
    encryption_manager = get_encryption_manager()
    blueprint_mapper = get_blueprint_mapper()

    async with get_isolated_db_session() as session:
        # Get user sync settings
        stmt = select(UserSyncSettings).where(UserSyncSettings.user_id == user_uuid)
        result = await session.execute(stmt)
        sync_settings = result.scalar_one_or_none()

        if not sync_settings:
            raise ValueError(f"User sync settings not found for user {user_uuid}")
        environment = sync_settings.execution_mode
        mode_version = sync_settings.mode_version
        if environment not in {"partial", "real"}:
            raise PermissionError("DEMO mode cannot read the CardTrader inventory")
        local_active_rows = (
            await session.execute(
                select(func.count())
                .select_from(UserInventoryItem)
                .where(
                    UserInventoryItem.user_id == user_uuid,
                    UserInventoryItem.source == "cardtrader",
                    UserInventoryItem.environment == environment,
                    UserInventoryItem.game_id == 1,
                    UserInventoryItem.quantity > 0,
                )
            )
        ).scalar_one()

        # Decrypt token
        token = encryption_manager.decrypt(sync_settings.cardtrader_token_encrypted)

        # The mapped column owns the canonical lowercase PostgreSQL enum type.
        update_stmt = (
            update(UserSyncSettings)
            .where(UserSyncSettings.user_id == user_uuid)
            .values(sync_status=SyncStatusEnum.INITIAL_SYNC.value)
        )
        await session.execute(update_stmt)
        await session.commit()

        watermark = int(
            (
                await session.execute(
                    select(func.coalesce(func.max(WebhookInbox.id), 0)).where(
                        WebhookInbox.user_id == user_uuid
                    )
                )
            ).scalar_one()
        )
        from app.services.webhook_ledger_processor import _quarantine_inventory

        await _quarantine_inventory(
            session,
            user_id=user_uuid,
            environment=environment,
            inbox_id=watermark,
            product_ids=(),
            full_quarantine=True,
        )
        await project_inventory_to_marketplace(
            session,
            user_uuid,
            environment,
        )
        await session.commit()

        # Load SyncOperation (created at task start) for progress/metadata updates
        stmt_op = select(SyncOperation).where(SyncOperation.operation_id == operation_id)
        res_op = await session.execute(stmt_op)
        sync_op = res_op.scalar_one_or_none()
        if sync_op:
            sync_op.status = "processing"
            await session.commit()

        try:
            # Initialize CardTrader client
            async with CardTraderClient(token, str(user_uuid)) as client:
                # Export all products
                logger.info(f"Starting bulk export for user {user_uuid}")
                products = await client.get_products_export()
                from app.services.reconciler import _assert_mutation_lease

                _assert_mutation_lease(mutation_lease, lost_lease)
                logger.info(f"Exported {len(products)} products from CardTrader")
                from app.services.reconciler import (
                    _filter_cards_prints,
                    _snapshot_checksum,
                    normalize_magic_snapshot,
                    validate_snapshot,
                )

                normalized, shape_problems = normalize_magic_snapshot(products)
                products, mapping_problems, unsupported_rows = _filter_cards_prints(
                    normalized,
                    blueprint_mapper.map_blueprint_id,
                )

                snapshot_ok, snapshot_problems = validate_snapshot(
                    products,
                    previous_snapshot_size=None,
                    local_active_rows=local_active_rows,
                )
                all_snapshot_problems = shape_problems + mapping_problems + snapshot_problems
                if not snapshot_ok or all_snapshot_problems:
                    raise ValueError(
                        "CardTrader export rejected: " + "; ".join(all_snapshot_problems)
                    )

                # Process in chunks with optimized commit strategy and parallelization
                total_processed = 0
                total_created = 0
                total_updated = 0
                total_skipped = 0

                total_chunks = (len(products) + CHUNK_SIZE - 1) // CHUNK_SIZE
                chunks = [products[i : i + CHUNK_SIZE] for i in range(0, len(products), CHUNK_SIZE)]

                # Process chunks in parallel batches (3-5 at a time)
                # This significantly speeds up processing while not overwhelming the DB
                PARALLEL_CHUNKS = 3

                for batch_start in range(0, len(chunks), PARALLEL_CHUNKS):
                    _assert_mutation_lease(mutation_lease, lost_lease)
                    batch_chunks = chunks[batch_start : batch_start + PARALLEL_CHUNKS]
                    batch_indices = range(
                        batch_start, min(batch_start + PARALLEL_CHUNKS, len(chunks))
                    )

                    # Process chunks in parallel (each chunk uses its own isolated DB session)
                    chunk_tasks = [
                        _process_products_chunk(
                            user_uuid,
                            chunk,
                            blueprint_mapper,
                            environment,
                            watermark,
                        )
                        for chunk in batch_chunks
                    ]

                    batch_results = await asyncio.gather(*chunk_tasks)
                    await session.refresh(sync_settings)
                    if (
                        sync_settings.execution_mode != environment
                        or sync_settings.mode_version != mode_version
                    ):
                        raise RuntimeError("Sync mode changed during initial inventory import")

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

                # Update through the mapped canonical PostgreSQL enum type.
                locked_settings = (
                    await session.execute(
                        select(UserSyncSettings)
                        .where(UserSyncSettings.user_id == user_uuid)
                        .with_for_update()
                    )
                ).scalar_one()
                if (
                    locked_settings.execution_mode != environment
                    or locked_settings.mode_version != mode_version
                ):
                    raise RuntimeError("Sync mode changed before initial inventory activation")
                latest_unresolved = int(
                    (
                        await session.execute(
                            select(func.coalesce(func.max(WebhookInbox.id), 0)).where(
                                WebhookInbox.user_id == user_uuid,
                                WebhookInbox.status.in_(
                                    (
                                        "received",
                                        "processing",
                                        "failed",
                                        "deferred",
                                        "reconcile_pending",
                                    )
                                ),
                            )
                        )
                    ).scalar_one()
                )
                superseded = latest_unresolved > watermark
                if superseded:
                    await _quarantine_inventory(
                        session,
                        user_id=user_uuid,
                        environment=environment,
                        inbox_id=latest_unresolved,
                        product_ids=(),
                        full_quarantine=True,
                    )

                update_stmt = (
                    update(UserSyncSettings)
                    .where(UserSyncSettings.user_id == user_uuid)
                    .values(
                        sync_status=SyncStatusEnum.ACTIVE.value,
                        last_sync_at=datetime.utcnow(),
                        last_error=None,
                    )
                )
                await session.execute(update_stmt)
                await project_inventory_to_marketplace(
                    session,
                    user_uuid,
                    environment,
                )
                _assert_mutation_lease(mutation_lease, lost_lease)

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
                        "unsupported_rows": unsupported_rows,
                        "snapshot_watermark": watermark,
                        "superseded_by_inbox": (latest_unresolved if superseded else None),
                    }
                session.add(
                    SyncSnapshot(
                        id=uuid.uuid4(),
                        user_id=user_uuid,
                        environment=environment,
                        status="rejected" if superseded else "applied",
                        product_count=len(products),
                        checksum=_snapshot_checksum(products),
                        problems_json=(
                            ["snapshot superseded by webhook " f"{latest_unresolved}"]
                            if superseded
                            else None
                        ),
                        result_json={
                            "operation": "initial_bulk_sync",
                            "processed": total_processed,
                            "created": total_created,
                            "updated": total_updated,
                            "skipped": total_skipped,
                            "snapshot_watermark": watermark,
                            "superseded_by_inbox": (latest_unresolved if superseded else None),
                        },
                        completed_at=datetime.utcnow(),
                    )
                )

                await session.commit()

                return {
                    "status": "superseded" if superseded else "completed",
                    "total_products": len(products),
                    "processed": total_processed,
                    "created": total_created,
                    "updated": total_updated,
                    "skipped": total_skipped,
                }

        except Exception as e:
            error_type = type(e).__name__
            # Update sync status to error - try with async session first, fallback to sync
            try:
                update_stmt = (
                    update(UserSyncSettings)
                    .where(UserSyncSettings.user_id == user_uuid)
                    .values(sync_status=SyncStatusEnum.ERROR.value, last_error=error_type)
                )
                await session.execute(update_stmt)
                if sync_op:
                    sync_op.status = "failed"
                    sync_op.completed_at = datetime.utcnow()
                    sync_op.operation_metadata = {"error_type": error_type}
                await session.commit()
            except Exception as update_error:
                # If async update fails, use sync connection as fallback
                logger.warning(
                    "Failed to update error status with async session (%s)",
                    type(update_error).__name__,
                )
                try:
                    from sqlalchemy import text

                    from app.core.database import get_sync_db_engine

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
                                "error": error_type,
                                "user_id": str(user_uuid),
                            },
                        )
                    logger.info(
                        f"Updated sync status to error using sync connection for user {user_uuid}"
                    )
                except Exception as sync_update_error:
                    logger.error(
                        "Failed to update error status with sync connection (%s)",
                        type(sync_update_error).__name__,
                    )
            raise


async def _process_products_chunk(
    user_uuid: uuid.UUID,
    products: List[Dict[str, Any]],
    blueprint_mapper,
    environment: str,
    snapshot_watermark: int,
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

        if product.get("game_id") != 1 or not blueprint_id or not product_id:
            logger.debug(
                "Sync skip: prodotto senza blueprint_id o id (blueprint_id=%s, product_id=%s)",
                blueprint_id,
                product_id,
            )
            skipped += 1
            continue

        valid_products.append(
            {
                "blueprint_id": blueprint_id,
                "game_id": 1,
                "external_stock_id": str(product_id),
                "quantity": product.get("quantity", 0),
                "price_cents": product.get("price_cents", 0),
                "properties": product.get("properties_hash", {}),
                "source": "cardtrader",
                "environment": environment,
            }
        )
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
        # Defense in depth: BRX currently imports only Magic cards_prints.
        if mapping and mapping[1] == "cards_prints":
            products_to_process.append(product)
        else:
            reason = (
                f"mapping non Magic ({mapping[1]})" if mapping else "nessun mapping nel catalogo"
            )
            logger.info(
                "Sync skip: blueprint_id=%s external_stock_id=%s — %s",
                blueprint_id,
                product.get("external_stock_id"),
                reason,
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
            (user_uuid, environment, p["blueprint_id"], p["external_stock_id"])
            for p in products_to_process
        ]
        existing_items_stmt = select(
            UserInventoryItem.id,
            UserInventoryItem.blueprint_id,
            UserInventoryItem.external_stock_id,
            UserInventoryItem.row_version,
        ).where(
            tuple_(
                UserInventoryItem.user_id,
                UserInventoryItem.environment,
                UserInventoryItem.blueprint_id,
                UserInventoryItem.external_stock_id,
            ).in_(lookup_keys)
        )
        result = await session.execute(existing_items_stmt)
        existing_items = result.all()
        existing_keys = {
            (item.blueprint_id, item.external_stock_id): (
                item.id,
                item.row_version,
            )
            for item in existing_items
        }
        # Step 5: Separate products into INSERT and UPDATE batches
        items_to_insert = []
        items_to_update = []
        now = datetime.utcnow()
        for product in products_to_process:
            key = (product["blueprint_id"], product["external_stock_id"])
            if key in existing_keys:
                item_id, expected_row_version = existing_keys[key]
                items_to_update.append(
                    {
                        "id": item_id,
                        "expected_row_version": expected_row_version,
                        "game_id": 1,
                        "quantity": product["quantity"],
                        "price_cents": product["price_cents"],
                        "properties": product["properties"],
                        "external_stock_id": product["external_stock_id"],
                        "source": "cardtrader",
                        "environment": environment,
                        "lifecycle_status": "sold_out" if product["quantity"] == 0 else "active",
                        "sync_state": "synced",
                        "sync_uncertain_event_id": None,
                        "missing_snapshot_count": 0,
                        "updated_at": now,
                    }
                )
            else:
                items_to_insert.append(
                    {
                        "user_id": user_uuid,
                        "blueprint_id": product["blueprint_id"],
                        "game_id": 1,
                        "quantity": product["quantity"],
                        "price_cents": product["price_cents"],
                        "properties": product["properties"],
                        "external_stock_id": product["external_stock_id"],
                        "source": "cardtrader",
                        "environment": environment,
                        "lifecycle_status": "sold_out" if product["quantity"] == 0 else "active",
                        "sync_state": "synced",
                        "sync_uncertain_event_id": None,
                        "mapping_status": "mapped",
                        "missing_snapshot_count": 0,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
        if items_to_insert:
            # Keep each statement well below PostgreSQL's bind-parameter
            # ceiling even when CHUNK_SIZE is large.
            for offset in range(0, len(items_to_insert), 1000):
                insert_batch = items_to_insert[offset : offset + 1000]
                result = await session.execute(
                    pg_insert(UserInventoryItem).values(insert_batch).on_conflict_do_nothing()
                )
                created += int(result.rowcount or 0)
            skipped += len(items_to_insert) - created

        if items_to_update:
            for item_data in items_to_update:
                item_id = item_data.pop("id")
                expected_row_version = item_data.pop("expected_row_version")
                eligible_inbound = or_(
                    and_(
                        UserInventoryItem.sync_state == "synced",
                        UserInventoryItem.sync_uncertain_event_id.is_(None),
                    ),
                    and_(
                        UserInventoryItem.sync_state.in_(("synced", "failed", "uncertain")),
                        UserInventoryItem.sync_uncertain_event_id.isnot(None),
                        UserInventoryItem.sync_uncertain_event_id <= snapshot_watermark,
                    ),
                )
                stmt = (
                    update(UserInventoryItem)
                    .where(
                        UserInventoryItem.id == item_id,
                        UserInventoryItem.user_id == user_uuid,
                        UserInventoryItem.environment == environment,
                        UserInventoryItem.source == "cardtrader",
                        UserInventoryItem.row_version == expected_row_version,
                        UserInventoryItem.reserved_quantity == 0,
                        eligible_inbound,
                    )
                    .values(
                        **item_data,
                        row_version=UserInventoryItem.row_version + 1,
                    )
                )
                result = await session.execute(stmt)
                if result.rowcount == 1:
                    updated += 1
                else:
                    skipped += 1
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
    async with get_isolated_db_session() as session:
        stmt = (
            update(UserSyncSettings)
            .where(UserSyncSettings.user_id == user_uuid)
            .values(
                sync_status=status,
                last_error=error,
                updated_at=datetime.utcnow(),
            )
        )
        await session.execute(stmt)
        await session.commit()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=10)
def process_webhook_notification(
    self,
    webhook_id: str,
) -> Dict[str, Any]:
    """
    Process webhook notification from CardTrader (order create/update).

    Args:
        webhook_id: Webhook UUID
        webhook_id: Identifier of a signature-verified durable inbox row

    Returns:
        Dict with processing result
    """
    try:
        if not isinstance(webhook_id, str) or not re.fullmatch(
            r"[A-Za-z0-9:_-]{1,128}", webhook_id
        ):
            raise ValueError("Invalid webhook id")
        result, user_id = run_async(_process_webhook_notification_async(webhook_id))
        if result.get("status") == "reconcile_required":
            # Arithmetic deltas cannot safely reconstruct destroy/legacy or
            # partial events. Queue the authoritative CardTrader export; if
            # enqueueing fails this task retries, and the event ledger returns
            # reconcile_required again on the duplicate delivery.
            from app.tasks.periodic_sync import reconcile_user

            reconcile_user.delay(user_id)
        return result
    except Exception as exc:
        logger.error("Webhook task failed for id=%s (%s)", webhook_id, type(exc).__name__)
        from app.services.webhook_ledger_processor import mark_webhook_failed

        run_async(mark_webhook_failed(webhook_id, exc))
        raise self.retry(exc=exc, countdown=min(60, 2**self.request.retries))


async def _process_webhook_notification_async(
    webhook_id: str,
) -> tuple[Dict[str, Any], str]:
    """
    Async implementation of webhook processing.

    Uses the WebhookProcessor for better organization and error handling.

    Args:
        webhook_id: Webhook UUID
        The payload and owner are always loaded from the signature-verified inbox.
    """
    from app.services.webhook_ledger_processor import WebhookLedgerProcessor

    async with get_isolated_db_session() as session:
        inbox = (
            await session.execute(
                select(WebhookInbox)
                .where(WebhookInbox.webhook_id == webhook_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if inbox is None or inbox.signature_valid is not True:
            raise ValueError("Verified webhook inbox row not found")
        sync_settings = (
            await session.execute(
                select(UserSyncSettings)
                .where(UserSyncSettings.user_id == inbox.user_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if sync_settings is None:
            raise ValueError("Webhook owner settings not found")
        payload = inbox.payload_json
        if not isinstance(payload, dict):
            raise ValueError("Persisted webhook payload is invalid")
        result = await WebhookLedgerProcessor().prepare_inbox(
            session,
            inbox=inbox,
            payload=dict(payload),
            settings=sync_settings,
        )
        await session.commit()
        return result, str(inbox.user_id)
