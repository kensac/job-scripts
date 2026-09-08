// Lever's hosted application form (jobs.lever.co/<company>/<id>/apply).
// Plain HTML, measured 2026-09-07: one .application-question per field
// with a .application-label; text and email inputs by name (name, email,
// phone, location, org, urls[LinkedIn] ...); a select for the location a
// posting is open in and for the EEO answers; custom questions as cards,
// cards[<card>][field<n>], radios whose value is the option text,
// checkboxes for pick-many, textareas for free text (the key the API's
// form reader stores drafts under); the resume as input[name=resume].
// Submit is #btn-submit and the thank-you page replaces the form.
//
// The named fields are the same on every Lever form, so the reader names
// their fact outright and the label is not read for them: the EEO
// selects, the disability signature (the person's name and today's date),
// the url slots. The location box is a typeahead over a hidden
// selected-location; a typed value counts once a result is clicked.
// Employers add "additional cards" for education and employment, one
// card block per entry with the same labels inside; those are offered as
// group fields filled from the profile's rows.
(() => {
  const FORM = "form#application-form";
  const clean = (s) => (s || "").replace(/\s+/g, " ").replace(/[✱*]\s*$/, "").trim();
  const PLACEHOLDER = /^select\b|^choose\b|^--/i;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function setNative(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement : HTMLInputElement;
    Object.getOwnPropertyDescriptor(proto.prototype, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // The fact each of Lever's fixed field names fills, so the resolve
  // never has to guess it from a label.
  const FACT_BY_NAME = {
    name: "full_name",
    email: "email",
    phone: "phone",
    org: "current_company",
    "urls[LinkedIn]": "linkedin",
    "urls[GitHub]": "github",
    "urls[Portfolio]": "website",
    "urls[Twitter]": "twitter",
    "urls[Other]": "website",
    "eeo[gender]": "gender",
    "eeo[race]": "ethnicity",
    "eeo[veteran]": "veteran",
    "eeo[disability]": "disability",
    "eeo[disabilitySignature]": "full_name",
    "eeo[disabilitySignatureDate]": "today",
  };
  const LOCATION = "#location-input";

  const controls = (q) => [...q.querySelectorAll("input:not([type=hidden]), textarea, select")];
  const optionLabel = (input) =>
    clean(input.closest("label")?.innerText || document.querySelector(`label[for="${CSS.escape(input.id)}"]`)?.innerText || input.value);
  const labelOf = (q) => clean(q.querySelector(".application-label")?.innerText || q.querySelector("label")?.innerText);

  // ---- the additional cards: education and employment entries ------------
  const CARDS = 'div[data-qa="additional-cards"]';
  const cardKind = (block) => {
    const h = clean(block.querySelector("h4, h3, h2")?.innerText).toLowerCase();
    if (/education/.test(h) && !/high/.test(h)) return "education";
    if (/employment|experience|work history/.test(h)) return "experience";
    return null;
  };
  const cardBlocks = (kind) => [...document.querySelectorAll(`${FORM} ${CARDS}`)].filter((b) => cardKind(b) === kind);
  // A question inside a card block that is an education or employment entry.
  const inGroup = (q) => {
    const block = q.closest(CARDS);
    return !!(block && cardKind(block));
  };

  function read() {
    const out = [];
    for (const q of document.querySelectorAll(`${FORM} .application-question`)) {
      if (inGroup(q)) continue;
      const ctls = controls(q);
      if (!ctls.length) continue;
      const label = labelOf(q);
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
      const field = {
        key: first.name || first.id || "label:" + label,
        label,
        kind,
        required: /✱/.test(q.innerText) || ctls.some((c) => c.required),
        options,
        _box: q,
      };
      if (FACT_BY_NAME[first.name]) field.fact = FACT_BY_NAME[first.name];
      out.push(field);
    }
    for (const kind of ["education", "experience"]) {
      const blocks = cardBlocks(kind);
      if (blocks.length) {
        out.push({ key: kind, label: kind === "education" ? "Education" : "Employment", kind: "group", fact: kind, required: false, options: [], _box: blocks[0].parentElement || blocks[0], _cards: kind });
      }
    }
    return out;
  }

  // The control under the label in a card that reads like the pattern.
  function cardControl(block, re, nth = 0) {
    const hits = [...block.querySelectorAll(".application-question")].filter((q) => re.test(labelOf(q).toLowerCase()));
    const q = hits[nth];
    return q ? controls(q)[0] || null : null;
  }
  function chooseSelect(sel, wants) {
    const low = wants.filter(Boolean).map((w) => String(w).toLowerCase());
    const opt = [...sel.options].find((o) => low.includes(o.text.trim().toLowerCase()) || low.some((w) => o.text.trim().toLowerCase().startsWith(w)));
    if (!opt) return false;
    sel.value = opt.value;
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }
  function put(ctl, value) {
    if (!ctl || value == null || value === "") return false;
    const wants = String(value).split("|").map((s) => s.trim()).filter(Boolean);
    if (ctl.tagName === "SELECT") return chooseSelect(ctl, wants);
    if (ctl.type === "radio" || ctl.type === "checkbox") {
      const row = [...ctl.closest(".application-question")?.querySelectorAll(`input[name="${CSS.escape(ctl.name)}"]`) || [ctl]];
      const low = wants.map((w) => w.toLowerCase());
      const hit = row.find((c) => low.includes(optionLabel(c).toLowerCase()));
      if (!hit) return false;
      if (!hit.checked) hit.click();
      return true;
    }
    setNative(ctl, wants[0]);
    return true;
  }
  const year = (s) => (String(s || "").match(/\d{4}/) || [null])[0];
  const mmddyyyy = (s) => {
    const t = String(s || "").trim();
    let m = t.match(/^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$/);
    if (m) return `${m[2].padStart(2, "0")}/${(m[3] || "01").padStart(2, "0")}/${m[1]}`;
    m = t.match(/^(\d{1,2})\/(\d{4})$/);
    if (m) return `${m[1].padStart(2, "0")}/01/${m[2]}`;
    return t;
  };
  const rows = (kind) => ((window.__jtProfile || {})[kind] || []).filter((r) => r && (r.school || r.company));

  function fillCards(kind) {
    const blocks = cardBlocks(kind);
    const items = rows(kind);
    let filled = false;
    blocks.forEach((block, i) => {
      const row = items[i];
      if (!row) return;
      if (kind === "education") {
        filled = put(cardControl(block, /^(school|university|college|institution)/), row.school) || filled;
        put(cardControl(block, /^gpa/), row.gpa);
        put(cardControl(block, /^degree/), row.degree);
        put(cardControl(block, /^(major|field of study)/), row.field);
        const y1 = year(row.start);
        const y2 = year(row.end);
        put(cardControl(block, /^years/), y1 && y2 ? String(Math.max(0, y2 - y1)) : null);
        const done = y2 && new Date(+y2, 11, 31) <= new Date();
        put(cardControl(block, /^graduated/), y2 ? (done ? "Yes" : "No") : null);
      } else {
        filled = put(cardControl(block, /^(company|employer|name)/), row.company) || filled;
        put(cardControl(block, /^(title|position)/), row.title);
        put(cardControl(block, /^(location|city, state|city)/), row.location);
        put(cardControl(block, /^date|start/), row.start ? mmddyyyy(row.start) : null);
        put(cardControl(block, /^date|end/, 1), row.current ? null : row.end ? mmddyyyy(row.end) : null);
        const y1 = year(row.start);
        const y2 = row.current ? String(new Date().getFullYear()) : year(row.end);
        put(cardControl(block, /^years/), y1 && y2 ? String(Math.max(0, y2 - y1)) : null);
        put(cardControl(block, /^description/), row.description);
      }
    });
    return filled;
  }
  function cardsCurrent(kind) {
    const n = cardBlocks(kind).filter((b) => cardControl(b, kind === "education" ? /^(school|university|college|institution)/ : /^(company|employer|name)/)?.value).length;
    return n ? `${n} entries` : "";
  }

  function current(field) {
    if (field._cards) return cardsCurrent(field._cards);
    const ctls = controls(field._box);
    const first = ctls[0];
    if (!first) return "";
    if (first.type === "file") return first.files && first.files.length ? first.files[0].name : "";
    if (first.tagName === "SELECT") return PLACEHOLDER.test(first.selectedOptions[0]?.text || "") ? "" : (first.selectedOptions[0]?.text.trim() || "");
    if (first.type === "radio" || first.type === "checkbox") {
      return ctls.filter((c) => c.checked).map(optionLabel).join(" | ");
    }
    return first.value || "";
  }

  // The location typeahead: type, wait for its results, take the first.
  // Without the click the hidden selected-location stays empty and the
  // form counts the field as blank.
  async function pickLocation(ctl, value) {
    const wants = String(value ?? "").split("|").map((s) => s.trim()).filter(Boolean);
    const root = ctl.closest(".application-question") || ctl.parentElement;
    for (const want of wants) {
      ctl.focus();
      setNative(ctl, want);
      let hit = null;
      for (let i = 0; i < 12 && !hit; i++) {
        await sleep(250);
        hit = root.querySelector(".dropdown-results .dropdown-location") || document.querySelector(".dropdown-results .dropdown-location");
      }
      if (hit) {
        hit.click();
        await sleep(200);
        return true;
      }
    }
    return !!ctl.value;
  }

  async function fill(field, value, file) {
    if (field._cards) return fillCards(field._cards);
    const ctls = controls(field._box);
    const first = ctls[0];
    if (!first) return false;
    const wants = String(value ?? "").split("|").map((s) => s.trim().toLowerCase()).filter(Boolean);
    if (first.type === "file") {
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
    if (first.matches(LOCATION)) return pickLocation(first, value);
    // The disability signature date wants MM/DD/YYYY; the today fact is ISO.
    const text = first.name === "eeo[disabilitySignatureDate]" ? mmddyyyy(value) : value;
    first.focus();
    setNative(first, text);
    return true;
  }

  const submitButton = () => document.querySelector("#btn-submit") || document.querySelector(`${FORM} button[type=submit]`);
  // The posting page's own Apply link, for a page that has not opened the form.
  const applyButton = () =>
    document.querySelector('a[data-qa="show-page-apply"]') || document.querySelector('a.template-btn-submit[href*="/apply"]') || null;
  const submitted = () =>
    !!document.querySelector('h3[data-qa="msg-submit-success"]') ||
    /\/thanks\b/.test(location.pathname) ||
    (!document.querySelector(FORM) && /application (submitted|received)|thank you for (submitting|applying)|thanks for submit/i.test(document.body.innerText));

  window.__jtReader = {
    host: "lever",
    ready: () => !!document.querySelector(`${FORM} .application-question`),
    read,
    fill,
    current,
    submitButton,
    applyButton,
    submitted,
  };
})();
