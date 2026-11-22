ALTER TABLE demo_stores ADD COLUMN fulfilment_mode TEXT NOT NULL DEFAULT 'dropshipping' CHECK(fulfilment_mode IN ('dropshipping','low_stock'));
ALTER TABLE demo_stores ADD COLUMN low_stock_threshold INTEGER NOT NULL DEFAULT 3 CHECK(low_stock_threshold >= 0);
CREATE TABLE IF NOT EXISTS demo_stock_rules (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES demo_stores(id) ON DELETE CASCADE,
    sku TEXT NOT NULL,
    threshold INTEGER NOT NULL DEFAULT 3 CHECK(threshold >= 0),
    updated_at TEXT NOT NULL,
    UNIQUE(store_id, sku)
);
