"""Stage 0 - ETL ingestion (build.md Sec. 8).

    "Kafka consumer reads raw_transactions, buffers to a staging structure,
     hands each payload to the validator. Accept CSV and JSON payloads;
     normalize with Pandas to the canonical schema before validation."

Normalisation is deliberately lossless and non-repairing. It renames known
column aliases, trims whitespace and drops empty strings to null - shape work
only. It never invents a missing field, coerces an unparseable amount to zero,
or substitutes a default currency. A record that arrives broken must stay
broken so the validator can quarantine it; a helpful ETL layer would silently
destroy the detection rate.
"""

from __future__ import annotations

import io
import json
import logging
import threading
import time
from collections.abc import Iterator
from typing import Any

import pandas as pd

from shared.config import settings

logger = logging.getLogger(__name__)

#: Aliases seen across bank, gateway and ERP feeds -> canonical field name.
#: Renaming is safe; anything requiring a value judgement is not done here.
COLUMN_ALIASES = {
    "transaction_id": "external_id",
    "txn_id": "external_id",
    "id": "external_id",
    "reference": "reference_code",
    "ref": "reference_code",
    "ref_code": "reference_code",
    "value": "amount",
    "transaction_amount": "amount",
    "amt": "amount",
    "ccy": "currency",
    "currency_code": "currency",
    "date": "txn_date",
    "transaction_date": "txn_date",
    "value_date": "txn_date",
    "posted_date": "txn_date",
    "narrative": "description",
    "memo": "description",
    "details": "description",
    "source": "source_type",
    "channel": "source_type",
}

CANONICAL_FIELDS = (
    "external_id",
    "source_type",
    "amount",
    "currency",
    "txn_date",
    "description",
    "reference_code",
)

PASSTHROUGH_FIELDS = ("checksum", "signature", "checksum_algorithm", "hmac", "digest")


def _decode(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        # errors="replace" rather than "ignore": a mangled byte becomes a
        # visible replacement char that fails validation, instead of silently
        # vanishing and turning a corrupt record into a clean-looking one.
        return raw.decode("utf-8", errors="replace")
    return raw


def looks_like_csv(text: str) -> bool:
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        return False
    first_line = stripped.splitlines()[0] if stripped.splitlines() else ""
    return "," in first_line or ";" in first_line


def normalize(raw: bytes | str | dict | list) -> list[dict[str, Any]]:
    """Turn one Kafka message into a list of canonical-shaped dicts."""
    if isinstance(raw, dict):
        return _normalize_frame(pd.DataFrame([raw]))
    if isinstance(raw, list):
        return _normalize_frame(pd.DataFrame(raw))

    text = _decode(raw).strip()
    if not text:
        return []

    if looks_like_csv(text):
        try:
            frame = pd.read_csv(io.StringIO(text))
        except Exception as exc:
            logger.warning("Unparseable CSV message (%s); passing through raw", exc)
            return [{"_unparseable": text[:2000], "_parse_error": str(exc)}]
        return _normalize_frame(frame)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Unparseable JSON message (%s); passing through raw", exc)
        # Handed on rather than dropped: the validator quarantines it and the
        # payload survives in the quarantine table for diagnosis.
        return [{"_unparseable": text[:2000], "_parse_error": str(exc)}]

    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return [{"_unparseable": text[:2000], "_parse_error": "not an object or array"}]

    return _normalize_frame(pd.DataFrame(parsed))


def _normalize_frame(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Pandas normalisation to the canonical schema. Shape only."""
    if frame.empty:
        return []

    frame = frame.copy()
    frame.columns = [str(c).strip().lower().replace(" ", "_") for c in frame.columns]

    # Only rename an alias when the canonical name is not already present, so a
    # feed carrying both never has one silently overwrite the other.
    renames = {
        alias: canonical
        for alias, canonical in COLUMN_ALIASES.items()
        if alias in frame.columns and canonical not in frame.columns
    }
    frame = frame.rename(columns=renames)

    # Applied to every column, never gated on dtype. pandas >= 2.2 may infer
    # `str` rather than `object` for text columns, so a dtype == object guard
    # silently skips exactly the columns that need cleaning. _clean_string is
    # a no-op on non-strings, so running it everywhere is both cheaper to
    # reason about and correct across pandas versions.
    for column in frame.columns:
        frame[column] = frame[column].map(_clean_string)

    keep = [
        c for c in frame.columns
        if c in CANONICAL_FIELDS or c in PASSTHROUGH_FIELDS or c.startswith("_")
    ]
    records = frame[keep].to_dict(orient="records")
    return [{k: _scalar_or_none(v) for k, v in record.items()} for record in records]


def _clean_string(value: Any) -> Any:
    """Trim strings; treat "" as absence.

    An empty CSV cell means "no value", not "the empty string". Keeping them
    distinct is what lets the not-null expectations in stage 2 fire instead of
    passing a blank through as if it were data.
    """
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _scalar_or_none(value: Any) -> Any:
    """Normalise pandas' missing markers (NaN, NaT, None) to a plain None.

    pd.isna returns an *array* for list-likes, so those are returned untouched
    rather than being tested for truthiness.
    """
    if isinstance(value, (list, dict, set, tuple)):
        return value
    try:
        return None if pd.isna(value) else value
    except (TypeError, ValueError):
        return value


class StagingBuffer:
    """The "staging structure" of Sec. 8.

    Accumulates messages and releases them as a batch once it is full or has
    been waiting too long, so a low-traffic topic still gets flushed promptly.
    """

    def __init__(self, batch_size: int = 500, max_wait_seconds: float = 5.0) -> None:
        self.batch_size = batch_size
        self.max_wait_seconds = max_wait_seconds
        self._records: list[dict[str, Any]] = []
        self._first_added_at: float | None = None
        self._lock = threading.Lock()

    def add(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        with self._lock:
            if self._first_added_at is None:
                self._first_added_at = time.monotonic()
            self._records.extend(records)

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._records)

    def should_flush(self) -> bool:
        with self._lock:
            if not self._records:
                return False
            if len(self._records) >= self.batch_size:
                return True
            waited = time.monotonic() - (self._first_added_at or time.monotonic())
            return waited >= self.max_wait_seconds

    def drain(self) -> list[dict[str, Any]]:
        with self._lock:
            records, self._records = self._records, []
            self._first_added_at = None
            return records


class KafkaIngestor:
    """Consumes `raw_transactions` and hands batches to a callback.

    The callback runs the pipeline and persists. Offsets are committed only
    after it returns, so a crash mid-batch replays those messages rather than
    losing them. Duplicate work is harmless - the fingerprint cache absorbs it.
    """

    def __init__(
        self,
        on_batch,
        topic: str | None = None,
        brokers: str | None = None,
        group_id: str = "financehub-validation-pipeline",
        batch_size: int = 500,
        max_wait_seconds: float = 5.0,
    ) -> None:
        self.on_batch = on_batch
        self.topic = topic or settings.kafka_topic_raw
        self.brokers = brokers or settings.kafka_broker
        self.group_id = group_id
        self.buffer = StagingBuffer(batch_size, max_wait_seconds)

        self._consumer = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.messages_consumed = 0
        self.batches_processed = 0
        self.last_error: str | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    def _connect(self):
        from kafka import KafkaConsumer

        return KafkaConsumer(
            self.topic,
            bootstrap_servers=self.brokers.split(","),
            group_id=self.group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            consumer_timeout_ms=1000,
            value_deserializer=lambda v: v,
        )

    def start(self) -> bool:
        """Start the consumer thread. False if Kafka is unreachable.

        A failure here is reported, never swallowed: /health turns unhealthy
        and the operator sees the reason.
        """
        try:
            self._consumer = self._connect()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.error("Kafka consumer could not start (%s)", self.last_error)
            return False

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="kafka-ingestor", daemon=True)
        self._thread.start()
        logger.info("Consuming %s from %s", self.topic, self.brokers)
        return True

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if self._consumer is not None:
            try:
                self._consumer.close()
            finally:
                self._consumer = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── loop ─────────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                for message in self._poll():
                    self.messages_consumed += 1
                    self.buffer.add(normalize(message))
                    if self.buffer.should_flush():
                        self._flush()
                if self.buffer.should_flush():
                    self._flush()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Ingestion loop error")
                time.sleep(2)

        if self.buffer.size:
            self._flush()

    def _poll(self) -> Iterator[bytes]:
        if self._consumer is None:
            return
        for message in self._consumer:
            yield message.value
            if self._stop.is_set():
                break

    def _flush(self) -> None:
        records = self.buffer.drain()
        if not records:
            return
        try:
            self.on_batch(records)
            self.batches_processed += 1
            if self._consumer is not None:
                self._consumer.commit()
        except Exception as exc:
            # Offsets stay uncommitted so the batch is redelivered.
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("Batch of %d failed; offsets not committed", len(records))

    def stats(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "topic": self.topic,
            "brokers": self.brokers,
            "messages_consumed": self.messages_consumed,
            "batches_processed": self.batches_processed,
            "buffered": self.buffer.size,
            "last_error": self.last_error,
        }


__all__ = ["normalize", "looks_like_csv", "StagingBuffer", "KafkaIngestor", "COLUMN_ALIASES"]
