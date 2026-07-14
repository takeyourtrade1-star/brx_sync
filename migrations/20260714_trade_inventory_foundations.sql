-- Fase 0 backend scambi: source inventario + registro operazioni idempotenti.
-- Idempotente; applicare prima del codice che legge user_inventory_items.source.

BEGIN;

ALTER TABLE user_inventory_items
    ADD COLUMN IF NOT EXISTS source VARCHAR(32);

UPDATE user_inventory_items
SET source = CASE
    WHEN external_stock_id IS NOT NULL THEN 'cardtrader'
    ELSE 'internal_test'
END
WHERE source IS NULL;

ALTER TABLE user_inventory_items
    ALTER COLUMN source SET DEFAULT 'internal_test',
    ALTER COLUMN source SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_user_inventory_items_source'
    ) THEN
        ALTER TABLE user_inventory_items
            ADD CONSTRAINT ck_user_inventory_items_source
            CHECK (source IN ('cardtrader', 'trade', 'internal_test'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_inventory_user_source_quantity
    ON user_inventory_items(user_id, source, quantity);

CREATE TABLE IF NOT EXISTS inventory_ops (
    id BIGSERIAL PRIMARY KEY,
    op_key VARCHAR(255) NOT NULL,
    kind VARCHAR(32) NOT NULL,
    payload_json JSONB NOT NULL,
    result_json JSONB,
    status VARCHAR(32) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CONSTRAINT uq_inventory_ops_op_key UNIQUE (op_key),
    CONSTRAINT ck_inventory_ops_kind CHECK (kind IN ('reserve', 'release', 'credit')),
    CONSTRAINT ck_inventory_ops_status CHECK (status IN ('processing', 'succeeded', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_inventory_ops_kind ON inventory_ops(kind);
CREATE INDEX IF NOT EXISTS idx_inventory_ops_status ON inventory_ops(status);

COMMIT;

