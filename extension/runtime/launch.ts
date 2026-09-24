import type { ContentScriptContext } from "wxt/utils/content-script-context";
import type { Adapter, AdapterContext } from "../adapters/types";
import { Operation } from "./operation";
import { startApplication } from "./application.js";
import { releaseAutomationFocus } from "./focus";

export async function launch(
  lifecycle: ContentScriptContext,
  factory: (context: AdapterContext) => Adapter | undefined,
): Promise<void> {
  const operation = new Operation(() => location.href, () => {});
  const context: AdapterContext = {
    operation,
    profile: {},
    getPublicJson: url => chrome.runtime.sendMessage({ kind: "get", url }),
  };
  lifecycle.onInvalidated(() => operation.stop());
  const adapter = factory(context);
  const ownerKey = Symbol.for("job-tracker.application-owner");
  const previous = Reflect.get(window, ownerKey) as { lifecycle: ContentScriptContext; hasAdapter: boolean } | undefined;
  if (previous?.lifecycle.isValid) {
    if (previous.hasAdapter || !adapter) return;
    previous.lifecycle.notifyInvalidated();
  }
  const owner = { lifecycle, hasAdapter: !!adapter };
  Reflect.set(window, ownerKey, owner);
  lifecycle.onInvalidated(() => {
    if (Reflect.get(window, ownerKey) === owner) Reflect.deleteProperty(window, ownerKey);
  });
  if (adapter) {
    const read = adapter.read.bind(adapter);
    const fill = adapter.fill.bind(adapter);
    adapter.read = () => releaseAutomationFocus(document, async () => read());
    adapter.fill = (field, value, file) => releaseAutomationFocus(document, () => fill(field, value, file));
  }
  await startApplication(adapter, context, lifecycle);
}
