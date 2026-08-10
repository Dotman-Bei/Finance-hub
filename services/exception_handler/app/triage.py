"""Classifying the open queue (build.md Sec. 10), as one reusable pass.

Extracted from `main.py`'s POST /triage so the Celery beat schedule can run
the same code rather than a second copy of it. Nothing here is new behaviour;
the endpoint is now a thin wrapper around `triage_batch`.

Why it needed scheduling at all: the matching engine opens an exception with
`category=None` and Subsystem 3 fills it in, but the only thing that ever
called /triage was a human with curl. Beat scheduled retraining and nothing
else, so a deployed box accumulated uncategorised exceptions indefinitely -
the dashboard showed them as "Untriaged", the classifier was never exercised,
and no amount of waiting fixed it.

Deliberately synchronous: it is called both from a threadpool (the endpoint)
and from a Celery worker, and neither wants an event loop of its own.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from shared.events import EventType, publisher

from .classifier import ExceptionClassifier
from .features import extract
from .feedback import apply_suggestion, load_untriaged
from .resolution import suggest

logger = logging.getLogger(__name__)


def triage_batch(
    session: Session,
    classifier: ExceptionClassifier,
    limit: int = 500,
) -> dict[str, Any]:
    """Classify OPEN exceptions and write their suggested resolutions.

    Moves each row to `state='SUGGESTED'` with the pathway from Sec. 10's
    table. Returns the same shape POST /triage has always returned.
    """
    # The Celery worker retrains in a separate process, so the file on disk can
    # be newer than what this process holds. Checked per batch (Sec. 11's
    # "hot-swapped on next classify()").
    if classifier.reload_if_changed():
        logger.info("Picked up a retrained classifier before triage")

    pending = load_untriaged(session, limit)
    if not pending:
        return {"triaged": 0, "by_category": {}, "engine": None}

    by_category: dict[str, int] = {}
    triaged: list[dict[str, Any]] = []
    engine_used = None

    for row in pending:
        features = extract(
            row["transaction"], row["counterparts"], row["matching_context"]
        )
        result = classifier.classify(features)
        engine_used = result.engine

        payload = suggest(
            result.category,
            features,
            confidence=result.confidence,
            engine=result.engine,
            rationale=result.rationale,
        )
        apply_suggestion(
            session, row["exception_id"], result.category,
            result.confidence, payload, features,
        )
        by_category[result.category] = by_category.get(result.category, 0) + 1
        triaged.append(
            {
                "id": str(row["exception_id"]),
                "category": result.category,
                "classifier_confidence": round(result.confidence, 4),
                "state": "SUGGESTED",
                "suggested_resolution": payload,
                "transaction": {
                    **row["transaction"],
                    "id": str(row["transaction"]["id"]),
                    "txn_date": str(row["transaction"]["txn_date"]),
                },
            }
        )

    session.commit()

    # Published after the commit so the dashboard never sees a suggestion that
    # a rollback then discarded.
    for item in triaged:
        publisher.publish(EventType.EXCEPTION_SUGGESTED, item)
    logger.info("Triaged %d exceptions via %s: %s", len(pending), engine_used, by_category)

    return {"triaged": len(pending), "by_category": by_category, "engine": engine_used}


__all__ = ["triage_batch"]
