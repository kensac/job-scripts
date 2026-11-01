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
the review-gate report) at startup, so a roll does not make the next visitor
wait. Key the cache on
everything the answer depends on, config included (the review report keys
on `title_screens`). conftest clears every cache before each test.

The provider's bill (/admin/spend/provider) is not a scan but an outside API
read of 1.2 to 1.5 s, the slowest panel on the spend page, so it reads through
the same cache keyed on window and UTC day. A read the provider did not answer
in full is returned and not cached: raise from the compute so the next visit
asks again rather than serving the failure for the cache window.

Lifetime sums never read `ledger_rows`: the view's per-row lookups took 116 s
at p90 for /admin/stats. They read the hourly sums (below). A newest-first
page of urls reads each table's `created_at` index directly, because the
view's fetch arm is not flattened and sorts every fetch (4.3 s) instead of
walking its index.

The filter vocabularies skip-scan `ai_queries` and `page_fetches` separately
for the same reason (30 s through the view, 1.1 s per table). /admin/spend
reads its window's answers once into the temporary table `spend_window`
(`api.spend.verdicts.window_table`) and computes every cut from it, rather
than rebuilding the answer-to-call join per cut. The review-gate report runs
the title screen once per distinct (recipe, title, source), materialized
before the screen so the planner cannot push the regex back into the join.

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
