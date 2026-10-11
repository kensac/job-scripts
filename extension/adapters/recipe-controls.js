import { commitControl } from "./dom";

export function createRecipeControls(context, getConfig, sleep, note) {
  // ---- fill methods, one per name the table uses ---------------------------
  const events = (extra) => ({ bubbles: true, cancelable: true, ...(getConfig().defaultEventOptions || {}), ...(extra || {}) });
  // Only an input or a textarea takes a value; a button or a div found by
  // a selector meant for one throws "Illegal invocation" from the native
  // setter (Workday's experience page, report 8), which aborted the fill.
  const takesValue = (el) => !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
  function setValue(el, value) {
    if (!takesValue(el)) return false;
    if (el.isContentEditable && el.tagName !== "INPUT" && el.tagName !== "TEXTAREA") {
      el.textContent = value;
      return true;
    }
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
    el.setAttribute("value", value);
    return true;
  }
  async function typeInto(el, value, opts, blur) {
    if (!takesValue(el)) return false;
    const t = events(opts);
    el.dispatchEvent(new FocusEvent("focus", t));
    setValue(el, value);
    el.dispatchEvent(new CustomEvent("textInput", t));
    el.dispatchEvent(new InputEvent("input", t));
    el.dispatchEvent(new Event("change", t));
    if (blur) await commitControl(el, context.operation, t);
    return true;
  }
  // A search box behind a picker (Workday's selectinput, any input that
  // says Search or is a combobox) filters on keystrokes, not on a value
  // that appears whole: measured 2026-09-08 on Workday's country-code
  // search, a set value with one input event and Enter left the list
  // unfiltered, while the same text typed one character at a time with key
  // events, a short pause and Enter filtered it and selected the match.
  const isSearchInput = (el) =>
    !!el &&
    el.tagName === "INPUT" &&
    (el.getAttribute("data-uxi-widget-type") === "selectinput" ||
      /search/i.test(el.getAttribute("placeholder") || "") ||
      el.getAttribute("role") === "combobox" ||
      el.getAttribute("aria-autocomplete") === "list");
  async function typeSlowly(el, value, opts) {
    const t = events(opts);
    // The click that opens the widget and puts the caret in the box: the
    // recipe's own click lands on the field's frame, and the measured
    // sequence clicked the input itself (captures 16 to 20).
    if (document.querySelector('[data-automation-activepopup="true"], [class*="select__menu"]')) {
      // A list left open by the step before would take the keystrokes.
      (document.activeElement || document.body).dispatchEvent(new KeyboardEvent("keydown", { ...t, key: "Escape", code: "Escape", keyCode: 27, which: 27 }));
      await sleep(300);
    }
    el.focus();
    el.dispatchEvent(new FocusEvent("focus", t));
    if (typeof el.click === "function") el.click();
    await sleep(300);
    note(`typing "${String(value).slice(0, 30)}" into ${el.id || el.name || "search"} one key at a time`);
    setValue(el, "");
    el.dispatchEvent(new InputEvent("input", { ...t, inputType: "deleteContentBackward" }));
    let typed = "";
    for (const ch of String(value ?? "")) {
      el.dispatchEvent(new KeyboardEvent("keydown", { ...t, key: ch }));
      el.dispatchEvent(new KeyboardEvent("keypress", { ...t, key: ch }));
      typed += ch;
      setValue(el, typed);
      el.dispatchEvent(new InputEvent("input", { ...t, data: ch, inputType: "insertText" }));
      el.dispatchEvent(new KeyboardEvent("keyup", { ...t, key: ch }));
      await sleep(60);
    }
    // The search needs a moment before Enter; measured at 700 ms on
    // Workday's country-code box, 400 was not enough for the field of study.
    await sleep(900);
    return true;
  }
  function clickOn(el, opts) {
    const t = events(opts);
    el.dispatchEvent(new FocusEvent("focus", t));
    el.dispatchEvent(new MouseEvent("mousedown", t));
    el.dispatchEvent(new MouseEvent("mouseup", t));
    if (typeof el.click === "function") el.click();
    else el.dispatchEvent(new MouseEvent("click", t));
    if (!(opts && opts.noBlur)) el.dispatchEvent(new FocusEvent("blur", t));
    return true;
  }
  function chooseOption(opt) {
    const sel = opt.closest("select");
    if (!sel) return clickOn(opt, { noBlur: true });
    const t = events();
    sel.dispatchEvent(new FocusEvent("focus", t));
    sel.value = opt.value;
    sel.dispatchEvent(new InputEvent("input", t));
    sel.dispatchEvent(new Event("change", t));
    sel.dispatchEvent(new FocusEvent("blur", t));
    return true;
  }
  function chooseNative(el, value) {
    const wants = String(value).split("|").map((s) => s.trim().toLowerCase()).filter(Boolean);
    const opts = [...el.options];
    const opt =
      opts.find((o) => wants.includes(o.text.trim().toLowerCase()) || wants.includes(o.value.toLowerCase())) ||
      opts.find((o) => wants.some((w) => o.text.trim().toLowerCase().startsWith(w)));
    return opt ? chooseOption(opt) : false;
  }
  // An option element the table pointed at: a native <option> is chosen
  // through its select, anything else (a listbox row) is clicked.
  const pickOption = (el) => (el.tagName === "OPTION" ? chooseOption(el) : clickOn(el, { noBlur: true }));
  function checkOrRadio(el, on) {
    const t = events();
    if (Boolean(el.checked) === Boolean(on)) return true;
    el.dispatchEvent(new FocusEvent("focus", t));
    if (el.type === "checkbox") el.click();
    else {
      el.dispatchEvent(new MouseEvent("click", t));
      el.checked = Boolean(on);
    }
    el.dispatchEvent(new InputEvent("input", t));
    el.dispatchEvent(new Event("change", t));
    el.dispatchEvent(new FocusEvent("blur", t));
    return true;
  }
  function uploadFile(el, file) {
    if (!file) return false;
    const dt = new DataTransfer();
    dt.items.add(file);
    el.files = dt.files;
    el.dispatchEvent(new Event("change", events()));
    return true;
  }
  // A rich-text editor hides its textarea behind an iframe named after it;
  // the page's editor object is out of a content script's reach, so the
  // editable body is written directly and the textarea kept in step.
  function richText(el, value) {
    const frame = document.getElementById(`${el.id}_ifr`) || el.parentElement?.querySelector("iframe");
    const body = frame?.contentDocument?.body;
    if (body) {
      body.innerHTML = String(value ?? "")
        .split(/\n{2,}/)
        .map((p) => `<p>${p.replace(/\n/g, "<br>")}</p>`)
        .join("");
      body.dispatchEvent(new InputEvent("input", { bubbles: true }));
      body.dispatchEvent(new Event("change", { bubbles: true }));
    }
    setValue(el, value);
    el.dispatchEvent(new Event("change", events()));
    return true;
  }
  async function runMethod(method, el, value, file, opts) {
    await context.operation.checkpoint();
    const m = method || getConfig().defaultMethod || "default";
    switch (m) {
      case "uploadResume":
        return uploadFile(el, file);
      case "click":
      case "reactClick":
        return clickOn(el, opts);
      case "selectCheckboxOrRadio":
        return checkOrRadio(el, true);
      case "clearValue":
        return typeInto(el, "", opts, false);
      case "setValueOnly":
        setValue(el, value);
        return true;
      case "vanillaWithBlur":
        return typeInto(el, value, opts, true);
      case "ui5": {
        // A UI5 input commits on Enter and on focus leaving, and reads the
        // value back off the element on both.
        await typeInto(el, value, opts, false);
        el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
        el.dispatchEvent(new KeyboardEvent("keyup", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
        await commitControl(el, context.operation, events(opts));
        return true;
      }
      case "tinyMCE":
        return richText(el, value);
      case "blur":
        await commitControl(el, context.operation, events(opts));
        return true;
      case "scrollListboxToOption":
        el.scrollIntoView({ block: "center" });
        return clickOn(el, { ...opts, noBlur: true });
      case "reactDatePickerMonth": {
        // A month picker fed by typing: the value, then Enter, then away.
        await typeInto(el, value, opts, false);
        el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
        await sleep(150);
        await commitControl(el, context.operation, events(opts));
        return true;
      }
      default: {
        const blur = m === "default";
        if (el.tagName === "SELECT") return chooseNative(el, value);
        if (el.type === "checkbox" || el.type === "radio") return checkOrRadio(el, true);
        if (isSearchInput(el) && value) return typeSlowly(el, value, opts);
        if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") return typeInto(el, value, opts, blur);
        return clickOn(el, opts);
      }
    }
  }

  return { events, clickOn, chooseNative, checkOrRadio, uploadFile, typeInto, pickOption, runMethod };
}
