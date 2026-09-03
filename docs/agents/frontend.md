# User-visible surfaces

The frontend lives in a separate repository. These rules hold in both.

## Components

**Use the shared component kit. Do not hand-roll what it already provides.**
If a pattern recurs and the kit has nothing for it, add it to the kit and
convert the call sites — then delete the hand-rolled versions. Leaving both is
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
it. When a rail holds something load-bearing — a way out of the app, a warning
— that thing needs a home that no viewport can hide.

Wide content scrolls inside its own container. The page body never scrolls
sideways.

## Talking to the API

**The client, its types, and any test fixture can each drift from the server
independently.** Verify shapes against the server source, not against your
expectation or your own fixture.

Response schemas are frequently undeclared, so nothing mechanical catches a
response-shape disagreement. Where you can declare one, do — it is the only
place this class of drift becomes detectable.

**Eligibility is decided by the server, never by the client.** The server
declares what is available and why something is not; the client renders that.
A client that decides what may be offered must be changed every time the rules
change, and will disagree with the server in the meantime.

## Actions

One primary action per view. A page of rows each carrying a primary button is a
wall.

A control that navigates is a link, so it can be opened in a new tab. A control
that acts is a button. Quiet, dotted treatment is for links inside prose — used
beside a real control it reads as a caption.

An action whose effect reaches beyond the row must say so before it is taken,
and its response must report what it actually touched rather than acknowledging
success.
