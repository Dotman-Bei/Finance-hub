"""Queue access and human-decision capture (build.md Sec. 10).

    "Every human decision (accept/reject/edit) is recorded; the audittrail
     trigger logs the state change automatically. These decisions become new
     labeled training data."

Two things this module is careful about.

**The audit trail is the database's job.** `trg_exception_audit` fires on every
UPDATE to exceptionqueue and writes old and new state to audittrail. This module
must not also write audit rows - doing so would double-log every decision and
break the tamper-evidence argument of Sec. 3.3.2. It sets `resolved_by` before
the update so the trigger records *who*, since the trigger reads that column.

**A rejection is not a label.** Accepting a suggestion confirms its category.
Editing it supplies the correct one. Rejecting says only that the suggestion was
wrong - it does not say what was right, so a rejected row yields no training
sample unless the reviewer also supplied a corrected category. Treating
rejections as labels for the suggested category would teach the forest the
opposite of what the human meant.

Feature vectors are stored on the queue row when a suggestion is written, so
training replays exactly the inputs the classifier saw rather than recomputing
them against transactions that may since have been matched.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from shared.events import EventType, publisher
from shared.models.enums import ExceptionCategory, ExceptionState
from shared.models.orm import ExceptionQueue, Transaction

from .features import ExceptionFeatures, extract

logger = logging.getLogger(__name__)

DECISION_ACCEPT = "ACCEPT"
DECISION_REJECT = "REJECT"
DECISION_EDIT = "EDIT"
DECISIONS = (DECISION_ACCEPT, DECISION_REJECT, DECISION_EDIT)


# ── Reading the queue ────────────────────────────────────────────────────


def _row_to_dict(txn: Transaction) -> dict[str, Any]:
    return {
        "id": txn.id,
        "external_id": txn.external_id,
        "source_type": txn.source_type,
        "amount": float(txn.amount) if txn.amount is not None else None,
        "currency": txn.currency,
        "txn_date": txn.txn_date,
        "description": txn.description,
        "reference_code": txn.reference_code,
    }


def load_untriaged(session: Session, limit: int = 500) -> list[dict[str, Any]]:
    """OPEN exceptions with their transaction and nominated counterparts.

    The matching engine records `best_counterpart_id` under
    `suggested_resolution.matching_engine`; those rows are fetched here so the
    features can be built without re-running Subsystem 1.
    """
    queue_rows = (
        session.query(ExceptionQueue)
        .filter(ExceptionQueue.state == ExceptionState.OPEN)
        .order_by(ExceptionQueue.created_at)
        .limit(limit)
        .all()
    )
    if not queue_rows:
        return []

    transaction_ids = {row.transaction_id for row in queue_rows}
    counterpart_ids: set[Any] = set()
    for row in queue_rows:
        for candidate in _nominated_ids(row):
            counterpart_ids.add(candidate)

    wanted = transaction_ids | counterpart_ids
    # Keyed by `str(id)`, not by `id`. The transaction ids come off the ORM as
    # UUID objects while the nominated counterpart ids come out of JSONB as
    # strings - persistence.py stringifies them deliberately, because JSONB has
    # no UUID type. Keying on the raw value made `cid in transactions` compare
    # a str against UUID keys, which is always False, so every nominated
    # counterpart was silently dropped and the classifier only ever saw
    # `counterpart_count == 0`. That collapses all four categories into the
    # "no counterpart was nominated" fallback, MISSING_REFERENCE_CODE.
    #
    # It survived every test because nothing but the deployed service reaches
    # the classifier through this function: verify_corpus.py and the unit tests
    # build the counterpart list themselves, keyed by the UUIDs they just
    # generated, and never round-trip through Postgres or JSONB.
    transactions = {
        str(txn.id): txn
        for txn in session.query(Transaction).filter(Transaction.id.in_(wanted)).all()
    }

    payload = []
    for row in queue_rows:
        txn = transactions.get(str(row.transaction_id))
        if txn is None:
            # A queue row whose transaction has been deleted cannot be triaged;
            # skipping it loudly is better than fabricating a placeholder.
            logger.warning("Exception %s references a missing transaction", row.id)
            continue

        counterparts = [
            _row_to_dict(transactions[str(cid)])
            for cid in _nominated_ids(row)
            if str(cid) in transactions
        ]

        payload.append(
            {
                "exception_id": row.id,
                "transaction": _row_to_dict(txn),
                "counterparts": counterparts,
                "matching_context": (row.suggested_resolution or {}).get(
                    "matching_engine", {}
                ),
            }
        )
    return payload


def _nominated_ids(row: ExceptionQueue) -> list[Any]:
    """Counterpart ids the matching engine recorded on this queue row."""
    context = (row.suggested_resolution or {}).get("matching_engine") or {}
    ids: list[Any] = []

    single = context.get("best_counterpart_id")
    if single:
        ids.append(single)

    for candidate in context.get("candidate_ids") or []:
        if candidate and candidate not in ids:
            ids.append(candidate)

    return ids


# ── Writing suggestions ──────────────────────────────────────────────────


def apply_suggestion(
    session: Session,
    exception_id: Any,
    category: str,
    confidence: float,
    suggestion: dict[str, Any],
    features: ExceptionFeatures,
) -> bool:
    """Write the classification and move the row to SUGGESTED (Sec. 10)."""
    row = session.get(ExceptionQueue, exception_id)
    if row is None:
        return False

    # Preserve what the matching engine recorded - the exception handler
    # augments that history rather than overwriting it.
    payload = dict(row.suggested_resolution or {})
    payload.update(suggestion)
    # Stored so retraining replays the exact inputs the classifier saw.
    payload["features"] = features.as_dict()

    row.category = ExceptionCategory(category)
    row.classifier_confidence = round(float(confidence), 4)
    row.suggested_resolution = payload
    row.state = ExceptionState.SUGGESTED

    session.add(row)
    return True


# ── Capturing decisions ──────────────────────────────────────────────────


def record_decision(
    session: Session,
    exception_id: Any,
    decision: str,
    actor: str,
    corrected_category: str | None = None,
    resolution: dict[str, Any] | None = None,
    note: str = "",
) -> dict[str, Any]:
    """Record a human accept / reject / edit.

    The audittrail row is written by `trg_exception_audit` on UPDATE, not here.
    """
    decision = decision.upper()
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, got {decision!r}")

    row = session.get(ExceptionQueue, exception_id)
    if row is None:
        raise LookupError(f"exception {exception_id} not found")

    if row.state in (ExceptionState.RESOLVED, ExceptionState.REJECTED):
        raise ValueError(
            f"exception {exception_id} is already {row.state.value}; "
            "reopening is not supported"
        )

    payload = dict(row.suggested_resolution or {})
    suggested_category = row.category.value if row.category else None

    if decision == DECISION_EDIT and corrected_category:
        row.category = ExceptionCategory(corrected_category)

    if resolution:
        payload["resolution"] = resolution

    payload["decision"] = {
        "decision": decision,
        "actor": actor,
        "note": note,
        "suggested_category": suggested_category,
        "final_category": row.category.value if row.category else None,
        # Whether this decision can be used as a training label, and why. Set
        # here rather than inferred later so the reasoning is auditable.
        "usable_as_label": _is_usable_label(decision, row.category),
        "decided_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }

    row.suggested_resolution = payload
    # Written before the state change: the audit trigger reads this column for
    # its `actor`, so setting it afterwards would log the change as 'system'.
    row.resolved_by = actor
    row.state = (
        ExceptionState.REJECTED if decision == DECISION_REJECT else ExceptionState.RESOLVED
    )
    row.resolved_at = dt.datetime.now(dt.timezone.utc)

    session.add(row)
    session.commit()

    outcome = {
        "id": str(row.id),
        "state": row.state.value,
        "category": row.category.value if row.category else None,
        "resolved_at": row.resolved_at.isoformat(),
        "usable_as_label": payload["decision"]["usable_as_label"],
    }

    # After the commit: a notification for a decision that then rolled back
    # would show the queue shrinking when it had not.
    publisher.publish(
        EventType.EXCEPTION_RESOLVED, {**outcome, "decision": decision, "actor": actor}
    )
    return outcome


def _is_usable_label(decision: str, category: Any) -> bool:
    if category is None:
        return False
    if decision == DECISION_ACCEPT:
        return True          # the human confirmed this category
    if decision == DECISION_EDIT:
        return True          # the human supplied the correct category
    return False             # REJECT says "wrong", never "right"


# ── Training data ────────────────────────────────────────────────────────


def training_samples(
    session: Session, limit: int = 20000
) -> tuple[list[tuple[ExceptionFeatures, str]], dict[str, int]]:
    """Labelled samples from resolved exceptions, plus a provenance count.

    The counts matter as much as the samples: a model trained entirely on
    bootstrap-derived suggestions has learned the rules, not the humans. The
    caller reports that distinction rather than hiding it behind an accuracy
    figure.
    """
    rows = (
        session.query(ExceptionQueue)
        .filter(
            or_(
                ExceptionQueue.state == ExceptionState.RESOLVED,
                ExceptionQueue.state == ExceptionState.REJECTED,
            )
        )
        .filter(ExceptionQueue.category.isnot(None))
        .order_by(ExceptionQueue.resolved_at.desc())
        .limit(limit)
        .all()
    )

    samples: list[tuple[ExceptionFeatures, str]] = []
    provenance = {"human_confirmed": 0, "human_corrected": 0, "unusable": 0}

    for row in rows:
        payload = row.suggested_resolution or {}
        decision = payload.get("decision") or {}
        stored_features = payload.get("features")

        if not decision.get("usable_as_label") or not stored_features:
            provenance["unusable"] += 1
            continue

        features = ExceptionFeatures(
            **{k: v for k, v in stored_features.items() if k != "context"}
        )
        samples.append((features, row.category.value))

        if decision.get("decision") == DECISION_EDIT:
            provenance["human_corrected"] += 1
        else:
            provenance["human_confirmed"] += 1

    provenance["total_usable"] = len(samples)
    return samples, provenance


def resolved_count(session: Session) -> int:
    """How many resolutions exist - the trigger for retraining (Sec. 11)."""
    return (
        session.query(ExceptionQueue)
        .filter(ExceptionQueue.state == ExceptionState.RESOLVED)
        .count()
    )


__all__ = [
    "load_untriaged",
    "apply_suggestion",
    "record_decision",
    "training_samples",
    "resolved_count",
    "DECISIONS",
    "DECISION_ACCEPT",
    "DECISION_REJECT",
    "DECISION_EDIT",
]
