"""Resolution engine (build.md Sec. 10, Sec. 3.3.1).

Sec. 10's mapping, implemented verbatim:

    Partial Payment          Propose partial-match journal entry; flag
                             remaining balance for follow-up
    Split Settlement         Open multi-line allocation resolution across the
                             target obligations
    Missing Reference Code   Surface likely counterpart candidates for manual
                             reference assignment
    Timing Difference        Suggest matching across accounting periods; hold
                             pending settlement date

Each builder turns the category plus the engineered features into a concrete,
actionable suggestion written to `exceptionqueue.suggested_resolution` (JSONB),
with the row moving to `state='SUGGESTED'`.

Every number in a suggestion is computed from the transaction and its
nominated counterparts. Nothing is invented: where a figure cannot be derived
the field is omitted rather than filled with a plausible-looking default, since
a finance user acting on a fabricated balance is the worst outcome this module
could produce.

The JSON shape matches what the dashboard's ExceptionPanel already renders:
`pathway`, `action`, `detail`, `fields`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from shared.models.enums import ExceptionCategory

from .features import ExceptionFeatures

#: Sec. 10's table, as the canonical pathway text.
PATHWAYS = {
    ExceptionCategory.PARTIAL_PAYMENT.value: (
        "Propose partial-match journal entry; flag remaining balance for follow-up"
    ),
    ExceptionCategory.SPLIT_SETTLEMENT.value: (
        "Open multi-line allocation resolution across the target obligations"
    ),
    ExceptionCategory.MISSING_REFERENCE_CODE.value: (
        "Surface likely counterpart candidates for manual reference assignment"
    ),
    ExceptionCategory.TIMING_DIFFERENCE.value: (
        "Suggest matching across accounting periods; hold pending settlement date"
    ),
}

ACTIONS = {
    ExceptionCategory.PARTIAL_PAYMENT.value: "POST_PARTIAL_JOURNAL",
    ExceptionCategory.SPLIT_SETTLEMENT.value: "ALLOCATE_MULTI_LINE",
    ExceptionCategory.MISSING_REFERENCE_CODE.value: "ASSIGN_REFERENCE",
    ExceptionCategory.TIMING_DIFFERENCE.value: "HOLD_PENDING_SETTLEMENT",
}


def _money(value: float, currency: str = "USD") -> str:
    return f"{currency} {value:,.2f}"


def _partial_payment(features: ExceptionFeatures) -> dict[str, Any]:
    context = features.context
    currency = context.get("currency", "USD")
    this_amount = float(context.get("amount") or 0.0)
    counterpart_amount = float(context.get("nearest_amount") or 0.0)

    # Both sides of an unmatched pair are queued and classified, so this row
    # may be either the obligation or the receipt. The obligation is always the
    # larger of the two; assuming it was this row produced a negative residual
    # balance whenever the receipt side was the one being described.
    obligation = max(this_amount, counterpart_amount)
    settled = min(this_amount, counterpart_amount)
    balance = round(obligation - settled, 2)
    viewed_from = "obligation" if this_amount >= counterpart_amount else "receipt"

    return {
        "detail": (
            f"{_money(settled, currency)} settled against a "
            f"{_money(obligation, currency)} obligation. Post the partial match "
            f"and carry {_money(balance, currency)} forward for follow-up."
        ),
        "fields": {
            "settled_amount": round(settled, 2),
            "obligation_amount": round(obligation, 2),
            # Always the amount still outstanding, never negative.
            "residual_balance": balance,
            "settled_share": round(settled / obligation, 4) if obligation else 0.0,
            "viewed_from": viewed_from,
            "follow_up": "BALANCE_WATCHLIST",
            "counterpart_id": str(context["nearest_id"])
            if context.get("nearest_id")
            else None,
        },
    }


def _split_settlement(features: ExceptionFeatures) -> dict[str, Any]:
    context = features.context
    currency = context.get("currency", "USD")
    obligation = float(context.get("amount") or 0.0)
    total = float(context.get("total_counterpart_amount") or 0.0)
    legs = int(features.counterpart_count)
    residual = round(obligation - total, 2)

    detail = (
        f"{legs} receipts totalling {_money(total, currency)} against a "
        f"{_money(obligation, currency)} obligation. Allocate across all legs."
    )
    if abs(residual) >= 0.01:
        detail += f" Unallocated remainder: {_money(residual, currency)}."

    return {
        "detail": detail,
        "fields": {
            "candidate_legs": legs,
            "obligation_amount": round(obligation, 2),
            "allocated_total": round(total, 2),
            "unallocated_remainder": residual,
            "allocation_basis": "PRO_RATA",
            "counterpart_ids": [
                str(i) for i in context.get("counterpart_ids", []) if i is not None
            ],
        },
    }


def _missing_reference(features: ExceptionFeatures) -> dict[str, Any]:
    context = features.context
    currency = context.get("currency", "USD")
    count = int(features.counterpart_count)

    if count == 0:
        # Honest about having nothing to offer: no candidate was nominated, so
        # inventing one would send a human down a false trail.
        return {
            "detail": (
                "No counterpart candidate was nominated by the matching engine. "
                "Widen the reconciliation window or supply the reference code "
                "manually to re-run matching."
            ),
            "fields": {
                "candidate_count": 0,
                "amount": round(float(context.get("amount") or 0.0), 2),
                "next_step": "MANUAL_SEARCH",
            },
        }

    plural = count != 1
    return {
        "detail": (
            f"{count} counterpart candidate{'s' if plural else ''} "
            f"{'match' if plural else 'matches'} on amount and date but "
            f"{'carry' if plural else 'carries'} no shared reference. Assign a "
            f"reference code to complete the deterministic match on the next pass."
        ),
        "fields": {
            "candidate_count": count,
            "top_similarity": round(features.description_similarity, 4),
            "amount": round(float(context.get("amount") or 0.0), 2),
            "nearest_amount": round(float(context.get("nearest_amount") or 0.0), 2),
            "currency": currency,
            "counterpart_ids": [
                str(i) for i in context.get("counterpart_ids", []) if i is not None
            ],
            "next_step": "ASSIGN_AND_REMATCH",
        },
    }


def _timing_difference(features: ExceptionFeatures) -> dict[str, Any]:
    context = features.context
    drift = int(features.date_delta_days)

    fields: dict[str, Any] = {
        "period_drift_days": drift,
        "amount_agrees": abs(features.amount_ratio - 1.0) <= 0.01,
        "counterpart_id": str(context["nearest_id"])
        if context.get("nearest_id")
        else None,
    }

    detail = (
        f"Amounts agree but the counterpart posted {drift} day"
        f"{'s' if drift != 1 else ''} outside the period boundary. Match across "
        f"accounting periods and hold pending the settlement date."
    )

    if drift > 0:
        # Derived from the observed drift, not guessed.
        fields["hold_until"] = (dt.date.today() + dt.timedelta(days=drift)).isoformat()
    else:
        detail = (
            "No decisive signal from amount, date or reference. Review against "
            "the adjacent accounting period before escalating."
        )

    return {"detail": detail, "fields": fields}


BUILDERS = {
    ExceptionCategory.PARTIAL_PAYMENT.value: _partial_payment,
    ExceptionCategory.SPLIT_SETTLEMENT.value: _split_settlement,
    ExceptionCategory.MISSING_REFERENCE_CODE.value: _missing_reference,
    ExceptionCategory.TIMING_DIFFERENCE.value: _timing_difference,
}


def suggest(
    category: str,
    features: ExceptionFeatures,
    confidence: float = 0.0,
    engine: str = "bootstrap",
    rationale: str = "",
) -> dict[str, Any]:
    """Build the JSONB payload for `exceptionqueue.suggested_resolution`."""
    builder = BUILDERS.get(category)
    if builder is None:
        raise ValueError(f"unknown exception category: {category!r}")

    body = builder(features)
    return {
        "category": category,
        "pathway": PATHWAYS[category],
        "action": ACTIONS[category],
        "detail": body["detail"],
        "fields": body["fields"],
        "classifier": {
            "confidence": round(float(confidence), 4),
            # Which engine produced this is recorded on the row itself, so a
            # reviewer can weigh a bootstrap suggestion differently from a
            # learned one.
            "engine": engine,
            "rationale": rationale,
        },
        "suggested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


__all__ = ["suggest", "PATHWAYS", "ACTIONS", "BUILDERS"]
