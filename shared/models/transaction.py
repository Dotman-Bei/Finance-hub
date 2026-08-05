"""Canonical Transaction model (build.md §6, Figure 3.4).

This Pydantic model doubles as the schema-validation stage of the ingestion
pipeline (§8) — `Transaction(**payload)` raising ValidationError *is* the
structural check that feeds the ≥98% detection-rate gate.
"""

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

# §3 gives MatchResult its own module; §6 shows it beside Transaction.
# It lives in match_result.py and is re-exported here so both import paths work.
from .match_result import MatchResult


class Transaction(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    id: UUID = Field(default_factory=uuid4)
    external_id: str | None = None
    source_type: str
    amount: Decimal
    currency: str = "USD"
    txn_date: date
    description: str | None = None
    reference_code: str | None = None

    @field_validator("amount")
    @classmethod
    def amount_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("amount must be positive")
        return v

    @field_validator("currency")
    @classmethod
    def currency_len(cls, v: str) -> str:
        if len(v) != 3:
            raise ValueError("currency must be a 3-letter ISO code")
        return v.upper()


__all__ = ["Transaction", "MatchResult"]
