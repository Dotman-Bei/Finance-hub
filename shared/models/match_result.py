"""Canonical MatchResult model (build.md §6, Figure 3.4).

The matching engine (§9) returns one of these per candidate pair. `status` and
`match_type` are typed against the enums rather than left as bare strings, so a
typo cannot reach `matchedrecords`.
"""

from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from .enums import MatchStatus, MatchType


class MatchResult(BaseModel):
    transaction_id: UUID
    counterpart_id: UUID | None = None
    status: MatchStatus
    match_type: MatchType | None = None
    confidence_score: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def matched_pairs_are_complete(self) -> "MatchResult":
        """A MATCHED result must name its counterpart and which layer found it;
        an UNMATCHED one must not claim either. This is what keeps the
        exception queue and matchedrecords mutually exclusive (§9)."""
        if self.status is MatchStatus.MATCHED:
            if self.counterpart_id is None:
                raise ValueError("a MATCHED result requires counterpart_id")
            if self.match_type is None:
                raise ValueError("a MATCHED result requires match_type (RULE or ML)")
        else:
            if self.counterpart_id is not None:
                raise ValueError("an UNMATCHED result must not carry a counterpart_id")
        return self

    def persists(self, threshold: float) -> bool:
        """True when this pair clears MATCH_CONFIDENCE_THRESHOLD and belongs in
        `matchedrecords`; False sends it to `exceptionqueue` (§9)."""
        return self.status is MatchStatus.MATCHED and self.confidence_score >= threshold


__all__ = ["MatchResult"]
