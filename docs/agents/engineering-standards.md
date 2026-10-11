# Engineering standards

## Correctness

Production-grade only. No patches, no hotfixes. If a design turns out wrong,
rearchitect it rather than working around it.

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

**`core.catalog` is the only writer of `jobs`.** A new write is a named
function there, so it keeps the lock order and a later change to what a
column means has one place to land. `jobs.active` is frozen and written by
nothing; availability is `jobs.available`, stored by `catalog`. `tests/test_catalog_one_writer.py` fails on an
`INSERT`, `UPDATE`, `DELETE` or `MERGE` of `jobs` anywhere else under `src`;
its allow-list holds only the pay and near-copy columns the derived facts move
is taking off `jobs`, and fails once a listed file stops writing.

When the same logic exists in several places and one has drifted, delete the
duplication. Do not fix the copy.

## Always retain all data

**Stored data is never deleted, truncated or capped because of age, count or
size.** No row ages out, no table keeps its last N rows, no text is cut to N
characters before it is saved, no payload is dropped after a while. The scale
here does not call for retention, and data that is gone cannot be measured.

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
in code.** Retry windows, caps, cycle sizes: declare the default, typed
validation, help text, choices, `kind` and `section` in `api/config.py`
(`CONFIG_KEYS`), and read it at the point of use with `db.get_config`. Database
seeding and GET /admin/config derive from that registry; the admin config page
renders every key from its metadata, so a new key needs no frontend entry; four
keys in one evening each needed one before it did. **The whole entry is served,
because a page that infers presentation from prose infers it wrong:** `kind`
picks the control (`text` is a paragraph, `groups` a chip list, `hosts` a
rate table, `columns` a JSON block) where the page used to regex the help
sentence for the word "rules"; `section` files the key under a heading where
the page used to show twenty keys in one flat list; `default` is what "changed"
and "Reset" mean. A new key names its section, or it lands in a General bucket
that says nothing. A constant in code needs a deploy to
change; a row changes on the next cycle. The seed is the default, so the
comment explaining the number goes beside the seed, not beside a literal
somewhere else. Values that are facts about a system (a provider's page size,
a word-boundary regex) stay in code; values that are a person's judgment
about how this deployment should behave do not. A value that differs per
host and that an administrator turns (how many tasks a worker runs at once)
is a map keyed by worker name, as `worker_task_slots` is, so one worker can be
switched without a deploy. A per-host value nobody turns from the admin page
(`JOBTRACKER_MAX_CONCURRENCY`, `JOBTRACKER_SCRAPE_CONCURRENCY`, the worker
poll) stays in that host's environment. `core` cannot read `app_config`, because core does
not import api, so `core.batch.BATCH_WAVE_CONCURRENCY` is still an environment
default; moving it means the caller passes the value down.

## Measuring

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
CTE boundaries. The verify sweep's candidate query (`tasks/verify.candidates_sql`
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

**Measure a large statement with JIT on and off.** Above `jit_above_cost`
PostgreSQL compiles the whole plan. For the verify candidate query that cost
more than the query itself: 78 s with JIT, 24 s without, most of the
difference inside one hash aggregate of 16,000 rows. Such a statement runs
in a transaction with `SET LOCAL jit = off`.

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

Preserve measured why-comments when refactoring. Move the evidence to the code
that now enforces the rule, qualify historical counts, and correct stale claims
without deleting the reason the guard exists.

Do not write docstrings or comments unless they carry something the code
cannot. When they do, they are load-bearing: record why a decision was made
and what breaks if it is reversed, not what the line does.

Comments are frequently wrong. Verify a claim in a comment before relying on
it, and correct it when you find it stale.

## Migrations are generated from the models

The ORM models in `src/api/orm/` are the schema. A schema change is a model change followed by `make migration m="..."`, which autogenerates the migration against the throwaway database; the generated file is then read and, where the change moves data (a copy, a backfill, a seed of both halves of a rename), the data step is added to it by hand. Nobody writes a schema migration from scratch: the two copies drifted every time someone did, and CI's autogenerate drift check exists to catch exactly that. Non-additive changes (drops, renames) still go through the expand-then-contract shape and the before-merge conversation with homelab; generation does not change what is safe to roll, only who writes it.

## Dependencies

Dependencies are declared in `pyproject.toml` and resolved into `uv.lock`; both
are committed and every install is `uv sync --frozen` (`make sync`), locally,
in CI and in the image. To add or move a pin, edit `pyproject.toml` and run
`uv lock`, then commit both files in the same change. A lockfile that does not
match the declaration fails the install rather than resolving something new,
which is the point: what ran in CI is what the image carries.

## Types

`pyright` checks every package under `src` that runs: `src/api`, `src/core`
and `src/tasks`, listed in `pyrightconfig.json`. A new top-level package goes
into `include` in the change that creates it. Code outside the list is not
type-checked, however green the check is. `src/tasks` was outside it until
2026-10-04 and had seven errors that nobody saw. `pythonVersion` is the
interpreter the image runs (`deploy/Dockerfile`), because typeshed's stubs
differ between versions.

Fix a type error with the true type or real `None` handling. A
`# type: ignore` is acceptable only when the checker is wrong, and it says why.
