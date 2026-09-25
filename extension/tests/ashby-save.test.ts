import { test } from "node:test";
import assert from "node:assert/strict";
import { Window } from "happy-dom";
import { observeAshbySaves } from "../runtime/ashby-save-events";
import { confirmAshbySave } from "../adapters/ashby-save";
import { Operation } from "../runtime/operation";

function setup() {
  const window = new Window({ url: "https://jobs.ashbyhq.com/example/application" });
  for (const key of ["window", "document", "location", "CustomEvent", "Event"]) {
    Object.defineProperty(globalThis, key, { configurable: true, value: Reflect.get(window, key) });
  }
  const operation = new Operation(() => window.location.href, () => {});
  operation.start();
  const write = () => window.fetch("/api/non-user-graphql?op=ApiSetFormValue", {
    method: "POST", body: JSON.stringify({ operationName: "ApiSetFormValue", variables: { path: "name", value: "Example Person" } }),
  });
  return { window, operation, write };
}

test("field completion waits for the site's receipt, not its visible value", async () => {
  const { window, operation, write } = setup();
  let respond: (response: Awaited<ReturnType<typeof window.fetch>>) => void = () => {};
  window.fetch = (() => new Promise(resolve => { respond = resolve; })) as typeof window.fetch;
  observeAshbySaves();
  let completed = false;
  const saving = confirmAshbySave("name", operation, async () => { void write(); return true; }).then(result => { completed = true; return result; });
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(completed, false);
  respond(window.Response.json({ data: { setFormValue: { id: "form" } } }));
  assert.equal(await saving, true);
  operation.finish();
  await window.happyDOM.close();
});

for (const body of [{ errors: [{ message: "Save refused" }] }, { data: { setFormValue: null } }]) {
  test(`a successful HTTP response with no saved field is not completion: ${JSON.stringify(body)}`, async () => {
    const { window, operation, write } = setup();
    window.fetch = async () => window.Response.json(body);
    observeAshbySaves();
    await assert.rejects(confirmAshbySave("name", operation, async () => { void write(); return true; }), /did not confirm/);
    operation.finish();
    await window.happyDOM.close();
  });
}

test("missing observation requires reload before writing any field", async () => {
  const { window, operation } = setup();
  let written = false;
  await assert.rejects(confirmAshbySave("name", operation, async () => { written = true; return true; }), /Reload/);
  assert.equal(written, false);
  operation.finish();
  await window.happyDOM.close();
});

test("stop interrupts waiting for a field save without retrying it", async () => {
  const { window, operation, write } = setup();
  let requests = 0;
  window.fetch = (() => { requests++; return new Promise(() => {}); }) as typeof window.fetch;
  observeAshbySaves();
  const saving = confirmAshbySave("name", operation, async () => { void write(); return true; });
  operation.stop();
  await assert.rejects(saving, { name: "AbortError" });
  assert.equal(requests, 1);
  operation.finish();
  await window.happyDOM.close();
});
