"""Stage 4 - persistence and quarantine (build.md Sec. 8).

    "On pass -> INSERT INTO transactions + validationlogs(status='PASSED').
     On fail -> write to the quarantine schema partition +
     validationlogs(status='QUARANTINED', violations=...) and emit a dashboard
     alert."

Every record produces a validationlogs row whichever way it went - that is what
makes the ingestion decision auditable end to end. A passed record's log points
at the transaction it created; a quarantined record's cannot (there is no row
yet), which is why validationlogs.transaction_id is nullable.

The whole batch commits in one transaction. A partial write would leave
transactions without their audit rows, and the >=98% detection claim is only
meaningful if the log is complete.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy.orm import Session

from shared.models.enums import ValidationState
from shared.models.orm import Quarantine, Transaction, ValidationLog

from .cache import ValidationCache
from .pipeline import BatchResult, RecordDecision

logger = logging.getLogger(__name__)


class PersistenceResult(dict):
    """Counts written, returned to the caller and surfaced by /validate."""


def _transaction_row(decision: RecordDecision) -> Transaction:
    txn = decision.transaction
    assert txn is not None, "a passed decision must carry its parsed Transaction"

    return Transaction(
        id=txn.id,
        external_id=txn.external_id,
        source_type=txn.source_type,
        amount=txn.amount,
        currency=txn.currency,
        txn_date=txn.txn_date,
        description=txn.description,
        reference_code=txn.reference_code,
        # The untouched source payload, kept for lineage.
        raw_payload=decision.payload,
    )


def _quarantine_row(decision: RecordDecision) -> Quarantine:
    payload = decision.payload if isinstance(decision.payload, dict) else {}
    return Quarantine(
        external_id=payload.get("external_id"),
        source_type=payload.get("source_type"),
        stage=decision.stage.value if decision.stage else "unknown",
        payload=payload,
        violations={"violations": decision.violations},
        payload_fingerprint=decision.fingerprint,
    )


def _validation_log(
    decision: RecordDecision, transaction_id: Any | None = None
) -> ValidationLog:
    return ValidationLog(
        transaction_id=transaction_id,
        stage=decision.stage.value if decision.stage else "checksum",
        status=(
            ValidationState.PASSED if decision.passed else ValidationState.QUARANTINED
        ),
        violations={"violations": decision.violations} if decision.violations else None,
    )


def persist_batch(
    session: Session,
    result: BatchResult,
    cache: ValidationCache | None = None,
) -> PersistenceResult:
    """Write a validated batch. Commits once, or rolls back entirely."""
    written_transactions = 0
    written_quarantine = 0
    alerts_emitted = 0
    duplicates = 0

    for decision in result.decisions:
        # A cached verdict means this exact payload was validated - and
        # persisted - on an earlier ingestion, so there is nothing to write.
        # Skipping is not an optimisation, it is the only correct action:
        # `to_cache_entry` deliberately stores the verdict alone and never the
        # parsed Transaction (its generated id must stay unique per
        # ingestion), so a cached PASSED decision reaches here with
        # `transaction=None` and re-persisting it used to trip
        # `_transaction_row`'s assertion and 500 the whole batch. Re-submitting
        # a record is normal - a retry, a replayed feed, a re-run seed - so
        # that turned an ordinary duplicate into an outage.
        if decision.from_cache:
            duplicates += 1
            continue

        if decision.passed:
            row = _transaction_row(decision)
            session.add(row)
            # `stage` on a passed record is the last one it cleared.
            session.add(_validation_log(decision, transaction_id=row.id))
            written_transactions += 1
        else:
            session.add(_quarantine_row(decision))
            session.add(_validation_log(decision, transaction_id=None))
            written_quarantine += 1

    session.commit()

    # Alerts only after the commit succeeds - announcing a quarantine that was
    # then rolled back would put a phantom item on the dashboard.
    if cache is not None:
        for decision in result.quarantined:
            # A duplicate was alerted on when it was first seen; re-announcing
            # it would put the same item on the dashboard twice.
            if decision.from_cache:
                continue
            if cache.publish_quarantine_alert(_alert(decision)):
                alerts_emitted += 1

    return PersistenceResult(
        transactions_inserted=written_transactions,
        quarantined=written_quarantine,
        alerts_emitted=alerts_emitted,
        duplicates_skipped=duplicates,
    )


def _alert(decision: RecordDecision) -> dict[str, Any]:
    payload = decision.payload if isinstance(decision.payload, dict) else {}
    return {
        "type": "validation.quarantined",
        "stage": decision.stage.value if decision.stage else "unknown",
        "external_id": payload.get("external_id"),
        "source_type": payload.get("source_type"),
        "fingerprint": decision.fingerprint,
        "violations": decision.violations,
        "detected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def replay_quarantined(
    session: Session,
    fingerprints: list[str] | None = None,
    ids: list[Any] | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Fetch quarantined payloads for re-ingestion after a feed is corrected.

    Marking `replayed_at` is the caller's job, and only once the replayed batch
    has actually passed - otherwise a failed replay would look resolved.
    """
    query = session.query(Quarantine).filter(Quarantine.replayed_at.is_(None))
    if ids:
        query = query.filter(Quarantine.id.in_(ids))
    if fingerprints:
        query = query.filter(Quarantine.payload_fingerprint.in_(fingerprints))

    rows = query.order_by(Quarantine.quarantined_at).limit(limit).all()
    return [
        {
            "id": str(row.id),
            "payload": row.payload,
            "stage": row.stage,
            "violations": row.violations,
            "fingerprint": row.payload_fingerprint,
        }
        for row in rows
    ]


def mark_replayed(session: Session, quarantine_ids: list[Any]) -> int:
    if not quarantine_ids:
        return 0
    updated = (
        session.query(Quarantine)
        .filter(Quarantine.id.in_(quarantine_ids))
        .update(
            {Quarantine.replayed_at: dt.datetime.now(dt.timezone.utc)},
            synchronize_session=False,
        )
    )
    session.commit()
    return updated


__all__ = ["persist_batch", "replay_quarantined", "mark_replayed", "PersistenceResult"]
