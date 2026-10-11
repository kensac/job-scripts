"""`page_texts` is the one statement of which rows hold page text."""

from __future__ import annotations

import pathlib
import re

from api import db
from core import page_fetches, store
from tests.factories import legacy_answer

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

# Page text read from ai_queries in the same statement. The admin row explorer
# shows a stored row as it is, the text the model saw included, so it reads
# the table.
_RAW_ROW_READERS = {"api/routers/admin/queries.py"}
_TEXT_FROM_TABLE = re.compile(
    r"input_content[^;]{0,400}?(?:FROM|JOIN) ai_queries"
    r"|(?:FROM|JOIN) ai_queries[^;]{0,400}?input_content",
    re.S,
)
PAGE = "Posting text. " * 40


def test_page_text_is_fetched_text_never_an_answer_copy_or_a_filter_input():
    fetched = page_fetches.record("https://x/a", "passed", "scraped", PAGE)
    page_fetches.record("https://x/a", "failed", "fetch returned nothing")
    legacy_answer("https://x/b", "passed", check_type="closed", input_content=PAGE)
    legacy_answer("https://x/c", "passed", check_type="custom", input_content="Acme\n" + PAGE)

    rows = db.query("SELECT id, url FROM page_texts ORDER BY url")
    assert rows == [{"id": fetched, "url": "https://x/a"}]


def test_the_sweeps_and_the_filters_read_the_same_text():
    """CONTENT_LATERAL read a custom filter's wrapped input as the page when it
    was the only text, while get_contents did not. Both now read page_texts."""
    legacy_answer("https://x/c", "passed", check_type="custom", input_content="Acme\n" + PAGE)
    lateral = db.query(
        "SELECT q.input_content FROM (SELECT 'https://x/c' AS url) j "
        + store.CONTENT_LATERAL.format(url="j.url", columns="input_content")
    )
    assert lateral == []
    assert store.get_contents(["https://x/c"]) == {}


def test_only_the_row_explorer_reads_page_text_from_the_table():
    readers = sorted(
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if _TEXT_FROM_TABLE.search(path.read_text())
    )
    assert set(readers) <= _RAW_ROW_READERS, (
        f"read page text from page_texts instead: {sorted(set(readers) - _RAW_ROW_READERS)}"
    )


def test_the_text_lookups_are_index_only():
    """CONTENT_LATERAL's id lookup and the content backfill's gap check read
    idx_page_fetches_url_text alone. A predicate that stops matching the view
    or MIN_CONTENT_CHARS sends every lookup back to the TOASTed heap."""
    db.execute(
        "INSERT INTO page_fetches (url, status, method, content) "
        "SELECT 'https://x/' || i, 'passed', 'scraped', repeat('x', 300) "
        "FROM generate_series(1, 5000) i"
    )
    db.execute("VACUUM ANALYZE page_fetches")
    lookups = (
        "SELECT q.id FROM (SELECT 'https://x/a' AS url) j "
        + store.CONTENT_LATERAL.format(url="j.url", columns="q.id"),
        "SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM page_texts q WHERE q.url = 'https://x/a' "
        f"AND length(q.input_content) > {store.MIN_CONTENT_CHARS})",
    )
    with db.pool.connection() as conn:
        conn.execute("SET LOCAL enable_seqscan = off")
        conn.execute("SET LOCAL enable_bitmapscan = off")
        for sql in lookups:
            plan = "\n".join(r["QUERY PLAN"] for r in conn.execute("EXPLAIN " + sql).fetchall())
            assert "Index Only Scan using idx_page_fetches_url_text" in plan, plan
