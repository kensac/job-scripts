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
  let aiAll = false;
  try {
    aiAll = localStorage.getItem("jt-apply-ai-all") === "1";
  } catch (_) {
    aiAll = false;
  }
  const askable = (e) => !e.never_ai && (ASKABLE.has(e.kind) || (aiAll && e.kind === "long"));

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
  let collapsed = false;
  try {
    collapsed = localStorage.getItem("jt-apply-collapsed") === "1";
  } catch (_) {
    collapsed = false;
  }

  const render = (html) => {
    if (!panel) return;
    panel.classList.toggle("collapsed", collapsed);
    panel.innerHTML = `
      <div class="head"><h3>Job Tracker Apply</h3>
        <button id="jt-min" title="${collapsed ? "Expand" : "Minimise"}">${collapsed ? "+" : "–"}</button></div>
      <div class="body">${html}
        <label class="muted switch"><input type="checkbox" id="jt-ai-all" ${aiAll ? "checked" : ""}> AI answers every blank box, free text too</label>
        <div id="jt-report"><button id="jt-report-btn">Report this page</button></div></div>`;
    panel.querySelector("#jt-report-btn").onclick = reportForm;
    panel.querySelector("#jt-ai-all").onchange = (ev) => {
      aiAll = ev.target.checked;
      try {
        localStorage.setItem("jt-apply-ai-all", aiAll ? "1" : "0");
      } catch (_) {
        // Nothing to remember it in; it holds for this page.
      }
    };
    panel.querySelector("#jt-min").onclick = () => {
      collapsed = !collapsed;
      try {
        localStorage.setItem("jt-apply-collapsed", collapsed ? "1" : "0");
      } catch (_) {
        // Nothing to remember it in; the panel still toggles.
      }
      panel.classList.toggle("collapsed", collapsed);
      panel.querySelector("#jt-min").textContent = collapsed ? "+" : "–";
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

  async function put(entry, value, file) {
    const field = fieldByKey(entry.key);
    if (!field) return false;
    let ok = false;
    try {
      ok = await reader.fill(field, value, file);
    } catch (e) {
      lastError = String(e);
    }
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
    await askModel(fill.fields.filter((e) => !isFilled(e) && askable(e)));
    await verify();
    show();
    // A form that spans pages: when this page has a Continue and no Submit,
    // go on to the next page and fill it too, until the page that submits.
    // Continue is not Submit; the person still clicks that.
    if (reader.nextButton && !reader.submitButton() && reader.nextButton() && step < 12) {
      const before = fingerprint();
      clickThrough(reader.nextButton());
      for (let i = 0; i < 40 && fingerprint() === before; i++) await sleep(250);
      await settle();
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
      userAgent: navigator.userAgent,
      at: new Date().toISOString(),
      fields: fields.map((f) => {
        const box = boxOf(f);
        const ctl = f._ctl || (box && box.querySelector("input:not([type=hidden]), textarea, select, button"));
        return {
          ...plain(f),
          trace: f._trace || null,
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
