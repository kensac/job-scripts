import { panelSurface } from "../ui/panel.js";
import { isStopped } from "./operation";
import { createApplicationPreferences } from "./preferences";
import { createApplicationReportCapture } from "./report-capture";
import { createSubmissionTracker } from "./submission-tracker";
import { createApplicationReview } from "./answer-review";
import { createApplicationContext } from "./application-context";
import { createPageFill } from "./page-fill";
import { createReportPanel } from "./report-panel";
import { createPanelShell } from "./panel-shell";
// The generic half of the extension. It runs on every page of an ATS
// host, watches for the application form to appear (the ATS is a single-
// page app, so the posting page becomes the form without a reload), and
// offers one button: Autofill. On the click it asks the API what goes in
// each field, fills, asks the model once for what is still blank, and
// shows what was filled and what was not. When the person clicks the
// form's own submit button and the ATS confirms, the final values go
// back so the bank and the ledger learn. It never clicks submit itself.
// The report button sends the page as the extension saw it, with a note.
export async function startApplication(adapter, adapterContext, lifecycle) {
  const operation = adapterContext.operation;
  // THE PANEL IS NOT ALWAYS IN THIS FRAME. The flow stays beside the form,
  // whose DOM it reads and fills; an embedded form's panel goes to the top
  // frame (README, "Where the panel sits"). `surface` is the same object
  // either way, and every call is one way. Nothing is read back across a frame.
  const embedded = window.top !== window.self;

  const reader = adapter || { ready: () => false, submitButton: () => null, submitted: () => false };
  // Stamped into every report, so a report from a build the person has not
  // reloaded yet is told apart from a bug (reports 9 to 11, 2026-09-08).
  const BUILD = "0.3.7 preserve draft editor position";

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
  const taskApi = (path, method, body) => operation.wait(api(path, method, body));

  // Bound by id rather than by node, so a repaint never loses a handler and
  // the same registration works whether the control is in this document or
  // in the top frame's. Every event carries the panel's input values, which
  // is what makes the remote surface one way.
  const handlers = new Map();
  const on = (type, id, fn) => handlers.set(`${type}:${id}`, fn);
  function dispatch(event) {
    const { type, id, values } = event;
    if (type === "review") return reviewAction(event);
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
      review: (...a) => op("review", ...a),
      appearance: (...a) => op("appearance", ...a),
      text: () => "",
    };
  };
  const surface = embedded ? remoteSurface() : panelSurface().create(dispatch);
  lifecycle.onInvalidated?.(() => { operation.stop(); surface.remove(); });
  if (embedded) {
    const panelEvent = (msg, _sender, reply) => {
      if (!msg || msg.kind !== "panel-event") return;
      dispatch(msg.event);
      reply({ ok: true });
    };
    chrome.runtime.onMessage.addListener(panelEvent);
    lifecycle.onInvalidated?.(() => chrome.runtime.onMessage.removeListener(panelEvent));
  }
  let submission = (await send({ kind: "submission", action: "get" })).state || null;
  if (lifecycle.isInvalid) return;
  if (submission && submission.status !== "confirmed" && reader.ready() && new URL(submission.url).pathname !== location.pathname) {
    await send({ kind: "submission", action: "clear", fillId: submission.fillId });
    submission = null;
  }
  if (!adapter && !submission) return;
  const sleep = (ms) => operation.sleep(ms);
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
  const { prefs, savePref, load: loadPreferences } = createApplicationPreferences(api);
  await loadPreferences();
  if (lifecycle.isInvalid) return;
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
  let reviewState = null;
  let reviewMessage = "";
  const touched = new Set();
  let humanGesture = false;
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
  const { THEME_NEXT, themeLabel, render, operationControls } = createPanelShell({ prefs, surface, operation, esc, getPanelUp: () => panelUp, getSubmission: () => submission, getApplicationContext: () => applicationContext });

  operation.subscribe(() => { surface.region("jt-operation", operationControls()); drawReview(); });
  on("click", "jt-pause", () => operation.state === "paused" ? operation.resume() : operation.pause());
  on("click", "jt-stop", () => operation.stop());

  async function execute(task) {
    if (operation.busy) return;
    humanGesture = false;
    operation.start();
    try {
      await task();
    } catch (error) {
      lastError = String(error);
      if (fill) {
        fill.stopped = isStopped(error)
          ? "Autofill stopped. Existing answers are unchanged. A request already sent may still be processed, but its answer will not be applied."
          : "Autofill could not finish. Your existing answers are still on the form. You can try again.";
        show();
      } else {
        offer(isStopped(error) ? "Autofill stopped. Nothing further will be filled." : "Could not fill this page. You can try again.");
      }
    } finally {
      pageSig = pageSignature();
      operation.finish();
    }
  }

  function reportForm() { return reportPanel.reportForm(); }
  on("click", "jt-report-btn", reportForm);
  on("change", "jt-ai-all", (values) => savePref("aiAll", (prefs.aiAll = !!values["jt-ai-all"])));
  on("change", "jt-advance", (values) => savePref("autoAdvance", (prefs.autoAdvance = !!values["jt-advance"])));
  on("click", "jt-theme", () => {
    prefs.theme = THEME_NEXT[String(prefs.theme)];
    savePref("theme", prefs.theme);
    surface.appearance({ theme: prefs.theme, collapsed: prefs.collapsed });
    surface.mark("jt-theme", { text: prefs.theme === "light" ? "☀" : prefs.theme === "dark" ? "☾" : "◐", label: `Theme: ${themeLabel(prefs.theme)}. Switch to ${themeLabel(THEME_NEXT[String(prefs.theme)])}` });
  });
  on("click", "jt-min", () => {
    prefs.collapsed = !prefs.collapsed;
    savePref("collapsed", prefs.collapsed);
    surface.appearance({ theme: prefs.theme, collapsed: prefs.collapsed });
    surface.mark("jt-min", { text: prefs.collapsed ? "+" : "−", label: prefs.collapsed ? "Expand panel" : "Minimise panel", expanded: !prefs.collapsed });
  });

  // Underscore keys are the reader's DOM handles; the API never sees them.
  const plain = (f) => Object.fromEntries(Object.entries(f).filter(([k]) => !k.startsWith("_")));
  const boxOf = (f) => f._box || (f._ctl && (f._ctl.closest("fieldset, .field, .application-question, .select-shell") || f._ctl.parentElement)) || null;
  const onScreen = (f) => {
    const node = f._el || f._ctl || f._box;
    return !!node?.isConnected && node.getClientRects().length > 0;
  };
  const fieldByKey = (key) => fields.find((f) => f.key === key);
  const entryByKey = (key) => fill.fields.find((e) => e.key === key);
  const readFields = async () => {
    fields = await reader.read();
  };
  const protectManualEdit = (event) => {
    if (!event.isTrusted || !(event.target instanceof HTMLElement) || event.target.id === "jt-apply-host") return;
    if (event.type === "beforeinput") humanGesture = true;
    if (!humanGesture) return;
    for (const field of fields) {
      const nodes = [field._el, field._ctl, field._box, ...(field._inputs || [])].filter(node => node instanceof Element);
      if (nodes.some(node => node === event.target || node.contains(event.target))) touched.add(field.key);
    }
    if (operation.active) operation.stop();
    drawReview();
  };
  // HTMLElement.click() produces trusted checkbox input/change events in
  // Chromium. Only a preceding human gesture or trusted beforeinput proves
  // an edit came from the person rather than the adapter's click.
  for (const type of ["pointerdown", "keydown"]) lifecycle.addEventListener(document, type, event => {
    if (event.isTrusted && event.target instanceof HTMLElement && event.target.id !== "jt-apply-host" && event.target.closest("input, textarea, select, label, button, [role]")) humanGesture = true;
  }, true);
  for (const type of ["beforeinput", "input", "change"]) lifecycle.addEventListener(document, type, protectManualEdit, true);

  // The panel appears when a form is on the page and goes when it is not,
  // so the posting page shows nothing and the form page shows the button
  // without a reload.
  // The controls on the page by id or name: it changes when the form moves
  // to its next page under the same url (Workday), not when the person
  // types. Recorded after a fill pass; a change with none of the filled
  // controls left on screen means a page the person moved to by hand, and
  // the panel offers Autofill for it.
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
    if (operation.busy) {
      if (location.href !== mountedFor) operation.stop();
      surface.ensure();
      return;
    }
    const here = location.href;
    if (reader.ready()) {
      // A page that hydrates after load (Greenhouse's board is a Remix app)
      // can throw the panel out of the body with the rest of the markup it
      // did not render; put it back rather than believing it is there.
      surface.ensure();
      if (mountedFor === here && panelUp && fill && pageSig && pageSignature() !== pageSig) {
        // A control coming or going beside the filled ones is the same page:
        // a required react-select drops its hidden input once the person
        // answers it (Greenhouse's location). Treating that as a new page
        // dropped the fill, and the Submit that followed recorded an empty one.
        if (fields.some(onScreen)) {
          pageSig = pageSignature();
          return;
        }
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
      applicationContext.reset();
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

  const applicationContext = createApplicationContext(api, lifecycle, esc, () => { if (showing === "offer") offer(offerLead); });

  // Which view is painted, so a late answer cannot paint over a newer one.
  let showing = null;
  let offerLead = undefined;
  function offer(lead) {
    offerLead = lead;
    showing = "offer";
    render(`
      ${lead ? `<p class="notice" role="status">${esc(lead)}</p>` : ""}
      <button id="jt-autofill" class="primary">Autofill this page <span aria-hidden="true">↗</span></button>
      ${applicationContext.sources()}
    `);
    on("click", "jt-autofill", run);
    applicationContext.load();
  }

  // A reader's trace of a fill, kept by key: the fields are read again
  // after a fill pass (revealed fields), and a fresh field object has no
  // trace, which left report 10 blank where report 8 had them.
  const traces = new Map();
  async function put(entry, value, file, explicit = false) {
    const field = fieldByKey(entry.key);
    if (!field) return false;
    const current = reader.current(field);
    const previous = filled.get(entry.key);
    if (!explicit && previous?.error && current === previous.value) return false;
    if (!explicit && (touched.has(entry.key) || filled.get(entry.key)?.kept || (current && current !== filled.get(entry.key)?.value))) {
      filled.set(entry.key, { ok: !!current, value: current, kept: true });
      return false;
    }
    let ok = false;
    let error = null;
    try {
      ok = await reader.fill(field, value, file);
    } catch (e) {
      if (isStopped(e)) throw e;
      lastError = String(e);
      error = String(e.message || e);
      traces.set(entry.key, [...(field._trace || []), `threw: ${String(e)}`]);
    }
    if (field._trace && field._trace.length) traces.set(entry.key, field._trace);
    filled.set(entry.key, { ok, value: file ? file.name : value, ...(error ? { error } : {}) });
    return ok;
  }

  function run() {
    return execute(runPage);
  }


  // DOM quiet is only a rendering heuristic, not a server-save receipt.
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
      const res = await taskApi("user/apply/resolve", "POST", {
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

  // One call for everything still blank. The answers land in the form
  // like any other rung, marked "ai" so the panel and the ledger say so.
  async function askModel(entries) {
    if (!entries.length) return;
    render(`<div class="working" role="status"><span class="spinner" aria-hidden="true"></span><div><h4>Preparing answers</h4><p>Working through ${entries.length} unanswered form items…</p></div></div>`);
    const res = await taskApi("user/apply/suggest", "POST", {
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

  async function trackFilledJob() {
    const current = fill;
    const result = await api(`user/apply/fills/${current.fill_id}/track`, "POST", {});
    if (fill !== current) return;
    if (result.ok) {
      fill.job_id = result.json.job_id;
      fill.board_saved = true;
      fill.board_error = false;
      applicationContext.markIdle();
      await applicationContext.load();
    } else fill.board_error = true;
  }

  const reviewController = createApplicationReview({ state: { get fill() { return fill; }, get filled() { return filled; }, get reviewState() { return reviewState; }, set reviewState(value) { reviewState = value; }, get reviewMessage() { return reviewMessage; }, set reviewMessage(value) { reviewMessage = value; }, get panelUp() { return panelUp; }, touched }, render, on, api, surface, operation, fieldByKey, reader, boxOf, allowed, askable, entryByKey, execute, taskApi, put, esc, trackFilledJob, run });
  const { show, drawReview, reviewAction } = reviewController;

  const { queueReport, autoReport } = createApplicationReportCapture({ reader, boxOf, plain, traces, send, VERSION, BUILD, getFields: () => fields, getFill: () => fill, getReviewState: () => reviewState, getFilled: () => filled, getLastError: () => lastError, getPolicy: () => policy, getRecipeRevision: () => recipeRevision });

  const reportPanel = createReportPanel({ on, surface, reader, readFields, getFields: () => fields, queueReport, esc });

  const runPage = createPageFill({
    state: {
      get showing() { return showing; }, set showing(value) { showing = value; },
      get policy() { return policy; }, set policy(value) { policy = value; },
      get recipeRevision() { return recipeRevision; }, set recipeRevision(value) { recipeRevision = value; },
      get fields() { return fields; }, set fields(value) { fields = value; },
      get step() { return step; }, set step(value) { step = value; },
      get fill() { return fill; }, set fill(value) { fill = value; },
      get lastError() { return lastError; }, set lastError(value) { lastError = value; },
      get filled() { return filled; }, set filled(value) { filled = value; },
      get reviewState() { return reviewState; }, set reviewState(value) { reviewState = value; },
      get reviewMessage() { return reviewMessage; }, set reviewMessage(value) { reviewMessage = value; },
      get pageSig() { return pageSig; }, set pageSig(value) { pageSig = value; },
    },
    render, operation, send, reader, allowed, readFields, taskApi, plain, esc,
    on, run, fieldByKey, adapterContext, pdf, put, settle, revealed, isFilled,
    askable, askModel, verify, trackFilledJob, show, autoReport, pageSignature,
    prefs, fingerprint, clickThrough, sleep,
  });

  const submissionTracker = createSubmissionTracker({
    on, reader, surface, lifecycle, send, render, esc, mount, fieldByKey,
    getFill: () => fill, getSubmission: () => submission,
    setSubmission: (value) => { submission = value; },
    setShowing: (value) => { showing = value; },
    setPanelUp: (value) => { panelUp = value; },
    setMountedFor: (value) => { mountedFor = value; },
  });
  submissionTracker.start();
}
