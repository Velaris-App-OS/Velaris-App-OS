/**
 * P67 — HxDBManager: AI-Powered Database Operations
 * Supabase-style DB panel inside Studio, powered by HxNexus.
 *
 * Tabs:
 *   Schema    — schema browser tree + table detail
 *   Table     — read-only paginated table viewer
 *   SQL       — SQL editor with results, EXPLAIN, history
 *   AI        — natural language → SQL via HxNexus
 *   Advisor   — slow queries + AI index recommendations
 */
import React, { useState, useEffect, useCallback, useMemo } from "react";
import { useAuth } from "@/auth";

const API = "/api/v1/hxdbmanager";

function _hdrs(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { "Content-Type": "application/json", Authorization: `Bearer ${t}` } : { "Content-Type": "application/json" };
}

type TabKey = "schema" | "table" | "sql" | "ai" | "advisor";

// ── Helpers ───────────────────────────────────────────────────────────────────

async function req<T = any>(method: string, path: string, body?: any): Promise<T> {
  const r = await fetch(API + path, {
    method,
    headers: _hdrs(),
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(d.detail ?? r.statusText);
  }
  return r.json();
}

function fmtMs(ms: number | null) { return ms == null ? "—" : ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(2)}s`; }
function fmtNum(n: number | null) { return n == null ? "—" : n.toLocaleString(); }
function fmtCountdown(s: number) { const m = Math.floor(s / 60), r = s % 60; return `${m}:${r.toString().padStart(2, "0")}`; }

// ── Pagination ────────────────────────────────────────────────────────────────
function PaginationBar({ page, totalPages, total, pageSize, onChange }: {
  page: number; totalPages: number; total: number; pageSize: number; onChange: (p: number) => void;
}) {
  const pages: (number | "…")[] = [];
  for (let i = 1; i <= totalPages; i++) {
    if (i === 1 || i === totalPages || Math.abs(i - page) <= 1) pages.push(i);
    else if (pages[pages.length - 1] !== "…") pages.push("…");
  }
  const btn: React.CSSProperties = {
    width: 28, height: 28, borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)",
    background: "transparent", color: "var(--text-secondary)", fontSize: 12, cursor: "pointer",
    fontFamily: "var(--font-mono)", display: "flex", alignItems: "center", justifyContent: "center",
  };
  return (
    <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
      <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginRight: 4, whiteSpace: "nowrap" }}>
        {total === 0 ? "0" : `${(page - 1) * pageSize + 1}–${Math.min(page * pageSize, total)}`} / {total}
      </span>
      <button onClick={() => onChange(page - 1)} disabled={page === 1} style={{ ...btn, opacity: page === 1 ? 0.35 : 1 }}>‹</button>
      {pages.map((p, i) => p === "…"
        ? <span key={`e${i}`} style={{ fontSize: 11, color: "var(--text-muted)", padding: "0 2px" }}>…</span>
        : <button key={p} onClick={() => onChange(p as number)} style={{ ...btn,
            background: page === p ? "var(--accent)" : "transparent",
            color: page === p ? "#fff" : "var(--text-secondary)",
            borderColor: page === p ? "var(--accent)" : "var(--border-default)" }}>{p}</button>
      )}
      <button onClick={() => onChange(page + 1)} disabled={page >= totalPages} style={{ ...btn, opacity: page >= totalPages ? 0.35 : 1 }}>›</button>
    </div>
  );
}

// ── Styles ────────────────────────────────────────────────────────────────────
const S = {
  page:    { display: "flex", flexDirection: "column" as const, height: "100%", overflow: "hidden" },
  tabBar:  { display: "flex", gap: 2, padding: "14px 24px 0", borderBottom: "1px solid var(--border)", flexShrink: 0 },
  tab:     { padding: "8px 18px", border: "none", background: "none", fontSize: 13, cursor: "pointer", borderBottom: "2px solid transparent", fontWeight: 400, color: "var(--text-secondary)", fontFamily: "var(--font-body)" } as React.CSSProperties,
  tabA:    { borderBottom: "2px solid var(--accent)", color: "var(--accent)", fontWeight: 700 } as React.CSSProperties,
  body:    { flex: 1, overflow: "auto", padding: "20px 24px" },
  card:    { background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 8, padding: 16, marginBottom: 14 } as React.CSSProperties,
  label:   { fontSize: 11, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase" as const, letterSpacing: "0.06em", marginBottom: 5 },
  input:   { width: "100%", padding: "8px 10px", background: "var(--bg-input)", border: "1px solid var(--border)", borderRadius: 6, color: "var(--text-primary)", fontSize: 13, fontFamily: "var(--font-body)", outline: "none", boxSizing: "border-box" as const },
  btn:     { padding: "7px 16px", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12, fontWeight: 600, fontFamily: "var(--font-body)" } as React.CSSProperties,
  btnPrimary: { background: "var(--accent)", color: "#fff" } as React.CSSProperties,
  btnGhost:   { background: "var(--bg-hover)", color: "var(--text-secondary)", border: "1px solid var(--border)" } as React.CSSProperties,
  mono:    { fontFamily: "var(--font-mono)", fontSize: 12 } as React.CSSProperties,
  err:     { color: "var(--status-failed)", fontSize: 13, padding: "10px 14px", background: "rgba(239,68,68,.08)", borderRadius: 6, marginBottom: 12 },
  tag:     { display: "inline-block", padding: "1px 7px", borderRadius: 4, fontSize: 10, fontWeight: 700 } as React.CSSProperties,
};

// ── Main component ────────────────────────────────────────────────────────────

export default function HxDBManager() {
  const [tab, setTab] = useState<TabKey>("schema");
  const [selectedTable, setSelectedTable] = useState<string | null>(null);

  const tabs: { key: TabKey; label: string }[] = [
    { key: "schema",  label: "Schema" },
    { key: "table",   label: "Table Viewer" },
    { key: "sql",     label: "SQL Editor" },
    { key: "ai",      label: "AI SQL" },
    { key: "advisor", label: "Advisor" },
  ];

  const openTable = (name: string) => { setSelectedTable(name); setTab("table"); };

  return (
    <div style={S.page}>
      <div style={S.tabBar}>
        {tabs.map(t => (
          <button key={t.key} style={{ ...S.tab, ...(tab === t.key ? S.tabA : {}) }} onClick={() => setTab(t.key)}>
            {t.label}
          </button>
        ))}
      </div>
      <div style={S.body}>
        {tab === "schema"  && <SchemaTab onOpenTable={openTable} />}
        {tab === "table"   && <TableTab initialTable={selectedTable} />}
        {tab === "sql"     && <SqlTab />}
        {tab === "ai"      && <AiTab />}
        {tab === "advisor" && <AdvisorTab />}
      </div>
    </div>
  );
}

// ── Schema Tab ────────────────────────────────────────────────────────────────

function SchemaTab({ onOpenTable }: { onOpenTable: (t: string) => void }) {
  const [tables, setTables] = useState<any[]>([]);
  const [selected, setSelected] = useState<any | null>(null);
  const [detail, setDetail] = useState<any | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    req("GET", "/schema").then(d => setTables(d.tables ?? [])).catch(e => setErr(e.message)).finally(() => setLoading(false));
  }, []);

  const selectTable = async (t: any) => {
    setSelected(t); setDetail(null);
    try { setDetail(await req("GET", `/schema/${t.table_name}`)); } catch {}
  };

  if (loading) return <div style={{ color: "var(--text-muted)", fontSize: 13 }}>Loading schema…</div>;
  if (err) return <div style={S.err}>{err}</div>;

  return (
    <div style={{ display: "grid", gridTemplateColumns: "260px 1fr", gap: 16, height: "100%" }}>
      {/* Table list */}
      <div style={{ ...S.card, overflowY: "auto", padding: 0 }}>
        <div style={{ padding: "10px 14px", borderBottom: "1px solid var(--border)", fontSize: 11, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase" }}>
          {tables.length} tables
        </div>
        {tables.map(t => (
          <div key={t.table_name} onClick={() => selectTable(t)}
            style={{ padding: "8px 14px", cursor: "pointer", fontSize: 13, borderBottom: "1px solid var(--border-subtle)",
              background: selected?.table_name === t.table_name ? "var(--bg-hover)" : "transparent",
              color: selected?.table_name === t.table_name ? "var(--accent)" : "var(--text-primary)" }}>
            <div style={{ fontWeight: 600 }}>{t.table_name}</div>
            <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
              {t.column_count} cols · {fmtNum(t.row_estimate)} rows · {t.total_size}
            </div>
          </div>
        ))}
      </div>

      {/* Detail */}
      <div style={{ overflowY: "auto" }}>
        {!selected && <div style={{ color: "var(--text-muted)", fontSize: 13, paddingTop: 40, textAlign: "center" }}>Select a table to inspect</div>}
        {selected && !detail && <div style={{ color: "var(--text-muted)", fontSize: 13 }}>Loading…</div>}
        {detail && (
          <>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
              <div style={{ fontWeight: 800, fontSize: 16, color: "var(--text-primary)" }}>{detail.table}</div>
              <button style={{ ...S.btn, ...S.btnPrimary }} onClick={() => onOpenTable(detail.table)}>Open in Table Viewer</button>
            </div>

            {/* Columns */}
            <div style={{ ...S.card }}>
              <div style={S.label}>Columns ({detail.columns.length})</div>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                <thead>
                  <tr style={{ borderBottom: "1px solid var(--border)" }}>
                    {["Column", "Type", "Nullable", "Default"].map(h => (
                      <th key={h} style={{ textAlign: "left", padding: "5px 8px", color: "var(--text-muted)", fontWeight: 700, fontSize: 10, textTransform: "uppercase" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {detail.columns.map((c: any) => (
                    <tr key={c.column_name} style={{ borderBottom: "1px solid var(--border-subtle)" }}>
                      <td style={{ padding: "6px 8px", fontWeight: 600, color: "var(--text-primary)", ...S.mono }}>{c.column_name}</td>
                      <td style={{ padding: "6px 8px", color: "var(--accent)", ...S.mono }}>{c.data_type}</td>
                      <td style={{ padding: "6px 8px", color: "var(--text-muted)" }}>{c.is_nullable === "YES" ? "null" : "not null"}</td>
                      <td style={{ padding: "6px 8px", color: "var(--text-muted)", ...S.mono }}>{c.column_default ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Indexes */}
            {detail.indexes.length > 0 && (
              <div style={S.card}>
                <div style={S.label}>Indexes ({detail.indexes.length})</div>
                {detail.indexes.map((idx: any) => (
                  <div key={idx.index_name} style={{ display: "flex", justifyContent: "space-between", padding: "5px 0", borderBottom: "1px solid var(--border-subtle)", fontSize: 12 }}>
                    <span style={{ ...S.mono, color: "var(--text-primary)" }}>{idx.index_name}</span>
                    <span style={{ color: "var(--text-muted)" }}>
                      {idx.is_primary && <span style={{ ...S.tag, background: "var(--accent)18", color: "var(--accent)", marginRight: 4 }}>PK</span>}
                      {idx.is_unique  && <span style={{ ...S.tag, background: "var(--bg-hover)", color: "var(--text-secondary)", marginRight: 4 }}>UNIQUE</span>}
                      {idx.size} · {fmtNum(idx.scans)} scans
                    </span>
                  </div>
                ))}
              </div>
            )}

            {/* Foreign keys */}
            {detail.foreign_keys.length > 0 && (
              <div style={S.card}>
                <div style={S.label}>Foreign Keys</div>
                {detail.foreign_keys.map((fk: any, i: number) => (
                  <div key={i} style={{ fontSize: 12, padding: "4px 0", borderBottom: "1px solid var(--border-subtle)", ...S.mono }}>
                    <span style={{ color: "var(--text-primary)" }}>{fk.column_name}</span>
                    <span style={{ color: "var(--text-muted)" }}> → {fk.foreign_table}.{fk.foreign_column} (ON DELETE {fk.delete_rule})</span>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// ── Table Viewer Tab ──────────────────────────────────────────────────────────

function TableTab({ initialTable }: { initialTable: string | null }) {
  const { user }                    = useAuth();
  const isSuperadmin                = user?.roles?.includes("superadmin") ?? false;

  const [tables, setTables]         = useState<string[]>([]);
  const [table, setTable]           = useState(initialTable ?? "");
  const [rows, setRows]             = useState<any[]>([]);
  const [total, setTotal]           = useState(0);
  const [page, setPage]             = useState(1);
  const [pageSize]                  = useState(50);
  const [sortCol, setSortCol]       = useState<string | null>(null);
  const [sortDir, setSortDir]       = useState<"asc" | "desc">("asc");
  const [filters, setFilters]       = useState<Record<string, string>>({});
  const [loading, setLoading]       = useState(false);
  const [err, setErr]               = useState<string | null>(null);
  const [masked, setMasked]         = useState(false);
  const [expose, setExpose]         = useState(false);

  // ── DBView — Tier 2 reveal for non-superadmin holders of db_manager.view_sensitive ──
  const [dbviewPrivilege, setDbviewPrivilege] = useState(false);
  const [dbviewExpiresIn, setDbviewExpiresIn] = useState(0);   // seconds remaining, 0 = not elevated
  const [showReauth, setShowReauth]           = useState(false);
  const [reauthPassword, setReauthPassword]   = useState("");
  const [reauthErr, setReauthErr]             = useState<string | null>(null);
  const [reauthBusy, setReauthBusy]           = useState(false);
  const dbviewElevated = dbviewExpiresIn > 0;

  const refreshDbviewStatus = useCallback(async () => {
    try {
      const d = await req("GET", "/reauth/status");
      setDbviewPrivilege(!!d.has_dbview_privilege);
      setDbviewExpiresIn(d.expires_in_seconds ?? 0);
    } catch { /* status check is best-effort */ }
  }, []);

  useEffect(() => { refreshDbviewStatus(); }, [refreshDbviewStatus]);

  // Countdown the elevation badge locally between status refreshes
  useEffect(() => {
    if (dbviewExpiresIn <= 0) return;
    const t = setInterval(() => setDbviewExpiresIn(s => Math.max(0, s - 1)), 1000);
    return () => clearInterval(t);
  }, [dbviewExpiresIn > 0]);

  useEffect(() => {
    req("GET", "/schema").then(d => setTables((d.tables ?? []).map((t: any) => t.table_name)));
  }, []);

  const load = useCallback(async (tbl: string, pg: number, sc: string | null, sd: "asc" | "desc", exp: boolean) => {
    if (!tbl) return;
    setLoading(true); setErr(null);
    try {
      let url = `/tables/${encodeURIComponent(tbl)}/rows?page=${pg}&page_size=${pageSize}`;
      if (sc) url += `&sort_col=${encodeURIComponent(sc)}&sort_dir=${sd}`;
      if (exp) url += `&expose=true`;
      const d = await req("GET", url);
      setRows(d.rows ?? []); setTotal(d.total ?? 0);
      setMasked(d.sensitive_cols_masked ?? false);
    } catch (e: any) { setErr(e.message); }
    finally { setLoading(false); }
  }, [pageSize]);

  useEffect(() => { if (table) load(table, page, sortCol, sortDir, expose); }, [table, page, sortCol, sortDir, expose, load]);

  const submitReauth = async () => {
    setReauthBusy(true); setReauthErr(null);
    try {
      const d = await req("POST", "/reauth", { password: reauthPassword });
      setDbviewExpiresIn(d.expires_in_seconds ?? 0);
      setShowReauth(false); setReauthPassword("");
      if (table) load(table, page, sortCol, sortDir, expose);   // re-fetch so Tier 2 reveals immediately
    } catch (e: any) { setReauthErr(e.message); }
    finally { setReauthBusy(false); }
  };

  const cols = rows.length > 0 ? Object.keys(rows[0]) : [];
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  // Client-side column filter applied on top of the server page
  const visibleRows = useMemo(() => {
    const active = Object.entries(filters).filter(([, v]) => v.trim());
    if (!active.length) return rows;
    return rows.filter(row =>
      active.every(([col, val]) =>
        String(row[col] ?? "").toLowerCase().includes(val.toLowerCase())
      )
    );
  }, [rows, filters]);

  const handleSort = (col: string) => {
    if (sortCol === col) { setSortDir(d => d === "asc" ? "desc" : "asc"); }
    else { setSortCol(col); setSortDir("asc"); }
    setPage(1);
  };

  const setFilter = (col: string, val: string) => {
    setFilters(prev => ({ ...prev, [col]: val }));
  };

  const exportTable = async (fmt: "csv" | "json") => {
    const url = `${API}/tables/${encodeURIComponent(table)}/export?fmt=${fmt}&limit=10000`;
    const r = await fetch(url, { headers: _hdrs() });
    const blob = await r.blob();
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
    a.download = `${table}.${fmt}`; a.click();
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {/* Toolbar */}
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <select value={table} onChange={e => { setTable(e.target.value); setPage(1); setSortCol(null); setFilters({}); setExpose(false); }}
          style={{ ...S.input, width: 240 }}>
          <option value="">— Select table —</option>
          {tables.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
        {table && !expose && <>
          <button style={{ ...S.btn, ...S.btnGhost }} onClick={() => exportTable("csv")}>Export CSV</button>
          <button style={{ ...S.btn, ...S.btnGhost }} onClick={() => exportTable("json")}>Export JSON</button>
        </>}
        {table && (masked || expose) && isSuperadmin && (
          <button
            onClick={() => setExpose(e => !e)}
            style={{ ...S.btn, background: expose ? "rgba(239,68,68,.15)" : "rgba(251,191,36,.12)",
              color: expose ? "var(--status-failed)" : "var(--status-running)",
              border: `1px solid ${expose ? "rgba(239,68,68,.3)" : "rgba(251,191,36,.3)"}` }}>
            {expose ? "Hide Sensitive Values" : "Expose Sensitive Values"}
          </button>
        )}
        {/* DBView — Tier 2 reveal for non-superadmin holders of db_manager.view_sensitive */}
        {table && !isSuperadmin && dbviewPrivilege && masked && !dbviewElevated && (
          <button onClick={() => { setReauthErr(null); setReauthPassword(""); setShowReauth(true); }}
            style={{ ...S.btn, background: "rgba(56,189,248,.12)", color: "var(--status-running)",
              border: "1px solid rgba(56,189,248,.3)" }}>
            Unlock Account Data (DBView)
          </button>
        )}
        {table && !isSuperadmin && dbviewElevated && (
          <span style={{ fontSize: 11, color: "var(--status-running)", background: "rgba(56,189,248,.1)", border: "1px solid rgba(56,189,248,.3)", borderRadius: 4, padding: "2px 8px" }}>
            DBView active — expires in {fmtCountdown(dbviewExpiresIn)}
          </span>
        )}
        {table && masked && !expose && !dbviewElevated && (
          <span style={{ fontSize: 11, color: "var(--status-running)", background: "rgba(251,191,36,.1)", border: "1px solid rgba(251,191,36,.3)", borderRadius: 4, padding: "2px 8px" }}>
            Sensitive columns masked
          </span>
        )}
        <span style={{ fontSize: 12, color: "var(--text-muted)", marginLeft: "auto" }}>{fmtNum(total)} rows total</span>
      </div>

      {/* Expose warning banner */}
      {expose && (
        <div style={{ background: "rgba(239,68,68,.08)", border: "1px solid rgba(239,68,68,.3)", borderRadius: 6, padding: "10px 14px", fontSize: 12, color: "var(--status-failed)" }}>
          Sensitive values are visible. This view is logged. Export is disabled while Expose is active — sensitive data cannot be written to a file.
        </div>
      )}

      {/* DBView elevation banner */}
      {!isSuperadmin && dbviewElevated && (
        <div style={{ background: "rgba(56,189,248,.08)", border: "1px solid rgba(56,189,248,.3)", borderRadius: 6, padding: "10px 14px", fontSize: 12, color: "var(--status-running)" }}>
          Account/financial data (Tier 2) is visible for {fmtCountdown(dbviewExpiresIn)}. This view is logged. Credentials and secrets (Tier 1) remain masked regardless.
        </div>
      )}

      {/* DBView re-authentication modal — "sudo mode" */}
      {showReauth && (
        <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.5)", display: "flex",
          alignItems: "center", justifyContent: "center", zIndex: 1000 }}
          onClick={() => !reauthBusy && setShowReauth(false)}>
          <div style={{ ...S.card, width: 360, marginBottom: 0 }} onClick={e => e.stopPropagation()}>
            <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 6 }}>Confirm your password</div>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 14 }}>
              Viewing account numbers, IBANs, and other Tier 2 data requires re-entering your
              password — like <code>sudo</code>. Access is unlocked for 15 minutes and is logged.
            </div>
            <input type="password" autoFocus value={reauthPassword}
              onChange={e => setReauthPassword(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter" && reauthPassword && !reauthBusy) submitReauth(); }}
              placeholder="Current password" style={{ ...S.input, marginBottom: 10 }} />
            {reauthErr && <div style={{ ...S.err, marginBottom: 10 }}>{reauthErr}</div>}
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button style={{ ...S.btn, ...S.btnGhost }} onClick={() => setShowReauth(false)} disabled={reauthBusy}>Cancel</button>
              <button style={{ ...S.btn, ...S.btnPrimary }} onClick={submitReauth} disabled={reauthBusy || !reauthPassword}>
                {reauthBusy ? "Verifying…" : "Unlock"}
              </button>
            </div>
          </div>
        </div>
      )}

      {err && <div style={S.err}>{err}</div>}
      {loading && <div style={{ color: "var(--text-muted)", fontSize: 13 }}>Loading…</div>}

      {!loading && rows.length > 0 && (
        <>
          <div style={{ overflowX: "auto", ...S.card, padding: 0 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                {/* Sort row */}
                <tr style={{ background: "var(--bg-elevated)" }}>
                  {cols.map(c => {
                    const isSorted = sortCol === c;
                    return (
                      <th key={c} onClick={() => handleSort(c)}
                        style={{ padding: "8px 10px", textAlign: "left", fontSize: 10, fontWeight: 700,
                          color: isSorted ? "var(--accent)" : "var(--text-muted)",
                          textTransform: "uppercase", borderBottom: "1px solid var(--border-subtle)",
                          whiteSpace: "nowrap", cursor: "pointer", userSelect: "none",
                          background: isSorted ? "color-mix(in srgb, var(--accent) 6%, transparent)" : undefined }}>
                        {c} {isSorted ? (sortDir === "asc" ? " ↑" : " ↓") : ""}
                      </th>
                    );
                  })}
                </tr>
                {/* Filter row */}
                <tr style={{ background: "var(--bg-elevated)" }}>
                  {cols.map(c => (
                    <th key={c} style={{ padding: "4px 6px", borderBottom: "1px solid var(--border)" }}>
                      <input
                        value={filters[c] ?? ""}
                        onChange={e => setFilter(c, e.target.value)}
                        placeholder="filter…"
                        style={{ width: "100%", padding: "3px 6px", fontSize: 11,
                          background: "var(--bg-input)", border: "1px solid var(--border)",
                          borderRadius: 4, color: "var(--text-primary)", outline: "none",
                          fontFamily: "var(--font-mono)", boxSizing: "border-box" as const }}
                      />
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {visibleRows.map((row, i) => (
                  <tr key={i}
                    style={{ borderBottom: "1px solid var(--border-subtle)",
                      background: i % 2 === 0 ? "transparent" : "color-mix(in srgb, var(--accent) 3%, transparent)" }}
                    onMouseEnter={e => (e.currentTarget.style.background = "color-mix(in srgb, var(--accent) 8%, transparent)")}
                    onMouseLeave={e => (e.currentTarget.style.background = i % 2 === 0 ? "transparent" : "color-mix(in srgb, var(--accent) 3%, transparent)")}>
                    {cols.map(c => (
                      <td key={c} title={row[c] == null ? "null" : String(row[c])}
                        style={{ padding: "6px 10px", maxWidth: 220, overflow: "hidden",
                          textOverflow: "ellipsis", whiteSpace: "nowrap", ...S.mono,
                          color: row[c] == null ? "var(--text-muted)" : row[c] === "••••••••" ? "var(--status-running)" : "var(--text-primary)" }}>
                        {row[c] == null ? "null" : String(row[c])}
                      </td>
                    ))}
                  </tr>
                ))}
                {visibleRows.length === 0 && (
                  <tr><td colSpan={cols.length} style={{ padding: "20px 10px", color: "var(--text-muted)", textAlign: "center" }}>
                    No rows match the current filters
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
              {Object.values(filters).some(v => v) && `${visibleRows.length} of ${rows.length} rows visible (filters active)`}
            </span>
            <PaginationBar page={page} totalPages={totalPages} total={total} pageSize={pageSize} onChange={p => setPage(p)} />
          </div>
        </>
      )}
      {!loading && !err && table && rows.length === 0 && (
        <div style={{ color: "var(--text-muted)", fontSize: 13, textAlign: "center", padding: 40 }}>No rows found</div>
      )}
    </div>
  );
}

// ── SQL Editor Tab ────────────────────────────────────────────────────────────

const HIST_PAGE_SIZE = 10;

function SqlTab() {
  const [sql, setSql]                   = useState("SELECT * FROM case_instances LIMIT 20;");
  const [result, setResult]             = useState<any | null>(null);
  const [explainResult, setExplainResult] = useState<any | null>(null);
  const [history, setHistory]           = useState<any[]>([]);
  const [histPage, setHistPage]         = useState(1);
  const [running, setRunning]           = useState(false);
  const [explaining, setExplaining]     = useState(false);
  const [err, setErr]                   = useState<string | null>(null);
  const [activeView, setActiveView]     = useState<"results" | "explain" | "history">("results");

  const loadHistory = useCallback(async () => {
    try { const d = await req("GET", "/history?limit=50"); setHistory(d.history ?? []); } catch {}
  }, []);

  useEffect(() => { loadHistory(); }, [loadHistory]);

  const run = async () => {
    setRunning(true); setErr(null); setResult(null); setActiveView("results");
    try {
      const d = await req("POST", "/execute", { sql, row_limit: 1000 });
      setResult(d); loadHistory();
    } catch (e: any) { setErr(e.message); }
    finally { setRunning(false); }
  };

  const explain = async () => {
    setExplaining(true); setErr(null); setExplainResult(null); setActiveView("explain");
    try {
      const d = await req("POST", "/explain", { sql });
      setExplainResult(d.plan);
    } catch (e: any) { setErr(e.message); }
    finally { setExplaining(false); }
  };

  const resultCols = result?.rows?.length > 0 ? Object.keys(result.rows[0]) : [];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {/* Editor */}
      <div style={S.card}>
        <textarea value={sql} onChange={e => setSql(e.target.value)}
          onKeyDown={e => { if (e.ctrlKey && e.key === "Enter") run(); }}
          rows={8} style={{ ...S.input, ...S.mono, resize: "vertical", minHeight: 120 }}
          placeholder="Enter SQL… (Ctrl+Enter to run)" />
        <div style={{ display: "flex", gap: 8, marginTop: 10, alignItems: "center" }}>
          <button style={{ ...S.btn, ...S.btnPrimary }} onClick={run} disabled={running}>
            {running ? "Running…" : "Run  (Ctrl+Enter)"}
          </button>
          <button style={{ ...S.btn, ...S.btnGhost }} onClick={explain} disabled={explaining}>
            {explaining ? "Explaining…" : "EXPLAIN"}
          </button>
          {result && (
            <span style={{ fontSize: 12, color: "var(--text-muted)", marginLeft: "auto" }}>
              {fmtNum(result.rows_returned)} rows · {fmtMs(result.duration_ms)}
              {result.truncated && " · truncated"}
            </span>
          )}
        </div>
      </div>

      {err && <div style={S.err}>{err}</div>}

      {/* View tabs */}
      <div style={{ display: "flex", gap: 4, borderBottom: "1px solid var(--border)", marginBottom: 4 }}>
        {(["results", "explain", "history"] as const).map(v => (
          <button key={v} onClick={() => setActiveView(v)}
            style={{ ...S.tab, ...(activeView === v ? S.tabA : {}), textTransform: "capitalize" }}>
            {v}
          </button>
        ))}
      </div>

      {/* Results */}
      {activeView === "results" && result && result.rows.length > 0 && (
        <div style={{ overflowX: "auto", ...S.card, padding: 0 }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr style={{ background: "var(--bg-base)" }}>
                {resultCols.map(c => (
                  <th key={c} style={{ padding: "7px 10px", textAlign: "left", fontSize: 10, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", borderBottom: "1px solid var(--border)", whiteSpace: "nowrap" }}>{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {result.rows.map((row: any, i: number) => (
                <tr key={i} style={{ borderBottom: "1px solid var(--border-subtle)" }}>
                  {resultCols.map(c => (
                    <td key={c} style={{ padding: "5px 10px", maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", ...S.mono, color: row[c] == null ? "var(--text-muted)" : "var(--text-primary)" }}>
                      {row[c] == null ? "null" : String(row[c])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {activeView === "results" && result && result.rows.length === 0 && (
        <div style={{ color: "var(--text-muted)", fontSize: 13, padding: "20px 0" }}>
          Query ran successfully — {result.rows_affected != null ? `${result.rows_affected} rows affected` : "no rows returned"}
        </div>
      )}

      {/* EXPLAIN */}
      {activeView === "explain" && explainResult && (
        <div style={{ ...S.card, ...S.mono, fontSize: 11, whiteSpace: "pre-wrap", maxHeight: 400, overflowY: "auto" }}>
          {JSON.stringify(explainResult, null, 2)}
        </div>
      )}

      {/* History */}
      {activeView === "history" && (() => {
        const totalHistPages = Math.max(1, Math.ceil(history.length / HIST_PAGE_SIZE));
        const histSlice = history.slice((histPage - 1) * HIST_PAGE_SIZE, histPage * HIST_PAGE_SIZE);
        return (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {history.length === 0 && <div style={{ color: "var(--text-muted)", fontSize: 13 }}>No query history yet</div>}
            {histSlice.map((h: any) => {
              const statusColor = h.status === "success" ? "var(--status-completed)" : h.status === "timeout" ? "var(--status-running)" : "var(--status-failed)";
              return (
                <div key={h.id} onClick={() => { setSql(h.query_text); setActiveView("results"); }}
                  style={{ ...S.card, cursor: "pointer", padding: "8px 12px",
                    borderLeft: `3px solid ${statusColor}` }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
                    <span style={{ ...S.tag,
                      background: `color-mix(in srgb, ${statusColor} 12%, transparent)`,
                      color: statusColor,
                      border: `1px solid color-mix(in srgb, ${statusColor} 25%, transparent)` }}>
                      {h.status}
                    </span>
                    <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                      {fmtMs(h.duration_ms)} · {fmtNum(h.rows_affected)} rows · {new Date(h.ran_at).toLocaleTimeString()}
                    </span>
                  </div>
                  <div style={{ ...S.mono, fontSize: 11, color: "var(--text-secondary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {h.query_text}
                  </div>
                </div>
              );
            })}
            {history.length > HIST_PAGE_SIZE && (
              <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 4 }}>
                <PaginationBar page={histPage} totalPages={totalHistPages} total={history.length} pageSize={HIST_PAGE_SIZE} onChange={setHistPage} />
              </div>
            )}
          </div>
        );
      })()}
    </div>
  );
}

// ── AI SQL Tab ────────────────────────────────────────────────────────────────

function AiTab() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<{ sql: string; explanation: string; estimated_rows: string } | null>(null);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [runResult, setRunResult] = useState<any | null>(null);
  const [running2, setRunning2] = useState(false);

  const generate = async () => {
    if (!question.trim()) return;
    setRunning(true); setErr(null); setResult(null); setRunResult(null);
    try { setResult(await req("POST", "/ai/sql", { question })); }
    catch (e: any) { setErr(e.message); }
    finally { setRunning(false); }
  };

  const runSql = async () => {
    if (!result?.sql) return;
    setRunning2(true); setRunResult(null);
    try { setRunResult(await req("POST", "/execute", { sql: result.sql, row_limit: 100 })); }
    catch (e: any) { setErr(e.message); }
    finally { setRunning2(false); }
  };

  const runResultCols = runResult?.rows?.length > 0 ? Object.keys(runResult.rows[0]) : [];

  return (
    <div>
      <div style={{ marginBottom: 16, fontSize: 13, color: "var(--text-muted)" }}>
        Ask HxNexus a question about your data in plain English. Your full schema is used as context.
      </div>

      <div style={S.card}>
        <div style={S.label}>Your question</div>
        <textarea value={question} onChange={e => setQuestion(e.target.value)}
          onKeyDown={e => { if (e.ctrlKey && e.key === "Enter") generate(); }}
          rows={3} style={{ ...S.input, resize: "vertical", marginBottom: 10 }}
          placeholder="e.g. Show me all open cases created in the last 7 days by tenant…" />
        <button style={{ ...S.btn, ...S.btnPrimary }} onClick={generate} disabled={running || !question.trim()}>
          {running ? "Generating…" : "Generate SQL"}
        </button>
      </div>

      {err && <div style={S.err}>{err}</div>}

      {result && (
        <div style={S.card}>
          <div style={S.label}>Generated SQL</div>
          <pre style={{ ...S.mono, fontSize: 12, background: "var(--bg-base)", padding: 12, borderRadius: 6, overflowX: "auto", marginBottom: 10 }}>
            {result.sql}
          </pre>
          <div style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 12 }}>
            <strong>Explanation:</strong> {result.explanation}
            {result.estimated_rows !== "unknown" && <span style={{ marginLeft: 8, color: "var(--text-muted)" }}>· ~{result.estimated_rows} rows</span>}
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <button style={{ ...S.btn, ...S.btnPrimary }} onClick={runSql} disabled={running2}>
              {running2 ? "Running…" : "Run this query"}
            </button>
          </div>

          {runResult && runResultCols.length > 0 && (
            <div style={{ overflowX: "auto", marginTop: 14, ...S.card, padding: 0 }}>
              <div style={{ padding: "6px 10px", borderBottom: "1px solid var(--border)", fontSize: 11, color: "var(--text-muted)" }}>
                {fmtNum(runResult.rows_returned)} rows · {fmtMs(runResult.duration_ms)}
              </div>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                <thead>
                  <tr>{runResultCols.map(c => <th key={c} style={{ padding: "6px 10px", textAlign: "left", fontSize: 10, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", borderBottom: "1px solid var(--border)" }}>{c}</th>)}</tr>
                </thead>
                <tbody>
                  {runResult.rows.map((row: any, i: number) => (
                    <tr key={i} style={{ borderBottom: "1px solid var(--border-subtle)" }}>
                      {runResultCols.map(c => <td key={c} style={{ padding: "5px 10px", ...S.mono, color: "var(--text-primary)", fontSize: 11 }}>{row[c] == null ? <span style={{ color: "var(--text-muted)" }}>null</span> : String(row[c])}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {runResult && runResultCols.length === 0 && (
            <div style={{ marginTop: 14, padding: "10px 12px", ...S.card, fontSize: 12, color: "var(--text-muted)" }}>
              0 rows found{runResult.duration_ms != null ? ` · ${fmtMs(runResult.duration_ms)}` : ""}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Advisor Tab ───────────────────────────────────────────────────────────────

function AdvisorTab() {
  const [slowQueries, setSlowQueries] = useState<any[]>([]);
  const [pgAvailable, setPgAvailable] = useState<boolean | null>(null);
  const [advice, setAdvice] = useState<any | null>(null);
  const [loadingAdvice, setLoadingAdvice] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    req("GET", "/slow-queries?limit=20").then(d => {
      setPgAvailable(d.available);
      setSlowQueries(d.queries ?? []);
    }).catch(() => setPgAvailable(false));
  }, []);

  const getAdvice = async () => {
    setLoadingAdvice(true); setErr(null); setAdvice(null);
    try { setAdvice(await req("GET", "/index-advisor")); }
    catch (e: any) { setErr(e.message); }
    finally { setLoadingAdvice(false); }
  };

  if (pgAvailable === null) return <div style={{ color: "var(--text-muted)", fontSize: 13 }}>Checking pg_stat_statements…</div>;

  if (!pgAvailable) return (
    <div style={{ ...S.card, borderLeft: "4px solid var(--status-warning)" }}>
      <div style={{ fontWeight: 700, marginBottom: 8 }}>pg_stat_statements not enabled</div>
      <div style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 12 }}>
        The Advisor tab requires <code>pg_stat_statements</code> to be enabled in your PostgreSQL configuration.
      </div>
      <pre style={{ ...S.mono, fontSize: 11, background: "var(--bg-base)", padding: 10, borderRadius: 6 }}>
        {`# Add to postgresql.conf:\nshared_preload_libraries = 'pg_stat_statements'\n\n# Then run:\nCREATE EXTENSION IF NOT EXISTS pg_stat_statements;`}
      </pre>
    </div>
  );

  return (
    <div>
      {/* Slow queries */}
      <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 12, color: "var(--text-primary)" }}>
        Top Slow Queries
      </div>
      <div style={{ ...S.card, padding: 0, marginBottom: 20 }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
          <thead>
            <tr style={{ background: "var(--bg-base)" }}>
              {["Query", "Calls", "Total Time", "Mean Time", "Rows"].map(h => (
                <th key={h} style={{ padding: "7px 10px", textAlign: "left", fontSize: 10, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", borderBottom: "1px solid var(--border)" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {slowQueries.map((q: any, i: number) => (
              <tr key={i} style={{ borderBottom: "1px solid var(--border-subtle)" }}>
                <td style={{ padding: "6px 10px", maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", ...S.mono, color: "var(--text-primary)" }}>{q.query}</td>
                <td style={{ padding: "6px 10px", color: "var(--text-secondary)" }}>{fmtNum(q.calls)}</td>
                <td style={{ padding: "6px 10px", color: "var(--text-secondary)" }}>{fmtMs(Math.round(q.total_exec_time))}</td>
                <td style={{ padding: "6px 10px", color: "var(--text-secondary)" }}>{fmtMs(Math.round(q.mean_exec_time))}</td>
                <td style={{ padding: "6px 10px", color: "var(--text-secondary)" }}>{fmtNum(q.rows)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* AI Index Advisor */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <div style={{ fontWeight: 700, fontSize: 14, color: "var(--text-primary)" }}>AI Index Advisor</div>
        <button style={{ ...S.btn, ...S.btnPrimary }} onClick={getAdvice} disabled={loadingAdvice}>
          {loadingAdvice ? "Analysing…" : "Get Recommendations"}
        </button>
      </div>

      {err && <div style={S.err}>{err}</div>}

      {advice?.recommendations?.map((r: any, i: number) => (
        <div key={i} style={{ ...S.card, borderLeft: "4px solid var(--accent)" }}>
          <div style={{ fontWeight: 700, marginBottom: 6, color: "var(--text-primary)" }}>
            {r.table} → <span style={{ color: "var(--accent)" }}>{r.column}</span>
            <span style={{ ...S.tag, background: "var(--bg-hover)", color: "var(--text-secondary)", marginLeft: 8 }}>{r.index_type}</span>
          </div>
          <div style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 8 }}>{r.reason}</div>
          {r.ddl_fix && (
            <pre style={{ ...S.mono, fontSize: 11, background: "var(--bg-base)", padding: 10, borderRadius: 6, marginBottom: 0 }}>
              {r.ddl_fix}
            </pre>
          )}
        </div>
      ))}
    </div>
  );
}
