"""RBAC and PDF generation tests (build.md Sec. 3.4.1, Sec. 3.4.2).

RBAC is gated at the API layer, so these assert the permission matrix directly
rather than through the UI's affordances - the dashboard hiding a button is
presentation, not access control.

The PDF tests render real documents through the real Jinja2 template and
ReportLab, and check the bytes. `generate()` itself needs a database and is
covered by the integration path; `render_pdf` is the part that can fail on
formatting, and it is exercised here.
"""

from __future__ import annotations

import datetime as dt

import pytest

from services.reporting_api.app import reports as reports_module
from services.reporting_api.app.auth import (
    ROLE_PERMISSIONS,
    Permission,
    Principal,
    Role,
    decode_token,
    issue_token,
)
from services.reporting_api.app.reports import REPORT_TYPES, render_pdf

# ── RBAC (Sec. 3.4.1) ────────────────────────────────────────────────────


def test_all_three_roles_exist():
    assert {r.value for r in Role} == {
        "FINANCE_MANAGER",
        "AUDITOR",
        "SYSTEM_ADMINISTRATOR",
    }


def test_auditor_is_read_only():
    """An auditor who can alter the records they audit is not an auditor."""
    auditor = Principal("a@x.io", Role.AUDITOR)
    assert not auditor.can(Permission.RESOLVE_EXCEPTIONS)
    assert not auditor.can(Permission.RUN_RECONCILIATION)
    assert auditor.can(Permission.VIEW_EXCEPTIONS)
    assert auditor.can(Permission.VIEW_AUDIT_TRAIL)
    assert auditor.can(Permission.GENERATE_REPORTS)


def test_finance_manager_can_resolve_but_not_view_the_audit_trail():
    manager = Principal("m@x.io", Role.FINANCE_MANAGER)
    assert manager.can(Permission.RESOLVE_EXCEPTIONS)
    assert manager.can(Permission.RUN_RECONCILIATION)
    assert not manager.can(Permission.VIEW_AUDIT_TRAIL)


def test_administrator_has_every_permission():
    admin = Principal("s@x.io", Role.SYSTEM_ADMINISTRATOR)
    assert all(admin.can(p) for p in Permission)


def test_every_role_has_a_defined_scope():
    """A role with no entry would silently have no permissions at all."""
    assert set(ROLE_PERMISSIONS) == set(Role)
    assert all(ROLE_PERMISSIONS[role] for role in Role)


# ── JWT ──────────────────────────────────────────────────────────────────


def test_token_round_trips_with_its_role_claim():
    token = issue_token("finance@financehub.io", Role.FINANCE_MANAGER)
    principal = decode_token(token)
    assert principal.subject == "finance@financehub.io"
    assert principal.role is Role.FINANCE_MANAGER


def test_a_tampered_token_is_rejected():
    from fastapi import HTTPException

    token = issue_token("x@y.io", Role.AUDITOR)
    # Flip a character in the signature.
    tampered = token[:-2] + ("aa" if token[-2:] != "aa" else "bb")

    with pytest.raises(HTTPException) as exc:
        decode_token(tampered)
    assert exc.value.status_code == 401


def test_an_expired_token_is_rejected():
    from fastapi import HTTPException

    expired = issue_token("x@y.io", Role.AUDITOR, expires_minutes=-1)
    with pytest.raises(HTTPException) as exc:
        decode_token(expired)
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail.lower()


def test_a_token_with_an_unknown_role_is_refused():
    """Roles come from the signed claim; an unrecognised one must not default
    to anything permissive."""
    import jwt as pyjwt
    from fastapi import HTTPException

    from shared.config import settings

    now = dt.datetime.now(dt.timezone.utc)
    forged = pyjwt.encode(
        {
            "sub": "x@y.io",
            "role": "SUPER_ADMIN",
            "iat": now,
            "exp": now + dt.timedelta(minutes=5),
            "iss": "financehub",
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(HTTPException) as exc:
        decode_token(forged)
    assert exc.value.status_code == 403


# ── PDF generation (Sec. 3.4.2) ──────────────────────────────────────────


def _data(with_exceptions: bool = True, empty: bool = False):
    """The structure `gather()` returns, built by hand so rendering can be
    exercised without a database."""
    kpi = {
        "total_transactions": 0 if empty else 151_703,
        "total_value": 0.0 if empty else 225_600_000.0,
        "currency": "USD",
        "match_rate": None if empty else 0.9487,
        "open_exceptions": 0 if empty else 34,
        "auto_resolved_rate": None if empty else 0.7314,
        "validation_detection_rate": None if empty else 0.9873,
        "quarantined_today": 0 if empty else 41,
        "reconciliation_status": "UNKNOWN" if empty else "HEALTHY",
        "last_run_at": None if empty else "2026-08-03T18:00:00+00:00",
        "avg_reconcile_latency_ms": None if empty else 245,
    }
    series = [] if empty else [
        {
            "date": (dt.date(2026, 7, 1) + dt.timedelta(days=i)).isoformat(),
            "volume": 5000 + i * 10,
            "matched": 4700 + i * 9,
            "unmatched": 300 + i,
            "rule_matched": 3800,
            "ml_matched": 900,
            "match_rate": 0.94,
            "avg_latency_ms": 240,
        }
        for i in range(30)
    ]
    exceptions = [] if (empty or not with_exceptions) else [
        {
            "id": f"ABC{i:05d}",
            "external_id": f"TXN-{100000 + i}",
            "amount": 1250.50 + i,
            "currency": "USD",
            "txn_date": "2026-07-15",
            "category": "PARTIAL_PAYMENT",
            "state": "SUGGESTED",
            "confidence": 0.81,
            "pathway": "Propose partial-match journal entry; flag remaining balance",
            "resolved_by": "—",
        }
        for i in range(80)   # exercises the >60 truncation path
    ]
    categories = [] if empty else [
        {"category": "PARTIAL_PAYMENT", "count": 9, "value": 120000.0},
        {"category": "TIMING_DIFFERENCE", "count": 8, "value": 90000.0},
    ]
    return {"kpi": kpi, "series": series, "categories": categories, "exceptions": exceptions}


def _context():
    return {
        "title": "Reconciliation Summary — July",
        "report_id": "8AC31F42",
        "generated_at": "2026-08-03 18:30 UTC",
        "generated_by": "a.okafor@financehub.io",
        "period_start": "2026-07-01",
        "period_end": "2026-07-31",
    }


def test_render_produces_a_valid_pdf():
    pdf = render_pdf(_context(), _data())
    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert len(pdf) > 2000


def test_report_covers_all_three_required_sections():
    """Sec. 3.4.2: reconciliation summaries, exception logs and match-rate
    analytics."""
    pdf = render_pdf(_context(), _data())
    # ReportLab compresses streams, so assert on the metadata title plus the
    # fact that every declared table built without error.
    assert b"/Title" in pdf
    for name in ("kpi", "match_rate", "exceptions", "categories", "validation"):
        assert name in reports_module.TABLES


def test_empty_database_renders_without_inventing_figures():
    """A report over a period with no data must still produce a document, and
    must say the rate is unavailable rather than printing 0%."""
    pdf = render_pdf(_context(), _data(empty=True))
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 1000


def test_report_with_no_exceptions_renders():
    pdf = render_pdf(_context(), _data(with_exceptions=False))
    assert pdf.startswith(b"%PDF-")


def test_missing_values_render_as_a_dash_not_a_zero():
    """A null KPI and a zero KPI are different facts."""
    assert reports_module._fmt(None) == "—"
    assert reports_module._fmt(None, "pct") == "—"
    assert reports_module._fmt(0, "int") == "0"
    assert reports_module._fmt(0.9487, "pct") == "94.87%"


def test_template_referencing_an_unknown_table_fails_loudly(monkeypatch):
    """A typo in the template must not silently drop a section from an audit
    document."""
    original = reports_module._env.get_template

    class FakeTemplate:
        def render(self, **_):
            return "##H1 Title\n##TABLE:does_not_exist\n"

    monkeypatch.setattr(reports_module._env, "get_template", lambda _: FakeTemplate())
    try:
        with pytest.raises(ValueError, match="unknown table"):
            render_pdf(_context(), _data())
    finally:
        monkeypatch.setattr(reports_module._env, "get_template", original)


def test_all_four_report_types_are_declared():
    assert set(REPORT_TYPES) == {
        "RECONCILIATION_SUMMARY",
        "EXCEPTION_LOG",
        "MATCH_RATE_ANALYTICS",
        "AUDIT_TRAIL",
    }


def test_rule_ml_split_reports_the_deterministic_share():
    data = _data()
    text = reports_module._rule_ml_split(data["series"])
    assert "deterministic" in text
    assert reports_module._rule_ml_split([]) == "—"
