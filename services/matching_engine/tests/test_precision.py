"""RELEASE GATE - Objective 1 match precision (build.md Sec. 14).

    "Rule + ML matched pairs vs. labeled ground truth; track false-positive
     rate as threshold varies."

Precision is the number that matters. Sec. 9 is explicit that the configurable
threshold exists "to suppress the false positives the literature warns about",
so a pair the engine confirms wrongly is worse than one it declines to confirm:
a wrong match silently corrupts the ledger, while a missed match lands in the
exception queue where a human sees it.

The gate is therefore asymmetric - high precision required, recall reported.
The threshold sweep is the evidence for Sec. 9's claim that the knob works.
"""

from __future__ import annotations

import pytest

from services.matching_engine.app.ml_model import FuzzyMatcher
from services.matching_engine.app.pipeline import MatchingPipeline
from services.matching_engine.tests.ground_truth import (
    ARCHETYPES,
    build_ground_truth,
    historical_descriptions,
)

#: A confirmed pair that is wrong corrupts the ledger, so this is strict.
PRECISION_TARGET = 0.99

#: Every true pair must be either auto-matched or surfaced with its correct
#: counterpart nominated in the exception queue. Raw recall is the wrong metric
#: here: Sec. 10 says a partial payment or a missing reference *should* go to a
#: human rather than be confirmed automatically, so auto-matching them would be
#: the defect, not the goal. What must never happen is a true pair vanishing
#: with no counterpart proposed at all.
COVERAGE_TARGET = 0.95

PAIR_COUNT = 200
DECOY_COUNT = 60


@pytest.fixture(scope="module")
def truth():
    return build_ground_truth(pair_count=PAIR_COUNT, decoy_count=DECOY_COUNT)


@pytest.fixture(scope="module")
def matcher() -> FuzzyMatcher:
    """Fitted on historical descriptions, as Sec. 9's offline fit prescribes."""
    return FuzzyMatcher().fit(historical_descriptions())


def grade(result, truth) -> dict:
    correct = sum(1 for p in result.matched if truth.is_correct(p.internal_id, p.external_id))
    wrong = len(result.matched) - correct

    return {
        "confirmed": len(result.matched),
        "correct": correct,
        "wrong": wrong,
        "precision": correct / len(result.matched) if result.matched else 1.0,
        "recall": correct / truth.true_pair_count if truth.true_pair_count else 0.0,
        "false_positive_rate": wrong / len(result.matched) if result.matched else 0.0,
        "rule": result.rule_matched,
        "ml": result.ml_matched,
    }


@pytest.fixture(scope="module")
def graded(truth, matcher):
    pipeline = MatchingPipeline(matcher=matcher)
    result = pipeline.reconcile(truth.transactions)
    return result, grade(result, truth)


# ── The gate ─────────────────────────────────────────────────────────────


def test_precision_meets_target(graded):
    _, metrics = graded
    assert metrics["precision"] >= PRECISION_TARGET, (
        f"precision {metrics['precision']:.4f} below {PRECISION_TARGET}. "
        f"{metrics['wrong']} of {metrics['confirmed']} confirmed pairs are wrong."
    )


def test_rule_layer_never_produces_a_false_positive(truth, matcher):
    """Layer 1 matches on an exact key triple. If it can be wrong, the key is
    wrong - and those pairs bypass the threshold entirely, so nothing
    downstream would catch it."""
    pipeline = MatchingPipeline(matcher=matcher)
    result = pipeline.reconcile(truth.transactions)

    rule_pairs = [p for p in result.matched if p.match_type == "RULE"]
    wrong = [p for p in rule_pairs if not truth.is_correct(p.internal_id, p.external_id)]

    assert not wrong, f"{len(wrong)} of {len(rule_pairs)} exact-key matches are wrong"


def _coverage(result, truth) -> tuple[set, set, set]:
    """Split true pairs into (auto-matched, nominated, lost).

    "Nominated" means the pair was scored below the threshold but the engine
    still recorded the correct counterpart on the unmatched item, which is
    what the exception handler consumes to build its suggestion (Sec. 10).
    """
    matched = {(p.internal_id, p.external_id) for p in result.matched}
    matched |= {(b, a) for a, b in matched}

    nominated = set()
    for item in result.unmatched:
        if item.best_counterpart_id is not None:
            nominated.add((item.transaction_id, item.best_counterpart_id))
            nominated.add((item.best_counterpart_id, item.transaction_id))

    auto, nom, lost = set(), set(), set()
    for pair in truth.true_pairs:
        if pair in matched:
            auto.add(pair)
        elif pair in nominated:
            nom.add(pair)
        else:
            lost.add(pair)
    return auto, nom, lost


def test_no_true_pair_is_lost(graded, truth):
    """The real recall requirement: a genuine pair is either confirmed or its
    counterpart is nominated for review. Silently losing one means a human
    never gets the chance to reconcile it.

    This also stops precision being faked by matching almost nothing - an
    engine that confirms one pair and drops the rest fails here.
    """
    result, _ = graded
    auto, nominated, lost = _coverage(result, truth)

    coverage = (len(auto) + len(nominated)) / truth.true_pair_count
    assert coverage >= COVERAGE_TARGET, (
        f"coverage {coverage:.2%} below {COVERAGE_TARGET:.0%}: "
        f"{len(auto)} auto-matched, {len(nominated)} nominated, "
        f"{len(lost)} lost entirely."
    )


def test_no_transaction_is_matched_twice(graded):
    """One-to-one is a hard invariant: a transaction reconciles against exactly
    one counterpart, or matchedrecords double-counts the ledger."""
    result, _ = graded
    internal_ids = [p.internal_id for p in result.matched]
    external_ids = [p.external_id for p in result.matched]

    assert len(internal_ids) == len(set(internal_ids)), "an internal row matched twice"
    assert len(external_ids) == len(set(external_ids)), "an external row matched twice"
    assert not (set(internal_ids) & set(external_ids)), "a row matched as both sides"


def test_every_input_is_accounted_for(graded):
    """Matched pairs plus unmatched items must cover the whole batch. A record
    that is neither reconciled nor queued has silently vanished."""
    result, _ = graded
    accounted = {p.internal_id for p in result.matched} | {p.external_id for p in result.matched}
    accounted |= {u.transaction_id for u in result.unmatched}

    assert len(accounted) == result.total_input, (
        f"{result.total_input - len(accounted)} transactions vanished"
    )


# ── Threshold sweep: Sec. 9's core claim ─────────────────────────────────


def test_raising_the_threshold_reduces_false_positives(truth, matcher, capsys):
    """Sec. 9: 'Keeping the threshold configurable is how you suppress the
    false positives the literature warns about.' This is that claim, measured."""
    sweep = []
    for threshold in (0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.95):
        pipeline = MatchingPipeline(matcher=matcher, threshold=threshold)
        metrics = grade(pipeline.reconcile(truth.transactions), truth)
        sweep.append((threshold, metrics))

    with capsys.disabled():
        print(f"\n  ground truth: {truth.true_pair_count} true pairs "
              f"+ {DECOY_COUNT} unpaired decoys")
        print("  thresh  confirmed  correct  wrong  precision  recall   FP rate")
        for threshold, m in sweep:
            print(
                f"   {threshold:.2f}   {m['confirmed']:>8}  {m['correct']:>7}  "
                f"{m['wrong']:>5}   {m['precision']:>8.2%}  {m['recall']:>6.2%}  "
                f"{m['false_positive_rate']:>7.2%}"
            )

    # False positives must be monotonically non-increasing as the bar rises.
    # If they are not, the score is not ordering pairs by correctness and the
    # knob does not do what Sec. 9 says it does.
    wrong_counts = [m["wrong"] for _, m in sweep]
    assert wrong_counts == sorted(wrong_counts, reverse=True), (
        f"raising the threshold did not monotonically reduce false positives: "
        f"{[(t, m['wrong']) for t, m in sweep]}"
    )


def test_an_impossible_threshold_confirms_only_exact_matches(truth, matcher):
    """At 1.0 only the rule layer can qualify - ML scores are < 1.0 by
    construction. This proves exact matches are never gated away and that
    nothing probabilistic sneaks past a maximal bar."""
    pipeline = MatchingPipeline(matcher=matcher, threshold=1.0)
    result = pipeline.reconcile(truth.transactions)

    assert result.ml_matched == 0
    assert result.rule_matched > 0
    for pair in result.matched:
        assert truth.is_correct(pair.internal_id, pair.external_id)


# ── Per-archetype behaviour ──────────────────────────────────────────────


def test_exact_pairs_are_caught_by_the_rule_layer(truth, matcher):
    """The cheap deterministic layer must be doing the bulk of the work; if
    exact pairs were falling through to ML, layer ordering has broken."""
    pipeline = MatchingPipeline(matcher=matcher)
    result = pipeline.reconcile(truth.transactions)

    exact_pairs = {
        pair for pair, kind in truth.pair_archetype.items() if kind == "exact"
    }
    rule_matched = {
        (p.internal_id, p.external_id) for p in result.matched if p.match_type == "RULE"
    }

    caught = len(exact_pairs & rule_matched)
    assert caught / len(exact_pairs) >= 0.95, (
        f"only {caught}/{len(exact_pairs)} exact pairs went through Layer 1"
    )


def test_report_disposition_by_archetype(graded, truth, capsys):
    """Diagnostic: where each archetype ends up.

    The expected shape is not "everything auto-matched". Exact and timing
    pairs are certain enough to confirm; partial payments and missing
    references belong in the queue with a candidate attached, because that is
    what Sec. 10's resolution pathways act on.
    """
    result, metrics = graded
    auto, nominated, lost = _coverage(result, truth)

    rows: dict[str, list[int]] = {a: [0, 0, 0] for a in ARCHETYPES}
    for pair, archetype in truth.pair_archetype.items():
        if pair in auto:
            slot = 0
        elif pair in nominated:
            slot = 1
        else:
            slot = 2
        rows[archetype][slot] += 1

    with capsys.disabled():
        print(f"\n  precision {metrics['precision']:.2%}  "
              f"({metrics['correct']}/{metrics['confirmed']} confirmed correct, "
              f"{metrics['wrong']} wrong)")
        print(f"  coverage  {(len(auto) + len(nominated)) / truth.true_pair_count:.2%}  "
              f"({len(auto)} auto-matched, {len(nominated)} nominated, {len(lost)} lost)")
        print(f"\n  {'archetype':<20} {'auto':>6} {'nominated':>10} {'lost':>6}")
        for archetype, (a, n, l) in rows.items():
            print(f"  {archetype:<20} {a:>6} {n:>10} {l:>6}")

    assert rows["exact"][0] == sum(rows["exact"]), (
        "every exact pair must be auto-matched - they are deterministic"
    )
