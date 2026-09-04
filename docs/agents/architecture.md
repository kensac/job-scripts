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

## Visibility and ownership

Job visibility is a read-time conjunctive predicate, spelled in one place and
formatted into per-object routes. Never write a fresh "can this user see this"
predicate.

**Route-level authorisation says nothing about object-level authorisation.**
Owning a parent does not imply owning a child: a nested identifier must be
checked against the caller, not assumed from the path.

Any authenticated request auto-provisions a user row. Treat authentication as a
write.
