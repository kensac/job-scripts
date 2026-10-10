# Visibility and ownership

Who sees which posting, who owns a row, and the response shapes every list
endpoint keeps to.

## A board row is scope, not visibility

`user_jobs` answers two questions and is named for one of them.

An UNTOUCHED row is the working set. It makes the posting worth paying to
check (`core/store.py` `ON_A_BOARD`) and it is where the re-verification sweep
finds its candidates. It does not make the posting visible: FULL admits an
untouched row only through its structural branch, which never references
`user_jobs`. Deleting one removes nothing from anybody's board.

A TOUCHED row, one carrying a status, a note or a date applied, is the
person's. It is visible whatever the criteria or the verdicts say, and both
`materialize_passing` and `demote_closed` leave it alone.

Reading the first as the second is how a board question was answered wrongly
on 2026-09-10. `tests/test_worker_board.py` pins the two apart.

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

**A recompute writes only the rows that changed.** `visibility.store` deletes
the members that left, inserts the ones that joined and leaves the rest alone,
in one transaction under the person's advisory lock (7001, user_id). Replacing
the whole set rewrote 1.28M rows and 3.97 GB of WAL in 36 hours on 2026-10-03
for boards that had barely moved; an unchanged board now costs one row.

So a `board_visible` row's `computed_at` is when that posting joined the board,
not when the board was computed. The recompute's own time is
`board_visible_recomputes`, one row per person. Read it through
`visibility.computed_at` or `visibility.COMPUTED_AT`, never `MAX(computed_at)`
over the rows: the reader takes the later of the two, which is what a writer
that replaces every row would have reported, and is still correct while such a
writer runs beside this one. A person with no rows reads as never computed.
`tests/test_board_visible_diff.py` pins all of it, including that writer.

`FAST` enumerates the three authorized ID sets first: computed membership,
uploads, and acted-on rows. `UNION` deduplicates overlaps before joining jobs
and the current user's private state. Keep this set-first shape: an OR over
the whole catalog makes a small board scan unrelated postings. The regression
in `tests/test_board_read_plan.py` checks examined catalog rows rather than
elapsed time. Predicates, totals, facets and per-object checks still consume
the same `FAST` template.

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

**"Is this person an admin" has one answer: `api.auth.is_admin(groups)`.**
It reads `JOBTRACKER_ADMIN_GROUPS` (default `infra-admins`), and
`require_admin` is that test as a route dependency. Code never names the
group: the signup gate and the health alert recipients each hard-coded
`infra-admins` and ignored the variable, so renaming the group would have
moved admin routes and left signups and alert mail behind.

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

**A managed board decides its own clearance gate.** Board candidates pass
the same structural gates as a person's board, closed and clearance, with
`managed_boards.bypass_sponsorship_filter` standing where a person's
`bypass_sponsorship_filter` does (default false). The closed gate always
holds. Bypass is for a field the restriction defines rather than narrows:
94 of 117 decided aerospace-titled postings in the week to 2026-10-05 were
rejected on clearance, citizenship or ITAR. A viewer of a published list can
take those rows back out with `hide_restricted=true`, which drops a posting
whose latest clearance verdict is a rejection and keeps one with no verdict.

**Every title screen is a named recipe in `core/screening.py`.** A recipe
decides from title and source alone. It is an ordered rule list that `screen`
reads in Python and `skips_sql` writes as SQL, so the two spellings come from
one source, and `tests/test_screening.py` holds them equal on every recipe. A
recipe is never edited in place: the same test pins a digest of each, and a
different rule is a new name, measured before anything enforces it. The board
title gates, the filter review gate's title stage and the verification volume
gate's occupation list are separate recipes there, kept separate because each
was measured against its own consumers; widening one to another's list is a
measured change of its own.

**A board's title gate is the cheap rung before the model.** A new recipe ships in `shadow` mode: the board's first run
judges every candidate and its `title_gate_report` lists what the gate would
have dropped, which is the recall measurement. Only then is it set to
`enforce`. Measured on Tech Internships 2026-10-05, `internship_v1` skips
96.7% of calls and dropped 7 of 1,494 model keeps. `aero_major_v1` would drop
70 of the Aerospace board's 268 keeps and `new_grad_v1` 59 of Tech New Grad's
1,068 (2026-10-07), so neither is enforced.

**A board's question rides on the posting's verification request.** Every board
candidate has passed verification, so verification has always read the posting
first, and each board then paid to read the same text again. `verify_new` asks
each published board whose run would buy an answer
(`managed_board_runs.verification_questions`: its sources, criteria, enforced
title gate and title review gate admit the posting, it has no verdict under its
prompt and model, and it runs on verification's model and effort) inside the
same request (`core.answers.joint_verification`). The board's verdict is
written under its own prompt hash, and its run finds it through
`decided_custom_urls` like any cached verdict. A posting no board admits keeps
the plain verification request, byte for byte. The board's closed and
clearance gates still apply to the cached verdict when its run projects.

Judged against re-running today's separate requests, not against stored
verdicts: re-asking the identical request flipped 56 of 188 past Tech New Grad
keeps (2026-10-07). On the same postings the joint request kept 136 of 400 for
Tech New Grad against 133 (sign test p=0.76), 32 against 29 for Aerospace
(p=0.45), found 173 against 169 of 300 stored closures (p=0.34), and agreed on
clearance. Inside the joint request verification writes a reason only for an axis
it flags (a passed axis's reason is empty and the board shows none); measured
equal on every axis and 8 to 12% cheaper (2026-10-09). The call is booked once, to verification: a board's cost page counts
only what its own runs still buy. `verify_answers_board_questions` turns it off.

**A posting that is a near copy of a verified one is not read again.** Many
employers list one role per location with the same text. `verify_new` keys
every candidate with `core.near_copy.key` (title and text, short location lines
and numbers removed) into `jobs.near_copy_key`; a candidate whose twin from the
same source already holds closed and clearance verdicts takes those and the
twin's verdicts under every live board and filter prompt, as `verify-near-copy`
rows that cost nothing, and one whose twin is in this sweep or a parked one
waits for it. The posting's own location, date and gates still apply where each
board and filter reads the verdict. Measured on 51,114 postings verified
2026-10-06 to 10-08: 17.6% had a twin, and across 674 twin groups Tech New
Grad agreed in 673, Aerospace in 674, closed in 672 and clearance in 671
(re-asking the same request flips about 10% of borderline keeps).
`verify_near_copy_reuse` turns it off.

**Verification does not read what no board or filter keeps.** Reading a posting
is most of what a posting costs, and since the Workday paging fix (2026-10-05)
most new volume is store, clinic and warehouse roles reposted under the same
titles. `verification_volume_gate` lists the prompt hashes that opt in; for
those targets `verification_candidates.REACHABLE` skips a posting whose (source,
title) was judged `title_min_judged` (50) times in `window_days` with no keep by
any board or filter, except a fixed `audit_percent` sample of urls so a title
that starts producing keeps comes back by itself, and a posting whose title
the `occupation_words_v1` screen skips. Both dropped no keep on three held-out splits. A posting someone tracks is
always read. A new or edited prompt is not covered until its hash is added,
because both rules were measured against these prompts' keeps.

**A company's board that boards and filters do not keep is not read.** This one
is a volume decision, not a zero-loss one (Kanishk, 2026-10-09: "if there are
companies that aren't getting on boards I don't see merit in keeping them").
A source with `source_min_judged` (50) postings judged in `window_days` and a
keep rate at or below `source_max_keep_rate` (0) is skipped, with the same
audit sample. Over the 30 days to 2026-10-08 that was 164 sources, 22% of the
last week's judged volume, and none of their postings was kept in those 30
days; a 0.2% rate would reach 35% for 68 of 18,566 keeps. On held-out splits a
zero-keep source occasionally produced a later keep (Bank of America, RR
Donnelley); the audit sample is what returns such a source. #821 first shipped
this at 300 postings as if it were zero-loss, which is why it was briefly
removed (#825).

## A posting's path is one read, in the drawer

`api.posting_path` answers "why is this posting on, or off, this board or
filter" in the order the pipeline decides it: catalog, stored text,
verification (and where each verdict came from, including the twin a
near-copy verdict was copied from), then per board or filter: source,
criteria (each named criterion), the verification volume gate, the title gate,
the review gate, the closed and clearance gate, the verdict and its origin, and
membership. A step is `recorded` when a row says what happened and
`evaluated_now` when the rule leaves no row (criteria, title gates, the volume
gate, reachability decide by leaving a posting out of a SELECT) and is run
again for this posting today. GET /admin/jobs/path?url= covers every published
board and enabled filter; GET /user/jobs/{id}/path covers the caller's own
filters and board, gated like every per-job route. Both job drawers render it;
a new rule that reads, skips, judges or shows a posting adds its stage here, or
the drawer goes back to having no answer for it.

## Location criteria match places, not words

Every distinct location string a board writes is one row of `locations`,
classified once by a model into the places it names (country, region, city, as
many as it lists) and remote (`tasks.locations`).

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

## A person's claim about a shared verdict queues a recheck

closed and clearance verdicts are shared: `ai_queries` has no user column and
every board reads the latest row per (url, check_type). So nothing a person
runs on their own model settings writes one. `POST /user/jobs/{id}/explain`
runs a shared check on the caller's settings and records it under
`explain:<check>`, which no shared reader matches; a custom `filter:<id>`
verdict is the caller's own and is recorded as `custom`. When the caller's
answer disagrees with the standing verdict, and whenever a person files a
`closed` report, `api.reports.request_recheck` queues a forced single-row
`reverify_chunk`, the fleet's own fetch and model, deduped per url per UTC
day. The fleet's answer is the one boards read. `tests/test_cross_user_writes.py`
pins it.

## A person's settings have one owner

`user_settings` is read and written only by `api/user_settings.py`. It holds
six kinds of a person's data in one row: board layout and page preferences,
their own AI credentials, their criteria, digest state, the apply profile and
their writing style. Each kind has a typed read (`criteria`, `credentials`,
`prefs`, `profile`, `writing_style`, `digest_recipients`); a statement that
needs settings beside other tables takes its SQL piece from the module
(`join`, `has_own_key_sql`, `CRITERIA_LOCATIONS_SQL`). The defaults a person
without a row reads live there once. `tests/test_user_settings_owner.py`
fails on SQL naming the table anywhere else.

The kinds stay in one table. Measured 2026-10-10: two rows, no stored API
key, every kind read by person id. A table per kind adds a join to every
reader and removes nothing; revisit when a kind gains a reader that is not
by person, or a second row per person.

## Authorisation

**Route-level authorisation says nothing about object-level authorisation.**
Owning a parent does not imply owning a child: a nested identifier must be
checked against the caller, not assumed from the path.

A per-job route on the board resolves its job through
`api.board.access.require_visible_job`, never a bare `WHERE id = %s`. A route
that addresses a posting by something other than the board (the apply
extension's url match, its suggest call, the board row a submitted fill
writes) uses the touchable rule instead, `person_state.touchable_job_ids`:
the public catalog or the person's own upload, never another person's
private upload.

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

A comma list has two parsers and no others: `api.params.csv` for query
parameters and headers, `core.env.env_list` for environment variables (in
core so core can use it). User ids go through `scoping.user_ids`, which keeps
plain non-negative integers and drops the rest. `tests/test_comma_lists.py`
fails on a module that splits its own; one admin lookup's copy had drifted to
accept negative ids.

**A view is a name that returns a page to a state.** `saved_views` holds, per
user and per page, the filters, the sort order across columns, the columns and
the search a person wants back; several per page, one default, ordered. The
state is the page's canonical request shape, the same names the API echoes in
`filters`, `sorts` and `sortable`, never a build's private wording, so a view
outlives the frontend that wrote it. A single column layout or filter set on
`user_settings` is the pre-view form.

The user board retains exact scalar `status` and `source` filters because
stored values may contain commas. Their set parameters are `statuses` and
`sources`; each merges with its scalar counterpart and echoes under canonical
`filters.status` and `filters.source`. `ats` accepts a comma list and echoes
lowercase values. Totals use the full selection; ATS facets omit only the ATS
selection so they describe the available alternatives.

List endpoints sort through `api.sorting` against a per-endpoint whitelist of
column expressions. `sort=a,b&dir=asc,desc` is several columns at once, and a
`dir` shorter than `sort` repeats its last value. Unknown keys drop rather
than refuse, and the response echoes `sorts` as applied and `sortable`. **A
sort parameter never reaches SQL as text.**

Every sort key orders NULLS LAST, except keys the caller passes to
`sorting.clause` as NOT NULL, read off the ORM model rather than listed by
hand. Those get no NULLS clause: the order is the same, and `DESC NULLS LAST`
does not match a btree read backwards, so the index goes unused. The admin
queries list's default `id DESC NULLS LAST` was a seq scan of 2.07M rows, 4 s
warm; `id DESC` is 3.6 ms. A list sorting a NOT NULL column of a large table
passes its set.

**Every admin list that returns rows a person owns takes `user=<id>[,<id>]`,
and the predicate per table lives in `api/scoping.py`.** Rows, summaries and
totals narrow together. The envelope carries `filterable`, the parameters the
endpoint filters on, beside the `filters` echo, so a client renders a User
control from the former. An endpoint with no user dimension (fleet workers,
the shared checks, source analytics) leaves `user` out of `filterable` rather
than pretending.

**Lists page in one of four shapes, and a new list uses the first.** They
are not unified because each is a published contract the frontend reads.

| Shape | In | Out | Where |
|---|---|---|---|
| Page number | `page`, `page_size` | `page`, `page_size`, `total`, `has_more` | `api.pagination.Page`: admin queries and jobs, catalog reports, review gates, review decisions |
| Id or keyset cursor | `limit` and `before_id` or `cursor` | `has_more`, and `next_cursor` where there is a cursor | the task queue (`before_id`), `GET /user/jobs` (`cursor` is the last id, legacy), public job lists (opaque keyset on the sort value and id) |
| Offset cursor | `limit`, `cursor` (an offset as a string) | `has_more`, `next_cursor` | `/admin/companies` only |
| Limit and offset | `limit`, `offset` | `has_more`, sometimes `total` | `GET /user/jobs` without a cursor, and older admin lists: users, sources, screened postings, batch rows, spend rows, filter insights, the catalog |

Use a page number unless rows arrive while someone is paging and a skipped
or repeated row would matter; then use an id or keyset cursor. Do not add
the last two shapes.

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
