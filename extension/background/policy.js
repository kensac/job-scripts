// The server's say over the bundled features, read from a fixed public
// route and validated to the byte before anything trusts it. Data only:
// four switches and a per-adapter kill switch. No selector, script, event
// or navigation ever arrives through the policy; those stay in the reviewed
// package. The recipe store below is the one deliberate exception, chosen
// for developer-mode and self-hosted installs (Kanishk, 2026-09-09): the
// table a config-driven reader runs from, published by the API and pinned
// per fill, with the bundled table as the fallback.
//
// One in-flight refresh per adapter, a cache in chrome.storage.local pinned
// to the fetch time, reuse only while the server's max_age holds, and a
// failure never turns a cached answer into defaults: with nothing valid to
// hand the caller gets unavailable and decides (the policy pauses, the
// recipe falls back to the bundled table).
const POLICY_SCHEMA = 1;
const POLICY_FEATURES = ["autofill", "ai_suggestions", "resume_upload", "auto_advance"];
const POLICY_REASONS = new Set([null, "FEATURE_DISABLED", "ADAPTER_DISABLED"]);
const ADAPTER_ID = /^[a-z0-9][a-z0-9_.-]{0,63}$/;
const HEX64 = /^[0-9a-f]{64}$/;

const sortedKeys = (o) => Object.keys(o).sort().join(",");

// A strict copy of a policy response, or null. Every key is checked, nothing
// unknown is carried, so a corrupt cache and a surprising server answer
// both read as "no configuration" rather than as permission.
function validatePolicy(raw, adapter) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  if (sortedKeys(raw) !== "adapter,features,max_age_seconds,revision,schema_version") return null;
  if (raw.schema_version !== POLICY_SCHEMA) return null;
  if (typeof raw.revision !== "string" || !HEX64.test(raw.revision)) return null;
  if (typeof raw.adapter !== "string" || !ADAPTER_ID.test(raw.adapter)) return null;
  if (adapter && raw.adapter !== adapter) return null;
  if (!Number.isInteger(raw.max_age_seconds) || raw.max_age_seconds < 0 || raw.max_age_seconds > 86400) return null;
  const f = raw.features;
  if (!f || typeof f !== "object" || Array.isArray(f)) return null;
  if (sortedKeys(f) !== [...POLICY_FEATURES].sort().join(",")) return null;
  const features = {};
  for (const name of POLICY_FEATURES) {
    const v = f[name];
    if (!v || typeof v !== "object" || Array.isArray(v)) return null;
    if (sortedKeys(v) !== "allowed,reason") return null;
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

async function chunkChainDigest(parts) {
  let previous = new Uint8Array(32);
  const domain = new TextEncoder().encode("recipe-chunk-v2\0");
  for (let index = 0; index < parts.length; index++) {
    const chunkDigest = new Uint8Array(await crypto.subtle.digest("SHA-256", parts[index]));
    const header = new Uint8Array(domain.length + 32 + 8 + 8 + 32);
    header.set(domain);
    header.set(previous, domain.length);
    const view = new DataView(header.buffer);
    view.setBigUint64(domain.length + 32, BigInt(index));
    view.setBigUint64(domain.length + 40, BigInt(parts[index].length));
    header.set(chunkDigest, domain.length + 48);
    previous = new Uint8Array(await crypto.subtle.digest("SHA-256", header));
  }
  return Array.from(previous, (b) => b.toString(16).padStart(2, "0")).join("");
}

const MANIFEST_KEYS = "adapter,chunk_count,digest_algorithm,encoding,max_age_seconds,revision,schema_version";
const CACHED_KEYS = "adapter,chunk_count,chunks,digest_algorithm,encoding,max_age_seconds,revision,schema_version";

// The manifest's identity, with or without the chunks a cached copy carries.
function validManifest(raw, adapter) {
  return !!raw && typeof raw === "object" && !Array.isArray(raw) &&
    raw.schema_version === 2 && raw.encoding === "identity-chunks" &&
    raw.digest_algorithm === "chunk-chain-sha256" &&
    Number.isSafeInteger(raw.chunk_count) && raw.chunk_count >= 1 &&
    typeof raw.revision === "string" && HEX64.test(raw.revision) &&
    raw.adapter === adapter && ADAPTER_ID.test(adapter) &&
    Number.isInteger(raw.max_age_seconds) && raw.max_age_seconds >= 0 && raw.max_age_seconds <= 86400;
}

// The recipe is validated by DECODING it: the chunks chain to the revision
// the server named, and the table inside is the engine's shape for this
// adapter. Anything else is no recipe, and the bundled table applies.
// Returns the decoded table beside the identity.
async function validateRecipe(raw) {
  if (sortedKeys(raw) !== CACHED_KEYS) return null;
  if (!Array.isArray(raw.chunks) || raw.chunks.length !== raw.chunk_count) return null;
  let parts;
  try {
    parts = raw.chunks.map((value) => Uint8Array.from(atob(value), (c) => c.charCodeAt(0)));
  } catch (_) { return null; }
  if ((await chunkChainDigest(parts)) !== raw.revision) return null;
  const bytes = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0));
  let offset = 0;
  for (const part of parts) { bytes.set(part, offset); offset += part.length; }
  let recipe;
  try { recipe = JSON.parse(new TextDecoder().decode(bytes)); } catch (_) { return null; }
  if (!recipe || typeof recipe !== "object" || Array.isArray(recipe)) return null;
  if (typeof recipe.name !== "string" || recipe.name.toLowerCase() !== raw.adapter) return null;
  if (!Array.isArray(recipe.matches) || !recipe.matches.length || !recipe.matches.every((m) => typeof m === "string" && m.startsWith("https://"))) return null;
  if (!Array.isArray(recipe.fields)) return null;
  return { schema_version: 2, revision: raw.revision, adapter: raw.adapter,
    max_age_seconds: raw.max_age_seconds, recipe };
}

export class PolicyStore {
  // storage: chrome.storage.local-shaped (get/set/remove with promises);
  // fetchFn: fetch; base: the public proxy URL; now: a clock, for tests.
  constructor(storage, fetchFn, base, now = () => Date.now()) {
    this.storage = storage;
    // Called free, never as this.fetch(...): fetch is a method of the global
    // scope, so any other receiver throws "Illegal invocation", which the
    // catch in refresh reported as NETWORK on every page (2026-09-10).
    this.fetch = (url, options) => fetchFn(url, options);
    this.base = base;
    this.now = now;
    this.inflight = new Map();
  }

  get prefix() {
    return "policy";
  }

  get schema() { return POLICY_SCHEMA; }

  // Sync or async; a subclass decodes.
  validate(raw, adapter) {
    return validatePolicy(raw, adapter);
  }

  key(adapter) {
    return `${this.prefix}:${this.schema}:${adapter}`;
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
    let raw = null;
    let reason = "UNAVAILABLE";
    try {
      const url = `${this.base}?schema_version=${this.schema}&adapter=${encodeURIComponent(adapter)}`;
      const res = await this.fetch(url, { credentials: "omit", cache: "no-store" });
      if (res.status === 200) {
        raw = await res.json();
        fresh = await this.validate(raw, adapter);
        if (!fresh) reason = "INVALID_RESPONSE";
      } else if (res.status === 404) reason = "NOT_PUBLISHED";
      else if (res.status === 409) reason = "UNSUPPORTED_SCHEMA";
      else if (res.status === 503) reason = "INVALID_POLICY";
      else reason = `HTTP_${res.status}`;
    } catch (_) {
      reason = "NETWORK";
    }
    if (fresh) {
      // max_age 0 permits this fill only: nothing is kept for offline reuse.
      // The response is cached as received and validated again on read.
      if (fresh.max_age_seconds > 0) await this.storage.set({ [key]: { fetchedAt: this.now(), config: raw } });
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
    const config = await this.validate(entry.config, adapter);
    if (!config || config.max_age_seconds <= 0) return null;
    const now = this.now();
    if (entry.fetchedAt > now) return null;
    if (now - entry.fetchedAt > config.max_age_seconds * 1000) return null;
    return config;
  }
}

export class RecipeStore extends PolicyStore {
  get prefix() {
    return "recipe";
  }

  get schema() { return 2; }

  async validate(raw, adapter) {
    if (!validManifest(raw, adapter)) return null;
    if (sortedKeys(raw) === MANIFEST_KEYS) {
      const chunks = [];
      for (let index = 0; index < raw.chunk_count; index++) {
        const url = `${this.base}?schema_version=2&adapter=${encodeURIComponent(adapter)}&revision=${encodeURIComponent(raw.revision)}&chunk_index=${index}`;
        const response = await this.fetch(url, { credentials: "omit", cache: "no-store" });
        if (response.status !== 200) return null;
        const part = await response.json();
        if (!part || sortedKeys(part) !== "adapter,index,payload,revision,schema_version" ||
            part.adapter !== adapter || part.revision !== raw.revision || part.index !== index ||
            part.schema_version !== 2 || typeof part.payload !== "string") return null;
        chunks.push(part.payload);
      }
      raw.chunks = chunks;
    }
    return validateRecipe(raw);
  }
}
