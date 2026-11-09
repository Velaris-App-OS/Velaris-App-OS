import React, { useEffect, useMemo, useState } from "react";
import {
  createReferenceTable, deleteReferenceTable, getReferenceTable, listReferenceTables, replaceReferenceRows,
  updateReferenceTable, type RefColumn, type RefRow, type RefTableSummary,
} from "@shared/api/client";
import { columnKey, sheetToTable, toCsv } from "./ReferenceEditor";

/* ═══════════════════════════════════════════════════════════════════
   Reference Tables — the lists forms read (Form Builder v2 step 3,
   2026-10-29): branches, products, price bands. Edit like a sheet,
   paste or import a CSV, export one. A form picks a table in the
   field's own panel; this page is for keeping them up to date.
   ═══════════════════════════════════════════════════════════════════ */

const TYPES: [RefColumn["type"], string][] = [["text", "Text"], ["number", "Number"], ["date", "Date"], ["boolean", "Yes / No"]];

export default function ReferenceTablesPage() {
  const [tables, setTables] = useState<RefTableSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const reload = () => listReferenceTables().then(setTables).catch(e => setError(e?.message || "Could not load"));
  useEffect(() => { reload(); }, []);

  const create = async () => {
    setError(null);
    try {
      const t = await createReferenceTable({ name: newName.trim(), columns: [
        { key: "code", label: "Code", type: "text" }, { key: "name", label: "Name", type: "text" }] });
      setNewName("");
      await reload();
      setSelected(t.id);
    } catch (e: any) { setError(e?.message || "Could not create"); }
  };

  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(200px, 260px) 1fr", gap: 16, padding: 20, minHeight: 0 }}>
      <aside>
        <h1 style={{ fontSize: 16, fontWeight: 600, margin: "0 0 4px" }}>Reference Tables</h1>
        <p style={{ fontSize: 12, color: "var(--text-muted)", margin: "0 0 12px" }}>
          Lists your forms choose from. A field picks one under Options → Reference table.
        </p>
        <div style={{ display: "flex", gap: 4, marginBottom: 10 }}>
          <input aria-label="New table name" placeholder="New table name" value={newName} onChange={e => setNewName(e.target.value)}
            onKeyDown={e => e.key === "Enter" && newName.trim() && create()} style={{ ...inp, flex: 1 }} />
          <button disabled={!newName.trim()} onClick={create} style={btn}>Add</button>
        </div>
        {error && <div role="alert" style={{ fontSize: 12, color: "var(--status-failed)", marginBottom: 8 }}>{error}</div>}
        {tables.length === 0 && <div style={{ fontSize: 12, color: "var(--text-muted)" }}>No tables yet.</div>}
        {tables.map(t => (
          <button key={t.id} onClick={() => setSelected(t.id)} style={{
            display: "block", width: "100%", textAlign: "left", padding: "8px 10px", marginBottom: 4, cursor: "pointer",
            border: `1px solid ${selected === t.id ? "var(--accent)" : "var(--border-subtle)"}`, borderRadius: "var(--radius-sm)",
            background: selected === t.id ? "var(--bg-elevated)" : "transparent", color: "var(--text-primary)",
          }}>
            <div style={{ fontSize: 13, fontWeight: 500 }}>{t.name}</div>
            <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{t.row_count} rows · {t.columns.length} columns{t.platform ? " · platform" : ""}</div>
          </button>
        ))}
      </aside>
      <main style={{ minWidth: 0 }}>
        {selected ? <TableEditor key={selected} id={selected} onChanged={reload} onDeleted={() => { setSelected(null); reload(); }} />
          : <div style={{ fontSize: 13, color: "var(--text-muted)", padding: 40, textAlign: "center" }}>Choose a table, or add one.</div>}
      </main>
    </div>
  );
}

function TableEditor({ id, onChanged, onDeleted }: { id: string; onChanged: () => void; onDeleted: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [columns, setColumns] = useState<RefColumn[]>([]);
  const [rows, setRows] = useState<RefRow[]>([]);
  const [platform, setPlatform] = useState(false);
  const [q, setQ] = useState("");
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [importing, setImporting] = useState(false);
  const [sheet, setSheet] = useState("");

  useEffect(() => {
    getReferenceTable(id).then(t => {
      setName(t.name); setDescription(t.description); setColumns(t.columns); setRows(t.rows); setPlatform(t.platform);
    }).catch(e => setMsg({ ok: false, text: e?.message || "Could not load" }));
  }, [id]);

  const touch = () => { setDirty(true); setMsg(null); };
  const shown = useMemo(() => {
    const n = q.trim().toLowerCase();
    return rows.map((r, i) => ({ r, i })).filter(({ r }) => !n || Object.values(r.data).some(v => String(v ?? "").toLowerCase().includes(n)));
  }, [rows, q]);

  const save = async () => {
    setBusy(true); setMsg(null);
    try {
      await updateReferenceTable(id, { name, description, columns });
      await replaceReferenceRows(id, rows.map(r => ({ ...r, data: Object.fromEntries(columns.map(c => [c.key, r.data[c.key] ?? null])) })));
      setDirty(false);
      setMsg({ ok: true, text: "Saved." });
      onChanged();
    } catch (e: any) { setMsg({ ok: false, text: e?.message || "Could not save" }); }
    finally { setBusy(false); }
  };
  const remove = async () => {
    if (!window.confirm(`Delete “${name}” and its ${rows.length} rows?`)) return;
    try { await deleteReferenceTable(id); onDeleted(); }
    catch (e: any) { setMsg({ ok: false, text: e?.message || "Could not delete" }); }
  };
  const importSheet = () => {
    const parsed = sheetToTable(sheet);
    if (!parsed.columns.length) return;
    // Headings that match a column (by key or label) fill it; new headings become new columns.
    const next = [...columns];
    const taken = new Set(next.map(c => c.key));
    const map = parsed.columns.map(pc => {
      const hit = next.find(c => c.key === pc.key || c.label.toLowerCase() === pc.label.toLowerCase());
      if (hit) return hit.key;
      const key = columnKey(pc.label, taken);
      next.push({ ...pc, key });
      return key;
    });
    setColumns(next);
    setRows(parsed.rows.map(r => ({ is_active: true, data: Object.fromEntries(parsed.columns.map((pc, i) => [map[i], r.data[pc.key]])) })));
    setImporting(false); setSheet(""); touch();
  };
  const download = () => {
    const blob = new Blob([toCsv(columns, rows)], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${name || "table"}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  };
  const setCell = (i: number, key: string, v: any) => { setRows(rs => rs.map((r, j) => (j === i ? { ...r, data: { ...r.data, [key]: v } } : r))); touch(); };

  return (
    <div>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 8 }}>
        <input aria-label="Table name" value={name} onChange={e => { setName(e.target.value); touch(); }} style={{ ...inp, fontSize: 15, fontWeight: 600, flex: "1 1 200px" }} />
        <button onClick={() => setImporting(i => !i)} style={btn}>Import CSV / paste</button>
        <button onClick={download} style={btn}>Export CSV</button>
        <button onClick={remove} style={{ ...btn, color: "var(--status-failed)" }}>Delete</button>
        <button onClick={save} disabled={!dirty || busy} style={{ ...btn, background: dirty ? "var(--accent)" : "transparent", color: dirty ? "#fff" : "var(--text-secondary)" }}>
          {busy ? "Saving…" : "Save"}
        </button>
      </div>
      {platform && <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 6 }}>A platform table: every tenant reads it; only the platform operator changes it.</div>}
      {msg && <div role={msg.ok ? "status" : "alert"} style={{ fontSize: 12, color: msg.ok ? "var(--status-completed)" : "var(--status-failed)", marginBottom: 6 }}>{msg.text}</div>}
      <input aria-label="Description" placeholder="What this table is for" value={description}
        onChange={e => { setDescription(e.target.value); touch(); }} style={{ ...inp, width: "100%", boxSizing: "border-box", marginBottom: 10 }} />

      {importing && (
        <div style={{ border: "1px dashed var(--border-default)", borderRadius: "var(--radius-sm)", padding: 10, marginBottom: 10 }}>
          <div style={{ fontSize: 12, marginBottom: 4 }}>Paste rows from a spreadsheet, or choose a CSV file — headings on the first line. This replaces the rows.</div>
          <input type="file" accept=".csv,text/csv" aria-label="CSV file" onChange={e => {
            const f = e.target.files?.[0];
            if (f) f.text().then(setSheet);
          }} />
          <textarea aria-label="Pasted rows" rows={5} value={sheet} onChange={e => setSheet(e.target.value)}
            style={{ ...inp, width: "100%", boxSizing: "border-box", marginTop: 6, fontFamily: "var(--font-mono)" }} />
          <button onClick={importSheet} disabled={!sheet.trim()} style={{ ...btn, marginTop: 6 }}>Use these rows</button>
        </div>
      )}

      <div style={{ fontSize: 12, fontWeight: 600, margin: "6px 0" }}>Columns</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 12 }}>
        {columns.map((c, ci) => (
          <div key={ci} style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)", padding: 6, display: "flex", gap: 4, alignItems: "center" }}>
            <input aria-label="Column heading" value={c.label} onChange={e => { setColumns(cs => cs.map((x, j) => j === ci ? { ...x, label: e.target.value } : x)); touch(); }} style={{ ...inp, width: 110 }} />
            <code style={{ fontSize: 10, color: "var(--text-muted)" }} title="Key — what a form stores and reads">{c.key}</code>
            <select aria-label="Column type" value={c.type} onChange={e => { setColumns(cs => cs.map((x, j) => j === ci ? { ...x, type: e.target.value as RefColumn["type"] } : x)); touch(); }} style={inp}>
              {TYPES.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            </select>
            <button aria-label={`Remove column ${c.label}`} onClick={() => { setColumns(cs => cs.filter((_, j) => j !== ci)); touch(); }} style={x}>×</button>
          </div>
        ))}
        <button onClick={() => {
          const label = `Column ${columns.length + 1}`;
          setColumns(cs => [...cs, { key: columnKey(label, new Set(cs.map(c => c.key))), label, type: "text" }]); touch();
        }} style={btn} disabled={columns.length >= 20}>+ Column</button>
      </div>

      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
        <span style={{ fontSize: 12, fontWeight: 600 }}>Rows</span>
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{rows.length} of at most 10,000</span>
        <input aria-label="Search rows" placeholder="Search" value={q} onChange={e => setQ(e.target.value)} style={{ ...inp, marginLeft: "auto", width: 180 }} />
      </div>
      <div style={{ overflowX: "auto", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
          <thead>
            <tr>
              {columns.map(c => <th key={c.key} style={th}>{c.label}</th>)}
              <th style={{ ...th, width: 60 }}>In use</th>
              <th style={{ ...th, width: 30 }} />
            </tr>
          </thead>
          <tbody>
            {shown.slice(0, 500).map(({ r, i }) => (
              <tr key={i} style={{ borderTop: "1px solid var(--border-subtle)", opacity: r.is_active ? 1 : 0.55 }}>
                {columns.map(c => (
                  <td key={c.key} style={{ padding: 2 }}>
                    {c.type === "boolean" ? (
                      <input type="checkbox" aria-label={`${c.label} row ${i + 1}`} checked={!!r.data[c.key]} onChange={e => setCell(i, c.key, e.target.checked)} />
                    ) : (
                      <input aria-label={`${c.label} row ${i + 1}`} value={r.data[c.key] ?? ""} type={c.type === "date" ? "date" : "text"}
                        inputMode={c.type === "number" ? "decimal" : undefined}
                        onChange={e => setCell(i, c.key, e.target.value === "" ? null : e.target.value)} style={{ ...cellInp }} />
                    )}
                  </td>
                ))}
                <td style={{ textAlign: "center" }}>
                  <input type="checkbox" aria-label={`Row ${i + 1} in use`} checked={r.is_active}
                    onChange={e => { setRows(rs => rs.map((x, j) => j === i ? { ...x, is_active: e.target.checked } : x)); touch(); }} />
                </td>
                <td><button aria-label={`Remove row ${i + 1}`} onClick={() => { setRows(rs => rs.filter((_, j) => j !== i)); touch(); }} style={x}>×</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        {shown.length > 500 && <div style={{ fontSize: 11, color: "var(--text-muted)", padding: 6 }}>Showing 500 of {shown.length} — search to narrow.</div>}
      </div>
      <button onClick={() => { setRows(rs => [...rs, { data: {}, is_active: true }]); touch(); }} style={{ ...btn, marginTop: 6 }} disabled={rows.length >= 10000}>+ Row</button>
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 8 }}>
        A row not in use stays here but is no longer offered or accepted. Forms using this table follow its changes at once.
      </div>
    </div>
  );
}

const inp: React.CSSProperties = {
  padding: "5px 8px", fontSize: 12, background: "var(--bg-input)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-primary)",
};
const cellInp: React.CSSProperties = { ...inp, width: "100%", boxSizing: "border-box", border: "1px solid transparent", background: "transparent" };
const btn: React.CSSProperties = {
  padding: "5px 10px", fontSize: 12, background: "transparent", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-secondary)", cursor: "pointer",
};
const x: React.CSSProperties = { background: "none", border: "none", color: "var(--status-failed)", cursor: "pointer", fontSize: 14 };
const th: React.CSSProperties = {
  textAlign: "left", padding: "6px 8px", fontSize: 10, fontWeight: 700, color: "var(--text-muted)",
  textTransform: "uppercase", borderBottom: "2px solid var(--border-default)", whiteSpace: "nowrap",
};
