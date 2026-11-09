/**
 * Controls batch M (Form Builder, 2026-10-29): address, signature, user / case /
 * document pickers, rich text, button. The rules are in validate.ts /
 * validate.py; the pickers' allowed ids are decided by the server
 * (velaris_shared/forms/pickers.py) — these only draw them.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { createContext, useContext, useEffect, useRef, useState } from "react";
import type { FormFieldDef, FormSectionDef } from "./FormBuilder";
import { getAccessToken } from "@/auth/tokenManager";
import { COUNTRIES, SIGNATURE_MAX, SIGNATURE_PREFIX, fieldKey, formState, localToday } from "./validate";
import { evaluate } from "./expr";

type In = { value: any; onChange: (v: any) => void; readOnly?: boolean; inputStyle: React.CSSProperties };

/* ── Address ──────────────────────────────────────────────────────── */

const ADDRESS_LABELS: [string, string][] = [
  ["line1", "Street and number"], ["line2", "Apartment, floor (optional)"], ["city", "Town or city"],
  ["region", "Region (optional)"], ["postcode", "Postcode"],
];

export function AddressInput({ value, onChange, readOnly, inputStyle }: In) {
  const a: Record<string, string> = value && typeof value === "object" ? value : {};
  const set = (k: string, v: string) => {
    const next = { ...a, [k]: v };
    onChange(Object.values(next).some(x => typeof x === "string" && x.trim()) ? next : undefined);
  };
  const box = (k: string, label: string, span: number) => (
    <label key={k} style={{ gridColumn: `span ${span}`, fontSize: 11, color: "var(--text-muted)", minWidth: 0 }}>
      {label}
      <input aria-label={label} value={a[k] ?? ""} disabled={readOnly} maxLength={200}
        autoComplete={{ line1: "address-line1", line2: "address-line2", city: "address-level2", region: "address-level1", postcode: "postal-code" }[k]}
        onChange={e => set(k, e.target.value)} style={{ ...inputStyle, marginTop: 2 }} />
    </label>
  );
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(6, minmax(0, 1fr))", gap: 8 }}>
      {box("line1", ADDRESS_LABELS[0][1], 6)}
      {box("line2", ADDRESS_LABELS[1][1], 6)}
      {box("city", ADDRESS_LABELS[2][1], 3)}
      {box("region", ADDRESS_LABELS[3][1], 3)}
      {box("postcode", ADDRESS_LABELS[4][1], 2)}
      <label style={{ gridColumn: "span 4", fontSize: 11, color: "var(--text-muted)", minWidth: 0 }}>
        Country
        <select aria-label="Country" value={a.country ?? ""} disabled={readOnly} autoComplete="country"
          onChange={e => set("country", e.target.value)} style={{ ...inputStyle, marginTop: 2 }}>
          <option value="">Choose…</option>
          {COUNTRIES.map(c => <option key={c.code} value={c.code}>{c.name}</option>)}
        </select>
      </label>
    </div>
  );
}

export function addressText(v: any): string {
  if (!v || typeof v !== "object") return "";
  return ["line1", "line2", "city", "region", "postcode", "country"].map(k => v[k]).filter(x => typeof x === "string" && x.trim()).join(", ");
}

/* ── Signature ────────────────────────────────────────────────────── */

export function SignaturePad({ value, onChange, readOnly }: Omit<In, "inputStyle">) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const drawing = useRef(false);
  const valid = typeof value === "string" && value.startsWith(SIGNATURE_PREFIX);
  if (readOnly) {
    return valid
      ? <img src={value} alt="Signature" style={{ maxWidth: 400, width: "100%", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)", background: "#fff" }} />
      : <div style={{ fontSize: 12, color: "var(--text-muted)" }}>Not signed</div>;
  }
  const point = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    return [(e.clientX - r.left) * (e.currentTarget.width / r.width), (e.clientY - r.top) * (e.currentTarget.height / r.height)];
  };
  const ctx = () => canvas.current?.getContext("2d") ?? null;
  return (
    <div>
      <canvas ref={canvas} width={480} height={160} aria-label="Signature pad — draw your signature"
        style={{ width: "100%", maxWidth: 480, height: "auto", aspectRatio: "3 / 1", touchAction: "none", cursor: "crosshair",
          background: "#fff", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", display: "block" }}
        onPointerDown={e => {
          const c = ctx(); if (!c) return;
          drawing.current = true; e.currentTarget.setPointerCapture?.(e.pointerId);
          const [x, y] = point(e); c.lineWidth = 2.2; c.lineCap = "round"; c.strokeStyle = "#111"; c.beginPath(); c.moveTo(x, y);
        }}
        onPointerMove={e => { if (!drawing.current) return; const c = ctx(); if (!c) return; const [x, y] = point(e); c.lineTo(x, y); c.stroke(); }}
        onPointerUp={() => {
          if (!drawing.current) return;
          drawing.current = false;
          const url = canvas.current?.toDataURL("image/png");
          if (url && url.length <= SIGNATURE_MAX) onChange(url);
        }} />
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 4 }}>
        <button type="button" onClick={() => { const c = ctx(); if (c && canvas.current) c.clearRect(0, 0, canvas.current.width, canvas.current.height); onChange(undefined); }}
          style={smallBtn}>Clear</button>
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{valid ? "Signed" : "Draw with a mouse, finger or pen"}</span>
      </div>
    </div>
  );
}

/* ── Pickers ──────────────────────────────────────────────────────── */

const KIND: Record<string, "user" | "case" | "document"> = { user_picker: "user", case_picker: "case", document_picker: "document" };
/** Labels seen in this page, so a chosen id reads as its name (review step, read-only). */
const labels = new Map<string, string>();
export const pickerLabel = (id: string) => labels.get(id) ?? id;

export function PickerInput({ field, value, onChange, readOnly, inputStyle, formId, caseId, groupKey }: In & {
  field: FormFieldDef; formId?: string; caseId?: string; groupKey?: string | null;
}) {
  const kind = KIND[field.type];
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [options, setOptions] = useState<{ value: string; label: string }[]>([]);
  const [error, setError] = useState<string | null>(null);
  const minChars = kind === "document" ? 0 : 2;
  const unavailable = !formId ? "Works on a saved form" : kind === "document" && !caseId ? "Opens on a case" : null;

  useEffect(() => {
    if (!open || unavailable || q.trim().length < minChars) { setOptions([]); return; }
    let live = true;
    const t = setTimeout(async () => {
      try {
        const token = getAccessToken();
        const r = await fetch("/api/v1/forms/picker-options", {
          method: "POST", headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
          body: JSON.stringify({ form_id: formId, field_key: fieldKey(field), group_key: groupKey || undefined, q, case_id: caseId }),
        });
        const d = await r.json().catch(() => ({}));
        if (!live) return;
        if (!r.ok) { setError(typeof d.detail === "string" ? d.detail : "Could not search"); return; }
        const opts = Array.isArray(d.options) ? d.options : [];
        opts.forEach((o: any) => labels.set(o.value, o.label));
        setOptions(opts); setError(null);
      } catch { if (live) setError("Could not search"); }
    }, 250);
    return () => { live = false; clearTimeout(t); };
  }, [q, open, formId, caseId, groupKey, unavailable, minChars, field]);

  const shown = value ? pickerLabel(value) : "";
  if (readOnly) return <input value={shown} disabled style={inputStyle} />;
  return (
    <div style={{ position: "relative" }}>
      <input role="combobox" aria-expanded={open} aria-label={field.label} disabled={!!unavailable}
        value={open ? q : shown} placeholder={unavailable ?? field.placeholder ?? (kind === "user" ? "Type a name…" : kind === "case" ? "Type a case number…" : "Choose a document…")}
        onFocus={() => { setOpen(true); setQ(""); }} onBlur={() => setTimeout(() => setOpen(false), 150)}
        onChange={e => setQ(e.target.value)} style={inputStyle} />
      {value && !open && <button type="button" aria-label="Clear" onClick={() => onChange(undefined)}
        style={{ position: "absolute", right: 6, top: 6, background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)" }}>×</button>}
      {open && !unavailable && (
        <div role="listbox" style={{ position: "absolute", zIndex: 20, left: 0, right: 0, top: "100%", marginTop: 2, maxHeight: 240, overflow: "auto",
          background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", boxShadow: "0 6px 20px rgba(0,0,0,.15)" }}>
          {error && <div style={{ padding: "8px 12px", fontSize: 12, color: "var(--status-failed)" }}>{error}</div>}
          {!error && q.trim().length < minChars && <div style={{ padding: "8px 12px", fontSize: 12, color: "var(--text-muted)" }}>Type at least {minChars} characters</div>}
          {!error && q.trim().length >= minChars && options.length === 0 && <div style={{ padding: "8px 12px", fontSize: 12, color: "var(--text-muted)" }}>No match</div>}
          {options.map(o => (
            <div key={o.value} role="option" aria-selected={o.value === value}
              onMouseDown={e => { e.preventDefault(); onChange(o.value); setOpen(false); }}
              style={{ padding: "7px 12px", fontSize: 13, cursor: "pointer", color: "var(--text-primary)",
                background: o.value === value ? "var(--accent-dim)" : "transparent" }}>{o.label}</div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ── Rich text: a small Markdown subset, drawn as React elements ─────
   **bold**, *italic*, [text](https://…), "- " bullets, "1. " numbers,
   blank line = new paragraph. Nothing is ever inserted as HTML, so a
   value cannot carry a script; a link is followed only for https / mailto. */

function inline(text: string, key: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  const re = /\*\*([^*]+)\*\*|\*([^*]+)\*|\[([^\]]{1,200})\]\(([^)\s]{1,500})\)/g;
  let last = 0, m: RegExpExecArray | null, i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1] !== undefined) out.push(<strong key={`${key}b${i}`}>{m[1]}</strong>);
    else if (m[2] !== undefined) out.push(<em key={`${key}i${i}`}>{m[2]}</em>);
    else {
      const href = m[4];
      out.push(/^(https:\/\/|mailto:)/i.test(href)
        ? <a key={`${key}a${i}`} href={href} target="_blank" rel="noopener noreferrer nofollow">{m[3]}</a>
        : `${m[3]} (${href})`);
    }
    last = re.lastIndex; i++;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function RichText({ text }: { text: string }) {
  const blocks = (text || "").replace(/\r\n/g, "\n").split(/\n{2,}/);
  return (
    <div style={{ fontSize: 13, lineHeight: 1.55, color: "var(--text-primary)" }}>
      {blocks.map((b, bi) => {
        const lines = b.split("\n");
        if (lines.every(l => /^\s*[-*] /.test(l))) return <ul key={bi} style={{ margin: "4px 0", paddingLeft: 20 }}>{lines.map((l, li) => <li key={li}>{inline(l.replace(/^\s*[-*] /, ""), `${bi}.${li}`)}</li>)}</ul>;
        if (lines.every(l => /^\s*\d+\. /.test(l))) return <ol key={bi} style={{ margin: "4px 0", paddingLeft: 20 }}>{lines.map((l, li) => <li key={li}>{inline(l.replace(/^\s*\d+\. /, ""), `${bi}.${li}`)}</li>)}</ol>;
        return <p key={bi} style={{ margin: "4px 0" }}>{lines.flatMap((l, li) => [...(li ? [<br key={`br${li}`} />] : []), ...inline(l, `${bi}.${li}`)])}</p>;
      })}
    </div>
  );
}

export function RichTextInput({ value, onChange, readOnly, inputStyle, placeholder, maxLength }: In & { placeholder?: string; maxLength?: number }) {
  const [preview, setPreview] = useState(false);
  const ta = useRef<HTMLTextAreaElement>(null);
  const text: string = typeof value === "string" ? value : "";
  if (readOnly) return <div style={{ ...inputStyle, minHeight: 40 }}><RichText text={text} /></div>;
  const wrap = (before: string, after = before, sample = "text") => {
    const el = ta.current; if (!el) return;
    const [s, e] = [el.selectionStart, el.selectionEnd];
    const picked = text.slice(s, e) || sample;
    onChange(text.slice(0, s) + before + picked + after + text.slice(e) || undefined);
  };
  const prefix = (p: string) => {
    const el = ta.current; if (!el) return;
    const s = text.lastIndexOf("\n", el.selectionStart - 1) + 1;
    onChange(text.slice(0, s) + p + text.slice(s));
  };
  return (
    <div>
      <div role="toolbar" aria-label="Formatting" style={{ display: "flex", gap: 4, marginBottom: 4 }}>
        <button type="button" onClick={() => wrap("**")} style={smallBtn} aria-label="Bold"><b>B</b></button>
        <button type="button" onClick={() => wrap("*")} style={smallBtn} aria-label="Italic"><i>I</i></button>
        <button type="button" onClick={() => prefix("- ")} style={smallBtn} aria-label="Bullet list">•</button>
        <button type="button" onClick={() => prefix("1. ")} style={smallBtn} aria-label="Numbered list">1.</button>
        <button type="button" onClick={() => wrap("[", "](https://)", "link text")} style={smallBtn} aria-label="Link">🔗</button>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={() => setPreview(p => !p)} style={smallBtn}>{preview ? "Edit" : "Preview"}</button>
      </div>
      {preview
        ? <div style={{ ...inputStyle, minHeight: 80 }}><RichText text={text} /></div>
        : <textarea ref={ta} value={text} rows={5} placeholder={placeholder} maxLength={maxLength}
            onChange={e => onChange(e.target.value || undefined)} style={{ ...inputStyle, resize: "vertical" }} />}
    </div>
  );
}

/* ── Button ───────────────────────────────────────────────────────── */

/** What a button may reach in its form: values, sections, lookups. */
export type FormActions = {
  sections: FormSectionDef[];
  setValues: (fn: (prev: Record<string, any>) => Record<string, any>) => void;
  goToSection?: (sectionId: string) => void;
  runLookup?: (fieldKey: string) => void;
};
export const FormActionsContext = createContext<FormActions | null>(null);
/** Connector lookups register here so a button can run one (field key → run). */
export const LookupRegistry = createContext<Map<string, () => void> | null>(null);

export const BUTTON_ACTIONS = ["set", "clear", "run_lookup", "open_link", "go_to_section"] as const;

/** An https address, or null. */
export const safeLink = (url: unknown): string | null => {
  if (typeof url !== "string") return null;
  try { const u = new URL(url); return u.protocol === "https:" && !u.username && !u.password ? u.href : null; } catch { return null; }
};

export function ButtonBlock({ field, readOnly }: { field: FormFieldDef; readOnly?: boolean }) {
  const ctx = useContext(FormActionsContext);
  const lookups = useContext(LookupRegistry);
  const f = field as any;
  const actions: any[] = Array.isArray(f.actions) ? f.actions.slice(0, 10) : [];
  const run = () => {
    for (const a of actions) {
      if (!a || typeof a !== "object") continue;
      if (a.action === "open_link") { const href = safeLink(a.url); if (href) window.open(href, "_blank", "noopener,noreferrer"); continue; }
      if (a.action === "go_to_section" && typeof a.section === "string") { ctx?.goToSection?.(a.section); continue; }
      if (a.action === "run_lookup" && typeof a.target === "string") { (lookups?.get(a.target) ?? (() => ctx?.runLookup?.(a.target)))(); continue; }
      if (!ctx || typeof a.target !== "string" || !a.target) continue;
      ctx.setValues(prev => {
        const st = formState(ctx.sections, prev);
        if (st.locked(a.target)) return prev;          // never a calculated or read-only field
        const target = ctx.sections.flatMap(s => s.fields).find(x => fieldKey(x) === a.target);
        if (!target || target.type === "consent") return prev;   // only the person agrees
        if (a.action === "clear") { const n = { ...prev }; delete n[a.target]; return n; }
        if (a.action === "set" && typeof a.expression === "string") {
          const v = evaluate(a.expression, k => st.reads(k, true), localToday());
          const n = { ...prev };
          if (v === null || v === undefined) delete n[a.target]; else n[a.target] = v;
          return n;
        }
        return prev;
      });
    }
  };
  const primary = f.style !== "secondary";
  return (
    <button type="button" disabled={readOnly || !actions.length} onClick={run} style={{
      padding: "7px 14px", fontSize: 13, fontWeight: 600, borderRadius: "var(--radius-sm)", cursor: readOnly ? "default" : "pointer",
      border: primary ? "none" : "1px solid var(--border-default)",
      background: primary ? "var(--accent)" : "var(--bg-surface)", color: primary ? "#fff" : "var(--text-primary)",
    }}>{field.label || "Button"}</button>
  );
}

const smallBtn: React.CSSProperties = {
  padding: "3px 8px", fontSize: 12, borderRadius: 6, cursor: "pointer",
  border: "1px solid var(--border-default)", background: "var(--bg-surface)", color: "var(--text-primary)",
};
