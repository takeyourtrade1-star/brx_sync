#!/usr/bin/env python3
"""
Script to apply composite index migration for optimized bulk sync.
"""
import asyncio
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from sqlalchemy import text
from app.core.database import get_sync_db_engine


def run_migration():
    """Apply composite index migration."""
    engine = get_sync_db_engine()
    
    migration_sql = """
    -- Add composite index for faster lookups during bulk sync
    -- This index optimizes the query: WHERE user_id = ? AND blueprint_id = ? AND external_stock_id = ?

    CREATE INDEX IF NOT EXISTS idx_inventory_user_blueprint_external 
    ON user_inventory_items(user_id, blueprint_id, external_stock_id);

    -- This index will be used for the batch SELECT to find existing items
    -- It covers the exact columns used in the WHERE clause
    """
    
    try:
        with engine.begin() as conn:
            print("🔄 Applying composite index migration...")
            conn.execute(text(migration_sql))
            print("✅ Composite index created successfully!")
            print("   Index: idx_inventory_user_blueprint_external")
            print("   Columns: (user_id, blueprint_id, external_stock_id)")
            return True
    except Exception as e:
        print(f"❌ Error applying migration: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        engine.dispose()


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)
