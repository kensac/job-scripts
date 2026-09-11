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
| 2 | A board row and the working set are told apart | `user_jobs` says what a person keeps; something else says what the sweeps carry |
| 3 | Catalog observations are facts | A re-listing is an appended row, not a mutated column |
| 4 | ~~Derivations are content addressed~~ | **Dropped 2026-09-10.** Measured; see below |
| 5 | Files move to the shape | Packages match this document |

**The API contract is the invariant.** `openapi.json` is canon and
`tests/test_openapi_current.py` fails the build when routes and schema
disagree. No phase changes an operation's shape. A frontend and a browser
extension read that contract and are not in this repository.

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
