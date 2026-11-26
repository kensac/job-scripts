"""Source analytics query definitions. Preserve counting and null semantics here."""

from api import signals
from api.board.person_state import PERSON_STATE
from api.mail import applications
from core import latest_posting_checks, verdict_reads
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

# Each posting's latest answer to each check, read as a set from the
# projection (core.latest_posting_checks.CURRENT) and hash-joined to its
# source. Built here from `verdicts` it took 7.2 s on a production copy
# (2026-11-26).
_FUNNEL_SQL = f"""
SELECT j.source, c.check_type,
       count(*) AS checked,
       count(*) FILTER (WHERE c.status = 'passed') AS passed,
       count(*) FILTER (WHERE c.status = 'rejected') AS rejected
FROM ({latest_posting_checks.CURRENT}) c
JOIN jobs j ON j.id = c.job_id
WHERE c.check_type = ANY(%(checks)s)
GROUP BY j.source, c.check_type
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
# arms avoid hashing all ai_queries. Each arm names its call's source before
# anything is summed, so the sums group on (source, model), a few thousand
# groups. Summed per (model, url) first, as this did, the planner grouped a
# million urls, spilled them to disk and probed jobs_url_key once per url:
# 24.6 s on a production copy (2026-11-26), against 3.8 s with the same
# totals.
# Recent live calls with no URL are excluded (about ten weekly on 2026-10-10).
# `priced` distinguishes a free call from one missing a price record.
#
# This response covers all time. /admin/spend defaults to 30 days; a board's
# lifetime cost needs an explicit all-time window for a valid comparison.
_SPEND_SQL = """
WITH calls AS (
    SELECT j.source, m.model, m.requests, m.cost_usd, m.total_tokens
    FROM model_calls m JOIN jobs j ON j.url = m.custom_id
    WHERE m.purpose IN ('verify', 'reverify', 'filter', 'managed_board', 'manual')
      AND m.custom_id IS NOT NULL
    UNION ALL
    SELECT j.source, m.model, m.requests, m.cost_usd, m.total_tokens
    FROM model_calls m
    JOIN ai_queries q ON q.id = m.source_id
    JOIN jobs j ON j.url = q.url
    WHERE m.purpose IN ('verify', 'reverify', 'filter', 'managed_board', 'manual')
      AND m.source = 'verdict' AND m.provider_batch_id IS NULL
)
SELECT source, model,
       sum(requests) AS calls,
       coalesce(sum(requests) FILTER (WHERE cost_usd IS NOT NULL), 0) AS priced_calls,
       sum(total_tokens) AS total_tokens,
       sum(cost_usd) AS cost_usd
FROM calls
GROUP BY source, model
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
# One pass gives both readings: per source, its keyed postings and how many
# of them another source also carries (partner NULL), and per pair of
# sources, how many postings they share (the detail page's partners: each of
# one source's postings counted against each matching posting of the other).
# The pairs are built only from keys more than one source carries. Asked per
# source, as the detail page did, the partner join took 13.6 s a request; for
# every pair at once it takes 9.2 s, read from the cached page
# (production copy, 2026-11-26).
_OVERLAP_SQL = """
WITH per AS MATERIALIZED (
    SELECT source, lower(btrim(company)) COLLATE "C" AS company,
           lower(btrim(title)) COLLATE "C" AS title, count(*) AS n
    FROM jobs WHERE company <> '' AND title <> ''
    GROUP BY 1, 2, 3
), shared AS MATERIALIZED (
    SELECT company, title FROM per GROUP BY company, title HAVING count(*) > 1
), shared_per AS MATERIALIZED (
    SELECT p.source, p.company, p.title, p.n
    FROM per p JOIN shared s USING (company, title)
)
SELECT p.source, NULL::text AS partner,
       sum(p.n)::bigint AS keyed_jobs,
       coalesce(sum(p.n) FILTER (WHERE s.company IS NOT NULL), 0)::bigint AS shared
FROM per p LEFT JOIN shared s ON s.company = p.company AND s.title = p.title
GROUP BY p.source
UNION ALL
SELECT a.source, b.source, NULL, sum(a.n * b.n)::bigint
FROM shared_per a JOIN shared_per b
  ON b.company = a.company AND b.title = a.title AND b.source <> a.source
GROUP BY a.source, b.source
"""
