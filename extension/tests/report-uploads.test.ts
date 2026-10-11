import { test } from "node:test";
import assert from "node:assert/strict";

import { ReportUploadQueue } from "../background/report-uploads.js";
import { storePageReport } from "../report-queue.js";
import { createApplicationReportCapture } from "../runtime/report-capture.js";

test("a failed report chunk remains queued and retries with the same upload id", async () => {
  const values = new Map<string, unknown>();
  const storage = {
    async set(entries: Record<string, unknown>) { for (const [key, value] of Object.entries(entries)) values.set(key, value); },
    async get(key: string) { return { [key]: values.get(key) }; },
    async getKeys() { return [...values.keys()]; },
    async remove(key: string) { values.delete(key); },
  };
  const parts = new Map<number, Uint8Array>();
  const started: string[] = [];
  let failPart = true;
  const request = async (path: string, method: string, body?: unknown) => {
    if (method === "POST" && path === "user/apply/report-uploads") {
      started.push((body as { id: string }).id);
      return { ok: true, json: { report_id: null } };
    }
    if (method === "PUT") {
      if (failPart) { failPart = false; return { ok: false, status: 503 }; }
      const index = Number(path.split("/").at(-1));
      parts.set(index, body as Uint8Array);
    }
    return { ok: true, json: { id: 7 } };
  };
  const queue = new ReportUploadQueue(storage, request);
  const page = { title: "Apply", fields: [{ key: "name" }], html: "🧭" + "x".repeat(2_100_000) };
  await storePageReport(storage, { url: "https://x.test/apply", note: "large", page });
  await queue.pump();
  assert.equal(values.size, 1);
  await queue.pump();
  assert.equal(values.size, 0);
  assert.equal(started[0], started[1]);
  const bytes = new Uint8Array([...parts.keys()].sort((a, b) => a - b).flatMap((index) => [...parts.get(index)!]));
  assert.deepEqual(JSON.parse(new TextDecoder().decode(bytes)), page);
});

test("capture keeps full field and page markup", () => {
  const originalDocument = globalThis.document;
  const originalChrome = globalThis.chrome;
  const box = { outerHTML: "<div>" + "f".repeat(5_000) + "</div>", parentElement: null,
    querySelector: () => null, contains: () => true };
  const body = { outerHTML: "<body>" + "p".repeat(500_000) + "</body>" };
  Object.assign(globalThis, { document: { title: "Apply", body }, chrome: { runtime: { getManifest: () => ({ version: "1" }) } } });
  try {
    const report = createApplicationReportCapture({
      reader: { host: "x.test", current: () => "" }, boxOf: () => box,
      plain: () => ({ key: "name" }), traces: new Map(), send: async () => ({ ok: true }),
      VERSION: "1", BUILD: "1", getFields: () => [{}], getFill: () => null,
      getReviewState: () => null, getFilled: () => new Map(), getLastError: () => null,
      getPolicy: () => null, getRecipeRevision: () => "none",
    }).capture();
    assert.equal(report.fields[0].html, box.outerHTML);
    assert.equal(report.html, body.outerHTML);
  } finally {
    Object.assign(globalThis, { document: originalDocument, chrome: originalChrome });
  }
});
