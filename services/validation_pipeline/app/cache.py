"""Redis validation-result cache (build.md Sec. 8).

"Cache frequently repeated schema/rule decisions in Redis keyed by a payload
fingerprint to cut recomputation on high-velocity streams."

The cache stores *decisions this pipeline already computed*, keyed by the
sha256 of the payload. It never invents a verdict: a miss, a Redis outage, or
a malformed cache entry all mean "recompute", never "assume pass". Redis being
down degrades throughput, never correctness.

The dashboard alert channel lives here too, because it is the same connection.
Sec. 8 requires an alert on quarantine; reporting_api's WS endpoint (Sec. 12)
subscribes to it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import redis

from shared.config import settings

logger = logging.getLogger(__name__)

KEY_PREFIX = "financehub:validation:"
ALERT_CHANNEL = "financehub:alerts:quarantine"

#: Decisions expire so a rule change cannot be masked indefinitely by a warm
#: cache. One hour is short enough to bound staleness, long enough to absorb
#: the duplicate bursts a replayed Kafka partition produces.
DEFAULT_TTL_SECONDS = 3600


class ValidationCache:
    """Thin Redis wrapper that fails open to "not cached"."""

    def __init__(self, url: str | None = None, ttl: int = DEFAULT_TTL_SECONDS) -> None:
        self.url = url or settings.redis_url
        self.ttl = ttl
        self._client: redis.Redis | None = None
        self._unavailable_logged = False

    # ── connection ───────────────────────────────────────────────────────

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
                self._unavailable_logged = False
            except Exception as exc:
                self._note_unavailable(exc)
                self._client = None
        return self._client

    def _note_unavailable(self, exc: Exception) -> None:
        # Log once per outage, not once per record - a high-velocity stream
        # would otherwise bury every other log line.
        if not self._unavailable_logged:
            logger.warning(
                "Redis unavailable at %s (%s). Validation continues uncached.",
                self.url,
                exc,
            )
            self._unavailable_logged = True

    def is_available(self) -> bool:
        return self.client is not None

    # ── decisions ────────────────────────────────────────────────────────

    def get(self, fingerprint: str) -> dict[str, Any] | None:
        """Return a previously computed decision, or None to recompute."""
        client = self.client
        if client is None:
            return None

        try:
            raw = client.get(KEY_PREFIX + fingerprint)
        except Exception as exc:
            self._note_unavailable(exc)
            self._client = None
            return None

        if raw is None:
            return None

        try:
            cached = json.loads(raw)
        except json.JSONDecodeError:
            # Corrupt entry: drop it and recompute rather than trust it.
            logger.warning("Discarding malformed cache entry %s", fingerprint[:12])
            self.delete(fingerprint)
            return None

        if not isinstance(cached, dict) or "status" not in cached:
            self.delete(fingerprint)
            return None

        return cached

    def set(self, fingerprint: str, decision: dict[str, Any]) -> bool:
        client = self.client
        if client is None:
            return False
        try:
            client.setex(KEY_PREFIX + fingerprint, self.ttl, json.dumps(decision))
            return True
        except Exception as exc:
            self._note_unavailable(exc)
            self._client = None
            return False

    def delete(self, fingerprint: str) -> None:
        client = self.client
        if client is None:
            return
        try:
            client.delete(KEY_PREFIX + fingerprint)
        except Exception:
            pass

    # ── alerts ───────────────────────────────────────────────────────────

    def publish_quarantine_alert(self, alert: dict[str, Any]) -> bool:
        """Sec. 8: "emit a dashboard alert" when a record is quarantined."""
        client = self.client
        if client is None:
            return False
        try:
            client.publish(ALERT_CHANNEL, json.dumps(alert, default=str))
            return True
        except Exception as exc:
            self._note_unavailable(exc)
            self._client = None
            return False

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None


__all__ = ["ValidationCache", "KEY_PREFIX", "ALERT_CHANNEL", "DEFAULT_TTL_SECONDS"]
