"""
Webhook processor for CardTrader order notifications.

Handles bidirectional synchronization:
- When orders are created/updated/cancelled on CardTrader
- Updates local inventory quantities accordingly
- Prevents infinite sync loops
"""
import hashlib
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_isolated_db_session
from app.models.inventory import SyncOperation, UserInventoryItem, UserSyncSettings
from app.services.cardtrader_client import CardTraderClient
from app.core.crypto import get_encryption_manager

logger = logging.getLogger(__name__)


def _normalized_order_items(raw_items: Any) -> list[dict[str, Any]]:
    """Project untrusted webhook rows to the fields used for stock arithmetic."""
    if not isinstance(raw_items, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        product_id = item.get("product_id")
        try:
            quantity = int(item.get("quantity", 0))
        except (TypeError, ValueError):
            continue
        if product_id is None or quantity <= 0:
            continue
        normalized.append(
            {"product_id": str(product_id), "quantity": quantity}
        )
    return normalized


async def _claim_inventory_adjustment(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    operation_id: str,
    metadata: dict[str, Any],
) -> int | None:
    return (
        await session.execute(
            pg_insert(SyncOperation)
            .values(
                user_id=user_id,
                operation_id=operation_id,
                operation_type="webhook_inventory_adjustment",
                status="pending",
                operation_metadata=metadata,
            )
            .on_conflict_do_nothing(index_elements=[SyncOperation.operation_id])
            .returning(SyncOperation.id)
        )
    ).scalar_one_or_none()


async def _complete_inventory_adjustment(
    session: AsyncSession,
    operation_pk: int,
    metadata: dict[str, Any],
) -> None:
    operation = await session.get(SyncOperation, operation_pk)
    if operation is None:
        raise RuntimeError("Inventory adjustment ledger row disappeared")
    operation.status = "completed"
    operation.completed_at = datetime.utcnow()
    operation.operation_metadata = metadata
    await session.flush()


def _order_lock_key(user_id: uuid.UUID, order_id: Any) -> int:
    """Stable signed bigint for PostgreSQL's transaction advisory lock."""
    digest = hashlib.sha256(
        f"{user_id}:{order_id}".encode("utf-8")
    ).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


async def _lock_order(
    session: AsyncSession,
    user_id: uuid.UUID,
    order_id: Any,
) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _order_lock_key(user_id, order_id)},
    )


class WebhookProcessor:
    """Processes CardTrader webhook notifications."""
    
    def __init__(self):
        self.crypto_manager = get_encryption_manager()
    
    async def process_order_webhook(
        self,
        webhook_id: str,
        payload: Dict[str, Any],
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Process order webhook from CardTrader.
        
        Handles:
        - order.create: Decrement quantities when order is paid
        - order.update: Handle state changes (cancellation, etc.)
        - order.destroy: Restore quantities when order is deleted
        
        Args:
            webhook_id: Webhook UUID
            payload: Webhook payload with order data
            user_id: Optional user UUID (from URL path, otherwise extracted from payload)
            
        Returns:
            Processing result
        """
        cause = payload.get("cause", "")
        data = payload.get("data", {})
        mode = payload.get("mode", "live")
        if not isinstance(data, dict):
            data = {}

        # If user_id not provided, try to extract from payload
        if not user_id:
            if isinstance(data, dict):
                seller = data.get("seller", {})
                if isinstance(seller, dict) and seller.get("id"):
                    user_id = str(seller.get("id"))

        logger.info(
            f"Processing webhook {webhook_id}: cause={cause}, mode={mode}, user_id={user_id}"
        )

        # Webhook di test di CardTrader: non devono mai toccare l'inventario reale.
        if str(mode).lower() == "test":
            logger.info(f"Webhook {webhook_id} mode=test: ignorato (nessuna modifica inventario reale)")
            return {"status": "ignored", "webhook_id": webhook_id, "reason": "test mode"}

        try:
            user_uuid = uuid.UUID(user_id or "")
        except ValueError:
            return {
                "status": "error",
                "webhook_id": webhook_id,
                "message": "Invalid or missing user_id",
            }
        if not webhook_id or webhook_id == "unknown":
            raise ValueError("Webhook id missing")

        # The idempotency record and every local inventory mutation share one
        # PostgreSQL transaction. A worker crash rolls both back; a concurrent
        # retry loses ON CONFLICT and cannot apply the delta twice.
        operation_id = f"webhook:{user_uuid}:{webhook_id}"[:255]
        async with get_isolated_db_session() as session:
            claimed = (
                await session.execute(
                    pg_insert(SyncOperation)
                    .values(
                        user_id=user_uuid,
                        operation_id=operation_id,
                        operation_type="webhook",
                        status="pending",
                        operation_metadata={"cause": cause, "webhook_id": webhook_id},
                    )
                    .on_conflict_do_nothing(
                        index_elements=[SyncOperation.operation_id]
                    )
                    .returning(SyncOperation.id)
                )
            ).scalar_one_or_none()
            if claimed is None:
                existing = (
                    await session.execute(
                        select(SyncOperation).where(
                            SyncOperation.operation_id == operation_id
                        )
                    )
                ).scalar_one_or_none()
                prior_status = (
                    (existing.operation_metadata or {}).get("result_status")
                    if existing is not None
                    else None
                )
                logger.info("Webhook %s già processato/in corso: skip", webhook_id)
                return {
                    "status": prior_status or "duplicate",
                    "webhook_id": webhook_id,
                    "reason": "already processing or processed",
                    "duplicate": True,
                }

            order_id = (
                data.get("id")
                if isinstance(data, dict)
                else None
            ) or payload.get("object_id")
            if cause in {"order.create", "order.update", "order.destroy"} and order_id is not None:
                # Sale and terminal actions use different ledger rows, so they
                # must share one transaction lock per user/order. This closes
                # the paid-vs-cancel race across workers and containers.
                await _lock_order(session, user_uuid, order_id)

            if cause == "order.create":
                result = await self._handle_order_create(
                    webhook_id, data, user_id, session
                )
            elif cause == "order.update":
                result = await self._handle_order_update(
                    webhook_id, data, user_id, session
                )
            elif cause == "order.destroy":
                result = await self._handle_order_destroy(
                    webhook_id,
                    data,
                    user_id,
                    session,
                    object_id=payload.get("object_id"),
                )
            else:
                result = {
                    "status": "ignored",
                    "webhook_id": webhook_id,
                    "cause": cause,
                    "reason": "Unsupported webhook cause",
                }

            operation = await session.get(SyncOperation, claimed)
            if operation is None:
                raise RuntimeError("Webhook idempotency record disappeared")
            operation.status = "completed"
            operation.completed_at = datetime.utcnow()
            operation.operation_metadata = {
                "cause": cause,
                "webhook_id": webhook_id,
                "result_status": result.get("status"),
            }
            await session.flush()
            return result
    
    async def _handle_order_create(
        self,
        webhook_id: str,
        order: Dict[str, Any],
        user_id: Optional[str] = None,
        session: AsyncSession | None = None,
    ) -> Dict[str, Any]:
        """Handle order.create webhook - decrement quantities."""
        order_state = order.get("state", "")
        order_id = order.get("id")
        
        # Only process paid orders (products are actually sold)
        if order_state != "paid":
            return {
                "status": "ignored",
                "webhook_id": webhook_id,
                "order_id": order_id,
                "reason": f"Order state is '{order_state}', not 'paid'"
            }
        
        # Use provided user_id or extract from order
        if not user_id:
            seller = order.get("seller", {})
            seller_id = seller.get("id")
            user_id = str(seller_id) if seller_id else None
        
        if not user_id:
            return {
                "status": "error",
                "webhook_id": webhook_id,
                "order_id": order_id,
                "message": "No user_id provided and cannot extract from order"
            }
        
        # Process order items
        order_items = _normalized_order_items(order.get("order_items", []))
        processed_items = []
        errors = []
        
        # Convert user_id to UUID for database query
        try:
            user_uuid = uuid.UUID(user_id)
        except ValueError:
            return {
                "status": "error",
                "webhook_id": webhook_id,
                "order_id": order_id,
                "message": f"Invalid user_id format: {user_id}"
            }

        if session is None:
            raise RuntimeError("Webhook transaction missing")
        if order_id is None or not order_items:
            return {
                "status": "reconcile_required",
                "webhook_id": webhook_id,
                "order_id": order_id,
                "reason": "Paid order is missing a stable id or item snapshot",
            }

        restore_operation_id = f"cardtrader-order:{user_uuid}:{order_id}:restore"
        terminal_adjustment = (
            await session.execute(
                select(SyncOperation.id).where(
                    SyncOperation.operation_id == restore_operation_id
                )
            )
        ).scalar_one_or_none()
        if terminal_adjustment is not None:
            return {
                "status": "reconcile_required",
                "webhook_id": webhook_id,
                "order_id": order_id,
                "reason": "A terminal order event was already observed",
            }

        adjustment_id = f"cardtrader-order:{user_uuid}:{order_id}:sale"
        adjustment_pk = await _claim_inventory_adjustment(
            session,
            user_id=user_uuid,
            operation_id=adjustment_id,
            metadata={
                "order_id": str(order_id),
                "action": "sale",
                "order_items": order_items,
            },
        )
        if adjustment_pk is None:
            return {
                "status": "duplicate",
                "webhook_id": webhook_id,
                "order_id": order_id,
                "reason": "Order sale already applied",
            }

        for item in order_items:
            product_id = item.get("product_id")
            quantity = item.get("quantity", 0)

            try:
                # Find inventory item by external_stock_id AND user_id
                stmt = select(UserInventoryItem).where(
                    UserInventoryItem.external_stock_id == str(product_id),
                    UserInventoryItem.user_id == user_uuid
                ).with_for_update()
                result = await session.execute(stmt)
                inventory_item = result.scalar_one_or_none()

                if inventory_item:
                    # Decrement quantity (but don't go below 0)
                    old_quantity = inventory_item.quantity
                    new_quantity = max(0, inventory_item.quantity - quantity)
                    inventory_item.quantity = new_quantity
                    inventory_item.updated_at = datetime.utcnow()

                    processed_items.append({
                        "product_id": product_id,
                        "old_quantity": old_quantity,
                        "new_quantity": new_quantity,
                        "sold_quantity": quantity
                    })

                    logger.info(
                        f"Decremented quantity for product {product_id}: "
                        f"{old_quantity} -> {new_quantity} (sold {quantity})"
                    )
                else:
                    errors.append({
                        "product_id": product_id,
                        "error": "Product not found in local inventory"
                    })
                    logger.warning(
                        f"Product {product_id} from order {order_id} not found in local inventory"
                    )

            except Exception as e:
                errors.append({
                    "product_id": product_id,
                    "error": str(e)
                })
                logger.error(
                    f"Error processing product {product_id} from order {order_id}: {e}",
                    exc_info=True
                )

        result_status = "reconcile_required" if errors else "processed"
        await _complete_inventory_adjustment(
            session,
            adjustment_pk,
            {
                "order_id": str(order_id),
                "action": "sale",
                "order_items": order_items,
                "items_processed": len(processed_items),
                "errors": errors,
            },
        )
        return {
            "status": result_status,
            "webhook_id": webhook_id,
            "order_id": order_id,
            "items_processed": len(processed_items),
            "items": processed_items,
            "errors": errors
        }
    
    async def _handle_order_update(
        self,
        webhook_id: str,
        order: Dict[str, Any],
        user_id: Optional[str] = None,
        session: AsyncSession | None = None,
    ) -> Dict[str, Any]:
        """Handle order.update webhook - handle state changes."""
        order_state = order.get("state", "")
        order_id = order.get("id")
        previous_state = order.get("previous_state")  # CardTrader might include this
        
        # Extract user_id if not provided
        if not user_id:
            seller = order.get("seller", {})
            if isinstance(seller, dict) and seller.get("id"):
                user_id = str(seller.get("id"))
        
        logger.info(
            f"Processing order.update for order {order_id}: "
            f"state={order_state}, previous_state={previous_state}, user_id={user_id}"
        )

        # Some CardTrader orders are created before reaching the paid state.
        # Apply the sale on the first paid transition; the order-level ledger
        # makes this safe if create/update notifications overlap.
        if order_state == "paid" and previous_state != "paid":
            return await self._handle_order_create(
                webhook_id, order, user_id, session
            )
        
        # If order was cancelled, restore quantities
        if order_state in ("canceled", "request_for_cancel"):
            return await self._restore_order_quantities(
                webhook_id, order, user_id, session
            )
        
        # If order changed from paid to another state, restore quantities
        if previous_state == "paid" and order_state != "paid":
            return await self._restore_order_quantities(
                webhook_id, order, user_id, session
            )
        
        # For other state changes, just log
        return {
            "status": "ignored",
            "webhook_id": webhook_id,
            "order_id": order_id,
            "reason": f"Order state change from '{previous_state}' to '{order_state}' doesn't require quantity adjustment"
        }
    
    async def _handle_order_destroy(
        self,
        webhook_id: str,
        order: Dict[str, Any],
        user_id: Optional[str] = None,
        session: AsyncSession | None = None,
        object_id: Any = None,
    ) -> Dict[str, Any]:
        """Destroy payloads have no item snapshot; request authoritative sync."""
        order_id = order.get("id") or object_id
        
        # Extract user_id if not provided
        if not user_id:
            seller = order.get("seller", {})
            if isinstance(seller, dict) and seller.get("id"):
                user_id = str(seller.get("id"))
        
        logger.info(f"Processing order.destroy for order {order_id}, user_id={user_id}")

        if session is not None and user_id and order_id is not None:
            try:
                user_uuid = uuid.UUID(user_id)
            except ValueError:
                user_uuid = None
            if user_uuid is not None:
                adjustment_id = (
                    f"cardtrader-order:{user_uuid}:{order_id}:restore"
                )
                adjustment_pk = await _claim_inventory_adjustment(
                    session,
                    user_id=user_uuid,
                    operation_id=adjustment_id,
                    metadata={
                        "order_id": str(order_id),
                        "action": "authoritative_reconcile",
                    },
                )
                if adjustment_pk is not None:
                    await _complete_inventory_adjustment(
                        session,
                        adjustment_pk,
                        {
                            "order_id": str(order_id),
                            "action": "authoritative_reconcile",
                        },
                    )

        return {
            "status": "reconcile_required",
            "webhook_id": webhook_id,
            "order_id": order_id,
            "reason": "order.destroy requires authoritative CardTrader reconciliation",
        }
    
    async def _restore_order_quantities(
        self,
        webhook_id: str,
        order: Dict[str, Any],
        user_id: Optional[str] = None,
        session: AsyncSession | None = None,
    ) -> Dict[str, Any]:
        """Restore quantities for cancelled/deleted orders."""
        order_id = order.get("id")
        order_items = _normalized_order_items(order.get("order_items", []))
        processed_items = []
        errors = []
        
        # Extract user_id if not provided
        if not user_id:
            seller = order.get("seller", {})
            if isinstance(seller, dict) and seller.get("id"):
                user_id = str(seller.get("id"))
        
        if not user_id:
            return {
                "status": "error",
                "webhook_id": webhook_id,
                "order_id": order_id,
                "message": "No user_id provided and cannot extract from order"
            }
        
        # Convert user_id to UUID
        try:
            user_uuid = uuid.UUID(user_id)
        except ValueError:
            return {
                "status": "error",
                "webhook_id": webhook_id,
                "order_id": order_id,
                "message": f"Invalid user_id format: {user_id}"
            }
        
        if session is None:
            raise RuntimeError("Webhook transaction missing")
        if order_id is None:
            return {
                "status": "reconcile_required",
                "webhook_id": webhook_id,
                "reason": "Canceled order is missing a stable id",
            }

        sale_operation_id = f"cardtrader-order:{user_uuid}:{order_id}:sale"
        sale_operation = (
            await session.execute(
                select(SyncOperation).where(
                    SyncOperation.operation_id == sale_operation_id,
                    SyncOperation.status == "completed",
                )
            )
        ).scalar_one_or_none()
        if sale_operation is None:
            # Record the terminal transition even if its earlier sale event is
            # absent/out of order. A late paid event will then reconcile rather
            # than decrementing stock after cancellation.
            adjustment_id = f"cardtrader-order:{user_uuid}:{order_id}:restore"
            adjustment_pk = await _claim_inventory_adjustment(
                session,
                user_id=user_uuid,
                operation_id=adjustment_id,
                metadata={
                    "order_id": str(order_id),
                    "action": "authoritative_reconcile",
                },
            )
            if adjustment_pk is not None:
                await _complete_inventory_adjustment(
                    session,
                    adjustment_pk,
                    {
                        "order_id": str(order_id),
                        "action": "authoritative_reconcile",
                    },
                )
            return {
                "status": "reconcile_required",
                "webhook_id": webhook_id,
                "order_id": order_id,
                "reason": "No local sale ledger exists for this order",
            }
        if not order_items:
            order_items = _normalized_order_items(
                (sale_operation.operation_metadata or {}).get("order_items", [])
            )
        if not order_items:
            return {
                "status": "reconcile_required",
                "webhook_id": webhook_id,
                "order_id": order_id,
                "reason": "No item snapshot is available for this order",
            }

        adjustment_id = f"cardtrader-order:{user_uuid}:{order_id}:restore"
        adjustment_pk = await _claim_inventory_adjustment(
            session,
            user_id=user_uuid,
            operation_id=adjustment_id,
            metadata={
                "order_id": str(order_id),
                "action": "restore",
                "order_items": order_items,
            },
        )
        if adjustment_pk is None:
            return {
                "status": "duplicate",
                "webhook_id": webhook_id,
                "order_id": order_id,
                "reason": "Order restoration already applied",
            }

        for item in order_items:
            product_id = item.get("product_id")
            quantity = item.get("quantity", 0)

            if not product_id or quantity <= 0:
                continue

            try:
                # Find inventory item by external_stock_id AND user_id
                stmt = select(UserInventoryItem).where(
                    UserInventoryItem.external_stock_id == str(product_id),
                    UserInventoryItem.user_id == user_uuid
                ).with_for_update()
                result = await session.execute(stmt)
                inventory_item = result.scalar_one_or_none()

                if inventory_item:
                    # Restore quantity
                    old_quantity = inventory_item.quantity
                    new_quantity = inventory_item.quantity + quantity
                    inventory_item.quantity = new_quantity
                    inventory_item.updated_at = datetime.utcnow()

                    processed_items.append({
                        "product_id": product_id,
                        "old_quantity": old_quantity,
                        "new_quantity": new_quantity,
                        "restored_quantity": quantity
                    })

                    logger.info(
                        f"Restored quantity for product {product_id}: "
                        f"{old_quantity} -> {new_quantity} (restored {quantity})"
                    )
                else:
                    errors.append({
                        "product_id": product_id,
                        "error": "Product not found in local inventory"
                    })

            except Exception as e:
                errors.append({
                    "product_id": product_id,
                    "error": str(e)
                })
                logger.error(
                    f"Error restoring quantity for product {product_id}: {e}",
                    exc_info=True
                )

        result_status = "reconcile_required" if errors else "processed"
        await _complete_inventory_adjustment(
            session,
            adjustment_pk,
            {
                "order_id": str(order_id),
                "action": "restore",
                "order_items": order_items,
                "items_processed": len(processed_items),
                "errors": errors,
            },
        )
        return {
            "status": result_status,
            "webhook_id": webhook_id,
            "order_id": order_id,
            "action": "restore_quantities",
            "items_processed": len(processed_items),
            "items": processed_items,
            "errors": errors
        }
    
    async def sync_products_from_cardtrader(
        self,
        user_uuid: uuid.UUID,
        blueprint_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Sync products from CardTrader to local database.
        
        This is used for periodic synchronization to catch changes
        made directly on CardTrader (not via our API).
        
        Args:
            user_uuid: User UUID
            blueprint_id: Optional blueprint_id filter
            
        Returns:
            Sync result
        """
        async with get_isolated_db_session() as session:
            # Get user sync settings
            stmt = select(UserSyncSettings).where(
                UserSyncSettings.user_id == user_uuid
            )
            result = await session.execute(stmt)
            sync_settings = result.scalar_one_or_none()
            
            if not sync_settings:
                return {
                    "status": "error",
                    "message": "User sync settings not found"
                }
            
            # Decrypt token
            token = self.crypto_manager.decrypt(
                sync_settings.cardtrader_token_encrypted
            )
            
            # Fetch products from CardTrader
            async with CardTraderClient(token, str(user_uuid)) as client:
                products = await client.get_products_export(
                    blueprint_id=blueprint_id
                )
            
            # Sync products to local database
            updated = 0
            created = 0
            errors = []
            
            for product in products:
                try:
                    product_id = str(product.get("id"))
                    blueprint_id_ct = product.get("blueprint_id")
                    quantity = product.get("quantity", 0)
                    price_cents = product.get("price_cents", 0)
                    description = product.get("description", "")
                    user_data_field = product.get("user_data_field", "")
                    graded = product.get("graded", False)
                    properties_hash = product.get("properties_hash", {})
                    
                    # Find or create inventory item
                    stmt = select(UserInventoryItem).where(
                        UserInventoryItem.external_stock_id == product_id,
                        UserInventoryItem.user_id == user_uuid
                    )
                    result = await session.execute(stmt)
                    inventory_item = result.scalar_one_or_none()
                    
                    if inventory_item:
                        # Update existing item
                        inventory_item.quantity = quantity
                        inventory_item.price_cents = price_cents
                        inventory_item.description = description
                        inventory_item.user_data_field = user_data_field
                        inventory_item.graded = graded
                        inventory_item.properties = properties_hash
                        inventory_item.source = "cardtrader"
                        inventory_item.updated_at = datetime.utcnow()
                        updated += 1
                    else:
                        # Create new item (if we have blueprint_id mapping)
                        # Note: We need blueprint_id from our MySQL mapping
                        from app.services.blueprint_mapper import get_blueprint_mapper
                        mapper = get_blueprint_mapper()
                        ebartex_blueprint_id = mapper.get_ebartex_blueprint_id(
                            blueprint_id_ct
                        )
                        
                        if ebartex_blueprint_id:
                            new_item = UserInventoryItem(
                                user_id=user_uuid,
                                blueprint_id=ebartex_blueprint_id,
                                quantity=quantity,
                                price_cents=price_cents,
                                description=description,
                                user_data_field=user_data_field,
                                graded=graded,
                                properties=properties_hash,
                                external_stock_id=product_id,
                                source="cardtrader",
                                created_at=datetime.utcnow(),
                                updated_at=datetime.utcnow()
                            )
                            session.add(new_item)
                            created += 1
                        else:
                            errors.append({
                                "product_id": product_id,
                                "error": f"Blueprint {blueprint_id_ct} not found in mapping"
                            })
                
                except Exception as e:
                    errors.append({
                        "product_id": product.get("id"),
                        "error": str(e)
                    })
                    logger.error(
                        f"Error syncing product {product.get('id')}: {e}",
                        exc_info=True
                    )
            
            await session.commit()
        
        return {
            "status": "completed",
            "updated": updated,
            "created": created,
            "errors": errors,
            "total_processed": updated + created
        }
