# Working on this project

Instructions for any coding agent working in this repository. Read this file
first, then the topic document covering what you are about to do.

These are instructions, not history. Follow them.

## Non-negotiable

1. **Production-grade only.** No patches, no hotfixes. If a design turns out
   wrong, rearchitect it. Never trade correctness for a smaller diff.
2. **Measure before you assert.** A mechanism inferred from reading code is a
   hypothesis until you count the rows it applies to. State what your sample
   can support, and count "cannot tell" separately.
3. **Before asserting what a system does, open the thing that does it.** Not
   the documentation about it, not your own earlier finding, not a summary.
   This applies hardest when the description is yours.
4. **Never attribute an AI assistant anywhere it is written down.** No tool or
   model name, no "generated with", no `Co-Authored-By` trailer, no "as an AI"
   aside: in commit messages, PR titles and bodies, code comments, docs, or
   anything else that lands in the repository. See
   [working-agreement.md](docs/agents/working-agreement.md) for why this needs
   checking rather than remembering.
5. **The production database is read-only.** SELECT and EXPLAIN only, unless
   explicitly authorised for a specific operation.
6. **Never send an authenticated request to the production API.** Any
   authenticated request auto-provisions a user row, which is a write.

## Topic documents

| Read this | Before you |
|---|---|
| [working-agreement.md](docs/agents/working-agreement.md) | Take a task, own a feature, or coordinate with another agent |
| [engineering-standards.md](docs/agents/engineering-standards.md) | Write or change any code |
| [config-registry.md](docs/agents/config-registry.md) | Change a persisted app setting or its validation |
| [data-and-truth.md](docs/agents/data-and-truth.md) | Put a number, a state, or an inference in front of a person |
| [admin-query-reads.md](docs/agents/admin-query-reads.md) | Change admin model-call ledger, posting inspection, or stats routes |
| [architecture.md](docs/agents/architecture.md) | Change the mail pipeline, the matcher, pricing, or a named place |
| [sources-and-boards.md](docs/agents/sources-and-boards.md) | Add a board, change ingest, or touch how a posting is fetched |
| [visibility.md](docs/agents/visibility.md) | Change who sees a posting, who owns a row, or a list response shape |
| [observability.md](docs/agents/observability.md) | Add a task, batch model calls, or add a metric, alert or event |
| [reading-production.md](docs/agents/reading-production.md) | Point anything at the production database |
| [architecture-migration.md](docs/agents/architecture-migration.md) | Take a phase of the move to facts, derivations and projections |
| [testing.md](docs/agents/testing.md) | Write a test or trust one |
| [migrations.md](docs/agents/migrations.md) | Touch the schema |
| [deployment.md](docs/agents/deployment.md) | Claim anything is deployed |
| [generated-files.md](docs/agents/generated-files.md) | Change a route, a model, or anything CI regenerates |
| [frontend.md](docs/agents/frontend.md) | Change any user-visible surface |
| [extension-runtime.md](docs/agents/extension-runtime.md) | Change the extension application runtime |

## Repository map

- `src/api/`: FastAPI application. Routers, the worker loop, matching, pipeline.
  Filter route implementations live in `src/api/routers/filtering/`; the
  `filters.py` and `filter_insights.py` modules register their routers. Keep
  related route handlers, request shapes, and domain queries together there.
  Source administration implementations live in
  `src/api/routers/source_management/`; `source_admin.py` registers them.
- `src/api/managed_boards/`: managed-board candidate selection, run admission,
  stored run records, and projection writes. Import public operations from
  `api.managed_boards`; keep transaction boundaries in the owning module.
- `src/tasks/`: the handlers the worker runs, grouped by domain where a
  workflow has several stages. `tasks.derivations` owns the shared model
  derivation sweep and its compensation, requirements, job profile, embedding,
  and location registrations. Register them in `tasks.DERIVATIONS`; keep
  candidate selection and storage with each registration. `tasks.mail` owns
  sync, classification, matching, and pointer backfill. `tasks.verification` owns new-posting
  verification, re-verification, and their shared question construction;
  `tasks.verify` keeps the worker-facing handler exports. `tasks.runtime` owns
  worker execution.
- `src/core/`: provider datasheets, pricing, routing, task shapes, storage
  primitives.
- `alembic/`: migrations. See migrations.md before adding one.
- `tests/`: three populations against a real database: hermetic, the
  generated corpus, and a synced copy of production. See testing.md.
- The frontend lives in a separate repository, `personal-portfolio`, under
  `app/job-tracker/` (a person's own data) and `app/job-scripts/` (the
  administrative view). It is not documented there: [frontend.md](docs/agents/frontend.md)
  in this repository carries the standards a change to those surfaces must
  meet, so a feature can be specified from here and coordinated with whoever
  is working in that repository.

## Commands

- `make check`: lint, format, types, compile, em dashes, migrations (apply
  and match the models), the extension, and the tests in parallel. CI gates on
  the same list; a gate that is more than one command is a make target CI
  calls.
- `make testdb-up` / `make testdb-down`: disposable test database.
- `make profile`: re-measure production into `tests/production_profile.json`,
  which is what the generated test corpus is built from. See testing.md.
- Regenerate `openapi.json` when routes change.
