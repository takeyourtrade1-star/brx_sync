-- Add composite index for faster lookups during bulk sync
-- This index optimizes the query: WHERE user_id = ? AND blueprint_id = ? AND external_stock_id = ?

CREATE INDEX IF NOT EXISTS idx_inventory_user_blueprint_external 
ON user_inventory_items(user_id, blueprint_id, external_stock_id);

-- This index will be used for the batch SELECT to find existing items
-- It covers the exact columns used in the WHERE clause
