# The fleet, batched work, and observability

How work runs and how you find out what it did. Named `observability.md`
because that is what most visits are for; the fleet and batching are here
because the signals only make sense beside them.

## The worker fleet

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
Cancellation remains an atomic transition from active states. Claiming and
retry policy belong to `api.worker`; claim-aware writes and progress updates
belong to `tasks.runtime.lifecycle`.

**A worker claims only kinds its own image has a handler for.** A roll goes
host by host, so for a minute an old image and a new one share the queue. A
kind the new image added must wait for a host that can run it, rather than be
claimed and failed as unknown by one that cannot. That happened to the first
classify_locations task on 2026-09-05, in the seconds before the claiming
host's own deploy. The registry in `src/tasks/__init__.py` is what the worker
can do, and the claim reads it; the kind allow and exclude lists narrow from
there.

**Process alive and handler progressing are different facts.** A liveness
signal decoupled from the work cannot observe the work stopping; a liveness
signal coupled to the work stops when the work stops. Neither alone is
sufficient, and reaping keyed on the wrong one either kills healthy work or
never recovers stuck work.

A handler that never yields holds its worker until it finishes. Long handlers
should hold progress in the database so an interruption resumes rather than
restarts.

A worker runs one task at a time, and its housekeeping (reaping, scheduling,
gauges) runs only between tasks. A long task therefore starves scheduling on
that worker; keep tasks short and let the queue carry the volume.

## Batched work

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

Batched work parks rather than holding a worker. Scheduled filter and draft
work can still run live when its key/provider path does not use batches.
Price the actual transport with `core.pricing`, not a blanket batch discount.

Filter request inputs live in `core.filters.build_custom_input`, shared by live,
batch and experiment callers. `api.verdicts.record_ai_verdict` persists their
common verdict shape; transport exceptions and retries remain the caller's concern.
Ordinary custom filters request only `FilterDecision.should_filter`; missing
reason text is stored as NULL. `FilterResult` still reads older paid responses
with reasons, and the explicit explanation endpoint retains `FilterVerdict`.
`compute_filter_hash` preserves the historical explanation-prompt identity while
`build_custom_decision_instructions` changes only the requested output. Editing
output presentation must not force existing criteria to be judged again.
Application drafts share request construction and result persistence in
`tasks.application.draft_rows`.

For these user-charged paths, `api.ai.batch_usage` normalises provider usage and
`api.budget.record_tokens` writes the user ledger with explicit batch pricing
and cached-token counts, including consumed calls that produced no valid answer.
The batch event hook must use `charged_to_user=True` to avoid booking the same
call to the fleet. Historical user ledger rows have no request or batch linkage;
do not infer their transport from timestamps or rewrite their prices on read.

On resume, `collect_pending` attaches model provenance from each `ai_batches`
row. `run_batched` resolves routing only for new submissions; absent persisted
model metadata stays unknown. Filter and application handlers collect paid work
before checking current keys, resumes, or automatic-draft settings. Original
filter input content is not retained in legacy task payloads, so resumed verdicts
leave that field unknown rather than attributing today's page to an earlier call.

Batch requests retain their immutable input, instructions and consumer context in
`batch_requests`. Collection checkpoints each provider batch/custom ID receipt
before removing pending batch IDs. A task payload marker retains empty terminal
collections too; request snapshots alone never imply accepted submission.
Consumers use `consume_result` to commit domain writes, user usage and
acknowledgment in one transaction; replay skips acknowledged
receipts. Fleet totals and their ledger entry share a transaction in the event hook.
Use receipt outcome counts for cumulative progress across partial collection and
replay. Both checkpoint tables expire with their owning task. Legacy requests
without a snapshot retain unknown input rather than using a current page.

Review-gate admissions have their own durable record in `review_gate_decisions`.
Each task and posting URL has one immutable decision, including detailed-review
admissions, with its exact policy, title, available content hash, proven profile
evidence and shadow-routing observation. Retries preserve that decision;
different inputs within the same run are refused rather than silently assigned
its provenance. Configuration changes apply to new runs. These records have no
cascading references to task or catalog retention. Managed projections prefer
them and read the legacy task plan only where no durable admissions exist.

`review_gate_outcomes` links an admission to the exact paid verdict written by
the shared verdict service. Live writes commit their outcome with the verdict;
batch writes commit it within receipt consumption. Replaying a receipt cannot
duplicate its outcome, while distinct paid retries remain distinct attempts.
The stored verdict price is copied at write time, never repriced on read or
charged again. Missing usage and pre-admission legacy requests stay unknown.
An admission alone proves neither provider submission nor a successful review;
skip counts are avoided requests, not observed dollar savings.

Distinguish submission rejection from per-request failure using the stored
provider errors. Failure counts alone do not establish the cause.

**Every error a batch returns is stored as the provider wrote it**
(`ai_batch_errors`, one row per failed request, or one row under an empty
custom_id for a batch rejected before any request ran). The batch row counts
failures; only the text says why, and a handler that skips errored results
must not be the only reader of it. The whole-failure alert carries the most
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

**A parked chunk must not hold what its siblings decided, nor the next run.**
Each filter chunk materializes its own passes when it finishes. A split run
does not block the next cycle's run: the splitter excludes every url a live
chunk still holds (`board.in_flight_urls`), so the new run judges only what
arrived since. Only a run that has not split yet blocks another.

Dry-run a handful of live calls before committing to a large batch. A batch
fails whole, and the dry run also measures real token counts.

**A model or effort is measured through the production path, never through
a hand export.** `POST /admin/experiments` names a step (filter, verify,
comp, requirements), a seeded sample size and the arms (model and effort);
the `run_experiment` task builds every request with the step's own
instructions, schema and input, submits one provider batch per arm, parks,
and on resume scores each arm: cost and tokens per request, parse failures,
per-field agreement with a reference arm (the dearest by default) and with
what production decided for the same postings. The same seed draws the same
postings later. The 2026-09-06 filter comparison was a script over an
export, and the script fed every request the posting's first line; its
headline was wrong and was nearly acted on. That is why this exists.

## Application answers

Browser autofill stops at the free-response box on an application form. The
questions are public on four ATSs (Greenhouse and Workable as JSON, Ashby
through the GraphQL call its own page makes, Lever as the apply page's HTML;
`core/forms.py`), read once per posting url into `application_forms` from
inside the task, under the same per-host budget as an ingest. Workday and
Oracle keep the form behind a sign-in and SmartRecruiters publishes none;
there the person pastes the question in (`source = 'manual'`). Only the
employer's own questions are kept: the identity block, the resume upload and
the cover-letter slot are left to autofill.

A draft is per person and per job (`application_answers`), written from a
resume the person keeps here as text (`user_resumes`; a PDF is read at upload
and the file is not stored) in a style they describe in their own words
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

**A report is the page as the extension saw it.** The panel's report
button posts `application_reports`: the fields as read with the markup
around each, what the API resolved, what took, and the person's note, for
triage later. `GET /user/apply/reports` lists them.

**The drafting rules are config, not code.** `application_draft_instructions`
and `application_suggest_instructions` in app_config hold the text the
model drafts and fills under; empty means the built-in constant. A wording
change is an admin edit that takes effect on the next draft, not a roll.
The person's writing style is appended per person, as before.

**Files first, then let the page settle.** Attaching a resume makes Ashby
rebuild its form, and a field wrapper read before the attach is a detached
copy a moment later: a click on it still flips the widget on screen, on a
node the form no longer owns (report 2, Clera, 2026-09-07). The content
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
choice field must be one of the options. The person still sees every
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
same reader interface the hand-written readers do, plus a step model: a
page with a Continue and no Submit is filled and advanced, up to twelve
pages, and the page that submits is left to the person. Every key the
table uses has a branch in the engine; `tools/audit_ats_config.py` diffs
the table against the converter and the engine's key lists and prints, per
ATS, what is dropped or ignored. Run it after touching any of the three;
on 2026-09-08 it went from 214 dropped fields, four unloaded ATSs and
some 300 ignored keys to zero dropped and zero ignored. A reader may
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

**Conditions** (`api/health.py`, `health_alerts`) answer "is something wrong":
app-aware detectors comparing a window against a baseline, opening and
resolving alerts, mailing once. A new detector is written when a pattern
emerges, never one per traceback.

**Errors, events, logs and traces** (`api/telemetry.py`, PostHog, one project
end to end with the frontend) answer "what failed, where, on which release,
inside which request or task". Four things ship:

- Every unhandled exception in a request handler or a worker's handler.
- A queryable event wherever the service swallows or retries a failure that
  would otherwise leave no trace (`task_failed`, `task_requeued`,
  `tasks_reaped`, `tasks_lost`, `ingest_pull_failed`, `fetch_failed`,
  `fetch_deferred`, `ai_call_failed`, `alert_opened`, `alert_resolved`,
  `worker_started`).
- Every log record at INFO and above, through OpenTelemetry, uvicorn's request
  log included. Its loggers do not propagate, so the handler is attached to
  them by name.
- One span per HTTP request, per worker task and per outbound `requests` call
  (the board pulls and ATS resolvers), so a trace of the queue reads as tasks
  with their fetches underneath.

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
Errors and events are never sampled.

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

Each section of `health.detect()` runs on its own. One that raises opens
`detector_failed` for itself, rather than taking the hour's run down and
letting every open alert auto-resolve.

The `_detect_silent` section watches for success that did nothing: a sweep
finished with work in front of it and none completed, a task kind failing
three times in three hours, a task the reaper keeps handing back, an open
alert never mailed, and a pattern that admits every posting. A batched sweep
counts a line as done only when its row lands.

Application drafts carry a reserved answer ID and revision from submission to
collection. A result applies only while that reservation still owns the answer;
question changes and newer user intent invalidate it. Results without an
original reservation are accounted for but never assigned a guessed revision.
Task progress reports written, superseded, failed and unknown-request outcomes
separately. Persist result acknowledgement with answer changes and usage in one
transaction so a replay cannot append duplicate turns or charge the ledger twice.


Scheduled embeddings use `embed_postings_batch`, with the legacy
`embed_postings` handler only enqueueing that kind. The worker claims only
registered kinds, so an older image cannot consume the new batch snapshots.
Embedding batches retain the provider request's packed inputs and indexed
vectors in the shared request snapshots and result receipts. Each packed
request applies its current vectors and acknowledges its receipt in one
transaction; newer content prevents an older vector replacing it. Progress
counts packed requests. Fleet usage is exact per provider request; per-posting
usage remains an approximate equal share, and absent provider usage is NULL.

New embedding purchases default to `embedding_visible_only`: the union of the
personal visibility read, including uploads and acted-on rows, rather than the
broader subscribed-source working set. The next sweep admits a posting when it
becomes visible. Similarity can therefore remain unavailable until that batch
completes. The switch restores broad collection without deleting vectors or
canceling paid work; collection never rechecks the current visibility scope.
Public lists have no similarity consumer and their membership is unaffected.

## Scheduled filter transport

`managed_board_cache_writes_enabled` controls provider caching on newly built
managed-board review requests whose model datasheet explicitly supports it.
When disabled, explicit-only mode with no breakpoints disables reads as well
as writes. Personal filters keep default caching, since their reusable prefix
may be valuable. The policy lives in immutable request context, not the
criteria hash: rollback does not invalidate verdicts or resubmit paid work.
Legacy snapshot readers accept the context during rollout; verify active
workers have the new transport before measuring adoption. A cache setting
does not guarantee identical sampled outputs or a particular saving.

Scheduled filter admission persists `scheduled=true`; older `batched=true`
parents retain the same meaning for their child chunks. Missing content is
fetched before submission, never used as a reason to make a live model call.
Failed fetches respect the content retry window and remain undecided for a
later eligible cycle; they are not immediately requeued inside the run.

`tasks.batch_policy.transport` decides how a scheduled run travels: the
shared key goes through the persisted batch collector, and a shared-key
configuration the collector cannot carry (a non-OpenAI provider, a model
without JSON-schema output) fails with `BATCH_UNSUPPORTED` in task
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

## Filter routing observations

`filter_routing_policy` provides independent off/shadow controls for shared-profile
constraints, evidence-backed title screening and an ambiguity-only routing proposal.
It applies to new submissions through the shared personal/managed batch executor;
interactive live calls keep their existing path. All controls default off. Shadow
mode still submits every original request, with the same instructions and schema.
It makes no additional inference calls and does not promise immediate savings.

Profiles are compared against the exact posting content submitted for review and
the supported profile version. Policies name the exact filter prompt hash; no
policy is inferred from prose. Satisfying partial taxonomy rules cannot establish
acceptance of the entire filter. Missing or unsupported evidence abstains.

The route proposal is retained in the run's `review_gate_decisions.evidence`,
and collection records the paid result in `review_gate_outcomes` inside the
receipt transaction, so a replayed receipt adds nothing.
`review_gate_reads.comparisons(task_id)` derives agreements, false rejects, false
accepts, abstentions and unresolved reference results from those rows. These are
agreement measurements against the existing filter, not ground-truth accuracy.
Old paid requests without a proposal collect normally. Observations never enter
the verdict cache, usage ledger or board projection.

**Nothing increments a counter inside `tasks.payload` per collected result.** A
managed batch payload carries its whole job list (up to 1.9 MB), so every
`jsonb_set` rewrites the entire TOASTed value and locks the task row. Measured
2026-10-03: 259 managed batches took 74,432 such increments, about 104 GB of
rewritten payload, for counters with no reader. Per-result observations go in a
row keyed by what they observe and are derived when read. The `routing_report`
and `review_gate_comparison` keys in older payloads are historical, written by
releases before this rule, and nothing reads or extends them.

Live enforcement is intentionally not an accepted mode. It requires representative
held-out validation and a versioned decision/projection invalidation contract so
disabling a shortcut cannot leave its decisions cached as ordinary paid verdicts.

## Reject-only review gates

`filter_review_gate` is separate from the experimental routing observer. Its
title and profile controls independently accept off, shadow and enforce. Exact
prompt-hash scopes opt into versioned nontechnical occupation/family recipes;
unconfigured revisions and uncertain evidence receive detailed review. Profiles
never accept a posting or decide experience, compensation or prestige.

The shared live/batch executor applies the gates only before new review calls.
Profile reuse requires exact cached content and a retained, consumed profile
request proving the original title, instructions, version, model and response.
Missing provenance or bounded lookup failure retains detailed review. Reuse
creates no additional classification calls; the existing profile derivation is
still responsible for producing shared profiles.

Task payload `review_gate` records the policy, candidate, proven-profile,
proposed-exclusion and remainder counts, written once at admission. **The
skipped URLs live only in `review_gate_decisions`**, and
`review_gate_records.exclusions` derives them: `persist` writes a row for every
job in the transaction that writes the plan, so the derived set is exactly
what the payload used to copy. That copy was 53 percent of filter-chunk
payload text over 7 days (measured 2026-10-03) and was rewritten with every
later payload write. Plans written before #638 (2026-09-16) recorded decisions
carry `skipped` and no rows; `replace_projection` reads it only then. Never
write it again. Shadow comparisons against the paid result are derived from
`review_gate_outcomes` by `review_gate_reads.comparisons`, never counted into the
payload. Paid batches bypass replanning, including
after a switch changes. Skips are not paid verdicts, do not enter the verdict
cache, and create no usage. Managed projection explicitly excludes this run's
skips even when fail-open is configured. Switching off restores eligibility on
the next new run without deleting historical decisions or person-owned state.

## Historical embedding receipt payloads

`api.ai.receipt_payloads` copies only vector arrays from consumed receipts of
`done` embedding tasks. The response retains text, usage, errors and an
`embedding_vectors_ref` containing bucket, content-addressed key, SHA-256,
uncompressed byte size and format version. Receipt identity, outcome and
acknowledgement timestamps remain in Postgres. Profile receipts and active
work are excluded. No object lifecycle expiration or garbage collection may
remove referenced objects.

**A new receipt's vectors are written to object storage before the receipt
exists, and the receipt holds only `embedding_vectors_ref`.** Never write
them inline again: on 2026-10-03, four receipts written after the backfill
still carried inline vectors for the tooling below to move.
`batch_results.checkpoint` uploads every result's vectors with
`PayloadStore.put_verified` outside any transaction, concurrently up to
`payload_objects.MAX_CONNECTIONS`, then writes the receipts and clears the
collected batch IDs in one transaction. Storage that is unconfigured or
failing raises `PayloadUnavailable` before any receipt is written, with the
provider batch IDs still pending, so the task takes the payload recovery path
below and its next run collects the same finished provider batch again;
nothing is resubmitted or paid twice. An upload whose receipt was never
written stays unreferenced and is reused by the retry, since objects are
content-addressed. Every reader of receipt vectors goes through
`batch_results.response_payload` (`unconsumed`, `payload_recovery.retry`);
the tooling below verifies references directly against their inline values.

Run `python -m api.ai.migrate_receipt_payloads copy --limit N` with the private
`JOBTRACKER_S3_ENDPOINT`, `REGION`, `BUCKET`, `ACCESS_KEY_ID` and
`SECRET_ACCESS_KEY` variables (each with the `JOBTRACKER_S3_` prefix).
New objects use version 2 uncompressed JSON; the reader also accepts historical
version 1 gzip objects. Deploy version 2 readers everywhere before writing new
references. Both formats verify the canonical JSON byte size and SHA-256.
Copy retains inline vectors and verifies a GET before attaching the reference.
`verify` checks existing references without database writes. All modes have
database timeouts and return a cursor for `--after BATCH_ID CUSTOM_ID`. An
unavailable object, changed receipt or lost eligibility stops the run with a
nonzero exit; the cursor remains before that row so a retry cannot silently
skip it. Only the contiguous successful prefix contributes verified byte counts.

To move or roll back a known set of receipts rather than scan from a cursor,
name each with `--receipt BATCH_ID CUSTOM_ID` (repeatable, serial modes only).
Every mode then selects only those keys, so `verify` and `restore`, whose
predicates match every referenced receipt, cannot reach any other row.

Verification defaults to the original serial path. To group reads and overlap
object GETs, supply all three explicit positive bounds:
`--verify-group-size N --verify-workers W --verify-byte-budget B`, with W no
greater than N. These flags apply only to `verify`. Each group reserves the
serialized receipt bytes plus declared uncompressed object bytes before loading
payloads. A receipt exceeding B stops before hydration or GET with
`stop_reason=byte_budget`; retry with an appropriate bound, not a cursor past it.
This is a serialized-payload budget, not a process RSS limit: decoded JSON,
driver buffers and metadata have additional bounded overhead.

Grouped verification reads size metadata and exact row snapshots in a short
read-only transaction, closes it before object IO, then rechecks the full exact
row and eligibility in one grouped read. GET concurrency never exceeds W and
only one byte-bounded group is submitted. Completed futures retain scalar
outcomes, not vector arrays. GETs later in the current group may finish before
an earlier failure is observed, but their outcomes never advance the cursor or
verified-byte total past the first failing row. Changing concurrency does not
weaken reference parsing, SHA-256, byte size, array-shape or inline-equality
checks. These verification options do not change copy or compaction throughput.

For grouped compaction, explicitly supply `--compact-group-size N
--compact-workers W --compact-byte-budget B` with `--backup-complete`. The same
serialized-byte reservation and GET bounds apply. Each object is downloaded and
verified again even after a previous verification pass. No database lock or
transaction spans object IO. Afterwards, the operator locks parent tasks in
ascending ID order and receipts in cursor order, rechecks exact canonical rows
and eligibility, and removes only `embedding_vectors` with a native JSONB update
for the successful ordered prefix. Other response fields retain their exact
PostgreSQL values, without a Python JSON round trip.

Each grouped conditional update must affect the entire verified prefix; a count
mismatch or transaction failure rolls back that group. The returned cursor and
byte count retain only earlier committed groups. A later object failure or
eligibility change permits only its earlier unchanged prefix to commit. Retry
from the returned cursor; already compacted receipts are not selected again.
A `database_error` stop reason requires resolving the database problem before
retrying. Grouped controls are opt-in; serial compaction remains available.
Neither path deletes references or objects, reprices usage, or touches live
embedding vectors.

Deploy the compatible receipt reader before any compaction. After an independent
database copy finishes, `compact --limit N --backup-complete` verifies the object
again and removes only inline vectors. `restore --limit N` restores the exact
vector array and removes the reference. Both recheck eligibility and the entire
source receipt under task and receipt locks after object IO. Never wrap these
operations in an outer database transaction. A failed upload or changed receipt
leaves inline data intact; an interrupted copy may leave an unreferenced object.

Replay reads only unconsumed receipts, so missing historical vectors cannot
reopen acknowledged work. A required external input raises `PayloadUnavailable`
before acknowledgement, never an empty substitute or a new paid submission.
The personal and administrative board reads continue using `job_embeddings`;
archival does not alter those vectors or board membership. Report verified
logical bytes separately from table size and filesystem space: removal alone
does not return filesystem space, and no table rewrite is part of this command.

## Shared review decision bodies and policies

A review decision is a per-task row plus content that is stored once.
`review_gate_policies` holds each exact policy, `review_gate_decision_bodies`
each distinct decision (prompt hash, stage, mode, action, reason, profile id,
title, content hash, policy id, evidence) and `review_gate_urls` each URL. The
per-task row keeps task, job, user, filter, board, revision and creation time.
On 2026-10-02 production wrote 570,747 decisions holding 38,017 distinct
bodies, because every task re-admits the same postings under the same prompt
and policy. Interning uses PostgreSQL's JSONB serialization for digests and
checks exact equality of every column after a digest conflict. Never
reconstruct a historical policy or body from current configuration or a
filter hash. None of the three tables cascades from task, posting or filter
retention.

**Admission writes only references.** `review_gate_records.persist` interns
the policy, the URLs and the bodies inside the admission transaction, each as
one set-based conflict-safe insert followed by a read, and inserts per-task
rows holding `url_id` and `body_id` with `ON CONFLICT (task_id, url_id)`.
Never write the inline columns again: a second copy is the 1,242 B a row this
replaced, and the backfill would have to move it later.

**Every reader selects from `review_decision_storage.DECISIONS`**, which joins
a row's `url_id` and `body_id` and presents the columns the inline table had;
`RESOLVED_FROM` adds the body's policy snapshot. Filter a URL with
`URL_MATCH`, which compares `url_id` and uses
`idx_review_gate_decisions_url_id`; the resolved `url` has no index.

**The per-task row holds nothing but identity and the two references.**
`url_id` and `body_id` are NOT NULL under validated foreign keys, so the
readers' inner joins cannot drop a decision. `(task_id, url_id)` is unique,
and a URL has one id, so a task cannot hold a URL twice. 7ca95d34ef5f dropped
the twelve inline columns, `uq_review_gate_decisions_task_url` and
`idx_review_gate_decisions_url_created` (3.1 GB of index on 2026-10-03) once
every production row held its references and no inline value. The table
shrinks only with a rewrite, which no migration performs.

Downgrading past 7ca95d34ef5f adds the columns back empty. That is enough for
every release from #751 on, because they read references. A release older than
#751 reads only the inline columns: deploy the #753 image and run its
`api.migrate_review_decisions restore` before rolling further back. Keep
bodies, snapshots and decision identities throughout; do not alter paid
outcomes or reprice usage.

## Request snapshot storage and recovery

`api.ai.request_snapshots` is the shared reader for inline and external request
snapshots. An external reference that cannot be
read is a required-input failure, never a legacy unknown request. Hydration and
object verification must occur outside database transactions, including outer
transactions inherited through the shared connection context.

**A new request is written to object storage before its row exists, and the row
holds only the reference.** `batch_results.snapshot_specs` uploads every request
the task has no row for with `PayloadStore.put_verified`, outside any
transaction and concurrently up to the client's connection pool
(`payload_objects.MAX_CONNECTIONS`), then inserts `snapshot=NULL` with
`snapshot_ref`. Never write a new request inline: a second copy written for the
backfill to move later costs the bytes twice and leaves dead TOAST behind.
An existing row, inline or referenced, always wins the conflict and is what gets
frozen and resubmitted; a request that already has a row is not uploaded again.
A row this call just wrote is frozen from the value its upload read back, with
no further read. Storage that is unconfigured or failing raises
`PayloadUnavailable` before any row is written or anything is submitted, so the
task takes the payload recovery path below with nothing paid. An object whose
insert lost the conflict, or whose batch failed on a later upload, stays
unreferenced; it is content-addressed, so a retry reuses it.

**`classify_job_profiles` requests are the one exception, kept inline in both
directions:** the write path stores them inline and the backfill's eligibility
excludes them. Review gate admission (`review_gate.proven_profiles`) reads them
on its hot path, where a referenced request costs one S3 GET per candidate that
`lookup_timeout_ms` does not bound, and their volume is negligible: 0 rows and
0 bytes of the week's `batch_requests` (measured 2026-10-03, against
run_managed_board_batch 343 MB, run_filter_batch_chunk 175 MB and verify_new
140 MB). Re-measure before moving them.

After deploying compatible readers to the whole fleet, run bounded operations
with `python -m api.ai.migrate_snapshot_payloads MODE --limit COUNT`. Modes are
`copy`, `verify`, `compact`, and `restore`; resume with the reported
`--after TASK_ID CUSTOM_ID`. Each invocation processes at most COUNT snapshots,
in pages selected by `--chunk-size` (default 100). `--workers` is the number of
pages in flight (default 1); each page does its object I/O serially and commits
on its own, and the object client's connection pool is sized to match. Object
storage is bound by per-request latency, not bandwidth: on 2026-10-03 one PUT
took about 0.67 s, 16 concurrent PUTs reached about 11/s and 64 about 16/s.
Object I/O finishes before a short transaction locks tasks and requests in key
order and performs one conditional set-based update. Pages are reported in
cursor order, one line per page with its cursor. If an object fails or a source
changes or becomes ineligible, only the preceding ordered prefix of that page
commits and the cursor stops before it. Pages already in flight past it still
commit and are counted, but the cursor never passes an uncommitted row; a rerun
from it finds them already done. **Two pages sharing a task never run at
once:** each page locks its tasks rows, and on 2026-10-03 single tasks held up
to 37,329 rows, about 38 bundle pages, whose concurrent pages queued on one
row past the 2 s `lock_timeout` and aborted the run. A page waits for every
earlier in-flight page that shares a task with it; other tasks run beside it.
A task with more pages than `--workers` therefore runs its pages one at a
time. A lock or statement timeout in a page's locked transaction is retried
twice (`LOCK_RETRY_DELAYS`); if it persists, that page commits nothing, the
run prints `{"error": "lock_timeout", "at": [TASK_ID, CUSTOM_ID]}`, counts
`lock_timeout`, stops the cursor before the page and exits nonzero, like any
other failure. Later successful uploads remain unreferenced
until retry. Compaction requires `--backup-complete`, confirmation that
the independent database copy has finished. Start with copy and verification,
then a bounded compaction canary. Keep the emitted counters and cursor. An
unavailable, changed or ineligible outcome exits unsuccessfully before advancing
past its row; investigate or restore the source/object and retry that cursor. Other database errors fail the invocation; rerun
from the last saved cursor, since completed operations are idempotent.

For a fixed historical population, use `--manifest-stdin` instead of a database
scan. Supply a JSON array of sorted, unique objects with `task_id`, `custom_id`,
and `snapshot_sha256`. The digest is SHA-256 of `encode_payload(snapshot)`, not
PostgreSQL's JSONB text representation. Optional `metadata_md5` binds
`md5((to_jsonb(batch_requests)-'snapshot'-'snapshot_ref')::text)`; optional
`reference` binds the exact reference obtained after copy. Keep the same original
payload digest across copy, verification, compaction and restore. Bound each
input batch by both `--limit` and `--chunk-size`; `--after` is rejected in this mode.
The manifest service also requires backup confirmation for compaction.

Manifest mode never selects a replacement for a missing or ineligible identity.
It prints one result per invocation: counts and logical bytes for that invocation,
`completed` for the successful prefix, `after` for its last identity, and `failed`
for the first rejected identity. `exhausted=true` means every identity in the
supplied batch completed, not that the database or an external full manifest is
exhausted. Resume with the uncompleted suffix of the same evidence manifest.
Persist results before sending another batch. If the process ends before emitting
its result, replay the batch; committed rows are checked idempotently. Keep
independent verification, a fresh preservation audit, and a separate final audit
as phase gates. The legacy scan mode retains cumulative chunk counters and its
repeated final summary; do not combine that accounting with manifest deltas.

**A historical snapshot can be a member of a bundle: one object holding many
snapshots of one task.** One object per row is bound by object storage write
latency, not bytes: on 2026-10-03 Garage (data on CIFS with `data_fsync=true`)
took about 0.67 s per PUT and reached about 16 PUTs/s at 64 in parallel, so
1,081,787 remaining rows were about 20 hours of copying and 2 more of
compaction GETs. Their reference is version 3:
`{bucket, key, sha256, size, version: 3, member, member_sha256, member_size}`.
`key`, `sha256` and `size` describe the bundle, which is `encode_payload` of a
JSON object mapping each `custom_id` to its snapshot, stored at
`payloads/v3/sha256/<sha256>.json`. `member` is the row's `custom_id`;
`member_sha256` and `member_size` are of `encode_payload(snapshot)`, the same
digest a manifest binds. A reader checks the bundle's size and digest, then the
member's, and refuses a member named for another request.
`payload_objects.parse_ref` reads every version; `PayloadRef.parse` still
accepts only 1 and 2, so a reader that predates bundles fails closed with
`PayloadUnavailable` instead of reading a bundle as a snapshot.

Every reader of `snapshot_ref` goes through `request_snapshots.resolve` or
`request_snapshots.load`. A caller resolving many rows passes one
`BundleCache` for that call, so each bundle is read once:
`batch_results.snapshot_specs`, `batch_results.unconsumed`,
`payload_recovery.retry`, and `snapshot_payloads.migrate_many`, which reads
every bundle a chunk references before verifying, compacting or restoring its
members. The cache lives for one call and is never filled inside a transaction.
Rows with a version 2 reference are never rewritten as members.

**Rollout order for bundles.** Deploy the readers to every API and worker
before any member reference is written; the bundle backfill is a separate,
later release. Rolling back past the readers requires restoring every
member-referenced row first (`restore` writes the inline value back and clears
the reference).

`migrate_snapshot_payloads bundle --limit COUNT` is the historical backfill.
It takes the same eligible rows as `copy` that hold no reference yet, one task
per page of at most `--chunk-size` rows (default 1,000 in this mode), and
uploads each page as one bundle, split further only past `BUNDLE_MAX_BYTES`.
Then one short locked transaction rechecks eligibility and each row's exact
inline value and attaches the member reference, keeping inline. Rows with a
version 2 reference are not candidates. A row that changed after its upload is
skipped and counted `changed`: the run continues, exits nonzero, and the row
stays inline-only for a later run. An upload or invalid snapshot is
`unavailable` and stops before its row, like `copy`. `--manifest-stdin` does
not take `bundle`; manifests verify, compact and restore member rows with the
member's digest. `compact`, `verify` and `restore` handle member rows as they
handle version 2, reading each bundle once per chunk, so a compaction chunk the
size of a bundle page GETs about one bundle.

1. Deploy the bundle readers to every API and worker.
2. `bundle --workers W` from the start; `verify` the same range. A nonzero exit with only
   `changed` counts means rerun `bundle` from the start.
3. Confirm the independent backup, compact a bounded canary, `verify` it, then
   compact the rest with `--chunk-size 1000 --workers W`. Overlapping pages,
   not a larger chunk, is what reaches Garage's concurrency; 16 to 64 pages
   in flight hold that many pages of rows in memory.

Eligibility is completed non-profile tasks (the exception above) with no unconsumed receipts. The
migration locks the task and request and rechecks eligibility and exact source
values after verified object I/O. It retains task/request identities and all
receipt outcomes/accounting. No age cutoff or object expiry is implied.
`restore` requires the verified object and reverses eligible inline removal.
Logical bytes moved are not a measurement of filesystem space reclaimed.

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
from terminalizing a parent after its child was recovered. Do not deploy this
lifecycle change without coordinating with the fleet deployment owner.

## Task job lists: managed-board runs and filter chunks

**Every reader of a task's job list goes through `task_jobs.run_jobs`, which
reads both shapes:** inline `jobs`, and `jobs_ref` with `candidate_count`, a
verified object. Two populations carry one (`task_jobs.POPULATIONS`):

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
appends, the review gate plan, `finish`) and every whole-payload read (the
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
again. `managed_board_runs.admit` plans without holding a lock, uploads with
`PayloadStore.put_verified` outside any transaction, then locks the board and
checks again what a concurrent writer can change (the board row, an active
run, the sponsor's reservations) before inserting. Storage that is
unconfigured or failing is the refusal `STORAGE_UNAVAILABLE` (503 from the
run route): no task is queued and nothing is paid, and the scheduler tries
again next cycle. Objects are content-addressed and never deleted; candidate
lists rarely repeat across runs, so each run adds its list to the bucket.

**A filter run writes each batch chunk's list the same way.** `_run_filters`
uploads every batch unit's list with `put_verified` outside any transaction,
concurrently up to `payload_objects.MAX_CONNECTIONS`, before it enqueues the
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

**Legacy inline lists move to exactly the shape the writer produces; a list
is never deleted.** The end state is zero tasks of either population with
inline `jobs`. **Roll the `run_jobs` and `CHUNK_URLS` readers across every API
and worker before converting a filter chunk**: an older worker reads
`payload["jobs"]` and fails the chunk. `externalize` converts every inline
task whatever its status, in-flight ones included: the handler holds the list
it already read, and a resume or recovery reads the row again through
`run_jobs`. Per task it uploads the exact inline list with `put_verified`
outside any transaction, then in one short transaction locks the row,
requires the identical list (same canonical bytes) and no reference keys
beside it, and replaces `jobs` with `jobs_ref`, `candidate_count` and, for
filter chunks, `urls`, in one UPDATE. Every other payload key is left as it
was. A list that changed in between writes nothing and reports `changed`.
Nothing runs this automatically. Choose a fixed high-water id with
`SELECT max(id) FROM tasks`, then, with `POPULATION` one of `managed_board`
or `filter_chunk`:

```
python -m api.migrate_task_jobs POPULATION count --through ID --after 0 --limit 20
python -m api.migrate_task_jobs POPULATION externalize --through ID --after 0 --limit 20 --workers 4
python -m api.migrate_task_jobs POPULATION verify --through ID --after 0 --limit 20
```

Each invocation examines at most `--limit` tasks in id order, so it
decompresses at most that many legacy payloads. Repeat from the printed
`after` until `exhausted` is true. `count` and `verify` run read-only: `count`
classifies tasks as `inline`, `referenced`, `missing` or `conflict` without
reading objects, and `verify` reads every reference through `run_jobs`, so a
missing object, a length that disagrees with `candidate_count` or URLs that
disagree with `urls` is `unavailable`. A writing invocation stops its cursor
before the first `changed` or `unavailable` task; resume from the same
`after`, and tasks already converted report `referenced`. `missing` and
`conflict` (a filter chunk reference without `urls`, a managed one with them,
an inline list beside reference keys) are not shapes any writer produced;
they are listed by id under `failed` (exit status 1) for a person to look at,
and the cursor moves past them. Done means `count` over the whole range
reports no `inline` and `verify` no `failed`.

`restore` is the rollback: it reads each reference through `run_jobs`, then
under the row lock puts the exact list back as `jobs` and removes `jobs_ref`,
`candidate_count` and `urls`, returning a legacy payload to exactly what it
was. Readers older than `run_jobs` need it on every task, including those
written with a reference. Objects are never deleted, so a restore is always
possible.

## Shared query instruction text

`core.query_instructions` stores exact instruction strings separately from
verdict `prompt_hash` and the prompt-reporting catalog. NULL and empty strings
remain distinct. New writes store only a shared reference; cached page content
and accounting columns are unchanged. Deploy the compatible-reader release
throughout the fleet before enabling these writes. Historical inline rows remain
readable and require the separate bounded migration below.
Query detail and custom-result readers hydrate a missing inline value and fail
explicitly if referenced content is missing or its digest is wrong.

The custom-verdict cache check (`store.has_custom_result`) answers whether a
decided verdict exists and returns nothing else, so it selects only the latest
row's instruction reference. It never reads the page text or inline
instructions: the filter sweeps call it once per candidate, 1.32M times in 36
hours (2026-10-03). A row held by reference is still hydrated, so a missing or
corrupt dictionary entry fails the check as it fails every other reader. A
caller that needs the verdict row's fields reads them in its own query.

Use `python -m api.ai.migrate_query_instructions MODE --through ID --after ID
--limit N` with a fixed maximum query ID and saved cursors. `copy` retains inline
text; a separate `verify` pass reports unreferenced values. Only after an
independent backup and fleet-wide compatible readers, `compact` with
`--backup-complete --readers-compatible` removes verified inline duplicates.
`restore` fills inline values again and keeps references. Each bounded chunk is
atomic, locks result rows and referenced dictionary content in ID order for
writes, and leaves the cursor before failed work. The service also enforces the
backup and compatible-reader confirmations, so direct calls cannot bypass the
CLI checks. Keep dictionary rows permanently while referenced. Before reverting to
old readers, restore and verify inline text. Logical bytes removed do not prove
that PostgreSQL relation files or filesystem use decreased.

Grouped receipt verification and compaction select a materialized page of cheap eligible
receipt keys before accessing any response JSON. The metadata size query therefore
detoasts at most that key page, including rows subsequently skipped for missing
references (or missing inline vectors in compact mode). `--scan-limit` bounds metadata
keys inspected per invocation, including repeated probes after byte-budget splits;
its default is `--limit` times the group size. This is independent of the successful
receipt limit and the serialized-byte reservation.

Grouped JSON summaries include `scanned`, `skipped`, `verified_after`, and `exhausted`.
`after` is the safe restart cursor through successful receipts and explicitly skipped
keys; `verified_after` is the last receipt verified or compacted in that invocation.
An all-skipped page is not end of input. Runners must continue from `after` until
`exhausted` is true, never infer completion from successful count below `--limit`.
Exhaustion is reported only after a fully processed short or empty key page, with no
blocked candidate. A scan or successful-count bound can finish with `exhausted=false`;
a bad reference or oversized candidate stops before that key and reports a failure.
