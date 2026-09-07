// Ashby's hosted application form (jobs.ashbyhq.com/<org>/<id>/application).
//
// Three shapes of field, measured on live forms 2026-09-07. A wrapper
// (.ashby-application-form-field-entry) carries a label and one control:
// text, email, tel, url, number, textarea, file, a date picker, a yes/no
// pair of buttons, or the location search box. A choice question with a
// few options is a fieldset of radios, the options' text in the radio
// labels. A choice question with many options is a fieldset holding a
// searchable dropdown: ArrowDown opens the full list as
// .ashby-application-form-input-autocomplete-popup-result elements, and
// a pointer sequence on one of them picks it. The location search box is
// the same widget with an unbounded list, filled by typing and taking
// the first result. In wrappers, the control's id is the key the API's
// form reader stores drafts under; a radio group's key is its radios'
// shared name, which is unique per question (the prefix is not).
(() => {
  const WRAP = ".ashby-application-form-field-entry";
  const RESULT = ".ashby-application-form-input-autocomplete-popup-result";
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
  const isAuto = (ctl) => !!ctl && /autocomplete/.test(ctl.className);
  const yesno = (box) =>
    [...box.querySelectorAll("button")].filter((b) => /^(yes|no)$/i.test(b.innerText.trim()));
  const choices = (box) =>
    [...box.querySelectorAll("input[type=radio], input[type=checkbox]")].map((input) => ({
      input,
      text: (box.querySelector(`label[for="${CSS.escape(input.id)}"]`)?.innerText || "").trim(),
    }));
  const clean = (s) => (s || "").replace(/\s+/g, " ").replace(/\s*\*$/, "").trim();
  const labelOf = (box) => clean(box.querySelector("label, legend")?.innerText);

  // The dropdown's full list, read once at read time: open, collect, close.
  async function listOptions(ctl) {
    ctl.focus();
    keydown(ctl, "ArrowDown", 40);
    await sleep(350);
    const texts = [...document.querySelectorAll(RESULT)].map((o) => o.innerText.trim()).filter(Boolean);
    keydown(ctl, "Escape", 27);
    ctl.blur();
    await sleep(100);
    return texts;
  }

  function kindOf(box, ctl) {
    if (yesno(box).length === 2) return "yesno";
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
      (b) => !b.parentElement.closest(`${WRAP}, fieldset`),
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
      });
    }
    return out;
  }

  function current(field) {
    const box = field._box;
    if (field._group) {
      return choices(box)
        .filter((o) => o.input.checked)
        .map((o) => o.text)
        .join(" | ");
    }
    if (field.kind === "yesno") {
      const on = yesno(box).find(
        (b) =>
          b.getAttribute("aria-pressed") === "true" ||
          b.getAttribute("aria-checked") === "true" ||
          /_active_|selected|checked/.test(b.className),
      );
      return on ? on.innerText.trim() : "";
    }
    const ctl = control(box);
    if (!ctl) return "";
    if (ctl.type === "file") return ctl.files && ctl.files.length ? ctl.files[0].name : "";
    return ctl.value || "";
  }

  // Type into a dropdown or search box and take a result. Alternatives
  // ("New York, NY, United States | New York") are tried in order until
  // one produces a result; a fixed list wants the exact option text.
  async function pickFromList(ctl, value) {
    const wants = String(value ?? "")
      .split("|")
      .map((s) => s.trim())
      .filter(Boolean);
    for (const want of wants) {
      ctl.focus();
      setNative(ctl, want);
      // The location box geocodes on the network; results take up to a
      // second or two, or never come for a phrasing it does not know.
      let results = [];
      for (let i = 0; i < 10 && !results.length; i++) {
        await sleep(250);
        results = [...document.querySelectorAll(RESULT)];
      }
      const low = want.toLowerCase();
      const hit =
        results.find((o) => o.innerText.trim().toLowerCase() === low) ||
        results.find((o) => o.innerText.trim().toLowerCase().startsWith(low)) ||
        results[0];
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
    if (field.kind === "yesno") {
      const btn = yesno(box).find((b) => b.innerText.trim().toLowerCase() === want);
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
    submitted,
  };
})();
