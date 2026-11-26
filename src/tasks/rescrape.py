"""A page scraped again that did not change, re-stamped rather than re-paid for.

A url reaches a sweep's candidate list when its current content row is not the
one its answer came from, which a re-scrape makes true whether or not the page
actually changed. A re-scrape that changed nothing is the common case.
Comparing the stored hash to the new text separates the two, and the unchanged
ones have their row id refreshed so they do not come back every cycle. Nothing
is paid for; the row keeps the answer it had.

Every answer-table sweep calls this rather than spelling its own copy.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, LiteralString

from api import db
from core.store import CONTENT_LATERAL

logger = logging.getLogger(__name__)

# One literal statement per answer table, rather than a table name formatted
# into one: the set of tables this may re-stamp and the set of statements it
# can run are the same set, so neither can drift and nothing is interpolated
# into SQL. A new sweep adds its line here deliberately.
_RESTAMP: dict[str, LiteralString] = {
    "job_embeddings": "UPDATE job_embeddings SET content_row_id = %s WHERE url = %s",
    "job_requirements": "UPDATE job_requirements SET content_row_id = %s WHERE url = %s",
    "job_comp": "UPDATE job_comp SET content_row_id = %s WHERE url = %s",
}
STAMPABLE = frozenset(_RESTAMP)


def drop_unchanged(rows: list[dict[str, Any]], *, table: str, limit: int) -> list[dict[str, Any]]:
    """The rows whose text actually moved, re-stamping the ones that did not.

    `rows` must carry `input_content`, `stored_hash`, `content_row_id` and
    `url`. Each row gets `content_hash` set, so a caller that goes on to store
    an answer has the hash it was computed over without hashing twice.
    """
    statement = _RESTAMP.get(table)
    if statement is None:
        raise ValueError(f"{table!r} is not a re-stampable table")

    changed = []
    unchanged: list[tuple[int | None, str]] = []
    for row in rows:
        text = row["input_content"][:limit]
        row["content_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if row["stored_hash"] and row["stored_hash"] == row["content_hash"]:
            unchanged.append((row["content_row_id"], row["url"]))
        else:
            changed.append(row)

    if unchanged:
        with db.pool.connection() as conn:
            conn.cursor().executemany(statement, unchanged)
        logger.info(
            f"{len(unchanged)} page(s) re-scraped without changing; {table} rows re-stamped"
        )
    return changed


def content_is_current(url: str, content_row_id: int | None) -> bool:
    if content_row_id is None:
        return False
    row = db.query_one(
        "SELECT q.id FROM (VALUES (%s::text)) AS page(url) "
        + CONTENT_LATERAL.format(url="page.url", columns="id"),
        (url,),
    )
    return row is not None and row["id"] == content_row_id
