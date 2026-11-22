CREATE TABLE IF NOT EXISTS demo_share_history (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES demo_stores(id) ON DELETE CASCADE,
    percent TEXT NOT NULL,
    loss_sharing INTEGER NOT NULL DEFAULT 0 CHECK(loss_sharing IN (0,1)),
    effective_at TEXT NOT NULL,
    UNIQUE(store_id, effective_at)
);
INSERT OR IGNORE INTO demo_share_history(store_id,percent,loss_sharing,effective_at)
SELECT id,share,loss_sharing,'1970-01-01T00:00:00+00:00' FROM demo_stores;
