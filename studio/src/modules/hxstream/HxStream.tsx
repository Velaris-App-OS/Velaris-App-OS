import React, { useState, useEffect, useRef, useCallback } from "react";
import { hxStream, type TraceEvent, type StreamEntry, type StreamGap } from "@shared/realtime/hxstream-singleton";
import { useAuth } from "@/auth/AuthContext";

/* ═══════════════════════════════════════════════════════════════════
   HxStream — Live Execution & Interaction Stream (P46)
   ═══════════════════════════════════════════════════════════════════ */

const API   = "/api/v1/hxstream";
const CASES = "/api/v1/cases";

function authHdr() {
  const t = localStorage.getItem("helix_token") ?? "";
  return { Authorization: `Bearer ${t}` };
}

const EVENT_TYPE_COLORS: Record<string, string> = {
  stage_transition:  "#6c8ef7",
  step_complete:     "#4caf7d",
  lock_acquire:      "#e8a838",
  lock_release:      "#a0a0b8",
  ui_interaction:    "#b07cf7",
  ai_invoke:         "#00bcd4",
  rule_eval:         "#ff7043",
  notification_sent: "#26c6da",
  queue_route:       "#78909c",
  automation_run:    "#66bb6a",
  form_submit:       "#ab47bc",
  integration_call:  "#ef5350",
  "api.post":        "#42a5f5",
  "api.patch":       "#7e57c2",
  "api.delete":      "#ef5350",
  "auth.login":      "#26a69a",
  "auth.login_failed": "#f44336",
  error:             "#f44336",
};

function isGap(e: StreamEntry): e is StreamGap {
  return (e as StreamGap).kind === "gap";
}

/* ── Actor search dropdown ────────────────────────────────────────── */

interface Actor { user_id: string; display_name?: string | null; email?: string | null; label?: string }

function ActorDropdown({
  value, onChange, isAdmin,
}: { value: string; onChange: (v: string) => void; isAdmin: boolean }) {
  const [actors, setActors]   = useState<Actor[]>([]);
  const [query, setQuery]     = useState(value);
  const [open, setOpen]       = useState(false);
  const [loading, setLoading] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const h = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);

  const search = useCallback(async (q: string) => {
    setLoading(true);
    try {
      const qs = q ? `?q=${encodeURIComponent(q)}` : "";
      const r  = await fetch(`${API}/actors${qs}`, { headers: authHdr() });
      if (r.ok) {
        const data = await r.json();
        setActors(data.actors ?? []);
      }
    } finally { setLoading(false); }
  }, []);

  useEffect(() => {
    if (open) search(query);
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  const select = (a: Actor) => {
    onChange(a.user_id);
    setQuery(a.label ?? a.display_name ?? a.user_id);
    setOpen(false);
  };

  const clear = () => { onChange(""); setQuery(""); setActors([]); };

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
        <input
          value={query}
          readOnly={!isAdmin}
          placeholder={isAdmin ? "Filter by actor…" : "Your events only"}
          onClick={() => isAdmin && setOpen(true)}
          onChange={e => { setQuery(e.target.value); search(e.target.value); setOpen(true); }}
          style={{
            ...filterStyle, width: 200,
            cursor: isAdmin ? "text" : "not-allowed",
            opacity: isAdmin ? 1 : 0.7,
          }}
        />
        {value && isAdmin && (
          <button onClick={clear} style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", fontSize: 12, padding: "0 2px" }}>✕</button>
        )}
      </div>
      {open && isAdmin && (
        <div style={{
          position: "absolute", top: "100%", left: 0, width: 280, zIndex: 200,
          background: "var(--bg-card)", border: "1px solid var(--border)",
          borderRadius: 4, boxShadow: "0 4px 12px rgba(0,0,0,0.2)", maxHeight: 240, overflowY: "auto", marginTop: 2,
        }}>
          {loading && <div style={{ padding: "8px 12px", fontSize: 12, color: "var(--text-muted)" }}>Searching…</div>}
          {!loading && actors.length === 0 && (
            <div style={{ padding: "8px 12px", fontSize: 12, color: "var(--text-muted)" }}>No actors found</div>
          )}
          {actors.map(a => (
            <div key={a.user_id} onMouseDown={() => select(a)} style={{
              padding: "7px 12px", cursor: "pointer", borderBottom: "1px solid var(--border)",
              fontSize: 12,
            }}
              onMouseEnter={e => (e.currentTarget.style.background = "var(--bg-surface)")}
              onMouseLeave={e => (e.currentTarget.style.background = "transparent")}
            >
              <div style={{ fontWeight: 600, color: "var(--text-primary)", fontFamily: "var(--font-mono)" }}>{a.user_id}</div>
              {(a.display_name || a.email) && (
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{a.display_name}{a.email ? ` · ${a.email}` : ""}</div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ── Case ID autocomplete ─────────────────────────────────────────── */

function CaseDropdown({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [cases, setCases]   = useState<{ id: string; case_number?: string; status?: string }[]>([]);
  const [open, setOpen]     = useState(false);
  const [query, setQuery]   = useState(value);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const h = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);

  const search = useCallback(async (q: string) => {
    try {
      const qs = new URLSearchParams({ limit: "20" });
      if (q) qs.set("q", q);
      const r = await fetch(`${CASES}?${qs}`, { headers: authHdr() });
      if (r.ok) {
        const data = await r.json();
        setCases((data.items ?? data ?? []).slice(0, 20));
      }
    } catch { /* ignore */ }
  }, []);

  useEffect(() => { if (open) search(query); }, [open]); // eslint-disable-line

  const select = (c: { id: string; case_number?: string }) => {
    onChange(c.id);
    setQuery(c.case_number ? `${c.case_number} (${c.id.slice(0, 8)}…)` : c.id);
    setOpen(false);
  };

  const clear = () => { onChange(""); setQuery(""); setCases([]); };

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
        <input
          value={query}
          placeholder="Filter by case…"
          onClick={() => setOpen(true)}
          onChange={e => { setQuery(e.target.value); search(e.target.value); setOpen(true); }}
          style={{ ...filterStyle, width: 220 }}
        />
        {value && (
          <button onClick={clear} style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", fontSize: 12, padding: "0 2px" }}>✕</button>
        )}
      </div>
      {open && (
        <div style={{
          position: "absolute", top: "100%", left: 0, width: 300, zIndex: 200,
          background: "var(--bg-card)", border: "1px solid var(--border)",
          borderRadius: 4, boxShadow: "0 4px 12px rgba(0,0,0,0.2)", maxHeight: 240, overflowY: "auto", marginTop: 2,
        }}>
          {cases.length === 0 && (
            <div style={{ padding: "8px 12px", fontSize: 12, color: "var(--text-muted)" }}>No cases found</div>
          )}
          {cases.map((c: any) => (
            <div key={c.id} onMouseDown={() => select(c)} style={{
              padding: "7px 12px", cursor: "pointer", borderBottom: "1px solid var(--border)", fontSize: 12,
            }}
              onMouseEnter={e => (e.currentTarget.style.background = "var(--bg-surface)")}
              onMouseLeave={e => (e.currentTarget.style.background = "transparent")}
            >
              <div style={{ fontWeight: 600, color: "var(--text-primary)", fontFamily: "var(--font-mono)" }}>
                {c.case_number || c.id.slice(0, 12) + "…"}
              </div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
                {c.status ?? ""}{c.created_by ? ` · by ${c.created_by}` : ""}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ── Main component ───────────────────────────────────────────────── */

export default function HxStream() {
  const { user } = useAuth();
  const isAdmin  = user?.is_admin ?? false;

  const [entries, setEntries]         = useState<StreamEntry[]>(() => hxStream.getBuffer());
  const [connected, setConnected]     = useState(() => hxStream.isConnected());
  const [paused, setPaused]           = useState(false);
  const [filterType, setFilterType]   = useState("");
  const [filterCase, setFilterCase]   = useState("");
  const [filterActor, setFilterActor] = useState(() => isAdmin ? "" : (user?.user_id ?? ""));
  const [selected, setSelected]       = useState<TraceEvent | null>(null);
  const [tab, setTab]                 = useState<"live" | "history">("live");
  const [historyEvents, setHistoryEvents]   = useState<TraceEvent[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const bufferRef  = useRef<StreamEntry[]>([]);
  const pausedRef  = useRef(false);

  useEffect(() => { pausedRef.current = paused; }, [paused]);

  // If non-admin, always lock actor filter to own user
  useEffect(() => {
    if (!isAdmin && user?.user_id) setFilterActor(user.user_id);
  }, [isAdmin, user?.user_id]);

  useEffect(() => {
    const unStatus = hxStream.onStatus(setConnected);
    const unEvent  = hxStream.onEvent((event) => {
      // Client-side scope enforcement (belt-and-suspenders with backend)
      if (!isAdmin && event.actor_user_id && event.actor_user_id !== user?.user_id) return;
      if (pausedRef.current) {
        bufferRef.current = [event, ...bufferRef.current].slice(0, 200);
      } else {
        setEntries(prev => [event, ...prev].slice(0, 500));
      }
    });
    return () => { unStatus(); unEvent(); };
  }, [isAdmin, user?.user_id]);

  const resume = useCallback(() => {
    setPaused(false);
    setEntries(prev => [...bufferRef.current, ...prev].slice(0, 500));
    bufferRef.current = [];
  }, []);

  const loadHistory = useCallback(async () => {
    setHistoryLoading(true);
    try {
      const params = new URLSearchParams({ limit: "200" });
      if (filterType)  params.set("event_type",    filterType);
      if (filterCase)  params.set("case_id",        filterCase);
      // Always send actor filter (backend enforces scope for non-admins)
      const actor = isAdmin ? filterActor : (user?.user_id ?? "");
      if (actor) params.set("actor_user_id", actor);
      const r = await fetch(`${API}/events?${params}`, { headers: authHdr() });
      if (r.ok) setHistoryEvents((await r.json()).events ?? []);
    } finally { setHistoryLoading(false); }
  }, [filterType, filterCase, filterActor, isAdmin, user?.user_id]);

  useEffect(() => { if (tab === "history") loadHistory(); }, [tab]);

  // Live-tab filter (case/actor filtering on already-received events)
  const displayEntries = (tab === "live" ? entries : historyEvents).filter((e) => {
    if (isGap(e)) return !filterType && !filterCase && !filterActor;
    if (filterType  && e.event_type    !== filterType)  return false;
    if (filterCase  && e.case_id       !== filterCase)  return false;
    const actorFilter = isAdmin ? filterActor : (user?.user_id ?? "");
    if (actorFilter && e.actor_user_id !== actorFilter) return false;
    return true;
  });

  const scopeLabel = isAdmin
    ? "Viewing all events (admin)"
    : `Viewing your events only (${user?.user_id ?? "—"})`;

  return (
    <div style={{ display: "flex", height: "100%", fontFamily: "var(--font-mono, monospace)", fontSize: 13 }}>

      {/* ── Main panel ── */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>

        {/* Header */}
        <div style={{ padding: "14px 20px", borderBottom: "1px solid var(--border)", display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ fontWeight: 700, fontSize: 15, color: "var(--text-primary)" }}>HxStream</span>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 11, color: connected ? "#4caf7d" : "#f59e0b" }}>
            <span style={{ width: 7, height: 7, borderRadius: "50%", background: connected ? "#4caf7d" : "#f59e0b", display: "inline-block" }} />
            {connected ? "LIVE" : "RECONNECTING…"}
          </span>
          <span style={{ fontSize: 10, color: isAdmin ? "var(--accent)" : "var(--text-muted)", padding: "2px 8px", borderRadius: 3, background: isAdmin ? "var(--accent-dim)" : "var(--bg-elevated)", fontFamily: "var(--font-mono)" }}>
            {scopeLabel}
          </span>

          {(["live", "history"] as const).map(t => (
            <button key={t} onClick={() => setTab(t)} style={{
              padding: "3px 10px", borderRadius: 4, border: "1px solid var(--border)",
              background: tab === t ? "var(--accent)" : "transparent",
              color: tab === t ? "#fff" : "var(--text-muted)", cursor: "pointer", fontSize: 12,
            }}>{t === "live" ? "Live" : "History"}</button>
          ))}

          <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
            {tab === "live" && (
              paused
                ? <button onClick={resume} style={btnStyle("#4caf7d")}>▶ Resume ({bufferRef.current.length} buffered)</button>
                : <button onClick={() => setPaused(true)} style={btnStyle("var(--text-muted)")}>⏸ Pause</button>
            )}
            {tab === "history" && (
              <button onClick={loadHistory} style={btnStyle("var(--accent)")}>↻ Refresh</button>
            )}
            <button onClick={() => setEntries([])} style={btnStyle("var(--text-muted)")}>✕ Clear</button>
          </div>
        </div>

        {/* Filters */}
        <div style={{ padding: "8px 20px", borderBottom: "1px solid var(--border)", display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <select value={filterType} onChange={e => setFilterType(e.target.value)} style={filterStyle}>
            <option value="">All event types</option>
            {Object.keys(EVENT_TYPE_COLORS).map(t => <option key={t} value={t}>{t}</option>)}
          </select>

          <CaseDropdown value={filterCase} onChange={v => {
            setFilterCase(v);
            if (tab === "history") setTimeout(loadHistory, 50);
          }} />

          <ActorDropdown value={filterActor} onChange={v => {
            setFilterActor(v);
            if (tab === "history") setTimeout(loadHistory, 50);
          }} isAdmin={isAdmin} />

          {(filterType || filterCase || (isAdmin && filterActor)) && (
            <button onClick={() => { setFilterType(""); setFilterCase(""); if (isAdmin) setFilterActor(""); }}
              style={{ ...btnStyle("var(--text-muted)"), fontSize: 11 }}>
              Clear filters
            </button>
          )}
        </div>

        {/* Event list */}
        <div style={{ flex: 1, overflow: "auto" }}>
          {historyLoading && <div style={{ padding: 20, color: "var(--text-muted)" }}>Loading…</div>}
          {displayEntries.length === 0 && !historyLoading && (
            <div style={{ padding: 40, color: "var(--text-muted)" }}>
              {tab === "live" ? "Waiting for events…" : "No events found."}
            </div>
          )}
          {displayEntries.map(entry =>
            isGap(entry)
              ? <GapRow key={entry.id} gap={entry} />
              : <EventRow key={entry.id} event={entry} selected={selected?.id === entry.id} onClick={() => setSelected(entry)} />
          )}
        </div>
      </div>

      {/* ── Detail panel ── */}
      {selected && (
        <div style={{ width: 360, borderLeft: "1px solid var(--border)", display: "flex", flexDirection: "column", overflow: "hidden" }}>
          <div style={{ padding: "12px 16px", borderBottom: "1px solid var(--border)", display: "flex", justifyContent: "space-between" }}>
            <span style={{ fontWeight: 600, color: "var(--text-primary)" }}>Event Detail</span>
            <button onClick={() => setSelected(null)} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)" }}>✕</button>
          </div>
          <div style={{ flex: 1, overflow: "auto", padding: 16 }}>
            <DetailRow label="Type"    value={<TypeBadge type={selected.event_type} />} />
            <DetailRow label="Time"    value={fmtTime(selected.occurred_at)} />
            <DetailRow label="Actor"   value={selected.actor_user_id ?? "—"} />
            <DetailRow label="Case"    value={selected.case_id ?? "—"} />
            <DetailRow label="Latency" value={selected.latency_ms != null ? `${selected.latency_ms} ms` : "—"} />
            <DetailRow label="Session" value={selected.session_id ?? "—"} />
            {selected.actor_ip && <DetailRow label="IP" value={selected.actor_ip} />}
            <div style={{ marginTop: 12 }}>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4 }}>PAYLOAD</div>
              <pre style={{
                background: "var(--bg-surface)", border: "1px solid var(--border)",
                borderRadius: 4, padding: 10, fontSize: 11, overflow: "auto",
                color: "var(--text-primary)", margin: 0,
              }}>{JSON.stringify(selected.payload, null, 2)}</pre>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ── Sub-components ─────────────────────────────────────────────────── */

function GapRow({ gap }: { gap: StreamGap }) {
  const secs = Math.round(gap.durationMs / 1000);
  const label = secs < 60 ? `${secs}s` : `${Math.round(secs / 60)}m ${secs % 60}s`;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "5px 20px", borderBottom: "1px solid var(--border)", background: "#f59e0b11" }}>
      <span style={{ width: 3, height: 24, background: "#f59e0b", borderRadius: 2, flexShrink: 0 }} />
      <span style={{ fontSize: 10, color: "#f59e0b", fontWeight: 700 }}>SESSION GAP</span>
      <span style={{ fontSize: 10, color: "var(--text-muted)" }}>
        {label} disconnected · {fmtTime(gap.from)} → {fmtTime(gap.to)}
      </span>
    </div>
  );
}

function EventRow({ event, selected, onClick }: { event: TraceEvent; selected: boolean; onClick: () => void }) {
  const color = EVENT_TYPE_COLORS[event.event_type] ?? "#888";
  return (
    <div onClick={onClick} style={{
      display: "flex", alignItems: "center", gap: 10, padding: "6px 20px",
      borderBottom: "1px solid var(--border)",
      background: selected ? "var(--bg-surface)" : "transparent",
      cursor: "pointer",
    }}>
      <span style={{ width: 3, height: 32, background: color, borderRadius: 2, flexShrink: 0 }} />
      <span style={{ width: 120, flexShrink: 0, fontSize: 11, color: "var(--text-muted)" }}>{fmtTime(event.occurred_at)}</span>
      <TypeBadge type={event.event_type} />
      <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--text-primary)" }}>
        {event.actor_user_id && <span style={{ color: "var(--text-muted)" }}>{event.actor_user_id} · </span>}
        {event.case_id && <span style={{ color: "var(--text-muted)" }}>{event.case_id.slice(0, 8)}… · </span>}
        {summarise(event)}
      </span>
      {event.latency_ms != null && (
        <span style={{ fontSize: 10, color: event.latency_ms > 500 ? "#f44336" : "var(--text-muted)", flexShrink: 0 }}>
          {event.latency_ms}ms
        </span>
      )}
    </div>
  );
}

function TypeBadge({ type }: { type: string }) {
  const color = EVENT_TYPE_COLORS[type] ?? "#888";
  return (
    <span style={{
      fontSize: 10, padding: "2px 6px", borderRadius: 3, flexShrink: 0,
      background: color + "22", color, border: `1px solid ${color}55`, fontWeight: 600,
    }}>
      {type.replace(/_/g, " ")}
    </span>
  );
}

function DetailRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
      <span style={{ width: 70, fontSize: 11, color: "var(--text-muted)", flexShrink: 0 }}>{label}</span>
      <span style={{ flex: 1, fontSize: 12, color: "var(--text-primary)", wordBreak: "break-all" }}>{value}</span>
    </div>
  );
}

/* ── Helpers ────────────────────────────────────────────────────────── */

function summarise(ev: TraceEvent): string {
  const p = ev.payload as Record<string, unknown>;
  switch (ev.event_type) {
    case "stage_transition":  return `${p.from_stage ?? "?"} → ${p.to_stage ?? "?"}`;
    case "step_complete":     return `step ${p.step_id ?? "?"} → ${p.status ?? "?"}`;
    case "lock_acquire":      return `locked ${p.step_id ?? "?"}`;
    case "lock_release":      return `released ${p.step_id ?? "?"}`;
    case "ui_interaction":    return `${p.action ?? "click"}`;
    case "ai_invoke":         return `${p.model ?? "?"} (${p.tokens ?? "?"} tok)`;
    case "error":             return String(p.msg ?? p.message ?? "error");
    case "auth.login":        return `login · ${p.username ?? "?"}`;
    case "auth.login_failed": return `FAILED login · ${p.username ?? "?"}`;
    default: {
      const method = p.method as string | undefined;
      const path   = p.path as string | undefined;
      if (method && path) return `${method} ${path}`;
      return JSON.stringify(p).slice(0, 60);
    }
  }
}

function fmtTime(iso: string): string {
  try {
    const d = new Date(iso);
    const t = d.toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
    return `${t}.${String(d.getMilliseconds()).padStart(3, "0")}`;
  } catch { return iso; }
}

const filterStyle: React.CSSProperties = {
  padding: "4px 8px", borderRadius: 4, border: "1px solid var(--border)",
  background: "var(--bg-surface)", color: "var(--text-primary)", fontSize: 12,
};

function btnStyle(color: string): React.CSSProperties {
  return {
    padding: "4px 10px", borderRadius: 4, border: `1px solid ${color}`,
    background: "transparent", color, cursor: "pointer", fontSize: 12,
  };
}
