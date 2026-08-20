"""Authoritative, fail-safe CardTrader inventory reconciliation.

The full CardTrader export is the only inbound authority for quantities.
Webhook workers merely quarantine rows with a durable inbox watermark.  A
validated snapshot may clear that quarantine only when no reservation/outgoing
mutation is active and no newer webhook is pending for the row.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import get_encryption_manager
from app.models.inventory import (
    SyncSnapshot,
    UserInventoryItem,
    UserSyncSettings,
    WebhookInbox,
)
from app.services.cardtrader_client import CardTraderClient
from app.services.cardtrader_mutation_lease import (
    CardTraderMutationBusyError,
    CardTraderMutationLease,
    cardtrader_mutation_lease,
)

logger = logging.getLogger(__name__)

MAGIC_GAME_ID = 1
MAGIC_MAPPING_TABLE = "cards_prints"
DETAIL_LIMIT = 50
MISSING_CONFIRMATIONS_REQUIRED = 2
SUSPICIOUS_DROP_RATIO = 0.10
SUSPICIOUS_DROP_ABSOLUTE = 3
SUSPICIOUS_SHRINK_CONFIRMATIONS_REQUIRED = 3
SUSPICIOUS_SHRINK_COUNT_DRIFT_RATIO = 0.02
SUSPICIOUS_SHRINK_COUNT_DRIFT_ABSOLUTE = 5
SUSPICIOUS_SHRINK_CONFIRMATION_WINDOW = timedelta(hours=48)
UNRESOLVED_INBOX_STATUSES = (
    "received",
    "processing",
    "failed",
    "deferred",
    "reconcile_pending",
)

BlueprintMapper = Callable[[int], Optional[tuple[int, str]]]


async def _refresh_mutation_lease(
    lease: CardTraderMutationLease,
    stopped: asyncio.Event,
    lost: list[BaseException],
) -> None:
    """Keep the shared outbound/inbound lease alive during a long export."""

    while not stopped.is_set():
        try:
            await asyncio.wait_for(stopped.wait(), timeout=30)
        except TimeoutError:
            try:
                lease.refresh()
            except Exception as exc:  # noqa: BLE001 - Redis client errors vary
                lost.append(exc)
                return


def _assert_mutation_lease(
    lease: CardTraderMutationLease,
    lost: list[BaseException],
) -> None:
    if lost:
        raise CardTraderMutationBusyError(
            "CardTrader mutation lease was lost during reconciliation"
        ) from lost[0]
    lease.refresh()


def _extract_price_cents(product: dict[str, Any]) -> int | None:
    price_cents = product.get("price_cents")
    if isinstance(price_cents, int) and not isinstance(price_cents, bool):
        return price_cents
    price = product.get("price")
    if isinstance(price, dict):
        cents = price.get("cents")
        if isinstance(cents, int) and not isinstance(cents, bool):
            return cents
    return None


def normalize_magic_snapshot(products: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate the raw export and return its strict Magic-only projection."""

    if not isinstance(products, list):
        return [], ["export non è una lista"]

    normalized: list[dict[str, Any]] = []
    problems: list[str] = []
    seen_ids: set[str] = set()

    for index, product in enumerate(products):
        if not isinstance(product, dict):
            problems.append(f"prodotto non valido all'indice {index}")
            continue

        game_id = product.get("game_id")
        if not isinstance(game_id, int) or isinstance(game_id, bool):
            problems.append(f"game_id mancante/non valido all'indice {index}")
            continue
        if game_id != MAGIC_GAME_ID:
            # Full exports may contain several games. They are intentionally
            # outside the BRX inventory namespace and never count as coverage.
            continue

        product_id = product.get("id")
        blueprint_id = product.get("blueprint_id")
        quantity = product.get("quantity")
        price_cents = _extract_price_cents(product)

        if product_id is None or not str(product_id).strip():
            problems.append(f"prodotto Magic senza id all'indice {index}")
            continue
        pid = str(product_id).strip()
        if pid in seen_ids:
            problems.append(f"product id duplicato nell'export Magic: {pid}")
            continue
        seen_ids.add(pid)

        if not isinstance(blueprint_id, int) or isinstance(blueprint_id, bool) or blueprint_id <= 0:
            problems.append(f"blueprint_id non valido per prodotto {pid}")
            continue
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
            problems.append(f"quantità non valida per prodotto {pid}: {quantity!r}")
            continue
        if price_cents is None or price_cents < 0:
            problems.append(f"prezzo non valido per prodotto {pid}")
            continue

        normalized_product = dict(product)
        normalized_product.update(
            {
                "id": pid,
                "game_id": MAGIC_GAME_ID,
                "blueprint_id": blueprint_id,
                "quantity": quantity,
                "price_cents": price_cents,
                "properties_hash": (
                    product.get("properties_hash")
                    if isinstance(product.get("properties_hash"), dict)
                    else {}
                ),
            }
        )
        normalized.append(normalized_product)

    return normalized, problems


def _snapshot_checksum(products: Iterable[dict[str, Any]]) -> str:
    """Stable checksum of the authoritative Magic inventory contents."""

    rows = []
    for product in products:
        if not isinstance(product, dict):
            continue
        try:
            game_id = int(product.get("game_id"))
        except (TypeError, ValueError):
            continue
        if game_id != MAGIC_GAME_ID:
            continue
        rows.append(
            {
                "id": str(product.get("id")),
                "blueprint_id": product.get("blueprint_id"),
                "quantity": product.get("quantity"),
                "price_cents": _extract_price_cents(product),
            }
        )
    encoded = json.dumps(
        sorted(rows, key=lambda row: row["id"]),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _snapshot_id_set_checksum(products: Iterable[dict[str, Any]]) -> str:
    ids = sorted(str(product["id"]) for product in products)
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()


def validate_snapshot(
    products: Any,
    previous_snapshot_size: int | None,
    local_active_rows: int | None = None,
    *,
    allow_confirmed_shrink: bool = False,
) -> tuple[bool, list[str]]:
    """Validate shape and set coverage for the strict Magic projection."""

    magic_products, problems = normalize_magic_snapshot(products)
    if problems:
        return False, problems

    baselines = [
        value
        for value in (previous_snapshot_size, local_active_rows)
        if value is not None and value > 0
    ]
    baseline = max(baselines, default=0)
    drop = baseline - len(magic_products)
    suspicious_drop = max(
        SUSPICIOUS_DROP_ABSOLUTE,
        math.ceil(baseline * SUSPICIOUS_DROP_RATIO),
    )
    if baseline > 0 and drop >= suspicious_drop and not allow_confirmed_shrink:
        problems.append(
            "set Magic implausibilmente ridotto: "
            f"export={len(magic_products)} baseline={baseline} drop={drop}"
        )

    return not problems, problems


def _filter_cards_prints(
    products: list[dict[str, Any]],
    map_blueprint: BlueprintMapper | None,
) -> tuple[list[dict[str, Any]], list[str], int]:
    """Require both game_id=1 and an explicit cards_prints mapping."""

    if map_blueprint is None:
        return [], ["blueprint mapper non disponibile"], 0

    accepted: list[dict[str, Any]] = []
    problems: list[str] = []
    unsupported = 0
    mapping_cache: dict[int, tuple[int, str] | None] = {}

    for product in products:
        blueprint_id = int(product["blueprint_id"])
        if blueprint_id not in mapping_cache:
            mapping_cache[blueprint_id] = map_blueprint(blueprint_id)
        mapping = mapping_cache[blueprint_id]
        if mapping is None or mapping[1] != MAGIC_MAPPING_TABLE:
            unsupported += 1
            continue
        accepted.append(product)
    return accepted, problems, unsupported


def diff_inventory(
    products: list[dict[str, Any]],
    local_items: list[UserInventoryItem],
    missing_counts: dict[str, int],
    map_blueprint: BlueprintMapper | None = None,
) -> dict[str, Any]:
    """Pure report diff over already normalized/mapped Magic products."""

    ct_by_pid = {str(product["id"]): product for product in products}
    local_by_pid = {
        str(item.external_stock_id): item for item in local_items if item.external_stock_id
    }
    quantity_diffs: list[dict[str, Any]] = []
    price_diffs: list[dict[str, Any]] = []
    missing_local: list[dict[str, Any]] = []
    missing_on_ct: list[dict[str, Any]] = []
    identical = 0

    for pid, product in ct_by_pid.items():
        local = local_by_pid.get(pid)
        if local is None:
            missing_local.append(
                {
                    "product_id": pid,
                    "ct_blueprint_id": product["blueprint_id"],
                    "quantity": product["quantity"],
                }
            )
            continue
        if local.quantity != product["quantity"]:
            quantity_diffs.append(
                {
                    "product_id": pid,
                    "local_quantity": local.quantity,
                    "ct_quantity": product["quantity"],
                }
            )
        if local.price_cents != product["price_cents"]:
            price_diffs.append(
                {
                    "product_id": pid,
                    "local_price_cents": local.price_cents,
                    "ct_price_cents": product["price_cents"],
                }
            )
        if local.quantity == product["quantity"] and local.price_cents == product["price_cents"]:
            identical += 1

    for pid, local in local_by_pid.items():
        if pid not in ct_by_pid:
            missing_on_ct.append(
                {
                    "product_id": pid,
                    "local_quantity": local.quantity,
                    "consecutive_missing": missing_counts.get(pid, 0) + 1,
                }
            )

    return {
        "identical": identical,
        "quantity_diffs": {
            "count": len(quantity_diffs),
            "items": quantity_diffs[:DETAIL_LIMIT],
        },
        "price_diffs": {
            "count": len(price_diffs),
            "items": price_diffs[:DETAIL_LIMIT],
        },
        "missing_local": {
            "count": len(missing_local),
            "items": missing_local[:DETAIL_LIMIT],
        },
        "missing_on_ct_active": {
            "count": len(missing_on_ct),
            "items": missing_on_ct[:DETAIL_LIMIT],
        },
        "archive_candidates": {
            "count": sum(
                1
                for item in missing_on_ct
                if item["consecutive_missing"] >= MISSING_CONFIRMATIONS_REQUIRED
            ),
            "items": [
                item
                for item in missing_on_ct
                if item["consecutive_missing"] >= MISSING_CONFIRMATIONS_REQUIRED
            ][:DETAIL_LIMIT],
        },
    }


async def _load_local_and_export(
    session: AsyncSession,
    sync_settings: UserSyncSettings,
) -> tuple[list[UserInventoryItem], int, Any, int]:
    user_id = sync_settings.user_id
    environment = str(sync_settings.execution_mode)
    local_items = list(
        (
            await session.execute(
                select(UserInventoryItem).where(
                    UserInventoryItem.user_id == user_id,
                    UserInventoryItem.environment == environment,
                    UserInventoryItem.source == "cardtrader",
                    UserInventoryItem.external_stock_id.isnot(None),
                    UserInventoryItem.external_stock_id != "",
                )
            )
        )
        .scalars()
        .all()
    )
    internal_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(UserInventoryItem)
                .where(
                    UserInventoryItem.user_id == user_id,
                    UserInventoryItem.environment == environment,
                    UserInventoryItem.external_stock_id.is_(None),
                )
            )
        ).scalar_one()
    )
    watermark = int(
        (
            await session.execute(
                select(func.coalesce(func.max(WebhookInbox.id), 0)).where(
                    WebhookInbox.user_id == user_id
                )
            )
        ).scalar_one()
    )
    token = get_encryption_manager().decrypt(sync_settings.cardtrader_token_encrypted)
    await session.commit()
    async with CardTraderClient(token, str(user_id)) as client:
        products = await client.get_products_export()
    return local_items, internal_count, products, watermark


def _unpack_loaded(
    loaded: tuple,
) -> tuple[list[UserInventoryItem], int, Any, int]:
    # Compatibility for focused tests and external report scripts that mocked
    # the pre-watermark helper.
    if len(loaded) == 3:
        local_items, internal_count, products = loaded
        return local_items, internal_count, products, 0
    local_items, internal_count, products, watermark = loaded
    return local_items, internal_count, products, int(watermark)


async def _previous_snapshot_size(
    session: AsyncSession,
    user_id: uuid.UUID,
    environment: str,
) -> int | None:
    return (
        await session.execute(
            select(SyncSnapshot.product_count)
            .where(
                SyncSnapshot.user_id == user_id,
                SyncSnapshot.environment == environment,
                SyncSnapshot.status == "applied",
            )
            .order_by(SyncSnapshot.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _latest_unresolved_inbox_id(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> int:
    return int(
        (
            await session.execute(
                select(func.coalesce(func.max(WebhookInbox.id), 0)).where(
                    WebhookInbox.user_id == user_id,
                    WebhookInbox.status.in_(UNRESOLVED_INBOX_STATUSES),
                )
            )
        ).scalar_one()
    )


def _is_suspicious_shrink_problem(problems: Any) -> bool:
    return bool(
        isinstance(problems, list)
        and any(
            isinstance(problem, str) and problem.startswith("set Magic implausibilmente ridotto:")
            for problem in problems
        )
    )


def _has_stable_shrink_confirmation(
    prior_snapshots: Iterable[tuple[str, int | None, Any, datetime]],
    current_count: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Confirm a large shrink only after recent, consecutive, stable exports.

    CardTrader inventories are live, so requiring an identical ID-set checksum
    can deadlock forever while a seller keeps making small changes.  We retain
    the fail-closed first observations, but accept the shrink only after three
    consecutive exports (current plus two persisted rejections) agree within a
    narrow count band.
    """

    rows = list(prior_snapshots)
    required_prior = SUSPICIOUS_SHRINK_CONFIRMATIONS_REQUIRED - 1
    if len(rows) != required_prior or current_count <= 0:
        return False

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    cutoff = current_time - SUSPICIOUS_SHRINK_CONFIRMATION_WINDOW
    counts = [current_count]

    for status, product_count, problems, created_at in rows:
        if status != "rejected" or not _is_suspicious_shrink_problem(problems):
            return False
        if not isinstance(product_count, int) or product_count <= 0:
            return False
        if not isinstance(created_at, datetime):
            return False
        created = created_at
        if created.tzinfo is None or created.utcoffset() is None:
            created = created.replace(tzinfo=timezone.utc)
        if created < cutoff or created > current_time + timedelta(minutes=5):
            return False
        counts.append(product_count)

    allowed_drift = max(
        SUSPICIOUS_SHRINK_COUNT_DRIFT_ABSOLUTE,
        math.ceil(max(counts) * SUSPICIOUS_SHRINK_COUNT_DRIFT_RATIO),
    )
    return max(counts) - min(counts) <= allowed_drift


async def _is_confirmed_suspicious_shrink(
    session: AsyncSession,
    user_id: uuid.UUID,
    environment: str,
    product_count: int,
) -> bool:
    prior = list(
        (
            await session.execute(
                select(
                    SyncSnapshot.status,
                    SyncSnapshot.product_count,
                    SyncSnapshot.problems_json,
                    SyncSnapshot.created_at,
                )
                .where(
                    SyncSnapshot.user_id == user_id,
                    SyncSnapshot.environment == environment,
                )
                .order_by(SyncSnapshot.created_at.desc())
                .limit(SUSPICIOUS_SHRINK_CONFIRMATIONS_REQUIRED - 1)
            )
        ).all()
    )
    return _has_stable_shrink_confirmation(
        prior,
        product_count,
    )


async def _record_snapshot(
    session: AsyncSession,
    *,
    snapshot_id: uuid.UUID,
    user_id: uuid.UUID,
    environment: str,
    status: str,
    product_count: int,
    checksum: str,
    problems: list[str] | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    session.add(
        SyncSnapshot(
            id=snapshot_id,
            user_id=user_id,
            environment=environment,
            status=status,
            product_count=product_count,
            checksum=checksum,
            problems_json=problems,
            result_json=result,
            completed_at=datetime.now(timezone.utc),
        )
    )


async def reconcile_user_report(
    session: AsyncSession,
    sync_settings: UserSyncSettings,
    map_blueprint: BlueprintMapper | None = None,
) -> dict[str, Any]:
    user_id = sync_settings.user_id
    environment = str(sync_settings.execution_mode)
    local_items, internal_count, raw_products, _watermark = _unpack_loaded(
        await _load_local_and_export(session, sync_settings)
    )
    normalized, shape_problems = normalize_magic_snapshot(raw_products)
    mapped, mapping_problems, unsupported = _filter_cards_prints(normalized, map_blueprint)
    previous_size = await _previous_snapshot_size(session, user_id, environment)
    confirmed_shrink = await _is_confirmed_suspicious_shrink(
        session,
        user_id,
        environment,
        len(mapped),
    )
    local_active = sum(
        1
        for item in local_items
        if item.quantity > 0 and getattr(item, "game_id", None) == MAGIC_GAME_ID
    )
    ok, coverage_problems = validate_snapshot(
        mapped,
        previous_size,
        local_active,
        allow_confirmed_shrink=confirmed_shrink,
    )
    problems = shape_problems + mapping_problems + coverage_problems
    if not ok or problems:
        return {
            "user_id": str(user_id),
            "status": "rejected",
            "problems": problems,
            "magic_export_size": len(mapped),
            "unsupported_rows": unsupported,
        }
    missing_counts = {
        str(item.external_stock_id): int(item.missing_snapshot_count)
        for item in local_items
        if item.external_stock_id
    }
    return {
        "user_id": str(user_id),
        "status": "ok",
        "magic_export_size": len(mapped),
        "unsupported_rows": unsupported,
        "local_linked_rows": len(local_items),
        "local_internal_rows": internal_count,
        "diff": diff_inventory(mapped, local_items, missing_counts),
    }


def _eligible_inbound_state(snapshot_watermark: int):
    return or_(
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


async def _apply_missing_products(
    session: AsyncSession,
    *,
    local_items: Iterable[UserInventoryItem],
    present_ids: set[str],
    map_blueprint: BlueprintMapper,
    user_id: uuid.UUID,
    environment: str,
    watermark: int,
    progress_check: Callable[[], None] | None = None,
) -> dict[str, int]:
    """Quarantine then zero CardTrader rows absent from complete exports.

    This is shared by initial imports and recurring reconciliation so a re-link
    cannot leave stale local rows behind. Reserved or concurrently mutated rows
    remain untouched through the same row-version and state predicates used by
    the regular reconciler.
    """

    result_counts = {
        "missing_quarantined": 0,
        "archived": 0,
        "skipped_unsafe": 0,
    }
    for index, local in enumerate(local_items, start=1):
        if index % 500 == 0:
            if progress_check is not None:
                progress_check()
            await session.flush()
        pid = str(local.external_stock_id) if local.external_stock_id else ""
        if not pid or pid in present_ids:
            continue
        mapping = map_blueprint(local.blueprint_id)
        if (
            getattr(local, "game_id", None) != MAGIC_GAME_ID
            or mapping is None
            or mapping[1] != MAGIC_MAPPING_TABLE
        ):
            continue

        next_missing = int(local.missing_snapshot_count) + 1
        if next_missing >= MISSING_CONFIRMATIONS_REQUIRED:
            values = {
                "quantity": 0,
                "lifecycle_status": "sold_out",
                "sync_state": "synced",
                "sync_uncertain_event_id": None,
                "missing_snapshot_count": next_missing,
                "row_version": UserInventoryItem.row_version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        else:
            values = {
                "lifecycle_status": "stale",
                "sync_state": "uncertain",
                "sync_uncertain_event_id": max(watermark, 0),
                "missing_snapshot_count": next_missing,
                "row_version": UserInventoryItem.row_version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        update_result = await session.execute(
            update(UserInventoryItem)
            .where(
                UserInventoryItem.id == local.id,
                UserInventoryItem.user_id == user_id,
                UserInventoryItem.environment == environment,
                UserInventoryItem.source == "cardtrader",
                UserInventoryItem.row_version == local.row_version,
                UserInventoryItem.reserved_quantity == 0,
                _eligible_inbound_state(watermark),
            )
            .values(**values)
        )
        if update_result.rowcount == 0:
            result_counts["skipped_unsafe"] += 1
        elif next_missing >= MISSING_CONFIRMATIONS_REQUIRED:
            result_counts["archived"] += 1
        else:
            result_counts["missing_quarantined"] += 1

    return result_counts


async def _quarantine_non_magic_legacy(
    session: AsyncSession,
    *,
    local_items: list[UserInventoryItem],
    user_id: uuid.UUID,
    environment: str,
    map_blueprint: BlueprintMapper,
    watermark: int,
    present_ids: set[str],
) -> int:
    quarantined = 0
    for local in local_items:
        if local.external_stock_id and str(local.external_stock_id) in present_ids:
            # A present row is verified/backfilled by the snapshot update below
            # under its original row-version CAS.
            continue
        mapping = map_blueprint(local.blueprint_id)
        verified_magic = (
            getattr(local, "game_id", None) == MAGIC_GAME_ID
            and mapping is not None
            and mapping[1] == MAGIC_MAPPING_TABLE
        )
        if verified_magic:
            continue
        result = await session.execute(
            update(UserInventoryItem)
            .where(
                UserInventoryItem.id == local.id,
                UserInventoryItem.user_id == user_id,
                UserInventoryItem.environment == environment,
                UserInventoryItem.source == "cardtrader",
                UserInventoryItem.row_version == local.row_version,
                UserInventoryItem.reserved_quantity == 0,
                _eligible_inbound_state(watermark),
            )
            .values(
                sync_state="uncertain",
                sync_uncertain_event_id=max(watermark, 0),
                mapping_status="unsupported",
                lifecycle_status="stale",
                row_version=UserInventoryItem.row_version + 1,
                updated_at=datetime.now(timezone.utc),
            )
        )
        quarantined += int(result.rowcount or 0)
    return quarantined


async def reconcile_user_apply(
    session: AsyncSession,
    sync_settings: UserSyncSettings,
    map_blueprint: BlueprintMapper | None = None,
) -> dict[str, Any]:
    """Apply one snapshot while excluding every outbound CT mutation."""

    async with cardtrader_mutation_lease(sync_settings.user_id) as lease:
        stopped = asyncio.Event()
        lost: list[BaseException] = []
        heartbeat = asyncio.create_task(_refresh_mutation_lease(lease, stopped, lost))
        try:
            return await _reconcile_user_apply_locked(
                session,
                sync_settings,
                map_blueprint,
                mutation_lease=lease,
                lost_lease=lost,
            )
        finally:
            stopped.set()
            await heartbeat


async def _reconcile_user_apply_locked(
    session: AsyncSession,
    sync_settings: UserSyncSettings,
    map_blueprint: BlueprintMapper | None = None,
    *,
    mutation_lease: CardTraderMutationLease,
    lost_lease: list[BaseException],
) -> dict[str, Any]:
    user_id = sync_settings.user_id
    environment = str(sync_settings.execution_mode)
    snapshot_id = uuid.uuid4()
    local_items, internal_count, raw_products, watermark = _unpack_loaded(
        await _load_local_and_export(session, sync_settings)
    )
    _assert_mutation_lease(mutation_lease, lost_lease)

    normalized, shape_problems = normalize_magic_snapshot(raw_products)
    mapped, mapping_problems, unsupported = _filter_cards_prints(normalized, map_blueprint)
    checksum = _snapshot_id_set_checksum(mapped)
    previous_size = await _previous_snapshot_size(session, user_id, environment)
    confirmed_shrink = await _is_confirmed_suspicious_shrink(
        session,
        user_id,
        environment,
        len(mapped),
    )
    local_active = sum(
        1
        for item in local_items
        if item.quantity > 0 and getattr(item, "game_id", None) == MAGIC_GAME_ID
    )
    ok, coverage_problems = validate_snapshot(
        mapped,
        previous_size,
        local_active,
        allow_confirmed_shrink=confirmed_shrink,
    )
    problems = shape_problems + mapping_problems + coverage_problems
    if not ok or problems:
        await session.execute(
            update(UserSyncSettings)
            .where(
                UserSyncSettings.user_id == user_id,
                UserSyncSettings.execution_mode == environment,
                UserSyncSettings.sync_status == "active",
            )
            .values(
                last_error="snapshot_rejected",
                updated_at=datetime.now(timezone.utc),
            )
        )
        await _record_snapshot(
            session,
            snapshot_id=snapshot_id,
            user_id=user_id,
            environment=environment,
            status="rejected",
            product_count=len(mapped),
            checksum=checksum,
            problems=problems,
        )
        await session.commit()
        return {
            "user_id": str(user_id),
            "status": "rejected",
            "problems": problems,
            "magic_export_size": len(mapped),
            "unsupported_rows": unsupported,
        }

    if map_blueprint is None:  # guarded by _filter_cards_prints, narrows typing
        raise RuntimeError("Strict Magic mapping unavailable")

    settings_now = (
        await session.execute(
            select(UserSyncSettings).where(UserSyncSettings.user_id == user_id).with_for_update()
        )
    ).scalar_one()
    if str(settings_now.sync_status) != "active" or str(settings_now.execution_mode) != environment:
        await session.rollback()
        return {
            "user_id": str(user_id),
            "status": "deferred",
            "reason": (
                f"sync_status={settings_now.sync_status},"
                f"execution_mode={settings_now.execution_mode}"
            ),
        }
    latest_unresolved = await _latest_unresolved_inbox_id(session, user_id)
    if latest_unresolved > watermark:
        await session.rollback()
        return {
            "user_id": str(user_id),
            "status": "superseded",
            "snapshot_watermark": watermark,
            "newer_inbox_id": latest_unresolved,
            "reason": "webhook_observed_after_export_started",
        }

    ct_by_pid = {str(product["id"]): product for product in mapped}
    local_by_pid = {
        str(item.external_stock_id): item for item in local_items if item.external_stock_id
    }
    applied = {
        "updated": 0,
        "created": 0,
        "sold_out": 0,
        "missing_quarantined": 0,
        "archived": 0,
        "skipped_unsafe": 0,
        "skipped_zero_qty": 0,
        "unsupported_export_rows": unsupported,
        "legacy_non_magic_quarantined": 0,
    }

    try:
        applied["legacy_non_magic_quarantined"] = await _quarantine_non_magic_legacy(
            session,
            local_items=local_items,
            user_id=user_id,
            environment=environment,
            map_blueprint=map_blueprint,
            watermark=watermark,
            present_ids=set(ct_by_pid),
        )

        for index, (pid, product) in enumerate(ct_by_pid.items(), start=1):
            if index % 500 == 0:
                _assert_mutation_lease(mutation_lease, lost_lease)
            local = local_by_pid.get(pid)
            if local is None:
                if product["quantity"] <= 0:
                    applied["skipped_zero_qty"] += 1
                    continue
                result = await session.execute(
                    pg_insert(UserInventoryItem)
                    .values(
                        user_id=user_id,
                        blueprint_id=product["blueprint_id"],
                        game_id=MAGIC_GAME_ID,
                        quantity=product["quantity"],
                        reserved_quantity=0,
                        price_cents=product["price_cents"],
                        properties=product["properties_hash"],
                        external_stock_id=pid,
                        source="cardtrader",
                        environment=environment,
                        lifecycle_status="active",
                        sync_state="synced",
                        sync_uncertain_event_id=None,
                        mapping_status="mapped",
                        missing_snapshot_count=0,
                        last_seen_snapshot_id=snapshot_id,
                        last_external_update_at=datetime.now(timezone.utc),
                        description=product.get("description"),
                        user_data_field=product.get("user_data_field"),
                        graded=product.get("graded"),
                    )
                    .on_conflict_do_nothing()
                )
                if result.rowcount == 1:
                    applied["created"] += 1
                else:
                    applied["skipped_unsafe"] += 1
                continue

            result = await session.execute(
                update(UserInventoryItem)
                .where(
                    UserInventoryItem.id == local.id,
                    UserInventoryItem.user_id == user_id,
                    UserInventoryItem.environment == environment,
                    UserInventoryItem.source == "cardtrader",
                    UserInventoryItem.row_version == local.row_version,
                    UserInventoryItem.reserved_quantity == 0,
                    _eligible_inbound_state(watermark),
                )
                .values(
                    game_id=MAGIC_GAME_ID,
                    quantity=product["quantity"],
                    price_cents=product["price_cents"],
                    properties=product["properties_hash"],
                    description=product.get("description"),
                    user_data_field=product.get("user_data_field"),
                    graded=product.get("graded"),
                    lifecycle_status=("active" if product["quantity"] > 0 else "sold_out"),
                    sync_state="synced",
                    sync_uncertain_event_id=None,
                    mapping_status="mapped",
                    missing_snapshot_count=0,
                    last_seen_snapshot_id=snapshot_id,
                    last_external_update_at=datetime.now(timezone.utc),
                    row_version=UserInventoryItem.row_version + 1,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            if result.rowcount == 0:
                applied["skipped_unsafe"] += 1
            elif product["quantity"] == 0 and local.quantity > 0:
                applied["sold_out"] += 1
            else:
                applied["updated"] += 1

        missing_result = await _apply_missing_products(
            session,
            local_items=local_by_pid.values(),
            present_ids=set(ct_by_pid),
            map_blueprint=map_blueprint,
            user_id=user_id,
            environment=environment,
            watermark=watermark,
            progress_check=lambda: _assert_mutation_lease(mutation_lease, lost_lease),
        )
        for metric, value in missing_result.items():
            applied[metric] += value
        _assert_mutation_lease(mutation_lease, lost_lease)

        settings_now.last_sync_at = datetime.now(timezone.utc)
        settings_now.last_error = None
        unresolved_marker = exists(
            select(1).where(
                UserInventoryItem.user_id == user_id,
                UserInventoryItem.environment == environment,
                UserInventoryItem.source == "cardtrader",
                UserInventoryItem.sync_uncertain_event_id == WebhookInbox.id,
            )
        )
        await session.execute(
            update(WebhookInbox)
            .where(
                WebhookInbox.user_id == user_id,
                WebhookInbox.id <= watermark,
                WebhookInbox.status == "reconcile_pending",
                ~unresolved_marker,
            )
            .values(
                status="completed",
                processed_at=datetime.now(timezone.utc),
                last_error=None,
            )
        )
        await _record_snapshot(
            session,
            snapshot_id=snapshot_id,
            user_id=user_id,
            environment=environment,
            status="applied",
            product_count=len(mapped),
            checksum=checksum,
            result={
                "watermark": watermark,
                "applied": applied,
                "strict_game_id": MAGIC_GAME_ID,
                "mapping_table": MAGIC_MAPPING_TABLE,
            },
        )

        from app.services.marketplace_projection import (
            project_inventory_to_marketplace,
        )

        await project_inventory_to_marketplace(session, user_id, environment)
        latest_unresolved = await _latest_unresolved_inbox_id(session, user_id)
        if latest_unresolved > watermark:
            await session.rollback()
            return {
                "user_id": str(user_id),
                "status": "superseded",
                "snapshot_watermark": watermark,
                "newer_inbox_id": latest_unresolved,
                "reason": "webhook_observed_before_snapshot_commit",
            }
        _assert_mutation_lease(mutation_lease, lost_lease)
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    return {
        "user_id": str(user_id),
        "status": "ok",
        "snapshot_id": str(snapshot_id),
        "snapshot_watermark": watermark,
        "magic_export_size": len(mapped),
        "local_linked_rows": len(local_items),
        "local_internal_rows": internal_count,
        "applied": applied,
    }
