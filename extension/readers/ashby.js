// Ashby's hosted application form (jobs.ashbyhq.com/<org>/<id>/application).
//
// Two shapes of field, measured on live forms 2026-09-07. A wrapper
// (.ashby-application-form-field-entry) carries a label and one control:
// text, email, tel, url, number, textarea, file, a location autocomplete,
// a date picker, or a yes/no pair of buttons. A choice question
// (Ashby's ValueSelect) is a fieldset of radios instead, its options'
// text in the radio labels, its radios named "<field id>_<n>". In both,
// the field id is the key the API's form reader stores drafts under.
(() => {
  const WRAP = ".ashby-application-form-field-entry";
  const GROUP = "fieldset.ashby-application-form-input-radio-group";

  function setNative(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement : HTMLInputElement;
    Object.getOwnPropertyDescriptor(proto.prototype, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  const control = (wrap) => wrap.querySelector("input:not([type=hidden]), textarea, select");
  const yesno = (wrap) =>
    [...wrap.querySelectorAll("button")].filter((b) => /^(yes|no)$/i.test(b.innerText.trim()));
  const choices = (group) =>
    [...group.querySelectorAll("input[type=radio], input[type=checkbox]")].map((input) => ({
      input,
      text: (group.querySelector(`label[for="${CSS.escape(input.id)}"]`)?.innerText || "").trim(),
    }));
  const clean = (s) => (s || "").replace(/\s+/g, " ").replace(/\s*\*$/, "").trim();

  function kindOf(wrap, ctl) {
    if (yesno(wrap).length === 2) return "yesno";
    if (!ctl) return "unknown";
    if (ctl.tagName === "TEXTAREA") return "long";
    if (ctl.tagName === "SELECT") return "select";
    if (ctl.type === "file") return "file";
    if (ctl.type === "number") return "number";
    if (ctl.type === "date" || /date/i.test(ctl.placeholder || "")) return "date";
    if (/select|autocomplete/.test(ctl.className) || ctl.getAttribute("role") === "combobox") {
      return "select";
    }
    return "text";
  }

  function keyOf(wrap, ctl, text) {
    const withId = ctl && (ctl.id || ctl.name) ? ctl.id || ctl.name : null;
    return withId || wrap.querySelector("[id]")?.id || "label:" + text;
  }

  function read() {
    const out = [];
    for (const wrap of document.querySelectorAll(WRAP)) {
      const ctl = control(wrap);
      const text = clean(wrap.querySelector("label")?.innerText);
      const kind = kindOf(wrap, ctl);
      const options =
        kind === "yesno"
          ? yesno(wrap).map((b) => b.innerText.trim())
          : ctl && ctl.tagName === "SELECT"
            ? [...ctl.options].map((o) => o.text).filter(Boolean)
            : [];
      out.push({
        key: keyOf(wrap, ctl, text),
        label: text,
        kind,
        required: !!(ctl && (ctl.required || ctl.getAttribute("aria-required") === "true")),
        options,
        _wrap: wrap,
      });
    }
    for (const group of document.querySelectorAll(GROUP)) {
      const opts = choices(group);
      if (!opts.length) continue;
      const text = clean(group.querySelector("label, legend")?.innerText);
      const multi = opts[0].input.type === "checkbox";
      out.push({
        key: opts[0].input.name.replace(/_[^_]*$/, "") || "label:" + text,
        label: text,
        kind: multi ? "multiselect" : "select",
        required: opts.some((o) => o.input.required),
        options: opts.map((o) => o.text),
        _wrap: group,
        _group: true,
      });
    }
    return out;
  }

  function current(field) {
    const wrap = field._wrap;
    if (field._group) {
      return choices(wrap)
        .filter((o) => o.input.checked)
        .map((o) => o.text)
        .join(" | ");
    }
    if (field.kind === "yesno") {
      const on = yesno(wrap).find(
        (b) =>
          b.getAttribute("aria-pressed") === "true" ||
          b.getAttribute("aria-checked") === "true" ||
          /_active_|selected|checked/.test(b.className),
      );
      return on ? on.innerText.trim() : "";
    }
    const ctl = control(wrap);
    if (!ctl) return "";
    if (ctl.type === "file") return ctl.files && ctl.files.length ? ctl.files[0].name : "";
    return ctl.value || "";
  }

  async function fill(field, value, file) {
    const wrap = field._wrap;
    const want = String(value ?? "").trim().toLowerCase();
    if (field._group) {
      let took = false;
      for (const part of want.split("|").map((s) => s.trim()).filter(Boolean)) {
        const opt = choices(wrap).find((o) => o.text.toLowerCase() === part);
        if (!opt) continue;
        if (!opt.input.checked) (wrap.querySelector(`label[for="${CSS.escape(opt.input.id)}"]`) || opt.input).click();
        took = true;
        if (field.kind !== "multiselect") break;
      }
      return took;
    }
    if (field.kind === "yesno") {
      const btn = yesno(wrap).find((b) => b.innerText.trim().toLowerCase() === want);
      if (!btn) return false;
      btn.click();
      return true;
    }
    const ctl = control(wrap);
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
    ctl.focus();
    setNative(ctl, value);
    if (field.kind === "select") {
      // The location autocomplete: the typed text filters the list and
      // Enter takes the first match. What it took is read back by current().
      // This is the one control that wants the blur, to close the list.
      await new Promise((r) => setTimeout(r, 400));
      for (const type of ["keydown", "keyup"]) {
        ctl.dispatchEvent(new KeyboardEvent(type, { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
      }
      await new Promise((r) => setTimeout(r, 200));
      ctl.blur();
    }
    // No blur otherwise: on a form with eager validation it raises the
    // error state on a field the person has not reached.
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
    ready: () => !!document.querySelector(`${WRAP}, ${GROUP}`),
    read,
    fill,
    current,
    submitButton,
    submitted,
  };
})();
