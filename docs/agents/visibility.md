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

**One enabled filter per person.** Enabling a second is refused (409
`ONE_FILTER`, naming the one that is on) on create, on turning it on and on
adopting a preset; a disabled second may exist. One prompt holds every
condition, and a new account's first pass judges the whole open catalog
once per enabled filter (23,000 postings and 10 dollars for one account on
2026-09-08), so the rule is a cost rule as much as a product one. When the
shared weekly budget is spent, every refusal says so in numbers (spent of
cap, resets weekly) and names the way past it: the person's own key,
under AI & keys, which has no cap and is billed to them; the frontend's
onboarding checklist carries that as its fifth step.

**A non-admin's board keeps postings at most 30 days old.** The age window
(`criteria.max_age_days`) is 30 when a non-admin saves criteria without one
and 30 at most; a wider value is refused at the write (400 `MAX_AGE_DAYS`)
by `PUT /user/settings`, and every non-admin row was set to 30 on
2026-09-08. Admins (the `JOBTRACKER_ADMIN_GROUPS` groups) keep the full
range. Checked at the write only, by Kanishk's choice; a stored value is
what applies. `GET /user/usage` carries both caps as `limits`
(`enabled_filters`, `max_age_days`, null for an admin) beside the weekly
allowance, so the Usage page states every limit in one place instead of
each surfacing only as a refusal.

**A run may go past the shared cap without the cap moving.** `POST
/admin/filters/run` `{user_id, filter_id?, ignore_budget}` queues a
`run_filter` (or `run_all_filters`) task for any person; with
`ignore_budget: true` the task, and the chunks it splits into, load their
entitlement with the weekly cap lifted for that run alone, the spend still
recorded in `api_usage`. Only an admin can queue it (the person's own run
endpoints never set the flag), and the same flag on a task row written by
hand works the same way. Raising `group_budgets.weekly_token_budget` for a
run and putting it back is not the tool for this.

**A filter save does not re-judge the board unless the person's group is in
`filter_rejudge_on_change_groups`** (app_config, seeded `[]`, `"*"` for
everyone). Three edits on one new account cost 10.27 dollars in a day
(2026-09-08): every save queued a full live run under the new hash while the
previous run kept judging under the old one. Outside the list a save returns
`run_blocked: "DEFERRED"` with the sentence the page shows, and the hourly
ingest sweep judges the board under the new hash at batch price; the person's
Run button still runs it at once. The board thins to acted-on rows until
verdicts land, because visibility keys on the current hash; carrying old
verdicts across an edit is the larger change this flag defers.

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

Page-number lists use `api.pagination.Page` for bounds, offsets and metadata.
Rows, totals and summaries share the same selection; pagination never narrows
a total or summary. The user board's legacy cursor orders by descending ID
and echoes that actual order, regardless of requested sort.

## The ATS on the board

Every board row carries `ats`, the applicant tracking system its url lives
on (ashby, greenhouse, lever, workable, workday and a few more by host;
anything else is "other", usually the employer's own careers site, of
which a real board has a couple of hundred, so listing them would make
the select useless). One SQL expression, `ATS_SQL` in api.routers.jobs, feeds the row,
the `ats=` filter on GET /user/jobs and the counted list in
GET /user/jobs/options, so the filter offers what is on the board rather
than a fixed list. It exists because assisted apply works one ATS at a
time: "only the Ashby ones" is the first question a person asks of the
board once the extension can fill Ashby. The counts a select shows come
from `with_facets=true` on the list, taken under every other filter the
page has on, so they agree with what choosing one will show; the
board-wide counts in the options describe the board, not the lens.
