// HELIX P34 — Escalation Tree Editor (React Flow)
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import ReactFlow, {
  Background, Controls, MiniMap,
  addEdge, useEdgesState, useNodesState,
  Connection, Edge, Node,
} from "reactflow";
import "reactflow/dist/style.css";
import { useBranchMode, useCommit, useCurrentUserGroups } from "@shared/hooks";
import { BranchModeBanner, CommitHistory, CommitModal, ReviewerPicker } from "@shared/components";
import type { CommitRecord } from "@shared/components/CommitHistory";
import { createBranch } from "@shared/api/client";

type Scope = "global" | "case_type";

type TreeLevel = {
  level: number;
  name: string;
  trigger: { type: "goal_pct" | "deadline_pct" | "fixed_duration" | "at_breach"; value: any };
  actions: Array<{
    type: "notify" | "reassign" | "priority" | "status";
    target_type?: string; target_id?: string; message?: string; set?: string;
  }>;
};

type Tree = {
  id: string;
  name: string;
  description: string;
  scope: Scope;
  case_type_id: string | null;
  tenant_id: string | null;
  tree_json: { levels: TreeLevel[] };
  is_active: boolean;
};

function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}

async function apiJSON<T>(url: string, opts: RequestInit = {}): Promise<T> {
  const r = await fetch(url, { ...opts, headers: { ..._authHdr(), ...opts.headers } });
  if (!r.ok) throw new Error(`${url} → ${r.status}`);
  return r.json();
}

const levelsToGraph = (levels: TreeLevel[]) => {
  const nodes: Node[] = [
    {
      id: "start", type: "input",
      data: { label: "SLA Started" },
      position: { x: 0, y: 0 },
      style: { background: "#2a7", color: "white", padding: 10, borderRadius: 6, border: "none" },
    },
  ];
  const edges: Edge[] = [];
  levels.sort((a, b) => a.level - b.level).forEach((lvl, i) => {
    const id = `l${lvl.level}`;
    const triggerLabel = lvl.trigger.type === "goal_pct" ? `${lvl.trigger.value}% of goal`
      : lvl.trigger.type === "deadline_pct" ? `${lvl.trigger.value}% of deadline`
      : lvl.trigger.type === "at_breach" ? "on breach"
      : `after ${lvl.trigger.value}`;
    nodes.push({
      id,
      data: { label: (
        <div style={{ textAlign: "left" }}>
          <strong>L{lvl.level}: {lvl.name}</strong>
          <div style={{ fontSize: 11, color: "#666" }}>Trigger: {triggerLabel}</div>
          <div style={{ fontSize: 11, color: "#666" }}>Actions: {lvl.actions.length}</div>
        </div>
      ) },
      position: { x: 0, y: (i + 1) * 130 },
      style: { background: "#fff", border: "2px solid #4a6cf7", borderRadius: 6, padding: 10, width: 240 },
    });
    edges.push({
      id: `e-${i}`,
      source: i === 0 ? "start" : `l${levels[i - 1].level}`,
      target: id,
      animated: true,
    });
  });
  return { nodes, edges };
};

export default function EscalationEditor() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const branchId = searchParams.get("branch");
  const branchMode = useBranchMode(branchId);
  const myGroups = useCurrentUserGroups();

  const [trees, setTrees] = useState<Tree[]>([]);
  const [current, setCurrent] = useState<Tree | null>(null);
  const [savedCurrent, setSavedCurrent] = useState<Tree | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [restoreBanner, setRestoreBanner] = useState<string | null>(null);

  const { commitOpen, commitSaving, requestCommit, handleCommit, cancelCommit } =
    useCommit("escalation", current?.id ?? "", current?.name ?? "Escalation Tree");

  // Branch creation inline state
  const [branchingId, setBranchingId]     = useState<string | null>(null);
  const [branchName, setBranchName]       = useState("");
  const [branchReviewer, setBranchReviewer] = useState("");
  const [branchBusy, setBranchBusy]       = useState(false);
  const [branchCreated, setBranchCreated] = useState<any>(null);
  const [branchErr, setBranchErr]         = useState<string | null>(null);

  function openBranchForm(t: Tree) {
    const slug = t.name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
    setBranchingId(t.id);
    setBranchName(`fix/${slug}`);
    setBranchReviewer(""); setBranchCreated(null); setBranchErr(null);
  }

  async function handleBranchCreate(treeId: string, treeName: string) {
    if (!branchName.trim()) return;
    setBranchBusy(true); setBranchErr(null);
    try {
      const b = await createBranch({
        name: branchName.trim(),
        artifact_type: "escalation",
        artifact_id: treeId,
        description: `Branch of escalation tree "${treeName}"`,
        assigned_reviewer_id: branchReviewer.trim() || undefined,
      });
      setBranchCreated(b);
    } catch (e: any) { setBranchErr(e.message); }
    finally { setBranchBusy(false); }
  }

  // Preview state
  const [previewGoal, setPreviewGoal] = useState("PT4H");
  const [previewDeadline, setPreviewDeadline] = useState("PT24H");
  const [previewResult, setPreviewResult] = useState<any>(null);

  const { nodes: initialNodes, edges: initialEdges } = useMemo(
    () => levelsToGraph(current?.tree_json.levels ?? []),
    [current]
  );
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
  useEffect(() => { setNodes(initialNodes); setEdges(initialEdges); }, [initialNodes, initialEdges, setNodes, setEdges]);

  const onConnect = useCallback((conn: Connection) => setEdges((eds) => addEdge(conn, eds)), [setEdges]);

  const loadTrees = useCallback(async () => {
    setError(null);
    try {
      const data = await apiJSON<Tree[]>("/api/v1/escalation-trees?active_only=false");
      setTrees(data);
      if (!current && data.length > 0 && !branchId) {
        setCurrent(data[0]);
        setSavedCurrent(data[0]);
      }
    } catch (e: any) { setError(e.message); }
  }, [current, branchId]);

  useEffect(() => { loadTrees(); }, []);

  // Auto-select the tree the branch belongs to when branch loads
  useEffect(() => {
    if (!branchMode.branch?.artifact_id || !trees.length) return;
    const t = trees.find(t => t.id === branchMode.branch.artifact_id);
    if (!t) return;
    const snap = branchMode.branch.content_snapshot;
    const resolved = {
      ...t,
      tree_json: snap?.tree_json ?? t.tree_json,
      is_active: snap?.is_active ?? t.is_active,
    };
    setCurrent(resolved);
    setSavedCurrent(resolved);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [branchMode.branch?.artifact_id, trees.length]);

  async function createTree() {
    const name = prompt("Tree name?") || "";
    if (!name.trim()) return;
    const scope = confirm("Global scope? (Cancel = case-type)") ? "global" : "case_type";
    const case_type_id = scope === "case_type" ? prompt("Case type UUID?") || null : null;
    const body: any = {
      name, scope, case_type_id,
      description: "",
      tree_json: { levels: [
        { level: 1, name: "Primary reminder", trigger: { type: "goal_pct", value: 80 }, actions: [{ type: "notify", target_type: "current_assignee" }] },
        { level: 2, name: "Escalate to manager", trigger: { type: "goal_pct", value: 100 }, actions: [{ type: "reassign", target_type: "queue", target_id: "managers" }] },
      ] },
      is_active: true,
    };
    setBusy(true);
    try {
      const created = await apiJSON<Tree>("/api/v1/escalation-trees", {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
      });
      await loadTrees();
      setCurrent(created);
    } catch (e: any) { setError(e.message); }
    finally { setBusy(false); }
  }

  async function doSave(treeToSave: Tree) {
    if (branchMode.isBranchMode) {
      await branchMode.patchContent({ tree_json: treeToSave.tree_json, is_active: treeToSave.is_active });
    } else {
      await apiJSON(`/api/v1/escalation-trees/${treeToSave.id}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ name: treeToSave.name, description: treeToSave.description, tree_json: treeToSave.tree_json, is_active: treeToSave.is_active }),
      });
      setSavedCurrent(treeToSave);
      setRestoreBanner(null);
      await loadTrees();
    }
  }

  function saveCurrent() {
    if (!current) return;
    if (branchMode.isBranchMode) {
      setBusy(true);
      doSave(current).catch(e => setError(e.message)).finally(() => setBusy(false));
      return;
    }
    const before = { tree_json: savedCurrent?.tree_json ?? {}, is_active: savedCurrent?.is_active ?? true };
    const after  = { tree_json: current.tree_json, is_active: current.is_active };
    requestCommit(() => doSave(current), { before, after });
  }

  function handleRestoreRequest(commit: CommitRecord) {
    const after = (commit.diff_snapshot as any)?.after;
    if (!after || !current) return;
    const restored: Tree = {
      ...current,
      tree_json: after.tree_json ?? current.tree_json,
      is_active: after.is_active ?? current.is_active,
    };
    const when = new Date(commit.committed_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    setCurrent(restored);
    setRestoreBanner(`Previewing state from ${when} — "${commit.commit_message}". Save to apply.`);
  }

  async function runPreview() {
    if (!current) return;
    setBusy(true);
    try {
      const r = await apiJSON("/api/v1/escalation-trees/preview", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          tree_json: current.tree_json,
          goal_duration: previewGoal,
          deadline_duration: previewDeadline,
        }),
      });
      setPreviewResult(r);
    } catch (e: any) { setError(e.message); }
    finally { setBusy(false); }
  }

  function addLevel() {
    if (!current) return;
    const lvls = [...current.tree_json.levels];
    const nextLevel = lvls.length > 0 ? Math.max(...lvls.map(l => l.level)) + 1 : 1;
    lvls.push({ level: nextLevel, name: `Level ${nextLevel}`, trigger: { type: "goal_pct", value: 50 }, actions: [] });
    setCurrent({ ...current, tree_json: { levels: lvls } });
  }

  function removeLevel(levelNum: number) {
    if (!current) return;
    setCurrent({ ...current, tree_json: { levels: current.tree_json.levels.filter(l => l.level !== levelNum) } });
  }

  function updateLevel(levelNum: number, patch: Partial<TreeLevel>) {
    if (!current) return;
    setCurrent({
      ...current,
      tree_json: {
        levels: current.tree_json.levels.map(l => l.level === levelNum ? { ...l, ...patch } : l),
      },
    });
  }

  return (
    <div style={{ fontFamily: "system-ui, sans-serif", width: "100%", height: "100%", display: "flex", flexDirection: "column", boxSizing: "border-box" }}>
      {branchMode.isBranchMode && (
        <BranchModeBanner
          branch={branchMode.branch}
          saving={branchMode.saving}
          error={branchMode.error}
          accessGroupId={myGroups[0]}
          onSubmitForReview={branchMode.submitForReview}
          onRecall={branchMode.recall}
        />
      )}
      <div style={{ padding: 20, flex: 1, display: "flex", flexDirection: "column", minHeight: 0 }}>
      {error && <div style={{ color: "#c33", marginBottom: 12, flexShrink: 0 }}>⚠ {error}</div>}

      <div style={{ display: "grid", gridTemplateColumns: "280px 1fr 320px", gap: 16, flex: 1, minHeight: 0 }}>
        {/* Left: tree list */}
        <div style={{ ...card, overflow: "auto" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <h2 style={h2}>Trees ({trees.length})</h2>
            <button onClick={createTree} style={btn()} disabled={busy}>+ New</button>
          </div>
          {trees.map(t => (
            <div key={t.id} style={{ marginBottom: 6 }}>
              <div onClick={() => setCurrent(t)}
                style={{
                  padding: 10, border: "1px solid #eee", borderRadius: 4, cursor: "pointer",
                  background: current?.id === t.id ? "#eef3ff" : "#fff",
                  display: "flex", alignItems: "flex-start", gap: 6,
                }}>
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 600, fontSize: 13 }}>{t.name}</div>
                  <div style={{ fontSize: 11, color: "#666" }}>
                    {t.scope} · {t.tree_json.levels.length} levels · {t.is_active ? "active" : "inactive"}
                  </div>
                </div>
                <button
                  onClick={e => { e.stopPropagation(); openBranchForm(t); }}
                  style={{ background: "none", border: "1px solid #ccc", borderRadius: 3, cursor: "pointer", fontSize: 10, padding: "2px 5px", color: "#666", flexShrink: 0 }}
                  title="Create branch">⎇</button>
              </div>
              {branchingId === t.id && (
                <div style={{ padding: 8, background: "#f8f8fb", border: "1px solid #eee", borderRadius: 4, marginTop: 2, fontSize: 11 }}>
                  {branchCreated ? (
                    <>
                      <div style={{ padding: "4px 8px", background: "#dcfce7", color: "#16a34a", borderRadius: 4, marginBottom: 6 }}>
                        ✓ Branch <b>{branchCreated.name}</b> created
                      </div>
                      <div style={{ display: "flex", gap: 6 }}>
                        <button
                          onClick={() => navigate(`/escalation?branch=${branchCreated.id}`)}
                          style={{ padding: "4px 8px", fontSize: 11, fontWeight: 600, background: "#4a6cf7", color: "#fff", border: "none", borderRadius: 3, cursor: "pointer" }}>
                          Open in Editor →
                        </button>
                        <button
                          onClick={() => setBranchingId(null)}
                          style={{ padding: "4px 8px", fontSize: 11, background: "none", border: "1px solid #ccc", borderRadius: 3, cursor: "pointer", color: "#666" }}>
                          Close
                        </button>
                      </div>
                    </>
                  ) : (
                    <>
                      {branchErr && <div style={{ color: "#ef4444", marginBottom: 4 }}>✗ {branchErr}</div>}
                      <input
                        style={{ width: "100%", padding: "4px 6px", border: "1px solid #ccc", borderRadius: 3, fontSize: 11, fontFamily: "monospace", marginBottom: 4, boxSizing: "border-box" as const }}
                        placeholder="branch name"
                        value={branchName}
                        onChange={e => setBranchName(e.target.value)}
                        onClick={e => e.stopPropagation()}
                      />
                      <div onClick={e => e.stopPropagation()} style={{ marginBottom: 6 }}>
                        <ReviewerPicker
                          value={branchReviewer}
                          onChange={setBranchReviewer}
                          accessGroupId={myGroups[0]}
                          placeholder="Reviewer (optional)"
                        />
                      </div>
                      <div style={{ display: "flex", gap: 6 }}>
                        <button
                          disabled={branchBusy || !branchName.trim()}
                          onClick={e => { e.stopPropagation(); handleBranchCreate(t.id, t.name); }}
                          style={{ flex: 1, padding: "4px 0", fontSize: 11, fontWeight: 600, background: "#4a6cf7", color: "#fff", border: "none", borderRadius: 3, cursor: "pointer", opacity: (branchBusy || !branchName.trim()) ? 0.6 : 1 }}>
                          {branchBusy ? "…" : "Create Branch"}
                        </button>
                        <button
                          onClick={e => { e.stopPropagation(); setBranchingId(null); }}
                          style={{ padding: "4px 6px", fontSize: 11, background: "none", border: "1px solid #ccc", borderRadius: 3, cursor: "pointer", color: "#666" }}>
                          ✕
                        </button>
                      </div>
                    </>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>

        {/* Center: graph — fills the full height of the grid row */}
        <div style={{ ...card, padding: 0, minHeight: 0 }}>
          <ReactFlow nodes={nodes} edges={edges}
            onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={onConnect}
            fitView style={{ width: "100%", height: "100%" }}>
            <Background />
            <Controls />
            <MiniMap />
          </ReactFlow>
        </div>

        {/* Right: property panel */}
        <div style={{ ...card, overflow: "auto" }}>
          {!current && <div style={{ color: "#888" }}>Select or create a tree.</div>}
          {current && (
            <>
              <h2 style={h2}>{current.name}</h2>
              <label style={lbl}>Name</label>
              <input style={inp} value={current.name} onChange={e => setCurrent({ ...current, name: e.target.value })} />
              <label style={lbl}>Description</label>
              <textarea style={{ ...inp, minHeight: 60 }} value={current.description}
                onChange={e => setCurrent({ ...current, description: e.target.value })} />
              <label style={{ ...lbl, display: "flex", alignItems: "center", gap: 6 }}>
                <input type="checkbox" checked={current.is_active}
                  onChange={e => setCurrent({ ...current, is_active: e.target.checked })} />
                Active
              </label>

              <hr style={{ border: "none", borderTop: "1px solid #eee", margin: "12px 0" }} />
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <strong style={{ fontSize: 13 }}>Levels</strong>
                <button onClick={addLevel} style={btn()}>+ Add</button>
              </div>
              {current.tree_json.levels.sort((a, b) => a.level - b.level).map(lvl => (
                <div key={lvl.level} style={{ border: "1px solid #eee", borderRadius: 4, padding: 8, marginTop: 8 }}>
                  <div style={{ display: "flex", justifyContent: "space-between" }}>
                    <strong>L{lvl.level}</strong>
                    <button onClick={() => removeLevel(lvl.level)} style={{ ...btn(), color: "#c33", padding: "2px 6px" }}>✕</button>
                  </div>
                  <input style={inp} value={lvl.name}
                    onChange={e => updateLevel(lvl.level, { name: e.target.value })} />
                  <div style={{ display: "flex", gap: 4, marginTop: 4 }}>
                    <select value={lvl.trigger.type}
                      onChange={e => updateLevel(lvl.level, { trigger: { ...lvl.trigger, type: e.target.value as any } })}
                      style={{ ...inp, flex: 1 }}>
                      <option value="goal_pct">% of goal</option>
                      <option value="deadline_pct">% of deadline</option>
                      <option value="fixed_duration">fixed (ISO 8601)</option>
                      <option value="at_breach">at breach</option>
                    </select>
                    {lvl.trigger.type !== "at_breach" && (
                      <input style={{ ...inp, width: 80 }} value={String(lvl.trigger.value ?? "")}
                        onChange={e => updateLevel(lvl.level, { trigger: { ...lvl.trigger, value: e.target.value } })} />
                    )}
                  </div>
                  <div style={{ fontSize: 11, color: "#666", marginTop: 4 }}>
                    {lvl.actions.length} action(s) — edit via JSON (see docs)
                  </div>
                </div>
              ))}

              {restoreBanner && (
                <div style={{ marginTop: 10, padding: "7px 10px", background: "#fefce8", border: "1px solid #fde047", borderRadius: 4, fontSize: 11, color: "#854d0e" }}>
                  ↩ {restoreBanner}
                  <button onClick={() => { setCurrent(savedCurrent); setRestoreBanner(null); }}
                    style={{ marginLeft: 8, background: "none", border: "1px solid #ca8a04", borderRadius: 3, padding: "1px 7px", fontSize: 10, cursor: "pointer", color: "#854d0e" }}>
                    Discard
                  </button>
                </div>
              )}

              <div style={{ display: "flex", gap: 6, marginTop: 12 }}>
                <button
                  onClick={saveCurrent}
                  style={{ ...btn(), flex: 1, background: "#4a6cf7", color: "white", opacity: (busy || commitSaving || branchMode.isLocked || branchMode.isReadOnly) ? 0.6 : 1 }}
                  disabled={busy || commitSaving || branchMode.saving || branchMode.isLocked || branchMode.isReadOnly}>
                  {(branchMode.saving || commitSaving) ? "Saving…" : branchMode.isLocked ? "Locked" : branchMode.isReadOnly ? "Read Only" : branchMode.isBranchMode ? "Save to Branch" : "Commit"}
                </button>
                {!branchMode.isBranchMode && current && (
                  <button onClick={() => setHistoryOpen(h => !h)} style={{ ...btn(), color: historyOpen ? "#4a6cf7" : undefined }}>
                    🕐 History
                  </button>
                )}
              </div>

              <hr style={{ border: "none", borderTop: "1px solid #eee", margin: "12px 0" }} />
              <h3 style={{ fontSize: 13, margin: "8px 0" }}>Preview schedule</h3>
              <label style={lbl}>Goal duration (ISO 8601)</label>
              <input style={inp} value={previewGoal} onChange={e => setPreviewGoal(e.target.value)} />
              <label style={lbl}>Deadline duration</label>
              <input style={inp} value={previewDeadline} onChange={e => setPreviewDeadline(e.target.value)} />
              <button onClick={runPreview} style={{ ...btn(), marginTop: 6, width: "100%" }}>Compute firing times</button>
              {previewResult && (
                <pre style={{ background: "#f5f5f5", padding: 8, marginTop: 8, fontSize: 10, maxHeight: 200, overflow: "auto" }}>
                  {JSON.stringify(previewResult.schedule, null, 2)}
                </pre>
              )}
            </>
          )}
        </div>
      </div>
      </div>

      {/* History slide-out */}
      {historyOpen && current && (
        <div style={{
          position: "fixed", right: 0, top: 0, bottom: 0, width: 380, zIndex: 200,
          background: "var(--bg-panel, #fff)", borderLeft: "1px solid #e3e3e8",
          display: "flex", flexDirection: "column", boxShadow: "-4px 0 20px rgba(0,0,0,0.08)",
        }}>
          <div style={{ padding: "14px 16px", borderBottom: "1px solid #e3e3e8", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div>
              <div style={{ fontWeight: 700, fontSize: 14 }}>History</div>
              <div style={{ fontSize: 11, color: "#888", fontFamily: "monospace" }}>{current.name}</div>
            </div>
            <button onClick={() => setHistoryOpen(false)} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 18, color: "#888" }}>✕</button>
          </div>
          <div style={{ flex: 1, overflow: "auto", padding: "12px 16px" }}>
            <CommitHistory
              componentType="escalation"
              componentId={current.id}
              onRestoreRequest={handleRestoreRequest}
            />
          </div>
        </div>
      )}

      <CommitModal
        open={commitOpen}
        saving={commitSaving}
        componentType="escalation"
        componentName={current?.name ?? "Escalation Tree"}
        onCommit={handleCommit}
        onCancel={cancelCommit}
      />
    </div>
  );
}

const card: React.CSSProperties = { background: "#fff", border: "1px solid #e3e3e8", borderRadius: 8, padding: 14 };
const h2: React.CSSProperties = { margin: 0, fontSize: 14, color: "#333" };
const lbl: React.CSSProperties = { fontSize: 11, color: "#666", display: "block", marginTop: 8 };
const inp: React.CSSProperties = { width: "100%", padding: "5px 8px", fontSize: 12, border: "1px solid #ccc", borderRadius: 3, marginTop: 2 };
function btn(): React.CSSProperties {
  return { padding: "5px 10px", border: "1px solid #ccc", borderRadius: 3, background: "#fafafa", fontSize: 12, cursor: "pointer" };
}
