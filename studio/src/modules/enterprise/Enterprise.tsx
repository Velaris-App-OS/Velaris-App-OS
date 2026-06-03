import React, { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useApi } from "@shared/hooks";
import {
  listSecurityEvents, getSecurityEventStats, listGDPRRequests,
  anonymizeUser, lookupUserData,
  listRetentionPolicies, updateRetentionPolicy,
  getEnterpriseSystemInfo, resolveGDPRRequest,
} from "@shared/api/client";
import { Card, Button, Spinner, Stat, EmptyState, TimeAgo } from "@shared/components";

/* ═══════════════════════════════════════════════════════════════════
   Enterprise — GDPR, Security Events, Retention (Phase 20)
   ═══════════════════════════════════════════════════════════════════ */

type Tab = "overview" | "events" | "gdpr" | "retention";

export default function Enterprise() {
  const [tab, setTab] = useState<Tab>("overview");

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", overflow: "auto", height: "100%", boxSizing: "border-box" }}>
      <div style={{ display: "flex", gap: 2, marginBottom: "var(--space-xl)", borderBottom: "1px solid var(--border-subtle)" }}>
        {(["overview", "events", "gdpr", "retention"] as Tab[]).map(t => (
          <button key={t} onClick={() => setTab(t)} style={{
            padding: "10px 16px", fontSize: 12, fontWeight: 500, fontFamily: "var(--font-mono)",
            textTransform: "uppercase", letterSpacing: "0.04em", border: "none", cursor: "pointer",
            color: tab === t ? "var(--accent)" : "var(--text-muted)",
            background: "transparent",
            borderBottom: tab === t ? "2px solid var(--accent)" : "2px solid transparent",
            marginBottom: -1,
          }}>{t}</button>
        ))}
      </div>

      {tab === "overview"  && <OverviewTab setTab={setTab} />}
      {tab === "events"    && <SecurityEventsTab />}
      {tab === "gdpr"      && <GDPRTab />}
      {tab === "retention" && <RetentionTab />}
    </div>
  );
}

/* ── Overview ──────────────────────────────────────────────────── */

const clickableCard: React.CSSProperties = { cursor: "pointer", transition: "opacity 0.15s" };

function OverviewTab({ setTab }: { setTab: (t: Tab) => void }) {
  const { data: info, loading } = useApi(getEnterpriseSystemInfo);
  const navigate = useNavigate();

  if (loading) return <Spinner size={28} />;

  const si = info ?? {};

  const gdprHealth = si.gdpr_requests_pending > 0
    ? { label: `${si.gdpr_requests_pending} pending`, color: "#f7b731" }
    : { label: "All resolved", color: "var(--status-completed)" };

  const secHealth = si.security_failed_30d > 0
    ? { label: `${si.security_failed_30d} failed/denied (30d)`, color: "#f7b731" }
    : { label: "Clean (30d)", color: "var(--status-completed)" };

  return (
    <>
      {/* Row 1 — Platform */}
      <SectionLabel>Platform</SectionLabel>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "var(--space-md)", marginBottom: "var(--space-lg)" }}>
        <Card><Stat label="Version"        value={si.version          ?? "—"} /></Card>
        <Card><Stat label="Total Cases"    value={si.total_cases      ?? 0}   /></Card>
        <Card><Stat label="Case Types"     value={si.total_case_types ?? 0}  /></Card>
        <Card><Stat label="Active Tenants" value={si.active_tenants   ?? 0}  /></Card>
      </div>

      {/* Row 2 — Users */}
      <SectionLabel link onClick={() => navigate("/user-directory")}>Users</SectionLabel>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "var(--space-md)", marginBottom: "var(--space-lg)" }}>
        <Card style={clickableCard} onClick={() => navigate("/user-directory")}><Stat label="Total Users"  value={si.total_users  ?? 0} /></Card>
        <Card style={clickableCard} onClick={() => navigate("/user-directory")}><Stat label="Active Users" value={si.active_users ?? 0} /></Card>
        <Card style={clickableCard} onClick={() => navigate("/user-directory")}><Stat label="Inactive Users" value={(si.total_users ?? 0) - (si.active_users ?? 0)} /></Card>
        <Card style={clickableCard} onClick={() => navigate("/user-directory")}><Stat label="Active Tenants" value={si.active_tenants ?? 0} /></Card>
      </div>

      {/* Row 3 — Security */}
      <SectionLabel link onClick={() => setTab("events")}>Security Events</SectionLabel>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "var(--space-md)", marginBottom: "var(--space-lg)" }}>
        <Card style={clickableCard} onClick={() => setTab("events")}><Stat label="Events (24h)" value={si.security_events_24h ?? 0} /></Card>
        <Card style={clickableCard} onClick={() => setTab("events")}><Stat label="Events (30d)" value={si.security_events_30d ?? 0} /></Card>
        <Card style={clickableCard} onClick={() => setTab("events")}>
          <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>Failed / Denied (30d)</div>
          <div style={{ fontSize: 22, fontWeight: 700, color: secHealth.color }}>{si.security_failed_30d ?? 0}</div>
          <div style={{ fontSize: 10, color: secHealth.color, fontFamily: "var(--font-mono)", marginTop: 2 }}>{secHealth.label}</div>
        </Card>
        <Card style={clickableCard} onClick={() => setTab("events")}>
          <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>Security Health</div>
          <div style={{ fontSize: 13, fontWeight: 600, color: secHealth.color, marginTop: 6 }}>{secHealth.label}</div>
        </Card>
      </div>

      {/* Row 4 — Compliance */}
      <SectionLabel link onClick={() => setTab("gdpr")}>Compliance</SectionLabel>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "var(--space-md)", marginBottom: "var(--space-lg)" }}>
        <Card style={clickableCard} onClick={() => setTab("gdpr")}>
          <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>GDPR Requests</div>
          <div style={{ fontSize: 22, fontWeight: 700, color: "var(--text-primary)" }}>{si.gdpr_requests_total ?? 0}</div>
          <div style={{ fontSize: 10, color: gdprHealth.color, fontFamily: "var(--font-mono)", marginTop: 2 }}>{gdprHealth.label}</div>
        </Card>
        <Card style={clickableCard} onClick={() => setTab("gdpr")}><Stat label="GDPR Completed" value={si.gdpr_requests_completed ?? 0} /></Card>
        <Card style={clickableCard} onClick={() => setTab("retention")}>
          <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>Retention Policies</div>
          <div style={{ fontSize: 22, fontWeight: 700, color: "var(--text-primary)" }}>{si.retention_policies_active ?? 0} <span style={{ fontSize: 13, fontWeight: 400, color: "var(--text-muted)" }}>/ {si.retention_policies_total ?? 0}</span></div>
          <div style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginTop: 2 }}>active / total</div>
        </Card>
        <Card style={clickableCard} onClick={() => setTab("gdpr")}>
          <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>Compliance Health</div>
          <div style={{ fontSize: 13, fontWeight: 600, color: gdprHealth.color, marginTop: 6 }}>{gdprHealth.label}</div>
        </Card>
      </div>
    </>
  );
}

function SectionLabel({ children, link, onClick }: { children: React.ReactNode; link?: boolean; onClick?: () => void }) {
  return (
    <div
      onClick={onClick}
      style={{
        fontSize: 11, fontWeight: 600, fontFamily: "var(--font-mono)", textTransform: "uppercase",
        letterSpacing: "0.08em", color: link ? "var(--accent)" : "var(--text-muted)",
        marginBottom: "var(--space-sm)", display: "flex", alignItems: "center", gap: 4,
        cursor: link ? "pointer" : "default",
      }}
    >
      {children}
      {link && <span style={{ fontSize: 9, opacity: 0.7 }}>→</span>}
    </div>
  );
}

/* ── Security Events ───────────────────────────────────────────── */

const PAGE_SIZE = 25;

const pageBtnStyle: React.CSSProperties = {
  width: 28, height: 28, borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)",
  background: "transparent", color: "var(--text-secondary)", fontSize: 12,
  cursor: "pointer", fontFamily: "var(--font-mono)", display: "flex", alignItems: "center", justifyContent: "center",
};

function PaginationBar({ page, totalPages, total, pageSize, onChange }: {
  page: number; totalPages: number; total: number; pageSize: number; onChange: (p: number) => void;
}) {
  const pages: (number | "…")[] = [];
  for (let i = 1; i <= totalPages; i++) {
    if (i === 1 || i === totalPages || Math.abs(i - page) <= 1) {
      pages.push(i);
    } else if (pages[pages.length - 1] !== "…") {
      pages.push("…");
    }
  }
  return (
    <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
      <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginRight: 4, whiteSpace: "nowrap" }}>
        {(page - 1) * pageSize + 1}–{Math.min(page * pageSize, total)} / {total}
      </span>
      <button onClick={() => onChange(page - 1)} disabled={page === 1}
        style={{ ...pageBtnStyle, opacity: page === 1 ? 0.35 : 1 }}>‹</button>
      {pages.map((p, i) =>
        p === "…" ? (
          <span key={`e${i}`} style={{ fontSize: 11, color: "var(--text-muted)", padding: "0 2px" }}>…</span>
        ) : (
          <button key={p} onClick={() => onChange(p as number)}
            style={{ ...pageBtnStyle, background: page === p ? "var(--accent)" : "transparent", color: page === p ? "#fff" : "var(--text-secondary)", borderColor: page === p ? "var(--accent)" : "var(--border-default)" }}>
            {p}
          </button>
        )
      )}
      <button onClick={() => onChange(page + 1)} disabled={page >= totalPages}
        style={{ ...pageBtnStyle, opacity: page >= totalPages ? 0.35 : 1 }}>›</button>
    </div>
  );
}

const SEVERITIES = ["all", "info", "warning", "critical"] as const;
const OUTCOMES   = ["all", "success", "denied", "error"]   as const;

type SevFilter     = typeof SEVERITIES[number];
type OutcomeFilter = typeof OUTCOMES[number];

const filterChipStyle = (active: boolean, color: string): React.CSSProperties => ({
  padding: "3px 10px", borderRadius: 12, fontSize: 10, fontFamily: "var(--font-mono)",
  textTransform: "uppercase", letterSpacing: "0.05em", cursor: "pointer", border: "1px solid",
  borderColor: active ? color : "var(--border-default)",
  background:  active ? color + "22" : "transparent",
  color:       active ? color : "var(--text-muted)",
  fontWeight:  active ? 700 : 400,
  transition: "all 0.12s",
});

function SecurityEventsTab() {
  const { data, loading } = useApi(() => listSecurityEvents({ limit: 500 }));
  const [page, setPage]         = useState(1);
  const [sevFilter, setSev]     = useState<SevFilter>("all");
  const [outFilter, setOut]     = useState<OutcomeFilter>("all");
  const [userFilter, setUser]   = useState("");
  const [typeFilter, setType]   = useState("");

  const allEvents = data ?? [];

  const filtered = allEvents.filter((e: any) => {
    if (sevFilter  !== "all" && e.severity !== sevFilter)  return false;
    if (outFilter  !== "all" && e.outcome  !== outFilter)  return false;
    if (userFilter && !(e.user_id || "").toLowerCase().includes(userFilter.toLowerCase())) return false;
    if (typeFilter && !(e.event_type || "").toLowerCase().includes(typeFilter.toLowerCase())) return false;
    return true;
  });

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const safePage   = Math.min(page, totalPages);
  const pageEvents = filtered.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  const resetPage = () => setPage(1);

  if (loading) return <Spinner size={28} />;
  if (allEvents.length === 0) return <EmptyState title="No security events" description="Events will appear as users interact with the system." />;

  return (
    <Card>
      {/* Filter bar */}
      <div style={{ display: "flex", gap: "var(--space-md)", flexWrap: "wrap", alignItems: "center", marginBottom: "var(--space-md)", paddingBottom: "var(--space-md)", borderBottom: "1px solid var(--border-subtle)" }}>
        {/* Severity */}
        <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
          <span style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginRight: 2 }}>SEV</span>
          {SEVERITIES.map(s => (
            <button key={s} onClick={() => { setSev(s); resetPage(); }}
              style={filterChipStyle(sevFilter === s, s === "critical" ? "var(--status-failed)" : s === "warning" ? "#f7b731" : "var(--accent)")}>
              {s}
            </button>
          ))}
        </div>

        {/* Outcome */}
        <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
          <span style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginRight: 2 }}>OUTCOME</span>
          {OUTCOMES.map(o => (
            <button key={o} onClick={() => { setOut(o); resetPage(); }}
              style={filterChipStyle(outFilter === o, o === "success" ? "var(--status-completed)" : o === "denied" ? "#f7b731" : o === "error" ? "var(--status-failed)" : "var(--accent)")}>
              {o}
            </button>
          ))}
        </div>

        {/* Event type search */}
        <input
          value={typeFilter}
          onChange={e => { setType(e.target.value); resetPage(); }}
          placeholder="Filter event type…"
          style={{ padding: "4px 10px", fontSize: 11, fontFamily: "var(--font-mono)", width: 160,
            background: "var(--bg-input)", border: "1px solid var(--border-default)",
            borderRadius: "var(--radius-sm)", color: "var(--text-primary)" }}
        />

        {/* User search */}
        <input
          value={userFilter}
          onChange={e => { setUser(e.target.value); resetPage(); }}
          placeholder="Filter user…"
          style={{ padding: "4px 10px", fontSize: 11, fontFamily: "var(--font-mono)", width: 140,
            background: "var(--bg-input)", border: "1px solid var(--border-default)",
            borderRadius: "var(--radius-sm)", color: "var(--text-primary)" }}
        />

        {/* Active filter count / clear */}
        {(sevFilter !== "all" || outFilter !== "all" || userFilter || typeFilter) && (
          <button onClick={() => { setSev("all"); setOut("all"); setUser(""); setType(""); resetPage(); }}
            style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", background: "none",
              border: "none", cursor: "pointer", textDecoration: "underline", padding: 0 }}>
            clear filters
          </button>
        )}

        <span style={{ marginLeft: "auto", fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
          {filtered.length} / {allEvents.length}
        </span>
      </div>

      {/* Top pagination */}
      <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", marginBottom: "var(--space-md)" }}>
        {totalPages > 1 && <PaginationBar page={safePage} totalPages={totalPages} total={filtered.length} pageSize={PAGE_SIZE} onChange={setPage} />}
      </div>

      <div style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)", overflow: "hidden" }}>
        <div style={{
          display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr 1fr",
          padding: "8px 16px", background: "var(--bg-elevated)",
          fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase",
        }}>
          <span>Event</span><span>User</span><span>Resource</span><span>Outcome</span><span>When</span>
        </div>
        {pageEvents.map((e: any) => (
          <div key={e.id} style={{
            display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr 1fr",
            padding: "8px 16px", borderTop: "1px solid var(--border-subtle)",
            fontSize: 11, alignItems: "center",
          }}>
            <span style={{ fontFamily: "var(--font-mono)", color: sevColor(e.severity) }}>{e.event_type}</span>
            <span style={{ color: "var(--text-secondary)" }}>{e.user_id || "—"}</span>
            <span style={{ color: "var(--text-secondary)" }}>
              {e.resource_type ? `${e.resource_type}:${(e.resource_id || "").slice(0, 10)}` : "—"}
            </span>
            <span style={{
              fontSize: 9, padding: "2px 6px", borderRadius: 3,
              background: outcomeColor(e.outcome) + "33", color: outcomeColor(e.outcome),
              fontFamily: "var(--font-mono)", textTransform: "uppercase", width: "fit-content",
            }}>{e.outcome}</span>
            <span style={{ color: "var(--text-muted)", fontSize: 10 }}><TimeAgo date={e.timestamp} /></span>
          </div>
        ))}
      </div>

      {filtered.length === 0 && (
        <div style={{ padding: "var(--space-xl)", textAlign: "center", fontSize: 12, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
          No events match the current filters.
        </div>
      )}

      {/* Bottom pagination */}
      {totalPages > 1 && (
        <div style={{ padding: "var(--space-lg) 0", display: "flex", justifyContent: "flex-end" }}>
          <PaginationBar page={safePage} totalPages={totalPages} total={filtered.length} pageSize={PAGE_SIZE} onChange={setPage} />
        </div>
      )}
    </Card>
  );
}

/* ── GDPR ──────────────────────────────────────────────────────── */

function GDPRTab() {
  const { data, loading, refetch } = useApi(listGDPRRequests);
  const requests = data ?? [];
  const [userId, setUserId]           = useState("");
  const [lookup, setLookup]           = useState<{ exists: boolean; canonical_id?: string; counts: Record<string, number> } | null>(null);
  const [lookupBusy, setLookupBusy]   = useState(false);
  const [lookupErr, setLookupErr]     = useState<string | null>(null);
  const [actionBusy, setActionBusy]   = useState(false);
  const [actionMsg, setActionMsg]     = useState<{ text: string; ok: boolean } | null>(null);
  const [showAnonConfirm, setShowAnonConfirm] = useState(false);
  const [resolvingId, setResolvingId] = useState<string | null>(null);

  const handleLookup = async () => {
    if (!userId.trim()) return;
    setLookupBusy(true); setLookupErr(null); setLookup(null); setActionMsg(null);
    try {
      const r = await lookupUserData(userId.trim());
      setLookup({ exists: r.exists, canonical_id: (r as any).canonical_id, counts: r.counts });
    } catch (e: any) {
      setLookupErr(e.message || "Lookup failed");
    } finally { setLookupBusy(false); }
  };

  const effectiveUserId = lookup?.canonical_id ?? userId.trim();

  const handleExport = async () => {
    setActionBusy(true); setActionMsg(null);
    try {
      const resp = await fetch(`/api/v1/enterprise/gdpr/export/${encodeURIComponent(effectiveUserId)}`, {
        headers: { Authorization: `Bearer ${localStorage.getItem("helix_token") ?? ""}` },
      });
      if (!resp.ok) throw new Error(`Server returned ${resp.status}`);
      const blob = await resp.blob();
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement("a");
      a.href = url; a.download = `gdpr-export-${effectiveUserId}.json`;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 200);
      // Backend auto-creates + completes the GDPR request — just refresh history
      setActionMsg({ text: "Export downloaded. GDPR request logged and completed.", ok: true });
      refetch();
    } catch (e: any) {
      setActionMsg({ text: e.message || "Export failed", ok: false });
    } finally { setActionBusy(false); }
  };

  const handleAnonymize = async () => {
    setShowAnonConfirm(false);
    setActionBusy(true); setActionMsg(null);
    try {
      const r = await anonymizeUser(effectiveUserId);
      const summary = Object.entries(r.counts || {}).map(([k, v]) => `${k}: ${v}`).join(", ");
      // Backend auto-creates + completes the GDPR request — just refresh history
      setActionMsg({ text: `Anonymized. ${summary}`, ok: true });
      setLookup(null);
      refetch();
    } catch (e: any) {
      setActionMsg({ text: e.message || "Anonymize failed", ok: false });
    } finally { setActionBusy(false); }
  };

  const handleResolve = async (id: string) => {
    setResolvingId(id);
    try {
      await resolveGDPRRequest(id, "completed");
      refetch();
    } finally { setResolvingId(null); }
  };

  const totalRecords = lookup ? Object.values(lookup.counts).reduce((a, b) => a + b, 0) : 0;

  return (
    <>
      <Card style={{ marginBottom: "var(--space-lg)" }}>
        <div style={{ fontSize: 14, fontWeight: 700, color: "var(--text-primary)", marginBottom: "var(--space-sm)" }}>
          GDPR Data Subject Request
        </div>
        <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: "var(--space-md)", lineHeight: 1.6,
          padding: "var(--space-sm) var(--space-md)", background: "var(--bg-elevated)",
          borderRadius: "var(--radius-sm)", borderLeft: "3px solid var(--accent)" }}>
          This tool covers <strong style={{ color: "var(--text-secondary)" }}>all users</strong> in Velaris —
          both <em>internal operators</em> (admins, agents) and <em>external customers / portal submitters</em>
          whose data is stored in cases, assignments, or audit logs.
          Enter the <strong style={{ color: "var(--text-secondary)" }}>user ID, username, or email</strong> as
          stored in the system. Click <strong style={{ color: "var(--text-secondary)" }}>Lookup</strong> first to
          verify existence before taking action. Every action is permanently logged.
        </div>

        {/* Step 1: Lookup */}
        <div style={{ display: "flex", gap: "var(--space-sm)", alignItems: "center", marginBottom: "var(--space-md)" }}>
          <input
            value={userId}
            onChange={e => { setUserId(e.target.value); setLookup(null); setActionMsg(null); setLookupErr(null); }}
            onKeyDown={e => e.key === "Enter" && handleLookup()}
            placeholder="user_id, email, or username…"
            style={{ flex: 1, padding: "8px 12px", fontSize: 13, background: "var(--bg-input)",
              border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)" }}
          />
          <Button variant="secondary" onClick={handleLookup} disabled={!userId.trim() || lookupBusy}>
            {lookupBusy ? "Looking up…" : "🔍 Lookup"}
          </Button>
        </div>

        {lookupErr && (
          <div style={{ fontSize: 12, color: "var(--status-failed)", marginBottom: "var(--space-md)", fontFamily: "var(--font-mono)" }}>{lookupErr}</div>
        )}

        {/* Step 2: Results */}
        {lookup && (
          <div style={{ marginBottom: "var(--space-md)", padding: "var(--space-md)", borderRadius: "var(--radius-sm)",
            border: `1px solid ${lookup.exists ? "var(--border-default)" : "var(--status-failed)"}`,
            background: lookup.exists ? "var(--bg-elevated)" : "color-mix(in srgb, var(--status-failed) 8%, var(--bg-elevated))" }}>
            {lookup.exists ? (
              <>
                <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", marginBottom: "var(--space-sm)" }}>
                  ✓ User found — {totalRecords} record{totalRecords !== 1 ? "s" : ""} across {Object.keys(lookup.counts).filter(k => (lookup.counts[k] as number) > 0).length} tables
                  {lookup.canonical_id && lookup.canonical_id !== userId.trim() && (
                    <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginLeft: 8 }}>
                      (canonical ID: {lookup.canonical_id})
                    </span>
                  )}
                </div>
                <div style={{ display: "flex", gap: "var(--space-lg)", flexWrap: "wrap" }}>
                  {Object.entries(lookup.counts).map(([k, v]) => (
                    <div key={k} style={{ fontSize: 12 }}>
                      <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{k.replace(/_/g, " ")}: </span>
                      <span style={{ fontWeight: 600, color: (v as number) > 0 ? "var(--text-primary)" : "var(--text-muted)" }}>{v as number}</span>
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <div style={{ fontSize: 13, color: "var(--status-failed)", fontWeight: 600 }}>
                ✗ No data found for "{userId}" — no user profile or activity records exist in this system.
                <div style={{ fontSize: 11, fontWeight: 400, color: "var(--text-muted)", marginTop: 4 }}>
                  Confirm the exact user_id or email as stored in Velaris (check User Directory for the correct value).
                </div>
              </div>
            )}
          </div>
        )}

        {/* Step 3: Actions */}
        {lookup?.exists && (
          <div style={{ display: "flex", gap: "var(--space-sm)", alignItems: "center", flexWrap: "wrap" }}>
            <Button onClick={handleExport} disabled={actionBusy}>
              {actionBusy ? "Working…" : "📥 Export Data (Article 15)"}
            </Button>
            <Button variant="danger" onClick={() => setShowAnonConfirm(true)} disabled={actionBusy}>
              🗑 Anonymize (Article 17)
            </Button>
            <div style={{ fontSize: 10, color: "var(--text-muted)", flex: 1 }}>
              Export downloads a JSON file of all user data. Anonymize replaces the user ID with an
              irreversible hash — preserves audit trail integrity. Both actions are auto-logged.
            </div>
          </div>
        )}

        {actionMsg && (
          <div style={{ marginTop: "var(--space-md)", fontSize: 12, padding: "8px 12px", borderRadius: "var(--radius-sm)",
            background: actionMsg.ok ? "color-mix(in srgb, var(--status-completed) 12%, transparent)" : "color-mix(in srgb, var(--status-failed) 12%, transparent)",
            color: actionMsg.ok ? "var(--status-completed)" : "var(--status-failed)", fontFamily: "var(--font-mono)" }}>
            {actionMsg.ok ? "✓" : "✗"} {actionMsg.text}
          </div>
        )}
      </Card>

      {/* Anonymize confirmation */}
      {showAnonConfirm && (
        <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <div style={{ background: "var(--bg-panel)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-md)", width: 460, padding: "var(--space-xl)" }}>
            <div style={{ fontSize: 16, fontWeight: 700, color: "var(--text-primary)", marginBottom: "var(--space-md)" }}>Confirm Anonymization</div>
            <div style={{ fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.7, marginBottom: "var(--space-lg)" }}>
              This will replace <strong>all occurrences</strong> of <code style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{effectiveUserId}</code> across cases,
              assignments, and audit logs with an irreversible anonymised ID.
              <br /><br />
              <strong style={{ color: "var(--status-failed)" }}>This cannot be undone.</strong> The data is not deleted — the user identity is permanently
              replaced with a hash. A GDPR request record will be logged automatically.
            </div>
            <div style={{ display: "flex", gap: "var(--space-sm)", justifyContent: "flex-end" }}>
              <Button variant="secondary" onClick={() => setShowAnonConfirm(false)}>Cancel</Button>
              <Button variant="danger" onClick={handleAnonymize}>Yes, Anonymize</Button>
            </div>
          </div>
        </div>
      )}

      {/* Request history */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center",
        marginBottom: "var(--space-md)", marginTop: "var(--space-lg)" }}>
        <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)" }}>Request History</div>
        <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
          Every export and anonymize action is permanently logged. Pending = action queued but not yet run by the system.
        </div>
      </div>
      {loading && <Spinner size={28} />}
      {!loading && requests.length === 0 && (
        <EmptyState title="No GDPR requests yet" description="Every export and anonymize action is logged here." />
      )}
      {(requests as any[]).map((r: any) => (
        <Card key={r.id} style={{ marginBottom: "var(--space-sm)" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 13, fontWeight: 600 }}>
                <span style={{ color: r.request_type === "delete" ? "var(--status-failed)" : "var(--accent)",
                  fontFamily: "var(--font-mono)", fontSize: 11, marginRight: 8 }}>{r.request_type}</span>
                {r.subject_id}
              </div>
              <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 2 }}>
                by {r.requested_by || "unknown"} · <TimeAgo date={r.created_at} />
                {r.completed_at && <> · completed <TimeAgo date={r.completed_at} /></>}
              </div>
            </div>
            <div style={{ display: "flex", gap: "var(--space-sm)", alignItems: "center" }}>
              {r.status === "pending" && (
                <Button size="sm" variant="secondary"
                  onClick={() => handleResolve(r.id)}
                  disabled={resolvingId === r.id}>
                  {resolvingId === r.id ? "Resolving…" : "Mark Resolved"}
                </Button>
              )}
              <span style={{ fontSize: 10, padding: "3px 8px", borderRadius: 3,
                background: statusBg(r.status), color: statusColor(r.status),
                fontFamily: "var(--font-mono)", textTransform: "uppercase" }}>
                {r.status}
              </span>
            </div>
          </div>
        </Card>
      ))}
    </>
  );
}

function statusBg(s: string) {
  if (s === "completed") return "color-mix(in srgb, var(--status-completed) 15%, transparent)";
  if (s === "rejected")  return "color-mix(in srgb, var(--status-failed) 15%, transparent)";
  if (s === "in_progress") return "color-mix(in srgb, var(--accent) 15%, transparent)";
  return "var(--bg-elevated)";
}
function statusColor(s: string) {
  if (s === "completed") return "var(--status-completed)";
  if (s === "rejected")  return "var(--status-failed)";
  if (s === "in_progress") return "var(--accent)";
  return "var(--text-muted)";
}

/* ── Retention ─────────────────────────────────────────────────── */

const ACTION_OPTIONS = ["archive", "delete", "anonymize"];

function RetentionTab() {
  const { data, loading, refetch } = useApi(listRetentionPolicies);
  const policies: any[] = data ?? [];
  const [edits, setEdits]   = useState<Record<string, { days: string; action: string }>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [confirmPolicy, setConfirmPolicy] = useState<any | null>(null);

  // Seed edit state for all policies when data arrives
  useEffect(() => {
    if (!policies.length) return;
    setEdits(prev => {
      const next = { ...prev };
      for (const p of policies) {
        if (!next[p.id]) {
          next[p.id] = { days: String(p.retention_days), action: p.action };
        }
      }
      return next;
    });
  }, [data]); // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = async (p: any) => {
    await updateRetentionPolicy(p.id, { enabled: !p.enabled });
    refetch();
  };

  const save = async (p: any) => {
    const e = edits[p.id];
    if (!e) return;
    setSaving(p.id);
    await updateRetentionPolicy(p.id, {
      retention_days: parseInt(e.days) || p.retention_days,
      action: e.action,
    });
    setSaving(null);
    refetch();
  };

  const handleSaveClick = (p: any) => {
    setConfirmPolicy(p);
  };

  const handleConfirmSave = async () => {
    const p = confirmPolicy;
    setConfirmPolicy(null);
    if (p) await save(p);
  };

  if (loading) return <Spinner size={28} />;

  return (
    <>
      <div style={{ padding: "var(--space-sm) var(--space-md)", marginBottom: "var(--space-lg)",
        background: "var(--accent-dim)", border: "1px solid var(--accent)", borderRadius: "var(--radius-sm)",
        fontSize: 12, color: "var(--text-secondary)" }}>
        Retention policies define when old data should be <strong>archived</strong>, <strong>deleted</strong>, or <strong>anonymized</strong>.
        Enable a policy to activate it. Retention period and action can be edited at any time on the card.
      </div>

      {policies.map((p: any) => {
        const e = edits[p.id] ?? { days: String(p.retention_days), action: p.action };
        const isDirty = e.days !== String(p.retention_days) || e.action !== p.action;
        return (
          <Card key={p.id} style={{ marginBottom: "var(--space-sm)", borderLeft: `3px solid ${p.enabled ? "var(--status-completed)" : "var(--border-subtle)"}` }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: "var(--space-md)" }}>
              <div style={{ flex: 1 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                  <span style={{ fontSize: 13, fontWeight: 600 }}>{p.name}</span>
                  <span style={{ fontSize: 9, padding: "1px 6px", borderRadius: 10, fontFamily: "var(--font-mono)", fontWeight: 700,
                    background: p.enabled ? "color-mix(in srgb, var(--status-completed) 15%, transparent)" : "var(--bg-elevated)",
                    color: p.enabled ? "var(--status-completed)" : "var(--text-muted)" }}>
                    {p.enabled ? "ACTIVE" : "DISABLED"}
                  </span>
                </div>
                <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginBottom: "var(--space-md)" }}>
                  Resource: {p.resource_type}
                  {p.last_run_at && ` · last run: ${new Date(p.last_run_at).toLocaleDateString()}`}
                </div>

                {/* Always-editable fields */}
                <div style={{ display: "flex", gap: "var(--space-md)", alignItems: "center", flexWrap: "wrap" }}>
                  <div>
                    <label style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase",
                      letterSpacing: "0.06em", display: "block", marginBottom: 3 }}>Retain (days)</label>
                    <input
                      type="number"
                      min={1}
                      value={e.days}
                      disabled={!p.enabled}
                      onChange={ev => setEdits(prev => ({
                        ...prev,
                        [p.id]: { ...(prev[p.id] ?? { days: String(p.retention_days), action: p.action }), days: ev.target.value },
                      }))}
                      style={{ width: 90, padding: "5px 8px", fontSize: 13, fontFamily: "var(--font-mono)",
                        background: p.enabled ? "var(--bg-input)" : "var(--bg-elevated)",
                        border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)",
                        color: p.enabled ? "var(--text-primary)" : "var(--text-muted)",
                        cursor: p.enabled ? "text" : "not-allowed", opacity: p.enabled ? 1 : 0.6 }}
                    />
                  </div>
                  <div>
                    <label style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase",
                      letterSpacing: "0.06em", display: "block", marginBottom: 3 }}>Action after period</label>
                    <select
                      value={e.action}
                      disabled={!p.enabled}
                      onChange={ev => setEdits(prev => ({
                        ...prev,
                        [p.id]: { ...(prev[p.id] ?? { days: String(p.retention_days), action: p.action }), action: ev.target.value },
                      }))}
                      style={{ padding: "5px 8px", fontSize: 12, fontFamily: "var(--font-mono)",
                        background: p.enabled ? "var(--bg-input)" : "var(--bg-elevated)",
                        border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)",
                        color: p.enabled ? "var(--text-primary)" : "var(--text-muted)",
                        cursor: p.enabled ? "pointer" : "not-allowed", opacity: p.enabled ? 1 : 0.6 }}
                    >
                      {ACTION_OPTIONS.map(a => <option key={a} value={a}>{a}</option>)}
                    </select>
                  </div>
                  {p.enabled && isDirty && (
                    <Button size="sm" onClick={() => handleSaveClick(p)} disabled={saving === p.id} style={{ alignSelf: "flex-end" }}>
                      {saving === p.id ? "Saving…" : "Save Changes"}
                    </Button>
                  )}
                  {p.enabled && !isDirty && (
                    <span style={{ fontSize: 11, color: "var(--status-completed)", alignSelf: "flex-end", fontFamily: "var(--font-mono)" }}>✓ saved</span>
                  )}
                  {!p.enabled && (
                    <span style={{ fontSize: 11, color: "var(--text-muted)", alignSelf: "flex-end", fontFamily: "var(--font-mono)" }}>enable to edit</span>
                  )}
                </div>
              </div>

              <Button variant={p.enabled ? "ghost" : "primary"} size="sm" onClick={() => toggle(p)}>
                {p.enabled ? "Disable" : "Enable"}
              </Button>
            </div>
          </Card>
        );
      })}

      {/* Retention save confirmation modal */}
      {confirmPolicy && (() => {
        const p = confirmPolicy;
        const e = edits[p.id] ?? { days: String(p.retention_days), action: p.action };
        return (
          <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center" }}>
            <div style={{ background: "var(--bg-panel)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-md)", width: 440, padding: "var(--space-xl)" }}>
              <div style={{ fontSize: 16, fontWeight: 700, color: "var(--text-primary)", marginBottom: "var(--space-md)" }}>Confirm Retention Policy Update</div>
              <div style={{ fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.7, marginBottom: "var(--space-md)" }}>
                You are about to update <strong>{p.name}</strong>:
              </div>
              <div style={{ background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)", padding: "var(--space-md)", marginBottom: "var(--space-lg)", fontSize: 12, fontFamily: "var(--font-mono)" }}>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 4, color: "var(--text-muted)", marginBottom: 6, fontSize: 10, textTransform: "uppercase", letterSpacing: "0.06em" }}>
                  <span></span><span>Before</span><span>After</span>
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 4, alignItems: "center" }}>
                  <span style={{ color: "var(--text-muted)" }}>Retain (days)</span>
                  <span style={{ color: "var(--text-secondary)" }}>{p.retention_days}</span>
                  <span style={{ color: e.days !== String(p.retention_days) ? "var(--accent)" : "var(--text-secondary)", fontWeight: e.days !== String(p.retention_days) ? 700 : 400 }}>{e.days}</span>
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 4, alignItems: "center", marginTop: 6 }}>
                  <span style={{ color: "var(--text-muted)" }}>Action</span>
                  <span style={{ color: "var(--text-secondary)" }}>{p.action}</span>
                  <span style={{ color: e.action !== p.action ? "var(--accent)" : "var(--text-secondary)", fontWeight: e.action !== p.action ? 700 : 400 }}>{e.action}</span>
                </div>
              </div>
              <div style={{ display: "flex", gap: "var(--space-sm)", justifyContent: "flex-end" }}>
                <Button variant="secondary" onClick={() => setConfirmPolicy(null)}>Cancel</Button>
                <Button onClick={handleConfirmSave}>Confirm Save</Button>
              </div>
            </div>
          </div>
        );
      })()}
    </>
  );
}

function sevColor(s: string): string {
  return ({ info: "var(--text-primary)", warning: "#f7b731", critical: "var(--status-failed)" } as any)[s] || "var(--text-secondary)";
}

function outcomeColor(o: string): string {
  return ({ success: "var(--status-completed)", denied: "#f7b731", error: "var(--status-failed)" } as any)[o] || "var(--text-muted)";
}
