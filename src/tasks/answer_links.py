"""Points older answers at the page fetch they judged and the call that paid.

Answers carried copies: input_content (the page they were asked about) and
the usage and cost of their call. Answers written since this task shipped
point at both instead (page_fetch_id, model_call_id). This fills the pointers
on the answers written before, so the copies can be cleared and dropped
(docs/agents/architecture-migration.md, phase 8).

A pointer is set only where it is exact:

- page: a fetch of the url from which core.answer_inputs rebuilds the copy
  byte for byte, the nearest at or before the answer, else the nearest
  after. Where no fetch holds the text, the text the answer saw becomes a
  fetch first (method 'verification', the answer's id and time, as the move
  out of ai_queries did), so no text is lost when the copy is cleared. Where
  that answer is newer than every fetch of its url, its text becomes the
  url's current page, which it is: the newest text the system saw (1,948
  `fulltime` answers from 2026-06 on 851 urls, 54 of them active postings,
  measured 2026-10-10; decided the same day).
- call: a batched answer's item, `(batch_id, url) = (provider_batch_id,
  custom_id)`; a live one copied from its verdict (`source_id`); or a live
  call booked apart from its verdict, matched on model, tokens and the minute
  after it, only where each has exactly one candidate. Its duration_ms, which
  only the verdict held, is copied onto the call.

One id range per transaction, idempotent by predicate: a rerun only reads
what is already linked. The worker queues a run each cycle until one links
nothing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import db
from core import answer_inputs
from tasks.runtime import cancelled, set_progress

logger = logging.getLogger(__name__)

# Ids per transaction. ai_queries held 1.96M answers over about 2.9M ids on
# 2026-10-10 (the gaps are the fetches that moved out), so about 580 ranges.
# A range rewrites at most this many row versions and decompresses each
# candidate's copy and its url's fetches, a few seconds' work.
CHUNK = 5_000

_RANGE = "q.id >= %(lo)s AND q.id < %(hi)s"
_UNLINKED_COPY = (
    "q.page_fetch_id IS NULL AND q.url IS NOT NULL "
    "AND q.input_content IS NOT NULL AND q.input_content <> ''"
)
_REBUILDS = (
    "f.url = q.url AND f.content IS NOT NULL "
    f"AND {answer_inputs.sql('q', 'f.content')} = q.input_content"
)

# The text an answer saw that no fetch of its url holds, stored as a fetch.
# One per distinct text, under the first answer that saw it; ON CONFLICT
# because the move out of ai_queries already used some answers' ids.
_STORE_UNHELD = f"""
    INSERT INTO page_fetches (id, url, status, method, content, worker, created_at)
    SELECT DISTINCT ON (q.url, md5({answer_inputs.seen("q")}))
           q.id, q.url, 'passed', 'verification', {answer_inputs.seen("q")}, q.worker, q.created_at
    FROM ai_queries q
    WHERE {_RANGE} AND {_UNLINKED_COPY}
      AND {answer_inputs.header_matches("q")} AND {answer_inputs.seen("q")} <> ''
      AND NOT EXISTS (SELECT 1 FROM page_fetches f WHERE {_REBUILDS})
    ORDER BY q.url, md5({answer_inputs.seen("q")}), q.id
    ON CONFLICT (id) DO NOTHING
"""

_LINK_PAGES = f"""
    UPDATE ai_queries a SET page_fetch_id = m.fetch_id
    FROM (
        SELECT q.id, (
            SELECT f.id FROM page_fetches f WHERE {_REBUILDS}
            ORDER BY f.id > q.id, abs(f.id - q.id) LIMIT 1
        ) AS fetch_id
        FROM ai_queries q WHERE {_RANGE} AND {_UNLINKED_COPY}
    ) m
    WHERE a.id = m.id AND m.fetch_id IS NOT NULL
"""

_LINK_BATCHED = f"""
    UPDATE ai_queries q SET model_call_id = m.id FROM model_calls m
    WHERE {_RANGE} AND q.model_call_id IS NULL AND q.batch_id IS NOT NULL
      AND m.provider_batch_id = q.batch_id AND m.custom_id = q.url
"""

_LINK_COPIED = f"""
    UPDATE ai_queries q SET model_call_id = m.id FROM model_calls m
    WHERE {_RANGE} AND q.model_call_id IS NULL AND q.batch_id IS NULL
      AND m.source = 'verdict' AND m.source_id = q.id
"""

# A live call its caller booked after run_check wrote the verdict: through
# the old usage ledger (copied as 'usage'), or as a 'call' by an image older
# than this task's release. Nothing names the pair, so a pair is taken only
# when neither side has another candidate.
_LINK_BOOKED = f"""
    UPDATE ai_queries a SET model_call_id = p.call_id
    FROM (
        SELECT q.id, m.id AS call_id,
               count(*) OVER (PARTITION BY q.id) AS calls,
               count(*) OVER (PARTITION BY m.id) AS answers
        FROM ai_queries q JOIN model_calls m
          ON m.provider_batch_id IS NULL AND m.source IN ('usage', 'call')
         AND m.model = q.model AND m.total_tokens = q.total_tokens
         AND m.prompt_tokens = q.prompt_tokens AND m.completion_tokens = q.completion_tokens
         AND m.created_at >= q.created_at AND m.created_at < q.created_at + interval '1 minute'
        WHERE {_RANGE} AND q.model_call_id IS NULL AND q.batch_id IS NULL
          AND q.total_tokens > 0
    ) p
    WHERE a.id = p.id AND p.calls = 1 AND p.answers = 1
"""

_COPY_DURATIONS = f"""
    UPDATE model_calls m SET duration_ms = q.duration_ms FROM ai_queries q
    WHERE {_RANGE} AND q.model_call_id = m.id AND q.duration_ms IS NOT NULL
      AND m.duration_ms IS NULL AND m.provider_batch_id IS NULL
"""

_STEPS = {
    "fetches_stored": _STORE_UNHELD,
    "pages_linked": _LINK_PAGES,
    "batched_calls_linked": _LINK_BATCHED,
    "copied_calls_linked": _LINK_COPIED,
    "booked_calls_linked": _LINK_BOOKED,
    "durations_copied": _COPY_DURATIONS,
}


def link_range(lo: int, hi: int) -> dict[str, int]:
    """Every step over answers with ids in [lo, hi), in one transaction."""
    with db.transaction():
        return {step: db.execute_count(sql, {"lo": lo, "hi": hi}) for step, sql in _STEPS.items()}


async def handle_link_answers(task_id: int, payload: dict[str, Any]) -> None:
    span = db.query_one("SELECT min(id) AS lo, max(id) AS hi FROM ai_queries")
    if not span or span["lo"] is None:
        set_progress(task_id, 0, 0, "no answers")
        return
    lo, hi = int(span["lo"]), int(span["hi"]) + 1
    done = dict.fromkeys(_STEPS, 0)
    set_progress(task_id, 0, 0, "linking answers")
    for start in range(lo, hi, CHUNK):
        if cancelled(task_id):
            return
        counts = await asyncio.to_thread(link_range, start, min(start + CHUNK, hi))
        for step, n in counts.items():
            done[step] += n
        set_progress(task_id, 0, 0, f"linked through id {start + CHUNK}", {"linked": done})
    # total is what this run changed: a run that changed nothing reads 0,
    # which is what stops the worker queueing another.
    total = sum(done.values())
    set_progress(task_id, total, total, "answers linked", {"linked": done})
    logger.info("link_answers: %s", done)
