"""Feature engineering for the exception classifier (build.md Sec. 10).

    "Features engineered from each unmatched transaction: amount ratio vs.
     nearest candidate, description similarity, presence of reference code,
     date delta."

Those four are the core and are named explicitly below. A handful of
corroborating features are added alongside them, each justified in place -
a Random Forest handles extra columns gracefully, and the four alone cannot
separate a split settlement from a partial payment.

The nearest candidate comes from the matching engine, which records it on the
queue row as `suggested_resolution.matching_engine.best_counterpart_id` along
with the confidence it reached. This module never re-runs matching; it reads
what Subsystem 1 already concluded.

Not in Sec. 3's file list. Split out from classifier.py so the features can be
tested in isolation - if a feature is computed wrongly, the classifier's
accuracy drops for reasons no confusion matrix would explain.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

#: Column order is the model's input contract. Appending is safe; reordering
#: or inserting silently invalidates every persisted model, so the list is
#: paired with FEATURE_VERSION and checked at load time.
FEATURE_NAMES = (
    # -- the four Sec. 10 names --
    "amount_ratio",
    "description_similarity",
    "has_reference_code",
    "date_delta_days",
    # -- corroborating --
    "counterpart_count",       # separates SPLIT_SETTLEMENT from PARTIAL_PAYMENT
    "amount_shortfall",        # signed gap; a partial payment is always short
    "best_confidence",         # how close the matching engine came
    "reference_agreement",     # both present and equal / present and differing
    "log_amount",              # magnitude; large-value items behave differently
    "is_internal",             # which side of the reconciliation this row is
)

FEATURE_VERSION = 1


@dataclass
class ExceptionFeatures:
    """One feature row, plus the context a resolution suggestion needs."""

    amount_ratio: float = 1.0
    description_similarity: float = 0.0
    has_reference_code: float = 0.0
    date_delta_days: float = 0.0
    counterpart_count: float = 0.0
    amount_shortfall: float = 0.0
    best_confidence: float = 0.0
    reference_agreement: float = 0.5
    log_amount: float = 0.0
    is_internal: float = 0.0

    #: Not features - carried so resolution.py can describe the suggestion.
    context: dict[str, Any] = field(default_factory=dict)

    def as_vector(self) -> list[float]:
        return [float(getattr(self, name)) for name in FEATURE_NAMES]

    def as_dict(self) -> dict[str, float]:
        return {name: float(getattr(self, name)) for name in FEATURE_NAMES}


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_date(value: Any) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _has_reference(row: dict[str, Any]) -> bool:
    reference = row.get("reference_code")
    return bool(reference and str(reference).strip())


def extract(
    transaction: dict[str, Any],
    counterparts: list[dict[str, Any]] | None = None,
    matching_context: dict[str, Any] | None = None,
    internal_sources: tuple[str, ...] = ("erp",),
) -> ExceptionFeatures:
    """Build the feature row for one unmatched transaction.

    `counterparts` are the candidate rows the matching engine nominated -
    usually one, occasionally several, which is exactly what distinguishes a
    split settlement.
    """
    counterparts = counterparts or []
    matching_context = matching_context or {}

    amount = _to_float(transaction.get("amount"))
    txn_date = _to_date(transaction.get("txn_date"))

    features = ExceptionFeatures()
    features.has_reference_code = 1.0 if _has_reference(transaction) else 0.0
    features.counterpart_count = float(len(counterparts))
    features.best_confidence = _to_float(matching_context.get("best_confidence"))
    features.is_internal = (
        1.0 if transaction.get("source_type") in internal_sources else 0.0
    )
    # log1p keeps a 12-figure settlement from dominating the split criteria
    # purely by magnitude, while preserving the ordering.
    features.log_amount = float(_safe_log1p(abs(amount)))

    if not counterparts:
        # No candidate at all. Ratio 0 and shortfall equal to the full amount
        # is the honest encoding: nothing was found to offset this row.
        features.amount_ratio = 0.0
        features.amount_shortfall = amount
        features.date_delta_days = 0.0
        features.context = {"counterpart_count": 0, "amount": amount}
        return features

    #: Split settlements offset one obligation with several receipts, so the
    #: comparison is against the *total* nominated, not the single best.
    total_counterpart = sum(_to_float(c.get("amount")) for c in counterparts)
    nearest = min(
        counterparts,
        key=lambda c: abs(_to_float(c.get("amount")) - amount),
    )
    nearest_amount = _to_float(nearest.get("amount"))

    if amount != 0:
        features.amount_ratio = round(nearest_amount / amount, 6)
        features.amount_shortfall = round(amount - total_counterpart, 2)
    else:
        features.amount_ratio = 0.0
        features.amount_shortfall = 0.0

    features.description_similarity = _to_float(
        matching_context.get("description_similarity"),
        default=_jaccard(
            transaction.get("description"), nearest.get("description")
        ),
    )

    nearest_date = _to_date(nearest.get("txn_date"))
    if txn_date and nearest_date:
        features.date_delta_days = float(abs((txn_date - nearest_date).days))

    features.reference_agreement = _reference_agreement(transaction, nearest)

    features.context = {
        "counterpart_count": len(counterparts),
        "amount": amount,
        "nearest_amount": nearest_amount,
        "total_counterpart_amount": round(total_counterpart, 2),
        "nearest_id": nearest.get("id"),
        "counterpart_ids": [c.get("id") for c in counterparts],
        "currency": transaction.get("currency", "USD"),
    }
    return features


def _reference_agreement(left: dict[str, Any], right: dict[str, Any]) -> float:
    """1.0 both present and equal, 0.0 present and differing, 0.5 either absent.

    Absence is neutral rather than negative - it is the signature of
    MISSING_REFERENCE_CODE, not evidence the pair is wrong.
    """
    if not _has_reference(left) or not _has_reference(right):
        return 0.5
    return (
        1.0
        if str(left["reference_code"]).strip().upper()
        == str(right["reference_code"]).strip().upper()
        else 0.0
    )


def _jaccard(left: Any, right: Any) -> float:
    """Token-overlap fallback when the matching engine recorded no similarity.

    Deliberately crude: it is a backstop for rows written before Subsystem 1
    started recording similarity, not a replacement for the TF-IDF cosine.
    """
    if not left or not right:
        return 0.0
    a = set(str(left).lower().split())
    b = set(str(right).lower().split())
    if not a or not b:
        return 0.0
    return round(len(a & b) / len(a | b), 4)


def _safe_log1p(value: float) -> float:
    import math

    try:
        return math.log1p(max(0.0, value))
    except (ValueError, OverflowError):
        return 0.0


def extract_batch(rows: list[dict[str, Any]]) -> list[ExceptionFeatures]:
    """Feature rows for a batch of {transaction, counterparts, context} dicts."""
    return [
        extract(
            row["transaction"],
            row.get("counterparts"),
            row.get("matching_context"),
        )
        for row in rows
    ]


__all__ = [
    "ExceptionFeatures",
    "extract",
    "extract_batch",
    "FEATURE_NAMES",
    "FEATURE_VERSION",
]
