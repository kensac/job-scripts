"""The one writer of a page fetch, and the one reader of its text's two shapes.

A fetch is a fact about a posting's page: when it was read, how, and what
came back. Readers of a posting's text use the page_texts view, which leaves
out fetches whose text is not the posting's (page_text_rejections: a
maintenance notice, a career site's shell); readers of what one fetch held,
by its id, use page_fetch_texts, which keeps them.

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

import bisect
import hashlib
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, NamedTuple

from core.payloads.objects import (
    BundleCache,
    Link,
    PayloadStore,
    PayloadUnavailable,
    encode_payload,
    put_values,
    shared_store,
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


# How long a fetch's write waits on each connect or read of its upload, with
# no retry: past it the text is stored inline and moved later, so a slow store
# delays a fetch by this much at most. On 2026-11-26 a 10 KB PUT took 1.1 s
# from hetzner and 1.4 s from optiplex with 32 in flight at once (Garage
# took 28 and 23 PUTs a second); this allows several times that.
WRITE_TIMEOUT_SECONDS = 10.0


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
    return put_values(values, store or shared_store(WRITE_TIMEOUT_SECONDS), PAGE_BUNDLE_MAX_BYTES)


def record(url: str, status: str, method: str, content: str | None = None) -> int:
    """Append one fetch and return its id."""
    return record_many([Fetch(url, status, method, content)])[0]


# The fetches that are the board's own record of one posting: the ATS
# answering for that posting's id, and the text its listing carried. One text
# under several titles there is one posting under variant names (a feed's
# "Associate I" and "Associate II", a curated list's renamed title, the same
# posting in French and English), so such a fetch is never judged foreign.
# Measured on the 4,826 texts stored for more than one title (2026-11-27):
# of the board-record fetches the role test below would reject, nearly all
# were real postings, where of the pages the fetcher read itself nearly all
# were maintenance notices, shells, cookie walls and "page not found".
BOARD_RECORDS = ("ats text", "listing text")

# Level numerals name the rung, not the role: "Engineer II" and "Engineer III"
# are one role.
_LEVELS = frozenset({"i", "ii", "iii", "iv", "v", "vi"})

# The shortest title word a page must say. Shorter words are connectives and
# abbreviations ("of", "in", "ai", "ml", "ui") that a posting spells out or
# leaves out, so requiring them would reject an "AI Engineer" whose page says
# "artificial intelligence".
_MIN_ROLE_WORD = 3

_WORD = re.compile(r"[a-z]+")


def role(title: str) -> str:
    """A title's role: its words without case, punctuation, digits or level
    numerals, so "Engineer II" and "engineer 3" are one role."""
    return " ".join(w for w in _WORD.findall(title.lower()) if w not in _LEVELS)


def names_role(title: str, text: str) -> bool:
    """Whether `text` says the role `title` names: every word of it, as the
    start of a word of the text, so "engineer" is said by "engineering" and
    "grad" by "graduate". A title with no word long enough says nothing to
    check, and is named."""
    said = sorted(set(_WORD.findall(text.lower())))
    for word in role(title).split():
        if len(word) < _MIN_ROLE_WORD:
            continue
        at = bisect.bisect_left(said, word)
        if at == len(said) or not said[at].startswith(word):
            return False
    return True


def foreign(members: Sequence[Mapping[str, Any]], text: str) -> list[int]:
    """Which of one text's fetches it is not the posting's text for.

    `members` are every passed fetch of the text, each with its `id`, its
    `method` and its posting's `title`. A text stored for postings with
    different roles is not each one's own posting text: it is a page that
    came back the same whatever was asked for (a maintenance notice, a shell,
    a cookie wall). It is still a posting's when it names that posting's role:
    a shared description, a template under variant titles. So a fetch is
    foreign when its text is shared across roles, it does not name its own
    posting's role, and it is not a board's record (BOARD_RECORDS)."""
    if len({role(m["title"]) for m in members}) < 2:
        return []
    return [
        m["id"]
        for m in members
        if m["method"] not in BOARD_RECORDS and not names_role(m["title"], text)
    ]


_MEMBERS = """
    SELECT f.id, f.content_sha256 AS sha, f.method, j.title
    FROM page_fetches f JOIN jobs j ON j.url = f.url
    WHERE f.content_sha256 = ANY(%s) AND f.status = 'passed'
"""


def members_of(shas: Iterable[bytes]) -> dict[bytes, list[dict[str, Any]]]:
    """Every passed fetch of each text, by the text's digest, with its
    posting's title (idx_page_fetches_content_sha256)."""
    wanted = list(set(shas))
    if not wanted:
        return {}
    groups: dict[bytes, list[dict[str, Any]]] = {}
    with connection() as conn:
        for row in conn.execute(_MEMBERS, (wanted,)).fetchall():
            groups.setdefault(bytes(row["sha"]), []).append(row)
    return groups


def reject(fetch_ids: Iterable[int], conn: Any | None = None) -> list[int]:
    """Record fetches as not their posting's text; the ids newly recorded."""
    ids = sorted(set(fetch_ids))
    if not ids:
        return []
    sql = (
        "INSERT INTO page_text_rejections (fetch_id) SELECT unnest(%s::bigint[]) "
        "ON CONFLICT DO NOTHING RETURNING fetch_id"
    )
    if conn is not None:
        return [r["fetch_id"] for r in conn.execute(sql, (ids,)).fetchall()]
    with connection() as own:
        return [r["fetch_id"] for r in own.execute(sql, (ids,)).fetchall()]


def _reject_new(ids: Sequence[int], fetches: Sequence[Fetch]) -> None:
    """Judge fetches just written against the text's other fetches, so a
    page already known to come back the same for every posting (the
    maintenance notice) is never served as a new posting's text. Only these
    fetches are judged here; the text's older fetches are the sweep's
    (tasks.page_text_judging), which also undoes what was derived from them."""
    texts = {
        bytes.fromhex(digest(f.content)): f.content
        for f in fetches
        if f.status == "passed" and f.content and f.method not in BOARD_RECORDS
    }
    if not texts:
        return
    groups = members_of(texts)
    new = set(ids)
    rejected = [
        fetch_id
        for sha, members in groups.items()
        for fetch_id in foreign(members, texts[sha])
        if fetch_id in new
    ]
    if rejected:
        reject(rejected)
        logger.info("%s new page fetches are not their posting's text", len(rejected))


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
    ids = sorted(row["id"] for row in rows)
    _reject_new(ids, fetches)
    return ids


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
    store = store or shared_store()
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
    """One fetch's text, either shape, whether or not it is its posting's
    (page_fetch_texts); None when the fetch holds none."""
    with connection() as conn:
        row = conn.execute(
            "SELECT input_content, content_ref FROM page_fetch_texts WHERE id = %s", (fetch_id,)
        ).fetchone()
    return resolve([row])[0]["input_content"] if row else None
