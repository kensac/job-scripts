const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../extension/background.js'), 'utf8');
// The routing only, without the worker's fetch paths and importScripts.
const routing = source.slice(source.indexOf('const ownerKey ='), source.indexOf('chrome.runtime.onMessage.addListener'));

// The worker is stopped whenever the browser feels like it, so a fresh
// context over the same session storage is what a restart looks like.
function worker(session, chromeExtras) {
  const context = {
    chrome: {
      storage: {
        session: {
          get: async (key) => (key in session ? { [key]: session[key] } : {}),
          set: async (values) => Object.assign(session, values),
          remove: async (key) => { delete session[key]; },
        },
      },
      ...chromeExtras,
    },
  };
  return vm.runInNewContext(`${routing}; ({ panelOp, panelEvent })`, context);
}

test('the first operation puts panel.js in the top frame and delivers again', async () => {
  const sent = [];
  const injected = [];
  let listening = false;
  const chromeExtras = {
    tabs: {
      sendMessage: async (tabId, msg, options) => {
        if (!listening) throw new Error('Could not establish connection.');
        sent.push({ tabId, msg, options });
      },
    },
    scripting: {
      executeScript: async (details) => { injected.push(details); listening = true; },
    },
  };
  const { panelOp } = worker({}, chromeExtras);
  const res = await panelOp({ op: 'paint', args: ['<p>hi</p>', {}] }, { tab: { id: 7 }, frameId: 3 });

  // Values built inside the vm context carry its Object.prototype, not this
  // one's, so equality here is on the contents.
  assert.equal(res.ok, true);
  assert.deepEqual(JSON.parse(JSON.stringify(injected)), [{ target: { tabId: 7, frameIds: [0] }, files: ['panel.js'] }]);
  assert.equal(sent.length, 1, 'the first delivery failed, so exactly one lands');
  assert.equal(sent[0].options.frameId, 0, 'the panel belongs to the top frame');
  assert.equal(sent[0].msg.op, 'paint');
});

// A click that came back to no owner is a button that silently did nothing.
test('an event goes back to the frame that painted it, across a worker restart', async () => {
  const session = {};
  const sent = [];
  const chromeExtras = {
    tabs: { sendMessage: async (tabId, msg, options) => sent.push({ tabId, msg, options }) },
    scripting: { executeScript: async () => {} },
  };
  await worker(session, chromeExtras).panelOp({ op: 'paint', args: ['<p>hi</p>'] }, { tab: { id: 7 }, frameId: 3 });
  assert.deepEqual(session, { 'panel:7': 3 });

  // A new context is a restarted worker: nothing in memory, storage intact.
  const event = { type: 'click', id: 'jt-autofill' };
  const res = await worker(session, chromeExtras).panelEvent({ event }, { tab: { id: 7 }, frameId: 0 });
  assert.equal(res.ok, true);
  assert.equal(sent.at(-1).options.frameId, 3, 'back to the frame beside the form');
  assert.equal(sent.at(-1).msg.kind, 'panel-event');
});

test('removing the panel forgets the owner', async () => {
  const session = { 'panel:7': 3 };
  const chromeExtras = {
    tabs: { sendMessage: async () => {} },
    scripting: { executeScript: async () => {} },
  };
  const { panelOp, panelEvent } = worker(session, chromeExtras);
  await panelOp({ op: 'remove', args: [] }, { tab: { id: 7 }, frameId: 3 });
  assert.deepEqual(session, {});
  const res = await panelEvent({ event: {} }, { tab: { id: 7 }, frameId: 0 });
  assert.equal(res.ok, false);
});
