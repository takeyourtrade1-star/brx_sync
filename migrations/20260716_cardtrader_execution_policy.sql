-- Additive, fail-closed CardTrader execution policy.
ALTER TABLE user_sync_settings
    ADD COLUMN IF NOT EXISTS execution_mode VARCHAR(20) NOT NULL DEFAULT 'demo',
    ADD COLUMN IF NOT EXISTS mode_version INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS writes_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS mode_changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Webhook credentials are now Fernet-encrypted and can exceed VARCHAR(255).
ALTER TABLE user_sync_settings
    ALTER COLUMN webhook_secret TYPE TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_user_sync_settings_execution_mode'
    ) THEN
        ALTER TABLE user_sync_settings
            ADD CONSTRAINT ck_user_sync_settings_execution_mode
            CHECK (execution_mode IN ('demo', 'partial', 'real'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_user_sync_settings_mode_version'
    ) THEN
        ALTER TABLE user_sync_settings
            ADD CONSTRAINT ck_user_sync_settings_mode_version
            CHECK (mode_version > 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_user_sync_settings_real_writes_only'
    ) THEN
        ALTER TABLE user_sync_settings
            ADD CONSTRAINT ck_user_sync_settings_real_writes_only
            CHECK (NOT writes_enabled OR execution_mode = 'real');
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_user_sync_settings_execution_policy
    ON user_sync_settings (execution_mode, writes_enabled);

CREATE TABLE IF NOT EXISTS cardtrader_webhook_inbox (
    id BIGSERIAL PRIMARY KEY,
    webhook_id VARCHAR(255) NOT NULL UNIQUE,
    user_id UUID NOT NULL,
    cause VARCHAR(100) NOT NULL,
    mode VARCHAR(20) NOT NULL DEFAULT 'live',
    payload_json JSONB NOT NULL,
    signature_valid BOOLEAN NOT NULL DEFAULT TRUE,
    status VARCHAR(32) NOT NULL DEFAULT 'received',
    attempts INTEGER NOT NULL DEFAULT 0,
    result_json JSONB,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    CONSTRAINT ck_cardtrader_webhook_inbox_status
        CHECK (status IN ('received','processing','completed','failed','ignored')),
    CONSTRAINT ck_cardtrader_webhook_inbox_attempts CHECK (attempts >= 0)
);

CREATE INDEX IF NOT EXISTS idx_cardtrader_webhook_inbox_user
    ON cardtrader_webhook_inbox (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cardtrader_webhook_inbox_status
    ON cardtrader_webhook_inbox (status, created_at);

ALTER TABLE cardtrader_webhook_inbox
    DROP CONSTRAINT IF EXISTS ck_cardtrader_webhook_inbox_status;
ALTER TABLE cardtrader_webhook_inbox
    ADD CONSTRAINT ck_cardtrader_webhook_inbox_status
    CHECK (status IN ('received','processing','completed','failed','ignored',
                      'deferred','reconcile_pending'));

CREATE TABLE IF NOT EXISTS cardtrader_order_stock_ledger (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL,
    order_id VARCHAR(255) NOT NULL,
    external_stock_id VARCHAR(255) NOT NULL,
    environment VARCHAR(20) NOT NULL DEFAULT 'real',
    quantity INTEGER NOT NULL,
    via_cardtrader_zero BOOLEAN NOT NULL DEFAULT FALSE,
    decrement_applied BOOLEAN NOT NULL DEFAULT FALSE,
    restore_applied BOOLEAN NOT NULL DEFAULT FALSE,
    last_state VARCHAR(100),
    last_webhook_id VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_cardtrader_order_stock_item
        UNIQUE (user_id, order_id, external_stock_id, environment),
    CONSTRAINT ck_cardtrader_order_stock_environment
        CHECK (environment IN ('partial','real')),
    CONSTRAINT ck_cardtrader_order_stock_quantity CHECK (quantity >= 0),
    CONSTRAINT ck_cardtrader_order_stock_restore_after_decrement
        CHECK (NOT restore_applied OR decrement_applied)
);

ALTER TABLE cardtrader_order_stock_ledger
    ADD COLUMN IF NOT EXISTS environment VARCHAR(20) NOT NULL DEFAULT 'real';
ALTER TABLE cardtrader_order_stock_ledger
    DROP CONSTRAINT IF EXISTS uq_cardtrader_order_stock_item;
ALTER TABLE cardtrader_order_stock_ledger
    ADD CONSTRAINT uq_cardtrader_order_stock_item
    UNIQUE (user_id, order_id, external_stock_id, environment);
ALTER TABLE cardtrader_order_stock_ledger
    DROP CONSTRAINT IF EXISTS ck_cardtrader_order_stock_quantity;
ALTER TABLE cardtrader_order_stock_ledger
    ADD CONSTRAINT ck_cardtrader_order_stock_quantity CHECK (quantity >= 0);

CREATE INDEX IF NOT EXISTS idx_cardtrader_order_stock_lookup
    ON cardtrader_order_stock_ledger (user_id, order_id);

ALTER TABLE user_inventory_items
    ADD COLUMN IF NOT EXISTS game_id INTEGER,
    ADD COLUMN IF NOT EXISTS environment VARCHAR(20) NOT NULL DEFAULT 'real',
    ADD COLUMN IF NOT EXISTS lifecycle_status VARCHAR(32) NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS sync_state VARCHAR(32) NOT NULL DEFAULT 'synced',
    ADD COLUMN IF NOT EXISTS sync_uncertain_event_id BIGINT,
    ADD COLUMN IF NOT EXISTS row_version INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS mapping_status VARCHAR(32) NOT NULL DEFAULT 'mapped',
    ADD COLUMN IF NOT EXISTS missing_snapshot_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_seen_snapshot_id UUID,
    ADD COLUMN IF NOT EXISTS last_external_update_at TIMESTAMPTZ;

UPDATE user_inventory_items
SET environment = CASE WHEN source = 'internal_test' THEN 'demo' ELSE 'real' END
WHERE environment = 'real';

DO $$
DECLARE
    legacy_constraint RECORD;
BEGIN
    FOR legacy_constraint IN
        SELECT constraint_row.conname
        FROM pg_constraint AS constraint_row
        WHERE constraint_row.conrelid = 'user_inventory_items'::regclass
          AND constraint_row.contype = 'u'
          AND (
              SELECT array_agg(attribute.attname::TEXT ORDER BY key_column.ordinality)
              FROM unnest(constraint_row.conkey)
                   WITH ORDINALITY AS key_column(attnum, ordinality)
              JOIN pg_attribute AS attribute
                ON attribute.attrelid = constraint_row.conrelid
               AND attribute.attnum = key_column.attnum
          ) = ARRAY['user_id','blueprint_id','external_stock_id']::TEXT[]
    LOOP
        EXECUTE format(
            'ALTER TABLE user_inventory_items DROP CONSTRAINT %I',
            legacy_constraint.conname
        );
    END LOOP;
END $$;

ALTER TABLE user_inventory_items
    DROP CONSTRAINT IF EXISTS uq_user_blueprint_external_stock;
ALTER TABLE user_inventory_items
    DROP CONSTRAINT IF EXISTS user_inventory_items_user_id_blueprint_id_external_stock_id_key;
ALTER TABLE user_inventory_items
    DROP CONSTRAINT IF EXISTS uq_user_environment_blueprint_external_stock;
ALTER TABLE user_inventory_items
    ADD CONSTRAINT uq_user_environment_blueprint_external_stock
    UNIQUE (user_id, environment, blueprint_id, external_stock_id);

CREATE TABLE IF NOT EXISTS cardtrader_sync_outbox (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    mode_version INTEGER NOT NULL,
    operation_type VARCHAR(50) NOT NULL,
    target_product_id VARCHAR(255) NOT NULL,
    inventory_item_id BIGINT,
    expected_row_version INTEGER,
    payload_json JSONB NOT NULL,
    context_json JSONB,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    job_uuid VARCHAR(255),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT ck_cardtrader_sync_outbox_operation
        CHECK (operation_type IN ('update_product','delete_product')),
    CONSTRAINT ck_cardtrader_sync_outbox_status
        CHECK (status IN ('pending','running','accepted','verified','failed','uncertain','cancelled')),
    CONSTRAINT ck_cardtrader_sync_outbox_attempts CHECK (attempts >= 0)
);

ALTER TABLE cardtrader_sync_outbox
    ADD COLUMN IF NOT EXISTS context_json JSONB;

CREATE INDEX IF NOT EXISTS idx_cardtrader_sync_outbox_pending
    ON cardtrader_sync_outbox (status, created_at);
CREATE INDEX IF NOT EXISTS idx_cardtrader_sync_outbox_user
    ON cardtrader_sync_outbox (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS cardtrader_sync_snapshots (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    environment VARCHAR(20) NOT NULL DEFAULT 'real',
    status VARCHAR(32) NOT NULL,
    product_count INTEGER,
    checksum VARCHAR(64),
    problems_json JSONB,
    result_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT ck_cardtrader_sync_snapshots_status
        CHECK (status IN ('validating','rejected','applied')),
    CONSTRAINT ck_cardtrader_sync_snapshots_environment
        CHECK (environment IN ('partial','real'))
);

ALTER TABLE cardtrader_sync_snapshots
    ADD COLUMN IF NOT EXISTS environment VARCHAR(20) NOT NULL DEFAULT 'real';

CREATE INDEX IF NOT EXISTS idx_cardtrader_sync_snapshots_user_environment_created
    ON cardtrader_sync_snapshots (user_id, environment, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_inventory_user_environment_visibility
    ON user_inventory_items
    (user_id, environment, source, lifecycle_status, sync_state);

CREATE INDEX IF NOT EXISTS idx_inventory_sync_uncertain_watermark
    ON user_inventory_items (user_id, sync_state, sync_uncertain_event_id);

CREATE INDEX IF NOT EXISTS idx_inventory_user_game
    ON user_inventory_items (user_id, game_id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_user_inventory_items_magic_only'
          AND conrelid = 'user_inventory_items'::regclass
    ) THEN
        ALTER TABLE user_inventory_items
            ADD CONSTRAINT ck_user_inventory_items_magic_only
            CHECK (game_id IS NULL OR game_id = 1);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_user_inventory_items_environment'
          AND conrelid = 'user_inventory_items'::regclass
    ) THEN
        ALTER TABLE user_inventory_items
            ADD CONSTRAINT ck_user_inventory_items_environment
            CHECK (environment IN ('demo','partial','real'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_user_inventory_items_lifecycle_status'
          AND conrelid = 'user_inventory_items'::regclass
    ) THEN
        ALTER TABLE user_inventory_items
            ADD CONSTRAINT ck_user_inventory_items_lifecycle_status
            CHECK (lifecycle_status IN
                ('active','sold_out','stale','archived','pending_delete','sync_failed'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_user_inventory_items_sync_state'
          AND conrelid = 'user_inventory_items'::regclass
    ) THEN
        ALTER TABLE user_inventory_items
            ADD CONSTRAINT ck_user_inventory_items_sync_state
            CHECK (sync_state IN ('synced','pending','accepted','failed','uncertain'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_user_inventory_items_row_version'
          AND conrelid = 'user_inventory_items'::regclass
    ) THEN
        ALTER TABLE user_inventory_items
            ADD CONSTRAINT ck_user_inventory_items_row_version CHECK (row_version > 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_user_inventory_items_mapping_status'
          AND conrelid = 'user_inventory_items'::regclass
    ) THEN
        ALTER TABLE user_inventory_items
            ADD CONSTRAINT ck_user_inventory_items_mapping_status
            CHECK (mapping_status IN ('mapped','unsupported','missing','error'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_user_inventory_items_missing_snapshot_count'
          AND conrelid = 'user_inventory_items'::regclass
    ) THEN
        ALTER TABLE user_inventory_items
            ADD CONSTRAINT ck_user_inventory_items_missing_snapshot_count
            CHECK (missing_snapshot_count >= 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_cardtrader_sync_outbox_operation'
          AND conrelid = 'cardtrader_sync_outbox'::regclass
    ) THEN
        ALTER TABLE cardtrader_sync_outbox
            ADD CONSTRAINT ck_cardtrader_sync_outbox_operation
            CHECK (operation_type IN ('update_product','delete_product'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_cardtrader_sync_outbox_status'
          AND conrelid = 'cardtrader_sync_outbox'::regclass
    ) THEN
        ALTER TABLE cardtrader_sync_outbox
            ADD CONSTRAINT ck_cardtrader_sync_outbox_status
            CHECK (status IN ('pending','running','accepted','verified','failed',
                              'uncertain','cancelled'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_cardtrader_sync_outbox_attempts'
          AND conrelid = 'cardtrader_sync_outbox'::regclass
    ) THEN
        ALTER TABLE cardtrader_sync_outbox
            ADD CONSTRAINT ck_cardtrader_sync_outbox_attempts CHECK (attempts >= 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_cardtrader_order_stock_environment'
          AND conrelid = 'cardtrader_order_stock_ledger'::regclass
    ) THEN
        ALTER TABLE cardtrader_order_stock_ledger
            ADD CONSTRAINT ck_cardtrader_order_stock_environment
            CHECK (environment IN ('partial','real'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_cardtrader_sync_snapshots_environment'
          AND conrelid = 'cardtrader_sync_snapshots'::regclass
    ) THEN
        ALTER TABLE cardtrader_sync_snapshots
            ADD CONSTRAINT ck_cardtrader_sync_snapshots_environment
            CHECK (environment IN ('partial','real'));
    END IF;
END $$;
