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


def test_copy_compact_restore_preserves_metadata_cache_and_admin_response(client, admin_headers):
    from api.ai.migrate_query_instructions import migrate_chunk

    query_id = store.add_ai_result(
        "https://example.test/history",
        "passed",
        check_type="custom",
        prompt_hash="filter-key",
        instructions="Exact historical instructions",
        input_content="Original cached content",
        model="original-model",
        prompt_tokens=10,
        completion_tokens=2,
    )
    db.execute(
        "UPDATE ai_queries SET instructions=%s,instructions_id=NULL WHERE id=%s",
        ("Exact historical instructions", query_id),
    )
    original = db.query_one("SELECT * FROM ai_queries WHERE id=%s", (query_id,))
    before = client.get(f"/v1/admin/queries/{query_id}", headers=admin_headers).json()
    copied = migrate_chunk(mode="copy", after=0, through=query_id, limit=1)
    assert copied["copied"] == 1
    assert migrate_chunk(mode="copy", after=0, through=query_id, limit=1)["copied"] == 0
    assert migrate_chunk(mode="verify", after=0, through=query_id, limit=1)["verified"] == 1
    assert (
        migrate_chunk(
            mode="compact",
            after=0,
            through=query_id,
            limit=1,
            backup_complete=True,
            readers_compatible=True,
        )["compacted"]
        == 1
    )
    compacted = db.query_one("SELECT * FROM ai_queries WHERE id=%s", (query_id,))
    assert compacted["instructions"] is None
    assert {k: v for k, v in compacted.items() if k not in ("instructions", "instructions_id")} == {
        k: v for k, v in original.items() if k not in ("instructions", "instructions_id")
    }
    assert store.decided_custom_urls(["https://example.test/history"], "filter-key") == {
        "https://example.test/history"
    }
    assert store.get_content("https://example.test/history") is None
    assert client.get(f"/v1/admin/queries/{query_id}", headers=admin_headers).json() == before
    responses = client.get(
        "/v1/admin/jobs/responses",
        params={"url": "https://example.test/history"},
        headers=admin_headers,
    )
    assert responses.status_code == 200
    assert responses.json()["rows"] == [before]
    assert migrate_chunk(mode="restore", after=0, through=query_id, limit=1)["restored"] == 1
    restored = db.query_one("SELECT * FROM ai_queries WHERE id=%s", (query_id,))
    assert restored["instructions"] == original["instructions"]
    assert restored["instructions_id"] == compacted["instructions_id"]


def test_bad_reference_rolls_back_whole_chunk():
    import pytest

    from api.ai.migrate_query_instructions import migrate_chunk
    from core.query_instructions import InstructionUnavailable

    first = store.add_ai_result("https://example.test/first", "passed", instructions="first")
    second = store.add_ai_result("https://example.test/second", "passed", instructions="second")
    db.execute(
        "UPDATE ai_queries SET instructions=%s,instructions_id=NULL WHERE id=%s", ("first", first)
    )
    db.execute("UPDATE ai_queries SET instructions='changed inline' WHERE id=%s", (second,))
    with pytest.raises(InstructionUnavailable):
        migrate_chunk(mode="copy", after=0, through=second, limit=2)
    assert (
        db.query_one("SELECT instructions_id FROM ai_queries WHERE id=%s", (first,))[
            "instructions_id"
        ]
        is None
    )


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


def test_compaction_requires_explicit_gates(monkeypatch):
    import pytest

    from api.ai.migrate_query_instructions import main

    monkeypatch.setattr("sys.argv", ["migration", "compact", "--through", "1", "--limit", "1"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


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


def test_service_compaction_requires_backup_and_reader_confirmations():
    import pytest

    from api.ai.migrate_query_instructions import migrate_chunk
    from core.query_instructions import InstructionUnavailable

    query_id = store.add_ai_result("https://example.test/gates", "passed", instructions="retain")
    db.execute("UPDATE ai_queries SET instructions=%s WHERE id=%s", ("retain", query_id))
    with pytest.raises(InstructionUnavailable):
        migrate_chunk(mode="compact", after=0, through=query_id, limit=1)
    assert (
        db.query_one("SELECT instructions FROM ai_queries WHERE id=%s", (query_id,))["instructions"]
        == "retain"
    )


def test_compaction_holds_dictionary_content_lock_through_validation(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from psycopg.errors import LockNotAvailable

    from api.ai import migrate_query_instructions as migration
    from core.pool import connection

    query_id = store.add_ai_result("https://example.test/locked", "passed", instructions="original")
    reference = db.query_one("SELECT instructions_id FROM ai_queries WHERE id=%s", (query_id,))[
        "instructions_id"
    ]
    db.execute("UPDATE ai_queries SET instructions=%s WHERE id=%s", ("original", query_id))
    original_hash = migration.hashlib.sha256

    def attempt_dictionary_change():
        try:
            with connection() as conn, conn.transaction():
                conn.execute("SET LOCAL lock_timeout='100ms'")
                conn.execute(
                    "UPDATE ai_instruction_texts SET instructions=instructions WHERE id=%s",
                    (reference,),
                )
        except LockNotAvailable:
            return "blocked"
        return "unprotected"

    def inspect_lock(value):
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(attempt_dictionary_change).result(timeout=5) == "blocked"
        return original_hash(value)

    monkeypatch.setattr(migration.hashlib, "sha256", inspect_lock)
    migration.migrate_chunk(
        mode="compact",
        after=0,
        through=query_id,
        limit=1,
        backup_complete=True,
        readers_compatible=True,
    )


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
    reads = [s for s in statements if "ai_queries" in s]
    assert reads
    assert not [s for s in reads if "*" in s or "input_content" in s]
