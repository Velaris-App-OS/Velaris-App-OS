import React from "react";
import type { ProcessStatus, InstanceStatus } from "@shared/types";

/* ═══════════════════════════════════════════════════════════════════
   StatusBadge — colored pill for process/instance status
   ═══════════════════════════════════════════════════════════════════ */

const STATUS_COLORS: Record<string, string> = {
  active: "var(--status-active)",
  running: "var(--status-running)",
  completed: "var(--status-completed)",
  failed: "var(--status-failed)",
  cancelled: "var(--status-cancelled)",
  suspended: "var(--status-cancelled)",
  deprecated: "var(--status-deprecated)",
};

export function StatusBadge({ status }: { status: ProcessStatus | InstanceStatus }) {
  const color = STATUS_COLORS[status] || "var(--text-muted)";
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "3px 10px",
        borderRadius: 100,
        fontSize: 12,
        fontWeight: 500,
        fontFamily: "var(--font-mono)",
        letterSpacing: "0.02em",
        color,
        background: `color-mix(in srgb, ${color} 12%, transparent)`,
        border: `1px solid color-mix(in srgb, ${color} 25%, transparent)`,
        textTransform: "uppercase",
      }}
    >
      <span
        style={{
          width: 6,
          height: 6,
          borderRadius: "50%",
          background: color,
          animation: status === "running" ? "pulse 2s infinite" : "none",
        }}
      />
      {status}
      <style>{`@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }`}</style>
    </span>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   Card — elevated surface container
   ═══════════════════════════════════════════════════════════════════ */

export function Card({
  children,
  onClick,
  style,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  style?: React.CSSProperties;
}) {
  return (
    <div
      onClick={onClick}
      style={{
        background: "var(--bg-card)",
        border: "1px solid var(--border-subtle)",
        borderRadius: "var(--radius-md)",
        padding: "var(--space-lg)",
        cursor: onClick ? "pointer" : "default",
        transition: "all 0.15s ease",
        ...(onClick && {
          ":hover": { background: "var(--bg-card-hover)" },
        }),
        ...style,
      }}
      onMouseEnter={(e) => onClick && (e.currentTarget.style.background = "var(--bg-card-hover)")}
      onMouseLeave={(e) => onClick && (e.currentTarget.style.background = "var(--bg-card)")}
    >
      {children}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   Button
   ═══════════════════════════════════════════════════════════════════ */

export function Button({
  children,
  onClick,
  variant = "primary",
  size = "md",
  disabled = false,
  style,
}: {
  children: React.ReactNode;
  onClick?: ((e?: React.MouseEvent<HTMLButtonElement>) => void);
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
  disabled?: boolean;
  style?: React.CSSProperties;
}) {
  const base: React.CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: 8,
    fontFamily: "var(--font-body)",
    fontWeight: 500,
    fontSize: size === "sm" ? 12 : 13,
    padding: size === "sm" ? "6px 12px" : "8px 16px",
    borderRadius: "var(--radius-sm)",
    border: "1px solid transparent",
    cursor: disabled ? "not-allowed" : "pointer",
    opacity: disabled ? 0.5 : 1,
    transition: "all 0.15s ease",
    letterSpacing: "0.01em",
  };

  const variants: Record<string, React.CSSProperties> = {
    primary: {
      background: "var(--accent)",
      color: "var(--text-inverse)",
      borderColor: "var(--accent)",
    },
    secondary: {
      background: "transparent",
      color: "var(--text-primary)",
      borderColor: "var(--border-default)",
    },
    ghost: {
      background: "transparent",
      color: "var(--text-secondary)",
      borderColor: "transparent",
    },
    danger: {
      background: "transparent",
      color: "var(--status-failed)",
      borderColor: "var(--status-failed)",
    },
  };

  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{ ...base, ...variants[variant], ...style }}
    >
      {children}
    </button>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   Spinner
   ═══════════════════════════════════════════════════════════════════ */

export function Spinner({ size = 20 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      style={{ animation: "spin 1s linear infinite" }}
    >
      <circle
        cx="12"
        cy="12"
        r="10"
        stroke="var(--border-default)"
        strokeWidth="2.5"
        fill="none"
      />
      <path
        d="M12 2a10 10 0 0 1 10 10"
        stroke="var(--accent)"
        strokeWidth="2.5"
        strokeLinecap="round"
        fill="none"
      />
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </svg>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   EmptyState
   ═══════════════════════════════════════════════════════════════════ */

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: React.ReactNode;
}) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: "var(--space-md)",
        padding: "var(--space-2xl)",
        color: "var(--text-muted)",
      }}
    >
      <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
        <rect
          x="8"
          y="8"
          width="32"
          height="32"
          rx="4"
          stroke="var(--border-default)"
          strokeWidth="2"
          strokeDasharray="4 4"
        />
        <path d="M20 24h8M24 20v8" stroke="var(--border-strong)" strokeWidth="2" strokeLinecap="round" />
      </svg>
      <div style={{ fontSize: 15, fontWeight: 500, color: "var(--text-secondary)" }}>{title}</div>
      {description && (
        <div style={{ fontSize: 13, maxWidth: 320, textAlign: "center" }}>{description}</div>
      )}
      {action && <div style={{ marginTop: "var(--space-sm)" }}>{action}</div>}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   TimeAgo — relative time display
   ═══════════════════════════════════════════════════════════════════ */

export function TimeAgo({ date }: { date: string }) {
  const seconds = Math.floor((Date.now() - new Date(date).getTime()) / 1000);

  if (seconds < 60) return <span title={date}>just now</span>;
  if (seconds < 3600) return <span title={date}>{Math.floor(seconds / 60)}m ago</span>;
  if (seconds < 86400) return <span title={date}>{Math.floor(seconds / 3600)}h ago</span>;
  return <span title={date}>{Math.floor(seconds / 86400)}d ago</span>;
}

/* ═══════════════════════════════════════════════════════════════════
   Stat — key-value display for dashboards
   ═══════════════════════════════════════════════════════════════════ */

export function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div>
      <div style={{ fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "var(--font-mono)" }}>
        {label}
      </div>
      <div style={{ fontSize: 24, fontWeight: 600, color: "var(--text-primary)", marginTop: 4, fontFamily: "var(--font-display)" }}>
        {value}
      </div>
    </div>
  );
}

export { CommitModal } from "./CommitModal";

export { CommitHistory } from "./CommitHistory";

export { BranchModeBanner } from "./BranchModeBanner";

export { ReviewerPicker } from "./ReviewerPicker";
