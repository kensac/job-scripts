export function createApplicationPreferences(api) {
  // The panel's preferences: minimised, the AI switch, the theme. In
  // chrome.storage.local, which every ATS host shares, so a panel minimised
  // on one board stays minimised on the next; localStorage was per origin
  // and forgot between hosts (job-scripts-5c, 2026-09-08). Read before the
  // first paint so the panel never flashes the default.
  const prefs = { collapsed: false, aiAll: false, theme: null, autoAdvance: false };
  const loadPrefs = async () => {
    try {
      const got = await new Promise((r) => chrome.storage.local.get(["collapsed", "aiAll", "theme", "autoAdvance"], r));
      if (got && typeof got.collapsed === "boolean") prefs.collapsed = got.collapsed;
      else prefs.collapsed = localStorage.getItem("jt-apply-collapsed") === "1";
      if (got && typeof got.aiAll === "boolean") prefs.aiAll = got.aiAll;
      else prefs.aiAll = localStorage.getItem("jt-apply-ai-all") === "1";
      prefs.theme = got && (got.theme === "light" || got.theme === "dark") ? got.theme : null;
      if (got && typeof got.autoAdvance === "boolean") prefs.autoAdvance = got.autoAdvance;
    } catch (_) {
      // Nothing stored or no storage: the defaults hold.
    }
  };
  // The person's copy: user_settings.prefs.apply through the settings
  // endpoints, so the same panel greets them in every browser they sign in
  // to (Kanishk, 2026-09-08). The account's value wins over the browser's
  // cache at start; a change goes to both. Merged on write so the other
  // prefs on the row (auto_draft) keep.
  const PREF_KEYS = ["collapsed", "aiAll", "theme", "autoAdvance"];
  const syncPrefsFromAccount = async () => {
    const res = await api("user/settings", "GET");
    const mine = res.ok && res.json && res.json.prefs && res.json.prefs.apply;
    if (!mine || typeof mine !== "object") return;
    for (const k of PREF_KEYS) if (k in mine) prefs[k] = mine[k];
    try {
      chrome.storage.local.set({ collapsed: prefs.collapsed, aiAll: prefs.aiAll, theme: prefs.theme, autoAdvance: prefs.autoAdvance });
    } catch (_) {
      // The cache is a convenience; the account has it.
    }
  };
  const savePrefsToAccount = async () => {
    const res = await api("user/settings", "GET");
    if (!res.ok || !res.json) return;
    const all = { ...(res.json.prefs || {}), apply: { collapsed: prefs.collapsed, aiAll: prefs.aiAll, theme: prefs.theme, autoAdvance: prefs.autoAdvance } };
    await api("user/settings", "PUT", { prefs: all });
  };
  const savePref = (key, value) => {
    try {
      chrome.storage.local.set({ [key]: value });
    } catch (_) {
      // Nothing to remember it in; it holds for this page.
    }
    savePrefsToAccount();
  };
  return { prefs, savePref, load: async () => { await loadPrefs(); await syncPrefsFromAccount(); } };
}
