"""Add the reports and reconciliation_runs tables required by build.md Sec. 12.

Two more tables beyond Figure 3.5, each forced by a reporting requirement the
six entities cannot satisfy:

  reports              Sec. 12 / Sec. 3.4.2 - "store under a report ID in
                       Postgres", "persist every generated report with its ID
                       for provenance".

  reconciliation_runs  GET /metrics/kpi must report reconciliation status and
                       latency. matchedrecords records what matched, never when
                       a pass ran or how long it took, so those KPIs would
                       otherwise have to be invented.

Mirrors the blocks appended to db/schema.sql. IF NOT EXISTS throughout because
0001 replays schema.sql, which already contains them.

Revision ID: 0003_reports_runs
Revises: 0002_quarantine
"""

from __future__ import annotations

from alembic import op

revision: str = "0003_reports_runs"
down_revision: str | None = "0002_quarantine"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS reports (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name           TEXT NOT NULL,
            report_type    TEXT NOT NULL,
            period_start   DATE,
            period_end     DATE,
            generated_by   TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'READY',
            size_bytes     INTEGER,
            content        BYTEA,
            parameters     JSONB,
            summary        JSONB,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_reports_created ON reports (created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_reports_type "
        "ON reports (report_type, created_at DESC)"
    )

    op.execute(
        """
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
            status         TEXT NOT NULL DEFAULT 'COMPLETED',
            error          TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_runs_started "
        "ON reconciliation_runs (started_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reconciliation_runs CASCADE")
    op.execute("DROP TABLE IF EXISTS reports CASCADE")
