"""Stage 3 of 4 - checksum / signature verification (build.md Sec. 8).

"If the source provides a checksum/signature, verify it here to catch
in-transit corruption before commit."

Two things follow from that sentence and both matter:

* A record *without* a checksum passes. Absence is not failure - most feeds
  do not sign their payloads, and rejecting them would destroy the detection
  rate with false positives.
* A record *with* a checksum that does not match is corrupt and is
  quarantined. We never repair, recompute, or ignore a mismatch.

The same canonical fingerprint doubles as the Redis cache key (Sec. 8,
"keyed by a payload fingerprint"), so it is computed once here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

#: Fields carrying the checksum itself, excluded before digesting.
CHECKSUM_FIELDS = ("checksum", "signature", "hmac", "digest", "_checksum")

#: Field naming the algorithm, when the feed declares one.
ALGORITHM_FIELD = "checksum_algorithm"

SUPPORTED_ALGORITHMS = {"sha256", "sha512", "md5", "hmac-sha256"}


def canonical_json(payload: dict[str, Any]) -> str:
    """Stable serialisation: sorted keys, no insignificant whitespace.

    Two payloads that differ only in key order or spacing must produce the
    same string, or fingerprints would differ across producers and the cache
    would never hit.
    """
    stripped = {k: v for k, v in payload.items() if k not in CHECKSUM_FIELDS}
    return json.dumps(stripped, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(payload: dict[str, Any]) -> str:
    """sha256 of the canonical form. Cache key and quarantine correlation id."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _extract_checksum(payload: dict[str, Any]) -> tuple[str | None, str]:
    """Return (supplied checksum, algorithm). Algorithm defaults to sha256."""
    supplied = None
    for field in CHECKSUM_FIELDS:
        if payload.get(field):
            supplied = str(payload[field]).strip().lower()
            break

    algorithm = str(payload.get(ALGORITHM_FIELD, "sha256")).strip().lower()
    return supplied, algorithm


def verify_checksum(
    payload: dict[str, Any], secret: str | None = None
) -> tuple[bool, list[str]]:
    """Verify a supplied checksum against the payload.

    Returns (True, []) when the checksum matches *or* when none was supplied.
    Returns (False, [reasons]) on mismatch or an unusable algorithm.
    """
    supplied, algorithm = _extract_checksum(payload)

    if supplied is None:
        return True, []

    if algorithm not in SUPPORTED_ALGORITHMS:
        return False, [
            f"checksum: unsupported algorithm {algorithm!r} "
            f"(supported: {', '.join(sorted(SUPPORTED_ALGORITHMS))})"
        ]

    body = canonical_json(payload).encode("utf-8")

    if algorithm == "hmac-sha256":
        if not secret:
            # Cannot verify without the shared secret. Failing closed is the
            # only safe answer: passing it would let a corrupt signed record
            # through on a configuration mistake.
            return False, [
                "checksum: payload is HMAC-signed but no secret is configured "
                "for this source"
            ]
        computed = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    else:
        computed = hashlib.new(algorithm, body).hexdigest()

    if not hmac.compare_digest(computed, supplied):
        return False, [
            f"checksum: {algorithm} mismatch - "
            f"expected {computed[:16]}..., got {supplied[:16]}..."
        ]

    return True, []


__all__ = [
    "canonical_json",
    "fingerprint",
    "verify_checksum",
    "CHECKSUM_FIELDS",
    "SUPPORTED_ALGORITHMS",
]
