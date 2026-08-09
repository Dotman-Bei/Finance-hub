"""Two-sided reconciliation corpus generator, with a retained answer key.

Not part of build.md's file list. It exists because build.md specifies the
system but never says what flows through it: every corpus in the repo lives
under a `tests/` directory and is consumed by a gate, so a freshly started
stack comes up healthy with an empty database and nothing to show.

Why generated rather than a public dataset
------------------------------------------
Reconciliation needs *two sides that partially disagree* - an internal ledger
and an external statement covering the same payments. Public financial
datasets are one-sided transaction logs labelled for fraud; you cannot
reconcile a list against itself, and `PARTIAL_PAYMENT` / `SPLIT_SETTLEMENT` /
`MISSING_REFERENCE_CODE` / `TIMING_DIFFERENCE` appear in none of them. Real
two-sided data is bank-confidential. build.md Sec. 14 sanctions synthetic
corpora for exactly this reason.

Generating also buys the thing real data cannot: a **known answer**. Precision
is only measurable against ground truth, and nobody has labelled which rows of
a real bank statement truly reconcile.

What this emits, and why it is not the test corpora
---------------------------------------------------
`matching_engine/tests/ground_truth.py` already generates labelled pairs, but
it emits records that are *already canonical* - it feeds the matcher directly
and deliberately bypasses ingestion. This tool emits **raw vendor-shaped**
payloads instead: ERP as CSV with vendor column names, the bank side as JSON
with different ones. That difference is the point. Data seeded here enters
through the real front door and exercises Pandas normalisation, the Pydantic
schema, the Great Expectations suite and checksum verification on the way in.

It also emits two things no test generator produces:

  * `split_settlement` - one obligation discharged by several receipts. The
    classifier has four categories and the test corpus covers it, but the
    matching corpus does not, so without it a demo can never show the fourth.
  * deliberately malformed records - without them the pipeline quarantines
    nothing, the dashboard's quarantine KPI reads zero, and Subsystem 2 looks
    untested in exactly the screenshot you want to show.

Where it writes
---------------
`files` or `kafka` - never Postgres. The architecture invariant is that
nothing reaches the database without passing validation first (build.md
Sec. 8: "It's the front door; nothing reaches the DB without it"). A seeder
that INSERTed directly would fabricate the very guarantee the system exists
to provide, and every downstream number would be measuring a fiction.

Usage
-----
    python tools/seed.py --count 2000 --sink files --out data/seed
    python tools/seed.py --count 2000 --sink kafka

The answer key is written alongside as `answer_key.json` and keyed on
`external_id` (ERP-xxxxxx / BNK-xxxxxx), never on UUID: primary keys are
assigned by Postgres at insert time, so the generator cannot know them and
must not pretend to.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import logging
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Running as a script puts tools/ on sys.path, not the repo root, so the
# `services.*` and `shared.*` imports below would fail. Resolve the root from
# this file rather than the cwd, so the tool works from any directory.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.validation_pipeline.app.checksum import canonical_json  # noqa: E402
from services.validation_pipeline.app.ingestion import normalize  # noqa: E402
from shared.models.enums import ExceptionCategory  # noqa: E402

logger = logging.getLogger("seed")

COUNTERPARTIES = [
    "Meridian Capital Ltd", "Northwind Logistics", "Arcadia Payments BV",
    "Solstice Retail Group", "Tessellate Software Inc", "Harborline Freight",
    "Lumen Energy Partners", "Vantage Clearing House", "Kestrel Media SA",
    "Orion Manufacturing", "Bluepeak Insurance", "Cadence Health Systems",
    "Fairmount Trading Co", "Silverbrook Holdings", "Ridgeway Chemicals",
]

NARRATIVES = ["settlement", "invoice remittance", "wire transfer", "ACH credit", "card batch"]
EXTERNAL_SOURCES = ["bank_api", "payment_gateway"]

#: Archetype -> the category the exception handler should reach *if* the pair
#: lands in the queue rather than being matched. Deliberately not a claim about
#: whether it will land there: that depends on MATCH_CONFIDENCE_THRESHOLD, and
#: baking an expectation in here would turn a tunable into a hardcoded verdict.
ARCHETYPE_CATEGORY = {
    "exact": None,                # rule layer should take this
    "noisy_description": None,    # ML layer should take this
    "timing_difference": ExceptionCategory.TIMING_DIFFERENCE.value,
    "missing_reference": ExceptionCategory.MISSING_REFERENCE_CODE.value,
    "partial_payment": ExceptionCategory.PARTIAL_PAYMENT.value,
    "split_settlement": ExceptionCategory.SPLIT_SETTLEMENT.value,
}

ARCHETYPES = tuple(ARCHETYPE_CATEGORY)

#: How much of the corpus each archetype accounts for.
#:
#: Deliberately not a flat 1/6 each. An even spread pushes two thirds of the
#: corpus into the exception queue and shows a ~25% match rate on the
#: dashboard - a number that reads as a broken reconciliation rather than a
#: working one, and that makes every Chapter 4 figure a measurement of the
#: corpus instead of the engine. Production reconciliation auto-matches the
#: large majority and leaves a tail of exceptions; this mix reproduces that
#: while still generating enough of each category to populate the panel.
ARCHETYPE_MIX = {
    "exact": 0.62,
    "noisy_description": 0.14,
    "timing_difference": 0.09,
    "missing_reference": 0.06,
    "partial_payment": 0.06,
    "split_settlement": 0.03,
}

#: Largest number of days an archetype pushes the external leg past the
#: internal one. The base date is drawn far enough back to absorb it: a
#: settlement lag applied to a transaction dated yesterday lands in the
#: future, and stage 2 rightly quarantines it as a rule violation. That would
#: silently destroy records this file labelled as good, so the answer key
#: would claim pairs the pipeline had already thrown away.
MAX_FORWARD_SHIFT = {
    "exact": 0,
    "noisy_description": 2,
    "timing_difference": 20,
    "missing_reference": 0,
    "partial_payment": 2,
    "split_settlement": 4,
}

#: Structural and rule defects only - the inconsistencies Objective 2 measures.
#: A semantically wrong but well-formed record (a duplicate, a plausible wrong
#: amount) is not ingestion's to catch and is deliberately absent.
DEFECTS = (
    "negative_amount",
    "zero_amount",
    "future_date",
    "bad_currency",
    "missing_amount",
    "missing_source",
    "corrupt_checksum",
)


@dataclass
class Corpus:
    """Raw feeds plus the answer key that makes them measurable."""

    erp_rows: list[dict[str, Any]] = field(default_factory=list)
    bank_rows: list[dict[str, Any]] = field(default_factory=list)
    pairs: list[dict[str, Any]] = field(default_factory=list)
    decoys: list[str] = field(default_factory=list)
    malformed: list[dict[str, str]] = field(default_factory=list)

    @property
    def record_count(self) -> int:
        return len(self.erp_rows) + len(self.bank_rows)


def _mangle(narrative: str, counterparty: str, rng: random.Random) -> str:
    """Bank-style narrative noise: truncation, casing, reference fragments."""
    short = counterparty.upper()[: rng.randint(8, len(counterparty))]
    return rng.choice(
        [
            f"{short}//REF{rng.randint(1000, 9999)} {narrative.upper()}",
            f"{narrative.upper()} {short} {rng.randint(100000, 999999)}",
            f"POS {short}  {narrative}",
            f"{short} - {narrative} - BATCH{rng.randint(10, 99)}",
        ]
    )


def _archetype_plan(pair_count: int, rng: random.Random) -> list[str]:
    """One archetype per pair, in ARCHETYPE_MIX proportions, covering all six.

    `max(1, ...)` guarantees every archetype appears even at small --count, so
    a quick 10-pair run still exercises split settlement. Rounding drift is
    absorbed by `exact`, the largest bucket, where a few records either way
    changes nothing.
    """
    counts = {
        name: max(1, round(pair_count * share)) for name, share in ARCHETYPE_MIX.items()
    }
    counts["exact"] = max(1, counts["exact"] + pair_count - sum(counts.values()))

    plan = [name for name, n in counts.items() for _ in range(n)]

    # Only reachable when the per-archetype floor overshoots a tiny --count.
    # Trim `exact` rather than truncating blindly, which could drop a category.
    while len(plan) > pair_count and "exact" in plan:
        plan.remove("exact")
    plan.extend(["exact"] * (pair_count - len(plan)))

    rng.shuffle(plan)
    return plan


def _erp_row(rng: random.Random, **over: Any) -> dict[str, Any]:
    """Internal ledger, in the ERP vendor's column vocabulary."""
    row = {
        "transaction_id": f"ERP-{rng.randint(100000, 999999)}",
        "source": "erp",
        "transaction_amount": None,
        "ccy": "USD",
        "value_date": None,
        "narrative": None,
        "ref": None,
    }
    row.update(over)
    return row


def _bank_row(rng: random.Random, **over: Any) -> dict[str, Any]:
    """External feed, in the bank's column vocabulary - deliberately different.

    Different aliases on each side is what makes normalisation load-bearing
    rather than decorative; a seeder that emitted canonical names on both
    sides would leave COLUMN_ALIASES untested by everything it feeds.
    """
    row = {
        "txn_id": f"BNK-{rng.randint(100000, 999999)}",
        "channel": rng.choice(EXTERNAL_SOURCES),
        "amt": None,
        "currency_code": "USD",
        "posted_date": None,
        "memo": None,
        "reference": None,
    }
    row.update(over)
    return row


def _attach_checksum(row: dict[str, Any]) -> dict[str, Any]:
    """Sign a bank row so stage 3 has something real to verify.

    The digest must be computed over the payload the pipeline actually checks,
    which is the *normalised* record - not this vendor-shaped one. Hashing the
    raw form would produce a mismatch on every record and quarantine the whole
    feed at stage 3, which reads exactly like a working checksum stage catching
    a corrupt source. Running the real `normalize` here is what stops this tool
    from certifying its own convention.
    """
    import hashlib

    # The algorithm field must be set *before* hashing. `canonical_json` strips
    # CHECKSUM_FIELDS ("checksum", "signature", ...) but deliberately keeps
    # `checksum_algorithm`, so it is part of the signed body. Attaching it
    # afterwards would change the payload between signing and verification and
    # fail every signed record at stage 3.
    row["checksum_algorithm"] = "sha256"

    normalized = normalize(dict(row))
    if not normalized:
        return row

    digest = hashlib.sha256(canonical_json(normalized[0]).encode("utf-8")).hexdigest()
    row["checksum"] = digest
    return row


def build_corpus(
    pair_count: int = 400,
    decoy_count: int = 80,
    malformed_count: int = 60,
    signed_fraction: float = 0.35,
    seed: int = 20260809,
) -> Corpus:
    """Generate both feeds with an even spread of archetypes."""
    rng = random.Random(seed)
    today = dt.date.today()
    corpus = Corpus()

    for archetype in _archetype_plan(pair_count, rng):
        counterparty = rng.choice(COUNTERPARTIES)
        narrative = rng.choice(NARRATIVES)
        amount = round(rng.uniform(120.0, 180000.0), 2)
        txn_date = today - dt.timedelta(
            days=rng.randint(MAX_FORWARD_SHIFT[archetype] + 1, 120)
        )
        reference = f"REF-{rng.randint(10000, 99999)}"
        description = f"{counterparty} - {narrative}"

        erp = _erp_row(
            rng,
            transaction_amount=amount,
            value_date=txn_date.isoformat(),
            narrative=description,
            ref=reference,
        )

        bank_defaults = {
            "amt": amount,
            "posted_date": txn_date.isoformat(),
            "memo": description,
            "reference": reference,
        }

        legs: list[dict[str, Any]] = []

        if archetype == "exact":
            legs.append(_bank_row(rng, **bank_defaults))

        elif archetype == "timing_difference":
            settled = txn_date + dt.timedelta(days=rng.randint(5, 20))
            legs.append(_bank_row(rng, **{**bank_defaults, "posted_date": settled.isoformat()}))

        elif archetype == "missing_reference":
            legs.append(
                _bank_row(
                    rng,
                    **{
                        **bank_defaults,
                        "reference": None,
                        "memo": _mangle(narrative, counterparty, rng),
                    },
                )
            )

        elif archetype == "partial_payment":
            legs.append(
                _bank_row(
                    rng,
                    **{
                        **bank_defaults,
                        "amt": round(amount * rng.uniform(0.60, 0.95), 2),
                        "reference": None,
                        "posted_date": (txn_date + dt.timedelta(days=rng.randint(0, 2))).isoformat(),
                    },
                )
            )

        elif archetype == "split_settlement":
            # Two to four receipts together discharging 92-100% of the
            # obligation. The remainder is left unallocated on purpose: a split
            # that always sums to exactly 100% is separable by arithmetic alone
            # and would flatter the classifier.
            leg_count = rng.randint(2, 4)
            covered = amount * rng.uniform(0.92, 1.0)
            weights = [rng.uniform(0.5, 1.5) for _ in range(leg_count)]
            total_weight = sum(weights)
            for n, weight in enumerate(weights):
                legs.append(
                    _bank_row(
                        rng,
                        **{
                            **bank_defaults,
                            "amt": round(covered * weight / total_weight, 2),
                            "reference": None,
                            "memo": f"{description} (part {n + 1}/{leg_count})",
                            "posted_date": (
                                txn_date + dt.timedelta(days=rng.randint(0, 4))
                            ).isoformat(),
                        },
                    )
                )

        elif archetype == "noisy_description":
            legs.append(
                _bank_row(
                    rng,
                    **{
                        **bank_defaults,
                        "memo": _mangle(narrative, counterparty, rng),
                        "reference": None,
                        "posted_date": (txn_date + dt.timedelta(days=rng.randint(0, 2))).isoformat(),
                    },
                )
            )

        for leg in legs:
            if rng.random() < signed_fraction:
                _attach_checksum(leg)

        corpus.erp_rows.append(erp)
        corpus.bank_rows.extend(legs)
        corpus.pairs.append(
            {
                "internal": erp["transaction_id"],
                "external": [leg["txn_id"] for leg in legs],
                "archetype": archetype,
                "expected_category_if_unmatched": ARCHETYPE_CATEGORY[archetype],
                "amount": amount,
            }
        )

    # Decoys: plausible but genuinely unpaired. Without them a matcher that
    # pairs everything with anything scores perfectly.
    for i in range(decoy_count):
        counterparty = rng.choice(COUNTERPARTIES)
        narrative = rng.choice(NARRATIVES)
        amount = round(rng.uniform(120.0, 180000.0), 2)
        txn_date = today - dt.timedelta(days=rng.randint(1, 120))
        description = f"{counterparty} - {narrative}"

        if i % 2 == 0:
            row = _erp_row(
                rng,
                transaction_amount=amount,
                value_date=txn_date.isoformat(),
                narrative=description,
                ref=f"REF-{rng.randint(10000, 99999)}",
            )
            corpus.erp_rows.append(row)
            corpus.decoys.append(row["transaction_id"])
        else:
            row = _bank_row(
                rng,
                amt=amount,
                posted_date=txn_date.isoformat(),
                memo=description,
                reference=f"REF-{rng.randint(10000, 99999)}",
            )
            corpus.bank_rows.append(row)
            corpus.decoys.append(row["txn_id"])

    for i in range(malformed_count):
        defect = DEFECTS[i % len(DEFECTS)]
        counterparty = rng.choice(COUNTERPARTIES)
        description = f"{counterparty} - {rng.choice(NARRATIVES)}"
        row = _bank_row(
            rng,
            amt=round(rng.uniform(120.0, 180000.0), 2),
            posted_date=(today - dt.timedelta(days=rng.randint(1, 120))).isoformat(),
            memo=description,
            reference=f"REF-{rng.randint(10000, 99999)}",
        )

        if defect == "negative_amount":
            row["amt"] = -abs(row["amt"])
        elif defect == "zero_amount":
            row["amt"] = 0
        elif defect == "future_date":
            row["posted_date"] = (today + dt.timedelta(days=rng.randint(3, 60))).isoformat()
        elif defect == "bad_currency":
            row["currency_code"] = rng.choice(["US", "DOLLARS", "U$D", ""])
        elif defect == "missing_amount":
            row["amt"] = None
        elif defect == "missing_source":
            row["channel"] = None
        elif defect == "corrupt_checksum":
            _attach_checksum(row)
            # Flip the leading hex digit: a wrong digest, still well-formed, so
            # it fails at stage 3 rather than being rejected earlier as garbage.
            row["checksum"] = ("f" if row["checksum"][0] != "f" else "0") + row["checksum"][1:]

        corpus.bank_rows.append(row)
        corpus.malformed.append({"external_id": row["txn_id"], "defect": defect})

    rng.shuffle(corpus.bank_rows)
    rng.shuffle(corpus.erp_rows)
    return corpus


def _answer_key(corpus: Corpus, args: argparse.Namespace) -> dict[str, Any]:
    by_archetype: dict[str, int] = {}
    for pair in corpus.pairs:
        by_archetype[pair["archetype"]] = by_archetype.get(pair["archetype"], 0) + 1

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "seed": args.seed,
        "generator": "tools/seed.py",
        "note": (
            "Keyed on external_id, not UUID: primary keys are assigned by "
            "Postgres at insert time and are unknowable to the generator."
        ),
        "counts": {
            "erp_records": len(corpus.erp_rows),
            "bank_records": len(corpus.bank_rows),
            "total_records": corpus.record_count,
            "true_pairs": len(corpus.pairs),
            "decoys": len(corpus.decoys),
            "malformed": len(corpus.malformed),
            "by_archetype": by_archetype,
        },
        "true_pairs": corpus.pairs,
        "decoys": corpus.decoys,
        "malformed": corpus.malformed,
    }


def _write_files(corpus: Corpus, out: Path, args: argparse.Namespace) -> None:
    out.mkdir(parents=True, exist_ok=True)

    erp_path = out / "erp_ledger.csv"
    with erp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(corpus.erp_rows[0]))
        writer.writeheader()
        writer.writerows(corpus.erp_rows)

    bank_path = out / "bank_feed.json"
    bank_path.write_text(json.dumps(corpus.bank_rows, indent=2, default=str), encoding="utf-8")

    key_path = out / "answer_key.json"
    key_path.write_text(json.dumps(_answer_key(corpus, args), indent=2, default=str), encoding="utf-8")

    print(f"  {erp_path}   {len(corpus.erp_rows)} records")
    print(f"  {bank_path}  {len(corpus.bank_rows)} records")
    print(f"  {key_path}")


def _publish_kafka(corpus: Corpus, args: argparse.Namespace) -> None:
    """Publish both feeds to the raw topic the validation pipeline consumes.

    The ERP side goes as one CSV message per batch and the bank side as JSON,
    because that is the shape mismatch `normalize` exists to absorb - sending
    both as JSON would leave the CSV path unexercised by the only thing that
    routinely feeds this system.
    """
    from kafka import KafkaProducer

    from shared.config import settings

    brokers = args.brokers or settings.kafka_broker
    topic = args.topic or settings.kafka_topic_raw

    producer = KafkaProducer(
        bootstrap_servers=brokers.split(","),
        value_serializer=lambda v: v if isinstance(v, bytes) else str(v).encode("utf-8"),
        acks="all",
        retries=3,
    )

    sent = 0
    for start in range(0, len(corpus.erp_rows), args.batch_size):
        chunk = corpus.erp_rows[start : start + args.batch_size]
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(chunk[0]))
        writer.writeheader()
        writer.writerows(chunk)
        producer.send(topic, buffer.getvalue().encode("utf-8"))
        sent += len(chunk)

    for start in range(0, len(corpus.bank_rows), args.batch_size):
        chunk = corpus.bank_rows[start : start + args.batch_size]
        producer.send(topic, json.dumps(chunk, default=str).encode("utf-8"))
        sent += len(chunk)

    producer.flush()
    producer.close()

    print(f"  published {sent} records to {topic!r} via {brokers}")

    key_path = Path(args.out) / "answer_key.json"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(json.dumps(_answer_key(corpus, args), indent=2, default=str), encoding="utf-8")
    print(f"  {key_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a two-sided reconciliation corpus with an answer key.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--count", type=int, default=400,
                        help="true pairs to generate; total records is roughly 2.4x this")
    parser.add_argument("--decoys", type=int, default=80,
                        help="plausible but genuinely unpaired records")
    parser.add_argument("--malformed", type=int, default=60,
                        help="deliberately invalid records, so stage 1-3 quarantine visibly")
    parser.add_argument("--signed-fraction", type=float, default=0.35,
                        help="fraction of bank records carrying a real sha256 checksum")
    parser.add_argument("--seed", type=int, default=20260809,
                        help="RNG seed; the same seed always yields the same corpus")
    parser.add_argument("--sink", choices=("files", "kafka"), default="files",
                        help="where to write; never Postgres, which must be reached through validation")
    parser.add_argument("--out", default="data/seed",
                        help="output directory (files sink, and the answer key in both)")
    parser.add_argument("--topic", default=None, help="Kafka topic (default: KAFKA_TOPIC_RAW)")
    parser.add_argument("--brokers", default=None, help="Kafka brokers (default: KAFKA_BROKER)")
    parser.add_argument("--batch-size", type=int, default=200, help="records per Kafka message")
    args = parser.parse_args(argv)

    if args.count < len(ARCHETYPES):
        parser.error(f"--count must be at least {len(ARCHETYPES)} to cover every archetype")

    corpus = build_corpus(
        pair_count=args.count,
        decoy_count=args.decoys,
        malformed_count=args.malformed,
        signed_fraction=args.signed_fraction,
        seed=args.seed,
    )

    print(
        f"Generated {corpus.record_count} records: "
        f"{len(corpus.erp_rows)} ERP / {len(corpus.bank_rows)} bank, "
        f"{len(corpus.pairs)} true pairs, {len(corpus.decoys)} decoys, "
        f"{len(corpus.malformed)} malformed."
    )

    if args.sink == "files":
        _write_files(corpus, Path(args.out), args)
    else:
        _publish_kafka(corpus, args)

    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
