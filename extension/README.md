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

"Report this page" sends the page as the extension saw it, with a note,
for triage: use it whenever a field was read wrong, filled wrong, or not
filled at all.

## Supported

- Ashby (`jobs.ashbyhq.com/<org>/<id>/application`): text, email, phone,
  url, number, textarea, file, yes/no buttons, choice questions (radio
  groups and searchable dropdowns), the location search box. Date pickers
  and Ashby's education history widget are left to you.

Forms embedded on an employer's own careers page in an iframe work
too; a form rendered inline by the ATS's embed script, with no iframe,
does not, because the extension only runs on the ATS hosts it names.
Readers are single-page: a paginated form with a proxy submit button
(Workday, Taleo, iCIMS and the rest of that tier) is out of scope until
readers gain a step model.

One reader per ATS lives under `readers/`. A reader exposes `ready`,
`read` (fields with key, label, kind, required, options), `fill`,
`current`, `submitButton` and `submitted` (the ATS's own confirmation
screen, which is what marks the posting submitted); `content.js` does
everything else.
