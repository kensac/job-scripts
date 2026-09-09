from openai.lib._pydantic import to_strict_json_schema

from api.tasks.models import FilterVerdict
from core.filters import build_custom_instructions, compute_prompt_hash


def test_filter_evidence_guidance_preserves_wire_shape_cache_and_legacy_reasons():
    schema = to_strict_json_schema(FilterVerdict)
    reason = schema["properties"]["reason"]
    guidance = reason["description"]
    assert all(term in guidance for term in ("evidence", "unstated", "ambiguity", "negation"))
    assert set(schema["required"]) == {"should_filter", "reason"}
    assert reason["type"] == "string" and "maxLength" not in reason
    historical = "Evidence and qualifications " * 30
    assert FilterVerdict(should_filter=True, reason=historical).reason == historical
    assert (
        compute_prompt_hash(build_custom_instructions("must sponsor visas", "keep"))
        == "1d65d2a88fd09a27"
    )
