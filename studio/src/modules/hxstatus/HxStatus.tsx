// HxStatus — public status page + SLA reporting (§8.8).
//
// Reads the SAME unauthenticated endpoint an external status page would, so
// what an operator sees here is exactly what a customer sees. That is
// deliberate: a status page that shows staff a richer view than the public one
// is how "it looks fine to us" outages happen.
//
// The privileged detail (raw exception text, latencies, sample history) lives
// behind /status/internal and is shown separately, clearly labelled.
import React, { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@shared/api/client";

interface Component {
  component: string;
  status: string;
  last_checked: string | null;
  uptime: Record<string, number | null>;
}

interface PublicStatus {
  status: string;
  components: Component[];
  windows: string[];
  generated_at: string;
}

const OVERALL: Record<string, { label: string; colour: string }> = {
  operational: { label: "All systems operational", colour: "var(--accent)" },
  degraded: { label: "Degraded performance", colour: "var(--status-warning, #d98c00)" },
  major_outage: { label: "Major outage", colour: "var(--status-failed)" },
  unknown: { label: "No data yet", colour: "var(--text-muted)" },
};

const DOT: Record<string, string> = {
  ok: "var(--accent)",
  degraded: "var(--status-warning, #d98c00)",
  down: "var(--status-failed)",
  unknown: "var(--text-muted)",
};

function uptime(v: number | null | undefined): string {
  // Null means "nothing was sampled" — not 100%. Showing a number here would
  // report perfect uptime from zero measurements.
  return v === null || v === undefined ? "—" : `${v.toFixed(2)}%`;
}

interface Readiness {
  checked_at: string;
  summary: { working: number; degraded: number; off: number };
  items: { key: string; label: string; state: "working" | "degraded" | "off"; reason: string }[];
}

const STATE: Record<string, { label: string; colour: string }> = {
  working: { label: "Working", colour: "var(--accent)" },
  degraded: { label: "Degraded", colour: "var(--status-warning, #d98c00)" },
  off: { label: "Off", colour: "var(--text-muted)" },
};

// R4: every optional dependency — working, degraded or off — with the reason in
// plain words. Off is a choice, never shown as an error. Admin only (the
// endpoint answers 403 otherwise, and this section then says nothing).
function Dependencies() {
  const [data, setData] = useState<Readiness | null>(null);
  const [state, setState] = useState<"loading" | "forbidden" | "error" | "ok">("loading");

  const load = useCallback(async () => {
    try {
      const r = await apiFetch("/status/readiness");
      if (r.status === 401 || r.status === 403) { setState("forbidden"); return; }
      if (!r.ok) throw new Error(String(r.status));
      setData(await r.json());
      setState("ok");
    } catch {
      setState("error");
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, [load]);

  if (state === "forbidden") return null;
  return (
    <div style={{ marginBottom: 28 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginBottom: 6 }}>
        <h3 style={{ margin: 0, fontSize: 14 }}>Dependencies</h3>
        <span style={{ fontSize: 10, color: "var(--text-muted)" }}>admin only — not part of the public view</span>
        {data && (
          <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
            {data.summary.working} working · {data.summary.degraded} degraded · {data.summary.off} off
            {" — checked "}{new Date(data.checked_at).toLocaleTimeString()}
          </span>
        )}
        <button onClick={() => void load()} style={{ fontSize: 11, marginLeft: "auto" }}>Check again</button>
      </div>
      {state === "error" && (
        <div style={{ fontSize: 12, color: "var(--status-failed)" }}>Could not check the dependencies.</div>
      )}
      {data && (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
          <tbody>
            {data.items.map(i => (
              <tr key={i.key} data-testid={`dep-${i.key}`} style={{ borderTop: "1px solid var(--border-subtle)" }}>
                <td style={{ padding: "7px 4px", whiteSpace: "nowrap", verticalAlign: "top" }}>{i.label}</td>
                <td style={{ padding: "7px 4px", whiteSpace: "nowrap", verticalAlign: "top",
                             color: (STATE[i.state] ?? STATE.off).colour }}>
                  <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%",
                                 background: (STATE[i.state] ?? STATE.off).colour, marginRight: 6 }} />
                  {(STATE[i.state] ?? STATE.off).label}
                </td>
                <td style={{ padding: "7px 4px", color: i.state === "degraded" ? "var(--text-primary)"
                             : "var(--text-muted)", lineHeight: 1.5 }}>{i.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export default function HxStatus() {
  const [data, setData] = useState<PublicStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [internal, setInternal] = useState<any>(null);
  const [showInternal, setShowInternal] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await apiFetch("/status/public");
      if (!r.ok) throw new Error(await r.text());
      setData(await r.json());
      setError(null);
    } catch (e: any) {
      setError(e?.message ?? "Could not load status");
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, [load]);

  async function loadInternal() {
    try {
      const r = await apiFetch("/status/internal?days=7");
      if (r.status === 403 || r.status === 401) {
        setInternal({ forbidden: true });
        return;
      }
      if (r.ok) setInternal(await r.json());
    } catch { /* advisory */ }
    setShowInternal(true);
  }

  const overall = OVERALL[data?.status ?? "unknown"] ?? OVERALL.unknown;

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", height: "100%", overflow: "auto" }}>
      <h2 style={{ margin: "0 0 4px", fontSize: 18 }}>Status</h2>
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 16 }}>
        This is the public view — exactly what an external status page shows. Staff
        seeing a healthier picture than customers is how outages get missed.
      </div>

      <Dependencies />

      {error && (
        <div style={{ padding: 10, fontSize: 12, borderRadius: 4, marginBottom: 16,
                      background: "var(--bg-input)", color: "var(--status-failed)" }}>
          {error}
        </div>
      )}

      {data && (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 20 }}>
            <span style={{ width: 12, height: 12, borderRadius: "50%", background: overall.colour }} />
            <span style={{ fontSize: 16, color: overall.colour }}>{overall.label}</span>
          </div>

          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--text-muted)", fontSize: 11 }}>
                <th style={{ padding: "8px 4px" }}>Component</th>
                <th style={{ padding: "8px 4px" }}>Status</th>
                {data.windows.map(w => (
                  <th key={w} style={{ padding: "8px 4px" }}>Uptime {w}</th>
                ))}
                <th style={{ padding: "8px 4px" }}>Last checked</th>
              </tr>
            </thead>
            <tbody>
              {data.components.map(c => (
                <tr key={c.component} style={{ borderTop: "1px solid var(--border-subtle)" }}>
                  <td style={{ padding: "8px 4px" }}>{c.component}</td>
                  <td style={{ padding: "8px 4px" }}>
                    <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%",
                                   background: DOT[c.status] ?? DOT.unknown, marginRight: 6 }} />
                    {c.status}
                  </td>
                  {data.windows.map(w => (
                    <td key={w} style={{ padding: "8px 4px" }}>{uptime(c.uptime[w])}</td>
                  ))}
                  <td style={{ padding: "8px 4px", color: "var(--text-muted)" }}>
                    {c.last_checked ? new Date(c.last_checked).toLocaleString() : "never"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {data.status === "unknown" && (
            <div style={{ marginTop: 16, fontSize: 11, color: "var(--text-muted)", lineHeight: 1.6 }}>
              No health samples have been recorded yet. The sampler writes one per
              component per interval; uptime becomes meaningful once a window's worth
              of history exists. It reports “no data” rather than claiming 100%.
            </div>
          )}

          <div style={{ marginTop: 24 }}>
            <button onClick={loadInternal} style={{ fontSize: 11 }}>
              Show operator detail
            </button>
            <span style={{ marginLeft: 8, fontSize: 10, color: "var(--text-muted)" }}>
              admin only — includes diagnostics never shown publicly
            </span>
          </div>

          {showInternal && internal?.forbidden && (
            <div style={{ marginTop: 10, fontSize: 11, color: "var(--text-muted)" }}>
              Operator detail requires admin.
            </div>
          )}

          {showInternal && internal && !internal.forbidden && (
            <div style={{ marginTop: 12 }}>
              <h4 style={{ fontSize: 12, margin: "0 0 6px" }}>
                Recent samples (last {internal.window_days}d)
              </h4>
              {(internal.samples ?? []).slice(0, 40).map((s: any, i: number) => (
                <div key={i} style={{ fontSize: 11, padding: "3px 0", display: "flex", gap: 10,
                                      borderBottom: "1px solid var(--border-subtle)" }}>
                  <span style={{ minWidth: 90 }}>{s.component}</span>
                  <span style={{ minWidth: 70, color: DOT[s.status] ?? DOT.unknown }}>{s.status}</span>
                  <span style={{ minWidth: 70, color: "var(--text-muted)" }}>
                    {s.latency_ms == null ? "—" : `${s.latency_ms} ms`}
                  </span>
                  <span style={{ flex: 1, color: "var(--text-muted)" }}>
                    {s.detail && Object.keys(s.detail).length ? JSON.stringify(s.detail) : ""}
                  </span>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
