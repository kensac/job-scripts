export function createPageFill({ state, render, operation, send, reader, allowed, readFields, taskApi, plain, esc, on, run, fieldByKey, adapterContext, pdf, put, settle, revealed, isFilled, askable, askModel, verify, trackFilledJob, show, autoReport, pageSignature, prefs, fingerprint, clickThrough, sleep }) {
  async function runPage() {
    state.showing = "flow";
    render(`<div class="working" role="status"><span class="spinner" aria-hidden="true"></span><div><h4>Checking autofill is on</h4><p>Reading the extension's configuration for this site…</p></div></div>`);
    const pol = await operation.wait(send({ kind: "policy", adapter: reader.host || "unknown" }));
    if (!pol.ok) {
      // Nothing valid cached and nothing fresh: new automation pauses, the
      // person keeps the page, manual Submit tracking and reporting.
      state.policy = null;
      render(`<div class="result-heading"><span class="eyebrow">Autofill paused</span><h4>Could not confirm autofill is enabled</h4><p>Job Tracker could not load the extension's configuration for this site${pol.reason ? ` (${esc(String(pol.reason))})` : ""}. Your application is still here to fill by hand; reporting and submission tracking still work.</p></div><button id="jt-autofill" class="primary">Try again</button>`);
      on("click", "jt-autofill", run);
      return;
    }
    state.policy = pol.config;
    if (!allowed("autofill")) {
      const why = state.policy.features.autofill.reason === "ADAPTER_DISABLED" ? "for this job site" : "for now";
      render(`<div class="result-heading"><span class="eyebrow">Autofill switched off</span><h4>Autofill is turned off ${why}</h4><p>Job Tracker has paused automatic filling here. You can complete the form yourself; reporting and submission tracking still work.</p></div>`);
      return;
    }
    // A config-driven reader runs from the table the API published for this
    // adapter when there is one, pinned for this fill; otherwise, and on any
    // failure, from the table bundled with the extension.
    state.recipeRevision = "bundled";
    if (reader.useConfig) {
      const rec = await operation.wait(send({ kind: "recipe", adapter: reader.host }));
      if (rec.ok && reader.useConfig(rec.config.recipe)) {
        state.recipeRevision = rec.config.revision.slice(0, 12);
      }
    }
    await readFields();
    render(`<div class="working" role="status"><span class="spinner" aria-hidden="true"></span><div><h4>Filling your application</h4><p>Matching ${state.fields.length} form items with your profile and saved answers…</p></div></div>`);
    const res = await taskApi("user/apply/resolve", "POST", {
      url: location.href,
      fields: state.fields.map(plain),
      step: state.step,
      fill_id: state.fill?.fill_id || null,
      resume_open: true,
    });
    if (!res.ok) {
      state.lastError = res;
      if (res.stale) {
        render(`<p class="warn">Job Tracker Apply was updated. Reload this page to continue.</p>`);
        return;
      }
      render(
        res.signin
          ? `<div class="result-heading"><span class="eyebrow">Sign-in needed</span><h4>Connect to Job Tracker</h4><p><a href="${esc(res.signin)}" target="_blank" rel="noopener noreferrer">Open Job Tracker</a> and sign in, then return here.</p></div><button id="jt-autofill" class="primary">Try again</button>`
          : `<div class="result-heading"><span class="eyebrow">Could not fill this page</span><h4>Let's try that again</h4><p>Job Tracker could not load your answers. Your application is still here.</p></div><button id="jt-autofill" class="primary">Try again</button><details class="help"><summary>Error details</summary><p class="warn">${esc(res.status)}: ${esc(JSON.stringify(res.json || res.error))}</p></details>`,
      );
      on("click", "jt-autofill", run);
      return;
    }
    state.fill = res.json;
    state.filled = new Map();
    state.reviewState = null;
    state.reviewMessage = "";
    for (const entry of state.fill.fields) {
      const field = fieldByKey(entry.key);
      const current = field && reader.current(field);
      if (current) state.filled.set(entry.key, { ok: true, value: current, kept: true });
    }
    // A field the reader leaves to the person is never offered to the model.
    for (const entry of state.fill.fields) if (fieldByKey(entry.key)?._person) entry.never_ai = true;
    // Repeated groups are filled from the profile's rows, not a value.
    adapterContext.profile = state.fill.profile || {};
    let file = null;
    if (state.fill.resume && state.fill.resume.has_pdf && allowed("resume_upload")) {
      const got = await operation.wait(pdf(`user/resumes/${state.fill.resume.id}/pdf`));
      if (got.ok) file = new File([new Uint8Array(got.bytes)], got.name, { type: "application/pdf" });
    }
    // Upload before filling text and reread controls after DOM activity.
    // Never clear populated fields to force a save: an async form may save
    // the blank and skip restoring a value it still considers persisted.
    let attached = false;
    for (const entry of state.fill.fields) {
      if (entry.rung === "resume") attached = (await put(entry, null, file)) || attached;
    }
    if (attached) {
      await settle();
      await readFields();
    }
    for (const entry of state.fill.fields) {
      if (entry.rung !== "resume" && entry.value != null && !state.filled.get(entry.key)?.kept) await put(entry, entry.value, null);
    }
    await revealed(file);
    // A search-backed picker that showed options nothing matched: the
    // model chooses among what was on offer, with the profile's value as
    // the hint, and its pick goes back through the reader as an exact.
    for (const entry of state.fill.fields) {
      const f = fieldByKey(entry.key);
      if (!isFilled(entry) && f && f._seen && f._seen.length) {
        entry.options = f._seen;
        entry.hint = entry.hint || entry.value || undefined;
        if (entry.kind === "text") entry.kind = "select";
      }
    }
    if (allowed("ai_suggestions")) await askModel(state.fill.fields.filter((e) => !isFilled(e) && askable(e)));
    await verify();
    if (state.fill.fields.some(isFilled)) await trackFilledJob();
    show();
    await autoReport(`filled page ${state.step + 1}`);
    // A form that spans pages: when this page has a Continue and no Submit,
    // go on to the next page and fill it too, until the page that submits.
    // Continue is not Submit; the person still clicks that.
    state.pageSig = pageSignature();
    if (prefs.autoAdvance && allowed("auto_advance") && reader.nextButton && !reader.submitButton() && reader.nextButton() && state.step < 12) {
      // Only a complete page is advanced: a required field still blank is
      // the person's to fill first (Workday, 2026-09-08: Continue pressed
      // with five required fields blank, and the page listed them).
      const blank = state.fill.fields.filter((e) => e.required && !isFilled(e) && e.kind !== "group");
      if (blank.length) {
        state.fill.stopped = `This page still needs ${blank.length} required field${blank.length === 1 ? "" : "s"}; fill them and press Continue yourself, or Fill again.`;
        show();
        return;
      }
      const before = fingerprint();
      clickThrough(reader.nextButton());
      for (let i = 0; i < 40 && fingerprint() === before; i++) await sleep(250);
      await settle();
      // The page's own verdict: a reader that can read the validation
      // scope lists the fields the form refused, and the loop stops there.
      const errors = reader.errors ? reader.errors() : [];
      if (errors.length) {
        state.fill.stopped = `The form refused the page: ${errors.slice(0, 6).join("; ")}.`;
        show();
        await autoReport("page refused");
        return;
      }
      if (reader.ready()) {
        state.step += 1;
        state.fields = await reader.read();
        state.fill = null;
        return runPage();
      }
    }
  }

  return runPage;
}
