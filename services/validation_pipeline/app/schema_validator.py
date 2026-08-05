"""Stage 1 of 4 - structural schema validation (build.md Sec. 8).

`validate_schema` is build.md's function verbatim. Everything else here is
batch plumbing around it.

This stage decides *structural* compliance only: is every required field
present, is each one the right type, does it satisfy the field-level
constraints on the canonical model. Logical checks (future dates, unknown
currencies) belong to stage 2.
"""

from __future__ import annotations

from pydantic import ValidationError

from shared.models.transaction import Transaction


def validate_schema(payload: dict) -> tuple[Transaction | None, list[str]]:
    """Return the parsed Transaction, or None plus the reasons it failed."""
    try:
        return Transaction(**payload), []
    except ValidationError as e:
        return None, [f"{err['loc']}: {err['msg']}" for err in e.errors()]


def validate_schema_batch(
    payloads: list[dict],
) -> tuple[dict[int, Transaction], dict[int, list[str]]]:
    """Run stage 1 across a batch.

    Returns (index -> Transaction) for records that passed and
    (index -> violations) for those that did not. Indices are positions in
    `payloads`, so the caller can keep every record aligned with its original
    source row through all four stages.
    """
    passed: dict[int, Transaction] = {}
    failed: dict[int, list[str]] = {}

    for index, payload in enumerate(payloads):
        if not isinstance(payload, dict):
            failed[index] = [f"payload: expected an object, got {type(payload).__name__}"]
            continue

        transaction, violations = validate_schema(payload)
        if transaction is None:
            failed[index] = violations
        else:
            passed[index] = transaction

    return passed, failed


__all__ = ["validate_schema", "validate_schema_batch"]
