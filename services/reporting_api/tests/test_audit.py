"""Audit trail unit tests (build.md Sec. 3.3.2, Sec. 16).

The database-backed behaviour — the trigger firing, integrity across real rows
— lives in tests/test_integration_db.py, which needs Postgres. These cover the
pure logic and, importantly, the access control: the audit trail is the one
surface where "who can read this" is itself the requirement.
"""

from __future__ import annotations

from services.reporting_api.app.audit import _diff
from services.reporting_api.app.auth import Permission, Principal, Role


# ── Who may read the trail ───────────────────────────────────────────────


def test_auditor_can_read_the_audit_trail():
    assert Principal("a@x.io", Role.AUDITOR).can(Permission.VIEW_AUDIT_TRAIL)


def test_administrator_can_read_the_audit_trail():
    assert Principal("s@x.io", Role.SYSTEM_ADMINISTRATOR).can(Permission.VIEW_AUDIT_TRAIL)


def test_finance_manager_cannot_read_the_audit_trail():
    """Separation of duties: the role that changes records is not the role that
    reviews the log of changes."""
    manager = Principal("m@x.io", Role.FINANCE_MANAGER)
    assert manager.can(Permission.RESOLVE_EXCEPTIONS)
    assert not manager.can(Permission.VIEW_AUDIT_TRAIL)


def test_the_permission_has_at_least_one_holder():
    """A permission nobody holds is dead code; a route behind it is unreachable.
    This is the gap that existed before the /audit routes were added — the
    permission was granted with nothing serving it."""
    holders = [r for r in Role if Principal("x", r).can(Permission.VIEW_AUDIT_TRAIL)]
    assert holders


# ── Change diffing ───────────────────────────────────────────────────────


def test_diff_reports_changed_fields():
    changes = _diff(
        {"state": "OPEN", "category": None, "resolved_by": None},
        {"state": "RESOLVED", "category": "PARTIAL_PAYMENT", "resolved_by": "a@x.io"},
    )
    fields = {c["field"] for c in changes}
    assert fields == {"state", "category", "resolved_by"}

    state = next(c for c in changes if c["field"] == "state")
    assert state["from"] == "OPEN"
    assert state["to"] == "RESOLVED"


def test_diff_ignores_unchanged_fields():
    changes = _diff({"a": 1, "b": 2}, {"a": 1, "b": 3})
    assert [c["field"] for c in changes] == ["b"]


def test_diff_handles_added_and_removed_keys():
    changes = _diff({"only_old": 1}, {"only_new": 2})
    assert {c["field"] for c in changes} == {"only_old", "only_new"}


def test_diff_tolerates_a_missing_state():
    """The trigger writes NULL for OLD on some operations; the viewer must not
    fall over on it."""
    assert _diff(None, {"state": "OPEN"}) == []
    assert _diff({"state": "OPEN"}, None) == []
    assert _diff(None, None) == []


def test_diff_excludes_timestamp_noise():
    """A field that moves on every write is noise in a change log."""
    changes = _diff(
        {"state": "OPEN", "updated_at": "2026-01-01"},
        {"state": "RESOLVED", "updated_at": "2026-01-02"},
    )
    assert [c["field"] for c in changes] == ["state"]
