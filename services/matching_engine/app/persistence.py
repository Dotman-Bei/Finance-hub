"""Writing reconciliation results to Postgres (build.md Sec. 9).

    "Matched -> matchedrecords (with match_type RULE or ML) and a corresponding
     ledgerentries posting. Unmatched -> exceptionqueue(state='OPEN')."

Not in Sec. 3's file list for this service; separated from pipeline.py so the
matching logic stays free of I/O and can be graded by test_precision.py
without a database, the same split the validation pipeline uses.

Ledger postings are double-entry: each confirmed pair yields two rows, a debit
against the internal book and a credit against the external receipt. Sec. 9
says "a corresponding ledgerentries posting" without fixing the count; one row
per pair would leave the ledger unbalanced, which no audit would accept.

Exception rows are written with `state='OPEN'` and no category. Classification
is Subsystem 3's job (Sec. 10) - guessing one here would poison the training
data the classifier learns from.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from shared.events import EventType, exception_created, publisher
from shared.models.enums import EntryType, ExceptionState, MatchType
from shared.models.orm import (
    ExceptionQueue,
    LedgerEntry,
    MatchedRecord,
    ReconciliationRun,
    Transaction,
)

from .pipeline import ReconcileResult

logger = logging.getLogger(__name__)


def persist_result(
    session: Session,
    result: ReconcileResult,
    post_ledger_entries: bool = True,
) -> dict[str, int]:
    """Write a reconciliation run. Commits once, or rolls back entirely.

    A partial write would leave matchedrecords without its ledger postings, so
    the reconciliation would look complete while the books did not balance.
    """
    amounts = _amount_index(session, result)

    matched_rows = 0
    ledger_rows = 0
    exception_rows = 0

    for pair in result.matched:
        session.add(
            MatchedRecord(
                transaction_id=pair.internal_id,
                counterpart_id=pair.external_id,
                match_type=(
                    MatchType.RULE if pair.match_type == "RULE" else MatchType.ML
                ),
                confidence_score=round(pair.confidence, 4),
            )
        )
        matched_rows += 1

        if not post_ledger_entries:
            continue

        internal_amount = amounts.get(pair.internal_id)
        external_amount = amounts.get(pair.external_id)
        if internal_amount is None or external_amount is None:
            # Refusing to post half an entry: an unbalanced ledger is worse
            # than a missing one, and the match itself is still recorded.
            logger.warning(
                "Skipping ledger posting for %s/%s - amount not found",
                pair.internal_id, pair.external_id,
            )
            continue

        session.add(
            LedgerEntry(
                transaction_id=pair.internal_id,
                entry_type=EntryType.DEBIT.value,
                amount=internal_amount,
            )
        )
        session.add(
            LedgerEntry(
                transaction_id=pair.external_id,
                entry_type=EntryType.CREDIT.value,
                amount=external_amount,
            )
        )
        ledger_rows += 2

    opened: list[tuple[ExceptionQueue, Any]] = []
    for item in result.unmatched:
        queue_row = ExceptionQueue(
            transaction_id=item.transaction_id,
            category=None,          # Subsystem 3 classifies (Sec. 10)
            state=ExceptionState.OPEN,
            classifier_confidence=None,
            # The near-miss is carried forward: Sec. 10 engineers its
            # features from how close the best candidate came.
            suggested_resolution=(
                {
                    "matching_engine": {
                        "reason": item.reason,
                        "best_confidence": item.best_confidence,
                        "best_counterpart_id": str(item.best_counterpart_id)
                        if item.best_counterpart_id
                        else None,
                        # Sec. 10 reads this to count counterparts, which is
                        # how it tells a split settlement from a partial
                        # payment. Stringified like the id above: JSONB has no
                        # UUID type, and the reader compares against ids it
                        # loaded the same way.
                        "candidate_ids": [str(c) for c in item.candidate_ids],
                        "threshold": result.threshold,
                    }
                }
                if item.best_counterpart_id or item.best_confidence
                else {"matching_engine": {"reason": item.reason}}
            ),
        )
        session.add(queue_row)
        opened.append((queue_row, item))
        exception_rows += 1

    run_id = _record_run(session, result)

    session.commit()

    # Announced only after the commit succeeds. Publishing first would put an
    # exception on the dashboard that a rollback then erased.
    _announce(session, result, opened, run_id)

    logger.info(
        "Persisted %d matches, %d ledger entries, %d exceptions",
        matched_rows, ledger_rows, exception_rows,
    )
    return {
        "matched_records": matched_rows,
        "ledger_entries": ledger_rows,
        "exceptions_opened": exception_rows,
        "run_id": str(run_id),
    }


def _announce(
    session: Session,
    result: ReconcileResult,
    opened: list[tuple[ExceptionQueue, Any]],
    run_id: Any,
) -> None:
    """Publish what this pass produced, for the dashboard's live feed (Sec. 12).

    Best-effort: a Redis outage costs live notifications, never data. Nothing
    here is substituted when publishing fails.
    """
    if not publisher.is_available():
        return

    if opened:
        transactions = {
            row.id: row
            for row in session.query(Transaction)
            .filter(Transaction.id.in_([q.transaction_id for q, _ in opened]))
            .all()
        }
        for queue_row, item in opened:
            txn = transactions.get(queue_row.transaction_id)
            if txn is None:
                continue
            publisher.publish(
                EventType.EXCEPTION_CREATED,
                exception_created(
                    queue_row.id,
                    {
                        "id": txn.id,
                        "external_id": txn.external_id,
                        "source_type": txn.source_type,
                        "amount": txn.amount,
                        "currency": txn.currency,
                        "txn_date": txn.txn_date,
                        "description": txn.description,
                        "reference_code": txn.reference_code,
                    },
                    reason=item.reason,
                    best_confidence=item.best_confidence,
                ),
            )

    publisher.publish(
        EventType.RECONCILIATION_COMPLETED,
        {
            "run_id": str(run_id),
            "matched": len(result.matched),
            "unmatched": len(result.unmatched),
            "match_rate": result.match_rate,
            "duration_ms": round(result.duration_ms, 2),
        },
    )


def _record_run(session: Session, result: ReconcileResult) -> Any:
    """Log the pass itself.

    GET /metrics/kpi reports reconciliation status and latency, and neither can
    be derived from matchedrecords - that table records what matched, never
    when a pass ran or how long it took. Without this row those KPIs could only
    be invented.
    """
    import datetime as dt

    completed = dt.datetime.now(dt.timezone.utc)
    run = ReconciliationRun(
        started_at=completed - dt.timedelta(milliseconds=result.duration_ms),
        completed_at=completed,
        duration_ms=round(result.duration_ms, 2),
        total_input=result.total_input,
        matched=len(result.matched),
        unmatched=len(result.unmatched),
        rule_matched=result.rule_matched,
        ml_matched=result.ml_matched,
        match_rate=result.match_rate,
        threshold=result.threshold,
        status="COMPLETED",
    )
    session.add(run)
    session.flush()   # so the id is available to the caller before commit
    return run.id


def _amount_index(session: Session, result: ReconcileResult) -> dict[Any, Any]:
    """Fetch amounts for every transaction referenced by a confirmed pair."""
    ids = {p.internal_id for p in result.matched} | {p.external_id for p in result.matched}
    if not ids:
        return {}

    rows = (
        session.query(Transaction.id, Transaction.amount)
        .filter(Transaction.id.in_(ids))
        .all()
    )
    return {row.id: row.amount for row in rows}


def load_unreconciled(session: Session, limit: int = 5000):
    """Transactions with no confirmed match and no open exception.

    This is what a scheduled reconciliation pass operates on: anything already
    matched or already queued must not be reprocessed, or the exception queue
    would fill with duplicates on every run.
    """
    import pandas as pd

    matched_ids = session.query(MatchedRecord.transaction_id).union(
        session.query(MatchedRecord.counterpart_id)
    )
    queued_ids = session.query(ExceptionQueue.transaction_id).filter(
        ExceptionQueue.state.in_([ExceptionState.OPEN, ExceptionState.SUGGESTED])
    )

    rows = (
        session.query(Transaction)
        .filter(~Transaction.id.in_(matched_ids))
        .filter(~Transaction.id.in_(queued_ids))
        .order_by(Transaction.ingested_at)
        .limit(limit)
        .all()
    )

    return pd.DataFrame(
        [
            {
                "id": row.id,
                "external_id": row.external_id,
                "source_type": row.source_type,
                "amount": float(row.amount),
                "currency": row.currency,
                "txn_date": row.txn_date,
                "description": row.description,
                "reference_code": row.reference_code,
            }
            for row in rows
        ]
    )


__all__ = ["persist_result", "load_unreconciled"]
