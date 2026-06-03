// HELIX P24 — Document Manager (multi-select + version delete)
import React, { useEffect, useState } from "react";

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
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}

async function apiJSON<T>(url: string, opts: RequestInit = {}): Promise<T> {
  const r = await fetch(url, { ...opts, headers: { ..._authHdr(), ...opts.headers } });
  if (!r.ok) throw new Error(`${url} → ${r.status}`);
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
  const [busy, setBusy] = useState(false);

  const focusedDoc = docs.find((d) => d.id === focusedDocId) || null;

  async function loadDocs() {
    if (!caseId) return;
    setError(null);
    try {
      const d = await apiJSON<Doc[]>(`/api/v1/documents/by-case/${caseId}`);
      setDocs(d);
      setSelectedDocIds(new Set());
      if (focusedDocId && !d.find((x) => x.id === focusedDocId)) {
        setFocusedDocId(null);
        setVersions([]);
      }
    } catch (e: any) { setError(e.message); }
  }

  async function loadVersions(docId: string) {
    try {
      const v = await apiJSON<Version[]>(`/api/v1/documents/${docId}/versions`);
      setVersions(v);
      setSelectedVersions(new Set());
    } catch (e: any) { setError(e.message); }
  }

  async function onUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (!f || !caseId) return;
    setBusy(true); setError(null);
    try {
      const fd = new FormData();
      fd.append("case_id", caseId);
      fd.append("file", f);
      const r = await fetch("/api/v1/documents/upload", { method: "POST", body: fd, headers: _authHdr() });
      if (!r.ok) throw new Error(`upload failed: ${r.status}`);
      await loadDocs();
    } catch (err: any) { setError(err.message); }
    finally { setBusy(false); e.target.value = ""; }
  }

  async function onUploadVersion(docId: string, file: File) {
    setBusy(true); setError(null);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const r = await fetch(`/api/v1/documents/${docId}/versions`, { method: "POST", body: fd, headers: _authHdr() });
      if (!r.ok) throw new Error(`version upload failed: ${r.status}`);
      await loadDocs();
      await loadVersions(docId);
    } catch (err: any) { setError(err.message); }
    finally { setBusy(false); }
  }

  async function onDeleteSelectedDocs() {
    const ids = Array.from(selectedDocIds);
    if (ids.length === 0) return;
    const names = docs.filter((d) => selectedDocIds.has(d.id)).map((d) => d.filename).join(", ");
    if (!confirm(`Delete ${ids.length} document(s)?\n\n${names}`)) return;

    setBusy(true); setError(null);
    const errors: string[] = [];
    for (const id of ids) {
      const r = await fetch(`/api/v1/documents/${id}`, { method: "DELETE", headers: _authHdr() });
      if (r.status === 403) errors.push(`${id}: permission denied`);
      else if (!r.ok) errors.push(`${id}: ${r.status}`);
    }
    setBusy(false);
    if (errors.length) setError(`Some deletes failed:\n${errors.join("\n")}`);
    setFocusedDocId(null);
    setVersions([]);
    await loadDocs();
  }

  async function onDeleteSelectedVersions() {
    if (!focusedDoc) return;
    const vers = Array.from(selectedVersions).sort((a, b) => a - b);
    if (vers.length === 0) return;
    if (vers.length >= versions.length) {
      setError("Cannot delete all versions. Delete the document instead, or leave at least one version.");
      return;
    }
    if (!confirm(`Delete ${vers.length} version(s) of "${focusedDoc.filename}"?`)) return;

    setBusy(true); setError(null);
    const errors: string[] = [];
    for (const v of vers) {
      const r = await fetch(`/api/v1/documents/${focusedDoc.id}/versions/${v}`, { method: "DELETE", headers: _authHdr() });
      if (r.status === 403) errors.push(`v${v}: permission denied`);
      else if (r.status === 409) errors.push(`v${v}: cannot delete last remaining version`);
      else if (!r.ok) errors.push(`v${v}: ${r.status}`);
    }
    setBusy(false);
    if (errors.length) setError(`Some version deletes failed:\n${errors.join("\n")}`);
    await loadDocs();
    await loadVersions(focusedDoc.id);
  }

  async function togglePortalVisible(docId: string, current: boolean) {
    const r = await fetch(`/api/v1/documents/${docId}/portal-visibility`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", ..._authHdr() },
      body: JSON.stringify({ portal_visible: !current }),
    });
    if (!r.ok) { setError("Failed to update portal visibility"); return; }
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
    <div style={{ padding: 24, fontFamily: "system-ui, sans-serif", width: "100%", height: "100%", overflow: "auto", boxSizing: "border-box" }}>
      {error && <div style={{ color: "#c33", marginBottom: 12, whiteSpace: "pre-wrap" }}>⚠ {error}</div>}

      <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 20 }}>
        <label style={{ fontSize: 13, color: "#555" }}>Case ID:</label>
        <input
          value={caseId}
          onChange={(e) => setCaseId(e.target.value)}
          placeholder="UUID of a case"
          style={{ padding: "6px 10px", border: "1px solid #ccc", borderRadius: 4, width: 320, fontFamily: "ui-monospace, monospace" }}
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
              style={{ ...btn(), color: selectedDocIds.size === 0 ? "#aaa" : "#c33" }}
            >
              Delete selected ({selectedDocIds.size})
            </button>
          </div>
          {docs.length > 0 && (
            <label style={{ fontSize: 12, color: "#666", display: "flex", alignItems: "center", gap: 6, marginBottom: 10 }}>
              <input
                type="checkbox"
                checked={selectedDocIds.size === docs.length && docs.length > 0}
                onChange={toggleAllDocs}
              />
              Select all
            </label>
          )}
          {docs.length === 0 && <div style={{ color: "#888", fontSize: 13 }}>No documents. Load a case or upload.</div>}
          {docs.map((d) => {
            const isSel = selectedDocIds.has(d.id);
            const isFocus = focusedDocId === d.id;
            return (
              <div
                key={d.id}
                style={{
                  padding: 10,
                  border: `2px solid ${isFocus ? "#4a6cf7" : "#eee"}`,
                  borderRadius: 4, marginBottom: 8,
                  background: isSel ? "#fff8e0" : isFocus ? "#eef3ff" : "#fff",
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
                  <div style={{ fontSize: 12, color: "#666" }}>
                    {d.content_type} · v{d.current_version} · {new Date(d.created_at).toLocaleString()}
                    {d.portal_source === "customer" && (
                      <span style={{ marginLeft: 8, color: "#0d9488", fontWeight: 600 }}>[Customer Upload]</span>
                    )}
                  </div>
                </div>
                <button
                  onClick={e => { e.stopPropagation(); togglePortalVisible(d.id, d.portal_visible); }}
                  title={d.portal_visible ? "Shared with customer — click to unshare" : "Share with customer portal"}
                  style={{
                    padding: "3px 8px", fontSize: 11, border: "1px solid",
                    borderRadius: 4, cursor: "pointer", flexShrink: 0,
                    borderColor: d.portal_visible ? "#16a34a" : "#d1d5db",
                    background: d.portal_visible ? "#f0fdf4" : "#f9fafb",
                    color: d.portal_visible ? "#16a34a" : "#6b7280",
                    fontWeight: 600,
                  }}
                >
                  {d.portal_visible ? "✓ Shared" : "Share"}
                </button>
              </div>
            );
          })}
          <div style={{ fontSize: 11, color: "#888", marginTop: 8 }}>
            Tip: checkbox = select for bulk delete; click row body to view details.
          </div>
        </section>

        {/* ── Details + versions with checkboxes ── */}
        <section style={card}>
          <h2 style={h2}>Details</h2>
          {!focusedDoc && <div style={{ color: "#888", fontSize: 13 }}>Click a document row to view details.</div>}
          {focusedDoc && (
            <div>
              <div style={{ fontSize: 14, marginBottom: 12 }}>
                <strong>{focusedDoc.filename}</strong><br />
                <span style={{ color: "#666", fontSize: 12 }}>{focusedDoc.id}</span>
              </div>
              <div style={{ display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap" }}>
                <a href={`/api/v1/documents/${focusedDoc.id}/download`} style={btn()}>Download</a>
                <a href={`/api/v1/documents/${focusedDoc.id}/preview`} target="_blank" rel="noreferrer" style={btn()}>Preview</a>
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

              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 12 }}>
                <h3 style={{ fontSize: 14, margin: 0 }}>Versions</h3>
                <button
                  onClick={onDeleteSelectedVersions}
                  disabled={selectedVersions.size === 0 || busy}
                  style={{ ...btn(), color: selectedVersions.size === 0 ? "#aaa" : "#c33" }}
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
                    <tr key={v.id} style={{ background: selectedVersions.has(v.version) ? "#fff8e0" : undefined }}>
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
                          <span style={{ marginLeft: 4, fontSize: 10, color: "#2a7" }}>(current)</span>
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

const card: React.CSSProperties = { background: "#fff", border: "1px solid #e3e3e8", borderRadius: 8, padding: 16 };
const h2: React.CSSProperties = { margin: "0", fontSize: 15, color: "#333" };
const th: React.CSSProperties = { textAlign: "left", borderBottom: "1px solid #eee", padding: "4px 6px", color: "#666", fontWeight: 500 };
const td: React.CSSProperties = { padding: "4px 6px", borderBottom: "1px solid #f5f5f5" };

function btn(): React.CSSProperties {
  return {
    padding: "6px 12px", border: "1px solid #ccc", borderRadius: 4,
    background: "#fafafa", fontSize: 13, textDecoration: "none", color: "#333",
    display: "inline-block", cursor: "pointer",
  };
}
