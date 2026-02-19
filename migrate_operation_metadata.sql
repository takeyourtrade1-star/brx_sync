-- Migration script to rename metadata to operation_metadata in sync_operations table
-- Run this if your database still has the old 'metadata' column name

-- Check if metadata column exists and rename it
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 
        FROM information_schema.columns 
        WHERE table_name = 'sync_operations' 
        AND column_name = 'metadata'
    ) THEN
        ALTER TABLE sync_operations RENAME COLUMN metadata TO operation_metadata;
        RAISE NOTICE 'Column renamed from metadata to operation_metadata';
    ELSE
        RAISE NOTICE 'Column metadata does not exist, checking if operation_metadata exists';
        IF NOT EXISTS (
            SELECT 1 
            FROM information_schema.columns 
            WHERE table_name = 'sync_operations' 
            AND column_name = 'operation_metadata'
        ) THEN
            ALTER TABLE sync_operations ADD COLUMN operation_metadata JSONB;
            RAISE NOTICE 'Column operation_metadata added';
        ELSE
            RAISE NOTICE 'Column operation_metadata already exists';
        END IF;
    END IF;
END $$;
