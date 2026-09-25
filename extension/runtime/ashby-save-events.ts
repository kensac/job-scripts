export const SAVE_EVENT = "job-tracker:ashby-save";
export const SAVE_PROBE = "job-tracker:ashby-save-probe";

/** Observe the site's own writes. Never initiate, retry, or change a request. */
export function observeAshbySaves(): void {
  const emit = (detail: object) => document.dispatchEvent(new CustomEvent(SAVE_EVENT, { detail: JSON.stringify(detail) }));
  document.addEventListener(SAVE_PROBE, () => emit({ phase: "ready" }));
  const original = window.fetch;
  let sequence = 0;
  window.fetch = function (input, init) {
    let tracked: { id: number; path: string } | undefined;
    try {
      const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, location.href);
      if (url.origin === location.origin && url.pathname === "/api/non-user-graphql" && typeof init?.body === "string") {
        const body = JSON.parse(init.body);
        if (body.operationName === "ApiSetFormValue" && typeof body.variables?.path === "string") {
          tracked = { id: ++sequence, path: body.variables.path };
          emit({ ...tracked, phase: "started" });
        }
      }
    } catch { /* An unrelated request must retain its original behavior. */ }
    const pending = original.call(this, input, init);
    if (tracked) {
      const receipt = tracked;
      void pending.then(async response => {
        try {
          const body = await response.clone().json();
          const accepted = response.ok && !body.errors?.length && body.data?.setFormValue != null;
          emit({ ...receipt, phase: accepted ? "accepted" : "failed" });
        } catch { emit({ ...receipt, phase: "failed" }); }
      }, () => emit({ ...receipt, phase: "failed" }));
    }
    return pending;
  };
}
