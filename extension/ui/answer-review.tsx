import React, { useEffect, useState } from "react";
import { createRoot, type Root } from "react-dom/client";

export interface ReviewField {
  key: string;
  label: string;
  kind: string;
  current: string;
  value: string;
  feedback: string;
  source: string;
  editable: boolean;
  canGenerate: boolean;
  history: { kind: string; at: string; value?: string; feedback?: string }[];
  required?: boolean;
  present?: boolean;
  saveUnconfirmed?: boolean;
}

export interface ReviewModel {
  fillId: number;
  fields: ReviewField[];
  state: "loading" | "ready" | "error";
  message: string;
  busy: boolean;
}

type Action = { type: "review"; action: string; key?: string; value?: string; feedback?: string };

function AnswerCard({ field, busy, emit }: { field: ReviewField; busy: boolean; emit: (action: Action) => void }) {
  const [value, setValue] = useState(field.value);
  const [feedback, setFeedback] = useState(field.feedback);
  const [copied, setCopied] = useState("");
  useEffect(() => { setValue(field.value); setFeedback(field.feedback); }, [field.value, field.feedback]);
  const action = (name: string) => emit({ type: "review", action: name, key: field.key, value, feedback });
  return <article className="answer-card">
    <div className="answer-heading"><h5>{field.label}</h5><span className="tag">{field.current.trim() ? field.source : "Unanswered"}</span></div>
    <p className="current-answer"><span>On the form</span>{field.current || "No answer yet"}</p>
    <div className="answer-actions"><button onClick={() => action("locate")}>Find on page</button></div>
    {field.editable ? <details className="answer-editor"><summary>Edit or improve answer</summary>
      <label>Answer<textarea disabled={busy} aria-label={`Answer for ${field.label}`} maxLength={20000} value={value} onChange={e => setValue(e.target.value)} /></label>
      <div className="answer-actions">
        <button disabled={busy} onClick={() => action("apply")}>Apply this answer</button>
        <button disabled={busy} onClick={() => action("refill")}>Refill saved answer</button>
        <button onClick={async () => { try { await navigator.clipboard.writeText(value); setCopied("Copied"); } catch { setCopied("Select the answer and copy it manually."); } }}>Copy</button>
      </div>
      {copied && <p role="status">{copied}</p>}
      <label>Suggestions for this answer<textarea disabled={busy} aria-label={`Suggestions for ${field.label}`} maxLength={4000} rows={2} value={feedback} onChange={e => setFeedback(e.target.value)} placeholder="For example: focus on my backend project and keep it shorter." /></label>
      <p>Saved with this application. Generating a new draft uses your AI allowance and does not change the form.</p>
      <div className="answer-actions"><button disabled={busy} onClick={() => action("save")}>Save draft & suggestions</button>
        {field.canGenerate && <button disabled={busy} onClick={() => action("generate")}>Generate new draft</button>}</div>
      {!!field.history.length && <details className="answer-history"><summary>Answer history ({field.history.length})</summary>{field.history.slice().reverse().map((item, index) => <div key={index}>
        <p>{item.kind === "generated" ? "Generated draft" : "Saved edit"} · {new Date(item.at).toLocaleString()}</p><p>{item.value}</p>{item.feedback && <p>Suggestions: {item.feedback}</p>}
      </div>)}</details>}
    </details> : <p>Complete this control on the form. Files, dates and repeated sections need direct review.</p>}
  </article>;
}

function AnswerReview({ model, emit }: { model: ReviewModel; emit: (action: Action) => void }) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");
  const remaining = model.fields.filter(field => field.present !== false && !field.current.trim());
  const unconfirmed = model.fields.filter(field => field.present !== false && field.current.trim() && field.saveUnconfirmed);
  const needsAttention = [...remaining, ...unconfirmed].sort((a, b) => Number(!!b.required) - Number(!!a.required));
  const shown = model.fields.filter(field => (status !== "blank" || !field.current.trim()) &&
    `${field.label} ${field.current} ${field.source}`.toLowerCase().includes(query.toLowerCase()));
  return <section className="answer-review" aria-label="Application answers">
    <div className="review-heading"><h4>Review your answers</h4><span className="count">{model.fields.length}</span></div>
    <p>Keep control of every answer. Nothing here submits your application.</p>
    <section className="remaining-fields" aria-label="Fields needing attention">
      <div className="review-heading"><h4>Finish on the form</h4><span className="count">{needsAttention.length}</span></div>
      {needsAttention.length ? <>
        <p>{remaining.length} unanswered{unconfirmed.length ? ` · ${unconfirmed.length} saves unconfirmed` : ""}. Choose a field to go straight to it.</p>
        <ul>{needsAttention.map(field => <li key={field.key}>
          <button disabled={model.busy} onClick={() => emit({ type: "review", action: "locate", key: field.key })}>
            <span>{field.label}</span><small>{field.saveUnconfirmed && field.current.trim() ? "Save unconfirmed" : field.required ? "Required" : "Unanswered"}</small>
          </button>
        </li>)}</ul>
      </> : <p>No unanswered fields detected on this page. Review the form before submitting.</p>}
      <p className="muted">After confirmed submission, short answers and choices you changed can be reused for the same question. Long answers and files stay with this application.</p>
    </section>
    {model.state === "ready" && <button disabled={model.busy} onClick={() => emit({ type: "review", action: "reload" })}>Reload saved answers</button>}
    {model.message && <p className="notice" role={model.state === "error" ? "alert" : "status"}>{model.message}</p>}
    {model.state === "loading" ? <p role="status">Loading saved drafts and suggestions…</p> : model.state === "error" ?
      <button onClick={() => emit({ type: "review", action: "reload" })}>Retry loading answers</button> : <>
        <div className="review-tools"><input type="search" aria-label="Search application answers" placeholder="Search questions or answers" value={query} onChange={e => setQuery(e.target.value)} />
          <select aria-label="Filter application answers" value={status} onChange={e => setStatus(e.target.value)}><option value="all">All fields</option><option value="blank">Unanswered</option></select></div>
        <p className="muted">Showing {shown.length} of {model.fields.length} fields</p>
        {!shown.length && <p className="review-empty">{model.fields.length ? "No fields match your search." : "No fields detected on this page."}</p>}
        {model.fields.map(field => <div key={`${model.fillId}:${field.key}`} hidden={!shown.includes(field)}><AnswerCard field={field} busy={model.busy} emit={emit} /></div>)}
      </>}
  </section>;
}

export function mountAnswerReview(node: HTMLElement, emit: (action: Action) => void): { render: (model: ReviewModel) => void; unmount: () => void } {
  const root: Root = createRoot(node);
  return { render: model => root.render(<AnswerReview model={model} emit={emit} />), unmount: () => root.unmount() };
}
