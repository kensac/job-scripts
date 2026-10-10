# Sources and boards

How postings enter the catalog: what a source is, how a board is paced, what
every pull stores, and when a row goes inactive. For the detectors that watch
these, see [observability.md](observability.md).

## A source is a row, never a code path

The format is read off its listings URL by `core/fetching/boards.py`. A new board in a
known format is added on the Sources page, and a new format is one fetcher
returning the same `JobPosting` as the rest.

**Every request a fetcher makes goes through `core.fetching.client`.** Board
pulls, ATS resolvers, form reads and the aggregator feeds share one session:
one user agent, one timeout, one transport retry (three retries on a 5xx or
a dropped connection, idempotent methods only) and the host's page pace. A
fetcher builds its request and parses the answer; it never sets any of those.
A 429 is returned at once, never retried there, because the host budget
answers a refusal for the whole address. A fetcher does not catch its own
failures: a feed that answered with something it cannot parse fails the pull,
so the failure run backs it off and switches it off. Before the aggregator
fetcher raised, it returned an empty list after its own retries; `Loop`
(a Rippling page, not a JSON feed) read as 151 empty pulls in the week to
2026-10-10.

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

**A paged listing stops on the count its first page states.** Workday sends
`total` on the first page only and 0 on every later one; a fetcher that re-read
it per page stopped after two pages, and because the pull is authoritative,
retired everything past them as closed. On 2026-10-05 that held 130 of 454
Workday sources at exactly 40 postings (Boeing listed 752). A source pinned at a
round multiple of its format's page size is the symptom to look for.

**An iCIMS board is one of two formats, and the listings URL says which.**
A portal (`https://careers-<tenant>.icims.com/jobs/search`, kind `icims`) is
HTML: cards in pages of 20 or 50 as the tenant set it, `pr=` zero-based while
the "Page 1 of 35" heading is one-based, and `pr=` past the end a 200 with no
cards. The heading counts pages, never postings, so the pull is complete only
when it read every stated page, every page but the last held as many cards as
the first, and no posting came twice; anything else is a sort that shifted
mid-pull, and the fetcher raises `PartialPull`. Fields are whatever the tenant
labelled them: a location is a field named "Location", "Job Location" or "Job
Locations" ("Seat Location" is a building), and a date only where the tenant
shows "Posted Date". A tenant that moved to iCIMS's hosted career site
(Jibe, kind `jibe`) answers its portal with a script redirect, and the fetch
fails naming the URL to store instead: `https://<careers host>/api/jobs`.
That API is JSON, `page=` one-based, `limit=` at most 100, `totalCount` on
every page, and each job carries its full text and its employer's name. The
portal search, the portal's own `sitemap.xml` and Jibe's `totalCount` agreed
posting for posting on 16 tenants on 2026-10-05.

**An offset walk needs a sort that cannot tie.** IBM's careers search sorts an
empty query by score and page views, every posting ties, and its 20 shards
break the tie differently per request: a `from`/`size` walk of 2,002 postings
returned 1,890 distinct on 2026-10-05, and the 112 it never saw would have been
retired as closed. `_ibm` walks by `search_after` on `_id` instead, which reads
the index to its end (2,002 of 2,002), and keeps the first page's count only as
the check. Before trusting an offset, pull a board twice and count distinct
urls against the stated total.

**A refusal inside a 200 fails the pull.** A GraphQL endpoint answers a bad
request with HTTP 200, `errors` and no data; Goldman's roleSearch does so for a
`pageSize` above 250. Read as a page, that is an empty board. `_goldman`
raises on `errors`.

## A posting has one URL, whichever feed spelled it

`jobs.url` is the posting's identity, and every row, verdict and link keys on
it, so two spellings of one posting are two postings. Every URL that enters
from outside goes through `ats.canonicalize`, directly or through
`urls.normalize_url`: the board fetchers, the sheet-era lists, uploads, the
apply panel's lookup (`forms.posting_urls`) and mail matching. A host whose
postings arrive under several spellings gets a canonical form there, chosen
as the one spelling every form reduces to without a request. amazon.jobs is
the worked case: the board lists `/en/jobs/<id>/<slug>`, the aggregators link
`/jobs/<id>/apply`, every form redirects to the slug page, and the slug
cannot be derived from the id, so the canonical URL is `/en/jobs/<id>`. A
host that is not an applicant-tracking system gets a function, not a
resolver, because a resolver's markers also make its host an ATS mail domain.
Rows stored before a host's rule existed keep their old spelling; nothing
rewrites `jobs.url`.

## A board host is paced per egress address, and the pace is learned

`host_budget` holds one row per upstream host and egress address. A worker
takes the host's next slot for its address before a pull (`api.hosts`), and
the claim query skips pulls whose slot is closed, so the queue is a buffer the
host's own rate drains.

A 429 doubles the gap and defers the pull (`Deferred`: back to pending with
`not_before`, no attempt spent); a good pull narrows it toward the floor in
`ingest_host_pace_seconds`. A response carrying `x-amzn-waf-action` is the
same refusal: an AWS WAF challenge, which Eightfold answers with a 405.
Counted as a failure it would switch the board off.

**A pace is kept under one key, and `core.fetching.hosts.pace_key` is the
only thing that computes it.** The `host_budget` row a pull takes, the
`ingest_host_pace_seconds` entry, the page pace inside a pull
(`client.pace`, which every pull request waits on whatever its format) and a
form read's budget all ask it, so a refusal learned by one is the pace of the
others. A key can cover many hosts: Workday tenants are `myworkdayjobs.com`,
every Greenhouse host is `boards-api.greenhouse.io`, and every Eightfold
tenant is `eightfold.ai` whatever domain it serves from. One WAF fronts the
Eightfold tenants, and once it challenged an address on 2026-10-05, Lockheed
Martin, Northrop Grumman, CACI, PayPal and Netflix all answered 405 for a few
minutes (Microsoft's tenant did not). About 110 requests a minute from one
address held for ten minutes; the limit itself was not measured. A change to
the key folds the rows kept under the old one in the same change, widest gap
and summed counts, or they sit on the host budgets page and in the blocked
detector forever (`bc1978f126b4`). A host matched against a URL prefix
(`fetch_host_limits`) is the literal `hosts.hostname`, not the pace key.

apply.workable.com refused 143 of 172 boards the hour a bundle first pulled,
on one address two workers shared. That is why the row is per address, and why
the worker names its address in `JOBTRACKER_EGRESS_GROUP`. The
`ingest_host_failing` detector names the host when pulls fail against one
upstream. A worker idle beside pulls whose slots are closed is not stalled.

## Everything a board returns is stored, once

Every pull records every listing in `listings`, matched by the pattern or not.
Each row holds the posting text the listing call carried and the raw record
minus that text. Greenhouse (with `content=true`), Lever and Ashby carry the
text; it is assembled by the same `core/fetching/ats.py` helpers the resolvers
use, so it is what a per-posting fetch would have returned. ByteDance carries
it too, as a description and a requirement, and Jibe carries it; neither has a
resolver. Amazon carries it too (the description and both qualification
lists); it has no resolver, so a re-check fetches the posting page. Goldman
carries it (`ats.goldman_text`), and its resolver reads the same text back
from the site's GraphQL `role`. Workday,
SmartRecruiters, Oracle Recruiting, Workable and Apple list without the text,
so their postings get it from the matching resolver, one call each, when a
check needs it. An iCIMS portal card holds a snippet, which is not
the text and is not stored; the iCIMS resolver reads the text from the
posting's frame. Taleo, IBM and Avature list without the text and
no resolver returns it, so their postings get it from the page fetch tiers
below. Every careers.ibm.com page answers the static tier with an AWS WAF
challenge (202), so IBM's text always comes from the browser (7 of 7 listed
postings read on 2026-10-05); the static tier read Bloomberg's and Two Sigma's
Avature posting pages whole the same day. Eightfold lists without the text,
and its resolver reads it from the tenant's detail endpoint.

**A board on its employers' own domains is known by its sources, never by a
URL's shape.** An Eightfold tenant posts on its own host
(`jobs.northropgrumman.com/careers/job/<id>`), so `refresh_content` looks for
a source whose listings URL is an Eightfold search on the posting's host and
hands that URL to `ats.resolve`; without one the resolver does not answer.
A posting host that differs from its listing host (Bayer lists on
bayer.eightfold.ai and posts on talent.bayer.com) is not resolved. The page is
a shell that clears the static gate: on 2026-10-05 the static tier returned
139,000 to 163,000 characters for eight Lockheed and Northrop postings, the
title and the site's theme JSON with no part of the description, and a removed
posting's page is the same shell. So a posting with an Eightfold source whose
resolver did not answer skips the static tier and goes to the browser.

**A board whose page is a shell gets a resolver, because the static tier
cannot tell a shell from a posting.** The static tier accepts any page whose
extracted text clears `static_fetch_min_chars`, and a careers site's navigation
alone can clear it. Apple's posting page builds its text from JSON embedded in
the page: on 2026-10-05 the static tier returned 3,268 to 3,322 characters for
nine postings, none of them the posting's own text, and a check would have
judged the site's menu. The `Apple` resolver reads
`GET https://jobs.apple.com/api/v1/jobDetails/{id}` instead (45 of 45 listed
postings, 2,014 to 6,655 characters, every one with its qualifications). Its
404 is the closure: 68 of 68 requisitions the board no longer listed answered
it. Its 200 is not proof of listing, since one requisition the board no longer
listed still answered 200 with full text, so that closure comes from the
board's authoritative pull. A new board gets the same check before it is
switched on: does the static tier's text contain the posting.

**A resolver says GONE only on an answer the board gives a closed posting and
nothing else.** Measure it against ids the board no longer lists (an
aggregator's history against a fresh pull) and ids it does, and open a few of
each in a browser. Anything that also answers an outage or a made-up id is
ERROR, and the page tiers and the board pull decide. Goldman's `role` gives
44 of 44 delisted roles the same INTERNAL_ERROR a made-up id and an upstream
failure give, and their pages read "Oops, something went wrong", so the
`Goldman` resolver never says GONE. IBM's Avature host redirects a closed
posting to `/careers/Error` before any WAF challenge: 19 of 19 delisted ids
did, and 9 of 10 listed ids went to the posting instead. The tenth was a
posting the search index still listed and IBM's own page called closed, so
the `Ibm` resolver reports that closure, and a closed posting can stay in an
authoritative pull until it does. An open IBM posting is UNSUPPORTED there,
and its text comes from the browser.

Every resolver asks through `AtsResolver.get` or `post`, so a request that got
no answer is ERROR the same way everywhere. `from_response` (404 and 410 are
GONE) is for an endpoint whose path names the posting. A resolver whose
endpoint does not (Goldman's GraphQL gateway answers a missing role 200 and a
wrong path 401, measured 2026-10-10; IBM answers with a redirect) maps the
status itself, because a 404 there is the endpoint moving, and reading it as
GONE would close every posting on the board at once.

**A listing's text is stored only when it is the whole posting.** Ingest stores
a non-empty `description` as the posting's content and never fetches the page,
so a partial text is judged as if it were complete. IBM's index carries a
256-character snippet and a `body` that drops the headings, the education and
the years of experience (posting 134730: 3,668 characters of a 7,663-character
page, 2026-10-05), so `_ibm` stores neither. A Goldman role without
`descriptionHtml` (5 of 953) stores none, rather than its title and place.
A SuccessFactors feed item carries the posting's whole body as HTML, stored as
title, location and the cleaned body (L3Harris posting 1407143600: 4,312
characters against the page's 4,243-character job description, ending on the
same line, 2026-10-05).

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

**A listing points at the pattern that judged it; it does not copy it.**
`title_patterns` holds each distinct pattern text once, keyed by the SHA-256
of the text (a btree entry cannot hold a long pattern), and
`listings.pattern_id` points at the one whose match set `kept`. Readers join
it. A digest match is accepted only when the text is equal
(`catalog.title_pattern_id`), and rows are never updated or deleted, so a
pointer cannot outlive its text. The upsert's change check compares
`pattern_id`. The copy this replaces was 492 MB of the table's 863 MB of row
data for 4 distinct values (1,060,806 rows, 2026-10-10).

`listings.pattern` is that old copy and nothing reads or writes it. A pull
empties each row it rewrites; `drop_listing_pattern_copies`
(`tasks/listing_patterns.py`), queued each ingest cycle until a run starts
with none left, empties the rest. Then the column is dropped (migrations.md).

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

**Every pull is also recorded as observations.** `catalog.observe` appends
to `source_observations` what the pull says about each catalog row whose
source's latest observation said something else: `appeared`, `reappeared`,
`filtered` (listed, the pattern does not match), `unlisted` (left out of a
complete authoritative pull, or flagged inactive by its feed) or `not_listed`
(left out of an aggregator's pull). A partial or empty pull records no
absence. Its inserts share-lock job rows through the foreign key, so they run
in url order in their own transaction. What each kind means, and the
availability projection read from them, is phase 3 of
[architecture-migration.md](architecture-migration.md).

**A pull that cannot prove it saw everything retires nothing.** Some Workday
tenants stop a search at 2,000 results (first page total=2000, later pages wrap
to the first): 19 of 355 measured on 2026-10-05, among them Airbus, NVIDIA and
Walmart. `_workday` then reads the tenant again one value of its own facets at
a time, the two widest facets whose every value is under the window, and raises
`boards.PartialPull` with the union. Ingest admits those postings but skips
`retire_unlisted`, because a posting outside the slices is not evidence of a
closure; re-verification closes them instead. Airbus went from 2,000 to 2,883
of about 2,940. Sources filter at our gate, the title pattern, and not at the
board: a listings URL carries no search. Oracle serves at most 10,000 rows of a search, newest first,
and a pull that ends short of the stated count raises `PartialPull` the same way.
So do IBM and Goldman. A role Goldman closes mid-walk shifts the next one back
past a page boundary unseen, and the count is what catches it.

**A Taleo careersection is read to its count, and its count is never met.**
The listings URL is the section's own search page,
`https://{tenant}.taleo.net/careersection/{section}/jobsearch.ftl?lang=en&portal={portal}`;
the section names the posting URL (`jobdetail.ftl?job={contestNo}`) and the
portal is what the search endpoint takes (a URL without it costs one GET to
read `portalNo` off the page). The endpoint,
`POST /careersection/rest/jobboard/searchjobs`, needs no cookie or token,
only a `tz` header, without which it answers 500. Its contract, measured on
2026-10-05:

- Pages are 25 rows and every page repeats `totalCount`. A page past the last
  returns the last page again (Kautex: pages 5 to 500 held the same posting),
  so the loop stops at the page the count implies, never on an empty page.
- Never send `pageSize`. It is echoed back, but the server caches a page by
  query and number, not size, for some minutes and for every caller: after a
  run at other sizes, a pull at 25 saw 100-row pages and 616 of Textron's 691.
  Postings are keyed by url, so a repeated row counts once.
- Pages come back short of 25 because the count includes requisitions the list
  never renders, on Bell the same ones under twelve sort orders and every
  job-type slice, so no re-reading recovers them. Textron listed
  691 of 751, Bell 111 of 133, Textron Aviation 97 of 106, AAR 187 of 193. A
  public posting can also be absent from the search entirely. So `_taleo`
  raises `PartialPull` whenever it holds fewer postings than the count, which
  on every section measured is every pull: Taleo boards close postings through
  re-verification, not by absence.
- Rows carry no text and no company. Which column holds the title, the
  locations (a JSON list inside a string) and the date is per section; the
  date is `10/02/2026` on Textron and `Oct 5, 2026` on AAR, both `lang=en`.

**A paged pull is held to its distinct postings, not its rows.** A sort with
ties lets each page request order the tied rows afresh, so a pass can return
exactly the stated count while serving some rows twice and others never.
jobs.apple.com does this: sorted newest, its managed pipeline roles carry the
request's own timestamp and tie at the top, and one pass on 2026-10-05
returned 6,192 rows of 6,192 with 6,190 distinct, the same in three runs.
`_apple` re-reads the pages that held those roles until the distinct count
reaches the stated one or a round finds nothing new (one round of five pages
recovered both), and raises `PartialPull` short of it. Its search body must
carry `format`: without it every page is an empty 200 stating 0, which reads
as an empty board.

**A board read by offset is complete only when it saw its stated count of
distinct postings.** ByteDance's careers API (TikTok and ByteDance, one
fetcher) pages by offset over a live board, so a posting closed between two
pages shifts the rest and one goes unseen; `_bytedance` counts distinct urls
against the first page's count and raises `PartialPull` when short. Its search
also stops at 10,000 rows: a request whose offset plus limit passes that
answers no rows and a count of 10,000, which a loop paging until an empty page
reads as the end of the board. The fetcher never asks past the window, and a
count at the window is a partial pull (2026-10-05; neither board was near it).

**A page advances by the rows it returned, never by the size asked for.**
Eightfold returns ten rows a page whatever `num` says, so a loop stepping by
its own page size reads one row in ten. Its listings URL is the tenant's
search endpoint with its `domain`
(`https://jobs.northropgrumman.com/api/pcsx/search?domain=ngc.com`, or
`/api/apply/v2/jobs?domain=` on a tenant still on the older site); a tenant
answers one generation and refuses the other with 403. Both state the count
on every page and answer an empty 200 past the end, with no result window up
to 21,774 (Starbucks, 2026-10-05). The search does not hold its order from
one request to the next: a posting re-dated or removed mid-pull shifts later
pages, and the order also moves while the count holds still (Qualcomm stated
2,052 on every page of two pulls on 2026-10-05, which held 2,009 and 2,043
distinct postings). `_eightfold` raises `PartialPull` when it holds fewer
distinct postings than the largest count any page stated, so on such a
tenant closures mostly come from re-verification.

**A listings URL names the board when the host does not.** Both ByteDance hosts
serve both boards; the `website-path` header picks one, and without it the API
answers 400. The listings URL therefore carries it as a query parameter
(`?website-path=tiktok`, `?website-path=en`), sent as the header, and the
fetcher refuses a value it has no public posting page for.

**A capped count is not a count.** amazon.jobs stops a search at 10,000 rows
and reports `hits` 10,000 for any search that reaches it, so the unfiltered
search said 10,000 for a board of 22,295 (2026-10-05). It refuses a page
past the window with HTTP 200, `hits` 0 and an `error`, so an unchecked loop
reads that as the end of the board. `_amazon` reads one job category at a time,
because categories partition the board and none was near the window (the
largest held 3,279). The pull is complete only when the category counts sum to
at least the country facet's total, every category's count is under the window,
and each category returned as many distinct postings as its first page stated.
That last check catches a posting closing mid-read, which moves every later row
up and makes a page boundary skip an open one. Anything else raises
`PartialPull`. When a format's total can equal its window, treat that total as
"at least", never as the size of the board.

**A feed that states no count is checked against a list that does.** A
SuccessFactors Career Site Builder board (an employer's own domain, listings URL
`https://<host>/services/rss/job/`) publishes one RSS feed with no total and no
paging: it returns its first `rows` items, 20 without the parameter, and
ignores `startrow`, `start`, `page` and `offset`. `_successfactors` asks for
far more rows than any board holds, then compares the requisition ids against
the site's `/sitemap.xml`, which lists the same `/job/` urls; an id in the
sitemap and not in the feed raises `PartialPull`. The sitemap is either a
`urlset` (its namespace varies) or a Google Base RSS feed of every posting read
by `<link>` (Deere, Halliburton, Boston Scientific, SAP); any other shape
proves nothing and the pull is partial. On 2026-10-05 the two agreed exactly
on nine boards, 115 to 2,233 postings. Quirks the fetcher depends on:
the feed answers 406 unless the request accepts `application/rss+xml`; a
malformed query answers 200 with `<xml>Error: ...</xml>`, which is a failed
pull; the feed spans every language the site posts in (Hensoldt's English and
German search pages said 535 and 512, the feed held 1,051), so the search
page's "Results 1 to 25 of N" is not the board's count; and an item's title is
`<title> (<primary location>)`, and either half can carry parentheses of its
own ("Saône (Haute), FR" on Deere), so the location is the balanced group that
closes the heading.
The legacy `career<N>.successfactors.com/career?company=` site has no
unauthenticated list call (its search is a session-bound DWR call), so it is
not a board format.

**A board that builds its own next link is followed, never paged by hand.**
Avature has no listings API: a tenant's search page is server-rendered HTML in
a template the tenant designs, and the listings URL is that page on the
tenant's `*.avature.net` host (`https://twosigma.avature.net/careers/OpenRoles`),
which redirects to a custom domain where there is one. The custom domain
cannot be told from any other site by its URL. The tenant fixes the page size
(6 to 25 on 2026-10-05) whatever is asked, and names the offset parameter
itself: Siemens pages by `folderOffset` and answers its first page to every
`jobOffset`. So `_avature` reads only the page's `paginationNextLink`. The RSS
feed returns the same 20 items at any offset, and the sitemap lists no postings
on some tenants (Two Sigma), so neither is a listings source. Some templates
state the count ("1-12 of 348 results"), others none (Two Sigma, Koch) or
"999+" (Siemens). A search serves no row past offset 2,000 (Koch answers 406,
Siemens an empty page). A pull is complete when it reaches the stated count,
or, with no count, ends under that window; meeting a posting it already read
(the list moved or wrapped) or an article it cannot parse is partial too.
Fields sit wherever the template puts them: a place is a `list-item-location`
span, a field labelled Location, or an unlabelled span under the title, and
Pomerleau shows none. IBM's and Delta's tenants answer every page with a 202
challenge, also to a browser-fingerprinted client, so they are not sources
(IBM is read through its own search API, `_ibm`).

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

**A page that frames its posting is read through the frame.** An iCIMS
portal posting (`*.icims.com/jobs/<id>/job`) is the portal's chrome with the
posting in an iframe. The static tier reads the chrome, which is long enough
to clear the gate (GDMS, 7,417 characters of navigation), and the browser's
body text never includes a frame, so neither tier ever saw the posting. The
iCIMS resolver reads the frame (`?in_iframe=1`) and its JobPosting JSON-LD;
a posting that is not public answers 410, which is gone. A frame without
the JSON-LD is an error, never text.

A page fetch's `method` (`ats text`, `static`, `scraped`) is how the share
each tier serves is read, so the engine can be switched in config and judged
from `page_fetches` rather than assumed.

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

An Eightfold detail endpoint answers HTTP 404 for a position the tenant no
longer has (`Position not found` on pcsx, `Job with ID <id> not found` on
v2), and the resolver reads that as `GONE` only because a source vouches that
the host is the tenant's. On 2026-10-05 the 72 Qualcomm postings in an
aggregator's history split exactly on the board: the 30 Qualcomm no longer
listed answered 404 and the 42 it listed answered 200 with their text. A WAF
challenge (405) or a 429 is `ERROR`.
