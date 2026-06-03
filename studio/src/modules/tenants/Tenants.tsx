import React, { useState, useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { useApi } from "@shared/hooks";
import {
  listTenants, createTenant, updateTenant, deleteTenant, permanentDeleteTenant,
  listTenantMembers, addTenantMember, updateTenantMember, removeTenantMember,
} from "@shared/api/client";
import type { Tenant } from "@shared/api/client";
import { Card, Button, Spinner, EmptyState, Stat, TimeAgo } from "@shared/components";

function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}

async function apiJSON<T>(url: string, opts: RequestInit = {}): Promise<T> {
  const r = await fetch(url, { ...opts, headers: { ..._authHdr(), ...opts.headers } });
  if (!r.ok) {
    let detail = `${url} → ${r.status}`;
    try { const j = await r.json(); if (j?.detail) detail = j.detail; } catch {}
    throw new Error(detail);
  }
  return r.json();
}

type UserEntry = { user_id: string; email?: string | null; display_name?: string | null };
type AccessRole = { name: string; display_name?: string | null };

function useUsers() {
  const [users, setUsers] = useState<UserEntry[]>([]);
  useEffect(() => {
    apiJSON<UserEntry[]>("/api/v1/user-directory?limit=500").catch(() => []).then(setUsers);
  }, []);
  return users;
}

function useAccessRoles() {
  const [roles, setRoles] = useState<AccessRole[]>([]);
  useEffect(() => {
    apiJSON<AccessRole[]>("/api/v1/access-roles").catch(() => []).then(setRoles);
  }, []);
  return roles;
}

/* ═══════════════════════════════════════════════════════════════════
   Tenants — multi-tenancy management (Phase 17)
   ═══════════════════════════════════════════════════════════════════ */

type ConfirmSpec = {
  title: string;
  body: string;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: () => void;
};

function ConfirmModal({ spec, onClose }: { spec: ConfirmSpec; onClose: () => void }) {
  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.5)", zIndex: 999, display: "flex", alignItems: "center", justifyContent: "center" }}>
      <div style={{ background: "var(--bg-panel)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-md)", padding: 28, width: 400, boxShadow: "0 8px 32px rgba(0,0,0,.3)" }}>
        <div style={{ fontSize: 15, fontWeight: 700, color: "var(--text-primary)", marginBottom: 10 }}>{spec.title}</div>
        <div style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 24, lineHeight: 1.6 }}>{spec.body}</div>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant={spec.danger ? "danger" : "primary"} onClick={() => { spec.onConfirm(); onClose(); }}>
            {spec.confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}

export default function Tenants() {
  const { data, loading, refetch } = useApi(listTenants);
  const tenants = data ?? [];
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Tenant | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null);

  const activeTenant = tenants.find(t => t.id === detailId);

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", overflow: "auto", height: "100%", boxSizing: "border-box" }}>
      {confirm && <ConfirmModal spec={confirm} onClose={() => setConfirm(null)} />}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-xl)" }}>
        <Button onClick={() => { setCreating(true); setEditing(null); }}>+ New Tenant</Button>
      </div>

      {/* Stats */}
      {!loading && tenants.length > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "var(--space-md)", marginBottom: "var(--space-lg)" }}>
          <Card><Stat label="Total Tenants" value={tenants.length} /></Card>
          <Card><Stat label="Active" value={tenants.filter(t => t.status === "active").length} /></Card>
          <Card><Stat label="Archived" value={tenants.filter(t => t.status === "archived").length} /></Card>
        </div>
      )}

      {/* Create/Edit form */}
      {(creating || editing) && (
        <TenantForm
          tenant={editing}
          onCancel={() => { setCreating(false); setEditing(null); }}
          onSaved={() => { setCreating(false); setEditing(null); refetch(); }}
        />
      )}

      {loading && <Spinner size={28} />}

      {!loading && tenants.length === 0 && (
        <EmptyState title="No tenants yet" description="Create a tenant to start isolating data by organization." />
      )}

      {/* Tenant list */}
      {tenants.map(t => (
        <Card key={t.id} style={{ marginBottom: "var(--space-sm)" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
            <div style={{ flex: 1, cursor: "pointer" }} onClick={() => setDetailId(detailId === t.id ? null : t.id)}>
              <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)" }}>
                <span style={{
                  width: 10, height: 10, borderRadius: "50%",
                  background: t.status === "active" ? "var(--status-completed)" : "var(--status-cancelled)",
                }} />
                <span style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>{t.name}</span>
                <code style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--accent)",
                  padding: "2px 6px", background: "var(--accent-dim)", borderRadius: 3 }}>{t.slug}</code>
                {t.slug === "default" && (
                  <span style={{ fontSize: 9, padding: "2px 6px", background: "var(--bg-elevated)",
                    color: "var(--text-muted)", borderRadius: 3, fontFamily: "var(--font-mono)" }}>DEFAULT</span>
                )}
              </div>
              {t.description && (
                <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 4 }}>{t.description}</div>
              )}
              <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 4 }}>
                created <TimeAgo date={t.created_at} />
                {t.max_cases && ` · max ${t.max_cases} cases`}
                {t.max_users && ` · max ${t.max_users} users`}
              </div>
            </div>
            <div style={{ display: "flex", gap: "var(--space-xs)" }}>
              <Button size="sm" variant="ghost" onClick={() => { setEditing(t); setCreating(false); }}>Edit</Button>
              {t.slug !== "default" && (<>
                {t.status === "active" ? (
                  <Button size="sm" variant="secondary" onClick={() => setConfirm({
                    title: `Archive "${t.name}"?`,
                    body: "The tenant will be deactivated. All data is preserved — you can reactivate it at any time.",
                    confirmLabel: "Archive",
                    onConfirm: async () => { await deleteTenant(t.id); refetch(); },
                  })}>Archive</Button>
                ) : (
                  <Button size="sm" variant="ghost" onClick={() => setConfirm({
                    title: `Reactivate "${t.name}"?`,
                    body: "The tenant will be set back to active. All existing data and members will be restored.",
                    confirmLabel: "Reactivate",
                    onConfirm: async () => { await updateTenant(t.id, { status: "active" }); refetch(); },
                  })}>Reactivate</Button>
                )}
                <Button size="sm" variant="danger" onClick={() => setConfirm({
                  title: `Permanently delete "${t.name}"?`,
                  body: `This will hard-delete the tenant and all associated data (members, cases, settings). Slug "${t.slug}" cannot be reused. This cannot be undone.`,
                  confirmLabel: "Delete permanently",
                  danger: true,
                  onConfirm: async () => { await permanentDeleteTenant(t.id); refetch(); },
                })}>Delete</Button>
              </>)}
            </div>
          </div>

          {/* Expanded detail */}
          {detailId === t.id && <TenantDetail tenantId={t.id} />}
        </Card>
      ))}
    </div>
  );
}

function TenantForm({ tenant, onCancel, onSaved }: {
  tenant: Tenant | null;
  onCancel: () => void;
  onSaved: () => void;
}) {
  const [slug, setSlug] = useState(tenant?.slug || "");
  const [name, setName] = useState(tenant?.name || "");
  const [description, setDescription] = useState(tenant?.description || "");
  const [maxCases, setMaxCases] = useState(tenant?.max_cases?.toString() || "");
  const [maxUsers, setMaxUsers] = useState(tenant?.max_users?.toString() || "");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async () => {
    if (!slug || !name) { setError("Slug and name are required"); return; }
    setSubmitting(true); setError(null);
    try {
      if (tenant) {
        await updateTenant(tenant.id, {
          name, description,
          max_cases: maxCases ? parseInt(maxCases) : null,
          max_users: maxUsers ? parseInt(maxUsers) : null,
        });
      } else {
        await createTenant({
          slug, name, description,
          max_cases: maxCases ? parseInt(maxCases) : null,
          max_users: maxUsers ? parseInt(maxUsers) : null,
        });
      }
      onSaved();
    } catch (e: any) {
      setError(e.message || "Commit failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Card style={{ marginBottom: "var(--space-lg)" }}>
      <div style={{ fontSize: 14, fontWeight: 600, marginBottom: "var(--space-md)" }}>
        {tenant ? "Edit Tenant" : "Create Tenant"}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-md)", marginBottom: "var(--space-md)" }}>
        <div>
          <Label>Slug (unique identifier)</Label>
          <input value={slug} onChange={e => setSlug(e.target.value.toLowerCase())}
            disabled={!!tenant}
            placeholder="acme-corp"
            style={inputStyle as any} />
        </div>
        <div>
          <Label>Display Name</Label>
          <input value={name} onChange={e => setName(e.target.value)}
            placeholder="Acme Corporation" style={inputStyle as any} />
        </div>
      </div>

      <div style={{ marginBottom: "var(--space-md)" }}>
        <Label>Description (optional)</Label>
        <textarea value={description} onChange={e => setDescription(e.target.value)}
          placeholder="What does this tenant do?" rows={2}
          style={{ ...inputStyle, resize: "vertical" } as any} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-md)", marginBottom: "var(--space-md)" }}>
        <div>
          <Label>Max Cases (optional)</Label>
          <input type="number" value={maxCases} onChange={e => setMaxCases(e.target.value)}
            placeholder="unlimited" style={inputStyle as any} />
        </div>
        <div>
          <Label>Max Users (optional)</Label>
          <input type="number" value={maxUsers} onChange={e => setMaxUsers(e.target.value)}
            placeholder="unlimited" style={inputStyle as any} />
        </div>
      </div>

      {error && <div style={{ color: "var(--status-failed)", fontSize: 12, marginBottom: "var(--space-md)" }}>{error}</div>}

      <div style={{ display: "flex", gap: "var(--space-sm)" }}>
        <Button onClick={handleSubmit} disabled={submitting}>
          {submitting ? "Committing..." : tenant ? "Commit Changes" : "Create Tenant"}
        </Button>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
    </Card>
  );
}

function TenantDetail({ tenantId }: { tenantId: string }) {
  const { data, loading, refetch } = useApi(() => listTenantMembers(tenantId), [tenantId]);
  const members = data ?? [];
  const [adding, setAdding] = useState(false);
  const [userId, setUserId] = useState("");
  const [role, setRole] = useState("");
  const [editingMember, setEditingMember] = useState<string | null>(null);
  const [editRole, setEditRole] = useState("");
  const allUsers = useUsers();
  const accessRoles = useAccessRoles();
  const defaultRole = accessRoles[0]?.name ?? "member";

  const handleAdd = async () => {
    if (!userId) return;
    try {
      await addTenantMember(tenantId, userId, role || defaultRole);
      setUserId(""); setRole(""); setAdding(false); refetch();
    } catch (e: any) {
      alert(e.message || "Failed to add member");
    }
  };

  const handleEdit = async (uid: string) => {
    try {
      await updateTenantMember(tenantId, uid, editRole);
      setEditingMember(null); refetch();
    } catch (e: any) {
      alert(e.message || "Failed to update member");
    }
  };

  return (
    <div style={{ marginTop: "var(--space-md)", paddingTop: "var(--space-md)",
      borderTop: "1px solid var(--border-subtle)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "var(--space-sm)" }}>
        <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase" }}>
          Members ({members.length})
        </div>
        <Button size="sm" variant="ghost" onClick={() => setAdding(true)}>+ Add Member</Button>
      </div>

      {adding && (
        <div style={{
          padding: "var(--space-md)", marginBottom: "var(--space-sm)",
          background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)",
          border: "1px solid var(--border-subtle)",
        }}>
          <div style={{ marginBottom: "var(--space-sm)" }}>
            <label style={{
              display: "block", fontSize: 10, fontWeight: 600, color: "var(--text-muted)",
              textTransform: "uppercase", fontFamily: "var(--font-mono)",
              marginBottom: 4, letterSpacing: "0.04em",
            }}>
              User <span style={{ color: "var(--status-failed)" }}>*</span>
            </label>
            <UserAutocomplete
              users={allUsers}
              value={userId}
              onChange={setUserId}
              autoFocus
            />
            {!userId && (
              <div style={{ fontSize: 10, color: "var(--status-failed)", marginTop: 2 }}>
                Required — select a user
              </div>
            )}
          </div>

          <div style={{ marginBottom: "var(--space-md)" }}>
            <label style={{
              display: "block", fontSize: 10, fontWeight: 600, color: "var(--text-muted)",
              textTransform: "uppercase", fontFamily: "var(--font-mono)",
              marginBottom: 4, letterSpacing: "0.04em",
            }}>Role</label>
            <RoleSelect roles={accessRoles} value={role || defaultRole} onChange={setRole} />
          </div>

          <div style={{ display: "flex", gap: "var(--space-xs)" }}>
            <Button size="sm" onClick={handleAdd} disabled={!userId.trim()}>Add Member</Button>
            <Button size="sm" variant="ghost" onClick={() => { setAdding(false); setUserId(""); }}>Cancel</Button>
          </div>
        </div>
      )}

      {loading && <Spinner size={20} />}

      {!loading && members.length === 0 && (
        <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "var(--space-sm)" }}>
          No members yet
        </div>
      )}

      {members.map((m: any) => (
        <div key={m.id} style={{ marginBottom: 4 }}>
          <div style={{
            display: "flex", justifyContent: "space-between", alignItems: "center",
            padding: "6px 10px", background: "var(--bg-elevated)",
            borderRadius: "var(--radius-sm)",
          }}>
            <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)" }}>
              <span style={{ fontSize: 12, fontWeight: 500 }}>{m.user_id}</span>
              <span style={{
                fontSize: 9, padding: "2px 6px",
                background: m.role === "owner" ? "var(--accent-dim)" : "transparent",
                color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase",
                borderRadius: 3, border: "1px solid var(--border-subtle)",
              }}>{m.role}</span>
            </div>
            <div style={{ display: "flex", gap: 4 }}>
              <Button size="sm" variant="ghost" onClick={() => {
                setEditingMember(editingMember === m.user_id ? null : m.user_id);
                setEditRole(m.role);
              }}>Edit</Button>
              <button onClick={async () => {
                if (confirm(`Remove ${m.user_id}?`)) {
                  await removeTenantMember(tenantId, m.user_id);
                  refetch();
                }
              }} style={{
                background: "transparent", border: "none", color: "var(--text-muted)",
                cursor: "pointer", fontSize: 14,
              }}>×</button>
            </div>
          </div>
          {editingMember === m.user_id && (
            <div style={{
              padding: "8px 10px", background: "var(--bg-elevated)",
              borderTop: "1px solid var(--border-subtle)",
              borderRadius: "0 0 var(--radius-sm) var(--radius-sm)",
              display: "flex", gap: "var(--space-sm)", alignItems: "center",
            }}>
              <label style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase" }}>Role</label>
              <RoleSelect roles={accessRoles} value={editRole || defaultRole} onChange={setEditRole} width={180} />
              <Button size="sm" onClick={() => handleEdit(m.user_id)}>Commit</Button>
              <Button size="sm" variant="ghost" onClick={() => setEditingMember(null)}>Cancel</Button>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function UserAutocomplete({ users, value, onChange, autoFocus }: {
  users: UserEntry[];
  value: string;
  onChange: (v: string) => void;
  autoFocus?: boolean;
}) {
  const [query, setQuery] = useState(value);
  const [open, setOpen] = useState(false);
  const [dropdownStyle, setDropdownStyle] = useState<React.CSSProperties>({});
  const inputRef = useRef<HTMLInputElement>(null);
  const ref = useRef<HTMLDivElement>(null);

  const filtered = query.length < 1 ? [] : users.filter(u => {
    const q = query.toLowerCase();
    return u.user_id.toLowerCase().includes(q) ||
      (u.email ?? "").toLowerCase().includes(q) ||
      (u.display_name ?? "").toLowerCase().includes(q);
  }).slice(0, 10);

  const updateDropdownPosition = () => {
    if (!inputRef.current) return;
    const rect = inputRef.current.getBoundingClientRect();
    setDropdownStyle({
      position: "fixed",
      top: rect.bottom + 4,
      left: rect.left,
      width: rect.width,
      zIndex: 9999,
    });
  };

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const select = (u: UserEntry) => {
    onChange(u.user_id);
    setQuery(u.display_name || u.email || u.user_id);
    setOpen(false);
  };

  return (
    <div ref={ref} style={{ position: "relative", width: "100%" }}>
      <input
        ref={inputRef}
        autoFocus={autoFocus}
        value={query}
        onChange={e => { setQuery(e.target.value); onChange(e.target.value); setOpen(true); updateDropdownPosition(); }}
        onFocus={() => { updateDropdownPosition(); setOpen(true); }}
        placeholder="Search by name, email or user ID…"
        style={{ ...inputStyle, width: "100%", borderColor: value ? "var(--border-default)" : "var(--status-failed)" } as any}
      />
      {open && filtered.length > 0 && createPortal(
        <div style={{
          ...dropdownStyle,
          background: "var(--bg-card)",
          border: "1px solid var(--border-default)",
          borderRadius: "var(--radius-sm)",
          boxShadow: "0 4px 16px rgba(0,0,0,0.2)",
          maxHeight: 220,
          overflowY: "auto",
        }}>
          {filtered.map(u => (
            <div key={u.user_id} onMouseDown={() => select(u)} style={{
              padding: "8px 12px", cursor: "pointer", display: "flex",
              flexDirection: "column", gap: 2,
              background: "var(--bg-card)",
            }}
              onMouseEnter={e => (e.currentTarget.style.background = "var(--bg-elevated)")}
              onMouseLeave={e => (e.currentTarget.style.background = "var(--bg-card)")}
            >
              <span style={{ fontSize: 13, fontWeight: 500, color: "var(--text-primary)" }}>
                {u.display_name || u.user_id}
              </span>
              <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                {u.email ?? u.user_id}
              </span>
            </div>
          ))}
        </div>,
        document.body
      )}
    </div>
  );
}

function RoleSelect({ roles, value, onChange, width }: {
  roles: AccessRole[];
  value: string;
  onChange: (v: string) => void;
  width?: number;
}) {
  const options = roles.length > 0 ? roles : [
    { name: "viewer", display_name: "Viewer" },
    { name: "member", display_name: "Member" },
    { name: "admin", display_name: "Admin" },
    { name: "owner", display_name: "Owner" },
  ];
  return (
    <select
      value={value}
      onChange={e => onChange(e.target.value)}
      style={{ ...inputStyle, width: width ?? 200 } as any}
    >
      {options.map(r => (
        <option key={r.name} value={r.name}>{r.display_name || r.name}</option>
      ))}
    </select>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return (
    <label style={{
      display: "block", fontSize: 10, fontWeight: 600, color: "var(--text-muted)",
      textTransform: "uppercase", fontFamily: "var(--font-mono)",
      marginBottom: 4, letterSpacing: "0.04em",
    }}>{children}</label>
  );
}

const inputStyle: React.CSSProperties = {
  width: "100%", padding: "8px 12px", fontSize: 13, fontFamily: "var(--font-body)",
  background: "var(--bg-input)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-primary)", outline: "none",
  boxSizing: "border-box",
};
