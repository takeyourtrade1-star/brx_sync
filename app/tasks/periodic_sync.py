"""
Riconciliazione CardTrader → database locale (reconciler v2).

- reconcile_all_users: gira via Celery beat per tutti gli utenti attivi.
- reconcile_user: riconciliazione manuale di un singolo utente (endpoint
  POST /sync/sync-from-cardtrader/{user_id}).

Per ogni utente: scarica l'export CardTrader, lo valida e applica il diff al
database locale (vedi app/services/reconciler.py). Non scrive MAI su CardTrader.

Single-flight per utente tramite lock Redis: se una riconciliazione per lo
stesso utente è già in corso, il giro viene saltato.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import String, and_, cast, select

from app.core.database import get_isolated_db_session
from app.core.redis_client import get_redis_sync
from app.models.inventory import SyncOperation, UserSyncSettings
from app.services.inventory_operations import (
    recover_stale_releases,
    recover_stale_reservations,
)
from app.services.reconciler import reconcile_user_apply
from app.services.webhook_ledger_processor import process_deferred_webhooks
from app.tasks.celery_app import celery_app
from app.tasks.sync_tasks import run_async

logger = logging.getLogger(__name__)

LOCK_KEY = "reconcile:lock:{user_id}"
LOCK_TTL_SECONDS = 1800  # 30 minuti: oltre il peggior export CardTrader
RELEASE_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
TERMINAL_RECONCILE_FAILURE_CODES = {
    "rejected": "snapshot_rejected",
    "deferred": "reconcile_deferred",
    "superseded": "reconcile_superseded",
    "locked": "reconcile_locked",
    "skipped": "reconcile_skipped",
}


def _terminal_reconcile_operation(
    result: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Map domain outcomes to durable operation states without false success."""

    outcome = str(result.get("status") or "")
    if outcome == "ok":
        return "completed", {"result": result}
    if outcome in TERMINAL_RECONCILE_FAILURE_CODES:
        operation_status = "failed" if outcome == "rejected" else "cancelled"
        return operation_status, {
            "result": result,
            "failure_code": TERMINAL_RECONCILE_FAILURE_CODES[outcome],
        }
    raise RuntimeError("unexpected reconciliation outcome")


def _build_blueprint_mapper():
    """Mapping opzionale: se MySQL/Redis non rispondono si salta solo il create."""
    try:
        from app.services.blueprint_mapper import get_blueprint_mapper

        mapper = get_blueprint_mapper()
        return lambda ct_blueprint_id: mapper.map_blueprint_id(ct_blueprint_id)
    except Exception as exc:  # noqa: BLE001 — il mapping serve solo ai create
        logger.warning("Blueprint mapper non disponibile (%s)", type(exc).__name__)
        return None


async def _reconcile_one(session, settings_row, redis, map_blueprint) -> dict[str, Any]:
    """Riconcilia un utente con lock single-flight. Non solleva mai."""
    user_id = settings_row.user_id
    lock_key = LOCK_KEY.format(user_id=user_id)
    lock_owner = uuid.uuid4().hex
    if not redis.set(lock_key, lock_owner, nx=True, ex=LOCK_TTL_SECONDS):
        logger.info("Reconcile %s saltata: già in corso", user_id)
        return {"user_id": str(user_id), "status": "locked"}

    try:
        # Deferred webhooks are durable evidence. Quarantine them before taking
        # the export watermark so this snapshot can safely resolve them.
        await process_deferred_webhooks(user_id)
        return await reconcile_user_apply(session, settings_row, map_blueprint)
    except Exception as exc:
        logger.error(
            "Reconcile fallita per %s (%s)",
            user_id,
            type(exc).__name__,
        )
        return {
            "user_id": str(user_id),
            "status": "error",
            "error": type(exc).__name__,
        }
    finally:
        try:
            redis.eval(RELEASE_LOCK_SCRIPT, 1, lock_key, lock_owner)
        except Exception as exc:
            logger.warning(
                "Impossibile rilasciare in sicurezza il lock reconcile %s (%s)",
                user_id,
                type(exc).__name__,
            )


@celery_app.task(bind=True, max_retries=2, default_retry_delay=600)
def reconcile_all_users(self) -> dict[str, Any]:
    """Fan-out: accoda un task indipendente per ogni utente sync attivo."""
    try:
        task_specs = run_async(_register_active_reconciles_async())
        task_ids: list[str] = []
        dispatch_failures = 0
        for user_id, task_id in task_specs:
            try:
                reconcile_user.apply_async(args=[user_id], task_id=task_id)
                task_ids.append(task_id)
            except Exception as exc:  # noqa: BLE001 - broker failures vary
                dispatch_failures += 1
                run_async(
                    _update_registered_reconcile(
                        task_id,
                        user_id,
                        "failed",
                        {
                            "trigger": "periodic",
                            "failure_code": "dispatch_failed",
                            "error_type": type(exc).__name__,
                        },
                    )
                )
                logger.error(
                    "Riconciliazione periodica non accodata per %s (%s)",
                    user_id,
                    type(exc).__name__,
                )
        result = {
            "users": len(task_specs),
            "queued": len(task_ids),
            "dispatch_failures": dispatch_failures,
            "task_ids": task_ids,
        }
        logger.info(
            "Riconciliazione periodica accodata per %d utenti",
            len(task_specs),
        )
        return result
    except Exception as exc:
        logger.error(
            "Riconciliazione periodica fallita (%s)",
            type(exc).__name__,
        )
        raise self.retry(exc=RuntimeError(type(exc).__name__))


async def _register_active_reconciles_async() -> list[tuple[str, str]]:
    """Register auditable periodic tasks only for CardTrader-reading modes."""

    async with get_isolated_db_session() as session:
        rows = (
            (
                await session.execute(
                    select(UserSyncSettings.user_id).where(
                        and_(
                            # la colonna è un enum Postgres: confronto come testo
                            cast(UserSyncSettings.sync_status, String) == "active",
                            UserSyncSettings.execution_mode.in_(("partial", "real")),
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        task_specs = [(str(user_id), str(uuid.uuid4())) for user_id in rows]
        session.add_all(
            [
                SyncOperation(
                    user_id=uuid.UUID(user_id),
                    operation_id=task_id,
                    operation_type="reconcile",
                    status="pending",
                    operation_metadata={"trigger": "periodic"},
                )
                for user_id, task_id in task_specs
            ]
        )
    return task_specs


async def _update_registered_reconcile(
    task_id: str | None,
    user_id: str,
    status: str,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Update an API or periodic reconcile task when its durable ledger exists."""

    if not task_id:
        return False
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        return False

    async with get_isolated_db_session() as session:
        operation = (
            await session.execute(
                select(SyncOperation)
                .where(
                    SyncOperation.operation_id == task_id,
                    SyncOperation.user_id == user_uuid,
                    SyncOperation.operation_type == "reconcile",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if operation is None:
            return False
        operation.status = status
        operation.operation_metadata = metadata
        operation.completed_at = (
            datetime.now(timezone.utc)
            if status in {"completed", "failed", "cancelled", "uncertain"}
            else None
        )
        return True


@celery_app.task(bind=True, max_retries=2, default_retry_delay=300)
def reconcile_user(self, user_id: str) -> dict[str, Any]:
    """Riconcilia un utente da trigger API o fan-out periodico registrato."""
    task_id = getattr(self.request, "id", None)
    try:
        run_async(
            _update_registered_reconcile(
                task_id,
                user_id,
                "processing",
                {"attempt": int(getattr(self.request, "retries", 0)) + 1},
            )
        )
        result = run_async(_reconcile_single_user_async(user_id))
        outcome = str(result.get("status") or "")
        if outcome == "error":
            raise RuntimeError(result.get("error") or "reconciliation failed")
        terminal_status, metadata = _terminal_reconcile_operation(result)
        if terminal_status != "completed":
            logger.warning(
                "Riconciliazione conclusa senza applicazione per %s (outcome=%s)",
                user_id,
                outcome,
            )
        run_async(
            _update_registered_reconcile(
                task_id,
                user_id,
                terminal_status,
                metadata,
            )
        )
        return result
    except Exception as exc:
        logger.error(
            "Riconciliazione manuale fallita per %s (%s)",
            user_id,
            type(exc).__name__,
        )
        retries = int(getattr(self.request, "retries", 0))
        terminal = retries >= int(self.max_retries or 0)
        run_async(
            _update_registered_reconcile(
                task_id,
                user_id,
                "failed" if terminal else "processing",
                {
                    "failure_code": "reconcile_failed",
                    "error_type": type(exc).__name__,
                    "attempt": retries + 1,
                    "retrying": not terminal,
                },
            )
        )
        raise self.retry(exc=RuntimeError(type(exc).__name__))


async def _reconcile_single_user_async(user_id: str) -> dict[str, Any]:
    redis = get_redis_sync()
    map_blueprint = _build_blueprint_mapper()
    user_uuid = uuid.UUID(user_id)

    async with get_isolated_db_session() as session:
        settings_row = (
            await session.execute(
                select(UserSyncSettings).where(UserSyncSettings.user_id == user_uuid)
            )
        ).scalar_one_or_none()
        if settings_row is None:
            return {"user_id": user_id, "status": "error", "error": "utente non trovato"}

        # Difesa in profondità: la route fa già questo check, ma il task può
        # essere accodato anche direttamente.
        if str(settings_row.sync_status) != "active":
            return {
                "user_id": user_id,
                "status": "skipped",
                "reason": f"sync_status={settings_row.sync_status}",
            }
        if str(settings_row.execution_mode) not in {"partial", "real"}:
            return {
                "user_id": user_id,
                "status": "skipped",
                "reason": f"execution_mode={settings_row.execution_mode}",
            }

        return await _reconcile_one(session, settings_row, redis, map_blueprint)


@celery_app.task(bind=True, max_retries=2, default_retry_delay=300)
def recover_inventory_reservations(self) -> dict[str, Any]:
    """Resolve stale trade reservations left by timeout/process crashes."""
    try:
        return run_async(_recover_inventory_reservations_async())
    except Exception as exc:
        logger.error(
            "Recupero prenotazioni inventario fallito (%s)",
            type(exc).__name__,
        )
        raise self.retry(exc=RuntimeError(type(exc).__name__))


async def _recover_inventory_reservations_async() -> dict[str, Any]:
    async with get_isolated_db_session() as session:
        reservations = await recover_stale_reservations(
            session,
            stale_minutes=5,
            limit=50,
        )
        releases = await recover_stale_releases(
            session,
            stale_minutes=5,
            limit=50,
        )
    result = {"reservations": reservations, "releases": releases}
    logger.info("Recupero prenotazioni inventario concluso: %s", result)
    return result
