"""Transactional queue and lease helpers for catalog repair.

The synchronizer writes a request in the same PostgreSQL transaction as the
inventory observation.  Celery is only a dispatcher: PostgreSQL remains the
source of truth when a broker or worker disappears between two steps.
"""

from __future__ import annotations

import math
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from sqlalchemy import and_, case, cast, exists, func, or_, select, String, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.catalog import (
    CatalogImportJob,
    CatalogImportRequest,
    CatalogImportStatus,
    CatalogIndexOutbox,
    CatalogOutboxStatus,
)
from app.models.inventory import UserSyncSettings

MAGIC_GAME_ID: Final[int] = 1
MAGIC_CATEGORY_ID: Final[int] = 1
_SAFE_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"demo", "partial", "real"})
_MAX_PRODUCT_JSON_BYTES: Final[int] = 32 * 1024
_SENSITIVE_KEY_PARTS: Final[tuple[str, ...]] = (
    "token",
    "secret",
    "password",
    "authorization",
    "cookie",
)
logger = logging.getLogger(__name__)


class CatalogQueueValidationError(ValueError):
    """The observed row cannot identify a safe catalog job."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CatalogImportLease:
    """A worker lease acquired with a compare-and-set update."""

    job_id: int
    provider: str
    game_id: int
    blueprint_id: int
    attempts: int
    lease_token: uuid.UUID
    lease_until: datetime


@dataclass(frozen=True)
class CatalogIndexLease:
    """A worker lease for one Search publication."""

    outbox_id: uuid.UUID
    job_id: int
    document_id: str
    document_json: dict[str, Any]
    attempts: int
    lease_token: uuid.UUID
    lease_until: datetime


def _settings():
    """Resolve settings lazily so unit tests and workers can override config."""

    return get_settings()


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise CatalogQueueValidationError("invalid_" + field, f"{field} must be a positive integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip().isdigit():
        result = int(value.strip())
    else:
        raise CatalogQueueValidationError("invalid_" + field, f"{field} must be a positive integer")
    if result <= 0:
        raise CatalogQueueValidationError("invalid_" + field, f"{field} must be a positive integer")
    return result


def _safe_json(value: Any, *, depth: int = 0) -> Any:
    """Bound JSON copied from a provider payload and drop unsafe keys."""

    if depth > 3:
        return None
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str):
            return value[:2000]
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [item for item in (_safe_json(item, depth=depth + 1) for item in value[:64])]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:64]:
            if not isinstance(key, str) or len(key) > 96:
                continue
            lowered = key.casefold()
            if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
                continue
            safe_item = _safe_json(item, depth=depth + 1)
            if safe_item is not None:
                result[key] = safe_item
        return result
    return None


def _normalise_environment(product: Mapping[str, Any]) -> str:
    value = product.get("environment")
    if value is None and isinstance(product.get("catalog_metadata"), Mapping):
        value = product["catalog_metadata"].get("environment")
    if value is None:
        raise CatalogQueueValidationError(
            "missing_environment", "catalog import requires an explicit inventory environment"
        )
    environment = str(value).strip().lower()
    if environment not in _SAFE_ENVIRONMENTS:
        raise CatalogQueueValidationError(
            "invalid_environment", "inventory environment is not supported"
        )
    if environment == "demo":
        raise CatalogQueueValidationError(
            "unsupported_environment", "demo inventory cannot create a global catalog job"
        )
    return environment


def _catalog_payload(product: Mapping[str, Any], *, blueprint_id: int, game_id: int) -> dict[str, Any]:
    """Copy only metadata the worker needs; never persist a CT credential."""

    allowed = {
        "id",
        "external_stock_id",
        "blueprint_id",
        "game_id",
        "category_id",
        "quantity",
        "price_cents",
        "environment",
        "properties_hash",
        "properties",
        "expansion",
        "expansion_id",
        "name_en",
        "name",
        "description",
        "graded",
        "catalog_metadata",
    }
    copied = {
        key: _safe_json(value)
        for key, value in product.items()
        if key in allowed and _safe_json(value) is not None
    }
    copied["blueprint_id"] = blueprint_id
    copied["game_id"] = game_id
    copied["external_stock_id"] = str(
        product.get("external_stock_id", product.get("id", ""))
    )[:255]
    if not copied["external_stock_id"]:
        raise CatalogQueueValidationError("invalid_product_id", "product id is required")
    return copied


async def _active_profile_mode_version(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    environment: str,
) -> int | None:
    """Return the current active profile version for the request namespace."""

    result = await session.execute(
        select(UserSyncSettings.mode_version).where(
            UserSyncSettings.user_id == user_id,
            cast(UserSyncSettings.sync_status, String).in_(
                ("active", "initial_sync")
            ),
            UserSyncSettings.execution_mode == environment,
        )
    )
    value = result.scalar_one_or_none()
    if value is None:
        return None
    version = int(value)
    return version if version > 0 else None


def _validate_candidate(
    user_id: uuid.UUID,
    product: Mapping[str, Any],
) -> tuple[uuid.UUID, int, int, str, dict[str, Any]]:
    try:
        user_uuid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except (TypeError, ValueError) as exc:
        raise CatalogQueueValidationError("invalid_user_id", "user id is invalid") from exc

    game_id = _positive_int(product.get("game_id"), "game_id")
    if game_id != MAGIC_GAME_ID:
        raise CatalogQueueValidationError("unsupported_game", "only Magic game_id=1 is supported")
    category_id = _positive_int(product.get("category_id"), "category_id")
    if category_id != MAGIC_CATEGORY_ID:
        raise CatalogQueueValidationError(
            "unsupported_category", "only single-card category_id=1 is supported"
        )
    blueprint_id = _positive_int(product.get("blueprint_id"), "blueprint_id")
    environment = _normalise_environment(product)
    payload = _catalog_payload(product, blueprint_id=blueprint_id, game_id=game_id)
    payload["environment"] = environment
    import json

    if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) > _MAX_PRODUCT_JSON_BYTES:
        raise CatalogQueueValidationError("product_too_large", "catalog product metadata is too large")
    return user_uuid, game_id, blueprint_id, environment, payload


def _reviewable_identity(
    user_id: uuid.UUID,
    product: Mapping[str, Any],
    error: CatalogQueueValidationError,
) -> tuple[uuid.UUID, int, int, str, dict[str, Any]] | None:
    """Retain a Magic row with a stable identity when metadata is ambiguous."""

    if error.code not in {"invalid_category_id", "unsupported_category"}:
        return None
    try:
        user_uuid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
        game_id = _positive_int(product.get("game_id"), "game_id")
        if game_id != MAGIC_GAME_ID:
            raise CatalogQueueValidationError(
                "unsupported_game", "only Magic game_id=1 is supported"
            )
        blueprint_id = _positive_int(product.get("blueprint_id"), "blueprint_id")
        environment = _normalise_environment(product)
        payload = _catalog_payload(product, blueprint_id=blueprint_id, game_id=game_id)
        payload["environment"] = environment
        payload["catalog_review_code"] = error.code
        return user_uuid, game_id, blueprint_id, environment, payload
    except CatalogQueueValidationError:
        return None


async def _retain_needs_review(
    session: AsyncSession,
    candidate: tuple[uuid.UUID, int, int, str, dict[str, Any]],
    error: CatalogQueueValidationError,
    *,
    mode_version: int,
) -> None:
    user_uuid, game_id, blueprint_id, environment, payload = candidate
    source = {
        "provider": "cardtrader",
        "game_id": game_id,
        "blueprint_id": blueprint_id,
        "status": CatalogImportStatus.NEEDS_REVIEW.value,
        "reason": error.code,
    }
    job_result = await session.execute(
        pg_insert(CatalogImportJob)
        .values(
            provider="cardtrader",
            game_id=game_id,
            blueprint_id=blueprint_id,
            status=CatalogImportStatus.NEEDS_REVIEW.value,
            source_json=source,
            last_error_code=error.code,
            last_error=str(error)[:1000],
        )
        .on_conflict_do_nothing(
            index_elements=[
                CatalogImportJob.provider,
                CatalogImportJob.game_id,
                CatalogImportJob.blueprint_id,
            ]
        )
        .returning(CatalogImportJob.id)
    )
    job_id = job_result.scalar_one_or_none()
    if job_id is None:
        job_id = (
            await session.execute(
                select(CatalogImportJob.id).where(
                    CatalogImportJob.provider == "cardtrader",
                    CatalogImportJob.game_id == game_id,
                    CatalogImportJob.blueprint_id == blueprint_id,
                )
            )
        ).scalar_one()
    await session.execute(
        pg_insert(CatalogImportRequest)
        .values(
            job_id=job_id,
            user_id=user_uuid,
            game_id=game_id,
            blueprint_id=blueprint_id,
            external_stock_id=payload["external_stock_id"],
            environment=environment,
            mode_version=mode_version,
            product_json=payload,
        )
        .on_conflict_do_update(
            index_elements=[
                CatalogImportRequest.job_id,
                CatalogImportRequest.user_id,
                CatalogImportRequest.external_stock_id,
                CatalogImportRequest.environment,
            ],
            set_={"mode_version": mode_version, "product_json": payload},
        )
    )


async def enqueue_catalog_import(
    session: AsyncSession,
    user_id: uuid.UUID,
    product: Mapping[str, Any],
) -> None:
    """Enqueue a global blueprint job without committing the caller's transaction.

    The caller must commit this session together with the inventory row.  An
    unsupported row is intentionally left to the caller's diagnostics and is
    not converted into a hidden fallback job.
    """

    if not _settings().CATALOG_IMPORT_ENABLED:
        return
    try:
        user_uuid, game_id, blueprint_id, environment, payload = _validate_candidate(user_id, product)
    except CatalogQueueValidationError as exc:
        # The stock observation is still authoritative.  The reconciler keeps
        # its own mapping/quarantine counters and can surface this code.
        review_candidate = _reviewable_identity(user_id, product, exc)
        if review_candidate is not None:
            _, _, _, environment, _ = review_candidate
            mode_version = await _active_profile_mode_version(
                session, user_id=review_candidate[0], environment=environment
            )
            if mode_version is not None:
                await _retain_needs_review(
                    session,
                    review_candidate,
                    exc,
                    mode_version=mode_version,
                )
            return
        logger.warning(
            "Catalog import not queued (%s): %s", exc.code, str(exc)
        )
        return

    mode_version = await _active_profile_mode_version(
        session, user_id=user_uuid, environment=environment
    )
    if mode_version is None:
        logger.warning(
            "Catalog import not queued: no active sync profile for %s",
            environment,
        )
        return

    source = {
        "provider": "cardtrader",
        "game_id": game_id,
        "blueprint_id": blueprint_id,
        "expansion": payload.get("expansion"),
    }
    job_result = await session.execute(
        pg_insert(CatalogImportJob)
        .values(
            provider="cardtrader",
            game_id=game_id,
            blueprint_id=blueprint_id,
            status=CatalogImportStatus.PENDING.value,
            source_json=source,
        )
        .on_conflict_do_update(
            index_elements=[
                CatalogImportJob.provider,
                CatalogImportJob.game_id,
                CatalogImportJob.blueprint_id,
            ],
            # Do not reset ``needs_review`` or ``succeeded`` from a duplicate
            # observation.  Failed jobs remain due according to their lease.
            set_={"source_json": source, "updated_at": func.now()},
        )
        .returning(CatalogImportJob.id)
    )
    job_id = job_result.scalar_one()
    await session.execute(
        pg_insert(CatalogImportRequest)
        .values(
            job_id=job_id,
            user_id=user_uuid,
            game_id=game_id,
            blueprint_id=blueprint_id,
            external_stock_id=payload["external_stock_id"],
            environment=environment,
            mode_version=mode_version,
            product_json=payload,
        )
        .on_conflict_do_update(
            index_elements=[
                CatalogImportRequest.job_id,
                CatalogImportRequest.user_id,
                CatalogImportRequest.external_stock_id,
                CatalogImportRequest.environment,
            ],
            # A profile switch creates a new server-side snapshot for the
            # same observation namespace; never trust a mode_version copied
            # from CardTrader JSON.
            set_={"mode_version": mode_version, "product_json": payload},
        )
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _claim_timestamp(session: AsyncSession, override: datetime | None) -> datetime:
    """Use the database clock for lease timestamps unless a test pins time."""

    if override is not None:
        return override
    return (await session.execute(select(func.now()))).scalar_one()


def _lease_until(now: datetime, lease_seconds: int | None) -> datetime:
    seconds = lease_seconds or _settings().CATALOG_IMPORT_LEASE_SECONDS
    return now + timedelta(seconds=seconds)


def _due_job_predicate(now: datetime | None, max_attempts: int):
    # Queue timestamps use PostgreSQL CURRENT_TIMESTAMP.  Use the same clock
    # for normal dispatch/claim so a worker host drifting from the DB cannot
    # delay a freshly committed row or reclaim a live lease early.
    due_now = now if now is not None else func.now()
    return or_(
        and_(
            CatalogImportJob.attempts < max_attempts,
            CatalogImportJob.next_attempt_at <= due_now,
            CatalogImportJob.status.in_(
                (CatalogImportStatus.PENDING.value, CatalogImportStatus.FAILED.value)
            ),
        ),
        # An expired lease at the attempt ceiling must still be observed and
        # transitioned to needs_review instead of becoming an immortal row.
        and_(
            CatalogImportJob.status == CatalogImportStatus.RUNNING.value,
            CatalogImportJob.lease_until.isnot(None),
            CatalogImportJob.lease_until <= due_now,
        ),
    )


def _active_catalog_request_exists():
    """Correlate work with the user's current active execution profile."""

    return exists(
        select(1)
        .select_from(CatalogImportRequest)
        .join(
            UserSyncSettings,
            UserSyncSettings.user_id == CatalogImportRequest.user_id,
        )
        .where(
            CatalogImportRequest.job_id == CatalogImportJob.id,
            CatalogImportRequest.environment.in_(('partial', 'real')),
            cast(UserSyncSettings.sync_status, String) == 'active',
            UserSyncSettings.execution_mode == CatalogImportRequest.environment,
            UserSyncSettings.mode_version == CatalogImportRequest.mode_version,
        )
    )


async def list_due_catalog_import_job_ids(
    session: AsyncSession,
    *,
    limit: int | None = None,
    now: datetime | None = None,
) -> list[int]:
    """Read a bounded due list; leases are acquired by the worker task."""

    bounded_limit = max(1, min(limit or _settings().CATALOG_IMPORT_BATCH_SIZE, 100))
    result = await session.execute(
        select(CatalogImportJob.id)
        .where(
            _due_job_predicate(now, _settings().CATALOG_IMPORT_MAX_ATTEMPTS),
            _active_catalog_request_exists(),
        )
        .order_by(CatalogImportJob.next_attempt_at, CatalogImportJob.id)
        .limit(bounded_limit)
    )
    return [int(row[0]) for row in result.all()]


async def claim_catalog_import_job(
    session: AsyncSession,
    *,
    job_id: int | None = None,
    lease_seconds: int | None = None,
    now: datetime | None = None,
) -> CatalogImportLease | None:
    """Atomically claim one due job with ``FOR UPDATE SKIP LOCKED``."""

    predicate = _due_job_predicate(now, _settings().CATALOG_IMPORT_MAX_ATTEMPTS)
    statement = select(CatalogImportJob).where(predicate, _active_catalog_request_exists())
    if job_id is not None:
        statement = statement.where(CatalogImportJob.id == job_id)
    row = (await session.execute(statement.with_for_update(skip_locked=True).limit(1))).scalar_one_or_none()
    if row is None:
        return None
    current = await _claim_timestamp(session, now)
    max_attempts = _settings().CATALOG_IMPORT_MAX_ATTEMPTS
    if row.status == CatalogImportStatus.RUNNING.value and int(row.attempts) >= max_attempts:
        row.status = CatalogImportStatus.NEEDS_REVIEW.value
        row.last_error_code = "lease_expired_max_attempts"
        row.last_error = "catalog import lease expired at the retry ceiling"
        row.lease_token = None
        row.lease_until = None
        row.completed_at = current
        row.updated_at = current
        return None
    token = uuid.uuid4()
    until = _lease_until(current, lease_seconds)
    next_attempts = int(row.attempts) + 1
    row.status = CatalogImportStatus.RUNNING.value
    row.attempts = next_attempts
    row.lease_token = token
    row.lease_until = until
    row.started_at = current
    row.updated_at = current
    return CatalogImportLease(
        job_id=int(row.id),
        provider=str(row.provider),
        game_id=int(row.game_id),
        blueprint_id=int(row.blueprint_id),
        attempts=next_attempts,
        lease_token=token,
        lease_until=until,
    )


async def get_catalog_import_request(
    session: AsyncSession,
    job_id: int,
) -> CatalogImportRequest | None:
    result = await session.execute(
        select(CatalogImportRequest)
        .join(
            UserSyncSettings,
            UserSyncSettings.user_id == CatalogImportRequest.user_id,
        )
        .where(
            CatalogImportRequest.job_id == job_id,
            CatalogImportRequest.environment.in_(('partial', 'real')),
            cast(UserSyncSettings.sync_status, String) == 'active',
            UserSyncSettings.execution_mode == CatalogImportRequest.environment,
            UserSyncSettings.mode_version == CatalogImportRequest.mode_version,
        )
        # A current active profile wins over an older disconnected request.
        .order_by(CatalogImportRequest.created_at.desc(), CatalogImportRequest.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_catalog_import_blueprint_id(
    session: AsyncSession,
    job_id: int,
) -> int | None:
    result = await session.execute(
        select(CatalogImportJob.blueprint_id).where(CatalogImportJob.id == job_id)
    )
    value = result.scalar_one_or_none()
    return int(value) if value is not None else None


async def finalize_catalog_mapping(
    session: AsyncSession,
    *,
    job_id: int,
    blueprint_id: int | None = None,
) -> int:
    """Mark only the observed stock rows as mapped after canonical MySQL DML.

    The update deliberately touches ``mapping_status`` alone.  Quantity,
    reservations, lifecycle and row versions remain authoritative inventory
    state and are never restored by catalog repair.
    """

    from app.models.inventory import UserInventoryItem

    from app.models.inventory import UserSyncSettings

    if blueprint_id is None:
        blueprint_id = int(
            (
                await session.execute(
                    select(CatalogImportJob.blueprint_id).where(CatalogImportJob.id == job_id)
                )
            ).scalar_one()
        )
    result = await session.execute(
        update(UserInventoryItem)
        .where(
            UserInventoryItem.blueprint_id == blueprint_id,
            UserInventoryItem.game_id == MAGIC_GAME_ID,
            UserInventoryItem.source == "cardtrader",
            UserInventoryItem.mapping_status.in_(("missing", "error", "unsupported")),
            UserInventoryItem.lifecycle_status.in_(("active", "sold_out")),
            UserInventoryItem.sync_state == "synced",
            UserInventoryItem.sync_uncertain_event_id.is_(None),
            exists(
                select(1)
                .select_from(CatalogImportRequest)
                .join(
                    UserSyncSettings,
                    UserSyncSettings.user_id == CatalogImportRequest.user_id,
                )
                .where(
                    CatalogImportRequest.job_id == job_id,
                    CatalogImportRequest.user_id == UserInventoryItem.user_id,
                    CatalogImportRequest.external_stock_id == UserInventoryItem.external_stock_id,
                    CatalogImportRequest.environment == UserInventoryItem.environment,
                    CatalogImportRequest.mode_version == UserSyncSettings.mode_version,
                    UserSyncSettings.user_id == UserInventoryItem.user_id,
                    cast(UserSyncSettings.sync_status, String) == "active",
                    UserSyncSettings.execution_mode == UserInventoryItem.environment,
                )
            ),
        )
        .values(mapping_status="mapped")
    )
    return int(result.rowcount or 0)


def _lease_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


async def complete_catalog_import_job(
    session: AsyncSession,
    job_id: int,
    lease_token: uuid.UUID | str,
    *,
    result_json: Mapping[str, Any],
) -> bool:
    """Complete only the worker that still owns the lease."""

    result = await session.execute(
        update(CatalogImportJob)
        .where(
            CatalogImportJob.id == job_id,
            CatalogImportJob.status == CatalogImportStatus.RUNNING.value,
            CatalogImportJob.lease_token == _lease_uuid(lease_token),
        )
        .values(
            status=CatalogImportStatus.SUCCEEDED.value,
            result_json=_safe_json(dict(result_json)),
            lease_token=None,
            lease_until=None,
            completed_at=func.now(),
            updated_at=func.now(),
            last_error_code=None,
            last_error=None,
        )
    )
    return bool(result.rowcount)


async def fail_catalog_import_job(
    session: AsyncSession,
    job_id: int,
    lease_token: uuid.UUID | str,
    *,
    code: str,
    message: str,
    retryable: bool = True,
) -> bool:
    """Persist bounded retry state; exhausted/ambiguous rows need review."""

    bounded_code = "".join(ch for ch in str(code) if ch.isalnum() or ch in "_-:")[:96]
    bounded_message = str(message).replace("\n", " ")[:1000]
    # The attempt count is incremented by claim, so this expression is safe
    # under concurrent stale workers guarded by the lease token.
    max_attempts = _settings().CATALOG_IMPORT_MAX_ATTEMPTS
    delay = min(
        _settings().CATALOG_IMPORT_RETRY_BACKOFF_MAX_SECONDS,
        _settings().CATALOG_IMPORT_RETRY_BACKOFF_BASE_SECONDS
        * (2 ** max(0, max_attempts - 1)),
    )
    # ``attempts >= max`` is evaluated by PostgreSQL using the current row.
    status_expression = case(
        (CatalogImportJob.attempts >= max_attempts, CatalogImportStatus.NEEDS_REVIEW.value),
        else_=CatalogImportStatus.FAILED.value if retryable else CatalogImportStatus.NEEDS_REVIEW.value,
    )
    values: dict[str, Any] = {
        "status": status_expression,
        "last_error_code": bounded_code or "catalog_import_failed",
        "last_error": bounded_message,
        "lease_token": None,
        "lease_until": None,
        "updated_at": func.now(),
    }
    if retryable:
        values["next_attempt_at"] = func.now() + timedelta(seconds=delay)
    else:
        values["completed_at"] = func.now()
    result = await session.execute(
        update(CatalogImportJob)
        .where(
            CatalogImportJob.id == job_id,
            CatalogImportJob.status == CatalogImportStatus.RUNNING.value,
            CatalogImportJob.lease_token == _lease_uuid(lease_token),
        )
        .values(**values)
    )
    return bool(result.rowcount)


async def enqueue_catalog_index_outbox(
    session: AsyncSession,
    *,
    job_id: int,
    document_id: str,
    document_json: Mapping[str, Any],
) -> None:
    """Create Search work in the same PostgreSQL transaction as job success."""

    if not document_id or len(document_id) > 255:
        raise CatalogQueueValidationError("invalid_document_id", "Search document id is invalid")
    raw_document = dict(document_json)
    safe_document = _safe_json(raw_document)
    if not isinstance(safe_document, dict):
        raise CatalogQueueValidationError("invalid_document", "Search document is not an object")
    # ``_safe_json`` removes keys containing ``token``.  Search's local
    # prefix-token field is generated by the importer, so preserve only its
    # bounded scalar-list shape explicitly; arbitrary provider payloads still
    # go through the sensitive-key filter above.
    raw_search_tokens = raw_document.get("search_tokens")
    if isinstance(raw_search_tokens, list):
        safe_document["search_tokens"] = [
            token.strip()[:200]
            for token in raw_search_tokens[:256]
            if isinstance(token, str) and token.strip()
        ]
    import json

    if len(json.dumps(safe_document, ensure_ascii=False, separators=(",", ":"))) > _MAX_PRODUCT_JSON_BYTES * 2:
        raise CatalogQueueValidationError("document_too_large", "Search document is too large")
    await session.execute(
        pg_insert(CatalogIndexOutbox)
        .values(job_id=job_id, document_id=document_id, document_json=safe_document)
        .on_conflict_do_update(
            index_elements=[CatalogIndexOutbox.job_id, CatalogIndexOutbox.document_id],
            set_={"document_json": safe_document, "updated_at": func.now()},
        )
    )


def _due_outbox_predicate(now: datetime | None, max_attempts: int):
    # Keep the default path on the DB clock for the same reason as import jobs.
    due_now = now if now is not None else func.now()
    return or_(
        and_(
            CatalogIndexOutbox.attempts < max_attempts,
            CatalogIndexOutbox.next_attempt_at <= due_now,
            CatalogIndexOutbox.status.in_(
                (CatalogOutboxStatus.PENDING.value, CatalogOutboxStatus.FAILED.value)
            ),
        ),
        and_(
            CatalogIndexOutbox.status == CatalogOutboxStatus.RUNNING.value,
            CatalogIndexOutbox.lease_until.isnot(None),
            CatalogIndexOutbox.lease_until <= due_now,
        ),
    )


async def list_due_catalog_index_outbox_ids(
    session: AsyncSession,
    *,
    limit: int | None = None,
    now: datetime | None = None,
) -> list[uuid.UUID]:
    bounded_limit = max(1, min(limit or _settings().CATALOG_INDEX_OUTBOX_BATCH_SIZE, 100))
    result = await session.execute(
        select(CatalogIndexOutbox.id)
        .where(_due_outbox_predicate(now, _settings().CATALOG_IMPORT_MAX_ATTEMPTS))
        .order_by(CatalogIndexOutbox.next_attempt_at, CatalogIndexOutbox.id)
        .limit(bounded_limit)
    )
    return [row[0] for row in result.all()]


async def claim_catalog_index_outbox(
    session: AsyncSession,
    *,
    outbox_id: uuid.UUID | str | None = None,
    lease_seconds: int | None = None,
    now: datetime | None = None,
) -> CatalogIndexLease | None:
    statement = select(CatalogIndexOutbox).where(
        _due_outbox_predicate(now, _settings().CATALOG_IMPORT_MAX_ATTEMPTS)
    )
    if outbox_id is not None:
        statement = statement.where(CatalogIndexOutbox.id == _lease_uuid(outbox_id))
    row = (await session.execute(statement.with_for_update(skip_locked=True).limit(1))).scalar_one_or_none()
    if row is None:
        return None
    current = await _claim_timestamp(session, now)
    max_attempts = _settings().CATALOG_IMPORT_MAX_ATTEMPTS
    if row.status == CatalogOutboxStatus.RUNNING.value and int(row.attempts) >= max_attempts:
        row.status = CatalogOutboxStatus.NEEDS_REVIEW.value
        row.last_error = "catalog index lease expired at the retry ceiling"
        row.lease_token = None
        row.lease_until = None
        row.updated_at = current
        return None
    token = uuid.uuid4()
    until = _lease_until(current, lease_seconds)
    attempts = int(row.attempts) + 1
    row.status = CatalogOutboxStatus.RUNNING.value
    row.attempts = attempts
    row.lease_token = token
    row.lease_until = until
    row.updated_at = current
    return CatalogIndexLease(
        outbox_id=row.id,
        job_id=int(row.job_id),
        document_id=str(row.document_id),
        document_json=dict(row.document_json),
        attempts=attempts,
        lease_token=token,
        lease_until=until,
    )


async def complete_catalog_index_outbox(
    session: AsyncSession,
    outbox_id: uuid.UUID | str,
    lease_token: uuid.UUID | str,
) -> bool:
    result = await session.execute(
        update(CatalogIndexOutbox)
        .where(
            CatalogIndexOutbox.id == _lease_uuid(outbox_id),
            CatalogIndexOutbox.status == CatalogOutboxStatus.RUNNING.value,
            CatalogIndexOutbox.lease_token == _lease_uuid(lease_token),
        )
        .values(
            status=CatalogOutboxStatus.SUCCEEDED.value,
            lease_token=None,
            lease_until=None,
            completed_at=func.now(),
            updated_at=func.now(),
            last_error=None,
        )
    )
    return bool(result.rowcount)


async def fail_catalog_index_outbox(
    session: AsyncSession,
    outbox_id: uuid.UUID | str,
    lease_token: uuid.UUID | str,
    *,
    message: str,
) -> bool:
    bounded_message = str(message).replace("\n", " ")[:1000]
    max_attempts = _settings().CATALOG_IMPORT_MAX_ATTEMPTS
    delay = min(
        _settings().CATALOG_IMPORT_RETRY_BACKOFF_MAX_SECONDS,
        _settings().CATALOG_IMPORT_RETRY_BACKOFF_BASE_SECONDS
        * (2 ** max(0, max_attempts - 1)),
    )
    result = await session.execute(
        update(CatalogIndexOutbox)
        .where(
            CatalogIndexOutbox.id == _lease_uuid(outbox_id),
            CatalogIndexOutbox.status == CatalogOutboxStatus.RUNNING.value,
            CatalogIndexOutbox.lease_token == _lease_uuid(lease_token),
        )
        .values(
            status=case(
                (
                    CatalogIndexOutbox.attempts >= max_attempts,
                    CatalogOutboxStatus.NEEDS_REVIEW.value,
                ),
                else_=CatalogOutboxStatus.FAILED.value,
            ),
            last_error=bounded_message,
            next_attempt_at=func.now() + timedelta(seconds=delay),
            lease_token=None,
            lease_until=None,
            updated_at=func.now(),
        )
    )
    return bool(result.rowcount)
