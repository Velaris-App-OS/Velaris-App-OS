import React, { useState, useEffect } from "react";
import { useSubscribe, useConnectionStatus } from "@shared/realtime/hooks";
import { Card, Spinner, Stat } from "@shared/components";
import { BRAND } from "@/branding";

/* ═══════════════════════════════════════════════════════════════════
   Live Activity — real-time event stream (Phase 22)
   ═══════════════════════════════════════════════════════════════════ */

interface Event {
  channel: string;
  timestamp: number;
  data: any;
}

export default function LiveActivity() {
  const [events, setEvents] = useState<Event[]>([]);
  const [stats, setStats] = useState<any>(null);
  const connected = useConnectionStatus();

  // Subscribe to all case events and global events
  useSubscribe("cases.*", (event) => {
    setEvents(prev => [event, ...prev].slice(0, 100));
  });
  useSubscribe("events.global", (event) => {
    setEvents(prev => [event, ...prev].slice(0, 100));
  });
  useSubscribe("assignments.*", (event) => {
    setEvents(prev => [event, ...prev].slice(0, 100));
  });

  // Periodic stats fetch
  useEffect(() => {
    const fetchStats = async () => {
      try {
        const token = localStorage.getItem("helix_token");
        const resp = await fetch("/api/v1/realtime/stats", {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (resp.ok) setStats(await resp.json());
      } catch {}
    };
    fetchStats();
    const timer = setInterval(fetchStats, 5000);
    return () => clearInterval(timer);
  }, []);

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", overflow: "auto", height: "100%", boxSizing: "border-box" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-xl)" }}>
        <div style={{
          display: "flex", alignItems: "center", gap: "var(--space-sm)",
          padding: "6px 12px", borderRadius: "var(--radius-sm)",
          background: connected ? "var(--accent-dim)" : "var(--bg-card)",
          border: `1px solid ${connected ? "var(--accent)" : "var(--border-subtle)"}`,
        }}>
          <span style={{
            width: 8, height: 8, borderRadius: "50%",
            background: connected ? "var(--status-completed)" : "var(--status-failed)",
            animation: connected ? "pulse 2s infinite" : "none",
          }} />
          <span style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text-secondary)" }}>
            {connected ? "Connected" : "Disconnected"}
          </span>
        </div>
      </div>

      {/* Stats */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "var(--space-md)", marginBottom: "var(--space-lg)" }}>
        <Card><Stat label="Active Connections" value={stats?.connections ?? 0} /></Card>
        <Card><Stat label="Channels" value={Object.keys(stats?.subscriptions || {}).length} /></Card>
        <Card><Stat label="Events Received" value={events.length} /></Card>
        <Card><Stat label="Resources Being Viewed" value={stats?.presence_resources ?? 0} /></Card>
      </div>

      {/* Event stream */}
      <Card>
        <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)",
          fontFamily: "var(--font-display)", marginBottom: "var(--space-md)" }}>
          Live Event Stream
        </div>

        {events.length === 0 && (
          <div style={{ padding: "var(--space-2xl)", color: "var(--text-muted)", fontSize: 13 }}>
            Waiting for events...
            <div style={{ fontSize: 11, marginTop: 8, fontFamily: "var(--font-mono)" }}>
              Try creating or updating a case to see events appear here in real-time.
            </div>
          </div>
        )}

        <div style={{ maxHeight: 600, overflow: "auto" }}>
          {events.map((e, i) => (
            <div key={i} style={{
              padding: "8px 12px", marginBottom: 4,
              background: i === 0 ? "var(--accent-dim)" : "var(--bg-elevated)",
              borderLeft: `3px solid ${channelColor(e.channel)}`,
              borderRadius: "var(--radius-sm)",
              animation: i === 0 ? "slideIn 0.3s ease-out" : "none",
            }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 2 }}>
                <span style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: channelColor(e.channel), fontWeight: 600 }}>
                  {e.channel}
                </span>
                <span style={{ fontSize: 9, fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>
                  {new Date(e.timestamp * 1000).toLocaleTimeString()}
                </span>
              </div>
              <div style={{ fontSize: 11, color: "var(--text-secondary)", fontFamily: "var(--font-mono)" }}>
                {e.data?.type && <span style={{ color: "var(--text-primary)", fontWeight: 600 }}>{e.data.type}</span>}
                {e.data?.actor_id && <span> · by <code>{e.data.actor_id}</code></span>}
                {e.data?.data && <span> · {JSON.stringify(e.data.data).slice(0, 100)}</span>}
              </div>
            </div>
          ))}
        </div>
      </Card>

      {/* CSS for animations */}
      <style>{`
        @keyframes pulse {
          0% { opacity: 1; }
          50% { opacity: 0.5; }
          100% { opacity: 1; }
        }
        @keyframes slideIn {
          from { opacity: 0; transform: translateY(-8px); }
          to { opacity: 1; transform: translateY(0); }
        }
      `}</style>
    </div>
  );
}

function channelColor(channel: string): string {
  if (channel.startsWith("cases.")) return "var(--accent)";
  if (channel.startsWith("assignments.")) return "#f7b731";
  if (channel.startsWith("presence.")) return "#9b59b6";
  if (channel.startsWith("events.")) return "var(--status-completed)";
  return "var(--text-muted)";
}
