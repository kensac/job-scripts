import { build } from "esbuild";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
await build({
  stdin: {
    contents: 'export { startApplication } from "./runtime/application.js"; export { Operation } from "./runtime/operation.ts"; export { SubmissionStore } from "./background/submissions.js";',
    resolveDir: root,
  },
  bundle: true,
  format: "esm",
  outfile: new URL("../.output/preview.js", import.meta.url).pathname,
});
