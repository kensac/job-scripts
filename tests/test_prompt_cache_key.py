"""Every request carrying the same instructions asks the provider for the
same prompt cache, so the shared prefix is served at the cached rate."""

from __future__ import annotations

from core.batch import BatchSpec, _build_line, prompt_cache_key


def test_requests_sharing_instructions_share_a_cache_key():
    a = BatchSpec("a", "judge this", "posting one", "V", {"type": "object"})
    b = BatchSpec("b", "judge this", "posting two", "V", {"type": "object"})
    c = BatchSpec("c", "extract that", "posting one", "V", {"type": "object"})
    la, lb, lc = (_build_line(s, "gpt-5-nano", "low", 100) for s in (a, b, c))
    assert la["body"]["prompt_cache_key"] == lb["body"]["prompt_cache_key"]
    assert la["body"]["prompt_cache_key"] != lc["body"]["prompt_cache_key"]
    # Within the provider's key length and stable across processes.
    key = prompt_cache_key("judge this")
    assert key == la["body"]["prompt_cache_key"] and len(key) <= 64
