"""Subsystem 1 - Hybrid Matching Engine (build.md Sec. 9).

    uvicorn services.matching_engine.app.main:app --reload --port 8002

Sec. 9 asks for "async endpoints so high transaction counts don't block".
Reconciliation is CPU-bound (TF-IDF, DBSCAN, cosine similarity), so `async def`
alone would be counterproductive - it would pin the event loop and block every
other request. The endpoints are async and hand the work to a thread via
`run_in_threadpool`, which is what actually keeps the service responsive under
load.

Persisted models are loaded once at startup (Sec. 9, "load at service start").
If none exist the service still starts and reconciles, fitting a vocabulary per
batch, and says so on /health - a first deployment has no history to fit on.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import pandas as pd
from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from shared.config import settings
from shared.db import get_session, healthcheck

from .ml_model import MODEL_DIR, FuzzyMatcher
from .persistence import load_unreconciled, persist_result
from .pipeline import MatchingPipeline

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

state: dict[str, Any] = {"pipeline": None, "matcher": None, "models_loaded": False}


class ReconcileRequest(BaseModel):
    transactions: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Batch to reconcile. Omit to pull unreconciled rows from Postgres.",
    )
    threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Override MATCH_CONFIDENCE_THRESHOLD for this run only.",
    )
    persist: bool = Field(default=True, description="Write results to Postgres.")
    limit: int = Field(default=5000, gt=0, le=50000)


class ReconcileResponse(BaseModel):
    """Sec. 9's specified shape: {matched, unmatched, match_rate} plus context."""

    matched: int
    unmatched: int
    match_rate: float
    rule_matched: int
    ml_matched: int
    threshold: float
    duration_ms: float
    total_input: int
    persistence: dict[str, int | str] | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting matching engine")

    matcher = FuzzyMatcher.load(MODEL_DIR)
    if matcher is None:
        logger.warning(
            "No persisted models in %s. Reconciliation will fit a vocabulary per "
            "batch, so confidence scores are not comparable across runs. "
            "POST /models/fit to build them from history.",
            MODEL_DIR,
        )
        matcher = FuzzyMatcher()
        state["models_loaded"] = False
    else:
        state["models_loaded"] = True

    state["matcher"] = matcher
    state["pipeline"] = MatchingPipeline(matcher=matcher)

    yield
    logger.info("Stopping matching engine")


app = FastAPI(
    title="FinanceHub - Matching Engine",
    description="Subsystem 1. Deterministic rule matching followed by "
    "unsupervised fuzzy matching, gated by a configurable confidence threshold.",
    version="0.1.0",
    lifespan=lifespan,
)


def get_pipeline() -> MatchingPipeline:
    pipeline = state.get("pipeline")
    if pipeline is None:
        raise HTTPException(status_code=503, detail="Engine is still starting")
    return pipeline


@app.get("/health")
def health() -> Any:
    database_ok = healthcheck()
    report = {
        "service": "matching_engine",
        "database": "up" if database_ok else "down",
        "models_loaded": state.get("models_loaded", False),
        "threshold": settings.match_confidence_threshold,
        "model_dir": str(MODEL_DIR),
    }
    if not database_ok:
        raise HTTPException(status_code=503, detail=report)
    report["status"] = "healthy"
    return report


@app.post("/reconcile", response_model=ReconcileResponse)
async def reconcile(
    request: ReconcileRequest = Body(default_factory=ReconcileRequest),
    pipeline: MatchingPipeline = Depends(get_pipeline),
    session: Session = Depends(get_session),
) -> ReconcileResponse:
    """Run rule -> ML -> scoring over a batch and return Sec. 9's summary."""
    if request.transactions:
        frame = pd.DataFrame(request.transactions)
    else:
        frame = await run_in_threadpool(load_unreconciled, session, request.limit)

    if frame.empty:
        return ReconcileResponse(
            matched=0, unmatched=0, match_rate=0.0, rule_matched=0, ml_matched=0,
            threshold=request.threshold or pipeline.threshold,
            duration_ms=0.0, total_input=0,
        )

    run = pipeline
    if request.threshold is not None and request.threshold != pipeline.threshold:
        run = MatchingPipeline(matcher=pipeline.matcher, threshold=request.threshold)

    # CPU-bound: off the event loop so concurrent requests are not blocked.
    result = await run_in_threadpool(run.reconcile, frame)

    persistence = None
    if request.persist:
        persistence = await run_in_threadpool(persist_result, session, result)

    return ReconcileResponse(**result.summary(), persistence=persistence)


@app.post("/models/fit")
async def fit_models(
    limit: int = Body(default=20000, embed=True),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Fit the vectorizer and outlier detector on historical descriptions.

    Sec. 9's "fit clustering models on historical data offline". Exposed as an
    endpoint so it can be run against real transactions rather than needing a
    separate batch job.
    """
    from shared.models.orm import Transaction

    rows = (
        session.query(Transaction.description)
        .filter(Transaction.description.isnot(None))
        .order_by(Transaction.ingested_at.desc())
        .limit(limit)
        .all()
    )
    descriptions = [r.description for r in rows if r.description]

    if len(descriptions) < 2:
        raise HTTPException(
            status_code=400,
            detail=f"Need at least 2 historical descriptions to fit, found "
            f"{len(descriptions)}. Ingest transactions first.",
        )

    matcher = FuzzyMatcher()
    await run_in_threadpool(matcher.fit, descriptions)
    paths = await run_in_threadpool(matcher.save, MODEL_DIR)

    state["matcher"] = matcher
    state["pipeline"] = MatchingPipeline(matcher=matcher)
    state["models_loaded"] = True

    return {
        "fitted_on": len(descriptions),
        "vocabulary_size": len(matcher.vectorizer.vocabulary_),
        "paths": paths,
        "fitted_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


@app.get("/models")
def model_status() -> dict[str, Any]:
    matcher: FuzzyMatcher | None = state.get("matcher")
    return {
        "loaded": state.get("models_loaded", False),
        "model_dir": str(MODEL_DIR),
        "vocabulary_size": (
            len(matcher.vectorizer.vocabulary_)
            if matcher and matcher.vectorizer is not None
            else 0
        ),
        "eps": matcher.eps if matcher else None,
        "min_samples": matcher.min_samples if matcher else None,
        # DBSCAN has no predict(), so it is refitted per batch by necessity.
        "refit_per_batch": ["DBSCAN"],
        "persisted": ["TfidfVectorizer", "LocalOutlierFactor(novelty=True)"],
    }
