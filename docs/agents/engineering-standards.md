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
- A timeout expressed as a multiple of that operation's own historical maximum
  needs no per-operation tuning and stays correct as the operation changes.
- A cap derived from a downstream consumer's limit cannot drift out of sync
  with it.

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
