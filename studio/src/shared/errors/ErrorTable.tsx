/**
 * ErrorTable — the single reusable error table, used everywhere errors are shown
 * (the per-page Error Documentation modal and its global search results).
 *
 * One template, fed a list of entries. Pass showComponent to add a Component column
 * (used in global / search results so each row's origin is visible).
 */
import React, { useState, useMemo } from "react";
import type { FlatErrorEntry } from "./catalog";

interface Props {
  entries:        FlatErrorEntry[];
  /** Show the Component column (for global/search results). */
  showComponent?: boolean;
  /** Substring to highlight (the active search query). */
  highlight?:     string;
  /** Show a per-column filter row under the header (default true). */
  columnFilters?: boolean;
}

type ColKey = "component" | "code" | "http" | "symptom" | "rootCause" | "resolution";

function cellValue(e: FlatErrorEntry, col: ColKey): string {
  switch (col) {
    case "component":  return e.componentLabel;
    case "code":       return e.code;
    case "http":       return e.http;
    case "symptom":    return e.symptom;
    case "rootCause":  return e.rootCause;
    case "resolution": return e.resolution + (e.related?.length ? " " + e.related.join(" ") : "");
  }
}

function statusColor(http: string): string {
  if (http.startsWith("2")) return "var(--status-completed)";
  if (http.startsWith("4")) return "#f59e0b";
  if (http.startsWith("5")) return "var(--status-failed)";
  return "var(--text-muted)";
}

function Highlighted({ text, term }: { text: string; term?: string }) {
  if (!term || !term.trim()) return <>{text}</>;
  const t = term.trim();
  const idx = text.toLowerCase().indexOf(t.toLowerCase());
  if (idx < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, idx)}
      <mark style={{ background: "color-mix(in srgb, var(--accent) 30%, transparent)", color: "inherit", padding: 0 }}>
        {text.slice(idx, idx + t.length)}
      </mark>
      {text.slice(idx + t.length)}
    </>
  );
}

const cellTh: React.CSSProperties = {
  textAlign: "left",
  padding: "8px 12px",
  fontSize: 11,
  fontWeight: 700,
  color: "var(--text-muted)",
  textTransform: "uppercase",
  letterSpacing: "0.04em",
  borderBottom: "1px solid var(--border-default)",
  position: "sticky",
  top: 0,
  background: "var(--bg-card)",
  whiteSpace: "nowrap",
};

const cellThFilter: React.CSSProperties = {
  padding: "4px 8px 8px",
  background: "var(--bg-card)",
  borderBottom: "1px solid var(--border-default)",
  position: "sticky",
  top: 28,
  zIndex: 1,
};

const cellTd: React.CSSProperties = {
  padding: "10px 12px",
  fontSize: 12.5,
  color: "var(--text-secondary)",
  borderBottom: "1px solid var(--border-subtle)",
  verticalAlign: "top",
  lineHeight: 1.5,
};

export default function ErrorTable({ entries, showComponent, highlight, columnFilters = true }: Props) {
  const [filters, setFilters] = useState<Partial<Record<ColKey, string>>>({});

  const setFilter = (col: ColKey, val: string) =>
    setFilters(f => ({ ...f, [col]: val }));

  const cols: ColKey[] = useMemo(
    () => (showComponent ? ["component"] : []).concat(["code", "http", "symptom", "rootCause", "resolution"]) as ColKey[],
    [showComponent],
  );

  const filtered = useMemo(() => {
    const active = cols.filter(c => (filters[c] ?? "").trim() !== "");
    if (active.length === 0) return entries;
    return entries.filter(e =>
      active.every(c => cellValue(e, c).toLowerCase().includes((filters[c] ?? "").trim().toLowerCase())),
    );
  }, [entries, filters, cols]);

  if (entries.length === 0) {
    return (
      <div style={{ padding: "28px 16px", textAlign: "center", color: "var(--text-muted)", fontSize: 13 }}>
        No errors documented for this view yet.
      </div>
    );
  }

  const filterInput = (col: ColKey, ph: string) => (
    <input
      value={filters[col] ?? ""}
      onChange={e => setFilter(col, e.target.value)}
      placeholder={ph}
      style={{
        width: "100%", boxSizing: "border-box", padding: "4px 6px", fontSize: 11,
        fontFamily: "var(--font-body)", background: "var(--bg-input)",
        border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)",
        color: "var(--text-primary)", outline: "none",
      }}
    />
  );

  return (
    <table style={{ width: "100%", borderCollapse: "collapse", tableLayout: "fixed" }}>
      <thead>
        <tr>
          {showComponent && <th style={{ ...cellTh, width: "11%" }}>Component</th>}
          <th style={{ ...cellTh, width: showComponent ? "17%" : "19%" }}>Code</th>
          <th style={{ ...cellTh, width: "6%" }}>HTTP</th>
          <th style={{ ...cellTh, width: "22%" }}>Symptom</th>
          <th style={{ ...cellTh, width: showComponent ? "22%" : "26%" }}>Root cause</th>
          <th style={{ ...cellTh }}>Resolution</th>
        </tr>
        {columnFilters && (
          <tr>
            {showComponent && <th style={{ ...cellThFilter }}>{filterInput("component", "filter…")}</th>}
            <th style={{ ...cellThFilter }}>{filterInput("code", "filter…")}</th>
            <th style={{ ...cellThFilter }}>{filterInput("http", "e.g. 4")}</th>
            <th style={{ ...cellThFilter }}>{filterInput("symptom", "filter…")}</th>
            <th style={{ ...cellThFilter }}>{filterInput("rootCause", "filter…")}</th>
            <th style={{ ...cellThFilter }}>{filterInput("resolution", "filter…")}</th>
          </tr>
        )}
      </thead>
      <tbody>
        {filtered.length === 0 && (
          <tr>
            <td colSpan={cols.length} style={{ ...cellTd, textAlign: "center", color: "var(--text-muted)", padding: "20px" }}>
              No rows match the column filters.
            </td>
          </tr>
        )}
        {filtered.map(e => (
          <tr key={e.code}>
            {showComponent && (
              <td style={{ ...cellTd, color: "var(--text-muted)", fontWeight: 600 }}>
                <Highlighted text={e.componentLabel} term={highlight} />
              </td>
            )}
            <td style={{ ...cellTd, fontFamily: "var(--font-mono)", color: "var(--text-primary)", fontWeight: 600, wordBreak: "break-word" }}>
              <Highlighted text={e.code} term={highlight} />
            </td>
            <td style={{ ...cellTd }}>
              <span style={{
                fontFamily: "var(--font-mono)", fontWeight: 700, fontSize: 12,
                color: statusColor(e.http),
              }}>
                {e.http}
              </span>
            </td>
            <td style={{ ...cellTd }}><Highlighted text={e.symptom} term={highlight} /></td>
            <td style={{ ...cellTd }}><Highlighted text={e.rootCause} term={highlight} /></td>
            <td style={{ ...cellTd }}>
              <Highlighted text={e.resolution} term={highlight} />
              {e.related && e.related.length > 0 && (
                <div style={{ marginTop: 6, fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                  See also: {e.related.join(", ")}
                </div>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
