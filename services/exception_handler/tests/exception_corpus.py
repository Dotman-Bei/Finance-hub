"""Labelled exception corpus for the classifier gate (build.md Sec. 14).

Sec. 14 sanctions synthetic datasets for tests. This lives under tests/ and is
never imported by application code.

Each record is generated *from its category's real-world signature*, then run
through the production `features.extract` - so the gate exercises the actual
feature pipeline, not a shortcut. A bug in feature engineering shows up here as
a drop in per-category recall, which is the point.

Signatures, drawn from Sec. 10:

  PARTIAL_PAYMENT         one counterpart settling 60-95% of the obligation,
                          posted within a couple of days
  SPLIT_SETTLEMENT        two to four receipts together covering 92-100%
  MISSING_REFERENCE_CODE  amount agrees, dates agree, no shared reference
  TIMING_DIFFERENCE       amount agrees, reference agrees, posted 5-20 days out

The categories deliberately overlap in places - a partial payment and a split
leg look alike in isolation, and both missing-reference and timing cases have
matching amounts. That overlap is the classification problem; a corpus without
it would measure nothing.
"""

from __future__ import annotations

import datetime as dt
import random
import uuid
from dataclasses import dataclass
from typing import Any

from services.exception_handler.app.features import ExceptionFeatures, extract
from shared.models.enums import ExceptionCategory

COUNTERPARTIES = [
    "Meridian Capital Ltd", "Northwind Logistics", "Arcadia Payments BV",
    "Solstice Retail Group", "Tessellate Software Inc", "Harborline Freight",
    "Lumen Energy Partners", "Vantage Clearing House", "Orion Manufacturing",
]
NARRATIVES = ["settlement", "invoice remittance", "wire transfer", "ACH credit"]
EXTERNAL_SOURCES = ["bank_api", "payment_gateway"]

CATEGORIES = [c.value for c in ExceptionCategory]


@dataclass
class LabelledException:
    features: ExceptionFeatures
    category: str
    transaction: dict[str, Any]
    counterparts: list[dict[str, Any]]


def _transaction(rng: random.Random, **overrides) -> dict[str, Any]:
    counterparty = rng.choice(COUNTERPARTIES)
    row = {
        "id": uuid.uuid4(),
        "external_id": f"ERP-{rng.randint(100000, 999999)}",
        "source_type": "erp",
        "amount": round(rng.uniform(250.0, 190000.0), 2),
        "currency": "USD",
        "txn_date": dt.date.today() - dt.timedelta(days=rng.randint(2, 120)),
        "description": f"{counterparty} - {rng.choice(NARRATIVES)}",
        "reference_code": f"REF-{rng.randint(10000, 99999)}",
    }
    row.update(overrides)
    return row


def _counterpart(rng: random.Random, base: dict[str, Any], **overrides) -> dict[str, Any]:
    row = {
        "id": uuid.uuid4(),
        "external_id": f"BNK-{rng.randint(100000, 999999)}",
        "source_type": rng.choice(EXTERNAL_SOURCES),
        "amount": base["amount"],
        "currency": "USD",
        "txn_date": base["txn_date"],
        "description": base["description"],
        "reference_code": base["reference_code"],
    }
    row.update(overrides)
    return row


def _partial_payment(rng: random.Random) -> LabelledException:
    txn = _transaction(rng)
    settled = round(txn["amount"] * rng.uniform(0.60, 0.95), 2)
    counterpart = _counterpart(
        rng, txn,
        amount=settled,
        txn_date=txn["txn_date"] + dt.timedelta(days=rng.randint(0, 2)),
        reference_code=txn["reference_code"] if rng.random() < 0.5 else None,
    )
    return _build(txn, [counterpart], ExceptionCategory.PARTIAL_PAYMENT.value, rng)


def _split_settlement(rng: random.Random) -> LabelledException:
    txn = _transaction(rng)
    legs = rng.randint(2, 4)
    coverage = rng.uniform(0.92, 1.0)
    total = txn["amount"] * coverage

    # Split the total into uneven legs, as real settlements arrive.
    weights = [rng.uniform(0.5, 1.5) for _ in range(legs)]
    scale = total / sum(weights)
    counterparts = [
        _counterpart(
            rng, txn,
            amount=round(w * scale, 2),
            txn_date=txn["txn_date"] + dt.timedelta(days=rng.randint(0, 3)),
            reference_code=None,
        )
        for w in weights
    ]
    return _build(txn, counterparts, ExceptionCategory.SPLIT_SETTLEMENT.value, rng)


def _missing_reference(rng: random.Random) -> LabelledException:
    txn = _transaction(rng, reference_code=None)
    counterpart = _counterpart(
        rng, txn,
        reference_code=None,
        txn_date=txn["txn_date"] + dt.timedelta(days=rng.randint(0, 2)),
    )
    return _build(txn, [counterpart], ExceptionCategory.MISSING_REFERENCE_CODE.value, rng)


def _timing_difference(rng: random.Random) -> LabelledException:
    txn = _transaction(rng)
    counterpart = _counterpart(
        rng, txn,
        txn_date=txn["txn_date"] + dt.timedelta(days=rng.randint(5, 20)),
    )
    return _build(txn, [counterpart], ExceptionCategory.TIMING_DIFFERENCE.value, rng)


def _build(
    txn: dict[str, Any],
    counterparts: list[dict[str, Any]],
    category: str,
    rng: random.Random,
) -> LabelledException:
    # Runs the production feature pipeline, so the gate covers it too.
    features = extract(
        txn,
        counterparts,
        matching_context={
            "best_confidence": round(rng.uniform(0.35, 0.84), 4),
            "description_similarity": round(rng.uniform(0.55, 1.0), 4),
        },
    )
    return LabelledException(
        features=features, category=category, transaction=txn, counterparts=counterparts
    )


GENERATORS = {
    ExceptionCategory.PARTIAL_PAYMENT.value: _partial_payment,
    ExceptionCategory.SPLIT_SETTLEMENT.value: _split_settlement,
    ExceptionCategory.MISSING_REFERENCE_CODE.value: _missing_reference,
    ExceptionCategory.TIMING_DIFFERENCE.value: _timing_difference,
}


# ── Hard variants ────────────────────────────────────────────────────────
# The clean generators above give each category a separable signature, which
# makes any classifier built from those same signatures look perfect. That is
# circular. These variants carry signals from two categories at once - the
# label is the one a finance reviewer would assign, and the competing signal
# is what the classifier has to weigh against it.
#
# Real exception queues are mostly these. A number measured only on the clean
# corpus overstates what the classifier will do in production.


def _hard_late_partial(rng: random.Random) -> LabelledException:
    """Short *and* late. Competes with TIMING_DIFFERENCE; the shortfall wins,
    because an unpaid balance must be followed up regardless of when it lands."""
    txn = _transaction(rng)
    counterpart = _counterpart(
        rng, txn,
        amount=round(txn["amount"] * rng.uniform(0.65, 0.93), 2),
        txn_date=txn["txn_date"] + dt.timedelta(days=rng.randint(6, 18)),
    )
    return _build(txn, [counterpart], ExceptionCategory.PARTIAL_PAYMENT.value, rng)


def _hard_drifted_timing(rng: random.Random) -> LabelledException:
    """Late with a rounding-scale discrepancy - FX or fees, not a short
    payment. Competes with PARTIAL_PAYMENT."""
    txn = _transaction(rng)
    counterpart = _counterpart(
        rng, txn,
        amount=round(txn["amount"] * rng.uniform(0.985, 0.999), 2),
        txn_date=txn["txn_date"] + dt.timedelta(days=rng.randint(6, 20)),
    )
    return _build(txn, [counterpart], ExceptionCategory.TIMING_DIFFERENCE.value, rng)


def _hard_two_leg_split(rng: random.Random) -> LabelledException:
    """Two legs covering only 85-91%. Each leg alone looks like a partial
    payment, and the coverage sits under the split threshold."""
    txn = _transaction(rng)
    total = txn["amount"] * rng.uniform(0.85, 0.91)
    first = round(total * rng.uniform(0.4, 0.6), 2)
    counterparts = [
        _counterpart(rng, txn, amount=first, reference_code=None),
        _counterpart(rng, txn, amount=round(total - first, 2), reference_code=None),
    ]
    return _build(txn, counterparts, ExceptionCategory.SPLIT_SETTLEMENT.value, rng)


def _hard_late_missing_reference(rng: random.Random) -> LabelledException:
    """No reference, amounts agree, but posted just past the drift threshold.
    Competes with TIMING_DIFFERENCE."""
    txn = _transaction(rng, reference_code=None)
    counterpart = _counterpart(
        rng, txn,
        reference_code=None,
        txn_date=txn["txn_date"] + dt.timedelta(days=rng.randint(4, 7)),
    )
    return _build(txn, [counterpart], ExceptionCategory.MISSING_REFERENCE_CODE.value, rng)


def _hard_orphan(rng: random.Random) -> LabelledException:
    """Nothing nominated at all. No feature can separate this beyond the fact
    that matching found nothing; it is included so the gate accounts for rows
    the classifier genuinely cannot resolve."""
    txn = _transaction(rng, reference_code=None)
    return _build(txn, [], ExceptionCategory.MISSING_REFERENCE_CODE.value, rng)


HARD_GENERATORS = {
    ExceptionCategory.PARTIAL_PAYMENT.value: [_hard_late_partial],
    ExceptionCategory.SPLIT_SETTLEMENT.value: [_hard_two_leg_split],
    ExceptionCategory.MISSING_REFERENCE_CODE.value: [
        _hard_late_missing_reference,
        _hard_orphan,
    ],
    ExceptionCategory.TIMING_DIFFERENCE.value: [_hard_drifted_timing],
}


def build_corpus(
    per_category: int = 150,
    seed: int = 20260803,
    imbalance: dict[str, float] | None = None,
    hard_fraction: float = 0.0,
) -> list[LabelledException]:
    """Generate a labelled corpus.

    `imbalance` scales individual categories. Sec. 10 calls Random Forest
    "robust to the class imbalance typical of exception data", so the gate has
    to be able to reproduce that imbalance and check the claim.

    `hard_fraction` replaces that share of each category with ambiguous cases
    carrying two competing signals. At 0.0 the categories are cleanly
    separable, which measures little; at 0.5 the corpus resembles a real
    queue. Both numbers are reported, because the gap between them is the
    honest measure of how much the clean figure is worth.
    """
    rng = random.Random(seed)
    records: list[LabelledException] = []

    for category, generator in GENERATORS.items():
        count = int(per_category * (imbalance or {}).get(category, 1.0))
        hard_count = int(count * hard_fraction)
        variants = HARD_GENERATORS.get(category, [])

        for i in range(count):
            if i < hard_count and variants:
                records.append(variants[i % len(variants)](rng))
            else:
                records.append(generator(rng))

    rng.shuffle(records)
    return records


__all__ = ["LabelledException", "build_corpus", "CATEGORIES", "GENERATORS"]
