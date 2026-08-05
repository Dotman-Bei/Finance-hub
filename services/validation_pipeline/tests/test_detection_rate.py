"""RELEASE GATE - Objective 2 (build.md Sec. 8, Sec. 14).

    "Feed a labeled corpus of good + deliberately malformed records; assert the
    pipeline quarantines >=98% of the malformed set with zero good records
    lost. Treat this as a release gate."

Two numbers decide the gate:

    detection rate      = malformed quarantined / malformed total    >= 0.98
    false positive rate = good quarantined      / good total         == 0.00

The false-positive requirement is the harsher of the two. A pipeline that
quarantines everything scores 100% detection and is worthless; "zero good
records lost" is what stops that.

These run the real pipeline - the real Pydantic models, the real Great
Expectations suite from expectations/, the real checksum verifier. No stubs.
The cache is left disabled so every record is genuinely recomputed.
"""

from __future__ import annotations

import datetime as dt

import pytest

from services.validation_pipeline.app.pipeline import ValidationPipeline
from services.validation_pipeline.tests.corpus import build_corpus, defect_types

DETECTION_TARGET = 0.98
GOOD_COUNT = 400
MALFORMED_COUNT = 400


@pytest.fixture(scope="module")
def pipeline() -> ValidationPipeline:
    # cache=None: no Redis, so nothing is served from a previous verdict.
    return ValidationPipeline(cache=None)


@pytest.fixture(scope="module")
def graded(pipeline):
    """Run the corpus once; every test in this module grades the same run."""
    corpus = build_corpus(good_count=GOOD_COUNT, malformed_count=MALFORMED_COUNT)
    result = pipeline.validate_batch([r.payload for r in corpus], as_of=dt.date.today())

    assert len(result.decisions) == len(corpus), "pipeline dropped records"

    rows = []
    for record, decision in zip(corpus, result.decisions):
        rows.append(
            {
                "is_malformed": record.is_malformed,
                "defect": record.defect,
                "quarantined": decision.quarantined,
                "stage": decision.stage.value if decision.stage else None,
                "violations": decision.violations,
            }
        )
    return rows


# ── The gate ─────────────────────────────────────────────────────────────


def test_detection_rate_meets_objective_2(graded):
    malformed = [r for r in graded if r["is_malformed"]]
    caught = [r for r in malformed if r["quarantined"]]
    rate = len(caught) / len(malformed)

    missed: dict[str, int] = {}
    for row in malformed:
        if not row["quarantined"]:
            missed[row["defect"]] = missed.get(row["defect"], 0) + 1

    assert rate >= DETECTION_TARGET, (
        f"detection rate {rate:.4f} is below the {DETECTION_TARGET:.0%} gate. "
        f"Undetected defects: {dict(sorted(missed.items(), key=lambda kv: -kv[1]))}"
    )


def test_zero_good_records_are_lost(graded):
    good = [r for r in graded if not r["is_malformed"]]
    lost = [r for r in good if r["quarantined"]]

    assert not lost, (
        f"{len(lost)} of {len(good)} valid records were quarantined. "
        f"First few reasons: {[r['violations'] for r in lost[:3]]}"
    )


# ── Per-defect coverage, so a regression names itself ────────────────────


@pytest.mark.parametrize("defect", defect_types())
def test_every_defect_type_is_detected(graded, defect):
    """No single defect class may go entirely undetected, even if the overall
    rate still clears 98% by volume."""
    rows = [r for r in graded if r["defect"] == defect]
    assert rows, f"corpus produced no records for defect {defect}"

    caught = sum(1 for r in rows if r["quarantined"])
    assert caught == len(rows), (
        f"{defect}: only {caught}/{len(rows)} detected. "
        f"Example verdict: {rows[0]['violations'] or 'PASSED'}"
    )


def test_each_defect_is_caught_by_its_intended_stage(graded):
    """The defect label names the stage that should catch it. A schema defect
    slipping through to the checksum stage means stage 1 has a hole, even
    though the overall rate would not show it."""
    mismatches = []
    for row in graded:
        if not row["is_malformed"] or not row["quarantined"]:
            continue
        expected_stage = row["defect"].split("/")[0]
        actual = row["stage"]
        if expected_stage == "rule":
            expected_stage = "business_rule"
        if actual != expected_stage:
            mismatches.append((row["defect"], expected_stage, actual))

    assert not mismatches, f"caught by the wrong stage: {sorted(set(mismatches))}"


# ── Reporting ────────────────────────────────────────────────────────────


def test_report_measured_rates(graded, capsys):
    """Not an assertion of quality - it prints the measured numbers so the
    gate's actual margin is visible in CI output, not just pass/fail."""
    malformed = [r for r in graded if r["is_malformed"]]
    good = [r for r in graded if not r["is_malformed"]]

    detection = sum(r["quarantined"] for r in malformed) / len(malformed)
    false_positive = sum(r["quarantined"] for r in good) / len(good)

    by_stage: dict[str, int] = {}
    for row in malformed:
        if row["quarantined"]:
            by_stage[row["stage"]] = by_stage.get(row["stage"], 0) + 1

    with capsys.disabled():
        print(f"\n  corpus              : {len(good)} good / {len(malformed)} malformed")
        print(f"  detection rate      : {detection:.2%}  (gate >= {DETECTION_TARGET:.0%})")
        print(f"  false positive rate : {false_positive:.2%}  (gate == 0%)")
        print(f"  caught by stage     : {dict(sorted(by_stage.items()))}")

    assert detection >= DETECTION_TARGET
    assert false_positive == 0.0
