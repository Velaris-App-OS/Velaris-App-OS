/**
 * P26 — HxAnalytics: Semantic Business Intelligence
 * Tabs: Dashboard · Query · Reports
 */
import React, { useState, useEffect, useCallback } from "react";
import { AiUnavailableBanner } from "@shared/components/AiUnavailableBanner";

const API = "/api/v1/analytics";

function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}

async function authFetch(url: string, opts: RequestInit = {}): Promise<Response> {
  return fetch(url, { ...opts, headers: { ..._authHdr(), ...opts.headers } });
}

type Snap = { total_cases: number; open_cases: number; new_last_7_days: number; new_last_30_days: number; avg_resolution_hours: number | null; sla_breach_pct: number; by_status: Record<string, number>; by_priority: Record<string, number>; by_type: { name: string; count: number }[] };
type Series = { label: string; value: number }[];
type TimePoint = { date: string; count: number }[];
type Report = { id: string; name: string; description: string | null; query_type: string; query_def: any; chart_type: string; created_at: string | null };
type QueryResult = { title: string; chart_type: string; series: Series; data?: any; question?: string; interpreted_as?: any };

const CHART_COLORS = ["#0d9488", "#22c55e", "#f59e0b", "#ef4444", "#3b82f6", "#ec4899", "#0f766e", "#14b8a6"];

const S: Record<string, React.CSSProperties> = {
  page:       { display: "flex", flexDirection: "column", height: "100%", background: "var(--bg-main)", color: "var(--text-primary)", fontFamily: "system-ui, sans-serif" },
  header:     { padding: "18px 24px 0", flexShrink: 0 },
  title:      { fontSize: 20, fontWeight: 700, margin: 0 },
  sub:        { fontSize: 13, color: "var(--text-secondary)", marginTop: 4 },
  tabBar:     { display: "flex", gap: 4, padding: "14px 24px 0", borderBottom: "1px solid var(--border)", flexShrink: 0 },
  tab:        { padding: "8px 20px", border: "none", background: "none", fontSize: 13, cursor: "pointer", borderBottom: "2px solid transparent", fontWeight: 400, color: "var(--text-secondary)" },
  tabActive:  { borderBottom: "2px solid var(--accent)", color: "var(--accent)", fontWeight: 700 },
  body:       { flex: 1, overflow: "auto", padding: "20px 28px" },
  card:       { background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: 8, padding: "16px 20px", marginBottom: 12 },
  metricCard: { background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: 10, padding: "16px 20px" as const },
  metricVal:  { fontSize: 32, fontWeight: 700, color: "var(--accent)", lineHeight: 1 },
  metricLbl:  { fontSize: 11, color: "var(--text-secondary)", marginTop: 4, textTransform: "uppercase" as const, letterSpacing: "0.05em" },
  btn:        { padding: "8px 18px", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12, fontWeight: 700 },
  btnP:       { background: "var(--accent)", color: "#fff" },
  btnS:       { background: "var(--bg-surface)", border: "1px solid var(--border)", color: "var(--text-secondary)" },
  input:      { width: "100%", padding: "8px 12px", border: "1px solid var(--border)", borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: "var(--bg-main)", color: "var(--text-primary)" },
  label:      { fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", marginBottom: 4, display: "block" },
  grid:       { display: "grid", gap: 12 },
};

// ── Mini bar chart ────────────────────────────────────────────────────────────

function BarChart({ series, height = 120 }: { series: Series; height?: number }) {
  if (!series.length) return <div style={{ color: "var(--text-secondary)", fontSize: 12 }}>No data</div>;
  const max = Math.max(...series.map(s => s.value), 1);
  return (
    <div style={{ display: "flex", alignItems: "flex-end", gap: 6, height, overflow: "hidden" }}>
      <AiUnavailableBanner featureName="Natural language queries" />

      {series.slice(0, 16).map((s, i) => (
        <div key={i} style={{ display: "flex", flexDirection: "column", alignItems: "center", flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 9, color: "var(--text-secondary)", marginBottom: 2, whiteSpace: "nowrap", overflow: "hidden", maxWidth: "100%", textOverflow: "ellipsis" }}>
            {s.value}
          </div>
          <div style={{
            width: "100%", background: CHART_COLORS[i % CHART_COLORS.length],
            height: `${Math.round((s.value / max) * (height - 30))}px`,
            borderRadius: "3px 3px 0 0", minHeight: 3,
          }} title={`${s.label}: ${s.value}`} />
          <div style={{ fontSize: 8, color: "var(--text-secondary)", marginTop: 3, whiteSpace: "nowrap", overflow: "hidden", maxWidth: "100%", textOverflow: "ellipsis" }}>
            {s.label}
          </div>
        </div>
      ))}
    </div>
  );
}

function PieChart({ series }: { series: Series }) {
  if (!series.length) return <div style={{ color: "var(--text-secondary)", fontSize: 12 }}>No data</div>;
  const total = series.reduce((a, s) => a + s.value, 0) || 1;
  return (
    <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
      {series.map((s, i) => (
        <div key={i} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}>
          <div style={{ width: 12, height: 12, borderRadius: 3, background: CHART_COLORS[i % CHART_COLORS.length], flexShrink: 0 }} />
          <span style={{ color: "var(--text-secondary)" }}>{s.label}</span>
          <span style={{ fontWeight: 700 }}>{s.value}</span>
          <span style={{ color: "var(--text-secondary)", fontSize: 10 }}>({Math.round(100 * s.value / total)}%)</span>
        </div>
      ))}
    </div>
  );
}

function RingChart({ series, size = 160 }: { series: Series; size?: number }) {
  if (!series.length) return <div style={{ color: "var(--text-secondary)", fontSize: 12 }}>No data</div>;
  const total = series.reduce((a, s) => a + s.value, 0) || 1;
  const cx = size / 2, cy = size / 2;
  const r = size * 0.36, strokeW = size * 0.18;

  // Build arc segments
  let cumAngle = -Math.PI / 2;
  const segments = series.map((s, i) => {
    const frac = s.value / total;
    const sweep = frac * 2 * Math.PI;
    const x1 = cx + r * Math.cos(cumAngle);
    const y1 = cy + r * Math.sin(cumAngle);
    cumAngle += sweep;
    const x2 = cx + r * Math.cos(cumAngle);
    const y2 = cy + r * Math.sin(cumAngle);
    const large = sweep > Math.PI ? 1 : 0;
    return { x1, y1, x2, y2, large, color: CHART_COLORS[i % CHART_COLORS.length], label: s.label, value: s.value, pct: Math.round(100 * frac) };
  });

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 24, flexWrap: "wrap" }}>
      <svg width={size} height={size} style={{ flexShrink: 0 }}>
        {segments.map((seg, i) => (
          <path
            key={i}
            d={`M ${seg.x1} ${seg.y1} A ${r} ${r} 0 ${seg.large} 1 ${seg.x2} ${seg.y2}`}
            fill="none"
            stroke={seg.color}
            strokeWidth={strokeW}
            strokeLinecap="butt"
          >
            <title>{seg.label}: {seg.value} ({seg.pct}%)</title>
          </path>
        ))}
        <text x={cx} y={cy - 6} textAnchor="middle" fontSize={size * 0.14} fontWeight={700} fill="var(--text-primary)">{total}</text>
        <text x={cx} y={cy + size * 0.1} textAnchor="middle" fontSize={size * 0.08} fill="var(--text-secondary)">total</text>
      </svg>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {segments.map((seg, i) => (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
            <div style={{ width: 10, height: 10, borderRadius: "50%", background: seg.color, flexShrink: 0 }} />
            <span style={{ color: "var(--text-secondary)", minWidth: 60 }}>{seg.label}</span>
            <span style={{ fontWeight: 700 }}>{seg.value}</span>
            <span style={{ color: "var(--text-secondary)", fontSize: 10 }}>({seg.pct}%)</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function LineChart({ series, height = 100 }: { series: Series; height?: number }) {
  if (!series.length) return <div style={{ color: "var(--text-secondary)", fontSize: 12 }}>No data</div>;
  const vals = series.map(s => s.value);
  const max = Math.max(...vals, 1), min = Math.min(...vals, 0);
  const range = max - min || 1;
  const w = 400, h = height;
  const pts = series.map((s, i) => ({
    x: (i / Math.max(series.length - 1, 1)) * w,
    y: h - ((s.value - min) / range) * (h - 20) - 10,
  }));
  const path = pts.map((p, i) => `${i === 0 ? "M" : "L"}${p.x},${p.y}`).join(" ");
  return (
    <div style={{ overflowX: "auto" }}>
      <svg width={w} height={h + 20} style={{ display: "block" }}>
        <path d={path} fill="none" stroke="#0d9488" strokeWidth={2} />
        {pts.map((p, i) => (
          <circle key={i} cx={p.x} cy={p.y} r={3} fill="#0d9488">
            <title>{series[i].label}: {series[i].value}</title>
          </circle>
        ))}
        {[0, Math.floor(series.length / 2), series.length - 1].filter(i => i >= 0 && i < series.length).map(i => (
          <text key={i} x={pts[i].x} y={h + 16} textAnchor="middle" fontSize={9} fill="var(--text-secondary)">{series[i].label}</text>
        ))}
      </svg>
    </div>
  );
}

function ChartView({ result }: { result: QueryResult }) {
  const { chart_type, series, title, data } = result;
  if (chart_type === "number") {
    const val = data?.value ?? data?.total_cases ?? series?.[0]?.value;
    return (
      <div style={{ padding: "20px 0" }}>
        <div style={{ fontSize: 48, fontWeight: 800, color: "var(--accent)" }}>{val ?? "—"}</div>
        {data?.unit && <div style={{ fontSize: 13, color: "var(--text-secondary)" }}>{data.unit}</div>}
      </div>
    );
  }
  if (chart_type === "pie" || chart_type === "ring") return <RingChart series={series} size={160} />;
  if (chart_type === "line") return <LineChart series={series} />;
  return <BarChart series={series} />;
}


// ── Dashboard tab ─────────────────────────────────────────────────────────────

function DashboardTab() {
  const [snap, setSnap] = useState<Snap | null>(null);
  const [timeSeries, setTimeSeries] = useState<TimePoint>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [sRes, tRes] = await Promise.all([
        authFetch(`${API}/metrics/snapshot`),
        authFetch(`${API}/metrics/time-series?days=30`),
      ]);
      if (sRes.ok) setSnap(await sRes.json());
      if (tRes.ok) setTimeSeries((await tRes.json()).series);
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (loading) return <div style={S.body}><div style={{ color: "var(--text-secondary)", fontSize: 13 }}>Loading…</div></div>;

  return (
    <div style={S.body}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <h2 style={{ margin: 0, fontSize: 15, fontWeight: 700 }}>Platform Overview</h2>
        <button style={{ ...S.btn, ...S.btnS, fontSize: 11 }} onClick={load}>↻ Refresh</button>
      </div>

      {/* Metric cards */}
      {snap && (
        <div style={{ ...S.grid, gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))", marginBottom: 20 }}>
          {[
            { label: "Total Cases",       value: snap.total_cases },
            { label: "Open Cases",        value: snap.open_cases },
            { label: "New (7 days)",      value: snap.new_last_7_days },
            { label: "New (30 days)",     value: snap.new_last_30_days },
            { label: "SLA Breach %",      value: `${snap.sla_breach_pct}%` },
            { label: "Avg Resolution",    value: snap.avg_resolution_hours ? `${snap.avg_resolution_hours}h` : "—" },
          ].map(({ label, value }) => (
            <div key={label} style={S.metricCard}>
              <div style={S.metricVal}>{value}</div>
              <div style={S.metricLbl}>{label}</div>
            </div>
          ))}
        </div>
      )}

      {/* Cases over time */}
      <div style={S.card}>
        <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 12 }}>Cases Created — Last 30 Days</div>
        <LineChart series={timeSeries.map(d => ({ label: d.date, value: d.count }))} height={100} />
      </div>

      {/* By type */}
      {snap && snap.by_type.length > 0 && (
        <div style={S.card}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 12 }}>Cases by Type</div>
          <BarChart series={snap.by_type.map(t => ({ label: t.name, value: t.count }))} height={140} />
        </div>
      )}

      {/* By status */}
      {snap && Object.keys(snap.by_status).length > 0 && (
        <div style={S.card}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 12 }}>Cases by Status</div>
          <PieChart series={Object.entries(snap.by_status).map(([label, value]) => ({ label, value }))} />
        </div>
      )}

      {/* By priority — ring chart */}
      {snap && Object.keys(snap.by_priority).length > 0 && (
        <div style={S.card}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 16 }}>Cases by Priority</div>
          <RingChart series={Object.entries(snap.by_priority).map(([label, value]) => ({ label, value }))} size={160} />
        </div>
      )}
    </div>
  );
}


// ── Query tab ─────────────────────────────────────────────────────────────────

function QueryTab() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<QueryResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);

  const EXAMPLES = [
    "Show me cases by status this month",
    "What is the SLA breach rate over the last 30 days?",
    "How many new cases were created last 7 days?",
    "Average case resolution time",
    "Cases by priority",
    "Case volume over time",
  ];

  async function handleQuery(e: React.FormEvent) {
    e.preventDefault(); setLoading(true); setResult(null); setSaveMsg(null);
    try {
      const r = await authFetch(`${API}/query`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      if (r.ok) setResult(await r.json());
    } finally { setLoading(false); }
  }

  async function handleSave() {
    if (!result) return; setSaving(true);
    const r = await authFetch(`${API}/reports`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: result.title || question,
        query_type: "nl",
        query_def: { question },
        chart_type: result.chart_type,
      }),
    });
    setSaving(false);
    setSaveMsg(r.ok ? "Report saved!" : "Save failed");
  }

  return (
    <div style={S.body}>
      <h2 style={{ margin: "0 0 16px", fontSize: 15, fontWeight: 700 }}>Ask HxNexus</h2>
      <form onSubmit={handleQuery} style={{ display: "flex", gap: 10, marginBottom: 16 }}>
        <input style={{ ...S.input, flex: 1 }} value={question} onChange={e => setQuestion(e.target.value)}
               placeholder="e.g. Show me SLA breach rate for the last 30 days" required />
        <button type="submit" disabled={loading} style={{ ...S.btn, ...S.btnP, flexShrink: 0 }}>
          {loading ? "Thinking…" : "▶ Ask"}
        </button>
      </form>

      {/* Example queries */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 20 }}>
        {EXAMPLES.map(ex => (
          <button key={ex} onClick={() => setQuestion(ex)} style={{
            fontSize: 11, padding: "4px 10px", border: "1px solid var(--border)",
            borderRadius: 20, cursor: "pointer", background: "var(--bg-surface)",
            color: "var(--text-secondary)",
          }}>{ex}</button>
        ))}
      </div>

      {result && (
        <div style={S.card}>
          <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 14 }}>
            <span style={{ fontSize: 14, fontWeight: 700, flex: 1 }}>{result.title}</span>
            <span style={{ fontSize: 10, color: "var(--text-secondary)", background: "var(--bg-main)", padding: "2px 8px", borderRadius: 10 }}>{result.chart_type}</span>
            <button style={{ ...S.btn, ...S.btnS, fontSize: 11 }} onClick={handleSave} disabled={saving}>
              {saving ? "Committing…" : "💾 Commit Report"}
            </button>
          </div>
          {saveMsg && <div style={{ fontSize: 11, color: "#22c55e", marginBottom: 8 }}>{saveMsg}</div>}
          {result.question && (
            <div style={{ fontSize: 11, color: "var(--text-secondary)", marginBottom: 12, fontStyle: "italic" }}>
              Interpreted: "{result.interpreted_as ? JSON.stringify(result.interpreted_as.metric) : result.question}"
            </div>
          )}
          <ChartView result={result} />
          {result.series?.length > 0 && (
            <details style={{ marginTop: 12 }}>
              <summary style={{ fontSize: 11, color: "var(--text-secondary)", cursor: "pointer" }}>Show data table</summary>
              <table style={{ width: "100%", borderCollapse: "collapse", marginTop: 8, fontSize: 12 }}>
                <thead>
                  <tr style={{ borderBottom: "1px solid var(--border)" }}>
                    <th style={{ textAlign: "left", padding: "4px 8px", color: "var(--text-secondary)", fontWeight: 600 }}>Label</th>
                    <th style={{ textAlign: "right", padding: "4px 8px", color: "var(--text-secondary)", fontWeight: 600 }}>Value</th>
                  </tr>
                </thead>
                <tbody>
                  {result.series.map((s, i) => (
                    <tr key={i} style={{ borderBottom: "1px solid var(--border)" }}>
                      <td style={{ padding: "4px 8px" }}>{s.label}</td>
                      <td style={{ padding: "4px 8px", textAlign: "right", fontWeight: 600 }}>{s.value}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
          )}
        </div>
      )}
    </div>
  );
}


// ── Reports tab ───────────────────────────────────────────────────────────────

function ReportsTab() {
  const [reports, setReports] = useState<Report[]>([]);
  const [selected, setSelected] = useState<Report | null>(null);
  const [runResult, setRunResult] = useState<QueryResult | null>(null);
  const [running, setRunning] = useState(false);

  const load = useCallback(async () => {
    const r = await authFetch(`${API}/reports`);
    if (r.ok) setReports((await r.json()).reports);
  }, []);

  useEffect(() => { load(); }, [load]);

  async function handleRun(r: Report) {
    setSelected(r); setRunning(true); setRunResult(null);
    const res = await authFetch(`${API}/reports/${r.id}/run`);
    if (res.ok) setRunResult(await res.json());
    setRunning(false);
  }

  async function handleDelete(r: Report) {
    await authFetch(`${API}/reports/${r.id}`, { method: "DELETE" });
    if (selected?.id === r.id) { setSelected(null); setRunResult(null); }
    await load();
  }

  function exportFromResult(r: Report, fmt: "csv" | "json", result: QueryResult | null) {
    const fname = r.name.replace(/ /g, "_");
    let content: string;
    let mime: string;
    if (fmt === "csv") {
      const series = result?.series ?? [];
      const rows = series.length
        ? ["label,value", ...series.map(s => `${JSON.stringify(s.label)},${s.value}`)].join("\n")
        : "label,value\n";
      content = rows;
      mime = "text/csv";
    } else {
      content = JSON.stringify(result ?? {}, null, 2);
      mime = "application/json";
    }
    const blob = new Blob([content], { type: mime });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${fname}.${fmt}`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  async function handleExport(r: Report, fmt: "csv" | "json") {
    if (runResult && selected?.id === r.id) {
      exportFromResult(r, fmt, runResult);
      return;
    }
    // No result loaded yet — run the report first then export
    const res = await authFetch(`${API}/reports/${r.id}/run`);
    if (!res.ok) return;
    const result: QueryResult = await res.json();
    exportFromResult(r, fmt, result);
  }

  return (
    <div style={{ display: "flex", height: "100%", overflow: "hidden" }}>
      {/* List */}
      <div style={{ width: 260, borderRight: "1px solid var(--border)", display: "flex", flexDirection: "column", background: "var(--bg-surface)", flexShrink: 0 }}>
        <div style={{ padding: "12px 14px", borderBottom: "1px solid var(--border)", fontSize: 12, fontWeight: 700, color: "var(--text-secondary)" }}>SAVED REPORTS</div>
        <div style={{ flex: 1, overflow: "auto" }}>
          {reports.map(r => (
            <div key={r.id} onClick={() => handleRun(r)} style={{
              padding: "10px 14px", cursor: "pointer", borderBottom: "1px solid var(--border)",
              background: selected?.id === r.id ? "var(--accent-light, #ede9fe)" : "transparent",
            }}>
              <div style={{ fontSize: 12, fontWeight: 600 }}>{r.name}</div>
              <div style={{ fontSize: 10, color: "var(--text-secondary)", marginTop: 2 }}>{r.chart_type} · {r.query_type}</div>
            </div>
          ))}
          {reports.length === 0 && <div style={{ padding: 16, fontSize: 12, color: "var(--text-secondary)" }}>No saved reports. Use the Query tab to save one.</div>}
        </div>
      </div>

      {/* Detail */}
      <div style={{ flex: 1, overflow: "auto", padding: "20px 24px" }}>
        {!selected && <div style={{ color: "var(--text-secondary)", fontSize: 13, paddingTop: 40 }}>Select a report to run it.</div>}
        {selected && (
          <>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16 }}>
              <h2 style={{ margin: 0, fontSize: 15, fontWeight: 700, flex: 1 }}>{selected.name}</h2>
              <button style={{ ...S.btn, ...S.btnS, fontSize: 11 }} onClick={() => handleExport(selected, "csv")}>↓ CSV</button>
              <button style={{ ...S.btn, ...S.btnS, fontSize: 11 }} onClick={() => handleExport(selected, "json")}>↓ JSON</button>
              <button style={{ ...S.btn, background: "#fee2e2", color: "#ef4444", border: "1px solid #fecaca", fontSize: 11 }}
                      onClick={() => handleDelete(selected)}>Delete</button>
            </div>
            {running && <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>Running…</div>}
            {runResult && (
              <div style={S.card}>
                <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 12 }}>{runResult.title}</div>
                <ChartView result={runResult} />
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}


// ── Root ──────────────────────────────────────────────────────────────────────

export default function HxAnalytics() {
  const [tab, setTab] = useState<"dashboard" | "query" | "reports">("dashboard");
  const tabs = [
    { key: "dashboard" as const, label: "Dashboard" },
    { key: "query"     as const, label: "Ask HxNexus" },
    { key: "reports"   as const, label: "Committed Reports" },
  ];
  return (
    <div style={S.page}>
      <div style={S.tabBar}>
        {tabs.map(t => (
          <button key={t.key} onClick={() => setTab(t.key)}
                  style={{ ...S.tab, ...(tab === t.key ? S.tabActive : {}) }}>{t.label}</button>
        ))}
      </div>
      <div style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column" }}>
        {tab === "dashboard" && <DashboardTab />}
        {tab === "query"     && <QueryTab />}
        {tab === "reports"   && <ReportsTab />}
      </div>
    </div>
  );
}
