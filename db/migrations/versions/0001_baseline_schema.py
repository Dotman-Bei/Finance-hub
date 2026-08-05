"""Baseline: the six entities of build.md Sec. 5.

This revision executes db/schema.sql rather than restating the DDL in Python.
schema.sql stays the one readable, canonical definition (and the file Postgres'
entrypoint applies on first boot); alembic simply replays it so a database
built purely by `alembic upgrade head` is identical.

schema.sql is written to be re-runnable, so this is safe on a database the
entrypoint already initialised: it becomes a no-op and only stamps the
revision.

ASCII only. Alembic echoes this docstring when running, and a cp1252 console
mangles anything else.

Revision ID: 0001_baseline
Revises:
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

SCHEMA_SQL = Path(__file__).resolve().parents[2] / "schema.sql"

# Reverse dependency order: children before parents, types after tables.
TABLES = [
    "audittrail",
    "validationlogs",
    "ledgerentries",
    "exceptionqueue",
    "matchedrecords",
    "transactions",
]

TYPES = [
    "validation_state",
    "exception_state",
    "exception_cat",
    "match_type",
    "match_status",
]


def upgrade() -> None:
    sql = SCHEMA_SQL.read_text(encoding="utf-8")
    if not sql.strip():
        raise RuntimeError(f"{SCHEMA_SQL} is empty — cannot build the baseline schema")
    op.execute(sql)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_exception_audit ON exceptionqueue")
    op.execute("DROP FUNCTION IF EXISTS log_exception_change()")
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for type_name in TYPES:
        op.execute(f"DROP TYPE IF EXISTS {type_name}")
