BEGIN;

LOCK TABLE cardtrader_sync_outbox IN ACCESS EXCLUSIVE MODE;

ALTER TABLE cardtrader_sync_outbox
    DROP CONSTRAINT IF EXISTS ck_cardtrader_sync_outbox_operation;

ALTER TABLE cardtrader_sync_outbox
    ADD CONSTRAINT ck_cardtrader_sync_outbox_operation
    CHECK (
        operation_type IN (
            'create_product',
            'update_product',
            'delete_product'
        )
    );

COMMIT;
