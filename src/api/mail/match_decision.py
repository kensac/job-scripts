"""The recorded outcome of matching one mail message to an application.

A null application id records no attachment. The method distinguishes a
matcher miss, a deliberate refusal, a human detach, and an admin finding no
application. Keep that distinction in the persisted method string.
"""

from __future__ import annotations

from dataclasses import dataclass

# These strings are persisted in application_matches.method. Keep their
# spelling stable, including for decisions confirmed by a person.
EXACT_LINK = "exact_link"
ATS_COMPANY = "ats_company"
COMPANY_TITLE = "company_title"
ADJUDICATED = "adjudicated"
DERIVED = "derived"
MANUAL = "manual"
UNMATCHED = "unmatched"
NOT_AN_APPLICATION = "not_an_application"
DETACHED = "detached"
HUMAN_METHODS = frozenset({MANUAL, DETACHED})

_NULL_METHODS = frozenset({UNMATCHED, NOT_AN_APPLICATION, DETACHED, MANUAL})
_ATTACHED_FORBIDDEN_METHODS = frozenset({UNMATCHED, NOT_AN_APPLICATION, DETACHED})


@dataclass(frozen=True)
class MatchDecision:
    application_id: int | None
    method: str
    confidence: str
    rationale: str

    def __post_init__(self) -> None:
        if self.application_id is None and self.method not in _NULL_METHODS:
            raise ValueError("a null application id needs a no-attachment method")
        if self.application_id is not None and self.method in _ATTACHED_FORBIDDEN_METHODS:
            raise ValueError("an attached application needs an attachment method")

    @classmethod
    def unmatched(cls, rationale: str) -> MatchDecision:
        return cls(None, UNMATCHED, "none", rationale)

    @classmethod
    def not_an_application(cls, rationale: str, *, confidence: str = "none") -> MatchDecision:
        return cls(None, NOT_AN_APPLICATION, confidence, rationale)

    @classmethod
    def detached(cls, rationale: str) -> MatchDecision:
        return cls(None, DETACHED, "none", rationale)

    def repeats(self, application_id: int | None, method: str, confidence: str | None) -> bool:
        """A repeated machine outcome ignores rationale, which is only prose."""
        return (
            self.application_id == application_id
            and self.method == method
            and self.confidence == confidence
        )
