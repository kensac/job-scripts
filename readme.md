# Job Scripts

A multi-user job-application tracker. Sources (GitHub job boards, Airtable
views) are ingested hourly into a shared catalog, run through AI filters
(closed / visa-clearance / per-user custom prompts), and served to a web app
where each user tracks their applications.

## Components

| Path | What it is |
|---|---|
| `src/api/` | FastAPI backend (`api.app`), task worker (`api.worker`), SQLAlchemy models + Alembic migrations |
| `src/tasks/` | The handlers the worker runs, and the runtime they run inside |
| `src/core/` | Shared pipeline: fetching, scraping (headless Chromium), AI checks, verdict/content cache, catalog |
| `alembic/` | Schema migrations, applied automatically on API/worker start |
| `tests/` | Integration tests against a real Postgres, including a generated corpus |
| `extension/` | The browser extension that fills application forms |
| `openapi.json` | Generated API schema (`make schema`), canon for frontend types |

## Architecture

- **API** (`uvicorn api.app:app`): multi-tenant REST API. Identity arrives via
  trusted headers from an authenticating proxy (`X-Service-Token`,
  `X-User-Sub`, `X-User-Groups`); Authentik groups drive entitlements and
  weekly AI-token budgets (`group_budgets` table).
- **Workers** (`python -m api.worker`): claim tasks from a Postgres queue
  (`FOR UPDATE SKIP LOCKED`), safe across any number of machines. Handle
  source ingestion, link-upload extraction, and per-user filter runs, with
  heartbeats, a stale-task reaper, attempt caps, and mid-task cancellation.
  A leaderless scheduler (dedupe keys) enqueues one ingest per active source
  per hour.
- **AI filters**: verdicts are cached globally by (url, prompt hash, model),
  so identical filters cost once across all users. Users bring their own key
  (OpenAI / Anthropic / OpenAI-compatible, encrypted at rest, SSRF-guarded)
  or spend a group budget on the shared key.
- **Metrics**: Prometheus on an internal port (`JOBTRACKER_METRICS_PORT`,
  default 9091). Never expose it publicly.

## Setup

You need [uv](https://docs.astral.sh/uv/), Docker, and Node 24 (`make check`
builds and tests the browser extension under `extension/`). Nothing here needs
a production credential.

```bash
git clone https://github.com/kensac/job-scripts && cd job-scripts
make sync && source .venv/bin/activate   # exactly uv.lock, into .venv

# Postgres with pgvector for this checkout. Its name and port derive from the
# checkout path, so parallel checkouts never share one.
make testdb-up && eval "$(make testdb-url)"   # sets TEST_DATABASE_URL

make test-par     # the Python suite, one database per core
make check        # everything CI gates on, including migrations and the extension
```

The tests read only `TEST_DATABASE_URL`. They refuse a database not named
`*_test`, `*_ci` or `test_*`, and they empty it between tests.
`make testdb-down` stops the container. See
[docs/agents/testing.md](docs/agents/testing.md).

## Running the API locally

The dev API runs against the same throwaway database, filled with the
generated corpus: a catalog shaped like production that holds none of its
data, with five users.

```bash
make testdb-up && eval "$(make testdb-url)"
export APP_ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
make migrate      # the schema, into the test database
make corpus       # fill it (writes ciphertext under APP_ENCRYPTION_KEY)
make dev-api      # http://127.0.0.1:8000, reloads on change
make dev-headers  # the identity headers a client must send
```

Keep the same `APP_ENCRYPTION_KEY` for `corpus` and `dev-api`. A test run
empties the database the dev API reads, so run `make corpus` again after one.
`make dev-worker` runs a worker against the same database.

`dev-api`, `dev-worker` and `migrate` use this checkout's test database unless
`JOBTRACKER_DEV_DATABASE_URL` names another. They refuse any database not
named like a disposable one. For a dev API over a synced copy of real rows,
see the comment above `dev-api` in the `Makefile`.

## Environment

`.env.example` lists every variable the code reads.

- The API and the worker read their process environment only. Nothing loads
  `.env` into them. In production the fleet's compose files set
  `DATABASE_URL`, `JOBTRACKER_SERVICE_TOKEN`, `APP_ENCRYPTION_KEY`,
  `OPENAI_API_KEY` and the rest. Locally the Makefile's dev targets set
  `DATABASE_URL`.
- `.env` holds production's DSN as `PRODUCTION_DATABASE_URL`, never as
  `DATABASE_URL`. Only `make profile`, `make profile-check` and
  `make testdb-sync` read it. See
  [docs/agents/reading-production.md](docs/agents/reading-production.md).
- Fleet-wide tunables (cycle cadence, per-cycle sizes, chunk sizes, retry
  limits) are rows in `app_config`, changed from the admin config page, not
  environment variables.

## Deployment

Images build for amd64/arm64 via GitHub Actions to
`ghcr.io/kensac/job-scripts`. `deploy/Dockerfile` bundles Chromium for
scraping. The api container runs `uvicorn api.app:app` and each worker runs
`python -m api.worker`. Both apply migrations on start. Healthcheck:
`python -m api.healthcheck`. See
[docs/agents/deployment.md](docs/agents/deployment.md).

## Configuration

Sources, source groups and filters all live in the database: sources through
the admin API, filters per user. Nothing is read from a file on disk.

## License

MIT, see `LICENSE`.
