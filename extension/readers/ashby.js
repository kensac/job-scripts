// Ashby's hosted application form (jobs.ashbyhq.com/<org>/<id>/application).
//
// Three shapes of field, measured on live forms 2026-09-07. A wrapper
// (.ashby-application-form-field-entry) carries a label and one control:
// text, email, tel, url, number, textarea, file, a date picker, a row of
// choice buttons (the yes/no pair, or more), or a combobox: the location
// search box, a country list, a school search. A choice question with a
// few options is a fieldset of radios, the options' text in the radio
// labels. A choice question with many options is a fieldset holding a
// searchable dropdown: ArrowDown opens the full list, whose results are
// .ashby-application-form-input-autocomplete-popup-result elements on
// one rendering and div[role=option] in a floating-ui portal on the
// other; a pointer sequence on one of them picks it. The location search
// box is the same widget with an unbounded list, filled by typing and
// taking the first result. In wrappers, the control's id is the key the
// API's form reader stores drafts under; a radio group's key is its
// radios' shared name, which is unique per question (the prefix is not).
//
// Education is a repeated widget of its own: one repeatableEducationEntry
// per school with a school search, a degree list, a field of study and
// month/year selects for the dates, and an add button for the next. The
// reader offers it as one group field filled from the profile's rows.
(() => {
  const WRAP = ".ashby-application-form-field-entry";
  const RESULT = ".ashby-application-form-input-autocomplete-popup-result";
  const PORTAL_RESULT = '[id^="floating-ui-"] [role="option"], [data-floating-ui-portal] [role="option"]';
  const EDU_ENTRY = '[class*="repeatableEducationEntry"]';
  const EDU_ADD = 'button[class*="repeatableEducationAddButton"]';
  const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function setNative(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement : HTMLInputElement;
    Object.getOwnPropertyDescriptor(proto.prototype, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  const keydown = (el, key, keyCode) =>
    el.dispatchEvent(new KeyboardEvent("keydown", { key, code: key, keyCode, bubbles: true }));
  const pointer = (el) => {
    for (const type of ["pointerdown", "mousedown", "mouseup", "click"]) {
      el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true }));
    }
  };

  const control = (box) => box.querySelector("input:not([type=hidden]), textarea, select");
  // A combobox by any of the marks Ashby has given it: the class, the
  // role, the aria hint, or the "Start typing..." placeholder.
  const isAuto = (ctl) =>
    !!ctl &&
    (/autocomplete/.test(ctl.className) ||
      ctl.getAttribute("role") === "combobox" ||
      ctl.getAttribute("aria-autocomplete") === "list" ||
      (!ctl.type || ctl.type === "text") && ctl.placeholder === "Start typing...");
  // A row of choice buttons: the yes/no pair, or any set of two or more
  // in a wrapper with no other control. The combobox toggle is not one.
  const choiceButtons = (box) =>
    [...box.querySelectorAll("button")].filter(
      (b) => !/toggleButton|clear/i.test(b.className) && b.innerText.trim() && !/^(add|remove|replace|upload|browse|choose)/i.test(b.innerText.trim()),
    );
  const yesno = (box) => choiceButtons(box).filter((b) => /^(yes|no)$/i.test(b.innerText.trim()));
  const isOn = (b) =>
    b.getAttribute("aria-pressed") === "true" || b.getAttribute("aria-checked") === "true" || /_active_|selected|checked|active/.test(b.className);
  const choices = (box) =>
    [...box.querySelectorAll("input[type=radio], input[type=checkbox]")].map((input) => ({
      input,
      text: (box.querySelector(`label[for="${CSS.escape(input.id)}"]`)?.innerText || "").trim(),
    }));
  const clean = (s) => (s || "").replace(/\s+/g, " ").replace(/\s*\*$/, "").trim();
  const labelOf = (box) => clean(box.querySelector("label, legend")?.innerText);
  const results = () => [...document.querySelectorAll(`${RESULT}, ${PORTAL_RESULT}`)];

  // The dropdown's full list, read once at read time: open, collect, close.
  async function listOptions(ctl) {
    ctl.focus();
    keydown(ctl, "ArrowDown", 40);
    await sleep(350);
    const texts = results().map((o) => o.innerText.trim()).filter(Boolean);
    keydown(ctl, "Escape", 27);
    ctl.blur();
    await sleep(100);
    return texts;
  }

  function kindOf(box, ctl) {
    if (yesno(box).length === 2) return "yesno";
    if (!ctl && choiceButtons(box).length >= 2) return "select";
    if (!ctl) return "unknown";
    if (ctl.tagName === "TEXTAREA") return "long";
    if (ctl.tagName === "SELECT" || isAuto(ctl)) return "select";
    if (ctl.type === "file") return "file";
    if (ctl.type === "number") return "number";
    if (ctl.type === "date" || /date/i.test(ctl.placeholder || "")) return "date";
    return "text";
  }

  function keyOf(box, ctl, text) {
    const withId = ctl && (ctl.id || ctl.name) ? ctl.id || ctl.name : null;
    return withId || box.querySelector("[id]")?.id || "label:" + text;
  }

  async function read() {
    const out = [];
    const boxes = [...document.querySelectorAll(`${WRAP}, fieldset`)].filter(
      (b) => !b.parentElement.closest(`${WRAP}, fieldset`) && !b.closest(EDU_ENTRY),
    );
    for (const box of boxes) {
      const text = labelOf(box);
      // The yes/no pair carries a hidden checkbox, so it is checked first.
      const opts = yesno(box).length === 2 ? [] : choices(box);
      if (opts.length) {
        const multi = opts[0].input.type === "checkbox";
        out.push({
          key: opts[0].input.name || "label:" + text,
          label: text,
          kind: multi ? "multiselect" : "select",
          required: opts.some((o) => o.input.required),
          options: opts.map((o) => o.text),
          _box: box,
          _group: true,
        });
        continue;
      }
      const ctl = control(box);
      const kind = kindOf(box, ctl);
      let options = [];
      if (kind === "yesno") options = yesno(box).map((b) => b.innerText.trim());
      else if (!ctl && kind === "select") options = choiceButtons(box).map((b) => b.innerText.trim());
      else if (ctl && ctl.tagName === "SELECT") options = [...ctl.options].map((o) => o.text).filter(Boolean);
      // A dropdown in a fieldset is a fixed list worth reading; the location
      // box in a wrapper searches the world and is filled by typing.
      else if (isAuto(ctl) && box.tagName === "FIELDSET") options = await listOptions(ctl);
      out.push({
        key: keyOf(box, ctl, text),
        label: text,
        kind,
        required: !!(ctl && (ctl.required || ctl.getAttribute("aria-required") === "true")),
        options,
        _box: box,
        _buttons: !ctl && kind === "select",
      });
    }
    // The education widget, once, filled from the profile's rows.
    const add = document.querySelector(EDU_ADD);
    const entry = document.querySelector(EDU_ENTRY);
    if (add || entry) {
      const box = (add || entry).closest(WRAP) || (add || entry).parentElement;
      out.push({ key: "education", label: "Education", kind: "group", fact: "education", required: false, options: [], _box: box, _edu: true });
    }
    return out;
  }

  // ---- education -----------------------------------------------------------
  function parseDate(s) {
    if (!s) return null;
    const t = String(s).trim();
    let m = t.match(/^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$/);
    if (m) return { y: +m[1], m: +m[2] };
    m = t.match(/^(\d{1,2})\/(?:(\d{1,2})\/)?(\d{4})$/);
    if (m) return { y: +m[3], m: +m[1] };
    m = t.match(/^([A-Za-z]{3,9})\.?\s+(\d{4})$/);
    if (m) {
      const idx = MONTHS.findIndex((n) => n.toLowerCase().startsWith(m[1].slice(0, 3).toLowerCase()));
      if (idx >= 0) return { y: +m[2], m: idx + 1 };
    }
    m = t.match(/^(\d{4})$/);
    if (m) return { y: +m[1], m: 1 };
    return null;
  }
  function chooseSelect(sel, wants) {
    const low = wants.filter(Boolean).map((w) => String(w).toLowerCase());
    const opt = [...sel.options].find((o) => low.includes(o.text.trim().toLowerCase()) || low.includes(o.value.toLowerCase()));
    if (!opt) return false;
    sel.value = opt.value;
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }
  // The month and year selects that follow the date's label in an entry.
  function setDate(entry, which, d) {
    if (!d) return;
    const lab = entry.querySelector(`label[for*="education_history-${which}"]`);
    const after = [...entry.querySelectorAll("select")].filter((s) => !lab || lab.compareDocumentPosition(s) & Node.DOCUMENT_POSITION_FOLLOWING);
    const [month, year] = after;
    if (month) chooseSelect(month, [MONTHS[d.m - 1], String(d.m), String(d.m).padStart(2, "0")]);
    if (year) chooseSelect(year, [String(d.y)]);
  }
  const schoolBox = (entry) => {
    const lab = entry.querySelector('label[for*="education_history-school"]');
    const combos = [...entry.querySelectorAll('input[role="combobox"], input[id*="education_history-school"]')];
    return combos.find((c) => !lab || lab.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING) || combos[0] || null;
  };
  const eduRows = () => ((window.__jtProfile || {}).education || []).filter((r) => r && r.school);

  function eduCurrent() {
    const n = [...document.querySelectorAll(EDU_ENTRY)].filter((e) => schoolBox(e)?.value).length;
    return n ? `${n} entries` : "";
  }

  async function fillEducation(rows) {
    let filled = 0;
    for (let i = 0; i < rows.length; i++) {
      let entries = [...document.querySelectorAll(EDU_ENTRY)];
      if (entries.length <= i) {
        const add = document.querySelector(EDU_ADD);
        if (!add) break;
        add.click();
        for (let t = 0; t < 20 && document.querySelectorAll(EDU_ENTRY).length <= i; t++) await sleep(150);
        entries = [...document.querySelectorAll(EDU_ENTRY)];
        if (entries.length <= i) break;
      }
      const entry = entries[i];
      const row = rows[i];
      const school = schoolBox(entry);
      // The school list is a search over institutions: only a result that
      // is the school, never the first thing offered.
      if (school && !school.value) await pickFromList(school, row.school, true);
      const degree = entry.querySelector('input[id*="education_history-degree"], select[id*="education_history-degree"]');
      if (degree && row.degree) {
        if (degree.tagName === "SELECT") chooseSelect(degree, [row.degree]);
        else if (isAuto(degree)) await pickFromList(degree, row.degree, false);
        else setNative(degree, row.degree);
      }
      const major = entry.querySelector('input[id*="education_history-major"]');
      if (major && row.field) setNative(major, row.field);
      setDate(entry, "startDate", parseDate(row.start));
      setDate(entry, "endDate", parseDate(row.end));
      filled++;
    }
    return filled > 0;
  }

  function current(field) {
    const box = field._box;
    if (field._edu) return eduCurrent();
    if (field._group) {
      return choices(box)
        .filter((o) => o.input.checked)
        .map((o) => o.text)
        .join(" | ");
    }
    if (field.kind === "yesno" || field._buttons) {
      const on = choiceButtons(box).find(isOn);
      return on ? on.innerText.trim() : "";
    }
    const ctl = control(box);
    if (!ctl) return "";
    if (ctl.type === "file") return ctl.files && ctl.files.length ? ctl.files[0].name : "";
    return ctl.value || "";
  }

  // Type into a dropdown or search box and take a result. Alternatives
  // ("New York, NY, United States | New York") are tried in order until
  // one produces a result; a fixed list wants the exact option text. With
  // strict set, only a result that is the text or starts with it is taken.
  async function pickFromList(ctl, value, strict = false) {
    const wants = String(value ?? "")
      .split("|")
      .map((s) => s.trim())
      .filter(Boolean);
    for (const want of wants) {
      ctl.focus();
      setNative(ctl, want);
      // The location box geocodes on the network; results take up to a
      // second or two, or never come for a phrasing it does not know.
      let found = [];
      for (let i = 0; i < 10 && !found.length; i++) {
        await sleep(250);
        found = results();
      }
      const low = want.toLowerCase();
      const hit =
        found.find((o) => o.innerText.trim().toLowerCase() === low) ||
        found.find((o) => o.innerText.trim().toLowerCase().startsWith(low)) ||
        (strict ? null : found[0]);
      if (hit) {
        pointer(hit);
        await sleep(300);
        if (ctl.value) return true;
      }
      setNative(ctl, "");
      ctl.blur();
      await sleep(250);
    }
    return false;
  }

  async function fill(field, value, file) {
    const box = field._box;
    const want = String(value ?? "").trim().toLowerCase();
    if (field._edu) return fillEducation(eduRows());
    if (field._group) {
      let took = false;
      for (const part of want.split("|").map((s) => s.trim()).filter(Boolean)) {
        const opt = choices(box).find((o) => o.text.toLowerCase() === part);
        if (!opt) continue;
        if (!opt.input.checked) (box.querySelector(`label[for="${CSS.escape(opt.input.id)}"]`) || opt.input).click();
        took = true;
        if (field.kind !== "multiselect") break;
      }
      return took;
    }
    if (field.kind === "yesno" || field._buttons) {
      const wants = want.split("|").map((s) => s.trim()).filter(Boolean);
      const btn = choiceButtons(box).find((b) => wants.includes(b.innerText.trim().toLowerCase()));
      if (!btn) return false;
      btn.click();
      return true;
    }
    const ctl = control(box);
    if (!ctl) return false;
    if (field.kind === "file") {
      if (!file) return false;
      const dt = new DataTransfer();
      dt.items.add(file);
      ctl.files = dt.files;
      ctl.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    if (ctl.tagName === "SELECT") {
      const opt = [...ctl.options].find((o) => o.text.trim().toLowerCase() === want);
      if (!opt) return false;
      ctl.value = opt.value;
      ctl.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    if (isAuto(ctl)) return pickFromList(ctl, value);
    ctl.focus();
    setNative(ctl, value);
    // No blur: on a form with eager validation it raises the error state
    // on a field the person has not reached.
    return true;
  }

  function submitButton() {
    return (
      document.querySelector("button.ashby-application-form-submit-button") ||
      [...document.querySelectorAll("button")].find((b) => /submit application/i.test(b.innerText))
    );
  }
  // The posting page's link to its own form, for a page that has not
  // opened it yet.
  const applyButton = () => [...document.querySelectorAll('a[href$="/application"]')].find((a) => a.offsetParent) || null;

  // The confirmation screen Ashby shows once the application is in. The
  // form is gone by then, so the values are read before the click lands.
  const submitted = () => !!document.querySelector('[class*="application-form-success-container"]');

  window.__jtReader = {
    host: "ashby",
    ready: () => !!document.querySelector(`${WRAP}, fieldset`),
    read,
    fill,
    current,
    submitButton,
    applyButton,
    submitted,
  };
})();
