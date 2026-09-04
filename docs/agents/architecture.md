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

**Selection must exclude work already in flight**, and a task's own in-flight
claim must not exclude the task itself when it resumes. A guard that hides a
task's own work from it will make the task discard results it already paid for.

**Collection must be reachable when there is nothing new to submit.** An early
return on an empty selection, placed before collection, strands completed work.

**A task waiting on several batches resumes only when every one of them is
terminal.** One straggler therefore holds every finished batch beside it, and
the results stay uncollected although they are already paid for. This is
latency rather than loss — the overdue path collects whatever landed once the
provider's window passes — but the wait is bounded by that window, not by the
batches that finished. Size a batch knowing that its slowest request sets when
any of it can be read.

Dry-run a handful of live calls before committing to a large batch. A batch
fails whole, and the dry run also measures real token counts.

## The worker fleet

Tasks are claimed with row-level locking and skip-locked selection. A worker
heartbeats while it holds a task.

**Process alive and handler progressing are different facts.** A liveness
signal decoupled from the work cannot observe the work stopping; a liveness
signal coupled to the work stops when the work stops. Neither alone is
sufficient, and reaping keyed on the wrong one either kills healthy work or
never recovers stuck work.

A handler that never yields holds its worker until it finishes. Long handlers
should hold progress in the database so an interruption resumes rather than
restarts.

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

**Route-level authorisation says nothing about object-level authorisation.**
Owning a parent does not imply owning a child: a nested identifier must be
checked against the caller, not assumed from the path.

Any authenticated request auto-provisions a user row. Treat authentication as a
write.
