"""Durable CardTrader mutation outbox workers."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import select, text, update

from app.core.crypto import get_encryption_manager
from app.core.database import get_isolated_db_session
from app.core.redis_client import get_redis_sync
from app.models.inventory import (
    CardTraderOutbox,
    SyncOperation,
    UserInventoryItem,
    UserSyncSettings,
)
from app.services.cardtrader_client import CardTraderAPIError, CardTraderClient, RateLimitError
from app.services.cardtrader_mutation_lease import cardtrader_mutation_lock_key
from app.services.sync_policy import (
    CardTraderWriteBlockedError,
    assert_cardtrader_write_allowed,
)
from app.tasks.celery_app import celery_app
from app.tasks.sync_tasks import run_async

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"verified", "failed", "uncertain", "cancelled"}
SUCCESS_JOB_STATES = {"completed", "complete", "successful", "succeeded", "success"}
FAILED_JOB_STATES = {"failed", "failure", "unprocessable", "rejected", "cancelled"}
LOCK_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class ExternalMutationBusyError(RuntimeError):
    """Another command for the same CardTrader account owns the lease."""


def _job_state(payload: Dict[str, Any]) -> str:
    return str(payload.get("status") or payload.get("state") or "").strip().lower()


def _job_has_errors(payload: Dict[str, Any]) -> bool:
    errors = payload.get("errors")
    if errors not in (None, [], {}, "", 0, False):
        return True
    stats = payload.get("stats")
    if isinstance(stats, dict):
        for key in ("failed", "error", "errors", "invalid", "rejected"):
            try:
                if int(stats.get(key) or 0) > 0:
                    return True
            except (TypeError, ValueError):
                return True
    results = payload.get("results")
    if isinstance(results, list):
        for result in results:
            if not isinstance(result, dict):
                continue
            state = str(
                result.get("result") or result.get("status") or result.get("state") or ""
            ).lower()
            if state in FAILED_JOB_STATES or result.get("errors"):
                return True
    return False


def _export_price_cents(product: Dict[str, Any]) -> Optional[int]:
    if isinstance(product.get("price_cents"), int):
        return product["price_cents"]
    price = product.get("price")
    if isinstance(price, dict) and isinstance(price.get("cents"), int):
        return price["cents"]
    return None


def _payload_matches_product(payload: Dict[str, Any], product: Dict[str, Any]) -> bool:
    if "quantity" in payload and product.get("quantity") != payload["quantity"]:
        return False
    if "price" in payload:
        expected_cents = round(float(payload["price"]) * 100)
        if _export_price_cents(product) != expected_cents:
            return False
    for field in ("description", "user_data_field", "graded"):
        if field in payload and product.get(field) != payload[field]:
            return False
    expected_properties = payload.get("properties")
    actual_properties = product.get("properties_hash") or product.get("properties") or {}
    if isinstance(expected_properties, dict):
        if not isinstance(actual_properties, dict):
            return False
        if any(actual_properties.get(key) != value for key, value in expected_properties.items()):
            return False
    return True


async def _poll_job(client: CardTraderClient, job_uuid: str) -> Dict[str, Any]:
    from app.core.config import get_settings

    settings = get_settings()
    deadline = asyncio.get_running_loop().time() + settings.CARDTRADER_JOB_POLL_TIMEOUT_SECONDS
    last: Dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        last = await client.get_job_status(job_uuid)
        state = _job_state(last)
        if state in SUCCESS_JOB_STATES:
            if _job_has_errors(last):
                raise CardTraderAPIError(
                    f"CardTrader job {job_uuid} completed with item errors",
                    status_code=422,
                )
            return last
        if state in FAILED_JOB_STATES:
            raise CardTraderAPIError(
                f"CardTrader job {job_uuid} ended as {state}",
                status_code=422,
            )
        await asyncio.sleep(settings.CARDTRADER_JOB_POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"CardTrader job {job_uuid} did not reach a terminal state")


async def _update_command_state(
    command_id: uuid.UUID,
    status: str,
    *,
    error: Optional[str] = None,
    job_uuid: Optional[str] = None,
    result: Optional[Dict[str, Any]] = None,
) -> None:
    async with get_isolated_db_session() as session:
        command = (
            await session.execute(
                select(CardTraderOutbox)
                .where(CardTraderOutbox.id == command_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if command is None:
            return
        command.status = status
        command.last_error = error
        if job_uuid is not None:
            command.job_uuid = job_uuid
        if status in TERMINAL_STATUSES:
            command.completed_at = datetime.now(timezone.utc)

        operation = (
            await session.execute(
                select(SyncOperation).where(
                    SyncOperation.operation_id == str(command_id)
                )
            )
        ).scalar_one_or_none()
        if operation is not None:
            operation.status = "completed" if status == "verified" else status
            operation.operation_metadata = result or {
                "status": status,
                "error": error,
                "job_uuid": job_uuid or command.job_uuid,
            }
            if status in TERMINAL_STATUSES:
                operation.completed_at = datetime.now(timezone.utc)

        if command.inventory_item_id is not None:
            item = (
                await session.execute(
                    select(UserInventoryItem).where(
                        UserInventoryItem.id == command.inventory_item_id
                    )
                )
            ).scalar_one_or_none()
            if item is not None and (
                command.expected_row_version is None
                or item.row_version == command.expected_row_version
            ):
                if status == "verified":
                    item.sync_state = "synced"
                    item.last_external_update_at = datetime.now(timezone.utc)
                    if command.operation_type == "delete_product":
                        item.lifecycle_status = "archived"
                        item.quantity = 0
                elif status in {"failed", "uncertain"}:
                    item.sync_state = status
                    if command.operation_type == "delete_product":
                        item.lifecycle_status = "sync_failed"

        await _apply_marketplace_result(session, command, status)


async def _apply_marketplace_result(
    session,
    command: CardTraderOutbox,
    status: str,
    *,
    restore_local: bool = True,
) -> None:
    context = command.context_json or {}
    context_type = context.get("type")
    if context_type == "marketplace_purchase":
        order_id = context.get("order_id")
        listing_id = context.get("listing_id")
        quantity = int(context.get("quantity") or 0)
        if status == "verified":
            await session.execute(
                text(
                    """
                    UPDATE mkt_orders
                    SET status = 'confirmed', updated_at = NOW()
                    WHERE id = CAST(:order_id AS uuid)
                      AND status = 'pending_external'
                    """
                ),
                {"order_id": order_id},
            )
        elif status in {"failed", "cancelled"}:
            restored = await session.execute(
                text(
                    """
                    UPDATE mkt_orders
                    SET status = 'cancelled', updated_at = NOW()
                    WHERE id = CAST(:order_id AS uuid)
                      AND status = 'pending_external'
                    RETURNING id
                    """
                ),
                {"order_id": order_id},
            )
            if restored.scalar_one_or_none() is not None:
                if restore_local and quantity > 0:
                    await session.execute(
                        text(
                            """
                            UPDATE user_inventory_items
                            SET quantity = quantity + :quantity,
                                lifecycle_status = 'active',
                                sync_state = 'synced',
                                row_version = row_version + 1,
                                updated_at = NOW()
                            WHERE id = :inventory_item_id
                              AND row_version = :expected_row_version
                            """
                        ),
                        {
                            "inventory_item_id": command.inventory_item_id,
                            "expected_row_version": command.expected_row_version,
                            "quantity": quantity,
                        },
                    )
                    await session.execute(
                        text(
                            """
                            UPDATE mkt_listings
                            SET quantity = quantity + :quantity,
                                status = 'active',
                                updated_at = NOW()
                            WHERE id = CAST(:listing_id AS uuid)
                            """
                        ),
                        {"listing_id": listing_id, "quantity": quantity},
                    )
                elif command.inventory_item_id is not None:
                    await session.execute(
                        text(
                            """
                            UPDATE mkt_listings AS listing
                            SET quantity = inventory.quantity,
                                status = CASE
                                    WHEN inventory.quantity > 0 THEN 'active'
                                    ELSE 'sold'
                                END,
                                updated_at = NOW()
                            FROM user_inventory_items AS inventory
                            WHERE listing.id = CAST(:listing_id AS uuid)
                              AND inventory.id = :inventory_item_id
                            """
                        ),
                        {
                            "listing_id": listing_id,
                            "inventory_item_id": command.inventory_item_id,
                        },
                    )
    elif context_type == "marketplace_listing_cancel":
        listing_id = context.get("listing_id")
        old_inventory_quantity = int(context.get("old_inventory_quantity") or 0)
        if (
            status in {"failed", "cancelled"}
            and restore_local
            and command.inventory_item_id is not None
        ):
            await session.execute(
                text(
                    """
                    UPDATE user_inventory_items
                    SET quantity = :quantity,
                        lifecycle_status = CASE
                            WHEN :quantity > 0 THEN 'active'
                            ELSE 'sold_out'
                        END,
                        sync_state = 'synced',
                        row_version = row_version + 1,
                        updated_at = NOW()
                    WHERE id = :inventory_item_id
                      AND row_version = :expected_row_version
                    """
                ),
                {
                    "quantity": old_inventory_quantity,
                    "inventory_item_id": command.inventory_item_id,
                    "expected_row_version": command.expected_row_version,
                },
            )
        next_status = {
            "verified": "cancelled",
            "failed": "active",
            "cancelled": "active",
            "uncertain": "sync_failed",
        }.get(status)
        if next_status:
            await session.execute(
                text(
                    """
                    UPDATE mkt_listings
                    SET status = :status, updated_at = NOW()
                    WHERE id = CAST(:listing_id AS uuid)
                    """
                ),
                {"listing_id": listing_id, "status": next_status},
            )


async def resolve_uncertain_command_from_export(
    command_id: uuid.UUID,
    product: Optional[Dict[str, Any]],
) -> str:
    """Resolve an unknown external outcome from a validated full export."""

    async with get_isolated_db_session() as session:
        command = (
            await session.execute(
                select(CardTraderOutbox)
                .where(CardTraderOutbox.id == command_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if command is None or command.status not in {"uncertain", "running", "accepted"}:
            return "skipped"

        verified = (
            product is None
            if command.operation_type == "delete_product"
            else product is not None
            and _payload_matches_product(command.payload_json, product)
        )
        if verified:
            # Leave this transaction before using the shared finalizer session.
            pass
        else:
            external_quantity = int(product.get("quantity") or 0) if product else 0
            external_price = _export_price_cents(product) if product else None
            command.status = "failed"
            command.last_error = "Validated export does not match the requested mutation"
            command.completed_at = datetime.now(timezone.utc)
            operation = (
                await session.execute(
                    select(SyncOperation).where(
                        SyncOperation.operation_id == str(command.id)
                    )
                )
            ).scalar_one_or_none()
            if operation is not None:
                operation.status = "failed"
                operation.completed_at = datetime.now(timezone.utc)
                operation.operation_metadata = {
                    "status": "failed",
                    "error": command.last_error,
                    "resolved_by": "validated_export",
                }

            if command.inventory_item_id is not None:
                values: Dict[str, Any] = {
                    "quantity": external_quantity,
                    "lifecycle_status": "active" if external_quantity > 0 else "sold_out",
                    "sync_state": "synced",
                    "row_version": UserInventoryItem.row_version + 1,
                    "last_external_update_at": datetime.now(timezone.utc),
                }
                if external_price is not None:
                    values["price_cents"] = external_price
                await session.execute(
                    update(UserInventoryItem)
                    .where(UserInventoryItem.id == command.inventory_item_id)
                    .values(**values)
                )

            context = command.context_json or {}
            context_type = context.get("type")
            if context_type == "marketplace_purchase":
                await session.execute(
                    text(
                        """
                        UPDATE mkt_orders
                        SET status = 'cancelled', updated_at = NOW()
                        WHERE id = CAST(:order_id AS uuid)
                          AND status = 'pending_external'
                        """
                    ),
                    {"order_id": context.get("order_id")},
                )
            if context_type in {"marketplace_purchase", "marketplace_listing_cancel"}:
                await session.execute(
                    text(
                        """
                        UPDATE mkt_listings
                        SET quantity = :quantity,
                            status = CASE WHEN :quantity > 0 THEN 'active' ELSE 'sold' END,
                            updated_at = NOW()
                        WHERE id = CAST(:listing_id AS uuid)
                        """
                    ),
                    {
                        "listing_id": context.get("listing_id"),
                        "quantity": external_quantity,
                    },
                )
            return "failed"

    await _update_command_state(
        command_id,
        "verified",
        result={"status": "verified", "resolved_by": "validated_export"},
    )
    return "verified"


async def _cancel_locked_command(
    session,
    command: CardTraderOutbox,
    error: str,
    *,
    restore_local: bool,
) -> None:
    command.status = "cancelled"
    command.last_error = error
    command.completed_at = datetime.now(timezone.utc)
    operation = (
        await session.execute(
            select(SyncOperation).where(
                SyncOperation.operation_id == str(command.id)
            )
        )
    ).scalar_one_or_none()
    if operation is not None:
        operation.status = "cancelled"
        operation.completed_at = datetime.now(timezone.utc)
        operation.operation_metadata = {"status": "cancelled", "error": error}

    if command.inventory_item_id is not None and restore_local:
        item = (
            await session.execute(
                select(UserInventoryItem).where(
                    UserInventoryItem.id == command.inventory_item_id
                )
            )
        ).scalar_one_or_none()
        if item is not None and (
            command.expected_row_version is None
            or item.row_version == command.expected_row_version
        ) and not (command.context_json or {}).get("type"):
            item.sync_state = "failed"
            if command.operation_type == "delete_product":
                item.lifecycle_status = "sync_failed"

    await _apply_marketplace_result(
        session,
        command,
        "cancelled",
        restore_local=restore_local,
    )


async def _claim_command(command_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    async with get_isolated_db_session() as session:
        command = (
            await session.execute(
                select(CardTraderOutbox)
                .where(CardTraderOutbox.id == command_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if command is None or command.status in TERMINAL_STATUSES:
            return None
        if command.status == "running":
            return None

        try:
            await assert_cardtrader_write_allowed(
                session,
                command.user_id,
                expected_mode_version=command.mode_version,
            )
        except CardTraderWriteBlockedError:
            await _cancel_locked_command(
                session,
                command,
                "Execution policy no longer authorises this command",
                restore_local=True,
            )
            return None

        if command.inventory_item_id is not None and command.expected_row_version is not None:
            item = (
                await session.execute(
                    select(UserInventoryItem).where(
                        UserInventoryItem.id == command.inventory_item_id
                    )
                )
            ).scalar_one_or_none()
            if (
                item is None
                or item.row_version != command.expected_row_version
                or item.source != "cardtrader"
                or item.environment != "real"
            ):
                await _cancel_locked_command(
                    session,
                    command,
                    "Inventory row changed after command creation",
                    restore_local=False,
                )
                return None

        settings = (
            await session.execute(
                select(UserSyncSettings).where(UserSyncSettings.user_id == command.user_id)
            )
        ).scalar_one()
        token = get_encryption_manager().decrypt(settings.cardtrader_token_encrypted)
        command.status = "running"
        command.attempts += 1
        command.last_error = None
        return {
            "id": command.id,
            "user_id": command.user_id,
            "operation_type": command.operation_type,
            "target_product_id": command.target_product_id,
            "payload": command.payload_json,
            "token": token,
            "job_uuid": command.job_uuid,
            "context": command.context_json,
        }


async def _process_command(command_id: uuid.UUID) -> Dict[str, Any]:
    claimed = await _claim_command(command_id)
    if claimed is None:
        return {"status": "skipped", "command_id": str(command_id)}

    lock_key = cardtrader_mutation_lock_key(claimed["user_id"])
    lock_owner = str(uuid.uuid4())
    try:
        redis = get_redis_sync()
        lock_acquired = redis.set(lock_key, lock_owner, nx=True, ex=300)
    except Exception as exc:
        await _update_command_state(
            command_id,
            "pending",
            error=f"Unable to acquire CardTrader command lease: {exc}",
        )
        raise ExternalMutationBusyError("CardTrader command lease unavailable") from exc
    if not lock_acquired:
        await _update_command_state(
            command_id,
            "pending",
            error="Another CardTrader command is already running for this user",
        )
        raise ExternalMutationBusyError("CardTrader user command lease is busy")

    job_uuid: Optional[str] = None
    try:
        async with CardTraderClient(claimed["token"], str(claimed["user_id"])) as client:
            if claimed["operation_type"] == "update_product":
                job_uuid = claimed.get("job_uuid")
                if not job_uuid:
                    current_product = await client.get_product(
                        int(claimed["target_product_id"])
                    )
                    old_quantity = (claimed.get("context") or {}).get("old_quantity")
                    if (
                        current_product is None
                        or old_quantity is not None
                        and current_product.get("quantity") != old_quantity
                    ):
                        resolution = await resolve_uncertain_command_from_export(
                            command_id,
                            current_product,
                        )
                        return {
                            "status": resolution,
                            "command_id": str(command_id),
                            "reason": "external stock changed before mutation",
                        }
                    accepted = await client.bulk_update_products([claimed["payload"]])
                    job_uuid = str(accepted.get("job") or "").strip() or None
                    if job_uuid is None:
                        raise RuntimeError("CardTrader update returned no job UUID")
                    await _update_command_state(
                        command_id,
                        "accepted",
                        job_uuid=job_uuid,
                        result={"status": "accepted", "job_uuid": job_uuid},
                    )
                job_result = await _poll_job(client, job_uuid)
                result = {
                    "status": "verified",
                    "command_id": str(command_id),
                    "job_uuid": job_uuid,
                    "job": job_result,
                }
            elif claimed["operation_type"] == "delete_product":
                await client.delete_product(int(claimed["target_product_id"]))
                result = {
                    "status": "verified",
                    "command_id": str(command_id),
                }
            else:
                raise RuntimeError("Unsupported outbox operation")

        await _update_command_state(
            command_id,
            "verified",
            job_uuid=job_uuid,
            result=result,
        )
        return result
    except RateLimitError:
        await _update_command_state(command_id, "pending", error="CardTrader rate limited")
        raise
    except TimeoutError as exc:
        await _update_command_state(
            command_id,
            "uncertain",
            error=str(exc),
            job_uuid=job_uuid,
        )
        return {"status": "uncertain", "command_id": str(command_id), "error": str(exc)}
    except CardTraderAPIError as exc:
        terminal = "failed" if exc.status_code and exc.status_code < 500 else "uncertain"
        await _update_command_state(
            command_id,
            terminal,
            error=str(exc),
            job_uuid=job_uuid,
        )
        return {"status": terminal, "command_id": str(command_id), "error": str(exc)}
    except Exception as exc:
        await _update_command_state(
            command_id,
            "uncertain",
            error=f"{type(exc).__name__}: {exc}",
            job_uuid=job_uuid,
        )
        return {"status": "uncertain", "command_id": str(command_id), "error": str(exc)}
    finally:
        try:
            redis.eval(LOCK_RELEASE_SCRIPT, 1, lock_key, lock_owner)
        except Exception as exc:
            logger.warning("Unable to release CardTrader outbox lease: %s", exc)


@celery_app.task(bind=True, max_retries=5, default_retry_delay=30)
def process_cardtrader_outbox_command(
    self,
    command_id: str,
) -> Dict[str, Any]:
    try:
        return run_async(_process_command(uuid.UUID(command_id)))
    except RateLimitError as exc:
        raise self.retry(exc=exc, countdown=min(300, 2 ** self.request.retries))
    except ExternalMutationBusyError as exc:
        raise self.retry(exc=exc, countdown=min(60, 2 ** self.request.retries))


@celery_app.task
def dispatch_pending_cardtrader_outbox(limit: int = 100) -> Dict[str, Any]:
    return run_async(_dispatch_pending(limit))


async def _dispatch_pending(limit: int) -> Dict[str, Any]:
    stale_ids: list[uuid.UUID] = []
    from app.core.config import get_settings

    accepted_before = datetime.now(timezone.utc) - timedelta(
        seconds=get_settings().CARDTRADER_JOB_POLL_TIMEOUT_SECONDS + 30
    )
    async with get_isolated_db_session() as session:
        stale_before = datetime.now(timezone.utc) - timedelta(minutes=10)
        stale_ids = list(
            (
                await session.execute(
                    select(CardTraderOutbox.id).where(
                        CardTraderOutbox.status == "running",
                        CardTraderOutbox.updated_at < stale_before,
                    )
                )
            ).scalars().all()
        )
        ids = list(
            (
                await session.execute(
                    select(CardTraderOutbox.id)
                    .where(
                        (CardTraderOutbox.status == "pending")
                        | (
                            (CardTraderOutbox.status == "accepted")
                            & (CardTraderOutbox.updated_at < accepted_before)
                        )
                    )
                    .order_by(CardTraderOutbox.created_at)
                    .limit(max(1, min(limit, 500)))
                )
            ).scalars().all()
        )
    for command_id in stale_ids:
        await _update_command_state(
            command_id,
            "uncertain",
            error="Worker stopped while external outcome was unknown",
        )
    for command_id in ids:
        process_cardtrader_outbox_command.delay(str(command_id))
    return {
        "status": "dispatched",
        "count": len(ids),
        "stale_uncertain": len(stale_ids),
    }
