# Moving to facts, derivations and projections

The shape this codebase is moving to, the order it moves in, and what may be
done without a person watching. Read this before taking a phase.

These are instructions. The reasoning is here because every phase is taken by
someone who was not present for the decision.

## Why

The recurring defect is **two places that decide the same thing**. Board
membership is decided by `demote_closed` (deletes rows), by
`materialize_passing` (inserts rows) and by `visibility.FULL` (computes the
answer from scratch). They disagreed, and the disagreement was invisible
because every test passed: the tests asserted each definition against itself.

The second defect is **state that changes with no record that it changed**.
`jobs.active` flips from false to true when a board re-lists a posting, and
nothing could observe the flip, so a closed verdict was permanent. The fix
shipped as `jobs.relisted_at`, which is a timestamp reconstructing an event
that should have been a fact.

The third is **identity entangled with implementation**. A verdict is keyed by
`(url, check_type, prompt_hash, model)`, so changing the model invalidates
every verdict and re-pays for the corpus. Which model answered is provenance,
not identity.

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

Each phase ships on its own and leaves the system working. The order is not
arbitrary: the file moves are LAST, because moving files before the seams are
real only relocates the problem.

| # | Phase | Done when |
|---|---|---|
| 0 | The layering is enforced | An import contract fails CI on a new upward edge |
| 1 | Check types are a registry | **Done.** A new posting check is a `POSTING_CHECKS` registration; dispatch does not grow a purpose ladder |
| 2 | A board row and the working set are told apart | **Done 2026-10-10.** `user_jobs` holds person state, `user_job_working_set` what the filters picked; every scope reader moved with a production comparison (2b, below) |
| 3 | Catalog observations are facts | A re-listing is an appended row, not a mutated column. **Dual write in progress**; see below |
| 4 | ~~Derivations are content addressed~~ | **Dropped 2026-09-10.** Measured; see below |
| 5 | Files move to the shape | **Done.** `tasks` is a sibling of `api` and `core`; domain seams, not directory names, own the remaining moves |
| 6 | The long files are split | **Done for the named multi-job modules.** `resolve.py` is a 160-line router, `health.py` an 85-line aggregator, and `jobs.py` a 22-line ordered aggregator |
| 7 | Every operation declares what it returns | **Done 2026-09-11.** 188 of 190 declare a model; the other two serve a file and the schema itself. Guarded, including against a `Decimal` field and a name a generator cannot use |
| 8 | One kind of record per table | **In progress.** `ai_queries` holds page text, verdicts and call usage; readers move to a name per kind before storage splits. See below |

**The API contract was the invariant, and is now a price.** `openapi.json` is
canon and `tests/test_openapi_current.py` fails the build when routes and
schema disagree. Every phase to this point left it alone.

Kanishk lifted that on 2026-09-10, for the late phases, and left the call to
whoever is taking one. So the rule is no longer "never"; it is "say what it
buys, against what it costs".

What it costs is not the backend change. A frontend repository and a browser
extension read that contract and neither is in this repository, so a changed
operation is three coordinated changes, and one of them ships to a browser
somebody has to reload. The extension holds its own copy of what the API
returns: read `extension/README.md` before assuming a response shape is
internal.

Nothing in phases 6 or 7 needs it. They are a module split and a row type,
both of which stop at this repository's edge. Take the permission when a
phase can say which operation, which consumer, and why the shape it has now
makes the work worse.

## Phase 8: one kind of record per table

`ai_queries` holds three kinds of record: page text (`check_type =
'content'`, 869,276 rows on 2026-10-09), decided answers about a posting
(about 1.9 million), and the token usage and cost of every call, written on
the answer rows. Most answer rows also carry a copy of the page text they
judged; 11,157 urls had text only on an answer row, so page text cannot be
split off by moving the content rows alone.

The order is readers first, storage second. A reader names the kind it reads
through a view, so changing the storage behind a kind changes the view and no
reader:

1. `verdicts`: decided answers. Done.
2. `page_texts`: page text, the same way. Done. It is the fetches that
   brought text back. A custom filter's input, which wraps the page with the
   company and title, is not page text, and neither is the copy of the page
   an answer carries: that is what the model saw, read only by the admin row
   explorer.
3. Page fetches as their own table, `page_fetches`, written only by
   `core.page_fetches.record`. Done. A fetch is a fact with none of an
   answer's columns. `core.store.ai_result_row` refuses `check_type = 'content'`,
   so a fetch cannot be written where no view reads it. Ids come from
   `ai_queries_id_seq`: the 910,622 fetches stored in ai_queries until
   2026-10-10 kept their ids when they moved, so every `content_row_id` that
   named one still does. For the 11,189 urls whose only text was a copy on a
   closed or clearance answer, the copies a reader picked became fetches with
   method `verification` and the answer's id. `ledger_rows` (ai_queries plus
   the fetches, in ai_queries' shape, for the admin ledger, board spend and
   the derivation scopes) leaves those out, because the answer is already
   listed.
   - `content_row_id` on `job_profiles`, `job_requirements`,
     `job_embeddings` and `job_comp` has no foreign key: some
     rows name a copy on an answer in ai_queries (on 2026-10-10: 3,502 of
     84,052 profiles, 6,610 of 59,645 requirements, 6,749 of 217,212
     embeddings, 52 of 48,239 comp rows). Such a row is not a url's newest
     fetch, so it does not name the url's current text.
   - An answer points at the fetch it judged (`ai_queries.page_fetch_id`)
     and the call that paid for it (`model_call_id`). A writer carries the
     fetch with its text: `store.Page` from `get_contents` and
     `verdicts.refresh_page`, a board run's frozen `content_query_id`, and
     `page_fetch_id` in a batch request's context for the collector. A
     batched answer finds its call when it is inserted, by `(batch_id, url)
     = (provider_batch_id, custom_id)`, because the receipt checkpoint
     records the call before any consumer writes an answer from it.
     `run_check` books a live call itself (`budget.book_live`), in the
     transaction that writes its verdict; its caller does not book it again.
     An answer no fetch or no call produced (near-copy reuse, an ATS
     closure, ingest) has neither.
   - What an answer was asked is `core.answer_inputs.sql` over its fetch:
     the page, wrapped with the company and title for a custom filter, cut
     where its code path cut it. `tasks.answer_links`, queued each cycle
     until a run links nothing, fills both pointers on older answers, only
     where exact: a fetch whose rebuilt input equals the stored copy byte
     for byte, and a call by the joins in the ledger section below. Text
     an answer saw that no fetch holds becomes a fetch first (method
     `verification`, the answer's id). Where that answer is newer than every
     fetch of its url, its text becomes the url's current page, because it is
     the newest text the system saw; nothing is kept only as a copy.
   - Writers store no copy of the input or the usage. Readers read both
     through `ledger_rows`: an answer with a fetch shows the input rebuilt
     from it, an answer with a call shows the call's numbers on the first
     answer naming the call and zeros on its siblings (how the writers
     stored them), and an answer with neither shows what it stored. Each
     derived column is a correlated lookup in the select list, not a join:
     a UNION ALL arm is flattened into the outer query only while its FROM
     is one table, and the admin ledger's skip scans and newest-first pages
     need it flattened (`tests/test_admin_jobs_read_plan.py`).
   - `tasks.answer_copies`, queued each cycle until a run clears nothing,
     empties the copies (`input_content`, the usage columns,
     `instructions`), one id range at a time after linking it, and only
     where `ledger_rows` reads the same without them: an input its fetch
     rebuilds byte for byte or an empty one, usage its call shows on it or
     zeros on a sibling or on an answer with no call. Anything else keeps
     its copy and blocks the drop. `ai_queries` already has per-table
     autovacuum at a 0.02 scale factor, so the rewritten row versions are
     reclaimed as it goes.
   - Next: the columns are dropped once proven empty, and the views stop
     falling back to them.
4. Call usage as one ledger that other tables point at instead of copying
   cost into themselves. See "The ledger of paid model calls" below.

A failed attempt is a call, not a verdict, so readers that count calls
(spend) stay on `ai_queries` until step 4.
`tests/test_verdicts_view.py` fails when a new reader restates which rows
are answers instead of reading the view.

How an answer is read is `core/verdict_reads.py`'s: the latest answer to a
check for a posting (`latest`, `latest_status`, `read_latest`), the latest
answer per key (`latest_per`, `latest_checks`, keyed on `jobs.id` where a url
key would sort text), what a closed answer shows a person (`closed_verdict`),
whether a check has been answered at all (`has_verdict`), and verified-open
(`verified_open`). What a posting is now is its latest answer, never "ever
rejected" or "ever passed": two readers that asked that disagreed with the
board about 313 active postings until 2026-10-10. The same test fails
when a module outside it writes a latest-answer shape (`ORDER BY id DESC`
over the view, or an `EXISTS` on one posting's answers); its allow-list
names each exception and why.

### The ledger of paid model calls

**One row per paid provider request, in `model_calls`, written only by
`api.model_calls.record`.** A batch item is a request: its identity is
`(provider_batch_id, custom_id)`, unique, so a replayed receipt adds nothing.
A live call has no provider identity and gets one row when its response
returns. The row carries purpose, model, transport, the five token counts,
`cost_usd` priced once at write time (architecture.md), duration, task, and
who pays: `user_id`, `managed_board_id`, or neither for the fleet. Nothing
else stores a token count or a cost for a call.

**Why.** On 2026-10-10 one call's numbers were written in up to five places,
each by its own code:

| Place | Grain | Written by | Read by |
|---|---|---|---|
| `ai_queries` usage columns | verdict row; tokens on the first row of a call, zeros on its siblings | `core.store.ai_result_row` | /admin/spend verdict diagnostics, board spend, the admin ledger |
| `api_usage` | one row per user or board request; one row per whole batch for the fleet | the three writers in `api.budget` | the weekly user budget, the fleet ceiling, /admin/spend ledger and calls, per-user spend |
| `ai_batches` token totals and `est_cost_usd` | one row per provider batch | `batch_event_hook` | fleet and task model screens |
| `batch_result_receipts.response.usage` | one row per batch item | `batch_results.checkpoint` | nothing reads it as money |
| `review_gate_outcomes.recorded_cost_usd`, `usage` | copied from `ai_queries` | nothing since 2026-10 (title screens write no rows); the table is dropped | nothing |
| `job_embeddings.input_tokens`, `cost_usd` | a packed request split per posting | `tasks.embeddings` | nothing |

The same verify call was a fleet row in `api_usage` with the batch's totals,
the same totals on `ai_batches`, a receipt, and a `closed` verdict holding
the tokens beside a `clearance` verdict holding zeros. Five copies is how
they came to disagree.

**What reconciles, measured 2026-10-10 against production.**

- For every provider batch submitted since 2026-09-13 that has receipts
  (7,466 of 7,541), the receipts' input and output tokens equal `ai_batches`
  exactly. The other 75 record no tokens: 70 are still open at the provider,
  one expired, and four are `completed` with 794 requests between them and
  neither tokens nor receipts, which is either unbilled work or calls nobody
  recorded. Receipts are the grain the ledger needs.
- `ai_queries` matches `ai_batches` token for token in 7,131 of 7,408
  batches that wrote verdicts. In the rest the batch is larger by the calls
  that wrote no verdict (invalid output, superseded, failed), $0.39 in all.
  No `(batch_id, url)` group holds more than one row with tokens, so a paid
  verdict row is exactly one call: 438,844 calls map to 931,033 verdict rows
  (up to five: closed, clearance and managed board customs from one verify
  answer).
- 2026-09-12 to 2026-10-10: `api_usage` $152.32; `ai_batches` completed in
  the window $141.40 plus live calls $10.99, $152.39. `ai_queries` holds
  $123.89 because mail, comp, job profiles, requirements, locations,
  embeddings and application drafts write no verdict. Filter work from
  2026-09-14 matches between `api_usage` and `ai_queries` to the row and the
  token (226,392 rows, $30.67).
- `review_gate_outcomes` differs from the `ai_queries` row it copied in 0 of
  552,317 rows. It is a pure copy.
- `ai_experiment_results` sums to $0.8685, the same as the 3,274 fleet
  `api_usage` rows for `experiment` (all on 2026-09-07); the 34 experiment
  batches carry no `est_cost_usd`.

**What does not reconcile, and is therefore not a backfill source.**

- `api_usage` before 2026-09-13 for user filter work. Batched requests were
  booked as `batched = false` at live prices: the week of 2026-08-31 has
  32,182 user rows at $21.60 against 32,128 batched verdict rows at $11.42
  with the same tokens. 5,926 user rows from 2026-08-24 have no price, and 87
  fleet `filter` rows (2026-08-26 to 2026-09-03, $1.04) book batches whose
  requests the user rows also booked. For that work and era `ai_queries` and
  `ai_batches` are right and `api_usage` is not.
- `job_embeddings` sums to 197.8M tokens and $2.69 against 127.0M tokens and
  $1.27 on the embedding batches. It is an allocation, nothing reads it, and
  its columns are dropped rather than carried.

**Who points at the ledger instead of copying it.**

- A verdict carries `model_call_id`. Siblings from one answer carry the same
  id, so "this verdict's call was paid on another row" is a join, not the
  zero-token guess `joint_call_rows` makes today. A verdict no call produced
  (`reverify-unchanged`, `verify-near-copy`, ingest, manual) has none, which
  says so. Its token and cost columns are then cleared and dropped.
- `ai_batches` keeps the batch lifecycle (status, requests, completed,
  `est_tokens` for chunking). Its totals become a sum over the batch's calls.
- `api_usage` is replaced, not pointed at: payer is a column of the call.
  The weekly user budget is `SUM(total_tokens)` over the person's calls on
  the owner key in seven days, and the fleet ceiling the same over calls with
  no user (managed boards included, as today).
- `ai_experiment_results` is not carried. Experiments run from
  `api.run_experiment` and keep their answers in files, and the table is
  dropped.

**Where a row is written.** Batch items in `batch_results.checkpoint`, the
one place every collected result passes through, from the receipt and the
batch: purpose and model from `ai_batches`, payer recorded on `ai_batches` at
submission by whoever submits, never inferred from a task payload. That
retires `charged_to_user` and the per-item `record_tokens` calls in
consumers: a consumer that wrote a verdict did not make the call, the
checkpoint saw it first, and a failed or superseded item is paid either way.
Live calls are written by the live writers of `api.budget`, which every live
caller already reaches with its payer, plus the admin manual check, which
today writes a verdict and no usage row at all.

**How pages read it.** /admin/spend's ledger, /admin/spend/calls and the
budget read `model_calls`. The verdict diagnostics (by check type, reach,
waste) and the review gate's cost read `model_calls.answers_with_usage`,
where an answer's usage is its call's on the first answer naming it, so
their numbers are the ones the copies gave and a call is summed once;
"calls" there still counts answers. It is one join to the call and one
aggregate for the first answer per call, not `ledger_rows`' per-row lookups,
which took 11 min 51 s against 6.6 s for the 30-day cuts on production on
2026-10-10: a sum over a window reads the join, a page of rows reads the
view.
`tests/test_ledger_rows_pointers.py` holds the page equal for the same
answers stored as copies and as pointers. Every page keeps the one-pass shape
(engineering-standards.md, `tests/test_spend_stats_single_pass.py`), and a
switched read ships with a test that holds the new query equal to the old
one on the same rows for the era where both are right.

**Board spend counts calls, not fetches.** `routers/analytics._SPEND_SQL`
reads `ledger_rows`, so a page fetch is a "call" with no price. Measured
2026-10-10, all time, joined to `jobs`: 2,800,724 "calls", 67.0 percent
priced. Without fetches: 1,889,467 and 99.4 percent; the remaining 0.6
percent are verdicts no call produced. On the ledger: 1,384,802 calls, 100
percent priced, the same dollars. A fetch costs bandwidth, not a model call,
and showing it as an unpriced call reads as a gap in pricing that does not
exist. `priced_coverage` moving from 67 to 100 is that correction, and the
change says so on the page in the same release.

**The sequence.** Each step ships alone and leaves every page showing what
it showed.

1. Expand: the table, `payer` on `ai_batches`, and `record` called from the
   checkpoint and the live writers. `api_usage` and the copies are still
   written. A test holds the ledger's rows for a collected batch equal to its
   receipts, and its cost to `ai_batches.est_cost_usd`.
2. Backfill, as a resumable task (`backfill_model_calls`) idempotent by
   predicate, one source per era. Every backfilled row names its source
   table and row (`source`, `source_id`), and a cost is copied as recorded,
   never repriced: repricing every receipt at 2026-10-10's rates came to
   $7.32 more than the batches recorded.
   - A paid batched verdict is one batch item, at the cost it recorded.
   - A receipt no verdict accounts for (failed, superseded, invalid, not
     yet consumed) is priced from its tokens, the only record of it. A batch
     of a purpose that writes no verdict is items only where its receipts
     price to what the batch recorded; otherwise (174 batches, most of them
     job profiles) it is one row at its recorded cost.
   - The rest of a batch with no receipts (about $53, mostly mail and
     requirements before 2026-09-09) is one row with its request count.
   - Live verdicts and the `api_usage` rows of purposes that write no
     verdict, before the first live call the ledger recorded itself. Filter
     and board work never comes from `api_usage`.
   - Payer: a batch's recorded payer, else the user or board its submitting
     task named, else the fleet for purposes only the fleet runs. A live
     verdict's payer is the user or board its `filter_name` names. Where
     nothing recorded one (14,589 custom answers from named filters before
     per-user filters, the admin's manual checks) the payer is NULL, not the
     fleet.
   - Previewed read-only on production on 2026-10-10: batch work $223.20
     against $223.56 on `ai_batches` (the gap is batches finished within the
     day and items repriced where nothing else priced them), live verdicts
     $30.88, `api_usage`-only calls $2.02.
   - A verdict's call is a join, not a copied id: a batched verdict's is
     `(batch_id, url) = (provider_batch_id, custom_id)`, a backfilled live
     verdict's is `source_id`. `tasks.answer_links` stores it as the
     verdict's `model_call_id`, with a live call its caller booked apart
     from its verdict, matched on model, tokens and the minute after only
     where each side has one candidate.
   - Receipts are deleted with their task, so this runs before they
     expire. The contract step deletes the task with the columns it reads.
3. Switch reads one page at a time, each with its equality test. Windows
   that reach before 2026-09-13 move down, because the old ledger
   priced that era's batched filter work at live rates; decided 2026-10-10,
   the ledger shows the true cost and the response's note says so. A
   difference a test cannot hold equal is measured and named in its PR.
4. Contract: stop the copies, then drop `api_usage`, the usage columns on
   `ai_queries` and `job_embeddings`, and the totals
   on `ai_batches`, each with the empty-then-drop sequence (migrations.md).
   `api_usage`, the `ai_batches` totals and the `job_embeddings` shares go
   first. `review_gate_outcomes` goes with the review gate tables. The usage
   columns on `ai_queries` wait for the verdict diagnostics of /admin/spend
   to read calls, and are cleared in the same rewrite that clears the page
   text copied onto answers, so the 9 GB table is rewritten once.

## What may be done unattended

A merge reaches six hosts on Renovate's cadence with nobody watching
([deployment.md](deployment.md)), so "the tests passed" has to be enough
before a change can land that way.

**May be taken by an agent, PR opened and merged on green CI:** every phase,
by Kanishk's standing instruction of 2026-09-10 ("merge it and keep going, you
don't have to keep pausing"). Green CI is the gate, and CI runs the full
suite, the type check and the import contract.

That instruction replaced an earlier rule here that phases 2 to 5 each needed
a person before merge. It does not replace the judgement the rule was
protecting. A phase still has to bring its own evidence, and the two below
still hold.

**Still stop and ask:** a change whose failure would be silent in production
and invisible in CI. Phase 2b and phase 3 are the named cases: one changes the
population the sweeps pay for, and the other decides what a source observation
means. Bring the parallel cutover's numbers first, then merge. Phase 2b's
product meanings are chosen (below), and so are phase 3's (2026-10-10, below).

Phase 2 decides who sees what. Its failure mode is two definitions silently
agreeing in the tests and disagreeing in production, which is the state the
codebase was already in while every test passed.

Phase 2 was first written as "one definition of board membership", on the
reading that `demote_closed`, `materialize_passing` and `visibility.FULL` were
three definitions of one thing. Opened, they are not. FULL admits an untouched
row only through its structural branch, which does not reference `user_jobs`
at all, so an untouched board row does not make anything visible and the two
writers cannot disagree with FULL about what a person sees. Visibility already
has one definition, computed into `board_visible` and never patched in place.

What those rows did instead was carry scope. `AI_ELIGIBLE_JOB` admitted any
job with a board row, and the re-verification sweep took its candidates from
`user_jobs`, so an untouched row was what kept a posting being paid for. That
was a second job the table was never named for; phase 2b moved it to
`user_job_working_set`.

Measured before assuming it was expensive: 3,709 board rows, 2,337 untouched,
and of the jobs eligible only through a board row, 861 are tracked by someone
and 214 are untouched. 214 is not a cost problem. The reason to separate the
two meanings is that one table answering two questions is how the next wrong
answer gets written, not that it is currently wasting money.

## Phase 4 was dropped

It was going to make a verdict's identity `(input_hash, recipe_id)` so that
changing the model did not invalidate one. Two things killed it.

**It contradicted a decision already taken deliberately.** `_check_filter`
says so in as many words: "Scoped to the model on purpose: a verdict from a
different model is not this model's verdict." The cost cliff that follows is
named there too. Switching to a better model and keeping the old model's
answers is not a saving, it is a stale corpus, and the phase was written
without reading the rationale that was already in the file.

**Reframed as staleness, it was measured and it is small.** The valuable half
of the idea was that a verdict should record the page it judged, so a changed
page invalidates it. Measured on 2026-09-10, as the share of latest verdicts
whose url has a newer page than the verdict:

| check | latest verdicts | judged on a page since replaced |
|---|---|---|
| closed | 66,022 | 170 (0%) |
| clearance | 66,016 | 2,532 (3%) |
| custom | 38,819 | 765 (1%) |

The clearance number was the largest because nothing ever re-checked
clearance, and that is fixed at its cause instead. Read that table with its
confound in view: pages are re-fetched mainly by the re-verification sweep, so
"the page changed" is partly a measure of how often we look.

What the idea was reaching for already exists here anyway.
`job_requirements` and `job_embeddings` carry `content_hash` and the
`ai_queries` row their answer was read from, and `tasks/rescrape.py`
generalises "re-scraped and unchanged, so re-stamp rather than re-pay" across
answer tables. Verdicts do not fit that helper, which updates one row per url
while `ai_queries` is append-only, but the pattern is there to extend the day
a measurement asks for it.

**A derived fact is one registration.** `tasks.DERIVATIONS` lists every
answer a model derives from data we already hold (pay, requirements, job
profiles, embeddings, locations), each a `tasks.derive.Derivation` naming its
table, its input (page text cut to `input_chars`, or not page text), its
recipe version, its model routing, its staleness rule (`select`) and its
store. `tasks.derive.sweep` is the one skeleton: switch, one pass in flight,
selection, the unchanged-page skip, batch submit and collect through
`tasks.runtime`, the page-currency check, parse, store. The worker schedules
and dispatches every entry from the list, so a new derivation touches neither.
A switched-off feature stays registered; its `switch` keeps the scheduler from
enqueuing it and a hand-started run from submitting, while paid batches are
still collected. Identity stays what phase 4's drop left it: the answer is
keyed by what it describes, and the recipe version re-derives only where the
table stores it (`job_profiles.classifier_version`). Pay is `job_comp`, keyed
by url like the other derived tables; a reader joins it on `j.url` (the
board's `visibility.FAST` already does, as `pay`), and `jobs` holds only what
listings say.
`jobs.near_copy_key` is not a registration: it costs nothing, is computed in
process before verify submits, and records the text verification read, so a
staleness rule recomputing it from a newer page would match a twin on text
its verdict never judged (2 of 3,000 sampled keys differed from the current
page on 2026-10-10).

Phase 2 still brings its numbers before it merges: it decides what gets paid
for.

Kanishk chose the three product meanings the storage cutover needed
(2026-10-10). They are the rules for every cutover step:

- **A legacy all-default row with no surviving provenance is working-set
  membership, not person state.** On 2026-10-09, 2,401 of 3,816 `user_jobs`
  rows had every person field at its default and `person_touched_at` NULL.
  Nothing a person wrote survives on them; an empty row is what
  `materialize_passing` has always inserted, and a person who acted on a row
  left a status, a note, a date or a hide behind. Kanishk's rule is "my jobs
  should be ones that I can see": such a row does not keep a posting on a
  board, `board_visible` does. Before the rows leave `user_jobs`, count how
  many would leave a person's visible board because no enabled filter admits
  them, and report it. The backfill already puts them in the working set, so
  converting them changes no sweep.
- **The daily digest announces postings that newly became visible to the
  person**: `board_visible` rows whose `computed_at` (when the posting joined
  the board, see [visibility.md](visibility.md)) is after the person's last
  digest. Not newly admitted working-set members: a working-set row is scope
  for paid sweeps, and a person cannot open a posting they cannot see.
- **Analytics report three labelled populations**: person state (postings
  the person acted on), working set (postings the filters picked) and visible
  (postings the person can see). The person-facing surface shows visible and
  acted on; the admin surface shows all three. The overloaded `user_jobs`
  count goes away. A response whose shape changes keeps its old field until
  the frontend reads the new one. The counts are spelled once,
  `api.board.populations.per_user_counts` and `person_state.PERSON_STATE`:
  `GET /user/stats` totals carry `visible` and `acted_on`, and
  `GET /admin/users` rows and source analytics `board_yield` carry
  `acted_on`, `working_set` and `visible` beside the old `tracked` and
  `board_rows`.

Phase 2b ran on 2026-10-10, one step a PR, each read checked on production
after the split backfill before it merged:

- Backfill: 1,369 legacy rows a person had touched got `person_touched_at`;
  1,293 all-default rows joined `user_job_working_set`.
- AI eligibility (#866) and the re-verification sweep (#869) moved to the
  working set; on production both selected the same postings as before (3,830
  and 192). The digest (#870) reads `board_visible`; analytics (#872) carry
  the three populations.
- `materialize_passing` stopped writing `user_jobs` (#905). Then the 2,416
  legacy all-default rows were deleted from `user_jobs`, every one already in
  the working set; 2,021 of them were postings the person could not see, and
  none granted visibility, so no board changed. `user_jobs` held 1,415 rows
  afterwards, all stamped.
- The split backfill, its admission and the working-set shadow report were
  deleted.

So `user_jobs` is what a person did, and nothing writes an empty row there.
A reader that wants "postings the filters picked" reads `user_job_working_set`.

## Phase 3: what a source observation means

Kanishk decided the product semantics on 2026-10-10. Each is here with its
reason, because the tables encode them.

1. **A job is its url.** `jobs.url` stays the canonical identity, because
   every row, verdict and link already keys on it and `ats.canonicalize`
   exists to make one spelling per posting. Each source's listing of a
   posting is its own observation pointing at the job, so one url carried by
   a company board and an aggregator is one job with two histories.
2. **A posting listed only by switched-off sources is not available.** This
   keeps `catalog.retire_switched_off`: retired unless a switched-on source
   lists it and would admit it. A switched-off source is never pulled, so
   nothing it says can be refreshed.
3. **Still listed but no longer admitted by the title pattern is `filtered`,
   never a closure.** `retire_unlisted` cannot tell the two apart today, and
   4,554 of 6,306 active company-board rows on 2026-09-04 were pattern misses.
   The observation records whether the pattern matched; whether enforcement
   is on is applied when availability is read, so flipping
   `source_title_patterns_enabled` does not rewrite history.
4. **Aggregator absence never means closed.** It is recorded as `not_listed`,
   "not listed since" its `at`, and leaves the posting available. Only the
   closed check says closed. A complete pull of a board in
   `boards.AUTHORITATIVE` records absence as `unlisted`, and whether that
   retires the job stays today's rule (it does). A pull that cannot show it
   saw everything (`PartialPull`, or an empty pull) records no absence.
5. **Changes only, kept forever.** A row is written when a source says
   something other than its latest row for that job, never per pull, and
   nothing prunes the table. At 74,000 postings an hour a row per pull would
   be millions a day saying nothing changed.

One case the decisions did not name: a feed whose own record carries an
inactive flag (the JSON listings feed). Today that writes `jobs.active =
false`. It is recorded as `unlisted`, because the source itself says the
posting is no longer listed, which is not the same as being left out of an
aggregator's pull.

### The tables

`source_observations` is the fact: `(job_id, source, kind, run_id, at)`,
kinds `appeared`, `reappeared`, `filtered`, `unlisted`, `not_listed`, written
only by `catalog.observe` after each pull. `run_id` is the `ingest_source`
task whose pull said so; tasks are never pruned, and the task's progress
carries the pull's counts and whether it was `complete`, so the run is
already a row and does not get a second table.

`job_listing_events` was the candidate to extend and is not this. It logs
flips of `jobs.active`, attributed to the source that caused the flip: a
second source listing an already active posting writes nothing there, and its
false rows do not say whether the board dropped the posting, the pattern
stopped admitting it, or the source was switched off. Adding kinds to it
would put two kinds of record in one table, which is what phase 8 is
removing elsewhere. It stays as it is, and keeps serving the re-verification
edge, until that reader moves to the projection.

`jobs.relisted_at`, the named example of overwritten state, was already
removed by `b64dadabb22d` when `job_listing_events` arrived.

**Availability is a projection**: a job is available when some switched-on
source's latest observation of it is `appeared`, `reappeared` or
`not_listed`, or `filtered` while enforcement is off. A job with
observations and none of those is unavailable. A job no source has observed
is `cannot tell`, never a default. Two more rules, decided 2026-10-10:

- `sheet_import` is a switched-off source. It has no `sources` row and
  nothing pulls it, so its rows are unavailable unless a switched-on source
  lists them. A person who touched one keeps it through their own state.
- An administrator's correction (`catalog.set_active`, the admin PATCH) is
  an observation under the source `admin`, which no `sources` row has. While
  it is the job's latest observation it decides; a source saying something
  new about the job ends it. `jobs.active` reopened on the next pull whatever
  the correction said; this holds it until a source's answer changes.

Enforcement is read from `app_config` inside the SQL, so `AVAILABLE` takes no
parameter and drops into a query of either placeholder style.

**History is not invented.** The first pull of each source after the dual
write lands records `appeared` for everything it lists: that row's `at` is
when the log began watching, not when the posting was first listed.
`job_listing_events` keeps the earlier history and is not copied. Its true
rows are certain returns; its false rows cannot be split into unlisted,
filtered or switched off, and count as `cannot tell`.

### Cutover

Dual write first, then a shadow comparison of the projection against
`jobs.active` over at least one full ingest cycle, then readers move where
the meanings match. `jobs.active` is feed state that every sweep and
selection gates on; a reader that wants "a source says this is open" moves,
and a reader that wants the feed's own flag stays. Measured differences are
explained before any reader moves.

The projection has one definition, `catalog.AVAILABLE`. The hourly
`retire_switched_off` task runs the shadow right after the legacy rule, so
`jobs.active` is as current as it gets, and leaves it on its own row:
`progress->'availability_shadow'` holds one cell per (legacy, projected) with
its count, the ten owning sources holding most of it and a few job ids. Read
the cells across at least one full ingest cycle after the dual write is
deployed (a source on a 24-hour interval has not been observed before then,
so its rows read `projected=None` until it has):

```sql
SELECT id, finished_at, progress->'availability_shadow'
FROM tasks WHERE kind = 'retire_switched_off' AND status = 'done'
ORDER BY id DESC LIMIT 24;
```

Disagreements expected by design, each to be confirmed by count rather than
assumed: `projected=None` for uploads and for every switched-off source's
rows (nothing pulls them, so nothing observes them); `legacy=False
projected=True` where an aggregator still lists a posting its owning board
dropped: `retire_unlisted` clears the flag on every pull of the board and the
aggregator's next upsert sets it again, so the legacy value flips with
whichever pulled last while the projection holds still.

### Readers move before every source is observed

Decided 2026-10-10: readers take availability from `catalog.IS_AVAILABLE`,
which is `AVAILABLE` where it can tell and `jobs.active` where it cannot. The
fallback shrinks as each source's first pull after the dual write lands. On
2026-10-10, against `jobs.active` over the whole catalog, it differed on
4,472 rows (all `sheet_import`, now unavailable) and 124 the other way (a
JSON feed's inactive record against another source that lists the posting).
A later change drops the fallback once the shadow's `projected=None` cells
for active rows hold only explained classes.

**Availability is stored, in `jobs.available`.** Computed per row,
`AVAILABLE` cost every reader that moved: the AI-eligible count went from
2.0 s to 6.5 s and a board recompute from 27 s to 38 s, at 992 recomputes a
day, about three hours of database time a day (production, 2026-10-10).
`IS_AVAILABLE` now reads `COALESCE(j.available, j.active)`, which costs
what `j.active` did. Measured on production after the first reconcile
(2026-10-10, 21:00 UTC): user 1's board recompute ran in 25.7 and 28.0 s
against 26.0 and 26.8 s on the flag, and the preset eligible count in 1.9 s
either way. `AVAILABLE` stays the one definition, and three writers
store it:

- `catalog.observe` refreshes the rows its observations changed, after they
  commit, in batches of 500.
- `catalog.set_active` refreshes its row in its own transaction.
- `catalog.reconcile_available`, in the hourly `retire_switched_off` task,
  writes every row where the stored value differs. A source switched on or
  off and the title pattern setting change availability without an
  observation, and land within the hour, as `retire_switched_off`'s own rule
  does.

Each writer locks first, in url order, and evaluates in a second statement.
Under READ COMMITTED that statement's snapshot is taken after the locks are
held, so it sees every observation committed before them, and a writer that
commits one later refreshes again behind it. Measured cost: about 8 to 10 s
an hour to evaluate the whole catalog, 0.3 s per 500 refreshed rows, and
writes only where the value changes. On deploy the first reconcile writes
about 161,000 rows, and later pulls write the rest as they are observed.

Every reader of `jobs.active`, and where it stands.
`tests/test_availability_readers.py` fails on a new `j.active` outside the
files listed there:

| Reader | Meaning | Stands |
|---|---|---|
| `core/catalog.py`: `retire_unlisted`, `retire_switched_off`, the upsert's change test, `correct_posting`, `availability_shadow` | feed state, written and compared | stays |
| `api/routers/analytics.py` source inventory | the owning feed's own flag, per source | stays |
| `tasks/comp.py`, `tasks/content.py`, `tasks/verify.py` (three sweeps), `tasks/locations.py`, `tasks/application.py` (three), `api/experiments.py` | which postings get work | moved |
| `api/board/eligibility.py` `STRUCTURAL` (board recompute, materialize, managed board runs), `tasks/board.py` `demote_closed` | which postings a board may show | moved |
| `api/routers/filters.py` preset coverage gates | how many postings a preset would show | moved, once availability was stored |
| `api/routers/job_board.py`, `job_detail.py`, `public_job_lists.py` (`active` in the response), `api/board/column_filters.py` ("Listed by source"), `api/posting_path.py` | what a person is told | moved |

### Contract: retiring jobs.active

Planned 2026-10-10. Each step is its own release, and each starts only after
the one before it is running on every worker.

1. **Readers read the column alone.** Once the shadow's `projected=None` cells
   for active rows hold only explained classes, `IS_AVAILABLE` becomes
   `j.available`, with NULL read as not available, and the board recompute is
   measured against its 27 s baseline. Two readers still read feed state, and
   each moves to observations: the source inventory in
   `api/routers/analytics.py` counts each source's latest observations, and
   the re-listing branch in `tasks/verify.py` keys on a `reappeared`
   observation newer than the last closed check, not on `job_listing_events`.
   `posting_path` stops reading `job_listing_events` too.
2. **Nothing writes it.** `upsert_postings` stops setting `active` and drops it
   from its change test. `retire_unlisted` and `retire_switched_off` go:
   observe records `unlisted`, and the reconcile covers switches. The
   `job_listing_events` inserts go with them. The shadow goes, because there
   is nothing left to compare. `set_active` writes only its observation.
3. **Prove nothing reads it.** The guard in `tests/test_availability_readers.py`
   widens to every spelling of the column (`active` selected from `jobs`,
   `jobs.active`, the ORM attribute) with an empty allow-list. Production
   `pg_stat_statements` is read for statements naming `jobs.active` across a
   full day after step 2 is on every worker, and the count must be zero.
4. **Freeze it, do not drop it.** Data is never deleted ("Always retain all
   data" in [engineering-standards.md](engineering-standards.md)), so the
   column keeps the last value each feed wrote, and `job_listing_events`
   keeps the history from before `source_observations` began. Both stop
   changing after step 2. Dropping either would delete data, which is
   Kanishk's decision, not part of this plan.

**Never in a loop:** any write to the production database, and any migration
that can refuse to apply ([migrations.md](migrations.md)). Neither of these is
covered by the standing instruction above, because neither is gated by CI.

## Phase 5 found its reason, in phase 6

The layering between the services and the handlers cannot be enforced while
`api.tasks` is a child of `api`. A `forbidden` contract skips a target that
sits inside its source, so `source_modules = ["api"]` with
`forbidden_modules = ["api.tasks"]` passes whatever the code does. That was
not reasoned, it was verified: a violation was added and the contract stayed
green. Narrowing the source to a single module catches it, so enforcing the
rule as things stand means enumerating forty-odd modules and leaving a hole
the day somebody adds the forty-first.

Moving `api/tasks/` to `src/tasks/`, beside `api` and `core`, makes the
contract one line and expresses what is already true: the handlers are not
part of the API, they are work the worker runs using it.

That is the first argument for moving a file in this plan that is about
something other than where a reader looks for it, and the move is done.

Turning the contract on then named six imports, and five were one smell wearing
five hats: every one reached past a handler for a SHAPE or a CONSTANT, never
for behaviour. `SHAPES` twice, a drafting default, a verdict model, an event
kinds list.

**Taken 2026-09-11, and the contract is down to four ignored imports.** What
each task declares is `core/shapes.py`: the purpose, the sanctioned models, the
output cap, the per-cycle size and the measured evidence, with the constants
each shape reads moved alongside it and imported back by the handler. So
`api.budget` prices a fleet cycle and `api.routers.task_models` configures one
without importing the code that runs them. `configured_model` is
`api/task_config.py` and `load_config` is `api/budget.py`; both read the
database on behalf of the services from inside the task runtime. What a draft
is written from is `api/apply/drafting.py`, so the two routers that draft one
live no longer borrow it from the sweep that batches them.

Two things this section said were wrong when the code was opened, and both were
load-bearing for the claim that `SHAPES` could not move.

**A registry is not an option, and that part was right.** `SHAPES` is built by
reading each declaration, and a registry the handlers wrote into on import
would be empty for any caller that had not imported `tasks` - which is exactly
the caller this move exists for. The failure would be a fleet cost silently
computed over no tasks.

**"mail_classify builds its two shapes in a function" is not an obstacle.** A
factory moves down as well as a literal does; `_classify_task` is a pure
`TaskShape` constructor and it now lives in `core/shapes.py` with the two
shapes it builds. **"application reads its purpose from another module" was
backwards.** The purpose was a bare string in `api/apply/writes.py`, which now
reads it off the shape instead of spelling it a second time.

The shapes went to `core/shapes.py` rather than beside `TaskShape` in
`core/routing.py`, which is what this document asked for. `core/routing.py` is
the resolver and is already 494 lines; adding 420 lines of declarations to it
would have made it do two jobs in the same phase whose point is that no module
does four.

## Phases 6 and 7

Added on Kanishk's instruction, to be taken after the move lands.

**6, the long files.** `tasks/runtime.py` was the clearest case rather than the
biggest: 775 lines holding queue primitives, batch orchestration, config
constants and model routing, imported by 24 modules. **Split 2026-09-11.** It
is now `tasks/runtime/`, three modules by job - `limits` is how much runs at
once, `lifecycle` is the claim and the progress and the end of a task, and
`batching` is the provider batch. The fourth job left the package entirely,
because it was never the runtime's: `configured_model` and `load_config` read
the database on behalf of the services and are now `api/task_config.py` and
`api/budget.py`.

The package re-exports the three, because that is what the importers want: a
handler takes a progress call, a chunk size and a batch submission in one
import and does not care which of the three it came from. Nothing outside the
package changed its import line except for the four names that moved out of it.

One expectation this document had did not survive. Splitting the runtime does
NOT let the layering be stated as layers. `api.worker` imports `tasks` for
HANDLERS whatever else it does, so it needs an exception regardless, and the
one it takes for the runtime is the same fact written twice rather than a
second problem. Only a worker that stopped being an `api` module would drop
those two lines, and that would not change a single import.

`admin.py` and `mail.py` were bigger and simpler: long because nothing ever
split them, not because anything was tangled. Both are taken. `mail.py` is
`routers/mail/`, four surfaces and the helpers two of them share; `admin.py`
is `routers/admin/`, ten subjects and one shared module.

**7, the schema is the contract.** Measured 2026-09-10: of 190 operations,
**173 return an undeclared object** and 11 declare a shape. So `openapi.json`
is a list of routes, not a contract. It cannot be dropped into a frontend and
generate anything, which is the whole reason it is generated and committed.

That is why the frontend writes those types by hand, and why they drift. One
had a capture field typed as a string when the extension sends an object, and
the page crashed on 2026-09-10 when a report with fields was opened. Nothing
could have caught it: there was no declared shape to disagree with.

The goal is therefore not internal tidiness. It is that a person can point a
generator at `openapi.json` and get types that work.

Reads returning bare dicts is the same problem seen from inside: renaming a
column is a grep across 678 call sites, and a typo is found at runtime.

**Failures are part of the contract and are not declared at all.** The schema
carries 200, 201, 202 and the 422 FastAPI adds for validation, and nothing
else. There are 164 `raise HTTPException` sites and at least three shapes
among them: 69 use `detail={"code": ..., "message": ...}`, 14 pass a bare
string, one passes an f-string. A client cannot know what a failure looks
like, so it guesses, and the guess is per client.

Declaring the error shape is the same job as declaring the success shape and
belongs in this phase. One convention, `{code, message}`, since that is
already the majority and the frontend already reads `detail.code`.

The primitive is `db.query_as(Shape, sql, params)` and its one-row sibling.
The SQL is unchanged; only what comes back has a name. A column the shape does
not declare raises there and then, which is the point: a SELECT and its shape
drift apart in one commit and are caught in the next test run rather than in a
bug report about a missing field.

Adopt it where a row is READ, not where one becomes JSON for a task payload.
`tasks/filters.py` puts candidate rows into `payload["jobs"]`, so typing that
one buys a conversion at the boundary and nothing else. An HTTP response is
not such a boundary: FastAPI serialises a declared model, and declaring it is
most of the value, because the shape reaches openapi.json and the frontend
stops writing those types by hand.

That is also the first place the API contract has been spent. `GET
/user/apply/reports` returned an undeclared dict, so the frontend's type for
it was written by reading the query, and it had drifted: a capture field typed
as a string was an object, and the page crashed on 2026-09-10. The wire format
did not change, only the declaration, which is the cheap half of the
permission: additive, no consumer breaks, and the hand-written type can be
generated instead. The fix is NOT an ORM:
the hot paths are hand-tuned SQL carrying measured query plans
(`visibility.FULL` records 320ms to 28ms), and an ORM would hide exactly what
has to stay readable, while inviting the N+1 shape this codebase has already
paid to remove. Keep the SQL, map the rows into dataclasses at the boundary,
one domain at a time. `pyright` already runs clean, so the types would be
enforced rather than decorative.

## Logging: what was wrong, and what turned out not to be

Measured 2026-09-10, closed 2026-09-11.

**Seven logger names where two would do.** True, and now one rule: every
module calls `logging.getLogger(__name__)`. The two conventions,
`jobtracker_worker` (24 modules) and `jobtracker_api` (11), were not wrong so
much as coarse: they said which half of the system logged, which is what the
module path says anyway, and more precisely. Five modules used neither, one of
them spelled `job_tracker`, and nothing enforced any of it because nothing
had to: telemetry attaches its handler to the ROOT logger, so a module needs
no registration to be shipped. The cost was only that filtering by source in a
log viewer did not work the way the names suggested. `__name__` is the Python
default, needs no decision when a module is added, and makes the record say
`tasks.verify` rather than `jobtracker_worker`.

**Tracebacks dropped.** Six `logger.error` against eighteen
`logger.exception`. Read rather than rewritten in bulk, as this document
said to: three were inside an `except` and interpolated the exception into the
message, which keeps the sentence and throws away the stack. Those are
`logger.exception` now. Three are deliberate and stay: two in `tasks/health.py`
report a refusal with no exception in flight, and one in
`core/fetching/listings.py` fires after a retry loop falls through, where
there is nothing to trace.

**"Unattended code that says nothing when it goes wrong" was wrong.** The row
named four task handlers that hold no logger: `experiments` (505 lines),
`embeddings` (232), `batch_policy` (64), `uploads` (61). Opened, none of them
is silent.

`api/worker.py` logs every task starting, done, parked, requeued on a
transient error, and `logger.exception` on failure, so a handler that logs
nothing still reports its failure with a traceback and the task id. None of
the four contains a single `except`, so nothing is swallowed on the way.
Three of the four call `set_progress`, which writes the outcome to the task
row where the fleet page reads it. The fourth, `uploads`, raises on every
failure path and writes `jobs.extraction_status`.

So the gap was reasoning from the absence of a logger to the absence of a
record, and the record is somewhere else. What a handler DID is in
`tasks.progress`; what went wrong is in the worker's log. Adding a logger to
each would have added lines, not information.

`api/mail/pipeline.py` (588 lines) is the one place the argument does not
reach, because it is a derivation rather than a task and no worker wraps it.
It has no `except` either, so a failure propagates to whoever called it. Left
alone until something actually goes unexplained.

## The gaps list

Everything measured and not yet closed, with the number that makes it a gap
rather than an opinion. A row leaves this list when it is fixed or when a
measurement says it was never worth fixing, and either way it says which.

**The schema is not a contract.** Was 173 of 190 operations returning an
undeclared object on 2026-09-10, then 143, then 54. **2 of 190 on
2026-09-11**, and neither returns a body a shape could describe: `GET
/v1/openapi` serves the schema and `GET /v1/user/resumes/{id}/pdf` serves a
file. The declaration half of phase 7 is done.

Held there by `tests/test_openapi_current.py`: a route added without a return
annotation fails, and the one exception, the PDF, is named with its reason and
asserted to still exist and still have no model. An exception nobody revisits
silently excuses whatever takes that path next. `GET /v1/openapi` is not an
exception. It returns the OpenAPI document, which has no narrower pydantic
shape worth writing, so `dict[str, Any]` is its shape rather than a gap in
one.

**A failure is in the contract now, but it is not one shape.** The schema
declares 400, 401, 403, 404 and 409 on 188 of 190 operations, so a client can
read what a refusal looks like. What it cannot read is which of two spellings
arrives: of 164 `raise HTTPException` sites, 109 send `detail={"code",
"message"}` and 19 send a bare string or an f-string. **All 19 are in
`routers/mail/`**, and converting them turns `detail` from a string into an
object for a frontend in another repository, so it is a coordinated change
rather than a tidy-up. Phase 7.

Both consumers were read before that was written down, and both already
prefer the object: `client.ts` takes `detail.code` and falls back to the
string, and the extension reads `res.json?.detail?.code || res.status`. So
converting upgrades them from `HTTP_404` to a real code rather than breaking
them. It is still a change to another repository's input, which is why it
waits for a person.

WHICH statuses are declared is closed. They are named sets by reason rather
than a dict retyped per site: `REFUSALS` (400, 401, 403, 404, 409) on every
router, `PROVIDER_REFUSALS` (502) where something outside this application has
to answer, `AI_REFUSALS` (402 plus that) where a model is asked,
`SIZE_REFUSALS` (413), `UNAVAILABLE_REFUSALS` (503). Splitting 502 out of the
AI set was not tidiness: three invite routes and a re-check can return a 502
and cannot return a 402, and a route should declare what it can actually
return.

`tests/test_refusals_declared.py` holds both directions. A status raised in
`src` and declared nowhere fails, and so does a named set declaring a status
nothing raises. 500 is excluded on purpose: it is a bug rather than a refusal,
and declaring one invites a client to handle it as an outcome.

**Reads are untyped, and the goal is all of them.** Was 563 db call sites
returning bare dicts. **521 on 2026-09-11, of which 153 carry a shape.**
`db.query_as` is the primitive and adoption is per domain, on Kanishk's
instruction of 2026-09-11 that every read should carry a shape.

Two things a generated client makes visible that this row does not.

**Two routers may not name two models the same thing.** FastAPI does not
refuse it; it mangles the name to `api__routers__source_admin__Source` and the
type a generator emits is unusable. There were five such pairs, three of them
predating phase 7. `tests/test_openapi_current.py` now fails on any mangled
name, so the rule is enforced rather than remembered.

Two things make it work that are worth knowing before starting a domain.

**A `SELECT *` cannot be typed until it names its columns.** Was 24, then 22,
then 19, then 12. **9 on 2026-09-11**: five in `tasks/`, three in
`routers/admin/`, and the one left in `core/store.py`. Naming
them is a good change on its own: a star select and the shape that reads it
drift silently, which is the same defect one level down, and on a wide table it
fetches a page of text to throw away. Three of `core/store.py`'s four went with
the dead readers named below rather than being named; typing a read nobody
performs is the cheapest kind of nothing.

**Declaring a shape can move the wire, quietly.** A dict omits a key it has
no value for; a model emits the key as null. `PATCH /user/jobs/{id}` returned
`autofilled: {}` when it filled nothing, and declaring the shape turned that
into `{"status": null, "date_applied": null}`. The existing tests caught it.
Where the old dict omitted keys, set `response_model_exclude_none=True` on the
route and say so beside the model. Declaring must not change the payload; that
is the whole reason it is safe to do everywhere.

Four ways it moves that are not obvious, each found the hard way.

**A `Decimal` field is served as a JSON string.** psycopg returns a `numeric`
column as a `Decimal`, and while a route returned a bare dict, FastAPI's
encoder turned each one into a number. Declaring the field as `Decimal` hands
serialisation to pydantic, which writes `"100.5"`. The schema then says
`type: string`, agreeing with neither the old wire nor the client, and nothing
fails: the board just renders a quoted number. Declare money as `float`.
`tests/test_openapi_current.py` walks every response model and fails on a
`Decimal`.

**A key that is sometimes absent cannot be typed.** `signals_for` omitted a
signal that could not clear its sample floor, and a declared optional field
emits null instead. A `@model_serializer` that drops the nulls keeps the wire
exact and ERASES the schema, because pydantic derives the serialisation schema
from the serialiser's return type, so the model becomes `{type: object,
additionalProperties: true}`. That is the useless generated type this phase
exists to stop producing, so the null wins and the consumer is checked instead.

The bill for that arrives when a SECOND surface serialises the same model and
its route can exclude nulls. `ResolveChoice` is built once and served by the
queue, which excludes them, and by the candidate picker, which cannot: the
rest of the picker's payload is full of real nulls it has always sent. So one
omits `reason` and the other sends it as null, saying the same thing two ways,
and the test that holds the two surfaces to one verb set compares what a
choice MEANS rather than which spelling it arrived in.

**A timestamp gains `Z`.** `2026-09-11T05:10:00.546051+00:00` becomes
`2026-09-11T05:10:00.546051Z`, the same instant, because pydantic serialises a
datetime rather than calling `.isoformat()`. Measured against `POST
/user/views`, which has sent it that way since it was declared.

**A sum is a `Decimal` too.** Postgres returns `numeric` for a sum over a
bigint column, so a token count arrives as one. Those declare `int`, which is
what they already were on the wire; the rule above is only about money.

**The completion check for this phase is a generator, not a count.** Running
`npx openapi-typescript openapi.json` and reading the output is what found the
`Decimal` bug, an hour after it merged in `routers/experiments.py` and minutes
before it would have merged again on the board. Nothing else would have: the
tests passed, pyright passed, the schema was self-consistent and wrong.

**A response model must be defined ABOVE the route that returns it.** This
module uses `from __future__ import annotations`, so a return annotation is a
string and FastAPI resolves it when the decorator runs. A model defined later
in the file raises `PydanticUserError: ... is not fully defined`, at import,
with a message that does not say the cause. Found the hard way on
`routers/apply.py`.

**The service boundary has only the task runner's two inherent imports.** Nine
imports crossed it on 2026-09-11. The contract in `pyproject.toml` is enforced
in CI and now carries only these two.

`api.worker -> tasks` and `api.worker -> tasks.runtime` are correct and are not
debt. The worker IS the task runner: it loads HANDLERS to dispatch them and
reads the runtime for the same reason. Two lines for one fact, and the fact
goes away only if the worker stops being an `api` module. Nothing in the code
would change if it did, so it has not been done for a line in a config file.

The location edge is closed. `api/locations.py` owns the place inputs,
normalisation and write, including the rule that a model result cannot replace
an administrator's correction. The admin route and classification task both
call that service. `tasks/locations.py` owns only candidate selection, request
construction, batch lifecycle and result consumption.

The experiment edge is closed. Request construction, arm validation, sampling
and scoring live in `api/experiments.py`, which `api.run_experiment` calls.
The extraction answer schemas and instructions that experiments also need
live beside their vocabularies in `core`, rather than making the service
reach through the handlers.

The four drafting helpers this list used to name are gone.
`api/apply/drafting.py` holds `resume_text`, `writing_style`, `instructions`,
`question_input` and the `Draft` shape, and both routers read them there.

~~**Unattended code that says nothing when it goes wrong.**~~ **Measured and
dropped 2026-09-11**: the worker logs every task's start, outcome and failure,
none of the four handlers swallows an exception, and three of the four write
their outcome to the task row. See the logging section above.

~~**Seven logger names where two would do.**~~ **Closed 2026-09-11**: every
module is `getLogger(__name__)`.

~~**Tracebacks dropped on purpose or by accident.**~~ **Closed 2026-09-11**:
read, three were accidental and are `logger.exception`, three are deliberate.

**Test coverage, measured for the first time on 2026-09-11: 87%.** 11,800
statements, 1,528 missed, across 1,656 tests. Better than "never measured"
usually means, and the shape of the miss is the useful part rather than the
total:

| | | |
|---|---|---|
| `core/store.py` | 59%, 50 of 123 statements | now 98%, 1 of 50 |
| `tasks/verify.py` | 70%, 58 of 195 | now 94%, 11 of 191 |
| `tasks/ingest.py` | 81% | |
| `tasks/filters.py` | 82%, 42 of 228 | |

The gap was concentrated in the sweeps and the store, which is exactly where
this session found its two live defects: a closed verdict that could never be
revisited, and a clearance verdict that was written once and never again.
Both lived in `tasks/verify.py`. Neither was caught by a test, and the
coverage number said why: that file was the least covered substantial module
in the repository after the store.

Both are now closed, and what closed them is worth knowing before taking
`ingest.py` or `filters.py`. **The store's miss was not untested code, it was
dead code.** Thirty-eight of its fifty missed statements were the verdict
caches and the predicate helpers the sheet-era pipeline called; their last
caller left with `core/pittcsc_simplify.py` in #394, and the store kept its
half of the interface for five days. The whole of `prefetch()` was deliberately
unhooked in #117, which says so in its own message. A test over any of it would
have measured nothing and hidden the fact that it was unreachable, so it was
deleted instead: 123 statements to 50, and the one still missed is a guard the
only caller already makes.

`tasks/verify.py` was the other shape: live code with the splitter untested.
`handle_reverify_open` decides whether a posting is ever looked at again and
had no test at all, while the query for its re-listed branch was COPIED into
`test_ingest.py` rather than called, so the copy could pass while the handler
drifted. `tests/test_reverify_sweep.py` now drives the handler itself.

`make coverage` prints it. Nothing gates on a threshold yet, and adding one
before the remaining sweeps are covered would only ratchet in what is already
there.

**What is left uncovered in `tasks/verify.py` is uncovered on purpose**, and
the list is short enough to state: the `parent_id` progress calls, the every
fifth posting progress call, the `LookupError` for a missing server key, the
`if not pending: break` that `AdaptiveLimiter`'s floor of one makes
unreachable, `_newer_evidence`'s early return for a result with no batch id
(the query it skips returns the same answer), and `verify_new`'s
`unknown_request` receipt, whose twin in the reverify path is pinned. None of
them decides anything a person or an invoice can see.

**The named long, multi-job files are split.** `routers/resolve.py` is now a
160-line ordered router over contracts, choice policy, read models, commands
and queue construction. `health.py` is an 85-line aggregator. `jobs.py` is a
22-line ordered router over board reads, person-state commands, detail,
explanation, uploads, reports and task status. File length alone is not a new
phase: a further split needs evidence that a module owns multiple behaviors.

`orm.py` was the first taken, and it is the easy shape of this problem: 51
table definitions with no logic between them, so the split is a partition and
the only risk is that a table stops being registered. It is now `api/orm/`,
six modules named for what the tables are for, and `__init__.py` imports all
six so one metadata still carries all 51.

`mail.py` was the second, and it is the other shape: a router splits along
what its handlers do, which is a reading before it is a move. The reading gave
four surfaces - the administrator's debug view over everybody's mail, a
person's applications, a person's own messages and conversations, and the
queue of proposals and actions the mail produces - plus one module for what
the administrator and the owner both do, because correcting a classification
and listing what a message could belong to are the same job over different
mailboxes.

A router split costs one thing the table split did not: **registration order
is part of what a router means**. FastAPI matches in that order, so a literal
path registered after the parameterised one that would swallow it is a live
defect, and `openapi.json` is keyed in it too. Grouping by subject therefore
moves routes relative to each other. Four operations moved here, the
`/user/suggestions` and `/user/actions` pair, which no longer sit between two
runs of mail routes. No path overlaps another, so nothing changed about what
matches what, and the generated schema parses equal to the committed one
document for document. The committed file carries the new key order, because
CI regenerates it and would otherwise push the reordering back as a commit
nobody wrote.

`routers/admin.py` was the third, 2,131 lines and 46 routes, and the reading
found ten subjects plus a `shared.py`: the preset library, the boards, the
people, the fleet, the catalog, a manual re-check, data health, the tunables,
the verdict ledger, the extension recipes. Two pairs that looked like separate
groups were not. A request for a board to be added is about the boards, and a
report about a posting is about that posting, so neither became a module of
its own, and the second sits beside the `close_posting` its drawer calls.
`require_admin` has one definition and the package re-exports it, because ten
routers outside the package import it from there.

It paid the registration-order cost above, and larger than `mail.py` did: the
subjects interleave, so 30 of the 162 path entries move. Nothing gains, loses
or alters an operation, the spec parses equal document for document, and the
one literal-before-parameter ordering this surface depends on,
`/queries/options` before `/queries/{query_id}`, is preserved inside the
module that holds both.

`tasks/runtime.py` is the middle shape: its four jobs were named in the file
before anyone split it, so the reading was already done, but two of the four
turned out not to belong to the runtime at all. The lesson worth carrying to
the routers is that a long module's last job is often somebody else's, and the
split is the moment that shows.

**`user_jobs` answers two questions.** What a person keeps, and what the
sweeps carry. Phase 2b: moving the sweeps' scope changes what gets paid for,
so each read moves with a cutover comparison.

## Taking phase 7 in the order that pays

173 operations is not a list to work alphabetically. The frontend calls about
forty of them, and those are where an undeclared shape actually costs
something, because that is where a hand-written type drifts from the query it
was read off. `lib/job-tracker/client.ts` in the frontend repository is the
list; the ones it calls most are `/user/jobs`, `/user/jobs/options`,
`/user/settings`, `/user/profile`, `/user/filters`, `/user/sources`,
`/user/stats`, `/user/usage`, `/user/funnel` and `/user/pipeline/summary`.

The admin surface is most of the remaining count and almost none of the
remaining value. It is worth declaring eventually, and last.

## Working in a stack

`git-spice` is set up, trunk `main`. It restacks a branch when its base moves,
which is the whole reason to use it here: a phase often has a follow-up that
should not wait for the first to merge.

Two things it will not do for you. `git-spice branch restack` rebases onto the
LOCAL base branch, so `git fetch` and move `main` first or it rebases onto a
stale one. And a pull request whose base branch is deleted on merge is CLOSED
by GitHub, not retargeted: retarget it at `main` BEFORE merging its base, or
open a new one afterwards.

A stack does not make the tests faster. Every pull request runs the full suite
either way; CI takes two to three minutes with the sharding it already has.
What the stack buys is not waiting.

## Revising this document

This plan was written from a reading of the codebase, and a phase that opens
the code may find the reading wrong. When it does, change the document in the
same pull request as the work, and say in the commit what the evidence was.
A phase that turns out to be unnecessary is a finding; record that it was
dropped and why, rather than deleting the row.

What may not change without asking: the API contract stays the invariant, and
a phase marked as needing a person keeps needing one.

## Parallel cutover

A phase that changes how something is computed builds the new answer beside
the old one and compares them on production data before the old one goes.
Agreement on a sample is the evidence that the cutover is safe, and a
disagreement is a finding whichever way it falls. The old path goes in a
separate commit from the new path arriving, so a revert is one commit.
