// HELIX P25 — Email Inbox
import React, { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

type Msg = {
  id: string;
  case_id: string | null;
  direction: "inbound" | "outbound";
  from_address: string;
  to_addresses: string[];
  subject: string;
  body_text: string;
  body_html: string | null;
  status: string;
  is_read: boolean;
  message_id: string | null;
  in_reply_to: string | null;
  error_message: string | null;
  sent_at: string | null;
  received_at: string | null;
  created_at: string | null;
};

type Stats = {
  unread_inbound: number;
  unmatched_inbound: number;
  failed_outbound: number;
};

type Filter = "inbox" | "unread" | "unmatched" | "outbound" | "failed";

const PAGE_SIZE = 20;
const AUTO_REFRESH_MS = 30_000;

function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}

async function apiJSON<T>(url: string, opts: RequestInit = {}): Promise<T> {
  const r = await fetch(url, { ...opts, headers: { ..._authHdr(), ...opts.headers } });
  if (!r.ok) {
    let detail = `${url} → ${r.status}`;
    try { const j = await r.json(); if (j?.detail) detail = j.detail; } catch {}
    throw new Error(detail);
  }
  return r.json();
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffH = diffMs / 3_600_000;
  if (diffH < 1) {
    const m = Math.round(diffMs / 60_000);
    return m <= 1 ? "just now" : `${m}m ago`;
  }
  if (diffH < 24) return `${Math.floor(diffH)}h ago`;
  if (diffH < 48) return "yesterday";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export default function EmailInbox() {
  const navigate = useNavigate();
  const [filter, setFilter] = useState<Filter>("inbox");
  const [messages, setMessages] = useState<Msg[]>([]);
  const [selected, setSelected] = useState<Msg | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [assignCaseId, setAssignCaseId] = useState("");
  const [page, setPage] = useState(1);
  const [lastSynced, setLastSynced] = useState<string | null>(null);
  const filterRef = useRef(filter);
  filterRef.current = filter;

  async function loadStats() {
    try {
      setStats(await apiJSON<Stats>("/api/v1/email/inbox/stats"));
      setLastSynced(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
    } catch (e: any) { setErr(e.message); }
  }

  async function loadMessages(f?: Filter) {
    setErr(null);
    const activeFilter = f ?? filterRef.current;
    try {
      const params = new URLSearchParams();
      if (activeFilter === "inbox" || activeFilter === "unread") params.set("direction", "inbound");
      if (activeFilter === "outbound" || activeFilter === "failed") params.set("direction", "outbound");
      if (activeFilter === "unread") params.set("unread_only", "true");
      if (activeFilter === "unmatched") { params.set("direction", "inbound"); params.set("unmatched_only", "true"); }
      params.set("limit", "500");
      let data = await apiJSON<Msg[]>(`/api/v1/email/messages?${params}`);
      if (activeFilter === "failed") data = data.filter(m => m.status === "failed");
      setMessages(data);
      setPage(1);
    } catch (e: any) { setErr(e.message); }
  }

  // Initial stats load + reload on filter change (fires on mount too)
  useEffect(() => { loadStats(); }, []);
  useEffect(() => { loadMessages(filter); }, [filter]);

  // Auto-refresh every 30s
  useEffect(() => {
    const id = setInterval(() => {
      loadStats();
      loadMessages();
    }, AUTO_REFRESH_MS);
    return () => clearInterval(id);
  }, []);

  async function markRead(m: Msg, read: boolean) {
    setBusy(true);
    try {
      await apiJSON(`/api/v1/email/messages/${m.id}/mark-read?read=${read}`, { method: "POST" });
      await loadMessages(); await loadStats();
      if (selected?.id === m.id) setSelected({ ...selected, is_read: read });
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  }

  async function assignToCase() {
    if (!selected || !assignCaseId.trim()) return;
    setBusy(true);
    try {
      const updated = await apiJSON<Msg>(
        `/api/v1/email/messages/${selected.id}/assign-case?case_id=${assignCaseId.trim()}`,
        { method: "POST" }
      );
      setSelected(updated);
      setAssignCaseId("");
      await loadMessages(); await loadStats();
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  }

  async function pollNow() {
    setBusy(true);
    try {
      await apiJSON("/api/v1/email/poll-now", { method: "POST" });
      await loadStats();
      await loadMessages();
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  }

  const total = messages.length;
  const totalPages = Math.ceil(total / PAGE_SIZE);
  const pageMessages = messages.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", height: "100%", overflow: "auto", boxSizing: "border-box" }}>
      {err && <div style={{ color: "var(--status-failed)", marginBottom: "var(--space-md)", fontSize: 13 }}>⚠ {err}</div>}

      {/* KPI row */}
      {stats && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "var(--space-md)", marginBottom: "var(--space-xl)" }}>
          <Kpi label="Unread inbound" value={stats.unread_inbound} warn={stats.unread_inbound > 0} onClick={() => setFilter("unread")} />
          <Kpi label="Unmatched inbound" value={stats.unmatched_inbound} warn={stats.unmatched_inbound > 0} onClick={() => setFilter("unmatched")} />
          <Kpi label="Failed outbound" value={stats.failed_outbound} warn={stats.failed_outbound > 0} onClick={() => setFilter("failed")} />
        </div>
      )}

      {/* Filter bar + actions */}
      <div style={{ display: "flex", gap: "var(--space-sm)", marginBottom: "var(--space-lg)", flexWrap: "wrap", alignItems: "center" }}>
        <div style={{ display: "flex", background: "var(--bg-card)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)" }}>
          {(["inbox", "unread", "unmatched", "outbound", "failed"] as Filter[]).map(f => (
            <button key={f} onClick={() => setFilter(f)} style={{
              padding: "7px 14px", fontSize: 12, fontWeight: 500,
              fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.04em",
              border: "none", cursor: "pointer", borderRadius: "var(--radius-sm)",
              color: filter === f ? "var(--accent)" : "var(--text-muted)",
              background: filter === f ? "var(--accent-dim)" : "transparent",
            }}>{f}</button>
          ))}
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: "var(--space-sm)", alignItems: "center" }}>
          {totalPages > 1 && (
            <PaginationBar page={page} totalPages={totalPages} total={total} onChange={p => { setPage(p); setSelected(null); }} />
          )}
          {lastSynced && (
            <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", whiteSpace: "nowrap" }}>
              ↻ {lastSynced}
            </span>
          )}
          <button onClick={pollNow} disabled={busy} style={actionBtn}>
            {busy ? "…" : "↻ Poll now"}
          </button>
          <button onClick={() => navigate("/email-admin")} style={{ ...actionBtn, color: "var(--text-muted)" }}
            title="Email account settings">
            ⚙ Settings
          </button>
        </div>
      </div>

      {/* Split view */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-lg)" }}>
        {/* Message list */}
        <div style={{ background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-md)", overflow: "hidden" }}>
          <div style={{ maxHeight: 560, overflow: "auto" }}>
            {pageMessages.length === 0 && (
              <div style={{ padding: "var(--space-xl)", color: "var(--text-muted)", fontSize: 13 }}>
                No messages.{filter === "inbox" && (
                  <span> Try <button onClick={pollNow} style={{ background: "none", border: "none", color: "var(--accent)", cursor: "pointer", fontSize: 13, padding: 0 }}>polling now</button> or check <button onClick={() => navigate("/email-admin")} style={{ background: "none", border: "none", color: "var(--accent)", cursor: "pointer", fontSize: 13, padding: 0 }}>account settings</button>.</span>
                )}
              </div>
            )}
            {pageMessages.map(m => (
              <div key={m.id} onClick={() => setSelected(m)}
                style={{
                  padding: "var(--space-sm) var(--space-md)",
                  borderBottom: "1px solid var(--border-subtle)",
                  cursor: "pointer",
                  background: selected?.id === m.id
                    ? "var(--accent-dim)"
                    : m.is_read ? "transparent" : "color-mix(in srgb, var(--status-running) 6%, transparent)",
                }}>
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, gap: 8 }}>
                  <strong style={{ color: m.is_read ? "var(--text-secondary)" : "var(--text-primary)", fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {m.direction === "inbound" ? m.from_address : `→ ${m.to_addresses.join(", ")}`}
                  </strong>
                  <span style={{ color: "var(--text-muted)", fontSize: 11, fontFamily: "var(--font-mono)", whiteSpace: "nowrap", flexShrink: 0 }}
                    title={new Date(m.received_at || m.sent_at || m.created_at || Date.now()).toLocaleString()}>
                    {fmtTime(m.received_at || m.sent_at || m.created_at || new Date().toISOString())}
                  </span>
                </div>
                <div style={{ fontSize: 13, marginTop: 2, color: m.is_read ? "var(--text-secondary)" : "var(--text-primary)", fontWeight: m.is_read ? 400 : 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {m.subject || "(no subject)"}
                </div>
                <div style={{ display: "flex", gap: 6, marginTop: 4, fontSize: 11 }}>
                  <StatusPill status={m.status} />
                  {m.case_id && <Tag color="var(--accent)">case</Tag>}
                  {!m.case_id && m.direction === "inbound" && <Tag color="var(--status-running)">unmatched</Tag>}
                </div>
              </div>
            ))}
          </div>
          {totalPages > 1 && (
            <div style={{ padding: "10px 16px", borderTop: "1px solid var(--border-subtle)", display: "flex", justifyContent: "flex-end" }}>
              <PaginationBar page={page} totalPages={totalPages} total={total} onChange={p => { setPage(p); setSelected(null); }} />
            </div>
          )}
        </div>

        {/* Detail panel */}
        <div style={{ background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-md)", padding: "var(--space-lg)", minHeight: 400 }}>
          {!selected && (
            <div style={{ color: "var(--text-muted)", fontSize: 13 }}>Select a message to read it.</div>
          )}
          {selected && (
            <>
              <div style={{ fontSize: 15, fontWeight: 600, color: "var(--text-primary)", marginBottom: "var(--space-sm)" }}>{selected.subject}</div>
              <div style={{ fontSize: 12, color: "var(--text-secondary)", marginBottom: "var(--space-sm)", display: "flex", flexDirection: "column", gap: 3 }}>
                <div><span style={{ color: "var(--text-muted)" }}>From:</span> {selected.from_address}</div>
                <div><span style={{ color: "var(--text-muted)" }}>To:</span> {selected.to_addresses.join(", ")}</div>
                {selected.message_id && (
                  <div style={{ fontFamily: "var(--font-mono)", fontSize: 10, color: "var(--text-muted)" }}>
                    Msg-Id: {selected.message_id}
                  </div>
                )}
                {selected.case_id && <div><span style={{ color: "var(--text-muted)" }}>Case:</span> <code style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>{selected.case_id}</code></div>}
                <div>
                  <span style={{ color: "var(--text-muted)" }}>Status:</span> {selected.status} · {selected.is_read ? "read" : "unread"}
                  {(selected.received_at || selected.sent_at) && (
                    <span style={{ color: "var(--text-muted)", marginLeft: 8, fontSize: 11, fontFamily: "var(--font-mono)" }}>
                      · {new Date(selected.received_at || selected.sent_at!).toLocaleString()}
                    </span>
                  )}
                </div>
                {selected.error_message && (
                  <div style={{ color: "var(--status-failed)" }}>Error: {selected.error_message}</div>
                )}
              </div>
              <div style={{ marginBottom: "var(--space-md)", display: "flex", gap: "var(--space-sm)", flexWrap: "wrap" }}>
                <button onClick={() => markRead(selected, !selected.is_read)} style={actionBtn}>
                  Mark {selected.is_read ? "unread" : "read"}
                </button>
                {!selected.case_id && selected.direction === "inbound" && (
                  <>
                    <input value={assignCaseId} onChange={e => setAssignCaseId(e.target.value)}
                      placeholder="Case UUID"
                      style={{ padding: "6px 10px", fontSize: 12, border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", width: 220, background: "var(--bg-input)", color: "var(--text-primary)", fontFamily: "var(--font-mono)" }} />
                    <button onClick={assignToCase} disabled={!assignCaseId.trim() || busy} style={actionBtn}>
                      Assign to case
                    </button>
                  </>
                )}
              </div>
              <hr style={{ margin: "var(--space-md) 0", border: 0, borderTop: "1px solid var(--border-subtle)" }} />
              {selected.body_html
                ? <iframe srcDoc={selected.body_html} sandbox="" style={{ width: "100%", height: 320, border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)" }} />
                : <pre style={{ whiteSpace: "pre-wrap", fontSize: 13, lineHeight: 1.6, color: "var(--text-primary)", fontFamily: "var(--font-body)" }}>{selected.body_text}</pre>}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Pagination ────────────────────────────────────────────────────────────────

const pageBtnStyle: React.CSSProperties = {
  width: 28, height: 28, borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)",
  background: "transparent", color: "var(--text-secondary)", fontSize: 12,
  cursor: "pointer", fontFamily: "var(--font-mono)", display: "flex", alignItems: "center", justifyContent: "center",
};

function PaginationBar({ page, totalPages, total, onChange }: {
  page: number; totalPages: number; total: number; onChange: (p: number) => void;
}) {
  const pages: (number | "…")[] = [];
  for (let i = 1; i <= totalPages; i++) {
    if (i === 1 || i === totalPages || Math.abs(i - page) <= 1) pages.push(i);
    else if (pages[pages.length - 1] !== "…") pages.push("…");
  }
  return (
    <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
      <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginRight: 4, whiteSpace: "nowrap" }}>
        {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, total)} / {total}
      </span>
      <button onClick={() => onChange(page - 1)} disabled={page === 1}
        style={{ ...pageBtnStyle, opacity: page === 1 ? 0.35 : 1 }}>‹</button>
      {pages.map((p, i) =>
        p === "…"
          ? <span key={`e${i}`} style={{ fontSize: 11, color: "var(--text-muted)", padding: "0 2px" }}>…</span>
          : <button key={p} onClick={() => onChange(p as number)} style={{
              ...pageBtnStyle,
              background: page === p ? "var(--accent)" : "transparent",
              color: page === p ? "#fff" : "var(--text-secondary)",
              borderColor: page === p ? "var(--accent)" : "var(--border-default)",
            }}>{p}</button>
      )}
      <button onClick={() => onChange(page + 1)} disabled={page >= totalPages}
        style={{ ...pageBtnStyle, opacity: page >= totalPages ? 0.35 : 1 }}>›</button>
    </div>
  );
}

// ── Sub-components ────────────────────────────────────────────────────────────

function Kpi({ label, value, warn = false, onClick }: { label: string; value: React.ReactNode; warn?: boolean; onClick?: () => void }) {
  return (
    <div onClick={onClick}
      style={{
        padding: "var(--space-md)",
        border: `1px solid ${warn ? "var(--status-failed)" : "var(--border-default)"}`,
        borderRadius: "var(--radius-md)",
        background: warn ? "color-mix(in srgb, var(--status-failed) 6%, transparent)" : "var(--bg-card)",
        borderLeft: warn ? `3px solid var(--status-failed)` : `3px solid var(--border-default)`,
        cursor: onClick ? "pointer" : "default",
        transition: "opacity 0.1s",
      }}
    >
      <div style={{ fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "var(--font-mono)", marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 24, fontWeight: 700, color: warn ? "var(--status-failed)" : "var(--text-primary)", fontFamily: "var(--font-mono)" }}>{value}</div>
    </div>
  );
}

function StatusPill({ status }: { status: string }) {
  const color = status === "sent" ? "var(--status-completed)"
    : status === "failed" ? "var(--status-failed)"
    : status === "received" ? "var(--status-completed)"
    : "var(--text-muted)";
  return (
    <span style={{
      fontSize: 10, fontWeight: 500, padding: "1px 7px", borderRadius: 100,
      fontFamily: "var(--font-mono)", textTransform: "uppercase",
      color, background: `color-mix(in srgb, ${color} 15%, transparent)`,
      border: `1px solid color-mix(in srgb, ${color} 25%, transparent)`,
    }}>{status}</span>
  );
}

function Tag({ color, children }: { color: string; children: React.ReactNode }) {
  return (
    <span style={{
      fontSize: 10, fontWeight: 500, padding: "1px 7px", borderRadius: 100,
      fontFamily: "var(--font-mono)", textTransform: "uppercase",
      color, background: `color-mix(in srgb, ${color} 15%, transparent)`,
      border: `1px solid color-mix(in srgb, ${color} 25%, transparent)`,
    }}>{children}</span>
  );
}

const actionBtn: React.CSSProperties = {
  padding: "6px 12px", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", background: "var(--bg-elevated)",
  fontSize: 12, cursor: "pointer", color: "var(--text-secondary)", fontFamily: "var(--font-body)",
};
