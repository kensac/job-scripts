const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const zlib = require('node:zlib');
const nodeCrypto = require('node:crypto');

// The worker's globals the decoder needs: base64, streams, digest.
const context = { URL, atob, Blob, Response, DecompressionStream, TextDecoder, crypto: globalThis.crypto };
const { PolicyStore: Store, RecipeStore } = vm.runInNewContext(
  fs.readFileSync(path.join(__dirname, '../../extension/policy.js'), 'utf8') + '; ({ PolicyStore, RecipeStore })',
  context,
);
const BASE = 'https://www.kanishksachdev.com/api/extension/config';
const RECIPES = 'https://www.kanishksachdev.com/api/extension/recipe';

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

// A recipe response the way the API builds it: canonical JSON, deflated,
// base64, with the sha256 of the canonical bytes as revision and digest.
function encodedRecipe(table, overrides = {}) {
  const raw = Buffer.from(JSON.stringify(table));
  const digest = nodeCrypto.createHash('sha256').update(raw).digest('hex');
  return {
    schema_version: 1,
    adapter: 'workday',
    revision: digest,
    max_age_seconds: 300,
    encoding: 'deflate+base64',
    payload: zlib.deflateSync(raw).toString('base64'),
    digest,
    ...overrides,
  };
}
const table = { name: 'Workday', matches: ['https://*.myworkdayjobs.com/*'], fields: [{ name: 'email', variants: [] }] };

test('a published recipe decodes to the table, checked against its digest, and is cached as received', async () => {
  const saved = storage();
  const got = await new RecipeStore(saved, responder(200, encodedRecipe(table)).fetchFn, RECIPES, () => 1000).resolve('workday');
  assert.equal(got.ok, true);
  assert.deepEqual(JSON.parse(JSON.stringify(got.config.recipe)), table);
  assert.equal(got.config.revision.length, 64);
  assert.equal(saved.data['recipe:1:workday'].config.encoding, 'deflate+base64');
  const offline = async () => { throw new Error('offline'); };
  const again = await new RecipeStore(saved, offline, RECIPES, () => 1000 + 10_000).resolve('workday');
  assert.equal(again.cached, true);
  assert.equal(again.config.recipe.name, 'Workday');
});

test('a recipe that does not decode, does not match its digest, or is not this adapter is no recipe', async () => {
  const wrongDigest = encodedRecipe(table);
  wrongDigest.revision = wrongDigest.digest = 'b'.repeat(64);
  const otherAdapter = encodedRecipe({ ...table, name: 'Lever' });
  const noMatches = encodedRecipe({ ...table, matches: [] });
  const garbage = encodedRecipe(table, { payload: Buffer.from('not deflate').toString('base64') });
  for (const bad of [wrongDigest, otherAdapter, noMatches, garbage, { ...encodedRecipe(table), extra: 1 }]) {
    const got = await new RecipeStore(storage(), responder(200, bad).fetchFn, RECIPES).resolve('workday');
    assert.equal(got.ok, false);
    assert.equal(got.reason, 'INVALID_RESPONSE');
  }
  const none = await new RecipeStore(storage(), responder(404, {}).fetchFn, RECIPES).resolve('workday');
  assert.equal(none.ok, false);
  assert.equal(none.reason, 'NOT_PUBLISHED');
});

test('concurrent refreshes for one adapter share a single request', async () => {
  const { fetchFn, calls } = responder(200, config());
  const store = new Store(storage(), fetchFn, BASE);
  const [a, b] = await Promise.all([store.resolve('greenhouse'), store.resolve('greenhouse')]);
  assert.equal(calls.length, 1);
  assert.equal(a.config.revision, b.config.revision);
  assert.equal((await store.resolve('not an adapter')).reason, 'BAD_ADAPTER');
});

// The worker passes the global fetch in. A method call on the store is a
// different receiver, which real fetch answers with "Illegal invocation" and
// refresh's catch turned into NETWORK on every page (2026-09-10).
test('fetch is called free of the store, the way the global scope demands', async () => {
  const strict = function (url, init) {
    if (this !== undefined && this !== globalThis) {
      throw new TypeError("Failed to execute 'fetch' on 'Window': Illegal invocation");
    }
    return responder(200, config({ adapter: 'greenhouse' })).fetchFn(url, init);
  };
  const got = await new Store(storage(), strict, BASE).resolve('greenhouse');
  assert.equal(got.ok, true);
  assert.equal(got.reason, undefined);
});
