// HELIX P33 — Customer Portal Admin
import React, { useEffect, useState, useCallback } from "react";
import { useFeatureFlags } from "@/app/FeatureFlagsContext";

type Tenant = {
  id: string;
  slug: string;
  name: string;
  portal_enabled: boolean;
  welcome_text: string;
  brand_color: string;
  logo_text: string;
  allowed_case_type_ids: string[];
  portal_case_type_count: number;
};

type Submission = {
  case_id: string;
  tracking_token: string;
  submitter_name: string;
  submitter_email: string;
  subject: string;
  status: string;
  priority: string;
  case_type_name: string;
  portal_slug: string;
  submitted_at: string;
};

type CaseType = {
  id: string;
  name: string;
  portal_enabled: boolean;
};

type Tab = "tenants" | "submissions" | "customers";

type PortalCustomer = {
  id: string; display_name: string;
  primary_email: string; alt_email: string | null;
  preferred_email: "primary" | "alt";
  phone: string | null; verified: boolean;
  created_at: string; last_active_at: string;
};
type CustomerDetail = PortalCustomer & {
  cases: { case_id: string; case_number: string | null; subject: string; status: string; submitted_at: string }[];
};

const STATUS_COLOR: Record<string, string> = {
  new: "#0d9488", open: "#3b82f6", in_progress: "#f59e0b", pending: "#0f766e",
  resolved: "#22c55e", closed: "#6b7280", cancelled: "#ef4444",
};

function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}

async function apiJSON<T>(url: string, opts: RequestInit = {}): Promise<T> {
  const r = await fetch(url, { ...opts, headers: { "Content-Type": "application/json", ..._authHdr(), ...(opts.headers || {}) } });
  if (!r.ok) throw new Error(await r.text().catch(() => r.statusText));
  return r.json();
}

export default function PortalAdmin() {
  const [tab, setTab] = useState<Tab>("tenants");
  const { isEnabled } = useFeatureFlags();

  const tabs: Tab[] = ["tenants", "submissions", ...(isEnabled("customer_accounts") ? ["customers" as Tab] : [])];

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", overflow: "auto", height: "100%", boxSizing: "border-box" }}>
      {/* Tabs */}
      <div style={{ display: "flex", gap: 4, marginBottom: "var(--space-lg)", borderBottom: "1px solid var(--border-subtle)", paddingBottom: 0 }}>
        {tabs.map(t => (
          <button key={t} onClick={() => setTab(t)} style={{
            padding: "8px 20px", border: "none", cursor: "pointer", fontSize: 13,
            background: "none", fontFamily: "var(--font-body)",
            color: tab === t ? "var(--accent)" : "var(--text-secondary)",
            fontWeight: tab === t ? 600 : 400,
            borderBottom: tab === t ? "2px solid var(--accent)" : "2px solid transparent",
            marginBottom: -1,
          }}>
            {t === "tenants" ? "Portal Config" : t === "submissions" ? "Submissions" : "Customers"}
          </button>
        ))}
      </div>

      {tab === "tenants" && <TenantsTab />}
      {tab === "submissions" && <SubmissionsTab />}
      {tab === "customers" && <CustomersTab />}
    </div>
  );
}


/* ── Tenants Tab ──────────────────────────────────────────── */

function TenantsTab() {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Tenant | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [caseTypes, setCaseTypes] = useState<CaseType[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiJSON<Tenant[]>("/api/v1/portal-admin/tenants");
      setTenants(data);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  // Load case types for the editor
  useEffect(() => {
    if (!editing) return;
    apiJSON<any>("/api/v1/case-types?page_size=100")
      .then(d => setCaseTypes((d.items ?? []).map((ct: any) => ({ id: ct.id, name: ct.name, portal_enabled: ct.portal_enabled ?? false }))))
      .catch(() => {});
  }, [editing]);

  async function togglePortal(slug: string, enabled: boolean) {
    try {
      await apiJSON(`/api/v1/portal-admin/tenants/${slug}`, {
        method: "PATCH", body: JSON.stringify({ enabled }),
      });
      load();
    } catch (e: any) {
      alert(`Failed: ${e.message}`);
    }
  }

  async function saveSettings() {
    if (!editing) return;
    setSaving(true);
    setSaveErr(null);
    try {
      await apiJSON(`/api/v1/portal-admin/tenants/${editing.slug}`, {
        method: "PATCH",
        body: JSON.stringify({
          welcome_text: editing.welcome_text,
          brand_color: editing.brand_color,
          logo_text: editing.logo_text,
          allowed_case_type_ids: editing.allowed_case_type_ids,
        }),
      });
      setEditing(null);
      load();
    } catch (e: any) {
      setSaveErr(e.message);
    } finally {
      setSaving(false);
    }
  }

  async function toggleCaseType(ctId: string, enabled: boolean) {
    try {
      await apiJSON(`/api/v1/portal-admin/case-types/${ctId}/portal?enabled=${enabled}`, { method: "PATCH" });
      setCaseTypes(cts => cts.map(c => c.id === ctId ? { ...c, portal_enabled: enabled } : c));
    } catch (e: any) {
      alert(`Failed: ${e.message}`);
    }
  }

  if (loading) return <div style={{ color: "var(--text-muted)", padding: 32 }}>Loading…</div>;
  if (error) return <div style={{ color: "var(--status-failed)", padding: 32 }}>Error: {error}</div>;

  return (
    <div>
      {tenants.length === 0 && (
        <div style={{ padding: 48, color: "var(--text-muted)" }}>
          No tenants found. Create tenants in the Tenants module first.
        </div>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-md)" }}>
        {tenants.map(t => (
          <div key={t.id} style={{
            background: "var(--bg-card)", border: "1px solid var(--border-subtle)",
            borderRadius: "var(--radius-md)", padding: "var(--space-md)",
            borderLeft: `4px solid ${t.portal_enabled ? "#22c55e" : "#d1d5db"}`,
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
              <div>
                <div style={{ fontWeight: 700, fontSize: 15, color: "var(--text-primary)" }}>{t.name}</div>
                <div style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 2 }}>
                  slug: {t.slug} · Portal URL: <span style={{ color: "var(--accent)" }}>/portal/{t.slug}</span>
                </div>
                <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 4 }}>
                  {t.portal_case_type_count} case type{t.portal_case_type_count !== 1 ? "s" : ""} enabled for portal
                </div>
              </div>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <div style={{
                  padding: "4px 12px", borderRadius: 20, fontSize: 12, fontWeight: 600,
                  background: t.portal_enabled ? "#dcfce7" : "#f3f4f6",
                  color: t.portal_enabled ? "#16a34a" : "#6b7280",
                }}>
                  {t.portal_enabled ? "Active" : "Inactive"}
                </div>
                <button
                  onClick={() => togglePortal(t.slug, !t.portal_enabled)}
                  style={{
                    padding: "6px 14px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)",
                    cursor: "pointer", fontSize: 12, background: "var(--bg-elevated)", color: "var(--text-secondary)",
                  }}
                >
                  {t.portal_enabled ? "Disable" : "Enable"}
                </button>
                <button
                  onClick={() => setEditing({ ...t })}
                  style={{
                    padding: "6px 14px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)",
                    cursor: "pointer", fontSize: 12, background: "var(--accent)", color: "#fff", fontWeight: 600,
                  }}
                >
                  Configure
                </button>
                <a
                  href={`/portal/${t.slug}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{
                    padding: "6px 14px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)",
                    cursor: "pointer", fontSize: 12, background: "var(--bg-elevated)", color: "var(--text-secondary)",
                    textDecoration: "none", display: "inline-flex", alignItems: "center", gap: 4,
                  }}
                >
                  ↗ Open Portal
                </a>
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* Edit modal */}
      {editing && (
        <div onClick={() => setEditing(null)} style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.4)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 100 }}>
          <div onClick={e => e.stopPropagation()} style={{
            background: "var(--bg-panel)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-lg)",
            padding: "var(--space-xl)", width: 600, maxHeight: "85vh", overflow: "auto", boxShadow: "var(--shadow-lg)",
          }}>
            <h2 style={{ fontFamily: "var(--font-display)", fontSize: 20, fontWeight: 700, marginBottom: "var(--space-lg)" }}>
              Configure Portal — {editing.name}
            </h2>

            <ModalField label="Welcome Text">
              <textarea
                value={editing.welcome_text}
                onChange={e => setEditing({ ...editing, welcome_text: e.target.value })}
                rows={3} style={modalInput}
              />
            </ModalField>

            <ModalField label="Logo Text">
              <input value={editing.logo_text} onChange={e => setEditing({ ...editing, logo_text: e.target.value })} style={modalInput} />
            </ModalField>

            <ModalField label="Brand Color">
              <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                <input type="color" value={editing.brand_color} onChange={e => setEditing({ ...editing, brand_color: e.target.value })}
                  style={{ width: 48, height: 36, border: "1px solid var(--border-default)", borderRadius: 6, padding: 2, cursor: "pointer" }} />
                <input value={editing.brand_color} onChange={e => setEditing({ ...editing, brand_color: e.target.value })} style={{ ...modalInput, flex: 1, marginBottom: 0 }} />
              </div>
            </ModalField>

            <div style={{ marginBottom: "var(--space-md)" }}>
              <div style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
                Case Types for Portal
              </div>
              {caseTypes.length === 0 ? (
                <p style={{ fontSize: 13, color: "var(--text-muted)" }}>No case types found.</p>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: 6, maxHeight: 240, overflow: "auto", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", padding: 8 }}>
                  {caseTypes.map(ct => (
                    <label key={ct.id} style={{ display: "flex", alignItems: "center", gap: 10, padding: 8, borderRadius: "var(--radius-sm)", cursor: "pointer", background: ct.portal_enabled ? "color-mix(in srgb, var(--accent) 8%, transparent)" : "transparent" }}>
                      <input
                        type="checkbox"
                        checked={ct.portal_enabled}
                        onChange={e => toggleCaseType(ct.id, e.target.checked)}
                        style={{ width: 16, height: 16, accentColor: "var(--accent)" }}
                      />
                      <span style={{ fontSize: 13, color: "var(--text-primary)" }}>{ct.name}</span>
                      {ct.portal_enabled && <span style={{ fontSize: 11, color: "var(--accent)", marginLeft: "auto" }}>Portal enabled</span>}
                    </label>
                  ))}
                </div>
              )}
            </div>

            {saveErr && <p style={{ color: "var(--status-failed)", fontSize: 13, marginBottom: 12 }}>{saveErr}</p>}

            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: "var(--space-lg)" }}>
              <button onClick={() => setEditing(null)} style={ghostBtn}>Cancel</button>
              <button onClick={saveSettings} disabled={saving} style={{ ...primaryBtn, opacity: saving ? 0.7 : 1 }}>
                {saving ? "Committing…" : "Commit Settings"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}


/* ── Submissions Tab ─────────────────────────────────────── */

function SubmissionsTab() {
  const [submissions, setSubmissions] = useState<Submission[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState("");

  useEffect(() => {
    setLoading(true);
    const qs = statusFilter ? `?status=${statusFilter}` : "";
    apiJSON<Submission[]>(`/api/v1/portal-admin/submissions${qs}`)
      .then(setSubmissions)
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, [statusFilter]);

  return (
    <div>
      <div style={{ display: "flex", gap: 8, marginBottom: "var(--space-lg)", alignItems: "center" }}>
        <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>Filter by status:</span>
        {["", "new", "open", "in_progress", "resolved", "closed"].map(s => (
          <button key={s} onClick={() => setStatusFilter(s)} style={{
            padding: "5px 12px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)",
            cursor: "pointer", fontSize: 12,
            background: statusFilter === s ? "var(--accent)" : "var(--bg-elevated)",
            color: statusFilter === s ? "#fff" : "var(--text-secondary)",
            fontWeight: statusFilter === s ? 600 : 400,
          }}>
            {s || "All"}
          </button>
        ))}
      </div>

      {loading && <div style={{ color: "var(--text-muted)" }}>Loading…</div>}
      {error && <div style={{ color: "var(--status-failed)" }}>Error: {error}</div>}

      {!loading && !error && submissions.length === 0 && (
        <div style={{ padding: 48, color: "var(--text-muted)" }}>
          No portal submissions yet.
        </div>
      )}

      {submissions.length > 0 && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "2fr 120px 80px 100px 120px 110px", padding: "6px 12px", fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em" }}>
            <span>Request</span><span>Type</span><span>Status</span><span>Submitter</span><span>Portal</span><span>Date</span>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-xs)" }}>
            {submissions.map(s => (
              <div key={s.case_id} style={{
                display: "grid", gridTemplateColumns: "2fr 120px 80px 100px 120px 110px",
                alignItems: "center", padding: "var(--space-md)",
                background: "var(--bg-card)", border: "1px solid var(--border-subtle)",
                borderRadius: "var(--radius-md)",
                borderLeft: `3px solid ${STATUS_COLOR[s.status] || "#0d9488"}`,
              }}>
                <div>
                  <div style={{ fontWeight: 600, fontSize: 13, color: "var(--text-primary)" }}>{s.subject || "—"}</div>
                  <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 1 }}>
                    {s.case_id.slice(0, 8)}…
                  </div>
                </div>
                <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>{s.case_type_name}</div>
                <div style={{
                  fontSize: 11, padding: "2px 8px", borderRadius: 12, width: "fit-content",
                  background: (STATUS_COLOR[s.status] || "#6b7280") + "22",
                  color: STATUS_COLOR[s.status] || "#6b7280", fontWeight: 600,
                  textTransform: "capitalize",
                }}>
                  {s.status.replace(/_/g, " ")}
                </div>
                <div>
                  <div style={{ fontSize: 12, color: "var(--text-primary)" }}>{s.submitter_name}</div>
                  <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{s.submitter_email}</div>
                </div>
                <div style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text-secondary)" }}>
                  {s.portal_slug || "—"}
                </div>
                <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                  {new Date(s.submitted_at).toLocaleDateString()}
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}


/* ── Helpers ─────────────────────────────────────────────── */

function ModalField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: "var(--space-md)" }}>
      <div style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.06em" }}>{label}</div>
      {children}
    </div>
  );
}

const modalInput: React.CSSProperties = {
  width: "100%", padding: "9px 12px", background: "var(--bg-input)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-primary)", fontFamily: "var(--font-body)", fontSize: 14,
  marginBottom: 0, boxSizing: "border-box",
};

const primaryBtn: React.CSSProperties = {
  padding: "8px 20px", background: "var(--accent)", color: "#fff", border: "none",
  borderRadius: "var(--radius-sm)", cursor: "pointer", fontWeight: 600, fontSize: 13,
};

const ghostBtn: React.CSSProperties = {
  padding: "8px 20px", background: "none", color: "var(--text-secondary)",
  border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", cursor: "pointer", fontSize: 13,
};

/* ── Customers Tab ─────────────────────────────────────────── */

function CustomersTab() {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [slug, setSlug]       = useState("");
  const [q, setQ]             = useState("");
  const [customers, setCustomers] = useState<PortalCustomer[]>([]);
  const [total, setTotal]     = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError]     = useState<string | null>(null);
  const [detail, setDetail]   = useState<CustomerDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);

  useEffect(() => {
    apiJSON<{ tenants: Tenant[] }>("/api/v1/portal/admin/tenants")
      .then(d => { setTenants(d.tenants ?? []); if (d.tenants?.length) setSlug(d.tenants[0].slug); })
      .catch(() => {});
  }, []);

  const loadCustomers = useCallback(async (s: string, query: string) => {
    if (!s) return;
    setLoading(true); setError(null);
    try {
      const d = await apiJSON<{ customers: PortalCustomer[]; total: number }>(
        `/api/v1/portal/${s}/customers?q=${encodeURIComponent(query)}&limit=50`
      );
      setCustomers(d.customers ?? []); setTotal(d.total ?? 0);
    } catch (e: any) { setError(e.message); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { if (slug) loadCustomers(slug, q); }, [slug, loadCustomers]);

  const openDetail = async (id: string) => {
    setDetailLoading(true);
    try {
      const d = await apiJSON<CustomerDetail>(`/api/v1/portal/${slug}/customers/${id}`);
      setDetail(d);
    } catch {}
    finally { setDetailLoading(false); }
  };

  const handleDelete = async (id: string) => {
    if (!confirm("Anonymise this customer? This action cannot be undone.")) return;
    setDeleting(id);
    try {
      await apiJSON(`/api/v1/portal/${slug}/customers/${id}`, { method: "DELETE" });
      setDetail(null);
      loadCustomers(slug, q);
    } catch {}
    finally { setDeleting(null); }
  };

  const STATUS_C: Record<string, string> = { new: "#0d9488", open: "#3b82f6", in_progress: "#f59e0b", pending: "#0f766e", resolved: "#22c55e", closed: "#6b7280", cancelled: "#ef4444" };

  return (
    <div>
      {/* Controls */}
      <div style={{ display: "flex", gap: 12, marginBottom: 20, flexWrap: "wrap" }}>
        <select value={slug} onChange={e => setSlug(e.target.value)}
          style={{ padding: "8px 12px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", background: "var(--bg-input)", color: "var(--text-primary)", fontSize: 13 }}>
          {tenants.map(t => <option key={t.slug} value={t.slug}>{t.name} ({t.slug})</option>)}
        </select>
        <input value={q} onChange={e => setQ(e.target.value)}
          onKeyDown={e => e.key === "Enter" && loadCustomers(slug, q)}
          placeholder="Search by email or name…"
          style={{ flex: 1, minWidth: 200, ...modalInput }} />
        <button onClick={() => loadCustomers(slug, q)} style={primaryBtn}>Search</button>
      </div>

      {error && <div style={{ color: "#ef4444", marginBottom: 12, fontSize: 13 }}>{error}</div>}
      {!error && !loading && <div style={{ fontSize: 12, color: "var(--text-tertiary)", marginBottom: 12 }}>{total} customer{total !== 1 ? "s" : ""}</div>}

      {loading && <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>Loading…</div>}

      {!loading && customers.length === 0 && slug && (
        <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>No customers found for this portal.</div>
      )}

      {!loading && customers.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
          {/* Header */}
          <div style={{ display: "grid", gridTemplateColumns: "2fr 2fr 1fr 1fr 80px", gap: 12, padding: "8px 12px", fontSize: 11, fontWeight: 700, color: "var(--text-tertiary)", textTransform: "uppercase", letterSpacing: "0.06em" }}>
            <div>Name</div><div>Email</div><div>Alt Email</div><div>Joined</div><div></div>
          </div>
          {customers.map(c => (
            <div key={c.id} onClick={() => openDetail(c.id)}
              style={{ display: "grid", gridTemplateColumns: "2fr 2fr 1fr 1fr 80px", gap: 12, padding: "10px 12px", background: "var(--bg-card)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)", cursor: "pointer", alignItems: "center" }}>
              <div>
                <div style={{ fontWeight: 600, fontSize: 13, color: "var(--text-primary)" }}>{c.display_name}</div>
                <div style={{ fontSize: 11, color: c.verified ? "#16a34a" : "#f59e0b" }}>{c.verified ? "Verified" : "Unverified"}</div>
              </div>
              <div style={{ fontSize: 13, color: "var(--text-secondary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {c.primary_email}
                {c.preferred_email === "primary" && <span style={{ marginLeft: 6, fontSize: 10, color: "var(--accent)", fontWeight: 700 }}>DEFAULT</span>}
              </div>
              <div style={{ fontSize: 13, color: "var(--text-secondary)" }}>
                {c.alt_email
                  ? <>{c.alt_email.split("@")[0]}…{c.preferred_email === "alt" && <span style={{ marginLeft: 4, fontSize: 10, color: "var(--accent)", fontWeight: 700 }}>DEFAULT</span>}</>
                  : <span style={{ color: "var(--text-tertiary)" }}>—</span>}
              </div>
              <div style={{ fontSize: 12, color: "var(--text-tertiary)" }}>{new Date(c.created_at).toLocaleDateString()}</div>
              <div onClick={e => { e.stopPropagation(); handleDelete(c.id); }}
                style={{ fontSize: 12, color: "#ef4444", cursor: "pointer", fontWeight: 600, opacity: deleting === c.id ? 0.4 : 1 }}>
                {deleting === c.id ? "…" : "Anonymise"}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Detail panel */}
      {detailLoading && <div style={{ marginTop: 24, color: "var(--text-secondary)", fontSize: 13 }}>Loading customer detail…</div>}
      {detail && !detailLoading && (
        <div style={{ marginTop: 24, background: "var(--bg-card)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)", padding: 20 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
            <div>
              <div style={{ fontWeight: 700, fontSize: 16, color: "var(--text-primary)" }}>{detail.display_name}</div>
              <div style={{ fontSize: 13, color: "var(--text-secondary)", marginTop: 2 }}>
                {detail.primary_email}{detail.preferred_email === "primary" && " (default)"}
                {detail.alt_email && <> · {detail.alt_email}{detail.preferred_email === "alt" && " (default)"}</>}
                {detail.phone && <> · {detail.phone}</>}
              </div>
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button onClick={() => handleDelete(detail.id)} style={{ ...ghostBtn, color: "#ef4444", borderColor: "#fca5a5", fontSize: 12 }}>
                Anonymise (GDPR)
              </button>
              <button onClick={() => setDetail(null)} style={{ ...ghostBtn, fontSize: 12 }}>Close</button>
            </div>
          </div>

          <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-tertiary)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 10 }}>
            {detail.cases.length} Case{detail.cases.length !== 1 ? "s" : ""}
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {detail.cases.length === 0 && <div style={{ fontSize: 13, color: "var(--text-tertiary)" }}>No cases linked.</div>}
            {detail.cases.map(c => (
              <div key={c.case_id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "8px 12px", background: "var(--bg-base)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)" }}>
                <div>
                  <div style={{ fontWeight: 600, fontSize: 13, color: "var(--text-primary)" }}>{c.subject}</div>
                  <div style={{ fontSize: 11, color: "var(--text-tertiary)" }}>
                    {c.case_number && <span style={{ marginRight: 8 }}>{c.case_number}</span>}
                    {new Date(c.submitted_at).toLocaleDateString()}
                  </div>
                </div>
                <span style={{ fontSize: 11, fontWeight: 700, color: STATUS_C[c.status] ?? "#6b7280", background: (STATUS_C[c.status] ?? "#6b7280") + "18", padding: "3px 8px", borderRadius: 4 }}>
                  {c.status.replace("_", " ")}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
