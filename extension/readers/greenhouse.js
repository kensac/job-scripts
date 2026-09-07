// Greenhouse's hosted application form (job-boards.greenhouse.io,
// boards.greenhouse.io, job-boards.eu.greenhouse.io), including the form
// an employer embeds on its own careers site, which is the same page in
// an iframe (boards.greenhouse.io/embed/job_app?for=<board>&token=<id>).
//
// Greenhouse publishes the form itself: GET boards-api.greenhouse.io/v1/
// boards/<board>/jobs/<id>?questions=true (fetched by the background
// worker, since the page's content security policy does not list that
// host) answers with every
// field's name, label, type and, for a choice, its options. The field name
// is the id of the control on the page and the key the API's form reader
// stores drafts under, so the reader pairs the API's fields with the DOM
// rather than reading widgets. Choices are react-select: the menu opens on
// a mousedown on its control (the focus that follows does the opening, so
// the tab must be visible) and an option is chosen with a click on it.
(() => {
  const FORM = "form#application-form";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clean = (s) => (s || "").replace(/\s+/g, " ").replace(/\s*\*$/, "").trim();

  function setNative(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement : HTMLInputElement;
    Object.getOwnPropertyDescriptor(proto.prototype, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  const mouse = (el, types) => {
    for (const t of types) el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window, button: 0 }));
  };

  // The board and job the page is for, from either url shape.
  function posting() {
    const u = new URL(location.href);
    const m = u.pathname.match(/^\/([^/]+)\/jobs\/(\d+)/);
    if (m) return { board: m[1], id: m[2] };
    if (u.pathname.startsWith("/embed/job_app") && u.searchParams.get("for") && u.searchParams.get("token")) {
      return { board: u.searchParams.get("for"), id: u.searchParams.get("token") };
    }
    return null;
  }
  const apiHost = () => (location.hostname.includes(".eu.") ? "boards-api.eu.greenhouse.io" : "boards-api.greenhouse.io");

  // These carry the file; the text twins and the cover letter are not ours.
  const SKIP = new Set(["resume_text", "cover_letter", "cover_letter_text", "latitude", "longitude"]);

  function control(name) {
    if (name.endsWith("[]")) return document.querySelector(`${FORM} input[name="${CSS.escape(name)}"]`);
    return document.getElementById(name) || document.querySelector(`${FORM} [name="${CSS.escape(name)}"]`);
  }
  const shellOf = (ctl) => ctl.closest(".select-shell") || ctl.closest('[class*="select__control"]')?.parentElement;
  const isReactSelect = (ctl) => !!ctl && /select__input/.test(ctl.className);

  let cached = null;
  async function questions() {
    const p = posting();
    if (!p) return [];
    const key = `${p.board}/${p.id}`;
    if (cached && cached.key === key) return cached.list;
    // Through the background worker: the page's content security policy
    // does not list the boards API, so a fetch from here is refused.
    const res = await new Promise((resolve) =>
      chrome.runtime.sendMessage({ kind: "get", url: `https://${apiHost()}/v1/boards/${p.board}/jobs/${p.id}?questions=true` }, resolve),
    );
    if (!res || !res.ok) return [];
    const j = res.json;
    const list = [];
    const groups = [...(j.questions || []), ...(j.location_questions || [])];
    for (const section of j.compliance || []) groups.push(...(section.questions || []));
    for (const q of groups) {
      for (const f of q.fields || []) {
        if (SKIP.has(f.name) || f.type === "input_hidden") continue;
        list.push({ name: f.name, label: clean(q.label), required: !!q.required, type: f.type, values: (f.values || []).map((v) => v.label) });
      }
    }
    cached = { key, list };
    return list;
  }

  function kindOf(f, ctl) {
    if (f.type === "input_file") return "file";
    if (f.type === "textarea") return "long";
    if (f.type === "multi_value_multi_select") return "multiselect";
    if (f.type === "multi_value_single_select") return "select";
    if (f.name === "location" || (ctl && isReactSelect(ctl))) return "select";
    return "text";
  }

  async function read() {
    const out = [];
    for (const f of await questions()) {
      const ctl = control(f.name);
      if (!ctl) continue;
      const kind = kindOf(f, ctl);
      out.push({
        key: f.name,
        label: f.label,
        kind,
        required: f.required,
        options: kind === "multiselect" || kind === "select" ? f.values : [],
        _ctl: ctl,
      });
    }
    return out;
  }

  const boxes = (name) => [...document.querySelectorAll(`${FORM} input[type=checkbox][name="${CSS.escape(name)}"]`)];
  const boxLabel = (box) => clean(document.querySelector(`label[for="${CSS.escape(box.id)}"]`)?.innerText);

  function current(field) {
    const ctl = field._ctl;
    if (field.kind === "multiselect") return boxes(field.key).filter((b) => b.checked).map(boxLabel).join(" | ");
    if (field.kind === "file") return ctl.files && ctl.files.length ? ctl.files[0].name : "";
    if (isReactSelect(ctl)) return shellOf(ctl)?.querySelector('[class*="select__single-value"]')?.innerText.trim() || "";
    return ctl.value || "";
  }

  // Open the menu, wait for its options, click the one that matches.
  async function pickReactSelect(ctl, value, search) {
    const shell = shellOf(ctl);
    if (!shell) return false;
    const menuOptions = () => [...shell.querySelectorAll('[class*="select__option"]')];
    const wants = String(value ?? "").split("|").map((s) => s.trim()).filter(Boolean);
    for (const want of wants) {
      const controlEl = shell.querySelector(".select__control") || shell;
      mouse(controlEl, ["mousedown"]);
      ctl.focus();
      if (search) setNative(ctl, want);
      let opts = [];
      for (let i = 0; i < 12 && !opts.length; i++) {
        await sleep(250);
        opts = menuOptions();
        if (!opts.length && i === 3) {
          ctl.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", code: "ArrowDown", keyCode: 40, which: 40, bubbles: true, view: window }));
        }
      }
      const low = want.toLowerCase();
      const hit =
        opts.find((o) => o.innerText.trim().toLowerCase() === low) ||
        opts.find((o) => o.innerText.trim().toLowerCase().startsWith(low)) ||
        (search ? opts[0] : null);
      if (hit) {
        mouse(hit, ["mousedown", "mouseup", "click"]);
        await sleep(300);
        if (current({ _ctl: ctl, kind: "select" })) return true;
      }
      ctl.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", keyCode: 27, bubbles: true, view: window }));
      if (search) setNative(ctl, "");
      await sleep(150);
    }
    return false;
  }

  async function fill(field, value, file) {
    const ctl = field._ctl;
    const want = String(value ?? "").trim().toLowerCase();
    if (field.kind === "file") {
      if (!file) return false;
      const dt = new DataTransfer();
      dt.items.add(file);
      ctl.files = dt.files;
      ctl.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    if (field.kind === "multiselect") {
      let took = false;
      for (const part of want.split("|").map((s) => s.trim()).filter(Boolean)) {
        const box = boxes(field.key).find((b) => boxLabel(b).toLowerCase() === part);
        if (box && !box.checked) {
          box.click();
          took = true;
        }
      }
      return took;
    }
    if (isReactSelect(ctl)) return pickReactSelect(ctl, value, field.key === "location" || field.key === "candidate-location" || !field.options.length);
    if (ctl.tagName === "SELECT") {
      const opt = [...ctl.options].find((o) => o.text.trim().toLowerCase() === want);
      if (!opt) return false;
      ctl.value = opt.value;
      ctl.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    ctl.focus();
    setNative(ctl, value);
    return true;
  }

  const submitButton = () => document.querySelector(`${FORM} button[type=submit]`);
  // The confirmation Greenhouse shows in place of the form.
  const submitted = () =>
    !document.querySelector(FORM) && /thank you for applying|application has been submitted|application received/i.test(document.body.innerText);

  window.__jtReader = {
    host: "greenhouse",
    ready: () => !!document.querySelector(FORM) && !!posting(),
    read,
    fill,
    current,
    submitButton,
    submitted,
  };
})();
