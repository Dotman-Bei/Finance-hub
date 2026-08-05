"""SQLAlchemy mappings for the six entities in db/schema.sql (build.md §5).

The SQL file owns the DDL — these classes map onto it, they do not generate it.
Enum columns therefore use `create_type=False`: Postgres already has the types,
and letting SQLAlchemy emit CREATE TYPE would fight the schema.

`shared/tests/test_orm_matches_schema.py` compiles every table against the
Postgres dialect and diffs it with schema.sql, so drift fails a test rather
than surfacing at runtime.

Note the name collision: `orm.Transaction` is the database row, while
`shared.models.Transaction` is the Pydantic wire model. This module is
deliberately not re-exported from `shared.models` — import it explicitly
(`from shared.models import orm`) so which one you mean is always visible.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .enums import ExceptionCategory, ExceptionState, MatchType, ValidationState


class Base(DeclarativeBase):
    pass


def _pg_enum(python_enum, name: str) -> PGEnum:
    """Bind a Python enum to an existing Postgres type by its *values*."""
    return PGEnum(
        python_enum,
        name=name,
        create_type=False,
        values_callable=lambda e: [member.value for member in e],
    )


_UUID_PK = dict(
    primary_key=True,
    server_default=text("gen_random_uuid()"),
    default=uuid.uuid4,
)
_NOW = text("now()")


class Transaction(Base):
    """Clean, validated financial records — the single source of truth."""

    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    external_id: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default=text("'USD'"))
    txn_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    reference_code: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB)
    ingested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )

    __table_args__ = (
        Index("idx_txn_ref", "reference_code"),
        Index("idx_txn_amount", "amount", "txn_date"),
    )


class MatchedRecord(Base):
    """Confirmed pairs from the matching engine (§9)."""

    __tablename__ = "matchedrecords"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id"), nullable=False
    )
    counterpart_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id"), nullable=False
    )
    match_type: Mapped[MatchType] = mapped_column(
        _pg_enum(MatchType, "match_type"), nullable=False
    )
    confidence_score: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    matched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )

    # Two FKs to the same table, so each side names its column explicitly.
    transaction: Mapped[Transaction] = relationship(foreign_keys=[transaction_id])
    counterpart: Mapped[Transaction] = relationship(foreign_keys=[counterpart_id])

    __table_args__ = (
        Index("idx_match_txn", "transaction_id"),
        Index("idx_match_dates", "matched_at"),
    )


class ExceptionQueue(Base):
    """Unmatched items awaiting triage / resolution (§10)."""

    __tablename__ = "exceptionqueue"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id"), nullable=False
    )
    category: Mapped[ExceptionCategory | None] = mapped_column(
        _pg_enum(ExceptionCategory, "exception_cat")
    )
    state: Mapped[ExceptionState] = mapped_column(
        _pg_enum(ExceptionState, "exception_state"),
        nullable=False,
        server_default=text("'OPEN'"),
    )
    classifier_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    suggested_resolution: Mapped[dict | None] = mapped_column(JSONB)
    resolved_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    transaction: Mapped[Transaction] = relationship()

    __table_args__ = (
        Index("idx_exc_state", "state"),
        Index("idx_exc_category", "category"),
        Index("idx_exc_created", "created_at"),
    )


class LedgerEntry(Base):
    """Posted entries derived from matched/resolved records."""

    __tablename__ = "ledgerentries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id"), nullable=False
    )
    entry_type: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    posted_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )

    transaction: Mapped[Transaction] = relationship()

    __table_args__ = (Index("idx_ledger_txn", "transaction_id"),)


class ValidationLog(Base):
    """Every ingestion decision, pass or quarantine (§8).

    `transaction_id` is nullable because a record quarantined before insert has
    no row in `transactions` to point at.
    """

    __tablename__ = "validationlogs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id")
    )
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ValidationState] = mapped_column(
        _pg_enum(ValidationState, "validation_state"), nullable=False
    )
    violations: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )

    __table_args__ = (Index("idx_val_status", "status", "created_at"),)


class AuditTrail(Base):
    """Immutable action log.

    Rows are written by the `trg_exception_audit` trigger, not by application
    code. Treat this table as append-only — never UPDATE or DELETE from a
    service, or the tamper-evidence property of §3.3.2 is lost.
    """

    __tablename__ = "audittrail"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    old_state: Mapped[dict | None] = mapped_column(JSONB)
    new_state: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )

    __table_args__ = (Index("idx_audit_entity", "entity_type", "entity_id"),)


class Quarantine(Base):
    """Rejected payloads held for inspection and replay (§8).

    Beyond Figure 3.5's six entities. `validationlogs` records *why* a record
    was rejected; this holds *what* was rejected, verbatim, so a feed can be
    corrected and the batch replayed rather than lost.
    """

    __tablename__ = "quarantine"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    external_id: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str | None] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    violations: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    quarantined_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )
    replayed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_quar_stage", "stage", "quarantined_at"),
        Index("idx_quar_fingerprint", "payload_fingerprint"),
        Index(
            "idx_quar_unreplayed",
            "quarantined_at",
            postgresql_where=text("replayed_at IS NULL"),
        ),
    )


class Report(Base):
    """Generated audit-ready PDFs (§12, §3.4.2). Beyond Figure 3.5.

    The rendered bytes live in `content`. An audit trail whose evidence can be
    lost to a detached object store is not an audit trail.
    """

    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    report_type: Mapped[str] = mapped_column(Text, nullable=False)
    period_start: Mapped[dt.date | None] = mapped_column(Date)
    period_end: Mapped[dt.date | None] = mapped_column(Date)
    generated_by: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'READY'")
    )
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[bytes | None] = mapped_column(LargeBinary)
    parameters: Mapped[dict | None] = mapped_column(JSONB)
    summary: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )

    __table_args__ = (
        Index("idx_reports_created", "created_at"),
        Index("idx_reports_type", "report_type", "created_at"),
    )


class ReconciliationRun(Base):
    """One matching-engine pass (§12). Beyond Figure 3.5.

    `matchedrecords` records *what* matched but never when a pass ran or how
    long it took, so GET /metrics/kpi could not report reconciliation status or
    latency from the six entities alone.
    """

    __tablename__ = "reconciliation_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    total_input: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    matched: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    unmatched: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    rule_matched: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    ml_matched: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    match_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    threshold: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'COMPLETED'")
    )
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("idx_runs_started", "started_at"),)


#: The entities of the thesis ER diagram (Figure 3.5).
CORE_TABLES = frozenset(
    {
        "transactions",
        "matchedrecords",
        "exceptionqueue",
        "ledgerentries",
        "validationlogs",
        "audittrail",
    }
)

#: Tables the implementation requires beyond Figure 3.5, with the section that
#: demands each. Keep this annotated -- it is the record of every deviation.
EXTENSION_TABLES = {
    "quarantine": "build.md §8 - quarantine schema partition",
    "reports": "build.md §12/§3.4.2 - persist every generated report by ID",
    "reconciliation_runs": "build.md §12 - KPI needs run status and latency",
}


__all__ = [
    "Base",
    "Transaction",
    "MatchedRecord",
    "ExceptionQueue",
    "LedgerEntry",
    "ValidationLog",
    "AuditTrail",
    "Quarantine",
    "Report",
    "ReconciliationRun",
    "CORE_TABLES",
    "EXTENSION_TABLES",
]
