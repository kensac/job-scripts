"""Company name query definitions and their measured semantics."""

from api.mail.current import current_event, current_match
from core import verdict_reads

# A count is not a rate: presence is the only threshold for applications.
APPLICATIONS_MIN = 1
# The open share renders as counts ("9 of 14 still answer"), not a rate, so no
# binomial floor applies; below five the phrase is not worth a sentence. It still
# omits the field for about 92% of names (616 of 7,564 reach it).
OPEN_MIN_CHECKED = 5

# Keyed by lower(btrim(company)).
# - Display spelling uses hash-aggregated spelling counts + DISTINCT ON instead
#   of mode() (2.7-3.2 s vs 3.6-4.2 s, production 2026-11-01).
# - Pay aggregates over job_comp before joining to avoid sort spill (2.9 s vs
#   4.3 s joining job_comp to all jobs first, production 2026-10-10).
# - Comp splits three ways: a job_comp row says the extractor ran, an amount that
#   it found one. Ran-and-found-nothing cannot tell "stated no pay" from "we
#   missed it", so it is its own bucket, named for what we did.
# - `apps` is strictly scoped to `user_id` and reads `applications` (including
#   mail-derived rows without a `user_jobs` entry), excluding dismissed rows.
#   Reading `user_jobs` reported 605 companies when 1,283 had evidence; a
#   dismissal says the row should not exist, so counting it counts a mistake.
_BASE_CTES = """
WITH seen AS (
    SELECT DISTINCT ON (company_key) company_key, company AS company_name,
           sum(n) OVER (PARTITION BY company_key)::bigint AS total_postings_seen
    FROM (
        SELECT lower(btrim(company)) AS company_key, company, count(*) AS n
        FROM jobs WHERE company <> ''
        GROUP BY lower(btrim(company)), company
    ) spellings
    ORDER BY company_key, n DESC, company
), paid AS (
    SELECT lower(btrim(j.company)) AS company_key,
           count(*) FILTER (
               WHERE pay.comp_min IS NOT NULL OR pay.comp_max IS NOT NULL
           ) AS comp_found,
           count(*) FILTER (
               WHERE pay.comp_min IS NULL AND pay.comp_max IS NULL
           ) AS comp_ran_found_nothing,
           count(*) AS comp_read
    FROM job_comp pay JOIN jobs j ON j.url = pay.url
    WHERE j.company <> ''
    GROUP BY lower(btrim(j.company))
), base AS (
    SELECT seen.company_key, seen.company_name, seen.total_postings_seen,
           COALESCE(paid.comp_found, 0) AS comp_found,
           COALESCE(paid.comp_ran_found_nothing, 0) AS comp_ran_found_nothing,
           seen.total_postings_seen - COALESCE(paid.comp_read, 0) AS comp_not_attempted
    FROM seen LEFT JOIN paid ON paid.company_key = seen.company_key
), apps AS (
    SELECT lower(btrim(a.company_name)) AS company_key,
           count(*) AS applications_n,
           max(a.applied_at)::date AS last_applied_at
    FROM applications a
    WHERE a.company_name IS NOT NULL AND btrim(a.company_name) <> ''
      AND a.dismissed_at IS NULL
      AND a.user_id = %(user_id)s
    GROUP BY lower(btrim(a.company_name))
)
"""

# Shared by page query (window total_names) and _COUNT_SQL (used when offset is past the end).
_BASE_SQL = (
    _BASE_CTES
    + """
SELECT base.company_key, base.company_name, base.total_postings_seen,
       base.comp_found, base.comp_ran_found_nothing, base.comp_not_attempted,
       COALESCE(apps.applications_n, 0) AS applications_n,
       apps.last_applied_at,
       count(*) OVER () AS total_names
FROM base LEFT JOIN apps ON apps.company_key = base.company_key
WHERE {cuts}
ORDER BY {order}, base.company_key
LIMIT %(limit)s OFFSET %(offset)s
"""
)

# Filter predicates shared between the page query and _COUNT_SQL.
_CUTS = """(%(q)s::text IS NULL OR base.company_key LIKE %(q)s::text)
  AND (NOT %(applied)s OR apps.applications_n IS NOT NULL)
  AND (NOT %(has_comp)s OR base.comp_found > 0)
  AND (NOT %(repost)s OR EXISTS (
      SELECT 1 FROM jobs r
      WHERE r.company <> '' AND r.title <> '' AND r.date_posted IS NOT NULL
        AND lower(btrim(r.company)) = base.company_key
      GROUP BY lower(btrim(r.title)), r.source, r.locations
      HAVING count(*) >= %(min_urls)s
         AND max(r.date_posted) - min(r.date_posted) > make_interval(days => %(min_span)s)))"""


# Decisive reply kinds. Counting acknowledgements (a mail server confirming the
# form) would report a 100% response rate for every company running an ATS.
_OUTCOME_KINDS = (
    "rejection",
    "interview_invite",
    "interview_scheduled",
    "assessment_invite",
    "offer",
    "position_closed",
)

# Excludes applications whose matched mail includes an intermediary domain, and
# restricts timing percentiles to tracker-sourced rows with first_outcome_at >= applied_at.
# The platform exclusion exists because a course provider replying to everyone put
# a perfect response rate on a university. The >= guard drops three rows whose
# outcome predates the hand-entered applied date.
_RESPONSE_SQL = f"""
WITH scoped AS (
    SELECT a.id, lower(btrim(a.company_name)) AS company_key,
           a.applied_at, a.source_provenance
    FROM applications a
    WHERE a.company_name IS NOT NULL AND btrim(a.company_name) <> ''
      AND a.dismissed_at IS NULL
      AND a.user_id = %(user_id)s
      AND lower(btrim(a.company_name)) = ANY(%(keys)s)
      AND NOT EXISTS (
          SELECT 1 FROM ({current_match("application_id")}) am2
          JOIN email_messages m2 ON m2.id = am2.message_id
          WHERE am2.application_id = a.id
            AND lower(COALESCE(NULLIF(split_part(m2.from_email, '@', 2), ''), ''))
                = ANY(%(intermediaries)s)
      )
),
latest_event AS (
    {current_event("kind")}
),
per_app AS (
    SELECT s.id, s.company_key, s.applied_at, s.source_provenance,
           count(e.kind) AS replies,
           count(*) FILTER (WHERE e.kind = ANY(%(outcomes)s)) AS outcomes,
           min(m.sent_at) FILTER (WHERE e.kind = ANY(%(outcomes)s)) AS first_outcome_at
    FROM scoped s
    LEFT JOIN ({current_match("application_id")}) am ON am.application_id = s.id
    LEFT JOIN email_messages m ON m.id = am.message_id
    LEFT JOIN latest_event e ON e.message_id = m.id
    GROUP BY s.id, s.company_key, s.applied_at, s.source_provenance
)
SELECT company_key,
       count(*) AS applications,
       count(*) FILTER (WHERE replies > 0) AS replied,
       count(*) FILTER (WHERE outcomes > 0) AS with_outcome,
       count(*) FILTER (
           WHERE source_provenance = 'tracker' AND applied_at IS NOT NULL
             AND first_outcome_at IS NOT NULL AND first_outcome_at >= applied_at
       ) AS timed_n,
       percentile_cont(0.5) WITHIN GROUP (
           ORDER BY CASE
               WHEN source_provenance = 'tracker' AND applied_at IS NOT NULL
                    AND first_outcome_at IS NOT NULL AND first_outcome_at >= applied_at
               THEN EXTRACT(epoch FROM (first_outcome_at - applied_at)) / 86400
           END
       ) AS median_days,
       percentile_cont(0.9) WITHIN GROUP (
           ORDER BY CASE
               WHEN source_provenance = 'tracker' AND applied_at IS NOT NULL
                    AND first_outcome_at IS NOT NULL AND first_outcome_at >= applied_at
               THEN EXTRACT(epoch FROM (first_outcome_at - applied_at)) / 86400
           END
       ) AS p90_days
FROM per_app GROUP BY company_key
"""

_COUNT_SQL = (
    _BASE_CTES
    + "SELECT count(*) AS c\nFROM base LEFT JOIN apps ON apps.company_key = base.company_key\n"
    + "WHERE {cuts}\n"
)

# Keeps NULL currency as its own bucket rather than assuming USD: currency is
# present on 452 of 11,442 extracted rows, and an amount with no captured
# currency is not a dollar figure.
_CURRENCY_SQL = """
SELECT lower(btrim(j.company)) AS company_key, pay.comp_currency AS currency,
       count(*) AS n, min(pay.comp_min) AS min, max(pay.comp_max) AS max
FROM jobs j JOIN job_comp pay ON pay.url = j.url
WHERE j.company <> ''
  AND (pay.comp_min IS NOT NULL OR pay.comp_max IS NOT NULL)
  AND lower(btrim(j.company)) = ANY(%(keys)s)
GROUP BY lower(btrim(j.company)), pay.comp_currency
"""

# Keyed and filtered on `applications` to stay consistent with `apps` counts;
# mail-only applications without a `user_jobs` row contribute to no status.
# The count and this breakdown render together, so they share table and key;
# they disagreed when this read `user_jobs` keyed on jobs.company.
_STATUS_SQL = """
SELECT lower(btrim(a.company_name)) AS company_key, uj.status, count(*) AS n
FROM applications a
JOIN user_jobs uj ON uj.job_id = a.job_id AND uj.user_id = a.user_id
WHERE a.company_name IS NOT NULL AND btrim(a.company_name) <> ''
  AND a.dismissed_at IS NULL AND NOT uj.hidden
  AND a.user_id = %(user_id)s
  AND uj.status IS NOT NULL AND uj.status <> ''
  AND lower(btrim(a.company_name)) = ANY(%(keys)s)
GROUP BY lower(btrim(a.company_name)), uj.status
"""

# The same instrument as the board row and the per-job signal: the latest closed
# verdict per posting. Counting any closed row (failed ones too) would let this
# page and the board disagree with nobody able to see which was wrong.
_LATEST_CLOSED_PER_JOB = verdict_reads.latest_per(
    "j.id",
    "lower(btrim(j.company)) AS company_key, v.status, v.created_at",
    "v.check_type = 'closed' AND lower(btrim(j.company)) = ANY(%(keys)s)",
    join="JOIN jobs j ON j.url = v.url",
)

_OPEN_SQL = f"""
WITH latest AS (
    {_LATEST_CLOSED_PER_JOB}
)
SELECT company_key,
       count(*) AS n_checked,
       count(*) FILTER (WHERE status = 'passed') AS n_open,
       max(created_at) AS last_checked_at
FROM latest GROUP BY company_key
"""

# Largest same-source, same-location recurring `(company, title)` group per company.
# Location is in the key: without it one chain listing a role across 120 locations
# read as a 1,056-url repost, which is bulk hiring, not re-listing.
# mode() runs only on the winning group so the outer GROUP BY can parallelize
# (430 ms vs 850 ms on a synthetic top page, 2026-10-04).
_REPOST_SQL = """
WITH g AS (
    SELECT lower(btrim(company)) AS company_key, lower(btrim(title)) AS title_key,
           source, locations,
           count(*) AS url_count,
           min(date_posted) AS first_posted_at,
           max(date_posted) AS last_posted_at
    FROM jobs
    WHERE company <> '' AND title <> '' AND date_posted IS NOT NULL
      AND lower(btrim(company)) = ANY(%(keys)s)
    GROUP BY lower(btrim(company)), lower(btrim(title)), source, locations
    HAVING count(*) >= %(min_urls)s
       AND max(date_posted) - min(date_posted) > make_interval(days => %(min_span)s)
), best AS (
    SELECT DISTINCT ON (company_key) *
    FROM g ORDER BY company_key, url_count DESC, last_posted_at DESC
)
SELECT b.company_key,
       (SELECT mode() WITHIN GROUP (ORDER BY j.title) FROM jobs j
        WHERE j.company <> '' AND j.title <> '' AND j.date_posted IS NOT NULL
          AND lower(btrim(j.company)) = b.company_key
          AND lower(btrim(j.title)) = b.title_key
          AND j.source = b.source AND j.locations = b.locations) AS title,
       b.url_count, b.first_posted_at, b.last_posted_at,
       (extract(epoch FROM b.last_posted_at - b.first_posted_at) / 86400)::int AS span_days
FROM best b
"""

_CAVEATS = [
    "Rows are keyed on the company NAME as extracted from a posting, not on a "
    "resolved company. Spelling variants are separate rows, so a distinct-name "
    "count is an upper bound on the number of real companies.",
    "Compensation counts describe what our extractor found, not what an "
    "employer published: a posting that stated pay we failed to read is "
    "indistinguishable from one that stated none.",
    "Most extracted amounts carry no currency, so they are grouped under a "
    "null currency rather than assumed to be USD.",
    "There is no closure or time-to-close figure: too few postings per name "
    "have been observed closing for a median to mean anything.",
]
