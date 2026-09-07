"""Idempotent inventory mutations used by the trades saga."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.internal_schemas import (
    CreditInventoryRequest,
    ReleaseInventoryRequest,
    ReserveInventoryRequest,
)
from app.core.config import get_settings
from app.core.crypto import get_encryption_manager
from app.models.inventory import (
    InventoryOperation,
    UserInventoryItem,
    UserSyncSettings,
)
from app.services.cardtrader_client import CardTraderAPIError, CardTraderClient
from app.services.cardtrader_mutation_lease import cardtrader_mutation_lease
from app.services.cardtrader_payloads import build_product_create_payload
from app.services.sync_policy import (
    CardTraderWriteBlockedError,
    SyncPolicySnapshot,
    assert_cardtrader_write_allowed,
)

logger = logging.getLogger(__name__)

ClientFactory = Callable[[str, str], Any]
TokenDecryptor = Callable[[str], str]
TRADABLE_SOURCES = ("cardtrader",)
settings = get_settings()


class InventoryOperationError(Exception):
    """Stable error returned by the internal inventory API."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 409,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.result = result


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _authoritative_remote_quantity(
    result: Any,
    *,
    expected_product_id: int,
) -> int:
    """Extract the authoritative post-mutation quantity or fail uncertain."""
    resource = result.get("resource") if isinstance(result, dict) else None
    identity_sources = [
        candidate for candidate in (result, resource) if isinstance(candidate, dict)
    ]
    for candidate in identity_sources:
        exposed_id = candidate.get("id")
        if exposed_id is not None:
            try:
                id_matches = (
                    not isinstance(exposed_id, bool) and int(exposed_id) == expected_product_id
                )
            except (TypeError, ValueError):
                id_matches = False
            if not id_matches:
                raise CardTraderAPIError(
                    "CardTrader increment response identifies a different product",
                    outcome_unknown=True,
                )
        exposed_game_id = candidate.get("game_id")
        if exposed_game_id is not None:
            try:
                game_matches = not isinstance(exposed_game_id, bool) and int(exposed_game_id) == 1
            except (TypeError, ValueError):
                game_matches = False
            if not game_matches:
                raise CardTraderAPIError(
                    "CardTrader increment response is not for Magic",
                    outcome_unknown=True,
                )
    quantity = (
        resource.get("quantity")
        if isinstance(resource, dict)
        else result.get("quantity") if isinstance(result, dict) else None
    )
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
        raise CardTraderAPIError(
            "CardTrader increment response has no authoritative quantity",
            outcome_unknown=True,
        )
    return quantity


async def _increment_cardtrader_for_trade(
    session: AsyncSession,
    user_id: UUID,
    client: Any,
    product_id: int,
    delta_quantity: int,
    expected_mode_version: Optional[int] = None,
) -> Dict[str, Any]:
    await _assert_trade_write_allowed(
        session,
        user_id,
        expected_mode_version=expected_mode_version,
    )
    await session.rollback()
    async with cardtrader_mutation_lease(user_id) as lease:
        lease.refresh()
        return await asyncio.wait_for(
            client.increment_product_quantity(product_id, delta_quantity),
            timeout=settings.TRADE_CARDTRADER_MUTATION_TIMEOUT_SECONDS,
        )


async def _create_cardtrader_for_trade(
    session: AsyncSession,
    user_id: UUID,
    client: Any,
    payload: Dict[str, Any],
    *,
    expected_mode_version: int,
) -> Dict[str, Any]:
    await _assert_trade_write_allowed(
        session,
        user_id,
        expected_mode_version=expected_mode_version,
    )
    await session.rollback()
    async with cardtrader_mutation_lease(user_id) as lease:
        lease.refresh()
        return await asyncio.wait_for(
            client.create_product(payload),
            timeout=settings.TRADE_CARDTRADER_MUTATION_TIMEOUT_SECONDS,
        )


async def _assert_trade_write_allowed(
    session: AsyncSession,
    user_id: UUID,
    *,
    expected_mode_version: Optional[int] = None,
) -> SyncPolicySnapshot:
    try:
        return await assert_cardtrader_write_allowed(
            session,
            user_id,
            expected_mode_version=expected_mode_version,
        )
    except CardTraderWriteBlockedError as exc:
        raise InventoryOperationError(
            "CARDTRADER_WRITES_BLOCKED",
            "CardTrader writes are blocked by the current sync policy",
            status_code=409,
        ) from exc


def _default_decryptor(encrypted_token: str) -> str:
    return get_encryption_manager().decrypt(encrypted_token)


def _reserve_payload(request: ReserveInventoryRequest) -> Dict[str, Any]:
    payload = request.model_dump(mode="json")
    payload["items"] = sorted(payload["items"], key=lambda item: item["item_id"])
    return payload


def _release_payload(request: ReleaseInventoryRequest) -> Dict[str, Any]:
    return request.model_dump(mode="json")


def _credit_payload(request: CreditInventoryRequest) -> Dict[str, Any]:
    return request.model_dump(mode="json")


async def _existing_operation(
    session: AsyncSession,
    op_key: str,
) -> Optional[Dict[str, Any]]:
    operation = (
        await session.execute(select(InventoryOperation).where(InventoryOperation.op_key == op_key))
    ).scalar_one_or_none()
    if operation is None:
        await session.rollback()
        return None

    snapshot = {
        "kind": operation.kind,
        "payload": operation.payload_json,
        "status": operation.status,
        "result": operation.result_json,
    }
    await session.rollback()
    return snapshot


async def _replay_or_raise(
    session: AsyncSession,
    *,
    op_key: str,
    kind: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    existing = await _existing_operation(session, op_key)
    if existing is None:
        raise InventoryOperationError(
            "OPERATION_CLAIM_FAILED",
            "Impossibile acquisire la chiave idempotente",
            status_code=500,
        )
    if existing["kind"] != kind or existing["payload"] != payload:
        raise InventoryOperationError(
            "IDEMPOTENCY_KEY_REUSED",
            "La stessa op_key e' gia' associata a un payload diverso",
        )
    if existing["status"] == "processing":
        raise InventoryOperationError(
            "OPERATION_IN_PROGRESS",
            "Operazione gia' in corso",
        )
    result = dict(existing["result"] or {})
    if existing["status"] == "failed":
        raise InventoryOperationError(
            result.get("code", "INVENTORY_OPERATION_FAILED"),
            result.get("message", "Operazione inventario fallita"),
            status_code=int(result.get("status_code", 409)),
            result=result,
        )
    result["replayed"] = True
    return result


async def _record_failed_claim(
    session: AsyncSession,
    *,
    op_key: str,
    kind: str,
    payload: Dict[str, Any],
    error: InventoryOperationError,
) -> None:
    result = {
        "op_key": op_key,
        "kind": kind,
        "status": "failed",
        "user_id": payload["user_id"],
        "items": [],
        "replayed": False,
        "code": error.code,
        "message": error.message,
        "status_code": error.status_code,
    }
    try:
        async with session.begin():
            session.add(
                InventoryOperation(
                    op_key=op_key,
                    kind=kind,
                    payload_json=payload,
                    result_json=result,
                    status="failed",
                    completed_at=_now(),
                )
            )
    except IntegrityError:
        await session.rollback()


async def _load_cardtrader_token(
    session: AsyncSession,
    user_id: UUID,
    decrypt_token: TokenDecryptor,
) -> str:
    encrypted = (
        await session.execute(
            select(UserSyncSettings.cardtrader_token_encrypted).where(
                UserSyncSettings.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    await session.rollback()
    if not encrypted:
        raise InventoryOperationError(
            "CARDTRADER_NOT_CONFIGURED",
            "Token CardTrader non configurato per l'utente",
        )
    token = decrypt_token(encrypted)
    if not token or not token.strip():
        raise InventoryOperationError(
            "CARDTRADER_NOT_CONFIGURED",
            "Token CardTrader non configurato per l'utente",
        )
    return token


def _snapshot_from_row(row: Any, requested_quantity: int) -> Dict[str, Any]:
    quantity_after = int(row.quantity)
    return {
        "item_id": int(row.id),
        "quantity": requested_quantity,
        "quantity_before": quantity_after + requested_quantity,
        "quantity_after": quantity_after,
        "reserved_quantity_after": int(row.reserved_quantity),
        "blueprint_id": int(row.blueprint_id),
        "price_cents": int(row.price_cents),
        "properties": row.properties,
        "description": row.description,
        "user_data_field": row.user_data_field,
        "graded": row.graded,
        "source": row.source,
        "external_stock_id": row.external_stock_id,
        "row_version": int(row.row_version),
        "cardtrader_reserved": False,
        "cardtrader_state": ("pending" if row.source == "cardtrader" else "not_applicable"),
    }


def _reservation_result(
    *,
    op_key: str,
    user_id: UUID,
    snapshots: List[Dict[str, Any]],
    status: str,
    replayed: bool = False,
    phase: Optional[str] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "op_key": op_key,
        "kind": "reserve",
        "status": status,
        "user_id": str(user_id),
        "items": [dict(item) for item in snapshots],
        "replayed": replayed,
    }
    if phase:
        result["phase"] = phase
    return result


async def _persist_reservation_progress(
    session: AsyncSession,
    *,
    op_key: str,
    user_id: UUID,
    snapshots: List[Dict[str, Any]],
    phase: str,
) -> None:
    async with session.begin():
        operation = (
            await session.execute(
                select(InventoryOperation)
                .where(InventoryOperation.op_key == op_key)
                .with_for_update()
            )
        ).scalar_one()
        if operation.status != "processing":
            return
        operation.result_json = _reservation_result(
            op_key=op_key,
            user_id=user_id,
            snapshots=snapshots,
            status="processing",
            phase=phase,
        )


async def _mark_reserved_item_synced(
    session: AsyncSession,
    *,
    user_id: UUID,
    snapshot: Dict[str, Any],
    remote_quantity: int,
) -> None:
    async with session.begin():
        updated = await session.execute(
            update(UserInventoryItem)
            .where(
                UserInventoryItem.id == snapshot["item_id"],
                UserInventoryItem.user_id == user_id,
                UserInventoryItem.row_version == snapshot["row_version"],
                UserInventoryItem.sync_state == "pending",
                UserInventoryItem.sync_uncertain_event_id.is_(None),
                UserInventoryItem.game_id == 1,
            )
            .values(
                quantity=remote_quantity,
                sync_state="synced",
                lifecycle_status=("active" if remote_quantity > 0 else "sold_out"),
                updated_at=func.now(),
            )
        )
        if updated.rowcount != 1:
            raise RuntimeError("Inventory row changed while CardTrader reserve was applying")


async def _insert_trade_row(
    session: AsyncSession,
    *,
    user_id: UUID,
    snapshot: Dict[str, Any],
) -> UserInventoryItem:
    item = UserInventoryItem(
        user_id=user_id,
        blueprint_id=snapshot["blueprint_id"],
        quantity=snapshot["quantity"],
        price_cents=snapshot["price_cents"],
        properties=snapshot.get("properties"),
        external_stock_id=None,
        source="trade",
        description=snapshot.get("description"),
        graded=snapshot.get("graded"),
    )
    session.add(item)
    await session.flush()
    return item


async def _compensate_failed_reservation(
    session: AsyncSession,
    *,
    operation_key: str,
    user_id: UUID,
    snapshots: List[Dict[str, Any]],
    availability: Dict[str, int],
    cardtrader_compensated: Iterable[int],
    cardtrader_compensation_failed: Iterable[int],
    error: InventoryOperationError,
) -> None:
    compensated = set(cardtrader_compensated)
    compensation_failed = set(cardtrader_compensation_failed)
    outcomes: List[Dict[str, Any]] = []

    async with session.begin():
        for snapshot in snapshots:
            item_id = snapshot["item_id"]
            quantity = snapshot["quantity"]
            outcome = "returned_to_owner"
            target_item_id = item_id

            if item_id in compensation_failed:
                await session.execute(
                    update(UserInventoryItem)
                    .where(
                        UserInventoryItem.id == item_id,
                        UserInventoryItem.user_id == user_id,
                        UserInventoryItem.reserved_quantity >= quantity,
                    )
                    .values(
                        reserved_quantity=UserInventoryItem.reserved_quantity - quantity,
                        lifecycle_status="sync_failed",
                        sync_state="uncertain",
                        updated_at=func.now(),
                    )
                )
                fallback = await _insert_trade_row(session, user_id=user_id, snapshot=snapshot)
                outcome = "returned_as_new_row"
                target_item_id = fallback.id
            else:
                quantity_value: Any = UserInventoryItem.quantity + quantity
                external_id = snapshot.get("external_stock_id")
                if (
                    snapshot["source"] == "cardtrader"
                    and item_id not in compensated
                    and external_id in availability
                ):
                    quantity_value = func.least(
                        UserInventoryItem.quantity + quantity,
                        availability[external_id],
                    )
                restored = await session.execute(
                    update(UserInventoryItem)
                    .where(
                        UserInventoryItem.id == item_id,
                        UserInventoryItem.user_id == user_id,
                        UserInventoryItem.reserved_quantity >= quantity,
                    )
                    .values(
                        quantity=quantity_value,
                        reserved_quantity=UserInventoryItem.reserved_quantity - quantity,
                        lifecycle_status="active",
                        sync_state="synced",
                        updated_at=func.now(),
                    )
                    .returning(UserInventoryItem.id)
                )
                if restored.scalar_one_or_none() is None:
                    fallback = await _insert_trade_row(session, user_id=user_id, snapshot=snapshot)
                    outcome = "returned_as_new_row"
                    target_item_id = fallback.id

            outcomes.append(
                {
                    "item_id": item_id,
                    "quantity": quantity,
                    "outcome": outcome,
                    "target_item_id": target_item_id,
                }
            )

        operation = (
            await session.execute(
                select(InventoryOperation).where(InventoryOperation.op_key == operation_key)
            )
        ).scalar_one()
        operation.status = "failed"
        operation.completed_at = _now()
        operation.result_json = {
            "op_key": operation_key,
            "kind": "reserve",
            "status": "failed",
            "user_id": str(user_id),
            "items": outcomes,
            "replayed": False,
            "code": error.code,
            "message": error.message,
            "status_code": error.status_code,
        }


async def reserve_inventory(
    session: AsyncSession,
    request: ReserveInventoryRequest,
    *,
    client_factory: ClientFactory = CardTraderClient,
    decrypt_token: TokenDecryptor = _default_decryptor,
) -> Dict[str, Any]:
    """Atomically reserve a user's batch, then mirror linked rows on CardTrader."""
    payload = _reserve_payload(request)
    operation = InventoryOperation(
        op_key=request.op_key,
        kind="reserve",
        payload_json=payload,
        status="processing",
    )
    snapshots: List[Dict[str, Any]] = []

    try:
        async with session.begin():
            policy = await _assert_trade_write_allowed(session, request.user_id)
            session.add(operation)
            await session.flush()
            for requested in sorted(request.items, key=lambda item: item.item_id):
                reserved = await session.execute(
                    update(UserInventoryItem)
                    .where(
                        UserInventoryItem.id == requested.item_id,
                        UserInventoryItem.user_id == request.user_id,
                        UserInventoryItem.source.in_(TRADABLE_SOURCES),
                        UserInventoryItem.environment == "real",
                        UserInventoryItem.lifecycle_status == "active",
                        UserInventoryItem.sync_state == "synced",
                        UserInventoryItem.sync_uncertain_event_id.is_(None),
                        or_(
                            UserInventoryItem.source != "cardtrader",
                            and_(
                                UserInventoryItem.game_id == 1,
                                UserInventoryItem.mapping_status == "mapped",
                            ),
                        ),
                        UserInventoryItem.quantity >= requested.quantity,
                    )
                    .values(
                        quantity=UserInventoryItem.quantity - requested.quantity,
                        reserved_quantity=(
                            UserInventoryItem.reserved_quantity + requested.quantity
                        ),
                        sync_state="pending",
                        row_version=UserInventoryItem.row_version + 1,
                        updated_at=func.now(),
                    )
                    .returning(
                        UserInventoryItem.id,
                        UserInventoryItem.quantity,
                        UserInventoryItem.reserved_quantity,
                        UserInventoryItem.blueprint_id,
                        UserInventoryItem.price_cents,
                        UserInventoryItem.properties,
                        UserInventoryItem.description,
                        UserInventoryItem.user_data_field,
                        UserInventoryItem.graded,
                        UserInventoryItem.source,
                        UserInventoryItem.external_stock_id,
                        UserInventoryItem.row_version,
                    )
                )
                row = reserved.one_or_none()
                if row is None:
                    raise InventoryOperationError(
                        "INVENTORY_UNAVAILABLE",
                        f"Item {requested.item_id} non disponibile o non scambiabile",
                    )
                if row.source == "cardtrader" and not row.external_stock_id:
                    raise InventoryOperationError(
                        "INVENTORY_SOURCE_INVALID",
                        f"Item {requested.item_id} CardTrader senza external_stock_id",
                        status_code=500,
                    )
                if row.source == "trade" and row.external_stock_id:
                    raise InventoryOperationError(
                        "INVENTORY_SOURCE_INVALID",
                        f"Item {requested.item_id} trade collegato per errore a CardTrader",
                        status_code=500,
                    )
                snapshots.append(_snapshot_from_row(row, requested.quantity))
            operation.result_json = _reservation_result(
                op_key=request.op_key,
                user_id=request.user_id,
                snapshots=snapshots,
                status="processing",
                phase="local_reserved",
            )
    except IntegrityError:
        await session.rollback()
        return await _replay_or_raise(
            session, op_key=request.op_key, kind="reserve", payload=payload
        )
    except InventoryOperationError as error:
        await session.rollback()
        await _record_failed_claim(
            session,
            op_key=request.op_key,
            kind="reserve",
            payload=payload,
            error=error,
        )
        raise error

    ct_items = [item for item in snapshots if item["source"] == "cardtrader"]

    try:
        if ct_items:
            token = await _load_cardtrader_token(session, request.user_id, decrypt_token)
            async with client_factory(token, str(request.user_id)) as client:
                for item in ct_items:
                    item["cardtrader_state"] = "applying"
                    await _persist_reservation_progress(
                        session,
                        op_key=request.op_key,
                        user_id=request.user_id,
                        snapshots=snapshots,
                        phase="cardtrader_applying",
                    )
                    increment_result = await _increment_cardtrader_for_trade(
                        session,
                        request.user_id,
                        client,
                        int(item["external_stock_id"]),
                        -item["quantity"],
                        expected_mode_version=policy.mode_version,
                    )
                    remote_quantity = _authoritative_remote_quantity(
                        increment_result,
                        expected_product_id=int(item["external_stock_id"]),
                    )
                    item["cardtrader_reserved"] = True
                    item["cardtrader_state"] = "applied"
                    item["remote_quantity"] = remote_quantity
                    await _mark_reserved_item_synced(
                        session,
                        user_id=request.user_id,
                        snapshot=item,
                        remote_quantity=remote_quantity,
                    )
                    await _persist_reservation_progress(
                        session,
                        op_key=request.op_key,
                        user_id=request.user_id,
                        snapshots=snapshots,
                        phase="cardtrader_applied",
                    )
    except InventoryOperationError as operation_error:
        await _compensate_failed_reservation(
            session,
            operation_key=request.op_key,
            user_id=request.user_id,
            snapshots=snapshots,
            availability={},
            cardtrader_compensated=(),
            cardtrader_compensation_failed=(),
            error=operation_error,
        )
        raise operation_error
    except Exception as external_error:
        # A transport/process failure after POST /increment has an unknown
        # outcome. Never compensate or expose the local stock here: the
        # periodic recovery verifies CardTrader and completes idempotently.
        await _persist_reservation_progress(
            session,
            op_key=request.op_key,
            user_id=request.user_id,
            snapshots=snapshots,
            phase="cardtrader_pending_recovery",
        )
        pending_error = InventoryOperationError(
            "CARDTRADER_RESERVATION_PENDING",
            "Prenotazione CardTrader in verifica automatica",
            status_code=503,
        )
        raise pending_error from external_error

    result = _reservation_result(
        op_key=request.op_key,
        user_id=request.user_id,
        snapshots=snapshots,
        status="succeeded",
    )
    async with session.begin():
        stored = (
            await session.execute(
                select(InventoryOperation).where(InventoryOperation.op_key == request.op_key)
            )
        ).scalar_one()
        stored.status = "succeeded"
        stored.result_json = result
        stored.completed_at = _now()
    return result


class _ReservationRecoveryAmbiguous(Exception):
    """CardTrader moved to a quantity that cannot prove our POST outcome."""


class _RecoveryCASFailed(_ReservationRecoveryAmbiguous):
    """A quarantined or concurrently changed row rejected recovery."""


async def _complete_recovered_reservation(
    session: AsyncSession,
    *,
    op_key: str,
    user_id: UUID,
    snapshots: List[Dict[str, Any]],
) -> bool:
    async with session.begin():
        operation = (
            await session.execute(
                select(InventoryOperation)
                .where(InventoryOperation.op_key == op_key)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if operation is None or operation.status != "processing":
            return False
        for snapshot in snapshots:
            if snapshot.get("source") != "cardtrader" or not snapshot.get(
                "recovery_local_finalize"
            ):
                continue
            updated = await session.execute(
                update(UserInventoryItem)
                .where(
                    UserInventoryItem.id == snapshot["item_id"],
                    UserInventoryItem.user_id == user_id,
                    UserInventoryItem.source == "cardtrader",
                    UserInventoryItem.game_id == 1,
                    UserInventoryItem.row_version == snapshot["row_version"],
                    UserInventoryItem.sync_state == "pending",
                    UserInventoryItem.sync_uncertain_event_id.is_(None),
                )
                .values(
                    sync_state="synced",
                    row_version=UserInventoryItem.row_version + 1,
                    updated_at=func.now(),
                )
                .returning(UserInventoryItem.row_version)
            )
            new_row_version = updated.scalar_one_or_none()
            if new_row_version is None:
                raise _RecoveryCASFailed(
                    f"Reserve recovery CAS rejected item {snapshot['item_id']}"
                )
            snapshot["row_version"] = int(new_row_version)
            snapshot["recovery_local_finalize"] = False
        operation.status = "succeeded"
        operation.result_json = _reservation_result(
            op_key=op_key,
            user_id=user_id,
            snapshots=snapshots,
            status="succeeded",
        )
        operation.completed_at = _now()
        return True


async def recover_stale_reservations(
    session: AsyncSession,
    *,
    stale_minutes: int = 5,
    limit: int = 50,
    client_factory: ClientFactory = CardTraderClient,
    decrypt_token: TokenDecryptor = _default_decryptor,
) -> Dict[str, int]:
    """Recover crash/timeout windows without ever reopening uncertain stock.

    Recovery is intentionally conservative. If CardTrader moved to neither
    the snapshot quantity-before nor quantity-after, the operation remains
    processing for manual/reconciler inspection instead of guessing.
    """
    claimed: List[tuple[str, UUID, List[Dict[str, Any]]]] = []
    async with session.begin():
        operation_filters = [
            InventoryOperation.kind == "reserve",
            InventoryOperation.status == "processing",
        ]
        if stale_minutes > 0:
            operation_filters.append(
                InventoryOperation.updated_at <= _now() - timedelta(minutes=stale_minutes)
            )
        operations = (
            (
                await session.execute(
                    select(InventoryOperation)
                    .where(*operation_filters)
                    .order_by(InventoryOperation.updated_at.asc())
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for operation in operations:
            result = dict(operation.result_json or {})
            snapshots = [dict(item) for item in result.get("items") or []]
            user_id = UUID(str(operation.payload_json["user_id"]))
            result.update(
                {
                    "phase": "recovery_claimed",
                    "last_recovery_at": _now().isoformat(),
                    "items": snapshots,
                }
            )
            operation.result_json = result
            claimed.append((operation.op_key, user_id, snapshots))

    grouped: Dict[UUID, List[tuple[str, List[Dict[str, Any]]]]] = {}
    for op_key, user_id, snapshots in claimed:
        grouped.setdefault(user_id, []).append((op_key, snapshots))

    recovered = 0
    pending = 0
    ambiguous = 0
    for user_id, user_operations in grouped.items():
        ct_required = any(
            item.get("source") == "cardtrader" and item.get("cardtrader_state") != "applied"
            for _, snapshots in user_operations
            for item in snapshots
        )
        availability: Dict[str, int] = {}
        client_context: Any = None
        client: Any = None
        try:
            if ct_required:
                token = await _load_cardtrader_token(session, user_id, decrypt_token)
                client_context = client_factory(token, str(user_id))
                client = await client_context.__aenter__()
                products = await client.get_products_export()
                from app.services.reconciler import (
                    normalize_magic_snapshot,
                    validate_snapshot,
                )

                normalized, shape_problems = normalize_magic_snapshot(products)
                snapshot_ok, coverage_problems = validate_snapshot(products, None)
                if not snapshot_ok or shape_problems or coverage_problems:
                    raise _ReservationRecoveryAmbiguous(
                        "CardTrader Magic export non valido durante recovery"
                    )
                availability = {
                    str(product["id"]): int(product.get("quantity", 0)) for product in normalized
                }
            for op_key, snapshots in user_operations:
                try:
                    for item in snapshots:
                        if item.get("source") != "cardtrader":
                            continue
                        if item.get("cardtrader_state") == "applied":
                            continue
                        external_id = str(item["external_stock_id"])
                        current = availability.get(external_id, 0)
                        quantity_before = int(item["quantity_before"])
                        quantity_after = int(item["quantity_after"])

                        if current == quantity_after:
                            # The unknown POST is already reflected remotely.
                            pass
                        else:
                            raise _ReservationRecoveryAmbiguous(
                                f"item={item['item_id']} current={current} "
                                f"before={quantity_before} after={quantity_after}"
                            )

                        item["cardtrader_reserved"] = True
                        item["cardtrader_state"] = "applied"
                        item["recovery_local_finalize"] = True
                        await _persist_reservation_progress(
                            session,
                            op_key=op_key,
                            user_id=user_id,
                            snapshots=snapshots,
                            phase="recovery_verified",
                        )

                    if await _complete_recovered_reservation(
                        session,
                        op_key=op_key,
                        user_id=user_id,
                        snapshots=snapshots,
                    ):
                        recovered += 1
                except _ReservationRecoveryAmbiguous as exc:
                    ambiguous += 1
                    logger.error(
                        "Inventory reservation recovery ambiguous op_key=%s: %s",
                        op_key,
                        exc,
                    )
                    await _persist_reservation_progress(
                        session,
                        op_key=op_key,
                        user_id=user_id,
                        snapshots=snapshots,
                        phase="manual_review_required",
                    )
                except Exception as exc:
                    pending += 1
                    logger.error(
                        "Inventory reservation recovery still pending op_key=%s (%s)",
                        op_key,
                        type(exc).__name__,
                    )
                    await _persist_reservation_progress(
                        session,
                        op_key=op_key,
                        user_id=user_id,
                        snapshots=snapshots,
                        phase="recovery_pending",
                    )
        except Exception as exc:
            pending += len(user_operations)
            logger.error(
                "Inventory reservation recovery unavailable user_id=%s (%s)",
                user_id,
                type(exc).__name__,
            )
        finally:
            if client_context is not None:
                try:
                    await client_context.__aexit__(None, None, None)
                except Exception as exc:
                    logger.error(
                        "Error closing CardTrader recovery client user_id=%s (%s)",
                        user_id,
                        type(exc).__name__,
                    )

    return {
        "scanned": len(claimed),
        "recovered": recovered,
        "pending": pending,
        "ambiguous": ambiguous,
    }


async def _load_reservation_result(
    session: AsyncSession,
    request: ReleaseInventoryRequest,
) -> List[Dict[str, Any]]:
    reservation = (
        await session.execute(
            select(InventoryOperation).where(
                InventoryOperation.op_key == request.reservation_op_key
            )
        )
    ).scalar_one_or_none()
    if reservation is None or reservation.kind != "reserve":
        await session.rollback()
        raise InventoryOperationError(
            "RESERVATION_NOT_FOUND", "Prenotazione originale non trovata", status_code=404
        )
    reservation_status = reservation.status
    result = dict(reservation.result_json or {})
    await session.rollback()
    if reservation_status != "succeeded":
        raise InventoryOperationError(
            "RESERVATION_NOT_RELEASABLE", "Prenotazione originale non completata"
        )
    if result.get("user_id") != str(request.user_id):
        raise InventoryOperationError(
            "RESERVATION_USER_MISMATCH", "La prenotazione appartiene a un altro utente"
        )
    return list(result.get("items") or [])


def _release_result(
    *,
    op_key: str,
    user_id: UUID,
    staged: List[Dict[str, Any]],
    status: str,
    replayed: bool = False,
    phase: Optional[str] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "op_key": op_key,
        "kind": "release",
        "status": status,
        "user_id": str(user_id),
        "items": [dict(item) for item in staged],
        "replayed": replayed,
    }
    if phase:
        result["phase"] = phase
    return result


async def _persist_release_progress(
    session: AsyncSession,
    *,
    op_key: str,
    user_id: UUID,
    staged: List[Dict[str, Any]],
    phase: str,
) -> None:
    async with session.begin():
        operation = (
            await session.execute(
                select(InventoryOperation)
                .where(InventoryOperation.op_key == op_key)
                .with_for_update()
            )
        ).scalar_one()
        if operation.status != "processing":
            return
        operation.result_json = _release_result(
            op_key=op_key,
            user_id=user_id,
            staged=staged,
            status="processing",
            phase=phase,
        )


async def _apply_released_cardtrader_item(
    session: AsyncSession,
    *,
    op_key: str,
    user_id: UUID,
    staged: List[Dict[str, Any]],
    item: Dict[str, Any],
    remote_quantity: int,
    external_stock_id: Optional[str] = None,
) -> None:
    """Apply local release and operation progress in the same transaction."""
    async with session.begin():
        old_external_stock_id = str(item["external_stock_id"])
        values: Dict[str, Any] = {
            "quantity": remote_quantity,
            "reserved_quantity": (
                UserInventoryItem.reserved_quantity - int(item["quantity"])
            ),
            "lifecycle_status": "active",
            "sync_state": "synced",
            "row_version": UserInventoryItem.row_version + 1,
            "updated_at": func.now(),
        }
        if external_stock_id is not None:
            values["external_stock_id"] = external_stock_id
            values["user_data_field"] = item.get("restore_user_data_field")
        updated = await session.execute(
            update(UserInventoryItem)
            .where(
                UserInventoryItem.id == item["target_item_id"],
                UserInventoryItem.user_id == user_id,
                UserInventoryItem.source == "cardtrader",
                UserInventoryItem.game_id == 1,
                UserInventoryItem.row_version == item["row_version"],
                UserInventoryItem.sync_state == "pending",
                UserInventoryItem.sync_uncertain_event_id.is_(None),
                UserInventoryItem.reserved_quantity >= int(item["quantity"]),
            )
            .values(**values)
            .returning(UserInventoryItem.id, UserInventoryItem.row_version)
        )
        updated_row = updated.one_or_none()
        if updated_row is None:
            raise _RecoveryCASFailed(
                "Inventory row changed or was quarantined while release was applying"
            )
        item["target_item_id"] = int(updated_row.id)
        item["row_version"] = int(updated_row.row_version)
        if external_stock_id is not None:
            item["external_stock_id"] = external_stock_id
            item["restored_external_stock_id"] = external_stock_id
            listing_table_exists = await session.scalar(
                text("SELECT to_regclass('mkt_listings') IS NOT NULL")
            )
            if listing_table_exists:
                await session.execute(
                    text("""
                        UPDATE mkt_listings
                        SET cardtrader_article_id = CAST(:new_product_id AS integer),
                            cardtrader_synced_at = NOW(),
                            updated_at = NOW()
                        WHERE user_id = CAST(:user_id AS uuid)
                          AND cardtrader_article_id = CAST(:old_product_id AS integer)
                    """),
                    {
                        "user_id": str(user_id),
                        "old_product_id": old_external_stock_id,
                        "new_product_id": external_stock_id,
                    },
                )
        item["outcome"] = "returned_to_owner"
        item["cardtrader_restored"] = True
        item["cardtrader_state"] = "applied"
        operation = (
            await session.execute(
                select(InventoryOperation)
                .where(InventoryOperation.op_key == op_key)
                .with_for_update()
            )
        ).scalar_one()
        operation.result_json = _release_result(
            op_key=op_key,
            user_id=user_id,
            staged=staged,
            status="processing",
            phase="cardtrader_release_applied",
        )


async def _complete_recovered_release(
    session: AsyncSession,
    *,
    op_key: str,
    user_id: UUID,
    staged: List[Dict[str, Any]],
) -> bool:
    async with session.begin():
        operation = (
            await session.execute(
                select(InventoryOperation)
                .where(InventoryOperation.op_key == op_key)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if operation is None or operation.status != "processing":
            return False
        operation.status = "succeeded"
        operation.result_json = _release_result(
            op_key=op_key,
            user_id=user_id,
            staged=staged,
            status="succeeded",
        )
        operation.completed_at = _now()
        return True


async def recover_stale_releases(
    session: AsyncSession,
    *,
    stale_minutes: int = 5,
    limit: int = 50,
    client_factory: ClientFactory = CardTraderClient,
    decrypt_token: TokenDecryptor = _default_decryptor,
) -> Dict[str, int]:
    """Recover release outcomes without duplicating returned inventory."""
    claimed: List[tuple[str, UUID, List[Dict[str, Any]]]] = []
    async with session.begin():
        operation_filters = [
            InventoryOperation.kind == "release",
            InventoryOperation.status == "processing",
        ]
        if stale_minutes > 0:
            operation_filters.append(
                InventoryOperation.updated_at <= _now() - timedelta(minutes=stale_minutes)
            )
        operations = (
            (
                await session.execute(
                    select(InventoryOperation)
                    .where(*operation_filters)
                    .order_by(InventoryOperation.updated_at.asc())
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for operation in operations:
            result = dict(operation.result_json or {})
            staged = [dict(item) for item in result.get("items") or []]
            user_id = UUID(str(operation.payload_json["user_id"]))
            result.update(
                {
                    "phase": "release_recovery_claimed",
                    "last_recovery_at": _now().isoformat(),
                    "items": staged,
                }
            )
            operation.result_json = result
            claimed.append((operation.op_key, user_id, staged))

    grouped: Dict[UUID, List[tuple[str, List[Dict[str, Any]]]]] = {}
    for op_key, user_id, staged in claimed:
        grouped.setdefault(user_id, []).append((op_key, staged))

    recovered = 0
    pending = 0
    ambiguous = 0
    for user_id, user_operations in grouped.items():
        ct_required = any(
            item.get("source") == "cardtrader" and item.get("cardtrader_state") != "applied"
            for _, staged in user_operations
            for item in staged
        )
        availability: Dict[str, int] = {}
        products_by_marker: Dict[str, List[Dict[str, Any]]] = {}
        client_context: Any = None
        client: Any = None
        try:
            if ct_required:
                token = await _load_cardtrader_token(session, user_id, decrypt_token)
                client_context = client_factory(token, str(user_id))
                client = await client_context.__aenter__()
                products = await client.get_products_export()
                from app.services.reconciler import (
                    normalize_magic_snapshot,
                    validate_snapshot,
                )

                normalized, shape_problems = normalize_magic_snapshot(products)
                snapshot_ok, coverage_problems = validate_snapshot(products, None)
                if not snapshot_ok or shape_problems or coverage_problems:
                    raise _ReservationRecoveryAmbiguous(
                        "CardTrader Magic export non valido durante release recovery"
                    )
                availability = {
                    str(product["id"]): int(product.get("quantity", 0)) for product in normalized
                }
                for product in normalized:
                    marker = product.get("user_data_field")
                    if isinstance(marker, str) and marker:
                        products_by_marker.setdefault(marker, []).append(product)

            for op_key, staged in user_operations:
                try:
                    for item in staged:
                        if item.get("source") != "cardtrader":
                            continue
                        if item.get("cardtrader_state") == "applied":
                            continue
                        external_id = str(item["external_stock_id"])
                        quantity_before = int(item["quantity_before"])
                        quantity_after = int(item["quantity_after"])
                        restored_external_stock_id: Optional[str] = None

                        if item.get("recreate_product"):
                            marker = str(item.get("restore_user_data_field") or "")
                            matches = products_by_marker.get(marker, [])
                            if len(matches) != 1:
                                raise _ReservationRecoveryAmbiguous(
                                    f"release item={item['item_id']} recreate marker count={len(matches)}"
                                )
                            recreated = matches[0]
                            current = int(recreated.get("quantity") or 0)
                            if current != quantity_before:
                                raise _ReservationRecoveryAmbiguous(
                                    f"release item={item['item_id']} recreated={current} "
                                    f"before={quantity_before}"
                                )
                            remote_quantity = current
                            restored_external_stock_id = str(recreated["id"])
                        else:
                            current = availability.get(external_id, 0)
                            if current == quantity_before:
                                # Exact authoritative target proves the unknown
                                # increment is already reflected. Never resend.
                                remote_quantity = current
                            else:
                                raise _ReservationRecoveryAmbiguous(
                                    f"release item={item['item_id']} current={current} "
                                    f"before={quantity_before} after={quantity_after}"
                                )

                        await _apply_released_cardtrader_item(
                            session,
                            op_key=op_key,
                            user_id=user_id,
                            staged=staged,
                            item=item,
                            remote_quantity=remote_quantity,
                            external_stock_id=restored_external_stock_id,
                        )

                    if await _complete_recovered_release(
                        session,
                        op_key=op_key,
                        user_id=user_id,
                        staged=staged,
                    ):
                        recovered += 1
                except _ReservationRecoveryAmbiguous as exc:
                    ambiguous += 1
                    logger.error(
                        "Inventory release recovery ambiguous op_key=%s: %s",
                        op_key,
                        exc,
                    )
                    await _persist_release_progress(
                        session,
                        op_key=op_key,
                        user_id=user_id,
                        staged=staged,
                        phase="release_manual_review_required",
                    )
                except Exception as exc:
                    pending += 1
                    logger.error(
                        "Inventory release recovery still pending op_key=%s (%s)",
                        op_key,
                        type(exc).__name__,
                    )
                    await _persist_release_progress(
                        session,
                        op_key=op_key,
                        user_id=user_id,
                        staged=staged,
                        phase="release_recovery_pending",
                    )
        except Exception as exc:
            pending += len(user_operations)
            logger.error(
                "Inventory release recovery unavailable user_id=%s (%s)",
                user_id,
                type(exc).__name__,
            )
        finally:
            if client_context is not None:
                try:
                    await client_context.__aexit__(None, None, None)
                except Exception as exc:
                    logger.error(
                        "Error closing CardTrader release recovery client user_id=%s (%s)",
                        user_id,
                        type(exc).__name__,
                    )

    return {
        "scanned": len(claimed),
        "recovered": recovered,
        "pending": pending,
        "ambiguous": ambiguous,
    }


async def release_inventory(
    session: AsyncSession,
    request: ReleaseInventoryRequest,
    *,
    client_factory: ClientFactory = CardTraderClient,
    decrypt_token: TokenDecryptor = _default_decryptor,
) -> Dict[str, Any]:
    """Release a successful reservation, with local fallback if CT is gone."""
    payload = _release_payload(request)
    snapshots = await _load_reservation_result(session, request)
    policy: Optional[SyncPolicySnapshot] = None
    if any(item.get("source") == "cardtrader" for item in snapshots):
        policy = await _assert_trade_write_allowed(session, request.user_id)
        await session.rollback()
    operation = InventoryOperation(
        op_key=request.op_key,
        kind="release",
        payload_json=payload,
        status="processing",
    )
    staged: List[Dict[str, Any]] = []

    try:
        async with session.begin():
            session.add(operation)
            await session.flush()
            for snapshot in snapshots:
                staged_item = dict(snapshot)
                staged_item["original_item_id"] = snapshot["item_id"]
                staged_item["cardtrader_restored"] = False

                if snapshot["source"] == "trade":
                    restored = await session.execute(
                        update(UserInventoryItem)
                        .where(
                            UserInventoryItem.id == snapshot["item_id"],
                            UserInventoryItem.user_id == request.user_id,
                            UserInventoryItem.source == "trade",
                            UserInventoryItem.reserved_quantity >= snapshot["quantity"],
                        )
                        .values(
                            quantity=UserInventoryItem.quantity + snapshot["quantity"],
                            reserved_quantity=(
                                UserInventoryItem.reserved_quantity - snapshot["quantity"]
                            ),
                            updated_at=func.now(),
                        )
                        .returning(UserInventoryItem.id)
                    )
                    target_id = restored.scalar_one_or_none()
                    if target_id is None:
                        target_id = (
                            await _insert_trade_row(
                                session, user_id=request.user_id, snapshot=snapshot
                            )
                        ).id
                        staged_item["outcome"] = "returned_as_new_row"
                    else:
                        staged_item["outcome"] = "returned_to_owner"
                    staged_item["target_item_id"] = target_id
                else:
                    target = (
                        await session.execute(
                            select(
                                UserInventoryItem.id,
                                UserInventoryItem.row_version,
                            )
                            .where(
                                UserInventoryItem.id == snapshot["item_id"],
                                UserInventoryItem.user_id == request.user_id,
                                UserInventoryItem.source == "cardtrader",
                                UserInventoryItem.game_id == 1,
                                UserInventoryItem.environment == "real",
                                UserInventoryItem.sync_state == "synced",
                                UserInventoryItem.sync_uncertain_event_id.is_(None),
                                UserInventoryItem.external_stock_id
                                == snapshot["external_stock_id"],
                                UserInventoryItem.reserved_quantity >= snapshot["quantity"],
                            )
                            .with_for_update()
                        )
                    ).one_or_none()
                    if target is None:
                        raise InventoryOperationError(
                            "RESERVATION_RELEASE_FAILED",
                            f"Prenotazione item {snapshot['item_id']} non rilasciabile",
                        )
                    staged_item["target_item_id"] = int(target.id)
                    staged_item["outcome"] = "cardtrader_pending"
                    staged_item["cardtrader_state"] = "pending"
                    staged_update = await session.execute(
                        update(UserInventoryItem)
                        .where(
                            UserInventoryItem.id == target.id,
                            UserInventoryItem.row_version == target.row_version,
                            UserInventoryItem.sync_state == "synced",
                            UserInventoryItem.sync_uncertain_event_id.is_(None),
                            UserInventoryItem.game_id == 1,
                        )
                        .values(
                            sync_state="pending",
                            row_version=UserInventoryItem.row_version + 1,
                            updated_at=func.now(),
                        )
                        .returning(UserInventoryItem.row_version)
                    )
                    staged_row_version = staged_update.scalar_one_or_none()
                    if staged_row_version is None:
                        raise InventoryOperationError(
                            "RESERVATION_RELEASE_FAILED",
                            f"Prenotazione item {snapshot['item_id']} cambiata",
                        )
                    staged_item["row_version"] = int(staged_row_version)
                staged.append(staged_item)
            operation.result_json = _release_result(
                op_key=request.op_key,
                user_id=request.user_id,
                staged=staged,
                status="processing",
                phase="release_local_staged",
            )
    except IntegrityError:
        await session.rollback()
        return await _replay_or_raise(
            session, op_key=request.op_key, kind="release", payload=payload
        )

    ct_items = [item for item in staged if item["source"] == "cardtrader"]
    client_context: Any = None
    try:
        if ct_items:
            if policy is None:
                raise InventoryOperationError(
                    "CARDTRADER_WRITES_BLOCKED",
                    "CardTrader write policy is missing",
                    status_code=409,
                )
            token = await _load_cardtrader_token(session, request.user_id, decrypt_token)
            client_context = client_factory(token, str(request.user_id))
            client = await client_context.__aenter__()
            for item in ct_items:
                if int(item["quantity_after"]) == 0:
                    # A reservation that removes the whole remote quantity makes
                    # CardTrader delete the product.  Use our own stable, unique
                    # marker for the compensating create so recovery can prove
                    # whether the write happened even if the response is lost.
                    item["restore_user_data_field"] = (
                        f"ebartex_inventory:{request.user_id}:{item['item_id']}"
                    )
                    item["recreate_product"] = True
                item["cardtrader_state"] = "applying"
                await _persist_release_progress(
                    session,
                    op_key=request.op_key,
                    user_id=request.user_id,
                    staged=staged,
                    phase="cardtrader_release_applying",
                )
                restored_external_stock_id: Optional[str] = None
                if item.get("recreate_product"):
                    create_payload = build_product_create_payload(
                        item,
                        quantity=int(item["quantity_before"]),
                        user_data_field=str(item["restore_user_data_field"]),
                    )
                    create_result = await _create_cardtrader_for_trade(
                        session,
                        request.user_id,
                        client,
                        create_payload,
                        expected_mode_version=policy.mode_version,
                    )
                    resource = (
                        create_result.get("resource")
                        if isinstance(create_result, dict)
                        else None
                    )
                    if not isinstance(resource, dict):
                        raise CardTraderAPIError(
                            "CardTrader recreate response has no product resource",
                            outcome_unknown=True,
                        )
                    try:
                        new_product_id = int(resource["id"])
                    except (KeyError, TypeError, ValueError) as exc:
                        raise CardTraderAPIError(
                            "CardTrader recreate response has no product id",
                            outcome_unknown=True,
                        ) from exc
                    if (
                        int(resource.get("blueprint_id") or 0) != int(item["blueprint_id"])
                        or resource.get("user_data_field")
                        != item["restore_user_data_field"]
                    ):
                        raise CardTraderAPIError(
                            "CardTrader recreated a different product",
                            outcome_unknown=True,
                        )
                    remote_quantity = _authoritative_remote_quantity(
                        create_result,
                        expected_product_id=new_product_id,
                    )
                    if remote_quantity != int(item["quantity_before"]):
                        raise CardTraderAPIError(
                            "CardTrader recreate quantity does not match the reservation",
                            outcome_unknown=True,
                        )
                    restored_external_stock_id = str(new_product_id)
                else:
                    increment_result = await _increment_cardtrader_for_trade(
                        session,
                        request.user_id,
                        client,
                        int(item["external_stock_id"]),
                        int(item["quantity"]),
                        expected_mode_version=policy.mode_version,
                    )
                    remote_quantity = _authoritative_remote_quantity(
                        increment_result,
                        expected_product_id=int(item["external_stock_id"]),
                    )
                await _apply_released_cardtrader_item(
                    session,
                    op_key=request.op_key,
                    user_id=request.user_id,
                    staged=staged,
                    item=item,
                    remote_quantity=remote_quantity,
                    external_stock_id=restored_external_stock_id,
                )
    except Exception as external_error:
        await _persist_release_progress(
            session,
            op_key=request.op_key,
            user_id=request.user_id,
            staged=staged,
            phase="cardtrader_release_pending_recovery",
        )
        raise InventoryOperationError(
            "CARDTRADER_RELEASE_PENDING",
            "Ripristino CardTrader in verifica automatica",
            status_code=503,
        ) from external_error
    finally:
        if client_context is not None:
            try:
                await client_context.__aexit__(None, None, None)
            except Exception as exc:
                logger.error(
                    "Errore chiudendo il client CardTrader di release (%s)",
                    type(exc).__name__,
                )

    result = _release_result(
        op_key=request.op_key,
        user_id=request.user_id,
        staged=staged,
        status="succeeded",
    )
    async with session.begin():
        stored = (
            await session.execute(
                select(InventoryOperation).where(InventoryOperation.op_key == request.op_key)
            )
        ).scalar_one()
        stored.status = "succeeded"
        stored.result_json = result
        stored.completed_at = _now()
    return result


async def consume_inventory(
    session: AsyncSession,
    request: ReleaseInventoryRequest,
) -> Dict[str, Any]:
    """Finalize a successful reservation without returning it to the seller."""
    payload = _release_payload(request)
    snapshots = await _load_reservation_result(session, request)
    operation = InventoryOperation(
        op_key=request.op_key,
        kind="consume",
        payload_json=payload,
        status="processing",
    )
    consumed: List[Dict[str, Any]] = []
    try:
        async with session.begin():
            session.add(operation)
            await session.flush()
            for snapshot in snapshots:
                finalized = await session.execute(
                    update(UserInventoryItem)
                    .where(
                        UserInventoryItem.id == snapshot["item_id"],
                        UserInventoryItem.user_id == request.user_id,
                        UserInventoryItem.reserved_quantity >= snapshot["quantity"],
                    )
                    .values(
                        reserved_quantity=(
                            UserInventoryItem.reserved_quantity - snapshot["quantity"]
                        ),
                        updated_at=func.now(),
                    )
                    .returning(UserInventoryItem.id)
                )
                target_id = finalized.scalar_one_or_none()
                if target_id is None:
                    raise InventoryOperationError(
                        "RESERVATION_CONSUME_FAILED",
                        f"Prenotazione item {snapshot['item_id']} non finalizzabile",
                    )
                consumed.append(
                    {
                        "original_item_id": snapshot["item_id"],
                        "target_item_id": target_id,
                        "quantity": snapshot["quantity"],
                        "outcome": "receiver_credited",
                    }
                )
            result = {
                "op_key": request.op_key,
                "kind": "consume",
                "status": "succeeded",
                "user_id": str(request.user_id),
                "items": consumed,
                "replayed": False,
            }
            operation.status = "succeeded"
            operation.result_json = result
            operation.completed_at = _now()
        return result
    except IntegrityError:
        await session.rollback()
        return await _replay_or_raise(
            session, op_key=request.op_key, kind="consume", payload=payload
        )
    except InventoryOperationError as error:
        await session.rollback()
        await _record_failed_claim(
            session,
            op_key=request.op_key,
            kind="consume",
            payload=payload,
            error=error,
        )
        raise error


async def credit_inventory(
    session: AsyncSession,
    request: CreditInventoryRequest,
) -> Dict[str, Any]:
    """Credit immutable trade snapshots as new internal inventory rows."""
    payload = _credit_payload(request)
    operation = InventoryOperation(
        op_key=request.op_key,
        kind="credit",
        payload_json=payload,
        status="processing",
    )
    credited: List[Dict[str, Any]] = []
    try:
        async with session.begin():
            session.add(operation)
            await session.flush()
            for snapshot in request.items:
                item = UserInventoryItem(
                    user_id=request.user_id,
                    blueprint_id=snapshot.blueprint_id,
                    quantity=snapshot.quantity,
                    price_cents=snapshot.price_cents,
                    properties=snapshot.properties,
                    external_stock_id=None,
                    source="trade",
                    description=snapshot.description,
                    graded=snapshot.graded,
                )
                session.add(item)
                await session.flush()
                credited.append(
                    {
                        "target_item_id": item.id,
                        "blueprint_id": snapshot.blueprint_id,
                        "quantity": snapshot.quantity,
                        "source": "trade",
                    }
                )
            result = {
                "op_key": request.op_key,
                "kind": "credit",
                "status": "succeeded",
                "user_id": str(request.user_id),
                "items": credited,
                "replayed": False,
            }
            operation.status = "succeeded"
            operation.result_json = result
            operation.completed_at = _now()
        return result
    except IntegrityError as integrity_error:
        await session.rollback()
        existing = await _existing_operation(session, request.op_key)
        if existing is not None:
            return await _replay_or_raise(
                session, op_key=request.op_key, kind="credit", payload=payload
            )
        error = InventoryOperationError(
            "CREDIT_FAILED",
            "Accredito inventario rifiutato dal database",
            status_code=409,
        )
        await _record_failed_claim(
            session,
            op_key=request.op_key,
            kind="credit",
            payload=payload,
            error=error,
        )
        raise error from integrity_error
