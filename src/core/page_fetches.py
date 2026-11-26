"""The one writer of a page fetch, and the one reader of its text's two shapes.

A fetch is a fact about a posting's page: when it was read, how, and what
came back. Readers of the text alone use the page_texts view.

A fetch's text lives in object storage, as a member of a verified bundle
(docs/agents/sources-and-boards.md, "A page's text is held by reference"):
the writer uploads it before the row exists, and the row keeps the
reference. Text is inline (`content`) only where storage was unavailable at
write time, or on a row written before references, until
tasks.page_text_tiering moves it. page_texts carries both shapes:
`input_content` is NULL on a referenced row and `content_ref` names its
member. Every reader that wants the text selects `content_ref` beside
`input_content` and passes the rows through resolve. `chars` is the text's
length either way.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, NamedTuple

from core.payloads.objects import (
    BundleCache,
    Link,
    PayloadStore,
    PayloadUnavailable,
    encode_payload,
    put_values,
)
from core.pool import connection, in_transaction
from core.store import WORKER

logger = logging.getLogger(__name__)

# A bundle is read whole to resolve one member, so page bundles stay a few
# pages wide: a reader of one page reads at most this much. A page averaged
# 5.5 KB (a 2% sample of production, 2026-11-26), and a 10 KB GET took 4 ms
# on hetzner and 220 ms from optiplex, latency-bound either way, where 8 MB
# bundles made 2,000 random held pages take 16 s on hetzner and 153 s from
# optiplex. 256 KB stays inside that latency on both while a backfill still
# bundles about 45 pages per PUT, at the 25 to 30 PUTs a second Garage took.
PAGE_BUNDLE_MAX_BYTES = 256 * 1024


class Fetch(NamedTuple):
    url: str
    # 'passed' when text came back, 'failed' when nothing usable did.
    status: str
    # How it was read, or why not.
    method: str
    content: str | None = None


def digest(text: str) -> str:
    return hashlib.sha256(encode_payload(text)).hexdigest()


def upload(texts: Iterable[str], store: PayloadStore | None = None) -> dict[str, Link]:
    """Each text's bundle link, keyed by the text's digest: uploaded and read
    back before this returns (PayloadStore.put_bundle). Outside transactions."""
    values = {digest(text): text for text in texts if text}
    if not values:
        return {}
    return put_values(values, store or PayloadStore.from_env(), PAGE_BUNDLE_MAX_BYTES)


def record(url: str, status: str, method: str, content: str | None = None) -> int:
    """Append one fetch and return its id."""
    return record_many([Fetch(url, status, method, content)])[0]


def record_many(fetches: Sequence[Fetch]) -> list[int]:
    """Append fetches, their text uploaded first, and return their ids in
    order. Storage that cannot take the text leaves it inline rather than
    losing the fetch; the hourly move catches it up."""
    if in_transaction():
        raise RuntimeError("A page fetch uploads its text, so it is not written in a transaction")
    texts = [f.content for f in fetches if f.content]
    links: dict[str, Link] = {}
    if texts:
        try:
            links = upload(texts)
        except PayloadUnavailable:
            logger.warning("Object storage unavailable; %s page texts stored inline", len(texts))
    columns: list[list[Any]] = [[] for _ in range(8)]
    for f in fetches:
        link = links.get(digest(f.content)) if f.content else None
        if link is None:
            stored: tuple[Any, ...] = (f.content, None, None, None, None)
        else:
            assert f.content is not None
            stored = (None, bytes.fromhex(digest(f.content)), *link, len(f.content))
        for column, value in zip(columns, (f.url, f.status, f.method, *stored), strict=True):
            column.append(value)
    # One statement for the whole list. Ids come from the sequence in the
    # order the SELECT feeds them, which ORDER BY fixes, so sorting the
    # returned ids lines them up with `fetches`.
    with connection() as conn:
        rows = conn.execute(
            "INSERT INTO page_fetches (url, status, method, content, content_sha256, "
            "content_object, content_object_size, content_chars, worker) "
            "SELECT url, status, method, content, sha256, object, size, chars, %s "
            "FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[], %s::bytea[], "
            "%s::bytea[], %s::int[], %s::int[]) WITH ORDINALITY "
            "AS f(url, status, method, content, sha256, object, size, chars, n) "
            "ORDER BY n RETURNING id",
            (WORKER, *columns),
        ).fetchall()
    return sorted(row["id"] for row in rows)


def resolve(
    rows: Iterable[Mapping[str, Any]],
    field: str = "input_content",
    ref: str = "content_ref",
    store: PayloadStore | None = None,
    cache: BundleCache | None = None,
) -> list[dict[str, Any]]:
    """Each row with `field` holding its page text, read from object storage
    where `ref` names a member. The bundles are read concurrently before any
    row is filled; an inline row costs nothing. A reference that cannot be
    read, or reads something else, raises PayloadUnavailable; it is never a
    page without text."""
    cache = {} if cache is None else cache
    resolved = [dict(row) for row in rows]
    links = [row[ref] for row in resolved if row.get(ref) is not None]
    if not links:
        return resolved
    if in_transaction():
        raise RuntimeError("Page text cannot be read inside a database transaction")
    store = store or PayloadStore.from_env()
    store.prefetch(((link["object"], link["size"]) for link in links), cache)
    for row in resolved:
        link = row.get(ref)
        if link is not None:
            value = store.get_digest_member(link["object"], link["size"], link["member"], cache)
            if not isinstance(value, str):
                raise PayloadUnavailable("Page text is not a string")
            row[field] = value
    return resolved


def text_of(fetch_id: int) -> str | None:
    """One fetch's text, either shape; None when the fetch holds none."""
    with connection() as conn:
        row = conn.execute(
            "SELECT input_content, content_ref FROM page_texts WHERE id = %s", (fetch_id,)
        ).fetchone()
    return resolve([row])[0]["input_content"] if row else None
