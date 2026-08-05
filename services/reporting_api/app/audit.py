"""Audit trail access and integrity checking (build.md Sec. 3.3.2, Sec. 16).

Sec. 16's Phase 6 milestone requires the audit-report gate to pass. That needs
two things this module provides: a way to *read* the trail, and a way to prove
it is *complete*.

The read side closes a real gap - `Permission.VIEW_AUDIT_TRAIL` was granted to
the Auditor role with no endpoint behind it, so the one role defined by its
need to inspect records had no way to inspect them.

The integrity side matters more. `audittrail` is written by the
`trg_exception_audit` database trigger, not by application code, which is what
makes it tamper-evident: a service cannot skip a row by forgetting to log. But
"the trigger exists" is an assumption until something checks that every state
change actually produced a row. `integrity_report` does that by comparing
resolved exceptions against their audit entries.

Everything here is read-only. `audittrail` is append-only by design - exposing
any mutation would defeat the property the table exists for.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shared.models.enums import ExceptionState
from shared.models.orm import AuditTrail, ExceptionQueue

logger = logging.getLogger(__name__)


def query_trail(
    session: Session,
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor: str | None = None,
    action: str | None = None,
    since: dt.datetime | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """Filtered slice of the audit trail, newest first."""
    stmt = select(AuditTrail)

    if entity_type:
        stmt = stmt.where(AuditTrail.entity_type == entity_type)
    if entity_id:
        try:
            stmt = stmt.where(AuditTrail.entity_id == UUID(str(entity_id)))
        except (ValueError, AttributeError):
            # A malformed id matches nothing; saying so beats a 500.
            return {"total": 0, "count": 0, "offset": offset, "items": []}
    if actor:
        stmt = stmt.where(AuditTrail.actor == actor)
    if action:
        stmt = stmt.where(AuditTrail.action == action)
    if since:
        stmt = stmt.where(AuditTrail.created_at >= since)

    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(
        stmt.order_by(AuditTrail.created_at.desc()).offset(offset).limit(limit)
    ).all()

    return {
        "total": int(total),
        "count": len(rows),
        "offset": offset,
        "items": [_serialise(row) for row in rows],
    }


def _serialise(row: AuditTrail) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "entity_type": row.entity_type,
        "entity_id": str(row.entity_id),
        "action": row.action,
        "actor": row.actor,
        "old_state": row.old_state,
        "new_state": row.new_state,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        # What actually changed, so a reviewer does not have to diff two JSONB
        # blobs by eye.
        "changed_fields": _diff(row.old_state, row.new_state),
    }


def _diff(old: dict | None, new: dict | None) -> list[dict[str, Any]]:
    """Fields that differ between the two states.

    Timestamps that move on every write are excluded - they are noise in a
    change log, not signal.
    """
    if not isinstance(old, dict) or not isinstance(new, dict):
        return []

    ignored = {"updated_at"}
    changes = []
    for key in sorted(set(old) | set(new)):
        if key in ignored:
            continue
        before, after = old.get(key), new.get(key)
        if before != after:
            changes.append({"field": key, "from": before, "to": after})
    return changes


def integrity_report(session: Session, sample_limit: int = 25) -> dict[str, Any]:
    """Check that every settled exception left an audit trail.

    The trigger fires on UPDATE to exceptionqueue, so any row that reached
    RESOLVED or REJECTED must have at least one audittrail entry. A gap means
    either the trigger is missing from this database or a write bypassed it -
    both of which invalidate the tamper-evidence claim, and neither of which
    shows up anywhere else.

    Reported as findings rather than raised: an operator needs the number and
    the examples, not an exception.
    """
    settled = select(ExceptionQueue.id).where(
        ExceptionQueue.state.in_([ExceptionState.RESOLVED, ExceptionState.REJECTED])
    )
    settled_ids = set(session.scalars(settled).all())

    audited_ids = set(
        session.scalars(
            select(AuditTrail.entity_id).where(AuditTrail.entity_type == "exceptionqueue")
        ).all()
    )

    missing = sorted(settled_ids - audited_ids, key=str)
    total_rows = session.scalar(select(func.count(AuditTrail.id))) or 0

    # An audit row whose actor is unknown is weaker evidence: the trigger falls
    # back to 'system' when resolved_by was not set before the update.
    system_attributed = session.scalar(
        select(func.count(AuditTrail.id)).where(AuditTrail.actor == "system")
    ) or 0

    covered = len(settled_ids) - len(missing)
    coverage = covered / len(settled_ids) if settled_ids else None

    return {
        "audit_rows": int(total_rows),
        "settled_exceptions": len(settled_ids),
        "audited_exceptions": covered,
        # None, not 1.0, when nothing has settled yet - "no data" and "perfect"
        # are different findings.
        "coverage": round(coverage, 4) if coverage is not None else None,
        "complete": not missing,
        "missing_count": len(missing),
        "missing_sample": [str(i) for i in missing[:sample_limit]],
        "system_attributed_rows": int(system_attributed),
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def actor_activity(session: Session, limit: int = 50) -> list[dict[str, Any]]:
    """Who has been changing records, and when they last did.

    The first thing an auditor asks of a change log.
    """
    rows = session.execute(
        select(
            AuditTrail.actor,
            func.count(AuditTrail.id),
            func.min(AuditTrail.created_at),
            func.max(AuditTrail.created_at),
        )
        .group_by(AuditTrail.actor)
        .order_by(func.count(AuditTrail.id).desc())
        .limit(limit)
    ).all()

    return [
        {
            "actor": row[0],
            "changes": int(row[1]),
            "first_seen": row[2].isoformat() if row[2] else None,
            "last_seen": row[3].isoformat() if row[3] else None,
        }
        for row in rows
    ]


__all__ = ["query_trail", "integrity_report", "actor_activity"]
