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
| 6 | The long files are split | No module does four jobs. `resolve.py` 1,339 lines, `health.py` 1,159. `orm.py`, `mail.py`, `admin.py` and `tasks/runtime.py` are done |
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

**A failure is in the contract now, but it is not one shape.** The schema
declares 400, 401, 403, 404 and 409 on 188 of 190 operations, so a client can
read what a refusal looks like. What it cannot read is which of two spellings
arrives: of 164 `raise HTTPException` sites, 109 send `detail={"code",
"message"}` and 19 send a bare string or an f-string. **All 19 are in
`routers/mail/`**, and converting them turns `detail` from a string into an
object for a frontend in another repository, so it is a coordinated change
rather than a tidy-up. Phase 7.

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

**The services still reach into the handlers, in four imports.** Nine on
2026-09-11, and the five that were a shape or a constant are closed. The
contract in `pyproject.toml` is enforced in CI and carries only these, each
with what would move it.

`api.worker -> tasks` and `api.worker -> tasks.runtime` are correct and are not
debt. The worker IS the task runner: it loads HANDLERS to dispatch them and
reads the runtime for the same reason. Two lines for one fact, and the fact
goes away only if the worker stops being an `api` module. Nothing in the code
would change if it did, so it has not been done for a line in a config file.

`api.routers.admin.catalog -> tasks.locations` and
`api.routers.experiments -> tasks.experiments` are behaviour, and they are the
design error rather than a missing home. the catalog calls `locations.store`;
experiments calls `steps`, `arm_ok`, `arm_name` and `summarise`. What moves
them is moving the functions, not a constant, and experiments is the bigger
half: `tasks/experiments.py` is 505 lines of which the handler is about 40 and
the rest is the experiment domain the router actually wants. That is a phase 6
split as much as a contract fix, and it should be taken as one.

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

**The long files.** `routers/resolve.py` 1,339 lines, `health.py` 1,159. Long
because nothing split them. Phase 6.

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
