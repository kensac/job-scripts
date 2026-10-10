"""Copies the pay stored on jobs into job_comp, while both are written.

Idempotent by predicate: a posting qualifies while its extracted pay on jobs
differs from its job_comp row, or it has none. The comp sweep writes both, so
after the first run only answers an older image wrote to jobs alone qualify,
and a run that finds none only reads. The worker offers it every cycle until
a finished run copied nothing.

What it cannot know about a copied answer (the model, the hash of the text it
read) is left NULL; a row it changes loses both, because they described the
answer it replaced.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import db
from tasks.runtime import cancelled, set_progress

logger = logging.getLogger(__name__)

# Extracted postings per statement: 110,243 of about 1M jobs rows on
# 2026-10-10, so about 23 statements a run. Ids are sparse (the largest was
# 38,381,774), so a batch is the next extracted rows by id, not an id range.
BATCH = 5_000

_COPY = """
    WITH batch AS (
        SELECT id, url, comp_min, comp_max, comp_text, comp_period, comp_currency,
               comp_basis, comp_content_row_id
        FROM jobs WHERE comp_extracted AND id > %(after)s ORDER BY id LIMIT %(limit)s
    ), copied AS (
        INSERT INTO job_comp (url, comp_min, comp_max, comp_text, comp_period,
                              comp_currency, comp_basis, content_row_id)
        SELECT url, comp_min, comp_max, comp_text, comp_period, comp_currency,
               comp_basis, comp_content_row_id
        FROM batch ORDER BY url
        ON CONFLICT (url) DO UPDATE SET
            comp_min = EXCLUDED.comp_min, comp_max = EXCLUDED.comp_max,
            comp_text = EXCLUDED.comp_text, comp_period = EXCLUDED.comp_period,
            comp_currency = EXCLUDED.comp_currency, comp_basis = EXCLUDED.comp_basis,
            content_row_id = EXCLUDED.content_row_id,
            model = NULL, content_hash = NULL, extracted_at = now()
        WHERE (job_comp.comp_min, job_comp.comp_max, job_comp.comp_text,
               job_comp.comp_period, job_comp.comp_currency, job_comp.comp_basis,
               job_comp.content_row_id)
              IS DISTINCT FROM
              (EXCLUDED.comp_min, EXCLUDED.comp_max, EXCLUDED.comp_text,
               EXCLUDED.comp_period, EXCLUDED.comp_currency, EXCLUDED.comp_basis,
               EXCLUDED.content_row_id)
        RETURNING 1
    )
    SELECT (SELECT max(id) FROM batch) AS last, (SELECT count(*) FROM copied) AS n
"""

# Every extracted posting whose job_comp row is missing or says something
# else. Zero is what readers need before they move.
DIFFERING = """
    SELECT count(*) AS n FROM jobs j LEFT JOIN job_comp c ON c.url = j.url
    WHERE j.comp_extracted AND (c.url IS NULL OR
        (c.comp_min, c.comp_max, c.comp_text, c.comp_period, c.comp_currency,
         c.comp_basis, c.content_row_id)
        IS DISTINCT FROM
        (j.comp_min, j.comp_max, j.comp_text, j.comp_period, j.comp_currency,
         j.comp_basis, j.comp_content_row_id))
"""


def copy_batch(after: int, limit: int = BATCH) -> tuple[int | None, int]:
    """The last jobs id the batch read (None past the end), and rows copied."""
    row = db.query_one(_COPY, {"after": after, "limit": limit})
    assert row is not None
    return row["last"], int(row["n"])


async def handle_copy_job_comp(task_id: int, payload: dict[str, Any]) -> None:
    copied, read, after = 0, 0, 0
    while not cancelled(task_id):
        last, n = await asyncio.to_thread(copy_batch, after)
        if last is None:
            break
        copied, read, after = copied + n, read + BATCH, last
        set_progress(task_id, copied, read, f"copied {copied} pay rows")
    left = db.query_one(DIFFERING)
    extra = {"copied": copied, "differing": int(left["n"]) if left else 0}
    set_progress(task_id, copied, read, f"copied {copied} pay rows", extra)
    logger.info("copy_job_comp: %s", extra)
