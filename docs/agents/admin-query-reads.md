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
