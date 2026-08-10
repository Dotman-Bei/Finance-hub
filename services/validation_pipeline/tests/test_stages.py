"""Unit tests for the four stages and the ETL front door (build.md Sec. 8).

test_detection_rate.py proves the pipeline hits the objective in aggregate.
These pin down the individual behaviours that make it true, so a regression
names the stage that broke rather than just moving the headline number.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal

import pandas as pd
import pytest

from services.validation_pipeline.app.checksum import (
    canonical_json,
    fingerprint,
    verify_checksum,
)
from services.validation_pipeline.app.ingestion import (
    StagingBuffer,
    looks_like_csv,
    normalize,
)
from services.validation_pipeline.app.pipeline import ValidationPipeline
from services.validation_pipeline.app.rule_processor import RuleProcessor
from services.validation_pipeline.app.schema_validator import (
    validate_schema,
    validate_schema_batch,
)
from shared.models.enums import ValidationStage, ValidationState

TODAY = dt.date.today()


def good_payload(**overrides):
    payload = {
        "external_id": "TXN-100001",
        "source_type": "bank_api",
        "amount": 1250.00,
        "currency": "USD",
        "txn_date": (TODAY - dt.timedelta(days=3)).isoformat(),
        "description": "Meridian Capital Ltd - settlement",
        "reference_code": "REF-10293",
    }
    payload.update(overrides)
    return payload


# ── Stage 1: schema ──────────────────────────────────────────────────────


def test_valid_payload_parses():
    txn, violations = validate_schema(good_payload())
    assert txn is not None and violations == []
    assert txn.amount == Decimal("1250.00")


def test_violations_name_the_offending_field():
    txn, violations = validate_schema(good_payload(amount=-1))
    assert txn is None
    assert any("amount" in v for v in violations)


def test_batch_keeps_indices_aligned():
    payloads = [good_payload(), good_payload(amount=-1), good_payload()]
    passed, failed = validate_schema_batch(payloads)
    assert set(passed) == {0, 2}
    assert set(failed) == {1}


def test_non_dict_payload_is_rejected_not_raised():
    passed, failed = validate_schema_batch(["a bare string", 42, ["list"]])
    assert passed == {}
    assert set(failed) == {0, 1, 2}


# ── Stage 2: business rules ──────────────────────────────────────────────


@pytest.fixture(scope="module")
def rules() -> RuleProcessor:
    return RuleProcessor()


def _frame(*payloads):
    return pd.DataFrame(list(payloads))


def test_clean_batch_has_no_violations(rules):
    assert rules.validate_frame(_frame(good_payload()), as_of=TODAY) == {}


def test_future_date_is_caught(rules):
    future = good_payload(txn_date=(TODAY + dt.timedelta(days=1)).isoformat())
    violations = rules.validate_frame(_frame(future), as_of=TODAY)
    assert 0 in violations
    assert any("future" in v for v in violations[0])


def test_today_is_not_in_the_future(rules):
    """Boundary: a same-day transaction is legitimate and must not be lost."""
    today = good_payload(txn_date=TODAY.isoformat())
    assert rules.validate_frame(_frame(today), as_of=TODAY) == {}


def test_unknown_currency_is_caught(rules):
    violations = rules.validate_frame(_frame(good_payload(currency="ZZZ")), as_of=TODAY)
    assert 0 in violations


def test_null_reference_code_passes(rules):
    """A missing reference becomes a MISSING_REFERENCE_CODE exception
    downstream (Sec. 10). Quarantining it here would delete a whole
    exception category before the classifier ever sees it."""
    assert rules.validate_frame(_frame(good_payload(reference_code=None)), as_of=TODAY) == {}


def test_malformed_reference_code_is_caught(rules):
    violations = rules.validate_frame(
        _frame(good_payload(reference_code="NOT-A-REF")), as_of=TODAY
    )
    assert 0 in violations


def test_only_the_offending_row_is_flagged(rules):
    """One bad record must not condemn its batch."""
    violations = rules.validate_frame(
        _frame(good_payload(), good_payload(currency="ZZZ"), good_payload()),
        as_of=TODAY,
    )
    assert set(violations) == {1}


def test_empty_frame_is_handled(rules):
    assert rules.validate_frame(pd.DataFrame(), as_of=TODAY) == {}


# ── Stage 3: checksum ────────────────────────────────────────────────────


def test_absent_checksum_passes():
    ok, violations = verify_checksum(good_payload())
    assert ok and violations == []


def test_matching_sha256_passes():
    payload = good_payload()
    payload["checksum"] = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    ok, _ = verify_checksum(payload)
    assert ok


def test_corrupted_payload_fails():
    payload = good_payload()
    payload["checksum"] = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    payload["amount"] = 9999.99  # tampered after signing
    ok, violations = verify_checksum(payload)
    assert not ok and "mismatch" in violations[0]


def test_hmac_without_secret_fails_closed():
    payload = good_payload()
    payload["checksum_algorithm"] = "hmac-sha256"
    payload["checksum"] = "0" * 64
    ok, violations = verify_checksum(payload, secret=None)
    assert not ok and "no secret" in violations[0]


def test_hmac_with_secret_verifies():
    import hmac as hmac_mod

    payload = good_payload()
    payload["checksum_algorithm"] = "hmac-sha256"
    payload["checksum"] = hmac_mod.new(
        b"s3cret", canonical_json(payload).encode(), hashlib.sha256
    ).hexdigest()
    ok, _ = verify_checksum(payload, secret="s3cret")
    assert ok


def test_unsupported_algorithm_fails():
    payload = good_payload()
    payload["checksum_algorithm"] = "rot13"
    payload["checksum"] = "abc"
    ok, violations = verify_checksum(payload)
    assert not ok and "unsupported algorithm" in violations[0]


def test_fingerprint_ignores_key_order_and_whitespace():
    a = {"b": 2, "a": 1}
    b = {"a": 1, "b": 2}
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_excludes_the_checksum_field():
    payload = good_payload()
    before = fingerprint(payload)
    payload["checksum"] = "anything"
    assert fingerprint(payload) == before


def test_fingerprint_changes_with_content():
    assert fingerprint(good_payload()) != fingerprint(good_payload(amount=1))


# ── Stage 0: ETL normalisation ───────────────────────────────────────────


def test_json_object_normalises():
    records = normalize(json.dumps(good_payload()))
    assert len(records) == 1 and records[0]["amount"] == 1250.0


def test_json_array_normalises():
    assert len(normalize(json.dumps([good_payload(), good_payload()]))) == 2


def test_csv_normalises_with_aliases():
    csv = (
        "transaction_id,source,transaction_amount,ccy,value_date,narrative,ref\n"
        "TXN-55,bank_api,940.50,USD,2026-01-04,Northwind,REF-88888\n"
    )
    records = normalize(csv)
    assert records[0]["external_id"] == "TXN-55"
    assert records[0]["amount"] == 940.50
    assert records[0]["currency"] == "USD"
    assert records[0]["reference_code"] == "REF-88888"
    assert records[0]["source_type"] == "bank_api"


def test_csv_detection():
    assert looks_like_csv("a,b,c\n1,2,3")
    assert not looks_like_csv('{"a": 1}')
    assert not looks_like_csv('[{"a": 1}]')


def test_unparseable_json_is_passed_through_not_dropped():
    """A corrupt message must reach the validator so it lands in quarantine
    with its payload intact, rather than disappearing from the audit trail."""
    records = normalize("{not valid json")
    assert len(records) == 1
    assert "_unparseable" in records[0]


def test_empty_strings_become_null():
    records = normalize(json.dumps(good_payload(reference_code="")))
    assert records[0]["reference_code"] is None


def test_normalisation_never_invents_a_missing_field():
    payload = good_payload()
    del payload["currency"]
    records = normalize(json.dumps(payload))
    assert records[0].get("currency") is None


def test_alias_does_not_overwrite_an_explicit_canonical_column():
    csv = "external_id,ref,reference_code,source_type,amount,currency,date\n" \
          "TXN-1,REF-11111,REF-99999,erp,10.00,USD,2026-01-01\n"
    records = normalize(csv)
    assert records[0]["reference_code"] == "REF-99999"


# ── Staging buffer ───────────────────────────────────────────────────────


def test_buffer_flushes_when_full():
    buffer = StagingBuffer(batch_size=3, max_wait_seconds=999)
    buffer.add([{"a": 1}, {"a": 2}])
    assert not buffer.should_flush()
    buffer.add([{"a": 3}])
    assert buffer.should_flush()
    assert len(buffer.drain()) == 3
    assert buffer.size == 0


def test_buffer_flushes_on_age():
    buffer = StagingBuffer(batch_size=1000, max_wait_seconds=0)
    buffer.add([{"a": 1}])
    assert buffer.should_flush()


def test_empty_buffer_does_not_flush():
    assert not StagingBuffer().should_flush()


# ── Pipeline composition ─────────────────────────────────────────────────


@pytest.fixture(scope="module")
def pipeline(rules) -> ValidationPipeline:
    return ValidationPipeline(rule_processor=rules, cache=None)


def test_pipeline_returns_one_decision_per_input(pipeline):
    payloads = [good_payload(), good_payload(amount=-1), "not a dict"]
    result = pipeline.validate_batch(payloads, as_of=TODAY)
    assert result.total == 3
    assert [d.index for d in result.decisions] == [0, 1, 2]


def test_pipeline_attributes_each_failure_to_its_stage(pipeline):
    payloads = [
        good_payload(),                                                  # passes
        good_payload(amount=-1),                                         # schema
        good_payload(currency="ZZZ"),                                    # business rule
    ]
    corrupt = good_payload()
    corrupt["checksum"] = "0" * 64
    payloads.append(corrupt)                                             # checksum

    decisions = pipeline.validate_batch(payloads, as_of=TODAY).decisions
    assert decisions[0].status is ValidationState.PASSED
    assert decisions[1].stage is ValidationStage.SCHEMA
    assert decisions[2].stage is ValidationStage.BUSINESS_RULE
    assert decisions[3].stage is ValidationStage.CHECKSUM


def test_passed_decisions_carry_their_parsed_transaction(pipeline):
    decision = pipeline.validate_batch([good_payload()], as_of=TODAY).decisions[0]
    assert decision.transaction is not None
    assert decision.transaction.currency == "USD"


def test_quarantined_decisions_keep_the_original_payload(pipeline):
    bad = good_payload(amount=-1)
    decision = pipeline.validate_batch([bad], as_of=TODAY).decisions[0]
    assert decision.payload == bad
    assert decision.violations


def test_empty_batch_is_safe(pipeline):
    assert pipeline.validate_batch([]).total == 0


def test_summary_counts_add_up(pipeline):
    payloads = [good_payload(), good_payload(currency="ZZZ"), good_payload(amount=0)]
    summary = pipeline.validate_batch(payloads, as_of=TODAY).summary()
    assert summary["total"] == 3
    assert summary["passed"] + summary["quarantined"] == 3


def test_cached_verdict_is_reused_not_recomputed():
    """The cache must return the pipeline's own earlier verdict, never a
    default. A miss recomputes; it never assumes pass."""

    class RecordingCache:
        def __init__(self):
            self.store: dict[str, dict] = {}
            self.hits = 0

        def get(self, fp):
            entry = self.store.get(fp)
            if entry:
                self.hits += 1
            return entry

        def set(self, fp, decision):
            self.store[fp] = decision

    cache = RecordingCache()
    pipe = ValidationPipeline(cache=cache)
    payload = good_payload(currency="ZZZ")

    first = pipe.validate_batch([payload], as_of=TODAY).decisions[0]
    second = pipe.validate_batch([payload], as_of=TODAY).decisions[0]

    assert cache.hits == 1
    assert second.from_cache and not first.from_cache
    assert second.status is first.status is ValidationState.QUARANTINED
    assert second.violations == first.violations


def test_resubmitting_a_valid_record_does_not_crash_persistence():
    """A duplicate submission must be skipped, not re-persisted.

    Regression: `to_cache_entry` deliberately caches the verdict alone and
    never the parsed Transaction, so a cached PASSED decision arrives at
    `persist_batch` with `transaction=None`. It was handed straight to
    `_transaction_row`, whose assertion then took down the entire batch with a
    500. Re-submitting a record is ordinary - a retry, a replayed feed, a
    re-run seeder - so an everyday duplicate became an outage.

    Uses a stub session because the defect fires before any database work;
    keeping it here means it runs without Postgres.
    """
    from services.validation_pipeline.app.quarantine import persist_batch

    class StubSession:
        def __init__(self):
            self.added = []
            self.commits = 0

        def add(self, row):
            self.added.append(row)

        def commit(self):
            self.commits += 1

    class DictCache:
        def __init__(self):
            self.store = {}

        def get(self, fp):
            return self.store.get(fp)

        def set(self, fp, decision):
            self.store[fp] = decision

    pipe = ValidationPipeline(cache=DictCache())
    payload = good_payload()

    first = pipe.validate_batch([payload], as_of=TODAY)
    assert first.decisions[0].passed and not first.decisions[0].from_cache

    session = StubSession()
    written = persist_batch(session, first)
    assert written["transactions_inserted"] == 1
    assert written["duplicates_skipped"] == 0

    # The same payload again: the verdict is cached, so nothing new is written.
    second = pipe.validate_batch([payload], as_of=TODAY)
    assert second.decisions[0].from_cache
    assert second.decisions[0].transaction is None

    repeat = StubSession()
    written_again = persist_batch(repeat, second)  # used to raise AssertionError
    assert written_again["transactions_inserted"] == 0
    assert written_again["duplicates_skipped"] == 1
    assert repeat.added == [], "a duplicate must not write any row"
