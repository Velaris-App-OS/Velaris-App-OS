import React, { useState } from "react";
import { useApi } from "@shared/hooks";
import {
  getPMSummary, getPMActivityStats, getPMBottlenecks,
  getPMVariants, getPMFlowGraph, listCaseTypes, listTenants,
} from "@shared/api/client";
import { Card, Spinner, Stat, EmptyState } from "@shared/components";

/* ═══════════════════════════════════════════════════════════════════
   Process Mining — advanced flow analytics & discovery
   ═══════════════════════════════════════════════════════════════════ */

type Tab = "overview" | "bottlenecks" | "variants" | "flow" | "activities" | "insights";

export default function ProcessMining() {
  const [tab, setTab] = useState<Tab>("overview");
  const [days, setDays] = useState(30);
  const [tenantId, setTenantId] = useState<string>("");
  const [caseTypeId, setCaseTypeId] = useState<string>("");

  const { data: tenantData } = useApi(listTenants);
  const tenants: any[] = tenantData ?? [];

  const handleTenantChange = (tid: string) => {
    setTenantId(tid);
    setCaseTypeId("");
  };

  const { data: ctData } = useApi(
    () => listCaseTypes(1, tenantId || undefined),
    [tenantId]
  );
  const caseTypes = ctData?.items ?? [];

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", overflow: "auto", height: "100%", boxSizing: "border-box" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-xl)" }}>
        <div style={{ display: "flex", gap: "var(--space-sm)", alignItems: "center", flexWrap: "wrap" }}>
          <select value={tenantId} onChange={e => handleTenantChange(e.target.value)} style={selectStyle}>
            <option value="">All Tenants</option>
            {tenants.map((t: any) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
          <select value={caseTypeId} onChange={e => setCaseTypeId(e.target.value)} style={selectStyle}>
            <option value="">All Case Types</option>
            {caseTypes.map((ct: any) => <option key={ct.id} value={ct.id}>{ct.name}</option>)}
          </select>
          <div style={{ display: "flex", background: "var(--bg-card)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)" }}>
            {[7, 30, 90].map(d => (
              <button key={d} onClick={() => setDays(d)} style={{
                padding: "6px 12px", fontSize: 11, fontFamily: "var(--font-mono)", border: "none", cursor: "pointer",
                color: days === d ? "var(--accent)" : "var(--text-muted)",
                background: days === d ? "var(--accent-dim)" : "transparent",
                borderRadius: "var(--radius-sm)",
              }}>{d}d</button>
            ))}
          </div>
        </div>
      </div>

      <div style={{
        display: "flex", gap: 2, marginBottom: "var(--space-xl)",
        borderBottom: "1px solid var(--border-subtle)",
      }}>
        {(["overview", "bottlenecks", "variants", "flow", "activities", "insights"] as Tab[]).map(t => (
          <button key={t} onClick={() => setTab(t)} style={{
            padding: "10px 16px", fontSize: 12, fontWeight: 500, fontFamily: "var(--font-mono)",
            textTransform: "uppercase", letterSpacing: "0.04em", border: "none", cursor: "pointer",
            color: tab === t ? "var(--accent)" : "var(--text-muted)",
            background: "transparent",
            borderBottom: tab === t ? "2px solid var(--accent)" : "2px solid transparent",
            marginBottom: -1,
          }}>{t}</button>
        ))}
      </div>

      {tab === "overview" && <OverviewTab caseTypeId={caseTypeId} days={days} tenantId={tenantId} />}
      {tab === "bottlenecks" && <BottlenecksTab caseTypeId={caseTypeId} days={days} tenantId={tenantId} />}
      {tab === "variants" && <VariantsTab caseTypeId={caseTypeId} days={days} tenantId={tenantId} />}
      {tab === "flow" && <FlowTab caseTypeId={caseTypeId} days={days} tenantId={tenantId} />}
      {tab === "activities" && <ActivitiesTab caseTypeId={caseTypeId} days={days} tenantId={tenantId} />}
      {tab === "insights" && <InsightsTab caseTypeId={caseTypeId} days={days} />}
    </div>
  );
}

/* ── HxEvolve — Optimization Insights (design: docs/Future/hxevolve-self-optimizing.md)
   The platform proposes and proves; only a human changes anything. ─────────────── */

function _evolveHdr(): Record<string, string> {
  const token = localStorage.getItem("helix_token");
  return { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) };
}

interface EvolveInsight {
  id: string; case_type_id: string; proposal_kind: string; status: string;
  evidence_kind: string | null; rationale: string | null; signal: any;
  replay_run_id: string | null; branch_id: string | null;
  proposal?: any; evidence?: any; created_at: string | null;
}

function InsightsTab({ caseTypeId, days }: { caseTypeId: string; days: number }) {
  const [insights, setInsights] = useState<EvolveInsight[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [scanNote, setScanNote] = useState<string | null>(null);

  async function load() {
    const q = caseTypeId ? `?case_type_id=${caseTypeId}` : "";
    const r = await fetch(`/api/v1/hxevolve/insights${q}`, { headers: _evolveHdr() });
    if (r.ok) setInsights((await r.json()).insights);
  }
  React.useEffect(() => { load(); }, [caseTypeId]);

  async function scan() {
    if (!caseTypeId) { setError("Pick a case type to scan."); return; }
    setBusy(true); setError(null); setScanNote(null);
    try {
      const r = await fetch(`/api/v1/hxevolve/scan`, {
        method: "POST", headers: _evolveHdr(),
        body: JSON.stringify({ case_type_id: caseTypeId, days }),
      });
      const j = await r.json();
      if (!r.ok) { setError(j.detail || "Scan failed"); return; }
      setScanNote(j.hint ?? `${j.candidates} candidate(s) examined, ${j.insights.length} insight(s) surfaced, ${j.recorded} recorded for provenance.`);
      await load();
    } finally { setBusy(false); }
  }

  async function act(id: string, action: "stage" | "dismiss") {
    setBusy(true); setError(null);
    try {
      const r = await fetch(`/api/v1/hxevolve/insights/${id}/${action}`, { method: "POST", headers: _evolveHdr() });
      const j = await r.json();
      if (!r.ok) { setError(j.detail || `${action} failed`); return; }
      await load();
    } finally { setBusy(false); }
  }

  const KIND_LABEL: Record<string, string> = {
    rule_adjust: "adjust rule", rule_add: "new rule", sla_duration: "SLA duration",
    routing: "routing", reorder: "reorder steps",
  };

  return <>
    <div style={{ display: "flex", gap: "var(--space-md)", alignItems: "center", marginBottom: "var(--space-lg)" }}>
      <button onClick={scan} disabled={busy || !caseTypeId} style={{
        padding: "8px 16px", borderRadius: "var(--radius-sm)", border: "none", cursor: "pointer",
        background: "var(--accent)", color: "#fff", fontSize: 12, fontWeight: 600 }}>
        {busy ? "Scanning…" : "Scan for optimizations"}
      </button>
      <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
        Detect (mining) &gt; propose (AI on rails) &gt; prove (replay vetoes) — a human approves every change via HxBranch.
      </span>
    </div>
    {error && <Card><span style={{ color: "#ef4444", fontSize: 13 }}>{error}</span></Card>}
    {scanNote && <Card><span style={{ color: "var(--text-secondary)", fontSize: 13 }}>{scanNote}</span></Card>}
    {insights.length === 0 && !scanNote && (
      <EmptyState title="No insights yet" description="Pick a case type and run a scan. Only proposals that survive the validation gate and the guardrail vetoes are surfaced here." />
    )}
    {insights.map(i => (
      <Card key={i.id}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <span style={{ padding: "2px 8px", borderRadius: 999, fontSize: 10, fontWeight: 700, textTransform: "uppercase",
                           background: "var(--accent-dim)", color: "var(--accent)" }}>
              {KIND_LABEL[i.proposal_kind] ?? i.proposal_kind}
            </span>
            <span style={{ padding: "2px 8px", borderRadius: 999, fontSize: 10, fontWeight: 600,
                           background: i.evidence_kind === "counterfactual" ? "#22c55e22" : "#f59e0b22",
                           color: i.evidence_kind === "counterfactual" ? "#22c55e" : "#f59e0b" }}
                  title={i.evidence_kind === "counterfactual"
                    ? "Proven by HxReplay cohort simulation on real history"
                    : "Descriptive mining statistics — not a simulated proof"}>
              {i.evidence_kind === "counterfactual" ? "replay-proven" : "descriptive"}
            </span>
            {i.status === "staged" && <span style={{ fontSize: 11, color: "#22c55e", fontWeight: 700 }}>PR open ✓ (HxBranch)</span>}
            {i.status === "dismissed" && <span style={{ fontSize: 11, color: "var(--text-muted)" }}>dismissed</span>}
          </div>
          {i.status === "surfaced" && (
            <span style={{ display: "flex", gap: 8 }}>
              <button onClick={() => act(i.id, "stage")} disabled={busy} style={{
                padding: "6px 12px", borderRadius: 6, border: "none", cursor: "pointer",
                background: "var(--accent)", color: "#fff", fontSize: 12, fontWeight: 600 }}
                title="Opens an HxBranch PR with the evidence attached — a reviewer's approval merges it. HxEvolve never applies anything itself.">
                Open change PR
              </button>
              <button onClick={() => act(i.id, "dismiss")} disabled={busy} style={{
                padding: "6px 12px", borderRadius: 6, border: "1px solid var(--border-subtle)", cursor: "pointer",
                background: "transparent", color: "#ef4444", fontSize: 12 }}>
                Dismiss
              </button>
            </span>
          )}
        </div>
        <p style={{ fontSize: 13, margin: "8px 0 4px" }}>{i.rationale}</p>
        <div style={{ fontSize: 12, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
          signal: {i.signal?.kind} {i.signal?.target ? `· ${i.signal.target}` : ""}
          {i.signal?.magnitude_seconds ? ` · avg ${Math.round(i.signal.magnitude_seconds / 60)}m` : ""}
          {i.replay_run_id ? ` · replay run ${i.replay_run_id.slice(0, 8)}` : ""}
        </div>
      </Card>
    ))}
  </>;
}

function OverviewTab({ caseTypeId, days, tenantId }: { caseTypeId: string; days: number; tenantId: string }) {
  const { data, loading } = useApi(
    () => getPMSummary(caseTypeId || undefined, days, tenantId || undefined),
    [caseTypeId, days, tenantId]
  );

  if (loading || !data) return <Spinner size={28} />;

  if (data.total_events === 0) {
    return <EmptyState title="No events yet" description="Process mining needs event data. Create and move cases through their lifecycle to generate events." />;
  }

  const d = data.duration_stats || {};

  return <>
    <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "var(--space-md)", marginBottom: "var(--space-xl)" }}>
      <Card><Stat label="Total Events" value={data.total_events} /></Card>
      <Card><Stat label="Distinct Cases" value={data.distinct_cases} /></Card>
      <Card><Stat label="Distinct Activities" value={data.distinct_activities} /></Card>
    </div>
    <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "var(--space-md)" }}>
      <Card><Stat label="Cases Analyzed" value={d.cases_analyzed ?? 0} /></Card>
      <Card><Stat label="Avg Duration" value={d.avg_duration_hours ? `${d.avg_duration_hours}h` : "—"} /></Card>
      <Card><Stat label="Median Duration" value={d.median_duration_hours ? `${d.median_duration_hours}h` : "—"} /></Card>
      <Card><Stat label="P95 Duration" value={d.p95_duration_hours ? `${d.p95_duration_hours}h` : "—"} /></Card>
    </div>
  </>;
}

function BottlenecksTab({ caseTypeId, days, tenantId }: { caseTypeId: string; days: number; tenantId: string }) {
  const { data, loading } = useApi(
    () => getPMBottlenecks(caseTypeId || undefined, days, tenantId || undefined),
    [caseTypeId, days, tenantId]
  );

  if (loading) return <Spinner size={28} />;
  if (!data || data.length === 0) return <EmptyState title="No bottlenecks detected" description="Need event data with duration information." />;

  const sevColor = (s: string) => ({
    critical: "var(--status-failed)", high: "#f7b731",
    medium: "var(--accent)", low: "var(--status-completed)",
  }[s] || "var(--text-muted)");

  return (
    <div>
      {data.map((b: any, i: number) => (
        <Card key={i} style={{ marginBottom: "var(--space-sm)" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "var(--space-md)" }}>
              <div style={{
                width: 32, height: 32, borderRadius: "50%",
                background: sevColor(b.severity) + "33",
                border: `2px solid ${sevColor(b.severity)}`,
                display: "flex", alignItems: "center", justifyContent: "center",
                fontSize: 14, fontWeight: 700, color: sevColor(b.severity),
              }}>{i + 1}</div>
              <div>
                <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>{b.activity}</div>
                <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 2 }}>
                  {b.occurrences} occurrences · max: {formatDuration(b.max_duration_seconds)}
                </div>
              </div>
            </div>
            <div style={{ textAlign: "right" }}>
              <div style={{ fontSize: 20, fontWeight: 700, color: sevColor(b.severity), fontFamily: "var(--font-display)" }}>
                {formatDuration(b.avg_duration_seconds)}
              </div>
              <div style={{ fontSize: 9, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase" }}>
                avg · {b.severity}
              </div>
            </div>
          </div>
        </Card>
      ))}
    </div>
  );
}

function VariantsTab({ caseTypeId, days, tenantId }: { caseTypeId: string; days: number; tenantId: string }) {
  const { data, loading } = useApi(
    () => getPMVariants(caseTypeId || undefined, days, tenantId || undefined),
    [caseTypeId, days, tenantId]
  );

  if (loading) return <Spinner size={28} />;
  if (!data || data.length === 0) return <EmptyState title="No variants yet" description="Variants emerge as cases flow through activities." />;

  return (
    <div>
      {data.map((v: any) => (
        <Card key={v.variant_id} style={{ marginBottom: "var(--space-sm)" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "var(--space-sm)" }}>
            <div>
              <span style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>Variant #{v.variant_id}</span>
              <span style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginLeft: "var(--space-sm)" }}>
                {v.sequence.length} steps
              </span>
            </div>
            <div style={{ textAlign: "right" }}>
              <div style={{ fontSize: 18, fontWeight: 700, color: "var(--accent)", fontFamily: "var(--font-display)" }}>
                {v.percentage}%
              </div>
              <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>
                {v.case_count} cases
              </div>
            </div>
          </div>
          <div style={{ display: "flex", gap: 4, flexWrap: "wrap", alignItems: "center" }}>
            {v.sequence.map((a: string, i: number) => (
              <React.Fragment key={i}>
                <span style={{
                  fontSize: 10, padding: "3px 8px", borderRadius: "var(--radius-sm)",
                  background: "var(--bg-elevated)", color: "var(--text-secondary)", fontFamily: "var(--font-mono)",
                }}>{a}</span>
                {i < v.sequence.length - 1 && <span style={{ fontSize: 10, color: "var(--text-muted)" }}>→</span>}
              </React.Fragment>
            ))}
          </div>
        </Card>
      ))}
    </div>
  );
}

function FlowTab({ caseTypeId, days, tenantId }: { caseTypeId: string; days: number; tenantId: string }) {
  const { data, loading } = useApi(
    () => getPMFlowGraph(caseTypeId || undefined, days, tenantId || undefined),
    [caseTypeId, days, tenantId]
  );

  if (loading) return <Spinner size={28} />;
  if (!data || !data.edges || data.edges.length === 0) {
    return <EmptyState title="No flow data" description="Transitions are shown when at least 2 events occur in the same case." />;
  }

  const maxCount = Math.max(...data.edges.map((e: any) => e.count));

  return (
    <Card>
      <div style={{ marginBottom: "var(--space-md)", fontSize: 12, color: "var(--text-secondary)" }}>
        Directly-follows graph — {data.nodes.length} activities, {data.edges.length} transitions
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {data.edges.slice(0, 30).map((e: any, i: number) => (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)" }}>
            <span style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-secondary)", width: 150, textAlign: "right", textTransform: "uppercase", overflow: "hidden", textOverflow: "ellipsis" }}>
              {e.source}
            </span>
            <span style={{ color: "var(--accent)", fontSize: 12 }}>→</span>
            <span style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-secondary)", width: 150, textTransform: "uppercase", overflow: "hidden", textOverflow: "ellipsis" }}>
              {e.target}
            </span>
            <div style={{ flex: 1, height: 14, background: "var(--bg-elevated)", borderRadius: 3, overflow: "hidden" }}>
              <div style={{
                height: "100%", width: `${(e.count / maxCount) * 100}%`,
                background: "var(--accent)", transition: "width 0.3s",
              }} />
            </div>
            <span style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-primary)", width: 40, textAlign: "right", fontWeight: 600 }}>
              {e.count}
            </span>
          </div>
        ))}
      </div>
    </Card>
  );
}

function ActivitiesTab({ caseTypeId, days, tenantId }: { caseTypeId: string; days: number; tenantId: string }) {
  const { data, loading } = useApi(
    () => getPMActivityStats(caseTypeId || undefined, days, tenantId || undefined),
    [caseTypeId, days, tenantId]
  );

  if (loading) return <Spinner size={28} />;
  if (!data || data.length === 0) return <EmptyState title="No activity data" description="Events will appear here as cases run." />;

  return (
    <div>
      <div style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)", overflow: "hidden" }}>
        <div style={{
          display: "grid", gridTemplateColumns: "2fr 1fr 1fr 1fr 1fr",
          padding: "8px 16px", background: "var(--bg-elevated)",
          fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase",
        }}>
          <span>Activity</span>
          <span>Type</span>
          <span style={{ textAlign: "right" }}>Count</span>
          <span style={{ textAlign: "right" }}>Avg Duration</span>
          <span style={{ textAlign: "right" }}>Max</span>
        </div>
        {data.map((a: any, i: number) => (
          <div key={i} style={{
            display: "grid", gridTemplateColumns: "2fr 1fr 1fr 1fr 1fr",
            padding: "10px 16px", borderTop: "1px solid var(--border-subtle)",
            fontSize: 12, alignItems: "center",
          }}>
            <span style={{ color: "var(--text-primary)", fontWeight: 500 }}>{a.activity}</span>
            <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>{a.activity_type}</span>
            <span style={{ textAlign: "right", fontFamily: "var(--font-mono)" }}>{a.count}</span>
            <span style={{ textAlign: "right", fontFamily: "var(--font-mono)", color: "var(--text-secondary)" }}>
              {formatDuration(a.avg_duration_seconds)}
            </span>
            <span style={{ textAlign: "right", fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>
              {formatDuration(a.max_duration_seconds)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function formatDuration(seconds: number | null | undefined): string {
  if (!seconds) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h`;
  return `${(seconds / 86400).toFixed(1)}d`;
}

const selectStyle: React.CSSProperties = {
  padding: "6px 12px", fontSize: 12, fontFamily: "var(--font-body)",
  background: "var(--bg-input)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-primary)", outline: "none",
};
