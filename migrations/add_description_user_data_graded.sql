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
