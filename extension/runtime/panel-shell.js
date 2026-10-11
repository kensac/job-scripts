export function createPanelShell({ prefs, surface, operation, esc, getPanelUp, getSubmission, getApplicationContext }) {
  const THEME_NEXT = { light: "dark", dark: null, null: "light" };
  const themeLabel = (t) => (t === "light" ? "Light" : t === "dark" ? "Dark" : "System");
  const render = (html) => {
    if (!getPanelUp()) return;
    const themeTitle = `Theme: ${themeLabel(prefs.theme)}. Switch to ${themeLabel(THEME_NEXT[String(prefs.theme)])}`;
    surface.paint(`
      <div class="head">
        <div class="brand"><span class="brand-mark" aria-hidden="true">↗</span><div><h3>Job Tracker</h3><span class="eyebrow">Apply assistant</span></div></div>
        <div class="head-actions"><button id="jt-theme" aria-label="${esc(themeTitle)}" title="${esc(themeTitle)}">${prefs.theme === "light" ? "☀" : prefs.theme === "dark" ? "☾" : "◐"}</button>
        <button id="jt-min" aria-label="${prefs.collapsed ? "Expand panel" : "Minimise panel"}" aria-expanded="${!prefs.collapsed}" title="${prefs.collapsed ? "Expand" : "Minimise"}">${prefs.collapsed ? "+" : "−"}</button></div>
      </div>
      <div class="body">
        <div class="application-context"><span class="eyebrow">Current application</span><div class="application-title">${esc(getApplicationContext().matchedTitle() || getSubmission()?.title || document.title || "Application form")}</div><span class="muted">${esc(location.hostname)}${getApplicationContext().current ? (getApplicationContext().current.job ? " · on your board" : " · not on your board") : ""}</span></div>
        ${getApplicationContext().previousApplicationNotice()}
        <div id="jt-operation" role="status">${operationControls()}</div>
        <div class="workspace">${html}</div>
        <details class="settings"><summary>Autofill preferences</summary>
          <label class="switch"><span><span class="setting-title">Draft unanswered text</span><span class="setting-description">Use AI for free-text questions too. Review before submitting.</span></span><input type="checkbox" role="switch" id="jt-ai-all" ${prefs.aiAll ? "checked" : ""}></label>
          <label class="switch"><span><span class="setting-title">Continue between pages</span><span class="setting-description">Advance after filling. Always stop before Submit.</span></span><input type="checkbox" role="switch" id="jt-advance" ${prefs.autoAdvance ? "checked" : ""}></label>
        </details>
        <div class="panel-footer"><div id="jt-report"><button id="jt-report-btn">Report an issue</button></div></div>
      </div>`, { theme: prefs.theme, collapsed: prefs.collapsed });
  };

  function operationControls() {
    if (!operation.busy) return "";
    if (operation.state === "stopped") return `<p>Stopping autofill…</p>`;
    const paused = operation.state === "paused";
    return `<div class="operation-controls"><span>${paused ? "Autofill paused" : "Autofill is running"}</span><button id="jt-pause" aria-label="${paused ? "Resume autofill" : "Pause autofill"}">${paused ? "Resume" : "Pause"}</button><button id="jt-stop" aria-label="Stop autofill">Stop</button></div>`;
  }
  return { THEME_NEXT, themeLabel, render, operationControls };
}
