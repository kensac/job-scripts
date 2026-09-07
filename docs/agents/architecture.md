# Architecture and domain semantics

The pipeline, the matcher, and the facts every other topic reads. Four
neighbouring documents carry what used to live here:
[sources-and-boards.md](sources-and-boards.md) for ingest and the catalog,
[visibility.md](visibility.md) for who sees what,
[observability.md](observability.md) for the worker fleet and batched work,
and [reading-production.md](reading-production.md) for reads from outside.

## The mail pipeline

Import → classify → match → derive.

- `email_messages`: the message as it arrived. Body text and body HTML are
  both retained; the text is derived from the HTML, so the HTML must be stored
  before anything re-derives the text.
- `email_events`: append-only. Latest row per message wins on read.
- `application_matches`: append-only. Latest row per message wins on read.
- `applications`: `job_id` is nullable. **Never synthesise a job row from an
  email.** Mail predating the catalog is the normal case.
- `action_items`: derived from events, resolvable by a person.

**Stage is derived at read time from the event stream and never stored.**
Terminal outcomes beat progress regardless of arrival order. Withdrawal comes
from the board, not from mail: no employer writes to say you withdrew.

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
land in between. That is not hypothetical. The only human decision ever
recorded in production was overwritten exactly that way, an hour after it was
made.

**Every write to `application_matches` goes through `mail_match.record`**, so
`actor_user_id` cannot be omitted. Three endpoints wrote the table directly and
none of them set it. So rows reading `manual` are not evidence a person was
there, and "has anyone reviewed this attachment" was unanswerable by query for
15,090 rows.

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
consumer is the market table, and measured on 2026-09-07 through the
experiments harness the deployed arm (nano, minimal) named a seniority for
5 of the 92 postings the reference named one for and shared about a third
of the skills, most of the rest being wording. Turning it on is a config
row, not a deploy; choosing an arm worth paying for is the experiment
(luna at none gets seniority to 74 percent of the reference for about 70
cents a day at the current verification rate). Comp and
requirements select on `core.store.VERIFIED_OPEN`: the posting's latest
closed and clearance verdicts both passed.

**An arm that answers nothing is re-bought every sweep.** Comp leaves a row
unextracted when the answer does not parse, so the next sweep selects it
again; that is the right retry for a transient failure and a spend loop
for a systematic one. The 2026-09-07 baselines showed the systematic case:
nano at medium or high spends the whole output cap on reasoning and
returns no text on verify, comp and requirements (65 to 100 of 100).
Production runs those steps at low and never hit it. Try an effort in an
experiment, where a failed arm costs one batch and shows as a count, not
on a production step, where it costs one batch per hour until someone
notices. On 2026-09-06 that was 37,438 of
74,477 active postings; the other half is closed or restricted and reaches
no board, so a number extracted from it is never read. The narrower cut,
extracting only for postings on someone's board (3,117 that day, 4 percent
of the catalog), is a documented option not yet taken: it would make a
posting that reaches a board later wait a cycle for its comp column and
build the market table from a smaller slice than the filters admit. If the
verified-open cost still reads as too high, that is the next step, and it
is the same one-line change to the same two selections.

## Time

Containers run on a local timezone by deliberate convention; hosts and the
database are UTC. **Store aware UTC, never naive local time.** A timezone-less
date is uncertain by exactly one day, and any comparison against it should
carry that width rather than inventing an hour by casting.

## Named places

These exist in exactly one place each. Change them there, and never write a
fresh copy:

- **Job visibility**: `api/visibility.py`. `FULL` is the one spelling of the
  predicate and only the recompute task runs it; `FAST` is the one spelling of
  the read, and every board read, per-object route and requirements slice goes
  through it. The board task's materialise and candidate queries share
  `criteria.SQL` and the structural gates with `FULL` and must stay consistent
  with it. See [visibility.md](visibility.md).
- **AI pricing**: `core/pricing.py`, rendered as both Python and SQL from one
  source with a parity test.
- **Provider facts**: one datasheet per provider under `core/providers/`.
- **Task handlers**: `api/tasks/`, one module per family. The task runtime
  imports nothing from the worker; the worker imports only the handler table.
- **Listing formats**: `core/boards.py`, one fetcher per board format, chosen
  by the listings URL. See [sources-and-boards.md](sources-and-boards.md).
- **What the mail implies the board should say**: `mail_pipeline.proposals_for`
  and `answer_proposal`. The route that lists proposals and the queue that
  merges them into everything else read the same function. A second spelling
  would drift, and the first one already had: an inner join against
  `user_jobs` silenced 947 of 1,159 proposals by never forming the question for
  applications that have no board row.
