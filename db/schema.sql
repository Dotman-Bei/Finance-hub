-- FinanceHub canonical schema (build.md Sec. 5, ER diagram Figure 3.5).
-- Six entities: TRANSACTIONS, MATCHEDRECORDS, EXCEPTIONQUEUE, LEDGERENTRIES,
-- VALIDATIONLOGS, AUDITTRAIL.
--
-- Written to be re-runnable. docker-compose mounts this into Postgres'
-- entrypoint so a fresh volume is built automatically, and the alembic
-- baseline revision executes the same file -- running both must not fail.
--
-- ASCII only, deliberately: this file is piped through psql, the Docker
-- entrypoint and `alembic upgrade --sql` on Windows consoles that default to
-- cp1252. A single box-drawing character breaks all three.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ---------------------------------------------------------------------------
-- Enumerated types
-- CREATE TYPE has no IF NOT EXISTS, so each is guarded.
-- ---------------------------------------------------------------------------
DO $$ BEGIN
    CREATE TYPE match_status AS ENUM ('MATCHED', 'UNMATCHED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE match_type AS ENUM ('RULE', 'ML');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE exception_cat AS ENUM ('PARTIAL_PAYMENT', 'SPLIT_SETTLEMENT',
                                       'MISSING_REFERENCE_CODE', 'TIMING_DIFFERENCE');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE exception_state AS ENUM ('OPEN', 'SUGGESTED', 'RESOLVED', 'REJECTED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE validation_state AS ENUM ('PASSED', 'QUARANTINED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------------------------
-- Clean, validated financial records (single source of truth)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transactions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id     TEXT,
    source_type     TEXT NOT NULL,               -- bank_api | payment_gateway | erp
    amount          NUMERIC(18,2) NOT NULL,
    currency        CHAR(3) NOT NULL DEFAULT 'USD',
    txn_date        DATE NOT NULL,
    description     TEXT,
    reference_code  TEXT,
    raw_payload     JSONB,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_txn_ref    ON transactions (reference_code);
CREATE INDEX IF NOT EXISTS idx_txn_amount ON transactions (amount, txn_date);

-- ---------------------------------------------------------------------------
-- Confirmed pairs from the matching engine
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS matchedrecords (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id    UUID NOT NULL REFERENCES transactions(id),
    counterpart_id    UUID NOT NULL REFERENCES transactions(id),
    match_type        match_type  NOT NULL,
    confidence_score  NUMERIC(5,4) NOT NULL,
    matched_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_match_txn   ON matchedrecords (transaction_id);
CREATE INDEX IF NOT EXISTS idx_match_dates ON matchedrecords (matched_at);

-- ---------------------------------------------------------------------------
-- Unmatched items awaiting triage / resolution
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exceptionqueue (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id       UUID NOT NULL REFERENCES transactions(id),
    category             exception_cat,
    state                exception_state NOT NULL DEFAULT 'OPEN',
    classifier_confidence NUMERIC(5,4),
    suggested_resolution JSONB,
    resolved_by          TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at          TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_exc_state    ON exceptionqueue (state);
CREATE INDEX IF NOT EXISTS idx_exc_category ON exceptionqueue (category);
CREATE INDEX IF NOT EXISTS idx_exc_created  ON exceptionqueue (created_at);

-- ---------------------------------------------------------------------------
-- Posted ledger entries derived from matched/resolved records
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ledgerentries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id  UUID NOT NULL REFERENCES transactions(id),
    entry_type      TEXT NOT NULL,               -- debit | credit
    amount          NUMERIC(18,2) NOT NULL,
    posted_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ledger_txn ON ledgerentries (transaction_id);

-- ---------------------------------------------------------------------------
-- Every ingestion decision (pass or quarantine) for full traceability
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS validationlogs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id  UUID REFERENCES transactions(id),   -- null if quarantined pre-insert
    stage           TEXT NOT NULL,               -- schema | business_rule | checksum
    status          validation_state NOT NULL,
    violations      JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_val_status ON validationlogs (status, created_at);

-- ---------------------------------------------------------------------------
-- Immutable, tamper-evident action log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audittrail (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type  TEXT NOT NULL,
    entity_id    UUID NOT NULL,
    action       TEXT NOT NULL,
    actor        TEXT NOT NULL,                  -- system | <username>
    old_state    JSONB,
    new_state    JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audittrail (entity_type, entity_id);

-- ---------------------------------------------------------------------------
-- Quarantine (Sec. 8). Beyond the six entities of Figure 3.5, but the
-- validation pipeline requires it: Sec. 8 routes failures to "the quarantine
-- schema partition" AND to validationlogs, which are two different writes.
-- validationlogs records WHY a record was rejected; this holds WHAT was
-- rejected, verbatim, so it can be inspected and replayed after a fix.
--
-- Implemented as a dedicated table rather than a native range partition.
-- Adding PARTITION BY RANGE (quarantined_at) later needs no write-path change.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quarantine (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id         TEXT,
    source_type         TEXT,
    stage               TEXT NOT NULL,              -- schema | business_rule | checksum

    payload             JSONB NOT NULL,             -- the rejected record, verbatim
    violations          JSONB NOT NULL,
    payload_fingerprint TEXT NOT NULL,              -- sha256, also the Redis cache key
    quarantined_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    replayed_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_quar_stage       ON quarantine (stage, quarantined_at);
CREATE INDEX IF NOT EXISTS idx_quar_fingerprint ON quarantine (payload_fingerprint);
CREATE INDEX IF NOT EXISTS idx_quar_unreplayed  ON quarantine (quarantined_at)
    WHERE replayed_at IS NULL;

-- ---------------------------------------------------------------------------
-- Reports (Sec. 12). Beyond Figure 3.5, required by the reporting API:
-- "produce an audit-ready PDF, store under a report ID in Postgres" and
-- "Persist every generated report with its ID for provenance" (Sec. 3.4.2).
--
-- The PDF bytes live in the row. Reconciliation reports are small (hundreds of
-- KB) and an audit trail that can lose its evidence to a detached object store
-- is not an audit trail.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reports (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name           TEXT NOT NULL,
    report_type    TEXT NOT NULL,   -- RECONCILIATION_SUMMARY | EXCEPTION_LOG | ...
    period_start   DATE,
    period_end     DATE,
    generated_by   TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'READY',
    size_bytes     INTEGER,
    content        BYTEA,           -- the rendered PDF
    parameters     JSONB,           -- what was requested
    summary        JSONB,           -- the figures as at generation time
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reports_created ON reports (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_type    ON reports (report_type, created_at DESC);

-- ---------------------------------------------------------------------------
-- Reconciliation runs (Sec. 12). Beyond Figure 3.5. GET /metrics/kpi has to
-- report "reconciliation status" and latency, and neither can be derived from
-- the six entities -- matchedrecords records what was matched, never when a
-- pass ran, how long it took, or whether it completed.
--
-- Without this table those KPIs could only be invented, which is not an
-- option. The matching engine writes one row per pass.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at   TIMESTAMPTZ,
    duration_ms    NUMERIC(12,2),
    total_input    INTEGER NOT NULL DEFAULT 0,
    matched        INTEGER NOT NULL DEFAULT 0,
    unmatched      INTEGER NOT NULL DEFAULT 0,
    rule_matched   INTEGER NOT NULL DEFAULT 0,
    ml_matched     INTEGER NOT NULL DEFAULT 0,
    match_rate     NUMERIC(5,4),
    threshold      NUMERIC(5,4),
    status         TEXT NOT NULL DEFAULT 'COMPLETED',   -- COMPLETED | FAILED
    error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON reconciliation_runs (started_at DESC);

-- ---------------------------------------------------------------------------
-- Trigger: auto-log every exception state change to audittrail (Sec. 3.3.2)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION log_exception_change() RETURNS trigger AS $$
BEGIN
    INSERT INTO audittrail (entity_type, entity_id, action, actor, old_state, new_state)
    VALUES ('exceptionqueue', NEW.id, TG_OP, COALESCE(NEW.resolved_by, 'system'),
            to_jsonb(OLD), to_jsonb(NEW));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_exception_audit ON exceptionqueue;
CREATE TRIGGER trg_exception_audit
    AFTER UPDATE ON exceptionqueue
    FOR EACH ROW EXECUTE FUNCTION log_exception_change();
