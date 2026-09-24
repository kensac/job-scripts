const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

test('Greenhouse confirmation can replace a hidden form without removing it', () => {
  let visible = false;
  const form = { getClientRects: () => visible ? [{}] : [] };
  const context = {
    window: {},
    document: { querySelector: () => form, body: { innerText: 'Thank you for applying to Example Company.' } },
  };
  const source = fs.readFileSync(path.join(__dirname, '../../extension/adapters/greenhouse.js'), 'utf8')
    .replace(/^import .*;\n/gm, '').replace('export function', 'function');
  const reader = vm.runInNewContext(source + '; createAdapter({})', context);
  assert.equal(reader.submitted(), true);
  visible = true;
  assert.equal(reader.submitted(), false);
  visible = false;
  context.document.body.innerText = 'Please correct the errors below.';
  assert.equal(reader.submitted(), false);
});
