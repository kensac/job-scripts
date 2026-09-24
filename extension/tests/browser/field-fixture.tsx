import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { createAdapter as ashby } from "../../adapters/ashby";
import { createAdapter as lever } from "../../adapters/lever";
import { createAdapter as greenhouse } from "../../adapters/greenhouse";
import { createRecipeAdapter } from "../../adapters/recipe";
import { Operation } from "../../runtime/operation";
import { releaseAutomationFocus } from "../../runtime/focus";

const adapterName = new URLSearchParams(location.search).get("adapter") || "ashby";
const operation = new Operation(() => location.href, () => {});
const context = { operation, profile: {}, getPublicJson: async () => ({ ok: false }) };
const adapter = adapterName === "recipe" ? createRecipeAdapter([{
  name: "Fixture", matches: [`${location.origin}/*`], fields: [
    { name: "email", fact: "email", variants: [{ paths: ['//*[@id="email"]'] }] },
    { name: "fullName", fact: "full_name", variants: [{ paths: ['//*[@id="name"]'] }] },
  ],
}], context)! : ({ ashby, lever, greenhouse })[adapterName as "ashby"](context);

function Field({ id, type }: { id: string; type: string }) {
  const [value, setValue] = useState("");
  const [committed, setCommitted] = useState("");
  const [error, setError] = useState("");
  return <div className="ashby-application-form-field-entry application-question">
    <label className="application-label" htmlFor={id}>{id === "email" ? "Email" : "Name"}</label>
    <input id={id} name={id} type={type} required value={value}
      onChange={e => setValue(e.target.value)}
      onBlur={() => { setCommitted(value); setError(value ? "" : "Required"); }} />
    <output id={`${id}-committed`}>{committed}</output>
    <span id={`${id}-error`}>{error}</span>
  </div>;
}

async function fill() {
  operation.start();
  try {
    const fields = await adapter.read();
    for (const field of fields) {
      if (new URLSearchParams(location.search).has("prefocused") && field.fact !== "email" && field.key !== "email") continue;
      await releaseAutomationFocus(document, () => adapter.fill(field, field.fact === "email" || field.key === "email" ? "alex@example.com" : "Alex", null));
    }
  } finally { operation.finish(); }
  document.querySelector("#done")!.textContent = "Done";
}

createRoot(document.querySelector("#root")!).render(<>
  <form id="application-form" onSubmit={event => event.preventDefault()}>
    <Field id="email" type="email" /><Field id="name" type="text" />
  </form>
  <button onMouseDown={event => {
    if (new URLSearchParams(location.search).has("prefocused")) event.preventDefault();
  }} onClick={fill}>Fill fixture</button><output id="done" />
</>);
