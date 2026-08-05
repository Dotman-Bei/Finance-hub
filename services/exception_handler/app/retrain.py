"""Phase 5 - feedback loop and retraining (build.md Sec. 11).

    "A Celery beat task periodically pulls all human-resolved exceptions and
     retrains the Random Forest once resolved-count crosses
     RETRAIN_TRIGGER_COUNT."

    "This is the 'gradually becomes more precise' mechanism from Sec. 3.3.1 -
     accuracy improves and manual intervention drops with each reconciliation
     round."

Three departures from Sec. 11's snippet, each protecting that claim rather
than contradicting it:

1. **The retrained model is not promoted unconditionally.** The snippet ends
   with `joblib.dump(...)`, so a single round of poor labels silently replaces
   a good classifier and every later suggestion degrades. Here the candidate is
   scored against the incumbent on the same held-out rows and kept only if it
   does not regress. Sec. 14's gate - "classifier accuracy after retraining >=
   accuracy before" - is that guarantee, so enforcing it in the task is what
   makes the gate meaningful rather than aspirational.

2. **Rejections are not labels.** `fetch_resolved_exceptions()` in the snippet
   reads `state='RESOLVED'`. An accept confirms a category and an edit supplies
   the correct one, but a rejection says only that the suggestion was wrong,
   never what was right. `feedback.training_samples` filters on the
   `usable_as_label` flag recorded at decision time.

3. **The trigger counts *usable* labels, not resolved rows.** Counting rows
   that carry no label would fire a retrain on the same data repeatedly and
   report progress that had not happened.

The worker is a separate process from the API, so writing the model file does
not by itself change what the API classifies with - see
`ExceptionClassifier.reload_if_changed`.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from celery import Celery
from celery.schedules import crontab

from shared.config import settings
from shared.db import session_scope

from .classifier import MODEL_PATH, ExceptionClassifier
from .feedback import resolved_count, training_samples

logger = logging.getLogger(__name__)

celery = Celery("financehub.retrain", broker=settings.celery_broker_url)

celery.conf.update(
    result_backend=settings.celery_broker_url,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # A retrain that outlives its schedule would stack workers on the same
    # model file.
    task_time_limit=30 * 60,
    task_soft_time_limit=25 * 60,
    worker_max_tasks_per_child=50,
    beat_schedule={
        "retrain-classifier-hourly": {
            "task": "financehub.retrain.retrain_if_ready",
            # Hourly, not per-resolution: retraining is minutes of CPU, and
            # a classifier that changes under reviewers mid-session makes the
            # queue behave inconsistently within one sitting.
            "schedule": crontab(minute=0),
        }
    },
)

#: Minimum usable labels before the first retrain is even attempted. Below
#: this the held-out split is too small for its score to mean anything.
MIN_SAMPLES = 8


@celery.task(name="financehub.retrain.retrain_if_ready", bind=True)
def retrain_if_ready(self, force: bool = False) -> dict[str, Any]:
    """Retrain the classifier from captured human decisions.

    Returns a structured outcome in every branch - skipped, rejected or
    promoted - so a beat log shows what actually happened rather than silence.
    """
    started = dt.datetime.now(dt.timezone.utc)

    with session_scope() as session:
        samples, provenance = training_samples(session)
        resolved = resolved_count(session)

    usable = len(samples)
    trigger = settings.retrain_trigger_count

    if not force and usable < trigger:
        logger.info(
            "Retrain skipped: %d usable labels of %d required (%d exceptions "
            "resolved, %d carried no usable label)",
            usable, trigger, resolved, provenance.get("unusable", 0),
        )
        return {
            "status": "skipped",
            "reason": f"{usable} usable labels below the {trigger} trigger",
            "usable_labels": usable,
            "resolved_exceptions": resolved,
            "provenance": provenance,
        }

    if usable < MIN_SAMPLES:
        return {
            "status": "skipped",
            "reason": f"{usable} labels is too few to train on, even forced",
            "usable_labels": usable,
            "provenance": provenance,
        }

    classifier = ExceptionClassifier(path=MODEL_PATH)
    before = classifier.evaluate(samples)

    try:
        outcome = classifier.train(
            samples,
            human_labelled=provenance["total_usable"],
            promote_only_if_better=True,
        )
    except ValueError as exc:
        # Too few categories, for instance. A failed retrain must leave the
        # incumbent in place and say so.
        logger.warning("Retrain could not run: %s", exc)
        return {
            "status": "failed",
            "reason": str(exc),
            "usable_labels": usable,
            "provenance": provenance,
        }

    duration = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()

    if not outcome.get("promoted"):
        logger.warning(
            "Retrain candidate rejected (%s); incumbent kept", outcome.get("reason")
        )
        return {
            "status": "rejected",
            "reason": outcome.get("reason"),
            "candidate": outcome.get("candidate"),
            "incumbent": outcome.get("incumbent"),
            "usable_labels": usable,
            "provenance": provenance,
            "duration_seconds": round(duration, 2),
        }

    logger.info(
        "Retrained and promoted: %d labels (%d confirmed, %d corrected), "
        "macro F1 %.3f",
        usable,
        provenance.get("human_confirmed", 0),
        provenance.get("human_corrected", 0),
        outcome["macro_f1"],
    )

    return {
        "status": "promoted",
        "usable_labels": usable,
        "resolved_exceptions": resolved,
        "provenance": provenance,
        "before": before,
        "after": {"accuracy": outcome["accuracy"], "macro_f1": outcome["macro_f1"]},
        "model_path": str(MODEL_PATH),
        "duration_seconds": round(duration, 2),
    }


@celery.task(name="financehub.retrain.training_readiness")
def training_readiness() -> dict[str, Any]:
    """How close the feedback loop is to its next retrain, without running one."""
    with session_scope() as session:
        samples, provenance = training_samples(session)
        resolved = resolved_count(session)

    usable = len(samples)
    trigger = settings.retrain_trigger_count
    return {
        "usable_labels": usable,
        "resolved_exceptions": resolved,
        "trigger": trigger,
        "remaining": max(0, trigger - usable),
        "ready": usable >= trigger,
        "provenance": provenance,
    }


__all__ = ["celery", "retrain_if_ready", "training_readiness", "MIN_SAMPLES"]
