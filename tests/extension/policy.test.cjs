const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const nodeCrypto = require('node:crypto');

// The worker's globals the decoder needs: base64 and digest.
const context = { URL, atob, TextDecoder, TextEncoder, crypto: globalThis.crypto };
const { PolicyStore: Store, RecipeStore } = vm.runInNewContext(
  fs.readFileSync(path.join(__dirname, '../../extension/background/policy.js'), 'utf8').replace(/^export /gm, '') + '; ({ PolicyStore, RecipeStore })',
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

// A published recipe the way the API serves it: a manifest naming the chunk
// chain digest, then each chunk by revision and index.
function chainDigest(pieces) {
  let digest = Buffer.alloc(32);
  for (const [index, piece] of pieces.entries()) {
    const indexBytes = Buffer.alloc(8);
    indexBytes.writeBigUInt64BE(BigInt(index));
    const lengthBytes = Buffer.alloc(8);
    lengthBytes.writeBigUInt64BE(BigInt(piece.length));
    digest = nodeCrypto.createHash('sha256').update(Buffer.concat([
      Buffer.from('recipe-chunk-v2\0'), digest, indexBytes, lengthBytes,
      nodeCrypto.createHash('sha256').update(piece).digest(),
    ])).digest();
  }
  return digest.toString('hex');
}
function published(pieces, overrides = {}) {
  const seen = [];
  const manifest = {
    schema_version: 2, adapter: 'workday', revision: chainDigest(pieces), max_age_seconds: 300,
    encoding: 'identity-chunks', digest_algorithm: 'chunk-chain-sha256', chunk_count: pieces.length,
    ...overrides,
  };
  const fetchFn = async (url) => {
    seen.push(url);
    const index = new URL(url).searchParams.get('chunk_index');
    if (index === null) return { status: 200, json: async () => manifest };
    return { status: 200, json: async () => ({ schema_version: 2, adapter: 'workday',
      revision: manifest.revision, index: Number(index), payload: pieces[Number(index)].toString('base64') }) };
  };
  return { fetchFn, seen };
}
const table = { name: 'Workday', matches: ['https://*.myworkdayjobs.com/*'], fields: [{ name: 'email', variants: [] }] };
const tableBytes = (t = table) => Buffer.from(JSON.stringify(t));

test('a chunked recipe verifies every byte before it is cached', async () => {
  const raw = tableBytes();
  const { fetchFn, seen } = published([raw.subarray(0, 17), raw.subarray(17)]);
  const saved = storage();
  const got = await new RecipeStore(saved, fetchFn, RECIPES, () => 1000).resolve('workday');
  assert.equal(got.ok, true);
  assert.deepEqual(JSON.parse(JSON.stringify(got.config.recipe)), table);
  assert.equal(seen.length, 3);
  assert.equal(saved.data['recipe:2:workday'].config.chunks.length, 2);
  const cached = await new RecipeStore(saved, async () => { throw new Error('offline'); }, RECIPES, () => 2000).resolve('workday');
  assert.equal(cached.cached, true);
  assert.equal(cached.config.recipe.name, 'Workday');
  saved.data['recipe:2:workday'].config.chunks[1] = Buffer.from('broken').toString('base64');
  const corrupt = await new RecipeStore(saved, async () => { throw new Error('offline'); }, RECIPES, () => 3000).resolve('workday');
  assert.equal(corrupt.ok, false);
});

test('an invalid chunk manifest causes no chunk requests', async () => {
  for (const overrides of [{ chunk_count: 1.5 }, { digest_algorithm: 'raw-sha256' }, { extra: 1 }]) {
    const { fetchFn, seen } = published([tableBytes()], overrides);
    const result = await new RecipeStore(storage(), fetchFn, RECIPES).resolve('workday');
    assert.equal(result.ok, false);
    assert.equal(seen.length, 1);
  }
});

test('a recipe that does not match its digest, is not this adapter, or is not a table is no recipe', async () => {
  const cases = [
    published([tableBytes()], { revision: 'b'.repeat(64) }),
    published([tableBytes({ ...table, name: 'Lever' })]),
    published([tableBytes({ ...table, matches: [] })]),
    published([Buffer.from('not json')]),
  ];
  for (const { fetchFn } of cases) {
    const got = await new RecipeStore(storage(), fetchFn, RECIPES).resolve('workday');
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
