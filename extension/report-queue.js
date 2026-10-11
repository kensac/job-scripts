export const REPORT_UPLOAD_PREFIX = "report-upload:";

export async function storePageReport(storage, { url, note, page }) {
  const id = crypto.randomUUID();
  const record = {
    id, url, note, title: page.title ? String(page.title).slice(0, 2000) : null,
    field_count: Array.isArray(page.fields) ? page.fields.length : 0,
    page: JSON.stringify(page),
  };
  await storage.set({ [`${REPORT_UPLOAD_PREFIX}${id}`]: record });
  return { ok: true, queued: true, id };
}
