from openai.lib._pydantic import to_strict_json_schema

from core.filters import (
    build_custom_decision_instructions,
    build_custom_instructions,
    compute_filter_hash,
    compute_prompt_hash,
)
from tasks.models import FilterDecision, FilterResult, FilterVerdict


def test_decision_wire_excludes_reason_without_changing_criteria_identity():
    prompt = "must sponsor visas"
    before = build_custom_instructions(prompt, "keep")
    after = build_custom_decision_instructions(prompt, "keep")
    assert before.split("\n\nreason:")[0] == after.split("\n\nReturn only")[0]
    assert "<=25 words" not in after
    assert set(to_strict_json_schema(FilterDecision)["properties"]) == {"should_filter"}
    assert compute_filter_hash(prompt, "keep") == compute_prompt_hash(before) == "1d65d2a88fd09a27"
    assert compute_filter_hash(prompt, "keep") != compute_filter_hash(prompt, "filter")
    assert set(to_strict_json_schema(FilterVerdict)["required"]) == {"should_filter", "reason"}


def test_batch_reader_preserves_paid_legacy_reason_without_inventing_new_one():
    legacy = FilterResult.model_validate_json('{"should_filter":true,"reason":"No sponsorship"}')
    decision = FilterResult.model_validate_json('{"should_filter":true}')
    assert legacy.should_filter == decision.should_filter
    assert legacy.reason == "No sponsorship"
    assert decision.reason is None
