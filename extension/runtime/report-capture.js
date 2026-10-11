import { storePageReport } from "../report-queue.js";

export function createApplicationReportCapture({ reader, boxOf, plain, traces, send, VERSION, BUILD, getFields, getFill, getReviewState, getFilled, getLastError, getPolicy, getRecipeRevision }) {
  // Everything the extension saw, for triage: the fields as read with the
  // markup around each, what the API resolved, what took, the person's note.
  function capture() {
    const clean = (html) => html.replace(/<script[\s\S]*?<\/script>/gi, "");
    const boxes = getFields().map(boxOf).filter(Boolean);
    let root = boxes[0] ? boxes[0].parentElement : document.body;
    while (root && root !== document.body && !boxes.every((b) => root.contains(b))) root = root.parentElement;
    return {
      title: document.title,
      host: reader.host,
      version: chrome.runtime.getManifest().version,
      build: BUILD,
      userAgent: navigator.userAgent,
      at: new Date().toISOString(),
      fields: getFields().map((f) => {
        const box = boxOf(f);
        const ctl = f._ctl || (box && box.querySelector("input:not([type=hidden]), textarea, select, button"));
        return {
          ...plain(f),
          trace: traces.get(f.key) || f._trace || null,
          current: reader.current(f),
          control: ctl ? { tag: ctl.tagName, type: ctl.type || null, id: ctl.id || null, class: ctl.className } : null,
          html: box ? clean(box.outerHTML) : null,
        };
      }),
      resolved: getFill(),
      answer_review: getReviewState(),
      filled: Object.fromEntries(getFilled()),
      error: getLastError(),
      html: clean((root || document.body).outerHTML),
    };
  }

  async function queueReport(note) {
    const result = await storePageReport(chrome.storage.local, { url: location.href, note, page: capture() });
    void send({ kind: "report-pump" });
    return result;
  }

  // Every fill pass posts its own capture, tagged automatic, so a page can
  // be read the way a report is without the person pressing anything
  // (Kanishk, 2026-09-08: "stream all pages back with traces"). The button
  // stays for a note in the person's words. Failures are silent: the
  // capture is for triage, never in the person's way.
  async function autoReport(reason) {
    try {
      const pinned = getPolicy() && getPolicy().revision ? getPolicy().revision.slice(0, 12) : "none";
      await queueReport(`auto: ${reason} [ext ${VERSION} ${reader.host || "unknown"} schema 1 rev ${pinned} recipe ${getRecipeRevision()}]`);
    } catch (error) {
      console.warn("Page report could not be queued", error);
    }
  }

  return { capture, queueReport, autoReport };
}
