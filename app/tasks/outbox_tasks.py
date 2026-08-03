"""Durable CardTrader mutation outbox workers."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import func, select, text, update

from app.core.crypto import get_encryption_manager
from app.core.database import get_isolated_db_session
from app.models.inventory import (
    CardTraderOutbox,
    SyncOperation,
    SyncSnapshot,
    UserInventoryItem,
    UserSyncSettings,
)
from app.services.cardtrader_client import (
    CardTraderAPIError,
    CardTraderClient,
    RateLimitError,
)
from app.services.cardtrader_mutation_lease import (
    CardTraderMutationBusyError,
    cardtrader_mutation_lease,
)
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


class ExternalMutationBusyError(RuntimeError):
    """Another command for the same CardTrader account owns the lease."""


def _safe_failure_code(exc: Exception) -> str:
    """Return bounded diagnostics without persisting provider or infrastructure text."""
    code = type(exc).__name__
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and 100 <= status_code <= 599:
        return f"{code}:{status_code}"
    return code


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


def _mutation_matches_product(
    operation_type: str,
    payload: Dict[str, Any],
    product: Optional[Dict[str, Any]],
) -> bool:
    if operation_type == "delete_product":
        return product is None
    if product is None:
        return payload.get("quantity") == 0
    return _payload_matches_product(payload, product)


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
                select(CardTraderOutbox).where(CardTraderOutbox.id == command_id).with_for_update()
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
                select(SyncOperation).where(SyncOperation.operation_id == str(command_id))
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

    async def quarantine_listing(listing_id: Any) -> None:
        await session.execute(
            text("""
                UPDATE mkt_listings
                SET status = 'pending_sync', updated_at = NOW()
                WHERE id = CAST(:listing_id AS uuid)
                """),
            {"listing_id": listing_id},
        )

    async def inventory_is_reconciled() -> bool:
        if command.inventory_item_id is None:
            return False
        row = (
            await session.execute(
                select(
                    UserInventoryItem.sync_state,
                    UserInventoryItem.sync_uncertain_event_id,
                ).where(UserInventoryItem.id == command.inventory_item_id)
            )
        ).one_or_none()
        return bool(row and row.sync_state == "synced" and row.sync_uncertain_event_id is None)

    if context_type == "marketplace_purchase":
        order_id = context.get("order_id")
        listing_id = context.get("listing_id")
        quantity = int(context.get("quantity") or 0)
        if status == "verified":
            await session.execute(
                text("""
                    UPDATE mkt_orders
                    SET status = 'confirmed', updated_at = NOW()
                    WHERE id = CAST(:order_id AS uuid)
                      AND status = 'pending_external'
                    """),
                {"order_id": order_id},
            )
            if not await inventory_is_reconciled():
                await quarantine_listing(listing_id)
        elif status in {"failed", "cancelled"}:
            cancelled_order = await session.execute(
                text("""
                    UPDATE mkt_orders
                    SET status = 'cancelled', updated_at = NOW()
                    WHERE id = CAST(:order_id AS uuid)
                      AND status = 'pending_external'
                    RETURNING id
                    """),
                {"order_id": order_id},
            )
            inventory_safe = False
            if (
                restore_local
                and quantity > 0
                and command.inventory_item_id is not None
                and command.expected_row_version is not None
            ):
                restored_inventory = await session.execute(
                    update(UserInventoryItem)
                    .where(
                        UserInventoryItem.id == command.inventory_item_id,
                        UserInventoryItem.row_version == command.expected_row_version,
                        UserInventoryItem.source == "cardtrader",
                        UserInventoryItem.game_id == 1,
                        UserInventoryItem.sync_uncertain_event_id.is_(None),
                    )
                    .values(
                        quantity=UserInventoryItem.quantity + quantity,
                        lifecycle_status="active",
                        sync_state="synced",
                        row_version=UserInventoryItem.row_version + 1,
                        updated_at=datetime.now(timezone.utc),
                    )
                    .returning(UserInventoryItem.id)
                )
                inventory_safe = restored_inventory.scalar_one_or_none() is not None
            elif not restore_local:
                inventory_safe = await inventory_is_reconciled()

            if not inventory_safe:
                await quarantine_listing(listing_id)
                return

            if cancelled_order.scalar_one_or_none() is not None:
                if restore_local:
                    await session.execute(
                        text("""
                            UPDATE mkt_listings
                            SET quantity = quantity + :quantity,
                                status = 'active',
                                updated_at = NOW()
                            WHERE id = CAST(:listing_id AS uuid)
                            """),
                        {"listing_id": listing_id, "quantity": quantity},
                    )
                else:
                    await session.execute(
                        text("""
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
                            """),
                        {
                            "listing_id": listing_id,
                            "inventory_item_id": command.inventory_item_id,
                        },
                    )
    elif context_type == "marketplace_listing_cancel":
        listing_id = context.get("listing_id")
        old_inventory_quantity = int(context.get("old_inventory_quantity") or 0)
        if status == "verified":
            await session.execute(
                text("""
                    UPDATE mkt_listings
                    SET status = 'cancelled', updated_at = NOW()
                    WHERE id = CAST(:listing_id AS uuid)
                    """),
                {"listing_id": listing_id},
            )
            return
        inventory_safe = await inventory_is_reconciled()
        if (
            status in {"failed", "cancelled"}
            and restore_local
            and command.inventory_item_id is not None
            and command.expected_row_version is not None
        ):
            restored_inventory = await session.execute(
                update(UserInventoryItem)
                .where(
                    UserInventoryItem.id == command.inventory_item_id,
                    UserInventoryItem.row_version == command.expected_row_version,
                    UserInventoryItem.source == "cardtrader",
                    UserInventoryItem.game_id == 1,
                    UserInventoryItem.sync_uncertain_event_id.is_(None),
                )
                .values(
                    quantity=old_inventory_quantity,
                    lifecycle_status=("active" if old_inventory_quantity > 0 else "sold_out"),
                    sync_state="synced",
                    row_version=UserInventoryItem.row_version + 1,
                    updated_at=datetime.now(timezone.utc),
                )
                .returning(UserInventoryItem.id)
            )
            inventory_safe = restored_inventory.scalar_one_or_none() is not None
        next_status = {
            "failed": "active",
            "cancelled": "active",
            "uncertain": "pending_sync",
        }.get(status)
        if next_status:
            if not inventory_safe:
                await quarantine_listing(listing_id)
                return
            await session.execute(
                text("""
                    UPDATE mkt_listings
                    SET status = :status, updated_at = NOW()
                    WHERE id = CAST(:listing_id AS uuid)
                    """),
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
                select(CardTraderOutbox).where(CardTraderOutbox.id == command_id).with_for_update()
            )
        ).scalar_one_or_none()
        if command is None or command.status not in {"uncertain", "running", "accepted"}:
            return "skipped"

        verified = _mutation_matches_product(
            command.operation_type,
            command.payload_json,
            product,
        )
        next_status = "verified" if verified else "failed"
        now = datetime.now(timezone.utc)
        values: Dict[str, Any] = {
            "sync_state": "synced",
            "row_version": UserInventoryItem.row_version + 1,
            "last_external_update_at": now,
        }
        if command.operation_type == "delete_product" and verified:
            values.update(
                quantity=0,
                lifecycle_status="archived",
            )
        elif not verified:
            external_quantity = int(product.get("quantity") or 0) if product else 0
            external_price = _export_price_cents(product) if product else None
            values.update(
                quantity=external_quantity,
                lifecycle_status="active" if external_quantity > 0 else "sold_out",
            )
            if external_price is not None:
                values["price_cents"] = external_price

        if command.inventory_item_id is None or command.expected_row_version is None:
            return "deferred"
        updated_item = await session.execute(
            update(UserInventoryItem)
            .where(
                UserInventoryItem.id == command.inventory_item_id,
                UserInventoryItem.row_version == command.expected_row_version,
                UserInventoryItem.source == "cardtrader",
                UserInventoryItem.game_id == 1,
                UserInventoryItem.sync_uncertain_event_id.is_(None),
                UserInventoryItem.sync_state.in_(("pending", "accepted", "uncertain")),
            )
            .values(**values)
            .returning(UserInventoryItem.id)
        )
        if updated_item.scalar_one_or_none() is None:
            # A webhook or another local transition won the race. The command
            # remains uncertain and the authoritative reconciler owns recovery.
            return "deferred"

        command.status = next_status
        command.last_error = (
            None if verified else "Validated Magic export does not match the requested mutation"
        )
        command.completed_at = now
        operation = (
            await session.execute(
                select(SyncOperation).where(SyncOperation.operation_id == str(command.id))
            )
        ).scalar_one_or_none()
        if operation is not None:
            operation.status = "completed" if verified else "failed"
            operation.completed_at = now
            operation.operation_metadata = {
                "status": next_status,
                "error": command.last_error,
                "resolved_by": "validated_magic_export",
            }

        await _apply_marketplace_result(
            session,
            command,
            next_status,
            restore_local=False,
        )
        return next_status


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
            select(SyncOperation).where(SyncOperation.operation_id == str(command.id))
        )
    ).scalar_one_or_none()
    if operation is not None:
        operation.status = "cancelled"
        operation.completed_at = datetime.now(timezone.utc)
        operation.operation_metadata = {"status": "cancelled", "error": error}

    if command.inventory_item_id is not None and restore_local:
        item = (
            await session.execute(
                select(UserInventoryItem).where(UserInventoryItem.id == command.inventory_item_id)
            )
        ).scalar_one_or_none()
        if (
            item is not None
            and (
                command.expected_row_version is None
                or item.row_version == command.expected_row_version
            )
            and not (command.context_json or {}).get("type")
        ):
            item.sync_state = "failed"
            if command.operation_type == "delete_product":
                item.lifecycle_status = "sync_failed"

    await _apply_marketplace_result(
        session,
        command,
        "cancelled",
        restore_local=restore_local,
    )


async def _revalidate_claim_before_remote_write(command_id: uuid.UUID) -> bool:
    """Recheck policy and inventory fencing while the per-user lease is held."""
    async with get_isolated_db_session() as session:
        command = (
            await session.execute(
                select(CardTraderOutbox).where(CardTraderOutbox.id == command_id).with_for_update()
            )
        ).scalar_one_or_none()
        if command is None or command.status != "running":
            return False
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
                "Execution policy changed before the remote write",
                restore_local=True,
            )
            return False

        item = (
            await session.execute(
                select(UserInventoryItem)
                .where(UserInventoryItem.id == command.inventory_item_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if item is None:
            await _cancel_locked_command(
                session,
                command,
                "Inventory row disappeared before the remote write",
                restore_local=False,
            )
            return False
        if item.sync_uncertain_event_id is not None:
            item.sync_state = "uncertain"
            item.row_version += 1
            await _cancel_locked_command(
                session,
                command,
                "Inbound webhook quarantined inventory before the remote write",
                restore_local=False,
            )
            return False
        if (
            command.expected_row_version is None
            or item.row_version != command.expected_row_version
            or item.source != "cardtrader"
            or item.game_id != 1
            or item.environment != "real"
        ):
            await _cancel_locked_command(
                session,
                command,
                "Inventory row changed before the remote write",
                restore_local=False,
            )
            return False
        return True


async def _claim_command(command_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    async with get_isolated_db_session() as session:
        command = (
            await session.execute(
                select(CardTraderOutbox).where(CardTraderOutbox.id == command_id).with_for_update()
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
            if item is not None and item.sync_uncertain_event_id is not None:
                item.sync_state = "uncertain"
                item.row_version += 1
                await _cancel_locked_command(
                    session,
                    command,
                    "Inbound webhook quarantined inventory before command claim",
                    restore_local=False,
                )
                return None
            if (
                item is None
                or item.row_version != command.expected_row_version
                or item.source != "cardtrader"
                or item.game_id != 1
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
            "mode_version": command.mode_version,
        }


async def _process_command(command_id: uuid.UUID) -> Dict[str, Any]:
    claimed = await _claim_command(command_id)
    if claimed is None:
        return {"status": "skipped", "command_id": str(command_id)}

    job_uuid: Optional[str] = None
    remote_started = False
    try:
        async with cardtrader_mutation_lease(claimed["user_id"]) as lease:
            lease.refresh()
            if not await _revalidate_claim_before_remote_write(command_id):
                return {"status": "cancelled", "command_id": str(command_id)}
            async with CardTraderClient(claimed["token"], str(claimed["user_id"])) as client:
                if claimed["operation_type"] == "update_product":
                    job_uuid = claimed.get("job_uuid")
                    remote_started = bool(job_uuid)
                    if not job_uuid:
                        lease.refresh()
                        current_product = await client.get_product(
                            int(claimed["target_product_id"])
                        )
                        lease.refresh()
                        old_quantity = (claimed.get("context") or {}).get("old_quantity")
                        if (
                            current_product is None
                            or old_quantity is not None
                            and current_product.get("quantity") != old_quantity
                        ):
                            reason = (
                                "CardTrader stock changed before mutation; "
                                "awaiting validated full export"
                            )
                            await _update_command_state(
                                command_id,
                                "uncertain",
                                error=reason,
                            )
                            return {
                                "status": "uncertain",
                                "command_id": str(command_id),
                                "reason": reason,
                            }
                        if not await _revalidate_claim_before_remote_write(command_id):
                            return {
                                "status": "cancelled",
                                "command_id": str(command_id),
                            }
                        lease.refresh()
                        remote_started = True
                        accepted = await client.bulk_update_products([claimed["payload"]])
                        lease.refresh()
                        job_uuid = str(accepted.get("job") or "").strip() or None
                        if job_uuid is None:
                            raise RuntimeError("CardTrader update returned no job UUID")
                        await _update_command_state(
                            command_id,
                            "accepted",
                            job_uuid=job_uuid,
                            result={"status": "accepted", "job_uuid": job_uuid},
                        )
                    lease.refresh()
                    job_result = await _poll_job(client, job_uuid)
                    lease.refresh()
                    current_product = await client.get_product(int(claimed["target_product_id"]))
                    lease.refresh()
                    post_job_verified = _mutation_matches_product(
                        claimed["operation_type"],
                        claimed["payload"],
                        current_product,
                    )
                    if not post_job_verified:
                        raise CardTraderAPIError(
                            "Completed CardTrader job does not match requested payload",
                            outcome_unknown=True,
                        )
                    result = {
                        "status": "verified",
                        "command_id": str(command_id),
                        "job_uuid": job_uuid,
                        "job": job_result,
                    }
                elif claimed["operation_type"] == "delete_product":
                    if not await _revalidate_claim_before_remote_write(command_id):
                        return {
                            "status": "cancelled",
                            "command_id": str(command_id),
                        }
                    lease.refresh()
                    remote_started = True
                    await client.delete_product(int(claimed["target_product_id"]))
                    lease.refresh()
                    result = {
                        "status": "verified",
                        "command_id": str(command_id),
                    }
                else:
                    raise RuntimeError("Unsupported outbox operation")
            lease.refresh()

        await _update_command_state(
            command_id,
            "verified",
            job_uuid=job_uuid,
            result=result,
        )
        return result
    except CardTraderMutationBusyError as exc:
        next_status = "uncertain" if remote_started else "pending"
        error_code = _safe_failure_code(exc)
        await _update_command_state(
            command_id,
            next_status,
            error=error_code,
            job_uuid=job_uuid,
        )
        if remote_started:
            return {
                "status": "uncertain",
                "command_id": str(command_id),
                "error": error_code,
            }
        raise ExternalMutationBusyError("CardTrader command lease unavailable") from exc
    except RateLimitError:
        await _update_command_state(command_id, "pending", error="CardTrader rate limited")
        raise
    except TimeoutError as exc:
        error_code = _safe_failure_code(exc)
        await _update_command_state(
            command_id,
            "uncertain",
            error=error_code,
            job_uuid=job_uuid,
        )
        return {
            "status": "uncertain",
            "command_id": str(command_id),
            "error": error_code,
        }
    except CardTraderAPIError as exc:
        terminal = (
            "uncertain"
            if exc.outcome_unknown
            else (
                "failed"
                if exc.status_code is not None
                and 400 <= exc.status_code < 500
                and exc.status_code != 429
                else "uncertain"
            )
        )
        error_code = _safe_failure_code(exc)
        await _update_command_state(
            command_id,
            terminal,
            error=error_code,
            job_uuid=job_uuid,
        )
        return {"status": terminal, "command_id": str(command_id), "error": error_code}
    except Exception as exc:
        error_code = _safe_failure_code(exc)
        await _update_command_state(
            command_id,
            "uncertain",
            error=error_code,
            job_uuid=job_uuid,
        )
        return {
            "status": "uncertain",
            "command_id": str(command_id),
            "error": error_code,
        }


@celery_app.task(bind=True, max_retries=5, default_retry_delay=30)
def process_cardtrader_outbox_command(
    self,
    command_id: str,
) -> Dict[str, Any]:
    try:
        return run_async(_process_command(uuid.UUID(command_id)))
    except RateLimitError as exc:
        raise self.retry(exc=exc, countdown=min(300, 2**self.request.retries))
    except ExternalMutationBusyError as exc:
        raise self.retry(exc=exc, countdown=min(60, 2**self.request.retries))


@celery_app.task
def dispatch_pending_cardtrader_outbox(limit: int = 100) -> Dict[str, Any]:
    return run_async(_dispatch_pending(limit))


async def _recover_mature_uncertain(limit: int) -> Dict[str, int]:
    """Resolve mature unknown outcomes from one validated full export per user."""
    from app.services.reconciler import normalize_magic_snapshot, validate_snapshot

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=30)
    async with get_isolated_db_session() as session:
        commands = list(
            (
                await session.execute(
                    select(CardTraderOutbox)
                    .where(
                        CardTraderOutbox.status == "uncertain",
                        CardTraderOutbox.updated_at < cutoff,
                    )
                    .order_by(CardTraderOutbox.updated_at)
                    .limit(max(1, min(limit, 500)))
                )
            )
            .scalars()
            .all()
        )

    by_user: Dict[uuid.UUID, list[CardTraderOutbox]] = {}
    for command in commands:
        by_user.setdefault(command.user_id, []).append(command)

    recovered = 0
    deferred = 0
    rejected_exports = 0
    for user_id, user_commands in by_user.items():
        try:
            async with cardtrader_mutation_lease(user_id) as lease:
                async with get_isolated_db_session() as session:
                    sync_settings = (
                        await session.execute(
                            select(UserSyncSettings).where(UserSyncSettings.user_id == user_id)
                        )
                    ).scalar_one_or_none()
                    if sync_settings is None:
                        deferred += len(user_commands)
                        continue
                    previous_size = (
                        await session.execute(
                            select(SyncSnapshot.product_count)
                            .where(
                                SyncSnapshot.user_id == user_id,
                                SyncSnapshot.environment == "real",
                                SyncSnapshot.status == "applied",
                            )
                            .order_by(SyncSnapshot.created_at.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    local_active = (
                        await session.execute(
                            select(func.count())
                            .select_from(UserInventoryItem)
                            .where(
                                UserInventoryItem.user_id == user_id,
                                UserInventoryItem.source == "cardtrader",
                                UserInventoryItem.game_id == 1,
                                UserInventoryItem.environment == "real",
                                UserInventoryItem.lifecycle_status == "active",
                            )
                        )
                    ).scalar_one()
                    token = get_encryption_manager().decrypt(
                        sync_settings.cardtrader_token_encrypted
                    )

                async with CardTraderClient(token, str(user_id)) as client:
                    raw_products = await client.get_products_export()
                lease.refresh()
                normalized, shape_problems = normalize_magic_snapshot(raw_products)
                valid, coverage_problems = validate_snapshot(
                    raw_products,
                    previous_size,
                    local_active,
                )
                if not valid or shape_problems or coverage_problems:
                    rejected_exports += 1
                    deferred += len(user_commands)
                    continue

                products_by_id = {str(product["id"]): product for product in normalized}
                for command in user_commands:
                    lease.refresh()
                    resolution = await resolve_uncertain_command_from_export(
                        command.id,
                        products_by_id.get(str(command.target_product_id)),
                    )
                    if resolution in {"verified", "failed"}:
                        recovered += 1
                    else:
                        deferred += 1
        except CardTraderMutationBusyError:
            deferred += len(user_commands)
        except Exception as exc:
            logger.warning(
                "Uncertain CardTrader recovery deferred for user %s: %s",
                user_id,
                exc,
            )
            deferred += len(user_commands)

    return {
        "recovered": recovered,
        "deferred": deferred,
        "rejected_exports": rejected_exports,
    }


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
            )
            .scalars()
            .all()
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
            )
            .scalars()
            .all()
        )
    for command_id in stale_ids:
        await _update_command_state(
            command_id,
            "uncertain",
            error="Worker stopped while external outcome was unknown",
        )
    for command_id in ids:
        process_cardtrader_outbox_command.delay(str(command_id))
    recovery = await _recover_mature_uncertain(limit)
    return {
        "status": "dispatched",
        "count": len(ids),
        "stale_uncertain": len(stale_ids),
        "uncertain_recovered": recovery["recovered"],
        "uncertain_deferred": recovery["deferred"],
        "uncertain_exports_rejected": recovery["rejected_exports"],
    }
