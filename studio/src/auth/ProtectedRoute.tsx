import React from "react";
import { useAuth } from "./AuthContext";
import LoginPage from "./LoginPage";
import { Spinner } from "@shared/components";

/* ═══════════════════════════════════════════════════════════════════
   ProtectedRoute — wraps content that requires authentication
   ═══════════════════════════════════════════════════════════════════ */

interface Props {
  children: React.ReactNode;
  requiredRole?: string;
}

export default function ProtectedRoute({ children, requiredRole }: Props) {
  const { user, loading, hasRole } = useAuth();

  if (loading) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100vh", background: "var(--bg-root)" }}>
        <Spinner size={32} />
      </div>
    );
  }

  if (!user) return <LoginPage />;

  if (requiredRole && !hasRole(requiredRole)) {
    return (
      <div style={{
        display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
        height: "100vh", background: "var(--bg-root)", color: "var(--text-muted)",
      }}>
        <div style={{ fontSize: 48, marginBottom: "var(--space-md)" }}>🔒</div>
        <div style={{ fontSize: 18, fontWeight: 600, color: "var(--text-primary)" }}>Access Denied</div>
        <div style={{ fontSize: 13, marginTop: "var(--space-sm)" }}>
          You need the <strong>{requiredRole}</strong> role to access this page.
        </div>
      </div>
    );
  }

  return <>{children}</>;
}
