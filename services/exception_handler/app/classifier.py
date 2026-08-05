"""Exception classification (build.md Sec. 10).

    "Random Forest (robust to the class imbalance typical of exception data).
     Output is one of four categories."

**The cold-start problem, and how it is handled honestly.**

A Random Forest is supervised. On day one no human has resolved anything, so
there are no labels and there is no model - build.md does not address this.
Three things could be done and only one of them is acceptable:

  * Ship a pre-fitted model trained on invented data. That is fabricated
    behaviour dressed as intelligence, and it is exactly what this project
    forbids. Not done.
  * Refuse to classify until 200 humans decisions exist. Correct but useless:
    the dashboard would show an untriaged queue for weeks.
  * Classify with a deterministic rule set derived from the feature semantics,
    label every prediction with the engine that made it, and let the Random
    Forest take over as soon as real labels exist. This is what is implemented.

`BootstrapClassifier` is real logic over real features - not fabricated data -
and every result it produces is tagged `engine="bootstrap"` so no consumer can
mistake it for a learned prediction. `/health` and the API response both report
which engine answered.

Training on bootstrap labels is *weak supervision*: the Random Forest learns to
reproduce the rules, which buys generalisation across feature combinations the
rules handle crudely, but it cannot exceed them. Only human resolutions (Sec.
11's feedback loop) make it genuinely better, and `train()` reports the mix so
that distinction is never lost.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from shared.models.enums import ExceptionCategory

from .features import FEATURE_NAMES, FEATURE_VERSION, ExceptionFeatures

logger = logging.getLogger(__name__)

CATEGORIES = [c.value for c in ExceptionCategory]

MODEL_DIR = Path(__file__).resolve().parent.parent / "models"
MODEL_PATH = MODEL_DIR / "rf_classifier.pkl"

#: Ratio within this of 1.0 counts as "the same amount".
AMOUNT_EQUAL_TOLERANCE = 0.01

#: Date gap beyond this is a period boundary problem, not noise.
TIMING_DRIFT_DAYS = 3

#: A split settlement's parts must account for most of the obligation.
SPLIT_COVERAGE = 0.90


@dataclass
class Classification:
    category: str
    confidence: float
    engine: str  # "random_forest" | "bootstrap"
    rationale: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "confidence": round(self.confidence, 4),
            "engine": self.engine,
            "rationale": self.rationale,
        }


class BootstrapClassifier:
    """Deterministic classifier over the engineered features.

    Ordering matters and is not arbitrary. Split settlement is tested first
    because it is the only category defined by *multiple* counterparts, and a
    split viewed one leg at a time looks exactly like a partial payment.
    """

    engine = "bootstrap"

    def classify(self, features: ExceptionFeatures) -> Classification:
        ratio = features.amount_ratio
        shortfall = features.amount_shortfall
        amount = float(features.context.get("amount") or 0.0)
        covered = (
            (amount - shortfall) / amount if amount else 0.0
        )

        # 1. Several counterparts against one obligation.
        #
        # Multiplicity is the definition, not coverage. An earlier version
        # also required covered >= SPLIT_COVERAGE, which sent every two-leg
        # split that settled 85-91% to PARTIAL_PAYMENT and cost 42% of
        # split-settlement recall. Coverage now only scales the confidence and
        # feeds the "unallocated remainder" the resolution reports.
        if features.counterpart_count >= 2:
            return Classification(
                ExceptionCategory.SPLIT_SETTLEMENT.value,
                confidence=min(
                    0.95,
                    0.55 + 0.08 * features.counterpart_count + 0.15 * min(covered, 1.0),
                ),
                engine=self.engine,
                rationale=(
                    f"{int(features.counterpart_count)} counterparts covering "
                    f"{covered:.0%} of the obligation"
                ),
            )

        amounts_equal = abs(ratio - 1.0) <= AMOUNT_EQUAL_TOLERANCE

        # 2. Same amount, but posted outside the period.
        if amounts_equal and features.date_delta_days > TIMING_DRIFT_DAYS:
            return Classification(
                ExceptionCategory.TIMING_DIFFERENCE.value,
                confidence=min(0.95, 0.70 + 0.02 * features.date_delta_days),
                engine=self.engine,
                rationale=(
                    f"amounts agree, counterpart posted "
                    f"{int(features.date_delta_days)} days apart"
                ),
            )

        # 3. Same amount, close in time, but no reference to join on.
        if amounts_equal and (
            features.has_reference_code == 0.0 or features.reference_agreement == 0.5
        ):
            return Classification(
                ExceptionCategory.MISSING_REFERENCE_CODE.value,
                confidence=0.85,
                engine=self.engine,
                rationale="amounts and dates agree but no reference code to match on",
            )

        # 4. The two amounts differ materially: one side is short.
        #
        # Tested symmetrically, because both sides of an unmatched pair land in
        # the queue and both get classified. Viewed from the obligation the
        # ratio is 0.64; viewed from the receipt it is 1.57. Only testing
        # ratio < 1 classified the obligation as a PARTIAL_PAYMENT and dropped
        # its counterpart through to the default, so the same discrepancy
        # appeared in the queue twice under two categories with two different
        # remediation pathways.
        if ratio > 0 and abs(ratio - 1.0) > AMOUNT_EQUAL_TOLERANCE:
            settled_share = min(ratio, 1.0 / ratio)
            return Classification(
                ExceptionCategory.PARTIAL_PAYMENT.value,
                confidence=min(0.92, 0.55 + 0.4 * settled_share),
                engine=self.engine,
                rationale=(
                    f"amounts differ; the smaller settles {settled_share:.0%} "
                    f"of the larger"
                ),
            )

        # 5. Nothing nominated at all. A missing reference is the most common
        # reason a genuine counterpart was never found, and it routes to the
        # pathway that surfaces candidates for manual assignment - the right
        # place for a human to start.
        if features.counterpart_count == 0:
            return Classification(
                ExceptionCategory.MISSING_REFERENCE_CODE.value,
                confidence=0.45,
                engine=self.engine,
                rationale="no counterpart candidate was nominated",
            )

        # 6. Counterpart larger than the obligation, or otherwise unclear.
        return Classification(
            ExceptionCategory.TIMING_DIFFERENCE.value,
            confidence=0.40,
            engine=self.engine,
            rationale="no decisive signal; defaulting to a period review",
        )


class ExceptionClassifier:
    """Random Forest classifier, falling back to the bootstrap rules.

    `classify` keeps build.md Sec. 10's signature and semantics - predict_proba,
    take the argmax, return (category, confidence) - wrapped so the caller also
    learns which engine answered.
    """

    def __init__(self, path: Path | str = MODEL_PATH, allow_bootstrap: bool = True):
        self.path = Path(path)
        self.model = None
        self.metadata: dict[str, Any] = {}
        self.bootstrap = BootstrapClassifier() if allow_bootstrap else None
        self._loaded_mtime: float | None = None
        self._load()

    # ── model lifecycle ──────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            logger.warning(
                "No classifier at %s. Serving bootstrap rules until one is "
                "trained (POST /models/train).",
                self.path,
            )
            return

        try:
            bundle = joblib.load(self.path)
        except Exception as exc:
            logger.error("Classifier at %s is unreadable (%s)", self.path, exc)
            return

        # Older bundles were the bare estimator; newer ones carry metadata.
        if isinstance(bundle, dict) and "model" in bundle:
            stored_features = tuple(bundle.get("feature_names", ()))
            if stored_features and stored_features != FEATURE_NAMES:
                # Silently predicting on mismatched columns would produce
                # confident nonsense, which is worse than not predicting.
                logger.error(
                    "Refusing to load %s: it was trained on a different feature "
                    "set (%d columns vs %d). Retrain.",
                    self.path, len(stored_features), len(FEATURE_NAMES),
                )
                return
            self.model = bundle["model"]
            self.metadata = {k: v for k, v in bundle.items() if k != "model"}
        else:
            self.model = bundle
            self.metadata = {"feature_version": "unknown"}

        try:
            self._loaded_mtime = self.path.stat().st_mtime
        except OSError:
            self._loaded_mtime = None

        logger.info("Loaded Random Forest from %s", self.path)

    def reload_if_changed(self) -> bool:
        """Pick up a model retrained by the Celery worker (Sec. 11 hot-swap).

        The worker is a *separate process*, so the API's in-memory model does
        not change when it writes a new file. Without this the retrain loop
        would appear to work - the file updates, /models reports the new
        metrics on restart - while every live classification kept using the
        stale model until the container was redeployed.

        Compares the file's mtime against what was loaded. Cheap enough to call
        before each triage batch.
        """
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False

        if self._loaded_mtime is not None and mtime <= self._loaded_mtime:
            return False

        previous = self.metadata.get("trained_at")
        self._load()
        if self.metadata.get("trained_at") != previous:
            logger.info(
                "Hot-swapped classifier (trained_at %s -> %s)",
                previous, self.metadata.get("trained_at"),
            )
            return True
        return False

    @property
    def is_trained(self) -> bool:
        return self.model is not None

    # ── inference ────────────────────────────────────────────────────────

    def classify(self, features: ExceptionFeatures | list[float]) -> Classification:
        vector = (
            features.as_vector()
            if isinstance(features, ExceptionFeatures)
            else list(features)
        )

        if self.model is None:
            if self.bootstrap is None or not isinstance(features, ExceptionFeatures):
                raise RuntimeError(
                    "No trained classifier and no bootstrap available. "
                    "Train a model before classifying."
                )
            return self.bootstrap.classify(features)

        proba = self.model.predict_proba([vector])[0]
        index = int(np.argmax(proba))
        category = self.model.classes_[index]

        return Classification(
            category=str(category),
            confidence=float(proba[index]),
            engine="random_forest",
            rationale=self._explain(vector),
        )

    def classify_batch(self, rows: list[ExceptionFeatures]) -> list[Classification]:
        if self.model is None:
            return [self.classify(row) for row in rows]

        vectors = [row.as_vector() for row in rows]
        probabilities = self.model.predict_proba(vectors)

        results = []
        for vector, proba in zip(vectors, probabilities):
            index = int(np.argmax(proba))
            results.append(
                Classification(
                    category=str(self.model.classes_[index]),
                    confidence=float(proba[index]),
                    engine="random_forest",
                    rationale=self._explain(vector),
                )
            )
        return results

    def _explain(self, vector: list[float]) -> str:
        """Name the features that drove the forest, for the audit trail."""
        importances = getattr(self.model, "feature_importances_", None)
        if importances is None:
            return ""
        top = np.argsort(importances)[::-1][:3]
        return ", ".join(
            f"{FEATURE_NAMES[i]}={vector[i]:.3g}" for i in top if i < len(vector)
        )

    # ── training ─────────────────────────────────────────────────────────

    def evaluate(self, samples: list[tuple[ExceptionFeatures, str]]) -> dict[str, Any] | None:
        """Score the *current* model on a labelled set. None if untrained.

        Used to compare an incumbent against a retrain candidate on identical
        data, which is the only comparison that means anything.
        """
        if self.model is None or not samples:
            return None

        from sklearn.metrics import classification_report

        X = np.array([f.as_vector() for f, _ in samples])
        y = np.array([label for _, label in samples])

        report = classification_report(
            y, self.model.predict(X), output_dict=True, zero_division=0
        )
        return {
            "accuracy": report.get("accuracy", 0.0),
            "macro_f1": report.get("macro avg", {}).get("f1-score", 0.0),
            "n": len(samples),
        }

    def train(
        self,
        samples: list[tuple[ExceptionFeatures, str]],
        human_labelled: int | None = None,
        n_estimators: int = 200,
        test_fraction: float = 0.25,
        random_state: int = 42,
        promote_only_if_better: bool = False,
        tolerance: float = 0.01,
    ) -> dict[str, Any]:
        """Fit the forest and persist it with its metrics.

        `class_weight="balanced"` is not optional here: exception data is
        heavily imbalanced by nature, and an unweighted forest would learn to
        predict the majority category and score well while being useless.

        `promote_only_if_better` guards the automated retrain path (Sec. 11).
        build.md's snippet dumps the new model unconditionally, which means one
        bad round of labels silently replaces a good classifier and every
        subsequent suggestion degrades. With the guard on, the candidate is
        scored against the incumbent **on the same held-out set** and is only
        persisted if it does not regress by more than `tolerance`. Sec. 14's
        gate - "accuracy after retraining >= accuracy before" - is exactly this
        property.
        """
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.metrics import classification_report
        from sklearn.model_selection import train_test_split

        if len(samples) < 8:
            raise ValueError(
                f"need at least 8 labelled exceptions to train, got {len(samples)}"
            )

        distinct = {label for _, label in samples}
        if len(distinct) < 2:
            raise ValueError(
                f"need at least 2 distinct categories, got {sorted(distinct)}"
            )

        X = np.array([f.as_vector() for f, _ in samples])
        y = np.array([label for _, label in samples])

        stratify = y if min(np.bincount(np.unique(y, return_inverse=True)[1])) >= 2 else None
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=test_fraction, random_state=random_state, stratify=stratify
        )

        model = RandomForestClassifier(
            n_estimators=n_estimators,
            class_weight="balanced",
            random_state=random_state,
            n_jobs=-1,
        )
        model.fit(X_train, y_train)

        report = classification_report(
            y_test, model.predict(X_test), output_dict=True, zero_division=0
        )
        candidate_accuracy = report.get("accuracy", 0.0)
        candidate_f1 = report.get("macro avg", {}).get("f1-score", 0.0)

        # Score the incumbent on the SAME held-out rows before replacing it.
        incumbent = None
        if promote_only_if_better and self.model is not None:
            incumbent_report = classification_report(
                y_test, self.model.predict(X_test), output_dict=True, zero_division=0
            )
            incumbent = {
                "accuracy": incumbent_report.get("accuracy", 0.0),
                "macro_f1": incumbent_report.get("macro avg", {}).get("f1-score", 0.0),
            }

            if candidate_f1 < incumbent["macro_f1"] - tolerance:
                logger.warning(
                    "Rejecting retrain: candidate macro F1 %.3f is worse than the "
                    "incumbent's %.3f. The existing model is kept.",
                    candidate_f1, incumbent["macro_f1"],
                )
                return {
                    "promoted": False,
                    "reason": "candidate regressed against the incumbent",
                    "candidate": {"accuracy": candidate_accuracy, "macro_f1": candidate_f1},
                    "incumbent": incumbent,
                    "tolerance": tolerance,
                    "n_samples": len(samples),
                    "report": report,
                }

        self.model = model
        self.metadata = {
            "feature_names": FEATURE_NAMES,
            "feature_version": FEATURE_VERSION,
            "trained_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "n_samples": len(samples),
            # The number that says whether this model knows anything humans
            # taught it, or is only echoing the bootstrap rules.
            "human_labelled": human_labelled if human_labelled is not None else 0,
            "categories": sorted(distinct),
            "accuracy": candidate_accuracy,
            "macro_f1": candidate_f1,
        }

        self._persist()
        logger.info(
            "Trained on %d samples (%s human-labelled); accuracy %.3f, macro F1 %.3f",
            len(samples), self.metadata["human_labelled"],
            self.metadata["accuracy"], self.metadata["macro_f1"],
        )
        return {
            **self.metadata,
            "promoted": True,
            "incumbent": incumbent,
            "report": report,
        }

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"model": self.model, **self.metadata}, self.path)

    def status(self) -> dict[str, Any]:
        return {
            "trained": self.is_trained,
            "engine": "random_forest" if self.is_trained else "bootstrap",
            "model_path": str(self.path),
            "feature_names": list(FEATURE_NAMES),
            "feature_version": FEATURE_VERSION,
            **self.metadata,
        }


__all__ = [
    "ExceptionClassifier",
    "BootstrapClassifier",
    "Classification",
    "CATEGORIES",
    "MODEL_PATH",
    "MODEL_DIR",
]
