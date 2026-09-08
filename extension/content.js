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
  const reader = window.__jtReader;
  if (!reader) return;
  // Stamped into every report, so a report from a build the person has not
  // reloaded yet is told apart from a bug (reports 9 to 11, 2026-09-08).
  const BUILD = "2026-09-08 01:45";

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
  const prefs = { collapsed: false, aiAll: false, theme: null };
  const loadPrefs = async () => {
    try {
      const got = await new Promise((r) => chrome.storage.local.get(["collapsed", "aiAll", "theme"], r));
      if (got && typeof got.collapsed === "boolean") prefs.collapsed = got.collapsed;
      else prefs.collapsed = localStorage.getItem("jt-apply-collapsed") === "1";
      if (got && typeof got.aiAll === "boolean") prefs.aiAll = got.aiAll;
      else prefs.aiAll = localStorage.getItem("jt-apply-ai-all") === "1";
      prefs.theme = got && (got.theme === "light" || got.theme === "dark") ? got.theme : null;
    } catch (_) {
      // Nothing stored or no storage: the defaults hold.
    }
  };
  // The person's copy: user_settings.prefs.apply through the settings
  // endpoints, so the same panel greets them in every browser they sign in
  // to (Kanishk, 2026-09-08). The account's value wins over the browser's
  // cache at start; a change goes to both. Merged on write so the other
  // prefs on the row (auto_draft) keep.
  const PREF_KEYS = ["collapsed", "aiAll", "theme"];
  const syncPrefsFromAccount = async () => {
    const res = await api("user/settings", "GET");
    const mine = res.ok && res.json && res.json.prefs && res.json.prefs.apply;
    if (!mine || typeof mine !== "object") return;
    for (const k of PREF_KEYS) if (k in mine) prefs[k] = mine[k];
    try {
      chrome.storage.local.set({ collapsed: prefs.collapsed, aiAll: prefs.aiAll, theme: prefs.theme });
    } catch (_) {
      // The cache is a convenience; the account has it.
    }
  };
  const savePrefsToAccount = async () => {
    const res = await api("user/settings", "GET");
    if (!res.ok || !res.json) return;
    const all = { ...(res.json.prefs || {}), apply: { collapsed: prefs.collapsed, aiAll: prefs.aiAll, theme: prefs.theme } };
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

  let panel = null;
  let step = 0;
  let fields = [];
  const clickThrough = (el) => {
    for (const t of ["mousedown", "mouseup"]) el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true }));
    el.click();
  };
  let fill = null;
  let filled = new Map();
  let lastError = null;
  let mountedFor = null;

  // The theme: data-jt-theme="light" or "dark" for an explicit choice, and
  // no attribute at all for "system", which follows prefers-color-scheme in
  // panel.css. The button cycles light, dark, system and names the next.
  const THEME_NEXT = { light: "dark", dark: null, null: "light" };
  const themeLabel = (t) => (t === "light" ? "Light" : t === "dark" ? "Dark" : "System");
  const applyTheme = () => {
    if (!panel) return;
    if (prefs.theme) panel.setAttribute("data-jt-theme", prefs.theme);
    else panel.removeAttribute("data-jt-theme");
    const btn = panel.querySelector("#jt-theme");
    if (btn) {
      btn.title = `Theme: ${themeLabel(prefs.theme)}. Switch to ${themeLabel(THEME_NEXT[String(prefs.theme)])}`;
      btn.textContent = prefs.theme === "light" ? "☀" : prefs.theme === "dark" ? "☾" : "◐";
    }
  };

  const render = (html) => {
    if (!panel) return;
    panel.classList.toggle("collapsed", prefs.collapsed);
    panel.innerHTML = `
      <div class="head"><h3>Job Tracker Apply</h3>
        <button id="jt-theme"></button>
        <button id="jt-min" title="${prefs.collapsed ? "Expand" : "Minimise"}">${prefs.collapsed ? "+" : "–"}</button></div>
      <div class="body">${html}
        <label class="muted switch"><input type="checkbox" id="jt-ai-all" ${prefs.aiAll ? "checked" : ""}> AI answers every blank box, free text too</label>
        <div id="jt-report"><button id="jt-report-btn">Report this page</button></div></div>`;
    applyTheme();
    panel.querySelector("#jt-report-btn").onclick = reportForm;
    panel.querySelector("#jt-ai-all").onchange = (ev) => {
      prefs.aiAll = ev.target.checked;
      savePref("aiAll", prefs.aiAll);
    };
    panel.querySelector("#jt-theme").onclick = () => {
      prefs.theme = THEME_NEXT[String(prefs.theme)];
      savePref("theme", prefs.theme);
      applyTheme();
    };
    panel.querySelector("#jt-min").onclick = () => {
      prefs.collapsed = !prefs.collapsed;
      savePref("collapsed", prefs.collapsed);
      panel.classList.toggle("collapsed", prefs.collapsed);
      panel.querySelector("#jt-min").textContent = prefs.collapsed ? "+" : "–";
      panel.querySelector("#jt-min").title = prefs.collapsed ? "Expand" : "Minimise";
    };
  };

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
  function mount() {
    const here = location.href;
    if (reader.ready()) {
      // A page that hydrates after load (Greenhouse's board is a Remix app)
      // can throw the panel out of the body with the rest of the markup it
      // did not render; put it back rather than believing it is there.
      if (panel && !panel.isConnected) document.body.appendChild(panel);
      if (mountedFor === here && panel) return;
      mountedFor = here;
      fields = [];
      fill = null;
      filled = new Map();
      if (!panel) {
        panel = document.createElement("div");
        panel.id = "jt-apply";
        document.body.appendChild(panel);
      }
      offer();
    } else if (!fill && reader.applyButton && reader.applyButton()) {
      // The posting page, with the button that opens the form on it (the
      // selector table clicks it on 25 ATSs). Offered, never pressed unasked;
      // once the form appears the tick above takes over.
      if (mountedFor === here + "#open" && panel) return;
      mountedFor = here + "#open";
      if (!panel) {
        panel = document.createElement("div");
        panel.id = "jt-apply";
        document.body.appendChild(panel);
      }
      render(`
        <p>This is the posting. The application form is a click away.</p>
        <button id="jt-open" class="primary">Open the application</button>
      `);
      panel.querySelector("#jt-open").onclick = () => {
        const btn = reader.applyButton();
        if (btn) clickThrough(btn);
        mountedFor = null;
      };
    } else if (panel && !fill) {
      // No form and nothing recorded on it: the posting page, or a page
      // away from the form. A panel after a submit stays for its message.
      panel.remove();
      panel = null;
      mountedFor = null;
    }
  }

  function offer() {
    render(`
      <p>Fill this application from your profile, your remembered answers and your drafts.</p>
      <button id="jt-autofill" class="primary">Autofill</button>
      <p class="muted">You check the form and click its own Submit button.</p>
    `);
    panel.querySelector("#jt-autofill").onclick = async () => {
      await readFields();
      run();
    };
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
    render(`<p class="muted">Resolving ${fields.length} fields…</p>`);
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
          ? `<p class="warn">Not signed in. <a href="${res.signin}" target="_blank">Open Job Tracker</a>, sign in, then try again.</p><button id="jt-autofill">Autofill</button>`
          : `<p class="warn">Could not resolve the form (${esc(res.status)}): ${esc(JSON.stringify(res.json || res.error))}</p><button id="jt-autofill">Try again</button>`,
      );
      panel.querySelector("#jt-autofill").onclick = async () => {
        await readFields();
        run();
      };
      return;
    }
    fill = res.json;
    filled = new Map();
    // A field the reader leaves to the person is never offered to the model.
    for (const entry of fill.fields) if (fieldByKey(entry.key)?._person) entry.never_ai = true;
    // Repeated groups are filled from the profile's rows, not a value.
    window.__jtProfile = fill.profile || {};
    let file = null;
    if (fill.resume && fill.resume.has_pdf) {
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
    await askModel(fill.fields.filter((e) => !isFilled(e) && askable(e)));
    await verify();
    show();
    await autoReport(`filled page ${step + 1}`);
    // A form that spans pages: when this page has a Continue and no Submit,
    // go on to the next page and fill it too, until the page that submits.
    // Continue is not Submit; the person still clicks that.
    if (reader.nextButton && !reader.submitButton() && reader.nextButton() && step < 12) {
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
    render(`<p class="muted">Asking the model about ${entries.length} fields…</p>`);
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
    const li = (e, extra = "") => `<li><span>${esc(e.label || e.key)}</span>${e.ai_note ? `<span class="muted">${esc(e.ai_note)}</span>` : ""}${extra}</li>`;
    render(`
      <p>${fill.job_id ? "On your board." : '<span class="warn">Not a posting on your board, so no drafts.</span>'}</p>
      <p><b>${done.length} filled</b>${todo.length ? `, <b class="todo">${todo.length} for you</b>` : ""}.${fill.ai_error ? ` <span class="warn">Model call failed: ${esc(fill.ai_error)}.</span>` : ""}</p>
      ${fill.stopped ? `<p class="warn">${esc(fill.stopped)}</p>` : ""}
      ${todo.length ? `<ul>${todo.map((e) => li(e, e.kind === "file" ? '<span class="muted">attach the file</span>' : e.never_ai ? '<span class="muted">yours to fill</span>' : `<button data-ai="${esc(e.key)}">fill with AI</button>`)).join("")}</ul>` : ""}
      <details><summary class="muted">filled (${done.length})</summary><ul>${done.map((e) => li(e, `<span class="muted">${esc(e.rung)}</span>`)).join("")}</ul></details>
      <details><summary class="muted">how this works</summary><p class="muted">Check the form, then click its own Submit button. What you type or pick, and every model answer you leave in place, is remembered for the next form with the same question; free-text answers are not.</p></details>
      <button id="jt-again">Fill again</button>
    `);
    panel.querySelector("#jt-again").onclick = async () => {
      await readFields();
      run();
    };
    for (const btn of panel.querySelectorAll("[data-ai]")) {
      btn.onclick = async () => {
        btn.disabled = true;
        btn.textContent = "asking…";
        const entry = entryByKey(btn.dataset.ai);
        await askModel([entry]);
        if (!isFilled(entry) && !fill.ai_error) {
          const f = filled.get(entry.key);
          entry.ai_note = f && f.value ? "the form did not take the answer" : "the model had no answer";
        }
        show();
      };
    }
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
      await api("user/apply/reports", "POST", { url: location.href, note: `auto: ${reason}`, page: capture() });
    } catch (_) {
      // Nothing to do; the next pass captures again.
    }
  }

  async function reportForm() {
    const box = panel.querySelector("#jt-report");
    if (!fields.length && reader.ready()) await readFields();
    box.innerHTML = `
      <textarea id="jt-note" rows="3" placeholder="What went wrong? (optional)"></textarea>
      <button id="jt-send">Send report</button> <button id="jt-cancel">Cancel</button>`;
    box.querySelector("#jt-cancel").onclick = () => {
      box.innerHTML = `<button id="jt-report-btn">Report this page</button>`;
      box.querySelector("#jt-report-btn").onclick = reportForm;
    };
    box.querySelector("#jt-send").onclick = async () => {
      box.querySelector("#jt-send").disabled = true;
      const res = await api("user/apply/reports", "POST", {
        url: location.href,
        note: box.querySelector("#jt-note").value,
        page: capture(),
      });
      box.innerHTML = res.ok
        ? `<p class="muted">Reported as #${res.json.id}. Thank you.</p>`
        : `<p class="warn">Report failed (${esc(res.status)}): ${esc(JSON.stringify(res.json || res.error))}</p>`;
    };
  }

  // The form is gone and the page says so, in the words every ATS uses.
  // The fallback for a reader without its own signal, and for the day an
  // ATS renames the class the reader looks for.
  const CONFIRMED = /thank you for (applying|your application)|application (has been )?(received|submitted)|we('ve| have) received your application|we got your application|successfully submitted/i;
  const confirmedByText = () => !reader.ready() && CONFIRMED.test(document.body.innerText);

  // Recorded only once the ATS confirms. The values are read at the click,
  // because the confirmation screen replaces the form; if no confirmation
  // comes, the person can record it by hand or try again.
  async function record(values) {
    const res = await api(`user/apply/fills/${fill.fill_id}/submitted`, "POST", { fields: values });
    render(
      res.ok
        ? `<p>Recorded.${res.json.job_id ? " The board row is marked submitted." : ""}</p>`
        : `<p class="warn">Could not record the submit (${esc(res.status)}).</p>`,
    );
  }

  document.addEventListener(
    "click",
    async (ev) => {
      const btn = reader.submitButton();
      if (!fill || !btn || !btn.contains(ev.target)) return;
      // The click goes through to the form untouched.
      const values = finals();
      for (let i = 0; i < 150; i++) {
        await sleep(200);
        if (reader.submitted() || confirmedByText()) return record(values);
      }
      render(`
        <p class="warn">The form did not confirm the submit within 30 seconds, so nothing was recorded.</p>
        <button id="jt-record">It did submit, record it</button>`);
      panel.querySelector("#jt-record").onclick = () => record(values);
    },
    true,
  );

  // A single-page app changes the url and the page without a load, so the
  // form is watched for rather than assumed at load time.
  mount();
  setInterval(mount, 700);
})();
