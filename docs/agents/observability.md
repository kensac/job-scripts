# The fleet, batched work, and observability

How work runs and how you find out what it did. Named `observability.md`
because that is what most visits are for; the fleet and batching are here
because the signals only make sense beside them.

## The worker fleet

**Resume PDF extraction runs after durable transfer.** The API verifies each
object part before recording it as received. Completion enqueues
`process_resume_upload`. The worker reads parts into a disk-backed file and
writes extracted text as ordered parts. Only a complete text version becomes
the current resume. A failure leaves the PDF and any partial text version
stored; the upload status shows the error. Retrying completion starts a new
text version. Replacing a resume saves its prior text and PDF in revisions.

**Resume conversion history stays recorded.** Keep `resume_backfill_runs` and
`resume_legacy_clear_runs` so each copied and cleared value remains auditable.
New resumes and revisions use canonical text versions and PDF uploads only.

Tasks are claimed with row-level locking and skip-locked selection. A worker
heartbeats while it holds a task.

**Admission and claiming are separate contracts.** `api.task_admission`
serializes application drafts, upload extraction and source pulls against a
stable job or source row before checking for conflicting tasks. Its
`in_flight` reader also supplies the application view. Filter runs use
`api.filter_runs`, which locks the user row because single-filter and all-filter
runs overlap. Route authorization stays with the caller; admission does not
grant access to a subject.

Interactive admission returns a conflict for pending, running, waiting or
parked work. Scheduled ingestion skips sources with pending work, may queue
behind an hourly pull already running, and respects longer source intervals
after running or successful work. Keep these checks in admission. Terminal
status alone does not prevent a new run; intervals and per-cycle dedupe keys
still apply.
Cancellation remains an atomic transition from active states. Queue claim and
reaping policy belong to `api.worker_lifecycle.claims`; task execution and failure
settlement and claim-guarded heartbeats belong to `api.worker_lifecycle.execution`;
cycle scheduling belongs to `api.worker_lifecycle.scheduling`. `api.worker` owns the
process loop, held claims, slots and signal release. Claim-aware writes and progress updates
belong to `tasks.runtime.lifecycle`.

**The admin fleet view has three owners.**
`api.routers.admin.fleet_tasks` lists and cancels tasks; the summary uses
the full filter selection while cursor pagination narrows only rows.
`api.routers.admin.fleet_workers` reads worker heartbeats, running work and
throughput without claiming tasks. `api.routers.admin.fleet_batches` lists
provider batches and prices each verdict in a batch drill-down.
`api.routers.admin.fleet` registers those routes in their existing order.
Keep cancellation in `api.queue` and keep worker claim and reaping writes
with their owners above.

**A task row is written only by its owners.** `api.worker_lifecycle.claims` claims
and reaps; `api.worker_lifecycle.execution` settles transient and deferred runs and
beats; `api.worker` releases claims on exit. `tasks.runtime` writes progress
(`set_progress`, `checkpoint`), parks (`park_waiting`, `park_awaiting_batch`) and finishes, each
behind the claim. `api.queue` inserts (`enqueue`), merges payload keys
(`merge_payload`, unguarded on purpose: the keys record paid or finished work)
and cancels. A handler that restates one of these writes drifts from it.
`tests/test_task_writes_owned.py` fails on an
`UPDATE tasks` anywhere else, and on an `INSERT INTO tasks` outside the
admission paths it lists with their reasons.

**A worker claims only kinds its own image has a handler for.** A roll goes
host by host, so for a minute an old image and a new one share the queue. A
kind the new image added must wait for a host that can run it, rather than be
claimed and failed as unknown by one that cannot. The registry in `src/tasks/__init__.py` is what the worker
can do, and the claim reads it; the kind allow and exclude lists narrow from
there.

**Eligible tasks are claimed by age, and a scheduled pull counts as one of
its source's intervals younger.** The order is `api.worker_lifecycle.claims.CLAIM_ORDER`:
`created_at` (or `not_before` when later, so a deferred task counts from
when it is due again), plus `sources.ingest_interval_hours` when the payload
names a host, then `id`. Only the scheduler's pulls name a host (it is what `api.hosts`
paces on), and they are the one kind enqueued hundreds at a time; every other
kind is at most one pending task per key. In plain id order a burst of pulls
held everything enqueued after it: on 2026-10-10 a `poll_batches` waited 95
minutes behind 240 pulls and 115 board recomputes. The shift is derived, not
tuned. A pull that has waited its source's interval has missed one pull, and
the scheduler makes no second one while it is pending, so the delay never
stacks. It also bounds the wait: a pull created at T is claimed before any task
created after T plus its interval, so a backlog of short kinds cannot starve
ingest. A pull a person asked for carries no host and keeps its place by age.
The order reads a `sources` row through a scalar subquery so the claim's
`FOR UPDATE` locks only the task row. It sorts every eligible pending row
instead of stopping at the first in index order; on production with 316
pending that cost 4 to 8 ms against 1 to 2 ms, both reading only
`idx_tasks_status`.

**Process alive and handler progressing are different facts.** A liveness
signal decoupled from the work cannot observe the work stopping; a liveness
signal coupled to the work stops when the work stops. Neither alone is
sufficient, and reaping keyed on the wrong one either kills healthy work or
never recovers stuck work.

A handler that never yields holds its worker until it finishes. Long handlers
should hold progress in the database so an interruption resumes rather than
restarts.

A worker runs one task at a time unless `JOBTRACKER_TASK_SLOTS` or
`worker_task_slots` gives it more, and
its housekeeping (reaping, scheduling, gauges) runs only between the tasks it
runs on its main loop. A long task on the main loop therefore starves
scheduling on that worker; keep tasks short and let the queue carry the
volume.

**A worker given more than one slot runs several tasks at once, and only
an audited kind leaves the main loop.** The ceiling comes from two places.
`JOBTRACKER_TASK_SLOTS` in the host's environment is the host's own value,
sized for its memory by whoever sizes the host. A `worker_task_slots` entry
for the worker is the administrator's override. With both set the smaller
wins, so the admin page can lower a host without a deploy but cannot raise it
past what the host was sized for; with neither, the worker runs one task at a
time. Either way the ceiling is capped at half the connection pool because
each task can hold a connection and its heartbeat another
(`api.worker._task_ceiling`). Under it an AIMD limit sets the live
limit. It starts at the ceiling when `JOBTRACKER_TASK_SLOTS` is set, because
the host's value is known at start and climbing from one after every deploy
cost each worker a window of completions per step; with only the
`worker_task_slots` entry it starts at one. It grows by one with each task
that ends normally, never by comparing completion rates: the queue mixes
five-second pulls with half-hour ones, so a window's rate measured which
tasks it drew, and on 2026-11-22 hetzner and oci sat at 1 of 4 with 4,000
pulls waiting. It halves when a task fails on the host running out of memory, threads or
disk (`_TRANSIENT_MARKERS`, `MemoryError`) or when free memory, the
tighter of the cgroup's headroom and the host's `MemAvailable`, falls under
`worker_memory_reserve_mb`. A task costs its kind's `worker_task_weights`
slots; a kind heavier than the free room is left for another worker. Nothing
past the first task is claimed without the reserve free, and a worker holding
nothing always claims one, so the floor is the worker as it was. The live
limit and every held task id are in `worker_status.task_limit` and
`current_task_ids`; `current_task_id` stays the oldest, so "idle" reads the
same.

A `worker_status` row outlives its host. `DELETE /admin/workers/{name}`
forgets a worker past `health.WORKER_FRESH` and refuses a live one with 409
`WORKER_ALIVE`, since it would re-register on its next beat.

Kinds in `api.worker.THREADED_KINDS` run in a thread of their own on an event
loop of their own; every other kind runs on the main loop one at a time
beside them. A thread, because handlers call the database synchronously and
some never await, so on a shared loop one task's statements stall the rest.
A loop of its own is why a kind is listed only after an audit: a module
global bound to the loop that made it (`core.batch._batch_client`) breaks on a
second loop, and process state a handler shares with its siblings (host
pacing in `core.fetching.client`) needs a lock. Only `ingest_source` is
listed: it makes no model call, holds no loop-bound global, and is the
volume. Add a kind to the list with the same audit written beside it.

The main loop is `api.worker._MainLoop`: one event loop for the worker's
life, on a thread of its own, so the claiming thread is never inside a task.
While a main-loop task runs, a worker with slots keeps claiming threaded
kinds and skips the other main-loop kinds. Before, the claiming thread ran
the task itself, and a worker in `run_all_filters` or
`fetch_missing_content` started no pull until it ended: on 2026-11-22
optiplex held 3 of its 10 slots through a filter run with 2,300 pulls
waiting.

Every in-flight task keeps its own claim (the runtime contextvar is per
task) and its own heartbeat thread, and is mirrored in `api.worker._in_flight`
for the one reader outside every task's context, the SIGTERM handler, which
requeues each held claim under its stamp and exits without waiting: a pull
can run half an hour and a stop grace is seconds. A task that ends with an
exception is settled by its own handler path and frees its slot; one whose
thread dies outside that path keeps its claim with the beat stopped and goes
to the reaper. An OOM kill of the process takes every held task to the
reaper, which requeues each under the same rules as one. The slots, the
claim stamps, the release and the back-off are pinned by
`tests/test_worker_slots.py`.

**A per-candidate database loop in a task handler is batched.** Workers run
far from the database: `oci` is about 103 ms from it, the hosts beside it under
a millisecond. A query issued once per candidate costs its round trips (BEGIN,
the statement, COMMIT) times the candidate count, which a near host never
notices and a far host pays in hours: a managed-board run's verdict-cache loop
went from 105,273 round trips to 6 (measured 2026-10-04). Read a candidate list's facts in one
statement over `= ANY(%s)` (`store.decided_custom_urls`, `store.get_contents`)
before the per-candidate work starts, and test it by counting statements for N
candidates, as `tests/test_verdict_cache_batch.py` does. A loop whose every
pass also makes a provider call is still batched when the query can be lifted
out of it.

**A lone statement runs in autocommit.** `db.query`, `query_one`,
`query_as`, `query_one_as`, `execute` and `execute_count` go through
`core.pool.statement()`: the open `transaction()` when there is one, otherwise
a pooled connection in autocommit, returned to the pool with autocommit off.
An implicit transaction around one statement adds BEGIN and COMMIT, two round
trips that change nothing; the `core.pool.statement` docstring has the
measurement. On `oci` the worker's housekeeping
(reaper, chunk reconcile, gauges, scheduler) held it about 28 s of every
minute between tasks during the 2026-10-10 backlog, against 8 s on `gcp-vps`
and under a second beside the database. Several statements that must commit
together still go in `transaction()`; `executemany` and `pipeline()` keep
their implicit transaction.

## The AI switch

**`ai_paused` stops every paid model request, and nothing else.**
`api.ai_pause.check()` sits at the two places every paid request passes:
`api.ai.parse` for live calls and `tasks.runtime.submit_or_collect` before a
batch is submitted. A batch the provider already accepted is still collected.
A task refused there ends `done` with the reason, not `failed`. The scheduler
enqueues no AI kind while it is on (verification, reverification, filter runs,
managed boards, application sweeps, derivations, mail classification), and an
AI route answers 503 `AI_PAUSED`, from `require_config` or the app's handler.
Ingest, page fetches, availability, board recomputes and digests keep their
schedule. A new paid path goes through one of the two chokepoints or calls
`ai_pause.check()` itself.

**Resuming reads the pause's last days, not its backlog.** A posting whose
`jobs.created_at` falls in an ended pause, earlier than
`ai_resume_backlog_days` before it ended, is left out of verification and
location classification (`ai_pause.SKIPPED`). Every board, filter and
extraction needs a verification answer first, so it reaches none of them. The
pauses are read from `app_config_changes`, so a pause set by anything but the
admin config route or a migration that writes the history row has no window.
`verify_new` with a `urls` payload still verifies such a posting by name.

## Batched work

`core.batch` is the public batch entry point and owns the per-host wave
concurrency setting and cached client. `core.batch.specs` owns immutable
request/result shapes, token estimates, chunk inputs and provider request
lines. `core.batch.transport` uploads and polls provider batches;
`core.batch.collection` reads completed output and records every provider
error; `core.batch.progress` interprets status and counts. Keep
`core.batch_capabilities` outside the runtime package: routing and model
selection read vendor capability without starting batch collection or storage.
Keep the caller's batch ids and paid results through partial submission or collection failures.
A terminal batch may be collected without waiting for unrelated unfinished
batches. The event hook reports progress and estimates; it does not book
model cost.

OpenAI refuses a `custom_id` over 512 characters, and a few Workday posting
urls are longer (three live ones ran 544 to 997 on 2026-11-20). Such an id is
sent as a digest (`core.batch.specs.wire_id`), and collection maps it back
through the request it answers or, when collecting later, the submitting
task's `batch_requests` rows.

**Two vendors' batches run here: OpenAI and Anthropic.** `core.batch` picks
the transport from the model's provider at submission, and from the batch id
afterwards: an Anthropic id starts `msgbatch_`, so status and collection need
no lookup. `core.batch.anthropic_batches` owns that vendor's protocol and
returns the same `BatchResult` and `BatchProgress` shapes, with usage in the
Responses receipt shape: cache reads and writes are counted inside
`input_tokens`, as OpenAI counts them, so consumers and `core.pricing` read
one shape. Its rules:

- A custom id outside `^[a-zA-Z0-9_-]{1,64}$` (every posting url) is sent as a
  digest (`wire_id`). Collection maps it back through the submitting task's
  `batch_requests` rows, found through `ai_batches.task_id`. A result whose
  sent id maps to no request is kept with an error, not dropped.
- Every request with instructions puts them in one system block ending at a
  one-hour cache breakpoint, except a `no_cache` request (#635). The vendor
  caches a prefix of 512 tokens or more and ignores a shorter one without
  error. Its tiers' write rate is therefore the one-hour rate, 2x input.
- Effort is checked against the model's declared set before anything is
  sent, and a request the transport cannot express fails the whole
  submission rather than one wave.
- A managed board may name an Anthropic model as its `requested_model`, and
  its runs submit through this transport. A model that declares no default
  effort runs at medium, the transport's own default (`filter_effort`),
  which admission writes into the run's snapshot.
- The vendor's states map to the poller's: `ended` is `completed`,
  `canceling` is `cancelling`, anything else is `in_progress`. Its errored,
  canceled and expired requests count as failed, and none of them is billed.

**A wave refused because the account cannot pay stops the submission.**
`core.batch.submit_batches` raises `core.fallback.ProviderExhausted` with the
waves already accepted and the requests never sent, since every later wave
would be refused the same way. `tasks.runtime.submit_or_collect`, given the
caller's shape as `fallback`, marks the provider, sends the unsent requests to
the next chain entry in the same sweep with a batch hook for that model, and
parks on every accepted wave. It stops and parks on what was accepted when no
entry can run or the fallback would cross the fleet ceiling; with nothing
accepted it fails with the provider's reason. Collection marks the provider
too when an accepted batch's requests come back refused for exhaustion: on
2026-10-31 batches accepted before the billing hard limit returned 33,365.
A submission a provider accepts clears its mark.

Another vendor's batches need their own module beside it and an entry in
`core.batch_capabilities` and `tasks.batch_policy`. A vendor that offers
batches is not one this application can collect.

Location classification uses `classify_locations_max_output_tokens` for new
submissions. The task model screen and fleet budget estimate read the same
configured shape. A multi-place response needs an array, so the default allows
more than a single city's answer; short answers still bill only emitted tokens.
Paid batches keep their original requests and incomplete JSON stays rejected.
Unclassified strings remain eligible for a later scheduled pass.

`job_profile_collection_enabled` controls new shared-profile collection at
scheduling, manual admission, and task execution. Pausing retains existing
profiles and lets paid batches collect their receipts. A task rechecks the
switch after selecting its inputs, immediately before handing off new work;
work already handed to the batch runtime may finish submitting. Re-enabling
the switch resumes normal eligibility selection, not a forced reclassification.

**A task's per-cycle size has one definition, and the fleet budget prices
the same one the handler applies.** The size is an `app_config` row named in
`api.task_config.PER_CYCLE_KEYS`, and the sweep and `api.budget` both read
it through `configured_shape`, so a change reaches both on the next cycle. A
handler that caps from anything else makes the ceiling price a fleet that is
not running. The shape's declared `per_cycle` is the row's seeded default,
and its derivation stays beside it in `core.shapes`.
A cap sized to fill the batch waves is computed from `core.batch`'s
`BATCH_TOKEN_BUDGET` and `BATCH_WAVE_CONCURRENCY` over a spec size measured
from `ai_batches.est_tokens / requests`, which is the estimate that chunks
the waves, not the billed input.

Batched work parks rather than holding a worker. Scheduled filter and draft
work can still run live when its key/provider path does not use batches.
Price the actual transport with `core.pricing`, not a blanket batch discount.

Filter request inputs live in `core.filters.build_custom_input`, shared by live,
batch and experiment callers. `api.ai.verdicts.Verdict` is their common verdict
shape and `record_ai_verdict` persists it; transport exceptions and retries remain
the caller's concern.
Ordinary custom filters request only `FilterDecision.should_filter`; missing
reason text is stored as NULL. `FilterResult` still reads older paid responses
with reasons, and the explicit explanation endpoint retains `FilterVerdict`.
`compute_filter_hash` preserves the historical explanation-prompt identity while
`build_custom_decision_instructions` changes only the requested output. Editing
output presentation must not force existing criteria to be judged again.
Application drafts share request construction and result persistence in
`tasks.application.draft_rows`.

The batch event hook takes the `payer` (`api.model_calls.Payer`): a user's or a
board's batch passes it, and the hook records it on `ai_batches` at submission.
The hook records progress only; it neither totals nor prices a batch.

**Every paid call is one row in `model_calls`, written by
`api.model_calls.record`.** A batch item is written by the receipt checkpoint
with its receipt, from the batch's payer, purpose and model, so a consumer that
writes nothing for an item has not left it unbooked; a batch whose payer was
never recorded writes nothing, and the task collecting it records its payer.
Live calls are written by `budget.record_tokens` and
`budget.record_managed_board_tokens` when not batched, and the admin re-check
by its route; a consumer's booking of a batched result only counts its tokens.
Each request is priced on its own tokens, so a tiered model's tier is chosen
per request. `tests/test_model_calls.py` holds that and fails on a second
writer.

**Spend and budget read `model_calls`, never `api_usage`.** The weekly user
budget, the fleet ceiling, /admin/spend's ledger and call list, a person's
usage, the admin's view of a person, and a board's cost all do. A count of
calls is `SUM(requests)`: a backfilled batch that kept no per-request record
is one row standing for its requests. A batch's token totals and cost are a sum
over its calls (`model_calls.BATCH_TOTALS`), not columns of `ai_batches`.
There is no other usage ledger: `api_usage`, the totals on `ai_batches` and
the cost shares on `job_embeddings` were dropped once the ledger held their
history.
`idx_model_calls_user_created` INCLUDEs the columns the per-person reads take
(`model_calls.user_spend_by_day`, `user_spend_by_purpose`,
`budget.spent_this_week`), so they are index-only. A column a new per-person
read takes goes into that list in the same change: otherwise the read is a
heap fetch per call, 4.2 s cold over the owner's 610k calls.

**Spend has separate cost populations.** `api/spend/ledger.py` owns
recorded model-call totals and cuts. `api/spend/verdicts.py` owns verdict
diagnostics, reach and supersession estimates. `api/routers/spend.py` exposes
those populations with distinct labels and handles call-list and provider-cost
routes. Keep recorded call counts distinct from verdict-row counts, preserve
NULL for unknown prices, and reconcile daily and dimensional cuts to their
own totals before changing a spend query. Money response fields remain
numbers; declaring a Decimal response field would serialize it as a string.

On resume, `collect_pending` attaches model provenance from each `ai_batches`
row. `run_batched` resolves routing only for new submissions; absent persisted
model metadata stays unknown. Filter and application handlers collect paid work
before checking current keys, resumes, or automatic-draft settings. Original
filter input content is not retained in legacy task payloads, so resumed verdicts
leave that field unknown rather than attributing today's page to an earlier call.

**Mail classification task stages keep distinct owners.**
`tasks.mail.classification.handler` owns the resume-first gate, message claim,
batch submission, receipt consumption and append-only event write.
`tasks.mail.classification.identity` derives mailbox addresses and supersedes
self-sent events without a model; `tasks.mail.classification.dates` resolves
stated dates without guessing a time zone; `tasks.mail.classification.spec` owns
the request text and output shape. Keep the
scheduler's persisted `mail_classification_enabled` switch paused until
the product decision changes it. A module split must not turn it on or discard
already submitted batch receipts. The prefilter orders the selection but
never removes stored mail from eligibility.

Batch requests retain their immutable input, instructions and consumer context in
`batch_requests`. Collection checkpoints each provider batch/custom ID receipt
before removing pending batch IDs. A task payload marker retains empty terminal
collections too; request snapshots alone never imply accepted submission.
Consumers use `consume_result` to commit domain writes, user usage and
acknowledgment in one transaction; replay skips acknowledged
receipts. Fleet totals and their ledger entry share a transaction in the event hook.

**Collection commits a chunk of receipts per transaction, not one.** Per
result, a filter verdict cost about ten round trips (BEGIN, the receipt lock,
the instruction lookup, the verdict, the ledger row, the review outcome, the
acknowledgement, COMMIT), which for a 23,396-result run measured 230,353
round trips: 6.6 h at the 103 ms `oci` is from the database. Filter collection
(`tasks.filtering.execution`) passes `COLLECT_CHUNK` results to
`batch_results.consume_results`, which locks and acknowledges them in one
statement each; verdicts go in through `verdicts.record_ai_verdicts` (one
pipelined insert) and outcomes and ledger rows through `db.pipeline()`. The
same run is 852 round trips, about 88 s at 103 ms. The contract is the one
`consume_result` has, held per chunk:

- A chunk is all or nothing. A crash inside it leaves no verdict, ledger row,
  outcome or acknowledgement, and replay collects it from the unconsumed
  receipts.
- A chunk that raises is collected again per result with `consume_result`.
  That is the isolation per-result collection always had: what precedes a
  poisoned result commits, the poison raises for the task's retry, and what
  follows stays unconsumed. Nothing catches a poison and moves on.
- Each table receives its rows in result order, so a chunk writes the rows
  the per-result form would, with the same ids unless a failed chunk drew
  from a sequence first. Timestamps are one per transaction.
- Whatever can fail runs before any usage hook, and verdict metrics are
  counted after COMMIT, because a process counter cannot be rolled back. A
  chunk failing at its ledger insert or COMMIT can still count the hooks'
  token metrics twice when it is collected again per result.

`tests/test_batch_collection_chunks.py` holds the chunked form to the
per-result form's database state, crash replay and poison isolation, and
times collection through a latency proxy so a round trip per result fails it.
Comp, job profiles, application drafts and the other consumers still consume
one receipt per transaction; a consumer moved to chunks takes the same
fallback and the same test.
Use receipt outcome counts for cumulative progress across partial collection and
replay. Both checkpoint tables expire with their owning task. Legacy requests
without a snapshot retain unknown input rather than using a current page.

**A rule that keeps a posting from a paid review writes nothing per posting.**
A title screen (core/screening.py) leaves the posting out of the candidate
SELECT, and the posting path and the admin funnel run it again when read. The
stored alternative, one decision row per task and posting with an outcome row
per paid verdict, reached 9.76M rows by 2026-10-10, 93.6% repeats.

**The data-flow page counts the gates by running them again.**
`GET /admin/data-flow` (api/routers/admin/data_flow.py) is posting_path
aggregated: one CASE built from the pipeline's own predicates (board criteria
`CONDITIONS`, `screening.skips_sql`, the latest closed, clearance and board
verdicts, `managed_board_jobs`) puts each posting a board's sources brought in
during the window at the first gate it stops at. The order is the pipeline's,
title gate and screen before the closed and clearance checks, because a gated
posting is never verified; a new gate goes into `STAGES` and the CASE at its
place in the pipeline, or the page and the pipeline disagree. One board's 30
days ran 7 to 47 seconds on production (2026-11-20), so answers are served
stale for `data_flow_cache_seconds`. The volume gate's skips are not counted
apart: they read as not yet verified.
A person's flow (`user_id`) is the same walk over the sources they follow,
with their criteria, their enabled filter's title screen and verdict, and
`board_visible`: the predicate api.board.visibility builds their board with,
so a change to that predicate changes `USER_STAGE_SQL` with it. People are
the product and come first on the page; the public boards come after.

Distinguish submission rejection from per-request failure using the stored
provider errors. Failure counts alone do not establish the cause. A submission
the provider refuses outright has no batch id to store an error under, so
when every wave is refused the task fails with the provider's reason in
`tasks.error`.

**Every error a batch returns is stored as the provider wrote it**
(`ai_batch_errors`, one row per failed request, or one row under an empty
custom_id for a batch rejected before any request ran). The batch row counts
failures; only the text says why, and a handler that skips errored results
must not be the only reader of it. A request the provider refused over HTTP
carries a null top-level `error` and its reason in `response.body.error`;
the reader takes the reason from either place, never a placeholder. The whole-failure alert carries the most
frequent stored reason and resolves once a later batch for the same purpose
succeeds, whatever fixed it.

**Selection must exclude work already in flight**, and a task's own in-flight
claim must not exclude the task itself when it resumes. A guard that hides a
task's own work from it will make the task discard results it already paid for.

Personal batch chunks recheck decisions after content preparation, before new
submission. One database snapshot reads exact prompt/model verdicts and older
same-user, same-prompt chunk owners. The lowest task id owns overlapping URLs,
including while pending; a failed owner with pending paid batch ids still holds
them for collection. Failed unpaid work can be retried. Paid resumes bypass
this check. Exclusions are logged as counts, not claimed dollar savings.

**Collection must be reachable when there is nothing new to submit.** An early
return on an empty selection, placed before collection, strands completed work.

**A task waiting on several batches collects the ones that finished and parks
again on the rest.** The unit of partial collection is the batch, not the
request: a provider batch yields nothing until it is terminal, so its slowest
request sets when any of it can be read. Size a batch knowing that.

The poll resumes a task once some of its batches are terminal and the rest
have run past `batch_straggler_hours` (persisted config). The resumed handler
goes through `collect_pending`, which takes what landed and rewrites the
payload to the ids still running. The worker parks a handler that returns with
ids left rather than finishing it. A handler must therefore be safe to run
again from the top with a subset of its results, which every batched sweep
already is: they iterate the results they were given and re-select on the next
run.

**A parked sweep does not hold up the next one.** A sweep whose predicate stays
true while its batch is in flight (no verdict yet) must not re-select what it
submitted, but it must not block every later posting either: one straggling
verify batch held the only `verify_new` for 15 hours on 2026-10-08 and no new
posting reached a board. `verify_new` starts a new sweep whenever none is
pending, running or waiting, and
`tasks.verification.selection._in_flight` excludes the postings a parked
sweep's `batch_requests` already name.

**A resume is not a retry.** Every claim increments `attempts`, because
`(worker, attempts)` is the generation stamp claim-guarded writes check. Only
the claim holder releasing its own claim (a `Deferred` requeue, a graceful
exit, `PayloadUnavailable`) gives the attempt back; a park never does,
because a lowered `attempts` on a parked task lets a later claim reuse a
stamp a lost worker still holds. The retry budget is `attempts - batch_resumes`
(`api.queue.RETRIES_SPENT`): `resume_parked` counts the claim it hands out,
and both the transient-error requeue and the reaper compare the difference
with `task_max_attempts` (app_config). Counting resumes as attempts let 404
batch tasks reach the cap by waiting alone in the 30 days to 2026-10-04.

**A failed task keeps its `batch_ids`.** Only `done` strips them, because only
then are the batches provably spent. A task failed by the reaper or by an
error still names batches nothing collected, and those ids are what a
recovery reattaches to.

**A parked chunk must not hold what its siblings decided, nor the next run.**
Each filter chunk materializes its own passes when it finishes. A split run
does not block the next cycle's run: the splitter excludes every url a live
chunk still holds (`board.in_flight_urls`), so the new run judges only what
arrived since. Only a run that has not split yet blocks another.

Dry-run a handful of live calls before committing to a large batch. A batch
fails whole, and the dry run also measures real token counts.

**A model or effort is measured through the production path, never through
a hand export.** An experiment names a step (filter, verify, comp,
requirements), a seeded sample size and the arms (model and effort). Every
request is built with the step's own instructions, schema and input (the
`ExperimentStep` declarations in `api/experiments.py`), one provider batch
goes per arm, and each arm is scored: cost and tokens per request, parse
failures, per-field agreement with a reference arm (the dearest by default)
and with what production decided for the same postings. The same seed draws
the same postings later. A script over an export does not build the request
production builds.

**`api.run_experiment` is the only way to run one.** There is no admin
screen, endpoint, task or table for it. Run it from a checkout. It uses the
production transport (`core.batch.run_responses_batch`), so a run on a
branch measures the branch's prompt and caps. It only reads the database, under a
read-only session, and writes answers to files, so it can point at production:

```
set -a && . ./.env && set +a
DATABASE_URL="$PRODUCTION_DATABASE_URL" PYTHONPATH=src python -m api.run_experiment run \
  --step verify --arm gpt-6-luna@low --arm gpt-6-luna@none \
  --sample 100 --seed s1 --label main --out ../exp/verify-main
```

Without `--submit` it sends nothing and prints the plan: requests, the
shortest, median and longest input (read them; a 110-character input is the
2026-09-06 bug), and the most the run can cost (every request spending its
whole output cap). `--submit` refuses a bound above `--max-usd`, 0.50 by
default. The run writes `run.json`, `results.jsonl` (answer, usage and cost
per arm and posting) and `summary.json`. A prompt change is evaluated by
running the same seed and arm on main and on the branch with different
`--label`s and scoring both together:
`python -m api.run_experiment score ../exp/verify-main ../exp/verify-branch`.
Put the summary in the PR. Spend from this path is in the run's files and the
provider's account, not in `model_calls`, because the tool does not write the
database.

## Application answers

Browser autofill stops at the free-response box on an application form. The
questions are public on four ATSs (Greenhouse and Workable as JSON, Ashby
through the GraphQL call its own page makes, Lever as the apply page's HTML;
`core/fetching/forms.py`), read once per posting url into `application_forms` from
inside the task, under the same per-host budget as an ingest. Workday and
Oracle keep the form behind a sign-in and SmartRecruiters publishes none;
there the person pastes the question in (`source = 'manual'`). Only the
employer's own questions are kept: the identity block, the resume upload and
the cover-letter slot are left to autofill.

A draft is per person and per job (`application_answers`), written from a
resume the person keeps here (its canonical text version) in a style they describe in their own words
(`user_settings.writing_style`, replacing the built-in default rather than
merging with it). The `application_draft` task batches the drafts through the
`application` shape, overridable from the task screen like any other step,
and books the tokens to the person, not the fleet; a person's own key runs
live, one question at a time, the way their filters do. The back-and-forth on
one draft is a live call on the person's configured model, because a person
is waiting for it, and every turn is kept on the row so the next request
starts from what they said. A draft is shown as editable text and never
submitted anywhere by this code.

**Answers are written ahead of need.** The hourly cycle queues an
`application_sweep` per person who has a resume in (their switch is
`prefs.auto_draft`, on unless set false): it reads the forms of the postings
on their board that have not been read, newest first and a bounded number
per cycle (`application_form_reads_per_cycle`), skipping a host whose slot
is closed rather than waiting on it; opens a row for every paragraph
question; and drafts every row without a draft in one half-price batch
(`application_drafts_per_cycle`). It never re-drafts: the button does that.
Sized 2026-09-06 against Kanishk's board: 2,614 visible postings, 1,307 on
readable hosts, about one form in three with a real question, about 13 new
postings a day; the first pass is under a dollar and the steady state is
cents.

## Assisted apply

The drafts cover the free-response box; the rest of an application form is
the same twenty facts asked in a hundred phrasings. A browser extension
(`extension/`) reads the form on the ATS page, asks the API what goes in
each field, fills, and stops. **It never clicks submit.** The person checks
the form and clicks the form's own button; the extension sees that click
and records what was finally in every field.

**Every field climbs one ladder (`api.apply.resolve`).** Bank: the person
typed an answer to this exact label before (`application_answer_bank`,
keyed by the normalised label). Profile: a regex table maps the label to a
fact on `user_settings.profile` (`api.apply.Profile`: identity, contact,
address, work authorisation, sponsorship, EEO answers defaulting to
declining, links, the default resume, and structured experience and
education). Draft: the label is a question with a draft in
`application_answers`, matched by the ATS field key or the question text.
Otherwise the field is left blank and listed for the person. The rules are
a table rather than a model call on purpose: the ledger shows which
phrasings the table misses, and a table grows from that evidence.

**The bank learns from submits, the ledger says what to fix.** A field the
person typed into and did not mark as free text goes into the bank on
submit; the next form with that label is filled from it. Every fill is a
row in `application_fills` with every field, the rung that filled it, the
final value and whether the person changed it. `GET /user/apply/report`
reads that ledger: fields by rung, the share filled and left unchanged, and
the labels most often blank or corrected. That list is the backlog; a label
on it is a rule to add or a fact the profile lacks. Never add a rule
without a label in the ledger asking for it.

**A submission lives on the fill that holds the fields.** A submit of an
open zero-field fill goes to the newest earlier open fill with fields for
the same person and url (`api.apply.submissions.target`). The empty fill
stays, names that fill in `superseded_by`, and holds no `submitted_at` or
`application_id`. Readers count submissions by `submitted_at` and need no
filter of their own. A field with no `changed` key has no known final: the
report counts it neither corrected nor unchanged.

**A report is the page as the extension saw it.** The panel's report
button posts `application_reports`: the fields as read with the markup
around each, what the API resolved, what took, and the person's note, for
triage later. `GET /user/apply/reports` lists them.

**The drafting rules are config, not code.** `application_draft_instructions`
and `application_suggest_instructions` in app_config hold the text the
model drafts and fills under; empty means the built-in constant. A wording
change is an admin edit that takes effect on the next draft, not a roll.
The person's writing style is appended per person.

**Files first, then let the page settle.** Attaching a resume makes Ashby
rebuild its form, and a field wrapper read before the attach is a detached
copy a moment later: a click on it still flips the widget on screen, on a
node the form no longer owns. The content
script attaches files first, waits until the page's inputs and element
count have been still for 1.5 seconds, re-reads every field, fills the
rest, and then checks what the page holds, filling once more anything it
dropped. Any ATS that rewrites its form after an upload is covered by the
same wait.

**What the rules leave blank, the model fills in one call.** After the
deterministic pass the extension sends every field still blank (except
free text, files and dates) to `POST /user/apply/suggest`, one live call
on the person's own model settings booked to the `application` purpose:
the fields with their options, the profile's answer as a hint where the
option wording was not recognised ("South Asian | Asian" against
"Asian/Pacific Islander"), the profile and the resume. An answer for a
choice field must be one of the options, through the same `pick_option` the
rules use. A multiselect answer is several options joined by " | ": the
request always says so (`MULTI_CHOICE`, appended whatever the configured
wording), and `pick_options` matches each item on its own, keeping the ones
that land. An answer that normalizes to nothing, such as "[]", matches no
option. The person still sees every
answer in the form before submitting, and an answer they leave in place
goes into the bank on submit, so the same question never costs a call
twice. A profile value may list alternatives in order, "South Asian |
Asian", and the matcher takes the first that lands. The profile's
willing_to_relocate and willing_onsite answer the two yes/no questions
every posting asks in its own words, and its free-text notes ("always
willing to relocate") go to the model with every call, so a standing
answer is written once rather than typed per form.

**The other ATSs are data, not code.** `extension/adapters/recipes/<ATS>.json` (one file per ATS, generated by
`tools/convert_ats_config.py` from a selector table in a common ATS-config format) describes 50 ATSs: url match patterns, fields that
name the fact they fill and the XPath and fill method that fill it, the
selectors that find custom questions and the recipe that answers them, the
Apply button that opens the form, the excluded urls and paths, the
embedded-form marks, the fill intervals and order, the deferred-submission
modal, the validation scope, and the continue, submit and success paths.
**The converter drops nothing**: a table field with no fact of its own is
kept with fact None and resolved by its label (the model drafts a cover
letter when the person's switch is on); a flow step with no value (begin,
save, expand a section, wait for the location box) is kept with fact
`step` and run in table order; second and third copies of a field map to
the one fact; the table's shared country and state maps ride in each
config. Three ATSs stay out (Homerun, PhenomPeople, Teamtailor): the table
gives them no url because they live on employers' own domains, and running
on every page to detect them is a separate decision. `extension/adapters/recipe.js`
interprets the config for whichever ATS matches the page and exposes the
same reader interface the hand-written readers do, plus a step model: with
the person's auto-advance preference on and no required field blank, a page
with a Continue and no Submit is filled and advanced, up to twelve pages,
and the page that submits is left to the person. Every key the
table uses has a branch in the engine; `tools/audit_ats_config.py` diffs
the table against the converter and the engine's key lists and prints, per
ATS, what is dropped or ignored. Run it after touching any of the three; it should report zero dropped and
zero ignored. A reader may
export `applyButton()`; on a posting page whose form is a click away the
panel offers "Open the application" and never presses it unasked. A field that names its fact is filled from
the profile without reading its label (`Field_.fact`); consents are
answered yes and a single-box consent is that box, because the extension
relays the consent of the one person it fills for, who asked for every
required field to be filled. Repeated groups (education, experience) are filled one entry per profile
row: the add button, the entry's container, the nested fields by the
table's name for each (which also carries the date format the field
wants, `start_date_slashes_MMYYYY`), the save step; the resolve response
carries the profile's rows for it. Ashby, Greenhouse and Lever keep their
hand-written adapter factories, which own their build entries; the registry
rejects duplicate adapter names. The Greenhouse reader also reads the employer's own
self-identification questions from the form API's demographic block and
matches them by label. **The page is the form**: every labelled control on
it is a field, its kind read off the widget (a react-select with one value
or several, checkboxes, radios, a native select, a textarea, a file, text),
and the API adds labels, required flags and option lists for the fields
it knows; a control the API never lists (Country, "Are you
Hispanic/Latino?") is a field all the same. The kind only tells the model
what shape of answer to give; the fill goes by the widget. A form reveals
fields as it is filled (race appears once Hispanic/Latino is answered), so
after a fill pass the form is read again and what appeared is resolved
onto the same ledger row (`fill_id` on the resolve) and filled, up to
three rounds. The city search box is left to the person and listed as
theirs to type: its geocoder answers late with the menu reporting closed,
and three attempts at it on Gusto's form each chose wrong ("New York"
landed in Sudan, "NY" in Nyala) or lost the menu; a wrong city on a
submitted application costs more than one box. A group's nested fields are
filled inside the entry's container or not at all, never against the
document, where the same selector matches the form's own fields.
Every fill pass posts its own page capture (fields, traces, what was
filled, the page) to `application_reports` with a note starting `auto:`,
and so does a page the form refused; the Report button remains for a note
in the person's words. Every capture carries the build stamp of the
extension that made it (runtime/application.js `BUILD`); a capture without today's
stamp is a stale folder, not a defect. The panel's switch "AI answers every blank box" sends the free-text boxes
to the model as well (the drafts cover the ones the board knew about). The
panel's preferences (that switch, minimised, the theme) are the person's:
they live in `user_settings.prefs.apply` through the settings endpoints,
read at start and written on every change with the row's other prefs
kept, and `chrome.storage.local` is the cache that paints first and is
shared across every ATS host. The admin list
`application_ai_never_fills` (location, by default) names the fields the
model never fills: the suggest call drops them and names them in its
reply, and the panel lists them as the person's. The model's answers go
on the fill's ledger row as they return (`ai_answer` as written, rung `ai`
with the value it became), so a fill nobody submitted still says what the
model said.
The form page maps back to the posting with and without a trailing slash,
which Workable's listings carry.

The extension calls the frontend's proxy from its background worker, which
rides the site's session cookie; the API's service token never leaves the
proxy. One adapter per ATS under `extension/adapters/`: Ashby reads the
page's widgets; Greenhouse pairs the fields of its public form API (fetched
by the background worker, since the page's content security policy does
not list that host) with the page by id, which is also the drafts' key;
Lever reads plain HTML. A form page maps back to the posting through
`posting_urls` in the apply router, which knows Ashby's /application,
Lever's /apply, Greenhouse's two hosts and its embed url.

**A board row change is an event too.** Every write to a board row, from
the board's own patch or the extension's submit, ends in `_write_board_row`
and publishes `{"type": "board_row", job_id, status, date_applied,
hidden}` on the person's channel. A status written at submit time is not a
task, so the task events never carried it and the board waited for a
reload to move the row; a view holding the board moves or drops the row on
this event and refreshes its counts. A bulk patch publishes once for the
whole selection, `{"type": "board_rows", job_ids, status, date_applied,
hidden}`: the publish is a synchronous post on the request path, and the
bulk endpoint exists so a 6,000-row selection is one request, not 6,000
posts (measured 2026-09-08: about 5 ms each when Centrifugo is healthy, 2
seconds each when it is not).

## Observability

Three layers, each answering a different question, none standing in for
another.

**Metrics** (`api/metrics.py`, Prometheus) answer "how much, how fast":
counters and gauges, no identity.

**Conditions** (`api/health/`, `health_alerts`) answer "is something wrong":
app-aware detectors comparing a window against a baseline, opening and
resolving alerts, mailing once. A new detector is written when a pattern
emerges, never one per traceback.

**Errors, events, logs and traces** (`api/telemetry.py`, PostHog, one project
end to end with the frontend) answer "what failed, where, on which release,
inside which request or task". Four things ship:

- Every unhandled exception in a request handler or a worker's handler.
- A queryable event wherever the service swallows or retries a failure that
  would otherwise leave no trace, for example (`task_failed`, `task_requeued`,
  `tasks_reaped`, `tasks_lost`, `ingest_pull_failed`, `source_switched_off`, `fetch_failed`,
  `fetch_deferred`, `ai_call_failed`, `alert_opened`, `alert_resolved`,
  `worker_started`).
- Every log record at INFO and above, through OpenTelemetry, uvicorn's request
  log included except probe paths (`/healthz`, `/metrics`). Its loggers do not
  propagate, so the handler is attached to them by name.
- One span per HTTP request and per worker task, with the database
  statements and outbound `requests` calls made inside it as children, so a
  trace of the queue reads as tasks with their fetches underneath. A
  statement or call made outside any task or request (the worker's
  heartbeat, claim and scheduler polls) starts no trace
  (`telemetry._root_sampler`).

Every record carries `service.name` (`jobtracker-api` or `jobtracker-worker`),
`service.instance.id` (the worker's fleet name), `host`, and `release` (the
image's commit, from the build arg). Every exception and event carries the ids
of the span it happened inside, so an error links to its request or task.

PostHog is a generic OTLP receiver: the full `/i/v1/logs` and `/i/v1/traces`
paths, bearer-authenticated with the same project key.

**A view that is the product of a task reloads on the task's event, matched
by subject, and reads in-flight work from the server.** Every change to a
task's state is published to `jobtracker:tasks` and, for a task with a
`user_id`, to `jobtracker:user.<id>`, carrying the kind, the status and the
subject (`job_id`, `filter_id`, `source`). A page matches "a task of this
kind about my subject reached a terminal state" from the event alone; it
does not match on a task id it remembered from its own request, because
that memory is gone on reload and never existed in another tab. For the
same reason the view's own GET serves the in-flight task (`task` on the
application view), so a button is disabled from server state, and the
request that would start a second one is refused with 409 `IN_PROGRESS`
rather than queued twice. The admin surfaces follow the same rule: the
sources ledger carries each board's in-flight pull, `POST /admin/ingest`
reports the boards it skipped for that reason and refuses only when every
board named is in flight, and a reparse of a posting already being parsed
is refused.

How much goes is a measurement, not a guess. Everything ships first
(`POSTHOG_TRACE_SAMPLE=1.0`, `POSTHOG_LOG_LEVEL=INFO`), the daily volume is
read in PostHog, and the two knobs come down if the bill or the noise says so.
Errors and events are never sampled. The volume by kind and worker is
`sum by (instance, kind) (increase(jobtracker_telemetry_exports_total{result="ok"}[7d]))`
in Prometheus; the week to 2026-11-26 shipped 16.0M spans and 0.59M log
records.

**What a log line is for.** INFO is for a fact a person reads that no row,
span or event already holds: a pull's summary, a decision the code made, a
fallback it took. A line per item that restates a row it writes (a page
fetched, a posting gone) or a task's row and span (started, done, parked,
deferred) is DEBUG, which a process prints only when it runs with
`JOBTRACKER_LOG_LEVEL=DEBUG`. Libraries that log every request they send
(`httpx`, `httpx2`) log from WARNING up, telemetry or not.

`telemetry` is a no-op without `POSTHOG_API_KEY`, so tests and a bare checkout
need no destination, and it says so once at startup. It never raises. It
counts what it could not send in `jobtracker_telemetry_failures_total`, and
every OTLP export attempt in
`jobtracker_telemetry_exports_total{kind,result}`, so whether it is shipping
is one query and zero failures is never mistaken for zero attempts.

The frontend captures its own exceptions and records upstream API failures on
its side; this layer is for failures inside the service that never surface as
a bad HTTP answer.

## A detector that raises is an alert

Health findings remain visible independently of email eligibility. Critical
incidents are eligible immediately; warnings wait for the daily UTC slot in
`health_warning_digest_hour_utc`. A late health run catches up. Reopened
incidents share the `health_notification_repeat_hours` cooldown, with stalled
tasks grouped by handler rather than task ID. A warning escalating to critical
bypasses the warning's cooldown. Empty feeds alone are warnings: successful
fetches returning no jobs do not prove the parser or source is broken.

The sender serializes delivery across workers and retries unacknowledged
eligible alerts. `notified_at` records successful SMTP delivery, never a delay
or suppression. SMTP and Postgres cannot commit atomically, so a crash between
delivery and acknowledgement can repeat a message. The unnotified detector
counts overdue eligible incidents, not warnings waiting for their digest.
Stall findings for tasks that stopped running resolve on the next successful
detector run; other absent findings retain the existing recovery grace.

**A provider out of credit is an open `provider_exhausted` alert, and the
alert is the state.** `api.model_fallback.mark` upserts it (critical, subject
the provider, the provider's reason in `detail`), so every worker reads one
row and the notifier mails it. No detector owns the kind, so a detection run
never resolves it; a submission the provider accepts does, and so does an
admin resolving it, which means "try it again". Routing skips the provider
for one scheduler cycle (`ingest_interval_minutes`) after its last refusal and
then tries it again, because a refused submission costs nothing and without
it nothing would observe the account paid. Each refusal counts in
`jobtracker_provider_exhausted_total{provider}`, each fallback request in
`jobtracker_model_fallback_requests_total{purpose,from_model,to_model}` with a
`model_fallback` event, and a live call refused this way counts as
`exhausted` in `jobtracker_ai_calls_total`. `GET /admin/model-fallback` shows
the exhausted providers and, per purpose, the chain as routing judges it and
the recent batches run on it.

Each section of `health.detect()` runs on its own. One that raises opens
`detector_failed` for itself, rather than taking the hour's run down and
letting every open alert auto-resolve.

**A detector reads the fleet's state, never the state of the worker it runs
on.** `data_health` is a queued task, so any worker runs it. The fleet
release check compares each worker that has run past `fleet_roll_minutes`
with the release most of those run. It once used its own release, and a
worker a Renovate bump behind flagged the ones ahead as hosts that did not
deploy. A commit cannot be ordered, so it names both releases and calls
neither older.

The `_detect_silent` section watches for success that did nothing: a sweep
finished with work in front of it and none completed, a task kind failing
three times in three hours, a task the reaper keeps handing back, an open
alert never mailed, and a pattern that admits every posting. A batched sweep
counts a line as done only when its row lands.

**`task_progress_stalled` means a handler is not advancing, so progress is
written at a granularity a slow host still advances.** The worker's heartbeat
is a thread: it proves the process is alive and nothing about the handler.
The detector judges the handler by `progress_at`, so a handler that writes
progress once per phase looks stuck for the whole phase. A loop over
messages, candidates or applications writes progress every few minutes of
work on the farthest host, as `match_mail` does every `PROGRESS_EVERY`
items. A phase that cannot report because it is one long
call is usually a per-candidate loop to batch.

Silence is measured from the later of `progress_at` and the current claim's
`started_at`, because `progress_at` survives a park, a graceful release and a
requeue; a resumed task must not inherit the hours it waited. Waiting on a
provider batch is `awaiting_batch`, not `running`, and is never judged. The
alert is critical because nothing else recovers a handler that has stopped:
its heartbeat thread keeps the reaper away and there is no runtime limit, so
it holds its worker until a person cancels it.

Application drafts carry a reserved answer ID and revision from submission to
collection. A result applies only while that reservation still owns the answer;
question changes and newer user intent invalidate it. Results without an
original reservation are accounted for but never assigned a guessed revision.
Task progress reports written, superseded, failed and unknown-request outcomes
separately. Persist result acknowledgement with answer changes and usage in one
transaction so a replay cannot append duplicate turns or charge the ledger twice.


Scheduled embeddings use `embed_postings_batch`. The worker claims only
registered kinds, so an older image cannot consume a kind it does not know.
Embedding batches retain the provider request's packed inputs and indexed
vectors in the shared request snapshots and result receipts. Each packed
request applies its current vectors and acknowledges its receipt in one
transaction; newer content prevents an older vector replacing it. Progress
counts packed requests. Fleet usage is exact per provider request; per-posting
usage remains an approximate equal share, and absent provider usage is NULL.

New embedding purchases cover the union of the personal visibility read,
including uploads and acted-on rows. The next sweep admits a posting when it
becomes visible, so similarity can remain unavailable until that batch
completes. Collection never rechecks the current visibility scope. Public lists
have no similarity consumer and their membership is unaffected.

## Embeddings from the fleet's own model

`embed_postings_local` (`tasks.derivations.local_embeddings`) embeds every
posting with page text into `posting_embeddings`, with a model served on
fleet hardware (`core.derivations.local_embeddings`, whose docstring carries
the judged comparison that chose it). It replaces the OpenAI sweep
(`embed_postings_batch`, `job_embeddings`): the similarity reader moves to
it, and the OpenAI sweep and table go.

**A kind that needs something only some hosts have is gated by the variable
that says a host has it** (`api.worker_lifecycle.claims.NEEDS_ENV`). A
worker without `JOBTRACKER_EMBEDDING_URLS` excludes the kind, so no other
host's denylist names it and a new host cannot claim and fail it. The
variable is a comma list of `llama-server --embedding` addresses; a host may
lend its GPU and its CPU as two servers. Requests in flight per server are
the slots the server reports (`/props`, llama-server's `-np`).

**A posting is embedded without the text its company says about itself.**
A paragraph is the company's when it repeats under at least two of its
titles and names the company (its full name, or without its legal form),
or repeats under at least half of its titles and at least three, unless
most of the postings carrying it put it under a role heading
("Responsibilities", "What you'll do", "Qualifications"; a company heading
such as "Benefits" closes the section);
those are taken out and the name is masked. The rule was chosen by reading
what each candidate removed: repetition across titles alone also stripped
role requirements (a hospital's nurse titles share their licensure line),
and exact phrasing across companies cannot tell company wording from
company content. The profile is derived per company (`company_text_profiles`,
`company_shared_paragraphs`), rebuilt when a company gains titles or was
profiled under an older `PROFILE_RULE` (`core.derivations.company_text`,
whose docstring carries the measurement: as written, 89% of a posting's ten
nearest OpenAI neighbours were its own employer's).

**A posting is embedded whole.** Its text up to `EMBEDDING_INPUT_CHARS` is
cut into chunks of the model's window, and the chunk vectors are averaged by
token count, then normalised. A chunk is sent as its own token ids, framed
as the server frames text (`read_frame`, read with each probe and checked
against the server's own tokenization of the prefixed text), not turned back
into text: two requests a posting, and what is embedded is exactly the
posting's tokenization. Special-token strings in a page stay text. Nothing is truncated to fit the window: over
150 postings, truncating to the window dropped 7% of the text and left 95% of
top-10 neighbours unchanged, but the whole posting is what the vector claims
to stand for.

**Every posting is embedded, the visible ones kept current.** A batch takes
the postings someone can see that need a vector first (a new page, a rebuilt
company profile), then never-embedded postings newest first, up to
`local_embed_batch`. A posting outside the visible slice is embedded once;
its row's `content_row_id` and `profiled_at` say what from. A run walks the
corpus batch after batch, checkpointing its cursor in the task payload, and
the next run resumes where the last one stopped, so a run a deploy kills,
whether requeued or replaced next cycle, loses nothing. Once the walk is
exhausted the next run starts again from the newest job, picking up what
arrived meanwhile and retrying what failed.
An idle run still walks every job to find none: 23 s over 2.1M jobs
(2026-11-26).

**A posting's requests are not traced.** The request threads run inside
`telemetry.untraced()`: the task's span stands for the run, and a span per
request was 130 a second from hetzner during the backfill, built under the
same interpreter lock as the requests.

**The servers do not wait for the next batch.** Selecting a batch, building
the profiles of companies the walk has not met, and stripping its text read
pages out of object storage; for a backfill that reaches a new company in
most batches, that took longer than embedding the batch (on hetzner at
32 postings a second with both rented GPUs often idle, 2026-11-26). The
next batch is prepared on a thread of its own while the current one is on
the servers, leaving out the current batch's visible postings, which still
need a vector until it lands.

**Servers come and go.** `JOBTRACKER_EMBEDDING_URLS` may name servers on
other machines, such as a laptop that serves only while unused. Each run
asks every server for its slots (`/props`) before each batch and runs that
many requests on it; a server that does not answer, does not connect, or
stops answering is left out of the batch and its posting handed to another.
A request that outlasts its read timeout is that posting's error, not the
server's absence. The
run's progress names the servers that did not answer.

**A failure lasts one pass of the walk.** A posting that errors on every
server still answering, or was in flight on two servers that went down, is
skipped, and tried again when the walk next comes round; so is one whose stored
text cannot be read (object storage answered, and the page is missing or
corrupt; storage that does not answer fails the run). Nothing records a
posting as bad: whether a failure
was the page's or a server's is a guess, and a wrong one would leave a
good posting without a vector for good. A run stops at a batch that stores
nothing, without moving its cursor, so failing servers end it rather than
walk past postings they could not embed.

`local_embedding_enabled` switches the sweep off, and an unchanged re-scrape
is re-stamped (`tasks.rescrape`).

## Scheduled filter transport

**Only the instructions are written to the prompt cache.** On a model with
explicit prompt caching (GPT-5.6 and later, where a cache write costs 1.25x
the input rate), `core.batch.cached_prompt` sends the instructions as a
developer `input_text` block that ends at a cache breakpoint, and turns the
implicit breakpoint off. The implicit breakpoint sits at the end of the
whole prompt, so every request wrote its posting text to the cache and
nothing read it back: in the 7 days to 2026-10-18, 140M tokens were written
against 126M cached reads, about $0.25/day of premium. The batch line and the
live call both use it, so no caller opts in. A prefix under the provider's
1,024-token minimum writes and reads nothing, which is right for plain
verification, reverify and comp, whose instructions are shorter than that.
The joint verify request carries a fixed description of its input
(`core.answers._INPUT_NOTE`) that adds no rule and keeps every combination of
board questions past the minimum: without it, unpublishing Aerospace New Grad
(2026-10-29) left the longest joint instructions near 860 tokens and
verification cached nothing, $0.120 per 1,000 batched requests against $0.091
with the block. The measurement is in that constant's comment. A board whose
question is removed or shortened can drop a combination back under the
minimum; `cached_tokens` on the verify purpose in `model_calls` shows it.
The block form left decisions unchanged against the previous request run
twice (2026-10-18, 200 postings per check, all sign tests p >= 0.45) and
took cache writes to zero.

A request that should never be written has no breakpoint at all; adding the
posting as a second breakpoint would bring the premium back.

`managed_board_cache_writes_enabled` controls provider caching on newly built
managed-board review requests whose model datasheet explicitly supports it.
When disabled, explicit-only mode with no breakpoints disables reads as well
as writes. The policy lives in immutable request context, not the
criteria hash: rollback does not invalidate verdicts or resubmit paid work.
A cache setting
does not guarantee identical sampled outputs or a particular saving.

Scheduled filter admission persists `scheduled=true`; older `batched=true`
parents retain the same meaning for their child chunks. Missing content is
fetched before submission, never used as a reason to make a live model call.
Failed fetches respect the content retry window and remain undecided for a
later eligible cycle; they are not immediately requeued inside the run.

`tasks.batch_policy.transport` decides how a scheduled run travels: the
shared key goes through the persisted batch collector, and a shared-key
configuration the collector cannot carry (a provider with no batch transport,
a model without JSON-schema output) fails with `BATCH_UNSUPPORTED` in task
progress/error rather than spending the shared budget live at full price. A
person's own key runs live, billed to them, because they were promised no cap
and their own bill and the collector holds only the server's key. Existing
batch receipts are collected before current settings are consulted.
Interactive filter runs keep their live path.

Managed-board admissions freeze an execution version, transport and resolved
effort in the task payload. New `managed_filter` work uses the persisted batch
collector only and is admitted as `run_managed_board_batch`, a kind an older
worker does not register and therefore cannot claim during a rolling deploy.
Payloads admitted before that contract existed keep the `run_managed_board`
live handler so work already running at deployment can finish, but a versioned
task never falls back to live execution. Managed-board batch receipts commit their
verdict and batch-priced board usage before acknowledgement, and the board
projection is replaced only after every provider batch in the immutable run
has reached a terminal state. `sponsor_filter_reuse` remains projection-only
and makes no inference call.

## Per-result observations

**Nothing increments a counter inside `tasks.payload` per collected result.** A
managed batch payload carries its whole job list (up to 1.9 MB), so every
`jsonb_set` rewrites the entire TOASTed value and locks the task row. Measured
2026-10-03: 259 managed batches took 74,432 such increments, about 104 GB of
rewritten payload, for counters with no reader. Per-result observations go in a
row keyed by what they observe, or are derived when read.

**A run's settings are stored once, and its payload points at them.** A filter
chunk and a managed board run carry `config_id`, a row of `run_configs`
interned by digest through `api.run_configs.intern`, instead of a copy of the
filter or the board's settings. Measured 2026-10-10: 8 distinct filter copies
over 17,733 chunks (81 MB of payload text) and 22 distinct board copies over
950 runs (88 MB, almost all the `sources` list). What changes per run (the
revision, the resolved model, the reservation, `urls`) stays in the payload.
Handlers read the settings through `api.run_configs.filter_of` and
`with_board_settings`; `tasks.board.submission_exclusions` is the one SQL
reader.

## Receipt responses are held by reference

A receipt's response (text, usage, error, finish reason and, for embeddings,
an `embedding_vectors_ref`) is a member of a verified bundle object, named by
`response_sha256` with the bundle in `response_object` and
`response_object_size`; `response` is NULL. The four are set together or not
at all (`ck_batch_result_receipts_response_whole`). Receipt identity, outcome,
acknowledgement timestamps, `finish_reason` and `output_tokens` remain on the
row, so outcome counts and the job profile report read no object. No object
lifecycle expiration or garbage collection may remove referenced objects.

**The checkpoint uploads before the receipt exists.** `checkpoint` bundles
the responses it is given with `put_values` and reads them back before its
transaction, as it does an embedding's vectors (`put_verified`); a storage
failure raises `PayloadUnavailable` before any receipt is written or any
batch ID removed, so the batch is collected again. While a worker below
`api.data_level` 3 runs, the response is written inline instead, because that
image reads the column itself.

**Readers resolve.** `unconsumed` and payload recovery pass receipt rows
through `batch_results.resolve_responses`, which reads each bundle once,
concurrently, outside any transaction. Consumed responses are never read
back by the replay path.

**Inline responses are moved, oldest first.** `move_receipt_responses`
(`tasks.receipt_responses`) moves every receipt still inline, found through
`idx_batch_result_receipts_inline`, once every running worker is at data
level 3, and stops between steps when `receipt_response_move_enabled` is off.
A step reads rows through a cursor until they hold a bundle per upload
connection. A moved row keeps its key, outcome and consumption.

The vectors themselves are an object of their own: `embedding_vectors_ref`
holds the bucket, content-addressed key, SHA-256, uncompressed byte size and
format version.

**Resume PDF parts remain available while any resume or revision references
them.** Do not expire or delete `uploads/v1/` objects or their receipts.
Account changes must not cascade away current resumes, uploads, text versions,
parts, or revisions. Verify bucket lifecycle rules before using object parts
as the sole PDF copy.

**PDF resume uploads start paused.** The persisted
`resume_pdf_uploads_enabled` setting stays off until the live bucket is checked
for expiry rules and available capacity. The admin config page exposes its
switch. The API and worker check it before completing an upload. Text resume
parts and existing PDF downloads remain available while uploads are paused.

**A new receipt's vectors are written to object storage before the receipt
exists, and the receipt holds only `embedding_vectors_ref`.** There is no
inline shape. `batch_results.checkpoint` uploads every
result's vectors with `PayloadStore.put_verified` outside any transaction,
concurrently up to `core.payloads.objects.MAX_CONNECTIONS`, then writes the receipts
and clears the collected batch IDs in one transaction. Storage that is
unconfigured or failing raises `PayloadUnavailable` before any receipt is
written, with the provider batch IDs still pending, so the task takes the
payload recovery path below and its next run collects the same finished
provider batch again; nothing is resubmitted or paid twice. An upload whose
receipt was never written stays unreferenced and is reused by the retry, since
objects are content-addressed. Every reader of receipt vectors goes through
`batch_results.response_payload` (`unconsumed`, `payload_recovery.retry`).

Objects are version 2 uncompressed JSON, and the reader verifies the canonical
JSON byte size and SHA-256. Version 1 (gzip) is not read and none remain. Do not write another format beside it without a reader for it first.

Replay reads only unconsumed receipts, so missing historical vectors cannot
reopen acknowledged work. A required external input raises `PayloadUnavailable`
before acknowledgement, never an empty substitute or a new paid submission.
The personal and administrative board reads continue using `job_embeddings`.

## Request snapshot storage and recovery

`api.ai.request_snapshots` is the one reader of request snapshots. A reference
that cannot be read is a required-input failure, never a legacy unknown
request. Hydration and
object verification must occur outside database transactions, including outer
transactions inherited through the shared connection context.

**Every request has one stored shape: a member of a version 3 bundle, held by
reference, with nothing inline.** There is no exception by kind, task status or
receipt state. Two shapes for one population means every reader, test and
audit must handle both, and the second shape becomes the place where the next
rule quietly does not apply. A hot path that needs care gets a solution, such
as proving a stored request by its digest instead of reading it. It never gets an exempt population. Readers accept nothing else, and
`snapshot_ref` is NOT NULL.

**A new request is written to object storage before its row exists, and the row
holds only the reference.** `batch_results.snapshot_specs` puts every request
the task has no row for into bundles (`PayloadStore.put_bundle`, split at
`core.payloads.objects.BUNDLE_MAX_BYTES` by `bundle_groups`). The bundles upload
outside any transaction, concurrently up to the client's connection pool
(`core.payloads.objects.MAX_CONNECTIONS`), and each one is read back member by member
before anything is written. Then each request is inserted with its member
reference. One call's requests are one task's, so a bundle never
mixes tasks. Never write a request inline or as a per-row (version 2)
reference: either is a second shape, and nothing reads it any more.
An existing row always wins the conflict and is what gets frozen and
resubmitted; a request that already has a row is not uploaded again.
A row this call just wrote is frozen from the value its upload read back, with
no further read. Freezing rows that already existed reads each of their bundles
once. Storage that is unconfigured or failing raises
`PayloadUnavailable` before any row is written or anything is submitted, so the
task takes the payload recovery path below with nothing paid. An object whose
insert lost the conflict, or whose batch failed on a later upload, stays
unreferenced; it is content-addressed, so a retry reuses it.

**A request is a member of a bundle: one object holding many requests of one
task.** One object per row is bound by object storage write latency, not
bytes: on 2026-10-03 Garage (data on CIFS with `data_fsync=true`) took about
0.67 s per PUT and reached about 16 PUTs/s at 64 in parallel. The reference
is version 3:
`{bucket, key, sha256, size, version: 3, member, member_sha256, member_size}`.
`key`, `sha256` and `size` describe the bundle, which is `encode_payload` of a
JSON object mapping each `custom_id` to its snapshot, stored at
`payloads/v3/sha256/<sha256>.json`. `member` is the row's `custom_id`;
`member_sha256` and `member_size` are of `encode_payload(snapshot)`. A reader checks the bundle's size and digest, then the
member's, and refuses a member named for another request.
`request_snapshots.load` reads only a member reference (`BundleMemberRef`);
`PayloadRef.parse` refuses one, so no other reader can mistake a bundle for a
single value.

**A bundle's fields are stored once, on `batch_objects`; a request row holds
the member's own.** Every row used to repeat its bundle's `bucket`, `key`,
`sha256`, `size` and `version`: 1,613,017 rows named 6,392 objects, 378 MB of
`snapshot_ref`'s 703 MB (2026-10-11). `batch_requests.object_id` points at
the object row, and the row keeps `member`, `member_sha256` and
`member_size`. A writer adds the object row (`request_snapshots.object_id`)
and the pointer with every new request. It also leaves the bundle's fields on
the row while `api.data_level.behind` counts a running image older than
`data_level.LEVEL`, because that image reads them there.
`tasks.batch_objects` (`consolidate_batch_objects`) moves the rows written
before: it points each row, waits on the same gate, then removes the fields
from a row only where its object row holds equal values (`@>`). A row that
differs keeps them and is counted in the task's progress (`unpointed`,
`holding`), and the task is not done while one does. The worker offers it
every ingest cycle until a run reports phase `done`. Its state is in its
payload, so a requeued run resumes at its last chunk and a new run at the
last run's. `tests/test_batch_objects.py` holds every phase to the readers
returning the reference the row was written with.

**Every reader selects the reference as `request_snapshots.REF` from
`request_snapshots.FROM`** (or `ROWS`, a task's requests), never
`batch_requests.snapshot_ref` alone: REF lays the object row's fields over
the row's, so a row reads the same before, during and after the move. Then
it goes through `request_snapshots.resolve` or
`request_snapshots.load`. A caller resolving many rows passes one
`BundleCache` for that call, so each bundle is read once:
`batch_results.snapshot_specs`, `batch_results.unconsumed`,
and `payload_recovery.retry`. The cache lives for one call and is never filled
inside a transaction.

A worker encountering `PayloadUnavailable` leaves a failed task with a
`payload_recovery.reason` of `payload_unavailable`, preserving provider batch
IDs and checkpoint state. The claim's attempt is returned, so repeated storage
outages do not exhaust execution attempts. After restoring object access, use
`python -m tasks.retry_payload_task TASK_ID` on the deployed worker environment.
It verifies required external request snapshots and unconsumed receipt payloads
before requeueing that exact task. Only when collection is explicitly
checkpointed and no provider batches remain does it skip consumed-only snapshot
hydration. It still rechecks all snapshot, receipt and task rows under locks. Missing evidence reports `unavailable`; concurrent
changes, ambiguous accepted provider work, and unsupported parent states report
`conflict`, without mutation. Neither result authorizes a fresh task submission.

Supported chunk parents can move from their derived chunk-failure state back
to `waiting`, never `pending`; cancelled, completed, or independently failed
parents require separate investigation. Recovery and parent finalization lock
the parent before the child state transition/count, preventing a stale count
from terminalizing a parent after its child was recovered.

## Listing text and raw records

Listings hold their text and raw record as members of bundles named by each
value's digest, with an inline fallback during an outage. The rules, the
backfill and the rollout order are in
[sources-and-boards.md](sources-and-boards.md#a-listings-text-and-raw-record-can-be-held-by-reference).

## Task job lists: managed-board runs and filter chunks

**Every reader of a task's job list goes through `task_jobs.run_jobs`, which
reads both shapes:** inline `jobs` (a live filter chunk), and `jobs_ref` with
`candidate_count`, a verified object. Two populations carry one (`task_jobs.MANAGED_BOARD_RUNS` and `FILTER_CHUNKS`):

- **Managed-board runs.** The candidate list was 7.9 to 8.6 MB of JSON per run
  and 94 percent of the payload (measured 2026-10-03). Its readers are the
  handler, once per run or resume, which passes the list on to
  `replace_projection`, and `tasks.retry_payload_task`.
- **Filter chunks** (`run_filter_chunk`, `run_filter_batch_chunk`). The list
  was 571 MB of the 1,301 MB of payload text written by 7,376 batch chunks in
  7 days (162 MB stored after compression; measured 2026-10-03). Its readers
  are the two chunk handlers, `tasks.retry_payload_task`, and two SQL readers,
  `tasks.board.in_flight_urls` and `submission_exclusions`, which run for
  every active chunk of a user on every split and every submission.

Either way the list rode along on every whole-payload write (batch id
appends, `finish`) and every whole-payload read (the
claim, progress events, the admin queue). A missing or unreadable object
raises `PayloadUnavailable`, so the task takes the payload recovery path
above; a payload with neither shape is refused the same way, never run as an
empty list. Every payload writer merges its own keys (`||`, `jsonb_set`) and
none writes `jobs`, which is what lets a task change shape underneath its
handler.

**A SQL reader cannot follow a reference, so a filter chunk keeps its URLs
inline as `urls` beside it.** That is the URL of every job in list order,
272 MB of the 571 MB (same measurement); company and title go to the object.
`run_jobs` refuses an object whose URLs differ from `urls`. Both SQL readers
read a chunk's URLs through `tasks.board.CHUNK_URLS`, which yields the same
rows from either shape. Do not add a SQL reader of `payload->'jobs'`.

**Managed admission writes the list as a verified object before the task row
exists, and the payload holds only the reference.** Never write it inline
again. `managed_boards.admit` plans without holding a lock, uploads with
`PayloadStore.put_verified` outside any transaction, then locks the board and
checks again what a concurrent writer can change (the board row, an active
run, the sponsor's reservations) before inserting. Storage that is
unconfigured or failing is the refusal `STORAGE_UNAVAILABLE` (503 from the
run route): no task is queued and nothing is paid, and the scheduler tries
again next cycle. Objects are content-addressed and never deleted; candidate
lists rarely repeat across runs, so each run adds its list to the bucket.

**A filter run writes each batch chunk's list the same way.** `_run_filters`
uploads every batch unit's list with `put_verified` outside any transaction,
concurrently up to `core.payloads.objects.MAX_CONNECTIONS`, before it enqueues the
first chunk; each chunk payload holds `jobs_ref`, `candidate_count` and
`urls`. Storage that is unconfigured or failing raises `PayloadUnavailable`
from the parent with no chunk enqueued and nothing paid, so the parent takes
the payload recovery path and the next scheduled cycle plans again. Live
chunks keep their list inline: they are interactive, run in minutes, and
must not depend on object storage (0 were written in the 7 days measured).

A board's runs are found through `idx_tasks_managed_board`, partial on the two
run kinds. Its readers spell the kinds as SQL literals
(`Population.kinds_sql`), because a prepared statement's generic plan cannot
prove a partial predicate from a bound array.

**Only a live filter chunk holds its list inline.** Do not reintroduce an inline managed-board run
or batch chunk: an inline list beside a referenced one is the second shape
this rule exists to avoid.

## Shared query instruction text

`core.query_instructions` stores exact instruction strings separately from
verdict `prompt_hash` and the prompt-reporting catalog. NULL and empty strings
remain distinct. A row holds only `instructions_id`, a reference into
`ai_instruction_texts`; cached page content and accounting columns are
unchanged. `hydrate` never reads the inline `instructions` column, which is empty.
Query detail and custom-result readers hydrate through the
reference and fail explicitly if referenced content is missing or its digest
is wrong. Keep dictionary rows permanently while referenced.

The custom-verdict cache check (`store.decided_custom_urls`) answers which of
a run's urls have a decided verdict and returns nothing else, so it selects
only each url's latest row's instruction reference, in one statement for the
whole list. It never reads the page text: the filter sweeps asked it once per
candidate, 1.32M times in 36 hours (2026-10-03). A latest row's reference is
still hydrated, so a missing or corrupt dictionary entry fails the whole check,
as it fails every other reader. A caller that needs the verdict row's fields
reads them in its own query.
