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

    table_exists = await session.scalar(
        text("SELECT to_regclass('public.mkt_listings') IS NOT NULL")
    )
    if not table_exists:
        return False

    await session.execute(
        text(
            """
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
              AND inventory.sync_state = 'synced'
              AND listing.cardtrader_article_id = CASE
                  WHEN inventory.external_stock_id ~ '^[0-9]+$'
                  THEN CAST(inventory.external_stock_id AS BIGINT)
                  ELSE NULL
              END
              AND listing.status IN ('active','sold','sync_failed')
            """
        ),
        {"user_id": str(user_id), "environment": environment},
    )
    return True
