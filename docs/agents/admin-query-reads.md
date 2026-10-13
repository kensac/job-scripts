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
one background recompute. `api.app` warms each page's default view at
startup, so a roll does not make the next visitor wait. Key the cache on
everything the answer depends on, config included (the review report keys
on `title_screens`). conftest clears every cache before each test.

Lifetime sums read `model_calls.answers_with_usage`, never `ledger_rows`:
the view's per-row lookups took 116 s at p90 for /admin/stats. A newest-first
page of urls reads each table's `created_at` index directly, because the
view's fetch arm is not flattened and sorts every fetch (4.3 s) instead of
walking its index.
