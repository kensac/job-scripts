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

  const api = (path, method, body) =>
    new Promise((resolve) => chrome.runtime.sendMessage({ path, method, body }, resolve));
  const pdf = (path) =>
    new Promise((resolve) => chrome.runtime.sendMessage({ kind: "pdf", path }, resolve));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  // The model is asked about these; free text has the drafts, and a file
  // or a date picker is the person's.
  const ASKABLE = new Set(["text", "number", "select", "yesno", "multiselect"]);
  // Agreeing to something is the person's click, never the model's.
  const CONSENT = /acknowledg|consent|agree|certif|arbitration|privacy|terms/i;
  const askable = (e) => ASKABLE.has(e.kind) && !CONSENT.test(e.label || "");

  let panel = null;
  let fields = [];
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
        <div id="jt-report"><button id="jt-report-btn">Report this page</button></div></div>`;
    panel.querySelector("#jt-report-btn").onclick = reportForm;
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
      fields: fields.map(({ _box, _group, ...f }) => f),
    });
    if (!res.ok) {
      lastError = res;
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
    let file = null;
    if (fill.resume && fill.resume.has_pdf) {
      const got = await pdf(`user/resumes/${fill.resume.id}/pdf`);
      if (got.ok) file = new File([new Uint8Array(got.bytes)], got.name, { type: "application/pdf" });
    }
    for (const entry of fill.fields) {
      if (entry.rung === "resume") await put(entry, null, file);
      else if (entry.value != null) await put(entry, entry.value, null);
    }
    await askModel(fill.fields.filter((e) => !isFilled(e) && askable(e)));
    show();
  }

  const isFilled = (entry) => {
    const f = filled.get(entry.key);
    return !!(f && f.ok && f.value != null && f.value !== "");
  };

  // One call for everything still blank. The answers land in the form
  // like any other rung, marked "ai" so the panel and the ledger say so.
  async function askModel(entries) {
    if (!entries.length) return;
    render(`<p class="muted">Asking the model about ${entries.length} fields…</p>`);
    const res = await api("user/apply/suggest", "POST", {
      job_id: fill.job_id,
      fields: entries.map((e) => ({ key: e.key, label: e.label || e.key, kind: e.kind, options: e.options, hint: e.hint })),
    });
    if (!res.ok) {
      lastError = res;
      fill.ai_error = res.json?.detail?.code || res.status;
      return;
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
    const li = (e, extra = "") => `<li><span>${esc(e.label || e.key)}</span>${extra}</li>`;
    render(`
      <p>${fill.job_id ? "On your board." : '<span class="warn">Not a posting on your board, so no drafts.</span>'}</p>
      <p><b>${done.length} filled</b>${todo.length ? `, <b class="warn">${todo.length} for you</b>` : ""}.${fill.ai_error ? ` <span class="warn">Model call failed: ${esc(fill.ai_error)}.</span>` : ""}</p>
      ${todo.length ? `<ul>${todo.map((e) => li(e, e.kind === "file" ? '<span class="muted">attach the file</span>' : `<button data-ai="${esc(e.key)}">fill with AI</button>`)).join("")}</ul>` : ""}
      <details><summary class="muted">filled (${done.length})</summary><ul>${done.map((e) => li(e, `<span class="muted">${esc(e.rung)}</span>`)).join("")}</ul></details>
      <p class="muted">Check the form, then click its own Submit button. What you type or pick, and every model answer you leave in place, is remembered for the next form with the same question; free-text answers are not.</p>
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
        await askModel([entryByKey(btn.dataset.ai)]);
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
    const boxes = fields.map((f) => f._box).filter(Boolean);
    let root = boxes[0] ? boxes[0].parentElement : document.body;
    while (root && root !== document.body && !boxes.every((b) => root.contains(b))) root = root.parentElement;
    return {
      title: document.title,
      host: reader.host,
      version: chrome.runtime.getManifest().version,
      userAgent: navigator.userAgent,
      at: new Date().toISOString(),
      fields: fields.map((f) => {
        const { _box, _group, ...plain } = f;
        const ctl = _box && _box.querySelector("input:not([type=hidden]), textarea, select, button");
        return {
          ...plain,
          current: reader.current(f),
          control: ctl ? { tag: ctl.tagName, type: ctl.type || null, id: ctl.id || null, class: ctl.className } : null,
          html: _box ? clean(_box.outerHTML, 4000) : null,
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
