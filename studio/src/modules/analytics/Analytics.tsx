import React, { useState } from "react";
import { useApi } from "@shared/hooks";
import { getAnalyticsDashboard, listCaseTypes, listTenants } from "@shared/api/client";
import type { AnalyticsDashboard } from "@shared/api/client";
import { Card, Spinner, Stat } from "@shared/components";

/* ═══════════════════════════════════════════════════════════════════
   Analytics — reporting & metrics dashboard
   ═══════════════════════════════════════════════════════════════════ */

const STATUS_COLORS: Record<string, string> = {
  new:              "#3b82f6",   // blue   — fresh, not yet touched
  open:             "#0d9488",   // teal   — actively being worked
  in_progress:      "#f59e0b",   // amber  — in motion
  pending_external: "#f97316",   // orange — waiting on external party
  pending_subcase:  "#8b5cf6",   // purple — blocked by sub-case
  reopened:         "#eab308",   // yellow — needs attention again
  resolved:         "#22c55e",   // green  — done successfully
  closed:           "#6b7280",   // gray   — archived/closed
  cancelled:        "#ef4444",   // red    — terminated
};

const PRIORITY_COLORS: Record<string, string> = {
  low: "#55556a", medium: "#8888a0", high: "#f7b731",
  critical: "#fc5c65", blocker: "#fc5c65",
};

const SLA_COLORS: Record<string, string> = {
  on_track: "#4ecdc4", at_risk: "#f7b731", breached: "#fc5c65", paused: "#8888a0",
};

export default function Analytics() {
  const [days, setDays] = useState(30);
  const [tenantId, setTenantId] = useState<string>("");
  const [caseTypeId, setCaseTypeId] = useState<string>("");

  const { data: tenantData } = useApi(listTenants);
  const tenants: any[] = tenantData ?? [];

  // When tenant changes, reset case type selection
  const handleTenantChange = (tid: string) => {
    setTenantId(tid);
    setCaseTypeId("");
  };

  const { data: ctData } = useApi(
    () => listCaseTypes(1, tenantId || undefined),
    [tenantId]
  );
  const caseTypes = ctData?.items ?? [];

  const { data, loading, error } = useApi(
    () => getAnalyticsDashboard({
      days,
      case_type_id: caseTypeId || undefined,
      tenant_id: tenantId || undefined,
    }),
    [days, caseTypeId, tenantId]
  );

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", overflow: "auto", height: "100%", boxSizing: "border-box" }}>
      {/* Header */}
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

      {loading && <div style={{ display: "flex", justifyContent: "center", padding: "var(--space-2xl)" }}><Spinner size={28} /></div>}
      {error && <Card style={{ borderColor: "var(--status-failed)" }}><p style={{ color: "var(--status-failed)", fontSize: 13 }}>Failed to load analytics: {error}</p></Card>}

      {data && <>
        {/* Overview stats */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "var(--space-md)", marginBottom: "var(--space-xl)" }}>
          <Card><Stat label="Total Cases" value={data.overview.total_cases} /></Card>
          <Card><Stat label="Open" value={data.overview.open_cases} /></Card>
          <Card><Stat label="Resolved" value={data.overview.resolved_cases} /></Card>
          <Card><Stat label="Avg Resolution" value={data.overview.avg_resolution_hours ? `${data.overview.avg_resolution_hours}h` : "—"} /></Card>
        </div>

        {/* Today's activity */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "var(--space-md)", marginBottom: "var(--space-xl)" }}>
          <Card><Stat label="Created Today" value={data.overview.cases_created_today} /></Card>
          <Card><Stat label="Resolved Today" value={data.overview.cases_resolved_today} /></Card>
          <Card><Stat label="SLA Compliance" value={`${data.sla_compliance.compliance_rate}%`} /></Card>
          <Card><Stat label="Unassigned" value={data.assignments.unassigned} /></Card>
        </div>

        {/* Charts row */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-md)", marginBottom: "var(--space-xl)" }}>
          {/* Status breakdown */}
          <Card>
            <SectionTitle>Case Status Distribution</SectionTitle>
            <BarChart items={data.status_breakdown.map(s => ({
              label: s.status.replace(/_/g, " "),
              value: s.count,
              color: STATUS_COLORS[s.status] || "var(--text-muted)",
            }))} />
          </Card>

          {/* Priority breakdown */}
          <Card>
            <SectionTitle>Priority Distribution</SectionTitle>
            <BarChart items={data.priority_breakdown.map(p => ({
              label: p.priority,
              value: p.count,
              color: PRIORITY_COLORS[p.priority] || "var(--text-muted)",
            }))} />
          </Card>
        </div>

        {/* SLA + Assignments row */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-md)", marginBottom: "var(--space-xl)" }}>
          {/* SLA compliance */}
          <Card>
            <SectionTitle>SLA Compliance</SectionTitle>
            {data.sla_compliance.total_sla_instances === 0 ? (
              <div style={{ color: "var(--text-muted)", fontSize: 12, padding: "var(--space-lg) 0" }}>
                No SLA data yet
              </div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-sm)" }}>
                <DonutChart items={[
                  { label: "On Track", value: data.sla_compliance.on_track, color: SLA_COLORS.on_track },
                  { label: "At Risk", value: data.sla_compliance.at_risk, color: SLA_COLORS.at_risk },
                  { label: "Breached", value: data.sla_compliance.breached, color: SLA_COLORS.breached },
                  { label: "Paused", value: data.sla_compliance.paused, color: SLA_COLORS.paused },
                ]} />
              </div>
            )}
          </Card>

          {/* Assignment metrics */}
          <Card>
            <SectionTitle>Assignments</SectionTitle>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-md)", marginTop: "var(--space-md)" }}>
              <MiniStat label="Active" value={data.assignments.active} color="var(--accent)" />
              <MiniStat label="Completed" value={data.assignments.completed} color="var(--status-completed)" />
              <MiniStat label="Unassigned" value={data.assignments.unassigned} color="var(--status-running)" />
              <MiniStat label="Avg Completion" value={data.assignments.avg_completion_hours ? `${data.assignments.avg_completion_hours}h` : "—"} />
            </div>
          </Card>
        </div>

        {/* Cases over time */}
        <Card style={{ marginBottom: "var(--space-xl)" }}>
          <SectionTitle>Cases Created Over Time ({days} days)</SectionTitle>
          {data.cases_over_time.length === 0 ? (
            <div style={{ color: "var(--text-muted)", fontSize: 12, padding: "var(--space-lg) 0" }}>
              No data for this period
            </div>
          ) : (
            <SparklineChart points={data.cases_over_time} />
          )}
        </Card>

        {/* Case type breakdown */}
        {data.case_type_breakdown.length > 0 && (
          <Card>
            <SectionTitle>Cases by Type</SectionTitle>
            <BarChart items={data.case_type_breakdown.map(ct => ({
              label: ct.case_type_name,
              value: ct.count,
              color: "var(--accent)",
            }))} />
          </Card>
        )}
      </>}
    </div>
  );
}

/* ── Chart Components ─────────────────────────────────────────── */

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div style={{
      fontSize: 12, fontWeight: 600, color: "var(--text-secondary)",
      fontFamily: "var(--font-display)", marginBottom: "var(--space-md)",
    }}>{children}</div>
  );
}

function BarChart({ items }: { items: { label: string; value: number; color: string }[] }) {
  const max = Math.max(...items.map(i => i.value), 1);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {items.map((item, i) => (
        <div key={i} style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)" }}>
          {/* Color dot + label */}
          <div style={{ display: "flex", alignItems: "center", gap: 6, width: 130, flexShrink: 0 }}>
            <span style={{ width: 8, height: 8, borderRadius: "50%", background: item.color, flexShrink: 0 }} />
            <span style={{ fontSize: 11, color: "var(--text-secondary)", fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.03em", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {item.label}
            </span>
          </div>
          {/* Bar */}
          <div style={{ flex: 1, height: 18, background: "var(--bg-elevated)", borderRadius: 3, overflow: "hidden" }}>
            <div style={{
              height: "100%", width: `${(item.value / max) * 100}%`,
              background: item.color,
              opacity: 0.85,
              borderRadius: 3,
              transition: "width 0.5s ease",
              minWidth: item.value > 0 ? 4 : 0,
            }} />
          </div>
          {/* Count */}
          <span style={{ fontSize: 12, fontWeight: 600, fontFamily: "var(--font-mono)", color: item.color, width: 36, textAlign: "right", flexShrink: 0 }}>
            {item.value}
          </span>
        </div>
      ))}
    </div>
  );
}

function DonutChart({ items }: { items: { label: string; value: number; color: string }[] }) {
  const total = items.reduce((s, i) => s + i.value, 0);
  if (total === 0) return <div style={{ color: "var(--text-muted)", fontSize: 12 }}>No data</div>;

  let cumulative = 0;
  const size = 120;
  const cx = size / 2, cy = size / 2, r = 45, stroke = 12;

  return (
    <div style={{ display: "flex", alignItems: "center", gap: "var(--space-xl)" }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        {items.filter(i => i.value > 0).map((item, idx) => {
          const pct = item.value / total;
          const dashLen = 2 * Math.PI * r * pct;
          const dashTotal = 2 * Math.PI * r;
          const offset = -2 * Math.PI * r * cumulative + dashTotal * 0.25;
          cumulative += pct;
          return (
            <circle key={idx} cx={cx} cy={cy} r={r} fill="none"
              stroke={item.color} strokeWidth={stroke}
              strokeDasharray={`${dashLen} ${dashTotal - dashLen}`}
              strokeDashoffset={offset}
              style={{ transition: "stroke-dasharray 0.5s" }}
            />
          );
        })}
        <text x={cx} y={cy - 4} textAnchor="middle" fill="var(--text-primary)"
          fontSize="18" fontWeight="700" fontFamily="var(--font-display)">{total}</text>
        <text x={cx} y={cy + 12} textAnchor="middle" fill="var(--text-muted)"
          fontSize="9" fontFamily="var(--font-mono)">TOTAL</text>
      </svg>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {items.map((item, i) => (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
            <span style={{ width: 8, height: 8, borderRadius: "50%", background: item.color, flexShrink: 0 }} />
            <span style={{ color: "var(--text-secondary)", flex: 1 }}>{item.label}</span>
            <span style={{ fontFamily: "var(--font-mono)", fontWeight: 600, color: "var(--text-primary)" }}>{item.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function SparklineChart({ points }: { points: { date: string; count: number }[] }) {
  if (points.length === 0) return null;
  const max = Math.max(...points.map(p => p.count), 1);
  const w = 800, h = 120, pad = 20;
  const stepX = (w - pad * 2) / Math.max(points.length - 1, 1);

  const pathPoints = points.map((p, i) => ({
    x: pad + i * stepX,
    y: h - pad - ((p.count / max) * (h - pad * 2)),
  }));
  const d = pathPoints.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x} ${p.y}`).join(" ");
  const areaD = `${d} L ${pathPoints[pathPoints.length - 1].x} ${h - pad} L ${pad} ${h - pad} Z`;

  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", height: 140 }}>
      <defs>
        <linearGradient id="sparkGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.3" />
          <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={areaD} fill="url(#sparkGrad)" />
      <path d={d} fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round" />
      {pathPoints.map((p, i) => (
        <circle key={i} cx={p.x} cy={p.y} r="3" fill="var(--accent)" opacity={i === pathPoints.length - 1 ? 1 : 0.5}>
          <title>{points[i].date}: {points[i].count}</title>
        </circle>
      ))}
      {/* X-axis labels (show first, mid, last) */}
      {[0, Math.floor(points.length / 2), points.length - 1].map(i => (
        <text key={i} x={pathPoints[i]?.x} y={h - 2} textAnchor="middle"
          fill="var(--text-muted)" fontSize="9" fontFamily="var(--font-mono)">
          {points[i]?.date?.slice(5)}
        </text>
      ))}
    </svg>
  );
}

function MiniStat({ label, value, color }: { label: string; value: string | number; color?: string }) {
  return (
    <div style={{  }}>
      <div style={{ fontSize: 20, fontWeight: 600, color: color || "var(--text-primary)", fontFamily: "var(--font-display)" }}>
        {value}
      </div>
      <div style={{ fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase", fontFamily: "var(--font-mono)" }}>
        {label}
      </div>
    </div>
  );
}

const selectStyle: React.CSSProperties = {
  padding: "6px 12px", fontSize: 12, fontFamily: "var(--font-body)",
  background: "var(--bg-input)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-primary)", outline: "none",
};
