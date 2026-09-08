const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../extension/content.js'), 'utf8');

test('opening a panel report does not look like a new application page', () => {
  const declaration = source.slice(source.indexOf('  const pageSignature ='), source.indexOf('  let pageSig ='));
  const form = { id: 'education-start', closest: () => null };
  const note = { id: 'jt-note', closest: () => ({ id: 'jt-apply' }) };
  let controls = [form];
  const context = { document: { querySelectorAll: () => controls } };
  const signature = vm.runInNewContext(`${declaration}; pageSignature`, context);
  const before = signature();
  controls = [form, note];
  assert.equal(signature(), before);
  controls = [{ id: 'next-page', closest: () => null }, note];
  assert.notEqual(signature(), before);
});
