// The panel's DOM, and nothing about applying.
//
// It runs in whichever frame SHOWS the panel, which is not always the frame
// that reads the form. An employer's careers page embeds the application in a
// cross-origin iframe (Greenhouse's embed is one, 6084px tall inside an 805px
// window on app.careerpuck.com), and position: fixed inside an iframe pins to
// that FRAME's viewport, so a panel mounted beside the form rides the page
// down and out of sight. The top layer does not help: it is per-document, and
// the form's document is the iframe.
//
// So content.js stays with the form, where the reader's DOM handles are, and
// asks the top frame to show the panel for it. This file is what runs there.
// It knows how to paint HTML and report which control the person used; every
// decision about WHAT to paint stays in content.js.
(() => {
  if (window.__jtPanel) return;

  // Every event carries the panel's current input values, so the frame that
  // owns the flow never has to ask for one across a frame boundary.
  const values = (root) => {
    const out = {};
    for (const el of root.querySelectorAll("input, textarea, select")) {
      if (el.id) out[el.id] = el.type === "checkbox" || el.type === "radio" ? el.checked : el.value;
    }
    return out;
  };

  function build(onEvent) {
    let host = null;
    let panel = null;

    // The host is a manual popover, so the browser puts it in the TOP LAYER.
    // position: fixed alone is not enough: a transform, filter,
    // backdrop-filter, contain or perspective on any ancestor makes that
    // ancestor the containing block, and the panel then scrolls with the page
    // as though it were pinned to the form. The top layer has no ancestor to
    // take the job, and it paints above every stacking context, so a page
    // cannot cover the panel either. "manual" means nothing dismisses it: not
    // Escape, not a click elsewhere. Where showPopover is missing the append
    // still stands and the panel behaves as it did before.
    const attach = () => {
      document.body.appendChild(host);
      try {
        host.showPopover();
      } catch (_) {
        // Already open, or a browser without the top layer. Either is fine.
      }
    };

    const mount = () => {
      host = document.createElement("div");
      host.id = "jt-apply-host";
      host.setAttribute("popover", "manual");
      const root = host.attachShadow({ mode: "open" });
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = chrome.runtime.getURL("panel.css");
      panel = document.createElement("div");
      panel.id = "jt-apply";
      root.append(link, panel);
      // One delegated listener per kind, so a repaint never loses a handler
      // and the frame that owns the flow binds by id rather than by node.
      for (const type of ["click", "change"]) {
        panel.addEventListener(type, (ev) => {
          const el = ev.target.closest("[id], [data-ai]");
          if (!el) return;
          onEvent({ type, id: el.id || null, ai: el.dataset.ai || null, values: values(panel) });
        });
      }
      attach();
    };

    // By id without a selector: a field's key is its own, and keys carry
    // colons and other characters a selector would have to escape.
    const byId = (id) => {
      for (const el of panel ? panel.querySelectorAll("[id]") : []) if (el.id === id) return el;
      return null;
    };

    return {
      // A hydrating app (Greenhouse's board is a Remix app) throws the host
      // out with the markup it did not render. The host is what the page can
      // remove, so it is what "still there" asks about: panel.isConnected
      // would be true for a panel inside a shadow root whose host is gone.
      ensure() {
        if (host && !host.isConnected) attach();
      },
      paint(html, { theme, collapsed } = {}) {
        if (!host) mount();
        else if (!host.isConnected) attach();
        if (theme) panel.setAttribute("data-jt-theme", theme);
        else panel.removeAttribute("data-jt-theme");
        panel.classList.toggle("collapsed", !!collapsed);
        panel.innerHTML = html;
      },
      // One region repainted in place, for the report box, which opens and
      // closes without disturbing the result above it.
      region(id, html) {
        const el = byId(id);
        if (el) el.innerHTML = html;
      },
      // A control's transient state: a button disabled while its request is
      // in flight, and the word it shows while it waits.
      mark(id, props) {
        const el = byId(id);
        if (!el) return;
        if ("disabled" in props) el.disabled = props.disabled;
        if ("text" in props) el.textContent = props.text;
      },
      // What the panel says, so a page's own text can be read without it.
      text: () => (panel ? panel.innerText : ""),
      remove() {
        if (host) host.remove();
        host = null;
        panel = null;
      },
    };
  }

  // ONE PANEL PER DOCUMENT. A page can match a content script in its top
  // frame and in a nested frame as well (an ATS embedded in an ATS), and two
  // of these stacked in the same corner is worse than one panel that the
  // later flow paints over. Events go to whoever asked most recently, which
  // is the flow that painted what the person is looking at.
  let instance = null;
  let notify = () => {};
  function create(onEvent) {
    notify = onEvent;
    if (!instance) instance = build((event) => notify(event));
    return instance;
  }

  window.__jtPanel = { create };

  // Driven from another frame: content.js cannot reach this document, so the
  // background worker relays an operation in and an event back out. The
  // listener is installed on every frame this file lands in and does nothing
  // until an operation arrives, so the frame that mounts its own panel simply
  // never sends one.
  let served = null;
  chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
    if (!msg || msg.kind !== "panel-op") return;
    if (!served) {
      served = create((event) => {
        // Nothing waits on the answer: the worker's reply exists only so the
        // message port closes cleanly.
        chrome.runtime.sendMessage({ kind: "panel-event", event }).catch(() => {});
      });
    }
    served[msg.op](...msg.args);
    if (msg.op === "remove") served = null;
    reply({ ok: true });
  });

})();
