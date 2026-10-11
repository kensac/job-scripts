"""Match decisions keep attachment state and persisted method in agreement."""

import pytest

from api.mail.match_decision import (
    ATS_COMPANY,
    DERIVED,
    DETACHED,
    MANUAL,
    NOT_AN_APPLICATION,
    UNMATCHED,
    MatchDecision,
)


@pytest.mark.parametrize(
    ("application_id", "method"),
    [
        (None, ATS_COMPANY),
        (None, DERIVED),
        (42, UNMATCHED),
        (42, NOT_AN_APPLICATION),
        (42, DETACHED),
    ],
)
def test_decision_refuses_contradictory_attachment_state(application_id, method):
    with pytest.raises(ValueError):
        MatchDecision(application_id, method, "high", "decision")


def test_decision_preserves_an_existing_attachment_method():
    decision = MatchDecision(42, "legacy_method", "high", "confirmed")
    assert decision.method == "legacy_method"


def test_decision_keeps_persisted_fields_and_human_confirmation_method():
    attached = MatchDecision(42, ATS_COMPANY, "medium", "linked employer")
    confirmed = MatchDecision(42, attached.method, attached.confidence, "confirmed")
    manual = MatchDecision(42, MANUAL, "high", "person chose this")
    manual_without_application = MatchDecision(None, MANUAL, "none", "none found")
    refused = MatchDecision(None, NOT_AN_APPLICATION, "high", "person refused this")

    assert (confirmed.application_id, confirmed.method, confirmed.confidence) == (
        42,
        ATS_COMPANY,
        "medium",
    )
    assert manual.method == "manual"
    assert manual_without_application.application_id is None
    assert refused.application_id is None and refused.method == "not_an_application"


def test_repeated_outcome_ignores_rationale_but_keeps_method_and_confidence():
    decision = MatchDecision.unmatched("first sweep")
    assert decision.repeats(None, UNMATCHED, "none")
    assert not decision.repeats(None, NOT_AN_APPLICATION, "none")
    assert not decision.repeats(None, UNMATCHED, "high")
    assert MatchDecision.detached("person rejected").method == DETACHED
