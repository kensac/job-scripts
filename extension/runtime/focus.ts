/** Release only focus acquired by automation, never a newer human interaction. */
export async function releaseAutomationFocus<T>(document: Document, work: () => Promise<T>): Promise<T> {
  const previous = document.activeElement;
  let interacted = false;
  const interaction = (event: Event) => { if (event.isTrusted) interacted = true; };
  document.addEventListener("pointerdown", interaction, true);
  document.addEventListener("keydown", interaction, true);
  try {
    return await work();
  } finally {
    document.removeEventListener("pointerdown", interaction, true);
    document.removeEventListener("keydown", interaction, true);
    const active = document.activeElement;
    if (!interacted && active !== previous && active instanceof HTMLElement) active.blur();
  }
}
