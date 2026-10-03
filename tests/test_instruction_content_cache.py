from api import db
from core import store


def test_instruction_storage_preserves_positive_content_cache_and_source_identity():
    from api.ai.migrate_query_instructions import migrate_chunk

    preferred_url = "https://example.test/instruction-cache/preferred"
    fallback_url = "https://example.test/instruction-cache/fallback"
    raw_page = "Original raw posting content. " * store.MIN_CONTENT_CHARS
    newer_check_page = "Newer verification page content. " * store.MIN_CONTENT_CHARS
    fallback_page = "Non-custom fallback page content. " * store.MIN_CONTENT_CHARS
    wrapped_input = "Company/title wrapped custom input. " * store.MIN_CONTENT_CHARS
    original_instructions = " Preserve exact rules, including Unicode: café.\n"

    preferred_id = store.add_ai_result(
        preferred_url,
        "passed",
        check_type="content",
        instructions=original_instructions,
        input_content=raw_page,
    )
    newer_check_id = store.add_ai_result(
        preferred_url,
        "passed",
        check_type="closed",
        instructions=original_instructions,
        input_content=newer_check_page,
    )
    custom_id = store.add_ai_result(
        preferred_url,
        "passed",
        check_type="custom",
        prompt_hash="instruction-cache-filter",
        instructions=original_instructions,
        input_content=wrapped_input,
    )
    fallback_id = store.add_ai_result(
        fallback_url,
        "passed",
        check_type="closed",
        instructions=original_instructions,
        input_content=fallback_page,
    )
    ids = [preferred_id, newer_check_id, custom_id, fallback_id]
    # Exercise legacy backfill, rather than only compatibility writes that
    # already carry a dictionary reference.
    db.execute(
        "UPDATE ai_queries SET instructions=%s,instructions_id=NULL WHERE id=ANY(%s)",
        (original_instructions, ids),
    )

    def assert_cache_and_provenance():
        # These getters deliberately select the newest non-custom raw input.
        assert store.get_content(preferred_url) == newer_check_page
        assert store.get_content(fallback_url) == fallback_page
        assert store.get_contents([preferred_url, fallback_url]) == {
            preferred_url: newer_check_page,
            fallback_url: fallback_page,
        }
        # The extraction selector deliberately prefers a content row, even
        # when a later verification or custom verdict carries another input.
        selected = db.query(
            "SELECT page.url,q.id AS content_row_id,q.input_content "
            "FROM (VALUES (%s::text),(%s::text)) AS page(url) "
            + store.CONTENT_LATERAL.format(url="page.url", columns="id,input_content")
            + " ORDER BY page.url",
            (preferred_url, fallback_url),
        )
        assert {row["url"]: (row["content_row_id"], row["input_content"]) for row in selected} == {
            preferred_url: (preferred_id, raw_page),
            fallback_url: (fallback_id, fallback_page),
        }
        retained = db.query("SELECT id,input_content FROM ai_queries WHERE id=ANY(%s)", (ids,))
        assert {row["id"]: row["input_content"] for row in retained} == {
            preferred_id: raw_page,
            newer_check_id: newer_check_page,
            custom_id: wrapped_input,
            fallback_id: fallback_page,
        }

    assert_cache_and_provenance()
    for mode, counter in (
        ("copy", "copied"),
        ("compact", "compacted"),
        ("restore", "restored"),
    ):
        confirmations = (
            {"backup_complete": True, "readers_compatible": True} if mode == "compact" else {}
        )
        result = migrate_chunk(
            mode=mode, after=0, through=max(ids), limit=len(ids), **confirmations
        )
        assert result[counter] == len(ids)
        assert_cache_and_provenance()
        stored = db.query("SELECT instructions FROM ai_queries WHERE id=ANY(%s)", (ids,))
        expected = None if mode == "compact" else original_instructions
        assert len(stored) == len(ids)
        assert all(row["instructions"] == expected for row in stored)
        assert migrate_chunk(mode="verify", after=0, through=max(ids), limit=len(ids))[
            "verified"
        ] == len(ids)
