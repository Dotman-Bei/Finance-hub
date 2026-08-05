"""Four-stage orchestration (build.md Sec. 8).

    ETL ingestion -> schema validation -> business rules -> checksum verify
        -> INSERT into transactions + VALIDATIONLOGS(PASSED)
             |
             +-- failure at any stage -> quarantine + VALIDATIONLOGS(QUARANTINED)

Deliberately free of I/O. `validate_batch` touches no database, no Kafka and
(optionally) no Redis, which is what lets tests/test_detection_rate.py measure
the >=98% release gate directly against the real pipeline rather than against
a stand-in. Persistence lives in quarantine.py, transport in ingestion.py.

Stage order follows Sec. 8 exactly. Checksum runs third, after business rules,
even though verifying corruption first would fail marginally faster - the
sequence is what the thesis specifies.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from shared.models.enums import ValidationStage, ValidationState
from shared.models.transaction import Transaction

from .cache import ValidationCache
from .checksum import fingerprint, verify_checksum
from .rule_processor import RuleProcessor
from .schema_validator import validate_schema_batch

logger = logging.getLogger(__name__)


@dataclass
class RecordDecision:
    """What the pipeline concluded about one record, and why."""

    index: int
    payload: dict[str, Any]
    fingerprint: str
    status: ValidationState
    stage: ValidationStage | None = None
    violations: list[str] = field(default_factory=list)
    transaction: Transaction | None = None
    from_cache: bool = False

    @property
    def passed(self) -> bool:
        return self.status is ValidationState.PASSED

    @property
    def quarantined(self) -> bool:
        return self.status is ValidationState.QUARANTINED

    def to_cache_entry(self) -> dict[str, Any]:
        """Only the verdict is cached - never the parsed Transaction, whose
        generated id must stay unique per ingestion."""
        return {
            "status": self.status.value,
            "stage": self.stage.value if self.stage else None,
            "violations": self.violations,
        }


@dataclass
class BatchResult:
    decisions: list[RecordDecision]

    @property
    def passed(self) -> list[RecordDecision]:
        return [d for d in self.decisions if d.passed]

    @property
    def quarantined(self) -> list[RecordDecision]:
        return [d for d in self.decisions if d.quarantined]

    @property
    def total(self) -> int:
        return len(self.decisions)

    @property
    def cache_hits(self) -> int:
        return sum(1 for d in self.decisions if d.from_cache)

    def by_stage(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for decision in self.quarantined:
            key = decision.stage.value if decision.stage else "unknown"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def summary(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": len(self.passed),
            "quarantined": len(self.quarantined),
            "cache_hits": self.cache_hits,
            "quarantined_by_stage": self.by_stage(),
        }


class ValidationPipeline:
    """Runs the four stages over a batch of raw payloads."""

    def __init__(
        self,
        rule_processor: RuleProcessor | None = None,
        cache: ValidationCache | None = None,
        hmac_secret: str | None = None,
    ) -> None:
        # GE context construction is slow, so it is built once and reused.
        self.rules = rule_processor or RuleProcessor()
        self.cache = cache
        self.hmac_secret = hmac_secret

    def validate_batch(
        self,
        payloads: list[dict[str, Any]],
        as_of: dt.date | None = None,
    ) -> BatchResult:
        if not payloads:
            return BatchResult(decisions=[])

        as_of = as_of or dt.date.today()
        decisions: dict[int, RecordDecision] = {}
        fingerprints = [fingerprint(p) if isinstance(p, dict) else "" for p in payloads]

        # ── Stage 0: cache lookup ────────────────────────────────────────
        pending: list[int] = []
        for index, payload in enumerate(payloads):
            cached = self._cache_lookup(fingerprints[index])
            if cached is None:
                pending.append(index)
                continue
            decisions[index] = RecordDecision(
                index=index,
                payload=payload if isinstance(payload, dict) else {"_raw": payload},
                fingerprint=fingerprints[index],
                status=ValidationState(cached["status"]),
                stage=ValidationStage(cached["stage"]) if cached.get("stage") else None,
                violations=list(cached.get("violations") or []),
                from_cache=True,
            )

        # ── Stage 1: schema (Pydantic) ───────────────────────────────────
        candidate_payloads = [payloads[i] for i in pending]
        parsed, schema_failures = validate_schema_batch(candidate_payloads)

        for offset, violations in schema_failures.items():
            index = pending[offset]
            decisions[index] = self._reject(
                index, payloads[index], fingerprints[index],
                ValidationStage.SCHEMA, violations,
            )

        survivors = [pending[offset] for offset in sorted(parsed)]
        transactions = {pending[offset]: txn for offset, txn in parsed.items()}

        # ── Stage 2: business rules (Great Expectations) ─────────────────
        if survivors:
            frame = pd.DataFrame(
                [transactions[i].model_dump() for i in survivors]
            )
            try:
                rule_failures = self.rules.validate_frame(frame, as_of=as_of)
            except Exception:
                # A rule engine that cannot run must not silently pass records.
                # Fail the batch loudly - Sec. 8 has nothing reach the DB
                # unvalidated.
                logger.exception("Great Expectations run failed for batch of %d", len(survivors))
                raise

            still_alive: list[int] = []
            for position, index in enumerate(survivors):
                violations = rule_failures.get(position)
                if violations:
                    decisions[index] = self._reject(
                        index, payloads[index], fingerprints[index],
                        ValidationStage.BUSINESS_RULE, violations,
                    )
                else:
                    still_alive.append(index)
            survivors = still_alive

        # ── Stage 3: checksum ────────────────────────────────────────────
        for index in survivors:
            ok, violations = verify_checksum(payloads[index], secret=self.hmac_secret)
            if ok:
                decisions[index] = RecordDecision(
                    index=index,
                    payload=payloads[index],
                    fingerprint=fingerprints[index],
                    status=ValidationState.PASSED,
                    transaction=transactions[index],
                )
            else:
                decisions[index] = self._reject(
                    index, payloads[index], fingerprints[index],
                    ValidationStage.CHECKSUM, violations,
                )

        ordered = [decisions[i] for i in range(len(payloads))]
        self._cache_store(ordered)
        return BatchResult(decisions=ordered)

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _reject(
        index: int,
        payload: Any,
        payload_fingerprint: str,
        stage: ValidationStage,
        violations: list[str],
    ) -> RecordDecision:
        return RecordDecision(
            index=index,
            payload=payload if isinstance(payload, dict) else {"_raw": repr(payload)},
            fingerprint=payload_fingerprint,
            status=ValidationState.QUARANTINED,
            stage=stage,
            violations=violations,
        )

    def _cache_lookup(self, payload_fingerprint: str) -> dict[str, Any] | None:
        if self.cache is None or not payload_fingerprint:
            return None
        try:
            return self.cache.get(payload_fingerprint)
        except Exception:
            logger.debug("Cache lookup failed; recomputing", exc_info=True)
            return None

    def _cache_store(self, decisions: list[RecordDecision]) -> None:
        if self.cache is None:
            return
        for decision in decisions:
            if decision.from_cache or not decision.fingerprint:
                continue
            try:
                self.cache.set(decision.fingerprint, decision.to_cache_entry())
            except Exception:
                logger.debug("Cache write failed", exc_info=True)


__all__ = ["ValidationPipeline", "BatchResult", "RecordDecision"]
