import React from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "./AuthContext";

interface Props {
  allowedRoles: string[];   // OR logic — user needs at least one
  children: React.ReactNode;
  pageName?: string;        // human-readable label shown on the denied page
}

const ROLE_LABELS: Record<string, string> = {
  admin:       "Administrator",
  manager:     "Manager",
  designer:    "Designer",
  case_worker: "Case Worker",
  devops:      "DevOps",
  integration: "Integration",
  security:    "Security",
  viewer:      "Viewer",
};

export default function RequireRole({ allowedRoles, children, pageName }: Props) {
  const { user, hasRole } = useAuth();
  const navigate = useNavigate();

  const permitted = allowedRoles.length === 0 || allowedRoles.some(r => hasRole(r));
  if (permitted) return <>{children}</>;

  const roleList = allowedRoles.map(r => ROLE_LABELS[r] ?? r).join(", ");
  const userRoleLabel = !user ? "unknown"
    : user.is_admin ? "Administrator"
    : user.roles.includes("manager") ? "Manager"
    : user.is_designer ? "Designer"
    : user.is_case_worker ? "Case Worker"
    : user.roles.includes("viewer") ? "Viewer"
    : user.roles[0] ?? "unknown";

  return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "center",
      height: "100%", background: "var(--bg-root)", padding: "var(--space-2xl)",
    }}>
      <div style={{
        maxWidth: 480, width: "100%", textAlign: "center",
        background: "var(--bg-card)", border: "1px solid var(--border-subtle)",
        borderRadius: "var(--radius-lg)", padding: "var(--space-2xl)",
        boxShadow: "var(--shadow-lg)",
      }}>
        {/* Lock icon */}
        <div style={{
          width: 56, height: 56, borderRadius: "50%",
          background: "color-mix(in srgb, var(--status-failed) 12%, transparent)",
          display: "flex", alignItems: "center", justifyContent: "center",
          margin: "0 auto var(--space-lg)",
        }}>
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
            <rect x="3" y="11" width="18" height="11" rx="2" stroke="var(--status-failed)" strokeWidth="1.5" />
            <path d="M7 11V7a5 5 0 0 1 10 0v4" stroke="var(--status-failed)" strokeWidth="1.5" strokeLinecap="round" />
            <circle cx="12" cy="16" r="1.5" fill="var(--status-failed)" />
          </svg>
        </div>

        <div style={{ fontSize: 18, fontWeight: 700, color: "var(--text-primary)", marginBottom: 8 }}>
          Access Restricted
        </div>

        {pageName && (
          <div style={{ fontSize: 13, color: "var(--text-muted)", marginBottom: "var(--space-md)" }}>
            <strong style={{ color: "var(--text-secondary)" }}>{pageName}</strong> is not available for your account.
          </div>
        )}

        <div style={{
          fontSize: 12, color: "var(--text-muted)", background: "var(--bg-elevated)",
          borderRadius: "var(--radius-sm)", padding: "10px 14px",
          marginBottom: "var(--space-lg)", textAlign: "left", lineHeight: 1.7,
        }}>
          <div><span style={{ color: "var(--text-secondary)", fontWeight: 500 }}>Your role:</span> {userRoleLabel}</div>
          <div><span style={{ color: "var(--text-secondary)", fontWeight: 500 }}>Required:</span> {roleList}</div>
        </div>

        <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: "var(--space-lg)" }}>
          Contact your system administrator if you need access to this page.
        </div>

        <div style={{ display: "flex", gap: "var(--space-sm)", justifyContent: "center" }}>
          <button
            onClick={() => navigate(-1)}
            style={{
              padding: "8px 16px", fontSize: 13, borderRadius: "var(--radius-sm)",
              border: "1px solid var(--border-default)", background: "var(--bg-elevated)",
              color: "var(--text-secondary)", cursor: "pointer",
            }}
          >
            Go Back
          </button>
          <button
            onClick={() => navigate("/")}
            style={{
              padding: "8px 16px", fontSize: 13, borderRadius: "var(--radius-sm)",
              border: "none", background: "var(--accent)", color: "#fff", cursor: "pointer",
            }}
          >
            Go to Dashboard
          </button>
        </div>
      </div>
    </div>
  );
}
