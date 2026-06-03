import React, { useState, useEffect, useCallback } from "react";
import { useApi } from "@shared/hooks";
import { useAuth } from "@/auth";
import { usePermissions } from "@/auth/PermissionsContext";
import { NAV_DATA } from "@/app/nav-data";
import {
  getSystemInfo,
  searchAuditLog,
  getAuditActions,
  listQueues,
  createQueue,
  updateQueue,
  deleteQueue,
  listTenants,
  listWebhooks,
  createWebhook,
  updateWebhook,
  testWebhook,
  listWebhookEvents,
  deleteWebhook,
  listCalendars,
  createCalendar,
  updateCalendar,
  listRules,
  createRule,
  updateRule,
  deleteRule,
  getCaseType,
  getDataModel,
  listCaseTypes,
  listForms,
  getForm,
} from "@shared/api/client";
import { Card, Button, Spinner, EmptyState, Stat, TimeAgo } from "@shared/components";

/* ═══════════════════════════════════════════════════════════════════
   Admin Console — system management dashboard
   ═══════════════════════════════════════════════════════════════════ */

type Tab = "overview" | "audit" | "queues" | "webhooks" | "rules" | "calendars" | "permissions" | "marketplace";

const TAB_LABELS: Record<Tab, string> = {
  overview: "Overview", audit: "Audit", queues: "Queues",
  webhooks: "Webhooks", rules: "Business Rules", calendars: "Calendars",
  permissions: "Permissions", marketplace: "Marketplace Sources", // hidden — see tab render below
};

export default function AdminConsole() {
  const [tab, setTab] = useState<Tab>("overview");

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", overflow: "auto", height: "100%", boxSizing: "border-box" }}>
      {/* Tabs */}
      <div style={{
        display: "flex", gap: 2, marginBottom: "var(--space-xl)",
        borderBottom: "1px solid var(--border-subtle)", paddingBottom: 0,
      }}>
        {(["overview", "audit", "queues", "webhooks", "rules", "calendars", "permissions"] as Tab[]).map(t => (
          // "marketplace" tab hidden — re-enable by adding it back to the array above
          <button key={t} onClick={() => setTab(t)} style={{
            padding: "10px 16px", fontSize: 12, fontWeight: 500, fontFamily: "var(--font-mono)",
            textTransform: "uppercase", letterSpacing: "0.04em", border: "none", cursor: "pointer",
            color: tab === t ? "var(--accent)" : "var(--text-muted)",
            background: "transparent",
            borderBottom: tab === t ? "2px solid var(--accent)" : "2px solid transparent",
            marginBottom: -1,
          }}>{TAB_LABELS[t]}</button>
        ))}
      </div>

      {tab === "overview"    && <OverviewTab />}
      {tab === "audit"       && <AuditLogTab />}
      {tab === "queues"      && <QueuesTab />}
      {tab === "webhooks"    && <WebhooksTab />}
      {tab === "rules"       && <RulesTab />}
      {tab === "calendars"   && <CalendarsTab />}
      {tab === "permissions" && <PermissionsTab />}
      {tab === "marketplace" && <MarketplaceSourcesTab />}
    </div>
  );
}

/* ── Overview Tab ─────────────────────────────────────────────── */

function OverviewTab() {
  const { data, loading } = useApi(getSystemInfo);

  if (loading || !data) return <Spinner size={28} />;

  const items = [
    { label: "Case Types", value: data.case_types },
    { label: "Cases", value: data.cases },
    { label: "Assignments", value: data.assignments },
    { label: "Queues", value: data.queues },
    { label: "Business Rules", value: data.rules },
    { label: "Forms", value: data.forms },
    { label: "Webhooks", value: data.webhooks },
    { label: "Calendars", value: data.calendars },
    { label: "Audit Entries", value: data.audit_entries },
  ];

  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "var(--space-md)" }}>
      {items.map(i => (
        <Card key={i.label}><Stat label={i.label} value={i.value} /></Card>
      ))}
    </div>
  );
}

/* ── Audit Log Tab ────────────────────────────────────────────── */

function AuditLogTab() {
  const [actionFilter, setActionFilter] = useState("");
  const [days, setDays] = useState(7);
  const [page, setPage] = useState(1);

  const { data: actionsData } = useApi(getAuditActions);
  const actions = actionsData?.actions ?? [];

  const { data, loading, refetch } = useApi(
    () => searchAuditLog({ action: actionFilter || undefined, days, page, page_size: 30 }),
    [actionFilter, days, page]
  );

  const entries = data?.items ?? [];
  const total = data?.total ?? 0;

  return (
    <div>
      {/* Filters */}
      <div style={{ display: "flex", gap: "var(--space-sm)", marginBottom: "var(--space-lg)", alignItems: "center" }}>
        <select value={actionFilter} onChange={e => { setActionFilter(e.target.value); setPage(1); }} style={selectStyle}>
          <option value="">All Actions</option>
          {actions.map((a: string) => <option key={a} value={a}>{a.replace(/_/g, " ")}</option>)}
        </select>
        <div style={{ display: "flex", background: "var(--bg-card)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)" }}>
          {[1, 7, 30, 90].map(d => (
            <button key={d} onClick={() => { setDays(d); setPage(1); }} style={{
              padding: "6px 10px", fontSize: 11, fontFamily: "var(--font-mono)", border: "none", cursor: "pointer",
              color: days === d ? "var(--accent)" : "var(--text-muted)",
              background: days === d ? "var(--accent-dim)" : "transparent",
              borderRadius: "var(--radius-sm)",
            }}>{d}d</button>
          ))}
        </div>
        <span style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginLeft: "auto" }}>
          {total} entries
        </span>
      </div>

      {loading && <Spinner size={28} />}

      {!loading && entries.length === 0 && (
        <EmptyState title="No audit entries" description="No matching audit log entries found." />
      )}

      {!loading && entries.length > 0 && (
        <>
          <div style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)", overflow: "hidden" }}>
            {/* Header */}
            <div style={{
              display: "grid", gridTemplateColumns: "140px 120px 100px 1fr 100px",
              padding: "8px 16px", background: "var(--bg-elevated)",
              fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase",
            }}>
              <span>Action</span><span>Actor</span><span>Case</span><span>Details</span><span>Time</span>
            </div>
            {entries.map((entry: any) => (
              <div key={entry.id} style={{
                display: "grid", gridTemplateColumns: "140px 120px 100px 1fr 100px",
                padding: "10px 16px", borderTop: "1px solid var(--border-subtle)",
                fontSize: 12, alignItems: "center",
              }}>
                <span style={{ fontFamily: "var(--font-mono)", color: actionColor(entry.action), fontWeight: 500 }}>
                  {entry.action.replace(/_/g, " ")}
                </span>
                <span style={{ color: "var(--text-secondary)", fontSize: 11 }}>
                  {entry.actor_id || "system"}
                </span>
                <span style={{ fontFamily: "var(--font-mono)", color: "var(--text-muted)", fontSize: 10 }}>
                  {entry.case_id?.slice(0, 8)}
                </span>
                <span style={{ color: "var(--text-muted)", fontSize: 11, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {JSON.stringify(entry.details || {}).slice(0, 80)}
                </span>
                <span style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                  {entry.timestamp ? <TimeAgo date={entry.timestamp} /> : "—"}
                </span>
              </div>
            ))}
          </div>

          {/* Pagination */}
          {total > 30 && (
            <div style={{ display: "flex", justifyContent: "center", gap: "var(--space-sm)", marginTop: "var(--space-lg)" }}>
              <Button variant="ghost" size="sm" disabled={page <= 1} onClick={() => setPage(p => p - 1)}>← Prev</Button>
              <span style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text-muted)", alignSelf: "center" }}>
                Page {page} of {Math.ceil(total / 30)}
              </span>
              <Button variant="ghost" size="sm" disabled={page * 30 >= total} onClick={() => setPage(p => p + 1)}>Next →</Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

/* ── Queues Tab ───────────────────────────────────────────────── */

const ALL_ROLES = ["admin", "manager", "designer", "case_worker", "devops", "integration", "security", "viewer"];

function QueuesTab() {
  const [tenantId, setTenantId] = useState<string>("");
  const { data: tenantData } = useApi(listTenants);
  const tenants: any[] = tenantData ?? [];

  const { data, loading, refetch } = useApi(
    () => listQueues(tenantId || undefined),
    [tenantId]
  );
  const queues = data ?? [];
  const [editing, setEditing] = useState<any | null>(null); // null = closed, {} = new
  const [form, setForm] = useState({ name: "", description: "", visible_to_roles: [] as string[], auto_assignment: false, filter_status: "", filter_priority: "", max_items: "" });
  const [saving, setSaving] = useState(false);

  const openNew = () => {
    setForm({ name: "", description: "", visible_to_roles: [], auto_assignment: false, filter_status: "", filter_priority: "", max_items: "" });
    setEditing({});
  };
  const openEdit = (q: any) => {
    const fc = q.filter_criteria || {};
    setForm({ name: q.name, description: q.description || "", visible_to_roles: q.visible_to_roles || [], auto_assignment: !!q.auto_assignment, filter_status: fc.status || "", filter_priority: fc.priority || "", max_items: q.max_items ? String(q.max_items) : "" });
    setEditing(q);
  };

  const handleSave = async () => {
    setSaving(true);
    const body: any = {
      name: form.name, description: form.description,
      tenant_id: tenantId || null,
      visible_to_roles: form.visible_to_roles,
      auto_assignment: form.auto_assignment,
      filter_criteria: { ...(form.filter_status ? { status: form.filter_status } : {}), ...(form.filter_priority ? { priority: form.filter_priority } : {}) },
      max_items: form.max_items ? parseInt(form.max_items) : null,
    };
    if (editing?.id) await updateQueue(editing.id, body);
    else await createQueue(body);
    setSaving(false); setEditing(null); refetch();
  };

  const toggleRole = (r: string) => setForm(f => ({ ...f, visible_to_roles: f.visible_to_roles.includes(r) ? f.visible_to_roles.filter(x => x !== r) : [...f.visible_to_roles, r] }));

  return (
    <div>
      {/* Explainer */}
      <Card style={{ marginBottom: "var(--space-lg)", background: "var(--bg-elevated)" }}>
        <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.7 }}>
          <strong style={{ color: "var(--text-primary)" }}>Work Queues</strong> are filtered views of cases for specific teams.
          Workers see their queue in <strong>Work Center → Queues</strong>, claim items, and complete their assigned steps.
          <br />
          <strong style={{ color: "var(--text-primary)" }}>Filter criteria</strong> controls which cases appear (e.g. only high-priority, only specific case type). <strong style={{ color: "var(--text-primary)" }}>Visible to roles</strong> limits who sees the queue. <strong style={{ color: "var(--text-primary)" }}>Auto-assignment</strong> automatically assigns the top item to the next available worker.
        </div>
      </Card>

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-lg)" }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <select
            value={tenantId}
            onChange={e => setTenantId(e.target.value)}
            style={{ padding: "6px 10px", background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", fontSize: 13 }}
          >
            <option value="">All Tenants</option>
            {tenants.map((t: any) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
          <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>
            {queues.length} queue{queues.length !== 1 ? "s" : ""}
            {tenantId && <span style={{ marginLeft: 4, fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--accent)" }}>filtered</span>}
          </span>
        </div>
        <Button size="sm" onClick={openNew}>+ New Queue</Button>
      </div>

      {loading && <Spinner size={28} />}
      {!loading && queues.length === 0 && <EmptyState title="No queues yet" description="Create a queue and assign it to a case type step in the Case Designer." />}

      {queues.map((q: any) => (
        <Card key={q.id} style={{ marginBottom: "var(--space-sm)" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
            <div>
              <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>{q.name}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginTop: 2 }}>
                {q.description || "No description"} · Auto-assign: {q.auto_assignment ? "✓" : "✗"}
                {q.visible_to_roles?.length > 0 && ` · Roles: ${q.visible_to_roles.join(", ")}`}
                {q.max_items && ` · Max: ${q.max_items}`}
                {q.tenant_id
                  ? ` · ${tenants.find((t: any) => t.id === q.tenant_id)?.name ?? "Tenant"}`
                  : " · Global"}
              </div>
              {q.filter_criteria && Object.keys(q.filter_criteria).length > 0 && (
                <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--accent)", marginTop: 4 }}>
                  Filter: {JSON.stringify(q.filter_criteria)}
                </div>
              )}
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              <Button size="sm" variant="secondary" onClick={() => openEdit(q)}>Edit</Button>
              <Button size="sm" variant="danger" onClick={async () => { if (confirm(`Delete "${q.name}"?`)) { await deleteQueue(q.id); refetch(); } }}>Delete</Button>
            </div>
          </div>
        </Card>
      ))}

      {/* Create/Edit modal */}
      {editing !== null && (
        <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <div style={{ background: "var(--bg-panel)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-md)", width: 520, maxHeight: "80vh", overflow: "auto" }}>
            <div style={{ padding: "var(--space-lg)", borderBottom: "1px solid var(--border-subtle)", display: "flex", justifyContent: "space-between" }}>
              <div style={{ fontWeight: 700, fontSize: 15 }}>{editing?.id ? "Edit Queue" : "New Queue"}</div>
              <button onClick={() => setEditing(null)} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 18, color: "var(--text-muted)" }}>✕</button>
            </div>
            <div style={{ padding: "var(--space-lg)", display: "flex", flexDirection: "column", gap: "var(--space-md)" }}>
              <AdminField label="Name"><input value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} style={inputStyle} /></AdminField>
              <AdminField label="Description"><input value={form.description} onChange={e => setForm(f => ({ ...f, description: e.target.value }))} style={inputStyle} placeholder="What this queue is for…" /></AdminField>
              <AdminField label="Filter — Status (optional)">
                <select value={form.filter_status} onChange={e => setForm(f => ({ ...f, filter_status: e.target.value }))} style={inputStyle}>
                  <option value="">Any status</option>
                  {["new","open","in_progress","pending_external","reopened"].map(s => <option key={s} value={s}>{s}</option>)}
                </select>
                <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>Only cases with this status appear in the queue.</div>
              </AdminField>
              <AdminField label="Filter — Priority (optional)">
                <select value={form.filter_priority} onChange={e => setForm(f => ({ ...f, filter_priority: e.target.value }))} style={inputStyle}>
                  <option value="">Any priority</option>
                  {["low","medium","high","critical","blocker"].map(p => <option key={p} value={p}>{p}</option>)}
                </select>
              </AdminField>
              <AdminField label="Visible to roles">
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 4 }}>
                  {ALL_ROLES.map(r => (
                    <label key={r} style={{ display: "flex", gap: 5, alignItems: "center", fontSize: 12, cursor: "pointer", padding: "3px 8px", borderRadius: 4, border: "1px solid var(--border-default)", background: form.visible_to_roles.includes(r) ? "var(--accent-dim)" : "transparent" }}>
                      <input type="checkbox" checked={form.visible_to_roles.includes(r)} onChange={() => toggleRole(r)} /> {r}
                    </label>
                  ))}
                </div>
                <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>Leave empty = visible to everyone.</div>
              </AdminField>
              <AdminField label="Auto-assignment">
                <label style={{ display: "flex", gap: 8, alignItems: "center", cursor: "pointer", fontSize: 13 }}>
                  <input type="checkbox" checked={form.auto_assignment} onChange={e => setForm(f => ({ ...f, auto_assignment: e.target.checked }))} />
                  Automatically assign the top item to the next available worker
                </label>
              </AdminField>
              <AdminField label="Max items (optional)">
                <input type="number" value={form.max_items} onChange={e => setForm(f => ({ ...f, max_items: e.target.value }))} style={{ ...inputStyle, width: 100 }} placeholder="e.g. 50" />
              </AdminField>
              <div style={{ display: "flex", gap: "var(--space-sm)", justifyContent: "flex-end", paddingTop: "var(--space-sm)" }}>
                <Button variant="secondary" onClick={() => setEditing(null)}>Cancel</Button>
                <Button onClick={handleSave} disabled={saving || !form.name}>{saving ? "Saving…" : editing?.id ? "Save Changes" : "Create Queue"}</Button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ── Webhooks Tab ─────────────────────────────────────────────── */

function WebhooksTab() {
  const { data, loading, refetch }  = useApi(listWebhooks);
  const { data: eventsData }        = useApi(listWebhookEvents);
  const hooks        = (data ?? []) as any[];
  const allEvents    = eventsData?.events ?? [];
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({ name: "", url: "", secret: "", events: ["*"] as string[], retry_count: 3, timeout_seconds: 10 });
  const [saving, setSaving]  = useState(false);
  const [testResult, setTestResult] = useState<Record<string, any>>({});

  const toggleEvent = (e: string) => {
    if (e === "*") { setForm(f => ({ ...f, events: ["*"] })); return; }
    setForm(f => {
      const evs = f.events.filter(x => x !== "*");
      return { ...f, events: evs.includes(e) ? evs.filter(x => x !== e) : [...evs, e] };
    });
  };

  const handleCreate = async () => {
    setSaving(true);
    try {
      await createWebhook({ name: form.name, url: form.url, events: form.events, secret: form.secret || undefined, retry_count: form.retry_count, timeout_seconds: form.timeout_seconds });
      setShowCreate(false); refetch();
    } catch (e: any) { alert(e.message); }
    setSaving(false);
  };

  const handleTest = async (id: string) => {
    const r = await testWebhook(id);
    setTestResult(prev => ({ ...prev, [id]: r }));
    setTimeout(() => setTestResult(prev => { const n = { ...prev }; delete n[id]; return n; }), 5000);
  };

  return (
    <div>
      {/* Explainer */}
      <Card style={{ marginBottom: "var(--space-lg)", background: "var(--bg-elevated)" }}>
        <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.7 }}>
          <strong style={{ color: "var(--text-primary)" }}>Webhooks</strong> send an HTTP POST to your URL whenever a case event happens (case created, stage transitioned, SLA breached, etc).
          Your server receives a JSON payload and can react — update a CRM, send a Slack message, trigger a workflow in another system.
          <br />
          <strong style={{ color: "var(--text-primary)" }}>Secret</strong> — optional. If set, every request includes an <code>X-Velaris-Signature</code> HMAC-SHA256 header so you can verify the payload is genuine.
          <strong style={{ color: "var(--text-primary)" }}> Events</strong> — choose specific events or leave as <code>*</code> to receive all.
        </div>
      </Card>

      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "var(--space-lg)" }}>
        <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>{hooks.length} webhook{hooks.length !== 1 ? "s" : ""}</span>
        <Button size="sm" onClick={() => setShowCreate(true)}>+ New Webhook</Button>
      </div>

      {loading && <Spinner size={28} />}
      {!loading && hooks.length === 0 && <EmptyState title="No webhooks" description="Create a webhook to notify external systems when cases change." />}

      {hooks.map((h: any) => (
        <Card key={h.id} style={{ marginBottom: "var(--space-sm)" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)" }}>
                <span style={{ width: 8, height: 8, borderRadius: "50%", flexShrink: 0, background: h.is_active ? "var(--status-completed)" : "var(--text-muted)" }} />
                <span style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>{h.name}</span>
                {!h.is_active && <span style={{ fontSize: 10, padding: "1px 6px", borderRadius: 4, background: "var(--bg-elevated)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>disabled</span>}
              </div>
              <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 4, wordBreak: "break-all" }}>{h.url}</div>
              <div style={{ display: "flex", gap: 4, marginTop: 6, flexWrap: "wrap" }}>
                {(h.events || []).map((e: string) => (
                  <span key={e} style={{ fontSize: 9, padding: "2px 6px", borderRadius: "var(--radius-sm)", background: "var(--bg-elevated)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{e}</span>
                ))}
              </div>
              <div style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginTop: 4 }}>
                Retry: {h.retry_count}× · Timeout: {h.timeout_seconds}s
              </div>
              {testResult[h.id] && (
                <div style={{ fontSize: 11, marginTop: 6, color: testResult[h.id].success ? "var(--status-completed)" : "var(--status-failed)", fontFamily: "var(--font-mono)" }}>
                  {testResult[h.id].success ? `✓ Test OK (${testResult[h.id].status_code})` : `✗ ${testResult[h.id].error || `HTTP ${testResult[h.id].status_code}`}`}
                </div>
              )}
            </div>
            <div style={{ display: "flex", gap: 6, flexShrink: 0, marginLeft: 12 }}>
              <Button size="sm" variant="secondary" onClick={() => handleTest(h.id)}>Test</Button>
              <Button size="sm" variant="secondary" onClick={async () => { await updateWebhook(h.id, { is_active: !h.is_active }); refetch(); }}>
                {h.is_active ? "Disable" : "Enable"}
              </Button>
              <Button size="sm" variant="danger" onClick={async () => { if (confirm(`Delete "${h.name}"?`)) { await deleteWebhook(h.id); refetch(); } }}>Delete</Button>
            </div>
          </div>
        </Card>
      ))}

      {/* Create modal */}
      {showCreate && (
        <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <div style={{ background: "var(--bg-panel)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-md)", width: 560, maxHeight: "80vh", overflow: "auto" }}>
            <div style={{ padding: "var(--space-lg)", borderBottom: "1px solid var(--border-subtle)", display: "flex", justifyContent: "space-between" }}>
              <div style={{ fontWeight: 700, fontSize: 15 }}>New Webhook</div>
              <button onClick={() => setShowCreate(false)} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 18, color: "var(--text-muted)" }}>✕</button>
            </div>
            <div style={{ padding: "var(--space-lg)", display: "flex", flexDirection: "column", gap: "var(--space-md)" }}>
              <AdminField label="Name"><input value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} style={inputStyle} placeholder="e.g. Notify CRM on case creation" /></AdminField>
              <AdminField label="URL — your endpoint that receives POST requests">
                <input value={form.url} onChange={e => setForm(f => ({ ...f, url: e.target.value }))} style={inputStyle} placeholder="https://your-server.com/webhooks/velaris" />
              </AdminField>
              <AdminField label="Secret (optional — for signature verification)">
                <input value={form.secret} onChange={e => setForm(f => ({ ...f, secret: e.target.value }))} style={inputStyle} placeholder="any random string" type="password" />
                <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>We'll send X-Velaris-Signature: HMAC-SHA256(payload, secret) so you can verify authenticity.</div>
              </AdminField>
              <AdminField label="Events to subscribe to">
                <div style={{ display: "flex", flexWrap: "wrap", gap: 5, marginTop: 4 }}>
                  <label style={{ display: "flex", gap: 5, alignItems: "center", fontSize: 11, cursor: "pointer", padding: "3px 8px", borderRadius: 4, border: "1px solid var(--border-default)", background: form.events.includes("*") ? "var(--accent-dim)" : "transparent", fontFamily: "var(--font-mono)" }}>
                    <input type="checkbox" checked={form.events.includes("*")} onChange={() => toggleEvent("*")} /> * (all events)
                  </label>
                  {allEvents.filter((e: string) => e !== "*").map((e: string) => (
                    <label key={e} style={{ display: "flex", gap: 5, alignItems: "center", fontSize: 11, cursor: "pointer", padding: "3px 8px", borderRadius: 4, border: "1px solid var(--border-default)", background: form.events.includes(e) ? "var(--accent-dim)" : "transparent", fontFamily: "var(--font-mono)" }}>
                      <input type="checkbox" checked={form.events.includes(e)} onChange={() => toggleEvent(e)} /> {e}
                    </label>
                  ))}
                </div>
              </AdminField>
              <div style={{ display: "flex", gap: "var(--space-md)" }}>
                <AdminField label="Retry count" style={{ flex: 1 }}><input type="number" value={form.retry_count} onChange={e => setForm(f => ({ ...f, retry_count: parseInt(e.target.value) || 3 }))} style={{ ...inputStyle, width: "100%" }} /></AdminField>
                <AdminField label="Timeout (seconds)" style={{ flex: 1 }}><input type="number" value={form.timeout_seconds} onChange={e => setForm(f => ({ ...f, timeout_seconds: parseInt(e.target.value) || 10 }))} style={{ ...inputStyle, width: "100%" }} /></AdminField>
              </div>
              <div style={{ display: "flex", gap: "var(--space-sm)", justifyContent: "flex-end" }}>
                <Button variant="secondary" onClick={() => setShowCreate(false)}>Cancel</Button>
                <Button onClick={handleCreate} disabled={saving || !form.name || !form.url}>{saving ? "Creating…" : "Create Webhook"}</Button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ── Business Rules Tab ───────────────────────────────────────── */

const OPERATORS = ["eq","neq","gt","gte","lt","lte","in","not_in","contains","not_contains","starts_with","ends_with","is_empty","is_not_empty","matches","between"];

// Paths always available in rule context regardless of case type
const COMMON_PATHS = [
  "case.id", "case.status", "case.priority", "case.urgency_score",
  "case.current_stage_id", "case.created_by",
  "stage.id", "stage.name",
  "assignee.id", "assignee.type",
];

function useFieldPaths(caseTypeId: string, directFormIds: string[] = []): string[] {
  const [paths, setPaths] = useState<string[]>(COMMON_PATHS);
  const formIdsKey = directFormIds.join(",");

  useEffect(() => {
    if (!caseTypeId && directFormIds.length === 0) { setPaths(COMMON_PATHS); return; }
    let cancelled = false;
    (async () => {
      try {
        const dataFields: string[] = [];

        const extractFormFields = (form: any) => {
          for (const section of form?.definition_json?.sections ?? []) {
            for (const field of section.fields ?? []) {
              const key = field.field_key || field.id;
              if (key) dataFields.push(`case.data.${key}`);
            }
          }
        };

        // 1. Direct form IDs from step/stage scope: fetch those forms immediately
        if (directFormIds.length > 0) {
          const fetched = await Promise.all(directFormIds.map(id => getForm(id).catch(() => null)));
          if (cancelled) return;
          fetched.filter(Boolean).forEach(extractFormFields);
        }

        // 2. Case type data model fields + (for case_type scope) all step form references
        if (caseTypeId) {
          const ct = await getCaseType(caseTypeId) as any;
          if (cancelled) return;

          if (ct.data_model_id) {
            try {
              const dm = await getDataModel(ct.data_model_id);
              if (!cancelled) {
                (dm.definition_json?.fields ?? []).forEach((f: any) => {
                  if (f.name) dataFields.push(`case.data.${f.name}`);
                });
              }
            } catch { /* skip */ }
          }

          if (directFormIds.length === 0) {
            const allFormIds = new Set<string>();
            const allFormKeys = new Set<string>();
            for (const stage of ct.definition_json?.stages ?? []) {
              for (const step of stage.steps ?? []) {
                if (step.form_id) allFormIds.add(step.form_id);
                if (step.form_key) allFormKeys.add(step.form_key);
              }
            }
            if ((allFormIds.size > 0 || allFormKeys.size > 0) && !cancelled) {
              const byId = await Promise.all([...allFormIds].map(id => getForm(id).catch(() => null)));
              if (!cancelled) byId.filter(Boolean).forEach(extractFormFields);
              if (allFormKeys.size > 0) {
                const list = await listForms();
                const all: any[] = list.items ?? list ?? [];
                if (!cancelled) all.filter((f: any) => allFormKeys.has(f.name)).forEach(extractFormFields);
              }
            }
          }
        }

        if (!cancelled) {
          const seen = new Set<string>();
          setPaths([...COMMON_PATHS, ...dataFields].filter(p => !seen.has(p) && seen.add(p)));
        }
      } catch { if (!cancelled) setPaths(COMMON_PATHS); }
    })();
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseTypeId, formIdsKey]);

  return paths;
}
const ACTION_TYPES = ["set_value","assign_to","raise_error","send_notification","log","create_subcase"];
const RULE_TYPES = ["when","decision_table","expression","validation","routing","constraint"];
const SCOPES = ["global","case_type","stage","step"];

type RuleCondition = { field_path: string; operator: string; value: string };
type RuleAction    = { action_type: string; target: string; value: string };
type TableColumn   = { id: string; name: string; field_path: string; is_condition: boolean };
type TableRow      = { priority: number; conditions: Record<string, string>; outcomes: Record<string, string> };

function blankForm() {
  return {
    name: "", version: "1.0", rule_type: "when", scope: "global",
    scope_target_id: "", priority: 0, enabled: true,
    conditions: [] as RuleCondition[],
    actions: [] as RuleAction[],
    expression: "", result_field_path: "",
    table_columns: [] as TableColumn[],
    table_rows: [] as TableRow[],
  };
}

function ruleToForm(r: any) {
  const d = r.definition_json ?? {};
  return {
    name: r.name, version: r.version, rule_type: r.rule_type,
    scope: r.scope, scope_target_id: r.scope_target_id ?? "",
    priority: r.priority, enabled: r.enabled,
    conditions: (d.conditions ?? []).map((c: any) => ({
      field_path: c.field_path ?? "", operator: c.operator ?? "eq", value: String(c.value ?? ""),
    })),
    actions: (d.actions ?? []).map((a: any) => ({
      action_type: a.action_type ?? "set_value", target: a.target ?? "", value: String(a.value ?? ""),
    })),
    expression: d.expression ?? "",
    result_field_path: d.result_field_path ?? "",
    table_columns: (d.table_columns ?? []).map((c: any) => ({
      id: c.id, name: c.name ?? c.id, field_path: c.field_path ?? "", is_condition: c.is_condition !== false,
    })),
    table_rows: (d.table_rows ?? []).map((row: any) => ({
      priority: row.priority ?? 0,
      conditions: row.conditions ?? {},
      outcomes: row.outcomes ?? {},
    })),
  };
}

function formToDefinition(form: ReturnType<typeof blankForm>): Record<string, any> {
  const rt = form.rule_type;
  if (rt === "expression" || rt === "declare_expression") {
    return { expression: form.expression, result_field_path: form.result_field_path || undefined };
  }
  if (rt === "decision_table" || rt === "decision_tree") {
    return {
      table_columns: form.table_columns.map(c => ({ id: c.id, name: c.name, field_path: c.field_path, is_condition: c.is_condition })),
      table_rows: form.table_rows.map(row => ({ priority: row.priority, conditions: row.conditions, outcomes: row.outcomes })),
    };
  }
  const def: Record<string, any> = {
    conditions: form.conditions.map(c => ({
      field_path: c.field_path,
      operator: c.operator,
      value: c.value === "" ? undefined : (isNaN(Number(c.value)) ? c.value : Number(c.value)),
    })),
  };
  if (rt === "when" || rt === "routing") {
    def.actions = form.actions.map(a => ({ action_type: a.action_type, target: a.target || undefined, value: a.value || undefined }));
  }
  return def;
}

function RulesTab() {
  const { data, loading, refetch } = useApi(listRules);
  const rules = data?.items ?? [];
  const [editing, setEditing] = useState<any | null>(null); // null=closed, "new"=new, rule=edit
  const [form, setForm] = useState(blankForm());
  const [saving, setSaving] = useState(false);
  const { options: scopeOptions, loading: scopeLoading, caseTypeMap } = useScopeOptions(form.scope);
  const [caseTypeIdForFields, setCaseTypeIdForFields] = useState("");
  const [formIdsForFields, setFormIdsForFields] = useState<string[]>([]);
  const fieldPaths = useFieldPaths(caseTypeIdForFields, formIdsForFields);

  // Reset field paths when scope changes
  useEffect(() => {
    if (form.scope === "global") { setCaseTypeIdForFields(""); setFormIdsForFields([]); }
  }, [form.scope]);

  const openNew = () => { setForm(blankForm()); setCaseTypeIdForFields(""); setFormIdsForFields([]); setEditing("new"); };
  const openEdit = (r: any) => {
    setForm(ruleToForm(r));
    setCaseTypeIdForFields(r.scope === "case_type" ? (r.scope_target_id ?? "") : "");
    setFormIdsForFields([]);
    setEditing(r);
  };
  const close = () => { setEditing(null); setSaving(false); };

  const handleSave = async () => {
    if (!form.name.trim()) return;
    setSaving(true);
    try {
      const definition_json = formToDefinition(form);
      if (editing === "new") {
        await createRule({ name: form.name, version: form.version, rule_type: form.rule_type, scope: form.scope, scope_target_id: form.scope_target_id || null, definition_json, enabled: form.enabled, priority: form.priority });
      } else {
        await updateRule(editing.id, { definition_json, enabled: form.enabled, priority: form.priority });
      }
      close(); refetch();
    } catch (e: any) { alert(e.message ?? "Save failed"); }
    setSaving(false);
  };

  const handleToggle = async (r: any) => {
    try { await updateRule(r.id, { enabled: !r.enabled }); refetch(); }
    catch (e: any) { alert(e.message); }
  };

  const isNew = editing === "new";

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-lg)" }}>
        <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>{rules.length} business rule(s)</span>
        <Button size="sm" onClick={openNew}>+ New Business Rule</Button>
      </div>

      {loading && <Spinner size={28} />}
      {!loading && rules.length === 0 && (
        <EmptyState title="No business rules" description="Business rules drive assignment routing, workflow decisions, validations, and escalations." />
      )}

      {rules.map((r: any) => (
        <Card key={r.id} style={{ marginBottom: "var(--space-sm)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)" }}>
              <span style={{ width: 8, height: 8, borderRadius: "50%", background: r.enabled ? "var(--status-completed)" : "var(--status-cancelled)", flexShrink: 0 }} />
              <span style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>{r.name}</span>
              <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", padding: "1px 6px", background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)" }}>
                {r.rule_type}
              </span>
              <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", padding: "1px 6px", background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)" }}>
                {r.scope}
              </span>
            </div>
            <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 2 }}>
              v{r.version} · priority: {r.priority}
              {r.scope_target_id && ` · target: ${r.scope_target_id.slice(0, 8)}`}
              {" · "}
              <span style={{ color: r.enabled ? "var(--status-completed)" : "var(--text-muted)" }}>{r.enabled ? "enabled" : "disabled"}</span>
            </div>
          </div>
          <div style={{ display: "flex", gap: "var(--space-xs)", alignItems: "center" }}>
            <Button size="sm" variant="ghost" onClick={() => handleToggle(r)}>{r.enabled ? "Disable" : "Enable"}</Button>
            <Button size="sm" variant="secondary" onClick={() => openEdit(r)}>Edit</Button>
            <Button size="sm" variant="danger" onClick={async () => {
              if (confirm(`Delete business rule "${r.name}"?`)) { await deleteRule(r.id); refetch(); }
            }}>Delete</Button>
          </div>
        </Card>
      ))}

      {/* Drawer */}
      {editing !== null && (
        <>
          <div onClick={close} style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.45)", zIndex: 1000 }} />
          <div style={{
            position: "fixed", top: 0, right: 0, bottom: 0, width: 600,
            background: "var(--bg-panel)", borderLeft: "1px solid var(--border-default)",
            zIndex: 1001, display: "flex", flexDirection: "column", overflow: "hidden",
          }}>
            {/* Header */}
            <div style={{ padding: "var(--space-lg)", borderBottom: "1px solid var(--border-subtle)", display: "flex", justifyContent: "space-between", alignItems: "center", flexShrink: 0 }}>
              <div style={{ fontWeight: 700, fontSize: 15 }}>{isNew ? "New Business Rule" : `Edit: ${editing.name}`}</div>
              <button onClick={close} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 20, color: "var(--text-muted)", lineHeight: 1 }}>✕</button>
            </div>

            {/* Body */}
            <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-lg)", display: "flex", flexDirection: "column", gap: "var(--space-lg)" }}>

              {/* Metadata */}
              <section>
                <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "var(--font-mono)", marginBottom: "var(--space-md)" }}>Metadata</div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-md)" }}>
                  <AdminField label="Name" style={{ gridColumn: "span 2" }}>
                    <input value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} style={inputStyle} placeholder="e.g. high-value-escalation" disabled={!isNew} />
                  </AdminField>
                  <AdminField label="Version">
                    <input value={form.version} onChange={e => setForm(f => ({ ...f, version: e.target.value }))} style={inputStyle} placeholder="1.0" disabled={!isNew} />
                  </AdminField>
                  <AdminField label="Priority">
                    <input type="number" value={form.priority} onChange={e => setForm(f => ({ ...f, priority: Number(e.target.value) }))} style={inputStyle} />
                  </AdminField>
                  {isNew && <>
                    <AdminField label="Rule Type">
                      <select value={form.rule_type} onChange={e => setForm(f => ({ ...f, rule_type: e.target.value }))} style={inputStyle}>
                        {RULE_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
                      </select>
                    </AdminField>
                    <AdminField label="Scope">
                      <select value={form.scope} onChange={e => setForm(f => ({ ...f, scope: e.target.value }))} style={inputStyle}>
                        {SCOPES.map(s => <option key={s} value={s}>{s}</option>)}
                      </select>
                    </AdminField>
                    {form.scope !== "global" && (
                      <AdminField label="Scope Target" style={{ gridColumn: "span 2" }}>
                        <ScopeTargetInput
                          scope={form.scope}
                          value={form.scope_target_id}
                          onChange={(v, resolvedCaseTypeId, resolvedFormIds) => {
                            setForm(f => ({ ...f, scope_target_id: v }));
                            setCaseTypeIdForFields(resolvedCaseTypeId);
                            setFormIdsForFields(resolvedFormIds);
                          }}
                          options={scopeOptions}
                          loading={scopeLoading}
                        />
                      </AdminField>
                    )}
                  </>}
                  <AdminField label="Enabled" style={{ gridColumn: "span 2" }}>
                    <label style={{ display: "flex", gap: 8, alignItems: "center", cursor: "pointer", fontSize: 13, color: "var(--text-primary)" }}>
                      <input type="checkbox" checked={form.enabled} onChange={e => setForm(f => ({ ...f, enabled: e.target.checked }))} style={{ accentColor: "var(--accent)", width: 15, height: 15 }} />
                      Rule is active
                    </label>
                  </AdminField>
                </div>
              </section>

              {/* Definition — varies by type */}
              <RuleDefinitionEditor form={form} setForm={setForm} fieldPaths={fieldPaths} />
            </div>

            {/* Footer */}
            <div style={{ padding: "var(--space-md) var(--space-lg)", borderTop: "1px solid var(--border-subtle)", display: "flex", gap: "var(--space-sm)", justifyContent: "flex-end", flexShrink: 0 }}>
              <Button variant="secondary" onClick={close}>Cancel</Button>
              <Button onClick={handleSave} disabled={saving || !form.name.trim()}>{saving ? "Saving…" : isNew ? "Create Rule" : "Save Changes"}</Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

/* ── Rule Definition Editor (per type) ────────────────────────── */

function RuleDefinitionEditor({ form, setForm, fieldPaths }: { form: ReturnType<typeof blankForm>; setForm: React.Dispatch<React.SetStateAction<ReturnType<typeof blankForm>>>; fieldPaths: string[] }) {
  const rt = form.rule_type;

  if (rt === "expression" || rt === "declare_expression") {
    return (
      <section>
        <SectionTitle>Expression</SectionTitle>
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-md)" }}>
          <AdminField label="Expression">
            <input value={form.expression} onChange={e => setForm(f => ({ ...f, expression: e.target.value }))} style={inputStyle} placeholder="e.g. case_data_amount * 0.1" />
            <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 3 }}>Dot paths flatten to underscores: case.data.amount → case_data_amount. Safe builtins: abs, min, max, round, len, sum.</div>
          </AdminField>
          <AdminField label="Store result at field path (optional)">
            <FieldPathInput value={form.result_field_path} onChange={v => setForm(f => ({ ...f, result_field_path: v }))} fieldPaths={fieldPaths} placeholder="e.g. case.data.fee" />
          </AdminField>
        </div>
      </section>
    );
  }

  if (rt === "decision_table" || rt === "decision_tree") {
    return <DecisionTableEditor form={form} setForm={setForm} fieldPaths={fieldPaths} />;
  }

  return (
    <>
      <ConditionBuilder
        conditions={form.conditions}
        onChange={conditions => setForm(f => ({ ...f, conditions }))}
        fieldPaths={fieldPaths}
      />
      {(rt === "when" || rt === "routing") && (
        <ActionBuilder
          actions={form.actions}
          onChange={actions => setForm(f => ({ ...f, actions }))}
          fieldPaths={fieldPaths}
        />
      )}
      {rt === "constraint" && (
        <div style={{ fontSize: 11, color: "var(--text-muted)", padding: "var(--space-sm)", background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)" }}>
          Constraint rules return <code>holds: true/false</code>. All conditions must pass for the constraint to hold.
        </div>
      )}
    </>
  );
}

/* ── Scope Target Input ───────────────────────────────────────── */

type ScopeOption = { id: string; label: string; caseTypeId?: string; formIds?: string[] };

/** Keep only the highest-version case type per name. */
function latestVersions(cts: any[]): any[] {
  const map = new Map<string, any>();
  for (const ct of cts) {
    const prev = map.get(ct.name);
    if (!prev || compareVersions(ct.version, prev.version) > 0) map.set(ct.name, ct);
  }
  return [...map.values()];
}

function compareVersions(a: string, b: string): number {
  const pa = (a ?? "0").split(".").map(Number);
  const pb = (b ?? "0").split(".").map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const diff = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (diff !== 0) return diff;
  }
  return 0;
}

function useScopeOptions(scope: string): { options: ScopeOption[]; loading: boolean; caseTypeMap: Record<string, string> } {
  const [options, setOptions] = useState<ScopeOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [caseTypeMap, setCaseTypeMap] = useState<Record<string, string>>({});

  useEffect(() => {
    if (scope === "global") { setOptions([]); setCaseTypeMap({}); return; }
    let cancelled = false;
    setLoading(true);
    (async () => {
      try {
        // Fetch all pages
        const res = await listCaseTypes(1);
        const all: any[] = res.items ?? [];
        for (let p = 2; p <= Math.ceil((res.total ?? all.length) / 50); p++) {
          const page = await listCaseTypes(p);
          all.push(...(page.items ?? []));
        }
        // Only the highest version of each case type
        const cts = latestVersions(all);

        if (scope === "case_type") {
          if (!cancelled) {
            setOptions(cts.map((ct: any) => ({ id: ct.id, label: `${ct.name} (v${ct.version})` })));
            setCaseTypeMap(Object.fromEntries(cts.map((ct: any) => [ct.id, ct.id])));
          }
        } else if (scope === "stage" || scope === "step") {
          const opts: ScopeOption[] = [];
          const map: Record<string, string> = {};
          for (const ct of cts) {
            for (const stage of (ct.definition_json?.stages ?? [])) {
              if (scope === "stage") {
                const stageFormIds = (stage.steps ?? []).map((s: any) => s.form_id).filter(Boolean);
                opts.push({ id: stage.id, label: `${ct.name} › ${stage.name ?? stage.id}`, caseTypeId: ct.id, formIds: stageFormIds });
                map[stage.id] = ct.id;
              } else {
                for (const step of stage.steps ?? []) {
                  const stepFormIds: string[] = step.form_id ? [step.form_id] : [];
                  opts.push({ id: step.id, label: `${ct.name} › ${stage.name ?? stage.id} › ${step.name ?? step.id}`, caseTypeId: ct.id, formIds: stepFormIds });
                  map[step.id] = ct.id;
                }
              }
            }
          }
          if (!cancelled) { setOptions(opts); setCaseTypeMap(map); }
        }
      } catch { if (!cancelled) { setOptions([]); setCaseTypeMap({}); } }
      if (!cancelled) setLoading(false);
    })();
    return () => { cancelled = true; };
  }, [scope]);

  return { options, loading, caseTypeMap };
}

function ScopeTargetInput({ scope, value, onChange, options, loading }: {
  scope: string; value: string;
  onChange: (v: string, resolvedCaseTypeId: string, resolvedFormIds: string[]) => void;
  options: ScopeOption[]; loading: boolean;
}) {
  const listId = React.useId();
  const matched = options.find(o => o.id === value);

  const resolve = (v: string): { caseTypeId: string; formIds: string[] } => {
    if (!v) return { caseTypeId: "", formIds: [] };
    if (scope === "case_type") return { caseTypeId: v, formIds: [] };
    const opt = options.find(o => o.id === v);
    return { caseTypeId: opt?.caseTypeId ?? "", formIds: opt?.formIds ?? [] };
  };

  const handleChange = (v: string) => {
    const { caseTypeId, formIds } = resolve(v);
    onChange(v, caseTypeId, formIds);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
        <input
          value={value}
          onChange={e => handleChange(e.target.value)}
          list={listId}
          style={inputStyle}
          placeholder={loading ? "Loading…" : `Select or paste ${scope} ID`}
          autoComplete="off"
          spellCheck={false}
        />
        <datalist id={listId}>
          {options.map(o => <option key={o.id} value={o.id}>{(o as any).label}</option>)}
        </datalist>
      </div>
      {matched && (
        <div style={{ fontSize: 11, color: "var(--text-secondary)", paddingLeft: 2 }}>
          {(matched as any).label}
        </div>
      )}
      {!matched && value && options.length > 0 && (
        <div style={{ fontSize: 11, color: "var(--status-failed)", paddingLeft: 2 }}>
          ID not found in loaded options — double-check or paste directly.
        </div>
      )}
    </div>
  );
}

/* ── Field Path Input (input + datalist) ─────────────────────── */

function FieldPathInput({ value, onChange, fieldPaths, placeholder, style }: {
  value: string; onChange: (v: string) => void;
  fieldPaths: string[]; placeholder?: string; style?: React.CSSProperties;
}) {
  const listId = React.useId();
  return (
    <>
      <input
        value={value}
        onChange={e => onChange(e.target.value)}
        list={listId}
        style={{ ...inputStyle, fontSize: 12, ...style }}
        placeholder={placeholder ?? "field path"}
        autoComplete="off"
      />
      <datalist id={listId}>
        {fieldPaths.map(p => <option key={p} value={p} />)}
      </datalist>
    </>
  );
}

/* ── Condition Builder ────────────────────────────────────────── */

function ConditionBuilder({ conditions, onChange, fieldPaths }: { conditions: RuleCondition[]; onChange: (c: RuleCondition[]) => void; fieldPaths: string[] }) {
  const add = () => onChange([...conditions, { field_path: "", operator: "eq", value: "" }]);
  const remove = (i: number) => onChange(conditions.filter((_, idx) => idx !== i));
  const update = (i: number, patch: Partial<RuleCondition>) =>
    onChange(conditions.map((c, idx) => idx === i ? { ...c, ...patch } : c));

  return (
    <section>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-sm)" }}>
        <SectionTitle style={{ margin: 0 }}>Conditions <span style={{ fontWeight: 400, color: "var(--text-muted)" }}>(all must match)</span></SectionTitle>
        <button onClick={add} style={{ fontSize: 11, color: "var(--accent)", background: "none", border: "1px solid var(--accent)", borderRadius: "var(--radius-sm)", padding: "3px 10px", cursor: "pointer" }}>+ Add</button>
      </div>
      {conditions.length === 0 && (
        <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "var(--space-sm)", textAlign: "center", border: "1px dashed var(--border-subtle)", borderRadius: "var(--radius-sm)" }}>
          No conditions — rule always matches
        </div>
      )}
      {conditions.map((c, i) => (
        <div key={i} style={{ display: "grid", gridTemplateColumns: "1fr 130px 1fr 28px", gap: 6, alignItems: "center", marginBottom: 6 }}>
          <FieldPathInput value={c.field_path} onChange={v => update(i, { field_path: v })} fieldPaths={fieldPaths} placeholder="field path (e.g. case.data.amount)" />
          <select value={c.operator} onChange={e => update(i, { operator: e.target.value })} style={{ ...selectStyle, fontSize: 12 }}>
            {OPERATORS.map(op => <option key={op} value={op}>{op}</option>)}
          </select>
          {["is_empty", "is_not_empty"].includes(c.operator)
            ? <div style={{ fontSize: 11, color: "var(--text-muted)" }}>(no value needed)</div>
            : <input value={c.value} onChange={e => update(i, { value: e.target.value })} style={{ ...inputStyle, fontSize: 12 }} placeholder="value" />
          }
          <button onClick={() => remove(i)} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--status-failed)", fontSize: 16, padding: 0, lineHeight: 1 }}>×</button>
        </div>
      ))}
    </section>
  );
}

/* ── Action Builder ───────────────────────────────────────────── */

function ActionBuilder({ actions, onChange, fieldPaths }: { actions: RuleAction[]; onChange: (a: RuleAction[]) => void; fieldPaths: string[] }) {
  const add = () => onChange([...actions, { action_type: "set_value", target: "", value: "" }]);
  const remove = (i: number) => onChange(actions.filter((_, idx) => idx !== i));
  const update = (i: number, patch: Partial<RuleAction>) =>
    onChange(actions.map((a, idx) => idx === i ? { ...a, ...patch } : a));

  return (
    <section>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-sm)" }}>
        <SectionTitle style={{ margin: 0 }}>Actions <span style={{ fontWeight: 400, color: "var(--text-muted)" }}>(fired when conditions match)</span></SectionTitle>
        <button onClick={add} style={{ fontSize: 11, color: "var(--accent)", background: "none", border: "1px solid var(--accent)", borderRadius: "var(--radius-sm)", padding: "3px 10px", cursor: "pointer" }}>+ Add</button>
      </div>
      {actions.length === 0 && (
        <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "var(--space-sm)", textAlign: "center", border: "1px dashed var(--border-subtle)", borderRadius: "var(--radius-sm)" }}>
          No actions defined
        </div>
      )}
      {actions.map((a, i) => (
        <div key={i} style={{ display: "grid", gridTemplateColumns: "140px 1fr 1fr 28px", gap: 6, alignItems: "center", marginBottom: 6 }}>
          <select value={a.action_type} onChange={e => update(i, { action_type: e.target.value })} style={{ ...selectStyle, fontSize: 12 }}>
            {ACTION_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
          {a.action_type === "set_value"
            ? <FieldPathInput value={a.target} onChange={v => update(i, { target: v })} fieldPaths={fieldPaths} placeholder="target field path" />
            : <input value={a.target} onChange={e => update(i, { target: e.target.value })} style={{ ...inputStyle, fontSize: 12 }} placeholder={a.action_type === "assign_to" ? "user / role / queue" : "target"} />
          }
          {a.action_type === "log"
            ? <input value={a.value} onChange={e => update(i, { value: e.target.value })} style={{ ...inputStyle, fontSize: 12 }} placeholder="log message" />
            : a.action_type === "send_notification"
            ? <input value={a.value} onChange={e => update(i, { value: e.target.value })} style={{ ...inputStyle, fontSize: 12 }} placeholder="recipient / template" />
            : <input value={a.value} onChange={e => update(i, { value: e.target.value })} style={{ ...inputStyle, fontSize: 12 }} placeholder="value" />
          }
          <button onClick={() => remove(i)} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--status-failed)", fontSize: 16, padding: 0, lineHeight: 1 }}>×</button>
        </div>
      ))}
      {actions.some(a => a.action_type === "assign_to") && (
        <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
          assign_to: target = assignee type (user / role / queue), value = assignee ID.
        </div>
      )}
    </section>
  );
}

/* ── Decision Table Editor ────────────────────────────────────── */

function DecisionTableEditor({ form, setForm, fieldPaths }: { form: ReturnType<typeof blankForm>; setForm: React.Dispatch<React.SetStateAction<ReturnType<typeof blankForm>>>; fieldPaths: string[] }) {
  const cols = form.table_columns;
  const rows = form.table_rows;

  const addCol = (isCondition: boolean) => {
    const id = `col_${Date.now()}`;
    setForm(f => ({
      ...f,
      table_columns: [...f.table_columns, { id, name: "", field_path: "", is_condition: isCondition }],
    }));
  };

  const removeCol = (colId: string) => {
    setForm(f => ({
      ...f,
      table_columns: f.table_columns.filter(c => c.id !== colId),
      table_rows: f.table_rows.map(row => {
        const { [colId]: _c, ...conds } = row.conditions;
        const { [colId]: _o, ...outs } = row.outcomes;
        return { ...row, conditions: conds, outcomes: outs };
      }),
    }));
  };

  const updateCol = (colId: string, patch: Partial<TableColumn>) =>
    setForm(f => ({ ...f, table_columns: f.table_columns.map(c => c.id === colId ? { ...c, ...patch } : c) }));

  const addRow = () =>
    setForm(f => ({ ...f, table_rows: [...f.table_rows, { priority: f.table_rows.length * 10, conditions: {}, outcomes: {} }] }));

  const removeRow = (i: number) =>
    setForm(f => ({ ...f, table_rows: f.table_rows.filter((_, idx) => idx !== i) }));

  const updateCell = (rowIdx: number, colId: string, value: string, isCondition: boolean) =>
    setForm(f => ({
      ...f,
      table_rows: f.table_rows.map((row, idx) => {
        if (idx !== rowIdx) return row;
        if (isCondition) return { ...row, conditions: { ...row.conditions, [colId]: value } };
        return { ...row, outcomes: { ...row.outcomes, [colId]: value } };
      }),
    }));

  const condCols = cols.filter(c => c.is_condition);
  const outCols  = cols.filter(c => !c.is_condition);

  return (
    <section>
      <SectionTitle>Decision Table</SectionTitle>

      {/* Column definitions */}
      <div style={{ marginBottom: "var(--space-md)" }}>
        <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6 }}>Define columns, then fill in rows below.</div>
        {cols.map(col => (
          <div key={col.id} style={{ display: "grid", gridTemplateColumns: "80px 1fr 1fr 28px", gap: 6, alignItems: "center", marginBottom: 6 }}>
            <span style={{ fontSize: 10, padding: "3px 6px", borderRadius: 4, background: col.is_condition ? "var(--accent-dim)" : "var(--bg-elevated)", color: col.is_condition ? "var(--accent)" : "var(--text-muted)", textAlign: "center", fontFamily: "var(--font-mono)" }}>
              {col.is_condition ? "condition" : "outcome"}
            </span>
            <input value={col.name} onChange={e => updateCol(col.id, { name: e.target.value })} style={{ ...inputStyle, fontSize: 12 }} placeholder="Column name" />
            <FieldPathInput value={col.field_path} onChange={v => updateCol(col.id, { field_path: v })} fieldPaths={fieldPaths} placeholder="field path (e.g. case.data.region)" />
            <button onClick={() => removeCol(col.id)} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--status-failed)", fontSize: 16, padding: 0, lineHeight: 1 }}>×</button>
          </div>
        ))}
        <div style={{ display: "flex", gap: "var(--space-sm)", marginTop: "var(--space-sm)" }}>
          <button onClick={() => addCol(true)} style={{ fontSize: 11, color: "var(--accent)", background: "none", border: "1px solid var(--accent)", borderRadius: "var(--radius-sm)", padding: "3px 10px", cursor: "pointer" }}>+ Condition Column</button>
          <button onClick={() => addCol(false)} style={{ fontSize: 11, color: "var(--text-secondary)", background: "none", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", padding: "3px 10px", cursor: "pointer" }}>+ Outcome Column</button>
        </div>
      </div>

      {/* Row grid */}
      {cols.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr>
                <th style={{ ...thStyle, width: 60 }}>Priority</th>
                {condCols.map(c => <th key={c.id} style={{ ...thStyle, color: "var(--accent)" }}>{c.name || c.id}</th>)}
                {outCols.map(c => <th key={c.id} style={{ ...thStyle, color: "var(--text-secondary)" }}>{c.name || c.id}</th>)}
                <th style={{ ...thStyle, width: 28 }} />
              </tr>
            </thead>
            <tbody>
              {rows.map((row, ri) => (
                <tr key={ri}>
                  <td style={tdStyle}>
                    <input type="number" value={row.priority} onChange={e => setForm(f => ({ ...f, table_rows: f.table_rows.map((r, i) => i === ri ? { ...r, priority: Number(e.target.value) } : r) }))} style={{ ...inputStyle, fontSize: 11, padding: "4px 6px" }} />
                  </td>
                  {condCols.map(c => (
                    <td key={c.id} style={tdStyle}>
                      <input value={row.conditions[c.id] ?? ""} onChange={e => updateCell(ri, c.id, e.target.value, true)} style={{ ...inputStyle, fontSize: 11, padding: "4px 6px" }} placeholder="value or {$gte:100}" />
                    </td>
                  ))}
                  {outCols.map(c => (
                    <td key={c.id} style={tdStyle}>
                      <input value={row.outcomes[c.id] ?? ""} onChange={e => updateCell(ri, c.id, e.target.value, false)} style={{ ...inputStyle, fontSize: 11, padding: "4px 6px" }} />
                    </td>
                  ))}
                  <td style={tdStyle}>
                    <button onClick={() => removeRow(ri)} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--status-failed)", fontSize: 16, padding: 0, lineHeight: 1 }}>×</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <button onClick={addRow} style={{ marginTop: 8, fontSize: 11, color: "var(--text-secondary)", background: "none", border: "1px dashed var(--border-subtle)", borderRadius: "var(--radius-sm)", padding: "4px 12px", cursor: "pointer", width: "100%" }}>+ Add Row</button>
        </div>
      )}
    </section>
  );
}

const thStyle: React.CSSProperties = { padding: "6px 8px", textAlign: "left", fontFamily: "var(--font-mono)", fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.05em", color: "var(--text-muted)", borderBottom: "1px solid var(--border-default)", background: "var(--bg-elevated)" };
const tdStyle: React.CSSProperties = { padding: 4, borderBottom: "1px solid var(--border-subtle)", verticalAlign: "middle" };

function SectionTitle({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "var(--font-mono)", marginBottom: "var(--space-sm)", ...style }}>
      {children}
    </div>
  );
}

/* ── Calendars Tab ────────────────────────────────────────────── */

const DAY_NAMES = ["", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const ALL_DAYS  = [1, 2, 3, 4, 5, 6, 7];
const TZ_GROUPS: Array<{ label: string; zones: string[] }> = [
  { label: "UTC", zones: ["UTC"] },
  { label: "Africa", zones: [
    "Africa/Abidjan","Africa/Accra","Africa/Addis_Ababa","Africa/Algiers","Africa/Cairo",
    "Africa/Casablanca","Africa/Johannesburg","Africa/Lagos","Africa/Nairobi","Africa/Tripoli","Africa/Tunis",
  ]},
  { label: "America", zones: [
    "America/Anchorage","America/Bogota","America/Buenos_Aires","America/Caracas",
    "America/Chicago","America/Costa_Rica","America/Denver","America/El_Salvador",
    "America/Guatemala","America/Guayaquil","America/Halifax","America/Indiana/Indianapolis",
    "America/La_Paz","America/Lima","America/Los_Angeles","America/Manaus",
    "America/Mexico_City","America/Monterrey","America/New_York","America/Panama",
    "America/Phoenix","America/Puerto_Rico","America/Santiago","America/Sao_Paulo",
    "America/St_Johns","America/Toronto","America/Vancouver",
  ]},
  { label: "Asia", zones: [
    "Asia/Almaty","Asia/Amman","Asia/Baghdad","Asia/Baku","Asia/Bangkok",
    "Asia/Beirut","Asia/Colombo","Asia/Dhaka","Asia/Dubai","Asia/Hong_Kong",
    "Asia/Irkutsk","Asia/Jakarta","Asia/Jerusalem","Asia/Kabul","Asia/Karachi",
    "Asia/Kathmandu","Asia/Kolkata","Asia/Krasnoyarsk","Asia/Kuala_Lumpur",
    "Asia/Kuwait","Asia/Macau","Asia/Magadan","Asia/Manila","Asia/Muscat",
    "Asia/Nicosia","Asia/Novosibirsk","Asia/Phnom_Penh","Asia/Rangoon",
    "Asia/Riyadh","Asia/Seoul","Asia/Shanghai","Asia/Singapore","Asia/Taipei",
    "Asia/Tashkent","Asia/Tbilisi","Asia/Tehran","Asia/Tokyo","Asia/Ulaanbaatar",
    "Asia/Vladivostok","Asia/Yakutsk","Asia/Yekaterinburg","Asia/Yerevan",
  ]},
  { label: "Atlantic", zones: [
    "Atlantic/Azores","Atlantic/Cape_Verde","Atlantic/Reykjavik",
  ]},
  { label: "Australia", zones: [
    "Australia/Adelaide","Australia/Brisbane","Australia/Darwin",
    "Australia/Hobart","Australia/Lord_Howe","Australia/Melbourne",
    "Australia/Perth","Australia/Sydney",
  ]},
  { label: "Europe", zones: [
    "Europe/Amsterdam","Europe/Athens","Europe/Belgrade","Europe/Berlin",
    "Europe/Brussels","Europe/Bucharest","Europe/Budapest","Europe/Copenhagen",
    "Europe/Dublin","Europe/Helsinki","Europe/Istanbul","Europe/Kiev",
    "Europe/Lisbon","Europe/London","Europe/Luxembourg","Europe/Madrid",
    "Europe/Minsk","Europe/Monaco","Europe/Moscow","Europe/Oslo",
    "Europe/Paris","Europe/Prague","Europe/Rome","Europe/Samara","Europe/Sofia",
    "Europe/Stockholm","Europe/Tallinn","Europe/Vienna","Europe/Vilnius",
    "Europe/Warsaw","Europe/Zurich",
  ]},
  { label: "Pacific", zones: [
    "Pacific/Auckland","Pacific/Chatham","Pacific/Fiji","Pacific/Guam",
    "Pacific/Honolulu","Pacific/Midway","Pacific/Noumea","Pacific/Pago_Pago",
    "Pacific/Port_Moresby","Pacific/Tongatapu",
  ]},
];

function CalendarsTab() {
  const { data, loading, refetch } = useApi(listCalendars);
  const calendars = (data ?? []) as any[];
  const [editing, setEditing] = useState<any | null>(null);
  const [form, setForm] = useState({ name: "", timezone: "UTC", work_days: [1,2,3,4,5], work_start_hour: 9, work_end_hour: 17, description: "" });
  const [saving, setSaving] = useState(false);

  const openNew = () => {
    setForm({ name: "", timezone: "UTC", work_days: [1,2,3,4,5], work_start_hour: 9, work_end_hour: 17, description: "" });
    setEditing({});
  };
  const openEdit = (c: any) => {
    setForm({ name: c.name, timezone: c.timezone, work_days: c.work_days || [1,2,3,4,5], work_start_hour: c.work_start_hour ?? 9, work_end_hour: c.work_end_hour ?? 17, description: c.description || "" });
    setEditing(c);
  };

  const toggleDay = (d: number) => setForm(f => ({ ...f, work_days: f.work_days.includes(d) ? f.work_days.filter(x => x !== d) : [...f.work_days, d].sort() }));

  const handleSave = async () => {
    setSaving(true);
    try {
      if (editing?.id) await updateCalendar(editing.id, form);
      else await createCalendar(form as any);
      setEditing(null); refetch();
    } catch (e: any) { alert(e.message); }
    setSaving(false);
  };

  return (
    <div>
      {/* Explainer */}
      <Card style={{ marginBottom: "var(--space-lg)", background: "var(--bg-elevated)" }}>
        <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.7 }}>
          <strong style={{ color: "var(--text-primary)" }}>Business Calendars</strong> define what counts as a "working hour" for SLA calculations.
          When an SLA says "resolve within 8 business hours", it counts only the hours within the calendar's work window — weekends and holidays don't count.
          <br />
          <strong style={{ color: "var(--text-primary)" }}>Not location-based automatically</strong> — you set the timezone explicitly. Create one calendar per timezone/region if your team spans multiple locations (e.g. "India Office: Asia/Kolkata, 9am–6pm Mon–Sat" and "London Office: Europe/London, 9am–5pm Mon–Fri").
          Each SLA policy in the Case Designer can be linked to a specific calendar.
        </div>
      </Card>

      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "var(--space-lg)" }}>
        <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>{calendars.length} calendar{calendars.length !== 1 ? "s" : ""}</span>
        <Button size="sm" onClick={openNew}>+ New Calendar</Button>
      </div>

      {loading && <Spinner size={28} />}
      {!loading && calendars.length === 0 && <EmptyState title="No calendars" description="Create at least one calendar so SLAs can calculate business hours correctly." />}

      {calendars.map((c: any) => (
        <Card key={c.id} style={{ marginBottom: "var(--space-sm)" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
            <div>
              <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>{c.name}</div>
              <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 4 }}>
                {c.timezone} · {c.work_start_hour}:00–{c.work_end_hour}:00
              </div>
              <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 4, display: "flex", gap: 4 }}>
                {ALL_DAYS.map(d => (
                  <span key={d} style={{ padding: "1px 5px", borderRadius: 3, background: (c.work_days || []).includes(d) ? "var(--accent-dim)" : "var(--bg-elevated)", color: (c.work_days || []).includes(d) ? "var(--accent)" : "var(--text-muted)" }}>
                    {DAY_NAMES[d]}
                  </span>
                ))}
              </div>
              {c.description && <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>{c.description}</div>}
            </div>
            <Button size="sm" variant="secondary" onClick={() => openEdit(c)}>Edit</Button>
          </div>
        </Card>
      ))}

      {/* Create/Edit modal */}
      {editing !== null && (
        <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <div style={{ background: "var(--bg-panel)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-md)", width: 480, maxHeight: "80vh", overflow: "auto" }}>
            <div style={{ padding: "var(--space-lg)", borderBottom: "1px solid var(--border-subtle)", display: "flex", justifyContent: "space-between" }}>
              <div style={{ fontWeight: 700, fontSize: 15 }}>{editing?.id ? "Edit Calendar" : "New Calendar"}</div>
              <button onClick={() => setEditing(null)} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 18, color: "var(--text-muted)" }}>✕</button>
            </div>
            <div style={{ padding: "var(--space-lg)", display: "flex", flexDirection: "column", gap: "var(--space-md)" }}>
              <AdminField label="Name"><input value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} style={inputStyle} placeholder="e.g. India Office" /></AdminField>
              <AdminField label="Timezone">
                <select value={form.timezone} onChange={e => setForm(f => ({ ...f, timezone: e.target.value }))} style={inputStyle}>
                  {TZ_GROUPS.map(g => (
                    <optgroup key={g.label} label={g.label}>
                      {g.zones.map(tz => <option key={tz} value={tz}>{tz}</option>)}
                    </optgroup>
                  ))}
                </select>
                <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>Determines when "working hours" start and end. SLA clocks run in this timezone.</div>
              </AdminField>
              <AdminField label="Working days">
                <div style={{ display: "flex", gap: 6, marginTop: 4 }}>
                  {ALL_DAYS.map(d => (
                    <button key={d} onClick={() => toggleDay(d)} style={{ width: 36, height: 32, borderRadius: 4, border: "1px solid var(--border-default)", cursor: "pointer", fontSize: 11, fontFamily: "var(--font-mono)", fontWeight: 600, background: form.work_days.includes(d) ? "var(--accent)" : "transparent", color: form.work_days.includes(d) ? "#fff" : "var(--text-secondary)" }}>
                      {DAY_NAMES[d]}
                    </button>
                  ))}
                </div>
              </AdminField>
              <div style={{ display: "flex", gap: "var(--space-md)" }}>
                <AdminField label="Work start hour" style={{ flex: 1 }}>
                  <input type="number" min={0} max={23} value={form.work_start_hour} onChange={e => setForm(f => ({ ...f, work_start_hour: parseInt(e.target.value) || 9 }))} style={{ ...inputStyle, width: "100%" }} />
                </AdminField>
                <AdminField label="Work end hour" style={{ flex: 1 }}>
                  <input type="number" min={1} max={24} value={form.work_end_hour} onChange={e => setForm(f => ({ ...f, work_end_hour: parseInt(e.target.value) || 17 }))} style={{ ...inputStyle, width: "100%" }} />
                </AdminField>
              </div>
              <AdminField label="Description (optional)"><input value={form.description} onChange={e => setForm(f => ({ ...f, description: e.target.value }))} style={inputStyle} /></AdminField>
              <div style={{ display: "flex", gap: "var(--space-sm)", justifyContent: "flex-end" }}>
                <Button variant="secondary" onClick={() => setEditing(null)}>Cancel</Button>
                <Button onClick={handleSave} disabled={saving || !form.name}>{saving ? "Saving…" : editing?.id ? "Save Changes" : "Create Calendar"}</Button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ── Permissions Tab ──────────────────────────────────────────── */

function PermissionsTab() {
  const { token } = useAuth();
  const { permissions, refresh } = usePermissions();
  const [draft, setDraft] = useState<Record<string, string[]>>({});
  const [roles, setRoles] = useState<string[]>([]);
  // Derive module list directly from nav-data — the single source of truth.
  // Any new component added to nav-data.ts (including future marketplace packages)
  // automatically appears here with no backend changes required.
  const modules = NAV_DATA.map(e => ({ path: e.path, label: e.label }));

  const [loadingMeta, setLoadingMeta] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Seed draft from context whenever permissions load
  useEffect(() => {
    setDraft({ ...permissions });
  }, [permissions]);

  // Load roles from Access Directory — only network call needed now
  useEffect(() => {
    if (!token) return;
    fetch("/api/v1/access-roles", { headers: { Authorization: `Bearer ${token}` } })
      .then(r => r.ok ? r.json() : [])
      .then((rolesData: any[]) => {
        const names = rolesData.map((r: any) => r.name);
        names.sort((a: string, b: string) => {
          const aAdmin = a.toLowerCase().includes("admin");
          const bAdmin = b.toLowerCase().includes("admin");
          if (aAdmin && !bAdmin) return -1;
          if (!aAdmin && bAdmin) return 1;
          return a.localeCompare(b);
        });
        setRoles(names);
      })
      .catch(() => {})
      .finally(() => setLoadingMeta(false));
  }, [token]);

  const toggle = useCallback((path: string, role: string) => {
    setDraft(prev => {
      const current = prev[path] ?? [];
      const next = current.includes(role)
        ? current.filter(r => r !== role)
        : [...current, role];
      return { ...prev, [path]: next };
    });
    setSaved(false);
  }, []);

  const save = async () => {
    if (!token) return;
    setSaving(true);
    setError(null);
    try {
      const res = await fetch("/api/v1/admin/permissions", {
        method: "PUT",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ permissions: draft }),
      });
      if (!res.ok) throw new Error(await res.text());
      refresh();
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch (e: any) {
      setError(e.message ?? "Commit failed");
    } finally {
      setSaving(false);
    }
  };

  const colW = 88;

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "var(--space-lg)" }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 600, color: "var(--text-primary)" }}>Route Permission Matrix</div>
          <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
            Roles come from Access Directory — adding a role there adds a column here automatically.
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          {saved && <span style={{ fontSize: 12, color: "var(--status-completed)" }}>Saved</span>}
          {error && <span style={{ fontSize: 12, color: "var(--status-failed)" }}>{error}</span>}
          <button
            onClick={save}
            disabled={saving}
            style={{
              padding: "8px 20px", fontSize: 13, fontWeight: 600, borderRadius: "var(--radius-sm)",
              border: "none", background: "var(--accent)", color: "#fff",
              cursor: saving ? "not-allowed" : "pointer", opacity: saving ? 0.7 : 1,
            }}
          >
            {saving ? "Committing…" : "Commit Changes"}
          </button>
        </div>
      </div>

      {loadingMeta ? (
        <div style={{ padding: "var(--space-xl)", color: "var(--text-muted)", fontSize: 13 }}>Loading pages and roles…</div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ borderCollapse: "collapse", fontSize: 12, width: "100%", minWidth: 600 }}>
            <thead>
              <tr>
                <th style={{ textAlign: "left", padding: "8px 12px", fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", borderBottom: "1px solid var(--border-subtle)", minWidth: 180, position: "sticky", left: 0, background: "var(--bg-root)", zIndex: 1 }}>
                  Components
                </th>
                {roles.map(role => (
                  <th key={role} style={{ padding: "8px 4px", fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", borderBottom: "1px solid var(--border-subtle)", width: colW, minWidth: colW }}>
                    {role.replace(/_/g, " ").toUpperCase()}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {modules.map(({ path, label }, i) => {
                const rowRoles = draft[path] ?? [];
                return (
                  <tr key={path} style={{ background: i % 2 === 0 ? "transparent" : "var(--bg-elevated)" }}>
                    <td style={{ padding: "7px 12px", color: "var(--text-primary)", fontWeight: 500, borderBottom: "1px solid var(--border-subtle)", position: "sticky", left: 0, background: i % 2 === 0 ? "var(--bg-root)" : "var(--bg-elevated)", zIndex: 1 }}>
                      {label}
                    </td>
                    {roles.map(role => (
                      <td key={role} style={{ padding: "7px 4px", borderBottom: "1px solid var(--border-subtle)" }}>
                        <input
                          type="checkbox"
                          checked={rowRoles.includes(role)}
                          onChange={() => toggle(path, role)}
                          style={{ cursor: "pointer", accentColor: "var(--accent)", width: 15, height: 15 }}
                        />
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <div style={{ marginTop: "var(--space-lg)", padding: "10px 14px", background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)", fontSize: 12, color: "var(--text-muted)" }}>
        <strong style={{ color: "var(--text-secondary)" }}>Admin</strong> always has access to everything regardless of this matrix.
      </div>
    </div>
  );
}

/* ── Helpers ──────────────────────────────────────────────────── */

function actionColor(action: string): string {
  if (action.includes("created")) return "var(--accent)";
  if (action.includes("resolved") || action.includes("completed")) return "var(--status-completed)";
  if (action.includes("cancelled") || action.includes("failed") || action.includes("breached")) return "var(--status-failed)";
  if (action.includes("sla") || action.includes("claimed")) return "var(--status-running)";
  return "var(--text-secondary)";
}

const selectStyle: React.CSSProperties = {
  padding: "6px 12px", fontSize: 12, fontFamily: "var(--font-body)",
  background: "var(--bg-input)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-primary)", outline: "none",
};

const inputStyle: React.CSSProperties = {
  padding: "8px 12px", fontSize: 13, fontFamily: "var(--font-body)",
  background: "var(--bg-input)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-primary)", outline: "none",
  boxSizing: "border-box" as const, width: "100%",
};

function AdminField({ label, children, style }: { label: string; children: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, ...style }}>
      <label style={{ fontSize: 10, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "var(--font-mono)" }}>
        {label}
      </label>
      {children}
    </div>
  );
}

/* ── Blacklist Section ────────────────────────────────────────────── */

function BlacklistSection({ mktFetch }: { mktFetch: (path: string, opts?: RequestInit) => Promise<Response> }) {
  const [entries, setEntries]     = React.useState<any[]>([]);
  const [showAdd, setShowAdd]     = React.useState(false);
  const [type, setType]           = React.useState("package");
  const [value, setValue]         = React.useState("");
  const [reason, setReason]       = React.useState("");
  const [notify, setNotify]       = React.useState(false);
  const [loading, setLoading]     = React.useState(true);

  const inp: React.CSSProperties = { width: "100%", padding: "8px 10px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" as const, marginBottom: 8 };
  const btn: React.CSSProperties = { padding: "6px 14px", border: "none", borderRadius: "var(--radius-sm)", fontSize: 12, fontWeight: 700, cursor: "pointer", background: "var(--accent)", color: "#fff" };
  const btnGhost: React.CSSProperties = { background: "transparent", border: "1px solid var(--border-default)", color: "var(--text-secondary)" };

  const load = () => {
    mktFetch("/blacklist").then(r => r.json()).then(d => { setEntries(d.blacklist ?? []); setLoading(false); }).catch(() => setLoading(false));
  };
  React.useEffect(load, []);

  const add = async () => {
    if (!value.trim() || !reason.trim()) return;
    await mktFetch("/blacklist", { method: "POST", body: JSON.stringify({ type, value: value.trim(), reason: reason.trim(), notify_velaris: notify }) });
    setShowAdd(false); setValue(""); setReason(""); setNotify(false); load();
  };

  const remove = async (id: string) => {
    await mktFetch(`/blacklist/${id}`, { method: "DELETE" }); load();
  };

  const TYPE_LABEL: Record<string, string> = { org: "GitHub Org", source: "Source URL", package: "Package ID" };

  return (
    <div style={{ marginTop: 28 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <div>
          <div style={{ fontSize: 14, fontWeight: 700, color: "var(--text-primary)" }}>Blacklist</div>
          <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
            Block a compromised package, publisher, or source from this instance immediately.
          </div>
        </div>
        <button style={btn} onClick={() => setShowAdd(v => !v)}>+ Blacklist</button>
      </div>

      {showAdd && (
        <div style={{ background: "var(--bg-card)", border: "1px solid #ef444455", borderRadius: "var(--radius-md)", padding: 16, marginBottom: 12 }}>
          <div style={{ display: "grid", gridTemplateColumns: "140px 1fr", gap: 10, marginBottom: 4 }}>
            <div>
              <label style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", display: "block", marginBottom: 4 }}>Type</label>
              <select style={inp} value={type} onChange={e => setType(e.target.value)}>
                <option value="package">Package ID</option>
                <option value="org">GitHub Org</option>
                <option value="source">Source URL</option>
              </select>
            </div>
            <div>
              <label style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", display: "block", marginBottom: 4 }}>{TYPE_LABEL[type]}</label>
              <input style={inp} value={value} onChange={e => setValue(e.target.value)}
                placeholder={type === "package" ? "acme/connector-id" : type === "org" ? "bad-actor-org" : "https://raw.githubusercontent.com/..."} />
            </div>
          </div>
          <input style={inp} value={reason} onChange={e => setReason(e.target.value)} placeholder="Reason (required for audit trail — e.g. Malware detected in v2.1.0)" />
          <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: "var(--text-secondary)", marginBottom: 12, cursor: "pointer" }}>
            <input type="checkbox" checked={notify} onChange={e => setNotify(e.target.checked)} />
            Report to Velaris security team (opt-in — no customer data sent)
          </label>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button style={{ ...btn, ...btnGhost }} onClick={() => setShowAdd(false)}>Cancel</button>
            <button style={{ ...btn, background: "#ef4444" }} onClick={add} disabled={!value.trim() || !reason.trim()}>Blacklist</button>
          </div>
        </div>
      )}

      {loading
        ? <div style={{ fontSize: 12, color: "var(--text-muted)" }}>Loading…</div>
        : entries.length === 0
          ? <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "10px 0" }}>No blacklist entries. Packages from all sources are allowed.</div>
          : entries.map(e => (
            <div key={e.id} style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 12px", background: "#ef44440a", border: "1px solid #ef444430", borderRadius: "var(--radius-sm)", marginBottom: 6 }}>
              <span style={{ fontSize: 10, fontWeight: 700, padding: "2px 7px", borderRadius: 4, background: "#ef44441a", color: "#ef4444", fontFamily: "var(--font-mono)", textTransform: "uppercase", flexShrink: 0 }}>{e.type}</span>
              <span style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text-primary)", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{e.value}</span>
              <span style={{ fontSize: 11, color: "var(--text-muted)", flexShrink: 0 }}>{e.reason}</span>
              <button style={{ ...btn, ...btnGhost, fontSize: 11, padding: "3px 10px", flexShrink: 0 }} onClick={() => remove(e.id)}>Remove</button>
            </div>
          ))
      }
    </div>
  );
}

/* ── Access Rules Section ─────────────────────────────────────────── */

function AccessRulesSection({ mktFetch }: { mktFetch: (path: string, opts?: RequestInit) => Promise<Response> }) {
  const [rules,    setRules]    = React.useState<any[]>([]);
  const [groups,   setGroups]   = React.useState<any[]>([]);
  const [editing,  setEditing]  = React.useState<string | null>(null);
  const [ruleType, setRuleType] = React.useState("allow_all");
  const [pkgIds,   setPkgIds]   = React.useState("");
  const { token } = useAuth();

  const inp: React.CSSProperties = { width: "100%", padding: "8px 10px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" as const };
  const btn: React.CSSProperties = { padding: "6px 14px", border: "none", borderRadius: "var(--radius-sm)", fontSize: 12, fontWeight: 700, cursor: "pointer", background: "var(--accent)", color: "#fff" };
  const btnGhost: React.CSSProperties = { background: "transparent", border: "1px solid var(--border-default)", color: "var(--text-secondary)" };

  React.useEffect(() => {
    mktFetch("/access-rules").then(r => r.json()).then(d => setRules(d.rules ?? [])).catch(() => {});
    if (token) {
      fetch("/api/v1/access-groups", { headers: { Authorization: `Bearer ${token}` } })
        .then(r => r.ok ? r.json() : { groups: [] })
        .then(d => setGroups(d.groups ?? d ?? [])).catch(() => {});
    }
  }, [token]);

  const ruleFor = (agId: string) => rules.find(r => r.access_group_id === agId);

  const save = async (agId: string) => {
    const isAllowlist = ruleType === "allowlist";
    const isBlocklist = ruleType === "blocklist";
    const ids = pkgIds.split(",").map(s => s.trim()).filter(Boolean);
    await mktFetch(`/access-rules/${agId}`, {
      method: "PUT",
      body: JSON.stringify({
        rule_type: ruleType,
        allowed_package_ids: isAllowlist ? ids : [],
        blocked_package_ids: isBlocklist ? ids : [],
      }),
    });
    const d = await mktFetch("/access-rules").then(r => r.json());
    setRules(d.rules ?? []);
    setEditing(null);
  };

  const reset = async (agId: string) => {
    await mktFetch(`/access-rules/${agId}`, { method: "DELETE" });
    const d = await mktFetch("/access-rules").then(r => r.json());
    setRules(d.rules ?? []);
  };

  const RULE_COLOR: Record<string, string> = { allow_all: "#22c55e", official_only: "#0d9488", allowlist: "#3b82f6", blocklist: "#f59e0b" };
  const RULE_LABEL: Record<string, string> = { allow_all: "All packages", official_only: "Official only", allowlist: "Allowlist", blocklist: "Blocklist" };

  if (groups.length === 0) return null;

  return (
    <div style={{ marginTop: 28 }}>
      <div style={{ fontSize: 14, fontWeight: 700, color: "var(--text-primary)", marginBottom: 4 }}>Install Restrictions by Access Group</div>
      <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
        Control which marketplace packages each access group can install into sandbox.
      </div>

      {groups.map((g: any) => {
        const agId = g.id ?? g.slug ?? g.name;
        const rule = ruleFor(agId);
        const isEdit = editing === agId;
        return (
          <div key={agId} style={{ background: "var(--bg-card)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)", padding: "10px 14px", marginBottom: 8 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", flex: 1 }}>{g.name ?? agId}</span>
              {rule && rule.rule_type !== "allow_all" && (
                <span style={{ fontSize: 10, fontWeight: 700, padding: "2px 8px", borderRadius: 4, color: RULE_COLOR[rule.rule_type], background: `${RULE_COLOR[rule.rule_type]}1a`, fontFamily: "var(--font-mono)" }}>
                  {RULE_LABEL[rule.rule_type]}
                </span>
              )}
              <button style={{ ...btn, ...btnGhost, fontSize: 11, padding: "3px 10px" }} onClick={() => { setEditing(isEdit ? null : agId); setRuleType(rule?.rule_type ?? "allow_all"); setPkgIds((rule?.allowed_package_ids ?? rule?.blocked_package_ids ?? []).join(", ")); }}>
                {isEdit ? "Cancel" : "Edit"}
              </button>
              {rule && rule.rule_type !== "allow_all" && (
                <button style={{ ...btn, ...btnGhost, fontSize: 11, padding: "3px 10px" }} onClick={() => reset(agId)}>Reset</button>
              )}
            </div>

            {isEdit && (
              <div style={{ marginTop: 10, paddingTop: 10, borderTop: "1px solid var(--border-subtle)" }}>
                <select style={{ ...inp, marginBottom: 8 }} value={ruleType} onChange={e => setRuleType(e.target.value)}>
                  <option value="allow_all">Allow all packages</option>
                  <option value="official_only">Official packages only</option>
                  <option value="allowlist">Allowlist — specific packages only</option>
                  <option value="blocklist">Blocklist — all except specific packages</option>
                </select>
                {(ruleType === "allowlist" || ruleType === "blocklist") && (
                  <input style={{ ...inp, marginBottom: 8 }} value={pkgIds} onChange={e => setPkgIds(e.target.value)}
                    placeholder="Comma-separated package IDs e.g. acme/connector, vendor/template" />
                )}
                <div style={{ display: "flex", justifyContent: "flex-end" }}>
                  <button style={btn} onClick={() => save(agId)}>Save Rule</button>
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/* ── Marketplace Sources Tab ──────────────────────────────────────── */

function _mktAuthHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}`, "Content-Type": "application/json" } : { "Content-Type": "application/json" };
}

async function mktFetch(path: string, opts: RequestInit = {}) {
  return fetch(`/api/v1/marketplace${path}`, {
    ...opts,
    headers: { ..._mktAuthHdr(), ...(opts.headers ?? {}) },
  });
}

function MarketplaceSourcesTab() {
  const [sources,   setSources]   = React.useState<any[]>([]);
  const [loading,   setLoading]   = React.useState(true);
  const [showAdd,   setShowAdd]   = React.useState(false);
  const [name,      setName]      = React.useState("");
  const [url,       setUrl]       = React.useState("");
  const [token,     setToken]     = React.useState("");
  const [tier,      setTier]      = React.useState("community");
  const [pollHours, setPollHours] = React.useState(6);
  const [syncing,   setSyncing]   = React.useState<string | null>(null);

  const inp: React.CSSProperties = {
    width: "100%", padding: "8px 10px", border: "1px solid var(--border-default)",
    borderRadius: "var(--radius-sm)", fontSize: 13, background: "var(--bg-input)",
    color: "var(--text-primary)", boxSizing: "border-box", marginBottom: 10,
  };
  const btn: React.CSSProperties = {
    padding: "7px 16px", border: "none", borderRadius: "var(--radius-sm)",
    fontSize: 12, fontWeight: 700, cursor: "pointer",
    background: "var(--accent)", color: "#fff",
  };
  const btnGhost: React.CSSProperties = {
    background: "transparent", border: "1px solid var(--border-default)", color: "var(--text-secondary)",
  };

  const load = () => {
    setLoading(true);
    mktFetch("/sources")
      .then(r => r.json())
      .then(d => { setSources(d.sources ?? []); setLoading(false); })
      .catch(() => setLoading(false));
  };
  React.useEffect(load, []);

  const addSource = async () => {
    if (!name.trim() || !url.trim()) return;
    await mktFetch("/sources", {
      method: "POST",
      body: JSON.stringify({ name, url, tier, token: token || null, poll_interval_hours: pollHours }),
    });
    setShowAdd(false); setName(""); setUrl(""); setToken(""); load();
  };

  const [decommission, setDecommission] = React.useState<{ source: any; preview: any } | null>(null);
  const [decommReason, setDecommReason] = React.useState("");
  const [decommitting, setDecommitting] = React.useState(false);

  const startDecommission = async (s: any) => {
    const r = await mktFetch(`/sources/${s.id}/decommission-preview`);
    if (r.ok) {
      const preview = await r.json();
      setDecommission({ source: s, preview });
      setDecommReason("");
    }
  };

  const confirmDecommission = async () => {
    if (!decommission || !decommReason.trim()) return;
    setDecommitting(true);
    const preview = decommission.preview;
    const allInstallIds = (preview.prod_installs_affected ?? []).map((i: any) => i.package_id);
    await mktFetch(`/sources/${decommission.source.id}/decommission`, {
      method: "POST",
      body: JSON.stringify({ confirm_uninstall_package_ids: allInstallIds, reason: decommReason }),
    });
    setDecommitting(false);
    setDecommission(null);
    load();
  };

  const syncSource = async (id: string) => {
    setSyncing(id);
    await mktFetch(`/sources/${id}/sync`, { method: "POST" });
    setSyncing(null); load();
  };

  const syncAll = async () => {
    setSyncing("all");
    await mktFetch("/sources/sync-all", { method: "POST" });
    setSyncing(null); load();
  };

  const TIER_COLOR: Record<string, string> = {
    official: "#0d9488", community: "#3b82f6", private: "#8b5cf6",
  };

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 20 }}>
        <div>
          <h3 style={{ margin: "0 0 4px", fontSize: 15, fontWeight: 700, color: "var(--text-primary)" }}>
            Marketplace Package Sources
          </h3>
          <p style={{ margin: 0, fontSize: 12, color: "var(--text-muted)", maxWidth: 600, lineHeight: 1.5 }}>
            Each source is a publisher's <code>velaris.json</code> URL hosted in their own repository.
            Velaris polls each source on schedule and caches the package catalogue. Adding a source here
            is the only step needed — the publisher never pushes code to Velaris.
          </p>
        </div>
        <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
          <button style={{ ...btn, ...btnGhost }} onClick={syncAll} disabled={syncing === "all"}>
            {syncing === "all" ? "Syncing…" : "Sync All"}
          </button>
          <button style={btn} onClick={() => setShowAdd(v => !v)}>+ Add Source</button>
        </div>
      </div>

      {showAdd && (
        <div style={{ background: "var(--bg-card)", border: "1px solid var(--accent)", borderRadius: "var(--radius-md)", padding: 20, marginBottom: 20 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", marginBottom: 14 }}>Add Package Source</div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 2fr", gap: 12, marginBottom: 12 }}>
            <div>
              <label style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", display: "block", marginBottom: 4 }}>Publisher Name</label>
              <input style={inp} value={name} onChange={e => setName(e.target.value)} placeholder="ACME Corp" />
            </div>
            <div>
              <label style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", display: "block", marginBottom: 4 }}>velaris.json URL</label>
              <input style={inp} value={url} onChange={e => setUrl(e.target.value)}
                placeholder="https://raw.githubusercontent.com/acme/connector/main/velaris.json" />
            </div>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 80px", gap: 12, marginBottom: 14 }}>
            <div>
              <label style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", display: "block", marginBottom: 4 }}>Personal Access Token (private repos)</label>
              <input style={{ ...inp, fontFamily: "var(--font-mono)", fontSize: 11 }} type="password"
                value={token} onChange={e => setToken(e.target.value)} placeholder="ghp_… (optional, stored encrypted)" />
            </div>
            <div>
              <label style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", display: "block", marginBottom: 4 }}>Trust Tier</label>
              <select style={{ ...inp, marginBottom: 0 }} value={tier} onChange={e => setTier(e.target.value)}>
                <option value="community">Community (sandbox required)</option>
                <option value="private">Private (admin-vouched)</option>
              </select>
            </div>
            <div>
              <label style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", display: "block", marginBottom: 4 }}>Poll (hrs)</label>
              <input style={{ ...inp, marginBottom: 0 }} type="number" min={1} max={168}
                value={pollHours} onChange={e => setPollHours(Number(e.target.value))} />
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button style={{ ...btn, ...btnGhost }} onClick={() => setShowAdd(false)}>Cancel</button>
            <button style={btn} onClick={addSource} disabled={!name.trim() || !url.trim()}>Add & Sync Now</button>
          </div>
        </div>
      )}

      {loading && (
        <div style={{ textAlign: "center", padding: 40, color: "var(--text-muted)", fontSize: 13 }}>Loading sources…</div>
      )}

      {!loading && sources.length === 0 && (
        <div style={{ textAlign: "center", padding: 40, color: "var(--text-muted)" }}>
          <div style={{ fontSize: 28, marginBottom: 10 }}>🔗</div>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>No sources registered</div>
          <div style={{ fontSize: 13 }}>Add a publisher's velaris.json URL to start syncing their packages.</div>
        </div>
      )}

      {sources.map(s => (
        <div key={s.id} style={{ background: "var(--bg-card)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)", padding: "16px 20px", marginBottom: 10 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16 }}>
            <div style={{ flex: 1 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                <span style={{ fontSize: 14, fontWeight: 700, color: "var(--text-primary)" }}>{s.name}</span>
                <span style={{
                  fontSize: 10, fontWeight: 700, padding: "2px 7px", borderRadius: 4, fontFamily: "var(--font-mono)",
                  color: TIER_COLOR[s.tier] ?? "#94a3b8", background: `${TIER_COLOR[s.tier] ?? "#94a3b8"}1a`,
                }}>{s.tier}</span>
                {s.has_token && <span style={{ fontSize: 10, color: "var(--text-muted)" }}>🔒 token</span>}
                {!s.enabled && <span style={{ fontSize: 10, color: "#ef4444", fontWeight: 700 }}>disabled</span>}
              </div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginBottom: 4, wordBreak: "break-all" }}>{s.url}</div>
              <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>
                {s.package_count} package{s.package_count !== 1 ? "s" : ""} · polls every {s.poll_interval_hours}h
                {s.last_polled_at && ` · last synced ${new Date(s.last_polled_at).toLocaleString()}`}
              </div>
              {s.last_error && (
                <div style={{ fontSize: 11, color: "#ef4444", marginTop: 4 }}>⚠ Sync error: {s.last_error}</div>
              )}
              {s.is_stale && !s.last_error && (
                <div style={{ fontSize: 11, color: "#f59e0b", marginTop: 4 }}>
                  ⚠ Stale — no successful sync in {s.stale_since_days ?? "?"} days. Publisher repo may be unavailable.
                </div>
              )}
              {s.is_stale && (
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
                  Production installs from this source continue running normally. Sync manually or decommission if the publisher has abandoned the package.
                </div>
              )}
            </div>
            <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
              <button style={{ ...btn, ...btnGhost, fontSize: 11, padding: "5px 12px" }}
                onClick={() => syncSource(s.id)} disabled={syncing === s.id}>
                {syncing === s.id ? "Syncing…" : "Sync"}
              </button>
              {s.tier !== "official" && (
                <button style={{ ...btn, background: "#ef44441a", color: "#ef4444", border: "1px solid #ef444430", fontSize: 11, padding: "5px 12px" }}
                  onClick={() => startDecommission(s)}>
                  Decommission
                </button>
              )}
            </div>
          </div>
        </div>
      ))}

      {/* Decommission confirmation modal */}
      {decommission && (
        <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.6)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <div style={{ background: "var(--bg-panel)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-lg)", padding: 28, width: 520, maxWidth: "90vw", maxHeight: "80vh", overflow: "auto" }}>
            <div style={{ fontSize: 16, fontWeight: 700, color: "#ef4444", marginBottom: 4 }}>⚠ Decommission Source</div>
            <div style={{ fontSize: 13, color: "var(--text-primary)", fontWeight: 600, marginBottom: 16 }}>{decommission.source.name}</div>

            {/* Impact summary */}
            <div style={{ background: "var(--bg-subtle)", borderRadius: "var(--radius-sm)", padding: 14, marginBottom: 16, fontSize: 12 }}>
              <div style={{ marginBottom: 6 }}>
                <strong>{decommission.preview.packages_in_source}</strong> packages will be removed from catalogue
              </div>
              {decommission.preview.prod_installs_affected?.length > 0 && (
                <div style={{ color: "#ef4444", marginBottom: 6 }}>
                  <strong>{decommission.preview.prod_installs_affected.length}</strong> production install(s) will be uninstalled:
                  <ul style={{ margin: "4px 0 0 16px", padding: 0 }}>
                    {decommission.preview.prod_installs_affected.map((i: any) => (
                      <li key={i.package_id} style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>{i.package_id}</li>
                    ))}
                  </ul>
                </div>
              )}
              <div>{decommission.preview.active_sandboxes_affected} sandbox workspace(s) will be destroyed</div>
            </div>

            {/* Warnings */}
            <div style={{ marginBottom: 16 }}>
              {decommission.preview.warnings?.map((w: string, i: number) => (
                <div key={i} style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4, paddingLeft: 12, borderLeft: "2px solid var(--border-subtle)" }}>{w}</div>
              ))}
            </div>

            {/* Reason */}
            <div style={{ marginBottom: 14 }}>
              <label style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", display: "block", marginBottom: 4 }}>
                Reason for decommissioning (required for audit trail)
              </label>
              <textarea style={{ width: "100%", padding: "8px 10px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", resize: "none", height: 64, boxSizing: "border-box" as const }}
                value={decommReason} onChange={e => setDecommReason(e.target.value)}
                placeholder="e.g. Publisher no longer supported, replaced by internal connector…" />
            </div>

            <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
              <button style={{ ...btn, ...btnGhost }} onClick={() => setDecommission(null)}>Cancel</button>
              <button
                style={{ ...btn, background: "#ef4444" }}
                disabled={!decommReason.trim() || decommitting}
                onClick={confirmDecommission}>
                {decommitting ? "Decommissioning…" : "Confirm Decommission"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Blacklist ──────────────────────────────────────────────── */}
      <BlacklistSection mktFetch={(path, opts) => mktFetch(path, opts ?? {})} />

      {/* ── Access Rules ───────────────────────────────────────────── */}
      <AccessRulesSection mktFetch={(path, opts) => mktFetch(path, opts ?? {})} />

      <div style={{ marginTop: 24, padding: 16, background: "var(--bg-subtle)", borderRadius: "var(--radius-md)", fontSize: 12, color: "var(--text-muted)", lineHeight: 1.6 }}>
        <strong style={{ color: "var(--text-secondary)" }}>How sources work:</strong> Publishers host a <code>velaris.json</code> in their own GitHub/GitLab repository.
        Once their URL is registered here, Velaris polls it automatically on the configured schedule.
        New packages and version updates appear in the Marketplace for developers to install and test.
        Updates with no new outbound domains can be approved directly from the Marketplace → Review Queue without developer retesting.
      </div>
    </div>
  );
}
