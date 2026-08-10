"""Layer 2 - unsupervised fuzzy matching (build.md Sec. 9).

    "vectorize normalized descriptions, cluster semantically-similar txns,
     flag isolated points as true outliers (real exceptions, not near-matches)"

build.md's `fuzzy_match` returns cluster labels and LOF flags. That is the
right decomposition but stops one step short: labels are not pairs. Two
transactions landing in the same cluster is the *hypothesis* that they
reconcile; this module turns that into ranked internal-to-external candidate
pairs that scoring.py can then judge.

Three practical corrections to the reference snippet, each load-bearing:

* `vecs.toarray()` densifies the TF-IDF matrix. At 50k transactions with a
  20k-term vocabulary that is an 8 GB allocation. LOF is fitted on the sparse
  matrix directly.
* `LocalOutlierFactor(n_neighbors=5)` raises when the batch has fewer than 6
  rows. Small batches are real - a quiet weekend feed - so the neighbourhood is
  clamped to the data.
* An empty or all-blank description column makes TF-IDF raise on an empty
  vocabulary. That is a legitimate batch (descriptions are nullable), so it
  degrades to "no candidates" rather than failing the reconciliation run.

Model persistence (Sec. 9, "save to models/*.pkl; load at service start") is
only partly possible, and the reason is a scikit-learn constraint rather than a
design choice: DBSCAN implements `fit_predict` but not `predict`, so it cannot
label a new batch from a stored fit and must be refitted each run. The
TfidfVectorizer and a `novelty=True` LocalOutlierFactor *do* transform and
predict on unseen data, so those are what get persisted - and persisting the
vectorizer is what matters most, since a vocabulary refitted per batch would
make scores incomparable between runs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.cluster import DBSCAN
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.neighbors import LocalOutlierFactor

logger = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).resolve().parent.parent / "models"
VECTORIZER_PATH = MODEL_DIR / "tfidf_vectorizer.pkl"
OUTLIER_PATH = MODEL_DIR / "lof_detector.pkl"

DEFAULT_EPS = 0.4
DEFAULT_MIN_SAMPLES = 2
DEFAULT_N_NEIGHBORS = 5

#: A same-amount pair posted this many days apart is still one settlement, not
#: two unrelated transactions. 20, not the previous 10, because a genuine
#: timing difference (bank processing lag, holidays, period-end drift) was
#: measured going out that far and the blocking channel is what is supposed to
#: catch it on amount+date alone - a real pair with an identical description
#: never needed this channel, only the ones the ML text similarity misses in a
#: description-heavy batch with a handful of repeated narratives. At 10 the
#: channel silently missed every pair posted 11-20 days apart and TIMING_
#: DIFFERENCE fell back on whatever Channel 1's tied cosine scores happened to
#: rank first, which was usually the wrong candidate.
DEFAULT_DATE_TOLERANCE_DAYS = 20

#: How many external candidates Channel 1 proposes per internal row, at most.
#: A split settlement's legs never clear Channel 2 (each leg is a fraction of
#: the obligation, so amount_proximity's relative tolerance always scores it
#: 0), so this cap is their only path to being nominated at all - and a split
#: is 2 to 4 legs. At the previous cap of 3, a 4-leg split could not possibly
#: get all its legs proposed even with a perfect ranking, and any 2- or 3-leg
#: split competing against a same-narrative decoy (a handful of repeated
#: descriptions is normal in real remittance data too, not just this corpus)
#: lost a slot to it. Raised to match MAX_CANDIDATES in pipeline.py, which is
#: what actually consumes these - proposing fewer candidates than the
#: downstream co-settling logic is willing to keep serves nothing.
DEFAULT_MAX_CANDIDATES_PER_ROW = 6

#: A partial payment or split leg settles somewhere in this range of the
#: obligation - never all of it (that is a match, not an exception) and never
#: a sliver (below MIN_LEG_SHARE it is closer to noise than a payment).
#: _fraction_blocking_candidates uses these to propose the pairs Channel 1 and
#: Channel 2 both structurally cannot: amount_proximity is 0 for anything
#: outside AMOUNT_TOLERANCE of *equal*, and a leg is by definition not equal
#: to the obligation, so neither channel ever nominates one on amount grounds.
MIN_LEG_SHARE = 0.05
MAX_LEG_SHARE = 0.95

_WHITESPACE = re.compile(r"\s+")
_NOISE = re.compile(r"[^a-z0-9 ]+")


@dataclass(frozen=True)
class CandidatePair:
    """A hypothesis that two rows reconcile. scoring.py decides if it holds."""

    internal_position: int
    external_position: int
    description_similarity: float
    cluster: int


def normalize_description(text: Any) -> str:
    """Lower-case, strip punctuation and collapse whitespace.

    Bank narratives carry reference noise ("ACH CREDIT   MERIDIAN CAP//REF9931")
    that would otherwise dominate the TF-IDF vocabulary with hapaxes.
    """
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return ""
    lowered = str(text).lower()
    return _WHITESPACE.sub(" ", _NOISE.sub(" ", lowered)).strip()


def fuzzy_match(unmatched_df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """build.md Sec. 9's entry point, kept as documented.

    Returns (cluster labels, LOF flags) aligned with `unmatched_df`. Same
    cluster -> candidate pair; LOF == -1 -> send to the exception queue.
    Callers wanting actual pairs should use `FuzzyMatcher.find_candidates`.
    """
    matcher = FuzzyMatcher()
    clusters, outliers, _ = matcher.cluster(unmatched_df)
    return clusters, outliers


class FuzzyMatcher:
    """TF-IDF + DBSCAN + LOF over unmatched transaction descriptions."""

    def __init__(
        self,
        vectorizer: TfidfVectorizer | None = None,
        outlier_detector: LocalOutlierFactor | None = None,
        eps: float = DEFAULT_EPS,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        n_neighbors: int = DEFAULT_N_NEIGHBORS,
    ) -> None:
        self.vectorizer = vectorizer
        self.outlier_detector = outlier_detector
        self.eps = eps
        self.min_samples = min_samples
        self.n_neighbors = n_neighbors
        self.is_fitted = vectorizer is not None

    # ── fitting and persistence ──────────────────────────────────────────

    def fit(self, descriptions: list[str] | pd.Series) -> "FuzzyMatcher":
        """Fit on historical descriptions so the vocabulary is stable.

        A vectorizer refitted per batch gives every run a different vector
        space, so similarity scores could not be compared across runs and the
        confidence threshold would mean something different every time.
        """
        corpus = [normalize_description(d) for d in descriptions]
        corpus = [c for c in corpus if c]

        if len(corpus) < 2:
            raise ValueError(
                f"need at least 2 non-empty descriptions to fit, got {len(corpus)}"
            )

        self.vectorizer = TfidfVectorizer(
            analyzer="word",
            ngram_range=(1, 2),
            min_df=1,
            sublinear_tf=True,
        )
        matrix = self.vectorizer.fit_transform(corpus)

        # novelty=True is what makes the detector reusable on later batches.
        neighbours = max(1, min(self.n_neighbors, matrix.shape[0] - 1))
        self.outlier_detector = LocalOutlierFactor(
            n_neighbors=neighbours, metric="cosine", novelty=True
        )
        self.outlier_detector.fit(matrix)

        self.is_fitted = True
        logger.info(
            "Fitted on %d descriptions; vocabulary %d terms",
            len(corpus), len(self.vectorizer.vocabulary_),
        )
        return self

    def save(self, model_dir: Path = MODEL_DIR) -> dict[str, str]:
        if not self.is_fitted:
            raise RuntimeError("refusing to persist an unfitted matcher")
        model_dir.mkdir(parents=True, exist_ok=True)
        joblib.dump(self.vectorizer, model_dir / VECTORIZER_PATH.name)
        joblib.dump(self.outlier_detector, model_dir / OUTLIER_PATH.name)
        return {
            "vectorizer": str(model_dir / VECTORIZER_PATH.name),
            "outlier_detector": str(model_dir / OUTLIER_PATH.name),
        }

    @classmethod
    def load(cls, model_dir: Path = MODEL_DIR) -> "FuzzyMatcher | None":
        """Load persisted models, or None when none have been fitted yet.

        None is returned rather than raising so the service can start and
        report an unfitted state on /health, instead of crash-looping.
        """
        vectorizer_path = model_dir / VECTORIZER_PATH.name
        outlier_path = model_dir / OUTLIER_PATH.name

        if not vectorizer_path.exists():
            logger.warning("No persisted vectorizer at %s", vectorizer_path)
            return None

        vectorizer = joblib.load(vectorizer_path)
        detector = joblib.load(outlier_path) if outlier_path.exists() else None
        logger.info("Loaded fuzzy matcher from %s", model_dir)
        return cls(vectorizer=vectorizer, outlier_detector=detector)

    # ── inference ────────────────────────────────────────────────────────

    def _vectorize(self, frame: pd.DataFrame):
        corpus = [normalize_description(d) for d in frame.get("description", [])]

        if not any(corpus):
            return None, corpus

        if self.is_fitted:
            return self.vectorizer.transform(corpus), corpus

        # No persisted model: fit on this batch so a first run still reconciles.
        # Scores are batch-local until a model is fitted on history.
        logger.warning("No fitted vectorizer; fitting on the current batch only")
        local = TfidfVectorizer(analyzer="word", ngram_range=(1, 2), sublinear_tf=True)
        try:
            return local.fit_transform(corpus), corpus
        except ValueError as exc:
            logger.warning("TF-IDF could not build a vocabulary (%s)", exc)
            return None, corpus

    def cluster(
        self, frame: pd.DataFrame
    ) -> tuple[np.ndarray, np.ndarray, Any]:
        """Return (cluster labels, outlier flags, tf-idf matrix).

        Label -1 means DBSCAN found no cluster; flag -1 means LOF considers the
        row isolated - "a real exception, not a near-match".
        """
        n = len(frame)
        if n == 0:
            return np.array([]), np.array([]), None

        matrix, _ = self._vectorize(frame)
        if matrix is None or matrix.shape[0] == 0:
            # No usable text: everything is unclustered, nothing is a candidate.
            return np.full(n, -1), np.ones(n), None

        clusters = DBSCAN(
            eps=self.eps, min_samples=self.min_samples, metric="cosine"
        ).fit_predict(matrix)

        outliers = self._detect_outliers(matrix, n)
        return clusters, outliers, matrix

    def _detect_outliers(self, matrix, n: int) -> np.ndarray:
        """LOF flags, or all-inliers when the batch is too small to judge."""
        if n < 3:
            # With one or two rows there is no neighbourhood to be isolated
            # from. Declaring them outliers would push every small batch
            # straight to the exception queue.
            return np.ones(n)

        try:
            if self.outlier_detector is not None and self.is_fitted:
                return self.outlier_detector.predict(matrix)
            neighbours = max(1, min(self.n_neighbors, n - 1))
            return LocalOutlierFactor(
                n_neighbors=neighbours, metric="cosine"
            ).fit_predict(matrix)
        except Exception as exc:
            # An outlier detector that cannot run must not silently mark
            # everything an exception.
            logger.warning("LOF unavailable (%s); treating all rows as inliers", exc)
            return np.ones(n)

    def _blocking_candidates(
        self,
        internal: pd.DataFrame,
        external: pd.DataFrame,
        amount_tolerance: float,
        date_tolerance_days: int,
        max_block_candidates: int = 10,
    ) -> set[tuple[int, int]]:
        """Second candidate channel: block on amount and date proximity.

        Clustering on descriptions alone loses any pair whose bank narrative
        was mangled beyond the eps cut - measured at 37 of 40 for both the
        missing-reference and noisy-description archetypes, which never
        reached scoring at all. Those pairs have near-identical amounts and
        dates, so blocking on the numeric fields recovers them.

        This is standard record-linkage blocking and is *additive*: it only
        proposes more hypotheses. Nothing is confirmed here - every candidate
        still goes through scoring.py and the configurable threshold, so the
        false-positive guarantee Sec. 9 rests on is untouched.

        Cost control matters here. The tolerance is *relative*, so a 20% window
        on a 180,000 settlement spans +/-36,000 and can cover a large share of a
        realistic batch - which made candidate generation effectively quadratic
        (measured at 8.4x the time for 3.5x the rows). Each internal row
        therefore keeps only its `max_block_candidates` nearest counterparts by
        absolute amount difference. The true counterpart of a genuine pair is
        near-identical in amount, so it is always among the closest few; what
        gets dropped is the long tail of coincidentally-similar amounts that
        scoring would have rejected anyway.
        """
        if "amount" not in internal.columns or "amount" not in external.columns:
            return set()

        external_amounts = pd.to_numeric(external["amount"], errors="coerce").to_numpy(
            dtype=float
        )
        internal_amounts = pd.to_numeric(internal["amount"], errors="coerce").to_numpy(
            dtype=float
        )

        order = np.argsort(external_amounts, kind="stable")
        sorted_amounts = external_amounts[order]

        internal_dates = pd.to_datetime(internal.get("txn_date"), errors="coerce")
        external_dates = pd.to_datetime(external.get("txn_date"), errors="coerce")

        pairs: set[tuple[int, int]] = set()

        for i, amount in enumerate(internal_amounts):
            if not np.isfinite(amount) or amount == 0:
                continue

            # A partial payment settles less than the invoice, so the window is
            # deliberately asymmetric-tolerant: anything within tolerance of the
            # larger of the two values.
            window = abs(amount) * amount_tolerance
            low = np.searchsorted(sorted_amounts, amount - window, side="left")
            high = np.searchsorted(sorted_amounts, amount + window, side="right")
            if low >= high:
                continue

            in_window = order[low:high]

            # Keep only the closest few by amount, so a wide window on a large
            # settlement cannot degenerate into a full scan.
            if len(in_window) > max_block_candidates:
                gaps = np.abs(external_amounts[in_window] - amount)
                in_window = in_window[
                    np.argpartition(gaps, max_block_candidates)[:max_block_candidates]
                ]

            for position in in_window:
                if internal_dates is not None and external_dates is not None:
                    left, right = internal_dates.iloc[i], external_dates.iloc[position]
                    if pd.notna(left) and pd.notna(right):
                        if abs((left - right).days) > date_tolerance_days:
                            continue
                pairs.add((i, int(position)))

        return pairs

    def _fraction_blocking_candidates(
        self,
        internal: pd.DataFrame,
        external: pd.DataFrame,
        date_tolerance_days: int,
        max_block_candidates: int = 10,
    ) -> set[tuple[int, int]]:
        """Third candidate channel: an external row that plausibly settles
        *part* of an internal row's amount, not roughly all of it.

        Channel 1 (description clustering) is a split or partial-payment
        leg's only path to being nominated today, because Channel 2 blocks on
        near-equal amounts and a leg is never near-equal by definition. That
        is fine at small scale, where a leg's description similarity (diluted
        by "(part n/m)" or simply not being the obligation's own text) still
        edges out unrelated rows for one of Channel 1's top-K slots. It stops
        being fine as the batch grows relative to a bounded narrative
        vocabulary: every exact-duplicate-text decoy scores a clean 1.0 and
        outranks a genuine leg's diluted similarity, so decoy density (not K)
        decides whether a leg survives the cut - and decoy density grows with
        the batch while K does not. This channel proposes those pairs on
        amount alone, so recovering a leg never depends on winning that race.

        The same cost-control shape as `_blocking_candidates`: only the
        `max_block_candidates` nearest by amount survive per internal row, so
        a wide fractional band on a large settlement cannot go quadratic.
        """
        if "amount" not in internal.columns or "amount" not in external.columns:
            return set()

        external_amounts = pd.to_numeric(external["amount"], errors="coerce").to_numpy(
            dtype=float
        )
        internal_amounts = pd.to_numeric(internal["amount"], errors="coerce").to_numpy(
            dtype=float
        )

        order = np.argsort(external_amounts, kind="stable")
        sorted_amounts = external_amounts[order]

        internal_dates = pd.to_datetime(internal.get("txn_date"), errors="coerce")
        external_dates = pd.to_datetime(external.get("txn_date"), errors="coerce")

        pairs: set[tuple[int, int]] = set()

        for i, amount in enumerate(internal_amounts):
            if not np.isfinite(amount) or amount <= 0:
                continue

            low = np.searchsorted(sorted_amounts, amount * MIN_LEG_SHARE, side="left")
            high = np.searchsorted(sorted_amounts, amount * MAX_LEG_SHARE, side="right")
            if low >= high:
                continue

            in_window = order[low:high]

            if len(in_window) > max_block_candidates:
                # Closest to the full amount first - the largest plausible
                # leg is also the one likeliest to be a genuine partial
                # payment rather than one piece of a longer split.
                gaps = np.abs(external_amounts[in_window] - amount)
                in_window = in_window[
                    np.argpartition(gaps, max_block_candidates)[:max_block_candidates]
                ]

            for position in in_window:
                if internal_dates is not None and external_dates is not None:
                    left, right = internal_dates.iloc[i], external_dates.iloc[position]
                    if pd.notna(left) and pd.notna(right):
                        if abs((left - right).days) > date_tolerance_days:
                            continue
                pairs.add((i, int(position)))

        return pairs

    def find_candidates(
        self,
        internal: pd.DataFrame,
        external: pd.DataFrame,
        max_candidates_per_row: int = DEFAULT_MAX_CANDIDATES_PER_ROW,
        amount_tolerance: float = 0.20,
        date_tolerance_days: int = DEFAULT_DATE_TOLERANCE_DAYS,
    ) -> tuple[list[CandidatePair], set[int], set[int]]:
        """Propose internal-to-external pairs.

        Three channels, unioned: DBSCAN cluster co-membership (Sec. 9's
        method), amount/date blocking for near-equal pairs, and amount/date
        blocking for fractional pairs (see `_blocking_candidates` and
        `_fraction_blocking_candidates` for why neither of the first two
        alone covers a split or partial payment). Returns (candidates,
        isolated internal positions, isolated external positions). Isolated
        rows are LOF outliers with no cluster - "real exceptions, not
        near-matches" - so they skip scoring entirely.
        """
        if internal.empty or external.empty:
            return [], set(), set()

        combined = pd.concat([internal, external], ignore_index=True)
        clusters, outliers, matrix = self.cluster(combined)

        split = len(internal)
        proposed: set[tuple[int, int]] = set()

        # Channel 1: description clusters.
        if matrix is not None:
            for label in set(clusters):
                if label == -1:
                    continue

                members = np.flatnonzero(clusters == label)
                internal_members = [m for m in members if m < split]
                external_members = [m for m in members if m >= split]
                if not internal_members or not external_members:
                    continue

                similarity = cosine_similarity(
                    matrix[internal_members], matrix[external_members]
                )

                for i, internal_position in enumerate(internal_members):
                    ranked = np.argsort(similarity[i])[::-1][:max_candidates_per_row]
                    for j in ranked:
                        proposed.add(
                            (
                                int(internal_position),
                                int(external_members[j] - split),
                            )
                        )

        # Channel 2: amount/date blocking.
        proposed |= self._blocking_candidates(
            internal, external, amount_tolerance, date_tolerance_days
        )

        # Channel 3: fractional amount blocking (splits and partial payments).
        proposed |= self._fraction_blocking_candidates(
            internal, external, date_tolerance_days
        )

        # Similarity is attached once, from the shared matrix, so all three
        # channels produce identically-comparable scores. Computed in one
        # vectorised pass - a cosine_similarity() call per candidate was pure
        # numpy call overhead repeated tens of thousands of times.
        ordered = sorted(proposed)
        similarities = self._similarities(matrix, ordered, split)

        candidates = [
            CandidatePair(
                internal_position=i,
                external_position=j,
                description_similarity=similarities[position],
                cluster=int(clusters[i]) if len(clusters) > i else -1,
            )
            for position, (i, j) in enumerate(ordered)
        ]

        isolated_internal = {
            int(p) for p in np.flatnonzero((clusters == -1) & (outliers == -1)) if p < split
        }
        isolated_external = {
            int(p) - split
            for p in np.flatnonzero((clusters == -1) & (outliers == -1))
            if p >= split
        }

        logger.debug(
            "ML layer: %d candidates across %d clusters; %d isolated",
            len(candidates),
            len(set(clusters)) - (1 if -1 in clusters else 0),
            len(isolated_internal) + len(isolated_external),
        )
        return candidates, isolated_internal, isolated_external

    @staticmethod
    def _similarities(matrix, pairs: list[tuple[int, int]], split: int) -> list[float]:
        """Cosine similarity for every candidate pair, in one pass.

        TfidfVectorizer L2-normalises its rows, so cosine similarity is just
        the row-wise dot product - computed here as an elementwise multiply and
        row sum over the two gathered submatrices.

        Zero when there is no usable text, which is honest rather than
        flattering: a blocking-only candidate with no description overlap
        should score on amount and date alone.
        """
        if matrix is None or not pairs:
            return [0.0] * len(pairs)

        rows = np.fromiter((i for i, _ in pairs), dtype=int, count=len(pairs))
        cols = np.fromiter((j + split for _, j in pairs), dtype=int, count=len(pairs))

        limit = matrix.shape[0]
        valid = (rows < limit) & (cols < limit)
        if not valid.any():
            return [0.0] * len(pairs)

        products = matrix[rows[valid]].multiply(matrix[cols[valid]]).sum(axis=1)
        computed = np.asarray(products).ravel()

        out = np.zeros(len(pairs), dtype=float)
        out[valid] = np.clip(computed, 0.0, 1.0)
        return out.tolist()


__all__ = [
    "FuzzyMatcher",
    "CandidatePair",
    "fuzzy_match",
    "normalize_description",
    "MODEL_DIR",
    "VECTORIZER_PATH",
    "OUTLIER_PATH",
]
