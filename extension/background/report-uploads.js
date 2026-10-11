import { REPORT_UPLOAD_PREFIX } from "../report-queue.js";

const CHUNK_BYTES = 64 * 1024;

export class ReportUploadQueue {
  constructor(storage, request) {
    this.storage = storage;
    this.request = request;
    this.running = null;
  }

  async pump() {
    if (this.running) return this.running;
    this.running = this._pump()
      .catch((error) => { console.warn("Page report queue will retry", error); })
      .finally(() => { this.running = null; });
    return this.running;
  }

  async _pump() {
    const keys = (await this.storage.getKeys()).filter((key) => key.startsWith(REPORT_UPLOAD_PREFIX));
    for (const key of keys) {
      const record = (await this.storage.get(key))[key];
      if (!record) continue;
      try {
        await this.upload(record);
        await this.storage.remove(key);
      } catch (error) {
        console.warn("Page report upload will retry", error);
      }
    }
  }

  async upload(record) {
    const bytes = new TextEncoder().encode(record.page);
    const chunkCount = Math.max(1, Math.ceil(bytes.length / CHUNK_BYTES));
    const path = `user/apply/report-uploads/${record.id}`;
    const started = await this.request("user/apply/report-uploads", "POST", {
      id: record.id, url: record.url, note: record.note,
      title: record.title, field_count: record.field_count,
      chunk_count: chunkCount,
    });
    if (!started.ok) throw new Error(`report start failed (${started.status})`);
    if (typeof started.json?.report_id === "number") return;
    if (started.json?.report_id !== null) throw new Error("report start receipt is missing");
    for (let index = 0; index < chunkCount; index += 1) {
      const part = bytes.subarray(index * CHUNK_BYTES, (index + 1) * CHUNK_BYTES);
      const received = await this.request(`${path}/chunks/${index}`, "PUT", part);
      if (!received.ok) throw new Error(`report chunk ${index} failed (${received.status})`);
    }
    const completed = await this.request(`${path}/complete`, "POST");
    if (!completed.ok || typeof completed.json?.id !== "number") {
      throw new Error(`report completion failed (${completed.status})`);
    }
  }
}
