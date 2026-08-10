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


def verify(directory: Path, threshold: float | None = None) -> dict[str, Any]:
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

    _rule("Subsystem 3 - triage")
    print(f"  counterparts nominated {dict(sorted(nominations.items()))}")
    for category in CATEGORIES:
        print(f"  {category:<24}{categories.get(category, 0)}")
    missing = [c for c in CATEGORIES if not categories.get(c)]
    print(f"  engine              {dict(engines)}")
    print(
        f"  unreachable         {', '.join(missing) if missing else 'none - all four occur'}"
    )

    return {
        "detection_rate": detection,
        "false_positive_rate": false_positive,
        "match_rate": 100 * summary["match_rate"],
        "pair_precision": precision,
        "pair_recall": recall,
        "categories": dict(categories),
        "unreachable": missing,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", help="a directory written by tools/seed.py --sink files")
    parser.add_argument("--threshold", type=float, default=None,
                        help="override MATCH_CONFIDENCE_THRESHOLD for this run")
    parser.add_argument("--json", action="store_true", help="emit the summary as JSON")
    parser.add_argument("--verbose", action="store_true", help="show library logging")
    args = parser.parse_args(argv)

    if not args.verbose:
        # Great Expectations prints a progress bar per batch and joblib warns
        # about pickle shapes; neither is the subject of this report.
        logging.disable(logging.CRITICAL)
        warnings.filterwarnings("ignore")

    summary = verify(Path(args.directory), threshold=args.threshold)

    if args.json:
        print()
        print(json.dumps(summary, indent=2))

    # A corpus in which a category cannot occur is the failure this tool was
    # written to catch, so it must not exit clean.
    return 1 if summary["unreachable"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
