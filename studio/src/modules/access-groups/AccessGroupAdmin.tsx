// Velaris P37 — Access Directory
// Unified: Access Groups, Access Roles, User Directory, Portals
import React, { useEffect, useState, useCallback, useRef } from "react";
import UserDirectory from "@modules/user-directory/UserDirectory";
import { getAccessToken } from "@/auth/tokenManager";
import { Tabs, Pill, Banner, SectionTitle, TextInput } from "@shared/components";

// ── Types ─────────────────────────────────────────────────────────────────────

type Portal = {
  id: string; name: string; portal_type: string;
  modules: string[]; homepage: string; theme: Record<string, string>;
  tenant_id: string | null; is_active: boolean;
};

type AccessRole = {
  id: string; name: string; description: string;
  privileges: Record<string, unknown>[]; tenant_id: string | null;
};

type AccessGroup = {
  id: string; name: string; description: string; tenant_id: string;
  portal_id: string; role_ids: string[];
  allowed_case_type_ids: string[]; allowed_queue_ids: string[];
  is_default: boolean; is_active: boolean;
};

type Member = {
  id: string; operator_id: string; is_primary: boolean;
  assigned_by: string | null; assigned_at: string;
};

type Tab = "groups" | "roles" | "users" | "portals" | "platform";

// ── API helpers ───────────────────────────────────────────────────────────────

function _authHdr(): Record<string, string> {
  const t = getAccessToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

async function api(path: string, opts?: RequestInit) {
  const r = await fetch(`/api/v1${path}`, {
    headers: { "Content-Type": "application/json", ..._authHdr(), ...opts?.headers },
    ...opts,
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  if (r.status === 204) return null;
  return r.json();
}

// ── Shared mini-components ────────────────────────────────────────────────────

function Badge({ label, color = "var(--accent)" }: { label: string; color?: string }) {
  return (
    <span style={{
      padding: "2px 8px", borderRadius: 10, fontSize: 10, fontWeight: 600,
      fontFamily: "var(--font-mono)", background: color + "22", color,
    }}>{label}</span>
  );
}

const PORTAL_TYPE_COLORS: Record<string, string> = {
  staff: "#0d9488", manager: "#f59e0b", admin: "#ef4444",
  customer: "#22c55e", mobile: "#0f766e",
};

function ErrBox({ err }: { err: string }) {
  return (
    <div style={{
      padding: "8px 12px", borderRadius: "var(--radius-sm)", fontSize: 12,
      background: "color-mix(in srgb, var(--status-failed) 10%, transparent)",
      color: "var(--status-failed)", marginBottom: 12,
    }}>{err}</div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

const TAB_LABELS: Record<Tab, string> = {
  groups: "Access Groups",
  roles:  "Access Roles",
  users:  "User Directory",
  portals: "Portals",
  platform: "Platform Roles",
};

export default function AccessGroupAdmin() {
  const [tab, setTab] = useState<Tab>("groups");

  return (
    <div style={{ display: "flex", flexDirection: "column", width: "100%", height: "100%", overflow: "hidden", boxSizing: "border-box" }}>
      <Tabs
        ariaLabel="Access directory"
        tabs={(["groups", "roles", "users", "portals", "platform"] as Tab[])
          .map(t => ({ key: t, label: TAB_LABELS[t] }))}
        value={tab}
        onChange={setTab}
      />
      {/* The bar is flush under the app header, like /help; the page's
          own padding moves onto the body so the underline can reach
          both edges. */}
      <div style={{ flex: 1, overflow: "auto", padding: "var(--space-xl) var(--space-2xl)", boxSizing: "border-box" }}>

      {tab === "groups"  && <GroupsTab />}
      {tab === "roles"   && <RolesTab />}
      {tab === "users"   && <UserDirectory embedded />}
      {tab === "portals" && <PortalsTab />}
      {tab === "platform" && <PlatformRolesTab />}
    </div>
    </div>
  );
}

// ── Portals Tab ───────────────────────────────────────────────────────────────

function PortalsTab() {
  const [portals, setPortals] = useState<Portal[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [editing, setEditing] = useState<Portal | null>(null);
  const [showForm, setShowForm] = useState(false);
  const load = useCallback(async () => {
    try { setPortals(await api("/portals")); }
    catch (e: any) { setErr(e.message); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function save(data: Partial<Portal>) {
    try {
      if (editing) await api(`/portals/${editing.id}`, { method: "PATCH", body: JSON.stringify(data) });
      else await api("/portals", { method: "POST", body: JSON.stringify(data) });
      setShowForm(false); setEditing(null); load();
    } catch (e: any) { setErr(e.message); }
  }

  async function del(id: string) {
    if (!confirm("Soft-delete this portal?")) return;
    try { await api(`/portals/${id}`, { method: "DELETE" }); load(); }
    catch (e: any) { setErr(e.message); }
  }

  return (
    <div>
      {err && <ErrBox err={err} />}
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 12 }}>
        <button onClick={() => { setEditing(null); setShowForm(true); }} style={btnStyle}>
          + New Portal
        </button>
      </div>

      {showForm && (
        <PortalForm
          initial={editing}
          onSave={save}
          onCancel={() => { setShowForm(false); setEditing(null); }}
        />
      )}

      {loading && <Spinner />}
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {portals.map((p) => {
          return (
            <div key={p.id} style={{ ...cardStyle, flexDirection: "column", alignItems: "stretch", gap: 0 }}>
              {/* Portal header row */}
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <Badge label={p.portal_type} color={PORTAL_TYPE_COLORS[p.portal_type] ?? "#0d9488"} />
                <span style={{ fontWeight: 600, fontSize: 14, flex: 1 }}>{p.name}</span>
                <span style={{ fontSize: 12, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                  → {p.homepage}
                </span>
                {p.tenant_id && (
                  <span style={{ fontSize: 11, color: "var(--text-muted)" }}>tenant: {p.tenant_id}</span>
                )}
                <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                  <a
                    href={p.portal_type === "customer" ? "/portal/" : p.homepage}
                    target="_blank" rel="noopener noreferrer"
                    style={{ ...smallBtnStyle, textDecoration: "none", display: "inline-flex", alignItems: "center", gap: 4 }}
                  >↗ Open</a>
                  <button style={smallBtnStyle} onClick={() => { setEditing(p); setShowForm(true); }}>Edit</button>
                  <button style={{ ...smallBtnStyle, color: "var(--status-failed)" }} onClick={() => del(p.id)}>Delete</button>
                </div>
              </div>

            </div>
          );
        })}
      </div>
    </div>
  );
}

function InlineModuleEditor({
  portalId, portalName, current, onSave, onCancel,
}: { portalId: string; portalName: string; current: string[]; onSave: (m: string[]) => void; onCancel: () => void }) {
  const [selected, setSelected] = useState<Set<string>>(new Set(current));
  const [saving, setSaving]     = useState(false);
  const [filter, setFilter]     = useState("");

  const visible = filter
    ? ALL_MODULE_SLUGS.filter(s => s.includes(filter.toLowerCase()))
    : ALL_MODULE_SLUGS;

  const toggle = (slug: string) =>
    setSelected(prev => {
      const next = new Set(prev);
      next.has(slug) ? next.delete(slug) : next.add(slug);
      return next;
    });

  const handleSave = async () => {
    setSaving(true);
    onSave(Array.from(selected));
    setSaving(false);
  };

  return (
    <div style={{ marginTop: 12, padding: "var(--space-md)", background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
        <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)" }}>
          Modules for <span style={{ color: "var(--accent)" }}>{portalName}</span>
          <span style={{ color: "var(--text-muted)", fontWeight: 400, marginLeft: 6 }}>({selected.size} enabled)</span>
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <button onClick={() => setSelected(new Set(ALL_MODULE_SLUGS))} style={{ ...smallBtnStyle, fontSize: 10 }}>All</button>
          <button onClick={() => setSelected(new Set())} style={{ ...smallBtnStyle, fontSize: 10 }}>None</button>
        </div>
      </div>

      <input
        placeholder="Search modules…"
        value={filter}
        onChange={e => setFilter(e.target.value)}
        style={{ ...inputStyle, marginBottom: 10, fontSize: 12 }}
      />

      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, maxHeight: 180, overflowY: "auto", marginBottom: 12 }}>
        {visible.map(slug => {
          const on = selected.has(slug);
          return (
            <button key={slug} onClick={() => toggle(slug)} style={{
              padding: "3px 10px", borderRadius: 10, fontSize: 11, fontFamily: "var(--font-mono)", cursor: "pointer",
              border: `1px solid ${on ? "var(--accent)" : "var(--border-default)"}`,
              background: on ? "var(--accent-dim)" : "transparent",
              color: on ? "var(--accent)" : "var(--text-secondary)",
              fontWeight: on ? 700 : 400,
            }}>{slug}</button>
          );
        })}
      </div>

      <div style={{ display: "flex", gap: 8 }}>
        <button onClick={handleSave} disabled={saving} style={btnStyle}>{saving ? "Saving…" : "Save Modules"}</button>
        <button onClick={onCancel} style={smallBtnStyle}>Cancel</button>
      </div>
    </div>
  );
}

// All available module slugs — derived from the platform's route list
const ALL_MODULE_SLUGS = [
  "work-center", "cases", "hxnexus", "help", "hxdocs", "hxcanvas",
  "analytics", "hxanalytics", "documents", "inbox",
  "case-designer", "form-builder", "reference-tables", "nlp-builder", "modeler", "app-builder",
  "hxwork", "importer", "graph", "process-mining", "live-activity", "monitor",
  "hxconnect", "hxbridge", "devconn", "hxsync", "hxfusion",
  "hxshield", "hxstream", "hxlogs", "compliance", "observability",
];

const ALL_HOMEPAGE_ROUTES = ALL_MODULE_SLUGS.map((s) => `/${s}`);

function ModulesInput({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const selected = value.split(",").map((s) => s.trim()).filter(Boolean);

  useEffect(() => {
    const h = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);

  const toggle = (slug: string) => {
    const next = selected.includes(slug) ? selected.filter((s) => s !== slug) : [...selected, slug];
    onChange(next.join(", "));
  };

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <div style={{ ...inputStyle, cursor: "pointer", display: "flex", flexWrap: "wrap", gap: 4, minHeight: 36, alignItems: "center" }}
        onClick={() => setOpen((o) => !o)}>
        {selected.length === 0 && <span style={{ color: "#999", fontSize: 12 }}>Click to select modules…</span>}
        {selected.map((s) => (
          <span key={s} style={{ background: "var(--accent-dim)", color: "var(--accent)", padding: "1px 7px", borderRadius: 10, fontSize: 11, fontFamily: "var(--font-mono)" }}>
            {s}
            <button onClick={(e) => { e.stopPropagation(); toggle(s); }} style={{ border: "none", background: "transparent", cursor: "pointer", color: "var(--accent)", marginLeft: 4, fontWeight: 700 }}>×</button>
          </span>
        ))}
      </div>
      {open && (
        <div style={{ position: "absolute", top: "100%", left: 0, right: 0, background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: 4, zIndex: 300, maxHeight: 220, overflowY: "auto", boxShadow: "0 4px 12px rgba(0,0,0,0.2)", marginTop: 2 }}>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, padding: 10 }}>
            {ALL_MODULE_SLUGS.map((slug) => (
              <button key={slug} onClick={() => toggle(slug)} style={{
                padding: "3px 10px", borderRadius: 10, fontSize: 11, fontFamily: "var(--font-mono)", cursor: "pointer", border: "1px solid",
                borderColor: selected.includes(slug) ? "var(--accent)" : "var(--border-default)",
                background: selected.includes(slug) ? "var(--accent-dim)" : "transparent",
                color: selected.includes(slug) ? "var(--accent)" : "var(--text-secondary)",
                fontWeight: selected.includes(slug) ? 700 : 400,
              }}>{slug}</button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function HomepageInput({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const h = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);

  const suggestions = ALL_HOMEPAGE_ROUTES.filter((r) => r.includes(value) && r !== value);

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <input style={inputStyle} value={value}
        onChange={(e) => { onChange(e.target.value); setOpen(true); }}
        onFocus={() => setOpen(true)}
        placeholder="/work-center" />
      {open && suggestions.length > 0 && (
        <div style={{ position: "absolute", top: "100%", left: 0, right: 0, background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: 4, zIndex: 300, maxHeight: 180, overflowY: "auto", boxShadow: "0 4px 12px rgba(0,0,0,0.2)", marginTop: 2 }}>
          {suggestions.map((r) => (
            <div key={r} onMouseDown={() => { onChange(r); setOpen(false); }}
              style={{ padding: "7px 12px", cursor: "pointer", fontSize: 12, fontFamily: "var(--font-mono)", borderBottom: "1px solid var(--border-subtle)" }}
              onMouseEnter={(e) => (e.currentTarget.style.background = "var(--bg-card-hover)")}
              onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
            >{r}</div>
          ))}
        </div>
      )}
    </div>
  );
}

function PortalForm({
  initial, onSave, onCancel,
}: { initial: Portal | null; onSave: (d: any) => void; onCancel: () => void }) {
  const [form, setForm] = useState({
    name: initial?.name ?? "",
    portal_type: initial?.portal_type ?? "staff",
    modules: (initial?.modules ?? []).join(", "),
    homepage: initial?.homepage ?? "/work-center",
    tenant_id: initial?.tenant_id ?? "",
  });
  const [tenants, setTenants] = useState<{ id: string; name: string; slug: string }[]>([]);

  useEffect(() => {
    api("/tenants").then((data: any) => {
      setTenants(Array.isArray(data) ? data : (data.items ?? []));
    }).catch(() => {});
  }, []);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    onSave({
      name: form.name, portal_type: form.portal_type,
      modules: form.modules.split(",").map((s) => s.trim()).filter(Boolean),
      homepage: form.homepage,
      tenant_id: form.tenant_id || null,
    });
  }

  return (
    <form onSubmit={submit} style={formStyle}>
      <div style={{ fontWeight: 600, marginBottom: 12 }}>{initial ? "Edit Portal" : "New Portal"}</div>

      <label style={labelStyle}>Name</label>
      <input style={inputStyle} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />

      <label style={labelStyle}>Type</label>
      <select style={inputStyle} value={form.portal_type} onChange={(e) => setForm({ ...form, portal_type: e.target.value })}>
        {["staff", "manager", "admin", "customer", "mobile"].map((t) => <option key={t}>{t}</option>)}
      </select>

      <label style={labelStyle}>Modules — select which sections this portal exposes</label>
      <ModulesInput value={form.modules} onChange={(v) => setForm({ ...form, modules: v })} />

      <label style={labelStyle}>Homepage route — first page users see after login</label>
      <HomepageInput value={form.homepage} onChange={(v) => setForm({ ...form, homepage: v })} />

      <label style={labelStyle}>Tenant</label>
      <select style={inputStyle} value={form.tenant_id}
        onChange={(e) => setForm({ ...form, tenant_id: e.target.value })}>
        <option value="">System-wide (no tenant restriction)</option>
        {tenants.map((t) => (
          <option key={t.id} value={t.id}>{t.name} ({t.slug})</option>
        ))}
      </select>
      {tenants.length === 0 && (
        <div style={{ fontSize: 10, color: "#888", marginTop: 2 }}>
          No tenants found — create tenants at <a href="/tenants" target="_blank" style={{ color: "var(--accent)" }}>/tenants</a>
        </div>
      )}

      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <button type="submit" style={btnStyle}>Commit</button>
        <button type="button" style={smallBtnStyle} onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

// ── Delete confirmation modal ─────────────────────────────────────────────────

function DeleteRoleModal({
  role, linkedGroups, onConfirm, onCancel,
}: {
  role: AccessRole;
  linkedGroups: AccessGroup[];
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.55)",
      display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
    }}>
      <div style={{
        background: "var(--bg-card)", border: "1px solid var(--border)",
        borderRadius: "var(--radius-md)", padding: 28, width: 420, boxShadow: "0 8px 32px rgba(0,0,0,0.3)",
      }}>
        <div style={{ fontSize: 16, fontWeight: 700, color: "var(--text-primary)", marginBottom: 8 }}>
          Delete Access Role
        </div>
        <div style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 16 }}>
          Are you sure you want to delete <strong style={{ color: "var(--text-primary)" }}>{role.name}</strong>?
          This action cannot be undone.
        </div>

        {!role.tenant_id && (
          <div style={{
            padding: "10px 14px", marginBottom: 12,
            background: "#ef444418", border: "1px solid #ef444455",
            borderRadius: "var(--radius-sm)", fontSize: 12, color: "#ef4444",
          }}>
            This is a <strong>built-in system role</strong>. Deleting it may break platform behaviour for all users that rely on it.
          </div>
        )}

        {linkedGroups.length > 0 && (
          <div style={{
            padding: "10px 14px", marginBottom: 16,
            background: "#f59e0b18", border: "1px solid #f59e0b55",
            borderRadius: "var(--radius-sm)", fontSize: 12,
          }}>
            <div style={{ fontWeight: 600, color: "#f59e0b", marginBottom: 6 }}>
              ⚠ This role is linked to {linkedGroups.length} group{linkedGroups.length !== 1 ? "s" : ""}:
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
              {linkedGroups.map(g => (
                <span key={g.id} style={{
                  padding: "2px 8px", borderRadius: 10, fontSize: 11,
                  background: "#f59e0b22", color: "#f59e0b", fontWeight: 600,
                }}>{g.name}</span>
              ))}
            </div>
            <div style={{ marginTop: 8, color: "var(--text-muted)", fontSize: 11 }}>
              Deleting this role will remove it from these groups.
            </div>
          </div>
        )}

        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button onClick={onCancel} style={smallBtnStyle}>Cancel</button>
          <button
            onClick={onConfirm}
            style={{
              ...smallBtnStyle,
              background: "var(--status-failed)", color: "#fff",
              border: "none", fontWeight: 600,
            }}
          >
            Delete
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Roles Tab ─────────────────────────────────────────────────────────────────

function RolesTab() {
  const [roles, setRoles] = useState<AccessRole[]>([]);
  const [groups, setGroups] = useState<AccessGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [editing, setEditing] = useState<AccessRole | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [linkingRole, setLinkingRole] = useState<string | null>(null);
  const [deletingRole, setDeletingRole] = useState<AccessRole | null>(null);

  const load = useCallback(async () => {
    try {
      const [r, g] = await Promise.all([api("/access-roles"), api("/access-groups")]);
      setRoles(r); setGroups(g);
    } catch (e: any) { setErr(e.message); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function save(data: Partial<AccessRole>) {
    try {
      if (editing) await api(`/access-roles/${editing.id}`, { method: "PATCH", body: JSON.stringify(data) });
      else await api("/access-roles", { method: "POST", body: JSON.stringify(data) });
      setShowForm(false); setEditing(null); load();
    } catch (e: any) { setErr(e.message); }
  }

  async function del(role: AccessRole) {
    try { await api(`/access-roles/${role.id}`, { method: "DELETE" }); load(); }
    catch (e: any) { setErr(e.message); }
    finally { setDeletingRole(null); }
  }

  async function toggleGroupLink(roleId: string, group: AccessGroup) {
    const hasRole = group.role_ids.includes(roleId);
    const newRoleIds = hasRole
      ? group.role_ids.filter(id => id !== roleId)
      : [...group.role_ids, roleId];
    try {
      await api(`/access-groups/${group.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          name: group.name, description: group.description,
          tenant_id: group.tenant_id, portal_id: group.portal_id,
          role_ids: newRoleIds,
          allowed_case_type_ids: group.allowed_case_type_ids,
          allowed_queue_ids: group.allowed_queue_ids,
          is_default: group.is_default, is_active: group.is_active,
        }),
      });
      load();
    } catch (e: any) { setErr(e.message); }
  }

  return (
    <div>
      {deletingRole && (
        <DeleteRoleModal
          role={deletingRole}
          linkedGroups={groups.filter(g => g.role_ids.includes(deletingRole.id))}
          onConfirm={() => del(deletingRole)}
          onCancel={() => setDeletingRole(null)}
        />
      )}
      {err && <ErrBox err={err} />}
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 12 }}>
        <button onClick={() => { setEditing(null); setShowForm(true); }} style={btnStyle}>+ New Role</button>
      </div>
      {showForm && (
        <RoleForm
          initial={editing}
          onSave={save}
          onCancel={() => { setShowForm(false); setEditing(null); }}
        />
      )}
      {loading && <Spinner />}
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {roles.map((r) => {
          const linkedGroups = groups.filter(g => g.role_ids.includes(r.id));
          const isLinking = linkingRole === r.id;
          return (
            <div key={r.id} style={{ ...cardStyle, flexDirection: "column", alignItems: "stretch", gap: 0 }}>
              <div style={{ display: "flex", alignItems: "flex-start" }}>
                <div style={{ flex: 1 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                    <span style={{ fontWeight: 600, fontSize: 14 }}>{r.name}</span>
                    {!r.tenant_id
                      ? <Badge label="built-in" color="#6b7280" />
                      : <Badge label="custom" color="var(--accent)" />}
                  </div>
                  <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>{r.description || "—"}</div>
                  <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginTop: 4 }}>
                    {r.privileges.length} privilege{r.privileges.length !== 1 ? "s" : ""}
                  </div>
                </div>
                <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                  <button
                    style={{ ...smallBtnStyle, color: isLinking ? "var(--accent)" : "var(--text-muted)" }}
                    onClick={() => setLinkingRole(isLinking ? null : r.id)}
                  >
                    Link Groups
                  </button>
                  <button style={smallBtnStyle} onClick={() => { setEditing(r); setShowForm(true); }}>Edit</button>
                  <button style={{ ...smallBtnStyle, color: "var(--status-failed)" }} onClick={() => setDeletingRole(r)}>Delete</button>
                </div>
              </div>

              {/* Linked groups summary */}
              {linkedGroups.length > 0 && !isLinking && (
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 10, paddingTop: 10, borderTop: "1px solid var(--border-subtle)" }}>
                  <span style={{ fontSize: 11, color: "var(--text-muted)" }}>Groups:</span>
                  {linkedGroups.map(g => <Badge key={g.id} label={g.name} color="#f59e0b" />)}
                </div>
              )}

              {/* Inline group picker */}
              {isLinking && (
                <div style={{ marginTop: 10, paddingTop: 10, borderTop: "1px solid var(--border-subtle)" }}>
                  <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 8 }}>
                    Select groups to link this role to:
                  </div>
                  {groups.length === 0 && (
                    <div style={{ fontSize: 12, color: "var(--text-muted)" }}>No groups yet.</div>
                  )}
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                    {groups.map(g => {
                      const linked = g.role_ids.includes(r.id);
                      return (
                        <label key={g.id} style={{
                          display: "flex", alignItems: "center", gap: 5, cursor: "pointer",
                          padding: "4px 10px", borderRadius: "var(--radius-sm)", fontSize: 12,
                          border: "1px solid var(--border-subtle)",
                          background: linked ? "#f59e0b22" : "var(--bg-elevated)",
                          color: linked ? "#f59e0b" : "var(--text-secondary)",
                        }}>
                          <input
                            type="checkbox"
                            checked={linked}
                            onChange={() => toggleGroupLink(r.id, g)}
                            style={{ width: 12, height: 12 }}
                          />
                          {g.name}
                          <span style={{ fontSize: 10, color: "var(--text-muted)" }}>· {g.tenant_id}</span>
                        </label>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Privilege editor types ────────────────────────────────────────────────────

interface CatalogAction   { id: string; label: string; description?: string }
interface CatalogResource { id: string; label: string; actions: CatalogAction[]; platform?: boolean }

// One privilege row: { resource, case_type_id, actions[] }
interface PrivRow { resource: string; case_type_id: string; actions: string[] }

function privToRows(privileges: Record<string, unknown>[]): PrivRow[] {
  return (privileges || []).map((p: any) => ({
    resource:     p.resource || "",
    case_type_id: p.case_type_id || "*",
    actions:      Array.isArray(p.actions) ? p.actions : [],
  }));
}

function rowsToPriv(rows: PrivRow[]): Record<string, unknown>[] {
  return rows
    .filter(r => r.resource && r.actions.length > 0)
    .map(r => ({ resource: r.resource, case_type_id: r.case_type_id || "*", actions: r.actions }));
}

/** "view_sensitive" → "View sensitive". Labels come from the catalog; a
 *  platform action's label IS its verb, so it is only tidied, never invented. */
function prettyAction(a: CatalogAction): string {
  const s = (a.label && a.label !== a.id ? a.label : a.id).replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** Which heading a resource is listed under in the picker. Case-level
 *  resources are one group; platform ones group by their first segment
 *  (`legal.holds` → Legal), so a new area appears with no list to update. */
function groupOf(c: CatalogResource): string {
  if (!c.platform) return "Case access";
  const head = c.id.split(".")[0].replace(/_/g, " ");
  return "Platform › " + head.charAt(0).toUpperCase() + head.slice(1);
}

// ── Resource picker: grouped + searchable ─────────────────────────────────────

function ResourcePicker({
  catalog, exclude, onPick, onClose,
}: {
  catalog: CatalogResource[];
  exclude: Set<string>;
  onPick: (id: string) => void;
  onClose: () => void;
}) {
  const [q, setQ] = useState("");

  const needle = q.trim().toLowerCase();
  const groups = new Map<string, CatalogResource[]>();
  for (const c of catalog) {
    if (exclude.has(c.id)) continue;
    if (needle && !(`${c.id} ${c.label} ${groupOf(c)}`.toLowerCase().includes(needle))) continue;
    const g = groupOf(c);
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g)!.push(c);
  }
  // Case access first, then platform areas alphabetically.
  const names = [...groups.keys()].sort((a, b) =>
    a === "Case access" ? -1 : b === "Case access" ? 1 : a.localeCompare(b));

  return (
    // Inline, full width of the panel — not a floating popover. Anchored to
    // the button it opened to the LEFT and was cut off by the panel edge.
    <div style={{
      marginTop: 6, width: "100%", maxHeight: 360, display: "flex", flexDirection: "column",
      boxSizing: "border-box",
      background: "var(--bg-card)", border: "1px solid var(--border-default)",
      borderRadius: "var(--radius-md)", boxShadow: "0 8px 24px rgba(0,0,0,0.25)",
    }}>
      <div style={{ padding: 8, borderBottom: "1px solid var(--border-subtle)" }}>
        <TextInput autoFocus placeholder="Search resources…" value={q}
          onChange={e => setQ(e.target.value)}
          onKeyDown={e => { if (e.key === "Escape") onClose(); }}
          style={{ width: "100%" }} />
      </div>
      <div style={{ overflowY: "auto", padding: "4px 0" }}>
        {names.length === 0 && (
          <div style={{ padding: "10px 12px", fontSize: 12, color: "var(--text-muted)" }}>Nothing matches.</div>
        )}
        {names.map(g => (
          <div key={g}>
            <div style={{ ...labelStyle, padding: "8px 12px 4px", marginBottom: 0 }}>{g}</div>
            {groups.get(g)!.map(c => (
              <button key={c.id} type="button" onClick={() => onPick(c.id)}
                title={c.id}
                style={{
                  display: "flex", justifyContent: "space-between", gap: 8, width: "100%",
                  padding: "6px 12px", background: "none", border: "none", cursor: "pointer",
                  color: "var(--text-primary)", fontSize: 13, textAlign: "left",
                }}
                onMouseEnter={e => (e.currentTarget.style.background = "var(--bg-elevated)")}
                onMouseLeave={e => (e.currentTarget.style.background = "none")}>
                <span>{c.label}</span>
                <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                  {c.actions.length}
                </span>
              </button>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Cascading privilege editor: ① resource → ② that resource's own actions ────
//
// The old grid gave every action in the whole catalog its own column (72 of
// them across 179 resources), so it ran far off the right edge while most cells
// were "—". Each row now shows only what its resource can do, and wraps.
//
// A role stays generic — which case types a person reaches is the ACCESS
// GROUP's `allowed_case_type_ids`, not the role's. New rows save as "*". A row
// that already carries a narrower `case_type_id` is kept exactly as saved and
// labelled, so editing a role never silently widens it.

function PrivilegeEditor({
  rows, catalog, onChange,
}: { rows: PrivRow[]; catalog: CatalogResource[]; onChange: (r: PrivRow[]) => void }) {
  const [picking, setPicking] = useState(false);
  // Outside-click closes the picker. The wrapper holds the button too, so the
  // button toggles it instead of closing and instantly reopening it.
  const pickRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!picking) return;
    const h = (e: MouseEvent) => {
      if (pickRef.current && !pickRef.current.contains(e.target as Node)) setPicking(false);
    };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [picking]);
  const catalogMap = Object.fromEntries(catalog.map(c => [c.id, c]));

  const toggle = (idx: number, action: string) => onChange(rows.map((r, i) => {
    if (i !== idx) return r;
    const has = r.actions.includes(action);
    return { ...r, actions: has ? r.actions.filter(a => a !== action) : [...r.actions, action] };
  }));
  const setAll = (idx: number, actions: string[]) =>
    onChange(rows.map((r, i) => i !== idx ? r : { ...r, actions }));
  const remove = (idx: number) => onChange(rows.filter((_, i) => i !== idx));
  const add = (id: string) => { onChange([...rows, { resource: id, case_type_id: "*", actions: [] }]); setPicking(false); };

  // A resource already granted for every case type is not offered again.
  const exclude = new Set(rows.filter(r => r.case_type_id === "*").map(r => r.resource));

  return (
    <div style={{ marginBottom: 12 }}>
      {rows.length === 0 && (
        <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "10px 0" }}>
          No privileges yet — add a resource, then tick what this role may do with it.
        </div>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {rows.map((row, idx) => {
          const res = catalogMap[row.resource];
          // Actions the catalog offers, plus any saved one it no longer lists —
          // dropping those on save would revoke something nobody chose to.
          const offered: CatalogAction[] = res ? [...res.actions] : [];
          for (const a of row.actions) if (!offered.some(o => o.id === a)) offered.push({ id: a, label: a });
          const allIds = offered.map(a => a.id);
          const allOn = allIds.length > 0 && allIds.every(a => row.actions.includes(a));
          return (
            <div key={idx} style={{
              border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)",
              background: "var(--bg-card)", padding: "10px 12px",
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
                <span style={{ fontWeight: 600, fontSize: 13, color: "var(--text-primary)" }}>
                  {res ? res.label : row.resource}
                </span>
                {res?.platform && <Pill tone="info">platform</Pill>}
                {!res && <Pill tone="warn" title="Not in this server's catalog — kept as saved">unknown resource</Pill>}
                {row.case_type_id !== "*" && (
                  <Pill tone="warn" title={`Saved as limited to case type ${row.case_type_id}. Kept as is; new privileges apply to all case types the access group allows.`}>
                    limited to 1 case type
                  </Pill>
                )}
                <span style={{ flex: 1 }} />
                {allIds.length > 1 && (
                  <button type="button" onClick={() => setAll(idx, allOn ? [] : allIds)}
                    style={{ ...smallBtnStyle, padding: "2px 8px", fontSize: 10 }}>
                    {allOn ? "Clear" : "All"}
                  </button>
                )}
                <button type="button" onClick={() => remove(idx)} aria-label={`Remove ${row.resource}`}
                  title="Remove this resource"
                  style={{ background: "none", border: "none", cursor: "pointer", color: "var(--status-failed)", fontSize: 14, padding: 0 }}>✕</button>
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {offered.map(a => {
                  const on = row.actions.includes(a.id);
                  return (
                    <label key={a.id} title={a.description || a.id} style={{
                      display: "inline-flex", alignItems: "center", gap: 6, cursor: "pointer",
                      padding: "3px 10px", borderRadius: 100, fontSize: 12,
                      border: `1px solid ${on ? "var(--accent)" : "var(--border-default)"}`,
                      background: on ? "color-mix(in srgb, var(--accent) 12%, transparent)" : "var(--bg-elevated)",
                      color: on ? "var(--accent-text)" : "var(--text-secondary)",
                    }}>
                      <input type="checkbox" checked={on} onChange={() => toggle(idx, a.id)}
                        style={{ width: 12, height: 12, margin: 0, accentColor: "var(--accent)" }} />
                      {prettyAction(a)}
                    </label>
                  );
                })}
              </div>
              {row.actions.length === 0 && (
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 6 }}>
                  Nothing ticked — this row is not saved.
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div ref={pickRef} style={{ marginTop: 10 }}>
        <button type="button" onClick={() => setPicking(p => !p)} style={{ ...smallBtnStyle, fontSize: 11 }}>
          + Add privilege
        </button>
        {picking && (
          <ResourcePicker catalog={catalog} exclude={exclude} onPick={add} onClose={() => setPicking(false)} />
        )}
      </div>
    </div>
  );
}

// ── Role form — opens as a side panel beside the role list ────────────────────

function RoleForm({
  initial, onSave, onCancel,
}: { initial: AccessRole | null; onSave: (d: any) => void; onCancel: () => void }) {
  const [form, setForm] = useState({
    name:        initial?.name ?? "",
    description: initial?.description ?? "",
    tenant_id:   initial?.tenant_id ?? "",
  });
  const [rows, setRows] = useState<PrivRow[]>(() => privToRows(initial?.privileges ?? []));
  const [catalog, setCatalog]       = useState<CatalogResource[]>([]);
  const [catalogErr, setCatalogErr] = useState<string | null>(null);
  const [showRaw, setShowRaw]       = useState(false);
  const [rawJson, setRawJson]       = useState("");
  const [rawErr, setRawErr]         = useState<string | null>(null);
  const [tenants, setTenants]       = useState<{ id: string; name: string; slug: string }[]>([]);

  useEffect(() => {
    api("/access-roles/catalog")
      .then((d: any) => { if (d?.resources) setCatalog(d.resources); else setCatalogErr("The privilege catalog came back empty."); })
      .catch((e: any) => setCatalogErr(e?.message || "Could not load the privilege catalog."));
    api("/tenants")
      .then((data: any) => setTenants(Array.isArray(data) ? data : (data?.items ?? [])))
      .catch(() => {});
  }, []);

  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") onCancel(); };
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, [onCancel]);

  // Sync raw JSON when rows change
  useEffect(() => {
    if (showRaw) setRawJson(JSON.stringify(rowsToPriv(rows), null, 2));
  }, [rows, showRaw]);

  function applyRaw() {
    try {
      const parsed = JSON.parse(rawJson);
      if (!Array.isArray(parsed)) throw new Error("not a list");
      setRows(privToRows(parsed));
      setRawErr(null);
      setShowRaw(false);
    } catch { setRawErr("Invalid JSON — expected a list of privileges."); }
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    onSave({
      name: form.name,
      description: form.description,
      privileges: rowsToPriv(rows),
      tenant_id: form.tenant_id || null,
    });
  }

  return (
    <div role="dialog" aria-modal="true" aria-label={initial ? "Edit role" : "New role"}
      style={{ position: "fixed", inset: 0, zIndex: 900, display: "flex", justifyContent: "flex-end", background: "rgba(0,0,0,0.35)" }}
      onMouseDown={e => { if (e.target === e.currentTarget) onCancel(); }}>
      <form onSubmit={submit} style={{
        width: "min(640px, 100vw)", height: "100%", boxSizing: "border-box",
        display: "flex", flexDirection: "column",
        background: "var(--bg-surface, var(--bg-card))", borderLeft: "1px solid var(--border-default)",
        boxShadow: "-8px 0 32px rgba(0,0,0,0.25)",
      }}>
        <div style={{ padding: "var(--space-lg) var(--space-xl)", borderBottom: "1px solid var(--border-subtle)" }}>
          <SectionTitle style={{ margin: 0 }} right={
            <button type="button" onClick={onCancel} aria-label="Close"
              style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 16 }}>✕</button>
          }>
            {initial ? `Edit role — ${initial.name}` : "New role"}
          </SectionTitle>
        </div>

        <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-lg) var(--space-xl)" }}>
          {initial && !initial.tenant_id && (
            <div style={{ marginBottom: 12 }}>
              <Banner tone="warn">
                Built-in system role — changes affect all groups that use it. Proceed carefully.
              </Banner>
            </div>
          )}

          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: "var(--space-md)" }}>
            <div>
              <label style={labelStyle}>Role name</label>
              <input style={inputStyle} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} required />
            </div>
            <div>
              <label style={labelStyle}>Tenant (blank = built-in)</label>
              <select style={inputStyle} value={form.tenant_id} onChange={e => setForm({ ...form, tenant_id: e.target.value })}>
                <option value="">— built-in (system-wide) —</option>
                {tenants.map(t => (
                  <option key={t.id} value={t.id}>{t.name} ({t.slug})</option>
                ))}
              </select>
            </div>
          </div>

          <label style={labelStyle}>Description</label>
          <input style={inputStyle} value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} />

          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", margin: "8px 0" }}>
            <label style={{ ...labelStyle, marginBottom: 0 }}>Privileges</label>
            <button type="button" onClick={() => {
              if (!showRaw) setRawJson(JSON.stringify(rowsToPriv(rows), null, 2));
              setShowRaw(!showRaw);
              setRawErr(null);
            }} style={{ ...smallBtnStyle, fontSize: 10 }}>
              {showRaw ? "← Back to Visual" : "{ } Raw JSON"}
            </button>
          </div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 10 }}>
            A role says what may be done. Which case types it reaches is set on the access group.
          </div>

          {showRaw ? (
            <>
              {rawErr && <ErrBox err={rawErr} />}
              <textarea
                style={{ ...inputStyle, height: 220, fontFamily: "var(--font-mono)", fontSize: 11 }}
                value={rawJson}
                onChange={e => { setRawErr(null); setRawJson(e.target.value); }}
              />
              <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 8 }}>
                Format: {`[{"resource":"case","case_type_id":"*","actions":["create","read"]}]`}
              </div>
              <button type="button" onClick={applyRaw} style={{ ...smallBtnStyle, marginBottom: 12 }}>Apply JSON</button>
            </>
          ) : catalogErr ? (
            <ErrBox err={`${catalogErr} The visual editor needs it; Raw JSON still works.`} />
          ) : catalog.length > 0 ? (
            <PrivilegeEditor rows={rows} catalog={catalog} onChange={setRows} />
          ) : (
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>Loading privilege catalog…</div>
          )}
        </div>

        <div style={{ display: "flex", gap: 8, padding: "var(--space-md) var(--space-xl)", borderTop: "1px solid var(--border-subtle)" }}>
          <button type="submit" style={btnStyle}>Save Role</button>
          <button type="button" style={smallBtnStyle} onClick={onCancel}>Cancel</button>
        </div>
      </form>
    </div>
  );
}

// ── Groups Tab (main view) ────────────────────────────────────────────────────

function GroupsTab() {
  const [groups, setGroups] = useState<AccessGroup[]>([]);
  const [portals, setPortals] = useState<Portal[]>([]);
  const [roles, setRoles] = useState<AccessRole[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<AccessGroup | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<AccessGroup | null>(null);

  const load = useCallback(async () => {
    try {
      const [g, p, r] = await Promise.all([
        api("/access-groups"), api("/portals"), api("/access-roles"),
      ]);
      setGroups(g); setPortals(p); setRoles(r);
    } catch (e: any) { setErr(e.message); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function save(data: Partial<AccessGroup>) {
    try {
      if (editing) await api(`/access-groups/${editing.id}`, { method: "PATCH", body: JSON.stringify(data) });
      else await api("/access-groups", { method: "POST", body: JSON.stringify(data) });
      setShowForm(false); setEditing(null); load();
    } catch (e: any) { setErr(e.message); }
  }

  async function del(id: string) {
    if (!confirm("Deactivate this access group?")) return;
    try { await api(`/access-groups/${id}`, { method: "DELETE" }); load(); }
    catch (e: any) { setErr(e.message); }
  }

  const portalMap = Object.fromEntries(portals.map((p) => [p.id, p]));
  const roleMap = Object.fromEntries(roles.map((r) => [r.id, r]));

  return (
    <div style={{ display: "flex", gap: "var(--space-lg)", height: "calc(100vh - 200px)" }}>
      {/* Left: group list */}
      <div style={{ flex: 1, overflow: "auto" }}>
        {err && <ErrBox err={err} />}
        <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 12 }}>
          <button onClick={() => { setEditing(null); setShowForm(true); }} style={btnStyle}>
            + New Group
          </button>
        </div>

        {showForm && (
          <GroupForm
            initial={editing}
            portals={portals}
            roles={roles}
            onSave={save}
            onCancel={() => { setShowForm(false); setEditing(null); }}
          />
        )}

        {loading && <Spinner />}
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {groups.map((g) => {
            const portal = portalMap[g.portal_id];
            const isSelected = selected?.id === g.id;
            return (
              <div
                key={g.id}
                onClick={() => setSelected(isSelected ? null : g)}
                style={{
                  ...cardStyle,
                  cursor: "pointer",
                  border: `1px solid ${isSelected ? "var(--accent)" : "var(--border-subtle)"}`,
                  background: isSelected ? "color-mix(in srgb, var(--accent) 6%, transparent)" : "var(--bg-card)",
                }}
              >
                <div style={{ flex: 1 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                    <span style={{ fontWeight: 600, fontSize: 14 }}>{g.name}</span>
                    {g.is_default && <Badge label="default" color="#22c55e" />}
                    <span style={{ fontSize: 11, color: "var(--text-muted)" }}>· {g.tenant_id}</span>
                  </div>
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                    {portal && (
                      <Badge label={portal.name} color={PORTAL_TYPE_COLORS[portal.portal_type] ?? "#0d9488"} />
                    )}
                    {g.role_ids.map((rid) => roleMap[rid] && (
                      <Badge key={rid} label={roleMap[rid].name} color="#f59e0b" />
                    ))}
                  </div>
                </div>
                <div style={{ display: "flex", gap: 6, alignItems: "center" }} onClick={(e) => e.stopPropagation()}>
                  {portal && (
                    <a
                      href={portal.portal_type === "customer" ? "/portal/" : portal.homepage}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{ ...smallBtnStyle, textDecoration: "none", display: "inline-flex", alignItems: "center", gap: 4 }}
                    >
                      ↗ Open
                    </a>
                  )}
                  <button style={smallBtnStyle} onClick={() => { setEditing(g); setShowForm(true); }}>Edit</button>
                  <button style={{ ...smallBtnStyle, color: "var(--status-failed)" }} onClick={() => del(g.id)}>Delete</button>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Right: members panel */}
      {selected && (
        <MembersPanel group={selected} onClose={() => setSelected(null)} />
      )}
    </div>
  );
}

function GroupForm({
  initial, portals, roles, onSave, onCancel,
}: {
  initial: AccessGroup | null;
  portals: Portal[];
  roles: AccessRole[];
  onSave: (d: any) => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState({
    name: initial?.name ?? "",
    description: initial?.description ?? "",
    tenant_id: initial?.tenant_id ?? "",
    portal_id: initial?.portal_id ?? (portals[0]?.id ?? ""),
    role_ids: initial?.role_ids ?? [] as string[],
    allowed_case_type_ids: (initial?.allowed_case_type_ids ?? ["*"]).join(", "),
    allowed_queue_ids: (initial?.allowed_queue_ids ?? ["*"]).join(", "),
    is_default: initial?.is_default ?? false,
  });

  function toggleRole(id: string) {
    setForm((f) => ({
      ...f,
      role_ids: f.role_ids.includes(id) ? f.role_ids.filter((r) => r !== id) : [...f.role_ids, id],
    }));
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    onSave({
      name: form.name, description: form.description, tenant_id: form.tenant_id,
      portal_id: form.portal_id, role_ids: form.role_ids,
      allowed_case_type_ids: form.allowed_case_type_ids.split(",").map((s) => s.trim()).filter(Boolean),
      allowed_queue_ids: form.allowed_queue_ids.split(",").map((s) => s.trim()).filter(Boolean),
      is_default: form.is_default,
    });
  }

  return (
    <form onSubmit={submit} style={formStyle}>
      <div style={{ fontWeight: 600, marginBottom: 12 }}>{initial ? "Edit Group" : "New Access Group"}</div>
      <label style={labelStyle}>Name</label>
      <input style={inputStyle} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
      <label style={labelStyle}>Description</label>
      <input style={inputStyle} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
      <label style={labelStyle}>Tenant ID</label>
      <input style={inputStyle} value={form.tenant_id} onChange={(e) => setForm({ ...form, tenant_id: e.target.value })} required />
      <label style={labelStyle}>Portal</label>
      <select style={inputStyle} value={form.portal_id} onChange={(e) => setForm({ ...form, portal_id: e.target.value })}>
        {portals.map((p) => <option key={p.id} value={p.id}>{p.name} ({p.portal_type})</option>)}
      </select>
      <label style={labelStyle}>Roles (check to assign)</label>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 12 }}>
        {roles.map((r) => (
          <label key={r.id} style={{
            display: "flex", alignItems: "center", gap: 4, cursor: "pointer",
            padding: "3px 8px", border: "1px solid var(--border-subtle)",
            borderRadius: "var(--radius-sm)", fontSize: 12,
            background: form.role_ids.includes(r.id) ? "var(--accent-dim)" : "var(--bg-elevated)",
            color: form.role_ids.includes(r.id) ? "var(--accent)" : "var(--text-secondary)",
          }}>
            <input type="checkbox" checked={form.role_ids.includes(r.id)} onChange={() => toggleRole(r.id)}
              style={{ width: 12, height: 12 }} />
            {r.name}
          </label>
        ))}
      </div>
      <label style={labelStyle}>Allowed case type IDs (comma-sep, * = all)</label>
      <input style={inputStyle} value={form.allowed_case_type_ids}
        onChange={(e) => setForm({ ...form, allowed_case_type_ids: e.target.value })} />
      <label style={labelStyle}>Allowed queue IDs (comma-sep, * = all)</label>
      <input style={inputStyle} value={form.allowed_queue_ids}
        onChange={(e) => setForm({ ...form, allowed_queue_ids: e.target.value })} />
      <label style={{ ...labelStyle, display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
        <input type="checkbox" checked={form.is_default} onChange={(e) => setForm({ ...form, is_default: e.target.checked })} />
        Auto-assign new operators in this tenant to this group
      </label>
      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
        <button type="submit" style={btnStyle}>Commit</button>
        <button type="button" style={smallBtnStyle} onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

// ── Members Panel ─────────────────────────────────────────────────────────────

function MembersPanel({ group, onClose }: { group: AccessGroup; onClose: () => void }) {
  const [members, setMembers] = useState<Member[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [newOp, setNewOp] = useState("");
  const [newPrimary, setNewPrimary] = useState(false);

  const load = useCallback(async () => {
    try { setMembers(await api(`/access-groups/${group.id}/members`)); }
    catch (e: any) { setErr(e.message); }
    finally { setLoading(false); }
  }, [group.id]);

  useEffect(() => { load(); }, [load]);

  async function addMember(e: React.FormEvent) {
    e.preventDefault();
    if (!newOp.trim()) return;
    try {
      await api(`/access-groups/${group.id}/members`, {
        method: "POST",
        body: JSON.stringify({ operator_id: newOp.trim(), is_primary: newPrimary }),
      });
      setNewOp(""); setNewPrimary(false); load();
    } catch (e: any) { setErr(e.message); }
  }

  async function removeMember(operatorId: string) {
    try { await api(`/access-groups/${group.id}/members/${operatorId}`, { method: "DELETE" }); load(); }
    catch (e: any) { setErr(e.message); }
  }

  return (
    <div style={{
      width: 320, border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)",
      background: "var(--bg-panel)", display: "flex", flexDirection: "column", overflow: "hidden",
    }}>
      {/* Header */}
      <div style={{
        padding: "var(--space-md)", borderBottom: "1px solid var(--border-subtle)",
        background: "var(--bg-card)", display: "flex", justifyContent: "space-between", alignItems: "center",
      }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: 14 }}>{group.name}</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
            Members
          </div>
        </div>
        <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 16, color: "var(--text-muted)" }}>✕</button>
      </div>

      {/* Add form */}
      <form onSubmit={addMember} style={{ padding: "var(--space-md)", borderBottom: "1px solid var(--border-subtle)" }}>
        {err && <ErrBox err={err} />}
        <label style={labelStyle}>Operator ID</label>
        <input style={inputStyle} value={newOp} onChange={(e) => setNewOp(e.target.value)} placeholder="user-id or username" />
        <label style={{ ...labelStyle, display: "flex", alignItems: "center", gap: 6, cursor: "pointer", marginBottom: 8 }}>
          <input type="checkbox" checked={newPrimary} onChange={(e) => setNewPrimary(e.target.checked)} />
          Set as primary group
        </label>
        <button type="submit" style={{ ...btnStyle, width: "100%" }}>Add Operator</button>
      </form>

      {/* Member list */}
      <div style={{ flex: 1, overflow: "auto", padding: "var(--space-md)" }}>
        {loading && <Spinner />}
        {members.length === 0 && !loading && (
          <div style={{ fontSize: 13, color: "var(--text-muted)", padding: 20 }}>
            No members yet
          </div>
        )}
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {members.map((m) => (
            <div key={m.id} style={{
              display: "flex", alignItems: "center", gap: 8,
              padding: "8px 10px", borderRadius: "var(--radius-sm)",
              border: "1px solid var(--border-subtle)", background: "var(--bg-elevated)",
            }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 13, fontWeight: 500 }}>{m.operator_id}</div>
                <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>
                  {m.is_primary ? "primary · " : ""}by {m.assigned_by ?? "system"}
                </div>
              </div>
              {m.is_primary && <Badge label="primary" color="#22c55e" />}
              <button
                onClick={() => removeMember(m.operator_id)}
                style={{ background: "none", border: "none", cursor: "pointer", color: "var(--status-failed)", fontSize: 14 }}
              >✕</button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Shared styles ─────────────────────────────────────────────────────────────

function Spinner() {
  return <div style={{ padding: 24, color: "var(--text-muted)", fontSize: 12 }}>Loading…</div>;
}

const cardStyle: React.CSSProperties = {
  display: "flex", alignItems: "center", gap: 12,
  padding: "var(--space-md)", borderRadius: "var(--radius-md)",
  border: "1px solid var(--border-subtle)", background: "var(--bg-card)",
  transition: "border-color 0.1s",
};

const formStyle: React.CSSProperties = {
  padding: "var(--space-md)", borderRadius: "var(--radius-md)",
  border: "1px solid var(--border-default)", background: "var(--bg-elevated)",
  marginBottom: 16, display: "flex", flexDirection: "column",
};

const labelStyle: React.CSSProperties = {
  fontSize: 11, fontWeight: 600, color: "var(--text-secondary)",
  textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 4,
};

const inputStyle: React.CSSProperties = {
  width: "100%", padding: "6px 10px", borderRadius: "var(--radius-sm)",
  border: "1px solid var(--border-default)", background: "var(--bg-input)",
  color: "var(--text-primary)", fontSize: 13, marginBottom: 10, boxSizing: "border-box",
};

const btnStyle: React.CSSProperties = {
  padding: "7px 16px", borderRadius: "var(--radius-sm)", border: "none",
  background: "var(--accent)", color: "#fff", fontWeight: 600, fontSize: 12,
  cursor: "pointer", fontFamily: "var(--font-mono)",
};

const smallBtnStyle: React.CSSProperties = {
  padding: "5px 12px", borderRadius: "var(--radius-sm)",
  border: "1px solid var(--border-default)", background: "var(--bg-elevated)",
  color: "var(--text-secondary)", fontSize: 11, cursor: "pointer", fontFamily: "var(--font-mono)",
};


/* ═══════════════════════════════════════════════════════════════════
   Platform role privileges — Phase 6.

   The other tabs edit ACCESS roles, which are the case-level concept and
   only reach a user who is in a group. This tab edits what a PLATFORM role
   (`velaris_users.roles`) may do, which until now came only from
   `SEEDED_ROLE_PRIVILEGES` — derived in code from each action's
   `legacy_roles`, i.e. the parity oracle. Changing it meant a deploy.

   Entries are ADDED to the seed, never a replacement, so the three columns
   below are genuinely different questions. An admin who cannot see the seed
   re-grants what the role already holds, or tries to remove it by deleting a
   row that never existed.
   ═══════════════════════════════════════════════════════════════════ */

interface RolePrivRow {
  role: string;
  seeded: Record<string, any>[];
  stored: Record<string, any>[];
  effective: Record<string, any>[];
}

function privLabel(p: Record<string, any>): string {
  const deny = String(p.effect || "allow").toLowerCase() === "deny";
  return `${deny ? "DENY " : ""}${p.resource}: ${(p.actions || []).join(", ")}`;
}

function PlatformRolesTab() {
  const [rows, setRows] = useState<RolePrivRow[]>([]);
  const [catalog, setCatalog] = useState<any[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, any>[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [r, c] = await Promise.all([
        api("/role-privileges"),
        api("/access-roles/catalog"),
      ]);
      setRows(r.roles || []);
      // Only the platform half is grantable here; the legacy case-level
      // resources belong to access roles and are evaluated by a different code
      // path entirely. Offering them would be an invitation to grant `case.read`
      // and wonder why a platform screen stayed shut.
      setCatalog((c.resources || []).filter((x: any) => x.platform));
    } catch (e: any) { setErr(e.message); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const startEdit = (row: RolePrivRow) => {
    setEditing(row.role);
    setDraft(row.stored.map((p) => ({ ...p })));
    setErr(null); setNote(null);
  };

  const save = async () => {
    if (!editing) return;
    setBusy(true); setErr(null); setNote(null);
    try {
      await api(`/role-privileges/${editing}`, {
        method: "PUT", body: JSON.stringify({ privileges: draft }),
      });
      setNote(`${editing} updated. Other workers pick this up within a minute.`);
      setEditing(null);
      await load();
    } catch (e: any) {
      // The lockout refusal (409) arrives here. Show it as-is: it names the
      // action that would have been left unheld, which is the only useful thing
      // to say.
      setErr(e.message);
    } finally { setBusy(false); }
  };

  const addEntry = (resource: string, action: string, effect: string) => {
    setDraft((d) => {
      const hit = d.find((p) => p.resource === resource &&
        String(p.effect || "allow") === effect);
      if (hit) {
        if (!hit.actions.includes(action)) hit.actions = [...hit.actions, action];
        return [...d];
      }
      const entry: Record<string, any> = { resource, actions: [action] };
      if (effect === "deny") entry.effect = "deny";
      return [...d, entry];
    });
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ fontSize: 12, color: "var(--text-muted)", maxWidth: 900 }}>
        What a <strong>platform role</strong> may do, stored as data. Entries here are
        <strong> added to</strong> the built-in defaults — they never replace them, so an
        empty list means "exactly the defaults". To take a default away, add the same
        resource with effect <code>deny</code>: deny always wins, and a revocation that is
        a row somebody wrote is easier to explain than one that is an absence.
      </div>
      {err && <div style={{ color: "var(--status-failed)", fontSize: 13 }}>{err}</div>}
      {note && <div style={{ color: "var(--text-secondary)", fontSize: 13,
        borderLeft: "2px solid var(--accent)", paddingLeft: 10 }}>{note}</div>}

      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ textAlign: "left", color: "var(--text-muted)", fontSize: 11,
              textTransform: "uppercase" }}>
              <th style={{ padding: "6px 8px" }}>Role</th>
              <th style={{ padding: "6px 8px" }}>Built-in</th>
              <th style={{ padding: "6px 8px" }}>Granted here</th>
              <th style={{ padding: "6px 8px" }}></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.role} style={{ borderTop: "1px solid var(--border-subtle)" }}>
                <td style={{ padding: "8px", fontWeight: 600 }}>{r.role}</td>
                <td style={{ padding: "8px", color: "var(--text-muted)" }}>
                  {r.seeded.length === 1 && r.seeded[0].resource === "*"
                    ? "everything"
                    : `${r.seeded.length} default${r.seeded.length === 1 ? "" : "s"}`}
                </td>
                <td style={{ padding: "8px" }}>
                  {r.stored.length === 0
                    ? <span style={{ color: "var(--text-muted)" }}>—</span>
                    : r.stored.map((p, i) => (
                        <div key={i} style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>
                          {privLabel(p)}
                        </div>
                      ))}
                </td>
                <td style={{ padding: "8px", textAlign: "right" }}>
                  <button onClick={() => startEdit(r)} style={{
                    fontSize: 12, padding: "4px 10px", borderRadius: 6, cursor: "pointer",
                    border: "1px solid var(--border-default)", background: "transparent",
                    color: "var(--text-primary)" }}>Edit</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {editing && (
        <div style={{ border: "1px solid var(--border-default)", borderRadius: 8,
          padding: 14, display: "flex", flexDirection: "column", gap: 10 }}>
          <div style={{ fontSize: 13, fontWeight: 600 }}>Granting to “{editing}”</div>

          {draft.length === 0
            ? <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                Nothing granted beyond the defaults.
              </div>
            : draft.map((p, i) => (
                <div key={i} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <code style={{ fontSize: 11 }}>{privLabel(p)}</code>
                  <button onClick={() => setDraft((d) => d.filter((_, j) => j !== i))}
                    style={{ fontSize: 11, border: "none", background: "transparent",
                      color: "var(--status-failed)", cursor: "pointer" }}>remove</button>
                </div>
              ))}

          <AddPrivilegeRow catalog={catalog} onAdd={addEntry} />

          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={save} disabled={busy} style={{
              fontSize: 12, padding: "6px 14px", borderRadius: 6, border: "none",
              background: "var(--accent)", color: "#fff", cursor: "pointer" }}>
              {busy ? "Saving…" : "Save"}
            </button>
            <button onClick={() => setEditing(null)} style={{
              fontSize: 12, padding: "6px 12px", borderRadius: 6,
              border: "1px solid var(--border-default)", background: "transparent",
              color: "var(--text-muted)", cursor: "pointer" }}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}


/** Resource → action → effect picker.

    State-driven rather than read off the DOM: the action list has to follow the
    selected resource, and a resource whose verbs do not include the stale
    selection would otherwise submit an action that resource does not have — a
    grant that saves cleanly and matches nothing.
 */
function AddPrivilegeRow({ catalog, onAdd }: {
  catalog: any[];
  onAdd: (resource: string, action: string, effect: string) => void;
}) {
  const [resource, setResource] = useState("");
  const [action, setAction] = useState("");
  const [effect, setEffect] = useState("allow");

  const current = catalog.find((c) => c.id === resource) || catalog[0];
  const actions: any[] = current?.actions || [];

  useEffect(() => {
    if (!resource && catalog.length) setResource(catalog[0].id);
  }, [catalog, resource]);

  useEffect(() => {
    // Keep the action valid for the chosen resource.
    if (actions.length && !actions.some((a) => a.id === action)) {
      setAction(actions[0].id);
    }
  }, [actions, action]);

  const sel = { fontSize: 12, padding: "5px 8px", borderRadius: 6,
    border: "1px solid var(--border-default)", background: "var(--bg-secondary)",
    color: "var(--text-primary)" } as const;

  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      <select value={resource} onChange={(e) => setResource(e.target.value)} style={sel}>
        {catalog.map((r) => <option key={r.id} value={r.id}>{r.id}</option>)}
      </select>
      <select value={action} onChange={(e) => setAction(e.target.value)} style={sel}>
        {actions.map((a: any) => <option key={a.id} value={a.id}>{a.id}</option>)}
      </select>
      <select value={effect} onChange={(e) => setEffect(e.target.value)} style={sel}>
        <option value="allow">allow</option>
        <option value="deny">deny</option>
      </select>
      <button
        onClick={() => resource && action && onAdd(resource, action, effect)}
        disabled={!resource || !action}
        style={{ fontSize: 12, padding: "5px 12px", borderRadius: 6,
          border: "1px solid var(--border-default)", background: "transparent",
          color: "var(--text-primary)", cursor: "pointer" }}>Add</button>
      {current?.actions?.find((a: any) => a.id === action)?.description && (
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
          {current.actions.find((a: any) => a.id === action).description}
        </span>
      )}
    </div>
  );
}
