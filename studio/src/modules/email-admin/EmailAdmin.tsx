// HELIX P25b — Email Admin (accounts + templates)
import React, { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

type Account = {
  id?: string;
  name: string;
  address: string;
  smtp_host: string;
  smtp_port: number;
  smtp_username: string | null;
  smtp_password?: string | null;
  smtp_password_set?: boolean;
  smtp_use_tls: boolean;
  imap_host: string | null;
  imap_port: number;
  imap_username: string | null;
  imap_password?: string | null;
  imap_password_set?: boolean;
  imap_use_ssl: boolean;
  imap_folder: string;
  poll_interval_seconds: number;
  is_active: boolean;
  is_default_outbound: boolean;
  tenant_id?: string | null;
};

type Template = {
  id?: string;
  name: string;
  description: string;
  subject: string;
  body_text: string;
  body_html: string | null;
  engine: "jinja2" | "fstring";
  scope: "global" | "case_type";
  case_type_id: string | null;
  is_active: boolean;
};

type Tab = "accounts" | "templates";

const emptyAccount: Account = {
  name: "", address: "",
  smtp_host: "localhost", smtp_port: 1025,
  smtp_username: "", smtp_password: "", smtp_use_tls: false,
  imap_host: "localhost", imap_port: 1143,
  imap_username: "", imap_password: "", imap_use_ssl: false,
  imap_folder: "INBOX",
  poll_interval_seconds: 15,
  is_active: true, is_default_outbound: false,
};

const PROVIDER_PRESETS: Record<string, Partial<Account>> = {
  gmail: {
    smtp_host: "smtp.gmail.com", smtp_port: 587, smtp_use_tls: true,
    imap_host: "imap.gmail.com", imap_port: 993, imap_use_ssl: true,
    imap_folder: "INBOX",
  },
  outlook: {
    smtp_host: "smtp.office365.com", smtp_port: 587, smtp_use_tls: true,
    imap_host: "outlook.office365.com", imap_port: 993, imap_use_ssl: true,
    imap_folder: "INBOX",
  },
};

const emptyTemplate: Template = {
  name: "", description: "",
  subject: "", body_text: "", body_html: "",
  engine: "jinja2", scope: "global", case_type_id: null, is_active: true,
};

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

function validateAccount(a: Account): string | null {
  if (!a.name.trim()) return "Name is required";
  if (!a.address.trim() || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(a.address)) return "Invalid 'from' address";
  if (!a.smtp_host.trim()) return "SMTP host required";
  if (a.smtp_port < 1 || a.smtp_port > 65535) return "SMTP port out of range";
  if (a.imap_host && (a.imap_port < 1 || a.imap_port > 65535)) return "IMAP port out of range";
  return null;
}

function validateTemplate(t: Template): string | null {
  if (!t.name.trim()) return "Name required";
  if (!t.subject.trim()) return "Subject required";
  if (!t.body_text.trim()) return "Body text required";
  if (t.scope === "case_type" && !t.case_type_id) return "case_type_id required for case-type scope";
  return null;
}

export default function EmailAdmin() {
  const [tab, setTab] = useState<Tab>("accounts");
  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", height: "100%", overflow: "auto", boxSizing: "border-box" }}>
      {/* Tabs — Work Center style */}
      <div style={{ display: "flex", marginBottom: "var(--space-xl)" }}>
        <div style={{ display: "flex", background: "var(--bg-card)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)" }}>
          {(["accounts", "templates"] as Tab[]).map(t => (
            <button key={t} onClick={() => setTab(t)} style={{
              padding: "8px 18px", fontSize: 12, fontWeight: 500,
              fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.04em",
              border: "none", cursor: "pointer", borderRadius: "var(--radius-sm)",
              color: tab === t ? "var(--accent)" : "var(--text-muted)",
              background: tab === t ? "var(--accent-dim)" : "transparent",
            }}>{t}</button>
          ))}
        </div>
      </div>
      {tab === "accounts" ? <AccountsPanel /> : <TemplatesPanel />}
    </div>
  );
}

/* ─── Accounts ─────────────────────────────────────────────────── */

function AccountsPanel() {
  const navigate = useNavigate();
  const [items, setItems] = useState<Account[]>([]);
  const [editing, setEditing] = useState<Account | null>(null);
  const [smtpPwStored, setSmtpPwStored] = useState(false);
  const [imapPwStored, setImapPwStored] = useState(false);
  const [smtpChangingPw, setSmtpChangingPw] = useState(false);
  const [imapChangingPw, setImapChangingPw] = useState(false);
  const [testResults, setTestResults] = useState<Record<string, { smtp: string | null; imap: string | null; available_folders?: string[] }>>({});
  const [testingId, setTestingId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [vErr, setVErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    setErr(null);
    try {
      const loaded = await apiJSON<Account[]>("/api/v1/email/accounts?active_only=false");
      setItems(loaded);
    } catch (e: any) { setErr(e.message); }
  }
  useEffect(() => { load(); }, []);

  function openNew() {
    setEditing({ ...emptyAccount });
    setVErr(null);
    setSmtpPwStored(false); setImapPwStored(false);
    setSmtpChangingPw(false); setImapChangingPw(false);
  }

  function openEdit(a: Account) {
    setEditing({ ...a, smtp_password: "", imap_password: "" });
    setSmtpPwStored(!!a.smtp_password_set);
    setImapPwStored(!!a.imap_password_set);
    setSmtpChangingPw(false);
    setImapChangingPw(false);
    setVErr(null);
  }

  function closeModal() {
    setEditing(null); setVErr(null);
    setSmtpChangingPw(false); setImapChangingPw(false);
  }

  async function save() {
    if (!editing) return;
    const v = validateAccount(editing);
    if (v) { setVErr(v); return; }
    setVErr(null); setBusy(true); setErr(null);
    try {
      const isNew = !editing.id;
      const url = isNew ? "/api/v1/email/accounts" : `/api/v1/email/accounts/${editing.id}`;
      const method = isNew ? "POST" : "PATCH";
      const body = { ...editing };
      if (body.smtp_password === "") body.smtp_password = null;
      if (body.imap_password === "") body.imap_password = null;
      await apiJSON(url, { method, headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
      closeModal();
      await load();
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  }

  async function deactivate(id: string) {
    if (!confirm("Deactivate this account? It will stop polling but remain in the list.")) return;
    setBusy(true);
    try {
      await apiJSON(`/api/v1/email/accounts/${id}/deactivate`, { method: "PATCH" });
      await load();
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  }

  async function hardDelete(id: string) {
    if (!confirm("Permanently delete this account? This cannot be undone.")) return;
    setBusy(true);
    try {
      await fetch(`/api/v1/email/accounts/${id}`, { method: "DELETE", headers: _authHdr() });
      await load();
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  }

  async function testConnection(id: string) {
    setTestingId(id);
    try {
      const r = await apiJSON<{ smtp: string | null; imap: string | null; available_folders?: string[] }>(
        `/api/v1/email/accounts/${id}/test-connection`, { method: "POST" }
      );
      setTestResults(prev => ({ ...prev, [id]: r }));
    } catch (e: any) {
      setTestResults(prev => ({ ...prev, [id]: { smtp: e.message, imap: e.message } }));
    } finally {
      setTestingId(null);
    }
  }

  return (
    <div>
      {err && <div style={errBanner}>⚠ {err}</div>}
      <div style={{ marginBottom: "var(--space-md)", display: "flex", alignItems: "center", gap: "var(--space-md)" }}>
        <button onClick={openNew} style={primaryBtn}>+ New account</button>
        <span style={{ color: "var(--text-muted)", fontSize: 12 }}>
          Mailpit dev preset: SMTP localhost:1025, IMAP localhost:1143, no TLS, no auth needed.
        </span>
        <button onClick={() => navigate("/inbox")}
          style={{ ...btn, marginLeft: "auto", color: "var(--accent)", borderColor: "var(--accent)", whiteSpace: "nowrap" }}>
          → Open Inbox
        </button>
      </div>
      <table style={tableStyle}>
        <thead><tr>
          {["Name", "Address", "SMTP", "IMAP", "Default out", "Active", ""].map(h => (
            <th key={h} style={th}>{h}</th>
          ))}
        </tr></thead>
        <tbody>
          {items.length === 0 && <tr><td colSpan={7} style={{ ...td, color: "var(--text-muted)" }}>No accounts yet.</td></tr>}
          {items.map(a => {
            const tr = testResults[a.id!];
            return (
              <React.Fragment key={a.id}>
                <tr style={{ opacity: a.is_active ? 1 : 0.55 }}>
                  <td style={td}>
                    <span style={{ fontWeight: a.is_default_outbound ? 600 : 400 }}>{a.name}</span>
                    {a.is_default_outbound && (
                      <span style={{ marginLeft: 6, fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--accent)", background: "var(--accent-dim)", padding: "1px 6px", borderRadius: 100 }}>default</span>
                    )}
                  </td>
                  <td style={{ ...td, fontFamily: "var(--font-mono)", fontSize: 12 }}>{a.address}</td>
                  <td style={td}>{a.smtp_host}:{a.smtp_port}{a.smtp_use_tls ? " (TLS)" : ""}</td>
                  <td style={td}>{a.imap_host ? `${a.imap_host}:${a.imap_port} · ${a.imap_folder}` : "—"}</td>
                  <td style={td}>{a.is_default_outbound ? "✓" : "—"}</td>
                  <td style={td}>
                    <span style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: a.is_active ? "#22c55e" : "var(--text-muted)" }}>
                      {a.is_active ? "● Active" : "● Inactive"}
                    </span>
                  </td>
                  <td style={td}>
                    <button onClick={() => openEdit(a)} style={btn}>Edit</button>{" "}
                    <button onClick={() => testConnection(a.id!)} disabled={testingId === a.id}
                      style={{ ...btn, color: "var(--accent)" }}>
                      {testingId === a.id ? "…" : "Test"}
                    </button>{" "}
                    {a.is_active
                      ? <button onClick={() => deactivate(a.id!)} style={{ ...btn, color: "var(--status-warning, #f59e0b)" }}>Deactivate</button>
                      : <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", padding: "0 4px" }}>inactive</span>
                    }{" "}
                    <button onClick={() => hardDelete(a.id!)} style={{ ...btn, color: "var(--status-failed)" }}>Delete</button>
                  </td>
                </tr>
                {tr && (
                  <tr>
                    <td colSpan={7} style={{ ...td, paddingTop: 6, paddingBottom: 8, background: "var(--bg-elevated)" }}>
                      <div style={{ display: "flex", gap: 24, flexWrap: "wrap", alignItems: "flex-start" }}>
                        <span>
                          <strong>SMTP:</strong>{" "}
                          <span style={{ color: tr.smtp === "ok" ? "#22c55e" : "#ef4444" }}>{tr.smtp === "ok" ? "✓ connected" : tr.smtp}</span>
                        </span>
                        <span>
                          <strong>IMAP:</strong>{" "}
                          <span style={{ color: tr.imap === "ok" ? "#22c55e" : tr.imap === "not configured" ? "var(--text-muted)" : "#ef4444" }}>{tr.imap === "ok" ? "✓ connected" : tr.imap}</span>
                        </span>
                        {tr.available_folders && tr.available_folders.length > 0 && (
                          <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                            <strong>Available folders:</strong>{" "}
                            {tr.available_folders.map((f, i) => (
                              <code key={i} style={{ fontFamily: "var(--font-mono)", background: "var(--bg-card)", padding: "1px 5px", borderRadius: 3, marginRight: 4, fontSize: 11 }}>{f}</code>
                            ))}
                          </span>
                        )}
                      </div>
                    </td>
                  </tr>
                )}
              </React.Fragment>
            );
          })}
        </tbody>
      </table>

      {editing && (
        <Modal title={editing.id ? "Edit account" : "New account"}
               onCancel={closeModal}
               onSave={save} busy={busy} validationErr={vErr}>

          {/* Provider presets */}
          <div style={{ display: "flex", gap: 6, marginBottom: "var(--space-md)", alignItems: "center", flexWrap: "wrap" }}>
            <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.04em" }}>Quick preset:</span>
            {Object.entries(PROVIDER_PRESETS).map(([key, preset]) => (
              <button key={key} style={{ ...btn, fontSize: 11, padding: "3px 10px", textTransform: "capitalize" }}
                onClick={() => setEditing({ ...editing, ...preset })}>
                {key === "gmail" ? "Gmail" : "Outlook / 365"}
              </button>
            ))}
            {editing.smtp_host === "smtp.gmail.com" && (
              <span style={{ fontSize: 11, color: "#f59e0b", marginLeft: 4 }}>
                Gmail requires an <strong>App Password</strong> — not your regular password.
                <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noreferrer"
                   style={{ color: "var(--accent)", marginLeft: 4 }}>Generate one ↗</a>
              </span>
            )}
          </div>

          <Field label="Name *">
            <input style={inp} value={editing.name} onChange={e => setEditing({ ...editing, name: e.target.value })} />
          </Field>
          <Field label="From address *">
            <input style={inp} value={editing.address} placeholder="you@gmail.com"
              onChange={e => setEditing({ ...editing, address: e.target.value })} />
          </Field>

          <Section title="SMTP (outbound)">
            <Row>
              <Field label="Host *" w="2fr"><input style={inp} value={editing.smtp_host} onChange={e => setEditing({ ...editing, smtp_host: e.target.value })} /></Field>
              <Field label="Port *" w="1fr"><input type="number" style={inp} value={editing.smtp_port} onChange={e => setEditing({ ...editing, smtp_port: Number(e.target.value) })} /></Field>
              <Field label="TLS" w="auto">
                <input type="checkbox" checked={editing.smtp_use_tls} onChange={e => setEditing({ ...editing, smtp_use_tls: e.target.checked })} />
              </Field>
            </Row>
            <Row>
              <Field label="Username"><input style={inp} value={editing.smtp_username || ""} onChange={e => setEditing({ ...editing, smtp_username: e.target.value })} /></Field>
              <Field label="Password">
                {smtpPwStored && !smtpChangingPw ? (
                  <PasswordSavedBadge onChange={() => setSmtpChangingPw(true)} />
                ) : (
                  <PasswordChangeInput
                    value={editing.smtp_password || ""}
                    autoFocus={smtpChangingPw}
                    placeholder={smtpPwStored ? "Enter new app password" : "App password"}
                    onChange={v => setEditing({ ...editing, smtp_password: v })}
                    onCancel={smtpPwStored ? () => {
                      setSmtpChangingPw(false);
                      setEditing({ ...editing, smtp_password: "" });
                    } : undefined}
                  />
                )}
              </Field>
            </Row>
          </Section>

          <Section title="IMAP (inbound polling, optional)">
            <Row>
              <Field label="Host" w="2fr"><input style={inp} value={editing.imap_host || ""} onChange={e => setEditing({ ...editing, imap_host: e.target.value })} /></Field>
              <Field label="Port" w="1fr"><input type="number" style={inp} value={editing.imap_port} onChange={e => setEditing({ ...editing, imap_port: Number(e.target.value) })} /></Field>
              <Field label="SSL" w="auto"><input type="checkbox" checked={editing.imap_use_ssl} onChange={e => setEditing({ ...editing, imap_use_ssl: e.target.checked })} /></Field>
            </Row>
            <Row>
              <Field label="Username"><input style={inp} value={editing.imap_username || ""} onChange={e => setEditing({ ...editing, imap_username: e.target.value })} /></Field>
              <Field label="Password">
                {imapPwStored && !imapChangingPw ? (
                  <PasswordSavedBadge onChange={() => setImapChangingPw(true)} />
                ) : (
                  <PasswordChangeInput
                    value={editing.imap_password || ""}
                    autoFocus={imapChangingPw}
                    placeholder={imapPwStored ? "Enter new app password" : "App password"}
                    onChange={v => setEditing({ ...editing, imap_password: v })}
                    onCancel={imapPwStored ? () => {
                      setImapChangingPw(false);
                      setEditing({ ...editing, imap_password: "" });
                    } : undefined}
                  />
                )}
              </Field>
            </Row>
            <Row>
              <Field label="Folder">
                <input style={inp} value={editing.imap_folder} onChange={e => setEditing({ ...editing, imap_folder: e.target.value })} />
                {editing.imap_host?.includes("gmail") && (
                  <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                    Gmail folders: <code style={{ fontFamily: "var(--font-mono)" }}>INBOX</code> · <code style={{ fontFamily: "var(--font-mono)" }}>[Gmail]/Sent Mail</code> · <code style={{ fontFamily: "var(--font-mono)" }}>[Gmail]/All Mail</code>
                    <br />To monitor both inbox <em>and</em> sent, create a second account entry with the Sent Mail folder.
                  </div>
                )}
              </Field>
              <Field label="Poll (s)"><input type="number" min={5} style={inp} value={editing.poll_interval_seconds} onChange={e => setEditing({ ...editing, poll_interval_seconds: Number(e.target.value) })} /></Field>
            </Row>
          </Section>

          <Row>
            <Field label="Active"><input type="checkbox" checked={editing.is_active} onChange={e => setEditing({ ...editing, is_active: e.target.checked })} /></Field>
            <Field label="Default outbound"><input type="checkbox" checked={editing.is_default_outbound} onChange={e => setEditing({ ...editing, is_default_outbound: e.target.checked })} /></Field>
          </Row>
        </Modal>
      )}
    </div>
  );
}

/* ─── Password security widgets ──────────────────────────────────── */

function PasswordSavedBadge({ onChange }: { onChange: () => void }) {
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 8, marginTop: 2,
      padding: "7px 10px",
      background: "color-mix(in srgb, #22c55e 8%, var(--bg-input))",
      border: "1px solid color-mix(in srgb, #22c55e 25%, var(--border-default))",
      borderRadius: "var(--radius-sm)",
    }}>
      <span style={{ fontSize: 14 }}>🔒</span>
      <span style={{ fontSize: 12, color: "var(--text-secondary)", flex: 1 }}>App password saved</span>
      <button type="button" style={{ ...btn, fontSize: 11, padding: "2px 10px" }} onClick={onChange}>
        Change
      </button>
    </div>
  );
}

function PasswordChangeInput({
  value, autoFocus, placeholder, onChange, onCancel,
}: {
  value: string; autoFocus?: boolean; placeholder?: string;
  onChange: (v: string) => void; onCancel?: () => void;
}) {
  return (
    <div>
      <input
        type="password"
        style={inp}
        value={value}
        placeholder={placeholder}
        autoFocus={autoFocus}
        onChange={e => onChange(e.target.value)}
      />
      {onCancel && (
        <button
          type="button"
          style={{ fontSize: 11, color: "var(--text-muted)", background: "none", border: "none", cursor: "pointer", padding: "3px 0", marginTop: 3, display: "block", textDecoration: "underline" }}
          onClick={onCancel}
        >
          ← Keep existing password
        </button>
      )}
    </div>
  );
}

/* ─── Templates ─────────────────────────────────────────────────── */

function TemplatesPanel() {
  const [items, setItems] = useState<Template[]>([]);
  const [editing, setEditing] = useState<Template | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [vErr, setVErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [previewCtx, setPreviewCtx] = useState('{"name": "Alice", "case": {"id": "ABC123"}}');
  const [previewResult, setPreviewResult] = useState<any>(null);
  const [previewErr, setPreviewErr] = useState<string | null>(null);

  async function load() {
    setErr(null);
    try {
      setItems(await apiJSON<Template[]>("/api/v1/email/templates?active_only=false"));
    } catch (e: any) { setErr(e.message); }
  }
  useEffect(() => { load(); }, []);

  async function save() {
    if (!editing) return;
    const v = validateTemplate(editing);
    if (v) { setVErr(v); return; }
    setVErr(null); setBusy(true); setErr(null);
    try {
      const isNew = !editing.id;
      const url = isNew ? "/api/v1/email/templates" : `/api/v1/email/templates/${editing.id}`;
      await apiJSON(url, {
        method: isNew ? "POST" : "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(editing),
      });
      setEditing(null);
      setPreviewResult(null);
      await load();
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  }

  async function runPreview() {
    if (!editing) return;
    setPreviewErr(null); setPreviewResult(null);
    let ctx: any = {};
    try { ctx = JSON.parse(previewCtx || "{}"); }
    catch (e: any) { setPreviewErr("Context must be valid JSON: " + e.message); return; }
    try {
      const res = await apiJSON<any>("/api/v1/email/templates/render-preview", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          subject: editing.subject, body_text: editing.body_text,
          body_html: editing.body_html || null, engine: editing.engine, ctx,
        }),
      });
      setPreviewResult(res);
    } catch (e: any) { setPreviewErr(e.message); }
  }

  async function deactivate(id: string) {
    if (!confirm("Deactivate this template?")) return;
    await fetch(`/api/v1/email/templates/${id}`, { method: "DELETE", headers: _authHdr() });
    await load();
  }

  return (
    <div>
      {err && <div style={errBanner}>⚠ {err}</div>}
      <div style={{ marginBottom: "var(--space-md)" }}>
        <button onClick={() => { setEditing({ ...emptyTemplate }); setVErr(null); setPreviewResult(null); }}
          style={primaryBtn}>+ New template</button>
      </div>
      <table style={tableStyle}>
        <thead><tr>
          {["Name", "Subject", "Engine", "Scope", "Active", ""].map(h => (
            <th key={h} style={th}>{h}</th>
          ))}
        </tr></thead>
        <tbody>
          {items.length === 0 && <tr><td colSpan={6} style={{ ...td, color: "var(--text-muted)" }}>No templates yet.</td></tr>}
          {items.map(t => (
            <tr key={t.id} style={{ opacity: t.is_active ? 1 : 0.55 }}>
              <td style={td}>{t.name}</td>
              <td style={td}><code style={{ fontSize: 11, fontFamily: "var(--font-mono)" }}>{t.subject}</code></td>
              <td style={td}>{t.engine}</td>
              <td style={td}>{t.scope}{t.case_type_id ? ` (${t.case_type_id.slice(0, 8)}…)` : ""}</td>
              <td style={td}>{t.is_active ? "Active" : "Inactive"}</td>
              <td style={td}>
                <button onClick={() => { setEditing({ ...t }); setVErr(null); setPreviewResult(null); }} style={btn}>Edit</button>{" "}
                {t.is_active && <button onClick={() => deactivate(t.id!)} style={{ ...btn, color: "var(--status-failed)" }}>Deactivate</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {editing && (
        <Modal title={editing.id ? "Edit template" : "New template"}
               onCancel={() => { setEditing(null); setVErr(null); setPreviewResult(null); }}
               onSave={save} busy={busy} validationErr={vErr} wide>
          <Row>
            <Field label="Name *"><input style={inp} value={editing.name} onChange={e => setEditing({ ...editing, name: e.target.value })} /></Field>
            <Field label="Engine">
              <select style={inp} value={editing.engine} onChange={e => setEditing({ ...editing, engine: e.target.value as any })}>
                <option value="jinja2">Jinja2 (full sandbox)</option>
                <option value="fstring">f-string ({"{var}"} only)</option>
              </select>
            </Field>
            <Field label="Scope">
              <select style={inp} value={editing.scope} onChange={e => setEditing({ ...editing, scope: e.target.value as any })}>
                <option value="global">Global</option>
                <option value="case_type">Case type</option>
              </select>
            </Field>
          </Row>
          {editing.scope === "case_type" && (
            <Field label="Case Type UUID *">
              <input style={inp} value={editing.case_type_id || ""} placeholder="UUID"
                onChange={e => setEditing({ ...editing, case_type_id: e.target.value || null })} />
            </Field>
          )}
          <Field label="Description">
            <input style={inp} value={editing.description} onChange={e => setEditing({ ...editing, description: e.target.value })} />
          </Field>
          <Field label="Subject *">
            <input style={inp} value={editing.subject} onChange={e => setEditing({ ...editing, subject: e.target.value })} />
          </Field>
          <Field label="Body (text) *">
            <textarea style={{ ...inp, minHeight: 110, fontFamily: "var(--font-mono)", fontSize: 12 }}
              value={editing.body_text} onChange={e => setEditing({ ...editing, body_text: e.target.value })} />
          </Field>
          <Field label="Body (HTML, optional)">
            <textarea style={{ ...inp, minHeight: 80, fontFamily: "var(--font-mono)", fontSize: 12 }}
              value={editing.body_html || ""} onChange={e => setEditing({ ...editing, body_html: e.target.value || null })} />
          </Field>

          <hr style={{ border: 0, borderTop: "1px solid var(--border-subtle)", margin: "var(--space-md) 0 var(--space-sm)" }} />
          <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", marginBottom: "var(--space-sm)" }}>Preview</div>
          <Field label="Context (JSON)">
            <textarea style={{ ...inp, fontFamily: "var(--font-mono)", fontSize: 12, minHeight: 60 }}
              value={previewCtx} onChange={e => setPreviewCtx(e.target.value)} />
          </Field>
          <button onClick={runPreview} style={btn}>Render preview</button>
          {previewErr && <div style={{ color: "var(--status-failed)", fontSize: 12, marginTop: 6 }}>⚠ {previewErr}</div>}
          {previewResult && (
            <div style={{ marginTop: 10, padding: "var(--space-md)", background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)", fontSize: 12 }}>
              <div style={{ color: "var(--text-primary)" }}><strong>Subject:</strong> {previewResult.subject}</div>
              <div style={{ marginTop: 6, color: "var(--text-secondary)" }}><strong>Body:</strong></div>
              <pre style={{ whiteSpace: "pre-wrap", margin: "4px 0 0", fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text-primary)" }}>{previewResult.body_text}</pre>
              {previewResult.body_html && (
                <>
                  <div style={{ marginTop: 6, color: "var(--text-secondary)" }}><strong>HTML:</strong></div>
                  <iframe srcDoc={previewResult.body_html} sandbox=""
                    style={{ width: "100%", height: 200, border: "1px solid var(--border-default)", marginTop: 4, borderRadius: "var(--radius-sm)" }} />
                </>
              )}
            </div>
          )}

          <Row>
            <Field label="Active"><input type="checkbox" checked={editing.is_active} onChange={e => setEditing({ ...editing, is_active: e.target.checked })} /></Field>
          </Row>
        </Modal>
      )}
    </div>
  );
}

/* ─── Reusable bits ─────────────────────────────────────────────── */

function Modal({
  title, children, onCancel, onSave, busy, validationErr, wide = false,
}: {
  title: string; children: React.ReactNode;
  onCancel: () => void; onSave: () => void;
  busy: boolean; validationErr: string | null; wide?: boolean;
}) {
  return (
    <div style={modalBg}>
      <div style={{ ...modalCard, width: wide ? 720 : 520 }}>
        <h2 style={{ margin: "0 0 var(--space-md)", fontSize: 15, fontWeight: 600, color: "var(--text-primary)" }}>{title}</h2>
        {validationErr && (
          <div style={{ background: "color-mix(in srgb, var(--status-failed) 10%, transparent)", color: "var(--status-failed)", padding: "var(--space-sm) var(--space-md)", borderRadius: "var(--radius-sm)", marginBottom: "var(--space-md)", fontSize: 12 }}>
            ⚠ {validationErr}
          </div>
        )}
        {children}
        <div style={{ marginTop: "var(--space-lg)", display: "flex", gap: "var(--space-sm)", justifyContent: "flex-end" }}>
          <button onClick={onCancel} style={btn}>Cancel</button>
          <button onClick={onSave} disabled={busy} style={primaryBtn}>{busy ? "Committing…" : "Commit"}</button>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children, w = "1fr" }: { label: string; children: React.ReactNode; w?: string }) {
  return (
    <label style={{ display: "block", marginTop: "var(--space-sm)", flex: w === "auto" ? "0 0 auto" : `0 0 calc(${w} - 8px)` }}>
      <div style={{ fontSize: 11, fontWeight: 500, color: "var(--text-muted)", marginBottom: 3, textTransform: "uppercase", letterSpacing: "0.04em", fontFamily: "var(--font-mono)" }}>{label}</div>
      {children}
    </label>
  );
}

function Row({ children }: { children: React.ReactNode }) {
  return <div style={{ display: "flex", gap: "var(--space-sm)", alignItems: "flex-end" }}>{children}</div>;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <fieldset style={{ border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", padding: "var(--space-sm) var(--space-md)", marginTop: "var(--space-md)" }}>
      <legend style={{ fontSize: 11, color: "var(--text-muted)", padding: "0 6px", fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.04em" }}>{title}</legend>
      {children}
    </fieldset>
  );
}

const errBanner: React.CSSProperties = { color: "var(--status-failed)", marginBottom: "var(--space-md)", fontSize: 13 };
const tableStyle: React.CSSProperties = { width: "100%", borderCollapse: "collapse", fontSize: 13 };
const th: React.CSSProperties = { textAlign: "left", padding: "6px 10px", borderBottom: "1px solid var(--border-subtle)", color: "var(--text-muted)", fontWeight: 500, fontSize: 11, fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.04em" };
const td: React.CSSProperties = { padding: "8px 10px", borderBottom: "1px solid var(--border-subtle)", fontSize: 13, color: "var(--text-primary)" };
const inp: React.CSSProperties = { width: "100%", padding: "6px 10px", fontSize: 13, border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", marginTop: 2, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box", fontFamily: "var(--font-body)" };
const btn: React.CSSProperties = { padding: "6px 12px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", background: "var(--bg-elevated)", fontSize: 12, cursor: "pointer", color: "var(--text-secondary)", fontFamily: "var(--font-body)" };
const primaryBtn: React.CSSProperties = { ...btn, background: "var(--accent)", color: "#fff", border: "1px solid var(--accent)", fontWeight: 600 };
const modalBg: React.CSSProperties = { position: "fixed", inset: 0, background: "var(--bg-overlay)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000 };
const modalCard: React.CSSProperties = { background: "var(--bg-panel)", border: "1px solid var(--border-default)", padding: "var(--space-xl)", borderRadius: "var(--radius-lg)", maxHeight: "85vh", overflow: "auto", boxShadow: "var(--shadow-lg)" };
