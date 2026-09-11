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

**The other ATSs are data, not code.** `extension/ats/<ATS>.js` (one file per ATS, generated by
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
on every page to detect them is a separate decision. `extension/engine.js`
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
hand-written readers, which own their hosts; the engine steps back when
one is present. The Greenhouse reader also reads the employer's own
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
extension that made it (content.js `BUILD`); a capture without today's
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
proxy. One reader per ATS under `extension/readers/`: Ashby reads the
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

## Scheduled filter transport

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
