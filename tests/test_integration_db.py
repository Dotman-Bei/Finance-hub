"""Integration tests against a live PostgreSQL (build.md Sec. 16, Phase 6).

Everything else in this suite runs offline, because the pipelines were built
I/O-free. These cover what only a real database can prove:

* the schema applies, and applies twice without error
* `trg_exception_audit` actually fires - Sec. 3.3.2's tamper-evidence rests
  entirely on the trigger, and "the trigger is in the DDL" is not the same
  claim as "the trigger writes a row"
* persistence round-trips: matched pairs, ledger postings, exception rows
* the metric aggregations compute what the dashboard reads
* **the audit-report gate**: every settled exception left an audit row

Skipped automatically when no database is reachable, so a developer without
Docker still gets a green local run. CI provides Postgres and runs them - if
these silently never ran anywhere, the trigger would be untested for the life
of the project, which is precisely the gap this file exists to close.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from shared.models.enums import (
    ExceptionCategory,
    ExceptionState,
    MatchType,
    ValidationState,
)
from shared.models.orm import (
    AuditTrail,
    ExceptionQueue,
    LedgerEntry,
    MatchedRecord,
    Quarantine,
    ReconciliationRun,
    Report,
    Transaction,
    ValidationLog,
)

pytestmark = pytest.mark.integration

DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    os.getenv("DATABASE_URL", "postgresql+psycopg2://financehub:changeme@localhost:5432/financehub"),
)

SCHEMA_SQL = os.path.join(os.path.dirname(os.path.dirname(__file__)), "db", "schema.sql")


def _reachable(url: str) -> bool:
    try:
        engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


pytest.importorskip("psycopg2")

if not _reachable(DATABASE_URL):
    pytest.skip(
        f"No PostgreSQL at {DATABASE_URL.split('@')[-1]} — integration tests skipped. "
        "Run `docker compose up -d postgres` to exercise them.",
        allow_module_level=True,
    )


@pytest.fixture(scope="module")
def engine():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    with engine.begin() as conn:
        with open(SCHEMA_SQL, encoding="utf-8") as fh:
            conn.execute(text(fh.read()))
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    """A session whose writes are rolled back, so tests cannot see each other."""
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.rollback()
    session.close()


def _txn(session, **overrides) -> Transaction:
    row = Transaction(
        external_id=overrides.pop("external_id", f"TXN-{uuid.uuid4().hex[:8]}"),
        source_type=overrides.pop("source_type", "erp"),
        amount=overrides.pop("amount", 1000.00),
        currency=overrides.pop("currency", "USD"),
        txn_date=overrides.pop("txn_date", dt.date.today()),
        description=overrides.pop("description", "Meridian Capital Ltd - settlement"),
        reference_code=overrides.pop("reference_code", "REF-10001"),
        **overrides,
    )
    session.add(row)
    session.flush()
    return row


# ── Schema ───────────────────────────────────────────────────────────────


def test_schema_applies_twice_without_error(engine):
    """Postgres' entrypoint and the alembic baseline both apply this file, so
    a second run must be a no-op rather than a duplicate_object failure."""
    with engine.begin() as conn:
        with open(SCHEMA_SQL, encoding="utf-8") as fh:
            conn.execute(text(fh.read()))


def test_all_nine_tables_exist(engine):
    expected = {
        "transactions", "matchedrecords", "exceptionqueue", "ledgerentries",
        "validationlogs", "audittrail", "quarantine", "reports",
        "reconciliation_runs",
    }
    with engine.connect() as conn:
        actual = {
            row[0]
            for row in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        }
    assert expected <= actual, f"missing: {sorted(expected - actual)}"


def test_the_audit_trigger_is_installed(engine):
    with engine.connect() as conn:
        triggers = {
            row[0]
            for row in conn.execute(
                text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
            )
        }
    assert "trg_exception_audit" in triggers


def test_amount_precision_survives_a_round_trip(session):
    """NUMERIC(18,2) must not silently round a settlement."""
    row = _txn(session, amount=123456789.99)
    session.flush()
    session.expire(row)
    assert float(row.amount) == 123456789.99


# ── The audit trigger (Sec. 3.3.2) ───────────────────────────────────────


def test_updating_an_exception_writes_an_audit_row(session):
    """The claim tamper-evidence rests on. Application code never writes to
    audittrail, so if the trigger does not fire, nothing is logged and nobody
    finds out."""
    txn = _txn(session)
    exception = ExceptionQueue(transaction_id=txn.id, state=ExceptionState.OPEN)
    session.add(exception)
    session.flush()

    before = session.scalar(
        select(AuditTrail).where(AuditTrail.entity_id == exception.id)
    )
    assert before is None, "insert should not produce an audit row"

    exception.state = ExceptionState.RESOLVED
    exception.resolved_by = "a.okafor@financehub.io"
    session.flush()

    audit = session.scalars(
        select(AuditTrail).where(AuditTrail.entity_id == exception.id)
    ).all()

    assert len(audit) == 1
    assert audit[0].entity_type == "exceptionqueue"
    assert audit[0].action == "UPDATE"
    assert audit[0].actor == "a.okafor@financehub.io"


def test_the_trigger_captures_both_states(session):
    """An audit row that cannot show what changed is not evidence."""
    txn = _txn(session)
    exception = ExceptionQueue(
        transaction_id=txn.id,
        state=ExceptionState.OPEN,
        category=ExceptionCategory.PARTIAL_PAYMENT,
    )
    session.add(exception)
    session.flush()

    exception.state = ExceptionState.RESOLVED
    exception.resolved_by = "auditor@financehub.io"
    session.flush()

    audit = session.scalar(select(AuditTrail).where(AuditTrail.entity_id == exception.id))
    assert audit.old_state["state"] == "OPEN"
    assert audit.new_state["state"] == "RESOLVED"


def test_actor_falls_back_to_system_when_unattributed(session):
    """`resolved_by` must be set *before* the state change or the trigger
    records the decision as the system's."""
    txn = _txn(session)
    exception = ExceptionQueue(transaction_id=txn.id, state=ExceptionState.OPEN)
    session.add(exception)
    session.flush()

    exception.state = ExceptionState.REJECTED   # no resolved_by
    session.flush()

    audit = session.scalar(select(AuditTrail).where(AuditTrail.entity_id == exception.id))
    assert audit.actor == "system"


def test_every_update_appends_rather_than_replacing(session):
    """Append-only: a second change must not overwrite the first."""
    txn = _txn(session)
    exception = ExceptionQueue(transaction_id=txn.id, state=ExceptionState.OPEN)
    session.add(exception)
    session.flush()

    exception.state = ExceptionState.SUGGESTED
    exception.resolved_by = "classifier"
    session.flush()
    exception.state = ExceptionState.RESOLVED
    exception.resolved_by = "human@financehub.io"
    session.flush()

    audit = session.scalars(
        select(AuditTrail).where(AuditTrail.entity_id == exception.id)
    ).all()
    assert len(audit) == 2


# ── Persistence round-trips ──────────────────────────────────────────────


def test_matched_records_and_ledger_postings_persist(session):
    internal = _txn(session, source_type="erp")
    external = _txn(session, source_type="bank_api")

    session.add(
        MatchedRecord(
            transaction_id=internal.id,
            counterpart_id=external.id,
            match_type=MatchType.RULE,
            confidence_score=1.0,
        )
    )
    session.add(LedgerEntry(transaction_id=internal.id, entry_type="debit", amount=1000.00))
    session.add(LedgerEntry(transaction_id=external.id, entry_type="credit", amount=1000.00))
    session.flush()

    entries = session.scalars(
        select(LedgerEntry).where(
            LedgerEntry.transaction_id.in_([internal.id, external.id])
        )
    ).all()
    # Double entry: debits and credits must balance.
    assert sum(float(e.amount) for e in entries if e.entry_type == "debit") == sum(
        float(e.amount) for e in entries if e.entry_type == "credit"
    )


def test_quarantine_keeps_the_payload_verbatim(session):
    payload = {"external_id": "BAD-1", "amount": -5, "currency": "ZZZ"}
    row = Quarantine(
        external_id="BAD-1",
        source_type="bank_api",
        stage="schema",
        payload=payload,
        violations={"violations": ["amount must be positive"]},
        payload_fingerprint="a" * 64,
    )
    session.add(row)
    session.flush()
    session.expire(row)

    # Replay is the point; a lossy copy would make it impossible.
    assert row.payload == payload
    assert row.replayed_at is None


def test_validation_log_allows_a_null_transaction(session):
    """A record quarantined before insert has no transactions row to point at."""
    session.add(
        ValidationLog(
            transaction_id=None,
            stage="schema",
            status=ValidationState.QUARANTINED,
            violations={"violations": ["missing amount"]},
        )
    )
    session.flush()


def test_reports_store_their_bytes(session):
    pdf = b"%PDF-1.4\ntest\n%%EOF"
    row = Report(
        name="Test report",
        report_type="RECONCILIATION_SUMMARY",
        generated_by="test@financehub.io",
        status="READY",
        size_bytes=len(pdf),
        content=pdf,
        parameters={"type": "RECONCILIATION_SUMMARY"},
    )
    session.add(row)
    session.flush()
    session.expire(row)
    assert bytes(row.content) == pdf


# ── Metric aggregation ───────────────────────────────────────────────────


def test_kpi_computes_from_real_rows(session):
    from services.reporting_api.app.metrics import kpi_summary

    internal = _txn(session, source_type="erp")
    external = _txn(session, source_type="bank_api")
    session.add(
        MatchedRecord(
            transaction_id=internal.id, counterpart_id=external.id,
            match_type=MatchType.ML, confidence_score=0.91,
        )
    )
    session.flush()

    kpi = kpi_summary(session)
    assert kpi["total_transactions"] >= 2
    assert kpi["reconciliation_status"] in {"HEALTHY", "ATTENTION", "UNKNOWN"}


def test_kpi_reports_null_not_zero_on_an_empty_window(session):
    """"No data" and "zero" are different facts; a dashboard showing 0% on an
    empty database is misleading."""
    from services.reporting_api.app.metrics import kpi_summary

    kpi = kpi_summary(session, window_days=1)
    if kpi["total_transactions"] == 0:
        assert kpi["match_rate"] is None


def test_reconciliation_runs_feed_the_status(session):
    from services.reporting_api.app.metrics import kpi_summary

    session.add(
        ReconciliationRun(
            started_at=dt.datetime.now(dt.timezone.utc),
            completed_at=dt.datetime.now(dt.timezone.utc),
            duration_ms=250.5, total_input=100, matched=45, unmatched=10,
            rule_matched=30, ml_matched=15, match_rate=0.90, threshold=0.85,
            status="COMPLETED",
        )
    )
    session.flush()

    kpi = kpi_summary(session)
    assert kpi["last_run_at"] is not None
    assert kpi["avg_reconcile_latency_ms"] is not None


# ── THE AUDIT-REPORT GATE (Sec. 16, Phase 6) ─────────────────────────────


def test_audit_integrity_gate(session):
    """Every settled exception must have left an audit row.

    This is Sec. 16's audit gate. A gap means the trigger is missing or a write
    bypassed it — either way the tamper-evidence claim in Sec. 3.3.2 is false,
    and nothing else in the system would reveal it.
    """
    from services.reporting_api.app.audit import integrity_report

    for state in (ExceptionState.RESOLVED, ExceptionState.REJECTED):
        txn = _txn(session)
        exception = ExceptionQueue(transaction_id=txn.id, state=ExceptionState.OPEN)
        session.add(exception)
        session.flush()
        exception.state = state
        exception.resolved_by = "gate@financehub.io"
        session.flush()

    report = integrity_report(session)

    assert report["complete"] is True, (
        f"{report['missing_count']} settled exceptions have no audit row: "
        f"{report['missing_sample']}"
    )
    assert report["coverage"] == 1.0
    assert report["audit_rows"] >= 2


def test_audit_trail_is_queryable_and_diffed(session):
    from services.reporting_api.app.audit import query_trail

    txn = _txn(session)
    exception = ExceptionQueue(transaction_id=txn.id, state=ExceptionState.OPEN)
    session.add(exception)
    session.flush()
    exception.state = ExceptionState.RESOLVED
    exception.resolved_by = "reviewer@financehub.io"
    session.flush()

    trail = query_trail(session, entity_type="exceptionqueue", limit=10)
    assert trail["count"] >= 1

    entry = next(i for i in trail["items"] if i["entity_id"] == str(exception.id))
    changed = {c["field"] for c in entry["changed_fields"]}
    assert "state" in changed


def test_audit_actor_activity(session):
    from services.reporting_api.app.audit import actor_activity

    txn = _txn(session)
    exception = ExceptionQueue(transaction_id=txn.id, state=ExceptionState.OPEN)
    session.add(exception)
    session.flush()
    exception.state = ExceptionState.RESOLVED
    exception.resolved_by = "named.actor@financehub.io"
    session.flush()

    actors = {row["actor"] for row in actor_activity(session)}
    assert "named.actor@financehub.io" in actors
