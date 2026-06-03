// HELIX P27 — Push Notifications Admin
import React, { useEffect, useState } from "react";

type Device = {
  id: string;
  user_id: string;
  channel: string;
  token_prefix: string;
  platform: string | null;
  label: string | null;
  is_active: boolean;
  last_seen_at: string | null;
  created_at: string;
};

type LogEntry = {
  id: string;
  user_id: string;
  event_type: string;
  channel: string;
  status: string;
  error: string | null;
  sent_at: string | null;
};

type TestSendForm = {
  user_id: string;
  event_type: string;
  title: string;
  body: string;
};

type Tab = "devices" | "logs" | "test";

function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}

async function apiJSON<T>(url: string, opts: RequestInit = {}): Promise<T> {
  const r = await fetch(url, { ...opts, headers: { ..._authHdr(), ...opts.headers } });
  if (!r.ok) {
    const txt = await r.text().catch(() => r.statusText);
    throw new Error(`${url} → ${r.status}: ${txt}`);
  }
  return r.json();
}

const CHANNEL_COLORS: Record<string, string> = {
  fcm:     "#22c55e",
  apns:    "#3b82f6",
  webpush: "#f59e0b",
};

export default function PushAdmin() {
  const [tab, setTab] = useState<Tab>("devices");

  const [devices, setDevices]       = useState<Device[]>([]);
  const [devTotal, setDevTotal]     = useState(0);
  const [devLoading, setDevLoading] = useState(false);
  const [devError, setDevError]     = useState<string | null>(null);

  const [logs, setLogs]             = useState<LogEntry[]>([]);
  const [logTotal, setLogTotal]     = useState(0);
  const [logLoading, setLogLoading] = useState(false);
  const [logError, setLogError]     = useState<string | null>(null);
  const [logFilter, setLogFilter]   = useState({ channel: "", status: "", user_id: "" });

  const [testForm, setTestForm]         = useState<TestSendForm>({ user_id: "", event_type: "test", title: "Velaris Test", body: "Test push notification." });
  const [testResults, setTestResults]   = useState<any[] | null>(null);
  const [testLoading, setTestLoading]   = useState(false);
  const [testError, setTestError]       = useState<string | null>(null);

  useEffect(() => {
    if (tab === "devices") loadDevices();
    if (tab === "logs") loadLogs();
  }, [tab]);

  async function loadDevices() {
    setDevLoading(true); setDevError(null);
    try {
      const data = await apiJSON<{ devices: Device[]; total: number }>(
        "/api/v1/push/admin/devices?active_only=false&page_size=100"
      );
      setDevices(data.devices);
      setDevTotal(data.total);
    } catch (e: any) { setDevError(e.message); }
    finally { setDevLoading(false); }
  }

  async function loadLogs() {
    setLogLoading(true); setLogError(null);
    const params = new URLSearchParams({ page_size: "100" });
    if (logFilter.channel) params.set("channel", logFilter.channel);
    if (logFilter.status) params.set("status", logFilter.status);
    if (logFilter.user_id) params.set("user_id", logFilter.user_id);
    try {
      const data = await apiJSON<{ logs: LogEntry[]; total: number }>(
        `/api/v1/push/admin/logs?${params}`
      );
      setLogs(data.logs);
      setLogTotal(data.total);
    } catch (e: any) { setLogError(e.message); }
    finally { setLogLoading(false); }
  }

  async function runTestSend() {
    setTestLoading(true); setTestError(null); setTestResults(null);
    try {
      const data = await apiJSON<{ results: any[] }>("/api/v1/push/test-send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(testForm),
      });
      setTestResults(data.results);
    } catch (e: any) { setTestError(e.message); }
    finally { setTestLoading(false); }
  }

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", height: "100%", overflow: "auto", boxSizing: "border-box" }}>
      {/* Tabs — Work Center style */}
      <div style={{ display: "flex", marginBottom: "var(--space-xl)" }}>
        <div style={{ display: "flex", background: "var(--bg-card)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)" }}>
          {(["devices", "logs", "test"] as Tab[]).map(t => (
            <button key={t} onClick={() => setTab(t)} style={{
              padding: "8px 18px", fontSize: 12, fontWeight: 500,
              fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.04em",
              border: "none", cursor: "pointer", borderRadius: "var(--radius-sm)",
              color: tab === t ? "var(--accent)" : "var(--text-muted)",
              background: tab === t ? "var(--accent-dim)" : "transparent",
            }}>
              {t === "test" ? "Test Send" : t}
            </button>
          ))}
        </div>
      </div>

      {/* ── Devices tab ── */}
      {tab === "devices" && (
        <div>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-md)" }}>
            <span style={{ color: "var(--text-muted)", fontSize: 13, fontFamily: "var(--font-mono)" }}>{devTotal} total devices</span>
            <button onClick={loadDevices} style={actionBtn}>↻ Refresh</button>
          </div>
          {devError && <div style={errBox}>{devError}</div>}
          {devLoading ? (
            <div style={{ color: "var(--text-muted)", padding: "var(--space-xl)" }}>Loading…</div>
          ) : (
            <table style={tableStyle}>
              <thead>
                <tr>
                  {["User", "Channel", "Token (prefix)", "Platform", "Label", "Active", "Last Seen", "Registered"].map(h => (
                    <th key={h} style={th}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {devices.map(d => (
                  <tr key={d.id}>
                    <td style={td}>{d.user_id}</td>
                    <td style={td}>
                      <ChannelBadge channel={d.channel} />
                    </td>
                    <td style={{ ...td, fontFamily: "var(--font-mono)", fontSize: 12 }}>{d.token_prefix}…</td>
                    <td style={td}>{d.platform || "—"}</td>
                    <td style={td}>{d.label || "—"}</td>
                    <td style={td}>
                      <span style={{ color: d.is_active ? "var(--status-completed)" : "var(--status-failed)", fontWeight: 500 }}>
                        {d.is_active ? "Active" : "Inactive"}
                      </span>
                    </td>
                    <td style={{ ...td, fontSize: 12, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                      {d.last_seen_at ? new Date(d.last_seen_at).toLocaleString() : "—"}
                    </td>
                    <td style={{ ...td, fontSize: 12, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                      {new Date(d.created_at).toLocaleDateString()}
                    </td>
                  </tr>
                ))}
                {devices.length === 0 && (
                  <tr><td colSpan={8} style={{ ...td, color: "var(--text-muted)", padding: "var(--space-xl)" }}>No devices registered yet.</td></tr>
                )}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* ── Logs tab ── */}
      {tab === "logs" && (
        <div>
          <div style={{ display: "flex", gap: "var(--space-md)", marginBottom: "var(--space-lg)", flexWrap: "wrap", alignItems: "flex-end" }}>
            {[
              { label: "User ID", key: "user_id", placeholder: "filter by user…" },
              { label: "Channel", key: "channel", placeholder: "fcm / apns / webpush" },
              { label: "Status", key: "status", placeholder: "delivered / failed" },
            ].map(({ label, key, placeholder }) => (
              <div key={key} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <label style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.04em" }}>{label}</label>
                <input
                  value={(logFilter as any)[key]}
                  onChange={e => setLogFilter(f => ({ ...f, [key]: e.target.value }))}
                  placeholder={placeholder}
                  style={inputStyle}
                />
              </div>
            ))}
            <button onClick={loadLogs} style={primaryBtn}>Apply</button>
          </div>
          <div style={{ color: "var(--text-muted)", marginBottom: "var(--space-md)", fontSize: 13, fontFamily: "var(--font-mono)" }}>{logTotal} entries</div>
          {logError && <div style={errBox}>{logError}</div>}
          {logLoading ? (
            <div style={{ color: "var(--text-muted)", padding: "var(--space-xl)" }}>Loading…</div>
          ) : (
            <table style={tableStyle}>
              <thead>
                <tr>
                  {["User", "Event", "Channel", "Status", "Error", "Sent At"].map(h => (
                    <th key={h} style={th}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {logs.map(l => (
                  <tr key={l.id}>
                    <td style={td}>{l.user_id}</td>
                    <td style={{ ...td, fontFamily: "var(--font-mono)", fontSize: 12 }}>{l.event_type}</td>
                    <td style={td}><ChannelBadge channel={l.channel} /></td>
                    <td style={td}>
                      <span style={{
                        color: l.status === "delivered" ? "var(--status-completed)" : l.status === "failed" ? "var(--status-failed)" : "var(--text-muted)",
                        fontWeight: 500, fontSize: 12,
                      }}>{l.status}</span>
                    </td>
                    <td style={{ ...td, color: "var(--status-failed)", fontSize: 12 }}>{l.error || "—"}</td>
                    <td style={{ ...td, fontSize: 12, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                      {l.sent_at ? new Date(l.sent_at).toLocaleString() : "—"}
                    </td>
                  </tr>
                ))}
                {logs.length === 0 && (
                  <tr><td colSpan={6} style={{ ...td, color: "var(--text-muted)", padding: "var(--space-xl)" }}>No delivery logs yet.</td></tr>
                )}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* ── Test Send tab ── */}
      {tab === "test" && (
        <div style={{ maxWidth: 520 }}>
          <p style={{ color: "var(--text-muted)", fontSize: 13, marginBottom: "var(--space-xl)", lineHeight: 1.6 }}>
            Send a test push notification to all active devices of a user.
          </p>
          <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-md)" }}>
            {[
              { label: "User ID *", key: "user_id", placeholder: "e.g. user-123" },
              { label: "Event Type", key: "event_type", placeholder: "e.g. test" },
              { label: "Title", key: "title", placeholder: "Notification title" },
              { label: "Body", key: "body", placeholder: "Notification body text" },
            ].map(({ label, key, placeholder }) => (
              <div key={key} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <label style={{ fontSize: 11, fontWeight: 500, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.04em", fontFamily: "var(--font-mono)" }}>{label}</label>
                <input
                  value={(testForm as any)[key]}
                  onChange={e => setTestForm(f => ({ ...f, [key]: e.target.value }))}
                  placeholder={placeholder}
                  style={inputStyle}
                />
              </div>
            ))}
            <button
              onClick={runTestSend}
              disabled={testLoading || !testForm.user_id}
              style={{ ...primaryBtn, padding: "10px 24px", fontSize: 13, opacity: testLoading || !testForm.user_id ? 0.6 : 1 }}
            >
              {testLoading ? "Sending…" : "Send Test Notification"}
            </button>
          </div>

          {testError && (
            <div style={{ marginTop: "var(--space-lg)", ...errBox }}>{testError}</div>
          )}

          {testResults && (
            <div style={{ marginTop: "var(--space-xl)" }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", marginBottom: "var(--space-md)" }}>
                Results ({testResults.length} device{testResults.length !== 1 ? "s" : ""})
              </div>
              {testResults.length === 0 ? (
                <p style={{ color: "var(--text-muted)", fontSize: 13 }}>No active devices found for this user.</p>
              ) : (
                testResults.map((r, i) => (
                  <div key={i} style={{
                    padding: "var(--space-md)", marginBottom: "var(--space-sm)",
                    borderRadius: "var(--radius-md)",
                    background: r.success
                      ? "color-mix(in srgb, var(--status-completed) 8%, transparent)"
                      : "color-mix(in srgb, var(--status-failed) 8%, transparent)",
                    border: `1px solid ${r.success ? "var(--status-completed)" : "var(--status-failed)"}`,
                    display: "flex", alignItems: "center", gap: "var(--space-sm)", flexWrap: "wrap",
                  }}>
                    <ChannelBadge channel={r.channel} />
                    <span style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--text-muted)" }}>{r.token_prefix}…</span>
                    <span style={{ marginLeft: "auto", color: r.success ? "var(--status-completed)" : "var(--status-failed)", fontWeight: 600, fontSize: 13 }}>
                      {r.success ? "Delivered" : `Failed: ${r.error}`}
                    </span>
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ChannelBadge({ channel }: { channel: string }) {
  const color = CHANNEL_COLORS[channel] || "var(--text-muted)";
  return (
    <span style={{
      fontSize: 10, fontWeight: 600, padding: "2px 8px", borderRadius: 100,
      fontFamily: "var(--font-mono)", textTransform: "uppercase",
      color, background: `color-mix(in srgb, ${color} 15%, transparent)`,
      border: `1px solid color-mix(in srgb, ${color} 30%, transparent)`,
    }}>{channel}</span>
  );
}

const tableStyle: React.CSSProperties = { width: "100%", borderCollapse: "collapse", fontSize: 13 };
const th: React.CSSProperties = { textAlign: "left", padding: "8px 10px", borderBottom: "1px solid var(--border-subtle)", color: "var(--text-muted)", fontWeight: 500, fontSize: 11, fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.04em", background: "var(--bg-elevated)" };
const td: React.CSSProperties = { padding: "8px 10px", borderBottom: "1px solid var(--border-subtle)", fontSize: 13, color: "var(--text-primary)" };
const inputStyle: React.CSSProperties = { padding: "7px 10px", fontSize: 13, border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", background: "var(--bg-input)", color: "var(--text-primary)", fontFamily: "var(--font-body)" };
const actionBtn: React.CSSProperties = { padding: "6px 14px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", background: "var(--bg-elevated)", fontSize: 12, cursor: "pointer", color: "var(--text-secondary)", fontFamily: "var(--font-body)" };
const primaryBtn: React.CSSProperties = { padding: "7px 16px", background: "var(--accent)", color: "#fff", border: "none", borderRadius: "var(--radius-sm)", cursor: "pointer", fontWeight: 600, fontSize: 12, fontFamily: "var(--font-body)" };
const errBox: React.CSSProperties = { padding: "var(--space-sm) var(--space-md)", background: "color-mix(in srgb, var(--status-failed) 10%, transparent)", border: "1px solid var(--status-failed)", borderRadius: "var(--radius-sm)", color: "var(--status-failed)", fontSize: 13, marginBottom: "var(--space-md)" };
