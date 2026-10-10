CREATE TABLE usage_ledger (
    event_id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    user_id UUID,
    kind TEXT NOT NULL,
    backend TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd NUMERIC(14,8) NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    cached BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (event_id, created_at)
) PARTITION BY RANGE (created_at);

CREATE INDEX usage_ledger_tenant_created_idx ON usage_ledger (tenant_id, created_at);

CREATE TABLE usage_ledger_default PARTITION OF usage_ledger DEFAULT;

CREATE OR REPLACE FUNCTION ensure_usage_partitions(start_month DATE, months INTEGER)
RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE
    m DATE;
    created INTEGER := 0;
    part TEXT;
BEGIN
    FOR i IN 0..months - 1 LOOP
        m := (date_trunc('month', start_month) + make_interval(months => i))::date;
        part := 'usage_ledger_' || to_char(m, 'YYYY_MM');
        IF to_regclass(part) IS NULL THEN
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF usage_ledger FOR VALUES FROM (%L) TO (%L)',
                part, m, (m + interval '1 month')::date
            );
            created := created + 1;
        END IF;
    END LOOP;
    RETURN created;
END;
$$;

SELECT ensure_usage_partitions((date_trunc('month', now()) - interval '2 months')::date, 6);
