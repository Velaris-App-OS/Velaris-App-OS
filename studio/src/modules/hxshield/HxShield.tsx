/**
 * P59 — HxShield: Case Fraud & Abuse Detection
 * Tabs: Rules · Incidents · Events · Stats
 */
import React, { useState, useEffect, useCallback } from "react";

const API = "/api/v1/shield";
function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}
function authFetch(url: string, opts: RequestInit = {}): Promise<Response> {
  return fetch(url, { ...opts, headers: { ..._authHdr(), ...opts.headers } });
}


type SecurityRule = {
  id: string; name: string; pattern_type: string; description: string | null;
  threshold: number; window_seconds: number; action: string;
  severity: string; enabled: boolean; tenant_id: string | null;
  created_by: string | null; created_at: string; updated_at: string;
};

type Incident = {
  id: string; rule_id: string | null; pattern_type: string;
  severity: string; status: string; actor_id: string | null;
  tenant_id: string | null; case_type_id: string | null;
  context: Record<string, unknown>; explanation: string | null;
  detected_at: string; resolved_at: string | null; resolved_by: string | null;
};

type ShieldEvent = {
  id: string; event_type: string; actor_id: string | null;
  tenant_id: string | null; score: number; patterns_matched: string[];
  recorded_at: string;
};

type Stats = {
  open_incidents: number; total_incidents: number; flagged_events: number;
  by_severity: Record<string, number>;
};

const PATTERN_TYPES = [
  "duplicate_case_flood", "dos_submission", "velocity_anomaly",
  "off_hours_bulk_access", "field_value_anomaly", "account_takeover",
  "insider_threat", "replay_attack",
];

const ACTIONS   = ["flag", "alert", "suspend", "block"];
const SEVERITIES = ["low", "medium", "high", "critical"];

const SEV_COLOR: Record<string, string> = {
  low: "#22c55e", medium: "#f59e0b", high: "#ef4444", critical: "#9333ea",
};
const ACTION_COLOR: Record<string, string> = {
  flag: "#0d9488", alert: "#f59e0b", suspend: "#ef4444", block: "#dc2626",
};
const STATUS_COLOR: Record<string, string> = {
  open: "#ef4444", resolved: "#22c55e", dismissed: "#94a3b8",
};

const S: Record<string, React.CSSProperties> = {
  page:      { display: "flex", flexDirection: "column", height: "100%", background: "var(--bg-main)", color: "var(--text-primary)", fontFamily: "system-ui, sans-serif" },
  header:    { padding: "18px 24px 0", flexShrink: 0 },
  title:     { fontSize: 20, fontWeight: 700, margin: 0 },
  sub:       { fontSize: 13, color: "var(--text-secondary)", marginTop: 4 },
  tabBar:    { display: "flex", gap: 4, padding: "14px 24px 0", borderBottom: "1px solid var(--border)", flexShrink: 0 },
  tab:       { padding: "8px 20px", border: "none", background: "none", fontSize: 13, cursor: "pointer", borderBottom: "2px solid transparent", fontWeight: 400, color: "var(--text-secondary)" },
  tabActive: { borderBottom: "2px solid var(--accent)", color: "var(--accent)", fontWeight: 700 },
  body:      { flex: 1, overflow: "auto", padding: "20px 28px" },
  card:      { background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: 8, padding: "16px 20px", marginBottom: 12 },
  btn:       { padding: "7px 16px", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12, fontWeight: 700 },
  btnP:      { background: "var(--accent)", color: "#fff" },
  btnS:      { background: "var(--bg-surface)", border: "1px solid var(--border)", color: "var(--text-secondary)" },
  btnD:      { background: "#fee2e2", color: "#ef4444", border: "1px solid #fecaca" },
  btnG:      { background: "#d1fae5", color: "#059669", border: "1px solid #a7f3d0" },
  input:     { width: "100%", padding: "7px 11px", border: "1px solid var(--border)", borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: "var(--bg-main)", color: "var(--text-primary)" },
  select:    { padding: "7px 11px", border: "1px solid var(--border)", borderRadius: 6, fontSize: 13, background: "var(--bg-main)", color: "var(--text-primary)", width: "100%" },
  label:     { fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", marginBottom: 4, display: "block" },
  badge:     { fontSize: 10, padding: "2px 8px", borderRadius: 10, fontWeight: 700 },
  tbl:       { width: "100%", borderCollapse: "collapse" as const, fontSize: 12 },
  th:        { textAlign: "left" as const, padding: "7px 10px", color: "var(--text-secondary)", fontWeight: 600, borderBottom: "1px solid var(--border)" },
  td:        { padding: "7px 10px", borderBottom: "1px solid var(--border)", verticalAlign: "middle" as const },
  row:       { display: "flex", gap: 10, marginBottom: 10 },
  statBox:   { background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: 8, padding: "18px 22px", flex: 1, minWidth: 140 },
  statNum:   { fontSize: 28, fontWeight: 800 },
  statLbl:   { fontSize: 12, color: "var(--text-secondary)", marginTop: 4 },
};

function Badge({ label, color }: { label: string; color: string }) {
  return <span style={{ ...S.badge, background: color + "22", color }}>{label}</span>;
}

function fmtDate(s: string | null) {
  return s ? new Date(s).toLocaleString() : "—";
}

function ScoreBar({ score }: { score: number }) {
  const color = score >= 0.8 ? "#ef4444" : score >= 0.5 ? "#f59e0b" : "#22c55e";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <div style={{ flex: 1, height: 6, background: "var(--border)", borderRadius: 3 }}>
        <div style={{ width: `${Math.round(score * 100)}%`, height: "100%", background: color, borderRadius: 3 }} />
      </div>
      <span style={{ fontSize: 11, color, fontWeight: 700, minWidth: 32 }}>{Math.round(score * 100)}%</span>
    </div>
  );
}


// ── Rules Tab ─────────────────────────────────────────────────────────────────

function RulesTab() {
  const [rules, setRules] = useState<SecurityRule[]>([]);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({
    name: "", pattern_type: PATTERN_TYPES[0], description: "",
    threshold: 10, window_seconds: 600, action: "flag", severity: "medium", enabled: true,
  });
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    const r = await authFetch(`${API}/rules`);
    if (r.ok) setRules(await r.json());
  }, []);
  useEffect(() => { load(); }, [load]);

  const save = async () => {
    setLoading(true);
    await authFetch(`${API}/rules`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...form }),
    });
    setCreating(false);
    setForm({ name: "", pattern_type: PATTERN_TYPES[0], description: "", threshold: 10, window_seconds: 600, action: "flag", severity: "medium", enabled: true });
    await load();
    setLoading(false);
  };

  const toggle = async (rule: SecurityRule) => {
    await authFetch(`${API}/rules/${rule.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !rule.enabled }),
    });
    await load();
  };

  const del = async (id: string) => {
    if (!confirm("Delete this rule?")) return;
    await authFetch(`${API}/rules/${id}`, { method: "DELETE" });
    await load();
  };

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <span style={{ fontWeight: 600 }}>{rules.length} detection rules</span>
        <button style={{ ...S.btn, ...S.btnP }} onClick={() => setCreating(true)}>+ New Rule</button>
      </div>

      {creating && (
        <div style={{ ...S.card, borderColor: "var(--accent)" }}>
          <div style={S.row}>
            <div style={{ flex: 2 }}>
              <label style={S.label}>Name</label>
              <input style={S.input} value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} placeholder="Rule name" />
            </div>
            <div style={{ flex: 2 }}>
              <label style={S.label}>Pattern Type</label>
              <select style={S.select} value={form.pattern_type} onChange={e => setForm(f => ({ ...f, pattern_type: e.target.value }))}>
                {PATTERN_TYPES.map(p => <option key={p} value={p}>{p}</option>)}
              </select>
            </div>
          </div>
          <div style={S.row}>
            <div style={{ flex: 1 }}>
              <label style={S.label}>Threshold (count)</label>
              <input style={S.input} type="number" value={form.threshold} onChange={e => setForm(f => ({ ...f, threshold: +e.target.value }))} />
            </div>
            <div style={{ flex: 1 }}>
              <label style={S.label}>Window (seconds)</label>
              <input style={S.input} type="number" value={form.window_seconds} onChange={e => setForm(f => ({ ...f, window_seconds: +e.target.value }))} />
            </div>
            <div style={{ flex: 1 }}>
              <label style={S.label}>Action</label>
              <select style={S.select} value={form.action} onChange={e => setForm(f => ({ ...f, action: e.target.value }))}>
                {ACTIONS.map(a => <option key={a} value={a}>{a}</option>)}
              </select>
            </div>
            <div style={{ flex: 1 }}>
              <label style={S.label}>Severity</label>
              <select style={S.select} value={form.severity} onChange={e => setForm(f => ({ ...f, severity: e.target.value }))}>
                {SEVERITIES.map(s => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
          </div>
          <div style={S.row}>
            <div style={{ flex: 1 }}>
              <label style={S.label}>Description</label>
              <input style={S.input} value={form.description} onChange={e => setForm(f => ({ ...f, description: e.target.value }))} placeholder="Optional" />
            </div>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <button style={{ ...S.btn, ...S.btnP }} onClick={save} disabled={loading || !form.name}>Commit</button>
            <button style={{ ...S.btn, ...S.btnS }} onClick={() => setCreating(false)}>Cancel</button>
          </div>
        </div>
      )}

      <table style={S.tbl}>
        <thead>
          <tr>
            <th style={S.th}>Name</th>
            <th style={S.th}>Pattern</th>
            <th style={S.th}>Threshold / Window</th>
            <th style={S.th}>Action</th>
            <th style={S.th}>Severity</th>
            <th style={S.th}>Enabled</th>
            <th style={S.th}></th>
          </tr>
        </thead>
        <tbody>
          {rules.map(r => (
            <tr key={r.id}>
              <td style={S.td}><span style={{ fontWeight: 600 }}>{r.name}</span></td>
              <td style={S.td}><Badge label={r.pattern_type} color="#0d9488" /></td>
              <td style={S.td}>{r.threshold}× in {r.window_seconds}s</td>
              <td style={S.td}><Badge label={r.action} color={ACTION_COLOR[r.action] ?? "#94a3b8"} /></td>
              <td style={S.td}><Badge label={r.severity} color={SEV_COLOR[r.severity] ?? "#94a3b8"} /></td>
              <td style={S.td}>
                <button
                  style={{ ...S.btn, ...(r.enabled ? S.btnG : S.btnD), fontSize: 11 }}
                  onClick={() => toggle(r)}
                >
                  {r.enabled ? "On" : "Off"}
                </button>
              </td>
              <td style={S.td}>
                <button style={{ ...S.btn, ...S.btnD, fontSize: 11 }} onClick={() => del(r.id)}>Delete</button>
              </td>
            </tr>
          ))}
          {rules.length === 0 && (
            <tr><td colSpan={7} style={{ ...S.td, color: "var(--text-secondary)", padding: 32 }}>No rules. Create one above.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}


// ── Incidents Tab ─────────────────────────────────────────────────────────────

function IncidentsTab() {
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [sevFilter, setSevFilter] = useState<string>("");
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async () => {
    const params = new URLSearchParams();
    if (statusFilter) params.set("status", statusFilter);
    if (sevFilter) params.set("severity", sevFilter);
    const r = await authFetch(`${API}/incidents?${params}`);
    if (r.ok) setIncidents(await r.json());
  }, [statusFilter, sevFilter]);
  useEffect(() => { load(); }, [load]);

  const resolve = async (id: string, action: "resolve" | "dismiss") => {
    await authFetch(`${API}/incidents/${id}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ resolved_by: "operator" }),
    });
    await load();
  };

  return (
    <div>
      <div style={{ display: "flex", gap: 10, marginBottom: 16, alignItems: "center" }}>
        <select style={{ ...S.select, width: 150 }} value={statusFilter} onChange={e => setStatusFilter(e.target.value)}>
          <option value="">All statuses</option>
          <option value="open">Open</option>
          <option value="resolved">Resolved</option>
          <option value="dismissed">Dismissed</option>
        </select>
        <select style={{ ...S.select, width: 150 }} value={sevFilter} onChange={e => setSevFilter(e.target.value)}>
          <option value="">All severities</option>
          {SEVERITIES.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>{incidents.length} incident(s)</span>
      </div>

      {incidents.map(inc => (
        <div key={inc.id} style={S.card}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
            <div>
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
                <Badge label={inc.pattern_type} color="#0d9488" />
                <Badge label={inc.severity} color={SEV_COLOR[inc.severity] ?? "#94a3b8"} />
                <Badge label={inc.status} color={STATUS_COLOR[inc.status] ?? "#94a3b8"} />
              </div>
              <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>
                Actor: <strong>{inc.actor_id ?? "—"}</strong> · {fmtDate(inc.detected_at)}
                {inc.tenant_id && <span> · Tenant: {inc.tenant_id}</span>}
              </div>
              {inc.explanation && (
                <div style={{ fontSize: 12, marginTop: 8, padding: "8px 12px", background: "var(--bg-main)", borderRadius: 6, borderLeft: "3px solid var(--accent)" }}>
                  {inc.explanation}
                </div>
              )}
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              {inc.status === "open" && (
                <>
                  <button style={{ ...S.btn, ...S.btnG, fontSize: 11 }} onClick={() => resolve(inc.id, "resolve")}>Resolve</button>
                  <button style={{ ...S.btn, ...S.btnS, fontSize: 11 }} onClick={() => resolve(inc.id, "dismiss")}>Dismiss</button>
                </>
              )}
              <button
                style={{ ...S.btn, ...S.btnS, fontSize: 11 }}
                onClick={() => setExpanded(expanded === inc.id ? null : inc.id)}
              >
                {expanded === inc.id ? "Hide" : "Context"}
              </button>
            </div>
          </div>
          {expanded === inc.id && (
            <pre style={{ marginTop: 10, fontSize: 11, background: "var(--bg-main)", padding: 10, borderRadius: 6, overflow: "auto", maxHeight: 200 }}>
              {JSON.stringify(inc.context, null, 2)}
            </pre>
          )}
        </div>
      ))}

      {incidents.length === 0 && (
        <div style={{ color: "var(--text-secondary)", padding: 60 }}>No incidents.</div>
      )}
    </div>
  );
}


// ── Events Tab ────────────────────────────────────────────────────────────────

function EventsTab() {
  const [events, setEvents] = useState<ShieldEvent[]>([]);
  const [minScore, setMinScore] = useState(0);

  const load = useCallback(async () => {
    const r = await authFetch(`${API}/events?min_score=${minScore / 100}&limit=100`);
    if (r.ok) setEvents(await r.json());
  }, [minScore]);
  useEffect(() => { load(); }, [load]);

  return (
    <div>
      <div style={{ display: "flex", gap: 10, marginBottom: 16, alignItems: "center" }}>
        <label style={{ fontSize: 12, color: "var(--text-secondary)" }}>Min score: {minScore}%</label>
        <input type="range" min={0} max={100} step={5} value={minScore} onChange={e => setMinScore(+e.target.value)} style={{ width: 150 }} />
        <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>{events.length} event(s)</span>
      </div>

      <table style={S.tbl}>
        <thead>
          <tr>
            <th style={S.th}>Event Type</th>
            <th style={S.th}>Actor</th>
            <th style={S.th}>Risk Score</th>
            <th style={S.th}>Patterns</th>
            <th style={S.th}>Recorded</th>
          </tr>
        </thead>
        <tbody>
          {events.map(ev => (
            <tr key={ev.id}>
              <td style={S.td}><Badge label={ev.event_type} color="#0d9488" /></td>
              <td style={S.td}>{ev.actor_id ?? "—"}</td>
              <td style={{ ...S.td, minWidth: 140 }}><ScoreBar score={ev.score} /></td>
              <td style={S.td}>{ev.patterns_matched.length > 0 ? ev.patterns_matched.map(p => <Badge key={p} label={p} color="#f59e0b" />) : <span style={{ color: "var(--text-secondary)" }}>none</span>}</td>
              <td style={S.td}>{fmtDate(ev.recorded_at)}</td>
            </tr>
          ))}
          {events.length === 0 && (
            <tr><td colSpan={5} style={{ ...S.td, color: "var(--text-secondary)", padding: 32 }}>No events yet.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}


// ── Stats Tab ─────────────────────────────────────────────────────────────────

function StatsTab() {
  const [stats, setStats] = useState<Stats | null>(null);

  const load = useCallback(async () => {
    const r = await authFetch(`${API}/stats`);
    if (r.ok) setStats(await r.json());
  }, []);
  useEffect(() => { load(); }, [load]);

  if (!stats) return <div style={{ padding: 40, color: "var(--text-secondary)" }}>Loading…</div>;

  return (
    <div>
      <div style={{ display: "flex", gap: 14, marginBottom: 24, flexWrap: "wrap" as const }}>
        <div style={S.statBox}>
          <div style={{ ...S.statNum, color: "#ef4444" }}>{stats.open_incidents}</div>
          <div style={S.statLbl}>Open Incidents</div>
        </div>
        <div style={S.statBox}>
          <div style={{ ...S.statNum }}>{stats.total_incidents}</div>
          <div style={S.statLbl}>Total Incidents</div>
        </div>
        <div style={S.statBox}>
          <div style={{ ...S.statNum, color: "#f59e0b" }}>{stats.flagged_events}</div>
          <div style={S.statLbl}>Flagged Events</div>
        </div>
      </div>

      {Object.keys(stats.by_severity).length > 0 && (
        <div style={S.card}>
          <div style={{ fontWeight: 600, marginBottom: 12 }}>By Severity</div>
          {Object.entries(stats.by_severity).map(([sev, count]) => (
            <div key={sev} style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 8 }}>
              <Badge label={sev} color={SEV_COLOR[sev] ?? "#94a3b8"} />
              <div style={{ flex: 1, height: 8, background: "var(--border)", borderRadius: 4 }}>
                <div style={{ width: `${Math.min(100, (count / stats.total_incidents) * 100)}%`, height: "100%", background: SEV_COLOR[sev] ?? "#94a3b8", borderRadius: 4 }} />
              </div>
              <span style={{ fontSize: 12, fontWeight: 700, minWidth: 24 }}>{count}</span>
            </div>
          ))}
        </div>
      )}

      <div style={{ ...S.card, marginTop: 16 }}>
        <div style={{ fontWeight: 600, marginBottom: 8 }}>Detection Patterns</div>
        <div style={{ display: "flex", flexWrap: "wrap" as const, gap: 6 }}>
          {PATTERN_TYPES.map(p => <Badge key={p} label={p} color="#0d9488" />)}
        </div>
      </div>
    </div>
  );
}


// ── Root ──────────────────────────────────────────────────────────────────────

const TABS = ["Rules", "Incidents", "Events", "Stats"] as const;

export default function HxShield() {
  const [tab, setTab] = useState<typeof TABS[number]>("Rules");

  return (
    <div style={S.page}>
      <div style={S.tabBar}>
        {TABS.map(t => (
          <button
            key={t}
            style={{ ...S.tab, ...(tab === t ? S.tabActive : {}) }}
            onClick={() => setTab(t)}
          >
            {t}
          </button>
        ))}
      </div>
      <div style={S.body}>
        {tab === "Rules"     && <RulesTab />}
        {tab === "Incidents" && <IncidentsTab />}
        {tab === "Events"    && <EventsTab />}
        {tab === "Stats"     && <StatsTab />}
      </div>
    </div>
  );
}
