export function createSubmissionTracker({ on, reader, surface, lifecycle, send, render, esc, mount, fieldByKey, getFill, getSubmission, setSubmission, setShowing, setPanelUp, setMountedFor }) {
  let submission = getSubmission();
  function finals() {
    return getFill().fields.map((entry) => {
      const field = fieldByKey(entry.key);
      const value = field ? reader.current(field) : null;
      const typed = (value || "") !== (entry.value || "");
      return {
        key: entry.key,
        final: value,
        // What the person typed or picked, and a model answer they left in
        // place. Never free text: that is per job, and the drafts cover it.
        remember: !!value && (typed || entry.rung === "ai") && entry.kind !== "long" && entry.kind !== "file",
      };
    });
  }

  let submissionBusy = false;
  let submissionView = null;
  let clearingSubmission = false;
  const CONFIRMED = /thank you for (applying|your application)|application (has been )?(received|submitted)|we('ve| have) received your application|we got your application|successfully submitted/i;
  const confirmationVisible = () => {
    if (reader.submitted()) return true;
    const pageText = document.body.innerText.replace(surface.text(), "");
    return !reader.ready() && CONFIRMED.test(pageText);
  };

  function showSubmission() {
    if (!submission) return;
    setShowing("submission");
    setPanelUp(true);
    surface.ensure();
    // The earlier watcher stopped at 30 seconds. Keep watching after that
    // point, but offer the person a way to confirm a missed success signal.
    const overdue = Date.now() - submission.startedAt >= 30000;
    const view = `${submission.status}:${overdue}:${submission.result?.status || ""}`;
    if (view === submissionView) return;
    submissionView = view;
    if (submission.status === "recorded") {
      render(`<div class="result-heading"><span class="eyebrow">Submission recorded</span><h4>${submission.result.json.job_id ? "Saved to your board" : "Application recorded"}</h4><p>${submission.result.json.job_id ? "Your job is marked Application Submitted." : "This submission is saved in your application history. No matching board job was found, so no board status changed."}</p></div>`);
    } else if (submission.status === "confirmed") {
      render(`<div class="result-heading"><span class="eyebrow">Submitted · not yet recorded</span><h4>Save your submission</h4><p>The application was confirmed, but Job Tracker has not saved it yet. Retrying will not submit the application again.</p></div><button id="jt-retry-record" class="primary">Retry saving</button>${submission.result?.signin ? `<p><a href="${esc(submission.result.signin)}" target="_blank" rel="noopener noreferrer">Sign in to Job Tracker</a>, then retry.</p>` : ""}`);
      on("click", "jt-retry-record", () => confirmSubmission("retry"));
    } else {
      render(`<div class="working" role="status"><span class="spinner" aria-hidden="true"></span><div><h4>${overdue ? "Still awaiting confirmation" : "Checking your submission"}</h4><p>${overdue ? "If the form shows errors, correct them and submit again. If it succeeded, you can confirm it below." : "Waiting for the application site to confirm success. Your board has not changed yet."}</p></div></div><button id="jt-confirm-record" class="primary">I submitted this application</button><button id="jt-dismiss-attempt">Back to autofill</button>`);
      on("click", "jt-confirm-record", () => confirmSubmission("confirm"));
      on("click", "jt-dismiss-attempt", async () => {
        const res = await send({ kind: "submission", action: "clear", fillId: submission.fillId });
        if (!res.ok) return;
        submission = null;
        setSubmission(submission);
        submissionView = null;
        setMountedFor(null);
        mount();
      });
    }
  }

  async function confirmSubmission(action) {
    if (!submission || submissionBusy) return;
    submissionBusy = true;
    for (const id of ["jt-retry-record", "jt-confirm-record"]) surface.mark(id, { disabled: true });
    const res = await send({ kind: "submission", action, fillId: submission.fillId });
    submissionBusy = false;
    if (res.ok) { submission = res.state; setSubmission(submission); }
    else {
      // The worker may have saved the receipt before its reply was lost.
      const saved = await send({ kind: "submission", action: "get" });
      if (saved.ok && saved.state) { submission = saved.state; setSubmission(submission); }
      else {
        render(`<p class="warn">Could not reach the extension. Reload this page to recover the saved submission attempt.</p>`);
        return;
      }
    }
    submissionView = null;
    showSubmission();
  }

  const armSubmission = async () => {
    if (submissionBusy) return;
    submissionBusy = true;
    const res = await send({ kind: "submission", action: "arm", fillId: getFill()?.fill_id || null, url: location.href, title: document.title, fields: getFill() ? finals() : [] });
    submissionBusy = false;
    if (res.ok) {
      submission = res.state;
      setSubmission(submission);
      submissionView = null;
      showSubmission();
    } else {
      render(`<p class="warn">Could not start submission tracking. ${res.stale ? "Reload this page to reconnect the extension." : "Your application can still submit, but its board status may need updating manually."}</p>`);
    }
  };
  function start() {
    lifecycle.addEventListener(document, "click", (ev) => {
      const button = reader.submitButton();
      if (button?.contains(ev.target)) armSubmission();
    }, true);
    lifecycle.addEventListener(document, "submit", (ev) => {
      const button = reader.submitButton();
      if (button && ev.target === button.form) armSubmission();
    }, true);

    // A single-page app changes the url and the page without a load, so the
    // form is watched for rather than assumed at load time.
    if (submission) {
      showSubmission();
      if (submission.status === "confirmed") confirmSubmission("retry");
    } else mount();
    lifecycle.setInterval(async () => {
      if (submission && submission.status !== "confirmed" && reader.ready() && new URL(submission.url).pathname !== location.pathname) {
        if (clearingSubmission || submissionBusy) return;
        clearingSubmission = true;
        await send({ kind: "submission", action: "clear", fillId: submission.fillId });
        submission = null;
        setSubmission(submission);
        submissionView = null;
        setMountedFor(null);
        clearingSubmission = false;
      }
      if (submission) {
        if (submission.status === "watching" && confirmationVisible()) confirmSubmission("confirm");
        else showSubmission();
      } else mount();
    }, 700);
  }
  return { start };
}
