#!/usr/bin/env python3
"""
Script per eseguire la migration add_description_user_data_graded.sql
"""
import sys
import os

# Aggiungi il path del progetto
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.core.config import get_settings
from app.core.database import get_sync_db_engine
from sqlalchemy import text

def run_migration():
    """Esegue la migration per aggiungere description, user_data_field, graded."""
    settings = get_settings()
    engine = get_sync_db_engine()
    
    migration_sql = """
    -- Migration: Add description, user_data_field, and graded columns to user_inventory_items
    -- Date: 2026-02-19
    -- Description: Adds support for CardTrader description, user_data_field, and graded fields

    -- Add description column
    ALTER TABLE user_inventory_items 
    ADD COLUMN IF NOT EXISTS description TEXT;

    -- Add user_data_field column
    ALTER TABLE user_inventory_items 
    ADD COLUMN IF NOT EXISTS user_data_field TEXT;

    -- Add graded column (boolean, top-level field, not in properties)
    ALTER TABLE user_inventory_items 
    ADD COLUMN IF NOT EXISTS graded BOOLEAN;

    -- Add comment to columns
    COMMENT ON COLUMN user_inventory_items.description IS 'Product description visible to all users';
    COMMENT ON COLUMN user_inventory_items.user_data_field IS 'Custom metadata field for internal use (warehouse location, etc.)';
    COMMENT ON COLUMN user_inventory_items.graded IS 'Whether the product is graded (top-level field, not in properties)';
    """
    
    print("🔄 Esecuzione migration: add_description_user_data_graded")
    print(f"📊 Database: {settings.DATABASE_URL.split('@')[-1] if '@' in settings.DATABASE_URL else 'N/A'}")
    
    try:
        with engine.begin() as conn:
            # Esegui ogni statement separatamente
            statements = [
                "ALTER TABLE user_inventory_items ADD COLUMN IF NOT EXISTS description TEXT;",
                "ALTER TABLE user_inventory_items ADD COLUMN IF NOT EXISTS user_data_field TEXT;",
                "ALTER TABLE user_inventory_items ADD COLUMN IF NOT EXISTS graded BOOLEAN;",
                "COMMENT ON COLUMN user_inventory_items.description IS 'Product description visible to all users';",
                "COMMENT ON COLUMN user_inventory_items.user_data_field IS 'Custom metadata field for internal use (warehouse location, etc.)';",
                "COMMENT ON COLUMN user_inventory_items.graded IS 'Whether the product is graded (top-level field, not in properties)';",
            ]
            
            for i, stmt in enumerate(statements, 1):
                print(f"  [{i}/{len(statements)}] Esecuzione statement...")
                conn.execute(text(stmt))
            
            print("✅ Migration completata con successo!")
            print("\n📋 Colonne aggiunte:")
            print("   - description (TEXT)")
            print("   - user_data_field (TEXT)")
            print("   - graded (BOOLEAN)")
            
    except Exception as e:
        print(f"❌ Errore durante la migration: {e}")
        sys.exit(1)

if __name__ == "__main__":
    run_migration()
