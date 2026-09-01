## General
- Do not give me .md files or summaries at all
- Do not write additional code beyond what is requested
- If you have a better solution than what is requested, interrupt and suggest it
- Avoid reinventing the wheel; use existing libraries and frameworks where appropriate
- Follow best practices for the specific programming language
- Ensure code is clean, well-structured, and maintainable
- Do not write scripts unless explicitly requested or necessary
- Never attribute Claude in commits or PRs
- Report conclusions, not narratives of what happened
- Keep this file short and current; update it when a decision or fact is learned. Never commit it.

## Python-Specific
- Do not write tests at all
- Do not write docstrings or comments unless absolutely necessary
- Keep code efficient but readable

## Workflow
- Backend changes go through PRs (squash merge, auto-merge on green). Frontend commits direct to main.
- Merge to main == deployed: CD auto-rolls all three hosts within ~an hour. Keep main release-ready.
- Tell homelab BEFORE merging anything that changes the task-claim/heartbeat/reaper contract, or any non-additive migration.
- Gates: `make check`, `pytest -q tests`, and openapi.json must be regenerated if routes change.

## Environment facts
- Read-only prod DB access: `set -a && . ./.env && set +a` exports DATABASE_URL. SELECT/EXPLAIN only.
- Containers run TZ=America/New_York (deliberate fleet convention); hosts and Postgres are UTC.
- `ai_queries.created_at` is TEXT written as naive local time, so it reads 4h behind DB `now()`. Every window query is shifted until this is fixed.
- Scale: ai_queries ~73k rows/422MB (235MB is TOASTed input_content), jobs ~49k, tasks ~2.4k and never pruned.

## Architecture invariants
- Job visibility is a read-time conjunctive predicate, nothing derived is stored. It is spelled in three places (`routers/jobs.py:_VISIBILITY`, `worker.py:_materialize_passing`, `worker.py:_candidates`) — change them together or they drift.
- Verdicts are an append-only log; latest row per (url, check_type) wins. Never write a verdict from cached text that may predate a closure.
- Sync AI only where a human waits (uploads, live filter runs, explain, improve-prompt). Everything scheduled batches at half price.
- `verdicts.py` is meant to be the only path that records verdicts; several worker paths still bypass it.

## Verification habits that caught real bugs
- Check behavior against prod data, not against config. A flag set correctly in compose gated only one of two code paths for five days.
- Dry-run `health.detect()` against prod before shipping detector changes.
- Measure before optimizing and say so when something measures fine.
