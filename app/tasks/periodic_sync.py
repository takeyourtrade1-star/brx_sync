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
import logging
import uuid
from typing import Any, Dict, List

from sqlalchemy import String, cast, select

from app.core.database import get_isolated_db_session
from app.core.redis_client import get_redis_sync
from app.models.inventory import UserSyncSettings
from app.services.inventory_operations import (
    recover_stale_releases,
    recover_stale_reservations,
)
from app.services.reconciler import reconcile_user_apply
from app.tasks.celery_app import celery_app
from app.tasks.sync_tasks import run_async

logger = logging.getLogger(__name__)

LOCK_KEY = "reconcile:lock:{user_id}"
LOCK_TTL_SECONDS = 1800  # 30 minuti: oltre il peggior export CardTrader


def _build_blueprint_mapper():
    """Mapping opzionale: se MySQL/Redis non rispondono si salta solo il create."""
    try:
        from app.services.blueprint_mapper import get_blueprint_mapper
        mapper = get_blueprint_mapper()
        return lambda ct_blueprint_id: mapper.map_blueprint_id(ct_blueprint_id)
    except Exception as exc:  # noqa: BLE001 — il mapping serve solo ai create
        logger.warning("Blueprint mapper non disponibile: %s", exc)
        return None


async def _reconcile_one(session, settings_row, redis, map_blueprint) -> Dict[str, Any]:
    """Riconcilia un utente con lock single-flight. Non solleva mai."""
    user_id = settings_row.user_id
    lock_key = LOCK_KEY.format(user_id=user_id)
    if not redis.set(lock_key, "1", nx=True, ex=LOCK_TTL_SECONDS):
        logger.info("Reconcile %s saltata: già in corso", user_id)
        return {"user_id": str(user_id), "status": "locked"}

    try:
        return await reconcile_user_apply(session, settings_row, map_blueprint)
    except Exception as exc:  # noqa: BLE001 — un utente rotto non blocca gli altri
        logger.error("Reconcile fallita per %s: %s", user_id, exc, exc_info=True)
        return {
            "user_id": str(user_id),
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        redis.delete(lock_key)


@celery_app.task(bind=True, max_retries=2, default_retry_delay=600)
def reconcile_all_users(self) -> Dict[str, Any]:
    """Riconcilia tutti gli utenti sync attivi. Pianificato da Celery beat."""
    try:
        return run_async(_reconcile_all_users_async())
    except Exception as exc:
        logger.error("Riconciliazione periodica fallita: %s", exc, exc_info=True)
        raise self.retry(exc=exc)


async def _reconcile_all_users_async() -> Dict[str, Any]:
    redis = get_redis_sync()
    map_blueprint = _build_blueprint_mapper()
    results: List[Dict[str, Any]] = []

    async with get_isolated_db_session() as session:
        rows = (
            await session.execute(
                select(UserSyncSettings).where(
                    # la colonna è un enum Postgres: confronto come testo
                    cast(UserSyncSettings.sync_status, String) == "active"
                )
            )
        ).scalars().all()

        logger.info("Riconciliazione periodica: %d utenti attivi", len(rows))

        for settings_row in rows:
            results.append(
                await _reconcile_one(session, settings_row, redis, map_blueprint)
            )

    summary = {
        "users": len(results),
        "ok": sum(1 for r in results if r.get("status") == "ok"),
        "rejected": sum(1 for r in results if r.get("status") == "rejected"),
        "errors": sum(1 for r in results if r.get("status") == "error"),
        "results": results,
    }
    logger.info("Riconciliazione periodica conclusa: %s", {
        k: v for k, v in summary.items() if k != "results"
    })
    return summary


@celery_app.task(bind=True, max_retries=2, default_retry_delay=300)
def reconcile_user(self, user_id: str) -> Dict[str, Any]:
    """Riconciliazione manuale di un singolo utente (trigger da API)."""
    try:
        result = run_async(_reconcile_single_user_async(user_id))
        if result.get("status") == "error":
            raise RuntimeError(result.get("error") or "reconciliation failed")
        return result
    except Exception as exc:
        logger.error(
            "Riconciliazione manuale fallita per %s: %s", user_id, exc, exc_info=True
        )
        raise self.retry(exc=exc)


async def _reconcile_single_user_async(user_id: str) -> Dict[str, Any]:
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

        return await _reconcile_one(session, settings_row, redis, map_blueprint)


@celery_app.task(bind=True, max_retries=2, default_retry_delay=300)
def recover_inventory_reservations(self) -> Dict[str, Any]:
    """Resolve stale trade reservations left by timeout/process crashes."""
    try:
        return run_async(_recover_inventory_reservations_async())
    except Exception as exc:
        logger.error(
            "Recupero prenotazioni inventario fallito: %s",
            exc,
            exc_info=True,
        )
        raise self.retry(exc=exc)


async def _recover_inventory_reservations_async() -> Dict[str, Any]:
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
