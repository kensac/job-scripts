// A per-tab, per-frame receipt survives a full-page redirect and a worker
// restart. Session storage keeps application answers out of disk storage.
class SubmissionStore {
  constructor(storage, request) {
    this.storage = storage;
    this.request = request;
    this.queues = new Map();
  }

  handle(message, sender) {
    if (!sender.tab || !Number.isInteger(sender.tab.id)) return Promise.resolve({ ok: false, error: "No application tab" });
    const key = `submission:${sender.tab.id}:${sender.frameId || 0}`;
    const work = (this.queues.get(key) || Promise.resolve()).catch(() => {}).then(() => this.run(key, message, sender));
    this.queues.set(key, work);
    work.finally(() => { if (this.queues.get(key) === work) this.queues.delete(key); }).catch(() => {});
    return work;
  }

  async run(key, message, sender) {
    const origin = new URL(sender.url).origin;
    let state = (await this.storage.get(key))[key];
    if (state && new URL(state.url).origin !== origin) state = null;
    const save = async () => this.storage.set({ [key]: state });
    if (message.action === "get") return { ok: true, state };
    if (message.action === "clear") {
      if (state && state.fillId === message.fillId) await this.storage.remove(key);
      return { ok: true };
    }
    if (message.action === "arm") {
      if (new URL(message.url).origin !== origin) return { ok: false, error: "Application origin changed" };
      if (state && message.fillId && state.fillId === message.fillId && state.status === "recorded") return { ok: true, state };
      state = { url: message.url, title: message.title, fillId: message.fillId, fields: message.fields || [], status: "watching", startedAt: Date.now() };
      await save();
      return { ok: true, state };
    }
    if (!state || state.fillId !== message.fillId) return { ok: false, error: "No matching submission attempt" };
    if (state.status === "recorded") return { ok: true, state };
    if (message.action !== "confirm" && message.action !== "retry") return { ok: false, error: "Unknown submission action" };
    if (message.action === "retry" && state.status !== "confirmed") return { ok: false, error: "Submission is not confirmed" };
    state.status = "confirmed";
    await save();
    let response;
    try {
      if (!state.fillId) {
        const resolved = await this.request({ path: "user/apply/resolve", method: "POST", body: { url: state.url, fields: [] } });
        if (!resolved.ok) {
          state.result = resolved;
          await save();
          return { ok: true, state };
        }
        state.fillId = resolved.json.fill_id;
        await save();
      }
      response = await this.request({ path: `user/apply/fills/${state.fillId}/submitted`, method: "POST", body: { fields: state.fields } });
    } catch (error) {
      response = { ok: false, error: String(error) };
    }
    state.result = response;
    if (response.ok) {
      state.status = "recorded";
      delete state.fields;
    }
    await save();
    return { ok: true, state };
  }
}
