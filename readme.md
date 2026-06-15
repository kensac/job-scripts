# Job Scripts

Automated tracking and filtering of job postings. The pipeline fetches listings
from multiple sources, runs them through AI-based filters, deduplicates against a
PostgreSQL cache, and appends the survivors to a Google Sheet.

## Pipeline

For each configured source the tracker will:

1. **Fetch** postings from the source (a `listings.json` feed or an Airtable
   shared view).
2. **Pre-filter** on activity, location, application term/season, posting date,
   and duplicates (against both the sheet and prior runs).
3. **Extract** each posting's page content with a headless Chrome browser
   (cached in Postgres so it is scraped at most once).
4. **AI-filter** the content through OpenAI checks:
   * **closed** — posting is no longer accepting applications.
   * **clearance** — requires security clearance / citizenship, or excludes
     visa sponsorship / F1 candidates.
   * **custom** — an optional, user-defined prompt (see `filters.toml`).
5. **Append** the remaining postings to the target Google Sheet.

All AI verdicts and scraped content are stored in Postgres, so re-runs only pay
for work they haven't done before.

## Sources

* **`listings.json` feeds** — e.g. the Simplify / ouckah internship and new-grad
  repos.
* **Airtable shared views** — public share links of the form
  `https://airtable.com/app.../shr.../tbl...`. These are read through Airtable's
  shared-view data endpoint and mapped onto the same `JobPosting` shape, so they
  flow through the identical pipeline. A source is treated as Airtable whenever
  its URL points at `airtable.com`.

## Prerequisites

* **Python 3.11+** (uses the stdlib `tomllib`).
* **Google Chrome + chromedriver** for headless content extraction.
* **PostgreSQL** database for the results/content cache.
* **OpenAI API key** for the AI filters.
* **Google service account** with a JSON key and write access to the target
  sheet(s).

## Installation

```bash
git clone https://github.com/kensac/job-scripts.git
cd job-scripts
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

### Environment (`.env`)

```ini
DATABASE_URL=<postgres-connection-string>
GOOGLE_APPLICATION_CREDENTIALS_CUSTOM=<path-to-service-account-key.json>
OPENAI_API_KEY=<your-openai-key>
CUSTOM_FILTER_PROMPT=<optional default custom-filter prompt>
```

* **`DATABASE_URL`** — Postgres connection string for the AI/content cache
  (required; the store connects on import).
* **`GOOGLE_APPLICATION_CREDENTIALS_CUSTOM`** — path to the service account key.
  Share each target sheet with the key's `client_email`.
* **`OPENAI_API_KEY`** — enables the closed/clearance/custom AI checks. Without
  it, only the non-AI pre-filters run.
* **`CUSTOM_FILTER_PROMPT`** — optional; the `default` custom filter. Named
  filters live in `filters.toml`.

`SHEET_ID` and `JOB_LISTINGS_URL` are set automatically per run from
`configs.toml`; you only need them in `.env` when running a source directly.

### Sources and groups (`configs.toml`)

Each config maps a name to a sheet and a source URL; groups are named lists of
configs run in sequence:

```toml
[configs.fulltime]
sheet_id = "<google-sheet-id>"
job_listings_url = "https://raw.githubusercontent.com/.../listings.json"

[configs.airtable1]
sheet_id = "<google-sheet-id>"
job_listings_url = "https://airtable.com/app.../shr.../tbl...?viewControls=on"

[groups]
ft = ["fulltime", "fulltime_ouckah", "fulltime_ouckah_2027"]
airtable = ["airtable1", "airtable2"]
```

Add or regroup sources by editing this file only — no code changes needed.

### Custom filters (`filters.toml`)

Named, prompt-based filters selectable with `--apply-filter <name>`. The
`default` filter comes from `CUSTOM_FILTER_PROMPT`. See the comments in
`filters.toml` for examples.

## Usage

Run a single config or a group by name:

```bash
python -m trackers.run_tracker <config-or-group>
```

Examples:

```bash
python -m trackers.run_tracker fulltime      # one config
python -m trackers.run_tracker ft            # a group of configs
python -m trackers.run_tracker airtable      # the Airtable sources
```

### Flags

* `--batch` — use the OpenAI Batch API (cheaper, asynchronous) for AI checks.
* `--limit N` — only AI-check the first N unseen jobs (useful for testing).
* `--apply-filter <name>` — apply a named filter from `filters.toml`, reusing
  cached content and closed/clearance verdicts, and add newly passing jobs.
* `--retry` — retry jobs whose AI checks previously failed.
* `--reevaluate-custom` — re-run the custom filter over cached jobs and add any
  that now pass to the sheet.

## Project layout

* **`src/core/pittcsc_simplify.py`** — fetching, filtering, AI checks, and sheet
  writing.
* **`src/core/configs.py`** / **`configs.toml`** — source and group definitions.
* **`src/core/filters.py`** / **`filters.toml`** — named custom filters.
* **`src/core/store.py`** — PostgreSQL cache for AI verdicts and scraped content.
* **`src/core/batch.py`** — OpenAI Batch API helper.
* **`src/trackers/run_tracker.py`** — CLI entry point.

## Troubleshooting

1. Confirm the service account has write access to each target sheet.
2. Verify `DATABASE_URL` is reachable — the store connects on import.
3. Ensure Chrome/chromedriver are installed for content extraction.
4. Check source URLs are reachable; Airtable share links must be public.
5. Review the logs for errors or warnings.

## License

MIT — see `LICENSE`.
