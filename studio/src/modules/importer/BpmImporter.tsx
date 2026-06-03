/**
 * P44 — BPM Importer: Upload a Pega JAR, Camunda BPMN, Appian ZIP, or
 * ServiceNow XML export and convert it into a Helix application.
 * Tabs: Import · Jobs · Result
 */
import React, { useState, useEffect, useCallback, useRef } from "react";

const API = "/api/v1/importer";

type Job = {
  id: string; tool: string; filename: string; status: string;
  created_by: string | null; created_at: string | null; completed_at: string | null;
};
type JobDetail = Job & {
  manifest: { total_files: number; skipped_files: number; type_counts: Record<string, number> };
  error?: string;
};
type Report = {
  tool: string; filename: string;
  summary: { extracted_total: number; auto_converted: number; needs_review: number; no_equivalent: number; conversion_pct: number };
  confidence: { exact: number; close: number; partial: number; manual: number };
  generated: { case_types: number; forms: number; sla_rules: number; access_groups: number };
  items_converted: { type: string; name: string; confidence: string }[];
  needs_review: { name: string; rule_type: string; helix_suggestion: string | null; confidence: string }[];
  no_equivalent: { name: string; rule_type: string }[];
};

const STATUS_COLOR: Record<string, string> = {
  pending: "#94a3b8", extracting: "#3b82f6", parsing: "#0d9488",
  mapping: "#0f766e", generating: "#f59e0b", complete: "#22c55e", failed: "#ef4444",
};
const TOOL_LABEL: Record<string, string> = {
  pega: "Pega", camunda: "Camunda", appian: "Appian", servicenow: "ServiceNow",
};
const CONF_COLOR: Record<string, string> = {
  exact: "#22c55e", close: "#3b82f6", partial: "#f59e0b", manual: "#ef4444",
};

const S: Record<string, React.CSSProperties> = {
  page:      { display: "flex", flexDirection: "column", height: "100%", background: "var(--bg-main)", color: "var(--text-primary)", fontFamily: "system-ui, sans-serif" },
  header:    { padding: "18px 24px 0", flexShrink: 0 },
  title:     { fontSize: 20, fontWeight: 700, margin: 0 },
  sub:       { fontSize: 13, color: "var(--text-secondary)", marginTop: 4 },
  tabs:      { display: "flex", gap: 4, padding: "14px 24px 0", borderBottom: "1px solid var(--border)", flexShrink: 0 },
  tab:       { padding: "8px 20px", border: "none", background: "none", fontSize: 13, cursor: "pointer", borderBottom: "2px solid transparent", fontWeight: 400, color: "var(--text-secondary)" },
  tabActive: { borderBottom: "2px solid var(--accent)", color: "var(--accent)", fontWeight: 700 },
  body:      { flex: 1, overflow: "hidden", display: "flex" },
  sidebar:   { width: 280, borderRight: "1px solid var(--border)", display: "flex", flexDirection: "column", background: "var(--bg-surface)", flexShrink: 0 },
  sideHead:  { padding: "12px 14px", borderBottom: "1px solid var(--border)", fontSize: 12, fontWeight: 700, color: "var(--text-secondary)", flexShrink: 0 },
  list:      { flex: 1, overflow: "auto" },
  detail:    { flex: 1, overflow: "auto", padding: "24px 28px" },
  btn:       { padding: "8px 18px", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12, fontWeight: 700 },
  btnPrimary:{ background: "var(--accent)", color: "#fff" },
  btnSecond: { background: "var(--bg-surface)", border: "1px solid var(--border)", color: "var(--text-secondary)" },
  badge:     { display: "inline-block", padding: "2px 8px", borderRadius: 10, fontSize: 10, fontWeight: 700, textTransform: "uppercase" as const },
  input:     { width: "100%", padding: "7px 10px", border: "1px solid var(--border)", borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: "var(--bg-main)", color: "var(--text-primary)" },
  label:     { fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", marginBottom: 4, display: "block" },
  card:      { background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: 8, padding: "14px 18px", marginBottom: 12 },
};

function StatusBadge({ status }: { status: string }) {
  const c = STATUS_COLOR[status] || "#94a3b8";
  return <span style={{ ...S.badge, background: c + "22", color: c }}>{status}</span>;
}


// ── Import tab ────────────────────────────────────────────────────────────────

function ImportTab({ onJobCreated }: { onJobCreated: (id: string) => void }) {
  const [tool, setTool] = useState("pega");
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const TOOLS = [
    { id: "pega", label: "Pega", ext: ".jar .zip", desc: "Pega application export (JAR or ZIP containing Flow, Section, SLARule XML)" },
    { id: "camunda", label: "Camunda", ext: ".bpmn .xml", desc: "Camunda BPMN 2.0 XML file" },
    { id: "appian", label: "Appian", ext: ".zip .xml .json", desc: "Appian application package (ZIP with process models and record types)" },
    { id: "servicenow", label: "ServiceNow", ext: ".xml .zip", desc: "ServiceNow update set or workflow export XML" },
  ];

  async function handleUpload(e: React.FormEvent) {
    e.preventDefault();
    if (!file) return;
    setUploading(true); setErr(null);
    try {
      const fd = new FormData();
      fd.append("tool", tool);
      fd.append("file", file);
      const r = await fetch(`${API}/upload`, { method: "POST", body: fd });
      if (!r.ok) { setErr((await r.json()).detail || "Upload failed"); return; }
      const d = await r.json();
      onJobCreated(d.job_id);
    } catch (e: any) {
      setErr(e.message);
    } finally { setUploading(false); }
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault(); setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) setFile(f);
  }

  const selected = TOOLS.find(t => t.id === tool)!;

  return (
    <div style={{ flex: 1, overflow: "auto", padding: "28px 32px", maxWidth: 640 }}>
      <h2 style={{ margin: "0 0 6px", fontSize: 16, fontWeight: 700 }}>Import a BPM Application</h2>
      <p style={{ fontSize: 13, color: "var(--text-secondary)", marginTop: 0, marginBottom: 24 }}>
        Upload your export file. The importer runs a five-pass pipeline: extract → parse → map → generate → report.
      </p>

      {/* Tool selector */}
      <div style={{ marginBottom: 20 }}>
        <span style={S.label}>Source BPM Tool</span>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {TOOLS.map(t => (
            <button key={t.id} onClick={() => setTool(t.id)} style={{
              ...S.btn,
              background: tool === t.id ? "var(--accent)" : "var(--bg-surface)",
              color: tool === t.id ? "#fff" : "var(--text-secondary)",
              border: `1px solid ${tool === t.id ? "var(--accent)" : "var(--border)"}`,
            }}>{t.label}</button>
          ))}
        </div>
        <div style={{ marginTop: 8, fontSize: 12, color: "var(--text-secondary)" }}>
          {selected.desc} — accepts {selected.ext}
        </div>
      </div>

      {/* File drop zone */}
      <form onSubmit={handleUpload}>
        <div
          onDragOver={e => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={handleDrop}
          onClick={() => fileRef.current?.click()}
          style={{
            border: `2px dashed ${dragging ? "var(--accent)" : "var(--border)"}`,
            borderRadius: 10, padding: "32px 20px",
            cursor: "pointer",
            background: dragging ? "var(--accent-light, #ede9fe)" : "var(--bg-surface)",
            marginBottom: 16, transition: "all 0.15s",
          }}>
          <input ref={fileRef} type="file" hidden
                 accept=".jar,.zip,.xml,.bpmn,.json"
                 onChange={e => setFile(e.target.files?.[0] ?? null)} />
          {file ? (
            <div>
              <div style={{ fontSize: 15, fontWeight: 700 }}>{file.name}</div>
              <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 4 }}>
                {(file.size / 1024).toFixed(1)} KB · click or drop to replace
              </div>
            </div>
          ) : (
            <div>
              <div style={{ fontSize: 32, marginBottom: 8 }}>⬆</div>
              <div style={{ fontSize: 14, fontWeight: 600 }}>Drop your {selected.label} export here</div>
              <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 4 }}>or click to browse</div>
            </div>
          )}
        </div>

        {err && <div style={{ fontSize: 12, color: "#ef4444", marginBottom: 10 }}>{err}</div>}

        <button type="submit" disabled={!file || uploading} style={{ ...S.btn, ...S.btnPrimary, fontSize: 13 }}>
          {uploading ? "Uploading…" : "⟳ Start Import"}
        </button>
      </form>

      {/* What the importer does */}
      <div style={{ ...S.card, marginTop: 28 }}>
        <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-secondary)", marginBottom: 10 }}>FIVE-PASS PIPELINE</div>
        {[
          ["1 Extract", "Unpack JAR/ZIP, classify each file by rule type"],
          ["2 Parse",   "Convert rule XML/JSON into structured Velaris-readable format"],
          ["3 Map",     "Use P42 BPM knowledge base to find Velaris equivalents"],
          ["4 Generate","Produce case type JSON, form schemas, and migration SQL"],
          ["5 Report",  "Summarise: auto-converted, needs review, no equivalent"],
        ].map(([step, desc]) => (
          <div key={step} style={{ display: "flex", gap: 10, marginBottom: 6 }}>
            <span style={{ fontWeight: 700, fontSize: 12, color: "var(--accent)", minWidth: 80 }}>{step}</span>
            <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>{desc}</span>
          </div>
        ))}
      </div>
    </div>
  );
}


// ── Jobs tab ──────────────────────────────────────────────────────────────────

function JobsTab({ highlightId, onSelectJob }: { highlightId: string | null; onSelectJob: (id: string) => void }) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [selected, setSelected] = useState<string | null>(highlightId);
  const [detail, setDetail] = useState<JobDetail | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    const r = await fetch(`${API}/jobs`);
    if (r.ok) setJobs((await r.json()).jobs);
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { if (highlightId) { setSelected(highlightId); loadDetail(highlightId); } }, [highlightId]);

  async function loadDetail(id: string) {
    setDetail(null); setReport(null);
    const r = await fetch(`${API}/jobs/${id}`);
    if (r.ok) {
      const d: JobDetail = await r.json();
      setDetail(d);
      if (d.status === "complete") loadReport(id);
      else if (!["failed"].includes(d.status)) startPolling(id);
    }
  }

  async function loadReport(id: string) {
    const r = await fetch(`${API}/jobs/${id}/report`);
    if (r.ok) setReport(await r.json());
  }

  function startPolling(id: string) {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      const r = await fetch(`${API}/jobs/${id}`);
      if (!r.ok) return;
      const d: JobDetail = await r.json();
      setDetail(d);
      if (d.status === "complete") {
        clearInterval(pollRef.current!);
        loadReport(id);
        load();
      } else if (d.status === "failed") {
        clearInterval(pollRef.current!);
        load();
      }
    }, 1500);
  }

  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  async function handleDelete(id: string) {
    await fetch(`${API}/jobs/${id}`, { method: "DELETE" });
    if (selected === id) { setSelected(null); setDetail(null); setReport(null); }
    await load();
  }

  return (
    <div style={S.body}>
      {/* Sidebar */}
      <div style={S.sidebar}>
        <div style={{ ...S.sideHead, display: "flex", alignItems: "center" }}>
          <span style={{ flex: 1 }}>IMPORT JOBS</span>
          <button style={{ ...S.btn, ...S.btnSecond, padding: "3px 8px", fontSize: 10 }} onClick={load}>↻</button>
        </div>
        <div style={S.list}>
          {jobs.map(j => (
            <div key={j.id}
                 onClick={() => { setSelected(j.id); loadDetail(j.id); }}
                 style={{
                   padding: "10px 14px", cursor: "pointer", borderBottom: "1px solid var(--border)",
                   background: selected === j.id ? "var(--accent-light, #ede9fe)" : "transparent",
                 }}>
              <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 3 }}>{j.filename}</div>
              <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <span style={{ ...S.badge, background: "#0d948822", color: "#0d9488", fontSize: 9 }}>
                  {TOOL_LABEL[j.tool] || j.tool}
                </span>
                <StatusBadge status={j.status} />
              </div>
            </div>
          ))}
          {jobs.length === 0 && (
            <div style={{ padding: 16, fontSize: 12, color: "var(--text-secondary)" }}>No import jobs yet.</div>
          )}
        </div>
      </div>

      {/* Detail */}
      <div style={S.detail}>
        {!selected && (
          <div style={{ color: "var(--text-secondary)", fontSize: 13, paddingTop: 40 }}>
            Select a job to see its status and report.
          </div>
        )}
        {detail && (
          <>
            <div style={{ display: "flex", alignItems: "flex-start", gap: 16, marginBottom: 20 }}>
              <div style={{ flex: 1 }}>
                <h2 style={{ margin: 0, fontSize: 16, fontWeight: 700 }}>{detail.filename}</h2>
                <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 4 }}>
                  {TOOL_LABEL[detail.tool] || detail.tool} ·{" "}
                  {detail.created_at ? new Date(detail.created_at).toLocaleString() : "—"}
                </div>
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <StatusBadge status={detail.status} />
                <button style={{ ...S.btn, ...S.btnSecond, padding: "4px 10px", fontSize: 11 }}
                        onClick={() => handleDelete(detail.id)}>Delete</button>
              </div>
            </div>

            {/* Progress pipeline */}
            <div style={S.card}>
              <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-secondary)", marginBottom: 10 }}>PIPELINE STATUS</div>
              <div style={{ display: "flex", gap: 0 }}>
                {["pending", "extracting", "parsing", "mapping", "generating", "complete"].map((s, i) => {
                  const statusOrder = ["pending", "extracting", "parsing", "mapping", "generating", "complete", "failed"];
                  const currentIdx = statusOrder.indexOf(detail.status);
                  const stepIdx = statusOrder.indexOf(s);
                  const done = detail.status === "complete" || (currentIdx > stepIdx && currentIdx !== -1);
                  const active = detail.status === s;
                  const color = done ? "#22c55e" : active ? STATUS_COLOR[s] : "var(--border)";
                  return (
                    <div key={s} style={{ flex: 1 }}>
                      <div style={{
                        height: 4, background: color,
                        borderRadius: i === 0 ? "4px 0 0 4px" : i === 5 ? "0 4px 4px 0" : 0,
                        marginBottom: 6, transition: "background 0.3s",
                      }} />
                      <div style={{ fontSize: 9, color: active ? color : "var(--text-secondary)", fontWeight: active ? 700 : 400 }}>
                        {s}
                      </div>
                    </div>
                  );
                })}
              </div>
              {detail.status === "failed" && detail.error && (
                <div style={{ marginTop: 10, fontSize: 12, color: "#ef4444", background: "#ef444411", borderRadius: 6, padding: 8 }}>
                  {detail.error}
                </div>
              )}
            </div>

            {/* Manifest */}
            {detail.manifest && detail.manifest.total_files > 0 && (
              <div style={S.card}>
                <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-secondary)", marginBottom: 10 }}>EXTRACTED FILES</div>
                <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
                  <div>
                    <div style={{ fontSize: 10, color: "var(--text-secondary)" }}>TOTAL</div>
                    <div style={{ fontSize: 20, fontWeight: 700, color: "var(--accent)" }}>{detail.manifest.total_files}</div>
                  </div>
                  <div>
                    <div style={{ fontSize: 10, color: "var(--text-secondary)" }}>SKIPPED</div>
                    <div style={{ fontSize: 20, fontWeight: 700, color: "#94a3b8" }}>{detail.manifest.skipped_files}</div>
                  </div>
                  {Object.entries(detail.manifest.type_counts || {}).map(([t, n]) => (
                    <div key={t}>
                      <div style={{ fontSize: 10, color: "var(--text-secondary)" }}>{t.toUpperCase()}</div>
                      <div style={{ fontSize: 20, fontWeight: 700 }}>{n as number}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Report */}
            {report && <ReportView report={report} />}
          </>
        )}
      </div>
    </div>
  );
}


// ── Report view ───────────────────────────────────────────────────────────────

function ReportView({ report }: { report: Report }) {
  const { summary, confidence, generated, items_converted, needs_review, no_equivalent } = report;

  return (
    <>
      {/* Headline metrics */}
      <div style={S.card}>
        <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-secondary)", marginBottom: 10 }}>IMPORT REPORT</div>
        <div style={{ display: "flex", gap: 24, flexWrap: "wrap", marginBottom: 16 }}>
          <div>
            <div style={{ fontSize: 10, color: "var(--text-secondary)" }}>AUTO-CONVERTED</div>
            <div style={{ fontSize: 28, fontWeight: 700, color: "#22c55e" }}>{summary.auto_converted}</div>
          </div>
          <div>
            <div style={{ fontSize: 10, color: "var(--text-secondary)" }}>NEEDS REVIEW</div>
            <div style={{ fontSize: 28, fontWeight: 700, color: "#f59e0b" }}>{summary.needs_review}</div>
          </div>
          <div>
            <div style={{ fontSize: 10, color: "var(--text-secondary)" }}>NO EQUIVALENT</div>
            <div style={{ fontSize: 28, fontWeight: 700, color: "#ef4444" }}>{summary.no_equivalent}</div>
          </div>
          <div>
            <div style={{ fontSize: 10, color: "var(--text-secondary)" }}>CONVERSION %</div>
            <div style={{ fontSize: 28, fontWeight: 700, color: "var(--accent)" }}>{summary.conversion_pct}%</div>
          </div>
        </div>

        {/* Generated counts */}
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
          {Object.entries(generated).map(([k, v]) => v > 0 && (
            <div key={k} style={{ fontSize: 12 }}>
              <span style={{ fontWeight: 700, color: "var(--accent)" }}>{v as number}</span>
              {" "}<span style={{ color: "var(--text-secondary)" }}>{k.replace(/_/g, " ")}</span>
            </div>
          ))}
        </div>
      </div>

      {/* Converted items */}
      {items_converted.length > 0 && (
        <div style={S.card}>
          <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-secondary)", marginBottom: 8 }}>CONVERTED ({items_converted.length})</div>
          {items_converted.map((item, i) => (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 0", borderBottom: "1px solid var(--border)" }}>
              <span style={{ ...S.badge, background: "#0d948822", color: "#0d9488", fontSize: 9 }}>{item.type}</span>
              <span style={{ flex: 1, fontSize: 12 }}>{item.name}</span>
              <span style={{ ...S.badge, background: (CONF_COLOR[item.confidence] || "#6b7280") + "22", color: CONF_COLOR[item.confidence] || "#6b7280" }}>
                {item.confidence}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Needs review */}
      {needs_review.length > 0 && (
        <div style={S.card}>
          <div style={{ fontSize: 12, fontWeight: 700, color: "#f59e0b", marginBottom: 8 }}>NEEDS REVIEW ({needs_review.length})</div>
          {needs_review.map((item, i) => (
            <div key={i} style={{ padding: "5px 0", borderBottom: "1px solid var(--border)" }}>
              <div style={{ fontSize: 12, fontWeight: 600 }}>{item.name} <span style={{ fontWeight: 400, color: "var(--text-secondary)" }}>({item.rule_type})</span></div>
              {item.helix_suggestion && (
                <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>Suggested: {item.helix_suggestion}</div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* No equivalent */}
      {no_equivalent.length > 0 && (
        <div style={S.card}>
          <div style={{ fontSize: 12, fontWeight: 700, color: "#ef4444", marginBottom: 8 }}>NO EQUIVALENT ({no_equivalent.length})</div>
          {no_equivalent.map((item, i) => (
            <div key={i} style={{ fontSize: 12, padding: "3px 0", color: "var(--text-secondary)" }}>
              {item.name} ({item.rule_type})
            </div>
          ))}
        </div>
      )}
    </>
  );
}


// ── Root ──────────────────────────────────────────────────────────────────────

export default function BpmImporter() {
  const [tab, setTab] = useState<"import" | "jobs">("import");
  const [newJobId, setNewJobId] = useState<string | null>(null);

  function handleJobCreated(id: string) {
    setNewJobId(id);
    setTab("jobs");
  }

  const tabs: { key: typeof tab; label: string }[] = [
    { key: "import", label: "Import" },
    { key: "jobs",   label: "Jobs" },
  ];

  return (
    <div style={S.page}>
      <div style={S.tabs}>
        {tabs.map(t => (
          <button key={t.key} onClick={() => setTab(t.key)}
                  style={{ ...S.tab, ...(tab === t.key ? S.tabActive : {}) }}>
            {t.label}
          </button>
        ))}
      </div>
      <div style={S.body}>
        {tab === "import" && <ImportTab onJobCreated={handleJobCreated} />}
        {tab === "jobs"   && <JobsTab highlightId={newJobId} onSelectJob={() => {}} />}
      </div>
    </div>
  );
}
