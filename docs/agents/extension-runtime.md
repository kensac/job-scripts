# Extension runtime

Read this before changing `extension/runtime/application.js` or its modules.

`application.js` owns the session, adapter reader, panel event routing, operation
lease, and manual edit guard. Keep browser messages and the reader contract at
this boundary. The modules under `extension/runtime/` each own one part of the
application flow:

- `preferences.js` loads browser and account preferences and saves changes.
- `application-context.js` reads the person's profile, saved answers, and
  drafts. A request from an earlier page must not repaint the current page.
- `page-fill.js` runs one page of autofill and the decision to advance.
- `answer-review.js` owns saved answer review and draft edits.
- `report-capture.js` records the form state; `report-panel.js` handles the
  person's report note.
- `submission-tracker.js` arms, watches, confirms, and records the person's
  submission attempt. It derives final values from the form at arming time.
- `panel-shell.js` renders the shared panel frame and operation controls.

Pass changing fill and panel state to a module through accessors, so a later
page or review action sees the current record. Build the modules before
starting the submission watcher or offering the Autofill button. The extension
never presses the employer's Submit control. The person's click or form submit
arms tracking, and a confirmation signal or explicit confirmation records it.

Verify a runtime split with extension typecheck, build, unit tests, and the
browser tests. The browser suite exercises startup, form filling, review,
manual edits, and submission tracking, which static checks do not establish.

## Page reports

Keep all captured non-script markup without a length cap. The background worker saves a report in
local storage before sending it. It retries pending reports after a failed
request or worker restart. Split each report into bounded requests, and reuse
the same upload id and chunk indexes on retries. The API marks a report
complete only after every chunk is stored. A completed report keeps its chunks
as the page's durable copy; list and detail reads must not assemble the whole
page in the API process. Remove the local copy only after completion succeeds.

## Published recipes

Fetch a version 2 manifest, then fetch each chunk for its named revision.
Reject a missing or changed chunk. Check `digest_algorithm`: uploaded recipes
chain the SHA-256 of each chunk with its index and length. Start with 32 zero
bytes. For each chunk, hash the domain `recipe-chunk-v2\0`, the previous
digest, its 8-byte big-endian index, its 8-byte big-endian length, and its
SHA-256 digest. Older JSONB revisions hash the assembled bytes. Verify before
parsing the table or saving it in local storage. A failed refresh may reuse
only an unexpired verified copy. Otherwise use the bundled table. The public
chunk route remains readable by revision after a newer publish so a refresh can
finish while an administrator changes the current recipe.
