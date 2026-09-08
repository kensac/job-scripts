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
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../extension/readers/greenhouse.js'), 'utf8'), context);
  assert.equal(context.window.__jtReader.submitted(), true);
  visible = true;
  assert.equal(context.window.__jtReader.submitted(), false);
  visible = false;
  context.document.body.innerText = 'Please correct the errors below.';
  assert.equal(context.window.__jtReader.submitted(), false);
});
