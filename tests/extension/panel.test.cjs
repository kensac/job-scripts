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

// A page that puts a transform, filter or contain on an ancestor makes that
// ancestor the containing block for position: fixed, and the panel then
// scrolls away with the form instead of staying at the window's edge
// (measured on a transformed body: top 16 becomes -1063 after one scroll).
// The top layer has no such ancestor, so the host is a manual popover.
test('the panel goes in the top layer and leaves the rest of the page clickable', () => {
  const slice = source.slice(source.indexOf('  function attach()'), source.indexOf('  // The host is what the page can remove'));
  const shown = [];
  const appended = [];
  const element = () => ({
    attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    attachShadow: () => ({ append: () => {} }),
    append: () => {},
    showPopover() { shown.push(this.attributes.popover); },
  });
  const context = {
    document: { createElement: element, body: { appendChild: (el) => appended.push(el) } },
    chrome: { runtime: { getURL: (p) => p } },
  };
  vm.runInNewContext(`let host = null; ${slice}; mountPanel()`, context);
  assert.deepEqual(shown, ['manual'], 'the host must be shown as a manual popover');
  assert.equal(appended.length, 1);

  // The browser's popover styles stretch the host over the whole viewport,
  // which would swallow every click meant for the page underneath.
  const css = fs.readFileSync(path.join(__dirname, '../../extension/panel.css'), 'utf8');
  const rule = css.slice(css.indexOf(':host {'), css.indexOf('#jt-apply {'));
  for (const declaration of ['width: 0 !important', 'height: 0 !important', 'display: block !important']) {
    assert.ok(rule.includes(declaration), `:host must keep "${declaration}"`);
  }
});
