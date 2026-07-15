/**
 * HxCheckout — commerce integration layer (marketplace app `velaris/hxcheckout`).
 * Overview · Orders · API Keys · Webhooks all hit /api/v1/checkout, which is
 * install-gated: when HxCheckout is not installed the API 404s and the whole page
 * shows an "install from Marketplace" notice instead (dark until activation).
 */
import React, { useState, useEffect, useCallback } from "react";
import { Button } from "@shared/components";

const API = "/api/v1/checkout";

function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}
function authFetch(url: string, opts: RequestInit = {}): Promise<Response> {
  return fetch(url, { ...opts, headers: { "Content-Type": "application/json", ..._authHdr(), ...opts.headers } });
}

const S: Record<string, React.CSSProperties> = {
  page:    { padding: "var(--space-xl) var(--space-2xl)", width: "100%", overflow: "auto", height: "100%", boxSizing: "border-box" as const },
  tabs:    { display: "flex", gap: 2, marginBottom: "var(--space-lg)", borderBottom: "1px solid var(--border)" },
  tab:     { padding: "8px 18px", fontSize: 13, fontWeight: 500, background: "none", border: "none", borderBottom: "2px solid transparent", cursor: "pointer", color: "var(--text-muted)", marginBottom: -1 },
  tabActive: { borderBottomColor: "var(--accent)", color: "var(--accent)", fontWeight: 700 },
  card:    { background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 8, padding: "var(--space-lg)", marginBottom: "var(--space-md)" },
  label:   { fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase" as const, letterSpacing: "0.05em", marginBottom: 4 },
  input:   { width: "100%", padding: "8px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" as const, marginBottom: 10 },
  row:     { display: "flex", gap: 10, alignItems: "center", padding: "10px 14px", borderBottom: "1px solid var(--border-subtle)", fontSize: 13 },
  badge:   { fontSize: 10, padding: "2px 8px", borderRadius: 4, fontWeight: 700, textTransform: "uppercase" as const, color: "#fff" },
  mono:    { fontFamily: "var(--font-mono)", fontSize: 11, wordBreak: "break-all" as const },
  kpi:     { display: "inline-block", minWidth: 150, padding: "14px 18px", background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 8, marginRight: 12 },
  kpiNum:  { fontSize: 26, fontWeight: 800, color: "var(--text-primary)" },
  kpiLbl:  { fontSize: 12, color: "var(--text-muted)", marginTop: 2 },
};

const statusColor = (s: string) => ({
  pending_payment: "#f59e0b", awaiting_fulfilment: "#3b82f6", paid: "#22c55e",
  cancelled: "#94a3b8", delivered: "#22c55e",
}[s] ?? "#94a3b8");

function money(cents: number, ccy: string) {
  return `${(cents / 100).toFixed(2)} ${ccy}`;
}

// ── Overview (KPIs) ───────────────────────────────────────────────────────────
function OverviewTab() {
  const [sum, setSum] = useState<any>(null);
  useEffect(() => { authFetch(`${API}/analytics/summary`).then(r => r.json()).then(setSum).catch(() => {}); }, []);
  if (!sum) return <div style={{ color: "var(--text-muted)" }}>Loading…</div>;
  return (
    <div>
      <div style={{ marginBottom: 20 }}>
        <div style={S.kpi}><div style={S.kpiNum}>{sum.total_orders ?? 0}</div><div style={S.kpiLbl}>Orders</div></div>
        <div style={S.kpi}><div style={S.kpiNum}>{((sum.total_revenue_cents ?? 0) / 100).toFixed(0)}</div><div style={S.kpiLbl}>Revenue</div></div>
      </div>
      <div style={S.card}>
        <div style={S.label}>Orders by status</div>
        {Object.entries(sum.by_status || {}).map(([s, v]: any) => (
          <div key={s} style={S.row}>
            <span style={{ ...S.badge, background: statusColor(s) }}>{s}</span>
            <span style={{ flex: 1 }} />
            <span>{v.count} orders</span>
            <span style={{ color: "var(--text-muted)" }}>{money(v.revenue_cents, "")}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Orders ─────────────────────────────────────────────────────────────────────
function OrdersTab() {
  const [orders, setOrders] = useState<any[]>([]);
  useEffect(() => { authFetch(`${API}/orders`).then(r => r.json()).then(d => setOrders(d.orders || [])).catch(() => {}); }, []);
  return (
    <div style={S.card}>
      {orders.length === 0 && <div style={{ color: "var(--text-muted)" }}>No orders yet.</div>}
      {orders.map(o => (
        <div key={o.order_id} style={S.row}>
          <span style={S.mono}>{o.tracking_token}</span>
          <span style={{ ...S.badge, background: statusColor(o.status) }}>{o.status}</span>
          {o.is_test && <span style={{ ...S.badge, background: "#a855f7" }}>test</span>}
          <span style={{ flex: 1 }} />
          <span>{o.customer_email || "—"}</span>
          <span>{money(o.total_cents, o.currency)}</span>
          <span style={{ color: "var(--text-muted)", fontSize: 11 }}>{o.source}</span>
        </div>
      ))}
    </div>
  );
}

// ── API Keys (service tokens) ───────────────────────────────────────────────────
function TokensTab() {
  const [tokens, setTokens] = useState<any[]>([]);
  const [label, setLabel] = useState("");
  const [mode, setMode] = useState("live");
  const [revealed, setRevealed] = useState<string | null>(null);

  const load = useCallback(() => {
    authFetch(`${API}/tokens`).then(r => r.json()).then(d => setTokens(d.tokens || [])).catch(() => {});
  }, []);
  useEffect(() => { load(); }, [load]);

  const create = async () => {
    const r = await authFetch(`${API}/tokens`, { method: "POST", body: JSON.stringify({ label, mode }) });
    if (r.ok) { const d = await r.json(); setRevealed(d.token); setLabel(""); load(); }
  };
  const revoke = async (id: string) => { await authFetch(`${API}/tokens/${id}`, { method: "DELETE" }); load(); };

  return (
    <div>
      <div style={S.card}>
        <div style={S.label}>New API key</div>
        <input style={S.input} placeholder="Label (e.g. My Shopify Store)" value={label} onChange={e => setLabel(e.target.value)} />
        <select style={S.input} value={mode} onChange={e => setMode(e.target.value)}>
          <option value="live">Live</option><option value="test">Test</option>
        </select>
        <Button onClick={create}>Generate token</Button>
        {revealed && (
          <div style={{ marginTop: 12, padding: 12, background: "var(--bg-input)", borderRadius: 6 }}>
            <div style={S.label}>Copy now — shown only once</div>
            <div style={S.mono}>{revealed}</div>
          </div>
        )}
      </div>
      <div style={S.card}>
        {tokens.map(t => (
          <div key={t.id} style={S.row}>
            <span style={S.mono}>{t.token_prefix}…</span>
            <span>{t.label}</span>
            <span style={{ ...S.badge, background: t.mode === "test" ? "#a855f7" : "#3b82f6" }}>{t.mode}</span>
            {t.revoked && <span style={{ ...S.badge, background: "#94a3b8" }}>revoked</span>}
            {t.suspended && <span style={{ ...S.badge, background: "#ef4444" }}>suspended</span>}
            <span style={{ flex: 1 }} />
            {!t.revoked && <Button variant="secondary" onClick={() => revoke(t.id)}>Revoke</Button>}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Webhooks (integrations) ─────────────────────────────────────────────────────
function WebhooksTab() {
  const [integrations, setIntegrations] = useState<any[]>([]);
  const [platform, setPlatform] = useState("shopify");
  const [label, setLabel] = useState("");
  const [secret, setSecret] = useState("");

  const load = useCallback(() => {
    authFetch(`${API}/integrations`).then(r => r.json()).then(d => setIntegrations(d.integrations || [])).catch(() => {});
  }, []);
  useEffect(() => { load(); }, [load]);

  const create = async () => {
    const r = await authFetch(`${API}/integrations`, { method: "POST", body: JSON.stringify({ platform, label, hmac_secret: secret }) });
    if (r.ok) { setLabel(""); setSecret(""); load(); }
  };
  const remove = async (id: string) => { await authFetch(`${API}/integrations/${id}`, { method: "DELETE" }); load(); };

  return (
    <div>
      <div style={S.card}>
        <div style={S.label}>New webhook integration</div>
        <select style={S.input} value={platform} onChange={e => setPlatform(e.target.value)}>
          <option value="shopify">Shopify</option><option value="woocommerce">WooCommerce</option>
          <option value="magento">Magento</option><option value="bigcommerce">BigCommerce</option>
          <option value="custom">Custom (field map)</option>
        </select>
        <input style={S.input} placeholder="Label" value={label} onChange={e => setLabel(e.target.value)} />
        <input style={S.input} placeholder="HMAC shared secret" value={secret} onChange={e => setSecret(e.target.value)} />
        <Button onClick={create}>Add integration</Button>
      </div>
      <div style={S.card}>
        {integrations.map(i => (
          <div key={i.id} style={S.row}>
            <span style={{ ...S.badge, background: "#0ea5e9" }}>{i.platform}</span>
            <span>{i.label}</span>
            {!i.enabled && <span style={{ ...S.badge, background: "#94a3b8" }}>disabled</span>}
            <span style={{ flex: 1 }} />
            <span style={S.mono}>{i.webhook_url}</span>
            <Button variant="secondary" onClick={() => remove(i.id)}>Remove</Button>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Not-installed notice ─────────────────────────────────────────────────────────
function NotInstalled() {
  return (
    <div style={S.card}>
      <h3 style={{ marginTop: 0 }}>HxCheckout is not installed</h3>
      <p style={{ color: "var(--text-muted)", fontSize: 13 }}>
        HxCheckout is a marketplace app. Install <code>velaris/hxcheckout</code> from the
        Marketplace to turn on the order intake API (<code>/api/v1/checkout</code>) and this page
        for your tenant.
      </p>
    </div>
  );
}

const TABS = ["Overview", "Orders", "API Keys", "Webhooks"];

export default function HxCheckout() {
  const [tab, setTab] = useState("Overview");
  const [installed, setInstalled] = useState<boolean | null>(null);

  useEffect(() => {
    // Probe install state via any gated endpoint (404 → not installed).
    authFetch(`${API}/tokens`).then(r => setInstalled(r.status !== 404)).catch(() => setInstalled(false));
  }, []);

  return (
    <div style={S.page}>
      <div style={S.tabs}>
        {TABS.map(t => (
          <button key={t} style={{ ...S.tab, ...(tab === t ? S.tabActive : {}) }} onClick={() => setTab(t)}>{t}</button>
        ))}
      </div>
      {installed === false && <NotInstalled />}
      {installed === true && tab === "Overview" && <OverviewTab />}
      {installed === true && tab === "Orders" && <OrdersTab />}
      {installed === true && tab === "API Keys" && <TokensTab />}
      {installed === true && tab === "Webhooks" && <WebhooksTab />}
    </div>
  );
}
