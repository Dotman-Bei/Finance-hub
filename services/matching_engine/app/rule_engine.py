"""Layer 1 - deterministic rule matching (build.md Sec. 9).

    "Layer 1 (exact match on ID + amount + date)"

build.md gives a two-line pandas merge for this. That merge is the right idea
but has three behaviours that would manufacture false matches at volume, and
Sec. 9's whole argument is about suppressing false positives:

1. **Null reference codes join to each other.** pandas `merge` treats NaN as
   equal to NaN, so two unrelated records that both lack a reference and happen
   to share an amount and date would be declared an exact match. Rows without a
   reference code are therefore excluded from this layer entirely and handed to
   the ML layer - which is also correct semantically, since a missing reference
   is a MISSING_REFERENCE_CODE exception (Sec. 10).

2. **Duplicate keys explode.** An inner merge on a key present 3 times either
   side yields 9 rows, and every one of them would be persisted as a confirmed
   pair. Matching is one-to-one: a transaction reconciles against exactly one
   counterpart. Pairing is done per key group, capped at the smaller side, with
   the surplus passed on.

3. **Float amounts do not compare equal.** 100.10 read from JSON and from CSV
   can differ in the last bit. Amounts are quantised to 2dp - the scale of
   NUMERIC(18,2) - before they are used as a key.

An exact match on all three keys is certain, so these pairs score 1.0 and
bypass the confidence threshold.
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

#: build.md Sec. 9 - "exact match on ID + amount + date".
RULE_KEYS = ["reference_code", "amount", "txn_date"]

#: Rule-layer pairs are exact on every key, so there is nothing to estimate.
RULE_CONFIDENCE = 1.0


def _quantise(value: Any) -> Decimal | None:
    """Amount to exactly 2dp, matching NUMERIC(18,2)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _normalise_key(row: pd.Series) -> tuple | None:
    """The join key for one row, or None when it cannot participate."""
    reference = row.get("reference_code")
    if reference is None or (isinstance(reference, float) and pd.isna(reference)):
        return None
    reference = str(reference).strip()
    if not reference:
        return None

    amount = _quantise(row.get("amount"))
    if amount is None:
        return None

    txn_date = row.get("txn_date")
    if txn_date is None or (isinstance(txn_date, float) and pd.isna(txn_date)):
        return None
    txn_date = pd.Timestamp(txn_date).date()

    return (reference, amount, txn_date)


def _index_by_key(frame: pd.DataFrame) -> dict[tuple, list[int]]:
    index: dict[tuple, list[int]] = {}
    for position, (_, row) in enumerate(frame.iterrows()):
        key = _normalise_key(row)
        if key is None:
            continue
        index.setdefault(key, []).append(position)
    return index


def rule_match(
    internal: pd.DataFrame, external: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pair internal against external on (reference_code, amount, txn_date).

    Returns (confirmed pairs, unmatched rows). Confirmed pairs are certain and
    go straight to `matchedrecords`; the unmatched frame is what Layer 2 sees.

    The returned pair frame carries `id_int`/`id_ext` plus the key columns, so
    the caller never has to re-derive which two rows were joined.
    """
    if internal.empty or external.empty:
        return _empty_pairs(), pd.concat([internal, external], ignore_index=True)

    internal = internal.reset_index(drop=True)
    external = external.reset_index(drop=True)

    external_index = _index_by_key(external)
    used_internal: set[int] = set()
    used_external: set[int] = set()
    pairs: list[dict[str, Any]] = []

    for position, (_, row) in enumerate(internal.iterrows()):
        key = _normalise_key(row)
        if key is None:
            continue

        candidates = external_index.get(key)
        if not candidates:
            continue

        # One-to-one: take the first counterpart not already spoken for.
        counterpart = next((c for c in candidates if c not in used_external), None)
        if counterpart is None:
            continue

        used_internal.add(position)
        used_external.add(counterpart)

        reference, amount, txn_date = key
        pairs.append(
            {
                "id_int": internal.at[position, "id"],
                "id_ext": external.at[counterpart, "id"],
                "reference_code": reference,
                "amount": amount,
                "txn_date": txn_date,
                "match_type": "RULE",
                "confidence_score": RULE_CONFIDENCE,
            }
        )

    unmatched = pd.concat(
        [
            internal.drop(index=list(used_internal)),
            external.drop(index=list(used_external)),
        ],
        ignore_index=True,
    )

    logger.debug(
        "Rule layer: %d pairs from %d internal / %d external; %d to ML",
        len(pairs), len(internal), len(external), len(unmatched),
    )

    merged = pd.DataFrame(pairs) if pairs else _empty_pairs()
    return merged, unmatched


def _empty_pairs() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "id_int", "id_ext", "reference_code", "amount",
            "txn_date", "match_type", "confidence_score",
        ]
    )


def split_by_side(
    transactions: pd.DataFrame,
    internal_sources: tuple[str, ...] = ("erp",),
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split one transaction set into the two sides to reconcile.

    Sec. 1's flow is "external sources -> ... -> ledger", so the ERP feed is the
    internal book of record and the bank/gateway feeds are what it is
    reconciled against. `source_type` is the discriminator.
    """
    if transactions.empty or "source_type" not in transactions.columns:
        return transactions.copy(), transactions.iloc[0:0].copy()

    is_internal = transactions["source_type"].isin(internal_sources)
    return (
        transactions[is_internal].reset_index(drop=True),
        transactions[~is_internal].reset_index(drop=True),
    )


__all__ = ["rule_match", "split_by_side", "RULE_KEYS", "RULE_CONFIDENCE"]
