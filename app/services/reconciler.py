"""
Reconciler v2 — Fase 3/4 del piano CardTrader.

Confronta l'export completo CardTrader (/products/export) con le righe locali
collegate (external_stock_id NOT NULL) di un utente.

Due modalità:
- reconcile_user_report: SOLO REPORT, nessuna mutazione su DB.
- reconcile_user_apply: applica il diff al database locale (quantità, prezzi,
  creazioni, esauriti, archiviazioni). NON scrive MAI su CardTrader.

Regole di sicurezza:
- Un export non valido (troncato, id duplicati, quantità negative) viene
  SCARTATO: nessuna mutazione, nessun contatore aggiornato.
- Un articolo sparito dall'export viene azzerato solo alla SECONDA snapshot
  valida consecutiva in cui manca (contatori in Redis). Mai hard delete.
- Gli update usano una condizione ottimistica (WHERE quantity = valore letto):
  se nel frattempo un acquisto ha cambiato la riga, l'update viene saltato e
  ripreso al giro successivo.
- Le righe interne (external_stock_id NULL) non vengono mai toccate.
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import get_encryption_manager
from app.core.redis_client import get_redis_sync
from app.models.inventory import UserInventoryItem, UserSyncSettings
from app.services.cardtrader_client import CardTraderClient

logger = logging.getLogger(__name__)

# Redis: contatori di assenza consecutiva e dimensione ultima snapshot valida
MISSING_KEY = "reconcile_report:missing:{user_id}"
LAST_SNAPSHOT_SIZE_KEY = "reconcile_report:last_snapshot_size:{user_id}"
STATE_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 giorni

# Limite voci dettagliate per categoria nel report (i conteggi restano completi)
DETAIL_LIMIT = 50


def _extract_price_cents(product: Dict[str, Any]) -> Optional[int]:
    """Prezzo in centesimi dall'export CT; None se il campo non è riconoscibile."""
    price_cents = product.get("price_cents")
    if isinstance(price_cents, int):
        return price_cents
    price = product.get("price")
    if isinstance(price, dict) and isinstance(price.get("cents"), int):
        return price["cents"]
    return None


def validate_snapshot(
    products: Any,
    previous_snapshot_size: Optional[int],
    local_active_rows: Optional[int] = None,
) -> Tuple[bool, List[str]]:
    """
    Valida l'export prima di usarlo. Ritorna (ok, problemi).
    Se non ok, la snapshot va SCARTATA: nessun diff, nessun contatore aggiornato.
    """
    problems: List[str] = []
    if not isinstance(products, list):
        return False, ["export non è una lista"]

    ids: List[str] = []
    for product in products:
        if not isinstance(product, dict) or product.get("id") is None:
            problems.append("prodotto senza id nell'export")
            break
        ids.append(str(product["id"]))
        quantity = product.get("quantity", 0)
        if not isinstance(quantity, int) or quantity < 0:
            problems.append(
                f"quantità non valida per prodotto {product['id']}: {quantity!r}"
            )
            break

    if len(ids) != len(set(ids)):
        problems.append("product id duplicati nell'export")

    # Plausibilità vs snapshot precedente: un export molto più piccolo del
    # precedente è probabilmente troncato (timeout/errore parziale).
    if (
        previous_snapshot_size is not None
        and previous_snapshot_size >= 10
        and len(ids) < previous_snapshot_size * 0.5
    ):
        problems.append(
            f"conteggio implausibile: export={len(ids)} vs snapshot precedente={previous_snapshot_size}"
        )

    # Plausibilità vs righe locali attive: a differenza della snapshot
    # precedente (Redis, azzerato a ogni redeploy) questo confronto
    # sopravvive ai riavvii e protegge anche il primo run.
    if (
        local_active_rows is not None
        and local_active_rows >= 10
        and len(ids) < local_active_rows * 0.5
    ):
        problems.append(
            f"conteggio implausibile: export={len(ids)} vs righe locali attive={local_active_rows}"
        )

    return (len(problems) == 0), problems


def diff_inventory(
    products: List[Dict[str, Any]],
    local_items: List[UserInventoryItem],
    missing_counts: Dict[str, int],
    map_blueprint: Optional[Callable[[int], Optional[Tuple[int, str]]]] = None,
) -> Dict[str, Any]:
    """
    Diff puro (nessun side effect) tra export CT e righe locali collegate.

    missing_counts: contatori di assenza consecutiva PRIMA di questa snapshot
    (serve per marcare i candidati archiviati alla seconda assenza).
    """
    ct_by_pid = {str(p["id"]): p for p in products}
    local_by_pid = {item.external_stock_id: item for item in local_items}

    identical = 0
    quantity_diffs: List[Dict[str, Any]] = []
    price_diffs: List[Dict[str, Any]] = []
    sold_out_on_ct: List[Dict[str, Any]] = []
    missing_local: List[Dict[str, Any]] = []
    missing_local_unmapped = 0
    missing_on_ct_active: List[Dict[str, Any]] = []
    missing_on_ct_zero = 0
    archive_candidates: List[Dict[str, Any]] = []

    for pid, product in ct_by_pid.items():
        local = local_by_pid.get(pid)
        ct_quantity = product.get("quantity", 0)
        ct_price = _extract_price_cents(product)

        if local is None:
            # Presente su CT, assente da noi
            entry = {
                "product_id": pid,
                "ct_blueprint_id": product.get("blueprint_id"),
                "name": product.get("name_en") or product.get("name"),
                "quantity": ct_quantity,
            }
            if map_blueprint is not None and product.get("blueprint_id") is not None:
                try:
                    mapped = map_blueprint(product["blueprint_id"])
                except Exception as exc:  # mapping è solo informativo nel report
                    mapped = None
                    entry["mapping_check_error"] = str(exc)
                entry["mapped"] = mapped is not None
                if mapped is None:
                    missing_local_unmapped += 1
            missing_local.append(entry)
            continue

        qty_equal = local.quantity == ct_quantity
        price_equal = ct_price is None or local.price_cents == ct_price

        if ct_quantity == 0 and local.quantity > 0:
            sold_out_on_ct.append({
                "product_id": pid,
                "local_quantity": local.quantity,
            })
        elif not qty_equal:
            quantity_diffs.append({
                "product_id": pid,
                "local_quantity": local.quantity,
                "ct_quantity": ct_quantity,
            })

        if not price_equal:
            price_diffs.append({
                "product_id": pid,
                "local_price_cents": local.price_cents,
                "ct_price_cents": ct_price,
            })

        if qty_equal and price_equal:
            identical += 1

    for pid, local in local_by_pid.items():
        if pid in ct_by_pid:
            continue
        # Da noi ma sparito dall'export CT
        if local.quantity > 0:
            entry = {
                "product_id": pid,
                "local_quantity": local.quantity,
                "consecutive_missing": missing_counts.get(pid, 0) + 1,
            }
            missing_on_ct_active.append(entry)
            if missing_counts.get(pid, 0) + 1 >= 2:
                archive_candidates.append(entry)
        else:
            missing_on_ct_zero += 1

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
        "sold_out_on_ct": {
            "count": len(sold_out_on_ct),
            "items": sold_out_on_ct[:DETAIL_LIMIT],
        },
        "missing_local": {
            "count": len(missing_local),
            "unmapped": missing_local_unmapped,
            "items": missing_local[:DETAIL_LIMIT],
        },
        "missing_on_ct_active": {
            "count": len(missing_on_ct_active),
            "items": missing_on_ct_active[:DETAIL_LIMIT],
        },
        "missing_on_ct_zero_count": missing_on_ct_zero,
        "archive_candidates": {
            "count": len(archive_candidates),
            "items": archive_candidates[:DETAIL_LIMIT],
        },
    }


def _load_missing_counts(redis, user_id: uuid.UUID) -> Dict[str, int]:
    raw = redis.get(MISSING_KEY.format(user_id=user_id))
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return {str(k): int(v) for k, v in data.items()}
    except (ValueError, TypeError):
        logger.warning("Contatori assenza corrotti per %s: reset", user_id)
        return {}


def _save_snapshot_state(
    redis,
    user_id: uuid.UUID,
    ct_pids: set,
    local_pids: set,
    missing_counts: Dict[str, int],
) -> Dict[str, int]:
    """Aggiorna i contatori di assenza SOLO dopo una snapshot valida."""
    new_counts: Dict[str, int] = {}
    for pid in local_pids:
        if pid not in ct_pids:
            new_counts[pid] = missing_counts.get(pid, 0) + 1
        # presente di nuovo → contatore azzerato (semplicemente non salvato)
    redis.set(
        MISSING_KEY.format(user_id=user_id),
        json.dumps(new_counts),
        ex=STATE_TTL_SECONDS,
    )
    redis.set(
        LAST_SNAPSHOT_SIZE_KEY.format(user_id=user_id),
        str(len(ct_pids)),
        ex=STATE_TTL_SECONDS,
    )
    return new_counts


async def _load_local_and_export(
    session: AsyncSession,
    sync_settings: UserSyncSettings,
) -> Tuple[List[UserInventoryItem], int, Any]:
    """Carica righe locali collegate, conteggio righe interne ed export CT."""
    user_id = sync_settings.user_id

    result = await session.execute(
        select(UserInventoryItem).where(
            UserInventoryItem.user_id == user_id,
            UserInventoryItem.external_stock_id.isnot(None),
            UserInventoryItem.external_stock_id != "",
        )
    )
    local_items = list(result.scalars().all())

    internal_rows = (
        await session.execute(
            select(UserInventoryItem.id).where(
                UserInventoryItem.user_id == user_id,
                UserInventoryItem.external_stock_id.is_(None),
            )
        )
    ).scalars().all()

    token = get_encryption_manager().decrypt(sync_settings.cardtrader_token_encrypted)

    # Chiudi la transazione di sola lettura PRIMA dell'HTTP verso CardTrader:
    # l'export può durare 2-3 minuti e la connessione non deve restare
    # in transazione per tutto quel tempo.
    await session.commit()

    async with CardTraderClient(token, str(user_id)) as client:
        products = await client.get_products_export()

    return local_items, len(internal_rows), products


async def reconcile_user_report(
    session: AsyncSession,
    sync_settings: UserSyncSettings,
    map_blueprint: Optional[Callable[[int], Optional[Tuple[int, str]]]] = None,
) -> Dict[str, Any]:
    """
    Report di riconciliazione per un utente. Nessuna scrittura su DB né su
    CardTrader; aggiorna solo lo stato snapshot in Redis se l'export è valido.
    """
    user_id = sync_settings.user_id
    redis = get_redis_sync()

    local_items, internal_count, products = await _load_local_and_export(
        session, sync_settings
    )

    raw_prev = redis.get(LAST_SNAPSHOT_SIZE_KEY.format(user_id=user_id))
    previous_size = int(raw_prev) if raw_prev else None
    local_active_rows = sum(1 for item in local_items if item.quantity > 0)

    ok, problems = validate_snapshot(products, previous_size, local_active_rows)
    if not ok:
        logger.warning("Snapshot RIFIUTATA per %s: %s", user_id, problems)
        return {
            "user_id": str(user_id),
            "status": "rejected",
            "problems": problems,
            "export_size": len(products) if isinstance(products, list) else None,
            "local_linked_rows": len(local_items),
            "local_internal_rows": internal_count,
        }

    missing_counts = _load_missing_counts(redis, user_id)
    diff = diff_inventory(products, local_items, missing_counts, map_blueprint)

    ct_pids = {str(p["id"]) for p in products}
    local_pids = {item.external_stock_id for item in local_items}
    _save_snapshot_state(redis, user_id, ct_pids, local_pids, missing_counts)

    return {
        "user_id": str(user_id),
        "status": "ok",
        "export_size": len(products),
        "previous_snapshot_size": previous_size,
        "local_linked_rows": len(local_items),
        "local_internal_rows": internal_count,
        "diff": diff,
    }


async def reconcile_user_apply(
    session: AsyncSession,
    sync_settings: UserSyncSettings,
    map_blueprint: Optional[Callable[[int], Optional[Tuple[int, str]]]] = None,
) -> Dict[str, Any]:
    """
    Applica la riconciliazione al database locale. NON scrive mai su CardTrader.

    Ordine: export → validazione → mutazioni locali in una transazione breve
    (l'HTTP verso CardTrader è già concluso quando si inizia a scrivere).
    """
    user_id = sync_settings.user_id
    redis = get_redis_sync()

    local_items, internal_count, products = await _load_local_and_export(
        session, sync_settings
    )

    raw_prev = redis.get(LAST_SNAPSHOT_SIZE_KEY.format(user_id=user_id))
    previous_size = int(raw_prev) if raw_prev else None
    local_active_rows = sum(1 for item in local_items if item.quantity > 0)

    ok, problems = validate_snapshot(products, previous_size, local_active_rows)
    if not ok:
        logger.warning(
            "Reconcile apply: snapshot RIFIUTATA per %s, nessuna mutazione: %s",
            user_id,
            problems,
        )
        return {
            "user_id": str(user_id),
            "status": "rejected",
            "problems": problems,
            "export_size": len(products) if isinstance(products, list) else None,
        }

    missing_counts = _load_missing_counts(redis, user_id)
    ct_by_pid = {str(p["id"]): p for p in products}
    local_by_pid = {item.external_stock_id: item for item in local_items}

    applied = {
        "updated": 0,
        "sold_out": 0,
        "created": 0,
        "archived": 0,
        "skipped_concurrent": 0,  # riga cambiata da un acquisto durante il run
        "skipped_unmapped": 0,    # blueprint senza mapping catalogo (o One Piece)
        "skipped_zero_qty": 0,    # nuovo su CT ma già esaurito: non creato
    }

    try:
        # 1) Aggiorna/crea gli articoli presenti nell'export
        for pid, product in ct_by_pid.items():
            local = local_by_pid.get(pid)
            ct_quantity = product.get("quantity", 0)
            ct_price = _extract_price_cents(product)

            if local is None:
                # Già esaurito su CT: creare una riga a quantità 0 è solo rumore
                if ct_quantity <= 0:
                    applied["skipped_zero_qty"] += 1
                    continue
                # Nuovo su CT: crea solo se il blueprint è mappato nel catalogo
                # (stessa regola del bulk sync iniziale; One Piece escluso).
                ct_blueprint_id = product.get("blueprint_id")
                mapping = None
                if map_blueprint is not None and ct_blueprint_id is not None:
                    try:
                        mapping = map_blueprint(ct_blueprint_id)
                    except Exception as exc:
                        logger.warning(
                            "Reconcile: mapping non verificabile per blueprint %s: %s",
                            ct_blueprint_id,
                            exc,
                        )
                if mapping is None or mapping[1] == "op_prints":
                    applied["skipped_unmapped"] += 1
                    continue
                # Insert idempotente: se un webhook ha creato la stessa riga
                # nel frattempo, il conflitto viene ignorato invece di far
                # fallire (e annullare) l'intero giro di riconciliazione.
                result = await session.execute(
                    pg_insert(UserInventoryItem)
                    .values(
                        user_id=user_id,
                        blueprint_id=ct_blueprint_id,
                        quantity=ct_quantity,
                        price_cents=ct_price or 0,
                        properties=product.get("properties_hash", {}),
                        external_stock_id=pid,
                        source="cardtrader",
                    )
                    .on_conflict_do_nothing()
                )
                if result.rowcount == 1:
                    applied["created"] += 1
                else:
                    applied["skipped_concurrent"] += 1
                continue

            # NB: l'UPDATE ORM sincronizza anche l'oggetto in memoria, quindi
            # il valore letto va salvato PRIMA di eseguire l'update.
            quantity_seen = local.quantity
            values: Dict[str, Any] = {}
            if quantity_seen != ct_quantity:
                values["quantity"] = ct_quantity
            if ct_price is not None and local.price_cents != ct_price:
                values["price_cents"] = ct_price
            if not values:
                continue

            # Update ottimistico: applica solo se la quantità è ancora quella
            # letta a inizio run (un acquisto concorrente la può aver cambiata).
            result = await session.execute(
                update(UserInventoryItem)
                .where(
                    UserInventoryItem.id == local.id,
                    UserInventoryItem.quantity == quantity_seen,
                )
                .values(**values)
            )
            if result.rowcount == 0:
                applied["skipped_concurrent"] += 1
            elif ct_quantity == 0 and quantity_seen > 0:
                applied["sold_out"] += 1
            else:
                applied["updated"] += 1

        # 2) Articoli spariti dall'export: azzera solo alla 2ª assenza consecutiva
        for pid, local in local_by_pid.items():
            if pid in ct_by_pid or local.quantity == 0:
                continue
            if missing_counts.get(pid, 0) + 1 >= 2:
                quantity_seen = local.quantity
                result = await session.execute(
                    update(UserInventoryItem)
                    .where(
                        UserInventoryItem.id == local.id,
                        UserInventoryItem.quantity == quantity_seen,
                    )
                    .values(quantity=0)
                )
                if result.rowcount == 0:
                    applied["skipped_concurrent"] += 1
                else:
                    applied["archived"] += 1

        # 3) last_sync_at nella stessa transazione
        await session.execute(
            update(UserSyncSettings)
            .where(UserSyncSettings.user_id == user_id)
            .values(last_sync_at=datetime.now(timezone.utc), last_error=None)
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    # Contatori assenza: solo dopo il commit di una snapshot valida applicata
    ct_pids = set(ct_by_pid.keys())
    local_pids = set(local_by_pid.keys())
    _save_snapshot_state(redis, user_id, ct_pids, local_pids, missing_counts)

    logger.info("Reconcile apply per %s: %s", user_id, applied)
    return {
        "user_id": str(user_id),
        "status": "ok",
        "export_size": len(products),
        "previous_snapshot_size": previous_size,
        "local_linked_rows": len(local_items),
        "local_internal_rows": internal_count,
        "applied": applied,
    }
