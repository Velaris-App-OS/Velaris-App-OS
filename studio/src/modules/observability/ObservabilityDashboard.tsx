// HELIX P23 — Observability Dashboard
import React, { useEffect, useMemo, useState } from "react";

type MetricsSummary = {
  total_requests: number;
  error_requests: number;
  error_rate: number;
  latency: Record<string, { avg_ms: number; p50_ms: number; p95_ms: number; count: number }>;
  slowest_endpoints: Array<{ path: string; avg_ms: number; p50_ms: number; p95_ms: number; count: number }>;
};

type Span = {
  method: string; path: string; status: number; duration_ms: number;
  request_id: string; tenant_id: string; timestamp: number;
};

const REFRESH_MS = 5000;
const TRACE_PAGE_SIZE = 20;
const ENDPOINT_PAGE_SIZE = 10;

function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url, { headers: _authHdr() });
  if (!r.ok) throw new Error(`${url} → ${r.status}`);
  return r.json();
}

export default function ObservabilityDashboard() {
  const [summary, setSummary] = useState<MetricsSummary | null>(null);
  const [spans, setSpans] = useState<Span[]>([]);
  const [history, setHistory] = useState<number[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [tracePage, setTracePage] = useState(1);
  const [endpointPage, setEndpointPage] = useState(1);

  async function refresh() {
    try {
      const [s, t] = await Promise.all([
        getJSON<MetricsSummary>("/api/v1/observability/metrics"),
        getJSON<{ spans: Span[] }>("/api/v1/observability/traces/recent?limit=50"),
      ]);
      setSummary(s);
      setSpans(t.spans);
      setTracePage(1);
      setEndpointPage(1);
      const allLat = Object.values(s.latency).map((l) => l.p95_ms);
      const p95 = allLat.length ? Math.max(...allLat) : 0;
      setHistory((h) => [...h.slice(-59), p95]);
      setError(null);
    } catch (e: any) {
      setError(e.message || String(e));
    }
  }

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(id);
  }, []);

  return (
    <div style={{ padding: 24, fontFamily: "system-ui, sans-serif", width: "100%", height: "100%", overflow: "auto", boxSizing: "border-box" }}>
      <header style={{ display: "flex", alignItems: "baseline", gap: 16, marginBottom: 20 }}>
        <span style={{ fontSize: 12, color: "#888" }}>auto-refresh every {REFRESH_MS / 1000}s</span>
        {error && <span style={{ color: "#c33", fontSize: 13 }}>⚠ {error}</span>}
      </header>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 16, marginBottom: 24 }}>
        <Kpi label="Total requests" value={summary?.total_requests ?? 0} />
        <Kpi label="Errors (4xx/5xx)" value={summary?.error_requests ?? 0} warn={(summary?.error_requests ?? 0) > 0} />
        <Kpi label="Error rate" value={`${((summary?.error_rate ?? 0) * 100).toFixed(2)}%`} warn={(summary?.error_rate ?? 0) > 0.01} />
        <Kpi label="Tracked endpoints" value={Object.keys(summary?.latency ?? {}).length} />
      </div>

      <section style={cardStyle}>
        <h2 style={h2Style}>p95 latency (rolling)</h2>
        <Sparkline data={history} />
      </section>

      <section style={cardStyle}>
        {(() => {
          const endpoints = summary?.slowest_endpoints ?? [];
          const totalEndpointPages = Math.ceil(endpoints.length / ENDPOINT_PAGE_SIZE);
          const pagedEndpoints = endpoints.slice((endpointPage - 1) * ENDPOINT_PAGE_SIZE, endpointPage * ENDPOINT_PAGE_SIZE);
          return (
            <>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
                <h2 style={{ ...h2Style, margin: 0 }}>Slowest endpoints</h2>
                {endpoints.length > ENDPOINT_PAGE_SIZE && (
                  <PaginationBar page={endpointPage} totalPages={totalEndpointPages} total={endpoints.length} pageSize={ENDPOINT_PAGE_SIZE} onChange={setEndpointPage} />
                )}
              </div>
              <Table
                rows={pagedEndpoints}
                columns={[
                  { key: "path", label: "Path" },
                  { key: "count", label: "Requests", align: "right" },
                  { key: "avg_ms", label: "Avg (ms)", align: "right", format: (v) => v.toFixed(2) },
                  { key: "p50_ms", label: "p50 (ms)", align: "right", format: (v) => v.toFixed(2) },
                  { key: "p95_ms", label: "p95 (ms)", align: "right", format: (v) => v.toFixed(2) },
                ]}
                empty="No traffic recorded yet — hit a few endpoints to populate."
              />
              {endpoints.length > ENDPOINT_PAGE_SIZE && (
                <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 12 }}>
                  <PaginationBar page={endpointPage} totalPages={totalEndpointPages} total={endpoints.length} pageSize={ENDPOINT_PAGE_SIZE} onChange={setEndpointPage} />
                </div>
              )}
            </>
          );
        })()}
      </section>

      <section style={cardStyle}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
          <h2 style={{ ...h2Style, margin: 0 }}>Recent traces</h2>
          {spans.length > TRACE_PAGE_SIZE && (
            <PaginationBar
              page={tracePage}
              totalPages={Math.ceil(spans.length / TRACE_PAGE_SIZE)}
              total={spans.length}
              pageSize={TRACE_PAGE_SIZE}
              onChange={setTracePage}
            />
          )}
        </div>
        <Table
          rows={spans.slice((tracePage - 1) * TRACE_PAGE_SIZE, tracePage * TRACE_PAGE_SIZE)}
          columns={[
            { key: "method", label: "Method" },
            { key: "path", label: "Path" },
            {
              key: "status", label: "Status",
              format: (v) => String(v),
              cellStyle: (row: Span) => ({
                color: row.status >= 500 ? "#c33" : row.status >= 400 ? "#d80" : "#2a7",
                fontWeight: 600,
              }),
            },
            { key: "duration_ms", label: "Duration (ms)", align: "right", format: (v) => v.toFixed(2) },
            { key: "tenant_id", label: "Tenant" },
            { key: "request_id", label: "Request ID", format: (v: string) => v.slice(0, 8) + "…" },
          ]}
          empty="No traces yet."
        />
        {spans.length > TRACE_PAGE_SIZE && (
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 12 }}>
            <PaginationBar
              page={tracePage}
              totalPages={Math.ceil(spans.length / TRACE_PAGE_SIZE)}
              total={spans.length}
              pageSize={TRACE_PAGE_SIZE}
              onChange={setTracePage}
            />
          </div>
        )}
      </section>
    </div>
  );
}

const cardStyle: React.CSSProperties = {
  background: "#fff", border: "1px solid #e3e3e8", borderRadius: 8, padding: 20, marginBottom: 20,
};
const h2Style: React.CSSProperties = { margin: "0 0 12px", fontSize: 15, color: "#333" };

function Kpi({ label, value, warn = false }: { label: string; value: React.ReactNode; warn?: boolean }) {
  return (
    <div style={{
      background: "#fff", border: `1px solid ${warn ? "#f0b0b0" : "#e3e3e8"}`,
      borderRadius: 8, padding: "14px 18px",
    }}>
      <div style={{ fontSize: 11, color: "#888", textTransform: "uppercase", letterSpacing: 0.5 }}>{label}</div>
      <div style={{ fontSize: 24, fontWeight: 600, color: warn ? "#c33" : "#222", marginTop: 4 }}>{value}</div>
    </div>
  );
}

function Sparkline({ data }: { data: number[] }) {
  const w = 720, h = 80, pad = 4;
  const max = useMemo(() => Math.max(1, ...data), [data]);
  const points = useMemo(() => {
    if (data.length === 0) return "";
    const xs = (i: number) => pad + (i / Math.max(1, data.length - 1)) * (w - 2 * pad);
    const ys = (v: number) => h - pad - (v / max) * (h - 2 * pad);
    return data.map((v, i) => `${i === 0 ? "M" : "L"} ${xs(i).toFixed(1)} ${ys(v).toFixed(1)}`).join(" ");
  }, [data, max]);
  if (data.length === 0) return <div style={{ color: "#888", fontSize: 13 }}>Collecting data…</div>;
  return (
    <svg width="100%" viewBox={`0 0 ${w} ${h}`} style={{ display: "block" }}>
      <path d={points} stroke="#4a6cf7" strokeWidth={2} fill="none" />
      <text x={w - 4} y={14} textAnchor="end" fontSize={11} fill="#888">peak {max.toFixed(1)} ms</text>
    </svg>
  );
}

function PaginationBar({ page, totalPages, total, pageSize, onChange }: {
  page: number; totalPages: number; total: number; pageSize: number; onChange: (p: number) => void;
}) {
  const pages: (number | "…")[] = [];
  for (let i = 1; i <= totalPages; i++) {
    if (i === 1 || i === totalPages || Math.abs(i - page) <= 1) pages.push(i);
    else if (pages[pages.length - 1] !== "…") pages.push("…");
  }
  const btnBase: React.CSSProperties = {
    width: 28, height: 28, borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)",
    background: "transparent", color: "var(--text-secondary)", fontSize: 12,
    cursor: "pointer", fontFamily: "var(--font-mono)",
    display: "flex", alignItems: "center", justifyContent: "center",
  };
  return (
    <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
      <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginRight: 4, whiteSpace: "nowrap" }}>
        {(page - 1) * pageSize + 1}–{Math.min(page * pageSize, total)} / {total}
      </span>
      <button onClick={() => onChange(page - 1)} disabled={page === 1}
        style={{ ...btnBase, opacity: page === 1 ? 0.35 : 1 }}>‹</button>
      {pages.map((p, i) =>
        p === "…" ? (
          <span key={`e${i}`} style={{ fontSize: 11, color: "var(--text-muted)", padding: "0 2px" }}>…</span>
        ) : (
          <button key={p} onClick={() => onChange(p as number)}
            style={{ ...btnBase, background: page === p ? "var(--accent)" : "transparent", color: page === p ? "#fff" : "var(--text-secondary)", borderColor: page === p ? "var(--accent)" : "var(--border-default)" }}>
            {p}
          </button>
        )
      )}
      <button onClick={() => onChange(page + 1)} disabled={page >= totalPages}
        style={{ ...btnBase, opacity: page >= totalPages ? 0.35 : 1 }}>›</button>
    </div>
  );
}

type Col<T> = {
  key: keyof T & string; label: string;
  align?: "left" | "right"; format?: (v: any) => string;
  cellStyle?: (row: T) => React.CSSProperties;
};

function Table<T extends Record<string, any>>({ rows, columns, empty }: { rows: T[]; columns: Col<T>[]; empty: string }) {
  if (rows.length === 0) return <div style={{ color: "#888", fontSize: 13 }}>{empty}</div>;
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
      <thead>
        <tr>
          {columns.map((c) => (
            <th key={c.key} style={{
              textAlign: c.align ?? "left", padding: "6px 8px",
              borderBottom: "1px solid #eee", color: "#666", fontWeight: 500,
            }}>{c.label}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r, idx) => (
          <tr key={idx}>
            {columns.map((c) => {
              const raw = r[c.key];
              const val = c.format ? c.format(raw) : raw;
              return (
                <td key={c.key} style={{
                  textAlign: c.align ?? "left", padding: "6px 8px",
                  borderBottom: "1px solid #f5f5f5",
                  fontFamily: c.key === "path" || c.key === "request_id" ? "ui-monospace, monospace" : undefined,
                  ...(c.cellStyle ? c.cellStyle(r) : {}),
                }}>{val}</td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
