-- Listings (eBay Trading API GetMyeBaySelling) and 30-day traffic (Analytics traffic_report by LISTING).
CREATE TABLE IF NOT EXISTS demo_listings (
    store_id INTEGER NOT NULL REFERENCES demo_stores(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    title TEXT,
    price TEXT,
    currency TEXT,
    quantity INTEGER,
    quantity_available INTEGER,
    quantity_sold INTEGER,
    watch_count INTEGER,
    has_variations INTEGER NOT NULL DEFAULT 0,
    start_time TEXT,
    view_url TEXT,
    image_url TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(store_id, item_id)
);
CREATE TABLE IF NOT EXISTS demo_listing_traffic (
    store_id INTEGER NOT NULL REFERENCES demo_stores(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    days INTEGER NOT NULL,
    impressions INTEGER,
    views INTEGER,
    transactions INTEGER,
    ctr REAL,
    conversion REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(store_id, item_id)
);
