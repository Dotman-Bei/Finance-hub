"""Add the quarantine partition required by build.md Sec. 8.

The six entities of Figure 3.5 have nowhere to hold a *rejected* payload.
Sec. 8 routes a failure to "the quarantine schema partition" AND to
validationlogs -- two writes, two purposes. validationlogs says why a record
was rejected; quarantine holds the record itself so it can be replayed once
the upstream feed is fixed.

Mirrors the block appended to db/schema.sql, so a database built from either
path ends up identical. Written with IF NOT EXISTS because 0001 replays
schema.sql, which already contains this table.

Revision ID: 0002_quarantine
Revises: 0001_baseline
"""

from __future__ import annotations

from alembic import op

revision: str = "0002_quarantine"
down_revision: str | None = "0001_baseline"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS quarantine (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            external_id         TEXT,
            source_type         TEXT,
            stage               TEXT NOT NULL,
            payload             JSONB NOT NULL,
            violations          JSONB NOT NULL,
            payload_fingerprint TEXT NOT NULL,
            quarantined_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            replayed_at         TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_quar_stage "
        "ON quarantine (stage, quarantined_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_quar_fingerprint "
        "ON quarantine (payload_fingerprint)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_quar_unreplayed "
        "ON quarantine (quarantined_at) WHERE replayed_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quarantine CASCADE")
