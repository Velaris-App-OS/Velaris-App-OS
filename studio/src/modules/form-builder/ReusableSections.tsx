/**
 * Reusable form sections (Form Builder §5 step 6, 2026-10-29).
 *
 * A section built once ("Address", "Bank details") and linked from many forms.
 * The server copies its fields into each form and, when the section changes,
 * into every form that links it — or into none, naming the form that would
 * break. Here: the list to insert from, the editor, and "save as reusable".
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useEffect, useState } from "react";
import {
  createFormSection, getFormSection, listFormSections, updateFormSection,
  type FormSectionBody, type ReusableSection,
} from "@shared/api/client";
import FormBuilder, { type FormDefinition, type FormSectionDef } from "./FormBuilder";

/** The palette's list: click to add a linked copy to the form. */
export function ReusableSectionsPanel({ onInsert, refreshKey }: { onInsert: (s: ReusableSection) => void; refreshKey?: number }) {
  const [items, setItems] = useState<ReusableSection[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    listFormSections().then(r => { setItems(r.items); setError(null); }, e => setError(e?.message || "Could not load sections"));
  }, [open, refreshKey]);
  return (
    <div role="group" aria-label="Reusable sections" style={{ marginBottom: 6 }}>
      <button type="button" onClick={() => setOpen(o => !o)} aria-expanded={open} style={{
        display: "flex", alignItems: "center", gap: 6, width: "100%", background: "none", border: "none",
        padding: "4px 0", cursor: "pointer", textAlign: "left", fontSize: 10, fontWeight: 600, color: "var(--text-muted)",
      }}>
        <span aria-hidden style={{ display: "inline-block", width: 8, transform: open ? "rotate(90deg)" : "none", transition: "transform 0.12s" }}>›</span>
        <span style={{ flex: 1 }}>Reusable sections</span>
      </button>
      {open && (
        <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 2 }}>
          {error && <div style={{ fontSize: 11, color: "var(--status-failed)" }}>{error}</div>}
          {items && items.length === 0 && (
            <div style={{ fontSize: 11, color: "var(--text-muted)" }}>None yet — open a section's ⚙ and choose "Save as reusable section".</div>
          )}
          {items?.map(s => (
            <button key={s.id} type="button" onClick={() => onInsert(s)} title={s.description || undefined} style={{
              display: "flex", justifyContent: "space-between", gap: 6, padding: "6px 8px", fontSize: 11, cursor: "pointer",
              background: "var(--bg-card)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)", color: "var(--text-secondary)",
            }}>
              <span>🔗 {s.name}</span>
              <span style={{ color: "var(--text-muted)" }}>{s.section.fields?.length ?? 0} fields · v{s.version}{s.platform ? " · platform" : ""}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** A linked copy of a reusable section, ready to put in a form. */
export function linkedSection(s: ReusableSection, order: number): FormSectionDef {
  return {
    id: `section-${Date.now()}`, title: s.section.title || s.name, order,
    fields: JSON.parse(JSON.stringify(s.section.fields || [])),
    ...(s.section.description ? { description: s.section.description } : {}),
    section_ref: { id: s.id, version: s.version },
  };
}

/** The body of a section as a reusable one holds it. */
export function sectionBody(section: FormSectionDef): FormSectionBody {
  return { title: section.title, ...(section.description ? { description: section.description } : {}), fields: section.fields };
}

/** Edit a reusable section in the same builder; saving updates every form that links it. */
export function ReusableSectionEditor({ sectionId, onClose, onSaved }: {
  sectionId: string; onClose: () => void; onSaved: (s: ReusableSection) => void;
}) {
  const [loaded, setLoaded] = useState<ReusableSection | null>(null);
  const [name, setName] = useState("");
  const [def, setDef] = useState<FormDefinition | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "saving">("loading");
  const [message, setMessage] = useState<{ text: string; ok: boolean } | null>(null);

  useEffect(() => {
    getFormSection(sectionId).then(s => {
      setLoaded(s); setName(s.name);
      setDef({ sections: [{ id: "reusable", title: s.section.title || s.name, order: 0, fields: s.section.fields || [],
        ...(s.section.description ? { description: s.section.description } : {}) }] });
      setState("ready");
    }, e => { setMessage({ text: e?.message || "Could not load the section", ok: false }); setState("ready"); });
  }, [sectionId]);

  const save = async () => {
    if (!def || !loaded) return;
    setState("saving"); setMessage(null);
    try {
      const s = await updateFormSection(loaded.id, { name: name.trim(), description: loaded.description, section: sectionBody(def.sections[0]) });
      const forms = s.updated_forms ?? [];
      setMessage({ text: `Saved as version ${s.version}.${forms.length ? ` Updated form(s): ${forms.join(", ")}.` : ""}`, ok: true });
      setLoaded(s);
      onSaved(s);
    } catch (e: any) {
      setMessage({ text: e?.message || "Not saved", ok: false });
    } finally {
      setState("ready");
    }
  };

  return (
    <div role="dialog" aria-label="Edit reusable section" onClick={onClose} style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,.45)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center",
    }}>
      <div onClick={e => e.stopPropagation()} style={{
        width: "min(1100px, calc(100vw - 32px))", height: "min(820px, calc(100vh - 32px))", display: "flex", flexDirection: "column",
        background: "var(--bg-panel)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)", overflow: "hidden",
      }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", padding: "10px 14px", borderBottom: "1px solid var(--border-subtle)" }}>
          <span style={{ fontSize: 13, fontWeight: 600 }}>🔗 Reusable section</span>
          <input aria-label="Section name" value={name} onChange={e => setName(e.target.value)} maxLength={100}
            style={{ flex: 1, padding: "5px 8px", fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)",
              border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)" }} />
          {loaded && <span style={{ fontSize: 11, color: "var(--text-muted)" }}>v{loaded.version}{loaded.used_by?.length ? ` · linked by ${loaded.used_by.length} form(s)` : ""}</span>}
          <button type="button" onClick={save} disabled={state !== "ready" || !name.trim()} style={{
            padding: "6px 14px", fontSize: 12, fontWeight: 600, background: "var(--accent)", color: "#fff", border: "none", borderRadius: 6, cursor: "pointer",
          }}>{state === "saving" ? "Saving…" : "Save everywhere"}</button>
          <button type="button" onClick={onClose} aria-label="Close" style={{ background: "none", border: "none", fontSize: 18, cursor: "pointer", color: "var(--text-muted)" }}>×</button>
        </div>
        {message && (
          <div role="status" style={{ padding: "6px 14px", fontSize: 12, color: message.ok ? "var(--status-success)" : "var(--status-failed)" }}>{message.text}</div>
        )}
        <div style={{ flex: 1, overflow: "auto", padding: 12 }}>
          {def ? <FormBuilder definition={def} onChange={d => setDef({ sections: d.sections.slice(0, 1).map(s => ({ ...s, section_ref: undefined })) })} />
            : <div style={{ fontSize: 12, color: "var(--text-muted)" }}>Loading…</div>}
        </div>
      </div>
    </div>
  );
}

/** "Save as reusable section": the section becomes a linked copy of the new one. */
export async function saveAsReusable(section: FormSectionDef, name: string): Promise<ReusableSection> {
  return createFormSection({ name: name.trim(), section: sectionBody(section) });
}
