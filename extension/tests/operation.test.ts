import { test } from "node:test";
import assert from "node:assert/strict";
import { Operation } from "../runtime/operation";

const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const create = () => new Operation(() => "https://example.com/apply", () => {});

test("pause holds an in-flight widget at its next checkpoint", async () => {
  const operation = create();
  operation.start();
  let wrote = false;
  operation.pause();
  const pending = operation.sleep(0).then(() => { wrote = true; });
  await tick();
  assert.equal(wrote, false);
  operation.resume();
  await pending;
  assert.equal(wrote, true);
  operation.finish();
});

test("stop interrupts a paused widget and excludes a second fill until unwind", async () => {
  const operation = create();
  operation.start();
  operation.pause();
  const pending = operation.checkpoint();
  operation.stop();
  await assert.rejects(pending, { name: "AbortError" });
  assert.throws(() => operation.start(), /already running/);
  operation.finish();
  operation.start();
  await operation.checkpoint();
  operation.finish();
});

test("a late network answer cannot write after stop or contaminate the next run", async () => {
  const operation = create();
  let resolve!: (value: string) => void;
  const response = new Promise<string>(r => { resolve = r; });
  operation.start();
  let wrote = false;
  const pending = operation.wait(response).then(() => { wrote = true; });
  operation.stop();
  await assert.rejects(pending, { name: "AbortError" });
  operation.finish();
  operation.start();
  resolve("old answer");
  await tick();
  assert.equal(wrote, false);
  operation.finish();
});

test("navigation invalidates the current operation before another write", async () => {
  let url = "https://example.com/job-one";
  const operation = new Operation(() => url, () => {});
  operation.start();
  url = "https://example.com/job-two";
  await assert.rejects(operation.checkpoint(), { name: "AbortError" });
});
