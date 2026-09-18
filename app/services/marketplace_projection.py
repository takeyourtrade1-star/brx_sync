"""Safe projection of synced CardTrader stock into marketplace listings."""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def project_inventory_to_marketplace(
    session: AsyncSession,
    user_id: uuid.UUID,
    environment: str,
) -> bool:
    """Project stock when marketplace migrations are present.

    The sync service can be deployed before the marketplace service. In that
    window the projection is optional and must not abort the inventory sync.
    """

    table_exists = await session.scalar(text("SELECT to_regclass('mkt_listings') IS NOT NULL"))
    if not table_exists:
        return False

    await session.execute(
        text("""
            UPDATE mkt_listings AS listing
            SET quantity = 0,
                status = 'pending_sync',
                updated_at = NOW()
            FROM user_inventory_items AS inventory
            WHERE listing.user_id = CAST(:user_id AS uuid)
              AND inventory.user_id = CAST(:user_id AS uuid)
              AND inventory.source = 'cardtrader'
              AND inventory.environment = :environment
              AND (
                  inventory.sync_state <> 'synced'
                  OR inventory.sync_uncertain_event_id IS NOT NULL
                  OR inventory.game_id IS DISTINCT FROM 1
                  OR inventory.mapping_status IS DISTINCT FROM 'mapped'
                  OR inventory.lifecycle_status NOT IN ('active', 'sold_out')
              )
              AND listing.cardtrader_article_id = CASE
                  WHEN inventory.external_stock_id ~ '^[0-9]+$'
                  THEN CAST(inventory.external_stock_id AS BIGINT)
                  ELSE NULL
              END
              AND listing.status <> 'cancelled'
            """),
        {"user_id": str(user_id), "environment": environment},
    )

    await session.execute(
        text("""
            UPDATE mkt_listings AS listing
            SET quantity = inventory.quantity,
                status = CASE
                    WHEN inventory.quantity > 0 THEN 'active'
                    ELSE 'sold'
                END,
                updated_at = NOW()
            FROM user_inventory_items AS inventory
            WHERE listing.user_id = CAST(:user_id AS uuid)
              AND inventory.user_id = CAST(:user_id AS uuid)
              AND inventory.source = 'cardtrader'
              AND inventory.environment = :environment
              AND inventory.game_id = 1
              AND inventory.mapping_status = 'mapped'
              AND inventory.lifecycle_status IN ('active', 'sold_out')
              AND inventory.sync_state = 'synced'
              AND inventory.sync_uncertain_event_id IS NULL
              AND listing.cardtrader_article_id = CASE
                  WHEN inventory.external_stock_id ~ '^[0-9]+$'
                  THEN CAST(inventory.external_stock_id AS BIGINT)
                  ELSE NULL
              END
              AND listing.status IN ('active','sold','pending_sync','sync_failed')
            """),
        {"user_id": str(user_id), "environment": environment},
    )
    return True
