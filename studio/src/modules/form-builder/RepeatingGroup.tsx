import React, { useEffect, useRef, useState } from "react";
import type { FormFieldDef, FormSectionDef } from "./FormBuilder";
import { applyOnChange, FieldRenderer } from "./FormRenderer";
import { fieldKey, formState, groupFields, ROWS_HARD_MAX, type FormState } from "./validate";
import { GroupKeyContext } from "./refOptions";

/* ═══════════════════════════════════════════════════════════════════
   RepeatingGroup — line items, beneficiaries, vehicles (Form Builder v2,
   2026-10-29). One set of fields per row, stored as a list of rows under
   the group's key. Each row is a small form: its calculations, show /
   required / read-only rules and checks see the row first, then the rest
   of the form, and the server checks every row again
   (velaris_shared/forms/validate.py, FormState._rows).

   A table on a wide screen, a card per row on a phone or when there are
   more columns than fit.
   ═══════════════════════════════════════════════════════════════════ */

type Row = Record<string, any>;

export function newRow(children: FormFieldDef[]): Row {
  const row: Row = {};
  for (const c of children) if (c.default_value !== undefined && c.default_value !== null) row[fieldKey(c)] = c.default_value;
  return row;
}

export default function RepeatingGroup({ field, value, onChange, readOnly, errors = {}, states, preview, caseId, formId, outer }: {
  /** The rest of the form's values — a cell's reference filters may name them. */
  outer?: Record<string, any>;
  field: FormFieldDef;
  value: any;
  onChange: (rows: Row[]) => void;
  readOnly: boolean;
  /** Every error of the form; this group's are `key[i].child`. */
  errors?: Record<string, string>;
  /** Each row's state from the form's resolver; computed here when absent (the canvas). */
  states?: FormState[];
  preview?: boolean;
  caseId?: string;
  formId?: string;
}) {
  const key = fieldKey(field);
  const children = groupFields(field);
  const rows: Row[] = Array.isArray(value) ? value : [];
  // The builder's canvas (read-only preview) shows one sample row; the portal
  // passes `preview` only to leave out dictation, and edits as usual.
  const sample = !!preview && readOnly;
  const shownRows: Row[] = sample && !rows.length ? [newRow(children)] : rows;
  const max = Math.min(typeof field.max_rows === "number" ? field.max_rows : ROWS_HARD_MAX, ROWS_HARD_MAX);
  const sub = [{ id: "row", title: "", order: 0, fields: children }] as FormSectionDef[];
  const stateOf = (i: number): FormState => states?.[i] ?? formState(sub, shownRows[i] ?? {});

  const [narrow, setNarrow] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([e]) => setNarrow(e.contentRect.width < 140 * Math.max(children.length, 1) + 80));
    ro.observe(el);
    return () => ro.disconnect();
  }, [children.length]);

  const setCell = (i: number, child: string, v: any, also?: Record<string, any>) =>
    onChange(rows.map((r, j) => (j === i ? applyOnChange(sub, child, { ...r, ...(also ?? {}), [child]: v }) : r)));
  const move = (i: number, d: number) => {
    const next = [...rows];
    const [r] = next.splice(i, 1);
    next.splice(i + d, 0, r);
    onChange(next);
  };
  const canAdd = !readOnly && rows.length < max;
  const canEdit = !readOnly;

  const cell = (i: number, c: FormFieldDef, bare: boolean) => {
    const st = stateOf(i);
    const k = fieldKey(c);
    if (!st.shown(k)) return bare ? <span style={{ color: "var(--text-muted)", fontSize: 12 }}>—</span> : null;
    const calc = typeof c.calculation === "string" && c.calculation.trim();
    return (
      <FieldRenderer field={c} value={calc ? st.value(k) : shownRows[i]?.[k]} error={errors[`${key}[${i}].${k}`]}
        onChange={(v, also) => setCell(i, k, v, also)} readOnly={readOnly || st.locked(k)} requiredNow={st.required(k)}
        allValues={{ ...(outer ?? {}), ...(shownRows[i] ?? {}) }} preview={preview} caseId={caseId} formId={formId} bare={bare} />
    );
  };

  const actions = (i: number) => canEdit && (
    <div style={{ display: "flex", gap: 2, justifyContent: "flex-end" }}>
      <button type="button" aria-label={`Move row ${i + 1} up`} disabled={i === 0} onClick={() => move(i, -1)} style={icon}>↑</button>
      <button type="button" aria-label={`Move row ${i + 1} down`} disabled={i === rows.length - 1} onClick={() => move(i, 1)} style={icon}>↓</button>
      <button type="button" aria-label={`Copy row ${i + 1}`} disabled={rows.length >= max}
        onClick={() => onChange([...rows.slice(0, i + 1), { ...rows[i] }, ...rows.slice(i + 1)])} style={icon}>⧉</button>
      <button type="button" aria-label={`Remove row ${i + 1}`} onClick={() => onChange(rows.filter((_, j) => j !== i))}
        style={{ ...icon, color: "var(--status-failed)" }}>×</button>
    </div>
  );

  return (
    <GroupKeyContext.Provider value={key}>
    <div ref={ref} data-testid={`group-${key}`}>
      {shownRows.length === 0 && (
        <div style={{ fontSize: 12, color: "var(--text-muted)", fontStyle: "italic", padding: "6px 0" }}>No rows yet.</div>
      )}
      {shownRows.length > 0 && !narrow && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr>
                <th style={{ ...th, width: 28 }}>#</th>
                {children.map(c => (
                  <th key={fieldKey(c)} style={th}>
                    {c.label}{c.required && <span style={{ color: "var(--status-failed)" }}>*</span>}
                  </th>
                ))}
                {canEdit && <th style={{ ...th, width: 100 }} />}
              </tr>
            </thead>
            <tbody>
              {shownRows.map((_, i) => (
                <tr key={i} style={{ borderTop: "1px solid var(--border-subtle)", verticalAlign: "top" }}>
                  <td style={{ ...td, color: "var(--text-muted)" }}>{i + 1}</td>
                  {children.map(c => <td key={fieldKey(c)} style={td}>{cell(i, c, true)}</td>)}
                  {canEdit && <td style={td}>{actions(i)}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {shownRows.length > 0 && narrow && shownRows.map((_, i) => (
        <div key={i} style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)", padding: 10, marginBottom: 8 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
            <span style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)" }}>{field.label} {i + 1}</span>
            {actions(i)}
          </div>
          <div style={{ display: "grid", gap: 10 }}>
            {children.map(c => <div key={fieldKey(c)}>{cell(i, c, false)}</div>)}
          </div>
        </div>
      ))}
      {Object.entries(errors).filter(([k]) => k.startsWith(`${key}[`) && /^\w+\[\d+\]$/.test(k)).map(([k, m]) => (
        <div key={k} style={{ fontSize: 11, color: "var(--status-failed)" }}>{m}</div>
      ))}
      {!readOnly && (
        <button type="button" disabled={!canAdd} onClick={() => onChange([...rows, newRow(children)])} style={add}>
          {field.add_label || "+ Add row"}
        </button>
      )}
      {!readOnly && rows.length >= max && (
        <span style={{ fontSize: 11, color: "var(--text-muted)", marginLeft: 8 }}>At most {max} rows.</span>
      )}
    </div>
    </GroupKeyContext.Provider>
  );
}

const th: React.CSSProperties = {
  textAlign: "left", padding: "4px 6px", fontSize: 10, fontWeight: 700, color: "var(--text-muted)",
  textTransform: "uppercase", borderBottom: "2px solid var(--border-default)", whiteSpace: "nowrap",
};
const td: React.CSSProperties = { padding: "4px 6px", minWidth: 90 };
const icon: React.CSSProperties = {
  background: "none", border: "1px solid var(--border-subtle)", borderRadius: 4, padding: "0 6px",
  fontSize: 12, color: "var(--text-secondary)", cursor: "pointer", lineHeight: "20px",
};
const add: React.CSSProperties = {
  marginTop: 6, padding: "4px 10px", fontSize: 12, background: "transparent", border: "1px dashed var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-secondary)", cursor: "pointer",
};
