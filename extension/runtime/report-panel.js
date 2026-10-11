export function createReportPanel({ on, surface, reader, readFields, getFields, api, capture, esc }) {
  const REPORT_BUTTON = `<button id="jt-report-btn">Report an issue</button>`;
  async function reportForm() {
    if (!getFields().length && reader.ready()) await readFields();
    surface.region("jt-report", `
      <label for="jt-note" class="setting-title">What went wrong?</label><p class="muted">Includes a capture of this page and its form values.</p><textarea id="jt-note" rows="3" placeholder="Add a note (optional)"></textarea>
      <button id="jt-send">Send report</button> <button id="jt-cancel">Cancel</button>`);
  }
  on("click", "jt-cancel", () => surface.region("jt-report", REPORT_BUTTON));
  // The note travels with the click, so the panel is never read across a frame.
  on("click", "jt-send", async (values) => {
    surface.mark("jt-send", { disabled: true });
    const res = await api("user/apply/reports", "POST", {
      url: location.href,
      note: values["jt-note"] || "",
      page: capture(),
    });
    surface.region("jt-report", res.ok
      ? `<p class="muted">Reported as #${res.json.id}. Thank you.</p>`
      : `<p class="warn">Report failed (${esc(res.status)}): ${esc(JSON.stringify(res.json || res.error))}</p>`);
  });

  return { reportForm };
}
