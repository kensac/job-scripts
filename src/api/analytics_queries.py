"""Source analytics query definitions. Preserve counting and null semantics here."""

from api import signals
from api.board.person_state import PERSON_STATE
from api.mail import applications
from core import verdict_reads
from core.checks import POSTING_CHECK_NAMES

# 'content' and 'extraction' are not in it, deliberately, because they record
# a scrape attempt rather than a judgement about the posting and neither
# writes a 'rejected' row.
_VERDICT_CHECKS = POSTING_CHECK_NAMES


_INVENTORY_SQL = """
SELECT source,
       count(*) AS total,
       count(*) FILTER (WHERE available) AS active,
       count(*) FILTER (WHERE available IS NOT TRUE) AS inactive,
       count(date_posted) AS with_date_posted,
       count(*) FILTER (WHERE date_posted >= now() - interval '7 days') AS posted_7d,
       count(*) FILTER (WHERE date_posted >= now() - interval '30 days') AS posted_30d,
       percentile_cont(0.5) WITHIN GROUP (
           ORDER BY extract(epoch FROM now() - date_posted) / 86400.0
       ) AS median_age_days,
       percentile_cont(0.9) WITHIN GROUP (
           ORDER BY extract(epoch FROM now() - date_posted) / 86400.0
       ) AS p90_age_days,
       min(created_at) AS first_loaded_at,
       max(created_at) AS last_loaded_at
FROM jobs
GROUP BY source
"""

# Joining jobs before deduping is load-bearing for speed, not just tidiness:
# DISTINCT ON over (url, check_type, id) sorts a 46k-row set keyed by a long
# text url and spills to disk (~1.15s measured on prod). Carrying jobs.id
# through and sorting on (job_id, check_type, id) keeps the same sort in
# memory and costs ~245ms for identical output.
_LATEST_CHECK_PER_JOB = verdict_reads.latest_per(
    "j.id, v.check_type",
    "j.source AS source, v.check_type, v.status",
    "v.check_type = ANY(%(checks)s)",
    join="JOIN jobs j ON j.url = v.url",
)

_FUNNEL_SQL = f"""
WITH latest AS (
    {_LATEST_CHECK_PER_JOB}
)
SELECT source, check_type,
       count(*) AS checked,
       count(*) FILTER (WHERE status = 'passed') AS passed,
       count(*) FILTER (WHERE status = 'rejected') AS rejected
FROM latest GROUP BY source, check_type
"""

_LATEST_FILTER_PER_JOB = verdict_reads.latest_per(
    "j.id, v.prompt_hash",
    "j.source AS source, j.id AS job_id, v.status",
    "v.check_type = 'custom' AND v.prompt_hash IS NOT NULL",
    join="JOIN jobs j ON j.url = v.url",
)

# Custom filters are per-user and a job can be judged by several of them, so
# there are two honest denominators and this returns both: evaluations (one per
# job per filter prompt) and jobs that passed every filter they were put in
# front of, which is the job-level predicate the board itself applies.
_CUSTOM_SQL = f"""
WITH latest AS (
    {_LATEST_FILTER_PER_JOB}
), per_job AS (
    SELECT source, job_id,
           count(*) AS evaluations,
           count(*) FILTER (WHERE status = 'passed') AS passed
    FROM latest GROUP BY source, job_id
)
SELECT source,
       count(*) AS jobs_evaluated,
       count(*) FILTER (WHERE passed = evaluations) AS jobs_passed_all,
       sum(evaluations) AS evaluations,
       sum(passed) AS evaluations_passed
FROM per_job GROUP BY source
"""

# Dead on arrival is defined once, in api.signals, because the per-job
# intelligence pane serves the same number for a single board. Two copies of a
# "first verdict per posting" query is how the two readings would drift.
_FIRST_CLOSED_SQL = signals.first_closed_sql()

# What a board's postings cost to check: the paid calls that asked a check
# about one of its postings, from the call ledger (model_calls). cost_usd is
# summed, never recomputed: it is written at call time against the price in
# force then, so re-deriving it on read would restate history every time the
# price table changes.
#
# A call, not a row. One verify answer writes a closed, a clearance and
# sometimes a board's custom verdict; it was one request and is counted once.
# A page fetch is not a model call and is not counted: read over every row
# ai_queries ever held, a board's "calls" included its fetches as unpriced,
# 2,800,724 calls at 67.0 percent priced on 2026-10-10, against 1,384,802
# paid calls at 100 percent with the same dollars.
#
# A batch item's custom_id is the posting's url for the check purposes; a live
# call backfilled from its verdict reaches the url through that verdict, by
# primary key: two arms rather than one join with a COALESCE, because the
# single join hashed all of ai_queries (11.0 s on production on 2026-10-10,
# against 6.2 s for the two arms and 5.4 s for the old read of every row). A
# live call recorded since the ledger began names no url and is not counted
# here (about ten a week on 2026-10-10).
#
# `priced` separates "this cost nothing" from "we do not know what this
# cost": a model missing from the price table is counted, never summed as zero.
#
# ALL TIME, deliberately, and said out loud in the response. /admin/spend
# defaults to 30 days and this takes no window at all, so a reader comparing
# the two was comparing a lifetime against a month with nothing on either page
# saying so (found by personal-portfolio-e3). All-time is the right window
# HERE: whether a board has earned its keep is a question about its whole life.
_SPEND_SQL = """
WITH calls AS (
    SELECT m.model, m.requests, m.cost_usd, m.total_tokens, m.custom_id AS url
    FROM model_calls m
    WHERE m.purpose IN ('verify', 'reverify', 'filter', 'managed_board', 'manual')
      AND m.custom_id IS NOT NULL
    UNION ALL
    SELECT m.model, m.requests, m.cost_usd, m.total_tokens, q.url
    FROM model_calls m JOIN ai_queries q ON q.id = m.source_id
    WHERE m.purpose IN ('verify', 'reverify', 'filter', 'managed_board', 'manual')
      AND m.source = 'verdict' AND m.provider_batch_id IS NULL
)
SELECT j.source AS source, c.model,
       sum(c.requests) AS calls,
       coalesce(sum(c.requests) FILTER (WHERE c.cost_usd IS NOT NULL), 0) AS priced_calls,
       sum(c.total_tokens) AS total_tokens,
       sum(c.cost_usd) AS cost_usd
FROM calls c JOIN jobs j ON j.url = c.url
GROUP BY j.source, c.model
"""

# last_success_at is when the ingest last ran cleanly; jobs.last_loaded_at (from
# _INVENTORY_SQL) is when it last produced a posting this catalog had not seen.
# They diverge, and the gap is the signal: a board whose ingest has succeeded
# hourly for a week without yielding a new row is serving a frozen feed.
_INGEST_SQL = """
SELECT payload->>'source' AS source,
       count(*) AS runs,
       count(*) FILTER (WHERE status = 'done') AS succeeded,
       count(*) FILTER (WHERE status = 'failed') AS failed,
       max(finished_at) FILTER (WHERE status = 'done') AS last_success_at,
       max(finished_at) FILTER (WHERE status = 'failed') AS last_failure_at
FROM tasks
WHERE kind = 'ingest_source' AND payload->>'source' IS NOT NULL
GROUP BY payload->>'source'
"""

_YIELD_SQL = f"""
SELECT j.source AS source,
       count(*) AS board_rows,
       count(*) FILTER (WHERE uj.status IS NOT NULL AND uj.status <> '') AS with_status,
       count(*) FILTER (WHERE {applications.applied_on("uj")} IS NOT NULL) AS applied,
       count(DISTINCT uj.user_id) AS users
FROM user_jobs uj
JOIN jobs j ON j.id = uj.job_id
WHERE NOT uj.hidden
GROUP BY j.source
"""

# The three labelled populations per source (phase 2b), as (user, posting)
# pairs: acted on (person state), picked by filters (working set), and on a
# computed board (board_visible). board_rows above is counted only as
# apply_rate's denominator.
_POPULATIONS_SQL = f"""
SELECT j.source AS source,
       count(*) FILTER (WHERE p.population = 'acted_on') AS acted_on,
       count(*) FILTER (WHERE p.population = 'working_set') AS working_set,
       count(*) FILTER (WHERE p.population = 'visible') AS visible
FROM (
    SELECT uj.job_id, 'acted_on' AS population FROM user_jobs uj WHERE {PERSON_STATE}
    UNION ALL SELECT job_id, 'working_set' FROM user_job_working_set
    UNION ALL SELECT job_id, 'visible' FROM board_visible
) p
JOIN jobs j ON j.id = p.job_id
GROUP BY j.source
"""

# Overlap keys on (company, title) because a url cannot express it: jobs.url is
# unique table-wide, so a posting belongs to exactly one source by
# construction and cross-source duplication is invisible at the url level.
#
# min(source) <> max(source) over the partition is "more than one distinct
# source carries this posting" - count(DISTINCT) is not a window function, and
# computing the distinct count as a separate aggregate means hashing the pairs
# and then joining them back, which measured 477ms against prod versus 358ms
# for the single windowed pass.
_OVERLAP_SQL = """
SELECT source,
       count(*) AS keyed_jobs,
       count(*) FILTER (WHERE lo <> hi) AS shared_with_other_source
FROM (
    SELECT source, min(source) OVER w AS lo, max(source) OVER w AS hi
    FROM (
        SELECT source, lower(btrim(company)) AS company, lower(btrim(title)) AS title
        FROM jobs WHERE company <> '' AND title <> ''
    ) keyed
    WINDOW w AS (PARTITION BY company, title)
) spread
GROUP BY source
"""

_OVERLAP_PARTNERS_SQL = """
WITH keyed AS (
    SELECT source, lower(btrim(company)) AS company, lower(btrim(title)) AS title
    FROM jobs WHERE company <> '' AND title <> ''
), mine AS (
    SELECT company, title FROM keyed WHERE source = %(source)s
)
SELECT k.source AS source, count(*) AS shared_postings
FROM keyed k JOIN mine m ON m.company = k.company AND m.title = k.title
WHERE k.source <> %(source)s
GROUP BY k.source ORDER BY shared_postings DESC
"""
