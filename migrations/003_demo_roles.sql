CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('manager','client')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS demo_stores (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'GBP',
    share TEXT NOT NULL DEFAULT '0',
    loss_sharing INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS demo_user_stores (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    store_id INTEGER NOT NULL REFERENCES demo_stores(id) ON DELETE CASCADE,
    PRIMARY KEY(user_id, store_id)
);
CREATE TABLE IF NOT EXISTS demo_orders (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES demo_stores(id) ON DELETE CASCADE,
    order_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    currency TEXT NOT NULL,
    proceeds TEXT NOT NULL,
    tax TEXT NOT NULL,
    refunds TEXT NOT NULL,
    fees TEXT NOT NULL,
    status TEXT NOT NULL,
    source_json TEXT NOT NULL,
    UNIQUE(store_id, order_id)
);
CREATE INDEX IF NOT EXISTS idx_demo_orders_store_date ON demo_orders(store_id, created_at);
CREATE TABLE IF NOT EXISTS demo_costs (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES demo_stores(id) ON DELETE CASCADE,
    order_id TEXT,
    sku TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('product','fulfilment','other')),
    amount TEXT NOT NULL,
    currency TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_demo_costs_store_order ON demo_costs(store_id, order_id);
