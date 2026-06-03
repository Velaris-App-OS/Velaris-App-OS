// HELIX P37 — Access Directory
// Unified: Access Groups, Access Roles, User Directory, Portals
import React, { useEffect, useState, useCallback, useRef } from "react";
import UserDirectory from "@modules/user-directory/UserDirectory";

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

type Tab = "groups" | "roles" | "users" | "portals";

// ── API helpers ───────────────────────────────────────────────────────────────

function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
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
};

export default function AccessGroupAdmin() {
  const [tab, setTab] = useState<Tab>("groups");

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", height: "100%", overflow: "auto", boxSizing: "border-box", display: "flex", flexDirection: "column" }}>
      {/* Tab bar — always visible, consistent styling */}
      <div style={{
        display: "flex", background: "var(--bg-card)", borderRadius: "var(--radius-sm)",
        border: "1px solid var(--border-subtle)", marginBottom: "var(--space-xl)", flexShrink: 0,
      }}>
        {(["groups", "roles", "users", "portals"] as Tab[]).map((t) => (
          <button key={t} onClick={() => setTab(t)} style={{
            padding: "8px 18px", fontSize: 12, fontWeight: 500,
            fontFamily: "var(--font-mono)", textTransform: "uppercase",
            letterSpacing: "0.04em", border: "none", cursor: "pointer",
            color: tab === t ? "var(--accent)" : "var(--text-muted)",
            background: tab === t ? "var(--accent-dim)" : "transparent",
            borderRadius: "var(--radius-sm)",
          }}>
            {TAB_LABELS[t]}
          </button>
        ))}
      </div>

      {tab === "groups"  && <GroupsTab />}
      {tab === "roles"   && <RolesTab />}
      {tab === "users"   && <UserDirectory embedded />}
      {tab === "portals" && <PortalsTab />}
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
  "case-designer", "form-builder", "nlp-builder", "modeler", "app-builder",
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

// ── Privilege matrix types ────────────────────────────────────────────────────

interface CatalogAction   { id: string; label: string }
interface CatalogResource { id: string; label: string; actions: CatalogAction[] }

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

// ── Interactive privilege matrix ──────────────────────────────────────────────

function PrivilegeMatrix({
  rows, catalog, onChange,
}: { rows: PrivRow[]; catalog: CatalogResource[]; onChange: (r: PrivRow[]) => void }) {
  const catalogMap = Object.fromEntries(catalog.map(c => [c.id, c]));
  const allActions = Array.from(new Set(catalog.flatMap(c => c.actions.map(a => a.id))));

  const toggleCell = (rowIdx: number, action: string) => {
    onChange(rows.map((r, i) => {
      if (i !== rowIdx) return r;
      const has = r.actions.includes(action);
      return { ...r, actions: has ? r.actions.filter(a => a !== action) : [...r.actions, action] };
    }));
  };

  const addRow = () => {
    const used = new Set(rows.map(r => r.resource));
    const next = catalog.find(c => !used.has(c.id));
    if (next) onChange([...rows, { resource: next.id, case_type_id: "*", actions: [] }]);
  };

  const removeRow   = (idx: number) => onChange(rows.filter((_, i) => i !== idx));
  const updateScope = (idx: number, val: string) => onChange(rows.map((r, i) => i !== idx ? r : { ...r, case_type_id: val }));
  const updateRes   = (idx: number, val: string) => onChange(rows.map((r, i) => i !== idx ? r : { ...r, resource: val, actions: [] }));

  const cols = `150px 90px repeat(${allActions.length}, minmax(48px, 1fr)) 28px`;

  return (
    <div style={{ marginBottom: 12 }}>
      {/* Header row */}
      <div style={{ display: "grid", gridTemplateColumns: cols, gap: 2, minWidth: 0, alignItems: "center", marginBottom: 4, borderBottom: "1px solid var(--border-subtle)", paddingBottom: 4 }}>
        <div style={matHdrCell}>Resource</div>
        <div style={matHdrCell}>Scope</div>
        {allActions.map(a => (
          <div key={a} style={{ display: "flex", justifyContent: "center", alignItems: "center", padding: "6px 2px" }}>
            <span style={{
              fontSize: 9, fontFamily: "var(--font-mono)",
              textTransform: "uppercase", letterSpacing: "0.05em",
              color: "var(--text-muted)", textAlign: "center",
            }}>{a}</span>
          </div>
        ))}
        <div />
      </div>

      {/* Data rows */}
      {rows.map((row, idx) => {
        const res = catalogMap[row.resource];
        const available = res ? new Set(res.actions.map(a => a.id)) : new Set<string>();
        return (
          <div key={idx} style={{ display: "grid", gridTemplateColumns: cols, gap: 2, marginBottom: 2, alignItems: "center", minWidth: 0 }}>
            <select value={row.resource} onChange={e => updateRes(idx, e.target.value)}
              style={{ ...matCell, padding: "5px 6px", cursor: "pointer" }}>
              {catalog.map(c => <option key={c.id} value={c.id}>{c.label}</option>)}
            </select>
            <input value={row.case_type_id} onChange={e => updateScope(idx, e.target.value)}
              placeholder="*" title="* = all case types"
              style={{ ...matCell, padding: "5px 6px", fontSize: 11, fontFamily: "var(--font-mono)" }} />
            {allActions.map(action => {
              const on = row.actions.includes(action);
              return (
                <div key={action} style={{ display: "flex", justifyContent: "center" }}>
                  {available.has(action)
                    ? <input type="checkbox" checked={on} onChange={() => toggleCell(idx, action)}
                        style={{ width: 14, height: 14, cursor: "pointer", accentColor: "var(--accent)" }} />
                    : <span style={{ color: "var(--border-subtle)", fontSize: 11 }}>—</span>}
                </div>
              );
            })}
            <button type="button" onClick={() => removeRow(idx)}
              style={{ background: "none", border: "none", cursor: "pointer", color: "var(--status-failed)", fontSize: 14, padding: 0 }}>✕</button>
          </div>
        );
      })}

      <button type="button" onClick={addRow} style={{ ...smallBtnStyle, marginTop: 8, fontSize: 11 }}>
        + Add Resource Row
      </button>
    </div>
  );
}

const matHdrCell: React.CSSProperties = {
  fontSize: 9, fontFamily: "var(--font-mono)", textTransform: "uppercase",
  letterSpacing: "0.05em", color: "var(--text-muted)", padding: "3px 4px",
};

const matCell: React.CSSProperties = {
  background: "var(--bg-input)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-primary)", fontSize: 12,
  width: "100%",
};

// ── Role form with interactive privilege matrix ───────────────────────────────

function RoleForm({
  initial, onSave, onCancel,
}: { initial: AccessRole | null; onSave: (d: any) => void; onCancel: () => void }) {
  const [form, setForm] = useState({
    name:        initial?.name ?? "",
    description: initial?.description ?? "",
    tenant_id:   initial?.tenant_id ?? "",
  });
  const [rows, setRows] = useState<PrivRow[]>(() => privToRows(initial?.privileges ?? []));
  const [catalog, setCatalog]     = useState<CatalogResource[]>([]);
  const [showRaw, setShowRaw]     = useState(false);
  const [rawJson, setRawJson]     = useState("");
  const [rawErr, setRawErr]       = useState<string | null>(null);
  const [tenants, setTenants]     = useState<{ id: string; name: string; slug: string }[]>([]);

  useEffect(() => {
    api("/access-roles/catalog")
      .then((d: any) => { if (d?.resources) setCatalog(d.resources); })
      .catch(() => {});
    api("/tenants")
      .then((data: any) => setTenants(Array.isArray(data) ? data : (data?.items ?? [])))
      .catch(() => {});
  }, []);

  // Sync raw JSON when rows change
  useEffect(() => {
    if (showRaw) setRawJson(JSON.stringify(rowsToPriv(rows), null, 2));
  }, [rows, showRaw]);

  function applyRaw() {
    try {
      const parsed = JSON.parse(rawJson);
      setRows(privToRows(parsed));
      setRawErr(null);
      setShowRaw(false);
    } catch { setRawErr("Invalid JSON — check format."); }
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
    <form onSubmit={submit} style={{ ...formStyle, width: "100%" }}>
      <div style={{ fontWeight: 600, marginBottom: 12 }}>{initial ? "Edit Role" : "New Role"}</div>

      {initial && !initial.tenant_id && (
        <div style={{ fontSize: 12, padding: "8px 12px", marginBottom: 12, borderRadius: "var(--radius-sm)", background: "color-mix(in srgb, #f59e0b 12%, transparent)", color: "#f59e0b", borderLeft: "3px solid #f59e0b" }}>
          Built-in system role — changes affect all groups that use it. Proceed carefully.
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-md)", marginBottom: 12 }}>
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

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <label style={labelStyle}>Privileges</label>
        <button type="button" onClick={() => {
          if (!showRaw) setRawJson(JSON.stringify(rowsToPriv(rows), null, 2));
          setShowRaw(!showRaw);
          setRawErr(null);
        }} style={{ ...smallBtnStyle, fontSize: 10 }}>
          {showRaw ? "← Back to Visual" : "{ } Raw JSON"}
        </button>
      </div>

      {showRaw ? (
        <>
          {rawErr && <ErrBox err={rawErr} />}
          <textarea
            style={{ ...inputStyle, height: 160, fontFamily: "var(--font-mono)", fontSize: 11 }}
            value={rawJson}
            onChange={e => { setRawErr(null); setRawJson(e.target.value); }}
          />
          <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 8 }}>
            Format: {`[{"resource":"case","case_type_id":"*","actions":["create","read"]}]`}
          </div>
          <button type="button" onClick={applyRaw} style={{ ...smallBtnStyle, marginBottom: 12 }}>Apply JSON</button>
        </>
      ) : (
        catalog.length > 0
          ? <PrivilegeMatrix rows={rows} catalog={catalog} onChange={setRows} />
          : <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>Loading privilege catalog…</div>
      )}

      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
        <button type="submit" style={btnStyle}>Save Role</button>
        <button type="button" style={smallBtnStyle} onClick={onCancel}>Cancel</button>
      </div>
    </form>
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
