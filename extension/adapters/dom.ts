/** React and similar controlled forms observe the native setter plus events. */
export function setNative(el: HTMLInputElement | HTMLTextAreaElement, value: string): void {
  const prototype = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
  if (!setter) throw new Error("This control has no native value setter");
  setter.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

export async function commitControl(el: HTMLElement, operation: Operation, options: FocusEventInit = {}): Promise<void> {
  // Let controlled input state render before blur handlers read it. This is
  // a task boundary, not a guessed wait for a site's network save.
  await operation.sleep(0);
  if (!el.isConnected) return;
  if (el.ownerDocument.activeElement === el) {
    el.blur();
  } else {
    // Recipe controls need not acquire focus. React observes focusout, while
    // direct DOM listeners can observe blur; dispatching only blur misses one.
    el.dispatchEvent(new FocusEvent("blur", { ...options, bubbles: false }));
    el.dispatchEvent(new FocusEvent("focusout", { ...options, bubbles: true }));
  }
}
import type { Operation } from "../runtime/operation";
