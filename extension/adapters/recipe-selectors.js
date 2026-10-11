export function createRecipeSelectors(operation) {
  const sleep = (ms) => operation.sleep(ms);
  const clean = (s) => (s || "").replace(/\s+/g, " ").replace(/[*✱]\s*$/, "").trim();
  const list = (x) => (Array.isArray(x) ? x : x ? [x] : []);
  const matchesPattern = (pattern, url) => {
    const re = new RegExp(
      "^" + pattern.split("*").map((s) => s.replace(/[.+?^${}()|[\]\\]/g, "\\$&")).join(".*") + "$",
    );
    return re.test(url);
  };

  // XPath with the table's placeholders: %INPUTPATH% is the field's own
  // path, %VALUE% and its cases the value being filled, %NUMBER0% and
  // %INDEX0% the entry of a repeated group, one- and zero-based.
  function expand(path, ctx) {
    const v = String(ctx.value ?? "");
    const raw = String(ctx.raw ?? v);
    return path
      .replaceAll("%INPUTPATH%", ctx.inputPath || "")
      .replaceAll("%UPPERUNMAPPEDVALUE%", raw.toUpperCase())
      .replaceAll("%LOWERUNMAPPEDVALUE%", raw.toLowerCase())
      .replaceAll("%UNMAPPEDVALUE%", raw)
      .replaceAll("%UPPERVALUE%", v.toUpperCase())
      .replaceAll("%LOWERVALUE%", v.toLowerCase())
      .replaceAll("%VALUE%", v)
      .replaceAll("%NUMBER0%", String((ctx.index ?? 0) + 1))
      .replaceAll("%INDEX0%", String(ctx.index ?? 0))
      .replaceAll("%LENGTHPLUSONE0%", String((ctx.length ?? 0) + 1));
  }
  function $x(path, root = document) {
    const out = [];
    try {
      const it = document.evaluate(path, root, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      for (let i = 0; i < it.snapshotLength; i++) out.push(it.snapshotItem(i));
    } catch (_) {
      // A selector the page's XPath engine rejects is a selector that
      // matches nothing.
    }
    return out;
  }
  const text = (node) => (node && node.nodeType === Node.TEXT_NODE ? node.textContent : node ? node.innerText || node.textContent : "");
  const visible = (el) => !!el && (el.nodeType !== Node.ELEMENT_NODE || el.offsetParent !== null || el.getClientRects().length > 0 || el.type === "file" || el.type === "hidden");
  // A variant marked hidden may fill an element the page does not show (a
  // radio behind a styled label); everything else must be visible.
  function first(paths, ctx = {}, root = document, allowHidden = false) {
    for (const p of list(paths)) {
      const hit = $x(expand(p, ctx), root).find((el) => allowHidden || visible(el));
      if (hit) return { el: hit, path: expand(p, ctx) };
    }
    return null;
  }
  async function waitFirst(paths, ctx, root, allowHidden, ms) {
    await operation.checkpoint();
    const deadline = Date.now() + (ms || 0);
    for (;;) {
      const hit = first(paths, ctx, root, allowHidden);
      if (hit || Date.now() >= deadline) return hit;
      await sleep(150);
    }
  }
  // Nested lists of paths: every path in an inner list must match for the
  // inner list to count, any inner list matching is a match.
  const anyGroup = (groups) => list(groups).some((g) => list(g).length && list(g).every((p) => $x(p).length));

  return { clean, list, matchesPattern, expand, $x, text, visible, first, waitFirst, anyGroup };
}
