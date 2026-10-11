export function createApplicationContext(api, lifecycle, esc, onLoaded) {
  // What this person already has for this page. Read once when the panel
  // appears and kept for the page: it opens no fill and writes nothing, so
  // asking again on every repaint would be a request that changes no answer.
  let context = null;
  let contextState = "idle";
  let contextGeneration = 0;
  function previousApplicationNotice() {
    const job = context?.job;
    if (!job || !(job.status || job.date_applied || job.submitted_at)) return "";
    const date = job.date_applied || job.submitted_at?.slice(0, 10);
    return `<div class="notice warn" role="status" aria-label="Existing application"><strong>${job.submitted_at ? "You already submitted this application." : "Existing application record"}</strong><p>${esc(job.status || "Submission recorded")}${date ? ` · ${esc(date)}` : ""}</p><p>Filling again will keep your existing status and application date.</p></div>`;
  }
  async function loadContext() {
    if (contextState !== "idle") return;
    contextState = "loading";
    const url = location.href;
    const generation = contextGeneration;
    const res = await api(`user/apply/context?url=${encodeURIComponent(url)}`);
    if (url !== location.href || generation !== contextGeneration || lifecycle.isInvalid) return;
    context = res.ok ? res.json : null;
    contextState = res.ok ? "ready" : "error";
    // Only if the offer is still what the person is looking at. They can
    // click Autofill while this is in flight, and a repaint then would throw
    // away the flow that click started.
    onLoaded();
  }

  // A fact the person set, in the words the form uses. A list fact (their
  // education, their jobs) is a count: the rows are long and the question
  // here is whether there is anything to fill from, not what it says.
  const factRow = (key, value) => {
    const said = Array.isArray(value) ? `${value.length} ${value.length === 1 ? "entry" : "entries"}` : String(value);
    return `<li><div class="field-copy"><span class="field-label">${esc(humanKey(key))}</span><span class="field-value">${esc(said)}</span></div></li>`;
  };
  // Underscores to words, except the names that own their own capitals.
  const KEY_LABEL = { linkedin: "LinkedIn", github: "GitHub", url: "Website", address_2: "Address line 2", address_3: "Address line 3" };
  const humanKey = (key) => KEY_LABEL[key] || key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

  // Each source says how much of it there is, and opens on what it holds.
  // The counts are the state; nothing here sends the person to the website
  // to find out what the extension is about to use.
  function sources() {
    if (contextState === "loading") return `<p class="muted">Reading your profile, answers and drafts…</p>`;
    if (contextState === "error" || !context) {
      return `<p class="muted">Could not read your profile, answers and drafts. Autofill still works; it reads them again as it fills.</p>`;
    }
    const facts = Object.entries(context.profile || {});
    const answers = context.answers || [];
    const drafts = context.drafts || [];
    const section = (id, title, count, empty, body) => `
      <details class="field-section" id="${id}"><summary>${title} <span class="count">${count}</span></summary>
      ${count ? `<ul>${body}</ul>` : `<p class="muted">${empty}</p>`}</details>`;
    return [
      section("jt-src-profile", "Profile details", facts.length, "Nothing set yet.",
        facts.map(([key, value]) => factRow(key, value)).join("")),
      section("jt-src-answers", "Saved answers", answers.length, "Nothing kept from a submitted form yet.",
        answers.map((a) => `<li><div class="field-copy"><span class="field-label">${esc(a.label)}</span><span class="field-value">${esc(a.value)}</span></div><span class="tag">${esc(a.times_used)} used</span></li>`).join("")),
      section("jt-src-drafts", "Drafts", drafts.length,
        context.job ? "No drafts written for this posting yet." : "This page is not a posting on your board, so there are no job drafts.",
        drafts.map((d) => `<li><div class="field-copy"><span class="field-label">${esc(d.question)}</span><span class="field-value">${esc(d.draft)}</span></div></li>`).join("")),
    ].join("");
  }

  // The posting this page is, when the board knows it. The panel's own
  // heading says it, so nothing below has to repeat it.
  const matchedTitle = () =>
    context && context.job ? [context.job.title, context.job.company].filter(Boolean).join(" · ") : "";

  return { get current() { return context; }, get state() { return contextState; }, reset() { context = null; contextState = "idle"; contextGeneration += 1; }, markIdle() { contextState = "idle"; }, load: loadContext, previousApplicationNotice, sources, matchedTitle };
}
