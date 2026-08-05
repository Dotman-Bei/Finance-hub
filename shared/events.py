"""Cross-service event channel (build.md Sec. 12).

    "WS /ws/exceptions - push new exceptions live so teams don't manually
     refresh"

For the dashboard to show work arriving, whichever service created that work
has to say so. Each subsystem publishes what it did; reporting_api subscribes
and relays to connected browsers.

One Redis channel with typed messages, rather than a channel per event: a
subscriber wanting everything would otherwise need to track a growing list of
channel names, and the WebSocket relay wants exactly that.

**Delivery is best-effort and that is a deliberate choice.** Redis pub/sub is
at-most-once: an event published while no subscriber is connected is gone. That
is correct for a live-notification feature - the durable record is Postgres, and
the dashboard fetches it on load. What must never happen is the relay inventing
an event to fill a gap, so a publish failure is logged and dropped, never
substituted.

The message shape matches what the dashboard's `useWebSocket` hook already
parses: `{"type": ..., "payload": {...}}`.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

import redis

from shared.config import settings

logger = logging.getLogger(__name__)

#: Every service publishes here; reporting_api is the only subscriber.
EVENT_CHANNEL = "financehub:events"


class EventType:
    """The message types the dashboard knows how to render."""

    EXCEPTION_CREATED = "exception.created"
    EXCEPTION_SUGGESTED = "exception.suggested"
    EXCEPTION_RESOLVED = "exception.resolved"
    VALIDATION_QUARANTINED = "validation.quarantined"
    RECONCILIATION_COMPLETED = "reconciliation.completed"


class EventPublisher:
    """Fire-and-forget publisher. A Redis outage degrades notifications only."""

    def __init__(self, url: str | None = None, channel: str = EVENT_CHANNEL):
        self.url = url or settings.redis_url
        self.channel = channel
        self._client: redis.Redis | None = None
        self._warned = False
        self.published = 0
        self.dropped = 0

    @property
    def client(self) -> redis.Redis | None:
        if self._client is None:
            try:
                self._client = redis.Redis.from_url(
                    self.url,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                self._client.ping()
                self._warned = False
            except Exception as exc:
                if not self._warned:
                    logger.warning(
                        "Redis unavailable at %s (%s). Live notifications are "
                        "degraded; Postgres remains the durable record.",
                        self.url, exc,
                    )
                    self._warned = True
                self._client = None
        return self._client

    def publish(self, event_type: str, payload: dict[str, Any]) -> bool:
        """Publish one event. False means it was dropped, never substituted."""
        client = self.client
        message = {
            "type": event_type,
            "payload": payload,
            "emitted_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

        if client is None:
            self.dropped += 1
            return False

        try:
            client.publish(self.channel, json.dumps(message, default=str))
            self.published += 1
            return True
        except Exception as exc:
            logger.debug("Event publish failed (%s)", exc)
            self._client = None
            self.dropped += 1
            return False

    def publish_many(self, event_type: str, payloads: list[dict[str, Any]]) -> int:
        return sum(1 for payload in payloads if self.publish(event_type, payload))

    def is_available(self) -> bool:
        return self.client is not None

    def stats(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "available": self.is_available(),
            "published": self.published,
            "dropped": self.dropped,
        }

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None


#: Process-wide publisher. Services import this rather than building their own,
#: so every event lands on the same channel in the same shape.
publisher = EventPublisher()


def exception_created(
    exception_id: Any,
    transaction: dict[str, Any],
    reason: str = "",
    best_confidence: float | None = None,
) -> dict[str, Any]:
    """Payload for a newly opened exception.

    Nests `transaction` because that is the shape the dashboard's
    ExceptionPanel renders and `normalizeException` expects.
    """
    return {
        "id": str(exception_id),
        "transaction_id": str(transaction.get("id")),
        "state": "OPEN",
        "category": None,
        "classifier_confidence": None,
        "suggested_resolution": None,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "transaction": {
            "id": str(transaction.get("id")),
            "external_id": transaction.get("external_id"),
            "source_type": transaction.get("source_type"),
            "amount": float(transaction["amount"]) if transaction.get("amount") is not None else 0.0,
            "currency": transaction.get("currency", "USD"),
            "txn_date": str(transaction.get("txn_date")),
            "description": transaction.get("description"),
            "reference_code": transaction.get("reference_code"),
        },
        "matching_engine": {
            "reason": reason,
            "best_confidence": best_confidence,
        },
    }


__all__ = [
    "EventPublisher",
    "EventType",
    "EVENT_CHANNEL",
    "publisher",
    "exception_created",
]
