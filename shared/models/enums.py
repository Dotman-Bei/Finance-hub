"""Canonical enumerations (build.md §6).

These mirror the PostgreSQL types in db/schema.sql one-for-one. Import from
here everywhere — never redeclare a category list in a service, or the schema
drifts. The frontend keeps a parallel copy in src/lib/constants.js.
"""

from enum import Enum


class MatchStatus(str, Enum):
    MATCHED = "MATCHED"
    UNMATCHED = "UNMATCHED"


class MatchType(str, Enum):
    RULE = "RULE"
    ML = "ML"


class ExceptionCategory(str, Enum):
    PARTIAL_PAYMENT = "PARTIAL_PAYMENT"
    SPLIT_SETTLEMENT = "SPLIT_SETTLEMENT"
    MISSING_REFERENCE_CODE = "MISSING_REFERENCE_CODE"
    TIMING_DIFFERENCE = "TIMING_DIFFERENCE"


# §6 lists only the three above, but db/schema.sql declares two further types.
# Services need them to read and write those columns, so they are defined here
# rather than being reinvented per service.


class ExceptionState(str, Enum):
    OPEN = "OPEN"
    SUGGESTED = "SUGGESTED"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"


class ValidationState(str, Enum):
    PASSED = "PASSED"
    QUARANTINED = "QUARANTINED"


class SourceType(str, Enum):
    """`transactions.source_type` is free TEXT in the DDL; these are the three
    sources build.md §5 names in its column comment."""

    BANK_API = "bank_api"
    PAYMENT_GATEWAY = "payment_gateway"
    ERP = "erp"


class ValidationStage(str, Enum):
    """`validationlogs.stage` — the four sequential stages of §8."""

    SCHEMA = "schema"
    BUSINESS_RULE = "business_rule"
    CHECKSUM = "checksum"


class EntryType(str, Enum):
    """`ledgerentries.entry_type`."""

    DEBIT = "debit"
    CREDIT = "credit"


__all__ = [
    "MatchStatus",
    "MatchType",
    "ExceptionCategory",
    "ExceptionState",
    "ValidationState",
    "SourceType",
    "ValidationStage",
    "EntryType",
]
