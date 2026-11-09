/**
 * The small controls (Form Builder, 2026-10-29): toggle, time, date range,
 * percentage, tags, country / currency pickers, IBAN, consent, and the static
 * blocks — heading, paragraph, divider, callout. The rules they follow are in
 * validate.ts / validate.py; these only draw them.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useState } from "react";
import type { FormFieldDef } from "./FormBuilder";
import { COUNTRIES, CURRENCIES, ibanCompact } from "./validate";
import { EmbedBlock } from "./Embed";

type In = { value: any; onChange: (v: any) => void; readOnly?: boolean; inputStyle: React.CSSProperties };

export function Toggle({ value, onChange, readOnly, label }: Omit<In, "inputStyle"> & { label: string }) {
  const on = value === true;
  return (
    <label style={{ display: "inline-flex", alignItems: "center", gap: 8, cursor: readOnly ? "default" : "pointer" }}>
      <button type="button" role="switch" aria-checked={on} aria-label={label} disabled={readOnly}
        onClick={() => onChange(!on)}
        style={{
          width: 36, height: 20, borderRadius: 10, border: "none", padding: 2, position: "relative",
          background: on ? "var(--accent)" : "var(--border-default)", cursor: readOnly ? "default" : "pointer",
          transition: "background 0.15s",
        }}>
        <span style={{
          display: "block", width: 16, height: 16, borderRadius: "50%", background: "#fff",
          transform: `translateX(${on ? 16 : 0}px)`, transition: "transform 0.15s",
        }} />
      </button>
      <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>{on ? "Yes" : value === false ? "No" : "—"}</span>
    </label>
  );
}

export function DateRange({ value, onChange, readOnly, inputStyle, min, max }: In & { min?: string; max?: string }) {
  const r = value && typeof value === "object" ? value : {};
  const set = (k: "start" | "end", d: string) => {
    const next = { ...r, [k]: d || undefined };
    onChange(next.start || next.end ? next : undefined);
  };
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      <input type="date" aria-label="From" value={r.start ?? ""} min={min} max={r.end || max} disabled={readOnly}
        onChange={e => set("start", e.target.value)} style={{ ...inputStyle, flex: "1 1 140px", width: "auto" }} />
      <span style={{ fontSize: 12, color: "var(--text-muted)" }}>to</span>
      <input type="date" aria-label="To" value={r.end ?? ""} min={r.start || min} max={max} disabled={readOnly}
        onChange={e => set("end", e.target.value)} style={{ ...inputStyle, flex: "1 1 140px", width: "auto" }} />
    </div>
  );
}

/** Free words as chips: Enter or comma adds one, Backspace on an empty box removes the last. */
export function Tags({ value, onChange, readOnly, inputStyle, placeholder }: In & { placeholder?: string }) {
  const tags: string[] = Array.isArray(value) ? value : [];
  const [draft, setDraft] = useState("");
  const add = () => {
    const t = draft.trim();
    setDraft("");
    if (!t || tags.some(x => x.toLowerCase() === t.toLowerCase())) return;
    onChange([...tags, t]);
  };
  const remove = (i: number) => {
    const next = tags.filter((_, j) => j !== i);
    onChange(next.length ? next : undefined);
  };
  return (
    <div style={{ ...inputStyle, display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center", padding: "4px 6px" }}>
      {tags.map((t, i) => (
        <span key={t} style={{ display: "inline-flex", alignItems: "center", gap: 4, padding: "2px 8px", borderRadius: 10,
          background: "var(--bg-elevated)", border: "1px solid var(--border-subtle)", fontSize: 12 }}>
          {t}
          {!readOnly && <button type="button" aria-label={`Remove ${t}`} onClick={() => remove(i)}
            style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", padding: 0, fontSize: 13 }}>×</button>}
        </span>
      ))}
      {!readOnly && (
        <input aria-label="Add a tag" value={draft} placeholder={tags.length ? "" : placeholder || "Type and press Enter"}
          onChange={e => setDraft(e.target.value.replace(",", ""))}
          onKeyDown={e => {
            if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(); }
            else if (e.key === "Backspace" && !draft && tags.length) remove(tags.length - 1);
          }}
          onBlur={add}
          style={{ flex: 1, minWidth: 80, border: "none", outline: "none", background: "transparent", color: "var(--text-primary)", fontSize: 13, padding: 4 }} />
      )}
    </div>
  );
}

const countryName = (() => {
  try { const n = new Intl.DisplayNames(undefined, { type: "region" }); return (c: string) => n.of(c) ?? c; }
  catch { return (c: string) => c; }
})();

/** Built-in lists: names in the person's language where the browser knows them. */
export function codeOptions(type: "country" | "currency_code"): { value: string; label: string }[] {
  return type === "country"
    ? COUNTRIES.map(c => ({ value: c.code, label: countryName(c.code) || c.name })).sort((a, b) => a.label.localeCompare(b.label))
    : CURRENCIES.map(c => ({ value: c.code, label: `${c.code} — ${c.name}` }));
}

/** The text a stored value reads as (review step, read-only places). */
export function smallText(f: FormFieldDef, v: any): string | null {
  switch (f.type) {
    case "date_range": return v && typeof v === "object" ? `${v.start ?? "?"} – ${v.end ?? "?"}` : null;
    case "country": return typeof v === "string" ? countryName(v) || v : null;
    case "percentage": return v !== undefined && v !== null && v !== "" ? `${v} %` : null;
    case "iban": return typeof v === "string" ? ibanGroups(v) : null;
    case "consent": return v === true ? "Agreed" : v === false ? "Not agreed" : null;
    default: return null;
  }
}

/** GB82 WEST 1234 … — how an IBAN is printed. */
export const ibanGroups = (v: string) => ibanCompact(v).replace(/(.{4})(?=.)/g, "$1 ");

export function Iban({ value, onChange, readOnly, inputStyle, placeholder }: In & { placeholder?: string }) {
  return (
    <input value={typeof value === "string" ? ibanGroups(value) : ""} disabled={readOnly} spellCheck={false} autoComplete="off"
      placeholder={placeholder || "GB82 WEST 1234 5698 7654 32"}
      onChange={e => { const c = ibanCompact(e.target.value); onChange(c || undefined); }}
      style={{ ...inputStyle, fontFamily: "var(--font-mono)", letterSpacing: 0.5 }} />
  );
}

export function Consent({ field, value, onChange, readOnly }: Omit<In, "inputStyle"> & { field: FormFieldDef }) {
  const terms = (field as any).terms as string | undefined;
  return (
    <div>
      {terms && (
        <div tabIndex={0} aria-label="Terms" style={{
          maxHeight: 160, overflow: "auto", padding: "8px 10px", marginBottom: 8, fontSize: 12, lineHeight: 1.5,
          whiteSpace: "pre-wrap", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)",
          background: "var(--bg-elevated)", color: "var(--text-secondary)",
        }}>{terms}</div>
      )}
      <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: readOnly ? "default" : "pointer" }}>
        <input type="checkbox" checked={value === true} disabled={readOnly} onChange={e => onChange(e.target.checked)}
          style={{ accentColor: "var(--accent)", width: 16, height: 16 }} />
        <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>{field.placeholder || "I have read and agree"}</span>
      </label>
    </div>
  );
}

const TONES: Record<string, { color: string; icon: string }> = {
  info:    { color: "var(--accent)",          icon: "ℹ" },
  success: { color: "var(--status-success)",  icon: "✓" },
  warning: { color: "var(--status-warning)",  icon: "!" },
  error:   { color: "var(--status-failed)",   icon: "✕" },
};

/** Heading, paragraph, divider, callout — the designer's own words, drawn as plain text —
 *  and an embed from an approved site. */
export function StaticBlock({ field }: { field: FormFieldDef }) {
  const text = ((field as any).text as string | undefined) ?? "";
  switch (field.type) {
    case "embed":
      return <EmbedBlock field={field} />;
    case "heading":
      return <div role="heading" aria-level={3} style={{ fontSize: 15, fontWeight: 600, color: "var(--text-primary)", marginTop: 4 }}>{field.label}</div>;
    case "paragraph":
      return <p style={{ margin: 0, fontSize: 13, lineHeight: 1.5, color: "var(--text-secondary)", whiteSpace: "pre-wrap" }}>{text}</p>;
    case "divider":
      return <hr style={{ border: "none", borderTop: "1px solid var(--border-subtle)", margin: "4px 0" }} />;
    case "callout": {
      const t = TONES[(field as any).tone] ?? TONES.info;
      return (
        <div role="note" style={{ display: "flex", gap: 8, padding: "8px 12px", borderRadius: "var(--radius-sm)",
          background: `color-mix(in srgb, ${t.color} 8%, transparent)`, borderLeft: `3px solid ${t.color}`,
          fontSize: 13, color: "var(--text-primary)" }}>
          <span aria-hidden style={{ color: t.color, fontWeight: 700 }}>{t.icon}</span>
          <div style={{ whiteSpace: "pre-wrap" }}>
            {field.label && <div style={{ fontWeight: 600, marginBottom: text ? 2 : 0 }}>{field.label}</div>}
            {text}
          </div>
        </div>
      );
    }
    default:
      return null;
  }
}
