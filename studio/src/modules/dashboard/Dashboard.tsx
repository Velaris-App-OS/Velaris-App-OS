import React, { useState } from "react";
import { useApi } from "@shared/hooks";
import { getAnalyticsDashboard } from "@shared/api/client";
import { Card, Button, Spinner, Stat } from "@shared/components";
import { PlatformUpdateBanner } from "@shared/components/PlatformUpdateBanner";
import type { AnalyticsDashboard } from "@shared/api/client";

const STATUS_COLORS: Record<string, string> = {
  open: "#3b82f6",
  in_progress: "#f59e0b",
  pending: "#0f766e",
  resolved: "#22c55e",
  closed: "#6b7280",
  cancelled: "#ef4444",
};

const PRIORITY_COLORS: Record<string, string> = {
  low: "#6b7280",
  medium: "#f59e0b",
  high: "#ef4444",
  critical: "#dc2626",
  blocker: "#7f1d1d",
};

const DONUT_COLORS = ["#0d9488", "#3b82f6", "#f59e0b", "#8b5cf6", "#ef4444", "#10b981", "#ec4899"];

function pct(n: number, total: number) {
  if (!total) return 0;
  return Math.round((n / total) * 100);
}

function MiniBar({ value, max, color }: { value: number; max: number; color: string }) {
  const w = max ? Math.round((value / max) * 100) : 0;
  return (
    <div style={{ flex: 1, height: 6, background: "#e5e7eb", borderRadius: 3, overflow: "hidden" }}>
      <div style={{ width: `${w}%`, height: "100%", background: color, borderRadius: 3, transition: "width 0.4s" }} />
    </div>
  );
}

function DonutChart({ items, total }: { items: { label: string; value: number; color: string }[]; total: number }) {
  const cx = 70, cy = 70, r = 56, inner = 34;
  let angle = -Math.PI / 2;

  function arc(sa: number, ea: number) {
    const x1 = cx + r * Math.cos(sa), y1 = cy + r * Math.sin(sa);
    const x2 = cx + r * Math.cos(ea), y2 = cy + r * Math.sin(ea);
    const xi1 = cx + inner * Math.cos(ea), yi1 = cy + inner * Math.sin(ea);
    const xi2 = cx + inner * Math.cos(sa), yi2 = cy + inner * Math.sin(sa);
    const large = ea - sa > Math.PI ? 1 : 0;
    return `M${x1.toFixed(2)},${y1.toFixed(2)} A${r},${r} 0 ${large},1 ${x2.toFixed(2)},${y2.toFixed(2)} L${xi1.toFixed(2)},${yi1.toFixed(2)} A${inner},${inner} 0 ${large},0 ${xi2.toFixed(2)},${yi2.toFixed(2)} Z`;
  }

  const slices = items.map(item => {
    const sweep = total > 0 ? (item.value / total) * 2 * Math.PI : 0;
    const sa = angle, ea = angle + sweep;
    angle = ea;
    return { ...item, sa, ea };
  });

  const labelStr = total >= 1000 ? `${(total / 1000).toFixed(1)}k` : String(total);

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 20 }}>
      <svg width={140} height={140} viewBox="0 0 140 140" style={{ flexShrink: 0 }}>
        {slices.map((s, i) => (
          <path key={i} d={arc(s.sa, s.ea)} fill={s.color}>
            <title>{s.label}: {s.value}</title>
          </path>
        ))}
        <text x={cx} y={cy - 5} textAnchor="middle" style={{ fill: "var(--text-primary)" as any, fontSize: 18, fontWeight: 700, fontFamily: "var(--font-mono)" }}>{labelStr}</text>
        <text x={cx} y={cy + 11} textAnchor="middle" style={{ fill: "var(--text-muted)" as any, fontSize: 9, textTransform: "uppercase" as any, letterSpacing: "0.05em" }}>total</text>
      </svg>
      <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 7, minWidth: 0 }}>
        {items.map((item, i) => (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <div style={{ width: 8, height: 8, borderRadius: "50%", background: item.color, flexShrink: 0 }} />
            <span style={{ flex: 1, fontSize: 12, color: "var(--text-secondary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {item.label}
            </span>
            <span style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text-primary)", flexShrink: 0 }}>{item.value}</span>
            <span style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", width: 32, textAlign: "right", flexShrink: 0 }}>
              {pct(item.value, total)}%
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function buildDonutData(breakdown: { case_type_id: string; case_type_name: string; count: number }[]) {
  const sorted = [...breakdown].sort((a, b) => b.count - a.count);
  const top = sorted.slice(0, 7);
  const rest = sorted.slice(7);
  const items = top.map((ct, i) => ({
    label: ct.case_type_name || ct.case_type_id,
    value: ct.count,
    color: DONUT_COLORS[i],
  }));
  if (rest.length > 0) {
    items.push({ label: `Other (${rest.length})`, value: rest.reduce((s, x) => s + x.count, 0), color: "#9ca3af" });
  }
  return items;
}

function VelocityChart({
  created,
  resolved,
  days,
}: {
  created: { date: string; count: number }[];
  resolved: { date: string; count: number }[];
  days: number;
}) {
  const resolvedMap = Object.fromEntries(resolved.map(r => [r.date, r.count]));
  const allDates = [...new Set([...created.map(c => c.date), ...resolved.map(r => r.date)])].sort();
  if (allDates.length === 0) return null;

  const points = allDates.map(date => ({
    date,
    created: created.find(c => c.date === date)?.count ?? 0,
    resolved: resolvedMap[date] ?? 0,
  }));

  const maxVal = Math.max(...points.map(p => Math.max(p.created, p.resolved)), 1);

  return (
    <div>
      <div style={{ display: "flex", alignItems: "flex-end", gap: 2, height: 80 }}>
        {points.map((p, i) => (
          <div key={i} title={`${p.date}\nCreated: ${p.created}\nResolved: ${p.resolved}`}
            style={{ flex: 1, minWidth: 4, display: "flex", flexDirection: "column", alignItems: "stretch", gap: 1, justifyContent: "flex-end", height: "100%" }}>
            <div style={{ display: "flex", gap: 1, alignItems: "flex-end", height: "100%" }}>
              <div style={{
                flex: 1, background: "var(--accent)", opacity: 0.85, borderRadius: "2px 2px 0 0",
                height: `${Math.max(3, (p.created / maxVal) * 100)}%`,
              }} />
              <div style={{
                flex: 1, background: "#22c55e", opacity: 0.75, borderRadius: "2px 2px 0 0",
                height: `${Math.max(3, (p.resolved / maxVal) * 100)}%`,
              }} />
            </div>
          </div>
        ))}
      </div>
      <div style={{ display: "flex", gap: 16, marginTop: 8 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 11, color: "var(--text-muted)" }}>
          <div style={{ width: 10, height: 10, borderRadius: 2, background: "var(--accent)", opacity: 0.85 }} />
          Created
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 11, color: "var(--text-muted)" }}>
          <div style={{ width: 10, height: 10, borderRadius: 2, background: "#22c55e", opacity: 0.75 }} />
          Resolved
        </div>
      </div>
    </div>
  );
}

function resolutionLabel(hours: number | null): { text: string; color: string } {
  if (hours === null) return { text: "—", color: "var(--text-muted)" };
  if (hours < 4) return { text: `${hours.toFixed(1)}h`, color: "#22c55e" };
  if (hours < 24) return { text: `${hours.toFixed(1)}h`, color: "#f59e0b" };
  const days = (hours / 24).toFixed(1);
  return { text: `${days}d`, color: hours > 72 ? "#ef4444" : "#f59e0b" };
}

export default function Dashboard() {
  const [days, setDays] = useState(30);
  const { data, loading, error, refetch } = useApi(() => getAnalyticsDashboard({ days }), [days]);

  const d = data as AnalyticsDashboard | undefined;

  const totalStatus = d?.status_breakdown.reduce((s, x) => s + x.count, 0) || 0;
  const totalPriority = d?.priority_breakdown.reduce((s, x) => s + x.count, 0) || 0;

  const resolution = resolutionLabel(d?.overview.avg_resolution_hours ?? null);

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", overflow: "auto", height: "100%", boxSizing: "border-box" }}>
      <PlatformUpdateBanner />
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-xl)" }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          {(["7", "30", "90"] as const).map(n => (
            <button
              key={n} onClick={() => setDays(Number(n))}
              style={{
                padding: "6px 14px", border: "1px solid var(--border-default)",
                borderRadius: "var(--radius-sm)", cursor: "pointer", fontSize: 12,
                fontFamily: "var(--font-mono)",
                background: days === Number(n) ? "var(--accent)" : "var(--bg-elevated)",
                color: days === Number(n) ? "#fff" : "var(--text-secondary)",
                fontWeight: days === Number(n) ? 600 : 400,
              }}
            >
              {n}d
            </button>
          ))}
          <Button variant="secondary" size="sm" onClick={refetch}>↻</Button>
        </div>
      </div>

      {loading && (
        <div style={{ display: "flex", justifyContent: "center", padding: "var(--space-2xl)" }}>
          <Spinner size={28} />
        </div>
      )}

      {error && (
        <Card style={{ borderColor: "var(--status-failed)" }}>
          <p style={{ color: "var(--status-failed)", fontSize: 13 }}>Failed to load analytics: {error}</p>
          <Button variant="secondary" size="sm" onClick={refetch} style={{ marginTop: "var(--space-sm)" }}>Retry</Button>
        </Card>
      )}

      {d && !loading && (
        <>
          {/* Unassigned alert */}
          {d.assignments.unassigned > 0 && (
            <div style={{
              display: "flex", alignItems: "center", gap: 12,
              padding: "10px 16px",
              marginBottom: "var(--space-lg)",
              background: "rgba(245,158,11,0.08)",
              border: "1px solid rgba(245,158,11,0.35)",
              borderRadius: "var(--radius-sm)",
            }}>
              <span style={{ fontSize: 16 }}>⚠</span>
              <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>
                <strong style={{ color: "#f59e0b" }}>{d.assignments.unassigned} case{d.assignments.unassigned > 1 ? "s" : ""}</strong>
                {" "}currently unassigned — assign them to keep SLAs on track.
              </span>
            </div>
          )}

          {/* Overview stats */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: "var(--space-md)", marginBottom: "var(--space-xl)" }}>
            <Card><Stat label="Total Cases" value={d.overview.total_cases} /></Card>
            <Card><Stat label="Open" value={d.overview.open_cases} /></Card>
            <Card><Stat label="Resolved Today" value={d.overview.cases_resolved_today} /></Card>
            <Card>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>Avg Resolution</div>
              <div style={{ fontSize: 24, fontWeight: 700, fontFamily: "var(--font-mono)", color: resolution.color }}>{resolution.text}</div>
            </Card>
            <Card>
              <Stat
                label="SLA Compliance"
                value={`${d.sla_compliance.compliance_rate.toFixed(1)}%`}
              />
            </Card>
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-lg)", marginBottom: "var(--space-lg)" }}>
            {/* Status breakdown */}
            <Card>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: "var(--space-md)" }}>
                Status Breakdown
              </div>
              {d.status_breakdown.length === 0 ? (
                <p style={{ fontSize: 13, color: "var(--text-muted)", padding: "var(--space-lg)" }}>No cases yet</p>
              ) : (
                d.status_breakdown.map(item => (
                  <div key={item.status} style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)", marginBottom: 10 }}>
                    <span style={{ width: 80, fontSize: 12, color: "var(--text-secondary)", fontFamily: "var(--font-mono)", textTransform: "capitalize" }}>
                      {item.status.replace("_", " ")}
                    </span>
                    <MiniBar value={item.count} max={totalStatus} color={STATUS_COLORS[item.status] || "#0d9488"} />
                    <span style={{ width: 36, fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textAlign: "right" }}>
                      {pct(item.count, totalStatus)}%
                    </span>
                    <span style={{ width: 24, fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text-primary)", textAlign: "right" }}>
                      {item.count}
                    </span>
                  </div>
                ))
              )}
            </Card>

            {/* Priority breakdown */}
            <Card>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: "var(--space-md)" }}>
                Priority Breakdown
              </div>
              {d.priority_breakdown.length === 0 ? (
                <p style={{ fontSize: 13, color: "var(--text-muted)", padding: "var(--space-lg)" }}>No cases yet</p>
              ) : (
                d.priority_breakdown.map(item => (
                  <div key={item.priority} style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)", marginBottom: 10 }}>
                    <span style={{ width: 80, fontSize: 12, color: "var(--text-secondary)", fontFamily: "var(--font-mono)", textTransform: "capitalize" }}>
                      {item.priority}
                    </span>
                    <MiniBar value={item.count} max={totalPriority} color={PRIORITY_COLORS[item.priority] || "#0d9488"} />
                    <span style={{ width: 36, fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textAlign: "right" }}>
                      {pct(item.count, totalPriority)}%
                    </span>
                    <span style={{ width: 24, fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text-primary)", textAlign: "right" }}>
                      {item.count}
                    </span>
                  </div>
                ))
              )}
            </Card>
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-lg)", marginBottom: "var(--space-lg)" }}>
            {/* SLA summary */}
            <Card>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: "var(--space-md)" }}>
                SLA Health
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-sm)" }}>
                {[
                  { label: "On Track", value: d.sla_compliance.on_track, color: "#22c55e" },
                  { label: "At Risk", value: d.sla_compliance.at_risk, color: "#f59e0b" },
                  { label: "Breached", value: d.sla_compliance.breached, color: "#ef4444" },
                  { label: "Paused", value: d.sla_compliance.paused, color: "#6b7280" },
                ].map(item => (
                  <div key={item.label} style={{
                    padding: "var(--space-sm) var(--space-md)",
                    background: "var(--bg-elevated)",
                    borderRadius: "var(--radius-sm)",
                    borderLeft: `3px solid ${item.color}`,
                  }}>
                    <div style={{ fontSize: 20, fontWeight: 700, color: item.color, fontFamily: "var(--font-mono)" }}>
                      {item.value}
                    </div>
                    <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>{item.label}</div>
                  </div>
                ))}
              </div>
            </Card>

            {/* Assignments */}
            <Card>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: "var(--space-md)" }}>
                Assignments
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-sm)" }}>
                {[
                  { label: "Active", value: d.assignments.active, color: "#3b82f6" },
                  { label: "Unassigned", value: d.assignments.unassigned, color: "#f59e0b" },
                  { label: "Completed", value: d.assignments.completed, color: "#22c55e" },
                  { label: "Avg Hours", value: d.assignments.avg_completion_hours?.toFixed(1) ?? "—", color: "#0f766e" },
                ].map(item => (
                  <div key={item.label} style={{
                    padding: "var(--space-sm) var(--space-md)",
                    background: "var(--bg-elevated)",
                    borderRadius: "var(--radius-sm)",
                    borderLeft: `3px solid ${item.color}`,
                  }}>
                    <div style={{ fontSize: 20, fontWeight: 700, color: item.color, fontFamily: "var(--font-mono)" }}>
                      {item.value}
                    </div>
                    <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>{item.label}</div>
                  </div>
                ))}
              </div>
            </Card>
          </div>

          {/* Case velocity */}
          {(d.cases_over_time.length > 0 || d.resolved_over_time.length > 0) && (
            <Card style={{ marginBottom: "var(--space-lg)" }}>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: "var(--space-md)" }}>
                Case Velocity — Last {days} Days
              </div>
              <VelocityChart created={d.cases_over_time} resolved={d.resolved_over_time} days={days} />
            </Card>
          )}

          {/* Case type breakdown — donut chart */}
          {d.case_type_breakdown.length > 0 && (
            <Card>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: "var(--space-md)" }}>
                By Case Type
              </div>
              <DonutChart
                items={buildDonutData(d.case_type_breakdown)}
                total={d.case_type_breakdown.reduce((s, x) => s + x.count, 0)}
              />
            </Card>
          )}

          {/* Empty state */}
          {d.overview.total_cases === 0 && (
            <Card style={{ padding: "var(--space-2xl)", border: "2px dashed var(--border-subtle)", background: "transparent" }}>
              <div style={{ fontSize: 32, marginBottom: 8 }}>📊</div>
              <p style={{ fontSize: 14, color: "var(--text-muted)", marginBottom: 4 }}>No cases yet</p>
              <p style={{ fontSize: 12, color: "var(--text-muted)" }}>
                Create a case type in the Case Designer, then open cases to see analytics here.
              </p>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
