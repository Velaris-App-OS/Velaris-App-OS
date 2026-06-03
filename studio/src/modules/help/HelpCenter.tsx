/**
 * Knowledge Center — redesigned P40
 * Tabs: Overview · Case Types · Glossary · Ask HxNexus
 */
import React, { useEffect, useRef, useState } from "react";

function authHeaders(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}
async function get<T>(path: string): Promise<T> {
  const r = await fetch(`/api/v1${path}`, { headers: authHeaders() });
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json();
}

// ── Types ─────────────────────────────────────────────────────────────────────

type Overview = {
  platform: string;
  stats: Record<string, number>;
  quick_start: { step: number; action: string; path: string; description: string }[];
};
type StepSummary   = { id: string; name: string; type: string; required: boolean };
type StageSummary  = { id: string; name: string; order: number; step_count: number; required_steps: number; steps: StepSummary[] };
type CaseTypeSummary = {
  id: string; name: string; version: string; description: string; color: string;
  portal_enabled: boolean; stage_count: number; sla_count: number;
  plain_english: string; stages: StageSummary[];
};
type GlossaryTerm = { term: string; definition: string; category: string; meta?: string };

type Tab = "overview" | "casetypes" | "glossary" | "ask";

const CATEGORY_LABELS: Record<string, string> = {
  concept:      "Platform Concepts",
  case_type:    "Case Types",
  portal:       "Portals",
  access_group: "Teams & Groups",
};
const CATEGORY_COLORS: Record<string, string> = {
  concept:      "#3b82f6",
  case_type:    "#7c3aed",
  portal:       "#0d9488",
  access_group: "#f59e0b",
};

const STEP_ICON: Record<string, string> = {
  "Form — operator fills in a form":            "📝",
  "Approval — approve or reject with a reason": "✅",
  "Document — upload required before advancing":"📎",
  "Automated — runs without operator input":    "⚙",
};

// ── Root ──────────────────────────────────────────────────────────────────────

export default function HelpCenter() {
  const [tab, setTab]           = useState<Tab>("overview");
  const [overview, setOverview] = useState<Overview | null>(null);
  const [caseTypes, setCTs]     = useState<CaseTypeSummary[]>([]);
  const [glossary, setGlossary] = useState<GlossaryTerm[]>([]);
  const [loading, setLoading]   = useState(true);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      get<Overview>("/knowledge/overview"),
      get<{ case_types: CaseTypeSummary[] }>("/knowledge/case-types"),
      get<{ glossary: GlossaryTerm[] }>("/knowledge/glossary"),
    ]).then(([ov, cts, gl]) => {
      setOverview(ov);
      setCTs(cts.case_types);
      setGlossary(gl.glossary);
    }).catch(() => {}).finally(() => setLoading(false));
  }, []);

  const TABS: { key: Tab; label: string }[] = [
    { key: "overview",   label: "Overview"   },
    { key: "casetypes",  label: "Case Types" },
    { key: "glossary",   label: "Glossary"   },
    { key: "ask",        label: "Ask HxNexus"},
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden", background: "var(--bg-root)" }}>

      {/* Tab bar */}
      <div style={{ padding: "0 32px", borderBottom: "1px solid var(--border-subtle)", flexShrink: 0, display: "flex", gap: 2 }}>
        {TABS.map(({ key, label }) => (
          <button key={key} onClick={() => setTab(key)} style={{
            padding: "14px 20px", border: "none", cursor: "pointer", fontSize: 13, fontWeight: 500,
            background: "none", color: tab === key ? "var(--accent)" : "var(--text-muted)",
            borderBottom: tab === key ? "2px solid var(--accent)" : "2px solid transparent",
            marginBottom: -1, transition: "color .15s",
          }}>{label}</button>
        ))}
      </div>

      {/* Body */}
      <div style={{ flex: 1, overflow: "auto" }}>
        {loading && tab !== "ask" && (
          <div style={{ padding: 40, fontSize: 13, color: "var(--text-muted)" }}>Loading knowledge base…</div>
        )}
        {!loading && tab === "overview"  && <OverviewTab overview={overview} onNavigate={setTab} />}
        {!loading && tab === "casetypes" && <CaseTypesTab caseTypes={caseTypes} />}
        {!loading && tab === "glossary"  && <GlossaryTab glossary={glossary} />}
        {tab === "ask" && <AskTab />}
      </div>
    </div>
  );
}

// ── Overview Tab ──────────────────────────────────────────────────────────────

function OverviewTab({ overview, onNavigate }: { overview: Overview | null; onNavigate: (t: Tab) => void }) {
  if (!overview) return null;
  const { stats, quick_start } = overview;

  const STAT_CARDS = [
    { label: "Case Types",    value: stats.case_types,    color: "#7c3aed", desc: "Process templates defined" },
    { label: "Active Cases",  value: stats.active_cases,  color: "#0d9488", desc: "In-flight right now" },
    { label: "Forms",         value: stats.forms,         color: "#3b82f6", desc: "Step forms configured" },
    { label: "Access Groups", value: stats.access_groups, color: "#f59e0b", desc: "Teams and roles" },
  ];

  return (
    <div style={{ padding: "32px 40px" }}>

      {/* Intro */}
      <div style={{ marginBottom: 36 }}>
        <h1 style={{ fontSize: 22, fontWeight: 700, color: "var(--text-primary)", margin: "0 0 8px" }}>
          Knowledge Center
        </h1>
        <p style={{ fontSize: 14, color: "var(--text-secondary)", margin: 0, lineHeight: 1.6 }}>
          Your guide to this Velaris platform — browse case types, understand concepts, and ask HxNexus anything.
        </p>
      </div>

      {/* Stats */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))", gap: 14, marginBottom: 40 }}>
        {STAT_CARDS.map(({ label, value, color, desc }) => (
          <div key={label} style={{
            background: "var(--bg-card)", border: "1px solid var(--border-subtle)",
            borderRadius: 12, padding: "20px 18px", borderTop: `3px solid ${color}`,
          }}>
            <div style={{ fontSize: 30, fontWeight: 800, color, lineHeight: 1, marginBottom: 4 }}>{value}</div>
            <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", marginBottom: 2 }}>{label}</div>
            <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{desc}</div>
          </div>
        ))}
      </div>

      {/* Quick start */}
      <h2 style={S.sectionHead}>Getting Started</h2>
      <div style={{ display: "flex", flexDirection: "column", gap: 10, marginBottom: 40 }}>
        {quick_start.map((qs, idx) => (
          <div key={qs.step} style={{
            background: "var(--bg-card)", border: "1px solid var(--border-subtle)",
            borderRadius: 10, padding: "16px 20px", display: "flex", alignItems: "center", gap: 18,
          }}>
            <div style={{
              width: 36, height: 36, borderRadius: "50%", flexShrink: 0,
              background: idx === 0 ? "var(--accent)" : "var(--bg-elevated)",
              color: idx === 0 ? "#fff" : "var(--text-muted)",
              border: idx === 0 ? "none" : "2px solid var(--border-default)",
              display: "flex", alignItems: "center", justifyContent: "center",
              fontWeight: 700, fontSize: 14,
            }}>{qs.step}</div>
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 600, fontSize: 14, color: "var(--text-primary)", marginBottom: 3 }}>{qs.action}</div>
              <div style={{ fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.5 }}>{qs.description}</div>
            </div>
            <a href={qs.path} style={{
              padding: "7px 16px", borderRadius: 8, fontSize: 12, fontWeight: 600, flexShrink: 0,
              background: "var(--accent)", color: "#fff", textDecoration: "none",
            }}>Open →</a>
          </div>
        ))}
      </div>

      {/* Navigation shortcuts */}
      <h2 style={S.sectionHead}>Explore</h2>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))", gap: 12 }}>
        {([
          { tab: "casetypes" as Tab, title: "Case Types", desc: "Understand the processes configured on this platform — stages, steps, and SLAs." },
          { tab: "glossary"  as Tab, title: "Glossary",   desc: "Definitions for every platform concept, term, and entity — searchable and categorised." },
          { tab: "ask"       as Tab, title: "Ask HxNexus",desc: "Get instant answers from the AI copilot — how-to questions, process explanations, guidance." },
        ]).map(({ tab, title, desc }) => (
          <button key={tab} onClick={() => onNavigate(tab)} style={{
            background: "var(--bg-card)", border: "1px solid var(--border-subtle)",
            borderRadius: 10, padding: "18px 20px", textAlign: "left", cursor: "pointer",
            transition: "border-color .15s",
          }}
            onMouseEnter={e => (e.currentTarget.style.borderColor = "var(--accent)")}
            onMouseLeave={e => (e.currentTarget.style.borderColor = "var(--border-subtle)")}
          >
            <div style={{ fontWeight: 700, fontSize: 14, color: "var(--accent)", marginBottom: 6 }}>{title} →</div>
            <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.5 }}>{desc}</div>
          </button>
        ))}
      </div>
    </div>
  );
}

// ── Case Types Tab ────────────────────────────────────────────────────────────

function CaseTypesTab({ caseTypes }: { caseTypes: CaseTypeSummary[] }) {
  const [search, setSearch]       = useState("");
  const [expandedId, setExpanded] = useState<string | null>(null);

  // Extra safety: deduplicate by name in case multiple versions slip through
  const unique = Object.values(
    caseTypes.reduce((acc, ct) => {
      if (!acc[ct.name] || ct.version > acc[ct.name].version) acc[ct.name] = ct;
      return acc;
    }, {} as Record<string, CaseTypeSummary>)
  );

  const filtered = unique.filter(ct =>
    !search
    || ct.name.toLowerCase().includes(search.toLowerCase())
    || ct.plain_english.toLowerCase().includes(search.toLowerCase())
    || ct.description.toLowerCase().includes(search.toLowerCase())
  );

  return (
    <div style={{ padding: "28px 40px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 24 }}>
        <div style={{ flex: 1 }}>
          <h2 style={{ ...S.sectionHead, marginBottom: 4 }}>Case Types</h2>
          <div style={{ fontSize: 12, color: "var(--text-muted)" }}>{caseTypes.length} process{caseTypes.length !== 1 ? "es" : ""} configured on this platform</div>
        </div>
        <input value={search} onChange={e => setSearch(e.target.value)}
          placeholder="Search case types…" style={S.searchInput} />
      </div>

      {filtered.length === 0 && (
        <div style={{ padding: "40px 0", textAlign: "center", color: "var(--text-muted)", fontSize: 13 }}>
          {search ? `No case types match "${search}".` : "No case types defined yet. Create one in Case Designer."}
        </div>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {filtered.map(ct => {
          const isOpen = expandedId === ct.id;
          const accent = ct.color || "var(--accent)";
          return (
            <div key={ct.id} style={{
              background: "var(--bg-card)", border: "1px solid var(--border-subtle)",
              borderRadius: 12, overflow: "hidden",
              borderLeft: `4px solid ${accent}`,
            }}>
              {/* Header row */}
              <button onClick={() => setExpanded(isOpen ? null : ct.id)} style={{
                width: "100%", background: "none", border: "none", cursor: "pointer",
                padding: "18px 20px", textAlign: "left", display: "flex", alignItems: "flex-start", gap: 14,
              }}>
                <div style={{ flex: 1 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: ct.plain_english ? 5 : 0 }}>
                    <span style={{ fontWeight: 700, fontSize: 15, color: "var(--text-primary)" }}>{ct.name}</span>
                    <span style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>v{ct.version}</span>
                    {ct.portal_enabled && (
                      <span style={{ fontSize: 10, padding: "2px 8px", borderRadius: 10, background: "#ede9fe", color: "#7c3aed", fontWeight: 600 }}>
                        Customer Portal
                      </span>
                    )}
                  </div>
                  {ct.plain_english && (
                    <div style={{ fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.55 }}>{ct.plain_english}</div>
                  )}
                </div>
                <div style={{ display: "flex", gap: 14, fontSize: 12, color: "var(--text-muted)", flexShrink: 0, alignItems: "center" }}>
                  <span>{ct.stage_count} stage{ct.stage_count !== 1 ? "s" : ""}</span>
                  {ct.sla_count > 0 && <span>{ct.sla_count} SLA</span>}
                  <span style={{ fontSize: 16 }}>{isOpen ? "▲" : "▼"}</span>
                </div>
              </button>

              {/* Stage pipeline (horizontal) */}
              {isOpen && (
                <div style={{ borderTop: "1px solid var(--border-subtle)", padding: "20px 20px 24px" }}>
                  {/* Stage flow */}
                  <div style={{ display: "flex", gap: 0, marginBottom: 20, overflowX: "auto", paddingBottom: 4 }}>
                    {ct.stages.map((stage, si) => (
                      <React.Fragment key={stage.id}>
                        <div style={{
                          background: "var(--bg-elevated)", border: "1px solid var(--border-subtle)",
                          borderRadius: 8, padding: "10px 14px", minWidth: 120, flexShrink: 0,
                        }}>
                          <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 3, fontFamily: "var(--font-mono)" }}>STAGE {si + 1}</div>
                          <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-primary)" }}>{stage.name}</div>
                          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
                            {stage.step_count} step{stage.step_count !== 1 ? "s" : ""}
                          </div>
                        </div>
                        {si < ct.stages.length - 1 && (
                          <div style={{ display: "flex", alignItems: "center", padding: "0 6px", color: "var(--text-muted)", fontSize: 16 }}>→</div>
                        )}
                      </React.Fragment>
                    ))}
                  </div>

                  {/* Steps detail per stage */}
                  {ct.stages.map((stage, si) => stage.steps.length > 0 && (
                    <div key={stage.id} style={{ marginBottom: 16 }}>
                      <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 8 }}>
                        {si + 1}. {stage.name}
                      </div>
                      <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                        {stage.steps.map(step => (
                          <div key={step.id} style={{
                            display: "flex", alignItems: "center", gap: 10, padding: "7px 12px",
                            background: "var(--bg-elevated)", borderRadius: 6, fontSize: 13,
                          }}>
                            <span style={{ fontSize: 15 }}>{STEP_ICON[step.type] ?? "•"}</span>
                            <span style={{ fontWeight: step.required ? 600 : 400, color: "var(--text-primary)", flex: 1 }}>{step.name}</span>
                            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{step.type}</span>
                            {step.required && (
                              <span style={{ fontSize: 10, padding: "1px 6px", borderRadius: 4, background: "#fee2e2", color: "#dc2626", fontWeight: 700 }}>required</span>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Glossary Tab ──────────────────────────────────────────────────────────────

function GlossaryTab({ glossary }: { glossary: GlossaryTerm[] }) {
  const [search, setSearch]     = useState("");
  const [category, setCategory] = useState<string>("all");

  const categories = ["all", ...Object.keys(CATEGORY_LABELS)];

  // Deduplicate by term within each category — keep last occurrence (live terms override static)
  const deduplicated = Object.values(
    glossary.reduce((acc, g) => {
      const key = `${g.category}::${g.term.toLowerCase()}`;
      acc[key] = g;
      return acc;
    }, {} as Record<string, GlossaryTerm>)
  );

  const filtered = deduplicated
    .filter(g => category === "all" || g.category === category)
    .filter(g =>
      !search
      || g.term.toLowerCase().includes(search.toLowerCase())
      || g.definition.toLowerCase().includes(search.toLowerCase())
    )
    .sort((a, b) => a.term.localeCompare(b.term));

  // Group alphabetically
  const groups: Record<string, GlossaryTerm[]> = {};
  for (const g of filtered) {
    const letter = g.term[0]?.toUpperCase() ?? "#";
    (groups[letter] ??= []).push(g);
  }
  const letters = Object.keys(groups).sort();

  return (
    <div style={{ padding: "28px 40px" }}>
      {/* Header + search */}
      <div style={{ marginBottom: 20 }}>
        <h2 style={{ ...S.sectionHead, marginBottom: 4 }}>Glossary</h2>
        <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 16 }}>
          {glossary.length} terms — sourced live from your platform. Case type definitions update automatically when you edit descriptions.
        </div>

        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" as const, alignItems: "center" }}>
          <input value={search} onChange={e => setSearch(e.target.value)}
            placeholder="Search terms and definitions…" style={{ ...S.searchInput, flex: 1, minWidth: 200 }} />
          {search && (
            <button onClick={() => setSearch("")} style={S.clearBtn}>✕</button>
          )}
        </div>
      </div>

      {/* Category filter chips */}
      <div style={{ display: "flex", gap: 8, marginBottom: 24, flexWrap: "wrap" as const }}>
        {categories.map(cat => {
          const count = cat === "all" ? deduplicated.length : deduplicated.filter(g => g.category === cat).length;
          const isActive = category === cat;
          const color = cat === "all" ? "var(--accent)" : CATEGORY_COLORS[cat];
          return (
            <button key={cat} onClick={() => setCategory(cat)} style={{
              padding: "5px 14px", borderRadius: 20, fontSize: 12, cursor: "pointer",
              border: `1px solid ${isActive ? color : "var(--border-default)"}`,
              background: isActive ? (color + "18") : "transparent",
              color: isActive ? color : "var(--text-muted)",
              fontWeight: isActive ? 700 : 400,
            }}>
              {cat === "all" ? "All" : CATEGORY_LABELS[cat]}{count > 0 ? ` (${count})` : ""}
            </button>
          );
        })}
      </div>

      {filtered.length === 0 && (
        <div style={{ padding: "40px 0", textAlign: "center", color: "var(--text-muted)", fontSize: 13 }}>
          No terms match your search.
        </div>
      )}

      {/* Alphabetical groups */}
      {letters.map(letter => (
        <div key={letter} style={{ marginBottom: 28 }}>
          <div style={{
            fontSize: 11, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase",
            letterSpacing: "0.1em", marginBottom: 10, paddingBottom: 6,
            borderBottom: "1px solid var(--border-subtle)",
          }}>{letter}</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
            {groups[letter].map(g => {
              const catColor = CATEGORY_COLORS[g.category] ?? "var(--text-muted)";
              return (
                <div key={g.term} style={{
                  display: "flex", alignItems: "flex-start", gap: 0,
                  padding: "13px 0", borderBottom: "1px solid var(--border-subtle)",
                }}>
                  <div style={{ width: 220, flexShrink: 0, paddingRight: 20 }}>
                    <div style={{ fontWeight: 600, fontSize: 14, color: "var(--text-primary)", marginBottom: 4 }}>{g.term}</div>
                    <span style={{
                      fontSize: 10, padding: "2px 8px", borderRadius: 10, fontWeight: 600,
                      background: catColor + "18", color: catColor,
                    }}>
                      {CATEGORY_LABELS[g.category] ?? g.category}
                    </span>
                  </div>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 13, color: "var(--text-primary)", lineHeight: 1.6 }}>{g.definition}</div>
                    {g.meta && g.meta !== g.definition && (
                      <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>{g.meta}</div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Ask HxNexus Tab ───────────────────────────────────────────────────────────

const SUGGESTIONS = [
  "How do I create a case type?",
  "What is an access group?",
  "How does SLA escalation work?",
  "What can customers see in the portal?",
  "How does step locking work?",
  "What is the difference between a stage and a step?",
];

function AskTab() {
  const [msg, setMsg]       = useState("");
  const [history, setHistory] = useState<{ role: "user" | "ai"; text: string }[]>([]);
  const [loading, setLoading] = useState(false);
  const [convId, setConvId] = useState<string | null>(null);
  const bottomRef           = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [history, loading]);

  async function send(e?: React.FormEvent) {
    e?.preventDefault();
    if (!msg.trim() || loading) return;
    const text = msg;
    setMsg("");
    setHistory(h => [...h, { role: "user", text }]);
    setLoading(true);
    try {
      const body: Record<string, string> = { message: text };
      if (convId) body.conversation_id = convId;
      const r = await fetch("/api/v1/hxnexus/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(body),
      });
      if (r.ok) {
        const d = await r.json();
        if (d.conversation_id) setConvId(d.conversation_id);
        setHistory(h => [...h, { role: "ai", text: d.reply || d.message || "No response." }]);
      } else {
        setHistory(h => [...h, { role: "ai", text: "HxNexus is unavailable right now." }]);
      }
    } catch {
      setHistory(h => [...h, { role: "ai", text: "HxNexus is unavailable right now." }]);
    } finally { setLoading(false); }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", maxWidth: 800, width: "100%", margin: "0 auto", padding: "0 32px", boxSizing: "border-box" }}>

      {/* Context banner */}
      <div style={{
        margin: "24px 0 16px", padding: "14px 18px", borderRadius: 10,
        background: "var(--bg-card)", border: "1px solid var(--border-subtle)",
        fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.6,
      }}>
        <strong style={{ color: "var(--text-primary)" }}>HxNexus</strong> knows your platform — ask about any case type, process, concept, or feature. Answers are grounded in your live configuration.
      </div>

      {/* Suggestions (shown only when no history) */}
      {history.length === 0 && !loading && (
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 11, color: "var(--text-muted)", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 10 }}>Suggested questions</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {SUGGESTIONS.map(q => (
              <button key={q} onClick={() => { setMsg(q); }}
                style={{
                  padding: "7px 14px", borderRadius: 20, fontSize: 12, cursor: "pointer",
                  border: "1px solid var(--border-default)", background: "var(--bg-elevated)",
                  color: "var(--text-secondary)", transition: "border-color .15s",
                }}
                onMouseEnter={e => (e.currentTarget.style.borderColor = "var(--accent)")}
                onMouseLeave={e => (e.currentTarget.style.borderColor = "var(--border-default)")}
              >{q}</button>
            ))}
          </div>
        </div>
      )}

      {/* Chat history */}
      <div style={{ flex: 1, overflow: "auto", display: "flex", flexDirection: "column", gap: 12, paddingBottom: 16 }}>
        {history.map((m, i) => (
          <div key={i} style={{
            maxWidth: "82%", alignSelf: m.role === "user" ? "flex-end" : "flex-start",
          }}>
            {m.role === "ai" && (
              <div style={{ fontSize: 10, fontWeight: 700, color: "var(--accent)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 5 }}>HxNexus</div>
            )}
            <div style={{
              padding: "12px 16px", borderRadius: m.role === "user" ? "16px 16px 4px 16px" : "16px 16px 16px 4px",
              fontSize: 13, lineHeight: 1.65,
              background: m.role === "user" ? "var(--accent)" : "var(--bg-card)",
              color: m.role === "user" ? "#fff" : "var(--text-primary)",
              border: m.role === "ai" ? "1px solid var(--border-subtle)" : "none",
              whiteSpace: "pre-wrap",
            }}>{m.text}</div>
          </div>
        ))}
        {loading && (
          <div style={{ alignSelf: "flex-start", maxWidth: "82%" }}>
            <div style={{ fontSize: 10, fontWeight: 700, color: "var(--accent)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 5 }}>HxNexus</div>
            <div style={{ padding: "12px 16px", background: "var(--bg-card)", border: "1px solid var(--border-subtle)", borderRadius: "16px 16px 16px 4px", fontSize: 13, color: "var(--text-muted)" }}>
              <ThinkingDots />
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <form onSubmit={send} style={{ display: "flex", gap: 10, padding: "12px 0 24px", flexShrink: 0, borderTop: "1px solid var(--border-subtle)" }}>
        <input
          value={msg} onChange={e => setMsg(e.target.value)}
          placeholder="Ask anything about this platform…"
          style={{ ...S.searchInput, flex: 1, fontSize: 13 }}
          autoFocus
        />
        <button type="submit" disabled={loading || !msg.trim()} style={{
          padding: "10px 22px", background: "var(--accent)", color: "#fff",
          border: "none", borderRadius: 8, cursor: "pointer", fontWeight: 600, fontSize: 13,
          opacity: loading || !msg.trim() ? 0.5 : 1,
        }}>Send</button>
      </form>
    </div>
  );
}

// Animated thinking dots
function ThinkingDots() {
  const [dots, setDots] = useState(".");
  useEffect(() => {
    const t = setInterval(() => setDots(d => d.length >= 3 ? "." : d + "."), 500);
    return () => clearInterval(t);
  }, []);
  return <span>Thinking{dots}</span>;
}

// ── Shared styles ─────────────────────────────────────────────────────────────

const S = {
  sectionHead: {
    fontSize: 15, fontWeight: 700, color: "var(--text-primary)",
    margin: "0 0 12px",
  } as React.CSSProperties,
  searchInput: {
    padding: "9px 14px", border: "1px solid var(--border-default)", borderRadius: 8,
    fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)",
    fontFamily: "var(--font-body)", outline: "none",
  } as React.CSSProperties,
  clearBtn: {
    padding: "9px 12px", border: "1px solid var(--border-default)", borderRadius: 8,
    fontSize: 12, cursor: "pointer", background: "var(--bg-elevated)", color: "var(--text-muted)",
  } as React.CSSProperties,
};
