// Lever's hosted application form (jobs.lever.co/<company>/<id>/apply).
// Plain HTML, measured 2026-09-07: one .application-question per field
// with a .application-label; text and email inputs by name (name, email,
// phone, location, org, urls[LinkedIn] ...); a select for the location a
// posting is open in and for the EEO answers; custom questions as cards,
// cards[<card>][field<n>], radios whose value is the option text,
// checkboxes for pick-many, textareas for free text (the key the API's
// form reader stores drafts under); the resume as input[name=resume].
// Submit is #btn-submit and the thank-you page replaces the form.
(() => {
  const FORM = "form#application-form";
  const clean = (s) => (s || "").replace(/\s+/g, " ").replace(/[✱*]\s*$/, "").trim();
  const PLACEHOLDER = /^select\b|^choose\b|^--/i;

  function setNative(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement : HTMLInputElement;
    Object.getOwnPropertyDescriptor(proto.prototype, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  const controls = (q) => [...q.querySelectorAll("input:not([type=hidden]), textarea, select")];
  const optionLabel = (input) =>
    clean(input.closest("label")?.innerText || document.querySelector(`label[for="${CSS.escape(input.id)}"]`)?.innerText || input.value);

  function read() {
    const out = [];
    for (const q of document.querySelectorAll(`${FORM} .application-question`)) {
      const ctls = controls(q);
      if (!ctls.length) continue;
      const label = clean(q.querySelector(".application-label")?.innerText || q.querySelector("label")?.innerText);
      const first = ctls[0];
      let kind = "text";
      let options = [];
      if (first.type === "file") kind = "file";
      else if (first.tagName === "TEXTAREA") kind = "long";
      else if (first.tagName === "SELECT") {
        kind = "select";
        options = [...first.options].map((o) => o.text.trim()).filter((t) => t && !PLACEHOLDER.test(t));
      } else if (first.type === "radio") {
        kind = "select";
        options = ctls.filter((c) => c.type === "radio").map(optionLabel);
      } else if (first.type === "checkbox") {
        kind = "multiselect";
        options = ctls.filter((c) => c.type === "checkbox").map(optionLabel);
      } else if (first.type === "number") kind = "number";
      out.push({
        key: first.name || "label:" + label,
        label,
        kind,
        required: /✱/.test(q.innerText) || ctls.some((c) => c.required),
        options,
        _box: q,
      });
    }
    return out;
  }

  function current(field) {
    const ctls = controls(field._box);
    const first = ctls[0];
    if (!first) return "";
    if (field.kind === "file") return first.files && first.files.length ? first.files[0].name : "";
    if (first.tagName === "SELECT") return PLACEHOLDER.test(first.selectedOptions[0]?.text || "") ? "" : (first.selectedOptions[0]?.text.trim() || "");
    if (first.type === "radio" || first.type === "checkbox") {
      return ctls.filter((c) => c.checked).map(optionLabel).join(" | ");
    }
    return first.value || "";
  }

  async function fill(field, value, file) {
    const ctls = controls(field._box);
    const first = ctls[0];
    if (!first) return false;
    const wants = String(value ?? "").split("|").map((s) => s.trim().toLowerCase()).filter(Boolean);
    if (field.kind === "file") {
      if (!file) return false;
      const dt = new DataTransfer();
      dt.items.add(file);
      first.files = dt.files;
      first.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    if (first.tagName === "SELECT") {
      const opt = [...first.options].find((o) => wants.includes(o.text.trim().toLowerCase()));
      if (!opt) return false;
      first.value = opt.value;
      first.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    if (first.type === "radio" || first.type === "checkbox") {
      let took = false;
      for (const c of ctls) {
        if (wants.includes(optionLabel(c).toLowerCase()) && !c.checked) {
          c.click();
          took = true;
          if (first.type === "radio") break;
        }
      }
      return took;
    }
    first.focus();
    setNative(first, value);
    return true;
  }

  const submitButton = () => document.querySelector("#btn-submit") || document.querySelector(`${FORM} button[type=submit]`);
  const submitted = () => /\/thanks\b/.test(location.pathname) || (!document.querySelector(FORM) && /application submitted|thank you/i.test(document.body.innerText));

  window.__jtReader = {
    host: "lever",
    ready: () => !!document.querySelector(`${FORM} .application-question`),
    read,
    fill,
    current,
    submitButton,
    submitted,
  };
})();
