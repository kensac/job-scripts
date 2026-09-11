from __future__ import annotations

import logging
import os
import socket
from typing import Any, LiteralString, cast

import dotenv

from core import pricing
from core.pool import connection

logger = logging.getLogger(__name__)


def _as_query(sql: str) -> LiteralString:
    """Same invariant as api.db._as_query: the interpolated fragments here are
    a whitelisted column list (_INSERT_COLUMNS) or a fixed condition, never a
    caller-supplied value. Stated once so the claim is auditable in one place
    instead of per call site.
    """
    return cast("LiteralString", sql)


dotenv.load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

_INSERT_COLUMNS = [
    "config_name",
    "url",
    "check_type",
    "status",
    "reason",
    "model",
    "reasoning_effort",
    "filter_name",
    "prompt_hash",
    "company",
    "job_title",
    "instructions",
    "input_content",
    "parsed_json",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "duration_ms",
    "error",
    "cost_usd",
    "worker",
    "batch_id",
]

_WORKER = os.environ.get("JOBTRACKER_WORKER_NAME") or socket.gethostname()


def add_ai_result(
    url: str,
    status: str,
    reason: str | None = "",
    check_type: str = "",
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    cached_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    filter_name: str | None = None,
    prompt_hash: str | None = None,
    company: str | None = None,
    job_title: str | None = None,
    instructions: str | None = None,
    input_content: str | None = None,
    parsed_json: str | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    config_name: str | None = None,
    batch_id: str | None = None,
) -> None:
    row = {
        # created_at is DELIBERATELY ABSENT: the column defaults to Postgres
        # now(), and letting the database supply it is what keeps every
        # timestamp in this system on ONE clock.
        #
        # It used to be written from Python here, which made
        # `ai_queries.created_at > ai_batches.submitted_at` a comparison
        # between the app host's clock and the database's. That inequality is
        # the whole of #162's rule - a parked reverify may not overturn a
        # closure newer than its evidence - and three worker hosts mean three
        # clocks against one database. A host running slightly fast makes its
        # verdicts look newer than batches submitted after them, and a
        # reverify that should record is discarded as stale. Silently:
        # recorded == 0 is a normal-looking outcome.
        "config_name": config_name or os.environ.get("CONFIG_NAME"),
        "url": url,
        "check_type": check_type,
        "status": status,
        "reason": reason,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "filter_name": filter_name,
        "prompt_hash": prompt_hash,
        "company": company,
        "job_title": job_title,
        "instructions": instructions,
        "input_content": input_content,
        "parsed_json": parsed_json,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "reasoning_tokens": reasoning_tokens,
        "duration_ms": duration_ms,
        "error": error,
        # Priced at write time, not read time: the rate table changes, and a
        # verdict's cost is what it cost when it ran. batch_id is the only
        # signal that this went through the half-price Batch API.
        "cost_usd": pricing.estimate_cost_usd(
            model,
            prompt_tokens,
            completion_tokens,
            cached_tokens=cached_tokens,
            batched=batch_id is not None,
        ),
        "worker": _WORKER,
        "batch_id": batch_id,
    }
    columns = ", ".join(_INSERT_COLUMNS)
    placeholders = ", ".join(f"%({c})s" for c in _INSERT_COLUMNS)
    with connection() as conn:
        conn.execute(_as_query(f"INSERT INTO ai_queries ({columns}) VALUES ({placeholders})"), row)


def get_custom_result(
    url: str, prompt_hash: str, model: str | None = None
) -> dict[str, Any] | None:
    """Latest decided custom result for a url under a specific filter (by hash).

    With `model`, only verdicts produced by that model count; without it, any
    model's verdict is reused.
    """
    clause = " AND model = %s" if model is not None else ""
    params = (url, prompt_hash, model) if model is not None else (url, prompt_hash)
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM ai_queries WHERE url = %s AND check_type = 'custom' "
            f"AND prompt_hash = %s{clause} AND status IN ('passed', 'rejected') "
            "ORDER BY id DESC LIMIT 1",
            params,
        ).fetchone()
    return dict(row) if row else None


# A page shorter than this is a login wall, an error stub or a cookie banner,
# not a posting. It is the threshold every content-consuming sweep already
# used inline; naming it here keeps the three of them from drifting apart.
MIN_CONTENT_CHARS = 200


# The one spelling of "which stored page text feeds the AI for this url".
#
# Prefers a raw 'content' row over the copy attached to a check, then takes the
# newest. Formatted with the url expression to join against, so a sweep over
# jobs passes 'j.url' and a sweep over ai_queries itself passes its own alias;
# every caller gets the same row for the same url either way.
#
# `columns` is what to take from that row. Usually input_content, but a sweep
# deciding WHETHER to re-read a page wants only the row's id: input_content is
# TOASTed and id is not, so asking "has the page changed" costs an index read
# instead of detoasting the whole corpus. Both spellings pick the same row,
# which is the point of having one lateral rather than two.
#
# Deliberately NOT the same query as get_content(), which takes the newest text
# whatever check produced it. Preferring a 'content' row can return older text,
# which is right for extracting stable facts and wrong for deciding whether a
# posting has since closed.
CONTENT_LATERAL = (
    """
        JOIN LATERAL (
            SELECT {columns} FROM ai_queries q
            WHERE q.url = {url} AND q.input_content IS NOT NULL
              AND length(q.input_content) > """
    + str(MIN_CONTENT_CHARS)
    + """
            ORDER BY (q.check_type = 'content') DESC, q.id DESC LIMIT 1
        ) q ON TRUE
"""
)


# Boards a person has asked for and an admin has not switched off. Formatted
# with the source expression to test.
#
# `sources` is the catalogue of scraped boards, so joining it is what makes
# `sources.active = false` mean "stop", rather than only stopping the scrape
# while the checks kept spending - which is how `airtable1` stayed switched off
# and still cost money.
#
# This is the SCRAPER's question: which boards do we fetch pages for. It is
# narrower than AI_ELIGIBLE_JOB below and deliberately so; see there.
SUBSCRIBED_SOURCE = """
    {source} IN (
        SELECT us.source FROM user_sources us
        JOIN sources s ON s.name = us.source AND s.active
    )
"""


# Which postings may cost tokens: the ones a person can actually reach.
# Formatted with the alias of the `jobs` row to test.
#
# One spelling, built on SUBSCRIBED_SOURCE rather than restating it. The gate
# lived only in the content backfill while every sweep that spends tokens
# selected without it. A job was scraped only if someone wanted its source and
# then checked forever regardless: over the seven days to 2026-09-03, 5,801
# calls and 6.7M tokens went to postings no user could open, against 14,345
# calls and 24.7M tokens that reached someone (#303).
#
# Reachable is WIDER than subscribed, and the two must not be collapsed.
# Gating on subscription alone would have stopped re-checking 5,342 active
# `sheet_import` postings - a person's own imported application history, which
# board.py serves to every user and which no board supplies. So two more
# clauses:
#
#   - It did not come from a board at all. A source absent from the catalogue
#     is one a PERSON put there: an upload, a sheet import. There is no
#     subscription to look for. This is also why SUBSCRIBED_SOURCE can
#     inner-join without going quiet on a subscription whose catalogue row went
#     missing - user_sources.source has no foreign key, and this clause catches
#     that case.
#   - It is already on somebody's board. A posting a person kept stays
#     answerable after they unsubscribe from where it came from.
#
# Those two clauses are why the content backfill keeps SUBSCRIBED_SOURCE and
# does not use this. Scraping and spending are different questions: a posting
# on a board already gets its page on demand when it is re-verified, and
# widening the backfill to reach them turned a 525ms query into a 7.5s one to
# queue 4,822 pages nobody had asked to re-fetch.
#
# Evaluated on every sweep rather than stamped onto rows, so subscribing to a
# source makes its jobs eligible on the next cycle and needs no backfill.
# THE WORKING SET, which is not the same question as what a person sees.
#
# A board row means the sweeps carry this posting. It does NOT mean anybody
# can see it: visibility.FULL admits an untouched row only through its
# structural branch, which does not reference user_jobs at all. The two
# meanings share one table and reading the wrong one is how a board question
# gets answered wrongly (2026-09-10).
#
# So this branch is scope, deliberately. A posting somebody tracks keeps being
# checked even after nobody subscribes to the source that found it, which is
# the point: it is their job now, not the board's.
ON_A_BOARD = "EXISTS (SELECT 1 FROM user_jobs uj WHERE uj.job_id = {job}.id)"

AI_ELIGIBLE_JOB = (
    """
    (
        """
    + SUBSCRIBED_SOURCE.format(source="{job}.source").strip()
    + """
        OR NOT EXISTS (SELECT 1 FROM sources s WHERE s.name = {job}.source)
        OR """
    + ON_A_BOARD
    + """
    )
"""
)


# A posting whose latest closed and clearance verdicts both passed. The
# extractors (comp, requirements) select on it: on 2026-09-06 the catalog
# held 74,477 active postings of which 37,438 were verified open, and both
# extractors were paying for the other half, whose numbers nothing reads
# because a closed or restricted posting reaches no board. The narrower
# option, extracting only for postings on someone's board (3,117 that day,
# 4 percent), is not taken yet: a posting reaching a board later would wait
# a cycle for its comp column, and the market table would be built from a
# smaller slice than the filters admit. Written down here so it is a
# decision and not an oversight.
#
# This reads the latest closed and clearance rows in ai_queries. A retention
# policy on that table (none exists; it is the largest table and the
# decision is open) must keep the latest verdict per (url, check_type), or
# a posting whose verdicts age out silently reads as unverified here and
# drops out of both extractors, then re-enters them at cost once re-verified.
VERIFIED_OPEN = """
    (SELECT lc.status FROM ai_queries lc
      WHERE lc.url = {url} AND lc.check_type = 'closed' AND lc.status IN ('passed', 'rejected')
      ORDER BY lc.id DESC LIMIT 1) = 'passed'
    AND (SELECT lc.status FROM ai_queries lc
      WHERE lc.url = {url} AND lc.check_type = 'clearance' AND lc.status IN ('passed', 'rejected')
      ORDER BY lc.id DESC LIMIT 1) = 'passed'
"""


# Custom verdicts contain wrapped input, so they cannot supply raw page text.
_RAW_CONTENT = "check_type != 'custom' AND input_content IS NOT NULL AND input_content != ''"


def get_contents(urls: list[str]) -> dict[str, str]:
    """Newest raw cached content per URL, with the same eligibility as get_content."""
    if not urls:
        return {}
    with connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (url) url, input_content FROM ai_queries "
            "WHERE url = ANY(%s) AND " + _RAW_CONTENT + " ORDER BY url, id DESC",
            (urls,),
        ).fetchall()
    return {row["url"]: row["input_content"] for row in rows}


def get_content(url: str) -> str | None:
    """Most recent non-empty raw scraped content stored for a url.

    Excludes 'custom' rows: those store the wrapped _build_custom_input() text
    (company/title prefix), not raw page content, so reusing them would re-wrap
    the content on every subsequent custom filter.
    """
    with connection() as conn:
        row = conn.execute(
            "SELECT input_content FROM ai_queries WHERE url = %s "
            "AND " + _RAW_CONTENT + " ORDER BY id DESC LIMIT 1",
            (url,),
        ).fetchone()
    return row["input_content"] if row else None


def record_batch_errors(provider_batch_id: str, errors: dict[str, str]) -> None:
    """Every per-request error a batch returned, as the provider wrote it.

    The batch row counts failures; only the text says whether the submission
    was rejected (every request, one reason) or a few inputs were bad. It was
    read at collection and dropped by every handler that skips errored
    results, so 21,525 failed requirements requests on 2026-09-04 left no
    reason anywhere. Stored whole; groom later.
    """
    rows = [(provider_batch_id, cid, err) for cid, err in errors.items() if err]
    if not rows:
        return
    with connection() as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO ai_batch_errors (provider_batch_id, custom_id, error) VALUES (%s, %s, %s)",
            rows,
        )
