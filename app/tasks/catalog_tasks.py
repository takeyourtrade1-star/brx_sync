"""Celery dispatcher and workers for durable catalog repair."""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.core.crypto import get_encryption_manager
from app.core.database import get_isolated_db_session
from app.models.catalog import CatalogImportRequest
from app.models.inventory import UserSyncSettings
from app.services.cardtrader_client import CardTraderClient
from app.services.catalog_importer import (
    CardTraderCatalogReader,
    CatalogImportError,
    CatalogImportNeedsReview,
    CatalogImportTransientError,
    CatalogImporter,
    CatalogIndexPublisher,
    ScryfallHttpReader,
    build_catalog_mysql_writer_from_settings,
)
from app.services.catalog_import_queue import (
    CatalogImportLease,
    claim_catalog_import_job,
    claim_catalog_index_outbox,
    complete_catalog_import_job,
    complete_catalog_index_outbox,
    enqueue_catalog_index_outbox,
    fail_catalog_import_job,
    fail_catalog_index_outbox,
    finalize_catalog_mapping,
    get_catalog_import_request,
    get_catalog_import_blueprint_id,
    list_due_catalog_import_job_ids,
    list_due_catalog_index_outbox_ids,
)
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

ImporterFactory = Callable[
    [uuid.UUID, Mapping[str, Any]],
    CatalogImporter | Awaitable[CatalogImporter],
]
_importer_factory: ImporterFactory | None = None
_index_publisher: CatalogIndexPublisher | None = None


@dataclass(frozen=True)
class _ImporterHandle:
    importer: CatalogImporter
    cleanup: Callable[[], Awaitable[None]] | None = None


def register_catalog_importer_factory(factory: ImporterFactory | None) -> None:
    """Register the worker's provider/DB adapter; useful for DI and tests."""

    global _importer_factory
    _importer_factory = factory


def register_catalog_index_publisher(publisher: CatalogIndexPublisher | None) -> None:
    """Register the Search adapter after the service client is configured."""

    global _index_publisher
    _index_publisher = publisher


def _run_async(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)


async def _default_importer(
    user_id: uuid.UUID,
    product: Mapping[str, Any],
    request_mode_version: int,
) -> _ImporterHandle:
    """Build the production adapter without persisting or returning the CT token."""

    environment = product.get("environment")
    if environment not in {"partial", "real"}:
        raise CatalogImportNeedsReview(
            "invalid_request_environment",
            "catalog repair requires an explicit partial or real inventory environment",
        )
    async with get_isolated_db_session() as session:
        settings_row = (
            await session.execute(
                select(UserSyncSettings).where(UserSyncSettings.user_id == user_id)
            )
        ).scalar_one_or_none()
    if settings_row is None:
        raise CatalogImportNeedsReview("sync_settings_missing", "CardTrader credentials are unavailable")
    if (
        str(settings_row.sync_status) != "active"
        or str(settings_row.execution_mode) != environment
        or int(settings_row.mode_version) != int(request_mode_version)
    ):
        raise CatalogImportTransientError(
            "sync_profile_changed",
            "CardTrader sync profile changed after this catalog request was queued",
        )
    try:
        token = get_encryption_manager().decrypt(settings_row.cardtrader_token_encrypted)
    except Exception as exc:  # noqa: BLE001 - encryption backends expose varied errors
        raise CatalogImportNeedsReview("credential_unavailable", "CardTrader credentials cannot be decrypted") from exc

    client = CardTraderClient(token, str(user_id))
    scryfall = ScryfallHttpReader()
    try:
        writer = build_catalog_mysql_writer_from_settings()
    except Exception:
        await client.close()
        await scryfall.close()
        raise
    importer = CatalogImporter(CardTraderCatalogReader(client), scryfall, writer)

    async def cleanup() -> None:
        await client.close()
        await scryfall.close()

    return _ImporterHandle(importer=importer, cleanup=cleanup)


async def _resolve_importer(
    user_id: uuid.UUID,
    product: Mapping[str, Any],
    request_mode_version: int,
) -> _ImporterHandle:
    factory = _importer_factory
    if factory is None:
        return await _default_importer(user_id, product, request_mode_version)
    result = factory(user_id, product)
    if inspect.isawaitable(result):
        result = await result
    return _ImporterHandle(importer=result)


async def _claim_job(job_id: int) -> tuple[CatalogImportLease, CatalogImportRequest] | None:
    async with get_isolated_db_session() as session:
        lease = await claim_catalog_import_job(session, job_id=job_id)
        if lease is None:
            return None
        request = await get_catalog_import_request(session, lease.job_id)
    if request is None:
        async with get_isolated_db_session() as session:
            await fail_catalog_import_job(
                session,
                lease.job_id,
                lease.lease_token,
                code="request_missing",
                message="catalog import request is missing",
                # A profile can move from active to initial_sync/error after
                # the claim transaction.  Leave the durable job retryable and
                # let the active-profile predicate pick it up later.
                retryable=True,
            )
        return None
    return lease, request


async def _fail_job(lease: CatalogImportLease, error: CatalogImportError) -> dict[str, Any]:
    async with get_isolated_db_session() as session:
        changed = await fail_catalog_import_job(
            session,
            lease.job_id,
            lease.lease_token,
            code=error.code,
            message=str(error),
            retryable=error.retryable,
        )
    return {"status": "failed", "job_id": lease.job_id, "updated": changed, "code": error.code}


async def _fail_unexpected(lease: CatalogImportLease, exc: Exception) -> dict[str, Any]:
    async with get_isolated_db_session() as session:
        changed = await fail_catalog_import_job(
            session,
            lease.job_id,
            lease.lease_token,
            code="unexpected_import_error",
            message=type(exc).__name__,
            retryable=True,
        )
    logger.error("Catalog import job %s failed (%s)", lease.job_id, type(exc).__name__)
    return {"status": "failed", "job_id": lease.job_id, "updated": changed}


async def _process_catalog_import_job(job_id: int) -> dict[str, Any]:
    if not get_settings().CATALOG_IMPORT_ENABLED:
        return {"status": "disabled", "job_id": job_id}
    claimed = await _claim_job(job_id)
    if claimed is None:
        return {"status": "not_due", "job_id": job_id}
    lease, request = claimed
    handle: _ImporterHandle | None = None
    try:
        handle = await _resolve_importer(
            request.user_id,
            request.product_json,
            request.mode_version,
        )
        result = await handle.importer.import_blueprint(
            lease.blueprint_id,
            request.product_json,
        )
    except CatalogImportError as exc:
        return await _fail_job(lease, exc)
    except Exception as exc:  # noqa: BLE001 - provider/writer errors vary
        return await _fail_unexpected(lease, exc)
    finally:
        if handle is not None and handle.cleanup is not None:
            try:
                await handle.cleanup()
            except Exception as exc:  # noqa: BLE001 - cleanup must not lose lease evidence
                logger.warning("Catalog importer cleanup failed (%s)", type(exc).__name__)

    try:
        async with get_isolated_db_session() as session:
            await enqueue_catalog_index_outbox(
                session,
                job_id=lease.job_id,
                document_id=result.document["id"],
                document_json=result.document,
            )
            completed = await complete_catalog_import_job(
                session,
                lease.job_id,
                lease.lease_token,
                result_json={
                    "blueprint_id": result.blueprint_id,
                    "scryfall_id": result.scryfall_id,
                    "local_print_id": result.local_print_id,
                    "document_id": result.document["id"],
                },
            )
            if not completed:
                raise CatalogImportNeedsReview(
                    "catalog_lease_lost", "catalog job lease was lost before finalization"
                )
        return {
            "status": "succeeded",
            "job_id": lease.job_id,
            "updated": completed,
        }
    except CatalogImportError as exc:
        return await _fail_job(lease, exc)
    except Exception as exc:  # noqa: BLE001 - outbox DB errors are retryable
        return await _fail_unexpected(lease, exc)


async def _invalidate_blueprint_mapping_cache(blueprint_id: int) -> None:
    """Invalidate the mapper's current cache key only after PG commit."""

    def invalidate() -> None:
        try:
            from app.services.blueprint_mapper import get_blueprint_mapper

            mapper = get_blueprint_mapper()
            mapper.redis.delete(mapper._get_cache_key(blueprint_id))
        except Exception as exc:  # noqa: BLE001 - cache is an optimization
            logger.warning(
                "Blueprint cache invalidation failed for %s (%s)",
                blueprint_id,
                type(exc).__name__,
            )

    await asyncio.to_thread(invalidate)


@celery_app.task(name="app.tasks.catalog_tasks.process_catalog_import_job")
def process_catalog_import_job(job_id: int) -> dict[str, Any]:
    """Claim and run one durable global blueprint job."""

    return _run_async(_process_catalog_import_job(int(job_id)))


async def _dispatch_catalog_imports() -> dict[str, Any]:
    if not get_settings().CATALOG_IMPORT_ENABLED:
        return {"status": "disabled", "queued": 0}
    async with get_isolated_db_session() as session:
        job_ids = await list_due_catalog_import_job_ids(session)
    queued = 0
    failures = 0
    for job_id in job_ids:
        try:
            process_catalog_import_job.apply_async(args=[job_id], queue="catalog-import")
            queued += 1
        except Exception as exc:  # noqa: BLE001 - broker failures leave DB row due
            failures += 1
            logger.error("Catalog import dispatch failed (%s)", type(exc).__name__)
    return {"status": "ok", "found": len(job_ids), "queued": queued, "dispatch_failures": failures}


@celery_app.task(name="app.tasks.catalog_tasks.dispatch_pending_catalog_imports")
def dispatch_pending_catalog_imports() -> dict[str, Any]:
    """Dispatch bounded due rows; duplicate broker messages are lease-safe."""

    return _run_async(_dispatch_catalog_imports())


async def _process_index_outbox(outbox_id: uuid.UUID) -> dict[str, Any]:
    settings = get_settings()
    if not settings.CATALOG_SEARCH_PUBLISH_ENABLED:
        return {"status": "disabled", "outbox_id": str(outbox_id)}
    async with get_isolated_db_session() as session:
        lease = await claim_catalog_index_outbox(session, outbox_id=outbox_id)
    if lease is None:
        return {"status": "not_due", "outbox_id": str(outbox_id)}
    publisher = _index_publisher
    if publisher is None:
        try:
            from app.services.catalog_index_publisher import (
                build_catalog_index_publisher_from_settings,
            )

            publisher = build_catalog_index_publisher_from_settings()
        except CatalogImportError as exc:
            async with get_isolated_db_session() as session:
                changed = await fail_catalog_index_outbox(
                    session,
                    lease.outbox_id,
                    lease.lease_token,
                    message=str(exc),
                )
            return {
                "status": "failed",
                "outbox_id": str(outbox_id),
                "updated": changed,
                "code": exc.code,
            }
        except Exception as exc:  # noqa: BLE001 - configuration failures are durable
            async with get_isolated_db_session() as session:
                changed = await fail_catalog_index_outbox(
                    session,
                    lease.outbox_id,
                    lease.lease_token,
                    message=type(exc).__name__,
                )
            return {"status": "failed", "outbox_id": str(outbox_id), "updated": changed}
    try:
        await publisher.publish(lease.document_json)
    except Exception as exc:  # noqa: BLE001 - publisher errors are retryable
        async with get_isolated_db_session() as session:
            changed = await fail_catalog_index_outbox(
                session,
                lease.outbox_id,
                lease.lease_token,
                message=type(exc).__name__,
            )
        return {"status": "failed", "outbox_id": str(outbox_id), "updated": changed}
    try:
        async with get_isolated_db_session() as session:
            completed = await complete_catalog_index_outbox(
                session, lease.outbox_id, lease.lease_token
            )
            if not completed:
                return {
                    "status": "stale",
                    "outbox_id": str(outbox_id),
                    "updated": False,
                }
            blueprint_id = await get_catalog_import_blueprint_id(session, lease.job_id)
            if blueprint_id is None:
                raise CatalogImportNeedsReview(
                    "catalog_job_missing",
                    "catalog import job disappeared before mapping finalization",
                )
            mapped_rows = await finalize_catalog_mapping(
                session,
                job_id=lease.job_id,
                blueprint_id=blueprint_id,
            )
    except CatalogImportError as exc:
        async with get_isolated_db_session() as session:
            changed = await fail_catalog_index_outbox(
                session,
                lease.outbox_id,
                lease.lease_token,
                message=str(exc),
            )
        return {
            "status": "failed",
            "outbox_id": str(outbox_id),
            "updated": changed,
            "code": exc.code,
        }
    except Exception as exc:  # noqa: BLE001 - finalization DB errors are retryable
        async with get_isolated_db_session() as session:
            changed = await fail_catalog_index_outbox(
                session,
                lease.outbox_id,
                lease.lease_token,
                message=type(exc).__name__,
            )
        return {"status": "failed", "outbox_id": str(outbox_id), "updated": changed}
    await _invalidate_blueprint_mapping_cache(blueprint_id)
    return {
        "status": "succeeded",
        "outbox_id": str(outbox_id),
        "updated": completed,
        "mapped_rows": mapped_rows,
    }


@celery_app.task(name="app.tasks.catalog_tasks.process_catalog_index_outbox")
def process_catalog_index_outbox(outbox_id: str) -> dict[str, Any]:
    return _run_async(_process_index_outbox(uuid.UUID(str(outbox_id))))


async def _dispatch_catalog_index_outbox() -> dict[str, Any]:
    # Beat runs in the main worker, which intentionally has no Meilisearch
    # credentials. Dispatching is therefore gated by catalog ingestion rather
    # than the publisher flag; the isolated catalog worker applies the actual
    # Search-publish gate before claiming an outbox row.
    if not get_settings().CATALOG_IMPORT_ENABLED:
        return {"status": "disabled", "queued": 0}
    async with get_isolated_db_session() as session:
        outbox_ids = await list_due_catalog_index_outbox_ids(session)
    queued = 0
    failures = 0
    for outbox_id in outbox_ids:
        try:
            process_catalog_index_outbox.apply_async(
                args=[str(outbox_id)], queue="catalog-index"
            )
            queued += 1
        except Exception as exc:  # noqa: BLE001 - broker failures leave row due
            failures += 1
            logger.error("Catalog Search dispatch failed (%s)", type(exc).__name__)
    return {
        "status": "ok",
        "found": len(outbox_ids),
        "queued": queued,
        "dispatch_failures": failures,
    }


@celery_app.task(name="app.tasks.catalog_tasks.dispatch_pending_catalog_index_outbox")
def dispatch_pending_catalog_index_outbox() -> dict[str, Any]:
    return _run_async(_dispatch_catalog_index_outbox())
