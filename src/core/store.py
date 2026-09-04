from __future__ import annotations

import atexit
import logging
import os
import socket
from typing import Any, LiteralString, cast

import dotenv
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from core import pricing

logger = logging.getLogger("jobtracker_store")


def _as_query(sql: str) -> LiteralString:
    """Same invariant as api.db._as_query: the interpolated fragments here are
    whitelisted column lists (_PREFETCH_COLS, _INSERT_COLUMNS) or fixed
    conditions, never a caller-supplied value. Stated once so the claim is
    auditable in one place instead of per call site.
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

_pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=10,
    kwargs={"row_factory": dict_row},
    open=True,
)
atexit.register(_pool.close)

# Read-through caches populated by prefetch() to avoid a network round-trip per
# url. Each maps url -> latest decided row (or None for a confirmed miss). Only
# `status` is read off these rows, so the heavy text columns are left out.
_latest_cache: dict[str, dict[str, dict[str, Any] | None]] = {}
_custom_cache: dict[str, dict[str, dict[str, Any] | None]] = {}

# Heavy text columns no caller reads off a verdict row; omitted to save bandwidth
# and filled back as None so a cached row keeps the same shape as a full row.
_PREFETCH_OMIT = ("input_content", "instructions", "parsed_json")
_PREFETCH_COLS = ["id"] + [c for c in _INSERT_COLUMNS if c not in _PREFETCH_OMIT]


def _prefetch_row(r: dict[str, Any]) -> dict[str, Any]:
    d = dict(r)
    for c in _PREFETCH_OMIT:
        d[c] = None
    return d


def prefetch(
    urls: list[str],
    check_types: tuple = ("closed", "clearance"),
    prompt_hashes: tuple = (),
) -> None:
    """Bulk-load latest-decided verdicts for a batch of urls into the caches.

    One query per check_type / prompt_hash instead of one per url, so the
    per-job cache lookups become in-memory hits.
    """
    unique = list({u for u in urls if u})
    if not unique:
        return
    cols = ", ".join(_PREFETCH_COLS)
    with _pool.connection() as conn:
        for check_type in check_types:
            sql = _as_query(
                f"SELECT DISTINCT ON (url) {cols} FROM ai_queries "
                "WHERE url = ANY(%s) AND check_type = %s "
                "AND status IN ('passed', 'rejected') ORDER BY url, id DESC"
            )
            rows = conn.execute(
                sql,
                (unique, check_type),
            ).fetchall()
            found = {r["url"]: _prefetch_row(r) for r in rows}
            for u in unique:
                _latest_cache.setdefault(u, {})[check_type] = found.get(u)
        for prompt_hash in prompt_hashes:
            rows = conn.execute(
                _as_query(
                    f"SELECT DISTINCT ON (url) {cols} FROM ai_queries "
                    "WHERE url = ANY(%s) AND check_type = 'custom' AND prompt_hash = %s "
                    "AND status IN ('passed', 'rejected') ORDER BY url, id DESC"
                ),
                (unique, prompt_hash),
            ).fetchall()
            found = {r["url"]: _prefetch_row(r) for r in rows}
            for u in unique:
                _custom_cache.setdefault(u, {})[prompt_hash] = found.get(u)


def _schema_present() -> bool:
    """Whether ai_queries already exists.

    The pool uses dict_row, so this must read the column by name - indexing
    row[0] raises KeyError, and swallowing that turned "schema exists" into
    "schema missing", which sent a read-only connection into CREATE TABLE.
    Only connection failures are caught: anything else is a real fault and
    should surface rather than be reported as an absent schema.
    """
    try:
        with _pool.connection() as conn:
            row = conn.execute("SELECT to_regclass('public.ai_queries') AS oid").fetchone()
    except psycopg.OperationalError:
        return False
    return bool(row and row["oid"])


def init_db() -> None:
    """Create the ai_queries table and its indexes if absent.

    Runs at import, so it must tolerate a connection that cannot do DDL: a
    read-only role against an already-provisioned database is a legitimate way
    to use this code (CI runs the integration suite that way), and crashing on
    import would make that impossible. Missing privileges are only a problem
    when the schema is also missing, which the caller finds out immediately.
    """
    if _schema_present():
        return
    with _pool.connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_queries (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                config_name TEXT,
                url TEXT,
                check_type TEXT,
                status TEXT,
                reason TEXT,
                model TEXT,
                reasoning_effort TEXT,
                filter_name TEXT,
                prompt_hash TEXT,
                company TEXT,
                job_title TEXT,
                instructions TEXT,
                input_content TEXT,
                parsed_json TEXT,
                prompt_tokens BIGINT,
                completion_tokens BIGINT,
                total_tokens BIGINT,
                cached_tokens BIGINT,
                reasoning_tokens BIGINT,
                duration_ms BIGINT,
                error TEXT,
                cost_usd NUMERIC(12, 6)
            )
            """
        )
        conn.execute("ALTER TABLE ai_queries ADD COLUMN IF NOT EXISTS worker TEXT")
        conn.execute("ALTER TABLE ai_queries ADD COLUMN IF NOT EXISTS batch_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_queries_batch ON ai_queries(batch_id)")
        for stmt in (
            "CREATE INDEX IF NOT EXISTS idx_ai_queries_url ON ai_queries(url)",
            "CREATE INDEX IF NOT EXISTS idx_ai_queries_url_check ON ai_queries(url, check_type)",
            "CREATE INDEX IF NOT EXISTS idx_ai_queries_status ON ai_queries(status)",
            "CREATE INDEX IF NOT EXISTS idx_ai_queries_check_type ON ai_queries(check_type)",
            "CREATE INDEX IF NOT EXISTS idx_ai_queries_created_at ON ai_queries(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_ai_queries_prompt_hash ON ai_queries(check_type, prompt_hash)",
        ):
            conn.execute(stmt)
    # Trigram indexes make the admin search (ILIKE %q%) index-backed instead of
    # a sequential scan; separate connection so a missing extension (no
    # superuser) can't poison the main init transaction.
    try:
        with _pool.connection() as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            for col in ("url", "company", "job_title", "reason"):
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_ai_queries_{col}_trgm "
                    f"ON ai_queries USING gin ({col} gin_trgm_ops)"
                )
    except Exception:
        # Trigram indexes need the pg_trgm extension, which needs superuser.
        # Their absence only makes admin search slower, so a database that
        # cannot create them must still start - but say so rather than
        # swallowing it, or a missing index looks like a mystery slowdown.
        logger.warning("could not create trigram indexes (pg_trgm unavailable?)")


def add_ai_result(
    url: str,
    status: str,
    reason: str = "",
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
    with _pool.connection() as conn:
        conn.execute(_as_query(f"INSERT INTO ai_queries ({columns}) VALUES ({placeholders})"), row)
    sub = _latest_cache.get(url)
    if sub is not None:
        sub.pop(check_type, None)
    if check_type == "custom" and prompt_hash is not None:
        csub = _custom_cache.get(url)
        if csub is not None:
            csub.pop(prompt_hash, None)


def get_ai_result(url: str) -> dict[str, Any] | None:
    with _pool.connection() as conn:
        row = conn.execute(
            "SELECT * FROM ai_queries WHERE url = %s ORDER BY id DESC LIMIT 1", (url,)
        ).fetchone()
    return dict(row) if row else None


def get_latest(url: str, check_type: str) -> dict[str, Any] | None:
    """Latest *decided* (passed/rejected) result for a url+check_type.

    Ignores 'failed' rows so failed checks are retried rather than cached.
    """
    sub = _latest_cache.get(url)
    if sub is not None and check_type in sub:
        return sub[check_type]
    with _pool.connection() as conn:
        row = conn.execute(
            "SELECT * FROM ai_queries WHERE url = %s AND check_type = %s "
            "AND status IN ('passed', 'rejected') ORDER BY id DESC LIMIT 1",
            (url, check_type),
        ).fetchone()
    return dict(row) if row else None


def get_custom_result(
    url: str, prompt_hash: str, model: str | None = None
) -> dict[str, Any] | None:
    """Latest decided custom result for a url under a specific filter (by hash).

    With `model`, only verdicts produced by that model count (bypasses the
    in-memory cache); without it, any model's verdict is reused.
    """
    if model is None:
        sub = _custom_cache.get(url)
        if sub is not None and prompt_hash in sub:
            return sub[prompt_hash]
    clause = " AND model = %s" if model is not None else ""
    params = (url, prompt_hash, model) if model is not None else (url, prompt_hash)
    with _pool.connection() as conn:
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
AI_ELIGIBLE_JOB = (
    """
    (
        """
    + SUBSCRIBED_SOURCE.format(source="{job}.source").strip()
    + """
        OR NOT EXISTS (SELECT 1 FROM sources s WHERE s.name = {job}.source)
        OR EXISTS (SELECT 1 FROM user_jobs uj WHERE uj.job_id = {job}.id)
    )
"""
)


def get_content(url: str) -> str | None:
    """Most recent non-empty raw scraped content stored for a url.

    Excludes 'custom' rows: those store the wrapped _build_custom_input() text
    (company/title prefix), not raw page content, so reusing them would re-wrap
    the content on every subsequent custom filter.
    """
    with _pool.connection() as conn:
        row = conn.execute(
            "SELECT input_content FROM ai_queries WHERE url = %s "
            "AND check_type != 'custom' "
            "AND input_content IS NOT NULL AND input_content != '' "
            "ORDER BY id DESC LIMIT 1",
            (url,),
        ).fetchone()
    return row["input_content"] if row else None


def is_prelim_rejected(url: str) -> bool:
    """Rejected by a prompt-independent check (closed or clearance)."""
    closed = get_latest(url, "closed")
    if closed and closed["status"] == "rejected":
        return True
    clearance = get_latest(url, "clearance")
    return bool(clearance and clearance["status"] == "rejected")


def is_url_rejected(url: str) -> bool:
    result = get_ai_result(url)
    return result is not None and result.get("status") == "rejected"


def is_url_passed(url: str) -> bool:
    result = get_ai_result(url)
    return result is not None and result.get("status") == "passed"


def is_url_failed(url: str) -> bool:
    result = get_ai_result(url)
    return result is not None and result.get("status") == "failed"


def _latest_per_url_where(condition: str, params: tuple) -> list[dict[str, Any]]:
    with _pool.connection() as conn:
        sql = _as_query(
            "SELECT * FROM ai_queries q WHERE id = "
            "(SELECT MAX(id) FROM ai_queries WHERE url = q.url) "
            f"AND {condition}"
        )
        rows = conn.execute(
            sql,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_all_failed_jobs() -> list[dict[str, Any]]:
    return _latest_per_url_where("status = %s", ("failed",))


def get_all_custom_filter_jobs() -> list[dict[str, Any]]:
    return _latest_per_url_where("check_type = %s", ("custom",))


init_db()
