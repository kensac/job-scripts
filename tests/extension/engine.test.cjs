const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(process.env.ENGINE_SOURCE || path.join(__dirname, '../../extension/engine.js'), 'utf8');
function engine(overrides = {}) {
  const context = {
    window: { __jtATS: [{ name: 'Test', matches: ['https://example.com/*'] }] },
    location: { href: 'https://example.com/apply' },
    Event,
    KeyboardEvent: class KeyboardEvent extends Event {
      constructor(type, options) { super(type, options); for (const key of ['key', 'code', 'keyCode', 'which']) this[key] = options[key]; }
    },
    ...overrides,
  };
  vm.runInNewContext(source.replace(/\}\)\(\);\s*$/, 'window.test = {runActions, fillGroup}; })();'), context);
  return context;
}
test('recipe Enter retains keyboard identity and legacy codes', async () => {
  const c = engine();
  let received;
  const target = new EventTarget();
  target.addEventListener('keydown', event => { received = event; });
  await c.window.test.runActions([{event:'keydown', eventOptions:{key:'Enter', code:'Enter', keyCode:13, which:13}}], {}, {}, target, '', null);
  assert.equal(received.key, 'Enter');
  assert.equal(received.code, 'Enter');
  assert.equal(received.keyCode, 13);
  assert.equal(received.which, 13);
  assert.ok(received instanceof c.KeyboardEvent);
});
for (const alternatives of [false, true]) test(`failed add preserves first entry, alternative wrappers: ${alternatives}`, async () => {
  let fills = 0;
  let adds = 0;
  const input = {tagName:'INPUT', getAttribute(){return null;}, setAttribute(){}, dispatchEvent(){}, set value(v){ fills++; }};
  const block = {compareDocumentPosition(){return 4;}};
  const wrapper = {compareDocumentPosition(){return 0;}};
  const add = {click(){adds++;}, dispatchEvent(){}};
  const c = engine({
    Node:{ELEMENT_NODE:1,DOCUMENT_POSITION_FOLLOWING:4},
    XPathResult:{ORDERED_NODE_SNAPSHOT_TYPE:7},
    document:{ evaluate(selector) {
      const nodes = selector === 'wrapper' ? [wrapper] : selector === 'block' ? [block] : selector === 'add' ? [add] : selector === 'input' ? [input] : [];
      return {snapshotLength:nodes.length,snapshotItem:i=>nodes[i]};
    } },
    HTMLInputElement: class {},
    FocusEvent: Event, MouseEvent: Event, InputEvent: Event, CustomEvent: Event,
    setTimeout: callback => callback(),
    Date: class extends Date { static now() { return this.clock = (this.clock || 0) + 5000; } },
  });
  await c.window.test.fillGroup({containerPath: alternatives ? ['block', 'wrapper'] : ['block'],addButtonPath:['add'],fields:[{name:'school',variants:[{paths:['input'],method:'default'}]}]}, [{school:'First'},{school:'Second'}],null,'education');
  assert.equal(fills,1);
  assert.equal(adds,1);
});
