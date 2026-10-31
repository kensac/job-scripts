"""Page text that nothing reads, held by reference; the rest inline.

Where a fetch's text lives is a function of one rule, COLD, so the stored
shape always matches the current settings (docs/agents/sources-and-boards.md,
"A page's text can be held by reference"). Each hourly run moves inline rows
the rule selects into verified bundles and brings back inline every held row
it no longer selects: a posting that reopened, a longer window, or the
setting switched off. Nothing is deleted; a moved row keeps its id, so every
content_row_id and page_fetch_id that names it stays true.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from api import db
from core import page_fetches
from core.payloads.listings import put_values
from core.payloads.objects import BUNDLE_MAX_BYTES, PayloadStore, encode_payload
from core.store import MIN_CONTENT_CHARS
from tasks.runtime import cancelled, set_progress

logger = logging.getLogger(__name__)

# A row older than the window whose text no reader of the latest page reaches:
# a newer fetch of its page holds text CONTENT_LATERAL prefers, or its posting
# has been unavailable, with no source observing it, for the whole window. On
# 2026-10-31, 98.8% of postings that reappeared did so within 14 days.
COLD = f"""(
    f.created_at < now() - make_interval(days => %(days)s) AND (
        EXISTS (SELECT 1 FROM page_texts n WHERE n.url = f.url AND n.id > f.id
                AND n.chars > {MIN_CONTENT_CHARS})
        OR EXISTS (
            SELECT 1 FROM jobs j WHERE j.url = f.url AND NOT COALESCE(j.available, false)
              AND NOT EXISTS (
                SELECT 1 FROM source_observations o WHERE o.job_id = j.id
                  AND o.at > now() - make_interval(days => %(days)s)))))"""


def _rule() -> tuple[str, dict[str, Any]]:
    if not db.get_config("page_text_tiering_enabled"):
        return "FALSE", {}
    return COLD, {"days": int(db.get_config("page_text_cold_after_days"))}


def move(limit: int) -> int:
    """Move up to `limit` inline rows the rule selects, oldest first so each
    run's writes fall in few ledger hours. Uploaded and read back before any
    row is cleared (PayloadStore.put_bundle)."""
    cold, params = _rule()
    rows = db.query(
        f"SELECT f.id, f.content FROM page_fetches f "
        f"WHERE f.content IS NOT NULL AND f.content <> '' AND {cold} "
        "ORDER BY f.id LIMIT %(limit)s",
        {**params, "limit": limit},
    )
    if not rows:
        return 0
    digests = {r["id"]: hashlib.sha256(encode_payload(r["content"])).hexdigest() for r in rows}
    links = put_values({digests[r["id"]]: r["content"] for r in rows}, PayloadStore.from_env())
    return db.execute_count(
        """
        UPDATE page_fetches f SET content = NULL, content_sha256 = m.member,
               content_object = m.object, content_object_size = m.size, content_chars = m.chars
        FROM unnest(%(ids)s::bigint[], %(members)s::bytea[], %(objects)s::bytea[],
                    %(sizes)s::int[], %(chars)s::int[]) AS m(id, member, object, size, chars)
        WHERE f.id = m.id AND f.content IS NOT NULL
        """,
        {
            "ids": [r["id"] for r in rows],
            "members": [bytes.fromhex(digests[r["id"]]) for r in rows],
            "objects": [links[digests[r["id"]]][0] for r in rows],
            "sizes": [links[digests[r["id"]]][1] for r in rows],
            "chars": [len(r["content"]) for r in rows],
        },
    )


def pull_back(limit: int) -> int:
    """Bring back inline up to `limit` held rows the rule no longer selects."""
    cold, params = _rule()
    rows = page_fetches.resolve(
        db.query(
            f"SELECT p.id, p.input_content, p.content_ref FROM page_texts p "
            f"JOIN page_fetches f ON f.id = p.id "
            f"WHERE f.content_object IS NOT NULL AND NOT {cold} "
            "ORDER BY f.id LIMIT %(limit)s",
            {**params, "limit": limit},
        )
    )
    if not rows:
        return 0
    return db.execute_count(
        """
        UPDATE page_fetches f SET content = m.content, content_sha256 = NULL,
               content_object = NULL, content_object_size = NULL, content_chars = NULL
        FROM unnest(%(ids)s::bigint[], %(texts)s::text[]) AS m(id, content)
        WHERE f.id = m.id AND f.content_object IS NOT NULL
        """,
        {"ids": [r["id"] for r in rows], "texts": [r["input_content"] for r in rows]},
    )


# Rows per move or pull-back step: a bundle holds at most BUNDLE_MAX_BYTES, and
# a page averages about 10 KB of JSON, so a step is one or two bundles.
_STEP = max(1, BUNDLE_MAX_BYTES // 10_000)


async def handle_tier_page_text(task_id: int, payload: dict[str, Any]) -> None:
    budget = int(db.get_config("page_text_tiering_per_cycle"))
    moved = pulled = 0
    # Pull-back first: a reopened posting's page is wanted inline now.
    while pulled < budget and not cancelled(task_id):
        n = await asyncio.to_thread(pull_back, min(_STEP, budget - pulled))
        pulled += n
        if not n:
            break
    while moved < budget and not cancelled(task_id):
        n = await asyncio.to_thread(move, min(_STEP, budget - moved))
        moved += n
        set_progress(task_id, moved, budget, f"held {moved}, brought back {pulled}")
        if not n:
            break
    set_progress(
        task_id,
        moved + pulled,
        moved + pulled,
        f"held {moved}, brought back {pulled}",
        extra={"held": moved, "brought_back": pulled},
    )
    logger.info("tier_page_text: held %s, brought back %s", moved, pulled)
