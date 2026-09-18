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

from sqlalchemy import and_, case, exists, func, literal, or_, select, union_all, update
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
from app.services.catalog_import_queue import enqueue_catalog_import
from app.services.catalog_mapping_gate import (
    blocked_catalog_blueprints,
    catalog_mapping_allowed_for,
    catalog_mapping_blocked,
)

logger = logging.getLogger(__name__)

MAGIC_GAME_ID = 1
MAGIC_MAPPING_TABLE = "cards_prints"
CATALOG_METADATA_KEY = "_cardtrader_catalog"
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


def _copy_count(products: Iterable[dict[str, Any]]) -> int:
    """Return the authoritative copy total without changing row semantics."""

    return sum(
        int(product.get("quantity", 0))
        for product in products
        if isinstance(product.get("quantity"), int)
        and not isinstance(product.get("quantity"), bool)
        and product.get("quantity", 0) >= 0
    )


def _bounded_catalog_metadata(product: dict[str, Any]) -> dict[str, Any]:
    """Keep only bounded, CT-authenticated fields for unresolved cards.

    The inventory row must remain useful to the UI while a catalog worker is
    filling MySQL.  We intentionally do not copy arbitrary export keys or
    accept a name/image from a caller: every value comes from the validated CT
    product and is bounded before entering JSONB.
    """

    metadata: dict[str, Any] = {
        "provider": "cardtrader",
        "id": str(product["id"]),
        "blueprint_id": int(product["blueprint_id"]),
        "game_id": MAGIC_GAME_ID,
        "environment": str(product.get("environment") or "real"),
        "category_id": product.get("category_id"),
        "quantity": int(product["quantity"]),
        "price_cents": int(product["price_cents"]),
    }
    scalar_keys = (
        "name_en",
        "name",
        "description",
        "user_data_field",
        "graded",
        "scryfall_id",
        "image_url",
        "image",
        "expansion_id",
        "expansion_name",
        "expansion_code",
    )
    for key in scalar_keys:
        value = product.get(key)
        if (isinstance(value, (str, int)) and not isinstance(value, bool)) or (
            isinstance(value, bool) and key == "graded"
        ):
            if isinstance(value, str):
                value = value[:1000]
            metadata[key] = value

    # The real export currently carries expansion={"id": ...}.  Keeping this
    # exact ID lets the catalog worker fetch one expansion instead of scanning
    # every CT blueprint.  Never trust arbitrary nested JSON from the export.
    expansion = product.get("expansion")
    if isinstance(expansion, dict):
        safe_expansion = {
            key: value
            for key in ("id", "name", "code")
            if isinstance((value := expansion.get(key)), (str, int))
        }
        if safe_expansion:
            metadata["expansion"] = safe_expansion

    uploaded_images = product.get("uploaded_images")
    if isinstance(uploaded_images, list):
        safe_images = [
            image[:1000]
            for image in uploaded_images[:8]
            if isinstance(image, str) and image.strip()
        ]
        if safe_images:
            metadata["uploaded_images"] = safe_images

    properties = product.get("properties_hash")
    if isinstance(properties, dict):
        safe_properties: dict[str, Any] = {}
        for key, value in list(properties.items())[:32]:
            if not isinstance(key, str) or len(key) > 64:
                continue
            if value is None or isinstance(value, bool):
                safe_properties[key] = value
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                safe_properties[key] = value
            elif isinstance(value, str):
                safe_properties[key] = value[:256]
        if safe_properties:
            metadata["properties"] = safe_properties
    return metadata


def _properties_for_unmapped_product(product: dict[str, Any]) -> dict[str, Any]:
    """Merge CT condition data with durable metadata for a pending catalog job."""

    properties = product.get("properties_hash")
    merged = dict(properties) if isinstance(properties, dict) else {}
    merged[CATALOG_METADATA_KEY] = _bounded_catalog_metadata(product)
    return merged


async def _enqueue_catalog_import(
    session: AsyncSession,
    user_id: uuid.UUID,
    product: dict[str, Any],
) -> None:
    """Persist one idempotent catalog job in the caller's transaction.

    The queue module is intentionally imported without a fallback.  A missing
    queue is a deployment/configuration error and must surface rather than
    pretending that an unmapped product was scheduled.
    """

    await enqueue_catalog_import(
        session,
        user_id,
        {
            **product,
            "external_stock_id": str(product["id"]),
            "catalog_metadata": _bounded_catalog_metadata(product),
        },
    )


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
    """Return the mapped subset while retaining the old public helper contract.

    New callers use :func:`_classify_magic_products` so an otherwise valid CT
    row is persisted as ``missing``/``unsupported`` instead of disappearing.
    Focused callers and older tests still receive the historical three-tuple.
    """

    accepted, _unmapped, problems, unsupported = _classify_magic_products(
        products,
        map_blueprint,
    )
    return accepted, problems, unsupported


def _classify_magic_products(
    products: list[dict[str, Any]],
    map_blueprint: BlueprintMapper | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], int]:
    """Classify every normalized Magic row without dropping catalog misses.

    ``unsupported`` remains a row count for backward-compatible metrics.  The
    returned unresolved products carry a private ``_mapping_status`` marker;
    callers persist that marker as the inventory mapping status and enqueue a
    shared catalog import by exact blueprint ID.
    """

    if map_blueprint is None:
        return [], [
            {**product, "_mapping_status": "error"} for product in products
        ], ["blueprint mapper non disponibile"], 0

    mapped: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    mapping_problems: list[str] = []
    mapping_cache: dict[int, tuple[int, str] | None] = {}

    for product in products:
        blueprint_id = int(product["blueprint_id"])
        if blueprint_id not in mapping_cache:
            try:
                mapping_cache[blueprint_id] = map_blueprint(blueprint_id)
            except Exception:  # noqa: BLE001 - mapper backends vary
                logger.exception("Blueprint mapping failed for %s", blueprint_id)
                mapping_cache[blueprint_id] = None
                mapping_problems.append(
                    f"mapping fallito per blueprint {blueprint_id}"
                )
        mapping = mapping_cache[blueprint_id]
        if mapping is not None and mapping[1] == MAGIC_MAPPING_TABLE:
            mapped.append(product)
            continue

        unresolved = dict(product)
        unresolved["_mapping_status"] = (
            "unsupported" if mapping is not None else "missing"
        )
        if mapping is not None:
            unresolved["_mapping_table"] = str(mapping[1])
        unmapped.append(unresolved)

    return mapped, unmapped, mapping_problems, len(unmapped)


async def _apply_catalog_mapping_gate(
    session: AsyncSession,
    mapped: list[dict[str, Any]],
    unmapped: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep MySQL discoveries pending until the Search ACK is durable.

    The mapper reads MySQL, while the catalog worker publishes Search through
    PostgreSQL.  A print can therefore exist in MySQL during the interval in
    which its Search outbox is still pending.  Reclassify only those blueprint
    rows that have a durable, non-ACKed catalog job; old mappings with no queue
    row remain fully compatible with the pre-queue inventory.
    """

    blocked = await blocked_catalog_blueprints(
        session,
        (int(product["blueprint_id"]) for product in mapped),
    )
    if not blocked:
        return mapped, unmapped

    allowed: list[dict[str, Any]] = []
    gated: list[dict[str, Any]] = []
    for product in mapped:
        if int(product["blueprint_id"]) not in blocked:
            allowed.append(product)
            continue
        pending = dict(product)
        pending["_mapping_status"] = "missing"
        pending["_mapping_reason"] = "catalog_search_pending"
        gated.append(pending)
    return allowed, [*unmapped, *gated]


def _mapped_inventory_insert_batch_statement(values: list[dict[str, Any]]):
    """Build a mapped INSERT ... SELECT guarded per blueprint row."""

    if not values:
        raise ValueError("mapped inventory insert requires at least one row")
    table = UserInventoryItem.__table__
    columns = list(values[0])
    row_selects = []
    for row in values:
        if list(row) != columns:
            raise ValueError("mapped inventory insert rows must share columns")
        row_selects.append(
            select(
                *(
                    literal(value, type_=table.c[column].type).label(column)
                    for column, value in row.items()
                )
            ).where(
                catalog_mapping_allowed_for(
                    literal(row["blueprint_id"], type_=table.c["blueprint_id"].type)
                )
            )
        )
    value_select = union_all(*row_selects)
    return (
        pg_insert(UserInventoryItem)
        .from_select(columns, value_select, include_defaults=False)
        .on_conflict_do_nothing()
    )


def _mapped_inventory_insert_statement(values: dict[str, Any]):
    """Build one mapped insert, retaining the focused-test helper contract."""

    return _mapped_inventory_insert_batch_statement([values])


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


def _snapshot_metrics(
    normalized: list[dict[str, Any]],
    mapped: list[dict[str, Any]],
    unmapped: list[dict[str, Any]],
    local_items: Iterable[UserInventoryItem] = (),
) -> dict[str, Any]:
    """Build explicit row/copy counters for API, task and audit consumers."""

    local = list(local_items)
    quarantined = [
        item
        for item in local
        if getattr(item, "source", None) == "cardtrader"
        and (
            getattr(item, "game_id", None) is None
            or getattr(item, "lifecycle_status", None)
            in {"stale", "sync_failed", "pending_delete"}
            or getattr(item, "sync_state", None) != "synced"
            or getattr(item, "sync_uncertain_event_id", None) is not None
            or (
                getattr(item, "mapping_status", None) != "mapped"
                and getattr(item, "lifecycle_status", None)
                not in {"active", "sold_out"}
            )
        )
    ]
    metrics = {
        "raw_rows": len(normalized),
        "raw_copies": _copy_count(normalized),
        "imported_rows": len(mapped),
        "imported_copies": _copy_count(mapped),
        "unmapped_rows": len(unmapped),
        "unmapped_copies": _copy_count(unmapped),
        "quarantined_rows": len(quarantined),
        "quarantined_copies": sum(
            max(int(getattr(item, "quantity", 0)), 0) for item in quarantined
        ),
    }
    metrics["incomplete"] = bool(metrics["unmapped_rows"] or metrics["quarantined_rows"])
    return metrics


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
    mapped, unmapped, mapping_problems, unsupported = _classify_magic_products(
        normalized,
        map_blueprint,
    )
    mapped, unmapped = await _apply_catalog_mapping_gate(session, mapped, unmapped)
    metrics = _snapshot_metrics(normalized, mapped, unmapped, local_items)
    previous_size = await _previous_snapshot_size(session, user_id, environment)
    confirmed_shrink = await _is_confirmed_suspicious_shrink(
        session,
        user_id,
        environment,
        len(normalized),
    )
    local_active = sum(
        1
        for item in local_items
        if item.quantity > 0 and getattr(item, "game_id", None) == MAGIC_GAME_ID
    )
    ok, coverage_problems = validate_snapshot(
        normalized,
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
            "magic_export_size": len(normalized),
            "unsupported_rows": unsupported,
            **metrics,
        }
    missing_counts = {
        str(item.external_stock_id): int(item.missing_snapshot_count)
        for item in local_items
        if item.external_stock_id
    }
    return {
        "user_id": str(user_id),
        "status": "ok",
        "magic_export_size": len(normalized),
        "unsupported_rows": unsupported,
        **metrics,
        "local_linked_rows": len(local_items),
        "local_internal_rows": internal_count,
        # The diagnostic diff is against the complete Magic export; mapping
        # status is reported separately so missing catalog rows are visible.
        "diff": diff_inventory(normalized, local_items, missing_counts),
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


async def _current_inventory_item(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    environment: str,
    product: dict[str, Any],
) -> UserInventoryItem | None:
    """Reload one stock row before a race fallback can apply an unresolved state."""

    result = await session.execute(
        select(UserInventoryItem).where(
            UserInventoryItem.user_id == user_id,
            UserInventoryItem.environment == environment,
            UserInventoryItem.source == "cardtrader",
            UserInventoryItem.blueprint_id == int(product["blueprint_id"]),
            UserInventoryItem.external_stock_id == str(product["id"]),
        )
    )
    return result.scalar_one_or_none()


async def _persist_unmapped_product(
    session: AsyncSession,
    *,
    product: dict[str, Any],
    local: UserInventoryItem | None,
    user_id: uuid.UUID,
    environment: str,
    watermark: int,
    snapshot_id: uuid.UUID,
) -> dict[str, int]:
    """Keep a valid CT product visible while its catalog mapping is pending.

    The queue insert and this inventory mutation share the caller's transaction.
    A queue error therefore rolls back both operations and lets the sync retry;
    a concurrent row mutation is rejected by the same CAS/lease predicates used
    by mapped products.
    """

    mapping_status = str(product.get("_mapping_status") or "missing")
    if mapping_status not in {"missing", "unsupported", "error"}:
        mapping_status = "missing"
    await _enqueue_catalog_import(
        session,
        user_id,
        {
            **product,
            "environment": environment,
        },
    )

    pid = str(product["id"])
    quantity = int(product["quantity"])
    now = datetime.now(timezone.utc)
    values = {
        "blueprint_id": int(product["blueprint_id"]),
        "game_id": MAGIC_GAME_ID,
        "quantity": quantity,
        "price_cents": int(product["price_cents"]),
        "properties": _properties_for_unmapped_product(product),
        "external_stock_id": pid,
        "source": "cardtrader",
        "environment": environment,
        "lifecycle_status": "active" if quantity > 0 else "sold_out",
        # The quantity is authoritative even while catalog enrichment is
        # pending.  ``mapping_status`` remains the publication/reservation
        # gate, so a verified unmapped row can be shown without being tradable.
        "sync_state": "synced",
        "sync_uncertain_event_id": None,
        "mapping_status": mapping_status,
        "missing_snapshot_count": 0,
        "last_seen_snapshot_id": snapshot_id,
        "last_external_update_at": now,
        "description": product.get("description"),
        "user_data_field": product.get("user_data_field"),
        "graded": product.get("graded"),
        "updated_at": now,
    }
    if local is None:
        result = await session.execute(
            pg_insert(UserInventoryItem)
            .values(
                user_id=user_id,
                created_at=now,
                **values,
            )
            .on_conflict_do_nothing()
        )
        return {
            "unmapped_created": int(result.rowcount or 0),
            "unmapped_updated": 0,
            "unmapped_skipped_unsafe": int((result.rowcount or 0) == 0),
        }

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
            **values,
            row_version=UserInventoryItem.row_version + 1,
        )
    )
    updated = int(result.rowcount or 0)
    return {
        "unmapped_created": 0,
        "unmapped_updated": updated,
        "unmapped_skipped_unsafe": int(updated == 0),
    }


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
        game_id = getattr(local, "game_id", None)
        mapping = None
        if game_id is None:
            # The mapper is needed only to prove that an old NULL game_id row
            # belongs to Magic before repairing its identity.  A current
            # game_id=1 row is already in the authoritative Magic namespace,
            # even when its catalog blueprint is still unresolved.
            mapping = map_blueprint(local.blueprint_id)
            if mapping is None or mapping[1] != MAGIC_MAPPING_TABLE:
                continue
        elif game_id != MAGIC_GAME_ID:
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
            if game_id is None:
                # A NULL game_id is repaired only after two complete exports
                # confirm the row is absent.  It never becomes active again.
                values.update(
                    {
                        "game_id": MAGIC_GAME_ID,
                        # The identity proof comes from MySQL, but the row is
                        # still held pending if a catalog job has not reached
                        # the Search ACK.  CASE keeps that decision in the
                        # same PostgreSQL UPDATE as the legacy repair.
                        "mapping_status": case(
                            (
                                catalog_mapping_allowed_for(
                                    UserInventoryItem.blueprint_id
                                ),
                                "mapped",
                            ),
                            else_="missing",
                        ),
                    }
                )
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
        game_id = getattr(local, "game_id", None)
        if game_id == MAGIC_GAME_ID:
            # Current Magic rows are reconciled by _apply_missing_products;
            # do not call the mapper and accidentally quarantine a valid card
            # whose catalog import is still pending.
            continue
        mapping = map_blueprint(local.blueprint_id)
        verified_magic = (
            game_id is None
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
    mapped, unmapped, mapping_problems, unsupported = _classify_magic_products(
        normalized,
        map_blueprint,
    )
    mapped, unmapped = await _apply_catalog_mapping_gate(session, mapped, unmapped)
    metrics = _snapshot_metrics(normalized, mapped, unmapped, local_items)
    checksum = _snapshot_id_set_checksum(normalized)
    previous_size = await _previous_snapshot_size(session, user_id, environment)
    confirmed_shrink = await _is_confirmed_suspicious_shrink(
        session,
        user_id,
        environment,
        len(normalized),
    )
    local_active = sum(
        1
        for item in local_items
        if item.quantity > 0 and getattr(item, "game_id", None) == MAGIC_GAME_ID
    )
    ok, coverage_problems = validate_snapshot(
        normalized,
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
            product_count=len(normalized),
            checksum=checksum,
            problems=problems,
        )
        await session.commit()
        return {
            "user_id": str(user_id),
            "status": "rejected",
            "problems": problems,
            "magic_export_size": len(normalized),
            "unsupported_rows": unsupported,
            **metrics,
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
    unresolved_by_pid = {str(product["id"]): product for product in unmapped}
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
        "unmapped_created": 0,
        "unmapped_updated": 0,
        "unmapped_skipped_unsafe": 0,
        "catalog_import_queued": 0,
        **metrics,
    }

    try:
        applied["legacy_non_magic_quarantined"] = await _quarantine_non_magic_legacy(
            session,
            local_items=local_items,
            user_id=user_id,
            environment=environment,
            map_blueprint=map_blueprint,
            watermark=watermark,
            present_ids=set(ct_by_pid) | set(unresolved_by_pid),
        )

        for index, (pid, product) in enumerate(unresolved_by_pid.items(), start=1):
            if index % 500 == 0:
                _assert_mutation_lease(mutation_lease, lost_lease)
            unresolved_result = await _persist_unmapped_product(
                session,
                product=product,
                local=local_by_pid.get(pid),
                user_id=user_id,
                environment=environment,
                watermark=watermark,
                snapshot_id=snapshot_id,
            )
            for metric, value in unresolved_result.items():
                applied[metric] += int(value)
            applied["catalog_import_queued"] += 1

        for index, (pid, product) in enumerate(ct_by_pid.items(), start=1):
            if index % 500 == 0:
                _assert_mutation_lease(mutation_lease, lost_lease)
            local = local_by_pid.get(pid)
            if local is None:
                if product["quantity"] <= 0:
                    applied["skipped_zero_qty"] += 1
                    continue
                result = await session.execute(
                    _mapped_inventory_insert_statement(
                        {
                            "user_id": user_id,
                            "blueprint_id": product["blueprint_id"],
                            "game_id": MAGIC_GAME_ID,
                            "quantity": product["quantity"],
                            "reserved_quantity": 0,
                            "price_cents": product["price_cents"],
                            "properties": product["properties_hash"],
                            "external_stock_id": pid,
                            "source": "cardtrader",
                            "environment": environment,
                            "lifecycle_status": "active",
                            "sync_state": "synced",
                            "sync_uncertain_event_id": None,
                            "mapping_status": "mapped",
                            "missing_snapshot_count": 0,
                            "last_seen_snapshot_id": snapshot_id,
                            "last_external_update_at": datetime.now(timezone.utc),
                            "description": product.get("description"),
                            "user_data_field": product.get("user_data_field"),
                            "graded": product.get("graded"),
                        }
                    )
                )
                if result.rowcount == 1:
                    applied["created"] += 1
                else:
                    # The local snapshot can be stale (the catalog worker or a
                    # concurrent sync may have inserted this stock row after
                    # _load_local_and_export).  Reload the conflicting row and
                    # apply the same guarded update used for rows present in
                    # the initial local read; this also repairs mapping_status
                    # and game_id when quantity/properties are unchanged.
                    current = await _current_inventory_item(
                        session,
                        user_id=user_id,
                        environment=environment,
                        product=product,
                    )
                    if current is not None and not await catalog_mapping_blocked(
                        session, int(product["blueprint_id"])
                    ):
                        update_result = await session.execute(
                            update(UserInventoryItem)
                            .where(
                                UserInventoryItem.id == current.id,
                                UserInventoryItem.user_id == user_id,
                                UserInventoryItem.environment == environment,
                                UserInventoryItem.source == "cardtrader",
                                UserInventoryItem.row_version == current.row_version,
                                UserInventoryItem.reserved_quantity == 0,
                                _eligible_inbound_state(watermark),
                                catalog_mapping_allowed_for(
                                    UserInventoryItem.blueprint_id
                                ),
                            )
                            .values(
                                game_id=MAGIC_GAME_ID,
                                quantity=product["quantity"],
                                price_cents=product["price_cents"],
                                properties=product["properties_hash"],
                                description=product.get("description"),
                                user_data_field=product.get("user_data_field"),
                                graded=product.get("graded"),
                                lifecycle_status=(
                                    "active"
                                    if product["quantity"] > 0
                                    else "sold_out"
                                ),
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
                        if update_result.rowcount == 1:
                            if product["quantity"] == 0 and current.quantity > 0:
                                applied["sold_out"] += 1
                            else:
                                applied["updated"] += 1
                            continue

                    if await catalog_mapping_blocked(
                        session, int(product["blueprint_id"])
                    ):
                        unresolved = dict(product)
                        unresolved["_mapping_status"] = "missing"
                        unresolved["_mapping_reason"] = "catalog_search_pending"
                        unresolved_result = await _persist_unmapped_product(
                            session,
                            product=unresolved,
                            local=current,
                            user_id=user_id,
                            environment=environment,
                            watermark=watermark,
                            snapshot_id=snapshot_id,
                        )
                        for metric, value in unresolved_result.items():
                            applied[metric] += int(value)
                        applied["catalog_import_queued"] += 1
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
                    catalog_mapping_allowed_for(UserInventoryItem.blueprint_id),
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
                if await catalog_mapping_blocked(session, int(product["blueprint_id"])):
                    unresolved = dict(product)
                    unresolved["_mapping_status"] = "missing"
                    unresolved["_mapping_reason"] = "catalog_search_pending"
                    unresolved_result = await _persist_unmapped_product(
                        session,
                        product=unresolved,
                        local=await _current_inventory_item(
                            session,
                            user_id=user_id,
                            environment=environment,
                            product=product,
                        ),
                        user_id=user_id,
                        environment=environment,
                        watermark=watermark,
                        snapshot_id=snapshot_id,
                    )
                    for metric, value in unresolved_result.items():
                        applied[metric] += int(value)
                    applied["catalog_import_queued"] += 1
                else:
                    applied["skipped_unsafe"] += 1
            elif product["quantity"] == 0 and local.quantity > 0:
                applied["sold_out"] += 1
            else:
                applied["updated"] += 1

        missing_result = await _apply_missing_products(
            session,
            local_items=local_by_pid.values(),
            present_ids=set(ct_by_pid) | set(unresolved_by_pid),
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
            product_count=len(normalized),
            checksum=checksum,
            result={
                "watermark": watermark,
                "applied": applied,
                "metrics": metrics,
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
        "magic_export_size": len(normalized),
        **metrics,
        "local_linked_rows": len(local_items),
        "local_internal_rows": internal_count,
        "applied": applied,
    }
