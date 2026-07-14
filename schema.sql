-- BRX Sync Microservice - Database Schema
-- PostgreSQL 16+

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Sync Status Enum
CREATE TYPE sync_status_enum AS ENUM ('idle', 'initial_sync', 'active', 'error');

-- ==========================================
-- 1. USER SYNC SETTINGS
-- ==========================================
CREATE TABLE user_sync_settings (
    user_id UUID PRIMARY KEY,
    cardtrader_token_encrypted TEXT NOT NULL,
    webhook_secret VARCHAR(255),
    sync_status sync_status_enum NOT NULL DEFAULT 'idle',
    last_sync_at TIMESTAMP WITH TIME ZONE,
    last_error TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_sync_settings_status ON user_sync_settings(sync_status);

-- ==========================================
-- 2. USER INVENTORY ITEMS
-- ==========================================
CREATE TABLE user_inventory_items (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL,
    blueprint_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    price_cents INTEGER NOT NULL,
    properties JSONB,
    external_stock_id VARCHAR(255),
    source VARCHAR(32) NOT NULL DEFAULT 'internal_test'
        CHECK (source IN ('cardtrader', 'trade', 'internal_test')),
    description TEXT,
    user_data_field TEXT,
    graded BOOLEAN,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, blueprint_id, external_stock_id)
);

CREATE INDEX idx_inventory_user_id ON user_inventory_items(user_id);
CREATE INDEX idx_inventory_blueprint_id ON user_inventory_items(blueprint_id);
CREATE INDEX idx_inventory_external_stock_id ON user_inventory_items(external_stock_id);
CREATE INDEX idx_inventory_updated_at ON user_inventory_items(updated_at);
CREATE INDEX idx_inventory_user_source_quantity ON user_inventory_items(user_id, source, quantity);

-- ==========================================
-- 3. SYNC OPERATIONS
-- ==========================================
CREATE TABLE sync_operations (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL,
    operation_id VARCHAR(255) NOT NULL UNIQUE,
    operation_type VARCHAR(50) NOT NULL,
    status VARCHAR(50) NOT NULL,
    operation_metadata JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX idx_sync_ops_user_id ON sync_operations(user_id);
CREATE INDEX idx_sync_ops_operation_id ON sync_operations(operation_id);
CREATE INDEX idx_sync_ops_status ON sync_operations(status);

-- ==========================================
-- 4. INTERNAL INVENTORY OPERATIONS
-- ==========================================
CREATE TABLE inventory_ops (
    id BIGSERIAL PRIMARY KEY,
    op_key VARCHAR(255) NOT NULL UNIQUE,
    kind VARCHAR(32) NOT NULL CHECK (kind IN ('reserve', 'release', 'credit')),
    payload_json JSONB NOT NULL,
    result_json JSONB,
    status VARCHAR(32) NOT NULL CHECK (status IN ('processing', 'succeeded', 'failed')),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX idx_inventory_ops_kind ON inventory_ops(kind);
CREATE INDEX idx_inventory_ops_status ON inventory_ops(status);
