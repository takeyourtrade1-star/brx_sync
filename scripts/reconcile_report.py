"""
Esegue il reconciler v2 in modalità SOLO REPORT su tutti gli utenti sync
attivi e stampa il report JSON su stdout.

Non scrive nulla su database né su CardTrader (vedi app/services/reconciler.py).
Uso (dentro il container): python scripts/reconcile_report.py
"""
import asyncio
import json

from sqlalchemy import String, cast, select

from app.core.database import get_db_session_context
from app.models.inventory import UserSyncSettings
from app.services.reconciler import reconcile_user_report


def _build_blueprint_mapper():
    """Mapping opzionale: se MySQL/Redis non rispondono il report resta valido."""
    try:
        from app.services.blueprint_mapper import get_blueprint_mapper
        mapper = get_blueprint_mapper()
        return lambda ct_blueprint_id: mapper.map_blueprint_id(ct_blueprint_id)
    except Exception as exc:  # noqa: BLE001 — il mapping è solo informativo
        print(f"# mapper non disponibile ({exc}): salto il check mapping")
        return None


async def main() -> None:
    map_blueprint = _build_blueprint_mapper()
    reports = []
    async with get_db_session_context() as session:
        rows = (
            await session.execute(
                select(UserSyncSettings).where(
                    # la colonna è un enum Postgres: confronto come testo
                    cast(UserSyncSettings.sync_status, String).in_(
                        ["active", "initial_sync"]
                    )
                )
            )
        ).scalars().all()
        print(f"# utenti sync da riconciliare: {len(rows)}")
        for settings_row in rows:
            try:
                report = await reconcile_user_report(
                    session, settings_row, map_blueprint
                )
            except Exception as exc:  # noqa: BLE001 — un utente rotto non blocca gli altri
                report = {
                    "user_id": str(settings_row.user_id),
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc!r}",
                }
            reports.append(report)

    print(json.dumps(reports, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    asyncio.run(main())
