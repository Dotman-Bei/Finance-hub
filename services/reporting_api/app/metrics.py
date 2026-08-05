"""Metric aggregation over Postgres (build.md Sec. 12).

    GET /metrics/kpi          total volume, overall match rate, open
                              exceptions, reconciliation status
    GET /metrics/match-rate   time-series for charts

    "A Redis cache sits between the gateway and the DB so repeated dashboard
     polls don't hammer the database."

Every figure here is computed from a table. Where a figure cannot be derived
the field is `null` and the dashboard renders a dash - it is never filled with
a plausible-looking number. `next_run_at` is the clearest case: nothing in this
system schedules reconciliation passes yet, so there is no honest value for it.

The cache is short-lived (30s) and keyed by query. A stale KPI is a worse
problem here than a database round trip, so the TTL is deliberately tight.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

import redis
from sqlalchemy import Date, case, cast, func, select
from sqlalchemy.orm import Session

from shared.config import settings
from shared.models.enums import ExceptionState, MatchType, ValidationState
from shared.models.orm import (
    ExceptionQueue,
    MatchedRecord,
    ReconciliationRun,
    Transaction,
    ValidationLog,
)

logger = logging.getLogger(__name__)

CACHE_PREFIX = "financehub:metrics:"
CACHE_TTL_SECONDS = 30


class MetricsCache:
    """Redis in front of the aggregations. Fails open to a live query."""

    def __init__(self, url: str | None = None, ttl: int = CACHE_TTL_SECONDS):
        self.url = url or settings.redis_url
        self.ttl = ttl
        self._client: redis.Redis | None = None
        self._warned = False

    @property
    def client(self) -> redis.Redis | None:
        if self._client is None:
            try:
                self._client = redis.Redis.from_url(
                    self.url, decode_responses=True,
                    socket_connect_timeout=2, socket_timeout=2,
                )
                self._client.ping()
                self._warned = False
            except Exception as exc:
                if not self._warned:
                    logger.warning("Redis unavailable (%s); serving uncached", exc)
                    self._warned = True
                self._client = None
        return self._client

    def get(self, key: str) -> Any | None:
        client = self.client
        if client is None:
            return None
        try:
            raw = client.get(CACHE_PREFIX + key)
            return json.loads(raw) if raw else None
        except Exception:
            self._client = None
            return None

    def set(self, key: str, value: Any) -> None:
        client = self.client
        if client is None:
            return
        try:
            client.setex(CACHE_PREFIX + key, self.ttl, json.dumps(value, default=str))
        except Exception:
            self._client = None

    def invalidate(self) -> None:
        """Called after a write so the next poll is not served a stale figure."""
        client = self.client
        if client is None:
            return
        try:
            for key in client.scan_iter(CACHE_PREFIX + "*"):
                client.delete(key)
        except Exception:
            self._client = None

    def is_available(self) -> bool:
        return self.client is not None


def _ratio(numerator: float, denominator: float) -> float | None:
    """None rather than 0.0 when there is nothing to divide by.

    A match rate of 0% and "no transactions yet" are different facts, and a
    dashboard showing 0% on an empty database is misleading.
    """
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def kpi_summary(session: Session, window_days: int = 30) -> dict[str, Any]:
    """Sec. 12's KPI tile figures, computed from Postgres."""
    now = dt.datetime.now(dt.timezone.utc)
    window_start = now - dt.timedelta(days=window_days)
    prior_start = now - dt.timedelta(days=window_days * 2)
    today = now.date()

    # ── Volume and value ─────────────────────────────────────────────────
    volume, value = session.execute(
        select(func.count(Transaction.id), func.coalesce(func.sum(Transaction.amount), 0))
        .where(Transaction.ingested_at >= window_start)
    ).one()

    prior_volume = session.scalar(
        select(func.count(Transaction.id)).where(
            Transaction.ingested_at >= prior_start,
            Transaction.ingested_at < window_start,
        )
    ) or 0

    # ── Match rate ───────────────────────────────────────────────────────
    # Each matched record consumes two transactions.
    matched_pairs = session.scalar(
        select(func.count(MatchedRecord.id)).where(MatchedRecord.matched_at >= window_start)
    ) or 0
    prior_pairs = session.scalar(
        select(func.count(MatchedRecord.id)).where(
            MatchedRecord.matched_at >= prior_start,
            MatchedRecord.matched_at < window_start,
        )
    ) or 0

    match_rate = _ratio(matched_pairs * 2, volume)
    prior_rate = _ratio(prior_pairs * 2, prior_volume)

    # ── Exceptions ───────────────────────────────────────────────────────
    open_exceptions = session.scalar(
        select(func.count(ExceptionQueue.id)).where(
            ExceptionQueue.state.in_([ExceptionState.OPEN, ExceptionState.SUGGESTED])
        )
    ) or 0

    prior_open = session.scalar(
        select(func.count(ExceptionQueue.id)).where(
            ExceptionQueue.created_at >= prior_start,
            ExceptionQueue.created_at < window_start,
        )
    ) or 0

    resolved, rejected = session.execute(
        select(
            func.count(case((ExceptionQueue.state == ExceptionState.RESOLVED, 1))),
            func.count(case((ExceptionQueue.state == ExceptionState.REJECTED, 1))),
        ).where(ExceptionQueue.resolved_at >= window_start)
    ).one()

    # ── Validation ───────────────────────────────────────────────────────
    passed, quarantined = session.execute(
        select(
            func.count(case((ValidationLog.status == ValidationState.PASSED, 1))),
            func.count(case((ValidationLog.status == ValidationState.QUARANTINED, 1))),
        ).where(ValidationLog.created_at >= window_start)
    ).one()

    quarantined_today = session.scalar(
        select(func.count(ValidationLog.id)).where(
            ValidationLog.status == ValidationState.QUARANTINED,
            cast(ValidationLog.created_at, Date) == today,
        )
    ) or 0

    # ── Reconciliation runs ──────────────────────────────────────────────
    last_run = session.scalars(
        select(ReconciliationRun).order_by(ReconciliationRun.started_at.desc()).limit(1)
    ).first()

    avg_latency = session.scalar(
        select(func.avg(ReconciliationRun.duration_ms)).where(
            ReconciliationRun.started_at >= window_start,
            ReconciliationRun.status == "COMPLETED",
        )
    )

    failed_runs = session.scalar(
        select(func.count(ReconciliationRun.id)).where(
            ReconciliationRun.started_at >= window_start,
            ReconciliationRun.status == "FAILED",
        )
    ) or 0

    return {
        "window_days": window_days,
        "total_transactions": int(volume or 0),
        "total_value": float(value or 0),
        "currency": "USD",
        "match_rate": match_rate,
        "match_rate_delta": (
            round(match_rate - prior_rate, 4)
            if match_rate is not None and prior_rate is not None
            else None
        ),
        "volume_delta": _delta(volume, prior_volume),
        "open_exceptions": int(open_exceptions),
        "open_exceptions_delta": _delta(open_exceptions, prior_open),
        "auto_resolved_rate": _ratio(resolved, resolved + rejected),
        "validation_detection_rate": _ratio(quarantined, passed + quarantined),
        "quarantined_today": int(quarantined_today),
        "avg_reconcile_latency_ms": (
            round(float(avg_latency)) if avg_latency is not None else None
        ),
        "reconciliation_status": _status(last_run, failed_runs, open_exceptions),
        "last_run_at": last_run.started_at.isoformat() if last_run else None,
        # Nothing in this system schedules passes yet. A guessed timestamp
        # would be a fabrication, so the field stays null.
        "next_run_at": None,
        "generated_at": now.isoformat(),
    }


def _delta(current: float, prior: float) -> float | None:
    if not prior:
        return None
    return round((current / prior) - 1, 4)


def _status(last_run, failed_runs: int, open_exceptions: int) -> str:
    """HEALTHY / ATTENTION / UNKNOWN, from observable facts only."""
    if last_run is None:
        return "UNKNOWN"       # no pass has ever run
    if failed_runs > 0 or last_run.status == "FAILED":
        return "ATTENTION"
    staleness = dt.datetime.now(dt.timezone.utc) - last_run.started_at
    if staleness > dt.timedelta(hours=24):
        return "ATTENTION"     # reconciliation has stalled
    return "HEALTHY"


def match_rate_series(
    session: Session,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Daily time series for the chart (Sec. 12).

    Bucketed on `transactions.txn_date` so the series follows the business date
    a finance user reasons about, not when a row happened to be ingested.
    """
    date_to = date_to or dt.date.today()
    date_from = date_from or (date_to - dt.timedelta(days=89))

    volumes = dict(
        session.execute(
            select(Transaction.txn_date, func.count(Transaction.id))
            .where(Transaction.txn_date.between(date_from, date_to))
            .group_by(Transaction.txn_date)
        ).all()
    )

    matched_rows = session.execute(
        select(
            Transaction.txn_date,
            func.count(MatchedRecord.id),
            func.count(case((MatchedRecord.match_type == MatchType.RULE, 1))),
            func.count(case((MatchedRecord.match_type == MatchType.ML, 1))),
        )
        .join(Transaction, Transaction.id == MatchedRecord.transaction_id)
        .where(Transaction.txn_date.between(date_from, date_to))
        .group_by(Transaction.txn_date)
    ).all()
    matched = {row[0]: (row[1], row[2], row[3]) for row in matched_rows}

    latency = dict(
        session.execute(
            select(
                cast(ReconciliationRun.started_at, Date),
                func.avg(ReconciliationRun.duration_ms),
            )
            .where(ReconciliationRun.status == "COMPLETED")
            .group_by(cast(ReconciliationRun.started_at, Date))
        ).all()
    )

    series = []
    cursor = date_from
    while cursor <= date_to:
        volume = int(volumes.get(cursor, 0))
        pairs, rule_pairs, ml_pairs = matched.get(cursor, (0, 0, 0))
        matched_count = min(volume, int(pairs) * 2)

        series.append(
            {
                "date": cursor.isoformat(),
                "volume": volume,
                "matched": matched_count,
                "unmatched": max(0, volume - matched_count),
                "rule_matched": int(rule_pairs) * 2,
                "ml_matched": int(ml_pairs) * 2,
                "match_rate": _ratio(matched_count, volume),
                "avg_latency_ms": (
                    round(float(latency[cursor])) if cursor in latency else None
                ),
            }
        )
        cursor += dt.timedelta(days=1)

    return series


def category_breakdown(session: Session) -> list[dict[str, Any]]:
    """Exception counts and exposure per category, for the mix chart."""
    rows = session.execute(
        select(
            ExceptionQueue.category,
            func.count(ExceptionQueue.id),
            func.coalesce(func.sum(Transaction.amount), 0),
        )
        .join(Transaction, Transaction.id == ExceptionQueue.transaction_id)
        .where(ExceptionQueue.state.in_([ExceptionState.OPEN, ExceptionState.SUGGESTED]))
        .group_by(ExceptionQueue.category)
    ).all()

    return [
        {
            "category": row[0].value if row[0] else "UNCLASSIFIED",
            "count": int(row[1]),
            "value": float(row[2]),
        }
        for row in rows
    ]


__all__ = [
    "MetricsCache",
    "kpi_summary",
    "match_rate_series",
    "category_breakdown",
    "CACHE_TTL_SECONDS",
]
