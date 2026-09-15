import math

import pytest
from pydantic import ValidationError

from core.title_screen import (
    TitleObservation,
    TitleScreenArtifact,
    build_artifact,
    evaluate,
    normalize_title,
)


def observation(key: str, title: str = "Senior Engineer", passed: bool = False):
    return TitleObservation(job_key=key, title=title, passed=passed)


def artifact(*, count=100, training=None, held_out=None, **updates):
    return build_artifact(
        prompt_hash="filter-revision",
        reference_model="reference-model",
        training=(observation("train"),) if training is None else training,
        held_out=tuple(observation(f"test-{i}") for i in range(count))
        if held_out is None
        else held_out,
        confidence=0.95,
        max_keep_probability=0.05,
        **updates,
    )


def test_reject_requires_disjoint_held_out_evidence():
    result = evaluate("Senior Engineer", "filter-revision", artifact(), model="reference-model")
    assert result.outcome == "reject"
    assert result.keep_probability_upper_bound == pytest.approx(1 - 0.05 ** (1 / 100))


def test_title_normalization_preserves_meaningful_qualifiers():
    assert normalize_title("  SENIOR\tEngineer \n") == "senior engineer"
    evidence = artifact()
    assert (
        evaluate("SENIOR  engineer", "filter-revision", evidence, model="reference-model").outcome
        == "reject"
    )
    for title in ("Engineer", "Sr. Engineer", "Senior Engineer (Intern)", "Senior-Engineer"):
        assert (
            evaluate(title, "filter-revision", evidence, model="reference-model").outcome
            == "abstain"
        )


@pytest.mark.parametrize(
    ("title", "prompt_hash", "evidence", "reason"),
    [
        ("Senior Engineer", "filter-revision", None, "artifact_missing"),
        ("Senior Engineer", "changed-revision", artifact(), "prompt_mismatch"),
        (" \n", "filter-revision", artifact(), "title_missing"),
        ("Unknown title", "filter-revision", artifact(), "title_unseen_in_training"),
        ("Senior Engineer", "filter-revision", artifact(training=()), "title_unseen_in_training"),
        ("Senior Engineer", "filter-revision", artifact(count=0), "held_out_missing"),
        ("Senior Engineer", "filter-revision", artifact(count=1), "insufficient_evidence"),
    ],
)
def test_uncertainty_abstains(title, prompt_hash, evidence, reason):
    result = evaluate(title, prompt_hash, evidence, model="reference-model")
    assert result.outcome == "abstain"
    assert result.reason == reason


@pytest.mark.parametrize("partition", ["training", "held_out"])
def test_any_keep_prevents_rejection(partition):
    evidence = artifact(**{partition: (observation("keep", passed=True),)})
    result = evaluate("Senior Engineer", "filter-revision", evidence, model="reference-model")
    assert result.outcome == "abstain"
    assert result.reason == f"{partition}_contains_keep"


def test_multiple_candidates_use_simultaneous_confidence_correction():
    evidence = artifact(
        training=(observation("train"), observation("other", title="Staff Engineer"))
    )
    result = evaluate("Senior Engineer", "filter-revision", evidence, model="reference-model")
    assert result.keep_probability_upper_bound == pytest.approx(1 - 0.025 ** (1 / 100))
    assert evidence.candidate_count == 2


def test_training_keeps_do_not_increase_number_of_selected_titles():
    evidence = artifact(
        training=(
            observation("train"),
            observation("other", title="Junior Engineer", passed=True),
        )
    )
    assert evidence.candidate_count == 1


@pytest.mark.parametrize("partition", ["training", "held_out"])
def test_duplicate_posting_identity_cannot_inflate_confidence(partition):
    with pytest.raises(ValueError, match="only once"):
        artifact(**{partition: (observation("duplicate"), observation("duplicate"))})


def test_training_posting_cannot_leak_into_held_out_partition():
    with pytest.raises(ValueError, match="only once"):
        artifact(held_out=(observation("train"),))


@pytest.mark.parametrize(
    "updates",
    [
        {"confidence": 0},
        {"confidence": 1},
        {"confidence": math.nan},
        {"max_keep_probability": -0.1},
        {"max_keep_probability": 1},
        {"version": "unknown"},
        {"prompt_hash": " "},
        {"reference_model": " "},
        {"unrecognized": True},
    ],
)
def test_invalid_artifact_rejected(updates):
    values = artifact().model_dump()
    values.update(updates)
    with pytest.raises(ValidationError):
        TitleScreenArtifact.model_validate(values)


def test_artifact_is_immutable_and_json_round_trips():
    evidence = artifact()
    assert TitleScreenArtifact.model_validate_json(evidence.model_dump_json()) == evidence
    with pytest.raises(ValidationError):
        evidence.prompt_hash = "different"
    with pytest.raises(TypeError):
        evidence.counts["senior engineer"] = evidence.counts["senior engineer"]


@pytest.mark.parametrize("model", [None, "different-model"])
def test_missing_or_changed_reference_model_abstains(model):
    result = evaluate("Senior Engineer", "filter-revision", artifact(), model=model)
    assert result.outcome == "abstain"
    assert result.reason == "reference_model_mismatch"


def test_compact_artifact_retains_digest_without_posting_identities():
    evidence = artifact()
    assert len(evidence.evidence) == 1
    assert evidence.evidence[0].held_out_count == 100
    assert "test-99" not in evidence.model_dump_json()
    assert len(evidence.evidence_digest) == len(evidence.fingerprint) == 64
    assert evidence.fingerprint != artifact(count=99).fingerprint


def test_evidence_input_order_does_not_change_fingerprint():
    observations = (observation("a"), observation("b"))
    assert (
        artifact(held_out=observations).fingerprint
        == artifact(held_out=observations[::-1]).fingerprint
    )
