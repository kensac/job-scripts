# Migrations and schema

Migrations are alembic. CI checks that the models and the migrations agree, so
a mismatch fails the build rather than reaching a host.

## Rules

**Generate with `make migration m="what changed"`.** Autogenerate connects to a
database to diff the models against it, and bare `alembic revision
--autogenerate` reads whatever `DATABASE_URL` the shell holds. If that is
production, it is only a read, but the next command in that shell is
`alembic upgrade`, and that one is not. The target points both at the
throwaway copy.


**A column with a server default still needs `nullable=False` if the model says
so.** Omitting it produces a drift failure in CI's autogenerate check.

**Additive migrations only, unless you have told the deployment owner first.**
A non-additive migration and any change to the task-claim, heartbeat or reaper
contract must be announced before it merges, not after.

**A migration that can refuse to apply must not reach the fleet unattended.**
Merges reach the fleet through Renovate: it opens a PR in each host repo for
the new image and automerges, so a migration lands on Renovate's cadence with
nobody watching. The api and every worker run `upgrade head` at startup, so a
preflight that raises (a uniqueness constraint whose data check fails, a
`RAISE EXCEPTION` on ambiguous rows) is seven containers refusing to start
across six hosts, found by whoever notices the board is down. Before merging
such a migration, check the condition against production yourself and say the
result in the PR; if it could plausibly raise, or the migration is not
additive, ask the deployment owner for an explicit roll and restore the
job-scripts opt-out in the host repos' `renovate.json` for that one release.
The two constraint migrations of #476 (2026-09-09) were the first to land
unattended; they passed because the data was clean and #472 already refused
the condition at write time.

**Parallel work produces two heads from one parent.** Each branch is green
against its own parent, and `upgrade head` fails only once both are on main.
The application runs `upgrade head` at startup, so two heads means new hosts
do not come up at all, while already-running hosts, whose version row is
populated, look perfectly healthy.

Check for this against **main merged into your branch**, not against your
branch alone. A check that runs only on your branch is structurally blind to
it. CI's `static` job does: on a pull request it runs `make migrations-check`
on the merge result. That merge result is as old as the run, so a branch
whose run predates another migration landing on main must be re-run before
it merges.

When two heads exist, resolve with a merge revision. Verify first that the two
migrations touch different objects; if they touch the same column, a merge
hides a real conflict.

**Re-parenting is safe only when nothing has applied the migration yet.** Once
a host has recorded a revision, its parent cannot change.

**An index on a large live table is built `CONCURRENTLY`.** A plain build
holds SHARE on the table for the whole build, and on `tasks` every heartbeat
and claim waits behind it. Build inside `op.get_context().autocommit_block()`,
with `postgresql_concurrently=True` and `if_not_exists=True`, after dropping
an INVALID index of the same name that an interrupted build left behind
(`IF NOT EXISTS` would accept it). Set a `lock_timeout`: a concurrent build
waits for every transaction older than itself, and a timeout turns a wait that
will not end into a failed start that retries. `bd1e66f153c3` is the worked
example.

**An index whose definition changes keeps its name through a staged swap.**
Build the new definition concurrently under a staged name, drop the old one
concurrently, then `ALTER INDEX ... RENAME` the staged one over it. RENAME
takes SHARE UPDATE EXCLUSIVE, so readers and writers continue, and the
readers always have one of the two. Skip the swap when the live definition
already matches, so a start that died after the rename does not rebuild it.
Autogenerate does not see a change to `INCLUDE` columns, so the migration is
written by hand. `1afab52e064c` is the worked example.

**An index with zero scans over the life of the statistics is dropped, after
checking every query.** Read `idx_scan` from `pg_stat_user_indexes` and
`stats_reset` from `pg_stat_database`; a zero means something only if the
counters cover the time the code that could use the index has been live.
Then find every query that could use it and EXPLAIN each on realistic volume
without the index (drop it inside a transaction and roll back). Zero can mean
"no plan can use it" (a partial predicate nothing implies) or "the planner
would use it but the query never ran"; in the second case keep it if the plan
without it is a scan of a large table on a path a non-administrator reaches.
An OR is index-backed only when every arm is, so one arm's unused index can
be what keeps the others usable. Drop with `DROP INDEX CONCURRENTLY IF
EXISTS` inside `autocommit_block()` under a `lock_timeout`, and have the
downgrade rebuild it concurrently. `7c0b33a7fd95` is the worked example,
including an index with zero scans it kept.

**A table that is about to take a bulk update gets per-table autovacuum
settings first.** Stock `autovacuum_vacuum_scale_factor` is 0.2: a table
updated row by row accumulates a fifth of itself in dead versions before
vacuum frees any space, and on full pages every new version extends the file
instead of reusing that space. Set the scale factors before the backfill
starts; `ALTER TABLE ... SET` takes SHARE UPDATE EXCLUSIVE and rewrites
nothing, so it is safe on the live table, and unlike fillfactor it applies at
autovacuum's next check.

**No session that takes the schema lock may hold a snapshot while a
migration runs.** `db.init_schema` polls `pg_try_advisory_lock` and commits
after every attempt; the session-level lock outlives the commit. A concurrent
build waits for every older snapshot. A waiter blocked inside
`pg_advisory_lock` holds one, and so does the lock holder idling in the
transaction its lock statement opened. Both hung the build without the
deadlock detector seeing it, reproduced on 2026-10-03: the holder only on a
database migrated from scratch, which is what CI provisions.
`tests/test_schema_lock.py` builds an index concurrently while a peer waits
in `init_schema`, and fails if the waiter holds a snapshot.

**A storage parameter applies to pages written afterwards.** Fillfactor set
on a live table leaves every existing page as packed as it was. Say so in the
migration, and do not rewrite the table to apply it: a rewrite locks it.
`507fe2f38949` is the example.

**A column is dropped only by a migration that proves it empty.** Add a
`CHECK (col IS NULL ...)` constraint `NOT VALID`, then `VALIDATE` it: the scan
runs under SHARE UPDATE EXCLUSIVE, so reads and writes continue. If any row
holds a value, the migration raises before anything is dropped, and until the
drop commits the constraint refuses new values. Every image that names the
column must be gone from the fleet first, because a dropped column fails every
statement that names it: readers stop naming it one release, the drop ships
the next. `7ca95d34ef5f` is the worked example.

**SET NOT NULL on a large table goes through a validated CHECK.** Plain
`SET NOT NULL` scans the table under ACCESS EXCLUSIVE. With a validated
`CHECK (col IS NOT NULL)` in place, PostgreSQL proves the column non-null from
the constraint and skips the scan. Validate in an `autocommit_block()`, then
set NOT NULL and drop the CHECK in a short transaction.

## Long-running work

Do not put a large data operation inside a migration. Migrations run at every
container start behind the schema lock, so a long one stalls every deploy.
Register it as a task instead, and make it idempotent by predicate so a
partial run resumes rather than restarting.

## Every table is a model

No table is created outside alembic. `ai_queries` was the last one, created
by `core/store.py` at import and hidden from the drift check; f3a4b5c6d7e8
adopted it with an `IF NOT EXISTS` migration mirroring what production held,
and the model has described it since. A table without a model has no
autogenerate, needs every column added in two places, and makes a clean drift
check a lie about the largest table in the database. When a table exists that
the models do not know, adopt it the same way rather than excluding it.

Per-table storage settings (autovacuum thresholds, fillfactor) live in the
migration that needs them, with the measurement that chose the value, because
autogenerate does not see them.

## A table that holds several kinds of record has a view per kind

Readers name the kind through the view (`verdicts` over `ai_queries`), not
the predicate that picks it out. The view is created in a migration with
`CREATE OR REPLACE VIEW`, which takes only ACCESS SHARE on the table; a plain
view is inlined by the planner, so partial indexes whose predicate the view
implies still serve its readers (checked with EXPLAIN on production for
`idx_ai_queries_latest_verdict` and `idx_ai_queries_latest_custom`). Define
the view by what it excludes when the included set is a registry, so a new
registration is included without a migration. A partial index makes a
reader of a view index-only only when its predicate states every condition
of the view that names a column outside the index: the planner drops a
condition the predicate implies, and it proves little (`length(content) > 200`
does not imply `content <> ''`). `1276e94618f8` is the example, and
`tests/test_page_texts_view.py` fails when its plan stops being index-only. Autogenerate does not see
views, and the test corpus and sync tools read base tables only.

## Derived state is not schema

Do not add a column for something derivable from rows you already have. A
stored derivation desyncs; a derived one cannot. Store the person's answer,
derive the system's inference.
