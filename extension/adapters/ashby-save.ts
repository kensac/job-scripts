import type { Operation } from "../runtime/operation";
import { SAVE_EVENT, SAVE_PROBE } from "../runtime/ashby-save-events";

// A safety deadline, not an assumption about when a save has completed.
const SAVE_DEADLINE_MS = 30_000;

export async function confirmAshbySave(path: string, operation: Operation, write: () => Promise<boolean>): Promise<boolean> {
  let ready = false;
  let requestId: number | undefined;
  let finish: (ok: boolean) => void = () => {};
  const receipt = new Promise<boolean>(resolve => { finish = resolve; });
  const listener = (event: Event) => {
    try {
      const data = JSON.parse((event as CustomEvent).detail);
      if (data.phase === "ready") ready = true;
      if (data.path !== path) return;
      if (data.phase === "started") requestId = data.id;
      else if (requestId !== undefined && data.id === requestId) finish(data.phase === "accepted");
    } catch { /* Page messages are untrusted and carry no extension authority. */ }
  };
  document.addEventListener(SAVE_EVENT, listener);
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    document.dispatchEvent(new Event(SAVE_PROBE));
    if (!ready) throw new Error("Reload this application page to enable field-save confirmation.");
    timer = setTimeout(() => finish(false), SAVE_DEADLINE_MS);
    if (!await write()) return false;
    const accepted = await operation.wait(receipt);
    if (!accepted) throw new Error("The application site did not confirm this field was saved. Review it before submitting.");
    return true;
  } finally {
    clearTimeout(timer);
    document.removeEventListener(SAVE_EVENT, listener);
  }
}
