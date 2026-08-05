"""Labelled corpus generator for the detection-rate gate (build.md Sec. 8, Sec. 14).

Sec. 14 explicitly sanctions synthetic datasets *for tests*. This module lives
under tests/ and is never imported by application code - the pipeline itself
has no fixtures, fallbacks or generated data anywhere.

Every malformed record here is a *structural or rule* inconsistency, which is
what Objective 2 measures. Records that are structurally sound but
semantically wrong (a duplicate, a plausible-but-incorrect amount) are
deliberately absent: ingestion cannot detect those, they are the matching
engine's and exception handler's job, and counting them here would understate
a rate the pipeline was never meant to catch.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
from dataclasses import dataclass
from typing import Any, Callable

from services.validation_pipeline.app.checksum import canonical_json

GOOD_CURRENCIES = ["USD", "EUR", "GBP", "NGN", "ZAR", "KES", "CAD", "JPY"]
GOOD_SOURCES = ["bank_api", "payment_gateway", "erp"]
COUNTERPARTIES = [
    "Meridian Capital Ltd", "Northwind Logistics", "Arcadia Payments BV",
    "Solstice Retail Group", "Tessellate Software Inc", "Harborline Freight",
    "Lumen Energy Partners", "Vantage Clearing House", "Orion Manufacturing",
]
NARRATIVES = ["settlement", "invoice remittance", "card batch", "wire transfer", "ACH credit"]


@dataclass
class LabelledRecord:
    payload: dict[str, Any]
    is_malformed: bool
    defect: str  # "" when well-formed


def _base_record(rng: random.Random, today: dt.date) -> dict[str, Any]:
    return {
        "external_id": f"TXN-{rng.randint(100000, 999999)}",
        "source_type": rng.choice(GOOD_SOURCES),
        "amount": round(rng.uniform(1.0, 250000.0), 2),
        "currency": rng.choice(GOOD_CURRENCIES),
        "txn_date": (today - dt.timedelta(days=rng.randint(0, 400))).isoformat(),
        "description": f"{rng.choice(COUNTERPARTIES)} - {rng.choice(NARRATIVES)}",
        "reference_code": f"REF-{rng.randint(10000, 99999)}",
    }


# ── Well-formed variants ─────────────────────────────────────────────────
# Each must pass all four stages. Any of these being quarantined is a false
# positive and fails the gate outright.


def _good_plain(rec, rng):
    return rec


def _good_no_reference(rec, rng):
    # A missing reference is a MISSING_REFERENCE_CODE exception downstream
    # (Sec. 10), never an ingestion failure.
    rec["reference_code"] = None
    return rec


def _good_no_external_id(rec, rng):
    rec["external_id"] = None
    return rec


def _good_no_description(rec, rng):
    rec["description"] = None
    return rec


def _good_today(rec, rng):
    rec["txn_date"] = dt.date.today().isoformat()
    return rec


def _good_tiny_amount(rec, rng):
    rec["amount"] = 0.01
    return rec


def _good_lowercase_currency(rec, rng):
    # The canonical model upper-cases; this must not be rejected.
    rec["currency"] = rec["currency"].lower()
    return rec


def _good_with_valid_checksum(rec, rng):
    rec["checksum_algorithm"] = "sha256"
    rec["checksum"] = hashlib.sha256(canonical_json(rec).encode()).hexdigest()
    return rec


GOOD_VARIANTS: list[Callable] = [
    _good_plain,
    _good_no_reference,
    _good_no_external_id,
    _good_no_description,
    _good_today,
    _good_tiny_amount,
    _good_lowercase_currency,
    _good_with_valid_checksum,
]


# ── Malformed variants ───────────────────────────────────────────────────
# Grouped by the stage that ought to catch each one.


def _bad_missing_amount(rec, rng):
    del rec["amount"]
    return rec, "schema/missing_amount"


def _bad_missing_txn_date(rec, rng):
    del rec["txn_date"]
    return rec, "schema/missing_txn_date"


def _bad_missing_source_type(rec, rng):
    del rec["source_type"]
    return rec, "schema/missing_source_type"


def _bad_zero_amount(rec, rng):
    rec["amount"] = 0
    return rec, "schema/zero_amount"


def _bad_negative_amount(rec, rng):
    rec["amount"] = -abs(rec["amount"])
    return rec, "schema/negative_amount"


def _bad_non_numeric_amount(rec, rng):
    rec["amount"] = rng.choice(["not-a-number", "12,50.00", "$400", ""])
    return rec, "schema/non_numeric_amount"


def _bad_currency_too_short(rec, rng):
    rec["currency"] = "US"
    return rec, "schema/currency_too_short"


def _bad_currency_too_long(rec, rng):
    rec["currency"] = "USDD"
    return rec, "schema/currency_too_long"


def _bad_malformed_date(rec, rng):
    rec["txn_date"] = rng.choice(["not-a-date", "2026-13-45", "31/02/2026", ""])
    return rec, "schema/malformed_date"


def _bad_null_amount(rec, rng):
    rec["amount"] = None
    return rec, "schema/null_amount"


def _bad_unknown_currency(rec, rng):
    rec["currency"] = rng.choice(["ZZZ", "XXX", "ABC"])
    return rec, "rule/unknown_currency"


def _bad_unknown_source(rec, rng):
    rec["source_type"] = rng.choice(["carrier_pigeon", "unknown", "fax"])
    return rec, "rule/unknown_source_type"


def _bad_future_date(rec, rng):
    rec["txn_date"] = (dt.date.today() + dt.timedelta(days=rng.randint(1, 900))).isoformat()
    return rec, "rule/future_date"


def _bad_reference_format(rec, rng):
    rec["reference_code"] = rng.choice(["BADFORMAT", "REF-12", "12345", "REF_9999", "ref-99999"])
    return rec, "rule/bad_reference_format"


def _bad_external_id_charset(rec, rng):
    rec["external_id"] = rng.choice(["TXN 123 456", "TXN/981;DROP", "<script>", "id\nwith\nnewlines"])
    return rec, "rule/bad_external_id"


def _bad_absurd_amount(rec, rng):
    rec["amount"] = round(rng.uniform(2e9, 9e12), 2)
    return rec, "rule/amount_overflows_column"


def _bad_checksum_mismatch(rec, rng):
    rec["checksum_algorithm"] = "sha256"
    rec["checksum"] = hashlib.sha256(
        (canonical_json(rec) + "corrupted-in-transit").encode()
    ).hexdigest()
    return rec, "checksum/mismatch"


def _bad_checksum_algorithm(rec, rng):
    rec["checksum_algorithm"] = rng.choice(["crc32", "adler", "rot13"])
    rec["checksum"] = "deadbeef" * 8
    return rec, "checksum/unsupported_algorithm"


def _bad_hmac_without_secret(rec, rng):
    rec["checksum_algorithm"] = "hmac-sha256"
    rec["checksum"] = hashlib.sha256(canonical_json(rec).encode()).hexdigest()
    return rec, "checksum/hmac_no_secret"


def _bad_not_an_object(rec, rng):
    return rng.choice([["a", "list"], "a bare string", 42]), "schema/not_an_object"


MALFORMED_VARIANTS: list[Callable] = [
    _bad_missing_amount,
    _bad_missing_txn_date,
    _bad_missing_source_type,
    _bad_zero_amount,
    _bad_negative_amount,
    _bad_non_numeric_amount,
    _bad_currency_too_short,
    _bad_currency_too_long,
    _bad_malformed_date,
    _bad_null_amount,
    _bad_unknown_currency,
    _bad_unknown_source,
    _bad_future_date,
    _bad_reference_format,
    _bad_external_id_charset,
    _bad_absurd_amount,
    _bad_checksum_mismatch,
    _bad_checksum_algorithm,
    _bad_hmac_without_secret,
    _bad_not_an_object,
]


def build_corpus(
    good_count: int = 1000,
    malformed_count: int = 1000,
    seed: int = 20260803,
) -> list[LabelledRecord]:
    """Deterministic labelled corpus, shuffled so ordering carries no signal.

    Malformed variants are cycled rather than sampled, so every defect type is
    represented in equal measure and the rate cannot be flattered by an
    over-representation of easy cases.
    """
    rng = random.Random(seed)
    today = dt.date.today()
    records: list[LabelledRecord] = []

    for i in range(good_count):
        variant = GOOD_VARIANTS[i % len(GOOD_VARIANTS)]
        payload = variant(_base_record(rng, today), rng)
        records.append(LabelledRecord(payload=payload, is_malformed=False, defect=""))

    for i in range(malformed_count):
        variant = MALFORMED_VARIANTS[i % len(MALFORMED_VARIANTS)]
        payload, defect = variant(_base_record(rng, today), rng)
        records.append(LabelledRecord(payload=payload, is_malformed=True, defect=defect))

    rng.shuffle(records)
    return records


def defect_types() -> list[str]:
    """Every defect label the generator can emit."""
    rng = random.Random(0)
    today = dt.date.today()
    return sorted({variant(_base_record(rng, today), rng)[1] for variant in MALFORMED_VARIANTS})


__all__ = ["LabelledRecord", "build_corpus", "defect_types", "GOOD_VARIANTS", "MALFORMED_VARIANTS"]
