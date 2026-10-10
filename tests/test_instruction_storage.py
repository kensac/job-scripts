from api import db
from core import store
from core.query_instructions import hydrate


def test_identical_instructions_share_storage_without_changing_verdict_identity():
    ids = [
        store.add_ai_result(
            f"https://example.test/{index}",
            "passed",
            check_type="custom",
            prompt_hash=f"verdict-identity-{index}",
            instructions="Preserve exact instructions.\n",
            input_content="Cached page content",
        )
        for index in range(2)
    ]
    rows = db.query(
        "SELECT id,instructions,instructions_id,input_content,prompt_hash,to_jsonb(q)->'instructions_id' AS reference "
        "FROM ai_queries q WHERE id=ANY(%s) ORDER BY id",
        (ids,),
    )
    rows = hydrate(rows)
    assert rows[0]["reference"] is not None
    assert rows[0]["reference"] == rows[1]["reference"]
    assert [r["prompt_hash"] for r in rows] == ["verdict-identity-0", "verdict-identity-1"]
    assert all(r["instructions"] == "Preserve exact instructions.\n" for r in rows)
    assert all(r["input_content"] == "Cached page content" for r in rows)


def test_null_empty_and_whitespace_instruction_values_remain_distinct():
    values = [None, "", "x", "x "]
    ids = [
        store.add_ai_result("https://example.test/values", "passed", instructions=s) for s in values
    ]
    rows = db.query(
        "SELECT instructions,instructions_id,to_jsonb(q)->'instructions_id' AS reference "
        "FROM ai_queries q WHERE id=ANY(%s) ORDER BY id",
        (ids,),
    )
    rows = hydrate(rows)
    assert [r["instructions"] for r in rows] == values
    assert rows[0]["reference"] is None
    assert all(r["reference"] is not None for r in rows[1:])
    assert len({r["reference"] for r in rows[1:]}) == 3


def test_concurrent_writers_share_exact_text():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(4)

    def write(index):
        barrier.wait(timeout=10)
        return store.add_ai_result(
            f"https://example.test/concurrent/{index}", "passed", instructions="Shared exact text"
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(write, range(4)))
    rows = db.query("SELECT instructions_id FROM ai_queries WHERE id=ANY(%s)", (ids,))
    assert len(rows) == 4
    assert len({row["instructions_id"] for row in rows}) == 1
    assert rows[0]["instructions_id"] is not None


def test_missing_reference_never_becomes_legacy_null():
    import pytest

    from core.query_instructions import InstructionUnavailable, hydrate

    with pytest.raises(InstructionUnavailable):
        hydrate([{"instructions": None, "instructions_id": -1}])
    assert hydrate([{"instructions": None, "instructions_id": None}]) == [{"instructions": None}]
    # An inline value is not a shape any writer produces; it is never read.
    assert hydrate([{"instructions": "stale", "instructions_id": None}]) == [{"instructions": None}]


def test_admin_query_routes_return_the_referenced_text(client, admin_headers):
    query_id = store.add_ai_result(
        "https://example.test/history",
        "passed",
        check_type="custom",
        prompt_hash="filter-key",
        instructions="Exact historical instructions",
        input_content="Original cached content",
    )
    detail = client.get(f"/v1/admin/queries/{query_id}", headers=admin_headers).json()
    assert detail["instructions"] == "Exact historical instructions"
    responses = client.get(
        "/v1/admin/jobs/responses",
        params={"url": "https://example.test/history"},
        headers=admin_headers,
    )
    assert responses.json()["rows"] == [detail]


def test_dictionary_corruption_is_explicit():
    import pytest

    from core.query_instructions import InstructionUnavailable

    query_id = store.add_ai_result(
        "https://example.test/corrupt",
        "passed",
        check_type="custom",
        prompt_hash="key",
        instructions="original",
    )
    db.execute("UPDATE ai_queries SET instructions=NULL WHERE id=%s", (query_id,))
    db.execute(
        "UPDATE ai_instruction_texts SET instructions='corrupted' WHERE id=(SELECT instructions_id FROM ai_queries WHERE id=%s)",
        (query_id,),
    )
    with pytest.raises(InstructionUnavailable):
        store.decided_custom_urls(["https://example.test/corrupt"], "key")


def test_the_verdict_cache_check_answers_from_the_row_without_reading_page_text(monkeypatch):
    """The filter sweeps ask this once per candidate, 1.32M times in 36 hours
    (pg_stat_statements, 2026-10-03), and only ever test the answer. Reading
    the whole row detoasted the cached page text on every one of them."""
    import psycopg

    long_text = "Posting text.\n" * 400
    store.add_ai_result(
        "https://example.test/cached",
        "passed",
        check_type="custom",
        prompt_hash="key",
        model="gpt-5-nano",
        instructions="Exact instructions.",
        input_content=long_text,
    )
    store.add_ai_result(
        "https://example.test/undecided",
        "failed",
        check_type="custom",
        prompt_hash="key",
        input_content=long_text,
    )
    statements = []
    execute = psycopg.Cursor.execute

    def recording(cursor, query, *args, **kwargs):
        statements.append(str(query))
        return execute(cursor, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", recording)

    urls = ["https://example.test/cached", "https://example.test/undecided"]
    answers = [
        store.decided_custom_urls(urls, "key"),
        store.decided_custom_urls(urls, "key", model="gpt-5-nano"),
        store.decided_custom_urls(urls, "key", model="gpt-5-mini"),
        store.decided_custom_urls(urls, "other"),
    ]

    assert answers == [{urls[0]}, {urls[0]}, set(), set()]
    # The verdicts view has no page text column, so naming it is the guarantee.
    reads = [s for s in statements if "FROM verdicts" in s]
    assert reads
    assert not [s for s in reads if "*" in s or "input_content" in s]
