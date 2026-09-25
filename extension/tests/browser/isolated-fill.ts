import { createAdapter } from "../../adapters/ashby";
import { Operation } from "../../runtime/operation";

const operation = new Operation(() => location.href, () => {});
const adapter = createAdapter({ operation, profile: {}, getPublicJson: async () => ({ ok: false }) });
Object.assign(globalThis, { fillIsolatedFixture: async () => {
  operation.start();
  try {
    const fields = await adapter.read();
    const name = fields.find(field => field.key === "name");
    if (!name) throw new Error("Missing fixture field");
    return await adapter.fill(name, "Isolated Person", null);
  } finally { operation.finish(); }
} });
