-- Mantiene visibile lo stock bloccato dagli scambi fino alla chiusura.
-- Idempotente; applicare dopo 20260714_trade_inventory_foundations.sql.

BEGIN;

ALTER TABLE user_inventory_items
    ADD COLUMN IF NOT EXISTS reserved_quantity INTEGER NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_user_inventory_items_reserved_quantity'
    ) THEN
        ALTER TABLE user_inventory_items
            ADD CONSTRAINT ck_user_inventory_items_reserved_quantity
            CHECK (reserved_quantity >= 0);
    END IF;
END $$;

-- Recupera eventuali escrow gia' attivi al momento del deploy.
DO $$
BEGIN
    IF to_regclass('public.trades') IS NOT NULL
       AND to_regclass('public.trade_items') IS NOT NULL THEN
        UPDATE user_inventory_items AS inventory
        SET reserved_quantity = active.reserved_quantity
        FROM (
            SELECT ti.inventory_item_id, SUM(ti.quantity)::INTEGER AS reserved_quantity
            FROM trade_items AS ti
            JOIN trades AS trade ON trade.id = ti.trade_id
            WHERE ti.inventory_source = 'sync'
              AND ti.inventory_item_id IS NOT NULL
              AND ti.escrowed_at IS NOT NULL
              AND ti.released_at IS NULL
              AND trade.status IN ('ACCEPTED', 'DISPUTED')
            GROUP BY ti.inventory_item_id
        ) AS active
        WHERE inventory.id = active.inventory_item_id;
    END IF;
END $$;

ALTER TABLE inventory_ops
    DROP CONSTRAINT IF EXISTS ck_inventory_ops_kind;
ALTER TABLE inventory_ops
    ADD CONSTRAINT ck_inventory_ops_kind
    CHECK (kind IN ('reserve', 'release', 'consume', 'credit'));

COMMIT;
