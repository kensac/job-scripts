export function createRecipeActions({ context, sleep, clean, text, visible, list, $x, expand, first, waitFirst, candidates, pickOption, runMethod, events, note }) {
  let seenOptions = null;
  // The closest option on screen to a value, for a picker whose list is a
  // search the table matches by string: Workday spells a school its own
  // way ("Pennsylvania State University-Main Campus"), a degree list may
  // not carry "Bachelor of Science" as written, a field of study may only
  // offer "Applied Mathematics". Tokens of the value (stems of five
  // letters, stop words dropped) are looked for in each option; the part
  // of the value after a comma is a preference, not a requirement, so
  // "..., University Park" picks that campus among several. Close enough
  // is most of the tokens present; ties go to the shorter option.
  const STOP = new Set(["the", "of", "and", "at", "in", "a", "an"]);
  const stems = (t) =>
    String(t || "")
      .toLowerCase()
      .replace(/[^a-z0-9 ]+/g, " ")
      .split(/\s+/)
      .filter((w) => w && !STOP.has(w))
      .map((w) => w.slice(0, 5));
  function closest(options, value) {
    const [main, ...rest] = String(value || "").split(",");
    const want = stems(main);
    const prefer = stems(rest.join(" "));
    if (!want.length) return null;
    let best = null;
    for (const el of options) {
      const label = shownLabel(el);
      const have = new Set(stems(label));
      const hit = want.filter((w) => have.has(w)).length / want.length;
      if (hit < 0.6) continue;
      const bonus = prefer.length ? prefer.filter((w) => have.has(w)).length / prefer.length : 0;
      const score = hit + bonus * 0.5;
      if (!best || score > best.score || (score === best.score && label.length < best.label.length)) best = { el, label, score };
    }
    return best;
  }
  // An option's text: its own, or the label attribute Workday puts on the
  // option itself or on a child (capture 14: 45 options read as empty and
  // none could score).
  const shownLabel = (el) =>
    clean(
      text(el) ||
        el.getAttribute("data-automation-label") ||
        el.getAttribute("aria-label") ||
        el.querySelector("[data-automation-label]")?.getAttribute("data-automation-label") ||
        "",
    );
  const OPTION_SELECTORS = '[role="option"], [data-automation-id="promptLeafNode"], [data-automation-id="promptOption"], [class*="select__option"], li[role="menuitem"]';
  // The options of the popup that is open now, not every option on the
  // page: with the degree list still open, the field-of-study fallback
  // clicked "Master of Computer Science" into the degree (capture 23).
  const POPUP_SELECTORS = '[data-automation-activepopup="true"], [data-automation-id="activeListContainer"], [class*="select__menu"], [role="listbox"]';
  const optionsOnScreen = () => {
    const popups = [...document.querySelectorAll(POPUP_SELECTORS)].filter(visible);
    const scope = popups.length ? popups[popups.length - 1] : null;
    return scope ? [...scope.querySelectorAll(OPTION_SELECTORS)].filter(visible) : [];
  };
  async function runActions(actions, ctx, root, fallbackEl, value, file) {
    for (const [i, a] of (actions || []).entries()) {
      await context.operation.checkpoint();
      if (a.delay) await sleep(a.delay);
      if (a.condition && !list(a.condition).some((c) => $x(expand(c, ctx), root).length)) continue;
      const cands = candidates(a, ctx, value);
      const have = cands.some(Boolean);
      if ((a.skipOnEmptyValue || a.valueRequired) && !have) continue;
      const actx = { ...ctx, value: cands[0] || "" };
      if (a.removed) {
        const deadline = Date.now() + (a.removedTime || a.time || 1000);
        while (Date.now() < deadline && first(a.path || ["."], actx, root, true)) await sleep(100);
        continue;
      }
      let target = a.path ? null : { el: fallbackEl, path: ctx.inputPath };
      if (!target) target = await waitFirst(a.path, actx, root, a.hidden === true, a.time || 0);
      const picksOption = list(a.path).some((p) => /%(UPPER|LOWER)?(UNMAPPED)?VALUE%/.test(p) && /option|promptLeafNode|promptOption|listbox/i.test(p));
      if (!target && picksOption) {
        const shown = optionsOnScreen();
        const best = closest(shown, ctx.raw ?? actx.value);
        if (best) {
          note(`action ${i}: closest option "${best.label}" for "${actx.value}"`);
          target = { el: best.el, path: "closest" };
        } else if (shown.length) {
          seenOptions = [...new Set(shown.map(shownLabel).filter(Boolean))].slice(0, 60);
          note(`action ${i}: ${shown.length} options shown, none close to "${actx.value}": ${seenOptions.slice(0, 6).join(" / ")}`);
        }
      }
      if (!target) {
        if (a.allowFailure) continue;
        note(`action ${i} (${a.method || a.event || "path"}): nothing at its path for "${actx.value}"`);
        return false;
      }
      if (a.event) {
        const EventType = /^(keydown|keypress|keyup)$/.test(a.event) ? KeyboardEvent : Event;
        target.el.dispatchEvent(new EventType(a.event, events(a.eventOptions)));
        continue;
      }
      if (a.valuePath) {
        let picked = false;
        for (const cand of cands) {
          const opt = await waitFirst(a.valuePath, { ...ctx, inputPath: target.path, value: cand, raw: value }, root, true, a.valueElementTime || 300);
          if (opt) {
            picked = pickOption(opt.el);
            break;
          }
        }
        if (!picked && !a.allowFailure) {
          note(`action ${i}: no option for "${cands.join(" | ")}"`);
          return false;
        }
        continue;
      }
      await runMethod(a.method || "click", target.el, cands[0] ?? "", file, a.eventOptions);
    }
    return true;
  }

  return { runActions, resetSeenOptions: () => { seenOptions = null; }, getSeenOptions: () => seenOptions };
}
