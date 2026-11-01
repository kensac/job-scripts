import { test } from "node:test";
import assert from "node:assert/strict";

import { call } from "../background/index.js";

const answer = (response: Response | (Partial<Response> & { headers: Headers })) => {
  globalThis.fetch = (async () => response) as typeof fetch;
  return call({ path: "user/apply/context", body: undefined });
};

test("only a redirect or a 401 asks the person to sign in", async () => {
  const redirected = await answer({ type: "opaqueredirect", status: 0, headers: new Headers() });
  assert.equal(redirected.signin, "https://www.kanishksachdev.com/job-tracker");
  const unauthenticated = await answer(new Response('{"error":{"code":"UNAUTHENTICATED"}}', { status: 401, headers: { "content-type": "application/json" } }));
  assert.ok(unauthenticated.signin);
});

test("an outage that is not JSON is an error, not a sign-in", async () => {
  // Starlette's unhandled-error reply, and an edge deny the proxy labels text/plain.
  for (const [body, status] of [["Internal Server Error", 500], ["", 502]] as const) {
    const got = await answer(new Response(body, { status, headers: { "content-type": "text/plain; charset=utf-8" } }));
    assert.equal(got.ok, false);
    assert.equal(got.status, status);
    assert.equal(got.signin, undefined);
  }
  const json = await answer(new Response('{"detail":"boom"}', { status: 503, headers: { "content-type": "application/json" } }));
  assert.deepEqual(json, { ok: false, status: 503, json: { detail: "boom" } });
});
