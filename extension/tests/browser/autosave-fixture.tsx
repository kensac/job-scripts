import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { createAdapter } from "../../adapters/ashby";
import { Operation } from "../../runtime/operation";
import { startApplication } from "../../runtime/application.js";
import { observeAshbySaves } from "../../runtime/ashby-save-events";

const receipts = new URLSearchParams(location.search).has("receipts");
if (receipts) {
  window.fetch = async (_input, init) => {
    const request = JSON.parse(String(init?.body));
    document.body.dataset.pendingSaves = String(Number(document.body.dataset.pendingSaves || 0) + 1);
    document.body.dataset.maxPendingSaves = String(Math.max(Number(document.body.dataset.maxPendingSaves || 0), Number(document.body.dataset.pendingSaves)));
    await new Promise(resolve => setTimeout(resolve, 200));
    document.body.dataset.pendingSaves = String(Number(document.body.dataset.pendingSaves) - 1);
    return new URLSearchParams(location.search).has("refused")
      ? Response.json({ errors: [{ message: "Fixture save refused" }] })
      : Response.json({ data: { setFormValue: { id: "fixture", path: request.variables.path } } });
  };
  observeAshbySaves();
}

// Ashby's mMe/$j components compare local text with the last saved prop,
// debounce changes, cancel that debounce on blur, and save asynchronously.
// Checked against its public frontend bundle on 2026-09-24. No employer
// requests are made by this fixture.
function Field({ id, label, type }: { id: string; label: string; type: string }) {
  const [value, setValue] = useState("");
  const [saved, setSaved] = useState("");
  const save = () => {
    if (value !== saved) {
      const pending = value;
      if (!pending) document.body.dataset.blankWrites = String(Number(document.body.dataset.blankWrites || 0) + 1);
      if (receipts) {
        void fetch("/api/non-user-graphql?op=ApiSetFormValue", {
          method: "POST", body: JSON.stringify({ operationName: "ApiSetFormValue", variables: { path: id, value: pending } }),
        }).then(response => response.json()).then(body => { if (body.data?.setFormValue) setSaved(pending); });
      } else window.setTimeout(() => setSaved(pending), 200);
    }
  };
  const callback = useRef(save);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => { callback.current = save; }, [save]);
  useEffect(() => {
    timer.current = window.setTimeout(() => callback.current(), 500);
    return () => window.clearTimeout(timer.current);
  }, [value]);
  return <div className="ashby-application-form-field-entry" data-field-path={receipts ? id : undefined}>
    <label htmlFor={id}>{label}</label>
    <input id={id} type={type} required value={value} onChange={e => setValue(e.target.value)}
      onBlur={() => { window.clearTimeout(timer.current); save(); }} />
    <output id={`${id}-saved`}>{saved}</output>
  </div>;
}

createRoot(document.querySelector("#root")!).render(<form onSubmit={e => e.preventDefault()}>
  <Field id="name" label="Name" type="text" />
  <Field id="email" label="Email" type="email" />
  <Field id="linkedin" label="Linkedin" type="url" />
  <div className="ashby-application-form-field-entry"><label htmlFor="resume">Resume</label><input id="resume" type="file" /></div>
  <button className="ashby-application-form-submit-button">Submit application</button>
</form>);

const answers: Record<string, string> = { name: "Alex Morgan", email: "alex@example.com", linkedin: "https://linkedin.com/in/example" };
const request = async (msg: any) => {
  if (msg.kind === "policy") {
    const on = { allowed: true, reason: null };
    return { ok: true, config: { schema_version: 1, revision: "fixture", adapter: "ashby", max_age_seconds: 300,
      features: { autofill: on, ai_suggestions: { allowed: false, reason: "DISABLED" }, resume_upload: on, auto_advance: on } } };
  }
  if (msg.kind === "pdf") return { ok: true, bytes: [37, 80, 68, 70], name: "fixture.pdf" };
  if (msg.path?.startsWith("user/apply/context")) return { ok: true, json: { job: null, profile: {}, answers: [], drafts: [] } };
  if (msg.path === "user/settings") return { ok: true, json: { prefs: {} } };
  if (msg.path === "user/apply/resolve") return { ok: true, json: { fill_id: 1, job_id: null, profile: {}, resume: { id: 1, has_pdf: true },
    fields: msg.body.fields.map((f: any) => ({ ...f, rung: f.kind === "file" ? "resume" : "profile", value: answers[f.key] ?? null })) } };
  return { ok: true, json: { id: 1, fields: [] } };
};
Object.assign(window, { chrome: {
  storage: { local: { get: (_: unknown, cb: any) => cb({}), set: () => {} } },
  runtime: { sendMessage: (msg: any, cb: any) => { const pending = request(msg); if (cb) pending.then(cb); return pending; },
    getURL: (p: string) => "../../extension/public/" + p, getManifest: () => ({ version: "fixture" }), onMessage: { addListener: () => {} } },
} });
const operation = new Operation(() => location.href, state => { document.body.dataset.operation = state; });
const context = { operation, profile: {}, getPublicJson: async () => ({ ok: false }) };
const lifecycle = { addEventListener: (target: any, type: string, callback: any, options: any) => target.addEventListener(type, callback, options),
  setInterval: (callback: any, delay: number) => window.setInterval(callback, delay) };
startApplication(createAdapter(context), context, lifecycle);
