import React, { useState } from "react";
import type { FormFieldDef } from "./FormBuilder";
import { fieldKey } from "./validate";
import { FORMATS, atPath, labelFromPath } from "./display";
import { pathsOf } from "./LookupEditor";

/* ═══════════════════════════════════════════════════════════════════
   DisplayEditor — where a table / record details gets its data, and
   which values it shows, labelled and formatted
   (Form Builder redesign part 8, 2026-10-28).
   ═══════════════════════════════════════════════════════════════════ */

type Col = { path: string; label?: string; format?: string; currency?: string };

export default function DisplayEditor({ field, fields, onUpdate }: {
  field: FormFieldDef;
  fields: FormFieldDef[];
  onUpdate: (p: Partial<FormFieldDef>) => void;
}) {
  const isTable = field.type === "table_display";
  const self = fieldKey(field);
  const others = fields.filter(f => fieldKey(f) !== self);
  const lookups = others.filter(f => f.type === "connector_lookup");
  const source = field.from ? fields.find(f => fieldKey(f) === field.from) : field;
  // Sample data: what the builder and View Form show — the source's sample
  // (a lookup's saved test result), else this field's own.
  const sample = source?.default_value;
  const listPaths = sample !== undefined ? pathsOf(sample).filter(p => p.kind === "list") : [];
  const target = atPath(sample, field.rows_path);
  const row = Array.isArray(target) ? target[0] : target;
  const rowPaths = row && typeof row === "object" ? pathsOf(row).filter(p => p.kind === "value" && p.path) : [];
  const cols: Col[] = (field.columns as Col[] | undefined) ?? [];
  const setCols = (next: Col[]) => onUpdate({ columns: next.length ? next : undefined });
  const setCol = (i: number, p: Partial<Col>) => setCols(cols.map((c, j) => (j === i ? { ...c, ...p } : c)));
  const move = (i: number, d: -1 | 1) => {
    const j = i + d;
    if (j < 0 || j >= cols.length) return;
    const next = [...cols];
    [next[i], next[j]] = [next[j], next[i]];
    setCols(next);
  };

  return (
    <div>
      <L>Show data from</L>
      <select aria-label="Data from" value={field.from ?? ""} style={sel}
        onChange={e => onUpdate({ from: e.target.value || undefined, rows_path: undefined })}>
        <option value="">This field (pre-filled from a case variable)</option>
        {lookups.length > 0 && <optgroup label="Lookups">
          {lookups.map(f => <option key={fieldKey(f)} value={fieldKey(f)}>{f.label || fieldKey(f)} — its whole result</option>)}
        </optgroup>}
        <optgroup label="Other fields">
          {others.filter(f => f.type !== "connector_lookup").map(f => <option key={fieldKey(f)} value={fieldKey(f)}>{f.label || fieldKey(f)}</option>)}
        </optgroup>
      </select>
      {field.from && lookups.some(l => fieldKey(l) === field.from) && sample === undefined && (
        <Hint>Run a test on that lookup and keep its result as sample data to pick from here.</Hint>
      )}

      <L top>{isTable ? "The rows are at" : "The record is at"}</L>
      {listPaths.length || !sample ? (
        <select aria-label="Rows at" value={field.rows_path ?? ""} style={sel} onChange={e => onUpdate({ rows_path: e.target.value || undefined })}>
          <option value="">{isTable ? "The data itself (a list)" : "The data itself"}</option>
          {listPaths.filter(p => p.path).map(p => <option key={p.path} value={p.path}>{p.path}</option>)}
          {field.rows_path && !listPaths.some(p => p.path === field.rows_path) && <option value={field.rows_path}>{field.rows_path}</option>}
        </select>
      ) : (
        <input aria-label="Rows at" value={field.rows_path ?? ""} placeholder="e.g. data.items" style={{ ...sel, fontFamily: "var(--font-mono)" }}
          onChange={e => onUpdate({ rows_path: e.target.value || undefined })} />
      )}

      <L top>{isTable ? "Columns" : "Values shown"}</L>
      <Hint>{cols.length ? "In this order." : isTable ? "None chosen: the first ten values of the first row, as named in the data." : "None chosen: every simple value, labelled from its name."}</Hint>
      {cols.map((c, i) => (
        <div key={i} style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)", padding: 6, marginBottom: 4 }}>
          <div style={{ display: "flex", gap: 4, marginBottom: 4 }}>
            {rowPaths.length ? (
              <select aria-label="Value" value={c.path} style={{ ...sel, flex: 1 }} onChange={e => setCol(i, { path: e.target.value })}>
                {!rowPaths.some(p => p.path === c.path) && <option value={c.path}>{c.path}</option>}
                {rowPaths.map(p => <option key={p.path} value={p.path}>{p.path}</option>)}
              </select>
            ) : (
              <input aria-label="Value" value={c.path} style={{ ...sel, flex: 1, fontFamily: "var(--font-mono)" }} onChange={e => setCol(i, { path: e.target.value })} />
            )}
            <Mini label="Move up" onClick={() => move(i, -1)}>▲</Mini>
            <Mini label="Move down" onClick={() => move(i, 1)}>▼</Mini>
            <Mini label="Remove" danger onClick={() => setCols(cols.filter((_, j) => j !== i))}>×</Mini>
          </div>
          <div style={{ display: "flex", gap: 4 }}>
            <input aria-label="Heading" value={c.label ?? ""} placeholder={labelFromPath(c.path)} style={{ ...sel, flex: 1 }}
              onChange={e => setCol(i, { label: e.target.value || undefined })} />
            <select aria-label="Format" value={c.format ?? "text"} style={{ ...sel, flex: "0 0 110px" }}
              onChange={e => setCol(i, { format: e.target.value === "text" ? undefined : e.target.value })}>
              {FORMATS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            </select>
            {c.format === "currency" && (
              <input aria-label="Currency" value={c.currency ?? ""} placeholder="EUR" style={{ ...sel, flex: "0 0 56px" }}
                onChange={e => setCol(i, { currency: e.target.value.toUpperCase() || undefined })} />
            )}
          </div>
        </div>
      ))}
      <div style={{ display: "flex", gap: 6 }}>
        <button style={btn} onClick={() => {
          const free = rowPaths.find(p => !cols.some(c => c.path === p.path))?.path ?? "";
          setCols([...cols, { path: free }]);
        }}>+ Add {isTable ? "a column" : "a value"}</button>
        {rowPaths.length > 0 && cols.length === 0 && (
          <button style={btn} onClick={() => setCols(rowPaths.slice(0, 10).map(p => ({ path: p.path })))}>Add all from the sample</button>
        )}
      </div>

      <L top>When there is no data</L>
      <input aria-label="Empty message" value={field.empty_message ?? ""} placeholder={isTable ? "No rows." : "No data yet."} style={sel}
        onChange={e => onUpdate({ empty_message: e.target.value || undefined })} />
      {isTable ? (
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
          <label style={{ fontSize: 12 }}>Rows per page
            <input aria-label="Rows per page" type="number" min={0} value={field.page_size ?? ""} style={{ ...sel, width: 70, marginLeft: 6 }}
              onChange={e => onUpdate({ page_size: e.target.value ? Number(e.target.value) : undefined })} />
          </label>
          <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 4 }}>
            <input type="checkbox" checked={!!field.sortable} onChange={e => onUpdate({ sortable: e.target.checked || undefined })} />
            Sort by clicking a heading
          </label>
        </div>
      ) : (
        <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 4, marginTop: 8 }}>
          <input type="checkbox" checked={!!field.show_raw} onChange={e => onUpdate({ show_raw: e.target.checked || undefined })} />
          Also offer "All data" (the raw record)
        </label>
      )}

      {!field.from && <SampleData field={field} onUpdate={onUpdate} />}
    </div>
  );
}

/** Sample data for the builder and View Form; never used in a real case. */
function SampleData({ field, onUpdate }: { field: FormFieldDef; onUpdate: (p: Partial<FormFieldDef>) => void }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState(() => (field.default_value === undefined ? "" : JSON.stringify(field.default_value, null, 2)));
  const [bad, setBad] = useState(false);
  return (
    <div style={{ marginTop: 12 }}>
      <button style={{ ...btn, border: "none", padding: 0 }} onClick={() => setOpen(o => !o)}>{open ? "▾" : "▸"} Sample data (builder and View Form only)</button>
      {open && (
        <>
          <textarea aria-label="Sample data" value={text} rows={6} style={{ ...sel, fontFamily: "var(--font-mono)", resize: "vertical", marginTop: 4 }}
            onChange={e => {
              setText(e.target.value);
              if (!e.target.value.trim()) { onUpdate({ default_value: undefined }); setBad(false); return; }
              try { onUpdate({ default_value: JSON.parse(e.target.value) }); setBad(false); } catch { setBad(true); }
            }} />
          {bad && <div style={{ fontSize: 11, color: "var(--status-warning, #d97706)" }}>Not valid JSON yet — the last valid sample is kept.</div>}
        </>
      )}
    </div>
  );
}

function L({ children, top }: { children: React.ReactNode; top?: boolean }) {
  return <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-secondary)", margin: `${top ? 12 : 0}px 0 4px` }}>{children}</div>;
}
function Hint({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 4 }}>{children}</div>;
}
function Mini({ children, onClick, label, danger }: { children: React.ReactNode; onClick: () => void; label: string; danger?: boolean }) {
  return <button aria-label={label} title={label} onClick={onClick}
    style={{ background: "none", border: "none", cursor: "pointer", fontSize: 11, color: danger ? "var(--status-failed)" : "var(--text-muted)" }}>{children}</button>;
}
const sel: React.CSSProperties = {
  width: "100%", padding: "5px 6px", fontSize: 12, background: "var(--bg-input)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-primary)", boxSizing: "border-box", minWidth: 0,
};
const btn: React.CSSProperties = {
  padding: "3px 8px", fontSize: 11, background: "transparent", border: "1px dashed var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-secondary)", cursor: "pointer", marginTop: 2,
};
