import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const json = (name: string) => JSON.parse(readFileSync(path.join(root, name), "utf8"));

test("every adapter has one isolated build entry and no old runtime is packaged", () => {
  const manifest = json(".output/chrome-mv3/manifest.json");
  const hosts = json("adapters/hosts.json");
  const recipes = readdirSync(path.join(root, "adapters/recipes")).filter(file => file.endsWith(".json")).map(file => json(`adapters/recipes/${file}`));
  const expected = new Map<string, string[]>(Object.entries(hosts));
  for (const recipe of recipes) expected.set(recipe.name.toLowerCase(), recipe.matches);
  assert.equal(manifest.content_scripts.length, expected.size + 1);
  const observer = manifest.content_scripts.find((entry: {js: string[]}) => entry.js.includes("content-scripts/ashby-save-observer.js"));
  assert.ok(observer);
  assert.equal(observer.world, "MAIN");
  assert.equal(observer.run_at, "document_start");
  assert.deepEqual(observer.matches, ["https://jobs.ashbyhq.com/*"]);
  for (const [name, matches] of expected) {
    const entry = manifest.content_scripts.find((entry: {js: string[]}) => entry.js.includes(`content-scripts/${name}.js`));
    assert.ok(entry, name);
    assert.deepEqual([...entry.matches].sort(), [...matches].sort());
    assert.equal(entry.all_frames, true);
    assert.equal(entry.run_at, "document_idle");
    assert.equal(entry.js.length, 1, "an adapter is a bundle, not an ordered chain of global scripts");
    const code = readFileSync(path.join(root, ".output/chrome-mv3", entry.js[0]), "utf8");
    assert.doesNotMatch(code, /__jtReader|__jtProfile|__jtATS/);
  }
  for (const removed of ["ats", "readers", "engine.js", "content.js", "manifest.json"]) assert.equal(existsSync(path.join(root, removed)), false, removed);
  assert.deepEqual(manifest.permissions.sort(), ["scripting", "storage"]);
});
