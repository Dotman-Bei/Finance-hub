"""Audit-ready PDF generation (build.md Sec. 12, Sec. 3.4.2).

    "Jinja2 renders the report layout; ReportLab produces the PDF containing
     reconciliation summaries, exception logs, and match-rate analytics.
     Persist every generated report with its ID for provenance."

Jinja2 owns the wording and the ordering; ReportLab owns typesetting. The
template emits a line-oriented script (##H1, ##TABLE:name, ...) which this
module turns into ReportLab flowables - see the template header for why that is
line-oriented rather than HTML.

Provenance is the point of the exercise. Every report row stores the parameters
it was asked for, the figures as they stood at generation time, and the bytes
that were produced. Regenerating tomorrow will legitimately give different
numbers; the stored copy is what was signed off.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy.orm import Session

from shared.models.enums import ExceptionState
from shared.models.orm import ExceptionQueue, Report, Transaction

from .metrics import category_breakdown, kpi_summary, match_rate_series

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

REPORT_TYPES = {
    "RECONCILIATION_SUMMARY": "Reconciliation Summary",
    "EXCEPTION_LOG": "Exception Log & Resolutions",
    "MATCH_RATE_ANALYTICS": "Match-Rate Analytics",
    "AUDIT_TRAIL": "Full Audit Trail",
}

BRAND = colors.HexColor("#FF8A65")
INK = colors.HexColor("#000000")
MUTED = colors.HexColor("#6A6A75")
HAIRLINE = colors.HexColor("#D5D5DC")

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    undefined=StrictUndefined,   # a missing variable must fail, not render blank
    trim_blocks=True,
    lstrip_blocks=True,
)


# ── Data gathering ───────────────────────────────────────────────────────


def _fmt(value: Any, kind: str = "text") -> str:
    """Render a value, or an em dash when it genuinely is not available."""
    if value is None:
        return "—"
    if kind == "pct":
        return f"{value:.2%}"
    if kind == "int":
        return f"{int(value):,}"
    if kind == "money":
        return f"{value:,.2f}"
    return str(value)


def gather(
    session: Session,
    period_start: dt.date | None,
    period_end: dt.date | None,
    exception_limit: int = 200,
) -> dict[str, Any]:
    """Everything the template needs, read once."""
    kpi = kpi_summary(session)
    series = match_rate_series(session, period_start, period_end)
    categories = category_breakdown(session)

    query = (
        session.query(ExceptionQueue, Transaction)
        .join(Transaction, Transaction.id == ExceptionQueue.transaction_id)
        .order_by(ExceptionQueue.created_at.desc())
    )
    if period_start:
        query = query.filter(Transaction.txn_date >= period_start)
    if period_end:
        query = query.filter(Transaction.txn_date <= period_end)

    exceptions = []
    for row, txn in query.limit(exception_limit).all():
        suggestion = row.suggested_resolution or {}
        exceptions.append(
            {
                "id": str(row.id)[:8].upper(),
                "external_id": txn.external_id or "—",
                "amount": float(txn.amount),
                "currency": txn.currency,
                "txn_date": txn.txn_date.isoformat(),
                "category": row.category.value if row.category else "UNCLASSIFIED",
                "state": row.state.value,
                "confidence": (
                    float(row.classifier_confidence)
                    if row.classifier_confidence is not None
                    else None
                ),
                "pathway": suggestion.get("pathway", "—"),
                "resolved_by": row.resolved_by or "—",
            }
        )

    return {
        "kpi": kpi,
        "series": series,
        "categories": categories,
        "exceptions": exceptions,
    }


# ── Table builders ───────────────────────────────────────────────────────


def _kpi_table(data: dict[str, Any]) -> list[list[str]]:
    kpi = data["kpi"]
    return [
        ["Metric", "Value"],
        ["Transactions in period", _fmt(kpi["total_transactions"], "int")],
        ["Total value", f"{kpi['currency']} {_fmt(kpi['total_value'], 'money')}"],
        ["Overall match rate", _fmt(kpi["match_rate"], "pct")],
        ["Rule vs ML split", _rule_ml_split(data["series"])],
        ["Open exceptions", _fmt(kpi["open_exceptions"], "int")],
        ["Auto-resolved rate", _fmt(kpi["auto_resolved_rate"], "pct")],
        ["Reconciliation status", kpi["reconciliation_status"]],
        ["Last pass", _fmt(kpi["last_run_at"])],
        ["Average pass latency", f"{_fmt(kpi['avg_reconcile_latency_ms'])} ms"],
    ]


def _rule_ml_split(series: list[dict[str, Any]]) -> str:
    rule = sum(row["rule_matched"] for row in series)
    ml = sum(row["ml_matched"] for row in series)
    if not (rule + ml):
        return "—"
    return f"{rule:,} rule / {ml:,} ML ({rule / (rule + ml):.0%} deterministic)"


def _match_rate_table(data: dict[str, Any]) -> list[list[str]]:
    rows = [r for r in data["series"] if r["volume"]]
    if not rows:
        return [["Date", "Volume", "Matched", "Unmatched", "Rate"],
                ["—", "—", "—", "—", "—"]]

    # Weekly buckets keep a 90-day report readable.
    step = max(1, len(rows) // 20)
    table = [["Date", "Volume", "Matched", "Unmatched", "Rate"]]
    for row in rows[::step]:
        table.append([
            row["date"],
            _fmt(row["volume"], "int"),
            _fmt(row["matched"], "int"),
            _fmt(row["unmatched"], "int"),
            _fmt(row["match_rate"], "pct"),
        ])
    return table


def _exceptions_table(data: dict[str, Any]) -> list[list[str]]:
    table = [["Ref", "Transaction", "Amount", "Category", "State", "Pathway"]]
    for item in data["exceptions"][:60]:
        table.append([
            item["id"],
            item["external_id"],
            f"{item['currency']} {item['amount']:,.2f}",
            item["category"].replace("_", " ").title(),
            item["state"].title(),
            item["pathway"][:52] + ("…" if len(item["pathway"]) > 52 else ""),
        ])
    if len(data["exceptions"]) > 60:
        table.append(["", f"... and {len(data['exceptions']) - 60} more", "", "", "", ""])
    return table


def _categories_table(data: dict[str, Any]) -> list[list[str]]:
    total = sum(c["count"] for c in data["categories"]) or 1
    table = [["Category", "Count", "Share", "Exposure"]]
    for entry in sorted(data["categories"], key=lambda c: -c["count"]):
        table.append([
            entry["category"].replace("_", " ").title(),
            _fmt(entry["count"], "int"),
            f"{entry['count'] / total:.0%}",
            _fmt(entry["value"], "money"),
        ])
    if len(table) == 1:
        table.append(["—", "—", "—", "—"])
    return table


def _validation_table(data: dict[str, Any]) -> list[list[str]]:
    kpi = data["kpi"]
    return [
        ["Metric", "Value"],
        ["Detection rate", _fmt(kpi["validation_detection_rate"], "pct")],
        ["Quarantined today", _fmt(kpi["quarantined_today"], "int")],
        ["Objective 2 target", "≥ 98.00%"],
    ]


TABLES = {
    "kpi": _kpi_table,
    "match_rate": _match_rate_table,
    "exceptions": _exceptions_table,
    "categories": _categories_table,
    "validation": _validation_table,
}


# ── Rendering ────────────────────────────────────────────────────────────


def _styles():
    base = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle(
            "H1", parent=base["Heading1"], fontName="Helvetica-Bold",
            fontSize=20, leading=24, textColor=INK, spaceAfter=4,
        ),
        "h2": ParagraphStyle(
            "H2", parent=base["Heading2"], fontName="Helvetica-Bold",
            fontSize=12, leading=15, textColor=INK, spaceBefore=10, spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "Body", parent=base["BodyText"], fontName="Helvetica",
            fontSize=9, leading=13, textColor=MUTED, alignment=TA_LEFT,
        ),
    }


def _table_style() -> TableStyle:
    return TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("TEXTCOLOR", (0, 0), (-1, 0), INK),
        ("TEXTCOLOR", (0, 1), (-1, -1), MUTED),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, HAIRLINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ])


def _decorate(canvas, doc, title: str, report_id: str) -> None:
    """Brand rule at the top, page number and provenance in the footer."""
    canvas.saveState()
    width, height = A4

    canvas.setStrokeColor(BRAND)
    canvas.setLineWidth(2)
    canvas.line(20 * mm, height - 15 * mm, width - 20 * mm, height - 15 * mm)

    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(20 * mm, 12 * mm, f"FinanceHub · {title}")
    canvas.drawRightString(width - 20 * mm, 12 * mm, f"Report {report_id} · page {doc.page}")
    canvas.restoreState()


def render_pdf(context: dict[str, Any], data: dict[str, Any]) -> bytes:
    """Run the Jinja2 template through ReportLab and return the PDF bytes."""
    template = _env.get_template("reconciliation_summary.txt.j2")
    script = template.render(**context, **data)

    styles = _styles()
    story: list[Any] = []

    for raw_line in script.splitlines():
        line = raw_line.rstrip()

        if not line.strip():
            continue
        if line.startswith("##H1 "):
            story.append(Paragraph(line[5:].strip(), styles["h1"]))
        elif line.startswith("##H2 "):
            story.append(Paragraph(line[5:].strip(), styles["h2"]))
        elif line.startswith("##TABLE:"):
            name = line[len("##TABLE:"):].strip()
            builder = TABLES.get(name)
            if builder is None:
                raise ValueError(f"template references unknown table {name!r}")
            rows = builder(data)
            table = Table(rows, hAlign="LEFT", repeatRows=1)
            table.setStyle(_table_style())
            story.append(KeepTogether(table) if len(rows) <= 12 else table)
            story.append(Spacer(1, 6))
        elif line.startswith("##SPACER"):
            story.append(Spacer(1, 10))
        elif line.startswith("##PAGEBREAK"):
            story.append(PageBreak())
        else:
            story.append(Paragraph(line, styles["body"]))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=22 * mm, bottomMargin=20 * mm,
        title=context["title"], author="FinanceHub",
        subject="Reconciliation report", creator="FinanceHub reporting_api",
    )

    def decorate(canvas, d):
        _decorate(canvas, d, context["title"], context["report_id"])

    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buffer.getvalue()


def generate(
    session: Session,
    report_type: str,
    generated_by: str,
    period_start: dt.date | None = None,
    period_end: dt.date | None = None,
    title: str | None = None,
) -> Report:
    """Generate, persist and return the report row (Sec. 3.4.2 provenance)."""
    if report_type not in REPORT_TYPES:
        raise ValueError(
            f"unknown report type {report_type!r}; expected one of "
            f"{sorted(REPORT_TYPES)}"
        )

    data = gather(session, period_start, period_end)

    row = Report(
        name=title or f"{REPORT_TYPES[report_type]} — {period_start or 'inception'} to "
        f"{period_end or dt.date.today()}",
        report_type=report_type,
        period_start=period_start,
        period_end=period_end,
        generated_by=generated_by,
        status="GENERATING",
        parameters={
            "report_type": report_type,
            "period_start": period_start.isoformat() if period_start else None,
            "period_end": period_end.isoformat() if period_end else None,
        },
    )
    session.add(row)
    session.flush()   # the id goes into the PDF footer, so it must exist first

    context = {
        "title": row.name,
        "report_id": str(row.id)[:8].upper(),
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "generated_by": generated_by,
        "period_start": period_start.isoformat() if period_start else None,
        "period_end": period_end.isoformat() if period_end else None,
    }

    try:
        pdf = render_pdf(context, data)
    except Exception as exc:
        # The row survives with FAILED status: a report that could not be
        # produced is itself an auditable event.
        row.status = "FAILED"
        row.summary = {"error": f"{type(exc).__name__}: {exc}"}
        session.commit()
        logger.exception("Report %s failed to render", row.id)
        raise

    row.content = pdf
    row.size_bytes = len(pdf)
    row.status = "READY"
    # The figures as they stood, so the numbers can be checked against the PDF
    # without regenerating (which would give different, later, values).
    row.summary = {
        "kpi": data["kpi"],
        "exception_count": len(data["exceptions"]),
        "categories": data["categories"],
    }

    session.commit()
    logger.info("Generated report %s (%s, %d bytes)", row.id, report_type, len(pdf))
    return row


__all__ = ["generate", "render_pdf", "gather", "REPORT_TYPES", "TEMPLATE_DIR"]
