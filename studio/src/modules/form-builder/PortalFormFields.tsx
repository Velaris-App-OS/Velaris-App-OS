import React, { useEffect, useRef, useState } from "react";
import type { FormSectionDef, FormSettings } from "./FormBuilder";
import { applyOnChange, FieldRenderer } from "./FormRenderer";
import { fieldKey, formState } from "./validate";
import { RefFetcherContext, type RefFetcher } from "./refOptions";
import { EmbedOriginsContext } from "./Embed";
import { FormActionsContext } from "./BatchM";

/* ═══════════════════════════════════════════════════════════════════
   PortalFormFields — a case type's Form Builder form inside the customer
   portal's request form (Form Builder redesign part 10, 2026-10-28).

   The portal used to draw a flat list as text boxes (dropdowns included).
   This draws the same fields Studio does, with the same renderer, rules
   and widths. The portal owns Submit; the server checks the values with
   the shared validator. Steps are not used here — the form sits inside the
   portal's own page. What a customer cannot use (lookups, uploads) the
   server never sends (velaris_shared/forms/portal.py).
   ═══════════════════════════════════════════════════════════════════ */

const SPAN: Record<string, number> = { full: 6, half: 3, third: 2 };

export default function PortalFormFields({ definition, values, onChange, errors, fetcher }: {
  definition: { sections: FormSectionDef[]; settings?: FormSettings };
  /** Loads a reference field's options through the portal (v2 step 3). */
  fetcher?: RefFetcher;
  values: Record<string, any>;
  onChange: (key: string, value: any) => void;
  errors: Record<string, string>;
}) {
  const [narrow, setNarrow] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([e]) => setNarrow(e.contentRect.width < 520));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const cols = definition.settings?.columns;
  // The same resolver as Studio and the server (Form Builder v2): shown,
  // read-only now, required now, calculated values, on-change actions.
  const st = formState(definition.sections, values);
  const view: Record<string, any> = { ...values };
  for (const f of definition.sections.flatMap(sec => sec.fields)) {
    if (typeof (f as any).calculation === "string" && (f as any).calculation.trim()) view[fieldKey(f)] = st.value(fieldKey(f));
  }
  const change = (key: string, v: any, also?: Record<string, any>) => {
    const next = applyOnChange(definition.sections, key, { ...values, ...(also ?? {}), [key]: v });
    for (const k of new Set([...Object.keys(next), ...Object.keys(values)])) {
      if (k === key || next[k] !== values[k]) onChange(k, next[k]);
    }
  };
  return (
    <RefFetcherContext.Provider value={fetcher ?? null}>
    {/* The portal's approved embed sites come with the form; never read from the signed-in API. */}
    <EmbedOriginsContext.Provider value={Array.isArray((definition as any).embed_origins) ? (definition as any).embed_origins : []}>
    {/* A button sets or clears fields here too; the portal has no lookups. */}
    <FormActionsContext.Provider value={{ sections: definition.sections, setValues: fn => {
      const next = fn(values);
      for (const k of new Set([...Object.keys(next), ...Object.keys(values)])) if (next[k] !== values[k]) onChange(k, next[k]);
    } }}>
    <div ref={ref}>
      {definition.sections.map(section => (
        <div key={section.id} style={{ marginBottom: 16 }}>
          {definition.sections.length > 1 && (
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: section.description ? 2 : 8 }}>{section.title}</div>
          )}
          {section.description && <div style={{ fontSize: 12, color: "#6b7280", marginBottom: 8 }}>{section.description}</div>}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(6, minmax(0, 1fr))", gap: 14 }}>
            {section.fields.filter(f => st.shown(fieldKey(f))).map(f => (
              <div key={f.id || fieldKey(f)} style={{ gridColumn: `span ${narrow ? 6 : f.width ? SPAN[f.width] ?? 6 : 6 / (cols && [1, 2, 3].includes(cols) ? cols : 1)}`, minWidth: 0 }}>
                <FieldRenderer field={f} value={view[fieldKey(f)]} error={errors[fieldKey(f)]}
                  onChange={(v, also) => change(fieldKey(f), v, also)} readOnly={st.locked(fieldKey(f))}
                  requiredNow={st.required(fieldKey(f))} allValues={view} errors={errors}
                  rowStates={st.rowStates[fieldKey(f)]} preview />
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
    </FormActionsContext.Provider>
    </EmbedOriginsContext.Provider>
    </RefFetcherContext.Provider>
  );
}
