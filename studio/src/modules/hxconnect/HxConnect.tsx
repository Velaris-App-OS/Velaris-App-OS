/**
 * P48 — HxConnect: Payment & Financial
 * Tabs: Connectors · Payments · Webhooks
 */
import React, { useState, useEffect, useCallback } from "react";

const API = "/api/v1/payments";

function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}
function authFetch(url: string, opts: RequestInit = {}): Promise<Response> {
  return fetch(url, { ...opts, headers: { ..._authHdr(), ...opts.headers } });
}

type Connector = {
  id: string; name: string; connector_type: string; description: string | null;
  config: Record<string, any>; credentials: Record<string, any>;
  enabled: boolean; last_tested_at: string | null; last_test_ok: boolean | null;
};
type PaymentRequest = {
  id: string; case_id: string; step_id: string; provider: string;
  provider_ref: string | null; checkout_url: string | null;
  amount_cents: number; currency: string; status: string;
  description: string | null; metadata: Record<string, any>;
  created_at: string; completed_at: string | null;
};
type WebhookEvent = {
  id: string; provider: string; event_type: string | null; provider_ref: string | null;
  verified: boolean; processed: boolean; error: string | null; received_at: string;
};

const STATUS_COLOR: Record<string, string> = {
  pending:    "#94a3b8",
  processing: "#3b82f6",
  succeeded:  "#22c55e",
  failed:     "#ef4444",
  refunded:   "#a855f7",
  cancelled:  "#6b7280",
};

const PROVIDER_ICON: Record<string, string> = {
  stripe: "S", paypal: "P", adyen: "A",
};

const S: Record<string, React.CSSProperties> = {
  page:       { display: "flex", flexDirection: "column", height: "100%", background: "var(--bg-main)", color: "var(--text-primary)", fontFamily: "system-ui, sans-serif" },
  header:     { padding: "18px 24px 0", flexShrink: 0 },
  title:      { fontSize: 20, fontWeight: 700, margin: 0 },
  sub:        { fontSize: 13, color: "var(--text-secondary)", marginTop: 4 },
  tabs:       { display: "flex", gap: 4, padding: "14px 24px 0", borderBottom: "1px solid var(--border)", flexShrink: 0 },
  tab:        { padding: "8px 20px", border: "none", background: "none", fontSize: 13, cursor: "pointer", borderBottom: "2px solid transparent", fontWeight: 400, color: "var(--text-secondary)" },
  tabActive:  { borderBottom: "2px solid var(--accent)", color: "var(--accent)", fontWeight: 700 },
  body:       { flex: 1, overflow: "hidden", display: "flex" },
  sidebar:    { width: 270, borderRight: "1px solid var(--border)", display: "flex", flexDirection: "column", background: "var(--bg-surface)", flexShrink: 0 },
  sideHead:   { padding: "12px 14px", borderBottom: "1px solid var(--border)", display: "flex", alignItems: "center", gap: 8, flexShrink: 0, fontSize: 12, fontWeight: 700, color: "var(--text-secondary)", textTransform: "uppercase" },
  list:       { flex: 1, overflow: "auto" },
  detail:     { flex: 1, overflow: "auto", padding: "24px 28px" },
  btn:        { padding: "7px 16px", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12, fontWeight: 700 },
  btnPrimary: { background: "var(--accent)", color: "#fff" },
  btnSecond:  { background: "var(--bg-surface)", border: "1px solid var(--border)", color: "var(--text-secondary)" },
  row:        { padding: "10px 14px", cursor: "pointer", borderBottom: "1px solid var(--border)", display: "flex", alignItems: "center", gap: 10 },
  rowActive:  { background: "var(--accent-dim)" },
  badge:      { fontSize: 9, fontWeight: 700, padding: "2px 6px", borderRadius: 4, fontFamily: "monospace", textTransform: "uppercase" },
  mono:       { fontFamily: "monospace", fontSize: 11 },
  label:      { fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase", fontWeight: 600, marginBottom: 4, letterSpacing: "0.05em" },
  value:      { fontSize: 13, color: "var(--text-primary)", marginBottom: 12 },
  card:       { background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: 8, padding: "16px 20px", marginBottom: 12 },
  empty:      { color: "var(--text-muted)", fontSize: 13, padding: "48px 24px" },
  input:      { width: "100%", padding: "8px 10px", border: "1px solid var(--border)", borderRadius: 6, background: "var(--bg-main)", color: "var(--text-primary)", fontSize: 13, boxSizing: "border-box" as const },
};

function statusBadge(status: string) {
  const color = STATUS_COLOR[status] ?? "#94a3b8";
  return <span style={{ ...S.badge, background: color + "22", color }}>{status}</span>;
}

function providerBadge(provider: string) {
  return (
    <span style={{ ...S.badge, background: "var(--accent-dim)", color: "var(--accent)", width: 20, height: 20, display: "inline-flex", alignItems: "center", justifyContent: "center", borderRadius: 4 }}>
      {PROVIDER_ICON[provider] ?? provider[0]?.toUpperCase()}
    </span>
  );
}

function fmtMoney(cents: number, currency: string) {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: currency.toUpperCase() }).format(cents / 100);
}

function fmtTime(iso: string) {
  return new Date(iso).toLocaleString();
}

// ── Connectors tab ────────────────────────────────────────────────────────────

function ConnectorsTab() {
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [selected, setSelected]     = useState<Connector | null>(null);
  const [testing, setTesting]       = useState(false);
  const [testMsg, setTestMsg]       = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await authFetch(`${API}/connectors`);
    if (r.ok) setConnectors(await r.json());
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleTest = async (id: string) => {
    setTesting(true); setTestMsg(null);
    const r = await authFetch(`${API}/connectors/${id}/test`, { method: "POST" });
    const data = await r.json();
    setTestMsg(data.ok ? "Connection successful" : "Connection failed");
    setTesting(false);
    load();
  };

  return (
    <div style={S.body}>
      {/* Sidebar */}
      <div style={S.sidebar}>
        <div style={S.sideHead}>Payment Connectors</div>
        <div style={S.list}>
          {connectors.length === 0 && (
            <div style={{ ...S.empty, padding: "24px 14px", fontSize: 12 }}>
              No payment connectors yet.<br />Add one via HxBridge → Connectors.
            </div>
          )}
          {connectors.map(c => (
            <div
              key={c.id}
              style={{ ...S.row, ...(selected?.id === c.id ? S.rowActive : {}) }}
              onClick={() => { setSelected(c); setTestMsg(null); }}
            >
              {providerBadge(c.connector_type)}
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.name}</div>
                <div style={{ fontSize: 10, color: "var(--text-muted)" }}>{c.connector_type}</div>
              </div>
              <span style={{ width: 8, height: 8, borderRadius: "50%", background: c.enabled ? "#22c55e" : "#94a3b8", flexShrink: 0 }} />
            </div>
          ))}
        </div>
      </div>

      {/* Detail */}
      <div style={S.detail}>
        {!selected ? (
          <div style={S.empty}>Select a connector to view details</div>
        ) : (
          <>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 20 }}>
              <div>
                <h2 style={{ margin: 0, fontSize: 18, fontWeight: 700 }}>{selected.name}</h2>
                <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 4 }}>{selected.connector_type} · {selected.id}</div>
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  style={{ ...S.btn, ...S.btnSecond }}
                  disabled={testing}
                  onClick={() => handleTest(selected.id)}
                >
                  {testing ? "Testing…" : "Test Connection"}
                </button>
              </div>
            </div>

            {testMsg && (
              <div style={{ marginBottom: 16, padding: "10px 14px", borderRadius: 6,
                background: testMsg.includes("successful") ? "#22c55e22" : "#ef444422",
                color: testMsg.includes("successful") ? "#22c55e" : "#ef4444",
                fontSize: 13 }}>
                {testMsg}
              </div>
            )}

            <div style={S.card}>
              <div style={S.label}>Status</div>
              <div style={S.value}>
                <span style={{ ...S.badge, background: selected.enabled ? "#22c55e22" : "#94a3b822", color: selected.enabled ? "#22c55e" : "#94a3b8" }}>
                  {selected.enabled ? "Enabled" : "Disabled"}
                </span>
              </div>
              <div style={S.label}>Last Tested</div>
              <div style={S.value}>
                {selected.last_tested_at
                  ? `${fmtTime(selected.last_tested_at)} — ${selected.last_test_ok ? "✓ OK" : "✗ Failed"}`
                  : "Never tested"}
              </div>
              <div style={S.label}>Configuration</div>
              <pre style={{ ...S.mono, background: "var(--bg-elevated)", padding: 10, borderRadius: 6, overflow: "auto", fontSize: 11 }}>
                {JSON.stringify(selected.config, null, 2)}
              </pre>
              <div style={S.label}>Credentials</div>
              <pre style={{ ...S.mono, background: "var(--bg-elevated)", padding: 10, borderRadius: 6, overflow: "auto", fontSize: 11 }}>
                {JSON.stringify(selected.credentials, null, 2)}
              </pre>
            </div>

            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 8 }}>
              To update credentials or config, use HxBridge → Connectors → Edit.
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ── Shared: global list hook + status filter bar ──────────────────────────────

function useGlobalList(globalUrl: string, statusOptions: string[]) {
  const [status, setStatus] = useState("");
  const [caseFilter, setCaseFilter] = useState("");
  const [rows, setRows]   = useState<any[]>([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (caseId?: string) => {
    setLoading(true);
    const url = caseId
      ? caseId  // caller passes full case URL
      : globalUrl + (status ? `?status=${status}` : "");
    try {
      const r = await authFetch(url);
      if (r.ok) setRows(await r.json()); else setRows([]);
    } catch { setRows([]); }
    setLoading(false);
  }, [globalUrl, status]);

  useEffect(() => { load(); }, [load]);

  return { rows, loading, status, setStatus, caseFilter, setCaseFilter, load, statusOptions };
}

function StatusBar({ options, value, onChange }: { options: string[]; value: string; onChange: (v: string) => void }) {
  const colors: Record<string, string> = {
    pending: "#94a3b8", processing: "#3b82f6", queued: "#3b82f6",
    sent: "#22c55e", delivered: "#22c55e", completed: "#22c55e", synced: "#22c55e",
    clear: "#22c55e", uploaded: "#22c55e", draft: "#3b82f6",
    failed: "#ef4444", undelivered: "#ef4444", declined: "#ef4444",
    in_progress: "#0d9488", consider: "#f59e0b",
  };
  const active: React.CSSProperties = { fontWeight: 700, borderBottom: "2px solid var(--accent)", color: "var(--accent)" };
  const inactive: React.CSSProperties = { color: "var(--text-muted)", borderBottom: "2px solid transparent" };

  return (
    <div style={{ display: "flex", gap: 0, padding: "0 14px", borderBottom: "1px solid var(--border)", flexWrap: "wrap" }}>
      <button onClick={() => onChange("")}
        style={{ ...S.btn, padding: "8px 10px", fontSize: 11, borderRadius: 0, ...(value === "" ? active : inactive) }}>All</button>
      {options.map(o => (
        <button key={o} onClick={() => onChange(o)}
          style={{ ...S.btn, padding: "8px 10px", fontSize: 11, borderRadius: 0, ...(value === o ? active : inactive), color: value === o ? (colors[o] ?? "var(--accent)") : "var(--text-muted)" }}>
          {o}
        </button>
      ))}
    </div>
  );
}

function CaseFilterBar({ value, onChange, onSearch, loading }: { value: string; onChange: (v: string) => void; onSearch: (v: string) => void; loading: boolean }) {
  return (
    <div style={{ padding: "8px 14px", borderBottom: "1px solid var(--border)", display: "flex", gap: 6 }}>
      <input style={{ ...S.input, flex: 1, margin: 0 }} placeholder="Filter by case ID…" value={value}
        onChange={e => onChange(e.target.value)}
        onKeyDown={e => { if (e.key === "Enter") onSearch(value); if (e.key === "Escape") { onChange(""); onSearch(""); } }} />
      {value && <button style={{ ...S.btn, padding: "5px 10px", fontSize: 11 }} onClick={() => { onChange(""); onSearch(""); }}>✕</button>}
    </div>
  );
}

// ── Payments tab ──────────────────────────────────────────────────────────────

function PaymentsTab() {
  const [caseSearch, setCaseSearch] = useState("");
  const [payments, setPayments]     = useState<PaymentRequest[]>([]);
  const [selected, setSelected]     = useState<PaymentRequest | null>(null);
  const [loading, setLoading]       = useState(false);
  const [refunding, setRefunding]   = useState(false);
  const [msg, setMsg]               = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState("");

  const load = useCallback(async (caseId?: string) => {
    setLoading(true); setMsg(null);
    const url = caseId
      ? `${API}/cases/${caseId}/requests`
      : `${API}/requests${statusFilter ? `?status=${statusFilter}` : ""}`;
    const r = await authFetch(url);
    if (r.ok) setPayments(await r.json()); else { setPayments([]); if (caseId) setMsg("No payments found."); }
    setLoading(false);
  }, [statusFilter]);

  useEffect(() => { load(); }, [load]);

  const search = () => { if (caseSearch.trim()) load(caseSearch.trim()); else load(); };

  const handleRefund = async (id: string) => {
    setRefunding(true); setMsg(null);
    const r = await authFetch(`${API}/requests/${id}/refund`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    if (r.ok) {
      const updated = await r.json();
      setSelected(updated);
      setPayments(ps => ps.map(p => p.id === id ? updated : p));
      setMsg("Refund initiated successfully.");
    } else {
      const err = await r.json();
      setMsg(`Refund failed: ${err.detail}`);
    }
    setRefunding(false);
  };

  return (
    <div style={S.body}>
      {/* Sidebar */}
      <div style={S.sidebar}>
        <div style={S.sideHead}>Payment Requests {loading && <span style={{ fontSize: 10, fontWeight: 400, color: "var(--text-muted)" }}>loading…</span>}</div>
        <StatusBar options={["pending","succeeded","failed","refunded"]} value={statusFilter} onChange={setStatusFilter} />
        <CaseFilterBar value={caseSearch} onChange={setCaseSearch} onSearch={v => v ? load(v) : load()} loading={loading} />
        <div style={S.list}>
          {payments.length === 0 && !msg && (
            <div style={{ ...S.empty, padding: "20px 14px", fontSize: 12 }}>No payment requests.</div>
          )}
          {msg && !payments.length && <div style={{ padding: "12px 14px", fontSize: 12, color: "var(--text-muted)" }}>{msg}</div>}
          {payments.map(p => (
            <div
              key={p.id}
              style={{ ...S.row, ...(selected?.id === p.id ? S.rowActive : {}) }}
              onClick={() => { setSelected(p); setMsg(null); }}
            >
              {providerBadge(p.provider)}
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 500 }}>{fmtMoney(p.amount_cents, p.currency)}</div>
                <div style={{ fontSize: 10, color: "var(--text-muted)" }}>{p.step_id}</div>
              </div>
              {statusBadge(p.status)}
            </div>
          ))}
        </div>
      </div>

      {/* Detail */}
      <div style={S.detail}>
        {!selected ? (
          <div style={S.empty}>Select a payment request to view details</div>
        ) : (
          <>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 20 }}>
              <div>
                <h2 style={{ margin: 0, fontSize: 18, fontWeight: 700 }}>
                  {fmtMoney(selected.amount_cents, selected.currency)}
                  <span style={{ marginLeft: 10 }}>{statusBadge(selected.status)}</span>
                </h2>
                <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 4 }}>{selected.id}</div>
              </div>
              {selected.status === "succeeded" && (
                <button
                  style={{ ...S.btn, background: "#ef444422", color: "#ef4444", border: "1px solid #ef444444" }}
                  disabled={refunding}
                  onClick={() => handleRefund(selected.id)}
                >
                  {refunding ? "Processing…" : "Refund"}
                </button>
              )}
            </div>

            {msg && (
              <div style={{ marginBottom: 16, padding: "10px 14px", borderRadius: 6,
                background: msg.includes("success") ? "#22c55e22" : "#ef444422",
                color: msg.includes("success") ? "#22c55e" : "#ef4444",
                fontSize: 13 }}>
                {msg}
              </div>
            )}

            <div style={S.card}>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 24px" }}>
                <div>
                  <div style={S.label}>Provider</div>
                  <div style={S.value}>{selected.provider}</div>
                </div>
                <div>
                  <div style={S.label}>Currency</div>
                  <div style={S.value}>{selected.currency.toUpperCase()}</div>
                </div>
                <div>
                  <div style={S.label}>Step</div>
                  <div style={{ ...S.value, ...S.mono }}>{selected.step_id}</div>
                </div>
                <div>
                  <div style={S.label}>Case</div>
                  <div style={{ ...S.value, ...S.mono }}>{selected.case_id}</div>
                </div>
                <div>
                  <div style={S.label}>Created</div>
                  <div style={S.value}>{fmtTime(selected.created_at)}</div>
                </div>
                <div>
                  <div style={S.label}>Completed</div>
                  <div style={S.value}>{selected.completed_at ? fmtTime(selected.completed_at) : "—"}</div>
                </div>
              </div>

              {selected.provider_ref && (
                <>
                  <div style={S.label}>Provider Reference</div>
                  <div style={{ ...S.value, ...S.mono }}>{selected.provider_ref}</div>
                </>
              )}

              {selected.description && (
                <>
                  <div style={S.label}>Description</div>
                  <div style={S.value}>{selected.description}</div>
                </>
              )}

              {selected.checkout_url && selected.status === "pending" && (
                <>
                  <div style={S.label}>Checkout URL</div>
                  <div style={{ marginBottom: 12 }}>
                    <a href={selected.checkout_url} target="_blank" rel="noreferrer"
                      style={{ fontSize: 12, color: "var(--accent)", wordBreak: "break-all" }}>
                      {selected.checkout_url}
                    </a>
                  </div>
                </>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ── Webhooks tab ──────────────────────────────────────────────────────────────

function WebhooksTab() {
  const [events, setEvents]     = useState<WebhookEvent[]>([]);
  const [selected, setSelected] = useState<WebhookEvent | null>(null);

  useEffect(() => {
    authFetch(`${API}/webhooks`).then(r => r.json()).then(setEvents).catch(() => {});
  }, []);

  return (
    <div style={S.body}>
      <div style={S.sidebar}>
        <div style={S.sideHead}>Recent Events ({events.length})</div>
        <div style={S.list}>
          {events.length === 0 && <div style={S.empty}>No webhook events yet.</div>}
          {events.map(e => (
            <div
              key={e.id}
              style={{ ...S.row, ...(selected?.id === e.id ? S.rowActive : {}) }}
              onClick={() => setSelected(e)}
            >
              <span style={{ ...S.badge, background: e.verified ? "#22c55e22" : "#ef444422", color: e.verified ? "#22c55e" : "#ef4444" }}>
                {e.verified ? "✓" : "✗"}
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12, fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{e.event_type ?? "unknown"}</div>
                <div style={{ fontSize: 10, color: "var(--text-muted)" }}>{new Date(e.received_at).toLocaleTimeString()}</div>
              </div>
              <span style={{ ...S.badge, background: e.processed ? "#3b82f622" : "#f59e0b22", color: e.processed ? "#3b82f6" : "#f59e0b" }}>
                {e.processed ? "processed" : "pending"}
              </span>
            </div>
          ))}
        </div>
      </div>

      <div style={S.detail}>
        {!selected ? (
          <div style={S.empty}>Select an event to inspect the payload</div>
        ) : (
          <>
            <div style={{ marginBottom: 20 }}>
              <h2 style={{ margin: 0, fontSize: 18, fontWeight: 700 }}>
                {selected.event_type ?? "Unknown Event"}
                <span style={{ marginLeft: 10, ...S.badge, background: selected.verified ? "#22c55e22" : "#ef444422", color: selected.verified ? "#22c55e" : "#ef4444" }}>
                  {selected.verified ? "Verified" : "Unverified"}
                </span>
                <span style={{ marginLeft: 6, ...S.badge, background: selected.processed ? "#3b82f622" : "#f59e0b22", color: selected.processed ? "#3b82f6" : "#f59e0b" }}>
                  {selected.processed ? "Processed" : "Unprocessed"}
                </span>
              </h2>
              <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 4 }}>{selected.id}</div>
            </div>

            <div style={S.card}>
              <div style={S.label}>Provider</div>
              <div style={S.value}>{selected.provider}</div>
              <div style={S.label}>Provider Reference</div>
              <div style={{ ...S.value, ...S.mono }}>{selected.provider_ref ?? "—"}</div>
              <div style={S.label}>Received At</div>
              <div style={S.value}>{fmtTime(selected.received_at)}</div>
              {selected.error && (
                <>
                  <div style={S.label}>Error</div>
                  <div style={{ ...S.value, color: "#ef4444" }}>{selected.error}</div>
                </>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ── Identity tab ──────────────────────────────────────────────────────────────

type VerificationRow = {
  id: string; case_id: string; step_id: string; provider: string;
  status: string; result: string | null; verification_url: string | null;
  created_at: string; completed_at: string | null;
};

function IdentityTab() {
  const [caseSearch, setCaseSearch] = useState("");
  const [rows, setRows]             = useState<VerificationRow[]>([]);
  const [selected, setSelected]     = useState<VerificationRow | null>(null);
  const [loading, setLoading]       = useState(false);
  const [statusFilter, setStatusFilter] = useState("");

  const load = useCallback(async (caseId?: string) => {
    setLoading(true);
    const url = caseId
      ? `/api/v1/identity/cases/${caseId}/verifications`
      : `/api/v1/identity/verifications${statusFilter ? `?status=${statusFilter}` : ""}`;
    const r = await authFetch(url);
    if (r.ok) setRows(await r.json()); else setRows([]);
    setLoading(false);
  }, [statusFilter]);

  useEffect(() => { load(); }, [load]);

  const statusColor: Record<string, string> = { pending: "#94a3b8", in_progress: "#3b82f6", complete: "#22c55e", withdrawn: "#6b7280" };
  const resultColor: Record<string, string> = { clear: "#22c55e", consider: "#f59e0b", unidentified: "#ef4444" };

  return (
    <div style={S.body}>
      <div style={S.sidebar}>
        <div style={S.sideHead}>Identity Verifications {loading && <span style={{ fontSize: 10, fontWeight: 400, color: "var(--text-muted)" }}>loading…</span>}</div>
        <StatusBar options={["pending","in_progress","complete","withdrawn"]} value={statusFilter} onChange={setStatusFilter} />
        <CaseFilterBar value={caseSearch} onChange={setCaseSearch} onSearch={v => v ? load(v) : load()} loading={loading} />
        <div style={S.list}>
          {rows.length === 0 && <div style={{ ...S.empty, padding: "16px 14px", fontSize: 12 }}>No verifications found.</div>}
          {rows.map(r => {
            const sc = statusColor[r.status] ?? "#94a3b8";
            return (
              <div key={r.id} style={{ ...S.row, ...(selected?.id === r.id ? S.rowActive : {}) }} onClick={() => setSelected(r)}>
                <span style={{ ...S.badge, background: "#0d948822", color: "#0d9488" }}>🪪</span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 12, fontWeight: 500 }}>{r.provider}</div>
                  <div style={{ fontSize: 10, color: "var(--text-muted)" }}>{r.step_id}</div>
                </div>
                <span style={{ ...S.badge, background: sc + "22", color: sc }}>{r.status}</span>
              </div>
            );
          })}
        </div>
      </div>
      <div style={S.detail}>
        {!selected ? <div style={S.empty}>Select a verification to view details</div> : (
          <div style={S.card}>
            <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 12 }}>
              <span style={{ fontSize: 16, fontWeight: 700 }}>{selected.provider} — {selected.step_id}</span>
              <span style={{ ...S.badge, background: (statusColor[selected.status] ?? "#94a3b8") + "22", color: statusColor[selected.status] ?? "#94a3b8" }}>{selected.status}</span>
              {selected.result && <span style={{ ...S.badge, background: (resultColor[selected.result] ?? "#94a3b8") + "22", color: resultColor[selected.result] ?? "#94a3b8" }}>{selected.result}</span>}
            </div>
            <div style={S.label}>Case ID</div><div style={{ ...S.value, ...S.mono }}>{selected.case_id}</div>
            <div style={S.label}>Created</div><div style={S.value}>{new Date(selected.created_at).toLocaleString()}</div>
            {selected.completed_at && <><div style={S.label}>Completed</div><div style={S.value}>{new Date(selected.completed_at).toLocaleString()}</div></>}
            {selected.verification_url && selected.status === "pending" && (
              <><div style={S.label}>Verification URL</div>
              <a href={selected.verification_url} target="_blank" rel="noreferrer" style={{ fontSize: 12, color: "var(--accent)", wordBreak: "break-all", display: "block", marginBottom: 12 }}>{selected.verification_url}</a></>
            )}
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 8 }}>
              Raw PII (DOB, document number) is not stored. Only status and result are kept.
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── E-Sign tab ────────────────────────────────────────────────────────────────

type ESignRow = {
  id: string; case_id: string; step_id: string; provider: string;
  envelope_id: string | null; signing_url: string | null;
  document_name: string | null; signer_email: string | null;
  status: string; signed_at: string | null; created_at: string;
};

function ESignTab() {
  const [caseSearch, setCaseSearch] = useState("");
  const [rows, setRows]             = useState<ESignRow[]>([]);
  const [selected, setSelected]     = useState<ESignRow | null>(null);
  const [loading, setLoading]       = useState(false);
  const [statusFilter, setStatusFilter] = useState("");

  const load = useCallback(async (caseId?: string) => {
    setLoading(true);
    const url = caseId
      ? `/api/v1/esign/cases/${caseId}/requests`
      : `/api/v1/esign/requests${statusFilter ? `?status=${statusFilter}` : ""}`;
    const r = await authFetch(url);
    if (r.ok) setRows(await r.json()); else setRows([]);
    setLoading(false);
  }, [statusFilter]);

  useEffect(() => { load(); }, [load]);

  const statusColor: Record<string, string> = { pending: "#94a3b8", sent: "#3b82f6", delivered: "#0d9488", completed: "#22c55e", declined: "#ef4444", voided: "#6b7280" };

  return (
    <div style={S.body}>
      <div style={S.sidebar}>
        <div style={S.sideHead}>E-Sign Requests {loading && <span style={{ fontSize: 10, fontWeight: 400, color: "var(--text-muted)" }}>loading…</span>}</div>
        <StatusBar options={["pending","sent","delivered","completed","declined","voided"]} value={statusFilter} onChange={setStatusFilter} />
        <CaseFilterBar value={caseSearch} onChange={setCaseSearch} onSearch={v => v ? load(v) : load()} loading={loading} />
        <div style={S.list}>
          {rows.length === 0 && <div style={{ ...S.empty, padding: "16px 14px", fontSize: 12 }}>No e-sign requests found.</div>}
          {rows.map(r => {
            const sc = statusColor[r.status] ?? "#94a3b8";
            return (
              <div key={r.id} style={{ ...S.row, ...(selected?.id === r.id ? S.rowActive : {}) }} onClick={() => setSelected(r)}>
                <span style={{ ...S.badge, background: "#f59e0b22", color: "#f59e0b" }}>✍</span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 12, fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.document_name ?? r.step_id}</div>
                  <div style={{ fontSize: 10, color: "var(--text-muted)" }}>{r.signer_email}</div>
                </div>
                <span style={{ ...S.badge, background: sc + "22", color: sc }}>{r.status}</span>
              </div>
            );
          })}
        </div>
      </div>
      <div style={S.detail}>
        {!selected ? <div style={S.empty}>Select a request to view details</div> : (
          <div style={S.card}>
            <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 12 }}>
              <span style={{ fontSize: 16, fontWeight: 700 }}>{selected.document_name ?? "Document"}</span>
              <span style={{ ...S.badge, background: (statusColor[selected.status] ?? "#94a3b8") + "22", color: statusColor[selected.status] ?? "#94a3b8" }}>{selected.status}</span>
            </div>
            <div style={S.label}>Signer</div><div style={S.value}>{selected.signer_email}</div>
            <div style={S.label}>Envelope ID</div><div style={{ ...S.value, ...S.mono }}>{selected.envelope_id ?? "—"}</div>
            <div style={S.label}>Case ID</div><div style={{ ...S.value, ...S.mono }}>{selected.case_id}</div>
            <div style={S.label}>Created</div><div style={S.value}>{new Date(selected.created_at).toLocaleString()}</div>
            {selected.signed_at && <><div style={S.label}>Signed At</div><div style={S.value}>{new Date(selected.signed_at).toLocaleString()}</div></>}
            {selected.signing_url && ["sent","delivered"].includes(selected.status) && (
              <><div style={S.label}>Signing URL</div>
              <a href={selected.signing_url} target="_blank" rel="noreferrer" style={{ fontSize: 12, color: "var(--accent)", wordBreak: "break-all", display: "block", marginBottom: 12 }}>{selected.signing_url}</a></>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ── CRM tab ───────────────────────────────────────────────────────────────────

function CrmTab() {
  const [caseSearch, setCaseSearch] = useState("");
  const [rows, setRows]   = useState<any[]>([]);
  const [selected, setSel]= useState<any | null>(null);
  const [loading, setLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState("");

  const load = useCallback(async (caseId?: string) => {
    setLoading(true);
    const url = caseId
      ? `/api/v1/crm/cases/${caseId}/records`
      : `/api/v1/crm/records${statusFilter ? `?status=${statusFilter}` : ""}`;
    const r = await authFetch(url);
    if (r.ok) setRows(await r.json()); else setRows([]);
    setLoading(false);
  }, [statusFilter]);

  useEffect(() => { load(); }, [load]);

  const sc = (s: string) => ({ pending: "#94a3b8", synced: "#22c55e", failed: "#ef4444" }[s] ?? "#94a3b8");

  return (
    <div style={S.body}>
      <div style={S.sidebar}>
        <div style={S.sideHead}>CRM Sync Records {loading && <span style={{ fontSize: 10, fontWeight: 400, color: "var(--text-muted)" }}>loading…</span>}</div>
        <StatusBar options={["pending","synced","failed"]} value={statusFilter} onChange={setStatusFilter} />
        <CaseFilterBar value={caseSearch} onChange={setCaseSearch} onSearch={v => v ? load(v) : load()} loading={loading} />
        <div style={S.list}>
          {!rows.length && <div style={{ ...S.empty, padding: "16px 14px", fontSize: 12 }}>No CRM records.</div>}
          {rows.map(r => (
            <div key={r.id} style={{ ...S.row, ...(selected?.id === r.id ? S.rowActive : {}) }} onClick={() => setSel(r)}>
              <span style={{ ...S.badge, background: "#0ea5e922", color: "#0ea5e9" }}>☁</span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12, fontWeight: 500 }}>{r.provider}</div>
                <div style={{ fontSize: 10, color: "var(--text-muted)" }}>{r.step_id}</div>
              </div>
              <span style={{ ...S.badge, background: sc(r.status) + "22", color: sc(r.status) }}>{r.status}</span>
            </div>
          ))}
        </div>
      </div>
      <div style={S.detail}>
        {!selected ? <div style={S.empty}>Select a record to view details</div> : (
          <div style={S.card}>
            <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 12 }}>{selected.provider} — {selected.crm_object_type ?? selected.step_id}</div>
            <div style={S.label}>CRM Record ID</div><div style={{ ...S.value, ...S.mono }}>{selected.crm_record_id ?? "—"}</div>
            <div style={S.label}>Status</div><div style={S.value}><span style={{ ...S.badge, background: sc(selected.status) + "22", color: sc(selected.status) }}>{selected.status}</span></div>
            {selected.crm_record_url && <><div style={S.label}>Link</div><a href={selected.crm_record_url} target="_blank" rel="noreferrer" style={{ fontSize: 12, color: "var(--accent)", display: "block", marginBottom: 12 }}>View in Salesforce ↗</a></>}
            {selected.error && <><div style={S.label}>Error</div><div style={{ ...S.value, color: "#ef4444" }}>{selected.error}</div></>}
            <div style={S.label}>Created</div><div style={S.value}>{new Date(selected.created_at).toLocaleString()}</div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Invoices tab ──────────────────────────────────────────────────────────────

function InvoicesTab() {
  const [caseSearch, setCaseSearch] = useState("");
  const [rows, setRows]   = useState<any[]>([]);
  const [selected, setSel]= useState<any | null>(null);
  const [loading, setLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState("");

  const load = useCallback(async (caseId?: string) => {
    setLoading(true);
    const url = caseId
      ? `/api/v1/invoices/cases/${caseId}/records`
      : `/api/v1/invoices/records${statusFilter ? `?status=${statusFilter}` : ""}`;
    const r = await authFetch(url);
    if (r.ok) setRows(await r.json()); else setRows([]);
    setLoading(false);
  }, [statusFilter]);

  useEffect(() => { load(); }, [load]);

  const sc = (s: string) => ({ pending: "#94a3b8", draft: "#3b82f6", authorised: "#22c55e", paid: "#22c55e", failed: "#ef4444" }[s] ?? "#94a3b8");

  return (
    <div style={S.body}>
      <div style={S.sidebar}>
        <div style={S.sideHead}>Invoices {loading && <span style={{ fontSize: 10, fontWeight: 400, color: "var(--text-muted)" }}>loading…</span>}</div>
        <StatusBar options={["draft","submitted","authorised","paid","failed"]} value={statusFilter} onChange={setStatusFilter} />
        <CaseFilterBar value={caseSearch} onChange={setCaseSearch} onSearch={v => v ? load(v) : load()} loading={loading} />
        <div style={S.list}>
          {!rows.length && <div style={{ ...S.empty, padding: "16px 14px", fontSize: 12 }}>No invoices.</div>}
          {rows.map(r => (
            <div key={r.id} style={{ ...S.row, ...(selected?.id === r.id ? S.rowActive : {}) }} onClick={() => setSel(r)}>
              <span style={{ ...S.badge, background: "#10b98122", color: "#10b981" }}>🧾</span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12, fontWeight: 500 }}>{r.invoice_number ?? "Draft"}</div>
                <div style={{ fontSize: 10, color: "var(--text-muted)" }}>{r.contact_name}</div>
              </div>
              <span style={{ ...S.badge, background: sc(r.status) + "22", color: sc(r.status) }}>{r.status}</span>
            </div>
          ))}
        </div>
      </div>
      <div style={S.detail}>
        {!selected ? <div style={S.empty}>Select an invoice to view details</div> : (
          <div style={S.card}>
            <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 12 }}>{selected.invoice_number ?? "Invoice Draft"}</div>
            <div style={S.label}>Contact</div><div style={S.value}>{selected.contact_name}</div>
            <div style={S.label}>Amount</div><div style={S.value}>{selected.amount_cents != null ? `${selected.currency.toUpperCase()} ${(selected.amount_cents / 100).toFixed(2)}` : "—"}</div>
            <div style={S.label}>Status</div><div style={S.value}><span style={{ ...S.badge, background: sc(selected.status) + "22", color: sc(selected.status) }}>{selected.status}</span></div>
            {selected.invoice_url && <><div style={S.label}>Link</div><a href={selected.invoice_url} target="_blank" rel="noreferrer" style={{ fontSize: 12, color: "var(--accent)", display: "block", marginBottom: 12 }}>View in Xero ↗</a></>}
            <div style={S.label}>Created</div><div style={S.value}>{new Date(selected.created_at).toLocaleString()}</div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── SMS Tab ───────────────────────────────────────────────────────────────────

function SmsTab() {
  const [caseSearch, setCaseSearch] = useState("");
  const [rows, setRows]   = useState<any[]>([]);
  const [selected, setSel]= useState<any | null>(null);
  const [loading, setLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState("");

  const load = useCallback(async (caseId?: string) => {
    setLoading(true);
    const url = caseId
      ? `/api/v1/comms/sms/cases/${caseId}/messages`
      : `/api/v1/comms/sms/messages${statusFilter ? `?status=${statusFilter}` : ""}`;
    const r = await authFetch(url);
    if (r.ok) setRows(await r.json()); else setRows([]);
    setLoading(false);
  }, [statusFilter]);

  useEffect(() => { load(); }, [load]);

  const sc = (s: string) => ({ pending: "#94a3b8", queued: "#3b82f6", sent: "#22c55e", delivered: "#22c55e", failed: "#ef4444", undelivered: "#ef4444" }[s] ?? "#94a3b8");

  return (
    <div style={S.body}>
      <div style={S.sidebar}>
        <div style={S.sideHead}>SMS Messages {loading && <span style={{ fontSize: 10, fontWeight: 400, color: "var(--text-muted)" }}>loading…</span>}</div>
        <StatusBar options={["pending","queued","sent","delivered","failed"]} value={statusFilter} onChange={setStatusFilter} />
        <CaseFilterBar value={caseSearch} onChange={setCaseSearch} onSearch={v => v ? load(v) : load()} loading={loading} />
        <div style={S.list}>
          {!rows.length && <div style={{ ...S.empty, padding: "16px 14px", fontSize: 12 }}>No SMS messages.</div>}
          {rows.map(r => (
            <div key={r.id} style={{ ...S.row, ...(selected?.id === r.id ? S.rowActive : {}) }} onClick={() => setSel(r)}>
              <span style={{ ...S.badge, background: "#3b82f622", color: "#3b82f6" }}>📱</span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12, fontWeight: 500 }}>{r.to_number}</div>
                <div style={{ fontSize: 10, color: "var(--text-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.body}</div>
              </div>
              <span style={{ ...S.badge, background: sc(r.status) + "22", color: sc(r.status) }}>{r.status}</span>
            </div>
          ))}
        </div>
      </div>
      <div style={S.detail}>
        {!selected ? <div style={S.empty}>Select a message to view details</div> : (
          <div style={S.card}>
            <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 12 }}>SMS to {selected.to_number}</div>
            <div style={S.label}>Status</div><div style={S.value}><span style={{ ...S.badge, background: sc(selected.status) + "22", color: sc(selected.status) }}>{selected.status}</span></div>
            <div style={S.label}>Message SID</div><div style={S.value}>{selected.message_sid ?? "—"}</div>
            <div style={S.label}>From</div><div style={S.value}>{selected.from_number ?? "—"}</div>
            <div style={S.label}>Body</div><div style={{ ...S.value, whiteSpace: "pre-wrap", fontStyle: "italic" }}>"{selected.body}"</div>
            {selected.error && <><div style={S.label}>Error</div><div style={{ ...S.value, color: "#ef4444" }}>{selected.error}</div></>}
            <div style={S.label}>Created</div><div style={S.value}>{new Date(selected.created_at).toLocaleString()}</div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Slack Tab ─────────────────────────────────────────────────────────────────

function SlackTab() {
  const [caseSearch, setCaseSearch] = useState("");
  const [rows, setRows]   = useState<any[]>([]);
  const [selected, setSel]= useState<any | null>(null);
  const [loading, setLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState("");

  const load = useCallback(async (caseId?: string) => {
    setLoading(true);
    const url = caseId
      ? `/api/v1/comms/slack/cases/${caseId}/notifications`
      : `/api/v1/comms/slack/notifications${statusFilter ? `?status=${statusFilter}` : ""}`;
    const r = await authFetch(url);
    if (r.ok) setRows(await r.json()); else setRows([]);
    setLoading(false);
  }, [statusFilter]);

  useEffect(() => { load(); }, [load]);

  const sc = (s: string) => ({ pending: "#94a3b8", sent: "#22c55e", failed: "#ef4444" }[s] ?? "#94a3b8");

  return (
    <div style={S.body}>
      <div style={S.sidebar}>
        <div style={S.sideHead}>Slack Notifications {loading && <span style={{ fontSize: 10, fontWeight: 400, color: "var(--text-muted)" }}>loading…</span>}</div>
        <StatusBar options={["pending","sent","failed"]} value={statusFilter} onChange={setStatusFilter} />
        <CaseFilterBar value={caseSearch} onChange={setCaseSearch} onSearch={v => v ? load(v) : load()} loading={loading} />
        <div style={S.list}>
          {!rows.length && <div style={{ ...S.empty, padding: "16px 14px", fontSize: 12 }}>No Slack notifications.</div>}
          {rows.map(r => (
            <div key={r.id} style={{ ...S.row, ...(selected?.id === r.id ? S.rowActive : {}) }} onClick={() => setSel(r)}>
              <span style={{ ...S.badge, background: "#0f766e22", color: "#0f766e" }}>💬</span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12, fontWeight: 500 }}>{r.channel ? `#${r.channel}` : "Default"}</div>
                <div style={{ fontSize: 10, color: "var(--text-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.message}</div>
              </div>
              <span style={{ ...S.badge, background: sc(r.status) + "22", color: sc(r.status) }}>{r.status}</span>
            </div>
          ))}
        </div>
      </div>
      <div style={S.detail}>
        {!selected ? <div style={S.empty}>Select a notification to view details</div> : (
          <div style={S.card}>
            <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 12 }}>Slack — {selected.channel ? `#${selected.channel}` : "Default channel"}</div>
            <div style={S.label}>Status</div><div style={S.value}><span style={{ ...S.badge, background: sc(selected.status) + "22", color: sc(selected.status) }}>{selected.status}</span></div>
            <div style={S.label}>Message</div><div style={{ ...S.value, whiteSpace: "pre-wrap", fontStyle: "italic" }}>"{selected.message}"</div>
            {selected.error && <><div style={S.label}>Error</div><div style={{ ...S.value, color: "#ef4444" }}>{selected.error}</div></>}
            <div style={S.label}>Created</div><div style={S.value}>{new Date(selected.created_at).toLocaleString()}</div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Doc Intel Tab ─────────────────────────────────────────────────────────────

function DocIntelTab() {
  const [caseSearch, setCaseSearch] = useState("");
  const [rows, setRows]   = useState<any[]>([]);
  const [selected, setSel]= useState<any | null>(null);
  const [loading, setLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState("");

  const load = useCallback(async (caseId?: string) => {
    setLoading(true);
    const url = caseId
      ? `/api/v1/docintel/cases/${caseId}/extractions`
      : `/api/v1/docintel/extractions${statusFilter ? `?status=${statusFilter}` : ""}`;
    const r = await authFetch(url);
    if (r.ok) setRows(await r.json()); else setRows([]);
    setLoading(false);
  }, [statusFilter]);

  useEffect(() => { load(); }, [load]);

  const sc = (s: string) => ({ pending: "#94a3b8", processing: "#3b82f6", completed: "#22c55e", failed: "#ef4444" }[s] ?? "#94a3b8");

  return (
    <div style={S.body}>
      <div style={S.sidebar}>
        <div style={S.sideHead}>Doc Extractions {loading && <span style={{ fontSize: 10, fontWeight: 400, color: "var(--text-muted)" }}>loading…</span>}</div>
        <StatusBar options={["pending","processing","completed","failed"]} value={statusFilter} onChange={setStatusFilter} />
        <CaseFilterBar value={caseSearch} onChange={setCaseSearch} onSearch={v => v ? load(v) : load()} loading={loading} />
        <div style={S.list}>
          {!rows.length && <div style={{ ...S.empty, padding: "16px 14px", fontSize: 12 }}>No extractions.</div>}
          {rows.map(r => (
            <div key={r.id} style={{ ...S.row, ...(selected?.id === r.id ? S.rowActive : {}) }} onClick={() => setSel(r)}>
              <span style={{ ...S.badge, background: "#f59e0b22", color: "#f59e0b" }}>🔍</span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12, fontWeight: 500 }}>{r.document_name ?? r.provider}</div>
                <div style={{ fontSize: 10, color: "var(--text-muted)" }}>{Object.keys(r.extracted_fields || {}).length} fields</div>
              </div>
              <span style={{ ...S.badge, background: sc(r.status) + "22", color: sc(r.status) }}>{r.status}</span>
            </div>
          ))}
        </div>
      </div>
      <div style={S.detail}>
        {!selected ? <div style={S.empty}>Select an extraction job</div> : (
          <div style={S.card}>
            <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 12 }}>{selected.document_name ?? "Extraction Job"}</div>
            <div style={S.label}>Status</div><div style={S.value}><span style={{ ...S.badge, background: sc(selected.status) + "22", color: sc(selected.status) }}>{selected.status}</span></div>
            <div style={S.label}>Provider</div><div style={S.value}>{selected.provider}</div>
            {selected.confidence != null && <><div style={S.label}>Confidence</div><div style={S.value}>{Math.round(selected.confidence * 100)}%</div></>}
            {Object.keys(selected.extracted_fields || {}).length > 0 && (
              <>
                <div style={S.label}>Extracted Fields</div>
                <div style={{ background: "var(--bg-elevated)", borderRadius: 6, padding: "8px 10px", marginBottom: 12 }}>
                  {Object.entries(selected.extracted_fields).map(([k, v]) => (
                    <div key={k} style={{ display: "flex", gap: 8, marginBottom: 3, fontSize: 11 }}>
                      <span style={{ color: "var(--text-muted)", minWidth: 130, fontWeight: 600 }}>{k}</span>
                      <span style={{ color: "var(--text-primary)" }}>{String(v)}</span>
                    </div>
                  ))}
                </div>
              </>
            )}
            {selected.error && <><div style={S.label}>Error</div><div style={{ ...S.value, color: "#ef4444" }}>{selected.error}</div></>}
            <div style={S.label}>Created</div><div style={S.value}>{new Date(selected.created_at).toLocaleString()}</div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Cloud Storage Tab ─────────────────────────────────────────────────────────

function CloudStorageTab() {
  const [caseSearch, setCaseSearch] = useState("");
  const [rows, setRows]   = useState<any[]>([]);
  const [selected, setSel]= useState<any | null>(null);
  const [loading, setLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState("");
  const [copied, setCopied] = useState(false);

  const load = useCallback(async (caseId?: string) => {
    setLoading(true);
    const url = caseId
      ? `/api/v1/docintel/cases/${caseId}/storage`
      : `/api/v1/docintel/storage${statusFilter ? `?status=${statusFilter}` : ""}`;
    const r = await authFetch(url);
    if (r.ok) setRows(await r.json()); else setRows([]);
    setLoading(false);
  }, [statusFilter]);

  useEffect(() => { load(); }, [load]);

  const sc = (s: string) => ({ pending: "#3b82f6", uploaded: "#22c55e", failed: "#ef4444" }[s] ?? "#94a3b8");

  return (
    <div style={S.body}>
      <div style={S.sidebar}>
        <div style={S.sideHead}>Cloud Storage {loading && <span style={{ fontSize: 10, fontWeight: 400, color: "var(--text-muted)" }}>loading…</span>}</div>
        <StatusBar options={["pending","uploaded","failed"]} value={statusFilter} onChange={setStatusFilter} />
        <CaseFilterBar value={caseSearch} onChange={setCaseSearch} onSearch={v => v ? load(v) : load()} loading={loading} />
        <div style={S.list}>
          {!rows.length && <div style={{ ...S.empty, padding: "16px 14px", fontSize: 12 }}>No storage routes.</div>}
          {rows.map(r => (
            <div key={r.id} style={{ ...S.row, ...(selected?.id === r.id ? S.rowActive : {}) }} onClick={() => setSel(r)}>
              <span style={{ ...S.badge, background: "#0d948822", color: "#0d9488" }}>☁️</span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12, fontWeight: 500 }}>{r.document_name}</div>
                <div style={{ fontSize: 10, color: "var(--text-muted)" }}>{r.bucket ?? r.provider}</div>
              </div>
              <span style={{ ...S.badge, background: sc(r.status) + "22", color: sc(r.status) }}>{r.status}</span>
            </div>
          ))}
        </div>
      </div>
      <div style={S.detail}>
        {!selected ? <div style={S.empty}>Select a storage route</div> : (
          <div style={S.card}>
            <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 12 }}>{selected.document_name}</div>
            <div style={S.label}>Status</div><div style={S.value}><span style={{ ...S.badge, background: sc(selected.status) + "22", color: sc(selected.status) }}>{selected.status}</span></div>
            <div style={S.label}>Provider</div><div style={S.value}>{selected.provider}</div>
            <div style={S.label}>Bucket</div><div style={S.value}>{selected.bucket ?? "—"}</div>
            <div style={S.label}>Object Key</div><div style={{ ...S.value, wordBreak: "break-all", fontFamily: "var(--font-mono)", fontSize: 10 }}>{selected.object_key ?? "—"}</div>
            {selected.presigned_url && (
              <>
                <div style={S.label}>Upload URL</div>
                <button onClick={() => { navigator.clipboard.writeText(selected.presigned_url); setCopied(true); setTimeout(() => setCopied(false), 2000); }}
                  style={{ fontSize: 11, color: "#0d9488", background: "none", border: "1px solid #0d948844", borderRadius: 4, padding: "4px 10px", cursor: "pointer", marginBottom: 12 }}>
                  {copied ? "Copied!" : "Copy Presigned URL"}
                </button>
              </>
            )}
            {selected.error && <><div style={S.label}>Error</div><div style={{ ...S.value, color: "#ef4444" }}>{selected.error}</div></>}
            <div style={S.label}>Created</div><div style={S.value}>{new Date(selected.created_at).toLocaleString()}</div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Root ──────────────────────────────────────────────────────────────────────

type Tab = "connectors" | "payments" | "webhooks" | "identity" | "esign" | "crm" | "invoices" | "sms" | "slack" | "docintel" | "storage";

export default function HxConnect() {
  const [tab, setTab] = useState<Tab>("connectors");

  const TABS: { key: Tab; label: string }[] = [
    { key: "connectors", label: "Connectors" },
    { key: "payments",   label: "Payments" },
    { key: "webhooks",   label: "Webhook Events" },
    { key: "identity",   label: "Identity / KYC" },
    { key: "esign",      label: "E-Sign" },
    { key: "crm",        label: "CRM" },
    { key: "invoices",   label: "Invoices" },
    { key: "sms",        label: "SMS" },
    { key: "slack",      label: "Slack" },
    { key: "docintel",   label: "Doc Intel" },
    { key: "storage",    label: "Cloud Storage" },
  ];

  return (
    <div style={S.page}>
      <div style={S.tabs}>
        {TABS.map(({ key, label }) => (
          <button key={key} style={{ ...S.tab, ...(tab === key ? S.tabActive : {}) }} onClick={() => setTab(key)}>{label}</button>
        ))}
      </div>

      {tab === "connectors" && <ConnectorsTab />}
      {tab === "payments"   && <PaymentsTab />}
      {tab === "webhooks"   && <WebhooksTab />}
      {tab === "identity"   && <IdentityTab />}
      {tab === "esign"      && <ESignTab />}
      {tab === "crm"        && <CrmTab />}
      {tab === "invoices"   && <InvoicesTab />}
      {tab === "sms"        && <SmsTab />}
      {tab === "slack"      && <SlackTab />}
      {tab === "docintel"   && <DocIntelTab />}
      {tab === "storage"    && <CloudStorageTab />}
    </div>
  );
}
