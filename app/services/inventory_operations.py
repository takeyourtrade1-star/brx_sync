"""Idempotent inventory mutations used by the trades saga."""

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.internal_schemas import (
    CreditInventoryRequest,
    ReleaseInventoryRequest,
    ReserveInventoryRequest,
)
from app.core.crypto import get_encryption_manager
from app.models.inventory import (
    InventoryOperation,
    UserInventoryItem,
    UserSyncSettings,
)
from app.services.cardtrader_client import CardTraderClient

logger = logging.getLogger(__name__)

ClientFactory = Callable[[str, str], Any]
TokenDecryptor = Callable[[str], str]
TRADABLE_SOURCES = ("cardtrader", "trade")


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
        "blueprint_id": int(row.blueprint_id),
        "price_cents": int(row.price_cents),
        "properties": row.properties,
        "description": row.description,
        "graded": row.graded,
        "source": row.source,
        "external_stock_id": row.external_stock_id,
        "cardtrader_reserved": False,
    }


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
                    )
                    .values(quantity=quantity_value, updated_at=func.now())
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
            session.add(operation)
            await session.flush()
            for requested in sorted(request.items, key=lambda item: item.item_id):
                reserved = await session.execute(
                    update(UserInventoryItem)
                    .where(
                        UserInventoryItem.id == requested.item_id,
                        UserInventoryItem.user_id == request.user_id,
                        UserInventoryItem.source.in_(TRADABLE_SOURCES),
                        UserInventoryItem.quantity >= requested.quantity,
                    )
                    .values(
                        quantity=UserInventoryItem.quantity - requested.quantity,
                        updated_at=func.now(),
                    )
                    .returning(
                        UserInventoryItem.id,
                        UserInventoryItem.quantity,
                        UserInventoryItem.blueprint_id,
                        UserInventoryItem.price_cents,
                        UserInventoryItem.properties,
                        UserInventoryItem.description,
                        UserInventoryItem.graded,
                        UserInventoryItem.source,
                        UserInventoryItem.external_stock_id,
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
            operation.result_json = {"items": snapshots}
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
    availability: Dict[str, int] = {}
    decremented: List[Dict[str, Any]] = []
    compensated: List[int] = []
    compensation_failed: List[int] = []

    try:
        if ct_items:
            token = await _load_cardtrader_token(session, request.user_id, decrypt_token)
            async with client_factory(token, str(request.user_id)) as client:
                try:
                    products = await client.get_products_export()
                    availability = {
                        str(product["id"]): int(product.get("quantity", 0))
                        for product in products
                        if isinstance(product, dict) and product.get("id") is not None
                    }
                    for item in ct_items:
                        external_id = item["external_stock_id"]
                        available = availability.get(external_id, 0)
                        if available < item["quantity"]:
                            raise InventoryOperationError(
                                "CARDTRADER_UNAVAILABLE",
                                f"Stock CardTrader insufficiente per item {item['item_id']}",
                            )
                    for item in ct_items:
                        await client.increment_product_quantity(
                            int(item["external_stock_id"]), -item["quantity"]
                        )
                        item["cardtrader_reserved"] = True
                        decremented.append(item)
                except Exception as external_error:
                    for item in reversed(decremented):
                        try:
                            await client.increment_product_quantity(
                                int(item["external_stock_id"]), item["quantity"]
                            )
                            compensated.append(item["item_id"])
                        except Exception:
                            compensation_failed.append(item["item_id"])
                            logger.exception(
                                "Compensazione CardTrader fallita per inventory item %s",
                                item["item_id"],
                            )
                    if isinstance(external_error, InventoryOperationError):
                        raise external_error
                    raise InventoryOperationError(
                        "CARDTRADER_RESERVATION_FAILED",
                        "Prenotazione CardTrader fallita; batch compensato",
                    ) from external_error
    except InventoryOperationError as operation_error:
        await _compensate_failed_reservation(
            session,
            operation_key=request.op_key,
            user_id=request.user_id,
            snapshots=snapshots,
            availability=availability,
            cardtrader_compensated=compensated,
            cardtrader_compensation_failed=compensation_failed,
            error=operation_error,
        )
        raise operation_error
    except Exception as external_error:
        unexpected_error = InventoryOperationError(
            "CARDTRADER_RESERVATION_FAILED",
            "Prenotazione CardTrader fallita; batch compensato",
        )
        await _compensate_failed_reservation(
            session,
            operation_key=request.op_key,
            user_id=request.user_id,
            snapshots=snapshots,
            availability=availability,
            cardtrader_compensated=compensated,
            cardtrader_compensation_failed=compensation_failed,
            error=unexpected_error,
        )
        raise unexpected_error from external_error

    result = {
        "op_key": request.op_key,
        "kind": "reserve",
        "status": "succeeded",
        "user_id": str(request.user_id),
        "items": snapshots,
        "replayed": False,
    }
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


async def _fallback_released_cardtrader_item(
    session: AsyncSession,
    *,
    user_id: UUID,
    snapshot: Dict[str, Any],
    local_target_id: Optional[int],
) -> int:
    if local_target_id is not None:
        await session.execute(
            update(UserInventoryItem)
            .where(
                UserInventoryItem.id == local_target_id,
                UserInventoryItem.user_id == user_id,
                UserInventoryItem.quantity >= snapshot["quantity"],
            )
            .values(
                quantity=UserInventoryItem.quantity - snapshot["quantity"],
                updated_at=func.now(),
            )
        )
    fallback = await _insert_trade_row(session, user_id=user_id, snapshot=snapshot)
    return fallback.id


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
                        )
                        .values(
                            quantity=UserInventoryItem.quantity + snapshot["quantity"],
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
                    restored = await session.execute(
                        update(UserInventoryItem)
                        .where(
                            UserInventoryItem.id == snapshot["item_id"],
                            UserInventoryItem.user_id == request.user_id,
                            UserInventoryItem.source == "cardtrader",
                            UserInventoryItem.external_stock_id == snapshot["external_stock_id"],
                        )
                        .values(
                            quantity=UserInventoryItem.quantity + snapshot["quantity"],
                            updated_at=func.now(),
                        )
                        .returning(UserInventoryItem.id)
                    )
                    staged_item["target_item_id"] = restored.scalar_one_or_none()
                    staged_item["outcome"] = "cardtrader_pending"
                staged.append(staged_item)
            operation.result_json = {"items": staged}
    except IntegrityError:
        await session.rollback()
        return await _replay_or_raise(
            session, op_key=request.op_key, kind="release", payload=payload
        )

    ct_items = [item for item in staged if item["source"] == "cardtrader"]
    client: Any = None
    client_context: Any = None
    try:
        if ct_items:
            token = await _load_cardtrader_token(session, request.user_id, decrypt_token)
            client_context = client_factory(token, str(request.user_id))
            client = await client_context.__aenter__()
    except Exception:
        client = None

    try:
        for item in ct_items:
            restored_on_cardtrader = False
            if client is not None:
                try:
                    await client.increment_product_quantity(
                        int(item["external_stock_id"]), item["quantity"]
                    )
                    restored_on_cardtrader = True
                except Exception:
                    logger.info(
                        "Prodotto CardTrader %s non ripristinabile: fallback locale trade",
                        item["external_stock_id"],
                    )

            async with session.begin():
                if restored_on_cardtrader:
                    if item["target_item_id"] is None:
                        recreated = UserInventoryItem(
                            user_id=request.user_id,
                            blueprint_id=item["blueprint_id"],
                            quantity=item["quantity"],
                            price_cents=item["price_cents"],
                            properties=item.get("properties"),
                            external_stock_id=item["external_stock_id"],
                            source="cardtrader",
                            description=item.get("description"),
                            graded=item.get("graded"),
                        )
                        session.add(recreated)
                        await session.flush()
                        item["target_item_id"] = recreated.id
                    item["outcome"] = "returned_to_owner"
                    item["cardtrader_restored"] = True
                else:
                    item["target_item_id"] = await _fallback_released_cardtrader_item(
                        session,
                        user_id=request.user_id,
                        snapshot=item,
                        local_target_id=item["target_item_id"],
                    )
                    item["outcome"] = "returned_as_new_row"
                    item["cardtrader_restored"] = False
    finally:
        if client_context is not None:
            try:
                await client_context.__aexit__(None, None, None)
            except Exception:
                logger.exception("Errore chiudendo il client CardTrader di release")

    result = {
        "op_key": request.op_key,
        "kind": "release",
        "status": "succeeded",
        "user_id": str(request.user_id),
        "items": staged,
        "replayed": False,
    }
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
