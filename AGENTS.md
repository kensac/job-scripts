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
| [data-and-truth.md](docs/agents/data-and-truth.md) | Put a number, a state, or an inference in front of a person |
| [architecture.md](docs/agents/architecture.md) | Change the pipeline, the matcher, or anything it reads |
| [testing.md](docs/agents/testing.md) | Write a test or trust one |
| [migrations.md](docs/agents/migrations.md) | Touch the schema |
| [deployment.md](docs/agents/deployment.md) | Claim anything is deployed |
| [generated-files.md](docs/agents/generated-files.md) | Change a route, a model, or anything CI regenerates |
| [frontend.md](docs/agents/frontend.md) | Change any user-visible surface |

## Repository map

- `src/api/`: FastAPI application. Routers, task handlers, matching, pipeline.
- `src/core/`: provider datasheets, pricing, routing, storage primitives.
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

- `make check`: lint, format, types, compile, tests. CI gates on this.
- `make testdb-up` / `make testdb-down`: disposable test database.
- `make profile`: re-measure production into `tests/production_profile.json`,
  which is what the generated test corpus is built from. See testing.md.
- Regenerate `openapi.json` when routes change.
