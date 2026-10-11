export function createReportPanel({ on, surface, reader, readFields, getFields, queueReport, esc }) {
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
    try {
      await queueReport(values["jt-note"] || "");
      surface.region("jt-report", '<p class="muted">Report saved. It will send automatically.</p>');
    } catch (error) {
      surface.region("jt-report", `<p class="warn">Report failed: ${esc(String(error))}</p>`);
    }
  });

  return { reportForm };
}
