---
name: ticket
description: Investigate a task, bug report, or idea against this codebase and file it as a GitHub issue on kensac/job-scripts. Use when the user describes something to look into and wants a ticket out of it — "look into X and file it", "raise a ticket for X", "/ticket X". Reads the real code paths, measures the affected population against production when a count is cheap, and files immediately with `gh issue create`.
---

# Filing a ticket

Read [AGENTS.md](../../../AGENTS.md) first. Its non-negotiables govern this
work unchanged — a ticket is a claim about the system, so it is held to the
standard a report is held to. This file adds only what is specific to turning
an investigation into a filed issue.

The output is a filed issue and its URL, in the same turn you finish
investigating. Not a draft, not a summary in the terminal.

## The rule that makes a ticket worth anything

Every claim in the body traces to a file and a line you actually opened, or to
a query you actually ran. A ticket assembled from what the code probably does
costs whoever picks it up more than filing nothing would have.

## Procedure

**Check it is not already filed or already fixed.**

```
gh issue list --state all --search "<keywords>"
git log --oneline -30
```

A closed issue on the same defect is a duplicate. A fix already on main is a
stale report. Either way say so and stop — both outcomes are cheaper than the
ticket, and neither is a failure.

**Read the code that does it.** Cite `src/api/…:123` for every mechanism the
body describes.

**Decide whether to measure, and say which you did.** Judge per task. Measure
when the count is cheap and would change how the ticket reads — "412 of 49,203
jobs" and "nothing yet" are different tickets. Skip it when the population is
obvious from the code, when no query separates the cases, or when the schema
does not record the thing.

Production credentials come from `.env`; AGENTS.md rules 5 and 6 say what you
may send that database and that API.

```
set -a && . ./.env && set +a
psql "$DATABASE_URL" -c "SELECT count(*) FROM … WHERE …"
```

Either way the body says so. *Not measured, because the schema does not record
whether anything looked* is a finding; silence is not.

**Ask the other surface's question**, per
[working-agreement.md](../../../docs/agents/working-agreement.md). Answer it in
the body — including when the answer is "nothing", with the reason.

**File it.**

```
gh label list
gh issue create --title "…" --body "…" --label "…"
```

Existing labels only; do not invent one. AGENTS.md rule 4 covers issue bodies
too — no footer, no co-author line, no "filed by".

Hand back the URL and two lines on what you measured. If the investigation
found the thing already correct, report that instead of filing.

## The body

Title reads as the outcome, in the repository's voice — a lowercase sentence
naming what is wrong or what should be true. Match the commit log: "an
internship rejection stops attaching to a full-time application", not
"[BUG] Rejection matching issue".

Sections in this order, dropping any with nothing to say:

**What is wrong** — one paragraph, in terms of what a person sees.

**Evidence** — the mechanism, `path:line` at each step. This is the part that
survives; write it so someone can follow it without you.

**Scope** — the count, what it is a count of, and the date it was taken. Or the
sentence saying why it was not measured.

**Both surfaces** — what `/job-tracker` and `/job-scripts` each need.

**What done looks like** — the observable end state, not an implementation
plan; whoever takes it may find a better design. If rows already written the
old way need correcting, that belongs here, because a fix that cannot reach
its own population reads as done and is not.

**Not verified** — everything you did not check. The ticket is the record; a
caveat that lives only in the investigation does not exist.

## What not to file

- A TODO nobody should get to. Record it as a measured negative instead.
- A hypothesis with no code read behind it.
- A fix for one copy of logic that exists in three places. That ticket is
  "delete the duplication", not "fix the copy".
