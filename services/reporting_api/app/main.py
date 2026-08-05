"""Subsystem 4 backend - Reporting API gateway (build.md Sec. 12).

    uvicorn services.reporting_api.app.main:app --reload --port 8000

Sec. 12's surface:

    GET  /metrics/kpi                  KPI tiles
    GET  /metrics/match-rate?from=&to= chart time-series
    GET  /exceptions                   filterable queue
    POST /reports/generate             audit-ready PDF, persisted by ID
    WS   /ws/exceptions                live notifications

plus what the dashboard also needs and Sec. 12 does not enumerate:
`GET /reports` (history), `GET /reports/{id}/download`, and
`POST /exceptions/{id}/resolve`.

**Reads are served here; the exception write is proxied.** Sec. 12 describes
this service as a gateway "exposing reporting-only endpoints that aggregate
from Postgres", while Sec. 10 assigns `POST /exceptions/{id}/resolve` to the
exception handler. Reimplementing the write here would duplicate the
feedback-capture and audit-trigger semantics in two places and let them drift,
so it is forwarded to Subsystem 3. If that service is unreachable the caller
gets a 503 - never a fabricated success.

Response field names follow the dashboard's contract, which differs from the
database's in places (`report_type` -> `type`, `size_bytes` -> `size_kb`). The
mapping lives here so the schema keeps sensible column names.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import (
    Body,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Response,
    WebSocket,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shared.config import settings
from shared.db import get_session, healthcheck
from shared.events import publisher
from shared.models.enums import ExceptionCategory, ExceptionState
from shared.models.orm import ExceptionQueue, Report, Transaction

from .audit import actor_activity, integrity_report, query_trail
from .auth import (
    REQUIRE_AUTH,
    Permission,
    Principal,
    Role,
    get_principal,
    issue_token,
    requires,
    warn_if_auth_disabled,
)
from .metrics import MetricsCache, category_breakdown, kpi_summary, match_rate_series
from .reports import REPORT_TYPES, generate
from .ws import ConnectionManager, EventRelay, serve

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

EXCEPTION_HANDLER_URL = os.getenv("EXCEPTION_HANDLER_URL", "http://exception_handler:8003")

#: Single shared credential for token issue. Not a user store - a real
#: deployment puts an identity provider in front of this. Documented on the
#: endpoint so nobody mistakes it for authentication.
SERVICE_API_KEY = os.getenv("SERVICE_API_KEY", "")

CORS_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()
]

state: dict[str, Any] = {}


# ── Request models ───────────────────────────────────────────────────────


class TokenRequest(BaseModel):
    role: Role
    api_key: str = Field(default="", description="Matched against SERVICE_API_KEY")
    subject: str = Field(default="dashboard", max_length=120)


class GenerateReportRequest(BaseModel):
    type: str = Field(default="RECONCILIATION_SUMMARY")
    from_: dt.date | None = Field(default=None, alias="from")
    to: dt.date | None = None
    title: str | None = None

    model_config = {"populate_by_name": True}


class ResolveRequest(BaseModel):
    decision: str
    resolution: dict[str, Any] | None = None
    note: str = ""
    corrected_category: str | None = None


# ── Lifespan ─────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting reporting API")
    warn_if_auth_disabled()

    manager = ConnectionManager()
    relay = EventRelay(manager)
    await relay.start()

    state["cache"] = MetricsCache()
    state["manager"] = manager
    state["relay"] = relay
    state["http"] = httpx.AsyncClient(timeout=15.0)

    yield

    logger.info("Stopping reporting API")
    await relay.stop()
    await state["http"].aclose()


app = FastAPI(
    title="FinanceHub - Reporting API",
    description="Subsystem 4 backend. Aggregates reconciliation metrics, serves "
    "the exception queue, and produces audit-ready PDF reports.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def cache() -> MetricsCache:
    return state["cache"]


# ── Health and auth ──────────────────────────────────────────────────────


@app.get("/health")
def health() -> Any:
    database_ok = healthcheck()
    relay: EventRelay = state.get("relay")

    report = {
        "service": "reporting_api",
        "database": "up" if database_ok else "down",
        "redis": "up" if state["cache"].is_available() else "down",
        "events": relay.stats() if relay else {"redis_connected": False},
        "auth_required": REQUIRE_AUTH,
    }
    if not database_ok:
        raise HTTPException(status_code=503, detail=report)
    report["status"] = "healthy"
    return report


@app.post("/auth/token")
def token(request: TokenRequest) -> dict[str, Any]:
    """Issue a signed JWT carrying a role claim.

    This is **not** an identity provider. It verifies one shared secret and
    mints a token for the requested role; there is no user store in this
    system. A deployment puts a real IdP in front of the gateway and drops
    this endpoint. It exists so the dashboard has a way to obtain a token
    without pretending to authenticate anybody.
    """
    if not SERVICE_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="SERVICE_API_KEY is not configured; token issue is disabled.",
        )
    # Constant-time compare: a naive == leaks the key by timing.
    import hmac

    if not hmac.compare_digest(request.api_key, SERVICE_API_KEY):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    return {
        "access_token": issue_token(request.subject, request.role),
        "token_type": "bearer",
        "role": request.role.value,
        "expires_in_minutes": settings.jwt_expire_minutes,
    }


@app.get("/auth/me")
def whoami(principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    return principal.as_dict()


# ── Metrics ──────────────────────────────────────────────────────────────


@app.get("/metrics/kpi")
async def metrics_kpi(
    window_days: int = Query(default=30, ge=1, le=365),
    principal: Principal = Depends(requires(Permission.VIEW_METRICS)),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    key = f"kpi:{window_days}"
    cached = cache().get(key)
    if cached is not None:
        return cached

    payload = await run_in_threadpool(kpi_summary, session, window_days)
    cache().set(key, payload)
    return payload


@app.get("/metrics/match-rate")
async def metrics_match_rate(
    from_: dt.date | None = Query(default=None, alias="from"),
    to: dt.date | None = Query(default=None),
    principal: Principal = Depends(requires(Permission.VIEW_METRICS)),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    key = f"series:{from_}:{to}"
    cached = cache().get(key)
    if cached is not None:
        return cached

    payload = await run_in_threadpool(match_rate_series, session, from_, to)
    cache().set(key, payload)
    return payload


@app.get("/metrics/categories")
async def metrics_categories(
    principal: Principal = Depends(requires(Permission.VIEW_METRICS)),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    cached = cache().get("categories")
    if cached is not None:
        return cached
    payload = await run_in_threadpool(category_breakdown, session)
    cache().set("categories", payload)
    return payload


# ── Exceptions ───────────────────────────────────────────────────────────


def _serialise_exception(row: ExceptionQueue, txn: Transaction | None) -> dict[str, Any]:
    """The shape `normalizeException` in the dashboard expects."""
    created = row.created_at
    age_hours = (
        int((dt.datetime.now(dt.timezone.utc) - created).total_seconds() // 3600)
        if created
        else 0
    )
    return {
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
        "created_at": created.isoformat() if created else None,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        "age_hours": age_hours,
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


@app.get("/exceptions")
async def list_exceptions(
    category: str | None = Query(default=None),
    state_filter: str | None = Query(default=None, alias="state"),
    search: str | None = Query(default=None),
    limit: int = Query(default=100, gt=0, le=1000),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(requires(Permission.VIEW_EXCEPTIONS)),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Sortable / filterable exception list with categories and suggestions."""

    def query() -> dict[str, Any]:
        stmt = (
            select(ExceptionQueue, Transaction)
            .join(Transaction, Transaction.id == ExceptionQueue.transaction_id)
        )

        if category:
            try:
                stmt = stmt.where(ExceptionQueue.category == ExceptionCategory(category))
            except ValueError:
                raise HTTPException(400, f"unknown category {category!r}")
        if state_filter:
            try:
                stmt = stmt.where(ExceptionQueue.state == ExceptionState(state_filter))
            except ValueError:
                raise HTTPException(400, f"unknown state {state_filter!r}")
        if search:
            needle = f"%{search.strip()}%"
            stmt = stmt.where(
                Transaction.external_id.ilike(needle)
                | Transaction.description.ilike(needle)
                | Transaction.reference_code.ilike(needle)
            )

        # COUNT over a subquery rather than len(...all()): the naive form
        # fetches every matching row across the wire just to discard them,
        # which on a large queue is the difference between a paged endpoint
        # and a full table scan per poll.
        total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = session.execute(
            stmt.order_by(ExceptionQueue.created_at.desc()).offset(offset).limit(limit)
        ).all()

        return {
            "total": total,
            "count": len(rows),
            "offset": offset,
            "items": [_serialise_exception(row, txn) for row, txn in rows],
        }

    return await run_in_threadpool(query)


@app.post("/exceptions/{exception_id}/resolve")
async def resolve_exception(
    exception_id: str,
    request: ResolveRequest,
    principal: Principal = Depends(requires(Permission.RESOLVE_EXCEPTIONS)),
) -> dict[str, Any]:
    """Forward a human decision to the exception handler (Sec. 10).

    Not reimplemented here: Subsystem 3 owns feedback capture, and duplicating
    it would let the two copies drift on the very records the audit trail is
    meant to fix.
    """
    client: httpx.AsyncClient = state["http"]
    url = f"{EXCEPTION_HANDLER_URL}/exceptions/{exception_id}/resolve"

    try:
        response = await client.post(
            url,
            json={
                "decision": request.decision,
                # The authenticated subject, not a client-supplied name.
                "actor": principal.subject,
                "corrected_category": request.corrected_category,
                "resolution": request.resolution,
                "note": request.note,
            },
        )
    except httpx.RequestError as exc:
        # A 503 is the truth. Returning success would tell a finance user their
        # decision was recorded when nothing was written.
        logger.error("Exception handler unreachable at %s (%s)", url, exc)
        raise HTTPException(
            status_code=503,
            detail=f"Exception handler unreachable at {EXCEPTION_HANDLER_URL}. "
            "The decision was NOT recorded.",
        )

    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.json().get("detail"))

    cache().invalidate()
    return response.json()


# ── Reports ──────────────────────────────────────────────────────────────


def _serialise_report(row: Report) -> dict[str, Any]:
    """Dashboard field names, mapped from the database's."""
    period = "All time"
    if row.period_start or row.period_end:
        period = f"{row.period_start or 'inception'} → {row.period_end or 'today'}"

    return {
        "id": str(row.id),
        "name": row.name,
        "type": row.report_type,
        "period": period,
        "generated_at": row.created_at.isoformat() if row.created_at else None,
        "generated_by": row.generated_by,
        "size_kb": round((row.size_bytes or 0) / 1024) if row.size_bytes else 0,
        "status": row.status,
    }


@app.get("/reports")
async def list_reports(
    limit: int = Query(default=50, gt=0, le=500),
    principal: Principal = Depends(requires(Permission.GENERATE_REPORTS)),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    def query():
        rows = session.scalars(
            select(Report).order_by(Report.created_at.desc()).limit(limit)
        ).all()
        return [_serialise_report(row) for row in rows]

    return await run_in_threadpool(query)


@app.post("/reports/generate")
async def generate_report(
    request: GenerateReportRequest,
    principal: Principal = Depends(requires(Permission.GENERATE_REPORTS)),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Produce an audit-ready PDF and persist it against a report ID."""
    if request.type not in REPORT_TYPES:
        raise HTTPException(400, f"unknown report type; expected one of {sorted(REPORT_TYPES)}")
    if request.from_ and request.to and request.from_ > request.to:
        raise HTTPException(400, "'from' must be on or before 'to'")

    row = await run_in_threadpool(
        generate,
        session,
        request.type,
        principal.subject,
        request.from_,
        request.to,
        request.title,
    )
    return _serialise_report(row)


@app.get("/reports/{report_id}/download")
async def download_report(
    report_id: str,
    principal: Principal = Depends(requires(Permission.GENERATE_REPORTS)),
    session: Session = Depends(get_session),
) -> Response:
    """Stream the stored PDF. The persisted bytes, never a re-render.

    Regenerating on download would produce a different document from the one
    that was reviewed, which defeats the provenance the report exists for.
    """
    def fetch():
        row = session.get(Report, report_id)
        if row is None:
            raise HTTPException(404, f"report {report_id} not found")
        if not row.content:
            raise HTTPException(
                409,
                f"report {report_id} has status {row.status} and holds no document",
            )
        return row

    row = await run_in_threadpool(fetch)
    filename = "".join(c if c.isalnum() or c in "-_" else "_" for c in row.name)[:80]

    return Response(
        content=bytes(row.content),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}.pdf"'},
    )


# ── Live notifications ───────────────────────────────────────────────────


@app.websocket("/ws/exceptions")
async def exceptions_socket(websocket: WebSocket) -> None:
    """Sec. 12's live feed. Relays only; it originates nothing."""
    await serve(websocket, state["manager"], state["relay"])


# ── Audit trail (§3.3.2) ─────────────────────────────────────────────────
# Gated on VIEW_AUDIT_TRAIL, which only Auditors and Administrators hold — a
# Finance Manager can change records but not review the log of changes.


@app.get("/audit")
async def audit_trail(
    entity_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    action: str | None = Query(default=None),
    since: dt.datetime | None = Query(default=None),
    limit: int = Query(default=200, gt=0, le=1000),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(requires(Permission.VIEW_AUDIT_TRAIL)),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Read the append-only change log. There is no write counterpart."""
    return await run_in_threadpool(
        query_trail, session, entity_type, entity_id, actor, action, since, limit, offset
    )


@app.get("/audit/integrity")
async def audit_integrity(
    principal: Principal = Depends(requires(Permission.VIEW_AUDIT_TRAIL)),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Check every settled exception left an audit row (§16's audit gate).

    `complete: false` means the trigger is absent from this database or a write
    bypassed it. Either invalidates the tamper-evidence claim, and neither
    surfaces anywhere else.
    """
    return await run_in_threadpool(integrity_report, session)


@app.get("/audit/actors")
async def audit_actors(
    principal: Principal = Depends(requires(Permission.VIEW_AUDIT_TRAIL)),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    return await run_in_threadpool(actor_activity, session)


@app.get("/stats")
def stats(principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    relay: EventRelay = state.get("relay")
    return {
        "events": relay.stats() if relay else {},
        "publisher": publisher.stats(),
        "cache_available": state["cache"].is_available(),
        "principal": principal.as_dict(),
    }
