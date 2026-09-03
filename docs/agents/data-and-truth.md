# Showing data to a person

The governing principle: **the more information and intelligence you give a
person, the easier it is for them to make a decision.** Surface the evidence
and let them decide. Do not decide for them and show only the conclusion.

## Rules

**Inferred things are possibilities, never facts.** Render them as an
intelligence pane, not a verdict.

**Every number ships with what it is a number of.** A rate below its sample
floor renders as "2 of 7", never as a percentage. If a section counts a
different population than the section above it, say so on the page.

**Show the disagreement rather than hiding it.** The sentence that lets someone
catch a wrong answer is usually the same sentence that lets them accept a right
one. "The mail says X, this posting says Y" does both jobs.

**A value you cannot compute is NULL and says so.** Never zero, never a guess.
An unpriced model is not free. An unattempted extraction is not "nothing
found". "We looked and found nothing" is a different fact from "nothing has
looked", and collapsing them is the most common bug in this codebase.

**The stored value and the effective value must never disagree silently.** If a
setting is overridden at runtime, show the effective value beside the chosen
one, with the reason.

**A capability says what is allowed and, when it is not, why not** — in the
same object. "You may not select this" is useless. "Not available because your
group is capped — bring your own key or ask an admin" renders itself.

## Distinctions that must not collapse

These have each been collapsed more than once. They are different facts:

| This | Is not this |
|---|---|
| Deliberately attached to nothing | Looked and found nothing |
| Nothing has looked yet | Looked and found nothing |
| NULL (nobody computed it) | Zero (computed, and it is zero) |
| Never asked | Asked and declined |
| Process alive | Handler making progress |
| Low priority | Cannot ever be resolved |
| Collected | Written |
| Merged | Deployed |

When a single column is carrying two of these, that is the bug. Split the
column rather than teaching every reader the distinction.

## Deriving rather than storing

Derive state at read time wherever the inputs are available. A derived value
cannot desync from reality; a stored one can.

Store only a person's answer. Their decision is a fact worth keeping; the
system's inference about them is not.

Consequences worth having: a suggestion disappears once they act on it
elsewhere, a checklist step closes when the thing is actually done rather than
when something marked it done, and a correction propagates without anyone
restating it.

## Append, never overwrite

Corrections are appends. The latest row wins on read; the superseded row stays
visible as history. This makes an undo another append rather than a delete, and
it means a wrong answer remains inspectable.

Record who made a correction as an identifier, not a role. Whether that person
was the owner or an administrator is derived by comparing against the row's
owner, so there is no second copy of that distinction to drift.
