"""Find the page fetches whose text is not their posting's.

core.page_fetches.foreign is the rule: a text stored for postings with
different roles that does not name a posting's own role, on a page the
fetcher read itself, is not that posting's text. The writer applies it to a
fetch as it is written, against the text's fetches so far. A text can become
shared later, when another posting's page comes back the same, and then its
older fetches are judged too: this sweep walks page_fetches in id order and
judges every text a new fetch carries, all of that text's fetches at once.

The walk stops below the oldest fetch whose text is still inline: such a row
has no digest until tasks.page_text_tiering moves it, so passing it would
skip it for good. The cursor is the latest run's `through`, so a run killed by
a deploy resumes where it stopped, and the first run walks the history.

A newly rejected fetch's local embedding is removed with it, in one
transaction, so tasks.derivations.local_embeddings embeds the posting again
from its own text, or leaves it unembedded when it has none. The other
derivations re-derive by themselves: each compares its stored content_row_id
with the posting's current text, which no longer is the rejected fetch.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import db
from core import page_fetches
from core.pool import connection
from tasks.runtime import cancelled, checkpoint

logger = logging.getLogger(__name__)

KIND = "judge_page_texts"

# Fetches a step reads, checkpointed after each, so a deploy loses at most
# one step. On a production copy (2026-11-27) 5,000 fetches carried about 2,300
# texts, looked up with their postings' titles in 0.1 s: the 2.1M-fetch history
# is some 420 steps and a minute of queries, and a step's checkpoint is one
# small write.
STEP_FETCHES = 5000


def _start() -> int:
    row = db.query_one(
        "SELECT payload->'through' AS through FROM tasks "
        "WHERE kind = %s AND payload ? 'through' ORDER BY id DESC LIMIT 1",
        (KIND,),
    )
    through = row and row["through"]
    return through if isinstance(through, int) else 0


def _limit() -> int:
    """The last fetch id the walk may pass: below the oldest inline text
    (idx_page_fetches_inline_text), else the newest fetch."""
    row = db.query_one(
        "SELECT (SELECT min(id) FROM page_fetches WHERE content <> '') AS inline, "
        "(SELECT max(id) FROM page_fetches) AS newest"
    )
    if row is None or row["newest"] is None:
        return 0
    return row["inline"] - 1 if row["inline"] is not None else row["newest"]


def _texts(groups: dict[bytes, list[dict[str, Any]]]) -> dict[bytes, str]:
    """One fetch's text for each digest, read from what that fetch held
    (page_fetch_texts), whether or not it was already rejected."""
    first = {members[0]["id"]: sha for sha, members in groups.items()}
    rows = db.query(
        "SELECT id, input_content, content_ref FROM page_fetch_texts WHERE id = ANY(%s)",
        (list(first),),
    )
    return {first[r["id"]]: r["input_content"] or "" for r in page_fetches.resolve(rows)}


def step(after: int, through: int) -> tuple[int, int, int]:
    """Judge the texts of fetches (after, through], at most STEP_FETCHES of
    them. Returns the last id read, the fetches newly rejected, and the
    embeddings removed with them."""
    rows = db.query(
        "SELECT id, content_sha256 AS sha FROM page_fetches "
        "WHERE id > %s AND id <= %s ORDER BY id LIMIT %s",
        (after, through, STEP_FETCHES),
    )
    if not rows:
        return through, 0, 0
    shas = {bytes(r["sha"]) for r in rows if r["sha"] is not None}
    groups = {
        sha: members
        for sha, members in page_fetches.members_of(shas).items()
        if len({page_fetches.role(m["title"]) for m in members}) > 1
        and any(m["method"] not in page_fetches.BOARD_RECORDS for m in members)
    }
    texts = _texts(groups) if groups else {}
    foreign = [
        fetch_id
        for sha, members in groups.items()
        for fetch_id in page_fetches.foreign(members, texts.get(sha, ""))
    ]
    rejected: list[int] = []
    removed = 0
    if foreign:
        with connection() as conn, conn.transaction():
            rejected = page_fetches.reject(foreign, conn)
            if rejected:
                removed = conn.execute(
                    "DELETE FROM posting_embeddings e USING page_fetches f "
                    "WHERE f.id = ANY(%s) AND e.url = f.url AND e.content_row_id = f.id",
                    (rejected,),
                ).rowcount
    return rows[-1]["id"], len(rejected), removed


def run(task_id: int) -> dict[str, Any]:
    after, through = _start(), _limit()
    read = rejected = removed = 0
    while after < through and not cancelled(task_id):
        last, newly, gone = step(after, through)
        read += 1
        rejected += newly
        removed += gone
        after = last
        if not checkpoint(
            task_id,
            after,
            through,
            f"judged fetches through {after}: {rejected} not their posting's text",
            {"rejected": rejected, "embeddings_removed": removed},
            payload={"through": after},
        ):
            break
    result = {"through": after, "steps": read, "rejected": rejected, "embeddings_removed": removed}
    logger.info("%s: %s", KIND, result)
    return result


async def handle_judge_page_texts(task_id: int, payload: dict[str, Any]) -> None:
    await asyncio.to_thread(run, task_id)
