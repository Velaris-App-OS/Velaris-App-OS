/**
 * P61 — HxWork: Development Lifecycle Board
 *
 * Boards are scoped to Helix artifacts (case types, integrations, rules, etc.).
 * Cards are user stories about building those artifacts — NOT case instances.
 * Stories auto-advance when a commit is made to the linked artifact.
 *
 * Lifecycle: Backlog → In Design → In Development → In Review → Done
 */
import React, { useState, useEffect, useCallback, useRef } from "react";
import { CommitHistory } from "@shared/components";
import { useAuth } from "@/auth";

const API = "/api/v1/hxwork";
const AUTH = () => ({ Authorization: `Bearer ${localStorage.getItem("helix_token") ?? ""}` });

type BoardStatus = "active" | "closed";

type Board = {
  id: string; name: string; description: string | null;
  artifact_type: string | null; artifact_id: string | null;
  status: BoardStatus;
  closed_at: string | null;
  created_by: string | null; created_at: string;
};
type Story = {
  id: string; board_id: string; sprint_id: string | null;
  branch_id: string | null; branch_name: string | null;
  title: string; description: string | null;
  acceptance_criteria: string | null;
  status: string; story_points: number | null; assigned_to: string | null;
  artifact_type: string | null; artifact_id: string | null;
  linked_commit_ids: string[]; created_by: string; created_at: string;
};
type Sprint = {
  id: string; name: string; goal: string | null;
  status: string; start_date: string | null; end_date: string | null;
  velocity: number | null; card_count: number;
};
type Columns = Record<string, Story[]>;
type DirectoryUser = { user_id: string; display_name: string | null; email: string | null; is_active: boolean };

const STATUSES = ["backlog", "in_design", "in_development", "in_review", "done"] as const;
const STATUS_LABEL: Record<string, string> = {
  backlog: "Backlog", in_design: "In Design",
  in_development: "In Development", in_review: "In Review", done: "Done",
};
const STATUS_COLOR: Record<string, string> = {
  backlog: "#94a3b8", in_design: "#a78bfa",
  in_development: "#3b82f6", in_review: "#f59e0b", done: "#22c55e",
};
const ARTIFACT_TYPES = [
  { value: "case_type",    label: "Case Type",     hint: "Copy the UUID from Case Designer → case type settings" },
  { value: "form",         label: "Form",           hint: "Copy the UUID from Form Builder → form settings" },
  { value: "integration",  label: "Integration",    hint: "Copy the UUID from HxConnect → integration details" },
  { value: "rule",         label: "Rule",           hint: "Copy the rule UUID from Case Designer → rules tab" },
  { value: "process",      label: "Process",        hint: "Copy the UUID from BPMN Modeler → process details" },
  { value: "global",       label: "Global Config",  hint: "Leave blank or use a meaningful identifier" },
];

const S: Record<string, React.CSSProperties> = {
  page:  { display: "flex", height: "100%" },
  side:  { width: 248, flexShrink: 0, borderRight: "1px solid var(--border-subtle)", display: "flex", flexDirection: "column", background: "var(--bg-elevated)" },
  main:  { flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" },
  btn:   { padding: "6px 14px", border: "none", borderRadius: "var(--radius-sm)", cursor: "pointer", fontSize: 12, fontWeight: 600, fontFamily: "var(--font-body)" },
  btnP:  { background: "var(--accent)", color: "#fff" },
  btnS:  { background: "var(--bg-elevated)", border: "1px solid var(--border-default)", color: "var(--text-secondary)" },
  btnD:  { background: "color-mix(in srgb, var(--status-failed) 10%, transparent)", color: "var(--status-failed)", border: "1px solid color-mix(in srgb, var(--status-failed) 25%, transparent)" },
  btnW:  { background: "color-mix(in srgb, var(--status-running) 10%, transparent)", color: "var(--status-running)", border: "1px solid color-mix(in srgb, var(--status-running) 25%, transparent)" },
  input: { width: "100%", padding: "7px 10px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" as const, fontFamily: "var(--font-body)" },
  label: { fontSize: 11, fontWeight: 600, color: "var(--text-muted)", marginBottom: 4, display: "block", textTransform: "uppercase" as const, letterSpacing: "0.04em", fontFamily: "var(--font-mono)" },
  card:  { background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-md)", padding: "var(--space-md)", marginBottom: "var(--space-sm)", cursor: "pointer" },
};

// ── Board persistence helpers (localStorage — backend doesn't persist status/activity) ──

const CLOSED_KEY = "hxwork_closed_boards";
function closedSet(): Set<string> {
  try { return new Set(JSON.parse(localStorage.getItem(CLOSED_KEY) ?? "[]")); }
  catch { return new Set(); }
}
function persistClose(boardId: string) {
  const s = closedSet(); s.add(boardId);
  localStorage.setItem(CLOSED_KEY, JSON.stringify([...s]));
}
function isBoardClosed(board: { id: string; status?: string }): boolean {
  return board.status === "closed" || closedSet().has(board.id);
}

// Session events are in-memory (sessionEventsRef) — persist them per board so
// move/update/close events survive page reloads.
function activityKey(boardId: string) { return `hxwork_activity_${boardId}`; }
function loadStoredEvents(boardId: string): ActivityEvent[] {
  try { return JSON.parse(localStorage.getItem(activityKey(boardId)) ?? "[]"); }
  catch { return []; }
}
function saveStoredEvents(boardId: string, events: ActivityEvent[]) {
  // Keep last 200 events to cap storage size
  try { localStorage.setItem(activityKey(boardId), JSON.stringify(events.slice(0, 200))); }
  catch {}
}

async function req(method: string, path: string, body?: unknown) {
  const r = await fetch(path, {
    method, headers: { "Content-Type": "application/json", ...AUTH() },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 204) return null;
  if (!r.ok) { const e = await r.json().catch(() => ({ detail: r.statusText })); throw new Error(e.detail); }
  return r.json();
}

async function fetchUsers(): Promise<DirectoryUser[]> {
  try {
    const data = await req("GET", "/api/v1/user-directory?page_size=200");
    return (Array.isArray(data) ? data : (data?.items ?? [])).filter((u: DirectoryUser) => u.is_active);
  } catch { return []; }
}

type ActivityEvent = {
  id: string;
  actor: string;           // user_id or "hxnexus"
  actor_name: string;      // display name
  is_ai: boolean;
  action: string;          // "created_story" | "updated_story" | "moved_story" | "committed" | "closed_board"
  story_title?: string;
  story_owner?: string;    // for AI moves: whose story it is
  from_status?: string;
  to_status?: string;
  artifact_ref?: string;
  timestamp: string;
};

async function fetchActivity(boardId: string): Promise<ActivityEvent[]> {
  try {
    return await req("GET", `${API}/boards/${boardId}/activity`);
  } catch { return []; }
}

/* ── ArtifactPicker — reused in BoardModal and StoryModal ─────────── */

function ArtifactPicker({ artifactType, setArtifactType, artifactId, setArtifactId, compact = false }: {
  artifactType: string; setArtifactType: (t: string) => void;
  artifactId: string; setArtifactId: (id: string) => void;
  compact?: boolean;
}) {
  const [artifacts, setArtifacts] = useState<ArtifactOption[]>([]);
  const [loading, setLoading]     = useState(false);
  const [manual, setManual]       = useState(false);

  const hasList = ARTIFACT_TYPES_WITH_LIST.has(artifactType);
  const selected = artifacts.find(a => a.id === artifactId);

  useEffect(() => {
    setArtifactId("");
    setManual(false);
    if (!hasList) { setArtifacts([]); return; }
    setLoading(true);
    fetchArtifacts(artifactType).then(setArtifacts).finally(() => setLoading(false));
  }, [artifactType]);

  return (
    <div style={compact ? {} : { padding: "var(--space-md)", background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)" }}>
      {!compact && (
        <div style={{ fontSize: 11, fontWeight: 700, color: "var(--accent)", fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: "var(--space-sm)" }}>
          🔗 Artifact Link <span style={{ fontWeight: 400, color: "var(--text-muted)" }}>(optional)</span>
        </div>
      )}

      <label style={S.label}>Artifact Type</label>
      <select style={{ ...S.input, marginBottom: "var(--space-sm)" }} value={artifactType}
        onChange={e => setArtifactType(e.target.value)}>
        {[{ value: "", label: "None" }, ...ARTIFACT_TYPES].map(t => (
          <option key={t.value} value={t.value}>{t.label}</option>
        ))}
      </select>

      {artifactType && hasList && !manual && (
        <>
          <label style={S.label}>
            {ARTIFACT_TYPES.find(t => t.value === artifactType)?.label}
            {loading && <span style={{ fontWeight: 400, marginLeft: 6, color: "var(--text-muted)" }}>loading…</span>}
          </label>
          <select style={{ ...S.input, marginBottom: "var(--space-xs)" }} value={artifactId}
            onChange={e => setArtifactId(e.target.value)} disabled={loading}>
            <option value="">— select —</option>
            {artifacts.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
          {selected && (
            <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginBottom: "var(--space-xs)", padding: "2px 6px", background: "var(--bg-card)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              ID: {selected.id}
            </div>
          )}
          {!loading && artifacts.length === 0 && (
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: "var(--space-xs)" }}>No items found.</div>
          )}
          <button type="button" onClick={() => { setManual(true); setArtifactId(""); }}
            style={{ fontSize: 11, color: "var(--accent)", background: "none", border: "none", cursor: "pointer", padding: 0, textDecoration: "underline" }}>
            Not in list? Enter UUID manually
          </button>
        </>
      )}

      {artifactType && (!hasList || manual) && (
        <>
          <label style={S.label}>Artifact UUID</label>
          <input style={{ ...S.input, fontFamily: "var(--font-mono)", fontSize: 12, marginBottom: "var(--space-xs)" }}
            value={artifactId} onChange={e => setArtifactId(e.target.value)} placeholder="Paste UUID" />
          {hasList && manual && (
            <button type="button" onClick={() => { setManual(false); setArtifactId(""); }}
              style={{ fontSize: 11, color: "var(--accent)", background: "none", border: "none", cursor: "pointer", padding: 0, textDecoration: "underline" }}>
              ← Back to list
            </button>
          )}
          {!hasList && (
            <div style={{ fontSize: 11, color: "var(--text-muted)", lineHeight: 1.4 }}>
              💡 {ARTIFACT_TYPES.find(t => t.value === artifactType)?.hint}
            </div>
          )}
        </>
      )}
    </div>
  );
}

type ArtifactOption = { id: string; name: string };

async function fetchArtifacts(type: string): Promise<ArtifactOption[]> {
  try {
    switch (type) {
      case "case_type": {
        const d = await req("GET", "/api/v1/case-types?page_size=200");
        return (d?.items ?? d ?? []).map((x: any) => ({ id: x.id, name: x.name }));
      }
      case "form": {
        const d = await req("GET", "/api/v1/forms");
        return (Array.isArray(d) ? d : (d?.items ?? [])).map((x: any) => ({ id: x.id, name: x.name }));
      }
      case "rule": {
        const d = await req("GET", "/api/v1/rules");
        return (Array.isArray(d) ? d : (d?.items ?? [])).map((x: any) => ({ id: x.id, name: x.name }));
      }
      case "process": {
        const d = await req("GET", "/api/processes");
        return (d?.processes ?? d ?? []).map((x: any) => ({ id: x.id, name: x.name ?? x.id }));
      }
      default:
        return [];
    }
  } catch { return []; }
}

/* ── Sidebar ─────────────────────────────────────────────────────── */

function Sidebar({ boards, selected, onSelect, onNew, onDelete }: {
  boards: Board[]; selected: Board | null;
  onSelect: (b: Board) => void; onNew: () => void;
  onDelete: (b: Board) => void;
}) {
  const active = boards.filter(b => b.status !== "closed");
  const closed = boards.filter(b => b.status === "closed");

  const BoardItem = ({ b }: { b: Board }) => (
    <div key={b.id} onClick={() => onSelect(b)}
      style={{
        padding: "10px 14px", cursor: "pointer", borderBottom: "1px solid var(--border-subtle)",
        background: selected?.id === b.id ? "var(--accent-dim)" : "transparent",
        borderLeft: selected?.id === b.id ? "3px solid var(--accent)" : "3px solid transparent",
        display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 6,
        opacity: b.status === "closed" ? 0.65 : 1,
      }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{b.name}</div>
          {b.status === "closed" && (
            <span style={{ fontSize: 9, fontWeight: 700, padding: "1px 5px", borderRadius: 3, background: "var(--bg-elevated)", color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase", flexShrink: 0 }}>CLOSED</span>
          )}
        </div>
        {b.artifact_type && <div style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{b.artifact_type}</div>}
      </div>
      <button title="Delete board" onClick={e => { e.stopPropagation(); onDelete(b); }}
        style={{ ...S.btn, ...S.btnD, padding: "2px 7px", fontSize: 11, flexShrink: 0 }}>🗑</button>
    </div>
  );

  return (
    <div style={S.side}>
      <div style={{ padding: "var(--space-md) 14px", borderBottom: "1px solid var(--border-subtle)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontWeight: 700, fontSize: 13, color: "var(--text-primary)" }}>Dev Boards</span>
        <button style={{ ...S.btn, ...S.btnP, padding: "4px 10px" }} onClick={onNew}>+</button>
      </div>
      <div style={{ flex: 1, overflow: "auto" }}>
        {boards.length === 0 && (
          <div style={{ padding: "var(--space-lg) 14px", fontSize: 12, color: "var(--text-muted)" }}>No boards yet.</div>
        )}
        {active.map(b => <BoardItem key={b.id} b={b} />)}
        {closed.length > 0 && (
          <>
            <div style={{ padding: "var(--space-sm) 14px", fontSize: 10, fontWeight: 700, color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.06em", borderTop: "1px solid var(--border-subtle)", background: "var(--bg-elevated)" }}>
              Closed
            </div>
            {closed.map(b => <BoardItem key={b.id} b={b} />)}
          </>
        )}
      </div>
    </div>
  );
}

/* ── New / Edit Board Modal ──────────────────────────────────────── */

const ARTIFACT_TYPES_WITH_LIST = new Set(["case_type", "form", "rule", "process"]);

function BoardModal({ existing, onClose, onSave }: {
  existing?: Board | null;
  onClose: () => void;
  onSave: (b: Board) => void;
}) {
  const [name, setName]                 = useState(existing?.name ?? "");
  const [desc, setDesc]                 = useState(existing?.description ?? "");
  const [artifactType, setArtifactType] = useState(existing?.artifact_type ?? "");
  const [artifactId, setArtifactId]     = useState(existing?.artifact_id ?? "");
  const [busy, setBusy]                 = useState(false);
  const [err, setErr]                   = useState<string | null>(null);

  const isEdit = !!existing;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true); setErr(null);
    try {
      const payload = {
        name: name.trim(),
        description: desc.trim() || null,
        artifact_type: artifactType,
        artifact_id: artifactId.trim() || null,
      };
      const b = isEdit
        ? await req("PATCH", `${API}/boards/${existing!.id}`, payload)
        : await req("POST", `${API}/boards`, payload);
      onSave(b);
    } catch (ex: any) { setErr(ex.message); }
    finally { setBusy(false); }
  };

  return (
    <>
      <div onClick={onClose} style={{ position: "fixed", inset: 0, background: "var(--bg-overlay)", zIndex: 99 }} />
      <div style={{ position: "fixed", top: "50%", left: "50%", transform: "translate(-50%,-50%)", zIndex: 100, width: 500, maxHeight: "90vh", overflowY: "auto", background: "var(--bg-panel)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-lg)", padding: "var(--space-xl)", boxShadow: "var(--shadow-lg)" }}>
        <div style={{ fontWeight: 700, fontSize: 15, marginBottom: "var(--space-lg)", color: "var(--text-primary)" }}>
          {isEdit ? "Edit Board" : "New Dev Board"}
        </div>

        {err && (
          <div style={{ fontSize: 12, color: "var(--status-failed)", marginBottom: "var(--space-md)", padding: "var(--space-sm) var(--space-md)", background: "color-mix(in srgb, var(--status-failed) 10%, transparent)", borderRadius: "var(--radius-sm)" }}>
            {err}
          </div>
        )}

        <form onSubmit={submit}>
          <label style={S.label}>Board Name *</label>
          <input style={{ ...S.input, marginBottom: "var(--space-md)" }} value={name}
            onChange={e => setName(e.target.value)} placeholder="e.g. Insurance Claim Case Type" required autoFocus />

          <label style={S.label}>Description</label>
          <textarea style={{ ...S.input, height: 56, resize: "vertical", marginBottom: "var(--space-lg)" } as React.CSSProperties}
            value={desc} onChange={e => setDesc(e.target.value)} placeholder="What is this board tracking?" />

          {/* Artifact section — uses shared ArtifactPicker */}
          <div style={{ marginBottom: "var(--space-md)" }}>
            <ArtifactPicker
              artifactType={artifactType} setArtifactType={setArtifactType}
              artifactId={artifactId} setArtifactId={setArtifactId}
            />
          </div>

          <div style={{ display: "flex", gap: "var(--space-sm)", justifyContent: "flex-end" }}>
            <button type="button" style={{ ...S.btn, ...S.btnS }} onClick={onClose}>Cancel</button>
            <button type="submit" style={{ ...S.btn, ...S.btnP }} disabled={busy}>
              {busy ? (isEdit ? "Saving…" : "Creating…") : (isEdit ? "Save Changes" : "Create Board")}
            </button>
          </div>
        </form>
      </div>
    </>
  );
}

/* ── Story Modal ─────────────────────────────────────────────────── */

function StoryModal({ story, boardId, users, readOnly, onClose, onSave, onDelete }: {
  story: Story | null; boardId: string;
  users: DirectoryUser[];
  readOnly: boolean;
  onClose: () => void;
  onSave: (patch: Partial<Story>) => Promise<void>;
  onDelete: () => Promise<void>;
}) {
  const [title, setTitle]         = useState(story?.title ?? "");
  const [description, setDesc]    = useState(story?.description ?? "");
  const [ac, setAc]               = useState(story?.acceptance_criteria ?? "");
  const [status, setStatus]       = useState(story?.status ?? "backlog");
  const [points, setPoints]       = useState(story?.story_points?.toString() ?? "");
  const [assignedTo, setAssign]   = useState(story?.assigned_to ?? "");
  const [storyArtType, setStoryArtType] = useState(story?.artifact_type ?? "");
  const [storyArtId, setStoryArtId]     = useState(story?.artifact_id ?? "");
  const [assignErr, setAssignErr] = useState<string | null>(null);
  const [busy, setBusy]           = useState(false);
  const [linkingBranch, setLinkingBranch] = useState(false);
  const [branchQuery, setBranchQuery]     = useState("");
  const [branchResults, setBranchResults] = useState<{ id: string; name: string; status: string; artifact_type: string | null }[]>([]);
  const [branchLoading, setBranchLoading] = useState(false);
  const [selectedBranch, setSelectedBranch] = useState<{ id: string; name: string } | null>(null);
  const [linkErr, setLinkErr]             = useState<string | null>(null);
  const branchDebounce = useRef<ReturnType<typeof setTimeout> | null>(null);

  const searchBranches = useCallback(async (q: string) => {
    setBranchLoading(true);
    try {
      const qs = new URLSearchParams();
      if (q.trim()) qs.set("q", q.trim());
      const d = await req("GET", `/api/v1/branches${qs.toString() ? "?" + qs : ""}`);
      const list = (d?.branches ?? []).filter(
        (b: any) => b.status !== "merged" && b.status !== "closed"
      );
      setBranchResults(list);
    } catch { setBranchResults([]); }
    setBranchLoading(false);
  }, []);

  const handleBranchQueryChange = (val: string) => {
    setBranchQuery(val);
    setSelectedBranch(null);
    if (branchDebounce.current) clearTimeout(branchDebounce.current);
    branchDebounce.current = setTimeout(() => searchBranches(val), 250);
  };

  const confirmLinkBranch = async () => {
    if (!selectedBranch) { setLinkErr("Select a branch first"); return; }
    setLinkErr(null);
    setBusy(true);
    try {
      await onSave({ branch_id: selectedBranch.id, branch_name: selectedBranch.name });
      setLinkingBranch(false);
      setBranchQuery("");
      setSelectedBranch(null);
      setBranchResults([]);
    } catch (e: any) { setLinkErr(e.message ?? "Failed to link"); }
    setBusy(false);
  };

  const validateAssignee = (val: string) => {
    if (!val.trim()) { setAssignErr(null); return true; }
    const match = users.find(u =>
      u.user_id === val.trim() ||
      u.display_name?.toLowerCase() === val.trim().toLowerCase() ||
      u.email?.toLowerCase() === val.trim().toLowerCase()
    );
    if (!match) { setAssignErr("User not found in the system. Choose from the suggestions."); return false; }
    setAssignErr(null); return true;
  };

  const save = async () => {
    if (!title.trim()) return;
    if (!validateAssignee(assignedTo)) return;
    let resolvedAssignee = assignedTo || null;
    if (resolvedAssignee) {
      const match = users.find(u =>
        u.display_name?.toLowerCase() === resolvedAssignee!.toLowerCase() ||
        u.email?.toLowerCase() === resolvedAssignee!.toLowerCase()
      );
      if (match) resolvedAssignee = match.user_id;
    }
    setBusy(true);
    await onSave({
      title: title.trim(), description: description || null,
      acceptance_criteria: ac || null, status,
      story_points: points ? parseInt(points) : null,
      assigned_to: resolvedAssignee,
      artifact_type: storyArtType || null,
      artifact_id: storyArtId || null,
    });
    setBusy(false);
  };

  const userListId = `hxwork-users-${boardId}`;
  const assignedUser = users.find(u => u.user_id === story?.assigned_to);

  return (
    <>
      <div onClick={onClose} style={{ position: "fixed", inset: 0, background: "var(--bg-overlay)", zIndex: 99 }} />
      <div style={{ position: "fixed", top: "50%", left: "50%", transform: "translate(-50%,-50%)", zIndex: 100, width: 600, maxHeight: "90vh", overflowY: "auto", background: "var(--bg-panel)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-lg)", padding: "var(--space-xl)", boxShadow: "var(--shadow-lg)" }}>

        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "var(--space-lg)" }}>
          <div style={{ fontSize: 15, fontWeight: 700, color: "var(--text-primary)" }}>{story ? "Story Details" : "New Story"}</div>
          {readOnly && (
            <span style={{ fontSize: 11, padding: "3px 8px", borderRadius: "var(--radius-sm)", background: "var(--bg-elevated)", color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase" }}>
              Read Only · Board Closed
            </span>
          )}
        </div>

        <label style={S.label}>Title *</label>
        <input style={{ ...S.input, marginBottom: "var(--space-md)" }} value={title}
          onChange={e => setTitle(e.target.value)} placeholder="As a developer, I want to…" autoFocus disabled={readOnly} />

        <label style={S.label}>Description</label>
        <textarea style={{ ...S.input, height: 64, resize: "vertical", marginBottom: "var(--space-md)" } as React.CSSProperties}
          value={description} onChange={e => setDesc(e.target.value)} placeholder="Context and background…" disabled={readOnly} />

        <label style={S.label}>Acceptance Criteria</label>
        <textarea style={{ ...S.input, height: 64, resize: "vertical", marginBottom: "var(--space-md)" } as React.CSSProperties}
          value={ac} onChange={e => setAc(e.target.value)}
          placeholder={"• Payment step visible\n• SLA clock starts on submit"} disabled={readOnly} />

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "var(--space-md)", marginBottom: "var(--space-md)" }}>
          <div>
            <label style={S.label}>Status</label>
            <select style={S.input} value={status} onChange={e => setStatus(e.target.value)} disabled={readOnly}>
              {STATUSES.map(s => <option key={s} value={s}>{STATUS_LABEL[s]}</option>)}
            </select>
          </div>
          <div>
            <label style={S.label}>Points</label>
            <input style={S.input} type="number" min={1} max={100} value={points}
              onChange={e => setPoints(e.target.value)} placeholder="3" disabled={readOnly} />
          </div>
          <div>
            <label style={S.label}>Assigned To</label>
            <datalist id={userListId}>
              {users.map(u => (
                <option key={u.user_id} value={u.user_id}>
                  {u.display_name ? `${u.display_name} (${u.email ?? u.user_id})` : u.email ?? u.user_id}
                </option>
              ))}
            </datalist>
            <input
              style={{ ...S.input, borderColor: assignErr ? "var(--status-failed)" : undefined }}
              list={userListId} value={assignedTo}
              onChange={e => { setAssign(e.target.value); setAssignErr(null); }}
              onBlur={e => validateAssignee(e.target.value)}
              placeholder="user ID or name" disabled={readOnly}
            />
            {assignErr && <div style={{ fontSize: 11, color: "var(--status-failed)", marginTop: 3 }}>{assignErr}</div>}
            {assignedUser && !assignErr && (
              <div style={{ fontSize: 11, color: "var(--status-completed)", marginTop: 3 }}>
                ✓ {assignedUser.display_name ?? assignedUser.email}
              </div>
            )}
          </div>
        </div>

        {/* Story-level artifact link */}
        {!readOnly && (
          <div style={{ marginBottom: "var(--space-md)" }}>
            <ArtifactPicker
              artifactType={storyArtType} setArtifactType={setStoryArtType}
              artifactId={storyArtId} setArtifactId={setStoryArtId}
              compact={false}
            />
          </div>
        )}
        {readOnly && story?.artifact_type && (
          <div style={{ marginBottom: "var(--space-md)", padding: "var(--space-sm) var(--space-md)", background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)", fontSize: 12 }}>
            <span style={{ color: "var(--text-muted)" }}>Artifact: </span>
            <span style={{ color: "var(--text-secondary)", fontFamily: "var(--font-mono)" }}>{story.artifact_type}</span>
            {story.artifact_id && <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}> · {story.artifact_id.slice(0, 16)}…</span>}
          </div>
        )}

        {/* Branch tracking */}
        {story && (
          <div style={{ marginBottom: "var(--space-md)", padding: "var(--space-sm) var(--space-md)", background: story.branch_name ? "var(--accent-dim)" : "var(--bg-elevated)", borderRadius: "var(--radius-sm)", border: `1px solid ${story.branch_name ? "var(--accent)" : "var(--border-subtle)"}` }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, marginBottom: linkingBranch ? 8 : 0 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span style={{ fontSize: 14, color: story.branch_name ? "var(--accent)" : "var(--text-muted)" }}>⎇</span>
                <div>
                  <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase" }}>Branch</div>
                  {story.branch_name
                    ? <div style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--accent)", fontWeight: 600 }}>{story.branch_name}</div>
                    : <div style={{ fontSize: 11, color: "var(--text-muted)", fontStyle: "italic" }}>Not linked</div>
                  }
                </div>
              </div>
              {!readOnly && !linkingBranch && (
                <button
                  style={{ ...S.btn, ...S.btnS, padding: "3px 10px", fontSize: 11 }}
                  onClick={() => { setLinkingBranch(true); setLinkErr(null); searchBranches(""); }}
                >
                  {story.branch_name ? "Change" : "Link Branch"}
                </button>
              )}
            </div>

            {linkingBranch && (
              <div>
                <div style={{ position: "relative", marginBottom: 4 }}>
                  <input
                    autoFocus
                    value={branchQuery}
                    onChange={e => handleBranchQueryChange(e.target.value)}
                    placeholder="Search branches by name…"
                    style={{ ...S.input, fontSize: 12, paddingRight: branchLoading ? 28 : undefined }}
                  />
                  {branchLoading && (
                    <span style={{ position: "absolute", right: 8, top: "50%", transform: "translateY(-50%)", fontSize: 11, color: "var(--text-muted)" }}>…</span>
                  )}
                </div>
                {branchResults.length > 0 && !selectedBranch && (
                  <div style={{ border: "1px solid var(--border-default)", borderRadius: 4, background: "var(--bg-panel)", maxHeight: 160, overflowY: "auto", marginBottom: 6 }}>
                    {branchResults.map(b => (
                      <div
                        key={b.id}
                        onMouseDown={() => { setSelectedBranch({ id: b.id, name: b.name }); setBranchQuery(b.name); setBranchResults([]); }}
                        style={{ padding: "6px 10px", cursor: "pointer", fontSize: 12, borderBottom: "1px solid color-mix(in srgb, var(--border-default) 40%, transparent)" }}
                        onMouseEnter={e => (e.currentTarget.style.background = "color-mix(in srgb, var(--accent) 10%, var(--bg-panel))")}
                        onMouseLeave={e => (e.currentTarget.style.background = "transparent")}
                      >
                        <span style={{ fontFamily: "var(--font-mono)", fontWeight: 600, color: "var(--text-primary)" }}>{b.name}</span>
                        <span style={{ marginLeft: 8, fontSize: 10, color: "var(--text-muted)" }}>{b.status}{b.artifact_type ? ` · ${b.artifact_type}` : ""}</span>
                      </div>
                    ))}
                  </div>
                )}
                {selectedBranch && (
                  <div style={{ fontSize: 11, color: "var(--accent)", marginBottom: 6, fontFamily: "var(--font-mono)" }}>
                    ✓ Selected: {selectedBranch.name}
                  </div>
                )}
                {branchResults.length === 0 && !branchLoading && branchQuery && !selectedBranch && (
                  <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6, fontStyle: "italic" }}>No open branches found matching "{branchQuery}"</div>
                )}
                {linkErr && <div style={{ fontSize: 11, color: "var(--status-failed)", marginBottom: 4 }}>{linkErr}</div>}
                <div style={{ display: "flex", gap: 6 }}>
                  <button style={{ ...S.btn, ...S.btnP, padding: "3px 10px", fontSize: 11 }} onClick={confirmLinkBranch} disabled={busy || !selectedBranch}>
                    {busy ? "Linking…" : "Confirm"}
                  </button>
                  <button style={{ ...S.btn, ...S.btnS, padding: "3px 10px", fontSize: 11 }} onClick={() => { setLinkingBranch(false); setBranchQuery(""); setSelectedBranch(null); setBranchResults([]); setLinkErr(null); }}>
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </div>
        )}

        {story && story.linked_commit_ids.length > 0 && (
          <div style={{ marginBottom: "var(--space-md)", padding: "var(--space-sm) var(--space-md)", background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)", fontSize: 12 }}>
            <div style={{ fontWeight: 700, color: "var(--text-secondary)", marginBottom: 4, fontSize: 11, fontFamily: "var(--font-mono)", textTransform: "uppercase" }}>
              Linked Commits ({story.linked_commit_ids.length})
            </div>
            {story.linked_commit_ids.slice(0, 5).map(id => (
              <div key={id} style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--text-muted)", padding: "2px 0" }}>• {id.slice(0, 20)}…</div>
            ))}
          </div>
        )}

        <div style={{ display: "flex", gap: "var(--space-sm)", justifyContent: "space-between", alignItems: "center" }}>
          {story && !readOnly && (
            <button style={{ ...S.btn, ...S.btnD }} onClick={async () => { setBusy(true); await onDelete(); setBusy(false); }}>Delete</button>
          )}
          <div style={{ display: "flex", gap: "var(--space-sm)", marginLeft: "auto" }}>
            <button style={{ ...S.btn, ...S.btnS }} onClick={onClose}>{readOnly ? "Close" : "Cancel"}</button>
            {!readOnly && (
              <button style={{ ...S.btn, ...S.btnP }} onClick={save} disabled={busy || !title.trim()}>
                {busy ? "Saving…" : story ? "Save Story" : "Create Story"}
              </button>
            )}
          </div>
        </div>
      </div>
    </>
  );
}

/* ── Activity Row ────────────────────────────────────────────────── */

function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

const ACTION_ICON: Record<string, string> = {
  created_story: "✦", updated_story: "✎", moved_story: "→",
  committed: "⎇", closed_board: "🔒", ai_advanced: "🤖",
};

function ActivityRow({ evt }: { evt: ActivityEvent }) {
  const initials = (name: string) => (name ?? "?").split(/[\s._@]/).filter(Boolean).slice(0, 2).map(w => w[0]?.toUpperCase() ?? "").join("") || "?";
  const avatarColor = evt.is_ai ? "var(--accent)" : "var(--status-running)";

  const desc = () => {
    switch (evt.action) {
      case "created_story": return <><strong>{evt.actor_name}</strong> created <em>"{evt.story_title}"</em></>;
      case "updated_story": return <><strong>{evt.actor_name}</strong> updated <em>"{evt.story_title}"</em></>;
      case "moved_story":   return (
        <>
          <strong>{evt.is_ai ? "HxNexus AI" : evt.actor_name}</strong> moved{" "}
          {evt.story_owner && <><em>{evt.story_owner}</em>'s story{" "}</>}
          <em>"{evt.story_title}"</em>{" "}
          <span style={{ color: "var(--text-muted)" }}>{STATUS_LABEL[evt.from_status ?? ""] || evt.from_status}</span>
          {" → "}
          <span style={{ color: STATUS_COLOR[evt.to_status ?? ""] || "var(--accent)" }}>{STATUS_LABEL[evt.to_status ?? ""] || evt.to_status}</span>
        </>
      );
      case "committed":   return <><strong>{evt.actor_name}</strong> committed to <em>{evt.artifact_ref}</em></>;
      case "closed_board": return <><strong>{evt.actor_name}</strong> closed this board</>;
      case "ai_advanced": return (
        <>
          <span style={{ color: "var(--accent)", fontWeight: 600 }}>HxNexus AI</span> auto-advanced{" "}
          {evt.story_owner && <><em>{evt.story_owner}</em>'s story{" "}</>}
          <em>"{evt.story_title}"</em> to{" "}
          <span style={{ color: STATUS_COLOR[evt.to_status ?? ""] }}>{STATUS_LABEL[evt.to_status ?? ""] || evt.to_status}</span>
        </>
      );
      default: return <><strong>{evt.actor_name}</strong> {evt.action}</>;
    }
  };

  return (
    <div style={{ display: "flex", gap: "var(--space-sm)", marginBottom: "var(--space-md)", alignItems: "flex-start" }}>
      {/* Avatar */}
      <div style={{
        width: 28, height: 28, borderRadius: "50%", flexShrink: 0,
        background: evt.is_ai ? "var(--accent-dim)" : "color-mix(in srgb, var(--status-running) 15%, transparent)",
        border: `1px solid ${avatarColor}`,
        display: "flex", alignItems: "center", justifyContent: "center",
        fontSize: 10, fontWeight: 700, color: avatarColor, fontFamily: "var(--font-mono)",
      }}>
        {evt.is_ai ? "AI" : initials(evt.actor_name)}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 12, color: "var(--text-primary)", lineHeight: 1.5 }}>{desc()}</div>
        <div style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginTop: 2 }}>
          {ACTION_ICON[evt.action] ?? "·"} {timeAgo(evt.timestamp)} · {new Date(evt.timestamp).toLocaleString()}
        </div>
      </div>
    </div>
  );
}

/* ── Board View ──────────────────────────────────────────────────── */

function BoardView({ board, users, currentUser, onBoardUpdate }: { board: Board; users: DirectoryUser[]; currentUser: string; onBoardUpdate: (b: Board) => void }) {
  const [columns, setColumns]     = useState<Columns>({});
  const [sprints, setSprints]     = useState<Sprint[]>([]);
  const [generating, setGen]      = useState(false);
  const [genMsg, setGenMsg]       = useState<string | null>(null);
  const [editStory, setEdit]      = useState<Story | null | "new">(null);
  const [showFeed, setShowFeed]       = useState(false);
  const [activity, setActivity]       = useState<ActivityEvent[]>([]);
  const [actLoading, setActLoading]   = useState(false);
  const [draggingId, setDraggingId]   = useState<string | null>(null);
  const [dragOver, setDragOver]       = useState<string | null>(null);

  // Activity filters
  const [actSearch, setActSearch]     = useState("");
  const [actActor, setActActor]       = useState("");
  const [actAction, setActAction]     = useState("");
  const [actDate, setActDate]         = useState<"all"|"today"|"week"|"month">("all");

  // Board global filters
  const [boardSearch, setBoardSearch] = useState("");
  const [boardAssignee, setBoardAssignee] = useState("");
  const [boardStatus, setBoardStatus]    = useState("");
  const [boardPoints, setBoardPoints]    = useState<""|"1-3"|"5-8"|"13+">("") ;
  const [boardArtifact, setBoardArtifact] = useState("");
  const boardFiltersActive = !!(boardSearch || boardAssignee || boardStatus || boardPoints || boardArtifact);
  // Session events: restored from localStorage on mount so they survive page reloads
  const sessionEventsRef = useRef<ActivityEvent[]>(loadStoredEvents(board.id));
  const [showEditBoard, setShowEditBoard] = useState(false);
  const [closingBusy, setClosingBusy] = useState(false);
  // Local closure flag — initialised from board prop OR localStorage (survives reload
  // even when the backend doesn't persist the status field).
  const [isClosed, setIsClosed] = useState(() => isBoardClosed(board));

  const isReadOnly = isClosed;

  const load = useCallback(async () => {
    const d = await req("GET", `${API}/boards/${board.id}/stories`).catch(() => null);
    if (d) setColumns(d.columns ?? {});
    const sd = await req("GET", `${API}/boards/${board.id}/sprints`).catch(() => null);
    if (sd) setSprints(sd ?? []);
  }, [board.id]);

  useEffect(() => { load(); }, [load]);

  const generateStories = async () => {
    setGen(true); setGenMsg(null);
    try {
      const r = await req("POST", `${API}/boards/${board.id}/generate-stories`, {});
      setGenMsg(`✓ Generated ${r.generated} stories`);
      pushEvent({
        actor: "hxnexus", actor_name: "HxNexus AI", is_ai: true,
        action: "created_story",
        story_title: `${r.generated} stories generated from artifact`,
      });
      await load();
      if (showFeed) loadActivity();
    } catch (ex: any) { setGenMsg(`✗ ${ex.message}`); }
    finally { setGen(false); }
  };

  const saveStory = async (patch: Partial<Story>) => {
    const isNew = editStory === "new";
    if (isNew) {
      await req("POST", `${API}/boards/${board.id}/stories`, { ...patch, status: patch.status ?? "backlog" });
    } else if (editStory) {
      const prev = editStory as Story;
      await req("PATCH", `${API}/boards/${board.id}/stories/${prev.id}`, patch);
      // Detect status change
      if (patch.status && patch.status !== prev.status) {
        pushEvent({
          actor: currentUser, actor_name: currentUser, is_ai: false,
          action: "moved_story", story_title: patch.title ?? prev.title,
          from_status: prev.status, to_status: patch.status,
        });
      } else {
        pushEvent({
          actor: currentUser, actor_name: currentUser, is_ai: false,
          action: "updated_story", story_title: patch.title ?? prev.title,
        });
      }
    }
    if (isNew) {
      pushEvent({
        actor: currentUser, actor_name: currentUser, is_ai: false,
        action: "created_story", story_title: patch.title ?? "New Story",
      });
    }
    setEdit(null);
    await load();
    if (showFeed) loadActivity();
  };

  const deleteStory = async () => {
    if (!editStory || editStory === "new") return;
    const story = editStory as Story;
    await req("DELETE", `${API}/boards/${board.id}/stories/${story.id}`);
    pushEvent({
      actor: currentUser, actor_name: currentUser, is_ai: false,
      action: "updated_story", story_title: `${story.title} (deleted)`,
    });
    setEdit(null);
    await load();
    if (showFeed) loadActivity();
  };

  const dropStory = async (storyId: string, toStatus: string) => {
    if (isReadOnly) return;
    const allStories = Object.values(columns).flat();
    const story = allStories.find(s => s.id === storyId);
    if (!story || story.status === toStatus) return;
    // Optimistic UI
    setColumns(prev => {
      const next: Columns = {};
      for (const [col, list] of Object.entries(prev)) {
        next[col] = list.filter(s => s.id !== storyId);
      }
      next[toStatus] = [...(next[toStatus] ?? []), { ...story, status: toStatus }];
      return next;
    });
    // Record event immediately so feed shows it right away
    pushEvent({
      actor: currentUser, actor_name: currentUser, is_ai: false,
      action: "moved_story", story_title: story.title,
      from_status: story.status, to_status: toStatus,
    });
    await req("PATCH", `${API}/boards/${board.id}/stories/${storyId}`, { status: toStatus });
  };

  const pushEvent = useCallback((evt: Omit<ActivityEvent, "id" | "timestamp">) => {
    if (isClosed) return;   // activity frozen once board is closed
    const full: ActivityEvent = { ...evt, id: `local-${Date.now()}`, timestamp: new Date().toISOString() };
    sessionEventsRef.current = [full, ...sessionEventsRef.current];
    saveStoredEvents(board.id, sessionEventsRef.current);  // persist across reloads
    setActivity(prev => [full, ...prev]);
  }, [isClosed, board.id]);

  const loadActivity = useCallback(async () => {
    // No early-return here — closed boards still need to load their historical activity.
    // New events are frozen via pushEvent (which checks isClosed independently).
    setActLoading(true);
    try {
      // Try backend activity endpoint
      const backendEvents = await fetchActivity(board.id);
      if (backendEvents.length > 0) {
        // Merge: session events first (most recent), then backend (dedup by id)
        const backendIds = new Set(backendEvents.map(e => e.id));
        const sessionOnly = sessionEventsRef.current.filter(e => !backendIds.has(e.id));
        const merged = [...sessionOnly, ...backendEvents]
          .sort((a, b) => b.timestamp.localeCompare(a.timestamp));
        setActivity(merged);
      } else {
        // Backend endpoint not ready — fetch fresh stories and build synthetic events
        const d = await req("GET", `${API}/boards/${board.id}/stories`).catch(() => null);
        const allStories: Story[] = d ? Object.values(d.columns ?? {}).flat() as Story[] : [];
        const synthetic: ActivityEvent[] = allStories.map(s => {
          const isAI = !s.created_by || s.created_by === "system" || s.created_by === "hxnexus";
          return {
            id: s.id,
            actor: s.created_by ?? "system",
            actor_name: isAI ? "HxNexus AI" : (s.created_by ?? "System"),
            is_ai: isAI,
            action: "created_story",
            story_title: s.title,
            timestamp: s.created_at,
          };
        });
        // Merge session events (status changes, new stories) with synthetic baseline
        const syntheticIds = new Set(synthetic.map(e => e.id));
        const sessionOnly = sessionEventsRef.current.filter(e => !syntheticIds.has(e.id));
        const merged = [...sessionOnly, ...synthetic]
          .sort((a, b) => b.timestamp.localeCompare(a.timestamp));
        setActivity(merged);
      }
    } finally {
      setActLoading(false);
    }
  }, [board.id]);

  // Load when feed opens, and whenever loadActivity changes (board switch).
  useEffect(() => { if (showFeed) loadActivity(); }, [showFeed, loadActivity]);

  const closeBoard = async () => {
    if (!window.confirm(`Close this board permanently?\n\nThe board will become read-only and archived. For any bug fixes, create a new board or use an existing active one.`)) return;
    setClosingBusy(true);
    // Push the close event FIRST (before isClosed blocks it), then lock immediately.
    pushEvent({ actor: currentUser, actor_name: currentUser, is_ai: false, action: "closed_board" });
    persistClose(board.id);  // survive page reloads regardless of backend response
    setIsClosed(true);       // read-only + activity frozen from this point on
    try {
      const updated = await req("PATCH", `${API}/boards/${board.id}`, { status: "closed" });
      // Force status: "closed" regardless of what the API echoes back
      onBoardUpdate({ ...board, ...(updated ?? {}), status: "closed", closed_at: new Date().toISOString() });
    } catch (ex: any) {
      // API failed — rollback the local closed state
      setIsClosed(false);
      alert(`Failed to close board: ${ex.message}`);
    }
    finally { setClosingBusy(false); }
  };

  const allStories   = Object.values(columns).flat();
  const doneStories  = (columns.done ?? []).length;
  const totalPts     = allStories.reduce((n, s) => n + (s.story_points ?? 0), 0);
  const donePts      = (columns.done ?? []).reduce((n, s) => n + (s.story_points ?? 0), 0);
  const pctComplete  = totalPts > 0
    ? Math.round((donePts / totalPts) * 100)
    : allStories.length > 0 ? Math.round((doneStories / allStories.length) * 100) : 0;
  const activeSprint = sprints.find(s => s.status === "active");
  const allDone = allStories.length > 0 && allStories.every(s => s.status === "done");

  // Board filtered columns
  const filteredColumns: Columns = boardFiltersActive
    ? Object.fromEntries(
        Object.entries(columns).map(([status, stories]) => [
          status,
          stories.filter(s => {
            if (boardSearch && !s.title.toLowerCase().includes(boardSearch.toLowerCase())) return false;
            if (boardAssignee && s.assigned_to !== boardAssignee) return false;
            if (boardStatus && s.status !== boardStatus) return false;
            if (boardArtifact && s.artifact_type !== boardArtifact) return false;
            if (boardPoints) {
              const pts = s.story_points ?? 0;
              if (boardPoints === "1-3" && (pts < 1 || pts > 3)) return false;
              if (boardPoints === "5-8" && (pts < 5 || pts > 8)) return false;
              if (boardPoints === "13+" && pts < 13) return false;
            }
            return true;
          }),
        ])
      )
    : columns;

  const filteredCount = boardFiltersActive ? Object.values(filteredColumns).flat().length : null;

  // Activity filtered list
  const now = Date.now();
  const filteredActivity = activity.filter(evt => {
    const actorName = evt.actor_name ?? "";
    if (actSearch) {
      const q = actSearch.toLowerCase();
      if (!(evt.story_title ?? "").toLowerCase().includes(q) &&
          !actorName.toLowerCase().includes(q)) return false;
    }
    if (actActor && !actorName.toLowerCase().includes(actActor.toLowerCase())) return false;
    if (actAction && evt.action !== actAction) return false;
    if (actDate !== "all") {
      const t = new Date(evt.timestamp).getTime();
      if (actDate === "today"  && now - t > 86400000) return false;
      if (actDate === "week"   && now - t > 604800000) return false;
      if (actDate === "month"  && now - t > 2592000000) return false;
    }
    return true;
  });

  return (
    <div style={S.main}>
      {/* Board header */}
      <div style={{ padding: "var(--space-md) var(--space-lg)", borderBottom: "1px solid var(--border-subtle)", display: "flex", alignItems: "center", gap: "var(--space-md)", flexShrink: 0 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)" }}>
            <div style={{ fontWeight: 700, fontSize: 15, color: "var(--text-primary)" }}>{board.name}</div>
            {isReadOnly && (
              <span style={{ fontSize: 10, fontWeight: 700, padding: "2px 7px", borderRadius: "var(--radius-sm)", background: "var(--bg-elevated)", color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase" }}>
                CLOSED {board.closed_at ? `· ${new Date(board.closed_at).toLocaleDateString()}` : ""}
              </span>
            )}
          </div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginTop: 2 }}>
            {[
              board.artifact_type,
              board.artifact_id && `${board.artifact_id.slice(0, 14)}…`,
              `${doneStories}/${allStories.length} stories done`,
              totalPts > 0 && `${donePts}/${totalPts}pts`,
              `${pctComplete}% complete`,
            ].filter(Boolean).join(" · ")}
          </div>
        </div>

        {genMsg && <span style={{ fontSize: 12, color: genMsg.startsWith("✓") ? "var(--status-completed)" : "var(--status-failed)" }}>{genMsg}</span>}

        {/* Actions — only shown on active boards */}
        {!isReadOnly && (
          <>
            <button style={{ ...S.btn, ...S.btnS }} disabled={generating} onClick={generateStories}>
              {generating ? "Generating…" : "✨ Generate Stories"}
            </button>
            <button style={{ ...S.btn, ...S.btnS }} onClick={() => setShowEditBoard(true)}>✎ Edit Board</button>
          </>
        )}
        {/* Activity feed toggle */}
        <button style={{ ...S.btn, ...S.btnS, color: showFeed ? "var(--accent)" : "var(--text-secondary)" }}
          onClick={() => setShowFeed(v => !v)}>
          📋 Activity
        </button>
        {/* Close Board — permanent, no reopen (use a new board for fixes) */}
        {!isReadOnly && (
          <button
            style={{ ...S.btn, ...(allDone ? S.btnP : S.btnS), opacity: allDone ? 1 : 0.75 }}
            onClick={closeBoard} disabled={closingBusy}
            title={allDone ? "All stories done — archive this board" : "Close board (marks as read-only archive)"}
          >
            {closingBusy ? "…" : "✓ Close Board"}
          </button>
        )}
        {!isReadOnly && (
          <button style={{ ...S.btn, ...S.btnP }} onClick={() => setEdit("new")}>+ Story</button>
        )}
      </div>

      {/* Active sprint banner */}
      {activeSprint && (
        <div style={{ padding: "var(--space-sm) var(--space-lg)", background: "var(--accent-dim)", borderBottom: "1px solid var(--border-subtle)", fontSize: 12, display: "flex", gap: 16, alignItems: "center" }}>
          <span style={{ fontWeight: 700, color: "var(--accent)" }}>⚡ {activeSprint.name}</span>
          {activeSprint.goal && <span style={{ color: "var(--text-secondary)" }}>{activeSprint.goal}</span>}
          {activeSprint.end_date && <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>ends {new Date(activeSprint.end_date).toLocaleDateString()}</span>}
        </div>
      )}

      {/* Closed banner — no reopen; create a new board for bug fixes */}
      {isReadOnly && (
        <div style={{ padding: "var(--space-sm) var(--space-lg)", background: "color-mix(in srgb, var(--text-muted) 6%, transparent)", borderBottom: "1px solid var(--border-subtle)", fontSize: 12, color: "var(--text-muted)", display: "flex", alignItems: "center", gap: "var(--space-md)" }}>
          <span>🔒 Archived · read-only. For bug fixes, create a new board or use an existing active board and link individual stories to this artifact.</span>
        </div>
      )}

      {/* Board global filter bar */}
      <div style={{ padding: "var(--space-sm) var(--space-lg)", borderBottom: "1px solid var(--border-subtle)", display: "flex", gap: "var(--space-sm)", alignItems: "center", flexWrap: "wrap", flexShrink: 0, background: boardFiltersActive ? "color-mix(in srgb, var(--accent) 4%, transparent)" : "transparent" }}>
        {/* Search */}
        <input
          value={boardSearch} onChange={e => setBoardSearch(e.target.value)}
          placeholder="🔍 Search stories…"
          style={{ ...S.input, width: 180, marginBottom: 0, fontSize: 12 }}
        />
        {/* Assigned to — datalist so it works with many users */}
        <datalist id="board-assignees-list">
          {users.map(u => (
            <option key={u.user_id} value={u.user_id}>
              {u.display_name ?? u.email ?? u.user_id}
            </option>
          ))}
        </datalist>
        <div style={{ position: "relative", width: 160 }}>
          <input
            list="board-assignees-list"
            value={boardAssignee}
            onChange={e => setBoardAssignee(e.target.value)}
            placeholder="Assignee…"
            style={{ ...S.input, marginBottom: 0, fontSize: 12, width: "100%", paddingRight: boardAssignee ? 24 : undefined }}
          />
          {boardAssignee && (
            <button onClick={() => setBoardAssignee("")}
              style={{ position: "absolute", right: 6, top: "50%", transform: "translateY(-50%)", background: "none", border: "none", cursor: "pointer", fontSize: 11, color: "var(--text-muted)", padding: 0, lineHeight: 1 }}>
              ✕
            </button>
          )}
        </div>
        {/* Status */}
        <select value={boardStatus} onChange={e => setBoardStatus(e.target.value)}
          style={{ ...S.input, width: 140, marginBottom: 0, fontSize: 12 }}>
          <option value="">All statuses</option>
          {STATUSES.map(s => <option key={s} value={s}>{STATUS_LABEL[s]}</option>)}
        </select>
        {/* Points */}
        <select value={boardPoints} onChange={e => setBoardPoints(e.target.value as any)}
          style={{ ...S.input, width: 120, marginBottom: 0, fontSize: 12 }}>
          <option value="">Any points</option>
          <option value="1-3">1–3 pts (small)</option>
          <option value="5-8">5–8 pts (medium)</option>
          <option value="13+">13+ pts (large)</option>
        </select>
        {/* Artifact type */}
        <select value={boardArtifact} onChange={e => setBoardArtifact(e.target.value)}
          style={{ ...S.input, width: 140, marginBottom: 0, fontSize: 12 }}>
          <option value="">All artifacts</option>
          {ARTIFACT_TYPES.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
        </select>
        {/* Active filter indicator + clear */}
        {boardFiltersActive && (
          <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)", marginLeft: "auto" }}>
            <span style={{ fontSize: 11, color: "var(--accent)", fontFamily: "var(--font-mono)" }}>
              {filteredCount} stor{filteredCount === 1 ? "y" : "ies"} shown
            </span>
            <button onClick={() => { setBoardSearch(""); setBoardAssignee(""); setBoardStatus(""); setBoardPoints(""); setBoardArtifact(""); }}
              style={{ ...S.btn, ...S.btnS, padding: "3px 10px", fontSize: 11 }}>✕ Clear</button>
          </div>
        )}
      </div>

      {/* Board body */}
      <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>
        {/* Kanban columns */}
        <div style={{ flex: 1, overflow: "auto", padding: "var(--space-lg) var(--space-xl)" }}>
          <div style={{ display: "grid", gridTemplateColumns: `repeat(${STATUSES.length}, minmax(190px, 1fr))`, gap: "var(--space-md)", minWidth: 950 }}>
            {STATUSES.map(status => {
              const stories = filteredColumns[status] ?? [];
              const color = STATUS_COLOR[status];
              const isDropTarget = dragOver === status && draggingId !== null;

              return (
                <div key={status}>
                  {/* Column header */}
                  <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)", marginBottom: "var(--space-md)" }}>
                    <span style={{ width: 8, height: 8, borderRadius: "50%", background: color, flexShrink: 0 }} />
                    <span style={{ fontSize: 11, fontWeight: 700, color: "var(--text-secondary)", textTransform: "uppercase", letterSpacing: "0.05em", fontFamily: "var(--font-mono)" }}>
                      {STATUS_LABEL[status]}
                    </span>
                    <span style={{ fontSize: 11, color: "var(--text-muted)", marginLeft: "auto", fontFamily: "var(--font-mono)" }}>{stories.length}</span>
                  </div>

                  {/* Drop zone */}
                  <div
                    onDragOver={isReadOnly ? undefined : e => { e.preventDefault(); setDragOver(status); }}
                    onDragLeave={isReadOnly ? undefined : () => setDragOver(null)}
                    onDrop={isReadOnly ? undefined : e => {
                      e.preventDefault();
                      const id = e.dataTransfer.getData("storyId");
                      if (id) dropStory(id, status);
                      setDraggingId(null);
                      setDragOver(null);
                    }}
                    style={{
                      minHeight: 100,
                      background: isDropTarget
                        ? `color-mix(in srgb, ${color} 12%, var(--bg-elevated))`
                        : "color-mix(in srgb, var(--bg-elevated) 50%, transparent)",
                      border: isDropTarget
                        ? `2px dashed ${color}`
                        : "2px solid transparent",
                      borderRadius: "var(--radius-sm)",
                      padding: "var(--space-xs)",
                      transition: "background 0.15s, border-color 0.15s",
                    }}
                  >
                    {stories.map(story => {
                      const assignedUser = users.find(u => u.user_id === story.assigned_to);
                      const isDragging = draggingId === story.id;
                      return (
                        <div
                          key={story.id}
                          draggable={!isReadOnly}
                          onDragStart={isReadOnly ? undefined : e => {
                            e.dataTransfer.setData("storyId", story.id);
                            e.dataTransfer.effectAllowed = "move";
                            setDraggingId(story.id);
                          }}
                          onDragEnd={isReadOnly ? undefined : () => {
                            setDraggingId(null);
                            setDragOver(null);
                          }}
                          onClick={() => setEdit(story)}
                          style={{
                            ...S.card,
                            borderLeft: `3px solid ${color}`,
                            opacity: isDragging ? 0.4 : isReadOnly ? 0.85 : 1,
                            cursor: isReadOnly ? "default" : "grab",
                            transform: isDragging ? "scale(0.97)" : "none",
                            transition: "opacity 0.15s, transform 0.15s",
                            userSelect: "none",
                          }}
                        >
                          <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.35, marginBottom: "var(--space-sm)", color: "var(--text-primary)" }}>
                            {story.title}
                          </div>
                          {story.branch_name && (
                            <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--accent)", marginBottom: "var(--space-xs)", display: "flex", alignItems: "center", gap: 4 }}>
                              <span>⎇</span>
                              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{story.branch_name}</span>
                            </div>
                          )}
                          {/* Story-level artifact badge */}
                          {story.artifact_type && (
                            <div style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginBottom: "var(--space-xs)" }}>
                              🔗 {story.artifact_type}
                            </div>
                          )}
                          <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                            {story.story_points != null && (
                              <span style={{ fontSize: 10, background: color + "22", color, padding: "1px 7px", borderRadius: 100, fontWeight: 700, fontFamily: "var(--font-mono)" }}>
                                {story.story_points}pt
                              </span>
                            )}
                            {story.assigned_to && (
                              <span style={{ fontSize: 10, color: "var(--text-muted)" }} title={assignedUser?.email ?? story.assigned_to}>
                                → {assignedUser?.display_name ?? story.assigned_to}
                              </span>
                            )}
                            {story.linked_commit_ids.length > 0 && (
                              <span style={{ fontSize: 10, color: "var(--accent)", marginLeft: "auto", fontFamily: "var(--font-mono)" }} title="Linked commits">
                                🔗{story.linked_commit_ids.length}
                              </span>
                            )}
                          </div>
                        </div>
                      );
                    })}
                    {stories.length === 0 && (
                      <div style={{ padding: "var(--space-lg) var(--space-md)", fontSize: 11, color: isDropTarget ? color : "var(--text-muted)", fontStyle: "italic" }}>
                        {isDropTarget ? `Drop here → ${STATUS_LABEL[status]}` : "No stories"}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {/* Activity Timeline — replaces plain commit feed */}
        {showFeed && (
          <div style={{ width: 380, flexShrink: 0, borderLeft: "1px solid var(--border-subtle)", background: "var(--bg-elevated)", display: "flex", flexDirection: "column", overflow: "hidden" }}>

            {/* Activity header */}
            <div style={{ padding: "var(--space-md) var(--space-lg)", borderBottom: "1px solid var(--border-subtle)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div style={{ fontWeight: 700, fontSize: 13, color: "var(--text-primary)" }}>
                Activity
                {filteredActivity.length !== activity.length && (
                  <span style={{ fontSize: 11, fontWeight: 400, color: "var(--text-muted)", marginLeft: 6, fontFamily: "var(--font-mono)" }}>
                    ({filteredActivity.length}/{activity.length})
                  </span>
                )}
              </div>
              {!isClosed && <button onClick={loadActivity} style={{ fontSize: 11, color: "var(--accent)", background: "none", border: "none", cursor: "pointer" }}>↻ Refresh</button>}
            </div>

            {/* Activity filters */}
            <div style={{ padding: "var(--space-sm) var(--space-md)", borderBottom: "1px solid var(--border-subtle)", display: "flex", flexDirection: "column", gap: "var(--space-xs)", flexShrink: 0 }}>
              {/* Search */}
              <input
                value={actSearch} onChange={e => setActSearch(e.target.value)}
                placeholder="🔍 Search by story or user…"
                style={{ ...S.input, marginBottom: 0, fontSize: 12 }}
              />
              <div style={{ display: "flex", gap: "var(--space-xs)" }}>
                {/* By actor — datalist for large user sets */}
                <datalist id="act-actors-list">
                  {[...new Set(activity.map(e => e.actor_name).filter(Boolean))].map(n => (
                    <option key={n} value={n}>{n}</option>
                  ))}
                </datalist>
                <input list="act-actors-list" value={actActor}
                  onChange={e => setActActor(e.target.value)}
                  placeholder="Updated by…"
                  style={{ ...S.input, marginBottom: 0, fontSize: 11, flex: 1 }} />
                {/* By action */}
                <select value={actAction} onChange={e => setActAction(e.target.value)}
                  style={{ ...S.input, marginBottom: 0, fontSize: 11, flex: 1 }}>
                  <option value="">All actions</option>
                  <option value="created_story">Created</option>
                  <option value="updated_story">Updated</option>
                  <option value="moved_story">Moved</option>
                  <option value="committed">Committed</option>
                  <option value="closed_board">Board closed</option>
                </select>
              </div>
              {/* Date range */}
              <div style={{ display: "flex", gap: "var(--space-xs)" }}>
                {(["all","today","week","month"] as const).map(d => (
                  <button key={d} onClick={() => setActDate(d)}
                    style={{ flex: 1, padding: "4px 0", fontSize: 10, fontFamily: "var(--font-mono)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", cursor: "pointer", textTransform: "uppercase",
                      background: actDate === d ? "var(--accent-dim)" : "transparent",
                      color: actDate === d ? "var(--accent)" : "var(--text-muted)",
                    }}>
                    {d === "all" ? "All time" : d === "today" ? "Today" : d === "week" ? "7d" : "30d"}
                  </button>
                ))}
              </div>
              {/* Clear filters */}
              {(actSearch || actActor || actAction || actDate !== "all") && (
                <button onClick={() => { setActSearch(""); setActActor(""); setActAction(""); setActDate("all"); }}
                  style={{ fontSize: 10, color: "var(--accent)", background: "none", border: "none", cursor: "pointer", padding: 0, textAlign: "left", fontFamily: "var(--font-mono)" }}>
                  ✕ Clear filters
                </button>
              )}
            </div>

            {/* Artifact commit history if linked */}
            {board.artifact_id && (
              <div style={{ borderBottom: "1px solid var(--border-subtle)", flexShrink: 0 }}>
                <div style={{ padding: "var(--space-xs) var(--space-lg)", fontSize: 11, fontWeight: 700, color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.04em" }}>
                  Commits · {board.artifact_type}
                </div>
                <div style={{ maxHeight: 140, overflow: "auto", padding: "0 var(--space-lg) var(--space-sm)" }}>
                  <CommitHistory componentType={board.artifact_type ?? ""} componentId={board.artifact_id} limit={8} compact />
                </div>
              </div>
            )}

            {/* Activity timeline */}
            <div style={{ flex: 1, overflow: "auto" }}>
              {actLoading ? (
                <div style={{ padding: "var(--space-xl) var(--space-lg)", fontSize: 12, color: "var(--text-muted)" }}>Loading activity…</div>
              ) : filteredActivity.length === 0 ? (
                <div style={{ padding: "var(--space-xl) var(--space-lg)", fontSize: 12, color: "var(--text-muted)" }}>
                  {activity.length > 0 ? "No activity matches your filters." : "No activity yet."}
                </div>
              ) : (
                <div style={{ padding: "var(--space-md) var(--space-lg)" }}>
                  {filteredActivity.map((evt, i) => <ActivityRow key={evt.id + i} evt={evt} />)}
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Story modal */}
      {editStory !== null && (
        <StoryModal
          story={editStory === "new" ? null : editStory}
          boardId={board.id}
          users={users}
          readOnly={isReadOnly}
          onClose={() => setEdit(null)}
          onSave={saveStory}
          onDelete={deleteStory}
        />
      )}

      {/* Edit board modal */}
      {showEditBoard && (
        <BoardModal
          existing={board}
          onClose={() => setShowEditBoard(false)}
          onSave={updated => { onBoardUpdate(updated); setShowEditBoard(false); }}
        />
      )}
    </div>
  );
}

/* ── Root ────────────────────────────────────────────────────────── */

export default function HxWork() {
  const { user } = useAuth();
  const currentUser = user?.username ?? user?.email ?? "You";

  const [boards, setBoards]     = useState<Board[]>([]);
  const [selected, setSelected] = useState<Board | null>(null);
  const [showNew, setShowNew]   = useState(false);
  const [users, setUsers]       = useState<DirectoryUser[]>([]);

  useEffect(() => {
    req("GET", `${API}/boards`).then((d: Board[]) => {
      const list = (d ?? []).map(b => ({
        ...b,
        status: isBoardClosed(b) ? "closed" as BoardStatus : (b.status ?? "active") as BoardStatus,
        closed_at: b.closed_at ?? null,
      }));
      setBoards(list);
      if (list.length && !selected) setSelected(list[0]);
    }).catch(() => {});
    fetchUsers().then(setUsers);
  }, []);

  const handleDeleteBoard = async (b: Board) => {
    if (!window.confirm(`Delete board "${b.name}" and all its stories? This cannot be undone.`)) return;
    try {
      await req("DELETE", `${API}/boards/${b.id}`);
      setBoards(prev => {
        const next = prev.filter(x => x.id !== b.id);
        setSelected(sel => sel?.id === b.id ? (next[0] ?? null) : sel);
        return next;
      });
    } catch (ex: any) { alert(`Failed to delete board: ${ex.message}`); }
  };

  const handleBoardUpdate = (updated: Board) => {
    setBoards(prev => prev.map(b => b.id === updated.id ? { ...b, ...updated } : b));
    setSelected(sel => sel?.id === updated.id ? { ...sel, ...updated } : sel);
  };

  return (
    <div style={S.page}>
      <Sidebar boards={boards} selected={selected} onSelect={setSelected} onNew={() => setShowNew(true)} onDelete={handleDeleteBoard} />
      {selected ? (
        <BoardView key={selected.id} board={selected} users={users} currentUser={currentUser} onBoardUpdate={handleBoardUpdate} />
      ) : (
        <div style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "flex-start", justifyContent: "center", gap: "var(--space-md)", padding: "var(--space-2xl)", color: "var(--text-muted)" }}>
          <div style={{ fontSize: 36 }}>📋</div>
          <div style={{ fontSize: 15, fontWeight: 700, color: "var(--text-primary)" }}>Select or create a dev board</div>
          <div style={{ fontSize: 13, maxWidth: 380, color: "var(--text-secondary)", lineHeight: 1.6 }}>
            Each board tracks development of one artifact (a Case Type, Form, Integration, etc.). Stories advance automatically when you commit changes to the linked artifact.
          </div>
          <button style={{ ...S.btn, ...S.btnP, marginTop: "var(--space-sm)", padding: "9px 20px", fontSize: 13 }} onClick={() => setShowNew(true)}>
            + New Board
          </button>
        </div>
      )}
      {showNew && (
        <BoardModal
          onClose={() => setShowNew(false)}
          onSave={b => {
            const board = { ...b, status: (b.status ?? "active") as BoardStatus, closed_at: b.closed_at ?? null };
            setBoards(prev => [...prev, board]);
            setSelected(board);
            setShowNew(false);
          }}
        />
      )}
    </div>
  );
}
