# Architecture and domain semantics

## The mail pipeline

Import → classify → match → derive.

- `email_messages` — the message as it arrived. Body text and body HTML are
  both retained; the text is derived from the HTML, so the HTML must be stored
  before anything re-derives the text.
- `email_events` — append-only. Latest row per message wins on read.
- `application_matches` — append-only. Latest row per message wins on read.
- `applications` — `job_id` is nullable. **Never synthesise a job row from an
  email.** Mail predating the catalog is the normal case.
- `action_items` — derived from events, resolvable by a person.

**Stage is derived at read time from the event stream and never stored.**
Terminal outcomes beat progress regardless of arrival order. Withdrawal comes
from the board, not from mail: no employer writes to say you withdrew.

## Matching

Tiers run in order and each may decline. **A tier never guesses.** Two
plausible candidates is a refusal, not a coin flip — the refusal is recorded
and a person resolves it.

An employer cannot reply before you applied, so a date lower bound is valid —
but only where the date means what it says. A date the system recorded when a
row was created is an upper bound on when the person applied, not a lower one.
A date derived from a subset of the evidence must not then veto the rest of it.

**Never derive a mail domain from a posting domain.** Applicant tracking
systems post on one domain and send from another.

**A verdict, once recorded, must remain reconsiderable.** Selection predicates
that exclude anything already decided freeze the answer against a smaller world
than exists now.

**The matcher never overturns a person, and the check belongs at the write.** A
human verdict stays reconsiderable by a human, from the queue that exists to
reconsider it; what it must not be is undone by the next sweep, because then
rejecting a match achieves nothing. A selection predicate cannot enforce this:
the sweep reads its candidates, then writes minutes later, and the decision can
land in between. That is not hypothetical - the only human decision ever
recorded in production was overwritten exactly that way, an hour after it was
made.

**Every write to `application_matches` goes through `mail_match.record`**, so
`actor_user_id` cannot be omitted. Three endpoints wrote the table directly and
none of them set it, which is why rows reading `manual` are not evidence a
person was there, and why "has anyone reviewed this attachment" was
unanswerable by query for 15,090 rows.

## Providers and cost

Provider facts live in one datasheet per provider, declared rather than
inferred. Rates, batch eligibility, structured-output mode and accepted
reasoning values are per **model**, not per provider.

`None` means nobody looked it up. Never zero, never "same as the other one".

**Cost is priced once, at call time, in one place.** Prices change, so deriving
cost on read rewrites history. There is one spend ledger, grouped by purpose.

Where two totals answer different questions, report both and label them. Never
present one as the other.

## Batched work

Scheduled work batches at half price and parks rather than holding a worker.
A human waiting is the only reason to call a model synchronously.

**A batch is submitted whole and fails whole.** All requests failing means the
submission was rejected on grounds that applied to every one of them; some
failing means bad inputs. Different causes, and only the first is certainly a
defect.

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
request sets when any of it can be read. Size a batch knowing that. The poll
resumes a task once some of its batches are terminal and the rest have run
past `batch_straggler_hours` (persisted config); the resumed handler goes
through `collect_pending`, which takes what landed and rewrites the payload
to the ids still running, and the worker parks a handler that returns with
ids left rather than finishing it. A handler must therefore be safe to run
again from the top with a subset of its results, which every batched sweep
already is: they iterate the results they were given and re-select on the
next run.

**A parked chunk must not hold what its siblings decided, nor the next run.**
Each filter chunk materializes its own passes when it finishes, and a split
run does not block the next cycle's run: the splitter excludes every url a
live chunk still holds (`board._in_flight_urls`), so the new run judges only
what arrived since. Only a run that has not split yet blocks another.

Dry-run a handful of live calls before committing to a large batch. A batch
fails whole, and the dry run also measures real token counts.

## Observability

Three layers, each answering a different question, none standing in for
another.

- **Metrics** (`api/metrics.py`, Prometheus) answer "how much, how fast":
  counters and gauges, no identity.
- **Conditions** (`api/health.py`, `health_alerts`) answer "is something
  wrong": app-aware detectors comparing a window against a baseline, opening
  and resolving alerts, mailing once. A new detector is written when a
  pattern emerges, never one per traceback.
- **Errors, events, logs and traces** (`api/telemetry.py`, PostHog, one
  project end to end with the frontend) answer "what failed, where, on which
  release, inside which request or task": every unhandled exception in a
  request handler or a worker's handler; a queryable event wherever the
  service swallows or retries a failure that would otherwise leave no trace
  (`task_failed`, `task_requeued`, `tasks_reaped`, `tasks_lost`,
  `ingest_pull_failed`, `fetch_failed`, `fetch_deferred`, `ai_call_failed`,
  `alert_opened`, `alert_resolved`, `worker_started`); every log record at
  INFO and above, through OpenTelemetry, uvicorn's request log included
  (its loggers do not propagate, so the handler is attached to them by
  name); and one span per HTTP request, per
  worker task and per outbound `requests` call (the board pulls and ATS
  resolvers), so a trace of the queue reads as tasks with their fetches
  underneath. Every record carries `service.name` (`jobtracker-api` or
  `jobtracker-worker`), `service.instance.id` (the worker's fleet name),
  `host`, and `release` (the image's commit, from the build arg); every
  exception and event carries the ids of the span it happened inside, so an
  error links to its request or task. PostHog is a generic OTLP receiver:
  the full `/i/v1/logs` and `/i/v1/traces` paths, bearer-authenticated with
  the same project key.

How much goes is a measurement, not a guess: everything ships first
(`POSTHOG_TRACE_SAMPLE=1.0`, `POSTHOG_LOG_LEVEL=INFO`), the daily volume is
read in PostHog, and the two knobs come down if the bill or the noise says
so. Errors and events are never sampled.

`telemetry` is a no-op without `POSTHOG_API_KEY`, so tests and a bare checkout
need no destination, and it says so once at startup; it never raises, and
counts what it could not send in `jobtracker_telemetry_failures_total` and every OTLP export attempt in `jobtracker_telemetry_exports_total{kind,result}`, so whether it is shipping is one query and zero failures is never mistaken for zero attempts. The
frontend captures its own exceptions and records upstream API failures on its
side; this layer is for failures inside the service that never surface as a
bad HTTP answer.

## The worker fleet

Tasks are claimed with row-level locking and skip-locked selection. A worker
heartbeats while it holds a task.

**A worker claims only kinds its own image has a handler for.** A roll goes
host by host, so for a minute an old image and a new one share the queue; a
kind the new image added must wait for a host that can run it rather than be
claimed and failed as unknown by one that cannot. That happened to the first
classify_locations task on 2026-09-05, in the seconds before the claiming
host's own deploy. The registry in `api/tasks/__init__.py` is what the worker
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

## Sources and boards

**A source is a row, never a code path.** The format is read off its
listings URL by `core/boards.py`; a new board in a known format is added on
the Sources page, and a new format is one fetcher returning the same
`JobPosting` as the rest. The row carries what ingest needs and nothing
derived: `company` (required where the system never names it), a
`title_pattern` that gates which titles enter the catalog, and an
`ingest_interval_hours`. `sources.active = false` stops both the scrape and
every AI check on that board's postings; a bundle (`source_groups`) or a
format is a way of selecting rows for that flag and the interval through
`POST /admin/sources/switch`, not a second layer of state.

**Everything a board returns is stored, once.** Every pull records every
listing in `listings`, kept by the pattern or not, with the posting text the
listing call carried (Greenhouse with `content=true`, Lever, Ashby; the text
is assembled by the same `core/ats.py` helpers the resolvers use, so it is
what a per-posting fetch would have returned) and the raw record minus that
text. Refreshed per pull and aged out by `screened_retention_days` after the
board stops listing it. A candidate pattern is judged against it
(`pattern-preview`) before it replaces the live one; a posting a wider
pattern admits arrives in `jobs` on the next pull; ingest stores the carried
text as the posting's content instead of fetching the page. Scraping is the
action that gets the fleet blocked, so a backtest or a backfill reads this
table rather than asking a board twice. Nothing downstream reads it.

**A company board's pull is the closure signal for its rows.** After a pull
from a board that lists every open posting (`boards.AUTHORITATIVE`), every
active catalog row of that source the pull did not admit is set inactive:
the board dropped it, or the pattern stopped admitting it. Inactive rows are
excluded from every sweep and leave boards through `_demote_closed`; listed
and admitted again, the upsert reactivates them. An aggregator list is not
such a signal, and an empty pull is a broken fetch rather than an empty
board, so neither retires anything.

**A posting page is fetched by the cheapest tier that plainly worked.** In
order: the ATS resolver (an API call), then, when `fetch_engine` is
`static_first`, a browserless fetch with a real Chrome fingerprint
(`fetching.fetch_static`) accepted only as an HTML 200 whose extracted text
clears `static_fetch_min_chars` and reads as a page, then the browser. A
tier that returns None costs nothing downstream; the browser is the
guaranteed floor. Measured on 2026-09-04 over 431 pages: the static tier
recovered one browser-served page in seven whole, and every JavaScript
shell fell under the gate. The content row's reason (`ats text`, `static`,
`scraped`) is how the share each tier serves is read, so the engine can be
switched in config and judged from the rows rather than assumed.

**A host that blocks bursts is drip-fed, not pulled off.** `fetch_host_limits`
(persisted config, host to page fetches per hour) paces the browser fetch
fleet-wide: `verdicts.host_paced` counts the hour's content rows for the host
and defers the fetch, writing nothing, so the next cycle tries again. The
fetch-failure alert names the host, and adding it to the map is the way to
resolve that alert; no host is written into code.

**A page fetch that returns nothing leaves a record.** It is a `content` row
with `status = 'failed'` and no text, and nothing retries that URL inside
`fetch_retry_after_hours`. Without the record the hourly cycle was the retry:
every dead link, every hour, from every worker, which was most of the fleet's
block rate.

**The scheduler queues one ingest per source, however far behind.** A pending
ingest blocks the next cycle's for that source; a running one does not. A
source on a longer interval than the cycle waits while its last successful or
in-flight pull is younger than the interval; a failed pull does not count.

**Every ingest leaves its counts on its task** (`fetched`, `kept`, `cached`,
`fetch_failed`, `gone`, `already_cached`, `skipped_recent_failure`). They are
the only record of what one pull saw, and they are what the board detectors
in `api/health.py` and the admin ingest summary read. A board that pulls fine
and delivers nothing is visible as exactly that.

The knobs above (`fetch_retry_after_hours`, `screened_retention_days`,
`queue_stall_minutes`, `ingest_backlog_cycles`) are `app_config` rows, not
constants; see engineering-standards.md.

## Time

Containers run on a local timezone by deliberate convention; hosts and the
database are UTC. **Store aware UTC, never naive local time.** A timezone-less
date is uncertain by exactly one day, and any comparison against it should
carry that width rather than inventing an hour by casting.

## Named places

These exist in exactly one place each. Change them there, and never write a
fresh copy:

- **Job visibility** — a read-time conjunctive predicate in `routers/jobs.py`,
  mirrored in the board task's materialise and candidate queries. Per-object
  routes format the same predicate. Change all of them together.
- **AI pricing** — `core/pricing.py`, rendered as both Python and SQL from one
  source with a parity test.
- **Provider facts** — one datasheet per provider under `core/providers/`.
- **Task handlers** — `api/tasks/`, one module per family. The task runtime
  imports nothing from the worker; the worker imports only the handler table.
- **Listing formats** — `core/boards.py`, one fetcher per board format,
  chosen by the listings URL. A source is a row, never a code path: a new
  board in a known format is added on the Sources page, and a new format is
  one fetcher returning the same `JobPosting` as the rest.
- **What the mail implies the board should say** — `mail_pipeline.proposals_for`
  and `answer_proposal`. The route that lists proposals and the queue that
  merges them into everything else read the same function; a second spelling
  would drift, and the first one already had - an inner join against
  `user_jobs` silenced 947 of 1,159 proposals by never forming the question for
  applications that have no board row.

## Reading production from outside it

**Anything that reads the production database shares production's blast
radius**, including development tooling. A tool that only reads is not
therefore safe.

**A long read blocks schema changes.** A bulk read holds a shared lock for its
whole duration; a queued `ALTER TABLE` waits behind it, and everything after
that queues behind the ALTER. The application runs migrations at startup, so a
read that outlives a deploy prevents the application from starting at all —
containers sit at "waiting for application startup" while every health check,
image digest and container status reports normal.

The property to build for is that such a tool **cannot** hold a lock long
enough to matter, not that someone remembers when to run it:

- **Chunk the read** so no cursor outlives a deploy. One transaction per range
  means a queued DDL waits one chunk instead of one table. This also makes the
  copy resumable, which a large transfer needs regardless.
- **Set `idle_in_transaction_session_timeout` on the reading role** as the
  backstop. A tool that dies at the client end can otherwise leave a cursor
  pinned indefinitely — the danger window is not the run's duration, it is
  unbounded until someone kills the connection.
- Set both as role-level defaults so a future caller cannot forget them.
- **Better than any timeout: do not keep the connection alive.** A process that
  exits when its work finishes cannot leak a lock, because the state is gone
  rather than bounded. A long-lived container holding a pool is what turns a
  finished job into an open transaction.

The exposure window is **not the duration of the run.** A connection can
outlive the job that opened it, so "is a copy running right now" returns no
while a lock is still held. Ask what connections exist, not what jobs are
running.

Two levers that look right and are not: `statement_timeout` caps how long the
read runs but the DDL still queues for that whole period, and any timeout long
enough to copy a large table is long enough to stall a deploy. `lock_timeout`
governs locks a session **waits for**, not ones it **holds**.

**A foreign-data-wrapper session is opaque from the source side.** It shows as
`FETCH n FROM cN` with no table name, so grepping the source for what you think
it is reading finds nothing and proves nothing. Identify it by client address
and application name — but note a host can present **more than one public IP**
depending on egress path, and the same resolver can return different answers
from different processes on that host. One reading does not identify a machine;
check several, and prefer a causal test (stop the suspected process, see if the
session goes) over an address match.

## Visibility and ownership

Job visibility is a read-time conjunctive predicate, spelled in one place and
formatted into per-object routes. Never write a fresh "can this user see this"
predicate.

**Location criteria match places, not words.** Every distinct location
string a board writes is one row of `locations`, classified once by a model
into the places it names (country, region, city, as many as it lists) and
remote (`api.tasks.locations`), and a user's
excluded and included locations are rows of the same table: a country
criterion takes every city in it, a city criterion that city, a bare Remote
criterion remote postings. Excluded hides a posting with any matching
location; included shows only a posting with one, a posting with no location
staying. There is no word match: a string not yet classified excludes
nothing for at most the one cycle that classifies it. A wrong row is a PUT to
/admin/locations, never a deploy; the sweep never re-asks about a string that
has a row.

**An admin closes a posting the way the closed check does.** POST
/admin/jobs/{id}/close writes a rejected closed verdict naming the admin and
the reason, so the posting leaves every board on the next read and nothing
re-runs; `active` stays the catalog's fact about whether the board still
lists it. The report row says `posting_closed` from the same verdict.

**A vocabulary the frontend renders is served with its meaning, never
copied.** Board statuses come with `status_meta` (terminal, outcome), report
kinds with their labels, pipeline stages with their order and which are
terminal, task rows with `cancellable`, tunables with type and help. The
frontend renders from the response and keeps no parallel list; a new value is
one backend entry.

**A set-shaped filter takes a comma list and the response echoes `filters`.**
`status=pending,running` through `api.params.csv`; the envelope carries
`filters: {status: [...]}` with what was applied, empty when nothing was, so
a client can tell "lists accepted" from an older build by the key's presence.

List endpoints sort through `api.sorting` against a per-endpoint whitelist of
column expressions: `sort=a,b&dir=asc,desc` is several columns at once, a
`dir` shorter than `sort` repeats its last value, unknown keys drop rather
than refuse, and the response echoes `sorts` as applied and `sortable`. A
sort parameter never reaches SQL as text.

**Route-level authorisation says nothing about object-level authorisation.**
Owning a parent does not imply owning a child: a nested identifier must be
checked against the caller, not assumed from the path.

Any authenticated request auto-provisions a user row. Treat authentication as a
write.

**Every admin list that returns rows a person owns takes `user=<id>[,<id>]`,
and the predicate per table lives in `api/scoping.py`.** Rows, summaries and
totals narrow together. The envelope carries `filterable`, the parameters the
endpoint filters on, beside the `filters` echo, so a client renders a User
control from the former. An endpoint with no user dimension (fleet workers,
the shared checks, source analytics) leaves `user` out of `filterable`
rather than pretending.
