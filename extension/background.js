// Every API call goes through here: a fetch from the service worker with
// host permission on the site rides the site's session cookie, while one
// from the content script on the ATS page is cross-site and gets the
// sign-in page instead. The content script sends {path, method, body} and
// gets back {ok, status, json} or {ok: false, signin} when there is no
// session.
const SITE = "https://www.kanishksachdev.com";
const API = `${SITE}/job-tracker/api/`;

async function call({ path, method = "GET", body }) {
  const res = await fetch(API + path, {
    method,
    credentials: "include",
    headers: body ? { "content-type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
    redirect: "manual",
  });
  const type = res.headers.get("content-type") || "";
  if (res.type === "opaqueredirect" || res.status === 401 || !type.includes("json")) {
    return { ok: false, status: res.status || 401, signin: `${SITE}/job-tracker` };
  }
  return { ok: res.ok, status: res.status, json: await res.json() };
}

async function pdf({ path }) {
  const res = await fetch(API + path, { credentials: "include", redirect: "manual" });
  if (!res.ok || !(res.headers.get("content-type") || "").includes("pdf")) {
    return { ok: false, status: res.status };
  }
  const name = /filename="([^"]+)"/.exec(res.headers.get("content-disposition") || "");
  return {
    ok: true,
    name: name ? name[1] : "resume.pdf",
    bytes: Array.from(new Uint8Array(await res.arrayBuffer())),
  };
}

// A public form API a reader needs that the page's own content security
// policy will not let a content script reach (Greenhouse's boards API).
// Allowlisted by host; the reader gets the JSON body.
const PUBLIC = ["boards-api.greenhouse.io", "boards-api.eu.greenhouse.io"];
async function get({ url }) {
  const u = new URL(url);
  if (!PUBLIC.includes(u.hostname)) return { ok: false, status: 0, error: "host not allowed" };
  const res = await fetch(url);
  return { ok: res.ok, status: res.status, json: res.ok ? await res.json() : null };
}

importScripts("submissions.js");
const submissions = new SubmissionStore(chrome.storage.session, call);
// The server's feature switches, from a fixed public route (no session,
// no identity): the content script names an adapter and gets back a
// validated configuration or unavailable. See policy.js.
importScripts("policy.js");
const policy = new PolicyStore(chrome.storage.local, fetch, `${SITE}/api/extension/config`);
// The published table for a config-driven reader, decoded and checked
// against its digest; 404 means the bundled table applies. See policy.js.
const recipes = new RecipeStore(chrome.storage.local, fetch, `${SITE}/api/extension/recipe`);
chrome.tabs.onRemoved.addListener(async (tabId) => {
  const all = await chrome.storage.session.get(null);
  const keys = Object.keys(all).filter((key) => key.startsWith(`submission:${tabId}:`) || key === `panel:${tabId}`);
  if (keys.length) await chrome.storage.session.remove(keys);
});

// THE PANEL IN THE TOP FRAME.
//
// When the form is embedded in a cross-origin iframe, content.js runs beside
// the form, where the reader's DOM handles are, and the panel has to be in
// the top frame or it rides the page out of sight (content.js says why). Two
// content scripts in one tab cannot speak to each other, so every operation
// and every event passes through here.
//
// The owner is kept in session storage rather than a variable: this worker is
// stopped whenever the browser feels like it, and a click that came back to
// no owner would be a button that silently did nothing.
const ownerKey = (tabId) => `panel:${tabId}`;

async function panelOp({ op, args }, sender) {
  const tabId = sender.tab && sender.tab.id;
  if (tabId === undefined) return { ok: false, status: 0, error: "no tab" };
  const deliver = () => chrome.tabs.sendMessage(tabId, { kind: "panel-op", op, args }, { frameId: 0 });
  if (op === "remove") await chrome.storage.session.remove(ownerKey(tabId));
  else await chrome.storage.session.set({ [ownerKey(tabId)]: sender.frameId });
  try {
    await deliver();
  } catch (_) {
    // Nothing is listening in the top frame yet, or the page navigated out
    // from under the last injection. Put panel.js there and deliver again;
    // the file guards itself against being run twice.
    await chrome.scripting.executeScript({ target: { tabId, frameIds: [0] }, files: ["panel.js"] });
    await deliver();
  }
  return { ok: true };
}

// A control the person used, back to the frame that painted it.
async function panelEvent({ event }, sender) {
  const tabId = sender.tab && sender.tab.id;
  if (tabId === undefined) return { ok: false, status: 0, error: "no tab" };
  const got = await chrome.storage.session.get(ownerKey(tabId));
  const frameId = got[ownerKey(tabId)];
  if (frameId === undefined) return { ok: false, status: 0, error: "no panel owner" };
  await chrome.tabs.sendMessage(tabId, { kind: "panel-event", event }, { frameId });
  return { ok: true };
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  (msg.kind === "panel" ? panelOp(msg, sender)
    : msg.kind === "panel-event" ? panelEvent(msg, sender)
    : msg.kind === "submission" ? submissions.handle(msg, sender)
    : msg.kind === "policy" ? policy.resolve(msg.adapter)
    : msg.kind === "recipe" ? recipes.resolve(msg.adapter)
    : (msg.kind === "pdf" ? pdf : msg.kind === "get" ? get : call)(msg))
    .then(reply)
    .catch((e) => reply({ ok: false, status: 0, error: String(e) }));
  return true;
});
