"""End-to-end flow across all four subsystems (build.md Sec. 1).

    "external sources -> validation pipeline -> matching engine ->
     (matched -> ledger) / (unmatched -> exception queue) -> dashboard"

Every phase so far has been tested in isolation. This exercises the seam
between them, which is where the interesting failures live: subsystem 1 writes
`best_counterpart_id` onto a queue row and subsystem 3 reads it; the matching
engine emits `source_type` values the GE suite has to accept; the classifier's
feature extraction consumes what the matcher actually recorded, not what it was
assumed to record.

Runs without any infrastructure. The pipelines were built I/O-free precisely so
this is possible - persistence, Kafka and Redis are separate layers, covered by
the integration tests that need a live database.

Data flows through the *real* components at every step: the real Pydantic
models, the real Great Expectations suite, the real TF-IDF/DBSCAN matcher, the
real classifier and resolution engine. Nothing is stubbed.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pandas as pd
import pytest

from services.exception_handler.app.classifier import ExceptionClassifier
from services.exception_handler.app.features import extract
from services.exception_handler.app.resolution import suggest
from services.matching_engine.app.ml_model import FuzzyMatcher
from services.matching_engine.app.pipeline import MatchingPipeline
from services.validation_pipeline.app.ingestion import normalize
from services.validation_pipeline.app.pipeline import ValidationPipeline
from shared.models.enums import ExceptionCategory, ValidationState

TODAY = dt.date.today()


# ── The feed ─────────────────────────────────────────────────────────────


def _erp_csv() -> str:
    """The internal book, arriving as CSV with vendor column names."""
    return (
        "transaction_id,source,transaction_amount,ccy,value_date,narrative,ref\n"
        "ERP-001,erp,12500.00,USD,{d},Meridian Capital Ltd - settlement,REF-40001\n"
        "ERP-002,erp,8400.00,USD,{d},Northwind Logistics - invoice remittance,REF-40002\n"
        "ERP-003,erp,3300.00,USD,{d},Orion Manufacturing - wire transfer,REF-40003\n"
        "ERP-004,erp,990.00,USD,{d},Harborline Freight - ACH credit,REF-40004\n"
    ).format(d=(TODAY - dt.timedelta(days=6)).isoformat())


def _bank_json() -> list[dict]:
    """The external feed, as JSON. Deliberately messier than the ERP side."""
    six = (TODAY - dt.timedelta(days=6)).isoformat()
    return [
        # 1. Exact counterpart to ERP-001 -> rule layer.
        {
            "external_id": "BNK-001", "source_type": "bank_api", "amount": 12500.00,
            "currency": "USD", "txn_date": six, "reference_code": "REF-40001",
            "description": "Meridian Capital Ltd - settlement",
        },
        # 2. Same amount as ERP-002 but posted late and with no reference
        #    -> ML layer, then an exception.
        {
            "external_id": "BNK-002", "source_type": "bank_api", "amount": 8400.00,
            "currency": "USD", "txn_date": TODAY.isoformat(), "reference_code": None,
            "description": "NORTHWIND//REF9931 INVOICE",
        },
        # 3. Settles only part of ERP-003 -> partial payment.
        {
            "external_id": "BNK-003", "source_type": "payment_gateway", "amount": 2100.00,
            "currency": "USD", "txn_date": six, "reference_code": None,
            "description": "Orion Manufacturing - wire transfer",
        },
        # 4. Structurally broken: negative amount. Must never reach matching.
        {
            "external_id": "BNK-BAD1", "source_type": "bank_api", "amount": -50.00,
            "currency": "USD", "txn_date": six, "reference_code": "REF-40009",
            "description": "Reversal",
        },
        # 5. Rule-breaking: currency outside the allowed set.
        {
            "external_id": "BNK-BAD2", "source_type": "bank_api", "amount": 700.00,
            "currency": "ZZZ", "txn_date": six, "reference_code": "REF-40010",
            "description": "Unknown currency",
        },
    ]


@pytest.fixture(scope="module")
def flow():
    """Run the whole chain once; every test inspects the same run."""
    # ── Stage 1: ingestion + validation (Subsystem 2) ────────────────────
    records = normalize(_erp_csv()) + _bank_json()
    validation = ValidationPipeline(cache=None).validate_batch(records, as_of=TODAY)

    accepted = [d for d in validation.decisions if d.passed]

    # Only validated transactions continue — Sec. 8's guarantee that nothing
    # reaches the database, or anything downstream, unvalidated.
    frame = pd.DataFrame(
        [
            {
                "id": uuid.uuid4(),
                "external_id": d.transaction.external_id,
                "source_type": d.transaction.source_type,
                "amount": float(d.transaction.amount),
                "currency": d.transaction.currency,
                "txn_date": d.transaction.txn_date,
                "description": d.transaction.description,
                "reference_code": d.transaction.reference_code,
            }
            for d in accepted
        ]
    )

    # ── Stage 2: reconciliation (Subsystem 1) ────────────────────────────
    reconciliation = MatchingPipeline(matcher=FuzzyMatcher()).reconcile(frame)

    # ── Stage 3: triage (Subsystem 3) ────────────────────────────────────
    by_id = {row["id"]: row for _, row in frame.iterrows()}
    classifier = ExceptionClassifier(path="/nonexistent/rf.pkl")   # bootstrap engine

    triaged = []
    for item in reconciliation.unmatched:
        txn = by_id[item.transaction_id]
        counterparts = (
            [by_id[item.best_counterpart_id]]
            if item.best_counterpart_id in by_id
            else []
        )
        features = extract(
            txn.to_dict(),
            [c.to_dict() for c in counterparts],
            {"best_confidence": item.best_confidence},
        )
        result = classifier.classify(features)
        triaged.append(
            {
                "transaction": txn,
                "reason": item.reason,
                "category": result.category,
                "confidence": result.confidence,
                "engine": result.engine,
                "suggestion": suggest(
                    result.category, features, result.confidence, result.engine
                ),
            }
        )

    return {
        "records": records,
        "validation": validation,
        "frame": frame,
        "reconciliation": reconciliation,
        "triaged": triaged,
    }


# ── Stage 1: nothing invalid escapes ─────────────────────────────────────


def test_csv_and_json_feeds_both_normalise(flow):
    """Sec. 8: 'Accept CSV and JSON payloads'. Vendor column names on the ERP
    side must map to the canonical schema."""
    externals = {r.get("external_id") for r in flow["records"]}
    assert "ERP-001" in externals   # came through the CSV alias path
    assert "BNK-001" in externals   # came through JSON


def test_malformed_records_are_quarantined_before_matching(flow):
    quarantined = {
        d.payload.get("external_id") for d in flow["validation"].quarantined
    }
    assert "BNK-BAD1" in quarantined   # negative amount, stage 1
    assert "BNK-BAD2" in quarantined   # unknown currency, stage 2


def test_quarantined_records_never_reach_the_matching_engine(flow):
    """The load-bearing guarantee of the whole ordering: a record the
    validation pipeline rejected must not appear downstream in any form."""
    downstream = set(flow["frame"]["external_id"])
    assert "BNK-BAD1" not in downstream
    assert "BNK-BAD2" not in downstream


def test_every_record_is_either_accepted_or_quarantined(flow):
    validation = flow["validation"]
    assert len(validation.passed) + len(validation.quarantined) == validation.total
    assert all(
        d.status in (ValidationState.PASSED, ValidationState.QUARANTINED)
        for d in validation.decisions
    )


# ── Stage 2: reconciliation ──────────────────────────────────────────────


def test_the_exact_pair_is_matched_by_the_rule_layer(flow):
    """ERP-001 and BNK-001 agree on reference, amount and date."""
    reconciliation = flow["reconciliation"]
    lookup = {row["id"]: row["external_id"] for _, row in flow["frame"].iterrows()}

    rule_pairs = {
        frozenset({lookup[p.internal_id], lookup[p.external_id]})
        for p in reconciliation.matched
        if p.match_type == "RULE"
    }
    assert frozenset({"ERP-001", "BNK-001"}) in rule_pairs


def test_partial_and_late_items_are_not_auto_matched(flow):
    """Sec. 10 exists because these need a human. Confirming them
    automatically would post wrong ledger entries."""
    lookup = {row["id"]: row["external_id"] for _, row in flow["frame"].iterrows()}
    matched_ids = {p.internal_id for p in flow["reconciliation"].matched}
    matched_ids |= {p.external_id for p in flow["reconciliation"].matched}
    matched = {lookup[i] for i in matched_ids}

    assert "BNK-003" not in matched   # settles 2100 of 3300


def test_no_transaction_is_both_matched_and_queued(flow):
    reconciliation = flow["reconciliation"]
    matched = {p.internal_id for p in reconciliation.matched} | {
        p.external_id for p in reconciliation.matched
    }
    queued = {u.transaction_id for u in reconciliation.unmatched}
    assert not (matched & queued)


def test_every_validated_transaction_is_accounted_for(flow):
    """Matched plus queued must cover the input. A transaction that is neither
    reconciled nor visible to a human has silently disappeared."""
    reconciliation = flow["reconciliation"]
    accounted = {p.internal_id for p in reconciliation.matched}
    accounted |= {p.external_id for p in reconciliation.matched}
    accounted |= {u.transaction_id for u in reconciliation.unmatched}
    assert len(accounted) == len(flow["frame"])


# ── Stage 3: the seam between subsystems 1 and 3 ─────────────────────────


def test_every_queued_item_is_classified_and_given_a_pathway(flow):
    """Sec. 16 Phase 3: 'classifier assigns all 4 categories; resolution
    suggestions written to queue'."""
    categories = {c.value for c in ExceptionCategory}
    assert flow["triaged"], "nothing reached the exception queue"

    for item in flow["triaged"]:
        assert item["category"] in categories
        assert item["suggestion"]["pathway"]
        assert item["suggestion"]["action"]
        assert item["suggestion"]["detail"]


def test_the_partial_payment_is_recognised_as_one(flow):
    """The end-to-end assertion that matters: a transaction that entered as raw
    JSON, survived validation, failed to match on amount, and was carried
    across two service boundaries still arrives at the right category with the
    right remediation."""
    partial = next(
        (t for t in flow["triaged"] if t["transaction"]["external_id"] == "BNK-003"),
        None,
    )
    assert partial is not None, "BNK-003 never reached the exception queue"
    assert partial["category"] == ExceptionCategory.PARTIAL_PAYMENT.value

    fields = partial["suggestion"]["fields"]
    # 3300 obligation, 2100 settled -> 1200 outstanding. Computed, not guessed.
    assert fields["residual_balance"] == pytest.approx(1200.0, abs=0.01)


def test_suggestions_record_which_engine_produced_them(flow):
    """No trained model exists here, so every suggestion must be attributed to
    the bootstrap rules — never presented as a learned prediction."""
    assert all(t["engine"] == "bootstrap" for t in flow["triaged"])
    assert all(
        t["suggestion"]["classifier"]["engine"] == "bootstrap" for t in flow["triaged"]
    )


def test_no_suggestion_invents_a_counterpart(flow):
    """Where the matcher nominated nothing, the resolution must say so rather
    than fabricate a candidate to look helpful."""
    for item in flow["triaged"]:
        fields = item["suggestion"]["fields"]
        if fields.get("candidate_count") == 0:
            assert "No counterpart candidate" in item["suggestion"]["detail"]


# ── The flow as a whole ──────────────────────────────────────────────────


def test_match_rate_is_reported_and_bounded(flow):
    summary = flow["reconciliation"].summary()
    assert 0.0 <= summary["match_rate"] <= 1.0
    assert summary["matched"] + summary["unmatched"] > 0


def test_the_chain_conserves_every_record(flow, capsys):
    """Nothing is lost or duplicated between the front door and the queue."""
    total_in = len(flow["records"])
    quarantined = len(flow["validation"].quarantined)
    validated = len(flow["frame"])
    matched_txns = len(flow["reconciliation"].matched) * 2
    queued = len(flow["reconciliation"].unmatched)

    with capsys.disabled():
        print(
            f"\n  {total_in} ingested -> {quarantined} quarantined, "
            f"{validated} validated -> {matched_txns} reconciled, {queued} queued"
        )
        for item in flow["triaged"]:
            print(
                f"    {item['transaction']['external_id']:<8} "
                f"{item['category']:<23} conf {item['confidence']:.2f}"
            )

    assert quarantined + validated == total_in
    assert matched_txns + queued == validated
