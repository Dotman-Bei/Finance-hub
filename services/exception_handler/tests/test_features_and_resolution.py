"""Unit tests for feature engineering, the resolution engine and feedback rules.

test_classifier.py gates accuracy in aggregate. These pin the behaviours that
produce it, so a regression names the component rather than moving a number.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from services.exception_handler.app.classifier import BootstrapClassifier
from services.exception_handler.app.features import (
    FEATURE_NAMES,
    ExceptionFeatures,
    extract,
)
from services.exception_handler.app.feedback import (
    DECISION_ACCEPT,
    DECISION_EDIT,
    DECISION_REJECT,
    _is_usable_label,
)
from services.exception_handler.app.resolution import ACTIONS, PATHWAYS, suggest
from shared.models.enums import ExceptionCategory

TODAY = dt.date.today()
CATEGORIES = [c.value for c in ExceptionCategory]


def txn(**overrides):
    row = {
        "id": uuid.uuid4(),
        "external_id": "ERP-1",
        "source_type": "erp",
        "amount": 10000.00,
        "currency": "USD",
        "txn_date": TODAY - dt.timedelta(days=10),
        "description": "Meridian Capital Ltd - settlement",
        "reference_code": "REF-12345",
    }
    row.update(overrides)
    return row


# ── Feature engineering ──────────────────────────────────────────────────


def test_feature_vector_matches_declared_names():
    """The vector order is the model's input contract."""
    features = extract(txn(), [txn(id=uuid.uuid4())])
    assert len(features.as_vector()) == len(FEATURE_NAMES)
    assert set(features.as_dict()) == set(FEATURE_NAMES)


def test_amount_ratio_uses_the_nearest_counterpart():
    base = txn(amount=1000.0)
    counterparts = [
        txn(id=uuid.uuid4(), amount=200.0),
        txn(id=uuid.uuid4(), amount=950.0),   # nearest
    ]
    assert extract(base, counterparts).amount_ratio == pytest.approx(0.95)


def test_shortfall_uses_the_total_not_the_nearest():
    """A split settlement is only recognisable against the sum of its legs."""
    base = txn(amount=1000.0)
    counterparts = [txn(id=uuid.uuid4(), amount=400.0), txn(id=uuid.uuid4(), amount=600.0)]
    features = extract(base, counterparts)
    assert features.amount_shortfall == pytest.approx(0.0)
    assert features.counterpart_count == 2


def test_no_counterpart_encodes_the_full_amount_as_shortfall():
    features = extract(txn(amount=500.0), [])
    assert features.counterpart_count == 0
    assert features.amount_ratio == 0.0
    assert features.amount_shortfall == pytest.approx(500.0)


def test_missing_reference_is_flagged():
    assert extract(txn(reference_code=None), []).has_reference_code == 0.0
    assert extract(txn(reference_code="   "), []).has_reference_code == 0.0
    assert extract(txn(), []).has_reference_code == 1.0


def test_reference_absence_is_neutral_not_negative():
    both = extract(txn(), [txn(id=uuid.uuid4())])
    one_missing = extract(txn(), [txn(id=uuid.uuid4(), reference_code=None)])
    differing = extract(txn(), [txn(id=uuid.uuid4(), reference_code="REF-99999")])

    assert both.reference_agreement == 1.0
    assert one_missing.reference_agreement == 0.5
    assert differing.reference_agreement == 0.0


def test_date_delta_is_absolute():
    base = txn(txn_date=TODAY)
    early = extract(base, [txn(id=uuid.uuid4(), txn_date=TODAY - dt.timedelta(days=6))])
    late = extract(base, [txn(id=uuid.uuid4(), txn_date=TODAY + dt.timedelta(days=6))])
    assert early.date_delta_days == late.date_delta_days == 6.0


def test_zero_amount_does_not_divide_by_zero():
    assert extract(txn(amount=0.0), [txn(id=uuid.uuid4())]).amount_ratio == 0.0


def test_malformed_dates_do_not_raise():
    features = extract(txn(txn_date="not-a-date"), [txn(id=uuid.uuid4())])
    assert features.date_delta_days == 0.0


# ── Resolution engine ────────────────────────────────────────────────────


@pytest.mark.parametrize("category", CATEGORIES)
def test_every_category_has_a_pathway_and_action(category):
    """Sec. 10's table must be complete, or a classified exception would
    reach the dashboard with no suggested next step."""
    assert category in PATHWAYS and PATHWAYS[category]
    assert category in ACTIONS and ACTIONS[category]


@pytest.mark.parametrize("category", CATEGORIES)
def test_suggestion_shape_matches_the_dashboard_contract(category):
    features = extract(txn(), [txn(id=uuid.uuid4(), amount=8000.0)])
    payload = suggest(category, features, confidence=0.8, engine="bootstrap")

    for key in ("category", "pathway", "action", "detail", "fields", "classifier"):
        assert key in payload
    assert payload["classifier"]["engine"] == "bootstrap"


def test_unknown_category_is_rejected():
    with pytest.raises(ValueError, match="unknown exception category"):
        suggest("NOT_A_CATEGORY", extract(txn(), []))


def test_partial_payment_reports_the_real_residual():
    features = extract(txn(amount=10000.0), [txn(id=uuid.uuid4(), amount=6500.0)])
    fields = suggest("PARTIAL_PAYMENT", features)["fields"]
    assert fields["settled_amount"] == pytest.approx(6500.0)
    assert fields["residual_balance"] == pytest.approx(3500.0)


def test_split_settlement_reports_the_unallocated_remainder():
    features = extract(
        txn(amount=10000.0),
        [txn(id=uuid.uuid4(), amount=4000.0), txn(id=uuid.uuid4(), amount=5000.0)],
    )
    fields = suggest("SPLIT_SETTLEMENT", features)["fields"]
    assert fields["candidate_legs"] == 2
    assert fields["unallocated_remainder"] == pytest.approx(1000.0)


def test_missing_reference_with_no_candidate_admits_it():
    """Inventing a candidate would send a reviewer down a false trail."""
    payload = suggest("MISSING_REFERENCE_CODE", extract(txn(reference_code=None), []))
    assert payload["fields"]["candidate_count"] == 0
    assert payload["fields"]["next_step"] == "MANUAL_SEARCH"
    assert "No counterpart candidate" in payload["detail"]


def test_timing_difference_derives_hold_date_from_observed_drift():
    features = extract(
        txn(txn_date=TODAY - dt.timedelta(days=20)),
        [txn(id=uuid.uuid4(), txn_date=TODAY - dt.timedelta(days=13))],
    )
    fields = suggest("TIMING_DIFFERENCE", features)["fields"]
    assert fields["period_drift_days"] == 7
    assert fields["hold_until"] == (TODAY + dt.timedelta(days=7)).isoformat()


# ── Bootstrap rules ──────────────────────────────────────────────────────


def test_bootstrap_never_returns_an_unknown_category():
    engine = BootstrapClassifier()
    for features in (
        extract(txn(), []),
        extract(txn(), [txn(id=uuid.uuid4())]),
        extract(txn(amount=0.0), []),
        ExceptionFeatures(),
    ):
        assert engine.classify(features).category in CATEGORIES


def test_bootstrap_confidence_is_a_probability():
    engine = BootstrapClassifier()
    features = extract(txn(), [txn(id=uuid.uuid4(), amount=7000.0)])
    assert 0.0 <= engine.classify(features).confidence <= 1.0


def test_bootstrap_always_gives_a_rationale():
    """It goes into the audit trail; a suggestion with no stated reason is
    not reviewable."""
    engine = BootstrapClassifier()
    assert engine.classify(extract(txn(), [txn(id=uuid.uuid4())])).rationale


def test_orphan_routes_to_a_manual_search_pathway():
    engine = BootstrapClassifier()
    result = engine.classify(extract(txn(reference_code=None), []))
    assert result.category == "MISSING_REFERENCE_CODE"
    # Low confidence: nothing was found, and the suggestion should say so.
    assert result.confidence < 0.6


# ── Feedback label rules ─────────────────────────────────────────────────


def test_accept_is_a_label():
    assert _is_usable_label(DECISION_ACCEPT, ExceptionCategory.PARTIAL_PAYMENT) is True


def test_edit_is_a_label():
    assert _is_usable_label(DECISION_EDIT, ExceptionCategory.TIMING_DIFFERENCE) is True


def test_reject_is_not_a_label():
    """A rejection says the suggestion was wrong, never what was right.
    Training on it would teach the forest the opposite of the human's meaning.
    """
    assert _is_usable_label(DECISION_REJECT, ExceptionCategory.PARTIAL_PAYMENT) is False


def test_no_category_is_not_a_label():
    assert _is_usable_label(DECISION_ACCEPT, None) is False
