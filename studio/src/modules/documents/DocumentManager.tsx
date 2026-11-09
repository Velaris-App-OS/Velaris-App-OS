// Velaris P24 — Document Manager (multi-select + version delete)
import React, { useEffect, useState } from "react";
import { errorFromResponse, verifyDocument, listDocumentVerifications, type DocCheck , openDocument } from "@shared/api/client";
import ErrorCodeLink from "@shared/errors/ErrorCodeLink";
import { getAccessToken } from "@/auth/tokenManager";

type Doc = {
  id: string;
  case_id: string;
  filename: string;
  content_type: string;
  current_version: number;
  uploaded_by: string | null;
  is_deleted: boolean;
  portal_visible: boolean;
  portal_source: string | null;
  created_at: string;
  updated_at: string;
};

type Version = {
  id: string;
  document_id: string;
  version: number;
  size_bytes: number;
  sha256: string;
  uploaded_by: string | null;
  created_at: string;
};

const fmtBytes = (n: number) => {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(2)} MB`;
};

function _authHdr(): Record<string, string> {
  const t = getAccessToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

async function apiJSON<T>(url: string, opts: RequestInit = {}): Promise<T> {
  const r = await fetch(url, { ...opts, headers: { ..._authHdr(), ...opts.headers } });
  // Reads the body so the stable error code survives to the UI. Throwing a
  // bare Error here is what kept VEL-DOC-* codes invisible.
  if (!r.ok) throw await errorFromResponse(r, `${url} → ${r.status}`);
  return r.json();
}

export default function DocumentManager() {
  const [caseId, setCaseId] = useState<string>("");
  const [docs, setDocs] = useState<Doc[]>([]);
  const [selectedDocIds, setSelectedDocIds] = useState<Set<string>>(new Set());
  const [focusedDocId, setFocusedDocId] = useState<string | null>(null);
  const [versions, setVersions] = useState<Version[]>([]);
  const [selectedVersions, setSelectedVersions] = useState<Set<number>>(new Set());
  const [error, setError] = useState<string | null>(null);
  // The thrown object is kept alongside its message: `e.message` is what the
  // banner shows, but the stable error code lives on the object, and
  // flattening to a string is what dropped it before.
  const [errorObj, setErrorObj] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const focusedDoc = docs.find((d) => d.id === focusedDocId) || null;

  async function loadDocs() {
    if (!caseId) return;
    setError(null); setErrorObj(null);
    try {
      const d = await apiJSON<Doc[]>(`/api/v1/documents/by-case/${caseId}`);
      setDocs(d);
      setSelectedDocIds(new Set());
      if (focusedDocId && !d.find((x) => x.id === focusedDocId)) {
        setFocusedDocId(null);
        setVersions([]);
      }
    } catch (e: any) { setError(e.message); setErrorObj(e); }
  }

  async function loadVersions(docId: string) {
    try {
      const v = await apiJSON<Version[]>(`/api/v1/documents/${docId}/versions`);
      setVersions(v);
      setSelectedVersions(new Set());
    } catch (e: any) { setError(e.message); setErrorObj(e); }
  }

  async function onUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (!f || !caseId) return;
    setBusy(true); setError(null); setErrorObj(null);
    try {
      const fd = new FormData();
      fd.append("case_id", caseId);
      fd.append("file", f);
      const r = await fetch("/api/v1/documents/upload", { method: "POST", body: fd, headers: _authHdr() });
      if (!r.ok) throw await errorFromResponse(r, `upload failed: ${r.status}`);
      await loadDocs();
    } catch (err: any) { setError(err.message); setErrorObj(err); }
    finally { setBusy(false); e.target.value = ""; }
  }

  async function onUploadVersion(docId: string, file: File) {
    setBusy(true); setError(null); setErrorObj(null);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const r = await fetch(`/api/v1/documents/${docId}/versions`, { method: "POST", body: fd, headers: _authHdr() });
      if (!r.ok) throw await errorFromResponse(r, `version upload failed: ${r.status}`);
      await loadDocs();
      await loadVersions(docId);
    } catch (err: any) { setError(err.message); setErrorObj(err); }
    finally { setBusy(false); }
  }

  async function onDeleteSelectedDocs() {
    const ids = Array.from(selectedDocIds);
    if (ids.length === 0) return;
    const names = docs.filter((d) => selectedDocIds.has(d.id)).map((d) => d.filename).join(", ");
    if (!confirm(`Delete ${ids.length} document(s)?\n\n${names}`)) return;

    setBusy(true); setError(null); setErrorObj(null);
    const errors: string[] = [];
    for (const id of ids) {
      const r = await fetch(`/api/v1/documents/${id}`, { method: "DELETE", headers: _authHdr() });
      if (r.status === 403) errors.push(`${id}: permission denied`);
      else if (!r.ok) errors.push(`${id}: ${r.status}`);
    }
    setBusy(false);
    if (errors.length) { setError(`Some deletes failed:\n${errors.join("\n")}`); setErrorObj(null); }
    setFocusedDocId(null);
    setVersions([]);
    await loadDocs();
  }

  async function onDeleteSelectedVersions() {
    if (!focusedDoc) return;
    const vers = Array.from(selectedVersions).sort((a, b) => a - b);
    if (vers.length === 0) return;
    if (vers.length >= versions.length) {
      setError("Cannot delete all versions. Delete the document instead, or leave at least one version."); setErrorObj(null);
      return;
    }
    if (!confirm(`Delete ${vers.length} version(s) of "${focusedDoc.filename}"?`)) return;

    setBusy(true); setError(null); setErrorObj(null);
    const errors: string[] = [];
    for (const v of vers) {
      const r = await fetch(`/api/v1/documents/${focusedDoc.id}/versions/${v}`, { method: "DELETE", headers: _authHdr() });
      if (r.status === 403) errors.push(`v${v}: permission denied`);
      else if (r.status === 409) errors.push(`v${v}: cannot delete last remaining version`);
      else if (!r.ok) errors.push(`v${v}: ${r.status}`);
    }
    setBusy(false);
    if (errors.length) { setError(`Some version deletes failed:\n${errors.join("\n")}`); setErrorObj(null); }
    await loadDocs();
    await loadVersions(focusedDoc.id);
  }

  async function togglePortalVisible(docId: string, current: boolean) {
    const r = await fetch(`/api/v1/documents/${docId}/portal-visibility`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", ..._authHdr() },
      body: JSON.stringify({ portal_visible: !current }),
    });
    if (!r.ok) { setError("Failed to update portal visibility"); setErrorObj(null); return; }
    setDocs(prev => prev.map(d => d.id === docId ? { ...d, portal_visible: !current, portal_source: !current ? "staff" : d.portal_source } : d));
  }

  function toggleDoc(id: string) {
    setSelectedDocIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAllDocs() {
    if (selectedDocIds.size === docs.length) setSelectedDocIds(new Set());
    else setSelectedDocIds(new Set(docs.map((d) => d.id)));
  }

  function toggleVersion(v: number) {
    setSelectedVersions((prev) => {
      const next = new Set(prev);
      if (next.has(v)) next.delete(v);
      else next.add(v);
      return next;
    });
  }

  useEffect(() => {
    if (focusedDocId) loadVersions(focusedDocId);
    else { setVersions([]); setSelectedVersions(new Set()); }
  }, [focusedDocId]);

  return (
    <div style={{ padding: 24, fontFamily: "system-ui, sans-serif", color: "var(--text-primary)", background: "var(--bg-root)", width: "100%", height: "100%", overflow: "auto", boxSizing: "border-box" }}>
      {error && (
        <div style={{ color: "var(--status-failed)", marginBottom: 12, whiteSpace: "pre-wrap" }}>
          ⚠ {error}
          <ErrorCodeLink error={errorObj} componentKey="documents" />
        </div>
      )}

      <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 20 }}>
        <label style={{ fontSize: 13, color: "var(--text-secondary)" }}>Case ID:</label>
        <input
          value={caseId}
          onChange={(e) => setCaseId(e.target.value)}
          placeholder="UUID of a case"
          style={{ padding: "6px 10px", border: "1px solid var(--border-default)", borderRadius: 4, width: 320, fontFamily: "ui-monospace, monospace", background: "var(--bg-input)", color: "var(--text-primary)" }}
        />
        <button onClick={loadDocs} style={btn()}>Load</button>
        <label style={{ ...btn(), cursor: busy ? "not-allowed" : "pointer" }}>
          {busy ? "Working…" : "Upload document"}
          <input type="file" onChange={onUpload} disabled={busy || !caseId} style={{ display: "none" }} />
        </label>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20 }}>
        {/* ── Documents list with checkboxes ── */}
        <section style={card}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
            <h2 style={h2}>Documents ({docs.length})</h2>
            <button
              onClick={onDeleteSelectedDocs}
              disabled={selectedDocIds.size === 0 || busy}
              style={{ ...btn(), color: selectedDocIds.size === 0 ? "var(--text-muted)" : "var(--status-failed)" }}
            >
              Delete selected ({selectedDocIds.size})
            </button>
          </div>
          {docs.length > 0 && (
            <label style={{ fontSize: 12, color: "var(--text-secondary)", display: "flex", alignItems: "center", gap: 6, marginBottom: 10 }}>
              <input
                type="checkbox"
                checked={selectedDocIds.size === docs.length && docs.length > 0}
                onChange={toggleAllDocs}
              />
              Select all
            </label>
          )}
          {docs.length === 0 && <div style={{ color: "var(--text-muted)", fontSize: 13 }}>No documents. Load a case or upload.</div>}
          {docs.map((d) => {
            const isSel = selectedDocIds.has(d.id);
            const isFocus = focusedDocId === d.id;
            return (
              <div
                key={d.id}
                style={{
                  padding: 10,
                  border: `2px solid ${isFocus ? "var(--accent)" : "var(--border-subtle)"}`,
                  borderRadius: 4, marginBottom: 8,
                  background: isSel ? "var(--accent-dim)" : isFocus ? "var(--bg-card-hover)" : "var(--bg-card)",
                  display: "flex", alignItems: "center", gap: 10,
                }}
              >
                <input
                  type="checkbox"
                  checked={isSel}
                  onChange={() => toggleDoc(d.id)}
                  style={{ cursor: "pointer" }}
                />
                <div
                  onClick={() => setFocusedDocId(d.id)}
                  style={{ flex: 1, cursor: "pointer" }}
                >
                  <div style={{ fontWeight: 600 }}>{d.filename}</div>
                  <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>
                    {d.content_type} · v{d.current_version} · {new Date(d.created_at).toLocaleString()}
                    {d.portal_source === "customer" && (
                      <span style={{ marginLeft: 8, color: "var(--accent-text)", fontWeight: 600 }}>[Customer Upload]</span>
                    )}
                  </div>
                </div>
                <button
                  onClick={e => { e.stopPropagation(); togglePortalVisible(d.id, d.portal_visible); }}
                  title={d.portal_visible ? "Shared with customer — click to unshare" : "Share with customer portal"}
                  style={{
                    padding: "3px 8px", fontSize: 11, border: "1px solid",
                    borderRadius: 4, cursor: "pointer", flexShrink: 0,
                    borderColor: d.portal_visible ? "var(--status-completed)" : "var(--border-default)",
                    background: d.portal_visible ? "rgba(13,148,136,0.12)" : "var(--bg-elevated)",
                    color: d.portal_visible ? "var(--status-completed)" : "var(--text-secondary)",
                    fontWeight: 600,
                  }}
                >
                  {d.portal_visible ? "✓ Shared" : "Share"}
                </button>
              </div>
            );
          })}
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 8 }}>
            Tip: checkbox = select for bulk delete; click row body to view details.
          </div>
        </section>

        {/* ── Details + versions with checkboxes ── */}
        <section style={card}>
          <h2 style={h2}>Details</h2>
          {!focusedDoc && <div style={{ color: "var(--text-muted)", fontSize: 13 }}>Click a document row to view details.</div>}
          {focusedDoc && (
            <div>
              <div style={{ fontSize: 14, marginBottom: 12 }}>
                <strong>{focusedDoc.filename}</strong><br />
                <span style={{ color: "var(--text-secondary)", fontSize: 12 }}>{focusedDoc.id}</span>
              </div>
              <div style={{ display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap" }}>
                {/* Buttons, not <a href>: browser navigation sends no
                    Authorization header, and these routes require one since
                    2026-08-15. See openDocument(). */}
                <button style={btn()} onClick={() => openDocument(focusedDoc.id, { filename: focusedDoc.filename })
                  .catch(e => setError(e?.message || "Download failed"))}>Download</button>
                <button style={btn()} onClick={() => openDocument(focusedDoc.id, { preview: true })
                  .catch(e => setError(e?.message || "Preview failed"))}>Preview</button>
                <label style={{ ...btn(), cursor: "pointer" }}>
                  New version
                  <input
                    type="file"
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      if (f) onUploadVersion(focusedDoc.id, f);
                      e.target.value = "";
                    }}
                    style={{ display: "none" }}
                  />
                </label>
              </div>

              <VerificationSection docId={focusedDoc.id} />

              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 12 }}>
                <h3 style={{ fontSize: 14, margin: 0 }}>Versions</h3>
                <button
                  onClick={onDeleteSelectedVersions}
                  disabled={selectedVersions.size === 0 || busy}
                  style={{ ...btn(), color: selectedVersions.size === 0 ? "var(--text-muted)" : "var(--status-failed)" }}
                >
                  Delete selected ({selectedVersions.size})
                </button>
              </div>
              <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse", marginTop: 8 }}>
                <thead>
                  <tr>
                    <th style={th}></th>
                    <th style={th}>v</th>
                    <th style={th}>Size</th>
                    <th style={th}>SHA-256</th>
                    <th style={th}>Uploaded</th>
                  </tr>
                </thead>
                <tbody>
                  {versions.map((v) => (
                    <tr key={v.id} style={{ background: selectedVersions.has(v.version) ? "var(--accent-dim)" : undefined }}>
                      <td style={td}>
                        <input
                          type="checkbox"
                          checked={selectedVersions.has(v.version)}
                          onChange={() => toggleVersion(v.version)}
                        />
                      </td>
                      <td style={td}>
                        {v.version}
                        {v.version === focusedDoc.current_version && (
                          <span style={{ marginLeft: 4, fontSize: 10, color: "var(--status-completed)" }}>(current)</span>
                        )}
                      </td>
                      <td style={td}>{fmtBytes(v.size_bytes)}</td>
                      <td style={{ ...td, fontFamily: "ui-monospace, monospace" }}>{v.sha256.slice(0, 12)}…</td>
                      <td style={td}>{new Date(v.created_at).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

/* ── HxMeet P4b — document verification (the document-first KYC gate) ──
   Automated checks run server-side; the worker's verdict is the record.
   The server rejects "passed" while any check fails (409). */

type Verification = {
  id: string; status: string; checks: DocCheck[];
  verified_by: string; notes: string | null; created_at: string | null;
};

const CHECKLIST_ITEMS: { key: string; label: string }[] = [
  { key: "photo_matches", label: "Photo matches the person" },
  { key: "no_visible_tampering", label: "No visible tampering" },
  { key: "details_match_case", label: "Details match the case" },
];

const statusColor = (s: string) =>
  s === "passed" ? "var(--status-completed)" : s === "failed" ? "var(--status-failed)" : "var(--status-running)";

function CheckChip({ c }: { c: DocCheck }) {
  const color = c.result === "pass" ? "var(--status-completed)" : c.result === "fail" ? "var(--status-failed)" : "var(--text-muted)";
  return (
    <span title={c.detail} style={{
      fontSize: 11, padding: "1px 6px", borderRadius: 3, marginRight: 4,
      border: `1px solid ${color}`, color, whiteSpace: "nowrap", display: "inline-block", marginBottom: 3,
    }}>
      {c.result === "pass" ? "✓" : c.result === "fail" ? "✕" : "–"} {c.name}
    </span>
  );
}

function VerificationSection({ docId }: { docId: string }) {
  const [history, setHistory] = useState<Verification[]>([]);
  const [open, setOpen] = useState(false);
  const [mrz, setMrz] = useState("");
  const [expiry, setExpiry] = useState("");
  const [checklist, setChecklist] = useState<Record<string, boolean>>({});
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    try {
      const r = await listDocumentVerifications(docId);
      setHistory(r.verifications);
    } catch { setHistory([]); }
  }

  useEffect(() => {
    setOpen(false); setMrz(""); setExpiry(""); setChecklist({}); setNotes(""); setErr(null);
    load();
  }, [docId]);

  async function submit(status: "passed" | "failed" | "review") {
    setBusy(true); setErr(null);
    try {
      await verifyDocument(docId, {
        status,
        mrz_line2: mrz.trim() || undefined,
        expiry_date: expiry || undefined,
        checklist: Object.fromEntries(CHECKLIST_ITEMS.map((i) => [i.key, !!checklist[i.key]])),
        notes: notes.trim() || undefined,
      });
      setOpen(false); setMrz(""); setExpiry(""); setChecklist({}); setNotes("");
      await load();
    } catch (e: any) { setErr(e.message || "Verification failed"); }
    finally { setBusy(false); }
  }

  const latest = history[0] || null;
  const allTicked = CHECKLIST_ITEMS.every((i) => checklist[i.key]);

  return (
    <div style={{ marginTop: 16, borderTop: "1px solid var(--border-subtle)", paddingTop: 12 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h3 style={{ fontSize: 14, margin: 0 }}>
          Verification
          {latest && (
            <span style={{ marginLeft: 8, fontSize: 11, fontWeight: 700, padding: "2px 8px",
              borderRadius: 10, border: `1px solid ${statusColor(latest.status)}`, color: statusColor(latest.status) }}>
              {latest.status.toUpperCase()}
            </span>
          )}
        </h3>
        <button onClick={() => setOpen((o) => !o)} style={btn()}>
          {open ? "Cancel" : "Verify document"}
        </button>
      </div>

      {open && (
        <div style={{ marginTop: 10, padding: 12, border: "1px solid var(--border-default)", borderRadius: 6, background: "var(--bg-elevated)" }}>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 10 }}>
            Automated checks (file integrity, image quality, MRZ, expiry) run on submit.
            They are evidence attached to your verdict — a document cannot be passed over a failing check.
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 10 }}>
            <label style={{ fontSize: 12, color: "var(--text-secondary)" }}>
              MRZ line 2 (optional)
              <input value={mrz} onChange={(e) => setMrz(e.target.value)}
                placeholder="L898902C36UTO7408122F1204159…"
                style={{ width: "100%", boxSizing: "border-box", marginTop: 3, padding: "5px 8px",
                  border: "1px solid var(--border-default)", borderRadius: 4, fontFamily: "ui-monospace, monospace", fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)" }} />
            </label>
            <label style={{ fontSize: 12, color: "var(--text-secondary)" }}>
              Expiry date (optional)
              <input type="date" value={expiry} onChange={(e) => setExpiry(e.target.value)}
                style={{ width: "100%", boxSizing: "border-box", marginTop: 3, padding: "5px 8px",
                  border: "1px solid var(--border-default)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)" }} />
            </label>
          </div>
          <div style={{ marginBottom: 10 }}>
            {CHECKLIST_ITEMS.map((i) => (
              <label key={i.key} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, marginBottom: 4 }}>
                <input type="checkbox" checked={!!checklist[i.key]}
                  onChange={(e) => setChecklist((p) => ({ ...p, [i.key]: e.target.checked }))} />
                {i.label}
              </label>
            ))}
          </div>
          <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={2} placeholder="Notes (optional)"
            style={{ width: "100%", boxSizing: "border-box", padding: "5px 8px", border: "1px solid var(--border-default)",
              borderRadius: 4, fontSize: 12, resize: "vertical", marginBottom: 10, background: "var(--bg-input)", color: "var(--text-primary)" }} />
          {err && <div style={{ color: "var(--status-failed)", fontSize: 12, marginBottom: 8, whiteSpace: "pre-wrap" }}>⚠ {err}</div>}
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <button disabled={busy || !allTicked} onClick={() => submit("passed")}
              title={allTicked ? "" : "Confirm every checklist item to record a pass"}
              style={{ ...btn(), color: "#fff", background: allTicked && !busy ? "var(--status-completed)" : "var(--border-strong)", borderColor: "transparent" }}>
              {busy ? "…" : "✓ Passed"}
            </button>
            <button disabled={busy} onClick={() => submit("review")}
              style={{ ...btn(), color: "var(--status-running)", borderColor: "var(--status-running)", background: "var(--bg-card)" }}>
              {busy ? "…" : "Needs review"}
            </button>
            <button disabled={busy} onClick={() => submit("failed")}
              style={{ ...btn(), color: "var(--status-failed)", borderColor: "var(--status-failed)", background: "var(--bg-card)" }}>
              {busy ? "…" : "✕ Failed"}
            </button>
          </div>
        </div>
      )}

      {history.length === 0 && !open && (
        <div style={{ color: "var(--text-muted)", fontSize: 12, marginTop: 8 }}>Not verified yet.</div>
      )}
      {history.map((v) => (
        <div key={v.id} style={{ marginTop: 8, padding: 8, border: "1px solid var(--border-subtle)", borderRadius: 4 }}>
          <div style={{ fontSize: 12, marginBottom: 4 }}>
            <strong style={{ color: statusColor(v.status) }}>{v.status.toUpperCase()}</strong>
            <span style={{ color: "var(--text-muted)", marginLeft: 8 }}>
              {v.created_at ? new Date(v.created_at).toLocaleString() : ""} · by {v.verified_by}
            </span>
          </div>
          <div>{v.checks.map((c, i) => <CheckChip key={i} c={c} />)}</div>
          {v.notes && <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 4 }}>{v.notes}</div>}
        </div>
      ))}
    </div>
  );
}

const card: React.CSSProperties = { background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: 8, padding: 16 };
const h2: React.CSSProperties = { margin: "0", fontSize: 15, color: "var(--text-primary)" };
const th: React.CSSProperties = { textAlign: "left", borderBottom: "1px solid var(--border-subtle)", padding: "4px 6px", color: "var(--text-secondary)", fontWeight: 500 };
const td: React.CSSProperties = { padding: "4px 6px", borderBottom: "1px solid var(--border-subtle)" };

function btn(): React.CSSProperties {
  return {
    padding: "6px 12px", border: "1px solid var(--border-default)", borderRadius: 4,
    background: "var(--bg-elevated)", fontSize: 13, textDecoration: "none", color: "var(--text-primary)",
    display: "inline-block", cursor: "pointer",
  };
}
