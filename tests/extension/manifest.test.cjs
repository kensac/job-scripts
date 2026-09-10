const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const manifest = JSON.parse(
  fs.readFileSync(path.join(__dirname, '../../extension/manifest.json'), 'utf8'),
);

const parts = (pattern) => {
  const m = /^(\*|https?):\/\/([^/]+)(\/.*)$/.exec(pattern);
  assert.ok(m, `not a match pattern: ${pattern}`);
  return { host: m[2], path: m[3] };
};

// Chrome refuses to load the whole extension when a pattern here carries a
// path, which is what copying the content scripts' patterns produces:
// "Invalid value for 'web_accessible_resources[0]'. Invalid match pattern."
test('every web-accessible resource pattern has the path Chrome demands', () => {
  for (const entry of manifest.web_accessible_resources) {
    for (const pattern of entry.matches) {
      assert.equal(parts(pattern).path, '/*', `${pattern} must end in /*`);
    }
  }
});

// A content script with no reachable panel.css draws an unstyled panel, so
// every host the extension runs on has to be covered here.
test('every host a content script runs on can read panel.css', () => {
  const covers = (war, host) =>
    war === '*' ||
    war === host ||
    (war.startsWith('*.') && (host === war.slice(2) || host.endsWith(war.slice(1))));
  const allowed = manifest.web_accessible_resources
    .filter((e) => e.resources.includes('panel.css'))
    .flatMap((e) => e.matches.map((m) => parts(m).host));
  for (const script of manifest.content_scripts) {
    for (const pattern of script.matches) {
      const { host } = parts(pattern);
      assert.ok(
        allowed.some((war) => covers(war, host)),
        `${host} runs a content script but cannot read panel.css`,
      );
    }
  }
});
