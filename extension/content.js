// The generic half of the extension. It runs on every page of an ATS
// host, watches for the application form to appear (the ATS is a single-
// page app, so the posting page becomes the form without a reload), and
// offers one button: Autofill. On the click it asks the API what goes in
// each field, fills, asks the model once for what is still blank, and
// shows what was filled and what was not. When the person clicks the
// form's own submit button and the ATS confirms, the final values go
// back so the bank and the ledger learn. It never clicks submit itself.
// The report button sends the page as the extension saw it, with a note.
(async () => {
  // THE PANEL IS NOT ALWAYS IN THIS FRAME.
  //
  // panel.js owns the panel's DOM, and says there why it is a shadow root in
  // the top layer. This file owns the flow, and the flow cannot leave the
  // form's own document: the reader hands back DOM nodes, capture() walks the
  // markup around each field, and the fill types into the controls.
  //
  // When the form is embedded in a cross-origin iframe, those two want
  // different frames. An employer's careers page embeds the application
  // (Greenhouse's embed is one: 6084px tall inside an 805px window on
  // app.careerpuck.com), and position: fixed inside an iframe pins to that
  // FRAME's viewport, so a panel mounted beside the form rides the page down
  // and out of sight. The top layer does not help, being per-document.
  //
  // So the flow stays here beside the form and the panel goes to the top
  // frame, where a window overlay is possible at all. `surface` is the same
  // object either way, and every call is one way: paint, region, mark,
  // ensure, remove. Nothing is read back across a frame.
  const embedded = window.top !== window.self;

  const reader = window.__jtReader || { ready: () => false, submitButton: () => null, submitted: () => false };
  // Stamped into every report, so a report from a build the person has not
  // reloaded yet is told apart from a bug (reports 9 to 11, 2026-09-08).
  const BUILD = "2026-09-09 policy-gates";

  // A message to the extension's background worker. After the extension is
  // reloaded, a page that was already open keeps the old script, whose
  // channel to the worker is gone: sendMessage throws "Extension context
  // invalidated" (Gusto, 2026-09-08). That comes back as a stale result
  // the panel turns into "reload this page", not an uncaught error.
  const STALE = { ok: false, stale: true, status: 0, error: "extension reloaded; reload this page" };
  const send = (msg) =>
    new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(msg, (res) => resolve(res === undefined ? STALE : res));
      } catch (_) {
        resolve(STALE);
      }
    });
  const api = (path, method, body) => send({ path, method, body });
  const pdf = (path) => send({ kind: "pdf", path });

  // Bound by id rather than by node, so a repaint never loses a handler and
  // the same registration works whether the control is in this document or
  // in the top frame's. Every event carries the panel's input values, which
  // is what makes the remote surface one way.
  const handlers = new Map();
  let onAi = null;
  const on = (type, id, fn) => handlers.set(`${type}:${id}`, fn);
  function dispatch({ type, id, ai, values }) {
    if (ai && type === "click") return onAi && onAi(ai);
    const fn = handlers.get(`${type}:${id}`);
    if (fn) fn(values || {});
  }

  // The panel in the top frame: the background worker puts panel.js there and
  // relays each operation, because two content scripts in one tab cannot
  // speak to each other directly. text() is empty rather than remote,
  // because a panel in another document is not in this one's innerText.
  const remoteSurface = () => {
    const op = (name, ...args) => send({ kind: "panel", op: name, args });
    return {
      paint: (...a) => op("paint", ...a),
      region: (...a) => op("region", ...a),
      mark: (...a) => op("mark", ...a),
      ensure: () => op("ensure"),
      remove: () => op("remove"),
      text: () => "",
    };
  };
  const surface = embedded ? remoteSurface() : window.__jtPanel.create(dispatch);
  if (embedded) {
    chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
      if (!msg || msg.kind !== "panel-event") return;
      dispatch(msg.event);
      reply({ ok: true });
    });
  }
  let submission = (await send({ kind: "submission", action: "get" })).state || null;
  if (submission && submission.status !== "confirmed" && reader.ready() && new URL(submission.url).pathname !== location.pathname) {
    await send({ kind: "submission", action: "clear", fillId: submission.fillId });
    submission = null;
  }
  if (!window.__jtReader && !submission) return;
  let submissionBusy = false;
  let submissionView = null;
  let clearingSubmission = false;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  // The model is asked about these; free text has the drafts, and a file
  // or a date picker is the person's.
  const ASKABLE = new Set(["text", "number", "select", "yesno", "multiselect"]);
  // Consents are filled like everything else: the extension relays the
  // consent of the one person it fills for, who asked for exactly that.
  // With the switch on, the free-text boxes go to the model as well (the
  // drafts cover the ones the board knew about). The admin's never-fill
  // list (location, for one) is applied by the API, which names the
  // fields it kept back so the panel lists them as the person's.
  // The panel's preferences: minimised, the AI switch, the theme. In
  // chrome.storage.local, which every ATS host shares, so a panel minimised
  // on one board stays minimised on the next; localStorage was per origin
  // and forgot between hosts (job-scripts-5c, 2026-09-08). Read before the
  // first paint so the panel never flashes the default.
  const prefs = { collapsed: false, aiAll: false, theme: null, autoAdvance: false };
  const loadPrefs = async () => {
    try {
      const got = await new Promise((r) => chrome.storage.local.get(["collapsed", "aiAll", "theme", "autoAdvance"], r));
      if (got && typeof got.collapsed === "boolean") prefs.collapsed = got.collapsed;
      else prefs.collapsed = localStorage.getItem("jt-apply-collapsed") === "1";
      if (got && typeof got.aiAll === "boolean") prefs.aiAll = got.aiAll;
      else prefs.aiAll = localStorage.getItem("jt-apply-ai-all") === "1";
      prefs.theme = got && (got.theme === "light" || got.theme === "dark") ? got.theme : null;
      if (got && typeof got.autoAdvance === "boolean") prefs.autoAdvance = got.autoAdvance;
    } catch (_) {
      // Nothing stored or no storage: the defaults hold.
    }
  };
  // The person's copy: user_settings.prefs.apply through the settings
  // endpoints, so the same panel greets them in every browser they sign in
  // to (Kanishk, 2026-09-08). The account's value wins over the browser's
  // cache at start; a change goes to both. Merged on write so the other
  // prefs on the row (auto_draft) keep.
  const PREF_KEYS = ["collapsed", "aiAll", "theme", "autoAdvance"];
  const syncPrefsFromAccount = async () => {
    const res = await api("user/settings", "GET");
    const mine = res.ok && res.json && res.json.prefs && res.json.prefs.apply;
    if (!mine || typeof mine !== "object") return;
    for (const k of PREF_KEYS) if (k in mine) prefs[k] = mine[k];
    try {
      chrome.storage.local.set({ collapsed: prefs.collapsed, aiAll: prefs.aiAll, theme: prefs.theme, autoAdvance: prefs.autoAdvance });
    } catch (_) {
      // The cache is a convenience; the account has it.
    }
  };
  const savePrefsToAccount = async () => {
    const res = await api("user/settings", "GET");
    if (!res.ok || !res.json) return;
    const all = { ...(res.json.prefs || {}), apply: { collapsed: prefs.collapsed, aiAll: prefs.aiAll, theme: prefs.theme, autoAdvance: prefs.autoAdvance } };
    await api("user/settings", "PUT", { prefs: all });
  };
  const savePref = (key, value) => {
    try {
      chrome.storage.local.set({ [key]: value });
    } catch (_) {
      // Nothing to remember it in; it holds for this page.
    }
    savePrefsToAccount();
  };
  await loadPrefs();
  await syncPrefsFromAccount();
  const askable = (e) => !e.never_ai && (ASKABLE.has(e.kind) || (prefs.aiAll && e.kind === "long"));

  let panelUp = false;
  let step = 0;
  let fields = [];
  const clickThrough = (el) => {
    for (const t of ["mousedown", "mouseup"]) el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true }));
    el.click();
  };
  let fill = null;
  let filled = new Map();
  let lastError = null;
  // The server's switches, pinned per fill (docs/agents/frontend.md, #494):
  // refreshed before each Autofill, held for that fill, nothing without it.
  let policy = null;
  // Which recipe table this fill ran from: the published revision, or "bundled".
  let recipeRevision = "bundled";
  const allowed = (name) => !!(policy && policy.features && policy.features[name] && policy.features[name].allowed);
  const VERSION = (() => { try { return chrome.runtime.getManifest().version; } catch (_) { return "unknown"; } })();
  let mountedFor = null;

  // The theme: data-jt-theme="light" or "dark" for an explicit choice, and
  // no attribute at all for "system", which follows prefers-color-scheme in
  // panel.css. The button cycles light, dark, system and names the next.
  const THEME_NEXT = { light: "dark", dark: null, null: "light" };
  const themeLabel = (t) => (t === "light" ? "Light" : t === "dark" ? "Dark" : "System");
  // The whole panel is repainted from prefs, so the theme button and the
  // minimise button say what prefs say without anyone patching them by hand.
  // A repaint is what a preference change does.
  let painted = "";
  const render = (html) => {
    if (!panelUp) return;
    painted = html;
    const themeTitle = `Theme: ${themeLabel(prefs.theme)}. Switch to ${themeLabel(THEME_NEXT[String(prefs.theme)])}`;
    surface.paint(`
      <div class="head">
        <div class="brand"><span class="brand-mark" aria-hidden="true">↗</span><div><h3>Job Tracker</h3><span class="eyebrow">Apply assistant</span></div></div>
        <div class="head-actions"><button id="jt-theme" aria-label="${esc(themeTitle)}" title="${esc(themeTitle)}">${prefs.theme === "light" ? "☀" : prefs.theme === "dark" ? "☾" : "◐"}</button>
        <button id="jt-min" aria-label="${prefs.collapsed ? "Expand panel" : "Minimise panel"}" aria-expanded="${!prefs.collapsed}" title="${prefs.collapsed ? "Expand" : "Minimise"}">${prefs.collapsed ? "+" : "−"}</button></div>
      </div>
      <div class="body">
        <div class="application-context"><span class="eyebrow">Current application</span><div class="application-title">${esc(matchedTitle() || submission?.title || document.title || "Application form")}</div><span class="muted">${esc(location.hostname)}${context ? (context.job ? " · on your board" : " · not on your board") : ""}</span></div>
        <div class="workspace">${html}</div>
        <details class="settings"><summary>Autofill preferences</summary>
          <label class="switch"><span><span class="setting-title">Draft unanswered text</span><span class="setting-description">Use AI for free-text questions too. Review before submitting.</span></span><input type="checkbox" role="switch" id="jt-ai-all" ${prefs.aiAll ? "checked" : ""}></label>
          <label class="switch"><span><span class="setting-title">Continue between pages</span><span class="setting-description">Advance after filling. Always stop before Submit.</span></span><input type="checkbox" role="switch" id="jt-advance" ${prefs.autoAdvance ? "checked" : ""}></label>
        </details>
        <div class="panel-footer"><div id="jt-report"><button id="jt-report-btn">Report an issue</button></div></div>
      </div>`, { theme: prefs.theme, collapsed: prefs.collapsed });
  };

  on("click", "jt-report-btn", reportForm);
  on("change", "jt-ai-all", (values) => savePref("aiAll", (prefs.aiAll = !!values["jt-ai-all"])));
  on("change", "jt-advance", (values) => savePref("autoAdvance", (prefs.autoAdvance = !!values["jt-advance"])));
  on("click", "jt-theme", () => {
    prefs.theme = THEME_NEXT[String(prefs.theme)];
    savePref("theme", prefs.theme);
    render(painted);
  });
  on("click", "jt-min", () => {
    prefs.collapsed = !prefs.collapsed;
    savePref("collapsed", prefs.collapsed);
    render(painted);
  });

  // Underscore keys are the reader's DOM handles; the API never sees them.
  const plain = (f) => Object.fromEntries(Object.entries(f).filter(([k]) => !k.startsWith("_")));
  const boxOf = (f) => f._box || (f._ctl && (f._ctl.closest("fieldset, .field, .application-question, .select-shell") || f._ctl.parentElement)) || null;
  const fieldByKey = (key) => fields.find((f) => f.key === key);
  const entryByKey = (key) => fill.fields.find((e) => e.key === key);
  const readFields = async () => {
    fields = await reader.read();
  };

  // The panel appears when a form is on the page and goes when it is not,
  // so the posting page shows nothing and the form page shows the button
  // without a reload.
  // The controls on the page by id or name: it changes when the form moves
  // to its next page under the same url (Workday), not when the person
  // types. Recorded after a fill pass; a change means a page the person
  // moved to by hand, and the panel offers Autofill for it.
  const pageSignature = () =>
    // The #jt-apply filter is belt and braces now rather than the mechanism: a
    // document query does not cross a shadow boundary, so the panel's own
    // switches and textarea are not in this list at all. It stays because it
    // costs nothing and tests/extension/panel.test.cjs encodes the bug it was
    // written for - opening the report box counted as the page changing and
    // reset the flow - and that guarantee should not come to depend on one
    // subtle fact about where the panel is mounted.
    [...document.querySelectorAll("input, textarea, select")].filter((e) => !e.closest("#jt-apply")).map((e) => e.id || e.name || e.type).join("|");
  let pageSig = null;
  function mount() {
    if (submission) return;
    const here = location.href;
    if (reader.ready()) {
      // A page that hydrates after load (Greenhouse's board is a Remix app)
      // can throw the panel out of the body with the rest of the markup it
      // did not render; put it back rather than believing it is there.
      surface.ensure();
      if (mountedFor === here && panelUp && fill && pageSig && pageSignature() !== pageSig) {
        pageSig = null;
        step += 1;
        fields = [];
        fill = null;
        filled = new Map();
        offer("The form moved to a new page.");
        return;
      }
      if (mountedFor === here && panelUp) return;
      mountedFor = here;
      fields = [];
      fill = null;
      filled = new Map();
      panelUp = true;
      offer();
    } else if (!fill && reader.applyButton && reader.applyButton()) {
      // The posting page, with the button that opens the form on it (the
      // selector table clicks it on 25 ATSs). Offered, never pressed unasked;
      // once the form appears the tick above takes over.
      if (mountedFor === here + "#open" && panelUp) return;
      mountedFor = here + "#open";
      panelUp = true;
      render(`
        <p>This is the posting. The application form is a click away.</p>
        <button id="jt-open" class="primary">Open the application</button>
      `);
      on("click", "jt-open", () => {
        const btn = reader.applyButton();
        if (btn) clickThrough(btn);
        mountedFor = null;
      });
    } else if (panelUp && !fill) {
      // No form and nothing recorded on it: the posting page, or a page
      // away from the form. A panel after a submit stays for its message.
      surface.remove();
      panelUp = false;
      mountedFor = null;
    }
  }

  // What this person already has for this page. Read once when the panel
  // appears and kept for the page: it opens no fill and writes nothing, so
  // asking again on every repaint would be a request that changes no answer.
  let context = null;
  let contextState = "idle";
  async function loadContext() {
    if (contextState !== "idle") return;
    contextState = "loading";
    const res = await api(`user/apply/context?url=${encodeURIComponent(location.href)}`);
    context = res.ok ? res.json : null;
    contextState = res.ok ? "ready" : "error";
    // Only if the offer is still what the person is looking at. They can
    // click Autofill while this is in flight, and a repaint then would throw
    // away the flow that click started.
    if (showing === "offer") offer(offerLead);
  }

  // A fact the person set, in the words the form uses. A list fact (their
  // education, their jobs) is a count: the rows are long and the question
  // here is whether there is anything to fill from, not what it says.
  const factRow = (key, value) => {
    const said = Array.isArray(value) ? `${value.length} ${value.length === 1 ? "entry" : "entries"}` : String(value);
    return `<li><div class="field-copy"><span class="field-label">${esc(humanKey(key))}</span><span class="field-value">${esc(said)}</span></div></li>`;
  };
  // Underscores to words, except the names that own their own capitals.
  const KEY_LABEL = { linkedin: "LinkedIn", github: "GitHub", url: "Website", address_2: "Address line 2", address_3: "Address line 3" };
  const humanKey = (key) => KEY_LABEL[key] || key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

  // Each source says how much of it there is, and opens on what it holds.
  // The counts are the state; nothing here sends the person to the website
  // to find out what the extension is about to use.
  function sources() {
    if (contextState === "loading") return `<p class="muted">Reading your profile, answers and drafts…</p>`;
    if (contextState === "error" || !context) {
      return `<p class="muted">Could not read your profile, answers and drafts. Autofill still works; it reads them again as it fills.</p>`;
    }
    const facts = Object.entries(context.profile || {});
    const answers = context.answers || [];
    const drafts = context.drafts || [];
    const section = (id, title, count, empty, body) => `
      <details class="field-section" id="${id}"><summary>${title} <span class="count">${count}</span></summary>
      ${count ? `<ul>${body}</ul>` : `<p class="muted">${empty}</p>`}</details>`;
    return [
      section("jt-src-profile", "Profile details", facts.length, "Nothing set yet.",
        facts.map(([key, value]) => factRow(key, value)).join("")),
      section("jt-src-answers", "Saved answers", answers.length, "Nothing kept from a submitted form yet.",
        answers.map((a) => `<li><div class="field-copy"><span class="field-label">${esc(a.label)}</span><span class="field-value">${esc(a.value)}</span></div><span class="tag">${esc(a.times_used)} used</span></li>`).join("")),
      section("jt-src-drafts", "Drafts", drafts.length,
        context.job ? "No drafts written for this posting yet." : "This page is not a posting on your board, so there are no job drafts.",
        drafts.map((d) => `<li><div class="field-copy"><span class="field-label">${esc(d.question)}</span><span class="field-value">${esc(d.draft)}</span></div></li>`).join("")),
    ].join("");
  }

  // The posting this page is, when the board knows it. The panel's own
  // heading says it, so nothing below has to repeat it.
  const matchedTitle = () =>
    context && context.job ? [context.job.title, context.job.company].filter(Boolean).join(" · ") : "";

  // Which view is painted, so a late answer cannot paint over a newer one.
  let showing = null;
  let offerLead = undefined;
  function offer(lead) {
    offerLead = lead;
    showing = "offer";
    render(`
      ${lead ? `<p class="notice" role="status">${esc(lead)}</p>` : ""}
      <button id="jt-autofill" class="primary">Autofill this page <span aria-hidden="true">↗</span></button>
      ${sources()}
    `);
    on("click", "jt-autofill", async () => {
      await readFields();
      run();
    });
    loadContext();
  }

  // A reader's trace of a fill, kept by key: the fields are read again
  // after a fill pass (revealed fields), and a fresh field object has no
  // trace, which left report 10 blank where report 8 had them.
  const traces = new Map();
  async function put(entry, value, file) {
    const field = fieldByKey(entry.key);
    if (!field) return false;
    let ok = false;
    try {
      ok = await reader.fill(field, value, file);
    } catch (e) {
      lastError = String(e);
      traces.set(entry.key, [...(field._trace || []), `threw: ${String(e)}`]);
    }
    if (field._trace && field._trace.length) traces.set(entry.key, field._trace);
    filled.set(entry.key, { ok, value: file ? file.name : value });
    return ok;
  }

  async function run() {
    showing = "flow";
    render(`<div class="working" role="status"><span class="spinner" aria-hidden="true"></span><div><h4>Checking autofill is on</h4><p>Reading the extension's configuration for this site…</p></div></div>`);
    const pol = await send({ kind: "policy", adapter: reader.host || "unknown" });
    if (!pol.ok) {
      // Nothing valid cached and nothing fresh: new automation pauses, the
      // person keeps the page, manual Submit tracking and reporting.
      policy = null;
      render(`<div class="result-heading"><span class="eyebrow">Autofill paused</span><h4>Could not confirm autofill is enabled</h4><p>Job Tracker could not load the extension's configuration for this site${pol.reason ? ` (${esc(String(pol.reason))})` : ""}. Your application is still here to fill by hand; reporting and submission tracking still work.</p></div><button id="jt-autofill" class="primary">Try again</button>`);
      on("click", "jt-autofill", run);
      return;
    }
    policy = pol.config;
    if (!allowed("autofill")) {
      const why = policy.features.autofill.reason === "ADAPTER_DISABLED" ? "for this job site" : "for now";
      render(`<div class="result-heading"><span class="eyebrow">Autofill switched off</span><h4>Autofill is turned off ${why}</h4><p>Job Tracker has paused automatic filling here. You can complete the form yourself; reporting and submission tracking still work.</p></div>`);
      return;
    }
    // A config-driven reader runs from the table the API published for this
    // adapter when there is one, pinned for this fill; otherwise, and on any
    // failure, from the table bundled with the extension.
    recipeRevision = "bundled";
    if (reader.useConfig) {
      const rec = await send({ kind: "recipe", adapter: reader.host });
      if (rec.ok && reader.useConfig(rec.config.recipe)) {
        recipeRevision = rec.config.revision.slice(0, 12);
        await readFields();
      }
    }
    render(`<div class="working" role="status"><span class="spinner" aria-hidden="true"></span><div><h4>Filling your application</h4><p>Matching ${fields.length} form items with your profile and saved answers…</p></div></div>`);
    const res = await api("user/apply/resolve", "POST", {
      url: location.href,
      fields: fields.map(plain),
      step: step,
    });
    if (!res.ok) {
      lastError = res;
      if (res.stale) {
        render(`<p class="warn">Job Tracker Apply was updated. Reload this page to continue.</p>`);
        return;
      }
      render(
        res.signin
          ? `<div class="result-heading"><span class="eyebrow">Sign-in needed</span><h4>Connect to Job Tracker</h4><p><a href="${esc(res.signin)}" target="_blank" rel="noopener noreferrer">Open Job Tracker</a> and sign in, then return here.</p></div><button id="jt-autofill" class="primary">Try again</button>`
          : `<div class="result-heading"><span class="eyebrow">Could not fill this page</span><h4>Let's try that again</h4><p>Job Tracker could not load your answers. Your application is still here.</p></div><button id="jt-autofill" class="primary">Try again</button><details class="help"><summary>Error details</summary><p class="warn">${esc(res.status)}: ${esc(JSON.stringify(res.json || res.error))}</p></details>`,
      );
      on("click", "jt-autofill", async () => {
        await readFields();
        run();
      });
      return;
    }
    fill = res.json;
    filled = new Map();
    // A field the reader leaves to the person is never offered to the model.
    for (const entry of fill.fields) if (fieldByKey(entry.key)?._person) entry.never_ai = true;
    // Repeated groups are filled from the profile's rows, not a value.
    window.__jtProfile = fill.profile || {};
    let file = null;
    if (fill.resume && fill.resume.has_pdf && allowed("resume_upload")) {
      const got = await pdf(`user/resumes/${fill.resume.id}/pdf`);
      if (got.ok) file = new File([new Uint8Array(got.bytes)], got.name, { type: "application/pdf" });
    }
    // Files first, then let the page settle before anything else: Ashby
    // parses an attached resume and rewrites its form a second or two
    // later, and a value set in that window is dropped from the form's
    // state while the widget still shows it (the Clera report, 2026-09-07).
    let attached = false;
    for (const entry of fill.fields) {
      if (entry.rung === "resume") attached = (await put(entry, null, file)) || attached;
    }
    if (attached) {
      await settle();
      await readFields();
    }
    for (const entry of fill.fields) {
      if (entry.rung !== "resume" && entry.value != null) await put(entry, entry.value, null);
    }
    await revealed(file);
    // A search-backed picker that showed options nothing matched: the
    // model chooses among what was on offer, with the profile's value as
    // the hint, and its pick goes back through the reader as an exact.
    for (const entry of fill.fields) {
      const f = fieldByKey(entry.key);
      if (!isFilled(entry) && f && f._seen && f._seen.length) {
        entry.options = f._seen;
        entry.hint = entry.hint || entry.value || undefined;
        if (entry.kind === "text") entry.kind = "select";
      }
    }
    if (allowed("ai_suggestions")) await askModel(fill.fields.filter((e) => !isFilled(e) && askable(e)));
    await verify();
    show();
    await autoReport(`filled page ${step + 1}`);
    // A form that spans pages: when this page has a Continue and no Submit,
    // go on to the next page and fill it too, until the page that submits.
    // Continue is not Submit; the person still clicks that.
    pageSig = pageSignature();
    if (prefs.autoAdvance && allowed("auto_advance") && reader.nextButton && !reader.submitButton() && reader.nextButton() && step < 12) {
      // Only a complete page is advanced: a required field still blank is
      // the person's to fill first (Workday, 2026-09-08: Continue pressed
      // with five required fields blank, and the page listed them).
      const blank = fill.fields.filter((e) => e.required && !isFilled(e) && e.kind !== "group");
      if (blank.length) {
        fill.stopped = `This page still needs ${blank.length} required field${blank.length === 1 ? "" : "s"}; fill them and press Continue yourself, or Fill again.`;
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
        fill.stopped = `The form refused the page: ${errors.slice(0, 6).join("; ")}.`;
        show();
        await autoReport("page refused");
        return;
      }
      if (reader.ready()) {
        step += 1;
        fields = await reader.read();
        return run();
      }
    }
    // Ashby parses an attached resume on its server and, when the answer
    // comes back seconds later, resets the form's own record of its fields
    // while the inputs keep showing what was typed (Clera, 2026-09-07: the
    // email on screen, "Missing entry for required field: Email" on
    // submit). The page gives no signal when that lands, so every filled
    // field is written again, as a real change, a few times after the
    // attach. A field the person has edited by hand is left alone.
    if (attached) {
      for (const wait of [2500, 3000, 4000]) {
        await sleep(wait);
        await resync();
      }
    }
  }

  async function resync() {
    if (!fill) return;
    let reread = false;
    for (const entry of fill.fields) {
      if (entry.value == null || entry.rung === "resume" || !isFilled(entry)) continue;
      let field = fieldByKey(entry.key);
      if (!field || !document.contains(boxOf(field) || field._ctl || null)) {
        if (!reread) {
          await readFields();
          reread = true;
        }
        field = fieldByKey(entry.key);
        if (!field) continue;
      }
      const now = reader.current(field);
      if (now && now !== entry.value && !entry.options?.includes(now)) continue;
      if (entry.kind === "text" || entry.kind === "long" || entry.kind === "number") {
        // Two real changes, so the form hears it even when the input already
        // shows the value.
        await reader.fill(field, "", null);
        await reader.fill(field, entry.value, null);
      } else if (!now) {
        await reader.fill(field, entry.value, null);
      }
    }
  }

  // The page as a string of its input values and element count; stable for
  // 1.5 seconds means whatever the attach set off has finished.
  const fingerprint = () =>
    [...document.querySelectorAll("input, textarea, select, button[aria-pressed]")]
      .map((e) => (e.value || "") + (e.getAttribute("aria-pressed") || ""))
      .join("") + document.querySelectorAll("*").length;
  async function settle() {
    let last = fingerprint();
    let quiet = 0;
    for (let i = 0; i < 16 && quiet < 3; i++) {
      await sleep(500);
      const now = fingerprint();
      quiet = now === last ? quiet + 1 : 0;
      last = now;
    }
  }

  // What the page holds after everything: a field the page reset is filled
  // once more, from fresh element references.
  // A form reveals fields as it is filled: Greenhouse's EEO block shows the
  // race question once Hispanic/Latino is answered (Bloomreach, 2026-09-08).
  // After a fill pass the form is read again; what appeared is resolved onto
  // the same ledger row and filled, and a reveal can reveal more, so up to
  // three rounds.
  async function revealed(file) {
    for (let round = 0; round < 3; round++) {
      await sleep(300);
      await readFields();
      const fresh = fields.filter((f) => !entryByKey(f.key));
      if (!fresh.length) return;
      const res = await api("user/apply/resolve", "POST", {
        url: location.href,
        fields: fresh.map(plain),
        step: step,
        fill_id: fill.fill_id,
      });
      if (!res.ok) return;
      for (const entry of res.json.fields) {
        if (fieldByKey(entry.key)?._person) entry.never_ai = true;
        fill.fields.push(entry);
        if (entry.rung === "resume") await put(entry, null, file);
        else if (entry.value != null) await put(entry, entry.value, null);
      }
    }
  }

  async function verify() {
    await sleep(400);
    const lost = fill.fields.filter((e) => {
      const field = fieldByKey(e.key);
      return field && e.value != null && e.rung !== "resume" && isFilled(e) && !reader.current(field);
    });
    if (!lost.length) return;
    await readFields();
    for (const entry of lost) await put(entry, entry.value, null);
  }

  const isFilled = (entry) => {
    const f = filled.get(entry.key);
    return !!(f && f.ok && f.value != null && f.value !== "");
  };
  // A repeated group is a field the model is never asked about.
  const ASKABLE_KINDS = ASKABLE;
  ASKABLE_KINDS.delete("group");

  // One call for everything still blank. The answers land in the form
  // like any other rung, marked "ai" so the panel and the ledger say so.
  async function askModel(entries) {
    if (!entries.length) return;
    render(`<div class="working" role="status"><span class="spinner" aria-hidden="true"></span><div><h4>Preparing answers</h4><p>Working through ${entries.length} unanswered form items…</p></div></div>`);
    const res = await api("user/apply/suggest", "POST", {
      job_id: fill.job_id,
      fill_id: fill.fill_id,
      fields: entries.map((e) => ({ key: e.key, label: e.label || e.key, kind: e.kind, options: e.options, hint: e.hint })),
    });
    if (!res.ok) {
      lastError = res;
      fill.ai_error = res.stale ? "extension updated, reload this page" : res.json?.detail?.code || res.status;
      return;
    }
    for (const key of res.json.skipped || []) {
      const entry = entryByKey(key);
      if (entry) entry.never_ai = true;
    }
    for (const [key, answer] of Object.entries(res.json.answers || {})) {
      const entry = entryByKey(key);
      if (!entry) continue;
      if (await put(entry, answer, null)) {
        entry.rung = "ai";
        entry.value = answer;
      }
    }
  }

  function show() {
    const done = fill.fields.filter(isFilled);
    const todo = fill.fields.filter((e) => !isFilled(e));
    const sourceLabel = { profile: "Profile", bank: "Saved answer", draft: "Job draft", ai: "AI answer" };
    const li = (e, extra = "", preview = false) => `<li><div class="field-copy"><span class="field-label">${esc(e.label || e.key)}</span>${preview ? `<span class="field-value">${esc(e.kind === "group" ? "Entries filled; review each on the form" : filled.get(e.key)?.value)}</span>` : ""}${e.ai_note ? `<span class="field-value">${esc(e.ai_note)}</span>` : ""}</div>${extra}</li>`;
    render(`
      <div class="result-heading"><span class="eyebrow">This page</span><h4>${!fill.fields.length ? "No fields detected" : todo.length ? "A few things to review" : "Ready for your review"}</h4><p>${!fill.fields.length ? "Open the application form, then try again." : "Check your answers on the form before continuing."}</p></div>
      <div class="metrics" aria-label="Autofill results"><div><strong>${done.length}</strong><span>Items filled</span></div><div><strong>${todo.length}</strong><span>Need attention</span></div></div>
      <p class="board-note">${fill.job_id ? "Matched to a Job Tracker posting." : "No matching Job Tracker posting. Profile and saved answers are available; job drafts are not."}</p>
      ${fill.ai_error ? `<p class="notice warn" role="alert">Could not prepare AI answers: ${esc(fill.ai_error)}.</p>` : ""}
      ${fill.stopped ? `<p class="notice warn" role="alert">${esc(fill.stopped)}</p>` : ""}
      <button id="jt-again" class="primary">Fill again <span aria-hidden="true">↻</span></button>
      ${todo.length ? `<section class="field-section"><h5>Needs your attention <span class="count">${todo.length}</span></h5><ul>${todo.map((e) => li(e, e.kind === "file" ? '<span class="tag">Attach file</span>' : e.never_ai || !askable(e) ? '<span class="tag">Fill on form</span>' : `<button id="jt-ai-${esc(e.key)}" data-ai="${esc(e.key)}" aria-label="Prepare an AI answer for ${esc(e.label || e.key)}">Draft answer</button>`)).join("")}</ul></section>` : ""}
      ${done.length ? `<details class="field-section"><summary>Filled items <span class="count">${done.length}</span></summary><ul>${done.map((e) => li(e, `<span class="tag success">${esc(sourceLabel[e.rung] || "Filled")}</span>`, true)).join("")}</ul></details>` : ""}
      <details class="help"><summary>What gets remembered?</summary><p>After you submit, choices and short answers you keep can be reused for the same question. Free-text answers stay specific to the application.</p></details>
    `);
    on("click", "jt-again", async () => {
      await readFields();
      run();
    });
    onAi = async (key) => {
      surface.mark(`jt-ai-${key}`, { disabled: true, text: "asking…" });
      const entry = entryByKey(key);
      if (!allowed("ai_suggestions")) {
        entry.ai_note = "AI suggestions are switched off right now";
        show();
        return;
      }
      await askModel([entry]);
      if (!isFilled(entry) && !fill.ai_error) {
        const f = filled.get(entry.key);
        entry.ai_note = f && f.value ? "the form did not take the answer" : "the model had no answer";
      }
      show();
    };
  }

  function finals() {
    return fill.fields.map((entry) => {
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

  // Everything the extension saw, for triage: the fields as read with the
  // markup around each, what the API resolved, what took, the person's note.
  function capture() {
    const clean = (html, cap) => html.replace(/<script[\s\S]*?<\/script>/gi, "").slice(0, cap);
    const boxes = fields.map(boxOf).filter(Boolean);
    let root = boxes[0] ? boxes[0].parentElement : document.body;
    while (root && root !== document.body && !boxes.every((b) => root.contains(b))) root = root.parentElement;
    return {
      title: document.title,
      host: reader.host,
      version: chrome.runtime.getManifest().version,
      build: BUILD,
      userAgent: navigator.userAgent,
      at: new Date().toISOString(),
      fields: fields.map((f) => {
        const box = boxOf(f);
        const ctl = f._ctl || (box && box.querySelector("input:not([type=hidden]), textarea, select, button"));
        return {
          ...plain(f),
          trace: traces.get(f.key) || f._trace || null,
          current: reader.current(f),
          control: ctl ? { tag: ctl.tagName, type: ctl.type || null, id: ctl.id || null, class: ctl.className } : null,
          html: box ? clean(box.outerHTML, 4000) : null,
        };
      }),
      resolved: fill,
      filled: Object.fromEntries(filled),
      error: lastError,
      html: clean((root || document.body).outerHTML, 400000),
    };
  }

  // Every fill pass posts its own capture, tagged automatic, so a page can
  // be read the way a report is without the person pressing anything
  // (Kanishk, 2026-09-08: "stream all pages back with traces"). The button
  // stays for a note in the person's words. Failures are silent: the
  // capture is for triage, never in the person's way.
  async function autoReport(reason) {
    try {
      const pinned = policy && policy.revision ? policy.revision.slice(0, 12) : "none";
      await api("user/apply/reports", "POST", { url: location.href, note: `auto: ${reason} [ext ${VERSION} ${reader.host || "unknown"} schema 1 rev ${pinned} recipe ${recipeRevision}]`, page: capture() });
    } catch (_) {
      // Nothing to do; the next pass captures again.
    }
  }

  const REPORT_BUTTON = `<button id="jt-report-btn">Report an issue</button>`;
  async function reportForm() {
    if (!fields.length && reader.ready()) await readFields();
    surface.region("jt-report", `
      <label for="jt-note" class="setting-title">What went wrong?</label><p class="muted">Includes a capture of this page and its form values.</p><textarea id="jt-note" rows="3" placeholder="Add a note (optional)"></textarea>
      <button id="jt-send">Send report</button> <button id="jt-cancel">Cancel</button>`);
  }
  on("click", "jt-cancel", () => surface.region("jt-report", REPORT_BUTTON));
  // The note travels with the click, so the panel is never read across a frame.
  on("click", "jt-send", async (values) => {
    surface.mark("jt-send", { disabled: true });
    const res = await api("user/apply/reports", "POST", {
      url: location.href,
      note: values["jt-note"] || "",
      page: capture(),
    });
    surface.region("jt-report", res.ok
      ? `<p class="muted">Reported as #${res.json.id}. Thank you.</p>`
      : `<p class="warn">Report failed (${esc(res.status)}): ${esc(JSON.stringify(res.json || res.error))}</p>`);
  });

  const CONFIRMED = /thank you for (applying|your application)|application (has been )?(received|submitted)|we('ve| have) received your application|we got your application|successfully submitted/i;
  const confirmationVisible = () => {
    if (reader.submitted()) return true;
    const pageText = document.body.innerText.replace(surface.text(), "");
    return !reader.ready() && CONFIRMED.test(pageText);
  };

  function showSubmission() {
    if (!submission) return;
    showing = "submission";
    panelUp = true;
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
        submissionView = null;
        mountedFor = null;
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
    if (res.ok) submission = res.state;
    else {
      // The worker may have saved the receipt before its reply was lost.
      const saved = await send({ kind: "submission", action: "get" });
      if (saved.ok && saved.state) submission = saved.state;
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
    const res = await send({ kind: "submission", action: "arm", fillId: fill?.fill_id || null, url: location.href, title: document.title, fields: fill ? finals() : [] });
    submissionBusy = false;
    if (res.ok) {
      submission = res.state;
      submissionView = null;
      showSubmission();
    } else {
      render(`<p class="warn">Could not start submission tracking. ${res.stale ? "Reload this page to reconnect the extension." : "Your application can still submit, but its board status may need updating manually."}</p>`);
    }
  };
  document.addEventListener("click", (ev) => {
    const button = reader.submitButton();
    if (button?.contains(ev.target)) armSubmission();
  }, true);
  document.addEventListener("submit", (ev) => {
    const button = reader.submitButton();
    if (button && ev.target === button.form) armSubmission();
  }, true);

  // A single-page app changes the url and the page without a load, so the
  // form is watched for rather than assumed at load time.
  if (submission) {
    showSubmission();
    if (submission.status === "confirmed") confirmSubmission("retry");
  } else mount();
  setInterval(async () => {
    if (submission && submission.status !== "confirmed" && reader.ready() && new URL(submission.url).pathname !== location.pathname) {
      if (clearingSubmission || submissionBusy) return;
      clearingSubmission = true;
      await send({ kind: "submission", action: "clear", fillId: submission.fillId });
      submission = null;
      submissionView = null;
      mountedFor = null;
      clearingSubmission = false;
    }
    if (submission) {
      if (submission.status === "watching" && confirmationVisible()) confirmSubmission("confirm");
      else showSubmission();
    } else mount();
  }, 700);
})();
