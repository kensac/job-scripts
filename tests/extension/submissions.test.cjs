const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const Store = vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../extension/submissions.js'), 'utf8') + '; SubmissionStore', { URL });
const sender = { tab: { id: 5 }, frameId: 0, url: 'https://job-boards.greenhouse.io/example/jobs/123' };
function storage() {
  const data = {};
  return {
    get: async key => ({ [key]: structuredClone(data[key]) }),
    set: async values => Object.assign(data, structuredClone(values)),
    remove: async key => { delete data[key]; },
  };
}
const arm = { action: 'arm', url: sender.url, title: 'Example job', fillId: 12, fields: [{ key: 'name', final: 'Alex', remember: false }] };

test('redirect and worker restart retain the attempt without recording it early', async () => {
  const saved = storage();
  let calls = 0;
  const request = async () => { calls++; return { ok: true, json: { job_id: 20 } }; };
  await new Store(saved, request).handle(arm, sender);
  const resumed = new Store(saved, request);
  const redirect = { ...sender, url: 'https://job-boards.greenhouse.io/example/confirmation' };
  const found = await resumed.handle({ action: 'get' }, redirect);
  assert.equal(found.state.fillId, 12);
  assert.equal(found.state.status, 'watching');
  assert.equal(calls, 0);
  const receipt = await resumed.handle({ action: 'confirm', fillId: 12 }, redirect);
  assert.equal(receipt.state.status, 'recorded');
  assert.equal(receipt.state.fields, undefined);
  assert.equal(calls, 1);
});

test('failed recording persists and duplicate retries send once', async () => {
  const saved = storage();
  const first = new Store(saved, async () => ({ ok: false, status: 503 }));
  await first.handle(arm, sender);
  assert.equal((await first.handle({ action: 'confirm', fillId: 12 }, sender)).state.status, 'confirmed');
  let calls = 0;
  const resumed = new Store(saved, async () => { calls++; return { ok: true, json: { job_id: 20 } }; });
  await Promise.all([resumed.handle({ action: 'retry', fillId: 12 }, sender), resumed.handle({ action: 'retry', fillId: 12 }, sender)]);
  assert.equal(calls, 1);
});

test('manual forms acquire a ledger row and unrelated tabs cannot confirm it', async () => {
  const calls = [];
  const store = new Store(storage(), async request => { calls.push(request); return { ok: true, json: { fill_id: 13 } }; });
  const armed = await store.handle({ ...arm, fillId: null, fields: [] }, sender);
  assert.equal(armed.state.fillId, null);
  assert.equal(calls.length, 0);
  assert.equal((await store.handle({ action: 'confirm', fillId: null }, { ...sender, tab: { id: 6 } })).ok, false);
  assert.equal((await store.handle({ action: 'confirm', fillId: null }, { ...sender, url: 'https://other.example/confirmation' })).ok, false);
  assert.equal((await store.handle({ action: 'retry', fillId: null }, sender)).ok, false);
  assert.equal(calls.length, 0);
  const recorded = await store.handle({ action: 'confirm', fillId: null }, sender);
  assert.equal(recorded.state.status, 'recorded');
  assert.equal(recorded.state.fillId, 13);
  assert.equal(calls[0].path, 'user/apply/resolve');
  assert.equal(calls[1].path, 'user/apply/fills/13/submitted');
});
