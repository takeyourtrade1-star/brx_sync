-- Durable CardTrader catalog repair queue.
-- The migration role owns this DDL; runtime workers only use the grants
-- already assigned to the sync service role.  No CardTrader or MySQL writes
-- are performed by this migration.

BEGIN;

-- PostgreSQL 13+ exposes gen_random_uuid() as a core function. Fail before
-- creating any queue table on an incompatible server instead of committing a
-- partially usable schema or depending on the optional uuid-ossp extension.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_proc
        WHERE proname = 'gen_random_uuid'
    ) THEN
        RAISE EXCEPTION 'catalog queue requires PostgreSQL gen_random_uuid()';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS catalog_import_jobs (
    id BIGSERIAL PRIMARY KEY,
    provider VARCHAR(32) NOT NULL DEFAULT 'cardtrader',
    game_id INTEGER NOT NULL,
    blueprint_id BIGINT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lease_token UUID,
    lease_until TIMESTAMPTZ,
    source_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_json JSONB,
    last_error_code VARCHAR(96),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CONSTRAINT uq_catalog_import_provider_game_blueprint
        UNIQUE (provider, game_id, blueprint_id),
    CONSTRAINT ck_catalog_import_jobs_game_id CHECK (game_id > 0),
    CONSTRAINT ck_catalog_import_jobs_blueprint_id CHECK (blueprint_id > 0),
    CONSTRAINT ck_catalog_import_jobs_attempts CHECK (attempts >= 0),
    CONSTRAINT ck_catalog_import_jobs_status
        CHECK (status IN ('pending','running','failed','needs_review','succeeded'))
);

CREATE INDEX IF NOT EXISTS idx_catalog_import_jobs_due
    ON catalog_import_jobs(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_catalog_import_jobs_lease
    ON catalog_import_jobs(lease_until);

CREATE TABLE IF NOT EXISTS catalog_import_requests (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES catalog_import_jobs(id) ON DELETE CASCADE,
    user_id UUID NOT NULL,
    game_id INTEGER NOT NULL,
    blueprint_id BIGINT NOT NULL,
    external_stock_id VARCHAR(255) NOT NULL,
    environment VARCHAR(20) NOT NULL,
    mode_version INTEGER NOT NULL DEFAULT 1,
    product_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_catalog_import_request_observation
        UNIQUE (job_id, user_id, external_stock_id, environment),
    CONSTRAINT ck_catalog_import_requests_game_id CHECK (game_id > 0),
    CONSTRAINT ck_catalog_import_requests_blueprint_id CHECK (blueprint_id > 0),
    CONSTRAINT ck_catalog_import_requests_environment
        CHECK (environment IN ('demo','partial','real')),
    CONSTRAINT ck_catalog_import_requests_mode_version CHECK (mode_version > 0)
);

CREATE INDEX IF NOT EXISTS idx_catalog_import_requests_job
    ON catalog_import_requests(job_id, created_at);
CREATE INDEX IF NOT EXISTS idx_catalog_import_requests_user
    ON catalog_import_requests(user_id, created_at);

CREATE TABLE IF NOT EXISTS catalog_index_outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id BIGINT NOT NULL REFERENCES catalog_import_jobs(id) ON DELETE CASCADE,
    document_id VARCHAR(255) NOT NULL,
    document_json JSONB NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lease_token UUID,
    lease_until TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CONSTRAINT uq_catalog_index_outbox_job_document UNIQUE (job_id, document_id),
    CONSTRAINT ck_catalog_index_outbox_attempts CHECK (attempts >= 0),
    CONSTRAINT ck_catalog_index_outbox_status
        CHECK (status IN ('pending','running','failed','needs_review','succeeded'))
);

CREATE INDEX IF NOT EXISTS idx_catalog_index_outbox_due
    ON catalog_index_outbox(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_catalog_index_outbox_lease
    ON catalog_index_outbox(lease_until);

-- Runtime API/worker role is deliberately limited to queue DML.  Keep this
-- conditional so disposable databases can apply the same migration before
-- the production role has been created.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brx_sync_runtime_v1') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON catalog_import_jobs, catalog_import_requests, catalog_index_outbox TO brx_sync_runtime_v1';
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE catalog_import_jobs_id_seq, catalog_import_requests_id_seq TO brx_sync_runtime_v1';
    END IF;
END $$;

COMMIT;
