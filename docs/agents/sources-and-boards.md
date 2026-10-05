# Sources and boards

How postings enter the catalog: what a source is, how a board is paced, what
every pull stores, and when a row goes inactive. For the detectors that watch
these, see [observability.md](observability.md).

## A source is a row, never a code path

The format is read off its listings URL by `core/fetching/boards.py`. A new board in a
known format is added on the Sources page, and a new format is one fetcher
returning the same `JobPosting` as the rest.

The row carries what ingest needs and nothing derived: `company` (required
where the system never names it), a `title_pattern` that normally gates which
titles enter the catalog, and an `ingest_interval_hours`.

`source_title_patterns_enabled` is the fleet-wide admission switch. When true,
only titles matching a source's pattern enter `jobs`. When false, every fetched
posting enters `jobs`; the pattern remains stored and `listings.kept` continues
to record whether it matched. Turning enforcement back on retires unmatched
rows on the next authoritative pull, so the experiment is reversible without
discarding its counterfactual.

`sources.active = false` stops both the scrape and every AI check on that
board's postings, and retires the postings themselves within the hour (below).
It keeps every subscription to it: a person can keep or leave a switched-off
board, only not join it. A bundle (`source_groups`) or a
format is a way of selecting rows for that flag and the interval through
`POST /admin/sources/switch`, not a second layer of state.

The list shape (`GET /admin/sources`) carries everything but `title_pattern`;
one row (`GET /admin/sources/{name}`) carries it. At 1,732 sources the pattern
was more than half of a 1.8 MB body, and only the edit form reads it.

## A board host is paced per egress address, and the pace is learned

`host_budget` holds one row per upstream host and egress address. A worker
takes the host's next slot for its address before a pull (`api.hosts`), and
the claim query skips pulls whose slot is closed, so the queue is a buffer the
host's own rate drains.

A 429 doubles the gap and defers the pull (`Deferred`: back to pending with
`not_before`, no attempt spent); a good pull narrows it toward the floor in
`ingest_host_pace_seconds`.

apply.workable.com refused 143 of 172 boards the hour a bundle first pulled,
on one address two workers shared. That is why the row is per address, and why
the worker names its address in `JOBTRACKER_EGRESS_GROUP`. The
`ingest_host_failing` detector names the host when pulls fail against one
upstream. A worker idle beside pulls whose slots are closed is not stalled.

## Everything a board returns is stored, once

Every pull records every listing in `listings`, matched by the pattern or not.
Each row holds the posting text the listing call carried and the raw record
minus that text. Greenhouse (with `content=true`), Lever and Ashby carry the
text; it is assembled by the same `core/fetching/ats.py` helpers the resolvers use, so
it is what a per-posting fetch would have returned. Workday, SmartRecruiters,
Oracle Recruiting and Workable list without the text, so their postings get it
from the matching resolver, one call each, when a check needs it.

Rows are aged out by `screened_retention_days` after the board stops listing
them.

**A pull rewrites a listings row only when the row would change.** Equal
content is filtered out before the insert, so an unchanged row gets no new
version, no lock and no WAL. Before this, every pull rewrote every row it
listed: 1.18M upserts a day, 99.2% of them unchanged, 9.17 GB of WAL a day and
19% of the cluster's (pg_stat_statements, 2026-10-03). Four rules keep it
that way, and a change to the upsert must keep all four:

- Compare what the row would hold after the update, not what the board sent.
  `date_posted` keeps the first date seen, so a board that dates by age
  ("Posted 3 Days Ago" on Workday, "5d" on the markdown lists) sends a new
  timestamp every pull and that is not a change. An empty description means
  the listing did not carry the text, and keeps the stored one.
- Filter in the `SELECT` feeding the insert, never in a `WHERE` on
  `DO UPDATE`. `DO UPDATE` locks the conflicting row even when its `WHERE`
  refuses the update, and the lock is a logged page write.
- Set `description` and `raw` from the old row when they are equal. Postgres
  reuses a TOASTed value only when it is handed the old row's own pointer. A
  value from `EXCLUDED` is a fresh copy and is written out again in full.
  A field's reference columns (below) move with it: all four from the old
  row or all four from the new one, so no reference outlives its value.
- No index on a column a refresh moves. `last_seen_at` was in the source
  index, and no update could be heap-only (860 of 1.17M a day). The index is
  on `source` alone, with fillfactor 80 so a page has room for the new
  version.

`last_seen_at` is refreshed only once it is older than
`listings_seen_refresh_hours` (24 by default; retention counts whole days, so
a day is the finest distinction it can draw). It can therefore lag the last
pull that listed the row by up to that interval. The admin screened list
returns it with that lag. Retention counts from `last_seen_at` plus the
interval, so a row is never deleted sooner than `screened_retention_days`
after its last listing, and at most the interval later. A listed row is never
deleted, because its pull refreshes it before the delete runs.

A candidate pattern is judged against this table (`pattern-preview`) before it
replaces the live one. A posting a wider pattern admits arrives in `jobs` on
the next pull, and ingest stores the carried text as the posting's content
instead of fetching the page.

Scraping is the action that gets the fleet blocked, so a backtest or a
backfill reads this table rather than asking a board twice. Nothing downstream
reads it.

## A listing's text and raw record can be held by reference

`description` and `raw` were 1.3 GB of TOAST on about 650k rows (about 595 MB
and 630 MB compressed, from a 5% sample, 2026-10-03), and the only thing that
read them was the upsert's own change check. Each field can instead be a
member of a verified bundle object: `<field>_sha256` is the SHA-256 of
`encode_payload(value)` and names the member, `<field>_object` is the bundle's
SHA-256 (the key is `payloads/v3/sha256/<hex>.json` in the configured bucket)
and `<field>_object_size` its length. The three are set together or not at all
(`ck_listings_<field>_reference`). A referenced field's inline column holds its
default, `''` or `'{}'`, which is not the value. An empty description is never
referenced: `''` with no reference is "the board never carried the text".

**Every reader of `description` or `raw` selects `listing_payloads.COLUMNS`
and goes through `listing_payloads.resolve`.** Selecting the inline columns
alone reads `''` and `'{}'` for every referenced row. A reference that cannot
be read, or reads a value whose digest differs, raises `PayloadUnavailable`;
it is never a listing without text. On 2026-10-03 nothing outside the upsert
read either column: pattern preview and the screened list select title, url,
company and dates only.

**The change check trusts a digest only beside its reference.** A stored
referenced value equals an incoming one when the digests match; an inline
value is compared as itself. A writer that stores a value inline writes no
digest, so no older writer can leave a digest that disagrees with the value
beside it.

**A pull writes references, uploaded before its upsert.**
`listing_payloads.reference_columns` reads the reference columns of the rows
the pull lists (never their text), reuses a stored reference wherever an
incoming value's digest matches one, and uploads every other value with
`PayloadStore.put_bundle`, outside any transaction: one bundle per pull, split
at `BUNDLE_MAX_BYTES`, members named by their own digest. A re-pull of
unchanged content uploads nothing and writes no row version, as before.
Objects are content-addressed and never deleted; an upload whose rows lost a
race stays unreferenced.

**An outage writes inline, never fails the pull.** Unconfigured or failing
storage puts the values that needed an upload inline with no digest, and the
pull counts them as `listings_inline` on its task and in a
`listings_stored_inline` event. Failing the pull instead would stop the
catalog upsert and the page caching that ride on it, for an archive nothing
downstream reads. Nothing stays half moved: an inline value never equals a
referenced one in the change check, so the next pull that lists the row moves
it, and the backfill reaches rows no pull lists again (switched-off sources,
rows waiting out retention). An empty description cannot be compared, so a
pull without text leaves a stored inline description to the backfill.

**Rollout order.** Deploy the compatible reader (the release that added the
columns) to every API and worker before the reference writer: an older
writer compares `description IN ('', stored)` and would write new text
inline beside a reference every reader still follows. Rolling back past the
reference writer is safe to the compatible reader, which writes inline and
clears the reference it replaces; rolling back past the compatible reader
needs `restore` over the whole table first.

`python -m api.migrate_listings MODE --limit N` is the backfill. Each
invocation takes at most N rows after `--after URL` in url order, in pages of
`--chunk-size` rows (default 1,000) with `--workers` pages in flight. Repeat
from the printed `after` until `exhausted` is true.

- `count` classifies rows `inline` or `referenced` without reading TOAST or
  objects.
- `externalize` reads a page's inline values, uploads them as bundles outside
  any transaction, then in one short transaction locks the page with
  `FOR UPDATE SKIP LOCKED`, requires each row unchanged, and replaces each
  value with its reference and the column default. A row a pull holds or
  rewrote in between is `changed`: left as it is, listed under `failed`
  (exit 1), and reached by a rerun. An upload failure is `unavailable` and
  stops the cursor before its page, with nothing written.
- `verify` reads every reference through `resolve`, each bundle once per
  page; a missing or altered object is `unavailable`.
- `restore` is the rollback: it writes each referenced value back inline and
  clears its reference, leaving the row exactly as an inline writer stored it.

Done means `count` from the start reports no `inline` and `verify` no
`failed`. One object per value would be about 825k PUTs (650k raw records
and 175k texts, the 27% of descriptions that are not empty) at about 16 a
second, over 14 hours before the read-back; a page of 1,000 rows is one
bundle, about 650 PUTs and their read-backs for the table. Each externalized
row is one new row version, so expect WAL about the size of the rows moved;
the TOAST it frees is reused after vacuum, and the files shrink only with a
rewrite, which is not part of this.

## A pull rewrites a catalog row only when the row would change

`catalog.upsert_postings` follows the first two listings rules above for
`jobs`. Rewriting every admitted row on every pull was 1.33M updates in 36
hours on a 605k-row catalog and 5.7 GB of WAL, while the feeds put back 5,336
postings in the same window (pg_stat_statements and `job_listing_events`,
2026-10-03).

- The filter is the update's `SET` evaluated against the stored row, column
  for column. A feed row keeps the company and title it was first stored with
  unless they are empty, `date_posted` keeps the first date seen, `raw_url` is
  written on insert only, and an `upload` row is taken over by the first feed
  that lists it. None of those is a change unless the `SET` would apply it. A
  change to the `SET` changes the filter in the same edit.
- It filters in the `SELECT` feeding the insert, never in a `WHERE` on
  `DO UPDATE`, for the reason the listings rule gives.

`jobs` has no seen-at or updated-at column for an unchanged pull to refresh,
and its columns stay inline (the largest `locations` was 1,680 bytes), so
neither the refresh interval nor the TOAST rule has anything to apply to. The
return events are read from the old row before the upsert, and a return
changes `active`, so every return is still both written and recorded.

## A company board's pull is the closure signal for its rows

After a pull from a board that lists every open posting
(`boards.AUTHORITATIVE`), every active catalog row of that source the pull did
not admit is set inactive: the board dropped it, or the pattern stopped
admitting it.

Inactive rows are excluded from every sweep and leave boards through
`demote_closed`. Listed and admitted again, the upsert reactivates them.

An aggregator list is not such a signal, and an empty pull is a broken fetch
rather than an empty board, so neither retires anything.

## A switched-off source holds no posting active

A source that is off is never pulled, so no pull will ever retire its rows,
and `active` on them stops meaning anything. On 2026-10-04 that was 50,994
active rows of 115 switched-off sources (24,736 of them `sr_domino_s`, about
9,800 the seven jobright aggregators).

`catalog.retire_switched_off` runs every cycle as the `retire_switched_off`
task and retires every active row of a switched-off source, logged in
`job_listing_events` like any other retirement. It is the one place this
happens, so every way a source goes off reaches it: the sources page, a bundle
switch, the automatic switch-off of a failing board, a direct write. A row
retires within the hour, not at the click. It is a no-op once the catalog
agrees, and skips rows a concurrent upsert holds rather than waiting on them.

The exception is a url that a switched-on source lists and would admit: its
`listings` row belongs to a source that is on, and is `kept` by that source's
pattern or pattern enforcement is off. That source's next pull would put the
row straight back and queue a re-check. A posting is therefore active while
some source that is still pulled says so, whichever source first stored it.
A url's `listings` row outlives its last listing by `screened_retention_days`,
so a row the other source stops listing retires up to that much later.

Retirement follows the ordinary path from there: inactive rows leave boards
through `demote_closed`, which removes only rows nobody has touched, so a
posting a person gave a status, applied to or wrote a note on stays on their board.
Switched back on, the source's first pull reactivates its rows through the
upsert, and each return is logged.

## A re-check answers both axes, because it has already paid for the page

The re-verification sweep asks `_VERIFY_INSTRUCTIONS` and records both the
closed and the clearance verdict, which is the same question the first pass
asks. It used to ask only whether the posting had closed, and that is why a
clearance verdict was written once and never again: nothing else revisits one.
On 2026-09-10 that held 12,444 active postings rejected on a clearance verdict
that had never been re-read, against 1,152 for closed.

The page is already fetched and the call is already made, so the second axis
costs its output tokens and nothing else. Usage books onto the first row
written and the second is a zero-token decided row, which is how
routers/spend.py tells a joint call apart.

Stale on one axis is stale on both. A parked batch carries page text as old as
its submission, so if anything decided either check after the batch went out,
the whole answer is dropped rather than the losing axis alone.

## A re-listing is the only thing that reopens a closed posting

A closed verdict was otherwise permanent. `demote_closed` takes the board row
away, the reverify sweep draws its candidates from board rows, and its full
run asks for verdicts that PASSED, so no path led back to the page: the
posting stayed closed forever on the copy fetched the moment it closed. On
2026-09-10 that held 1,125 postings whose feed still listed them.

So the catalog appends to `job_listing_events` when a feed changes its mind:
`listed` true when a pull puts a posting back, false when an authoritative
pull stops admitting one. The reverify sweep takes as a candidate any active
posting whose latest return is newer than its latest closed verdict. The
verdict itself settles it, because a fresh answer is newer than the return
that asked for it.

A row per change, never per pull. At 74,000 postings an hour, a row per
observation would be millions a day saying nothing changed.

`jobs.active` remains the current answer and the log is how it got there,
which is the question a boolean cannot answer. It is what makes a flapping
board countable, and a flapping board matters now precisely because a return
bills a re-check.

Key it on the edge, never on a timer. A sweep over everything ever closed
grows without bound and is mostly postings that can no longer change: 464 of
those 1,125 belonged to sources since switched off, and 295 were sheet
imports with no feed behind them. The edge costs one check per posting a feed
actually puts back.

It belongs to reverify and not to `verify_new`, which judges from the cached
page copy. For a posting that was closed, that copy is the one that showed it
closed; only reverify re-fetches (`verdicts.refresh_content`).

An aggregator row is never retired, so it has no edge and this cannot reach
it. That is a deliberate hole, not an oversight: those feeds hold a posting
active continuously, so "still listed" is a standing condition rather than an
event, and there is nothing to trigger on.

## A posting page is fetched by the cheapest tier that plainly worked

In order: the ATS resolver (an API call); then, when `fetch_engine` is
`static_first`, a browserless fetch with a real Chrome fingerprint
(`fetching.fetch_static`); then the browser. The static tier is accepted only
as an HTML 200 whose extracted text clears `static_fetch_min_chars` and reads
as a page.

A tier that returns None costs nothing downstream; the browser is the
guaranteed floor. Measured on 2026-09-04 over 431 pages: the static tier
recovered one browser-served page in seven whole, and every JavaScript shell
fell under the gate.

The content row's reason (`ats text`, `static`, `scraped`) is how the share
each tier serves is read, so the engine can be switched in config and judged
from the rows rather than assumed.

## A host that blocks bursts is drip-fed, not pulled off

`fetch_host_limits` (persisted config, host to page fetches per hour) paces
the browser fetch fleet-wide. `verdicts.host_paced` counts the hour's content
rows for the host and defers the fetch, writing nothing, so the next cycle
tries again.

The fetch-failure alert names the host, and adding it to the map is the way to
resolve that alert. No host is written into code.

## A page fetch that returns nothing leaves a record

It is a `content` row with `status = 'failed'` and no text. Without the record
the hourly cycle was the retry: every dead link, every hour, from every
worker, which was most of the fleet's block rate.

## A page that keeps failing is retried less often, then not at all

The run of failed `content` rows since the URL's last passed one decides
whether an automatic path may fetch it (`verdicts.fetch_parked_sql`). After
the k-th consecutive failure the wait is `fetch_retry_after_hours * 2^(k-1)`,
capped at `fetch_retry_max_hours`. At `fetch_give_up_after_failures` the
posting is unfetchable and nothing automatic fetches it again.

Every automatic fetch path asks this before fetching: ingest, the content
backfill, filter preparation, the live filter run, and reverify. A new
automatic caller of `refresh_content` asks it too. `refresh_content` itself
never asks, so an admin re-check (`POST /admin/checks/run`), a person's
explain and a forced reverify still fetch, and one success ends the run.

The state is derived from the rows, not stored. A flag beside them could
disagree with them, and the rows are already the only record of an attempt.
A host-paced deferral writes no row, so it never counts toward the run.

Before the give-up, 3,511 postings that had never fetched once carried
31,433 failed rows, 9 a posting on average and 50 at most, on a flat day each
(production, 2026-10-04). The admin Jobs list and its timeline show the run as
`content_failures` and `unfetchable`; a posting with no verdict reads
`unfetchable` instead of `other`.

## Scheduling and counts

**The scheduler queues one ingest per source, however far behind.** A pending
ingest blocks the next cycle's for that source; a running one does not. A
source on a longer interval than the cycle waits while its last successful or
in-flight pull is younger than the interval.

## A board that keeps failing is pulled less often, then switched off

A failed pull counts toward the interval, and a run of them backs off. After
the k-th failed pull in a row the board waits its own interval times 2^(k-1),
capped at `ingest_retry_max_hours` but never below the interval
(`queue.pull_wait_hours`). Before this a failed pull did not count, so a board
answering 404 was pulled every hour: twelve boards for up to eleven days, and
every board together failed 1,509 pulls in the week to 2026-10-04.

The pull that makes the run `ingest_give_up_after_failures` long switches the
source off (`tasks.ingest`). That retires its postings through the ordinary
path for switched-off sources, sends a `source_switched_off` event, and opens a
`source_switched_off` warning. The warning stays open until someone switches
the board back on or deletes it. A board switched back on is pulled at the next
cycle. One success ends the run. One more failure switches it off again.

The run is derived from the `ingest_source` tasks, never stored
(`queue.failure_runs`): failed tasks since the source's last done one. A task
that failed because the source was already off (`INACTIVE_SOURCE_ERROR`) never
asked the board, so it does not count. A 429 is a deferral, not a failure. The
`ingest_failing` alert reads the same run instead of a time window, because a
daily board's third failure now lands three days after its first.

A board that pulls fine and lists nothing is not switched off. An empty board
may be a company with no open roles, and the pull costs about one request a
day. Switching it off would miss the day it posts a role. Instead, one
`sources_never_produced` warning lists every switched-on board that has
existed for over a week, listed nothing in 8 days, and never had a posting or
a listing. There were 105 on 2026-10-04.

**Every ingest leaves its counts on its task** (`fetched`, `kept`, `cached`,
`fetch_failed`, `gone`, `already_cached`, `skipped_recent_failure`). They are
the only record of what one pull saw, and they are what the board detectors in
`api/health/boards.py` and the admin ingest summary read. A board that pulls fine and
delivers nothing is visible as exactly that.

The knobs above (`fetch_retry_after_hours`, `fetch_retry_max_hours`,
`fetch_give_up_after_failures`, `ingest_retry_max_hours`,
`ingest_give_up_after_failures`, `screened_retention_days`,
`listings_seen_refresh_hours`, `queue_stall_minutes`, `ingest_backlog_cycles`) are `app_config` rows, not
constants; see [engineering-standards.md](engineering-standards.md).

## Public availability from ATS detail endpoints

A successful detail response can retain a closed posting's description.
SmartRecruiters `active=false` or `visibility=INTERNAL` means unavailable to
the public-board audience, even at HTTP 200. Missing flags are not evidence
of closure. The resolver returns `GONE` before extracting retained text.

Embedded Greenhouse URLs carry a job ID but may omit the board token.
`refresh_content` resolves that token from the job's configured catalog source
when it is a Greenhouse API listing URL. The original posting URL remains the
verdict key and browser fallback. An explicit board's HTTP 404/410 is terminal;
a hostname-derived guess returning 404 is inconclusive and must not close a
posting. Never replace an authoritative response with a later guess.
