# Sources and boards

How postings enter the catalog: what a source is, how a board is paced, what
every pull stores, and when a row goes inactive. For the detectors that watch
these, see [observability.md](observability.md).

## A source is a row, never a code path

The format is read off its listings URL by `core/boards.py`. A new board in a
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
board's postings. It keeps every subscription to it: a person can keep or
leave a switched-off board, only not join it. A bundle (`source_groups`) or a
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
text; it is assembled by the same `core/ats.py` helpers the resolvers use, so
it is what a per-posting fetch would have returned. Workday, SmartRecruiters,
Oracle Recruiting and Workable list without the text, so their postings get it
from the matching resolver, one call each, when a check needs it.

Rows are refreshed per pull and aged out by `screened_retention_days` after
the board stops listing it.

A candidate pattern is judged against this table (`pattern-preview`) before it
replaces the live one. A posting a wider pattern admits arrives in `jobs` on
the next pull, and ingest stores the carried text as the posting's content
instead of fetching the page.

Scraping is the action that gets the fleet blocked, so a backtest or a
backfill reads this table rather than asking a board twice. Nothing downstream
reads it.

## A company board's pull is the closure signal for its rows

After a pull from a board that lists every open posting
(`boards.AUTHORITATIVE`), every active catalog row of that source the pull did
not admit is set inactive: the board dropped it, or the pattern stopped
admitting it.

Inactive rows are excluded from every sweep and leave boards through
`demote_closed`. Listed and admitted again, the upsert reactivates them.

An aggregator list is not such a signal, and an empty pull is a broken fetch
rather than an empty board, so neither retires anything.

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

It is a `content` row with `status = 'failed'` and no text, and nothing
retries that URL inside `fetch_retry_after_hours`.

Without the record the hourly cycle was the retry: every dead link, every
hour, from every worker, which was most of the fleet's block rate.

## Scheduling and counts

**The scheduler queues one ingest per source, however far behind.** A pending
ingest blocks the next cycle's for that source; a running one does not. A
source on a longer interval than the cycle waits while its last successful or
in-flight pull is younger than the interval; a failed pull does not count.

**Every ingest leaves its counts on its task** (`fetched`, `kept`, `cached`,
`fetch_failed`, `gone`, `already_cached`, `skipped_recent_failure`). They are
the only record of what one pull saw, and they are what the board detectors in
`api/health.py` and the admin ingest summary read. A board that pulls fine and
delivers nothing is visible as exactly that.

The knobs above (`fetch_retry_after_hours`, `screened_retention_days`,
`queue_stall_minutes`, `ingest_backlog_cycles`) are `app_config` rows, not
constants; see [engineering-standards.md](engineering-standards.md).
