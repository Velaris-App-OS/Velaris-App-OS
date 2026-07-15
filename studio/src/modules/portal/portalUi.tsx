/**
 * Portal v2 (P1) — shared UI vocabulary for the customer portal.
 * Extracted from Portal.tsx so new views (CaseDetail, …) don't duplicate it.
 */
import React from "react";

export const STATUS_CFG: Record<string, { color: string; bg: string; icon: string; label: string }> = {
  new:         { color: "#0d9488", bg: "#eef2ff", icon: "🆕", label: "New" },
  open:        { color: "#3b82f6", bg: "#eff6ff", icon: "📂", label: "Open" },
  in_progress: { color: "#f59e0b", bg: "#fffbeb", icon: "⚙️", label: "In Progress" },
  pending:     { color: "#0f766e", bg: "#f5f3ff", icon: "⏳", label: "Pending" },
  resolved:    { color: "#22c55e", bg: "#f0fdf4", icon: "✅", label: "Resolved" },
  closed:      { color: "#6b7280", bg: "#f9fafb", icon: "🔒", label: "Closed" },
  cancelled:   { color: "#ef4444", bg: "#fef2f2", icon: "❌", label: "Cancelled" },
};

export function sl(s: string) { return s.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase()); }

export function fmtDate(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function fmtDateTime(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function darken(hex: string, amt: number): string {
  try {
    const n = parseInt(hex.replace("#", ""), 16);
    const r = Math.max(0, Math.min(255, (n >> 16) + amt));
    const g = Math.max(0, Math.min(255, ((n >> 8) & 0xff) + amt));
    const b = Math.max(0, Math.min(255, (n & 0xff) + amt));
    return `#${((1 << 24) + (r << 16) + (g << 8) + b).toString(16).slice(1)}`;
  } catch { return hex; }
}

export const C = {
  card:       { background: "#fff", borderRadius: 12, border: "1px solid #eaecf0", padding: 24, boxShadow: "0 1px 4px rgba(0,0,0,0.05)" } as React.CSSProperties,
  cardTitle:  { fontSize: 18, fontWeight: 800, marginBottom: 20, color: "#111827", letterSpacing: "-0.02em" } as React.CSSProperties,
  label:      { display: "block", fontSize: 11, fontWeight: 700, color: "#374151", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.05em" } as React.CSSProperties,
  input:      { display: "block", width: "100%", padding: "10px 12px", border: "1px solid #d1d5db", borderRadius: 8, fontSize: 14, marginBottom: 16, boxSizing: "border-box", fontFamily: "inherit", outline: "none" } as React.CSSProperties,
  primary:    (color: string) => ({ padding: "10px 20px", border: "none", borderRadius: 8, cursor: "pointer", background: color, color: "#fff", fontWeight: 700, fontSize: 13, display: "inline-flex", alignItems: "center", gap: 6 } as React.CSSProperties),
  secondary:  { padding: "10px 20px", border: "1px solid #d1d5db", borderRadius: 8, cursor: "pointer", background: "#fff", color: "#374151", fontWeight: 600, fontSize: 13, display: "inline-flex", alignItems: "center", gap: 6 } as React.CSSProperties,
  ghost:      { padding: "6px 0", border: "none", background: "none", cursor: "pointer", fontWeight: 500, fontSize: 13 } as React.CSSProperties,
  err:        { color: "#ef4444", fontSize: 13, marginBottom: 12 } as React.CSSProperties,
  metaLabel:  { fontSize: 10, color: "#9ca3af", textTransform: "uppercase", letterSpacing: "0.06em", fontWeight: 700 } as React.CSSProperties,
  sectionLabel:{ fontSize: 10, fontWeight: 700, color: "#9ca3af", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 12 } as React.CSSProperties,
};

export function StatusBadge({ status, large }: { status: string; large?: boolean }) {
  const sc = STATUS_CFG[status] ?? { color: "#6b7280", bg: "#f9fafb", icon: "•", label: sl(status) };
  return (
    <div style={{
      display: "inline-flex", alignItems: "center", gap: 5, flexShrink: 0,
      padding: large ? "6px 14px" : "4px 10px",
      borderRadius: 20, background: sc.bg, color: sc.color,
      fontSize: large ? 13 : 11, fontWeight: 700,
      border: `1px solid ${sc.color}28`,
    }}>
      <span style={{ fontSize: large ? 14 : 11 }}>{sc.icon}</span>
      {sc.label}
    </div>
  );
}

/** Grey shimmer block shown while a fetch is in flight. */
export function Skeleton({ h = 16, w = "100%", style }: { h?: number; w?: number | string; style?: React.CSSProperties }) {
  return (
    <div style={{
      height: h, width: w, borderRadius: 6,
      background: "linear-gradient(90deg, #eef0f3 25%, #f7f8fa 50%, #eef0f3 75%)",
      backgroundSize: "200% 100%", animation: "pshimmer 1.2s ease-in-out infinite",
      ...style,
    }} />
  );
}

export function SkeletonCard({ lines = 3 }: { lines?: number }) {
  return (
    <div style={C.card}>
      {Array.from({ length: lines }).map((_, i) => (
        <Skeleton key={i} h={i === 0 ? 20 : 14} w={i === 0 ? "50%" : `${90 - i * 12}%`}
                  style={{ marginBottom: 10 }} />
      ))}
    </div>
  );
}
