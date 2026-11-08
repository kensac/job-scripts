# Reading production from outside it

**Anything that reads the production database shares production's blast
radius**, including development tooling. A tool that only reads is not
therefore safe.

## Production has its own variable name

`.env` holds production's DSN as `PRODUCTION_DATABASE_URL`, never as
`DATABASE_URL`. `DATABASE_URL` is what the application connects to, and
nothing loads `.env` into it: the fleet sets it in its compose files, and
`make dev-api`, `make dev-worker` and `make migrate` set it to the checkout's
disposable database and refuse any other name. Only the commands that exist to
read production (`make profile`, `make profile-check`)
read `PRODUCTION_DATABASE_URL`. Pointing anything else at production is a
command that says so:

```
DATABASE_URL='postgresql:///?service=jobtracker_prod' python tools/publish_recipes.py
```

Keep it that way: no module loads `.env` on import. `.env.example` lists
every variable the code reads.

Importing `api` or `core` opens the pool, so a script that only needs the code
sets a DSN that cannot connect, as `api/export_schema.py` does:
`DATABASE_URL=postgresql://unused:unused@127.0.0.1:1/no_database_here`.

## Reaching it

The database is the `jobtracker-db` container on hetzner and listens on the
tailnet only, so GitHub's runners cannot reach it. A connection that times out
from the laptop means the tailnet is down: say so rather than probing
addresses. Host addresses and ssh users are in homelab-config's
`docs/ansible-inventory/hosts.yml`, and the fleet's layout is its
`docs/jobtracker/README.md`. This repository is public, so neither is copied
here.

Pick the route by the size of the job:

- **A short lookup from the laptop:** `psql service=jobtracker_prod` (defined in
  `~/.pg_service.conf`) with
  `PGOPTIONS='-c default_transaction_read_only=on'`. From Python the DSN is
  `postgresql:///?service=jobtracker_prod`; a bare `service=` string is not a
  URL and alembic's env refuses it. Worktree sessions refuse `. ./.env`, so use
  the service, not the variable.
- **Anything heavier runs on hetzner**, next to the data: pipe SQL into
  `docker exec -i jobtracker-db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'`,
  starting with `SET default_transaction_read_only = on;`, under
  `systemd-run --collect` when it is long. From the laptop, a scan over
  `page_fetches` ran past ten minutes, and a 14-minute read held up a
  migration's `DROP VIEW` during a roll. Cancel the backend of any laptop query
  you abandon.
- **Repository code against production** runs inside `jobtracker-api`:
  `docker exec -i jobtracker-api python - < script.py`. That code is the
  deployed release, so it matches the schema, and its environment already
  names the database. A laptop checkout can be ahead of or behind the fleet.

## Where an answer already lives

Read these before writing SQL from table names:

- **AI spend:** `model_calls` (`purpose`, `cost_usd`, `created_at`), and per
  check type `answer_usage_hourly`. Never `api_usage`
  ([observability.md](observability.md)).
- **A person's sources:** the view `user_source_set`. `user_sources` alone
  misses every source that reaches them through a bundle
  ([sources-and-boards.md](sources-and-boards.md)).
- **A person's board:** `board_visible` ([visibility.md](visibility.md)).
  `posting_path.for_user` explains one posting; it rescans the verdict ledger
  each call, so a bulk "why is it missing" is set-based SQL instead.
- **Which commit each worker runs:** `worker_status.release` for rows with a
  recent `last_seen` ([deployment.md](deployment.md)).
- **Column names:** `src/api/orm/` or `\d`, never a guess.

## Writing as an admin would

When a write is authorised (rule 5 in AGENTS.md), make it through the code the
admin route runs, inside `jobtracker-api`, so the validation, locks and history
rows happen. A hand `UPDATE` skips them. A setting goes through `put_config`'s
transaction ([config-registry.md](config-registry.md)). An off-cycle task goes
through `task_admission.enqueue` with a `manual-` cycle, as `trigger_ingest`
does, so the scheduled cycle's dedupe is untouched. No task can be pinned to
one worker; which kinds a host runs is its environment
([config-registry.md](config-registry.md)).

## A long read blocks schema changes

A bulk read holds a shared lock for its whole duration. A queued `ALTER TABLE`
waits behind it, and everything after that queues behind the ALTER.

The application runs migrations at startup, so a read that outlives a deploy
prevents the application from starting at all. Containers sit at "waiting for
application startup" while every health check, image digest and container
status reports normal.

## Build so it cannot happen

The property to build for is that such a tool **cannot** hold a lock long
enough to matter, not that someone remembers when to run it:

- **Chunk the read** so no cursor outlives a deploy. One transaction per range
  means a queued DDL waits one chunk instead of one table. This also makes the
  copy resumable, which a large transfer needs regardless.
- **Set `idle_in_transaction_session_timeout` on the reading role** as the
  backstop. A tool that dies at the client end can otherwise leave a cursor
  pinned indefinitely. The danger window is not the run's duration; it is
  unbounded until someone kills the connection.
- Set both as role-level defaults so a future caller cannot forget them.
- **Better than any timeout: do not keep the connection alive.** A process
  that exits when its work finishes cannot leak a lock, because the state is
  gone rather than bounded. A long-lived container holding a pool is what
  turns a finished job into an open transaction.

The exposure window is **not the duration of the run.** A connection can
outlive the job that opened it, so "is a copy running right now" returns no
while a lock is still held. Ask what connections exist, not what jobs are
running.

Two levers look right and are not. `statement_timeout` caps how long the read
runs, but the DDL still queues for that whole period. And any timeout long
enough to copy a large table is long enough to stall a deploy. `lock_timeout`
governs locks a session **waits for**, not ones it **holds**.

## A foreign-data-wrapper session is opaque from the source side

It shows as `FETCH n FROM cN` with no table name, so grepping the source for
what you think it is reading finds nothing and proves nothing.

Identify it by client address and application name. But note that a host can
present **more than one public IP** depending on egress path, and the same
resolver can return different answers from different processes on that host.

One reading does not identify a machine. Check several, and prefer a causal
test (stop the suspected process, see if the session goes) over an address
match.
