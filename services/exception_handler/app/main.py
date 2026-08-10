"""Subsystem 3 - Smart Exception Handling Module (build.md Sec. 10).

    uvicorn services.exception_handler.app.main:app --reload --port 8003

Sec. 10's API surface:

    GET  /exceptions                 query and display the queue
    POST /exceptions/{id}/resolve    accept / reject / edit

plus the operational endpoints the module needs to function: /triage drains
OPEN rows through the classifier and resolution engine, and /models/train fits
the forest on captured human decisions (Sec. 11's feedback loop).

The classifier is loaded once at startup. If none exists the service still
triages using the bootstrap rules and says so on /health and on every
suggestion it writes - a cold-start deployment has no labels yet, and refusing
to triage until it does would leave the queue untouched for weeks.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from shared.config import settings
from shared.db import get_session, healthcheck
from shared.events import EventType, publisher
from shared.models.enums import ExceptionCategory, ExceptionState
from shared.models.orm import ExceptionQueue

from .classifier import MODEL_PATH, ExceptionClassifier
from .features import extract
from .feedback import (
    DECISIONS,
    apply_suggestion,
    load_untriaged,
    record_decision,
    resolved_count,
    training_samples,
)
from .resolution import suggest
from .triage import triage_batch

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

state: dict[str, Any] = {"classifier": None}


class ResolveRequest(BaseModel):
    decision: str = Field(description="ACCEPT | REJECT | EDIT")
    actor: str = Field(min_length=1, description="Who is deciding; goes to audittrail")
    corrected_category: str | None = Field(
        default=None, description="Required when decision is EDIT and the category changes"
    )
    resolution: dict[str, Any] | None = None
    note: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting exception handler")
    classifier = ExceptionClassifier(path=MODEL_PATH)
    if not classifier.is_trained:
        logger.warning(
            "No trained classifier at %s. Triage will use the deterministic "
            "bootstrap rules; every suggestion is tagged engine='bootstrap'. "
            "POST /models/train once human decisions exist.",
            MODEL_PATH,
        )
    state["classifier"] = classifier
    yield
    logger.info("Stopping exception handler")


app = FastAPI(
    title="FinanceHub - Exception Handler",
    description="Subsystem 3. Classifies unmatched transactions into four "
    "categories and proposes a resolution pathway for each.",
    version="0.1.0",
    lifespan=lifespan,
)


def get_classifier() -> ExceptionClassifier:
    classifier = state.get("classifier")
    if classifier is None:
        raise HTTPException(status_code=503, detail="Classifier is still loading")
    return classifier


@app.get("/health")
def health() -> Any:
    classifier: ExceptionClassifier | None = state.get("classifier")
    database_ok = healthcheck()

    report = {
        "service": "exception_handler",
        "database": "up" if database_ok else "down",
        "classifier": classifier.status() if classifier else {"trained": False},
        "retrain_trigger_count": settings.retrain_trigger_count,
    }
    if not database_ok:
        raise HTTPException(status_code=503, detail=report)
    report["status"] = "healthy"
    return report


@app.post("/triage")
async def triage(
    limit: int = Body(default=500, embed=True),
    classifier: ExceptionClassifier = Depends(get_classifier),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Classify OPEN exceptions and write their suggested resolutions.

    The work lives in `triage.triage_batch` so the Celery beat schedule runs
    the same pass rather than a second copy of it. Handed to a threadpool
    because it is CPU-bound and blocking - `async def` alone would pin the
    event loop for the whole batch.
    """
    return await run_in_threadpool(triage_batch, session, classifier, limit)


@app.get("/exceptions")
def list_exceptions(
    category: str | None = Query(default=None),
    state_filter: str | None = Query(default=None, alias="state"),
    limit: int = Query(default=100, gt=0, le=1000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """The queue, filterable - Sec. 10's 'query, display and update'.

    The response nests `transaction` under each row, which is the shape the
    dashboard's ExceptionPanel consumes.
    """
    from shared.models.orm import Transaction

    query = session.query(ExceptionQueue)
    if category:
        try:
            query = query.filter(ExceptionQueue.category == ExceptionCategory(category))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"unknown category {category!r}")
    if state_filter:
        try:
            query = query.filter(ExceptionQueue.state == ExceptionState(state_filter))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"unknown state {state_filter!r}")

    total = query.count()
    rows = (
        query.order_by(ExceptionQueue.created_at.desc()).offset(offset).limit(limit).all()
    )

    transactions = {
        t.id: t
        for t in session.query(Transaction)
        .filter(Transaction.id.in_([r.transaction_id for r in rows]))
        .all()
    } if rows else {}

    items = []
    for row in rows:
        txn = transactions.get(row.transaction_id)
        items.append(
            {
                "id": str(row.id),
                "transaction_id": str(row.transaction_id),
                "category": row.category.value if row.category else None,
                "state": row.state.value,
                "classifier_confidence": (
                    float(row.classifier_confidence)
                    if row.classifier_confidence is not None
                    else None
                ),
                "suggested_resolution": row.suggested_resolution,
                "resolved_by": row.resolved_by,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
                "transaction": {
                    "id": str(txn.id),
                    "external_id": txn.external_id,
                    "source_type": txn.source_type,
                    "amount": float(txn.amount),
                    "currency": txn.currency,
                    "txn_date": txn.txn_date.isoformat(),
                    "description": txn.description,
                    "reference_code": txn.reference_code,
                }
                if txn
                else None,
            }
        )

    return {"total": total, "count": len(items), "offset": offset, "items": items}


@app.post("/exceptions/{exception_id}/resolve")
def resolve(
    exception_id: str,
    request: ResolveRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Record a human decision (Sec. 10 feedback capture).

    The audittrail row is written by the database trigger, not here.
    """
    if request.decision.upper() not in DECISIONS:
        raise HTTPException(
            status_code=400, detail=f"decision must be one of {list(DECISIONS)}"
        )
    if request.corrected_category:
        try:
            ExceptionCategory(request.corrected_category)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"unknown category {request.corrected_category!r}",
            )

    try:
        return record_decision(
            session,
            exception_id,
            decision=request.decision,
            actor=request.actor,
            corrected_category=request.corrected_category,
            resolution=request.resolution,
            note=request.note,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/models/train")
async def train(
    classifier: ExceptionClassifier = Depends(get_classifier),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Fit the forest on captured human decisions.

    Rejections are excluded: a rejection says the suggestion was wrong, never
    what was right, so it carries no label.
    """
    samples, provenance = await run_in_threadpool(training_samples, session)

    if len(samples) < 8:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Not enough labelled exceptions to train.",
                "usable_samples": len(samples),
                "required": 8,
                "provenance": provenance,
            },
        )

    metrics = await run_in_threadpool(
        classifier.train, samples, provenance["total_usable"]
    )
    return {"provenance": provenance, **{k: v for k, v in metrics.items() if k != "report"}}


@app.get("/models")
def model_status(
    classifier: ExceptionClassifier = Depends(get_classifier),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    classifier.reload_if_changed()
    samples, provenance = training_samples(session)
    usable = len(samples)

    return {
        **classifier.status(),
        "resolved_exceptions": resolved_count(session),
        "retrain_trigger_count": settings.retrain_trigger_count,
        # How close the feedback loop is to its next retrain (Sec. 11).
        "feedback": {
            "usable_labels": usable,
            "remaining_until_retrain": max(0, settings.retrain_trigger_count - usable),
            **provenance,
        },
    }
