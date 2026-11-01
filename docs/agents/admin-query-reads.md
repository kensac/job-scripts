# Admin query reads

Keep the admin model-call ledger reads in the module that owns their meaning.

- `query_ledger.py` owns individual model-call rows, filters, sorting, detail,
  and the filtered query vocabulary.
- `posting_inspection.py` owns posting rollups, a posting's model-call
  responses, timeline, and path inspection. A posting rollup is a view of the
  model-call ledger, including URLs absent from the jobs catalog.
- `ledger_stats.py` owns the unscoped vocabulary and aggregate ledger
  statistics, including their cache.
- `queries.py` registers those routers in that order. Keep route paths,
  response models, and registration order stable when moving implementation.

A test that inspects an internal helper imports its owning module. Do not
re-export internals through the router registration module. Keep the
`/queries/options` filtered vocabulary distinct from the lifetime `/options`
vocabulary, and preserve recorded costs when aggregating historical calls.

## Slow admin pages are served stale

An admin page whose answer is an aggregate over the whole ledger or catalog
(stats, the jobs total, query options, companies, spend, the review-gate
report) reads through `api.stale_cache`: `Stale` for one value, `StaleByKey`
for one per query. The first read in a process waits; after that a read
older than `admin_stats_cache_seconds` returns the cached answer and starts
one background recompute. `_warm_admin_pages` in `api.app` warms the
default views that take 15 to 70 s (stats, query options, checked urls, spend,
the review-gate report), the jobs list's Passed and Rejected totals, and the
dashboard's content mix that every live tick refetches, at startup, so a roll
does not make the next visitor
wait. Key the cache on
everything the answer depends on, config included (the review report keys
on `title_screens`). conftest clears every cache before each test.

The provider's bill (/admin/spend/provider) is not a scan but an outside API
read of 1.2 to 1.5 s, the slowest panel on the spend page, so it reads through
the same cache keyed on window and UTC day. /admin/spend/balance caches each
provider's raw read the same way, keyed on provider, grant day and UTC day, and
derives from the current settings on every request. A read the provider did not answer
in full is returned and not cached: raise from the compute so the next visit
asks again rather than serving the failure for the cache window.

Lifetime sums never read `ledger_rows`: the view's per-row lookups took 116 s
at p90 for /admin/stats. They read the hourly sums (below). The Passed and
Rejected tabs read the newest-first window of urls and keep the urls whose
history holds the verdict, checked per url. Grouping the whole ledger under a
HAVING took 66 s for the count alone on production (2026-11-01), and the tab
answered 503. Their totals are served stale. Passed is counted as checked less
rejected less the few with no verdict, because a direct count took 3 to 4.5
min.

Both arms of `ledger_rows` are flattened into the query that reads the view,
so a filter or an order on it reaches each table's indexes. An arm with a
WHERE of its own is not flattened: it is planned sealed, and a newest-first
page sorted every fetch (2.0 s on production) instead of walking
`page_fetches_pkey`. So the view filters out verification copies in its outer
query, by arm (58e4e8b6b54d), and a new arm filter goes there too.
`tests/test_admin_queries.py` fails if the fetch arm stops walking its index.
The newest-first page of urls and the filter vocabularies read the two tables
directly; they were written while the arm was sealed (4.3 s and 30 s through
the view then). /admin/spend
reads its window's answers once into the temporary table `spend_window`
(`api.spend.verdicts.window_table`) and computes every cut from it, rather
than rebuilding the answer-to-call join per cut. The review-gate report runs
the title screen once per distinct (recipe, title, source), materialized
before the screen so the planner cannot push the regex back into the join.

The catalog (/jobs/catalog) searches `company`, `title` and `url` with
`ILIKE` joined by OR, and each column has a trigram GIN index. An OR is
index-backed only when every arm is, so a column added to the search needs
its own trigram index, or the whole search falls back to a seq scan of the
catalog (2.4 s warm on 1.49M jobs, 2026-11-01). Its date sort reads
`idx_jobs_date_posted`, which matches `date_posted DESC NULLS LAST, id DESC`
only; a change to that ORDER BY or its tiebreak changes the index with it.

## The answer ledger's hourly sums

Reads that add up a month or a lifetime of answers (/admin/stats, the spend
breakdowns) group `api.ledger_rollup.groups()` instead of the answers: the
answer and fetch ledger summed per UTC hour and per dimension (arm, model,
check type, status, config, prompt hash, batched) in `answer_usage_hourly`.
A read is exact by construction. It takes an hour from the table only when
the hour lies wholly inside the window, is older than
`ledger_rollup_state.computed_until`, and has no row in `ledger_dirty_hours`.
It aggregates everything else (the partial first hour, everything from the
watermark on, each dirty hour) from the source tables in the same statement.
Triggers on `ai_queries`, `model_calls` and `page_fetches` append the hour of
every row a write touches, and the hours of every answer sharing a call whose
first answer may have changed. The `ledger_rollup` task recomputes the dirty
hours and advances the watermark, two days per transaction, hourly. Missing a
refresh makes a read slower, never wrong.

This is the one exception to "Derived state is not schema" (migrations.md).
It is allowed because no read can return a stale hour: a write that changes an
hour also logs it, in the same transaction. A write path that bypasses row
triggers (TRUNCATE, `session_replication_role = replica`, disabled triggers)
must refresh the rollup or clear it. `tests/test_ledger_rollup.py` fails if
the triggers stop covering a late write.

A new cut adds its dimension to `DIMENSIONS`, `aggregate()` and the table, and
then clears the table and `ledger_rollup_state` so the next refresh re-sums
history. Measured on production (2026-10-13): the live part of a read covers
the last hour or two (about 1 s), and a two-day chunk of history takes about
10 s to sum.
