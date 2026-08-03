"""Fail-safe CardTrader webhook processing backed by the durable inbox.

Webhook payloads are useful evidence that CardTrader may have changed stock,
but they are not an authoritative quantity snapshot.  This processor therefore
never applies arithmetic deltas.  It quarantines the affected inventory rows
and leaves ``reconcile_user_apply`` as the only inbound quantity writer.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_isolated_db_session
from app.models.inventory import (
    OrderStockLedger,
    UserInventoryItem,
    UserSyncSettings,
    WebhookInbox,
)

logger = logging.getLogger(__name__)

NORMAL_NON_STOCK_STATES = {"sent", "arrived", "done", "closed", "lost"}
TERMINAL_OR_UNCERTAIN_STATES = {"request_for_cancel", "canceled"}
ACTIVE_SYNC_STATUS = "active"


@dataclass(frozen=True)
class WebhookDecision:
    """Pure classification result for one order webhook."""

    action: str
    reason: str
    order_id: str | None
    items: tuple[dict[str, Any], ...]
    full_quarantine: bool = False

    @property
    def requires_reconcile(self) -> bool:
        return self.action == "reconcile"


def normalize_order_items(raw_items: Any) -> tuple[dict[str, Any], ...]:
    """Return only stable fields needed for quarantine and order evidence."""

    if not isinstance(raw_items, list):
        return ()

    normalized: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict) or raw.get("product_id") is None:
            continue
        try:
            quantity = int(raw.get("quantity", 0))
        except (TypeError, ValueError):
            continue
        if quantity <= 0:
            continue
        item: dict[str, Any] = {
            "product_id": str(raw["product_id"]),
            "quantity": quantity,
        }
        if raw.get("hub_pending_order_id") is not None:
            item["hub_pending_order_id"] = str(raw["hub_pending_order_id"])
        normalized.append(item)
    return tuple(normalized)


def classify_order_webhook(payload: dict[str, Any]) -> WebhookDecision:
    """Classify CardTrader order semantics without performing stock arithmetic."""

    cause = str(payload.get("cause") or "")
    data = payload.get("data")
    order = data if isinstance(data, dict) else {}
    order_id_raw = order.get("id") or payload.get("object_id")
    order_id = str(order_id_raw) if order_id_raw is not None else None

    if cause == "order.destroy":
        return WebhookDecision(
            action="reconcile",
            reason="destroy_requires_authoritative_snapshot",
            order_id=order_id,
            items=(),
            full_quarantine=True,
        )

    if cause not in {"order.create", "order.update"}:
        return WebhookDecision(
            action="ignore",
            reason="unsupported_cause",
            order_id=order_id,
            items=(),
        )

    items = normalize_order_items(order.get("order_items"))
    state = str(order.get("state") or "").lower()
    via_zero = bool(order.get("via_cardtrader_zero"))

    if state in TERMINAL_OR_UNCERTAIN_STATES:
        return WebhookDecision(
            action="reconcile",
            reason=f"{state}_requires_authoritative_snapshot",
            order_id=order_id,
            items=items,
            full_quarantine=not bool(items),
        )

    if via_zero and state == "hub_pending":
        # CardTrader documents hub_pending as the stock-changing CT Zero state.
        # For presales, only rows tied to this hub_pending order are relevant.
        if order.get("presale") and order_id is not None:
            presale_items = tuple(
                item for item in items if item.get("hub_pending_order_id") == order_id
            )
            items = presale_items
        return WebhookDecision(
            action="reconcile",
            reason="cardtrader_zero_hub_pending",
            order_id=order_id,
            items=items,
            full_quarantine=not bool(items),
        )

    if via_zero and state == "paid":
        # The weekly merged paid order must never be decremented.  Quarantine
        # and verify authoritatively to cover a delayed/missing hub_pending event.
        return WebhookDecision(
            action="reconcile",
            reason="cardtrader_zero_paid_verify_only",
            order_id=order_id,
            items=items,
            full_quarantine=not bool(items),
        )

    if not via_zero and state == "paid":
        return WebhookDecision(
            action="reconcile",
            reason="standard_paid_order",
            order_id=order_id,
            items=items,
            full_quarantine=not bool(items),
        )

    if state in NORMAL_NON_STOCK_STATES:
        return WebhookDecision(
            action="ignore",
            reason=f"normal_lifecycle_state:{state}",
            order_id=order_id,
            items=items,
        )

    # Unknown order states are not safe inputs for local arithmetic.  If item
    # evidence exists, quarantine it; otherwise quarantine the whole CT inventory.
    return WebhookDecision(
        action="reconcile",
        reason=f"unknown_order_state:{state or 'missing'}",
        order_id=order_id,
        items=items,
        full_quarantine=not bool(items),
    )


async def _store_order_evidence(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    environment: str,
    webhook_id: str,
    state: str,
    via_zero: bool,
    order_id: str | None,
    items: Iterable[dict[str, Any]],
) -> None:
    """Upsert item snapshots for destroy recovery; delta flags remain false."""

    if order_id is None:
        return
    for item in items:
        statement = pg_insert(OrderStockLedger).values(
            user_id=user_id,
            order_id=order_id,
            external_stock_id=item["product_id"],
            environment=environment,
            quantity=item["quantity"],
            via_cardtrader_zero=via_zero,
            decrement_applied=False,
            restore_applied=False,
            last_state=state or None,
            last_webhook_id=webhook_id,
        )
        await session.execute(
            statement.on_conflict_do_update(
                constraint="uq_cardtrader_order_stock_item",
                set_={
                    "quantity": item["quantity"],
                    "via_cardtrader_zero": via_zero,
                    "last_state": state or None,
                    "last_webhook_id": webhook_id,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        )


async def _load_order_evidence(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    environment: str,
    order_id: str | None,
) -> tuple[str, ...]:
    if order_id is None:
        return ()
    result = await session.execute(
        select(OrderStockLedger.external_stock_id).where(
            OrderStockLedger.user_id == user_id,
            OrderStockLedger.environment == environment,
            OrderStockLedger.order_id == order_id,
        )
    )
    return tuple(str(value) for value in result.scalars().all())


async def _quarantine_inventory(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    environment: str,
    inbox_id: int,
    product_ids: Iterable[str],
    full_quarantine: bool,
) -> int:
    """Attach the newest inbound watermark without changing stock quantities.

    An outgoing ``pending``/``accepted`` row retains that state so its own
    state machine can finish.  Its non-null inbound marker still makes it
    non-tradable and keeps the webhook pending until a later safe snapshot.
    """

    eligible_inbound_state = or_(
        UserInventoryItem.sync_uncertain_event_id.is_(None),
        UserInventoryItem.sync_uncertain_event_id <= inbox_id,
    )
    statement = (
        update(UserInventoryItem)
        .where(
            UserInventoryItem.user_id == user_id,
            UserInventoryItem.environment == environment,
            UserInventoryItem.source == "cardtrader",
            UserInventoryItem.sync_state.in_(("synced", "pending", "accepted", "uncertain")),
            eligible_inbound_state,
        )
        .values(
            sync_state=case(
                (
                    UserInventoryItem.sync_state.in_(("pending", "accepted")),
                    UserInventoryItem.sync_state,
                ),
                else_="uncertain",
            ),
            sync_uncertain_event_id=inbox_id,
            row_version=case(
                (
                    UserInventoryItem.sync_state.in_(("pending", "accepted")),
                    UserInventoryItem.row_version,
                ),
                else_=UserInventoryItem.row_version + 1,
            ),
            updated_at=datetime.now(timezone.utc),
        )
    )
    ids = tuple(dict.fromkeys(str(value) for value in product_ids if value))
    if not full_quarantine:
        if not ids:
            return 0
        statement = statement.where(UserInventoryItem.external_stock_id.in_(ids))
    result = await session.execute(statement)
    return int(result.rowcount or 0)


class WebhookLedgerProcessor:
    """Process one durable inbox row and quarantine affected inventory."""

    async def process_order_webhook(
        self,
        webhook_id: str,
        payload: dict[str, Any],
        user_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            user_uuid = uuid.UUID(user_id or "")
        except ValueError as exc:
            raise ValueError("Invalid or missing webhook user_id") from exc

        async with get_isolated_db_session() as session:
            inbox = await self._load_or_create_inbox(session, webhook_id, payload, user_uuid)
            if inbox.user_id != user_uuid:
                raise ValueError("Webhook inbox ownership mismatch")

            settings = (
                await session.execute(
                    select(UserSyncSettings)
                    .where(UserSyncSettings.user_id == user_uuid)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if settings is None:
                raise ValueError("User sync settings not found")
            return await self.prepare_inbox(
                session,
                inbox=inbox,
                payload=payload,
                settings=settings,
            )

    async def prepare_inbox(
        self,
        session: AsyncSession,
        *,
        inbox: WebhookInbox,
        payload: dict[str, Any],
        settings: UserSyncSettings,
    ) -> dict[str, Any]:
        """Quarantine and project one locked inbox row in the caller's txn."""

        webhook_id = inbox.webhook_id
        user_uuid = inbox.user_id
        environment = str(settings.execution_mode)

        if inbox.status in {"completed", "ignored"}:
            return dict(inbox.result_json or {"status": inbox.status})
        if inbox.status == "reconcile_pending":
            # Reassert the fail-closed listing projection on every replay.
            if environment in {"partial", "real"}:
                from app.services.marketplace_projection import (
                    project_inventory_to_marketplace,
                )

                await project_inventory_to_marketplace(session, user_uuid, environment)
            result = dict(inbox.result_json or {})
            result.setdefault("status", "reconcile_required")
            result["duplicate"] = True
            return result

        inbox.attempts += 1
        inbox.last_error = None

        if str(payload.get("mode") or inbox.mode).lower() == "test":
            return self._finish_inbox(
                inbox,
                status="ignored",
                result={
                    "status": "ignored",
                    "webhook_id": webhook_id,
                    "reason": "test_mode",
                },
            )

        if str(settings.sync_status) != ACTIVE_SYNC_STATUS or environment not in {
            "partial",
            "real",
        }:
            return self._finish_inbox(
                inbox,
                status="deferred",
                result={
                    "status": "deferred",
                    "webhook_id": webhook_id,
                    "reason": (
                        f"sync_status={settings.sync_status}," f"execution_mode={environment}"
                    ),
                },
                processed=False,
            )

        decision = classify_order_webhook(payload)
        order = payload.get("data")
        order_data = order if isinstance(order, dict) else {}
        state = str(order_data.get("state") or "").lower()
        via_zero = bool(order_data.get("via_cardtrader_zero"))
        await _store_order_evidence(
            session,
            user_id=user_uuid,
            environment=environment,
            webhook_id=webhook_id,
            state=state,
            via_zero=via_zero,
            order_id=decision.order_id,
            items=decision.items,
        )

        if not decision.requires_reconcile:
            return self._finish_inbox(
                inbox,
                status="completed",
                result={
                    "status": "ignored",
                    "webhook_id": webhook_id,
                    "reason": decision.reason,
                },
            )

        product_ids = tuple(item["product_id"] for item in decision.items)
        full_quarantine = decision.full_quarantine
        if payload.get("cause") == "order.destroy":
            ledger_ids = await _load_order_evidence(
                session,
                user_id=user_uuid,
                environment=environment,
                order_id=decision.order_id,
            )
            if ledger_ids:
                product_ids = ledger_ids
                full_quarantine = False

        quarantined = await _quarantine_inventory(
            session,
            user_id=user_uuid,
            environment=environment,
            inbox_id=inbox.id,
            product_ids=product_ids,
            full_quarantine=full_quarantine,
        )
        from app.services.marketplace_projection import (
            project_inventory_to_marketplace,
        )

        await project_inventory_to_marketplace(session, user_uuid, environment)
        return self._finish_inbox(
            inbox,
            status="reconcile_pending",
            result={
                "status": "reconcile_required",
                "webhook_id": webhook_id,
                "order_id": decision.order_id,
                "reason": decision.reason,
                "quarantined": quarantined,
                "full_quarantine": full_quarantine,
                "inbox_id": inbox.id,
                "quantity_delta_applied": False,
            },
            processed=False,
        )

    async def _load_or_create_inbox(
        self,
        session: AsyncSession,
        webhook_id: str,
        payload: dict[str, Any],
        user_id: uuid.UUID,
    ) -> WebhookInbox:
        inbox = (
            await session.execute(
                select(WebhookInbox).where(WebhookInbox.webhook_id == webhook_id).with_for_update()
            )
        ).scalar_one_or_none()
        if inbox is not None:
            return inbox

        inserted_id = (
            await session.execute(
                pg_insert(WebhookInbox)
                .values(
                    webhook_id=webhook_id,
                    user_id=user_id,
                    cause=str(payload.get("cause") or ""),
                    mode=str(payload.get("mode") or "live").lower(),
                    payload_json=payload,
                    signature_valid=True,
                    status="received",
                )
                .on_conflict_do_nothing(index_elements=[WebhookInbox.webhook_id])
                .returning(WebhookInbox.id)
            )
        ).scalar_one_or_none()
        if inserted_id is None:
            return (
                await session.execute(
                    select(WebhookInbox)
                    .where(WebhookInbox.webhook_id == webhook_id)
                    .with_for_update()
                )
            ).scalar_one()
        return await session.get(WebhookInbox, inserted_id)

    @staticmethod
    def _finish_inbox(
        inbox: WebhookInbox,
        *,
        status: str,
        result: dict[str, Any],
        processed: bool = True,
    ) -> dict[str, Any]:
        inbox.status = status
        inbox.result_json = result
        inbox.processed_at = datetime.now(timezone.utc) if processed else None
        return result


async def process_deferred_webhooks(
    user_id: uuid.UUID,
    *,
    limit: int = 100,
) -> dict[str, int]:
    """Reactivate durable deferred events before an authoritative reconcile."""

    async with get_isolated_db_session() as session:
        rows = (
            await session.execute(
                select(
                    WebhookInbox.webhook_id,
                    WebhookInbox.payload_json,
                )
                .where(
                    WebhookInbox.user_id == user_id,
                    WebhookInbox.status == "deferred",
                )
                .order_by(WebhookInbox.id.asc())
                .limit(limit)
            )
        ).all()

    processor = WebhookLedgerProcessor()
    processed = 0
    reconcile_pending = 0
    for webhook_id, payload in rows:
        result = await processor.process_order_webhook(
            webhook_id, dict(payload or {}), str(user_id)
        )
        processed += 1
        if result.get("status") == "reconcile_required":
            reconcile_pending += 1
    return {"processed": processed, "reconcile_pending": reconcile_pending}


async def mark_webhook_failed(webhook_id: str, error: Exception) -> None:
    """Record task failure without losing an already-quarantined event."""

    async with get_isolated_db_session() as session:
        inbox = (
            await session.execute(
                select(WebhookInbox).where(WebhookInbox.webhook_id == webhook_id).with_for_update()
            )
        ).scalar_one_or_none()
        if inbox is None or inbox.status in {"completed", "ignored"}:
            return
        if inbox.status != "reconcile_pending":
            inbox.status = "failed"
        inbox.last_error = type(error).__name__
