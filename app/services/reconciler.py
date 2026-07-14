"""
Reconciler v2 — Fase 3 del piano CardTrader, modalità SOLO REPORT.

Confronta l'export completo CardTrader (/products/export) con le righe locali
collegate (external_stock_id NOT NULL) e produce un diff SENZA scrivere nulla:
nessuna mutazione su database, nessuna chiamata di scrittura a CardTrader.

Unica scrittura ammessa: lo stato snapshot in Redis (chiavi reconcile_report:*)
per la regola delle due assenze consecutive — un articolo è candidato
"archiviato" solo se manca da DUE export completi consecutivi; un export
scartato non incrementa mai i contatori.

Le righe interne (external_stock_id NULL) non vengono mai considerate.
"""
import json
import logging
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import select
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

    return (len(problems) == 0), problems


def diff_inventory(
    products: List[Dict[str, Any]],
    local_items: List[UserInventoryItem],
    missing_counts: Dict[str, int],
    map_blueprint: Optional[Callable[[int], Optional[int]]] = None,
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


async def reconcile_user_report(
    session: AsyncSession,
    sync_settings: UserSyncSettings,
    map_blueprint: Optional[Callable[[int], Optional[int]]] = None,
) -> Dict[str, Any]:
    """
    Report di riconciliazione per un utente. Nessuna scrittura su DB né su
    CardTrader; aggiorna solo lo stato snapshot in Redis se l'export è valido.
    """
    user_id = sync_settings.user_id
    redis = get_redis_sync()

    # Righe locali: solo quelle collegate a CardTrader
    result = await session.execute(
        select(UserInventoryItem).where(
            UserInventoryItem.user_id == user_id,
            UserInventoryItem.external_stock_id.isnot(None),
            UserInventoryItem.external_stock_id != "",
        )
    )
    local_items = list(result.scalars().all())

    internal_count = (
        await session.execute(
            select(UserInventoryItem.id).where(
                UserInventoryItem.user_id == user_id,
                UserInventoryItem.external_stock_id.is_(None),
            )
        )
    ).scalars().all()

    token = get_encryption_manager().decrypt(sync_settings.cardtrader_token_encrypted)
    async with CardTraderClient(token, str(user_id)) as client:
        products = await client.get_products_export()

    raw_prev = redis.get(LAST_SNAPSHOT_SIZE_KEY.format(user_id=user_id))
    previous_size = int(raw_prev) if raw_prev else None

    ok, problems = validate_snapshot(products, previous_size)
    if not ok:
        logger.warning("Snapshot RIFIUTATA per %s: %s", user_id, problems)
        return {
            "user_id": str(user_id),
            "status": "rejected",
            "problems": problems,
            "export_size": len(products) if isinstance(products, list) else None,
            "local_linked_rows": len(local_items),
            "local_internal_rows": len(internal_count),
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
        "local_internal_rows": len(internal_count),
        "diff": diff,
    }
