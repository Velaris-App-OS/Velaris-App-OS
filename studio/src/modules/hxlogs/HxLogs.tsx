/**
 * P63 — HxLogs: AI-Driven Log Analyser
 *
 * Unified log viewer for all Helix services. Select lines and click
 * "Analyse →" to get HxNexus root-cause analysis on any error or traceback.
 */
import React, { useState, useEffect, useCallback, useRef } from "react";

const API  = "/api/v1/hxlogs";
const AUTH = () => ({ Authorization: `Bearer ${localStorage.getItem("helix_token") ?? ""}` });

// Single source of truth for column widths — header + rows both reference this
const LOG_COLS = "148px 54px 90px 1fr";

type LogEntry = {
  service:      string;
  level:        string;
  line_no:      number;
  occurred_at:  string | null;
  message:      string;
  raw:          string;
  is_traceback: boolean;
  frames:       string[];
  innermost:    string | null;
};

type Analysis = {
  summary:            string;
  root_cause:         string;
  likely_file:        string | null;
  suggested_fix:      string;
  severity:           string;
  related_components: string[];
  ai_available:       boolean;
};

const LEVEL_COLOR: Record<string, string> = {
  ERROR:    "#ef4444",
  CRITICAL: "#dc2626",
  WARNING:  "#f59e0b",
  WARN:     "#f59e0b",
  INFO:     "#3b82f6",
  DEBUG:    "#94a3b8",
};

const SEV_COLOR: Record<string, string> = {
  critical: "#dc2626", high: "#ef4444", medium: "#f59e0b", low: "#22c55e", unknown: "#94a3b8",
};

function timeNow(): string {
  return new Date().toLocaleTimeString([], { hour12: false });
}

async function apiFetch(method: string, path: string, body?: unknown) {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", ...AUTH() },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(r.statusText);
  return r.json();
}

export default function HxLogs() {
  const [entries, setEntries]         = useState<LogEntry[]>([]);
  const [services, setServices]       = useState<{ service: string; available: boolean }[]>([]);
  const [service, setService]         = useState<string>("");
  const [severity, setSeverity]       = useState<string>("");
  const [since, setSince]             = useState<number>(60);
  const [loading, setLoading]         = useState(false);
  const [lastRefresh, setLastRefresh] = useState<string>("");
  const [selected, setSelected]       = useState<Set<number>>(new Set());
  const [expanded, setExpanded]       = useState<Set<number>>(new Set());
  const [analysis, setAnalysis]       = useState<Analysis | null>(null);
  const [analysing, setAnalysing]     = useState(false);
  const [analyseErr, setAnalyseErr]   = useState<string | null>(null);

  useEffect(() => {
    apiFetch("GET", `${API}/services`).then(d => setServices(d.services ?? [])).catch(() => {});
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const qs = new URLSearchParams({ since_minutes: String(since), limit: "300" });
      if (service)  qs.set("service",  service);
      if (severity) qs.set("severity", severity);
      const d = await apiFetch("GET", `${API}/entries?${qs}`);
      setEntries(d.entries ?? []);
      setLastRefresh(timeNow());
      setSelected(new Set());
      setAnalysis(null);
    } catch { /* ignore */ }
    finally { setLoading(false); }
  }, [service, severity, since]);

  useEffect(() => { load(); }, [load]);

  const toggleSelect = (idx: number) => {
    setSelected(prev => {
      const next = new Set(prev);
      next.has(idx) ? next.delete(idx) : next.add(idx);
      return next;
    });
  };

  const toggleExpand = (idx: number) => {
    setExpanded(prev => {
      const next = new Set(prev);
      next.has(idx) ? next.delete(idx) : next.add(idx);
      return next;
    });
  };

  const selectedText = entries
    .filter((_, i) => selected.has(i))
    .map(e => e.raw)
    .join("\n\n");

  const analyse = async () => {
    if (!selectedText && entries.length === 0) return;
    const text = selectedText || entries.filter(e => e.level === "ERROR" || e.is_traceback).slice(0, 5).map(e => e.raw).join("\n\n");
    setAnalysing(true); setAnalyseErr(null); setAnalysis(null);
    try {
      const r = await apiFetch("POST", `${API}/analyse`, { log_text: text });
      setAnalysis(r);
    } catch (e: any) { setAnalyseErr(e.message); }
    finally { setAnalysing(false); }
  };

  const errorCount = entries.filter(e => e.level === "ERROR" || e.is_traceback).length;
  const warnCount  = entries.filter(e => e.level === "WARNING" || e.level === "WARN").length;

  return (
    <div style={{ display: "flex", height: "100%", fontFamily: "var(--font-mono, monospace)", fontSize: 12, background: "var(--bg-main)", color: "var(--text-primary)" }}>

      {/* ── Left: log feed ── */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden", minWidth: 0 }}>

        {/* Toolbar */}
        <div style={{ padding: "10px 16px", borderBottom: "1px solid var(--border)", display: "flex", gap: 8, alignItems: "center", flexShrink: 0, flexWrap: "wrap" }}>
          <span style={{ fontWeight: 700, fontSize: 14, color: "var(--text-primary)", marginRight: 4 }}>HxLogs</span>

          <select value={service} onChange={e => setService(e.target.value)}
            style={{ padding: "4px 8px", borderRadius: 4, border: "1px solid var(--border)", background: "var(--bg-surface)", color: "var(--text-primary)", fontSize: 12 }}>
            <option value="">All services</option>
            {services.filter(s => s.available).map(s => (
              <option key={s.service} value={s.service}>{s.service}</option>
            ))}
          </select>

          <select value={severity} onChange={e => setSeverity(e.target.value)}
            style={{ padding: "4px 8px", borderRadius: 4, border: "1px solid var(--border)", background: "var(--bg-surface)", color: "var(--text-primary)", fontSize: 12 }}>
            <option value="">All levels</option>
            {["ERROR","WARNING","INFO","DEBUG"].map(l => <option key={l} value={l}>{l}</option>)}
          </select>

          <select value={since} onChange={e => setSince(Number(e.target.value))}
            style={{ padding: "4px 8px", borderRadius: 4, border: "1px solid var(--border)", background: "var(--bg-surface)", color: "var(--text-primary)", fontSize: 12 }}>
            {[15,30,60,120,360,1440].map(m => <option key={m} value={m}>Last {m < 60 ? `${m}m` : `${m/60}h`}</option>)}
          </select>

          <button onClick={load} disabled={loading}
            style={{ padding: "4px 12px", borderRadius: 4, border: "1px solid var(--border)", background: "transparent", color: "var(--accent)", cursor: "pointer", fontSize: 12 }}>
            {loading ? "Loading…" : "↻ Refresh"}
          </button>

          <div style={{ marginLeft: "auto", display: "flex", gap: 12, alignItems: "center" }}>
            {errorCount > 0 && <span style={{ fontSize: 11, color: "#ef4444" }}>✕ {errorCount} errors</span>}
            {warnCount  > 0 && <span style={{ fontSize: 11, color: "#f59e0b" }}>⚠ {warnCount} warnings</span>}
            {lastRefresh && <span style={{ fontSize: 10, color: "var(--text-muted)" }}>updated {lastRefresh}</span>}
            <button onClick={analyse} disabled={analysing || (entries.length === 0)}
              style={{ padding: "5px 14px", borderRadius: 4, border: "none", background: "var(--accent)", color: "#fff", cursor: "pointer", fontSize: 12, fontWeight: 700 }}>
              {analysing ? "Analysing…" : selected.size > 0 ? `Analyse ${selected.size} selected →` : "Analyse errors →"}
            </button>
          </div>
        </div>

        {/* Column headers — grid matches LOG_COLS exactly */}
        <div style={{
          display: "grid", gridTemplateColumns: LOG_COLS,
          padding: "5px 16px", columnGap: "12px",
          borderBottom: "2px solid var(--border)", flexShrink: 0,
          background: "var(--bg-surface)",
        }}>
          {["Timestamp","Level","Service","Message"].map(h => (
            <span key={h} style={{ fontSize: 9, fontWeight: 700, textTransform: "uppercase" as const, letterSpacing: "0.07em", color: "var(--text-muted)" }}>
              {h}
            </span>
          ))}
        </div>

        {/* Hint */}
        {entries.length > 0 && selected.size === 0 && (
          <div style={{ padding: "3px 16px", fontSize: 10, color: "var(--text-muted)", borderBottom: "1px solid var(--border)", flexShrink: 0 }}>
            Click rows to select · "Analyse errors →" uses HxNexus · Click a traceback row to expand frames
          </div>
        )}

        {/* Log list */}
        <div style={{ flex: 1, overflow: "auto", fontFamily: "monospace" }}>
          {entries.length === 0 && !loading && (
            <div style={{ padding: 40, color: "var(--text-muted)" }}>
              No log entries found. Adjust filters or refresh.
            </div>
          )}
          {entries.map((entry, idx) => {
            const color    = LEVEL_COLOR[entry.level] ?? "var(--text-muted)";
            const isSel    = selected.has(idx);
            const isExp    = expanded.has(idx);
            const isErr    = entry.level === "ERROR" || entry.is_traceback;

            return (
              <div key={idx}
                onClick={() => toggleSelect(idx)}
                style={{
                  borderBottom: "1px solid var(--border)",
                  background: isSel ? `${color}18` : isErr ? `${color}08` : "transparent",
                  cursor: "pointer",
                  borderLeft: `3px solid ${isSel ? color : "transparent"}`,
                }}>

                {/* Main grid row */}
                <div style={{
                  display: "grid",
                  gridTemplateColumns: LOG_COLS,
                  columnGap: "12px",
                  padding: "4px 16px",
                  alignItems: "start",
                }}>
                  {/* Timestamp + line number stacked */}
                  <div style={{ overflow: "hidden" }}>
                    <div style={{ fontSize: 10, fontFamily: "monospace", color: isErr ? color : "var(--text-secondary)", fontWeight: isErr ? 600 : 400, whiteSpace: "nowrap" }}>
                      {entry.occurred_at ?? "—"}
                    </div>
                    <div style={{ fontSize: 9, fontFamily: "monospace", color: "var(--text-muted)", marginTop: 1 }}>
                      ln {entry.line_no.toLocaleString()}
                    </div>
                  </div>

                  {/* Level */}
                  <span style={{ fontSize: 10, fontWeight: 700, color }}>
                    {entry.is_traceback ? "TRACE" : entry.level}
                  </span>

                  {/* Service */}
                  <span style={{ fontSize: 10, color: "var(--text-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {entry.service}
                  </span>

                  {/* Message + expand button */}
                  <div style={{ display: "flex", alignItems: "center", gap: "8px", overflow: "hidden", minWidth: 0 }}>
                    <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 11, color: "var(--text-primary)" }}>
                      {entry.message || entry.raw.split("\n")[0]}
                    </span>
                    {entry.is_traceback && (
                      <button onClick={e => { e.stopPropagation(); toggleExpand(idx); }}
                        style={{ background: "none", border: "none", cursor: "pointer", color: "#ef4444", fontSize: 10, flexShrink: 0, whiteSpace: "nowrap" }}>
                        {isExp ? "▲ collapse" : `▼ ${entry.frames.length} frames`}
                      </button>
                    )}
                  </div>
                </div>

                {/* Innermost frame teaser */}
                {!isExp && entry.is_traceback && entry.innermost && (
                  <div style={{ padding: "0 16px 4px", paddingLeft: "calc(16px + 148px + 54px + 90px + 36px)", fontSize: 10, color: "#ef4444", fontStyle: "italic" }}>
                    ↳ {entry.innermost}
                  </div>
                )}

                {/* Expanded traceback frames */}
                {isExp && (
                  <div style={{ padding: "2px 16px 6px", paddingLeft: "calc(16px + 148px + 54px + 90px + 36px)" }}>
                    {entry.frames.map((frame, fi) => (
                      <div key={fi} style={{ fontSize: 11, color: fi === entry.frames.length - 1 ? "#ef4444" : "var(--text-secondary)", fontWeight: fi === entry.frames.length - 1 ? 700 : 400, marginTop: 2 }}>
                        {fi === entry.frames.length - 1 ? "▶ " : "  "}{frame}
                      </div>
                    ))}
                  </div>
                )}

                {/* Full raw when selected */}
                {isSel && !entry.is_traceback && entry.raw.split("\n").length > 1 && (
                  <pre style={{ margin: "0 16px 6px", fontSize: 11, color: "var(--text-secondary)", whiteSpace: "pre-wrap", wordBreak: "break-all", background: "var(--bg-elevated)", padding: "6px 10px", borderRadius: 4 }}>
                    {entry.raw.split("\n").slice(1).join("\n")}
                  </pre>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* ── Right: AI Analysis panel ── */}
      <div style={{ width: 360, flexShrink: 0, borderLeft: "1px solid var(--border)", display: "flex", flexDirection: "column", overflow: "hidden" }}>
        <div style={{ padding: "10px 16px", borderBottom: "1px solid var(--border)", fontWeight: 700, fontSize: 13 }}>
          AI Analysis
        </div>

        <div style={{ flex: 1, overflow: "auto", padding: 16 }}>
          {!analysis && !analysing && !analyseErr && (
            <div style={{ color: "var(--text-muted)", fontSize: 12, paddingTop: 48 }}>
              <div style={{ fontSize: 32, marginBottom: 12 }}>🔍</div>
              Select error lines and click<br />"Analyse →" for root-cause analysis.<br /><br />
              Or click "Analyse errors →" to send<br />all recent errors to HxNexus.
            </div>
          )}

          {analysing && (
            <div style={{ paddingTop: 48, color: "var(--text-muted)" }}>
              <div style={{ fontSize: 24, marginBottom: 12 }}>🧠</div>
              Analysing with HxNexus…
            </div>
          )}

          {analyseErr && (
            <div style={{ color: "#ef4444", fontSize: 12, padding: "8px 12px", background: "#fee2e2", borderRadius: 6 }}>
              {analyseErr}
            </div>
          )}

          {analysis && (
            <div>
              {/* Severity badge */}
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 16 }}>
                <span style={{
                  fontSize: 11, padding: "3px 10px", borderRadius: 10, fontWeight: 700,
                  background: (SEV_COLOR[analysis.severity] ?? "#888") + "22",
                  color: SEV_COLOR[analysis.severity] ?? "#888",
                  border: `1px solid ${(SEV_COLOR[analysis.severity] ?? "#888")}44`,
                  textTransform: "uppercase",
                }}>
                  {analysis.severity}
                </span>
                {!analysis.ai_available && (
                  <span style={{ fontSize: 10, color: "var(--text-muted)" }}>AI offline — heuristic</span>
                )}
              </div>

              <Section label="Summary" value={analysis.summary} />
              <Section label="Root Cause" value={analysis.root_cause} color="#ef4444" />
              {analysis.likely_file && (
                <Section label="Location" value={analysis.likely_file} mono />
              )}
              <Section label="Suggested Fix" value={analysis.suggested_fix} color="#22c55e" />

              {analysis.related_components.length > 0 && (
                <div style={{ marginBottom: 14 }}>
                  <div style={{ fontSize: 10, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", marginBottom: 6 }}>Related Modules</div>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                    {analysis.related_components.map((c, i) => (
                      <span key={i} style={{ fontSize: 10, padding: "2px 8px", borderRadius: 10, background: "var(--accent-dim)", color: "var(--accent)" }}>{c}</span>
                    ))}
                  </div>
                </div>
              )}

              <button onClick={() => setAnalysis(null)}
                style={{ marginTop: 8, background: "none", border: "none", cursor: "pointer", fontSize: 11, color: "var(--text-muted)", padding: 0 }}>
                ✕ Clear analysis
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function Section({ label, value, color, mono }: { label: string; value: string; color?: string; mono?: boolean }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontSize: 10, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 12, color: color ?? "var(--text-primary)", lineHeight: 1.5, fontFamily: mono ? "monospace" : "inherit" }}>
        {value}
      </div>
    </div>
  );
}
