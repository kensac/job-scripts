"""`page_texts` is the one statement of which ai_queries rows hold page text."""

from __future__ import annotations

import pathlib
import re

from api import db
from core import store
from core.store import add_ai_result

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

# Page text read from ai_queries in the same statement. The admin row explorer
# shows a stored row as it is, page text included, so it reads the table, and
# the move of fetches out of ai_queries reads the rows it moves.
_RAW_ROW_READERS = {"api/routers/admin/queries.py", "tasks/page_fetch_move.py"}
_TEXT_FROM_TABLE = re.compile(
    r"input_content[^;]{0,400}?(?:FROM|JOIN) ai_queries"
    r"|(?:FROM|JOIN) ai_queries[^;]{0,400}?input_content",
    re.S,
)
PAGE = "Posting text. " * 40


def test_page_text_is_a_fetched_page_or_a_check_copy_never_a_filter_input():
    fetched = add_ai_result("https://x/a", "passed", check_type="content", input_content=PAGE)
    copied = add_ai_result("https://x/b", "passed", check_type="closed", input_content=PAGE)
    add_ai_result("https://x/c", "passed", check_type="custom", input_content="Acme\n" + PAGE)
    add_ai_result("https://x/d", "failed", check_type="content")

    rows = db.query("SELECT id, url, on_verdict FROM page_texts ORDER BY url")
    assert rows == [
        {"id": fetched, "url": "https://x/a", "on_verdict": False},
        {"id": copied, "url": "https://x/b", "on_verdict": True},
    ]


def test_the_sweeps_and_the_filters_read_the_same_text():
    """CONTENT_LATERAL read a custom filter's wrapped input as the page when it
    was the only text, while get_contents did not. Both now read page_texts."""
    add_ai_result("https://x/c", "passed", check_type="custom", input_content="Acme\n" + PAGE)
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
