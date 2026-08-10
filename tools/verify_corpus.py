"""Grade a generated corpus against its own answer key, through the real code.

`seed.py` produces feeds and an answer key; this reads both back and runs them
through the real validation pipeline, the real matching engine and the real
classifier, then reports what actually happened against what should have.

Why this exists separately from the test gates
----------------------------------------------
The gates in `services/*/tests/` measure each subsystem against a corpus built
for it, and they run without infrastructure by design. They cannot answer
"what does the assembled system do to *this* corpus", which is the question
Chapter 4 asks and the one a demo answers visually.

It is also how the SPLIT_SETTLEMENT gap was found: three categories appeared
and the fourth never did, because nothing wrote the `candidate_ids` the
classifier needed. No single-subsystem test could have shown that.

No database is touched. The pipelines are I/O-free by construction, so this
grades the logic without needing Postgres, Redis or a running service.

    python tools/verify_corpus.py data/seed
    python tools/verify_corpus.py data/seed --accuracy

`--accuracy` grades Subsystem 3 against the answer key's
`expected_category_if_unmatched`, one row per obligation. Previously this was
a throwaway script run by hand (HANDOFF.md's Sec. 7 table); folding it in here
means it is graded through the same real code and the same run as everything
else, instead of a number nobody can reproduce.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
import warnings
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from services.exception_handler.app.classifier import ExceptionClassifier  # noqa: E402
from services.exception_handler.app.features import extract  # noqa: E402
from services.matching_engine.app.ml_model import FuzzyMatcher  # noqa: E402
from services.matching_engine.app.pipeline import MatchingPipeline  # noqa: E402
from services.validation_pipeline.app.ingestion import normalize  # noqa: E402
from services.validation_pipeline.app.pipeline import ValidationPipeline  # noqa: E402

CATEGORIES = (
    "PARTIAL_PAYMENT",
    "SPLIT_SETTLEMENT",
    "MISSING_REFERENCE_CODE",
    "TIMING_DIFFERENCE",
)


def _rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n" + "-" * 62)


def verify(
    directory: Path, threshold: float | None = None, grade_accuracy: bool = False
) -> dict[str, Any]:
    key = json.loads((directory / "answer_key.json").read_text(encoding="utf-8"))

    payloads = normalize((directory / "erp_ledger.csv").read_text(encoding="utf-8"))
    payloads += normalize(
        json.loads((directory / "bank_feed.json").read_text(encoding="utf-8"))
    )

    # -- Subsystem 2 ------------------------------------------
    result = ValidationPipeline().validate_batch(payloads)
    quarantined = {d.payload.get("external_id"): d for d in result.quarantined}

    malformed = {m["external_id"] for m in key["malformed"]}
    good = {p["internal"] for p in key["true_pairs"]}
    for pair in key["true_pairs"]:
        good.update(pair["external"])
    good.update(key["decoys"])

    caught = len(malformed & set(quarantined))
    lost = good & set(quarantined)
    detection = 100 * caught / len(malformed) if malformed else 0.0
    false_positive = 100 * len(lost) / len(good) if good else 0.0

    _rule("Subsystem 2 - validation")
    print(f"  submitted           {len(payloads)}")
    print(f"  passed / quarantined{len(result.passed):>6} / {len(result.quarantined)}")
    print(f"  caught by stage     {result.by_stage()}")
    print(f"  detection rate      {detection:.2f}%   (gate >= 98%)")
    print(f"  false positive rate {false_positive:.2f}%   (gate == 0%)")
    if lost:
        for external_id in sorted(lost)[:5]:
            decision = quarantined[external_id]
            print(f"    ! lost {external_id}: {decision.violations[:1]}")

    # -- Subsystem 1 ------------------------------------------
    rows = []
    for decision in result.passed:
        row = dict(decision.payload)
        row["id"] = uuid.uuid4()
        rows.append(row)

    pipeline = MatchingPipeline(matcher=FuzzyMatcher(), threshold=threshold)
    reconciliation = pipeline.reconcile(pd.DataFrame(rows))

    by_uuid = {r["id"]: r for r in rows}
    external_of = {r["id"]: r.get("external_id") for r in rows}

    # The key records which external_ids genuinely reconcile; the engine
    # reports UUIDs it was handed, so grading means translating back.
    truth: set[frozenset[str]] = set()
    for pair in key["true_pairs"]:
        for leg in pair["external"]:
            truth.add(frozenset((pair["internal"], leg)))

    correct = wrong = 0
    for scored in reconciliation.matched:
        claim = frozenset(
            (external_of.get(scored.internal_id), external_of.get(scored.external_id))
        )
        if claim in truth:
            correct += 1
        else:
            wrong += 1

    precision = 100 * correct / (correct + wrong) if (correct + wrong) else 0.0
    recall = 100 * correct / len(truth) if truth else 0.0

    _rule("Subsystem 1 - matching")
    summary = reconciliation.summary()
    print(f"  matched / unmatched {summary['matched']} / {summary['unmatched']}")
    print(f"  rule / ML           {summary['rule_matched']} / {summary['ml_matched']}")
    print(f"  match rate          {100 * summary['match_rate']:.2f}%")
    print(f"  threshold           {summary['threshold']}")
    print(f"  duration            {summary['duration_ms']:.0f} ms")
    print(f"  pair precision      {precision:.2f}%   ({correct} correct, {wrong} wrong)")
    print(f"  pair recall         {recall:.2f}%   (of {len(truth)} true pairs)")

    # -- Subsystem 3 ------------------------------------------
    classifier = ExceptionClassifier(path="/nonexistent/rf.pkl")  # bootstrap rules
    categories: Counter[str] = Counter()
    engines: Counter[str] = Counter()
    nominations: Counter[int] = Counter()

    # Graded on the internal (ERP) leg only, never an external one. A split
    # settlement's external legs each look exactly like a partial payment
    # viewed alone - that is what "split settlement" means from one receipt's
    # vantage point, not a classifier error. `pair["internal"]` is always the
    # obligation side, so keying expectations off it (and never off
    # `pair["external"]`) is what keeps that ambiguity out of the score.
    expected_category = {
        pair["internal"]: pair["expected_category_if_unmatched"]
        for pair in key["true_pairs"]
        if pair.get("expected_category_if_unmatched")
    }
    archetype_of_internal = {
        pair["internal"]: pair["archetype"] for pair in key["true_pairs"]
    }
    in_key: Counter[str] = Counter(
        pair["archetype"]
        for pair in key["true_pairs"]
        if pair.get("expected_category_if_unmatched")
    )

    graded: list[tuple[str, str, str, bool]] = []  # (archetype, expected, actual, correct)

    for item in reconciliation.unmatched:
        transaction = by_uuid.get(item.transaction_id)
        if transaction is None:
            continue
        counterparts = [by_uuid[c] for c in item.candidate_ids if c in by_uuid]
        nominations[len(counterparts)] += 1
        features = extract(
            transaction, counterparts, {"best_confidence": item.best_confidence}
        )
        classification = classifier.classify(features)
        categories[classification.category] += 1
        engines[classification.engine] += 1

        expected = expected_category.get(transaction.get("external_id"))
        if expected is not None:
            archetype = archetype_of_internal[transaction["external_id"]]
            graded.append(
                (archetype, expected, classification.category, classification.category == expected)
            )

    _rule("Subsystem 3 - triage")
    print(f"  counterparts nominated {dict(sorted(nominations.items()))}")
    for category in CATEGORIES:
        print(f"  {category:<24}{categories.get(category, 0)}")
    missing = [c for c in CATEGORIES if not categories.get(c)]
    print(f"  engine              {dict(engines)}")
    print(
        f"  unreachable         {', '.join(missing) if missing else 'none - all four occur'}"
    )

    accuracy: dict[str, Any] | None = None
    if grade_accuracy:
        reached = Counter(archetype for archetype, *_ in graded)
        right = Counter(archetype for archetype, _, _, ok in graded if ok)
        total_right = sum(right.values())
        total_graded = len(graded)
        overall = 100 * total_right / total_graded if total_graded else 0.0

        _rule("Subsystem 3 - classification accuracy (graded on the obligation leg)")
        print(f"  engine: {classifier.status()['engine']}")
        print(f"  {'archetype':<22}{'in key':>8}{'reached queue':>16}{'correct':>10}")
        for archetype in sorted(in_key):
            k = in_key[archetype]
            r = reached.get(archetype, 0)
            c = right.get(archetype, 0)
            pct = f"{100 * c / r:.0f}%" if r else "n/a"
            print(f"  {archetype:<22}{k:>8}{r:>16}{pct:>10}")
        print(f"\n  overall             {overall:.2f}%   ({total_right}/{total_graded} graded)")

        wrong = [(a, e, act) for a, e, act, ok in graded if not ok]
        if wrong:
            print("  misclassified:")
            for archetype, expected_cat, actual_cat in wrong[:10]:
                print(f"    {archetype}: expected {expected_cat}, got {actual_cat}")
            if len(wrong) > 10:
                print(f"    ... and {len(wrong) - 10} more")

        accuracy = {
            "engine": classifier.status()["engine"],
            "overall": overall,
            "graded": total_graded,
            "correct": total_right,
            "by_archetype": {
                a: {
                    "in_key": in_key[a],
                    "reached_queue": reached.get(a, 0),
                    "correct": right.get(a, 0),
                }
                for a in sorted(in_key)
            },
        }

    return {
        "detection_rate": detection,
        "false_positive_rate": false_positive,
        "match_rate": 100 * summary["match_rate"],
        "pair_precision": precision,
        "pair_recall": recall,
        "categories": dict(categories),
        "unreachable": missing,
        "classification_accuracy": accuracy,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", help="a directory written by tools/seed.py --sink files")
    parser.add_argument("--threshold", type=float, default=None,
                        help="override MATCH_CONFIDENCE_THRESHOLD for this run")
    parser.add_argument("--json", action="store_true", help="emit the summary as JSON")
    parser.add_argument("--verbose", action="store_true", help="show library logging")
    parser.add_argument(
        "--accuracy", action="store_true",
        help="grade Subsystem 3's classification against the answer key, "
             "one row per obligation (not per leg - see verify()'s docstring "
             "note on why a split leg is graded once, from the internal side)",
    )
    args = parser.parse_args(argv)

    if not args.verbose:
        # Great Expectations prints a progress bar per batch and joblib warns
        # about pickle shapes; neither is the subject of this report.
        logging.disable(logging.CRITICAL)
        warnings.filterwarnings("ignore")

    summary = verify(
        Path(args.directory), threshold=args.threshold, grade_accuracy=args.accuracy
    )

    if args.json:
        print()
        print(json.dumps(summary, indent=2))

    # A corpus in which a category cannot occur is the failure this tool was
    # written to catch, so it must not exit clean.
    return 1 if summary["unreachable"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
