from api import ai


def test_batch_usage_preserves_reported_cache_write_tokens_and_unknowns():
    reported = ai.batch_usage(
        {
            "input_tokens": 1174,
            "output_tokens": 52,
            "total_tokens": 1226,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 1171},
        }
    )
    assert reported["cache_write_tokens"] == 1171

    absent = ai.batch_usage(
        {
            "input_tokens": 1174,
            "output_tokens": 52,
            "total_tokens": 1226,
            "input_tokens_details": {"cached_tokens": 0},
        }
    )
    assert absent["cache_write_tokens"] is None
