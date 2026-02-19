"""
Webhook processor for CardTrader order notifications.

Handles bidirectional synchronization:
- When orders are created/updated/cancelled on CardTrader
- Updates local inventory quantities accordingly
- Prevents infinite sync loops
"""
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_isolated_db_session, get_db_session_context
from app.models.inventory import UserInventoryItem, UserSyncSettings
from app.services.cardtrader_client import CardTraderClient
from app.core.crypto import get_encryption_manager

logger = logging.getLogger(__name__)


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
        
        # If user_id not provided, try to extract from payload
        if not user_id:
            if isinstance(data, dict):
                seller = data.get("seller", {})
                if isinstance(seller, dict) and seller.get("id"):
                    user_id = str(seller.get("id"))
        
        logger.info(
            f"Processing webhook {webhook_id}: cause={cause}, mode={mode}, user_id={user_id}"
        )
        
        # Handle different webhook causes
        if cause == "order.create":
            return await self._handle_order_create(webhook_id, data, user_id)
        elif cause == "order.update":
            return await self._handle_order_update(webhook_id, data, user_id)
        elif cause == "order.destroy":
            return await self._handle_order_destroy(webhook_id, data, user_id)
        else:
            return {
                "status": "ignored",
                "webhook_id": webhook_id,
                "cause": cause,
                "reason": "Unsupported webhook cause"
            }
    
    async def _handle_order_create(
        self,
        webhook_id: str,
        order: Dict[str, Any],
        user_id: Optional[str] = None,
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
        order_items = order.get("order_items", [])
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
        
        async with get_db_session_context() as session:
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
                    )
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
            
            await session.commit()
        
        return {
            "status": "processed",
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
        
        # If order was cancelled, restore quantities
        if order_state in ("canceled", "request_for_cancel"):
            return await self._restore_order_quantities(webhook_id, order, user_id)
        
        # If order changed from paid to another state, restore quantities
        if previous_state == "paid" and order_state != "paid":
            return await self._restore_order_quantities(webhook_id, order, user_id)
        
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
    ) -> Dict[str, Any]:
        """Handle order.destroy webhook - restore quantities."""
        order_id = order.get("id")
        
        # Extract user_id if not provided
        if not user_id:
            seller = order.get("seller", {})
            if isinstance(seller, dict) and seller.get("id"):
                user_id = str(seller.get("id"))
        
        logger.info(f"Processing order.destroy for order {order_id}, user_id={user_id}")
        
        # When order is destroyed, restore quantities
        return await self._restore_order_quantities(webhook_id, order, user_id)
    
    async def _restore_order_quantities(
        self,
        webhook_id: str,
        order: Dict[str, Any],
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Restore quantities for cancelled/deleted orders."""
        order_id = order.get("id")
        order_items = order.get("order_items", [])
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
        
        async with get_db_session_context() as session:
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
                    )
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
            
            await session.commit()
        
        return {
            "status": "processed",
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
        async with get_db_session_context() as session:
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
