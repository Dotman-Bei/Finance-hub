"""Shared models — the one definition of every entity, imported by all services.

Pydantic models carry data across service boundaries; the SQLAlchemy models in
`orm` map the same entities onto db/schema.sql. Both are defined here so the
two can never drift apart.
"""

from .enums import (
    EntryType,
    ExceptionCategory,
    ExceptionState,
    MatchStatus,
    MatchType,
    SourceType,
    ValidationStage,
    ValidationState,
)
from .match_result import MatchResult
from .transaction import Transaction

__all__ = [
    "Transaction",
    "MatchResult",
    "MatchStatus",
    "MatchType",
    "ExceptionCategory",
    "ExceptionState",
    "ValidationState",
    "SourceType",
    "ValidationStage",
    "EntryType",
]
