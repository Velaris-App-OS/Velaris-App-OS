/**
 * View Form — the form as the person filling it in sees it, rendered by the
 * same FormRenderer that Case Manager and Work Center use. Works on what is in
 * the editor (unsaved changes included), at desktop, tablet and phone widths.
 * Submitting runs the real validation and shows the values it would send;
 * nothing is saved.
 */
import React, { useEffect, useMemo, useState } from "react";
import FormRenderer from "./FormRenderer";
import type { FormDefinition } from "./FormBuilder";

type Device = "desktop" | "tablet" | "phone";

const WIDTHS: Record<Device, number | "100%"> = { desktop: "100%", tablet: 768, phone: 390 };
const LABELS: Record<Device, string> = { desktop: "Desktop", tablet: "Tablet", phone: "Phone" };

export default function FormPreview({ name, definition, unsaved, onClose, formId, recording, onSaveTest }: {
  name: string;
  /** Opened to record a test scenario: the answers are saved as one (§5 step 7). */
  recording?: boolean;
  onSaveTest?: (values: Record<string, any>) => string;
  /** The saved form — a lookup field runs only what the saved form says. */
  formId?: string;
  definition: FormDefinition;
  unsaved: boolean;
  onClose: () => void;
}) {
  const [device, setDevice] = useState<Device>("desktop");
  const [submitted, setSubmitted] = useState<Record<string, any> | null>(null);
  const [run, setRun] = useState(0);              // remounts the form on "Fill in again"

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // A field's default value is what the person sees first (display fields
  // carry their sample data this way).
  const initialValues = useMemo(() => {
    const v: Record<string, any> = {};
    for (const s of definition.sections || []) {
      for (const f of s.fields || []) {
        if (f.default_value !== undefined) v[f.field_key || f.id] = f.default_value;
      }
    }
    return v;
  }, [definition]);

  const sections = [...(definition.sections || [])].sort((a, b) => a.order - b.order);
  const fieldCount = sections.reduce((n, s) => n + (s.fields?.length || 0), 0);

  return (
    <div role="dialog" aria-modal="true" aria-label={`Preview of ${name}`} style={{
      position: "fixed", inset: 0, zIndex: 1000, display: "flex", flexDirection: "column",
      background: "var(--bg-root)",
    }}>
      {/* Bar */}
      <div style={{
        display: "flex", alignItems: "center", gap: 12, padding: "10px 20px",
        borderBottom: "1px solid var(--border)", background: "var(--bg-surface)", flexWrap: "wrap",
      }}>
        <div style={{ flex: 1, minWidth: 200 }}>
          <div style={{ fontWeight: 700, fontSize: 14, color: "var(--text-primary)" }}>
            View Form — {name}
          </div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 1 }}>
            As the person filling it in sees it · {sections.length} section{sections.length !== 1 ? "s" : ""}, {fieldCount} field{fieldCount !== 1 ? "s" : ""}
            {unsaved && <span style={{ color: "var(--status-warning, #ca8a04)", marginLeft: 6 }}>· includes unsaved changes</span>}
            {" "}· nothing you submit here is saved
          </div>
        </div>
        <div role="group" aria-label="Screen width" style={{
          display: "flex", background: "var(--bg-root)", borderRadius: 6,
          border: "1px solid var(--border)", overflow: "hidden",
        }}>
          {(Object.keys(WIDTHS) as Device[]).map(d => (
            <button key={d} onClick={() => setDevice(d)} aria-pressed={device === d} style={{
              padding: "5px 14px", fontSize: 12, fontWeight: device === d ? 600 : 400, border: "none",
              cursor: "pointer", background: device === d ? "var(--accent)" : "transparent",
              color: device === d ? "#fff" : "var(--text-secondary)",
            }}>{LABELS[d]}</button>
          ))}
        </div>
        <button onClick={onClose} style={{
          padding: "6px 16px", fontSize: 13, fontWeight: 600, background: "var(--bg-surface)",
          color: "var(--text-primary)", border: "1px solid var(--border)", borderRadius: 6, cursor: "pointer",
        }}>Close</button>
      </div>

      {/* Canvas */}
      <div style={{ flex: 1, overflow: "auto", padding: "24px 16px", background: "var(--bg-base, var(--bg-root))" }}>
        <div style={{ width: WIDTHS[device], maxWidth: "100%", margin: "0 auto", transition: "width 0.2s" }}>
          <div style={{ maxWidth: device === "desktop" ? 760 : undefined, margin: "0 auto" }}>
            {fieldCount === 0 ? (
              <div style={{ padding: 40, textAlign: "center", color: "var(--text-muted)", fontSize: 13 }}>
                This form has no fields yet — add some in Build.
              </div>
            ) : submitted ? (
              <div style={{
                background: "var(--bg-card)", border: "1px solid var(--border-subtle)",
                borderRadius: "var(--radius-md)", padding: 20,
              }}>
                <div style={{ fontWeight: 700, fontSize: 15, color: "var(--status-completed, #16a34a)", marginBottom: 6 }}>
                  ✓ The form passed validation
                </div>
                <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
                  In a case these values would be submitted. This is a preview — nothing was saved.
                </div>
                <pre style={{
                  margin: 0, padding: 12, fontSize: 11, fontFamily: "var(--font-mono)", whiteSpace: "pre-wrap",
                  background: "var(--bg-elevated)", borderRadius: 6, maxHeight: 360, overflow: "auto",
                  color: "var(--text-primary)",
                }}>{JSON.stringify(submitted, null, 2)}</pre>
                <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
                  <button onClick={() => { setSubmitted(null); setRun(r => r + 1); }} style={{
                    padding: "6px 16px", fontSize: 13, fontWeight: 600, background: "var(--accent)",
                    color: "#fff", border: "none", borderRadius: 6, cursor: "pointer",
                  }}>Fill in again</button>
                  <button onClick={onClose} style={{
                    padding: "6px 16px", fontSize: 13, background: "var(--bg-surface)", color: "var(--text-primary)",
                    border: "1px solid var(--border)", borderRadius: 6, cursor: "pointer",
                  }}>Close</button>
                </div>
              </div>
            ) : (
              <FormRenderer
                key={run}
                formName={name}
                definition={{ ...definition, sections }}
                initialValues={initialValues}
                onSubmit={async (values) => { setSubmitted(values); }}
                onCancel={onClose}
                formId={formId}
                secondaryAction={onSaveTest ? { label: "Save as test scenario", run: onSaveTest } : undefined}
                notice={recording ? "Fill in the answers to test — valid or not — then Save as test scenario. The scenario expects what the form does now; you can change that." : undefined}
              />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
