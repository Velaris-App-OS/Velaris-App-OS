import React, { useMemo, useState } from "react";

/* ═══════════════════════════════════════════════════════════════════
   Display fields — Record details and the table, configured instead of
   dumped (Form Builder redesign part 8, 2026-10-28).

   Both read data from a field of the form (`from`, e.g. a lookup whose
   whole result is stored under its key) or their own value, pick values
   by path, label them and format them. With nothing configured they
   behave as before: the table shows the first ten keys of row one.
   ═══════════════════════════════════════════════════════════════════ */

export type Format = "text" | "number" | "currency" | "date" | "datetime" | "yes_no" | "link";
export type Column = { path: string; label?: string; format?: Format; currency?: string };

export const FORMATS: [Format, string][] = [
  ["text", "Text"], ["number", "Number"], ["currency", "Amount"], ["date", "Date"],
  ["datetime", "Date and time"], ["yes_no", "Yes / No"], ["link", "Link"],
];

export function atPath(obj: any, path?: string): any {
  if (!path) return obj;
  return path.split(".").reduce((o: any, k) => (o && typeof o === "object" ? o[k] : undefined), obj);
}

/** "customer_name" → "Customer name" — the label until the designer writes one. */
export function labelFromPath(path: string): string {
  const last = path.split(".").pop() || path;
  const words = last.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/[_-]+/g, " ").trim().toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function formatValue(v: any, format: Format = "text", currency?: string): React.ReactNode {
  if (v === undefined || v === null || v === "") return <span style={{ color: "var(--text-muted)" }}>—</span>;
  try {
    switch (format) {
      case "number":
        return typeof v === "number" || !isNaN(Number(v)) ? Number(v).toLocaleString() : String(v);
      case "currency": {
        const n = Number(v);
        if (isNaN(n)) return String(v);
        return currency && /^[A-Z]{3}$/.test(currency)
          ? n.toLocaleString(undefined, { style: "currency", currency })
          : `${currency ?? ""}${currency ? " " : ""}${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
      }
      case "date": case "datetime": {
        const d = new Date(v);
        if (isNaN(d.getTime())) return String(v);
        return format === "date" ? d.toLocaleDateString() : d.toLocaleString();
      }
      case "yes_no":
        return v === true || v === "true" || v === 1 || v === "1" || v === "yes" ? "Yes" : "No";
      case "link": {
        const url = String(v);
        // Only web links — a javascript: or data: URL from a connector must never become clickable.
        return /^https?:\/\//i.test(url)
          ? <a href={url} target="_blank" rel="noopener noreferrer" style={{ color: "var(--accent)" }}>{url}</a>
          : url;
      }
    }
  } catch { /* fall through to text */ }
  if (typeof v === "object") return <code style={{ fontSize: 11 }}>{JSON.stringify(v)}</code>;
  return String(v);
}

/* ── Record details ───────────────────────────────────────────── */

export function RecordDetails({ data, items, layout, emptyMessage, showRaw }: {
  data: any; items?: Column[]; layout?: "list" | "grid"; emptyMessage?: string; showRaw?: boolean;
}) {
  const [raw, setRaw] = useState(false);
  const empty = data === undefined || data === null || (typeof data === "object" && !Object.keys(data).length);
  if (empty) return <div style={{ fontSize: 12, color: "var(--text-muted)", fontStyle: "italic" }}>{emptyMessage || "No data yet."}</div>;
  // Nothing chosen: the record's own simple values, labelled from their names.
  const rows: Column[] = items?.length ? items
    : typeof data === "object" && !Array.isArray(data)
      ? Object.keys(data).filter(k => data[k] === null || typeof data[k] !== "object").slice(0, 12).map(k => ({ path: k }))
      : [];
  return (
    <div>
      {rows.length > 0 && (
        <dl style={{
          display: "grid", gridTemplateColumns: layout === "grid" ? "repeat(auto-fill, minmax(180px, 1fr))" : "minmax(120px, 35%) 1fr",
          gap: layout === "grid" ? 12 : "6px 12px", margin: 0, fontSize: 13,
        }}>
          {rows.map(c => layout === "grid" ? (
            <div key={c.path} style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)", padding: "8px 10px" }}>
              <dt style={{ fontSize: 11, color: "var(--text-muted)" }}>{c.label || labelFromPath(c.path)}</dt>
              <dd style={{ margin: 0, color: "var(--text-primary)", fontWeight: 500 }}>{formatValue(atPath(data, c.path), c.format, c.currency)}</dd>
            </div>
          ) : (
            <React.Fragment key={c.path}>
              <dt style={{ color: "var(--text-muted)" }}>{c.label || labelFromPath(c.path)}</dt>
              <dd style={{ margin: 0, color: "var(--text-primary)" }}>{formatValue(atPath(data, c.path), c.format, c.currency)}</dd>
            </React.Fragment>
          ))}
        </dl>
      )}
      {(showRaw || !rows.length) && (
        <div style={{ marginTop: rows.length ? 8 : 0 }}>
          {rows.length > 0 && (
            <button type="button" onClick={() => setRaw(r => !r)}
              style={{ background: "none", border: "none", padding: 0, fontSize: 11, color: "var(--text-muted)", cursor: "pointer" }}>
              {raw ? "▾" : "▸"} All data
            </button>
          )}
          {(raw || !rows.length) && (
            <pre style={{ margin: "4px 0 0", padding: "8px 12px", fontSize: 11, fontFamily: "var(--font-mono)", whiteSpace: "pre-wrap",
              maxHeight: 300, overflow: "auto", background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)" }}>
              {JSON.stringify(data, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

/* ── Table ─────────────────────────────────────────────────────── */

export function DataTable({ rows, columns, emptyMessage, pageSize, sortable, total }: {
  rows: any[]; columns?: Column[]; emptyMessage?: string; pageSize?: number; sortable?: boolean; total?: number;
}) {
  const [page, setPage] = useState(0);
  const [sort, setSort] = useState<{ path: string; dir: 1 | -1 } | null>(null);
  // Nothing chosen: the first ten keys of row one, as before part 8.
  const cols: Column[] = columns?.length ? columns
    : Object.keys(rows[0] ?? {}).slice(0, 10).map(k => ({ path: k, label: k }));
  const sorted = useMemo(() => {
    if (!sort) return rows;
    return [...rows].sort((a, b) => {
      const x = atPath(a, sort.path), y = atPath(b, sort.path);
      if (x === y) return 0;
      if (x === undefined || x === null) return 1;
      if (y === undefined || y === null) return -1;
      return (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true })) * sort.dir;
    });
  }, [rows, sort]);
  if (rows.length === 0) return <div style={{ fontSize: 12, color: "var(--text-muted)", fontStyle: "italic" }}>{emptyMessage || "No rows."}</div>;
  const size = pageSize && pageSize > 0 ? pageSize : sorted.length;
  const pages = Math.max(1, Math.ceil(sorted.length / size));
  const p = Math.min(page, pages - 1);
  const shown = sorted.slice(p * size, p * size + size);

  return (
    <div style={{ overflowX: "auto", fontSize: 12 }}>
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr style={{ borderBottom: "2px solid var(--border-default)" }}>
            {cols.map(c => (
              <th key={c.path} onClick={() => sortable && setSort(s => s?.path === c.path ? { path: c.path, dir: s.dir === 1 ? -1 : 1 } : { path: c.path, dir: 1 })}
                aria-sort={sort?.path === c.path ? (sort.dir === 1 ? "ascending" : "descending") : undefined}
                style={{ padding: "5px 8px", textAlign: c.format === "number" || c.format === "currency" ? "right" : "left", fontSize: 10,
                  fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", whiteSpace: "nowrap", cursor: sortable ? "pointer" : "default" }}>
                {c.label ?? labelFromPath(c.path)}{sort?.path === c.path ? (sort.dir === 1 ? " ▲" : " ▼") : ""}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((row, i) => (
            <tr key={i} style={{ borderBottom: "1px solid var(--border-subtle)", background: i % 2 === 0 ? "transparent" : "var(--bg-elevated)" }}>
              {cols.map(c => (
                <td key={c.path} style={{ padding: "5px 8px", color: "var(--text-primary)", maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis",
                  whiteSpace: "nowrap", textAlign: c.format === "number" || c.format === "currency" ? "right" : "left" }}>
                  {formatValue(atPath(row, c.path), c.format, c.currency)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {(pages > 1 || total !== undefined) && (
        <div style={{ padding: "6px 8px", fontSize: 11, color: "var(--text-muted)", display: "flex", gap: 12, alignItems: "center" }}>
          <span>{total ?? rows.length} rows</span>
          {pages > 1 && (
            <>
              <button type="button" disabled={p === 0} onClick={() => setPage(p - 1)} style={pager}>‹ Previous</button>
              <span>Page {p + 1} of {pages}</span>
              <button type="button" disabled={p >= pages - 1} onClick={() => setPage(p + 1)} style={pager}>Next ›</button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

const pager: React.CSSProperties = {
  background: "none", border: "1px solid var(--border-default)", borderRadius: 4, padding: "1px 8px",
  fontSize: 11, color: "var(--text-secondary)", cursor: "pointer",
};

/** The rows a table shows: `rows_path` inside the data, else the data itself or its `items`. */
export function tableRows(data: any, rowsPath?: string): any[] {
  const v = atPath(data, rowsPath);
  if (Array.isArray(v)) return v;
  if (!rowsPath && Array.isArray(v?.items)) return v.items;
  return [];
}
