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

Each phase ships on its own and leaves the system working. The order is not
arbitrary: the file moves are LAST, because moving files before the seams are
real only relocates the problem.

| # | Phase | Done when |
|---|---|---|
| 0 | The layering is enforced | An import contract fails CI on a new upward edge |
| 1 | Check types are a registry | A new check type is a registration; no literal names it |
| 2 | One definition of board membership | Deleting `board_visible` and recomputing changes nothing |
| 3 | Catalog observations are facts | A re-listing is an appended row, not a mutated column |
| 4 | Derivations are content addressed | Changing the model does not invalidate a verdict |
| 5 | Files move to the shape | Packages match this document |

**The API contract is the invariant.** `openapi.json` is canon and
`tests/test_openapi_current.py` fails the build when routes and schema
disagree. No phase changes an operation's shape. A frontend and a browser
extension read that contract and are not in this repository.

## What may be done unattended

A merge reaches six hosts on Renovate's cadence with nobody watching
([deployment.md](deployment.md)), so "the tests passed" has to be enough
before a change can land that way.

**May be taken by an agent, PR opened and merged on green CI:** phase 0 and
phase 1. Both are mechanical and have a decisive test: a new upward import
fails the contract; a registered fake check type needs no other edit.

**Needs a person before merge:** phases 2, 3, 4 and 5.

Phase 2 decides who sees what. Its failure mode is two definitions silently
agreeing in the tests and disagreeing in production, which is the state the
codebase was already in while every test passed.

Phase 4 migrates the verdict cache. Those rows cost money to produce and
cannot be casually rebuilt.

**Never in a loop:** any write to the production database, and any migration
that can refuse to apply ([migrations.md](migrations.md)).

## Parallel cutover

A phase that changes how something is computed builds the new answer beside
the old one and compares them on production data before the old one goes.
Agreement on a sample is the evidence that the cutover is safe, and a
disagreement is a finding whichever way it falls. The old path goes in a
separate commit from the new path arriving, so a revert is one commit.
