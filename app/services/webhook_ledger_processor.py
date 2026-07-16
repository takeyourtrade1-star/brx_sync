"""Transactional CardTrader webhook inbox and order stock ledger."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session_context
from app.models.inventory import (
    OrderStockLedger,
    UserInventoryItem,
    UserSyncSettings,
    WebhookInbox,
)

logger = logging.getLogger(__name__)


class WebhookProcessingError(RuntimeError):
    """The event must be retried without committing a partial stock delta."""


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _normalise_items(items: Iterable[Dict[str, Any]]) -> dict[str, int]:
    quantities: dict[str, int] = {}
    for item in items:
        product_id = str(item.get("product_id") or "").strip()
        try:
            quantity = int(item.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if product_id and quantity > 0:
            quantities[product_id] = quantities.get(product_id, 0) + quantity
    return quantities


def _extract_order_id(payload: Dict[str, Any], order: Dict[str, Any]) -> str:
    value = (
        order.get("id")
        or payload.get("object_id")
        or payload.get("order_id")
        or (payload.get("object") or {}).get("id")
    )
    return str(value or "").strip()


def classify_order_action(cause: str, state: str, via_zero: bool) -> str:
    """Map CardTrader order semantics to one stock-ledger action."""

    if cause == "order.destroy" or state == "canceled":
        return "restore"
    if state == "request_for_cancel":
        return "ignore"
    if (not via_zero and state == "paid") or (via_zero and state == "hub_pending"):
        return "decrement"
    return "ignore"


async def mark_webhook_failed(webhook_id: str, error: Exception) -> None:
    async with get_db_session_context() as session:
        await session.execute(
            update(WebhookInbox)
            .where(
                WebhookInbox.webhook_id == webhook_id,
                WebhookInbox.status.notin_(["completed", "ignored"]),
            )
            .values(
                status="failed",
                attempts=WebhookInbox.attempts + 1,
                last_error=f"{type(error).__name__}: {error}"[:4000],
            )
        )


class WebhookLedgerProcessor:
    """Apply each CardTrader order transition exactly once."""

    async def process_order_webhook(
        self,
        webhook_id: str,
        payload: Dict[str, Any],
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not webhook_id or webhook_id == "unknown":
            raise WebhookProcessingError("Missing webhook ID")
        if not user_id:
            raise WebhookProcessingError("Missing platform user ID")

        try:
            user_uuid = uuid.UUID(user_id)
        except ValueError as exc:
            raise WebhookProcessingError("Invalid platform user ID") from exc

        cause = str(payload.get("cause") or "")
        mode = str(payload.get("mode") or "live").lower()
        raw_data = payload.get("data")
        order = raw_data if isinstance(raw_data, dict) else {}

        async with get_db_session_context() as session:
            await self._ensure_inbox(
                session,
                webhook_id,
                user_uuid,
                cause,
                mode,
                payload,
            )
            claimed = await self._claim_inbox(session, webhook_id)
            if claimed is None:
                return {
                    "status": "duplicate",
                    "webhook_id": webhook_id,
                    "reason": "already claimed or completed",
                }

            if mode == "test":
                result = {
                    "status": "ignored",
                    "webhook_id": webhook_id,
                    "reason": "test mode",
                }
                self._finish_inbox(claimed, "ignored", result)
                return result

            if cause not in {"order.create", "order.update", "order.destroy"}:
                result = {
                    "status": "ignored",
                    "webhook_id": webhook_id,
                    "reason": f"unsupported cause: {cause}",
                }
                self._finish_inbox(claimed, "ignored", result)
                return result

            order_id = _extract_order_id(payload, order)
            if not order_id:
                raise WebhookProcessingError("Missing CardTrader order ID")

            state = str(order.get("state") or "")
            via_zero = _as_bool(order.get("via_cardtrader_zero"))

            action = classify_order_action(cause, state, via_zero)
            if action == "restore":
                result = await self._restore_order(
                    session,
                    user_uuid,
                    order_id,
                    state or "destroyed",
                    webhook_id,
                )
            elif state == "request_for_cancel":
                result = {
                    "status": "ignored",
                    "webhook_id": webhook_id,
                    "order_id": order_id,
                    "reason": "cancellation requested but not final",
                }
            elif action == "decrement":
                execution_mode = (
                    await session.execute(
                        select(UserSyncSettings.execution_mode).where(
                            UserSyncSettings.user_id == user_uuid
                        )
                    )
                ).scalar_one_or_none()
                if execution_mode not in {"partial", "real"}:
                    result = {
                        "status": "ignored",
                        "webhook_id": webhook_id,
                        "order_id": order_id,
                        "reason": f"execution mode {execution_mode or 'missing'}",
                    }
                    self._finish_inbox(claimed, "ignored", result)
                    return result
                result = await self._decrement_order(
                    session,
                    user_uuid,
                    order_id,
                    _normalise_items(order.get("order_items") or []),
                    state,
                    via_zero,
                    webhook_id,
                    execution_mode,
                )
            else:
                result = {
                    "status": "ignored",
                    "webhook_id": webhook_id,
                    "order_id": order_id,
                    "reason": f"state {state or 'unknown'} requires no stock delta",
                }

            self._finish_inbox(claimed, "completed", result)
            return result

    async def _ensure_inbox(
        self,
        session: AsyncSession,
        webhook_id: str,
        user_id: uuid.UUID,
        cause: str,
        mode: str,
        payload: Dict[str, Any],
    ) -> None:
        await session.execute(
            pg_insert(WebhookInbox)
            .values(
                webhook_id=webhook_id,
                user_id=user_id,
                cause=cause,
                mode=mode,
                payload_json=payload,
                signature_valid=True,
                status="received",
            )
            .on_conflict_do_nothing(index_elements=[WebhookInbox.webhook_id])
        )

    async def _claim_inbox(
        self,
        session: AsyncSession,
        webhook_id: str,
    ) -> Optional[WebhookInbox]:
        result = await session.execute(
            update(WebhookInbox)
            .where(
                WebhookInbox.webhook_id == webhook_id,
                WebhookInbox.status.in_(["received", "failed"]),
            )
            .values(
                status="processing",
                attempts=WebhookInbox.attempts + 1,
                last_error=None,
            )
            .returning(WebhookInbox)
        )
        return result.scalar_one_or_none()

    async def _decrement_order(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        order_id: str,
        items: dict[str, int],
        state: str,
        via_zero: bool,
        webhook_id: str,
        environment: str,
    ) -> Dict[str, Any]:
        if not items:
            raise WebhookProcessingError("Order has no valid items")

        changed: list[dict[str, Any]] = []
        for product_id, sold_quantity in items.items():
            ledger = (
                await session.execute(
                    select(OrderStockLedger)
                    .where(
                        OrderStockLedger.user_id == user_id,
                        OrderStockLedger.order_id == order_id,
                        OrderStockLedger.external_stock_id == product_id,
                        OrderStockLedger.environment == environment,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()

            if ledger is not None and ledger.decrement_applied:
                ledger.last_state = state
                ledger.last_webhook_id = webhook_id
                continue

            inventory = (
                await session.execute(
                    select(UserInventoryItem)
                    .where(
                        UserInventoryItem.user_id == user_id,
                        UserInventoryItem.source == "cardtrader",
                        UserInventoryItem.environment == environment,
                        UserInventoryItem.external_stock_id == product_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if inventory is None:
                raise WebhookProcessingError(
                    f"Product {product_id} not found in local inventory"
                )

            old_quantity = inventory.quantity
            inventory.quantity = max(0, old_quantity - sold_quantity)
            applied_quantity = old_quantity - inventory.quantity
            inventory.lifecycle_status = (
                "sold_out" if inventory.quantity == 0 else "active"
            )
            inventory.sync_state = "synced"
            inventory.row_version += 1
            inventory.last_external_update_at = datetime.now(timezone.utc)
            inventory.updated_at = datetime.now(timezone.utc)
            await self._mirror_marketplace_listing(
                session,
                user_id,
                product_id,
                inventory.quantity,
            )

            if ledger is None:
                ledger = OrderStockLedger(
                    user_id=user_id,
                    order_id=order_id,
                    external_stock_id=product_id,
                    environment=environment,
                    quantity=applied_quantity,
                    via_cardtrader_zero=via_zero,
                    decrement_applied=True,
                    restore_applied=False,
                    last_state=state,
                    last_webhook_id=webhook_id,
                )
                session.add(ledger)
            else:
                ledger.quantity = applied_quantity
                ledger.via_cardtrader_zero = via_zero
                ledger.decrement_applied = True
                ledger.last_state = state
                ledger.last_webhook_id = webhook_id

            changed.append(
                {
                    "product_id": product_id,
                    "old_quantity": old_quantity,
                    "new_quantity": inventory.quantity,
                    "sold_quantity": sold_quantity,
                    "applied_quantity": applied_quantity,
                }
            )

        return {
            "status": "processed",
            "webhook_id": webhook_id,
            "order_id": order_id,
            "action": "decrement",
            "items": changed,
        }

    async def _restore_order(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        order_id: str,
        state: str,
        webhook_id: str,
    ) -> Dict[str, Any]:
        ledgers = list(
            (
                await session.execute(
                    select(OrderStockLedger)
                    .where(
                        OrderStockLedger.user_id == user_id,
                        OrderStockLedger.order_id == order_id,
                    )
                    .with_for_update()
                )
            ).scalars().all()
        )
        changed: list[dict[str, Any]] = []
        for ledger in ledgers:
            if not ledger.decrement_applied or ledger.restore_applied:
                ledger.last_state = state
                ledger.last_webhook_id = webhook_id
                continue

            inventory = (
                await session.execute(
                    select(UserInventoryItem)
                    .where(
                        UserInventoryItem.user_id == user_id,
                        UserInventoryItem.source == "cardtrader",
                        UserInventoryItem.environment == ledger.environment,
                        UserInventoryItem.external_stock_id == ledger.external_stock_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if inventory is None:
                raise WebhookProcessingError(
                    f"Product {ledger.external_stock_id} missing during restore"
                )

            old_quantity = inventory.quantity
            inventory.quantity += ledger.quantity
            inventory.lifecycle_status = "active" if inventory.quantity > 0 else "sold_out"
            inventory.sync_state = "synced"
            inventory.row_version += 1
            inventory.last_external_update_at = datetime.now(timezone.utc)
            inventory.updated_at = datetime.now(timezone.utc)
            await self._mirror_marketplace_listing(
                session,
                user_id,
                ledger.external_stock_id,
                inventory.quantity,
            )
            ledger.restore_applied = True
            ledger.last_state = state
            ledger.last_webhook_id = webhook_id
            changed.append(
                {
                    "product_id": ledger.external_stock_id,
                    "old_quantity": old_quantity,
                    "new_quantity": inventory.quantity,
                    "restored_quantity": ledger.quantity,
                }
            )

        return {
            "status": "processed",
            "webhook_id": webhook_id,
            "order_id": order_id,
            "action": "restore",
            "items": changed,
        }

    @staticmethod
    async def _mirror_marketplace_listing(
        session: AsyncSession,
        user_id: uuid.UUID,
        product_id: str,
        quantity: int,
    ) -> None:
        """Keep the marketplace sellable projection aligned in the same transaction."""

        try:
            article_id = int(product_id)
        except (TypeError, ValueError):
            return
        await session.execute(
            text(
                """
                UPDATE mkt_listings
                SET quantity = :quantity,
                    status = CASE WHEN :quantity > 0 THEN 'active' ELSE 'sold' END,
                    updated_at = NOW()
                WHERE user_id = CAST(:user_id AS uuid)
                  AND cardtrader_article_id = :article_id
                  AND status IN ('active','sold','pending_sync','sync_failed')
                """
            ),
            {
                "quantity": quantity,
                "user_id": str(user_id),
                "article_id": article_id,
            },
        )

    @staticmethod
    def _finish_inbox(
        inbox: WebhookInbox,
        status: str,
        result: Dict[str, Any],
    ) -> None:
        inbox.status = status
        inbox.result_json = result
        inbox.last_error = None
        inbox.processed_at = datetime.now(timezone.utc)
