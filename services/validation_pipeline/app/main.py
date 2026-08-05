"""Subsystem 2 - Real-Time Validation Pipeline (build.md Sec. 8).

FastAPI entrypoint. Owns the long-lived objects (the GE context, the Redis
connection, the Kafka consumer thread) and exposes the pipeline over REST for
direct submission and for the release-gate harness.

    uvicorn services.validation_pipeline.app.main:app --reload --port 8001

/health reports each dependency truthfully and returns 503 when Postgres is
unreachable. It never claims healthy on a degraded stack - an operator reading
a green check while records pile up undelivered is worse than no check.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from shared.db import engine, get_session, healthcheck

from .cache import ValidationCache
from .ingestion import KafkaIngestor, normalize
from .pipeline import ValidationPipeline
from .quarantine import mark_replayed, persist_batch, replay_quarantined
from .rule_processor import RuleProcessor

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

#: Set CONSUME_KAFKA=false to run the REST surface without a broker.
CONSUME_KAFKA = os.getenv("CONSUME_KAFKA", "true").lower() not in ("false", "0", "no")

state: dict[str, Any] = {"pipeline": None, "cache": None, "ingestor": None}


# ── Request / response models ────────────────────────────────────────────


class ValidateRequest(BaseModel):
    records: list[dict[str, Any]] = Field(
        default_factory=list, description="Pre-parsed records"
    )
    raw: str | None = Field(
        default=None, description="A raw CSV or JSON document to normalise first"
    )
    persist: bool = Field(
        default=True, description="Write results to Postgres. False validates only."
    )


class ValidateResponse(BaseModel):
    total: int
    passed: int
    quarantined: int
    cache_hits: int
    quarantined_by_stage: dict[str, int]
    detection_summary: list[dict[str, Any]]
    persistence: dict[str, int] | None = None


# ── Lifespan ─────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting validation pipeline")

    cache = ValidationCache()
    # Building the GE context takes about a second; do it once at startup so
    # the first request is not the one that pays for it.
    pipeline = ValidationPipeline(
        rule_processor=RuleProcessor(),
        cache=cache,
        hmac_secret=os.getenv("SOURCE_HMAC_SECRET"),
    )

    state["cache"] = cache
    state["pipeline"] = pipeline

    if CONSUME_KAFKA:
        ingestor = KafkaIngestor(on_batch=_process_batch_from_kafka)
        if not ingestor.start():
            logger.error(
                "Kafka ingestion is not running. REST submission still works; "
                "/health will report the failure."
            )
        state["ingestor"] = ingestor
    else:
        logger.info("CONSUME_KAFKA=false - REST-only mode")

    yield

    logger.info("Stopping validation pipeline")
    if state.get("ingestor"):
        state["ingestor"].stop()
    if state.get("cache"):
        state["cache"].close()


app = FastAPI(
    title="FinanceHub - Validation Pipeline",
    description="Subsystem 2. Detects and quarantines structural inconsistencies "
    "at ingestion, before any data reaches the database.",
    version="0.1.0",
    lifespan=lifespan,
)


def _process_batch_from_kafka(records: list[dict[str, Any]]) -> None:
    """Kafka callback: validate then persist, in its own DB session."""
    from shared.db import session_scope

    pipeline: ValidationPipeline = state["pipeline"]
    result = pipeline.validate_batch(records)

    with session_scope() as session:
        written = persist_batch(session, result, cache=state.get("cache"))

    logger.info(
        "Batch of %d: %d passed, %d quarantined %s",
        result.total,
        len(result.passed),
        len(result.quarantined),
        result.by_stage() or "",
    )
    return written


def get_pipeline() -> ValidationPipeline:
    pipeline = state.get("pipeline")
    if pipeline is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Pipeline is still starting",
        )
    return pipeline


# ── Endpoints ────────────────────────────────────────────────────────────


@app.get("/health")
def health() -> Any:
    """Per-dependency truth. 503 if Postgres is down."""
    cache: ValidationCache | None = state.get("cache")
    ingestor: KafkaIngestor | None = state.get("ingestor")

    database_ok = healthcheck()
    report = {
        "service": "validation_pipeline",
        "database": "up" if database_ok else "down",
        "redis": "up" if (cache and cache.is_available()) else "down",
        "kafka": ingestor.stats() if ingestor else {"running": False, "reason": "disabled"},
        "pipeline_ready": state.get("pipeline") is not None,
    }

    if not database_ok:
        # Nothing can be persisted without Postgres, so this is not healthy.
        raise HTTPException(status_code=503, detail=report)

    report["status"] = "healthy"
    return report


@app.post("/validate", response_model=ValidateResponse)
def validate(
    request: ValidateRequest = Body(...),
    pipeline: ValidationPipeline = Depends(get_pipeline),
    session: Session = Depends(get_session),
) -> ValidateResponse:
    """Run the four stages over a submitted batch.

    Accepts either pre-parsed `records` or a `raw` CSV/JSON document, which is
    normalised through the same Pandas path Kafka messages take.
    """
    records = list(request.records)
    if request.raw:
        records.extend(normalize(request.raw))

    if not records:
        raise HTTPException(status_code=400, detail="No records supplied")

    result = pipeline.validate_batch(records, as_of=dt.date.today())

    persistence = None
    if request.persist:
        persistence = dict(persist_batch(session, result, cache=state.get("cache")))

    return ValidateResponse(
        **result.summary(),
        detection_summary=[
            {
                "index": d.index,
                "status": d.status.value,
                "stage": d.stage.value if d.stage else None,
                "violations": d.violations,
                "from_cache": d.from_cache,
            }
            for d in result.decisions
        ],
        persistence=persistence,
    )


@app.get("/quarantine")
def list_quarantine(
    limit: int = 100, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Unreplayed quarantined payloads, for diagnosis and replay."""
    rows = replay_quarantined(session, limit=limit)
    return {"count": len(rows), "items": rows}


@app.post("/quarantine/replay")
def replay(
    quarantine_ids: list[str] = Body(..., embed=True),
    pipeline: ValidationPipeline = Depends(get_pipeline),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Re-run quarantined payloads after the upstream feed is corrected.

    Only records that pass this time are marked replayed; the rest stay
    quarantined with a fresh verdict.
    """
    selected = replay_quarantined(session, ids=quarantine_ids, limit=len(quarantine_ids))
    if not selected:
        return {"replayed": 0, "still_quarantined": 0, "detail": "no matching rows"}

    result = pipeline.validate_batch([r["payload"] for r in selected])
    persist_batch(session, result, cache=state.get("cache"))

    now_passing = [
        selected[d.index]["id"] for d in result.decisions if d.passed
    ]
    mark_replayed(session, now_passing)

    return {
        "replayed": len(now_passing),
        "still_quarantined": len(result.quarantined),
        "by_stage": result.by_stage(),
    }


@app.get("/stats")
def stats() -> dict[str, Any]:
    ingestor: KafkaIngestor | None = state.get("ingestor")
    cache: ValidationCache | None = state.get("cache")
    return {
        "kafka": ingestor.stats() if ingestor else {"running": False},
        "cache_available": bool(cache and cache.is_available()),
        "database_url": engine.url.render_as_string(hide_password=True),
    }
