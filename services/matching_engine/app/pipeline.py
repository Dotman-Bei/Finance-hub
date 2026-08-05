"""Sequential rule -> ML orchestration (build.md Sec. 9).

    "Two layers in one sequential pipeline. Deterministic first; only the
     leftovers go to ML."

The ordering is the whole point. Exact matches are certain and cheap, so they
are taken first and never exposed to a probabilistic score; the ML layer only
ever sees what determinism could not settle. That keeps the expensive TF-IDF
and clustering work proportional to the genuinely ambiguous remainder rather
than to total volume, and it means a false positive can only ever originate
in Layer 2 - where the threshold controls it.

Like the validation pipeline, this module performs no I/O. `reconcile` takes a
frame and returns a decision object; persistence.py writes it. That is what
lets test_precision.py grade the engine against labelled ground truth and
test_latency.py time it, both without a database.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from shared.config import settings

from .ml_model import CandidatePair, FuzzyMatcher
from .rule_engine import RULE_CONFIDENCE, rule_match, split_by_side
from .scoring import ScoredPair, partition_by_threshold, resolve_one_to_one, score_pair

logger = logging.getLogger(__name__)


@dataclass
class UnmatchedItem:
    """A transaction that reconciled against nothing. Bound for exceptionqueue."""

    transaction_id: Any
    reason: str
    best_confidence: float = 0.0
    best_counterpart_id: Any = None


@dataclass
class ReconcileResult:
    matched: list[ScoredPair] = field(default_factory=list)
    unmatched: list[UnmatchedItem] = field(default_factory=list)
    duration_ms: float = 0.0
    threshold: float = 0.0
    total_input: int = 0

    @property
    def match_rate(self) -> float:
        """Share of input transactions that ended up in a confirmed pair.

        Each pair consumes two transactions, so the numerator is doubled. A
        batch where every record reconciles scores 1.0.
        """
        if self.total_input == 0:
            return 0.0
        return round(min(1.0, (len(self.matched) * 2) / self.total_input), 4)

    @property
    def rule_matched(self) -> int:
        return sum(1 for p in self.matched if p.match_type == "RULE")

    @property
    def ml_matched(self) -> int:
        return sum(1 for p in self.matched if p.match_type == "ML")

    def summary(self) -> dict[str, Any]:
        """The response shape Sec. 9 specifies for POST /reconcile."""
        return {
            "matched": len(self.matched),
            "unmatched": len(self.unmatched),
            "match_rate": self.match_rate,
            "rule_matched": self.rule_matched,
            "ml_matched": self.ml_matched,
            "threshold": self.threshold,
            "duration_ms": round(self.duration_ms, 2),
            "total_input": self.total_input,
        }


class MatchingPipeline:
    def __init__(
        self,
        matcher: FuzzyMatcher | None = None,
        threshold: float | None = None,
        internal_sources: tuple[str, ...] = ("erp",),
    ) -> None:
        self.matcher = matcher or FuzzyMatcher()
        # Read once at construction; Sec. 9 wants it configurable, not constant.
        self.threshold = (
            threshold if threshold is not None else settings.match_confidence_threshold
        )
        self.internal_sources = internal_sources

    def reconcile(self, transactions: pd.DataFrame) -> ReconcileResult:
        started = time.perf_counter()

        if transactions.empty:
            return ReconcileResult(threshold=self.threshold, total_input=0)

        transactions = transactions.reset_index(drop=True)
        internal, external = split_by_side(transactions, self.internal_sources)

        if internal.empty or external.empty:
            # Only one side present: nothing can reconcile, and saying so is
            # more useful than returning an empty result.
            reason = "no counterpart feed in this batch"
            return ReconcileResult(
                unmatched=[
                    UnmatchedItem(transaction_id=row["id"], reason=reason)
                    for _, row in transactions.iterrows()
                ],
                duration_ms=(time.perf_counter() - started) * 1000,
                threshold=self.threshold,
                total_input=len(transactions),
            )

        # ── Layer 1: deterministic ───────────────────────────────────────
        rule_pairs, _ = rule_match(internal, external)

        claimed_internal = set(rule_pairs["id_int"]) if not rule_pairs.empty else set()
        claimed_external = set(rule_pairs["id_ext"]) if not rule_pairs.empty else set()

        matched: list[ScoredPair] = [
            ScoredPair(
                internal_id=row["id_int"],
                external_id=row["id_ext"],
                confidence=RULE_CONFIDENCE,
                match_type="RULE",
                components={"exact_key_match": 1.0},
            )
            for _, row in rule_pairs.iterrows()
        ]

        # ── Layer 2: only the leftovers ──────────────────────────────────
        internal_left = internal[~internal["id"].isin(claimed_internal)].reset_index(drop=True)
        external_left = external[~external["id"].isin(claimed_external)].reset_index(drop=True)

        scored: list[ScoredPair] = []
        isolated_ids: set[Any] = set()

        if not internal_left.empty and not external_left.empty:
            candidates, iso_int, iso_ext = self.matcher.find_candidates(
                internal_left, external_left
            )
            scored = self._score(candidates, internal_left, external_left)
            isolated_ids = {internal_left.at[p, "id"] for p in iso_int if p < len(internal_left)}
            isolated_ids |= {external_left.at[p, "id"] for p in iso_ext if p < len(external_left)}

        # ── Threshold ────────────────────────────────────────────────────
        resolved = resolve_one_to_one(scored)
        above, below = partition_by_threshold(resolved, self.threshold)
        matched.extend(above)

        unmatched = self._collect_unmatched(
            internal_left, external_left, above, below, isolated_ids
        )

        result = ReconcileResult(
            matched=matched,
            unmatched=unmatched,
            duration_ms=(time.perf_counter() - started) * 1000,
            threshold=self.threshold,
            total_input=len(transactions),
        )

        logger.info(
            "Reconciled %d: %d matched (%d rule / %d ML), %d unmatched, rate %.2f%%",
            result.total_input, len(result.matched), result.rule_matched,
            result.ml_matched, len(result.unmatched), result.match_rate * 100,
        )
        return result

    # ── helpers ──────────────────────────────────────────────────────────

    def _score(
        self,
        candidates: list[CandidatePair],
        internal: pd.DataFrame,
        external: pd.DataFrame,
    ) -> list[ScoredPair]:
        scored: list[ScoredPair] = []
        for candidate in candidates:
            if candidate.internal_position >= len(internal):
                continue
            if candidate.external_position >= len(external):
                continue
            scored.append(
                score_pair(
                    internal.iloc[candidate.internal_position].to_dict(),
                    external.iloc[candidate.external_position].to_dict(),
                    candidate.description_similarity,
                )
            )
        return scored

    @staticmethod
    def _collect_unmatched(
        internal: pd.DataFrame,
        external: pd.DataFrame,
        above: list[ScoredPair],
        below: list[ScoredPair],
        isolated_ids: set[Any],
    ) -> list[UnmatchedItem]:
        """Everything the ML layer could not confirm, with why.

        The reason and the best score reached are carried through because the
        exception handler (Sec. 10) engineers its features from exactly this:
        how close the nearest candidate came, and on what.
        """
        claimed = {p.internal_id for p in above} | {p.external_id for p in above}

        best: dict[Any, ScoredPair] = {}
        for pair in below:
            for side_id in (pair.internal_id, pair.external_id):
                current = best.get(side_id)
                if current is None or pair.confidence > current.confidence:
                    best[side_id] = pair

        unmatched: list[UnmatchedItem] = []
        for frame in (internal, external):
            for _, row in frame.iterrows():
                transaction_id = row["id"]
                if transaction_id in claimed:
                    continue

                near = best.get(transaction_id)
                if near is not None:
                    counterpart = (
                        near.external_id
                        if near.internal_id == transaction_id
                        else near.internal_id
                    )
                    unmatched.append(
                        UnmatchedItem(
                            transaction_id=transaction_id,
                            reason="below confidence threshold",
                            best_confidence=near.confidence,
                            best_counterpart_id=counterpart,
                        )
                    )
                elif transaction_id in isolated_ids:
                    unmatched.append(
                        UnmatchedItem(
                            transaction_id=transaction_id,
                            reason="isolated outlier - no similar transaction found",
                        )
                    )
                else:
                    unmatched.append(
                        UnmatchedItem(
                            transaction_id=transaction_id,
                            reason="no candidate counterpart",
                        )
                    )

        return unmatched


__all__ = ["MatchingPipeline", "ReconcileResult", "UnmatchedItem"]
