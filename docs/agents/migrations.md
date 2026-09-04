# Migrations and schema

Migrations are alembic. CI checks that the models and the migrations agree, so
a mismatch fails the build rather than reaching a host.

## Rules

**A column with a server default still needs `nullable=False` if the model says
so.** Omitting it produces a drift failure in CI's autogenerate check.

**Additive migrations only, unless you have told the deployment owner first.**
A non-additive migration and any change to the task-claim, heartbeat or reaper
contract must be announced before it merges, not after.

**Parallel work produces two heads from one parent.** Each branch is green
against its own parent, and `upgrade head` fails only once both are on main.
The application runs `upgrade head` at startup, so two heads means new hosts
do not come up at all — while already-running hosts, whose version row is
populated, look perfectly healthy.

Check for this against **main merged into your branch**, not against your
branch alone. A check that runs only on your branch is structurally blind to
it.

When two heads exist, resolve with a merge revision. Verify first that the two
migrations touch different objects; if they touch the same column, a merge
hides a real conflict.

**Re-parenting is safe only when nothing has applied the migration yet.** Once
a host has recorded a revision, its parent cannot change.

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

## Derived state is not schema

Do not add a column for something derivable from rows you already have. A
stored derivation desyncs; a derived one cannot. Store the person's answer,
derive the system's inference.
