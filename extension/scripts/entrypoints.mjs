import { readdir, readFile, mkdir, writeFile, unlink } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = fileURLToPath(new URL("../", import.meta.url));
const entries = path.join(root, "entrypoints");
await mkdir(entries, { recursive: true });
const expected = new Map();
expected.set("ashby-save-observer.content.ts", `// Built by scripts/entrypoints.mjs.
import { defineContentScript } from "wxt/utils/define-content-script";
import { observeAshbySaves } from "../runtime/ashby-save-events";
export default defineContentScript({
  matches: ["https://jobs.ashbyhq.com/*"],
  world: "MAIN",
  allFrames: true,
  runAt: "document_start",
  main: observeAshbySaves,
});
`);
const content = (name, matches, factory) => `// Built from the adapter registry by scripts/entrypoints.mjs.\nimport { defineContentScript } from "wxt/utils/define-content-script";\nimport { launch } from "../runtime/launch";\n${factory.imports}\nexport default defineContentScript({\n  matches: ${JSON.stringify(matches)},\n  allFrames: true,\n  runAt: "document_idle",\n  main(ctx) { return launch(ctx, ${factory.create}); },\n});\n`;
const hosts = JSON.parse(await readFile(path.join(root, "adapters/hosts.json"), "utf8"));
for (const [name, matches] of Object.entries(hosts)) {
  expected.set(`${name}.content.ts`, content(name, matches, {
    imports: `import { createAdapter } from "../adapters/${name}.js";`,
    create: "createAdapter",
  }));
}
for (const file of (await readdir(path.join(root, "adapters/recipes"))).filter(f => f.endsWith(".json")).sort()) {
  const config = JSON.parse(await readFile(path.join(root, "adapters/recipes", file), "utf8"));
  const name = config.name.toLowerCase();
  if (expected.has(`${name}.content.ts`)) throw new Error(`Duplicate adapter: ${name}`);
  expected.set(`${name}.content.ts`, content(name, config.matches, {
    imports: `import { createRecipeAdapter } from "../adapters/recipe.js";\nimport recipe from "../adapters/recipes/${file}";`,
    create: "context => createRecipeAdapter([recipe], context)",
  }));
}
for (const file of await readdir(entries)) {
  if (file.endsWith(".content.ts") && !expected.has(file)) await unlink(path.join(entries, file));
}
for (const [file, source] of expected) await writeFile(path.join(entries, file), source);
