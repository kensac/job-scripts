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

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  (msg.kind === "pdf" ? pdf : msg.kind === "get" ? get : call)(msg)
    .then(reply)
    .catch((e) => reply({ ok: false, status: 0, error: String(e) }));
  return true;
});
