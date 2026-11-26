-- Live eBay stores share the demo_* tables so roles, costs, insights and exports work unchanged.
-- source='demo' rows are synthetic; source='ebay' rows are connected seller accounts.
ALTER TABLE demo_stores ADD COLUMN source TEXT NOT NULL DEFAULT 'demo' CHECK(source IN ('demo','ebay'));
ALTER TABLE demo_stores ADD COLUMN marketplace TEXT;
ALTER TABLE demo_stores ADD COLUMN ebay_user TEXT;
ALTER TABLE demo_stores ADD COLUMN connected INTEGER NOT NULL DEFAULT 0;
ALTER TABLE demo_stores ADD COLUMN token BLOB;
ALTER TABLE demo_stores ADD COLUMN token_expires TEXT;
ALTER TABLE demo_stores ADD COLUMN refresh_token BLOB;
ALTER TABLE demo_stores ADD COLUMN refresh_expires TEXT;
ALTER TABLE demo_stores ADD COLUMN sync_at TEXT;
ALTER TABLE demo_stores ADD COLUMN sync_cursor TEXT;
ALTER TABLE demo_stores ADD COLUMN sync_error TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_demo_stores_ebay_user ON demo_stores(ebay_user) WHERE ebay_user IS NOT NULL;
