export function createApplicationReview({ state, render, on, api, surface, operation, fieldByKey, reader, boxOf, allowed, askable, entryByKey, execute, taskApi, put, esc, trackFilledJob, run }) {
  function show() {
    const unconfirmed = state.fill.fields.filter(entry => state.filled.get(entry.key)?.error);
    render(`
      ${state.fill.board_saved ? `<p class="muted">Saved to your board. Existing status and application date preserved.</p>` : ""}
      ${state.fill.board_error ? `<p class="notice warn" role="alert">Your form was filled, but saving this job to your board failed.</p><button id="jt-track-retry">Retry saving to board</button>` : ""}
      ${state.fill.ai_error ? `<p class="notice warn" role="alert">Could not prepare AI answers: ${esc(state.fill.ai_error)}.</p>` : ""}
      ${state.fill.stopped ? `<p class="notice warn" role="alert">${esc(state.fill.stopped)}</p>` : ""}
      ${unconfirmed.length ? `<div class="notice warn" role="alert"><p>Some fields could not be confirmed. Review these before submitting:</p><ul>${unconfirmed.map(entry => `<li>${esc(entry.label || entry.key)}: ${esc(state.filled.get(entry.key).error)}</li>`).join("")}</ul></div>` : ""}
      <div id="jt-answer-review"></div>
      <button id="jt-again">Fill remaining blanks</button>
      <details class="help"><summary>What gets remembered?</summary><p>Drafts and suggestions are saved with this application. After submission, choices and short answers can be reused for the same question. The final submission record keeps the values actually on the form.</p></details>
    `);
    on("click", "jt-again", run);
    on("click", "jt-track-retry", async () => { await trackFilledJob(); show(); });
    drawReview();
    if (!state.reviewState) loadReview();
  }

  async function loadReview() {
    const fillId = state.fill?.fill_id;
    if (!fillId) return;
    const refreshing = state.reviewState?.id === fillId && state.reviewState.state === "ready";
    // Refresh existing cards in place. Removing them collapses the scroll
    // container and discards open editors and unsaved edits in other fields.
    if (!refreshing) state.reviewState = { id: fillId, state: "loading", fields: [] };
    drawReview();
    const result = await api(`user/apply/fills/${fillId}`, "GET");
    if (state.fill?.fill_id !== fillId) return;
    if (result.ok) state.reviewState = { ...result.json, state: "ready" };
    else if (!refreshing) state.reviewState = { id: fillId, state: "error", fields: [] };
    if (!result.ok) state.reviewMessage = "Could not load saved answers. Check your connection and sign-in, then retry. Your form has not changed.";
    drawReview();
  }

  function drawReview() {
    if (!state.fill || !state.panelUp) return;
    const names = { profile: "Profile", bank: "Saved answer", draft: "Job draft", ai: "Generated", manual: "Your edit" };
    surface.review({
      fillId: state.fill.fill_id, state: state.reviewState?.state || "loading", message: state.reviewMessage,
      busy: operation.busy || !!state.reviewState?.submitted_at,
      fields: state.fill.fields.map(entry => {
        const saved = state.reviewState?.fields.find(f => f.key === entry.key) || entry;
        const field = fieldByKey(entry.key);
        const current = String((field && reader.current(field)) || "");
        const node = field && (field._el || field._ctl || field._box || boxOf(field));
        return { key: entry.key, label: entry.label || entry.key, kind: entry.kind, current,
          required: field?.required === true, present: !!node?.isConnected,
          saveUnconfirmed: !!state.filled.get(entry.key)?.error,
          value: saved.review_value ?? current ?? entry.value ?? "", feedback: saved.feedback || "",
          source: state.filled.get(entry.key)?.error ? "Save unconfirmed" : state.touched.has(entry.key) || state.filled.get(entry.key)?.kept ? "Kept on form" : names[entry.rung] || "Filled",
          editable: !field?._person && ["text", "long", "number", "select", "yesno", "multiselect"].includes(entry.kind),
          canGenerate: allowed("ai_suggestions") && !saved.never_ai && !field?._person && (entry.kind === "long" || askable(entry)), history: saved.answer_history || [] };
      }),
    });
  }

  async function reviewAction({ action, key, value, feedback }) {
    if (action === "reload") return loadReview();
    if (!state.fill || operation.busy) return;
    const entry = entryByKey(key);
    const field = fieldByKey(key);
    if (!entry || !field) return;
    if (action === "locate") {
      const node = field._el || field._ctl || field._box || boxOf(field);
      if (node) {
        node.scrollIntoView?.({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
        const selector = 'input:not([type="hidden"]):not([type="file"]):not(:disabled), textarea:not(:disabled), select:not(:disabled), button:not(:disabled), [contenteditable="true"]';
        const target = node.matches(selector) ? node : [...node.querySelectorAll(selector)].find(el => el.getClientRects().length);
        target?.focus({ preventScroll: true });
      }
      return;
    }
    if (state.reviewState?.state !== "ready") return;
    if (field._person || !["text", "long", "number", "select", "yesno", "multiselect"].includes(entry.kind)) return;
    const before = reader.current(field);
    const originalFill = state.fill.fill_id;
    return execute(async () => {
      const saved = state.reviewState.fields.find(f => f.key === key);
      if (!saved) throw new Error("The field is no longer part of this application.");
      const answer = action === "refill" ? (saved.review_value ?? saved.value ?? "") : String(value ?? "");
      const response = await taskApi(`user/apply/fills/${originalFill}/answer`, "PUT", {
        key, revision: saved.answer_revision || 0, value: answer, feedback: String(feedback ?? ""),
      });
      if (!response.ok) {
        state.reviewMessage = response.status === 409 ? "This answer changed elsewhere. Reload saved answers before trying again." : "Could not save your draft. The form has not changed.";
        drawReview();
        return;
      }
      state.reviewState = { ...response.json, state: "ready" };
      if (action === "generate") {
        if (!allowed("ai_suggestions") || saved.never_ai || !(entry.kind === "long" || askable(entry))) return;
        const generated = await taskApi("user/apply/suggest", "POST", {
          fill_id: originalFill, job_id: state.fill.job_id, review_only: true,
          revision: state.reviewState.fields.find(f => f.key === key).answer_revision,
          fields: [{ key, label: entry.label || key, kind: entry.kind, options: entry.options || [] }],
        });
        state.reviewMessage = generated.ok ? (generated.json.answers?.[key] ? "New draft ready. Review it, then choose Apply this answer." : "No draft was generated for this field. Your existing answer is unchanged.") : "Could not generate a new draft. Your saved suggestions and form answer are unchanged.";
        await loadReview();
      } else if (action === "apply" || action === "refill") {
        if (state.fill.fill_id !== originalFill || reader.current(field) !== before) {
          state.reviewMessage = "The form changed while saving. Your newer answer was kept. Review and apply again if needed.";
        } else {
          const ok = await put(entry, answer, null, true);
          if (ok) state.touched.add(key);
          state.reviewMessage = ok ? "Answer applied. Check the employer's form before submitting." : "The form did not accept this answer. Choose its value directly on the page.";
        }
      } else state.reviewMessage = "Draft and suggestions saved. The form was not changed.";
      drawReview();
    });
  }

  return { show, drawReview, reviewAction };
}
