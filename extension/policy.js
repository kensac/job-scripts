// The server's say over the bundled features, read from a fixed public
// route and validated to the byte before anything trusts it. Data only:
// four switches and a per-adapter kill switch. No selector, script, event
// or navigation ever arrives this way; those stay in the reviewed package,
// and a new operation is a store release (docs/agents/frontend.md, #494).
//
// One in-flight refresh per adapter, a cache in chrome.storage.local pinned
// to the fetch time, reuse only while the server's max_age holds, and a
// failure never turns a cached disablement back into enabled defaults: with
// nothing valid to hand the caller gets unavailable and pauses.
const POLICY_SCHEMA = 1;
const POLICY_FEATURES = ["autofill", "ai_suggestions", "resume_upload", "auto_advance"];
const POLICY_REASONS = new Set([null, "FEATURE_DISABLED", "ADAPTER_DISABLED"]);
const ADAPTER_ID = /^[a-z0-9][a-z0-9_.-]{0,63}$/;

// A strict copy of a response, or null. Every key is checked, nothing
// unknown is carried, so a corrupt cache and a surprising server answer
// both read as "no configuration" rather than as permission.
function validatePolicy(raw, adapter) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const keys = Object.keys(raw).sort().join(",");
  if (keys !== "adapter,features,max_age_seconds,revision,schema_version") return null;
  if (raw.schema_version !== POLICY_SCHEMA) return null;
  if (typeof raw.revision !== "string" || !/^[0-9a-f]{64}$/.test(raw.revision)) return null;
  if (typeof raw.adapter !== "string" || !ADAPTER_ID.test(raw.adapter)) return null;
  if (adapter && raw.adapter !== adapter) return null;
  if (!Number.isInteger(raw.max_age_seconds) || raw.max_age_seconds < 0 || raw.max_age_seconds > 86400) return null;
  const f = raw.features;
  if (!f || typeof f !== "object" || Array.isArray(f)) return null;
  if (Object.keys(f).sort().join(",") !== [...POLICY_FEATURES].sort().join(",")) return null;
  const features = {};
  for (const name of POLICY_FEATURES) {
    const v = f[name];
    if (!v || typeof v !== "object" || Array.isArray(v)) return null;
    if (Object.keys(v).sort().join(",") !== "allowed,reason") return null;
    if (typeof v.allowed !== "boolean" || !POLICY_REASONS.has(v.reason)) return null;
    if (v.allowed && v.reason !== null) return null;
    if (!v.allowed && v.reason === null) return null;
    features[name] = { allowed: v.allowed, reason: v.reason };
  }
  return {
    schema_version: POLICY_SCHEMA,
    revision: raw.revision,
    adapter: raw.adapter,
    max_age_seconds: raw.max_age_seconds,
    features,
  };
}

class PolicyStore {
  // storage: chrome.storage.local-shaped (get/set/remove with promises);
  // fetchFn: fetch; base: the public proxy URL; now: a clock, for tests.
  constructor(storage, fetchFn, base, now = () => Date.now()) {
    this.storage = storage;
    this.fetch = fetchFn;
    this.base = base;
    this.now = now;
    this.inflight = new Map();
  }

  key(adapter) {
    return `policy:${POLICY_SCHEMA}:${adapter}`;
  }

  // {ok: true, config, cached} or {ok: false, unavailable: true, reason}.
  resolve(adapter) {
    if (typeof adapter !== "string" || !ADAPTER_ID.test(adapter)) {
      return Promise.resolve({ ok: false, unavailable: true, reason: "BAD_ADAPTER" });
    }
    const key = this.key(adapter);
    let work = this.inflight.get(key);
    if (!work) {
      work = this.refresh(adapter, key).finally(() => {
        if (this.inflight.get(key) === work) this.inflight.delete(key);
      });
      this.inflight.set(key, work);
    }
    return work;
  }

  async refresh(adapter, key) {
    const cached = await this.cached(key, adapter);
    let fresh = null;
    let reason = "UNAVAILABLE";
    try {
      const url = `${this.base}?schema_version=${POLICY_SCHEMA}&adapter=${encodeURIComponent(adapter)}`;
      const res = await this.fetch(url, { credentials: "omit", cache: "no-store" });
      if (res.status === 200) {
        fresh = validatePolicy(await res.json(), adapter);
        if (!fresh) reason = "INVALID_RESPONSE";
      } else if (res.status === 409) reason = "UNSUPPORTED_SCHEMA";
      else if (res.status === 503) reason = "INVALID_POLICY";
      else reason = `HTTP_${res.status}`;
    } catch (_) {
      reason = "NETWORK";
    }
    if (fresh) {
      // max_age 0 permits this fill only: nothing is kept for offline reuse.
      if (fresh.max_age_seconds > 0) await this.storage.set({ [key]: { fetchedAt: this.now(), config: fresh } });
      else await this.storage.remove(key);
      return { ok: true, config: fresh, cached: false };
    }
    if (cached) return { ok: true, config: cached, cached: true, reason };
    return { ok: false, unavailable: true, reason };
  }

  // The stored response while it is still inside the server's max_age. A
  // stamp in the future, a moved clock or a corrupt entry cannot extend it.
  async cached(key, adapter) {
    const got = await this.storage.get(key);
    const entry = got && got[key];
    if (!entry || typeof entry !== "object" || typeof entry.fetchedAt !== "number") return null;
    const config = validatePolicy(entry.config, adapter);
    if (!config || config.max_age_seconds <= 0) return null;
    const now = this.now();
    if (entry.fetchedAt > now) return null;
    if (now - entry.fetchedAt > config.max_age_seconds * 1000) return null;
    return config;
  }
}
