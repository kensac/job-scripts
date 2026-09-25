import { test } from "node:test";
import assert from "node:assert/strict";
import { Window } from "happy-dom";
import { Operation } from "../runtime/operation";
import { createAdapter as lever } from "../adapters/lever.js";
import { createAdapter as ashby } from "../adapters/ashby.js";
import { createAdapter as greenhouse } from "../adapters/greenhouse.js";
import { createRecipeAdapter } from "../adapters/recipe.js";
import { releaseAutomationFocus } from "../runtime/focus";
import { commitControl } from "../adapters/dom";
import type { Adapter, AdapterContext } from "../adapters/types";

function dom(markup: string, url = "https://jobs.lever.co/example/apply") {
  const window = new Window({ url });
  window.document.body.innerHTML = markup;
  for (const key of ["window", "document", "location", "Node", "HTMLElement", "HTMLInputElement", "HTMLTextAreaElement", "Event", "MouseEvent", "KeyboardEvent", "InputEvent", "FocusEvent", "CustomEvent", "CSS", "XPathResult"] as const) {
    Object.defineProperty(globalThis, key, { configurable: true, value: key === "window" ? window : Reflect.get(window, key) });
  }
  const operation = new Operation(() => window.location.href, () => {});
  const context: AdapterContext = { operation, profile: {}, getPublicJson: async () => ({ ok: false }) };
  return { window, context, operation };
}

test("Lever reads and writes the same named facts without a window profile", async () => {
  const { window, context, operation } = dom('<form id="application-form"><div class="application-question"><label class="application-label">Email</label><input name="email"></div></form>');
  const adapter: Adapter = lever(context);
  const fields = await adapter.read();
  assert.equal(fields.length, 1);
  assert.equal(fields[0]!.fact, "email");
  operation.start();
  await adapter.fill(fields[0]!, "alex@example.com", null);
  assert.equal(adapter.current(fields[0]!), "alex@example.com");
  operation.finish();
  await window.happyDOM.close();
});

test("Ashby's canonical name field identifies the full-name fact despite its label", async () => {
  const { window, context } = dom('<div class="ashby-application-form-field-entry"><label for="_systemfield_name">Preferred First &amp; Last Name</label><input id="_systemfield_name"></div>', "https://jobs.ashbyhq.com/example/application");
  const fields = await ashby(context).read();
  assert.equal(fields.length, 1);
  assert.equal(fields[0].fact, "full_name");
  await window.happyDOM.close();
});

for (const [name, create, markup, url] of [
  ["Lever", lever, '<form id="application-form"><div class="application-question"><label class="application-label">Name</label><input name="name"></div></form>', "https://jobs.lever.co/example/apply"],
  ["Ashby", ashby, '<div class="ashby-application-form-field-entry"><label for="name">Name</label><input id="name"></div>', "https://jobs.ashbyhq.com/example/application"],
  ["Greenhouse", greenhouse, '<form id="application-form"><label for="name">Name</label><input id="name"></form>', "https://job-boards.greenhouse.io/example/jobs/123"],
] as const) test(`${name} cannot start writing while paused or after stop`, async () => {
  const { window, context, operation } = dom(markup, url);
  const adapter = create(context);
  const fields = await adapter.read();
  assert.equal(fields.length, 1);
  operation.start();
  operation.pause();
  const filling = adapter.fill(fields[0], "Do not write", null);
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(adapter.current(fields[0]), "");
  operation.stop();
  await assert.rejects(filling, { name: "AbortError" });
  assert.equal(adapter.current(fields[0]), "");
  operation.finish();
  await window.happyDOM.close();
});

test("recipe factories keep profile context and chosen configuration private", () => {
  const { context } = dom("", "https://example.com/apply");
  const recipe = { name: "Test", matches: ["https://example.com/*"], fields: [] };
  const first = createRecipeAdapter([recipe], context)!;
  const second = createRecipeAdapter([recipe], context)!;
  assert.equal(first.host, "test");
  assert.equal(first.useConfig({ ...recipe, name: "Other" }), false);
  assert.equal(first.useConfig({ ...recipe, fields: [{ name: "one", variants: [] }] }), true);
  assert.equal(second.host, "test");
  assert.equal("__jtReader" in window, false);
  assert.equal("__jtProfile" in window, false);
});

test("automation releases its control focus and leaves the page scrollable", async () => {
  const { window } = dom('<input id="name">');
  const input = document.querySelector<HTMLInputElement>("input")!;
  await releaseAutomationFocus(document, async () => { input.focus(); });
  assert.notEqual(document.activeElement, input);
  await window.happyDOM.close();
});

test("stop cancels a pending field commit without emitting more form events", async () => {
  const { window, operation } = dom('<input id="name">');
  const input = document.querySelector<HTMLInputElement>("input")!;
  input.focus();
  let blurred = 0;
  input.addEventListener("blur", () => blurred++);
  operation.start();
  const committing = commitControl(input, operation);
  operation.stop();
  await assert.rejects(committing, { name: "AbortError" });
  assert.equal(blurred, 0);
  operation.finish();
  await window.happyDOM.close();
});

for (const focused of [false, true]) test(`field commit emits one blur and focusout pair (focused: ${focused})`, async () => {
  const { window, operation } = dom('<input id="name">');
  const input = document.querySelector<HTMLInputElement>("input")!;
  if (focused) input.focus();
  const seen: string[] = [];
  input.addEventListener("blur", () => seen.push("blur"));
  document.addEventListener("focusout", () => seen.push("focusout"));
  operation.start();
  await commitControl(input, operation);
  assert.deepEqual(seen, ["blur", "focusout"]);
  assert.notEqual(document.activeElement, input);
  operation.finish();
  await window.happyDOM.close();
});
