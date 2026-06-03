/**
 * P28 — HxBridge: Connector Registry, call history, DLQ, and webhook receiver.
 * Tabs: Connectors · Call History · Dead Letter Queue
 */
import React, { useState, useEffect, useCallback } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import { useBranchMode, useCurrentUserGroups } from "@shared/hooks";
import { BranchModeBanner, ReviewerPicker } from "@shared/components";
import { createBranch } from "@shared/api/client";

const API = "/api/v1/hxbridge";

function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}
function authFetch(url: string, opts: RequestInit = {}): Promise<Response> {
  return fetch(url, { ...opts, headers: { ..._authHdr(), ...opts.headers } });
}

type ConnectorType = { connector_type: string; display_name: string; description: string; config_schema: any; credential_schema: any };
type Connector = { id: string; name: string; connector_type: string; description: string | null; enabled: boolean; last_tested_at: string | null; last_test_ok: boolean | null; created_by: string | null; created_at: string | null; config?: any; credentials?: any; credential_expires_at: string | null; credentials_updated_at: string | null };
type Call = { id: string; connector_id: string | null; case_id: string | null; step_id: string | null; status: string; latency_ms: number | null; error: string | null; retry_count: number; created_at: string | null };
type DLQItem = { id: string; connector_id: string | null; error: string | null; retry_count: number; max_retries: number; next_retry_at: string | null; resolution: string | null; created_at: string | null };

const STATUS_COLOR: Record<string, string> = { success: "#22c55e", failed: "#ef4444", running: "#3b82f6", pending: "#94a3b8", retrying: "#f59e0b" };
const S: Record<string, React.CSSProperties> = {
  page:      { display: "flex", flexDirection: "column", height: "100%", background: "var(--bg-main)", color: "var(--text-primary)", fontFamily: "system-ui, sans-serif" },
  header:    { padding: "18px 24px 0", flexShrink: 0 },
  title:     { fontSize: 20, fontWeight: 700, margin: 0 },
  sub:       { fontSize: 13, color: "var(--text-secondary)", marginTop: 4 },
  tabs:      { display: "flex", gap: 4, padding: "14px 24px 0", borderBottom: "1px solid var(--border)", flexShrink: 0 },
  tab:       { padding: "8px 20px", border: "none", background: "none", fontSize: 13, cursor: "pointer", borderBottom: "2px solid transparent", fontWeight: 400, color: "var(--text-secondary)" },
  tabActive: { borderBottom: "2px solid var(--accent)", color: "var(--accent)", fontWeight: 700 },
  body:      { flex: 1, overflow: "hidden", display: "flex" },
  sidebar:   { width: 260, borderRight: "1px solid var(--border)", display: "flex", flexDirection: "column", background: "var(--bg-surface)", flexShrink: 0 },
  sideHead:  { padding: "12px 14px", borderBottom: "1px solid var(--border)", display: "flex", alignItems: "center", gap: 8, flexShrink: 0, fontSize: 12, fontWeight: 700, color: "var(--text-secondary)" },
  list:      { flex: 1, overflow: "auto" },
  detail:    { flex: 1, overflow: "auto", padding: "24px 28px" },
  btn:       { padding: "7px 16px", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12, fontWeight: 700 },
  btnPrimary:{ background: "var(--accent)", color: "#fff" },
  btnSecond: { background: "var(--bg-surface)", border: "1px solid var(--border)", color: "var(--text-secondary)" },
  badge:     { display: "inline-block", padding: "2px 8px", borderRadius: 10, fontSize: 10, fontWeight: 700, textTransform: "uppercase" as const },
  input:     { width: "100%", padding: "7px 10px", border: "1px solid var(--border)", borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: "var(--bg-main)", color: "var(--text-primary)" },
  label:     { fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", marginBottom: 4, display: "block" },
  card:      { background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: 8, padding: "14px 18px", marginBottom: 12 },
};

// ── Connectors tab ────────────────────────────────────────────────────────────

function ConnectorsTab() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const branchId = searchParams.get("branch");
  const branchMode = useBranchMode(branchId);
  const myGroups = useCurrentUserGroups();

  const [connectorTypes, setConnectorTypes] = useState<ConnectorType[]>([]);
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [selected, setSelected] = useState<Connector | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: "", connector_type: "http", description: "", config: "{}", credentials: "{}" });
  const [sandboxInput, setSandboxInput] = useState("{}");
  const [sandboxResult, setSandboxResult] = useState<any>(null);
  const [testResult, setTestResult] = useState<{ ok: boolean; error?: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [rotateMsg, setRotateMsg] = useState<{ ok: boolean; text: string } | null>(null);
  // Branch mode: editable config JSON (credentials NEVER included)
  const [branchConfigStr, setBranchConfigStr] = useState("{}");
  const [branchConfigErr, setBranchConfigErr] = useState<string | null>(null);
  // Branch creation inline state
  const [branchingId, setBranchingId]       = useState<string | null>(null);
  const [branchName, setBranchName]         = useState("");
  const [branchReviewer, setBranchReviewer] = useState("");
  const [branchBusy, setBranchBusy]         = useState(false);
  const [branchCreated, setBranchCreated]   = useState<any>(null);
  const [branchCreateErr, setBranchCreateErr] = useState<string | null>(null);

  function openBranchForm(c: Connector) {
    const slug = c.name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
    setBranchingId(c.id);
    setBranchName(`fix/${slug}`);
    setBranchReviewer(""); setBranchCreated(null); setBranchCreateErr(null);
  }

  async function handleBranchCreate(connId: string, connName: string) {
    if (!branchName.trim()) return;
    setBranchBusy(true); setBranchCreateErr(null);
    try {
      const b = await createBranch({
        name: branchName.trim(),
        artifact_type: "integration",
        artifact_id: connId,
        description: `Branch of connector "${connName}"`,
        assigned_reviewer_id: branchReviewer.trim() || undefined,
      });
      setBranchCreated(b);
    } catch (e: any) { setBranchCreateErr(e.message); }
    finally { setBranchBusy(false); }
  }

  const load = useCallback(async () => {
    const [typesRes, connsRes] = await Promise.all([
      authFetch(`${API}/connector-types`), authFetch(`${API}/connectors`),
    ]);
    if (typesRes.ok) setConnectorTypes((await typesRes.json()).connector_types);
    if (connsRes.ok) setConnectors((await connsRes.json()).connectors);
  }, []);

  useEffect(() => { load(); }, [load]);

  // Auto-select the connector the branch belongs to when branch + connector list both load
  useEffect(() => {
    if (!branchMode.branch?.artifact_id || !connectors.length) return;
    const c = connectors.find(c => c.id === branchMode.branch.artifact_id);
    if (!c) return;
    loadDetail(c);
    const snap = branchMode.branch.content_snapshot;
    setBranchConfigStr(JSON.stringify(snap?.config ?? {}, null, 2));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [branchMode.branch?.artifact_id, connectors.length]);

  async function loadDetail(c: Connector) {
    const r = await authFetch(`${API}/connectors/${c.id}`);
    if (r.ok) setSelected(await r.json());
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault(); setLoading(true); setErr(null);
    try {
      const config = JSON.parse(form.config);
      const credentials = JSON.parse(form.credentials);
      const r = await authFetch(`${API}/connectors`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: form.name, connector_type: form.connector_type, description: form.description, config, credentials }),
      });
      if (!r.ok) { setErr((await r.json()).detail || "Error"); return; }
      setCreating(false); setForm({ name: "", connector_type: "http", description: "", config: "{}", credentials: "{}" });
      await load();
    } catch (ex: any) { setErr(ex.message); }
    finally { setLoading(false); }
  }

  async function handleTest(c: Connector) {
    setTestResult(null);
    const r = await authFetch(`${API}/connectors/${c.id}/test`, { method: "POST" });
    if (r.ok) setTestResult(await r.json());
    await load();
  }

  async function handleSandbox(c: Connector) {
    setSandboxResult(null);
    try {
      const input_data = JSON.parse(sandboxInput);
      const r = await authFetch(`${API}/connectors/${c.id}/execute`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input_data }),
      });
      setSandboxResult(await r.json());
    } catch (ex: any) { setSandboxResult({ ok: false, error: ex.message }); }
  }

  async function handleDelete(c: Connector) {
    await authFetch(`${API}/connectors/${c.id}`, { method: "DELETE" });
    setSelected(null); await load();
  }

  async function handleToggle(c: Connector) {
    await authFetch(`${API}/connectors/${c.id}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !c.enabled }),
    });
    await load(); if (selected?.id === c.id) loadDetail({ ...c, enabled: !c.enabled });
  }

  async function handleRotate(c: Connector) {
    if (!window.confirm(`This will clear stored credentials for '${c.name}'. You will need to re-enter them. Continue?`)) return;
    setRotateMsg(null);
    try {
      const r = await authFetch(`${API}/connectors/${c.id}/rotate-credentials`, { method: "POST" });
      const data = await r.json();
      if (r.ok) {
        setRotateMsg({ ok: true, text: "Credentials cleared — re-enter via Edit" });
        await load();
        if (selected?.id === c.id) loadDetail(c);
      } else {
        setRotateMsg({ ok: false, text: data.detail || "Rotation failed" });
      }
    } catch (ex: any) {
      setRotateMsg({ ok: false, text: ex.message });
    }
  }

  async function handleSaveToBranch() {
    if (!selected) return;
    setBranchConfigErr(null);
    let config: any;
    try {
      config = JSON.parse(branchConfigStr);
    } catch {
      setBranchConfigErr("Invalid JSON — fix the config before saving.");
      return;
    }
    // SECURITY: only config, name, enabled — NEVER credentials
    await branchMode.patchContent({ config, name: selected.name, enabled: selected.enabled });
  }

  const selectedType = connectorTypes.find(t => t.connector_type === form.connector_type);

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, overflow: "hidden" }}>
    {branchMode.isBranchMode && (
      <BranchModeBanner
        branch={branchMode.branch}
        saving={branchMode.saving}
        error={branchMode.error}
        accessGroupId={myGroups[0]}
        onSubmitForReview={branchMode.submitForReview}
        onRecall={branchMode.recall}
      />
    )}
    <div style={S.body}>
      {/* Sidebar */}
      <div style={S.sidebar}>
        <div style={S.sideHead}>
          <span style={{ flex: 1 }}>CONNECTORS</span>
          <button style={{ ...S.btn, ...S.btnPrimary, padding: "4px 10px", fontSize: 11 }} onClick={() => setCreating(c => !c)}>+ New</button>
        </div>
        {creating && (
          <form onSubmit={handleCreate} style={{ padding: 12, borderBottom: "1px solid var(--border)" }}>
            <div style={{ marginBottom: 8 }}>
              <span style={S.label}>Type</span>
              <select style={{ ...S.input }} value={form.connector_type} onChange={e => setForm(f => ({ ...f, connector_type: e.target.value }))}>
                {connectorTypes.map(ct => <option key={ct.connector_type} value={ct.connector_type}>{ct.display_name}</option>)}
              </select>
            </div>
            <div style={{ marginBottom: 8 }}>
              <span style={S.label}>Name</span>
              <input style={S.input} value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} required placeholder="My Connector" />
            </div>
            <div style={{ marginBottom: 8 }}>
              <span style={S.label}>Description</span>
              <input style={S.input} value={form.description} onChange={e => setForm(f => ({ ...f, description: e.target.value }))} placeholder="Optional" />
            </div>
            <div style={{ marginBottom: 8 }}>
              <span style={S.label}>Config (JSON)</span>
              <textarea style={{ ...S.input, height: 70, resize: "vertical", fontFamily: "monospace", fontSize: 11 }}
                        value={form.config} onChange={e => setForm(f => ({ ...f, config: e.target.value }))} />
            </div>
            <div style={{ marginBottom: 10 }}>
              <span style={S.label}>Credentials (JSON — encrypted at rest)</span>
              <textarea style={{ ...S.input, height: 50, resize: "vertical", fontFamily: "monospace", fontSize: 11 }}
                        value={form.credentials} onChange={e => setForm(f => ({ ...f, credentials: e.target.value }))} />
            </div>
            {selectedType && (
              <div style={{ fontSize: 10, color: "var(--text-secondary)", marginBottom: 8 }}>{selectedType.description}</div>
            )}
            {err && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 6 }}>{err}</div>}
            <button type="submit" disabled={loading} style={{ ...S.btn, ...S.btnPrimary, width: "100%" }}>{loading ? "Creating…" : "Create"}</button>
          </form>
        )}
        <div style={S.list}>
          {connectors.map(c => (
            <div key={c.id} style={{ borderBottom: "1px solid var(--border)" }}>
              <div onClick={() => loadDetail(c)} style={{
                padding: "10px 14px", cursor: "pointer",
                background: selected?.id === c.id ? "var(--accent-light, #ede9fe)" : "transparent",
                display: "flex", alignItems: "flex-start", gap: 6,
              }}>
                <div style={{ flex: 1 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 3 }}>
                    <span style={{ fontSize: 12, fontWeight: 600, flex: 1 }}>{c.name}</span>
                    {!c.enabled && <span style={{ ...S.badge, background: "#94a3b822", color: "#94a3b8" }}>off</span>}
                    {c.last_test_ok === true  && <span style={{ fontSize: 10, color: "#22c55e" }}>✓</span>}
                    {c.last_test_ok === false && <span style={{ fontSize: 10, color: "#ef4444" }}>✗</span>}
                  </div>
                  <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>{c.connector_type}</div>
                </div>
                <button
                  onClick={e => { e.stopPropagation(); openBranchForm(c); }}
                  style={{ ...S.btn, padding: "2px 6px", fontSize: 10, background: "none", color: "var(--text-muted)", border: "1px solid var(--border)", flexShrink: 0, marginTop: 2 }}
                  title="Create branch">⎇</button>
              </div>
              {branchingId === c.id && (
                <div style={{ padding: "8px 14px", background: "var(--bg-elevated)", fontSize: 11 }}>
                  {branchCreated ? (
                    <>
                      <div style={{ padding: "4px 8px", background: "#dcfce7", color: "#16a34a", borderRadius: 4, marginBottom: 6 }}>
                        ✓ Branch <b>{branchCreated.name}</b> created
                      </div>
                      <div style={{ display: "flex", gap: 6 }}>
                        <button
                          onClick={() => navigate(`/hxbridge?branch=${branchCreated.id}`)}
                          style={{ ...S.btn, ...S.btnPrimary, padding: "4px 10px", fontSize: 11 }}>
                          Open in Editor →
                        </button>
                        <button
                          onClick={() => setBranchingId(null)}
                          style={{ ...S.btn, ...S.btnSecond, padding: "4px 8px", fontSize: 11 }}>
                          Close
                        </button>
                      </div>
                    </>
                  ) : (
                    <>
                      {branchCreateErr && <div style={{ color: "#ef4444", marginBottom: 4 }}>✗ {branchCreateErr}</div>}
                      <input
                        style={{ ...S.input, padding: "4px 8px", fontSize: 11, fontFamily: "monospace", marginBottom: 4 }}
                        placeholder="branch name"
                        value={branchName}
                        onChange={e => setBranchName(e.target.value)}
                        onClick={e => e.stopPropagation()}
                      />
                      <div onClick={e => e.stopPropagation()} style={{ marginBottom: 6 }}>
                        <ReviewerPicker
                          value={branchReviewer}
                          onChange={setBranchReviewer}
                          accessGroupId={myGroups[0]}
                          placeholder="Reviewer (optional)"
                        />
                      </div>
                      <div style={{ display: "flex", gap: 6 }}>
                        <button
                          disabled={branchBusy || !branchName.trim()}
                          onClick={e => { e.stopPropagation(); handleBranchCreate(c.id, c.name); }}
                          style={{ ...S.btn, ...S.btnPrimary, padding: "4px 10px", fontSize: 11, opacity: (branchBusy || !branchName.trim()) ? 0.6 : 1 }}>
                          {branchBusy ? "…" : "Create Branch"}
                        </button>
                        <button
                          onClick={e => { e.stopPropagation(); setBranchingId(null); }}
                          style={{ ...S.btn, ...S.btnSecond, padding: "4px 8px", fontSize: 11 }}>
                          ✕
                        </button>
                      </div>
                    </>
                  )}
                </div>
              )}
            </div>
          ))}
          {connectors.length === 0 && <div style={{ padding: 16, fontSize: 12, color: "var(--text-secondary)" }}>No connectors yet.</div>}
        </div>
      </div>

      {/* Detail */}
      <div style={S.detail}>
        {!selected && <div style={{ color: "var(--text-secondary)", fontSize: 13, paddingTop: 40 }}>Select a connector or create a new one.</div>}
        {selected && (
          <>
            <div style={{ display: "flex", alignItems: "flex-start", gap: 16, marginBottom: 20 }}>
              <div style={{ flex: 1 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" as const }}>
                  <h2 style={{ margin: 0, fontSize: 17, fontWeight: 700 }}>{selected.name}</h2>
                  {selected.credential_expires_at && (
                    <span style={{ ...S.badge, background: "#fef3c7", color: "#92400e", border: "1px solid #fcd34d", fontSize: 10 }}>
                      Expires: {new Date(selected.credential_expires_at).toLocaleDateString()}
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 4 }}>{selected.connector_type} · created {selected.created_at ? new Date(selected.created_at).toLocaleDateString() : "—"}</div>
                {selected.description && <div style={{ fontSize: 13, marginTop: 6 }}>{selected.description}</div>}
              </div>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" as const }}>
                <button style={{ ...S.btn, ...S.btnSecond }} onClick={() => handleTest(selected)}>⚡ Test</button>
                <button style={{ ...S.btn, ...S.btnSecond }} onClick={() => handleToggle(selected)}>{selected.enabled ? "Disable" : "Enable"}</button>
                <button style={{ ...S.btn, background: "#f59e0b", color: "#fff" }} onClick={() => handleRotate(selected)}>↺ Rotate Credentials</button>
                <button style={{ ...S.btn, background: "#fee2e2", color: "#ef4444", border: "1px solid #fecaca" }} onClick={() => handleDelete(selected)}>Delete</button>
              </div>
            </div>

            {testResult && (
              <div style={{ ...S.card, background: testResult.ok ? "#f0fdf4" : "#fef2f2", border: `1px solid ${testResult.ok ? "#bbf7d0" : "#fecaca"}`, marginBottom: 12 }}>
                <span style={{ fontWeight: 700, color: testResult.ok ? "#22c55e" : "#ef4444" }}>{testResult.ok ? "✓ Connection OK" : "✗ Connection Failed"}</span>
                {testResult.error && <div style={{ fontSize: 12, marginTop: 4, color: "#ef4444" }}>{testResult.error}</div>}
              </div>
            )}

            {rotateMsg && (
              <div style={{ ...S.card, background: rotateMsg.ok ? "#fffbeb" : "#fef2f2", border: `1px solid ${rotateMsg.ok ? "#fcd34d" : "#fecaca"}`, marginBottom: 12 }}>
                <span style={{ fontWeight: 700, color: rotateMsg.ok ? "#92400e" : "#ef4444" }}>{rotateMsg.ok ? "↺ " : "✗ "}{rotateMsg.text}</span>
              </div>
            )}

            {/* Config — editable in branch mode, read-only otherwise */}
            {branchMode.isBranchMode ? (
              <div style={S.card}>
                <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-secondary)", marginBottom: 4 }}>
                  BRANCH CONFIG EDIT
                  <span style={{ marginLeft: 8, fontWeight: 400, color: "#f59e0b" }}>credentials never included</span>
                </div>
                <textarea
                  style={{ ...S.input, height: 160, fontFamily: "monospace", fontSize: 11, resize: "vertical" }}
                  value={branchConfigStr}
                  onChange={e => { setBranchConfigStr(e.target.value); setBranchConfigErr(null); }}
                  disabled={branchMode.isLocked}
                />
                {branchConfigErr && (
                  <div style={{ fontSize: 11, color: "#ef4444", marginTop: 4 }}>{branchConfigErr}</div>
                )}
                <button
                  style={{ ...S.btn, ...S.btnPrimary, marginTop: 8, opacity: (branchMode.saving || branchMode.isLocked) ? 0.6 : 1 }}
                  onClick={handleSaveToBranch}
                  disabled={branchMode.saving || branchMode.isLocked}>
                  {branchMode.saving ? "Saving…" : branchMode.isLocked ? "Locked" : "Save to Branch"}
                </button>
              </div>
            ) : (
              <div style={S.card}>
                <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-secondary)", marginBottom: 8 }}>CONFIGURATION</div>
                <pre style={{ margin: 0, fontSize: 11, fontFamily: "monospace", whiteSpace: "pre-wrap", color: "var(--text-primary)" }}>
                  {JSON.stringify(selected.config || {}, null, 2)}
                </pre>
              </div>
            )}

            {/* Sandbox */}
            <div style={S.card}>
              <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-secondary)", marginBottom: 8 }}>SANDBOX — TEST EXECUTE</div>
              <textarea style={{ ...S.input, height: 80, fontFamily: "monospace", fontSize: 11, resize: "vertical", marginBottom: 8 }}
                        value={sandboxInput} onChange={e => setSandboxInput(e.target.value)} />
              <button style={{ ...S.btn, ...S.btnPrimary, marginBottom: 8 }} onClick={() => handleSandbox(selected)}>▶ Execute</button>
              {sandboxResult && (
                <pre style={{ fontSize: 11, fontFamily: "monospace", whiteSpace: "pre-wrap", background: sandboxResult.ok ? "#f0fdf4" : "#fef2f2", padding: 10, borderRadius: 6, margin: 0 }}>
                  {JSON.stringify(sandboxResult, null, 2)}
                </pre>
              )}
            </div>

            {/* Webhook URL */}
            {selected.connector_type === "webhook" && (
              <div style={S.card}>
                <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-secondary)", marginBottom: 6 }}>INBOUND WEBHOOK URL</div>
                <div style={{ fontSize: 12, fontFamily: "monospace", background: "var(--bg-main)", padding: "6px 10px", borderRadius: 6, border: "1px solid var(--border)", wordBreak: "break-all" }}>
                  {window.location.origin}/api/v1/webhooks/{selected.id}/receive
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
    </div>
  );
}

// ── Events tab (unified inbound + outbound) ───────────────────────────────────

const DIRECTION_COLOR: Record<string, string> = { outbound: "#3b82f6", inbound: "#22c55e" };

function EventsTab() {
  const [events, setEvents]           = useState<any[]>([]);
  const [direction, setDirection]     = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [page, setPage]               = useState(1);
  const [totalPages, setTotalPages]   = useState(1);
  const [total, setTotal]             = useState(0);
  const PAGE_SIZE = 50;

  const load = useCallback(async () => {
    const params = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) });
    if (direction) params.set("direction", direction);
    if (statusFilter) params.set("status", statusFilter);
    const r = await authFetch(`${API}/events?${params}`);
    if (r.ok) {
      const d = await r.json();
      setEvents(d.events ?? []);
      setTotal(d.total ?? 0);
      setTotalPages(d.total_pages ?? 1);
    }
  }, [direction, statusFilter, page]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { setPage(1); }, [direction, statusFilter]);

  return (
    <div style={{ flex: 1, overflow: "auto", padding: "20px 28px" }}>
      {/* Direction pills */}
      <div style={{ display: "flex", gap: 6, marginBottom: 10, alignItems: "center" }}>
        <span style={{ fontSize: 11, fontWeight: 600, color: "var(--text-secondary)", marginRight: 4 }}>Direction:</span>
        {[["", "All"], ["outbound", "↗ Outbound"], ["inbound", "↙ Inbound"]].map(([val, label]) => (
          <button key={val} onClick={() => setDirection(val)} style={{
            ...S.btn, padding: "4px 12px", fontSize: 11,
            background: direction === val ? "var(--accent)" : "var(--bg-surface)",
            color: direction === val ? "#fff" : "var(--text-secondary)",
            border: "1px solid var(--border)",
          }}>{label}</button>
        ))}
        <span style={{ fontSize: 11, fontWeight: 600, color: "var(--text-secondary)", marginLeft: 12, marginRight: 4 }}>Status:</span>
        {["", "success", "failed", "running", "matched", "no_match", "error"].map(s => (
          <button key={s} onClick={() => setStatusFilter(s)} style={{
            ...S.btn, padding: "4px 10px", fontSize: 11,
            background: statusFilter === s ? "var(--accent)" : "var(--bg-surface)",
            color: statusFilter === s ? "#fff" : "var(--text-secondary)",
            border: "1px solid var(--border)",
          }}>{s || "All"}</button>
        ))}
        <button style={{ ...S.btn, ...S.btnSecond, padding: "4px 10px", fontSize: 11, marginLeft: "auto" }} onClick={load}>↻</button>
      </div>

      {events.length === 0 ? (
        <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>No events yet.</div>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
          <thead>
            <tr style={{ borderBottom: "1px solid var(--border)" }}>
              {["Dir", "Status", "Connector", "Case", "Latency", "Error", "When"].map(h => (
                <th key={h} style={{ textAlign: "left", padding: "6px 10px", fontSize: 10, fontWeight: 700, color: "var(--text-secondary)", textTransform: "uppercase" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {events.map((e: any) => (
              <tr key={e.id} style={{ borderBottom: "1px solid var(--border)" }}>
                <td style={{ padding: "8px 10px" }}>
                  <span style={{ ...S.badge, background: (DIRECTION_COLOR[e.direction] || "#6b7280") + "22", color: DIRECTION_COLOR[e.direction] || "#6b7280" }}>
                    {e.direction === "outbound" ? "↗" : "↙"} {e.direction}
                  </span>
                </td>
                <td style={{ padding: "8px 10px" }}>
                  <span style={{ ...S.badge, background: (STATUS_COLOR[e.status] || "#6b7280") + "22", color: STATUS_COLOR[e.status] || "#6b7280" }}>{e.status}</span>
                </td>
                <td style={{ padding: "8px 10px", color: "var(--text-secondary)" }}>{e.connector_id?.slice(0, 8) ?? "—"}</td>
                <td style={{ padding: "8px 10px", color: "var(--text-secondary)" }}>{e.case_id?.slice(0, 8) ?? "—"}</td>
                <td style={{ padding: "8px 10px" }}>{e.latency_ms != null ? `${e.latency_ms}ms` : "—"}</td>
                <td style={{ padding: "8px 10px", color: "#ef4444", maxWidth: 180 }}>{e.error ? e.error.slice(0, 50) : "—"}</td>
                <td style={{ padding: "8px 10px", color: "var(--text-secondary)", whiteSpace: "nowrap" }}>{e.created_at ? new Date(e.created_at).toLocaleString() : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* Pagination */}
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 14, fontSize: 12, color: "var(--text-secondary)" }}>
        <button style={{ ...S.btn, ...S.btnSecond, padding: "4px 10px" }} disabled={page <= 1} onClick={() => setPage(p => p - 1)}>‹ Prev</button>
        <span>Page {page} of {totalPages} ({total} events)</span>
        <button style={{ ...S.btn, ...S.btnSecond, padding: "4px 10px" }} disabled={page >= totalPages} onClick={() => setPage(p => p + 1)}>Next ›</button>
      </div>
    </div>
  );
}

// ── DLQ tab ───────────────────────────────────────────────────────────────────

function DLQTab() {
  const [items, setItems] = useState<DLQItem[]>([]);
  const [showResolved, setShowResolved] = useState(false);

  const load = useCallback(async () => {
    const r = await authFetch(`${API}/dlq?unresolved_only=${!showResolved}`);
    if (r.ok) setItems((await r.json()).items);
  }, [showResolved]);

  useEffect(() => { load(); }, [load]);

  async function handleRetry(id: string) {
    const r = await authFetch(`${API}/dlq/${id}/retry`, { method: "POST" });
    await load();
  }

  return (
    <div style={{ flex: 1, overflow: "auto", padding: "20px 28px" }}>
      <div style={{ display: "flex", gap: 10, marginBottom: 16, alignItems: "center" }}>
        <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
          <input type="checkbox" checked={showResolved} onChange={e => setShowResolved(e.target.checked)} />
          Show resolved items
        </label>
        <button style={{ ...S.btn, ...S.btnSecond, padding: "4px 10px", fontSize: 11 }} onClick={load}>↻ Refresh</button>
      </div>
      {items.length === 0 ? (
        <div style={{ color: "#22c55e", fontSize: 13 }}>✓ Dead letter queue is empty.</div>
      ) : (
        <div>
          {items.map(item => (
            <div key={item.id} style={{ ...S.card, borderLeft: `3px solid ${item.resolution ? "#22c55e" : "#ef4444"}` }}>
              <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                    Connector: {item.connector_id?.slice(0, 8) ?? "unknown"}
                    {item.resolution && <span style={{ ...S.badge, background: "#22c55e22", color: "#22c55e", marginLeft: 8 }}>{item.resolution}</span>}
                  </div>
                  {item.error && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 4 }}>{item.error}</div>}
                  <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>
                    Retries: {item.retry_count}/{item.max_retries}
                    {item.next_retry_at && !item.resolution && ` · Next retry: ${new Date(item.next_retry_at).toLocaleString()}`}
                  </div>
                </div>
                {!item.resolution && (
                  <button style={{ ...S.btn, background: "#fef3c7", color: "#92400e", border: "1px solid #fcd34d" }}
                          onClick={() => handleRetry(item.id)}>↻ Retry Now</button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Root ──────────────────────────────────────────────────────────────────────

export default function HxBridge() {
  const [tab, setTab] = useState<"connectors" | "events" | "dlq">("connectors");
  const tabs = [
    { key: "connectors" as const, label: "Connectors" },
    { key: "events"     as const, label: "Events" },
    { key: "dlq"        as const, label: "Dead Letter Queue" },
  ];
  return (
    <div style={S.page}>
      <div style={S.tabs}>
        {tabs.map(t => (
          <button key={t.key} onClick={() => setTab(t.key)}
                  style={{ ...S.tab, ...(tab === t.key ? S.tabActive : {}) }}>{t.label}</button>
        ))}
      </div>
      <div style={S.body}>
        {tab === "connectors" && <ConnectorsTab />}
        {tab === "events"     && <EventsTab />}
        {tab === "dlq"        && <DLQTab />}
      </div>
    </div>
  );
}
