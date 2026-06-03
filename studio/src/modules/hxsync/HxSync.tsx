/**
 * P29 — HxSync: Data Pipeline & Warehouse Bridge
 * Tabs: Destinations · Run History · Field Mapping · Health
 */
import React, { useState, useEffect, useCallback } from "react";

const API = "/api/v1/sync";
function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}
function authFetch(url: string, opts: RequestInit = {}): Promise<Response> {
  return fetch(url, { ...opts, headers: { ..._authHdr(), ...opts.headers } });
}


type Destination = {
  id: string; name: string; dest_type: string;
  connection_config: Record<string, string>; enabled: boolean;
  last_synced_at: string | null; last_sync_status: string;
};
type Run = {
  id: string; destination_id: string; status: string;
  rows_synced: number; error_msg: string | null;
  watermark_from: string | null; watermark_to: string | null;
  started_at: string | null; finished_at: string | null;
};
type FieldMapping = {
  id: string; destination_id: string; source_field: string;
  dest_column: string; transform: string; pii: boolean;
};
type RedactionRule = {
  id: string; destination_id: string; field_path: string;
  action: string; reason: string | null;
};
type HealthItem = Destination & { ok: boolean; message: string; latency_ms: number };

const DEST_TYPES = ["duckdb", "bigquery", "snowflake", "kafka", "kinesis", "pubsub"];
const TRANSFORMS  = ["passthrough", "seconds_to_hours", "to_string", "to_int"];

const STATUS_COLOR: Record<string, string> = {
  success: "#22c55e", error: "#ef4444", running: "#f59e0b", never: "#94a3b8",
};

const S: Record<string, React.CSSProperties> = {
  page:      { display: "flex", flexDirection: "column", height: "100%", background: "var(--bg-main)", color: "var(--text-primary)", fontFamily: "system-ui, sans-serif" },
  header:    { padding: "18px 24px 0", flexShrink: 0 },
  title:     { fontSize: 20, fontWeight: 700, margin: 0 },
  sub:       { fontSize: 13, color: "var(--text-secondary)", marginTop: 4 },
  tabBar:    { display: "flex", gap: 4, padding: "14px 24px 0", borderBottom: "1px solid var(--border)", flexShrink: 0 },
  tab:       { padding: "8px 20px", border: "none", background: "none", fontSize: 13, cursor: "pointer", borderBottom: "2px solid transparent", fontWeight: 400, color: "var(--text-secondary)" },
  tabActive: { borderBottom: "2px solid var(--accent)", color: "var(--accent)", fontWeight: 700 },
  body:      { flex: 1, overflow: "auto", padding: "20px 28px" },
  card:      { background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: 8, padding: "16px 20px", marginBottom: 12 },
  btn:       { padding: "7px 16px", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12, fontWeight: 700 },
  btnP:      { background: "var(--accent)", color: "#fff" },
  btnS:      { background: "var(--bg-surface)", border: "1px solid var(--border)", color: "var(--text-secondary)" },
  btnD:      { background: "#fee2e2", color: "#ef4444", border: "1px solid #fecaca" },
  input:     { width: "100%", padding: "7px 11px", border: "1px solid var(--border)", borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: "var(--bg-main)", color: "var(--text-primary)" },
  select:    { padding: "7px 11px", border: "1px solid var(--border)", borderRadius: 6, fontSize: 13, background: "var(--bg-main)", color: "var(--text-primary)", width: "100%" },
  label:     { fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", marginBottom: 4, display: "block" },
  row:       { display: "flex", gap: 10, alignItems: "center", marginBottom: 10 },
  badge:     { fontSize: 10, padding: "2px 8px", borderRadius: 10, fontWeight: 700 },
  tbl:       { width: "100%", borderCollapse: "collapse" as const, fontSize: 12 },
  th:        { textAlign: "left" as const, padding: "7px 10px", color: "var(--text-secondary)", fontWeight: 600, borderBottom: "1px solid var(--border)" },
  td:        { padding: "7px 10px", borderBottom: "1px solid var(--border)", verticalAlign: "middle" as const },
};

function StatusBadge({ status }: { status: string }) {
  const color = STATUS_COLOR[status] ?? "#94a3b8";
  return <span style={{ ...S.badge, background: color + "22", color }}>{status}</span>;
}

function fmtDate(s: string | null) {
  if (!s) return "—";
  return new Date(s).toLocaleString();
}


// ── Destinations tab ──────────────────────────────────────────────────────────

function DestinationsTab() {
  const [dests, setDests] = useState<Destination[]>([]);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: "", dest_type: "duckdb", config_raw: "{}" });
  const [testResult, setTestResult] = useState<Record<string, { ok: boolean; message: string }>>({});
  const [syncing, setSyncing] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState({ name: "", config_raw: "{}", enabled: true });

  const load = useCallback(async () => {
    const r = await authFetch(`${API}/destinations`);
    if (r.ok) setDests((await r.json()).destinations);
  }, []);

  useEffect(() => { load(); }, [load]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    let config: Record<string, string> = {};
    try { config = JSON.parse(form.config_raw); } catch { setMsg("Invalid JSON in connection config"); return; }
    const r = await authFetch(`${API}/destinations`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: form.name, dest_type: form.dest_type, connection_config: config }),
    });
    if (r.ok) { setCreating(false); setForm({ name: "", dest_type: "duckdb", config_raw: "{}" }); setMsg(null); await load(); }
    else { const err = await r.json(); setMsg(err.detail ?? "Create failed"); }
  }

  async function handleTest(d: Destination) {
    const r = await authFetch(`${API}/destinations/${d.id}/test`, { method: "POST" });
    if (r.ok) {
      const result = await r.json();
      setTestResult(prev => ({ ...prev, [d.id]: result }));
    }
  }

  async function handleSync(d: Destination) {
    setSyncing(d.id); setMsg(null);
    const r = await authFetch(`${API}/run/${d.id}/sync`, { method: "POST" });
    const data = await r.json();
    setSyncing(null);
    setMsg(data.status === "success"
      ? `✓ Synced ${data.rows_synced} rows`
      : `✗ Sync failed: ${data.error}`);
    await load();
  }

  function startEdit(d: Destination) {
    setEditingId(d.id);
    setEditForm({
      name: d.name,
      config_raw: JSON.stringify(d.connection_config || {}, null, 2),
      enabled: d.enabled,
    });
    setMsg(null);
  }

  async function handleEdit(e: React.FormEvent) {
    e.preventDefault();
    if (!editingId) return;
    let connection_config: Record<string, string> = {};
    try { connection_config = JSON.parse(editForm.config_raw); } catch { setMsg("Invalid JSON in connection config"); return; }
    const r = await authFetch(`${API}/destinations/${editingId}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: editForm.name, connection_config, enabled: editForm.enabled }),
    });
    if (r.ok) { setEditingId(null); setMsg(null); await load(); }
    else { const err = await r.json(); setMsg(err.detail ?? "Update failed"); }
  }

  async function handleDelete(d: Destination) {
    await authFetch(`${API}/destinations/${d.id}`, { method: "DELETE" });
    await load();
  }

  return (
    <div style={S.body}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <h2 style={{ margin: 0, fontSize: 15, fontWeight: 700 }}>Sync Destinations</h2>
        <button style={{ ...S.btn, ...S.btnP }} onClick={() => setCreating(v => !v)}>
          {creating ? "✕ Cancel" : "+ Add Destination"}
        </button>
      </div>

      {msg && <div style={{ fontSize: 12, marginBottom: 12, color: msg.startsWith("✓") ? "#22c55e" : "#ef4444" }}>{msg}</div>}

      {creating && (
        <div style={{ ...S.card, marginBottom: 20 }}>
          <form onSubmit={handleCreate}>
            <div style={S.row}>
              <div style={{ flex: 2 }}>
                <label style={S.label}>Name</label>
                <input style={S.input} value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} required placeholder="My BigQuery DWH" />
              </div>
              <div style={{ flex: 1 }}>
                <label style={S.label}>Type</label>
                <select style={S.select} value={form.dest_type} onChange={e => setForm(f => ({ ...f, dest_type: e.target.value }))}>
                  {DEST_TYPES.map(t => <option key={t}>{t}</option>)}
                </select>
              </div>
            </div>
            <div style={{ marginBottom: 12 }}>
              <label style={S.label}>Connection Config (JSON)</label>
              <textarea style={{ ...S.input, height: 80, resize: "vertical", fontFamily: "monospace" }}
                value={form.config_raw} onChange={e => setForm(f => ({ ...f, config_raw: e.target.value }))} />
              <div style={{ fontSize: 10, color: "var(--text-secondary)", marginTop: 4 }}>
                e.g. BigQuery: {`{"project_id":"my-proj","dataset_id":"helix"}`} · Kafka: {`{"brokers":"localhost:9092"}`} · DuckDB: {`{"path":"/data/helix.duckdb"}`}
              </div>
            </div>
            <button type="submit" style={{ ...S.btn, ...S.btnP }}>Create</button>
          </form>
        </div>
      )}

      {dests.length === 0 && !creating && (
        <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>No destinations configured. Add one to start syncing.</div>
      )}

      {dests.map(d => (
        <div key={d.id} style={S.card}>
          <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 10 }}>
            <span style={{ fontSize: 13, fontWeight: 700, flex: 1 }}>{d.name}</span>
            <span style={{ ...S.badge, background: "#e0e7ff", color: "#0d9488" }}>{d.dest_type}</span>
            <StatusBadge status={d.last_sync_status ?? "never"} />
            {!d.enabled && <span style={{ ...S.badge, background: "#94a3b822", color: "#94a3b8" }}>disabled</span>}
            <button style={{ ...S.btn, ...S.btnS }} onClick={() => handleTest(d)}>Test</button>
            <button style={{ ...S.btn, ...S.btnP }} disabled={syncing === d.id} onClick={() => handleSync(d)}>
              {syncing === d.id ? "Syncing…" : "▶ Sync Now"}
            </button>
            <button style={{ ...S.btn, ...S.btnS }} onClick={() => editingId === d.id ? setEditingId(null) : startEdit(d)}>
              {editingId === d.id ? "Cancel" : "Edit"}
            </button>
            <button style={{ ...S.btn, ...S.btnD }} onClick={() => handleDelete(d)}>Delete</button>
          </div>
          <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>
            Last sync: {fmtDate(d.last_synced_at)}
            {d.connection_config && Object.keys(d.connection_config).length > 0 && (
              <span style={{ marginLeft: 16 }}>Config: {Object.keys(d.connection_config).join(", ")}</span>
            )}
          </div>
          {testResult[d.id] && (
            <div style={{ marginTop: 8, fontSize: 11, color: testResult[d.id].ok ? "#22c55e" : "#ef4444" }}>
              {testResult[d.id].ok ? "✓" : "✗"} {testResult[d.id].message}
            </div>
          )}

          {/* Inline edit form */}
          {editingId === d.id && (
            <form onSubmit={handleEdit} style={{ marginTop: 14, paddingTop: 14, borderTop: "1px solid var(--border)" }}>
              {msg && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8 }}>{msg}</div>}
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 10 }}>
                <div>
                  <label style={S.label}>Name</label>
                  <input style={S.input} value={editForm.name} onChange={e => setEditForm(f => ({ ...f, name: e.target.value }))} required />
                </div>
                <div style={{ display: "flex", alignItems: "center", paddingTop: 18 }}>
                  <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 12, cursor: "pointer" }}>
                    <input type="checkbox" checked={editForm.enabled} onChange={e => setEditForm(f => ({ ...f, enabled: e.target.checked }))} />
                    Enabled
                  </label>
                </div>
              </div>
              <div style={{ marginBottom: 12 }}>
                <label style={S.label}>Connection Config (JSON)</label>
                <textarea style={{ ...S.input, height: 80, resize: "vertical", fontFamily: "monospace" }}
                  value={editForm.config_raw} onChange={e => setEditForm(f => ({ ...f, config_raw: e.target.value }))} />
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <button type="submit" style={{ ...S.btn, ...S.btnP }}>Save Changes</button>
                <button type="button" style={{ ...S.btn, ...S.btnS }} onClick={() => setEditingId(null)}>Cancel</button>
              </div>
            </form>
          )}
        </div>
      ))}
    </div>
  );
}


// ── Run History tab ───────────────────────────────────────────────────────────

function RunHistoryTab() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [dests, setDests] = useState<Destination[]>([]);
  const [filter, setFilter] = useState("");

  const load = useCallback(async () => {
    const [rr, dr] = await Promise.all([authFetch(`${API}/runs`), authFetch(`${API}/destinations`)]);
    if (rr.ok) setRuns((await rr.json()).runs);
    if (dr.ok) setDests((await dr.json()).destinations);
  }, []);

  useEffect(() => { load(); }, [load]);

  const destName = (id: string) => dests.find(d => d.id === id)?.name ?? id.slice(0, 8);
  const filtered = filter ? runs.filter(r => r.destination_id === filter) : runs;

  return (
    <div style={S.body}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <h2 style={{ margin: 0, fontSize: 15, fontWeight: 700 }}>Run History</h2>
        <div style={{ display: "flex", gap: 8 }}>
          <select style={{ ...S.select, width: 200 }} value={filter} onChange={e => setFilter(e.target.value)}>
            <option value="">All destinations</option>
            {dests.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}
          </select>
          <button style={{ ...S.btn, ...S.btnS }} onClick={load}>↻</button>
        </div>
      </div>
      {filtered.length === 0
        ? <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>No runs yet.</div>
        : (
          <table style={S.tbl}>
            <thead>
              <tr>
                {["Destination", "Status", "Rows Synced", "Started", "Finished", "Error"].map(h => (
                  <th key={h} style={S.th}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.map(r => (
                <tr key={r.id}>
                  <td style={S.td}>{destName(r.destination_id)}</td>
                  <td style={S.td}><StatusBadge status={r.status} /></td>
                  <td style={S.td}>{r.rows_synced}</td>
                  <td style={S.td}>{fmtDate(r.started_at)}</td>
                  <td style={S.td}>{fmtDate(r.finished_at)}</td>
                  <td style={{ ...S.td, color: "#ef4444", fontSize: 11 }}>{r.error_msg ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
    </div>
  );
}


// ── Field Mapping tab ─────────────────────────────────────────────────────────

function FieldMappingTab() {
  const [dests, setDests] = useState<Destination[]>([]);
  const [selDest, setSelDest] = useState("");
  const [mappings, setMappings] = useState<FieldMapping[]>([]);
  const [redactions, setRedactions] = useState<RedactionRule[]>([]);
  const [mForm, setMForm] = useState({ source_field: "", dest_column: "", transform: "passthrough", pii: false });
  const [rForm, setRForm] = useState({ field_path: "", action: "hash", reason: "" });

  const loadDests = useCallback(async () => {
    const r = await authFetch(`${API}/destinations`);
    if (r.ok) setDests((await r.json()).destinations);
  }, []);

  const loadMappings = useCallback(async (id: string) => {
    const [mr, rr] = await Promise.all([
      authFetch(`${API}/destinations/${id}/field-mappings`),
      authFetch(`${API}/destinations/${id}/redaction-rules`),
    ]);
    if (mr.ok) setMappings((await mr.json()).mappings);
    if (rr.ok) setRedactions((await rr.json()).rules);
  }, []);

  useEffect(() => { loadDests(); }, [loadDests]);
  useEffect(() => { if (selDest) loadMappings(selDest); }, [selDest, loadMappings]);

  async function addMapping(e: React.FormEvent) {
    e.preventDefault();
    await authFetch(`${API}/destinations/${selDest}/field-mappings`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(mForm),
    });
    setMForm({ source_field: "", dest_column: "", transform: "passthrough", pii: false });
    await loadMappings(selDest);
  }

  async function delMapping(id: string) {
    await authFetch(`${API}/field-mappings/${id}`, { method: "DELETE" });
    await loadMappings(selDest);
  }

  async function addRedaction(e: React.FormEvent) {
    e.preventDefault();
    await authFetch(`${API}/destinations/${selDest}/redaction-rules`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...rForm, reason: rForm.reason || null }),
    });
    setRForm({ field_path: "", action: "hash", reason: "" });
    await loadMappings(selDest);
  }

  async function delRedaction(id: string) {
    await authFetch(`${API}/redaction-rules/${id}`, { method: "DELETE" });
    await loadMappings(selDest);
  }

  return (
    <div style={S.body}>
      <div style={{ marginBottom: 16 }}>
        <label style={S.label}>Select Destination</label>
        <select style={{ ...S.select, maxWidth: 300 }} value={selDest} onChange={e => setSelDest(e.target.value)}>
          <option value="">— choose —</option>
          {dests.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}
        </select>
      </div>

      {!selDest && <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>Select a destination to configure field mappings and redaction rules.</div>}

      {selDest && (
        <>
          {/* Field mappings */}
          <div style={S.card}>
            <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 12 }}>Field Mappings</div>
            <form onSubmit={addMapping} style={{ display: "flex", gap: 8, marginBottom: 12, flexWrap: "wrap" }}>
              <input style={{ ...S.input, flex: 1, minWidth: 120 }} placeholder="source_field" value={mForm.source_field}
                onChange={e => setMForm(f => ({ ...f, source_field: e.target.value }))} required />
              <input style={{ ...S.input, flex: 1, minWidth: 120 }} placeholder="dest_column" value={mForm.dest_column}
                onChange={e => setMForm(f => ({ ...f, dest_column: e.target.value }))} required />
              <select style={{ ...S.select, width: 160 }} value={mForm.transform}
                onChange={e => setMForm(f => ({ ...f, transform: e.target.value }))}>
                {TRANSFORMS.map(t => <option key={t}>{t}</option>)}
              </select>
              <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, whiteSpace: "nowrap" }}>
                <input type="checkbox" checked={mForm.pii} onChange={e => setMForm(f => ({ ...f, pii: e.target.checked }))} /> PII
              </label>
              <button type="submit" style={{ ...S.btn, ...S.btnP }}>+ Add</button>
            </form>
            {mappings.length === 0
              ? <div style={{ color: "var(--text-secondary)", fontSize: 12 }}>No mappings. Default schema is used.</div>
              : (
                <table style={S.tbl}>
                  <thead><tr>{["Source Field", "Dest Column", "Transform", "PII", ""].map(h => <th key={h} style={S.th}>{h}</th>)}</tr></thead>
                  <tbody>
                    {mappings.map(m => (
                      <tr key={m.id}>
                        <td style={S.td}><code>{m.source_field}</code></td>
                        <td style={S.td}><code>{m.dest_column}</code></td>
                        <td style={S.td}>{m.transform}</td>
                        <td style={S.td}>{m.pii ? <span style={{ color: "#f59e0b" }}>⚠ PII</span> : "—"}</td>
                        <td style={S.td}><button style={{ ...S.btn, ...S.btnD }} onClick={() => delMapping(m.id)}>✕</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
          </div>

          {/* Redaction rules */}
          <div style={S.card}>
            <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 12 }}>GDPR Redaction Rules</div>
            <form onSubmit={addRedaction} style={{ display: "flex", gap: 8, marginBottom: 12, flexWrap: "wrap" }}>
              <input style={{ ...S.input, flex: 1, minWidth: 140 }} placeholder="field_path (e.g. created_by)" value={rForm.field_path}
                onChange={e => setRForm(f => ({ ...f, field_path: e.target.value }))} required />
              <select style={{ ...S.select, width: 100 }} value={rForm.action}
                onChange={e => setRForm(f => ({ ...f, action: e.target.value }))}>
                {["hash", "drop", "mask"].map(a => <option key={a}>{a}</option>)}
              </select>
              <input style={{ ...S.input, flex: 1, minWidth: 140 }} placeholder="reason (optional)" value={rForm.reason}
                onChange={e => setRForm(f => ({ ...f, reason: e.target.value }))} />
              <button type="submit" style={{ ...S.btn, ...S.btnP }}>+ Add</button>
            </form>
            {redactions.length === 0
              ? <div style={{ color: "var(--text-secondary)", fontSize: 12 }}>No redaction rules. All fields are synced as-is.</div>
              : (
                <table style={S.tbl}>
                  <thead><tr>{["Field Path", "Action", "Reason", ""].map(h => <th key={h} style={S.th}>{h}</th>)}</tr></thead>
                  <tbody>
                    {redactions.map(r => (
                      <tr key={r.id}>
                        <td style={S.td}><code>{r.field_path}</code></td>
                        <td style={S.td}><StatusBadge status={r.action} /></td>
                        <td style={{ ...S.td, color: "var(--text-secondary)", fontSize: 11 }}>{r.reason ?? "—"}</td>
                        <td style={S.td}><button style={{ ...S.btn, ...S.btnD }} onClick={() => delRedaction(r.id)}>✕</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
          </div>
        </>
      )}
    </div>
  );
}


// ── Health tab ────────────────────────────────────────────────────────────────

function HealthTab() {
  const [items, setItems] = useState<HealthItem[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const r = await authFetch(`${API}/health`);
    if (r.ok) setItems((await r.json()).health);
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  if (loading) return <div style={{ ...S.body, color: "var(--text-secondary)", fontSize: 13 }}>Loading…</div>;

  return (
    <div style={S.body}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <h2 style={{ margin: 0, fontSize: 15, fontWeight: 700 }}>Destination Health</h2>
        <button style={{ ...S.btn, ...S.btnS }} onClick={load}>↻ Refresh</button>
      </div>
      {items.length === 0
        ? <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>No destinations configured.</div>
        : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gap: 12 }}>
            {items.map(item => (
              <div key={item.id} style={{ ...S.card, borderLeft: `3px solid ${item.ok ? "#22c55e" : "#ef4444"}`, marginBottom: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
                  <span style={{ fontSize: 13, fontWeight: 700, flex: 1 }}>{item.name}</span>
                  <span style={{ ...S.badge, background: "#e0e7ff", color: "#0d9488" }}>{item.dest_type}</span>
                  <span style={{ fontSize: 11, color: item.ok ? "#22c55e" : "#ef4444", fontWeight: 700 }}>
                    {item.ok ? "● Online" : "● Offline"}
                  </span>
                </div>
                <div style={{ fontSize: 11, color: "var(--text-secondary)", marginBottom: 4 }}>{item.message}</div>
                <div style={{ display: "flex", gap: 16, fontSize: 11 }}>
                  <span>Latency: <b>{item.latency_ms}ms</b></span>
                  <span>Last sync: <b>{item.last_sync_status}</b></span>
                </div>
                {item.last_synced_at && (
                  <div style={{ fontSize: 10, color: "var(--text-secondary)", marginTop: 4 }}>{fmtDate(item.last_synced_at)}</div>
                )}
              </div>
            ))}
          </div>
        )}
    </div>
  );
}


// ── Root ──────────────────────────────────────────────────────────────────────

export default function HxSync() {
  const [tab, setTab] = useState<"destinations" | "runs" | "mapping" | "health">("destinations");
  const tabs = [
    { key: "destinations" as const, label: "Destinations" },
    { key: "runs"         as const, label: "Run History" },
    { key: "mapping"      as const, label: "Field Mapping" },
    { key: "health"       as const, label: "Health" },
  ];
  return (
    <div style={S.page}>
      <div style={S.tabBar}>
        {tabs.map(t => (
          <button key={t.key} onClick={() => setTab(t.key)}
            style={{ ...S.tab, ...(tab === t.key ? S.tabActive : {}) }}>{t.label}</button>
        ))}
      </div>
      <div style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column" }}>
        {tab === "destinations" && <DestinationsTab />}
        {tab === "runs"         && <RunHistoryTab />}
        {tab === "mapping"      && <FieldMappingTab />}
        {tab === "health"       && <HealthTab />}
      </div>
    </div>
  );
}
