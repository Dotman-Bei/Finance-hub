"""RELEASE GATE - Objective 3 classifier accuracy (build.md Sec. 14).

    "Per-category precision/recall on held-out exception set."

Per-category is the operative word. A single accuracy figure hides the failure
mode that matters here: exception data is imbalanced, so a model that always
predicts the largest category can score well overall while being worthless on
the rest. Every category is therefore gated individually.

Both engines are measured. The bootstrap rules are what serves a cold-start
deployment, so their accuracy is a real production number, not a curiosity -
if they are poor, the first weeks of triage are poor.
"""

from __future__ import annotations

import pytest
from sklearn.metrics import classification_report, confusion_matrix

from services.exception_handler.app.classifier import (
    BootstrapClassifier,
    ExceptionClassifier,
)
from services.exception_handler.tests.exception_corpus import CATEGORIES, build_corpus

#: Gates are measured on the HARD corpus - half ambiguous cases carrying two
#: competing signals. On the clean corpus every category separates perfectly
#: for both engines, which measures the corpus, not the classifier.
HARD_FRACTION = 0.5

#: The bootstrap rules are fixed thresholds, so genuinely ambiguous cases cost
#: them accuracy. This is the floor for what a cold-start deployment serves.
BOOTSTRAP_PRECISION = 0.70
BOOTSTRAP_RECALL = 0.70

#: The forest can weigh features jointly, which is the whole reason Sec. 10
#: specifies a learned model over a rule set.
FOREST_PRECISION = 0.90
FOREST_RECALL = 0.90
FOREST_MACRO_F1 = 0.90

PER_CATEGORY = 200


@pytest.fixture(scope="module")
def corpus():
    """The corpus the gates are measured on: half ambiguous."""
    return build_corpus(per_category=PER_CATEGORY, hard_fraction=HARD_FRACTION)


@pytest.fixture(scope="module")
def clean_corpus():
    """Cleanly separable categories - used only for the coverage milestone."""
    return build_corpus(per_category=100, hard_fraction=0.0)


@pytest.fixture(scope="module")
def bootstrap_report(corpus):
    engine = BootstrapClassifier()
    predicted = [engine.classify(record.features).category for record in corpus]
    actual = [record.category for record in corpus]
    return actual, predicted, classification_report(
        actual, predicted, output_dict=True, zero_division=0
    )


@pytest.fixture(scope="module")
def forest(tmp_path_factory, corpus):
    """Train on 75%, hold out 25% - the split is inside `train`."""
    model_path = tmp_path_factory.mktemp("models") / "rf_classifier.pkl"
    classifier = ExceptionClassifier(path=model_path)
    metrics = classifier.train(
        [(r.features, r.category) for r in corpus], human_labelled=0
    )
    return classifier, metrics


# ── Bootstrap engine: what a cold-start deployment actually serves ───────


@pytest.mark.parametrize("category", CATEGORIES)
def test_bootstrap_precision_per_category(bootstrap_report, category):
    _, _, report = bootstrap_report
    precision = report.get(category, {}).get("precision", 0.0)
    assert precision >= BOOTSTRAP_PRECISION, (
        f"bootstrap precision for {category} is {precision:.3f}"
    )


@pytest.mark.parametrize("category", CATEGORIES)
def test_bootstrap_recall_per_category(bootstrap_report, category):
    _, _, report = bootstrap_report
    recall = report.get(category, {}).get("recall", 0.0)
    assert recall >= BOOTSTRAP_RECALL, (
        f"bootstrap recall for {category} is {recall:.3f}"
    )


def test_bootstrap_assigns_all_four_categories(clean_corpus):
    """Sec. 16's Phase 3 milestone: 'classifier assigns all 4 categories'."""
    engine = BootstrapClassifier()
    predicted = {engine.classify(r.features).category for r in clean_corpus}
    assert predicted == set(CATEGORIES)


def test_multiple_counterparts_always_read_as_a_split(clean_corpus):
    """Multiplicity defines a split settlement. Requiring high coverage as
    well sent two-leg splits settling 85-91% to PARTIAL_PAYMENT and cost 42%
    of split recall - this pins the corrected rule."""
    engine = BootstrapClassifier()
    multi = [r for r in clean_corpus if r.features.counterpart_count >= 2]
    assert multi, "corpus produced no multi-counterpart cases"

    for record in multi:
        assert engine.classify(record.features).category == "SPLIT_SETTLEMENT"


# ── Random Forest: the trained engine ────────────────────────────────────


@pytest.mark.parametrize("category", CATEGORIES)
def test_forest_precision_and_recall_per_category(forest, category):
    _, metrics = forest
    stats = metrics["report"].get(category)
    assert stats is not None, f"{category} absent from the held-out set"
    assert stats["precision"] >= FOREST_PRECISION, (
        f"forest precision for {category} is {stats['precision']:.3f}"
    )
    assert stats["recall"] >= FOREST_RECALL, (
        f"forest recall for {category} is {stats['recall']:.3f}"
    )


def test_forest_macro_f1_clears_target(forest):
    _, metrics = forest
    assert metrics["macro_f1"] >= FOREST_MACRO_F1, (
        f"macro F1 {metrics['macro_f1']:.3f} below {FOREST_MACRO_F1}"
    )


def test_forest_materially_beats_the_rules_it_replaces(forest, bootstrap_report):
    """This is Sec. 10's justification for a learned model, measured.

    Both engines read the same features. The forest wins by weighing them
    jointly - a no-reference item posted five days late is ambiguous to any
    fixed threshold but separable to a tree ensemble. If the forest ever stops
    beating the rules, the rules should be shipped instead.
    """
    _, metrics = forest
    _, _, report = bootstrap_report
    bootstrap_f1 = report["macro avg"]["f1-score"]

    assert metrics["macro_f1"] > bootstrap_f1, (
        f"forest macro F1 {metrics['macro_f1']:.3f} does not beat the "
        f"bootstrap's {bootstrap_f1:.3f} - the learned model earns nothing"
    )


def test_forest_survives_class_imbalance(tmp_path):
    """Sec. 10 chose Random Forest as 'robust to the class imbalance typical of
    exception data'. With one category at 8% of the corpus, an unweighted model
    would learn to ignore it entirely - class_weight='balanced' is what stops
    that, so this test fails if it is ever removed."""
    corpus = build_corpus(
        per_category=200,
        seed=99,
        imbalance={
            "PARTIAL_PAYMENT": 1.0,
            "SPLIT_SETTLEMENT": 0.08,
            "MISSING_REFERENCE_CODE": 1.0,
            "TIMING_DIFFERENCE": 1.0,
        },
    )
    classifier = ExceptionClassifier(path=tmp_path / "rf.pkl")
    metrics = classifier.train([(r.features, r.category) for r in corpus])

    minority = metrics["report"].get("SPLIT_SETTLEMENT", {})
    assert minority.get("recall", 0.0) >= 0.70, (
        f"minority-class recall collapsed to {minority.get('recall', 0):.3f}"
    )


# ── Model lifecycle ──────────────────────────────────────────────────────


def test_untrained_classifier_falls_back_to_bootstrap(tmp_path, corpus):
    """A fresh deployment has no model. It must still triage, and must say
    which engine answered."""
    classifier = ExceptionClassifier(path=tmp_path / "absent.pkl")
    assert not classifier.is_trained

    result = classifier.classify(corpus[0].features)
    assert result.engine == "bootstrap"
    assert result.category in CATEGORIES


def test_trained_classifier_reports_the_forest(forest, corpus):
    classifier, _ = forest
    result = classifier.classify(corpus[0].features)
    assert result.engine == "random_forest"
    assert 0.0 <= result.confidence <= 1.0


def test_persisted_model_round_trips(forest, corpus):
    classifier, _ = forest
    reloaded = ExceptionClassifier(path=classifier.path)

    assert reloaded.is_trained
    original = classifier.classify(corpus[0].features)
    assert reloaded.classify(corpus[0].features).category == original.category


def test_model_trained_on_different_features_is_refused(forest, monkeypatch):
    """Predicting with mismatched columns yields confident nonsense. Refusing
    to load is the only safe response."""
    classifier, _ = forest
    import services.exception_handler.app.classifier as classifier_module

    monkeypatch.setattr(classifier_module, "FEATURE_NAMES", ("a", "b", "c"))
    reloaded = ExceptionClassifier(path=classifier.path)
    assert not reloaded.is_trained


def test_training_refuses_insufficient_data():
    classifier = ExceptionClassifier(path="/nonexistent/rf.pkl")
    with pytest.raises(ValueError, match="at least 8"):
        classifier.train([])


def test_training_refuses_a_single_category(corpus):
    classifier = ExceptionClassifier(path="/nonexistent/rf.pkl")
    single = [(r.features, "PARTIAL_PAYMENT") for r in corpus[:20]]
    with pytest.raises(ValueError, match="at least 2 distinct"):
        classifier.train(single)


def test_training_records_human_label_provenance(forest):
    """A model trained only on bootstrap suggestions has learned the rules, not
    the humans. That has to be visible, not buried in an accuracy score."""
    _, metrics = forest
    assert "human_labelled" in metrics
    assert metrics["human_labelled"] == 0


# ── Reporting ────────────────────────────────────────────────────────────


def test_report_measured_metrics(bootstrap_report, forest, capsys):
    actual, predicted, report = bootstrap_report
    _, metrics = forest

    with capsys.disabled():
        print(f"\n  corpus: {PER_CATEGORY} per category, {len(actual)} total, "
              f"{HARD_FRACTION:.0%} ambiguous")
        print(f"\n  {'category':<24} {'bootstrap P/R':>16} {'forest P/R':>16}")
        for category in CATEGORIES:
            b = report.get(category, {})
            f = metrics["report"].get(category, {})
            print(
                f"  {category:<24} "
                f"{b.get('precision', 0):>7.2%}/{b.get('recall', 0):<8.2%}"
                f"{f.get('precision', 0):>7.2%}/{f.get('recall', 0):<8.2%}"
            )
        print(f"\n  bootstrap macro F1 : {report['macro avg']['f1-score']:.3f}")
        print(f"  forest    macro F1 : {metrics['macro_f1']:.3f}"
              f"   (accuracy {metrics['accuracy']:.3f})")

        print("\n  bootstrap confusion (rows=actual, cols=predicted):")
        matrix = confusion_matrix(actual, predicted, labels=CATEGORIES)
        print(f"    {'':<24}" + "".join(f"{c[:11]:>13}" for c in CATEGORIES))
        for label, row in zip(CATEGORIES, matrix):
            print(f"    {label:<24}" + "".join(f"{v:>13}" for v in row))

    assert report["macro avg"]["f1-score"] > 0
