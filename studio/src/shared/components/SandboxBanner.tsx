/**
 * SandboxBanner — amber top bar shown when developer is in sandbox mode.
 *
 * Sandbox mode is activated when a developer clicks "Enter Sandbox" on a
 * workspace in the Marketplace → Workspaces tab. The active workspace ID
 * is stored in localStorage under "velaris_sandbox_workspace_id".
 *
 * The banner shows: workspace name · expiry countdown · package count ·
 * network events badge · workspace switcher · [Exit Sandbox] button.
 */
import React, { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";

export const SANDBOX_KEY = "velaris_sandbox_workspace_id";

function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}

async function apiFetch(path: string) {
  return fetch(`/api/v1/marketplace${path}`, { headers: _authHdr() });
}

interface Workspace {
  id: string;
  name: string;
  status: string;
  expires_at: string;
  items: { package_id: string; status: string }[];
}

function daysUntil(isoDate: string): number {
  return Math.ceil((new Date(isoDate).getTime() - Date.now()) / 86_400_000);
}

export function useSandboxMode() {
  const [workspaceId, setWorkspaceId] = useState<string | null>(
    () => localStorage.getItem(SANDBOX_KEY)
  );

  const enter = useCallback((id: string) => {
    localStorage.setItem(SANDBOX_KEY, id);
    setWorkspaceId(id);
  }, []);

  const exit = useCallback(() => {
    localStorage.removeItem(SANDBOX_KEY);
    setWorkspaceId(null);
  }, []);

  return { workspaceId, enter, exit, active: !!workspaceId };
}

export default function SandboxBanner() {
  const navigate = useNavigate();
  const { workspaceId, exit } = useSandboxMode();
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [networkCount, setNetworkCount] = useState(0);
  const [allWorkspaces, setAllWorkspaces] = useState<Workspace[]>([]);
  const [showSwitcher, setShowSwitcher] = useState(false);

  const load = useCallback(async () => {
    if (!workspaceId) return;
    try {
      const [wsRes, logRes, allRes] = await Promise.all([
        apiFetch(`/workspaces`),
        apiFetch(`/workspaces/${workspaceId}/network-log`),
        apiFetch(`/workspaces`),
      ]);
      if (wsRes.ok) {
        const data = await wsRes.json();
        const ws = data.workspaces?.find((w: Workspace) => w.id === workspaceId);
        if (ws) setWorkspace(ws);
        setAllWorkspaces((data.workspaces ?? []).filter((w: Workspace) => w.status === "active"));
      }
      if (logRes.ok) {
        const logData = await logRes.json();
        setNetworkCount(logData.logs?.length ?? 0);
      }
    } catch { /* silent — banner is non-critical */ }
  }, [workspaceId]);

  useEffect(() => {
    load();
    const interval = setInterval(load, 30_000);
    return () => clearInterval(interval);
  }, [load]);

  // Listen for sandbox key changes from other tabs/components
  useEffect(() => {
    const handler = (e: StorageEvent) => {
      if (e.key === SANDBOX_KEY) window.location.reload();
    };
    window.addEventListener("storage", handler);
    return () => window.removeEventListener("storage", handler);
  }, []);

  if (!workspaceId || !workspace) return null;

  const days = daysUntil(workspace.expires_at);
  const expiryColor = days <= 1 ? "#ef4444" : days <= 7 ? "#f59e0b" : "#22c55e";
  const pkgCount = workspace.items?.length ?? 0;

  return (
    <div style={{
      background: "linear-gradient(90deg, #78350f 0%, #92400e 100%)",
      borderBottom: "2px solid #f59e0b",
      padding: "0 20px",
      height: 44,
      display: "flex",
      alignItems: "center",
      gap: 16,
      flexShrink: 0,
      zIndex: 100,
      position: "relative",
    }}>
      {/* Mode label */}
      <div style={{
        display: "flex", alignItems: "center", gap: 8,
        padding: "3px 10px", background: "#f59e0b22",
        border: "1px solid #f59e0b55", borderRadius: 6,
      }}>
        <span style={{ fontSize: 11, fontWeight: 800, color: "#f59e0b",
          fontFamily: "var(--font-mono)", letterSpacing: "0.1em" }}>
          ▣ SANDBOX
        </span>
      </div>

      {/* Workspace name + switcher */}
      <div style={{ position: "relative" }}>
        <button
          onClick={() => setShowSwitcher(v => !v)}
          style={{
            background: "transparent", border: "none", cursor: "pointer",
            display: "flex", alignItems: "center", gap: 6,
          }}
        >
          <span style={{ fontSize: 13, fontWeight: 700, color: "#fef3c7" }}>
            {workspace.name}
          </span>
          {allWorkspaces.length > 1 && (
            <span style={{ fontSize: 10, color: "#f59e0b" }}>▾</span>
          )}
        </button>

        {showSwitcher && allWorkspaces.length > 1 && (
          <div style={{
            position: "absolute", top: "100%", left: 0, marginTop: 4,
            background: "var(--bg-panel)", border: "1px solid var(--border-default)",
            borderRadius: "var(--radius-md)", minWidth: 220, zIndex: 200,
            boxShadow: "0 8px 24px rgba(0,0,0,.4)",
          }}>
            {allWorkspaces.map(ws => (
              <button key={ws.id}
                onClick={() => {
                  localStorage.setItem(SANDBOX_KEY, ws.id);
                  setShowSwitcher(false);
                  window.location.reload();
                }}
                style={{
                  width: "100%", textAlign: "left", padding: "10px 14px",
                  background: ws.id === workspaceId ? "var(--bg-subtle)" : "transparent",
                  border: "none", cursor: "pointer", fontSize: 13,
                  color: "var(--text-primary)", borderBottom: "1px solid var(--border-subtle)",
                }}
              >
                {ws.name}
                {ws.id === workspaceId && (
                  <span style={{ fontSize: 10, color: "var(--accent)", marginLeft: 8 }}>active</span>
                )}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Stats */}
      <div style={{ display: "flex", gap: 12, flex: 1 }}>
        <Pill label={`${pkgCount} package${pkgCount !== 1 ? "s" : ""}`} />
        <Pill
          label={`Expires in ${days}d`}
          color={expiryColor}
          title={new Date(workspace.expires_at).toLocaleDateString()}
        />
        {networkCount > 0 && (
          <Pill
            label={`${networkCount} network events`}
            color="#3b82f6"
            onClick={() => navigate("/marketplace")}
            clickable
          />
        )}
        <Pill label="⚠ Synthetic data only" color="#f59e0b" />
      </div>

      {/* Actions */}
      <div style={{ display: "flex", gap: 8 }}>
        <button
          onClick={() => navigate("/marketplace")}
          style={{
            padding: "4px 12px", fontSize: 11, fontWeight: 600,
            background: "#f59e0b22", border: "1px solid #f59e0b55",
            borderRadius: 6, cursor: "pointer", color: "#fef3c7",
          }}
        >
          View Workspace
        </button>
        <button
          onClick={() => { exit(); }}
          style={{
            padding: "4px 12px", fontSize: 11, fontWeight: 600,
            background: "transparent", border: "1px solid #fef3c755",
            borderRadius: 6, cursor: "pointer", color: "#fef3c7",
          }}
        >
          Exit Sandbox ×
        </button>
      </div>
    </div>
  );
}

function Pill({ label, color = "#fef3c7aa", title, onClick, clickable }: {
  label: string; color?: string; title?: string; onClick?: () => void; clickable?: boolean;
}) {
  return (
    <span
      title={title}
      onClick={onClick}
      style={{
        fontSize: 11, color, fontWeight: 500,
        cursor: clickable ? "pointer" : "default",
        textDecoration: clickable ? "underline" : "none",
      }}
    >
      {label}
    </span>
  );
}
