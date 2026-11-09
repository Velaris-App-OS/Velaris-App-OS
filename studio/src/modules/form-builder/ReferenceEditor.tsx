import React, { useEffect, useState } from "react";
import {
  createReferenceTable, getReferenceTable, listReferenceTables, replaceReferenceRows,
  type RefColumn, type RefRow, type RefTableSummary,
} from "@shared/api/client";
import type { FormFieldDef } from "./FormBuilder";
import { fieldKey } from "./validate";

/* ═══════════════════════════════════════════════════════════════════
   Options from a reference table, set where the field is (Form Builder
   v2 step 3, 2026-10-29): which table, which column is stored and which
   is shown, cascading filters ("only rows whose Region is the Region
   field"), and which other fields the chosen row fills. A table that
   does not exist yet is made here — name, columns, paste the rows —
   without leaving the form.
   ═══════════════════════════════════════════════════════════════════ */

type Update = (p: Partial<FormFieldDef>) => void;

/** CSV (or tab-separated, as pasted from a spreadsheet) → rows of cells. Quotes as in RFC 4180. */
export function parseCsv(text: string): string[][] {
  const sep = text.includes("\t") && !text.split("\n")[0].includes(",") ? "\t" : ",";
  const rows: string[][] = [];
  let row: string[] = [], cell = "", quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { cell += '"'; i++; }
      else if (c === '"') quoted = false;
      else cell += c;
    } else if (c === '"' && cell === "") quoted = true;
    else if (c === sep) { row.push(cell); cell = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(cell); cell = "";
      if (row.some(x => x.trim() !== "")) rows.push(row);
      row = [];
    } else cell += c;
  }
  row.push(cell);
  if (row.some(x => x.trim() !== "")) rows.push(row);
  return rows;
}

export function toCsv(columns: RefColumn[], rows: RefRow[]): string {
  const q = (v: any) => {
    const t = v === null || v === undefined ? "" : String(v);
    return /[",\n\r]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
  };
  return [columns.map(c => q(c.key)).join(","), ...rows.map(r => columns.map(c => q(r.data[c.key])).join(","))].join("\n");
}

/** "Product code" → "product_code" — a column's key from its heading. */
export function columnKey(label: string, taken: Set<string>): string {
  let base = label.toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "").slice(0, 36);
  if (!/^[a-z]/.test(base)) base = `c_${base || "col"}`;
  let k = base;
  for (let n = 2; taken.has(k); n++) k = `${base}_${n}`;
  taken.add(k);
  return k;
}

/** A pasted sheet (first line = headings) → columns and rows. */
export function sheetToTable(text: string): { columns: RefColumn[]; rows: RefRow[] } {
  const cells = parseCsv(text.trim());
  if (!cells.length) return { columns: [], rows: [] };
  const taken = new Set<string>();
  const columns: RefColumn[] = cells[0].map(h => ({ key: columnKey(h || "column", taken), label: h.trim() || "Column", type: "text" }));
  const rows = cells.slice(1).map(r => ({ data: Object.fromEntries(columns.map((c, i) => [c.key, (r[i] ?? "").trim() || null])), is_active: true }));
  // A column whose every value is a number is a number column.
  for (const c of columns) {
    const vals = rows.map(r => r.data[c.key]).filter(v => v !== null);
    if (vals.length && vals.every(v => /^-?\d+(\.\d+)?$/.test(String(v)))) c.type = "number";
  }
  return { columns, rows };
}

export default function ReferenceEditor({ field, fields, onUpdate }: { field: FormFieldDef; fields: FormFieldDef[]; onUpdate: Update }) {
  const src: any = (field as any).options_source ?? {};
  const set = (p: Record<string, any>) => onUpdate({ options_source: { ...src, kind: "reference", ...p } } as any);
  const [tables, setTables] = useState<RefTableSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [preview, setPreview] = useState<RefRow[]>([]);
  const reload = () => listReferenceTables().then(setTables).catch(e => setError(e?.message || "Could not load reference tables"));
  useEffect(() => { reload(); }, []);
  const table = tables?.find(t => t.id === src.table_id);
  const cols = table?.columns ?? [];
  useEffect(() => {
    if (!src.table_id) { setPreview([]); return; }
    getReferenceTable(src.table_id).then(t => setPreview(t.rows.slice(0, 5))).catch(() => setPreview([]));
  }, [src.table_id]);

  const self = fieldKey(field);
  const others = fields.filter(f => fieldKey(f) !== self && !["connector_lookup", "connector_result", "json_display", "table_display", "record_details", "repeating_group"].includes(f.type));
  const filters: any[] = Array.isArray(src.filters) ? src.filters : [];
  const fill: any[] = Array.isArray(src.fill) ? src.fill : [];
  const choose = (t: RefTableSummary) => set({
    table_id: t.id, value_column: t.columns[0]?.key, label_column: (t.columns[1] ?? t.columns[0])?.key, filters: [], fill: [],
  });

  return (
    <div>
      {error && <div style={{ fontSize: 11, color: "var(--status-failed)" }}>{error}</div>}
      <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
        <select aria-label="Reference table" value={src.table_id ?? ""} style={{ ...sel, flex: 1 }}
          onChange={e => { const t = tables?.find(x => x.id === e.target.value); if (t) choose(t); }}>
          <option value="">{tables === null ? "Loading…" : "— choose a table —"}</option>
          {(tables ?? []).map(t => <option key={t.id} value={t.id}>{t.name} ({t.row_count} rows{t.platform ? ", platform" : ""})</option>)}
        </select>
        <button type="button" onClick={() => setCreating(c => !c)} style={dashed}>+ New table</button>
      </div>
      {creating && <NewTable onCreated={t => { setCreating(false); reload().then(() => choose(t)); }} />}
      {src.table_id && !table && tables && <div style={{ fontSize: 11, color: "var(--status-warning, #d97706)", marginTop: 4 }}>
        This table is gone or not yours — the field accepts nothing until another is chosen.</div>}
      {table && (
        <>
          <div style={{ display: "flex", gap: 4, marginTop: 8 }}>
            <label style={lbl}>Stored<select aria-label="Value column" value={src.value_column ?? ""} onChange={e => set({ value_column: e.target.value })} style={sel}>
              {cols.map(c => <option key={c.key} value={c.key}>{c.label}</option>)}</select></label>
            <label style={lbl}>Shown<select aria-label="Label column" value={src.label_column ?? ""} onChange={e => set({ label_column: e.target.value })} style={sel}>
              {cols.map(c => <option key={c.key} value={c.key}>{c.label}</option>)}</select></label>
          </div>

          <div style={head}>Only rows where (cascading)</div>
          {filters.map((f, i) => (
            <div key={i} style={{ display: "flex", gap: 4, alignItems: "center", marginBottom: 4 }}>
              <select aria-label="Filter column" value={f.column ?? ""} onChange={e => set({ filters: filters.map((x, j) => j === i ? { ...x, column: e.target.value } : x) })} style={{ ...sel, flex: 1 }}>
                {cols.map(c => <option key={c.key} value={c.key}>{c.label}</option>)}</select>
              <span style={{ fontSize: 11 }}>is the</span>
              <select aria-label="Filter field" value={f.field ?? ""} onChange={e => set({ filters: filters.map((x, j) => j === i ? { ...x, field: e.target.value } : x) })} style={{ ...sel, flex: 1 }}>
                <option value="">— field —</option>
                {others.map(o => <option key={fieldKey(o)} value={fieldKey(o)}>{o.label}</option>)}</select>
              <button aria-label="Remove filter" onClick={() => set({ filters: filters.filter((_, j) => j !== i) })} style={x}>×</button>
            </div>
          ))}
          <button type="button" onClick={() => set({ filters: [...filters, { column: cols[0]?.key, field: "" }] })} style={dashed}>+ Filter by another field</button>

          <div style={head}>When chosen, fill</div>
          {fill.map((f, i) => (
            <div key={i} style={{ display: "flex", gap: 4, alignItems: "center", marginBottom: 4 }}>
              <select aria-label="Fill field" value={f.field ?? ""} onChange={e => set({ fill: fill.map((x, j) => j === i ? { ...x, field: e.target.value } : x) })} style={{ ...sel, flex: 1 }}>
                <option value="">— field —</option>
                {others.map(o => <option key={fieldKey(o)} value={fieldKey(o)}>{o.label}</option>)}</select>
              <span style={{ fontSize: 11 }}>with</span>
              <select aria-label="Fill column" value={f.column ?? ""} onChange={e => set({ fill: fill.map((x, j) => j === i ? { ...x, column: e.target.value } : x) })} style={{ ...sel, flex: 1 }}>
                {cols.map(c => <option key={c.key} value={c.key}>{c.label}</option>)}</select>
              <button aria-label="Remove fill" onClick={() => set({ fill: fill.filter((_, j) => j !== i) })} style={x}>×</button>
            </div>
          ))}
          <button type="button" onClick={() => set({ fill: [...fill, { field: "", column: cols[0]?.key }] })} style={dashed}>+ Fill another field</button>
          <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 6 }}>
            The server checks every choice against the table. A filled field that is read-only holds the table's value — never one typed or sent.
          </div>
          {preview.length > 0 && (
            <div style={{ marginTop: 8, overflowX: "auto" }}>
              <table style={{ fontSize: 11, borderCollapse: "collapse", width: "100%" }}>
                <thead><tr>{cols.map(c => <th key={c.key} style={{ textAlign: "left", color: "var(--text-muted)", fontWeight: 600, padding: "2px 4px" }}>{c.label}</th>)}</tr></thead>
                <tbody>{preview.map((r, i) => <tr key={i}>{cols.map(c => <td key={c.key} style={{ padding: "2px 4px" }}>{String(r.data[c.key] ?? "")}</td>)}</tr>)}</tbody>
              </table>
              {table.row_count > preview.length && <div style={{ fontSize: 10, color: "var(--text-muted)" }}>…and {table.row_count - preview.length} more</div>}
            </div>
          )}
        </>
      )}
    </div>
  );
}

/** Make a table here: a name, and the rows pasted with their headings on the first line. */
function NewTable({ onCreated }: { onCreated: (t: RefTableSummary) => void }) {
  const [name, setName] = useState("");
  const [sheet, setSheet] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const parsed = sheetToTable(sheet);
  const save = async () => {
    setBusy(true); setError(null);
    try {
      const t = await createReferenceTable({ name: name.trim(), columns: parsed.columns });
      if (parsed.rows.length) await replaceReferenceRows(t.id, parsed.rows);
      onCreated({ ...t, row_count: parsed.rows.length });
    } catch (e: any) { setError(e?.message || "Could not create the table"); }
    finally { setBusy(false); }
  };
  return (
    <div style={{ border: "1px dashed var(--border-default)", borderRadius: "var(--radius-sm)", padding: 8, marginTop: 6 }}>
      <input aria-label="Table name" placeholder="Table name, e.g. Branches" value={name} onChange={e => setName(e.target.value)} style={{ ...sel, width: "100%", boxSizing: "border-box" }} />
      <textarea aria-label="Rows" rows={4} placeholder={"Paste from a spreadsheet — headings first:\ncode,name,region\nBER,Berlin,east"} value={sheet}
        onChange={e => setSheet(e.target.value)} style={{ ...sel, width: "100%", boxSizing: "border-box", marginTop: 4, fontFamily: "var(--font-mono)" }} />
      <div style={{ fontSize: 10, color: "var(--text-muted)" }}>
        {parsed.columns.length ? `${parsed.columns.length} columns (${parsed.columns.map(c => c.label).join(", ")}), ${parsed.rows.length} rows` : "No columns yet."}
      </div>
      {error && <div role="alert" style={{ fontSize: 11, color: "var(--status-failed)" }}>{error}</div>}
      <button type="button" disabled={busy || !name.trim() || !parsed.columns.length} onClick={save} style={{ ...dashed, marginTop: 4 }}>
        {busy ? "Creating…" : "Create and use"}
      </button>
    </div>
  );
}

const sel: React.CSSProperties = {
  padding: "4px 6px", fontSize: 12, background: "var(--bg-input)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-primary)", minWidth: 0,
};
const dashed: React.CSSProperties = {
  padding: "3px 8px", fontSize: 11, background: "transparent", border: "1px dashed var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-secondary)", cursor: "pointer", whiteSpace: "nowrap",
};
const x: React.CSSProperties = { background: "none", border: "none", color: "var(--status-failed)", cursor: "pointer" };
const lbl: React.CSSProperties = { display: "flex", flexDirection: "column", fontSize: 10, color: "var(--text-muted)", gap: 2, flex: 1 };
const head: React.CSSProperties = { fontSize: 11, color: "var(--text-primary)", margin: "10px 0 4px" };
