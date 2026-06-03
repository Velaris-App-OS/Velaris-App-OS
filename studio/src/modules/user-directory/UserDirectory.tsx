import React, { useEffect, useState, useRef } from "react";

type Entry = {
  id?: string;
  user_id: string;
  email?: string | null;
  display_name?: string | null;
  manager_user_id?: string | null;
  access_group_ids: string[];
  roles: string[];
  timezone: string;
  tenant_id?: string | null;
  is_active: boolean;
  metadata_json: Record<string, any>;
  created_at?: string;
  updated_at?: string;
};

type StatusFilter = "active" | "inactive" | "all";

const empty: Entry = {
  user_id: "", access_group_ids: [], roles: [], timezone: "UTC",
  is_active: true, metadata_json: {},
};

const USER_ID_RE = /^[a-zA-Z0-9._@-]+$/;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const TZ_RE = /^[A-Za-z][A-Za-z0-9_/+-]*$/;

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

function validate(e: Entry): string | null {
  if (!e.user_id.trim()) return "User ID is required";
  if (!USER_ID_RE.test(e.user_id)) return "User ID may only contain letters, digits, . _ @ -";
  if (e.email && !EMAIL_RE.test(e.email)) return "Email format is invalid";
  if (!e.timezone.trim()) return "Timezone is required";
  if (!TZ_RE.test(e.timezone)) return "Timezone format looks invalid (e.g. UTC, America/New_York)";
  if (e.manager_user_id && e.manager_user_id === e.user_id) return "User cannot be their own manager";
  if (e.manager_user_id && !USER_ID_RE.test(e.manager_user_id)) return "Manager ID has invalid characters";
  if (e.roles.some(r => !r.trim())) return "Empty role detected";
  if (e.access_group_ids.some(g => !g.trim())) return "Empty access group detected";
  return null;
}

export default function UserDirectory({ embedded }: { embedded?: boolean } = {}) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [filter, setFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("active");
  const [editing, setEditing] = useState<Entry | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [validationErr, setValidationErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [newPassword, setNewPassword] = useState("");
  const [requirePwChange, setRequirePwChange] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [syncResult, setSyncResult] = useState<{ created: number; scanned: number; new_user_ids: string[] } | null>(null);

  // ── Autocomplete data ─────────────────────────────────────────
  const [allRoles, setAllRoles]   = useState<{ id: string; name: string }[]>([]);
  const [allGroups, setAllGroups] = useState<{ id: string; name: string }[]>([]);

  async function load() {
    setErr(null);
    try {
      const params = new URLSearchParams();
      if (filter) params.set("q", filter);
      params.set("active_only", "false");
      const data = await apiJSON<Entry[]>(`/api/v1/user-directory?${params}`);
      setEntries(data);
    } catch (e: any) { setErr(e.message); }
  }

  async function loadAutocompleteData() {
    try {
      const [roles, groups] = await Promise.all([
        apiJSON<any[]>("/api/v1/access-roles").catch(() => []),
        apiJSON<any[]>("/api/v1/access-groups").catch(() => []),
      ]);
      setAllRoles(roles.map((r: any) => ({ id: r.id, name: r.name })));
      setAllGroups(groups.map((g: any) => ({ id: g.id, name: g.name })));
    } catch {}
  }

  useEffect(() => { load(); loadAutocompleteData(); }, []);

  async function syncFromDb() {
    setSyncing(true); setSyncResult(null); setErr(null);
    try {
      const data = await apiJSON<any>("/api/v1/user-directory/sync-from-db", { method: "POST" });
      setSyncResult(data);
      await load();
    } catch (e: any) { setErr(e.message); }
    finally { setSyncing(false); }
  }

  const filteredEntries = entries.filter(u => {
    if (statusFilter === "active") return u.is_active;
    if (statusFilter === "inactive") return !u.is_active;
    return true;
  });

  async function save() {
    if (!editing) return;
    const v = validate(editing);
    if (v) { setValidationErr(v); return; }

    const isNew = !editing.id;

    // New user: password is required to create their login account
    if (isNew && newPassword.length < 8) {
      setValidationErr("Initial password must be at least 8 characters.");
      return;
    }
    if (isNew && !editing.email?.trim()) {
      setValidationErr("Email is required when creating a new user.");
      return;
    }

    setValidationErr(null);
    setBusy(true); setErr(null);
    try {
      if (isNew) {
        // Step 1: create the login account (HelixUserModel) so the user can sign in
        try {
          await apiJSON("/api/v1/auth/real/register", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
              username: editing.user_id,
              email: editing.email,
              display_name: editing.display_name || null,
              password: newPassword,
              roles: editing.roles.length ? editing.roles : ["viewer"],
              password_change_required: requirePwChange,
            }),
          });
        } catch (e: any) {
          // 409 = auth account already exists — that's fine, just create the directory entry
          if (!e.message.includes("409") && !e.message.toLowerCase().includes("already in use")) {
            throw e;
          }
        }

        // Step 2: create the directory entry (UserDirectoryModel)
        await apiJSON("/api/v1/user-directory", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(editing),
        });
      } else {
        // Edit: update directory entry only (password managed via profile drawer)
        await apiJSON(`/api/v1/user-directory/${editing.user_id}`, {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(editing),
        });
      }

      setEditing(null);
      setNewPassword("");
      setRequirePwChange(true);
      await load();
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  }

  async function deactivate(user_id: string) {
    if (!confirm(`Deactivate ${user_id}?`)) return;
    setBusy(true); setErr(null);
    try {
      const r = await fetch(`/api/v1/user-directory/${user_id}`, { method: "DELETE", headers: _authHdr() });
      if (!r.ok && r.status !== 204) throw new Error(`Deactivate failed: ${r.status}`);
      await load();
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  }

  async function reactivate(user: Entry) {
    if (!confirm(`Reactivate ${user.user_id}?`)) return;
    setBusy(true);
    try {
      await apiJSON(`/api/v1/user-directory/${user.user_id}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ...user, is_active: true }),
      });
      await load();
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  }

  const counts = {
    active: entries.filter(u => u.is_active).length,
    inactive: entries.filter(u => !u.is_active).length,
    all: entries.length,
  };

  return (
    <div style={{ width: "100%", height: "100%", overflow: "auto", boxSizing: "border-box", fontFamily: "var(--font-body)" }}>

      {err && (
        <div style={{ color: "var(--status-failed)", background: "color-mix(in srgb, var(--status-failed) 10%, transparent)", border: "1px solid color-mix(in srgb, var(--status-failed) 25%, transparent)", borderRadius: "var(--radius-sm)", padding: "8px 12px", marginBottom: "var(--space-md)", fontSize: 12 }}>
          ⚠ {err}
        </div>
      )}

      {/* Toolbar */}
      <div style={{ display: "flex", gap: "var(--space-sm)", alignItems: "center", marginBottom: "var(--space-lg)", flexWrap: "wrap" }}>
        <input value={filter} onChange={e => setFilter(e.target.value)}
          onKeyDown={e => e.key === "Enter" && load()}
          placeholder="Search user_id / email / name"
          style={{ ...inp, width: 260, marginTop: 0 }} />
        <button onClick={load} style={btn()}>Search</button>

        <div style={{ display: "flex", gap: 4, marginLeft: "var(--space-sm)" }}>
          {(["active", "inactive", "all"] as StatusFilter[]).map(s => (
            <button key={s} onClick={() => setStatusFilter(s)} style={{
              ...btn(),
              background: statusFilter === s ? "var(--accent)" : "var(--bg-elevated)",
              color: statusFilter === s ? "#fff" : "var(--text-secondary)",
              border: `1px solid ${statusFilter === s ? "var(--accent)" : "var(--border-default)"}`,
              textTransform: "capitalize",
            }}>
              {s === "active" ? `Active (${counts.active})` : s === "inactive" ? `Inactive (${counts.inactive})` : `All (${counts.all})`}
            </button>
          ))}
        </div>

        <button onClick={syncFromDb} disabled={syncing}
          style={{ ...btn(), marginLeft: "auto", background: "#f0fdf4", color: "#16a34a", border: "1px solid #bbf7d0", fontWeight: 600 }}>
          {syncing ? "Syncing…" : "↻ Sync from DB"}
        </button>

        <button onClick={() => { setEditing({ ...empty }); setValidationErr(null); setNewPassword(""); setRequirePwChange(true); setErr(null); setSyncResult(null); }}
          style={{ ...btn(), background: "var(--accent)", color: "#fff", border: "1px solid var(--accent)", fontWeight: 600 }}>
          + New user
        </button>
      </div>

      {syncResult && (
        <div style={{ marginBottom: 16, padding: "10px 14px", borderRadius: 6, background: syncResult.created > 0 ? "#f0fdf4" : "#f8fafc", border: `1px solid ${syncResult.created > 0 ? "#bbf7d0" : "#e2e8f0"}`, fontSize: 12 }}>
          <span style={{ fontWeight: 700, color: syncResult.created > 0 ? "#16a34a" : "#64748b" }}>
            {syncResult.created > 0
              ? `✓ Synced ${syncResult.created} new user${syncResult.created !== 1 ? "s" : ""} from DB`
              : "✓ All DB users already in directory — nothing to add"}
          </span>
          {syncResult.created > 0 && (
            <span style={{ marginLeft: 10, color: "#64748b" }}>
              ({syncResult.new_user_ids.join(", ")})
            </span>
          )}
          <span style={{ marginLeft: 10, color: "#94a3b8" }}>· {syncResult.scanned} unique IDs scanned</span>
        </div>
      )}

      {/* Table */}
      <div style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)", overflow: "hidden", width: "100%" }}>
        <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1.2fr 1.8fr 1.2fr 1.3fr 1.3fr 0.7fr 1fr", background: "var(--bg-elevated)", padding: "8px 16px", fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em" }}>
          <span>User ID</span><span>Display Name</span><span>Email</span><span>Manager</span><span>Roles</span><span>Groups</span><span>Status</span><span></span>
        </div>
        {filteredEntries.map((u, i) => (
          <div key={u.user_id} style={{ display: "grid", gridTemplateColumns: "1.4fr 1.2fr 1.8fr 1.2fr 1.3fr 1.3fr 0.7fr 1fr", alignItems: "center", padding: "10px 16px", borderTop: "1px solid var(--border-subtle)", background: i % 2 === 0 ? "var(--bg-card)" : "var(--bg-panel)", opacity: u.is_active ? 1 : 0.55, fontSize: 13 }}>
            <code style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--text-primary)" }}>{u.user_id}</code>
            <span style={{ color: "var(--text-primary)" }}>{u.display_name || "—"}</span>
            <span style={{ color: "var(--text-secondary)", fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{u.email || "—"}</span>
            <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--text-muted)" }}>{u.manager_user_id || "—"}</span>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 3 }}>
              {u.roles.length ? u.roles.map(r => <Chip key={r}>{r}</Chip>) : <span style={{ color: "var(--text-muted)" }}>—</span>}
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 3 }}>
              {u.access_group_ids.length ? u.access_group_ids.map(g => <Chip key={g}>{allGroups.find(ag => ag.id === g)?.name || g}</Chip>) : <span style={{ color: "var(--text-muted)" }}>—</span>}
            </div>
            <span style={{ fontSize: 11, fontWeight: 600, color: u.is_active ? "var(--status-completed)" : "var(--text-muted)" }}>
              {u.is_active ? "Active" : "Inactive"}
            </span>
            <div style={{ display: "flex", gap: 4 }}>
              <button onClick={() => { setEditing({ ...u }); setValidationErr(null); }} style={btn()}>Edit</button>
              {u.is_active
                ? <button onClick={() => deactivate(u.user_id)} style={{ ...btn(), color: "var(--status-failed)", borderColor: "color-mix(in srgb, var(--status-failed) 30%, var(--border-default))" }}>Deactivate</button>
                : <button onClick={() => reactivate(u)} style={{ ...btn(), color: "var(--status-completed)", borderColor: "color-mix(in srgb, var(--status-completed) 30%, var(--border-default))" }}>Reactivate</button>}
            </div>
          </div>
        ))}
        {filteredEntries.length === 0 && (
          <div style={{ padding: "var(--space-2xl)", color: "var(--text-muted)", fontSize: 13, background: "var(--bg-card)" }}>
            {entries.length === 0 ? "No users yet. Click + New user to get started." : "No users match the current filter."}
          </div>
        )}
      </div>

      {/* Edit / Create modal */}
      {editing && (
        <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000, backdropFilter: "blur(2px)" }}>
          <div style={{ background: "var(--bg-panel)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-md)", width: 540, maxHeight: "88vh", overflow: "auto", boxShadow: "0 20px 60px rgba(0,0,0,0.35)" }}>
            {/* Modal header */}
            <div style={{ padding: "var(--space-lg) var(--space-xl)", borderBottom: "1px solid var(--border-subtle)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div>
                <div style={{ fontSize: 16, fontWeight: 700, color: "var(--text-primary)" }}>{editing.id ? "Edit User" : "New User"}</div>
                {!editing.id && (
                  <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 3 }}>
                    Creates both a <strong style={{ color: "var(--text-secondary)" }}>login account</strong> and a <strong style={{ color: "var(--text-secondary)" }}>directory profile</strong>.
                  </div>
                )}
              </div>
              <button onClick={() => { setEditing(null); setValidationErr(null); }} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 18, color: "var(--text-muted)" }}>✕</button>
            </div>

            <div style={{ padding: "var(--space-lg) var(--space-xl)" }}>
              {validationErr && (
                <div style={{ background: "color-mix(in srgb, var(--status-failed) 10%, transparent)", color: "var(--status-failed)", border: "1px solid color-mix(in srgb, var(--status-failed) 25%, transparent)", padding: "8px 12px", borderRadius: "var(--radius-sm)", marginBottom: "var(--space-md)", fontSize: 12 }}>
                  ⚠ {validationErr}
                </div>
              )}

              <label style={lbl}>Username / User ID *</label>
              <input style={inp} value={editing.user_id} disabled={!!editing.id} placeholder="e.g. john.doe" onChange={e => setEditing({ ...editing, user_id: e.target.value })} />
              <label style={lbl}>Display Name</label>
              <input style={inp} value={editing.display_name || ""} placeholder="Full name" onChange={e => setEditing({ ...editing, display_name: e.target.value })} />
              <label style={lbl}>Email {!editing.id && "*"}</label>
              <input style={inp} value={editing.email || ""} onChange={e => setEditing({ ...editing, email: e.target.value })} placeholder="user@example.com" />

              {/* Login credentials — new users only */}
              {!editing.id && (
                <div style={{ background: "var(--accent-dim)", border: "1px solid color-mix(in srgb, var(--accent) 25%, transparent)", borderRadius: "var(--radius-sm)", padding: "10px 14px", margin: "var(--space-md) 0 var(--space-sm)" }}>
                  <div style={{ fontSize: 10, fontWeight: 700, color: "var(--accent)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.07em", fontFamily: "var(--font-mono)" }}>
                    Login Credentials
                  </div>
                  <label style={lbl}>Initial password * <span style={{ color: "var(--text-muted)", fontWeight: 400 }}>(min 8 chars)</span></label>
                  <input type="password" style={inp} value={newPassword} onChange={e => setNewPassword(e.target.value)} placeholder="Temporary password" autoComplete="new-password" />
                  <label style={{ ...lbl, display: "flex", alignItems: "center", gap: 6, marginTop: 8, cursor: "pointer" }}>
                    <input type="checkbox" checked={requirePwChange} onChange={e => setRequirePwChange(e.target.checked)} />
                    Require password change on first login
                  </label>
                </div>
              )}

              <label style={lbl}>Manager</label>
              <ManagerSelect value={editing.manager_user_id || ""} onChange={(v) => setEditing({ ...editing, manager_user_id: v })} users={entries.filter(e => e.user_id !== editing.user_id)} />

              <label style={lbl}>Roles</label>
              <AutocompleteChipInput values={editing.roles} onChange={(v) => setEditing({ ...editing, roles: v })}
                options={allRoles.map(r => ({ value: r.name, label: r.name }))} placeholder="Select or type a role…" storeValue="value" />
              {allRoles.length === 0 && <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 3 }}>No roles found — create roles in the Access Roles tab.</div>}

              <label style={lbl}>Access Groups</label>
              <AutocompleteChipInput values={editing.access_group_ids} onChange={(v) => setEditing({ ...editing, access_group_ids: v })}
                options={allGroups.map(g => ({ value: g.id, label: g.name }))} placeholder="Select or type a group…" storeValue="value"
                displayValue={(v) => allGroups.find(g => g.id === v)?.name || v} />
              {allGroups.length === 0 && <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 3 }}>No groups found — create groups in the Access Groups tab.</div>}

              <label style={lbl}>Timezone *</label>
              <input style={inp} value={editing.timezone} onChange={e => setEditing({ ...editing, timezone: e.target.value })} placeholder="UTC, America/New_York, Asia/Kolkata" />

              <label style={{ ...lbl, display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                <input type="checkbox" checked={editing.is_active} onChange={e => setEditing({ ...editing, is_active: e.target.checked })} />
                Active
              </label>
            </div>

            <div style={{ padding: "var(--space-md) var(--space-xl)", borderTop: "1px solid var(--border-subtle)", display: "flex", gap: "var(--space-sm)", justifyContent: "flex-end", background: "var(--bg-elevated)" }}>
              <button onClick={() => { setEditing(null); setValidationErr(null); }} style={btn()}>Cancel</button>
              <button onClick={save} style={{ ...btn(), background: "var(--accent)", color: "#fff", border: "1px solid var(--accent)", fontWeight: 600 }} disabled={busy}>
                {busy ? "Committing…" : "Commit"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ── ManagerSelect: searchable dropdown of users ─────────────────────── */

function ManagerSelect({ value, onChange, users }: {
  value: string; onChange: (v: string) => void; users: Entry[];
}) {
  const [q, setQ] = useState(value);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const u = users.find(u => u.user_id === value);
    setQ(u ? `${u.display_name || u.user_id} (${u.user_id})` : value);
  }, [value]);

  useEffect(() => {
    const h = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);

  const filtered = users.filter(u => {
    const s = q.toLowerCase();
    return u.user_id.toLowerCase().includes(s) || (u.display_name || "").toLowerCase().includes(s) || (u.email || "").toLowerCase().includes(s);
  }).slice(0, 10);

  return (
    <div ref={ref} style={{ position: "relative", marginTop: 2 }}>
      <input style={inp} value={q} onChange={e => { setQ(e.target.value); setOpen(true); if (!e.target.value) onChange(""); }} onFocus={() => setOpen(true)} placeholder="Search by name, username, or email…" />
      {value && (
        <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2, fontFamily: "var(--font-mono)" }}>
          Selected: {value}
          <button onClick={() => { onChange(""); setQ(""); }} style={{ marginLeft: 6, border: "none", background: "none", cursor: "pointer", color: "var(--status-failed)", fontSize: 11 }}>✕ clear</button>
        </div>
      )}
      {open && q.length > 0 && filtered.length > 0 && (
        <div style={dropdown}>
          {filtered.map(u => (
            <div key={u.user_id} onMouseDown={() => { onChange(u.user_id); setQ(`${u.display_name || u.user_id} (${u.user_id})`); setOpen(false); }}
              style={ddItem} onMouseEnter={e => (e.currentTarget.style.background = "var(--bg-card-hover)")} onMouseLeave={e => (e.currentTarget.style.background = "transparent")}>
              <div style={{ fontWeight: 600, color: "var(--text-primary)" }}>{u.display_name || u.user_id}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{u.user_id}{u.email ? ` · ${u.email}` : ""}</div>
            </div>
          ))}
        </div>
      )}
      {open && q.length > 0 && filtered.length === 0 && (
        <div style={{ ...dropdown, padding: "8px 12px", fontSize: 12, color: "var(--text-muted)" }}>No users match "{q}"</div>
      )}
    </div>
  );
}

/* ── AutocompleteChipInput: chip input with dropdown suggestions ─────── */

function AutocompleteChipInput({ values, onChange, options, placeholder, storeValue, displayValue }: {
  values: string[];
  onChange: (v: string[]) => void;
  options: { value: string; label: string }[];
  placeholder?: string;
  storeValue: "value" | "label";
  displayValue?: (v: string) => string;
}) {
  const [draft, setDraft] = useState("");
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const filtered = options.filter(o =>
    !values.includes(o[storeValue]) &&
    (o.label.toLowerCase().includes(draft.toLowerCase()) || o.value.toLowerCase().includes(draft.toLowerCase()))
  ).slice(0, 10);

  function addOption(opt: { value: string; label: string }) {
    const stored = opt[storeValue];
    if (!values.includes(stored)) onChange([...values, stored]);
    setDraft(""); setOpen(false);
  }

  function commitDraft() {
    const t = draft.trim();
    if (!t) return;
    const parts = t.split(",").map(s => s.trim()).filter(Boolean);
    const next = Array.from(new Set([...values, ...parts]));
    onChange(next); setDraft("");
  }

  function remove(v: string) { onChange(values.filter(x => x !== v)); }

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <div style={{ border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", padding: "4px 6px", marginTop: 2, background: "var(--bg-input)", minHeight: 36, display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
        {values.map(v => (
          <span key={v} style={{ background: "var(--accent-dim)", color: "var(--accent)", padding: "2px 8px", borderRadius: 12, fontSize: 12, display: "inline-flex", alignItems: "center", gap: 4 }}>
            {displayValue ? displayValue(v) : v}
            <button onClick={() => remove(v)} style={{ border: "none", background: "transparent", color: "var(--accent)", cursor: "pointer", padding: 0, fontSize: 14, lineHeight: 1 }}>×</button>
          </span>
        ))}
        <input value={draft} onChange={e => { setDraft(e.target.value); setOpen(true); }} onFocus={() => setOpen(true)}
          onKeyDown={e => {
            if ((e.key === "Enter" || e.key === "Tab") && draft.trim()) { e.preventDefault(); commitDraft(); }
            else if (e.key === "Backspace" && !draft && values.length) onChange(values.slice(0, -1));
          }}
          onBlur={() => draft.trim() && commitDraft()}
          placeholder={values.length === 0 ? placeholder : ""}
          style={{ border: "none", outline: "none", flex: 1, minWidth: 120, fontSize: 13, background: "transparent", color: "var(--text-primary)" }}
        />
      </div>
      {open && (filtered.length > 0 || draft.trim()) && (
        <div style={dropdown}>
          {filtered.map(opt => (
            <div key={opt.value} onMouseDown={() => addOption(opt)} style={ddItem}
              onMouseEnter={e => (e.currentTarget.style.background = "var(--bg-card-hover)")} onMouseLeave={e => (e.currentTarget.style.background = "transparent")}>
              <span style={{ fontWeight: 600, color: "var(--text-primary)" }}>{opt.label}</span>
              {opt.label !== opt.value && <span style={{ fontSize: 10, color: "var(--text-muted)", marginLeft: 6, fontFamily: "var(--font-mono)" }}>{opt.value.slice(0, 16)}…</span>}
            </div>
          ))}
          {draft.trim() && !options.find(o => o.label === draft.trim()) && (
            <div onMouseDown={commitDraft} style={{ ...ddItem, color: "var(--accent)", borderTop: "1px solid var(--border-subtle)" }}
              onMouseEnter={e => (e.currentTarget.style.background = "var(--bg-card-hover)")} onMouseLeave={e => (e.currentTarget.style.background = "transparent")}>
              + Add "{draft.trim()}" as custom value
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ── ChipInput: tag-style multi-value input with comma support ──────── */

function ChipInput({
  values, onChange, placeholder,
}: { values: string[]; onChange: (v: string[]) => void; placeholder?: string }) {
  const [draft, setDraft] = useState("");

  function commit() {
    // Splits on commas so user can paste "a, b, c" or just type
    const parts = draft.split(",").map(s => s.trim()).filter(Boolean);
    if (parts.length === 0) return;
    const next = Array.from(new Set([...values, ...parts]));
    onChange(next);
    setDraft("");
  }

  function remove(v: string) {
    onChange(values.filter(x => x !== v));
  }

  return (
    <div style={{
      border: "1px solid #ccc", borderRadius: 3, padding: "4px 6px",
      marginTop: 2, background: "#fff", minHeight: 32,
      display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center",
    }}>
      {values.map(v => (
        <span key={v} style={{
          background: "#eef3ff", color: "#345", padding: "2px 8px",
          borderRadius: 12, fontSize: 12, display: "inline-flex", alignItems: "center", gap: 4,
        }}>
          {v}
          <button onClick={() => remove(v)}
            style={{ border: "none", background: "transparent", color: "#789", cursor: "pointer", padding: 0, fontSize: 14, lineHeight: 1 }}>
            ×
          </button>
        </span>
      ))}
      <input
        value={draft}
        onChange={e => {
          const val = e.target.value;
          if (val.endsWith(",")) {
            // Auto-commit on comma
            setDraft(val);
            setTimeout(() => commit(), 0);
          } else {
            setDraft(val);
          }
        }}
        onKeyDown={e => {
          if (e.key === "Enter" || e.key === "Tab") {
            if (draft.trim()) { e.preventDefault(); commit(); }
          } else if (e.key === "Backspace" && !draft && values.length) {
            onChange(values.slice(0, -1));
          }
        }}
        onBlur={() => draft.trim() && commit()}
        placeholder={values.length === 0 ? placeholder : ""}
        style={{ border: "none", outline: "none", flex: 1, minWidth: 100, fontSize: 13 }}
      />
    </div>
  );
}

function Chip({ children }: { children: React.ReactNode }) {
  return (
    <span style={{
      background: "var(--bg-elevated)", color: "var(--text-secondary)",
      padding: "1px 7px", borderRadius: 10, fontSize: 10,
      fontFamily: "var(--font-mono)", display: "inline-block",
    }}>{children}</span>
  );
}

const lbl: React.CSSProperties = {
  fontSize: 10, color: "var(--text-muted)", display: "block", marginTop: "var(--space-md)",
  fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.06em", fontWeight: 600,
};
const inp: React.CSSProperties = {
  width: "100%", padding: "7px 10px", fontSize: 13, fontFamily: "var(--font-body)",
  border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", marginTop: 4,
  background: "var(--bg-input)", color: "var(--text-primary)", outline: "none", boxSizing: "border-box",
};
const dropdown: React.CSSProperties = {
  position: "absolute", top: "100%", left: 0, right: 0, marginTop: 2,
  background: "var(--bg-panel)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", zIndex: 300, maxHeight: 220, overflowY: "auto",
  boxShadow: "0 4px 16px rgba(0,0,0,0.25)",
};
const ddItem: React.CSSProperties = {
  padding: "8px 12px", cursor: "pointer", fontSize: 13,
  borderBottom: "1px solid var(--border-subtle)", background: "transparent", transition: "background 0.1s",
};
function btn(): React.CSSProperties {
  return {
    padding: "5px 12px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)",
    background: "var(--bg-elevated)", color: "var(--text-secondary)", fontSize: 12,
    cursor: "pointer", fontFamily: "var(--font-body)",
  };
}
