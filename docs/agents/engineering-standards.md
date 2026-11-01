# Engineering standards

## Correctness

Keep OAuth consent state and authorization URL construction in
`api.oauth.authorization`, provider settings and typed errors in
`api.oauth.provider`, and grant storage, refresh, and revocation in the
`api.oauth` package initializer. The grant refresh must hold the token row lock through the
provider request and persist a rejected grant before raising. Routes and
workers continue to import the public OAuth API from `api.oauth`.
When moving or renaming an environment reader, keep
`tests/test_env_example.py` able to discover its reads so `.env.example`
stays checked in both directions.

Every module logs through `logging.getLogger(__name__)`
(`tests/test_logger_names.py`). Telemetry attaches to the root logger, so a
module needs no registration. Inside an `except`, use
`logger.exception`, which keeps the traceback.

Preserve concurrency and locking semantics across refactors, and check them
explicitly rather than assuming a refactor kept them.

**A write that locks several rows another writer can also lock takes them in
one order.** Boards share urls, so two ingests write overlapping rows of
`jobs` and `listings`. Each such write locks by url in code-point order
(`core.catalog._LOCK_ORDER`): an executemany sorts its rows in Python, and an
UPDATE or DELETE selected by a predicate locks through a subquery ordered
`COLLATE "C"` with `FOR UPDATE`, because a bare one locks in scan order. One
transaction holds one ordered pass; a second pass after the first is two
orders, so it gets its own transaction. 36 ingests failed on deadlocks in the
60 days to 2026-10-04, 32 on the listings upsert, which ran in board order.
`tests/test_catalog_lock_order.py` reproduces each interleaving with a held
row and fails on the deadlock.

**`core.catalog` is the public API for every write to `jobs`.** Its named
owners are `core.catalog.jobs` for ingest, corrections and uploads, and
`core.catalog.observations` for stored availability.
`core.catalog.listings` owns retained evidence in `listings`. Keep multi-row locks in URL code-point order
across these modules and preserve each operation's transaction boundary.
`jobs.active` is frozen and written by nothing; availability is
`jobs.available`, stored by `core.catalog.observations`.
`tests/test_catalog_one_writer.py` fails on an `INSERT`, `UPDATE`, `DELETE` or
`MERGE` of `jobs` anywhere else under `src`; its allow-list holds only
dev-only seed writes and fails once a listed file stops writing. The two
catalog owner modules must each still contain a job write.

## Naming and deprecations

Name public conversion functions for the domain value they return. When a
function maps request spelling to a stored vocabulary, name the stored domain
and update all callers before removing the old name.

Call a path deprecated only when it is still supported and has a named
replacement. State the condition for removing it, including the stored-data
check when old rows may still use it. An active second format is not legacy
merely because it was introduced first. A public re-export is a supported
entry point until its callers move.

## Always retain all data

Store large uploaded files as independently verified parts. Bound the bytes
handled by one request, read, or worker step without limiting the number of
parts in a file. Do not assemble the whole file in an API process. Keep a
durable receipt before marking an upload complete.

**Stored data is never deleted, truncated or capped because of age, count or
size.** No row ages out, no table keeps its last N rows, no text is cut to N
characters before it is saved, no payload is dropped after a while. The scale
here does not call for retention, and data that is gone cannot be measured.
Do not limit the number of resumes a person can keep. Bound each upload request
and worker read, while storing every part of the file.
Store complete extracted resume text as immutable parts. Keep the prior resume
revision before replacement or deletion. Read only the existing model context
window for drafting; the full text stays available to the person in parts.
Read resume text and PDFs through their canonical version and upload pointers.
After exact backfill verification, remove old-column readers before dropping
old columns. Keep archived revisions linked to their immutable parts.

An extracted skill keeps its raw text and canonical spelling regardless of
length. Reject known non-skill categories by meaning, not by a character count.

What the rule does not cover:

- A bound on a request or a read: a request field's `max_length`, a page's
  `limit`, the slice of stored text sent as model input. These refuse or
  shorten what crosses a boundary; the stored value stays whole.
- A delete a person asks for (their filter, their resume, their grant), and
  a delete an administrator makes.
- Rebuilding a derived projection (`board_visible`, `user_job_working_set`,
  `managed_board_jobs`, a `job_skills` re-derive), which removes no fact.
- Removing an exact duplicate of a stored row.

**Because nothing is deleted, existence is not currency.** A reader that
means "true now" says so with a predicate on the row's own timestamps, never
by assuming an old row is gone. `catalog.LISTED_NOW` is the worked example
for `listings`.

## Constants

**Magic numbers are a shortcut. Derive the constant or name where the value
came from, in a comment beside it.**

A derived threshold beats a tuned one because it adapts and needs no
maintenance. Prefer a rule that falls out of how the system actually works:

- A count that is bimodal gives you the threshold directly.
- A cap derived from a downstream consumer's limit cannot drift out of sync
  with it.
- A bound taken from a value the system already declares, such as a provider's stated
  completion window or an existing timeout, inherits that value's meaning
  instead of inventing one.

**A threshold must not be derived from a statistic the failure it detects can
move.** A bound set from an operation's own historical maximum looks like the
ideal derived threshold: it adapts, needs no tuning, and tracks the operation
as it changes. It is a trap when the failure mode ends in a completed run,
because each failure that survives enters the history and raises the bound.
The detector then goes blind exactly as often as the problem occurs, while
continuing to look like it works, which is worse than having none.

Before deriving a bound from history, ask whether the thing being detected can
end up inside that history. If it can, the statistic is contaminated and you
need a signal the failure cannot contribute to.

**A verification claim can have a shelf life.** Checking a threshold against
live data proves it was right at that moment, not that it stays right. If the
data behind a bound can move, say when it was measured, and prefer a bound that
cannot drift out from under the claim.

State the scale a constant depends on. A value derived from one user's data
should say so.

**A value an administrator might want to change lives in `app_config`, not
in code.** Retry windows, caps, cycle sizes: one registry entry per key
([config-registry.md](config-registry.md)), read at the point of use with
`db.get_config`. The admin config page renders every key from its metadata,
so a new key needs no frontend entry: `kind` picks the control (`text` is a
paragraph, `groups` a chip list, `hosts` a rate table, `columns` a JSON
block), `section` files it under a heading, and `default` is what "changed"
and "Reset" mean. A constant in code needs a deploy to
change; a row changes on the next cycle. The seed is the default, so the
comment explaining the number goes beside the seed, not beside a literal
somewhere else. Values that are facts about a system (a provider's page size,
a word-boundary regex) stay in code; values that are a person's judgment
about how this deployment should behave do not. A value that differs per
host and that an administrator turns (how many tasks a worker runs at once)
is a map keyed by worker name, as `worker_task_slots` is, so one worker can be
switched without a deploy. Task slots are also a fact about the host's
memory, so the host's own value is `JOBTRACKER_TASK_SLOTS` and the map is the
override, which can lower it and not raise it. A per-host value nobody turns from the admin page
(`JOBTRACKER_MAX_CONCURRENCY`, `JOBTRACKER_SCRAPE_CONCURRENCY`, the worker
poll) stays in that host's environment. `core` cannot read `app_config`, because core does
not import api, so `core.batch.BATCH_WAVE_CONCURRENCY` is still an environment
default; moving it means the caller passes the value down.

## Measuring

Filter insight reason groups may overlap. Keep each group's decision count,
distinct job URLs, and phrasing count in `ReasonBreakdownAccumulator`. Keep
prompt-level missing-evidence decisions in `PromptEvidenceAccumulator`, counted
once per rejected verdict before assigning its reason to groups. Never derive
prompt totals by summing group totals.

Price an optimisation before defending it. Count the population before
describing a mechanism as costly.

A measurement taken in one window is a snapshot, not a property. If a number
could differ an hour from now, say when it was taken. Prefer answering from
mechanism plus full-corpus counts over watching a trend.

Before concluding from an aggregate, check that the filter producing it does
not exclude the population in question. A check scoped by the thing it is
checking can never fail.

Before changing a hot path, measure requests, queries, transactions and runtime
before and after at a stated workload scale. Report what was measured; a cleaner
abstraction or passing test does not establish a performance improvement.

**A page that cuts one large table several ways reads it once.** Group by the
union of the keys the cuts need, keep conditional measures as `FILTER`s inside
each group, and fold the groups in Python with `api/grouped.py`, which keeps
SQL's rule that a SUM of only NULLs is NULL. Numeric sums arrive as exact
Decimals, so the fold is the same number. /admin/spend and /admin/stats did
this on 2026-10-04: five per-cut scans each, measured on production at about
97 s and 9.6 s, against 3.2-4.0 s and 3.98 s for one pass. Keep the old
per-cut SQL as the reference in the equality test (`tests/test_spend_stats_single_pass.py`).
`GROUPING SETS` over the raw table measured slower for stats on 2M synthetic
rows locally (3.2 s against 2.6 s), because it sorts per set and spilled to
disk.

**A query that puts most of the catalog through costly checks runs them as
stages, cheapest first, each a `MATERIALIZED` CTE.** Written as one predicate,
an `EXISTS` under an `OR` becomes a subplan run per row, and the planner
orders the remaining checks by its own row estimates, which are wrong across
CTE boundaries. The verify sweep's candidate query (`tasks/verification/selection.candidates_sql`
and `api/verification_candidates.reachable`) ran past 14 minutes on
production on 2026-10-10 and held locks a deploy's `DROP VIEW` queued behind.
Staged, it read 21 s warm and 35 to 43 s with a cold cache. Three rules came out of it:
- a check that depends on a few of a row's values runs once per distinct
  value, not per row: the place checks ran 67,000 times rather than 272,000;
- a check that must run per surviving row goes in a scalar subquery, which
  the planner never turns into a join. As a join, it drove the check from all
  840,000 fetched urls;
- read wide columns (page text) only for the rows the `LIMIT` keeps.
Keep the old SQL as the reference in the equality test, built from the same
fragments, and give the fixture a row for every branch
(`tests/test_verification_candidates.py`).

**Join a table only when the statement reads it.** The planner removes an
unused LEFT JOIN to a unique key, but the join column stays needed, so an
index-only scan must still return it. The admin catalog count joined
`job_comp` on `url` for pay filters it rarely had, and counted through the
256 MB url index instead of the 23 MB company index: 0.72-0.83 s against
0.23 s warm on 1.48M rows (production, 2026-11-01). Add the join when a
filter names the table (`column_filters.reads_pay`).

**Measure a large statement with JIT on and off.** Above `jit_above_cost`
PostgreSQL compiles the whole plan. For the verify candidate query that cost
more than the query itself: 78 s with JIT, 24 s without, most of the
difference inside one hash aggregate of 16,000 rows. Such a statement runs
in a transaction with `SET LOCAL jit = off`.

**The most common value per group over a large table is counted, not
`mode()`.** `mode()` is an ordered-set aggregate: it needs every row of the
group in order, so it sorts or walks an index through the heap. Count each
(group, value) in a hash aggregate, then keep the top value per group with
`DISTINCT ON (group) ... ORDER BY group, n DESC, value`, which breaks ties as
`mode()` does. The company page's display names took 2.7-3.2 s this way
against 3.6-4.2 s, identical on all 10,751 rows (production, 2026-11-01).

**A distinct count over text sorts in `COLLATE "C"`.** `count(DISTINCT url)`
sorts its input under the database collation, en_US, which compares through
the locale. Two strings are equal under a deterministic collation exactly when
their bytes are, so `count(DISTINCT url COLLATE "C")` is the same number. On
the filter insight totals (740,000 verdicts) the sort took 2.5-3 s against
18 s (production, 2026-11-01).

**A window read of a large table that is most of the page's cost is a
partial covering index.** The filter insight page read 740,000 custom
verdicts spread over 1.5 GB of `ai_queries` heap: 18-27 s cold before any
aggregate. `idx_ai_queries_custom_window` and `idx_ai_queries_custom_reasons`
carry every column the two statements read, keyed on `created_at` so the cost
follows the window rather than the history. A column one statement needs only
for a count (`reason`, for `rejected_with_reason`) is counted from the rows a
second, narrower index already returns, instead of widening the large index.

Prompt changes require before/after output-token and decision-quality evaluation
on the same inputs. Schema and parsing tests establish compatibility, not outcome
quality or savings. Keep evaluation costs explicitly bounded. `api.run_experiment`
runs the same seed on main and on the branch and scores them together; see
[observability.md](observability.md).

## A fix must reach its own population

**Measure after shipping, not only before.** A fix that corrects a rule but
cannot reach the data the rule already produced reads as done and is not.

Three shapes of this, each invisible from the code alone and each needing a
production count to see:

- **The rule is fixed for new data only.** A derivation corrected at write time
  leaves every existing row computed the old way, and nothing recomputes them.
  Prefer a correction that runs every cycle, only ever moves in the safe
  direction, and becomes a no-op once the data is right. Then the next change
  to that rule reaches everything automatically.
- **Two predicates in one module mean different things.** One reads "nothing
  has looked", its sibling reads "looked and found nothing". Both look correct
  in isolation, and the docstrings can describe behaviour neither implements.
- **A field's name and its value disagree.** A counter named for successes may
  be summing every outcome. Read the assignment, not the name.

After a change lands, count the population it was supposed to affect. If the
number did not move, the fix did not reach it, regardless of what the tests
say, because the tests exercise the new path and the old data is not on it.

## Trusting a signal

**The summary line is not the measurement.** A process's report of itself is
not evidence about the underlying state. Compare the state directly.

This applies to: exit codes, "no changes" messages, completion counts, task
status, a truncated command's first lines, and any field whose name describes
an outcome. Read the field's definition before treating its name as its
meaning: a counter named for successes may be counting attempts.

A clean exit reads as success everywhere. Verify that the thing happened, not
that the process finished.

## Comments

Write comments and docstrings as short statements of the current rule and its
reason. Keep a measurement when it explains a non-obvious bound or choice,
with its date and scope. Remove historical narration that no longer helps a
reader decide what the code must do.

Preserve measured why-comments when refactoring. Move the evidence to the code
that now enforces the rule, qualify historical counts, and correct stale claims
without deleting the reason the guard exists.

Do not write docstrings or comments unless they carry something the code
cannot. When they do, they are load-bearing: record why a decision was made
and what breaks if it is reversed, not what the line does.

Comments are frequently wrong. Verify a claim in a comment before relying on
it, and correct it when you find it stale.

## ORM models

The ORM models in `src/api/orm/` are the schema; migrations are generated from
them ([migrations.md](migrations.md)).

**Keep catalog ORM models grouped by the data they represent.**
`api.orm.catalog_postings` owns the posting, upload and page-fetch facts;
`api.orm.catalog_derivations` owns extracted answers and classified places;
`api.orm.catalog_sources` owns source settings, listing evidence and source
observations. `api.orm.__init__` imports the groups to register every table
on one `Base.metadata` for Alembic. A model move preserves its mapped table, columns,
constraints and indexes exactly. Compare the full metadata before and after,
then run `migrations-check`; moving a class alone needs no migration.

## Dependencies

Dependencies are declared in `pyproject.toml` and resolved into `uv.lock`; both
are committed and every install is `uv sync --frozen` (`make sync`), locally,
in CI and in the image. To add or move a pin, edit `pyproject.toml` and run
`uv lock`, then commit both files in the same change. A lockfile that does not
match the declaration fails the install rather than resolving something new,
which is the point: what ran in CI is what the image carries. Do not pin
`pydantic-core` separately from Pydantic, which requires a matching core.

A heavy SDK is imported with `lazy import` / `lazy from` (PEP 810), so a
process pays for it only once it uses it: anthropic, openai, boto3, botocore,
pypdf, and fastapi outside the API's own modules. Our own modules stay eager.
Measured 2026-10-31, `import api.worker` loaded 4,673 modules in about 200 MB
before and 941 in 79 MB after. gcp-vps has a 400 MB worker cap and never uses
anthropic. `tests/test_lazy_imports.py` fails if a fresh worker loads any of
them. Do not turn on the global mode (`PYTHON_LAZY_IMPORTS=all`): it breaks
third-party code written for eager imports, pytest among them.

## Types

`pyright` checks every package under `src` that runs: `src/api`, `src/core`
and `src/tasks`, listed in `pyrightconfig.json`. A new top-level package goes
into `include` in the change that creates it. Code outside the list is not
type-checked, however green the check is. Editors and `pyright` use the
repository `.venv`. `pythonVersion` is the
interpreter the image runs (`deploy/Dockerfile`), because typeshed's stubs
differ between versions.

`tests` is in the list too, so a test that imports a name that no longer
exists fails `make check` instead of failing collection in every CI lane at
once. Its execution environment turns off six rules that `src` keeps:
`reportArgumentType` and the five `reportOptional*` rules. Measured
2026-11-01, they were 929 of the 972 errors in `tests`. A test reads
`db.query_one(...)["n"]` and passes `enqueue()`'s `int | None` straight on,
and a None there fails the test at once. The other 43 were fixed. Missing
imports and unknown import symbols stay errors in `tests`. Turn a rule back on
by fixing what it reports, not by widening the list.

Fix a type error with the true type or real `None` handling. A
`# type: ignore` is acceptable only when the checker is wrong, and it says why.
