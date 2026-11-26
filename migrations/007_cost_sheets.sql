-- Order costs imported from a per-store Google Sheet (AliExpress / supplier order log).
-- source='sheet' rows are replaced on every re-import; manual rows have source NULL.
ALTER TABLE demo_costs ADD COLUMN source TEXT;
ALTER TABLE demo_stores ADD COLUMN cost_sheet_url TEXT;
ALTER TABLE demo_stores ADD COLUMN cost_sheet_at TEXT;
ALTER TABLE demo_stores ADD COLUMN cost_sheet_report TEXT;
CREATE INDEX IF NOT EXISTS idx_demo_costs_source ON demo_costs(store_id, source);
