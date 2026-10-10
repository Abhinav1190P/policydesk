CREATE INDEX IF NOT EXISTS usage_ledger_created_brin ON usage_ledger USING brin (created_at) WITH (pages_per_range = 32);
