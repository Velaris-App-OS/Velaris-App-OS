/**
 * Operations editor (2026-11-04) — a connector's named requests, without typing
 * JSON. Reads and writes the `operations` list inside the connector's config
 * JSON text; everything else in the config is left exactly as it is. The server
 * checks the same rules (velaris_shared/hxbridge/operations.py) on save.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React from "react";

export type Operation = { key: string; label: string; method: string; path: string; sql?: string };
const METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"];
export const OPERATION_TYPES = new Set(["http", "http_custom", "sql_query"]);

/** A database query connector's saved query (2026-11-08): the first check the
 * server makes (velaris_shared/hxbridge/sql_query.py), said in the browser. */
export function queryProblem(sql: string): string | null {
  const bare = (sql || "").replace(/'(?:[^'\\]|''|\\.)*'|"(?:[^"]|"")*"|`[^`]*`/g, "''").trim().replace(/;\s*$/, "");
  if (!bare) return "A query is required";
  if (/--|\/\*|#|\$[A-Za-z_]*\$/.test(bare)) return "No comments or dollar-quoted text";
  if (bare.includes(";")) return "One statement only — no ';' inside";
  if (!/^\s*(select|with)\b/i.test(bare)) return "Starts with SELECT or WITH";
  const w = bare.match(/\b(insert|update|delete|merge|truncate|drop|alter|create|grant|revoke|into)\b/i);
  if (w) return `Only reads — not allowed: ${w[1].toUpperCase()}`;
  return null;
}

const DB_STARTER = { engine: "postgresql", host: "", port: 5432, database: "", ssl_mode: "require",
  timeout_seconds: 10, max_rows: 100 };

/** The rule the server applies, said in the browser first. */
export function operationProblem(op: Operation, all: Operation[]): string | null {
  if (!/^[a-z][a-z0-9_]{0,39}$/.test(op.key)) return "Key: lowercase letters, digits and _ (starts with a letter)";
  if (all.filter(o => o.key === op.key).length > 1) return "Each key once";
  if (!op.path.startsWith("/") || op.path.startsWith("//") || op.path.includes("://")
      || /[?#\\]/.test(op.path) || op.path.split("/").some(s => s === ".." || s === "."))
    return "Path: starts with /, no host, query, # or ../";
  return null;
}

function read(configText: string): { cfg: Record<string, any> | null; ops: Operation[] } {
  try {
    const cfg = JSON.parse(configText || "{}");
    const ops = Array.isArray(cfg?.operations) ? cfg.operations : [];
    return { cfg, ops: ops.map((o: any) => (o && typeof o.sql === "string"
      ? { key: o.key ?? "", label: o.label ?? "", method: "QUERY", path: "", sql: o.sql }
      : { key: o.key ?? "", label: o.label ?? "", method: o.method ?? "GET", path: o.path ?? "/" })) };
  } catch {
    return { cfg: null, ops: [] };
  }
}

export default function OperationsEditor({ connectorType, configText, onChange, disabled }: {
  connectorType: string; configText: string; onChange: (text: string) => void; disabled?: boolean;
}) {
  if (!OPERATION_TYPES.has(connectorType)) return null;
  const { cfg, ops } = read(configText);
  if (cfg === null) return <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Fix the config JSON to edit operations.</div>;
  const write = (next: Operation[]) => {
    const out = { ...cfg };
    if (next.length) out.operations = next; else delete out.operations;
    onChange(JSON.stringify(out, null, 2));
  };
  const setOp = (i: number, p: Partial<Operation>) => write(ops.map((o, j) => (j === i ? { ...o, ...p } : o)));
  const cell: React.CSSProperties = { fontSize: 11, padding: "3px 5px", border: "1px solid var(--border-default)",
    borderRadius: 4, background: "var(--bg-input, var(--bg-card))", color: "var(--text-primary)", minWidth: 0 };
  if (connectorType === "sql_query") {
    const writeQ = (next: Operation[]) => {
      const out = { ...cfg };
      if (next.length) out.operations = next.map(o => ({ key: o.key, label: o.label, sql: o.sql ?? "" }));
      else delete out.operations;
      onChange(JSON.stringify(out, null, 2));
    };
    const setQ = (i: number, p: Partial<Operation>) => writeQ(ops.map((o, j) => (j === i ? { ...o, ...p } : o)));
    return (
      <div style={{ marginBottom: 8 }}>
        <div style={{ fontSize: 11, fontWeight: 600, marginBottom: 2 }}>Saved queries</div>
        <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 4 }}>
          One read-only SELECT each. A form calls a query by its key; <code>:name</code> in the query is
          filled from the form's inputs as a value, never as SQL. Credentials: <code>{"{\"username\", \"password\"}"}</code> —
          give a read-only database account.
        </div>
        {!cfg.engine && (
          <button type="button" disabled={disabled} style={{ fontSize: 11, padding: "3px 8px", cursor: "pointer", marginBottom: 4 }}
            onClick={() => onChange(JSON.stringify({ ...DB_STARTER, ...cfg }, null, 2))}>Fill database settings</button>
        )}
        {ops.map((op, i) => {
          const problem = (!/^[a-z][a-z0-9_]{0,39}$/.test(op.key) && "Key: lowercase letters, digits and _ (starts with a letter)")
            || (ops.filter(o => o.key === op.key).length > 1 && "Each key once") || queryProblem(op.sql ?? "");
          return (
            <div key={i} style={{ marginBottom: 6 }}>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1.4fr 20px", gap: 4, marginBottom: 2 }}>
                <input aria-label="Query key" placeholder="key" style={cell} value={op.key} disabled={disabled}
                  onChange={e => setQ(i, { key: e.target.value })} />
                <input aria-label="Query label" placeholder="Label" style={cell} value={op.label} disabled={disabled}
                  onChange={e => setQ(i, { label: e.target.value })} />
                <button type="button" aria-label="Remove query" disabled={disabled}
                  onClick={() => writeQ(ops.filter((_, j) => j !== i))}
                  style={{ ...cell, cursor: "pointer", padding: 0 }}>✕</button>
              </div>
              <textarea aria-label="Query SQL" placeholder="SELECT id, name FROM customers WHERE email = :email"
                style={{ ...cell, width: "100%", height: 54, fontFamily: "monospace", resize: "vertical", boxSizing: "border-box" }}
                value={op.sql ?? ""} disabled={disabled} onChange={e => setQ(i, { sql: e.target.value })} />
              {problem && <div style={{ fontSize: 10, color: "var(--status-failed)", marginTop: 2 }}>{problem}</div>}
            </div>
          );
        })}
        <button type="button" disabled={disabled || ops.length >= 50}
          onClick={() => writeQ([...ops, { key: `query_${ops.length + 1}`, label: "", method: "QUERY", path: "", sql: "SELECT " }])}
          style={{ fontSize: 11, padding: "3px 8px", cursor: "pointer" }}>+ Add query</button>
      </div>
    );
  }
  const base = connectorType === "http" ? "base_url" : "url";
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ fontSize: 11, fontWeight: 600, marginBottom: 2 }}>Operations</div>
      <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 4 }}>
        Named requests a form can call. Each path is added to the connector's <code>{base}</code>;
        {" {name}"} in a path is filled from the form's inputs (encoded — it cannot change the host).
        None: the connector makes its one request, as before.
      </div>
      {ops.map((op, i) => {
        const problem = operationProblem(op, ops);
        return (
          <div key={i} style={{ marginBottom: 4 }}>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1.2fr 70px 1.6fr 20px", gap: 4 }}>
              <input aria-label="Operation key" placeholder="key" style={cell} value={op.key} disabled={disabled}
                onChange={e => setOp(i, { key: e.target.value })} />
              <input aria-label="Operation label" placeholder="Label" style={cell} value={op.label} disabled={disabled}
                onChange={e => setOp(i, { label: e.target.value })} />
              <select aria-label="Operation method" style={cell} value={op.method} disabled={disabled}
                onChange={e => setOp(i, { method: e.target.value })}>
                {METHODS.map(m => <option key={m}>{m}</option>)}
              </select>
              <input aria-label="Operation path" placeholder="/customers/{id}" style={{ ...cell, fontFamily: "monospace" }}
                value={op.path} disabled={disabled} onChange={e => setOp(i, { path: e.target.value })} />
              <button type="button" aria-label="Remove operation" disabled={disabled}
                onClick={() => write(ops.filter((_, j) => j !== i))}
                style={{ ...cell, cursor: "pointer", padding: 0 }}>✕</button>
            </div>
            {problem && <div style={{ fontSize: 10, color: "var(--status-failed)", marginTop: 2 }}>{problem}</div>}
          </div>
        );
      })}
      <button type="button" disabled={disabled || ops.length >= 50}
        onClick={() => write([...ops, { key: `op_${ops.length + 1}`, label: "", method: "GET", path: "/" }])}
        style={{ fontSize: 11, padding: "3px 8px", cursor: "pointer" }}>+ Add operation</button>
    </div>
  );
}
