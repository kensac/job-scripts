# User-visible surfaces

The frontend lives in the `personal-portfolio` repository, under
`app/job-tracker/` and `app/job-scripts/`. These are the standards a change to
those surfaces must meet.

They are recorded here rather than there so a feature can be specified
completely from this repository: an API change and the surface that consumes
it are one piece of work, and whoever owns the frontend half needs to be told
what the whole thing is, not just the endpoint.

## Components

**Use the shared component kit. Do not hand-roll what it already provides.**
If a pattern recurs and the kit has nothing for it, add it to the kit and
convert the call sites, then delete the hand-rolled versions. Leaving both is
worse than either.

Consistency across pages matters more than any single page being clever. When
the same question is answered two ways on two pages, pick one and make it the
only one.

**A shared class that carries design tokens must not also paint a background.**
Applied to anything full-viewport, it covers the page rather than sitting on
it. Keep tokens and paint separable.

**Required parameters over defaulted ones** in shared helpers. A default is how
a wrong assumption hides inside shared code; requiring the caller to state it
makes a deliberate choice one visible line instead of an invisible property.

## The four states

Every asynchronous surface has four states: loading, empty, error, and loaded.
All four are part of the feature.

**A failure must never render as an empty result.** "You have no filters" when
the API is down is a lie the reader cannot detect. Empty means "there is
nothing"; error means "we could not find out".

**An early return on missing data makes every error branch below it dead
code.** If a component returns a skeleton while data is null, an error that
leaves data null shimmers forever. Check for that shape specifically.

Never swallow a failed fetch into an empty default.

## Mobile

Every surface works at 390px wide. Test at that width; a window resize that
reports success is not proof the viewport changed.

**Anything that exists only in a desktop-only element does not exist on
mobile.** A navigation rail hidden below a breakpoint takes its contents with
it. When a rail holds something load-bearing, a way out of the app or a warning,
that thing needs a home that no viewport can hide.

Wide content scrolls inside its own container. The page body never scrolls
sideways.

## Talking to the API

**The client, its types, and any test fixture can each drift from the server
independently.** Verify shapes against the server source, not against your
expectation or your own fixture.

Response schemas are frequently undeclared, so nothing mechanical catches a
response-shape disagreement. Where you can declare one, do. It is the only
place this class of drift becomes detectable.

**Eligibility is decided by the server, never by the client.** The server
declares what is available and why something is not; the client renders that.
A client that decides what may be offered must be changed every time the rules
change, and will disagree with the server in the meantime.

**A default is served, not assumed by the client.** `GET /user/settings`
resolves an unset `column_layout` from `board_default_column_layout` in
`app_config`. Render the returned layout for both a new account and a reset.

### Updates and resets

Do not infer update semantics from the HTTP verb or a nullable request type.
Check the request model and write path before building a form or reset action.

| Write | Omitted fields | Explicit values |
|---|---|---|
| `PUT /user/settings` | Preserve saved values | Null clears `column_layout`, `ai_model`, and `writing_style`. Null preserves `prefs`, `ai_params`, `criteria`, `bypass_sponsorship_filter`, and `email_digest`. Supplied objects replace the whole object; false is saved. |
| `PATCH /user/filters/{id}` and `PATCH /user/views/{id}` | Preserve saved values | Null is rejected. An empty patch is rejected. False, zero and empty objects are saved where the field allows them. |
| `PUT /user/profile` | Restore profile defaults | Replaces the entire profile. Unknown top-level keys are rejected. Send the complete intended profile, including collections. |

An empty or whitespace-only writing style clears the override. Empty criteria
clear the location/date constraints, but the non-admin age limit still applies.
A null model removes the saved choice; read the returned effective model and
availability rather than assuming which model will run.

The implementations are `SettingsPut` and `FilterPatch` in `api/models.py`,
`put_settings` in `api/routers/users.py`, and the request models and writers in
`api/routers/views.py` and `api/routers/apply.py`. Filter and view updates share
`api.updates.NonNullUpdate`; preserve its distinction between omission and null.

## Actions

One primary action per view. A page of rows each carrying a primary button is a
wall.

A control that navigates is a link, so it can be opened in a new tab. A control
that acts is a button. Quiet, dotted treatment is for links inside prose, used
beside a real control it reads as a caption.

An action whose effect reaches beyond the row must say so before it is taken,
and its response must report what it actually touched rather than acknowledging
success.

## Application answer updates

A manual draft request reserves the selected answer generations when queued;
it supersedes older automatic work. Edits, clears and refinements invalidate
older results. A refinement that loses this race returns `409 ANSWER_CHANGED`:
keep the newer answer and reload it instead of replacing it with the response
from an earlier request. The model call can still have consumed usage.

Automatic drafting only fills untouched answers. An explicit clear remains a
person's edit and must not be treated as a request for another automatic draft.
The request and write rules live in `api/application_writes.py` and
`api/routers/application.py`; do not reproduce generation checks in the client.

## Spend reporting

Use `/admin/spend`'s `ledger.totals`, `ledger.by_model`, `ledger.by_day`, and
`ledger.by_purpose` for recorded usage estimates. They share one population;
daily buckets are UTC. `ledger_rows` counts usage records, which may contain
batch aggregates; the legacy `calls` field is the same record count. Show
`priced_calls`, `unpriced_calls`, and
`unknown_model_calls` alongside costs: a zero known subtotal can still have
unknown cost. The basis is `recorded_estimate`, not an invoice or proof that all
provider calls were recorded.

The old top-level totals and breakdowns remain compatibility fields for
`verdict_diagnostics`, not ledger spend. Verdict rows need not correspond to
unique calls; current source reach is not historical reach. The legacy
`batching.unrealized_savings_usd` is a hypothetical half-cost scenario. Missing
batch IDs and recorded batch flags do not establish historical transport, and
superseded verdicts do not establish wasted spend.

## Extension configuration

`GET /v1/extension/config?schema_version=1&adapter=<reader.host>` is public,
nonsecret configuration. The typed response grants bundled features only;
permission never overrides a person's preference. Admins replace the single
`extension_policy` object through the validated config registry. Rollback
replaces it with earlier settings; the content-derived revision returns with
those settings. Revisions are identities, not ordered counters.

The background worker owns the cache. Refresh before each fill, pin the
validated response during it, and use an unexpired compatible cache only when
refresh fails. Expired or invalid configuration pauses new automation; it must
never re-enable a cached disablement. Keep manual submission and receipt
recovery available. Report schema, revision, adapter and extension versions
with outcomes, not field values. Older extensions without this protocol ignore
these controls and require a packaged update first.

All selectors, DOM operations, conditions and navigation stay bundled. Never
serve the current ATS action recipes as remote configuration. New browser
operations require a store release; remote answers and feature switches do not
establish store approval or adapter stability.
