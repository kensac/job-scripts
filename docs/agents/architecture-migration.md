# Moving to facts, derivations and projections

The shape this codebase is moving to, the order it moves in, and what may be
done without a person watching. Read this before taking a phase.

These are instructions. The reasoning is here because every phase is taken by
someone who was not present for the decision.

## Why

The recurring defect is **two places that decide the same thing**. They
disagree, and the disagreement is invisible because every test passes: the
tests assert each definition against itself.

The second defect is **state that changes with no record that it changed**.
`jobs.active` flipped from false to true when a board re-listed a posting, and
nothing could observe the flip. Phase 3 replaced it with observations.

The third is **identity entangled with implementation**. A verdict is keyed by
`(url, check_type, prompt_hash, model)`. That one was examined and kept on
purpose (phase 4, below).

## The shape

Four layers, by what the data is, not by which feature it serves.

**Facts.** Append only, never updated. A source listed a posting. A page was
fetched and here is its content. A message arrived. A fact is an observation
with a time, and observations are not edited.

**Derivations.** Pure functions of facts, cached, addressed by the content
they read and the recipe that read it. A derivation declares its inputs, its
cost, and when it goes stale. Adding one is a registration, not an edit to
every query that mentions its name.

**Projections.** What a person sees, materialised per person, with exactly
one definition. A projection is rebuilt from derivations and never patched in
place. Deleting one and recomputing it must be a no-op.

**Person state.** Applications, statuses, notes. Small, mutable, ordinary.

The property that makes it hold: **derived data is reproducible, so it can be
thrown away**. Anything that cannot be rebuilt from facts is a fact.

## The phases, in order

Which phase is done, and what the current one has measured, is issue #508.
This document is the rules; that issue is the working state.

Each phase ships on its own and leaves the system working.

| # | Phase | State |
|---|---|---|
| 0 | The layering is enforced | **Done.** `lint-imports` runs in CI over the contracts in `pyproject.toml` |
| 1 | Check types are a registry | **Done.** A new posting check is a `core.checks.POSTING_CHECKS` registration |
| 2 | A board row and the working set are told apart | **Done.** `user_jobs` holds person state, `user_job_working_set` what the filters picked |
| 3 | Catalog observations are facts | **Done.** Availability is `source_observations`; `jobs.active` is frozen |
| 4 | ~~Derivations are content addressed~~ | **Dropped.** Measured; see below |
| 5 | Files move to the shape | **Done.** `tasks` is a sibling of `api` and `core` |
| 6 | The long files are split | **Done for the named multi-job modules** |
| 7 | Every operation declares what it returns | **Done** for responses ([generated-files.md](generated-files.md)); typed reads continue per domain |
| 8 | One kind of record per table | **In progress.** Readers name a kind through a view; see below |

**The API contract is a price, not an invariant.** `openapi.json` is canon
and `tests/test_openapi_current.py` fails the build when routes and schema
disagree. Changing an operation is allowed when the change says what it buys
against what it costs. The cost is not the backend change: a frontend
repository and a browser extension read that contract, so a changed operation
is three coordinated changes, one of which ships to a browser somebody has to
reload. Read `extension/README.md` before assuming a response shape is
internal. Additive changes and declarations that leave the wire unchanged are
cheap; take the permission for anything else only when you can name the
operation, the consumer, and why the current shape makes the work worse.

## Phase 8: one kind of record per table

`ai_queries` holds decided answers about a posting. Page text and call usage
have left it. Readers name the kind they read through a view, so changing the
storage behind a kind changes the view and no reader.

- **`verdicts`** is the decided answers. `tests/test_verdicts_view.py` fails
  when a new reader restates which rows are answers instead of reading the
  view, and when a module outside `core/verdict_reads.py` writes a
  latest-answer shape (`ORDER BY id DESC` over the view, or an `EXISTS` on one
  posting's answers); its allow-list names each exception and why.
- **How an answer is read is `core/verdict_reads.py`'s**: `latest`,
  `latest_status`, `read_latest`, `latest_per`, `latest_checks` (keyed on
  `jobs.id`, not url), `closed_verdict`, `has_verdict`, `verified_open`. What
  a posting is now is its latest answer, never "ever rejected" or "ever
  passed": two readers that asked that disagreed with the board about 313
  active postings (2026-10-10).
- **`page_texts`** is the fetches that brought text back. A custom filter's
  input, which wraps the page with company and title, is not page text.
- **Page fetches are their own table, `page_fetches`, written only by
  `core.page_fetches.record`.** `core.store.ai_result_row` refuses
  `check_type = 'content'`, so a fetch cannot be written where no view reads
  it. Fetch ids come from `ai_queries_id_seq`, so every `content_row_id` that
  named a fetch before the move still does.
- **`content_row_id` on `job_profiles`, `job_requirements`, `job_embeddings`
  and `job_comp` has no foreign key.** Some rows name an answer's id rather
  than a fetch (on 2026-10-10: 3,502 of 84,052 profiles, 6,610 of 59,645
  requirements, 6,749 of 217,212 embeddings, 52 of 48,239 comp rows). A store
  guard that refuses an "older" write by comparing ids must treat a stored id
  that names no `page_texts` row as older than any. The embeddings guard did
  not, and re-paid for the same 126 postings every hour until 2026-10-18.
- **An answer points at what produced it**: the fetch it judged
  (`ai_queries.page_fetch_id`) and the call that paid for it
  (`model_call_id`). A writer carries the fetch with its text: `store.Page`
  from `get_contents` and `api.ai.verdicts.refresh_page`, a board run's frozen
  `content_query_id`, and `page_fetch_id` in a batch request's context. A
  batched answer finds its call by `(batch_id, url) = (provider_batch_id,
  custom_id)`, because the receipt checkpoint records the call before any
  consumer writes an answer. `run_check` books a live call itself
  (`budget.book_live`) in the transaction that writes its verdict. An answer
  no fetch or call produced (near-copy reuse, an ATS closure, ingest) has
  neither.
- **Writers store no copy of the input or the usage.** What an answer was
  asked is `core.answer_inputs.sql` over its fetch. Readers read both through
  `ledger_rows`: the input rebuilt from the fetch, and the call's numbers on
  the first answer naming the call with zeros on its siblings. Each derived
  column is a correlated lookup in the select list, not a join, because a
  UNION ALL arm is flattened into the outer query only while its FROM is one
  table, and the admin ledger's newest-first pages need it flattened
  (`tests/test_admin_jobs_read_plan.py`).

### The ledger of paid model calls

The rule is in [observability.md](observability.md) ("Every paid call is one
row in `model_calls`"). What follows is the evidence that the old copies
agreed, so the backfill could take them as recorded.

Measured against production on 2026-10-10, before the copies were dropped:

- For 7,466 of 7,541 provider batches since 2026-09-13, the receipts' input
  and output tokens equalled `ai_batches` exactly. The other 75 recorded no
  tokens (70 open, one expired, four completed with neither tokens nor
  receipts).
- No `(batch_id, url)` group held more than one answer row with tokens, so a
  paid answer row is exactly one call: 438,844 calls mapped to 931,033 rows
  (up to five answers from one verify call).
- Before 2026-09-13 the old usage ledger booked batched user filter work at
  live prices (the week of 2026-08-31: $21.60 booked against $11.42 batched).
  That era's cost comes from the batches, not that ledger, so windows reaching
  before 2026-09-13 read lower than they did.
- Repricing every receipt at 2026-10-10 rates came to $7.32 more than the
  batches recorded, which is why a cost is stored as recorded and never
  repriced.

A sum over a window reads `model_calls.answers_with_usage` (one join and one
aggregate), not `ledger_rows`: on 2026-10-10 the 30-day cuts took 6.6 s
against 11 min 51 s. A page of rows reads the view.

## What may be done unattended

A merge reaches every host on Renovate's cadence with nobody watching
([deployment.md](deployment.md)), so "the tests passed" has to be enough
before a change can land that way.

**May be taken by an agent, PR opened and merged on green CI:** every phase,
by Kanishk's standing instruction of 2026-09-10. A phase still brings its own
evidence.

**Still stop and ask:** a change whose failure would be silent in production
and invisible in CI, such as one that changes the population the sweeps pay
for or what a source observation means. Bring the parallel cutover's numbers
first.

**Never in a loop:** any write to the production database, and any migration
that can refuse to apply ([migrations.md](migrations.md)). The standing
instruction does not cover either, because CI gates neither.

## Phase 2: the working set is not person state

`user_jobs` is what a person did. `user_job_working_set` is what the filters
picked, which is scope for paid sweeps. Visibility is `board_visible`.
[visibility.md](visibility.md) holds the mechanics. Two product rules:

- **The daily digest announces postings that newly became visible**:
  `board_visible` rows whose `computed_at` is after the person's last digest,
  not newly admitted working-set members.
- **Analytics report three labelled populations**: acted on, working set and
  visible. The person-facing surface shows visible and acted on; the admin
  surface shows all three. They are spelled once,
  `api.board.populations.per_user_counts` and `person_state.PERSON_STATE`.

## Phase 4 was dropped

It would have made a verdict's identity `(input_hash, recipe_id)` so that
changing the model did not invalidate one. It contradicted a deliberate
decision: a verdict from a different model is not this model's verdict, and
keeping the old model's answers after switching is a stale corpus, not a
saving. What replaced it keeps model identity and lets measured pairs share
answers by explicit, revertable groups (architecture.md, "A verdict counts
for the model that gave it").

**Reframed as staleness, it was small.** Share of latest verdicts whose url
has a newer page than the verdict, 2026-09-10:

| check | latest verdicts | judged on a page since replaced |
|---|---|---|
| closed | 66,022 | 170 (0%) |
| clearance | 66,016 | 2,532 (3%) |
| custom | 38,819 | 765 (1%) |

Pages are re-fetched mainly by the re-verification sweep, so "the page
changed" is partly a measure of how often we look. `tasks/rescrape.py`
re-stamps rather than re-pays an unchanged page for the derived tables, and
is there to extend the day a measurement asks for it.

**Derived posting contracts live in `core.derivations`.** Compensation periods
and conversion, embedding dimensions, job-profile and requirement
vocabularies, and skill normalization are shared by their worker, API, and
ORM readers. A migration freezes its own schema values and does not import a
live contract.

**A derived fact is one registration.** `tasks.DERIVATIONS` lists every
answer a model derives from data already held (pay, requirements, job
profiles, embeddings, locations), each a `tasks.derivations.sweep.Derivation`
naming its table, its input (page text cut to `input_chars`, or not page
text), its model routing, its staleness rule (`select`) and its store.
`tasks.derivations.sweep` owns the one execution path: switch, one pass in
flight, selection, the unchanged-page skip, batch submit and collect, the
page-currency check, parse, store. The worker schedules and dispatches every
entry from the list, so a new derivation touches neither. A switched-off
feature stays registered; its `switch` stops scheduling and hand-started
submits, while paid batches are still collected. A recipe version re-derives
only where the table stores it (`job_profiles.classifier_version`). Pay is
`job_comp`, keyed by url; `jobs` holds only what listings say.

`jobs.near_copy_key` is not a registration: it costs nothing, is computed
before verify submits, and records the text verification read. Recomputing it
from a newer page would match a twin on text its verdict never judged.

## Phase 3: what a source observation means

Kanishk decided these on 2026-10-10. The tables encode them.

1. **A job is its url.** `jobs.url` is the identity; `ats.canonicalize` makes
   one spelling per posting. Each source's listing is its own observation, so
   one url on a company board and an aggregator is one job with two
   histories.
2. **A posting listed only by switched-off sources is not available.** A
   switched-off source is never pulled, so nothing it says can be refreshed.
   `sheet_import` counts as switched off.
3. **Still listed but no longer admitted by the title pattern is `filtered`,
   never a closure.** The observation records whether the pattern matched;
   enforcement is applied when availability is read, so flipping
   `source_title_patterns_enabled` does not rewrite history.
4. **Aggregator absence never means closed.** It is `not_listed` and leaves
   the posting available. A complete pull of a board in `boards.AUTHORITATIVE`
   records absence as `unlisted`, which makes it unavailable. A pull that
   cannot show it saw everything (`PartialPull`, or an empty pull) records no
   absence. A feed record carrying its own inactive flag is `unlisted`.
5. **Changes only, kept forever.** A row is written when a source says
   something other than its latest row for that job, never per pull.

### The tables

`source_observations` is the fact: `(job_id, source, kind, run_id, at)`,
kinds `appeared`, `reappeared`, `filtered`, `unlisted`, `not_listed`, written
by `catalog.observe` after each pull and by `catalog.set_active` for an
administrator's correction (source `admin`, `CORRECTION_SOURCE`). `run_id` is
the `ingest_source` task; its progress carries the pull's counts and whether
it was complete.

`job_listing_events` holds the history from before observations began. Its
true rows are certain returns; its false rows cannot be split into unlisted,
filtered or switched off. History is not invented: the first observation of a
posting is when the log began watching, not when it was first listed.

**Availability is a projection with one definition, `catalog.AVAILABLE`**
(`core/catalog/observations.py`, which comments each branch). An
administrator's correction holds while it is the job's latest observation.
Otherwise a job is available when some switched-on source's latest
observation is `appeared`, `reappeared` or `not_listed`, or `filtered` while
enforcement is off. Enforcement is read from `app_config` inside the SQL, so
`AVAILABLE` takes no parameter.

**Availability is stored, in `jobs.available`, and readers use
`catalog.IS_AVAILABLE`.** Computed per row, `AVAILABLE` took the AI-eligible
count from 2.0 s to 6.5 s and a board recompute from 27 s to 38 s
(production, 2026-10-10). Where no observation decides, the column stores
the posting's last known feed state (`_STORED`). Three writers keep it:

- `catalog.observe` refreshes the rows its observations changed, after they
  commit, in batches of 500.
- `catalog.set_active` refreshes its row in its own transaction.
- `catalog.reconcile_available`, in the hourly `retire_switched_off` task,
  writes every row where the stored value differs, in id chunks, skipping
  rows another writer holds. Source switches and the title pattern setting
  land here within the hour.

`_refresh_available` locks in url order, then evaluates in a second
statement: under READ COMMITTED that snapshot is taken after the locks, so it
sees every observation committed before them.

### Contract: retiring jobs.active

`jobs.active` is frozen: nothing reads it and nothing writes it. It keeps the
last value each feed wrote because data is retained ("Always retain all data"
in [engineering-standards.md](engineering-standards.md)), and so does
`job_listing_events`. Dropping either is Kanishk's decision.
`tests/test_availability_readers.py` fails on a new reader of `j.active` and
on any write to the column outside the dev seed.

## Phases 5 to 7: layering, splits, typed reads

**The service boundary.** `pyproject.toml` forbids `core -> api` and
`api -> tasks`. `tasks` must stay a sibling of `api`, because import-linter's
`forbidden` skips a target inside its source. The ignored imports are the
worker's own (`api.worker` and `api.worker_lifecycle` load the handlers and
the runtime); they are not debt. A shape or constant `api` needs from a task
belongs in `core/shapes.py`.

**A split needs evidence that a module owns several behaviours**, not file
length. A router split must keep registration order where it matters:
FastAPI matches in order, so a literal path registered after a parameterised
one that would swallow it is a live defect (`/queries/options` before
`/queries/{query_id}`). Regenerate `openapi.json`, whose keys follow that
order.

**Reads carry a shape.** `db.query_as(Shape, sql, params)` and its one-row
sibling map rows into a declared shape at the read; the SQL is unchanged, and
a column the shape does not declare raises. Adopt it where a row is read, not
where one becomes a task payload. Keep hand-tuned SQL rather than an ORM on
the hot paths: their plans are measured and have to stay readable. A `SELECT
*` cannot be typed until it names its columns.

## Working in a stack

`git-spice` is set up, trunk `main`. `git-spice branch restack` rebases onto
the LOCAL base branch, so `git fetch` and move `main` first. A pull request
whose base branch is deleted on merge is closed by GitHub, not retargeted:
retarget it at `main` before merging its base.

## Revising this document

A phase that opens the code may find this plan wrong. Change the document in
the same pull request as the work, and say in the commit what the evidence
was. A phase that turns out to be unnecessary is a finding: record that it
was dropped and why.

## Parallel cutover

A phase that changes how something is computed builds the new answer beside
the old one and compares them on production data before the old one goes.
Agreement on a sample is the evidence that the cutover is safe, and a
disagreement is a finding whichever way it falls. The old path goes in a
separate commit from the new path arriving, so a revert is one commit.
