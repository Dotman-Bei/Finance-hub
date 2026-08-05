"""Phase 0 gate: the SQLAlchemy models must describe db/schema.sql exactly.

schema.sql owns the DDL and the ORM maps onto it, so nothing at runtime forces
the two to agree — this test does. It parses the SQL directly rather than
trusting a copy, and compiles every table against the real Postgres dialect so
type errors surface without a live server.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from shared.models.orm import CORE_TABLES, EXTENSION_TABLES, Base

SCHEMA_SQL = Path(__file__).resolve().parents[2] / "db" / "schema.sql"

# Line prefixes inside a CREATE TABLE body that are constraints, not columns.
_NOT_A_COLUMN = ("primary", "foreign", "unique", "check", "constraint", "--")


@pytest.fixture(scope="module")
def schema_text() -> str:
    assert SCHEMA_SQL.exists(), f"missing {SCHEMA_SQL}"
    return SCHEMA_SQL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def sql_tables(schema_text: str) -> dict[str, set[str]]:
    """{table_name: {column names}} parsed straight out of schema.sql."""
    tables: dict[str, set[str]] = {}
    pattern = re.compile(
        r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)\s*\((.*?)\n\);",
        re.DOTALL | re.IGNORECASE,
    )

    for name, body in pattern.findall(schema_text):
        columns = set()
        for raw in body.splitlines():
            line = raw.strip()
            if not line or line.lower().startswith(_NOT_A_COLUMN):
                continue
            columns.add(line.split()[0].strip('",'))
        tables[name.lower()] = columns

    return tables


# ── Tables ───────────────────────────────────────────────────────────────

EXPECTED_TABLES = CORE_TABLES | set(EXTENSION_TABLES)


def test_schema_declares_the_six_entities_of_figure_3_5(sql_tables):
    """The ER diagram's six must all be present and none renamed."""
    assert CORE_TABLES <= set(sql_tables)


def test_every_extra_table_is_a_documented_deviation(sql_tables):
    """Any table beyond Figure 3.5 must be justified in EXTENSION_TABLES,
    so deviations from the thesis schema stay visible rather than accruing."""
    extras = set(sql_tables) - CORE_TABLES
    assert extras == set(EXTENSION_TABLES), (
        f"undocumented tables: {sorted(extras - set(EXTENSION_TABLES))}"
    )


def test_orm_maps_exactly_the_tables_in_schema_sql(sql_tables):
    assert set(Base.metadata.tables) == set(sql_tables)


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_orm_columns_match_sql_columns(table_name, sql_tables):
    orm_columns = {c.name for c in Base.metadata.tables[table_name].columns}
    sql_columns = sql_tables[table_name]

    assert orm_columns == sql_columns, (
        f"{table_name} drifted — "
        f"only in ORM: {sorted(orm_columns - sql_columns)}, "
        f"only in SQL: {sorted(sql_columns - orm_columns)}"
    )


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_every_table_compiles_for_postgres(table_name):
    """Catches unmappable types without needing a database."""
    ddl = str(CreateTable(Base.metadata.tables[table_name]).compile(
        dialect=postgresql.dialect()
    ))
    assert f"CREATE TABLE {table_name}" in ddl


# ── Enum types ───────────────────────────────────────────────────────────

EXPECTED_TYPES = {
    "match_status",
    "match_type",
    "exception_cat",
    "exception_state",
    "validation_state",
}


def test_schema_declares_all_five_enum_types(schema_text):
    declared = set(re.findall(r"CREATE TYPE (\w+) AS ENUM", schema_text, re.IGNORECASE))
    assert declared == EXPECTED_TYPES


def test_orm_never_emits_create_type(schema_text):
    """schema.sql owns the types. If the ORM tried to create them too, a
    migration against an initialised database would fail on duplicate_object."""
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, postgresql.ENUM):
                assert column.type.create_type is False, (
                    f"{table.name}.{column.name} would emit CREATE TYPE"
                )


def test_enum_columns_reference_types_that_exist(schema_text):
    declared = set(re.findall(r"CREATE TYPE (\w+) AS ENUM", schema_text, re.IGNORECASE))
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, postgresql.ENUM):
                assert column.type.name in declared, (
                    f"{table.name}.{column.name} uses undeclared type "
                    f"{column.type.name}"
                )


# ── Constraints the pipeline depends on ──────────────────────────────────


def test_validationlogs_transaction_id_is_nullable():
    """A record quarantined before insert has no transactions row to point at
    (§8), so this column must stay nullable or the pipeline cannot log it."""
    column = Base.metadata.tables["validationlogs"].columns["transaction_id"]
    assert column.nullable is True


def test_exceptionqueue_transaction_id_is_not_nullable():
    column = Base.metadata.tables["exceptionqueue"].columns["transaction_id"]
    assert column.nullable is False


def test_matchedrecords_has_two_foreign_keys_to_transactions():
    table = Base.metadata.tables["matchedrecords"]
    targets = [list(c.foreign_keys)[0].target_fullname for c in table.columns if c.foreign_keys]
    assert sorted(targets) == ["transactions.id", "transactions.id"]


def test_audit_trigger_is_installed_on_exceptionqueue(schema_text):
    assert "CREATE TRIGGER trg_exception_audit" in schema_text
    assert "AFTER UPDATE ON exceptionqueue" in schema_text


def test_schema_is_ascii_only(schema_text):
    """This file is piped through psql, Docker's entrypoint and
    `alembic upgrade --sql`. On a Windows cp1252 console a single non-ASCII
    character raises UnicodeEncodeError and the migration dies mid-run."""
    offenders = {c for c in schema_text if ord(c) > 127}
    assert not offenders, (
        "schema.sql must stay ASCII-only; found code points "
        f"{sorted(hex(ord(c)) for c in offenders)}"
    )


def test_quarantine_holds_what_validationlogs_cannot(sql_tables):
    """§8 writes a failure to both. The pair is only useful if quarantine keeps
    the payload verbatim and both are non-null."""
    table = Base.metadata.tables["quarantine"]
    assert table.columns["payload"].nullable is False
    assert table.columns["violations"].nullable is False
    assert table.columns["payload_fingerprint"].nullable is False
    # Replay is the point of keeping it; the column must start empty.
    assert table.columns["replayed_at"].nullable is True


def test_schema_is_rerunnable(schema_text):
    """Postgres' entrypoint and the alembic baseline both apply this file."""
    assert schema_text.count("CREATE TABLE IF NOT EXISTS") == len(EXPECTED_TABLES)
    assert "DROP TRIGGER IF EXISTS trg_exception_audit" in schema_text
    for type_name in EXPECTED_TYPES:
        assert f"CREATE TYPE {type_name}" in schema_text
    # Every CREATE TYPE sits inside a duplicate_object guard.
    assert schema_text.count("EXCEPTION WHEN duplicate_object") == len(EXPECTED_TYPES)
