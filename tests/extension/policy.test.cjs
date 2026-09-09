const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const Store = vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../extension/policy.js'), 'utf8') + '; PolicyStore', { URL });
const BASE = 'https://www.kanishksachdev.com/api/extension/config';

function storage() {
  const data = {};
  return {
    data,
    get: async key => ({ [key]: data[key] === undefined ? undefined : structuredClone(data[key]) }),
    set: async values => Object.assign(data, structuredClone(values)),
    remove: async key => { delete data[key]; },
  };
}
const revision = 'a'.repeat(64);
function config(overrides = {}) {
  const on = { allowed: true, reason: null };
  return {
    schema_version: 1,
    revision,
    adapter: 'greenhouse',
    max_age_seconds: 300,
    features: { autofill: on, ai_suggestions: on, resume_upload: on, auto_advance: on },
    ...overrides,
  };
}
function responder(status, body) {
  const calls = [];
  const fetchFn = async (url, init) => {
    calls.push({ url, init });
    return { status, json: async () => (typeof body === 'function' ? body() : body) };
  };
  return { fetchFn, calls };
}

test('a fresh response is validated, cached and served without credentials', async () => {
  const saved = storage();
  const { fetchFn, calls } = responder(200, config());
  const got = await new Store(saved, fetchFn, BASE, () => 1000).resolve('greenhouse');
  assert.equal(got.ok, true);
  assert.equal(got.cached, false);
  assert.equal(got.config.features.autofill.allowed, true);
  assert.equal(calls[0].init.credentials, 'omit');
  assert.match(calls[0].url, /schema_version=1&adapter=greenhouse$/);
  assert.equal(saved.data['policy:1:greenhouse'].fetchedAt, 1000);
});

test('a cached disablement survives an outage and never turns back into enabled defaults', async () => {
  const saved = storage();
  const off = { allowed: false, reason: 'ADAPTER_DISABLED' };
  const disabled = config({ features: { autofill: off, ai_suggestions: off, resume_upload: off, auto_advance: off } });
  await new Store(saved, responder(200, disabled).fetchFn, BASE, () => 1000).resolve('greenhouse');
  const later = await new Store(saved, async () => { throw new Error('offline'); }, BASE, () => 1000 + 60_000).resolve('greenhouse');
  assert.equal(later.ok, true);
  assert.equal(later.cached, true);
  assert.equal(later.reason, 'NETWORK');
  assert.equal(later.config.features.autofill.allowed, false);
  assert.equal(later.config.features.autofill.reason, 'ADAPTER_DISABLED');
});

test('no valid response and no unexpired cache is unavailable, not permission', async () => {
  for (const [status, reason] of [[409, 'UNSUPPORTED_SCHEMA'], [503, 'INVALID_POLICY'], [500, 'HTTP_500']]) {
    const got = await new Store(storage(), responder(status, {}).fetchFn, BASE).resolve('greenhouse');
    assert.equal(got.ok, false); assert.equal(got.unavailable, true); assert.equal(got.reason, reason);
  }
  const invalid = await new Store(storage(), responder(200, { ...config(), extra: 1 }).fetchFn, BASE).resolve('greenhouse');
  assert.equal(invalid.ok, false); assert.equal(invalid.reason, 'INVALID_RESPONSE');
  const wrongAdapter = await new Store(storage(), responder(200, config({ adapter: 'lever' })).fetchFn, BASE).resolve('greenhouse');
  assert.equal(wrongAdapter.ok, false);
  const contradictory = config();
  contradictory.features.autofill = { allowed: false, reason: null };
  assert.equal((await new Store(storage(), responder(200, contradictory).fetchFn, BASE).resolve('greenhouse')).ok, false);
});

test('the cache expires on the server clock, and a future or corrupt stamp does not extend it', async () => {
  const saved = storage();
  await new Store(saved, responder(200, config({ max_age_seconds: 60 })).fetchFn, BASE, () => 1000).resolve('greenhouse');
  const offline = async () => { throw new Error('offline'); };
  assert.equal((await new Store(saved, offline, BASE, () => 1000 + 59_000).resolve('greenhouse')).ok, true);
  assert.equal((await new Store(saved, offline, BASE, () => 1000 + 61_000).resolve('greenhouse')).ok, false);
  saved.data['policy:1:greenhouse'].fetchedAt = 5_000_000;
  assert.equal((await new Store(saved, offline, BASE, () => 1000 + 30_000).resolve('greenhouse')).ok, false);
  saved.data['policy:1:greenhouse'] = { fetchedAt: 1000, config: { ...config(), features: null } };
  assert.equal((await new Store(saved, offline, BASE, () => 1000 + 30_000).resolve('greenhouse')).ok, false);
});

test('max_age 0 permits the fresh operation only, with nothing kept for reuse', async () => {
  const saved = storage();
  const got = await new Store(saved, responder(200, config({ max_age_seconds: 0 })).fetchFn, BASE, () => 1000).resolve('greenhouse');
  assert.equal(got.ok, true);
  assert.equal(saved.data['policy:1:greenhouse'], undefined);
  assert.equal((await new Store(saved, async () => { throw new Error('offline'); }, BASE, () => 1001).resolve('greenhouse')).ok, false);
});

test('concurrent refreshes for one adapter share a single request', async () => {
  const { fetchFn, calls } = responder(200, config());
  const store = new Store(storage(), fetchFn, BASE);
  const [a, b] = await Promise.all([store.resolve('greenhouse'), store.resolve('greenhouse')]);
  assert.equal(calls.length, 1);
  assert.equal(a.config.revision, b.config.revision);
  assert.equal((await store.resolve('not an adapter')).reason, 'BAD_ADAPTER');
});
