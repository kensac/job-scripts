# Job Scripts

A multi-user job-application tracker. Sources (GitHub job boards, Airtable
views) are ingested hourly into a shared catalog, run through AI filters
(closed / visa-clearance / per-user custom prompts), and served to a
spreadsheet UI where each user tracks their applications.

## Components

| Path | What it is |
|---|---|
| `src/api/` | FastAPI backend (`api.app`), task worker (`api.worker`), SQLAlchemy models + Alembic migrations |
| `src/core/` | Shared pipeline: fetching, scraping (headless Chromium), AI checks, verdict/content cache, catalog |
| `src/trackers/run_tracker.py` | Legacy CLI: appends filtered jobs to a Google Sheet |
| `src/trackers/backfill.py` | One-time import of sources, filters, presets, and sheet rows onto a user |
| `alembic/` | Schema migrations, applied automatically on API/worker start |
| `openapi.json` | Generated API schema (`python -m api.export_schema`), canon for frontend types |

## Architecture

- **API** (`uvicorn api.app:app`): multi-tenant REST API. Identity arrives via
  trusted headers from an authenticating proxy (`X-Service-Token`,
  `X-User-Sub`, `X-User-Groups`); Authentik groups drive entitlements and
  weekly AI-token budgets (`group_budgets` table).
- **Workers** (`python -m api.worker`): claim tasks from a Postgres queue
  (`FOR UPDATE SKIP LOCKED`) — safe across any number of machines. Handle
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

## Environment

```ini
DATABASE_URL=postgresql://...            # required everywhere
JOBTRACKER_SERVICE_TOKEN=...             # API auth (proxy-held secret)
APP_ENCRYPTION_KEY=...                   # Fernet key for stored user API keys
OPENAI_API_KEY=...                       # shared key for budgeted users + ingestion
GOOGLE_APPLICATION_CREDENTIALS_CUSTOM=   # legacy sheet tracker only
```

Optional worker knobs: `JOBTRACKER_WORKER_POLL`, `JOBTRACKER_WORKER_KINDS`
(CSV of task kinds to claim), `JOBTRACKER_INGEST_SCHEDULER`,
`JOBTRACKER_INGEST_INTERVAL_MINUTES`,
`JOBTRACKER_OWNER_KEY_MODELS`, `JOBTRACKER_ADMIN_GROUPS`.

## Running

```bash
pip install -r requirements.txt
export PYTHONPATH=src

uvicorn api.app:app --port 8000      # API (migrates + seeds on start)
python -m api.worker                 # worker (any number, any machine)
python -m trackers.run_tracker ft    # legacy sheet tracker (config/group name)
python -m trackers.backfill --help   # one-time user/sheet import
```

Container images build for amd64/arm64 via GitHub Actions to
`ghcr.io/kensac/job-scripts`; `deploy/Dockerfile` bundles Chromium for
scraping. Healthcheck: `python -m api.healthcheck`.

## Configuration files

- `configs.toml` — source feeds (name → listings URL + sheet id). Seeds the
  `sources` and `source_groups` tables; runtime source management is in the
  DB via the admin API.
- `filters.toml` — named filter prompts for the legacy CLI
  (`--apply-filter <name>`); product filters live per-user in the DB.

## License

MIT — see `LICENSE`.
