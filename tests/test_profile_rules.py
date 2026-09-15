import pytest
from pydantic import ValidationError

from core.job_profile import JobProfileAnswer
from core.profile_rules import ProfileRules, evaluate


def profile(**updates: object) -> JobProfileAnswer:
    return JobProfileAnswer.model_validate(
        {
            "primary_role_family": "engineering",
            "role_tracks": ["backend", "infrastructure"],
            "career_stage": "entry",
            "employment_type": "full_time",
            "organization_sector": "unknown",
            "people_manager": False,
            "company_selectivity": "unknown",
            "role_selectivity": "unknown",
            **updates,
        }
    )


def test_all_explicit_constraints_match_without_unrelated_facts():
    rules = ProfileRules(
        allowed_role_families=("engineering", "data"),
        allowed_role_tracks=("backend",),
        allowed_career_stages=("entry",),
        allowed_employment_types=("full_time",),
        people_manager=False,
    )
    assert evaluate(profile(), rules).outcome == "accept"


@pytest.mark.parametrize(
    ("constraint", "allowed", "reason"),
    [
        ("allowed_role_families", ["product"], "primary_role_family_mismatch"),
        ("allowed_role_tracks", ["frontend"], "role_tracks_mismatch"),
        ("allowed_career_stages", ["senior"], "career_stage_mismatch"),
        ("allowed_employment_types", ["internship"], "employment_type_mismatch"),
        ("people_manager", True, "people_manager_mismatch"),
    ],
)
def test_each_known_mismatch_rejects(constraint, allowed, reason):
    result = evaluate(profile(), ProfileRules.model_validate({constraint: allowed}))
    assert result.outcome == "reject"
    assert result.reason == reason


@pytest.mark.parametrize(
    ("field", "value", "constraint", "allowed"),
    [
        ("primary_role_family", "unknown", "allowed_role_families", ["engineering"]),
        ("career_stage", "unknown", "allowed_career_stages", ["entry"]),
        ("employment_type", "unknown", "allowed_employment_types", ["full_time"]),
        ("people_manager", None, "people_manager", False),
        ("role_tracks", [], "allowed_role_tracks", ["backend"]),
        ("role_tracks", ["unknown"], "allowed_role_tracks", ["backend"]),
        ("role_tracks", ["frontend", "unknown"], "allowed_role_tracks", ["backend"]),
    ],
)
def test_missing_relevant_evidence_abstains(field, value, constraint, allowed):
    result = evaluate(profile(**{field: value}), ProfileRules.model_validate({constraint: allowed}))
    assert result.outcome == "abstain"
    assert result.reason == f"unknown:{field}"


def test_known_track_match_is_sufficient_with_unknown_second_track():
    result = evaluate(
        profile(role_tracks=["backend", "unknown"]),
        ProfileRules(allowed_role_tracks=("backend",)),
    )
    assert result.outcome == "accept"


def test_known_mismatch_rejects_despite_other_unknown_constraints():
    result = evaluate(
        profile(primary_role_family="unknown", career_stage="senior"),
        ProfileRules(allowed_role_families=("engineering",), allowed_career_stages=("entry",)),
    )
    assert result.outcome == "reject"
    assert result.reason == "career_stage_mismatch"


def test_absent_profile_and_empty_rules_cannot_accept():
    assert evaluate(None, ProfileRules(people_manager=False)).reason == "profile_missing"
    assert evaluate(profile(), ProfileRules()).outcome == "abstain"
    assert evaluate(None, ProfileRules()).reason == "no_rules"


@pytest.mark.parametrize(
    "values",
    [
        {"allowed_role_families": []},
        {"allowed_role_tracks": ["unknown"]},
        {"allowed_career_stages": ["entry", "entry"]},
        {"allowed_employment_types": ["invalid"]},
        {"allowed_role_families": "engineering"},
        {"people_manager": "false"},
        {"people_manager": 0},
        {"prompt": "keep good jobs"},
    ],
)
def test_invalid_rules_are_rejected(values):
    with pytest.raises(ValidationError):
        ProfileRules.model_validate(values)


def test_rules_are_deeply_immutable_and_json_round_trip():
    values = ["engineering"]
    rules = ProfileRules.model_validate({"allowed_role_families": values})
    values.append("sales")
    assert rules.allowed_role_families == ("engineering",)
    assert ProfileRules.model_validate_json(rules.model_dump_json()) == rules
    with pytest.raises(ValidationError):
        rules.people_manager = True
