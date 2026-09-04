# Engineering standards

## Correctness

Production-grade only. No patches, no hotfixes. If a design turns out wrong,
rearchitect it rather than working around it.

Preserve concurrency and locking semantics across refactors, and check them
explicitly rather than assuming a refactor kept them.

When the same logic exists in several places and one has drifted, delete the
duplication. Do not fix the copy.

## Constants

**Magic numbers are a shortcut. Derive the constant or name where the value
came from, in a comment beside it.**

A derived threshold beats a tuned one because it adapts and needs no
maintenance. Prefer a rule that falls out of how the system actually works:

- A count that is bimodal gives you the threshold directly.
- A cap derived from a downstream consumer's limit cannot drift out of sync
  with it.
- A bound taken from a value the system already declares — a provider's stated
  completion window, an existing timeout — inherits that value's meaning
  instead of inventing one.

**A threshold must not be derived from a statistic the failure it detects can
move.** A bound set from an operation's own historical maximum looks like the
ideal derived threshold: it adapts, needs no tuning, and tracks the operation
as it changes. It is a trap when the failure mode ends in a completed run,
because each failure that survives enters the history and raises the bound.
The detector then goes blind exactly as often as the problem occurs, while
continuing to look like it works — which is worse than having none.

Before deriving a bound from history, ask whether the thing being detected can
end up inside that history. If it can, the statistic is contaminated and you
need a signal the failure cannot contribute to.

**A verification claim can have a shelf life.** Checking a threshold against
live data proves it was right at that moment, not that it stays right. If the
data behind a bound can move, say when it was measured, and prefer a bound that
cannot drift out from under the claim.

State the scale a constant depends on. A value derived from one user's data
should say so.

## Measuring

Price an optimisation before defending it. Count the population before
describing a mechanism as costly.

A measurement taken in one window is a snapshot, not a property. If a number
could differ an hour from now, say when it was taken. Prefer answering from
mechanism plus full-corpus counts over watching a trend.

Before concluding from an aggregate, check that the filter producing it does
not exclude the population in question. A check scoped by the thing it is
checking can never fail.

## A fix must reach its own population

**Measure after shipping, not only before.** A fix that corrects a rule but
cannot reach the data the rule already produced reads as done and is not.

Three shapes of this, each invisible from the code alone and each needing a
production count to see:

- **The rule is fixed for new data only.** A derivation corrected at write time
  leaves every existing row computed the old way, and nothing recomputes them.
  Prefer a correction that runs every cycle, only ever moves in the safe
  direction, and becomes a no-op once the data is right — then the next change
  to that rule reaches everything automatically.
- **Two predicates in one module mean different things.** One reads "nothing
  has looked", its sibling reads "looked and found nothing". Both look correct
  in isolation, and the docstrings can describe behaviour neither implements.
- **A field's name and its value disagree.** A counter named for successes may
  be summing every outcome. Read the assignment, not the name.

After a change lands, count the population it was supposed to affect. If the
number did not move, the fix did not reach it — regardless of what the tests
say, because the tests exercise the new path and the old data is not on it.

## Trusting a signal

**The summary line is not the measurement.** A process's report of itself is
not evidence about the underlying state. Compare the state directly.

This applies to: exit codes, "no changes" messages, completion counts, task
status, a truncated command's first lines, and any field whose name describes
an outcome. Read the field's definition before treating its name as its
meaning — a counter named for successes may be counting attempts.

A clean exit reads as success everywhere. Verify that the thing happened, not
that the process finished.

## Comments

Do not write docstrings or comments unless they carry something the code
cannot. When they do, they are load-bearing: record why a decision was made
and what breaks if it is reversed, not what the line does.

Comments are frequently wrong. Verify a claim in a comment before relying on
it, and correct it when you find it stale.
