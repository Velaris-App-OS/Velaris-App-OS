/**
 * HxDBMigrate — P1 (Connect + Discover)
 * Register a read-only external source DB, then run a discovery analysis
 * (Schema Autobiography + data-quality score). Migration/dual-write are later phases.
 */
import React, { useCallback, useEffect, useState } from "react";
import { Button } from "@shared/components";

const API = "/api/v1/hxdbmigrate";

function authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t
    ? { Authorization: `Bearer ${t}`, "Content-Type": "application/json" }
    : { "Content-Type": "application/json" };
}
async function apiFetch(path: string, opts: RequestInit = {}) {
  return fetch(`${API}${path}`, { ...opts, headers: { ...authHdr(), ...(opts.headers ?? {}) } });
}

interface Source {
  id: string; name: string; source_type: string; host: string; port: number;
  database: string; username: string; ssl_mode: string; last_connect_ok: boolean | null;
  status: string; cutover_at: string | null; rollback_deadline: string | null;
  rollback_window_hours: number;
}
interface SyncTable {
  table: string; linked_rows: number; source_rows: number | null;
  coverage_pct: number; last_synced_at: string | null; stale_hours: number | null;
}
interface Finding { severity: string; issue: string; tables?: string[]; details?: any[]; }
interface PiiFinding { table: string; column: string; category: string; sensitivity: string; recommended_action: string; masked_examples?: string[]; }
interface Analysis {
  id: string; status: string; table_count: number | null; quality_score: number | null;
  pii_count: number | null;
  report: { autobiography: string; quality: { score: number; findings: Finding[] };
            schema: any[]; table_count: number; deep?: boolean; ai_narrative?: string | null;
            compliance?: { findings: PiiFinding[]; summary: { pii_column_count: number; tokenize_required: string[]; by_sensitivity: Record<string, number> } } };
}

const S: Record<string, React.CSSProperties> = {
  page:  { display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" },
  body:  { flex: 1, overflow: "auto", padding: "var(--space-xl) var(--space-2xl)" },
  card:  { border: "1px solid var(--border-subtle)", borderRadius: 8, padding: "var(--space-lg)", marginBottom: "var(--space-md)" },
  row:   { display: "flex", gap: "var(--space-md)", alignItems: "center", flexWrap: "wrap" },
  input: { padding: "8px 10px", borderRadius: 6, border: "1px solid var(--border-subtle)", background: "var(--surface-2)", color: "var(--text-primary)", minWidth: 140 },
  label: { fontSize: 12, color: "var(--text-secondary)", marginBottom: 4, display: "block" },
  mono:  { fontFamily: "var(--font-mono, monospace)", whiteSpace: "pre-wrap", fontSize: 13, background: "var(--surface-2)", padding: "var(--space-md)", borderRadius: 6, maxHeight: 360, overflow: "auto" },
  score: { fontSize: 34, fontWeight: 700 },
  pill:  { padding: "2px 8px", borderRadius: 999, fontSize: 11, fontWeight: 600 },
};

const SEV_COLOR: Record<string, string> = { high: "#ef4444", medium: "#f59e0b", low: "#94a3b8" };

export default function HxDBMigrate() {
  const [sources, setSources] = useState<Source[]>([]);
  const [types, setTypes] = useState<string[]>([]);
  const [sslModes, setSslModes] = useState<string[]>(["disable", "require", "verify"]);
  const [form, setForm] = useState({ name: "", source_type: "postgresql", host: "", port: "", database: "", username: "", password: "", ssl_mode: "disable" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [deep, setDeep] = useState(true);
  const [useAi, setUseAi] = useState(false);
  const [analyzedSourceId, setAnalyzedSourceId] = useState<string | null>(null);
  const [wfTables, setWfTables] = useState<{ table: string; status_column: string | null; column_count: number }[]>([]);
  const [draft, setDraft] = useState<any | null>(null);
  const [applyMsg, setApplyMsg] = useState<string | null>(null);
  const [appliedCtId, setAppliedCtId] = useState<string | null>(null);
  const [piiMode, setPiiMode] = useState("safe");
  const [migLimit, setMigLimit] = useState("100");
  const [migDry, setMigDry] = useState(true);
  const [migResult, setMigResult] = useState<any | null>(null);
  const [syncResult, setSyncResult] = useState<any | null>(null);
  const [syncStatus, setSyncStatus] = useState<{ tables: SyncTable[]; health_score: number | null; source_status?: string; hint?: string } | null>(null);
  const [syncStatusSourceId, setSyncStatusSourceId] = useState<string | null>(null);
  const [lifecycleMsg, setLifecycleMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [s, t] = await Promise.all([apiFetch("/sources"), apiFetch("/source-types")]);
    if (s.ok) setSources((await s.json()).sources);
    if (t.ok) { const j = await t.json(); setTypes(j.source_types); if (j.ssl_modes) setSslModes(j.ssl_modes); }
  }, []);
  useEffect(() => { load(); }, [load]);

  async function createSource() {
    setBusy(true); setError(null);
    try {
      const body = { ...form, port: form.port ? Number(form.port) : null };
      const r = await apiFetch("/sources", { method: "POST", body: JSON.stringify(body) });
      if (!r.ok) { setError((await r.json()).detail || "Failed to add source"); return; }
      setForm({ name: "", source_type: types[0] || "postgresql", host: "", port: "", database: "", username: "", password: "", ssl_mode: "disable" });
      await load();
    } finally { setBusy(false); }
  }

  async function analyze(id: string) {
    setBusy(true); setError(null); setAnalysis(null); setWfTables([]); setDraft(null); setApplyMsg(null);
    try {
      const r = await apiFetch(`/sources/${id}/analyze`, { method: "POST", body: JSON.stringify({ deep, ai: useAi }) });
      if (!r.ok) { setError((await r.json()).detail || "Analysis failed"); return; }
      setAnalysis(await r.json());
      setAnalyzedSourceId(id);
      if (deep) {
        const w = await apiFetch(`/sources/${id}/workflow-tables`);
        if (w.ok) setWfTables((await w.json()).candidates || []);
      }
    } finally { setBusy(false); }
  }

  async function genCaseType(table: string) {
    if (!analyzedSourceId) return;
    setBusy(true); setError(null); setDraft(null); setApplyMsg(null);
    try {
      const r = await apiFetch(`/sources/${analyzedSourceId}/generate-case-type`, { method: "POST", body: JSON.stringify({ table }) });
      if (!r.ok) { setError((await r.json()).detail || "Generation failed"); return; }
      setDraft(await r.json());
    } finally { setBusy(false); }
  }

  async function applyDraft() {
    if (!draft) return;
    setBusy(true); setError(null); setApplyMsg(null);
    try {
      const def = draft.definition_json;
      const r = await apiFetch(`/apply-case-type`, { method: "POST", body: JSON.stringify({ name: def.name, version: "1.0.0", definition_json: def }) });
      const j = await r.json();
      if (!r.ok) { setError(j.detail || "Apply failed"); return; }
      setApplyMsg(`Created case-type "${j.name}" v${j.version}. Find it in Case Designer.`);
      setAppliedCtId(j.id); setMigResult(null);
    } finally { setBusy(false); }
  }

  async function migrateData() {
    if (!analyzedSourceId || !appliedCtId || !draft) return;
    setBusy(true); setError(null); setMigResult(null);
    try {
      const body = { table: draft.source_table, case_type_id: appliedCtId, limit: Number(migLimit) || 100, dry_run: migDry, pii_mode: piiMode };
      const r = await apiFetch(`/sources/${analyzedSourceId}/migrate`, { method: "POST", body: JSON.stringify(body) });
      const j = await r.json();
      if (!r.ok) { setError(j.detail || "Migration failed"); return; }
      setMigResult(j);
    } finally { setBusy(false); }
  }

  async function del(id: string) {
    await apiFetch(`/sources/${id}`, { method: "DELETE" });
    await load();
  }

  // P5 — one incremental sync pass on the migrated table
  async function syncNow() {
    if (!analyzedSourceId || !appliedCtId || !draft) return;
    setBusy(true); setError(null); setSyncResult(null);
    try {
      const r = await apiFetch(`/sources/${analyzedSourceId}/sync`, {
        method: "POST",
        body: JSON.stringify({ table: draft.source_table, case_type_id: appliedCtId, pii_mode: piiMode }),
      });
      const j = await r.json();
      if (!r.ok) { setError(j.detail || "Sync failed"); return; }
      setSyncResult(j);
      if (syncStatusSourceId === analyzedSourceId) await loadSyncStatus(analyzedSourceId);
    } finally { setBusy(false); }
  }

  async function loadSyncStatus(id: string) {
    setBusy(true); setError(null);
    try {
      const r = await apiFetch(`/sources/${id}/sync-status`);
      const j = await r.json();
      if (!r.ok) { setError(j.detail || "Sync status failed"); return; }
      setSyncStatus(j); setSyncStatusSourceId(id);
    } finally { setBusy(false); }
  }

  // P6 — lifecycle transitions
  async function lifecycle(id: string, action: "cutover" | "rollback" | "complete") {
    const confirmMsg = action === "cutover"
      ? "Cut over now? A final delta sync runs, then migrate/sync freeze. Rollback stays available inside the window."
      : action === "rollback"
        ? "Roll back? All cases created from this source will be cancelled and the source unfrozen."
        : "Mark this migration final? The rollback window closes immediately.";
    if (!window.confirm(confirmMsg)) return;
    setBusy(true); setError(null); setLifecycleMsg(null);
    try {
      const r = await apiFetch(`/sources/${id}/${action}`, { method: "POST", body: action === "cutover" ? JSON.stringify({}) : undefined });
      const j = await r.json();
      if (!r.ok) { setError(j.detail || `${action} failed`); return; }
      setLifecycleMsg(action === "cutover"
        ? `Cutover complete — final sync read ${j.final_sync?.rows_read ?? 0} row(s). Rollback until ${j.rollback_deadline ?? "window end"}.`
        : action === "rollback"
          ? `Rolled back — ${j.cases_cancelled} case(s) cancelled, source active again.`
          : "Migration marked final.");
      await load();
      if (syncStatusSourceId === id) await loadSyncStatus(id);
    } finally { setBusy(false); }
  }

  // P7 — download the signed certificate PDF (fetch with auth → blob)
  async function downloadCertificate(id: string, name: string) {
    setBusy(true); setError(null);
    try {
      const r = await apiFetch(`/sources/${id}/certificate?fmt=pdf`);
      if (!r.ok) { const j = await r.json().catch(() => ({})); setError(j.detail || "Certificate failed"); return; }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `migration-certificate-${name}.pdf`; a.click();
      URL.revokeObjectURL(url);
    } finally { setBusy(false); }
  }

  return (
    <div style={S.page}>
      <div style={S.body}>
        {error && <div style={{ ...S.card, borderColor: "#ef4444", color: "#ef4444" }}>{error}</div>}

        {/* Connect Source */}
        <div style={S.card}>
          <h3 style={{ marginTop: 0 }}>Connect a source database</h3>
          <div style={S.row}>
            <div><label style={S.label}>Name</label>
              <input style={S.input} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></div>
            <div><label style={S.label}>Type</label>
              <select style={S.input} value={form.source_type} onChange={e => setForm({ ...form, source_type: e.target.value })}>
                {(types.length ? types : ["postgresql"]).map(t => <option key={t} value={t}>{t}</option>)}
              </select></div>
            <div><label style={S.label}>Host</label>
              <input style={S.input} value={form.host} onChange={e => setForm({ ...form, host: e.target.value })} /></div>
            <div><label style={S.label}>Port</label>
              <input style={{ ...S.input, minWidth: 80 }} value={form.port} placeholder="default" onChange={e => setForm({ ...form, port: e.target.value })} /></div>
            <div><label style={S.label}>Database</label>
              <input style={S.input} value={form.database} onChange={e => setForm({ ...form, database: e.target.value })} /></div>
            <div><label style={S.label}>Username</label>
              <input style={S.input} value={form.username} onChange={e => setForm({ ...form, username: e.target.value })} /></div>
            <div><label style={S.label}>Password</label>
              <input style={S.input} type="password" value={form.password} onChange={e => setForm({ ...form, password: e.target.value })} /></div>
            <div><label style={S.label}>SSL</label>
              <select style={{ ...S.input, minWidth: 110 }} value={form.ssl_mode} onChange={e => setForm({ ...form, ssl_mode: e.target.value })}>
                {sslModes.map(m => <option key={m} value={m}>{m}</option>)}
              </select></div>
            <Button onClick={createSource} disabled={busy || !form.name || !form.host || !form.database}>Test &amp; Add</Button>
          </div>
          <p style={{ color: "var(--text-secondary)", fontSize: 12, marginBottom: 0 }}>
            Read-only. Credentials are encrypted (HxVault). Open-source relational sources only.
          </p>
        </div>

        {/* Sources */}
        <div style={S.card}>
          <div style={{ ...S.row, justifyContent: "space-between" }}>
            <h3 style={{ margin: 0 }}>Sources</h3>
            <span style={{ ...S.row, fontSize: 13, color: "var(--text-secondary)" }}>
              <label style={S.row}><input type="checkbox" checked={deep} onChange={e => setDeep(e.target.checked)} /> Deep scan (semantic + PII)</label>
              <label style={S.row}><input type="checkbox" checked={useAi} onChange={e => setUseAi(e.target.checked)} /> AI narrative</label>
            </span>
          </div>
          {sources.length === 0 && <p style={{ color: "var(--text-secondary)" }}>No sources yet.</p>}
          {sources.map(s => (
            <div key={s.id} style={{ ...S.row, justifyContent: "space-between", borderBottom: "1px solid var(--border-subtle)", padding: "8px 0" }}>
              <span>
                <strong>{s.name}</strong>{" "}
                <span style={{ ...S.pill, marginRight: 6,
                               background: s.status === "active" ? "#22c55e22" : s.status === "cutover" ? "#f59e0b22" : "#94a3b822",
                               color: s.status === "active" ? "#22c55e" : s.status === "cutover" ? "#f59e0b" : "#94a3b8" }}>
                  {s.status}{s.status === "cutover" && s.rollback_deadline ? ` · rollback until ${new Date(s.rollback_deadline).toLocaleString()}` : ""}
                </span>
                <span style={{ color: "var(--text-secondary)" }}>· {s.source_type} · {s.username}@{s.host}:{s.port}/{s.database} · SSL: {s.ssl_mode}</span>
              </span>
              <span style={S.row}>
                <Button onClick={() => analyze(s.id)} disabled={busy}>Analyze</Button>
                <Button variant="ghost" onClick={() => loadSyncStatus(s.id)} disabled={busy}>Sync status</Button>
                {s.status === "active" && <Button variant="ghost" onClick={() => lifecycle(s.id, "cutover")} disabled={busy}>Cutover</Button>}
                {s.status === "cutover" && <>
                  <Button variant="ghost" onClick={() => lifecycle(s.id, "rollback")} disabled={busy}>Rollback</Button>
                  <Button variant="ghost" onClick={() => lifecycle(s.id, "complete")} disabled={busy}>Complete</Button>
                </>}
                <Button variant="ghost" onClick={() => downloadCertificate(s.id, s.name)} disabled={busy}>Certificate</Button>
                <Button variant="ghost" onClick={() => del(s.id)}>Delete</Button>
              </span>
            </div>
          ))}
          {lifecycleMsg && <div style={{ ...S.card, borderColor: "#22c55e", color: "#22c55e", marginTop: "var(--space-md)", marginBottom: 0 }}>{lifecycleMsg}</div>}
        </div>

        {/* P5 — sync status + Migration Health Score */}
        {syncStatus && (
          <div style={S.card}>
            <div style={{ ...S.row, justifyContent: "space-between" }}>
              <h3 style={{ margin: 0 }}>Sync status{syncStatus.source_status ? ` — source ${syncStatus.source_status}` : ""}</h3>
              {syncStatus.health_score != null && (
                <div style={{ textAlign: "right" }}>
                  <div style={{ ...S.score, color: syncStatus.health_score >= 70 ? "#22c55e" : "#f59e0b" }}>{syncStatus.health_score}</div>
                  <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>migration health</div>
                </div>
              )}
            </div>
            {syncStatus.hint && <p style={{ color: "var(--text-secondary)" }}>{syncStatus.hint}</p>}
            {syncStatus.tables.length > 0 && (
              <div style={{ overflowX: "auto" }}>
                <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 13 }}>
                  <thead><tr style={{ textAlign: "left", color: "var(--text-secondary)" }}>
                    <th style={{ padding: "4px 8px" }}>Table</th><th style={{ padding: "4px 8px" }}>Linked</th>
                    <th style={{ padding: "4px 8px" }}>Source rows</th><th style={{ padding: "4px 8px" }}>Coverage</th>
                    <th style={{ padding: "4px 8px" }}>Last synced</th>
                  </tr></thead>
                  <tbody>
                    {syncStatus.tables.map(t => (
                      <tr key={t.table} style={{ borderTop: "1px solid var(--border-subtle)" }}>
                        <td style={{ padding: "4px 8px", fontFamily: "var(--font-mono, monospace)" }}>{t.table}</td>
                        <td style={{ padding: "4px 8px" }}>{t.linked_rows}</td>
                        <td style={{ padding: "4px 8px" }}>{t.source_rows ?? "?"}</td>
                        <td style={{ padding: "4px 8px", color: t.coverage_pct >= 99.9 ? "#22c55e" : "#f59e0b" }}>{t.coverage_pct}%</td>
                        <td style={{ padding: "4px 8px", color: "var(--text-secondary)" }}>
                          {t.last_synced_at ? new Date(t.last_synced_at).toLocaleString() : "—"}
                          {t.stale_hours != null && t.stale_hours > 1 ? ` (${t.stale_hours}h ago)` : ""}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {/* Discovery report */}
        {analysis && analysis.report && (
          <div style={S.card}>
            <div style={{ ...S.row, justifyContent: "space-between" }}>
              <h3 style={{ margin: 0 }}>Discovery — {analysis.table_count} tables</h3>
              <div style={{ textAlign: "right" }}>
                <div style={{ ...S.score, color: (analysis.quality_score ?? 0) >= 70 ? "#22c55e" : "#f59e0b" }}>{analysis.quality_score}</div>
                <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>data-quality</div>
              </div>
            </div>
            {analysis.report.quality.findings.map((f, i) => (
              <div key={i} style={{ ...S.row, margin: "6px 0" }}>
                <span style={{ ...S.pill, background: SEV_COLOR[f.severity] + "22", color: SEV_COLOR[f.severity] }}>{f.severity}</span>
                <span>{f.issue}{f.tables ? ` — ${f.tables.join(", ")}` : ""}</span>
              </div>
            ))}
            {/* Compliance (deep scan) */}
            {analysis.report.compliance && (
              <div style={{ marginTop: "var(--space-md)" }}>
                <h4 style={{ marginBottom: 6 }}>
                  Compliance — {analysis.report.compliance.summary.pii_column_count} sensitive column(s)
                  {analysis.report.compliance.summary.tokenize_required.length > 0 &&
                    <span style={{ ...S.pill, background: "#ef444422", color: "#ef4444", marginLeft: 8 }}>
                      {analysis.report.compliance.summary.tokenize_required.length} need tokenizing
                    </span>}
                </h4>
                {analysis.report.compliance.findings.length === 0
                  ? <p style={{ color: "var(--text-secondary)", fontSize: 13 }}>No PII/PHI detected in the sample.</p>
                  : <div style={{ overflowX: "auto" }}>
                      <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 13 }}>
                        <thead><tr style={{ textAlign: "left", color: "var(--text-secondary)" }}>
                          <th style={{ padding: "4px 8px" }}>Column</th><th style={{ padding: "4px 8px" }}>Category</th>
                          <th style={{ padding: "4px 8px" }}>Sensitivity</th><th style={{ padding: "4px 8px" }}>Action</th>
                          <th style={{ padding: "4px 8px" }}>Examples (masked)</th>
                        </tr></thead>
                        <tbody>
                          {analysis.report.compliance.findings.map((f, i) => (
                            <tr key={i} style={{ borderTop: "1px solid var(--border-subtle)" }}>
                              <td style={{ padding: "4px 8px", fontFamily: "var(--font-mono, monospace)" }}>{f.table}.{f.column}</td>
                              <td style={{ padding: "4px 8px" }}>{f.category}</td>
                              <td style={{ padding: "4px 8px" }}>{f.sensitivity}</td>
                              <td style={{ padding: "4px 8px" }}>
                                <span style={{ ...S.pill, background: f.recommended_action === "tokenize" ? "#ef444422" : "#f59e0b22", color: f.recommended_action === "tokenize" ? "#ef4444" : "#f59e0b" }}>{f.recommended_action}</span>
                              </td>
                              <td style={{ padding: "4px 8px", color: "var(--text-secondary)" }}>{(f.masked_examples || []).join(", ")}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>}
              </div>
            )}

            {/* AI narrative (optional) */}
            {analysis.report.ai_narrative && (
              <div style={{ marginTop: "var(--space-md)" }}>
                <h4 style={{ marginBottom: 6 }}>AI narrative</h4>
                <div style={S.mono}>{analysis.report.ai_narrative}</div>
              </div>
            )}

            <h4 style={{ marginTop: "var(--space-md)" }}>Schema Autobiography</h4>
            <div style={S.mono}>{analysis.report.autobiography}</div>
          </div>
        )}

        {/* P3 — Case-type generation */}
        {wfTables.length > 0 && (
          <div style={S.card}>
            <h3 style={{ marginTop: 0 }}>Generate case-types from workflow tables</h3>
            {wfTables.map(w => (
              <div key={w.table} style={{ ...S.row, justifyContent: "space-between", borderBottom: "1px solid var(--border-subtle)", padding: "8px 0" }}>
                <span><strong>{w.table}</strong> <span style={{ color: "var(--text-secondary)" }}>· status column: {w.status_column || "—"} · {w.column_count} cols</span></span>
                <Button onClick={() => genCaseType(w.table)} disabled={busy}>Generate</Button>
              </div>
            ))}
          </div>
        )}

        {/* Draft preview + Apply */}
        {draft && draft.definition_json && (
          <div style={S.card}>
            <div style={{ ...S.row, justifyContent: "space-between" }}>
              <h3 style={{ margin: 0 }}>Draft case-type: {draft.definition_json.name}</h3>
              <Button onClick={applyDraft} disabled={busy}>Apply — create case-type</Button>
            </div>
            <p style={{ color: "var(--text-secondary)", fontSize: 13 }}>{draft.rationale}</p>
            {(draft.warnings || []).map((w: string, i: number) => (
              <div key={i} style={{ ...S.row, margin: "4px 0" }}>
                <span style={{ ...S.pill, background: "#f59e0b22", color: "#f59e0b" }}>warning</span><span>{w}</span>
              </div>
            ))}
            {applyMsg && <div style={{ ...S.card, borderColor: "#22c55e", color: "#22c55e" }}>{applyMsg}</div>}
            {/* P4 — migrate data into the applied case-type */}
            {appliedCtId && (
              <div style={{ marginTop: "var(--space-md)", borderTop: "1px solid var(--border-subtle)", paddingTop: "var(--space-md)" }}>
                <h4 style={{ marginBottom: 6 }}>Migrate data from {draft.source_table}</h4>
                <div style={S.row}>
                  <div><label style={S.label}>PII mode</label>
                    <select style={{ ...S.input, minWidth: 130 }} value={piiMode} onChange={e => setPiiMode(e.target.value)}>
                      <option value="safe">safe (drop cards/SSNs)</option>
                      <option value="exclude_all">exclude_all PII</option>
                      <option value="as_is">as_is (copy all)</option>
                    </select></div>
                  <div><label style={S.label}>Row limit</label>
                    <input style={{ ...S.input, minWidth: 90 }} value={migLimit} onChange={e => setMigLimit(e.target.value)} /></div>
                  <label style={{ ...S.row, alignSelf: "flex-end" }}><input type="checkbox" checked={migDry} onChange={e => setMigDry(e.target.checked)} /> Dry run</label>
                  <Button onClick={migrateData} disabled={busy} style={{ alignSelf: "flex-end" }}>{migDry ? "Preview" : "Migrate rows"}</Button>
                  <span title="Incremental pass — new source rows become cases, changed rows update their linked case (idempotent)" style={{ alignSelf: "flex-end" }}>
                    <Button variant="ghost" onClick={syncNow} disabled={busy}>Sync now</Button>
                  </span>
                </div>
                {migResult && (
                  <div style={{ ...S.card, marginTop: "var(--space-md)", borderColor: migResult.dry_run ? "var(--border-subtle)" : "#22c55e" }}>
                    <strong>{migResult.dry_run ? "Dry run" : "Migrated"}</strong> — read {migResult.rows_read}, created {migResult.rows_migrated} case(s)
                    {migResult.rows_skipped_already_linked ? `, skipped ${migResult.rows_skipped_already_linked} already-linked` : ""}.
                    {migResult.excluded_columns.length > 0 &&
                      <div style={{ marginTop: 4, fontSize: 13 }}>Excluded (PII): <span style={{ color: "#ef4444" }}>{migResult.excluded_columns.join(", ")}</span></div>}
                    {migResult.dry_run && migResult.preview?.[0] &&
                      <div style={{ marginTop: 4, fontSize: 12, color: "var(--text-secondary)" }}>Sample fields: {Object.keys(migResult.preview[0]).join(", ")}</div>}
                  </div>
                )}
                {syncResult && (
                  <div style={{ ...S.card, marginTop: "var(--space-md)", borderColor: "#3b82f6" }}>
                    <strong>Sync pass</strong> — read {syncResult.rows_read}, created {syncResult.cases_created},
                    updated {syncResult.cases_updated}, unchanged {syncResult.rows_unchanged}
                    {syncResult.done ? " · table fully covered" : " · more rows remain (run again)"}
                  </div>
                )}
              </div>
            )}
            <div style={S.row}>
              <div><strong>Stages</strong>
                <div style={{ ...S.row, marginTop: 6 }}>
                  {draft.definition_json.stages.map((st: any, i: number) => (
                    <span key={st.id}>
                      <span style={{ ...S.pill, background: "var(--accentDim, rgba(13,148,136,0.15))", color: "var(--accent, #0d9488)" }}>{st.name}</span>
                      {i < draft.definition_json.stages.length - 1 && <span style={{ margin: "0 4px", color: "var(--text-secondary)" }}>&gt;</span>}
                    </span>
                  ))}
                </div>
              </div>
            </div>
            <h4 style={{ marginBottom: 6 }}>Fields</h4>
            <div style={{ overflowX: "auto" }}>
              <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 13 }}>
                <thead><tr style={{ textAlign: "left", color: "var(--text-secondary)" }}>
                  <th style={{ padding: "4px 8px" }}>Field</th><th style={{ padding: "4px 8px" }}>Type</th><th style={{ padding: "4px 8px" }}>Required</th><th style={{ padding: "4px 8px" }}>Options</th>
                </tr></thead>
                <tbody>
                  {draft.definition_json.fields.map((f: any) => (
                    <tr key={f.id} style={{ borderTop: "1px solid var(--border-subtle)" }}>
                      <td style={{ padding: "4px 8px" }}>{f.label}</td>
                      <td style={{ padding: "4px 8px", fontFamily: "var(--font-mono, monospace)" }}>{f.field_type}</td>
                      <td style={{ padding: "4px 8px" }}>{f.required ? "yes" : "no"}</td>
                      <td style={{ padding: "4px 8px", color: "var(--text-secondary)" }}>{(f.options || []).join(", ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
