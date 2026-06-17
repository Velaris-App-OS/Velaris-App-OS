/**
 * P58 — HxDocs: Living Documentation
 * Block-based editor with AI generation, live data embeds, version history, search.
 */
import React, { useState, useEffect, useCallback, useRef } from "react";
import { Button } from "@shared/components";
import { AiUnavailableBanner } from "@shared/components/AiUnavailableBanner";

const API = "/api/v1/hxdocs";

function authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}

async function hxFetch(url: string, opts: RequestInit = {}): Promise<Response> {
  return fetch(url, {
    ...opts,
    headers: { "Content-Type": "application/json", ...authHdr(), ...(opts.headers || {}) },
  });
}

// ── Types ─────────────────────────────────────────────────────────────────────

interface Space { id: string; name: string; slug: string; description?: string; is_public: boolean; created_at: string; }
interface Article { id: string; space_id: string; title: string; slug: string; status: string; is_public: boolean; auto_generated: boolean; source_concept?: string; word_count: number; version: number; tags: string[]; created_by?: string; updated_by?: string; created_at: string; updated_at: string; }
interface ArticleDetail extends Article { content: Block[]; }
interface Block { id: string; type: string; level?: number; text?: string; language?: string; embed_type?: string; case_type_id?: string; concept?: string; label?: string; _live?: any; }
interface Version { id: string; version: number; title: string; saved_by?: string; saved_at: string; }

// ── Styles ────────────────────────────────────────────────────────────────────

const S: Record<string, React.CSSProperties> = {
  page:     { height: "100%", display: "flex", flexDirection: "column", overflow: "hidden" },
  topbar:   { padding: "var(--space-md) var(--space-2xl)", borderBottom: "1px solid var(--border-subtle)", display: "flex", alignItems: "center", gap: 12, flexShrink: 0 },
  title:    { fontSize: 15, fontWeight: 600, margin: 0, color: "var(--text-primary)" },
  body:     { flex: 1, display: "flex", overflow: "hidden" },
  sidebar:  { width: 240, borderRight: "1px solid var(--border-subtle)", background: "var(--bg-elevated)", display: "flex", flexDirection: "column", flexShrink: 0, overflow: "hidden" },
  content:  { flex: 1, overflow: "auto", padding: "var(--space-xl) var(--space-2xl)", width: "100%" },
  input:    { width: "100%", padding: "7px 10px", border: "1px solid var(--border-default)", borderRadius: 4, fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" as const, marginBottom: 8 },
  badge:    { fontSize: 10, padding: "2px 8px", borderRadius: 4, fontWeight: 700, textTransform: "uppercase" as const },
};

const STATUS_COLOR: Record<string, string> = { draft: "#94a3b8", published: "#22c55e" };
const BLOCK_TYPES = ["heading", "paragraph", "callout", "code", "live_data"];

// ── Live Data Block ───────────────────────────────────────────────────────────

const EMBED_TYPES: { value: string; label: string; description: string; fields: { key: string; label: string; placeholder: string; hint: string }[] }[] = [
  {
    value: "case_count",
    label: "Case Count",
    description: "Shows a live count of cases, optionally filtered to one case type.",
    fields: [
      { key: "label",        label: "Display label",  placeholder: "e.g. Active Claims",    hint: "Text shown next to the number" },
      { key: "case_type_id", label: "Case Type ID",   placeholder: "(leave blank for all)", hint: "UUID of a specific case type, or blank to count all cases" },
    ],
  },
  {
    value: "case_status_breakdown",
    label: "Case Status Breakdown",
    description: "Shows a live breakdown of case counts by status (open / in_progress / resolved / closed).",
    fields: [
      { key: "case_type_id", label: "Case Type ID", placeholder: "(leave blank for all)", hint: "Scope to a specific case type, or leave blank for platform-wide" },
    ],
  },
  {
    value: "graph_node",
    label: "Graph Node",
    description: "Embeds a live card from the HxGraph knowledge graph — name, type, and summary auto-update as the platform evolves.",
    fields: [
      { key: "concept", label: "Concept name", placeholder: "e.g. Insurance Claim, HxBridge", hint: "Partial name match — picks the closest node from HxGraph" },
    ],
  },
];

function LiveDataBlock({ block, live, onUpdate }: { block: Block; live: any; onUpdate: (b: Block) => void }) {
  const [configuring, setConfiguring] = useState(!block.embed_type || block.embed_type === "case_count" && !block.label);
  const [draft, setDraft] = useState<Record<string, string>>({
    embed_type:    block.embed_type    ?? "case_count",
    label:         block.label         ?? "",
    case_type_id:  block.case_type_id  ?? "",
    concept:       block.concept       ?? "",
  });

  const [resolving, setResolving] = useState(false);
  const selected = EMBED_TYPES.find(e => e.value === draft.embed_type) ?? EMBED_TYPES[0];

  const apply = async () => {
    const updated: Block = {
      ...block,
      embed_type:   draft.embed_type,
      label:        draft.label        || undefined,
      case_type_id: draft.case_type_id || undefined,
      concept:      draft.concept      || undefined,
      _live:        undefined,  // clear stale live data
    };
    setConfiguring(false);
    // Re-fetch live data for just this block immediately after config change
    setResolving(true);
    try {
      const r = await hxFetch(`${API}/resolve-block`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ block: updated }),
      });
      onUpdate(r.ok ? await r.json() : updated);
    } catch {
      onUpdate(updated);
    } finally {
      setResolving(false);
    }
  };

  if (configuring) {
    return (
      <div style={{ border: "2px solid var(--accent)", borderRadius: 8, padding: 16, background: "var(--accent-dim)" }}>
      <AiUnavailableBanner featureName="AI article generation" />

        <div style={{ fontSize: 11, color: "#818cf8", fontWeight: 700, textTransform: "uppercase" as const, marginBottom: 12 }}>⚡ Configure Live Data Block</div>

        {/* Embed type selector */}
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-muted)", marginBottom: 8 }}>What do you want to embed?</div>
          <div style={{ display: "flex", flexDirection: "column" as const, gap: 8 }}>
            {EMBED_TYPES.map(et => (
              <label key={et.value} style={{ display: "flex", gap: 10, padding: "10px 12px", borderRadius: 6, cursor: "pointer", border: `2px solid ${draft.embed_type === et.value ? "var(--accent)" : "var(--border-subtle)"}`, background: draft.embed_type === et.value ? "var(--accent-dim)" : "var(--bg-elevated)" }}>
                <input type="radio" name="embed_type" value={et.value} checked={draft.embed_type === et.value} onChange={() => setDraft(d => ({ ...d, embed_type: et.value }))} style={{ marginTop: 2, flexShrink: 0 }} />
                <div>
                  <div style={{ fontSize: 13, fontWeight: 700 }}>{et.label}</div>
                  <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>{et.description}</div>
                </div>
              </label>
            ))}
          </div>
        </div>

        {/* Fields for selected embed type */}
        {selected.fields.length > 0 && (
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-muted)", marginBottom: 8 }}>Configuration</div>
            {selected.fields.map(f => (
              <div key={f.key} style={{ marginBottom: 10 }}>
                <label style={{ fontSize: 12, fontWeight: 600, display: "block", marginBottom: 3 }}>{f.label}</label>
                <input
                  style={{ width: "100%", padding: "6px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" as const }}
                  placeholder={f.placeholder}
                  value={draft[f.key] ?? ""}
                  onChange={e => setDraft(d => ({ ...d, [f.key]: e.target.value }))}
                />
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 3 }}>{f.hint}</div>
              </div>
            ))}
          </div>
        )}

        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button style={{ padding: "7px 16px", background: "var(--accent)", color: "#fff", border: "none", borderRadius: 4, cursor: "pointer", fontSize: 13, fontWeight: 600 }} onClick={apply}>Apply & Fetch</button>
          {block.embed_type && <button style={{ padding: "7px 12px", background: "none", border: "1px solid var(--border-default)", borderRadius: 4, cursor: "pointer", fontSize: 13, color: "var(--text-muted)" }} onClick={() => setConfiguring(false)}>Cancel</button>}
        </div>
      </div>
    );
  }

  if (resolving) {
    return (
      <div style={{ border: "1px solid var(--border-subtle)", borderRadius: 8, padding: "16px", background: "var(--accent-dim)", fontSize: 13, color: "var(--accent)" }}>
        ⚡ Fetching live data…
      </div>
    );
  }

  // Resolved display
  return (
    <div style={{ border: "1px solid var(--border-subtle)", borderRadius: 8, padding: "12px 16px", background: "var(--accent-dim)", cursor: "default" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <div style={{ fontSize: 10, color: "#818cf8", fontWeight: 700, textTransform: "uppercase" as const }}>
          ⚡ {selected.label}
        </div>
        <button style={{ fontSize: 11, padding: "2px 8px", borderRadius: 3, border: "1px solid var(--border-default)", background: "none", cursor: "pointer", color: "var(--accent)" }} onClick={() => setConfiguring(true)}>
          Edit
        </button>
      </div>

      {/* Config summary */}
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 8 }}>
        {selected.fields.map(f => {
          const val = draft[f.key];
          return val ? <span key={f.key} style={{ marginRight: 12 }}>{f.label}: <strong style={{ color: "var(--text-primary)" }}>{val}</strong></span> : null;
        })}
        {!selected.fields.some(f => draft[f.key]) && <span>No parameters set — click Edit to configure</span>}
      </div>

      {/* Live data output */}
      {block.embed_type === "case_count" && live && (
        <div style={{ fontSize: 32, fontWeight: 700, color: "var(--text-primary)" }}>
          {live.count} <span style={{ fontSize: 14, fontWeight: 400, color: "var(--text-muted)" }}>{block.label || "cases"}</span>
        </div>
      )}
      {block.embed_type === "case_status_breakdown" && live?.breakdown && (
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap" as const }}>
          {Object.entries(live.breakdown).map(([s, c]: any) => (
            <div key={s} style={{  }}>
              <div style={{ fontSize: 24, fontWeight: 700 }}>{c}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", textTransform: "capitalize" as const }}>{s.replace("_", " ")}</div>
            </div>
          ))}
        </div>
      )}
      {block.embed_type === "graph_node" && live && (
        <div>
          <div style={{ fontSize: 14, fontWeight: 700 }}>{live.label ?? live.name}</div>
          <div style={{ fontSize: 11, color: "#818cf8", marginBottom: 4 }}>{live.node_type}</div>
          {live.summary && <div style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.5 }}>{live.summary}</div>}
        </div>
      )}
      {!live && (
        <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "8px 0" }}>
          Live values load automatically each time the article is opened. To preview immediately, click <strong>Edit → Apply &amp; Fetch</strong> above, or use <strong>🔄 Force Sync</strong> in the toolbar to refresh all live blocks at once.
        </div>
      )}
    </div>
  );
}

// ── Block Renderer ────────────────────────────────────────────────────────────

function BlockView({ block, onUpdate, onDelete, onMoveUp, onMoveDown }: {
  block: Block;
  onUpdate: (b: Block) => void;
  onDelete: () => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(block.text ?? "");

  const save = () => { setEditing(false); onUpdate({ ...block, text: draft }); };

  const controls = (
    <div style={{ display: "flex", gap: 4, opacity: 0, transition: "opacity 0.1s" }} className="block-controls">
      <button style={{ fontSize: 11, padding: "2px 6px", borderRadius: 3, border: "1px solid var(--border-subtle)", background: "var(--bg-elevated)", cursor: "pointer", color: "var(--text-muted)" }} onClick={onMoveUp}>↑</button>
      <button style={{ fontSize: 11, padding: "2px 6px", borderRadius: 3, border: "1px solid var(--border-subtle)", background: "var(--bg-elevated)", cursor: "pointer", color: "var(--text-muted)" }} onClick={onMoveDown}>↓</button>
      <button style={{ fontSize: 11, padding: "2px 6px", borderRadius: 3, border: "none", background: "#ef444422", cursor: "pointer", color: "#ef4444" }} onClick={onDelete}>✕</button>
    </div>
  );

  const wrapper = (children: React.ReactNode) => (
    <div
      style={{ position: "relative", marginBottom: 8 }}
      onMouseEnter={e => { const c = (e.currentTarget as HTMLElement).querySelector<HTMLElement>(".block-controls"); if (c) c.style.opacity = "1"; }}
      onMouseLeave={e => { const c = (e.currentTarget as HTMLElement).querySelector<HTMLElement>(".block-controls"); if (c) c.style.opacity = "0"; }}
    >
      <div style={{ position: "absolute", right: 0, top: 0, zIndex: 10 }}>{controls}</div>
      {children}
    </div>
  );

  if (block.type === "heading") {
    const Tag = (`h${block.level ?? 1}`) as keyof JSX.IntrinsicElements;
    const fs = [32, 24, 20][( block.level ?? 1) - 1] ?? 20;
    if (editing) return wrapper(
      <div>
        <input style={{ ...S.input, fontSize: fs, fontWeight: 700 }} autoFocus value={draft} onChange={e => setDraft(e.target.value)} onBlur={save} onKeyDown={e => e.key === "Enter" && save()} />
      </div>
    );
    return wrapper(<div style={{ fontSize: fs, fontWeight: 700, color: "var(--text-primary)", padding: "4px 0", cursor: "text" }} onClick={() => setEditing(true)}>{block.text || "Click to edit heading"}</div>);
  }

  if (block.type === "paragraph") {
    if (editing) return wrapper(
      <textarea style={{ ...S.input, minHeight: 80, resize: "vertical" as const, lineHeight: 1.7 }} autoFocus value={draft} onChange={e => setDraft(e.target.value)} onBlur={save} />
    );
    return wrapper(
      <div style={{ fontSize: 15, lineHeight: 1.7, color: "var(--text-primary)", whiteSpace: "pre-wrap", cursor: "text", padding: "4px 0", minHeight: 28 }} onClick={() => setEditing(true)}>
        {block.text || <span style={{ color: "var(--text-muted)" }}>Click to write…</span>}
      </div>
    );
  }

  if (block.type === "callout") {
    if (editing) return wrapper(
      <textarea style={{ ...S.input, minHeight: 60, background: "#3b82f622", borderColor: "#3b82f6" }} autoFocus value={draft} onChange={e => setDraft(e.target.value)} onBlur={save} />
    );
    return wrapper(
      <div style={{ background: "#3b82f611", border: "1px solid #3b82f644", borderLeft: "4px solid #3b82f6", borderRadius: 6, padding: "12px 16px", cursor: "text", fontSize: 14, lineHeight: 1.6 }} onClick={() => setEditing(true)}>
        <span style={{ marginRight: 8 }}>💡</span>{block.text || "Click to add note…"}
      </div>
    );
  }

  if (block.type === "code") {
    if (editing) return wrapper(
      <textarea style={{ ...S.input, fontFamily: "var(--font-mono)", fontSize: 13, minHeight: 120, background: "#0f172a", color: "#e2e8f0", borderColor: "#334155" }} autoFocus value={draft} onChange={e => setDraft(e.target.value)} onBlur={save} />
    );
    return wrapper(
      <pre style={{ background: "#0f172a", color: "#e2e8f0", padding: "14px 16px", borderRadius: 8, fontSize: 13, overflowX: "auto", cursor: "text", margin: 0 }} onClick={() => setEditing(true)}>
        <code>{block.text || "// click to edit code"}</code>
      </pre>
    );
  }

  if (block.type === "live_data") {
    const live = block._live;
    return wrapper(<LiveDataBlock block={block} live={live} onUpdate={onUpdate} />);
  }

  return wrapper(<div style={{ fontSize: 13, color: "var(--text-muted)", padding: 8, border: "1px dashed var(--border)" }}>Unknown block type: {block.type}</div>);
}

// ── Article Editor ────────────────────────────────────────────────────────────

function ArticleEditor({ article: initial, spaceId, onBack, onRefresh, autoSyncedNotify }: {
  article: ArticleDetail;
  spaceId: string;
  onBack: () => void;
  onRefresh: (fresh: ArticleDetail) => void;
  autoSyncedNotify?: boolean;
}) {
  // NOTE: blocks/title are initialized from initial ONCE on mount.
  // Any update that changes content must go through onRefresh() so SpaceView
  // updates activeArticle, the key changes, and this component remounts clean.
  const [article, setArticle]       = useState(initial);
  const [blocks, setBlocks]         = useState<Block[]>(initial.content ?? []);
  const [title, setTitle]           = useState(initial.title);
  const [saving, setSaving]         = useState(false);
  const [syncing, setSyncing]       = useState(false);
  const [versions, setVersions]     = useState<Version[]>([]);
  const [showVersions, setShowVersions] = useState(false);
  const [addingBlock, setAddingBlock]   = useState(false);

  const save = useCallback(async (saveVersion = false) => {
    setSaving(true);
    const r = await hxFetch(`${API}/articles/${article.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, content: blocks, save_version: saveVersion }),
    });
    if (r.ok) setArticle(a => ({ ...a, ...(r as any) }));
    setSaving(false);
  }, [article.id, title, blocks]);

  const publish = async (isPublic: boolean) => {
    const r = await hxFetch(`${API}/articles/${article.id}/publish`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_public: isPublic }),
    });
    if (r.ok) setArticle(a => ({ ...a, status: "published", is_public: isPublic }));
  };

  const loadVersions = async () => {
    const r = await hxFetch(`${API}/articles/${article.id}/versions`);
    if (r.ok) setVersions(await r.json());
    setShowVersions(true);
  };

  const syncLifecycle = async () => {
    setSyncing(true);
    const r = await hxFetch(`${API}/articles/${article.id}/regenerate-lifecycle`, { method: "POST" });
    if (r.ok) {
      const fresh = await r.json();
      onRefresh(fresh);  // update parent → key changes → clean remount with new content
    }
    setSyncing(false);
  };

  const updateBlock = (idx: number, b: Block) => setBlocks(prev => prev.map((x, i) => i === idx ? b : x));
  const deleteBlock = (idx: number) => setBlocks(prev => prev.filter((_, i) => i !== idx));
  const moveUp   = (idx: number) => setBlocks(prev => { if (idx === 0) return prev; const a = [...prev]; [a[idx-1], a[idx]] = [a[idx], a[idx-1]]; return a; });
  const moveDown = (idx: number) => setBlocks(prev => { if (idx === prev.length-1) return prev; const a = [...prev]; [a[idx], a[idx+1]] = [a[idx+1], a[idx]]; return a; });

  const addBlock = (type: string) => {
    const id = `b${Date.now()}`;
    let block: Block = { id, type };
    if (type === "heading") block = { ...block, level: 2, text: "" };
    else if (type === "paragraph") block = { ...block, text: "" };
    else if (type === "callout") block = { ...block, text: "" };
    else if (type === "code") block = { ...block, language: "python", text: "" };
    else if (type === "live_data") block = { ...block, embed_type: "case_count", label: "Cases" };
    setBlocks(prev => [...prev, block]);
    setAddingBlock(false);
  };

  const sc = STATUS_COLOR[article.status] ?? "#94a3b8";

  return (
    <div style={S.page}>
      <div style={S.topbar}>
        <button onClick={onBack} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 20 }}>←</button>
        <input
          style={{ fontSize: 20, fontWeight: 700, border: "none", background: "transparent", color: "var(--text-primary)", outline: "none", flex: 1 }}
          value={title}
          onChange={e => setTitle(e.target.value)}
        />
        <span style={{ ...S.badge, background: sc + "22", color: sc }}>{article.status}</span>
        {article.auto_generated && <span style={{ ...S.badge, background: "var(--accent-dim)", color: "var(--accent)" }}>AI</span>}
        <div style={{ display: "flex", gap: 8, marginLeft: 8, alignItems: "center" }}>
          <span style={{ fontSize: 10, color: "#22c55e", background: "#22c55e18", border: "1px solid #22c55e44", padding: "2px 8px", borderRadius: 10, fontWeight: 700, letterSpacing: "0.04em" }} title="Live data blocks resolve automatically on open">⚡ LIVE</span>
          {autoSyncedNotify && (
            <span style={{ fontSize: 11, color: "#22c55e", background: "#22c55e11", border: "1px solid #22c55e44", padding: "2px 10px", borderRadius: 12 }}>
              ✓ Auto-updated from case type
            </span>
          )}
          {article.auto_generated && (
            <button
              style={{ padding: "4px 10px", borderRadius: 4, border: "1px solid var(--border-default)", background: syncing ? "var(--accent-dim)" : "none", cursor: "pointer", fontSize: 12, color: "var(--accent)" }}
              onClick={syncLifecycle}
              disabled={syncing}
              title="Manually force a sync with the current case type definition"
            >
              {syncing ? "Syncing…" : "🔄 Force Sync"}
            </button>
          )}
          <Button onClick={() => save(false)} disabled={saving}>{saving ? "Committing…" : "Commit"}</Button>
          <Button onClick={() => save(true)} disabled={saving}>Commit + Version</Button>
          {article.status !== "published"
            ? <Button onClick={() => publish(false)}>Publish</Button>
            : <button style={{ ...S.input, width: "auto", marginBottom: 0, padding: "4px 10px", cursor: "pointer", fontSize: 12 }} onClick={() => publish(!article.is_public)}>{article.is_public ? "Make Private" : "Make Public"}</button>
          }
          <button style={{ ...S.input, width: "auto", marginBottom: 0, padding: "4px 10px", cursor: "pointer", fontSize: 12 }} onClick={loadVersions}>History</button>
        </div>
      </div>

      <div style={{ flex: 1, overflow: "auto" }}>
        <div style={{ maxWidth: 860, margin: "0 auto", padding: "40px 72px" }}>
          {/* Meta bar */}
          <div style={{ display: "flex", gap: 12, marginBottom: 32, fontSize: 12, color: "var(--text-muted)" }}>
            <span>v{article.version}</span>
            <span>·</span>
            <span>{article.word_count} words</span>
            {article.source_concept && <><span>·</span><span>📡 {article.source_concept}</span></>}
            <span>·</span>
            <span>Updated {new Date(article.updated_at).toLocaleDateString()}</span>
          </div>

          {/* Blocks */}
          {blocks.map((block, idx) => (
            <BlockView
              key={block.id}
              block={block}
              onUpdate={b => updateBlock(idx, b)}
              onDelete={() => deleteBlock(idx)}
              onMoveUp={() => moveUp(idx)}
              onMoveDown={() => moveDown(idx)}
            />
          ))}

          {/* Add block */}
          <div style={{ marginTop: 24 }}>
            {addingBlock ? (
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" as const }}>
                {BLOCK_TYPES.map(t => (
                  <button key={t} style={{ padding: "6px 14px", borderRadius: 6, border: "1px solid var(--border-default)", background: "var(--bg-elevated)", cursor: "pointer", fontSize: 12, color: "var(--text-primary)" }} onClick={() => addBlock(t)}>
                    + {t.replace("_", " ")}
                  </button>
                ))}
                <button style={{ padding: "6px 14px", borderRadius: 6, border: "1px solid var(--border-default)", background: "none", cursor: "pointer", fontSize: 12, color: "var(--text-muted)" }} onClick={() => setAddingBlock(false)}>Cancel</button>
              </div>
            ) : (
              <button
                style={{ width: "100%", padding: "10px", border: "2px dashed var(--border-subtle)", borderRadius: 8, background: "none", cursor: "pointer", fontSize: 13, color: "var(--text-muted)" as const }}
                onClick={() => setAddingBlock(true)}
              >
                + Add block
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Version history drawer */}
      {showVersions && (
        <div style={{ position: "fixed", inset: 0, background: "#00000066", zIndex: 1000, display: "flex", justifyContent: "flex-end" }}>
          <div style={{ width: 340, background: "var(--bg-card)", borderLeft: "1px solid var(--border-default)", padding: 24, overflowY: "auto" as const }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 16 }}>
              <div style={{ fontWeight: 700 }}>Version History</div>
              <button onClick={() => setShowVersions(false)} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 18 }}>×</button>
            </div>
            <div style={{ fontSize: 12, padding: "10px 12px", border: "1px solid var(--border-default)", borderRadius: 6, background: "var(--accent-dim)", marginBottom: 8 }}>
              <div style={{ fontWeight: 700 }}>v{article.version} (current)</div>
              <div style={{ color: "var(--text-muted)" }}>Updated by {article.updated_by}</div>
            </div>
            {versions.map(v => (
              <div key={v.id} style={{ fontSize: 12, padding: "10px 12px", border: "1px solid var(--border-subtle)", borderRadius: 6, marginBottom: 8 }}>
                <div style={{ fontWeight: 700 }}>v{v.version} — {v.title}</div>
                <div style={{ color: "var(--text-muted)" }}>{v.saved_by} · {new Date(v.saved_at).toLocaleString()}</div>
              </div>
            ))}
            {versions.length === 0 && <div style={{ fontSize: 12, color: "var(--text-muted)" }}>No committed versions yet. Use "Commit + Version" to snapshot.</div>}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Space View ────────────────────────────────────────────────────────────────

function SpaceView({ space, onBack }: { space: Space; onBack: () => void }) {
  const [articles, setArticles]     = useState<Article[]>([]);
  const [loading, setLoading]       = useState(true);
  const [activeArticle, setActive]  = useState<ArticleDetail | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [showGenerate, setShowGenerate] = useState(false);
  const [showLifecycle, setShowLifecycle] = useState(false);
  const [newTitle, setNewTitle]     = useState("");
  const [genConcept, setGenConcept] = useState("");
  const [generating, setGenerating] = useState(false);
  const [filterStatus, setFilterStatus] = useState("");
  const [searchQ, setSearchQ]       = useState("");
  const [caseTypes, setCaseTypes]   = useState<{id:string;name:string}[]>([]);
  const [selectedCtId, setSelectedCtId] = useState("");

  const LAST_KEY = `hxdocs_last_${space.id}`;
  const activeArticleRef = useRef<ArticleDetail | null>(null);
  const [syncedBanner, setSyncedBanner] = useState(false);
  useEffect(() => { activeArticleRef.current = activeArticle; }, [activeArticle]);

  const load = async () => {
    setLoading(true);
    const qs = filterStatus ? `?status=${filterStatus}` : "";
    const r = await hxFetch(`${API}/spaces/${space.id}/articles${qs}`);
    if (r.ok) setArticles(await r.json());
    setLoading(false);
  };

  const openArticle = async (a: Article | { id: string }) => {
    const r = await hxFetch(`${API}/articles/${a.id}?resolve_live=true`);
    if (!r.ok) return;
    const detail = await r.json();
    sessionStorage.setItem(LAST_KEY, a.id);  // remember for auto-restore
    setActive(detail);
  };

  useEffect(() => { load(); }, [filterStatus]);

  // On mount: restore last-open article (catches return from Case Designer etc.)
  useEffect(() => {
    const lastId = sessionStorage.getItem(LAST_KEY);
    if (lastId) openArticle({ id: lastId });
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  // When tab regains visibility, refresh list AND re-open last article if any
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      load();
      const lastId = sessionStorage.getItem(LAST_KEY);
      if (lastId) openArticle({ id: lastId });
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [filterStatus]);  // eslint-disable-line react-hooks/exhaustive-deps

  // Staleness polling — runs in SpaceView so it survives same-tab SPA navigation.
  // Every 10 s: if there's an active lifecycle article, check if DB has a newer version.
  // If yes, re-fetch and replace activeArticle → key changes → ArticleEditor remounts clean.
  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      if (cancelled) return;
      const cur = activeArticleRef.current;
      if (!cur?.auto_generated) return;
      try {
        const r = await hxFetch(`${API}/articles/${cur.id}`, { cache: "no-store" });
        if (!r.ok || cancelled) return;
        const meta = await r.json();
        if (new Date(meta.updated_at) <= new Date(cur.updated_at)) return;
        // Newer version in DB — re-fetch full content and replace
        const full = await hxFetch(`${API}/articles/${cur.id}?resolve_live=true`, { cache: "no-store" });
        if (!full.ok || cancelled) return;
        const fresh = await full.json();
        setActive(fresh);   // key changes → ArticleEditor remounts with new initial
        setSyncedBanner(true);
        setTimeout(() => setSyncedBanner(false), 5000);
      } catch { /* ignore */ }
    };
    const t = setInterval(poll, 10_000);
    return () => { cancelled = true; clearInterval(t); };
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  const createArticle = async () => {
    if (!newTitle.trim()) return;
    const r = await hxFetch(`${API}/spaces/${space.id}/articles`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: newTitle, content: [
        { id: "b1", type: "heading", level: 1, text: newTitle },
        { id: "b2", type: "paragraph", text: "" },
      ]}),
    });
    if (r.ok) { setShowCreate(false); setNewTitle(""); await load(); }
  };

  const loadCaseTypes = async () => {
    const r = await hxFetch("/api/v1/case-types?page_size=100");
    if (r.ok) {
      const data = await r.json();
      setCaseTypes(Array.isArray(data) ? data : (data.items ?? []));
    }
  };

  const generateLifecycle = async () => {
    if (!selectedCtId) return;
    setGenerating(true);
    const r = await hxFetch(`${API}/generate-lifecycle`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ case_type_id: selectedCtId, space_id: space.id }),
    });
    if (r.ok) {
      const data = await r.json();
      setShowLifecycle(false); setSelectedCtId("");
      setActive(data);
    }
    setGenerating(false);
  };

  const generateArticle = async () => {
    if (!genConcept.trim()) return;
    setGenerating(true);
    const r = await hxFetch(`${API}/generate`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ concept: genConcept, space_id: space.id }),
    });
    if (r.ok) {
      const data = await r.json();
      setShowGenerate(false); setGenConcept("");
      setActive(data);
    }
    setGenerating(false);
  };

  const deleteArticle = async (id: string) => {
    if (!confirm("Delete this article?")) return;
    await hxFetch(`${API}/articles/${id}`, { method: "DELETE" });
    await load();
  };

  const filtered = searchQ
    ? articles.filter(a => a.title.toLowerCase().includes(searchQ.toLowerCase()))
    : articles;

  if (activeArticle) {
    return (
      <ArticleEditor
        key={`${activeArticle.id}-v${activeArticle.version}`}
        article={activeArticle}
        spaceId={space.id}
        onBack={() => { sessionStorage.removeItem(LAST_KEY); setActive(null); load(); }}
        onRefresh={fresh => setActive(fresh)}
        autoSyncedNotify={syncedBanner}
      />
    );
  }

  return (
    <div style={S.page}>
      <div style={S.topbar}>
        <button onClick={onBack} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 20 }}>←</button>
        <h1 style={S.title}>{space.name}</h1>
        {space.is_public && <span style={{ ...S.badge, background: "#22c55e22", color: "#22c55e" }}>Public</span>}
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <Button onClick={() => { setShowLifecycle(true); setShowGenerate(false); setShowCreate(false); loadCaseTypes(); }}>📋 Lifecycle Guide</Button>
          <Button onClick={() => { setShowGenerate(true); setShowLifecycle(false); setShowCreate(false); }}>✨ AI Generate</Button>
          <Button onClick={() => { setShowCreate(true); setShowGenerate(false); setShowLifecycle(false); }}>+ New Article</Button>
        </div>
      </div>

      <div style={S.body}>
        {/* Sidebar filters */}
        <div style={S.sidebar}>
          <div style={{ padding: "var(--space-sm) var(--space-md)", borderBottom: "1px solid var(--border-subtle)" }}>
            <input style={S.input} placeholder="Search articles…" value={searchQ} onChange={e => setSearchQ(e.target.value)} />
          </div>
          <div style={{ padding: "8px 16px" }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", marginBottom: 8 }}>Filter by Status</div>
            {["", "draft", "published"].map(s => (
              <div key={s} style={{ padding: "6px 8px", borderRadius: 4, cursor: "pointer", fontSize: 13, background: filterStatus === s ? "var(--bg-elevated)" : "none", color: "var(--text-primary)", marginBottom: 2 }}
                onClick={() => setFilterStatus(s)}>
                {s === "" ? "All" : s.charAt(0).toUpperCase() + s.slice(1)}
              </div>
            ))}
          </div>
          <div style={{ padding: "8px 16px", marginTop: 8 }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", marginBottom: 8 }}>Stats</div>
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>{articles.length} articles</div>
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>{articles.filter(a => a.auto_generated).length} AI-generated</div>
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>{articles.filter(a => a.status === "published").length} published</div>
          </div>
        </div>

        {/* Article list */}
        <div style={{ flex: 1, overflow: "auto", padding: 24 }}>
          {(showCreate || showGenerate || showLifecycle) && (
            <div style={{ background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: 8, padding: 16, marginBottom: 24, maxWidth: 520 }}>
              {showCreate && (
                <>
                  <div style={{ fontWeight: 700, marginBottom: 12 }}>New Article</div>
                  <input style={S.input} placeholder="Article title" value={newTitle} onChange={e => setNewTitle(e.target.value)} onKeyDown={e => e.key === "Enter" && createArticle()} autoFocus />
                  <div style={{ display: "flex", gap: 8 }}>
                    <Button onClick={createArticle}>Create</Button>
                    <button style={{ padding: "6px 12px", borderRadius: 4, border: "1px solid var(--border-default)", background: "none", cursor: "pointer", fontSize: 12 }} onClick={() => setShowCreate(false)}>Cancel</button>
                  </div>
                </>
              )}
              {showGenerate && (
                <>
                  <div style={{ fontWeight: 700, marginBottom: 4 }}>✨ AI Generate Article</div>
                  <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>Enter a concept, case type, or module name. HxNexus will write the article from HxGraph data.</div>
                  <input style={S.input} placeholder="e.g. Insurance Claim, HxBridge, Mortgage Workflow" value={genConcept} onChange={e => setGenConcept(e.target.value)} onKeyDown={e => e.key === "Enter" && generateArticle()} autoFocus />
                  <div style={{ display: "flex", gap: 8 }}>
                    <Button onClick={generateArticle} disabled={generating}>{generating ? "Generating…" : "Generate"}</Button>
                    <button style={{ padding: "6px 12px", borderRadius: 4, border: "1px solid var(--border-default)", background: "none", cursor: "pointer", fontSize: 12 }} onClick={() => setShowGenerate(false)}>Cancel</button>
                  </div>
                </>
              )}
              {showLifecycle && (
                <>
                  <div style={{ fontWeight: 700, marginBottom: 4 }}>📋 Generate Lifecycle Guide</div>
                  <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
                    Reads every stage, step, form, and field from a case type and writes a complete human-readable narrative explaining what happens at each point in the lifecycle.
                  </div>
                  <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>Select a Case Type</div>
                  {caseTypes.length === 0 ? (
                    <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>Loading case types…</div>
                  ) : (
                    <select
                      style={{ ...S.input, marginBottom: 12 }}
                      value={selectedCtId}
                      onChange={e => setSelectedCtId(e.target.value)}
                    >
                      <option value="">— choose a case type —</option>
                      {caseTypes.map((ct: any) => (
                        <option key={ct.id} value={ct.id}>{ct.name}</option>
                      ))}
                    </select>
                  )}
                  <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 12, background: "var(--bg-elevated)", padding: "8px 12px", borderRadius: 6 }}>
                    The article will include: overview · each stage with description · each step with type &amp; assignment · all form fields with labels, types, required/optional status, and descriptions · transition notes between stages · live case count embed.
                  </div>
                  <div style={{ display: "flex", gap: 8 }}>
                    <Button onClick={generateLifecycle} disabled={generating || !selectedCtId}>{generating ? "Generating…" : "Generate Guide"}</Button>
                    <button style={{ padding: "6px 12px", borderRadius: 4, border: "1px solid var(--border-default)", background: "none", cursor: "pointer", fontSize: 12 }} onClick={() => setShowLifecycle(false)}>Cancel</button>
                  </div>
                </>
              )}
            </div>
          )}

          {loading ? (
            <div style={{ color: "var(--text-muted)", padding: 40 }}>Loading…</div>
          ) : filtered.length === 0 ? (
            <div style={{ color: "var(--text-muted)", padding: 60 }}>
              <div style={{ fontSize: 36, marginBottom: 12 }}>📄</div>
              <div style={{ fontSize: 15, fontWeight: 600 }}>No articles yet</div>
              <div style={{ fontSize: 13, marginTop: 4 }}>Create one or use AI Generate to write from HxGraph</div>
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {filtered.map(a => {
                const sc = STATUS_COLOR[a.status] ?? "#94a3b8";
                return (
                  <div key={a.id} style={{ background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: 8, padding: "14px 16px", cursor: "pointer", display: "flex", alignItems: "center", gap: 12 }}
                    onClick={() => openArticle(a)}>
                    <div style={{ flex: 1 }}>
                      <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 3 }}>{a.title}</div>
                      <div style={{ fontSize: 11, color: "var(--text-muted)", display: "flex", gap: 8 }}>
                        <span>v{a.version}</span>
                        <span>·</span>
                        <span>{a.word_count}w</span>
                        {a.source_concept && <><span>·</span><span>📡 {a.source_concept}</span></>}
                        <span>·</span>
                        <span>{new Date(a.updated_at).toLocaleDateString()}</span>
                      </div>
                    </div>
                    <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                      <span style={{ ...S.badge, background: sc + "22", color: sc }}>{a.status}</span>
                      {a.auto_generated && <span style={{ ...S.badge, background: "#0d948822", color: "#818cf8" }}>AI</span>}
                      {a.is_public && <span style={{ ...S.badge, background: "#22c55e22", color: "#22c55e" }}>Public</span>}
                      <button style={{ padding: "3px 8px", borderRadius: 4, border: "none", background: "#ef444422", color: "#ef4444", cursor: "pointer", fontSize: 11 }}
                        onClick={e => { e.stopPropagation(); deleteArticle(a.id); }}>×</button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Root: Space List ──────────────────────────────────────────────────────────

export default function HxDocs() {
  const [spaces, setSpaces]         = useState<Space[]>([]);
  const [loading, setLoading]       = useState(true);
  const [activeSpace, setActiveSpace] = useState<Space | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm]             = useState({ name: "", description: "", is_public: false });
  const [searchQ, setSearchQ]       = useState("");
  const [searchResults, setSearchResults] = useState<Article[]>([]);

  const load = async () => {
    setLoading(true);
    const r = await hxFetch(`${API}/spaces`);
    if (r.ok) setSpaces(await r.json());
    setLoading(false);
  };

  useEffect(() => { load(); }, []);

  useEffect(() => {
    if (!searchQ.trim() || searchQ.length < 2) { setSearchResults([]); return; }
    const t = setTimeout(async () => {
      const r = await hxFetch(`${API}/search?q=${encodeURIComponent(searchQ)}`);
      if (r.ok) setSearchResults(await r.json());
    }, 300);
    return () => clearTimeout(t);
  }, [searchQ]);

  const createSpace = async () => {
    if (!form.name.trim()) return;
    const r = await hxFetch(`${API}/spaces`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(form),
    });
    if (r.ok) { setShowCreate(false); setForm({ name: "", description: "", is_public: false }); await load(); }
  };

  const deleteSpace = async (id: string) => {
    if (!confirm("Delete this space and all its articles?")) return;
    // Spaces don't have a delete endpoint yet — just reload
    await load();
  };

  if (activeSpace) {
    return <SpaceView space={activeSpace} onBack={() => { setActiveSpace(null); load(); }} />;
  }

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", height: "100%", overflow: "auto", boxSizing: "border-box" }}>
      {/* Action bar — Work Center format */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-xl)" }}>
        <div style={{ position: "relative", width: 300 }}>
          <input
            style={{ ...S.input, marginBottom: 0 }}
            placeholder="Search across all articles…"
            value={searchQ}
            onChange={e => setSearchQ(e.target.value)}
          />
          {searchResults.length > 0 && (
            <div style={{ position: "absolute", top: "100%", left: 0, right: 0, background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", zIndex: 100, maxHeight: 240, overflowY: "auto", boxShadow: "0 4px 16px rgba(0,0,0,0.3)" }}>
              {searchResults.map(a => (
                <div key={a.id} style={{ padding: "8px 12px", cursor: "pointer", fontSize: 13, borderBottom: "1px solid var(--border-subtle)" }}
                  onClick={async () => {
                    setSearchQ(""); setSearchResults([]);
                    const r = await hxFetch(`${API}/articles/${a.id}?resolve_live=true`);
                    if (r.ok) {
                      const space = spaces.find(s => s.id === a.space_id);
                      if (space) setActiveSpace(space);
                    }
                  }}>
                  <div style={{ fontWeight: 600, color: "var(--text-primary)" }}>{a.title}</div>
                  <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{a.status} · v{a.version} · {a.word_count}w</div>
                </div>
              ))}
            </div>
          )}
        </div>
        <Button onClick={() => setShowCreate(true)}>+ New Space</Button>
      </div>

      {showCreate && (
        <div style={{ background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-md)", padding: "var(--space-lg)", marginBottom: "var(--space-xl)", maxWidth: 400 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: "var(--text-primary)", marginBottom: "var(--space-md)" }}>New Documentation Space</div>
          <input style={S.input} placeholder="Space name (e.g. Engineering, Product)" value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} autoFocus />
          <input style={S.input} placeholder="Description (optional)" value={form.description} onChange={e => setForm(f => ({ ...f, description: e.target.value }))} />
          <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13, marginBottom: "var(--space-md)", cursor: "pointer", color: "var(--text-secondary)" }}>
            <input type="checkbox" checked={form.is_public} onChange={e => setForm(f => ({ ...f, is_public: e.target.checked }))} />
            Public space (visible on Customer Portal)
          </label>
          <div style={{ display: "flex", gap: "var(--space-sm)" }}>
            <Button onClick={createSpace}>Create</Button>
            <button style={{ padding: "6px 12px", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)", background: "none", cursor: "pointer", fontSize: 12, color: "var(--text-secondary)" }} onClick={() => setShowCreate(false)}>Cancel</button>
          </div>
        </div>
      )}

      {loading ? (
        <div style={{ color: "var(--text-muted)", padding: 40 }}>Loading spaces…</div>
      ) : spaces.length === 0 ? (
        <div style={{ color: "var(--text-muted)", padding: 60 }}>
          <div style={{ fontSize: 40, marginBottom: 12 }}>📚</div>
          <div style={{ fontSize: 16, fontWeight: 600, color: "var(--text-primary)" }}>No documentation spaces yet</div>
          <div style={{ fontSize: 13, marginTop: 4 }}>Create a space to start writing living documentation</div>
        </div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(240px,1fr))", gap: "var(--space-md)" }}>
          {spaces.map(s => (
            <div key={s.id}
              style={{ background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-md)", padding: "var(--space-lg)", cursor: "pointer" }}
              onClick={() => setActiveSpace(s)}>
              <div style={{ fontSize: 28, marginBottom: "var(--space-sm)" }}>📚</div>
              <div style={{ fontWeight: 700, fontSize: 14, color: "var(--text-primary)", marginBottom: 4 }}>{s.name}</div>
              {s.description && <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: "var(--space-sm)" }}>{s.description}</div>}
              <div style={{ display: "flex", gap: 6 }}>
                {s.is_public && <span style={{ ...S.badge, background: "#22c55e22", color: "#22c55e" }}>Public</span>}
                <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>/{s.slug}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
