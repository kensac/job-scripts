# Job Tracker Apply

A Chrome extension that fills an application form from your Job Tracker
profile, drafts and remembered answers. You check the form and click its
submit button; the extension never does.

## Load it

1. Sign in at https://www.kanishksachdev.com/job-tracker in the Chrome
   profile you apply from. The extension uses that session; it cannot sign
   in for you, and when the session has expired the panel says so.
2. `chrome://extensions`, turn on Developer mode, Load unpacked, pick this
   directory.
3. Fill in your profile on the site (Applications page): the facts every
   form asks, plus your experience and education, and choose a default
   resume. A resume must be uploaded as a PDF for the extension to attach
   it; a pasted one has no file.

## Use it

Open a posting's application page on a supported ATS; the panel in the
corner appears when the form does, with or without a reload, and can be
minimised to a pill. Click Autofill: it fills what your profile, your
remembered answers and your drafts cover, then asks the model once about
whatever is still blank and fills that too, marked "ai". Consents and acknowledgements, files and date
pickers are always yours. Check everything, and click the form's own
Submit button: what you typed or picked, and every model answer you left
in place, is remembered for the next form with the same question
(free-text answers are not), and the posting is marked submitted on your
board.

A profile value may list alternatives in order of preference, such as
"South Asian | Asian" for ethnicity: a choice field takes the first
alternative the form offers, a text box takes the first alternative.

"Report an issue" sends the page as the extension saw it, with a note,
for triage: use it whenever a field was read wrong, filled wrong, or not
filled at all.

## Submission tracking

The panel separates an attempted submission, a confirmed application, and a
submission saved to your board. It watches the supported form's Submit
button and native form submission, including when you did not use Autofill.
An attempt survives redirects within the same tab, frame and origin while
the browser session stays open. The extension must run on the destination
page to observe its confirmation.

A confirmed submission that could not be saved offers Retry saving. That
retries the Job Tracker receipt, never the employer's Submit button. If no
confirmation appears, I submitted this application lets you confirm the
outcome yourself. A saved submission without a matching catalog job says
that no board status changed. Nothing infers success from a button click
alone.

Autofill preferences holds the free-text drafting and automatic page
advance switches. Filled items shows answer previews and their sources;
repeated sections still need review on the form. Theme and minimise controls
stay in the header.

## Server switches

Before each Autofill the extension asks the site's public configuration
route for this job site's adapter and pins the answer for that fill: four
switches (autofill, AI suggestions, resume upload, automatic page advance)
and whether the adapter is disabled. The response is data only, validated to
the byte, and cached in the browser for the lifetime the server names; a
cached disablement is never replaced by enabled defaults because a refresh
failed. With nothing valid to hand, Autofill pauses and offers Try again,
while reporting, manual Submit and submission tracking keep working. Page
advance needs the server's switch and your own. Selectors, event dispatch
and navigation never arrive through the switches; a new form operation is a
release.

The one exception, chosen for developer-mode and self-hosted installs, is the
table a config-driven reader runs from (the `ats/` files). Before each
Autofill the extension also asks the public recipe route for this adapter's
published table; if one comes back and decodes to the digest the server
named, that table is pinned for the fill, otherwise the bundled table is
used. A publish fixes a site whose form changed without a new package;
rollback is server-side. The payload is compressed and encoded, which keeps
the tables out of casual view and stops nobody who reads this decoder. The
Chrome Web Store's policy treats such a table as remote logic, so an install
from the store would run with this route switched off.

## Where the panel sits

The panel is in a shadow root, and its host is a manual popover, so the
browser puts it in the top layer. `position: fixed` alone is not enough: a
transform, filter, backdrop-filter, contain or perspective anywhere above it
makes that ancestor the containing block, and the panel then scrolls away
with the form. Nothing dismisses a manual popover, and nothing on the page
can paint over the top layer. The host itself is a box of no size, so the
viewport outside the panel stays the page's to click.

That covers a form the window holds directly. An employer's careers page
embeds the application instead (Greenhouse's embed is one: 6084px tall inside
an 805px window on `app.careerpuck.com`), and `position: fixed` inside an
iframe pins to that FRAME's viewport, so a panel mounted beside the form
rides the page down and out of sight. The top layer does not help, being
per-document.

So the two split by frame. `content.js` stays with the form, because the flow
cannot leave the form's document: the reader hands back DOM nodes, `capture()`
walks the markup around each field, and the fill types into the controls.
`panel.js` shows the panel, in the top frame when the form is embedded, and
the background worker relays each operation there and each event back, since
two content scripts in one tab cannot speak to each other. Everything the
frames send is one way: the flow paints by id and every event carries the
panel's input values with it, so nothing is ever read back across a frame.

This is what `scripting` and the `https://*/*` host permission are for: the
worker puts `panel.js` in the top frame of whatever page did the embedding,
which is not a host the extension can name in advance. Nothing else runs
there. The three named hosts under `host_permissions` are subsumed by that
pattern and stay because they record which API endpoints the worker calls.

`web_accessible_resources` publishes `panel.css` to the shadow root. Every
match pattern there must have the path `/*` exactly, whatever the content
script matches: Chrome refuses to load the extension otherwise, with
"Invalid value for 'web_accessible_resources[0]'. Invalid match pattern."

## Local panel preview

Run `python -m http.server 8768` at the repository root, then open
`http://localhost:8768/tests/extension/panel-preview.html?reset=1`.
The fixture uses fictional values and replaces every extension API call.
`state=error`, `state=empty`, and `state=loading` exercise resolve states,
and `policy=off` the paused panel a failed configuration read leaves.
Submitting the demo navigates to a confirmation page with a failed save;
remove `fail=1` from that URL to exercise recovery. No real application is
submitted. Run `node --test tests/extension/*.test.cjs` for regressions.

## Supported

- Ashby (`jobs.ashbyhq.com/<org>/<id>/application`): text, email, phone,
  url, number, textarea, file, yes/no buttons, choice questions (radio
  groups and searchable dropdowns), the location search box. Date pickers
  and Ashby's education history widget are left to you.
- Greenhouse (`job-boards.greenhouse.io`, `boards.greenhouse.io`, the
  `.eu` hosts, and the form an employer embeds on its own careers site,
  which is the same page in an iframe): the fields come from Greenhouse's
  public form API, fetched by the background worker, and are paired with
  the page by id; text, phone, file, checkbox lists, and the react-select
  dropdowns, which open only in a visible tab.
- Lever (`jobs.lever.co/<company>/<id>/apply`): text, select, radio and
  checkbox cards, textareas, file.

Forms embedded on an employer's own careers page in an iframe work
too (Greenhouse's embed is one); a form rendered inline by an ATS's
script, with no iframe, does not, because the extension only runs on the
ATS hosts it names.
Readers are single-page: a paginated form with a proxy submit button
(Workday, Taleo, iCIMS and the rest of that tier) is out of scope until
readers gain a step model.

Every other ATS under `ats/` (49 of them, Workday, SmartRecruiters, iCIMS,
Jobvite, Workable, Rippling and the rest) is filled by `engine.js` from
that data: fields that know their fact, custom questions found by
selector, and forms that span pages, advanced page by page up to the one
that submits. Education and experience sections are filled from the rows on your
profile, one entry at a time. Consents are filled: the extension relays
your consent.
Workday and the other enterprise systems need you signed in first; the
panel appears once the form does.

One reader per ATS lives under `readers/`. A reader exposes `ready`,
`read` (fields with key, label, kind, required, options), `fill`,
`current`, `submitButton` and `submitted` (the ATS's own confirmation
screen, which is what marks the posting submitted); `content.js` does
everything else.
