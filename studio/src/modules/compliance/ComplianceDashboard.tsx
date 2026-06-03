// HELIX P36 — Compliance Dashboard
import React, { useEffect, useState } from "react";

type ChainStatus = {
  audit_rows: number;
  sealed_rows: number;
  unsealed_rows: number;
  tip_sequence: number;
  tip_hash: string;
  last_sealed_at: string | null;
};

type VerifyResult = {
  verified: boolean;
  chain_length: number;
  breaks: any[];
  tip_sequence: number;
};

type Report = {
  id: string;
  framework: string;
  period_start: string;
  period_end: string;
  generated_by: string | null;
  generated_at: string;
  summary: Record<string, any>;
  chain_verified: boolean;
  cadence: string;
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

export default function ComplianceDashboard() {
  const [status, setStatus] = useState<ChainStatus | null>(null);
  const [verify, setVerify] = useState<VerifyResult | null>(null);
  const [reports, setReports] = useState<Report[]>([]);
  const [framework, setFramework] = useState("soc2");
  const [periodDays, setPeriodDays] = useState(30);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [lineageCaseId, setLineageCaseId] = useState("");
  const [lineage, setLineage] = useState<any[] | null>(null);

  async function loadAll() {
    setErr(null);
    try {
      const [st, rep] = await Promise.all([
        apiJSON<ChainStatus>("/api/v1/compliance/audit/status"),
        apiJSON<Report[]>("/api/v1/compliance/reports?limit=20"),
      ]);
      setStatus(st);
      setReports(rep);
    } catch (e: any) { setErr(e.message); }
  }
  useEffect(() => { loadAll(); }, []);

  async function seal() {
    setBusy("seal"); setErr(null);
    try {
      await apiJSON("/api/v1/compliance/audit/seal", { method: "POST" });
      await loadAll();
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(null); }
  }

  async function runVerify() {
    setBusy("verify"); setErr(null);
    try {
      const v = await apiJSON<VerifyResult>("/api/v1/compliance/audit/verify");
      setVerify(v);
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(null); }
  }

  async function generate() {
    setBusy("gen"); setErr(null);
    try {
      await apiJSON("/api/v1/compliance/reports/generate", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ framework, period_days: periodDays }),
      });
      await loadAll();
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(null); }
  }

  async function loadLineage() {
    if (!lineageCaseId.trim()) return;
    setBusy("lineage"); setErr(null);
    try {
      const r = await apiJSON<{ events: any[] }>(`/api/v1/compliance/lineage/${lineageCaseId.trim()}`);
      setLineage(r.events);
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(null); }
  }

  return (
    <div style={{ padding: 24, fontFamily: "system-ui, sans-serif", width: "100%", height: "100%", overflow: "auto", boxSizing: "border-box" }}>
      {err && <div style={{ color: "#c33", marginBottom: 12 }}>⚠ {err}</div>}

      {/* Audit chain status */}
      <section style={card}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
          <h2 style={h2}>Audit Chain Integrity</h2>
          <div style={{ display: "flex", gap: 6 }}>
            <button onClick={seal} disabled={!!busy} style={btn()}>
              {busy === "seal" ? "Sealing…" : "Seal new entries"}
            </button>
            <button onClick={runVerify} disabled={!!busy} style={{ ...btn(), background: "#4a6cf7", color: "white" }}>
              {busy === "verify" ? "Verifying…" : "Verify chain"}
            </button>
          </div>
        </div>

        {status && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
            <Kpi label="Audit rows" value={status.audit_rows} />
            <Kpi label="Sealed" value={status.sealed_rows} />
            <Kpi label="Unsealed" value={status.unsealed_rows} warn={status.unsealed_rows > 100} />
            <Kpi label="Tip sequence" value={status.tip_sequence} />
          </div>
        )}

        {verify && (
          <div style={{ marginTop: 14, padding: 10, borderRadius: 4,
                        background: verify.verified ? "#e8f5e9" : "#fdecea",
                        color: verify.verified ? "#1b5e20" : "#b71c1c", fontSize: 13 }}>
            {verify.verified
              ? `✓ Chain verified — ${verify.chain_length} sealed rows, no tampering.`
              : `✗ ${verify.breaks.length} breaks detected over ${verify.chain_length} rows.`}
          </div>
        )}
      </section>

      {/* Generate evidence pack */}
      <section style={card}>
        <h2 style={h2}>Generate Evidence Pack</h2>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <label style={{ fontSize: 12, color: "#666" }}>Framework</label>
          <select value={framework} onChange={e => setFramework(e.target.value)} style={inp}>
            <option value="soc2">SOC 2 Type II</option>
            <option value="iso27001">ISO/IEC 27001:2022</option>
          </select>
          <label style={{ fontSize: 12, color: "#666", marginLeft: 8 }}>Last</label>
          <input type="number" min={1} max={365} value={periodDays}
            onChange={e => setPeriodDays(Number(e.target.value) || 30)}
            style={{ ...inp, width: 80 }} />
          <span style={{ fontSize: 12, color: "#666" }}>days</span>
          <button onClick={generate} disabled={!!busy}
            style={{ ...btn(), background: "#4a6cf7", color: "white", marginLeft: "auto" }}>
            {busy === "gen" ? "Generating…" : "Generate"}
          </button>
        </div>

        <h3 style={{ fontSize: 13, marginTop: 16 }}>Recent reports</h3>
        <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={th}>Framework</th>
              <th style={th}>Period</th>
              <th style={th}>Generated</th>
              <th style={th}>Chain</th>
              <th style={th}>Cadence</th>
              <th style={th}>Download</th>
            </tr>
          </thead>
          <tbody>
            {reports.map(r => (
              <tr key={r.id}>
                <td style={td}>{r.framework.toUpperCase()}</td>
                <td style={td}>{r.period_start.slice(0, 10)} → {r.period_end.slice(0, 10)}</td>
                <td style={td}>{new Date(r.generated_at).toLocaleString()}</td>
                <td style={td}>{r.chain_verified
                  ? <span style={{ color: "#2a7" }}>✓</span>
                  : <span style={{ color: "#c33" }}>✗</span>}</td>
                <td style={td}>{r.cadence}</td>
                <td style={td}>
                  <a href={`/api/v1/compliance/reports/${r.id}/download?fmt=json`} style={btn()}>JSON</a>
                  {" "}
                  <a href={`/api/v1/compliance/reports/${r.id}/download?fmt=pdf`} style={btn()}>PDF</a>
                </td>
              </tr>
            ))}
            {reports.length === 0 && <tr><td colSpan={6} style={{ ...td, color: "#888" }}>No reports yet.</td></tr>}
          </tbody>
        </table>
      </section>

      {/* Lineage */}
      <section style={card}>
        <h2 style={h2}>Data Lineage (per case)</h2>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input value={lineageCaseId} onChange={e => setLineageCaseId(e.target.value)}
            placeholder="Case UUID"
            style={{ ...inp, width: 360, fontFamily: "ui-monospace, monospace" }} />
          <button onClick={loadLineage} disabled={!!busy} style={btn()}>
            {busy === "lineage" ? "Loading…" : "Load timeline"}
          </button>
        </div>
        {lineage && (
          <div style={{ marginTop: 12, maxHeight: 320, overflow: "auto", border: "1px solid #eee", borderRadius: 4 }}>
            <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  <th style={th}>When</th>
                  <th style={th}>Source</th>
                  <th style={th}>Kind</th>
                  <th style={th}>Field</th>
                  <th style={th}>Actor</th>
                </tr>
              </thead>
              <tbody>
                {lineage.length === 0 && <tr><td colSpan={5} style={{ ...td, color: "#888" }}>No events.</td></tr>}
                {lineage.map((e: any, i: number) => (
                  <tr key={i}>
                    <td style={td}>{e.at ? new Date(e.at).toLocaleString() : "—"}</td>
                    <td style={td}><code>{e.source_table}</code></td>
                    <td style={td}>{e.kind}</td>
                    <td style={td}>{e.field_path || "—"}</td>
                    <td style={td}>{e.actor_id || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function Kpi({ label, value, warn = false }: { label: string; value: React.ReactNode; warn?: boolean }) {
  return (
    <div style={{ padding: "10px 12px", border: `1px solid ${warn ? "#f0b0b0" : "#e3e3e8"}`, borderRadius: 6, background: "#fafbfc" }}>
      <div style={{ fontSize: 11, color: "#888", textTransform: "uppercase", letterSpacing: 0.4 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 600, color: warn ? "#c33" : "#222", marginTop: 2 }}>{value}</div>
    </div>
  );
}

const card: React.CSSProperties = { background: "#fff", border: "1px solid #e3e3e8", borderRadius: 8, padding: 16, marginBottom: 18 };
const h2: React.CSSProperties = { margin: "0 0 10px", fontSize: 15, color: "#333" };
const th: React.CSSProperties = { textAlign: "left", padding: "6px 8px", borderBottom: "1px solid #eee", color: "#666", fontWeight: 500, fontSize: 11 };
const td: React.CSSProperties = { padding: "6px 8px", borderBottom: "1px solid #f5f5f5", fontSize: 12 };
const inp: React.CSSProperties = { padding: "5px 8px", fontSize: 13, border: "1px solid #ccc", borderRadius: 3 };
function btn(): React.CSSProperties {
  return { padding: "5px 10px", border: "1px solid #ccc", borderRadius: 3, background: "#fafafa", fontSize: 12, cursor: "pointer", textDecoration: "none", color: "#333", display: "inline-block" };
}
