"""Confidence scoring and the persistence threshold (build.md Sec. 9).

    "Every candidate pair gets a confidence score. Only pairs above
     MATCH_CONFIDENCE_THRESHOLD (from .env) persist to matchedrecords;
     everything else goes to exceptionqueue. Keeping the threshold configurable
     is how you suppress the false positives the literature warns about."

Sec. 9 requires a score but does not define one, so the composition is set
here. Four signals, each independently defensible for reconciliation:

  description similarity  0.40   what the ML layer actually proposed the pair on
  amount proximity        0.35   the strongest single reconciliation signal
  date proximity          0.15   settlement lag is normal; large drift is not
  reference agreement     0.10   corroborating when present, never decisive

Weights are module constants rather than magic numbers inline so a
sensitivity analysis can sweep them, and they are asserted to sum to 1.0 so a
score is always a true [0, 1] value comparable with the .env threshold.

Amount proximity uses *relative* difference. An absolute tolerance would treat
a 5.00 discrepancy as equally serious on a 20.00 invoice and a 200,000.00
settlement, which is the wrong shape for the partial-payment and
split-settlement cases the exception handler has to distinguish (Sec. 10).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

WEIGHT_DESCRIPTION = 0.40
WEIGHT_AMOUNT = 0.35
WEIGHT_DATE = 0.15
WEIGHT_REFERENCE = 0.10

assert (
    abs(WEIGHT_DESCRIPTION + WEIGHT_AMOUNT + WEIGHT_DATE + WEIGHT_REFERENCE - 1.0) < 1e-9
), "scoring weights must sum to 1.0 or scores are not comparable to the threshold"

#: Beyond this relative gap the amounts are not the same payment.
AMOUNT_TOLERANCE = 0.20

#: Settlement lag beyond this many days scores zero on the date component.
DATE_TOLERANCE_DAYS = 10


@dataclass
class ScoredPair:
    """A candidate pair with its confidence and the components behind it."""

    internal_id: Any
    external_id: Any
    confidence: float
    match_type: str = "ML"
    components: dict[str, float] = field(default_factory=dict)

    def persists(self, threshold: float) -> bool:
        """True -> matchedrecords. False -> exceptionqueue (Sec. 9).

        Clearing the threshold is necessary but not sufficient: the pair also
        has to agree on something that identifies it, meaning either the
        amounts match exactly or the two references match.

        Description similarity cannot play that role however high it scores.
        Two transactions from the same counterparty carry the same narrative
        by construction, so a 1.0 there says "same counterparty", not "same
        payment" - and weighted at 0.40 it can carry a pair over the line on
        its own. That is how the engine confirmed two *different* obligations
        from one counterparty whose amounts were 0.87% apart and whose bank
        side had no reference at all: description 1.0, amount 0.96, reference
        neutral, total 0.86 against a 0.85 threshold.

        This is the asymmetry the gate is built on. A wrong match silently
        corrupts the ledger; a declined one lands in the exception queue where
        a human sees it, with its candidate still nominated. Sec. 10 also
        *wants* partial payments reviewed rather than auto-posted, so most of
        what this refuses is what should have been refused anyway.
        """
        if self.confidence < threshold:
            return False
        amounts_agree = self.components.get("amount", 0.0) >= 0.999
        references_agree = self.components.get("reference", 0.0) >= 0.999
        # An exact rule-layer match carries no components; it agreed on
        # reference, amount and date by definition.
        return amounts_agree or references_agree or self.match_type == "RULE"


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _to_date(value: Any) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def amount_proximity(left: Any, right: Any) -> float:
    """1.0 for identical amounts, decaying linearly to 0 at the tolerance."""
    a, b = _to_decimal(left), _to_decimal(right)
    if a is None or b is None:
        return 0.0

    largest = max(abs(a), abs(b))
    if largest == 0:
        return 1.0 if a == b else 0.0

    relative_gap = float(abs(a - b) / largest)
    if relative_gap >= AMOUNT_TOLERANCE:
        return 0.0
    return 1.0 - (relative_gap / AMOUNT_TOLERANCE)


def date_proximity(left: Any, right: Any) -> float:
    """1.0 same day, decaying to 0 at DATE_TOLERANCE_DAYS."""
    a, b = _to_date(left), _to_date(right)
    if a is None or b is None:
        return 0.0

    drift = abs((a - b).days)
    if drift >= DATE_TOLERANCE_DAYS:
        return 0.0
    return 1.0 - (drift / DATE_TOLERANCE_DAYS)


def reference_agreement(left: Any, right: Any) -> float:
    """1.0 when both carry the same reference, 0.0 when they disagree.

    Neutral (0.5) when either is missing: absence is not evidence against a
    match - it is precisely the MISSING_REFERENCE_CODE case (Sec. 10) - so it
    must neither reward nor punish the pair.
    """
    if not left or not right:
        return 0.5
    return 1.0 if str(left).strip().upper() == str(right).strip().upper() else 0.0


def score_pair(
    internal_row: dict[str, Any],
    external_row: dict[str, Any],
    description_similarity: float,
) -> ScoredPair:
    """Compute the confidence for one candidate pair."""
    components = {
        "description": max(0.0, min(1.0, float(description_similarity))),
        "amount": amount_proximity(internal_row.get("amount"), external_row.get("amount")),
        "date": date_proximity(internal_row.get("txn_date"), external_row.get("txn_date")),
        "reference": reference_agreement(
            internal_row.get("reference_code"), external_row.get("reference_code")
        ),
    }

    confidence = (
        components["description"] * WEIGHT_DESCRIPTION
        + components["amount"] * WEIGHT_AMOUNT
        + components["date"] * WEIGHT_DATE
        + components["reference"] * WEIGHT_REFERENCE
    )

    return ScoredPair(
        internal_id=internal_row.get("id"),
        external_id=external_row.get("id"),
        confidence=round(min(1.0, max(0.0, confidence)), 4),
        match_type="ML",
        components={k: round(v, 4) for k, v in components.items()},
    )


def resolve_one_to_one(pairs: list[ScoredPair]) -> list[ScoredPair]:
    """Reduce overlapping candidates to a one-to-one assignment.

    The ML layer proposes several counterparts per row. A transaction
    reconciles against exactly one, so pairs are taken highest-confidence
    first and any later pair reusing a claimed id is dropped - those rows fall
    through to the exception queue, which is the correct outcome for an
    ambiguous match.
    """
    claimed_internal: set[Any] = set()
    claimed_external: set[Any] = set()
    resolved: list[ScoredPair] = []

    for pair in sorted(pairs, key=lambda p: p.confidence, reverse=True):
        if pair.internal_id in claimed_internal or pair.external_id in claimed_external:
            continue
        claimed_internal.add(pair.internal_id)
        claimed_external.add(pair.external_id)
        resolved.append(pair)

    return resolved


def partition_by_threshold(
    pairs: list[ScoredPair], threshold: float
) -> tuple[list[ScoredPair], list[ScoredPair]]:
    """Split into (persist to matchedrecords, send to exceptionqueue)."""
    above = [p for p in pairs if p.persists(threshold)]
    below = [p for p in pairs if not p.persists(threshold)]
    return above, below


__all__ = [
    "ScoredPair",
    "score_pair",
    "resolve_one_to_one",
    "partition_by_threshold",
    "amount_proximity",
    "date_proximity",
    "reference_agreement",
    "WEIGHT_DESCRIPTION",
    "WEIGHT_AMOUNT",
    "WEIGHT_DATE",
    "WEIGHT_REFERENCE",
    "AMOUNT_TOLERANCE",
    "DATE_TOLERANCE_DAYS",
]
