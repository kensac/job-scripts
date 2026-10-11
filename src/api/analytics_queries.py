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

# Count paid checks for a board's postings from `model_calls`. Sum the
# recorded cost_usd so later price changes cannot rewrite historical cost.
# One request counts once even when it writes closed, clearance, and custom
# verdicts. Page fetches are not model calls. Counting every ai_queries row
# reported 2,800,724 calls at 67.0% priced on 2026-10-10; counting paid
# requests reported 1,384,802 at 100% priced with the same total dollars.
#
# Batch custom_id identifies the posting URL. A live call backfilled from a
# verdict gets its URL through that verdict's primary key. Separate query
# arms avoid hashing all ai_queries: 6.2 s on production 2026-10-10, against
# 11.0 s for the combined join and 5.4 s for the former full-row read.
# Recent live calls with no URL are excluded (about ten weekly on that date).
# `priced` distinguishes a free call from one missing a price record.
#
# This response covers all time. /admin/spend defaults to 30 days; a board's
# lifetime cost needs an explicit all-time window for a valid comparison.
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
