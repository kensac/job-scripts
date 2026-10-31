"""The one writer of a page fetch, and the one reader of its text's two shapes.

A fetch is a fact about a posting's page: when it was read, how, and what
came back. Readers of the text alone use the page_texts view.

A fetch's text is held inline (`content`) or by reference to a member of a
verified bundle object (docs/agents/sources-and-boards.md, "A page's text can
be held by reference"). page_texts carries both: `input_content` is NULL on a
referenced row and `content_ref` names its member. Every reader that wants the
text selects `content_ref` beside `input_content` and passes the rows through
resolve. `chars` is the text's length either way.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from core.payloads.objects import BundleCache, PayloadStore, PayloadUnavailable
from core.pool import connection, in_transaction
from core.store import WORKER


def record(url: str, status: str, method: str, content: str | None = None) -> int:
    """Append one fetch. `status` is 'passed' when text came back, 'failed'
    when nothing usable did; `method` says how it was read or why not."""
    with connection() as conn:
        row = conn.execute(
            "INSERT INTO page_fetches (url, status, method, content, worker) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (url, status, method, content, WORKER),
        ).fetchone()
    assert row is not None
    return row["id"]


def resolve(
    rows: Iterable[Mapping[str, Any]],
    field: str = "input_content",
    ref: str = "content_ref",
    store: PayloadStore | None = None,
    cache: BundleCache | None = None,
) -> list[dict[str, Any]]:
    """Each row with `field` holding its page text, read from object storage
    where `ref` names a member. An inline row costs nothing. A reference that
    cannot be read, or reads something else, raises PayloadUnavailable; it is
    never a page without text."""
    cache = {} if cache is None else cache
    resolved = []
    for stored in rows:
        row = dict(stored)
        link = row.get(ref)
        if link is not None:
            if in_transaction():
                raise RuntimeError("Page text cannot be read inside a database transaction")
            store = store or PayloadStore.from_env()
            value = store.get_digest_member(link["object"], link["size"], link["member"], cache)
            if not isinstance(value, str):
                raise PayloadUnavailable("Page text is not a string")
            row[field] = value
        resolved.append(row)
    return resolved


def text_of(fetch_id: int) -> str | None:
    """One fetch's text, either shape; None when the fetch holds none."""
    with connection() as conn:
        row = conn.execute(
            "SELECT input_content, content_ref FROM page_texts WHERE id = %s", (fetch_id,)
        ).fetchone()
    return resolve([row])[0]["input_content"] if row else None
