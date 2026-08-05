"""Labelled reconciliation corpus for the precision gate (build.md Sec. 14).

Sec. 14 sanctions synthetic datasets for tests. This module lives under tests/
and is never imported by application code.

Each generated pair carries a known correct answer, so precision and recall are
measured against ground truth rather than against the engine's own opinion.

The pair archetypes are drawn from the four exception categories the system has
to distinguish (Sec. 10), because those are exactly the cases where a matcher
either earns its keep or produces false positives:

  exact               same reference, amount and date       -> Layer 1
  timing_difference   same everything, settlement lag        -> Layer 2
  missing_reference   reference absent on the bank side      -> Layer 2
  partial_payment     bank settles part of the invoice       -> Layer 2
  noisy_description   same payment, mangled bank narrative    -> Layer 2

Distractors matter as much as pairs. `decoys` are internal and external records
that genuinely have no counterpart but look plausible - same counterparty,
similar amounts, nearby dates. Without them a matcher that pairs everything
with anything would score perfectly.
"""

from __future__ import annotations

import datetime as dt
import random
import uuid
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

COUNTERPARTIES = [
    "Meridian Capital Ltd", "Northwind Logistics", "Arcadia Payments BV",
    "Solstice Retail Group", "Tessellate Software Inc", "Harborline Freight",
    "Lumen Energy Partners", "Vantage Clearing House", "Kestrel Media SA",
    "Orion Manufacturing", "Bluepeak Insurance", "Cadence Health Systems",
    "Fairmount Trading Co", "Silverbrook Holdings", "Ridgeway Chemicals",
]

NARRATIVES = ["settlement", "invoice remittance", "wire transfer", "ACH credit", "card batch"]
EXTERNAL_SOURCES = ["bank_api", "payment_gateway"]

ARCHETYPES = (
    "exact",
    "timing_difference",
    "missing_reference",
    "partial_payment",
    "noisy_description",
)


@dataclass
class GroundTruth:
    transactions: pd.DataFrame
    #: {(internal_id, external_id)} that genuinely reconcile.
    true_pairs: set[tuple[Any, Any]] = field(default_factory=set)
    #: archetype label per true pair, for per-archetype reporting.
    pair_archetype: dict[tuple[Any, Any], str] = field(default_factory=dict)

    @property
    def true_pair_count(self) -> int:
        return len(self.true_pairs)

    def is_correct(self, internal_id: Any, external_id: Any) -> bool:
        """Direction-agnostic: the engine may report either side first."""
        return (
            (internal_id, external_id) in self.true_pairs
            or (external_id, internal_id) in self.true_pairs
        )


def _mangle(narrative: str, counterparty: str, rng: random.Random) -> str:
    """Bank-style narrative noise: truncation, casing, reference fragments."""
    short = counterparty.upper()[: rng.randint(8, len(counterparty))]
    return rng.choice(
        [
            f"{short}//REF{rng.randint(1000, 9999)} {narrative.upper()}",
            f"{narrative.upper()} {short} {rng.randint(100000, 999999)}",
            f"POS {short}  {narrative}",
            f"{short} - {narrative} - BATCH{rng.randint(10, 99)}",
        ]
    )


def build_ground_truth(
    pair_count: int = 200,
    decoy_count: int = 60,
    seed: int = 20260803,
) -> GroundTruth:
    """Generate a labelled corpus with an even spread of archetypes."""
    rng = random.Random(seed)
    today = dt.date.today()
    rows: list[dict[str, Any]] = []
    true_pairs: set[tuple[Any, Any]] = set()
    archetypes: dict[tuple[Any, Any], str] = {}

    for i in range(pair_count):
        archetype = ARCHETYPES[i % len(ARCHETYPES)]
        counterparty = rng.choice(COUNTERPARTIES)
        narrative = rng.choice(NARRATIVES)
        amount = round(rng.uniform(120.0, 180000.0), 2)
        txn_date = today - dt.timedelta(days=rng.randint(1, 120))
        reference = f"REF-{rng.randint(10000, 99999)}"
        description = f"{counterparty} - {narrative}"

        internal_id = uuid.uuid4()
        external_id = uuid.uuid4()

        internal = {
            "id": internal_id,
            "external_id": f"ERP-{rng.randint(100000, 999999)}",
            "source_type": "erp",
            "amount": amount,
            "currency": "USD",
            "txn_date": txn_date,
            "description": description,
            "reference_code": reference,
        }

        external = {
            "id": external_id,
            "external_id": f"BNK-{rng.randint(100000, 999999)}",
            "source_type": rng.choice(EXTERNAL_SOURCES),
            "amount": amount,
            "currency": "USD",
            "txn_date": txn_date,
            "description": description,
            "reference_code": reference,
        }

        if archetype == "timing_difference":
            external["txn_date"] = txn_date + dt.timedelta(days=rng.randint(1, 5))
        elif archetype == "missing_reference":
            external["reference_code"] = None
            external["description"] = _mangle(narrative, counterparty, rng)
        elif archetype == "partial_payment":
            external["amount"] = round(amount * rng.uniform(0.86, 0.97), 2)
            external["reference_code"] = None
        elif archetype == "noisy_description":
            external["description"] = _mangle(narrative, counterparty, rng)
            external["reference_code"] = None
            external["txn_date"] = txn_date + dt.timedelta(days=rng.randint(0, 2))

        rows.extend([internal, external])
        true_pairs.add((internal_id, external_id))
        archetypes[(internal_id, external_id)] = archetype

    # Decoys: plausible but genuinely unpaired.
    for i in range(decoy_count):
        counterparty = rng.choice(COUNTERPARTIES)
        narrative = rng.choice(NARRATIVES)
        side_is_internal = i % 2 == 0
        rows.append(
            {
                "id": uuid.uuid4(),
                "external_id": f"{'ERP' if side_is_internal else 'BNK'}-{rng.randint(100000, 999999)}",
                "source_type": "erp" if side_is_internal else rng.choice(EXTERNAL_SOURCES),
                "amount": round(rng.uniform(120.0, 180000.0), 2),
                "currency": "USD",
                "txn_date": today - dt.timedelta(days=rng.randint(1, 120)),
                "description": f"{counterparty} - {narrative}",
                "reference_code": f"REF-{rng.randint(10000, 99999)}",
            }
        )

    frame = pd.DataFrame(rows)
    frame = frame.sample(frac=1.0, random_state=seed).reset_index(drop=True)

    return GroundTruth(
        transactions=frame, true_pairs=true_pairs, pair_archetype=archetypes
    )


def historical_descriptions(count: int = 600, seed: int = 7) -> list[str]:
    """A corpus for fitting the TF-IDF vocabulary, as Sec. 9's offline fit."""
    rng = random.Random(seed)
    corpus = []
    for _ in range(count):
        counterparty = rng.choice(COUNTERPARTIES)
        narrative = rng.choice(NARRATIVES)
        corpus.append(f"{counterparty} - {narrative}")
        corpus.append(_mangle(narrative, counterparty, rng))
    return corpus


__all__ = ["GroundTruth", "build_ground_truth", "historical_descriptions", "ARCHETYPES"]
