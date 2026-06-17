/**
 * P55 — HxDeploy: Intelligent Deployment Governance
 * Tabs: Environments · Promote · Runs · Change Windows
 */
import React, { useState, useEffect, useCallback, useRef } from "react";
import { Button } from "@shared/components";
import { PlatformRolloutPlans } from "@shared/components/PlatformRolloutPlans";
import { useAuth } from "@/auth";

const API = "/api/v1/deploy";

function authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}`, "Content-Type": "application/json" } : { "Content-Type": "application/json" };
}

async function apiFetch(path: string, opts: RequestInit = {}) {
  return fetch(`${API}${path}`, { ...opts, headers: { ...authHdr(), ...(opts.headers ?? {}) } });
}

const RISK_COLOR: Record<string, string> = {
  low: "#22c55e", medium: "#f59e0b", high: "#ef4444", critical: "#7c3aed",
};

const STATUS_COLOR: Record<string, string> = {
  pending: "#94a3b8", awaiting_approval: "#f59e0b", approved: "#3b82f6",
  deploying: "#0d9488", triggered: "#8b5cf6", deployed: "#22c55e",
  rejected: "#ef4444", failed: "#ef4444", rolled_back: "#f97316",
};

const STATUS_LABEL: Record<string, string> = {
  triggered: "Triggered — Awaiting CI/CD callback",
};

const DAYS_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

const S: Record<string, React.CSSProperties> = {
  page:   { display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" },
  body:   { flex: 1, overflow: "auto", padding: "var(--space-xl) var(--space-2xl)" },
  tabBar: { display: "flex", gap: 2, borderBottom: "1px solid var(--border-subtle)", marginBottom: "var(--space-lg)" },
  tab:    { padding: "10px 16px", fontSize: 12, fontWeight: 500, fontFamily: "var(--font-mono)", textTransform: "uppercase" as const, letterSpacing: "0.04em", border: "none", cursor: "pointer", color: "var(--text-muted)", background: "transparent", borderBottom: "2px solid transparent", marginBottom: -1 },
  tabA:   { color: "var(--accent)", borderBottomColor: "var(--accent)" },
  card:   { background: "var(--bg-card)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)", padding: "var(--space-lg)", marginBottom: "var(--space-md)" },
  label:  { fontSize: 11, fontWeight: 600, color: "var(--text-secondary)", textTransform: "uppercase" as const, letterSpacing: "0.06em", marginBottom: 4, display: "block" },
  value:  { fontSize: 13, color: "var(--text-primary)", marginBottom: 12 },
  badge:  { fontSize: 10, padding: "2px 8px", borderRadius: 4, fontWeight: 700, textTransform: "uppercase" as const, fontFamily: "var(--font-mono)" },
  input:  { width: "100%", padding: "8px 10px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" as const, marginBottom: 10 },
  grid2:  { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 },
};

function Confirm({ msg, onYes, onNo }: { msg: string; onYes: () => void; onNo: () => void }) {
  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.5)", zIndex: 999, display: "flex", alignItems: "center", justifyContent: "center" }}>
      <div style={{ background: "var(--bg-panel)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-md)", padding: 28, width: 360 }}>
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 20, color: "var(--text-primary)" }}>{msg}</div>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <Button variant="secondary" onClick={onNo}>Cancel</Button>
          <Button variant="danger" onClick={onYes}>Delete</Button>
        </div>
      </div>
    </div>
  );
}

// ── Environments Tab ──────────────────────────────────────────────────────────

function EnvironmentsTab({ activeEnv, onEnvsChanged }: { activeEnv: string; onEnvsChanged?: () => void }) {
  const [envs, setEnvs]         = useState<any[]>([]);
  const [showForm, setShowForm]  = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editForm, setEditForm]   = useState<any>({});
  const [deleteId, setDeleteId]   = useState<string | null>(null);
  const [form, setForm]           = useState({ name: "", label: "", url: "", order_index: 0, delivery_method: "manual", webhook_url: "", webhook_secret: "", import_api_key: "" });
  const [msg, setMsg]             = useState<{ text: string; ok: boolean } | null>(null);
  const [testingConn, setTestingConn] = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await apiFetch("/environments");
    if (r.ok) setEnvs(await r.json());
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleAdd = async () => {
    const payload: any = { ...form };
    if (!payload.webhook_secret) delete payload.webhook_secret;
    if (!payload.import_api_key) delete payload.import_api_key;
    if (!payload.webhook_url) delete payload.webhook_url;
    const r = await apiFetch("/environments", { method: "POST", body: JSON.stringify(payload) });
    if (r.ok) { await load(); onEnvsChanged?.(); setShowForm(false); setMsg({ text: "Environment registered.", ok: true }); }
    else { const e = await r.json().catch(() => ({})); setMsg({ text: e.detail || "Failed", ok: false }); }
  };

  const handleEdit = async (id: string) => {
    const payload: any = { ...editForm };
    if (!payload.webhook_secret) delete payload.webhook_secret;
    if (!payload.import_api_key) delete payload.import_api_key;
    const r = await apiFetch(`/environments/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
    if (r.ok) { await load(); setEditingId(null); setMsg({ text: "Environment updated.", ok: true }); }
    else { const e = await r.json().catch(() => ({})); setMsg({ text: e.detail || "Failed", ok: false }); }
  };

  const handleTestConn = async (id: string) => {
    setTestingConn(id);
    const r = await apiFetch(`/environments/${id}/status`);
    const data = await r.json().catch(() => ({}));
    setTestingConn(null);
    setMsg({ text: data.reachable ? `✓ Reachable (${data.response_ms}ms)` : `✗ Unreachable: ${data.error || "no response"}`, ok: !!data.reachable });
  };

  const handleDelete = async () => {
    if (!deleteId) return;
    const r = await apiFetch(`/environments/${deleteId}`, { method: "DELETE" });
    if (r.ok || r.status === 204) { await load(); onEnvsChanged?.(); setMsg({ text: "Environment deleted.", ok: true }); }
    else setMsg({ text: "Failed to delete.", ok: false });
    setDeleteId(null);
  };

  const sorted = [...envs].sort((a, b) => a.order_index - b.order_index);

  return (
    <div>
      {deleteId && <Confirm msg="Delete this environment? This cannot be undone." onYes={handleDelete} onNo={() => setDeleteId(null)} />}

      {/* Swimlane */}
      {sorted.length === 0 && (
        <div style={{ ...S.card, textAlign: "center", color: "var(--text-muted)", fontSize: 13, padding: "28px 16px", marginBottom: 24 }}>
          No environments registered yet. Use <strong>+ Register Environment</strong> below to add your first one.
        </div>
      )}
      {sorted.length > 0 && (
      <div style={{ display: "grid", gridTemplateColumns: `repeat(${sorted.length}, 1fr)`, gap: 12, marginBottom: 24 }}>
        {sorted.map((env: any) => {
          const sc = env.status === "healthy" ? "#22c55e" : env.status === "degraded" ? "#f59e0b" : env.status === "down" ? "#ef4444" : "#94a3b8";
          return (
            <div key={env.name} style={{ ...S.card, borderTop: `3px solid ${sc}`, marginBottom: 0, outline: env.name === activeEnv ? `2px solid var(--accent)` : "none", outlineOffset: 2 }}>
              <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 4 }}>{env.label || env.name}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 8, fontFamily: "var(--font-mono)" }}>{env.name}</div>
              <div style={{ fontSize: 12, marginBottom: 8 }}>
                <span style={{ color: "var(--text-muted)" }}>Version: </span>
                <span style={{ fontWeight: 600 }}>{env.current_version ?? "—"}</span>
              </div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" as const, marginBottom: 4 }}>
                <span style={{ ...S.badge, background: sc + "22", color: sc }}>{env.status ?? "unknown"}</span>
                {env.delivery_method && env.delivery_method !== "manual" && (
                  <span style={{ ...S.badge, background: env.delivery_method === "push" ? "#6366f122" : "#f59e0b22", color: env.delivery_method === "push" ? "#6366f1" : "#f59e0b" }}>
                    {env.delivery_method === "push" ? "⬆ push" : "🔗 webhook"}
                  </span>
                )}
              </div>
              {env.url && <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4, wordBreak: "break-all" }}>{env.url}</div>}
              {env.last_deployed_at && <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 6 }}>Last deploy: {new Date(env.last_deployed_at).toLocaleString()}</div>}
            </div>
          );
        })}
      </div>
      )}

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <div style={{ fontWeight: 700 }}>Registered Environments ({envs.length})</div>
        <Button size="sm" onClick={() => setShowForm(s => !s)}>{showForm ? "Cancel" : "+ Register Environment"}</Button>
      </div>

      {msg && <div style={{ fontSize: 12, padding: "8px 12px", borderRadius: 4, marginBottom: 12, background: msg.ok ? "#22c55e22" : "#ef444422", color: msg.ok ? "#22c55e" : "#ef4444" }}>{msg.text}</div>}

      {sorted.length > 0 && (
        <div style={{ marginBottom: 20 }}>
          {sorted.map((env: any) => (
            <div key={env.id} style={{ ...S.card, padding: "12px 16px", marginBottom: 8 }}>
              {editingId === env.id ? (
                <div>
                  <div style={S.grid2}>
                    <div><span style={S.label}>Label</span><input value={editForm.label ?? ""} onChange={e => setEditForm((f: any) => ({ ...f, label: e.target.value }))} style={{ ...S.input, marginBottom: 0 }} /></div>
                    <div><span style={S.label}>Order</span><input type="number" value={editForm.order_index ?? 0} onChange={e => setEditForm((f: any) => ({ ...f, order_index: +e.target.value }))} style={{ ...S.input, marginBottom: 0 }} /></div>
                  </div>
                  <span style={S.label}>URL (health-check base)</span>
                  <input value={editForm.url ?? ""} onChange={e => setEditForm((f: any) => ({ ...f, url: e.target.value }))} placeholder="https://…" style={S.input} />
                  <span style={S.label}>Delivery Method</span>
                  <select value={editForm.delivery_method ?? "manual"} onChange={e => setEditForm((f: any) => ({ ...f, delivery_method: e.target.value }))} style={S.input}>
                    <option value="manual">Manual (governance only)</option>
                    <option value="webhook">Webhook / CI-CD trigger</option>
                    <option value="push">Push (built-in packager)</option>
                  </select>
                  {editForm.delivery_method === "webhook" && <>
                    <span style={S.label}>Webhook URL</span>
                    <input value={editForm.webhook_url ?? ""} onChange={e => setEditForm((f: any) => ({ ...f, webhook_url: e.target.value }))} placeholder="https://ci.example.com/webhook/deploy" style={S.input} />
                    <span style={S.label}>Webhook Secret (HMAC key — leave blank to keep existing)</span>
                    <input type="password" value={editForm.webhook_secret ?? ""} onChange={e => setEditForm((f: any) => ({ ...f, webhook_secret: e.target.value }))} placeholder="••••••••" style={S.input} />
                  </>}
                  {editForm.delivery_method === "push" && <>
                    <span style={S.label}>Import API Key (leave blank to keep existing)</span>
                    <input type="password" value={editForm.import_api_key ?? ""} onChange={e => setEditForm((f: any) => ({ ...f, import_api_key: e.target.value }))} placeholder="••••••••" style={S.input} />
                    {env.has_import_api_key && <div style={{ fontSize: 11, color: "#22c55e", marginTop: -8, marginBottom: 8 }}>✓ API key is set</div>}
                  </>}
                  <div style={{ display: "flex", gap: 8 }}>
                    <Button size="sm" onClick={() => handleEdit(env.id)}>Save</Button>
                    <Button size="sm" variant="secondary" onClick={() => setEditingId(null)}>Cancel</Button>
                    {env.url && <Button size="sm" variant="secondary" onClick={() => handleTestConn(env.id)}>{testingConn === env.id ? "Testing…" : "Test Connection"}</Button>}
                  </div>
                </div>
              ) : (
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <div>
                    <span style={{ fontWeight: 700, fontSize: 14 }}>{env.label}</span>
                    <span style={{ fontSize: 12, color: "var(--text-muted)", marginLeft: 8 }}>({env.name})</span>
                    {env.url && <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{env.url}</div>}
                    <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
                      Order: {env.order_index} · Version: {env.current_version ?? "—"} · Delivery: <strong>{env.delivery_method ?? "manual"}</strong>
                      {env.delivery_method === "push" && env.has_import_api_key && <span style={{ color: "#22c55e", marginLeft: 6 }}>✓ key set</span>}
                      {env.delivery_method === "webhook" && env.has_webhook_secret && <span style={{ color: "#22c55e", marginLeft: 6 }}>✓ secret set</span>}
                    </div>
                  </div>
                  <div style={{ display: "flex", gap: 6 }}>
                    <Button size="sm" variant="secondary" onClick={() => { setEditingId(env.id); setEditForm({ label: env.label, url: env.url || "", order_index: env.order_index, name: env.name, delivery_method: env.delivery_method || "manual", webhook_url: env.webhook_url || "", webhook_secret: "", import_api_key: "" }); }}>Edit</Button>
                    <Button size="sm" variant="danger" onClick={() => setDeleteId(env.id)}>Delete</Button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {showForm && (
        <div style={S.card}>
          <div style={S.grid2}>
            <div>
              <span style={S.label}>Name (ID — e.g. dev, staging, uat, prod)</span>
              <input value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value.toLowerCase().replace(/\s+/g, "-") }))} placeholder="dev" style={S.input} />
            </div>
            <div>
              <span style={S.label}>Label</span>
              <input value={form.label} onChange={e => setForm(f => ({ ...f, label: e.target.value }))} style={S.input} />
            </div>
          </div>
          <span style={S.label}>URL (health-check base / target instance)</span>
          <input value={form.url} onChange={e => setForm(f => ({ ...f, url: e.target.value }))} placeholder="https://staging.yourdomain.com" style={S.input} />
          <span style={S.label}>Promotion Order (0 = first)</span>
          <input type="number" min={0} max={10} value={form.order_index} onChange={e => setForm(f => ({ ...f, order_index: +e.target.value }))} style={{ ...S.input, maxWidth: 120 }} />
          <span style={S.label}>Delivery Method</span>
          <select value={form.delivery_method} onChange={e => setForm(f => ({ ...f, delivery_method: e.target.value }))} style={S.input}>
            <option value="manual">Manual — governance only, no auto-push</option>
            <option value="webhook">Webhook — trigger external CI/CD pipeline</option>
            <option value="push">Push — built-in packager sends bundle to target</option>
          </select>
          {form.delivery_method === "webhook" && <>
            <span style={S.label}>Webhook URL</span>
            <input value={form.webhook_url} onChange={e => setForm(f => ({ ...f, webhook_url: e.target.value }))} placeholder="https://ci.example.com/webhook/deploy" style={S.input} />
            <span style={S.label}>Webhook Secret (used to sign payload with HMAC-SHA256)</span>
            <input type="password" value={form.webhook_secret} onChange={e => setForm(f => ({ ...f, webhook_secret: e.target.value }))} placeholder="my-secret-key" style={S.input} />
          </>}
          {form.delivery_method === "push" && <>
            <span style={S.label}>Import API Key (target instance key for /deploy/import)</span>
            <input type="password" value={form.import_api_key} onChange={e => setForm(f => ({ ...f, import_api_key: e.target.value }))} placeholder="generate on target instance" style={S.input} />
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: -8, marginBottom: 12 }}>
              On the target Velaris instance, run: <code style={{ fontFamily: "var(--font-mono)", background: "var(--bg-subtle)", padding: "1px 4px", borderRadius: 3 }}>python -c "import secrets; print(secrets.token_urlsafe(32))"</code>
            </div>
          </>}
          <Button onClick={handleAdd}>Register</Button>
        </div>
      )}

      {/* PUO Phase 3 — platform code rollouts (separate from artifact deployment) */}
      <PlatformRolloutPlans />
    </div>
  );
}

// ── Promote Tab ───────────────────────────────────────────────────────────────

// Searchable multi-select dropdown — scales to hundreds of items
function MultiSelectDropdown({ label, items, selected, onToggle, emptyMsg }: {
  label: string;
  items: Array<{ id: string; name: string }>;
  selected: string[];
  onToggle: (id: string) => void;
  emptyMsg?: string;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  const filtered = items.filter(i => i.name.toLowerCase().includes(search.toLowerCase()));
  const selCount  = selected.length;
  const btnText   = selCount === 0 ? `${label}: All` : `${label}: ${selCount} / ${items.length} selected`;

  const selectAll  = () => filtered.forEach(i => { if (!selected.includes(i.id)) onToggle(i.id); });
  const clearAll   = () => filtered.forEach(i => { if (selected.includes(i.id))  onToggle(i.id); });

  return (
    <div ref={ref} style={{ position: "relative", marginBottom: 8 }}>
      <button type="button" onClick={() => setOpen(v => !v)} style={{
        width: "100%", padding: "8px 10px", border: "1px solid var(--border-default)",
        borderRadius: "var(--radius-sm)", background: "var(--bg-input)", cursor: "pointer",
        textAlign: "left" as const, fontSize: 13, display: "flex", justifyContent: "space-between",
        alignItems: "center", color: selCount > 0 ? "var(--accent)" : "var(--text-secondary)",
        fontWeight: selCount > 0 ? 600 : 400,
      }}>
        <span>{items.length === 0 ? (emptyMsg ?? `${label}: none available`) : btnText}</span>
        {items.length > 0 && <span style={{ fontSize: 10, color: "var(--text-muted)", marginLeft: 8 }}>{open ? "▲" : "▼"}</span>}
      </button>

      {open && items.length > 0 && (
        <div style={{
          position: "absolute", top: "100%", left: 0, right: 0, zIndex: 100,
          background: "var(--bg-panel)", border: "1px solid var(--border-default)",
          borderRadius: "var(--radius-sm)", boxShadow: "0 6px 20px rgba(0,0,0,.18)",
          marginTop: 3, display: "flex", flexDirection: "column" as const, maxHeight: 280,
        }}>
          <div style={{ padding: "6px 8px", borderBottom: "1px solid var(--border-subtle)" }}>
            <input autoFocus value={search} onChange={e => setSearch(e.target.value)}
              placeholder={`Search ${label.toLowerCase()}…`} style={{
                width: "100%", padding: "5px 8px", border: "1px solid var(--border-default)",
                borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)",
                boxSizing: "border-box" as const,
              }} />
          </div>
          <div style={{ padding: "4px 10px", borderBottom: "1px solid var(--border-subtle)", display: "flex", gap: 12, fontSize: 11 }}>
            <button type="button" onClick={selectAll} style={{ color: "var(--accent)", background: "none", border: "none", cursor: "pointer", padding: 0 }}>Select all</button>
            <button type="button" onClick={clearAll}  style={{ color: "var(--text-muted)", background: "none", border: "none", cursor: "pointer", padding: 0 }}>Clear</button>
            <span style={{ marginLeft: "auto", color: "var(--text-muted)" }}>{filtered.length} result{filtered.length !== 1 ? "s" : ""}</span>
          </div>
          <div style={{ overflowY: "auto", flex: 1 }}>
            {filtered.length === 0 && <div style={{ padding: "10px 12px", fontSize: 12, color: "var(--text-muted)" }}>No matches.</div>}
            {filtered.map(item => {
              const on = selected.includes(item.id);
              return (
                <label key={item.id} style={{
                  display: "flex", alignItems: "center", gap: 8, padding: "7px 10px", cursor: "pointer",
                  background: on ? "color-mix(in srgb, var(--accent) 7%, transparent)" : "transparent",
                  borderBottom: "1px solid var(--border-subtle)",
                }}>
                  <input type="checkbox" checked={on} onChange={() => onToggle(item.id)}
                    style={{ accentColor: "var(--accent)", cursor: "pointer" }} />
                  <span style={{ fontSize: 13, color: on ? "var(--accent)" : "var(--text-primary)", fontWeight: on ? 600 : 400 }}>
                    {item.name}
                  </span>
                </label>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

function PromoteTab({ onDone, activeEnv }: { onDone: () => void; activeEnv: string }) {
  const [envs, setEnvs]             = useState<any[]>([]);
  const [users, setUsers]           = useState<any[]>([]);
  const [caseTypes, setCaseTypes]   = useState<any[]>([]);
  const [forms, setForms]           = useState<any[]>([]);
  const [connectors, setConnectors] = useState<any[]>([]);
  const [toEnv, setToEnv]           = useState("");
  const [fromEnv, setFromEnv]       = useState("");
  const [notes, setNotes]           = useState("");
  const [version, setVersion]       = useState("1.0.0");
  const [selCTs, setSelCTs]         = useState<string[]>([]);
  const [selForms, setSelForms]     = useState<string[]>([]);
  const [selConns, setSelConns]     = useState<string[]>([]);
  const [inclSLA, setInclSLA]       = useState(true);
  const [inclRules, setInclRules]   = useState(true);
  const [assignUserId, setAssignUserId] = useState("");
  const [showRaw, setShowRaw]       = useState(false);
  const [rawJson, setRawJson]       = useState('{"case_types":[],"forms":[],"connectors":[],"include_sla":true,"include_rules":true,"version":"1.0.0"}');
  const [risk, setRisk]             = useState<any | null>(null);
  const [loading, setLoading]       = useState(false);
  const [bundlePreview, setBundlePreview] = useState<any | null>(null);
  const [loadingBundle, setLoadingBundle] = useState(false);
  const [msg, setMsg]               = useState<{ text: string; ok: boolean } | null>(null);

  useEffect(() => {
    apiFetch("/environments").then(r => r.ok ? r.json() : []).then((data: any[]) => {
      setEnvs(data);
      // Pre-select the active env as target if not already set
      if (!toEnv && activeEnv) {
        const match = data.find((e: any) => e.name === activeEnv);
        if (match) setToEnv(match.id);
      }
    }).catch(() => {});
    fetch("/api/v1/case-types", { headers: authHdr() })
      .then(r => r.ok ? r.json() : [])
      .then((d: any) => setCaseTypes(Array.isArray(d) ? d : (d.items ?? [])))
      .catch(() => {});
    fetch("/api/v1/forms", { headers: authHdr() })
      .then(r => r.ok ? r.json() : [])
      .then((d: any) => setForms(Array.isArray(d) ? d : (d.items ?? [])))
      .catch(() => {});
    fetch("/api/v1/hxbridge/connectors", { headers: authHdr() })
      .then(r => r.ok ? r.json() : [])
      .then((d: any) => setConnectors(Array.isArray(d) ? d : []))
      .catch(() => {});
    fetch("/api/v1/user-directory?limit=200", { headers: authHdr() })
      .then(r => r.ok ? r.json() : [])
      .then((d: any) => setUsers(Array.isArray(d) ? d : []))
      .catch(() => {});
  }, []);

  const toggle = (setFn: React.Dispatch<React.SetStateAction<string[]>>) => (id: string) =>
    setFn(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);

  const buildManifest = () => {
    if (showRaw) { try { return JSON.parse(rawJson); } catch { return {}; } }
    return { case_types: selCTs, forms: selForms, connectors: selConns, include_sla: inclSLA, include_rules: inclRules, version };
  };
  const syncRaw = () => setRawJson(JSON.stringify(buildManifest(), null, 2));

  const analyseRisk = async () => {
    if (!toEnv) return;
    setLoading(true);
    try {
      const r = await apiFetch("/analyse-risk", { method: "POST", body: JSON.stringify({ to_env_id: toEnv, package_manifest: buildManifest() }) });
      if (r.ok) setRisk(await r.json());
    } finally { setLoading(false); }
  };

  const handlePromote = async () => {
    if (!toEnv) { setMsg({ text: "Select a target environment.", ok: false }); return; }
    setLoading(true); setMsg(null);
    try {
      const assignedUser = users.find(u => u.user_id === assignUserId);
      const body: any = {
        to_env_id: toEnv,
        from_env_id: fromEnv || null,
        package_manifest: buildManifest(),
        deploy_notes: notes,
      };
      if (assignUserId) {
        body.assign_to_user_id = assignUserId;
        body.assign_to_name    = assignedUser?.display_name ?? assignedUser?.email ?? assignUserId;
      }
      const r = await apiFetch("/promote", { method: "POST", body: JSON.stringify(body) });
      if (r.ok) {
        const run = await r.json();
        const assignMsg = assignUserId ? ` Assigned to ${body.assign_to_name} for approval.` : " Go to Deployment Runs to approve.";
        setMsg({ text: `Queued — awaiting approval (risk: ${run.risk_level}).${assignMsg}`, ok: true });
        onDone();
      } else {
        const e = await r.json().catch(() => ({}));
        setMsg({ text: e.detail || "Promotion failed", ok: false });
      }
    } catch (e: any) { setMsg({ text: e.message, ok: false }); }
    setLoading(false);
  };

  const rc = risk ? (RISK_COLOR[risk.risk_level] ?? "#94a3b8") : null;
  const totalSel = selCTs.length + selForms.length + selConns.length;

  return (
    <div style={{ maxWidth: 800 }}>
      <div style={S.card}>
        <div style={{ fontSize: 15, fontWeight: 700, marginBottom: 16 }}>Promote Package</div>

        <div style={S.grid2}>
          <div>
            <span style={S.label}>From Environment (optional)</span>
            <select value={fromEnv} onChange={e => setFromEnv(e.target.value)} style={S.input}>
              <option value="">— Any —</option>
              {envs.map(e => <option key={e.id} value={e.id}>{e.label} ({e.current_version ?? "no version"})</option>)}
            </select>
          </div>
          <div>
            <span style={S.label}>To Environment *</span>
            <select value={toEnv} onChange={e => { setToEnv(e.target.value); setRisk(null); }} style={S.input}>
              <option value="">Select target…</option>
              {envs.map(e => <option key={e.id} value={e.id}>{e.label}</option>)}
            </select>
          </div>
        </div>

        {/* What to promote */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
          <span style={S.label}>
            What to promote
            {totalSel > 0
              ? <span style={{ color: "var(--accent)", marginLeft: 6 }}>({totalSel} items selected)</span>
              : <span style={{ color: "var(--text-muted)", marginLeft: 6, fontWeight: 400 }}> — leave blank to include everything</span>}
          </span>
          <button type="button" onClick={() => { if (!showRaw) syncRaw(); setShowRaw(v => !v); }}
            style={{ fontSize: 11, background: "none", border: "1px solid var(--border-default)", borderRadius: 4, padding: "3px 10px", cursor: "pointer", color: "var(--text-muted)" }}>
            {showRaw ? "← Visual" : "{ } Raw JSON"}
          </button>
        </div>

        {showRaw ? (
          <textarea value={rawJson} onChange={e => setRawJson(e.target.value)} rows={8}
            style={{ ...S.input, fontFamily: "var(--font-mono)", fontSize: 11, resize: "vertical" }} />
        ) : (
          <div style={{ background: "var(--bg-elevated)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", padding: 14, marginBottom: 10 }}>
            {/* Version */}
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
              <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)" }}>Package Version</span>
              <input value={version} onChange={e => setVersion(e.target.value)} placeholder="1.0.0"
                style={{ width: 120, padding: "4px 8px", border: "1px solid var(--border-default)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)" }} />
            </div>

            {/* Artifact selectors */}
            <div style={S.grid2}>
              <div>
                <span style={S.label}>Case Types</span>
                <MultiSelectDropdown
                  label="Case Types"
                  items={caseTypes.map((ct: any) => ({ id: ct.id, name: ct.name }))}
                  selected={selCTs} onToggle={toggle(setSelCTs)}
                  emptyMsg="No case types — all included" />
              </div>
              <div>
                <span style={S.label}>Forms</span>
                <MultiSelectDropdown
                  label="Forms"
                  items={forms.map((f: any) => ({ id: f.id, name: f.name || f.title || f.id }))}
                  selected={selForms} onToggle={toggle(setSelForms)}
                  emptyMsg="No forms — all included" />
              </div>
            </div>

            <div style={S.grid2}>
              <div>
                <span style={S.label}>Connectors / Integrations</span>
                <MultiSelectDropdown
                  label="Connectors"
                  items={connectors.map((c: any) => ({ id: c.id, name: `${c.name} (${c.connector_type ?? c.type ?? "connector"})` }))}
                  selected={selConns} onToggle={toggle(setSelConns)}
                  emptyMsg="No connectors — all included" />
              </div>
              <div>
                <span style={S.label}>Additional Artifacts</span>
                <div style={{ padding: "8px 10px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", background: "var(--bg-input)", display: "flex", flexDirection: "column" as const, gap: 8 }}>
                  <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 13 }}>
                    <input type="checkbox" checked={inclSLA} onChange={e => setInclSLA(e.target.checked)} style={{ accentColor: "var(--accent)" }} />
                    <span style={{ color: "var(--text-primary)" }}>SLA Policies</span>
                  </label>
                  <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 13 }}>
                    <input type="checkbox" checked={inclRules} onChange={e => setInclRules(e.target.checked)} style={{ accentColor: "var(--accent)" }} />
                    <span style={{ color: "var(--text-primary)" }}>Business Rules</span>
                  </label>
                </div>
              </div>
            </div>
          </div>
        )}

        <div style={S.grid2}>
          <div>
            <span style={S.label}>Deployment Notes</span>
            <input value={notes} onChange={e => setNotes(e.target.value)} placeholder="What changed in this release?" style={S.input} />
          </div>
          <div>
            <span style={S.label}>Assign Approver (optional)</span>
            <select value={assignUserId} onChange={e => setAssignUserId(e.target.value)} style={S.input}>
              <option value="">— Anyone can approve —</option>
              {users.map((u: any) => (
                <option key={u.user_id} value={u.user_id}>
                  {u.display_name || u.email || u.user_id}
                  {u.email && u.display_name ? ` (${u.email})` : ""}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div style={{ display: "flex", gap: 10, marginBottom: 16 }}>
          <Button size="sm" disabled={!toEnv || loading} onClick={analyseRisk}>{loading ? "Analysing…" : "Analyse Risk"}</Button>
        </div>

        {risk && rc && (
          <div style={{ padding: "12px 16px", borderRadius: 8, background: rc + "11", border: `1px solid ${rc}44`, marginBottom: 16 }}>
            <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 6 }}>
              <span style={{ ...S.badge, background: rc + "22", color: rc }}>{risk.risk_level} risk</span>
              <span style={{ fontSize: 12, color: "var(--text-muted)" }}>Requires approval before deployment</span>
            </div>
            <div style={{ fontSize: 13, color: "var(--text-primary)", marginBottom: 4 }}>{risk.reason}</div>
            {risk.recommendation && <div style={{ fontSize: 12, color: "var(--text-muted)", fontStyle: "italic" }}>{risk.recommendation}</div>}
          </div>
        )}

        {msg && <div style={{ fontSize: 12, padding: "8px 12px", borderRadius: 4, marginBottom: 12, background: msg.ok ? "#22c55e22" : "#ef444422", color: msg.ok ? "#22c55e" : "#ef4444" }}>{msg.text}</div>}

        {bundlePreview && (
          <div style={{ ...S.card, marginBottom: 12, background: "#6366f111", border: "1px solid #6366f144" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ fontWeight: 700, fontSize: 13, color: "#6366f1" }}>⬆ Bundle Preview — v{bundlePreview.bundle_schema_version}</span>
                {bundlePreview.is_delta && (
                  <span style={{ fontSize: 10, fontWeight: 700, padding: "2px 7px", borderRadius: 10, background: "#8b5cf622", color: "#8b5cf6", letterSpacing: 0.5 }}>
                    DELTA — changed since {bundlePreview.delta_since ? new Date(bundlePreview.delta_since).toLocaleDateString() : "last push"}
                  </span>
                )}
                {!bundlePreview.is_delta && (
                  <span style={{ fontSize: 10, fontWeight: 700, padding: "2px 7px", borderRadius: 10, background: "#6366f122", color: "#6366f1", letterSpacing: 0.5 }}>FULL</span>
                )}
              </div>
              <button style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 11 }} onClick={() => setBundlePreview(null)}>✕</button>
            </div>
            {/* Summary grid — skip the needs_configuration count row, show it separately */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(6, 1fr)", gap: 6, marginBottom: 10 }}>
              {Object.entries(bundlePreview.summary || {})
                .filter(([k]) => k !== "needs_configuration")
                .map(([k, v]) => (
                  <div key={k} style={{ textAlign: "center" as const, padding: "6px 4px", background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)" }}>
                    <div style={{ fontSize: 16, fontWeight: 700, color: Number(v) > 0 ? "var(--text-primary)" : "var(--text-muted)" }}>{String(v)}</div>
                    <div style={{ fontSize: 9, color: "var(--text-muted)", textTransform: "capitalize" as const, lineHeight: 1.3 }}>{k.replace(/_/g, " ")}</div>
                  </div>
                ))}
            </div>
            {/* Needs configuration warning */}
            {(bundlePreview.needs_configuration?.length ?? 0) > 0 && (
              <div style={{ background: "#f59e0b11", border: "1px solid #f59e0b44", borderRadius: "var(--radius-sm)", padding: "8px 12px", marginBottom: 8 }}>
                <div style={{ fontWeight: 700, fontSize: 12, color: "#f59e0b", marginBottom: 6 }}>
                  ⚠ {bundlePreview.needs_configuration.length} item(s) need credentials configured on the target after import
                </div>
                {bundlePreview.needs_configuration.map((item: any, i: number) => (
                  <div key={i} style={{ fontSize: 11, color: "var(--text-secondary)", paddingLeft: 8 }}>
                    <strong>{item.type}</strong>: {item.name} — missing: <code style={{ fontFamily: "var(--font-mono)", fontSize: 10 }}>{item.missing?.join(", ")}</code>
                  </div>
                ))}
              </div>
            )}
            <div style={{ fontSize: 10, color: "var(--text-muted)" }}>Built at: {bundlePreview.created_at}</div>
          </div>
        )}

        <div style={{ display: "flex", gap: 8 }}>
          <Button disabled={loading || !toEnv} onClick={handlePromote}>Submit for Approval</Button>
          <Button variant="secondary" disabled={loadingBundle} onClick={async () => {
            setLoadingBundle(true);
            // Use delta bundle if env is selected (only include changed artifacts since last push)
            const selectedEnv = envs.find((e: any) => e.id === toEnv);
            let r;
            if (toEnv && selectedEnv?.last_deployed_at) {
              r = await apiFetch(`/package/delta?env_id=${toEnv}`, { method: "POST", body: JSON.stringify({ case_type_ids: selCTs.length ? selCTs : null }) });
            } else {
              r = await apiFetch("/package", { method: "POST", body: JSON.stringify({ case_type_ids: selCTs.length ? selCTs : null }) });
            }
            if (r.ok) setBundlePreview(await r.json());
            setLoadingBundle(false);
          }}>{loadingBundle ? "Building…" : toEnv && envs.find((e: any) => e.id === toEnv)?.last_deployed_at ? "Preview Delta" : "Preview Bundle"}</Button>
        </div>
      </div>

      <div style={{ ...S.card, background: "var(--accent-dim)", border: "1px solid var(--accent)" }}>
        <div style={{ fontSize: 12, fontWeight: 700, color: "var(--accent)", marginBottom: 6 }}>Approval flow</div>
        <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.8 }}>
          Every promotion creates a run with status <code style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>awaiting_approval</code> — no auto-deploys.<br />
          If you assign an approver, the run appears in their <b>My Approvals</b> queue in Deployment Runs.<br />
          The approver selects the run and clicks <b>Approve</b> or <b>Reject</b>.
        </div>
      </div>
    </div>
  );
}

// ── Runs Tab ──────────────────────────────────────────────────────────────────

function RunsTab({ activeEnv }: { activeEnv: string }) {
  const { user }                  = useAuth();
  const [runs, setRuns]           = useState<any[]>([]);
  const [envs, setEnvs]           = useState<any[]>([]);
  const [selected, setSel]        = useState<any | null>(null);
  const [statusFilter, setStatus] = useState("");
  const [loading, setLoading]     = useState(false);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [rejectReason, setRejectReason] = useState("");
  const [deleteId, setDeleteId]   = useState<string | null>(null);

  // Edit state
  const [editingRun, setEditingRun]   = useState(false);
  const [editNotes, setEditNotes]     = useState("");
  const [editEnvId, setEditEnvId]     = useState("");
  const [editRisk, setEditRisk]       = useState<any | null>(null);
  const [editLoading, setEditLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    const qs = statusFilter ? `?status=${statusFilter}` : "";
    const r = await apiFetch(`/runs${qs}`);
    if (r.ok) setRuns(await r.json());
    setLoading(false);
  }, [statusFilter]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    apiFetch("/environments").then(r => r.ok ? r.json() : []).then(setEnvs).catch(() => {});
  }, []);

  const openEdit = (run: any) => {
    setEditingRun(true);
    setEditNotes(run.deploy_notes || "");
    setEditEnvId(run.to_env_id || "");
    setEditRisk(null);
    setActionMsg(null);
  };

  const reanalyseRisk = async () => {
    if (!selected) return;
    setEditLoading(true);
    try {
      const manifest = { case_types: (selected.risk_summary?.affected_items ?? []) };
      const r = await apiFetch("/analyse-risk", {
        method: "POST",
        body: JSON.stringify({ to_env_id: editEnvId || selected.to_env_id, package_manifest: manifest }),
      });
      if (r.ok) setEditRisk(await r.json());
      else setActionMsg("Risk analysis failed.");
    } finally { setEditLoading(false); }
  };

  const saveRun = async () => {
    if (!selected) return;
    setEditLoading(true);
    try {
      const body: any = { deploy_notes: editNotes };
      if (editEnvId && editEnvId !== selected.to_env_id) body.to_env_id = editEnvId;
      const r = await apiFetch(`/runs/${selected.id}`, { method: "PATCH", body: JSON.stringify(body) });
      if (r.ok) {
        const u = await r.json();
        setSel(u);
        setRuns(rs => rs.map(x => x.id === u.id ? u : x));
        setEditingRun(false);
        setActionMsg("Run updated.");
      } else {
        const e = await r.json().catch(() => ({}));
        setActionMsg(e.detail || "Save failed.");
      }
    } finally { setEditLoading(false); }
  };

  const approve = async (id: string) => {
    const r = await apiFetch(`/runs/${id}/approve`, { method: "POST", body: "{}" });
    if (r.ok) { const u = await r.json(); setSel(u); setRuns(rs => rs.map(x => x.id === id ? u : x)); setActionMsg("✓ Approved and deployed."); }
    else setActionMsg("Failed to approve.");
  };

  const reject = async (id: string) => {
    if (!rejectReason.trim()) { setActionMsg("Enter a rejection reason first."); return; }
    const r = await apiFetch(`/runs/${id}/reject`, { method: "POST", body: JSON.stringify({ reason: rejectReason }) });
    if (r.ok) { const u = await r.json(); setSel(u); setRuns(rs => rs.map(x => x.id === id ? u : x)); setActionMsg("Run rejected."); }
    else setActionMsg("Failed to reject.");
  };

  const healthCheck = async (id: string) => {
    const r = await apiFetch(`/runs/${id}/health-check`, { method: "POST" });
    if (r.ok) { const h = await r.json(); setActionMsg(`Health: ${h.healthy ? "✓ Healthy" : "✗ Unhealthy"} (HTTP ${h.status_code}, ${h.response_ms}ms)`); }
    else setActionMsg("Health check failed.");
  };

  const handleDelete = async () => {
    if (!deleteId) return;
    const r = await apiFetch(`/runs/${deleteId}`, { method: "DELETE" });
    if (r.ok || r.status === 204) {
      setRuns(rs => rs.filter(x => x.id !== deleteId));
      if (selected?.id === deleteId) setSel(null);
      setActionMsg("Run deleted.");
    } else setActionMsg("Failed to delete.");
    setDeleteId(null);
  };

  const STATUSES = ["pending", "awaiting_approval", "approved", "deploying", "deployed", "rejected", "failed", "rolled_back"];
  const canEdit  = (run: any) => run && ["pending", "awaiting_approval"].includes(run.status);

  // client-side "My Approvals" filter
  // user-directory uses username as user_id; JWT stores UUID — match both
  const myIds = new Set([user?.user_id, user?.username, user?.email].filter(Boolean) as string[]);
  const myApprovalsFilter = statusFilter === "my_approvals";
  const activeEnvId = activeEnv ? envs.find(e => e.name === activeEnv)?.id : null;
  const visibleRuns = (myApprovalsFilter
    ? runs.filter(r => r.status === "awaiting_approval" && myIds.has(r.risk_summary?.assigned_to_user_id))
    : runs
  ).filter(r => !activeEnvId || r.to_env_id === activeEnvId);

  return (
    <div>
      {deleteId && <Confirm msg="Delete this deployment run? This cannot be undone." onYes={handleDelete} onNo={() => setDeleteId(null)} />}

      {runs.some(r => r.status === "awaiting_approval") && (
        <div style={{ padding: "10px 14px", borderRadius: "var(--radius-sm)", background: "#f59e0b22", border: "1px solid #f59e0b44", marginBottom: 16, fontSize: 12, color: "#f59e0b" }}>
          ⚠ {runs.filter(r => r.status === "awaiting_approval").length} run(s) awaiting approval — select a run on the left, then click <b>Approve &amp; Deploy</b> in the detail pane.
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "320px 1fr", gap: 20 }}>
        {/* Left: run list */}
        <div style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)", overflow: "hidden" }}>
          <div style={{ padding: "10px 14px", background: "var(--bg-elevated)", fontWeight: 700, fontSize: 12, borderBottom: "1px solid var(--border-subtle)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span>Deployment Runs {loading && "…"}</span>
            <button onClick={load} style={{ fontSize: 11, color: "var(--accent)", background: "none", border: "none", cursor: "pointer" }}>↻</button>
          </div>
          <div style={{ display: "flex", gap: 0, padding: "4px 8px", borderBottom: "1px solid var(--border-subtle)", flexWrap: "wrap" as const }}>
            {/* My Approvals quick-filter */}
            {(() => {
              const myCount = runs.filter(r => r.status === "awaiting_approval" && myIds.has(r.risk_summary?.assigned_to_user_id)).length;
              return (
                <button onClick={() => setStatus("my_approvals")}
                  style={{ fontSize: 10, padding: "3px 6px", background: "none", border: "none", cursor: "pointer",
                    fontWeight: myApprovalsFilter ? 700 : 400,
                    color: myApprovalsFilter ? "#f59e0b" : "var(--text-muted)",
                    borderBottom: myApprovalsFilter ? "2px solid #f59e0b" : "2px solid transparent" }}>
                  My Approvals{myCount > 0 ? ` (${myCount})` : ""}
                </button>
              );
            })()}
            <span style={{ borderLeft: "1px solid var(--border-subtle)", margin: "4px 4px" }} />
            {["", ...STATUSES].map(s => {
              const count = s ? runs.filter(r => r.status === s).length : runs.length;
              return (
                <button key={s} onClick={() => setStatus(s)}
                  style={{ fontSize: 10, padding: "3px 6px", background: "none", border: "none", cursor: "pointer",
                    fontWeight: !myApprovalsFilter && statusFilter === s ? 700 : 400,
                    color: !myApprovalsFilter && statusFilter === s ? "var(--accent)" : "var(--text-muted)",
                    borderBottom: !myApprovalsFilter && statusFilter === s ? "2px solid var(--accent)" : "2px solid transparent" }}>
                  {s || "All"}{count > 0 ? ` (${count})` : ""}
                </button>
              );
            })}
          </div>
          <div style={{ overflowY: "auto" as const, maxHeight: 500 }}>
            {visibleRuns.length === 0 && <div style={{ padding: 16, fontSize: 12, color: "var(--text-muted)" }}>No runs found.</div>}
            {visibleRuns.map(run => {
              const sc  = STATUS_COLOR[run.status] ?? "#94a3b8";
              const rc2 = RISK_COLOR[run.risk_level] ?? "#94a3b8";
              return (
                <div key={run.id} onClick={() => { setSel(run); setActionMsg(null); setRejectReason(""); setEditingRun(false); }}
                  style={{ padding: "10px 14px", borderBottom: "1px solid var(--border-subtle)", cursor: "pointer",
                    background: selected?.id === run.id ? "color-mix(in srgb, var(--accent) 8%, transparent)" : "transparent",
                    borderLeft: run.status === "awaiting_approval" ? "3px solid #f59e0b" : "3px solid transparent" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                    <span style={{ ...S.badge, background: rc2 + "22", color: rc2 }}>{run.risk_level}</span>
                    <span style={{ ...S.badge, background: sc + "22", color: sc }}>{run.status}</span>
                  </div>
                  <div style={{ fontSize: 11, color: "var(--text-muted)" }}>by {run.initiated_by}</div>
                  <div style={{ fontSize: 10, color: "var(--text-muted)" }}>{new Date(run.created_at).toLocaleString()}</div>
                </div>
              );
            })}
          </div>
        </div>

        {/* Right: detail */}
        <div>
          {!selected ? (
            <div style={{ ...S.card, color: "var(--text-muted)", fontSize: 13 }}>Select a run to view details and take action.</div>
          ) : (
            <div style={S.card}>
              {/* Header row */}
              <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 16, justifyContent: "space-between" }}>
                <div style={{ display: "flex", gap: 8 }}>
                  <span style={{ ...S.badge, background: (RISK_COLOR[selected.risk_level] ?? "#94a3b8") + "22", color: RISK_COLOR[selected.risk_level] ?? "#94a3b8" }}>{selected.risk_level} risk</span>
                  <span style={{ ...S.badge, background: (STATUS_COLOR[selected.status] ?? "#94a3b8") + "22", color: STATUS_COLOR[selected.status] ?? "#94a3b8" }}>{selected.status}</span>
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  {canEdit(selected) && !editingRun && (
                    <Button size="sm" variant="secondary" onClick={() => openEdit(selected)}>Edit</Button>
                  )}
                  <Button size="sm" variant="danger" onClick={() => setDeleteId(selected.id)}>Delete</Button>
                </div>
              </div>

              {/* Inline edit form */}
              {editingRun && canEdit(selected) && (
                <div style={{ marginBottom: 16, padding: 14, background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)" }}>
                  <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 12, color: "var(--text-secondary)" }}>Edit Run</div>

                  <span style={S.label}>Deployment Notes</span>
                  <textarea value={editNotes} onChange={e => setEditNotes(e.target.value)} rows={3}
                    style={{ ...S.input, fontFamily: "var(--font-mono)", fontSize: 12, resize: "vertical" }} />

                  <span style={S.label}>Target Environment</span>
                  <select value={editEnvId} onChange={e => { setEditEnvId(e.target.value); setEditRisk(null); }}
                    style={S.input}>
                    <option value="">— unchanged —</option>
                    {envs.map(e => <option key={e.id} value={e.id}>{e.label} ({e.name})</option>)}
                  </select>

                  {editRisk && (() => {
                    const rc = RISK_COLOR[editRisk.risk_level] ?? "#94a3b8";
                    return (
                      <div style={{ padding: "10px 14px", borderRadius: 6, background: rc + "11", border: `1px solid ${rc}44`, marginBottom: 10 }}>
                        <span style={{ ...S.badge, background: rc + "22", color: rc }}>{editRisk.risk_level} risk</span>
                        <div style={{ fontSize: 12, color: "var(--text-primary)", marginTop: 6 }}>{editRisk.reason}</div>
                        {editRisk.recommendation && <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 3, fontStyle: "italic" }}>{editRisk.recommendation}</div>}
                      </div>
                    );
                  })()}

                  <div style={{ display: "flex", gap: 8 }}>
                    <Button size="sm" disabled={editLoading} onClick={saveRun}>{editLoading ? "Saving…" : "Save"}</Button>
                    <Button size="sm" variant="secondary" disabled={editLoading} onClick={reanalyseRisk}>{editLoading ? "…" : "Re-analyse Risk"}</Button>
                    <Button size="sm" variant="secondary" onClick={() => { setEditingRun(false); setEditRisk(null); }}>Cancel</Button>
                  </div>
                </div>
              )}

              {selected.risk_summary?.reason && !editingRun && (
                <div style={{ fontSize: 13, color: "var(--text-primary)", marginBottom: 12, padding: "10px 14px", background: "var(--bg-elevated)", borderRadius: 6 }}>
                  {selected.risk_summary.reason}
                  {selected.risk_summary.recommendation && <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 4, fontStyle: "italic" }}>{selected.risk_summary.recommendation}</div>}
                </div>
              )}

              <div style={S.grid2}>
                <div><span style={S.label}>Initiated by</span><div style={S.value}>{selected.initiated_by}</div></div>
                <div><span style={S.label}>Created</span><div style={S.value}>{new Date(selected.created_at).toLocaleString()}</div></div>
                {selected.to_env_id && envs.length > 0 && (
                  <div><span style={S.label}>Target Environment</span><div style={S.value}>{envs.find(e => e.id === selected.to_env_id)?.label ?? selected.to_env_id}</div></div>
                )}
                {selected.risk_summary?.assigned_to_name && (
                  <div>
                    <span style={S.label}>Assigned Approver</span>
                    <div style={{ ...S.value, display: "flex", alignItems: "center", gap: 6 }}>
                      <span style={{ ...S.badge, background: "#f59e0b22", color: "#f59e0b" }}>assigned</span>
                      {selected.risk_summary.assigned_to_name}
                    </div>
                  </div>
                )}
                {selected.approved_by && <div><span style={S.label}>Approved by</span><div style={S.value}>{selected.approved_by}</div></div>}
                {selected.rejected_by && <div><span style={S.label}>Rejected by</span><div style={{ ...S.value, color: "#ef4444" }}>{selected.rejected_by} — {selected.rejection_reason}</div></div>}
                {selected.deployed_at && <div><span style={S.label}>Deployed at</span><div style={S.value}>{new Date(selected.deployed_at).toLocaleString()}</div></div>}
              </div>
              {selected.deploy_notes && !editingRun && (
                <><span style={S.label}>Notes</span><div style={{ ...S.value, fontStyle: "italic" }}>{selected.deploy_notes}</div></>
              )}

              {actionMsg && <div style={{ fontSize: 12, padding: "8px 12px", borderRadius: 4, marginBottom: 12, background: "var(--bg-elevated)", color: "var(--text-primary)" }}>{actionMsg}</div>}

              {selected.status === "triggered" && (
                <div style={{ marginTop: 8, padding: 14, background: "#8b5cf611", borderRadius: "var(--radius-sm)", border: "1px solid #8b5cf644" }}>
                  <div style={{ fontSize: 13, color: "#8b5cf6", fontWeight: 600, marginBottom: 4 }}>🔗 Webhook triggered — awaiting CI/CD callback</div>
                  <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                    The external CI/CD pipeline has been notified. It will call back to <code style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>POST /api/v1/deploy/runs/{selected.id}/callback</code> when done.
                  </div>
                </div>
              )}

              {selected.status === "awaiting_approval" && !editingRun && (
                <div style={{ marginTop: 8, padding: 14, background: "#f59e0b11", borderRadius: "var(--radius-sm)", border: "1px solid #f59e0b44" }}>
                  <div style={{ fontSize: 12, color: "#f59e0b", fontWeight: 600, marginBottom: 10 }}>
                    Awaiting approval
                    {selected.risk_summary?.assigned_to_name
                      ? ` — assigned to ${selected.risk_summary.assigned_to_name}`
                      : ""}
                  </div>
                  {/* Reject reason input */}
                  <input value={rejectReason} onChange={e => setRejectReason(e.target.value)}
                    placeholder="Rejection reason (required to reject)…"
                    style={{ ...S.input, marginBottom: 10 }} />
                  {/* Consistent side-by-side action buttons */}
                  <div style={{ display: "flex", gap: 8 }}>
                    <Button onClick={() => approve(selected.id)}>Approve</Button>
                    <Button variant="danger" onClick={() => reject(selected.id)}>Reject</Button>
                  </div>
                </div>
              )}

              {selected.status === "deployed" && (
                <Button size="sm" variant="secondary" onClick={() => healthCheck(selected.id)}>Health Check</Button>
              )}
              {/* Post-push checklist */}
              {selected.status === "deployed" && (selected.risk_summary?.push_result?.needs_configuration?.length ?? 0) > 0 && (
                <div style={{ marginTop: 10, padding: 12, background: "#f59e0b11", border: "1px solid #f59e0b44", borderRadius: "var(--radius-sm)" }}>
                  <div style={{ fontWeight: 700, fontSize: 12, color: "#f59e0b", marginBottom: 6 }}>
                    ⚠ Post-Import Checklist — {selected.risk_summary.push_result.needs_configuration.length} item(s) need credentials on target
                  </div>
                  {selected.risk_summary.push_result.needs_configuration.map((item: any, i: number) => (
                    <div key={i} style={{ fontSize: 11, color: "var(--text-secondary)", paddingLeft: 8, marginBottom: 2 }}>
                      <strong>{item.type}</strong>: {item.name} — set: <code style={{ fontFamily: "var(--font-mono)", fontSize: 10, background: "var(--bg-subtle)", padding: "1px 3px", borderRadius: 2 }}>{item.missing?.join(", ")}</code>
                    </div>
                  ))}
                  {selected.risk_summary.push_result.imported !== undefined && (
                    <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 6 }}>
                      Imported: {selected.risk_summary.push_result.imported} · Skipped: {selected.risk_summary.push_result.skipped}
                      {selected.risk_summary.push_result.errors?.length > 0 && <span style={{ color: "#ef4444" }}> · Errors: {selected.risk_summary.push_result.errors.length}</span>}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Change Windows Tab ────────────────────────────────────────────────────────

function formatHourInTz(utcHour: number, tz: string): string {
  try {
    const d = new Date("2024-01-15T00:00:00Z");
    d.setUTCHours(utcHour, 0, 0, 0);
    return d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: true, timeZone: tz });
  } catch {
    return `${String(utcHour).padStart(2, "0")}:00`;
  }
}

function WindowsTab({ activeEnv }: { activeEnv: string }) {
  const [envs, setEnvs]         = useState<any[]>([]);
  const [calendars, setCalendars] = useState<any[]>([]);
  const [displayTz, setDisplayTz] = useState("UTC");
  const [windows, setWindows]   = useState<any[]>([]);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editForm, setEditForm]   = useState<any>({});
  const [deleteId, setDeleteId]   = useState<string | null>(null);
  const [form, setForm]           = useState({
    env_id: "", name: "Production Window",
    start_hour_utc: 2, end_hour_utc: 4,
    days_of_week: [0, 1, 2, 3, 4], enabled: true,
  });
  const [msg, setMsg]             = useState<string | null>(null);

  const load = useCallback(async () => {
    const [e, w, c] = await Promise.all([
      apiFetch("/environments").then(r => r.ok ? r.json() : []).catch(() => []),
      apiFetch("/windows").then(r => r.ok ? r.json() : []).catch(() => []),
      fetch("/api/v1/admin/calendars", { headers: authHdr() }).then(r => r.ok ? r.json() : []).catch(() => []),
    ]);
    setEnvs(Array.isArray(e) ? e : []);
    setWindows(Array.isArray(w) ? w : []);
    const cals = Array.isArray(c) ? c : [];
    setCalendars(cals);
    if (cals.length > 0 && displayTz === "UTC") setDisplayTz(cals[0].timezone || "UTC");
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { load(); }, [load]);

  const handleAdd = async () => {
    if (!form.env_id) { setMsg("Select an environment."); return; }
    const r = await apiFetch("/windows", { method: "POST", body: JSON.stringify(form) });
    if (r.ok) { await load(); setMsg("Change window created."); } else setMsg("Failed to create.");
  };

  const handleEdit = async (id: string) => {
    const r = await apiFetch(`/windows/${id}`, { method: "PATCH", body: JSON.stringify(editForm) });
    if (r.ok) { await load(); setEditingId(null); setMsg("Window updated."); }
    else setMsg("Failed to update.");
  };

  const handleDelete = async () => {
    if (!deleteId) return;
    const r = await apiFetch(`/windows/${deleteId}`, { method: "DELETE" });
    if (r.ok || r.status === 204) { await load(); setMsg("Window deleted."); }
    else setMsg("Failed to delete.");
    setDeleteId(null);
  };

  const toggleDay = (f: any, setFn: any, i: number) =>
    setFn((prev: any) => ({ ...prev, days_of_week: prev.days_of_week.includes(i) ? prev.days_of_week.filter((d: number) => d !== i) : [...prev.days_of_week, i].sort() }));

  const envMap = Object.fromEntries(envs.map(e => [e.id, e.label]));
  const activeEnvId = activeEnv ? envs.find(e => e.name === activeEnv)?.id : null;
  const visibleWindows = windows.filter(w => !activeEnvId || w.env_id === activeEnvId);
  const isCurrentlyActive = (w: any): boolean => {
    if (!w.enabled) return false;
    const now = new Date();
    const utcDay  = now.getUTCDay(); // 0=Sun, but our array is Mon=0
    const helixDay = utcDay === 0 ? 6 : utcDay - 1;
    const utcHour = now.getUTCHours();
    return (w.days ?? []).includes(helixDay) && utcHour >= w.start && utcHour < w.end;
  };

  const HOURS = Array.from({ length: 24 }, (_, i) => i);

  const HourSelect = ({ value, onChange }: { value: number; onChange: (v: number) => void }) => (
    <select value={value} onChange={e => onChange(+e.target.value)} style={S.input}>
      {HOURS.map(h => (
        <option key={h} value={h}>
          {String(h).padStart(2, "0")}:00 UTC
          {displayTz !== "UTC" ? ` (${formatHourInTz(h, displayTz)})` : ""}
        </option>
      ))}
    </select>
  );

  return (
    <div>
      {deleteId && <Confirm msg="Delete this change window?" onYes={handleDelete} onNo={() => setDeleteId(null)} />}

      {/* Timezone selector */}
      {calendars.length > 0 && (
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16, padding: "10px 14px", background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)" }}>
          <span style={{ fontSize: 12, color: "var(--text-secondary)", fontWeight: 600 }}>Display times in:</span>
          <select value={displayTz} onChange={e => setDisplayTz(e.target.value)}
            style={{ fontSize: 12, padding: "4px 8px", border: "1px solid var(--border-default)", borderRadius: 4, background: "var(--bg-input)", color: "var(--text-primary)" }}>
            <option value="UTC">UTC</option>
            {calendars.map((c: any) => (
              <option key={c.id} value={c.timezone}>{c.name} ({c.timezone})</option>
            ))}
          </select>
          {displayTz !== "UTC" && (
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>Times stored in UTC — shown here in {displayTz}</span>
          )}
        </div>
      )}

      <div style={S.grid2}>
        {/* Create form */}
        <div style={S.card}>
          <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 14 }}>New Change Window</div>

          <span style={S.label}>Environment</span>
          <select value={form.env_id} onChange={e => setForm(f => ({ ...f, env_id: e.target.value }))} style={S.input}>
            <option value="">Select…</option>
            {envs.map((e: any) => <option key={e.id} value={e.id}>{e.label}</option>)}
          </select>

          <span style={S.label}>Window Name</span>
          <input value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} style={S.input} />

          <span style={S.label}>Allowed Days</span>
          <div style={{ display: "flex", gap: 6, marginBottom: 10, flexWrap: "wrap" as const }}>
            {DAYS_LABELS.map((d, i) => (
              <label key={d} style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, cursor: "pointer",
                padding: "3px 10px", borderRadius: 20, border: "1px solid",
                borderColor: form.days_of_week.includes(i) ? "var(--accent)" : "var(--border-default)",
                background: form.days_of_week.includes(i) ? "var(--accent-dim)" : "transparent",
                color: form.days_of_week.includes(i) ? "var(--accent)" : "var(--text-secondary)" }}>
                <input type="checkbox" checked={form.days_of_week.includes(i)} onChange={() => toggleDay(form, setForm, i)} style={{ display: "none" }} />
                {d}
              </label>
            ))}
          </div>

          <div style={S.grid2}>
            <div>
              <span style={S.label}>Start Hour (UTC)</span>
              <HourSelect value={form.start_hour_utc} onChange={v => setForm(f => ({ ...f, start_hour_utc: v }))} />
            </div>
            <div>
              <span style={S.label}>End Hour (UTC)</span>
              <HourSelect value={form.end_hour_utc} onChange={v => setForm(f => ({ ...f, end_hour_utc: v }))} />
            </div>
          </div>

          <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", marginBottom: 14 }}>
            <input type="checkbox" checked={form.enabled} onChange={e => setForm(f => ({ ...f, enabled: e.target.checked }))} />
            <span style={{ fontSize: 13, color: "var(--text-primary)" }}>Active (enforces window)</span>
          </label>

          {msg && <div style={{ fontSize: 12, color: msg.startsWith("Failed") ? "#ef4444" : "#22c55e", marginBottom: 8 }}>{msg}</div>}
          <Button onClick={handleAdd}>Add Window</Button>
        </div>

        {/* Window list */}
        <div style={S.card}>
          <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 12 }}>Configured Windows ({visibleWindows.length})</div>
          {visibleWindows.length === 0 && <div style={{ fontSize: 12, color: "var(--text-muted)" }}>No change windows configured. All hours are open by default.</div>}
          {visibleWindows.map((w: any) => {
            const active = isCurrentlyActive(w);
            return (
              <div key={w.id} style={{ padding: "10px 12px", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)", marginBottom: 8, background: "var(--bg-elevated)", opacity: w.enabled === false ? 0.6 : 1 }}>
                {editingId === w.id ? (
                  <div>
                    <input value={editForm.name} onChange={e => setEditForm((f: any) => ({ ...f, name: e.target.value }))} style={S.input} placeholder="Window name" />
                    <div style={{ display: "flex", gap: 6, marginBottom: 8, flexWrap: "wrap" as const }}>
                      {DAYS_LABELS.map((d, i) => (
                        <label key={d} style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, cursor: "pointer",
                          padding: "2px 8px", borderRadius: 20, border: "1px solid",
                          borderColor: (editForm.days_of_week || []).includes(i) ? "var(--accent)" : "var(--border-default)",
                          background: (editForm.days_of_week || []).includes(i) ? "var(--accent-dim)" : "transparent",
                          color: (editForm.days_of_week || []).includes(i) ? "var(--accent)" : "var(--text-secondary)" }}>
                          <input type="checkbox" checked={(editForm.days_of_week || []).includes(i)}
                            onChange={() => toggleDay(editForm, setEditForm, i)} style={{ display: "none" }} />
                          {d}
                        </label>
                      ))}
                    </div>
                    <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
                      <div style={{ flex: 1 }}>
                        <span style={{ ...S.label, marginBottom: 2 }}>Start Hour</span>
                        <HourSelect value={editForm.start_hour_utc} onChange={v => setEditForm((f: any) => ({ ...f, start_hour_utc: v }))} />
                      </div>
                      <div style={{ flex: 1 }}>
                        <span style={{ ...S.label, marginBottom: 2 }}>End Hour</span>
                        <HourSelect value={editForm.end_hour_utc} onChange={v => setEditForm((f: any) => ({ ...f, end_hour_utc: v }))} />
                      </div>
                    </div>
                    <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", marginBottom: 10, fontSize: 13 }}>
                      <input type="checkbox" checked={editForm.enabled ?? true}
                        onChange={e => setEditForm((f: any) => ({ ...f, enabled: e.target.checked }))} />
                      <span style={{ color: "var(--text-primary)" }}>Active</span>
                    </label>
                    <div style={{ display: "flex", gap: 6 }}>
                      <Button size="sm" onClick={() => handleEdit(w.id)}>Save</Button>
                      <Button size="sm" variant="secondary" onClick={() => setEditingId(null)}>Cancel</Button>
                    </div>
                  </div>
                ) : (
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                    <div>
                      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 3 }}>
                        <span style={{ fontWeight: 600, fontSize: 13 }}>{w.name}</span>
                        {w.enabled === false
                          ? <span style={{ ...S.badge, background: "#94a3b822", color: "#94a3b8" }}>disabled</span>
                          : active
                            ? <span style={{ ...S.badge, background: "#22c55e22", color: "#22c55e" }}>active now</span>
                            : <span style={{ ...S.badge, background: "#3b82f622", color: "#3b82f6" }}>enabled</span>
                        }
                      </div>
                      <div style={{ fontSize: 12, color: "var(--text-muted)" }}>{envMap[w.env_id] ?? w.env_id}</div>
                      <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
                        {DAYS_LABELS.filter((_, i) => (w.days ?? []).includes(i)).join(", ")}
                      </div>
                      <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 2, fontFamily: "var(--font-mono)" }}>
                        {String(w.start).padStart(2, "0")}:00 – {String(w.end).padStart(2, "0")}:00 UTC
                        {displayTz !== "UTC" && (
                          <span style={{ color: "var(--text-muted)", fontWeight: 400 }}>
                            {" "}({formatHourInTz(w.start, displayTz)} – {formatHourInTz(w.end, displayTz)} {displayTz.split("/").pop()})
                          </span>
                        )}
                      </div>
                    </div>
                    <div style={{ display: "flex", gap: 6 }}>
                      <Button size="sm" variant="secondary" onClick={() => {
                        setEditingId(w.id);
                        setEditForm({ name: w.name, days_of_week: w.days ?? [], start_hour_utc: w.start, end_hour_utc: w.end, env_id: w.env_id, enabled: w.enabled !== false });
                      }}>Edit</Button>
                      <Button size="sm" variant="danger" onClick={() => setDeleteId(w.id)}>Delete</Button>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ── Packages Tab (from App Registry) ─────────────────────────────────────────

const APPS_API = "/api/v1/apps";

type Pkg = {
  id: string; name: string; version: string;
  description: string | null; status: string;
  created_by: string | null; created_at: string | null;
};

const PKG_STATUS_COLOR: Record<string, string> = {
  draft: "#f59e0b", published: "#22c55e", deprecated: "#94a3b8",
};

function PackagesTab() {
  const [packages, setPackages]   = useState<Pkg[]>([]);
  const [selected, setSelected]   = useState<Pkg | null>(null);
  const [detail, setDetail]       = useState<any>(null);
  const [loading, setLoading]     = useState(false);
  const [creating, setCreating]   = useState(false);
  const [form, setForm]           = useState({ name: "", version: "", description: "" });
  const [err, setErr]             = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await fetch(`${APPS_API}/packages`, { headers: authHdr() });
    if (r.ok) setPackages((await r.json()).packages);
  }, []);

  useEffect(() => { load(); }, [load]);

  async function loadDetail(pkg: Pkg) {
    setSelected(pkg); setDetail(null);
    const r = await fetch(`${APPS_API}/packages/${pkg.id}`, { headers: authHdr() });
    if (r.ok) setDetail(await r.json());
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault(); setLoading(true); setErr(null);
    try {
      const r = await fetch(`${APPS_API}/package`, {
        method: "POST",
        headers: { ...authHdr(), "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      if (!r.ok) { setErr((await r.json()).detail || "Error"); return; }
      setForm({ name: "", version: "", description: "" });
      setCreating(false);
      await load();
    } finally { setLoading(false); }
  }

  async function handleStatusChange(pkg: Pkg, status: string) {
    await fetch(`${APPS_API}/packages/${pkg.id}/status`, {
      method: "PATCH",
      headers: { ...authHdr(), "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    });
    await load();
    if (selected?.id === pkg.id) loadDetail({ ...pkg, status });
  }

  async function handleDownload(pkg: Pkg) {
    const r = await fetch(`${APPS_API}/packages/${pkg.id}/download`, { headers: authHdr() });
    if (!r.ok) return;
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${pkg.name.replace(/ /g, "_")}_v${pkg.version}.zip`;
    a.click();
  }

  const pkgInputStyle: React.CSSProperties = { width: "100%", padding: "7px 10px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", fontSize: 13, boxSizing: "border-box", background: "var(--bg-input)", color: "var(--text-primary)", marginBottom: 8 };

  return (
    <div style={{ display: "flex", flex: 1, overflow: "hidden" }}>
      {/* Sidebar */}
      <div style={{ width: 260, borderRight: "1px solid var(--border-subtle)", display: "flex", flexDirection: "column", background: "var(--bg-card)", flexShrink: 0 }}>
        <div style={{ padding: "12px 14px", borderBottom: "1px solid var(--border-subtle)", display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ flex: 1, fontSize: 11, fontWeight: 700, color: "var(--text-secondary)", textTransform: "uppercase", letterSpacing: "0.06em" }}>PACKAGES</span>
          <Button size="sm" onClick={() => setCreating(c => !c)}>{creating ? "Cancel" : "+ New"}</Button>
        </div>
        {creating && (
          <form onSubmit={handleCreate} style={{ padding: 12, borderBottom: "1px solid var(--border-subtle)" }}>
            <span style={S.label}>App name</span>
            <input style={pkgInputStyle} value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} required placeholder="MyApp" />
            <span style={S.label}>Version</span>
            <input style={pkgInputStyle} value={form.version} onChange={e => setForm(f => ({ ...f, version: e.target.value }))} required placeholder="1.0.0" />
            <span style={S.label}>Description (optional)</span>
            <input style={pkgInputStyle} value={form.description} onChange={e => setForm(f => ({ ...f, description: e.target.value }))} placeholder="Release notes…" />
            {err && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8 }}>{err}</div>}
            <Button disabled={loading}>{loading ? "Packaging…" : "Package Now"}</Button>
          </form>
        )}
        <div style={{ flex: 1, overflow: "auto" }}>
          {packages.map(p => (
            <div key={p.id} onClick={() => loadDetail(p)} style={{
              padding: "10px 14px", cursor: "pointer", borderBottom: "1px solid var(--border-subtle)",
              background: selected?.id === p.id ? "color-mix(in srgb, var(--accent) 8%, transparent)" : "transparent",
            }}>
              <div style={{ fontSize: 13, fontWeight: 600 }}>{p.name}</div>
              <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 3 }}>
                <span style={{ fontSize: 11, color: "var(--text-muted)" }}>v{p.version}</span>
                <span style={{ ...S.badge, background: (PKG_STATUS_COLOR[p.status] || "#6b7280") + "22", color: PKG_STATUS_COLOR[p.status] || "#6b7280" }}>{p.status}</span>
              </div>
            </div>
          ))}
          {packages.length === 0 && (
            <div style={{ padding: 16, fontSize: 12, color: "var(--text-muted)" }}>No packages yet.</div>
          )}
        </div>
      </div>

      {/* Detail */}
      <div style={{ flex: 1, overflow: "auto", padding: "24px 28px" }}>
        {!selected && (
          <div style={{ color: "var(--text-muted)", fontSize: 13, paddingTop: 40 }}>
            Select a package to view details, or create a new one.
          </div>
        )}
        {selected && detail && (
          <>
            <div style={{ display: "flex", alignItems: "flex-start", gap: 16, marginBottom: 20 }}>
              <div style={{ flex: 1 }}>
                <h2 style={{ margin: 0, fontSize: 18, fontWeight: 700, color: "var(--text-primary)" }}>{detail.name}</h2>
                <div style={{ fontSize: 13, color: "var(--text-muted)", marginTop: 4 }}>
                  v{detail.version} · {detail.created_at ? new Date(detail.created_at).toLocaleString() : "—"}
                </div>
                {detail.description && <div style={{ fontSize: 13, marginTop: 8, color: "var(--text-secondary)" }}>{detail.description}</div>}
              </div>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <Button size="sm" variant="secondary" onClick={() => handleDownload(selected)}>↓ Download ZIP</Button>
                {detail.status === "draft" && (
                  <Button size="sm" onClick={() => handleStatusChange(selected, "published")}>✓ Publish</Button>
                )}
                {detail.status !== "deprecated" && (
                  <Button size="sm" variant="danger" onClick={() => handleStatusChange(selected, "deprecated")}>Archive</Button>
                )}
              </div>
            </div>

            {detail.manifest && (
              <div style={S.card}>
                <div style={{ ...S.label, marginBottom: 10 }}>MANIFEST</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 20 }}>
                  {Object.entries(detail.manifest).filter(([k]) => !["checksum", "packaged_at"].includes(k)).map(([k, v]) => (
                    <div key={k}>
                      <div style={{ fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase" }}>{k.replace(/_/g, " ")}</div>
                      <div style={{ fontSize: 18, fontWeight: 700, color: "var(--accent)" }}>{String(v)}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div style={{ ...S.card, background: "var(--accent-dim)", border: "1px solid var(--accent)", marginTop: 12 }}>
              <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.8 }}>
                To promote this package to an environment, use the <strong>Promote</strong> tab — full governance, risk analysis, and approval flow applies.
              </div>
            </div>
          </>
        )}
        {selected && !detail && (
          <div style={{ color: "var(--text-muted)", fontSize: 13 }}>Loading…</div>
        )}
      </div>
    </div>
  );
}


// ── Diff Tab (from App Registry) ──────────────────────────────────────────────

type DiffSection = {
  added: string[]; removed: string[]; changed: { id: string; label: string }[];
  unchanged: number; total_old: number; total_new: number;
};
type DiffResult = {
  has_changes: boolean;
  summary: { added: number; removed: number; changed: number };
  package_a: { name: string; version: string };
  package_b: { name: string; version: string };
  sections: Record<string, DiffSection>;
};

function DiffTab() {
  const [packages, setPackages]   = useState<Pkg[]>([]);
  const [pkgA, setPkgA]           = useState("");
  const [pkgB, setPkgB]           = useState("");
  const [result, setResult]       = useState<DiffResult | null>(null);
  const [loading, setLoading]     = useState(false);

  useEffect(() => {
    fetch(`${APPS_API}/packages`, { headers: authHdr() })
      .then(r => r.ok ? r.json() : null)
      .then(d => setPackages(d?.packages ?? []));
  }, []);

  async function handleDiff(e: React.FormEvent) {
    e.preventDefault(); setLoading(true); setResult(null);
    try {
      const r = await fetch(`${APPS_API}/packages/${pkgA}/diff/${pkgB}`, { headers: authHdr() });
      if (r.ok) setResult(await r.json());
    } finally { setLoading(false); }
  }

  const diffSelectStyle: React.CSSProperties = { width: "100%", padding: "8px 10px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)" };
  const sections = result ? Object.entries(result.sections) : [];
  const changedSections = sections.filter(([, d]) => d.added.length || d.removed.length || d.changed.length);

  return (
    <div style={{ padding: "20px 28px", overflow: "auto", flex: 1 }}>
      <form onSubmit={handleDiff} style={{ display: "flex", gap: 12, alignItems: "flex-end", marginBottom: 24 }}>
        <div style={{ flex: 1 }}>
          <span style={S.label}>Base (older)</span>
          <select value={pkgA} onChange={e => setPkgA(e.target.value)} required style={diffSelectStyle}>
            <option value="">Select package…</option>
            {packages.map(p => <option key={p.id} value={p.id}>{p.name} v{p.version}</option>)}
          </select>
        </div>
        <div style={{ flex: 1 }}>
          <span style={S.label}>Compare (newer)</span>
          <select value={pkgB} onChange={e => setPkgB(e.target.value)} required style={diffSelectStyle}>
            <option value="">Select package…</option>
            {packages.map(p => <option key={p.id} value={p.id}>{p.name} v{p.version}</option>)}
          </select>
        </div>
        <Button disabled={loading || !pkgA || !pkgB}>{loading ? "Diffing…" : "Compare"}</Button>
      </form>

      {result && (
        <>
          <div style={{ ...S.card, marginBottom: 20 }}>
            <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 8, color: "var(--text-primary)" }}>
              {result.package_a.name} v{result.package_a.version} → {result.package_b.name} v{result.package_b.version}
            </div>
            {!result.has_changes ? (
              <div style={{ color: "#22c55e", fontSize: 13 }}>✓ No changes between these packages.</div>
            ) : (
              <div style={{ display: "flex", gap: 20 }}>
                <div><span style={{ color: "#22c55e", fontWeight: 700 }}>+{result.summary.added}</span> <span style={{ fontSize: 12, color: "var(--text-muted)" }}>added</span></div>
                <div><span style={{ color: "#ef4444", fontWeight: 700 }}>-{result.summary.removed}</span> <span style={{ fontSize: 12, color: "var(--text-muted)" }}>removed</span></div>
                <div><span style={{ color: "#f59e0b", fontWeight: 700 }}>{result.summary.changed}</span> <span style={{ fontSize: 12, color: "var(--text-muted)" }}>changed</span></div>
              </div>
            )}
          </div>

          {changedSections.map(([section, d]) => (
            <div key={section} style={{ ...S.card, marginBottom: 10 }}>
              <div style={{ ...S.label, marginBottom: 8 }}>{section.replace(/_/g, " ")}</div>
              {d.added.map((name, i) => <div key={i} style={{ fontSize: 12, color: "#22c55e" }}>+ {name}</div>)}
              {d.removed.map((name, i) => <div key={i} style={{ fontSize: 12, color: "#ef4444" }}>− {name}</div>)}
              {d.changed.map((c, i) => <div key={i} style={{ fontSize: 12, color: "#f59e0b" }}>~ {c.label}</div>)}
            </div>
          ))}

          {result.has_changes && changedSections.length === 0 && (
            <div style={{ fontSize: 13, color: "var(--text-muted)" }}>All changes are in metadata fields.</div>
          )}
        </>
      )}
    </div>
  );
}


// ── Root ──────────────────────────────────────────────────────────────────────

type Tab = "environments" | "promote" | "runs" | "windows" | "packages" | "diff";

const TABS: { key: Tab; label: string }[] = [
  { key: "environments", label: "Environments" },
  { key: "promote",      label: "Promote" },
  { key: "runs",         label: "Deployment Runs" },
  { key: "windows",      label: "Change Windows" },
  { key: "packages",     label: "Packages" },
  { key: "diff",         label: "Diff" },
];

export default function HxDeploy() {
  const [tab, setTab]             = useState<Tab>("environments");
  const [envs, setEnvs]           = useState<any[]>([]);
  const [activeEnv, setActiveEnv] = useState<string>("");

  const loadEnvs = useCallback(async () => {
    const r = await apiFetch("/environments");
    if (!r.ok) return;
    const data: any[] = await r.json();
    const sorted = [...data].sort((a, b) => a.order_index - b.order_index);
    setEnvs(sorted);
    setActiveEnv(prev => prev && sorted.some(e => e.name === prev) ? prev : (sorted[0]?.name ?? ""));
  }, []);

  useEffect(() => { loadEnvs(); }, [loadEnvs]);

  const activeLabel = envs.find(e => e.name === activeEnv)?.label ?? "";

  return (
    <div style={S.page}>
      <div style={S.body}>
        {/* Env switcher — built from registered environments, no hardcoding */}
        {envs.length > 0 && (
          <div style={{ display: "flex", gap: 6, marginBottom: 16, alignItems: "center" }}>
            <span style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", marginRight: 4 }}>Environment</span>
            {envs.map(e => (
              <button key={e.name} onClick={() => setActiveEnv(e.name)} style={{
                padding: "5px 14px", borderRadius: 20, border: "1px solid",
                borderColor: activeEnv === e.name ? "var(--accent)" : "var(--border-default)",
                background: activeEnv === e.name ? "var(--accent-dim)" : "transparent",
                color: activeEnv === e.name ? "var(--accent)" : "var(--text-muted)",
                fontSize: 12, fontWeight: activeEnv === e.name ? 700 : 400,
                fontFamily: "var(--font-mono)", cursor: "pointer",
                textTransform: "uppercase" as const, letterSpacing: "0.04em",
              }}>
                {e.name}
              </button>
            ))}
            {activeLabel && (
              <span style={{ fontSize: 12, color: "var(--text-muted)", marginLeft: 8 }}>— {activeLabel}</span>
            )}
          </div>
        )}

        <div style={S.tabBar}>
          {TABS.map(({ key, label }) => (
            <button key={key} style={{ ...S.tab, ...(tab === key ? S.tabA : {}) }} onClick={() => setTab(key)}>
              {label}
            </button>
          ))}
        </div>
        {tab === "environments" && <EnvironmentsTab activeEnv={activeEnv} onEnvsChanged={loadEnvs} />}
        {tab === "promote"      && <PromoteTab onDone={() => setTab("runs")} activeEnv={activeEnv} />}
        {tab === "runs"         && <RunsTab activeEnv={activeEnv} />}
        {tab === "windows"      && <WindowsTab activeEnv={activeEnv} />}
        {tab === "packages"     && <PackagesTab />}
        {tab === "diff"         && <DiffTab />}
      </div>
    </div>
  );
}
