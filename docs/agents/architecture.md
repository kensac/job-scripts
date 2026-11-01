# Architecture and domain semantics

The pipeline, the matcher, and the facts every other topic reads. Related:
[sources-and-boards.md](sources-and-boards.md) for ingest and the catalog,
[visibility.md](visibility.md) for who sees what,
[observability.md](observability.md) for the worker fleet and batched work,
and [reading-production.md](reading-production.md) for reads from outside.

## Assisted application boundaries

The extension reads fields; the server resolves them. `api/apply/fields.py`
defines the field read and resolved field shapes. `api/apply/profile.py` owns a
person's application facts and values derived from them. `api/apply/rules.py`
maps labels to facts, `api/apply/options.py` selects an offered choice, and
`api/apply/resolver.py` applies the answer bank, profile and draft ladder in
that order. A field with no supported value remains blank. Keep those stages
separate so a new label rule cannot change how choices are selected or how a
saved answer takes precedence.

The extension route entry point is `api/routers/apply.py`. It owns fill tracking,
resolution and submission, including the transaction that records the final
fields, answer-bank edits and application link. `api/routers/apply_routes/`
holds public extension configuration, profile and answer reads, suggestions,
and reports by their respective responsibilities. Route models live in
`apply_routes/models.py`, and extension telemetry is recorded through
`apply_routes/events.py`. A route split preserves the published paths,
request and response schemas, owner checks, and submission transaction.

## The mail pipeline

Import → classify → match → derive.

**Gmail sync must revisit the whole mailbox.** The recent search reads a
bounded number of provider pages. `tasks.mail.sync` also keeps unfinished
search ranges and pending message IDs in `gmail_scan_cursors`. A range with
another provider page splits into smaller date ranges. Do not save Gmail page
tokens across cycles. Store every fetched message before removing its ID from
the pending list. A failed fetch retries that ID, and a completed sweep starts
again so mail added to an earlier range is found later. If more than one page
falls in a single second, fail visibly instead of marking the sweep complete.
The per-cycle work budgets live in `app_config`; they never discard mail.

- `email_messages`: the message as it arrived. Body text and body HTML are
  both retained; the text is derived from the HTML, so the HTML must be stored
  before anything re-derives the text. `provider_message_id` is the
  Message-ID as the header spells it, angle brackets included, whatever the
  source, because it is how one message imported from two archives dedupes.
  The .olm export drops the brackets and the importer puts them back.
- `email_events`: append-only. Latest row per message wins on read.
- `application_matches`: append-only. Latest row per message wins on read.
- `applications`: `job_id` is nullable. **Never synthesise a job row from an
  email.** Mail predating the catalog is the normal case. It is the one
  record that a person applied, written where the act is recorded and not by
  a sweep (`api/mail/applications.py`). It has three provenances: `tracker`,
  written by the board write that moves a row into an applied status;
  `apply`, written by an extension submit whose posting is not on the board
  (the fill's `application_id` names it, and a submit on a board posting
  names that posting's application); and `email`, written by the matcher. A
  posting has at most one application per person (a unique index). The
  board's applied day is that application's `applied_at` as a UTC date
  (`applications.applied_on`); a board patch that sets or clears
  `date_applied` writes the application, and a row moving into an applied
  status dates it once. No board row stores its own copy.
- Asks ("schedule the interview", "respond to the offer") and status
  proposals are derived when read (`mail.pipeline.action_items`,
  `proposals_for`) from each message's current event on its current
  application; an ask is closed by a later event that settles it. Their ids
  are the asking event's. Only what a person answered is stored, in
  `event_answers`: append-only, one row per answer, so a reopen is an answer
  and the closing one stays readable. No sweep keeps them in step.

**Stage is derived at read time from the event stream and never stored.**
Terminal outcomes beat progress regardless of arrival order. Withdrawal comes
from the board, not from mail: no employer writes to say you withdrew.

**Keep each mail derivation with its owner.** `api.mail.pipeline.events` reads
current events and matches and derives stage; `proposals` derives board
status suggestions and records a person's answer; `actions` derives
asks and reads the append-only answer log; `senders` reads sender
evidence. `api.mail.pipeline` is the stable import surface for their callers.
A split must preserve the same current-event and current-match precedence,
user scope, and event answer writes. Sender signals only suggest review and
never filter an application. Automatic mail classification remains paused.

**Match decisions keep method and attachment state together.**
`api.mail.match_decision.MatchDecision` owns the persisted match methods and
rejects a null application id with an attachment method, or an application id
with a no-attachment method. A null id with `manual` remains valid: an admin
can record that no application was found. The same value object compares
repeat machine outcomes without rationale; `api.mail.match.record` still owns
human precedence and the append with its pointer update. Preserve method
strings when confirming an existing decision, including a method from older
stored data.

**Each log has one writer, and it moves the message's pointer as it
appends.** `mail.events.append` is the only INSERT into `email_events` and
`mail.match.record` the only one into `application_matches`
(`tests/test_mail_pointers.py` fails on another). Each sets
`email_messages.current_event_id` or `current_match_id` in the same statement,
to `GREATEST(pointer, new id)` so two appends committing out of order still
end on the newest. `backfill_mail_pointers` fills messages the writers never
saw and runs once a cycle; the `mail_pointer_stale` health alert fires when a
pointer lags after a backfill has finished, which means something appended
another way. A rule that writes an event with no model names itself in
`model` (`rule:self_sent`), because no model and no person is a bug.

**The current event and match are read through the message's pointers.** A
statement holding the message row joins `email_events e ON e.id =
m.current_event_id` (or `application_matches` on `current_match_id`). One
that needs the current rows of many messages without the message takes its
subquery from `api/mail/current.py` (`current_event(...)`,
`current_match(...)`), naming the columns it reads; one message's current
match is `match.latest`. `tests/test_mail_current.py` fails on a "newest row
per message" (`DISTINCT ON (message_id)`) written anywhere else. Join on the pointer alone, not on the pointer
and the message id together: the planner multiplies the two and expects one
row (the module docstring has the production measurements).
`idx_email_messages_current_match` INCLUDEs the message columns those
whole-mailbox reads take (`current_event_id`, `sent_at`, `from_email`,
`subject`), so they are index-only. A message column a new one of them reads
goes into that list in the same change: otherwise each matched message is a
heap fetch of a row that carries the mail body, about 5 s cold. Test fixtures
that write a log directly go through `tests/mail_log.py`, which moves the
pointer the way the writers do.

**The mail matching task keeps its stages in order.**
`tasks.mail.matching.handler` registers the handler and runs detachment, derived
date-floor repair, pending message matching, then mail-derived application
creation. `tasks.mail.matching.sweep` owns pending decisions and detachment;
`tasks.mail.matching.derived` owns application creation and date floors. Do not derive before matching: an extra application
at the same employer makes the matcher's company tier ambiguous. Creation
keeps its conditional insert against the anchor message's current match, so
two workers cannot both claim the same evidence. Keep current event and match
queries person-scoped, and do not activate classification from this task.

**A matcher sweep starts from what changed since the last finished sweep.**
`api/mail/match_sweeps.last_sweep_start` is the cutoff, re-exported by
`api.mail.match`: the start of the newest
finished `match_mail` that covered the user and had no `limit`. The worker
enqueues a sweep only when `changed_since` finds a new application, event or
match, or a board row moved into an applied status. A sweep skips a user with
none of these, decides again only messages whose verdict or the cutoff is
older than a new application or event. The verdict timestamp alone is not a
cutoff, because `match.record` does not append a verdict that repeats the
standing one.

**A request reads the current event and current match once.** "Latest row per
message" is a pass over every row of both tables, so a surface that needs it
for several derivations takes it in one statement and derives the rest from
those rows, as the resolve queue does (`resolve/queue_items.current_rows`).
Where such a derivation restates one that `api/mail/pipeline/` also serves
(`events_by_application`, `proposals_for`), a test holds the two equal. A
surface that ranks before it pages reads only what ranking needs for every row
and reads the shown columns for the page alone.
The resolve queue keeps current-row selection and ranking in `resolve/queue_items.py`,
shared row shapes in `resolve/queue_types.py`, and page-only detail reads and
response construction in `resolve/queue_item_rendering.py`. Preserve that
selection-before-page-detail boundary and the response shape when changing a
queue item.

## Matching

Tiers run in order and each may decline. **A tier never guesses.** Two
plausible candidates is a refusal, not a coin flip. The refusal is recorded
and a person resolves it.

An employer cannot reply before you applied, so a date lower bound is valid,
but only where the date means what it says. A date the system recorded when a
row was created is an upper bound on when the person applied, not a lower one.
A date derived from a subset of the evidence must not then veto the rest of it.

**Never derive a mail domain from a posting domain.** Applicant tracking
systems post on one domain and send from another.

**A verdict, once recorded, must remain reconsiderable.** Selection predicates
that exclude anything already decided freeze the answer against a smaller world
than exists now.

**The matcher never overturns a person, and the check belongs at the write.**
A human verdict stays reconsiderable by a human, from the queue that exists to
reconsider it. What it must not be is undone by the next sweep, because then
rejecting a match achieves nothing. A selection predicate cannot enforce this:
the sweep reads its candidates, then writes minutes later, and the decision can
land in between.

**Every write to `application_matches` goes through `api.mail.match.record`**,
so `actor_user_id` cannot be omitted. Older rows reading `manual` are not
evidence a person was there.

## Providers and cost

Provider facts live in one datasheet per provider, declared rather than
inferred. Rates, batch eligibility, structured-output mode and accepted
reasoning values are per **model**, not per provider.

`None` means nobody looked it up. Never zero, never "same as the other one".

**Cost is priced once, at call time, in one place.** Prices change, so deriving
cost on read rewrites history. There is one spend ledger, grouped by purpose.

Where two totals answer different questions, report both and label them. Never
present one as the other.

**Extraction is paid for only where its result can be read, and not at all
where nothing reads it.** Requirements extraction is off
(`requirements_extraction_enabled`, persisted config, seeded false): its one
consumer is the market table, and on 2026-09-07 the deployed arm (nano,
minimal) named a seniority for 5 of the 92 postings the reference named one
for. Turning it on is a config row; choosing an arm worth paying for is an
experiment. Comp and requirements select on
`core.verdict_reads.verified_open`: the posting's latest closed and clearance
verdicts both passed. On 2026-09-06 that was 37,438 of 74,477 active
postings; the rest is closed or restricted and reaches no board.

**An arm that answers nothing is re-bought every sweep.** Comp leaves a row
unextracted when the answer does not parse, so the next sweep selects it
again: the right retry for a transient failure, a spend loop for a systematic
one. On 2026-09-07 nano at medium or high effort spent the whole output cap
on reasoning and returned no text on verify, comp and requirements (65 to 100
of 100). Try an effort in an experiment, where a failed arm costs one batch,
not on a production step, where it costs one batch per hour.

**Verification has three owners.** `tasks/verification/questions.py` builds the
request and hashes its exact submitted shape.
`tasks/verification/reverification.py` refreshes existing verdicts from current
pages and compares that question identity before reusing an answer.
The new-posting sweep selects uncleared postings
(`tasks/verification/selection.py`), reuses verified near copies
(`tasks/verification/near_copies.py`), and records new closed, clearance and
board verdicts (`tasks/verification/new_postings.py`). Keep candidate selection and result settlement
with their respective sweep; only request construction is shared. The task
registry still maps the same three handler kinds through `tasks/verify.py`.

**The same question is not bought twice.** A verification verdict records
`request_sha256`, the hash of everything that could change its answer: model,
effort, schema, instructions and the exact page text (`tasks/verification/questions.py`,
`question_sha256`, hashed from the request as submitted). Re-verification
re-fetches the page and, where the latest closed and clearance verdicts both
answered the identical question, writes the standing answer again under
`config_name = 'reverify-unchanged'` with no model and no tokens, the shape
`record_manual` uses for a verdict no call produced, so spend and call counts
stay true. NULL never matches: legacy and manual verdicts are asked again,
and that answer carries the hash forward. A forced sweep always pays. Measured
for the week to 2026-10-04: 5,780 of 7,244 re-checks sent byte-identical text
to the same model, and 5,776 got back the answer already held. A new purpose
that re-asks on a timer gets the same treatment rather than a second
mechanism.

**A provider out of credit moves work down the purpose's fallback chain, and
nothing else moves it.** `core.fallback.exhaustion_reason` reads an error
against the vendor's declared `Provider.exhaustion_markers`: out of credit, a
billing hard limit, a quota used up. A rate limit, a 5xx or a timeout keeps its
own retry. A vendor with no recorded shape (xAI) has no markers, so nothing it
returns is exhaustion. `app_config.model_fallback_chains` lists, per purpose
(`core.fallback.chain_purposes`: every `SHAPES` task but `job_profile`, whose
model its stored profiles pin, the personal `filter` and `managed_board`), the
models to try in order; it is empty by default, which is the old behavior
exactly. Routing (`api.model_fallback.route`) skips an exhausted provider and
takes the first chain entry that `resolve()` admits for the task's shape
(batch endpoint, structured mode, effort, price) and that this host holds a
key for. **A purpose whose verdicts are read through `answering_models`
falls back only inside the primary's `verdict_model_equivalence` group**:
`filter` and `managed_board` in the `custom` kind, `verify` (and so
re-verification) in the `verify` kind. Any other model's verdicts would be
paid for and counted by no board, filter or reuse check. A purpose that writes
no verdicts (extractions, classifications) may use any chain entry; its rows
mix by model by design. The filter and a managed board choose before they
select jobs, so the decided-postings check asks for the model that will
answer. A board's fallback keeps `no_cache` while
`managed_board_cache_writes_enabled` is off, on either vendor's transport.
Every row names the model that ran: the batch row's `model`, and
`ai_batches.fallback_from` for the model it replaced; the ledger, verdicts and
extractions take the batch row's model. Spend stays inside the same caps:
a fleet submission is priced at the fallback's rate against the weekly
ceiling, and a person's weekly token budget is model-blind. Live calls on the
server's key mark the provider and fail; they do not switch models mid-call
(28 owner-key live calls in the 30 days to 2026-11-01, against 805,000
batched).

**A verdict counts for the model that gave it, unless two models are grouped.**
Every reuse check (filter and board candidates, board membership, near-copy
reuse, reverify's unchanged page) asks for the model it will call, so a model
switch re-pays for the corpus. `app_config.verdict_model_equivalence`
(`core/verdict_equivalence.py`) lists groups of models whose answers count as
each other's, per kind: `custom` for filter and board verdicts, `verify` for
closed and clearance. Readers go through `api.verdict_equivalence.answering_models`
and match `model = ANY(...)`; reverify accepts an answer to the same question
asked of a grouped model. It changes reads only: each verdict still records the
model that answered, and clearing a group makes the next runs ask again. Add a
group only after measuring that the two models agree about as often as one
model agrees with itself across two runs, on the same kind of check. A new
reuse check takes its model list from `answering_models`, never `model = %s`.

**A re-listing re-asks a closure; it is not a first read.** The sweep's
re-listing branch takes only postings that already hold a closed verdict. A
re-listed posting never checked is `verify_new`'s, because only its gate knows
whether any board or filter can keep it.

## Time

Containers run on a local timezone by deliberate convention; hosts and the
database are UTC. **Store aware UTC, never naive local time.** A timezone-less
date is uncertain by exactly one day, and any comparison against it should
carry that width rather than inventing an hour by casting.

## Named places

These exist in exactly one place each. Change them there, and never write a
fresh copy:

- **Job visibility**: `api/board/visibility.py` (`FULL` and `FAST`). See
  [visibility.md](visibility.md).
- **AI pricing**: `core/pricing.py`, rendered as both Python and SQL from one
  source with a parity test.
- **Provider facts**: one datasheet per provider under `core/providers/`.
- **Task handlers**: `src/tasks/`, one module per family. The task runtime
  imports nothing from the worker. The worker imports the handler table, the
  runtime and `tasks.DERIVATIONS`, and nothing else from `tasks` (the
  `ignore_imports` in `pyproject.toml`).
- **Person-state writes**: `api/board/person_state.py`. HTTP adapters may
  authorize and shape a command, but the status/date rules, history and board
  event publication have one service owner.
- **Experiment semantics**: `api/experiments.py`. Its explicit, immutable
  `ExperimentStep` declarations own request construction, comparison
  projection and deployed-result loading; `api.run_experiment` is its only
  caller. Experiments are opt-in; this is not a universal derivation registry.
- **Structured batch transport**: `core/batch/__init__.py` is the public entry point;
  `core/batch/specs.py` derives the response-model name and strict JSON schema
  together through `structured_response_spec`. Transport, collection and
  progress have separate owners in `core/batch/transport.py`,
  `core/batch/collection.py` and `core/batch/progress.py`.
  Prompts, inputs, context, output limits, model choice and persistence remain
  owned by their domain or task.
- **Listing formats**: `core/fetching/boards/formats.py`, one format object with its URL
  matcher, fetcher and source rules, chosen by the listings URL. See [sources-and-boards.md](sources-and-boards.md).
- **What the mail implies the board should say**: `api.mail.pipeline.proposals_for`
  and `answer_proposal`. The route that lists proposals and the queue that
  merges them into everything else read the same function.

## Compensation source provenance

`compensation_demand_gate_enabled` narrows new compensation work to current
enabled personal-filter passes within their criteria, published managed-board
members, and personally tracked or uploaded postings. The selector lives in
`api/compensation_candidates.py`; an empty machine-created `user_jobs` row is
not personal demand. Verification and content-generation guards still apply.
The hourly sweep discovers newly selected jobs without a backfill. Already
submitted batch results are consumed even if demand disappears meanwhile.

`job_comp.content_row_id` records the exact cached content used by a successful
compensation extraction, and `job_comp.content_hash` the text it read, so an
unchanged re-fetch is re-stamped rather than paid for again. Writes verify that
source in the insert statement;
subsequent content changes make known older generations eligible again. A null
source is unknown legacy provenance, not proof that the compensation is current.
It does not itself trigger extraction, and must not be backfilled by guessing
from timestamps. Existing unextracted and missing-period repair rules still apply.

## Provider-reported costs

`GET /admin/spend/provider` reads the provider cost service automatically using
`OPENAI_ADMIN_KEY`. Keep that secret server-side. `OPENAI_BILLING_PROJECT_IDS`
optionally scopes costs to comma-separated projects; otherwise costs cover the
organization, not necessarily this application. Completed UTC days are reported
separately from ledger estimates. Missing configuration or failed reads return
unavailable costs, never a zero bill. Provider data can arrive late or change;
do not overwrite historical usage estimates or allocate organization costs to
users without attribution evidence.

## Provider balances

`GET /admin/spend/balance` reports, for OpenAI and Anthropic, the spend each
provider reports for completed UTC days (yesterday, last 7 and 30 days, month
to date, by model) and the money left. Anthropic is read from the legacy
`/v1/organizations/cost_report` with `ANTHROPIC_ADMIN_KEY`, because its
successor is a beta whose data lags up to a day. Both admin keys live only in
the environment of the api and of every worker that may run the health task.

**Money left is never provider-reported, because neither provider reports a
prepaid balance or a credit's expiry (checked 2026-11-01). It is derived, and
every derived figure says so in its `derivation` and its label.** Two bases:

- `prepaid`: an admin-entered grant in `provider_prepaid_credits` minus the
  provider-reported spend from its `granted_on` day. The expiry is the admin's
  too. Do not substitute ledger estimates for the provider's spend.
- `spend_limit`: OpenAI's `GET /v1/organization/spend_limit` threshold minus
  provider-reported spend this UTC month. Not derived when costs are
  project-scoped, because the limit is the organization's.

A basis that applies but cannot be derived carries `unavailable` and no
amount, never 0. Today's partial day is in no figure.

`_detect_openai_money` and `_detect_anthropic_money` open
`<provider>_balance_low` when a headroom is at or under
`provider_balance_alert_usd`, lasts fewer than `provider_runway_alert_days` at
the last seven completed days' rate, or a grant expires sooner. A spend-limit
headroom that outlasts the month's reset is not an alert. Under a day of runway
is critical, because a warning waits for the daily digest. A configured
provider whose read fails raises, so its open alerts stay open; one section
per provider, so one unreadable provider never hides the other. The route
caches provider reads like `/admin/spend/provider` and never caches a failed
read; the detector reads fresh.

## Person pipeline route ownership

`api.routers.mail.pipeline` registers the person's pipeline views before the
application ID actions. `pipeline_rows` owns person-scoped application reads,
match tiers, and derived stages. `pipeline_views` owns summary, list, and
detail responses. `pipeline_actions` owns match corrections and application
dismissal or restoration. **Keep every action scoped to an application owned
by the caller, and bind a match ID to that application before appending a
correction.** A match ID alone never grants access to its message. Stage stays
derived from current mail events; corrections append to the history instead
of rewriting earlier decisions. These routes do not activate automatic mail
classification.

## Administrative mail debug ownership

`api.routers.mail.debug` assembles the routes in registration order. Register
the consistency and analytics paths before the message ID path, or the ID path
will intercept their names. `debug_inspection` owns filtered current-verdict
lists and complete message histories. `debug_analytics` owns read-only
distributions and their stated populations. `debug_actions` owns explicit
administrator corrections, which append verdicts through the shared writers.
`debug_models` declares the response and correction shapes. Keep classification
activation separate from these inspection and correction routes; the paused
classification switch must stay paused until the product changes it.
