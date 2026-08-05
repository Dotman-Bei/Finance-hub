"""RELEASE GATE - reconciliation latency (build.md Sec. 14).

    "Reconcile N transactions under a fixed p95 budget; assert no regression."

Two things are measured, and the second matters more than the first:

* **p95 latency** for a fixed batch, against a budget.
* **Scaling behaviour.** A budget alone cannot catch the failure mode that
  actually kills reconciliation engines - accidentally quadratic candidate
  generation. Doubling the batch must not roughly quadruple the time, or the
  engine works in test and dies on a month-end run.

Budgets are wall-clock on ordinary CI hardware, so they are set with headroom.
The scaling assertion is the tighter constraint and is what a regression will
trip first.
"""

from __future__ import annotations

import statistics
import time

import pytest

from services.matching_engine.app.ml_model import FuzzyMatcher
from services.matching_engine.app.pipeline import MatchingPipeline
from services.matching_engine.tests.ground_truth import (
    build_ground_truth,
    historical_descriptions,
)

#: Transactions per batch for the headline measurement (2 rows per pair).
BATCH_PAIRS = 250
BATCH_SIZE = BATCH_PAIRS * 2 + 100

#: p95 budget for BATCH_SIZE transactions.
P95_BUDGET_MS = 4000.0

#: Per-transaction budget, the number that actually has to hold at volume.
PER_TRANSACTION_BUDGET_MS = 8.0

REPEATS = 5


@pytest.fixture(scope="module")
def matcher() -> FuzzyMatcher:
    return FuzzyMatcher().fit(historical_descriptions())


@pytest.fixture(scope="module")
def timings(matcher) -> list[float]:
    """Reconcile the same batch REPEATS times and collect wall-clock ms."""
    truth = build_ground_truth(pair_count=BATCH_PAIRS, decoy_count=100)
    pipeline = MatchingPipeline(matcher=matcher)

    # One warm-up: the first call pays scikit-learn's lazy imports and would
    # otherwise dominate the distribution.
    pipeline.reconcile(truth.transactions)

    samples = []
    for _ in range(REPEATS):
        started = time.perf_counter()
        pipeline.reconcile(truth.transactions)
        samples.append((time.perf_counter() - started) * 1000)
    return samples


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[index]


def test_p95_is_within_budget(timings, capsys):
    p95 = _p95(timings)
    with capsys.disabled():
        print(
            f"\n  {BATCH_SIZE} transactions x{REPEATS}: "
            f"min {min(timings):.0f}ms  median {statistics.median(timings):.0f}ms  "
            f"p95 {p95:.0f}ms  (budget {P95_BUDGET_MS:.0f}ms)"
        )
    assert p95 <= P95_BUDGET_MS, (
        f"p95 {p95:.0f}ms exceeds the {P95_BUDGET_MS:.0f}ms budget for "
        f"{BATCH_SIZE} transactions"
    )


def test_per_transaction_cost_is_bounded(timings):
    per_transaction = _p95(timings) / BATCH_SIZE
    assert per_transaction <= PER_TRANSACTION_BUDGET_MS, (
        f"{per_transaction:.2f}ms per transaction exceeds "
        f"{PER_TRANSACTION_BUDGET_MS}ms"
    )


def test_scaling_is_not_quadratic(matcher, capsys):
    """Doubling the batch must not quadruple the time.

    This is the regression that matters. Candidate generation compares
    internal rows against external rows; if a blocking window degenerates the
    cost becomes O(n*m), which passes a small-batch budget and then fails on a
    real month-end volume.
    """
    pipeline = MatchingPipeline(matcher=matcher)
    measurements = []

    for pairs in (100, 200, 400):
        truth = build_ground_truth(pair_count=pairs, decoy_count=40, seed=11 + pairs)
        pipeline.reconcile(truth.transactions)  # warm

        started = time.perf_counter()
        pipeline.reconcile(truth.transactions)
        elapsed = (time.perf_counter() - started) * 1000
        measurements.append((len(truth.transactions), elapsed))

    with capsys.disabled():
        print("\n  scaling:")
        for size, elapsed in measurements:
            print(f"    {size:>5} txns  {elapsed:>8.0f}ms  "
                  f"({elapsed / size:.2f}ms/txn)")

    # Compare the largest against the smallest. Perfectly linear would give a
    # ratio equal to the size ratio (~4x); quadratic would give ~16x. The
    # ceiling sits between, with headroom for the fixed TF-IDF cost.
    small_size, small_ms = measurements[0]
    large_size, large_ms = measurements[-1]
    size_ratio = large_size / small_size
    time_ratio = large_ms / max(small_ms, 1e-6)

    assert time_ratio <= size_ratio**1.6, (
        f"time grew {time_ratio:.1f}x for a {size_ratio:.1f}x larger batch - "
        f"that is superlinear enough to suggest quadratic candidate generation"
    )


def test_empty_batch_returns_immediately(matcher):
    import pandas as pd

    pipeline = MatchingPipeline(matcher=matcher)
    started = time.perf_counter()
    result = pipeline.reconcile(pd.DataFrame())
    assert (time.perf_counter() - started) * 1000 < 50
    assert result.total_input == 0


def test_reported_duration_matches_measured(matcher):
    """`duration_ms` is what the API returns and what any dashboard would
    chart, so it must reflect real work rather than being decorative."""
    truth = build_ground_truth(pair_count=60, decoy_count=20, seed=5)
    pipeline = MatchingPipeline(matcher=matcher)
    pipeline.reconcile(truth.transactions)

    started = time.perf_counter()
    result = pipeline.reconcile(truth.transactions)
    measured = (time.perf_counter() - started) * 1000

    assert result.duration_ms > 0
    assert result.duration_ms <= measured + 5
