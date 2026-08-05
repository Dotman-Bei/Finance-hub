"""Phase 0 gate: "shared models importable" (build.md §16).

These assert the Pydantic contracts actually enforce what §6 claims. The
amount/currency validators are the schema-validation stage of the ingestion
pipeline (§8), so a hole here becomes a hole in the ≥98% detection rate.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from shared.models import (
    EntryType,
    ExceptionCategory,
    ExceptionState,
    MatchResult,
    MatchStatus,
    MatchType,
    SourceType,
    Transaction,
    ValidationState,
)


def _valid_payload(**overrides) -> dict:
    payload = {
        "source_type": "bank_api",
        "amount": Decimal("1250.00"),
        "currency": "usd",
        "txn_date": dt.date(2026, 1, 15),
        "description": "Meridian Capital Ltd — settlement",
        "reference_code": "REF-10293",
    }
    payload.update(overrides)
    return payload


# ── Enums mirror db/schema.sql ───────────────────────────────────────────


def test_enum_members_match_postgres_types():
    assert [e.value for e in MatchStatus] == ["MATCHED", "UNMATCHED"]
    assert [e.value for e in MatchType] == ["RULE", "ML"]
    assert [e.value for e in ExceptionCategory] == [
        "PARTIAL_PAYMENT",
        "SPLIT_SETTLEMENT",
        "MISSING_REFERENCE_CODE",
        "TIMING_DIFFERENCE",
    ]
    assert [e.value for e in ExceptionState] == [
        "OPEN",
        "SUGGESTED",
        "RESOLVED",
        "REJECTED",
    ]
    assert [e.value for e in ValidationState] == ["PASSED", "QUARANTINED"]


def test_exception_categories_are_the_four_of_objective_3():
    assert len(ExceptionCategory) == 4


def test_source_and_entry_enums_match_ddl_comments():
    assert {e.value for e in SourceType} == {"bank_api", "payment_gateway", "erp"}
    assert {e.value for e in EntryType} == {"debit", "credit"}


# ── Transaction ──────────────────────────────────────────────────────────


def test_valid_transaction_parses_and_defaults_an_id():
    txn = Transaction(**_valid_payload())
    assert txn.amount == Decimal("1250.00")
    assert txn.id is not None


def test_currency_is_upper_cased():
    assert Transaction(**_valid_payload(currency="usd")).currency == "USD"


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-0.01"), Decimal("-9999")])
def test_non_positive_amount_is_rejected(amount):
    with pytest.raises(ValidationError, match="amount must be positive"):
        Transaction(**_valid_payload(amount=amount))


@pytest.mark.parametrize("currency", ["US", "USDD", ""])
def test_bad_currency_length_is_rejected(currency):
    with pytest.raises(ValidationError, match="3-letter ISO code"):
        Transaction(**_valid_payload(currency=currency))


def test_missing_required_field_is_rejected():
    payload = _valid_payload()
    del payload["txn_date"]
    with pytest.raises(ValidationError):
        Transaction(**payload)


def test_optional_fields_may_be_absent():
    payload = _valid_payload()
    for key in ("description", "reference_code"):
        del payload[key]
    txn = Transaction(**payload)
    assert txn.description is None
    assert txn.reference_code is None


def test_amount_accepts_string_input_from_csv_ingestion():
    # Pandas hands the pipeline strings; Decimal coercion must hold precision.
    assert Transaction(**_valid_payload(amount="1250.05")).amount == Decimal("1250.05")


# ── MatchResult ──────────────────────────────────────────────────────────


def test_matched_result_requires_counterpart_and_layer():
    result = MatchResult(
        transaction_id=uuid4(),
        counterpart_id=uuid4(),
        status=MatchStatus.MATCHED,
        match_type=MatchType.RULE,
        confidence_score=0.99,
    )
    assert result.status is MatchStatus.MATCHED


def test_matched_result_without_counterpart_is_rejected():
    with pytest.raises(ValidationError, match="requires counterpart_id"):
        MatchResult(
            transaction_id=uuid4(),
            status=MatchStatus.MATCHED,
            match_type=MatchType.ML,
            confidence_score=0.9,
        )


def test_matched_result_without_match_type_is_rejected():
    with pytest.raises(ValidationError, match="requires match_type"):
        MatchResult(
            transaction_id=uuid4(),
            counterpart_id=uuid4(),
            status=MatchStatus.MATCHED,
            confidence_score=0.9,
        )


def test_unmatched_result_may_not_carry_a_counterpart():
    with pytest.raises(ValidationError, match="must not carry a counterpart_id"):
        MatchResult(
            transaction_id=uuid4(),
            counterpart_id=uuid4(),
            status=MatchStatus.UNMATCHED,
            confidence_score=0.1,
        )


@pytest.mark.parametrize("score", [-0.01, 1.01])
def test_confidence_score_is_bounded(score):
    with pytest.raises(ValidationError):
        MatchResult(
            transaction_id=uuid4(),
            status=MatchStatus.UNMATCHED,
            confidence_score=score,
        )


def test_persists_honours_the_configurable_threshold():
    """§9: only pairs above MATCH_CONFIDENCE_THRESHOLD reach matchedrecords."""
    pair = MatchResult(
        transaction_id=uuid4(),
        counterpart_id=uuid4(),
        status=MatchStatus.MATCHED,
        match_type=MatchType.ML,
        confidence_score=0.86,
    )
    assert pair.persists(threshold=0.85) is True
    assert pair.persists(threshold=0.90) is False


def test_unmatched_never_persists_regardless_of_score():
    unmatched = MatchResult(
        transaction_id=uuid4(),
        status=MatchStatus.UNMATCHED,
        confidence_score=1.0,
    )
    assert unmatched.persists(threshold=0.0) is False
