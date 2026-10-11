export function createRecipeFields(getConfig, list, $x, expand, visible, text, clean, isGroup) {
  // The containers the repeated groups on this page own (work experience 1,
  // 2, ...; education 1, ...): the inputs inside them are the group's, not
  // questions. Read as questions they took the block's heading as their
  // label and the resolve answered "Job Title" into company, dates and
  // description alike (Workday, report 8).
  function groupContainers(root) {
    const out = [];
    for (const f of getConfig().fields) {
      for (const v of f.variants) {
        if (!isGroup(v)) continue;
        for (const path of list(v.containerPath)) {
          for (let n = 0; n < 15; n++) out.push(...$x(expand(path, { index: n, length: 15 }), root));
        }
      }
    }
    return out;
  }
  // A section's name: the element its aria-labelledby points at, a heading
  // itself, or the first heading inside it; never its whole text.
  function sectionName(el) {
    if (!el) return "";
    const ids = (el.getAttribute && el.getAttribute("aria-labelledby")) || "";
    for (const id of ids.split(/\s+/).filter(Boolean)) {
      const t = clean(text(document.getElementById(id)));
      if (t) return t.slice(0, 80);
    }
    if (/^H[1-6]$/.test(el.tagName)) return clean(text(el)).slice(0, 80);
    const h = el.querySelector && el.querySelector("h1, h2, h3, h4, h5, h6, legend");
    return h ? clean(text(h)).slice(0, 80) : "";
  }
  function questionFields(taken, root) {
    const out = [];
    const owned = groupContainers(root);
    for (const q of getConfig().questions || []) {
      for (const box of list(q.fieldPath).flatMap((p) => $x(p, root))) {
        if (!visible(box)) continue;
        if (owned.some((c) => c !== box && c.contains(box))) continue;
        const input = list(q.inputPath).map((p) => $x(p, box)[0]).find(Boolean);
        const optionEls = list(q.optionsPath).flatMap((p) => $x(p, box));
        if (!input && !optionEls.length) continue;
        const anchor = input || optionEls[0];
        // A control a named fact already owns is not a question as well.
        if (taken.has(anchor) || optionEls.some((o) => taken.has(o))) continue;
        taken.add(anchor);
        const optionText = (o) => clean(list(q.optionsTextPath).map((p) => text($x(p, o)[0])).find((t) => t && t.trim()) || text(o) || o.value);
        let options = optionEls.map(optionText).filter(Boolean);
        // The question's label is the first candidate that is not one of its
        // own options; a group's option labels share the label selector.
        let label = clean(
          list(q.labelPath)
            .flatMap((p) => $x(p, box).map(text))
            .map(clean)
            .find((t) => t && !options.includes(t)),
        );
        if (!label) continue;
        // The section the question sits under names it too ("Education:
        // Degree" is not "Degree" on its own).
        const section = list(q.sectionPath).map((p) => sectionName($x(p, box)[0])).find((t) => t);
        if (section && !label.toLowerCase().startsWith(section.toLowerCase())) label = `${section}: ${label}`;
        let kind = "text";
        if (input && input.tagName === "TEXTAREA") kind = "long";
        else if (input && input.type === "file") kind = "file";
        else if (input && input.tagName === "SELECT") {
          kind = "select";
          options = [...input.options].map((o) => o.text.trim()).filter(Boolean);
        } else if (optionEls.length) kind = optionEls[0].type === "checkbox" ? "multiselect" : "select";
        else if (input && (input.getAttribute("role") === "combobox" || q.optionsSource)) kind = "select";
        out.push({
          key: (input && (input.name || input.id)) || (optionEls[0] && optionEls[0].name) || `label:${label}`,
          label,
          kind,
          required: /[*✱]\s*$/.test(clean(box.innerText)),
          options,
          _q: q,
          _box: box,
          _el: input || null,
          _options: optionEls.map((el, i) => ({ el, text: options[i] || "" })),
          _selectedPath: q.optionsSelectedPath || null,
          _textPath: q.inputTextPath || null,
          _fillEmpty: q.valueRequired === false,
        });
      }
    }
    return out;
  }
  return { questionFields };
}
