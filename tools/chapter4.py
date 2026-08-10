"""Run every release gate and the corpus grading, and emit one results table.

Chapter 4 reports what the system achieves against the four objectives. Until
now those numbers only existed in scrolling pytest output and in a separate
verify_corpus.py run, which meant assembling the table by hand each time and
having no single command anyone else could re-run to check it.

    python tools/chapter4.py                 # generate a corpus, grade, gate
    python tools/chapter4.py --count 5000    # the sample size to defend
    python tools/chapter4.py --corpus data/seed --skip-gates
    python tools/chapter4.py --json results.json

What it does NOT do is compute any metric itself. Every number below is
produced by the same code that produces it anywhere else - the gates are the
real pytest run, the corpus figures are `verify_corpus.verify()` - so this
tool cannot report a result the test suite would disagree with. A gate that
fails is reported as failed and makes the run exit non-zero; a gate that
skipped is reported as skipped rather than silently counted as passing.

Output is deliberately ASCII-only: it is read on a Windows console (cp1252)
as often as a Linux one, and box-drawing characters raise UnicodeEncodeError
there.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import tempfile
import time
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: Gate test files, mapped to the objective each one evidences. Keyed by the
#: path pytest reports so a renamed file fails loudly here rather than
#: quietly dropping an objective's evidence from the table.
GATES: dict[str, tuple[str, str]] = {
    "services/validation_pipeline/tests/test_detection_rate.py":
        ("Obj 2", "Validation detection rate >= 98%, false positives == 0%"),
    "services/validation_pipeline/tests/test_stages.py":
        ("Obj 2", "Four validation stages behave as specified"),
    "services/matching_engine/tests/test_precision.py":
        ("Obj 1", "Match precision >= 99%, candidate coverage >= 95%"),
    "services/matching_engine/tests/test_layers.py":
        ("Obj 1", "Rule layer then ML layer, in that order"),
    "services/matching_engine/tests/test_latency.py":
        ("Obj 1", "p95 <= 4000ms, <= 8ms/txn, sub-quadratic scaling"),
    "services/exception_handler/tests/test_classifier.py":
        ("Obj 3", "Per-category precision/recall; forest macro F1 >= 0.90"),
    "services/exception_handler/tests/test_features_and_resolution.py":
        ("Obj 3", "Feature extraction and suggested resolutions"),
    "services/exception_handler/tests/test_feedback.py":
        ("Obj 3", "Human feedback loop; retrain does not regress"),
    "services/reporting_api/tests/test_auth_and_reports.py":
        ("Obj 4", "Role permissions and report generation"),
    "services/reporting_api/tests/test_auth_http.py":
        ("Obj 4", "Auth enforced over HTTP"),
    "services/reporting_api/tests/test_audit.py":
        ("Obj 4", "Audit trail queries"),
    "services/reporting_api/tests/test_ws.py":
        ("Obj 4", "Live exception feed"),
    "tests/test_end_to_end.py":
        ("All", "Ingest -> validate -> match -> triage, in one pass"),
    "tests/test_integration_db.py":
        ("All", "Real PostgreSQL: schema, audit trigger, KPIs"),
    "shared/tests/test_orm_matches_schema.py":
        ("All", "ORM has not drifted from db/schema.sql"),
    "shared/tests/test_models.py":
        ("All", "Wire models and enums"),
}


def _rule(title: str) -> None:
    print(f"\n{title}\n" + "=" * 74)


def _case_path(case: ET.Element) -> str:
    """Repo-relative test file for one <testcase>.

    pytest's JUnit writer emits no `file` attribute - only a dotted
    `classname` ("services.validation_pipeline.tests.test_stages"), which has
    to be turned back into a path. A test defined inside a class appends the
    class name, and a test skipped at collection time carries an empty
    classname with the module in `name` instead, so both are handled by
    walking segments off the end until something on disk matches.
    """
    dotted = case.get("classname") or case.get("name") or ""
    parts = dotted.split(".")
    while parts:
        candidate = "/".join(parts) + ".py"
        if (REPO_ROOT / candidate).exists():
            return candidate
        parts.pop()
    return dotted


def run_gates(python: str) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Run the suite once and read per-file results out of its JUnit XML.

    Parsed from XML rather than scraped from stdout because pytest's console
    output is formatted for humans and changes between versions; the XML
    schema does not, and it distinguishes skipped from passed, which a dot in
    the progress line does not.
    """
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "results.xml"
        started = time.perf_counter()
        proc = subprocess.run(
            [python, "-m", "pytest", "-q", "--tb=no", f"--junit-xml={report}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        elapsed = time.perf_counter() - started

        if not report.exists():
            print("  pytest produced no XML report; its output follows:\n")
            print(proc.stdout[-4000:] or proc.stderr[-4000:])
            raise SystemExit(2)

        root = ET.parse(report).getroot()

    per_file: dict[str, dict[str, Any]] = {}
    for case in root.iter("testcase"):
        path = _case_path(case)
        entry = per_file.setdefault(
            path, {"passed": 0, "failed": 0, "skipped": 0, "failures": []}
        )
        if case.find("failure") is not None or case.find("error") is not None:
            entry["failed"] += 1
            node = case.find("failure")
            if node is None:
                node = case.find("error")
            entry["failures"].append(
                f"{case.get('name')}: {(node.get('message') or '').splitlines()[0][:140]}"
            )
        elif case.find("skipped") is not None:
            entry["skipped"] += 1
        else:
            entry["passed"] += 1

    suite = root if root.tag == "testsuite" else root.find("testsuite")
    totals = {
        "tests": int(suite.get("tests", 0)),
        "failures": int(suite.get("failures", 0)),
        "errors": int(suite.get("errors", 0)),
        "skipped": int(suite.get("skipped", 0)),
        "seconds": round(elapsed, 1),
    }
    return per_file, totals


def print_gate_table(per_file: dict[str, dict[str, Any]]) -> list[str]:
    """One row per gate, grouped by objective. Returns the failing gates."""
    _rule("Release gates (build.md Sec. 14)")
    print(f"  {'Obj':<6}{'Result':<10}{'Tests':<8}{'Gate'}")
    print("  " + "-" * 70)

    failing: list[str] = []
    unknown = sorted(set(per_file) - set(GATES) - {""})

    for path, (objective, description) in GATES.items():
        entry = per_file.get(path)
        if entry is None:
            verdict, count = "MISSING", "-"
            failing.append(f"{path} (not collected)")
        else:
            count = str(entry["passed"] + entry["failed"] + entry["skipped"])
            if entry["failed"]:
                verdict = "FAIL"
                failing.append(path)
            elif entry["passed"] == 0 and entry["skipped"]:
                verdict = "SKIPPED"
            else:
                verdict = "pass"
        print(f"  {objective:<6}{verdict:<10}{count:<8}{description}")

    for path in unknown:
        entry = per_file[path]
        total = entry["passed"] + entry["failed"] + entry["skipped"]
        verdict = "FAIL" if entry["failed"] else "pass"
        if entry["failed"]:
            failing.append(path)
        print(f"  {'-':<6}{verdict:<10}{total:<8}{path} (not mapped to an objective)")

    for path in sorted(set(failing)):
        for line in per_file.get(path, {}).get("failures", []):
            print(f"    ! {line}")

    return failing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=2000,
                        help="obligations to generate when no --corpus is given")
    parser.add_argument("--corpus", default=None,
                        help="grade an existing corpus directory instead of generating one")
    parser.add_argument("--threshold", type=float, default=None,
                        help="override MATCH_CONFIDENCE_THRESHOLD for the corpus run")
    parser.add_argument("--skip-gates", action="store_true",
                        help="grade the corpus only; do not run the test suite")
    parser.add_argument("--python", default=sys.executable,
                        help="interpreter to run pytest with (default: this one)")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="also write the full results to this path as JSON")
    args = parser.parse_args(argv)

    logging.disable(logging.CRITICAL)
    warnings.filterwarnings("ignore")

    from tools.verify_corpus import verify  # noqa: E402

    results: dict[str, Any] = {}
    temporary: tempfile.TemporaryDirectory | None = None

    if args.corpus:
        corpus_dir = Path(args.corpus)
        if not (corpus_dir / "answer_key.json").exists():
            print(f"No answer_key.json in {corpus_dir} - is it a seed.py --sink files output?")
            return 2
        print(f"Grading existing corpus: {corpus_dir}")
    else:
        temporary = tempfile.TemporaryDirectory()
        corpus_dir = Path(temporary.name) / "corpus"
        print(f"Generating a {args.count}-obligation corpus...")
        # Shelled out rather than imported: seed.py's writer is private and
        # takes the parsed CLI namespace, so calling it directly would mean
        # faking an argparse object and silently pinning this tool to that
        # function's internals. The CLI is the contract seed.py actually
        # supports, and it is the same path a person seeding by hand takes.
        seeded = subprocess.run(
            [args.python, str(REPO_ROOT / "tools" / "seed.py"),
             "--count", str(args.count), "--sink", "files", "--out", str(corpus_dir)],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        if seeded.returncode != 0:
            print(seeded.stdout[-2000:] or seeded.stderr[-2000:])
            return 2
        print("  " + (seeded.stdout.strip().splitlines() or ["(no output)"])[0])

    try:
        summary = verify(corpus_dir, threshold=args.threshold, grade_accuracy=True)
        results["corpus"] = {"path": str(corpus_dir), "count": args.count, **summary}

        failing: list[str] = []
        if not args.skip_gates:
            print("\nRunning the release gates (this takes a couple of minutes)...")
            per_file, totals = run_gates(args.python)
            failing = print_gate_table(per_file)
            results["gates"] = {
                "totals": totals,
                "failing": sorted(set(failing)),
                "per_file": per_file,
            }
            print(
                f"\n  {totals['tests']} tests, {totals['failures']} failed, "
                f"{totals['errors']} errored, {totals['skipped']} skipped, "
                f"{totals['seconds']}s"
            )
            if totals["skipped"]:
                print("  note: a skipped integration gate needs a reachable PostgreSQL "
                      "(set DATABASE_URL).")

        # ---- the table Chapter 4 actually reports -------------------------
        accuracy = summary.get("classification_accuracy") or {}
        _rule("Objectives")
        print(f"  {'Objective':<44}{'Measure':<22}{'Result'}")
        print("  " + "-" * 70)
        print(f"  {'1 - Hybrid matching engine':<44}{'pair precision':<22}"
              f"{summary['pair_precision']:.2f}%")
        print(f"  {'':<44}{'pair recall':<22}{summary['pair_recall']:.2f}%")
        print(f"  {'':<44}{'match rate':<22}{summary['match_rate']:.2f}%")
        print(f"  {'2 - Real-time validation':<44}{'detection rate':<22}"
              f"{summary['detection_rate']:.2f}%")
        print(f"  {'':<44}{'false positive rate':<22}"
              f"{summary['false_positive_rate']:.2f}%")
        if accuracy:
            print(f"  {'3 - Smart exception handling':<44}"
                  f"{'classification acc.':<22}{accuracy['overall']:.2f}%"
                  f"   ({accuracy['correct']}/{accuracy['graded']}, "
                  f"{accuracy['engine']})")
            for archetype, row in accuracy["by_archetype"].items():
                reached, correct = row["reached_queue"], row["correct"]
                share = f"{100 * correct / reached:.0f}%" if reached else "n/a"
                print(f"  {'':<44}{'  ' + archetype:<22}{share:<8}"
                      f"({correct}/{reached} of {row['in_key']} in key)")
        print(f"  {'4 - Reporting dashboard':<44}{'gates':<22}"
              f"{'see table above' if not args.skip_gates else 'not run'}")

        categories = summary.get("categories", {})
        print(f"\n  exception categories reached: "
              f"{len(categories)}/4 ({', '.join(sorted(categories)) or 'none'})")
        if summary["unreachable"]:
            print(f"  UNREACHABLE: {', '.join(summary['unreachable'])}")

        if args.json_path:
            Path(args.json_path).write_text(json.dumps(results, indent=2), encoding="utf-8")
            print(f"\n  wrote {args.json_path}")

        # An unreachable category or a failing gate both mean the table above
        # is not a clean result, and a harness that exits 0 anyway is one
        # nobody can put in CI.
        if summary["unreachable"]:
            print("\nFAILED: an exception category was unreachable.")
            return 1
        if failing:
            print(f"\nFAILED: {len(set(failing))} gate(s) did not pass.")
            return 1
        if args.skip_gates:
            print("\nAll four categories occur. Gates were skipped and are NOT "
                  "part of this result.")
        else:
            print("\nAll gates passed and all four categories occur.")
        return 0
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
