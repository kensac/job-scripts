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

// panel.js is the panel's DOM, and it runs in whichever frame shows the
// panel, which is not always the frame that reads the form.
const panelSource = fs.readFileSync(path.join(__dirname, '../../extension/panel.js'), 'utf8');

function fakeDom() {
  const listeners = {};
  const make = (tag) => {
    const el = {
      tag,
      id: '',
      children: [],
      attributes: {},
      classes: new Set(),
      innerHTML: '',
      isConnected: false,
      shown: 0,
      dataset: {},
      setAttribute(name, value) { this.attributes[name] = String(value); },
      removeAttribute(name) { delete this.attributes[name]; },
      attachShadow() { this.shadow = { append: (...kids) => this.children.push(...kids) }; return this.shadow; },
      addEventListener(type, fn) { listeners[type] = fn; },
      classList: { toggle: (name, on) => (on ? el.classes.add(name) : el.classes.delete(name)) },
      // Crude but honest: the two selectors panel.js actually uses.
      querySelectorAll: (selector) =>
        (el.ids || []).filter((child) =>
          selector === '[id]' ? child.id : ['input', 'textarea', 'select'].includes(child.tag),
        ),
      showPopover() { this.shown += 1; },
      remove() { this.isConnected = false; },
    };
    return el;
  };
  const body = make('body');
  return {
    listeners,
    body,
    context: {
      window: {},
      document: { createElement: make, body: { appendChild: (el) => { el.isConnected = true; body.children.push(el); } } },
      chrome: { runtime: { getURL: (p) => p, onMessage: { addListener: () => {} } } },
    },
  };
}

function panelUnderTest() {
  const dom = fakeDom();
  const events = [];
  vm.runInNewContext(panelSource, dom.context);
  const panel = dom.context.window.__jtPanel.create((event) => events.push(event));
  return { dom, events, panel };
}

// A transform, filter or contain on an ancestor makes that ancestor the
// containing block for position: fixed, and the panel then scrolls away with
// the form instead of holding the window's edge (measured on a transformed
// body: top 16px becomes -1063px after one scroll). The top layer has no such
// ancestor, so the host is a manual popover.
test('the panel goes in the top layer and leaves the rest of the page clickable', () => {
  const { dom, panel } = panelUnderTest();
  panel.paint('<p>hello</p>', { theme: 'dark', collapsed: true });
  const host = dom.body.children[0];
  assert.equal(host.id, 'jt-apply-host');
  assert.equal(host.attributes.popover, 'manual', 'the host must be a manual popover');
  assert.equal(host.shown, 1, 'and must be shown, which is what puts it in the top layer');

  // The browser's popover styles stretch the host over the whole viewport,
  // which would swallow every click meant for the page underneath.
  const css = fs.readFileSync(path.join(__dirname, '../../extension/panel.css'), 'utf8');
  const rule = css.slice(css.indexOf(':host {'), css.indexOf('#jt-apply {'));
  for (const declaration of ['width: 0 !important', 'height: 0 !important', 'display: block !important']) {
    assert.ok(rule.includes(declaration), `:host must keep "${declaration}"`);
  }
});

// The frame that owns the flow binds by id and never reads the panel back,
// because in an embedded form the panel is in a document it cannot touch.
test('an event carries the id and every input value with it', () => {
  const { dom, events, panel } = panelUnderTest();
  panel.paint('<textarea id="jt-note"></textarea><button id="jt-send">Send</button>', {});
  const note = { tag: 'textarea', id: 'jt-note', type: 'textarea', value: 'the select would not open' };
  const send = { tag: 'button', id: 'jt-send', dataset: {}, closest: () => send };
  const shadowPanel = dom.body.children[0].children[1];
  shadowPanel.ids = [note, send];
  dom.listeners.click({ target: send });
  // Through JSON because the event is built inside the vm context, whose
  // Object.prototype is not this one's.
  assert.deepEqual(JSON.parse(JSON.stringify(events)), [
    { type: 'click', id: 'jt-send', ai: null, values: { 'jt-note': 'the select would not open' } },
  ]);
});

// A field's key is its own: "label:Location" and "_systemfield_name" are both
// real keys, and a selector would have to escape the first.
test('a control is found by id even when the id is not a selector', () => {
  const { dom, panel } = panelUnderTest();
  panel.paint('<button id="jt-ai-label:Location">Draft answer</button>', {});
  const button = { tag: 'button', id: 'jt-ai-label:Location', disabled: false, textContent: 'Draft answer' };
  dom.body.children[0].children[1].ids = [button];
  panel.mark('jt-ai-label:Location', { disabled: true, text: 'asking…' });
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, 'asking…');
});

// A hydrating app throws the host out with the markup it did not render.
test('a panel the page threw away goes back without a repaint', () => {
  const { dom, panel } = panelUnderTest();
  panel.paint('<p>filled</p>', {});
  const host = dom.body.children[0];
  host.remove();
  panel.ensure();
  assert.equal(host.isConnected, true);
  assert.equal(host.shown, 2, 'and is shown again, or it is out of the top layer');
});
