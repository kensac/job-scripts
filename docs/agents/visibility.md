# Visibility and ownership

Who sees which posting, who owns a row, and the response shapes every list
endpoint keeps to.

## Visibility is computed, never evaluated on read

Job visibility is one predicate, spelled once (`api.visibility.FULL`), and it
is COMPUTED. A worker runs it per person into `board_visible`. Every board
read, per-object route and requirements slice then goes through
`api.visibility.FAST`, a lookup.

It was evaluated on every read until 2026-09-05, at 2 to 7 seconds a sort.

Membership changes when a preference changes or a verdict lands, so a
preference write asks for a recompute within a minute, and the scheduler asks
every `board_refresh_minutes` (persisted config, seeded 3) for everyone the
predicate can admit anything for: a subscription, an acted-on row or an
upload. A users row with none of those is skipped; one such row drew 825
recomputes in a day for zero rows. A posting the person uploaded or acted on
is visible without waiting.

**Never write a fresh "can this user see this" predicate, and never evaluate
FULL on a request.**

## A board row is a grant only when the person acted on it

The worker materialises an empty row for every posting that passes a person's
filters. That row is bookkeeping, not a decision, so it obeys the whole
predicate like any other posting: the person's criteria (locations, posted
date) and the verdicts. The first version re-checked only the criteria, so
a filter that later rejected a posting could not remove the row the earlier
pass had made: 614 rejected postings sat on one board on 2026-09-07 after a
model change, and the change read as having no effect.

A status, a note or a date applied is a decision, and the row is theirs
whatever the criteria or the verdicts say.

## The criteria are the first paid rung

A person's criteria (`user_settings.criteria`: a posted-after date, a rolling
`max_age_days`, included and excluded places) are applied by the same SQL
in two places: the board membership and the filter's candidate selection.
So a posting outside the criteria is never sent to the filter model, and
the criteria are the cheapest place to narrow spend. The verify and comp
sweeps run on every verified-open posting regardless, because their results
are shared across people. The rolling window reads the date the board gave
the posting and falls back to the day the catalog first saw it, so an
undated posting still expires.

## Location criteria match places, not words

Every distinct location string a board writes is one row of `locations`,
classified once by a model into the places it names (country, region, city, as
many as it lists) and remote (`api.tasks.locations`).

A user's excluded and included locations are rows of the same table. A country
criterion takes every city in it, a city criterion that city, a bare Remote
criterion remote postings.

Excluded hides a posting with any matching location. Included shows only a
posting with one, a posting with no location staying.

There is no word match: a string not yet classified excludes nothing for at
most the one cycle that classifies it. A wrong row is a PUT to
/admin/locations, never a deploy; the sweep never re-asks about a string that
has a row.

## An admin closes a posting the way the closed check does

POST /admin/jobs/{id}/close writes a rejected closed verdict naming the admin
and the reason, so the posting leaves every board on the next read and nothing
re-runs. `active` stays the catalog's fact about whether the board still lists
it. The report row says `posting_closed` from the same verdict.

## Authorisation

**Route-level authorisation says nothing about object-level authorisation.**
Owning a parent does not imply owning a child: a nested identifier must be
checked against the caller, not assumed from the path.

Any authenticated request auto-provisions a user row. Treat authentication as
a write.

## Response shapes

**A vocabulary the frontend renders is served with its meaning, never copied.**
Board statuses come with `status_meta` (terminal, outcome), report kinds with
their labels, pipeline stages with their order and which are terminal, task
rows with `cancellable`, tunables with type and help. The frontend renders
from the response and keeps no parallel list; a new value is one backend entry.

**A set-shaped filter takes a comma list and the response echoes `filters`.**
`status=pending,running` goes through `api.params.csv`. The envelope carries
`filters: {status: [...]}` with what was applied, empty when nothing was, so a
client can tell "lists accepted" from an older build by the key's presence.

**A view is a name that returns a page to a state.** `saved_views` holds, per
user and per page, the filters, the sort order across columns, the columns and
the search a person wants back; several per page, one default, ordered. The
state is the page's canonical request shape, the same names the API echoes in
`filters`, `sorts` and `sortable`, never a build's private wording, so a view
outlives the frontend that wrote it. A single column layout or filter set on
`user_settings` is the pre-view form.

List endpoints sort through `api.sorting` against a per-endpoint whitelist of
column expressions. `sort=a,b&dir=asc,desc` is several columns at once, and a
`dir` shorter than `sort` repeats its last value. Unknown keys drop rather
than refuse, and the response echoes `sorts` as applied and `sortable`. **A
sort parameter never reaches SQL as text.**

**Every admin list that returns rows a person owns takes `user=<id>[,<id>]`,
and the predicate per table lives in `api/scoping.py`.** Rows, summaries and
totals narrow together. The envelope carries `filterable`, the parameters the
endpoint filters on, beside the `filters` echo, so a client renders a User
control from the former. An endpoint with no user dimension (fleet workers,
the shared checks, source analytics) leaves `user` out of `filterable` rather
than pretending.
