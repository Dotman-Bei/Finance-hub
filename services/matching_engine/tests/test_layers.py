"""Unit tests for the two layers, scoring and model persistence (Sec. 9).

The gates in test_precision.py and test_latency.py measure the engine in
aggregate. These pin the individual behaviours that make those numbers hold,
so a regression names the component that broke.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pandas as pd
import pytest

from services.matching_engine.app.ml_model import FuzzyMatcher, normalize_description
from services.matching_engine.app.pipeline import MatchingPipeline
from services.matching_engine.app.rule_engine import rule_match, split_by_side
from services.matching_engine.app.scoring import (
    ScoredPair,
    amount_proximity,
    date_proximity,
    partition_by_threshold,
    reference_agreement,
    resolve_one_to_one,
    score_pair,
)
from services.matching_engine.tests.ground_truth import historical_descriptions

TODAY = dt.date.today()


def txn(**overrides):
    row = {
        "id": uuid.uuid4(),
        "external_id": "TXN-1",
        "source_type": "erp",
        "amount": 1000.00,
        "currency": "USD",
        "txn_date": TODAY - dt.timedelta(days=5),
        "description": "Meridian Capital Ltd - settlement",
        "reference_code": "REF-12345",
    }
    row.update(overrides)
    return row


# ── Layer 1: rule engine ─────────────────────────────────────────────────


def test_exact_triple_matches():
    internal = pd.DataFrame([txn()])
    external = pd.DataFrame([txn(source_type="bank_api")])
    pairs, unmatched = rule_match(internal, external)

    assert len(pairs) == 1
    assert pairs.iloc[0]["match_type"] == "RULE"
    assert pairs.iloc[0]["confidence_score"] == 1.0
    assert unmatched.empty


def test_null_reference_codes_do_not_match_each_other():
    """pandas merge treats NaN as equal to NaN. Two unrelated records that both
    lack a reference but share an amount and date would otherwise be declared
    an exact match - a false positive the threshold can never catch, because
    rule-layer pairs bypass it."""
    internal = pd.DataFrame([txn(reference_code=None)])
    external = pd.DataFrame([txn(reference_code=None, source_type="bank_api")])
    pairs, unmatched = rule_match(internal, external)

    assert pairs.empty
    assert len(unmatched) == 2


def test_differing_amount_does_not_match():
    internal = pd.DataFrame([txn(amount=1000.00)])
    external = pd.DataFrame([txn(amount=1000.01, source_type="bank_api")])
    pairs, _ = rule_match(internal, external)
    assert pairs.empty


def test_amounts_are_compared_at_two_decimal_places():
    """100.10 from JSON and from CSV can differ in the last bit; NUMERIC(18,2)
    would store them identically, so the key must too."""
    internal = pd.DataFrame([txn(amount=1000.1)])
    external = pd.DataFrame([txn(amount=1000.100000001, source_type="bank_api")])
    pairs, _ = rule_match(internal, external)
    assert len(pairs) == 1


def test_duplicate_keys_pair_one_to_one_not_cartesian():
    """Three identical keys either side is 9 rows under an inner merge, and
    every one would be persisted as a confirmed pair."""
    internal = pd.DataFrame([txn(), txn(), txn()])
    external = pd.DataFrame([txn(source_type="bank_api") for _ in range(3)])
    pairs, unmatched = rule_match(internal, external)

    assert len(pairs) == 3
    assert len(set(pairs["id_int"])) == 3
    assert len(set(pairs["id_ext"])) == 3
    assert unmatched.empty


def test_surplus_on_one_side_goes_to_the_ml_layer():
    internal = pd.DataFrame([txn(), txn(), txn()])
    external = pd.DataFrame([txn(source_type="bank_api")])
    pairs, unmatched = rule_match(internal, external)
    assert len(pairs) == 1
    assert len(unmatched) == 2


def test_empty_side_is_safe():
    pairs, unmatched = rule_match(pd.DataFrame(), pd.DataFrame([txn()]))
    assert pairs.empty
    assert len(unmatched) == 1


def test_split_by_side_uses_source_type():
    frame = pd.DataFrame([
        txn(source_type="erp"),
        txn(source_type="bank_api"),
        txn(source_type="payment_gateway"),
    ])
    internal, external = split_by_side(frame)
    assert len(internal) == 1 and len(external) == 2


# ── Scoring ──────────────────────────────────────────────────────────────


def test_identical_amounts_score_one():
    assert amount_proximity(100.00, 100.00) == 1.0


def test_amount_proximity_is_relative_not_absolute():
    """A 5.00 gap is trivial on 200,000 and fatal on 20."""
    assert amount_proximity(200_000, 199_995) > 0.9
    assert amount_proximity(20, 15) == 0.0


def test_amount_beyond_tolerance_scores_zero():
    assert amount_proximity(100, 70) == 0.0


def test_same_day_scores_one_and_drift_decays():
    assert date_proximity(TODAY, TODAY) == 1.0
    assert 0 < date_proximity(TODAY, TODAY - dt.timedelta(days=5)) < 1
    assert date_proximity(TODAY, TODAY - dt.timedelta(days=30)) == 0.0


def test_reference_absence_is_neutral_not_penalising():
    """A missing reference is the MISSING_REFERENCE_CODE case, not evidence
    against the match."""
    assert reference_agreement("REF-1", None) == 0.5
    assert reference_agreement("REF-1", "REF-1") == 1.0
    assert reference_agreement("REF-1", "REF-2") == 0.0


def test_score_is_bounded_and_components_reported():
    pair = score_pair(txn(), txn(source_type="bank_api"), description_similarity=1.0)
    assert 0.0 <= pair.confidence <= 1.0
    assert set(pair.components) == {"description", "amount", "date", "reference"}


def test_a_perfect_pair_scores_one():
    left = txn()
    right = txn(id=uuid.uuid4(), source_type="bank_api")
    assert score_pair(left, right, description_similarity=1.0).confidence == 1.0


def test_partial_payment_scores_below_a_full_match():
    left = txn(amount=1000.00)
    partial = txn(id=uuid.uuid4(), amount=880.00, source_type="bank_api")
    full = txn(id=uuid.uuid4(), source_type="bank_api")

    assert (
        score_pair(left, partial, 1.0).confidence
        < score_pair(left, full, 1.0).confidence
    )


def test_one_to_one_resolution_prefers_higher_confidence():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    pairs = [
        ScoredPair(internal_id=a, external_id=b, confidence=0.70),
        ScoredPair(internal_id=a, external_id=c, confidence=0.95),
    ]
    resolved = resolve_one_to_one(pairs)
    assert len(resolved) == 1
    assert resolved[0].external_id == c


def test_threshold_partitions_correctly():
    pairs = [
        ScoredPair(internal_id=1, external_id=2, confidence=0.90),
        ScoredPair(internal_id=3, external_id=4, confidence=0.80),
    ]
    above, below = partition_by_threshold(pairs, 0.85)
    assert len(above) == 1 and len(below) == 1


# ── Layer 2: fuzzy matcher ───────────────────────────────────────────────


def test_description_normalisation_strips_bank_noise():
    assert normalize_description("MERIDIAN//REF4821   SETTLEMENT") == (
        "meridian ref4821 settlement"
    )
    assert normalize_description(None) == ""


def test_fit_requires_enough_text():
    with pytest.raises(ValueError, match="at least 2"):
        FuzzyMatcher().fit(["only one"])


def test_persisted_models_round_trip(tmp_path):
    original = FuzzyMatcher().fit(historical_descriptions(count=100))
    original.save(tmp_path)

    loaded = FuzzyMatcher.load(tmp_path)
    assert loaded is not None
    assert loaded.is_fitted
    assert loaded.vectorizer.vocabulary_ == original.vectorizer.vocabulary_


def test_load_returns_none_when_nothing_is_fitted(tmp_path):
    """The service must start and report the state, not crash-loop."""
    assert FuzzyMatcher.load(tmp_path) is None


def test_refusing_to_persist_an_unfitted_matcher(tmp_path):
    with pytest.raises(RuntimeError, match="unfitted"):
        FuzzyMatcher().save(tmp_path)


def test_tiny_batch_is_not_all_flagged_as_outliers():
    """LOF needs a neighbourhood. A two-row weekend batch has none, and
    declaring both isolated would push every quiet day to the exception queue."""
    matcher = FuzzyMatcher()
    frame = pd.DataFrame([txn(), txn(source_type="bank_api")])
    _, outliers, _ = matcher.cluster(frame)
    assert all(flag == 1 for flag in outliers)


def test_blank_descriptions_do_not_raise():
    """TF-IDF raises on an empty vocabulary; descriptions are nullable."""
    matcher = FuzzyMatcher()
    frame = pd.DataFrame([txn(description=None), txn(description="", source_type="bank_api")])
    clusters, outliers, matrix = matcher.cluster(frame)
    assert matrix is None
    assert len(clusters) == 2


# ── Pipeline invariants ──────────────────────────────────────────────────


@pytest.fixture(scope="module")
def pipeline() -> MatchingPipeline:
    return MatchingPipeline(matcher=FuzzyMatcher().fit(historical_descriptions()))


def test_rule_matches_bypass_the_threshold(pipeline):
    """An exact triple is certain. Even a threshold of 1.0 must not discard it."""
    frame = pd.DataFrame([txn(), txn(id=uuid.uuid4(), source_type="bank_api")])
    strict = MatchingPipeline(matcher=pipeline.matcher, threshold=1.0)
    result = strict.reconcile(frame)
    assert result.rule_matched == 1


def test_single_sided_batch_yields_no_matches(pipeline):
    frame = pd.DataFrame([txn(), txn(id=uuid.uuid4())])  # both erp
    result = pipeline.reconcile(frame)
    assert not result.matched
    assert len(result.unmatched) == 2


def test_match_rate_is_bounded(pipeline):
    frame = pd.DataFrame([txn(), txn(id=uuid.uuid4(), source_type="bank_api")])
    assert 0.0 <= pipeline.reconcile(frame).match_rate <= 1.0


def test_threshold_is_read_from_settings():
    from shared.config import settings

    assert MatchingPipeline().threshold == settings.match_confidence_threshold


def test_summary_has_the_shape_section_9_specifies(pipeline):
    frame = pd.DataFrame([txn(), txn(id=uuid.uuid4(), source_type="bank_api")])
    summary = pipeline.reconcile(frame).summary()
    for key in ("matched", "unmatched", "match_rate"):
        assert key in summary


# ── Co-settling nomination (what makes SPLIT_SETTLEMENT reachable) ───────
#
# These guard a seam, not a component. The engine recorded one best
# counterpart per unmatched row, so `counterpart_count` never exceeded 1 and
# Sec. 10's SPLIT_SETTLEMENT - which is defined by multiplicity - could not
# occur in the assembled system. Every isolated test still passed, because the
# classifier corpus builds features from a hand-made counterpart list and
# never runs the matcher. Only a test spanning both sides catches it.


#: Filler counterparties. The ML layer reaches split legs through the
#: description-cluster channel, not amount/date blocking - a leg worth a third
#: of the obligation is far outside the 20% blocking tolerance. Clustering
#: needs a batch with structure in it, so these pad the frame; a four-row
#: frame produces no clusters and every row reports "no candidate counterpart".
_FILLER = [
    "Arcadia Payments BV", "Solstice Retail Group", "Harborline Freight",
    "Lumen Energy Partners", "Orion Manufacturing", "Bluepeak Insurance",
    "Fairmount Trading Co", "Ridgeway Chemicals",
]


def _obligation_and_legs(total: float, shares: list[float]) -> pd.DataFrame:
    """One internal obligation discharged by several external receipts."""
    rows = [
        txn(
            external_id="ERP-SPLIT",
            amount=total,
            reference_code="REF-SPLIT",
            description="Northwind Logistics - invoice remittance",
        )
    ]
    rows.extend(
        txn(
            id=uuid.uuid4(),
            external_id=f"BNK-LEG-{n}",
            source_type="bank_api",
            amount=round(total * share, 2),
            reference_code=None,
            description=f"Northwind Logistics - invoice remittance part {n}",
            txn_date=TODAY - dt.timedelta(days=4),
        )
        for n, share in enumerate(shares)
    )

    # Unrelated but well-formed traffic, so the batch clusters like a real one.
    for n, counterparty in enumerate(_FILLER):
        amount = 500.00 + n * 250
        for side in ("erp", "bank_api"):
            rows.append(
                txn(
                    id=uuid.uuid4(),
                    external_id=f"{side.upper()}-FILL-{n}",
                    source_type=side,
                    amount=amount,
                    reference_code=f"REF-FILL-{n}",
                    description=f"{counterparty} - settlement",
                    txn_date=TODAY - dt.timedelta(days=7),
                )
            )

    return pd.DataFrame(rows)


def _item_for(result, frame, external_id):
    wanted = frame.loc[frame["external_id"] == external_id, "id"].iloc[0]
    return next(i for i in result.unmatched if i.transaction_id == wanted)


def test_split_settlement_nominates_every_leg(pipeline):
    frame = _obligation_and_legs(9000.00, [0.34, 0.33, 0.31])
    item = _item_for(pipeline.reconcile(frame), frame, "ERP-SPLIT")

    # Multiplicity is what Sec. 10 keys on; one candidate makes the category
    # unreachable no matter how well the classifier performs.
    assert len(item.candidate_ids) >= 2


def test_a_single_partial_payment_nominates_only_one(pipeline):
    """The inverse guard: over-nominating relabels partials as splits."""
    frame = _obligation_and_legs(9000.00, [0.70])
    item = _item_for(pipeline.reconcile(frame), frame, "ERP-SPLIT")
    assert len(item.candidate_ids) <= 1


def test_equal_value_lookalikes_are_not_co_settling(pipeline):
    """Similar counterparties settling similar amounts are competing matches.

    Each candidate here is worth nearly the whole obligation, so they cannot
    be legs of it. Nominating them would turn every crowded cluster into a
    split settlement.
    """
    frame = _obligation_and_legs(9000.00, [0.99, 0.98, 0.97])
    item = _item_for(pipeline.reconcile(frame), frame, "ERP-SPLIT")
    assert len(item.candidate_ids) <= 1


def test_candidate_ids_survive_into_the_queue_payload():
    """persistence.py must write the key feedback.py reads, or the fix is inert."""
    from services.matching_engine.app.pipeline import UnmatchedItem

    item = UnmatchedItem(
        transaction_id=uuid.uuid4(),
        reason="below confidence threshold",
        best_confidence=0.6,
        best_counterpart_id=uuid.uuid4(),
        candidate_ids=[uuid.uuid4(), uuid.uuid4()],
    )
    assert len(item.candidate_ids) == 2
