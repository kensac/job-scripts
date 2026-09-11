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
| 1 | Check types are a registry | A new check type is a registration; no literal names it |
| 2 | A board row and the working set are told apart | Named and pinned apart (2a). Moving the sweeps' scope off `user_jobs` (2b) waits for a cutover comparison |
| 3 | Catalog observations are facts | A re-listing is an appended row, not a mutated column |
| 4 | ~~Derivations are content addressed~~ | **Dropped 2026-09-10.** Measured; see below |
| 5 | Files move to the shape | `tasks` is a sibling of `api` and `core`. `apply` is a package. The rest is judgement about churn |
| 6 | The long files are split | No module does four jobs. `admin.py` 2,136 lines, `mail.py` 2,117, `resolve.py` 1,337, `health.py` 1,155, `tasks/runtime.py` 796. `orm.py` is done |
| 7 | Every operation declares what it returns | `openapi.json` generates the frontend's types. 173 of 190 operations declare nothing today |

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
and invisible in CI. Phase 2 and phase 4 are the named cases: one decides what
gets paid for, the other migrates verdicts that cost money to produce. Bring
the parallel cutover's numbers first, then merge.

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

What those rows do instead is carry scope. `AI_ELIGIBLE_JOB` admits any job
with a board row, and the re-verification sweep takes its candidates from
`user_jobs`, so an untouched row is what keeps a posting being paid for. That
is a second job the table was never named for, and `materialize_passing` still
describes itself as a mirror of the step that wrote a Google Sheet.

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

Phase 2 still brings its numbers before it merges: it decides what gets paid
for.

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

Turning the contract on then named six imports, and five are one smell wearing
five hats: every one reaches past a handler for a SHAPE or a CONSTANT, never
for behaviour. `SHAPES` twice, a drafting default, a verdict model, an event
kinds list. So the contract is written down in `pyproject.toml` and not
enabled, because five exceptions is a contract that records debt rather than
preventing it.

The slice that lands it: move the shapes to where both layers can read them.
`tasks/models.py` is already a shapes module and is under `tasks` by habit.
`SHAPES` is the awkward one, assembled BY importing each task module to read
the shape it declares, so those declarations invert and live beside
`TaskShape` in `core/routing.py`.

## Phases 6 and 7

Added on Kanishk's instruction, to be taken after the move lands.

**6, the long files.** `tasks/runtime.py` is the clearest case rather than the
biggest: 796 lines holding queue primitives, batch orchestration, config
constants and model routing, imported by 24 modules. Splitting it is what
would let the layering be stated as layers, because three of the four imports
that make `api` and `api.tasks` circular are reaching past the handlers for a
queue primitive. `admin.py` and `mail.py` are bigger and simpler: they are
long because nothing ever split them, not because anything is tangled.

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

## Logging is nearly consistent, and the gap is small

Measured 2026-09-10: 38 modules log, under seven logger names. Two are the
convention, `jobtracker_worker` (24) and `jobtracker_api` (11). Five are
one-offs, and one of those is `job_tracker` in `core/batch.py`, spelled
differently from every other.

Nothing is lost by it. Telemetry attaches its handler to the ROOT logger
precisely so a module needs no registration, which was checked before this was
written down. The cost is only that filtering by source in a log viewer does
not work the way the names suggest.

Also worth one pass: 18 sites use `logger.exception` and 6 use `logger.error`.
The second drops the traceback. Some of those six are deliberate, because the
error is expected and the traceback is noise; they are worth reading rather
than rewriting in bulk.

## The gaps list

Everything measured and not yet closed, with the number that makes it a gap
rather than an opinion. A row leaves this list when it is fixed or when a
measurement says it was never worth fixing, and either way it says which.

**The schema is not a contract.** 173 of 190 operations return an undeclared
object; 11 declare a shape. Phase 7.

**A failure is not in the contract at all.** 164 `raise HTTPException` sites,
at least three `detail` shapes among them (69 `{code, message}`, 14 a bare
string, 1 an f-string), and the schema declares 200, 201, 202 and the 422
FastAPI adds. No 4xx. Phase 7.

**Reads are untyped, and the goal is all of them.** 563 db call sites return
bare dicts. `db.query_as` is the primitive and adoption is per domain, on
Kanishk's instruction of 2026-09-11 that every read should carry a shape.

Two things make it work that are worth knowing before starting a domain.

**A `SELECT *` cannot be typed until it names its columns.** There are 24, in
`core/store.py`, `tasks/filters.py`, `tasks/ingest.py`, `tasks/experiments.py`
and `tasks/uploads.py`. Naming them is a good change on its own: a star select
and the shape that reads it drift silently, which is the same defect one level
down.

**A response model must be defined ABOVE the route that returns it.** This
module uses `from __future__ import annotations`, so a return annotation is a
string and FastAPI resolves it when the decorator runs. A model defined later
in the file raises `PydanticUserError: ... is not fully defined`, at import,
with a message that does not say the cause. Found the hard way on
`routers/apply.py`.

**The services still reach into the handlers.** Nine ignored imports in three
causes. The largest is four routers calling four helpers that live inside a
handler because that is where they were first needed: `resume_text`,
`writing_style`, `instructions`, `locations.store`. Moving those four is what
empties most of the list.

**SHAPES cannot move down.** It is assembled by importing each task module to
read the shape that module declares, and two do not inverse cleanly:
`mail_classify` builds its two shapes in a function, and `application` reads
its purpose from another module.

**Unattended code that says nothing when it goes wrong.** Four task handlers
log nothing at all: `experiments` (505 lines), `embeddings` (232),
`batch_policy` (64), `uploads` (61). A handler runs with nobody watching, so
silence there is different from silence in a router, where uvicorn's access
log carries the request.

**Derivations that say nothing.** `api/mail/pipeline.py` (588 lines) derives
an application's state from an event stream and logs nothing;
`api/ai/__init__.py` (452) is the call path.

**Seven logger names where two would do.** `jobtracker_worker` (24 modules)
and `jobtracker_api` (11) are the convention; five are one-offs and
`core/batch.py` uses `job_tracker`, spelled differently from all of them.
Nothing is dropped, because telemetry attaches at the root; filtering by
source is what does not work.

**Tracebacks dropped on purpose or by accident, unknown which.** 18 sites use
`logger.exception` and 6 use `logger.error`. The second keeps no traceback.
Worth reading rather than rewriting in bulk: some are deliberate.

**Test coverage, measured for the first time on 2026-09-11: 87%.** 11,800
statements, 1,528 missed, across 1,656 tests. Better than "never measured"
usually means, and the shape of the miss is the useful part rather than the
total:

| | |
|---|---|
| `core/store.py` | 59%, 50 of 123 statements |
| `tasks/verify.py` | 70%, 58 of 195 |
| `tasks/ingest.py` | 81% |
| `tasks/filters.py` | 82%, 42 of 228 |

The gap is concentrated in the sweeps and the store, which is exactly where
this session found its two live defects: a closed verdict that could never be
revisited, and a clearance verdict that was written once and never again.
Both lived in `tasks/verify.py`. Neither was caught by a test, and the
coverage number says why: that file is the least covered substantial module in
the repository after the store.

`make coverage` prints it. Nothing gates on a threshold yet, and adding one
before the sweeps are covered would only ratchet in what is already there.

**The long files.** `routers/admin.py` 2,136 lines, `routers/mail.py` 2,117,
`routers/resolve.py` 1,339, `health.py` 1,155. Long because nothing split
them. Phase 6.

`orm.py` was the first taken, and it is the easy shape of this problem: 51
table definitions with no logic between them, so the split is a partition and
the only risk is that a table stops being registered. It is now `api/orm/`,
six modules named for what the tables are for, and `__init__.py` imports all
six so one metadata still carries all 51. The others are not this shape; a
router splits along what its handlers do, and that is a reading, not a
partition.

**`user_jobs` answers two questions.** What a person keeps, and what the
sweeps carry. Phase 2b, deferred: moving the sweeps' scope changes what gets
paid for, so it waits for a cutover comparison.

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
