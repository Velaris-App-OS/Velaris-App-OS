/**
 * P54 — HxMigrate: Unified Migration Intelligence Pipeline
 * Upload once → 5 automatic stages → deployable Helix app
 */
import React, { useState, useEffect, useCallback, useRef } from "react";
import { Button } from "@shared/components";

const API = "/api/v1/hxmigrate";
function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}
function authFetch(url: string, opts: RequestInit = {}): Promise<Response> {
  return fetch(url, { ...opts, headers: { ..._authHdr(), ...opts.headers } });
}


const STAGE_ICONS = ["", "🔍", "🤖", "⚙️", "📋", "📦"];
const STAGE_LABELS = ["", "Scout Assessment", "AI Analysis", "BPM Generation", "Orchestrator", "App Package"];

const STATUS_COLOR: Record<string, string> = {
  pending:   "#94a3b8",
  running:   "#3b82f6",
  completed: "#22c55e",
  failed:    "#ef4444",
  skipped:   "#f59e0b",
  partial:   "#f59e0b",
};

const S: Record<string, React.CSSProperties> = {
  page:    { padding: "var(--space-xl)", maxWidth: 1100, margin: "0 auto" },
  header:  { marginBottom: "var(--space-lg)" },
  title:   { fontSize: 24, fontWeight: 700, margin: 0 },
  sub:     { fontSize: 13, color: "var(--text-muted)", marginTop: 4 },
  card:    { background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 10, padding: "var(--space-lg)", marginBottom: "var(--space-md)" },
  label:   { fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase" as const, letterSpacing: "0.05em", marginBottom: 4 },
  value:   { fontSize: 13, color: "var(--text-primary)", marginBottom: 12 },
  badge:   { fontSize: 10, padding: "2px 8px", borderRadius: 4, fontWeight: 700, textTransform: "uppercase" as const },
  mono:    { fontFamily: "var(--font-mono)", fontSize: 11 },
};

// ── Stage Progress Bar ────────────────────────────────────────────────────────

function StageBar({ stages, currentStage }: { stages: any[]; currentStage: number }) {
  const stageMap = Object.fromEntries((stages ?? []).map(s => [s.stage, s]));

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 0, margin: "20px 0" }}>
      {[1, 2, 3, 4, 5].map((n, i) => {
        const s = stageMap[n];
        const status = s?.status ?? "pending";
        const color  = STATUS_COLOR[status] ?? "#94a3b8";
        const isActive = n === currentStage;
        return (
          <React.Fragment key={n}>
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", minWidth: 90 }}>
              <div style={{
                width: 40, height: 40, borderRadius: "50%",
                background: status === "completed" ? "#22c55e" : status === "running" ? "#3b82f6" : status === "failed" ? "#ef4444" : "var(--bg-elevated)",
                border: `2px solid ${color}`,
                display: "flex", alignItems: "center", justifyContent: "center",
                fontSize: 18,
                boxShadow: isActive ? `0 0 0 3px ${color}44` : "none",
                transition: "all 0.3s",
              }}>
                {status === "completed" ? "✓" : status === "failed" ? "✗" : STAGE_ICONS[n]}
              </div>
              <div style={{ fontSize: 10, marginTop: 6, color: isActive ? "var(--accent)" : "var(--text-muted)", fontWeight: isActive ? 700 : 400 }}>
                {STAGE_LABELS[n]}
              </div>
              <div style={{ ...S.badge, background: color + "22", color, marginTop: 4 }}>{status}</div>
              {s?.summary && Object.keys(s.summary).length > 0 && status === "completed" && (
                <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>
                  {Object.entries(s.summary).slice(0,2).map(([k,v]) => `${k}: ${v}`).join(" · ")}
                </div>
              )}
              {s?.error && <div style={{ fontSize: 10, color: "#ef4444", marginTop: 2, maxWidth: 80 }}>{s.error.slice(0,40)}</div>}
            </div>
            {n < 5 && (
              <div style={{ flex: 1, height: 2, background: stageMap[n]?.status === "completed" ? "#22c55e" : "var(--border-subtle)", margin: "0 4px", marginBottom: 30 }} />
            )}
          </React.Fragment>
        );
      })}
    </div>
  );
}

// ── Upload Form ───────────────────────────────────────────────────────────────

function UploadForm({ onStarted }: { onStarted: (runId: string) => void }) {
  const [platforms, setPlatforms] = useState<any[]>([]);
  const [platform, setPlatform]   = useState("pega");
  const [name, setName]           = useState("");
  const [file, setFile]           = useState<File | null>(null);
  const [mode, setMode]           = useState("full");
  const [loading, setLoading]     = useState(false);
  const [err, setErr]             = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    authFetch(`${API}/platforms`).then(r => r.ok ? r.json() : []).then(setPlatforms).catch(() => {});
  }, []);

  const handleSubmit = async () => {
    if (!file) { setErr("Select a file."); return; }
    setLoading(true); setErr(null);
    const fd = new FormData();
    fd.append("file", file);
    fd.append("source_platform", platform);
    fd.append("name", name || file.name);
    fd.append("mode", mode);
    const r = await authFetch(`${API}/run`, { method: "POST", body: fd });
    if (r.ok) {
      const data = await r.json();
      onStarted(data.run_id);
    } else {
      const e = await r.json().catch(() => ({}));
      setErr(e.detail || "Upload failed.");
    }
    setLoading(false);
  };

  const selected = platforms.find(p => p.id === platform);

  return (
    <div style={S.card}>
      <div style={{ fontSize: 15, fontWeight: 700, marginBottom: 16 }}>New Migration Run</div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 16 }}>
        <div>
          <div style={S.label}>Source Platform</div>
          <select value={platform} onChange={e => setPlatform(e.target.value)}
            style={{ width: "100%", padding: "8px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)" }}>
            {platforms.map(p => <option key={p.id} value={p.id}>{p.label}</option>)}
          </select>
        </div>
        <div>
          <div style={S.label}>Pipeline Mode</div>
          <select value={mode} onChange={e => setMode(e.target.value)}
            style={{ width: "100%", padding: "8px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)" }}>
            <option value="full">Full Run (all 5 stages)</option>
            <option value="step_by_step">Step by Step (pause between stages)</option>
          </select>
        </div>
      </div>

      <div style={S.label}>Run Name (optional)</div>
      <input value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Insurance Claims Migration v1"
        style={{ width: "100%", padding: "8px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box", marginBottom: 14 }} />

      <div style={S.label}>Upload Export File {selected && <span style={{ fontWeight: 400 }}>({selected.accepts.join(", ")})</span>}</div>
      <div
        onClick={() => fileRef.current?.click()}
        style={{ border: "2px dashed var(--border-subtle)", borderRadius: 8, padding: "24px 20px", cursor: "pointer", marginBottom: 14,
          background: file ? "color-mix(in srgb, #22c55e 5%, transparent)" : "var(--bg-elevated)",
          borderColor: file ? "#22c55e66" : "var(--border-subtle)" }}>
        <div style={{ fontSize: 28, marginBottom: 6 }}>📂</div>
        <div style={{ fontSize: 13, color: file ? "#22c55e" : "var(--text-muted)" }}>
          {file ? `✓ ${file.name} (${(file.size / 1024).toFixed(0)} KB)` : "Click to select or drag & drop"}
        </div>
        <input ref={fileRef} type="file" style={{ display: "none" }}
          accept=".jar,.zip,.xml,.bpmn,.json"
          onChange={e => setFile(e.target.files?.[0] ?? null)} />
      </div>

      {err && <div style={{ fontSize: 12, color: "#ef4444", marginBottom: 12 }}>{err}</div>}
      <Button disabled={loading || !file} onClick={handleSubmit}>
        {loading ? "Starting pipeline…" : "🚀 Start HxMigrate Pipeline"}
      </Button>
    </div>
  );
}

// ── Run Detail ────────────────────────────────────────────────────────────────

function RunDetail({ runId, onBack }: { runId: string; onBack: () => void }) {
  const [run, setRun] = useState<any | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    const r = await authFetch(`${API}/runs/${runId}`);
    if (r.ok) setRun(await r.json());
  }, [runId]);

  useEffect(() => {
    load();
    pollRef.current = setInterval(() => {
      setRun((prev: any) => {
        if (prev && ["completed", "failed"].includes(prev.status)) {
          clearInterval(pollRef.current!);
        }
        return prev;
      });
      load();
    }, 2500);
    return () => clearInterval(pollRef.current!);
  }, [load]);

  if (!run) return <div style={{ padding: 24, color: "var(--text-muted)" }}>Loading…</div>;

  const sc = STATUS_COLOR[run.status] ?? "#94a3b8";

  return (
    <div>
      <button onClick={onBack} style={{ fontSize: 13, color: "var(--accent)", background: "none", border: "none", cursor: "pointer", marginBottom: 16, padding: 0 }}>← Back to runs</button>

      <div style={S.card}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 4 }}>
          <div style={{ fontSize: 18, fontWeight: 700 }}>{run.name}</div>
          <span style={{ ...S.badge, background: sc + "22", color: sc }}>{run.status}</span>
        </div>
        <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 16 }}>
          {run.source_platform} · {run.source_filename} · {run.mode} mode · {new Date(run.created_at).toLocaleString()}
        </div>

        <StageBar stages={run.stages} currentStage={run.current_stage} />

        {run.status === "running" && (
          <div style={{ fontSize: 12, color: "#3b82f6", display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ animation: "spin 1s linear infinite", display: "inline-block" }}>⟳</span>
            Pipeline running — auto-refreshing every 2.5 s…
          </div>
        )}

        {run.error && (
          <div style={{ marginTop: 12, padding: "10px 14px", background: "#ef444422", borderRadius: 6, fontSize: 12, color: "#ef4444" }}>
            {run.error}
          </div>
        )}

        {run.status === "completed" && (
          <div style={{ marginTop: 16, display: "flex", gap: 10, flexWrap: "wrap" as const }}>
            {run.scan_id && <div style={{ ...S.badge, background: "#3b82f622", color: "#3b82f6" }}>Scout scan: {String(run.scan_id).slice(0,8)}…</div>}
            {run.import_job_id && <div style={{ ...S.badge, background: "#0d948822", color: "#0d9488" }}>Import job: {String(run.import_job_id).slice(0,8)}…</div>}
            {run.project_id && <div style={{ ...S.badge, background: "#f59e0b22", color: "#f59e0b" }}>Project: {String(run.project_id).slice(0,8)}…</div>}
            {run.package_id && <div style={{ ...S.badge, background: "#22c55e22", color: "#22c55e" }}>Package: {String(run.package_id).slice(0,8)}…</div>}
          </div>
        )}

        {run.status === "completed" && (
          <div style={{ marginTop: 16 }}>
            <a href={`${API}/runs/${runId}/result`} target="_blank" rel="noreferrer">
              <Button size="sm">Download Result JSON</Button>
            </a>
          </div>
        )}
      </div>

      {/* Stage details */}
      {(run.stages ?? []).filter((s: any) => s.status !== "pending").map((s: any) => (
        <div key={s.stage} style={{ ...S.card, borderLeft: `3px solid ${STATUS_COLOR[s.status] ?? "#94a3b8"}` }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
            <div style={{ fontWeight: 700, fontSize: 14 }}>{STAGE_ICONS[s.stage]} {s.stage_name}</div>
            <span style={{ ...S.badge, background: (STATUS_COLOR[s.status] ?? "#94a3b8") + "22", color: STATUS_COLOR[s.status] ?? "#94a3b8" }}>{s.status}</span>
          </div>
          {s.started_at && <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 8 }}>Started: {new Date(s.started_at).toLocaleTimeString()} {s.finished_at ? `· Finished: ${new Date(s.finished_at).toLocaleTimeString()}` : ""}</div>}
          {Object.keys(s.summary ?? {}).length > 0 && (
            <div style={{ display: "flex", gap: 12, flexWrap: "wrap" as const }}>
              {Object.entries(s.summary).map(([k, v]) => (
                <div key={k} style={{ fontSize: 12 }}>
                  <span style={{ color: "var(--text-muted)" }}>{k}: </span>
                  <span style={{ fontWeight: 600 }}>{typeof v === "object" ? JSON.stringify(v) : String(v)}</span>
                </div>
              ))}
            </div>
          )}
          {s.error && <div style={{ fontSize: 12, color: "#ef4444", marginTop: 6 }}>{s.error}</div>}
        </div>
      ))}
    </div>
  );
}

// ── Runs List ─────────────────────────────────────────────────────────────────

const RUNS_PAGE_SIZE = 10;

const runsPagBtnStyle: React.CSSProperties = {
  width: 28, height: 28, borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)",
  background: "transparent", color: "var(--text-secondary)", fontSize: 12,
  cursor: "pointer", fontFamily: "var(--font-mono)", display: "flex", alignItems: "center", justifyContent: "center",
};

function RunsPaginationBar({ page, totalPages, total, onChange }: {
  page: number; totalPages: number; total: number; onChange: (p: number) => void;
}) {
  const pages: (number | "…")[] = [];
  for (let i = 1; i <= totalPages; i++) {
    if (i === 1 || i === totalPages || Math.abs(i - page) <= 1) {
      pages.push(i);
    } else if (pages[pages.length - 1] !== "…") {
      pages.push("…");
    }
  }
  return (
    <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
      <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginRight: 4, whiteSpace: "nowrap" }}>
        {(page - 1) * RUNS_PAGE_SIZE + 1}–{Math.min(page * RUNS_PAGE_SIZE, total)} / {total}
      </span>
      <button onClick={() => onChange(page - 1)} disabled={page === 1}
        style={{ ...runsPagBtnStyle, opacity: page === 1 ? 0.35 : 1 }}>‹</button>
      {pages.map((p, i) =>
        p === "…" ? (
          <span key={`e${i}`} style={{ fontSize: 11, color: "var(--text-muted)", padding: "0 2px" }}>…</span>
        ) : (
          <button key={p} onClick={() => onChange(p as number)}
            style={{ ...runsPagBtnStyle, background: page === p ? "var(--accent)" : "transparent", color: page === p ? "#fff" : "var(--text-secondary)", borderColor: page === p ? "var(--accent)" : "var(--border-default)" }}>
            {p}
          </button>
        )
      )}
      <button onClick={() => onChange(page + 1)} disabled={page >= totalPages}
        style={{ ...runsPagBtnStyle, opacity: page >= totalPages ? 0.35 : 1 }}>›</button>
    </div>
  );
}

function RunsList({ onSelect }: { onSelect: (runId: string) => void }) {
  const [runs, setRuns] = useState<any[]>([]);
  const [page, setPage] = useState(1);

  useEffect(() => {
    authFetch(`${API}/runs`).then(r => r.ok ? r.json() : []).then(setRuns).catch(() => {});
  }, []);

  if (runs.length === 0) return <div style={{ fontSize: 13, color: "var(--text-muted)" }}>No runs yet. Start one above.</div>;

  const total = runs.length;
  const totalPages = Math.ceil(total / RUNS_PAGE_SIZE);
  const pageRuns = runs.slice((page - 1) * RUNS_PAGE_SIZE, page * RUNS_PAGE_SIZE);

  return (
    <div style={S.card}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <div style={{ fontSize: 14, fontWeight: 700 }}>Recent Runs</div>
        {totalPages > 1 && <RunsPaginationBar page={page} totalPages={totalPages} total={total} onChange={setPage} />}
      </div>
      {pageRuns.map(run => {
        const sc = STATUS_COLOR[run.status] ?? "#94a3b8";
        return (
          <div key={run.id} onClick={() => onSelect(run.id)}
            style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "10px 0", borderBottom: "1px solid var(--border-subtle)", cursor: "pointer" }}>
            <div>
              <div style={{ fontWeight: 600, fontSize: 13 }}>{run.name}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{run.source_platform} · {run.source_filename} · {new Date(run.created_at).toLocaleString()}</div>
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Stage {run.current_stage}/5</div>
              <span style={{ ...S.badge, background: sc + "22", color: sc }}>{run.status}</span>
            </div>
          </div>
        );
      })}
      {totalPages > 1 && (
        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 12 }}>
          <RunsPaginationBar page={page} totalPages={totalPages} total={total} onChange={setPage} />
        </div>
      )}
    </div>
  );
}

// ── Root ──────────────────────────────────────────────────────────────────────

export default function HxMigrate() {
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);

  const handleStarted = (runId: string) => {
    setSelectedRunId(runId);
  };

  if (selectedRunId) {
    return (
      <div style={S.page}>
        <RunDetail runId={selectedRunId} onBack={() => { setSelectedRunId(null); setRefresh(r => r + 1); }} />
      </div>
    );
  }

  return (
    <div style={S.page}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: "var(--space-lg)" }}>
        <div style={{ display: "grid", gridTemplateColumns: "32px 1fr", gap: 0, marginBottom: 20 }}>
          {[1,2,3,4,5].map(n => (
            <React.Fragment key={n}>
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
                <div style={{ width: 28, height: 28, borderRadius: "50%", background: "var(--bg-elevated)", border: "2px solid var(--border)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 14, flexShrink: 0 }}>{STAGE_ICONS[n]}</div>
                {n < 5 && <div style={{ width: 2, flex: 1, background: "var(--border-subtle)", minHeight: 16 }} />}
              </div>
              <div style={{ paddingLeft: 12, paddingBottom: n < 5 ? 16 : 0 }}>
                <div style={{ fontWeight: 600, fontSize: 13, marginTop: 4 }}>{STAGE_LABELS[n]}</div>
                <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                  {[
                    "Classify all artifacts by type and migration compatibility. Estimate effort.",
                    "HxNexus deep analysis on complex artifacts — business logic, external calls, data flows.",
                    "Auto-generate Velaris case types, forms, SLAs, migration SQL from the enriched manifest.",
                    "Create migration project for artifacts needing human review. Auto-complete FULL/HIGH tasks.",
                    "Bundle all generated objects into a versioned, promotable App Registry package.",
                  ][n - 1]}
                </div>
              </div>
            </React.Fragment>
          ))}
        </div>

        <UploadForm onStarted={handleStarted} key={refresh} />
        <RunsList onSelect={setSelectedRunId} key={`list-${refresh}`} />
      </div>
    </div>
  );
}
