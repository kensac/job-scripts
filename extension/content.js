// The generic half of the extension: wait for the reader to see the form,
// ask the API what goes in each field, fill, ask the model once for what
// is still blank, show what was filled and what was not, and when the
// person clicks the form's own submit button, send the final values back
// so the bank and the ledger learn. It never clicks submit itself. The
// report button sends the page as the extension saw it, with a note, for
// triage later.
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

  const panel = document.createElement("div");
  panel.id = "jt-apply";
  document.body.appendChild(panel);
  const render = (html) => {
    panel.innerHTML = `<h3>Job Tracker Apply</h3>${html}
      <div id="jt-report"><button id="jt-report-btn">Report this page</button></div>`;
    panel.querySelector("#jt-report-btn").onclick = reportForm;
  };

  let fields = [];
  let fill = null;
  let filled = new Map();
  let lastError = null;

  for (let i = 0; i < 50 && !reader.ready(); i++) await sleep(200);
  if (!reader.ready()) {
    render(`<p class="warn">No form found on this page.</p>`);
    return;
  }
  fields = reader.read();
  const fieldByKey = (key) => fields.find((f) => f.key === key);
  const entryByKey = (key) => fill.fields.find((e) => e.key === key);

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
      fields: fields.map(({ _wrap, _group, ...f }) => f),
    });
    if (!res.ok) {
      lastError = res;
      render(
        res.signin
          ? `<p class="warn">Not signed in. <a href="${res.signin}" target="_blank">Open Job Tracker</a>, sign in, then reload this page.</p>`
          : `<p class="warn">Could not resolve the form (${esc(res.status)}): ${esc(JSON.stringify(res.json || res.error))}</p>`,
      );
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
    return !!(f && f.ok && f.value != null);
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
    const li = (e, extra = "") => `<li>${esc(e.label || e.key)}${extra}</li>`;
    render(`
      <p>${fill.job_id ? "On your board." : '<span class="warn">Not a posting on your board, so no drafts.</span>'}</p>
      <p><b>${done.length} filled</b>${todo.length ? `, <b class="warn">${todo.length} for you</b>` : ""}.${fill.ai_error ? ` <span class="warn">Model call failed: ${esc(fill.ai_error)}.</span>` : ""}</p>
      ${todo.length ? `<ul>${todo.map((e) => li(e, e.kind === "file" ? ' <span class="muted">(attach the file)</span>' : ` <button data-ai="${esc(e.key)}">fill with AI</button>`)).join("")}</ul>` : ""}
      <details><summary class="muted">filled (${done.length})</summary><ul>${done.map((e) => li(e, ` <span class="muted">${esc(e.rung)}</span>`)).join("")}</ul></details>
      <p class="muted">Check the form, then click its own Submit button. What you type or pick, and every model answer you leave in place, is remembered for the next form with the same question; free-text answers are not.</p>
      <button id="jt-again">Fill again</button>
    `);
    panel.querySelector("#jt-again").onclick = () => {
      fields = reader.read();
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
    const wraps = fields.map((f) => f._wrap).filter(Boolean);
    let root = wraps[0] ? wraps[0].parentElement : document.body;
    while (root && root !== document.body && !wraps.every((w) => root.contains(w))) root = root.parentElement;
    return {
      title: document.title,
      host: reader.host,
      version: chrome.runtime.getManifest().version,
      userAgent: navigator.userAgent,
      at: new Date().toISOString(),
      fields: fields.map((f) => {
        const { _wrap, _group, ...plain } = f;
        const ctl = _wrap && _wrap.querySelector("input:not([type=hidden]), textarea, select, button");
        return {
          ...plain,
          current: reader.current(f),
          control: ctl ? { tag: ctl.tagName, type: ctl.type || null, id: ctl.id || null, class: ctl.className } : null,
          html: _wrap ? clean(_wrap.outerHTML, 4000) : null,
        };
      }),
      resolved: fill,
      filled: Object.fromEntries(filled),
      error: lastError,
      html: clean((root || document.body).outerHTML, 400000),
    };
  }

  function reportForm() {
    const box = panel.querySelector("#jt-report");
    box.innerHTML = `
      <textarea id="jt-note" rows="3" style="width:100%" placeholder="What went wrong? (optional)"></textarea>
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

  await run();
})();
