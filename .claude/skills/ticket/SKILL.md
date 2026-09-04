---
name: ticket
description: Investigate a task, bug report, or idea against this codebase and file it as a GitHub issue on kensac/job-scripts. Use when the user describes something to look into and wants a ticket out of it, for example "look into X and file it", "raise a ticket for X", "/ticket X". Reads the real code paths, measures the affected population against production when a count is cheap, and files immediately with `gh issue create`.
---

# Filing a ticket

Read [AGENTS.md](../../../AGENTS.md) first. Its non-negotiables apply here
unchanged. A ticket is a claim about the system, so it is held to the standard
a report is held to. This file adds only what is specific to turning an
investigation into a filed issue.

The output is a filed issue and its URL, in the same turn you finish
investigating. Not a draft, not a summary in the terminal.

## The rule that makes a ticket worth anything

Every claim in the body traces to a file and a line you actually opened, or to
a query you actually ran. A ticket built from what the code probably does costs
the person who picks it up more than filing nothing would have.

## Procedure

**Check it is not already filed or already fixed.**

```
gh issue list --state all --search "<keywords>"
git log --oneline -30
```

A closed issue on the same defect is a duplicate. A fix already on main is a
stale report. Say so and stop. Both are cheaper than the ticket.

**Read the code that does it.** Cite `src/api/…:123` for every mechanism the
body describes.

**Decide whether to measure, and say which you did.** Judge per task. Measure
when the count is cheap and would change how the ticket reads. "412 of 49,203
jobs" and "nothing yet" are different tickets. Skip it when the population is
obvious from the code, when no query separates the cases, or when the schema
does not record the thing.

Production credentials come from `.env`. AGENTS.md rules 5 and 6 say what you
may send that database and that API.

```
set -a && . ./.env && set +a
psql "$DATABASE_URL" -c "SELECT count(*) FROM … WHERE …"
```

Either way the body says so. "Not measured, because the schema does not record
whether anything looked" is a finding. Silence is not.

**Ask the other surface's question**, per
[working-agreement.md](../../../docs/agents/working-agreement.md). Answer it in
the body, including when the answer is "nothing", with the reason.

**File it.**

```
gh label list
gh issue create --title "…" --body-file <path>
```

Existing labels only. Do not invent one. AGENTS.md rule 4 covers issue bodies,
so no footer, no co-author line, no "filed by".

Hand back the URL and two lines on what you measured. If the investigation
found the thing already correct, report that instead of filing.

## The body

Five headings, in this order. Nothing else.

**Objective.** One sentence. What should be true when this is done.

**Reason.** Why it matters, with the numbers and the `path:line` citations.
Aim for under 200 words. Put counts in a table, not in a paragraph.

**Requirements.** A numbered list. One testable thing each. If a requirement
needs a sentence of justification, the justification belongs in Reason.

**Passing criteria.** A checklist someone can actually run or query. Each line
must be true or false with no judgement call. "The suite passes with no
secrets set" is a criterion. "Test coverage is good" is not.

Include a criterion covering data that already exists whenever the change has
an existing population. A fix that only affects new rows reads as done and is
not.

**Not verified.** Short bullets. What you did not check. The ticket is the
record, so a caveat that lives only in the investigation does not exist.

## Writing style

Write for someone who can program but has never seen this codebase. A high
schooler who knows Python should be able to follow it.

- **Short sentences. One idea each.** Break a long sentence into two.
- **No em dashes.** Use a full stop, a comma, or brackets.
- **Plain words.** "Hides" not "suppresses". "Missing" not "absent".
- **One screen.** If the body does not fit on one, it is probably two tickets.
  Split it and link them.
- **No throat clearing.** Do not restate the problem before stating it. Do not
  end with a summary of what you just said.
- **Numbers go in a table.** Prose full of figures is unreadable.
- Titles are lowercase and name the outcome, matching the commit log. Use
  "generate test data from production shapes", not "[FEATURE] Test data
  improvements".

## What not to file

- A TODO nobody should get to. Record it as a measured negative instead.
- A hypothesis with no code read behind it.
- A fix for one copy of logic that exists in three places. That ticket is
  "delete the duplication", not "fix the copy".
