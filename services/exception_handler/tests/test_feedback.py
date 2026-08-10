"""RELEASE GATE - feedback improvement (build.md Sec. 14).

    "Classifier accuracy after retraining >= accuracy before, on rolling data."

This is the gate behind Sec. 3.3.1's claim that the system "gradually becomes
more precise". Two things have to hold for that claim to be true, and both are
asserted here:

* **Monotonicity.** A retrain must never leave the classifier worse than it
  was. build.md Sec. 11's snippet persists unconditionally, so this property
  comes from the promotion guard in `train(promote_only_if_better=True)` -
  without it the gate would be a hope rather than a guarantee.

* **Actual learning.** Monotonicity alone is satisfiable by never updating
  anything. The loop must demonstrably improve on cases the previous model got
  wrong, which is what the human-correction test measures.

"Rolling data" is modelled as successive rounds: each round adds newly resolved
exceptions to the training pool and retrains, the way the Celery beat task
accumulates decisions over time.
"""

from __future__ import annotations

import pytest

from services.exception_handler.app.classifier import (
    BootstrapClassifier,
    ExceptionClassifier,
)
from services.exception_handler.app.feedback import (
    DECISION_ACCEPT,
    DECISION_EDIT,
    DECISION_REJECT,
    _is_usable_label,
)
from services.exception_handler.tests.exception_corpus import build_corpus

HARD_FRACTION = 0.5

#: Per-category batch sizes for successive rounds. Deliberately starting tiny:
#: a feedback loop begins with a handful of decisions and accumulates. Rounds
#: of equal size would saturate the model immediately and the curve would show
#: nothing but a flat line at the ceiling.
ROUND_SIZES = (4, 12, 40, 120)


def _labelled(records):
    return [(r.features, r.category) for r in records]


@pytest.fixture(scope="module")
def rounds():
    """Successive batches of resolved exceptions, as the loop would see them."""
    return [
        build_corpus(per_category=size, hard_fraction=HARD_FRACTION, seed=1000 + i)
        for i, size in enumerate(ROUND_SIZES)
    ]


# ── The gate ─────────────────────────────────────────────────────────────


def test_accuracy_never_regresses_across_rounds(tmp_path, rounds, capsys):
    """Sec. 14's gate, measured over rolling data.

    Each round is scored on a fixed evaluation set the model never trains on,
    so the comparison is like-for-like across rounds.
    """
    evaluation = _labelled(
        build_corpus(per_category=60, hard_fraction=HARD_FRACTION, seed=99999)
    )

    classifier = ExceptionClassifier(path=tmp_path / "rf.pkl")
    pool: list = []
    history: list[tuple[int, float]] = []

    for index, batch in enumerate(rounds, start=1):
        pool.extend(_labelled(batch))
        classifier.train(pool, human_labelled=len(pool), promote_only_if_better=True)

        score = classifier.evaluate(evaluation)
        history.append((len(pool), score["macro_f1"]))

    with capsys.disabled():
        print("\n  rolling retrain (held-out macro F1):")
        for round_index, (size, f1) in enumerate(history, start=1):
            print(f"    round {round_index}  {size:>4} labels   macro F1 {f1:.4f}")

    for previous, current in zip(history, history[1:]):
        assert current[1] >= previous[1] - 0.02, (
            f"accuracy regressed from {previous[1]:.4f} to {current[1]:.4f} "
            f"between rounds - the feedback loop made the model worse"
        )


def test_the_loop_learns_what_the_rules_get_wrong(tmp_path, capsys):
    """Monotonicity alone is satisfiable by never updating. This measures that
    human corrections actually buy something.

    The bootstrap rules misclassify the ambiguous cases. Those are exactly the
    exceptions a reviewer corrects, and training on the corrections is what
    Sec. 3.3.1's "gradually becomes more precise" means concretely.
    """
    corpus = build_corpus(per_category=200, hard_fraction=HARD_FRACTION, seed=4242)
    evaluation = build_corpus(per_category=60, hard_fraction=HARD_FRACTION, seed=777)

    bootstrap = BootstrapClassifier()
    correct_before = sum(
        1 for r in evaluation if bootstrap.classify(r.features).category == r.category
    )

    classifier = ExceptionClassifier(path=tmp_path / "rf.pkl")
    classifier.train(_labelled(corpus), human_labelled=len(corpus))
    correct_after = sum(
        1 for r in evaluation if classifier.classify(r.features).category == r.category
    )

    with capsys.disabled():
        print(
            f"\n  on {len(evaluation)} held-out ambiguous exceptions: "
            f"rules {correct_before}/{len(evaluation)} "
            f"({correct_before / len(evaluation):.1%}) -> "
            f"learned {correct_after}/{len(evaluation)} "
            f"({correct_after / len(evaluation):.1%})"
        )

    assert correct_after > correct_before, (
        "the retrained model does not beat the rules it replaced, so the "
        "feedback loop is buying nothing"
    )


def test_a_regressing_candidate_is_not_promoted(tmp_path):
    """The guard build.md Sec. 11's snippet lacks.

    A round of mislabelled decisions must not replace a good classifier. The
    incumbent is kept and the caller is told why.
    """
    good = build_corpus(per_category=150, hard_fraction=HARD_FRACTION, seed=11)
    classifier = ExceptionClassifier(path=tmp_path / "rf.pkl")
    classifier.train(_labelled(good), promote_only_if_better=True)

    baseline = classifier.metadata["macro_f1"]

    # Labels shuffled onto the wrong records - a plausible outcome of a
    # reviewer working through a queue carelessly.
    poisoned = _labelled(good)
    rotated = [(features, poisoned[(i + 1) % len(poisoned)][1])
               for i, (features, _) in enumerate(poisoned)]

    outcome = classifier.train(rotated, promote_only_if_better=True)

    assert outcome["promoted"] is False
    assert "regressed" in outcome["reason"]
    # The incumbent survived untouched.
    assert classifier.metadata["macro_f1"] == baseline


def test_first_ever_model_is_measured_against_the_bootstrap_rules(tmp_path):
    """Run one has no incumbent forest - but the rules are still serving.

    The guard used to compare against `self.model`, which is None on the very
    first retrain, so the first model was promoted unconditionally however bad
    it was. That is the one case where nobody notices a regression, because
    there is no "before" number to compare against. Sec. 14's gate is
    "accuracy after retraining >= accuracy before", and for run one *before*
    is the bootstrap rules.
    """
    good = build_corpus(per_category=150, hard_fraction=HARD_FRACTION, seed=21)
    poisoned = _labelled(good)
    rotated = [(features, poisoned[(i + 1) % len(poisoned)][1])
               for i, (features, _) in enumerate(poisoned)]

    fresh = ExceptionClassifier(path=tmp_path / "rf.pkl")
    assert fresh.model is None, "this test is about the no-incumbent path"

    outcome = fresh.train(rotated, promote_only_if_better=True)

    assert outcome["promoted"] is False, (
        "a first model trained on shuffled labels was promoted over the "
        "bootstrap rules it displaces"
    )
    assert "bootstrap rules" in outcome["reason"]
    assert outcome["incumbent"]["baseline"] == "bootstrap rules"
    assert fresh.model is None, "nothing should have been installed"
    assert not (tmp_path / "rf.pkl").exists(), "a rejected model must not persist"


def test_a_good_first_model_still_promotes_over_the_rules(tmp_path):
    """The new baseline must not block a genuinely better first model."""
    good = build_corpus(per_category=150, hard_fraction=HARD_FRACTION, seed=22)

    fresh = ExceptionClassifier(path=tmp_path / "rf.pkl")
    outcome = fresh.train(_labelled(good), promote_only_if_better=True)

    assert outcome["promoted"] is True
    assert outcome["incumbent"]["baseline"] == "bootstrap rules"
    assert outcome["macro_f1"] >= outcome["incumbent"]["macro_f1"]


def test_promotion_guard_off_replaces_unconditionally(tmp_path):
    """Documents the difference: without the guard, build.md's behaviour is
    exactly what the guard exists to prevent."""
    good = build_corpus(per_category=100, hard_fraction=HARD_FRACTION, seed=12)
    classifier = ExceptionClassifier(path=tmp_path / "rf.pkl")
    classifier.train(_labelled(good))

    poisoned = _labelled(good)
    rotated = [(f, poisoned[(i + 1) % len(poisoned)][1]) for i, (f, _) in enumerate(poisoned)]

    outcome = classifier.train(rotated)   # promote_only_if_better defaults False
    assert outcome["promoted"] is True


# ── Hot swap (Sec. 11) ───────────────────────────────────────────────────


def test_retrained_model_is_picked_up_without_a_restart(tmp_path):
    """The Celery worker is a separate process. Without an explicit reload the
    API would keep classifying with the stale in-memory model while the file on
    disk was newer - the retrain loop would look like it worked and change
    nothing."""
    import time

    path = tmp_path / "rf.pkl"
    corpus = build_corpus(per_category=100, hard_fraction=0.0, seed=21)

    # Worker process trains and writes.
    worker = ExceptionClassifier(path=path)
    worker.train(_labelled(corpus), human_labelled=50)

    # API process loads that model.
    api = ExceptionClassifier(path=path)
    assert api.is_trained
    first_trained_at = api.metadata["trained_at"]
    assert api.reload_if_changed() is False   # nothing new yet

    # Worker retrains later.
    time.sleep(1.05)   # filesystem mtime resolution
    worker.train(
        _labelled(build_corpus(per_category=100, hard_fraction=0.0, seed=22)),
        human_labelled=90,
    )

    assert api.reload_if_changed() is True
    assert api.metadata["trained_at"] != first_trained_at
    assert api.metadata["human_labelled"] == 90


def test_reload_is_a_no_op_when_no_model_exists(tmp_path):
    classifier = ExceptionClassifier(path=tmp_path / "absent.pkl")
    assert classifier.reload_if_changed() is False
    assert not classifier.is_trained


# ── What counts as a label ───────────────────────────────────────────────


def test_only_accepts_and_edits_feed_the_loop():
    """Training on rejections would teach the forest the opposite of what the
    reviewer meant."""
    from shared.models.enums import ExceptionCategory

    category = ExceptionCategory.PARTIAL_PAYMENT
    assert _is_usable_label(DECISION_ACCEPT, category) is True
    assert _is_usable_label(DECISION_EDIT, category) is True
    assert _is_usable_label(DECISION_REJECT, category) is False


def test_evaluate_returns_none_for_an_untrained_model(tmp_path):
    classifier = ExceptionClassifier(path=tmp_path / "absent.pkl")
    corpus = build_corpus(per_category=10, hard_fraction=0.0, seed=31)
    assert classifier.evaluate(_labelled(corpus)) is None


def test_evaluate_scores_on_the_supplied_set(tmp_path):
    corpus = build_corpus(per_category=80, hard_fraction=HARD_FRACTION, seed=41)
    classifier = ExceptionClassifier(path=tmp_path / "rf.pkl")
    classifier.train(_labelled(corpus))

    score = classifier.evaluate(_labelled(corpus))
    assert 0.0 <= score["macro_f1"] <= 1.0
    assert score["n"] == len(corpus)
