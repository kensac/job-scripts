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

  // The API's name for the location question is "location"; the page's
  // control is "candidate-location".
  const ALIAS = { location: "candidate-location" };
  function control(name) {
    if (name.endsWith("[]")) return document.querySelector(`${FORM} input[name="${CSS.escape(name)}"]`);
    return (
      document.getElementById(name) ||
      (ALIAS[name] && document.getElementById(ALIAS[name])) ||
      document.querySelector(`${FORM} [name="${CSS.escape(name)}"]`)
    );
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
    // The employer's own self-identification questions (gender identity,
    // transgender, orientation, race, veteran, disability, first generation
    // on Gusto's form, report 5). They have no field name on the page, so
    // the control is found by the question's label.
    for (const q of (j.demographic_questions && j.demographic_questions.questions) || []) {
      const options = (q.answer_options || []).filter((o) => !o.free_form).map((o) => clean(o.label));
      list.push({ name: `demographic_${q.id}`, label: clean(q.label), required: !!q.required, type: q.type, values: options, byLabel: true });
    }
    cached = { key, list };
    return list;
  }

  // The control under the label that reads like this question.
  function controlByLabel(label) {
    const want = clean(label).toLowerCase();
    for (const lab of document.querySelectorAll(`${FORM} label`)) {
      const t = clean(lab.innerText).toLowerCase();
      if (!t || !(t === want || t.startsWith(want.slice(0, 60)) || want.startsWith(t.slice(0, 60)))) continue;
      const id = lab.getAttribute("for");
      const byFor = id && document.getElementById(id);
      if (byFor) return byFor;
      const inside = lab.querySelector("input:not([type=hidden]), select, textarea");
      if (inside) return inside;
      const near = lab.parentElement && lab.parentElement.querySelector("input:not([type=hidden]), select, textarea");
      if (near) return near;
    }
    return null;
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
      const ctl = f.byLabel ? controlByLabel(f.label) : control(f.name);
      if (!ctl) continue;
      const kind = kindOf(f, ctl);
      // The page pairs the API's location question with a Country box of
      // its own that the API never lists (Gusto, 2026-09-08). It goes
      // first: the city search is scoped to the country.
      if (f.name === "location") {
        const country = document.getElementById("country");
        if (country) out.push({ key: "country", label: "Country", kind: "select", required: true, options: [], _ctl: country });
      }
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
  //
  // On the Yext form, 2026-09-08, a visible tab left every react-select
  // closed to a mousedown on the control, a focus event, ArrowDown and
  // typing (report 3). The sequence that works on these widgets goes on
  // the value box: a focus event, mousedown, mouseup, click, and up to
  // eight seconds of patience for the menu; that sequence goes first. The
  // menu's options carry ids of the form react-select-<input id>-option-N,
  // so they are found by id anywhere in the document, portal or not. The
  // trace of every attempt rides on the field into the page report.
  const keydown = (el, key, keyCode) =>
    el.dispatchEvent(new KeyboardEvent("keydown", { key, code: key, keyCode, which: keyCode, bubbles: true, cancelable: true }));
  const gesture = (el) => {
    const t = { bubbles: true, cancelable: true };
    el.dispatchEvent(new FocusEvent("focus", t));
    el.dispatchEvent(new MouseEvent("mousedown", t));
    el.dispatchEvent(new MouseEvent("mouseup", t));
    el.click();
  };

  async function pickReactSelect(field, ctl, value, search) {
    const shell = shellOf(ctl);
    const trace = [];
    field._trace = trace;
    if (!shell) {
      trace.push("no shell");
      return false;
    }
    const controlEl = shell.querySelector(".select__control") || shell;
    const valueBox = shell.querySelector('[class*="select__value-container"]') || controlEl;
    const menuOptions = () => [
      ...document.querySelectorAll(`[id^="react-select-${CSS.escape(ctl.id)}-option-"], [class*="select__option"]`),
    ].filter((o) => shell.contains(o) || o.id.startsWith(`react-select-${ctl.id}-option-`));
    const opened = () => ctl.getAttribute("aria-expanded") === "true" || menuOptions().length > 0;
    const waitOpen = async (label, ms) => {
      for (let i = 0; i < ms / 150 && !opened(); i++) await sleep(150);
      trace.push(`${label}:${opened() ? "open" : "closed"}`);
      return opened();
    };
    const wants = String(value ?? "").split("|").map((s) => s.trim()).filter(Boolean);
    // A search box (the location) asks a geocoder: its menu reports closed
    // and shows no notice while the answer is on its way, only a spinner
    // for a moment, and the answer can take seconds (Gusto, 2026-09-08).
    // So it is typed into at once, and the options are waited for until
    // they come, or the spinner has come and gone with none, or eight
    // seconds pass; two seconds at least.
    const loading = () =>
      !!shell.querySelector('[class*="loading-indicator"]') ||
      [...document.querySelectorAll('[class*="select__menu-notice"]')].some((n) => /loading/i.test(n.innerText));
    for (const want of wants) {
      const low = want.toLowerCase();
      let open = false;
      if (search) {
        gesture(valueBox);
        await sleep(150);
      } else {
        // 1. The value-box gesture, with patience.
        gesture(valueBox);
        open = await waitOpen("gesture", 3000);
        // 2. The same on the control, then the focus it hands the input.
        if (!open) {
          gesture(controlEl);
          ctl.focus();
          open = await waitOpen("control+focus", 1500);
        }
        // 3. The keyboard: ArrowDown opens a closed menu.
        if (!open) {
          keydown(ctl, "ArrowDown", 40);
          open = await waitOpen("arrowdown", 900);
        }
      }
      // 4. Typing filters the list and opens it; the search box needs it.
      if (!open || search) {
        setNative(ctl, want);
        if (!search) open = await waitOpen("typed", 1200);
      }
      trace.push(`active:${document.activeElement === ctl}`);
      let opts = [];
      let sawLoading = false;
      for (let i = 0; i < 40 && !opts.length; i++) {
        await sleep(200);
        opts = menuOptions();
        const busy = loading();
        sawLoading = sawLoading || busy;
        if (i >= 10 && !busy && (sawLoading || !search)) break;
      }
      trace.push(`options:${opts.length}${sawLoading ? " after loading" : ""}`);
      const texts = opts.map((o) => o.innerText.trim().toLowerCase());
      const parts = wants.map((w) => w.toLowerCase()).flatMap((w) => w.split(",").map((s) => s.trim())).filter((s) => s && s !== low);
      // The place itself: the first part of the fullest alternative.
      const place = (wants[0] || "").split(",")[0].trim().toLowerCase();
      const hit =
        opts.find((o, i) => texts[i] === low) ||
        // A search result must name the place; "NY" typed into the world's
        // places brought Nyala, Sudan (Gusto, 2026-09-08). The result that
        // also carries the state or country the person gave comes first.
        (search
          ? opts.find((o, i) => texts[i].includes(place) && parts.some((part) => texts[i].includes(part))) ||
            opts.find((o, i) => texts[i].includes(place))
          : opts.find((o, i) => texts[i].startsWith(low)));
      if (hit) {
        gesture(hit);
        await sleep(300);
        if (current({ _ctl: ctl, kind: "select" })) {
          trace.push("clicked:took");
          return true;
        }
        trace.push("clicked:not taken");
      }
      // 5. With text typed, Enter takes the option react-select has focused.
      if (ctl.value) {
        keydown(ctl, "Enter", 13);
        await sleep(300);
        if (current({ _ctl: ctl, kind: "select" })) {
          trace.push("enter:took");
          return true;
        }
        trace.push("enter:not taken");
      }
      keydown(ctl, "Escape", 27);
      setNative(ctl, "");
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
    if (isReactSelect(ctl)) return pickReactSelect(field, ctl, value, field.key === "location" || !field.options.length);
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
