from datetime import UTC, datetime

import pytest

from core.verification import VerificationEvidence, derive_verification


def evidence(**changes):
    return VerificationEvidence.model_validate(
        {
            "closure": "no_closure_signal",
            "closure_quote": None,
            "deadline": None,
            "deadline_kind": "none",
            "deadline_quote": None,
            "restricted": False,
            "restriction_quote": None,
            **changes,
        }
    )


@pytest.mark.parametrize(
    ("quote", "deadline"),
    [
        ("Applications will close on 30th September 2026.", "2026-09-30"),
        ("Recruiting for this role ends on September 23, 2026.", "2026-09-23"),
    ],
)
def test_future_deadline_does_not_close_posting(quote, deadline):
    facts = evidence(deadline=deadline, deadline_kind="hard", deadline_quote=quote)
    result = derive_verification(facts, quote, as_of=datetime(2026, 9, 16, tzinfo=UTC))
    assert result.closed is False


def test_same_facts_age_without_another_model_call():
    text = "Applications close September 23, 2026."
    facts = evidence(deadline="2026-09-23", deadline_kind="hard", deadline_quote=text)
    assert derive_verification(facts, text, as_of=datetime(2026, 9, 23, tzinfo=UTC)).closed is False
    assert derive_verification(facts, text, as_of=datetime(2026, 9, 24, tzinfo=UTC)).closed is None
    result = derive_verification(facts, text, as_of=datetime(2026, 9, 25, tzinfo=UTC))
    assert result.closed is True
    assert result.closed_reason == text


def test_minimum_application_window_is_not_a_deadline():
    text = "Applications for this job will be accepted at least until August 9, 2026."
    facts = evidence(deadline="2026-08-09", deadline_kind="minimum_window", deadline_quote=text)
    assert derive_verification(facts, text, as_of=datetime(2026, 9, 16, tzinfo=UTC)).closed is False


@pytest.mark.parametrize(
    ("quote", "deadline"),
    [
        ("Applications close September 23.", "2026-09-23"),
        ("Applications close September 23, 2026.", "2025-09-23"),
        ("Applications close 09/10/2026.", "2026-09-10"),
    ],
)
def test_unproven_date_abstains(quote, deadline):
    facts = evidence(deadline=deadline, deadline_kind="hard", deadline_quote=quote)
    assert derive_verification(facts, quote, as_of=datetime(2026, 10, 1, tzinfo=UTC)).closed is None


@pytest.mark.parametrize("closure", ["explicitly_closed", "unknown"])
def test_unquoted_closure_cannot_be_used(closure):
    facts = evidence(closure=closure, closure_quote="Position filled")
    assert (
        derive_verification(facts, "Apply now", as_of=datetime(2026, 9, 16, tzinfo=UTC)).closed
        is None
    )


def test_explicit_closure_overrides_future_deadline():
    text = "Position filled. Original deadline September 30, 2026."
    facts = evidence(
        closure="explicitly_closed",
        closure_quote="Position filled",
        deadline="2026-09-30",
        deadline_kind="hard",
        deadline_quote="Original deadline September 30, 2026.",
    )
    assert derive_verification(facts, text, as_of=datetime(2026, 9, 16, tzinfo=UTC)).closed is True


def test_unknown_page_and_unquoted_restriction_remain_unknown():
    facts = evidence(closure="unknown", restricted=True, restriction_quote="US citizens only")
    result = derive_verification(facts, "Access denied", as_of=datetime(2026, 9, 16, tzinfo=UTC))
    assert result.closed is None
    assert result.restricted is None


def test_restriction_evidence_is_independent_of_closure():
    text = "Position filled. Must hold an active security clearance."
    facts = evidence(
        closure="explicitly_closed",
        closure_quote="Position filled",
        restricted=True,
        restriction_quote="Must hold an active security clearance.",
    )
    result = derive_verification(facts, text, as_of=datetime(2026, 9, 16, tzinfo=UTC))
    assert result.closed is True
    assert result.restricted is True


def test_naive_evaluation_time_is_rejected():
    with pytest.raises(ValueError, match="timezone"):
        derive_verification(evidence(), "Apply", as_of=datetime(2026, 9, 16))
